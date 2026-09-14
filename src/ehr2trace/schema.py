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
    #: handed over by a pharmacy or a cabinet. Not evidence the patient received it.
    drug_dispense = "drug_dispense"
    drug_admin = "drug_admin"
    #: a request for something that is not a drug -- a test, an image, a consult. One
    #: kind rather than one per domain: they share a lifecycle (requested, performed,
    #: resulted) and the result already arrives as its own observational event. The
    #: domain lives in the code, the way FHIR keeps every non-medication request in a
    #: single ServiceRequest resource.
    service_order = "service_order"
    procedure = "procedure"
    measurement = "measurement"
    #: a fact that is not a condition, drug, procedure or measurement: a follow-up
    #: contact, a documented date, a social or administrative observation. OMOP has a
    #: table for exactly this and until now nothing could be declared as one.
    observation = "observation"
    note = "note"
    death = "death"
    #: a stay inside a visit: a transfer between units, a change of service, an ICU
    #: stay. Published to OMOP's VISIT_DETAIL under the visit it belongs to, never as
    #: a visit of its own.
    visit_detail = "visit_detail"


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
    #: this record is not evidence the drug reached the patient. It covers a refusal, a
    #: held dose, and equally the nursing actions an administration record also logs --
    #: a line flush, a pain reassessment, an infusion reconciliation.
    NOT_ADMINISTERED = "NOT_ADMINISTERED"
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
    # -- merging (remediation plan T1.2, T1.3; decisions D-R2 to D-R6) ----------------
    #: rows that merged into this event disagreed on a field the rule set to null
    VALUE_CONFLICT = "VALUE_CONFLICT"
    #: rows that merged into this event disagreed on a field with no declared rule
    MERGE_CONFLICT = "MERGE_CONFLICT"
    #: the merged rows stated different availability times; the earliest was kept
    AVAILABILITY_MERGED = "AVAILABILITY_MERGED"
    #: a `priority` rule saw two values it was told to treat as contradictory
    STATUS_CONFLICT = "STATUS_CONFLICT"
    #: no encounter id among the merged rows was seen in another table; set to null
    ENCOUNTER_UNLINKED = "ENCOUNTER_UNLINKED"
    #: exactly one of the merged rows' encounter ids was seen elsewhere; it was kept
    ENCOUNTER_FROM_LINKED_ROW = "ENCOUNTER_FROM_LINKED_ROW"
    #: one service billed twice (facility and professional); one event, both rows linked
    BILLING_DUPLICATE = "BILLING_DUPLICATE"
    # -- units and values (T1.6, T1.7, T1.9; D-R1, D-R10, D-R17) ---------------------
    #: the source's unit was replaced for normalization by a declared one; the
    #: source's own string stays in unit_source
    UNIT_OVERRIDDEN = "UNIT_OVERRIDDEN"
    #: the source carried no unit and the dataset declared one for this code
    UNIT_DECLARED = "UNIT_DECLARED"
    #: the unit string is not in the unit table, so nothing was normalized
    UNIT_UNKNOWN = "UNIT_UNKNOWN"
    #: outside the declared plausible range; the value is kept and not normalized
    IMPLAUSIBLE = "IMPLAUSIBLE"
    #: this code carries its unit as a suffix because the source mixed incommensurable ones
    CODE_SPLIT_BY_UNIT = "CODE_SPLIT_BY_UNIT"
    #: the rate text did not parse as a number; rate_source keeps it verbatim
    RATE_UNPARSED = "RATE_UNPARSED"
    # -- deaths (T1.8; D-R11) ---------------------------------------------------------
    #: two sources put the death on the same local date; one event, the more precise time
    DEATH_TIME_MERGED = "DEATH_TIME_MERGED"
    #: two sources put the death on different local dates; both kept, neither published to OMOP
    DEATH_DATE_CONFLICT = "DEATH_DATE_CONFLICT"
    # -- preparation-time provenance (D-R18) ------------------------------------------
    #: the drug name was recovered from the order this record points at
    NAME_FROM_LINKED_ORDER = "NAME_FROM_LINKED_ORDER"
    #: the rows merged into this event named one fact differently -- a compounded
    #: order's MAIN and BASE rows describing one bag -- so one name was kept and every
    #: name stays in the lineage
    NAME_VARIANTS_MERGED = "NAME_VARIANTS_MERGED"


class QuarantineReason(StrEnum):
    FIELD_COUNT_MISMATCH = "FIELD_COUNT_MISMATCH"
    UNPARSEABLE_TIME = "UNPARSEABLE_TIME"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    MISSING_PERSON_KEY = "MISSING_PERSON_KEY"
    UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"
    UNTIMED_CLINICAL_VALUE = "UNTIMED_CLINICAL_VALUE"
    UNTIMED_VITAL_STATUS = "UNTIMED_VITAL_STATUS"
    DECODE_ERROR = "DECODE_ERROR"
    #: a value a `null_and_flag` merge rule refused to choose between; the row keeps
    #: its link to the event and its losing value is recorded here
    VALUE_CONFLICT = "VALUE_CONFLICT"


#: Person attributes that may legitimately carry no time.
#:
#: A baseline attribute is one whose value holds for the whole record, so a model that
#: reads it at any decision point learns nothing it could not have known at the first.
#: Vital status is deliberately absent. It is an outcome, and an outcome with no time
#: sits in every history the record can produce, including the ones that end before the
#: patient died. Anything not on this list must resolve to a time or be withheld.
TIMELESS_BASELINE_CODES: frozenset[str] = frozenset(
    {"GENDER", "RACE", "ETHNICITY", "AGE", "BIRTH_DATE"}
)


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
        # Normalization adds columns and never overwrites: value_number and unit_source
        # stay exactly as the source wrote them. These two hold the value after an exact
        # conversion (degF -> Cel) and the unit in its UCUM spelling, or null where no
        # exact conversion is known or the value is outside its plausible range.
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
        pa.field("provenance_status", pa.string()),
        pa.field("mapping_version", pa.string()),
        pa.field("quality_flags", pa.list_(pa.string())),
        # bookkeeping, not part of the identity of an event
        pa.field("source_id", pa.string()),
        pa.field("parent_event_id", pa.string()),
        pa.field("caused_by_event_id", pa.string()),
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
    value_number_normalized: float | None = None
    unit_normalized: str | None = None
    rate_source: str | None = None
    rate: float | None = None
    rate_unit: str | None = None
    action: str | None = None
    discharged_to: str | None = None
    provenance_status: ProvenanceStatus = ProvenanceStatus.observed
    mapping_version: str = "0"
    quality_flags: list[str] = Field(default_factory=list)
    source_id: str | None = None
    parent_event_id: str | None = None
    #: the action this one carries out: an administration points at its order. Resolved
    #: across sources during the build, because the causing row is read by a different
    #: source than the row that names it.
    caused_by_event_id: str | None = None


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
