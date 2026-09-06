"""Generate manuscript quantities from the read-only trajectory-readiness audit."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def readiness_macros(doc):
    a,b=doc['datasets']['ctpe'],doc['datasets']['mimiciv']
    integer=lambda n:f'{n:,}'.replace(',', '{,}')
    def assumed(d):
        m=d['saved_validation_metrics']['MEDS_AVAILABILITY_PREVENTS_LEAKAGE']['metrics']
        return f"{100*m['availability_assumed']/m['timed_events']:.1f}"
    return {'ctpeVitalTimeless':integer(a['vital_status']['timeless']),
            'mimicVitalTimeless':integer(b['vital_status']['timeless']),
            'mimicEmarEvents':integer(sum(x['events'] for x in b['by_source_and_kind'] if x['source_id']=='emar')),
            'mimicNonMedicationOrders':integer(sum(n for k,n in b['poe_order_types'].items() if k!='Medications')),
            'mimicPyxisEvents':integer(sum(x['events'] for x in b['by_source_and_kind'] if x['source_id']=='ed_pyxis')),
            'ctpeAssumedAvailabilityPct':assumed(a), 'mimicAssumedAvailabilityPct':assumed(b)}


if __name__=='__main__':
    d=json.loads((ROOT/'results/world_model_readiness.json').read_text())
    (ROOT/'paper/readiness_numbers.tex').write_text('% Generated from results/world_model_readiness.json.\n'+''.join('\\newcommand{\\'+k+'}{'+v+'}\n' for k,v in readiness_macros(d).items()))
