"""canonical -> OMOP CDM 5.4 (design section 6).

Ground rules, in the order they matter:

* the official DDL defines the core tables and this project does not touch it. Anything
  the converter needs to say beyond OMOP goes in the ``etl_audit`` schema;
* every published row has at least one source row behind it in ``etl_audit.lineage``;
* ``*_source_value`` keeps what the source said; ``*_concept_id`` accepts only concepts
  that exist in the local vocabulary with a compatible domain, and 0 otherwise --
  0 plus a preserved source value plus a review entry, never an invented id;
* the birth-year policy cannot be bypassed. Under ``strict`` a patient with no
  derivable year of birth is not published to PERSON at all and is counted in the
  blocker report. Canonical and MEDS are unaffected, because they can state an age
  honestly and OMOP cannot.

OMOP primary keys are 32-bit in the DDL while subject ids are 63-bit hashes, so this
layer assigns dense integer keys by a deterministic sort and records the correspondence
in ``etl_audit.lineage``. Sorting, rather than a counter, is what makes them
reproducible across runs and worker counts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import polars as pl

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.schema import EventKind, QualityFlag
from ehr2cdm.terminology import (
    DOMAIN_FOR_KIND,
    MappingRegistry,
    TermRequest,
    Vocabulary,
    collect_terms,
    normalize_term,
    resolve_terms,
)
from ehr2cdm.version import CODE_VERSION

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
class OmopContext:
    cfg: DatasetConfig
    layout: WorkLayout
    vocabulary: Any
    mappings: MappingRegistry
    person_ids: dict[int, int]
    resolved: dict[tuple[str, str], Any]


def build_omop(cfg: DatasetConfig, layout: WorkLayout, vocabulary_dir: Path | None = None) -> dict[str, Any]:
    import duckdb

    events = pl.read_parquet(layout.canonical_path("events"))
    links = pl.read_parquet(layout.canonical_path("event_source"))
    anchors = _read_optional(layout.canonical_path("anchors"))
    memberships = _read_optional(layout.canonical_path("cohort_membership"))
    issues = _read_optional(layout.canonical_path("quality_issue"))

    vocab_dir = vocabulary_dir or _env_vocabulary_dir()
    vocabulary = Vocabulary.open(vocab_dir)
    mappings = MappingRegistry.load(Path.cwd() / "mappings")

    db_path = layout.omop_dir / "omop.duckdb"
    if db_path.exists():
        # Rebuilt from scratch rather than updated in place: incremental writes are how
        # a published layer drifts away from the lineage that explains it.
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    _create_schema(con)

    subject_ids = sorted({int(s) for s in events["subject_id"].to_list()})
    person_ids = {sid: i + 1 for i, sid in enumerate(subject_ids)}

    terms = collect_terms(events.iter_rows(named=True))
    resolved, unresolved = resolve_terms(list(terms.values()), vocabulary, mappings)
    ctx = OmopContext(cfg, layout, vocabulary, mappings, person_ids, resolved)

    lineage: list[dict[str, Any]] = []
    link_index = _index_links(links)
    counts: dict[str, int] = {}

    person_rows, blocked, birth_issues = _build_person(ctx, events)
    counts["person"] = _load(con, "person", person_rows)
    _record_lineage(lineage, "person", person_rows, "person_id", link_index)
    published = {r["person_id"] for r in person_rows}

    visit_rows = _build_visits(ctx, events)
    counts["visit_occurrence"] = _load(con, "visit_occurrence", visit_rows)
    _record_lineage(lineage, "visit_occurrence", visit_rows, "visit_occurrence_id", link_index)
    visits = {
        (r["person_id"], r["visit_source_value"]): r["visit_occurrence_id"]
        for r in visit_rows
        if r.get("visit_source_value")
    }

    for table, pk, builder in (
        ("condition_occurrence", "condition_occurrence_id", _build_conditions),
        ("drug_exposure", "drug_exposure_id", _build_drugs),
        ("procedure_occurrence", "procedure_occurrence_id", _build_procedures),
        ("measurement", "measurement_id", _build_measurements),
        ("note", "note_id", _build_notes),
    ):
        rows = builder(ctx, events, visits)
        counts[table] = _load(con, table, rows)
        _record_lineage(lineage, table, rows, pk, link_index)

    death_rows, death_issues = _build_death(ctx, events)
    counts["death"] = _load(con, "death", death_rows)
    _record_lineage(lineage, "death", death_rows, "person_id", link_index)

    counts["observation_period"] = _load(
        con, "observation_period", _build_observation_periods(ctx, events, published)
    )
    counts["cdm_source"] = _load(con, "cdm_source", [_cdm_source_row(cfg, vocabulary)])

    # One controlled load path, at the end. Never concurrent inserts.
    _load_audit(con, "etl_audit.lineage", lineage)
    _load_audit(con, "etl_audit.anchor", _with_person(anchors, person_ids))
    _load_audit(con, "etl_audit.cohort_membership", _with_person(memberships, person_ids))
    _load_audit(con, "etl_audit.quality_issue", _rows(issues) + birth_issues + death_issues)
    _load_audit(con, "etl_audit.run", [_run_row(cfg, mappings, vocabulary, layout)])
    con.close()

    pending = _write_pending(layout, unresolved, vocabulary)
    result = {
        "database": str(db_path),
        "tables": counts,
        "distinct_terms": len(terms),
        "resolved_terms": len(resolved),
        "unmapped_terms": len(unresolved),
        "pending_csv": str(pending) if pending else None,
        "vocabulary": vocabulary.version,
        "blocked_subjects": blocked,
        "block_reason": (
            "person_birth_policy=strict and no age_as_of_date supplied; year_of_birth "
            "cannot be derived from an age alone"
            if blocked
            else ""
        ),
        "lineage_rows": len(lineage),
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


def _env_vocabulary_dir() -> Path | None:
    raw = os.environ.get("OMOP_VOCAB_DIR")
    return Path(raw) if raw else None


# --------------------------------------------------------------------------------
# table builders
# --------------------------------------------------------------------------------


def _build_person(ctx: OmopContext, events: pl.DataFrame) -> tuple[list[dict], int, list[dict]]:
    """PERSON, subject to the birth-year policy.

    OMOP requires a year of birth. This export carries an age and no date it was taken
    on, so under the default policy the patient is not published here at all. A number
    the database would accept is not the same thing as a fact about a person.
    """
    policy = ctx.cfg.omop.person_birth_policy
    demo = events.filter(pl.col("event_kind") == str(EventKind.demographic))
    by_subject: dict[int, dict[str, Any]] = {}
    for row in demo.sort("event_id").iter_rows(named=True):
        entry = by_subject.setdefault(int(row["subject_id"]), {"event_ids": []})
        entry["event_ids"].append(row["event_id"])
        code = (row.get("source_code") or "").upper()
        if code in {"GENDER", "RACE", "ETHNICITY"}:
            entry[code.lower()] = row.get("value_text")
        elif code == "AGE":
            entry["age"] = row.get("value_number")

    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    blocked = 0
    for sid in sorted(by_subject):
        entry = by_subject[sid]
        year_of_birth, approximate = _year_of_birth(policy, entry.get("age"))
        if year_of_birth is None:
            blocked += 1
            issues.append(
                _issue(
                    "OMOP_PERSON_BLOCKED",
                    "error",
                    sid,
                    "no derivable year_of_birth under person_birth_policy="
                    f"{policy.mode}; the source has an age and no reference date",
                )
            )
            continue
        if approximate:
            issues.append(
                _issue(
                    str(QualityFlag.DERIVED_APPROXIMATE_BIRTH_YEAR),
                    "warning",
                    sid,
                    f"year_of_birth estimated from age as of {policy.age_as_of_date}; may "
                    "be off by one year because it is unknown whether the birthday had passed",
                )
            )
        gender, race, ethnicity = entry.get("gender"), entry.get("race"), entry.get("ethnicity")
        rows.append(
            {
                "person_id": ctx.person_ids[sid],
                "gender_concept_id": _attribute_concept(ctx, "Gender", "GENDER", gender),
                "year_of_birth": year_of_birth,
                "month_of_birth": None,
                "day_of_birth": None,
                "birth_datetime": None,
                "race_concept_id": _attribute_concept(ctx, "Race", "RACE", race),
                "ethnicity_concept_id": _attribute_concept(ctx, "Ethnicity", "ETHNICITY", ethnicity),
                "location_id": None,
                "provider_id": None,
                "care_site_id": None,
                # Deliberately empty: no direct patient identifier is published.
                "person_source_value": None,
                "gender_source_value": _v(gender, 50),
                "gender_source_concept_id": 0,
                "race_source_value": _v(race, 50),
                "race_source_concept_id": 0,
                "ethnicity_source_value": _v(ethnicity, 50),
                "ethnicity_source_concept_id": 0,
                "_event_ids": entry["event_ids"],
            }
        )
    return rows, blocked, issues


def _year_of_birth(policy, age: float | None) -> tuple[int | None, bool]:
    if age is None or policy.mode != "approved_approximation":
        return None, False
    reference = datetime.strptime(policy.age_as_of_date, "%Y-%m-%d").date()
    return reference.year - int(age), True


def _build_visits(ctx: OmopContext, events: pl.DataFrame) -> list[dict]:
    rows: list[dict[str, Any]] = []
    frame = events.filter(pl.col("event_kind") == str(EventKind.visit)).sort("event_id")
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        end = row.get("end_time") or start
        rows.append(
            {
                "visit_occurrence_id": i,
                "person_id": ctx.person_ids[int(row["subject_id"])],
                "visit_concept_id": _concept_id(ctx, row),
                "visit_start_date": start.date(),
                "visit_start_datetime": start,
                "visit_end_date": end.date(),
                "visit_end_datetime": end,
                "visit_type_concept_id": _type_concept(ctx, "visit"),
                "provider_id": None,
                "care_site_id": None,
                "visit_source_value": _v(row.get("encounter_id"), 50),
                "visit_source_concept_id": _source_concept_id(ctx, row),
                "admitted_from_concept_id": 0,
                "admitted_from_source_value": None,
                "discharged_to_concept_id": 0,
                "discharged_to_source_value": None,
                "preceding_visit_occurrence_id": None,
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _build_conditions(ctx: OmopContext, events: pl.DataFrame, visits: dict) -> list[dict]:
    rows: list[dict[str, Any]] = []
    frame = events.filter(pl.col("event_kind") == str(EventKind.condition)).sort("event_id")
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        person_id = ctx.person_ids[int(row["subject_id"])]
        rows.append(
            {
                "condition_occurrence_id": i,
                "person_id": person_id,
                "condition_concept_id": _concept_id(ctx, row),
                "condition_start_date": start.date(),
                "condition_start_datetime": start,
                "condition_end_date": None,
                "condition_end_datetime": None,
                # The source says when a problem was first noted. That is not an onset
                # date, and the type concept is what keeps this row from claiming one.
                "condition_type_concept_id": _type_concept(ctx, "condition_problem_list"),
                "condition_status_concept_id": 0,
                "stop_reason": None,
                "provider_id": None,
                "visit_occurrence_id": visits.get((person_id, row.get("encounter_id"))),
                "visit_detail_id": None,
                "condition_source_value": _v(row.get("source_code"), 50),
                "condition_source_concept_id": _source_concept_id(ctx, row),
                "condition_status_source_value": _v(row.get("status_source"), 50),
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _build_drugs(ctx: OmopContext, events: pl.DataFrame, visits: dict) -> list[dict]:
    """DRUG_EXPOSURE for orders and administrations, kept apart by type concept.

    The source status (ordered, dispensed, discontinued) has no home in the OMOP core
    for an order, so it stays on the canonical event, which the lineage points at. It
    is not dropped, and it is not forced into ``stop_reason``, which means something
    else.
    """
    from ehr2cdm.canonical.values import parse_value

    rows: list[dict[str, Any]] = []
    frame = events.filter(
        pl.col("event_kind").is_in([str(EventKind.drug_order), str(EventKind.drug_admin)])
    ).sort("event_id")
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        person_id = ctx.person_ids[int(row["subject_id"])]
        dose = row.get("dose_source")
        quantity = dose_unit = None
        if dose:
            parsed = parse_value(dose)
            quantity, dose_unit = parsed.number, parsed.unit
        is_order = row["event_kind"] == str(EventKind.drug_order)
        rows.append(
            {
                "drug_exposure_id": i,
                "person_id": person_id,
                "drug_concept_id": _concept_id(ctx, row),
                "drug_exposure_start_date": start.date(),
                "drug_exposure_start_datetime": start,
                "drug_exposure_end_date": (row.get("end_time") or start).date(),
                "drug_exposure_end_datetime": row.get("end_time") or start,
                "verbatim_end_date": None,
                "drug_type_concept_id": _type_concept(ctx, "drug_order" if is_order else "drug_admin"),
                "stop_reason": None,
                "refills": None,
                "quantity": quantity,
                "days_supply": None,
                "sig": _v(dose, 250) if quantity is None else None,
                "route_concept_id": 0,
                "lot_number": None,
                "provider_id": None,
                "visit_occurrence_id": visits.get((person_id, row.get("encounter_id"))),
                "visit_detail_id": None,
                "drug_source_value": _v(row.get("source_code"), 50),
                "drug_source_concept_id": _source_concept_id(ctx, row),
                "route_source_value": _v(row.get("route_source"), 50),
                "dose_unit_source_value": _v(dose_unit, 50),
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _build_procedures(ctx: OmopContext, events: pl.DataFrame, visits: dict) -> list[dict]:
    rows: list[dict[str, Any]] = []
    frame = events.filter(pl.col("event_kind") == str(EventKind.procedure)).sort("event_id")
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        person_id = ctx.person_ids[int(row["subject_id"])]
        rows.append(
            {
                "procedure_occurrence_id": i,
                "person_id": person_id,
                "procedure_concept_id": _concept_id(ctx, row),
                "procedure_date": start.date(),
                "procedure_datetime": start,
                "procedure_end_date": None,
                "procedure_end_datetime": None,
                "procedure_type_concept_id": _type_concept(ctx, "procedure"),
                "modifier_concept_id": 0,
                "quantity": None,
                "provider_id": None,
                "visit_occurrence_id": visits.get((person_id, row.get("encounter_id"))),
                "visit_detail_id": None,
                "procedure_source_value": _v(row.get("source_code"), 50),
                "procedure_source_concept_id": _source_concept_id(ctx, row),
                "modifier_source_value": None,
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _build_measurements(ctx: OmopContext, events: pl.DataFrame, visits: dict) -> list[dict]:
    """MEASUREMENT, with the value rules from design section 5.4.

    A ranged result keeps its original text in ``value_source_value`` with
    ``value_as_number`` empty. It does **not** go to ``range_low``/``range_high``:
    those are the reference range for the test, and a patient's own value there would
    be a different clinical claim. Reference ranges come from configuration, because
    the source's own range columns are empty throughout.
    """
    rows: list[dict[str, Any]] = []
    frame = events.filter(pl.col("event_kind") == str(EventKind.measurement)).sort("event_id")
    ranges = ctx.cfg.reference_ranges
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        person_id = ctx.person_ids[int(row["subject_id"])]
        reference = ranges.get(row.get("source_code") or "")
        rows.append(
            {
                "measurement_id": i,
                "person_id": person_id,
                "measurement_concept_id": _concept_id(ctx, row),
                "measurement_date": start.date(),
                "measurement_datetime": start,
                "measurement_time": None,
                "measurement_type_concept_id": _type_concept(ctx, "measurement"),
                "operator_concept_id": 0,
                "value_as_number": row.get("value_number"),
                "value_as_concept_id": 0,
                "unit_concept_id": 0,
                "range_low": reference.low if reference else None,
                "range_high": reference.high if reference else None,
                "provider_id": None,
                "visit_occurrence_id": visits.get((person_id, row.get("encounter_id"))),
                "visit_detail_id": None,
                "measurement_source_value": _v(row.get("source_code"), 50),
                "measurement_source_concept_id": _source_concept_id(ctx, row),
                "unit_source_value": _v(row.get("unit_source"), 50),
                "unit_source_concept_id": 0,
                "value_source_value": _v(_value_source_value(row), 50),
                "measurement_event_id": None,
                "meas_event_field_concept_id": None,
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _value_source_value(row: dict[str, Any]) -> str | None:
    if row.get("value_text"):
        return str(row["value_text"])
    if row.get("value_low") is not None and row.get("value_high") is not None:
        return f"{row['value_low']}-{row['value_high']}"
    if row.get("value_number") is not None:
        return str(row["value_number"])
    return None


def _build_notes(ctx: OmopContext, events: pl.DataFrame, visits: dict) -> list[dict]:
    rows: list[dict[str, Any]] = []
    frame = events.filter(pl.col("event_kind") == str(EventKind.note)).sort("event_id")
    for i, row in enumerate(frame.iter_rows(named=True), start=1):
        start = row["event_time"]
        person_id = ctx.person_ids[int(row["subject_id"])]
        rows.append(
            {
                "note_id": i,
                "person_id": person_id,
                "note_date": start.date(),
                "note_datetime": start,
                "note_type_concept_id": _type_concept(ctx, "note"),
                "note_class_concept_id": _type_concept(ctx, "note_class"),
                "note_title": _v(row.get("source_name"), 250),
                # Verbatim. Any later extraction is a separate event with its own
                # lineage and never edits this text.
                "note_text": row.get("value_text") or "",
                "encoding_concept_id": 0,
                "language_concept_id": 0,
                "provider_id": None,
                "visit_occurrence_id": visits.get((person_id, row.get("encounter_id"))),
                "visit_detail_id": None,
                "note_source_value": _v(row.get("source_code"), 50),
                "note_event_id": None,
                "note_event_field_concept_id": None,
                "_event_ids": [row["event_id"]],
            }
        )
    return rows


def _build_death(ctx: OmopContext, events: pl.DataFrame) -> tuple[list[dict], list[dict]]:
    """DEATH, one row per person, and only where the sources agree.

    Where they agree the event already collapsed to one by content. Where they
    disagree, both survive in canonical and neither is published here: choosing the
    earlier or the later date would be inventing the answer to a question a human has
    to settle.
    """
    frame = events.filter(pl.col("event_kind") == str(EventKind.death)).sort("event_id")
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for row in frame.iter_rows(named=True):
        by_subject.setdefault(int(row["subject_id"]), []).append(row)

    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for sid in sorted(by_subject):
        candidates = by_subject[sid]
        distinct = sorted({r["event_time"] for r in candidates})
        if len(distinct) > 1:
            issues.append(
                _issue(
                    "DEATH_DATE_CONFLICT",
                    "error",
                    sid,
                    "sources disagree: " + "; ".join(d.isoformat() for d in distinct),
                    event_id=candidates[0]["event_id"],
                )
            )
            continue
        if sid not in ctx.person_ids:
            continue
        when = candidates[0]["event_time"]
        rows.append(
            {
                "person_id": ctx.person_ids[sid],
                "death_date": when.date(),
                "death_datetime": when,
                "death_type_concept_id": _type_concept(ctx, "death"),
                "cause_concept_id": 0,
                "cause_source_value": None,
                "cause_source_concept_id": 0,
                "_event_ids": [candidates[0]["event_id"]],
            }
        )
    return rows, issues


def _build_observation_periods(ctx: OmopContext, events: pl.DataFrame, published: set[int]) -> list[dict]:
    """OBSERVATION_PERIOD from an explicit, versioned heuristic.

    There is no enrolment data here, so the period is the span of trustworthy dated
    events. Records dated after death are excluded from the span but not deleted, and
    the rule carries a version so that changing it is visible rather than silent.
    """
    trustworthy = events.filter(
        pl.col("event_time").is_not_null()
        & ~pl.col("quality_flags").list.contains(str(QualityFlag.RECORDED_AFTER_DEATH))
    )
    if trustworthy.height == 0:
        return []
    spans = trustworthy.group_by("subject_id").agg(
        pl.col("event_time").min().alias("start"), pl.col("event_time").max().alias("end")
    )
    rows: list[dict[str, Any]] = []
    for row in spans.sort("subject_id").iter_rows(named=True):
        person_id = ctx.person_ids.get(int(row["subject_id"]))
        if person_id is None or person_id not in published:
            continue
        rows.append(
            {
                "observation_period_id": len(rows) + 1,
                "person_id": person_id,
                "observation_period_start_date": row["start"].date(),
                "observation_period_end_date": row["end"].date(),
                "period_type_concept_id": _type_concept(ctx, "observation_period"),
            }
        )
    return rows


def _cdm_source_row(cfg: DatasetConfig, vocabulary) -> dict[str, Any]:
    return {
        "cdm_source_name": cfg.omop.source_name or cfg.dataset_id,
        "cdm_source_abbreviation": cfg.dataset_id,
        "cdm_holder": cfg.omop.cdm_holder or "unspecified (research extract; holder not declared)",
        "source_description": cfg.description,
        "source_documentation_reference": "PATIENT_CDM_AGENT_SYSTEM_DESIGN.md",
        "cdm_etl_reference": f"ehr2cdm {CODE_VERSION}, config {cfg.config_hash()[:12]}",
        "source_release_date": (
            datetime.strptime(cfg.omop.source_release_date, "%Y-%m-%d").date()
            if cfg.omop.source_release_date
            else date.today()
        ),
        "cdm_release_date": date.today(),
        "cdm_version": cfg.omop.cdm_version,
        "cdm_version_concept_id": 0,
        "vocabulary_version": vocabulary.version or "none",
    }


def _run_row(cfg: DatasetConfig, mappings: MappingRegistry, vocabulary, layout: WorkLayout) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return {
        "run_id": f"omop-{now.strftime('%Y%m%dT%H%M%SZ')}",
        "config_hash": cfg.config_hash(),
        "code_version": CODE_VERSION,
        "mapping_version": mappings.version,
        "vocabulary_version": vocabulary.version,
        "started_at": now,
        "finished_at": now,
        "input_files": str(layout.manifest_dir / "inputs.json"),
        "notes": ""
        if vocabulary.available
        else "no vocabulary available: every concept_id is 0 and every term is in review",
    }


# --------------------------------------------------------------------------------
# concept resolution
# --------------------------------------------------------------------------------


def _concept_id(ctx: OmopContext, row: dict[str, Any]) -> int:
    """Standard concept for an event, or 0. Never an invented id."""
    code = row.get("source_code")
    if not code:
        return 0
    match = ctx.resolved.get((row.get("code_system") or "SOURCE", normalize_term(str(code))))
    return int(match.concept_id) if match else 0


def _source_concept_id(ctx: OmopContext, row: dict[str, Any]) -> int:
    code = row.get("source_code")
    if not code:
        return 0
    match = ctx.resolved.get((row.get("code_system") or "SOURCE", normalize_term(str(code))))
    return int(match.source_concept_id) if match and match.source_concept_id else 0


def _attribute_concept(ctx: OmopContext, domain: str, key: str, value: str | None) -> int:
    if not value:
        return 0
    approved = ctx.mappings.get(key, value)
    if approved:
        return approved.concept_id
    match = ctx.vocabulary.lookup_name(domain, value)
    return int(match.concept_id) if match else 0


def _type_concept(ctx: OmopContext, key: str) -> int:
    """A fixed type concept, resolved from the git-tracked mapping registry.

    Not a literal in code: an id typed into a source file has no provenance and nobody
    revalidates it when the vocabulary is updated.
    """
    approved = ctx.mappings.get("TYPE_CONCEPT", key)
    return approved.concept_id if approved else 0


# --------------------------------------------------------------------------------
# loading and lineage
# --------------------------------------------------------------------------------


def _table_columns(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]


def _load(con, table: str, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = _table_columns(con, table)
    frame = pl.DataFrame({c: [r.get(c) for r in rows] for c in columns})
    con.register("_staging", frame.to_arrow())
    con.execute(f"INSERT INTO {table} SELECT {', '.join(columns)} FROM _staging")
    con.unregister("_staging")
    return len(rows)


def _load_audit(con, table: str, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = _table_columns(con, table)
    frame = pl.DataFrame({c: [r.get(c) for r in rows] for c in columns})
    con.register("_staging_audit", frame.to_arrow())
    con.execute(f"INSERT INTO {table} SELECT {', '.join(columns)} FROM _staging_audit")
    con.unregister("_staging_audit")
    return len(rows)


def _index_links(links: pl.DataFrame) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for event_id, source_row_id in zip(links["event_id"].to_list(), links["source_row_id"].to_list()):
        index.setdefault(event_id, []).append(source_row_id)
    return index


def _record_lineage(
    lineage: list[dict[str, Any]],
    table: str,
    rows: Sequence[dict[str, Any]],
    pk_field: str,
    link_index: dict[str, list[str]],
) -> None:
    for row in rows:
        for event_id in row.get("_event_ids") or []:
            for source_row_id in link_index.get(event_id, []):
                lineage.append(
                    {
                        "target_table": table,
                        "target_pk": row[pk_field],
                        "event_id": event_id,
                        "source_row_id": source_row_id,
                        "mapping_version": "0",
                    }
                )


def _with_person(frame: pl.DataFrame | None, person_ids: dict[int, int]) -> list[dict]:
    if frame is None or frame.height == 0:
        return []
    out = []
    for row in frame.iter_rows(named=True):
        record = dict(row)
        record["person_id"] = person_ids.get(int(row["subject_id"]))
        out.append(record)
    return out


def _rows(frame: pl.DataFrame | None) -> list[dict]:
    return [] if frame is None or frame.height == 0 else frame.to_dicts()


def _issue(issue_type: str, severity: str, subject_id: int, detail: str, event_id: str | None = None) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "stage": "omop",
        "subject_id": subject_id,
        "source_row_id": None,
        "event_id": event_id,
        "partition_id": None,
        "source_id": None,
        "detail": detail,
    }


def _write_pending(layout: WorkLayout, unresolved: Sequence[TermRequest], vocabulary) -> Path | None:
    """Unmapped terms go to the review queue rather than silently becoming 0."""
    if not unresolved:
        return None
    from ehr2cdm.review import write_pending

    items = []
    for term in sorted(unresolved, key=lambda t: (-t.occurrences, t.source_code)):
        candidates = vocabulary.candidates(
            term.source_name or term.source_code, DOMAIN_FOR_KIND.get(term.event_kind), limit=8
        )
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
    return write_pending(layout, items)


def _read_optional(path: Path) -> pl.DataFrame | None:
    return pl.read_parquet(path) if path.exists() else None


def _v(value: object, limit: int) -> str | None:
    """Truncate to the DDL's declared width so a Postgres export behaves identically."""
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text
