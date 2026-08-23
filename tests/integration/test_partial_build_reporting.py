"""A layer that was not built is not a layer that passed.

This exists because of a specific, embarrassing incident. A MIMIC-IV run failed at the
canonical stage; OMOP and MEDS therefore produced nothing; and `validate` reported
**34/34 checks passed**. Every check that had nothing to examine returned a skip, skips
were counted as passes, and the OMOP checks that did run found no violations because
there were no rows to violate anything -- "every clinical row resolves to one of the 0
published persons" is true and worthless.

A validator that reports success on a failed build is worse than no validator, because
it is trusted.
"""

from __future__ import annotations

import pytest

from ehr2cdm.config import load_dataset_config
from ehr2cdm.ingest import plan_ingest, run_ingest_task, write_manifest
from ehr2cdm.paths import WorkLayout
from ehr2cdm.run import execute
from ehr2cdm.validate import run_checks
from tests.integration.test_ctpe_shape_anomalies import CONFIG, FIXTURE


@pytest.fixture(scope="module")
def source_only(tmp_path_factory):
    """Ingest and nothing else: the state a pipeline is in when canonical fails."""
    import os

    os.environ["CTPE_SHAPE_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=tmp_path_factory.mktemp("partial") / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()
    results = execute(plan_ingest(cfg, layout), run_ingest_task, 1)
    write_manifest(layout, cfg, results)
    return cfg, layout


def test_a_partial_build_is_reported_as_skipped_not_passed(source_only):
    cfg, layout = source_only
    results = run_checks(cfg, layout, include_slow=True)

    skipped = [r for r in results if r.skipped]
    genuinely_passed = [r for r in results if r.passed and not r.skipped]

    assert skipped, "canonical, OMOP and MEDS are absent; something must have skipped"
    # The headline number must not be able to reach the full count on this build.
    assert len(genuinely_passed) < len(results), (
        "a build with no canonical, OMOP or MEDS layer reported every check as passed"
    )


def test_no_omop_check_passes_on_an_empty_database(source_only, tmp_path):
    """An OMOP file with tables and no rows must skip, not pass vacuously.

    This is the half of the incident that a skip-versus-pass distinction alone does not
    fix: the database existed, so the checks ran, and counting violations in an empty
    table finds none.
    """
    import duckdb

    cfg, layout = source_only
    layout.omop_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"))
    con.execute("CREATE TABLE person (person_id INTEGER, year_of_birth INTEGER, gender_concept_id INTEGER)")
    con.execute("CREATE TABLE condition_occurrence (condition_occurrence_id BIGINT, person_id INTEGER)")
    con.close()

    results = {r.check_id: r for r in run_checks(cfg, layout, include_slow=True)}
    omop_checks = {k: v for k, v in results.items() if k.startswith("OMOP_")}
    assert omop_checks, "no OMOP checks registered"

    vacuous = sorted(k for k, r in omop_checks.items() if r.passed and not r.skipped)
    assert not vacuous, f"these OMOP checks passed against an empty database: {vacuous}"
