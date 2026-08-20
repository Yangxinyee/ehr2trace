"""canonical -> MEDS (design section 7).

MEDS is the event-stream view: one row per event, one shard per patient, sorted by
time. Everything interesting about this conversion is about what is *not* written.

* ``available_time`` is an extension column and is the field that makes an
  availability-aware model possible at all. Where the source only says when something
  happened, not when anyone could see it, the same time is used and the row is flagged
  ``AVAILABILITY_ASSUMED`` rather than left to look like ground truth.
* The cohort label, the partition id and the file names do **not** appear in event
  rows. The label is the thing a model would be asked to predict, and a directory name
  that encodes it is the most direct leak there is. It stays in the audit layer.
* One clinical fact gets one code: the mapped OMOP code when there is one, otherwise
  the source code. Emitting both would train a model on the same fact twice.
* Splits are assigned by a stable hash of the subject id, after identity resolution
  across every partition, so a patient who appears in two batches cannot land in two
  splits.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

import meds as meds_spec
from ehr2cdm.config import DatasetConfig
from ehr2cdm.hashing import split_of
from ehr2cdm.paths import WorkLayout, write_table_atomic
from ehr2cdm.schema import EventKind, QualityFlag
from ehr2cdm.terminology import MappingRegistry, Vocabulary, collect_terms, normalize_term, resolve_terms
from ehr2cdm.version import CODE_VERSION

#: Required MEDS columns plus this project's extensions (design section 7.1).
MEDS_SCHEMA = pa.schema(
    [
        pa.field("subject_id", pa.int64()),
        pa.field("time", pa.timestamp("us")),
        pa.field("code", pa.string()),
        pa.field("numeric_value", pa.float32()),
        pa.field("text_value", pa.large_string()),
        # extensions
        pa.field("event_id", pa.string()),
        pa.field("encounter_id", pa.string()),
        pa.field("available_time", pa.timestamp("us")),
        pa.field("end_time", pa.timestamp("us")),
        pa.field("source_table", pa.string()),
        pa.field("source_row_ids", pa.list_(pa.string())),
        pa.field("omop_concept_id", pa.int64()),
        pa.field("source_code", pa.string()),
        pa.field("unit", pa.string()),
        pa.field("event_kind", pa.string()),
        pa.field("quality_flags", pa.list_(pa.string())),
    ]
)

CODE_METADATA_SCHEMA = pa.schema(
    [
        pa.field("code", pa.string()),
        pa.field("description", pa.string()),
        pa.field("parent_codes", pa.list_(pa.string())),
        pa.field("source_vocabulary", pa.string()),
        pa.field("omop_concept_id", pa.int64()),
        pa.field("mapping_status", pa.string()),
        pa.field("event_kind", pa.string()),
        pa.field("n_events", pa.int64()),
    ]
)

SUBJECT_SPLIT_SCHEMA = pa.schema(
    [pa.field("subject_id", pa.int64()), pa.field("split", pa.string())]
)

#: Columns that must never reach an event row, whatever a future edit does.
FORBIDDEN_EVENT_COLUMNS = frozenset({"membership_label", "partition_id", "batch", "source_file", "person_source_id"})


def build_meds(cfg: DatasetConfig, layout: WorkLayout) -> dict[str, Any]:
    events = pl.read_parquet(layout.canonical_path("events"))
    links = pl.read_parquet(layout.canonical_path("event_source"))

    vocabulary = Vocabulary.open(_vocab_dir())
    mappings = MappingRegistry.load(Path.cwd() / "mappings")
    terms = collect_terms(events.iter_rows(named=True))
    resolved, _unresolved = resolve_terms(list(terms.values()), vocabulary, mappings)

    row_ids = _source_rows_by_event(links)
    coded = _to_meds_rows(cfg, events, resolved, row_ids)

    subjects = sorted({int(r["subject_id"]) for r in coded})
    splits = {
        sid: split_of(sid, [tuple(s) for s in cfg.meds.splits], salt=cfg.meds.split_salt)
        for sid in subjects
    }

    data_dir = layout.meds_dir / meds_spec.data_subdirectory
    _clear(data_dir)
    shards = _write_shards(data_dir, coded, splits, cfg.meds.shard_size)

    codes_path = layout.meds_dir / meds_spec.code_metadata_filepath
    _write_code_metadata(codes_path, coded, resolved, vocabulary)

    splits_path = layout.meds_dir / meds_spec.subject_splits_filepath
    write_table_atomic(
        pa.table(
            {
                "subject_id": [sid for sid in subjects],
                "split": [splits[sid] for sid in subjects],
            },
            schema=SUBJECT_SPLIT_SCHEMA,
        ),
        splits_path,
    )

    metadata_path = layout.meds_dir / meds_spec.dataset_metadata_filepath
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(_dataset_metadata(cfg, vocabulary), indent=2, sort_keys=True), encoding="utf-8"
    )

    vocabulary.close()
    result = {
        "events": len(coded),
        "subjects": len(subjects),
        "shards": shards,
        "splits": {name: sum(1 for s in splits.values() if s == name) for name, _w in cfg.meds.splits},
        "codes": _count_codes(coded),
        "mapped_codes": sum(1 for r in coded if r["omop_concept_id"]),
        "membership_label_included": cfg.meds.include_membership_label,
        "data_dir": str(data_dir),
    }
    (layout.meds_dir / "build_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


# --------------------------------------------------------------------------------


def _to_meds_rows(cfg: DatasetConfig, events: pl.DataFrame, resolved: dict, row_ids: dict) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events.iter_rows(named=True):
        code, concept_id = _code_for(event, resolved)
        available = event.get("available_time")
        flags = list(event.get("quality_flags") or [])
        if available is None and event.get("event_time") is not None:
            available = event["event_time"]
            if str(QualityFlag.AVAILABILITY_ASSUMED) not in flags:
                flags.append(str(QualityFlag.AVAILABILITY_ASSUMED))
        rows.append(
            {
                "subject_id": int(event["subject_id"]),
                "time": event.get("event_time"),
                "code": code,
                "numeric_value": _as_float32(event.get("value_number")),
                "text_value": event.get("value_text"),
                "event_id": event["event_id"],
                "encounter_id": event.get("encounter_id"),
                "available_time": available,
                "end_time": event.get("end_time"),
                "source_table": event.get("source_id"),
                "source_row_ids": sorted(row_ids.get(event["event_id"], [])),
                "omop_concept_id": concept_id,
                "source_code": event.get("source_code"),
                "unit": event.get("unit_source"),
                "event_kind": event.get("event_kind"),
                "quality_flags": sorted(set(flags)),
            }
        )
    return rows


def _code_for(event: dict[str, Any], resolved: dict) -> tuple[str, int | None]:
    """One code per fact: the mapped concept where there is one, else the source code.

    Never both. Two codes for one clinical fact is a duplicated training signal, and
    the unmapped source code stays available in its own extension column regardless.
    """
    kind = event.get("event_kind")
    if kind == str(EventKind.death):
        return meds_spec.death_code, None

    code_system = event.get("code_system") or "SOURCE"
    source_code = event.get("source_code")
    match = resolved.get((code_system, normalize_term(str(source_code)))) if source_code else None
    if match is not None:
        return f"OMOP/{match.concept_id}", int(match.concept_id)

    source_table = event.get("source_id") or "unknown"
    return f"SOURCE/{source_table}/{normalize_term(str(source_code)) or 'unspecified'}", None


def _source_rows_by_event(links: pl.DataFrame) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for event_id, source_row_id in zip(links["event_id"].to_list(), links["source_row_id"].to_list()):
        out.setdefault(event_id, []).append(source_row_id)
    return out


def _write_shards(data_dir: Path, rows: list[dict[str, Any]], splits: dict[int, str], shard_size: int) -> int:
    """Write shards grouped by split, with each subject's events contiguous and sorted.

    A subject never spans two shards, which is what lets a reader stream one patient
    without a global index -- and what stops one patient's events landing in two splits.
    """
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_subject.setdefault(row["subject_id"], []).append(row)

    by_split: dict[str, list[int]] = {}
    for subject_id in sorted(by_subject):
        by_split.setdefault(splits[subject_id], []).append(subject_id)

    written = 0
    size = max(1, shard_size)
    for split in sorted(by_split):
        subjects = by_split[split]
        for index in range(0, len(subjects), size):
            chunk = subjects[index : index + size]
            shard_rows: list[dict[str, Any]] = []
            for subject_id in chunk:
                shard_rows.extend(_sorted_events(by_subject[subject_id]))
            path = data_dir / split / f"{index // size:06d}.parquet"
            write_table_atomic(_table(shard_rows), path)
            written += 1
    return written


def _sorted_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Time-sorted, with static (timeless) events first and ties broken deterministically."""
    return sorted(
        rows,
        key=lambda r: (
            r["time"] is not None,
            r["time"] or datetime.min,
            r["code"],
            r["event_id"],
        ),
    )


def _table(rows: list[dict[str, Any]]) -> pa.Table:
    table = pa.table({f.name: [r.get(f.name) for r in rows] for f in MEDS_SCHEMA}, schema=MEDS_SCHEMA)
    # The installed MEDS version owns the definition of a valid data table; failing
    # here is preferable to publishing something a downstream reader will reject.
    meds_spec.DataSchema.validate(table)
    return table


def _write_code_metadata(path: Path, rows: list[dict[str, Any]], resolved: dict, vocabulary) -> None:
    """Every code that actually appears, with its mapping status. No more, no less."""
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = seen.get(row["code"])
        if entry is None:
            mapped = row["omop_concept_id"] is not None
            seen[row["code"]] = {
                "code": row["code"],
                "description": row.get("source_code") or row["code"],
                "parent_codes": [],
                "source_vocabulary": "OMOP" if mapped else "SOURCE",
                "omop_concept_id": row["omop_concept_id"],
                "mapping_status": "mapped" if mapped else "unmapped",
                "event_kind": row.get("event_kind"),
                "n_events": 1,
            }
        else:
            entry["n_events"] += 1
    table = pa.table(
        {f.name: [seen[c].get(f.name) for c in sorted(seen)] for f in CODE_METADATA_SCHEMA},
        schema=CODE_METADATA_SCHEMA,
    )
    meds_spec.CodeMetadataSchema.validate(table)
    write_table_atomic(table, path)


def _dataset_metadata(cfg: DatasetConfig, vocabulary) -> dict[str, Any]:
    return {
        "dataset_name": cfg.dataset_id,
        "dataset_version": cfg.config_hash()[:12],
        "etl_name": "ehr2cdm",
        "etl_version": CODE_VERSION,
        "meds_version": getattr(meds_spec, "__version__", "0.4.1"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": "not specified: research use, contains identifiable source data",
        "extension_columns": [f.name for f in MEDS_SCHEMA][5:],
        "vocabulary_version": vocabulary.version,
        "notes": (
            "available_time is the field that prevents time leakage: an as-of view must "
            "use only rows with available_time <= prediction_time. Cohort membership is "
            "deliberately absent from event rows and lives in the audit layer."
        ),
    }


def _count_codes(rows: list[dict[str, Any]]) -> int:
    return len({r["code"] for r in rows})


def _as_float32(value: float | None) -> float | None:
    return None if value is None else float(value)


def _clear(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _vocab_dir() -> Path | None:
    import os

    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None


def as_of_view(layout: WorkLayout, prediction_time: datetime, subject_id: int | None = None) -> pl.DataFrame:
    """The rows a model may see at ``prediction_time``.

    Filtered on ``available_time``, not on ``time``: a result collected before the
    prediction point but released after it did not exist yet, and training on it is
    the quiet kind of leakage that makes a model look excellent and be useless.
    """
    data_dir = layout.meds_dir / meds_spec.data_subdirectory
    files = sorted(str(p) for p in data_dir.rglob("*.parquet"))
    if not files:
        return pl.DataFrame()
    frame = pl.read_parquet(files)
    if subject_id is not None:
        frame = frame.filter(pl.col("subject_id") == subject_id)
    return frame.filter(
        pl.col("available_time").is_null() | (pl.col("available_time") <= prediction_time)
    )
