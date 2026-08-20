"""Time parsing and timezone handling (design section 5.3, checklist P1-4)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ehr2cdm.errors import QuarantineRow
from ehr2cdm.schema import QualityFlag
from ehr2cdm.timeutil import TimeContext, days_between, looks_date_only, parse_naive, parse_utc, to_utc

FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
CTX = TimeContext(formats=FORMATS, null_literals=("NULL",), timezone_name=None)
NY = TimeContext(formats=FORMATS, null_literals=("NULL",), timezone_name="America/New_York")


def test_seven_digit_fraction_parses():
    """One batch writes seven fractional digits, which strptime's %f will not take."""
    assert parse_naive("2018-03-03 13:49:00.0000000", CTX) == datetime(2018, 3, 3, 13, 49)


def test_three_digit_fraction_parses():
    assert parse_naive("2017-12-15 11:01:00.000", CTX) == datetime(2017, 12, 15, 11, 1)


def test_date_only_parses():
    assert parse_naive("2018-03-03", CTX) == datetime(2018, 3, 3)


def test_native_datetime_and_date_cells():
    assert parse_naive(datetime(2031, 2, 3, 4, 5), CTX) == datetime(2031, 2, 3, 4, 5)
    assert parse_naive(date(2031, 2, 3), CTX) == datetime(2031, 2, 3)


def test_null_and_blank_are_absent():
    for raw in (None, "", "  ", "NULL"):
        assert parse_naive(raw, CTX) is None


def test_unparseable_timestamp_is_quarantined_not_guessed():
    with pytest.raises(QuarantineRow):
        parse_naive("last tuesday", CTX)
    with pytest.raises(QuarantineRow):
        parse_naive("03/03/2018", CTX)  # not a declared format for this dataset


def test_conversion_to_utc_uses_the_declared_zone():
    utc, flags = to_utc(datetime(2018, 3, 3, 13, 49), NY)
    assert utc == datetime(2018, 3, 3, 18, 49)
    assert flags == []


def test_operator_supplied_zone_is_flagged_on_every_row():
    assumed = TimeContext(FORMATS, ("NULL",), "America/New_York", timezone_assumed=True)
    _utc, flags = to_utc(datetime(2018, 3, 3, 13, 49), assumed)
    assert str(QualityFlag.TZ_ASSUMED) in flags


def test_no_zone_stores_the_recorded_time_and_flags_it():
    """Reachable only under an explicit policy; never a silent 'treat it as UTC'."""
    utc, flags = to_utc(datetime(2018, 3, 3, 13, 49), CTX)
    assert utc == datetime(2018, 3, 3, 13, 49)
    assert str(QualityFlag.TZ_ASSUMED) in flags


def test_daylight_saving_boundary_is_deterministic():
    ambiguous = datetime(2018, 11, 4, 1, 30)
    first, _ = to_utc(ambiguous, NY)
    second, _ = to_utc(ambiguous, NY)
    assert first == second


def test_date_only_detection_distinguishes_the_two_batches():
    assert looks_date_only("2018-03-03")
    assert not looks_date_only("2018-03-03 13:49:00.0000000")
    assert looks_date_only(date(2018, 3, 3))
    assert not looks_date_only(datetime(2018, 3, 3, 13, 49))


def test_day_difference_is_computed_from_timestamps():
    a = datetime(2018, 3, 5, 12, 0)
    b = datetime(2018, 3, 3, 12, 0)
    assert days_between(a, b) == 2.0
    assert days_between(None, b) is None


def test_parse_utc_combines_both_steps():
    utc, _flags = parse_utc("2018-03-03 13:49:00.0000000", NY)
    assert utc == datetime(2018, 3, 3, 18, 49)
