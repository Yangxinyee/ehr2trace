"""Event identity, unit normalization and the new roles (remediation plan T1.1, T1.6-T1.9).

The audit of 2026-09-13 found the converter merging different orders because their dose,
route and status were not part of the identity (P-C1), labelling Fahrenheit as Celsius
with nowhere to say so (P-C4), and reading `2 SINUS TACHYCARDIA` as a number in units of
sinus tachycardia (P-C8). Each test here is one of those failure modes, with the tempting
wrong answer named.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from fractions import Fraction

import pytest

from ehr2trace.canonical.normalize import COL_PREFIX, filter_rows, get_shape
from ehr2trace.canonical.values import ValueParsingSpec
from ehr2trace.reference import Conversion, PlausibleRange, ReferenceTables, UnitTable
from ehr2trace.schema import EventKind, QualityFlag, QuarantineReason
from tests.unit.test_normalize import make_ctx, make_rows

UNITS = UnitTable({
    "mg": "mg", "mcg": "ug", "units": "[U]", "ml/hr": "mL/h", "mmol/l": "mmol/L",
    "meq/l": "meq/L", "degf": "[degF]", "degree celsius": "Cel", "ms": "ms",
    "ng/ml": "ng/mL", "ng/ml feu": "ng/mL{FEU}", "%": "%",
})
REFERENCE = ReferenceTables(
    units=UNITS,
    conversions={"[degF]": Conversion("Cel", Fraction(5, 9), Fraction(-160, 9))},
    ranges={("SOURCE", "TEMP", "Cel"): PlausibleRange(30.0, 45.0),
            ("SOURCE", "RESP", None): PlausibleRange(0.0, 80.0)},
    digest="test",
)


def ctx_with(source_id: str, spec: dict, reference: ReferenceTables = REFERENCE):
    ctx = make_ctx(source_id, spec)
    return dataclasses.replace(
        ctx,
        reference=reference,
        values=ValueParsingSpec(null_literals=("NULL",), known_units=reference.units.spellings),
    )


def events_of(ctx, records: list[dict]):
    return get_shape(ctx.spec.shape)(ctx, make_rows(ctx, records)).events


ORDER_SPEC = {
    "adapter": "delimited",
    "shape": "point_event",
    "event_kind": "drug_order",
    "fields": {
        "person_id": {"from": ["PID"]},
        "encounter_id": {"from": ["CSN"]},
        "source_code": {"from": ["drug"]},
        "dose": {"from": ["dose"]},
        "unit": {"from": ["dose_unit"]},
        "route": {"from": ["route"]},
        "status": {"from": ["status"]},
        "event_time": {"from": ["ordered"]},
        "end_time": {"from": ["ended"]},
        "sequence_number": {"from": ["seq"]},
    },
}
ORDER = {"PID": "P1", "CSN": "C1", "drug": "TESTDRUG", "ordered": "2020-01-01 08:00:00"}


# -- identity ---------------------------------------------------------------------


def test_same_time_orders_with_different_doses_stay_two_events():
    """The tempting answer is one event: same drug, same minute. It is two orders."""
    ctx = ctx_with("orders", ORDER_SPEC)
    events = events_of(ctx, [{**ORDER, "dose": "10 mg"}, {**ORDER, "dose": "20 mg"}])
    assert len({e.event_id for e in events}) == 2
    assert {e.dose_source for e in events} == {"10 mg", "20 mg"}


def test_the_same_dose_written_differently_collapses_to_one_event():
    """Two batches spell one dose two ways; the identity reads the dose, not the text."""
    ctx = ctx_with("orders", ORDER_SPEC)
    events = events_of(ctx, [{**ORDER, "dose": "10 mg"}, {**ORDER, "dose": "10 MG"},
                             {**ORDER, "dose": "10.0 mg"}])
    assert len({e.event_id for e in events}) == 1
    assert {e.dose_source for e in events} == {"10 mg", "10 MG", "10.0 mg"}, "the text is kept as written"


def test_an_unparseable_dose_still_separates_orders_by_its_text():
    ctx = ctx_with("orders", ORDER_SPEC)
    events = events_of(ctx, [{**ORDER, "dose": "1 tablet"}, {**ORDER, "dose": "2 tablets"},
                             {**ORDER, "dose": "1 TABLET"}])
    assert len({e.event_id for e in events}) == 2


@pytest.mark.parametrize("column,values", [
    ("dose_unit", ("mg", "mcg")),
    ("route", ("Oral", "Intravenous")),
    ("status", ("Sent", "Discontinued")),
    ("ended", ("2020-01-02 08:00:00", "2020-01-03 08:00:00")),
    ("seq", ("1", "2")),
])
def test_unit_route_status_end_time_and_sequence_number_are_part_of_a_drug_identity(column, values):
    ctx = ctx_with("orders", ORDER_SPEC)
    events = events_of(ctx, [{**ORDER, "dose": "10", column: values[0]}, {**ORDER, "dose": "10", column: values[1]}])
    assert len({e.event_id for e in events}) == 2, column


def test_a_dose_with_no_unit_and_a_unit_role_are_read_together():
    ctx = ctx_with("orders", ORDER_SPEC)
    a = events_of(ctx, [{**ORDER, "dose": "10", "dose_unit": "mg"}])[0]
    b = events_of(ctx, [{**ORDER, "dose": "10", "dose_unit": "MG"}])[0]
    assert a.event_id == b.event_id
    assert a.unit_source == "mg" and a.unit_normalized == "mg"


CONDITION_SPEC = {
    "adapter": "delimited",
    "shape": "point_event",
    "event_kind": "condition",
    "code_system": "ICD10CM",
    "fields": {
        "person_id": {"from": ["PID"]},
        "source_code": {"from": ["dx"]},
        "event_time": {"from": ["noted"]},
        "status": {"from": ["status"]},
    },
}


def test_a_condition_status_is_not_part_of_its_identity():
    """One problem-list entry, first Active then Resolved, is one diagnosis (D-R6)."""
    ctx = ctx_with("problems", CONDITION_SPEC)
    base = {"PID": "P1", "dx": "I26.99", "noted": "2020-01-01"}
    events = events_of(ctx, [{**base, "status": "Active"}, {**base, "status": "Resolved"}])
    assert len({e.event_id for e in events}) == 1


VISIT_SPEC = {
    "adapter": "parquet",
    "shape": "visit",
    "event_kind": "visit",
    "fields": {
        "person_id": {"from": ["PID"]},
        "event_time": {"from": ["start"]},
        "length_of_stay": {"from": ["los"]},
        "visit_type": {"from": ["kind"]},
        "discharged_to": {"from": ["disposition"]},
    },
}


def test_two_stays_starting_the_same_day_are_two_visits_only_when_the_config_says_so():
    """An ICU stay recorded to the day: the length of stay is what tells them apart (D-R4)."""
    rows = [{"PID": "P1", "start": "2020-01-01", "los": "0.5", "kind": "ICU"},
            {"PID": "P1", "start": "2020-01-01", "los": "3", "kind": "ICU"}]
    plain = events_of(ctx_with("icu", VISIT_SPEC), rows)
    assert len({e.event_id for e in plain}) == 1
    extra = events_of(ctx_with("icu", {**VISIT_SPEC, "identity_extra_fields": ["length_of_stay"]}), rows)
    assert len({e.event_id for e in extra}) == 2


def test_an_identity_extra_reads_a_number_as_a_number_and_a_time_as_a_time():
    spec = {**VISIT_SPEC, "identity_extra_fields": ["length_of_stay", "end_time"],
            "fields": {**VISIT_SPEC["fields"], "end_time": {"from": ["end"]}}}
    ctx = ctx_with("icu", spec)
    base = {"PID": "P1", "start": "2020-01-01", "kind": "ICU"}
    a = events_of(ctx, [{**base, "los": "3", "end": "2020-01-04 00:00:00"}])[0]
    b = events_of(ctx, [{**base, "los": "3.0", "end": "2020-01-04"}])[0]
    assert a.event_id == b.event_id


NOTE_SPEC = {
    "adapter": "parquet",
    "shape": "point_event",
    "event_kind": "note",
    "value_expect": "text",
    "fields": {
        "person_id": {"from": ["PID"]},
        "encounter_id": {"from": ["CSN"]},
        "source_code": {"from": ["kind"]},
        "event_time": {"from": ["charted"]},
        "text": {"from": ["body"]},
    },
}


def test_the_encounter_leaves_the_identity_when_the_source_says_its_encounter_ids_are_noise():
    """The same note filed under two encounter ids is one note (D-R2)."""
    rows = [{"PID": "P1", "CSN": "C1", "kind": "H&P", "charted": "2020-01-01", "body": "Admitted."},
            {"PID": "P1", "CSN": "C2", "kind": "H&P", "charted": "2020-01-01", "body": "Admitted."}]
    assert len({e.event_id for e in events_of(ctx_with("notes", NOTE_SPEC), rows)}) == 2
    collapsed = events_of(ctx_with("notes", {**NOTE_SPEC, "encounter_in_identity": False}), rows)
    assert len({e.event_id for e in collapsed}) == 1
    assert {e.encounter_id for e in collapsed} == {"C1", "C2"}, "each instance still carries its own encounter for the merge"


# -- units --------------------------------------------------------------------------


VITALS_SPEC = {
    "adapter": "parquet",
    "shape": "point_event",
    "event_kind": "measurement",
    "fields": {
        "person_id": {"from": ["PID"]},
        "source_code": {"from": ["name"]},
        "event_time": {"from": ["taken"]},
        "value": {"from": ["val"]},
        "unit": {"from": ["unit"]},
    },
}
VITAL = {"PID": "P1", "taken": "2020-01-01 08:00:00"}


def test_a_unit_override_converts_the_value_and_keeps_the_source_string():
    """CU-CTPA's temperatures: labelled Celsius, numerically Fahrenheit (D-R1)."""
    ctx = ctx_with("vitals", {**VITALS_SPEC, "unit_override": {"TEMP": "degF"}})
    event = events_of(ctx, [{**VITAL, "name": "TEMP", "val": "98.6", "unit": "degree Celsius"}])[0]
    assert event.value_number == 98.6 and event.unit_source == "degree Celsius", "the original is never overwritten"
    assert event.unit_normalized == "Cel" and event.value_number_normalized == 37.0
    assert str(QualityFlag.UNIT_OVERRIDDEN) in event.quality_flags


def test_a_declared_unit_fills_in_for_a_source_that_states_none():
    """ECG intervals in milliseconds with an empty unit column (T1.7)."""
    ctx = ctx_with("vitals", {**VITALS_SPEC, "declared_units": {"QTC": "ms"}})
    event = events_of(ctx, [{**VITAL, "name": "QTC", "val": "412"}])[0]
    assert event.unit_source is None, "the source stated no unit and none is invented for it"
    assert event.unit_normalized == "ms" and event.value_number_normalized == 412.0
    assert str(QualityFlag.UNIT_DECLARED) in event.quality_flags
    # a source unit, when present, wins over the declaration
    stated = events_of(ctx, [{**VITAL, "name": "QTC", "val": "412", "unit": "ms"}])[0]
    assert str(QualityFlag.UNIT_DECLARED) not in stated.quality_flags


def test_an_unknown_unit_normalizes_nothing_and_says_so():
    ctx = ctx_with("vitals", VITALS_SPEC)
    event = events_of(ctx, [{**VITAL, "name": "K", "val": "4.1", "unit": "furlongs"}])[0]
    assert event.value_number == 4.1 and event.unit_source == "furlongs"
    assert event.unit_normalized is None and event.value_number_normalized is None
    assert str(QualityFlag.UNIT_UNKNOWN) in event.quality_flags


def test_a_known_unit_without_a_conversion_passes_the_value_through():
    ctx = ctx_with("vitals", VITALS_SPEC)
    event = events_of(ctx, [{**VITAL, "name": "NA", "val": "138", "unit": "mmol/L"}])[0]
    assert (event.unit_normalized, event.value_number_normalized) == ("mmol/L", 138.0)
    assert not {str(QualityFlag.UNIT_UNKNOWN), str(QualityFlag.UNIT_DECLARED)} & set(event.quality_flags)


def test_a_unitless_number_keeps_its_value_in_the_normalized_column():
    ctx = ctx_with("vitals", VITALS_SPEC)
    event = events_of(ctx, [{**VITAL, "name": "RESP", "val": "18"}])[0]
    assert event.unit_normalized is None and event.value_number_normalized == 18.0
    assert str(QualityFlag.UNIT_UNKNOWN) not in event.quality_flags


def test_an_implausible_value_is_kept_flagged_and_not_normalized():
    """A temperature of -15.6 C is not deleted and not rewritten; it is marked (D-R17)."""
    ctx = ctx_with("vitals", {**VITALS_SPEC, "unit_override": {"TEMP": "degF"}})
    event = events_of(ctx, [{**VITAL, "name": "TEMP", "val": "134.6", "unit": "degree Celsius"}])[0]
    assert event.value_number == 134.6
    assert event.value_number_normalized is None and event.unit_normalized == "Cel"
    assert str(QualityFlag.IMPLAUSIBLE) in event.quality_flags
    # the range is judged after conversion: 98.6 F is 37 C and inside [30, 45]
    fine = events_of(ctx, [{**VITAL, "name": "TEMP", "val": "98.6", "unit": "degree Celsius"}])[0]
    assert str(QualityFlag.IMPLAUSIBLE) not in fine.quality_flags
    # a unitless range applies to a unitless value
    resp = events_of(ctx, [{**VITAL, "name": "RESP", "val": "196"}])[0]
    assert str(QualityFlag.IMPLAUSIBLE) in resp.quality_flags and resp.value_number == 196.0


def test_a_code_that_mixes_incommensurable_units_is_split_by_unit():
    """D-dimer in ng/mL and in ng/mL FEU are two measurements, not one (D-R10)."""
    ctx = ctx_with("vitals", {**VITALS_SPEC, "split_code_by_unit": ["DDIMER"]})
    a = events_of(ctx, [{**VITAL, "name": "DDIMER", "val": "500", "unit": "ng/mL"}])[0]
    b = events_of(ctx, [{**VITAL, "name": "DDIMER", "val": "500", "unit": "ng/mL FEU"}])[0]
    assert a.source_code == "DDIMER|ng/mL" and b.source_code == "DDIMER|ng/mL FEU"
    assert a.event_id != b.event_id
    assert str(QualityFlag.CODE_SPLIT_BY_UNIT) in a.quality_flags
    assert a.unit_normalized == "ng/mL" and b.unit_normalized == "ng/mL{FEU}"
    # a row of that code with no unit keeps the bare code
    bare = events_of(ctx, [{**VITAL, "name": "DDIMER", "val": "500"}])[0]
    assert bare.source_code == "DDIMER" and str(QualityFlag.CODE_SPLIT_BY_UNIT) not in bare.quality_flags


def test_a_number_followed_by_words_that_are_not_a_unit_is_text():
    """`2 SINUS TACHYCARDIA` is a diagnosis line, not two of something (P-C8)."""
    ctx = ctx_with("vitals", VITALS_SPEC)
    event = events_of(ctx, [{**VITAL, "name": "DX", "val": "2 SINUS TACHYCARDIA"}])[0]
    assert event.value_number is None and event.value_text == "2 SINUS TACHYCARDIA"
    assert event.unit_source is None
    known = events_of(ctx, [{**VITAL, "name": "K", "val": "4.1 mmol/L"}])[0]
    assert known.value_number == 4.1 and known.unit_source == "mmol/L" and known.unit_normalized == "mmol/L"


def test_a_number_followed_by_words_is_quarantined_where_a_number_is_required():
    ctx = ctx_with("vitals", {**VITALS_SPEC, "value_expect": "numeric"})
    out = get_shape("point_event")(ctx, make_rows(ctx, [{**VITAL, "name": "DX", "val": "2 SINUS TACHYCARDIA"}]))
    assert out.events == []
    assert out.quarantine[0]["reason"] == str(QuarantineReason.UNPARSEABLE_VALUE)


COMPONENT_SPEC = {
    "adapter": "delimited",
    "shape": "component_measurements",
    "event_kind": "measurement",
    "fields": {
        "person_id": {"from": ["PID"]},
        "display_name": {"from": ["study"]},
        "event_time": {"from": ["resulted"]},
        "source_code": {"from": ["component"]},
        "value": {"from": ["val"]},
    },
}


def test_components_get_declared_units_and_ranges_too():
    ctx = ctx_with("ekg", {**COMPONENT_SPEC, "declared_units": {"QTC": "ms"}})
    rows = [{"PID": "P1", "study": "ECG", "resulted": "2020-01-01 08:00:00", "component": "QTC", "val": "412"},
            {"PID": "P1", "study": "ECG", "resulted": "2020-01-01 08:00:00", "component": "DX", "val": "2 SINUS TACHYCARDIA"}]
    events = events_of(ctx, rows)
    qtc = next(e for e in events if e.source_code == "QTC")
    assert qtc.unit_normalized == "ms" and str(QualityFlag.UNIT_DECLARED) in qtc.quality_flags
    dx = next(e for e in events if e.source_code == "DX")
    assert dx.value_text == "2 SINUS TACHYCARDIA" and dx.value_number is None


# -- new roles --------------------------------------------------------------------


ADMIN_SPEC = {
    "adapter": "parquet",
    "shape": "point_event",
    "event_kind": "drug_admin",
    "fields": {
        "person_id": {"from": ["PID"]},
        "source_code": {"from": ["drug"]},
        "event_time": {"from": ["given"]},
        "rate": {"from": ["rate"]},
        "rate_unit": {"from": ["rate_unit"]},
        "action": {"from": ["action"]},
    },
}
GIVEN = {"PID": "P1", "drug": "HEPARIN", "given": "2020-01-01 08:00:00"}


def test_rate_rate_unit_and_action_reach_their_columns():
    ctx = ctx_with("emar", ADMIN_SPEC)
    event = events_of(ctx, [{**GIVEN, "rate": "12.5", "rate_unit": "units/hr", "action": "Started"}])[0]
    assert (event.rate_source, event.rate, event.rate_unit, event.action) == ("12.5", 12.5, "units/hr", "Started")
    assert str(QualityFlag.RATE_UNPARSED) not in event.quality_flags


def test_a_rate_carrying_its_own_unit_supplies_the_unit_when_none_is_declared():
    ctx = ctx_with("emar", ADMIN_SPEC)
    event = events_of(ctx, [{**GIVEN, "rate": "25 mL/hr"}])[0]
    assert (event.rate_source, event.rate, event.rate_unit) == ("25 mL/hr", 25.0, "mL/hr")


def test_a_rate_that_is_not_a_number_is_kept_verbatim_and_flagged():
    ctx = ctx_with("emar", ADMIN_SPEC)
    event = events_of(ctx, [{**GIVEN, "rate": "titrate to effect"}])[0]
    assert event.rate_source == "titrate to effect" and event.rate is None
    assert str(QualityFlag.RATE_UNPARSED) in event.quality_flags


def test_a_visit_carries_where_it_discharged_to():
    ctx = ctx_with("visits", VISIT_SPEC)
    event = events_of(ctx, [{"PID": "P1", "start": "2020-01-01 10:00:00", "kind": "Inpatient", "disposition": "HOME"}])[0]
    assert event.discharged_to == "HOME"


def test_the_visit_shape_publishes_a_visit_detail_when_the_config_says_so():
    """A transfer or an ICU stay is a stay inside a visit, never a visit of its own."""
    ctx = ctx_with("transfers", {**VISIT_SPEC, "event_kind": "visit_detail"})
    event = events_of(ctx, [{"PID": "P1", "start": "2020-01-01 10:00:00", "kind": "MICU"}])[0]
    assert event.event_kind == EventKind.visit_detail
    with pytest.raises(ValueError, match="visit"):
        events_of(ctx_with("transfers", {**VISIT_SPEC, "event_kind": "condition"}),
                  [{"PID": "P1", "start": "2020-01-01 10:00:00", "kind": "MICU"}])


def test_an_observation_is_just_a_kind_of_point_event():
    ctx = ctx_with("followup", {**VITALS_SPEC, "event_kind": "observation"})
    event = events_of(ctx, [{**VITAL, "name": "FOLLOWUP", "val": "alive at last contact"}])[0]
    assert event.event_kind == EventKind.observation and event.value_text == "alive at last contact"


# -- flag_when and excluded_status ------------------------------------------------


def test_a_truthy_marker_column_adds_the_declared_flag():
    """A preparation step's provenance marker reaches the event (D-R18)."""
    spec = {**ADMIN_SPEC, "flag_when": {"NAME_FROM_LINKED_ORDER": "name_recovered"}}
    ctx = ctx_with("emar", spec)
    rows = [{**GIVEN, "name_recovered": v} for v in ("1", "true", "YES", "y", "T", "0", "false", "", None)]
    events = events_of(ctx, rows)
    flagged = [str(QualityFlag.NAME_FROM_LINKED_ORDER) in e.quality_flags for e in events]
    assert flagged == [True] * 5 + [False] * 4


def test_a_marker_column_the_source_lacks_raises_rather_than_flagging_nothing():
    spec = {**ADMIN_SPEC, "flag_when": {"NAME_FROM_LINKED_ORDER": "absent_column"}}
    ctx = ctx_with("emar", spec)
    with pytest.raises(ValueError, match="absent_column"):
        events_of(ctx, [GIVEN])


def test_excluded_statuses_are_dropped_before_shaping_like_a_row_filter():
    """A problem list's deleted entries are not diagnoses (D-R6)."""
    ctx = ctx_with("problems", {**CONDITION_SPEC, "excluded_status": ["Deleted"]})
    records = [{"PID": "P1", "dx": "I26.99", "noted": "2020-01-01", "status": "Active"},
               {"PID": "P1", "dx": "E11.9", "noted": "2020-01-01", "status": "deleted"},
               {"PID": "P1", "dx": "J45.909", "noted": "2020-01-01", "status": None}]
    rows = make_rows(ctx, records)
    columns = {f"{COL_PREFIX}{k}" for r in records for k in r}
    kept = filter_rows(ctx.spec, rows, columns)
    assert [r.text("source_code") for r in kept] == ["I26.99", "J45.909"]


# -- the merge payload ------------------------------------------------------------


def test_every_event_is_emitted_with_the_row_values_the_merge_needs():
    spec = {**ORDER_SPEC, "keep_columns": ["billing"],
            "merge_rules": {"billing": "keep_all_flag", "status": "priority", "encounter_id": "prefer_linked"},
            "fields": {**ORDER_SPEC["fields"], "encounter_linked": {"from": ["linked"]}}}
    spec["merge_rules"]["status"] = {"rule": "priority", "order": ["Sent"]}
    ctx = ctx_with("orders", spec)
    out = get_shape("point_event")(ctx, make_rows(ctx, [{**ORDER, "dose": "10 mg", "billing": "HB", "linked": "1", "status": "Sent"}]))
    assert len(out.instances) == len(out.events) == 1
    instance = out.instances[0]
    assert instance.source_row_id == "row1" and instance.partition_id == "p1"
    # a rule keyed by a canonical field or its role compares the event itself; only kept
    # columns and the linked-by role are carried beside it
    assert instance.extra == {"billing": "HB", "encounter_linked": "1"}


LAB_SPEC = {
    "adapter": "delimited",
    "shape": "point_event",
    "event_kind": "measurement",
    "fields": {
        "person_id": {"from": ["PID"]},
        "source_code": {"from": ["test"]},
        "event_time": {"from": ["taken"]},
        "value": {"from": ["result"]},
        "unit": {"from": ["units"]},
        "value_low": {"from": ["ref_low"]},
        "value_high": {"from": ["ref_high"]},
    },
}


def test_a_reference_range_reaches_its_own_columns_and_never_the_result():
    """P-J6: the laboratory's reference interval was mapped and read by nothing."""
    ctx = ctx_with("labs", LAB_SPEC)
    row = {"PID": "P1", "test": "NA", "units": "mmol/L"}
    plain, ranged, words = events_of(ctx, [
        {**row, "taken": "2020-01-01 08:00:00", "result": "138", "ref_low": "135", "ref_high": "145"},
        {**row, "taken": "2020-01-01 09:00:00", "result": "5-10", "ref_low": "", "ref_high": ""},
        {**row, "taken": "2020-01-01 10:00:00", "result": "140", "ref_low": "<135", "ref_high": "NEGATIVE"},
    ])
    assert (plain.range_low, plain.range_high) == (135.0, 145.0)
    assert (plain.value_low, plain.value_high) == (None, None)
    assert (ranged.range_low, ranged.range_high) == (None, None)
    assert (ranged.value_low, ranged.value_high) == (5.0, 10.0)
    assert (words.range_low, words.range_high) == (None, None)
