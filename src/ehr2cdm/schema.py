"""Frozen data contracts (design section 5.2).

Everything downstream talks to these definitions and nothing else. Changing a schema
means changing this file, bumping ``CANONICAL_SCHEMA_VERSION`` and updating the fixture
expectations in the same commit.

Timestamps are stored as ``timestamp[us]`` holding UTC instants without a tz suffix:
MEDS wants exactly that, and the assumption that produced them is recorded once per run
(``timezone_assumption``) rather than repeated on every row.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Iterable, Sequence

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    demographic = "demographic"
    visit = "visit"
    condition = "condition"
    drug_order = "drug_order"
    drug_admin = "drug_admin"
    procedure = "procedure"
    measurement = "measurement"
    note = "note"
    death = "death"


class ProvenanceStatus(StrEnum):
    observed = "observed"
    derived = "derived"
    llm_proposed = "llm_proposed"
    human_approved = "human_approved"


class ParseStatus(StrEnum):
    ok = "ok"
    quarantined = "quarantined"


class SourceRelation(StrEnum):
    derived_from = "derived_from"
    duplicate_of = "duplicate_of"
    anchored_to = "anchored_to"


class Coverage(StrEnum):
    present = "present"
    empty = "empty"
    #: the source was not part of this extract. NOT the same as "the patient had none".
    not_extracted = "not_extracted"


class QualityFlag(StrEnum):
    RECORDED_AFTER_DEATH = "RECORDED_AFTER_DEATH"
    AVAILABILITY_ASSUMED = "AVAILABILITY_ASSUMED"
    AVAILABILITY_BEFORE_EVENT = "AVAILABILITY_BEFORE_EVENT"
    COMPARATOR_VALUE = "COMPARATOR_VALUE"
    NON_NUMERIC_RESULT = "NON_NUMERIC_RESULT"
    RANGE_VALUE = "RANGE_VALUE"
    SIGNATURE_LINE = "SIGNATURE_LINE"
    TIME_FALLBACK = "TIME_FALLBACK"
    TZ_ASSUMED = "TZ_ASSUMED"
    ANCHOR_TIME_UNKNOWN = "ANCHOR_TIME_UNKNOWN"
    DERIVED_APPROXIMATE_BIRTH_YEAR = "DERIVED_APPROXIMATE_BIRTH_YEAR"
    DERIVED_END_TIME = "DERIVED_END_TIME"
    END_BEFORE_START = "END_BEFORE_START"
    UNTIMED_VALUE = "UNTIMED_VALUE"
    UNIT_UNPARSED = "UNIT_UNPARSED"
    DUPLICATE_ACROSS_PARTITIONS = "DUPLICATE_ACROSS_PARTITIONS"


class QuarantineReason(StrEnum):
    FIELD_COUNT_MISMATCH = "FIELD_COUNT_MISMATCH"
    UNPARSEABLE_TIME = "UNPARSEABLE_TIME"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    MISSING_PERSON_KEY = "MISSING_PERSON_KEY"
    UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"
    UNTIMED_CLINICAL_VALUE = "UNTIMED_CLINICAL_VALUE"
    DECODE_ERROR = "DECODE_ERROR"


# --------------------------------------------------------------------------------
# Source layer (design section 5.1)
# --------------------------------------------------------------------------------

SOURCE_BASE_FIELDS: list[pa.Field] = [
    pa.field("source_row_id", pa.string()),
    pa.field("dataset_id", pa.string()),
    pa.field("partition_id", pa.string()),
    pa.field("batch", pa.string()),
    pa.field("membership_label", pa.string()),
    pa.field("source_id", pa.string()),
    pa.field("source_file", pa.string()),
    pa.field("source_sheet", pa.string()),
    pa.field("source_row_number", pa.int64()),
    pa.field("source_file_sha256", pa.string()),
    pa.field("source_row_sha256", pa.string()),
    pa.field("person_source_id", pa.string()),
    pa.field("encounter_source_id", pa.string()),
    pa.field("parse_status", pa.string()),
    pa.field("parse_issues", pa.list_(pa.string())),
]

#: Roles that are parsed into typed columns. The original text is always kept too,
#: under the source's own column name; typed columns carry the ``_parsed`` suffix.
PARSED_SUFFIX = "_parsed"


def source_schema(original_columns: Sequence[str], parsed: Sequence[tuple[str, pa.DataType]]) -> pa.Schema:
    """Base lineage columns + original columns (as strings) + typed ``*_parsed`` columns."""
    fields = list(SOURCE_BASE_FIELDS)
    seen = {f.name for f in fields}
    for col in original_columns:
        name = f"col__{col}"
        if name not in seen:
            fields.append(pa.field(name, pa.string()))
            seen.add(name)
    for name, dtype in parsed:
        pname = f"{name}{PARSED_SUFFIX}"
        if pname not in seen:
            fields.append(pa.field(pname, dtype))
            seen.add(pname)
    return pa.schema(fields)


QUARANTINE_SCHEMA = pa.schema(
    [
        pa.field("source_row_id", pa.string()),
        pa.field("dataset_id", pa.string()),
        pa.field("partition_id", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("source_file", pa.string()),
        pa.field("source_sheet", pa.string()),
        pa.field("source_row_number", pa.int64()),
        pa.field("stage", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("detail", pa.string()),
        pa.field("person_source_id", pa.string()),
        pa.field("raw_row", pa.string()),
    ]
)


# --------------------------------------------------------------------------------
# Canonical layer (design section 5.2)
# --------------------------------------------------------------------------------

CANONICAL_EVENT_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("subject_id", pa.int64()),
        pa.field("encounter_id", pa.string()),
        pa.field("event_kind", pa.string()),
        pa.field("event_time", pa.timestamp("us")),
        pa.field("available_time", pa.timestamp("us")),
        pa.field("end_time", pa.timestamp("us")),
        pa.field("code_system", pa.string()),
        pa.field("source_code", pa.string()),
        pa.field("source_name", pa.string()),
        pa.field("standard_concept_id", pa.int64()),
        pa.field("value_number", pa.float64()),
        pa.field("value_text", pa.large_string()),
        pa.field("value_low", pa.float64()),
        pa.field("value_high", pa.float64()),
        pa.field("unit_source", pa.string()),
        pa.field("unit_concept_id", pa.int64()),
        pa.field("status_source", pa.string()),
        pa.field("route_source", pa.string()),
        pa.field("dose_source", pa.string()),
        pa.field("provenance_status", pa.string()),
        pa.field("mapping_version", pa.string()),
        pa.field("quality_flags", pa.list_(pa.string())),
        # bookkeeping, not part of the identity of an event
        pa.field("source_id", pa.string()),
        pa.field("parent_event_id", pa.string()),
    ]
)

EVENT_SOURCE_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("source_row_id", pa.string()),
        pa.field("relation", pa.string()),
        pa.field("partition_id", pa.string()),
    ]
)

ANCHOR_SCHEMA = pa.schema(
    [
        pa.field("anchor_id", pa.string()),
        pa.field("subject_id", pa.int64()),
        pa.field("anchor_type", pa.string()),
        pa.field("anchor_date", pa.date32()),
        pa.field("anchor_time", pa.timestamp("us")),
        pa.field("anchor_time_known", pa.bool_()),
        pa.field("partition_id", pa.string()),
        pa.field("source_row_id", pa.string()),
    ]
)

COHORT_MEMBERSHIP_SCHEMA = pa.schema(
    [
        pa.field("subject_id", pa.int64()),
        pa.field("partition_id", pa.string()),
        pa.field("batch", pa.string()),
        pa.field("membership_label", pa.string()),
        pa.field("anchor_id", pa.string()),
        pa.field("label_scope", pa.string()),
        pa.field("source_row_id", pa.string()),
    ]
)

QUALITY_ISSUE_SCHEMA = pa.schema(
    [
        pa.field("issue_type", pa.string()),
        pa.field("severity", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("subject_id", pa.int64()),
        pa.field("source_row_id", pa.string()),
        pa.field("event_id", pa.string()),
        pa.field("partition_id", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("detail", pa.string()),
    ]
)

SUBJECT_MAP_SCHEMA = pa.schema(
    [
        pa.field("subject_id", pa.int64()),
        pa.field("person_source_id", pa.string()),
        pa.field("bucket", pa.int32()),
        pa.field("partitions", pa.list_(pa.string())),
    ]
)

COVERAGE_SCHEMA = pa.schema(
    [
        pa.field("partition_id", pa.string()),
        pa.field("source_id", pa.string()),
        pa.field("coverage", pa.string()),
        pa.field("rows_read", pa.int64()),
        pa.field("rows_parsed", pa.int64()),
        pa.field("rows_quarantined", pa.int64()),
        pa.field("detail", pa.string()),
    ]
)


class CanonicalEvent(BaseModel):
    """One clinical fact. The primary key ``event_id`` never depends on an anchor."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    subject_id: int
    encounter_id: str | None = None
    event_kind: EventKind
    event_time: datetime | None = None
    available_time: datetime | None = None
    end_time: datetime | None = None
    code_system: str = "SOURCE"
    source_code: str | None = None
    source_name: str | None = None
    standard_concept_id: int | None = None
    value_number: float | None = None
    value_text: str | None = None
    value_low: float | None = None
    value_high: float | None = None
    unit_source: str | None = None
    unit_concept_id: int | None = None
    status_source: str | None = None
    route_source: str | None = None
    dose_source: str | None = None
    provenance_status: ProvenanceStatus = ProvenanceStatus.observed
    mapping_version: str = "0"
    quality_flags: list[str] = Field(default_factory=list)
    source_id: str | None = None
    parent_event_id: str | None = None


def events_to_table(events: Iterable[CanonicalEvent]) -> pa.Table:
    """Canonical events -> Arrow, column order fixed by the schema."""
    rows = [e.model_dump(mode="python") for e in events]
    cols: dict[str, list] = {f.name: [] for f in CANONICAL_EVENT_SCHEMA}
    for row in rows:
        for name in cols:
            value = row.get(name)
            if isinstance(value, StrEnum):
                value = str(value)
            cols[name].append(value)
    return pa.table(cols, schema=CANONICAL_EVENT_SCHEMA)


def table_to_events(table: pa.Table) -> list[CanonicalEvent]:
    """Arrow -> canonical events; the inverse of :func:`events_to_table`."""
    return [CanonicalEvent.model_validate(row) for row in table.to_pylist()]


def empty_table(schema: pa.Schema) -> pa.Table:
    return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)
