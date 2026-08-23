"""Render the fault-injection results as the LaTeX table the paper includes.

Generated rather than typed, so the table cannot drift from the run that produced it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SHORT = {
    "ANCHOR_NEVER_AN_EVENT_TIME": "anchor-lint",
    "ANCHOR_TIMES_ARE_NOT_THE_EVENT_CLOCK": "anchor-clock",
    "ANCHORS_ARE_NOT_EVENTS": "anchors-sep",
    "CANONICAL_SCHEMA_AS_DECLARED": "canon-schema",
    "COHORT_LABEL_NEVER_A_CLINICAL_FACT": "label-not-fact",
    "EVENT_LINEAGE_COMPLETE": "lineage-complete",
    "EVENT_SUBJECTS_WERE_ISSUED_BY_IDENTITY": "subj-issued",
    "FAN_OUT_AND_DEDUP": "fanout",
    "IDENTITY_RESOLVED_ACROSS_PARTITIONS": "identity",
    "LINK_TARGETS_EXIST": "link-targets",
    "MEDS_AVAILABILITY_PREVENTS_LEAKAGE": "meds-avail",
    "MEDS_CODES_METADATA_COMPLETE": "meds-codes",
    "MEDS_LINEAGE_COMPLETE": "meds-lineage",
    "MEDS_NO_LABEL_LEAKAGE": "meds-no-label",
    "MEDS_SCHEMA_VALID": "meds-schema",
    "MEDS_SHARDS_CONTIGUOUS_AND_SORTED": "meds-sorted",
    "MEDS_SPLITS_DISJOINT_AND_COMPLETE": "meds-splits",
    "OMOP_BIRTH_POLICY_ENFORCED": "birth-policy",
    "OMOP_BIRTH_YEAR_IS_REPRODUCIBLE": "birth-repro",
    "OMOP_CONCEPTS_EXIST_AND_FIT_THEIR_DOMAIN": "domain-fit",
    "OMOP_EVERY_ROW_HAS_LINEAGE": "omop-lineage",
    "OMOP_NO_FABRICATED_MEASUREMENT_DATES": "no-fab-date",
    "OMOP_PRIMARY_KEYS_UNIQUE": "pk-unique",
    "OMOP_REFERENTIAL_INTEGRITY": "omop-refint",
    "POST_DEATH_RECORDS_FLAGGED_NOT_DELETED": "post-death",
    "QUARANTINE_IS_EXPLAINED": "quar-reason",
    "SOURCE_ROWS_ACCOUNTED": "rows-accounted",
    "UNDECIDED_PROPOSALS_NEVER_PUBLISHED": "undecided",
}


def label(check_id: str) -> str:
    return SHORT.get(check_id, check_id.lower().replace("_", "-")[:16])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", type=Path, required=True)
    ap.add_argument("--after", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    before = {r["fault_id"]: r for r in json.loads(args.before.read_text())["results"]}
    after_doc = json.loads(args.after.read_text())
    after = {r["fault_id"]: r for r in after_doc["results"]}

    lines = [
        r"\begin{tabular}{llcl}",
        r"\toprule",
        r"\textbf{Injected fault} & \textbf{Layer} & \textbf{Before} & \textbf{Detected by (after)} \\",
        r"\midrule",
    ]
    layer_now = None
    for fid, rec in after.items():
        if rec["skipped"]:
            continue
        if rec["layer"] != layer_now:
            layer_now = rec["layer"]
        was = before.get(fid, {}).get("detected", False)
        mark = r"\checkmark" if was else r"\textbf{--}"
        detectors = ", ".join(f"\\texttt{{{label(d)}}}" for d in rec["detectors"][:2])
        name = fid.lower().replace("_", r"\_")
        lines.append(f"\\texttt{{\\footnotesize {name}}} & {rec['layer']} & {mark} & {detectors} \\\\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
    ]
    args.out.write_text("\n".join(lines) + "\n")
    n_before = sum(1 for r in before.values() if r["detected"] and not r["skipped"])
    n_after = sum(1 for r in after.values() if r["detected"] and not r["skipped"])
    total = sum(1 for r in after.values() if not r["skipped"])
    print(f"{n_before}/{total} before, {n_after}/{total} after -> {args.out}")


if __name__ == "__main__":
    main()
