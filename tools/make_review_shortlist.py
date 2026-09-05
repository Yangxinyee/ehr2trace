"""Turn the review queue into a shortlist a clinician can actually sign.

The queue on our reference export holds 34,526 undecided terms, which reads as
unreviewable and is not. Occurrences are power-law distributed: the top 50 terms cover
65% of the affected rows and 141 laboratory names cover 14.9M of them. Nobody has to look
at 34,526 things. They have to look at a few hundred, in the right order, with candidates
already in front of them.

So this does the part a machine can do and stops:

  1. rank the queue by how many rows each term decides, and take the head
  2. expand the abbreviation first, if a model is available. Dense retrieval finds the
     right concept readily once the string says what it means and reliably fails while
     it does not: `K SERUM` retrieved vitamin K in all eight candidates and `potassium
     serum` retrieved serum potassium first. The model writes a *query* here, never a
     concept id, which is the narrowest useful place to put it
  3. retrieve candidates both densely and lexically, and merge. Neither alone is enough
     and the two fail on different things. Dense retrieval handles the abbreviations
     (`NA`, `K`, `HGB`) that lexical matching cannot touch. Lexical matching handles the
     compositional drug names that dense retrieval cannot: RxNorm calls lactated
     Ringer's `Calcium Chloride ... / Sodium Chloride 6 MG/ML Irrigation Solution
     [Lactated Ringer'S Irrigation]`, and the only recognisable words are 150 characters
     in, past where the encoder truncates
  4. optionally re-rank with the same model, which on laboratory strings is worth
     +9.3 points and on diagnosis descriptions is worth -4.4 (see the paper); it is
     therefore opt-in per run rather than always on

and writes a sheet whose `decision` column is **empty**. Nothing here reaches a published
mapping: `compile` only compiles rows a person marked `accept`, and the check
`UNDECIDED_PROPOSALS_NEVER_PUBLISHED` fails the build if that boundary is crossed. The
model's pick is pre-filled in `concept_id` so that agreeing is one word, and the four
runners-up sit in the same row so that disagreeing is also one word.

Usage::

    python tools/make_review_shortlist.py --dataset datasets/ctpe.yaml \\
        --built $WORK/ctpe --vocab $VOCAB --model /path/to/bge-m3 \\
        --kinds measurement --top 500 --llm --out review/shortlist.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.terminology import DOMAIN_FOR_KIND  # noqa: E402

#: How many candidates to show a reviewer. Five fits on a screen; past that the sheet
#: stops being readable, which defeats the point of making one.
#: Candidate columns written to the sheet. The ranker sees the full merged list
#: (`--show`); this is how many survive into the file a person reads.
SHOWN = 8

SHEET_FIELDS = [
    "id", "decision", "concept_id", "concept_name", "domain_id", "vocabulary_id",
    "reviewer", "decided_on", "note",
    "source_string", "source_name", "event_kind", "occurrences", "rows_share_pct",
    "retrieved_by", "expansion", "expansion_confidence",
    "model_pick_rank", "model_rationale", "flag",
]
for _i in range(1, SHOWN + 1):
    SHEET_FIELDS += [f"cand{_i}_id", f"cand{_i}_name", f"cand{_i}_vocab", f"cand{_i}_score"]


def select(pending: list[dict], kinds: set[str], top: int) -> list[dict]:
    """Every term of the named kinds, plus the head of the queue by rows decided.

    The union rather than the intersection: a kind small enough to review exhaustively
    should be reviewed exhaustively, and everything else is worth doing in the order that
    clears the most rows per decision.
    """
    rows = [r for r in pending if (r.get("status") or "open") == "open"]
    for row in rows:
        row["occurrences"] = int(row.get("occurrences") or 0)
    by_rows = sorted(rows, key=lambda r: -r["occurrences"])
    chosen = {r["id"]: r for r in by_rows[:top]}
    chosen.update({r["id"]: r for r in rows if r.get("event_kind") in kinds})
    return sorted(chosen.values(), key=lambda r: -r["occurrences"])


def expand(rows: list[dict], concurrency: int) -> None:
    """Ask the model what each local string means. It writes a query, not an answer."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ehr2cdm.llm import LlmClient

    local = threading.local()

    def client():
        if not hasattr(local, "c"):
            local.c = LlmClient.from_env()
        return local.c

    def one(row: dict) -> None:
        domain = DOMAIN_FOR_KIND.get(row.get("event_kind") or "")
        if not domain:
            return
        result = client().expand_term(
            row.get("source_string") or "", row.get("source_name") or "", domain
        )
        if result is None or not (result.expansion or "").strip():
            return
        row["expansion"] = result.expansion.strip()
        row["expansion_confidence"] = round(float(result.confidence), 2)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(one, rows))


def query_text(row: dict) -> tuple[str, str]:
    """What to hand the retriever, and a note saying which field it came from.

    `source_string` is often the abbreviation the hospital types (`NA`) and `source_name`
    the fuller label behind it (`LAB CHEM SODIUM`). Where they differ, both are given:
    the abbreviation is what a human would search for and the label is what actually
    carries the meaning, and throwing either away loses a real signal.
    """
    expansion = (row.get("expansion") or "").strip()
    code = (row.get("source_string") or "").strip()
    name = (row.get("source_name") or "").strip()
    if expansion:
        # The expansion replaces rather than joins the abbreviation: leaving `K` in the
        # query is what pulled the whole neighbourhood towards vitamin K.
        return expansion, "expansion"
    if code.isdigit() and name:
        # A purely numeric code is a surrogate key, not an abbreviation. MIMIC-IV names
        # its laboratory tests `50912`; there is nothing in that string to embed, and
        # leaving it in front of the label asks the encoder to place the query somewhere
        # between a creatinine assay and a five-digit number. The label carries the whole
        # signal, so the number is dropped rather than joined -- the same reason an
        # expansion replaces `K` instead of sitting beside it.
        return name, "name"
    if name and name.lower() != code.lower():
        return f"{code} {name}", "code+name"
    return code or name, "code"


#: Concepts that are real, standard, and useless as retrieval candidates for a hospital's
#: own vocabulary. They are excluded from the pool rather than down-ranked, because with
#: 32 slots a term competes against its own packaging variants: RxNorm Extension carries
#: one concept per box size and per manufacturer, so `cefazolin 2000 MG Injection` sits
#: behind dozens of `... Box of 25 [Cefazolin Hikma]` entries that mean the same drug.
#:
#: Measured on the installed bundle: 1,640,071 of 2,022,711 standard Drug concepts (81%)
#: are a brand, a manufacturer or a box, and every concept this project has verified by
#: hand as a correct answer survives the filter.
#:
#: The cost is real and worth stating: a product that exists *only* under a brand becomes
#: unreachable. That is the right trade for mapping a ward's generic drug names, and the
#: wrong one for mapping a pharmacy's branded inventory -- hence a flag, not a default
#: buried in code.
POOL_EXCLUSIONS = {
    "Drug": (
        "concept_name NOT LIKE '%Box of %' "
        "AND NOT regexp_matches(concept_name, ' by [A-Z][A-Za-z0-9&. -]+$') "
        "AND concept_name NOT LIKE '%[%]%'"
    ),
    # Gene variants and cancer staging are standard Measurement concepts that no ordinary
    # laboratory name should ever retrieve. They were 14% of the measurement pool and are
    # what `STX1` and `UTX3` were matching against.
    "Measurement": "vocabulary_id NOT IN ('OMOP Genomic', 'Cancer Modifier', 'SNOMED Veterinary')",
}


def load_pool(vocab_dir: Path, domain: str, denoise: bool = False) -> tuple[list[int], list[str], list[str]]:
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    options = "delim='\\t', header=true, all_varchar=true, quote=''"
    rows = con.execute(
        f"""SELECT CAST(concept_id AS BIGINT), concept_name, vocabulary_id
            FROM read_csv('{vocab_dir / "CONCEPT.csv"}', {options})
            WHERE standard_concept = 'S' AND domain_id = '{domain}'
              AND (invalid_reason IS NULL OR invalid_reason = '')
              AND {POOL_EXCLUSIONS.get(domain, '1=1') if denoise else '1=1'}
            ORDER BY concept_id"""
    ).fetchall()
    con.close()
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def embed(texts: list[str], model_dir: Path, device: str, batch: int, cache: Path | None, label: str):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    if cache and cache.exists():
        print(f"  [{label}] cached", flush=True)
        return np.load(cache)
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModel.from_pretrained(str(model_dir), dtype=torch.float32).to(device).eval()
    out, t0 = [], time.time()
    with torch.inference_mode():
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            enc = tokenizer(chunk, padding=True, truncation=True, max_length=64,
                            return_tensors="pt").to(device)
            vec = torch.nn.functional.normalize(model(**enc).last_hidden_state[:, 0], p=2, dim=1)
            out.append(vec.cpu().numpy().astype("float32"))
            if i and i % (batch * 100) == 0:
                done = i + len(chunk)
                print(f"  [{label}] {done:,}/{len(texts):,}  {done / (time.time() - t0):.0f}/s", flush=True)
    array = np.vstack(out)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, array)
    return array


#: Words that say how a drug was given rather than what it was. A token match treats
#: them as evidence: `LACTATED RINGERS IV BOLUS` retrieves `COCHLIOBOLUS LUNATUS`.
_FORM_WORDS = frozenset("""
iv ivpb po pr im sq subq intravenous intravenously oral orally injection injectable
infusion bolus solution soln syringe vial bag premix premixed tab tabs tablet capsule
cap caps supp suppository cream ointment patch drops spray inhaler neb nebulizer
pf preservative free continuous drip push each ml mg mcg gm gram units unit
""".split())


def core_form(text: str) -> str:
    """The substance words, with route, form and strength taken out.

    Not a general normaliser -- it is the crude filter that makes a token match usable on
    an order description, and it is applied only to build an *extra* query. The unstripped
    forms are still searched, so nothing is lost if this strips too much.
    """
    import re

    cleaned = re.sub(r"[()\[\],/%]", " ", (text or "").lower())
    words = [
        w for w in cleaned.split()
        if w not in _FORM_WORDS and not any(ch.isdigit() for ch in w) and len(w) > 1
    ]
    return " ".join(words)


def retrieve_lexically(rows: list[dict], vocab_dir: Path, k: int) -> None:
    """Attach `lexical` candidates, from the vocabulary's own text search.

    Queried on the expansion and, separately, on the source string: the expansion carries
    the meaning but also carries route and form words that derail a token match --
    `LACTATED RINGERS IV BOLUS` retrieves `COCHLIOBOLUS LUNATUS` because `BOLUS` is a
    token. Both queries are cheap and the union is what gets ranked.
    """
    from ehr2cdm.terminology import Vocabulary

    vocabulary = Vocabulary.open(vocab_dir)
    if not getattr(vocabulary, "available", False):
        return
    try:
        for row in rows:
            domain = DOMAIN_FOR_KIND.get(row.get("event_kind") or "")
            if not domain:
                continue
            queries = list(dict.fromkeys(
                q for q in (core_form(row.get("expansion") or ""),
                            (row.get("expansion") or "").strip(),
                            core_form(row.get("source_name") or ""),
                            (row.get("source_name") or "").strip(),
                            (row.get("source_string") or "").strip()) if q
            ))
            # Kept per query and round-robined below, not concatenated. Concatenating let
            # a bad query fill the quota before a good one was reached: for `LACTATED
            # RINGERS IV BOLUS` the unstripped query returns things that merely share the
            # words `intravenous` and `solution`, and the stripped one returns the answer.
            per_query = [
                [{"concept_id": c.concept_id, "concept_name": c.concept_name,
                  "vocabulary_id": c.vocabulary_id, "score": round(float(c.score), 4),
                  "found_by": "lexical"}
                 for c in vocabulary.candidates(q, domain, limit=k)]
                for q in queries
            ]
            seen: dict[int, dict] = {}
            for rank in range(k):
                for results in per_query:
                    if rank < len(results):
                        seen.setdefault(results[rank]["concept_id"], results[rank])
            row["lexical"] = list(seen.values())[: 2 * k]
    finally:
        vocabulary.close()


def merge(rows: list[dict], keep: int) -> None:
    """Interleave the dense and lexical lists, best-first, deduplicated by concept.

    Interleaved rather than concatenated because the ranker sees a fixed number of
    candidates: appending one list to the other would let whichever went first crowd the
    other out entirely, which is the failure this merge exists to prevent.
    """
    for row in rows:
        dense = list(row.get("candidates") or [])
        lexical = list(row.get("lexical") or [])
        for c in dense:
            c.setdefault("found_by", "dense")
        merged: dict[int, dict] = {}
        for i in range(max(len(dense), len(lexical))):
            for source in (dense, lexical):
                if i < len(source):
                    existing = merged.get(source[i]["concept_id"])
                    if existing is None:
                        merged[source[i]["concept_id"]] = dict(source[i])
                    elif existing["found_by"] != source[i]["found_by"]:
                        # Found by both. Worth saying: agreement between two retrievers
                        # that fail on different things is the strongest signal here.
                        existing["found_by"] = "both"
            if len(merged) >= keep:
                break
        row["candidates"] = list(merged.values())[:keep]
        row.pop("lexical", None)


def retrieve(rows: list[dict], vocab: Path, model_dir: Path, device: str, batch: int,
             cache_dir: Path, k: int, denoise: bool = False) -> None:
    """Attach `candidates` to each row, densely retrieved within its own domain."""
    import numpy as np

    by_domain: dict[str, list[dict]] = {}
    for row in rows:
        domain = DOMAIN_FOR_KIND.get(row.get("event_kind") or "")
        if domain:
            by_domain.setdefault(domain, []).append(row)
        else:
            row["candidates"] = []

    for domain, group in sorted(by_domain.items()):
        ids, names, vocabs = load_pool(vocab, domain, denoise)
        tag = f"{domain.lower()}{'_denoised' if denoise else ''}"
        print(f"{domain}: {len(group)} terms against {len(ids):,} concepts"
              + (" (denoised)" if denoise else ""), flush=True)
        pool = embed(names, model_dir, device, batch, cache_dir / f"bge-m3_{tag}.npy", domain)
        queries = embed([query_text(r)[0] for r in group], model_dir, device, batch, None, f"{domain} queries")
        scores = queries @ pool.T
        top = np.argpartition(-scores, kth=k, axis=1)[:, :k]
        order = np.take_along_axis(top, np.argsort(-np.take_along_axis(scores, top, axis=1), axis=1), axis=1)
        for i, row in enumerate(group):
            row["candidates"] = [
                {"concept_id": int(ids[j]), "concept_name": names[j],
                 "vocabulary_id": vocabs[j], "score": round(float(scores[i, j]), 4),
                 "found_by": "dense"}
                for j in order[i]
            ]


#: Phrases the ranking prompt invites when nothing fits. Matching on the model's own
#: words is crude; the alternative is trusting a pre-filled answer it disowned.
_NO_MATCH = ("none of the candidates", "no candidate", "none match", "does not match",
             "none of these", "no good match", "not a good match")


def _says_no_match(rationale: str) -> bool:
    text = (rationale or "").lower()
    return any(phrase in text for phrase in _NO_MATCH)


def rerank(rows: list[dict], concurrency: int) -> None:
    """Ask a local model to reorder each candidate list. Advisory, never authoritative."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ehr2cdm.llm import LlmClient

    local = threading.local()

    def client():
        if not hasattr(local, "c"):
            local.c = LlmClient.from_env()
        return local.c

    def one(row: dict) -> None:
        from ehr2cdm.terminology import Candidate

        if not row.get("candidates"):
            return
        domain = DOMAIN_FOR_KIND.get(row.get("event_kind") or "") or ""
        candidates = [
            Candidate(concept_id=c["concept_id"], concept_name=c["concept_name"],
                      domain_id=domain, vocabulary_id=c["vocabulary_id"], score=c["score"])
            for c in row["candidates"]
        ]
        ranking = client().rank_candidates(query_text(row)[0], domain, candidates)
        if ranking is None:
            row["model_rationale"] = "model returned no usable ranking"
            row["flag"] = "NO_RANKING"
            return
        ordered = sorted(ranking.ranking, key=lambda c: c.rank)
        if not ordered:
            return
        pick = ordered[0].concept_id
        row["model_pick_rank"] = next(
            (i + 1 for i, c in enumerate(row["candidates"]) if c["concept_id"] == pick), None
        )
        index = {c["concept_id"]: c for c in row["candidates"]}
        row["candidates"] = [index[c.concept_id] for c in ordered if c.concept_id in index] + [
            c for c in row["candidates"] if c["concept_id"] not in {o.concept_id for o in ordered}
        ]
        row["model_rationale"] = (ranking.rationale or "")[:300]
        # The model is told to say so when nothing fits, and it does. Worth surfacing --
        # but only as a warning. An earlier version also blanked the pre-filled answer,
        # and that discarded correct ones: RxNorm normalises salts, so `ONDANSETRON HCL
        # 4 MG/2 ML` really is `ondansetron 2 MG/ML Injection`, and the model rejected it
        # for not saying hydrochloride. Of the flags raised here, most are that kind of
        # fussiness about salt, route, form or strength rather than a retrieval failure.
        if _says_no_match(row["model_rationale"]):
            row["flag"] = "CHECK_CAREFULLY"

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(one, rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pending", type=Path, required=True, help="review/pending.csv")
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True, help="a bge-m3 style encoder")
    ap.add_argument("--cache-dir", type=Path, required=True, help="where pool embeddings live")
    ap.add_argument("--kinds", default="measurement", help="event kinds to include exhaustively")
    ap.add_argument("--top", type=int, default=500, help="plus this many by rows decided")
    ap.add_argument("--k", type=int, default=8, help="candidates retrieved per term, per retriever")
    ap.add_argument("--show", type=int, default=12, help="candidates kept after merging")
    ap.add_argument("--dense-only", action="store_true", help="skip the lexical pass")
    ap.add_argument("--denoise", action="store_true",
                    help="drop brand, manufacturer and packaging variants from the candidate "
                         "pool, and gene/cancer-staging concepts from the measurement pool. "
                         "See POOL_EXCLUSIONS for what this costs.")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--expand", action="store_true",
                    help="expand each abbreviation with LLM_MODEL before retrieving")
    ap.add_argument("--llm", action="store_true", help="re-rank the candidates with LLM_MODEL")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--candidates-json", type=Path, default=None,
                    help="also write every merged candidate as JSON. The sheet shows the "
                         "first eight because that is what a person can read on a screen; "
                         "a ranker can see further down the list than that, and the two "
                         "consumers should not be forced to share a width.")
    args = ap.parse_args()

    with open(args.pending, newline="", encoding="utf-8") as fh:
        pending = list(csv.DictReader(fh))
    total_rows = sum(int(r.get("occurrences") or 0) for r in pending)
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    rows = select(pending, kinds, args.top)
    covered = sum(r["occurrences"] for r in rows)
    print(f"{len(rows)} of {len(pending)} terms, deciding {covered:,} of {total_rows:,} rows "
          f"({100 * covered / max(1, total_rows):.1f}%)", flush=True)

    if args.expand:
        print("expanding abbreviations", flush=True)
        expand(rows, args.concurrency)
        print(f"  expanded {sum(1 for r in rows if r.get('expansion'))}/{len(rows)}", flush=True)
    retrieve(rows, args.vocab, args.model, args.device, args.batch, args.cache_dir, args.k, args.denoise)
    if not args.dense_only:
        print("retrieving lexically", flush=True)
        retrieve_lexically(rows, args.vocab, args.k)
        merge(rows, args.show)
        both = sum(1 for r in rows for c in (r.get("candidates") or []) if c["found_by"] == "both")
        print(f"  {both} candidates found by both retrievers", flush=True)
    if args.llm:
        print("re-ranking", flush=True)
        rerank(rows, args.concurrency)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SHEET_FIELDS)
        writer.writeheader()
        for row in rows:
            candidates = row.get("candidates") or []
            best = candidates[0] if candidates else {}
            record = {
                "id": row["id"],
                # Deliberately empty. `compile` ignores anything that is not `accept`, and
                # a sheet that arrives pre-accepted is not a review.
                "decision": "",
                "concept_id": best.get("concept_id", ""),
                "concept_name": best.get("concept_name", ""),
                "domain_id": DOMAIN_FOR_KIND.get(row.get("event_kind") or "") or "",
                "vocabulary_id": best.get("vocabulary_id", ""),
                "reviewer": "", "decided_on": "", "note": "",
                "source_string": row.get("source_string", ""),
                "source_name": row.get("source_name", ""),
                "event_kind": row.get("event_kind", ""),
                "occurrences": row["occurrences"],
                "rows_share_pct": round(100 * row["occurrences"] / max(1, total_rows), 3),
                "retrieved_by": query_text(row)[1],
                "expansion": row.get("expansion", ""),
                "expansion_confidence": row.get("expansion_confidence", ""),
                "model_pick_rank": row.get("model_pick_rank", ""),
                "model_rationale": row.get("model_rationale", ""),
                "flag": row.get("flag", ""),
            }
            for i in range(SHOWN):
                c = candidates[i] if i < len(candidates) else {}
                record[f"cand{i + 1}_id"] = c.get("concept_id", "")
                record[f"cand{i + 1}_name"] = c.get("concept_name", "")
                record[f"cand{i + 1}_vocab"] = c.get("vocabulary_id", "")
                record[f"cand{i + 1}_score"] = c.get("score", "")
            writer.writerow(record)

    if args.candidates_json:
        args.candidates_json.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "id": row["id"],
                "source_string": row.get("source_string", ""),
                "source_name": row.get("source_name", ""),
                "event_kind": row.get("event_kind", ""),
                "code_system": row.get("code_system", ""),
                "occurrences": row.get("occurrences", 0),
                "expansion": row.get("expansion", ""),
                "candidates": [
                    {
                        "concept_id": c.get("concept_id"),
                        "concept_name": c.get("concept_name", ""),
                        "vocabulary_id": c.get("vocabulary_id", ""),
                        "found_by": c.get("found_by", ""),
                    }
                    for c in (row.get("candidates") or [])
                ],
            }
            for row in rows
        ]
        args.candidates_json.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(f"-> {args.candidates_json}  ({len(payload)} terms, "
              f"{sum(len(r['candidates']) for r in payload)} candidates)", flush=True)

    summary = args.out.with_suffix(".summary.json")
    summary.write_text(json.dumps({
        "pending_terms": len(pending),
        "shortlisted_terms": len(rows),
        "rows_in_queue": total_rows,
        "rows_decided_by_shortlist": covered,
        "share_pct": round(100 * covered / max(1, total_rows), 1),
        "kinds_exhaustive": sorted(kinds),
        "top_by_rows": args.top,
        "k": args.k,
        "reranked": bool(args.llm),
        "terms_with_no_candidate": sum(1 for r in rows if not r.get("candidates")),
        "expanded": sum(1 for r in rows if r.get("expansion")),
        "flagged_check_carefully": sum(1 for r in rows if r.get("flag") == "CHECK_CAREFULLY"),
    }, indent=2))
    print(f"-> {args.out}\n-> {summary}")


if __name__ == "__main__":
    main()
