"""Terminology lookup and candidate recall (design section 6.4).

The flow is deterministic first and only then assisted:

    normalized source string
      -> exact source-code lookup in the local vocabulary
      -> the source concept's "Maps to" standard concept
      -> domain and validity check
      |- pass -> adopted automatically
      '- fail -> lexical candidate recall -> ranking and explanation only
                 -> a human confirms -> written to mappings/

Two rules that are not negotiable:

* **no concept id appears in this file, or anywhere else in the package.** Even the
  fixed type concepts are looked up, from a git-tracked CSV a human wrote, and
  validated against the vocabulary before use. A concept id typed into code is a
  number nobody can trace and that silently rots when the vocabulary is updated.
* work is dispatched **by unique normalized string**, not per row. Millions of drug
  order rows collapse to a few thousand distinct names, which is the difference
  between a review queue a person can work through and one they cannot.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s%./+-]")

#: Vocabulary tables the deterministic flow needs. Loaded from an Athena download.
REQUIRED_TABLES = (
    "CONCEPT",
    "CONCEPT_RELATIONSHIP",
    "CONCEPT_ANCESTOR",
    "VOCABULARY",
    "DOMAIN",
    "CONCEPT_CLASS",
    "RELATIONSHIP",
    "DRUG_STRENGTH",
)


def normalize_term(text: str | None) -> str:
    """Fold a source string to its dispatch key.

    Deliberately conservative: case and whitespace are noise, but nothing that could
    change a clinical meaning (numbers, units, dose strengths, slashes) is touched.
    """
    if not text:
        return ""
    lowered = _PUNCT.sub(" ", text.strip().lower())
    return _WHITESPACE.sub(" ", lowered).strip()


@dataclass(frozen=True)
class ConceptMatch:
    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    standard_concept: str | None
    source_concept_id: int | None = None
    #: how it was reached: exact_code / mapped_relationship / approved_mapping
    path: str = "exact_code"


@dataclass
class Candidate:
    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    score: float


@dataclass
class MappingRegistry:
    """Human-confirmed mappings, git-tracked and versioned (design section 8.4)."""

    entries: dict[tuple[str, str], ConceptMatch] = field(default_factory=dict)
    version: str = "0"

    @classmethod
    def load(cls, directory: Path, version: str = "0") -> "MappingRegistry":
        registry = cls(version=version)
        if not directory.is_dir():
            return registry
        for path in sorted(directory.glob("*.csv")):
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if not row.get("concept_id"):
                        continue
                    key = (row.get("code_system", "").strip(), normalize_term(row.get("source_string")))
                    registry.entries[key] = ConceptMatch(
                        concept_id=int(row["concept_id"]),
                        concept_name=row.get("concept_name", ""),
                        domain_id=row.get("domain_id", ""),
                        vocabulary_id=row.get("vocabulary_id", ""),
                        standard_concept="S",
                        path="approved_mapping",
                    )
        return registry

    def get(self, code_system: str, source_string: str) -> ConceptMatch | None:
        return self.entries.get((code_system, normalize_term(source_string)))


class NullVocabulary:
    """Stand-in used when no vocabulary is licensed or downloaded yet.

    It maps nothing. That is the honest behaviour: every term keeps its source value,
    every concept id stays 0, and every distinct string goes to the review queue. What
    it must never do is let the pipeline quietly proceed as if terms were mapped.
    """

    available = False
    version = "none"

    def lookup_code(self, code_system: str, code: str) -> ConceptMatch | None:
        return None

    def lookup_name(self, domain: str, name: str) -> ConceptMatch | None:
        return None

    def candidates(self, text: str, domain: str | None = None, limit: int = 10) -> list[Candidate]:
        return []

    def concept_exists(self, concept_id: int) -> bool:
        return concept_id == 0

    def domain_of(self, concept_id: int) -> str | None:
        return None

    def close(self) -> None:
        return None


class Vocabulary:
    """A local OMOP vocabulary in DuckDB, loaded from an Athena CSV download."""

    available = True

    def __init__(self, connection, version: str = "unknown"):
        self.con = connection
        self.version = version

    # -- construction ---------------------------------------------------------

    @classmethod
    def open(cls, directory: Path | None, duckdb_path: Path | None = None):
        """Open a vocabulary, or return :class:`NullVocabulary` if there is none."""
        import duckdb

        if directory is None or not Path(directory).is_dir():
            return NullVocabulary()
        directory = Path(directory)
        files = {t: cls._find_table_file(directory, t) for t in REQUIRED_TABLES}
        missing = [t for t, p in files.items() if p is None]
        if "CONCEPT" in missing:
            return NullVocabulary()

        con = duckdb.connect(str(duckdb_path) if duckdb_path else ":memory:")
        for table, path in files.items():
            if path is None:
                continue
            con.execute(
                f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv_auto(?, delim='\t', "
                "header=true, quote='', all_varchar=true)",
                [str(path)],
            )
        version = "unknown"
        if files.get("VOCABULARY"):
            row = con.execute(
                "SELECT vocabulary_version FROM VOCABULARY WHERE vocabulary_id = 'None' LIMIT 1"
            ).fetchone()
            if row and row[0]:
                version = str(row[0])
        return cls(con, version=version)

    @staticmethod
    def _find_table_file(directory: Path, table: str) -> Path | None:
        for name in (f"{table}.csv", f"{table.lower()}.csv", f"{table}.tsv", f"{table}.txt"):
            path = directory / name
            if path.exists():
                return path
        return None

    # -- deterministic lookup -------------------------------------------------

    def lookup_code(self, code_system: str, code: str) -> ConceptMatch | None:
        """Source code -> source concept -> its standard concept via "Maps to"."""
        row = self.con.execute(
            """
            SELECT c.concept_id, c.concept_name, c.domain_id, c.vocabulary_id, c.standard_concept
            FROM CONCEPT c
            WHERE c.vocabulary_id = ? AND c.concept_code = ?
              AND (c.invalid_reason IS NULL OR c.invalid_reason = '')
            LIMIT 1
            """,
            [code_system, code],
        ).fetchone()
        if not row:
            return None
        source = ConceptMatch(int(row[0]), row[1], row[2], row[3], row[4])
        if source.standard_concept == "S":
            return source
        mapped = self.con.execute(
            """
            SELECT t.concept_id, t.concept_name, t.domain_id, t.vocabulary_id, t.standard_concept
            FROM CONCEPT_RELATIONSHIP r
            JOIN CONCEPT t ON t.concept_id = r.concept_id_2
            WHERE r.concept_id_1 = ? AND r.relationship_id = 'Maps to'
              AND (r.invalid_reason IS NULL OR r.invalid_reason = '')
              AND t.standard_concept = 'S'
            LIMIT 1
            """,
            [source.concept_id],
        ).fetchone()
        if not mapped:
            return None
        return ConceptMatch(
            int(mapped[0]),
            mapped[1],
            mapped[2],
            mapped[3],
            mapped[4],
            source_concept_id=source.concept_id,
            path="mapped_relationship",
        )

    def lookup_name(self, domain: str, name: str) -> ConceptMatch | None:
        """Exact standard-concept name match. Never a fuzzy match posing as exact."""
        row = self.con.execute(
            """
            SELECT concept_id, concept_name, domain_id, vocabulary_id, standard_concept
            FROM CONCEPT
            WHERE lower(concept_name) = lower(?) AND domain_id = ? AND standard_concept = 'S'
              AND (invalid_reason IS NULL OR invalid_reason = '')
            LIMIT 1
            """,
            [name, domain],
        ).fetchone()
        return ConceptMatch(int(row[0]), row[1], row[2], row[3], row[4]) if row else None

    def candidates(self, text: str, domain: str | None = None, limit: int = 10) -> list[Candidate]:
        """Lexical recall for review. Ranking is a suggestion, never an adoption."""
        term = normalize_term(text)
        if not term:
            return []
        tokens = [t for t in term.split() if len(t) > 2][:6]
        if not tokens:
            tokens = term.split()[:2]
        where = ["standard_concept = 'S'", "(invalid_reason IS NULL OR invalid_reason = '')"]
        params: list[object] = []
        if domain:
            where.append("domain_id = ?")
            params.append(domain)
        score = " + ".join(["CASE WHEN lower(concept_name) LIKE ? THEN 1 ELSE 0 END"] * len(tokens))
        params_score = [f"%{t}%" for t in tokens]
        rows = self.con.execute(
            f"""
            SELECT concept_id, concept_name, domain_id, vocabulary_id, ({score}) AS hits
            FROM CONCEPT
            WHERE {' AND '.join(where)}
            QUALIFY hits > 0
            ORDER BY hits DESC, length(concept_name) ASC, concept_id ASC
            LIMIT ?
            """,
            params_score + params + [limit],
        ).fetchall()
        return [
            Candidate(int(r[0]), r[1], r[2], r[3], float(r[4]) / max(1, len(tokens))) for r in rows
        ]

    def concept_exists(self, concept_id: int) -> bool:
        if concept_id == 0:
            return True
        row = self.con.execute("SELECT 1 FROM CONCEPT WHERE concept_id = ?", [concept_id]).fetchone()
        return bool(row)

    def domain_of(self, concept_id: int) -> str | None:
        row = self.con.execute(
            "SELECT domain_id FROM CONCEPT WHERE concept_id = ?", [concept_id]
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.con.close()


#: Which OMOP domain each canonical event kind expects. Domain names are OMOP's, not
#: this dataset's, so a concept landing in the wrong domain is a mapping error.
DOMAIN_FOR_KIND = {
    "condition": "Condition",
    "drug_order": "Drug",
    "drug_admin": "Drug",
    "procedure": "Procedure",
    "measurement": "Measurement",
    "demographic": "Observation",
    "visit": "Visit",
    "note": None,
    "death": None,
}


@dataclass
class TermRequest:
    """One distinct string needing a concept, with the rows it stands for."""

    code_system: str
    source_code: str
    source_name: str | None
    event_kind: str
    occurrences: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.code_system, normalize_term(self.source_code))


def collect_terms(events: Iterable[dict]) -> dict[tuple[str, str], TermRequest]:
    """Distinct terms across events, with occurrence counts for review triage."""
    out: dict[tuple[str, str], TermRequest] = {}
    for event in events:
        code = event.get("source_code")
        if not code:
            continue
        request = TermRequest(
            code_system=event.get("code_system") or "SOURCE",
            source_code=str(code),
            source_name=event.get("source_name"),
            event_kind=event.get("event_kind") or "",
        )
        existing = out.get(request.key)
        if existing is None:
            request.occurrences = 1
            out[request.key] = request
        else:
            existing.occurrences += 1
    return out


def resolve_terms(
    terms: Sequence[TermRequest], vocabulary, mappings: MappingRegistry
) -> tuple[dict[tuple[str, str], ConceptMatch], list[TermRequest]]:
    """Resolve terms deterministically. Whatever fails is returned, not guessed at."""
    resolved: dict[tuple[str, str], ConceptMatch] = {}
    unresolved: list[TermRequest] = []
    for term in terms:
        approved = mappings.get(term.code_system, term.source_code)
        if approved is not None:
            resolved[term.key] = approved
            continue
        match = vocabulary.lookup_code(term.code_system, term.source_code)
        if match is not None:
            expected = DOMAIN_FOR_KIND.get(term.event_kind)
            if expected and match.domain_id != expected:
                # Right code, wrong domain for the field it would land in. That is a
                # mapping question for a human, not something to force into a column.
                unresolved.append(term)
                continue
            resolved[term.key] = match
            continue
        unresolved.append(term)
    return resolved, unresolved
