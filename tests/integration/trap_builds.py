"""Whole builds of a fixture dataset, from its YAML or from an edited copy of it.

The fixture traps of remediation plan T1.12 each come as a pair. Built with the fixture's
own declarations, a trap gives the right output and the checks pass. Built without the
declaration that settles it, the same raw rows give the wrong output, or the check that
watches for it fails. The converter from before the remediation no longer exists, so a
missing declaration is how "before" is expressed.

The canonical stage reads its configuration from a file -- every worker loads it again --
so an edited configuration is written out as YAML before it is built, and read back to
confirm that the file says exactly what the edited object says.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import yaml

import meds as meds_spec
from ehr2trace.canonical.build import merge_buckets, plan_canonical, plan_stage, run_canonical_task, run_stage_task
from ehr2trace.config import DatasetConfig, SourceSpec, load_dataset_config
from ehr2trace.hashing import subject_id_from_person_key
from ehr2trace.identity import build_identity
from ehr2trace.ingest import plan_ingest, run_ingest_task, write_manifest
from ehr2trace.meds import build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.run import execute
from ehr2trace.timeutil import TimeContext, parse_utc
from ehr2trace.validate import CHECKS, CheckResult, Layers


@dataclass(frozen=True)
class Build:
    """One built work tree and the configuration it was built with."""

    cfg: DatasetConfig
    layout: WorkLayout

    def canonical(self, name: str = "events") -> pl.DataFrame:
        return pl.read_parquet(self.layout.canonical_path(name))

    def source_rows(self, source_id: str) -> pl.DataFrame:
        """Every parsed row of one source as ingest stored it, from the files the manifest names."""
        manifest = json.loads((self.layout.manifest_dir / "inputs.json").read_text(encoding="utf-8"))
        frames = [
            pl.read_parquet(unit["output_path"]).with_columns(pl.lit(unit["partition_id"]).alias("partition_id"))
            for unit in manifest["inputs"]
            if unit["source_id"] == source_id and unit["rows_parsed"]
        ]
        return pl.concat(frames, how="diagonal_relaxed")

    def omop(self, sql: str, params: list | None = None) -> list[tuple]:
        con = duckdb.connect(str(self.layout.omop_dir / "omop.duckdb"), read_only=True)
        try:
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def meds(self) -> pl.DataFrame:
        return pl.read_parquet(sorted((self.layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))

    def subject(self, person_key: str) -> int:
        return subject_id_from_person_key(self.cfg.dataset_id, person_key, self.cfg.subject_salt())

    def instant(self, cell: object) -> datetime | None:
        """A source cell as the naive UTC instant the build stores, read on the dataset's clock."""
        time = self.cfg.time
        context = TimeContext(tuple(time.formats), tuple(time.null_literals), time.timezone_assumption)
        return parse_utc(cell, context)[0]

    def checks(self, *check_ids: str, cfg: DatasetConfig | None = None) -> dict[str, CheckResult]:
        """The named checks, judged with this build's configuration unless another is given."""
        return run_named_checks(cfg or self.cfg, self.layout, *check_ids)


def edited(cfg: DatasetConfig, source_id: str, **changes: Any) -> DatasetConfig:
    """``cfg`` with fields of one source replaced, validated as a YAML saying so would be."""
    payload = cfg.sources[source_id].model_dump(by_alias=True)
    payload.update(changes)
    spec = SourceSpec.model_validate(payload)
    return cfg.model_copy(update={"sources": {**cfg.sources, source_id: spec}})


def write_config(cfg: DatasetConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(mode="json", by_alias=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    written = load_dataset_config(path)
    assert written.model_dump(mode="json") == cfg.model_dump(mode="json"), (
        f"{path} does not say what the edited configuration says"
    )
    return path


def build(config: Path | DatasetConfig, fixture: Path, work: Path, *, workers: int = 1, publish: bool = True) -> Build:
    """Source through canonical, and OMOP and MEDS when ``publish``; never with a vocabulary."""
    if isinstance(config, DatasetConfig):
        config = write_config(config, work / "config" / f"{config.dataset_id}.yaml")
    cfg = load_dataset_config(config)
    os.environ[cfg.root_env] = str(fixture)
    layout = WorkLayout(root=work / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()
    results = execute(plan_ingest(cfg, layout), run_ingest_task, workers)
    write_manifest(layout, cfg, results)
    build_identity(cfg, layout)
    execute(plan_stage(cfg, layout), run_stage_task, workers)
    tasks = plan_canonical(cfg, layout, config, cfg.time.timezone_assumption, False)
    execute(tasks, run_canonical_task, workers)
    merge_buckets(layout, {t.bucket: t.digest for t in tasks})
    if publish:
        # `vocabulary_dir=None` reads the environment, and a developer with a vocabulary
        # installed must get the build CI gets.
        previous = os.environ.pop("OMOP_VOCAB_DIR", None)
        try:
            build_omop(cfg, layout, vocabulary_dir=None)
            build_meds(cfg, layout)
        finally:
            if previous is not None:
                os.environ["OMOP_VOCAB_DIR"] = previous
    return Build(cfg, layout)


def run_named_checks(cfg: DatasetConfig, layout: WorkLayout, *check_ids: str) -> dict[str, CheckResult]:
    """Only the named checks, each run the way ``run_checks`` runs it, without rewriting its report."""
    registry = dict(CHECKS)
    unknown = sorted(set(check_ids) - set(registry))
    assert not unknown, f"no such checks: {unknown}"
    layers = Layers.load(cfg, layout)
    results: dict[str, CheckResult] = {}
    for check_id in check_ids:
        fn, _slow = registry[check_id]
        result = fn(layers)
        result.check_id = check_id
        results[check_id] = result
    return results
