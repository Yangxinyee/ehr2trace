"""canonical -> MEDS (design section 7).

MEDS is the event-stream view: one row per event, each subject whole and time-sorted.
Everything interesting about this conversion is about what is *not* written.

* ``available_time`` is an extension column and is the field that makes an
  availability-aware model possible at all. Where the source says only when something
  happened, not when anyone could see it, the same time is used and the row is flagged
  ``AVAILABILITY_ASSUMED`` rather than left looking like ground truth.
* The cohort label, the partition id and the file names do **not** appear in event
  rows. The label is the thing a model would be asked to predict, and a directory name
  that encodes it is the most direct leak there is. It stays in the audit layer.
* One clinical fact gets one code: the mapped OMOP code when there is one, otherwise
  the source code. Emitting both would train a model on the same fact twice.
* Splits are assigned by a stable hash of the subject id, after identity resolution
  across every partition, so a patient appearing in two batches cannot land in two
  splits.

The join and the ordering happen in the database engine, and shards are then streamed
out one subject at a time. Tens of millions of events do not fit in memory, and the
whole point of the sharding contract is that a reader never needs them to.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

import meds as meds_spec
from ehr2trace.analytics import HEAVY_THREADS, analytic_connection
from ehr2trace.config import DatasetConfig
from ehr2trace.errors import BlockerError
from ehr2trace.hashing import split_of
from ehr2trace.paths import WorkLayout, write_table_atomic
from ehr2trace.schema import EventKind, QualityFlag
from ehr2trace.terminology import (
    load_build_mappings,
    MappingRegistry,
    mappings_directory,
    normalize_term,
    resolve_terms_batch,
    TermRequest,
    Vocabulary,
)
from ehr2trace.version import CODE_VERSION

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
        # An administration without a dose, a route or an execution status is not an
        # action a model can learn from, only the fact that something happened. All
        # three are in the canonical layer and were being dropped by this projection.
        pa.field("dose", pa.string()),
        pa.field("route", pa.string()),
        pa.field("status", pa.string()),
        #: the order this event carried out, when one is known
        pa.field("caused_by_event_id", pa.string()),
        pa.field("quality_flags", pa.list_(pa.string())),
        # -- extension columns version 2 (remediation plan T1.11, P-C13) -----------------
        # Appended after the version-1 columns so a reader that indexes by position sees
        # what it saw before. The value after an exact unit conversion and the unit in
        # its UCUM spelling; the original value and unit stay in numeric_value and unit.
        pa.field("value_number_normalized", pa.float64()),
        pa.field("unit_normalized", pa.string()),
        # An infusion's rate, kept apart from its dose: verbatim, parsed, and its unit.
        pa.field("rate_source", pa.string()),
        pa.field("rate", pa.float64()),
        pa.field("rate_unit", pa.string()),
        # What was done to the record (an order placed, changed, discontinued).
        pa.field("action", pa.string()),
        # Where a visit discharged to, as the source wrote it.
        pa.field("discharged_to", pa.string()),
    ]
)

#: Bumped whenever an extension column is added or changes meaning; recorded in
#: ``dataset.json`` next to the full list so a reader can tell which columns to expect.
#: 1: the columns up to ``quality_flags``. 2: the seven added on 2026-09-13.
EXTENSION_COLUMNS_VERSION = 2
EXTENSION_COLUMNS_ADDED: dict[str, tuple[str, ...]] = {
    "2": (
        "value_number_normalized", "unit_normalized", "rate_source", "rate", "rate_unit",
        "action", "discharged_to",
    ),
}
#: The required MEDS columns come first; everything after them is this project's.
REQUIRED_MEDS_COLUMNS = 5

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

SUBJECT_SPLIT_SCHEMA = pa.schema([pa.field("subject_id", pa.int64()), pa.field("split", pa.string())])

#: Columns that must never reach an event row, whatever a future edit does.
FORBIDDEN_EVENT_COLUMNS = frozenset(
    {"membership_label", "partition_id", "batch", "source_file", "person_source_id"}
)

SHARD_BATCH_ROWS = 200_000


def build_meds(cfg: DatasetConfig, layout: WorkLayout, vocabulary_dir: Path | None = None) -> dict[str, Any]:

    vocabulary = Vocabulary.open(vocabulary_dir or _vocab_dir())
    _refuse_to_unmap_what_omop_mapped(layout, vocabulary)
    mappings = load_build_mappings()

    # The work below collapses a link table of hundreds of millions of rows and orders
    # every event in the dataset. It needs somewhere to spill -- without it this was
    # killed at 178 GB resident on MIMIC-IV -- and a bounded thread count, because a
    # hash aggregate keeps per-thread state and 48 of them do not fit whatever the
    # budget says.
    scratch = layout.meds_dir / "_scratch"
    with analytic_connection(scratch, threads=HEAVY_THREADS) as con:
        con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{layout.canonical_path('events')}')")
        con.execute(f"CREATE VIEW lnk AS SELECT * FROM read_parquet('{layout.canonical_path('event_source')}')")
        # The same terminology settings as the OMOP stage, or the two layers resolve a
        # name differently and MEDS_CONCEPTS_ARE_OMOPS says so.
        distinct, resolved = _build_term_map(
            con, vocabulary, mappings, cfg.terminology.drug_name_noise,
            cfg.terminology.drug_name_truncated_at,
        )

        rows_path = layout.meds_dir / "_rows.parquet"
        rows_path.parent.mkdir(parents=True, exist_ok=True)
        _materialize_rows(con, rows_path, scratch)

        subjects = [int(r[0]) for r in con.execute(
            f"SELECT DISTINCT subject_id FROM read_parquet('{rows_path}') ORDER BY subject_id"
        ).fetchall()]
        splits = {
            sid: split_of(sid, [tuple(s) for s in cfg.meds.splits], salt=cfg.meds.split_salt)
            for sid in subjects
        }

        data_dir = layout.meds_dir / meds_spec.data_subdirectory
        _clear(data_dir)
        shards, events = _write_shards(rows_path, data_dir, splits, cfg.meds.shard_size)
        code_counts = _write_code_metadata(con, rows_path, layout)
        rows_path.unlink()

    write_table_atomic(
        pa.table(
            {"subject_id": subjects, "split": [splits[s] for s in subjects]},
            schema=SUBJECT_SPLIT_SCHEMA,
        ),
        layout.meds_dir / meds_spec.subject_splits_filepath,
    )
    metadata_path = layout.meds_dir / meds_spec.dataset_metadata_filepath
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(_dataset_metadata(cfg, vocabulary), indent=2, sort_keys=True), encoding="utf-8"
    )
    vocabulary.close()

    result = {
        "events": events,
        "subjects": len(subjects),
        "shards": shards,
        "splits": {name: sum(1 for s in splits.values() if s == name) for name, _w in cfg.meds.splits},
        "codes": code_counts["codes"],
        "mapped_codes": code_counts["mapped"],
        "distinct_terms": distinct,
        "resolved_terms": resolved,
        "mapping_entries": len(mappings.entries),
        "mappings_dir": str(mappings_directory()),
        "membership_label_included": cfg.meds.include_membership_label,
        "data_dir": str(layout.meds_dir / meds_spec.data_subdirectory),
    }
    (layout.meds_dir / "build_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    return result


# --------------------------------------------------------------------------------


def _refuse_to_unmap_what_omop_mapped(layout: WorkLayout, vocabulary) -> None:
    """A MEDS layer built without the vocabulary its OMOP layer had is not one with fewer
    concepts; it is one where every code is unmapped and nothing says so.

    The vocabulary reaches this stage through an environment variable, and a stage rerun
    by hand without it published 311 million MIMIC-IV events whose every code was
    SOURCE/ while the OMOP layer beside them carried 29,459 concepts. Thirty-nine checks
    passed on that pair. The OMOP layer records which vocabulary built it, so the
    mismatch is knowable before a row is written, and a question the operator has to
    answer is a blocker, not a default.
    """
    if getattr(vocabulary, "available", False):
        return
    db = layout.omop_dir / "omop.duckdb"
    if not db.exists():
        return
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        row = con.execute("SELECT vocabulary_version FROM cdm_source LIMIT 1").fetchone()
    except duckdb.Error:
        return
    finally:
        con.close()
    if row and row[0] and row[0] != "none":
        raise BlockerError(
            "MEDS_VOCABULARY_MISSING",
            f"the OMOP layer was built against vocabulary {row[0]!r} and none is available "
            "here; MEDS would publish every code unmapped beside an OMOP layer that maps "
            "them. Pass --vocabulary or set OMOP_VOCAB_DIR to the same directory",
            needed_from="operator",
        )


def _build_term_map(con, vocabulary, mappings: MappingRegistry,
                    drug_name_noise: Sequence[str] = (),
                    drug_name_truncated_at: int | None = None) -> tuple[int, int]:
    """Resolve each distinct source string once and keep its normalized form.

    The normalized form is what a source code looks like in the MEDS namespace, so it
    is computed by the same function the terminology lookup uses -- one definition of
    "the same string", not two that drift apart.
    """
    rows = con.execute(
        """
        SELECT code_system, source_code, min(source_name) AS source_name,
               min(event_kind) AS event_kind, count(*) AS occurrences
        FROM evt WHERE source_code IS NOT NULL
        GROUP BY code_system, source_code
        """
    ).fetchall()
    terms = [
        TermRequest(r[0] or "SOURCE", r[1], r[2], r[3] or "", int(r[4])) for r in rows
    ]
    resolved, _unresolved = resolve_terms_batch(
        terms, vocabulary, mappings, drug_name_noise, drug_name_truncated_at)
    # The concept's name travels with the map so codes.parquet can describe a mapped
    # code by what the vocabulary calls it rather than by one of the source names that
    # happened to map there.
    con.execute(
        "CREATE TABLE term_map (code_system VARCHAR, source_code VARCHAR, "
        "concept_id BIGINT, normalized VARCHAR, concept_name VARCHAR)"
    )
    con.executemany(
        "INSERT INTO term_map VALUES (?, ?, ?, ?, ?)",
        [
            (
                t.code_system,
                t.source_code,
                int(resolved[t.key].concept_id) if t.key in resolved else None,
                normalize_term(t.source_code),
                (resolved[t.key].concept_name or None) if t.key in resolved else None,
            )
            for t in terms
        ],
    )
    return len(terms), len(resolved)


def _code_sql(event: str = "e", term: str = "m") -> str:
    """The MEDS code of an event, as one SQL expression used wherever a code is derived.

    One clinical fact gets one code: the reserved death code, the mapped OMOP concept,
    or the source code under its source's namespace. Written once so the rows and the
    code metadata cannot disagree about what a code is.
    """
    return f"""CASE
                       WHEN {event}.event_kind = '{EventKind.death}' THEN '{meds_spec.death_code}'
                       WHEN {term}.concept_id IS NOT NULL THEN 'OMOP/' || CAST({term}.concept_id AS VARCHAR)
                       ELSE 'SOURCE/' || coalesce({event}.source_id, 'unknown') || '/'
                            || coalesce(nullif({term}.normalized, ''), 'unspecified')
                   END"""


#: Lineage links collapsed in one pass. A grouped ``list()`` cannot spill, and on
#: MIMIC-IV's 801 million links it exhausted a 195 GiB budget on its own. The links are
#: therefore collapsed in hash buckets of ``event_id``, each small enough to hold; every
#: event lands in exactly one bucket, and the final ordering makes the published rows
#: independent of how many buckets there were.
LINKS_PER_BUCKET = 50_000_000


def _materialize_rows(con, out_path: Path, scratch_dir: Path) -> None:
    """Join, code and order every event once, out of core.

    The ordering is the sharding contract: subjects in order, each subject's events in
    time order, ties broken deterministically so two runs produce identical bytes.

    Collapsing the lineage links runs as its own pass rather than as a CTE inside the
    join. A grouped ``list()`` is one of the operators that cannot spill -- the
    accumulating lists have to be held -- so on MIMIC-IV's 301 million links it hit the
    memory ceiling while also competing with the sort it fed. Given the budget to
    itself, and written to disk before the join reads it back, each half fits.
    """
    assumed = str(QualityFlag.AVAILABILITY_ASSUMED)
    import shutil

    links_dir = scratch_dir / "links"
    if links_dir.exists():
        shutil.rmtree(links_dir)  # buckets a failed run left behind would be read twice
    links_dir.mkdir(parents=True, exist_ok=True)
    total = int(con.execute("SELECT count(*) FROM lnk").fetchone()[0])
    buckets = max(1, -(-total // LINKS_PER_BUCKET))
    for bucket in range(buckets):
        where = f"WHERE hash(event_id) % {buckets} = {bucket}" if buckets > 1 else ""
        con.execute(
            f"""
            COPY (
                SELECT event_id, list_sort(list(source_row_id)) AS source_row_ids
                FROM lnk {where} GROUP BY event_id
            ) TO '{links_dir / f"part-{bucket:04d}.parquet"}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
    links_path = links_dir / "*.parquet"
    # A canonical file written under schema version 1 lacks the version-2 columns; it
    # is still a valid input and publishes nulls there.
    present = {r[0] for r in con.execute("DESCRIBE evt").fetchall()}

    def optional(name: str, sql_type: str) -> str:
        return f"e.{name}" if name in present else f"CAST(NULL AS {sql_type})"

    con.execute(
        f"""
        COPY (
            WITH links AS (
                SELECT event_id, source_row_ids FROM read_parquet('{links_path}')
            )
            SELECT e.subject_id,
                   e.event_time AS time,
                   {_code_sql()} AS code,
                   CAST(e.value_number AS FLOAT) AS numeric_value,
                   e.value_text AS text_value,
                   e.event_id,
                   e.encounter_id,
                   coalesce(e.available_time, e.event_time) AS available_time,
                   e.end_time,
                   e.source_id AS source_table,
                   coalesce(l.source_row_ids, []) AS source_row_ids,
                   m.concept_id AS omop_concept_id,
                   e.source_code,
                   e.unit_source AS unit,
                   e.event_kind,
                   e.dose_source AS dose,
                   e.route_source AS route,
                   e.status_source AS status,
                   e.caused_by_event_id,
                   CASE
                       WHEN e.available_time IS NULL AND e.event_time IS NOT NULL
                       THEN list_sort(list_distinct(list_append(e.quality_flags, '{assumed}')))
                       ELSE list_sort(e.quality_flags)
                   END AS quality_flags,
                   {optional('value_number_normalized', 'DOUBLE')} AS value_number_normalized,
                   {optional('unit_normalized', 'VARCHAR')} AS unit_normalized,
                   {optional('rate_source', 'VARCHAR')} AS rate_source,
                   {optional('rate', 'DOUBLE')} AS rate,
                   {optional('rate_unit', 'VARCHAR')} AS rate_unit,
                   {optional('action', 'VARCHAR')} AS action,
                   {optional('discharged_to', 'VARCHAR')} AS discharged_to
            FROM evt e
            LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
            LEFT JOIN links l ON l.event_id = e.event_id
            ORDER BY e.subject_id, e.event_time NULLS FIRST, code, e.event_id
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )


def _write_shards(rows_path: Path, data_dir: Path, splits: dict[int, str], shard_size: int) -> tuple[int, int]:
    """Stream ordered rows into shards, never splitting a subject across two files."""
    size = max(1, shard_size)
    counts = {"shards": 0, "events": 0}
    buffers: dict[str, list[pa.Table]] = {}
    subjects_in_shard: dict[str, int] = {}
    shard_index: dict[str, int] = {}

    def flush(split: str) -> None:
        tables = buffers.get(split)
        if not tables:
            return
        index = shard_index.get(split, 0)
        path = data_dir / split / f"{index:06d}.parquet"
        table = pa.concat_tables(tables).cast(MEDS_SCHEMA)
        # The installed MEDS version owns what a valid data table is; failing here
        # beats publishing something a downstream reader will reject.
        meds_spec.DataSchema.validate(table)
        write_table_atomic(table, path)
        counts["shards"] += 1
        counts["events"] += table.num_rows
        buffers[split] = []
        subjects_in_shard[split] = 0
        shard_index[split] = index + 1

    for subject_id, table in _iter_subject_tables(rows_path):
        split = splits[subject_id]
        buffers.setdefault(split, []).append(table)
        subjects_in_shard[split] = subjects_in_shard.get(split, 0) + 1
        if subjects_in_shard[split] >= size:
            flush(split)
    for split in list(buffers):
        flush(split)
    return counts["shards"], counts["events"]


def _iter_subject_tables(rows_path: Path) -> Iterator[tuple[int, pa.Table]]:
    """Yield one Arrow table per subject from an already subject-ordered file."""
    parquet = pq.ParquetFile(rows_path)
    pending: list[pa.Table] = []
    current: int | None = None
    for batch in parquet.iter_batches(batch_size=SHARD_BATCH_ROWS):
        table = pa.Table.from_batches([batch])
        subjects = table.column("subject_id").to_pylist()
        start = 0
        for i, subject_id in enumerate(subjects):
            if current is None:
                current = subject_id
            if subject_id != current:
                pending.append(table.slice(start, i - start))
                yield current, pa.concat_tables(pending)
                pending = []
                current = subject_id
                start = i
        pending.append(table.slice(start))
    if current is not None and pending:
        combined = pa.concat_tables(pending)
        if combined.num_rows:
            yield current, combined


def _write_code_metadata(con, rows_path: Path, layout: WorkLayout) -> dict[str, int]:
    """Every code that actually appears, with its mapping status. No more, no less.

    The description is what the vocabulary calls a mapped concept, and for a source
    code the name the source uses for it most often (remediation plan T1.10, P-C9).
    It used to be the alphabetically smallest name, which described a code carrying
    127,700 rows named one way and 15 named another by the fifteen. Ties among the
    most frequent break alphabetically, so the choice is total; a code with no name
    falls back to its source code, then to itself.
    """
    table = con.execute(
        f"""
        WITH counted AS (
            SELECT code,
                   min(omop_concept_id) AS omop_concept_id,
                   min(event_kind) AS event_kind,
                   count(*) AS n_events
            FROM read_parquet('{rows_path}')
            GROUP BY code
        ),
        named AS (
            SELECT {_code_sql()} AS code, e.source_name, count(*) AS occurrences
            FROM evt e
            LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
            WHERE e.source_name IS NOT NULL
            GROUP BY 1, 2
        ),
        representative AS (
            SELECT code, source_name FROM (
                SELECT code, source_name,
                       row_number() OVER (PARTITION BY code ORDER BY occurrences DESC, source_name) AS rank
                FROM named
            ) WHERE rank = 1
        ),
        concept_names AS (
            SELECT concept_id, min(concept_name) AS concept_name
            FROM term_map
            WHERE concept_id IS NOT NULL AND concept_name IS NOT NULL AND concept_name <> ''
            GROUP BY concept_id
        ),
        coded AS (
            SELECT {_code_sql()} AS code, min(e.source_code) AS source_code
            FROM evt e
            LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
            GROUP BY 1
        )
        SELECT c.code,
               coalesce(n.concept_name, r.source_name, s.source_code, c.code) AS description,
               CAST([] AS VARCHAR[]) AS parent_codes,
               CASE WHEN c.omop_concept_id IS NOT NULL THEN 'OMOP' ELSE 'SOURCE' END
                   AS source_vocabulary,
               c.omop_concept_id,
               CASE WHEN c.omop_concept_id IS NOT NULL THEN 'mapped' ELSE 'unmapped' END
                   AS mapping_status,
               c.event_kind,
               c.n_events
        FROM counted c
        LEFT JOIN concept_names n ON n.concept_id = c.omop_concept_id
        LEFT JOIN representative r ON r.code = c.code
        LEFT JOIN coded s ON s.code = c.code
        ORDER BY c.code
        """
    ).arrow().read_all().cast(CODE_METADATA_SCHEMA)
    meds_spec.CodeMetadataSchema.validate(table)
    write_table_atomic(table, layout.meds_dir / meds_spec.code_metadata_filepath)
    mapped = int(
        con.execute(
            f"SELECT count(DISTINCT code) FROM read_parquet('{rows_path}') "
            "WHERE omop_concept_id IS NOT NULL"
        ).fetchone()[0]
    )
    return {"codes": table.num_rows, "mapped": mapped}


def _dataset_metadata(cfg: DatasetConfig, vocabulary) -> dict[str, Any]:
    return {
        "dataset_name": cfg.dataset_id,
        "dataset_version": cfg.config_hash()[:12],
        "etl_name": "ehr2trace",
        "etl_version": CODE_VERSION,
        "meds_version": getattr(meds_spec, "__version__", "0.4.1"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": "not specified: research use, derived from identifiable source data",
        "extension_columns": [f.name for f in MEDS_SCHEMA][REQUIRED_MEDS_COLUMNS:],
        "extension_columns_version": EXTENSION_COLUMNS_VERSION,
        "extension_columns_added": {
            version: list(columns) for version, columns in EXTENSION_COLUMNS_ADDED.items()
        },
        "vocabulary_version": vocabulary.version,
        "notes": (
            "available_time is the field that prevents time leakage: an as-of view must "
            "use only rows with available_time <= prediction_time. Cohort membership is "
            "deliberately absent from event rows and lives in the audit layer."
        ),
    }


def _clear(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _vocab_dir() -> Path | None:
    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None


def as_of_view(layout: WorkLayout, prediction_time: datetime, subject_id: int | None = None):
    """The rows a model may see at ``prediction_time``.

    Filtered on ``available_time``, not on ``time``: a result collected before the
    prediction point but released after it did not exist yet, and training on it is the
    quiet kind of leakage that makes a model look excellent and be useless.
    """
    import polars as pl

    data_dir = layout.meds_dir / meds_spec.data_subdirectory
    files = sorted(str(p) for p in data_dir.rglob("*.parquet"))
    if not files:
        return pl.DataFrame()
    frame = pl.scan_parquet(files)
    if subject_id is not None:
        frame = frame.filter(pl.col("subject_id") == subject_id)
    return frame.filter(
        pl.col("available_time").is_null() | (pl.col("available_time") <= prediction_time)
    ).collect()
