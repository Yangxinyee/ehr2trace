# -*- coding: utf-8 -*-
"""Check an Athena vocabulary download before trusting it.

A vocabulary is a licensed multi-gigabyte download that arrives by email, and the two
ways it goes wrong are quiet: a truncated file, or a bundle that omits a vocabulary the
dataset actually needs. Both surface later as "everything is concept_id 0", which looks
exactly like having no vocabulary at all.

This answers three questions before a single row is mapped:

  1. are the required tables present and readable?
  2. does it include every vocabulary *this dataset* needs?
  3. how much of *this* dataset's terminology would map with it?

The third one is the useful one -- it estimates the answer without running the ETL.

The second one is derived rather than listed. A dataset's source vocabularies are
exactly the `code_system` values its YAML declares, and its standard targets follow from
the domains its event kinds land in; both are read from the config. A hardcoded list is
how a bundle carrying ICD10CM alone once passed this check for a dataset that also
declares ICD9CM, ICD9Proc and ICD10PCS, putting 24,054 billing codes into a review queue
that no person should ever have been shown.

    python3 tools/check_vocabulary.py /path/to/unzipped/vocab [--dataset ctpe]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2trace.terminology import DOMAIN_FOR_KIND, _unpunctuated  # noqa: E402

REQUIRED = ("CONCEPT", "CONCEPT_RELATIONSHIP", "VOCABULARY", "DOMAIN", "CONCEPT_CLASS", "RELATIONSHIP")
RECOMMENDED = ("CONCEPT_ANCESTOR", "DRUG_STRENGTH")

#: Standard target vocabularies, keyed by OMOP domain. Which vocabulary a *standard*
#: concept lives in is a property of OMOP, not of anybody's export, so this is reached
#: through DOMAIN_FOR_KIND instead of being written per dataset. The names under one
#: domain are alternatives: any one of them can carry the target concept.
#:
#: UCUM is deliberately absent. Nothing in src/ resolves a unit to a concept, so
#: demanding it here would report a gap this converter does not actually have.
STANDARD_TARGETS = {
    "Condition": ("SNOMED",),
    "Drug": ("RxNorm", "RxNorm Extension"),
    "Measurement": ("LOINC", "SNOMED"),
    "Procedure": ("SNOMED",),
    "Observation": ("SNOMED",),
    "Visit": ("Visit",),
}


def needed_vocabularies(cfg):
    """Config -> (source vocabulary -> sources declaring it, domain -> event kinds).

    The first half is the part only the dataset can answer: a `code_system` other than
    SOURCE names a vocabulary whose codes have to be looked up by code, and no amount of
    reading the converter tells you which ones a given export uses. The second half is
    the part only OMOP can answer.
    """
    sources: dict[str, list[str]] = {}
    domains: dict[str, set[str]] = {}
    for source_id, spec in sorted(cfg.sources.items()):
        system = (spec.code_system or "SOURCE").strip()
        if system and system.upper() != "SOURCE":
            sources.setdefault(system, []).append(source_id)
        domain = DOMAIN_FOR_KIND.get(spec.event_kind or "")
        if domain:
            domains.setdefault(domain, set()).add(spec.event_kind)
    return sources, domains


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--dataset", default="ctpe")
    parser.add_argument("--sample", type=int, default=2000, help="terms to test-map")
    args = parser.parse_args(argv)

    directory: Path = args.directory
    if not directory.is_dir():
        print(f"not a directory: {directory}")
        return 2

    from ehr2trace.config import find_dataset_config, load_dataset_config

    try:
        cfg = load_dataset_config(find_dataset_config(args.dataset))
    except Exception as exc:
        print(f"cannot read dataset config {args.dataset!r}: {exc}")
        print("the bundle can still be checked for readable tables, but what it *needs*")
        print("is a property of a dataset, so pass a --dataset that loads.")
        return 2

    print(f"vocabulary at {directory}")
    print(f"checked against {cfg.dataset_id} ({len(cfg.sources)} logical sources)\n")

    # 1. files present and non-trivial
    missing = []
    for table in REQUIRED + RECOMMENDED:
        hit = next((p for p in directory.glob("*") if p.stem.upper() == table), None)
        if hit is None:
            missing.append(table)
            print(f"  MISSING   {table}")
        else:
            size = hit.stat().st_size
            print(f"  ok        {table:<22} {size / 1e6:>10.1f} MB")
    hard = [t for t in missing if t in REQUIRED]
    if hard:
        print(f"\nrequired tables missing: {hard}. Re-download including them.")
        return 1

    # 2. what the bundle actually contains
    import duckdb

    con = duckdb.connect()
    concept = next(p for p in directory.glob("*") if p.stem.upper() == "CONCEPT")
    con.register(
        "concept",
        con.read_csv(str(concept), sep="\t", header=True, quotechar="", all_varchar=True),
    )
    total = con.execute("SELECT count(*) FROM concept").fetchone()[0]
    print(f"\n{total:,} concepts\n")
    rows = con.execute(
        "SELECT vocabulary_id, count(*) n FROM concept GROUP BY 1 ORDER BY n DESC LIMIT 15"
    ).fetchall()
    print("  included vocabularies (top 15)")
    for vocab, n in rows:
        print(f"    {vocab:<24} {n:>12,}")

    present = {r[0] for r in con.execute("SELECT DISTINCT vocabulary_id FROM concept").fetchall()}
    sources, domains = needed_vocabularies(cfg)
    gaps: list[str] = []

    print(f"\n  source vocabularies {cfg.dataset_id} declares")
    if not sources:
        print("    (none -- every source declares code_system SOURCE, so nothing is looked")
        print("     up by code and the whole queue rests on lexical recall plus review)")
    for vocab, source_ids in sorted(sources.items()):
        shown = ", ".join(source_ids[:3]) + (" ..." if len(source_ids) > 3 else "")
        if vocab in present:
            print(f"    ok        {vocab:<16} present       ({shown})")
        else:
            gaps.append(vocab)
            print(f"    MISSING   {vocab:<16} declared by   ({shown})")

    print(f"\n  standard targets for the domains {cfg.dataset_id} publishes into")
    for domain, kinds in sorted(domains.items()):
        options = STANDARD_TARGETS.get(domain, ())
        if not options:
            continue
        have = [v for v in options if v in present]
        why = f"{domain} <- {'/'.join(sorted(kinds))}"
        if have:
            print(f"    ok        {', '.join(have):<24} present       ({why})")
        else:
            gaps.extend(options)
            print(f"    MISSING   {' or '.join(options):<24} needed for    ({why})")

    if gaps:
        print(f"\n  -> re-download including {sorted(set(gaps))}, or those terms stay unmapped.")
        print("     Athena rebuilds a whole bundle each time, so re-request everything")
        print("     already present plus the missing names and replace this directory.")

    # 3. how much of this dataset would actually map
    try:
        _estimate(con, args, cfg)
    except Exception as exc:
        print(f"\n(skipped the coverage estimate: {exc})")
    con.close()
    print("\nIf this all looks right:")
    print(f"    export OMOP_VOCAB_DIR={directory}")
    print(f"    ehr2trace omop --dataset {args.dataset}")
    return 0


def _estimate_structured_drugs(cfg, con, free: list[tuple[str, str]]) -> None:
    """What the structured drug pass would settle out of the free-text terms.

    A term whose `code_system` is SOURCE has no code to look up, so the four gates above
    say nothing about it. Drug names are the exception: they are structured, and
    `ehr2trace.drug_match` resolves them by ingredient, strength and dose form. Reporting
    them as plain free text would understate what this vocabulary can do by millions of
    rows, which is the same failure -- an estimate that does not model the ETL -- that
    the stride sample above exists to avoid.
    """
    from ehr2trace.drug_match import DrugIndex, match_drug

    drugs = sorted({code for code, kind in free if DOMAIN_FOR_KIND.get(kind) == "Drug"})
    other = len(free) - len(drugs)
    if other:
        print(f"\n  {'SOURCE':<12} {other:>7,} terms   free text -- lexical recall plus review")
    if not drugs:
        return
    try:
        index = DrugIndex(con)
    except Exception:
        print(f"  {'SOURCE drugs':<12} {len(drugs):>7,} terms   DRUG_STRENGTH missing -- "
              "structured matching unavailable")
        return
    noise = list(cfg.terminology.drug_name_noise)
    settled = sum(1 for name in drugs if match_drug(index, name, noise)[1] == "unique")
    print(f"\n  {'SOURCE':<12} {len(drugs):>7,} drug names   {settled:,} "
          f"({settled / len(drugs):.1%}) resolve by ingredient + strength + dose form; "
          f"the rest go to review")


def _estimate(con, args, cfg) -> None:
    """Test-map a sample of the dataset's own pending terms against this bundle.

    Modelled on the same gates `ehr2trace.terminology` applies, in the same order, because
    an estimate that models fewer of them is not conservative -- it is wrong in whichever
    direction it happens to be wrong. Matching on the literal string alone reported 0.0%
    for a code system the ETL then resolved almost completely, which reads as "this
    vocabulary is useless here" about a bundle that was doing its job.

    The four gates, each of which a term has to clear:

      1. the code is in the vocabulary as written;
      2. or it is there once `. - /` and spaces are ignored, and exactly one vocabulary
         code reduces to the same string -- the uniqueness is checked, not assumed;
      3. the concept found is standard, or has a `Maps to` hop to one;
      4. that standard concept's domain is the one this event kind publishes into.
    """
    from ehr2trace.paths import WorkLayout
    from ehr2trace.review import read_pending

    if not os.environ.get("EHR_WORK_ROOT"):
        return
    layout = WorkLayout.from_env(cfg.dataset_id)
    pending = read_pending(layout)
    if not pending:
        return

    by_system: dict[str, list[tuple[str, str]]] = {}
    for row in pending:
        system = row.get("code_system") or "SOURCE"
        code = (row.get("source_string") or "").strip()
        if code:
            by_system.setdefault(system, []).append((code, row.get("event_kind") or ""))

    print(f"\ncoverage estimate against {len(pending):,} pending terms")
    print("  (the same four gates the ETL applies: exact code, then punctuation-insensitive")
    print("   where exactly one vocabulary code matches, then `Maps to`, then the domain)")

    coded = {s: v for s, v in by_system.items() if s.upper() != "SOURCE"}
    free = by_system.get("SOURCE", [])
    if free:
        _estimate_structured_drugs(cfg, con, free)
    if not coded:
        return

    con.execute("CREATE OR REPLACE TEMP TABLE _q (code_system VARCHAR, code VARCHAR, "
                "stripped VARCHAR, event_kind VARCHAR)")
    sampled: dict[str, list[tuple[str, str]]] = {}
    payload = []
    for system, entries in coded.items():
        seen: dict[str, str] = {}
        for code, kind in entries:
            seen.setdefault(code, kind)
        # An evenly-spaced stride, not the head. Codes sort into clinical order -- the
        # first 500 ICD-10-CM codes are one chapter, all of them conditions that map
        # cleanly -- so a head sample reports the easy end of the code space and calls
        # it the whole. Deterministic, because every other number this repo prints is.
        ordered = sorted(seen.items())
        stride = max(1, len(ordered) // args.sample)
        sample = ordered[::stride][: args.sample]
        sampled[system] = sample
        payload += [(system, code, _unpunctuated(code), kind) for code, kind in sample]
    con.executemany("INSERT INTO _q VALUES (?, ?, ?, ?)", payload)

    # gates 1 and 2, in one pass over CONCEPT
    found = {
        (r[0], r[1]): r
        for r in con.execute(
            """
            WITH cand AS (
                SELECT q.code_system, q.code, q.event_kind, c.concept_code,
                       CAST(c.concept_id AS BIGINT) AS cid,
                       c.standard_concept, c.domain_id,
                       (c.concept_code = q.code) AS is_exact
                FROM _q q
                JOIN concept c
                  ON c.vocabulary_id = q.code_system
                 AND (c.concept_code = q.code
                      OR upper(replace(replace(replace(replace(
                           c.concept_code, '.', ''), '-', ''), '/', ''), ' ', '')) = q.stripped)
                 AND (c.invalid_reason IS NULL OR c.invalid_reason = '')
            )
            SELECT code_system, code,
                   max(is_exact) AS exact_hit,
                   count(DISTINCT concept_code) AS n_codes,
                   coalesce(min(CASE WHEN is_exact THEN cid END),
                            CASE WHEN count(DISTINCT concept_code) = 1 THEN min(cid) END) AS cid,
                   coalesce(min(CASE WHEN is_exact THEN standard_concept END),
                            CASE WHEN count(DISTINCT concept_code) = 1
                                 THEN min(standard_concept) END) AS standard_concept,
                   coalesce(min(CASE WHEN is_exact THEN domain_id END),
                            CASE WHEN count(DISTINCT concept_code) = 1
                                 THEN min(domain_id) END) AS domain_id
            FROM cand GROUP BY 1, 2
            """
        ).fetchall()
    }

    # gate 3, in one pass over CONCEPT_RELATIONSHIP
    source_ids = {r[4] for r in found.values() if r[4] is not None and r[5] != "S"}
    maps_to: dict[int, str] = {}
    if source_ids:
        rel = next((p for p in args.directory.glob("*")
                    if p.stem.upper() == "CONCEPT_RELATIONSHIP"), None)
        if rel is not None:
            con.register("concept_relationship", con.read_csv(
                str(rel), sep="\t", header=True, quotechar="", all_varchar=True))
            con.execute("CREATE OR REPLACE TEMP TABLE _src (cid BIGINT)")
            con.executemany("INSERT INTO _src VALUES (?)", [(int(c),) for c in sorted(source_ids)])
            maps_to = {
                int(r[0]): r[1]
                for r in con.execute(
                    """
                    SELECT CAST(r.concept_id_1 AS BIGINT) AS src, min(m.domain_id)
                    FROM concept_relationship r
                    JOIN _src s ON s.cid = CAST(r.concept_id_1 AS BIGINT)
                    JOIN concept m ON m.concept_id = r.concept_id_2 AND m.standard_concept = 'S'
                    WHERE r.relationship_id = 'Maps to'
                    GROUP BY 1
                    """
                ).fetchall()
            }

    for system in sorted(coded, key=lambda s: -len(coded[s])):
        sample = sampled[system]
        n = len(sample)
        if not n:
            continue
        exact = punct = standard = in_domain = 0
        for code, kind in sample:
            row = found.get((system, code))
            if row is None or row[4] is None:
                continue
            if row[2]:
                exact += 1
            else:
                punct += 1
            if row[5] == "S":
                domain = row[6]
            elif int(row[4]) in maps_to:
                domain = maps_to[int(row[4])]
            else:
                continue
            standard += 1
            expected = DOMAIN_FOR_KIND.get(kind)
            if expected is None or domain == expected:
                in_domain += 1

        def pct(k: int) -> str:
            return f"{k:>6,} ({100 * k / n:>5.1f}%)"

        print(f"\n  {system:<12} {len(coded[system]):>7,} terms   sample {n:,}")
        print(f"      in the vocabulary as written        {pct(exact)}")
        print(f"      + once punctuation is ignored       {pct(punct)}")
        print(f"      reaches a standard concept          {pct(standard)}")
        print(f"      lands in the domain it needs        {pct(in_domain)}   <- would map")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
