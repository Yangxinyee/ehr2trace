"""What DUPLICATES_AGREE counts as two merged rows disagreeing.

Two rows that became one event were given the same identity, and the identity hashes each
cell in its canonical form (``hashing.canonical_cell``): an integral number is the same
number however a workbook typed it, a timestamp is the same instant however it was
written, and a null literal is nothing. The check compares cells in that same form, so
it can only report a disagreement the identity could also have seen -- and it keeps the
differences the identity keeps, such as case.

A row can also feed more than one event: a visit table's rows each make a visit, and the
rows of one patient that name the same death make one death between them. The death
merged rows that differ in every visit attribute, correctly; those are not the visit's
disagreements, and the check compares a source's rows only on the events of the kinds
that source declares.

This file pins both, on a layer written by hand. The JHU-CTPE build of 2026-09-13
reported 1,252 encounter-id and 685 visit-type disagreements in its visit sheet that were
exactly this: death events, each merging one patient's several visits.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.validate import merge_disagreements


def config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": "merge_check",
        "identity": {"person_key": "PID", "encounter_key": "CSN"},
        "partitions": [{"id": "p1", "dir": "p1"}, {"id": "p2", "dir": "p2"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {
            "stays": {
                "adapter": "parquet", "shape": "visit", "event_kind": "visit",
                "fields": {
                    "person_id": {"from": ["PID"]}, "event_time": {"from": ["ADMIT"]},
                    "encounter_id": {"from": ["CSN"]}, "visit_type": {"from": ["TYPE"]},
                    "length_of_stay": {"from": ["LOS"]}, "death_time": {"from": ["DIED"]},
                },
            },
        },
    })


def layer(root: Path, rows: list[dict], links: list[tuple[str, str, str]], events: list[tuple[str, str]]):
    source = root / "stays.parquet"
    pl.DataFrame(rows).write_parquet(source)
    links_path = root / "event_source.parquet"
    pl.DataFrame({
        "event_id": [e for e, _r, _p in links], "source_row_id": [r for _e, r, _p in links],
        "partition_id": [p for _e, _r, p in links],
    }).write_parquet(links_path)
    events_path = root / "events.parquet"
    pl.DataFrame(
        {"event_id": [e for e, _k in events], "event_kind": [k for _e, k in events],
         "source_id": ["stays"] * len(events), "quality_flags": [[] for _ in events]},
        schema={"event_id": pl.String, "event_kind": pl.String, "source_id": pl.String,
                "quality_flags": pl.List(pl.String)},
    ).write_parquet(events_path)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{events_path}')")
    manifest = {"inputs": [{"source_id": "stays", "partition_id": "p1", "output_path": str(source), "rows_parsed": len(rows)}]}
    return manifest, links_path, con


def row(rid: str, csn: str, los: str, admit: str, kind: str = "Inpatient", died: str = "") -> dict:
    return {"source_row_id": rid, "col__PID": "A", "col__CSN": csn, "col__LOS": los,
            "col__ADMIT": admit, "col__TYPE": kind, "col__DIED": died}


def test_a_number_or_a_time_written_two_ways_is_one_value(tmp_path: Path):
    """One admission extracted twice: a CSN typed as an integer and as a float, a length of
    stay the same way, and a discharge time with and without its fraction of a second."""
    manifest, links, con = layer(
        tmp_path,
        rows=[row("r1", "123", "5", "2019-03-01 09:30:00.000", died="NULL"),
              row("r2", "123.0", "5.0", "2019-03-01T09:30", died="")],
        links=[("v1", "r1", "p1"), ("v1", "r2", "p2")],
        events=[("v1", "visit")],
    )
    report = merge_disagreements(config(), manifest, links, con)["stays"]
    assert report["merged_events"] == 1
    assert report["disagreements"] == {}


def test_case_is_a_difference_as_the_identity_sees_it(tmp_path: Path):
    manifest, links, con = layer(
        tmp_path,
        rows=[row("r1", "123", "5", "2019-03-01", kind="Inpatient"),
              row("r2", "123", "5", "2019-03-01", kind="INPATIENT")],
        links=[("v1", "r1", "p1"), ("v1", "r2", "p2")],
        events=[("v1", "visit")],
    )
    report = merge_disagreements(config(), manifest, links, con)["stays"]
    assert report["disagreements"] == {"visit_type": 1}


def test_an_event_of_another_kind_built_from_the_rows_is_not_their_disagreement(tmp_path: Path):
    """Two visits of one patient, both naming the day the patient died: two visits and one death."""
    manifest, links, con = layer(
        tmp_path,
        rows=[row("r1", "456", "3", "2019-01-01", kind="Inpatient", died="2020-01-01"),
              row("r2", "789", "8", "2019-06-01", kind="Emergency", died="2020-01-01")],
        links=[("v1", "r1", "p1"), ("v2", "r2", "p1"), ("d1", "r1", "p1"), ("d1", "r2", "p1")],
        events=[("v1", "visit"), ("v2", "visit"), ("d1", "death")],
    )
    report = merge_disagreements(config(), manifest, links, con)["stays"]
    assert report["events"] == 2 and report["merged_events"] == 0
    assert report["disagreements"] == {}
