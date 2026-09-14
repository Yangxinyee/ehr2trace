"""DUPLICATES_AGREE confirms a declared merge rule was applied, not merely declared.

A merge rule in a dataset's YAML says how rows that collapsed into one event are settled.
Excluding the ruled field from the comparison is right for a build that applied the rule,
and exactly wrong for a build made before the rule was written: its rows disagree, nothing
settled them, and the check would call it clean. So the ruled field is compared too, and
the event is asked for the evidence the rule leaves -- the flag it writes when it fires, or
for a priority the value it keeps.

The layer here is four files written by hand, so the rows, the events and their flags are
exactly where the assertions say they are.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.validate import merge_disagreements

EVENT_SCHEMA = {"event_id": pl.String, "source_id": pl.String, "event_kind": pl.String,
                "status_source": pl.String, "quality_flags": pl.List(pl.String)}


def config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": "rule_check",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {
            "orders": {
                "adapter": "parquet", "shape": "point_event", "event_kind": "drug_order",
                "fields": {
                    "person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                    "source_code": {"from": ["K"]}, "status": {"from": ["S"]}, "end_time": {"from": ["E"]},
                },
                "merge_rules": {
                    "status_source": {"rule": "priority", "order": ["Resolved", "Active"]},
                    "end_time": "earliest",
                },
            },
        },
    })


def write_events(path: Path, rows: list[tuple[str, str, str | None, list[str]]]) -> None:
    pl.DataFrame(
        {"event_id": [r[0] for r in rows], "source_id": ["orders"] * len(rows), "event_kind": [r[1] for r in rows],
         "status_source": [r[2] for r in rows], "quality_flags": [r[3] for r in rows]},
        schema=EVENT_SCHEMA,
    ).write_parquet(path)


def layer(root: Path, events: list[tuple[str, str, str | None, list[str]]] | None = None,
          extra_links: list[tuple[str, str]] = ()) -> tuple[dict, Path, duckdb.DuckDBPyConnection]:
    """Two merged orders whose rows disagree on status and end time, and one order alone.

    e1 was merged the way its rules say: it kept the higher-priority status and carries the
    flag the earliest rule writes. e2 was merged by arrival order: it kept the status the
    order ranks lower, and carries nothing.
    """
    source = root / "orders.parquet"
    pl.DataFrame({
        "source_row_id": ["r1", "r2", "r3", "r4", "r5"],
        "col__PID": ["A", "A", "B", "B", "C"],
        "col__T": ["2020-01-01 10:00:00"] * 5,
        "col__K": ["X"] * 5,
        "col__S": ["Active", "Resolved", "Active", "Resolved", "Active"],
        "col__E": ["2020-01-02 00:00:00", "2020-01-03 00:00:00", "2020-01-02 00:00:00", "2020-01-03 00:00:00", None],
    }).write_parquet(source)
    links = root / "event_source.parquet"
    pairs = [("e1", "r1"), ("e1", "r2"), ("e2", "r3"), ("e2", "r4"), ("e3", "r5"), *extra_links]
    pl.DataFrame({
        "event_id": [e for e, _ in pairs], "source_row_id": [r for _, r in pairs], "partition_id": ["p1"] * len(pairs),
    }).write_parquet(links)
    events_path = root / "events.parquet"
    write_events(events_path, events or [
        ("e1", "drug_order", "Resolved", ["AVAILABILITY_MERGED"]),
        ("e2", "drug_order", "Active", []),
        ("e3", "drug_order", "Active", []),
    ])
    con = duckdb.connect()
    con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{events_path}')")
    manifest = {"inputs": [{"source_id": "orders", "partition_id": "p1", "output_path": str(source), "rows_parsed": 5}]}
    return manifest, links, con


def test_a_rule_the_build_applied_passes_and_one_it_did_not_is_counted(tmp_path: Path):
    manifest, links, con = layer(tmp_path)
    report = merge_disagreements(config(), manifest, links, con)["orders"]

    assert report["ruled"] == ["end_time", "status_source"]
    assert report["ruled_disagreements"] == {"end_time": 2, "status_source": 2}
    # e2 disagrees on both and shows neither the flag nor the priority's choice.
    assert report["rules_not_applied"] == {"end_time": 1, "status_source": 1}
    # A ruled field is never also an unruled disagreement.
    assert "status" not in report["disagreements"] and "end_time" not in report["disagreements"]


def test_a_merge_conflict_the_build_recorded_is_evidence_it_looked(tmp_path: Path):
    """A build that could not settle a field says so with MERGE_CONFLICT, which fails elsewhere."""
    manifest, links, con = layer(tmp_path, events=[
        ("e1", "drug_order", "Resolved", ["AVAILABILITY_MERGED"]),
        ("e2", "drug_order", "Active", ["MERGE_CONFLICT"]),
        ("e3", "drug_order", "Active", []),
    ])
    report = merge_disagreements(config(), manifest, links, con)["orders"]
    assert report["rules_not_applied"] == {}


def test_an_event_of_another_kind_built_from_the_same_rows_is_not_asked(tmp_path: Path):
    """A visit table's rows also carry a death; what they disagree on is the visit's business."""
    manifest, links, con = layer(tmp_path, extra_links=[("e4", "r3"), ("e4", "r4")], events=[
        ("e1", "drug_order", "Resolved", ["AVAILABILITY_MERGED"]),
        ("e2", "drug_order", "Active", []),
        ("e3", "drug_order", "Active", []),
        ("e4", "death", None, []),
    ])
    report = merge_disagreements(config(), manifest, links, con)["orders"]
    assert report["ruled_disagreements"] == {"end_time": 3, "status_source": 3}
    assert report["rules_not_applied"] == {"end_time": 1, "status_source": 1}


def test_a_rule_that_leaves_nothing_to_read_is_reported_unverifiable(tmp_path: Path):
    manifest, links, con = layer(tmp_path)
    cfg = config()
    spec = cfg.sources["orders"]
    rules = dict(spec.merge_rules)
    rules["status_source"] = rules["status_source"].model_copy(update={"order": []})
    cfg = cfg.model_copy(update={"sources": {"orders": spec.model_copy(update={"merge_rules": rules})}})

    report = merge_disagreements(cfg, manifest, links, con)["orders"]
    assert report["rules_unverifiable"] == ["status_source"]
    assert report["rules_not_applied"] == {"end_time": 1}
