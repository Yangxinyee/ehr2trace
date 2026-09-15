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
import sys
from pathlib import Path
from typing import Optional

import typer

from ehr2trace.config import DatasetConfig, find_dataset_config, load_dataset_config
from ehr2trace.errors import BlockerError, ConfigError
from ehr2trace.paths import WorkLayout
from ehr2trace.run import RunReport, execute, new_run_id
from ehr2trace.version import CODE_VERSION

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
    from ehr2trace.discover import inspect as do_inspect

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
            f"({c['text_files']} text + {c['workbooks']} workbooks"
            f" + {c.get('columnar_files', 0)} columnar, {c['total_bytes'] / 1e9:.1f} GB)"
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
    from ehr2trace.ingest import plan_ingest, run_ingest_task, write_manifest

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
    from ehr2trace.identity import build_identity

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
    from ehr2trace.canonical.build import (
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
    from ehr2trace.omop import build_omop

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = build_omop(cfg, layout, vocabulary_dir=vocabulary)
    if as_json:
        _echo_json(result)
    else:
        for table, n in sorted(result["tables"].items()):
            if not table.startswith("_"):
                typer.echo(f"  {table:<24} {n:,}")
        typer.echo(f"  {'etl_audit.lineage':<24} {result['lineage_rows']:,}")
        if result["blocked_subjects"]:
            typer.echo(
                f"\nWITHHELD: {result['blocked_subjects']:,} patients were not published to "
                "OMOP at all -- not to PERSON and not to any clinical table."
            )
            typer.echo(f"  reason: {result['block_reason']}")
            typer.echo(
                "  This is the policy working, not a failure. Rows referencing a person "
                "who does not exist\n  are not a CDM instance, and a year of birth "
                "invented from an age is not a fact.\n"
                "  To unblock: have the data owner supply omop.person_birth_policy."
                "age_as_of_date plus\n  an approval note, then rebuild. Canonical and "
                "MEDS are unaffected and remain complete."
            )
        if result["unmapped_terms"]:
            typer.echo(
                f"\n{result['unmapped_terms']:,} distinct terms had no concept and went to "
                f"review rather than\nbecoming 0 silently: {result['pending_csv']}"
            )
        if result["vocabulary"] == "none":
            typer.echo(
                "\nNo vocabulary is installed, so every concept_id is 0 and every source "
                "value is preserved.\nSet OMOP_VOCAB_DIR to an Athena download to map terms."
            )
    raise typer.Exit(code=0)


@app.command()
def meds(
    dataset: str = DatasetOpt,
    as_json: bool = JsonOpt,
    vocabulary: Optional[Path] = typer.Option(None, "--vocabulary", help="OMOP vocabulary directory, the one the OMOP layer was built with"),
) -> None:
    """canonical -> MEDS shards plus metadata."""
    from ehr2trace.meds import build_meds

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = build_meds(cfg, layout, vocabulary_dir=vocabulary)
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
    from ehr2trace.validate import run_checks

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    results = run_checks(cfg, layout, include_slow=all_checks)
    failed = [r for r in results if not r.passed]
    skipped = [r for r in results if r.skipped]
    passed = [r for r in results if r.passed and not r.skipped]
    if as_json:
        _echo_json([r.__dict__ for r in results])
    else:
        for r in results:
            mark = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
            typer.echo(f"  [{mark}] {r.check_id}: {r.detail}")
        # A skipped check is not a passed check. Counting the two together once
        # reported a full house on a run whose canonical, OMOP and MEDS layers had all
        # failed to build -- the single most misleading thing this tool has ever said.
        typer.echo(
            f"\n{len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed"
            f"  ({len(results)} checks)"
        )
        if skipped:
            typer.echo(
                f"  {len(skipped)} check(s) had nothing to examine. A layer that was not "
                "built is not a layer that passed."
            )
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def propose(
    dataset: str = DatasetOpt,
    kind: str = typer.Option("terminology", "--kind", help="terminology | columns"),
    limit: int = typer.Option(200, "--limit"),
    use_llm: bool = typer.Option(False, "--llm/--no-llm", help="rank candidates with the local model"),
) -> None:
    """Write proposals to review/pending.csv. Proposals are never applied automatically."""
    from ehr2trace.review import propose_columns, propose_terminology

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
    replace: Optional[list[str]] = typer.Option(
        None, "--replace", help="decision id to apply over a row decided the same day; repeatable"
    ),
) -> None:
    """review/decisions.csv -> mappings/. Only human decisions are compiled.

    decisions.csv is a log, so a decision replaces a row only if it was decided on a
    later day. An older one is reported as superseded and changes nothing; one decided
    the same day as a row it disagrees with is a conflict until --replace names it.
    Every row that changes is printed. A conflict, or an accepted decision without a
    reviewer and a date, exits non-zero.
    """
    from ehr2trace.review import CompileOverrideError, compile_decisions, describe_compile
    from ehr2trace.terminology import mappings_directory

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    target = mappings_dir or mappings_directory()
    try:
        result = compile_decisions(layout, target, replace=replace or ())
    except CompileOverrideError as exc:
        raise typer.BadParameter(str(exc), param_hint="--replace") from exc
    typer.echo(f"compiled {layout.review_dir / 'decisions.csv'} into {target}")
    for line in describe_compile(result):
        typer.echo(line)
    if not result.complete:
        typer.echo(
            "not every accepted decision was applied: a conflicting one needs a person to name it "
            "with --replace ID or to decide it again on a later day; an incomplete one needs a "
            "reviewer and a YYYY-MM-DD date"
        )
    raise typer.Exit(code=0 if result.complete else 1)


@app.command()
def trace(
    dataset: str = DatasetOpt,
    patient: str = typer.Option(..., "--patient", help="patient key as it appears in the source"),
    samples: int = typer.Option(3, "--samples", help="how many events to follow back to the raw file"),
    as_json: bool = JsonOpt,
) -> None:
    """Follow one patient through every layer, back to file and row number.

    A debugging and fixture-generation aid: it reads and never writes, and terminology
    still comes from the global `mappings/`, so what it shows is what a full run
    produces for that patient. It prints a real patient key, so use it on a terminal
    you would be willing to show the data owner.
    """
    from ehr2trace.trace import render, trace_patient

    cfg, _path = _load(dataset)
    layout = _layout(cfg)
    result = trace_patient(cfg, layout, patient, samples=samples)
    if as_json:
        _echo_json(result.to_dict())
    else:
        typer.echo(render(result))
    raise typer.Exit(code=0)


@app.command()
def clean(
    dataset: str = DatasetOpt,
    dry_run: bool = typer.Option(True, "--dry-run/--delete", help="list what would be removed"),
    assume_timezone: Optional[str] = typer.Option(None, "--assume-timezone"),
) -> None:
    """Remove artifacts that no longer match the current content address.

    Content addressing means a code or config change leaves the previous run's outputs
    on disk, still valid for the inputs that produced them and no longer reachable.
    That is the right trade for resumability, but on a dataset this size the dead
    weight is measured in tens of gigabytes, so removing it is an explicit command
    rather than something a run does silently behind your back.
    """
    from ehr2trace.canonical.build import plan_canonical, plan_stage
    from ehr2trace.ingest import plan_ingest
    from ehr2trace.paths import PARTIAL_SUFFIX

    cfg, path = _load(dataset)
    layout = _layout(cfg)

    keep: set[Path] = set()
    for task in plan_ingest(cfg, layout):
        from ehr2trace.hashing import file_sha256

        digest = task.digest(file_sha256(task.file_path))
        keep.add(layout.source_task_path(task.partition_id, task.source_id, digest))
        keep.add(layout.quarantine_task_path("ingest", task.partition_id, task.source_id, digest))
    # Not strict: this command exists to remove artifacts a version bump stranded, so
    # refusing to plan because they are stranded would be exactly backwards.
    for task in plan_stage(cfg, layout, strict=False):
        keep.add(layout.staged_dir / "_done" / f"{task.partition_id}__{task.source_id}__{task.digest}.json")
    try:
        tz, tz_assumed = _resolve_timezone(cfg, assume_timezone)
        # Not strict, for the same reason plan_stage above is not.
        for task in plan_canonical(cfg, layout, path, tz, tz_assumed, strict=False):
            keep.add(layout.bucket_dir(task.bucket) / task.digest)
    except BlockerError:
        typer.echo("note: canonical buckets left alone (no timezone resolved for this run)")

    stale: list[Path] = []
    freed = 0
    for root in (layout.source_dir, layout.quarantine_dir):
        for candidate in root.rglob("*.parquet*"):
            if candidate in keep or candidate.name.endswith(PARTIAL_SUFFIX):
                if candidate.name.endswith(PARTIAL_SUFFIX):
                    stale.append(candidate)
                    freed += candidate.stat().st_size
                continue
            stale.append(candidate)
            freed += candidate.stat().st_size
    for marker in (layout.staged_dir / "_done").glob("*.json"):
        if marker not in keep:
            stale.append(marker)
    for bucket_dir in layout.canonical_dir.glob("buckets/bucket=*/*"):
        if bucket_dir.is_dir() and bucket_dir not in keep:
            stale.append(bucket_dir)
            freed += sum(f.stat().st_size for f in bucket_dir.rglob("*") if f.is_file())

    typer.echo(f"{len(stale)} stale artifacts, {freed / 1e9:.2f} GB")
    if dry_run:
        for item in stale[:20]:
            typer.echo(f"  would remove {item}")
        if len(stale) > 20:
            typer.echo(f"  ... and {len(stale) - 20} more")
        typer.echo("\nnothing was removed; pass --delete to actually remove these")
    else:
        import shutil

        for item in stale:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        typer.echo(f"removed {len(stale)} artifacts")
    raise typer.Exit(code=0)


@app.command()
def measure(
    dataset: str = DatasetOpt,
    kind: str = typer.Option("terminology", "--kind", help="terminology | columns"),
    limit: int = typer.Option(0, "--limit", help="score at most this many items (0 = all)"),
    gold: Optional[Path] = typer.Option(None, "--gold", help="gold set CSV; defaults to review/gold.csv"),
    from_decisions: bool = typer.Option(False, "--from-decisions", help="build the gold set from accepted review decisions first"),
    use_llm: bool = typer.Option(True, "--llm/--no-llm"),
    vocabulary: Optional[Path] = typer.Option(None, "--vocabulary"),
    as_json: bool = JsonOpt,
) -> None:
    """Measure whether the model actually helps, and say so plainly (checklist P4-4)."""
    from ehr2trace.measure import gold_from_decisions, measure as run_measure, measure_columns

    cfg, _path = _load(dataset)
    layout = _layout(cfg)

    if kind == "columns":
        # The answer key is the dataset YAML: someone decided what every column means,
        # and reproducing that decision is exactly what the model is being asked to do.
        result = measure_columns(cfg, layout, use_llm=use_llm, limit=limit)
        if as_json:
            _echo_json(result)
        else:
            typer.echo(f"columns scored: {result['columns_scored']}  key: {result['answer_key']}")
            for arm in result["arms"]:
                typer.echo(
                    f"  {arm['arm']:<22} top1 {arm['top1_accuracy']:.1%}  "
                    f"{arm['seconds_per_item']:.2f}s/item"
                    + (f"  schema failures {arm['schema_failures']}" if arm["schema_failures"] else "")
                )
            typer.echo(f"\nverdict: {result['verdict']}")
        raise typer.Exit(code=0)

    gold_path = gold or (layout.review_dir / "gold.csv")
    if from_decisions:
        n = gold_from_decisions(layout, gold_path)
        typer.echo(f"exported {n} accepted decisions to {gold_path}")
    result = run_measure(cfg, layout, gold_path, vocabulary_dir=vocabulary, use_llm=use_llm)
    if as_json:
        _echo_json(result)
    else:
        typer.echo(f"gold items: {result['gold_items']}  vocabulary: {result['vocabulary']}")
        for arm in result["arms"]:
            typer.echo(
                f"  {arm['arm']:<22} top1 {arm['top1_accuracy']:.1%}  "
                f"recall@5 {arm['recall_at_5']:.1%}  {arm['seconds_per_item']:.2f}s/item"
            )
        typer.echo(f"\nverdict: {result['verdict']}")
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
