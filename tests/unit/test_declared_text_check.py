"""A source that declares a text column, and a build where it produced nothing.

The MIMIC-IV demonstration subset is the real case: it ships no note tables, so the two
sources that declare a text column contribute no events. Reporting that they publish
their text would be a claim about a build that published none, which is the shape of
statement this check exists to refuse.
"""

from __future__ import annotations

import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import CHECKS, Layers

BASE = {
    "dataset_id": "t",
    "identity": {"person_key": "PID"},
    "partitions": [{"id": "p1", "dir": "p1"}],
    "time": {"timezone_assumption": "UTC"},
    "sources": {
        "notes": {
            "adapter": "parquet",
            "shape": "point_event",
            "event_kind": "note",
            "fields": {
                "person_id": {"from": ["PID"]},
                "event_time": {"from": ["T"]},
                "source_code": {"from": ["K"]},
                "text": {"from": ["BODY"]},
            },
        }
    },
}

CHECK = dict(CHECKS)["TEXT_SOURCES_PUBLISH_THEIR_TEXT"][0]


def layers(rows: list[dict], tmp_path) -> Layers:
    frame = pl.DataFrame(rows, schema={"source_id": pl.Utf8, "value_text": pl.Utf8}) \
        if rows else pl.DataFrame({"source_id": [], "value_text": []},
                                  schema={"source_id": pl.Utf8, "value_text": pl.Utf8})
    return Layers(
        cfg=DatasetConfig.model_validate(BASE),
        layout=WorkLayout(root=tmp_path, dataset_id="t"),
        events=frame, links_path=None, link_count=None, events_path=None,
        anchors=None, memberships=None, issues=None, quarantine=None, manifest=None,
    )


def test_a_source_that_produced_no_events_is_a_skip_not_a_pass(tmp_path):
    result = CHECK(layers([], tmp_path))
    assert result.skipped, result.detail
    assert "no events" in result.detail


def test_published_text_passes(tmp_path):
    result = CHECK(layers([{"source_id": "notes", "value_text": "Discharge summary."}], tmp_path))
    assert result.passed and not result.skipped, result.detail


def test_events_without_text_fail(tmp_path):
    result = CHECK(layers([{"source_id": "notes", "value_text": None}], tmp_path))
    assert not result.passed, result.detail
    assert "no text at all" in result.detail
