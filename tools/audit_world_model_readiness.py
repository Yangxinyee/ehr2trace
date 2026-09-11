"""Read-only aggregate audit of converted artifacts for patient trajectory learning.

No patient identifiers, notes, or row-level values are written to the repository.
This is a readiness inventory, not an additional validation pass or an RL benchmark.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import duckdb
import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=ROOT/'results/world_model_readiness.json')
    args = parser.parse_args()
    con = duckdb.connect()
    con.execute('SET threads=2')
    con.execute("SET memory_limit='6GB'")
    out = {'collected_utc':datetime.now(timezone.utc).isoformat(),
           'scope':'Full canonical aggregate scans; one MEDS shard schema per dataset; saved validation metrics. No patient-level export, new clinical experiment, or validation rerun.',
           'repository_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           'implementation_sha256':{str(p.relative_to(ROOT)):sha(p) for p in [ROOT/'src/ehr2trace/schema.py',ROOT/'src/ehr2trace/meds.py',ROOT/'src/ehr2trace/canonical/normalize.py',ROOT/'tools/prepare_mimiciv.py',Path(__file__)]},
           'datasets':{}}
    for name in ('ctpe','mimiciv','cu_ctpa'):
        root = args.work_root/name
        canonical = root/'canonical/events.parquet'
        schema = pq.read_schema(canonical)
        print('Scanning canonical aggregate fields:',name,flush=True)
        con.read_parquet(str(canonical)).create_view('events',replace=True)
        result = con.execute('''SELECT source_id,event_kind,count(*) AS events,
          count(*) FILTER (WHERE event_time IS NULL) AS untimed,
          count(*) FILTER (WHERE event_time IS NOT NULL AND available_time IS NULL) AS timed_without_recorded_availability,
          count(*) FILTER (WHERE encounter_id IS NULL) AS no_encounter,
          count(*) FILTER (WHERE dose_source IS NOT NULL AND trim(dose_source) <> '') AS with_dose_text,
          count(*) FILTER (WHERE route_source IS NOT NULL AND trim(route_source) <> '') AS with_route_text,
          count(*) FILTER (WHERE status_source IS NOT NULL AND trim(status_source) <> '') AS with_status_text,
          count(*) FILTER (WHERE unit_source IS NOT NULL AND trim(unit_source) <> '') AS with_unit,
          count(*) FILTER (WHERE value_number IS NOT NULL) AS with_numeric_value,
          count(*) FILTER (WHERE end_time IS NOT NULL) AS with_end_time,
          count(*) FILTER (WHERE end_time < event_time) AS inverted_intervals
          FROM events GROUP BY source_id,event_kind ORDER BY source_id,event_kind''')
        fields = [x[0] for x in result.description]
        groups = [dict(zip(fields,row)) for row in result.fetchall()]
        demographics = con.execute('''SELECT source_code,count(*) AS events,
          count(*) FILTER (WHERE event_time IS NULL AND available_time IS NULL) AS timeless
          FROM events WHERE event_kind='demographic' GROUP BY source_code ORDER BY source_code''').fetchall()
        vital = con.execute('''SELECT count(*) AS events,
          count(*) FILTER (WHERE event_time IS NULL AND available_time IS NULL) AS timeless,
          count(*) FILTER (WHERE lower(value_text)='deceased' AND event_time IS NULL AND available_time IS NULL) AS timeless_deceased
          FROM events WHERE source_code='VITAL_STATUS' AND event_kind='demographic' ''').fetchone()
        # Only public order-type categories, never free text or note contents.
        poe = con.execute("SELECT source_code,count(*) FROM events WHERE source_id='poe' GROUP BY source_code ORDER BY count(*) DESC").fetchall() if name=='mimiciv' else []
        first_shard = next((root/'meds/data').rglob('*.parquet'))
        meds_schema = pq.read_schema(first_shard)
        checks = json.loads((root/'runs/validation.json').read_text())
        selected = {r['check_id']:{'passed':r['passed'],'skipped':r.get('skipped',False),'metrics':r['metrics']}
                    for r in checks if r['check_id'] in ('END_TIME_NEVER_PRECEDES_START','MEDS_AVAILABILITY_PREVENTS_LEAKAGE')}
        cfg_path = ROOT/'datasets'/f'{name}.yaml'
        cfg = yaml.safe_load(cfg_path.read_text())
        d = {'canonical_rows':pq.ParquetFile(canonical).metadata.num_rows,
             'canonical_file':{'bytes':canonical.stat().st_size,'modified_utc':datetime.fromtimestamp(canonical.stat().st_mtime,timezone.utc).isoformat()},
             'canonical_schema':{f.name:str(f.type) for f in schema},
             'example_meds_shard_schema':{f.name:str(f.type) for f in meds_schema},
             'by_source_and_kind':groups,
             'demographic_codes':[{'code':code,'events':n,'timeless':t} for code,n,t in demographics],
             'vital_status':dict(zip(('events','timeless','timeless_deceased'),vital)),
             'poe_order_types':dict(poe), 'saved_validation_metrics':selected,
             'validation_sha256':sha(root/'runs/validation.json'),
             'config_sha256':sha(cfg_path),'configured_sources':list(cfg['sources'])}
        assert sum(x['events'] for x in groups)==d['canonical_rows']
        out['datasets'][name]=d
        args.out.write_text(json.dumps(out,indent=2)+'\n')
        print(name,json.dumps({'events':d['canonical_rows'],'groups':len(groups),'vital_status':d['vital_status']}),flush=True)
    con.close()


if __name__=='__main__':
    main()
