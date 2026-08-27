"""Render a retrieval/ranking matrix as the LaTeX table the paper includes.

Generated rather than transcribed, so the table cannot drift from the runs that
produced it.

Two tasks share the layout. `condition` hides an ICD-10-CM code and hands over its
description; `measurement` hides a LOINC concept and hands over one of its synonyms,
which is the terse, abbreviated shape a hospital's own laboratory labels have. The
second exists to test whether the first one's conclusion is a property of the method or
of English diagnosis phrases.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TASKS = {
    "condition": {
        "lexical": "terminology_lexical_k{k}.json",
        "dense": "retrieval_bge-m3_condition.json",
        "rankers": (
            ("terminology_medgemma_k{k}.json", "MedGemma-27B", "27B"),
            ("terminology_qwen3-32b_k{k}.json", "Qwen3-32B", "32B"),
        ),
        "compose": "compose_dense_medgemma_k{k}.json",
    },
    "measurement": {
        "lexical": "loinc_lexical_k{k}.json",
        "dense": "retrieval_bge-m3_measurement.json",
        "rankers": (("loinc_medgemma_k{k}.json", "MedGemma-27B", "27B"),),
        "compose": "loinc_compose_dense_medgemma_k{k}.json",
    },
}


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--task", choices=sorted(TASKS), default="condition")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    r, k, spec = args.results, args.k, TASKS[args.task]

    arms = []
    lex = load(r / spec["lexical"].format(k=k))
    if lex:
        arms.append(("lexical", "lexical score", lex["recall_at_k_pct"],
                     lex["lexical_top1_pct_when_recalled"], lex["lexical_top1_pct_overall"], None))
    for pattern, name, params in spec["rankers"]:
        d = load(r / pattern.format(k=k))
        if d:
            arms.append(("lexical", name, d["recall_at_k_pct"],
                         d["model_top1_pct_when_recalled"], d["model_top1_pct_overall"], params))
    dense = load(r / spec["dense"])
    if dense:
        recall = dense["recall_at_k_pct"][str(k)]
        # top-1 over the recalled subset, for comparability with the ranking arms
        of_recalled = round(100.0 * dense["top1_pct"] / recall, 1) if recall else None
        arms.append(("dense", "vector similarity", recall, of_recalled, dense["top1_pct"], "568M"))
    comp = load(r / spec["compose"].format(k=k))
    if comp:
        arms.append(("dense", "MedGemma-27B", comp["recall_at_k_pct"],
                     comp["model_top1_pct_when_recalled"], comp["model_top1_pct_overall"], "27B"))

    if not arms:
        raise SystemExit(f"no result files for task {args.task} at k={k}")

    lines = [
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"\textbf{Retrieval} & \textbf{Ranking} & \textbf{params} & \textbf{recall@" + str(k)
        + r"} & \multicolumn{2}{c}{\textbf{top-1 accuracy}} \\",
        r"\cmidrule(lr){5-6}",
        r" & & & & of recalled & overall \\",
        r"\midrule",
    ]
    best = max(a[4] for a in arms if a[4] is not None)
    for retrieval, ranking, recall, of_rec, overall, params in arms:
        cell = f"{overall:.1f}\\%"
        if overall == best:
            cell = f"\\textbf{{{cell}}}"
        lines.append(
            f"{retrieval} & {ranking} & {params or '---'} & {recall:.1f}\\% & "
            f"{of_rec:.1f}\\% & {cell} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}"]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
