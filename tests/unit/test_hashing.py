"""Canonical serialization and stable ids (checklist P0-3).

These are the tests the checklist says to write first, because every failure mode here
is silent: a hash that differs by type produces two events where there should be one,
and nothing raises.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from ehr2cdm.hashing import (
    bucket_of,
    canonical_cell,
    canonical_row,
    row_sha256,
    source_row_id,
    split_of,
    stable_id,
    subject_id_from_person_key,
)


def test_death_date_str_and_datetime_normalize_identically():
    """One workbook stores this as text and another as a date. Same patient, same value."""
    as_datetime = datetime(2019, 4, 17, 0, 0, 0)
    as_text = "2019-04-17 00:00:00"
    assert canonical_cell(as_datetime) == canonical_cell(as_text) == "2019-04-17T00:00:00"


def test_death_date_with_subsecond_text_matches_datetime():
    assert canonical_cell(datetime(2019, 4, 17, 13, 5, 9, 123456)) == "2019-04-17T13:05:09"


def test_length_of_stay_int_and_str_normalize_identically():
    """Integer in three partitions, text in the fourth."""
    assert canonical_cell(4) == canonical_cell("4") == "4"


def test_float_and_int_normalize_identically():
    assert canonical_cell(5.0) == canonical_cell(5) == "5"
    assert canonical_cell(-0.0) == "0"


def test_non_integral_float_round_trips():
    assert canonical_cell(4.25) == "4.25"
    assert float(canonical_cell(0.1 + 0.2)) == 0.1 + 0.2


def test_null_literal_empty_and_none_agree():
    assert canonical_cell(None) == canonical_cell("") == canonical_cell("NULL") == ""
    assert canonical_cell("  NULL  ") == ""


def test_null_literal_is_configurable_and_case_sensitive_by_default():
    assert canonical_cell("null") == "null"
    assert canonical_cell("n/a", null_literals=("n/a",)) == ""


def test_bool_before_int():
    assert canonical_cell(True) == "true"
    assert canonical_cell(False) == "false"


def test_date_normalizes_to_midnight():
    assert canonical_cell(date(2020, 2, 29)) == "2020-02-29T00:00:00"


def test_unknown_cell_type_raises_rather_than_guessing():
    class Weird:
        pass

    with pytest.raises(TypeError):
        canonical_cell(Weird())


def test_row_hash_is_type_drift_immune():
    typed = ["BV1", datetime(2019, 4, 17), 4, None]
    texted = ["BV1", "2019-04-17 00:00:00", "4", "NULL"]
    assert row_sha256(typed) == row_sha256(texted)


def test_row_hash_separates_fields():
    """Joining without a separator would make ('ab','c') and ('a','bc') collide."""
    assert row_sha256(["ab", "c"]) != row_sha256(["a", "bc"])
    assert "\x1f" in canonical_row(["a", "b"])


def test_source_row_id_is_stable_and_row_specific():
    a = source_row_id("ds", "p1", "labs", "f" * 64, 7)
    b = source_row_id("ds", "p1", "labs", "f" * 64, 7)
    c = source_row_id("ds", "p1", "labs", "f" * 64, 8)
    assert a == b != c


def test_source_row_id_separates_partitions_and_sources():
    base = dict(dataset_id="ds", file_sha256="f" * 64, row_number=1)
    assert source_row_id(partition_id="p1", source_id="labs", **base) != source_row_id(
        partition_id="p2", source_id="labs", **base
    )
    assert source_row_id(partition_id="p1", source_id="labs", **base) != source_row_id(
        partition_id="p1", source_id="ekg", **base
    )


def test_subject_id_is_stable_positive_and_salted():
    unsalted = subject_id_from_person_key("ctpe", "ZQ99000001")
    assert unsalted == subject_id_from_person_key("ctpe", " ZQ99000001 ")
    assert 0 < unsalted < 2**63
    assert subject_id_from_person_key("ctpe", "ZQ99000001", salt="s3cret") != unsalted
    assert subject_id_from_person_key("other", "ZQ99000001") != unsalted


def test_subject_id_does_not_leak_the_key():
    """The mapping is one-way: the id carries no substring of the patient key."""
    key = "ZQ99000001"
    assert key not in str(subject_id_from_person_key("ctpe", key))


def test_bucket_is_stable_and_in_range():
    sid = subject_id_from_person_key("ctpe", "BV1")
    assert bucket_of(sid, 64) == bucket_of(sid, 64)
    assert 0 <= bucket_of(sid, 64) < 64


def test_bucket_count_must_be_positive():
    with pytest.raises(ValueError):
        bucket_of(1, 0)


def test_split_assignment_is_stable_and_covers_the_weights():
    weights = [("train", 0.8), ("tuning", 0.1), ("held_out", 0.1)]
    sids = [subject_id_from_person_key("ctpe", f"P{i}") for i in range(400)]
    first = [split_of(s, weights) for s in sids]
    assert first == [split_of(s, weights) for s in sids]
    assert set(first) == {"train", "tuning", "held_out"}


def test_stable_id_depends_on_every_part():
    assert stable_id("a", "b") != stable_id("a", "c")
    assert stable_id("a", "b") == stable_id("a", "b")


def test_text_timestamp_matches_datetime_cell():
    """The drift that would break cross-batch dedup: text date vs date cell."""
    assert canonical_cell("2031-02-03 04:05:00") == canonical_cell(datetime(2031, 2, 3, 4, 5))
    assert canonical_cell("2031-02-03 04:05") == canonical_cell(datetime(2031, 2, 3, 4, 5))
    assert canonical_cell("2031-02-03 04:05:00.0000000") == canonical_cell(
        datetime(2031, 2, 3, 4, 5)
    )
    assert canonical_cell("2031-02-03") == canonical_cell(date(2031, 2, 3))


def test_non_timestamp_text_is_left_alone():
    assert canonical_cell("SINUS TACHYCARDIA") == "SINUS TACHYCARDIA"
    assert canonical_cell("2031-02-03 patient seen") == "2031-02-03 patient seen"


def test_storage_keeps_the_written_shape_while_hashing_normalizes_it():
    """The two functions have different jobs, and both jobs matter.

    Hashing must flatten a date typed as text onto a date typed as a date, or
    cross-batch deduplication fails silently. Storage must *not* flatten a bare date
    onto midnight, because "this source had no time to give" is a fact the anchor
    handling depends on.
    """
    from ehr2cdm.hashing import source_cell

    assert canonical_cell("2031-02-03 04:05:00") == canonical_cell(datetime(2031, 2, 3, 4, 5))
    assert source_cell("2031-02-03") == "2031-02-03"
    assert source_cell(datetime(2031, 2, 3, 0, 0)) == "2031-02-03T00:00:00"
    assert source_cell("NULL") == ""
    assert source_cell(None) == ""
    assert source_cell(5.0) == "5"
