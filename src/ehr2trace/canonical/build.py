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

import csv
import json
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ehr2trace.analytics import analytic_connection
from ehr2trace.canonical.anchors import emit_anchors
from ehr2trace.canonical.dedup import (
    INSTANCE_KEY,
    apply_duplicate_flags,
    dedup_records,
    merge_anchors,
    merge_events,
    merge_links,
    sort_events,
)
from ehr2trace.canonical.normalize import (
    Emission, Row, ShapeContext, build_role_map, filter_rows, get_shape, split_codes,
)
from ehr2trace.canonical.values import spec_from_config
from ehr2trace.config import DatasetConfig
from ehr2trace.paths import StreamingParquetWriter, WorkLayout, task_hash, write_table_atomic
from ehr2trace.reference import load_reference, reference_digest
from ehr2trace.schema import (
    ANCHOR_SCHEMA,
    CANONICAL_EVENT_SCHEMA,
    COHORT_MEMBERSHIP_SCHEMA,
    EVENT_SOURCE_SCHEMA,
    QUALITY_ISSUE_SCHEMA,
    QUARANTINE_SCHEMA,
    EventKind,
    QualityFlag,
)
from ehr2trace.timeutil import TimeContext
from ehr2trace.version import CODE_VERSION, DEFAULT_MAPPING_VERSION, HASH_RULE_VERSION

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
    from ehr2trace.ingest import plan_ingest

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
    #: address of the staged rows this task reads, folded in from the stage plan
    staged_digest: str = ""
    #: address of the reference tables this task reads (see ehr2trace.reference)
    reference_digest: str = ""
    mapping_version: str = DEFAULT_MAPPING_VERSION

    @property
    def digest(self) -> str:
        # `bucket` alone is not enough to identify this unit of work: bucket 5 of 64 and
        # bucket 5 of 256 hold different subjects and would otherwise share an address.
        #
        # `staged_digest` is the input. Without it this address described only *how* to
        # build a bucket and never *what from*, so a run whose data changed while its
        # config, code version and bucket count did not kept every old address, reported
        # success, and silently republished the previous answer. Re-preparing a source,
        # fixing a preparation script and receiving a corrected extract all take that
        # path, and none of them announces itself.
        #
        # `reference_digest` is the other input: which spelling is which unit, what an
        # exact conversion is and what a value can plausibly be are judgements this
        # stage applies, and editing one of those tables has to rebuild the events it
        # changes. It sits here rather than in the config hash so that it invalidates
        # canonical alone -- no ingest reads a reference table.
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
            self.staged_digest,
            self.reference_digest,
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


def plan_canonical(
    cfg: DatasetConfig,
    layout: WorkLayout,
    config_path: Path,
    tz: str | None,
    tz_assumed: bool,
    strict: bool = True,
) -> list[CanonicalTask]:
    """Plan one task per staged bucket, addressed by the staged rows it will read.

    The staged address is taken from the stage plan rather than from the staged files
    themselves. Staging is already content-addressed -- its digest folds in the source
    parquet paths, and those paths *are* content addresses -- so hashing the plan
    carries source content through to here without reading gigabytes to find out that
    nothing changed. ``strict`` follows :func:`plan_stage`: only ``clean`` wants a plan
    over artifacts a version bump has already stranded.
    """
    buckets = sorted(
        int(p.name.split("=")[1]) for p in layout.staged_dir.glob("bucket=*") if p.is_dir()
    )
    staged_digest = task_hash(
        "staged", *sorted(task.digest for task in plan_stage(cfg, layout, strict=strict))
    )
    tables_digest = reference_digest(None)
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
            staged_digest=staged_digest,
            reference_digest=tables_digest,
        )
        for b in buckets
    ]


def run_canonical_task(task: CanonicalTask) -> CanonicalResult:
    """Build one bucket's canonical events, deduplicated and deterministically sorted."""
    from ehr2trace.config import load_dataset_config

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
    # The unit table decides what a `number + unit` cell is: with it, `4.1 mmol/L` is a
    # value and `2 SINUS TACHYCARDIA` is a diagnosis line rather than two of something.
    reference = load_reference(None, cfg.dataset_id)
    value_spec = spec_from_config(None, cfg.time.null_literals, reference.units.spellings)

    all_events: list[dict[str, Any]] = []
    all_links: list[dict[str, Any]] = []
    all_action_keys: list[dict[str, Any]] = []
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
            reference=reference,
        )
        shape = get_shape(spec.shape)
        df = pl.read_parquet(path)
        roles = build_role_map(spec, df.columns)
        for rows in _iter_subject_groups(df):
            wrapped = [Row(r, roles) for r in rows]
            # Both are declared per source and both are no-ops unless declared. Order
            # matters: filtering first means a split never runs on a row that was never
            # part of this logical source.
            wrapped = filter_rows(spec, wrapped, df.columns)
            if spec.code_split is not None:
                wrapped = [piece for row in wrapped for piece in split_codes(ctx, row)]
            if not wrapped:
                continue
            emission: Emission = shape(ctx, wrapped)
            # The merge needs what each row said, not only what the event kept, so the
            # row's values travel beside the event as far as the merge and no further.
            # `strict` because a silent truncation here would attribute one row's
            # values to another row's event.
            for event, instance in zip(emission.events, emission.instances, strict=True):
                row = event.model_dump(mode="python")
                row[INSTANCE_KEY] = instance
                all_events.append(row)
            all_links.extend(emission.links)
            all_action_keys.extend(emission.action_keys)
            all_issues.extend(_with_source(emission.issues, source_id))
            all_quarantine.extend(_with_dataset(emission.quarantine, cfg.dataset_id, source_id))
            anchors, memberships = emit_anchors(ctx, wrapped)
            all_anchors.extend(anchors)
            all_memberships.extend(memberships)

    merged = merge_events(
        all_events,
        {source_id: spec.merge_rules for source_id, spec in cfg.sources.items()},
        cfg.dataset_id,
    )
    all_issues.extend(merged.issues)
    all_quarantine.extend(merged.quarantine)

    events, death_issues, merged_deaths = apply_cross_event_rules(
        merged.events, task.timezone_name
    )
    all_issues.extend(death_issues)
    # A death that collapsed onto a more precise one takes its source rows with it:
    # every row that recorded the death still points at the death that survived.
    # Rewritten in place -- a bucket's link list is the largest thing here, and a copy
    # of it to change a handful of rows is a copy this stage cannot afford.
    if merged_deaths:
        for link in all_links:
            survivor = merged_deaths.get(link["event_id"])
            if survivor is not None:
                link["event_id"] = survivor

    links, partitions = merge_links(all_links)
    events = apply_duplicate_flags(events, partitions)
    events, _caused = resolve_causes(events, all_action_keys)
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


def resolve_causes(
    events: Sequence[dict[str, Any]], action_keys: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Turn the source keys the shapes recorded into ``caused_by_event_id``.

    An administration names the order it carries out, but it names it by the order's own
    key, because that is what the source row holds. The order's event id exists only
    once the order entry table has been read too, so the join waits until here.

    A key naming nothing is left null rather than guessed. The order may sit outside the
    extract, or in a source nobody configured, and an invented parent would assert that
    a dispensed drug was ordered when the evidence for that is simply absent.
    """
    declared: dict[str, str] = {}
    # Sorted so that two rows claiming one key resolve the same way on every rebuild,
    # whatever order the workers happened to finish in.
    for rec in sorted(action_keys, key=lambda r: (r["key"], r["event_id"])):
        if rec["role"] == "declares":
            declared.setdefault(rec["key"], rec["event_id"])
    wanted = {r["event_id"]: r["key"] for r in action_keys if r["role"] == "caused_by"}
    if not declared or not wanted:
        return list(events), 0

    out: list[dict[str, Any]] = []
    resolved = 0
    for event in events:
        key = wanted.get(event["event_id"])
        target = declared.get(key) if key is not None else None
        if target is not None and target != event["event_id"]:
            event = {**event, "caused_by_event_id": target}
            resolved += 1
        out.append(event)
    return out, resolved


def apply_cross_event_rules(
    events: Sequence[dict[str, Any]], zone_name: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Rules that need a whole subject in view (T1.8, D-R11).

    Two sources rarely record a death the same way: one files the date the registrar
    has, the other the hour the monitor stopped. On one calendar day those are one
    death written down twice, and keeping both would publish a patient who died twice.
    They collapse onto the more precise time -- a recorded hour beats a bare date --
    and the dropped event's source rows are re-pointed at the survivor, so the merge
    loses no lineage. The returned mapping says which id became which.

    The day is the *local* day. ``event_time`` is a naive UTC instant, so a death at
    21:30 in New York is stored under the next UTC date; comparing UTC dates would
    split one death in two for every patient who died after seven in the evening, and
    the split would look like a genuine disagreement.

    Two different local days is the case a converter must not settle: which source is
    wrong about the day someone died cannot be read off the data. Both events stay,
    both are flagged, and the pair is reported for a human to resolve.

    Records dated after a patient's death are real -- back-filled problem lists are
    routine -- so they are flagged and kept, never deleted and never re-dated.
    """
    zone = ZoneInfo(zone_name) if zone_name else None
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("event_kind") == str(EventKind.death) and e.get("event_time") is not None:
            by_subject.setdefault(int(e["subject_id"]), []).append(e)

    issues: list[dict[str, Any]] = []
    merged_deaths: dict[str, str] = {}
    added_flags: dict[str, set[str]] = {}
    deaths: dict[int, list[datetime]] = {}
    for subject_id in sorted(by_subject):
        by_day: dict[date, list[dict[str, Any]]] = {}
        for e in by_subject[subject_id]:
            by_day.setdefault(_local(e["event_time"], zone).date(), []).append(e)
        surviving: list[dict[str, Any]] = []
        for day in sorted(by_day):
            group = sorted(by_day[day], key=lambda e: _death_precision(e, zone))
            survivor = group[0]
            surviving.append(survivor)
            if len(group) > 1:
                added_flags.setdefault(survivor["event_id"], set()).add(
                    str(QualityFlag.DEATH_TIME_MERGED)
                )
                for dropped in group[1:]:
                    merged_deaths[dropped["event_id"]] = survivor["event_id"]
        if len(by_day) > 1:
            for e in surviving:
                added_flags.setdefault(e["event_id"], set()).add(
                    str(QualityFlag.DEATH_DATE_CONFLICT)
                )
            issues.append(
                {
                    "issue_type": str(QualityFlag.DEATH_DATE_CONFLICT),
                    "severity": "error",
                    "stage": "canonical",
                    "subject_id": subject_id,
                    "source_row_id": None,
                    "event_id": None,
                    "partition_id": None,
                    "source_id": None,
                    "detail": "; ".join(d.isoformat() for d in sorted(by_day)),
                }
            )
        deaths[subject_id] = [e["event_time"] for e in surviving]

    out: list[dict[str, Any]] = []
    for e in events:
        if e["event_id"] in merged_deaths:
            continue
        row = dict(e)
        flags = set(row.get("quality_flags") or [])
        flags |= added_flags.get(row["event_id"], set())
        subject_deaths = deaths.get(int(row["subject_id"]))
        if (
            subject_deaths
            and row.get("event_time") is not None
            and row.get("event_kind") != str(EventKind.death)
        ):
            earliest = min(subject_deaths)
            if row["event_time"] > earliest + timedelta(days=1):
                flags.add(str(QualityFlag.RECORDED_AFTER_DEATH))
        if flags != set(row.get("quality_flags") or []):
            row["quality_flags"] = sorted(flags)
        out.append(row)
    return out, issues, merged_deaths


def _local(instant: datetime, zone: ZoneInfo | None) -> datetime:
    """A naive UTC instant read as the wall clock of the dataset's own zone."""
    if zone is None:
        return instant
    return instant.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


def _death_precision(event: dict[str, Any], zone: ZoneInfo | None) -> tuple[bool, datetime, str]:
    """Rank of a death event within one local day: the most precise time first.

    A local midnight is what a date with no time becomes, so any other local time is
    better evidence. Among equals the earliest wins, and the id settles the rest, so
    the choice does not depend on which worker produced which row.
    """
    local = _local(event["event_time"], zone)
    midnight = (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)
    return (midnight, event["event_time"], event["event_id"])


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
    write_death_conflicts(layout)
    return counts


#: ``review/death_conflicts.csv``: one row per subject whose death dates disagree
DEATH_CONFLICT_FIELDS: tuple[str, ...] = ("subject_id", "dates")


def write_death_conflicts(layout: WorkLayout) -> None:
    """List the deaths a person has to look at, one row per subject.

    Written even when it is empty. "No subject has two death dates" is a result, and a
    file that only appears when something is wrong cannot be told from a step that did
    not run.
    """
    rows: list[tuple[Any, ...]] = []
    source = layout.canonical_path("quality_issue")
    if source.exists():
        frame = (
            pl.read_parquet(source)
            .filter(pl.col("issue_type") == str(QualityFlag.DEATH_DATE_CONFLICT))
            .select(["subject_id", "detail"])
            .unique()
            .sort(["subject_id", "detail"])
        )
        rows = list(frame.iter_rows())
    path = layout.review_dir / "death_conflicts.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(DEATH_CONFLICT_FIELDS)
        writer.writerows(rows)
