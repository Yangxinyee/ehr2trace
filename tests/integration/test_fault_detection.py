"""Every catalogued fault must be detected by something (paper section 5.2).

This is the paper's central experiment, run as a regression test on the fixture. A
check suite that passes on its own output proves nothing; what has to hold is that
injecting a corruption makes something fail that was passing before.

Each fault is applied to a clone of a clean build, so faults cannot contaminate each
other -- which they did the first time this was run, when the clone hard-linked the
DuckDB file and the OMOP mutations wrote straight through it.
"""

from __future__ import annotations

import pytest

from ehr2trace.config import load_dataset_config
from ehr2trace.faults import FAULTS, clone_work_tree
from ehr2trace.meds import build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import run_checks
from tests.integration.test_ctpe_shape_anomalies import CONFIG, FIXTURE


@pytest.fixture(scope="module")
def clean_build(tmp_path_factory):
    """One full build -- source through MEDS -- for every fault to be cloned from.

    The shape fixture rather than the generic one: it has extraction anchors, cohort
    partitions and a death, so it can exercise the faults that only exist because a
    dataset has those things. Built without a vocabulary, so it runs in CI.
    """
    import os

    from ehr2trace.canonical.build import merge_buckets, plan_canonical, plan_stage, run_canonical_task, run_stage_task
    from ehr2trace.identity import build_identity
    from ehr2trace.ingest import plan_ingest, run_ingest_task, write_manifest
    from ehr2trace.run import execute

    os.environ["CTPE_SHAPE_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=tmp_path_factory.mktemp("faults") / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()

    results = execute(plan_ingest(cfg, layout), run_ingest_task, 1)
    write_manifest(layout, cfg, results)
    build_identity(cfg, layout)
    execute(plan_stage(cfg, layout), run_stage_task, 1)
    tasks = plan_canonical(cfg, layout, CONFIG, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, 1)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    build_omop(cfg, layout, vocabulary_dir=None)
    build_meds(cfg, layout)

    baseline = {r.check_id: r.passed for r in run_checks(cfg, layout, include_slow=True)}
    return cfg, layout, baseline


def test_the_clean_build_passes_everything(clean_build):
    """Without this the experiment measures nothing: a check already failing is not a detector."""
    _cfg, _layout, baseline = clean_build
    failing = sorted(k for k, ok in baseline.items() if not ok)
    assert not failing, f"clean build already fails: {failing}"


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f.id)
def test_an_injected_fault_is_detected(clean_build, tmp_path, fault):
    cfg, source_layout, baseline = clean_build
    scratch = tmp_path / fault.id.lower()
    clone_work_tree(source_layout.root, scratch)
    layout = WorkLayout(root=scratch, dataset_id=cfg.dataset_id)

    effect = fault.apply(layout, cfg)
    if effect.startswith("skipped"):
        pytest.skip(f"{fault.id}: {effect}")

    after = {r.check_id: r.passed for r in run_checks(cfg, layout, include_slow=True)}
    # A detector passes clean and fails dirty. Anything already failing tells us nothing.
    detectors = sorted(k for k, ok in after.items() if not ok and baseline.get(k, False))
    assert detectors, (
        f"{fault.id} was injected ({effect}) and no check noticed.\n"
        f"This fault is drawn from a real incident: {fault.origin}"
    )


def test_injection_does_not_reach_the_build_it_was_cloned_from(clean_build, tmp_path):
    """The hard-link clone must not let an OMOP mutation write through to the original.

    This happened. The clone shared inodes to stay cheap at full scale, which is safe
    for parquet because mutations write a new file and rename, and unsafe for a DuckDB
    file because it is opened read-write. The corruption then leaked into every
    subsequent fault's result.
    """
    cfg, source_layout, baseline = clean_build
    omop_faults = [f for f in FAULTS if f.layer == "omop"]
    assert omop_faults, "no OMOP faults to test the isolation with"

    scratch = tmp_path / "isolation"
    clone_work_tree(source_layout.root, scratch)
    omop_faults[0].apply(WorkLayout(root=scratch, dataset_id=cfg.dataset_id), cfg)

    after = {r.check_id: r.passed for r in run_checks(cfg, source_layout, include_slow=True)}
    assert after == baseline, "injecting into the clone changed the original build"
