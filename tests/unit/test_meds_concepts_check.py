"""The MEDS and OMOP layers resolve a term alike, or the check says which terms do not.

The case behind it is real: a MEDS stage rerun without the vocabulary the OMOP stage had
published every code as SOURCE/ with no concept, and thirty-nine checks passed on it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
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
        "dx": {
            "adapter": "parquet", "shape": "point_event", "event_kind": "condition",
            "code_system": "ICD10CM",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                       "source_code": {"from": ["K"]}},
        },
        "labs": {
            "adapter": "parquet", "shape": "point_event", "event_kind": "measurement",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                       "source_code": {"from": ["K"]}},
        },
    },
}

CHECK = dict(CHECKS)["MEDS_CONCEPTS_ARE_OMOPS"][0]


def _layers(tmp_path: Path, meds_rows: list[tuple], omop_rows: list[tuple] | None) -> Layers:
    layout = WorkLayout(root=tmp_path, dataset_id="t")
    shard = layout.meds_dir / "data" / "train" / "000000.parquet"
    shard.parent.mkdir(parents=True)
    pl.DataFrame(
        {"source_table": [r[0] for r in meds_rows], "source_code": [r[1] for r in meds_rows],
         "omop_concept_id": [r[2] for r in meds_rows]},
        schema={"source_table": pl.Utf8, "source_code": pl.Utf8, "omop_concept_id": pl.Int64},
    ).write_parquet(shard)
    if omop_rows is not None:
        layout.omop_dir.mkdir(parents=True)
        con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"))
        con.execute("CREATE TABLE person (person_id BIGINT)")
        con.execute("INSERT INTO person VALUES (1)")
        con.execute("CREATE TABLE term_map (code_system VARCHAR, source_code VARCHAR, concept_id BIGINT)")
        con.executemany("INSERT INTO term_map VALUES (?, ?, ?)", omop_rows)
        con.close()
    return Layers(
        cfg=DatasetConfig.model_validate(BASE), layout=layout,
        events=None, links_path=None, link_count=None, events_path=None,
        anchors=None, memberships=None, issues=None, quarantine=None, manifest=None,
    )


def test_without_an_omop_layer_there_is_nothing_to_compare(tmp_path):
    result = CHECK(_layers(tmp_path, [("dx", "E11", 201826)], None))
    assert result.skipped


def test_the_same_concepts_in_both_layers_pass(tmp_path):
    # A combination code has two OMOP rows; MEDS picking one of them is agreement, and
    # so is a term neither layer resolves.
    layers = _layers(
        tmp_path,
        [("dx", "E11", 201826), ("dx", "I1", 316866), ("labs", "K", None)],
        [("ICD10CM", "E11", 201826), ("ICD10CM", "I1", 316866), ("ICD10CM", "I1", 319826)],
    )
    result = CHECK(layers)
    assert result.passed and not result.skipped, result.detail
    assert result.metrics["agreeing"] == 3 and result.metrics["mapped_in_meds"] == 2


def test_a_meds_layer_that_lost_its_concepts_fails_and_names_the_terms(tmp_path):
    layers = _layers(
        tmp_path,
        [("dx", "E11", None), ("labs", "K", None)],
        [("ICD10CM", "E11", 201826)],
    )
    result = CHECK(layers)
    assert not result.passed, result.detail
    assert "dx/E11" in result.detail and result.metrics["disagreeing"] == 1


def test_a_concept_the_omop_layer_never_assigned_fails(tmp_path):
    layers = _layers(tmp_path, [("dx", "E11", 999)], [("ICD10CM", "E11", 201826)])
    assert not CHECK(layers).passed


def test_the_same_code_string_under_another_source_is_another_term(tmp_path):
    # `E11` from a source with no code system is a SOURCE term, unresolved in both
    # layers; the resolved ICD10CM term of the same spelling does not make it a miss.
    layers = _layers(
        tmp_path,
        [("dx", "E11", 201826), ("labs", "E11", None)],
        [("ICD10CM", "E11", 201826)],
    )
    assert CHECK(layers).passed
