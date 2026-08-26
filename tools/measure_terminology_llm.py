"""Does a language model improve concept selection, measured where truth is known?

The column-semantics measurement (`ehr2cdm measure`) has an answer key: the field roles
declared in the dataset config. Terminology has no such key for the terms that actually
need help -- a hospital's local laboratory names belong to no standard vocabulary, which
is precisely why they are hard, and adjudicating them would take a clinician.

So we measure the same task where the answer *is* known. A diagnosis code carries a
description, and the vocabulary states which standard concept that code maps to. Hide
the code, hand over the description alone, and the task becomes exactly the one the
model is asked to do on a local lab name: given a string and a set of lexically recalled
candidates, pick the right concept. The vocabulary's own `Maps to` is the answer key.

Three numbers are reported, and the first bounds the other two:

  recall@k   is the true concept among the candidates at all? Neither arm can win
             outside this set, so accuracy is reported over it.
  lexical    top-1 by recall score. The baseline the model has to beat.
  model      the model's first choice among the same candidates.

The model is given the identical candidate list, so this isolates ranking. It cannot
introduce a concept from memory: `rank_candidates` rejects any id that was not supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl  # noqa: E402

from ehr2cdm.config import load_dataset_config  # noqa: E402
from ehr2cdm.llm import LlmClient  # noqa: E402
from ehr2cdm.paths import WorkLayout  # noqa: E402
from ehr2cdm.terminology import DOMAIN_FOR_KIND, MappingRegistry, Vocabulary, resolve_terms_batch  # noqa: E402
from ehr2cdm.terminology import TermRequest  # noqa: E402


def truth_set(events: pl.DataFrame, vocabulary, code_system: str, event_kind: str) -> list[dict]:
    """Terms whose standard concept the vocabulary already states, with their text."""
    frame = (
        events.filter((pl.col("code_system") == code_system) & (pl.col("event_kind") == event_kind))
        .select("source_code", "source_name")
        .drop_nulls()
        .filter(pl.col("source_name").str.len_chars() > 3)
        .unique(subset=["source_code"])
    )
    requests = [
        TermRequest(
            code_system=code_system,
            source_code=r["source_code"],
            source_name=r["source_name"],
            event_kind=event_kind,
            occurrences=1,
        )
        for r in frame.iter_rows(named=True)
    ]
    resolved, _unresolved = resolve_terms_batch(requests, vocabulary, MappingRegistry())
    # Resolution keys are normalized (lower-cased, whitespace-collapsed), so the map back
    # to the original text has to be keyed the same way rather than by the raw code.
    by_key = {r.key: r for r in requests}
    items = [
        {
            "source_code": by_key[key].source_code,
            "text": by_key[key].source_name,
            "true_concept_id": match.concept_id,
            "true_concept_name": match.concept_name,
        }
        for key, match in resolved.items()
        if key in by_key and match.concept_id and by_key[key].source_name
    ]
    # Sorted before it is sampled. The resolution step returns rows in whatever order
    # the query engine produced them, which is not stable across runs, so seeding the
    # shuffle is not enough on its own -- the first version of this script drew a
    # different sample every time and quietly reported different numbers for the same
    # seed.
    items.sort(key=lambda r: r["source_code"])
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--built", type=Path, required=True)
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--code-system", default="ICD10CM")
    ap.add_argument("--event-kind", default="condition")
    ap.add_argument("--n", type=int, default=300, help="how many terms to evaluate")
    ap.add_argument("--k", type=int, default=8, help="candidates recalled per term")
    ap.add_argument("--seed", type=int, default=20260823)
    ap.add_argument("--no-llm", action="store_true", help="measure recall only")
    ap.add_argument(
        "--candidates-from",
        type=Path,
        default=None,
        help="a measure_retrieval.py result file. Rank the candidates it retrieved "
        "instead of lexically recalled ones, over the identical terms, so retrieval and "
        "ranking can be varied one at a time.",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    layout = WorkLayout(root=args.built, dataset_id=cfg.dataset_id)
    events = pl.read_parquet(layout.canonical_path("events"))
    vocabulary = Vocabulary.open(args.vocab)

    domain = DOMAIN_FOR_KIND.get(args.event_kind)
    supplied: dict[str, list] = {}
    if args.candidates_from:
        prior = json.loads(args.candidates_from.read_text())
        sample = [
            {"source_code": "", "text": r["text"], "true_concept_id": r["true_concept_id"],
             "true_concept_name": ""}
            for r in prior["rows"]
        ]
        supplied = {r["text"]: r.get("candidates", []) for r in prior["rows"]}
        print(f"{len(sample)} terms and their candidates reused from {args.candidates_from.name}")
    else:
        pool = truth_set(events, vocabulary, args.code_system, args.event_kind)
        if not pool:
            raise SystemExit(f"no {args.code_system}/{args.event_kind} terms resolve; nothing to measure against")
        random.Random(args.seed).shuffle(pool)
        sample = pool[: args.n]

    client = None
    if not args.no_llm:
        client = LlmClient.from_env()
        if not client.probe():
            raise SystemExit("the endpoint could not be probed for schema-constrained output")

    rows: list[dict] = []
    t0 = time.time()
    for i, item in enumerate(sample, 1):
        if supplied:
            from ehr2cdm.terminology import Candidate

            candidates = [
                Candidate(concept_id=c["concept_id"], concept_name=c["concept_name"],
                          domain_id=domain or "", vocabulary_id="", score=0.0)
                for c in supplied.get(item["text"], [])[: args.k]
            ]
        else:
            candidates = vocabulary.candidates(item["text"], domain, limit=args.k)
        ids = [c.concept_id for c in candidates]
        in_candidates = item["true_concept_id"] in ids
        lexical_pick = ids[0] if ids else None

        model_pick, rationale, failed = None, "", False
        if client is not None and candidates:
            ranking = client.rank_candidates(item["text"], domain, candidates)
            if ranking is None:
                failed = True
            else:
                ordered = sorted(ranking.ranking, key=lambda c: c.rank)
                model_pick = ordered[0].concept_id if ordered else None
                rationale = ranking.rationale

        rows.append(
            {
                "source_code": item["source_code"],
                "text": item["text"],
                "true_concept_id": item["true_concept_id"],
                "true_concept_name": item["true_concept_name"],
                "n_candidates": len(candidates),
                "true_in_candidates": in_candidates,
                "lexical_pick": lexical_pick,
                "lexical_correct": lexical_pick == item["true_concept_id"],
                "model_pick": model_pick,
                "model_correct": model_pick == item["true_concept_id"],
                "model_failed": failed,
                "model_rationale": rationale[:200],
            }
        )
        if i % 25 == 0:
            print(f"  {i}/{len(sample)}  ({time.time() - t0:.0f}s)", flush=True)

    vocabulary.close()

    n = len(rows)
    recalled = [r for r in rows if r["true_in_candidates"]]
    def rate(subset, key):
        return round(100.0 * sum(1 for r in subset if r[key]) / len(subset), 1) if subset else None

    summary = {
        "dataset": cfg.dataset_id,
        "code_system": args.code_system,
        "candidate_source": args.candidates_from.name if args.candidates_from else "lexical",
        "event_kind": args.event_kind,
        "domain": domain,
        "model": os.environ.get("LLM_MODEL", "") if client else None,
        "n_terms": n,
        "k": args.k,
        "seed": args.seed,
        # The ceiling. Outside this set the answer was never on the table, so neither
        # arm could have picked it and reporting accuracy over everything would
        # understate both equally and hide the real bottleneck.
        "recall_at_k_pct": round(100.0 * len(recalled) / n, 1) if n else None,
        "lexical_top1_pct_overall": rate(rows, "lexical_correct"),
        "model_top1_pct_overall": rate(rows, "model_correct") if client else None,
        "lexical_top1_pct_when_recalled": rate(recalled, "lexical_correct"),
        "model_top1_pct_when_recalled": rate(recalled, "model_correct") if client else None,
        "model_failures": sum(1 for r in rows if r["model_failed"]),
        "candidate_count_distribution": dict(Counter(r["n_candidates"] for r in rows)),
        "seconds": round(time.time() - t0, 1),
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))

    print(f"\n{n} terms, k={args.k}")
    print(f"  recall@{args.k}          {summary['recall_at_k_pct']}%   (the ceiling)")
    print(f"  lexical top-1        {summary['lexical_top1_pct_when_recalled']}%  of recalled"
          f"   ({summary['lexical_top1_pct_overall']}% overall)")
    if client:
        print(f"  model top-1          {summary['model_top1_pct_when_recalled']}%  of recalled"
              f"   ({summary['model_top1_pct_overall']}% overall)")
        print(f"  model failures       {summary['model_failures']}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
