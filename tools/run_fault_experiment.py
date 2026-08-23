"""Measure what the check suite detects, one injected fault at a time.

For each fault in the catalogue: clone the built work tree, inject, run every check,
record which ones fired. The comparison that matters is against the clean baseline --
a check that fails on the untouched build is not a detector, it is a bug.

The experiment reports three things per fault:

  detected      any check failed that passes on the clean build
  detectors     which checks those were
  as expected   whether the checks that fired include the ones a reader would predict

The third is recorded, never enforced. A fault caught by a check nobody predicted is a
more interesting result than one caught by the check named after it, and a catalogue
that only ever confirms its own expectations is not measuring anything.

Usage::

    python tools/run_fault_experiment.py --dataset datasets/ctpe_shape.yaml \\
        --work /tmp/faultlab --out results/faults.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.config import load_dataset_config  # noqa: E402
from ehr2cdm.faults import FAULTS, clone_work_tree  # noqa: E402
from ehr2cdm.paths import WorkLayout  # noqa: E402
from ehr2cdm.validate import run_checks  # noqa: E402


def check_state(cfg, layout: WorkLayout, slow: bool) -> dict[str, bool]:
    return {r.check_id: r.passed for r in run_checks(cfg, layout, include_slow=slow)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--built", type=Path, required=True, help="an already-built work tree for this dataset")
    ap.add_argument("--work", type=Path, required=True, help="scratch directory for the mutated clones")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--slow", action="store_true", help="include the full-dataset checks")
    ap.add_argument("--layers", default="canonical,omop,meds", help="which fault layers to run")
    ap.add_argument(
        "--exclude-check",
        action="append",
        default=[],
        help="ignore this check id when deciding detection. Repeatable. Used to measure "
        "what an earlier version of the suite would have caught, with everything else held "
        "fixed -- the only honest way to report a before-and-after.",
    )
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    wanted = {s.strip() for s in args.layers.split(",") if s.strip()}
    args.work.mkdir(parents=True, exist_ok=True)

    excluded = set(args.exclude_check)
    baseline_layout = WorkLayout(root=args.built, dataset_id=cfg.dataset_id)
    baseline = {k: v for k, v in check_state(cfg, baseline_layout, args.slow).items() if k not in excluded}
    if excluded:
        print(f"ignoring {len(excluded)} check(s): {sorted(excluded)}")
    clean_failures = sorted(k for k, ok in baseline.items() if not ok)
    print(f"baseline: {sum(baseline.values())}/{len(baseline)} checks pass")
    if clean_failures:
        print(f"  NOTE: already failing before injection: {clean_failures}")

    results = []
    for f in FAULTS:
        if f.layer not in wanted:
            continue
        scratch = args.work / f.id.lower()
        t0 = time.time()
        clone_work_tree(args.built, scratch)
        layout = WorkLayout(root=scratch, dataset_id=cfg.dataset_id)
        try:
            effect = f.apply(layout, cfg)
        except Exception as exc:
            effect = f"injection failed: {type(exc).__name__}: {exc}"
        skipped = effect.startswith("skipped")

        after = (
            {k: v for k, v in check_state(cfg, layout, args.slow).items() if k not in excluded}
            if not skipped
            else {}
        )
        # A detector is a check that passes clean and fails dirty. Anything already
        # failing on the clean build tells us nothing about this fault.
        detectors = sorted(k for k, ok in after.items() if not ok and baseline.get(k, False))
        expected_hit = sorted(set(f.expect) & set(detectors))
        expected_miss = sorted(set(f.expect) - set(detectors))

        results.append(
            {
                "fault_id": f.id,
                "layer": f.layer,
                "silent": f.silent,
                "description": f.description,
                "origin": f.origin,
                "effect": effect,
                "skipped": skipped,
                "detected": bool(detectors),
                "detectors": detectors,
                "n_detectors": len(detectors),
                "expected": sorted(f.expect),
                "expected_hit": expected_hit,
                "expected_missed": expected_miss,
                "unexpected_detectors": sorted(set(detectors) - set(f.expect)),
                "seconds": round(time.time() - t0, 1),
            }
        )
        mark = "SKIP" if skipped else ("DETECTED" if detectors else "MISSED  ")
        print(f"  [{mark}] {f.id:44} {len(detectors):>2} detector(s)  {effect[:60]}")
        shutil.rmtree(scratch, ignore_errors=True)

    ran = [r for r in results if not r["skipped"]]
    summary = {
        "dataset": cfg.dataset_id,
        "excluded_checks": sorted(excluded),
        "code_version": os.environ.get("EHR2CDM_VERSION", ""),
        "checks_total": len(baseline),
        "baseline_passing": sum(baseline.values()),
        "baseline_failing": clean_failures,
        "faults_total": len(results),
        "faults_run": len(ran),
        "faults_skipped": len(results) - len(ran),
        "faults_detected": sum(1 for r in ran if r["detected"]),
        "faults_missed": sorted(r["fault_id"] for r in ran if not r["detected"]),
        "detection_rate": round(sum(1 for r in ran if r["detected"]) / len(ran), 3) if ran else None,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(
        f"\n{summary['faults_detected']}/{summary['faults_run']} injected faults detected"
        f" ({summary['faults_skipped']} not applicable to this dataset)"
    )
    if summary["faults_missed"]:
        print(f"MISSED: {summary['faults_missed']}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
