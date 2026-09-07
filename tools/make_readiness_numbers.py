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
            'ctpeVitalTimelessDeceased':integer(a['vital_status']['timeless_deceased']),
            'mimicVitalTimelessDeceased':integer(b['vital_status']['timeless_deceased']),
            'mimicVitalTimeless':integer(b['vital_status']['timeless']),
            'mimicEmarEvents':integer(sum(x['events'] for x in b['by_source_and_kind'] if x['source_id']=='emar')),
            'mimicNonMedicationOrders':integer(sum(n for k,n in b['poe_order_types'].items() if k!='Medications')),
            'mimicPyxisEvents':integer(sum(x['events'] for x in b['by_source_and_kind'] if x['source_id']=='ed_pyxis')),
            'mimicEmarDose':integer(sum(x['with_dose_text'] for x in b['by_source_and_kind'] if x['source_id']=='emar')),
            'mimicEmarRoute':integer(sum(x['with_route_text'] for x in b['by_source_and_kind'] if x['source_id']=='emar')),
            'mimicDispenses':integer(sum(x['events'] for x in b['by_source_and_kind'] if x['event_kind']=='drug_dispense')),
            'ctpeAssumedAvailabilityPct':assumed(a), 'mimicAssumedAvailabilityPct':assumed(b)}


if __name__=='__main__':
    d=json.loads((ROOT/'results/world_model_readiness.json').read_text())
    lines=['% Generated from results/world_model_readiness.json.\n']
    lines+= ['\\newcommand{\\'+k+'}{'+v+'}\n' for k,v in readiness_macros(d).items()]
    # The gaps the rebuild closed are no longer measurable from the current audit, so
    # the audit that measured them is kept and read here. A number the paper states as
    # history still has to come from a record rather than from memory.
    prev=json.loads((ROOT/'results/world_model_readiness_before_rebuild.json').read_text())
    lines.append('\\newcommand{\\prevMimicUnflaggedIntervals}{'
                 + f"{prev['datasets']['mimiciv']['saved_validation_metrics']['END_TIME_NEVER_PRECEDES_START']['metrics']['unflagged']:,}".replace(',', '{,}')
                 + '}\n')
    before=ROOT/'results/world_model_readiness_before_rebuild.json'
    if before.exists():
        lines.append('% Pre-rebuild values, from results/world_model_readiness_before_rebuild.json.\n')
        lines+= ['\\newcommand{\\prev'+k[0].upper()+k[1:]+'}{'+v+'}\n'
                 for k,v in readiness_macros(json.loads(before.read_text())).items()]
    (ROOT/'paper/readiness_numbers.tex').write_text(''.join(lines))
