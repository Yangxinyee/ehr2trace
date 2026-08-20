"""Every structural trap in the reference export, on fabricated data (checklist P0-7).

The real export's traps are the kind that fail silently: a byte-order mark present in
one file of a source and absent in another, a table repeated once per extraction
anchor, a workbook that types a date as text where its sibling types it as a date.
None of them raise. All of them corrupt the output.

`tests/fixtures/ctpe_shape/` reproduces every one of them with invented patients and
invented values, so the regression can live in the repository. The real three-patient
baselines are asserted separately, against the real data, in `test_ctpe_baselines.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
import pytest

from ehr2cdm.canonical.build import merge_buckets, plan_canonical, plan_stage, run_canonical_task, run_stage_task
from ehr2cdm.config import load_dataset_config
from ehr2cdm.identity import build_identity
from ehr2cdm.ingest import plan_ingest, run_ingest_task, write_manifest
from ehr2cdm.paths import WorkLayout
from ehr2cdm.run import execute
from ehr2cdm.schema import EventKind, QualityFlag, QuarantineReason

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ctpe_shape"
CONFIG = Path(__file__).resolve().parents[2] / "datasets" / "ctpe_shape.yaml"

#: The patient who appears in both a "has" and a "no" partition of the same batch.
CROSS_LABEL_PATIENT = "SUBJ-2"
EXPECTED_ANCHOR_DATES = 4
#: One report is seven lines, repeated in full under every anchor.
REPORT_LINES = 7


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[WorkLayout, object]:
    os.environ["CTPE_SHAPE_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=tmp_path_factory.mktemp("shape") / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()
    results = execute(plan_ingest(cfg, layout), run_ingest_task, 1)
    write_manifest(layout, cfg, results)
    build_identity(cfg, layout)
    execute(plan_stage(cfg, layout), run_stage_task, 1)
    tasks = plan_canonical(cfg, layout, CONFIG, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, 1)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    return layout, cfg


@pytest.fixture(scope="module")
def events(built) -> pl.DataFrame:
    return pl.read_parquet(built[0].canonical_path("events"))


def subject_of(cfg, patient: str) -> int:
    from ehr2cdm.hashing import subject_id_from_person_key

    return subject_id_from_person_key(cfg.dataset_id, patient, cfg.subject_salt())


# -- discovery -------------------------------------------------------------------


def test_the_fixture_has_the_same_shape_as_the_real_export(built):
    """25 physical files, 21 text sources, 19 sheets: the counts the design measured."""
    from ehr2cdm.discover import inspect

    report = inspect(built[1], compute_hashes=False)
    assert report.counts["physical_files"] == 25
    assert report.counts["logical_sources_text"] == 21
    assert report.counts["logical_sources_sheets"] == 19


def test_an_inconsistent_byte_order_mark_does_not_change_a_column_name(built):
    from ehr2cdm.discover import inspect

    report = inspect(built[1], compute_hashes=False)
    text_files = [f for f in report.files if f.kind == "text"]
    assert sum(1 for f in text_files if f.bom) == 19
    assert sum(1 for f in text_files if not f.bom) == 2
    problem_lists = [s for s in report.sources if s.source_id == "problem_list"]
    assert len({u.columns[0] for s in problem_lists for u in s.units}) == 1


def test_both_sheet_naming_variants_resolve(built):
    from ehr2cdm.discover import inspect

    report = inspect(built[1], compute_hashes=False)
    for source_id in ("pft_narrative", "pft_values"):
        present = [s for s in report.sources if s.source_id == source_id and s.units]
        assert len(present) == 4, source_id


def test_one_logical_source_arrives_as_a_file_and_as_a_sheet(built):
    from ehr2cdm.discover import inspect

    report = inspect(built[1], compute_hashes=False)
    adapters = {u.adapter for s in report.sources if s.source_id == "medication_admin" for u in s.units}
    assert adapters == {"delimited", "excel"}


def test_the_unnamed_leading_column_is_skipped_positionally(built):
    from ehr2cdm.discover import inspect

    report = inspect(built[1], compute_hashes=False)
    narratives = [s for s in report.sources if s.source_id == "pft_narrative"]
    skipped = [u for s in narratives for u in s.units if u.warnings]
    assert len(skipped) == 1 and "unnamed" in skipped[0].warnings[0]


# -- hashing and type drift ------------------------------------------------------


def test_a_date_typed_as_text_hashes_like_one_typed_as_a_date(built, events):
    """The drift that would break cross-batch dedup with nothing raising.

    One workbook writes the death date as text and its sibling writes it as a date
    cell. The stored strings differ, because the written shape is information. The row
    hashes must not, and neither must the resulting event.
    """
    layout, cfg = built
    stored, hashes = {}, {}
    for partition in ("b1_has", "b2_has"):
        files = sorted((layout.source_dir / partition / "demographics").glob("*.parquet"))
        frame = pl.read_parquet(files[0]).filter(pl.col("person_source_id") == CROSS_LABEL_PATIENT)
        assert frame.height == 1, partition
        stored[partition] = frame["col__Death_Date"][0]
        hashes[partition] = frame["source_row_sha256"][0]

    assert stored["b1_has"] != stored["b2_has"], "the fixture must actually exercise the drift"
    assert hashes["b1_has"] == hashes["b2_has"], "the same record must hash identically"

    # and the two spellings collapse to exactly one death event
    deaths = events.filter(pl.col("event_kind") == str(EventKind.death))
    assert deaths.height == 1
    assert deaths["subject_id"][0] == subject_of(cfg, CROSS_LABEL_PATIENT)


def test_a_length_of_stay_typed_as_text_matches_one_typed_as_an_integer(built):
    layout = built[0]
    values = set()
    for partition in ("b1_no", "b2_no"):
        files = sorted((layout.source_dir / partition / "outcome").glob("*.parquet"))
        if not files:
            continue
        frame = pl.read_parquet(files[0])
        values |= {v for v in frame["col__Length_of_stay_days"].to_list() if v}
    assert values and all(v.isdigit() for v in values), values


# -- anchors ---------------------------------------------------------------------


def test_the_anchor_column_never_becomes_an_event_time(built, events):
    layout, cfg = built
    anchors = pl.read_parquet(layout.canonical_path("anchors"))
    sid = subject_of(cfg, CROSS_LABEL_PATIENT)
    anchor_dates = set(anchors.filter(pl.col("subject_id") == sid)["anchor_date"].to_list())
    studies = events.filter((pl.col("subject_id") == sid) & (pl.col("source_id") == "echo"))
    assert studies.height > 0
    assert not {t.date() for t in studies["event_time"].to_list()} & anchor_dates


def test_the_anchor_set_is_per_date_and_per_partition(built):
    layout, cfg = built
    anchors = pl.read_parquet(layout.canonical_path("anchors"))
    mine = anchors.filter(pl.col("subject_id") == subject_of(cfg, CROSS_LABEL_PATIENT))
    assert mine["anchor_date"].n_unique() == EXPECTED_ANCHOR_DATES
    # the same calendar date in two partitions stays two anchors
    assert mine.height > mine["anchor_date"].n_unique()


def test_a_timestamped_batch_and_a_date_only_batch_are_both_recorded_honestly(built):
    layout, cfg = built
    anchors = pl.read_parquet(layout.canonical_path("anchors"))
    mine = anchors.filter(pl.col("subject_id") == subject_of(cfg, CROSS_LABEL_PATIENT))
    known = mine.filter(pl.col("anchor_time_known"))
    unknown = mine.filter(~pl.col("anchor_time_known"))
    assert known.height > 0 and unknown.height > 0
    assert known["anchor_time"].null_count() == 0
    assert unknown["anchor_time"].null_count() == unknown.height


def test_a_known_time_survives_a_date_only_duplicate_of_the_same_anchor(built):
    """Two sources in one partition describe one anchor; the time must not be lost."""
    layout, cfg = built
    anchors = pl.read_parquet(layout.canonical_path("anchors"))
    same_key = anchors.group_by(["subject_id", "anchor_date", "partition_id"]).len()
    assert int(same_key["len"].max()) == 1, "anchors are deduplicated per date and partition"
    b1 = anchors.filter(pl.col("partition_id") == "b1_has")
    assert b1.filter(pl.col("anchor_time_known")).height > 0


# -- deduplication ---------------------------------------------------------------


def test_a_report_repeated_under_every_anchor_becomes_one_note(built, events):
    layout, cfg = built
    sid = subject_of(cfg, CROSS_LABEL_PATIENT)
    notes = events.filter(
        (pl.col("subject_id") == sid)
        & (pl.col("event_kind") == str(EventKind.note))
        & (pl.col("source_id") == "echo")
    )
    assert notes.height == 1
    assert notes["value_text"][0].count("\n") == REPORT_LINES - 2  # one blank line is dropped


def test_every_repeated_row_stays_linked_to_that_one_note(built, events):
    layout, cfg = built
    links = pl.read_parquet(layout.canonical_path("event_source"))
    sid = subject_of(cfg, CROSS_LABEL_PATIENT)
    note = events.filter(
        (pl.col("subject_id") == sid)
        & (pl.col("event_kind") == str(EventKind.note))
        & (pl.col("source_id") == "echo")
    )
    linked = links.filter(pl.col("event_id") == note["event_id"][0])
    # six anchors' worth of rows across two batches, all still traceable
    assert linked.height >= REPORT_LINES * 5
    assert linked["partition_id"].n_unique() >= 2


def test_a_fact_extracted_into_two_batches_is_flagged_as_such(events):
    duplicated = events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.DUPLICATE_ACROSS_PARTITIONS))
    )
    assert duplicated.height > 0


# -- cohort labels ---------------------------------------------------------------


def test_a_patient_in_both_cohorts_keeps_both_labels_unmerged(built):
    layout, cfg = built
    memberships = pl.read_parquet(layout.canonical_path("cohort_membership"))
    mine = memberships.filter(pl.col("subject_id") == subject_of(cfg, CROSS_LABEL_PATIENT))
    assert set(mine["membership_label"].to_list()) == {"has", "no"}
    assert mine["label_scope"].unique().to_list() == ["episode"]


def test_the_cohort_label_is_not_a_clinical_event(events, built):
    labels = {p.membership_label for p in built[1].partitions if p.membership_label}
    codes = {c for c in events["source_code"].to_list() if c}
    assert not codes & labels
    texts = {t for t in events["value_text"].to_list() if t}
    assert not texts & labels


# -- values, times and quarantine ------------------------------------------------


def test_every_value_form_lands_where_the_design_says(events):
    measurements = events.filter(pl.col("event_kind") == str(EventKind.measurement))
    by_code = {row["source_code"]: row for row in measurements.iter_rows(named=True)}

    assert by_code["NA"]["value_number"] == 138.0
    assert by_code["WBC"]["value_number"] == 38.62 and by_code["WBC"]["unit_source"] == "K/cu mm"
    assert by_code["CREAT"]["value_number"] is None
    assert by_code["CREAT"]["value_text"] == "<0.50"
    assert str(QualityFlag.COMPARATOR_VALUE) in by_code["CREAT"]["quality_flags"]
    assert by_code["K"]["value_number"] is None and by_code["K"]["value_text"] == "see below"
    assert str(QualityFlag.NON_NUMERIC_RESULT) in by_code["K"]["quality_flags"]
    assert by_code["DIAGNOSIS"]["value_text"] in {"SINUS TACHYCARDIA", "RIGHT AXIS DEVIATION"}


def test_a_missing_collection_time_falls_back_and_says_so(events):
    fallback = events.filter(pl.col("quality_flags").list.contains(str(QualityFlag.TIME_FALLBACK)))
    assert fallback.height > 0
    assert fallback.filter(pl.col("event_time").is_null()).height == 0


def test_a_null_ordering_date_is_quarantined_rather_than_invented(built):
    quarantine = pl.read_parquet(built[0].canonical_path("quarantine"))
    orders = quarantine.filter(
        (pl.col("source_id") == "all_rx") & (pl.col("reason") == str(QuarantineReason.MISSING_EVENT_TIME))
    )
    assert orders.height == 7


def test_untimed_measurements_are_quarantined_rather_than_dated(built):
    quarantine = pl.read_parquet(built[0].canonical_path("quarantine"))
    untimed = quarantine.filter(pl.col("reason") == str(QuarantineReason.UNTIMED_CLINICAL_VALUE))
    assert untimed.height > 0
    assert {d.split("=")[0] for d in untimed["detail"].to_list()} == {"BMI", "PULSE", "BP_SYSTOLIC"}


def test_nothing_is_quarantined_for_an_unparseable_time(built):
    """Every timestamp shape in the fixture is one the config declares or we ourselves write."""
    quarantine = pl.read_parquet(built[0].canonical_path("quarantine"))
    assert quarantine.filter(pl.col("reason") == str(QuarantineReason.UNPARSEABLE_TIME)).height == 0


def test_a_record_dated_after_death_is_flagged_and_keeps_its_date(events, built):
    flagged = events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.RECORDED_AFTER_DEATH))
    )
    assert flagged.height > 0
    assert flagged.filter(pl.col("event_kind") == str(EventKind.condition)).height > 0
    assert all(t.year == 2031 for t in flagged["event_time"].to_list())


def test_orders_and_administrations_stay_distinguishable(events):
    orders = events.filter(pl.col("event_kind") == str(EventKind.drug_order))
    admins = events.filter(pl.col("event_kind") == str(EventKind.drug_admin))
    assert orders.height > 0 and admins.height > 0
    assert not set(orders["event_id"].to_list()) & set(admins["event_id"].to_list())
    assert orders["source_id"].unique().to_list() == ["all_rx"]
    assert admins["source_id"].unique().to_list() == ["medication_admin"]


def test_the_masked_duration_is_kept_verbatim_and_never_parsed(events):
    visits = events.filter(pl.col("event_kind") == str(EventKind.visit))
    assert visits.height > 0
    assert all("*" in (v or "") for v in visits["value_text"].to_list())


def test_the_full_pipeline_publishes_and_validates(built):
    """The fixture also exercises OMOP and MEDS, including the approved-approximation path."""
    from ehr2cdm.meds import build_meds
    from ehr2cdm.omop import build_omop
    from ehr2cdm.validate import run_checks

    layout, cfg = built
    omop_result = build_omop(cfg, layout, vocabulary_dir=None)
    # Three patients publish; the fourth is withheld because two extracts recorded
    # different ages and neither carries a reference date.
    assert omop_result["tables"]["person"] == 3
    assert omop_result["blocked_subjects"] == 1
    meds_result = build_meds(cfg, layout)
    assert meds_result["subjects"] == 4, "MEDS can state an age honestly, so nobody is withheld"
    failed = [r for r in run_checks(cfg, layout, include_slow=True) if not r.passed]
    assert not failed, [f"{r.check_id}: {r.detail}" for r in failed]


def test_disagreeing_ages_block_that_patient_and_say_why(built):
    """Two extracts, two ages, no reference date: a birth year cannot be derived."""
    import duckdb

    from ehr2cdm.omop import build_omop

    layout, cfg = built
    build_omop(cfg, layout, vocabulary_dir=None)
    con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"), read_only=True)
    try:
        conflicts = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue WHERE issue_type = 'AGE_CONFLICT'"
        ).fetchone()[0]
        blocked = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue WHERE issue_type = 'OMOP_PERSON_BLOCKED'"
        ).fetchone()[0]
        # and nothing of that patient leaked into a clinical table
        dangling = con.execute(
            "SELECT count(*) FROM measurement m "
            "WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = m.person_id)"
        ).fetchone()[0]
    finally:
        con.close()
    assert conflicts == 1 and blocked == 1 and dangling == 0
