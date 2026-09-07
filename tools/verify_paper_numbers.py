"""Re-derive every measured quantity in the paper from the result files.

The paper defines each measured number once, as a LaTeX macro, so the prose cannot
disagree with itself. That protects against one kind of drift and not the other: a macro
can still disagree with the run that produced it, silently, because nothing connects the
two. This connects them.

Each entry below says how to recompute one macro from `results/`. A mismatch is an error
with both values printed, and the fix is to update the macro -- never the result file.

Usage::

    python tools/verify_paper_numbers.py
    python tools/verify_paper_numbers.py --update   # rewrite the macros to match
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    return json.loads((ROOT / "results" / name).read_text())


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", "{,}")


def _leakage() -> dict[str, str]:
    doc = _load("leakage_downstream.json")
    first = doc["horizons"][0]
    by = {a["arm"]: a for a in first["arms"]}
    clean = by["respects_availability"]
    dated = by["diagnoses_at_admission"]
    worst_availability = max(
        h["auroc_inflation"]["ignores_availability"] for h in doc["horizons"]
    )
    return {
        "leakSubjects": _thousands(doc["cohort"]["n_subjects"]),
        "leakDeaths": _thousands(doc["cohort"]["n_positive"]),
        "leakPrevalence": f"{doc['cohort']['prevalence_pct']:.1f}\\%",
        "leakCleanAuroc": f"{clean['held_out_auroc']:.3f}",
        "leakDatedAuroc": f"{dated['held_out_auroc']:.3f}",
        "leakInflation": f"{100 * (dated['held_out_auroc'] - clean['held_out_auroc']):.1f}",
        "leakCleanAuprc": f"{clean['held_out_auprc']:.3f}",
        "leakDatedAuprc": f"{dated['held_out_auprc']:.3f}",
        "availInflation": f"{worst_availability:.3f}",
    }


def _dqd() -> dict[str, str]:
    doc = _load("dqd_baseline.json")
    return {
        "dqdChecks": _thousands(doc["checks_total"]),
        "dqdDetected": str(doc["faults_detected"]),
        "dqdBlind": str(len(doc["faults_never_reaching_the_cdm"])),
        "dqdMissedInCdm": str(len(doc["faults_missed_though_present_in_the_cdm"])),
        "dqdVersion": doc["versions"]["DataQualityDashboard"],
        "dqdNotApplicable": _thousands(doc["baseline_not_applicable"]),
        "dqdBaselineFailing": str(len(doc["baseline_failing"])),
        "dqdAfterDeletion": str(
            next(r["checks_failing"] for r in doc["results"]
                 if r["fault_id"] == "POST_DEATH_RECORDS_DELETED")
        ),
    }


NUMBERS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _reproducibility() -> dict[str, str]:
    doc = _load("reproducibility.json")
    count = doc["configurations"]
    return {
        "reproConfigs": NUMBERS[count] if count < len(NUMBERS) else str(count),
        "reproArtifacts": str(doc["artifacts_compared"]),
        "checkCount": str(doc["checks_run"]),
        "falseAlarms": str(len(doc["false_positive_checks"])),
    }


def _terminology() -> dict[str, str]:
    """The two composition deltas, which are the section's whole point and disagree."""
    condition_dense = _load("retrieval_bge-m3_condition.json")["top1_pct"]
    condition_composed = _load("compose_dense_medgemma_k8.json")["model_top1_pct_overall"]
    loinc_dense = _load("retrieval_bge-m3_measurement.json")["top1_pct"]
    loinc_composed = _load("loinc_compose_dense_medgemma_k8.json")["model_top1_pct_overall"]
    loinc_lexical = _load("loinc_lexical_k8.json")["lexical_top1_pct_overall"]
    loinc_reranked = _load("loinc_medgemma_k8.json")["model_top1_pct_overall"]
    qwen_lexical = _load("loinc_qwen3-32b_k8.json")["model_top1_pct_overall"]
    qwen_composed = _load("loinc_compose_dense_qwen3-32b_k8.json")["model_top1_pct_overall"]
    terms = _load("loinc_synonym_terms.json")
    return {
        "conditionComposeDelta": f"{condition_composed - condition_dense:.1f}",
        "loincComposeDelta": f"{loinc_composed - loinc_dense:.1f}",
        "loincRerankDelta": f"{loinc_reranked - loinc_lexical:.1f}",
        "loincDenseRecall": f"{_load('retrieval_bge-m3_measurement.json')['recall_at_k_pct']['8']:.1f}",
        "conditionDenseRecall": f"{_load('retrieval_bge-m3_condition.json')['recall_at_k_pct']['8']:.1f}",
        "loincQwenRerankDelta": f"{qwen_lexical - loinc_lexical:.1f}",
        "loincQwenComposeDelta": f"{qwen_composed - loinc_dense:.1f}",
        "loincEligible": _thousands(terms["eligible_synonyms"]),
        "loincPool": _thousands(_load("retrieval_bge-m3_measurement.json")["pool_size"]),
    }


def _scale() -> dict[str, str]:
    """Only the quantities the cost run establishes; the rest of it is timings."""
    doc = _load("cost.json")
    term_map = doc["digest_before"]["omop"].get("term_map", "0:")
    return {"termMapRows": _thousands(int(term_map.split(":")[0]))}


def _faults() -> dict[str, str]:
    doc = _load("faults_fixture.json")
    return {"localised": str(doc["faults_localised"])}


def _snapshot() -> dict[str, str]:
    from make_paper_tables import snapshot_macros
    return snapshot_macros(_load("paper_snapshot.json"))


def _readiness() -> dict[str, str]:
    from make_readiness_numbers import readiness_macros
    return readiness_macros(_load("world_model_readiness.json"))


def _cost() -> dict[str, str]:
    """Stage cost, from whichever run of each stage the cost record kept."""
    doc = _load("cost.json")
    rows = doc["recorded"] + doc.get("measured", [])
    latest = {}
    for row in rows:
        if row.get("wall_seconds") is not None:
            latest[row["stage"]] = row
    out = {}
    for stage, macro in (("ingest", "mimicIngestMinutes"), ("canonical", "mimicCanonicalMinutes"),
                         ("omop", "mimicOmopMinutes"), ("meds", "mimicMedsMinutes")):
        if stage in latest:
            out[macro] = f"{latest[stage]['wall_seconds'] / 60:.0f}"
    if "canonical" in latest and latest["canonical"].get("peak_rss_gb") is not None:
        out["mimicCanonicalPeakGb"] = f"{latest['canonical']['peak_rss_gb']:.1f}"
    return out


def _rebuild_defects() -> dict[str, str]:
    """The two defects the rebuild surfaced, which no injected fault predicted."""
    record = _load("rebuild_defects.json")
    doc = record["routing_precedence"]
    ties = record["surrogate_key_ties"]
    return {
        "leakedAdministrations": _thousands(doc["not_administered_in_drug_exposure"]),
        "leakedFlushes": _thousands(doc["of_which_flushes"]),
        "faninDrug": _thousands(ties["fanout_drug_events"]),
        "faninCondition": _thousands(ties["fanout_condition_events"]),
    }


SOURCES: tuple[Callable[[], dict[str, str]], ...] = (
    _leakage, _dqd, _reproducibility, _terminology, _scale, _faults, _snapshot, _readiness,
    _rebuild_defects, _cost,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paper", type=Path, default=ROOT / "paper" / "main.tex")
    ap.add_argument("--update", action="store_true", help="rewrite the macros to match the results")
    args = ap.parse_args()

    expected: dict[str, str] = {}
    for source in SOURCES:
        expected.update(source())

    # Quantity definitions are split by provenance in the revised paper.
    paths = [args.paper]
    for filename in ("experiment_numbers.tex", "snapshot_numbers.tex", "readiness_numbers.tex"):
        path = args.paper.parent / filename
        if path.exists():
            paths.append(path)
    contents = {p: p.read_text() for p in paths}
    text = "\n".join(contents.values())
    drift, missing = [], []
    for name, value in sorted(expected.items()):
        match = re.search(r"\\newcommand\{\\" + name + r"\}\{(.*?)\}\n", text)
        if not match:
            missing.append(name)
        elif match.group(1) != value:
            drift.append((name, match.group(1), value))

    if args.update and drift:
        for path, original in contents.items():
            updated = original
            for name, _old, value in drift:
                updated = re.sub(
                    r"(\\newcommand\{\\" + name + r"\}\{).*?(\}\n)",
                    lambda m, value=value: m.group(1) + value + m.group(2),
                    updated,
                )
            if updated != original:
                path.write_text(updated)
        print(f"updated {len(drift)} macro(s)")
        if missing:
            raise SystemExit(f"{len(missing)} absent macros remain")
        return

    for name, found, want in drift:
        print(f"DRIFT  \\{name}: paper says {found!r}, results say {want!r}")
    for name in missing:
        print(f"ABSENT \\{name}: no macro defined")
    if drift or missing:
        raise SystemExit(f"{len(drift)} drifted, {len(missing)} absent")
    print(f"{len(expected)} measured quantities agree with results/")


if __name__ == "__main__":
    main()
