"""Merge rules: what happens when rows that are one event disagree (T1.2, T1.3).

The audit of 2026-09-13 found ``merge_events`` keeping whichever value it met first
(P-C2): a visit's end, a diagnosis's status, a result's availability. Every rule here is
a choice a dataset makes in its YAML; the one thing the merge decides on its own is that
an undeclared disagreement is a recorded conflict and never a silent pick.
"""

from __future__ import annotations

import random
from datetime import datetime

from ehr2trace.canonical.dedup import INSTANCE_KEY, Instance, merge_events
from ehr2trace.config import MergeRuleSpec
from ehr2trace.schema import QualityFlag, QuarantineReason


def instance(event_id: str, row: str, extra: dict | None = None, **fields) -> dict:
    base = {
        "event_id": event_id,
        "subject_id": 1,
        "source_id": "src",
        "event_kind": "measurement",
        "event_time": datetime(2020, 1, 1, 10, 0),
        "quality_flags": [],
        INSTANCE_KEY: Instance(source_row_id=row, partition_id="p1", extra=extra),
    }
    base.update(fields)
    return base


def rules(**declared) -> dict:
    return {"src": {k: MergeRuleSpec.model_validate({"rule": v} if isinstance(v, str) else v)
                    for k, v in declared.items()}}


T1, T2, T3 = datetime(2020, 1, 1, 12, 0), datetime(2020, 1, 1, 13, 0), datetime(2020, 1, 2, 9, 0)


def test_earliest_keeps_the_smallest_value_and_flags_the_event():
    result = merge_events([instance("e", "r2", available_time=T2), instance("e", "r1", available_time=T1)],
                          rules(available_time="earliest"))
    survivor = result.events[0]
    assert survivor["available_time"] == T1
    assert str(QualityFlag.AVAILABILITY_MERGED) in survivor["quality_flags"]
    assert result.issues == []


def test_latest_keeps_the_largest_value():
    result = merge_events([instance("e", "r1", end_time=T1), instance("e", "r2", end_time=T3)],
                          rules(end_time={"rule": "latest", "flag": "MERGE_CONFLICT"}))
    assert result.events[0]["end_time"] == T3
    assert result.events[0]["quality_flags"] == [str(QualityFlag.MERGE_CONFLICT)]


def test_availability_defaults_to_earliest_even_when_undeclared():
    """D-R5: the earliest visibility is the conservative one, and needs no declaration."""
    result = merge_events([instance("e", "r1", available_time=T2), instance("e", "r2", available_time=T1)])
    assert result.events[0]["available_time"] == T1
    assert result.events[0]["quality_flags"] == [str(QualityFlag.AVAILABILITY_MERGED)]
    assert result.issues == []


def test_null_and_flag_keeps_nothing_and_quarantines_every_value_it_refused():
    """D-R4: two ends for one second-precise stay; the merge will not pick one."""
    result = merge_events([instance("e", "r1", end_time=T1), instance("e", "r2", end_time=T2),
                           instance("e", "r3", end_time=None)],
                          rules(end_time="null_and_flag"), dataset_id="d")
    survivor = result.events[0]
    assert survivor["end_time"] is None
    assert str(QualityFlag.VALUE_CONFLICT) in survivor["quality_flags"]
    assert [(q["source_row_id"], q["detail"]) for q in result.quarantine] == [
        ("r1", "end_time=2020-01-01 12:00:00"), ("r2", "end_time=2020-01-01 13:00:00")]
    assert {q["reason"] for q in result.quarantine} == {str(QuarantineReason.VALUE_CONFLICT)}
    assert {q["stage"] for q in result.quarantine} == {"canonical"}
    assert {q["dataset_id"] for q in result.quarantine} == {"d"}
    assert result.issues == []


def test_priority_takes_the_first_declared_value_present_case_insensitively():
    """D-R6: a problem list entry Active in one row and Resolved in another."""
    declared = rules(status={"rule": "priority", "order": ["Resolved", "Active"],
                             "conflict_when": [["Active", "Resolved"]]})
    result = merge_events([instance("e", "r1", status_source="active"), instance("e", "r2", status_source="Resolved")],
                          declared)
    survivor = result.events[0]
    assert survivor["status_source"] == "Resolved"
    assert str(QualityFlag.STATUS_CONFLICT) in survivor["quality_flags"]
    # only one of the pair present: the winner, no contradiction flagged
    calm = merge_events([instance("e", "r1", status_source="Active"), instance("e", "r2", status_source="ACTIVE ")],
                        declared)
    assert calm.events[0]["quality_flags"] == [] and calm.events[0]["status_source"] == "Active"


def test_a_rule_keyed_by_the_role_name_applies_to_the_field_the_role_feeds():
    result = merge_events([instance("e", "r1", status_source="Held"), instance("e", "r2", status_source="Given")],
                          rules(status={"rule": "priority", "order": ["Given"]}))
    assert result.events[0]["status_source"] == "Given"


def test_priority_with_none_of_the_declared_values_is_an_unresolved_conflict():
    result = merge_events([instance("e", "r1", status_source="Draft"), instance("e", "r2", status_source="Pending")],
                          rules(status={"rule": "priority", "order": ["Resolved"]}))
    assert result.events[0]["status_source"] == "Draft"
    assert str(QualityFlag.MERGE_CONFLICT) in result.events[0]["quality_flags"]
    assert len(result.issues) == 1 and "priority" in result.issues[0]["detail"]


def test_prefer_linked_keeps_the_one_encounter_seen_elsewhere():
    """D-R2: the same note under several encounter ids, one of which other tables know."""
    linked = rules(encounter_id="prefer_linked")
    rows = [instance("e", "r1", {"encounter_linked": "0"}, encounter_id="E1"),
            instance("e", "r2", {"encounter_linked": "1"}, encounter_id="E2"),
            instance("e", "r3", {"encounter_linked": "1"}, encounter_id="E2")]
    survivor = merge_events(rows, linked).events[0]
    assert survivor["encounter_id"] == "E2"
    assert str(QualityFlag.ENCOUNTER_FROM_LINKED_ROW) in survivor["quality_flags"]


def test_prefer_linked_with_none_or_several_linked_values_keeps_nothing():
    linked = rules(encounter_id="prefer_linked")
    none = merge_events([instance("e", "r1", {"encounter_linked": None}, encounter_id="E1"),
                         instance("e", "r2", {"encounter_linked": "no"}, encounter_id="E2")], linked).events[0]
    assert none["encounter_id"] is None
    assert str(QualityFlag.ENCOUNTER_UNLINKED) in none["quality_flags"]
    several = merge_events([instance("e", "r1", {"encounter_linked": "1"}, encounter_id="E1"),
                            instance("e", "r2", {"encounter_linked": "true"}, encounter_id="E2")], linked).events[0]
    assert several["encounter_id"] is None
    assert str(QualityFlag.ENCOUNTER_UNLINKED) in several["quality_flags"]


def test_keep_all_flag_keeps_the_survivor_and_marks_the_event():
    """D-R3: a facility and a professional bill for one service are one event."""
    declared = rules(procedure_name={"rule": "keep_all_flag", "flag": "BILLING_DUPLICATE"})
    rows = [instance("e", "r2", {"procedure_name": "PR ECHO"}, source_name="ECHO"),
            instance("e", "r1", {"procedure_name": "HB CHG ECHO"}, source_name="ECHO")]
    result = merge_events(rows, declared)
    survivor = result.events[0]
    assert survivor["quality_flags"] == [str(QualityFlag.BILLING_DUPLICATE)]
    assert result.issues == [] and result.quarantine == []
    assert INSTANCE_KEY not in survivor, "the payload never reaches the written row"


def test_an_undeclared_disagreement_is_a_recorded_conflict_never_a_silent_pick():
    rows = [instance("e", "r2", unit_source="mg/dL", value_text="lots of text"),
            instance("e", "r1", unit_source="mmol/L", value_text="other text")]
    result = merge_events(rows)
    survivor = result.events[0]
    assert survivor["unit_source"] == "mmol/L", "the smallest source row id survives"
    assert str(QualityFlag.MERGE_CONFLICT) in survivor["quality_flags"]
    issues = sorted(i["detail"] for i in result.issues)
    assert issues == ["unit_source: 2 distinct values across 2 merged rows",
                      "value_text: 2 distinct values across 2 merged rows"]
    assert all(i["issue_type"] == "MERGE_CONFLICT" and i["severity"] == "error" and i["stage"] == "canonical"
               and i["event_id"] == "e" and i["source_id"] == "src" for i in result.issues)
    assert "lots of text" not in " ".join(i["detail"] for i in result.issues)


def test_bookkeeping_fields_follow_the_survivor_and_never_conflict():
    rows = [instance("e", "r2", source_id="b", parent_event_id="p2", provenance_status="derived"),
            instance("e", "r1", source_id="a", parent_event_id=None, provenance_status="observed")]
    result = merge_events(rows)
    survivor = result.events[0]
    assert (survivor["source_id"], survivor["provenance_status"]) == ("a", "observed")
    assert survivor["parent_event_id"] is None, "bookkeeping is not filled across rows"
    assert result.issues == [] and survivor["quality_flags"] == []


def test_the_rules_of_the_surviving_row_source_apply_to_the_group():
    """Death events from two sources share an id; the survivor's source decides."""
    declared = {"a": {"end_time": MergeRuleSpec(rule="earliest")}, "b": {}}
    rows = [instance("e", "r2", source_id="b", end_time=T2), instance("e", "r1", source_id="a", end_time=T1)]
    assert merge_events(rows, declared).events[0]["end_time"] == T1
    rows = [instance("e", "r2", source_id="a", end_time=T2), instance("e", "r1", source_id="b", end_time=T1)]
    result = merge_events(rows, declared)
    assert str(QualityFlag.MERGE_CONFLICT) in result.events[0]["quality_flags"]


def test_the_merge_is_the_same_whatever_order_the_rows_arrive_in():
    declared = rules(available_time="earliest", status={"rule": "priority", "order": ["Resolved", "Active"],
                                                         "conflict_when": [["Active", "Resolved"]]},
                     encounter_id="prefer_linked")
    rows = [
        instance("e", "r1", {"encounter_linked": "0"}, available_time=T2, status_source="Active", encounter_id="E1", unit_source="a"),
        instance("e", "r2", {"encounter_linked": "1"}, available_time=T1, status_source="Resolved", encounter_id="E2", unit_source="b"),
        instance("e", "r3", {"encounter_linked": "0"}, available_time=T3, status_source="Active", encounter_id="E3", unit_source="c"),
        instance("f", "r4", available_time=T1),
    ]
    reference = merge_events(list(rows), declared)
    for seed in range(5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert merge_events(shuffled, declared) == reference
    assert reference.events[0]["unit_source"] == "a"
    assert reference.events[0]["encounter_id"] == "E2"
