"""Render the retrieval/ranking matrix as the LaTeX table the paper includes.

Generated rather than transcribed, so the table cannot drift from the runs that
produced it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--k", type=int, default=8)
    args = ap.parse_args()
    r, k = args.results, args.k

    lex = load(r / f"terminology_lexical_k{k}.json")
    dense = load(r / "retrieval_bge-m3_condition.json")
    arms = []

    if lex:
        arms.append(("lexical", "lexical score", lex["recall_at_k_pct"],
                     lex["lexical_top1_pct_when_recalled"], lex["lexical_top1_pct_overall"], None))
    for tag, name, params in (
        ("terminology_medgemma", "MedGemma-27B", "27B"),
        ("terminology_qwen3-32b", "Qwen3-32B", "32B"),
    ):
        d = load(r / f"{tag}_k{k}.json")
        if d:
            arms.append(("lexical", f"{name}", d["recall_at_k_pct"],
                         d["model_top1_pct_when_recalled"], d["model_top1_pct_overall"], params))
    if dense:
        recall = dense["recall_at_k_pct"][str(k)]
        # top-1 over the recalled subset, for comparability with the ranking arms
        of_recalled = round(100.0 * dense["top1_pct"] / recall, 1) if recall else None
        arms.append(("dense", "vector similarity", recall, of_recalled, dense["top1_pct"], "568M"))
    comp = load(r / f"compose_dense_medgemma_k{k}.json")
    if comp:
        arms.append(("dense", "MedGemma-27B", comp["recall_at_k_pct"],
                     comp["model_top1_pct_when_recalled"], comp["model_top1_pct_overall"], "27B"))

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
