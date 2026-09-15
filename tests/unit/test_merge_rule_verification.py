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
    # The death is left out entirely: its rows' disagreements are not the orders' to settle.
    assert report["ruled_disagreements"] == {"end_time": 2, "status_source": 2}
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


def results_config() -> DatasetConfig:
    return DatasetConfig.model_validate({
        "dataset_id": "availability_check",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}, {"id": "p2", "dir": "p2"}],
        "time": {"timezone_assumption": "America/New_York"},
        "sources": {
            "results": {
                "adapter": "parquet", "shape": "point_event", "event_kind": "measurement",
                "fields": {
                    "person_id": {"from": ["PID"]}, "event_time": {"from": ["COLLECTED"]},
                    "available_time": {"from": ["RESULTED"]}, "source_code": {"from": ["K"]},
                },
                "merge_rules": {"available_time": "earliest"},
            },
        },
    })


def test_result_times_that_all_precede_the_collection_need_no_trace_of_the_rule(tmp_path: Path):
    """One result delivered by two batches, reported 6 minutes apart, both before the specimen was taken.

    The converter moves each row's availability up to the collection time and flags the
    contradiction, so both rows reach the merge with one availability and the merge writes
    nothing. A result time after the collection is still a disagreement the merge must have
    settled. Times are local (New York) in the source and naive UTC on the event.
    """
    from datetime import datetime

    source = tmp_path / "results.parquet"
    collected = "2020-01-01 10:00:00"  # 15:00 UTC
    pl.DataFrame({
        "source_row_id": ["r1", "r2", "r3", "r4", "r5", "r6"],
        "col__PID": ["A"] * 6,
        "col__K": ["K"] * 6,
        "col__COLLECTED": [collected] * 6,
        # both before; one before and one after; both after
        "col__RESULTED": ["2020-01-01 09:50:00", "2020-01-01 09:56:00",
                          "2020-01-01 09:50:00", "2020-01-01 10:30:00",
                          "2020-01-01 10:10:00", "2020-01-01 10:20:00"],
    }).write_parquet(source)
    links = tmp_path / "event_source.parquet"
    pl.DataFrame({
        "event_id": ["before", "before", "straddle", "straddle", "after", "after"],
        "source_row_id": ["r1", "r2", "r3", "r4", "r5", "r6"],
        "partition_id": ["p1", "p2"] * 3,
    }).write_parquet(links)
    events = tmp_path / "events.parquet"
    pl.DataFrame(
        {"event_id": ["before", "straddle", "after"], "source_id": ["results"] * 3,
         "event_kind": ["measurement"] * 3, "event_time": [datetime(2020, 1, 1, 15, 0)] * 3,
         "quality_flags": [["AVAILABILITY_BEFORE_EVENT"], ["AVAILABILITY_BEFORE_EVENT"], ["AVAILABILITY_MERGED"]]},
        schema={"event_id": pl.String, "source_id": pl.String, "event_kind": pl.String,
                "event_time": pl.Datetime("us"), "quality_flags": pl.List(pl.String)},
    ).write_parquet(events)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{events}')")
    manifest = {"inputs": [{"source_id": "results", "partition_id": "p1", "output_path": str(source), "rows_parsed": 6}]}

    report = merge_disagreements(results_config(), manifest, links, con)["results"]
    assert report["ruled_disagreements"] == {"available_time": 3}, "the raw result times differ in all three"
    # "before": both moved to 15:00 UTC, one value, nothing to settle. "after": settled and
    # flagged. "straddle": 15:00 and 15:30 UTC reached the merge, and nothing shows it was settled.
    assert report["rules_not_applied"] == {"available_time": 1}


def test_result_times_in_the_repeated_hour_are_placed_as_the_converter_places_them(tmp_path: Path):
    """On the day New York falls back, 01:30 happens twice. The converter reads it as the first.

    Both result times precede the collection as the converter places them (fold=0), so the
    merge saw one availability and needs no trace. The query engine's own conversion would
    have placed them an hour later, after the collection, and asked for one. A pair with one
    result after the repeated hour did reach the merge unsettled, and is still counted.
    """
    from datetime import datetime, timezone as tz
    from zoneinfo import ZoneInfo

    def utc(wall: datetime) -> datetime:
        return wall.replace(tzinfo=ZoneInfo("America/New_York"), fold=0).astimezone(tz.utc).replace(tzinfo=None)

    source = tmp_path / "results.parquet"
    pl.DataFrame({
        "source_row_id": ["r1", "r2", "r3", "r4", "r5", "r6"],
        "col__PID": ["A"] * 6,
        "col__K": ["K"] * 6,
        "col__COLLECTED": ["2020-11-01 01:50:00"] * 6,
        "col__RESULTED": ["2020-11-01 01:30:00", "2020-11-01 01:36:00",   # both before, in the repeated hour
                          "2020-11-01 01:40:00", "2020-11-01 02:20:00",   # one after the repeated hour
                          "2020-11-01 02:10:00", "2020-11-01 02:30:00"],  # both after, settled
    }).write_parquet(source)
    links = tmp_path / "event_source.parquet"
    pl.DataFrame({
        "event_id": ["before", "before", "straddle", "straddle", "after", "after"],
        "source_row_id": ["r1", "r2", "r3", "r4", "r5", "r6"],
        "partition_id": ["p1", "p2"] * 3,
    }).write_parquet(links)
    events = tmp_path / "events.parquet"
    pl.DataFrame(
        {"event_id": ["before", "straddle", "after"], "source_id": ["results"] * 3,
         "event_kind": ["measurement"] * 3, "event_time": [utc(datetime(2020, 11, 1, 1, 50))] * 3,
         "quality_flags": [["AVAILABILITY_BEFORE_EVENT"], ["AVAILABILITY_BEFORE_EVENT"], ["AVAILABILITY_MERGED"]]},
        schema={"event_id": pl.String, "source_id": pl.String, "event_kind": pl.String,
                "event_time": pl.Datetime("us"), "quality_flags": pl.List(pl.String)},
    ).write_parquet(events)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{events}')")
    manifest = {"inputs": [{"source_id": "results", "partition_id": "p1", "output_path": str(source), "rows_parsed": 6}]}

    report = merge_disagreements(results_config(), manifest, links, con)["results"]
    assert report["ruled_disagreements"] == {"available_time": 3}
    assert report["rules_not_applied"] == {"available_time": 1}
