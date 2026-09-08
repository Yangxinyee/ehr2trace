"""raw -> source layer, with row-level lineage (design section 5.1).

One task reads one physical unit -- a delimited file, or one sheet of a workbook -- and
writes one parquet file whose name is the content address of its inputs. Nothing here
interprets clinical meaning; the only judgements made are structural:

* a row whose field count does not match the header is quarantined, not padded;
* a row with no patient key is quarantined, not dropped silently;
* every surviving row carries the identifiers needed to find it again in the raw file.

Cell values are stored as the source wrote them (whitespace trimmed, null literals
emptied). The stronger canonical form of section 5.1 is used for the row hash and
nowhere else, which is what makes the same record hash identically when one workbook
types a date as text and another types it as a date -- the difference between
cross-batch deduplication working and failing without a sound.

Storage keeps the written shape on purpose: a bare ``2019-03-04`` says the source had
no time to give, and flattening it to midnight would erase a fact the design requires
to be recorded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from ehr2trace.adapters import get_adapter
from ehr2trace.adapters.base import PhysicalUnit
from ehr2trace.config import DatasetConfig, SourceSpec
from ehr2trace.discover import resolve_source_units
from ehr2trace.hashing import file_sha256, row_sha256, source_cell, source_row_id
from ehr2trace.paths import StreamingParquetWriter, WorkLayout, task_hash
from ehr2trace.schema import (
    QUARANTINE_SCHEMA,
    SOURCE_BASE_FIELDS,
    Coverage,
    ParseStatus,
    QuarantineReason,
)
from ehr2trace.version import CODE_VERSION, HASH_RULE_VERSION

COL_PREFIX = "col__"


@dataclass
class IngestTask:
    dataset_id: str
    partition_id: str
    source_id: str
    file_path: str
    sheet: str | None
    adapter: str
    options: dict[str, Any]
    config_hash: str
    work_root: str
    batch: str | None
    membership_label: str | None
    person_role_aliases: list[str]
    encounter_role_aliases: list[str]
    null_literals: list[str]
    chunk_rows: int = 200_000

    def digest(self, file_sha: str) -> str:
        return task_hash(
            "ingest",
            CODE_VERSION,
            HASH_RULE_VERSION,
            self.config_hash,
            self.dataset_id,
            self.partition_id,
            self.source_id,
            self.sheet or "",
            file_sha,
        )


@dataclass
class IngestResult:
    partition_id: str
    source_id: str
    file_path: str
    sheet: str | None
    file_sha256: str
    columns: list[str]
    rows_read: int
    rows_parsed: int
    rows_quarantined: int
    output_path: str
    quarantine_path: str | None
    reused: bool
    coverage: str
    warnings: list[str] = field(default_factory=list)


def plan_ingest(cfg: DatasetConfig, layout: WorkLayout) -> list[IngestTask]:
    """Config -> task list. A pure function: no IO order dependence, no shared state."""
    tasks: list[IngestTask] = []
    for part in cfg.partitions:
        for source_id, spec in cfg.sources_for(part.id).items():
            for unit in resolve_source_units(cfg, part.id, source_id, spec):
                tasks.append(
                    IngestTask(
                        dataset_id=cfg.dataset_id,
                        partition_id=part.id,
                        source_id=source_id,
                        file_path=str(unit.path),
                        sheet=unit.sheet,
                        adapter=unit.adapter,
                        options=unit.options.model_dump(),
                        config_hash=cfg.config_hash(),
                        work_root=str(layout.root),
                        batch=part.batch,
                        membership_label=part.membership_label,
                        person_role_aliases=_aliases(spec, "person_id"),
                        encounter_role_aliases=_aliases(spec, "encounter_id"),
                        null_literals=list(cfg.time.null_literals),
                        chunk_rows=cfg.execution.chunk_rows,
                    )
                )
    tasks.sort(key=lambda t: (t.partition_id, t.source_id, t.file_path, t.sheet or ""))
    return tasks


def _aliases(spec: SourceSpec, role: str) -> list[str]:
    fs = spec.fields.get(role)
    return list(fs.from_) if fs else []


def run_ingest_task(task: IngestTask) -> IngestResult:
    """Read one physical unit into the source layer. Safe to run in any process."""
    from ehr2trace.config import AdapterOptions

    layout = WorkLayout(root=Path(task.work_root), dataset_id=task.dataset_id)
    file_sha = file_sha256(task.file_path)
    digest = task.digest(file_sha)
    out_path = layout.source_task_path(task.partition_id, task.source_id, digest)
    q_path = layout.quarantine_task_path("ingest", task.partition_id, task.source_id, digest)

    if out_path.exists():
        # Content addressing is the resumption mechanism: this file can only exist if a
        # previous run produced it from byte-identical inputs with the same code.
        import pyarrow.parquet as pq

        meta = pq.read_metadata(out_path)
        columns = [c[len(COL_PREFIX) :] for c in meta.schema.names if c.startswith(COL_PREFIX)]
        q_rows = pq.read_metadata(q_path).num_rows if q_path.exists() else 0
        return IngestResult(
            partition_id=task.partition_id,
            source_id=task.source_id,
            file_path=task.file_path,
            sheet=task.sheet,
            file_sha256=file_sha,
            columns=columns,
            rows_read=meta.num_rows + q_rows,
            rows_parsed=meta.num_rows,
            rows_quarantined=q_rows,
            output_path=str(out_path),
            quarantine_path=str(q_path) if q_path.exists() else None,
            reused=True,
            coverage=str(Coverage.present if meta.num_rows else Coverage.empty),
        )

    options = AdapterOptions.model_validate(task.options)
    unit = PhysicalUnit(
        path=Path(task.file_path), adapter=task.adapter, options=options, sheet=task.sheet
    )
    stream = get_adapter(task.adapter).open(unit)
    columns = list(stream.columns)
    schema = _source_schema(columns)

    person_cols = _match_columns(columns, task.person_role_aliases)
    encounter_cols = _match_columns(columns, task.encounter_role_aliases)
    nulls = tuple(task.null_literals)

    rows_read = rows_parsed = rows_quarantined = 0
    buffer: list[dict[str, Any]] = []
    q_buffer: list[dict[str, Any]] = []

    writer = StreamingParquetWriter(out_path, schema)
    q_writer: StreamingParquetWriter | None = None
    try:
        for row_number, values in stream.rows:
            rows_read += 1
            if len(values) != len(columns):
                q_buffer.append(
                    _quarantine_record(
                        task,
                        file_sha,
                        row_number,
                        QuarantineReason.FIELD_COUNT_MISMATCH,
                        f"expected {len(columns)} fields, found {len(values)}",
                        raw=_preview(values),
                    )
                )
                rows_quarantined += 1
            else:
                cells = [source_cell(v, nulls) for v in values]
                by_name = dict(zip(columns, cells))
                person = _first_present(by_name, person_cols)
                if person is None:
                    q_buffer.append(
                        _quarantine_record(
                            task,
                            file_sha,
                            row_number,
                            QuarantineReason.MISSING_PERSON_KEY,
                            "no patient key in this row",
                            raw=_preview(values),
                        )
                    )
                    rows_quarantined += 1
                else:
                    record: dict[str, Any] = {
                        "source_row_id": source_row_id(
                            task.dataset_id, task.partition_id, task.source_id, file_sha, row_number
                        ),
                        "dataset_id": task.dataset_id,
                        "partition_id": task.partition_id,
                        "batch": task.batch,
                        "membership_label": task.membership_label,
                        "source_id": task.source_id,
                        "source_file": task.file_path,
                        "source_sheet": task.sheet,
                        "source_row_number": row_number,
                        "source_file_sha256": file_sha,
                        "source_row_sha256": row_sha256(values, nulls),
                        "person_source_id": person,
                        "encounter_source_id": _first_present(by_name, encounter_cols),
                        "parse_status": str(ParseStatus.ok),
                        "parse_issues": [],
                    }
                    for name, cell in by_name.items():
                        record[f"{COL_PREFIX}{name}"] = cell
                    buffer.append(record)
                    rows_parsed += 1

            if len(buffer) >= task.chunk_rows:
                writer.write_rows(buffer)
                buffer = []
            if len(q_buffer) >= task.chunk_rows:
                q_writer = q_writer or StreamingParquetWriter(q_path, QUARANTINE_SCHEMA)
                q_writer.write_rows(q_buffer)
                q_buffer = []

        writer.write_rows(buffer)
        if q_buffer:
            q_writer = q_writer or StreamingParquetWriter(q_path, QUARANTINE_SCHEMA)
            q_writer.write_rows(q_buffer)
    finally:
        handle = getattr(stream, "close", None)
        if hasattr(handle, "close"):
            try:
                handle.close()
            except Exception:
                pass

    writer.close()
    if q_writer is not None:
        q_writer.close()

    return IngestResult(
        partition_id=task.partition_id,
        source_id=task.source_id,
        file_path=task.file_path,
        sheet=task.sheet,
        file_sha256=file_sha,
        columns=columns,
        rows_read=rows_read,
        rows_parsed=rows_parsed,
        rows_quarantined=rows_quarantined,
        output_path=str(out_path),
        quarantine_path=str(q_path) if q_writer is not None else None,
        reused=False,
        coverage=str(Coverage.present if rows_parsed else Coverage.empty),
        warnings=list(stream.warnings),
    )


def _source_schema(columns: list[str]) -> pa.Schema:
    fields = list(SOURCE_BASE_FIELDS)
    seen = {f.name for f in fields}
    for col in columns:
        name = f"{COL_PREFIX}{col}"
        if name in seen:
            continue
        fields.append(pa.field(name, pa.string()))
        seen.add(name)
    return pa.schema(fields)


def _match_columns(columns: list[str], aliases: list[str]) -> list[str]:
    """Alias list -> real column names, case-insensitively and in declared order."""
    lowered = {c.strip().lower(): c for c in columns}
    out: list[str] = []
    for alias in aliases:
        hit = lowered.get(alias.strip().lower())
        if hit and hit not in out:
            out.append(hit)
    return out


def _first_present(by_name: dict[str, str], candidates: list[str]) -> str | None:
    for name in candidates:
        value = by_name.get(name)
        if value:
            return value
    return None


def _preview(values: list[object], limit: int = 500) -> str:
    return "\t".join("" if v is None else str(v) for v in values)[:limit]


def _quarantine_record(
    task: IngestTask, file_sha: str, row_number: int, reason: QuarantineReason, detail: str, raw: str
) -> dict[str, Any]:
    return {
        "source_row_id": source_row_id(
            task.dataset_id, task.partition_id, task.source_id, file_sha, row_number
        ),
        "dataset_id": task.dataset_id,
        "partition_id": task.partition_id,
        "source_id": task.source_id,
        "source_file": task.file_path,
        "source_sheet": task.sheet,
        "source_row_number": row_number,
        "stage": "ingest",
        "reason": str(reason),
        "detail": detail,
        "person_source_id": None,
        "raw_row": raw,
    }


def write_manifest(layout: WorkLayout, cfg: DatasetConfig, results: list[IngestResult]) -> Path:
    """Immutable record of exactly which bytes were read."""
    import json

    payload = {
        "dataset_id": cfg.dataset_id,
        "config_hash": cfg.config_hash(),
        "code_version": CODE_VERSION,
        "inputs": [asdict(r) for r in sorted(results, key=lambda r: (r.partition_id, r.source_id, r.file_path, r.sheet or ""))],
    }
    path = layout.manifest_dir / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.partial")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path
