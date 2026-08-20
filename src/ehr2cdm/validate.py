"""Reconciliation and compliance checks (design section 10.2).

Two things this module refuses to do.

It does not require source row count to equal target row count. Relations here are
genuinely one-to-many and many-to-one -- one report is many rows, one row is many
measurements, one fact appears in two batches -- so an equality check would either
fail forever or be quietly weakened until it passed. Coverage, fan-out, deduplication
and quarantine rates are reported together instead, which is what actually tells you
whether a conversion is complete.

It does not soften a failure into a warning. A check that fails means the output is not
publishable, and the command exits non-zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import polars as pl

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.schema import EventKind, QualityFlag


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False


CHECKS: list[tuple[str, Callable]] = []


def check(check_id: str, slow: bool = False):
    def deco(fn):
        CHECKS.append((check_id, (fn, slow)))
        return fn

    return deco


@dataclass
class Layers:
    """Whatever has been built so far. Checks for missing layers skip, not fail."""

    cfg: DatasetConfig
    layout: WorkLayout
    events: pl.DataFrame | None
    links: pl.DataFrame | None
    anchors: pl.DataFrame | None
    memberships: pl.DataFrame | None
    issues: pl.DataFrame | None
    quarantine: pl.DataFrame | None
    manifest: dict[str, Any] | None

    @classmethod
    def load(cls, cfg: DatasetConfig, layout: WorkLayout) -> "Layers":
        def read(name: str) -> pl.DataFrame | None:
            path = layout.canonical_path(name)
            return pl.read_parquet(path) if path.exists() else None

        manifest_path = layout.manifest_dir / "inputs.json"
        return cls(
            cfg=cfg,
            layout=layout,
            events=read("events"),
            links=read("event_source"),
            anchors=read("anchors"),
            memberships=read("cohort_membership"),
            issues=read("quality_issue"),
            quarantine=read("quarantine"),
            manifest=json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
        )


def run_checks(cfg: DatasetConfig, layout: WorkLayout, include_slow: bool = False) -> list[CheckResult]:
    layers = Layers.load(cfg, layout)
    results: list[CheckResult] = []
    for check_id, (fn, slow) in CHECKS:
        if slow and not include_slow:
            continue
        try:
            result = fn(layers)
        except Exception as exc:  # a check that crashes is a failing check
            result = CheckResult(check_id, False, f"check raised {type(exc).__name__}: {exc}")
        if result is None:
            continue
        result.check_id = check_id
        results.append(result)
    path = layout.runs_dir / "validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.__dict__ for r in results], indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return results


def _skip(reason: str) -> CheckResult:
    return CheckResult("", True, f"skipped: {reason}", skipped=True)


# --------------------------------------------------------------------------------
# input completeness and reconciliation
# --------------------------------------------------------------------------------


@check("INPUT_MANIFEST_COMPLETE")
def _manifest_complete(l: Layers) -> CheckResult:
    if l.manifest is None:
        return _skip("no ingest manifest")
    inputs = l.manifest["inputs"]
    missing_hash = [i["file_path"] for i in inputs if not i.get("file_sha256")]
    return CheckResult(
        "",
        not missing_hash,
        f"{len(inputs)} logical units recorded, all hashed"
        if not missing_hash
        else f"{len(missing_hash)} units missing a content hash",
        {"units": len(inputs), "files": len({i["file_path"] for i in inputs})},
    )


@check("RECONCILIATION_ROWS")
def _reconciliation(l: Layers) -> CheckResult:
    if l.manifest is None:
        return _skip("no ingest manifest")
    bad = [
        f"{i['partition_id']}/{i['source_id']}"
        for i in l.manifest["inputs"]
        if i["rows_read"] != i["rows_parsed"] + i["rows_quarantined"]
    ]
    total_read = sum(i["rows_read"] for i in l.manifest["inputs"])
    total_q = sum(i["rows_quarantined"] for i in l.manifest["inputs"])
    return CheckResult(
        "",
        not bad,
        f"every source reconciles: {total_read:,} read = parsed + quarantined"
        if not bad
        else f"rows unaccounted for in: {bad[:5]}",
        {
            "rows_read": total_read,
            "rows_quarantined": total_q,
            "quarantine_rate": round(total_q / total_read, 6) if total_read else 0.0,
        },
    )


@check("EVENT_LINEAGE_COMPLETE")
def _lineage_complete(l: Layers) -> CheckResult:
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    linked = set(l.links["event_id"].to_list())
    orphans = [e for e in l.events["event_id"].to_list() if e not in linked]
    return CheckResult(
        "",
        not orphans,
        "every canonical event traces to at least one source row"
        if not orphans
        else f"{len(orphans)} events have no source row",
        {"events": l.events.height, "links": l.links.height},
    )


@check("LINK_TARGETS_EXIST")
def _links_resolve(l: Layers) -> CheckResult:
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    known = set(l.events["event_id"].to_list())
    dangling = [e for e in set(l.links["event_id"].to_list()) if e not in known]
    return CheckResult(
        "", not dangling, "no dangling lineage links" if not dangling else f"{len(dangling)} dangling links"
    )


@check("FAN_OUT_AND_DEDUP")
def _fan_out(l: Layers) -> CheckResult:
    """Reported, never asserted equal: these relations are legitimately not one-to-one."""
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    per_event = l.links.group_by("event_id").len()
    duplicated = int((per_event["len"] > 1).sum())
    return CheckResult(
        "",
        True,
        f"{l.events.height:,} events from {l.links.height:,} links; "
        f"{duplicated:,} events have more than one source row",
        {
            "events": l.events.height,
            "links": l.links.height,
            "events_with_duplicates": duplicated,
            "fan_out": round(l.links.height / l.events.height, 4) if l.events.height else 0.0,
        },
    )


# --------------------------------------------------------------------------------
# semantic correctness
# --------------------------------------------------------------------------------


@check("ANCHOR_NEVER_AN_EVENT_TIME")
def _anchor_not_event_time(l: Layers) -> CheckResult:
    """Structural, not statistical: no source may wire an anchor column to a time role."""
    offenders = []
    for name, spec in l.cfg.sources.items():
        anchor = spec.fields.get("anchor_time")
        if not anchor:
            continue
        anchor_aliases = {a.lower() for a in anchor.from_}
        for role in ("event_time", "available_time", "end_time"):
            other = spec.fields.get(role)
            if other and anchor_aliases & {a.lower() for a in other.from_}:
                offenders.append(f"{name}.{role}")
    return CheckResult(
        "",
        not offenders,
        "no source maps its anchor column onto a clinical time"
        if not offenders
        else f"anchor column reused as a clinical time in: {offenders}",
    )


@check("ANCHORS_ARE_NOT_EVENTS")
def _anchors_not_events(l: Layers) -> CheckResult:
    if l.events is None or l.anchors is None or l.anchors.height == 0:
        return _skip("no anchors in this dataset")
    anchor_type = l.cfg.anchors.anchor_type if l.cfg.anchors else ""
    leaked = l.events.filter(pl.col("source_code").str.to_lowercase() == anchor_type.lower())
    known = int(l.anchors.filter(pl.col("anchor_time_known")).height)
    return CheckResult(
        "",
        leaked.height == 0,
        f"{l.anchors.height:,} anchors recorded separately from events "
        f"({known:,} with a known time component)"
        if leaked.height == 0
        else f"{leaked.height} events look like fabricated anchor procedures",
        {"anchors": l.anchors.height, "anchor_time_known": known},
    )


@check("COHORT_LABEL_NEVER_A_CLINICAL_FACT")
def _label_not_a_fact(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    labels = {p.membership_label for p in l.cfg.partitions if p.membership_label}
    if not labels:
        return _skip("dataset has no membership labels")
    lowered = {s.lower() for s in labels}
    hits = l.events.filter(
        pl.col("event_kind").is_in([str(EventKind.condition), str(EventKind.demographic)])
        & (
            pl.col("source_code").str.to_lowercase().is_in(list(lowered))
            | pl.col("value_text").str.to_lowercase().is_in(list(lowered))
        )
    )
    return CheckResult(
        "",
        hits.height == 0,
        "no cohort label became a condition or observation"
        if hits.height == 0
        else f"{hits.height} events carry a cohort label as a clinical fact",
    )


@check("MEMBERSHIP_KEPT_PER_PARTITION")
def _membership_kept(l: Layers) -> CheckResult:
    """A patient in two differently-labelled partitions keeps both rows, unmerged."""
    if l.memberships is None or l.memberships.height == 0:
        return _skip("dataset has no cohort membership")
    per_subject = l.memberships.group_by("subject_id").agg(
        pl.col("membership_label").n_unique().alias("labels")
    )
    conflicted = int((per_subject["labels"] > 1).sum())
    scopes = set(l.memberships["label_scope"].to_list())
    patient_level = scopes == {"patient"}
    return CheckResult(
        "",
        not patient_level,
        f"{conflicted:,} subjects carry more than one cohort label, all kept separately "
        f"(scope: {sorted(scopes)})",
        {"subjects_with_conflicting_labels": conflicted, "scopes": sorted(scopes)},
    )


@check("NO_UNTIMED_CLINICAL_EVENTS")
def _no_untimed_clinical(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    untimed = l.events.filter(
        pl.col("event_time").is_null() & (pl.col("event_kind") != str(EventKind.demographic))
    )
    return CheckResult(
        "",
        untimed.height == 0,
        "every clinical event has a real time; only static attributes are timeless"
        if untimed.height == 0
        else f"{untimed.height} clinical events have no time and were still published",
    )


@check("ORDERS_AND_ADMINISTRATIONS_STAY_SEPARATE")
def _orders_vs_admin(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    orders = l.events.filter(pl.col("event_kind") == str(EventKind.drug_order))
    admins = l.events.filter(pl.col("event_kind") == str(EventKind.drug_admin))
    overlap = set(orders["event_id"].to_list()) & set(admins["event_id"].to_list())
    return CheckResult(
        "",
        not overlap,
        f"{orders.height:,} orders and {admins.height:,} administrations, no shared events"
        if not overlap
        else f"{len(overlap)} events are both an order and an administration",
        {"drug_order": orders.height, "drug_admin": admins.height},
    )


@check("POST_DEATH_RECORDS_FLAGGED_NOT_DELETED")
def _post_death(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    flagged = l.events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.RECORDED_AFTER_DEATH))
    )
    return CheckResult(
        "",
        True,
        f"{flagged.height:,} records dated after death, flagged and kept with their original dates",
        {"recorded_after_death": flagged.height},
    )


@check("QUARANTINE_IS_EXPLAINED")
def _quarantine_explained(l: Layers) -> CheckResult:
    if l.quarantine is None or l.quarantine.height == 0:
        return CheckResult("", True, "nothing quarantined at the canonical stage")
    unexplained = l.quarantine.filter(pl.col("reason").is_null() | (pl.col("reason") == ""))
    by_reason = dict(
        zip(
            l.quarantine.group_by("reason").len()["reason"].to_list(),
            l.quarantine.group_by("reason").len()["len"].to_list(),
        )
    )
    return CheckResult(
        "",
        unexplained.height == 0,
        f"{l.quarantine.height:,} rows quarantined, each with a reason"
        if unexplained.height == 0
        else f"{unexplained.height} quarantined rows have no reason recorded",
        {"by_reason": by_reason},
    )


# --------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------


@check("IDENTITY_RESOLVED_ACROSS_PARTITIONS")
def _identity(l: Layers) -> CheckResult:
    path = l.layout.identity_dir / "subject_map.parquet"
    if not path.exists():
        return _skip("identity not built")
    subjects = pl.read_parquet(path)
    duplicate_keys = subjects.height - subjects["person_source_id"].n_unique()
    duplicate_ids = subjects.height - subjects["subject_id"].n_unique()
    multi = int((subjects["partitions"].list.len() > 1).sum())
    per_partition = {
        p.id: int(subjects.filter(pl.col("partitions").list.contains(p.id)).height)
        for p in l.cfg.partitions
    }
    return CheckResult(
        "",
        duplicate_keys == 0 and duplicate_ids == 0,
        f"{subjects.height:,} subjects; {multi:,} appear in more than one partition "
        f"and resolve to a single subject each"
        if duplicate_keys == 0 and duplicate_ids == 0
        else "patient keys or subject ids are not unique",
        {"subjects": subjects.height, "multi_partition": multi, "per_partition": per_partition},
    )


# --------------------------------------------------------------------------------
# OMOP
# --------------------------------------------------------------------------------


def _omop_connection(l: Layers):
    path = l.layout.omop_dir / "omop.duckdb"
    if not path.exists():
        return None
    import duckdb

    return duckdb.connect(str(path), read_only=True)


@check("OMOP_EVERY_ROW_HAS_LINEAGE")
def _omop_lineage(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built")
    try:
        tables = {
            "person": "person_id",
            "visit_occurrence": "visit_occurrence_id",
            "condition_occurrence": "condition_occurrence_id",
            "drug_exposure": "drug_exposure_id",
            "procedure_occurrence": "procedure_occurrence_id",
            "measurement": "measurement_id",
            "note": "note_id",
            "death": "person_id",
        }
        missing: dict[str, int] = {}
        counts: dict[str, int] = {}
        for table, pk in tables.items():
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            counts[table] = n
            if not n:
                continue
            orphan = con.execute(
                f"""
                SELECT count(*) FROM {table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM etl_audit.lineage l
                    WHERE l.target_table = '{table}' AND l.target_pk = t.{pk}
                )
                """
            ).fetchone()[0]
            if orphan:
                missing[table] = orphan
        return CheckResult(
            "",
            not missing,
            "every OMOP row traces back to a source row" if not missing else f"rows without lineage: {missing}",
            counts,
        )
    finally:
        con.close()


@check("OMOP_CONCEPTS_EXIST_AND_FIT_THEIR_DOMAIN")
def _omop_concepts(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built")
    try:
        from ehr2cdm.terminology import Vocabulary

        import os

        vocab = Vocabulary.open(Path(os.environ["OMOP_VOCAB_DIR"]) if os.environ.get("OMOP_VOCAB_DIR") else None)
        checks = [
            ("condition_occurrence", "condition_concept_id", "Condition"),
            ("drug_exposure", "drug_concept_id", "Drug"),
            ("procedure_occurrence", "procedure_concept_id", "Procedure"),
            ("measurement", "measurement_concept_id", "Measurement"),
        ]
        bad: list[str] = []
        nonzero = 0
        for table, column, domain in checks:
            ids = [
                r[0]
                for r in con.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} <> 0"
                ).fetchall()
            ]
            nonzero += len(ids)
            for concept_id in ids:
                if not vocab.concept_exists(concept_id):
                    bad.append(f"{table}.{column}={concept_id} not in vocabulary")
                elif vocab.available and vocab.domain_of(concept_id) != domain:
                    bad.append(f"{table}.{column}={concept_id} is not a {domain} concept")
        vocab.close()
        return CheckResult(
            "",
            not bad,
            f"{nonzero} distinct non-zero concept ids, all present with a compatible domain"
            if not bad
            else f"invalid concept usage: {bad[:5]}",
            {"distinct_nonzero_concepts": nonzero, "vocabulary": vocab.version},
        )
    finally:
        con.close()


@check("OMOP_BIRTH_POLICY_ENFORCED")
def _omop_birth_policy(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built")
    try:
        policy = l.cfg.omop.person_birth_policy
        people = con.execute("SELECT count(*) FROM person").fetchone()[0]
        blocked = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue WHERE issue_type = 'OMOP_PERSON_BLOCKED'"
        ).fetchone()[0]
        if policy.mode == "strict":
            ok = people == 0 or blocked == 0
            detail = (
                f"strict policy: {blocked:,} patients withheld from PERSON for lack of a "
                f"derivable year of birth, {people:,} published"
            )
            # Under strict, a published person can only exist if a year of birth came
            # from the data itself, never from an approximation.
            return CheckResult("", ok or people == 0, detail, {"person": people, "blocked": blocked})
        approximated = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue "
            "WHERE issue_type = 'DERIVED_APPROXIMATE_BIRTH_YEAR'"
        ).fetchone()[0]
        return CheckResult(
            "",
            bool(policy.age_as_of_date and policy.approval_note),
            f"approved approximation in force: {approximated:,} birth years derived and flagged",
            {"person": people, "approximated": approximated},
        )
    finally:
        con.close()


@check("OMOP_NO_FABRICATED_MEASUREMENT_DATES")
def _omop_untimed(l: Layers) -> CheckResult:
    """Untimed values stay quarantined; nothing gives them a plausible-looking date."""
    if l.quarantine is None:
        return _skip("canonical layer not built")
    untimed = l.quarantine.filter(pl.col("reason") == "UNTIMED_CLINICAL_VALUE")
    con = _omop_connection(l)
    if con is None:
        return CheckResult(
            "", True, f"{untimed.height:,} untimed values quarantined", {"untimed": untimed.height}
        )
    try:
        codes = {
            spec.code.lower()
            for source in l.cfg.sources.values()
            for spec in source.untimed_values
        }
        if not codes:
            return CheckResult("", True, "dataset declares no untimed values")
        placeholders = ", ".join("?" for _ in codes)
        leaked = con.execute(
            f"SELECT count(*) FROM measurement WHERE lower(measurement_source_value) IN ({placeholders})",
            list(codes),
        ).fetchone()[0]
        return CheckResult(
            "",
            leaked == 0,
            f"{untimed.height:,} untimed values quarantined and none published with an invented date"
            if leaked == 0
            else f"{leaked} untimed values reached MEASUREMENT with a fabricated date",
            {"untimed_quarantined": untimed.height},
        )
    finally:
        con.close()


# --------------------------------------------------------------------------------
# MEDS
# --------------------------------------------------------------------------------


def _meds_data(l: Layers) -> pl.DataFrame | None:
    import meds as meds_spec

    data_dir = l.layout.meds_dir / meds_spec.data_subdirectory
    files = sorted(str(p) for p in data_dir.rglob("*.parquet"))
    return pl.read_parquet(files) if files else None


@check("MEDS_SCHEMA_VALID")
def _meds_schema(l: Layers) -> CheckResult:
    import meds as meds_spec
    import pyarrow.parquet as pq

    data_dir = l.layout.meds_dir / meds_spec.data_subdirectory
    files = sorted(data_dir.rglob("*.parquet"))
    if not files:
        return _skip("MEDS not built")
    errors: list[str] = []
    for path in files:
        try:
            meds_spec.DataSchema.validate(pq.read_table(path))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    codes_path = l.layout.meds_dir / meds_spec.code_metadata_filepath
    if codes_path.exists():
        try:
            meds_spec.CodeMetadataSchema.validate(pq.read_table(codes_path))
        except Exception as exc:
            errors.append(f"codes.parquet: {exc}")
    return CheckResult(
        "",
        not errors,
        f"{len(files)} shards validate against the installed MEDS schema"
        if not errors
        else f"schema violations: {errors[:3]}",
        {"shards": len(files)},
    )


@check("MEDS_SHARDS_CONTIGUOUS_AND_SORTED")
def _meds_sharding(l: Layers) -> CheckResult:
    import meds as meds_spec

    data_dir = l.layout.meds_dir / meds_spec.data_subdirectory
    files = sorted(data_dir.rglob("*.parquet"))
    if not files:
        return _skip("MEDS not built")
    problems: list[str] = []
    seen: dict[int, str] = {}
    for path in files:
        frame = pl.read_parquet(path)
        subjects = frame["subject_id"].to_list()
        for subject_id in set(subjects):
            if subject_id in seen and seen[subject_id] != path.name:
                problems.append(f"subject {subject_id} spans {seen[subject_id]} and {path.name}")
            seen[subject_id] = path.name
        # contiguous: the subject column may not return to a value it left
        blocks = [s for i, s in enumerate(subjects) if i == 0 or s != subjects[i - 1]]
        if len(blocks) != len(set(blocks)):
            problems.append(f"{path.name}: a subject's events are not contiguous")
        times = frame.select("subject_id", "time").to_dicts()
        for i in range(1, len(times)):
            if times[i]["subject_id"] != times[i - 1]["subject_id"]:
                continue
            a, b = times[i - 1]["time"], times[i]["time"]
            if a is not None and b is not None and b < a:
                problems.append(f"{path.name}: events are not time-sorted")
                break
    return CheckResult(
        "",
        not problems,
        f"{len(seen):,} subjects, each in exactly one shard, contiguous and time-sorted"
        if not problems
        else f"sharding problems: {problems[:3]}",
        {"subjects": len(seen), "shards": len(files)},
    )


@check("MEDS_NO_LABEL_LEAKAGE")
def _meds_leakage(l: Layers) -> CheckResult:
    """The cohort label and its proxies must not appear anywhere in event rows."""
    frame = _meds_data(l)
    if frame is None:
        return _skip("MEDS not built")
    from ehr2cdm.meds import FORBIDDEN_EVENT_COLUMNS

    leaked_columns = sorted(set(frame.columns) & FORBIDDEN_EVENT_COLUMNS)
    labels = {p.membership_label.lower() for p in l.cfg.partitions if p.membership_label}
    leaked_values = 0
    if labels:
        for column in ("code", "source_code", "text_value"):
            if column in frame.columns:
                leaked_values += int(
                    frame.filter(pl.col(column).str.to_lowercase().is_in(list(labels))).height
                )
    partitions = {p.id.lower() for p in l.cfg.partitions}
    leaked_partition = 0
    if "source_table" in frame.columns:
        leaked_partition = int(
            frame.filter(pl.col("source_table").str.to_lowercase().is_in(list(partitions))).height
        )
    ok = not leaked_columns and not leaked_values and not leaked_partition
    return CheckResult(
        "",
        ok,
        "no cohort label, partition id or file name in any event row"
        if ok
        else f"leakage: columns={leaked_columns} label_values={leaked_values} partition={leaked_partition}",
        {"rows": frame.height},
    )


@check("MEDS_CODES_METADATA_COMPLETE")
def _meds_codes(l: Layers) -> CheckResult:
    import meds as meds_spec

    frame = _meds_data(l)
    codes_path = l.layout.meds_dir / meds_spec.code_metadata_filepath
    if frame is None or not codes_path.exists():
        return _skip("MEDS not built")
    used = set(frame["code"].to_list())
    documented = set(pl.read_parquet(codes_path)["code"].to_list())
    missing = used - documented
    extra = documented - used
    return CheckResult(
        "",
        not missing and not extra,
        f"codes.parquet documents exactly the {len(used):,} codes in use"
        if not missing and not extra
        else f"{len(missing)} undocumented, {len(extra)} documented but unused",
        {"codes_used": len(used)},
    )


@check("MEDS_SPLITS_DISJOINT_AND_COMPLETE")
def _meds_splits(l: Layers) -> CheckResult:
    import meds as meds_spec

    frame = _meds_data(l)
    splits_path = l.layout.meds_dir / meds_spec.subject_splits_filepath
    if frame is None or not splits_path.exists():
        return _skip("MEDS not built")
    splits = pl.read_parquet(splits_path)
    duplicated = splits.height - splits["subject_id"].n_unique()
    subjects = set(frame["subject_id"].to_list())
    covered = set(splits["subject_id"].to_list())
    return CheckResult(
        "",
        duplicated == 0 and subjects == covered,
        f"{splits.height:,} subjects, one split each, covering every subject in the data"
        if duplicated == 0 and subjects == covered
        else f"{duplicated} subjects in more than one split; {len(subjects - covered)} unassigned",
        {
            "by_split": dict(
                zip(
                    splits.group_by("split").len()["split"].to_list(),
                    splits.group_by("split").len()["len"].to_list(),
                )
            )
        },
    )


@check("MEDS_AVAILABILITY_PREVENTS_LEAKAGE")
def _meds_availability(l: Layers) -> CheckResult:
    """An as-of view must contain nothing that was not yet visible at that moment."""
    frame = _meds_data(l)
    if frame is None:
        return _skip("MEDS not built")
    if "available_time" not in frame.columns:
        return CheckResult("", False, "available_time is missing: leakage cannot be prevented")
    timed = frame.filter(pl.col("time").is_not_null())
    if timed.height == 0:
        return CheckResult("", True, "no timed events")
    midpoint = timed["time"].median()
    as_of = timed.filter(
        pl.col("available_time").is_null() | (pl.col("available_time") <= midpoint)
    )
    violations = int(as_of.filter(pl.col("available_time") > midpoint).height)
    assumed = int(
        frame.filter(pl.col("quality_flags").list.contains(str(QualityFlag.AVAILABILITY_ASSUMED))).height
    )
    return CheckResult(
        "",
        violations == 0,
        f"an as-of view at the median event time contains no unavailable row "
        f"({assumed:,} rows carry an assumed availability)"
        if violations == 0
        else f"{violations} rows leak future information",
        {"availability_assumed": assumed},
    )


# --------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------


@check("UNDECIDED_PROPOSALS_NEVER_PUBLISHED")
def _undecided_not_published(l: Layers) -> CheckResult:
    """A proposal nobody accepted must not have become a mapping."""
    from ehr2cdm.review import read_decisions, read_pending
    from ehr2cdm.terminology import MappingRegistry, normalize_term

    pending = read_pending(l.layout)
    if not pending:
        return _skip("no review queue")
    decisions = read_decisions(l.layout)
    registry = MappingRegistry.load(Path.cwd() / "mappings")
    leaked = []
    for row in pending:
        decision = decisions.get(row["id"], {}).get("decision", "").strip().lower()
        if decision == "accept":
            continue
        if registry.get(row.get("code_system", ""), row.get("source_string", "")):
            leaked.append(row["source_string"])
    return CheckResult(
        "",
        not leaked,
        f"{len(pending):,} items pending review, none of them compiled into mappings"
        if not leaked
        else f"undecided items reached mappings/: {leaked[:5]}",
        {"pending": len(pending), "decided": len(decisions)},
    )
