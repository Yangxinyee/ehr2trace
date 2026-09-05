# -*- coding: utf-8 -*-
"""Split the agent's abstentions into what to do about them, rather than one pile.

An abstention says "none of these 32 candidates is right". That single answer covers
three situations a clinician's time is worth very different amounts on:

  retrieval   the concept exists in the vocabulary and the retriever missed it. Sending
              this to a person means asking them to search a 10-million-row vocabulary by
              hand, which is the machine's job. Fix the query, not the reviewer's day.
  not_a_term  the string is not a clinical concept at all -- a blood tube, a ward, an
              order category, a free-text comment field. It maps to nothing because there
              is nothing to map. Excluding these from the source is the fix.
  ask_owner   the source does not carry the information the mapping needs. A CD19 count
              with no specimen is a different fact in bone marrow than in CSF, and no
              amount of retrieval or clinical knowledge recovers which one it was.

Only the residue is a genuine clinical question. The classification is keyword-based on
the agent's own stated reason plus the source string, which is a heuristic: it is meant
to route a queue, and every bucket stays inspectable rather than being acted on silently.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import re
from pathlib import Path

#: The sixteen values MIMIC-IV's `poe.order_type` actually takes, and the hospital
#: services and care units that appear as visit or order strings. Exact membership, not
#: a pattern: a keyword rule put "Blood Gases/Whole Blood" in the clinical pile and
#: mistook `RDW`, a red cell index, for a ward abbreviation.
POE_ORDER_TYPES = {
    "medications", "lab", "general care", "adt orders", "iv therapy", "nutrition",
    "radiology", "consults", "blood bank", "respiratory", "cardiology", "tpn",
    "critical care", "hemodialysis", "neurology", "ob", "blood gases/whole blood",
    "admit", "activity", "medication",
}

WARDS = {
    "pacu", "tsicu", "micu", "sicu", "ccu", "cvicu", "micu/sicu", "ortho", "nmed",
    "omed", "csurg", "nsurg", "tsurg", "surg", "surgery", "med", "medicine",
    "med/surg", "med/surg/gyn", "med/surg/trauma", "discharge lounge", "urgent",
    "elective", "observation admit", "obs", "home", "transplant", "vascular",
    "psychiatry", "obstetrics", "cardiac surgery", "hematology/oncology",
    "medicine/cardiology", "labor & delivery", "mgh", "eu observation",
}

#: Specimen containers and handling steps, matched on the source string. A tube is not
#: an analyte. Kept deliberately narrow -- every pattern here names a physical container
#: or a laboratory step, never a measurement.
CONTAINER = re.compile(
    r"\b(top hold|uhold|edta hold|hold\b|voided specimen|problem specimen|"
    r"deparaffinization|cryopreservation|cytospin review)\b", re.I)


def classify(source_string: str, source_name: str, why: str) -> str:
    """Certain, or not. Two buckets, because the third was wrong too often to trust.

    An earlier version of this sorted abstentions four ways by pattern-matching the
    agent's free-text reason. It read well and it was wrong: `RDW` -- red cell
    distribution width, a real measurement whose concept the retriever simply missed --
    landed in "not a clinical concept" because the rule for opaque local codes caught any
    short uppercase string. That is the exact failure this project exists to catch, so
    the classification now only claims what it can check by membership in a list.

    Everything else goes to review carrying the agent's own stated reason, which a person
    can read in a second. Routing badly costs a clinician's afternoon; routing not at all
    costs them a scroll.
    """
    s = (source_string or "").strip().lower()
    n = (source_name or "").strip().lower()
    if s in POE_ORDER_TYPES or n in POE_ORDER_TYPES:
        return "excluded_order_category"
    if s in WARDS or n in WARDS:
        return "excluded_ward_or_service"
    if CONTAINER.search(f"{source_string} {source_name}"):
        return "excluded_container"
    return "review"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    items = {r["id"]: r for r in json.loads((args.review_dir / "candidates_k32.json").read_text())}
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    for f in sorted((args.review_dir / "agent_out").glob("batch_*.json")):
        for r in json.loads(f.read_text()):
            if r.get("pick") is not None:
                continue
            it = items.get(r["id"])
            if it is None:
                continue
            b = classify(it.get("source_string", ""), it.get("source_name", ""), r.get("why", ""))
            buckets[b].append({"id": r["id"], "source_string": it.get("source_string", ""),
                               "source_name": it.get("source_name", ""),
                               "event_kind": it.get("event_kind", ""),
                               "occurrences": int(it.get("occurrences") or 0),
                               "confidence": r.get("confidence", ""), "why": r.get("why", "")})

    total = sum(len(v) for v in buckets.values())
    rows = {k: sum(x["occurrences"] for x in v) for k, v in buckets.items()}
    print(f"{total} abstentions\n")
    order = ["excluded_order_category", "excluded_ward_or_service",
             "excluded_container", "review"]
    label = {"excluded_order_category": "provider-order category, not a drug -> config fix",
             "excluded_ward_or_service": "hospital service or care unit -> config fix",
             "excluded_container": "specimen tube or handling step -> config fix",
             "review": "goes to a person, with the agent's reason attached"}
    for k in order:
        v = buckets.get(k, [])
        if not v:
            continue
        print(f"  {k:<12} {len(v):>4} terms  {rows[k]:>12,} rows   {label[k]}")
        for x in sorted(v, key=lambda x: -x["occurrences"])[:4]:
            nm = (x["source_name"] or x["source_string"])[:44]
            print(f"      {nm:<46} {x['occurrences']:>10,}  {x['why'][:44]}")
    if args.out:
        args.out.write_text(json.dumps(buckets, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
