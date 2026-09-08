"""Two claims the paper makes that were, until measured, only intentions.

The converter is called deterministic: the same inputs and configuration must produce the
same output whatever the concurrency, because the content address deliberately excludes
the execution settings. And the check suite's detection rate is only meaningful next to a
false-alarm rate -- a suite that fails on everything detects every fault and is worthless.

Both are asserted here on every commit rather than only in the paper's experiment, which
runs the same comparison over a larger fixture and four configurations.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ehr2trace.config import load_dataset_config
from ehr2trace.digest import changed, fingerprint
from ehr2trace.meds import build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import run_checks
from tests.integration.test_generic_ehr_pipeline import CONFIG, run_pipeline

WORKER_COUNTS = (1, 4)


def build(work_root: Path, workers: int) -> tuple[WorkLayout, list]:
    # `vocabulary_dir=None` reads the environment, and a developer with a vocabulary
    # installed would otherwise get a different build from CI.
    previous = os.environ.pop("OMOP_VOCAB_DIR", None)
    try:
        layout = run_pipeline(work_root, workers=workers)
        cfg = load_dataset_config(CONFIG)
        build_omop(cfg, layout, vocabulary_dir=None)
        build_meds(cfg, layout)
        return layout, run_checks(cfg, layout, include_slow=True)
    finally:
        if previous is not None:
            os.environ["OMOP_VOCAB_DIR"] = previous


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> list[tuple[int, WorkLayout, list]]:
    return [
        (workers, *build(tmp_path_factory.mktemp(f"w{workers}"), workers))
        for workers in WORKER_COUNTS
    ]


def test_the_worker_count_does_not_change_the_output(builds):
    reference = fingerprint(builds[0][1])
    assert reference, "nothing was published, so agreement would be vacuous"
    for workers, layout, _results in builds[1:]:
        difference = changed(reference, fingerprint(layout))
        assert difference == [], f"{workers} workers produced different {difference}"


def test_a_correct_build_raises_no_false_alarm(builds):
    for workers, _layout, results in builds:
        failed = sorted(r.check_id for r in results if not r.passed and not r.skipped)
        assert failed == [], f"{workers} workers: {failed} failed on a correct build"


def test_the_comparison_covers_every_layer(builds):
    # A fingerprint that silently covered only one layer would make the first test pass
    # for the wrong reason.
    keys = fingerprint(builds[0][1])
    for prefix in ("canonical/", "omop/", "meds/"):
        assert any(k.startswith(prefix) for k in keys), f"no {prefix} artifact compared"
