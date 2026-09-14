"""NOTE_TEXT_UNIQUE judges same-day note repeats per source, against what the source declares.

One export's repeats were a converter defect: the same note under several encounter ids
became several events (P-CU4). Another's are the delivery: separate reports of one
examination written in identical words, each with its own sequence number and time (W6).
The check cannot tell the two apart by looking, so a source states the share it is known
to carry, with a reason, and the check fails only beyond it. The declaration is not
content -- it cannot change a produced byte -- so it stays out of the config hash.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import CHECKS, Layers


def config(declared: dict | None = None) -> DatasetConfig:
    reports = {
        "adapter": "parquet", "shape": "point_event", "event_kind": "note",
        "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                   "source_code": {"from": ["K"]}, "text": {"from": ["X"]}},
    }
    if declared is not None:
        reports["expected_note_repeats"] = declared
    return DatasetConfig.model_validate({
        "dataset_id": "note_check",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {"reports": reports},
    })


def layout_with_notes(root: Path) -> WorkLayout:
    """Ten reports: one text written twice on one day, one text again on another day."""
    layout = WorkLayout(root=root / "note_check", dataset_id="note_check").ensure()
    times = [datetime(2020, 1, d, 9, 0) for d in range(1, 9)] + [datetime(2020, 1, 1, 17, 0), datetime(2020, 2, 1, 9, 0)]
    texts = [f"report {i}" for i in range(8)] + ["report 0", "report 1"]
    pl.DataFrame({
        "event_id": [f"e{i}" for i in range(10)],
        "subject_id": [1] * 10,
        "event_kind": ["note"] * 10,
        "event_time": times,
        "source_code": ["CHEST XR"] * 10,
        "value_text": texts,
        "source_id": ["reports"] * 10,
        "quality_flags": [[] for _ in range(10)],
    }, schema_overrides={"quality_flags": pl.List(pl.String)}).write_parquet(layout.canonical_path("events"))
    return layout


def judge(cfg: DatasetConfig, layout: WorkLayout):
    return dict(CHECKS)["NOTE_TEXT_UNIQUE"][0](Layers.load(cfg, layout))


def test_an_undeclared_repeat_fails_and_reports_its_share(tmp_path: Path):
    result = judge(config(), layout_with_notes(tmp_path))
    source = result.metrics["per_source"]["reports"]
    assert not result.passed
    assert (source["exact_duplicate_groups"], source["surplus_note_events"], source["notes_with_text"]) == (1, 1, 10)
    assert source["share"] == 0.1 and source["max_share"] is None
    assert source["same_text_other_date_groups"] == 1


def test_a_declared_share_passes_at_or_under_it_and_fails_above_it(tmp_path: Path):
    layout = layout_with_notes(tmp_path)
    assert judge(config({"max_share": 0.1, "reason": "separate reports of one study"}), layout).passed
    above = judge(config({"max_share": 0.05, "reason": "separate reports of one study"}), layout)
    assert not above.passed and "declared up to" in above.detail


def test_the_declaration_is_not_content():
    plain = config()
    declared = config({"max_share": 0.01, "reason": "separate reports of one study"})
    assert declared.sources["reports"].expected_note_repeats is not None
    assert plain.config_hash() == declared.config_hash()
