"""Fault injection: does the check suite actually detect anything?

Thirty checks passing on the pipeline that produced the data proves very little. What a
reader needs to know is the opposite direction: when a specific corruption is present,
does anything fire, and is it the check you would expect?

Every fault here is drawn from an incident that actually happened while building this
converter, not from imagination. That matters for the same reason a real vocabulary
exposed three loader bugs that no fixture had: invented faults are the ones you already
knew how to prevent. The catalogue is deliberately weighted toward the failures that are
*silent* -- the ones that leave row counts plausible, schemas valid, and spot checks
clean, and are therefore the ones a conversion ships with.

The faults themselves know nothing about any particular dataset: what they need -- a
partition id, a concept from the wrong domain -- is read out of the built artifacts at
injection time. `docs/FAULT_CATALOGUE.md` records which incident each one is drawn from.

Each fault mutates a built artifact rather than the code, so the experiment measures the
checks and not some particular bug's blast radius. Mutations always write a new file and
rename it into place, never truncating an existing inode, so a work tree cloned with
hard links stays safe to mutate.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import polars as pl

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout

#: Whether a corruption survives the checks a conversion normally gets: a schema
#: validation, a row count, an eyeball on a few patients.
SILENT = "silent"
LOUD = "loud"


@dataclass(frozen=True)
class Fault:
    id: str
    layer: str
    origin: str
    silent: str
    description: str
    apply: Callable[[WorkLayout, DatasetConfig], str]
    #: what a reader would expect to fire. Recorded to compare against what *does*
    #: fire -- never used to decide the outcome.
    expect: tuple[str, ...] = ()


FAULTS: list[Fault] = []


def fault(fault_id: str, layer: str, origin: str, silent: str, description: str, expect: tuple[str, ...] = ()):
    def deco(fn: Callable[[WorkLayout, DatasetConfig], str]) -> Callable:
        FAULTS.append(Fault(fault_id, layer, origin, silent, description, fn, expect))
        return fn

    return deco


# --------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------


def _replace(path: Path, frame: pl.DataFrame) -> None:
    """Write beside the target and rename over it.

    Never opens the existing file for writing: a work tree cloned with hard links
    shares inodes with the original, and truncating one would corrupt both.
    """
    tmp = path.with_suffix(path.suffix + ".mutating")
    frame.write_parquet(tmp)
    tmp.replace(path)


def _canonical(layout: WorkLayout, name: str) -> tuple[Path, pl.DataFrame]:
    path = layout.canonical_path(name)
    return path, pl.read_parquet(path)


def _omop_con(layout: WorkLayout):
    import duckdb

    return duckdb.connect(str(layout.omop_dir / "omop.duckdb"))


def _meds_shards(layout: WorkLayout) -> list[Path]:
    return sorted((layout.meds_dir / "data").rglob("*.parquet"))


# --------------------------------------------------------------------------------
# canonical-layer faults
# --------------------------------------------------------------------------------


@fault(
    "ANCHOR_USED_AS_EVENT_TIME",
    "canonical",
    "An export that repeats each ancillary result once per index study carries the "
    "study's date on every repeated row. Reading that column as the result's own time "
    "is the easiest mistake such a layout invites.",
    SILENT,
    "Overwrite each event's clinical time with the extraction anchor date.",
    expect=("ANCHOR_NEVER_AN_EVENT_TIME",),
)
def _anchor_as_event_time(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    apath, anchors = _canonical(layout, "anchors")
    if anchors.is_empty():
        return "skipped: this dataset has no anchors"
    first = anchors.group_by("subject_id").agg(pl.col("anchor_date").min().alias("_anchor"))
    merged = events.join(first, on="subject_id", how="left")
    changed = merged.filter(pl.col("_anchor").is_not_null()).height
    merged = merged.with_columns(
        pl.when(pl.col("_anchor").is_not_null())
        .then(pl.col("_anchor").cast(pl.Datetime("us")))
        .otherwise(pl.col("event_time"))
        .alias("event_time")
    ).drop("_anchor")
    _replace(epath, merged.select(events.columns))
    return f"{changed:,} events now carry an anchor date as their clinical time"


@fault(
    "COHORT_LABEL_BECAME_A_DIAGNOSIS",
    "canonical",
    "Cohort partitions are named after the condition that defines them. Turning that "
    "name into a condition row is how a cohort definition becomes a clinical fact, and "
    "then a model's own target handed back to it as a feature.",
    SILENT,
    "Emit the cohort label as a condition event on every labelled subject.",
    expect=("COHORT_LABEL_NEVER_A_CLINICAL_FACT",),
)
def _label_as_condition(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    _, memberships = _canonical(layout, "cohort_membership")
    if memberships.is_empty():
        return "skipped: this dataset has no cohort labels"
    labels = memberships.select("subject_id", "membership_label").unique().drop_nulls()
    template = events.head(1)
    rows = []
    for i, rec in enumerate(labels.iter_rows(named=True)):
        row = template.to_dicts()[0].copy()
        row.update(
            {
                "event_id": f"injected-label-{i}",
                "subject_id": rec["subject_id"],
                "event_kind": "condition",
                "code_system": "SOURCE",
                "source_code": rec["membership_label"],
                "source_name": f"cohort label {rec['membership_label']}",
            }
        )
        rows.append(row)
    injected = pl.DataFrame(rows, schema=events.schema)
    _replace(epath, pl.concat([events, injected]))
    return f"{len(rows):,} condition events carrying the cohort label"


@fault(
    "PARTITION_COLUMN_LEAKED_INTO_CANONICAL",
    "canonical",
    "Writing canonical tables with hive partitioning silently appended a `bucket` "
    "column to every one of them. Nothing failed; the schema simply grew a field that "
    "encodes how the data was sharded.",
    SILENT,
    "Append the physical bucketing key as a column of the canonical event table.",
    expect=(),
)
def _partition_column_leak(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    _replace(epath, events.with_columns((pl.col("subject_id") % 64).alias("bucket")))
    return "canonical events gained a `bucket` column"


@fault(
    "POST_DEATH_RECORDS_DELETED",
    "canonical",
    "Real exports contain records dated after the patient's recorded death. Deleting "
    "them looks like data cleaning and is the destruction of evidence: either the death "
    "date or the record is wrong, and which one it is matters.",
    SILENT,
    "Drop every event that falls after its subject's recorded death.",
    expect=("POST_DEATH_RECORDS_FLAGGED_NOT_DELETED", "EVENT_LINEAGE_COMPLETE"),
)
def _delete_post_death(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    deaths = (
        events.filter(pl.col("event_kind") == "death")
        .group_by("subject_id")
        .agg(pl.col("event_time").min().alias("_death"))
    )
    if deaths.is_empty():
        return "skipped: no death events in this dataset"
    merged = events.join(deaths, on="subject_id", how="left")
    doomed = merged.filter(pl.col("_death").is_not_null() & (pl.col("event_time") > pl.col("_death")))
    kept = merged.filter(~(pl.col("_death").is_not_null() & (pl.col("event_time") > pl.col("_death"))))
    _replace(epath, kept.drop("_death").select(events.columns))
    return f"{doomed.height:,} post-death events deleted"


@fault(
    "QUARANTINE_REASON_ERASED",
    "canonical",
    "A quarantine row without a reason is indistinguishable from data loss. A globbing "
    "bug once doubled the quarantine unnoticed, because the headline event counts were "
    "deduplicated and still looked correct.",
    SILENT,
    "Blank out the reason on the quarantined rows.",
    expect=("QUARANTINE_IS_EXPLAINED",),
)
def _erase_quarantine_reason(layout: WorkLayout, cfg: DatasetConfig) -> str:
    qpath, quarantine = _canonical(layout, "quarantine")
    if quarantine.is_empty():
        return "skipped: nothing was quarantined"
    _replace(qpath, quarantine.with_columns(pl.lit(None, dtype=pl.String).alias("reason")))
    return f"{quarantine.height:,} quarantine rows lost their reason"


@fault(
    "LINEAGE_LINKS_DANGLE",
    "canonical",
    "Rebuilding one layer without the other leaves links pointing at events that no "
    "longer exist. The published tables still look complete.",
    SILENT,
    "Point a slice of the lineage links at event ids that do not exist.",
    expect=("LINK_TARGETS_EXIST", "EVENT_LINEAGE_COMPLETE"),
)
def _dangle_links(layout: WorkLayout, cfg: DatasetConfig) -> str:
    lpath, links = _canonical(layout, "event_source")
    n = max(1, links.height // 100)
    mutated = links.with_columns(
        pl.when(pl.int_range(pl.len()) < n)
        .then(pl.lit("nonexistent-event-") + pl.int_range(pl.len()).cast(pl.String))
        .otherwise(pl.col("event_id"))
        .alias("event_id")
    )
    _replace(lpath, mutated)
    return f"{n:,} lineage links now point at nothing"


@fault(
    "IDENTITY_NOT_RESOLVED_ACROSS_PARTITIONS",
    "canonical",
    "Patients recur across partitions of a multi-cohort export. Hashing the partition "
    "into the subject key splits each of them into several people, which inflates the "
    "cohort and truncates every timeline.",
    SILENT,
    "Give the same patient a different subject id in each partition.",
    expect=("IDENTITY_RESOLVED_ACROSS_PARTITIONS", "MEDS_SHARDS_CONTIGUOUS_AND_SORTED"),
)
def _split_identity(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    lpath, links = _canonical(layout, "event_source")
    part = links.select("event_id", "partition_id").unique(subset=["event_id"])
    merged = events.join(part, on="event_id", how="left")
    shifted = merged.with_columns(
        (pl.col("subject_id") + pl.col("partition_id").hash() % 1000).alias("subject_id")
    ).drop("partition_id")
    _replace(epath, shifted.select(events.columns))
    return "subject ids now vary by partition for the same person"


# --------------------------------------------------------------------------------
# OMOP-layer faults
# --------------------------------------------------------------------------------


@fault(
    "WITHHELD_PATIENT_STILL_PUBLISHED",
    "omop",
    "A birth-year policy that withholds a patient from PERSON, while the clinical "
    "builders go on publishing that patient's rows, produces millions of dangling "
    "references in a database that reports a successful build.",
    SILENT,
    "Delete two people from PERSON while leaving their clinical rows in place.",
    expect=("OMOP_REFERENTIAL_INTEGRITY",),
)
def _withhold_person(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    victims = [r[0] for r in con.execute("SELECT person_id FROM person LIMIT 2").fetchall()]
    if not victims:
        con.close()
        return "skipped: no persons published"
    con.execute(f"DELETE FROM person WHERE person_id IN ({','.join(str(v) for v in victims)})")
    con.close()
    return f"persons {victims} removed, their clinical rows left behind"


@fault(
    "MEASUREMENT_DATE_FABRICATED",
    "omop",
    "Some real measurements carry no time anywhere in the export. Giving them the run "
    "date, or the patient's first event date, makes them look like measurements that "
    "happened when they did not.",
    SILENT,
    "Publish untimed values with an invented measurement date.",
    expect=("OMOP_NO_FABRICATED_MEASUREMENT_DATES", "OMOP_EVERY_ROW_HAS_LINEAGE"),
)
def _fabricate_measurement_date(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    n = con.execute("SELECT count(*) FROM measurement").fetchone()[0]
    if not n:
        con.close()
        return "skipped: no measurements published"
    con.execute(
        """
        INSERT INTO measurement (measurement_id, person_id, measurement_concept_id,
                                 measurement_date, measurement_datetime, measurement_type_concept_id,
                                 value_as_number, measurement_source_value)
        SELECT 900000000 + row_number() OVER (), person_id, 0,
               DATE '2020-01-01', TIMESTAMP '2020-01-01 00:00:00', 32817,
               1.0, 'INJECTED_UNTIMED'
        FROM person LIMIT 50
        """
    )
    con.close()
    return "50 untimed values published with an invented date"


@fault(
    "PRIMARY_KEY_COLLISION",
    "omop",
    "Deriving a surrogate key from a hash prefix that is too short collides silently; "
    "the row count is right and one fact overwrites another downstream.",
    SILENT,
    "Duplicate a primary key in a clinical table.",
    expect=("OMOP_PRIMARY_KEYS_UNIQUE",),
)
def _duplicate_pk(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    for table in ("condition_occurrence", "measurement", "drug_exposure"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n:
            con.execute(f"INSERT INTO {table} SELECT * FROM {table} LIMIT 1")
            con.close()
            return f"one {table} row duplicated, primary key reused"
    con.close()
    return "skipped: no clinical rows published"


@fault(
    "CONCEPT_PLACED_IN_THE_WRONG_DOMAIN",
    "omop",
    "A diagnosis code that maps to an Observation concept does not belong in "
    "CONDITION_OCCURRENCE. Thousands of real diagnosis codes map outside the Condition "
    "domain, so the gate is not hypothetical.",
    SILENT,
    "Give a condition row a concept id from another domain.",
    expect=("OMOP_CONCEPTS_EXIST_AND_FIT_THEIR_DOMAIN",),
)
def _wrong_domain(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    n = con.execute("SELECT count(*) FROM condition_occurrence").fetchone()[0]
    if not n:
        con.close()
        return "skipped: no conditions published"
    # Borrow a concept the build already published in a different domain, rather than
    # naming one: the fault has to travel to datasets whose vocabulary differs.
    row = con.execute("SELECT DISTINCT gender_concept_id FROM person WHERE gender_concept_id <> 0 LIMIT 1").fetchone()
    if row is None:
        con.close()
        return "skipped: no non-zero concept available to borrow"
    borrowed = row[0]
    con.execute(
        "UPDATE condition_occurrence SET condition_concept_id = ? "
        "WHERE condition_occurrence_id IN "
        "(SELECT condition_occurrence_id FROM condition_occurrence LIMIT 5)",
        [borrowed],
    )
    con.close()
    return f"5 condition rows now carry concept {borrowed}, published elsewhere as a gender"


@fault(
    "BIRTH_YEAR_INVENTED_UNDER_STRICT_POLICY",
    "omop",
    "When an export carries an age but no birth date and no as-of date, filling "
    "year_of_birth from the run year is the default a permissive ETL takes, and it "
    "shifts every patient's age by the gap between extraction and run.",
    SILENT,
    "Overwrite year_of_birth with a value derived from the run year.",
    expect=("OMOP_BIRTH_POLICY_ENFORCED",),
)
def _invent_birth_year(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    n = con.execute("SELECT count(*) FROM person").fetchone()[0]
    if not n:
        con.close()
        return "skipped: no persons published"
    con.execute("UPDATE person SET year_of_birth = year_of_birth - 7")
    con.close()
    return f"{n:,} birth years shifted by seven years"


# --------------------------------------------------------------------------------
# MEDS-layer faults
# --------------------------------------------------------------------------------


@fault(
    "COHORT_LABEL_LEAKED_INTO_MEDS_EVENTS",
    "meds",
    "The partition a patient came from is perfectly correlated with the label. A "
    "partition column in the event stream is a free answer key, and it arrives looking "
    "like ordinary provenance.",
    SILENT,
    "Add the partition id to every MEDS event row.",
    expect=("MEDS_NO_LABEL_LEAKAGE", "MEDS_SCHEMA_VALID"),
)
def _leak_label_into_meds(layout: WorkLayout, cfg: DatasetConfig) -> str:
    shards = _meds_shards(layout)
    if not shards:
        return "skipped: MEDS not built"
    # Use whatever this dataset's first partition is actually called.
    _, links = _canonical(layout, "event_source")
    partition = links.select("partition_id").drop_nulls().head(1).item() if links.height else "partition-0"
    touched = 0
    for shard in shards[:200]:
        frame = pl.read_parquet(shard)
        _replace(shard, frame.with_columns(pl.lit(partition).alias("partition_id")))
        touched += 1
    return f"partition_id={partition!r} added to {touched} shards"


@fault(
    "AVAILABILITY_PRECEDES_OCCURRENCE",
    "meds",
    "available_time is the field that stops a model reading a lab result before the "
    "lab reported it. Defaulting it to the event time -- the obvious thing to do when "
    "a result time is missing -- removes the protection while leaving the column "
    "populated and the schema valid.",
    SILENT,
    "Move availability earlier than the event it belongs to.",
    expect=("MEDS_AVAILABILITY_PREVENTS_LEAKAGE",),
)
def _availability_before_event(layout: WorkLayout, cfg: DatasetConfig) -> str:
    shards = _meds_shards(layout)
    if not shards:
        return "skipped: MEDS not built"
    touched = 0
    for shard in shards[:200]:
        frame = pl.read_parquet(shard)
        if "available_time" not in frame.columns:
            continue
        _replace(
            shard,
            frame.with_columns(
                pl.when(pl.col("time").is_not_null())
                .then(pl.col("time") - pl.duration(hours=6))
                .otherwise(pl.col("available_time"))
                .alias("available_time")
            ),
        )
        touched += 1
    return f"availability moved six hours earlier in {touched} shards"


@fault(
    "SHARD_NOT_TIME_SORTED",
    "meds",
    "A shard whose rows are not in time order still validates against the schema. "
    "Every model that reads it as a sequence reads a shuffled history.",
    SILENT,
    "Reverse the row order inside the shards.",
    expect=("MEDS_SHARDS_CONTIGUOUS_AND_SORTED",),
)
def _unsort_shard(layout: WorkLayout, cfg: DatasetConfig) -> str:
    shards = _meds_shards(layout)
    if not shards:
        return "skipped: MEDS not built"
    touched = 0
    for shard in shards[:200]:
        frame = pl.read_parquet(shard)
        if frame.height < 2:
            continue
        _replace(shard, frame.reverse())
        touched += 1
    return f"{touched} shards reversed"


@fault(
    "SUBJECT_IN_TWO_SPLITS",
    "meds",
    "A patient present in both train and test is the oldest leak there is, and "
    "bucketing by anything that is not the subject key reintroduces it.",
    SILENT,
    "Assign one subject to a second split.",
    expect=("MEDS_SPLITS_DISJOINT_AND_COMPLETE",),
)
def _duplicate_split(layout: WorkLayout, cfg: DatasetConfig) -> str:
    path = layout.meds_dir / "metadata" / "subject_splits.parquet"
    if not path.exists():
        return "skipped: no splits written"
    splits = pl.read_parquet(path)
    first = splits.head(1).with_columns(pl.lit("held_out").alias("split"))
    _replace(path, pl.concat([splits, first]))
    return "one subject now appears in two splits"


@fault(
    "CODE_METADATA_INCOMPLETE",
    "meds",
    "codes.parquet is the only description a downstream consumer gets of what a code "
    "means. Silently dropping entries leaves events referring to codes nothing "
    "documents.",
    SILENT,
    "Remove entries from the code metadata table.",
    expect=("MEDS_CODES_METADATA_COMPLETE",),
)
def _truncate_codes(layout: WorkLayout, cfg: DatasetConfig) -> str:
    path = layout.meds_dir / "metadata" / "codes.parquet"
    if not path.exists():
        return "skipped: no code metadata"
    codes = pl.read_parquet(path)
    keep = max(1, codes.height // 2)
    _replace(path, codes.head(keep))
    return f"{codes.height - keep:,} of {codes.height:,} code descriptions removed"


#: Files that are opened for writing in place rather than replaced wholesale. These
#: must be real copies: a hard link shares the inode, so a mutation would reach through
#: the clone and corrupt the build it was cloned from.
_MUST_COPY_SUFFIXES = {".duckdb", ".db", ".sqlite", ".wal"}


def clone_work_tree(src: Path, dst: Path) -> None:
    """Clone the built artifacts into a scratch tree, hard-linking what is safe to.

    Parquet mutations here always write a new file and rename it into place, so those
    can share inodes and the clone costs nothing even on a full-size build. A database
    file cannot: it is opened read-write and modified in place. Linking one and then
    injecting an OMOP fault silently rewrites the original -- which is exactly what
    happened the first time this ran, and is why the distinction is explicit.
    """
    if dst.exists():
        shutil.rmtree(dst)

    def link_or_copy(a: str, b: str) -> None:
        if Path(a).suffix.lower() in _MUST_COPY_SUFFIXES:
            shutil.copy2(a, b)
            return
        try:
            import os

            os.link(a, b)
        except OSError:
            shutil.copy2(a, b)

    shutil.copytree(src, dst, copy_function=link_or_copy)
