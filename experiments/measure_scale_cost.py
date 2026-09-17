"""What the contract costs, and whether determinism survives the real dataset.

Two gaps the fixture cannot close. The first is price: every claim in this paper is about
correctness, and a reader deciding whether to adopt any of it is entitled to know what it
costs to run. The second is scope: the reproducibility experiment shows that four
concurrency settings agree on a four-subject fixture, which is evidence about the code
and not about the scale it is claimed to work at.

Both are answered the same way. Clone a built work tree, discard the layers to be
measured, rebuild them, and compare what comes out against what was there. The clone is
made of hard links, so a 272 GB tree costs no disk to copy and the rebuild writes new
inodes rather than through them.

The stages that already write a run report -- ingest and canonical -- record their own
wall time and peak memory, so by default they are read from the original build rather
than repeated; re-running a fifty-three-minute ingest to learn a number it already wrote
down would be theatre. Only the layers with no recorded timing are rebuilt. The exception
is a recorded run that was not measured under conditions worth reporting -- the 0.6.0
build of MIMIC-IV ran its ingest and canonical stages beside 69 GB of orphaned worker
processes from an earlier month -- and then ``--stages`` may name those layers too, and
they are discarded from the clone and rebuilt like the others. The canonical layer is
digested before and after as well, in the engine, so determinism is measured there at
full scale rather than assumed from the fixture.

Usage::

    python tools/measure_scale_cost.py --dataset datasets/mimiciv.yaml \\
        --built $WORK/mimiciv --work $WORK/_scalelab --stages omop,meds \\
        --vocab /path/to/vocab --out results/cost.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ehr2trace  # noqa: E402
from ehr2trace.config import load_dataset_config  # noqa: E402
from ehr2trace.digest import digest_duckdb, digest_meds_data, digest_parquet_files  # noqa: E402
from ehr2trace.faults import clone_work_tree  # noqa: E402
from ehr2trace.paths import WorkLayout, portable_work_path  # noqa: E402


def recorded_costs(layout: WorkLayout) -> list[dict]:
    """Wall time and peak memory as the runs themselves recorded it."""
    out = []
    for report in sorted((Path(layout.root) / "runs").glob("*/report.json")):
        data = json.loads(report.read_text())
        env = data.get("environment", {})
        run_id = data.get("run_id", "")
        out.append({
            "stage": run_id.split("-")[0],
            "run_id": run_id,
            "wall_seconds": env.get("wall_seconds"),
            "peak_rss_gb": env.get("peak_rss_gb"),
            "workers": data.get("execution", {}).get("workers"),
            "source": "run report",
        })
    return out


#: What each stage writes, as layout attributes: the directories a clone must lose for
#: the stage to have to do the work. Quarantine is per stage underneath one directory.
WRITES = {
    "ingest": ("manifest_dir", "source_dir", "staged_dir"),
    "identity": ("identity_dir",),
    "canonical": ("canonical_dir",),
    "omop": ("omop_dir",),
    "meds": ("meds_dir",),
}

CANONICAL_TABLES = ("events", "event_source", "anchors", "cohort_membership", "quality_issue", "quarantine")

#: Runs one stage in a fresh interpreter and writes the peak resident size of that
#: stage's whole process tree. ru_maxrss under RUSAGE_CHILDREN is a high-water mark
#: over every child a process has ever waited for and never comes back down, so read
#: from this process it would report the ingest's peak for every stage after it. A
#: fresh process has waited for nothing, so its children's mark is this stage's alone.
_ISOLATED = (
    "import resource, subprocess, sys\n"
    "rc = subprocess.run(sys.argv[2:]).returncode\n"
    "open(sys.argv[1], 'w').write(str(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss))\n"
    "sys.exit(rc)\n"
)


def measure(layout: WorkLayout, config: Path, stage: str, env: dict, vocab: Path | None,
            workers: int | None = None, scratch: Path | None = None) -> dict:
    cmd = [sys.executable, "-m", "ehr2trace.cli", stage, "--dataset", str(config)]
    if stage == "omop" and vocab:
        cmd += ["--vocabulary", str(vocab)]
    if workers:
        cmd += ["--workers", str(workers)]
    mark = (scratch or Path(".")) / f"{stage}.maxrss"
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-c", _ISOLATED, str(mark), *cmd],
                          env=env, capture_output=True, text=True)
    peak_kb = int(mark.read_text()) if mark.exists() else 0
    return {
        "stage": stage,
        "wall_seconds": round(time.time() - t0, 1),
        "peak_rss_gb": round(peak_kb / (1024**2), 2),
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr.strip()[-600:] if proc.returncode else "",
        "workers": workers,
        "source": "measured",
    }


def layer_digest(layout: WorkLayout, stage: str, scratch: Path) -> object:
    if stage == "omop":
        db = Path(layout.omop_dir) / "omop.duckdb"
        return digest_duckdb(db) if db.exists() else None
    if stage == "meds":
        return digest_meds_data(Path(layout.meds_dir), temp_dir=scratch)
    if stage == "canonical":
        return digest_parquet_files({n: Path(layout.canonical_dir) / f"{n}.parquet" for n in CANONICAL_TABLES},
                                    temp_dir=scratch)
    if stage == "identity":
        return digest_parquet_files({"subject_map": Path(layout.identity_dir) / "subject_map.parquet"})
    # Staged output is a directory of many files and its determinism is not a claim the
    # paper makes; the ingest stage is timed, not digested.
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--built", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--stages", default="omop,meds")
    ap.add_argument("--vocab", type=Path, default=None)
    ap.add_argument("--root-env", default=None, help="env var the config reads its data root from")
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--ingest-workers", type=int, default=None, help="passed to `ingest --workers` when it is measured")
    ap.add_argument("--canonical-workers", type=int, default=None, help="passed to `canonical --workers` when it is measured")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    original = WorkLayout(root=args.built, dataset_id=cfg.dataset_id)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in WRITES]
    if unknown:
        raise SystemExit(f"unknown stage(s) {unknown}; choose from {list(WRITES)}")

    scratch = args.work / "_tmp"
    if args.work.exists():
        shutil.rmtree(args.work)
    args.work.mkdir(parents=True)
    scratch.mkdir()

    print("digesting the original", flush=True)
    t0 = time.time()
    before = {s: layer_digest(original, s, scratch) for s in stages}
    print(f"  ({time.time() - t0:.0f}s)", flush=True)

    clone_root = args.work / cfg.dataset_id
    print("cloning (hard links)", flush=True)
    t1 = time.time()
    clone_work_tree(args.built, clone_root)
    print(f"  ({time.time() - t1:.0f}s)", flush=True)
    clone = WorkLayout(root=clone_root, dataset_id=cfg.dataset_id)

    # Discard exactly the layers being measured, so the stage has to do the work rather
    # than recognise its own content address and return.
    for stage in stages:
        for attr in WRITES[stage]:
            shutil.rmtree(Path(getattr(clone, attr)), ignore_errors=True)
        shutil.rmtree(Path(clone.quarantine_dir) / stage, ignore_errors=True)

    env = dict(os.environ)
    env["EHR_WORK_ROOT"] = str(args.work)
    env["OFFLINE_MODE"] = "1"
    # The registry defaults to ./mappings and this tool runs from the paper checkout,
    # which has none: the 2026-09-17 rebuilds published every type concept as 0 and
    # lost 893 confirmed terms that way, and the digest blamed the converter.
    mappings = Path(os.environ.get("EHR_MAPPINGS_DIR")
                    or Path(ehr2trace.__file__).resolve().parents[2] / "mappings")
    if not mappings.is_dir():
        raise SystemExit(f"no mapping registry at {mappings}; set EHR_MAPPINGS_DIR")
    env["EHR_MAPPINGS_DIR"] = str(mappings)
    if args.vocab:
        env["OMOP_VOCAB_DIR"] = str(args.vocab)
    if args.root_env and args.data:
        env[args.root_env] = str(args.data)

    workers = {"ingest": args.ingest_workers, "canonical": args.canonical_workers}
    measured = []
    for stage in stages:
        print(f"rebuilding {stage}", flush=True)
        row = measure(clone, args.dataset, stage, env, args.vocab, workers.get(stage), scratch)
        measured.append(row)
        print(f"  {stage}: {row['wall_seconds']}s, peak {row['peak_rss_gb']} GB"
              f"{' FAILED' if row['returncode'] else ''}", flush=True)

    print("digesting the rebuild", flush=True)
    after = {s: layer_digest(clone, s, scratch) for s in stages}
    # Only a stage with a digest can be said to reproduce; a None on both sides is not
    # agreement, it is the absence of a measurement.
    reproduced = {s: before[s] == after[s] for s in stages if before[s] is not None}

    summary = {
        "dataset": cfg.dataset_id,
        "built": portable_work_path(args.built),
        "recorded": recorded_costs(original),
        "measured": measured,
        "rebuilt_identically": reproduced,
        "mappings": str(mappings),
        "digest_before": before,
        "digest_after": after,
        "seconds": round(time.time() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))

    if not args.keep:
        shutil.rmtree(args.work, ignore_errors=True)

    print()
    for row in summary["recorded"] + measured:
        print(f"  {row['stage']:<10} {row['wall_seconds']:>9}s  peak {row['peak_rss_gb']:>7} GB"
              f"   ({row['source']})")
    for stage, same in reproduced.items():
        print(f"  {stage}: rebuild {'reproduces' if same else 'DIFFERS FROM'} the original")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
