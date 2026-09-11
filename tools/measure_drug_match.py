# -*- coding: utf-8 -*-
"""Measure the structured drug matcher against mappings a clinician already confirmed.

The claim in `docs/DECISIONS.md` is that the pass reproduces a physician's decisions and
that where it differs, it differs in the spelling of a dose form or the reading of a
concentration -- never in which drug or which strength. That is a measurement, so it
should be re-derivable rather than remembered.

The physician's decisions live in `mappings/`, which the pass would otherwise short-cut,
so they are held out here: each source string is matched from scratch and compared with
what the person chose. Disagreements are then classified using `DRUG_STRENGTH` rather
than by reading names, because two concepts with the same ingredient set and the same
strength are the same drug however differently they are written.

    python3 tools/measure_drug_match.py --vocabulary <athena dir> --dataset datasets/ctpe.yaml

With `--audit`, it also runs a second, independent check over every drug term in a
build's review queue: it re-derives a strength from the matched concept's *name* -- a
field written by a different process from the numbers in `DRUG_STRENGTH` -- and compares
that with the source string. The numbers agreeing with themselves proves nothing; the
name agreeing with the source is what caught a formless concentration matching a tablet.

    python3 tools/measure_drug_match.py --vocabulary <athena dir> --dataset datasets/ctpe.yaml

Writes `results/drug_match.json`. Exit code is non-zero if any disagreement turns out to
be a different drug or a different strength, or if the name audit finds a mismatch,
because those are the failures that matter.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ehr2trace.config import load_dataset_config  # noqa: E402
from ehr2trace.drug_match import (  # noqa: E402
    DrugIndex, _canonical, _round, _vocabulary_signature, match_drug,
)
from ehr2trace.terminology import Vocabulary  # noqa: E402


def strength_index(con):
    """ingredient set and strength signature per drug concept, straight from the table."""
    signature: dict[int, set] = collections.defaultdict(set)
    ingredients: dict[int, set] = collections.defaultdict(set)
    rows = con.execute(
        """
        SELECT CAST(d.drug_concept_id AS BIGINT), CAST(d.ingredient_concept_id AS BIGINT),
               TRY_CAST(d.amount_value AS DOUBLE), au.concept_code,
               TRY_CAST(d.numerator_value AS DOUBLE), nu.concept_code,
               TRY_CAST(d.denominator_value AS DOUBLE), du.concept_code
        FROM DRUG_STRENGTH d
        LEFT JOIN CONCEPT au ON au.concept_id = d.amount_unit_concept_id
        LEFT JOIN CONCEPT nu ON nu.concept_id = d.numerator_unit_concept_id
        LEFT JOIN CONCEPT du ON du.concept_id = d.denominator_unit_concept_id
        WHERE d.invalid_reason IS NULL OR d.invalid_reason = ''
        """
    ).fetchall()
    for drug, ingredient, amount, amount_unit, num, num_unit, den, den_unit in rows:
        signature[drug].add((ingredient, _vocabulary_signature(
            amount, amount_unit, num, num_unit, den, den_unit)))
        ingredients[drug].add(ingredient)
    return signature, ingredients


#: How the vocabulary writes a unit inside a concept *name*, which is not always how it
#: writes it in `DRUG_STRENGTH`: `1000 UNT/ML Injection` against unit concept `[U]`.
NAME_UNITS = {"MG": "mg", "G": "g", "MCG": "ug", "ML": "mL", "UNT": "[U]", "UNIT": "[U]",
              "IU": "[U]", "MEQ": "10*-3.eq", "MMOL": "mmol", "ACTUAT": "{actuat}", "%": "%",
              "HR": "h", "H": "h", "CM2": "cm2"}
_UNIT_ALT = "|".join(sorted(NAME_UNITS, key=len, reverse=True))
#: A denominator in a concept name is not always a volume: an inhaler is dosed per
#: actuation and a patch per hour. Reading `0.875 MG/HR` as a plain 0.875 MG amount is
#: what made this audit report a correct nicotine patch as a disagreement.
NAME_RATIO = re.compile(rf"([\d.]+)\s*({_UNIT_ALT})\s*/\s*([\d.]*)\s*({_UNIT_ALT})\b", re.I)
NAME_AMOUNT = re.compile(rf"([\d.]+)\s*({_UNIT_ALT})\b", re.I)


def name_strength(name: str):
    """The strength the concept's own name states, or None if it states none."""
    match = NAME_RATIO.search(name)
    if match:
        top = _canonical(float(match.group(1)), NAME_UNITS[match.group(2).upper()])
        bottom = _canonical(float(match.group(3) or 1), NAME_UNITS[match.group(4).upper()])
        if top and bottom and bottom[1]:
            return ("ratio", top[0], bottom[0], _round(top[1] / bottom[1]))
        return None
    match = NAME_AMOUNT.search(name)
    if match:
        value = _canonical(float(match.group(1)), NAME_UNITS[match.group(2).upper()])
        return ("amount", value[0], _round(value[1])) if value else None
    return None


def source_readings(parsed):
    """Every strength reading the matcher is allowed to take from this source string."""
    if len(parsed.components) != 1 or parsed.components[0].strength is None:
        return []
    strength = parsed.components[0].strength
    if strength.kind == "amount":
        value = _canonical(strength.value, strength.unit)
        return [("amount", value[0], _round(value[1]))] if value else []
    top = _canonical(strength.value, strength.unit)
    bottom = _canonical(strength.denominator or 1.0, strength.denominator_unit or "mL")
    if not top or not bottom or not bottom[1]:
        return []
    out = [("ratio", top[0], bottom[0], _round(top[1] / bottom[1]))]
    if strength.origin == "explicit":
        out.append(("amount", top[0], _round(top[1])))
    if strength.origin == "percent":
        # weight in weight, which RxNorm states per gram for an ointment or a gel
        out.append(("ratio", "mass", "mass", _round(strength.value / 100)))
    return out


#: The matcher compares strengths within RxNorm's own rounding (`2.5 MG/3 ML` is its
#: `0.83 MG/ML`), and so must the audit, or it reports the rounding as a disagreement.
TOLERANCE = 0.01


def _agrees(reading, stated) -> bool:
    return (len(reading) == len(stated) and reading[:-1] == stated[:-1]
            and abs(reading[-1] - stated[-1]) <= TOLERANCE * max(abs(reading[-1]), abs(stated[-1])))


def audit_names(index, pending: Path, noise: list[str], width: int | None = None) -> dict:
    """Check every match in a build's queue against the matched concept's own name."""
    terms = [(int(r["occurrences"]), r["source_string"])
             for r in csv.DictReader(open(pending, encoding="utf-8"))
             if r.get("event_kind") in ("drug_order", "drug_admin")]
    settled = comparable = agree = 0
    rows = 0
    routes: collections.Counter = collections.Counter()
    disagreements = []
    for occurrences, source in terms:
        parsed, status, matches = match_drug(index, source, noise, width)
        if status != "unique":
            continue
        settled += 1
        rows += occurrences
        routes[matches[0].route] += 1
        readings = source_readings(parsed)
        stated = name_strength(matches[0].concept_name)
        if not readings or stated is None:
            continue
        comparable += 1
        if any(_agrees(r, stated) for r in readings):
            agree += 1
        else:
            disagreements.append({
                "source_string": source, "concept_name": matches[0].concept_name,
                "route": matches[0].route, "occurrences": occurrences,
                "source_readings": readings, "concept_name_states": stated,
            })
    return {"terms": len(terms), "settled": settled, "rows_settled": rows,
            "routes": dict(routes.most_common()), "comparable": comparable,
            "name_agrees": agree, "disagreements": disagreements[:50],
            "disagreement_count": len(disagreements)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocabulary", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets" / "ctpe.yaml")
    parser.add_argument("--mappings", type=Path, default=ROOT / "mappings" / "drug.csv")
    parser.add_argument("--audit", type=Path, default=None,
                        help="a build's review/pending.csv, to audit every match by name")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "drug_match.json")
    parser.add_argument("--gold-through", default=None, metavar="DATE",
                        help="count only mappings decided on or before this date as the confirmed "
                             "set; later rows are decisions recorded because the matcher abstains "
                             "on them by design, and measuring it against those measures nothing")
    args = parser.parse_args()

    gold = [r for r in csv.DictReader(open(args.mappings, encoding="utf-8"))
            if r.get("concept_id")
            and (args.gold_through is None or (r.get("decided_on") or "") <= args.gold_through)]
    if not gold:
        print("no confirmed drug mappings to measure against", file=sys.stderr)
        return 1

    terminology = load_dataset_config(args.dataset).terminology
    noise = list(terminology.drug_name_noise)
    width = terminology.drug_name_truncated_at
    vocabulary = Vocabulary.open(args.vocabulary)
    if not getattr(vocabulary, "available", False):
        print(f"no vocabulary at {args.vocabulary}", file=sys.stderr)
        return 1
    index = DrugIndex(vocabulary.con)
    signature, ingredients = strength_index(vocabulary.con)

    outcome = collections.Counter()
    examples: dict[str, list[dict]] = collections.defaultdict(list)
    for row in gold:
        _parsed, status, matches = match_drug(index, row["source_string"], noise, width)
        if status != "unique":
            outcome[f"abstained_{status}"] += 1
            continue
        chosen, wanted = matches[0].concept_id, int(row["concept_id"])
        if chosen == wanted:
            outcome["exact_agreement"] += 1
            continue
        if ingredients[chosen] != ingredients[wanted]:
            kind = "different_ingredient"
        elif signature[chosen] != signature[wanted]:
            kind = "same_ingredient_other_strength_reading"
        else:
            kind = "same_drug_other_spelling"
        outcome[kind] += 1
        examples[kind].append({
            "source_string": row["source_string"],
            "clinician": row["concept_name"],
            "matcher": matches[0].concept_name,
            "route": matches[0].route,
        })

    resolved = sum(v for k, v in outcome.items() if not k.startswith("abstained_"))
    report = {
        "vocabulary_version": vocabulary.version,
        "confirmed_mappings": len(gold),
        "resolved": resolved,
        "outcome": dict(sorted(outcome.items())),
        "share_of_resolved": {k: round(v / resolved, 4)
                              for k, v in sorted(outcome.items())
                              if not k.startswith("abstained_")} if resolved else {},
        "examples": {k: v[:20] for k, v in examples.items()},
    }
    if args.audit:
        report["name_audit"] = audit_names(index, args.audit, noise, width)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    vocabulary.close()

    print(f"{len(gold):,} confirmed mappings; {resolved:,} resolved by the matcher")
    for key, value in sorted(outcome.items()):
        share = f"  {value / resolved:6.1%}" if resolved and not key.startswith("abstained_") else ""
        print(f"  {key:<42} {value:>4}{share}")
    print(f"wrote {args.out}")
    audit = report.get("name_audit")
    if audit:
        print(f"\nname audit over {audit['terms']:,} drug terms: {audit['settled']:,} settled "
              f"({audit['rows_settled']:,} rows), {audit['comparable']:,} comparable by name, "
              f"{audit['disagreement_count']:,} disagree")
        for row in audit["disagreements"][:10]:
            print(f"  {row['occurrences']:>8,} {row['source_string'][:48]:<48} -> "
                  f"{row['concept_name'][:44]}")
    wrong = outcome["different_ingredient"] + (audit["disagreement_count"] if audit else 0)
    if wrong:
        print(f"\n{wrong} mapping(s) chose a different drug or contradict the concept's own "
              "name -- these are the failures that matter", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
