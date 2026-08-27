"""Render the per-stage cost measurement as the LaTeX table the paper includes.

Generated rather than transcribed, so the table cannot drift from the runs that produced
it. Rows come from two places and the table says which: the stages that write a run
report carry their own wall time and peak memory, and the rest were timed by rebuilding
them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ORDER = ("ingest", "identity", "canonical", "omop", "meds")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--result", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    doc = json.loads(args.result.read_text())
    rows: dict[str, dict] = {}
    # A stage that ran more than once keeps its last run: the earlier ones are the
    # attempts that were killed, and reporting the fastest of those would be dishonest.
    for row in doc["recorded"] + doc["measured"]:
        if row.get("wall_seconds") is not None:
            rows[row["stage"]] = row

    lines = [
        r"\begin{tabular}{lrrl}",
        r"\toprule",
        r"\textbf{Stage} & \textbf{wall time} & \textbf{peak memory} & \textbf{timing from} \\",
        r"\midrule",
    ]
    total = 0.0
    for stage in ORDER:
        row = rows.get(stage)
        if not row:
            continue
        seconds = float(row["wall_seconds"])
        total += seconds
        clock = f"{seconds / 60:.0f}\\,min" if seconds >= 90 else f"{seconds:.0f}\\,s"
        memory = f"{row['peak_rss_gb']:.1f}\\,GB" if row.get("peak_rss_gb") else "---"
        lines.append(f"{stage} & {clock} & {memory} & {row['source']} \\\\")
    lines += [
        r"\midrule",
        f"total & {total / 3600:.1f}\\,h & & \\\\",
        r"\bottomrule",
        r"\end{tabular}",
    ]
    args.out.write_text("\n".join(lines) + "\n")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
