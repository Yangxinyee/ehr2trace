"""Is retrieval the bottleneck, and does a better retriever move it?

The ranking experiment (`measure_terminology_llm.py`) found that recall@k is the
ceiling: from k=4 to k=64 lexical recall nearly doubles while overall accuracy does not
move, because the lexical score carries almost no information past the first few
candidates. A re-ranker cannot recover a term whose answer was never retrieved, and for
the strings that actually need help -- a hospital's local laboratory names, which are
abbreviations like `HGB` and `NA` -- lexical recall against a standard vocabulary is
close to hopeless by construction.

That diagnosis implies a remedy, and this measures whether the remedy works: replace the
lexical recall with dense retrieval over concept names and see what happens to recall@k.

The held-out terms are read back from the ranking experiment's own output rather than
resampled, so the two are the same 300 items and the recall columns are directly
comparable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def load_pool(vocab_dir: Path, domain: str) -> tuple[list[int], list[str]]:
    """Standard concepts of one domain: the set a correct answer must come from."""
    import duckdb

    con = duckdb.connect()
    opt = "delim='\\t', header=true, all_varchar=true, quote=''"
    rows = con.execute(
        f"""
        SELECT CAST(concept_id AS BIGINT), concept_name
        FROM read_csv('{vocab_dir / "CONCEPT.csv"}', {opt})
        WHERE standard_concept = 'S'
          AND domain_id = '{domain}'
          AND (invalid_reason IS NULL OR invalid_reason = '')
        ORDER BY concept_id
        """
    ).fetchall()
    con.close()
    return [r[0] for r in rows], [r[1] for r in rows]


def embed(texts: list[str], model_dir: Path, device: str, batch: int, cache: Path | None, label: str):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    if cache and cache.exists():
        print(f"  [{label}] cached", flush=True)
        return np.load(cache)

    tok = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModel.from_pretrained(str(model_dir), dtype=torch.float32).to(device).eval()
    out = []
    t0 = time.time()
    with torch.inference_mode():
        for i in range(0, len(texts), batch):
            chunk = texts[i : i + batch]
            enc = tok(chunk, padding=True, truncation=True, max_length=64, return_tensors="pt").to(device)
            hidden = model(**enc).last_hidden_state
            # CLS pooling, then L2 normalise so a dot product is a cosine.
            vec = hidden[:, 0]
            vec = torch.nn.functional.normalize(vec, p=2, dim=1)
            out.append(vec.cpu().numpy().astype("float32"))
            if i and i % (batch * 50) == 0:
                done = i + len(chunk)
                rate = done / (time.time() - t0)
                print(f"  [{label}] {done:,}/{len(texts):,}  {rate:.0f}/s", flush=True)
    arr = np.vstack(out)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, arr)
    return arr


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=Path, required=True, help="a terminology_* result file to reuse the terms from")
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--domain", default="Condition")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--ks", default="4,8,16,32,64")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    import numpy as np

    prior = json.loads(args.sample.read_text())
    terms = [{"text": r["text"], "true": r["true_concept_id"]} for r in prior["rows"]]
    print(f"{len(terms)} held-out terms reused from {args.sample.name}")

    ids, names = load_pool(args.vocab, args.domain)
    print(f"retrieval pool: {len(ids):,} standard {args.domain} concepts")

    pool_vecs = embed(names, args.model, args.device, args.batch, args.cache, "pool")
    query_vecs = embed([t["text"] for t in terms], args.model, args.device, args.batch, None, "query")

    ks = sorted(int(k) for k in args.ks.split(","))
    index = {cid: i for i, cid in enumerate(ids)}
    biggest = max(ks)
    scores = query_vecs @ pool_vecs.T
    top = np.argpartition(-scores, kth=biggest, axis=1)[:, :biggest]
    ordered = np.take_along_axis(top, np.argsort(-np.take_along_axis(scores, top, axis=1), axis=1), axis=1)

    rows = []
    for i, term in enumerate(terms):
        ranked = [ids[j] for j in ordered[i]]
        truth = term["true"]
        rank = ranked.index(truth) + 1 if truth in ranked else None
        rows.append({
            "text": term["text"], "true_concept_id": truth,
            "rank": rank, "in_pool": truth in index,
            "top1": ranked[0] if ranked else None,
            # The retrieved list itself, so a re-ranking arm can be run over exactly
            # these candidates rather than re-deriving them and drifting.
            "candidates": [{"concept_id": ids[j], "concept_name": names[j]} for j in ordered[i]],
        })

    n = len(rows)
    # A term whose answer is not a standard concept of this domain could never be
    # retrieved by anything; reported separately rather than counted as a miss.
    in_pool = [r for r in rows if r["in_pool"]]
    summary = {
        "model": args.model.name,
        "domain": args.domain,
        "pool_size": len(ids),
        "n_terms": n,
        "terms_whose_answer_is_in_the_pool": len(in_pool),
        "recall_at_k_pct": {
            k: round(100.0 * sum(1 for r in in_pool if r["rank"] and r["rank"] <= k) / len(in_pool), 1)
            for k in ks
        },
        "top1_pct": round(100.0 * sum(1 for r in in_pool if r["rank"] == 1) / len(in_pool), 1),
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(f"\npool {len(ids):,}  |  {len(in_pool)}/{n} answers are in it")
    for k in ks:
        print(f"  recall@{k:<3} {summary['recall_at_k_pct'][k]:>5}%")
    print(f"  top-1     {summary['top1_pct']:>5}%")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
