"""Concurrency settings say how to run, never what to produce.

The content address exists so that identical inputs are not recomputed. It once covered
`execution` -- worker count, chunk size, bucket count -- which meant that lowering the
worker count to survive an out-of-memory kill invalidated a fifty-three-minute ingest
whose output was byte-identical either way.

The correction has a trap in it, and both halves are pinned here: `bucket_count` cannot
change the merged canonical layer, because merging sorts and is order-independent, but
it does decide which subject lands in which bucket file. Drop it from the config hash
without putting it into the per-task digests and a run resumed under a different
partitioning will reuse a bucket computed under the old one -- silently, since the file
is exactly where it is expected to be.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ehr2trace.canonical.build import CanonicalTask, StageTask
from ehr2trace.config import load_dataset_config

CONFIG = Path(__file__).resolve().parents[2] / "datasets" / "ctpe_shape.yaml"


def config_with(tmp_path: Path, **execution):
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw.setdefault("execution", {}).update(execution)
    path = tmp_path / "variant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_dataset_config(path)


@pytest.mark.parametrize("setting", ["workers", "chunk_rows", "bucket_count"])
def test_an_execution_setting_does_not_change_the_config_hash(tmp_path, setting):
    base = load_dataset_config(CONFIG)
    variant = config_with(tmp_path, **{setting: {"workers": 99, "chunk_rows": 7, "bucket_count": 256}[setting]})
    assert base.config_hash() == variant.config_hash()


def test_a_semantic_change_still_changes_the_config_hash(tmp_path):
    """The guard above must not have made the hash indifferent to everything."""
    base = load_dataset_config(CONFIG)
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = next(iter(raw["sources"]))
    raw["sources"][source]["code_system"] = "SOMETHING_ELSE"
    path = tmp_path / "semantic.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert base.config_hash() != load_dataset_config(path).config_hash()


def _stage(bucket_count: int) -> StageTask:
    return StageTask(
        dataset_id="d",
        partition_id="p",
        source_id="s",
        inputs=["a.parquet"],
        work_root="/tmp/w",
        config_hash="abc",
        bucket_count=bucket_count,
    )


def test_the_bucket_count_still_reaches_the_stage_digest():
    """Otherwise a resumed run reuses a bucket built under a different partitioning."""
    assert _stage(64).digest != _stage(256).digest


def _canonical(bucket: int, bucket_count: int) -> CanonicalTask:
    return CanonicalTask(
        dataset_id="d",
        bucket=bucket,
        work_root="/tmp/w",
        config_path="/tmp/c.yaml",
        config_hash="abc",
        timezone_name="UTC",
        timezone_assumed=False,
        bucket_count=bucket_count,
    )


def test_bucket_five_of_sixty_four_is_not_bucket_five_of_two_hundred_and_fifty_six():
    """The same index under a different partitioning holds different subjects."""
    assert _canonical(5, 64).digest != _canonical(5, 256).digest


def test_the_same_bucket_under_the_same_partitioning_is_the_same_work():
    assert _canonical(5, 64).digest == _canonical(5, 64).digest
