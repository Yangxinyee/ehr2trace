"""Render revision tables and macros from the frozen aggregate snapshot."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def integer(n):
    return f'{n:,}'.replace(',', '{,}')


def snapshot_macros(doc):
    a,b=doc['datasets']['ctpe'],doc['datasets']['mimiciv']
    d=doc['drug_match']
    return {
        'currentCheckCount':str(len(doc['registered_check_ids'])),
        'totalCanonicalMillions':f"{(a['canonical']['events'] + b['canonical']['events']) / 1_000_000:.1f}",
        **{prefix+'Validation'+key.title(): str(dataset['validation_recorded'][key])
           for prefix,dataset in [('ctpe',a),('mimic',b)] for key in ('total','passed','skipped','failed')},
        **({'mimicInvertedIntervals': integer(b['validation_recorded']['interval_ordering']['end_before_start']),
            'mimicUnflaggedIntervals': integer(b['validation_recorded']['interval_ordering']['unflagged'])}
           if b['validation_recorded'].get('interval_ordering') else {}),
        'ctpeRows':integer(a['source_rows']), 'ctpeEvents':integer(a['canonical']['events']),
        'ctpeLinks':integer(a['canonical']['event_source']), 'ctpeSubjects':integer(a['identity_subjects']),
        'ctpeAnchors':integer(a['canonical']['anchors']), 'ctpeQuarantine':integer(a['canonical']['quarantine']),
        'mimicSources':'21', 'mimicPrepRows':integer(b['source_rows']),
        'mimicSubjects':integer(b['canonical_subjects']), 'mimicPersons':integer(b['omop_persons']),
        'mimicQuarantine':integer(b['canonical']['quarantine']),
        'drugGold':integer(d['confirmed_mappings']), 'drugResolved':integer(d['resolved']),
        'drugExact':integer(d['outcome']['exact_agreement']),
        'drugAgreement':f"{100*d['outcome']['exact_agreement']/d['resolved']:.1f}",
    }


def tables(doc):
    a,b=doc['datasets']['ctpe'],doc['datasets']['mimiciv']
    rows=[
        ('Source rows',a['source_rows'],b['source_rows']),
        ('Identities encountered',a['identity_subjects'],b['identity_subjects']),
        ('Subjects with canonical events',a['canonical_subjects'],b['canonical_subjects']),
        ('Canonical events',a['canonical']['events'],b['canonical']['events']),
        ('Event-to-source links',a['canonical']['event_source'],b['canonical']['event_source']),
        ('Quarantine entries',a['canonical']['quarantine'],b['canonical']['quarantine']),
        ('OMOP persons',a['omop_persons'],b['omop_persons']),
    ]
    data=[r'\begin{tabularx}{\linewidth}{@{}Yrr@{}}',r'\toprule',r'Measure & CTPE & MIMIC-IV \\',r'\midrule']
    data += [f'{k} & {integer(x)} & {integer(y)} \\\\' for k,x,y in rows]
    data += [r'\midrule', 'Validation (pass / skip / fail) & ' + ' & '.join(
        ' / '.join(str(d['validation_recorded'][key]) for key in ('passed','skipped','failed'))
        for d in (a,b)) + r' \\']
    data += [r'\bottomrule',r'\end{tabularx}']
    cov=[r'\begin{tabularx}{\linewidth}{@{}Yrrrr@{}}',r'\toprule',r'& \multicolumn{2}{c}{CTPE} & \multicolumn{2}{c}{MIMIC-IV} \\',r'\cmidrule(lr){2-3}\cmidrule(lr){4-5}',r'Domain & Rows & Mapped & Rows & Mapped \\',r'\midrule']
    for k,label in [('condition_occurrence','Condition'),('drug_exposure','Drug'),('measurement','Measurement'),('procedure_occurrence','Procedure'),('observation','Observation')]:
        parts=[]
        for d in [a,b]:
            v=d['omop_tables'].get(k,{'rows':0,'mapped_pct':None})
            parts += [integer(v['rows']), f"{v['mapped_pct']:.1f}\\%" if v['mapped_pct'] is not None else r'\textemdash']
        cov += [' & '.join([label]+parts)+r' \\']
    cov += [r'\bottomrule',r'\end{tabularx}']
    validation=[r'\begin{tabularx}{\linewidth}{@{}Yrrrrl@{}}', r'\toprule',
                r'Dataset & Checks & Pass & Skip & Fail & Report date (UTC) \\', r'\midrule']
    for label,dataset in [('CTPE',a),('MIMIC-IV',b)]:
        v=dataset['validation_recorded']
        validation.append(' & '.join([label]+[str(v[k]) for k in ('total','passed','skipped','failed')]
                                    +[dataset['report_modified_utc']['validation'][:10]])+r' \\')
    validation += [r'\bottomrule',r'\end{tabularx}']
    return {'dataset_table.tex':'\n'.join(data)+'\n','coverage_table.tex':'\n'.join(cov)+'\n',
            'validation_table.tex':'\n'.join(validation)+'\n',
            'snapshot_numbers.tex':'% Generated from results/paper_snapshot.json.\n'+''.join('\\newcommand{\\'+k+'}{'+v+'}\n' for k,v in snapshot_macros(doc).items())}


if __name__=='__main__':
    doc=json.loads((ROOT/'results/paper_snapshot.json').read_text())
    for name,content in tables(doc).items():
        (ROOT/'paper'/name).write_text(content)
    print('Wrote dataset table, coverage table and snapshot macros.')
