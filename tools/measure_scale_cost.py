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
wall time and peak memory, so they are read from the original build rather than repeated;
re-running a fifty-three-minute ingest to learn a number it already wrote down would be
theatre. Only the layers with no recorded timing are rebuilt.

Usage::

    python tools/measure_scale_cost.py --dataset datasets/mimiciv.yaml \\
        --built $WORK/mimiciv --work $WORK/_scalelab --stages omop,meds \\
        --vocab /path/to/vocab --out results/cost.json
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.config import load_dataset_config  # noqa: E402
from ehr2cdm.digest import digest_duckdb, digest_meds_data  # noqa: E402
from ehr2cdm.faults import clone_work_tree  # noqa: E402
from ehr2cdm.paths import WorkLayout, portable_work_path  # noqa: E402


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


def measure(layout: WorkLayout, config: Path, stage: str, env: dict, vocab: Path | None) -> dict:
    cmd = [sys.executable, "-m", "ehr2cdm.cli", stage, "--dataset", str(config)]
    if stage == "omop" and vocab:
        cmd += ["--vocabulary", str(vocab)]
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    t0 = time.time()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "stage": stage,
        "wall_seconds": round(time.time() - t0, 1),
        # ru_maxrss is the high-water mark over all children, so it is only attributable
        # to this stage when stages are run one at a time, which they are here.
        "peak_rss_gb": round(max(after, before) / (1024**2), 2),
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr.strip()[-600:] if proc.returncode else "",
        "source": "measured",
    }


def layer_digest(layout: WorkLayout, stage: str, scratch: Path) -> object:
    if stage == "omop":
        db = Path(layout.omop_dir) / "omop.duckdb"
        return digest_duckdb(db) if db.exists() else None
    if stage == "meds":
        return digest_meds_data(Path(layout.meds_dir), temp_dir=scratch)
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
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    original = WorkLayout(root=args.built, dataset_id=cfg.dataset_id)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]

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
        target = Path(clone.omop_dir) if stage == "omop" else Path(clone.meds_dir)
        shutil.rmtree(target, ignore_errors=True)

    env = dict(os.environ)
    env["EHR_WORK_ROOT"] = str(args.work)
    env["OFFLINE_MODE"] = "1"
    if args.vocab:
        env["OMOP_VOCAB_DIR"] = str(args.vocab)
    if args.root_env and args.data:
        env[args.root_env] = str(args.data)

    measured = []
    for stage in stages:
        print(f"rebuilding {stage}", flush=True)
        row = measure(clone, args.dataset, stage, env, args.vocab)
        measured.append(row)
        print(f"  {stage}: {row['wall_seconds']}s, peak {row['peak_rss_gb']} GB"
              f"{' FAILED' if row['returncode'] else ''}", flush=True)

    print("digesting the rebuild", flush=True)
    after = {s: layer_digest(clone, s, scratch) for s in stages}
    reproduced = {s: before[s] == after[s] for s in stages}

    summary = {
        "dataset": cfg.dataset_id,
        "built": portable_work_path(args.built),
        "recorded": recorded_costs(original),
        "measured": measured,
        "rebuilt_identically": reproduced,
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
