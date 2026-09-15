"""CODE_DESCRIPTION_IS_REPRESENTATIVE ranks a source code's names the way the publisher does.

The MEDS publisher describes a SOURCE code by the name its rows carry most often, with ties
broken alphabetically, counted over the rows filed under that code -- after the vocabulary
has mapped some spellings of the same normalized code to an OMOP concept instead. The check
once took the first of two tied names in whatever order the query returned them, and
counted names over every spelling of the normalized code. JHU-CTPE's final build showed the
first: two spellings of one eye drop, one row each, "(PF)" and "PF", and the publisher
rightly chose the one that sorts first.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.terminology import normalize_term
from ehr2trace.validate import CHECKS, Layers


def config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": "description_check",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {"rx": {
            "adapter": "parquet", "shape": "point_event", "event_kind": "drug_order",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                       "source_code": {"from": ["NAME"]}, "source_name": {"from": ["NAME"]}},
        }},
    })


def source_code(spelling: str) -> str:
    return f"SOURCE/rx/{normalize_term(spelling)}"


def build(tmp_path: Path, events: list[tuple[str, str, str]], meds_rows: list[tuple[str, str]],
          descriptions: dict[str, tuple[str, int | None]]) -> WorkLayout:
    """events: (spelling, name, kind); meds_rows: (code, spelling); descriptions: code -> (text, concept)."""
    layout = WorkLayout(root=tmp_path / "work" / "description_check", dataset_id="description_check").ensure()
    pl.DataFrame({
        "event_id": [f"e{i}" for i in range(len(events))],
        "source_id": ["rx"] * len(events),
        "source_code": [e[0] for e in events],
        "source_name": [e[1] for e in events],
        "event_kind": [e[2] for e in events],
    }).write_parquet(layout.canonical_path("events"))
    (layout.meds_dir / "data").mkdir(parents=True, exist_ok=True)
    (layout.meds_dir / "metadata").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "subject_id": [1] * len(meds_rows), "code": [r[0] for r in meds_rows], "source_code": [r[1] for r in meds_rows],
        "unit_normalized": [None] * len(meds_rows),
    }, schema_overrides={"unit_normalized": pl.String}).write_parquet(layout.meds_dir / "data" / "0.parquet")
    pl.DataFrame({
        "code": list(descriptions), "description": [d[0] for d in descriptions.values()],
        "omop_concept_id": [d[1] for d in descriptions.values()],
    }, schema_overrides={"omop_concept_id": pl.Int64}).write_parquet(layout.meds_dir / "metadata" / "codes.parquet")
    (layout.meds_dir / "metadata" / "dataset.json").write_text(json.dumps({"extension_columns_version": 2}), encoding="utf-8")
    return layout


@pytest.fixture(autouse=True)
def no_vocabulary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OMOP_VOCAB_DIR", raising=False)


def judge(layout: WorkLayout):
    return dict(CHECKS)["CODE_DESCRIPTION_IS_REPRESENTATIVE"][0](Layers.load(config(), layout))


def test_two_names_tied_on_their_count_are_decided_alphabetically(tmp_path: Path):
    plain, marked = "TESTDROP PF 1 % EYE DROPS", "TESTDROP (PF) 1 % EYE DROPS"
    assert normalize_term(plain) == normalize_term(marked)
    code = source_code(plain)
    # The name that sorts second is written first, so arrival order would pick it.
    layout = build(tmp_path, [(plain, plain, "drug_order"), (marked, marked, "drug_order")],
                   [(code, plain), (code, marked)], {code: (marked, None)})
    result = judge(layout)
    assert result.passed, result.detail
    assert result.metrics["source_codes_tied_on_most_frequent_name"] == 1


def test_a_spelling_the_vocabulary_mapped_is_not_counted_for_the_source_code(tmp_path: Path):
    mapped, unmapped = "TESTTAB XR 5 MG", "TESTTAB (XR) 5 MG"
    assert normalize_term(mapped) == normalize_term(unmapped)
    code = source_code(unmapped)
    events = [(mapped, mapped, "drug_order")] * 3 + [(unmapped, unmapped, "drug_order")]
    meds = [("OMOP/123", mapped)] * 3 + [(code, unmapped)]
    layout = build(tmp_path, events, meds, {code: (unmapped, None), "OMOP/123": ("testtab", 123)})
    result = judge(layout)
    assert result.passed, result.detail
    assert result.metrics["source_codes_agreeing_once_mapped_spellings_are_left_out"] == 1


def test_a_code_described_by_its_rarer_name_still_fails(tmp_path: Path):
    common, rare = "TESTCAP 10 MG", "testcap 10 mg capsule, old label"
    code = source_code(common)
    events = [(common, common, "drug_order")] * 3 + [(common, rare, "drug_order")] + [(common, "PATIENT DIED", "death")] * 5
    layout = build(tmp_path, events, [(code, common)] * 4, {code: (rare, None)})
    result = judge(layout)
    assert not result.passed
    assert result.metrics["wrong_source"] == 1
