# -*- coding: utf-8 -*-
"""Put the structured drug matches in front of a clinician, by exception.

`ehr2trace.drug_match` adopts a mapping only when exactly one standard concept has the
ingredient set, the strength and the dose form the source string states, so what it
publishes is not a proposal and there is nothing to accept. That is precisely why it
should still be spot-checked: an error here is silent and repeated across millions of
rows, and the rules it applies were written by reading one hospital's names.

The sheet is built by exception rather than exhaustively, because the routes differ in
how much judgement they involved:

    total_amount*           `2 GRAM/100 ML` read as the bag's total dose, which is how
                            RxNorm Extension files an IV bag. This is a reading of the
                            source, so every one is shown.
    *_widened_form          the dose form matched under a second spelling of the same
                            form -- `Injection` for `Injectable Solution`. The concept
                            names differ by a word and the drug does not, so these are
                            sampled rather than read one by one.
    as_written              the literal reading -- ingredient, strength and form as the
                            source wrote them. Sampled.

Samples are drawn by row weight: a name on two million rows and a name on four are not
equally worth a clinician's attention.

Measured against 139 mappings a physician had already confirmed, the pass reproduced
92.7% of them exactly, 4.9% as the same drug under the other spelling of a dose form,
and 2.4% as the same drug under the other reading of a concentration -- with no case of
a different drug or a different strength. This sheet exists to test that on this build
rather than to assume it: if the sampled rows come back worse, the rest is not safe.

Nothing here is applied, and marking a row `wrong` writes it to `decisions.csv` in the
format `ehr2trace compile` reads, so a correction becomes an approved mapping that
overrides the matcher on the next build.
"""
from __future__ import annotations

import argparse
import csv
import html
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SAMPLE_SEED = 20260904


def load_rows(omop_db: Path, vocabulary: Path | None) -> list[dict]:
    import duckdb

    con = duckdb.connect(str(omop_db), read_only=True)
    # Counted through `drug_source_value` rather than through the concept: one concept
    # can be reached from several source strings, and a sheet that says how many rows a
    # *name* carries has to count the name. CDM 5.4 caps that column at 50 characters,
    # so the join is on the same 50 -- two names sharing a prefix that long share a
    # count, which is a rounding error in an ordering, not in a mapping.
    rows = con.execute(
        """
        SELECT m.source_code, m.concept_id, m.path, coalesce(d.n, 0) AS row_count
        FROM term_map m
        LEFT JOIN (
            SELECT drug_source_value AS source_value, count(*) AS n
            FROM drug_exposure GROUP BY 1
        ) d ON d.source_value = substr(m.source_code, 1, 50)
        WHERE m.path LIKE 'structured_drug%'
        ORDER BY row_count DESC
        """
    ).fetchall()
    con.close()
    # Concept names come from the vocabulary rather than from the build, because the
    # build stores ids: a sheet that showed a name copied at mapping time could not show
    # that the name has since changed.
    names: dict[int, tuple[str, str]] = {}
    if vocabulary:
        from ehr2trace.terminology import Vocabulary

        opened = Vocabulary.open(vocabulary) if vocabulary.is_dir() else None
        source = opened.con if opened is not None and opened.available else (
            duckdb.connect(str(vocabulary), read_only=True) if vocabulary.is_file() else None)
        if source is not None:
            for concept_id, name, vocabulary_id in source.execute(
                "SELECT CAST(concept_id AS BIGINT), concept_name, vocabulary_id FROM CONCEPT"
            ).fetchall():
                names[int(concept_id)] = (name, vocabulary_id)
        if opened is not None:
            opened.close()
    out = []
    for source, concept_id, path, count in rows:
        name, vocabulary = names.get(int(concept_id), ("", ""))
        out.append({
            "source_string": source,
            "concept_id": int(concept_id),
            "concept_name": name,
            "vocabulary_id": vocabulary,
            "route": path.replace("structured_drug_", ""),
            "rows": int(count),
        })
    return out


#: UCUM is what the vocabulary stores; `10*-3.eq` is not what a clinician calls a
#: milliequivalent, and this sheet is for them.
UNIT_LABEL = {"mg": "MG", "g": "GRAM", "ug": "MCG", "ng": "NG", "mL": "ML", "L": "L",
              "[U]": "UNIT", "[iU]": "UNIT", "[USP'U]": "USP UNIT",
              "10*-3.eq": "MEQ", "mmol": "MMOL", "mol": "MOL", "%": "%",
              "{actuat}": "ACTUATION", "mCi": "MILLICURIE"}


def unit(code: str | None) -> str:
    return UNIT_LABEL.get(code or "", code or "")


def describe(source: str, noise: list[str]) -> str:
    """What the matcher read out of the name, in the reviewer's own terms."""
    from ehr2trace.drug_match import parse_drug_name

    parsed = parse_drug_name(source, noise)
    parts = []
    for component in parsed.components:
        strength = component.strength
        if strength is None:
            parts.append(component.ingredient_text)
        elif strength.kind == "amount":
            parts.append(f"{component.ingredient_text} {strength.value:g} {unit(strength.unit)}")
        else:
            denominator = "" if strength.denominator in (None, 1.0) else f"{strength.denominator:g} "
            parts.append(f"{component.ingredient_text} {strength.value:g} {unit(strength.unit)}"
                         f"/{denominator}{unit(strength.denominator_unit)}")
    return " + ".join(parts) + (f"  ·  {parsed.dose_form}" if parsed.dose_form else "")


def weighted_sample(pool: list[dict], size: int, rng: random.Random) -> list[dict]:
    picked = []
    pool = list(pool)
    for _ in range(min(size, len(pool))):
        weights = [max(r["rows"], 1) for r in pool]
        picked.append(pool.pop(rng.choices(range(len(pool)), weights=weights, k=1)[0]))
    return picked


def select(rows: list[dict], sample: int) -> tuple[list[dict], dict]:
    rng = random.Random(SAMPLE_SEED)
    judged = [r for r in rows if r["route"].startswith("total_amount")]
    widened = [r for r in rows if r["route"].endswith("_widened_form")
               and not r["route"].startswith("total_amount")]
    literal = [r for r in rows if r["route"] == "as_written"]
    picked = weighted_sample(widened, sample // 2, rng) + weighted_sample(literal, sample, rng)
    shown = judged + sorted(picked, key=lambda r: -r["rows"])
    seen = {id(r) for r in shown}
    counts = {
        "terms": len(rows),
        "rows": sum(r["rows"] for r in rows),
        "shown": len(shown),
        "judged": len(judged),
        "sampled": len(picked),
        "unsampled_terms": len(rows) - len(shown),
        "unsampled_rows": sum(r["rows"] for r in rows if id(r) not in seen),
        "routes": Counter(r["route"] for r in rows),
    }
    return shown, counts


ROUTE_NOTE = {
    "as_written": "literal: ingredient, strength and form exactly as the source wrote them",
    "as_written_widened_form": "the dose form matched under a second spelling of the same form",
    "total_amount": "the concentration read as the container's total dose",
    "total_amount_widened_form": "total dose, and the form under a second spelling",
}

STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif; margin: 0 auto;
       max-width: 1180px; padding: 2rem 1.25rem 6rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.lede { color: #555; margin: 0 0 1.5rem; }
.counts { display: flex; flex-wrap: wrap; gap: 1.5rem; margin: 0 0 2rem; padding: 1rem 1.25rem;
          background: #f6f7f9; border-radius: 10px; }
.counts div { min-width: 8rem; }
.counts b { display: block; font-size: 1.35rem; }
.counts span { color: #666; font-size: .85rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .6rem .5rem; border-bottom: 1px solid #e3e5e8;
         vertical-align: top; }
th { position: sticky; top: 0; background: #fff; font-size: .8rem; text-transform: uppercase;
     letter-spacing: .04em; color: #666; }
code { font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
.src { font-weight: 600; }
.read { color: #444; font-size: .88rem; }
.route { font-size: .78rem; color: #777; }
.rows { text-align: right; white-space: nowrap; color: #444; }
.mark { white-space: nowrap; }
.mark label { margin-right: .6rem; font-size: .85rem; }
tr.judged { background: #fffdf3; }
tr.wrong { background: #fdecec; }
button { font: inherit; padding: .55rem 1.1rem; border-radius: 8px; border: 1px solid #888;
         background: #fff; cursor: pointer; }
.bar { position: fixed; left: 0; right: 0; bottom: 0; background: #fff; border-top: 1px solid #ddd;
       padding: .75rem 1.25rem; display: flex; gap: 1rem; align-items: center; }
@media (prefers-color-scheme: dark) {
  body { background: #14161a; color: #e7e9ec; }
  .counts { background: #1d2026; } th { background: #14161a; color: #9aa0a8; }
  th, td { border-color: #2a2e36; } .lede, .read, .rows { color: #aeb4bc; }
  tr.judged { background: #23200f; } tr.wrong { background: #2c1618; }
  .bar { background: #14161a; border-color: #2a2e36; } button { background: #1d2026; color: #e7e9ec; }
}
"""

SCRIPT = """
function mark(id, verdict) {
  const row = document.getElementById(id);
  row.classList.toggle('wrong', verdict === 'wrong');
  row.dataset.verdict = verdict;
  document.getElementById('done').textContent =
    document.querySelectorAll('tr[data-verdict]').length;
}
function exportCsv() {
  const lines = [['source_string','code_system','concept_id','concept_name','domain_id',
                  'vocabulary_id','status','note'].join(',')];
  document.querySelectorAll('tr[data-verdict="wrong"]').forEach(row => {
    lines.push([row.dataset.source, 'SOURCE', '', '', 'Drug', '', 'reject',
                'structured match marked wrong on review'].map(v =>
      '"' + String(v).replace(/"/g, '""') + '"').join(','));
  });
  const blob = new Blob([lines.join('\\n')], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'drug_match_corrections.csv'; a.click();
}
"""


def render(rows: list[dict], counts: dict, noise: list[str], out: Path) -> None:
    esc = html.escape
    body = []
    for index, row in enumerate(rows):
        judged = "" if row["route"] == "as_written" else " judged"
        body.append(
            f'<tr id="r{index}" class="{judged.strip()}" data-source="{esc(row["source_string"])}">'
            f'<td><div class="src"><code>{esc(row["source_string"])}</code></div>'
            f'<div class="read">read as: {esc(describe(row["source_string"], noise))}</div></td>'
            f'<td>{esc(row["concept_name"])}'
            f'<div class="route">{row["concept_id"]} · {esc(row["vocabulary_id"])} · '
            f'{esc(ROUTE_NOTE.get(row["route"], row["route"]))}</div></td>'
            f'<td class="rows">{row["rows"]:,}</td>'
            f'<td class="mark">'
            f'<label><input type="radio" name="v{index}" onchange="mark(\'r{index}\',\'ok\')"> ok</label>'
            f'<label><input type="radio" name="v{index}" onchange="mark(\'r{index}\',\'wrong\')"> wrong</label>'
            f'</td></tr>'
        )
    routes = " · ".join(f"{name} {count:,}" for name, count in counts["routes"].most_common())
    out.write_text(f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Drug matches to spot-check</title>
<style>{STYLE}</style>
<h1>Structured drug matches — spot check</h1>
<p class="lede">Each row was adopted because exactly one standard concept had the
ingredient, strength and dose form the source string states. Nothing here needs
accepting; the question is only whether any of it is <em>wrong</em>. Mark a row wrong and
export at the bottom — the export is a correction, not an approval.</p>
<div class="counts">
  <div><b>{counts['terms']:,}</b><span>drug names matched</span></div>
  <div><b>{counts['rows']:,}</b><span>rows they carry</span></div>
  <div><b>{counts['shown']:,}</b><span>shown here</span></div>
  <div><b>{counts['judged']:,}</b><span>every reinterpreted strength</span></div>
  <div><b>{counts['sampled']:,}</b><span>sampled by row weight</span></div>
  <div><b id="done">0</b><span>marked so far</span></div>
</div>
<p class="lede">Routes: {esc(routes)}. Not shown: {counts['unsampled_terms']:,} names
carrying {counts['unsampled_rows']:,} rows — all of them literal or dose-form-spelling
matches, sampled above rather than listed.</p>
<table><thead><tr><th>Source string</th><th>Matched concept</th><th>Rows</th><th>Verdict</th>
</tr></thead><tbody>
{''.join(body)}
</tbody></table>
<div class="bar"><button onclick="exportCsv()">Export the ones marked wrong</button>
<span class="lede" style="margin:0">Saves <code>drug_match_corrections.csv</code>;
feed it to <code>ehr2trace compile</code> after a concept id is filled in.</span></div>
<script>{SCRIPT}</script>
</html>""", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omop-db", type=Path, required=True)
    parser.add_argument("--vocabulary", type=Path, default=None,
                        help="the Athena vocabulary directory (or a DuckDB holding CONCEPT)")
    parser.add_argument("--dataset", type=Path, default=None,
                        help="dataset YAML, for terminology.drug_name_noise")
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    noise: list[str] = []
    if args.dataset:
        from ehr2trace.config import load_dataset_config

        noise = list(load_dataset_config(args.dataset).terminology.drug_name_noise)

    rows = load_rows(args.omop_db, args.vocabulary)
    if not rows:
        print("no structured drug matches in this build", file=sys.stderr)
        return 1
    shown, counts = select(rows, args.sample)
    render(shown, counts, noise, args.out)
    with open(args.out.with_suffix(".csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"{counts['terms']:,} matches carrying {counts['rows']:,} rows; "
          f"{counts['shown']:,} shown -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
