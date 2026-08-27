"""Three claims the paper makes about the converter, measured instead of asserted.

The abstract calls this a *deterministic* converter and the fault experiment reports a
detection rate. Neither statement had a measurement behind it. Determinism was a design
intention, and a detection rate with no false-positive rate is half a number -- a suite
that fails on everything detects every fault and is worthless.

This builds the same fixture several times under settings that are legal to vary and
that the content address is supposed to ignore, and reads three things off the result:

  determinism     do all builds agree, artifact by artifact, on a digest computed from
                  the rows themselves? Byte comparison would be the wrong test: parquet
                  embeds a writer version and its own block layout, so two files can be
                  logically identical and physically different. The digest is over sorted
                  row content.

  false positives do the checks pass on every one of these builds? A check that fails
                  here fails on a correct conversion, which makes it noise. This is the
                  denominator the fault experiment's 17/17 is missing.

  cost            wall time and peak resident memory per stage per configuration, which
                  is the only place in the paper where the price of the contract appears.

The configurations vary worker count and chunk size, both of which are execution
settings that were deliberately excluded from the content address after an earlier
incident: lowering the worker count on a large run invalidated a fifty-three-minute
ingest whose output was identical either way. Bucket count is *not* varied, because it
is part of the address by design -- bucket 5-of-64 and bucket 5-of-256 hold different
subjects, so treating them as the same artifact would be wrong.

Usage::

    python tools/run_reproducibility_experiment.py --dataset datasets/ctpe_shape.yaml \\
        --root-env CTPE_SHAPE_ROOT --data tests/fixtures/ctpe_shape \\
        --work /path/to/scratch --out results/reproducibility.json
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

from ehr2cdm.digest import changed, fingerprint  # noqa: E402

# Legal variations. Each is a setting the content address is supposed to ignore, so
# every build here must agree with every other one.
CONFIGURATIONS = (
    {"label": "workers=1", "workers": 1, "chunk_rows": 1000},
    {"label": "workers=2", "workers": 2, "chunk_rows": 1000},
    {"label": "workers=8", "workers": 8, "chunk_rows": 1000},
    {"label": "workers=4,chunk=97", "workers": 4, "chunk_rows": 97},
)

STAGES = ("ingest", "identity", "canonical", "omop", "meds")


def write_config(source: Path, dest: Path, workers: int, chunk_rows: int) -> None:
    import yaml

    cfg = yaml.safe_load(source.read_text())
    cfg.setdefault("execution", {})
    cfg["execution"]["workers"] = workers
    cfg["execution"]["chunk_rows"] = chunk_rows
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def run_build(config: Path, work: Path, env: dict[str, str], vocab: Path | None) -> list[dict]:
    """Run every stage, returning the per-stage cost. A failure is reported, not raised."""
    stages = []
    for stage in STAGES:
        cmd = [sys.executable, "-m", "ehr2cdm.cli", stage, "--dataset", str(config)]
        if stage == "omop" and vocab:
            cmd += ["--vocabulary", str(vocab)]
        t0 = time.time()
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        stages.append({
            "stage": stage,
            "seconds": round(time.time() - t0, 2),
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr.strip()[-400:] if proc.returncode else "",
        })
        if proc.returncode:
            break
    return stages


def stage_costs(work: Path, dataset_id: str) -> list[dict]:
    """Wall time and peak RSS as the runs themselves recorded it."""
    out = []
    for report in sorted((work / dataset_id / "runs").glob("*/report.json")):
        data = json.loads(report.read_text())
        env = data.get("environment", {})
        out.append({
            "run_id": data.get("run_id", ""),
            "stage": data.get("run_id", "").split("-")[0],
            "wall_seconds": env.get("wall_seconds"),
            "peak_rss_gb": env.get("peak_rss_gb"),
            "rows": sum(s.get("counts", {}).get("rows", 0) for s in data.get("stages", [])),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--root-env", required=True, help="env var the config reads its data root from")
    ap.add_argument("--data", type=Path, required=True, help="the read-only input tree")
    ap.add_argument("--work", type=Path, required=True, help="scratch root; one subdirectory per build")
    ap.add_argument("--vocab", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="do not delete the build trees afterwards")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    from ehr2cdm.config import load_dataset_config
    from ehr2cdm.paths import WorkLayout
    from ehr2cdm.validate import run_checks

    args.work.mkdir(parents=True, exist_ok=True)
    builds: list[dict] = []
    t0 = time.time()

    for spec in CONFIGURATIONS:
        work = args.work / spec["label"].replace("=", "").replace(",", "_")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        config = work / args.dataset.name
        write_config(args.dataset, config, spec["workers"], spec["chunk_rows"])

        env = dict(os.environ)
        env[args.root_env] = str(args.data)
        env["EHR_WORK_ROOT"] = str(work)
        env["OFFLINE_MODE"] = "1"
        if args.vocab:
            env["OMOP_VOCAB_DIR"] = str(args.vocab)

        print(f"[{spec['label']}] building", flush=True)
        stages = run_build(config, work, env, args.vocab)
        failed = [s for s in stages if s["returncode"]]

        cfg = load_dataset_config(config)
        layout = WorkLayout(root=work / cfg.dataset_id, dataset_id=cfg.dataset_id)
        results = [] if failed else run_checks(cfg, layout, include_slow=True)
        builds.append({
            "label": spec["label"],
            "workers": spec["workers"],
            "chunk_rows": spec["chunk_rows"],
            "stages": stages,
            "build_failed": bool(failed),
            "checks_run": len(results),
            "checks_failed": sorted(r.check_id for r in results if not r.passed and not r.skipped),
            "checks_skipped": sorted(r.check_id for r in results if r.skipped),
            "run_costs": stage_costs(work, cfg.dataset_id),
            "fingerprint": {} if failed else fingerprint(layout),
        })
        print(f"[{spec['label']}] {'FAILED' if failed else 'built'}; "
              f"{len(builds[-1]['checks_failed'])} check failures", flush=True)

    reference = next((b for b in builds if b["fingerprint"]), None)
    for build in builds:
        if build is reference or not build["fingerprint"]:
            build["differs_from_reference"] = None
            continue
        build["differs_from_reference"] = changed(
            reference["fingerprint"], build["fingerprint"]
        )

    reproduced = [b for b in builds if b.get("differs_from_reference") == []]
    all_failures = sorted({c for b in builds for c in b["checks_failed"]})
    summary = {
        "dataset": args.dataset.name,
        "configurations": len(builds),
        "reference": reference["label"] if reference else None,
        "identical_to_reference": len(reproduced),
        "artifacts_compared": len(reference["fingerprint"]) if reference else 0,
        # The false-positive denominator: every one of these builds is a correct
        # conversion, so any check failing on any of them is a false alarm.
        "false_positive_checks": all_failures,
        "false_positive_rate_pct": round(
            100.0 * len(all_failures) / max(1, builds[0]["checks_run"]), 1
        ) if builds else None,
        "checks_run": builds[0]["checks_run"] if builds else 0,
        "builds": builds,
        "seconds": round(time.time() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))

    if not args.keep:
        shutil.rmtree(args.work, ignore_errors=True)

    print(f"\n{len(builds)} configurations, {summary['artifacts_compared']} artifacts compared")
    print(f"  identical to {summary['reference']}: {summary['identical_to_reference']}/{len(builds) - 1}"
          if reference else "  no reference build")
    print(f"  checks run per build: {summary['checks_run']}")
    print(f"  checks failing on a correct build: {len(all_failures)} {all_failures}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
