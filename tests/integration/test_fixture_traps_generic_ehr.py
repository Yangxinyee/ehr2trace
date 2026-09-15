"""The fixture traps of remediation plan T1.12 that live in the generic fixture.

The generic fixture is hand-written CSV (``tests/fixtures/generic_ehr/``), so a trap here
is rows and columns written for it, settled by declarations in
``datasets/generic_ehr.yaml``. As with the shape fixture's traps
(``test_fixture_traps_ctpe_shape.py``), each is asserted on the fixture built with its own
declarations and on the same raw files without the declaration that settles it; where a
fault in ``ehr2trace.faults`` injects the same damage into a finished build, the test
names it.

A declaration that only tells the checks what to expect -- ``expected_empty``,
``ignored_columns`` -- is not part of the configuration hash, so a build without it is
this build byte for byte. Those traps judge the one build twice, with and without the
declaration, and assert that the hash agrees.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from ehr2trace.config import load_dataset_config
from ehr2trace.schema import QualityFlag, QuarantineReason
from tests.integration.test_generic_ehr_pipeline import CONFIG, FIXTURE
from tests.integration.trap_builds import Build, build, edited


@pytest.fixture(scope="module")
def after(tmp_path_factory) -> Build:
    return build(CONFIG, FIXTURE, tmp_path_factory.mktemp("generic_traps_after"))


@pytest.fixture(scope="module")
def before(tmp_path_factory) -> Build:
    """The same raw files, built without the declarations that change what a build produces.

    One build carries every such removal. Each touches a different source, and each test
    below reads only its own trap's source.
    """
    cfg = load_dataset_config(CONFIG)
    # Trap 4: the temperatures without their unit override.
    cfg = edited(cfg, "results", unit_override={})
    # Trap 5: the notes with their encounter id back in the identity.
    cfg = edited(cfg, "notes", encounter_in_identity=True)
    # Trap 6: the procedures without the rule for their bill type.
    cfg = edited(cfg, "procedures", merge_rules={})
    # Trap 7: the intensive-care stays without their length of stay in the identity.
    cfg = edited(cfg, "icu_stays", identity_extra_fields=[])
    return build(cfg, FIXTURE, tmp_path_factory.mktemp("generic_traps_before"), publish=False)


@pytest.fixture(scope="module")
def before_rule(tmp_path_factory) -> Build:
    """Trap 5's other declaration removed alone: the encounter out of the identity, no rule to choose one."""
    cfg = edited(load_dataset_config(CONFIG), "notes", merge_rules={})
    return build(cfg, FIXTURE, tmp_path_factory.mktemp("generic_traps_before_rule"), publish=False)


def _flagged(frame: pl.DataFrame, flag: QualityFlag) -> list[bool]:
    return [str(flag) in flags for flags in frame["quality_flags"].to_list()]


def _links_of(b: Build, event_ids: list[str]) -> pl.DataFrame:
    return b.canonical("event_source").filter(pl.col("event_id").is_in(event_ids))


def _manifest(b: Build) -> dict:
    return json.loads((b.layout.manifest_dir / "inputs.json").read_text(encoding="utf-8"))


# -- trap 2: a wide vital-sign table declared as components ------------------------------------


def test_a_wide_table_read_as_components_yields_nothing_and_declares_it(after):
    """Trap 2 (P-M2). Fault SOURCE_PARSED_ROWS_AND_YIELDED_NOTHING covers the same ground on a built layer.

    The table holds one column per vital sign and no component name, so every parsed
    row is quarantined for lacking one and no event comes from it. The source declares
    that, so the check reports it as expected rather than silent.
    """
    parsed = after.source_rows("vitals_wide")
    assert parsed.height == 3, "the fixture's three wide rows were read"
    assert after.canonical().filter(pl.col("source_id") == "vitals_wide").is_empty()

    quarantined = after.canonical("quarantine").filter(pl.col("source_id") == "vitals_wide")
    assert set(quarantined["reason"].to_list()) == {str(QuarantineReason.UNPARSEABLE_VALUE)}
    assert set(quarantined["source_row_id"].to_list()) == set(parsed["source_row_id"].to_list())

    result = after.checks("SOURCE_YIELDS_EVENTS")["SOURCE_YIELDS_EVENTS"]
    assert result.passed, result.detail
    assert result.metrics["declared_empty"] == ["site_a/vitals_wide"]


def test_without_expected_empty_the_silent_source_fails(after):
    """Trap 2, before: the same build judged without ``expected_empty``."""
    undeclared = edited(after.cfg, "vitals_wide", expected_empty=None)
    assert undeclared.config_hash() == after.cfg.config_hash(), "a declaration changes no produced byte"

    result = after.checks("SOURCE_YIELDS_EVENTS", cfg=undeclared)["SOURCE_YIELDS_EVENTS"]
    assert not result.passed
    assert result.metrics["silent"] == ["site_a/vitals_wide (3 rows parsed)"]


# -- trap 4: Fahrenheit readings under a Celsius label -------------------------------------------


def _temperatures(b: Build) -> dict[float, dict]:
    rows = b.canonical().filter((pl.col("source_id") == "results") & (pl.col("source_code") == "TEMP"))
    return {row["value_number"]: row for row in rows.iter_rows(named=True)}


def test_a_temperature_labelled_celsius_is_normalized_as_the_fahrenheit_it_is(after):
    """Trap 4 (P-C4, P-CU2, D-R1). Fault VALUE_IN_A_DIFFERENT_UNIT_THAN_ITS_LABEL covers the same ground on a built layer.

    Every temperature row says ``degree Celsius``; two hold Fahrenheit readings and one a
    Celsius reading. The override reads the code's values as [degF]: the Fahrenheit
    readings convert to body temperatures, and the Celsius one converts to a temperature
    nobody has, so it is withheld from the normalized column as implausible. The source's
    label and value stay as written.
    """
    temperatures = _temperatures(after)
    assert set(temperatures) == {98.6, 101.3, 37.2}
    for row in temperatures.values():
        assert (row["unit_source"], row["unit_normalized"]) == ("degree Celsius", "Cel")
        assert str(QualityFlag.UNIT_OVERRIDDEN) in row["quality_flags"]
    assert temperatures[98.6]["value_number_normalized"] == 37.0
    assert temperatures[101.3]["value_number_normalized"] == 38.5
    assert str(QualityFlag.IMPLAUSIBLE) not in temperatures[98.6]["quality_flags"]
    misfiled = temperatures[37.2]
    assert misfiled["value_number_normalized"] is None
    assert str(QualityFlag.IMPLAUSIBLE) in misfiled["quality_flags"]

    result = after.checks("UNIT_VALUE_PLAUSIBLE")["UNIT_VALUE_PLAUSIBLE"]
    assert result.passed, result.detail


def test_without_the_override_real_temperatures_are_withheld_and_the_misfiled_one_published(before):
    """Trap 4, before: the same rows built without ``unit_override``.

    Read as the Celsius their label claims, both Fahrenheit readings are implausible and
    neither reaches the normalized column, while the one reading that really was Celsius
    is the only temperature published. Nothing says a unit was overridden.
    """
    temperatures = _temperatures(before)
    for value in (98.6, 101.3):
        row = temperatures[value]
        assert row["unit_normalized"] == "Cel" and row["value_number_normalized"] is None
        assert str(QualityFlag.IMPLAUSIBLE) in row["quality_flags"]
    assert temperatures[37.2]["value_number_normalized"] == 37.2
    assert not any(str(QualityFlag.UNIT_OVERRIDDEN) in row["quality_flags"] for row in temperatures.values())


# -- trap 5: one note filed under two encounter ids (D-R2) --------------------------------------


def _notes(b: Build, patient: str) -> pl.DataFrame:
    return b.canonical().filter((pl.col("source_id") == "notes") & (pl.col("subject_id") == b.subject(patient)))


def test_one_note_under_two_encounter_ids_is_one_note_under_the_encounter_other_tables_know(after):
    """Trap 5 (P-CU4, P-CU12, D-R2). Fault ONE_NOTE_UNDER_TWO_ENCOUNTERS covers the same ground on a built layer.

    PX-1's progress note is filed under V-1001, the admission the encounters table
    carries, and under V-1777, which no table carries; only the first row is marked as
    seen elsewhere. PX-2's nursing note is filed under two ids no table carries. Each is
    one note with both rows linked: the first keeps V-1001 and says it came from the linked
    row, the second keeps no encounter and says it is unlinked.
    """
    linked = _notes(after, "PX-1")
    assert linked["encounter_id"].to_list() == ["V-1001"]
    assert _flagged(linked, QualityFlag.ENCOUNTER_FROM_LINKED_ROW) == [True]
    assert _links_of(after, linked["event_id"].to_list()).height == 2

    unlinked = _notes(after, "PX-2")
    assert unlinked["encounter_id"].to_list() == [None]
    assert _flagged(unlinked, QualityFlag.ENCOUNTER_UNLINKED) == [True]
    assert _links_of(after, unlinked["event_id"].to_list()).height == 2

    results = after.checks("NOTE_TEXT_UNIQUE", "DUPLICATES_AGREE", "ENCOUNTER_RESOLVES")
    for check_id, result in results.items():
        assert result.passed, f"{check_id}: {result.detail}"
    assert results["NOTE_TEXT_UNIQUE"].metrics["per_source"]["notes"]["exact_duplicate_groups"] == 0


def test_with_the_encounter_in_the_identity_the_note_is_published_once_per_id(before):
    """Trap 5, before: the notes built with ``encounter_in_identity`` at its default."""
    assert sorted(_notes(before, "PX-1")["encounter_id"].to_list()) == ["V-1001", "V-1777"]
    assert _notes(before, "PX-2").height == 2
    result = before.checks("NOTE_TEXT_UNIQUE")["NOTE_TEXT_UNIQUE"]
    assert not result.passed
    assert result.metrics["per_source"]["notes"]["exact_duplicate_groups"] == 2


def test_without_a_rule_for_the_encounter_the_merged_note_is_a_conflict(before_rule):
    """Trap 5, before, the other declaration: one note each, and nothing to say which encounter it keeps."""
    notes = pl.concat([_notes(before_rule, "PX-1"), _notes(before_rule, "PX-2")])
    assert notes.height == 2
    assert all(_flagged(notes, QualityFlag.MERGE_CONFLICT))
    result = before_rule.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert not result.passed
    assert result.metrics["per_source"]["notes"]["disagreements"] == {"encounter_id": 2}


# -- trap 6: one procedure billed twice (D-R3) --------------------------------------------------


def _procedures(b: Build, patient: str) -> pl.DataFrame:
    return b.canonical().filter((pl.col("source_id") == "procedures") & (pl.col("subject_id") == b.subject(patient)))


def test_a_procedure_billed_by_facility_and_professional_is_one_procedure(after):
    """Trap 6 (P-CU7, D-R3). No fault injects this.

    PX-1's CT angiogram is billed once by the facility and once by the professional. It is
    one procedure published once, both bills stay in its lineage, and the event is flagged
    BILLING_DUPLICATE; PX-2's procedure, billed once, is not flagged.
    """
    billed_twice = _procedures(after, "PX-1")
    assert billed_twice.height == 1
    assert _flagged(billed_twice, QualityFlag.BILLING_DUPLICATE) == [True]
    assert _links_of(after, billed_twice["event_id"].to_list()).height == 2
    assert _flagged(_procedures(after, "PX-2"), QualityFlag.BILLING_DUPLICATE) == [False]

    published = after.omop(
        "SELECT count(DISTINCT target_table || ':' || CAST(target_pk AS VARCHAR)) FROM etl_audit.lineage "
        "WHERE event_id = ?",
        [billed_twice["event_id"][0]],
    )
    assert published == [(1,)], "one published row, not one per bill"

    result = after.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert result.passed, result.detail
    assert result.metrics["per_source"]["procedures"]["ruled_disagreements"] == {"bill_type": 1}


def test_without_the_billing_rule_the_two_bills_disagree_unannounced(before):
    """Trap 6, before: the procedures built without their ``keep_all_flag`` rule."""
    billed_twice = _procedures(before, "PX-1")
    assert _flagged(billed_twice, QualityFlag.BILLING_DUPLICATE) == [False]
    result = before.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert not result.passed
    assert result.metrics["per_source"]["procedures"]["disagreements"] == {"column:bill_type": 1}


# -- trap 7: two intensive-care stays that began on the same day (D-R4) ---------------------------


def _stays(b: Build) -> pl.DataFrame:
    return b.canonical().filter((pl.col("source_id") == "icu_stays") & (pl.col("event_kind") == "visit_detail"))


def test_two_stays_that_began_the_same_day_are_two_visit_details_under_their_admission(after):
    """Trap 7 (P-CU6, D-R4). No fault injects this.

    PX-1 entered intensive care twice on 5 January 2021, for a quarter of a day and for a
    day and three quarters. They are two stays, each ending when its length says, and each
    is published under the admission whose span contains its start, since the table names
    no encounter to place it by.
    """
    from datetime import datetime

    stays = _stays(after).sort("end_time")
    assert stays["event_time"].to_list() == [datetime(2021, 1, 5)] * 2
    assert stays["end_time"].to_list() == [datetime(2021, 1, 5, 6), datetime(2021, 1, 6, 18)]
    assert all(_flagged(stays, QualityFlag.DERIVED_END_TIME))

    parents = after.omop(
        "SELECT DISTINCT d.visit_detail_id, d.visit_occurrence_id FROM visit_detail d "
        "JOIN etl_audit.lineage l ON l.target_table = 'visit_detail' AND l.target_pk = d.visit_detail_id "
        "WHERE list_contains(?, l.event_id)",
        [stays["event_id"].to_list()],
    )
    admission = after.omop("SELECT visit_occurrence_id FROM visit_occurrence WHERE visit_source_value = 'V-1001'")
    assert len(parents) == 2 and {visit for _detail, visit in parents} == {admission[0][0]}
    assert not after.omop("SELECT 1 FROM etl_audit.quality_issue WHERE issue_type = 'VISIT_DETAIL_UNPARENTED'")

    result = after.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert result.passed, result.detail


def test_without_the_length_of_stay_in_the_identity_two_stays_are_one_that_contradicts_itself(before):
    """Trap 7, before: the stays built without ``identity_extra_fields``."""
    stays = _stays(before)
    assert stays.height == 1
    assert _flagged(stays, QualityFlag.MERGE_CONFLICT) == [True]
    issues = before.canonical("quality_issue").filter(
        (pl.col("issue_type") == str(QualityFlag.MERGE_CONFLICT)) & (pl.col("source_id") == "icu_stays")
    )
    assert issues["detail"].to_list() == ["end_time: 2 distinct values across 2 merged rows"]
    result = before.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert not result.passed
    assert result.metrics["per_source"]["icu_stays"]["disagreements"] == {"length_of_stay": 1}


# -- trap 11: a delivered column that no role reads ---------------------------------------------


def test_a_delivered_column_no_role_reads_is_declared_with_its_reason(after):
    """Trap 11 (P-C10). Fault RAW_COLUMN_NEVER_DECLARED covers the same ground on a built manifest."""
    delivered = {c for u in _manifest(after)["inputs"] if u["source_id"] == "orders" for c in u["columns"]}
    assert "order_channel" in delivered, "the fixture must deliver the column"

    result = after.checks("RAW_COVERAGE_DECLARED")["RAW_COVERAGE_DECLARED"]
    assert result.passed, result.detail
    orders = result.metrics["columns"]["orders"]
    assert orders["undeclared"] == [] and orders["ignored_with_reason"] == 1


def test_without_the_ignored_column_raw_coverage_fails(after):
    """Trap 11, before: the same build judged without the ``ignored_columns`` entry."""
    undeclared = edited(after.cfg, "orders", ignored_columns={})
    assert undeclared.config_hash() == after.cfg.config_hash(), "a declaration changes no produced byte"

    result = after.checks("RAW_COVERAGE_DECLARED", cfg=undeclared)["RAW_COVERAGE_DECLARED"]
    assert not result.passed
    assert result.metrics["columns"]["orders"]["undeclared"] == ["order_channel"]
