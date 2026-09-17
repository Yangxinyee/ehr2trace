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

from ehr2trace.config import load_dataset_config  # noqa: E402
from ehr2trace.version import CODE_VERSION
from ehr2trace.digest import changed, fingerprint, mentions  # noqa: E402
from ehr2trace.faults import FAULTS, clone_work_tree  # noqa: E402
from ehr2trace.paths import WorkLayout  # noqa: E402
from ehr2trace.validate import CHECKS, run_checks  # noqa: E402


def check_state(cfg, layout: WorkLayout, slow: bool) -> dict[str, bool]:
    return {r.check_id: r.passed for r in run_checks(cfg, layout, include_slow=slow)}


def check_details(cfg, layout: WorkLayout, slow: bool) -> dict[str, str]:
    return {r.check_id: r.detail for r in run_checks(cfg, layout, include_slow=slow)}


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
    ap.add_argument(
        "--exclude-group",
        action="append",
        default=[],
        metavar="NAME=CHECK[,CHECK...]",
        help="exclude several checks at once and record under NAME why, for example the "
        "checks written after one kind of miss. Repeatable. The result file keeps the "
        "groups, so it says which checks an ablation removed and why.",
    )
    ap.add_argument("--software-commit", default=None,
                    help="the software commit the checks and faults were imported from, recorded verbatim")
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    wanted = {s.strip() for s in args.layers.split(",") if s.strip()}
    args.work.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[str]] = {}
    for spec in args.exclude_group:
        name, _, ids = spec.partition("=")
        if not name.strip() or not ids.strip():
            ap.error(f"--exclude-group wants NAME=CHECK[,CHECK...], got {spec!r}")
        groups.setdefault(name.strip(), []).extend(c.strip() for c in ids.split(",") if c.strip())
    excluded = set(args.exclude_check) | {c for ids in groups.values() for c in ids}
    # A mistyped id would exclude nothing and quietly report the full suite as the reduced one.
    unregistered = excluded - {check_id for check_id, _ in CHECKS}
    if unregistered:
        ap.error(f"excluded check(s) not in the registry: {sorted(unregistered)}")
    baseline_layout = WorkLayout(root=args.built, dataset_id=cfg.dataset_id)
    baseline = {k: v for k, v in check_state(cfg, baseline_layout, args.slow).items() if k not in excluded}
    # The clean tree's artifact digests. Diffing a mutated clone against these says which
    # artifacts a fault actually damaged, without each fault having to declare it -- and a
    # fault's own account of its blast radius is exactly the thing not to trust here.
    baseline_fingerprint = fingerprint(baseline_layout)
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
        # An injection that raised can leave the clone half-corrupted, and a check firing on
        # that state is evidence about the harness, not about the fault. Such a fault is not
        # run, not counted as detected, and named in the summary.
        injection_failed = effect.startswith("injection failed")
        not_run = skipped or injection_failed

        damaged = [] if not_run else changed(baseline_fingerprint, fingerprint(layout))
        detail = {} if not_run else check_details(cfg, layout, args.slow)
        after = {k: v for k, v in check_state(cfg, layout, args.slow).items() if k not in excluded} if not not_run else {}
        # A detector is a check that passes clean and fails dirty. Anything already
        # failing on the clean build tells us nothing about this fault.
        detectors = sorted(k for k, ok in after.items() if not ok and baseline.get(k, False))
        expected_hit = sorted(set(f.expect) & set(detectors))
        expected_miss = sorted(set(f.expect) - set(detectors))
        # Detection is not the same as being able to act on it. A check that fires but
        # names no damaged artifact leaves an engineer with a failing build and nowhere to
        # start, which is the limitation this column exists to measure rather than concede.
        pointing = {c: mentions(f"{c} {detail.get(c, '')}", damaged) for c in detectors}
        pointing = {c: v for c, v in pointing.items() if v}

        results.append(
            {
                "fault_id": f.id,
                "layer": f.layer,
                "silent": f.silent,
                "description": f.description,
                "origin": f.origin,
                "effect": effect,
                "skipped": skipped,
                "injection_failed": injection_failed,
                "detected": bool(detectors),
                "detectors": detectors,
                "n_detectors": len(detectors),
                "expected": sorted(f.expect),
                "expected_hit": expected_hit,
                "expected_missed": expected_miss,
                "unexpected_detectors": sorted(set(detectors) - set(f.expect)),
                "damaged_artifacts": damaged,
                "detectors_naming_a_damaged_artifact": sorted(pointing),
                "localised": bool(pointing),
                "seconds": round(time.time() - t0, 1),
            }
        )
        mark = "SKIP" if skipped else ("FAILED  " if injection_failed else ("DETECTED" if detectors else "MISSED  "))
        print(f"  [{mark}] {f.id:44} {len(detectors):>2} detector(s)  {effect[:60]}")
        shutil.rmtree(scratch, ignore_errors=True)

    ran = [r for r in results if not r["skipped"] and not r["injection_failed"]]
    summary = {
        "dataset": cfg.dataset_id,
        "excluded_checks": sorted(excluded),
        # Why each excluded check is excluded, as the operator grouped them; empty when
        # nothing was excluded or checks were excluded one by one.
        "excluded_check_groups": {name: sorted(ids) for name, ids in groups.items()},
        "layers_run": sorted(wanted),
        "software_commit": args.software_commit,
        # Read from the package, not from an environment variable nobody sets. The old
        # name predated the rename and had been recording an empty string into every
        # result file since -- a measurement that does not say which code it measured.
        "code_version": CODE_VERSION,
        "checks_total": len(baseline),
        "baseline_passing": sum(baseline.values()),
        "baseline_failing": clean_failures,
        "faults_total": len(results),
        "faults_run": len(ran),
        "faults_skipped": sum(1 for r in results if r["skipped"]),
        "faults_injection_failed": sorted(r["fault_id"] for r in results if r["injection_failed"]),
        "faults_detected": sum(1 for r in ran if r["detected"]),
        "faults_missed": sorted(r["fault_id"] for r in ran if not r["detected"]),
        "detection_rate": round(sum(1 for r in ran if r["detected"]) / len(ran), 3) if ran else None,
        "faults_localised": sum(1 for r in ran if r["localised"]),
        "faults_detected_but_not_localised": sorted(
            r["fault_id"] for r in ran if r["detected"] and not r["localised"]
        ),
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
    if summary["faults_injection_failed"]:
        raise SystemExit(f"injection failed for {summary['faults_injection_failed']}: those faults were not measured")


if __name__ == "__main__":
    main()
