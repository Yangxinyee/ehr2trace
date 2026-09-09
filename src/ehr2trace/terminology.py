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
import os
import re
from dataclasses import dataclass, field, replace
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
    #: Who accepted this mapping, for entries that came from the curated registry.
    #: A registry row carries its own reviewer and date, and that is the acceptance
    #: record; requiring a second one per work root made every mapping shipped with
    #: the repository fail on a reader's first conversion.
    decided_by: str = ""
    #: The other standard concepts this source code also maps to, as (id, domain).
    #:
    #: A source code with several `Maps to` targets asserts every one of them, and on
    #: MIMIC-IV they are almost never nested: of 4,258 such codes only 2 have one target
    #: as an ancestor of another, so keeping one drops a fact that the kept one does not
    #: imply. `E1122` is "type 2 diabetes mellitus with diabetic chronic kidney disease"
    #: and maps to `Type 2 diabetes mellitus` and `Chronic kidney disease due to type 2
    #: diabetes mellitus`, which sit in different branches: a cohort searching for either
    #: one alone finds nothing of the other.
    #:
    #: Carried rather than acted on. OMOP has a row per concept and publishes all of
    #: them; MEDS is one event per thing that happened and publishes the primary pick,
    #: because one recorded code is one event whatever the vocabulary says it means.
    alternates: tuple[tuple[int, str], ...] = ()


@dataclass
class Candidate:
    concept_id: int
    concept_name: str
    domain_id: str
    vocabulary_id: str
    score: float


def mappings_directory() -> Path:
    """Where the human-confirmed mappings live.

    The working directory by default, which is what a person running `ehr2trace` expects.
    `EHR_MAPPINGS_DIR` overrides it, the same way `OMOP_VOCAB_DIR` overrides the
    vocabulary -- a build should not silently pick up whichever mappings happen to be
    beside the shell it was launched from, and a test should not depend on the state of
    a developer's checkout.
    """
    raw = os.environ.get("EHR_MAPPINGS_DIR")
    return Path(raw) if raw else Path.cwd() / "mappings"


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
                        decided_by=(row.get("decided_by") or "").strip(),
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
            # The relation API rather than a parameterized CREATE TABLE: DuckDB refuses
            # to prepare a CREATE statement, so passing the path as a bound parameter
            # fails the moment a real vocabulary is supplied -- which is exactly when
            # nobody is looking. Everything is read as text: concept ids are compared as
            # strings here and cast once, at the point of use.
            relation = con.read_csv(
                str(path), sep="\t", header=True, quotechar="", all_varchar=True
            )
            con.register(f"_src_{table}", relation)
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _src_{table}")
            con.unregister(f"_src_{table}")
        # The bundle's version string is metadata, not data. Athena records it on a
        # sentinel row; a bundle that does not carry it is still perfectly usable, so a
        # missing or oddly-shaped VOCABULARY table downgrades the version to "unknown"
        # rather than failing the load.
        version = "unknown"
        if files.get("VOCABULARY"):
            try:
                row = con.execute(
                    "SELECT vocabulary_version FROM VOCABULARY WHERE vocabulary_id = 'None' LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    version = str(row[0])
            except Exception:
                pass
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
        # The score is filtered in an outer query rather than with QUALIFY, which
        # DuckDB reserves for window functions. Ties break toward the shorter concept
        # name: given "Heart failure" and "Heart failure with reduced ejection
        # fraction", the less specific one is the safer thing to put in front of a
        # reviewer, because adding specificity nobody wrote down is the failure mode
        # that matters here.
        rows = self.con.execute(
            f"""
            SELECT concept_id, concept_name, domain_id, vocabulary_id, hits FROM (
                SELECT concept_id, concept_name, domain_id, vocabulary_id,
                       ({score}) AS hits
                FROM CONCEPT
                WHERE {' AND '.join(where)}
            ) WHERE hits > 0
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


def resolve_terms_batch(
    terms: Sequence[TermRequest], vocabulary, mappings: MappingRegistry,
    drug_name_noise: Sequence[str] = (),
) -> tuple[dict[tuple[str, str], ConceptMatch], list[TermRequest]]:
    """Resolve every term in two SQL joins rather than one query per term.

    Semantically identical to :func:`resolve_terms` -- same source-concept lookup, same
    ``Maps to`` hop, same domain check -- but a real export has tens of thousands of
    distinct terms and a real vocabulary has millions of concepts, so asking one
    question at a time turns twenty minutes of work into a query per term. Falls back
    to the per-term path when there is no vocabulary to join against.
    """
    if not getattr(vocabulary, "available", False):
        return resolve_terms(terms, vocabulary, mappings)

    con = vocabulary.con
    resolved: dict[tuple[str, str], ConceptMatch] = {}
    pending: list[TermRequest] = []
    for term in terms:
        approved = mappings.get(term.code_system, term.source_code)
        confirmed = _confirm(con, approved) if approved is not None else None
        if confirmed is not None:
            resolved[term.key] = confirmed
        else:
            # An approved mapping whose concept this vocabulary does not have is not a
            # mapping yet. The decision was made against some vocabulary; publishing the
            # id anyway would put a number in the output that nothing here can confirm,
            # which is the one thing this module is not allowed to do. It goes back in
            # the queue, and `ehr2trace validate` reports the gap as a missing concept
            # rather than as a mapping that quietly did nothing.
            pending.append(term)
    if not pending:
        return resolved, []
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _terms "
        "(code_system VARCHAR, source_code VARCHAR, event_kind VARCHAR)"
    )
    con.executemany(
        "INSERT INTO _terms VALUES (?, ?, ?)",
        [(t.code_system, t.source_code, t.event_kind) for t in pending],
    )
    rows = con.execute(
        """
        WITH src AS (
            SELECT t.code_system, t.source_code, t.event_kind,
                   CAST(c.concept_id AS BIGINT) AS source_concept_id,
                   c.standard_concept, c.concept_name, c.domain_id, c.vocabulary_id
            FROM _terms t
            JOIN CONCEPT c
              ON c.vocabulary_id = t.code_system
             AND c.concept_code = t.source_code
             AND (c.invalid_reason IS NULL OR c.invalid_reason = '')
        )
        SELECT s.code_system, s.source_code, s.event_kind,
               CASE WHEN s.standard_concept = 'S' THEN s.source_concept_id
                    ELSE CAST(m.concept_id AS BIGINT) END AS concept_id,
               CASE WHEN s.standard_concept = 'S' THEN s.concept_name ELSE m.concept_name END,
               CASE WHEN s.standard_concept = 'S' THEN s.domain_id ELSE m.domain_id END,
               CASE WHEN s.standard_concept = 'S' THEN s.vocabulary_id ELSE m.vocabulary_id END,
               s.source_concept_id,
               s.standard_concept = 'S' AS was_already_standard,
               -- How many standard concepts this source code maps to. One `Maps to` row
               -- is the common case; 26,562 ICD-10-CM codes have more than one, and
               -- which of them a run picks must not depend on the query plan.
               count(DISTINCT CASE WHEN s.standard_concept = 'S' THEN s.source_concept_id
                                   ELSE CAST(m.concept_id AS BIGINT) END)
                 OVER (PARTITION BY s.code_system, s.source_code) AS competing
        FROM src s
        LEFT JOIN CONCEPT_RELATIONSHIP r
               ON r.concept_id_1 = CAST(s.source_concept_id AS VARCHAR)
              AND r.relationship_id = 'Maps to'
              AND (r.invalid_reason IS NULL OR r.invalid_reason = '')
        LEFT JOIN CONCEPT m
               ON m.concept_id = r.concept_id_2 AND m.standard_concept = 'S'
        WHERE s.standard_concept = 'S' OR m.concept_id IS NOT NULL
        -- The loop below keeps the first row per code. Without a total order that is
        -- whichever row the engine emitted first, which varies with the thread count --
        -- so two runs of the same build mapped the same code to different concepts.
        ORDER BY s.code_system, s.source_code, concept_id
        """
    ).fetchall()
    con.execute("DROP TABLE IF EXISTS _terms")

    hits: dict[tuple[str, str], ConceptMatch] = {}
    for (code_system, source_code, event_kind, concept_id, name, domain, vocab, source_id,
         direct, competing) in rows:
        if concept_id is None:
            continue
        # The domain is carried, not gated on. It used to be a rejection: a code whose
        # standard concept was not the domain the source column implied was dropped and
        # queued for review, on the reasoning that forcing it into the wrong column was
        # worse than publishing nothing. That reasoning was right about the column and
        # wrong about the conclusion -- OMOP decides a fact's table by its concept's
        # domain, not by the column it arrived in, and it has an OBSERVATION table for
        # exactly the codes that are not conditions. Gating here withheld 5,737 terms
        # carrying 1,944,952 rows, nearly all of them Z codes: family history, screening
        # encounters, socioeconomic factors. The routing now happens where the row is
        # written, which is the only place that can see which tables exist.
        key = (code_system, normalize_term(source_code))
        if key in hits:
            # Same source code, another standard concept. The first is the primary pick
            # (the query orders by concept id, so the choice is deterministic); the rest
            # are recorded so a caller with room for them can publish them.
            existing = hits[key]
            if int(concept_id) != existing.concept_id:
                hits[key] = replace(
                    existing,
                    alternates=existing.alternates + ((int(concept_id), domain or ""),),
                )
            continue
        hits[key] = ConceptMatch(
            concept_id=int(concept_id),
            concept_name=name or "",
            domain_id=domain or "",
            vocabulary_id=vocab or "",
            standard_concept="S",
            source_concept_id=None if direct else int(source_id),
            path=_path("exact_code" if direct else "mapped_relationship", competing),
        )

    resolved.update(hits)
    unresolved = [t for t in pending if t.key not in hits]
    if unresolved:
        second, unresolved = _resolve_unpunctuated(con, unresolved)
        resolved.update(second)
    if unresolved:
        third, unresolved = _resolve_structured_drugs(vocabulary, unresolved, drug_name_noise)
        resolved.update(third)
    return resolved, unresolved


def _resolve_structured_drugs(vocabulary, pending: Sequence[TermRequest],
                              drug_name_noise: Sequence[str] = ()):
    """Third pass, for drug names that are strings rather than codes.

    A hospital's medication file names the drug rather than coding it, so the first two
    passes -- which look a code up -- have nothing to look up and every distinct name
    lands in the review queue: on one export that was 26,490 names carrying 8.2 million
    rows. The names are not free text, though. `OXYCODONE 5 MG TABLET` states an
    ingredient, a strength and a dose form, and the vocabulary states the same three
    things about `oxycodone hydrochloride 5 MG Oral Tablet` -- the strength as a number
    in `DRUG_STRENGTH`. Comparing the three is exact, and it is what
    :mod:`ehr2trace.drug_match` does.

    This stays a deterministic pass: a name resolves only when exactly one standard
    concept has that ingredient set, that strength and that dose form. Nothing here
    ranks, scores or approximates, and a name that fits two concepts or none is
    returned unresolved for a person to decide.
    """
    from .drug_match import DrugIndex, match_drug

    drugs = [t for t in pending if DOMAIN_FOR_KIND.get(t.event_kind or "") == "Drug"]
    if not drugs:
        return {}, list(pending)
    index = getattr(vocabulary, "_drug_index", None)
    if index is None:
        try:
            index = DrugIndex(vocabulary.con)
        except Exception:
            # A vocabulary without DRUG_STRENGTH cannot answer this question. That is a
            # missing table, not a mapping failure: the terms stay unresolved and the
            # `ehr2trace vocabulary` check is what reports the gap.
            return {}, list(pending)
        vocabulary._drug_index = index

    resolved: dict[tuple[str, str], ConceptMatch] = {}
    for term in drugs:
        name = term.source_name or term.source_code
        _parsed, status, matches = match_drug(index, name, drug_name_noise)
        if status != "unique":
            continue
        match = matches[0]
        resolved[term.key] = ConceptMatch(
            concept_id=match.concept_id,
            concept_name=match.concept_name,
            domain_id="Drug",
            vocabulary_id=match.vocabulary_id,
            standard_concept="S",
            # The route is part of the record: a reviewer can list every mapping that
            # needed the total-dose reading of a concentration, or a second spelling of
            # a dose form, without re-deriving anything.
            path=f"structured_drug_{match.route}",
        )
    return resolved, [t for t in pending if t.key not in resolved]


def _confirm(con, approved: ConceptMatch) -> ConceptMatch | None:
    """Check a human-approved concept id against the vocabulary actually loaded.

    A mapping in `mappings/` is a person's decision, and it is still only as good as the
    vocabulary it was made against: a concept can be retired, or the bundle in use can
    simply not contain it. The domain and name come back from the vocabulary rather than
    from the CSV, so a stale name in a git-tracked file cannot travel into the output.
    """
    row = con.execute(
        """
        SELECT concept_name, domain_id, vocabulary_id, standard_concept
        FROM CONCEPT
        WHERE concept_id = ? AND standard_concept = 'S'
          AND (invalid_reason IS NULL OR invalid_reason = '')
        """,
        [str(approved.concept_id)],
    ).fetchone()
    if not row:
        return None
    return replace(approved, concept_name=row[0], domain_id=row[1], vocabulary_id=row[2],
                   standard_concept=row[3])


def _path(path: str, competing: int | None) -> str:
    """Name the route a mapping took, and say when the route had a fork in it.

    A source code with several `Maps to` targets genuinely maps to all of them; this
    converter publishes one concept per event, so it takes the lowest concept id and
    records that it did. The suffix is what makes the choice reviewable instead of
    silent -- a reviewer can list every mapping that had an alternative, which is not
    something a `concept_id` column can be asked afterwards.
    """
    return f"{path}_ambiguous" if competing and competing > 1 else path


#: Characters that are presentation, not identity, in a code: `F17.210` and `F17210`
#: are the same ICD-10-CM code written two ways.
_PUNCTUATION = str.maketrans("", "", ". -/")


def _unpunctuated(code: str) -> str:
    return code.upper().translate(_PUNCTUATION)


def _resolve_unpunctuated(con, pending: Sequence[TermRequest]):
    """Second pass for codes that differ from the vocabulary only in punctuation.

    MIMIC-IV writes ICD-10-CM as `F17210`; the vocabulary writes `F17.210`. Matching on
    the literal string mapped 197 of 19,440 codes -- the three-character ones, which
    have no decimal point to disagree about -- and turned the other 99% into
    `concept_id = 0`. That is legal OMOP and it is not a true statement about the data.

    A match is only accepted where exactly one vocabulary code reduces to the same
    string. The uniqueness is checked here rather than assumed: it holds for ICD-10-CM
    today, and a vocabulary in which it did not would otherwise silently pick one of
    two different codes.
    """
    resolved: dict[tuple[str, str], ConceptMatch] = {}
    by_stripped: dict[tuple[str, str], list[TermRequest]] = {}
    for term in pending:
        by_stripped.setdefault((term.code_system, _unpunctuated(term.source_code)), []).append(term)
    if not by_stripped:
        return resolved, list(pending)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE _stripped (code_system VARCHAR, stripped VARCHAR)"
    )
    con.executemany("INSERT INTO _stripped VALUES (?, ?)", sorted(by_stripped))
    rows = con.execute(
        """
        WITH candidates AS (
            SELECT s.code_system, s.stripped, c.concept_code,
                   CAST(c.concept_id AS BIGINT) AS source_concept_id,
                   c.standard_concept, c.concept_name, c.domain_id, c.vocabulary_id
            FROM _stripped s
            JOIN CONCEPT c
              ON c.vocabulary_id = s.code_system
             AND upper(replace(replace(replace(replace(c.concept_code, '.', ''), '-', ''), '/', ''), ' ', '')) = s.stripped
             AND (c.invalid_reason IS NULL OR c.invalid_reason = '')
        ),
        unambiguous AS (
            SELECT code_system, stripped FROM candidates
            GROUP BY code_system, stripped HAVING count(DISTINCT concept_code) = 1
        )
        SELECT c.code_system, c.stripped,
               CASE WHEN c.standard_concept = 'S' THEN c.source_concept_id ELSE CAST(m.concept_id AS BIGINT) END,
               CASE WHEN c.standard_concept = 'S' THEN c.concept_name ELSE m.concept_name END,
               CASE WHEN c.standard_concept = 'S' THEN c.domain_id ELSE m.domain_id END,
               CASE WHEN c.standard_concept = 'S' THEN c.vocabulary_id ELSE m.vocabulary_id END,
               c.source_concept_id,
               c.standard_concept = 'S' AS was_already_standard,
               count(DISTINCT CASE WHEN c.standard_concept = 'S' THEN c.source_concept_id
                                   ELSE CAST(m.concept_id AS BIGINT) END)
                 OVER (PARTITION BY c.code_system, c.stripped) AS competing
        FROM candidates c
        JOIN unambiguous u USING (code_system, stripped)
        LEFT JOIN CONCEPT_RELATIONSHIP r
               ON r.concept_id_1 = CAST(c.source_concept_id AS VARCHAR)
              AND r.relationship_id = 'Maps to'
              AND (r.invalid_reason IS NULL OR r.invalid_reason = '')
        LEFT JOIN CONCEPT m ON m.concept_id = r.concept_id_2 AND m.standard_concept = 'S'
        WHERE c.standard_concept = 'S' OR m.concept_id IS NOT NULL
        -- See the first pass: `unambiguous` guarantees one source code, not one target,
        -- so without this the winner among several targets was the engine's choice.
        ORDER BY c.code_system, c.stripped, 3
        """
    ).fetchall()
    con.execute("DROP TABLE IF EXISTS _stripped")

    for (code_system, stripped, concept_id, name, domain, vocab, source_id,
         direct, competing) in rows:
        if concept_id is None:
            continue
        for term in by_stripped.get((code_system, stripped), ()):
            expected = DOMAIN_FOR_KIND.get(term.event_kind or "")
            if expected and domain != expected:
                continue
            if term.key in resolved:
                continue
            resolved[term.key] = ConceptMatch(
                concept_id=int(concept_id),
                concept_name=name or "",
                domain_id=domain or "",
                vocabulary_id=vocab or "",
                standard_concept="S",
                source_concept_id=None if direct else int(source_id),
                # Recorded distinctly, so a reviewer can see which mappings depended on
                # ignoring punctuation rather than on the code as written.
                path=_path(
                    "unpunctuated_code" if direct else "unpunctuated_mapped_relationship",
                    competing,
                ),
            )
    still = [t for t in pending if t.key not in resolved]
    return resolved, still


def resolve_terms(
    terms: Sequence[TermRequest], vocabulary, mappings: MappingRegistry
) -> tuple[dict[tuple[str, str], ConceptMatch], list[TermRequest]]:
    """Resolve terms deterministically. Whatever fails is returned, not guessed at."""
    resolved: dict[tuple[str, str], ConceptMatch] = {}
    unresolved: list[TermRequest] = []
    available = getattr(vocabulary, "available", False)
    for term in terms:
        approved = mappings.get(term.code_system, term.source_code)
        if approved is not None:
            if not available or not vocabulary.concept_exists(approved.concept_id):
                # See `_confirm`: with no vocabulary there is nothing to check the id
                # against, so the term stays unresolved rather than publishing a number
                # taken on trust.
                unresolved.append(term)
                continue
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
