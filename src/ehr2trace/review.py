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

The queue also has to shrink. A term the vocabulary maps on a later run no longer needs
a reviewer, and leaving it listed turns the backlog into a number nobody trusts. Such an
item is marked ``resolved`` rather than deleted -- ids stay stable, and what was asked
stays traceable -- but it leaves the open queue.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Collection, Sequence

from ehr2trace.config import DatasetConfig
from ehr2trace.errors import Ehr2TraceError
from ehr2trace.hashing import sha256_hex
from ehr2trace.paths import WorkLayout
from ehr2trace.terminology import normalize_term

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
    #: 'open' = still unmapped and worth a reviewer's time; 'resolved' = a later run
    #: mapped it without human help. Resolved rows stay in the file so that ids remain
    #: stable and a decision made earlier can still be traced, but they leave the queue.
    "status",
]

OPEN = "open"
RESOLVED = "resolved"

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


def write_pending(
    layout: WorkLayout, items: Sequence[dict[str, Any]], *, retire_absent: bool = False
) -> Path:
    """Write or extend the pending queue, keeping already-listed items stable.

    ``retire_absent`` is for the one caller that passes the *complete* current set of
    unmapped terms -- the OMOP build. Anything already in the file and absent from that
    set has since been mapped, so it is marked resolved and drops out of the queue.
    Every other caller proposes a subset (``propose --limit``) and must leave the rest
    alone, which is why this is opt-in rather than the default: retiring on a partial
    set would silently empty the queue down to whatever the last ``propose`` looked at.

    Nothing is ever deleted. Ids stay stable, and a decision a human already recorded
    against a now-resolved item can still be traced back to what was asked.
    """
    path = layout.review_dir / "pending.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                # files written before 'status' existed carry no such column
                row.setdefault("status", "")
                existing[row["id"]] = row

    seen: set[str] = set()
    for item in items:
        row = {k: item.get(k, "") for k in PENDING_FIELDS}
        row["id"] = item_id(item.get("kind", "terminology"), item.get("code_system", ""), item.get("source_string", ""))
        row["status"] = OPEN
        existing[row["id"]] = row
        seen.add(row["id"])

    for key, row in existing.items():
        if retire_absent and key not in seen:
            row["status"] = RESOLVED
        elif not row.get("status"):
            row["status"] = OPEN

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


def read_pending(layout: WorkLayout, *, open_only: bool = False) -> list[dict[str, str]]:
    """Every row in the queue, or only the ones still awaiting a reviewer.

    A row written before ``status`` existed has no such column; it is read as open,
    which is what it was.
    """
    path = layout.review_dir / "pending.csv"
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [{**row, "status": row.get("status") or OPEN} for row in csv.DictReader(fh)]
    return [r for r in rows if r["status"] == OPEN] if open_only else rows


def read_decisions(layout: WorkLayout) -> dict[str, dict[str, str]]:
    path = layout.review_dir / "decisions.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["id"]: row for row in csv.DictReader(fh) if row.get("id")}


#: What compile did with one accepted decision.
ADDED = "added"  # the term had no row
REPLACED = "replaced"  # decided on a later day than the row it replaced
UNCHANGED = "unchanged"  # the row already says this
SUPERSEDED = "superseded"  # the row was decided on a later day, so the row stands
CONFLICTING = "conflicting"  # decided the same day as a row that says something else
INCOMPLETE = "incomplete"  # no reviewer, no date or no term: nothing can be written
OUTCOMES = (ADDED, REPLACED, UNCHANGED, SUPERSEDED, CONFLICTING, INCOMPLETE)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class CompileOverrideError(Ehr2TraceError):
    """``replace`` named a decision that has no same-day conflict to settle."""


@dataclass(frozen=True)
class CompiledDecision:
    """One accepted decision, the row it writes, and what compile did with it."""

    decision_id: str
    outcome: str
    #: the mapping file, without ``.csv``, that holds the term's row or would hold it
    file: str
    row: dict[str, str]
    #: the row the decision was measured against, if its term had one
    standing: dict[str, str] | None = None
    #: a same-day conflict that a person settled by naming the decision in ``replace``
    confirmed: bool = False


@dataclass
class CompileResult:
    decisions: list[CompiledDecision] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)

    def of(self, outcome: str) -> list[CompiledDecision]:
        return [d for d in self.decisions if d.outcome == outcome]

    @property
    def complete(self) -> bool:
        """Every accepted decision is in mappings/ or was superseded by a later row."""
        return not self.of(CONFLICTING) and not self.of(INCOMPLETE)


def compile_decisions(
    layout: WorkLayout, mappings_dir: Path, *, replace: Collection[str] = ()
) -> CompileResult:
    """decisions.csv -> mappings/<domain>.csv.

    Only accepted decisions with a concept id are compiled. An undecided or deferred
    item stays undecided: it must not reach a published layer, and there is no path
    here that turns a proposal into a mapping without a person saying so.

    decisions.csv is a log, not a snapshot. A decision stays in it after its row has
    been corrected, and the correction may come from another work root, since one row
    serves every dataset that writes the same string. Replaying the log must not undo
    the correction, so a decision replaces a row only if it was decided on a later day.
    An older decision is superseded and changes nothing. A same-day one that differs is
    a conflict -- the dates cannot say which came second -- and is applied only if its
    id is in ``replace``: a person saying so, one row at a time.

    An accepted decision without a reviewer and a YYYY-MM-DD date is not compiled. It
    could only write a row that cannot say who approved it and when.

    Every outcome is returned, and a file is rewritten only if one of its rows changed.
    """
    pending = {row["id"]: row for row in read_pending(layout)}
    files = _read_mapping_files(mappings_dir)
    # The registry reads every file under one key, so a decision meets its term's row
    # wherever that row is, not only in the file the decision's domain would pick.
    where = {key: name for name, rows in files.items() for key in rows}
    confirmed = set(replace)
    result = CompileResult()
    changed: set[str] = set()

    for item_key, decision in sorted(read_decisions(layout).items()):
        if decision.get("decision", "").strip().lower() != "accept":
            continue
        concept_id = (decision.get("concept_id") or "").strip()
        if not concept_id or not concept_id.isdigit():
            continue
        source = pending.get(item_key, {})
        domain = (decision.get("domain_id") or source.get("event_kind") or "misc").strip().lower()
        row = {
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
        key = _mapping_key(row)
        held_in = where.get(key)
        standing = files[held_in][key] if held_in else None
        outcome = _outcome(row, standing)
        settled = outcome == CONFLICTING and item_key in confirmed
        if settled:
            outcome = REPLACED
        # A replacement stays in the file that holds the row; only a new term goes by domain.
        name = held_in or domain or "misc"
        if outcome in (ADDED, REPLACED):
            files.setdefault(name, {})[key] = row
            where[key] = name
            changed.add(name)
        result.decisions.append(CompiledDecision(item_key, outcome, name, row, standing, settled))

    unsettled = confirmed - {d.decision_id for d in result.decisions if d.confirmed}
    if unsettled:
        found = {d.decision_id: d.outcome for d in result.decisions}
        raise CompileOverrideError(
            "nothing was written: replace settles only a decision that conflicts with a row "
            "decided the same day, and "
            + "; ".join(f"{i} is {found.get(i, 'not an accepted decision')}" for i in sorted(unsettled))
            + ". A decision older than its row is restored by recording it again, dated the "
            "day it is decided."
        )

    for name in sorted(changed):
        path = mappings_dir / f"{name}.csv"
        _write_mapping_file(path, files[name])
        result.written.append(path)
    return result


def describe_compile(result: CompileResult) -> list[str]:
    """What compile did, one line per row it changed or declined to change.

    Unchanged rows are only counted. Everything else is listed with both sides, because
    a diff of the file shows it poorly: the row count does not move, and a rewritten
    file is re-sorted.
    """
    counts = ", ".join(f"{len(result.of(outcome))} {outcome}" for outcome in OUTCOMES)
    lines = [f"accepted decisions with a concept id: {len(result.decisions)} ({counts})"]
    for outcome in OUTCOMES:
        if outcome != UNCHANGED:
            lines.extend(f"{outcome}: {_describe(item)}" for item in result.of(outcome))
    names = ", ".join(path.name for path in result.written)
    lines.append(f"wrote {names}" if names else "no mapping file changed")
    return lines


def _outcome(row: dict[str, str], standing: dict[str, str] | None) -> str:
    """Where an accepted decision stands against the row its term already has, if any."""
    if _missing(row):
        return INCOMPLETE
    if standing is None:
        return ADDED
    if not _differing_fields(standing, row):
        return UNCHANGED
    decided, standing_decided = _iso_date(row["decided_on"]), _iso_date(standing.get("decided_on"))
    if standing_decided is None or decided == standing_decided:
        return CONFLICTING
    return REPLACED if decided > standing_decided else SUPERSEDED


def _missing(row: dict[str, str]) -> list[str]:
    """What an accepted decision lacks to write a row that says who approved it and when."""
    missing = []
    if not normalize_term(row.get("source_string")):
        missing.append("a pending item naming its term")
    if not (row.get("decided_by") or "").strip():
        missing.append("a reviewer")
    if _iso_date(row.get("decided_on")) is None:
        missing.append("a YYYY-MM-DD decided_on")
    return missing


def _iso_date(text: str | None) -> date | None:
    """``decided_on`` as a date, if it is written the way every row writes it."""
    text = (text or "").strip()
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:  # the right shape, but not a day: 2026-02-30
        return None


def _differing_fields(standing: dict[str, str], row: dict[str, str]) -> list[str]:
    """The fields a decision's row changes. The source string's spelling is not one of
    them: the key already matched, and two spellings of one key are one term."""
    return [
        name
        for name in MAPPING_FIELDS
        if name != "source_string" and (standing.get(name) or "").strip() != (row.get(name) or "").strip()
    ]


def _mapping_key(row: dict[str, str]) -> tuple[str, str]:
    """The key the registry reads a row under (``MappingRegistry.load``)."""
    return ((row.get("code_system") or "").strip(), normalize_term(row.get("source_string")))


def _read_mapping_files(mappings_dir: Path) -> dict[str, dict[tuple[str, str], dict[str, str]]]:
    """Every mapping file by name without ``.csv``, its rows keyed as the registry keys them."""
    files: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
    if not mappings_dir.is_dir():
        return files
    for path in sorted(mappings_dir.glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as fh:
            files[path.stem] = {_mapping_key(row): row for row in csv.DictReader(fh)}
    return files


def _write_mapping_file(path: Path, rows: dict[tuple[str, str], dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.partial")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MAPPING_FIELDS)
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({k: rows[key].get(k, "") for k in MAPPING_FIELDS})
    tmp.replace(path)


def _describe(item: CompiledDecision) -> str:
    term = f"{item.file}.csv {item.row['code_system']}/{item.row['source_string']} [{item.decision_id}]"
    decision = _describe_row(item.row)
    if item.outcome == INCOMPLETE:
        return f"{term}: {decision} lacks {', '.join(_missing(item.row))}"
    if item.standing is None:
        return f"{term}: {decision}"
    row = _describe_row(item.standing)
    differs = ", ".join(_differing_fields(item.standing, item.row))
    if item.outcome == REPLACED:
        verb = "confirmed over" if item.confirmed else "replaces"
        return f"{term}: {decision} {verb} {row}; differs in {differs}"
    if item.outcome == SUPERSEDED:
        return f"{term}: {decision} is older than {row}; differs in {differs}"
    if _iso_date(item.standing.get("decided_on")) is None:
        return f"{term}: {decision} cannot be ordered against the undated {row}; differs in {differs}"
    return f"{term}: {decision} was decided the same day as {row}; differs in {differs}"


def _describe_row(row: dict[str, str]) -> str:
    concept = " ".join(part for part in (row.get("concept_id"), row.get("concept_name")) if part)
    return f"{concept} ({row.get('decided_by') or 'no reviewer'}, {row.get('decided_on') or 'undated'})"


def undecided_ids(layout: WorkLayout) -> set[str]:
    """Open items nobody has accepted. A resolved item needs no decision."""
    decisions = read_decisions(layout)
    return {
        row["id"]
        for row in read_pending(layout, open_only=True)
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

    from ehr2trace.terminology import (DOMAIN_FOR_KIND, MappingRegistry, Vocabulary, collect_terms,
                                     mappings_directory, resolve_terms)

    events = pl.read_parquet(layout.canonical_path("events"))
    vocabulary = Vocabulary.open(_vocab_dir())
    mappings = MappingRegistry.load(mappings_directory())
    terms = collect_terms(events.iter_rows(named=True))
    _resolved, unresolved = resolve_terms(list(terms.values()), vocabulary, mappings)

    ranked = sorted(unresolved, key=lambda t: (-t.occurrences, t.source_code))[:limit]
    client = None
    if use_llm:
        from ehr2trace.llm import LlmClient

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

    from ehr2trace.canonical.normalize import COL_PREFIX

    client = None
    if use_llm:
        from ehr2trace.llm import LlmClient

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
