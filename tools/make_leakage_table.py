"""Render the downstream leakage measurement as the LaTeX table the paper includes.

Generated rather than transcribed, so the table cannot drift from the run that produced
it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HEADINGS = {
    "respects_availability": r"\textbf{respects availability}",
    "ignores_availability": "ignores availability",
    "diagnoses_at_admission": "diagnoses at admission",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    doc = json.loads(args.result.read_text())
    arms = [a["arm"] for a in doc["horizons"][0]["arms"]]

    lines = [
        r"\begin{tabular}{l" + "r" * len(arms) + r"}",
        r"\toprule",
        r"\textbf{Prediction point} & " + " & ".join(HEADINGS.get(a, a) for a in arms) + r" \\",
        r"\midrule",
    ]
    for h in doc["horizons"]:
        by = {a["arm"]: a for a in h["arms"]}
        cells = []
        for arm in arms:
            value = f"{by[arm]['held_out_auroc']:.3f}"
            if arm != "respects_availability":
                value += f" \\ {{\\footnotesize ($+{h['auroc_inflation'][arm]:.3f}$)}}"
            cells.append(value)
        lines.append(f"{h['horizon_hours']}\\,h after admission & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
