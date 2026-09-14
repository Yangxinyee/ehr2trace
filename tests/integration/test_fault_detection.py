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


def test_the_audit_tool_reads_the_build_and_reports_only_aggregates(clean_build):
    """`tools/audit_conversion.py` is the audit's queries; it must run and stay aggregate.

    The tool exists so the numbers the remediation is judged by can be recomputed rather
    than quoted. That makes two things testable: it reads a build of any dataset without
    being told anything about it, and nothing patient-level reaches its output -- no
    subject id, no note text -- because the document it writes is meant to be read,
    shared and diffed.
    """
    import json

    import polars as pl

    from tools.audit_conversion import audit

    cfg, layout, _baseline = clean_build
    report = audit(cfg.dataset_id, layout.root.parent)

    assert report["layers"] == {"manifest": True, "canonical": True, "omop": True, "meds": True}
    assert report["events"] > 0 and report["sources"]
    for section in ("merge_disagreements", "units", "visits", "doses", "death", "notes",
                    "encounter_link_rate", "raw_coverage"):
        assert section in report, f"{section} missing from the audit"
    # The clean build is clean: nothing merged rows that disagree, every column declared.
    assert not [s for s, e in report["merge_disagreements"].items() if e.get("disagreements")]
    assert not [c for c in report["raw_coverage"]["columns"].values() if c["undeclared"]]

    written = json.dumps(report, default=str)
    events = pl.read_parquet(layout.canonical_path("events"))
    for subject_id in events["subject_id"].unique().to_list():
        assert str(subject_id) not in written, "a subject id reached the audit document"
    for text in events["value_text"].drop_nulls().unique().to_list():
        assert str(text)[:40] not in written, "text from a value column reached the audit document"


def test_a_unit_column_the_build_never_read_is_seen_in_the_source(clean_build):
    """DOSE_UNIT_CARRIED reads the source parquet as well as the canonical layer.

    The audit's worst dose finding was invisible from inside the build: one export kept
    its dose unit in a column of its own that the configuration never mapped, so no
    event carried a unit, every check reading the canonical layer saw nothing missing,
    and 1,897,802 bare numbers reached MEDS. Here the build is the clean one, read with a
    configuration that says the order table's dose column states a unit: that is a
    column the build did not read as one, and the check must say so from the source.
    """
    from ehr2trace.validate import run_checks

    cfg, layout, baseline = clean_build
    assert baseline["DOSE_UNIT_CARRIED"]
    sid, spec = next((sid, spec) for sid, spec in cfg.sources.items()
                     if spec.event_kind == "drug_order" and "dose" in spec.fields)
    fields = dict(spec.fields)
    fields["unit"] = spec.fields["dose"]
    claimed = cfg.model_copy(update={"sources": {**cfg.sources, sid: spec.model_copy(update={"fields": fields})}})

    (result,) = [r for r in run_checks(claimed, layout, include_slow=True) if r.check_id == "DOSE_UNIT_CARRIED"]
    assert not result.passed
    assert f"{sid} states a dose unit" in result.detail
    assert result.metrics["source_unit_statements"][sid]["events_carrying_a_unit"] == 0


def test_an_unset_prepared_root_skips_only_the_files_under_it(clean_build, monkeypatch):
    """One source's root being unset must not stop the walk of every other root.

    A dataset that gains a prepared source living under a root of its own gains a
    variable its old environments do not set; reading that as "examine no file at all"
    turned a coverage check into a pass that had looked at nothing.
    """
    import json

    from ehr2trace.validate import raw_coverage

    cfg, layout, _baseline = clean_build
    sid, spec = next(iter(cfg.sources.items()))
    variable = "EHR2TRACE_TEST_UNSET_PREPARED_ROOT"
    monkeypatch.delenv(variable, raising=False)
    moved = cfg.model_copy(update={"sources": {**cfg.sources, sid: spec.model_copy(update={"root_env": variable})}})
    manifest = json.loads((layout.manifest_dir / "inputs.json").read_text(encoding="utf-8"))

    report = raw_coverage(moved, manifest)
    assert variable in report["files"]["not_examined"]
    assert report["files"]["partitions_examined"], "the dataset's own root is still walked"


def test_a_declared_merge_rule_the_build_never_applied_fails(clean_build, tmp_path):
    """A rule written after a build was made must not make that build look settled.

    The clean build's outcome sheet merges two extracts of one admission under declared
    rules, and the flag those rules write is on the event. Take the flag away -- the state
    of a build made before the rules existed -- and the same rows must fail.
    """
    import polars as pl

    from ehr2trace.faults import clone_work_tree
    from ehr2trace.paths import WorkLayout
    from ehr2trace.validate import run_checks

    cfg, source_layout, baseline = clean_build
    assert baseline["DUPLICATES_AGREE"]
    clone_work_tree(source_layout.root, tmp_path / "unapplied")
    layout = WorkLayout(root=tmp_path / "unapplied", dataset_id=cfg.dataset_id)
    path = layout.canonical_path("events")
    events = pl.read_parquet(path)
    ruled_flags = {rule.flag_name for spec in cfg.sources.values() for rule in spec.merge_rules.values()}
    stripped = events.with_columns(
        pl.col("quality_flags").list.eval(pl.element().filter(~pl.element().is_in(sorted(ruled_flags))))
    )
    mutating = path.with_suffix(".parquet.mutating")
    stripped.write_parquet(mutating)
    mutating.replace(path)

    (result,) = [r for r in run_checks(cfg, layout, include_slow=True) if r.check_id == "DUPLICATES_AGREE"]
    assert not result.passed
    assert "declared merge rules the build did not apply" in result.detail
    assert result.metrics["sources_with_rules_not_applied"]
