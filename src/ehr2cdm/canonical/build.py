"""Staging and canonical construction (design sections 3.3, 5.2).

Two stages live here.

*Staging* attaches the subject id decided by the identity barrier and rewrites the
source layer bucketed by subject, so that everything about one patient is in one place.
A patient always lands in the same bucket, which is what makes bucket-parallel work
give the same answer as single-process work.

*Canonical* turns each bucket's rows into events. Because a subject's rows are all in
one bucket, deduplication inside a bucket is complete deduplication -- no global pass,
no cross-worker coordination.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ehr2cdm.analytics import analytic_connection
from ehr2cdm.canonical.anchors import emit_anchors
from ehr2cdm.canonical.dedup import (
    apply_duplicate_flags,
    dedup_records,
    merge_anchors,
    merge_events,
    merge_links,
    sort_events,
)
from ehr2cdm.canonical.normalize import Emission, Row, ShapeContext, build_role_map, get_shape
from ehr2cdm.canonical.values import spec_from_config
from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import StreamingParquetWriter, WorkLayout, task_hash, write_table_atomic
from ehr2cdm.schema import (
    ANCHOR_SCHEMA,
    CANONICAL_EVENT_SCHEMA,
    COHORT_MEMBERSHIP_SCHEMA,
    EVENT_SOURCE_SCHEMA,
    QUALITY_ISSUE_SCHEMA,
    QUARANTINE_SCHEMA,
    EventKind,
    QualityFlag,
)
from ehr2cdm.timeutil import TimeContext
from ehr2cdm.version import CODE_VERSION, DEFAULT_MAPPING_VERSION, HASH_RULE_VERSION

STAGE_BATCH_ROWS = 100_000


# --------------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------------


@dataclass
class StageTask:
    dataset_id: str
    partition_id: str
    source_id: str
    inputs: list[str]
    work_root: str
    config_hash: str
    bucket_count: int

    @property
    def digest(self) -> str:
        # bucket_count is no longer inside config_hash -- it says how to split the work,
        # not what to produce. It still belongs here: it decides which subject lands in
        # which bucket file, so a run resumed under a different partitioning must not
        # reuse these outputs.
        return task_hash(
            "stage",
            CODE_VERSION,
            self.config_hash,
            self.partition_id,
            self.source_id,
            self.bucket_count,
            *sorted(self.inputs),
        )


@dataclass
class StageResult:
    partition_id: str
    source_id: str
    rows: int
    buckets: int
    reused: bool


def plan_stage(cfg: DatasetConfig, layout: WorkLayout, strict: bool = True) -> list[StageTask]:
    """Plan staging from the ingest manifest, never from a directory listing.

    Source outputs are content-addressed, so a code or config change leaves the
    previous version's parquet beside the new one until someone cleans up. Globbing the
    directory would read both and stage every row twice -- which is silent, because
    events deduplicate by id and only the counts that do not (quarantine) come out
    wrong. The manifest records exactly which files the current ingest produced, so it
    is the only honest answer to "what is the input to staging".
    """
    manifest_path = layout.manifest_dir / "inputs.json"
    if not manifest_path.exists():
        raise RuntimeError(f"no ingest manifest at {manifest_path}; run ingest first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # The manifest can also be stale: it survives a code-version bump that invalidated
    # the very files it names. Checking each recorded output against the address the
    # current code would give it turns the version guard from advisory into enforced.
    from ehr2cdm.ingest import plan_ingest

    expected = {
        (t.partition_id, t.source_id, t.file_path, t.sheet or ""): t
        for t in plan_ingest(cfg, layout)
    }

    by_source: dict[tuple[str, str], list[str]] = {}
    missing: list[str] = []
    stale: list[str] = []
    for unit in manifest["inputs"]:
        path = Path(unit["output_path"])
        if not path.exists():
            missing.append(unit["output_path"])
            continue
        task = expected.get(
            (unit["partition_id"], unit["source_id"], unit["file_path"], unit["sheet"] or "")
        )
        if task is not None and path.stem != task.digest(unit["file_sha256"]):
            stale.append(f"{unit['partition_id']}/{unit['source_id']}")
            continue
        by_source.setdefault((unit["partition_id"], unit["source_id"]), []).append(str(path))
    # `strict=False` is for callers whose whole job is dealing with stale artifacts --
    # refusing to plan because things are stale would make cleanup impossible exactly
    # when it is needed.
    if missing and strict:
        raise RuntimeError(
            f"{len(missing)} source files named by the manifest are gone (first: "
            f"{missing[0]}); re-run ingest"
        )
    if stale and strict:
        raise RuntimeError(
            f"{len(stale)} source files were produced by a different code or config "
            f"version (first: {stale[0]}); re-run ingest before canonical"
        )

    tasks = [
        StageTask(
            dataset_id=cfg.dataset_id,
            partition_id=partition_id,
            source_id=source_id,
            inputs=sorted(set(paths)),
            work_root=str(layout.root),
            config_hash=cfg.config_hash(),
            bucket_count=cfg.execution.bucket_count,
        )
        for (partition_id, source_id), paths in by_source.items()
        if source_id in cfg.sources
    ]
    tasks.sort(key=lambda t: (t.partition_id, t.source_id))
    return tasks


def run_stage_task(task: StageTask) -> StageResult:
    """Attach subject ids and rewrite one source bucketed by subject."""
    layout = WorkLayout(root=Path(task.work_root), dataset_id=task.dataset_id)
    marker = layout.staged_dir / "_done" / f"{task.partition_id}__{task.source_id}__{task.digest}.json"
    if marker.exists():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return StageResult(task.partition_id, task.source_id, payload["rows"], payload["buckets"], True)

    subject_map = pl.read_parquet(layout.identity_dir / "subject_map.parquet").select(
        ["person_source_id", "subject_id", "bucket"]
    )

    writers: dict[int, StreamingParquetWriter] = {}
    schema: pa.Schema | None = None
    total = 0
    try:
        for input_path in task.inputs:
            pf = pq.ParquetFile(input_path)
            for batch in pf.iter_batches(batch_size=STAGE_BATCH_ROWS):
                df = pl.from_arrow(pa.Table.from_batches([batch]))
                df = df.join(subject_map, on="person_source_id", how="inner")
                if df.height == 0:
                    continue
                if schema is None:
                    schema = df.to_arrow().schema
                for bucket, part_df in df.partition_by("bucket", as_dict=True).items():
                    b = int(bucket[0] if isinstance(bucket, tuple) else bucket)
                    writer = writers.get(b)
                    if writer is None:
                        path = layout.staged_path(b, task.partition_id, task.source_id)
                        writer = StreamingParquetWriter(path, schema)
                        writers[b] = writer
                    table = part_df.to_arrow().cast(schema)
                    writer._writer.write_table(table)  # already an Arrow table; avoid a row round-trip
                    writer.rows_written += table.num_rows
                    total += table.num_rows
    finally:
        for writer in writers.values():
            writer.close()

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"rows": total, "buckets": len(writers)}), encoding="utf-8")
    return StageResult(task.partition_id, task.source_id, total, len(writers), False)


# --------------------------------------------------------------------------------
# canonical
# --------------------------------------------------------------------------------


@dataclass
class CanonicalTask:
    dataset_id: str
    bucket: int
    work_root: str
    config_path: str
    config_hash: str
    timezone_name: str | None
    timezone_assumed: bool
    bucket_count: int
    mapping_version: str = DEFAULT_MAPPING_VERSION

    @property
    def digest(self) -> str:
        # `bucket` alone is not enough to identify this unit of work: bucket 5 of 64 and
        # bucket 5 of 256 hold different subjects and would otherwise share an address.
        return task_hash(
            "canonical",
            CODE_VERSION,
            HASH_RULE_VERSION,
            self.config_hash,
            self.mapping_version,
            self.timezone_name or "",
            self.timezone_assumed,
            self.bucket_count,
            self.bucket,
        )


@dataclass
class CanonicalResult:
    bucket: int
    events: int
    links: int
    anchors: int
    memberships: int
    issues: int
    quarantined: int
    reused: bool


def plan_canonical(cfg: DatasetConfig, layout: WorkLayout, config_path: Path, tz: str | None, tz_assumed: bool) -> list[CanonicalTask]:
    buckets = sorted(
        int(p.name.split("=")[1]) for p in layout.staged_dir.glob("bucket=*") if p.is_dir()
    )
    return [
        CanonicalTask(
            dataset_id=cfg.dataset_id,
            bucket=b,
            work_root=str(layout.root),
            config_path=str(config_path),
            config_hash=cfg.config_hash(),
            timezone_name=tz,
            timezone_assumed=tz_assumed,
            bucket_count=cfg.execution.bucket_count,
        )
        for b in buckets
    ]


def run_canonical_task(task: CanonicalTask) -> CanonicalResult:
    """Build one bucket's canonical events, deduplicated and deterministically sorted."""
    from ehr2cdm.config import load_dataset_config

    cfg = load_dataset_config(task.config_path)
    layout = WorkLayout(root=Path(task.work_root), dataset_id=task.dataset_id)
    out_dir = layout.bucket_dir(task.bucket) / task.digest
    if (out_dir / "_done").exists():
        payload = json.loads((out_dir / "_done").read_text(encoding="utf-8"))
        return CanonicalResult(task.bucket, **payload, reused=True)

    time_ctx = TimeContext(
        formats=tuple(cfg.time.formats),
        null_literals=tuple(cfg.time.null_literals),
        timezone_name=task.timezone_name,
        timezone_assumed=task.timezone_assumed,
    )
    value_spec = spec_from_config(None, cfg.time.null_literals)

    all_events: list[dict[str, Any]] = []
    all_links: list[dict[str, Any]] = []
    all_anchors: list[dict[str, Any]] = []
    all_memberships: list[dict[str, Any]] = []
    all_issues: list[dict[str, Any]] = []
    all_quarantine: list[dict[str, Any]] = []

    staged = sorted((layout.staged_dir / f"bucket={task.bucket:04d}").glob("*.parquet"))
    for path in staged:
        partition_id, source_id = path.stem.split("__", 1)
        spec = cfg.sources.get(source_id)
        if spec is None:
            continue
        ctx = ShapeContext(
            cfg=cfg,
            source_id=source_id,
            spec=spec,
            time=time_ctx,
            values=value_spec,
            mapping_version=task.mapping_version,
        )
        shape = get_shape(spec.shape)
        df = pl.read_parquet(path)
        roles = build_role_map(spec, df.columns)
        for rows in _iter_subject_groups(df):
            wrapped = [Row(r, roles) for r in rows]
            emission: Emission = shape(ctx, wrapped)
            all_events.extend(e.model_dump(mode="python") for e in emission.events)
            all_links.extend(emission.links)
            all_issues.extend(_with_source(emission.issues, source_id))
            all_quarantine.extend(_with_dataset(emission.quarantine, cfg.dataset_id, source_id))
            anchors, memberships = emit_anchors(ctx, wrapped)
            all_anchors.extend(anchors)
            all_memberships.extend(memberships)

    events = merge_events(all_events)
    links, partitions = merge_links(all_links)
    events = apply_duplicate_flags(events, partitions)
    events, death_issues = apply_cross_event_rules(events)
    all_issues.extend(death_issues)
    events = sort_events(events)
    anchors = merge_anchors(all_anchors)
    memberships = dedup_records(all_memberships, ["subject_id", "partition_id", "anchor_id"])
    issues = dedup_records(
        all_issues, ["issue_type", "subject_id", "source_row_id", "event_id", "detail"]
    )
    # One source row can only be quarantined once for one reason. Deduplicating here
    # means a double-staged input shows up as an unchanged count rather than as a
    # quietly inflated one.
    quarantine = dedup_records(all_quarantine, ["source_row_id", "stage", "reason", "detail"])

    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "events.parquet", events, CANONICAL_EVENT_SCHEMA)
    _write(out_dir / "event_source.parquet", links, EVENT_SOURCE_SCHEMA)
    _write(out_dir / "anchors.parquet", anchors, ANCHOR_SCHEMA)
    _write(out_dir / "cohort_membership.parquet", memberships, COHORT_MEMBERSHIP_SCHEMA)
    _write(out_dir / "quality_issue.parquet", issues, QUALITY_ISSUE_SCHEMA)
    _write(out_dir / "quarantine.parquet", quarantine, QUARANTINE_SCHEMA)

    counts = {
        "events": len(events),
        "links": len(links),
        "anchors": len(anchors),
        "memberships": len(memberships),
        "issues": len(issues),
        "quarantined": len(quarantine),
    }
    (out_dir / "_done").write_text(json.dumps(counts), encoding="utf-8")
    return CanonicalResult(task.bucket, **counts, reused=False)


def _iter_subject_groups(df: pl.DataFrame) -> Iterator[list[dict[str, Any]]]:
    """Rows grouped by subject, in subject order.

    Grouping never crosses a subject, so a worker holds one patient's rows for one
    source at a time regardless of how large the dataset is.
    """
    if df.height == 0:
        return
    ordered = df.sort("subject_id", "source_row_id")
    current: list[dict[str, Any]] = []
    current_subject: int | None = None
    for row in ordered.iter_rows(named=True):
        subject = row["subject_id"]
        if current_subject is None:
            current_subject = subject
        if subject != current_subject:
            yield current
            current = []
            current_subject = subject
        current.append(row)
    if current:
        yield current


def apply_cross_event_rules(events: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rules that need a whole subject in view.

    Records dated after a patient's death are real -- back-filled problem lists are
    routine -- so they are flagged and kept, never deleted and never re-dated. A
    disagreement between two sources about the death date is reported rather than
    resolved by picking one.
    """
    deaths: dict[int, list[datetime]] = {}
    for e in events:
        if e.get("event_kind") == str(EventKind.death) and e.get("event_time") is not None:
            deaths.setdefault(int(e["subject_id"]), []).append(e["event_time"])

    issues: list[dict[str, Any]] = []
    for subject_id, times in deaths.items():
        distinct = sorted(set(times))
        if len(distinct) > 1:
            issues.append(
                {
                    "issue_type": "DEATH_DATE_CONFLICT",
                    "severity": "error",
                    "stage": "canonical",
                    "subject_id": subject_id,
                    "source_row_id": None,
                    "event_id": None,
                    "partition_id": None,
                    "source_id": None,
                    "detail": "; ".join(d.isoformat() for d in distinct),
                }
            )

    out: list[dict[str, Any]] = []
    for e in events:
        row = dict(e)
        subject_deaths = deaths.get(int(row["subject_id"]))
        if (
            subject_deaths
            and row.get("event_time") is not None
            and row.get("event_kind") != str(EventKind.death)
        ):
            earliest = min(subject_deaths)
            if row["event_time"] > earliest + timedelta(days=1):
                row["quality_flags"] = sorted(
                    set(row.get("quality_flags") or []) | {str(QualityFlag.RECORDED_AFTER_DEATH)}
                )
        out.append(row)
    return out, issues


def _write(path: Path, rows: Sequence[dict[str, Any]], schema: pa.Schema) -> None:
    cols = {f.name: [r.get(f.name) for r in rows] for f in schema}
    write_table_atomic(pa.table(cols, schema=schema), path)


def _with_source(issues: Sequence[dict[str, Any]], source_id: str) -> list[dict[str, Any]]:
    out = []
    for issue in issues:
        row = dict(issue)
        row["source_id"] = source_id
        out.append(row)
    return out


def _with_dataset(rows: Sequence[dict[str, Any]], dataset_id: str, source_id: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        row = dict(r)
        row["dataset_id"] = dataset_id
        row["source_id"] = source_id
        out.append(row)
    return out


# --------------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------------

MERGE_TABLES = {
    "events": CANONICAL_EVENT_SCHEMA,
    "event_source": EVENT_SOURCE_SCHEMA,
    "anchors": ANCHOR_SCHEMA,
    "cohort_membership": COHORT_MEMBERSHIP_SCHEMA,
    "quality_issue": QUALITY_ISSUE_SCHEMA,
    "quarantine": QUARANTINE_SCHEMA,
}

SORT_KEYS = {
    "events": ["subject_id", "event_time", "event_id"],
    "event_source": ["event_id", "source_row_id"],
    "anchors": ["subject_id", "anchor_date", "partition_id"],
    "cohort_membership": ["subject_id", "partition_id", "anchor_id"],
    "quality_issue": ["issue_type", "subject_id", "source_row_id"],
    "quarantine": ["partition_id", "source_id", "source_row_id"],
}


def merge_buckets(layout: WorkLayout, digests: dict[int, str]) -> dict[str, int]:
    """Concatenate bucket outputs into the canonical layer, sorted, order-independent.

    The sort is what makes the result independent of which worker finished first, and
    it runs in the database engine rather than in memory: the link table alone reaches
    a hundred million rows, and a merge that only works while it fits in RAM is a merge
    that fails on the next dataset.

    That last sentence was aspirational until MIMIC-IV falsified it: an in-memory
    connection with nowhere to spill grew to 195 GB and the kernel killed it, on exactly
    the "next dataset" the docstring predicted. :func:`analytic_connection` is what makes
    the claim true, and it lives in one place because the MEDS build made the identical
    promise and broke it the same way.
    """
    counts: dict[str, int] = {}
    with analytic_connection(layout.root / "_merge_scratch") as con:
        for name, schema in MERGE_TABLES.items():
            files = [
                str(layout.bucket_dir(bucket) / digests[bucket] / f"{name}.parquet")
                for bucket in sorted(digests)
                if (layout.bucket_dir(bucket) / digests[bucket] / f"{name}.parquet").exists()
            ]
            target = layout.canonical_path(name)
            if not files:
                write_table_atomic(
                    pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema),
                    target,
                )
                counts[name] = 0
                continue
            keys = [k for k in SORT_KEYS[name] if k in schema.names]
            order = f"ORDER BY {', '.join(f'{k} NULLS LAST' for k in keys)}" if keys else ""
            tmp = target.with_suffix(".parquet.partial")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Columns are named explicitly and hive partitioning is off: bucket
            # directories are an internal detail of how the work was split, and a
            # reader that infers a column from a directory name would silently widen
            # the frozen canonical schema.
            columns = ", ".join(f.name for f in schema)
            con.execute(
                f"COPY (SELECT {columns} FROM read_parquet($files, hive_partitioning=false) "
                f"{order}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                {"files": files},
            )
            os.replace(tmp, target)
            counts[name] = int(
                con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
            )
    return counts
