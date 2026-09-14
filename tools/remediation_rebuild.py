"""Rebuild one dataset end to end, the way the remediation plan's phase 3 asks.

One command, one dataset, every stage in order -- inspect, ingest, identity, canonical,
omop, meds, validate, audit -- with the wall time and peak memory of each recorded, so
the cost table can be filled from a record rather than from memory. Three things the
plan's phase 3 requires are done here rather than left to the operator:

* the machine is checked for stray ``multiprocessing.spawn`` workers before anything
  starts (they held 68.7 GB during a build on 2026-09-10 and got its MEDS stage killed);
* the config hash is recorded at the start and compared at the end, so a yaml edited
  during the build -- which invalidates every ingest output -- is caught rather than
  discovered on the next resume;
* nothing here reads or writes anything but the work root, and the record it writes
  carries counts and timings only.

    python tools/remediation_rebuild.py --dataset cu_ctpa --workers 8 \\
        --record results/cu_ctpa/rebuild_2026-09-14.json

Environment as for the CLI: EHR_WORK_ROOT, the dataset's root variable, OMOP_VOCAB_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

STAGES: tuple[tuple[str, list[str]], ...] = (
    # `inspect` computes a hash of every input; on a multi-terabyte export that is the
    # slowest stage of the run and says nothing the manifest will not.
    ("inspect", ["inspect", "--no-hashes"]),
    ("ingest", ["ingest"]),
    ("identity", ["identity"]),
    ("canonical", ["canonical"]),
    ("omop", ["omop"]),
    ("meds", ["meds"]),
    ("validate", ["validate", "--all"]),
)


def stray_workers() -> list[str]:
    out = subprocess.run(["ps", "-eo", "pid,etimes,rss,args"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "multiprocessing.spawn" in line or "resource_tracker" in line]


def config_hash(dataset: str) -> str:
    code = (
        "from ehr2trace.config import find_dataset_config, load_dataset_config; "
        f"print(load_dataset_config(find_dataset_config({dataset!r})).config_hash())"
    )
    return subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT).stdout.strip()


def run_stage(name: str, argv: list[str], log_dir: Path) -> dict:
    log = log_dir / f"{name}.log"
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.run([PYTHON, "-m", "ehr2trace.cli", *argv], stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT)
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "stage": name,
        "exit_code": proc.returncode,
        "wall_seconds": round(time.monotonic() - started, 1),
        # Linux reports kilobytes; the children figure is a running maximum, so the
        # first stage to exceed the previous peak owns the new one.
        "peak_rss_gb_children_so_far": round(max(before, after) / (1024 * 1024), 2),
        "log": str(log),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--workers", type=int, default=0, help="0 uses the config's value")
    ap.add_argument("--record", type=Path, required=True, help="where to write the timing record (JSON)")
    ap.add_argument("--audit-out", type=Path, default=None, help="conversion_audit.json path (default beside the record)")
    ap.add_argument("--skip", nargs="*", default=[], help="stage names to skip (e.g. inspect)")
    ap.add_argument("--allow-stray-workers", action="store_true")
    ap.add_argument("--continue-on-failure", action="store_true", help="run later stages after a failed one (validate only)")
    args = ap.parse_args()

    strays = stray_workers()
    if strays and not args.allow_stray_workers:
        sys.stderr.write("stray worker processes are resident; kill them or pass --allow-stray-workers:\n")
        sys.stderr.write("\n".join(strays[:20]) + "\n")
        return 2

    work_root = os.environ.get("EHR_WORK_ROOT")
    if not work_root:
        sys.stderr.write("EHR_WORK_ROOT is not set\n")
        return 2
    log_dir = Path(work_root) / args.dataset / "runs" / f"rebuild-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log_dir.mkdir(parents=True, exist_ok=True)

    record: dict = {
        "dataset": args.dataset,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config_hash_at_start": config_hash(args.dataset),
        "workers": args.workers,
        "stray_workers_at_start": len(strays),
        "stages": [],
    }
    failed = False
    for name, argv in STAGES:
        if name in args.skip:
            continue
        if failed and not (args.continue_on_failure and name == "validate"):
            record["stages"].append({"stage": name, "skipped": "an earlier stage failed"})
            continue
        # The subcommand first, then its options: typer parses `ehr2trace ingest
        # --dataset x`, and the other order is a parse error every stage fails on.
        stage_argv = [*argv, "--dataset", args.dataset]
        if args.workers and name in ("ingest", "canonical"):
            stage_argv += ["--workers", str(args.workers)]
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {name} ...", flush=True)
        result = run_stage(name, stage_argv, log_dir)
        record["stages"].append(result)
        print(f"    exit {result['exit_code']} in {result['wall_seconds']}s", flush=True)
        # inspect exits 1 while any blocker is open, which for the reference export is
        # the known label questions; it is reported, not treated as a failed build.
        if result["exit_code"] != 0 and name != "inspect":
            failed = True
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(record, indent=2), encoding="utf-8")

    audit_out = args.audit_out or args.record.with_name("conversion_audit.json")
    audit_tool = ROOT / "tools" / "audit_conversion.py"
    if audit_tool.exists() and not failed:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] audit ...", flush=True)
        started = time.monotonic()
        log = log_dir / "audit.log"
        with open(log, "w", encoding="utf-8") as fh:
            proc = subprocess.run(
                [PYTHON, str(audit_tool), "--dataset", args.dataset, "--out", str(audit_out)],
                stdout=fh, stderr=subprocess.STDOUT, cwd=ROOT,
            )
        record["stages"].append({
            "stage": "audit", "exit_code": proc.returncode,
            "wall_seconds": round(time.monotonic() - started, 1), "log": str(log), "out": str(audit_out),
        })

    record["config_hash_at_end"] = config_hash(args.dataset)
    record["config_changed_during_build"] = record["config_hash_at_end"] != record["config_hash_at_start"]
    record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["total_wall_seconds"] = round(sum(s.get("wall_seconds", 0) for s in record["stages"]), 1)
    args.record.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"record: {args.record}")
    if record["config_changed_during_build"]:
        print("WARNING: the dataset config changed during the build; its ingest outputs are stale", file=sys.stderr)
        return 3
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
