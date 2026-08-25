"""Render the terminology-ranking results as the LaTeX table the paper includes.

Generated rather than transcribed, so the table cannot drift from the runs.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path


def load(pattern: str) -> dict[int, dict]:
    out = {}
    for path in glob.glob(pattern):
        d = json.loads(Path(path).read_text())
        out[d["k"]] = d
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    lexical = load(f"{args.results}/terminology_lexical_k*.json")
    model = load(f"{args.results}/terminology_medgemma_k*.json")

    lines = [
        r"\begin{tabular}{rrrrrr}",
        r"\toprule",
        r" & & \multicolumn{2}{c}{\textbf{lexical top-1}} & \multicolumn{2}{c}{\textbf{model top-1}} \\",
        r"\cmidrule(lr){3-4}\cmidrule(lr){5-6}",
        r"$k$ & \textbf{recall@$k$} & of recalled & overall & of recalled & overall \\",
        r"\midrule",
    ]
    for k in sorted(lexical):
        d = lexical[k]
        m = model.get(k)
        recall = f"{d['recall_at_k_pct']:.1f}\\%"
        lex_r = f"{d['lexical_top1_pct_when_recalled']:.1f}\\%"
        lex_o = f"{d['lexical_top1_pct_overall']:.1f}\\%"
        if m:
            mdl_r = f"\\textbf{{{m['model_top1_pct_when_recalled']:.1f}\\%}}"
            mdl_o = f"\\textbf{{{m['model_top1_pct_overall']:.1f}\\%}}"
        else:
            mdl_r = mdl_o = "---"
        lines.append(f"{k} & {recall} & {lex_r} & {lex_o} & {mdl_r} & {mdl_o} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
