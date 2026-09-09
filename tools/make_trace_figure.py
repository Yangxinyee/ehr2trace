#!/usr/bin/env python3
"""Draw two subjects' published traces as a TikZ figure.

A trace here is what the canonical layer publishes for one subject: every event
it holds, in occurrence order, carrying both clocks, its action type, its quality
flags and its link back to the source rows it came from. The figure is therefore
a rendering of stored artifacts rather than a schematic -- the same stance the
validation contract takes.

What it shows, per subject:

  * each timed event at its occurrence time, and a bar running to its
    availability time where the two differ;
  * a decision time tau, placed inside the widest availability gap so that the
    admissibility rule has something to exclude: an event observed before tau
    that a history at tau still may not see;
  * medication actions typed by lifecycle, so an order and an administration are
    different marks rather than one "drug" mark;
  * death as a timed event rather than a static attribute;
  * one lineage callout, following a published event back to its source rows.

Subjects and tau are chosen by property, never by identifier: subject ids are
salted hashes and would differ between installations, so hard-coding them would
make the figure unreproducible. Run it against any converted work root:

    python3 tools/make_trace_figure.py --work <work>/generic_ehr --out trace.tex
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import polars as pl

# Lane order within a subject panel, top to bottom.
LANES = [
    ("observation", "Observations", {"measurement", "note"}),
    ("action", "Actions", {"drug_order", "drug_dispense", "drug_admin", "service_order", "procedure"}),
    ("outcome", "Outcome", {"death", "condition", "visit", "demographic"}),
]

MARK = {
    "measurement": "obs",
    "note": "obs",
    "drug_order": "order",
    "drug_dispense": "dispense",
    "drug_admin": "admin",
    "service_order": "order",
    "procedure": "proc",
    "death": "death",
    "visit": "visit",
    "condition": "cond",
}


def load(work: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    events = pl.read_parquet(work / "canonical" / "events.parquet")
    links = pl.read_parquet(work / "canonical" / "event_source.parquet")
    return events, links


def score(frame: pl.DataFrame) -> tuple[int, int, int, int]:
    """Rank a subject by how much of the contract its trace exercises."""
    lagged = frame.filter(
        pl.col("available_time").is_not_null()
        & pl.col("event_time").is_not_null()
        & (pl.col("available_time") > pl.col("event_time"))
    ).height
    kinds = set(frame["event_kind"].to_list())
    actions = int({"drug_order", "drug_admin"} <= kinds)
    outcome = int("death" in kinds)
    return (lagged, actions, outcome, frame.height)


def pick_subjects(events: pl.DataFrame, n: int) -> list[str]:
    timed = events.filter(pl.col("event_time").is_not_null())
    ranked = sorted(
        ((score(timed.filter(pl.col("subject_id") == s)), s)
         for s in timed["subject_id"].unique().to_list()),
        key=lambda pair: pair[0], reverse=True,
    )
    # Prefer a second subject that adds something the first did not, so the pair
    # covers more of the contract than the top two by raw score usually would.
    chosen = [ranked[0][1]]
    have = set(timed.filter(pl.col("subject_id") == chosen[0])["event_kind"].to_list())
    for sc, sid in ranked[1:]:
        if len(chosen) >= n:
            break
        kinds = set(timed.filter(pl.col("subject_id") == sid)["event_kind"].to_list())
        if kinds - have or len(ranked) <= n:
            chosen.append(sid)
            have |= kinds
    for sc, sid in ranked:
        if len(chosen) >= n:
            break
        if sid not in chosen:
            chosen.append(sid)
    return chosen[:n]


def decision_time(frame: pl.DataFrame) -> datetime | None:
    """Put tau inside the widest availability gap, so the rule has a case to make."""
    gaps = frame.filter(
        pl.col("available_time").is_not_null()
        & pl.col("event_time").is_not_null()
        & (pl.col("available_time") > pl.col("event_time"))
    )
    if not gaps.height:
        return None
    gaps = gaps.with_columns((pl.col("available_time") - pl.col("event_time")).alias("gap"))
    row = gaps.sort("gap", descending=True).row(0, named=True)
    return row["event_time"] + (row["available_time"] - row["event_time"]) / 2


def hours(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def label_for(row: dict) -> str:
    name = (row.get("source_name") or row.get("source_code") or row["event_kind"]) or ""
    name = str(name)
    for cut in (" (", ","):
        if cut in name:
            name = name.split(cut)[0]
    return name.strip()[:26]


def tex_escape(s: str) -> str:
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
                 ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"), ("~", r"\~{}"),
                 ("^", r"\^{}")):
        s = s.replace(a, b)
    return s


def build(events: pl.DataFrame, links: pl.DataFrame, subjects: list[str]) -> dict:
    panels = []
    for sid in subjects:
        frame = events.filter(
            (pl.col("subject_id") == sid) & pl.col("event_time").is_not_null()
        ).sort("event_time")

        # The panel is one encounter. Anchoring on the first event instead would put
        # a years-old prior diagnosis at the origin and compress the stay to nothing;
        # anchoring on the encounter is also how the paper's own cohort is defined.
        inside = frame.filter(pl.col("encounter_id").is_not_null())
        if not inside.height:
            inside = frame
        origin = inside["event_time"].min()
        last = max(inside["event_time"].max(),
                   inside["available_time"].drop_nulls().max() or inside["event_time"].max())
        span = max(hours(origin, last), 1.0)
        tau = decision_time(inside)

        marks, outside = [], []
        for row in frame.iter_rows(named=True):
            kind = row["event_kind"]
            at = hours(origin, row["event_time"])
            av = hours(origin, row["available_time"]) if row["available_time"] else at
            if row["encounter_id"] is None and not (0.0 <= at <= span):
                outside.append({"kind": kind, "at": round(at, 3), "label": label_for(row),
                                "side": "before" if at < 0 else "after"})
                continue
            lane = next((i for i, (_, _, ks) in enumerate(LANES) if kind in ks), 0)
            if tau is None:
                state = "in"
            elif row["event_time"] > tau:
                state = "future"
            elif (row["available_time"] or row["event_time"]) > tau:
                state = "withheld"       # observed by tau, not yet knowable at tau
            else:
                state = "in"
            marks.append({
                "lane": lane, "kind": kind, "mark": MARK.get(kind, "obs"),
                "at": round(at, 4), "available": round(av, 4), "lag": round(av - at, 4),
                "label": label_for(row), "state": state,
                "status": row.get("status_source"), "event_id": row["event_id"],
            })

        # Stack marks that share a lane and an instant, so a cluster is countable.
        seen: dict[tuple[int, float], int] = {}
        for m in marks:
            key = (m["lane"], round(m["at"], 2))
            m["stack"] = seen.get(key, 0)
            seen[key] = m["stack"] + 1

        panels.append({"span": round(span, 3),
                       "tau": round(hours(origin, tau), 4) if tau else None,
                       "marks": marks, "outside": outside,
                       "n_events": frame.height, "n_shown": len(marks)})

    counts = links.group_by("event_id").agg(pl.len().alias("n"),
                                            pl.col("relation").unique().alias("rels"))
    shown = {m["event_id"] for p in panels for m in p["marks"]}
    counts = counts.filter(pl.col("event_id").is_in(list(shown))).sort("n", descending=True)
    callout = None
    if counts.height:
        top = counts.row(0, named=True)
        owner = next(((pi, mi) for pi, p in enumerate(panels)
                      for mi, m in enumerate(p["marks"]) if m["event_id"] == top["event_id"]), None)
        if owner:
            callout = {"panel": owner[0], "mark": owner[1], "rows": int(top["n"]),
                       "relations": sorted(top["rels"])}
    return {"panels": panels, "callout": callout}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, required=True,
                    help="a converted work root, e.g. <work>/generic_ehr")
    ap.add_argument("--out", type=Path, required=True, help="TikZ fragment to write")
    ap.add_argument("--subjects", type=int, default=2)
    ap.add_argument("--json", type=Path, help="also write the extracted trace as JSON")
    args = ap.parse_args()

    events, links = load(args.work)
    chosen = pick_subjects(events, args.subjects)
    model = build(events, links, chosen)
    if args.json:
        args.json.write_text(json.dumps(model, indent=2, default=str) + "\n")
    args.out.write_text(render(model), encoding="utf-8")
    print(f"wrote {args.out} from {args.work}: "
          f"{len(model['panels'])} subjects, "
          f"{sum(len(p['marks']) for p in model['panels'])} events")
    return 0


LANE_Y = (6.8, 12.2, 17.6)
AX_X, AX_W, PANEL_H = 17.0, 68.0, 25.0
PANEL_GAP = 3.5
HEAD_SHARE = 0.72          # axis width given to the dense part of the encounter

STATE = {"in":       ("ehrblue",   "ehrblue",  "ehrblue!55"),
         "withheld": ("ehrorange", "white",    "ehrorange!65"),
         "future":   ("black!30",  "black!30", "black!18")}


def axis_map(panel: dict):
    """A focus-and-context time axis.

    An encounter's events are not spread evenly: the admission workup happens in
    the first hour and the rest of the stay is nearly empty, so a linear axis puts
    every mark that matters inside five per cent of the width. Where one gap holds
    more than a quarter of the span, the axis breaks there and gives most of its
    width to the dense head. The break is drawn, so the reader is told.
    """
    span = panel["span"]
    xs = sorted({m["at"] for m in panel["marks"]} | {m["available"] for m in panel["marks"]})
    gap_lo = gap_hi = None
    best = 0.0
    for a, b in zip(xs, xs[1:]):
        if b - a > best:
            best, gap_lo, gap_hi = b - a, a, b
    if best < 0.25 * span or gap_lo is None:
        return (lambda h: AX_X + h / span * AX_W), None, span
    head_end = gap_lo + 0.06 * span
    tail_start = gap_hi - 0.06 * span
    hw, tw = AX_W * HEAD_SHARE, AX_W * (1 - HEAD_SHARE)

    def fx(h: float) -> float:
        if h <= head_end:
            return AX_X + h / head_end * hw
        if h < tail_start:
            return AX_X + hw
        return AX_X + hw + (h - tail_start) / max(span - tail_start, 1e-6) * tw

    return fx, (AX_X + hw, head_end, tail_start), span


LABELLED = {"drug_order", "death", "visit"}


def render(model: dict) -> str:
    out = ["% Generated by tools/make_trace_figure.py from a converted work root.",
           "% Edit the generator, not this file.", "",
           r"\setlength{\figunit}{0.01\linewidth}%",
           r"\begin{tikzpicture}[figbase, x=\figunit, y=\figunit]"]

    for p_i, panel in enumerate(model["panels"]):
        top = -p_i * (PANEL_H + PANEL_GAP)
        fx, brk, span = axis_map(panel)
        out.append(f"% ------------------------------------------------- subject {p_i + 1}")
        out.append(f"\\node[minitext, anchor=north west] at (0,{top:.2f}) "
                   f"{{\\figstage{{ehrslate}}{{{'ab'[p_i]}}}\\ \\ Subject {p_i + 1}"
                   f"\\ \\ {{\\color{{black!45}}{panel['n_shown']} events in one encounter,"
                   f" {span:.0f}\\,h}}}};")

        y_top, y_bot = top - 3.6, top - 20.4
        if panel["tau"] is not None:
            tx = fx(panel["tau"])
            out.append(f"\\fill[black!4] ({tx:.2f},{y_top:.2f}) rectangle ({AX_X + AX_W:.2f},{y_bot:.2f});")
            out.append(f"\\draw[ehrorange, line width=0.7pt, dash pattern=on 1.1mm off 0.8mm] "
                       f"({tx:.2f},{y_top:.2f}) -- ({tx:.2f},{y_bot:.2f});")
            out.append(f"\\node[anchor=south, inner sep=0.2\\figunit] at ({tx:.2f},{y_top:.2f}) "
                       f"{{\\scriptsize\\color{{ehrorange}}$\\tau$}};")

        for l_i, (_, lane_name, _) in enumerate(LANES):
            ly = top - LANE_Y[l_i]
            out.append(f"\\node[minitext, anchor=east, text=black!55] at ({AX_X - 1.8:.2f},{ly:.2f}) "
                       f"{{{lane_name}}};")
            out.append(f"\\draw[black!11, line width=0.4pt] ({AX_X:.2f},{ly:.2f}) -- ({AX_X + AX_W:.2f},{ly:.2f});")

        # Stack a coincident cluster symmetrically about its lane, so it stays inside
        # the panel however many events share the instant.
        for m in panel["marks"]:
            k = m["stack"]
            dy = 0.0 if k == 0 else (1.85 * ((k + 1) // 2)) * (1 if k % 2 else -1)
            x, y = fx(m["at"]), top - LANE_Y[m["lane"]] + dy
            stroke, fill, bar = STATE[m["state"]]
            if m["lag"] > 0.01:
                xa = fx(m["available"])
                out.append(f"\\draw[{bar}, line width=1.4pt] ({x:.2f},{y:.2f}) -- ({xa:.2f},{y:.2f});")
                out.append(f"\\draw[{stroke}, line width=0.6pt, fill=white] ({xa:.2f},{y:.2f}) circle (0.78);")
            out.append(_glyph(m["mark"], x, y, stroke, fill))
            m["_xy"] = (x, y)

        # Label sparingly: an administration repeats its order's drug name, and an
        # unlabelled dot in a cluster is better than four labels on top of each other.
        for m in panel["marks"]:
            if not (m["state"] == "withheld" or m["kind"] in LABELLED):
                continue
            x, y = m["_xy"]
            col = "ehrorange" if m["state"] == "withheld" else "black!58"
            if m["kind"] == "visit":
                pos, dx, dy = "north west", -0.6, -1.3
            elif m["stack"] % 2:
                # Two labels in one cluster land on top of each other unless the
                # stack alternates which side of its mark each one takes.
                pos, dx, dy = "south west", 1.2, 0.9
            else:
                pos, dx, dy = "north west", 1.2, -0.9
            out.append(f"\\node[anchor={pos}, inner sep=0pt] at ({x + dx:.2f},{y + dy:.2f}) "
                       f"{{\\fontsize{{5.4}}{{6.2}}\\selectfont\\color{{{col}}}{tex_escape(m['label'])}}};")

        for o in panel["outside"]:
            if o["side"] == "after":
                out.append(f"\\node[anchor=west, inner sep=0pt] at ({AX_X + AX_W + 0.8:.2f},"
                           f"{top - LANE_Y[2]:.2f}) {{\\fontsize{{5.4}}{{6.2}}\\selectfont"
                           f"\\color{{ehrslate}}$\\rightarrow$ {tex_escape(o['label'])}"
                           f"\\,+{o['at']:.0f}\\,h}};")
                break

        ay = top - 21.6
        out.append(f"\\draw[black!45, line width=0.5pt] ({AX_X:.2f},{ay:.2f}) -- ({AX_X + AX_W:.2f},{ay:.2f});")
        if brk:
            bx = brk[0]
            out.append(f"\\draw[white, line width=1.6pt] ({bx - 0.7:.2f},{ay:.2f}) -- ({bx + 0.7:.2f},{ay:.2f});")
            for o in (-0.5, 0.4):
                out.append(f"\\draw[black!45, line width=0.5pt] ({bx + o - 0.5:.2f},{ay - 0.8:.2f}) -- "
                           f"({bx + o + 0.5:.2f},{ay + 0.8:.2f});")
        ticks = _ticks(span, brk)
        for h in ticks:
            tx = fx(h)
            out.append(f"\\draw[black!45, line width=0.5pt] ({tx:.2f},{ay:.2f}) -- ({tx:.2f},{ay - 0.9:.2f});")
            out.append(f"\\node[anchor=north, inner sep=0.3\\figunit] at ({tx:.2f},{ay - 0.9:.2f}) "
                       f"{{\\fontsize{{5.8}}{{7}}\\selectfont\\color{{black!60}}{h:g}}};")
        out.append(f"\\node[anchor=north east, inner sep=0pt] at ({AX_X + AX_W:.2f},{ay - 2.7:.2f}) "
                   f"{{\\fontsize{{5.8}}{{7}}\\selectfont\\color{{black!55}}hours from admission}};")
        out.append("")

    out.append(_legend(-(len(model['panels']) - 1) * (PANEL_H + PANEL_GAP) - 28.6))
    out.append(r"\end{tikzpicture}")
    return "\n".join(out) + "\n"


def _ticks(span: float, brk) -> list[float]:
    if not brk:
        step = _tick_step(span)
        n, ticks = 0, []
        while n * step <= span + 1e-6:
            ticks.append(n * step); n += 1
        return ticks
    _, head_end, tail_start = brk
    step = _tick_step(head_end)
    ticks, n = [], 0
    while n * step <= head_end + 1e-6:
        ticks.append(n * step); n += 1
    ticks = [h for h in ticks if h <= head_end - 0.25 * step] + [round(span)]
    return sorted(set(ticks))


def _tick_step(span: float) -> float:
    for s in (1, 2, 3, 6, 12, 24, 48, 96):
        if span / s <= 9:
            return float(s)
    return 168.0


def _legend(y: float) -> str:
    parts, x = [], 4.0
    for mark, text in (("obs", "observation"), ("order", "order"), ("admin", "administration")):
        parts.append(_glyph(mark, x, y, "ehrblue", "ehrblue"))
        parts.append(f"\\node[anchor=west, inner sep=0pt] at ({x + 1.4:.2f},{y:.2f}) "
                     f"{{\\fontsize{{6}}{{7}}\\selectfont\\color{{black!62}}{text}}};")
        x += 4.0 + 1.62 * len(text)
    parts.append(f"\\draw[ehrorange!65, line width=1.4pt] ({x:.2f},{y:.2f}) -- ({x + 3.2:.2f},{y:.2f});")
    parts.append(f"\\draw[ehrorange, line width=0.6pt, fill=white] ({x + 3.2:.2f},{y:.2f}) circle (0.78);")
    parts.append(f"\\node[anchor=west, inner sep=0pt] at ({x + 4.6:.2f},{y:.2f}) "
                 f"{{\\fontsize{{6}}{{7}}\\selectfont\\color{{ehrorange}}"
                 f"occurrence to availability}};")
    return "\n".join(parts)


def _glyph(mark: str, x: float, y: float, col: str, fill: str = "") -> str:
    fill = fill or col
    if mark == "order":
        return (f"\\draw[{col}, line width=0.6pt, fill=white] "
                f"({x:.2f},{y + 0.95:.2f}) -- ({x + 0.95:.2f},{y:.2f}) -- "
                f"({x:.2f},{y - 0.95:.2f}) -- ({x - 0.95:.2f},{y:.2f}) -- cycle;")
    if mark == "admin":
        return (f"\\fill[{fill}] ({x - 0.9:.2f},{y - 0.8:.2f}) -- ({x + 0.9:.2f},{y - 0.8:.2f}) -- "
                f"({x:.2f},{y + 1.0:.2f}) -- cycle;")
    if mark == "death":
        return (f"\\draw[{col}, line width=0.9pt] ({x - 0.85:.2f},{y - 0.85:.2f}) -- "
                f"({x + 0.85:.2f},{y + 0.85:.2f}) ({x - 0.85:.2f},{y + 0.85:.2f}) -- "
                f"({x + 0.85:.2f},{y - 0.85:.2f});")
    if mark == "visit":
        return f"\\draw[{col}, line width=0.9pt] ({x:.2f},{y - 1.1:.2f}) -- ({x:.2f},{y + 1.1:.2f});"
    return f"\\draw[{col}, line width=0.6pt, fill={fill}] ({x:.2f},{y:.2f}) circle (0.72);"


if __name__ == "__main__":
    raise SystemExit(main())
