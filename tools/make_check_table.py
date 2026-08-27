"""Render the check suite by group as the LaTeX table the paper includes.

The grouping lives here rather than in the checks themselves, but it is verified against
the registry: every registered check must appear in exactly one group and no group may
name a check that does not exist. Adding a check without classifying it fails this tool
rather than silently producing a table whose total is wrong, which is what happened to
the hand-written version it replaces.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.validate import CHECKS  # noqa: E402

GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Reconciliation and lineage",
        "every source row is an event, a quarantine row, or explicitly factless",
        (
            "INPUT_MANIFEST_COMPLETE", "RECONCILIATION_ROWS", "SOURCE_COVERAGE_REPORTED",
            "EVENT_LINEAGE_COMPLETE", "LINK_TARGETS_EXIST", "FAN_OUT_AND_DEDUP",
            "SOURCE_ROWS_ACCOUNTED",
        ),
    ),
    (
        "Semantic correctness",
        r"anchors are not clocks; labels are not facts; orders $\neq$ administrations",
        (
            "ANCHOR_NEVER_AN_EVENT_TIME", "CANONICAL_SCHEMA_AS_DECLARED",
            "EVENT_SUBJECTS_WERE_ISSUED_BY_IDENTITY", "ANCHOR_TIMES_ARE_NOT_THE_EVENT_CLOCK",
            "ANCHORS_ARE_NOT_EVENTS", "COHORT_LABEL_NEVER_A_CLINICAL_FACT",
            "MEMBERSHIP_KEPT_PER_PARTITION", "NO_UNTIMED_CLINICAL_EVENTS",
            "ORDERS_AND_ADMINISTRATIONS_STAY_SEPARATE", "POST_DEATH_RECORDS_FLAGGED_NOT_DELETED",
            "QUARANTINE_IS_EXPLAINED", "IDENTITY_RESOLVED_ACROSS_PARTITIONS",
        ),
    ),
    (
        "OMOP compliance",
        "referential integrity, key uniqueness, domain fit, no invented dates",
        (
            "OMOP_EVERY_ROW_HAS_LINEAGE", "OMOP_CONCEPTS_EXIST_AND_FIT_THEIR_DOMAIN",
            "TERMINOLOGY_COVERAGE_PLAUSIBLE", "OMOP_REFERENTIAL_INTEGRITY",
            "OMOP_PRIMARY_KEYS_UNIQUE", "OMOP_BIRTH_YEAR_IS_REPRODUCIBLE",
            "OMOP_BIRTH_POLICY_ENFORCED", "OMOP_NO_FABRICATED_MEASUREMENT_DATES",
        ),
    ),
    (
        "MEDS compliance",
        "schema, shard contiguity and sorting, split disjointness, code metadata",
        (
            "MEDS_SCHEMA_VALID", "MEDS_SHARDS_CONTIGUOUS_AND_SORTED", "MEDS_LINEAGE_COMPLETE",
            "MEDS_NO_LABEL_LEAKAGE", "MEDS_CODES_METADATA_COMPLETE",
            "MEDS_SPLITS_DISJOINT_AND_COMPLETE", "MEDS_AVAILABILITY_PREVENTS_LEAKAGE",
        ),
    ),
    (
        "Review governance",
        "nothing a human has not decided reaches a published mapping",
        ("UNDECIDED_PROPOSALS_NEVER_PUBLISHED",),
    ),
)

#: Checks that exist only because the output is training data. Cross-cutting: they sit in
#: several of the groups above, which is why they are counted separately.
TRAINING_DATA_SPECIFIC = frozenset({
    "ANCHOR_NEVER_AN_EVENT_TIME", "ANCHOR_TIMES_ARE_NOT_THE_EVENT_CLOCK", "ANCHORS_ARE_NOT_EVENTS",
    "COHORT_LABEL_NEVER_A_CLINICAL_FACT", "MEMBERSHIP_KEPT_PER_PARTITION",
    "NO_UNTIMED_CLINICAL_EVENTS", "POST_DEATH_RECORDS_FLAGGED_NOT_DELETED",
    "EVENT_SUBJECTS_WERE_ISSUED_BY_IDENTITY", "MEDS_SHARDS_CONTIGUOUS_AND_SORTED",
    "MEDS_NO_LABEL_LEAKAGE", "MEDS_SPLITS_DISJOINT_AND_COMPLETE",
    "MEDS_AVAILABILITY_PREVENTS_LEAKAGE",
})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    registered = {cid for cid, _ in CHECKS}
    grouped: list[str] = [cid for _, _, ids in GROUPS for cid in ids]
    if len(grouped) != len(set(grouped)):
        raise SystemExit("a check is classified into more than one group")
    missing = registered - set(grouped)
    invented = set(grouped) - registered
    if missing or invented:
        raise SystemExit(f"grouping is stale: unclassified {sorted(missing)}, unknown {sorted(invented)}")
    stray = TRAINING_DATA_SPECIFIC - registered
    if stray:
        raise SystemExit(f"training-data list names checks that do not exist: {sorted(stray)}")

    lines = [
        r"\begin{tabular}{llc}",
        r"\toprule",
        r"\textbf{Group} & \textbf{What it asserts} & \textbf{$n$} \\",
        r"\midrule",
    ]
    for name, blurb, ids in GROUPS:
        lines.append(f"{name} & {blurb} & {len(ids)} \\\\")
    lines += [
        r"\midrule",
        f"& & \\textbf{{{len(registered)}}} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"{len(registered)} checks, {len(TRAINING_DATA_SPECIFIC)} training-data specific -> {args.out}")


if __name__ == "__main__":
    main()
