"""Row shapes: what becomes an event, and what deliberately does not.

Every test here corresponds to a rule the design states in prose. The failure paths
matter more than the happy ones: a clinical event with no time, an untimed measurement
and a duplicated report all have a tempting wrong answer.
"""

from __future__ import annotations

from datetime import datetime

from ehr2cdm.canonical.normalize import (
    COL_PREFIX,
    Row,
    ShapeContext,
    build_role_map,
    get_shape,
)
from ehr2cdm.canonical.values import ValueParsingSpec
from ehr2cdm.config import DatasetConfig
from ehr2cdm.schema import EventKind, QualityFlag, QuarantineReason, SourceRelation
from ehr2cdm.timeutil import TimeContext

BASE_CONFIG = {
    "dataset_id": "t",
    "identity": {"person_key": "PID"},
    "partitions": [{"id": "p1", "dir": "p1", "membership_label": "has", "batch": "b1"}],
    "sources": {},
    "time": {"timezone_assumption": "UTC"},
    "anchors": {"anchor_type": "study"},
}


def make_ctx(source_id: str, spec_dict: dict) -> ShapeContext:
    cfg = DatasetConfig.model_validate({**BASE_CONFIG, "sources": {source_id: spec_dict}})
    return ShapeContext(
        cfg=cfg,
        source_id=source_id,
        spec=cfg.sources[source_id],
        time=TimeContext(("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"), ("NULL",), "UTC"),
        values=ValueParsingSpec(),
        mapping_version="0",
    )


def make_rows(ctx: ShapeContext, records: list[dict], subject_id: int = 7) -> list[Row]:
    columns = {f"{COL_PREFIX}{k}" for r in records for k in r}
    roles = build_role_map(ctx.spec, columns)
    rows = []
    for i, record in enumerate(records, start=1):
        data = {f"{COL_PREFIX}{k}": v for k, v in record.items()}
        data.update(
            {
                "subject_id": subject_id,
                "source_row_id": f"row{i}",
                "partition_id": "p1",
                "batch": "b1",
                "membership_label": "has",
                "encounter_source_id": record.get("CSN"),
                "person_source_id": "PID1",
                "source_file": "f.txt",
                "source_sheet": None,
                "source_row_number": i,
            }
        )
        rows.append(Row(data, roles))
    return rows


LAB_SPEC = {
    "adapter": "delimited",
    "shape": "point_event",
    "event_kind": "measurement",
    "fields": {
        "person_id": {"from": ["PID"]},
        "encounter_id": {"from": ["CSN"]},
        "event_time": {"from": ["collected", "resulted"]},
        "available_time": {"from": ["resulted"]},
        "anchor_time": {"from": ["anchor"]},
        "source_code": {"from": ["code"]},
        "value": {"from": ["val"]},
        "unit": {"from": ["unit"]},
    },
}


def test_point_event_uses_collection_time_not_the_anchor():
    """The anchor is the date a cohort was pulled around. It is not when this happened."""
    ctx = make_ctx("labs", LAB_SPEC)
    rows = make_rows(
        ctx,
        [{"PID": "P1", "collected": "2018-02-22 13:33:00", "resulted": "2018-02-22 13:55:00",
          "anchor": "2018-03-03 13:49:00", "code": "PHART", "val": "7.31"}],
    )
    out = get_shape("point_event")(ctx, rows)
    assert len(out.events) == 1
    event = out.events[0]
    assert event.event_time == datetime(2018, 2, 22, 13, 33)
    assert event.available_time == datetime(2018, 2, 22, 13, 55)
    assert event.event_time != datetime(2018, 3, 3, 13, 49)


def test_time_fallback_is_flagged_when_the_preferred_column_is_empty():
    ctx = make_ctx("labs", LAB_SPEC)
    rows = make_rows(
        ctx,
        [{"PID": "P1", "collected": "NULL", "resulted": "2018-02-22 13:55:00", "code": "PHART", "val": "7.31"}],
    )
    event = get_shape("point_event")(ctx, rows).events[0]
    assert event.event_time == datetime(2018, 2, 22, 13, 55)
    assert str(QualityFlag.TIME_FALLBACK) in event.quality_flags


def test_clinical_event_without_a_time_is_quarantined_not_stamped_with_now():
    ctx = make_ctx("labs", LAB_SPEC)
    rows = make_rows(ctx, [{"PID": "P1", "collected": "NULL", "resulted": "NULL", "code": "PHART", "val": "7.31"}])
    out = get_shape("point_event")(ctx, rows)
    assert out.events == []
    assert out.quarantine[0]["reason"] == str(QuarantineReason.MISSING_EVENT_TIME)


def test_the_same_fact_in_two_partitions_gets_one_event_id():
    """Cross-batch duplicates must collapse; the partition may not enter the key."""
    ctx = make_ctx("labs", LAB_SPEC)
    record = {"PID": "P1", "collected": "2018-02-22 13:33:00", "resulted": "2018-02-22 13:55:00",
              "code": "PHART", "val": "7.31"}
    first = get_shape("point_event")(ctx, make_rows(ctx, [record])).events[0]
    rows_b = make_rows(ctx, [{**record, "anchor": "2019-01-01 00:00:00"}])
    rows_b[0].data["partition_id"] = "p2"
    rows_b[0].data["batch"] = "b2"
    second = get_shape("point_event")(ctx, rows_b).events[0]
    assert first.event_id == second.event_id


def test_a_different_value_is_a_different_event():
    ctx = make_ctx("labs", LAB_SPEC)
    base = {"PID": "P1", "collected": "2018-02-22 13:33:00", "code": "PHART"}
    a = get_shape("point_event")(ctx, make_rows(ctx, [{**base, "val": "7.31"}])).events[0]
    b = get_shape("point_event")(ctx, make_rows(ctx, [{**base, "val": "7.44"}])).events[0]
    assert a.event_id != b.event_id


NARRATIVE_SPEC = {
    "adapter": "delimited",
    "shape": "narrative_lines",
    "event_kind": "note",
    "study_event_kind": "procedure",
    "fields": {
        "person_id": {"from": ["PID"]},
        "encounter_id": {"from": ["CSN"]},
        "display_name": {"from": ["study"]},
        "event_time": {"from": ["resulted"]},
        "available_time": {"from": ["resulted"]},
        "anchor_time": {"from": ["anchor"]},
        "text_line": {"from": ["line"]},
        "text": {"from": ["narrative"]},
    },
}


def narrative_rows(anchor: str, lines: list[str]) -> list[dict]:
    return [
        {"PID": "P1", "CSN": "C1", "study": "ECHO", "resulted": "2018-03-05 10:00:00",
         "anchor": anchor, "line": str(i), "narrative": text}
        for i, text in enumerate(lines, start=1)
    ]


def test_a_report_repeated_once_per_anchor_collapses_to_one_note():
    """The whole table is duplicated per anchor; the note must not be."""
    ctx = make_ctx("echo", NARRATIVE_SPEC)
    lines = ["Echo report", "Indications: dyspnea", "Normal study."]
    records = narrative_rows("2018-03-03", lines) + narrative_rows("2018-06-01", lines)
    out = get_shape("narrative_lines")(ctx, make_rows(ctx, records))
    notes = [e for e in out.events if e.event_kind == EventKind.note]
    assert len(notes) == 1
    assert notes[0].value_text == "\n".join(lines)


def test_every_duplicated_row_still_links_to_the_single_note():
    ctx = make_ctx("echo", NARRATIVE_SPEC)
    lines = ["a", "b"]
    records = narrative_rows("2018-03-03", lines) + narrative_rows("2018-06-01", lines)
    out = get_shape("narrative_lines")(ctx, make_rows(ctx, records))
    note = next(e for e in out.events if e.event_kind == EventKind.note)
    linked = {l["source_row_id"] for l in out.links if l["event_id"] == note.event_id}
    assert len(linked) == 4, "all four source rows must remain traceable"
    assert {l["relation"] for l in out.links if l["event_id"] == note.event_id} == {
        str(SourceRelation.derived_from),
        str(SourceRelation.duplicate_of),
    }


def test_a_report_also_produces_a_study_event():
    ctx = make_ctx("echo", NARRATIVE_SPEC)
    out = get_shape("narrative_lines")(ctx, make_rows(ctx, narrative_rows("2018-03-03", ["a"])))
    assert {e.event_kind for e in out.events} == {EventKind.note, EventKind.procedure}


def test_an_empty_report_produces_no_note_and_no_negative_fact():
    ctx = make_ctx("echo", NARRATIVE_SPEC)
    records = [{"PID": "P1", "CSN": "C1", "study": "ECHO", "resulted": "2018-03-05 10:00:00",
                "line": "NULL", "narrative": "NULL"}]
    out = get_shape("narrative_lines")(ctx, make_rows(ctx, records))
    assert [e for e in out.events if e.event_kind == EventKind.note] == []
    assert out.issues and out.issues[0]["issue_type"] == "EMPTY_NARRATIVE"


COMPONENT_SPEC = {
    "adapter": "delimited",
    "shape": "component_measurements",
    "event_kind": "measurement",
    "study_event_kind": "procedure",
    "fields": {
        "person_id": {"from": ["PID"]},
        "encounter_id": {"from": ["CSN"]},
        "display_name": {"from": ["study"]},
        "event_time": {"from": ["resulted"]},
        "available_time": {"from": ["resulted"]},
        "anchor_time": {"from": ["anchor"]},
        "source_code": {"from": ["component"]},
        "text_line": {"from": ["line"]},
        "value": {"from": ["val"]},
    },
}


def test_components_become_measurements_hanging_off_one_study():
    ctx = make_ctx("ekg", COMPONENT_SPEC)
    records = [
        {"PID": "P1", "CSN": "C1", "study": "ECG", "resulted": "2018-03-05 10:00:00",
         "component": "VENTRICULAR RATE", "line": "1", "val": "102"},
        {"PID": "P1", "CSN": "C1", "study": "ECG", "resulted": "2018-03-05 10:00:00",
         "component": "DIAGNOSIS", "line": "1", "val": "SINUS TACHYCARDIA"},
    ]
    out = get_shape("component_measurements")(ctx, make_rows(ctx, records))
    procedures = [e for e in out.events if e.event_kind == EventKind.procedure]
    measurements = [e for e in out.events if e.event_kind == EventKind.measurement]
    assert len(procedures) == 1 and len(measurements) == 2
    assert all(m.parent_event_id == procedures[0].event_id for m in measurements)
    assert {m.value_number for m in measurements} == {102.0, None}
    assert {m.value_text for m in measurements} == {None, "SINUS TACHYCARDIA"}


def test_the_same_component_on_two_lines_stays_two_measurements():
    ctx = make_ctx("ekg", COMPONENT_SPEC)
    records = [
        {"PID": "P1", "CSN": "C1", "study": "ECG", "resulted": "2018-03-05 10:00:00",
         "component": "DIAGNOSIS", "line": str(i), "val": text}
        for i, text in enumerate(["SINUS TACHYCARDIA", "RIGHT AXIS DEVIATION"], start=1)
    ]
    out = get_shape("component_measurements")(ctx, make_rows(ctx, records))
    measurements = [e for e in out.events if e.event_kind == EventKind.measurement]
    assert len(measurements) == 2


PERSON_SPEC = {
    "adapter": "excel",
    "shape": "person_attributes",
    "event_kind": "demographic",
    "fields": {
        "person_id": {"from": ["PID"]},
        "age": {"from": ["Age"]},
        "gender": {"from": ["Gender"]},
        "death_time": {"from": ["Died"]},
    },
    "untimed_values": [{"column": "BMI", "code": "BMI"}],
}


def test_static_attributes_are_timeless_events():
    ctx = make_ctx("demographics", PERSON_SPEC)
    out = get_shape("person_attributes")(ctx, make_rows(ctx, [{"PID": "P1", "Age": "64", "Gender": "Female"}]))
    assert {e.source_code for e in out.events} == {"AGE", "GENDER"}
    assert all(e.event_time is None for e in out.events)


def test_an_untimed_measurement_is_quarantined_not_dated():
    """A body-mass index with no measurement time gets no date, not a plausible one."""
    ctx = make_ctx("demographics", PERSON_SPEC)
    out = get_shape("person_attributes")(ctx, make_rows(ctx, [{"PID": "P1", "Age": "64", "BMI": "27.4"}]))
    assert not any(e.source_code == "BMI" for e in out.events)
    assert out.quarantine[0]["reason"] == str(QuarantineReason.UNTIMED_CLINICAL_VALUE)


def test_a_death_date_becomes_a_death_event_with_a_source_independent_id():
    """Two sources carrying the same death date must produce one event, not two."""
    ctx = make_ctx("demographics", PERSON_SPEC)
    out = get_shape("person_attributes")(ctx, make_rows(ctx, [{"PID": "P1", "Died": "2021-06-02 08:15:00"}]))
    death = next(e for e in out.events if e.event_kind == EventKind.death)

    visit_ctx = make_ctx(
        "outcome",
        {
            "adapter": "excel",
            "shape": "visit",
            "event_kind": "visit",
            "fields": {
                "person_id": {"from": ["PID"]},
                "encounter_id": {"from": ["CSN"]},
                "event_time": {"from": ["admitted"]},
                "death_time": {"from": ["Died"]},
            },
        },
    )
    visit_out = get_shape("visit")(
        visit_ctx,
        make_rows(visit_ctx, [{"PID": "P1", "CSN": "C1", "admitted": "2021-05-28 22:05:00",
                               "Died": "2021-06-02 08:15:00"}]),
    )
    other_death = next(e for e in visit_out.events if e.event_kind == EventKind.death)
    assert death.event_id == other_death.event_id


def test_a_derived_visit_end_is_marked_derived():
    ctx = make_ctx(
        "outcome",
        {
            "adapter": "excel",
            "shape": "visit",
            "event_kind": "visit",
            "fields": {
                "person_id": {"from": ["PID"]},
                "event_time": {"from": ["admitted"]},
                "length_of_stay": {"from": ["los"]},
            },
        },
    )
    out = get_shape("visit")(ctx, make_rows(ctx, [{"PID": "P1", "admitted": "2021-01-04 09:30:00", "los": "3"}]))
    visit = out.events[0]
    assert visit.end_time == datetime(2021, 1, 7, 9, 30)
    assert str(QualityFlag.DERIVED_END_TIME) in visit.quality_flags
    assert visit.provenance_status == "derived"
