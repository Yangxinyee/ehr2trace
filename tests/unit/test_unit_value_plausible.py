"""UNIT_VALUE_PLAUSIBLE counts what the converter withheld, and never reads a number in the wrong unit.

When a normalized value falls outside its plausible range, the converter flags the event
IMPLAUSIBLE and leaves the normalized column empty, keeping the source's number beside it.
That number is in the unit it was read in. A temperature column labelled Celsius but
declared Fahrenheit reads 37.2 -- really Celsius -- as 37.2 degrees Fahrenheit, about 2.9
Celsius, which is withheld. Reading the raw 37.2 against the Celsius range would have
counted the row as plausible. The check's count of values outside a range is the number a
paper quotes, so it must include every row the converter withheld.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import CHECKS, Layers

DATASET = "plausible_check"


@pytest.fixture()
def reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reference"
    (root / "units").mkdir(parents=True)
    (root / "plausible_ranges").mkdir()
    (root / "units" / "test.csv").write_text(
        "source_unit,ucum,basis\nC,Cel,test\nF,[degF],test\n", encoding="utf-8")
    (root / "unit_conversions.csv").write_text(
        "from_ucum,to_ucum,factor,offset,basis\n[degF],Cel,5/9,-160/9,exact\n", encoding="utf-8")
    (root / "plausible_ranges" / f"{DATASET}.csv").write_text(
        "code_system,source_code,ucum,low,high,basis\nSOURCE,TEMP,Cel,25,45,body temperature\n", encoding="utf-8")
    monkeypatch.setenv("EHR_REFERENCE_DIR", str(root))
    return root


def config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": DATASET,
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {"vitals": {
            "adapter": "parquet", "shape": "point_event", "event_kind": "measurement",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                       "source_code": {"from": ["K"]}, "value": {"from": ["V"]}, "unit": {"from": ["U"]}},
        }},
    })


def judge(tmp_path: Path, rows: list[tuple[float, float | None, str | None, list[str]]]):
    """rows: (raw value, normalized value, normalized unit, flags), all labelled Celsius at the source."""
    layout = WorkLayout(root=tmp_path / "work" / DATASET, dataset_id=DATASET).ensure()
    pl.DataFrame(
        {"event_id": [f"e{i}" for i in range(len(rows))], "event_kind": ["measurement"] * len(rows),
         "code_system": ["SOURCE"] * len(rows), "source_code": ["TEMP"] * len(rows),
         "unit_source": ["C"] * len(rows), "value_number": [r[0] for r in rows],
         "value_number_normalized": [r[1] for r in rows], "unit_normalized": [r[2] for r in rows],
         "quality_flags": [r[3] for r in rows]},
        schema={"event_id": pl.String, "event_kind": pl.String, "code_system": pl.String, "source_code": pl.String,
                "unit_source": pl.String, "value_number": pl.Float64, "value_number_normalized": pl.Float64,
                "unit_normalized": pl.String, "quality_flags": pl.List(pl.String)},
    ).write_parquet(layout.canonical_path("events"))
    return dict(CHECKS)["UNIT_VALUE_PLAUSIBLE"][0](Layers.load(config(), layout))


OVERRIDE = ["UNIT_OVERRIDDEN"]


def test_a_row_the_converter_withheld_counts_as_outside(tmp_path: Path, reference: Path):
    result = judge(tmp_path, [
        (98.6, 37.0, "Cel", OVERRIDE),
        (99.1, 37.3, "Cel", OVERRIDE),
        # really Celsius: read as Fahrenheit it is about 2.9 Celsius, withheld and flagged
        (37.2, None, "Cel", OVERRIDE + ["IMPLAUSIBLE"]),
    ])
    assert result.passed, result.detail
    assert (result.metrics["values_judged"], result.metrics["outside"], result.metrics["unflagged"]) == (3, 1, 0)
    assert result.metrics["withheld_by_converter"] == 1
    assert "1 lie outside and every one is flagged" in result.detail


def test_an_unflagged_value_outside_still_fails_beside_a_withheld_one(tmp_path: Path, reference: Path):
    result = judge(tmp_path, [
        (98.6, 37.0, "Cel", OVERRIDE),
        (37.2, None, "Cel", OVERRIDE + ["IMPLAUSIBLE"]),
        (120.0, 48.9, "Cel", OVERRIDE),  # outside and not flagged
    ])
    assert not result.passed
    assert (result.metrics["outside"], result.metrics["unflagged"]) == (2, 1)


def test_a_normalized_unit_without_a_value_or_a_flag_is_reported_not_judged(tmp_path: Path, reference: Path):
    """Its raw number is in the unit it was read in, so it is not compared with the Celsius range."""
    result = judge(tmp_path, [
        (98.6, 37.0, "Cel", OVERRIDE),
        (20.0, None, "Cel", OVERRIDE),
    ])
    assert result.passed, result.detail
    assert (result.metrics["values_judged"], result.metrics["not_judged"], result.metrics["outside"]) == (1, 1, 0)
    assert "not judged" in result.detail
