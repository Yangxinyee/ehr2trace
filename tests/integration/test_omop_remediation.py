"""The OMOP half of the remediation plan of 2026-09-13 (T1.4, T1.5, T1.8, T1.13;
decisions D-R11, D-R14, D-R15), on the hand-built canonical layer.

See :mod:`tests.integration.hand_built_layer` for why the layer is written by hand.
"""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from ehr2trace import omop as omop_module
from ehr2trace.omop import SIG_LIMIT, build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind
from tests.integration.hand_built_layer import (
    CROSS_DAY,
    SAME_DAY,
    SINGLE,
    T,
    WARD,
    config,
    demographics,
    drug_row,
    event,
    lineage_of,
    one,
    query,
    the_layer,
    write_canonical,
)
from tests.unit.test_unpunctuated_codes import write_vocab

#: the columns canonical schema version 2 added; a file without them is a version-1 file
VERSION_TWO_COLUMNS = (
    "value_number_normalized", "unit_normalized", "rate_source", "rate", "rate_unit",
    "action", "discharged_to",
)


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> tuple[WorkLayout, dict]:
    layout = write_canonical(tmp_path_factory.mktemp("hand"), the_layer())
    return layout, build_omop(config(), layout, vocabulary_dir=None)


# -- T1.4 dose unit and rate (D-R15) -------------------------------------------------


def test_the_dose_unit_falls_back_to_the_events_own_unit(published):
    """A source that keeps the unit in its own column used to publish every dose without one."""
    layout, _r = published
    assert drug_row(layout, "order-unit-in-dose")[:2] == (5.0, "mg")
    assert drug_row(layout, "order-unit-in-column")[:2] == (5.0, "mg")


def test_a_rate_is_written_into_sig_in_the_fixed_format(published):
    layout, _r = published
    assert drug_row(layout, "order-with-rate") == (5.0, "mg", "5 mg; rate 10 mL/hr")
    assert drug_row(layout, "order-rate-only")[2] == "rate 1.5"


def test_without_a_rate_sig_holds_only_an_unparsed_dose(published):
    layout, _r = published
    assert drug_row(layout, "order-unit-in-dose")[2] is None
    assert drug_row(layout, "order-text-dose") == (None, None, "one tablet")


def test_sig_with_a_rate_is_cut_at_the_column_width(published):
    layout, _r = published
    sig = drug_row(layout, "order-long-with-rate")[2]
    assert len(sig) == SIG_LIMIT == 250
    assert sig == "x" * SIG_LIMIT


# -- T1.5 unit concepts (P-C3) ---------------------------------------------------------


def test_without_a_vocabulary_a_unit_is_concept_zero_and_still_in_the_lookup(published):
    layout, _r = published
    concept, source = one(
        layout,
        "SELECT unit_concept_id, unit_source_value FROM measurement WHERE measurement_source_value = 'CREAT'",
    )
    assert (concept, source) == (0, "mg/dL")
    assert query(layout, "SELECT unit_source, unit_normalized, concept_id FROM unit_map "
                         "WHERE unit_source = 'mg/dL'") == [("mg/dL", "mg/dL", 0)]


def test_a_unit_the_vocabulary_knows_gets_its_ucum_concept_and_only_in_the_unit_domain(tmp_path):
    vocab = write_vocab(
        tmp_path / "v",
        "8840\tmilligram per deciliter\tUnit\tUCUM\tUnit\tS\tmg/dL\t19700101\t20991231\t\n"
        "9999\tnot a unit\tObservation\tUCUM\tUnit\tS\tzz\t19700101\t20991231\t\n",
    )
    layout = write_canonical(tmp_path, [
        *demographics(WARD),
        event("known", WARD, EventKind.measurement, source_code="CREAT", event_time=T(2020, 3, 1),
              value_number=1.0, unit_source="MG/DL", unit_normalized="mg/dL"),
        event("wrong-domain", WARD, EventKind.measurement, source_code="ZZ", event_time=T(2020, 3, 1),
              value_number=1.0, unit_source="zz", unit_normalized="zz"),
    ])
    build_omop(config(), layout, vocabulary_dir=vocab)
    rows = dict(query(layout, "SELECT measurement_source_value, unit_concept_id FROM measurement"))
    assert rows == {"CREAT": 8840, "ZZ": 0}


# -- T1.8 death (D-R11) ------------------------------------------------------------------


def test_two_deaths_on_the_same_local_date_publish_one_row_with_the_timed_one(published):
    layout, _r = published
    _date, death_datetime = one(
        layout,
        "SELECT d.death_date, d.death_datetime FROM death d JOIN pmap p ON p.person_id = d.person_id "
        "WHERE p.subject_id = ?", [SAME_DAY],
    )
    assert death_datetime == T(2020, 1, 1, 21, 30), "the time of day beats local midnight, on the site's clock"
    assert lineage_of(layout, "death", "death-timed") and not lineage_of(layout, "death", "death-date-only")
    assert not query(layout, "SELECT 1 FROM etl_audit.quality_issue "
                             "WHERE issue_type = 'DEATH_DATE_CONFLICT' AND subject_id = ?", [SAME_DAY])


def test_deaths_on_different_local_dates_publish_nothing_and_are_reported(published):
    layout, _r = published
    assert not query(layout, "SELECT 1 FROM death d JOIN pmap p ON p.person_id = d.person_id "
                             "WHERE p.subject_id = ?", [CROSS_DAY])
    assert query(layout, "SELECT count(*) FROM etl_audit.quality_issue "
                         "WHERE issue_type = 'DEATH_DATE_CONFLICT' AND subject_id = ?", [CROSS_DAY]) == [(1,)]


def test_a_single_death_publishes_as_before(published):
    layout, result = published
    assert query(layout, "SELECT 1 FROM death d JOIN pmap p ON p.person_id = d.person_id "
                         "WHERE p.subject_id = ?", [SINGLE]) == [(1,)]
    assert result["tables"]["death"] == 2


def test_the_same_pair_read_on_the_utc_calendar_would_have_been_a_conflict(tmp_path):
    """Why the comparison is on the local date: the timed death is on the next UTC day."""
    layout = write_canonical(tmp_path, [e for e in the_layer() if e["subject_id"] == SAME_DAY])
    build_omop(config(zone="UTC"), layout, vocabulary_dir=None)
    assert query(layout, "SELECT count(*) FROM death") == [(0,)]
    assert query(layout, "SELECT count(*) FROM etl_audit.quality_issue "
                         "WHERE issue_type = 'DEATH_DATE_CONFLICT'") == [(1,)]


def test_the_session_clock_is_pinned_to_utc(tmp_path, monkeypatch):
    """The machine's zone must not reach any zoned cast the build makes."""
    seen: dict[str, str] = {}
    real = omop_module._connect

    def spy(path):
        con = real(path)
        seen["zone"] = con.execute("SELECT current_setting('TimeZone')").fetchone()[0]
        return con

    monkeypatch.setattr(omop_module, "_connect", spy)
    layout = write_canonical(tmp_path, [e for e in the_layer() if e["subject_id"] == SINGLE])
    build_omop(config(), layout, vocabulary_dir=None)
    assert seen["zone"] == "UTC"


# -- T1.13 visit_detail (D-R14) ------------------------------------------------------------


def visit_id(layout: WorkLayout, source_value: str) -> int:
    return one(layout, "SELECT visit_occurrence_id FROM visit_occurrence WHERE visit_source_value = ?",
               [source_value])[0]


def detail_parent(layout: WorkLayout, event_id: str) -> int:
    (pk,) = lineage_of(layout, "visit_detail", event_id)
    return one(layout, "SELECT visit_occurrence_id FROM visit_detail WHERE visit_detail_id = ?", [pk])[0]


def test_a_detail_is_parented_by_the_visit_carrying_its_encounter(published):
    layout, _r = published
    admission = visit_id(layout, "ENC-1")
    assert detail_parent(layout, "detail-by-encounter") == admission
    # the ICU visit also contains this one's start; the encounter id still decides
    assert detail_parent(layout, "detail-encounter-wins") == admission


def test_a_detail_without_a_matching_encounter_is_parented_by_the_innermost_containing_visit(published):
    layout, _r = published
    assert detail_parent(layout, "detail-nested") == visit_id(layout, "ENC-2")
    assert detail_parent(layout, "detail-unknown-encounter") == visit_id(layout, "ENC-1")


def test_a_detail_nothing_can_place_is_withheld_and_reported(published):
    layout, result = published
    for orphan in ("detail-orphan", "detail-no-visits"):
        assert not lineage_of(layout, "visit_detail", orphan)
    reported = sorted(r[0] for r in query(
        layout, "SELECT event_id FROM etl_audit.quality_issue WHERE issue_type = 'VISIT_DETAIL_UNPARENTED'"))
    assert reported == ["detail-no-visits", "detail-orphan"]
    assert result["tables"]["visit_detail"] == 4
    orphans = query(layout, "SELECT count(*) FROM visit_detail v WHERE NOT EXISTS ("
                            "SELECT 1 FROM etl_audit.lineage l WHERE l.target_table = 'visit_detail' "
                            "AND l.target_pk = v.visit_detail_id)")
    assert orphans == [(0,)]


def test_a_detail_carries_what_the_source_said(published):
    layout, _r = published
    (pk,) = lineage_of(layout, "visit_detail", "detail-by-encounter")
    row = one(layout, "SELECT visit_detail_concept_id, visit_detail_source_value, discharged_to_source_value, "
                      "discharged_to_concept_id, visit_detail_start_datetime, visit_detail_end_date, "
                      "parent_visit_detail_id FROM visit_detail WHERE visit_detail_id = ?", [pk])
    assert row == (0, "WARD_A", "ICU", 0, T(2020, 3, 1, 7), T(2020, 3, 1).date(), None)


def test_a_visit_records_where_it_discharged_to(published):
    layout, _r = published
    assert one(layout, "SELECT discharged_to_source_value, discharged_to_concept_id FROM visit_occurrence "
                       "WHERE visit_source_value = 'ENC-1'") == ("HOME", 0)


def test_an_unmapped_observation_lands_in_observation(published):
    layout, _r = published
    assert one(layout, "SELECT observation_concept_id, observation_source_value, value_as_string "
                       "FROM observation") == (0, "FOLLOWUP", "alive")
    assert lineage_of(layout, "observation", "followup")


# -- an older canonical file ---------------------------------------------------------------


def test_a_canonical_file_without_the_version_two_columns_still_publishes(tmp_path):
    """A layer written under schema version 1 is a valid input: the new columns read as nulls."""
    layout = write_canonical(
        tmp_path, [e for e in the_layer() if e["subject_id"] == WARD], drop_columns=VERSION_TWO_COLUMNS
    )
    assert not set(VERSION_TWO_COLUMNS) & set(pq.read_schema(layout.canonical_path("events")).names)
    result = build_omop(config(), layout, vocabulary_dir=None)
    assert result["tables"]["visit_detail"] == 4 and result["tables"]["drug_exposure"] == 6
    assert drug_row(layout, "order-with-rate") == (5.0, "mg", None), "no rate column, no rate in sig"
    assert one(layout, "SELECT unit_concept_id FROM measurement WHERE measurement_source_value = 'CREAT'") == (0,)
    assert one(layout, "SELECT discharged_to_source_value FROM visit_occurrence "
                       "WHERE visit_source_value = 'ENC-1'") == (None,)
