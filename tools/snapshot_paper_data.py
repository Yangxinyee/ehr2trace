"""Read built datasets and save aggregate evidence for the paper, without patient rows.

Usage: .venv/bin/python tools/snapshot_paper_data.py --work-root /path/to/work
The work root is opened read-only. Only results/paper_snapshot.json is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]


def stamp(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(work):
    data = {}
    con = duckdb.connect()
    con.execute("SET threads=2")
    for name in ("ctpe", "mimiciv"):
        root = work / name
        omop_path = root / "omop/build_report.json"
        meds_path = root / "meds/build_report.json"
        validation_path = root / "runs/validation.json"
        omop = json.loads(omop_path.read_text())
        meds = json.loads(meds_path.read_text())
        checks = json.loads(validation_path.read_text())
        by_id = {x["check_id"]: x for x in checks}
        files = {n: root / "canonical" / f"{n}.parquet"
                 for n in ("events", "event_source", "anchors", "quarantine")}
        counts = {n: pq.ParquetFile(p).metadata.num_rows for n, p in files.items()}
        db = duckdb.connect(str(root / "omop/omop.duckdb"), read_only=True)
        db.execute("SET threads=2")
        tables = {}
        for table, field in (("condition_occurrence", "condition_concept_id"),
                             ("drug_exposure", "drug_concept_id"),
                             ("measurement", "measurement_concept_id"),
                             ("procedure_occurrence", "procedure_concept_id"),
                             ("observation", "observation_concept_id")):
            exists = db.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?", [table]).fetchone()[0]
            if not exists:
                continue
            n, mapped = db.execute(f"SELECT count(*), count(*) FILTER (WHERE {field} <> 0) FROM {table}").fetchone()
            tables[table] = {"rows": n, "mapped_rows": mapped,
                             "mapped_pct": round(100 * mapped / n, 4) if n else None}
        persons = db.execute("SELECT count(*) FROM person").fetchone()[0]
        lineage = db.execute("SELECT count(*) FROM etl_audit.lineage").fetchone()[0]
        db.close()
        canonical_subjects = con.execute("SELECT count(DISTINCT subject_id) FROM read_parquet(?)", [str(files['events'])]).fetchone()[0]
        quarantine = con.execute("SELECT reason, count(*) FROM read_parquet(?) GROUP BY reason ORDER BY reason", [str(files['quarantine'])]).fetchall()
        # Reading split metadata gives counts without emitting subject identifiers.
        splits = con.execute("SELECT split, count(*) FROM read_parquet(?) GROUP BY split", [str(root / 'meds/metadata/subject_splits.parquet')]).fetchall()
        report_times = {"omop": stamp(omop_path), "meds": stamp(meds_path), "validation": stamp(validation_path)}
        data[name] = {
            "source_rows": by_id["RECONCILIATION_ROWS"]["metrics"]["rows_read"],
            "identity_subjects": json.loads((root / "identity/summary.json").read_text())["subjects"],
            "canonical_subjects": canonical_subjects, "canonical": counts,
            "canonical_quarantine_by_reason": dict(quarantine),
            "source_accounting_recorded": by_id["SOURCE_ROWS_ACCOUNTED"]["metrics"],
            "omop_persons": persons, "omop_lineage_rows": lineage,
            "omop_tables": tables, "vocabulary": omop["vocabulary"],
            "meds": {k: meds[k] for k in ("events", "subjects", "shards", "codes", "mapped_codes")},
            "meds_splits_from_metadata": dict(splits),
            "validation_recorded": {
                "total": len(checks), "passed": sum(x['passed'] and not x.get('skipped', False) for x in checks),
                "skipped": sum(bool(x.get('skipped')) for x in checks),
                "failed": sum(not x['passed'] for x in checks),
                "check_ids": [x['check_id'] for x in checks],
                "nonpassing_checks": [{"check_id": x['check_id'], "skipped": bool(x.get('skipped')), "detail": x['detail']}
                                      for x in checks if x.get('skipped') or not x['passed']],
                "interval_ordering": by_id.get('END_TIME_NEVER_PRECEDES_START', {}).get('metrics'),
                "predates_target_reports": report_times['validation'] < max(report_times['omop'], report_times['meds']),
            },
            "report_modified_utc": report_times,
            "report_sha256": {"omop": digest(omop_path), "meds": digest(meds_path), "validation": digest(validation_path)},
            "canonical_files": {n: {"bytes": p.stat().st_size, "modified_utc": stamp(p)} for n,p in files.items()},
        }
        assert counts['events'] == meds['events'], f"{name}: canonical and MEDS report disagree"
        assert canonical_subjects == meds['subjects'], f"{name}: subject counts disagree"
        print(name, json.dumps({"canonical": counts, "subjects":canonical_subjects, "tables":tables, "validation":data[name]['validation_recorded']['total']}), flush=True)
    con.close()
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--work-root', type=Path, required=True)
    ap.add_argument('--out', type=Path, default=ROOT / 'results/paper_snapshot.json')
    args = ap.parse_args()
    from ehr2cdm.validate import CHECKS
    from ehr2cdm.version import CODE_VERSION
    doc = {"collected_utc": datetime.now(timezone.utc).isoformat(),
           "repository_head_at_collection": subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           "code_version": CODE_VERSION, "registered_check_ids": [x[0] for x in CHECKS],
           "scope": "Read-only aggregate inventory of the current artifacts and saved validation reports. Validation rerun provenance, when available, is recorded separately. Existing benchmark JSON files remain historical snapshots.",
           "datasets": snapshot(args.work_root)}
    rerun = ROOT / 'results/mimiciv_validation_rerun.json'
    if rerun.exists():
        evidence = json.loads(rerun.read_text())
        current = doc['datasets']['mimiciv']
        assert evidence['validation_sha256'] == current['report_sha256']['validation'], 'MIMIC validation changed after the recorded rerun'
        assert evidence['target_reports_unchanged'], 'MIMIC target reports changed during validation'
        for name in ('omop', 'meds'):
            assert evidence['target_reports_after'][name]['sha256'] == current['report_sha256'][name], f'MIMIC {name} report changed after validation'
        current['validation_rerun'] = {'artifact': str(rerun.relative_to(ROOT)), 'sha256': digest(rerun),
                                     'started_utc': evidence['started_utc'], 'completed_utc': evidence['completed_utc'],
                                     'include_slow': evidence['include_slow'], 'target_reports_unchanged': True}
    drug = ROOT / 'results/drug_match.json'
    d = json.loads(drug.read_text())
    doc['drug_match'] = {k:d[k] for k in ('confirmed_mappings','resolved','outcome','share_of_resolved','name_audit','vocabulary_version')}
    doc['drug_match']['name_audit'].pop('disagreements', None)
    doc['drug_match']['source_sha256'] = digest(drug)
    doc['drug_match']['source_modified_utc'] = stamp(drug)
    doc['evidence_files'] = {p.name:{'sha256':digest(p),'modified_utc':stamp(p)} for p in sorted((ROOT/'results').glob('*.json')) if p.name != args.out.name}
    doc['implementation_files'] = {str(p.relative_to(ROOT)): digest(p) for p in [ROOT/'src/ehr2cdm/drug_match.py',ROOT/'src/ehr2cdm/drug_lexicon.py',ROOT/'tools/measure_drug_match.py',ROOT/'tools/run_leakage_experiment.py',ROOT/'datasets/ctpe.yaml',ROOT/'datasets/mimiciv.yaml']}
    args.out.write_text(json.dumps(doc,indent=2)+'\n')


if __name__ == '__main__':
    main()
