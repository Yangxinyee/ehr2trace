# -*- coding: utf-8 -*-
"""Render the agent's proposals as a sheet a clinician can work through by exception.

The shortlist asked a person to decide 1,427 terms. This asks them to decide far fewer,
because a blinded benchmark on the same retriever measured which of the agent's answers
are worth a human's time and which are not:

    high-confidence picks   97.7% correct   -> spot-check a sample, do not read them all
    medium                  91.3% correct   -> read every one
    low                     33.3% correct   -> read every one
    abstentions             97.9% correct   -> the answer really was not in the candidates

So the sheet contains every medium and low item, every abstention, and a random sample of
the high-confidence picks large enough to catch a regression in that 97.7%. If the sample
comes back worse than the benchmark, the rest of the high-confidence block is not safe to
accept and the sheet says so rather than quietly relying on it.

Nothing here is applied. Exporting writes `decisions.csv` in the format `compile` reads,
and `compile` still only compiles rows a person marked `accept`.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import collections
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SAMPLE_SEED = 20260902


def load(review_dir: Path) -> tuple[list[dict], dict]:
    items = {r["id"]: r for r in json.loads((review_dir / "candidates_k32.json").read_text())}
    picks: dict[str, dict] = {}
    for f in sorted((review_dir / "agent_out").glob("batch_*.json")):
        for r in json.loads(f.read_text()):
            picks[r["id"]] = r
    merged = []
    for tid, item in items.items():
        p = picks.get(tid)
        if p is None:
            continue
        by_id = {c["concept_id"]: c for c in item["candidates"]}
        chosen = by_id.get(p.get("pick"))
        merged.append({**item, "pick": p.get("pick"), "confidence": p.get("confidence", "low"),
                       "why": p.get("why", ""), "chosen": chosen})
    stats = {"terms_total": len(items), "answered": len(merged), "missing": len(items) - len(merged)}
    return merged, stats


def select(rows: list[dict], sample: int) -> tuple[list[dict], dict]:
    """What a person actually has to read, and why the rest is not on the sheet.

    Three things are held back, for three different reasons:

    * abstentions that `triage_abstentions` could place in a list -- provider-order
      categories, wards, specimen tubes. Those are not clinical questions and no
      clinician should be shown a blood tube and asked what it measures. They are fixed
      in the dataset config, and the counts are reported here so the fix is not forgotten;
    * high-confidence picks beyond the sample, on the strength of a measured 97.7%. The
      sample is what keeps that from being an assumption;
    * nothing else. Every medium, every low, and every abstention the triage could not
      place is on the sheet, carrying the agent's own reason.
    """
    from tools.triage_abstentions import classify  # noqa: F401  (kept import-local)

    high_picks, needs_review, excluded = [], [], collections.Counter()
    for r in rows:
        if r["pick"] is None:
            bucket = classify(r.get("source_string", ""), r.get("source_name", ""), r.get("why", ""))
            if bucket != "review":
                excluded[bucket] += 1
                continue
            needs_review.append(r)
        elif r["confidence"] == "high":
            high_picks.append(r)
        else:
            needs_review.append(r)

    rng = random.Random(SAMPLE_SEED)
    sampled = rng.sample(high_picks, min(sample, len(high_picks)))
    for r in sampled:
        r["_bucket"] = "sample"
    for r in needs_review:
        r["_bucket"] = "review"
    out = sorted(needs_review + sampled, key=lambda r: -int(r.get("occurrences") or 0))
    return out, {
        "high_picks": len(high_picks),
        "high_sampled": len(sampled),
        "needs_review": len(needs_review),
        "excluded_by_triage": sum(excluded.values()),
        "excluded_detail": dict(excluded),
        "on_sheet": len(out),
        "rows_on_sheet": sum(int(r.get("occurrences") or 0) for r in out),
        "rows_total": sum(int(r.get("occurrences") or 0) for r in rows),
    }


CSS = """
:root{--bg:#fbfbfa;--card:#fff;--ink:#1a1a19;--muted:#6b6b68;--line:#e4e4e1;
--accent:#2d6a4f;--warn:#9a5b00;--warnbg:#fdf6e9;--pick:#eef5f1;--chip:#f0efec;--bad:#a03030}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1e1e1c;--ink:#eceae6;
--muted:#9a9a95;--line:#33322e;--accent:#74c69d;--warn:#d9a441;--warnbg:#2a2317;
--pick:#20302a;--chip:#26251f;--bad:#e08585}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:12px 20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:650}
main{max-width:1000px;margin:0 auto;padding:20px}
.intro{color:var(--muted);font-size:13.5px;margin:0 0 22px;padding:13px 15px;
background:var(--chip);border-radius:8px;border:1px solid var(--line)}
.intro b{color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:14px}
.card.sample{border-left:3px solid var(--accent)}
.card.low{border-left:3px solid var(--warn)}
.hd{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:4px}
.term{font-size:15.5px;font-weight:640}
.code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 12px}
.chip{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--chip);
color:var(--muted);border:1px solid var(--line)}
.chip.rows{background:var(--warnbg);color:var(--warn);border-color:transparent;font-weight:600}
.chip.c-high{background:var(--pick);color:var(--accent);border-color:transparent;font-weight:600}
.chip.c-medium{background:var(--warnbg);color:var(--warn);border-color:transparent;font-weight:600}
.chip.c-low{color:var(--bad);border-color:var(--bad);font-weight:600}
.why{font-size:13px;color:var(--muted);font-style:italic;margin:0 0 10px}
.opt{display:block;border:1px solid var(--line);border-radius:7px;padding:8px 11px;
margin-bottom:5px;cursor:pointer;font-size:14px}
.opt:hover{border-color:var(--accent)}
.opt.on{background:var(--pick);border-color:var(--accent)}
.opt.agent{box-shadow:inset 2px 0 0 var(--accent)}
.opt input{margin-right:8px}
.opt .vocab{color:var(--muted);font-size:11.5px;margin-left:6px}
.opt .tag{float:right;font-size:10.5px;color:var(--accent);font-weight:600;letter-spacing:.03em}
.more{font-size:12.5px;color:var(--muted);cursor:pointer;user-select:none;margin:2px 0 8px}
.note{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);font:inherit;font-size:13px;margin-top:6px}
button{padding:7px 13px;border:1px solid var(--line);border-radius:7px;background:var(--card);
color:var(--ink);font:inherit;font-size:13.5px;cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
input[type=text]{padding:6px 10px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--ink);font:inherit;font-size:13.5px}
.count{margin-left:auto;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
footer{max-width:1000px;margin:0 auto;padding:0 20px 60px;color:var(--muted);font-size:12.5px}
"""

JS = r"""
const N = __N__;
const st = {};
try { Object.assign(st, JSON.parse(localStorage.getItem("ehr2cdm_agent_review")||"{}")); } catch(e){}
function save(){
  try { localStorage.setItem("ehr2cdm_agent_review", JSON.stringify(st)); } catch(e){}
  const d = Object.values(st).filter(s=>s&&s.choice!==undefined).length;
  document.getElementById("count").textContent = d+" / "+N+" decided";
}
document.querySelectorAll(".opt").forEach(el=>{
  el.addEventListener("click",()=>{
    const id=el.dataset.q, v=el.dataset.v;
    st[id]=Object.assign({},st[id],{choice:v});
    document.querySelectorAll('.opt[data-q="'+CSS.escape(id)+'"]').forEach(o=>{
      const on=o.dataset.v===v; o.classList.toggle("on",on); o.querySelector("input").checked=on;});
    save();
  });
});
document.querySelectorAll(".note").forEach(el=>{
  if(st[el.dataset.q]&&st[el.dataset.q].note) el.value=st[el.dataset.q].note;
  el.addEventListener("input",()=>{st[el.dataset.q]=Object.assign({},st[el.dataset.q],{note:el.value});save();});
});
document.querySelectorAll(".opt").forEach(o=>{const s=st[o.dataset.q];
  if(s&&s.choice===o.dataset.v){o.classList.add("on");o.querySelector("input").checked=true;}});
document.querySelectorAll(".more").forEach(m=>m.addEventListener("click",()=>{
  const box=m.nextElementSibling; const open=box.style.display==="block";
  box.style.display=open?"none":"block"; m.textContent=(open?"▸ show":"▾ hide")+m.dataset.label;}));
save();
document.getElementById("export").addEventListener("click",()=>{
  const who=document.getElementById("who").value.trim();
  const today=new Date().toISOString().slice(0,10);
  const stamp=new Date().toISOString().slice(0,16).replace(/[-:T]/g,"");
  const q=s=>'"'+String(s==null?"":s).replace(/"/g,'""')+'"';
  const lines=["id,decision,concept_id,concept_name,domain_id,vocabulary_id,reviewer,decided_on,note"];
  for(const [id,s] of Object.entries(st)){
    if(!s||s.choice===undefined) continue;
    const el=document.querySelector('.opt[data-q="'+CSS.escape(id)+'"][data-v="'+s.choice+'"]');
    const acc=s.choice!=="none";
    lines.push([id, acc?"accept":"reject", acc?(el?.dataset.cid||""):"", acc?(el?.dataset.cname||""):"",
                el?.dataset.dom||"", acc?(el?.dataset.vocab||""):"", who, today, s.note||""].map(q).join(","));
  }
  const name=["decisions",who.replace(/[^A-Za-z0-9]+/g,"_"),stamp].filter(Boolean).join("_")+".csv";
  const b=new Blob(["﻿"+lines.join("\n")+"\n"],{type:"text/csv;charset=utf-8"});
  const a=document.createElement("a"); a.href=URL.createObjectURL(b); a.download=name; a.click();
  URL.revokeObjectURL(a.href);
});
"""


DOMAIN_FOR_KIND = {"condition": "Condition", "drug_order": "Drug", "drug_admin": "Drug",
                   "procedure": "Procedure", "measurement": "Measurement",
                   "demographic": "Observation", "visit": "Visit"}

SHOWN_FIRST = 6


def card(r: dict) -> str:
    tid = html.escape(str(r["id"]))
    dom = DOMAIN_FOR_KIND.get(r.get("event_kind") or "", "")
    name = html.escape(r.get("source_name") or r.get("source_string") or "")
    code = html.escape(r.get("source_string") or "")
    conf = r["confidence"]
    cands = r["candidates"]
    pick = r["pick"]
    # the agent's pick first, then the rest in retrieval order
    ordered = ([c for c in cands if c["concept_id"] == pick] +
               [c for c in cands if c["concept_id"] != pick]) if pick else list(cands)

    def opt(c, agent):
        return (f'<label class="opt{" agent" if agent else ""}" data-q="{tid}" '
                f'data-v="{c["concept_id"]}" data-cid="{c["concept_id"]}" '
                f'data-cname="{html.escape(c["concept_name"])}" data-dom="{dom}" '
                f'data-vocab="{html.escape(c.get("vocabulary_id") or "")}">'
                f'<input type="radio" name="{tid}">{html.escape(c["concept_name"])}'
                f'<span class="vocab">{html.escape(c.get("vocabulary_id") or "")}</span>'
                + ('<span class="tag">AGENT PICK</span>' if agent else '') + '</label>')

    head = "".join(opt(c, c["concept_id"] == pick) for c in ordered[:SHOWN_FIRST])
    rest = "".join(opt(c, False) for c in ordered[SHOWN_FIRST:])
    none = (f'<label class="opt" data-q="{tid}" data-v="none" data-dom="{dom}">'
            f'<input type="radio" name="{tid}">None of these is correct</label>')
    tail = (f'<div class="more" data-label=" the other {len(ordered)-SHOWN_FIRST} candidates">'
            f'▸ show the other {len(ordered)-SHOWN_FIRST} candidates</div>'
            f'<div style="display:none">{rest}</div>') if rest else ""
    badge = ("agent abstained" if pick is None else f"agent picked, {conf} confidence")
    bucket = ('<span class="chip">random check of the high-confidence block</span>'
              if r.get("_bucket") == "sample" else "")
    return f"""<section class="card {'sample' if r.get('_bucket')=='sample' else conf}">
  <div class="hd"><span class="term">{name}</span><span class="code">{code}</span></div>
  <div class="meta">
    <span class="chip rows">{int(r.get('occurrences') or 0):,} rows</span>
    <span class="chip">{html.escape(r.get('event_kind') or '')}</span>
    <span class="chip c-{conf}">{badge}</span>{bucket}
  </div>
  <p class="why">{html.escape(r.get('why') or '')}</p>
  {head}{tail}{none}
  <input class="note" data-q="{tid}" placeholder="note (optional)">
</section>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample", type=int, default=120,
                    help="how many high-confidence picks to put in front of a person anyway")
    args = ap.parse_args()

    rows, stats = load(args.review_dir)
    if not rows:
        print("no agent output yet"); return 1
    sheet, sel = select(rows, args.sample)
    cards = "\n".join(card(r) for r in sheet)
    pct = 100 * sel["rows_on_sheet"] / max(1, sel["rows_total"])
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminology review by exception</title>
<style>{CSS}</style></head><body>
<header>
  <h1>Terminology review</h1>
  <input type="text" id="who" placeholder="your name">
  <button class="primary" id="export">Export decisions.csv</button>
  <span class="count" id="count"></span>
</header>
<main>
  <p class="intro">
    An agent proposed a concept for each of <b>{stats['answered']:,}</b> source terms and said how
    sure it was. On a blinded benchmark against the same retriever, its high-confidence picks were
    <b>97.7%</b> correct, medium <b>91.3%</b>, low <b>33.3%</b>, and when it declined to pick, the
    answer really was absent from the candidates <b>97.9%</b> of the time.
    <br><br>
    This sheet is therefore <b>not</b> all {stats['answered']:,} terms. <b>{sel['excluded_by_triage']}</b>
    abstentions were held back because they are not clinical questions at all &mdash; provider-order
    categories, ward names and specimen tubes, fixed in the dataset config rather than by a
    reviewer. What remains is every item the agent was unsure of, every abstention that is a real
    question, and a random sample of
    <b>{sel['high_sampled']}</b> of its {sel['high_picks']:,} high-confidence picks — the sample is
    there to check that 97.7% holds on your data. <b>If the sample comes back materially worse,
    the unreviewed high-confidence block is not safe to accept.</b>
    <br><br>
    <b>{sel['on_sheet']:,} items</b> here, covering {sel['rows_on_sheet']:,} patient rows ({pct:.1f}% of
    the queue's rows). Nothing is applied by exporting: <code>compile</code> reads only rows marked
    accept, and a check fails the build if an undecided proposal ever reaches a mapping.
  </p>
{cards}
</main>
<footer>
  Decisions are kept in this browser until you export them. Terms are a hospital's own laboratory
  and drug vocabulary: this file is local and is not published anywhere.
</footer>
<script>{JS.replace("__N__", str(len(sheet)))}</script>
</body></html>"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(json.dumps({**stats, **sel}, indent=1))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
