"""The canonical schema is a contract (checklist P0-4)."""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa
import pytest

from ehr2trace.schema import (
    ANCHOR_SCHEMA,
    CANONICAL_EVENT_SCHEMA,
    COHORT_MEMBERSHIP_SCHEMA,
    EVENT_SOURCE_SCHEMA,
    CanonicalEvent,
    EventKind,
    ProvenanceStatus,
    events_to_table,
    table_to_events,
)


def sample_event(**over) -> CanonicalEvent:
    base = dict(
        event_id="e1",
        subject_id=42,
        encounter_id="c1",
        event_kind=EventKind.measurement,
        event_time=datetime(2018, 3, 3, 18, 49),
        available_time=datetime(2018, 3, 3, 19, 0),
        code_system="SOURCE",
        source_code="PHART",
        source_name="PH, ARTERIAL",
        value_number=7.31,
        quality_flags=["TIME_FALLBACK"],
    )
    base.update(over)
    return CanonicalEvent(**base)


def test_arrow_round_trip_preserves_every_field():
    events = [sample_event(), sample_event(event_id="e2", value_number=None, value_text="see below")]
    table = events_to_table(events)
    assert table.schema == CANONICAL_EVENT_SCHEMA
    restored = table_to_events(table)
    assert [e.model_dump() for e in restored] == [e.model_dump() for e in events]


def test_static_event_may_have_no_time():
    """Demographics legitimately have no time; clinical events without one do not."""
    event = sample_event(event_kind=EventKind.demographic, event_time=None, available_time=None)
    restored = table_to_events(events_to_table([event]))[0]
    assert restored.event_time is None


def test_unknown_field_is_rejected():
    with pytest.raises(Exception):
        CanonicalEvent(event_id="e", subject_id=1, event_kind=EventKind.note, invented_column=1)


def test_event_kinds_cover_the_documented_set():
    assert {k.value for k in EventKind} == {
        "demographic",
        "visit",
        "condition",
        # A medication has three distinct actions behind it and they are not the same
        # evidence: ordering it, handing it over, and giving it to the patient.
        "drug_order",
        "drug_dispense",
        "drug_admin",
        # Everything else that can be requested shares one lifecycle, so it shares one
        # kind; the domain lives in the code.
        "service_order",
        "procedure",
        "measurement",
        "note",
        "death",
    }


def test_provenance_defaults_to_observed():
    assert sample_event().provenance_status is ProvenanceStatus.observed


def test_relation_tables_are_keyed_for_lineage():
    assert set(EVENT_SOURCE_SCHEMA.names) >= {"event_id", "source_row_id", "relation"}
    assert set(ANCHOR_SCHEMA.names) >= {"anchor_id", "subject_id", "anchor_date", "partition_id"}
    assert set(COHORT_MEMBERSHIP_SCHEMA.names) >= {"subject_id", "partition_id", "membership_label"}


def test_membership_and_anchors_are_not_event_columns():
    """A directory label must have nowhere to hide inside an event row."""
    names = set(CANONICAL_EVENT_SCHEMA.names)
    assert not names & {"membership_label", "partition_id", "batch", "anchor_id", "anchor_date"}


def test_note_text_column_is_large_enough_for_reports():
    assert CANONICAL_EVENT_SCHEMA.field("value_text").type == pa.large_string()


def test_times_are_microsecond_timestamps_for_meds():
    for name in ("event_time", "available_time", "end_time"):
        assert CANONICAL_EVENT_SCHEMA.field(name).type == pa.timestamp("us")
