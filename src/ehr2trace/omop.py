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

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind, QualityFlag
from ehr2trace.terminology import (
    DOMAIN_FOR_KIND,
    MappingRegistry,
    mappings_directory,
    TermRequest,
    Vocabulary,
    resolve_terms_batch,
)
from ehr2trace.version import CODE_VERSION, DEFAULT_MAPPING_VERSION

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
    vocabulary = Vocabulary.open(vocabulary_dir or _env_vocabulary_dir())
    mappings = MappingRegistry.load(mappings_directory())

    db_path = layout.omop_dir / "omop.duckdb"
    if db_path.exists():
        # Rebuilt from scratch rather than updated in place: incremental writes are how
        # a published layer drifts away from the lineage that explains it.
        db_path.unlink()
    con = _connect(db_path)
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
                    drug_name_noise: Sequence[str] = (),
                    drug_name_truncated_at: int | None = None) -> tuple[int, int, list[TermRequest]]:
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
    resolved, unresolved = resolve_terms_batch(
        terms, vocabulary, mappings, drug_name_noise, drug_name_truncated_at)

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
        _bulk_insert(con, "term_map", payload, ("code_system", "source_code", "concept_id", "source_concept_id", "domain_id", "path"))
    return len(terms), len(resolved), unresolved


#: What OMOP's ``quantity NUMERIC`` can hold: DuckDB reads that as DECIMAL(18,3), which
#: leaves fifteen digits ahead of the point.
QUANTITY_LIMIT = 10**15


def _known_units() -> frozenset[str] | None:
    """The unit spellings a ``number + unit`` cell may end in, or None if there is no table.

    Remediation plan T1.9: the value parser accepts a numeric cell with a trailing word
    as a number and a unit only when the word is in the unit table under
    ``reference/units/``. A dose string is parsed by the same rule, so a dose whose tail
    is not a unit stays whole in ``sig`` instead of becoming a quantity with an invented
    unit. Without the table -- a checkout that predates it -- every tail is accepted, as
    it was before, and the returned None says so.

    TODO(remediation T1.9, reconcile at merge): the core branch adds
    ``ehr2trace.reference.load_units()``, the merged ``reference/units/*.csv`` table
    keyed by source spelling (compared case-insensitively) with the UCUM code as value.
    Replace the guarded import with a plain one once that branch is in, and if the core
    branch spells the rule as a ``parse_value`` argument instead, pass the table there.
    """
    try:
        from ehr2trace.reference import load_units
    except ImportError:
        return None
    return frozenset(str(spelling).strip().lower() for spelling in load_units())


def _build_dose_map(con) -> None:
    """Parse each distinct dose string once, with the same parser the rest uses.

    A dose too large for the target column is published as a null quantity with its
    source string kept in ``sig`` -- the treatment a dose that never parsed already
    gets. MIMIC-IV's ``emar_detail`` has sixteen rows whose ``dose_given`` is an
    eighteen-digit integer, which reads as an identifier that leaked into a measurement
    column; 500110360505613004 mcg is not a dose anyone administered. Rounding it to
    fit would invent a quantity, and failing the build would lose the other 34 million.

    A dose whose trailing word is not in the unit table gets the same treatment (see
    :func:`_known_units`): no quantity, and the string kept whole.
    """
    from ehr2trace.canonical.values import parse_value
    from ehr2trace.errors import QuarantineRow

    con.execute(
        "CREATE TABLE dose_map (dose_source VARCHAR, quantity DOUBLE, dose_unit VARCHAR)"
    )
    rows = con.execute(
        "SELECT DISTINCT dose_source FROM evt WHERE dose_source IS NOT NULL"
    ).fetchall()
    known = _known_units()
    payload = []
    for (dose,) in rows:
        try:
            parsed = parse_value(dose)
        except QuarantineRow:
            payload.append((dose, None, None))
            continue
        number, unit = parsed.number, parsed.unit
        if (
            parsed.form == "number_unit"
            and known is not None
            and (unit or "").strip().lower() not in known
        ):
            number, unit = None, None
        if number is not None and abs(number) >= QUANTITY_LIMIT:
            number = None
        payload.append((dose, number, unit))
    if payload:
        _bulk_insert(con, "dose_map", payload, ("dose_source", "quantity", "dose_unit"))


def _build_unit_map(con, vocabulary, mappings: MappingRegistry) -> None:
    """Unit concept per distinct (source spelling, UCUM code) pair (T1.5, P-C3).

    The audit found every measurement carrying ``unit_concept_id = 0`` because the
    column was a literal in the SQL. The concept now comes, in this order, from an
    approved mapping of the source spelling, an approved mapping of the normalized
    UCUM code (``mappings/unit.csv``, code system ``UNIT``), the vocabulary's own UCUM
    entry for the normalized code, or that entry for the source spelling; and it is
    used only if the vocabulary places it in the Unit domain. Anything else is 0 with
    the source spelling preserved, like every other concept in this layer.

    Resolved once per distinct pair rather than per row: a hundred million measurements
    carry a few hundred spellings.
    """
    con.execute(
        "CREATE TABLE unit_map (unit_source VARCHAR, unit_normalized VARCHAR, concept_id BIGINT)"
    )
    normalized = _optional(_evt_columns(con), "unit_normalized", "VARCHAR")
    rows = con.execute(
        f"""
        SELECT DISTINCT e.unit_source, {normalized} AS unit_normalized
        FROM evt e
        WHERE e.unit_source IS NOT NULL OR {normalized} IS NOT NULL
        ORDER BY 1, 2
        """
    ).fetchall()
    memo: dict[tuple[str | None, str | None], int] = {}
    payload = []
    for unit_source, unit_normalized in rows:
        key = (unit_source, unit_normalized)
        if key not in memo:
            memo[key] = _unit_concept(vocabulary, mappings, unit_source, unit_normalized)
        payload.append((unit_source, unit_normalized, memo[key]))
    if payload:
        _bulk_insert(con, "unit_map", payload, ("unit_source", "unit_normalized", "concept_id"))


def _unit_concept(vocabulary, mappings: MappingRegistry, unit_source: str | None,
                  unit_normalized: str | None) -> int:
    """The Unit-domain concept for one (spelling, UCUM code) pair, or 0."""
    for text in (unit_source, unit_normalized):
        approved = mappings.get("UNIT", text) if text else None
        if approved is not None and vocabulary.domain_of(int(approved.concept_id)) == "Unit":
            return int(approved.concept_id)
    for text in (unit_normalized, unit_source):
        match = vocabulary.lookup_code("UCUM", text) if text else None
        if match is not None and match.domain_id == "Unit":
            return int(match.concept_id)
    return 0


def _build_discharge_map(con, vocabulary, mappings: MappingRegistry) -> None:
    """Where a visit discharged to, resolved once per distinct source value.

    An approved mapping (``mappings/discharge.csv``, code system ``DISCHARGE``) whose
    concept the loaded vocabulary has; otherwise 0 with the source value kept on the
    row. The domain is the reviewer's to get right when the decision is compiled.
    """
    con.execute("CREATE TABLE discharge_map (discharged_to VARCHAR, concept_id BIGINT)")
    if "discharged_to" not in _evt_columns(con):
        return
    rows = con.execute(
        "SELECT DISTINCT discharged_to FROM evt WHERE discharged_to IS NOT NULL ORDER BY 1"
    ).fetchall()
    payload = []
    for (value,) in rows:
        approved = mappings.get("DISCHARGE", value)
        concept = (
            int(approved.concept_id)
            if approved is not None and vocabulary.concept_exists(int(approved.concept_id))
            else 0
        )
        payload.append((value, concept))
    if payload:
        _bulk_insert(con, "discharge_map", payload, ("discharged_to", "concept_id"))


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
        _bulk_insert(con, "attr_map", payload, ("attr", "value_text", "concept_id"))


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
        "visit_detail",
    )
    con.execute("CREATE TABLE type_concept (key VARCHAR, concept_id BIGINT)")
    _bulk_insert(
        con, "type_concept",
        [(k, (mappings.get("TYPE_CONCEPT", k).concept_id if mappings.get("TYPE_CONCEPT", k) else 0)) for k in keys],
        ("key", "concept_id"),
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
    # Every OMOP *_date column is the calendar date in the dataset's own zone: a date is
    # the day of care, not the day the naive-UTC instant happens to fall on (D-R19).
    zone = cfg.time.timezone_assumption
    distinct, resolved, unmapped = _build_term_map(
        con, vocabulary, mappings, cfg.terminology.drug_name_noise,
        cfg.terminology.drug_name_truncated_at,
    )
    _build_dose_map(con)
    _build_unit_map(con, vocabulary, mappings)
    _build_discharge_map(con, vocabulary, mappings)
    _build_attribute_map(con, vocabulary, mappings)
    _type_concept_map(con, mappings)

    counts: dict[str, int] = {}
    blocked = _publish_person(con, cfg)
    counts["person"] = _count(con, "person")

    counts["visit_occurrence"] = _stage_and_load(con, "visit_occurrence", "visit_occurrence_id", _visit_sql(con, zone))
    con.execute(
        """
        CREATE TABLE visit_lookup AS
        SELECT person_id, visit_source_value, min(visit_occurrence_id) AS visit_occurrence_id
        FROM visit_occurrence WHERE visit_source_value IS NOT NULL
        GROUP BY person_id, visit_source_value
        """
    )
    # After the visits and their lookup: a detail is published under its parent visit
    # or not at all.
    counts["visit_detail"] = _publish_visit_detail(con, zone)

    counts["condition_occurrence"] = _stage_and_load(
        con, "condition_occurrence", "condition_occurrence_id", _condition_sql(con, zone)
    )
    counts["drug_exposure"] = _stage_and_load(con, "drug_exposure", "drug_exposure_id", _drug_sql(con, zone))
    counts["procedure_occurrence"] = _stage_and_load(
        con, "procedure_occurrence", "procedure_occurrence_id", _procedure_sql(con, zone)
    )
    counts["measurement"] = _stage_and_load(con, "measurement", "measurement_id", _measurement_sql(con, cfg))
    counts["observation"] = _stage_and_load(con, "observation", "observation_id", _observation_sql(con, zone))
    counts["note"] = _stage_and_load(con, "note", "note_id", _note_sql(con, zone))
    counts["death"] = _publish_death(con, cfg)
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

#: Event kinds whose rows are routed by domain. Visits, visit details, notes and deaths
#: are built from their own structure rather than from a mapped code, so they are not
#: re-routed: a visit's table is decided by it being a visit.
ROUTED_KINDS = (
    "condition", "drug_order", "drug_admin", "procedure", "measurement", "observation",
    "demographic",
)

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


#: The surrogate key's order, and why it carries a second column.
#:
#: A source code that maps to more than one standard concept produces more than one
#: published row from one canonical event, so ``ORDER BY event_id`` alone leaves those
#: rows tied and the engine free to number them either way. Two builds then agree on
#: every row's content and disagree on which of them holds which id, which is enough to
#: make an order-independent digest of the table differ. 83,016 drug events and 33
#: condition events fan out this way on MIMIC-IV; the tables with no fan-out reproduced
#: exactly, which is what located this. The concept id is the column the fan-out is
#: along, so adding it makes the order total.
SURROGATE_ORDER = "e.event_id, coalesce(m.concept_id, 0)"


def _routes_here(table: str, kinds: tuple[str, ...]) -> str:
    """The WHERE clause deciding whether an event belongs in ``table``.

    Three cases, and the middle one is the whole point:

    * unmapped (no concept, or a domain with no table): it stays where its source column
      put it, because nothing better is known -- a `concept_id = 0` row still has to live
      somewhere, and the column it came from is the honest guess;
    * mapped, and its domain names this table: it lands here, whichever column it came
      from;
    * mapped, and its domain names another table: it is not this table's row.

    The whole disjunction is wrapped, and that is not cosmetic. Unparenthesised, a
    caller writing ``WHERE {routes} AND NOT <excluded>`` gets ``A OR (B AND NOT
    excluded)`` from the parser: the exclusion silently applies to one branch and every
    row arriving through the other keeps its exemption. That is not a syntax error and
    nothing downstream reports it -- the rows are well formed, they simply should not
    be there. It cost 8,483,405 refusals and line flushes published as drug exposure.
    """
    kind_list = ", ".join(f"'{k}'" for k in kinds)
    domains = ", ".join(f"'{d}'" for d in DOMAIN_TABLE)
    mine = ", ".join(f"'{d}'" for d, tb in DOMAIN_TABLE.items() if tb == table)
    unrouted = f"(m.domain_id IS NULL OR m.domain_id NOT IN ({domains}))"
    return (
        f"((e.event_kind IN ({kind_list}) AND {unrouted})"
        f" OR (e.event_kind IN ({', '.join(repr(k) for k in ROUTED_KINDS)})"
        f" AND m.domain_id IN ({mine})))"
    )


def _visit_sql(con, zone: str | None) -> str:
    start, end = _local_time("e.event_time", zone), _local_time("coalesce(e.end_time, e.event_time)", zone)
    discharged_to = _optional(_evt_columns(con), "discharged_to", "VARCHAR")
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS visit_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS visit_concept_id,
               CAST({start} AS DATE) AS visit_start_date,
               {start} AS visit_start_datetime,
               CAST({end} AS DATE) AS visit_end_date,
               {end} AS visit_end_datetime,
               {_type_id(con, 'visit')} AS visit_type_concept_id,
               CAST(NULL AS INTEGER) AS provider_id,
               CAST(NULL AS INTEGER) AS care_site_id,
               substr(e.encounter_id, 1, 50) AS visit_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS visit_source_concept_id,
               0 AS admitted_from_concept_id,
               CAST(NULL AS VARCHAR) AS admitted_from_source_value,
               CAST(coalesce(dm.concept_id, 0) AS INTEGER) AS discharged_to_concept_id,
               substr({discharged_to}, 1, 50) AS discharged_to_source_value,
               CAST(NULL AS INTEGER) AS preceding_visit_occurrence_id,
               e.event_id
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely: clinical rows
        -- pointing at a person who was never published are not a CDM instance,
        -- they are dangling references with a patient's data attached.
        JOIN person pr ON pr.person_id = p.person_id
        LEFT JOIN term_map m ON m.code_system = e.code_system AND m.source_code = e.source_code
        LEFT JOIN discharge_map dm ON dm.discharged_to = {discharged_to}
        WHERE e.event_kind = '{EventKind.visit}'
    """


#: How a visit detail finds the visit it belongs to (remediation plan T1.13, D-R14),
#: in order: the same person's visit carrying the detail's encounter id; failing that,
#: the same person's visit whose span contains the detail's start, the one that began
#: latest -- the innermost -- and among equals the shortest, then the lower id so the
#: choice is total. A detail neither rule can place is not published: a VISIT_DETAIL row
#: with no parent is invalid CDM, and inventing a visit to hold it would be worse.
VISIT_DETAIL_PARENT_RULES = ("encounter", "containment")


def _publish_visit_detail(con, zone: str | None) -> int:
    """VISIT_DETAIL: transfers, service changes and ICU stays under their visit.

    Before this table existed every such record was published as a visit of its own,
    which is how 86% of one export's visits came to carry no concept: a ward transfer
    is not a visit type. The parent is resolved once per detail into a temp table that
    the publish and the audit rows both read, so the row published and the row reported
    as unparented are decided by the same query.
    """
    cols = _evt_columns(con)
    discharged_to = _optional(cols, "discharged_to", "VARCHAR")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE visit_detail_candidates AS
        SELECT e.event_id, e.subject_id, p.person_id, e.source_id, e.encounter_id,
               e.code_system, e.source_code,
               -- The local clock, once: VISIT_OCCURRENCE is published on it, and the
               -- containment join below compares against those published columns.
               {_local_time('e.event_time', zone)} AS event_time,
               {_local_time('coalesce(e.end_time, e.event_time)', zone)} AS end_time,
               {discharged_to} AS discharged_to
        FROM evt e
        JOIN pmap p ON p.subject_id = e.subject_id
        -- A patient withheld from PERSON is withheld entirely, details included.
        JOIN person pr ON pr.person_id = p.person_id
        WHERE e.event_kind = '{EventKind.visit_detail}' AND e.event_time IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE visit_detail_parent AS
        WITH by_encounter AS (
            SELECT c.event_id, v.visit_occurrence_id
            FROM visit_detail_candidates c
            JOIN visit_lookup v ON v.person_id = c.person_id AND v.visit_source_value = c.encounter_id
        ),
        by_containment AS (
            SELECT event_id, visit_occurrence_id FROM (
                SELECT c.event_id, v.visit_occurrence_id,
                       row_number() OVER (
                           PARTITION BY c.event_id
                           ORDER BY v.visit_start_datetime DESC, v.visit_end_datetime ASC,
                                    v.visit_occurrence_id
                       ) AS rank
                FROM visit_detail_candidates c
                JOIN visit_occurrence v
                  ON v.person_id = c.person_id
                 AND v.visit_start_datetime <= c.event_time
                 AND c.event_time <= v.visit_end_datetime
                WHERE NOT EXISTS (SELECT 1 FROM by_encounter b WHERE b.event_id = c.event_id)
            ) WHERE rank = 1
        )
        SELECT event_id, visit_occurrence_id FROM by_encounter
        UNION ALL
        SELECT event_id, visit_occurrence_id FROM by_containment
        """
    )
    con.execute(
        """
        INSERT INTO etl_audit.quality_issue
        SELECT 'VISIT_DETAIL_UNPARENTED', 'warning', 'omop', c.subject_id, NULL, c.event_id,
               NULL, c.source_id,
               'no visit of this person carries the encounter id or contains the start time; '
               'the detail was not published'
        FROM visit_detail_candidates c
        WHERE NOT EXISTS (SELECT 1 FROM visit_detail_parent v WHERE v.event_id = c.event_id)
        ORDER BY c.event_id
        """
    )
    n = _stage_and_load(con, "visit_detail", "visit_detail_id", _visit_detail_sql(con, zone))
    con.execute("DROP TABLE visit_detail_parent")
    con.execute("DROP TABLE visit_detail_candidates")
    return n


def _visit_detail_sql(con, zone: str | None) -> str:
    # c.event_time and c.end_time are already on the site clock (see _publish_visit_detail).
    start, end = "c.event_time", "c.end_time"
    # Its own type concept where the registry names one; otherwise the visit's, because
    # a detail is an encounter record of the same kind as the visit that holds it.
    type_concept = _type_id(con, "visit_detail") or _type_id(con, "visit")
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY c.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS visit_detail_id,
               c.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS visit_detail_concept_id,
               CAST({start} AS DATE) AS visit_detail_start_date,
               {start} AS visit_detail_start_datetime,
               CAST({end} AS DATE) AS visit_detail_end_date,
               {end} AS visit_detail_end_datetime,
               {type_concept} AS visit_detail_type_concept_id,
               CAST(NULL AS INTEGER) AS provider_id,
               CAST(NULL AS INTEGER) AS care_site_id,
               substr(coalesce(c.source_code, c.encounter_id), 1, 50) AS visit_detail_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS visit_detail_source_concept_id,
               0 AS admitted_from_concept_id,
               CAST(NULL AS VARCHAR) AS admitted_from_source_value,
               -- the DDL orders these two the other way round from VISIT_OCCURRENCE
               substr(c.discharged_to, 1, 50) AS discharged_to_source_value,
               CAST(coalesce(dm.concept_id, 0) AS INTEGER) AS discharged_to_concept_id,
               CAST(NULL AS INTEGER) AS preceding_visit_detail_id,
               CAST(NULL AS INTEGER) AS parent_visit_detail_id,
               pv.visit_occurrence_id,
               c.event_id
        FROM visit_detail_candidates c
        JOIN visit_detail_parent pv ON pv.event_id = c.event_id
        LEFT JOIN term_map m ON m.code_system = c.code_system AND m.source_code = c.source_code
        LEFT JOIN discharge_map dm ON dm.discharged_to = c.discharged_to
    """


def _condition_sql(con, zone: str | None) -> str:
    start = _local_time("e.event_time", zone)
    # The source says when a problem was first noted. That is not an onset date, and
    # the type concept is what keeps this row from claiming one.
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS condition_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS condition_concept_id,
               CAST({start} AS DATE) AS condition_start_date,
               {start} AS condition_start_datetime,
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


#: The width of DRUG_EXPOSURE.sig in the DDL; everything written there is cut to it.
SIG_LIMIT = 250

#: How an infusion rate reaches DRUG_EXPOSURE (remediation decision D-R15). OMOP has no
#: column for a rate, and ``sig`` -- the directions on the order -- is borrowed for it
#: on purpose, with a fixed shape so a reader can take it apart again:
#:
#:     "<dose text>; rate <rate as written, else the parsed number> <rate unit>"
#:
#: The dose text is the source's dose string (the same text ``sig`` already held when
#: the dose could not be published as a quantity), left out when there is none; the
#: unit is left out when there is none; the whole is cut at ``SIG_LIMIT``. The
#: structured rate itself is not in OMOP: it stays on the canonical event and in the
#: MEDS ``rate``/``rate_unit`` columns, which are the columns to compute with.
SIG_RATE_SEPARATOR = "; "
SIG_RATE_PREFIX = "rate "
SIG_RATE_FORMAT = f"<dose text>{SIG_RATE_SEPARATOR}{SIG_RATE_PREFIX}<rate> <rate unit>"


def _drug_sql(con, zone: str | None) -> str:
    start, end = _local_time("e.event_time", zone), _local_time("coalesce(e.end_time, e.event_time)", zone)
    """Orders and administrations share a table; the type concept keeps them apart.

    The source status of an order has no home in the OMOP core, so it stays on the
    canonical event that the lineage points at. It is not dropped, and it is not forced
    into ``stop_reason``, which means something else.

    ``dose_unit_source_value`` is the unit parsed out of the dose text, else the event's
    own unit: a source that keeps the unit in a column of its own (remediation T1.4,
    P-C6) used to publish every dose without one.
    """
    cols = _evt_columns(con)
    rate = _optional(cols, "rate", "DOUBLE")
    rate_source = _optional(cols, "rate_source", "VARCHAR")
    rate_unit = _optional(cols, "rate_unit", "VARCHAR")
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS drug_exposure_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS drug_concept_id,
               CAST({start} AS DATE) AS drug_exposure_start_date,
               {start} AS drug_exposure_start_datetime,
               CAST({end} AS DATE) AS drug_exposure_end_date,
               {end} AS drug_exposure_end_datetime,
               CAST(NULL AS DATE) AS verbatim_end_date,
               CASE WHEN e.event_kind = '{EventKind.drug_order}'
                    THEN {_type_id(con, 'drug_order')} ELSE {_type_id(con, 'drug_admin')} END
                    AS drug_type_concept_id,
               CAST(NULL AS VARCHAR) AS stop_reason,
               CAST(NULL AS INTEGER) AS refills,
               d.quantity,
               CAST(NULL AS INTEGER) AS days_supply,
               CASE
                   WHEN {rate} IS NOT NULL OR {rate_source} IS NOT NULL THEN substr(
                       concat_ws('{SIG_RATE_SEPARATOR}', e.dose_source,
                                 '{SIG_RATE_PREFIX}' || concat_ws(' ',
                                     coalesce({rate_source}, CAST({rate} AS VARCHAR)),
                                     {rate_unit})),
                       1, {SIG_LIMIT})
                   WHEN d.quantity IS NULL THEN substr(e.dose_source, 1, {SIG_LIMIT})
               END AS sig,
               0 AS route_concept_id,
               CAST(NULL AS VARCHAR) AS lot_number,
               CAST(NULL AS INTEGER) AS provider_id,
               v.visit_occurrence_id,
               CAST(NULL AS INTEGER) AS visit_detail_id,
               substr(e.source_code, 1, 50) AS drug_source_value,
               CAST(coalesce(m.source_concept_id, 0) AS INTEGER) AS drug_source_concept_id,
               substr(e.route_source, 1, 50) AS route_source_value,
               substr(coalesce(d.dose_unit, e.unit_source), 1, 50) AS dose_unit_source_value,
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
          -- A refusal, a held dose and a line flush are all real records and all stay
          -- in the canonical layer. None of them is a drug exposure, which is a claim
          -- that this patient received this drug.
          AND NOT list_contains(e.quality_flags, '{QualityFlag.NOT_ADMINISTERED}')
    """


def _procedure_sql(con, zone: str | None) -> str:
    start = _local_time("e.event_time", zone)
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS procedure_occurrence_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS procedure_concept_id,
               CAST({start} AS DATE) AS procedure_date,
               {start} AS procedure_datetime,
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
    be a different clinical claim. The reference range is the one the source reported
    with the result (canonical ``range_low``/``range_high``, schema 3), and where the
    source reported none, the range configured for the code.
    """
    start = _local_time("e.event_time", cfg.time.timezone_assumption)
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
    range_low = f"coalesce(e.range_low, {range_low})"
    range_high = f"coalesce(e.range_high, {range_high})"

    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS measurement_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS measurement_concept_id,
               CAST({start} AS DATE) AS measurement_date,
               {start} AS measurement_datetime,
               CAST(NULL AS VARCHAR) AS measurement_time,
               {_type_id(con, 'measurement')} AS measurement_type_concept_id,
               0 AS operator_concept_id,
               e.value_number AS value_as_number,
               0 AS value_as_concept_id,
               CAST(coalesce(u.concept_id, 0) AS INTEGER) AS unit_concept_id,
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
        {_unit_join(con)}
        WHERE {_routes_here('measurement', (str(EventKind.measurement),))}
    """


def _unit_join(con) -> str:
    """The join that turns a unit spelling into its concept, shared by every table
    with a ``unit_concept_id`` (see :func:`_build_unit_map`)."""
    normalized = _optional(_evt_columns(con), "unit_normalized", "VARCHAR")
    return (
        "LEFT JOIN unit_map u ON u.unit_source IS NOT DISTINCT FROM e.unit_source\n"
        f"                     AND u.unit_normalized IS NOT DISTINCT FROM {normalized}"
    )


def _observation_sql(con, zone: str | None) -> str:
    start = _local_time("e.event_time", zone)
    """Facts that are not conditions, drugs, procedures or measurements.

    Almost everything that lands here is a Z code: a family history, a screening
    encounter, a socioeconomic factor. None of them is a diagnosis, and until the
    routing existed none of them was published at all -- 5,737 terms carrying 1,944,952
    rows sat in a review queue that no reviewer could have emptied, because the question
    was never "what does this code mean" but "which table does this kind of fact go in".

    A row of another kind reaches this table only through a mapped concept: an unmapped
    condition stays in the table its source column implies, so nothing arrives here
    merely because it failed to resolve elsewhere. An event declared as an
    ``observation`` (a follow-up contact, a documented date) is the exception, and the
    same rule every other kind gets: unmapped, it stays in its own table with
    ``concept_id = 0`` and its source value intact, rather than being withheld.
    """
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id, coalesce(m.concept_id, 0)) AS INTEGER) AS observation_id,
               p.person_id,
               CAST(coalesce(m.concept_id, 0) AS INTEGER) AS observation_concept_id,
               CAST({start} AS DATE) AS observation_date,
               {start} AS observation_datetime,
               {_type_id(con, 'observation')} AS observation_type_concept_id,
               e.value_number AS value_as_number,
               substr(e.value_text, 1, 60) AS value_as_string,
               0 AS value_as_concept_id,
               0 AS qualifier_concept_id,
               CAST(coalesce(u.concept_id, 0) AS INTEGER) AS unit_concept_id,
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
        {ROUTABLE_JOIN}
        LEFT JOIN visit_lookup v ON v.person_id = p.person_id AND v.visit_source_value = e.encounter_id
        {_unit_join(con)}
        WHERE {_routes_here('observation', (str(EventKind.observation),))}
          AND e.event_time IS NOT NULL
    """


def _note_sql(con, zone: str | None) -> str:
    start = _local_time("e.event_time", zone)
    # note_text is verbatim. Any later extraction is a separate event with its own
    # lineage and never edits this text.
    return f"""
        SELECT CAST(row_number() OVER (ORDER BY e.event_id) AS INTEGER) AS note_id,
               p.person_id,
               CAST({start} AS DATE) AS note_date,
               {start} AS note_datetime,
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


def _publish_death(con, cfg: DatasetConfig) -> int:
    """DEATH, one row per person, and only where the sources agree on the day.

    Agreement is on the calendar date in the dataset's declared zone, not on the
    timestamp (remediation plan T1.8, decision D-R11). A registry records a death as a
    date and an admission record as a time of day; both are the same death, and read as
    instants they can never be equal -- a date-only death is local midnight, and a time
    of day that evening falls on the next UTC day. Comparing timestamps withheld 11,402
    MIMIC-IV deaths of which 11,401 agreed on the day. The canonical layer already
    merges the same-day pair into one event; this comparison is what makes a file that
    has not been rebuilt under that rule publish the same rows.

    Where the dates agree the most precise event is published: one with a time of day
    over one at local midnight, the earlier among equals. Where they disagree, both
    survive in canonical and neither is published here: choosing the earlier or the
    later date would be inventing the answer to a question a human has to settle.

    With no zone declared the stored times are compared as they are, which is right for
    a layer stored naive and flagged, and a documented approximation otherwise.
    """
    local_time = _local_time("e.event_time", cfg.time.timezone_assumption)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE death_candidates AS
        WITH local AS (
            SELECT e.subject_id, e.event_id, e.event_time, {local_time} AS local_time
            FROM evt e WHERE e.event_kind = '{EventKind.death}' AND e.event_time IS NOT NULL
        ),
        dates AS (
            SELECT subject_id, count(DISTINCT CAST(local_time AS DATE)) AS variants
            FROM local GROUP BY subject_id
        ),
        ranked AS (
            SELECT subject_id, event_id, event_time,
                   row_number() OVER (
                       PARTITION BY subject_id
                       ORDER BY CAST(local_time AS TIME) = TIME '00:00:00', event_time, event_id
                   ) AS precision_rank
            FROM local
        )
        SELECT d.subject_id, d.variants, r.event_time AS death_time, r.event_id
        FROM dates d
        JOIN ranked r ON r.subject_id = d.subject_id AND r.precision_rank = 1
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
               CAST({_local_time('d.death_time', cfg.time.timezone_assumption)} AS DATE) AS death_date,
               {_local_time('d.death_time', cfg.time.timezone_assumption)} AS death_datetime,
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
            f"ehr2trace {CODE_VERSION}, config {cfg.config_hash()[:12]}",
            datetime.strptime(cfg.omop.source_release_date, "%Y-%m-%d").date()
            if cfg.omop.source_release_date
            else date.today(),
            datetime.strptime(cfg.omop.cdm_release_date, "%Y-%m-%d").date()
            if cfg.omop.cdm_release_date
            else date.today(),
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


def _connect(db_path: Path):
    """The target database, with its session clock pinned to UTC.

    ``evt.event_time`` is a naive UTC instant. DuckDB resolves every implicit cast to a
    zoned type against the session's TimeZone, which defaults to the machine's, so a
    build in Denver and one in London would read the same file differently the moment
    a query touched ``timestamptz``. The one place a zone belongs is the explicit
    conversion in :func:`_local_time`.
    """
    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute("SET TimeZone = 'UTC'")
    # Checkpoint rarely. The default threshold is 16 MB, so loading a 30 GB database
    # checkpoints thousands of times, and every checkpoint is a burst of fsyncs; on a
    # drive that answers an fsync in 11 ms (a QLC SSD past its cache) MIMIC-IV's publish
    # wrote 7 MB in nine minutes. The write-ahead log may grow to this size between
    # checkpoints, on the same disk as the database.
    con.execute("SET checkpoint_threshold = '8GB'")
    from ehr2trace.analytics import operator_memory_limit_gb

    cap = operator_memory_limit_gb()
    if cap is not None:
        # Only when an operator asked for one: otherwise the publisher keeps DuckDB's default.
        con.execute(f"SET memory_limit = '{cap}GB'")
    return con


def _bulk_insert(con, table: str, rows: list[tuple], columns: tuple[str, ...]) -> None:
    """Load ``rows`` into ``table`` as one statement.

    Row-at-a-time inserts run one autocommitted INSERT per row, and DuckDB syncs its log
    on every commit. MIMIC-IV's dose map is 5,030,040 distinct dose texts: row by row
    that was 5 million fsyncs, about 84 minutes on an NVMe drive and most of a day on
    the QLC drive the work root lives on, with the whole publish sitting in the kernel's
    journal-commit wait. Handed over as one Arrow table it is one insert, one commit.
    """
    if not rows:
        return
    import pyarrow as pa

    arrays = [pa.array(list(col)) for col in zip(*rows)]
    view = f"_load_{table}"
    con.register(view, pa.Table.from_arrays(arrays, names=list(columns)))
    try:
        con.execute(f"INSERT INTO {table} SELECT * FROM {view}")
    finally:
        con.unregister(view)


def _evt_columns(con) -> frozenset[str]:
    return frozenset(_columns(con, "evt"))


def _optional(columns: frozenset[str], name: str, sql_type: str, alias: str = "e") -> str:
    """``alias.name`` when the canonical file has the column, else a typed NULL.

    A file written under canonical schema version 1 lacks the columns version 2 added
    (normalized value and unit, rate, action, discharge destination). It is still a
    valid input, and reads as one that carries nulls there.
    """
    return f"{alias}.{name}" if name in columns else f"CAST(NULL AS {sql_type})"


def _local_time(expr: str, zone: str | None) -> str:
    """SQL for ``expr``, a naive UTC timestamp, on the wall clock of ``zone``.

    The inner call stamps the naive value as UTC; the outer one moves it to the zone
    and drops the stamp, so the result is a plain timestamp whose date and time are the
    local ones. With no zone the expression is returned unchanged.
    """
    if not zone:
        return expr
    return f"timezone('{zone.replace(chr(39), chr(39) * 2)}', timezone('UTC', {expr}))"


def _env_vocabulary_dir() -> Path | None:
    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None


def _write_pending(layout: WorkLayout, unresolved: Sequence[TermRequest], vocabulary) -> Path | None:
    """Unmapped terms go to the review queue rather than silently becoming 0."""
    if not unresolved:
        return None
    from ehr2trace.review import write_pending

    # Candidates are deliberately *not* computed here. Lexical recall is a scan of the
    # whole concept table per term, and publishing should not spend hours building a
    # review queue nobody has asked for yet. `ehr2trace propose` does recall on demand,
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
