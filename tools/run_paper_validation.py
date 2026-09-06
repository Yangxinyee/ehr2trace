"""Run the existing full validator and retain aggregate provenance for the paper.

Existing converted data are read by the validator; its report and scratch files
are written to the dataset work directory. The previous report is backed up.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from ehr2cdm.config import load_dataset_config
from ehr2cdm.paths import WorkLayout
from ehr2cdm import validate

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--vocab-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    cfg = load_dataset_config(args.dataset)
    layout = WorkLayout(args.work_root / cfg.dataset_id, cfg.dataset_id)
    os.environ['OMOP_VOCAB_DIR'] = str(args.vocab_dir.resolve())
    report = layout.runs_dir / 'validation.json'
    previous_hash = digest(report) if report.exists() else None
    if report.exists():
        backup = report.with_name('validation.before-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        shutil.copy2(report, backup)

    def targets():
        return {name: {'sha256': digest(path), 'modified_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
                for name in ('omop', 'meds')
                for path in [layout.root / name / 'build_report.json']}

    evidence = {'dataset': cfg.dataset_id, 'started_utc': now(), 'include_slow': True,
                'repository_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'validator_sha256': digest(Path(validate.__file__)),
                'config_sha256': digest(args.dataset), 'runner_sha256': digest(Path(__file__)),
                'previous_validation_sha256': previous_hash, 'target_reports_before': targets()}
    timings = {}
    original_checks = validate.CHECKS[:]

    def timed(check_id, fn):
        def run(layers):
            start = time.monotonic()
            print(f'START {check_id}', flush=True)
            try:
                result = fn(layers)
                status = 'OMITTED' if result is None else 'SKIP' if result.skipped else 'PASS' if result.passed else 'FAIL'
                print(f'{status} {check_id}', flush=True)
                return result
            finally:
                timings[check_id] = round(time.monotonic() - start, 3)
        return run

    validate.CHECKS[:] = [(check_id, (timed(check_id, fn), slow)) for check_id, (fn, slow) in original_checks]
    start = time.monotonic()
    print(f'Loading canonical layers for {cfg.dataset_id}; {len(original_checks)} registered checks, include_slow=True', flush=True)
    try:
        results = validate.run_checks(cfg, layout, include_slow=True)
    finally:
        validate.CHECKS[:] = original_checks
    evidence.update(completed_utc=now(), elapsed_seconds=round(time.monotonic() - start, 3),
                    target_reports_after=targets(), validation_sha256=digest(report),
                    total=len(results), passed=sum(r.passed and not r.skipped for r in results),
                    skipped=sum(r.skipped for r in results), failed=sum(not r.passed for r in results),
                    checks=[{'check_id': r.check_id, 'passed': r.passed, 'skipped': r.skipped,
                             'elapsed_seconds': timings[r.check_id]} for r in results])
    evidence['target_reports_unchanged'] = evidence['target_reports_before'] == evidence['target_reports_after']
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: evidence[k] for k in ('total', 'passed', 'skipped', 'failed', 'elapsed_seconds', 'target_reports_unchanged')}), flush=True)
    raise SystemExit(1 if evidence['failed'] or not evidence['target_reports_unchanged'] else 0)


if __name__ == '__main__':
    main()
