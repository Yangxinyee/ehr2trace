"""Anchors, cohort membership and deterministic merging.

The two places the checklist says are most likely to be wrong without raising an
error: anchor handling and the merge that collapses duplicates.
"""

from __future__ import annotations

from datetime import date, datetime

from ehr2cdm.canonical.anchors import anchor_identity, emit_anchors
from ehr2cdm.canonical.build import resolve_causes
from ehr2cdm.canonical.dedup import (
    apply_duplicate_flags,
    dedup_records,
    merge_events,
    merge_links,
    sort_events,
)
from ehr2cdm.schema import QualityFlag, SourceRelation
from tests.unit.test_normalize import COMPONENT_SPEC, make_ctx, make_rows


def anchor_rows(records):
    ctx = make_ctx("ekg", COMPONENT_SPEC)
    return ctx, make_rows(ctx, records)


def record(anchor: str, partition: str = "p1", line: str = "1"):
    return {
        "PID": "P1",
        "CSN": "C1",
        "study": "ECG",
        "resulted": "2018-03-05 10:00:00",
        "anchor": anchor,
        "component": "RATE",
        "line": line,
        "val": "80",
    }


def test_a_full_timestamp_anchor_records_that_its_time_is_known():
    ctx, rows = anchor_rows([record("2018-03-03 13:49:00")])
    anchors, _memberships = emit_anchors(ctx, rows)
    assert len(anchors) == 1
    assert anchors[0]["anchor_date"] == date(2018, 3, 3)
    assert anchors[0]["anchor_time_known"] is True


def test_a_date_only_anchor_records_that_the_time_component_is_lost():
    """One batch ships a timestamp and the other a bare date. Both are kept honestly."""
    ctx, rows = anchor_rows([record("2018-03-03")])
    anchors, _ = emit_anchors(ctx, rows)
    assert anchors[0]["anchor_date"] == date(2018, 3, 3)
    assert anchors[0]["anchor_time_known"] is False
    assert anchors[0]["anchor_time"] is None


def test_anchors_from_the_two_batch_formats_meet_at_date_granularity():
    """They cannot be joined by equality, so the key is the date, not the instant."""
    with_time = anchor_identity("t", 7, "study", date(2018, 3, 3), "p1")
    date_only = anchor_identity("t", 7, "study", date(2018, 3, 3), "p1")
    assert with_time == date_only


def test_the_same_date_in_two_partitions_is_two_anchors():
    """Collapsing them would erase that a patient's episodes were split across cohorts."""
    ctx, rows = anchor_rows([record("2018-03-03"), record("2018-03-03")])
    rows[1].data["partition_id"] = "p2"
    anchors, _ = emit_anchors(ctx, rows)
    assert len({a["anchor_id"] for a in anchors}) == 2


def test_many_rows_under_one_anchor_produce_one_anchor_row():
    ctx, rows = anchor_rows([record("2018-03-03", line=str(i)) for i in range(1, 51)])
    anchors, _ = emit_anchors(ctx, rows)
    assert len(anchors) == 1


def test_cohort_membership_is_recorded_separately_per_partition():
    ctx, rows = anchor_rows([record("2018-03-03"), record("2018-04-04")])
    rows[1].data["partition_id"] = "p2"
    rows[1].data["membership_label"] = "no"
    _anchors, memberships = emit_anchors(ctx, rows)
    assert {m["membership_label"] for m in memberships} == {"has", "no"}
    assert {m["partition_id"] for m in memberships} == {"p1", "p2"}


def test_membership_scope_comes_from_configuration_and_defaults_to_unknown():
    """A label whose scope nobody defined must not be published as patient-level."""
    ctx, rows = anchor_rows([record("2018-03-03")])
    _anchors, memberships = emit_anchors(ctx, rows)
    assert memberships[0]["label_scope"] == "unknown"


def test_a_time_carrying_batch_upgrades_a_date_only_anchor_without_changing_the_key():
    ctx, rows = anchor_rows([record("2018-03-03"), record("2018-03-03 13:49:00")])
    anchors, _ = emit_anchors(ctx, rows)
    assert len(anchors) == 1
    assert anchors[0]["anchor_time_known"] is True
    assert anchors[0]["anchor_time"] == datetime(2018, 3, 3, 13, 49)


# -- merging --------------------------------------------------------------------


def event(event_id: str, **over):
    base = {
        "event_id": event_id,
        "subject_id": 1,
        "event_kind": "measurement",
        "event_time": datetime(2018, 3, 5, 10, 0),
        "quality_flags": [],
    }
    base.update(over)
    return base


def test_events_sharing_an_id_merge_and_union_their_flags():
    merged = merge_events(
        [
            event("a", quality_flags=["TIME_FALLBACK"]),
            event("a", quality_flags=["AVAILABILITY_ASSUMED"]),
            event("b"),
        ]
    )
    assert len(merged) == 2
    assert merged[0]["quality_flags"] == ["AVAILABILITY_ASSUMED", "TIME_FALLBACK"]


def test_merging_fills_a_gap_but_never_overwrites_an_observation():
    merged = merge_events([event("a", unit_source=None), event("a", unit_source="mmol/L")])
    assert merged[0]["unit_source"] == "mmol/L"
    merged = merge_events([event("a", unit_source="mmol/L"), event("a", unit_source="mg/dL")])
    assert merged[0]["unit_source"] == "mmol/L"


def test_link_relations_are_decided_by_sorting_not_by_arrival_order():
    """Which duplicate counts as the origin must not depend on which worker finished."""
    links = [
        {"event_id": "a", "source_row_id": "r2", "partition_id": "p2"},
        {"event_id": "a", "source_row_id": "r1", "partition_id": "p1"},
    ]
    forward, _ = merge_links(links)
    backward, _ = merge_links(list(reversed(links)))
    assert forward == backward
    assert forward[0]["source_row_id"] == "r1"
    assert forward[0]["relation"] == str(SourceRelation.derived_from)
    assert forward[1]["relation"] == str(SourceRelation.duplicate_of)


def test_duplicate_links_are_deduplicated_by_source_row():
    links = [{"event_id": "a", "source_row_id": "r1", "partition_id": "p1"}] * 3
    merged, _ = merge_links(links)
    assert len(merged) == 1


def test_an_event_seen_in_two_partitions_is_flagged_as_such():
    """The evidence that cross-batch deduplication actually happened."""
    links = [
        {"event_id": "a", "source_row_id": "r1", "partition_id": "p1"},
        {"event_id": "a", "source_row_id": "r2", "partition_id": "p2"},
    ]
    _merged, partitions = merge_links(links)
    flagged = apply_duplicate_flags([event("a")], partitions)
    assert str(QualityFlag.DUPLICATE_ACROSS_PARTITIONS) in flagged[0]["quality_flags"]


def test_single_partition_events_are_not_flagged():
    links = [{"event_id": "a", "source_row_id": "r1", "partition_id": "p1"}]
    _merged, partitions = merge_links(links)
    flagged = apply_duplicate_flags([event("a")], partitions)
    assert flagged[0]["quality_flags"] == []


def test_sorting_is_total_and_puts_timeless_events_first():
    events = [
        event("b", event_time=datetime(2020, 1, 2)),
        event("a", event_time=None),
        event("c", event_time=datetime(2020, 1, 1)),
    ]
    assert [e["event_id"] for e in sort_events(events)] == ["a", "c", "b"]
    assert sort_events(events) == sort_events(list(reversed(events)))


def test_generic_dedup_is_order_independent():
    """Which of several equal records survives must not depend on arrival order."""
    records = [
        {"subject_id": 1, "partition_id": "p1", "anchor_id": "x", "source_row_id": "r9"},
        {"subject_id": 1, "partition_id": "p1", "anchor_id": "x", "source_row_id": "r1"},
    ]
    keys = ["subject_id", "partition_id", "anchor_id"]
    forward = dedup_records(records, keys)
    backward = dedup_records(list(reversed(records)), keys)
    assert len(forward) == 1
    assert forward == backward
    assert forward[0]["source_row_id"] == "r1"


def test_an_administration_is_joined_to_the_order_it_carried_out():
    """The source names the order by the order's key, not by an event id.

    The order is read by a different source, so the id only exists once both have been
    read. That join is the whole reason this runs after the per-source pass.
    """
    events = [{"event_id": "order1", "caused_by_event_id": None},
              {"event_id": "admin1", "caused_by_event_id": None}]
    keys = [{"event_id": "order1", "key": "poe7", "role": "declares"},
            {"event_id": "admin1", "key": "poe7", "role": "caused_by"}]
    out, resolved = resolve_causes(events, keys)
    assert resolved == 1
    assert {e["event_id"]: e["caused_by_event_id"] for e in out} == {
        "order1": None, "admin1": "order1"}


def test_a_key_naming_nothing_stays_null_rather_than_being_guessed():
    """The order may sit outside the extract. An invented parent asserts evidence."""
    events = [{"event_id": "admin1", "caused_by_event_id": None}]
    keys = [{"event_id": "admin1", "key": "poe_not_extracted", "role": "caused_by"},
            {"event_id": "order9", "key": "poe_other", "role": "declares"}]
    out, resolved = resolve_causes(events, keys)
    assert resolved == 0 and out[0]["caused_by_event_id"] is None


def test_two_rows_claiming_one_key_resolve_the_same_way_every_run():
    events = [{"event_id": "admin1", "caused_by_event_id": None}]
    keys = [{"event_id": "admin1", "key": "k", "role": "caused_by"},
            {"event_id": "zzz", "key": "k", "role": "declares"},
            {"event_id": "aaa", "key": "k", "role": "declares"}]
    first = resolve_causes(events, keys)[0][0]["caused_by_event_id"]
    assert first == resolve_causes(events, list(reversed(keys)))[0][0]["caused_by_event_id"]
    assert first == "aaa"
