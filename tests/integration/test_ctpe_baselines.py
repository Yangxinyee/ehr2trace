"""Baselines measured from the real export (design section 2, checklist P0-5, P1-11).

These are regression baselines, not aspirations. **When a number here changes, report
the drift and investigate the data. Do not edit the expected value.** A converter whose
expectations follow whatever the data happens to say is not checking anything.

Everything is skipped without ``EHR_DATA_ROOT``; the per-patient assertions additionally
need ``tools/baselines.local.json``, which holds real identifiers and is git-ignored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import polars as pl
import pytest

pytestmark = pytest.mark.realdata

# -- design section 2.1 / 2.2 ----------------------------------------------------

EXPECTED_PHYSICAL_FILES = 25
EXPECTED_TEXT_SOURCES = 21
EXPECTED_SHEET_SOURCES = 19

# -- design section 2.4 ----------------------------------------------------------

EXPECTED_MRNS = {"29_has": 5633, "29_no": 8623, "29b_has": 8247, "29b_no": 8468}
EXPECTED_UNION = 22982
EXPECTED_PAIRWISE = {
    ("29_has", "29_no"): 723,
    ("29_has", "29b_has"): 5633,
    ("29_has", "29b_no"): 392,
    ("29_no", "29b_has"): 923,
    ("29_no", "29b_no"): 747,
    ("29b_has", "29b_no"): 927,
}
EXPECTED_HAS_NO_OVERLAP = 1609

#: A patient's echo report is one base set of rows repeated once per anchor.
EXPECTED_ECHO_ROWS_PER_ANCHOR = 547
EXPECTED_ANCHORS_FOR_ANCHOR_PATIENT = 4


@pytest.fixture(scope="module", autouse=True)
def _requires_the_real_export(data_root):
    """Every test here reads the real export; without it they skip rather than fail."""
    return data_root


@pytest.fixture(scope="module")
def work_layout(ctpe_config):
    from ehr2cdm.paths import WorkLayout

    if not os.environ.get("EHR_WORK_ROOT"):
        pytest.skip("EHR_WORK_ROOT is not set")
    return WorkLayout.from_env(ctpe_config.dataset_id)


@pytest.fixture(scope="module")
def report(ctpe_config):
    from ehr2cdm.discover import inspect

    return inspect(ctpe_config, compute_hashes=False)


def test_every_physical_file_is_discovered(report):
    assert report.counts["physical_files"] == EXPECTED_PHYSICAL_FILES


def test_every_logical_source_is_recognized(report):
    assert report.counts["logical_sources_text"] == EXPECTED_TEXT_SOURCES
    assert report.counts["logical_sources_sheets"] == EXPECTED_SHEET_SOURCES
    assert report.counts["logical_sources"] == EXPECTED_TEXT_SOURCES + EXPECTED_SHEET_SOURCES


def test_the_two_sheet_naming_variants_both_resolve(report):
    """One partition names the same sheets differently; both must be found."""
    for source_id in ("pft_narrative", "pft_values"):
        found = [s for s in report.sources if s.source_id == source_id and s.coverage == "present"]
        assert len(found) == 4, f"{source_id} missing in {4 - len(found)} partitions"


def test_the_standalone_medication_file_and_the_sheet_are_one_logical_source(report):
    found = {s.partition_id for s in report.sources if s.source_id == "medication_admin" and s.units}
    assert len(found) == 4
    adapters = {u.adapter for s in report.sources if s.source_id == "medication_admin" for u in s.units}
    assert adapters == {"delimited", "excel"}, "both physical forms must be in use"


def test_the_byte_order_mark_is_inconsistent_and_that_is_handled(report):
    """Nineteen text files carry one and two do not, within the same logical source."""
    text_files = [f for f in report.files if f.kind == "text"]
    with_bom = [f for f in text_files if f.bom]
    without = [f for f in text_files if not f.bom]
    assert len(with_bom) == 19 and len(without) == 2
    # the parser must produce the same first column name either way
    problem_lists = [s for s in report.sources if s.source_id == "problem_list"]
    first_columns = {u.columns[0] for s in problem_lists for u in s.units}
    assert len(first_columns) == 1, f"BOM handling changed a column name: {first_columns}"


def test_one_sheet_has_an_extra_unnamed_leading_column_that_is_skipped(report):
    narratives = [s for s in report.sources if s.source_id == "pft_narrative"]
    skipped = [u for s in narratives for u in s.units if u.warnings]
    assert len(skipped) == 1
    assert "unnamed" in skipped[0].warnings[0]


def test_the_blockers_are_reported_rather_than_assumed(report):
    ids = {b.id.split(":")[0] for b in report.blockers}
    assert {
        "TIMEZONE_UNDECLARED",
        "AGE_REFERENCE_DATE_MISSING",
        "LABEL_DEFINITION_UNDEFINED",
        "BATCH_RELATIONSHIP_UNKNOWN",
        "IMAGING_REPORTS_UNCONFIRMED",
        "VOCABULARY_SOURCE_MISSING",
    } <= ids


# -- identity --------------------------------------------------------------------


@pytest.fixture(scope="module")
def subjects(work_layout):
    path = work_layout.identity_dir / "subject_map.parquet"
    if not path.exists():
        pytest.skip("identity not built for the real dataset")
    return pl.read_parquet(path)


def test_patient_counts_per_partition_match_the_baselines(subjects):
    for partition, expected in EXPECTED_MRNS.items():
        actual = subjects.filter(pl.col("partitions").list.contains(partition)).height
        assert actual == expected, f"{partition}: {actual} != {expected} (investigate the data)"


def test_the_union_matches_and_identity_is_resolved_once(subjects):
    assert subjects.height == EXPECTED_UNION
    assert subjects["person_source_id"].n_unique() == EXPECTED_UNION
    assert subjects["subject_id"].n_unique() == EXPECTED_UNION


def test_pairwise_overlaps_match_the_baselines(subjects):
    for (a, b), expected in EXPECTED_PAIRWISE.items():
        actual = subjects.filter(
            pl.col("partitions").list.contains(a) & pl.col("partitions").list.contains(b)
        ).height
        assert actual == expected, f"{a} x {b}: {actual} != {expected}"


def test_one_partition_is_wholly_contained_in_the_other_batch(subjects):
    only_in_first = subjects.filter(
        pl.col("partitions").list.contains("29_has") & ~pl.col("partitions").list.contains("29b_has")
    )
    assert only_in_first.height == 0


def test_the_cohort_label_overlap_is_the_measured_one(subjects):
    """Patients in both a 'has' and a 'no' partition: the reason the label is episode-level."""
    both = subjects.filter(
        (pl.col("partitions").list.contains("29_has") | pl.col("partitions").list.contains("29b_has"))
        & (pl.col("partitions").list.contains("29_no") | pl.col("partitions").list.contains("29b_no"))
    )
    assert both.height == EXPECTED_HAS_NO_OVERLAP


# -- canonical -------------------------------------------------------------------


@pytest.fixture(scope="module")
def canonical(work_layout):
    if not work_layout.canonical_path("events").exists():
        pytest.skip("canonical layer not built for the real dataset")
    return work_layout


@pytest.fixture(scope="module")
def local_baselines():
    path = Path(__file__).resolve().parents[2] / "tools" / "baselines.local.json"
    if not path.exists():
        pytest.skip("tools/baselines.local.json not generated (holds real identifiers)")
    return json.loads(path.read_text(encoding="utf-8"))


def subject_id_for(ctpe_config, mrn: str) -> int:
    from ehr2cdm.hashing import subject_id_from_person_key

    return subject_id_from_person_key(ctpe_config.dataset_id, mrn, ctpe_config.subject_salt())


def test_the_anchor_patient_has_the_measured_anchor_set(canonical, ctpe_config, local_baselines):
    anchors = pl.read_parquet(canonical.canonical_path("anchors"))
    mrn = local_baselines["alias_to_mrn"]["PT-B"]
    sid = subject_id_for(ctpe_config, mrn)
    mine = anchors.filter(pl.col("subject_id") == sid)
    assert mine.height > 0, "the anchor patient has no anchors"
    assert mine["anchor_date"].n_unique() == EXPECTED_ANCHORS_FOR_ANCHOR_PATIENT


def test_anchor_duplication_collapses_but_keeps_every_source_row(canonical, ctpe_config, local_baselines):
    """The repeated report is one note; all its repeated rows stay linked."""
    events = pl.read_parquet(
        canonical.canonical_path("events"), columns=["event_id", "subject_id", "source_id", "event_kind"]
    )
    links = pl.read_parquet(canonical.canonical_path("event_source"), columns=["event_id", "source_row_id"])
    mrn = local_baselines["alias_to_mrn"]["PT-B"]
    sid = subject_id_for(ctpe_config, mrn)
    notes = events.filter((pl.col("subject_id") == sid) & (pl.col("event_kind") == "note"))
    assert notes.height > 0
    linked = links.join(notes.select("event_id"), on="event_id")
    assert linked.height > notes.height, "duplicated rows must remain traceable"


def test_the_cohort_label_conflict_is_preserved_per_partition(canonical, ctpe_config, local_baselines):
    """The patient in both a 'has' and a 'no' partition keeps both rows, unmerged."""
    memberships = pl.read_parquet(canonical.canonical_path("cohort_membership"))
    mrn = local_baselines["alias_to_mrn"]["PT-B"]
    sid = subject_id_for(ctpe_config, mrn)
    mine = memberships.filter(pl.col("subject_id") == sid)
    assert set(mine["membership_label"].to_list()) == {"has", "no"}
    assert mine["label_scope"].unique().to_list() == ["episode"]


def test_no_event_time_equals_its_own_anchor_for_the_anchor_patient(canonical, ctpe_config, local_baselines):
    """The anchor is not an event time, checked against the data rather than the config."""
    anchors = pl.read_parquet(canonical.canonical_path("anchors"))
    events = pl.read_parquet(canonical.canonical_path("events"), columns=["subject_id", "event_time", "source_id"])
    mrn = local_baselines["alias_to_mrn"]["PT-B"]
    sid = subject_id_for(ctpe_config, mrn)
    anchor_dates = set(anchors.filter(pl.col("subject_id") == sid)["anchor_date"].to_list())
    studies = events.filter(
        (pl.col("subject_id") == sid) & pl.col("source_id").is_in(["echo", "ekg", "labs"])
    )
    dates = {t.date() for t in studies["event_time"].to_list() if t is not None}
    # Some clinical events may legitimately fall on an anchor date, but not all of them.
    assert dates - anchor_dates, "every study time coincides with an anchor: dos leaked in"
