"""End-to-end on the synthetic fixture, including the properties that only appear
once the whole pipeline has run: determinism, resumption, and the fact that a
structurally different source needs only a new YAML (checklist P1-10, P5-2).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import polars as pl
import pytest

from ehr2trace.canonical.build import merge_buckets, plan_canonical, plan_stage, run_canonical_task, run_stage_task
from ehr2trace.config import load_dataset_config
from ehr2trace.identity import build_identity
from ehr2trace.ingest import plan_ingest, run_ingest_task, write_manifest
from ehr2trace.paths import WorkLayout
from ehr2trace.run import execute
from ehr2trace.schema import EventKind, QualityFlag

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "generic_ehr"
CONFIG = Path(__file__).resolve().parents[2] / "datasets" / "generic_ehr.yaml"


def run_pipeline(work_root: Path, workers: int = 1) -> WorkLayout:
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=work_root / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()

    results = execute(plan_ingest(cfg, layout), run_ingest_task, workers)
    write_manifest(layout, cfg, results)
    build_identity(cfg, layout)
    execute(plan_stage(cfg, layout), run_stage_task, workers)
    tasks = plan_canonical(cfg, layout, CONFIG, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, workers)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    return layout


def data_hash(layout: WorkLayout) -> str:
    """Content hash of the canonical layer, excluding anything runtime-dependent."""
    digest = hashlib.sha256()
    for name in ("events", "event_source", "anchors", "cohort_membership", "quarantine"):
        path = layout.canonical_path(name)
        if not path.exists():
            continue
        frame = pl.read_parquet(path)
        digest.update(name.encode())
        digest.update(str(frame.sort(frame.columns).to_dicts()).encode())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> WorkLayout:
    return run_pipeline(tmp_path_factory.mktemp("single"), workers=1)


def test_a_structurally_different_source_needs_only_a_new_yaml(built: WorkLayout):
    """The only evidence for the word 'generalization' anywhere in this project."""
    events = pl.read_parquet(built.canonical_path("events"))
    kinds = set(events["event_kind"].to_list())
    assert kinds >= {
        str(EventKind.demographic),
        str(EventKind.visit),
        str(EventKind.condition),
        str(EventKind.drug_order),
        str(EventKind.drug_admin),
        str(EventKind.measurement),
        str(EventKind.note),
        str(EventKind.procedure),
        str(EventKind.death),
    }


def test_a_dataset_without_anchors_produces_no_anchor_rows(built: WorkLayout):
    """Anchors are one export's quirk, not a requirement of the pipeline."""
    anchors = pl.read_parquet(built.canonical_path("anchors"))
    assert anchors.height == 0


def test_one_patient_at_two_sites_resolves_to_one_subject(built: WorkLayout):
    subjects = pl.read_parquet(built.identity_dir / "subject_map.parquet")
    assert subjects.height == 4
    multi = subjects.filter(pl.col("partitions").list.len() > 1)
    assert multi.height == 1


def test_the_same_fact_at_two_sites_becomes_one_event_with_both_rows_linked(built: WorkLayout):
    events = pl.read_parquet(built.canonical_path("events"))
    links = pl.read_parquet(built.canonical_path("event_source"))
    duplicated = events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.DUPLICATE_ACROSS_PARTITIONS))
    )
    assert duplicated.height > 0
    for event_id in duplicated["event_id"].to_list():
        assert links.filter(pl.col("event_id") == event_id).height >= 2


def test_a_null_order_date_never_becomes_an_order_with_an_invented_date(built: WorkLayout):
    quarantine = pl.read_parquet(built.canonical_path("quarantine"))
    orders = quarantine.filter(
        (pl.col("source_id") == "orders") & (pl.col("reason") == "MISSING_EVENT_TIME")
    )
    assert orders.height == 1
    events = pl.read_parquet(built.canonical_path("events"))
    assert events.filter(pl.col("event_kind") == str(EventKind.drug_order)).height == 4


def test_a_record_dated_after_death_is_flagged_and_kept(built: WorkLayout):
    events = pl.read_parquet(built.canonical_path("events"))
    flagged = events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.RECORDED_AFTER_DEATH))
    )
    assert flagged.height == 1
    # kept with its original date, not deleted and not rewritten
    assert flagged["event_time"].to_list()[0].year == 2021


def test_every_event_traces_to_a_source_row(built: WorkLayout):
    events = pl.read_parquet(built.canonical_path("events"))
    links = pl.read_parquet(built.canonical_path("event_source"))
    assert set(events["event_id"].to_list()) <= set(links["event_id"].to_list())


def test_source_rows_reconcile(built: WorkLayout):
    import json

    manifest = json.loads((built.manifest_dir / "inputs.json").read_text(encoding="utf-8"))
    for unit in manifest["inputs"]:
        assert unit["rows_read"] == unit["rows_parsed"] + unit["rows_quarantined"]


def test_one_worker_and_four_workers_agree(tmp_path_factory, built: WorkLayout):
    """The determinism guarantee, checked rather than asserted in a docstring."""
    parallel = run_pipeline(tmp_path_factory.mktemp("parallel"), workers=4)
    assert data_hash(parallel) == data_hash(built)


def test_rerunning_reuses_content_addressed_outputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("resume")
    first = run_pipeline(root, workers=1)
    before = data_hash(first)
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    results = execute(plan_ingest(cfg, first), run_ingest_task, 1)
    assert all(r.reused for r in results), "a second ingest must reuse, not recompute"
    assert data_hash(first) == before


def test_an_interrupted_run_leaves_no_partial_output_in_the_merge(tmp_path_factory):
    """A crash mid-write must not be able to contribute half a file to the result."""
    root = tmp_path_factory.mktemp("crash")
    layout = run_pipeline(root, workers=1)
    before = data_hash(layout)

    # Simulate the debris an interrupted run leaves behind.
    victim = next(layout.source_dir.rglob("*.parquet"))
    shutil.copy(victim, victim.with_suffix(".parquet.partial"))
    (layout.staged_dir / "bucket=0000" / "junk.parquet.partial").write_bytes(b"not parquet")

    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    build_identity(cfg, layout)
    tasks = plan_canonical(cfg, layout, CONFIG, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, 1)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    assert data_hash(layout) == before


def test_changing_the_config_changes_the_content_address(tmp_path_factory):
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=tmp_path_factory.mktemp("addr") / "g", dataset_id="generic_ehr")
    original = plan_ingest(cfg, layout)[0]
    changed = cfg.model_copy(update={"dataset_id": "generic_ehr_v2"})
    other = plan_ingest(changed, layout)[0]
    assert original.digest("f" * 64) != other.digest("f" * 64)


def test_the_merged_layer_matches_the_frozen_schema_exactly(built: WorkLayout):
    """Not one column more: how the work was split is not part of the contract."""
    import pyarrow.parquet as pq

    from ehr2trace.canonical.build import MERGE_TABLES

    for name, schema in MERGE_TABLES.items():
        path = built.canonical_path(name)
        assert path.exists(), name
        assert pq.read_schema(path).names == schema.names, name


def test_staging_reads_the_manifest_not_the_directory(tmp_path_factory):
    """A stale content-addressed file beside a current one must not be staged too.

    This is the failure the content-addressing scheme is meant to prevent, and a
    directory glob quietly undoes it: events deduplicate by id so they look right,
    and only the counts that do not deduplicate come out wrong.
    """
    import shutil

    from ehr2trace.canonical.build import plan_stage

    root = tmp_path_factory.mktemp("stale")
    layout = run_pipeline(root, workers=1)
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)

    before = plan_stage(cfg, layout)
    victim = next(layout.source_dir.rglob("*.parquet"))
    shutil.copy(victim, victim.with_name("deadbeefdeadbeef.parquet"))

    after = plan_stage(cfg, layout)
    assert [t.inputs for t in after] == [t.inputs for t in before], (
        "a leftover from a previous code version was picked up by staging"
    )


def test_staging_refuses_to_run_without_a_manifest(tmp_path_factory):
    from ehr2trace.canonical.build import plan_stage

    root = tmp_path_factory.mktemp("nomanifest")
    layout = run_pipeline(root, workers=1)
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    (layout.manifest_dir / "inputs.json").unlink()
    with pytest.raises(RuntimeError, match="run ingest first"):
        plan_stage(cfg, layout)


def test_staging_refuses_a_manifest_from_a_different_code_version(tmp_path_factory, monkeypatch):
    """The version guard is enforced, not advisory.

    A manifest outlives the code version that produced it. Running canonical against
    source files the current code would address differently is exactly the situation
    content addressing exists to catch, so it stops rather than proceeding.
    """
    from ehr2trace.canonical.build import plan_stage

    root = tmp_path_factory.mktemp("versioned")
    layout = run_pipeline(root, workers=1)
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)
    assert plan_stage(cfg, layout)

    monkeypatch.setattr("ehr2trace.ingest.CODE_VERSION", "99.0.0")
    with pytest.raises(RuntimeError, match="different code or config version"):
        plan_stage(cfg, layout)


def test_cleanup_still_works_when_everything_is_stale(tmp_path_factory, monkeypatch):
    """The command for removing stranded artifacts must not refuse because of them."""
    from ehr2trace.canonical.build import plan_stage

    root = tmp_path_factory.mktemp("cleanstale")
    layout = run_pipeline(root, workers=1)
    os.environ["GENERIC_EHR_ROOT"] = str(FIXTURE)
    cfg = load_dataset_config(CONFIG)

    monkeypatch.setattr("ehr2trace.ingest.CODE_VERSION", "99.0.0")
    with pytest.raises(RuntimeError):
        plan_stage(cfg, layout)
    assert plan_stage(cfg, layout, strict=False) == []


def test_new_source_data_is_not_answered_from_the_previous_build(tmp_path):
    """A rebuild after the data changed must not republish the previous answer.

    The canonical bucket address once described only *how* to build a bucket -- code
    version, config, timezone, bucket count -- and never *what from*. Re-preparing a
    source therefore left every bucket at the address it already had, the build reported
    success, and the new rows were never read. It is silent by construction: counts are
    plausible because they are the previous run's counts, and every check passes because
    the previous run was correct for the data it saw.

    Nothing else in this suite catches it. The reproducibility test varies worker counts
    over identical inputs, which is precisely the case where reusing the earlier answer
    is right.
    """
    root = tmp_path / "data"
    shutil.copytree(FIXTURE, root)
    work = tmp_path / "work"
    previous = os.environ.get("GENERIC_EHR_ROOT")
    os.environ["GENERIC_EHR_ROOT"] = str(root)
    try:
        layout = _run_pipeline_from(root, work)
        before = pl.read_parquet(layout.canonical_path("events")).height

        # One more diagnosis for a patient the fixture already carries -- the shape of
        # change a corrected extract or a fixed preparation script produces.
        diagnoses = root / "site_a" / "diagnoses.csv"
        diagnoses.write_text(
            diagnoses.read_text()
            + "PX-1,J96.01,Acute respiratory failure with hypoxia,2021-01-05 00:00:00,Active\n"
        )

        layout = _run_pipeline_from(root, work)
        after = pl.read_parquet(layout.canonical_path("events")).height
    finally:
        if previous is None:
            os.environ.pop("GENERIC_EHR_ROOT", None)
        else:
            os.environ["GENERIC_EHR_ROOT"] = previous

    assert after == before + 1, (
        f"the added row never reached the canonical layer: {before} events before, "
        f"{after} after. The rebuild answered from the previous build."
    )


def _run_pipeline_from(data_root: Path, work_root: Path) -> WorkLayout:
    """Run the whole pipeline against ``data_root``, into a work root that may be warm."""
    os.environ["GENERIC_EHR_ROOT"] = str(data_root)
    cfg = load_dataset_config(CONFIG)
    layout = WorkLayout(root=work_root / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()
    results = execute(plan_ingest(cfg, layout), run_ingest_task, 1)
    write_manifest(layout, cfg, results)
    build_identity(cfg, layout)
    execute(plan_stage(cfg, layout), run_stage_task, 1)
    tasks = plan_canonical(cfg, layout, CONFIG, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, 1)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    return layout
