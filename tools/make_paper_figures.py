"""Generate editable draw.io figures directly from the paper's result records.

Export each .drawio using draw.io Desktop:
  drawio --export --format pdf --crop --border 8 --output figure.pdf figure.drawio
All marks, text, edges, and data coordinates are native mxGraph objects.
"""
from pathlib import Path
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'paper/figures'
INK, MUTED, RULE = '#203747', '#5B6B76', '#D8E0E5'
BLUE, TEAL, ORANGE = '#2368A0', '#168277', '#C86530'


class Diagram:
    def __init__(self, name, width, height):
        self.name = name
        self.doc = ET.Element('mxfile', host='Electron', agent='ehr2cdm figure generator', version='31.4.2')
        page = ET.SubElement(self.doc, 'diagram', id=name, name=name.replace('_', ' ').title())
        model = ET.SubElement(page, 'mxGraphModel', grid='1', gridSize='10', page='0', pageScale='1',
                              pageWidth=str(width), pageHeight=str(height), background='#FFFFFF', math='0', shadow='0')
        self.root = ET.SubElement(model, 'root')
        ET.SubElement(self.root, 'mxCell', id='0')
        ET.SubElement(self.root, 'mxCell', id='1', parent='0')
        self.n = 1
        self.box(0, 0, width, height, '#FFFFFF', 'none')

    def cell(self, style, **attrs):
        self.n += 1
        return ET.SubElement(self.root, 'mxCell', id=str(self.n), parent='1', style=style, **attrs)

    def box(self, x, y, w, h, fill='#FFFFFF', stroke=RULE, rounded=False, shape='rectangle'):
        c = self.cell(f'shape={shape};rounded={int(rounded)};arcSize=8;whiteSpace=wrap;html=0;fillColor={fill};strokeColor={stroke};strokeWidth=1.2;', vertex='1')
        ET.SubElement(c, 'mxGeometry', x=str(x), y=str(y), width=str(w), height=str(h), **{'as': 'geometry'})
        return c

    def text(self, x, y, w, h, text, size=18, bold=False, color=INK, align='left'):
        c = self.cell(f'text;html=0;whiteSpace=wrap;overflow=hidden;align={align};verticalAlign=middle;spacing=0;fontFamily=Arial;fontSize={size};fontStyle={int(bold)};fontColor={color};strokeColor=none;fillColor=none;', value=str(text), vertex='1')
        ET.SubElement(c, 'mxGeometry', x=str(x), y=str(y), width=str(w), height=str(h), **{'as': 'geometry'})

    def line(self, x1, y1, x2, y2, color=RULE, width=1, arrow=False, dashed=False, waypoints=()):
        c = self.cell(f'edgeStyle=none;rounded=0;html=0;strokeColor={color};strokeWidth={width};endArrow={"block" if arrow else "none"};endFill=1;endSize=7;dashed={int(dashed)};dashPattern=5 4;', edge='1')
        geo = ET.SubElement(c, 'mxGeometry', relative='1', **{'as': 'geometry'})
        ET.SubElement(geo, 'mxPoint', x=str(x1), y=str(y1), **{'as': 'sourcePoint'})
        ET.SubElement(geo, 'mxPoint', x=str(x2), y=str(y2), **{'as': 'targetPoint'})
        if waypoints:
            points = ET.SubElement(geo, 'Array', **{'as': 'points'})
            for x, y in waypoints:
                ET.SubElement(points, 'mxPoint', x=str(x), y=str(y))

    def save(self):
        ET.indent(self.doc)
        ET.ElementTree(self.doc).write(OUT / (self.name + '.drawio'), encoding='utf-8', xml_declaration=True)


def architecture():
    d = Diagram('architecture', 900, 510)
    d.box(18, 14, 864, 44, '#F1F5F8', 'none', True)
    d.text(34, 21, 830, 30, 'Dataset configuration   /   fields, identity, time policies, mappings', 19)
    d.line(435, 58, 435, 96, MUTED, 1.3, True, True)
    d.box(18, 98, 218, 142, '#F6F8FA', RULE, True)
    d.text(34, 108, 186, 32, 'Source adapters', 22, True)
    d.text(34, 148, 186, 76, 'CSV · Excel · Parquet\nRaw cell values\nSource row IDs', 18)
    d.box(283, 98, 307, 142, '#EDF5F8', BLUE, True)
    d.text(301, 108, 275, 32, 'Canonical events', 22, True, BLUE)
    d.text(301, 147, 275, 78, 'Identity · event / available time\nCodes · values · quality flags\nMany-to-many source lineage', 18)
    d.line(236, 169, 281, 169, INK, 1.8, True)
    for y, title, body in [(88, 'OMOP CDM 5.4', 'Relational analysis'), (185, 'MEDS', 'Event streams + splits')]:
        d.box(681, y, 201, 70, '#EEF6F2', TEAL, True)
        d.text(696, y+8, 171, 28, title, 21, True, TEAL)
        d.text(696, y+39, 176, 24, body, 17)
    d.line(590, 169, 629, 169, INK, 1.8)
    d.line(629, 123, 629, 220, INK, 1.8)
    d.line(629, 123, 679, 123, INK, 1.8, True)
    d.line(629, 220, 679, 220, INK, 1.8, True)
    d.box(18, 270, 864, 38, '#F6F8FA', 'none', True)
    d.text(32, 275, 836, 28, 'Audit sidecars   /   anchors · cohort membership · quarantine · review decisions', 18)
    d.line(18, 327, 882, 327, TEAL, 1.5)
    count = len(json.loads((ROOT/'results/paper_snapshot.json').read_text())['registered_check_ids'])
    d.text(18, 333, 204, 24, f'{count} artifact checks', 18, True, TEAL)
    d.text(223, 333, 660, 24, 'Reconciliation  /  event semantics  /  target integrity  /  review', 18)
    d.line(882, 220, 862, 383, ORANGE, 1.5, True, True,
           waypoints=((893, 220), (893, 371), (862, 371)))
    future = d.box(18, 386, 864, 109, '#FCF8F3', ORANGE, True)
    future.set('style', future.get('style') + 'dashed=1;dashPattern=6 4;')
    d.text(34, 394, 830, 30, 'Downstream requirements · not implemented', 20, True, ORANGE)
    d.text(34, 430, 830, 27, 'Episodes · as-of histories · logged actions · task-defined rewards', 18)
    d.text(34, 465, 830, 24, 'Patient world models · clinical agents · offline reinforcement learning', 18)
    d.save()


def fault_matrix():
    pre = json.loads((ROOT/'results/faults_fixture_before.json').read_text())
    post = json.loads((ROOT/'results/faults_fixture.json').read_text())
    q = json.loads((ROOT/'results/dqd_baseline.json').read_text())
    total = post['faults_total']
    assert total == pre['faults_total'] == 17
    outside = len(q['faults_never_reaching_the_cdm'])
    values = [('DQD', q['faults_detected'], total-outside-q['faults_detected'], outside),
              (f"Contract · {pre['checks_total']} checks", pre['faults_detected'], total-pre['faults_detected'], 0),
              (f"Contract · {post['checks_total']} checks", post['faults_detected'], total-post['faults_detected'], 0)]
    d = Diagram('fault_matrix', 900, 272)
    d.text(18, 5, 560, 28, 'Detection on the 17-fault catalogue', 21, True)
    for x, label, color in [(477, 'Detected', TEAL), (627, 'Missed', ORANGE), (750, 'Outside scope', '#DCE3E8')]:
        d.box(x, 13, 13, 13, color, 'none')
        d.text(x+21, 5, 134, 28, label, 16)
    unit = 535/total
    for i, (label, found, missed, excluded) in enumerate(values):
        y = 55 + i*48
        d.text(18, y, 255, 28, label, 20)
        start = 282
        for n, color in [(found, TEAL), (missed, ORANGE), (excluded, '#DCE3E8')]:
            if n:
                d.box(start, y, unit*n, 28, color, '#FFFFFF')
                d.text(start, y, unit*n, 28, n, 18, True, INK if color == '#DCE3E8' else '#FFFFFF', 'center')
                start += unit*n
        d.text(830, y, 64, 28, f'{found}/{total-excluded}', 18, True, align='center')
    d.line(18, 204, 882, 204)
    d.text(18, 214, 862, 24, 'Four ablation misses closed by direct artifact checks', 18, True)
    d.text(18, 242, 862, 23, 'Anchor clocks  ·  Canonical fields  ·  Subject identity  ·  Birth-year provenance', 18, color=MUTED)
    d.save()


def leakage():
    data = json.loads((ROOT/'results/leakage_downstream.json').read_text())
    d = Diagram('leakage', 900, 320)
    arms = [('respects_availability', 'Availability filter', BLUE, False, 'ellipse'),
            ('ignores_availability', 'Availability ignored', TEAL, True, 'rhombus'),
            ('diagnoses_at_admission', 'Diagnosis backdating', ORANGE, False, 'rectangle')]
    for left, metric, title, low, high, ticks in [
        (62, 'held_out_auroc', 'a   AUROC', .82, 1., [.85, .90, .95, 1.]),
        (527, 'held_out_auprc', 'b   AUPRC', 0., .8, [0., .2, .4, .6, .8])]:
        width, top, height = 337, 53, 184
        d.text(left-3, 7, 330, 30, title, 22, True)
        def px(t): return left + (t-6)/42*width
        def py(v): return top + (high-v)/(high-low)*height
        for v in ticks:
            y = py(v)
            d.line(left, y, left+width, y, RULE, 1)
            d.text(left-54, y-12, 43, 24, f'{v:.2f}', 17, color=MUTED, align='right')
        d.line(left, top, left, top+height, MUTED)
        d.line(left, top+height, left+width, top+height, MUTED)
        for t in [6, 12, 24, 48]:
            x = px(t)
            d.line(x, top+height, x, top+height+5, MUTED)
            d.text(x-20, top+height+9, 40, 24, t, 17, color=MUTED, align='center')
        d.text(left, 270, width, 22, 'Hours after admission', 17, color=MUTED, align='center')
        # Draw the dashed near-overlapping arm first; use different marker shapes.
        for arm, _, color, dashed, shape in [arms[1], arms[0], arms[2]]:
            points = [(px(h['horizon_hours']), py(next(a[metric] for a in h['arms'] if a['arm']==arm))) for h in data['horizons']]
            for (x1,y1),(x2,y2) in zip(points,points[1:]):
                d.line(x1,y1,x2,y2,color,2.3,False,dashed)
            for x,y in points:
                d.box(x-4, y-4, 8, 8, '#FFFFFF' if dashed else color, color, shape=shape)
    for x, (_, label, color, dashed, shape) in zip([64, 337, 634], arms):
        d.line(x, 308, x+34, 308, color, 2.3, False, dashed)
        d.box(x+13, 304, 8, 8, '#FFFFFF' if dashed else color, color, shape=shape)
        d.text(x+44, 296, 224, 24, label, 17)
    d.save()


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    architecture(); fault_matrix(); leakage()
    print('Wrote three native, editable draw.io diagrams.')
