#!/usr/bin/env python3
"""Print two subjects' published traces as a LaTeX table.

A trace here is what the canonical layer publishes for one subject: every event
it holds for one encounter, in occurrence order, carrying both clocks, its action
type, the detail an action needs to be reconstructed, and the quality flags that
travel with it. The table is the stored rows, not a drawing of them, which is
also why it shows the column names.

Reading down one subject's block answers, without prose: whether a fact was
knowable at a decision time (both clocks against tau), whether an order was
executed (a drug_order row with no drug_admin beside it was not), what an
administration actually delivered (dose and route), and what the converter
thought was wrong with a value (the flags).

Subjects and tau are chosen by property, never by identifier: subject ids are
salted hashes and would differ between installations. Run it against any
converted work root:

    python3 tools/make_trace_table.py --work <work>/generic_ehr --out trace.tex
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import polars as pl

# Lane order within a subject panel, top to bottom.
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


def pick_encounter(frame: pl.DataFrame) -> str | None:
    """The subject's encounter that exercises most of the contract.

    Ranked the same way a subject is: an encounter that shows a laboratory result
    arriving after its specimen, an order and the administration it led to, and a
    manageable number of rows says more than the subject's longest stay.
    """
    ids = frame.filter(pl.col("encounter_id").is_not_null())["encounter_id"].unique().to_list()
    if not ids:
        return None
    ranked = sorted(
        ((score(frame.filter(pl.col("encounter_id") == e)), e) for e in ids),
        key=lambda pair: (pair[0][0] > 0, pair[0][1], pair[0][2], -pair[0][3]), reverse=True,
    )
    return ranked[0][1]


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
    return str(name).strip()[:42]


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

        # The panel is one encounter, and on a real export that means one encounter
        # id rather than every timed event that has one. A MIMIC subject carries
        # several admissions over years: scoping to "has an encounter" put a stay
        # three years later in the same window and left tau fifteen thousand hours
        # from the origin.
        encounter = pick_encounter(frame)
        inside = (frame.filter(pl.col("encounter_id") == encounter)
                  if encounter is not None else frame)
        if not inside.height:
            inside = frame
        origin = inside["event_time"].min()
        last = max(inside["event_time"].max(),
                   inside["available_time"].drop_nulls().max() or inside["event_time"].max())
        span = max(hours(origin, last), 1.0)
        tau = decision_time(inside)

        rows, outside = [], []
        for row in frame.iter_rows(named=True):
            at = hours(origin, row["event_time"])
            av = hours(origin, row["available_time"]) if row["available_time"] else at
            if row["encounter_id"] != encounter:
                # Only the outcome is worth reporting from beyond the window, and on a
                # real subject the rest runs to thousands of rows.
                if row["event_kind"] == "death":
                    outside.append({"kind": row["event_kind"], "at": round(at, 3),
                                    "label": label_for(row),
                                    "side": "before" if at < 0 else "after"})
                continue
            if tau is None:
                state = "in"
            elif row["event_time"] > tau:
                state = "future"
            elif (row["available_time"] or row["event_time"]) > tau:
                state = "withheld"       # observed by tau, not yet knowable at tau
            else:
                state = "in"
            # What an action delivered and what the converter thought was wrong with
            # the value both live on the row; a trace that hid them would be a
            # drawing of the schema rather than the schema.
            # Escape each part before joining: the separator is LaTeX, the parts are
            # stored strings, and flag names carry underscores.
            detail = r" \textperiodcentered\ ".join(
                tex_escape(str(x)) for x in (
                    row.get("dose_source"), row.get("route_source"), row.get("status_source"),
                    ", ".join(f for f in (row.get("quality_flags") or []) if f) or None,
                ) if x)
            rows.append({"kind": row["event_kind"], "at": round(at, 4),
                         "available": round(av, 4), "lag": round(av - at, 4),
                         "name": label_for(row), "detail": detail, "state": state,
                         "event_id": row["event_id"]})

        panels.append({"span": round(span, 3),
                       "tau": round(hours(origin, tau), 4) if tau else None,
                       "rows": rows, "outside": outside,
                       "n_events": frame.height, "n_shown": len(rows)})

    counts = links.group_by("event_id").agg(pl.len().alias("n"),
                                            pl.col("relation").unique().alias("rels"))
    shown = {m["event_id"] for p in panels for m in p["rows"]}
    counts = counts.filter(pl.col("event_id").is_in(list(shown))).sort("n", descending=True)
    callout = None
    if counts.height:
        top = counts.row(0, named=True)
        owner = next(((pi, mi) for pi, p in enumerate(panels)
                      for mi, m in enumerate(p["rows"]) if m["event_id"] == top["event_id"]), None)
        if owner:
            callout = {"panel": owner[0], "mark": owner[1], "rows": int(top["n"]),
                       "relations": sorted(top["rels"])}
    return {"panels": panels, "callout": callout}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", type=Path, required=True,
                    help="a converted work root, e.g. <work>/generic_ehr")
    ap.add_argument("--out", type=Path, required=True, help="LaTeX tabular to write")
    ap.add_argument("--subjects", type=int, default=2)
    ap.add_argument("--max-rows", type=int, default=7,
                    help="rows shown per subject; the remainder is elided, and said to be")
    ap.add_argument("--json", type=Path, help="also write the extracted trace as JSON")
    args = ap.parse_args()

    events, links = load(args.work)
    chosen = pick_subjects(events, args.subjects)
    model = build(events, links, chosen)
    elide(model, args.max_rows)
    if args.json:
        args.json.write_text(json.dumps(model, indent=2, default=str) + "\n")
    args.out.write_text(render(model), encoding="utf-8")
    print(f"wrote {args.out} from {args.work}: {len(model['panels'])} subjects, "
          f"{sum(len(p['rows']) for p in model['panels'])} rows")
    return 0


def elide(model: dict, cap: int) -> None:
    """Drop filler, keep the argument, and say how much was dropped.

    Taking the first `cap` rows in time order loses the medication stages, which
    happen after the admission workup and are most of what a trace is for. Rows
    are ranked by what they carry instead, then put back in occurrence order:
    anything withheld at tau or whose two clocks disagree, every medication stage
    and death, the encounter anchor, then whatever else fits. Trailing routine
    events can go, but silently dropping them would misrepresent the table as the
    whole encounter, so the count stays.
    """
    for panel in model["panels"]:
        rows = panel["rows"]
        if len(rows) <= cap:
            continue

        # Only the first visit is the anchor. MIMIC types every transfer and service
        # change as a visit too, and ranking all of them first filled the table with
        # ward moves and left out the laboratory results the argument rests on.
        anchor = next((id(r) for r in rows if r["kind"] == "visit"), None)

        def rank(r: dict) -> int:
            if id(r) == anchor:
                return 0        # the anchor "hours from admission" is measured from
            if r["state"] == "withheld":
                return 1        # the case the decision rule exists to make
            if r["kind"].startswith("drug_") or r["kind"] == "death":
                return 2
            if r["lag"] > 0.005:
                return 3
            if any(f in (r["detail"] or "")
                   for f in ("NON_NUMERIC", "COMPARATOR", "RANGE_VALUE")):
                return 4
            return 5

        # Rank alone fills the table with one kind: a real encounter administers six
        # drugs in the same minute, and seven rows of drug_admin say once what one row
        # says. Take at most two of a kind per pass, then come round again.
        order = {id(r): i for i, r in enumerate(rows)}
        pool = sorted(rows, key=lambda r: (rank(r), order[id(r)]))
        keep: list[dict] = []
        while pool and len(keep) < cap:
            seen: dict[str, int] = {}
            leftover = []
            for r in pool:
                if len(keep) < cap and seen.get(r["kind"], 0) < 2:
                    keep.append(r)
                    seen[r["kind"]] = seen.get(r["kind"], 0) + 1
                else:
                    leftover.append(r)
            if len(leftover) == len(pool):
                break
            pool = leftover
        panel["rows"] = sorted(keep, key=lambda r: order[id(r)])
        panel["elided"] = len(rows) - len(keep)


VERDICT = {"in": r"\checkmark", "withheld": r"\textbf{withheld}", "future": r"---"}


def render(model: dict) -> str:
    out = ["% Generated by tools/make_trace_table.py from a converted work root.",
           "% Edit the generator, not this file.",
           r"\begin{tabularx}{\linewidth}{@{}l r r Y l@{}}",
           r"\toprule",
           r"\texttt{event\_kind} & \multicolumn{2}{c}{hours from admission} &"
           r" \texttt{source\_name}, and the detail an action carries & at $\tau$ \\",
           r"\cmidrule(lr){2-3}",
           r" & \texttt{event} & \texttt{avail.} & & \\",
           r"\midrule"]
    for i, panel in enumerate(model["panels"]):
        if i:
            out.append(r"\midrule")
        out.append(rf"\multicolumn{{5}}{{@{{}}l}}{{\itshape Subject {i + 1}, one encounter of "
                   rf"{panel['span']:.0f}\,h; decision time $\tau$ at +{panel['tau']:.2f}\,h}} \\[1pt]")
        for r in panel["rows"]:
            detail = r["detail"]
            body = tex_escape(r["name"])
            if detail:
                body += rf" {{\scriptsize\color{{black!55}}{detail}}}"
            avail = f"{r['available']:.2f}" if r["lag"] > 0.005 else r"\textperiodcentered"
            kind = rf"\texttt{{{tex_escape(r['kind'])}}}"
            if r["state"] == "withheld":
                kind = rf"\color{{ehrorange}}{kind}"
            out.append(rf"{kind} & {r['at']:.2f} & {avail} & {body} & {VERDICT[r['state']]} \\")
        if panel.get("elided"):
            out.append(rf"\multicolumn{{5}}{{@{{}}l}}{{\quad{{\scriptsize\color{{black!55}}"
                       rf"{panel['elided']} further events in this encounter, all after $\tau$}}}} \\")
        for o in panel["outside"]:
            if o["side"] == "after":
                out.append(rf"\texttt{{{tex_escape(o['kind'])}}} & \multicolumn{{2}}{{r}}{{+{o['at']:.0f}}} & "
                           rf"{tex_escape(o['label'])} \ {{\scriptsize\color{{black!55}}"
                           rf"outside the encounter window}} & --- \\")
                break
    out += [r"\bottomrule", r"\end{tabularx}"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
