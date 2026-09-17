"""Put one dataset's conversion before and after the remediation side by side.

The 2026-09 remediation (docs/DECISIONS.md) is judged by whether its
section 5 numbers move, and section 3 of phase 3 asks for one document per dataset that
says so. Both sides are the output of ``tools/audit_conversion.py``: the "before" side is
normally the 2026-09-13 baseline file, which wraps that audit together with the
validation results of the old build, and the "after" side is the audit of the rebuilt
layers plus that build's ``runs/validation.json``.

Nothing here queries a build. Every number is read from the two documents, so the
comparison can be re-rendered without the builds, and it carries aggregates only: no
patient, no cell value, no note text. A target the audit does not measure is listed with
its plan value and the words "not measured here", rather than filled from somewhere else.

    python tools/remediation_compare.py --dataset cu_ctpa \\
        --before results/cu_ctpa/remediation_baseline_2026-09-13.json \\
        --after results/cu_ctpa/conversion_audit_after.json \\
        --after-validation "$EHR_WORK_ROOT/cu_ctpa/runs/validation.json" \\
        --out results/cu_ctpa/remediation_compare.md
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: The checks the plan's phase 0 added (T0.2). Section 5.1 asks that all of them pass,
#: apart from what a dataset declares as expected.
NEW_CHECKS = (
    "DUPLICATES_AGREE", "SOURCE_YIELDS_EVENTS", "QUARANTINE_SHARE_DECLARED", "DEATH_PUBLISHED",
    "UNIT_CONCEPT_COVERAGE", "UNIT_KNOWN", "UNIT_VALUE_PLAUSIBLE", "UNIT_HOMOGENEOUS_PER_CODE",
    "DOSE_UNIT_CARRIED", "VISIT_CONCEPT_COVERAGE", "NOTE_TEXT_UNIQUE", "ENCOUNTER_RESOLVES",
    "CODE_DESCRIPTION_IS_REPRESENTATIVE", "RAW_COVERAGE_DECLARED", "EXCLUDED_STATUS_NOT_PUBLISHED",
)

NOT_MEASURED = "not measured here"


def get(d: Any, *path: str, default: Any = None) -> Any:
    for part in path:
        if isinstance(d, dict) and part in d:
            d = d[part]
        else:
            return default
    return d


def load(path: Path) -> tuple[dict, list | None, dict]:
    """(audit, validation results or None, provenance) from a baseline or a bare audit."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc.get("audit"), dict):
        return doc["audit"], doc.get("validation"), {
            "repository_head": doc.get("repository_head"), "written_utc": doc.get("written_utc"), "taken": doc.get("taken"),
        }
    return doc, None, {"repository_head": doc.get("repository_head"), "written_utc": doc.get("collected_utc")}


def fmt(v: Any, kind: str = "int") -> str:
    if v is None:
        return "n/a"
    if kind == "share" and isinstance(v, (int, float)):
        return f"{v:.2%}"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.4g}"
    return str(v)


def near(value: Any, target: float, tolerance: float = 0.02) -> bool | None:
    if not isinstance(value, (int, float)):
        return None
    return abs(value - target) <= tolerance * target


@dataclass
class Metric:
    label: str
    value: Callable[[dict], Any]
    plan_before: str
    target: str
    met: Callable[[Any], bool | None] = lambda v: None
    kind: str = "int"


def undeclared_raw(a: dict) -> int | None:
    rc = a.get("raw_coverage")
    if not isinstance(rc, dict):
        return None
    columns = sum(len(c.get("undeclared") or []) for c in (rc.get("columns") or {}).values() if isinstance(c, dict))
    files = len(get(rc, "files", "unclaimed", default=[]) or [])
    unread = get(rc, "prepare_manifest", "undeclared_unread_inputs_count", default=0) or 0
    return columns + files + unread


def unruled_sources(a: dict) -> int | None:
    md = a.get("merge_disagreements")
    if not isinstance(md, dict):
        return None
    return sum(1 for m in md.values() if isinstance(m, dict) and (m.get("disagreements") or m.get("rules_not_applied")))


def source_events(name: str) -> Callable[[dict], Any]:
    return lambda a: get(a, "sources", name, "events")


def uncovered_visits(a: dict) -> float | None:
    coverage = get(a, "visits", "visit_occurrence", "coverage")
    return None if coverage is None else 1.0 - coverage


COMMON = [
    Metric("Numeric measurements with a unit that carry a unit concept", lambda a: get(a, "units", "omop_measurement", "coverage"),
           "0%", ">= 95%", lambda v: None if v is None else v >= 0.95, "share"),
    Metric("Delivered columns, files and unread inputs nobody declared", undeclared_raw, "not counted", "0",
           lambda v: None if v is None else v == 0),
    Metric("Sources whose merged rows disagree outside a declared rule", unruled_sources, "every source with merges", "0",
           lambda v: None if v is None else v == 0),
    Metric("Note groups with one text twice on one day (beyond declarations: see NOTE_TEXT_UNIQUE)",
           lambda a: get(a, "notes", "exact_duplicate_groups"), "reported", "reported"),
]

PER_DATASET: dict[str, list[Metric]] = {
    "cu_ctpa": [
        Metric("Drug order events", lambda a: get(a, "events_by_kind", "drug_order"), "3,088,590", "about 4,195,000",
               lambda v: near(v, 4_195_000)),
        Metric("Note events", lambda a: get(a, "events_by_kind", "note"), "2,664,292", "about 1,728,000",
               lambda v: near(v, 1_728_000, 0.10)),
        Metric("Same-day identical note groups", lambda a: get(a, "notes", "exact_duplicate_groups"), "491,192", "0",
               lambda v: None if v is None else v == 0),
        Metric("ICU stay events", source_events("icu_stays"), "39,148", "39,325", lambda v: None if v is None else v == 39_325),
        Metric("CPT procedure events", source_events("procedures_cpt"), "718,261", "718,261",
               lambda v: None if v is None else v == 718_261),
        Metric("CPT events merged from a facility and a professional bill",
               lambda a: get(a, "merge_disagreements", "procedures_cpt", "ruled_disagreements", "procedure_name"),
               "97,183 (unflagged)", "97,183 flagged BILLING_DUPLICATE", lambda v: near(v, 97_183, 0.01)),
        Metric("Drug exposures without a dose unit", lambda a: get(a, "doses", "omop_drug_exposure", "without_dose_unit"),
               "3,088,590", "only rows whose source dose unit is empty",
               lambda v: None),
        Metric("Drug events whose source dose unit is empty",
               lambda a: (get(a, "doses", "canonical", "drug_events") or 0) - (get(a, "doses", "canonical", "with_unit_source") or 0)
               if get(a, "doses", "canonical") else None, NOT_MEASURED, "equals the line above"),
    ],
    "ctpe": [
        Metric("Drug order events", lambda a: get(a, "events_by_kind", "drug_order"), "8,479,129", "about 9,800,000",
               lambda v: near(v, 9_800_000, 0.05)),
        Metric("Problem-list merges whose status disagrees outside the rule",
               lambda a: get(a, "merge_disagreements", "problem_list", "disagreements", "status", default=0),
               "45,348 groups", "0", lambda v: None if v is None else v == 0),
        Metric("Visit occurrences without a visit concept", uncovered_visits, "34%", "< 5%",
               lambda v: None if v is None else v < 0.05, "share"),
        Metric("Follow-up events", source_events("followup"), "0", "one per patient with a follow-up"),
        Metric("ICU transfer events (canonical)", source_events("icu_transfers"), "0", "946,025 after cross-partition merge",
               lambda v: None if v is None else v == 946_025),
        Metric("ICU transfers published to VISIT_DETAIL", lambda a: get(a, "visits", "visit_detail", "rows"), "0",
               "those with a parent visit (OMOP requires one)"),
        Metric("Visit details withheld for want of a parent visit", lambda a: get(a, "visits", "visit_detail_unparented_issues"),
               "n/a", "reported"),
    ],
    "mimiciv": [
        Metric("OMOP DEATH rows", lambda a: get(a, "death", "omop_death_rows"), "26,899", "38,300",
               lambda v: near(v, 38_300, 0.001)),
        Metric("Subjects whose death records disagree on the local date",
               lambda a: get(a, "death", "subjects_with_conflicting_local_dates"), "11,402 by timestamp", "1",
               lambda v: None if v is None else v <= 1),
        Metric("Visit occurrences without a visit concept", uncovered_visits, "86%", "< 5%",
               lambda v: None if v is None else v < 0.05, "share"),
        Metric("Zero-length visit occurrences", lambda a: get(a, "visits", "visit_occurrence", "zero_length"),
               "546,196 UNKNOWN", "0 UNKNOWN", lambda v: None if v is None else v == 0),
        Metric("ED vital-sign events", source_events("ed_vitalsign"), "0", "every non-null prepared value"),
        Metric("ED triage events", source_events("ed_triage"), "0 (2,849,786 values quarantined)", "all, flagged TIME_FALLBACK"),
        Metric("Susceptibility events", source_events("micro_susceptibility"), "0", "about 1,410,000",
               lambda v: near(v, 1_410_000, 0.05)),
        Metric("Drug exposures without a dose unit", lambda a: get(a, "doses", "omop_drug_exposure", "without_dose_unit"),
               "about 18.4 million", "only rows whose source dose unit is empty"),
        Metric("eMAR rows quarantined, by reason", lambda a: get(a, "sources", "emar", "quarantine"),
               "2,120,873 unnamed", "about 657,716 unnamed"),
        Metric("Pharmacy rows quarantined, by reason", lambda a: get(a, "sources", "pharmacy", "quarantine"),
               "1,137,574 unnamed", "about 68,452 unnamed"),
    ],
}


def summarize(validation: list | None) -> dict[str, dict]:
    return {r["check_id"]: r for r in (validation or []) if isinstance(r, dict) and r.get("check_id")}


def status(r: dict | None) -> str:
    if r is None:
        return "absent"
    return "skip" if r.get("skipped") else ("pass" if r.get("passed") else "FAIL")


def render(dataset: str, before: tuple, after: tuple) -> str:
    (ba, bv, bp), (aa, av, ap) = before, after
    out: list[str] = [f"# Remediation comparison: {dataset}", ""]
    out += [f"- Before: {bp.get('taken') or 'audit'}; repository {str(bp.get('repository_head') or '')[:12]}; written {bp.get('written_utc')}",
            f"- After: code {aa.get('code_version')}, config {str(aa.get('config_hash') or '')[:12]}; repository {str(ap.get('repository_head') or aa.get('repository_head') or '')[:12]}; collected {aa.get('collected_utc')}",
            "- Every number is an aggregate read from the two audit documents; nothing identifies a patient.", ""]

    out += ["## Plan targets (section 5)", "", "| Metric | Plan before | Before (measured) | After | Target | Met |", "|---|---:|---:|---:|---|:-:|"]
    for m in COMMON + PER_DATASET.get(dataset, []):
        try:
            b = m.value(ba)
        except Exception:
            b = None
        try:
            v = m.value(aa)
        except Exception:
            v = None
        met = m.met(v)
        mark = "" if met is None else ("yes" if met else "**no**")
        bv_s = json.dumps(b, sort_keys=True) if isinstance(b, dict) else fmt(b, m.kind)
        av_s = json.dumps(v, sort_keys=True) if isinstance(v, dict) else fmt(v, m.kind)
        out.append(f"| {m.label} | {m.plan_before} | {bv_s} | {av_s} | {m.target} | {mark} |")
    out.append("")

    bs, as_ = summarize(bv), summarize(av)
    if av is None:
        out += ["## Checks", "", "No validation results were given for the rebuilt layers.", ""]
    else:
        def counts(s: dict) -> str:
            vals = [status(r) for r in s.values()]
            return f"{vals.count('pass')} passed, {vals.count('skip')} skipped, {vals.count('FAIL')} failed"
        out += ["## Checks", "", f"- Before: {counts(bs) if bv is not None else 'not given'}", f"- After: {counts(as_)}", ""]
        out += ["| New check (T0.2) | Before | After | After, in brief |", "|---|:-:|:-:|---|"]
        for cid in NEW_CHECKS:
            detail = (get(as_, cid, "detail") or "").replace("|", "/").replace("\n", " ")
            out.append(f"| {cid} | {status(bs.get(cid))} | {status(as_.get(cid))} | {detail[:180]} |")
        regressions = sorted(c for c, r in bs.items() if status(r) == "pass" and status(as_.get(c)) == "FAIL")
        others = sorted(c for c, r in as_.items() if status(r) == "FAIL" and c not in NEW_CHECKS)
        out += ["", f"- Checks that passed before and fail now: {', '.join(regressions) or 'none'}",
                f"- Other failing checks after: {', '.join(others) or 'none'}", ""]

    names = sorted(set(get(ba, "sources", default={}) or {}) | set(get(aa, "sources", default={}) or {}))
    out += ["## Events by source", "", "| Source | Before | After | Change | Quarantined after, by reason |", "|---|---:|---:|---:|---|"]
    for n in names:
        b, v = get(ba, "sources", n, "events"), get(aa, "sources", n, "events")
        change = "" if not isinstance(b, int) or not isinstance(v, int) else f"{v - b:+,}"
        q = get(aa, "sources", n, "quarantine") or {}
        out.append(f"| {n} | {fmt(b)} | {fmt(v)} | {change} | {', '.join(f'{k} {c:,}' for k, c in sorted(q.items())) or ''} |")
    out.append("")

    md = get(aa, "merge_disagreements", default={}) or {}
    open_ = {s: m for s, m in md.items() if isinstance(m, dict) and (m.get("disagreements") or m.get("rules_not_applied"))}
    out += ["## Merge disagreements after", ""]
    if not open_:
        out.append("Every merged row agrees with its event, or the disagreement falls under a rule the build applied.")
    for s, m in sorted(open_.items()):
        out.append(f"- {s}: outside a rule {m.get('disagreements')}; rules not applied {m.get('rules_not_applied')}")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--before", required=True, type=Path, help="baseline file (audit + validation) or a bare audit")
    ap.add_argument("--after", required=True, type=Path, help="audit of the rebuilt layers (or a baseline-shaped file)")
    ap.add_argument("--after-validation", type=Path, help="runs/validation.json of the rebuilt layers")
    ap.add_argument("--out", type=Path, help="markdown to write (default results/<dataset>/remediation_compare.md)")
    args = ap.parse_args()
    before, after = load(args.before), load(args.after)
    if args.after_validation:
        after = (after[0], json.loads(args.after_validation.read_text(encoding="utf-8")), after[2])
    out = args.out or Path("results") / args.dataset / "remediation_compare.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(args.dataset, before, after), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
