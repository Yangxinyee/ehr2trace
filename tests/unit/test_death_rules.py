"""Two recordings of one death, and two recordings of two deaths (T1.8, D-R11).

The audit of 2026-09-13 found the same patient dying twice: one source files the date
the registrar has, another the hour the monitor stopped, and both became events. The
dates are compared in the dataset's own zone, because ``event_time`` is a naive UTC
instant and a death in the evening in New York is stored under the next UTC date -- the
tempting wrong answer, and the one that would split one death in two for every patient
who died after seven in the evening.
"""

from __future__ import annotations

import csv
from datetime import datetime

from ehr2trace.canonical.build import apply_cross_event_rules, write_death_conflicts
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind, QualityFlag

ZONE = "America/New_York"

#: 2020-01-01 as a bare date in New York, and 21:30 the same evening: 05:00 and 02:30
#: UTC, on two different UTC dates and on one local day.
DATE_ONLY = datetime(2020, 1, 1, 5, 0)
THAT_EVENING = datetime(2020, 1, 2, 2, 30)
NEXT_DAY = datetime(2020, 1, 3, 5, 0)


def death(event_id: str, event_time: datetime, subject_id: int = 1) -> dict:
    return {
        "event_id": event_id,
        "subject_id": subject_id,
        "event_kind": str(EventKind.death),
        "event_time": event_time,
        "quality_flags": [],
    }


def other(event_id: str, event_time: datetime, subject_id: int = 1) -> dict:
    return {
        "event_id": event_id,
        "subject_id": subject_id,
        "event_kind": str(EventKind.condition),
        "event_time": event_time,
        "quality_flags": [],
    }


def test_a_date_and_an_hour_on_one_local_day_are_one_death():
    events, issues, merged = apply_cross_event_rules(
        [death("bare", DATE_ONLY), death("timed", THAT_EVENING)], ZONE
    )
    assert [e["event_id"] for e in events] == ["timed"], "the recorded hour is the better record"
    assert str(QualityFlag.DEATH_TIME_MERGED) in events[0]["quality_flags"]
    assert merged == {"bare": "timed"}, "the dropped event's source rows follow it"
    assert issues == []


def test_the_local_day_is_what_counts_not_the_utc_day():
    """Both readings, in UTC, are on different dates; in New York they are one evening."""
    assert DATE_ONLY.date() != THAT_EVENING.date()
    _events, issues, merged = apply_cross_event_rules(
        [death("bare", DATE_ONLY), death("timed", THAT_EVENING)], ZONE
    )
    assert merged and issues == []
    # read as UTC -- no zone declared -- the same two rows are a disagreement
    events, issues, merged = apply_cross_event_rules(
        [death("bare", DATE_ONLY), death("timed", THAT_EVENING)], None
    )
    assert merged == {} and len(events) == 2 and len(issues) == 1


def test_two_different_local_days_keep_both_deaths_and_ask_a_human():
    events, issues, merged = apply_cross_event_rules(
        [death("first", DATE_ONLY), death("second", NEXT_DAY)], ZONE
    )
    assert merged == {}, "a converter may not decide which day someone died on"
    assert {e["event_id"] for e in events} == {"first", "second"}
    assert all(str(QualityFlag.DEATH_DATE_CONFLICT) in e["quality_flags"] for e in events)
    assert len(issues) == 1
    assert issues[0]["issue_type"] == str(QualityFlag.DEATH_DATE_CONFLICT)
    assert issues[0]["severity"] == "error" and issues[0]["stage"] == "canonical"
    assert issues[0]["detail"] == "2020-01-01; 2020-01-03", "the local dates, not the instants"


def test_three_recordings_over_two_days_collapse_to_one_death_per_day():
    events, issues, merged = apply_cross_event_rules(
        [death("bare", DATE_ONLY), death("timed", THAT_EVENING), death("later", NEXT_DAY)], ZONE
    )
    assert merged == {"bare": "timed"}
    assert {e["event_id"] for e in events} == {"timed", "later"}
    assert all(str(QualityFlag.DEATH_DATE_CONFLICT) in e["quality_flags"] for e in events)
    assert len(issues) == 1


def test_the_answer_does_not_depend_on_the_order_the_events_arrive_in():
    """Events reach here in whatever order a worker finished; publication order is
    ``sort_events``'s job, so what has to match is which event survived and why."""
    rows = [death("timed", THAT_EVENING), death("bare", DATE_ONLY), death("later", NEXT_DAY)]

    def outcome(ordered):
        events, issues, merged = apply_cross_event_rules(ordered, ZONE)
        return sorted((e["event_id"], tuple(e["quality_flags"])) for e in events), issues, merged

    expected = outcome(list(rows))
    for order in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
        assert outcome([rows[i] for i in order]) == expected


def test_one_subjects_deaths_never_reach_another_subject():
    events, issues, merged = apply_cross_event_rules(
        [death("a", DATE_ONLY, subject_id=1), death("b", NEXT_DAY, subject_id=2)], ZONE
    )
    assert merged == {} and issues == []
    assert not any(e["quality_flags"] for e in events)


def test_a_record_after_the_earliest_death_is_flagged_and_kept():
    events, _issues, _merged = apply_cross_event_rules(
        [death("bare", DATE_ONLY), death("later", NEXT_DAY),
         other("late", datetime(2020, 1, 9, 12, 0)),
         other("before", datetime(2019, 12, 30, 12, 0))], ZONE
    )
    flagged = {e["event_id"] for e in events if str(QualityFlag.RECORDED_AFTER_DEATH) in e["quality_flags"]}
    assert flagged == {"late"}, "the earliest death is the one a later record is measured against"
    assert {e["event_id"] for e in events} == {"bare", "later", "late", "before"}


def test_an_untimed_death_is_left_alone():
    rows = [death("no_time", None), death("timed", THAT_EVENING)]
    events, issues, merged = apply_cross_event_rules(rows, ZONE)
    assert merged == {} and issues == []
    assert len(events) == 2


def test_the_conflicts_are_listed_for_review_even_when_there_are_none(tmp_path):
    layout = WorkLayout(root=tmp_path, dataset_id="d").ensure()
    write_death_conflicts(layout)
    path = layout.review_dir / "death_conflicts.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh)) == [["subject_id", "dates"]], "an empty list is a result"
