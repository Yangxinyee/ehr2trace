"""The human review loop (design section 8.4).

Three CSV files and no workflow engine:

    review/pending.csv     awaiting confirmation
    review/decisions.csv   what a human decided, edited in any tool they like
    mappings/<domain>.csv  compiled, git-tracked, versioned

Flow: ``propose`` writes pending, a human edits decisions, ``compile`` produces
mappings, ``omop`` reruns. Nothing else may write ``mappings/`` -- not the vocabulary
lookup, not the model, not a retry path. That single rule is what makes it possible to
answer "who approved this mapping, and when" for any published row.

An item's id is a hash of what is being asked about, so re-proposing after new data
arrives does not renumber decisions a human already made.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from ehr2cdm.config import DatasetConfig
from ehr2cdm.hashing import sha256_hex
from ehr2cdm.paths import WorkLayout
from ehr2cdm.terminology import normalize_term

PENDING_FIELDS = [
    "id",
    "kind",
    "code_system",
    "source_string",
    "source_name",
    "event_kind",
    "occurrences",
    "candidates",
    "context",
    "proposed_by",
    "proposed_rationale",
]

DECISION_FIELDS = [
    "id",
    "decision",  # accept | reject | defer
    "concept_id",
    "concept_name",
    "domain_id",
    "vocabulary_id",
    "reviewer",
    "decided_on",
    "note",
]

MAPPING_FIELDS = [
    "source_string",
    "code_system",
    "concept_id",
    "concept_name",
    "domain_id",
    "vocabulary_id",
    "mapping_version",
    "decided_by",
    "decided_on",
    "note",
]


def item_id(kind: str, code_system: str, source_string: str) -> str:
    return sha256_hex(f"{kind}|{code_system}|{normalize_term(source_string)}")[:16]


def write_pending(layout: WorkLayout, items: Sequence[dict[str, Any]]) -> Path:
    """Write or extend the pending queue, keeping already-listed items stable."""
    path = layout.review_dir / "pending.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing[row["id"]] = row

    for item in items:
        row = {k: item.get(k, "") for k in PENDING_FIELDS}
        row["id"] = item_id(item.get("kind", "terminology"), item.get("code_system", ""), item.get("source_string", ""))
        existing[row["id"]] = row

    tmp = path.with_suffix(".csv.partial")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PENDING_FIELDS)
        writer.writeheader()
        for key in sorted(existing):
            writer.writerow({k: existing[key].get(k, "") for k in PENDING_FIELDS})
    tmp.replace(path)
    _ensure_decisions(layout)
    return path


def _ensure_decisions(layout: WorkLayout) -> Path:
    """Create an empty decisions file so a reviewer has something to open."""
    path = layout.review_dir / "decisions.csv"
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=DECISION_FIELDS).writeheader()
    return path


def read_pending(layout: WorkLayout) -> list[dict[str, str]]:
    path = layout.review_dir / "pending.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_decisions(layout: WorkLayout) -> dict[str, dict[str, str]]:
    path = layout.review_dir / "decisions.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["id"]: row for row in csv.DictReader(fh) if row.get("id")}


def compile_decisions(layout: WorkLayout, mappings_dir: Path) -> int:
    """decisions.csv -> mappings/<domain>.csv.

    Only accepted decisions with a concept id are compiled. An undecided or deferred
    item stays undecided: it must not reach a published layer, and there is no path
    here that turns a proposal into a mapping without a person saying so.
    """
    pending = {row["id"]: row for row in read_pending(layout)}
    decisions = read_decisions(layout)
    mappings_dir.mkdir(parents=True, exist_ok=True)

    by_domain: dict[str, list[dict[str, str]]] = {}
    written = 0
    for item_key, decision in sorted(decisions.items()):
        if decision.get("decision", "").strip().lower() != "accept":
            continue
        concept_id = (decision.get("concept_id") or "").strip()
        if not concept_id or not concept_id.isdigit():
            continue
        source = pending.get(item_key, {})
        domain = (decision.get("domain_id") or source.get("event_kind") or "misc").strip().lower()
        by_domain.setdefault(domain or "misc", []).append(
            {
                "source_string": source.get("source_string", ""),
                "code_system": source.get("code_system", ""),
                "concept_id": concept_id,
                "concept_name": decision.get("concept_name", ""),
                "domain_id": decision.get("domain_id", ""),
                "vocabulary_id": decision.get("vocabulary_id", ""),
                "mapping_version": "1",
                "decided_by": decision.get("reviewer", ""),
                "decided_on": decision.get("decided_on", ""),
                "note": decision.get("note", ""),
            }
        )
        written += 1

    for domain, rows in sorted(by_domain.items()):
        path = mappings_dir / f"{domain}.csv"
        merged: dict[tuple[str, str], dict[str, str]] = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    merged[(row.get("code_system", ""), normalize_term(row.get("source_string")))] = row
        for row in rows:
            merged[(row["code_system"], normalize_term(row["source_string"]))] = row
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MAPPING_FIELDS)
            writer.writeheader()
            for key in sorted(merged):
                writer.writerow({k: merged[key].get(k, "") for k in MAPPING_FIELDS})
    return written


def undecided_ids(layout: WorkLayout) -> set[str]:
    decisions = read_decisions(layout)
    return {
        row["id"]
        for row in read_pending(layout)
        if decisions.get(row["id"], {}).get("decision", "").strip().lower() != "accept"
    }


# --------------------------------------------------------------------------------
# proposals
# --------------------------------------------------------------------------------


def propose_terminology(cfg: DatasetConfig, layout: WorkLayout, limit: int = 200, use_llm: bool = False) -> int:
    """Propose mappings for the distinct unmapped strings, most frequent first.

    Dispatch is by distinct normalized string, not by row: the difference between a
    queue a person can work through and one they cannot.
    """
    import polars as pl

    from ehr2cdm.terminology import DOMAIN_FOR_KIND, MappingRegistry, Vocabulary, collect_terms, resolve_terms

    events = pl.read_parquet(layout.canonical_path("events"))
    vocabulary = Vocabulary.open(_vocab_dir())
    mappings = MappingRegistry.load(Path.cwd() / "mappings")
    terms = collect_terms(events.iter_rows(named=True))
    _resolved, unresolved = resolve_terms(list(terms.values()), vocabulary, mappings)

    ranked = sorted(unresolved, key=lambda t: (-t.occurrences, t.source_code))[:limit]
    client = None
    if use_llm:
        from ehr2cdm.llm import LlmClient

        client = LlmClient.from_env()

    items: list[dict[str, Any]] = []
    for term in ranked:
        domain = DOMAIN_FOR_KIND.get(term.event_kind)
        candidates = vocabulary.candidates(term.source_name or term.source_code, domain, limit=8)
        proposed_by, rationale = "lexical_recall", ""
        payload = [
            {"concept_id": c.concept_id, "concept_name": c.concept_name, "score": c.score}
            for c in candidates
        ]
        if client is not None and candidates:
            ranking = client.rank_candidates(term.source_name or term.source_code, domain, candidates)
            if ranking is not None:
                payload = ranking.as_payload()
                proposed_by, rationale = "llm_ranking", ranking.rationale
        items.append(
            {
                "kind": "terminology",
                "code_system": term.code_system,
                "source_string": term.source_code,
                "source_name": term.source_name or "",
                "event_kind": term.event_kind,
                "occurrences": term.occurrences,
                "candidates": json.dumps(payload),
                "context": f"domain={domain or ''}",
                "proposed_by": proposed_by,
                "proposed_rationale": rationale,
            }
        )
    write_pending(layout, items)
    vocabulary.close()
    return len(items)


def propose_columns(cfg: DatasetConfig, layout: WorkLayout, use_llm: bool = False) -> int:
    """Propose a role for each source column a human has not already assigned.

    The profile sent out is column names, type and fill statistics, and a handful of
    values that survive a de-identification filter. Never a patient's record.
    """
    import polars as pl

    from ehr2cdm.canonical.normalize import COL_PREFIX

    client = None
    if use_llm:
        from ehr2cdm.llm import LlmClient

        client = LlmClient.from_env()

    items: list[dict[str, Any]] = []
    for part in cfg.partitions:
        for source_id, spec in cfg.sources_for(part.id).items():
            directory = layout.source_dir / part.id / source_id
            files = sorted(directory.glob("*.parquet"))
            if not files:
                continue
            frame = pl.read_parquet(files[0], n_rows=200)
            assigned = {c.lower() for aliases in spec.alias_map().values() for c in aliases}
            for column in frame.columns:
                if not column.startswith(COL_PREFIX):
                    continue
                name = column[len(COL_PREFIX) :]
                if name.lower() in assigned:
                    continue
                profile = _profile(frame, column)
                proposed_by, rationale, suggestion = "unassigned", "", ""
                if client is not None:
                    proposal = client.propose_column_role(source_id, name, profile)
                    if proposal is not None:
                        proposed_by, rationale = "llm_proposal", proposal.rationale
                        suggestion = proposal.role
                items.append(
                    {
                        "kind": "column_semantics",
                        "code_system": f"{part.id}/{source_id}",
                        "source_string": name,
                        "source_name": suggestion,
                        "event_kind": spec.event_kind or "",
                        "occurrences": profile["non_null"],
                        "candidates": json.dumps(profile),
                        "context": f"source={source_id} partition={part.id}",
                        "proposed_by": proposed_by,
                        "proposed_rationale": rationale,
                    }
                )
            break  # one partition's profile is enough to ask the question
    write_pending(layout, items)
    return len(items)


def _profile(frame, column: str) -> dict[str, Any]:
    """Column statistics plus a few short, non-identifying sample values."""
    series = frame[column]
    non_null = int(series.is_not_null().sum() - (series == "").sum())
    samples = [
        v
        for v in series.unique().to_list()[:20]
        if v not in (None, "") and len(str(v)) <= 32
    ][:5]
    return {
        "non_null": non_null,
        "rows_profiled": frame.height,
        "distinct": int(series.n_unique()),
        "samples": samples,
    }


def _vocab_dir() -> Path | None:
    import os

    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None
