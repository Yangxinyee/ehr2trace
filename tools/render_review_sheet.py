"""Render the review shortlist as a page a clinician can work through.

The shortlist is a twenty-five column CSV because it has to round-trip into
`decisions.csv`, and that is a bad thing to ask a person to read: the five candidates a
decision is actually made between are spread across fifteen columns. This renders the
same rows as one card per term -- the candidates as radio buttons, the counts and flags
where they can be seen -- and exports a `decisions.csv` in exactly the format `compile`
expects.

The page is a local file. It is deliberately not published anywhere: the terms are a
hospital's own laboratory and drug vocabulary, which is not patient data but is not ours
to distribute either.

Nothing here decides anything. A term is undecided until someone picks `accept`, and the
export writes only what was picked; `compile` ignores the rest and
UNDECIDED_PROPOSALS_NEVER_PUBLISHED fails the build if that is ever bypassed.

Usage::

    python tools/render_review_sheet.py --shortlist $WORK/ctpe/review/shortlist.csv \\
        --out $WORK/ctpe/review/shortlist.html
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path

SHOWN = 8

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminology review &mdash; __DATASET__</title>
<style>
:root {
  --bg:#fbfbfa; --card:#fff; --ink:#1a1a19; --muted:#6b6b68; --line:#e4e4e1;
  --accent:#2d6a4f; --warn:#9a5b00; --warnbg:#fdf6e9; --pick:#eef5f1;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#161614; --card:#1e1e1c; --ink:#eceae6; --muted:#9a9a95; --line:#33322e;
          --accent:#74c69d; --warn:#d9a441; --warnbg:#2a2317; --pick:#20302a; }
}
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif }
header { position:sticky; top:0; z-index:5; background:var(--bg);
  border-bottom:1px solid var(--line); padding:14px 20px }
h1 { margin:0 0 4px; font-size:17px; font-weight:600 }
.sub { color:var(--muted); font-size:13px }
.bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px }
button, select { font:inherit; padding:5px 11px; border:1px solid var(--line);
  border-radius:6px; background:var(--card); color:var(--ink); cursor:pointer }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff }
main { padding:16px 20px 60px; max-width:1000px; margin:0 auto }
.card { background:var(--card); border:1px solid var(--line); border-radius:9px;
  padding:14px 16px; margin-bottom:10px }
.card.decided { border-color:var(--accent) }
.card.flagged { background:var(--warnbg) }
.head { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap }
.term { font-weight:600; font-family:ui-monospace,SFMono-Regular,Menlo,monospace }
.expn { color:var(--muted) }
.count { margin-left:auto; color:var(--muted); font-variant-numeric:tabular-nums;
  white-space:nowrap }
.flag { color:var(--warn); font-size:12px; border:1px solid currentColor;
  border-radius:4px; padding:1px 6px }
.why { color:var(--muted); font-size:13px; margin:6px 0 0 }
.opts { margin:10px 0 0; display:grid; gap:3px }
label.opt { display:flex; gap:9px; align-items:baseline; padding:5px 8px;
  border-radius:6px; cursor:pointer }
label.opt:hover { background:var(--pick) }
label.opt input { margin:0 }
.cname { flex:1 }
.cmeta { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums }
.acts { display:flex; gap:6px; margin-top:10px; align-items:center }
.status { font-size:12px; color:var(--muted); margin-left:auto }
.none { color:var(--muted); font-style:italic; padding:5px 8px }
</style></head><body>
<header>
  <h1>Terminology review &mdash; __DATASET__</h1>
  <div class="sub" id="sub"></div>
  <div class="bar">
    <select id="filter">
      <option value="all">All terms</option>
      <option value="undecided">Undecided only</option>
      <option value="flagged">Flagged: check carefully</option>
      <option value="measurement">Laboratory only</option>
    </select>
    <input id="who" placeholder="your name (goes in decisions.csv)"
           style="font:inherit;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)">
    <button class="primary" id="export">Export decisions.csv</button>
    <button id="clear">Clear my decisions</button>
  </div>
</header>
<main id="list"></main>
<script>
const ROWS = __ROWS__;
const KEY = "review:" + __DATASETJSON__;
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} };
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));
const nf = n => Number(n || 0).toLocaleString();

function summary() {
  const done = ROWS.filter(r => (state[r.id] || {}).decision === "accept");
  const rows = done.reduce((a, r) => a + Number(r.occurrences || 0), 0);
  const total = ROWS.reduce((a, r) => a + Number(r.occurrences || 0), 0);
  document.getElementById("sub").textContent =
    `${done.length} of ${ROWS.length} accepted · ${nf(rows)} of ${nf(total)} rows decided`
    + ` (${total ? (100 * rows / total).toFixed(1) : 0}%)`;
}

function render() {
  const mode = document.getElementById("filter").value;
  const list = document.getElementById("list");
  list.innerHTML = "";
  for (const r of ROWS) {
    const st = state[r.id] || {};
    if (mode === "undecided" && st.decision) continue;
    if (mode === "flagged" && r.flag !== "CHECK_CAREFULLY") continue;
    if (mode === "measurement" && r.event_kind !== "measurement") continue;

    const card = document.createElement("div");
    card.className = "card" + (st.decision === "accept" ? " decided" : "")
      + (r.flag === "CHECK_CAREFULLY" ? " flagged" : "");
    let h = `<div class="head"><span class="term">${esc(r.source_string)}</span>`;
    if (r.source_name && r.source_name !== r.source_string)
      h += `<span class="expn">${esc(r.source_name)}</span>`;
    if (r.expansion) h += `<span class="expn">&rarr; ${esc(r.expansion)}</span>`;
    if (r.flag === "CHECK_CAREFULLY") h += `<span class="flag">check carefully</span>`;
    h += `<span class="count">${nf(r.occurrences)} rows &middot; ${esc(r.event_kind)}</span></div>`;
    if (r.model_rationale) h += `<p class="why">${esc(r.model_rationale)}</p>`;

    h += `<div class="opts">`;
    if (!r.candidates.length) {
      h += `<div class="none">No candidates retrieved &mdash; this one needs a manual search.</div>`;
    }
    r.candidates.forEach((c, i) => {
      const on = st.concept_id ? String(st.concept_id) === String(c.id) : false;
      h += `<label class="opt"><input type="radio" name="c_${esc(r.id)}" value="${esc(c.id)}"`
        + `${on ? " checked" : ""}><span class="cname">${esc(c.name)}</span>`
        + `<span class="cmeta">${esc(c.vocab)} &middot; ${esc(c.id)} &middot; ${esc(c.score)}</span></label>`;
    });
    h += `</div><div class="acts">`
      + `<button data-act="accept">Accept selected</button>`
      + `<button data-act="reject">Reject &mdash; none of these</button>`
      + `<button data-act="clear">Undo</button>`
      + `<span class="status">${st.decision ? esc(st.decision) + (st.concept_name ? ": " + esc(st.concept_name) : "") : "undecided"}</span></div>`;
    card.innerHTML = h;

    card.querySelectorAll("input[type=radio]").forEach(input => {
      input.addEventListener("change", () => {
        const c = r.candidates.find(x => String(x.id) === input.value);
        state[r.id] = Object.assign({}, state[r.id], {
          concept_id: c.id, concept_name: c.name, vocabulary_id: c.vocab, domain_id: r.domain_id,
        });
        save();
      });
    });
    card.querySelectorAll("button[data-act]").forEach(b => {
      b.addEventListener("click", () => {
        const act = b.dataset.act;
        if (act === "clear") delete state[r.id];
        else if (act === "reject") state[r.id] = { decision: "reject" };
        else {
          const cur = state[r.id] || {};
          const c = cur.concept_id ? cur : r.candidates[0];
          if (!c) return;
          state[r.id] = {
            decision: "accept",
            concept_id: c.concept_id || c.id,
            concept_name: c.concept_name || c.name,
            vocabulary_id: c.vocabulary_id || c.vocab,
            domain_id: r.domain_id,
          };
        }
        save(); render();
      });
    });
    list.appendChild(card);
  }
  summary();
}

document.getElementById("filter").addEventListener("change", render);
document.getElementById("clear").addEventListener("click", () => {
  if (confirm("Discard every decision recorded in this browser?")) { state = {}; save(); render(); }
});
document.getElementById("export").addEventListener("click", () => {
  const who = document.getElementById("who").value.trim();
  // Local clock, not toISOString(): that reports UTC, which is already the next day
  // for anyone reviewing in the evening east of Greenwich.
  const now = new Date();
  const pad = n => String(n).padStart(2, "0");
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const stamp = `${today}_${pad(now.getHours())}${pad(now.getMinutes())}`;
  const cols = ["id","decision","concept_id","concept_name","domain_id","vocabulary_id",
                "reviewer","decided_on","note"];
  const q = v => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
  const lines = [cols.join(",")];
  for (const r of ROWS) {
    const st = state[r.id];
    if (!st || !st.decision) continue;
    lines.push([r.id, st.decision, st.decision === "accept" ? (st.concept_id || "") : "",
      st.decision === "accept" ? (st.concept_name || "") : "", st.domain_id || "",
      st.decision === "accept" ? (st.vocabulary_id || "") : "", who, today, ""].map(q).join(","));
  }
  // Named so that two reviewers, or one reviewer on two days, do not silently
  // overwrite each other in a downloads folder. `compile` reads review/decisions.csv,
  // so this gets renamed when it is put back.
  const safe = who.replace(/[\\\\/:*?"<>|\\s]+/g, "_").replace(/^_+|_+$/g, "");
  const name = ["decisions", safe, stamp].filter(Boolean).join("_") + ".csv";
  const blob = new Blob(["\\ufeff" + lines.join("\\n") + "\\n"],
                        { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
});
render();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shortlist", type=Path, required=True)
    ap.add_argument("--dataset", default="", help="label for the page title")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    with open(args.shortlist, newline="", encoding="utf-8") as fh:
        raw = list(csv.DictReader(fh))

    rows = []
    for r in raw:
        candidates = []
        for i in range(1, SHOWN + 1):
            cid = (r.get(f"cand{i}_id") or "").strip()
            if not cid:
                continue
            candidates.append({
                "id": cid,
                "name": r.get(f"cand{i}_name") or "",
                "vocab": r.get(f"cand{i}_vocab") or "",
                "score": r.get(f"cand{i}_score") or "",
            })
        rows.append({
            "id": r["id"],
            "source_string": r.get("source_string") or "",
            "source_name": r.get("source_name") or "",
            "expansion": r.get("expansion") or "",
            "event_kind": r.get("event_kind") or "",
            "domain_id": r.get("domain_id") or "",
            "occurrences": int(r.get("occurrences") or 0),
            "flag": r.get("flag") or "",
            "model_rationale": r.get("model_rationale") or "",
            "candidates": candidates,
        })
    rows.sort(key=lambda r: -r["occurrences"])

    label = args.dataset or args.shortlist.parent.parent.name
    page = (PAGE
            .replace("__ROWS__", json.dumps(rows, ensure_ascii=False))
            .replace("__DATASETJSON__", json.dumps(label))
            .replace("__DATASET__", html.escape(label)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    covered = sum(r["occurrences"] for r in rows)
    print(f"{len(rows)} terms, {covered:,} rows -> {args.out}")


if __name__ == "__main__":
    main()
