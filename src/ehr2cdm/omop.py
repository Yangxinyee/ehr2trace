"""canonical -> OMOP CDM 5.4 (design section 6).

Ground rules, in the order they matter:

* the official DDL defines the core tables and this project does not touch it. Anything
  the converter needs to say beyond OMOP goes in the ``etl_audit`` schema;
* every published row has at least one source row behind it in ``etl_audit.lineage``;
* ``*_source_value`` keeps what the source said; ``*_concept_id`` accepts only concepts
  that exist in the local vocabulary with a compatible domain, and 0 otherwise --
  0 plus a preserved source value plus a review entry, never an invented id;
* the birth-year policy cannot be bypassed. Under ``strict`` a patient with no
  derivable year of birth is not published **at all** -- not to PERSON and not to any
  clinical table, because rows referencing a person who does not exist are not a CDM
  instance. Canonical and MEDS are unaffected, because they can state an age honestly
  and OMOP cannot. If that leaves the OMOP layer empty, that is the accurate report of
  what this export supports, and the blocker names what would change it.

The transformation runs as SQL inside the target database rather than as Python over
materialized rows. That is not a micro-optimization: the measurement table alone is
tens of millions of rows, and a builder that only works while the whole table fits in
memory is one that quietly stops working on the next export.

Judgements that need real parsing rules -- a dose string, a terminology lookup -- are
still made in Python, but **once per distinct value**, and joined back in. There is
exactly one implementation of each rule.

OMOP primary keys are 32-bit in the DDL while subject ids are 63-bit hashes, so this
layer assigns dense keys by a deterministic ordering and records the correspondence in
``etl_audit.lineage``. Ordering, rather than a counter, is what makes them reproducible
across runs and worker counts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.schema import EventKind, QualityFlag
from ehr2cdm.terminology import (
    DOMAIN_FOR_KIND,
    MappingRegistry,
    mappings_directory,
    TermRequest,
    Vocabulary,
    resolve_terms_batch,
)
from ehr2cdm.version import CODE_VERSION, DEFAULT_MAPPING_VERSION

DDL_DIR = Path(__file__).resolve().parents[2] / "sql" / "omop_5.4"
CDM_SCHEMA = "main"

ETL_AUDIT_DDL = """
CREATE SCHEMA IF NOT EXISTS etl_audit;

CREATE TABLE IF NOT EXISTS etl_audit.run (
    run_id VARCHAR, config_hash VARCHAR, code_version VARCHAR, mapping_version VARCHAR,
    vocabulary_version VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP,
    input_files VARCHAR, notes VARCHAR
);

-- Every published row points back at the event and the source rows behind it.
CREATE TABLE IF NOT EXISTS etl_audit.lineage (
    target_table VARCHAR, target_pk BIGINT, event_id VARCHAR, source_row_id VARCHAR,
    mapping_version VARCHAR
);

-- Extraction anchors: provenance, deliberately not a clinical fact.
CREATE TABLE IF NOT EXISTS etl_audit.anchor (
    anchor_id VARCHAR, subject_id BIGINT, person_id INTEGER, anchor_type VARCHAR,
    anchor_date DATE, anchor_time TIMESTAMP, anchor_time_known BOOLEAN,
    partition_id VARCHAR, source_row_id VARCHAR
);

-- Cohort membership: a directory label, never a diagnosis.
CREATE TABLE IF NOT EXISTS etl_audit.cohort_membership (
    subject_id BIGINT, person_id INTEGER, partition_id VARCHAR, batch VARCHAR,
    membership_label VARCHAR, anchor_id VARCHAR, label_scope VARCHAR, source_row_id VARCHAR
);

CREATE TABLE IF NOT EXISTS etl_audit.quality_issue (
    issue_type VARCHAR, severity VARCHAR, stage VARCHAR, subject_id BIGINT,
    source_row_id VARCHAR, event_id VARCHAR, partition_id VARCHAR, source_id VARCHAR,
    detail VARCHAR
);
"""


@dataclass
class BuildStats:
    tables: dict[str, int]
    distinct_terms: int
    resolved_terms: int
    unmapped: list[TermRequest]
    blocked_subjects: int


def build_omop(cfg: DatasetConfig, layout: WorkLayout, vocabulary_dir: Path | None = None) -> dict[str, Any]:
    import duckdb

    vocabulary = Vocabulary.open(vocabulary_dir or _env_vocabulary_dir())
    mappings = MappingRegistry.load(mappings_directory())

    db_path = layout.omop_dir / "omop.duckdb"
    if db_path.exists():
        # Rebuilt from scratch rather than updated in place: incremental writes are how
        # a published layer drifts away from the lineage that explains it.
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        _create_schema(con)
        _register_sources(con, layout)
        stats = _publish_all(con, cfg, layout, vocabulary, mappings)
        _publish_audit(con, cfg, layout, vocabulary, mappings)
    finally:
        con.close()

    pending = _write_pending(layout, stats.unmapped, vocabulary)
    result = {
        "database": str(db_path),
        "tables": stats.tables,
        "distinct_terms": stats.distinct_terms,
        "resolved_terms": stats.resolved_terms,
        "unmapped_terms": len(stats.unmapped),
        "pending_csv": str(pending) if pending else None,
        "vocabulary": vocabulary.version,
        "blocked_subjects": stats.blocked_subjects,
        "block_reason": (
            "person_birth_policy=strict and no age_as_of_date supplied; year_of_birth "
            "cannot be derived from an age alone"
            if stats.blocked_subjects
            else ""
        ),
        "lineage_rows": stats.tables.get("_lineage", 0),
    }
    (layout.omop_dir / "build_report.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    vocabulary.close()
    return result


def _create_schema(con) -> None:
    ddl = (DDL_DIR / "OMOPCDM_duckdb_5.4_ddl.sql").read_text(encoding="utf-8")
    con.execute(ddl.replace("@cdmDatabaseSchema", CDM_SCHEMA))
    con.execute(ETL_AUDIT_DDL)


def _register_sources(con, layout: WorkLayout) -> None:
    con.execute("PRAGMA preserve_insertion_order = false")
    con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{layout.canonical_path('events')}')")
    con.execute(f"CREATE VIEW lnk AS SELECT * FROM read_parquet('{layout.canonical_path('event_source')}')")
    con.execute(
        """
        CREATE TABLE pmap AS
        SELECT subject_id, CAST(row_number() OVER (ORDER BY subject_id) AS INTEGER) AS person_id
        FROM (SELECT DISTINCT subject_id FROM evt)
        """
    )


# --------------------------------------------------------------------------------
# lookups resolved once per distinct value
# --------------------------------------------------------------------------------


def _build_term_map(con, vocabulary, mappings: MappingRegistry,
                    drug_name_noise: Sequence[str] = ()) -> tuple[int, int, list[TermRequest]]:
    """Resolve every distinct source string once, not once per row.

    Millions of medication rows collapse to a few thousand distinct names. Dispatching
    by row instead would be the difference between a review queue a person can work
    through and one nobody ever will.
    """
    rows = con.execute(
        """
        SELECT code_system, source_code, min(source_name) AS source_name,
               min(event_kind) AS event_kind, count(*) AS occurrences
        FROM evt
        WHERE source_code IS NOT NULL
        GROUP BY code_system, source_code
        """
    ).fetchall()
    terms = [
        TermRequest(
            code_system=r[0] or "SOURCE",
            source_code=r[1],
            source_name=r[2],
            event_kind=r[3] or "",
            occurrences=int(r[4]),
        )
        for r in rows
    ]
    resolved, unresolved = resolve_terms_batch(terms, vocabulary, mappings, drug_name_noise)

    con.execute(
        # `path` is the route the mapping took -- exact code, punctuation-insensitive
        # code, an approved mapping, or one of the structured drug readings. Carried on
        # the row rather than recomputed, so a reviewer can ask the published database
        # which mappings depended on which rule instead of re-deriving it.
        "CREATE TABLE term_map (code_system VARCHAR, source_code VARCHAR, "
        "concept_id BIGINT, source_concept_id BIGINT, domain_id VARCHAR, path VARCHAR)"
    )
    payload = []
    for term in terms:
        match = resolved.get(term.key)
        if match is None:
            continue
        # One row per standard concept the source code maps to. OMOP's tables have a row
        # per concept, and a combination code asserts every one of its targets: keeping
        # only the first would make a cohort query for either fact miss patients who have
        # it. The join downstream fans out, which is the intended shape here and is why
        # MEDS builds its own single-pick map instead of sharing this one.
        for concept_id, domain in ((match.concept_id, match.domain_id), *match.alternates):
            payload.append(
                (term.code_system, term.source_code, int(concept_id),
                 int(match.source_concept_id or 0), domain or "", match.path)
            )
    if payload:
        con.executemany("INSERT INTO term_map VALUES (?, ?, ?, ?, ?, ?)", payload)
    return len(terms), len(resolved), unresolved


def _build_dose_map(con) -> None:
    """Parse each distinct dose string once, with the same parser the rest uses."""
    from ehr2cdm.canonical.values import parse_value
    from ehr2cdm.errors import QuarantineRow

    con.execute(
        "CREATE TABLE dose_map (dose_source VARCHAR, quantity DOUBLE, dose_unit VARCHAR)"
    )
    rows = con.execute(
        "SELECT DISTINCT dose_source FROM evt WHERE dose_source IS NOT NULL"
    ).fetchall()
    payload = []
    for (dose,) in rows:
        try:
            parsed = parse_value(dose)
        except QuarantineRow:
            payload.append((dose, None, None))
            continue
        payload.append((dose, parsed.number, parsed.unit))
    if payload:
        con.executemany("INSERT INTO dose_map VALUES (?, ?, ?)", payload)


def _build_attribute_map(con, vocabulary, mappings: MappingRegistry) -> None:
    """Gender, race and ethnicity values resolved once each."""
    con.execute("CREATE TABLE attr_map (attr VARCHAR, value_text VARCHAR, concept_id BIGINT)")
    rows = con.execute(
        """
        SELECT DISTINCT upper(source_code) AS attr, value_text
        FROM evt
        WHERE event_kind = 'demographic' AND upper(source_code) IN ('GENDER', 'RACE', 'ETHNICITY')
          AND value_text IS NOT NULL
        """
    ).fetchall()
    domains = {"GENDER": "Gender", "RACE": "Race", "ETHNICITY": "Ethnicity"}
    payload = []
    for attr, value in rows:
        approved = mappings.get(attr, value)
        if approved is not None:
            payload.append((attr, value, int(approved.concept_id)))
            continue
        match = vocabulary.lookup_name(domains[attr], value)
        payload.append((attr, value, int(match.concept_id) if match else 0))
    if payload:
        con.executemany("INSERT INTO attr_map VALUES (?, ?, ?)", payload)


def _type_concept_map(con, mappings: MappingRegistry) -> None:
    """Fixed type concepts, from the git-tracked registry rather than from code.

    An id typed into a source file has no provenance and nobody revalidates it when the
    vocabulary is updated, so there are none here; an unresolved key is 0.
    """
    keys = (
        "visit",
        "condition_problem_list",
        "drug_order",
        "drug_admin",
        "procedure",
        "measurement",
        "note",
        "note_class",
        "death",
        "observation",
        "observation_period",
    )
    con.execute("CREATE TABLE type_concept (key VARCHAR, concept_id BIGINT)")
    con.executemany(
        "INSERT INTO type_concept VALUES (?, ?)",
        [(k, (mappings.get("TYPE_CONCEPT", k).concept_id if mappings.get("TYPE_CONCEPT", k) else 0)) for k in keys],
    )


def _type_id(con, key: str) -> int:
    row = con.execute("SELECT concept_id FROM type_concept WHERE key = ?", [key]).fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------------

#: ``event_id`` travels with every staged row so lineage can be produced by a join,
#: then is dropped before the row reaches a core table.
STAGE_EXTRA = "event_id"


def _publish_all(con, cfg: DatasetConfig, layout: WorkLayout, vocabulary, mappings: MappingRegistry) -> BuildStats:
    distinct, resolved, unmapped = _build_term_map(
        con, vocabulary, mappings, cfg.terminology.drug_name_noise
    )
    _build_dose_map(con)
    _build_attribute_map(con, vocabulary, mappings)
    _type_concept_map(con, mappings)

    counts: dict[str, int] = {}
    blocked = _publish_person(con, cfg)
    counts["person"] = _count(con, "person")

    counts["visit_occurrence"] = _stage_and_load(con, "visit_occurrence", "visit_occurrence_id", _visit_sql(con))
    con.execute(
        """
        CREATE TABLE visit_lookup AS
        SELECT person_id, visit_source_value, min(visit_occurrence_id) AS visit_occurrence_id
        FROM visit_occurrence WHERE visit_source_value IS NOT NULL
        GROUP BY person_id, visit_source_value
        """
    )

    counts["condition_occurrence"] = _stage_and_load(
        con, "condition_occurrence", "condition_occurrence_id", _condition_sql(con)
    )
    counts["drug_exposure"] = _stage_and_load(con, "drug_exposure", "drug_exposure_id", _drug_sql(con))
    counts["procedure_occurrence"] = _stage_and_load(
        con, "procedure_occurrence", "procedure_occurrence_id", _procedure_sql(con)
    )
    counts["measurement"] = _stage_and_load(con, "measurement", "measurement_id", _measurement_sql(con, cfg))
    counts["observation"] = _stage_and_load(con, "observation", "observation_id", _observation_sql(con))
    counts["note"] = _stage_and_load(con, "note", "note_id", _note_sql(con))
    counts["death"] = _publish_death(con)
    counts["observation_period"] = _publish_observation_periods(con)
    counts["cdm_source"] = _publish_cdm_source(con, cfg, vocabulary)
    counts["_lineage"] = _count(con, "etl_audit.lineage")
    return BuildStats(counts, distinct, resolved, unmapped, blocked)


def _stage_and_load(con, table: str, pk: str, select_sql: str) -> int:
    """Stage rows, load the core columns, then derive lineage from the same staging.

    One controlled load path per table, executed once. Never concurrent inserts, and
    never a core row that was written before its lineage could be.
    """
    con.execute(f"CREATE OR REPLACE TEMP TABLE stage AS {select_sql}")
    columns = [c for c in _columns(con, "stage") if c != STAGE_EXTRA]
    con.execute(f"INSERT INTO {table} SELECT {', '.join(columns)} FROM stage")
    con.execute(
        f"""
        INSERT INTO etl_audit.lineage
        SELECT '{table}', s.{pk}, s.event_id, l.source_row_id, '{DEFAULT_MAPPING_VERSION}'
        FROM stage s JOIN lnk l ON l.event_id = s.event_id
        """
    )
    n = _count(con, table)
    con.execute("DROP TABLE stage")
    return n


def _publish_person(con, cfg: DatasetConfig) -> int:
    """PERSON, subject to the birth-year policy.

    OMOP requires a year of birth. This export carries an age and no date it was taken
    on, so under the default policy the patient is not published here at all. A number
    the database accepts is not the same thing as a fact about a person.

    Two different ages for one patient -- routine when an export is taken twice --
    cannot both be right against a single reference date, so that patient is blocked
    and the disagreement is recorded rather than resolved by picking one.
    """
    policy = cfg.omop.person_birth_policy
    con.execute(
        """
        CREATE TABLE person_attrs AS
        SELECT e.subject_id,
               min(CASE WHEN upper(e.source_code) = 'GENDER' THEN e.value_text END) AS gender_source_value,
               min(CASE WHEN upper(e.source_code) = 'RACE' THEN e.value_text END) AS race_source_value,
               min(CASE WHEN upper(e.source_code) = 'ETHNICITY' THEN e.value_text END) AS ethnicity_source_value,
               min(CASE WHEN upper(e.source_code) = 'AGE' THEN e.value_number END) AS age,
               count(DISTINCT CASE WHEN upper(e.source_code) = 'AGE' THEN e.value_number END) AS age_variants,
               min(CASE WHEN upper(e.source_code) = 'BIRTH_DATE' THEN e.value_text END) AS birth_date,
               count(DISTINCT CASE WHEN upper(e.source_code) = 'BIRTH_DATE' THEN e.value_text END) AS birth_date_variants
        FROM evt e
        WHERE e.event_kind = 'demographic'
        GROUP BY e.subject_id
        """
    )
    con.execute(
        """
        INSERT INTO etl_audit.quality_issue
        SELECT 'AGE_CONFLICT', 'error', 'omop', subject_id, NULL, NULL, NULL, NULL,
               'the sources record more than one age and no reference date for either'
        FROM person_attrs WHERE age_variants > 1
        """
    )

    # A real date of birth is a fact and needs no policy. Where the source carries one,
    # it is used under either mode -- including strict, which exists to publish when the
    # data supports it and refuse when it does not, not to refuse unconditionally. This
    # was wrong until a dataset that supplies one arrived: `birth_date` was a declared
    # field role that nothing read, so a patient with a recorded date of birth was
    # withheld exactly like one with nothing at all.
    from_date = "CAST(year(CAST(a.birth_date AS DATE)) AS INTEGER)"
    has_date = "a.birth_date IS NOT NULL AND a.birth_date_variants = 1"

    if policy.mode == "approved_approximation":
        reference_year = datetime.strptime(policy.age_as_of_date, "%Y-%m-%d").year
        # The date wins where both exist: an age plus a reference year is an
        # approximation of what the date states outright.
        birth_expr = (
            f"CASE WHEN {has_date} THEN {from_date} "
            f"ELSE CAST({reference_year} - a.age AS INTEGER) END"
        )
        eligible = f"{has_date} OR (a.age IS NOT NULL AND a.age_variants = 1)"
    else:
        # Strict: a recorded birth date, or nothing. An age alone cannot produce a
        # defensible year of birth without a reference date, and there is none.
        birth_expr = f"CASE WHEN {has_date} THEN {from_date} ELSE CAST(NULL AS INTEGER) END"
        eligible = has_date

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stage AS
        SELECT p.person_id,
               CAST(coalesce(g.concept_id, 0) AS INTEGER) AS gender_concept_id,
               {birth_expr} AS year_of_birth,
               CAST(NULL AS INTEGER) AS month_of_birth,
               CAST(NULL AS INTEGER) AS day_of_birth,
               CAST(NULL AS TIMESTAMP) AS birth_datetime,
               CAST(coalesce(r.concept_id, 0) AS INTEGER) AS race_concept_id,
               CAST(coalesce(t.concept_id, 0) AS INTEGER) AS ethnicity_concept_id,
               CAST(NULL AS INTEGER) AS location_id,
               CAST(NULL AS INTEGER) AS provider_id,
               CAST(NULL AS INTEGER) AS care_site_id,
               -- deliberately empty: no direct patient identifier is published
               CAST(NULL AS VARCHAR) AS person_source_value,
               a.gender_source_value,
               0 AS gender_source_concept_id,
               a.race_source_value,
               0 AS race_source_concept_id,
               a.ethnicity_source_value,
               0 AS ethnicity_source_concept_id
        FROM person_attrs a
        JOIN pmap p ON p.subject_id = a.subject_id
        LEFT JOIN attr_map g ON g.attr = 'GENDER' AND g.value_text = a.gender_source_value
        LEFT JOIN attr_map r ON r.attr = 'RACE' AND r.value_text = a.race_source_value
        LEFT JOIN attr_map t ON t.attr = 'ETHNICITY' AND t.value_text = a.ethnicity_source_value
        WHERE {eligible}
        """
    )
    columns = _columns(con, "stage")
    con.execute(f"INSERT INTO person SELECT {', '.join(columns)} FROM stage")
    con.execute(
        f"""
        INSERT INTO etl_audit.lineage
        SELECT 'person', s.person_id, e.event_id, l.source_row_id, '{DEFAULT_MAPPING_VERSION}'
        FROM stage s
        JOIN pmap p ON p.person_id = s.person_id
        JOIN evt e ON e.subject_id = p.subject_id AND e.event_kind = 'demographic'
        JOIN lnk l ON l.event_id = e.event_id
        """
    )
    published = _count(con, "person")
    if policy.mode == "approved_approximation":
        # Only the years actually estimated from an age. A patient whose record carries
        # a real date of birth is not an approximation, and flagging them as one would
        # misreport the very thing the flag exists to make visible.
        con.execute(
            f"""
            INSERT INTO etl_audit.quality_issue
            SELECT 'DERIVED_APPROXIMATE_BIRTH_YEAR', 'warning', 'omop', p.subject_id, NULL, NULL,
                   NULL, NULL,
                   'year_of_birth estimated from an age with no birthday known; may be off by one year'
            FROM person s
            JOIN pmap p ON p.person_id = s.person_id
            JOIN person_attrs a ON a.subject_id = p.subject_id
            WHERE NOT ({has_date})
            """
        )
    total = int(con.execute("SELECT count(*) FROM person_attrs").fetchone()[0])
    blocked = total - published
    if blocked:
        con.execute(
            f"""
            INSERT INTO etl_audit.quality_issue
            SELECT 'OMOP_PERSON_BLOCKED', 'error', 'omop', a.subject_id, NULL, NULL, NULL,
                   'demographics',
                   'no derivable year_of_birth under person_birth_policy={policy.mode}'
            FROM person_attrs a
            JOIN pmap p ON p.subject_id = a.subject_id
            WHERE p.person_id NOT IN (SELECT person_id FROM person)
            """
        )
    con.execute("DROP TABLE stage")
    return blocked


#: Which OMOP table a mapped fact belongs in. OMOP decides this by the standard
#: concept's domain rather than by the source column, and OBSERVATION is where a fact
#: that is not a condition, drug, procedure or measurement goes -- a family history, a
#: screening encounter, a socioeconomic factor. A domain with no table of its own here
#: lands in OBSERVATION too, which is the convention and not a fallback of last resort.
DOMAIN_TABLE = {
    "Condition": "condition_occurrence",
    "Drug": "drug_exposure",
    "Procedure": "procedure_occurrence",
    "Measurement": "measurement",
    "Observation": "observation",
}

#: Event kinds whose rows are routed by domain. Visits, notes and deaths are built from
#: their own structure rather than from a mapped code, so they are not re-routed: a
#: visit's table is decided by it being a visit.
ROUTED_KINDS = ("condition", "drug_order", "drug_admin", "procedure", "measurement", "demographic")

#: The term_map join used by every routed table. A concept whose domain has no table
#: here cannot be published faithfully anywhere -- OMOP 5.4 has an EPISODE table and this
#: converter does not write one, so `Remission` and `Progression` have nowhere to go --
#: and the tempting answer is to leave such a row in whichever table its source column
#: implied. That is precisely the thing the old domain gate existed to prevent: it puts a
#: non-Condition concept in condition_concept_id, and OMOP_CONCEPTS_EXIST_AND_FIT_THEIR
#: _DOMAIN catches it, as it did the first time this routing ran.
#:
#: So the concept is not used at all. The row is published with concept_id 0, its source
#: value intact, and it stays in the review queue -- the same treatment as a code the
#: vocabulary never resolved, which is what it amounts to here.
ROUTABLE_JOIN = (
    "LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code\n"
    "                    AND m.domain_id IN ("
    + ", ".join(f"'{d}'" for d in DOMAIN_TABLE)
    + ")"
)


def _routes_here(table: str, kinds: tuple[str, ...]) -> str:
    """The WHERE clause deciding whether an event belongs in ``table``.

    Three cases, and the middle one is the whole point:

    * unmapped (no concept, or a domain with no table): it stays where its source column
      put it, because nothing better is known -- a `concept_id = 0` row still has to live
      somewhere, and the column it came from is the honest guess;
    * mapped, and its domain names this table: it lands here, whichever column it came
      from;
    * mapped, and its domain names another table: it is not this table's row.
    """
    kind_list = ", ".join(f"'{k}'" for k in kinds)
    domains = ", ".join(f"'{d}'" for d in DOMAIN_TABLE)
    mine = ", ".join(f"'{d}'" for d, tb in DOMAIN_TABLE.items() if tb == table)
    unrouted = f"(m.domain_id IS NULL OR m.domain_id NOT IN ({domains}))"
    return (
        f"(e.event_kind IN ({kind_list}) AND {unrouted})"
        f" OR (e.event_kind IN ({', '.join(repr(k) for k in ROUTED_KINDS)})"
        f" AND m.domain_id IN ({mine}))"
    )


def _visit_sql(con) -> str:
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS visit_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS visit_concept_id,
               CAST(e.event_time AS DATE) AS visit_start_date,
               e.event_time AS visit_start_datetime,
               CAST(coalesce(e.end_time, e.event_time) AS DATE) AS visit_end_date,
               coalesce(e.end_time, e.event_time) AS visit_end_datetime,
               {_type_id(con, 'visit')} AS visit_type_concept_id,
               CAST(NULL AS INTEGER) AS provider_id,
               CAST(NULL AS INTEGER) AS care_site_id,
               substr(e.encounter_id, 1, 50) AS visit_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS visit_source_concept_id,
               0 AS admitted_from_concept_id,
               CAST(NULL AS VARCHAR) AS admitted_from_source_value,
               0 AS discharged_to_concept_id,
               CAST(NULL AS VARCHAR) AS discharged_to_source_value,
               CAST(NULL AS INTEGER) AS preceding_visit_occurrence_id,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
        WHERE e.event_kind = '{EventKind.visit}'
    """


def _condition_sql(con) -> str:
    # The source says when a problem was first noted. That is not an onset date, and
    # the type concept is what keeps this row from claiming one.
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS condition_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS condition_concept_id,
               CAST(e.event_time AS DATE) AS condition_start_date,
               e.event_time AS condition_start_datetime,
               CAST(NULL AS DATE) AS condition_end_date,
               CAST(NULL AS TIMESTAMP) AS condition_end_datetime,
               {_type_id(con, 'condition_problem_list')} AS condition_type_concept_id,
               0 AS condition_status_concept_id,
               CAST(NULL AS VARCHAR) AS stop_reason,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS condition_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS condition_source_concept_id,
               substr(e.status_source, 1, 50) AS condition_status_source_value,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        {ROUTABLE_JOIN}
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE {_routes_here('condition_occurrence', (str(EventKind.condition),))}
    """


def _drug_sql(con) -> str:
    """Orders and administrations share a table; the type concept keeps them apart.

    The source status of an order has no home in the OMOP core, so it stays on the
    canonical event that the lineage points at. It is not dropped, and it is not forced
    into ``stop_reason``, which means something else.
    """
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS drug_exposure_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS drug_concept_id,
               CAST(e.event_time AS DATE) AS drug_exposure_start_date,
               e.event_time AS drug_exposure_start_datetime,
               CAST(coalesce(e.end_time, e.event_time) AS DATE) AS drug_exposure_end_date,
               coalesce(e.end_time, e.event_time) AS drug_exposure_end_datetime,
               CAST(NULL AS DATE) AS verbatim_end_date,
               CASE WHEN e.event_kind = '{EventKind.drug_order}'
                    THEN {_type_id(con, 'drug_order')} ELSE {_type_id(con, 'drug_admin')} END
                    AS drug_type_concept_id,
               CAST(NULL AS VARCHAR) AS stop_reason,
               CAST(NULL AS INTEGER) AS refills,
               d.quantity,
               CAST(NULL AS INTEGER) AS days_supply,
               CASE WHEN d.quantity IS NULL THEN substr(e.dose_source, 1, 250) END AS sig,
               0 AS route_concept_id,
               CAST(NULL AS VARCHAR) AS lot_number,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS drug_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS drug_source_concept_id,
               substr(e.route_source, 1, 50) AS route_source_value,
               substr(d.dose_unit, 1, 50) AS dose_unit_source_value,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        {ROUTABLE_JOIN}
        LEFT JOIN dose_map d ON d.dose_source = e.dose_source
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE {_routes_here('drug_exposure', (str(EventKind.drug_order), str(EventKind.drug_admin)))}
    """


def _procedure_sql(con) -> str:
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS procedure_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS procedure_concept_id,
               CAST(e.event_time AS DATE) AS procedure_date,
               e.event_time AS procedure_datetime,
               CAST(NULL AS DATE) AS procedure_end_date,
               CAST(NULL AS TIMESTAMP) AS procedure_end_datetime,
               {_type_id(con, 'procedure')} AS procedure_type_concept_id,
               0 AS modifier_concept_id,
               CAST(NULL AS INTEGER) AS quantity,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS procedure_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS procedure_source_concept_id,
               CAST(NULL AS VARCHAR) AS modifier_source_value,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        {ROUTABLE_JOIN}
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE {_routes_here('procedure_occurrence', (str(EventKind.procedure),))}
    """


def _measurement_sql(con, cfg: DatasetConfig) -> str:
    """MEASUREMENT, with the value rules from design section 5.4.

    A ranged result keeps its original text in ``value_source_value`` with
    ``value_as_number`` empty. It does **not** go to ``range_low``/``range_high``:
    those are the reference range for the test, and a patient's own value there would
    be a different clinical claim. Reference ranges come from configuration, because
    the source's own range columns are empty throughout this export.
    """
    ranges = cfg.reference_ranges
    if ranges:
        cases_low = " ".join(
            f"WHEN e.source_code = '{code}' THEN {r.low if r.low is not None else 'NULL'}"
            for code, r in ranges.items()
        )
        cases_high = " ".join(
            f"WHEN e.source_code = '{code}' THEN {r.high if r.high is not None else 'NULL'}"
            for code, r in ranges.items()
        )
        range_low = f"CASE {cases_low} ELSE NULL END"
        range_high = f"CASE {cases_high} ELSE NULL END"
    else:
        range_low = range_high = "CAST(NULL AS DOUBLE)"

    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS measurement_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS measurement_concept_id,
               CAST(e.event_time AS DATE) AS measurement_date,
               e.event_time AS measurement_datetime,
               CAST(NULL AS VARCHAR) AS measurement_time,
               {_type_id(con, 'measurement')} AS measurement_type_concept_id,
               0 AS operator_concept_id,
               e.value_number AS value_as_number,
               0 AS value_as_concept_id,
               0 AS unit_concept_id,
               {range_low} AS range_low,
               {range_high} AS range_high,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS measurement_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS measurement_source_concept_id,
               substr(e.unit_source, 1, 50) AS unit_source_value,
               0 AS unit_source_concept_id,
               substr(coalesce(
                   e.value_text,
                   CASE WHEN e.value_low IS NOT NULL AND e.value_high IS NOT NULL
                        THEN CAST(e.value_low AS VARCHAR) || '-' || CAST(e.value_high AS VARCHAR) END,
                   CAST(e.value_number AS VARCHAR)
               ), 1, 50) AS value_source_value,
               CAST(NULL AS BIGINT) AS measurement_event_id,
               CAST(NULL AS INTEGER) AS meas_event_field_concept_id,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        {ROUTABLE_JOIN}
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE {_routes_here('measurement', (str(EventKind.measurement),))}
    """


def _observation_sql(con) -> str:
    """Facts that are not conditions, drugs, procedures or measurements.

    Almost everything that lands here is a Z code: a family history, a screening
    encounter, a socioeconomic factor. None of them is a diagnosis, and until the
    routing existed none of them was published at all -- 5,737 terms carrying 1,944,952
    rows sat in a review queue that no reviewer could have emptied, because the question
    was never "what does this code mean" but "which table does this kind of fact go in".

    No row reaches this table without a mapped concept: an unmapped event stays in the
    table its source column implies, so nothing arrives here merely because it failed to
    resolve elsewhere.
    """
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS observation_id,
               p.person_id,
               CAST(m.concept_id AS INTEGER) AS observation_concept_id,
               CAST(e.event_time AS DATE) AS observation_date,
               e.event_time AS observation_datetime,
               {_type_id(con, 'observation')} AS observation_type_concept_id,
               e.value_number AS value_as_number,
               substr(e.value_text, 1, 60) AS value_as_string,
               0 AS value_as_concept_id,
               0 AS qualifier_concept_id,
               0 AS unit_concept_id,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS observation_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS observation_source_concept_id,
               substr(e.unit_source, 1, 50) AS unit_source_value,
               CAST(NULL AS VARCHAR) AS qualifier_source_value,
               substr(coalesce(e.value_text, CAST(e.value_number AS VARCHAR)), 1, 50) AS value_source_value,
               CAST(NULL AS BIGINT) AS observation_event_id,
               CAST(NULL AS INTEGER) AS obs_event_field_concept_id,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE e.event_kind IN ({", ".join(repr(k) for k in ROUTED_KINDS)})
          AND m.domain_id = 'Observation'
          AND e.event_time IS NOT NULL
    """


def _note_sql(con) -> str:
    # note_text is verbatim. Any later extraction is a separate event with its own
    # lineage and never edits this text.
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS note_id,
               p.person_id,
               CAST(e.event_time AS DATE) AS note_date,
               e.event_time AS note_datetime,
               {_type_id(con, 'note')} AS note_type_concept_id,
               {_type_id(con, 'note_class')} AS note_class_concept_id,
               substr(e.source_name, 1, 250) AS note_title,
               coalesce(e.value_text, '') AS note_text,
               0 AS encoding_concept_id,
               0 AS language_concept_id,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS note_source_value,
               CAST(NULL AS BIGINT) AS note_event_id,
               CAST(NULL AS INTEGER) AS note_event_field_concept_id,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        WHERE e.event_kind = '{EventKind.note}'
    """


def _publish_death(con) -> int:
    """DEATH, one row per person, and only where the sources agree.

    Where they agree the event already collapsed to one by content. Where they
    disagree, both survive in canonical and neither is published here: choosing the
    earlier or the later date would be inventing the answer to a question a human has
    to settle.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE death_candidates AS
        SELECT e.subject_id, count(DISTINCT e.event_time) AS variants,
               min(e.event_time) AS death_time, min(e.event_id) AS event_id
        FROM evt e WHERE e.event_kind = '{EventKind.death}' AND e.event_time IS NOT NULL
        GROUP BY e.subject_id
        """
    )
    con.execute(
        """
        INSERT INTO etl_audit.quality_issue
        SELECT 'DEATH_DATE_CONFLICT', 'error', 'omop', subject_id, NULL, event_id, NULL, NULL,
               'sources disagree about the death date; no DEATH row was published'
        FROM death_candidates WHERE variants > 1
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stage AS
        SELECT p.person_id,
               CAST(d.death_time AS DATE) AS death_date,
               d.death_time AS death_datetime,
               {_type_id(con, 'death')} AS death_type_concept_id,
               0 AS cause_concept_id,
               CAST(NULL AS VARCHAR) AS cause_source_value,
               0 AS cause_source_concept_id,
               d.event_id
        FROM death_candidates d
        JOIN pmap p ON p.subject_id = d.subject_id
        JOIN person pr ON pr.person_id = p.person_id
        WHERE d.variants = 1
        """
    )
    columns = [c for c in _columns(con, "stage") if c != STAGE_EXTRA]
    con.execute(f"INSERT INTO death SELECT {', '.join(columns)} FROM stage")
    con.execute(
        f"""
        INSERT INTO etl_audit.lineage
        SELECT 'death', s.person_id, s.event_id, l.source_row_id, '{DEFAULT_MAPPING_VERSION}'
        FROM stage s JOIN lnk l ON l.event_id = s.event_id
        """
    )
    n = _count(con, "death")
    con.execute("DROP TABLE stage")
    return n


def _publish_observation_periods(con) -> int:
    """OBSERVATION_PERIOD from an explicit, versioned heuristic.

    There is no enrolment data here, so the period is the span of trustworthy dated
    events. Records dated after death are excluded from the span but not deleted, and
    the rule carries a version so that changing it is visible rather than silent.
    """
    con.execute(
        f"""
        INSERT INTO observation_period
        SELECT CAST(row_number() OVER (ORDER BY p.person_id) AS INTEGER),
               p.person_id,
               CAST(min(e.event_time) AS DATE),
               CAST(max(e.event_time) AS DATE),
               {_type_id(con, 'observation_period')}
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        WHERE e.event_time IS NOT NULL
          AND NOT list_contains(e.quality_flags, '{QualityFlag.RECORDED_AFTER_DEATH}')
        GROUP BY p.person_id
        """
    )
    return _count(con, "observation_period")


def _publish_cdm_source(con, cfg: DatasetConfig, vocabulary) -> int:
    con.execute(
        "INSERT INTO cdm_source VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cfg.omop.source_name or cfg.dataset_id,
            cfg.dataset_id[:25],
            cfg.omop.cdm_holder or "unspecified (research extract; holder not declared)",
            cfg.description,
            "PATIENT_CDM_AGENT_SYSTEM_DESIGN.md",
            f"ehr2cdm {CODE_VERSION}, config {cfg.config_hash()[:12]}",
            datetime.strptime(cfg.omop.source_release_date, "%Y-%m-%d").date()
            if cfg.omop.source_release_date
            else date.today(),
            date.today(),
            cfg.omop.cdm_version,
            0,
            (vocabulary.version or "none")[:20],
        ],
    )
    return 1


def _publish_audit(con, cfg: DatasetConfig, layout: WorkLayout, vocabulary, mappings: MappingRegistry) -> None:
    """Load the audit tables: anchors, cohort membership, quality issues, the run row.

    Columns are named explicitly rather than taken positionally. These tables carry the
    provenance that explains every published row, and a silent column shift here would
    misattribute it.
    """
    anchors = layout.canonical_path("anchors")
    if anchors.exists():
        con.execute(
            f"""
            INSERT INTO etl_audit.anchor
            SELECT s.anchor_id, s.subject_id, p.person_id, s.anchor_type, s.anchor_date,
                   s.anchor_time, s.anchor_time_known, s.partition_id, s.source_row_id
            FROM read_parquet('{anchors}') s
            LEFT JOIN pmap p ON p.subject_id = s.subject_id
            """
        )
    memberships = layout.canonical_path("cohort_membership")
    if memberships.exists():
        con.execute(
            f"""
            INSERT INTO etl_audit.cohort_membership
            SELECT s.subject_id, p.person_id, s.partition_id, s.batch, s.membership_label,
                   s.anchor_id, s.label_scope, s.source_row_id
            FROM read_parquet('{memberships}') s
            LEFT JOIN pmap p ON p.subject_id = s.subject_id
            """
        )
    issues = layout.canonical_path("quality_issue")
    if issues.exists():
        con.execute(
            f"""
            INSERT INTO etl_audit.quality_issue
            SELECT issue_type, severity, stage, subject_id, source_row_id, event_id,
                   partition_id, source_id, detail
            FROM read_parquet('{issues}')
            """
        )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    con.execute(
        "INSERT INTO etl_audit.run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            f"omop-{now.strftime('%Y%m%dT%H%M%SZ')}",
            cfg.config_hash(),
            CODE_VERSION,
            mappings.version,
            vocabulary.version,
            now,
            now,
            str(layout.manifest_dir / "inputs.json"),
            ""
            if vocabulary.available
            else "no vocabulary available: every concept_id is 0 and every term is in review",
        ],
    )


# --------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------


def _columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]


def _count(con, table: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _env_vocabulary_dir() -> Path | None:
    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None


def _write_pending(layout: WorkLayout, unresolved: Sequence[TermRequest], vocabulary) -> Path | None:
    """Unmapped terms go to the review queue rather than silently becoming 0."""
    if not unresolved:
        return None
    from ehr2cdm.review import write_pending

    # Candidates are deliberately *not* computed here. Lexical recall is a scan of the
    # whole concept table per term, and publishing should not spend hours building a
    # review queue nobody has asked for yet. `ehr2cdm propose` does recall on demand,
    # for as many of the most frequent terms as a reviewer intends to work through.
    items = []
    for term in sorted(unresolved, key=lambda t: (-t.occurrences, t.source_code)):
        candidates: list = []
        items.append(
            {
                "kind": "terminology",
                "code_system": term.code_system,
                "source_string": term.source_code,
                "source_name": term.source_name or "",
                "event_kind": term.event_kind,
                "occurrences": term.occurrences,
                "candidates": json.dumps(
                    [
                        {"concept_id": c.concept_id, "concept_name": c.concept_name, "score": c.score}
                        for c in candidates
                    ]
                ),
                "context": f"domain={DOMAIN_FOR_KIND.get(term.event_kind) or ''}",
            }
        )
    # This is the only caller holding the complete current unmapped set, so it is the
    # only one allowed to retire what the vocabulary has since resolved. Without this
    # the queue only ever grows: terms mapped by a later run stay listed forever and a
    # reviewer works through items that no longer need reviewing.
    return write_pending(layout, items, retire_absent=True)
