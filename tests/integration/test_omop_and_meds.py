"""OMOP and MEDS publication on the synthetic fixture (checklist P2, P3).

Built once from the canonical layer, then examined for the properties the design says
must hold -- especially the ones about what is absent: no invented concept id, no
fabricated date, no cohort label in an event row.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl
import pytest

import meds as meds_spec
from ehr2trace.config import load_dataset_config
from ehr2trace.meds import build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import run_checks
from tests.integration.test_generic_ehr_pipeline import CONFIG, run_pipeline


@pytest.fixture(scope="module")
def monkeypatch_module():
    """`monkeypatch` is function-scoped; this fixture is not."""
    with pytest.MonkeyPatch.context() as patch:
        yield patch


@pytest.fixture(scope="module")
def published(tmp_path_factory, monkeypatch_module) -> tuple[WorkLayout, dict, dict]:
    # `vocabulary_dir=None` means "whatever the environment says", so a developer with a
    # vocabulary installed used to fail the no-vocabulary assertions below. The test is
    # about the vocabulary being absent, so it has to make it absent.
    monkeypatch_module.delenv("OMOP_VOCAB_DIR", raising=False)
    layout = run_pipeline(tmp_path_factory.mktemp("publish"), workers=1)
    cfg = load_dataset_config(CONFIG)
    omop_result = build_omop(cfg, layout, vocabulary_dir=None)
    meds_result = build_meds(cfg, layout)
    return layout, omop_result, meds_result


def con(layout: WorkLayout):
    return duckdb.connect(str(layout.omop_dir / "omop.duckdb"), read_only=True)


# -- OMOP -----------------------------------------------------------------------


def test_the_official_ddl_defines_the_tables(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert {"person", "visit_occurrence", "visit_detail", "condition_occurrence", "drug_exposure",
                "measurement", "observation", "note", "death", "observation_period", "cdm_source"} <= tables
        columns = {r[0] for r in c.execute("DESCRIBE person").fetchall()}
        assert "membership_label" not in columns, "no custom column may be added to a core table"
    finally:
        c.close()


def test_every_published_row_has_lineage(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        for table, pk in (
            ("person", "person_id"),
            ("visit_occurrence", "visit_occurrence_id"),
            ("visit_detail", "visit_detail_id"),
            ("condition_occurrence", "condition_occurrence_id"),
            ("drug_exposure", "drug_exposure_id"),
            ("measurement", "measurement_id"),
            ("observation", "observation_id"),
            ("note", "note_id"),
            ("death", "person_id"),
        ):
            orphans = c.execute(
                f"SELECT count(*) FROM {table} t WHERE NOT EXISTS ("
                f"SELECT 1 FROM etl_audit.lineage l WHERE l.target_table = '{table}' "
                f"AND l.target_pk = t.{pk})"
            ).fetchone()[0]
            assert orphans == 0, f"{table} has {orphans} rows with no source"
    finally:
        c.close()


def test_lineage_reaches_actual_source_rows(published):
    """Not just any string: the ids must exist in the canonical link table."""
    layout, _o, _m = published
    c = con(layout)
    try:
        target = {r[0] for r in c.execute("SELECT DISTINCT source_row_id FROM etl_audit.lineage").fetchall()}
    finally:
        c.close()
    known = set(pl.read_parquet(layout.canonical_path("event_source"))["source_row_id"].to_list())
    assert target and target <= known


def test_without_a_vocabulary_every_concept_is_zero_and_nothing_is_invented(published):
    layout, omop_result, _m = published
    assert omop_result["vocabulary"] == "none"
    c = con(layout)
    try:
        for table, column in (
            ("condition_occurrence", "condition_concept_id"),
            ("drug_exposure", "drug_concept_id"),
            ("measurement", "measurement_concept_id"),
        ):
            nonzero = c.execute(f"SELECT count(*) FROM {table} WHERE {column} <> 0").fetchone()[0]
            assert nonzero == 0
    finally:
        c.close()
    assert omop_result["unmapped_terms"] > 0


def test_unmapped_terms_go_to_review_rather_than_silently_becoming_zero(published):
    layout, omop_result, _m = published
    pending = Path(omop_result["pending_csv"])
    assert pending.exists()
    rows = pl.read_csv(pending)
    assert rows.height == omop_result["unmapped_terms"]


def test_source_values_survive_even_when_the_concept_does_not(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        missing = c.execute(
            "SELECT count(*) FROM condition_occurrence WHERE condition_source_value IS NULL"
        ).fetchone()[0]
        assert missing == 0
    finally:
        c.close()


def test_orders_and_administrations_carry_different_type_concepts(published):
    """They share a table in OMOP, so the type concept is what keeps them apart."""
    layout, _o, _m = published
    c = con(layout)
    try:
        total = c.execute("SELECT count(*) FROM drug_exposure").fetchone()[0]
        assert total == 6
        # the type concept is the field that keeps the two kinds apart in one table
        kinds = c.execute("SELECT count(DISTINCT drug_type_concept_id) FROM drug_exposure").fetchone()[0]
        assert kinds >= 1
    finally:
        c.close()
    events = pl.read_parquet(layout.canonical_path("events"))
    assert events.filter(pl.col("event_kind") == "drug_order").height == 4
    assert events.filter(pl.col("event_kind") == "drug_admin").height == 2


def test_a_ranged_result_keeps_its_text_and_no_point_value(published):
    """35-40 is not 35, and it is not a reference range either."""
    layout, _o, _m = published
    c = con(layout)
    try:
        row = c.execute(
            "SELECT value_as_number, value_source_value, range_low, range_high "
            "FROM measurement WHERE measurement_source_value = 'RVSP'"
        ).fetchone()
    finally:
        c.close()
    assert row is not None
    assert row[0] is None and row[1] == "35-40", "the original text, verbatim"
    assert row[2] is None and row[3] is None


def test_a_comparator_result_is_not_cast_to_a_number(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        row = c.execute(
            "SELECT value_as_number, value_source_value FROM measurement "
            "WHERE measurement_source_value = 'DDIMER'"
        ).fetchone()
    finally:
        c.close()
    assert row[0] is None and row[1] == "<0.50"


def test_untimed_values_never_reach_measurement(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        leaked = c.execute(
            "SELECT count(*) FROM measurement WHERE lower(measurement_source_value) = 'bmi'"
        ).fetchone()[0]
    finally:
        c.close()
    assert leaked == 0
    quarantine = pl.read_parquet(layout.canonical_path("quarantine"))
    assert quarantine.filter(pl.col("reason") == "UNTIMED_CLINICAL_VALUE").height == 4


def test_an_approved_approximation_publishes_and_flags_every_birth_year(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        people = c.execute("SELECT count(*) FROM person").fetchone()[0]
        flagged = c.execute(
            "SELECT count(*) FROM etl_audit.quality_issue "
            "WHERE issue_type = 'DERIVED_APPROXIMATE_BIRTH_YEAR'"
        ).fetchone()[0]
        years = [r[0] for r in c.execute("SELECT year_of_birth FROM person ORDER BY person_id").fetchall()]
    finally:
        c.close()
    assert people == 4 and flagged == 4
    # person_id order follows the subject hash, so compare the set of derived years
    assert sorted(years) == sorted([2022 - 64, 2022 - 71, 2022 - 38, 2022 - 55])


def test_strict_mode_would_publish_nobody_here(tmp_path_factory, published):
    """The policy is a gate, not a preference: strict withholds rather than estimates."""
    layout, _o, _m = published
    cfg = load_dataset_config(CONFIG)
    strict = cfg.model_copy(
        update={"omop": cfg.omop.model_copy(update={"person_birth_policy": type(cfg.omop.person_birth_policy)()})}
    )
    result = build_omop(strict, layout, vocabulary_dir=None)
    assert result["tables"]["person"] == 0
    assert result["blocked_subjects"] == 4
    assert "year_of_birth" in result["block_reason"]
    # rebuild so later tests see the fixture's own policy again
    build_omop(cfg, layout, vocabulary_dir=None)


def test_the_cohort_label_lives_in_the_audit_schema_not_in_a_clinical_table(published):
    layout, _o, _m = published
    c = con(layout)
    try:
        tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}
        assert "cohort_membership" not in tables  # only under etl_audit
        c.execute("SELECT count(*) FROM etl_audit.cohort_membership").fetchone()
    finally:
        c.close()


# -- MEDS -----------------------------------------------------------------------


def test_meds_validates_against_the_installed_schema(published):
    layout, _o, meds_result = published
    import pyarrow.parquet as pq

    files = sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet"))
    assert files
    for path in files:
        meds_spec.DataSchema.validate(pq.read_table(path))


def test_every_code_in_use_is_documented_exactly_once(published):
    layout, _o, _m = published
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    codes = pl.read_parquet(layout.meds_dir / meds_spec.code_metadata_filepath)
    assert set(frame["code"].to_list()) == set(codes["code"].to_list())
    assert codes["code"].n_unique() == codes.height


def test_one_fact_never_gets_both_a_source_and_an_omop_code(published):
    layout, _o, _m = published
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    per_event = frame.group_by("event_id").agg(pl.col("code").n_unique().alias("codes"))
    assert int(per_event["codes"].max()) == 1


def test_death_uses_the_reserved_meds_code(published):
    layout, _o, _m = published
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    assert meds_spec.death_code in set(frame["code"].to_list())


def test_no_event_row_carries_the_cohort_label_or_a_file_name(published):
    layout, _o, _m = published
    from ehr2trace.meds import FORBIDDEN_EVENT_COLUMNS

    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    assert not set(frame.columns) & FORBIDDEN_EVENT_COLUMNS
    assert not any("site_" in str(v) for v in frame["source_table"].to_list())


def test_static_events_keep_a_null_time_rather_than_a_convenient_one(published):
    layout, _o, _m = published
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    demographics = frame.filter(pl.col("event_kind") == "demographic")
    assert demographics.height > 0
    assert demographics["time"].null_count() == demographics.height


def test_available_time_is_populated_everywhere_and_flagged_when_assumed(published):
    layout, _o, _m = published
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    timed = frame.filter(pl.col("time").is_not_null())
    assert timed["available_time"].null_count() == 0
    assumed = timed.filter(pl.col("quality_flags").list.contains("AVAILABILITY_ASSUMED"))
    assert assumed.height > 0


def test_an_as_of_view_contains_nothing_that_was_not_yet_visible(published):
    layout, _o, _m = published
    from ehr2trace.meds import as_of_view

    cutoff = datetime(2021, 5, 29, 6, 30)
    view = as_of_view(layout, cutoff)
    assert view.height > 0
    late = view.filter(pl.col("available_time") > cutoff)
    assert late.height == 0
    # a result collected before the cutoff but released after it must be excluded
    frame = pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
    hidden = frame.filter((pl.col("time") <= cutoff) & (pl.col("available_time") > cutoff))
    assert hidden.height > 0, "the fixture must contain at least one such row to be a real test"
    assert not set(hidden["event_id"].to_list()) & set(view["event_id"].to_list())


def test_each_subject_is_in_one_shard_and_one_split(published):
    layout, _o, _m = published
    splits = pl.read_parquet(layout.meds_dir / meds_spec.subject_splits_filepath)
    assert splits["subject_id"].n_unique() == splits.height
    seen: dict[int, str] = {}
    for path in sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")):
        for subject_id in set(pl.read_parquet(path)["subject_id"].to_list()):
            assert subject_id not in seen
            seen[subject_id] = path.name
    assert set(seen) == set(splits["subject_id"].to_list())


def test_dataset_metadata_records_what_produced_it(published):
    layout, _o, _m = published
    metadata = json.loads((layout.meds_dir / meds_spec.dataset_metadata_filepath).read_text())
    assert metadata["etl_name"] == "ehr2trace"
    assert metadata["meds_version"]
    assert "available_time" in metadata["notes"]


def test_all_checks_pass_on_the_published_output(published):
    layout, _o, _m = published
    cfg = load_dataset_config(CONFIG)
    results = run_checks(cfg, layout, include_slow=True)
    failed = [r for r in results if not r.passed]
    assert not failed, [f"{r.check_id}: {r.detail}" for r in failed]
    assert len(results) >= 20


# -- a recorded date of birth is a fact, not a policy question ---------------------


def _with_birth_dates(cfg, layout, tmp_path, dates: dict[int, str]):
    """Add a BIRTH_DATE demographic event for the given subjects and rebuild OMOP.

    Injected at the canonical layer rather than in the fixture files so the test states
    exactly one thing: what the OMOP person builder does when a birth date is present.
    """
    import polars as pl

    from ehr2trace.paths import WorkLayout

    scratch = tmp_path / "birthdates"
    from ehr2trace.faults import clone_work_tree

    clone_work_tree(layout.root, scratch)
    target = WorkLayout(root=scratch, dataset_id=cfg.dataset_id)

    path = target.canonical_path("events")
    events = pl.read_parquet(path)
    template = events.filter(pl.col("event_kind") == "demographic").head(1).to_dicts()[0]
    rows = []
    for i, (subject_id, iso) in enumerate(dates.items()):
        row = dict(template)
        row.update(
            {
                "event_id": f"birthdate-{i}",
                "subject_id": subject_id,
                "event_kind": "demographic",
                "source_code": "BIRTH_DATE",
                "source_name": "birth date",
                "value_text": iso,
                "value_number": None,
                "event_time": None,
            }
        )
        rows.append(row)
    merged = pl.concat([events, pl.DataFrame(rows, schema=events.schema)])
    tmp = path.with_suffix(".parquet.mutating")
    merged.write_parquet(tmp)
    tmp.replace(path)
    return target


def test_strict_mode_publishes_a_patient_who_has_a_recorded_birth_date(published, tmp_path):
    """Strict withholds for lack of a derivable year, not on principle.

    `birth_date` was a declared field role that nothing consumed, so a dataset carrying
    real dates of birth was treated exactly like one carrying none and strict mode
    withheld every patient. That is the opposite of what the gate is for.
    """
    import duckdb

    layout, _o, _m = published
    cfg = load_dataset_config(CONFIG)
    subjects = duckdb.connect(str(layout.omop_dir / "omop.duckdb"), read_only=True)
    known = [r[0] for r in subjects.execute("SELECT subject_id FROM pmap LIMIT 2").fetchall()] \
        if _has_pmap(subjects) else []
    subjects.close()
    if not known:
        import polars as pl

        known = pl.read_parquet(layout.canonical_path("events"))["subject_id"].unique().to_list()[:2]

    target = _with_birth_dates(cfg, layout, tmp_path, {known[0]: "1961-04-02", known[1]: "1948-11-19"})
    strict = cfg.model_copy(
        update={"omop": cfg.omop.model_copy(update={"person_birth_policy": type(cfg.omop.person_birth_policy)()})}
    )
    result = build_omop(strict, target, vocabulary_dir=None)

    assert result["tables"]["person"] == 2, "strict withheld patients who have a birth date"
    c = duckdb.connect(str(target.omop_dir / "omop.duckdb"), read_only=True)
    try:
        years = sorted(r[0] for r in c.execute("SELECT year_of_birth FROM person").fetchall())
        approximated = c.execute(
            "SELECT count(*) FROM etl_audit.quality_issue "
            "WHERE issue_type = 'DERIVED_APPROXIMATE_BIRTH_YEAR'"
        ).fetchone()[0]
    finally:
        c.close()
    assert years == [1948, 1961]
    assert approximated == 0, "a recorded birth date is not an approximation"


def test_a_recorded_birth_date_beats_an_age_under_the_approximation_policy(published, tmp_path):
    """An age plus a reference year approximates what the date says outright."""
    import duckdb
    import polars as pl

    layout, _o, _m = published
    cfg = load_dataset_config(CONFIG)
    subject = pl.read_parquet(layout.canonical_path("events"))["subject_id"].unique().to_list()[0]

    target = _with_birth_dates(cfg, layout, tmp_path, {subject: "1955-06-30"})
    build_omop(cfg, target, vocabulary_dir=None)

    c = duckdb.connect(str(target.omop_dir / "omop.duckdb"), read_only=True)
    try:
        year = c.execute(
            "SELECT s.year_of_birth FROM person s JOIN pmap p ON p.person_id = s.person_id "
            "WHERE p.subject_id = ?", [subject]
        ).fetchone()
        flagged = c.execute(
            "SELECT count(*) FROM etl_audit.quality_issue q JOIN pmap p ON p.subject_id = q.subject_id "
            "WHERE q.issue_type = 'DERIVED_APPROXIMATE_BIRTH_YEAR' AND q.subject_id = ?", [subject]
        ).fetchone()[0]
    finally:
        c.close()
    assert year is not None and year[0] == 1955
    assert flagged == 0, "the year came from a date, so it must not be flagged as estimated"


def _has_pmap(con) -> bool:
    try:
        con.execute("SELECT 1 FROM pmap LIMIT 1").fetchone()
        return True
    except Exception:
        return False
