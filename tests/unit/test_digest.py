"""A digest that answers two questions has to be blind to the right things.

Determinism is measured by comparing two builds, and fault localisation by comparing a
mutated clone against the tree it came from. Both comparisons are only meaningful if the
digest ignores what two correct builds are allowed to differ on -- row order, column
order, and how parquet chose to lay the file out -- while still noticing a changed value.

The last test is the uncomfortable one: the digest is deliberately blind to row order, so
a fault that only reorders rows leaves it with nothing to report. That is a real blind
spot in the instrument, and it is asserted here rather than left to be rediscovered.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ehr2cdm.digest import changed, digest_frame, digest_parquet, mentions


def _frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "subject_id": [3, 1, 2],
            "value": [1.5, None, 2.5],
            "flags": [["a"], [], ["b", "c"]],
        }
    )


def test_row_order_does_not_change_the_digest():
    frame = _frame()
    assert digest_frame(frame) == digest_frame(frame.reverse())


def test_column_order_does_not_change_the_digest():
    frame = _frame()
    reordered = frame.select(["flags", "value", "subject_id"])
    assert digest_frame(frame) == digest_frame(reordered)


def test_a_changed_value_changes_the_digest():
    frame = _frame()
    mutated = frame.with_columns(pl.col("value").fill_null(9.0))
    assert digest_frame(frame) != digest_frame(mutated)


def test_a_dropped_row_changes_the_digest():
    frame = _frame()
    assert digest_frame(frame) != digest_frame(frame.head(2))


def test_a_renamed_column_changes_the_digest():
    frame = _frame()
    assert digest_frame(frame) != digest_frame(frame.rename({"value": "amount"}))


def test_nested_columns_do_not_raise():
    # A cast to string raises on list[str]; the first version of the digest died here
    # while measuring reproducibility, on `quality_flags`.
    digest_frame(_frame())


def test_the_digest_survives_a_round_trip_through_parquet(tmp_path: Path):
    frame = _frame()
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    frame.write_parquet(a)
    frame.reverse().write_parquet(b, compression="uncompressed")
    assert digest_parquet(a) == digest_parquet(b)
    assert a.read_bytes() != b.read_bytes()


def test_changed_reports_additions_removals_and_edits():
    before = {"canonical/events": "1", "omop/person": "2"}
    after = {"canonical/events": "9", "meds/data": "3"}
    assert changed(before, after) == ["canonical/events", "meds/data", "omop/person"]


def test_mentions_matches_a_check_id_as_well_as_its_message():
    artifacts = ["meds/metadata/subject_splits", "canonical/events"]
    hit = mentions("MEDS_SPLITS_DISJOINT_AND_COMPLETE one subject in two", artifacts)
    assert hit == ["meds/metadata/subject_splits"]


def test_mentions_ignores_short_words():
    # `omop/death` would otherwise be named by any message containing "the".
    assert mentions("a message with no artifact name", ["omop/death"]) == []


def test_reordering_rows_is_invisible_to_the_digest():
    # Asserted, not lamented: the shard-ordering fault is detected by the check suite and
    # not by this instrument, and the paper reports the localisation number as a ceiling
    # for exactly this reason.
    frame = _frame()
    assert digest_frame(frame) == digest_frame(frame.sort("subject_id"))
