"""A check's result depends on the build, not on the order the engine returns rows in.

Proving that a memory change left every check result unchanged showed three results that
changed between two runs of the same code on the same build. QUARANTINE_IS_EXPLAINED paired
the reasons of one grouping with the counts of another, whose order is not defined, and
reported counts under the wrong reasons. UNIT_VALUE_PLAUSIBLE, temperature_like_summary,
EXCLUDED_STATUS_NOT_PUBLISHED and UNIT_HOMOGENEOUS_PER_CODE listed rows tied on their counts
in whatever order they arrived. Each now has a total order.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import QUARANTINE_SCHEMA
from ehr2trace.validate import CHECKS, Layers


def config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": "determinism_check",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {"s": {
            "adapter": "parquet", "shape": "point_event", "event_kind": "measurement",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]}, "source_code": {"from": ["K"]}},
        }},
    })


def test_quarantine_counts_stay_with_their_reasons(tmp_path: Path):
    layout = WorkLayout(root=tmp_path / "work" / "determinism_check", dataset_id="determinism_check").ensure()
    reasons = ["MISSING_EVENT_TIME"] * 4 + ["UNTIMED_CLINICAL_VALUE"] * 7 + ["UNTIMED_VITAL_STATUS"] * 21
    frame = pl.DataFrame({name: [None] * len(reasons) for name in QUARANTINE_SCHEMA.names},
                         schema={name: pl.String for name in QUARANTINE_SCHEMA.names})
    frame = frame.with_columns(pl.Series("reason", reasons), pl.lit("s").alias("source_id"))
    frame.sample(fraction=1.0, shuffle=True, seed=7).write_parquet(layout.canonical_path("quarantine"))

    check = dict(CHECKS)["QUARANTINE_IS_EXPLAINED"][0]
    for _ in range(5):
        result = check(Layers.load(config(), layout))
        assert result.metrics["by_reason"] == {
            "MISSING_EVENT_TIME": 4, "UNTIMED_CLINICAL_VALUE": 7, "UNTIMED_VITAL_STATUS": 21,
        }
