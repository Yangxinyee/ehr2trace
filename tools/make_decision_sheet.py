# -*- coding: utf-8 -*-
"""Render the modelling decisions a retriever cannot answer, as a page someone can sign.

The term shortlist asks "what does this string mean". These are the other kind of
question: ones where the string is already understood and what is undecided is what the
converter should *do* with it. One answer here moves more rows than a hundred terms
there, and none of them can be settled by retrieval, a vocabulary, or a model -- which
is exactly why they are still open.
"""
import argparse
import html
import json
from pathlib import Path

DECISIONS = [
  dict(
    id="DOMAIN_GATE_NON_CONDITION",
    title="ICD-10 codes whose standard concept is not a condition",
    kind="clinical + modelling",
    at_stake="5,737 terms · 1,944,952 rows (MIMIC 3,274 / 1,079,226 · CTPE 2,463 / 865,726)",
    evidence=[
      "A diagnosis column produces events of kind <code>condition</code>, and the converter writes those to <code>CONDITION_OCCURRENCE</code> only.",
      "Many ICD codes map to a standard concept whose domain is <b>not</b> Condition: Observation 4,799 terms (mostly Z codes &mdash; family history, screening encounters, socioeconomic factors), Measurement 526, Procedure 350, Drug 61.",
      "The converter refuses to force them into the wrong table, so every one lands in the review queue instead. None of them is published today.",
      "The OMOP CDM has an <code>OBSERVATION</code> table for exactly this. This converter does not write one.",
    ],
    options=[
      ("route_by_domain", "Add OBSERVATION and route by the standard concept's domain",
       "The OHDSI convention: the concept's domain decides the table, not the source column it came from. Recovers all 1.23M rows. Largest change &mdash; a new table, new lineage, new checks, and the MEDS layer gains a new event kind."),
      ("force_condition", "Write them into CONDITION_OCCURRENCE anyway",
       "Cheap, and wrong in a way that is hard to see later: a screening encounter becomes a diagnosis. Any cohort built by counting conditions would over-count."),
      ("keep_dropping", "Leave them unpublished (today's behaviour)",
       "Nothing breaks, 1.23M rows stay invisible, and the review queue keeps 4,064 items nobody can action."),
    ],
    why_me="Whether a Z code is a clinical fact worth publishing, and as what, is a modelling judgement with clinical consequences. The counts are certain; the right answer is not."),

  dict(
    id="POE_MIXED_ORDERS",
    title="Provider order entry is typed as drug orders, and 55% of it is not drugs",
    kind="modelling",
    at_stake="28,915,531 rows of 52,211,309 (55.4%) mis-typed",
    evidence=[
      "<code>datasets/mimiciv.yaml</code> declares the whole <code>poe</code> table as <code>event_kind: drug_order</code>.",
      "Only <b>Medications</b> (23,295,778 rows) is a drug order. The rest: Lab 8,908,993 · General Care 7,670,925 · ADT orders 2,776,808 · IV therapy 2,747,821 · Nutrition 2,131,521 · Radiology 1,876,077 · Consults 826,947 · and nine smaller types.",
      "Harmless today only because the drug domain maps nothing. The moment RxNorm or NDC lookup starts working, a radiology order can become a DRUG_EXPOSURE row.",
      "The retrieval run makes this visible: the three highest-volume &lsquo;drugs&rsquo; in the queue are <i>Medications</i>, <i>Lab</i> and <i>General Care</i>, and the retriever dutifully offered RxNorm candidates for all three.",
    ],
    options=[
      ("row_filter", "Add a generic row filter to the config, keep only Medications as drug_order",
       "A <code>where:</code> clause on a source is dataset-general &mdash; a mixed table needing a split by a type column is common. The other 28.9M rows are dropped, or declared separately under an honest kind."),
      ("keep_all_reclassify", "Keep every row but stop calling them drug orders",
       "Needs an event kind that does not exist yet. Honest, and it puts 52M weakly-informative rows into the event stream."),
      ("drop_poe", "Stop ingesting poe entirely",
       "The medication orders it holds are largely also in <code>prescriptions</code>; the rest is order metadata. Simplest, and it loses the ordering signal."),
    ],
    why_me="Which order types count as medications, and whether order metadata belongs in a patient event stream at all, is a decision about what the corpus is for."),

  dict(
    id="MICROBIOLOGY_ORGANISM",
    title="Culture results carry no organism",
    kind="clinical",
    at_stake="1,635,365 of 3,988,224 microbiology rows have an organism; none of it reaches any layer",
    evidence=[
      "The config maps <code>value</code> to <code>interpretation</code> (the S/I/R susceptibility) and <code>source_code</code> to <code>test_name</code>. It does not map <code>org_name</code>.",
      "In MEDS a blood culture is therefore an event with a code and <b>no value at all</b> &mdash; you can see that a culture was sent, not what grew.",
      "Present in the source: E. coli 579,398 · S. aureus 219,582 · K. pneumoniae 155,688 · P. aeruginosa 83,290 · P. mirabilis 68,163.",
      "<code>keep_columns</code> exists in the config schema and would have been the obvious carrier &mdash; it is declared but never read anywhere in <code>src/</code>, so it silently does nothing.",
    ],
    options=[
      ("value_organism", "Put the organism in the value: <code>value: {from: [org_name, interpretation]}</code>",
       "One line. Recovers 1.64M organism results into <code>text_value</code>. Where nothing grew it falls back to the interpretation, which conflates two different facts in one field."),
      ("two_sources", "Declare microbiology twice: organism identification, and susceptibility",
       "Correct modelling &mdash; they are different facts. Costs a second pass over the table and quarantines rows that have no antibiotic name."),
      ("keep", "Leave it (today's behaviour)", "The organism stays in the source layer and never reaches OMOP or MEDS."),
    ],
    why_me="Whether an organism and its susceptibility are one fact or two is a clinical modelling call, and it decides what an infection cohort can be built from."),

  dict(
    id="CTPE_MULTI_CODE_CELLS",
    title="A diagnosis cell holding several codes",
    kind="clinical + modelling",
    at_stake="4,836 of 32,467 distinct codes (14.9%) &mdash; 86,468 of 4,256,405 condition rows (2.0%)",
    evidence=[
      "The reference export writes problem lists as one cell holding several codes: <code>R78.81, B95.7, Z16.29</code> &mdash; three distinct diagnoses in one string.",
      "The converter treats the whole string as one code, so it matches nothing and every such cell stays unmapped.",
      "<b>Corrected from the first version of this page.</b> That version said the column resolved at 0.0%. That figure was the share <i>within the review queue</i>, which contains only the codes that failed &mdash; it is true of the queue and false of the column. Measured against the whole canonical layer, ICD-10-CM on this dataset resolves <b>76.6% of distinct codes and 77.2% of condition rows</b>.",
      "So this is a long-tail problem, not a broken column: the multi-code cells are 14.9% of distinct codes but only 2.0% of rows.",
    ],
    options=[
      ("split", "Split on the separator into one condition event per code",
       "Recovers 86,468 rows. Needs a declared separator in the config and a decision about whether the codes are ranked (primary vs secondary) or unordered."),
      ("keep_whole", "Keep the cell as one term (today's behaviour)",
       "Nothing is invented, and 4,836 codes carrying 2.0% of condition rows stay permanently unmapped."),
      ("ask_owner", "Ask the data owner what the separator means before touching it",
       "The safe answer if the ordering carries meaning &mdash; a comma list may or may not be rank-ordered."),
    ],
    why_me="Whether the codes in one cell are co-equal diagnoses or a ranked list changes what splitting them asserts."),

  dict(
    id="END_BEFORE_START",
    title="Intervals that end before they start",
    kind="policy · already implemented, please confirm",
    at_stake="816,994 of 20,292,611 MIMIC prescription rows (4.03%)",
    evidence=[
      "MIMIC-IV <code>prescriptions</code> carries <code>stoptime</code> earlier than <code>starttime</code> on 4% of rows &mdash; a source contradiction, not a converter bug.",
      "Until this session nothing marked it: no quality flag, and none of the 35 checks compared the two. A consumer computing exposure duration got a negative number silently.",
      "Now implemented: the times are kept exactly as written, the event carries <code>END_BEFORE_START</code>, and a 36th check fails the build if such an event is ever unflagged.",
      "Deliberately <b>not</b> repaired: clamping the end to the start invents a zero duration, swapping them invents an interval, dropping it destroys the evidence.",
    ],
    options=[
      ("flag_and_keep", "Flag and keep, as now", "Consistent with how post-death records are handled. The contradiction stays visible and reviewable."),
      ("quarantine", "Quarantine the row instead", "Stronger: nothing impossible reaches the event stream. Costs ~817k drug orders."),
      ("null_end", "Keep the row, null the end time, flag it", "The interval is unusable anyway; this stops anyone computing on it. Loses the source's stated value."),
    ],
    why_me="Confirming a policy already implemented. Say if you want it stricter."),

  dict(
    id="AMBIGUOUS_MAPS_TO",
    title="One code, several standard concepts",
    kind="clinical",
    at_stake="623 terms (MIMIC 277 · CTPE 346)",
    evidence=[
      "An ICD-10-CM combination code genuinely maps to more than one SNOMED concept &mdash; &lsquo;diabetes with nephropathy&rsquo; is two findings, not one.",
      "The converter publishes one concept per event, so it takes the lowest concept id and records the choice as <code>_ambiguous</code> in the mapping path.",
      "The choice is therefore reviewable but unreviewed: nobody has looked at any of the 623.",
    ],
    options=[
      ("keep_lowest", "Keep the deterministic pick, flagged (today's behaviour)",
       "Reproducible and arbitrary. Fine if nobody analyses these codes; wrong for whichever half of the combination matters."),
      ("review_each", "Put all 623 in front of a clinician",
       "The accurate answer. 623 decisions, and the term shortlist format already supports it."),
      ("emit_both", "Emit one event per target concept",
       "Faithful to the vocabulary and it multiplies rows; a patient gains two conditions where the source recorded one code."),
    ],
    why_me="Which half of a combination code to keep is a clinical question, and the answer differs per code."),
]

CSS = """
:root{--bg:#fbfbfa;--card:#fff;--ink:#1a1a19;--muted:#6b6b68;--line:#e4e4e1;
--accent:#2d6a4f;--warn:#9a5b00;--warnbg:#fdf6e9;--pick:#eef5f1;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1e1e1c;--ink:#eceae6;
--muted:#9a9a95;--line:#33322e;--accent:#74c69d;--warn:#d9a441;--warnbg:#2a2317;
--pick:#20302a;--chip:#26251f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:14px 22px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
main{max-width:960px;margin:0 auto;padding:22px}
.intro{color:var(--muted);font-size:14px;margin:0 0 26px;padding:14px 16px;
background:var(--chip);border-radius:8px;border:1px solid var(--line)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin-bottom:20px}
.card h2{font-size:16px;margin:0 0 4px;font-weight:640;letter-spacing:-.01em}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 14px}
.chip{font-size:11.5px;padding:3px 9px;border-radius:999px;background:var(--chip);
color:var(--muted);border:1px solid var(--line);font-weight:500}
.chip.stake{background:var(--warnbg);color:var(--warn);border-color:transparent;font-weight:600}
ul.ev{margin:0 0 16px;padding-left:20px;font-size:14px;color:var(--ink)}
ul.ev li{margin-bottom:5px}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.opt{display:block;border:1px solid var(--line);border-radius:8px;padding:11px 14px;
margin-bottom:8px;cursor:pointer;transition:background .12s,border-color .12s}
.opt:hover{border-color:var(--accent)}
.opt.on{background:var(--pick);border-color:var(--accent)}
.opt input{margin-right:9px}
.opt b{font-weight:600}
.opt .cons{display:block;margin:5px 0 0 24px;color:var(--muted);font-size:13px}
.why{font-size:13px;color:var(--muted);border-left:2px solid var(--line);
padding-left:12px;margin:14px 0 16px;font-style:italic}
textarea{width:100%;min-height:62px;padding:9px 11px;border:1px solid var(--line);
border-radius:7px;background:var(--bg);color:var(--ink);font:inherit;font-size:13.5px;resize:vertical}
input[type=text]{padding:7px 11px;border:1px solid var(--line);border-radius:7px;
background:var(--card);color:var(--ink);font:inherit;font-size:13.5px}
button{padding:8px 15px;border:1px solid var(--line);border-radius:7px;background:var(--card);
color:var(--ink);font:inherit;font-size:13.5px;cursor:pointer;font-weight:500}
button.primary{background:var(--accent);color:#fff;border-color:transparent}
button:hover{border-color:var(--accent)}
.count{margin-left:auto;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
footer{max-width:960px;margin:0 auto;padding:0 22px 60px;color:var(--muted);font-size:13px}
"""

JS = """
const N = __N__;
const state = {};
try { Object.assign(state, JSON.parse(localStorage.getItem("ehr2cdm_decisions") || "{}")); } catch (e) {}

function save() {
  try { localStorage.setItem("ehr2cdm_decisions", JSON.stringify(state)); } catch (e) {}
  const done = Object.values(state).filter(s => s && s.choice).length;
  document.getElementById("count").textContent = done + " of " + N + " answered";
}
document.querySelectorAll(".opt").forEach(el => {
  el.addEventListener("click", () => {
    const id = el.dataset.q, val = el.dataset.v;
    state[id] = Object.assign({}, state[id], { choice: val });
    document.querySelectorAll('.opt[data-q="' + id + '"]').forEach(o => {
      o.classList.toggle("on", o.dataset.v === val);
      o.querySelector("input").checked = o.dataset.v === val;
    });
    save();
  });
});
document.querySelectorAll("textarea").forEach(el => {
  if (state[el.dataset.q] && state[el.dataset.q].note) el.value = state[el.dataset.q].note;
  el.addEventListener("input", () => {
    state[el.dataset.q] = Object.assign({}, state[el.dataset.q], { note: el.value });
    save();
  });
});
document.querySelectorAll(".opt").forEach(o => {
  const s = state[o.dataset.q];
  if (s && s.choice === o.dataset.v) { o.classList.add("on"); o.querySelector("input").checked = true; }
});
save();

document.getElementById("export").addEventListener("click", () => {
  const who = document.getElementById("who").value.trim();
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
  const payload = { reviewer: who, decided_on: new Date().toISOString().slice(0, 10),
                    answers: state };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = ["modelling_decisions", who.replace(/[^A-Za-z0-9]+/g, "_"), stamp]
                 .filter(Boolean).join("_") + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
});
document.getElementById("clear").addEventListener("click", () => {
  if (!confirm("Clear every answer on this page?")) return;
  for (const k of Object.keys(state)) delete state[k];
  try { localStorage.removeItem("ehr2cdm_decisions"); } catch (e) {}
  location.reload();
});
"""

def card(d, i):
    ev = "\n".join(f"      <li>{x}</li>" for x in d["evidence"])
    opts = "\n".join(
        f'''      <label class="opt" data-q="{d['id']}" data-v="{v}">
        <input type="radio" name="{d['id']}"><b>{html.escape(label)}</b>
        <span class="cons">{cons}</span>
      </label>''' for v, label, cons in d["options"])
    return f'''  <section class="card">
    <h2>{i}. {html.escape(d['title'])}</h2>
    <div class="meta">
      <span class="chip">{html.escape(d['kind'])}</span>
      <span class="chip stake">{html.escape(d['at_stake'])}</span>
      <span class="chip">{d['id']}</span>
    </div>
    <ul class="ev">
{ev}
    </ul>
{opts}
    <p class="why">{html.escape(d['why_me'])}</p>
    <textarea data-q="{d['id']}" placeholder="Reasoning, caveats, or a different answer entirely"></textarea>
  </section>'''

cards = "\n".join(card(d, i) for i, d in enumerate(DECISIONS, 1))
page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Modelling decisions &mdash; ehr-to-omop-meds</title>
<style>{CSS}</style></head><body>
<header>
  <h1>Modelling decisions</h1>
  <span class="sub">questions retrieval cannot answer</span>
  <input type="text" id="who" placeholder="your name">
  <button class="primary" id="export">Export answers</button>
  <button id="clear">Clear</button>
  <span class="count" id="count"></span>
</header>
<main>
  <p class="intro">
    The term shortlist asks <i>what does this string mean</i>. These are the other kind of
    question: the string is already understood, and what is undecided is what the converter
    should <b>do</b> with it. Each answer below moves more rows than a hundred term mappings,
    and none can be settled by a vocabulary, a retriever or a model &mdash; which is why they
    are still open. Nothing here is applied automatically; exporting writes a JSON file and a
    person decides what to implement.
  </p>
{cards}
</main>
<footer>
  Answers are kept in this browser only until you export them. Counts were measured on 2026-09-02 against the
  full Athena bundle (131 vocabularies, 10,167,185 concepts) and the conversions rebuilt on it;
  every number above is reproducible from the review queues, the canonical layer and the source
  parquet. Where a number differs from the first version of this page, the difference is noted
  in the card rather than silently corrected.
</footer>
<script>{JS.replace("__N__", str(len(DECISIONS)))}</script>
</body></html>"""
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True, help="where to write the page")
args = parser.parse_args()
args.out.write_text(page, encoding="utf-8")
print(f"{len(DECISIONS)} decisions -> {args.out}  ({len(page)/1024:.0f} KB)")
