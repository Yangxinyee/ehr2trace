"""A held-out concept-selection set for the case the paper is actually about.

The ICD experiment (`measure_terminology_llm.py`) measures concept selection on
diagnosis descriptions, which are full English phrases. The strings that actually need
help in a hospital export are laboratory names, and those are terse and abbreviated --
`HGB`, `NA`, `PLT`. Whether the ICD conclusion transfers to that shape of string is a
different question, and it needs an answer key.

MIMIC-IV cannot supply one: its laboratory events are coded by `itemid`, and version 3.1
dropped the `loinc_code` column from `d_labitems`, so nothing in the release states which
standard concept a lab label means. The FHIR demo codes the same items against a MIMIC
code system rather than LOINC.

The vocabulary supplies one. A LOINC concept carries synonyms, and among them are the
LOINC short names -- `C4 NeF SerPl Ql`, `Psychosine RBC-sCnt` -- which are exactly the
abbreviated, punctuation-dense shape a local lab label has. Hand over the synonym alone
and the task is the one a mapper faces: given a terse string, pick the concept. The
vocabulary's own synonym table is the key.

Two rules keep the set honest:

  * No selection on difficulty. Every ASCII synonym that differs from its concept's name
    is eligible; the sample is drawn from all of them, and the length distribution is
    reported rather than filtered. Picking only the short ones would manufacture the
    result.
  * The answer must be unique. A synonym string that belongs to more than one standard
    Measurement concept has no single correct answer, so it is excluded -- otherwise a
    correct pick could be scored wrong.

Usage::

    python tools/make_loinc_synonym_terms.py --vocab /path/to/vocab \\
        --n 300 --out results/loinc_synonym_terms.json
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

CSV_OPTIONS = "delim='\\t', header=true, all_varchar=true, quote=''"


def build(vocab_dir: Path, domain: str, vocabulary_id: str) -> list[dict]:
    import duckdb

    con = duckdb.connect()
    rows = con.execute(
        f"""
        WITH concept AS (
            SELECT CAST(concept_id AS BIGINT) AS concept_id, concept_name
            FROM read_csv('{vocab_dir / "CONCEPT.csv"}', {CSV_OPTIONS})
            WHERE vocabulary_id = '{vocabulary_id}'
              AND standard_concept = 'S'
              AND domain_id = '{domain}'
              AND (invalid_reason IS NULL OR invalid_reason = '')
        ),
        synonym AS (
            SELECT CAST(concept_id AS BIGINT) AS concept_id, concept_synonym_name AS text
            FROM read_csv('{vocab_dir / "CONCEPT_SYNONYM.csv"}', {CSV_OPTIONS})
        ),
        pair AS (
            SELECT s.text, c.concept_id, c.concept_name
            FROM synonym s JOIN concept c USING (concept_id)
            WHERE lower(s.text) <> lower(c.concept_name)
              -- The vocabulary carries translations. A Chinese rendering of a LOINC term
              -- is a different task from the one being measured.
              AND s.text = regexp_replace(s.text, '[^\\x20-\\x7E]', '', 'g')
              AND length(s.text) > 2
        ),
        unique_answer AS (
            SELECT lower(text) AS key
            FROM pair GROUP BY 1 HAVING count(DISTINCT concept_id) = 1
        )
        SELECT p.text, p.concept_id, p.concept_name
        FROM pair p JOIN unique_answer u ON lower(p.text) = u.key
        ORDER BY p.concept_id, p.text
        """
    ).fetchall()
    con.close()
    return [
        {"source_code": "", "text": t, "true_concept_id": cid, "true_concept_name": name}
        for t, cid, name in rows
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--domain", default="Measurement")
    ap.add_argument("--vocabulary-id", default="LOINC")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    pool = build(args.vocab, args.domain, args.vocabulary_id)
    if not pool:
        raise SystemExit(f"no {args.vocabulary_id}/{args.domain} synonyms with a unique answer")
    # Sorted by the query above before it is shuffled, for the same reason the ICD
    # experiment sorts: a seeded shuffle over an engine-ordered list is not reproducible.
    random.Random(args.seed).shuffle(pool)
    sample = pool[: args.n]

    lengths = [len(r["text"]) for r in sample]
    summary = {
        "vocabulary_id": args.vocabulary_id,
        "domain": args.domain,
        "eligible_synonyms": len(pool),
        "eligible_concepts": len({r["true_concept_id"] for r in pool}),
        "n_terms": len(sample),
        "seed": args.seed,
        "query_length_chars": {
            "min": min(lengths), "median": sorted(lengths)[len(lengths) // 2], "max": max(lengths),
            "at_most_24": sum(1 for n in lengths if n <= 24),
        },
        "answer_length_chars_median": sorted(len(r["true_concept_name"]) for r in sample)[len(sample) // 2],
        "duplicate_texts": sum(c for c in Counter(r["text"] for r in sample).values() if c > 1),
        "rows": sample,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(f"{len(pool):,} eligible synonyms over {summary['eligible_concepts']:,} concepts")
    print(f"sampled {len(sample)}; query length median {summary['query_length_chars']['median']} chars, "
          f"{summary['query_length_chars']['at_most_24']} are 24 chars or shorter")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
