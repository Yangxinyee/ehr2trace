"""Synthetic diagnostic: does the current as-of helper isolate future attributes?

Uses no patient data. Writes only diagnostic booleans, not synthetic event rows.
This deliberately probes behavior outside the existing 36-check validator.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from ehr2trace.meds import MEDS_SCHEMA, as_of_view
from ehr2trace.paths import WorkLayout

ROOT = Path(__file__).resolve().parents[1]


def main():
    cutoff = datetime(2020,1,1,12)
    with tempfile.TemporaryDirectory(prefix='ehr-visibility-probe-') as temp:
        layout = WorkLayout(Path(temp), 'synthetic')
        data = layout.meds_dir/'data';data.mkdir(parents=True)
        rows = [dict(subject_id=1,time=None,available_time=None,code='SOURCE/person/VITAL_STATUS',
                     text_value='Deceased',event_id='synthetic-status',event_kind='demographic'),
                dict(subject_id=1,time=datetime(2020,1,1),available_time=datetime(2020,1,1),
                     end_time=datetime(2020,1,5),code='SOURCE/visit/ADMISSION',event_id='synthetic-visit',event_kind='visit')]
        pq.write_table(pa.Table.from_pylist(rows,schema=MEDS_SCHEMA),data/'synthetic.parquet')
        before = as_of_view(layout,cutoff,subject_id=1)
        first = before.filter(before['event_id']=='synthetic-status')['text_value'].to_list()
        rows[0]['text_value']='Alive'
        pq.write_table(pa.Table.from_pylist(rows,schema=MEDS_SCHEMA),data/'synthetic.parquet')
        after = as_of_view(layout,cutoff,subject_id=1)
        second = after.filter(after['event_id']=='synthetic-status')['text_value'].to_list()
        outcome = {'scope':'Synthetic two-row probe of the current as_of_view helper; no patient data and no population leakage estimate.',
                   'meds_implementation_sha256':hashlib.sha256((ROOT/'src/ehr2trace/meds.py').read_bytes()).hexdigest(),
                   'timeless_dynamic_status_is_visible':bool(first),
                   'changing_retrospective_status_changes_observation':first != second,
                   'future_visit_end_is_visible':any(x and x>cutoff for x in before['end_time'].to_list())}
        (ROOT/'results/world_model_visibility_probe.json').write_text(json.dumps(outcome,indent=2)+'\n')
        print(json.dumps(outcome,indent=2))


if __name__=='__main__':
    main()
