"""A masked display value gives way to a later alias that holds the number."""
from ehr2trace.canonical.normalize import Row

ROLES = {"value": ["col__value", "col__valuenum"]}
MASKED = {"value": frozenset({"___"})}


def test_masked_value_falls_back_to_the_next_alias():
    row = Row({"col__value": "___", "col__valuenum": "102.6"}, ROLES, MASKED)
    assert row.raw_with_index("value") == ("102.6", 1)


def test_unmasked_value_is_still_preferred():
    row = Row({"col__value": ">150", "col__valuenum": "150"}, ROLES, MASKED)
    assert row.raw_with_index("value") == (">150", 0)


def test_masked_value_with_nothing_behind_it_is_kept():
    row = Row({"col__value": " ___ ", "col__valuenum": None}, ROLES, MASKED)
    assert row.raw_with_index("value") == (" ___ ", 0)


def test_without_a_declaration_nothing_changes():
    row = Row({"col__value": "___", "col__valuenum": "102.6"}, ROLES)
    assert row.raw_with_index("value") == ("___", 0)


def test_point_event_publishes_the_number_and_flags_it():
    from test_normalize import LAB_SPEC, make_ctx, make_rows

    from ehr2trace.canonical.normalize import build_masked_map, get_shape
    from ehr2trace.schema import QualityFlag

    spec = {**LAB_SPEC, "fields": {**LAB_SPEC["fields"], "value": {"from": ["val", "valnum"], "masked": ["___"]}}}
    ctx = make_ctx("labs", spec)
    records = [
        {"PID": "P1", "collected": "2018-02-22 13:33:00", "code": "PTT", "val": "___", "valnum": "102.6"},
        {"PID": "P1", "collected": "2018-02-22 14:33:00", "code": "PTT", "val": "32.7", "valnum": "32.7"},
    ]
    rows = [Row(r.data, r.roles, build_masked_map(ctx.spec)) for r in make_rows(ctx, records)]
    masked, plain = get_shape("point_event")(ctx, rows).events
    assert masked.value_number == 102.6 and str(QualityFlag.VALUE_FALLBACK) in masked.quality_flags
    assert plain.value_number == 32.7 and str(QualityFlag.VALUE_FALLBACK) not in plain.quality_flags
