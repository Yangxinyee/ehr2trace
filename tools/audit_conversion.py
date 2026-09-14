"""The conversion audit of 2026-09-13, as a command anyone can re-run.

That audit was a few hundred ad-hoc queries in a terminal. Everything it concluded --
that one export collapsed 1,118,171 drug rows into fewer events, that 11,402 deaths
never reached a DEATH row, that an entire intensive-care module was never read -- rests
on queries nobody can reproduce, which is a poor foundation for a remediation that will
be judged by whether those numbers move.

So the queries live here. The tool reads the built layers of one dataset, read-only,
and writes one JSON document of aggregates: what each source produced, which fields the
rows behind one event disagree on, how units and visits are coded, where doses lose
their unit, whether every death is published, which notes repeat, how many encounter
ids resolve, and what the delivery holds that nothing reads. Run before and after the
fix, the two documents are the evidence.

It shares its summaries with ``src/ehr2trace/validate.py``: the checks decide pass or
fail against a declared threshold, this reports the distribution behind that decision,
and both read the same function so the two can never drift apart.

Identity: aggregates only. No patient identifier, no note text, no cell of any text
column is written -- a text is counted and hashed, never carried -- and the source
parquet is read in the query engine with a projection rather than loaded.

    python tools/audit_conversion.py --dataset ctpe
    python tools/audit_conversion.py --dataset mimiciv --out results/mimiciv/audit.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ehr2trace.analytics import HEAVY_THREADS, analytic_connection  # noqa: E402
from ehr2trace.config import load_dataset_config  # noqa: E402
from ehr2trace.paths import WorkLayout  # noqa: E402
from ehr2trace.reference import load_reference  # noqa: E402
from ehr2trace.schema import EventKind  # noqa: E402
from ehr2trace.validate import (  # noqa: E402
    DRUG_KINDS,
    VISIT_KINDS,
    artifact_in_this_tree,
    death_local_dates,
    encounter_link_rates,
    encounter_reference,
    merge_disagreements,
    note_duplicate_groups,
    raw_coverage,
    reference_root,
    reportable_spelling,
    temperature_like_summary,
    unit_spellings_per_code,
    _local_date_sql,
    _sql_str,
)

#: Where the engine spills. Inside the work root, beside the other remediation output:
#: these spills are the size of the dataset, and never inside a built dataset directory,
#: which this tool treats as read-only.
SCRATCH_SUBDIR = Path("_remediation") / "validation" / "scratch"

#: How many codes' unit spellings are reported. The point is to see the spread of
#: spellings on the codes that carry the data, not to enumerate a vocabulary.
TOP_CODES = 50


def _work_root() -> Path:
    raw = os.environ.get("EHR_WORK_ROOT")
    if not raw:
        raise SystemExit("EHR_WORK_ROOT is not set: it must point at the directory holding the builds")
    return Path(raw).expanduser()


def _head() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return None


def _manifest(layout: WorkLayout) -> dict[str, Any] | None:
    path = layout.manifest_dir / "inputs.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sources(con, layout: WorkLayout, manifest: dict[str, Any] | None, has_events: bool) -> dict[str, Any]:
    """Per source: what was read, what became events, what was quarantined and why."""
    out: dict[str, dict[str, Any]] = {}
    for unit in (manifest or {}).get("inputs", []):
        entry = out.setdefault(unit["source_id"], {
            "rows_read": 0, "rows_parsed": 0, "rows_quarantined_at_ingest": 0,
            "partitions": [], "events": 0, "linked_rows": 0, "events_from_several_rows": 0,
            "quarantine": {},
        })
        entry["rows_read"] += int(unit.get("rows_read") or 0)
        entry["rows_parsed"] += int(unit.get("rows_parsed") or 0)
        entry["rows_quarantined_at_ingest"] += int(unit.get("rows_quarantined") or 0)
        if unit["partition_id"] not in entry["partitions"]:
            entry["partitions"].append(unit["partition_id"])
    if has_events:
        for source_id, events, merged in con.execute(
            """
            WITH per_event AS (
                SELECT e.source_id, e.event_id, count(k.source_row_id) AS rows
                FROM evt e LEFT JOIN lnk k USING (event_id) GROUP BY 1, 2
            )
            SELECT source_id, count(*), count(*) FILTER (WHERE rows > 1) FROM per_event GROUP BY 1
            """
        ).fetchall():
            entry = out.setdefault(str(source_id), {})
            entry["events"] = int(events)
            entry["events_from_several_rows"] = int(merged)
        for source_id, links in con.execute(
            "SELECT e.source_id, count(*) FROM lnk k JOIN evt e USING (event_id) GROUP BY 1"
        ).fetchall():
            out.setdefault(str(source_id), {})["linked_rows"] = int(links)
    quarantine = layout.canonical_path("quarantine")
    if quarantine.exists():
        for source_id, reason, rows in con.execute(
            f"SELECT source_id, reason, count(DISTINCT source_row_id) FROM read_parquet('{quarantine}') GROUP BY 1, 2"
        ).fetchall():
            out.setdefault(str(source_id), {}).setdefault("quarantine", {})[str(reason)] = int(rows)
    for unit in (manifest or {}).get("inputs", []):
        path = artifact_in_this_tree(layout, unit.get("quarantine_path"))
        if path is None:
            continue
        for reason, rows in con.execute(
            f"SELECT reason, count(*) FROM read_parquet('{path}') GROUP BY 1"
        ).fetchall():
            per = out.setdefault(unit["source_id"], {}).setdefault("quarantine", {})
            key = f"ingest/{reason}"
            per[key] = per.get(key, 0) + int(rows)
    for entry in out.values():
        parsed = entry.get("rows_parsed") or 0
        entry["quarantine_share"] = {
            reason: round(rows / parsed, 4) for reason, rows in sorted(entry.get("quarantine", {}).items())
        } if parsed else {}
    return out


def _units(con, omop, columns: set[str], dataset_id: str) -> dict[str, Any]:
    """Unit concept coverage, the spellings each code carries, and unknown spellings."""
    out: dict[str, Any] = {}
    if omop is not None:
        total, with_unit, with_concept = omop.execute(
            "SELECT count(*), count(*) FILTER (WHERE unit_source_value IS NOT NULL), "
            "count(*) FILTER (WHERE unit_source_value IS NOT NULL AND unit_concept_id <> 0) FROM measurement"
        ).fetchone()
        out["omop_measurement"] = {
            "rows": int(total or 0), "with_unit_source_value": int(with_unit or 0),
            "with_unit_concept": int(with_concept or 0),
            "coverage": round(int(with_concept or 0) / int(with_unit), 4) if with_unit else None,
        }
    out["per_code"] = unit_spellings_per_code(con, columns, top=TOP_CODES)
    out["temperature_like"] = temperature_like_summary(con, columns)
    tables = load_reference(None, dataset_id)
    units = tables.units
    if not units.by_spelling and not units.not_units:
        out["unit_table"] = {"loaded": False, "directory": str(reference_root() / "units")}
        return out
    rows = con.execute(
        "SELECT trim(unit_source) AS u, count(*) FROM evt WHERE unit_source IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    undeclared = [(str(u), int(n)) for u, n in rows if not units.declared(u)]
    out["unit_table"] = {
        "loaded": True, "directory": str(reference_root() / "units"), "distinct_spellings": len(rows),
        "undeclared_spellings": len(undeclared), "undeclared_events": sum(n for _, n in undeclared),
        "declared_not_a_unit_events": sum(int(n) for u, n in rows if units.declared(u) and not units.known(u)),
        "top_undeclared": {reportable_spelling(u): n for u, n in undeclared[:25]},
        "plausible_ranges_declared": len(tables.ranges), "exact_conversions": len(tables.conversions),
    }
    return out


def _visits(omop, con, has_events: bool) -> dict[str, Any]:
    """Visit concept coverage per published table, and visits that state no type."""
    out: dict[str, Any] = {}
    if omop is not None:
        tables = (
            ("visit_occurrence", "visit_concept_id", "visit_start_datetime", "visit_end_datetime"),
            ("visit_detail", "visit_detail_concept_id", "visit_detail_start_datetime", "visit_detail_end_datetime"),
        )
        for table, column, start, end in tables:
            try:
                rows, covered, zero_length = omop.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE {column} <> 0), "
                    f"count(*) FILTER (WHERE {start} = {end}) FROM {table}"
                ).fetchone()
            except Exception:
                continue
            out[table] = {
                "rows": int(rows or 0), "with_concept": int(covered or 0),
                "coverage": round(int(covered or 0) / int(rows), 4) if rows else None,
                "zero_length": int(zero_length or 0),
            }
    if omop is not None:
        try:
            out["visit_detail_unparented_issues"] = int(omop.execute(
                "SELECT count(DISTINCT event_id) FROM etl_audit.quality_issue "
                "WHERE issue_type = 'VISIT_DETAIL_UNPARENTED'"
            ).fetchone()[0])
        except Exception:
            out["visit_detail_unparented_issues"] = None
    if has_events:
        kinds = ", ".join(_sql_str(k) for k in VISIT_KINDS)
        rows = con.execute(
            f"""
            SELECT event_kind, count(*), count(*) FILTER (WHERE end_time IS NULL OR end_time = event_time),
                   count(*) FILTER (WHERE source_code IS NULL OR trim(source_code) = '')
            FROM evt WHERE event_kind IN ({kinds}) GROUP BY 1
            """
        ).fetchall()
        out["canonical"] = {
            str(kind): {"events": int(n), "zero_length": int(z), "without_type": int(u)}
            for kind, n, z, u in rows
        }
    return out


def _doses(con, omop, meds_files: list[str], has_events: bool) -> dict[str, Any]:
    """How often a drug row that could carry a dose unit does."""
    out: dict[str, Any] = {}
    if has_events:
        kinds = ", ".join(_sql_str(k) for k in DRUG_KINDS)
        rows, with_unit, with_dose = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE unit_source IS NOT NULL), "
            f"count(*) FILTER (WHERE dose_source IS NOT NULL) FROM evt WHERE event_kind IN ({kinds})"
        ).fetchone()
        out["canonical"] = {"drug_events": int(rows or 0), "with_unit_source": int(with_unit or 0),
                            "with_dose_text": int(with_dose or 0)}
    if omop is not None:
        rows, without = omop.execute(
            "SELECT count(*), count(*) FILTER (WHERE dose_unit_source_value IS NULL) FROM drug_exposure"
        ).fetchone()
        out["omop_drug_exposure"] = {
            "rows": int(rows or 0), "without_dose_unit": int(without or 0),
            "null_rate": round(int(without or 0) / int(rows), 4) if rows else None,
        }
    if meds_files:
        import duckdb

        con2 = duckdb.connect()
        try:
            con2.read_parquet(meds_files, hive_partitioning=False).create_view("meds")
            kinds = ", ".join(_sql_str(k) for k in DRUG_KINDS)
            rows, no_unit, bare = con2.execute(
                f"SELECT count(*), count(*) FILTER (WHERE unit IS NULL), "
                f"count(*) FILTER (WHERE unit IS NULL AND dose IS NOT NULL) "
                f"FROM meds WHERE event_kind IN ({kinds})"
            ).fetchone()
            out["meds"] = {"drug_rows": int(rows or 0), "without_unit": int(no_unit or 0),
                           "bare_dose_numbers": int(bare or 0)}
        except Exception as exc:
            out["meds"] = {"not_read": f"{type(exc).__name__}: {exc}"}
        finally:
            con2.close()
    return out


def _death(con, omop, zone: str | None, has_events: bool) -> dict[str, Any]:
    """Deaths in the canonical layer against DEATH rows, compared as local dates."""
    out: dict[str, Any] = {"zone": zone or "UTC"}
    if has_events:
        out.update(death_local_dates(con, zone))
        local = _local_date_sql("event_time", zone)
        out["distinct_local_dates_per_subject"] = {
            str(dates): int(n) for dates, n in con.execute(
                f"""
                WITH d AS (SELECT subject_id, count(DISTINCT {local}) AS dates FROM evt
                           WHERE event_kind = '{EventKind.death}' AND event_time IS NOT NULL GROUP BY 1)
                SELECT dates, count(*) FROM d GROUP BY 1 ORDER BY 1
                """
            ).fetchall()
        }
    if omop is not None:
        out["omop_death_rows"] = int(omop.execute("SELECT count(*) FROM death").fetchone()[0])
        out["omop_persons"] = int(omop.execute("SELECT count(*) FROM person").fetchone()[0])
    return out


def audit(dataset_id: str, work_root: Path) -> dict[str, Any]:
    cfg, config_path = _load_config(dataset_id)
    layout = WorkLayout(root=work_root / dataset_id, dataset_id=dataset_id)
    if not layout.root.is_dir():
        raise SystemExit(f"no build for {dataset_id} under {work_root}")
    manifest = _manifest(layout)
    events_path = layout.canonical_path("events")
    links_path = layout.canonical_path("event_source")
    omop_path = layout.omop_dir / "omop.duckdb"
    meds_files = sorted(str(p) for p in (layout.meds_dir / "data").rglob("*.parquet"))
    zone = cfg.time.timezone_assumption

    report: dict[str, Any] = {
        "dataset_id": dataset_id,
        "collected_utc": datetime.now(timezone.utc).isoformat(),
        "repository_head": _head(),
        "config": str(config_path.relative_to(ROOT)),
        "work_root": str(work_root),
        "layers": {
            "manifest": manifest is not None,
            "canonical": events_path.exists(),
            "omop": omop_path.exists(),
            "meds": bool(meds_files),
        },
        "timezone_assumption": zone,
    }
    if manifest is not None:
        report["code_version"] = manifest.get("code_version")
        report["config_hash"] = manifest.get("config_hash")

    omop = None
    if omop_path.exists():
        import duckdb

        omop = duckdb.connect(str(omop_path), read_only=True)
        omop.execute("SET TimeZone='UTC'")
    scratch = work_root / SCRATCH_SUBDIR / dataset_id
    try:
        with analytic_connection(scratch, threads=HEAVY_THREADS) as con:
            con.execute("SET TimeZone='UTC'")
            has_events = events_path.exists()
            if has_events:
                con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{events_path}')")
            if links_path.exists():
                con.execute(f"CREATE VIEW lnk AS SELECT * FROM read_parquet('{links_path}')")
            columns: set[str] = set()
            if has_events:
                import pyarrow.parquet as pq

                columns = set(pq.read_schema(events_path).names)
                report["events"] = int(con.execute("SELECT count(*) FROM evt").fetchone()[0])
                report["events_by_kind"] = {
                    str(k): int(n) for k, n in con.execute(
                        "SELECT event_kind, count(*) FROM evt GROUP BY 1 ORDER BY 2 DESC").fetchall()
                }
            report["sources"] = _sources(con, layout, manifest, has_events and links_path.exists())
            if manifest is not None and links_path.exists():
                report["merge_disagreements"] = merge_disagreements(
                    cfg, manifest, links_path, con, per_partition=True, layout=layout)
            if has_events:
                report["units"] = _units(con, omop, columns, dataset_id)
                report["visits"] = _visits(omop, con, has_events)
                report["doses"] = _doses(con, omop, meds_files, has_events)
                report["death"] = _death(con, omop, zone, has_events)
                report["notes"] = note_duplicate_groups(con, zone)
                report["encounter_link_rate"] = {"resolved_against": encounter_reference(cfg),
                                                 "per_source": encounter_link_rates(con, encounter_reference(cfg))}
            else:
                report["visits"] = _visits(omop, con, False)
                report["doses"] = _doses(con, omop, meds_files, False)
                report["death"] = _death(con, omop, zone, False)
        report["raw_coverage"] = raw_coverage(cfg, manifest)
    finally:
        if omop is not None:
            omop.close()
    return report


def _load_config(dataset_id: str):
    path = ROOT / "datasets" / f"{dataset_id}.yaml"
    if not path.exists():
        raise SystemExit(f"no dataset configuration at {path}")
    return load_dataset_config(path), path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="dataset id, as in datasets/<id>.yaml")
    parser.add_argument("--work-root", type=Path, default=None, help="default: $EHR_WORK_ROOT")
    parser.add_argument("--out", type=Path, default=None,
                        help="default: results/<dataset>/conversion_audit.json")
    args = parser.parse_args()

    work_root = args.work_root or _work_root()
    report = audit(args.dataset, work_root)
    out = args.out or (ROOT / "results" / args.dataset / "conversion_audit.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {out}")
    for line in _headlines(report):
        print(f"  {line}")
    return 0


def _headlines(report: dict[str, Any]) -> list[str]:
    """The half-dozen numbers the audit was actually about."""
    lines = [f"{report.get('events', 0):,} events in {len(report.get('sources', {}))} sources"]
    disagreeing = {
        sid: entry["disagreements"]
        for sid, entry in (report.get("merge_disagreements") or {}).items() if entry.get("disagreements")
    }
    if disagreeing:
        lines.append("merged rows disagree: " + "; ".join(
            f"{sid} " + ", ".join(f"{k}={v:,}" for k, v in sorted(d.items())) for sid, d in sorted(disagreeing.items())))
    not_applied = {
        sid: entry["rules_not_applied"]
        for sid, entry in (report.get("merge_disagreements") or {}).items() if entry.get("rules_not_applied")
    }
    if not_applied:
        lines.append("declared merge rules the build did not apply: " + "; ".join(
            f"{sid} " + ", ".join(f"{k}={v:,}" for k, v in sorted(d.items())) for sid, d in sorted(not_applied.items())))
    units = (report.get("units") or {}).get("omop_measurement") or {}
    if units.get("coverage") is not None:
        lines.append(f"unit concept coverage {units['coverage']:.1%} of {units['with_unit_source_value']:,} rows")
    doses = (report.get("doses") or {}).get("omop_drug_exposure") or {}
    if doses.get("null_rate") is not None:
        lines.append(f"dose unit missing on {doses['null_rate']:.1%} of {doses['rows']:,} drug exposures")
    death = report.get("death") or {}
    if "omop_death_rows" in death:
        lines.append(
            f"{death.get('subjects_with_death_event', 0):,} subjects have a death event, "
            f"{death['omop_death_rows']:,} DEATH rows, "
            f"{death.get('subjects_with_conflicting_local_dates', 0):,} disagree on the local date")
    notes = report.get("notes") or {}
    if notes.get("exact_duplicate_groups"):
        lines.append(f"{notes['exact_duplicate_groups']:,} note groups repeat one text on one day "
                     f"({notes.get('surplus_note_events', 0):,} surplus events)")
    coverage = report.get("raw_coverage") or {}
    undeclared = sum(len(c.get("undeclared", [])) for c in (coverage.get("columns") or {}).values())
    unclaimed = len((coverage.get("files") or {}).get("unclaimed", []))
    prepared = coverage.get("prepare_manifest") or {}
    left = prepared.get("unread_beside_inputs_count", 0) + prepared.get(
        "undeclared_unread_inputs_count", len(prepared.get("undeclared_unread_inputs", [])))
    lines.append(f"{undeclared} delivered columns and {unclaimed} files nothing reads or declares; "
                 + (f"{left} files a preparation step left unread without a declaration "
                    f"({prepared.get('manifests', 0)} manifest(s))" if prepared.get("manifests")
                    else "no preparation manifest found"))
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
