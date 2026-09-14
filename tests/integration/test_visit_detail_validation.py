"""VISIT_CONCEPT_COVERAGE on visit details, on the hand-built layer the publisher tests use.

Two facts measured while the CU-CTPA and JHU-CTPE halves of the remediation merged decide
how a visit detail may be judged. A detail no visit can parent cannot be published --
CDM 5.4 declares ``visit_detail.visit_occurrence_id`` NOT NULL -- so on one export most
ICU transfers never reach OMOP, by design, and a check reading that gap as loss would
fail every correct build. And a detail names a ward rather than a visit type, so its
concept coverage is a review queue with a threshold of its own, not the visit's.

See :mod:`tests.integration.hand_built_layer` for why the layer is written by hand.
"""

from __future__ import annotations

import duckdb
import pytest

from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import run_checks
from tests.integration.hand_built_layer import config, the_layer, write_canonical


def coverage(cfg, layout: WorkLayout):
    (result,) = [r for r in run_checks(cfg, layout) if r.check_id == "VISIT_CONCEPT_COVERAGE"]
    return result


def built(root) -> WorkLayout:
    layout = write_canonical(root, the_layer())
    build_omop(config(), layout, vocabulary_dir=None)
    return layout


def as_if_built_with_a_vocabulary(layout: WorkLayout, visits_with_concept: str, details_with_concept: str) -> None:
    """Give the layer a vocabulary version and the stated rows a concept.

    The concept ids are arbitrary non-zero integers: the check counts coverage, it does
    not look concepts up, and a vocabulary is not needed to say whether a row has one.
    """
    con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"))
    try:
        con.execute("UPDATE cdm_source SET vocabulary_version = 'v5.0 test vocabulary'")
        con.execute(f"UPDATE visit_occurrence SET visit_concept_id = CASE WHEN {visits_with_concept} THEN 9201 ELSE 0 END")
        con.execute(f"UPDATE visit_detail SET visit_detail_concept_id = CASE WHEN {details_with_concept} THEN 32037 ELSE 0 END")
    finally:
        con.close()


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> WorkLayout:
    return built(tmp_path_factory.mktemp("visit_detail_check"))


def test_a_withheld_visit_detail_is_reported_and_not_counted_as_loss(published):
    result = coverage(config(), published)
    withheld = result.metrics["visit_detail_withheld"]

    assert result.passed and not result.skipped, result.detail
    # Every detail is either published or reported, and the report names the reason.
    assert withheld["unparented_issues"] == 2
    assert withheld["published_visit_details"] == 4
    assert withheld["canonical_visit_details"] == withheld["published_visit_details"] + withheld["unparented_issues"]
    assert "NOT NULL" in withheld["reason"]
    assert "withheld as unparented" in result.detail


def test_a_visit_detail_is_held_to_its_own_threshold_not_the_visits(tmp_path):
    layout = built(tmp_path / "judged")
    # every visit coded; one detail in four coded, the way a ward review in progress looks
    as_if_built_with_a_vocabulary(layout, "true", "visit_detail_id = (SELECT min(visit_detail_id) FROM visit_detail)")
    cfg = config()

    result = coverage(cfg, layout)
    assert not result.passed
    assert "visit_detail" in result.detail and "visit_occurrence" not in result.detail
    assert result.metrics["coverage"]["visit_detail"]["threshold"] == cfg.validation.visit_detail_concept_coverage_min
    assert result.metrics["coverage"]["visit_occurrence"]["threshold"] == cfg.validation.visit_concept_coverage_min

    # A dataset whose wards are a known review queue lowers the detail threshold, says why,
    # and still has its visits held to theirs.
    lowered = cfg.model_copy(update={"validation": cfg.validation.model_copy(update={
        "visit_detail_concept_coverage_min": 0.2,
        "note": "ward names are under review; one in four is mapped so far",
    })})
    result = coverage(lowered, layout)
    assert result.passed, result.detail
    assert result.metrics["note"].startswith("ward names are under review")


def test_uncoded_visits_fail_on_the_visit_threshold_whatever_the_details_carry(tmp_path):
    layout = built(tmp_path / "visits")
    as_if_built_with_a_vocabulary(layout, "false", "true")
    result = coverage(config(), layout)
    assert not result.passed
    assert "visit_occurrence" in result.detail and "visit_detail:" not in result.detail
