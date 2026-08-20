"""Command line interface (design section 9.2).

Conventions that hold for every command:

* a non-zero exit code means incomplete, not merely "printed a warning";
* ``--json`` emits a stable machine-readable form;
* ``--workers 1`` is always valid and is the deterministic baseline;
* outputs that already exist with a matching content hash are reused, which is what
  resumption means here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer

from ehr2cdm.config import DatasetConfig, find_dataset_config, load_dataset_config
from ehr2cdm.errors import BlockerError, ConfigError
from ehr2cdm.paths import WorkLayout
from ehr2cdm.run import RunReport, execute, new_run_id
from ehr2cdm.version import CODE_VERSION

app = typer.Typer(add_completion=False, help="Deterministic EHR -> OMOP CDM 5.4 / MEDS converter")

DatasetOpt = typer.Option(..., "--dataset", "-d", help="dataset id or path to a dataset YAML")
WorkersOpt = typer.Option(0, "--workers", "-w", help="worker processes; 0 uses the config value")
JsonOpt = typer.Option(False, "--json", help="emit machine-readable JSON on stdout")


def _load(dataset: str) -> tuple[DatasetConfig, Path]:
    path = find_dataset_config(dataset)
    return load_dataset_config(path), path


def _layout(cfg: DatasetConfig) -> WorkLayout:
    return WorkLayout.from_env(cfg.dataset_id).ensure()


def _workers(cfg: DatasetConfig, workers: int) -> int:
    return workers if workers and workers > 0 else cfg.execution.workers


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _resolve_timezone(cfg: DatasetConfig, assume: Optional[str]) -> tuple[str | None, bool]:
    """The timezone to convert with, and whether it was an operator assumption.

    An operator may supply one the config does not declare, but never invisibly: the
    choice is recorded in the run report and every converted event is flagged.
    """
    if cfg.time.timezone_assumption:
        return cfg.time.timezone_assumption, False
    if assume:
        return assume, True
    if cfg.time.naive_time_policy == "store_naive_flagged":
        return None, True
    raise BlockerError(
        "TIMEZONE_UNDECLARED",
        "no source timezone is declared. Set time.timezone_assumption in the dataset "
        "config once the data owner answers, or pass --assume-timezone to record an "
        "explicit, flagged operator assumption for this run",
    )


# --------------------------------------------------------------------------------


@app.command()
def inspect(
    dataset: str = DatasetOpt,
    as_json: bool = JsonOpt,
    hashes: bool = typer.Option(True, "--hashes/--no-hashes", help="compute input file hashes"),
    workers: int = WorkersOpt,
    out: Optional[Path] = typer.Option(None, "--out", help="also write the report here"),
) -> None:
    """Discover inputs, line them up against the config, and list the blockers."""
    from ehr2cdm.discover import inspect as do_inspect

    cfg, _path = _load(dataset)
    report = do_inspect(cfg, compute_hashes=hashes, workers=_workers(cfg, workers) * 2)
    payload = report.to_dict()

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    if as_json:
        _echo_json(payload)
    else:
        c = report.counts
        typer.echo(f"dataset        {report.dataset_id}  (config {report.config_hash[:12]}, code {CODE_VERSION})")
        typer.echo(f"data root      {report.data_root}")
        typer.echo(
            f"physical files {c['physical_files']}  "
            f"({c['text_files']} text + {c['workbooks']} workbooks, {c['total_bytes'] / 1e9:.1f} GB)"
        )
        typer.echo(
            f"logical srcs   {c['logical_sources']}  "
            f"({c['logical_sources_text']} text + {c['logical_sources_sheets']} sheets)"
        )
        for part in cfg.partitions:
            size = c["bytes_per_partition"].get(part.id, 0)
            present = [s for s in report.sources if s.partition_id == part.id and s.coverage == "present"]
            missing = [s for s in report.sources if s.partition_id == part.id and s.coverage == "not_extracted"]
            typer.echo(
                f"  {part.id:<9} {size / 1e9:>5.1f} GB  {len(present)} sources present"
                + (f", not extracted: {', '.join(s.source_id for s in missing)}" if missing else "")
            )
        typer.echo("")
        if report.blockers:
            typer.echo(f"BLOCKERS ({len(report.blockers)}) -- these must be answered, not guessed:")
            for b in report.blockers:
                typer.echo(f"  [{b.id}] ({b.needed_from}; blocks {b.blocks})")
                typer.echo(f"      {b.question}")
        else:
            typer.echo("no open blockers")

    raise typer.Exit(code=1 if report.blockers else 0)


@app.command()
def ingest(
    dataset: str = DatasetOpt,
    workers: int = WorkersOpt,
    as_json: bool = JsonOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be read and written"),
) -> None:
    """raw -> source layer, with a row-level lineage record for every row."""
    from ehr2cdm.ingest import plan_ingest, run_ingest_task, write_manifest

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    tasks = plan_ingest(cfg, layout)
    if dry_run:
        _echo_json(
            [
                {"partition": t.partition_id, "source": t.source_id, "file": t.file_path, "sheet": t.sheet}
                for t in tasks
            ]
        )
        raise typer.Exit(code=0)

    report = RunReport(run_id=new_run_id("ingest"), dataset_id=cfg.dataset_id, config_hash=cfg.config_hash())
    report.workers = _workers(cfg, workers)
    results = execute(tasks, run_ingest_task, report.workers)
    write_manifest(layout, cfg, results)

    counts = {
        "units": len(results),
        "rows_read": sum(r.rows_read for r in results),
        "rows_parsed": sum(r.rows_parsed for r in results),
        "rows_quarantined": sum(r.rows_quarantined for r in results),
        "reused": sum(1 for r in results if r.reused),
    }
    report.stage("ingest", counts, results)
    report.write(layout)

    if as_json:
        _echo_json(counts)
    else:
        typer.echo(
            f"ingested {counts['units']} units: {counts['rows_parsed']:,} rows, "
            f"{counts['rows_quarantined']:,} quarantined, {counts['reused']} reused"
        )
        for r in results:
            if r.rows_quarantined:
                typer.echo(f"  quarantine {r.partition_id}/{r.source_id}: {r.rows_quarantined:,}")
    raise typer.Exit(code=0)


@app.command()
def identity(dataset: str = DatasetOpt, as_json: bool = JsonOpt) -> None:
    """Resolve patient identity across every partition. The pipeline's only barrier."""
    from ehr2cdm.identity import build_identity

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = build_identity(cfg, layout)
    payload = {"subjects": result.subjects, "per_partition": result.per_partition}
    if as_json:
        _echo_json(payload)
    else:
        typer.echo(f"subjects: {result.subjects:,} across {len(result.per_partition)} partitions")
        for part, n in result.per_partition.items():
            typer.echo(f"  {part:<9} {n:,}")
    raise typer.Exit(code=0)


@app.command()
def canonical(
    dataset: str = DatasetOpt,
    workers: int = WorkersOpt,
    as_json: bool = JsonOpt,
    assume_timezone: Optional[str] = typer.Option(
        None, "--assume-timezone", help="operator-supplied source timezone; recorded and flagged"
    ),
    skip_stage: bool = typer.Option(False, "--skip-stage", help="reuse existing staged buckets"),
) -> None:
    """source -> canonical events: identity, time semantics, values, anchors, dedup."""
    from ehr2cdm.canonical.build import (
        merge_buckets,
        plan_canonical,
        plan_stage,
        run_canonical_task,
        run_stage_task,
    )

    cfg, path = _load(dataset)
    layout = _layout(cfg)
    tz, tz_assumed = _resolve_timezone(cfg, assume_timezone)

    report = RunReport(run_id=new_run_id("canonical"), dataset_id=cfg.dataset_id, config_hash=cfg.config_hash())
    report.workers = _workers(cfg, workers)
    if tz_assumed:
        report.assumption(
            "timezone",
            tz,
            "not declared by the data owner; supplied by the operator for this run and "
            "flagged on every converted event",
        )

    if not skip_stage:
        stage_tasks = plan_stage(cfg, layout)
        stage_results = execute(stage_tasks, run_stage_task, report.workers)
        report.stage(
            "stage",
            {"tasks": len(stage_results), "rows": sum(r.rows for r in stage_results)},
            stage_results,
        )

    tasks = plan_canonical(cfg, layout, path, tz, tz_assumed)
    results = execute(tasks, run_canonical_task, report.workers)
    digests = {t.bucket: t.digest for t in tasks}
    merged = merge_buckets(layout, digests)

    counts = {
        "buckets": len(results),
        "events": merged.get("events", 0),
        "event_source": merged.get("event_source", 0),
        "anchors": merged.get("anchors", 0),
        "cohort_membership": merged.get("cohort_membership", 0),
        "quality_issue": merged.get("quality_issue", 0),
        "quarantine": merged.get("quarantine", 0),
    }
    report.stage("canonical", counts, results)
    report.write(layout)

    if as_json:
        _echo_json(counts)
    else:
        for key, value in counts.items():
            typer.echo(f"  {key:<18} {value:,}")
    raise typer.Exit(code=0)


@app.command()
def omop(
    dataset: str = DatasetOpt,
    as_json: bool = JsonOpt,
    vocabulary: Optional[Path] = typer.Option(None, "--vocabulary", help="OMOP vocabulary directory"),
) -> None:
    """canonical -> OMOP CDM 5.4 (DuckDB), with lineage for every row."""
    from ehr2cdm.omop import build_omop

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = build_omop(cfg, layout, vocabulary_dir=vocabulary)
    if as_json:
        _echo_json(result)
    else:
        for table, n in sorted(result["tables"].items()):
            typer.echo(f"  {table:<24} {n:,}")
        if result["blocked_subjects"]:
            typer.echo(f"\nPERSON blocked for {result['blocked_subjects']:,} subjects: {result['block_reason']}")
        if result["unmapped_terms"]:
            typer.echo(f"unmapped terms sent to review: {result['unmapped_terms']:,}")
    raise typer.Exit(code=0)


@app.command()
def meds(dataset: str = DatasetOpt, as_json: bool = JsonOpt) -> None:
    """canonical -> MEDS shards plus metadata."""
    from ehr2cdm.meds import build_meds

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = build_meds(cfg, layout)
    if as_json:
        _echo_json(result)
    else:
        for key, value in result.items():
            typer.echo(f"  {key:<20} {value}")
    raise typer.Exit(code=0)


@app.command()
def validate(
    dataset: str = DatasetOpt,
    as_json: bool = JsonOpt,
    all_checks: bool = typer.Option(False, "--all", help="include the slow full-dataset checks"),
) -> None:
    """Run the reconciliation and compliance checks. Non-zero exit means a failure."""
    from ehr2cdm.validate import run_checks

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    results = run_checks(cfg, layout, include_slow=all_checks)
    failed = [r for r in results if not r.passed]
    if as_json:
        _echo_json([r.__dict__ for r in results])
    else:
        for r in results:
            mark = "PASS" if r.passed else "FAIL"
            typer.echo(f"  [{mark}] {r.check_id}: {r.detail}")
        typer.echo(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def propose(
    dataset: str = DatasetOpt,
    kind: str = typer.Option("terminology", "--kind", help="terminology | columns"),
    limit: int = typer.Option(200, "--limit"),
    use_llm: bool = typer.Option(False, "--llm/--no-llm", help="rank candidates with the local model"),
) -> None:
    """Write proposals to review/pending.csv. Proposals are never applied automatically."""
    from ehr2cdm.review import propose_columns, propose_terminology

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    if kind == "terminology":
        n = propose_terminology(cfg, layout, limit=limit, use_llm=use_llm)
    elif kind == "columns":
        n = propose_columns(cfg, layout, use_llm=use_llm)
    else:
        raise typer.BadParameter("kind must be 'terminology' or 'columns'")
    typer.echo(f"{n} proposals written to {layout.review_dir / 'pending.csv'}")
    raise typer.Exit(code=0)


@app.command("compile")
def compile_mappings(
    dataset: str = DatasetOpt,
    mappings_dir: Optional[Path] = typer.Option(None, "--mappings", help="target mappings directory"),
) -> None:
    """review/decisions.csv -> mappings/. Only human decisions are compiled."""
    from ehr2cdm.review import compile_decisions

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    target = mappings_dir or Path.cwd() / "mappings"
    written = compile_decisions(layout, target)
    typer.echo(f"compiled {written} decisions into {target}")
    raise typer.Exit(code=0)


@app.command()
def report(
    dataset: str = DatasetOpt,
    run_id: Optional[str] = typer.Option(None, "--run-id"),
) -> None:
    """Show a run report."""
    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    runs = sorted(p for p in layout.runs_dir.glob("*/report.json"))
    if not runs:
        typer.echo("no runs recorded yet")
        raise typer.Exit(code=1)
    target = next((p for p in runs if run_id and p.parent.name == run_id), runs[-1])
    typer.echo(target.read_text(encoding="utf-8"))
    raise typer.Exit(code=0)


def main() -> None:
    try:
        app()
    except (BlockerError, ConfigError) as exc:
        typer.echo(f"error: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
