"""Fault injection: does the check suite actually detect anything?

Thirty-nine checks passing on the pipeline that produced the data proves very little. What a
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

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind

#: The kinds a dose belongs to, for the faults that need a drug record.
DRUG_KINDS = (EventKind.drug_order, EventKind.drug_admin, EventKind.drug_dispense)

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


@dataclass(frozen=True)
class Injection:
    """What applying one fault did."""

    #: whether the corruption is in the clone: False when the fault skipped or raised
    injected: bool
    effect: str
    #: the exception an injection raised, or None
    error: str | None = None


def inject(fault: Fault, layout: WorkLayout, cfg: DatasetConfig) -> Injection:
    """Apply one fault, telling a corruption that took effect from one that skipped or broke.

    An injection that raises has left its clone in whatever state it reached -- the
    vocabulary fault once rewrote its shards and then failed to regenerate their code
    metadata -- and checks run on that state measure the crash, not the fault. So an
    injection that raises is not injected: nothing a check finds on its clone may be
    counted as a detection, and a caller should report it as broken.
    """
    try:
        effect = fault.apply(layout, cfg)
    except Exception as exc:  # the injection broke; the clone is not a fault's state
        return Injection(False, f"not injected: {type(exc).__name__} while injecting", f"{type(exc).__name__}: {exc}")
    return Injection(not effect.startswith("skipped"), effect)


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


def _unresolved_term_stage(con, layout: WorkLayout) -> None:
    """The connection state ``_write_code_metadata`` reads, as a MEDS stage run without a vocabulary leaves it.

    The stage creates an ``evt`` view over the canonical events and a ``term_map`` with one
    row per distinct term, spelled as ``_build_term_map`` spells it. Without a vocabulary no
    term resolves, so every row carries its normalized form and no concept -- which is what
    makes the codes the metadata computes the same SOURCE/ codes the fault writes into the
    shards. An empty map would not be that state: every code would become
    ``SOURCE/<source>/unspecified``, and the regenerated descriptions would drift with it.
    """
    from ehr2trace.terminology import normalize_term

    con.execute(f"CREATE OR REPLACE VIEW evt AS SELECT * FROM read_parquet('{layout.canonical_path('events')}')")
    con.execute(
        "CREATE OR REPLACE TABLE term_map (code_system VARCHAR, source_code VARCHAR, "
        "concept_id BIGINT, normalized VARCHAR, concept_name VARCHAR)"
    )
    terms = con.execute("SELECT DISTINCT code_system, source_code FROM evt WHERE source_code IS NOT NULL").fetchall()
    if terms:
        con.executemany(
            "INSERT INTO term_map VALUES (?, ?, NULL, ?, NULL)",
            [(system or "SOURCE", code, normalize_term(code)) for system, code in terms],
        )


@fault(
    "MEDS_BUILT_WITHOUT_VOCABULARY",
    "meds",
    "The vocabulary reaches the MEDS stage through an environment variable. A stage "
    "rerun by hand without it -- after an out-of-memory kill, on 2026-09-10 -- published "
    "311 million MIMIC-IV events with every code SOURCE/ and every concept null, beside "
    "an OMOP layer carrying 29,459 concepts, and thirty-nine checks passed on the pair.",
    SILENT,
    "Strip every concept from the shards, rewrite each mapped code in the SOURCE/ form "
    "an unresolved term takes, and regenerate codes.parquet so it still documents "
    "exactly the codes in use.",
    expect=("MEDS_CONCEPTS_ARE_OMOPS",),
)
def _meds_without_vocabulary(layout: WorkLayout, cfg: DatasetConfig) -> str:
    import duckdb

    from ehr2trace.meds import _write_code_metadata
    from ehr2trace.terminology import normalize_term

    import pyarrow.parquet as pq

    shards = _meds_shards(layout)
    if not any(pq.read_table(p, columns=["omop_concept_id"]).column(0).null_count < pq.read_metadata(p).num_rows
               for p in shards):
        return "skipped: no mapped concepts to strip"
    # Everything the metadata needs is set up before a shard is touched, so a failure here
    # leaves the clone as it was rather than half corrupted.
    con = duckdb.connect()
    try:
        _unresolved_term_stage(con, layout)
    except Exception:
        con.close()
        raise
    changed = 0
    for path in shards:
        original = pq.read_table(path)
        shard = pl.from_arrow(original)
        mapped = pl.col("omop_concept_id").is_not_null()
        n = shard.filter(mapped).height
        if not n:
            continue
        changed += n
        # The code an unresolved term gets, spelled the way the MEDS stage spells it.
        unresolved = (
            pl.lit("SOURCE/") + pl.col("source_table").fill_null("unknown") + pl.lit("/")
            + pl.col("source_code").map_elements(
                lambda v: normalize_term(v) or "unspecified", return_dtype=pl.Utf8
            )
        )
        mutated = shard.with_columns(
            pl.when(mapped & (pl.col("event_kind") != "death"))
            .then(unresolved).otherwise(pl.col("code")).alias("code"),
            pl.lit(None).cast(shard.schema["omop_concept_id"]).alias("omop_concept_id"),
        ).select(shard.columns)
        # Written back with the shard's own arrow types. A polars round trip widens
        # strings to large_string, and the MEDS schema check would fire on that rather
        # than on the missing concepts -- a detection the build that shipped never
        # offered, and so not one this fault is allowed to.
        tmp = path.with_suffix(path.suffix + ".mutating")
        pq.write_table(mutated.to_arrow().cast(original.schema), tmp)
        tmp.replace(path)
    try:
        if not changed:
            return "skipped: no mapped concepts to strip"
        _write_code_metadata(con, str(layout.meds_dir / "data" / "*" / "*.parquet"), layout)
    finally:
        con.close()
    return (f"{changed:,} rows lost their concept and their code became SOURCE/; "
            "codes.parquet regenerated to match")


#: Files that are opened for writing in place rather than replaced wholesale. These
#: must be real copies: a hard link shares the inode, so a mutation would reach through
#: the clone and corrupt the build it was cloned from.
_MUST_COPY_SUFFIXES = {".duckdb", ".db", ".sqlite", ".wal"}


@fault(
    "NOTE_TEXT_SILENTLY_DROPPED",
    "canonical",
    "`text` was a declared field role that only one shape read. A source whose rows are "
    "each a whole note could map its text column, convert without a complaint, and "
    "publish notes with no content; MIMIC-IV shipped 2,652,887 of them that way, and "
    "the OMOP exporter's coalesce turned the absence into an empty string that counted "
    "as present.",
    SILENT,
    "Blank the text on every note event, leaving the events themselves intact.",
    expect=("TEXT_SOURCES_PUBLISH_THEIR_TEXT",),
)
def _note_text_dropped(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    notes = pl.col("event_kind") == "note"
    changed = events.filter(notes & pl.col("value_text").is_not_null()).height
    if not changed:
        return "skipped: this dataset publishes no note text"
    _replace(
        epath,
        events.with_columns(
            pl.when(notes).then(None).otherwise(pl.col("value_text")).alias("value_text")
        ).select(events.columns),
    )
    return f"{changed:,} note events keep their code, time and lineage and lose their text"


# --------------------------------------------------------------------------------
# faults from the conversion audit of 2026-09-13
# --------------------------------------------------------------------------------
#
# The audit read three finished conversions and found nine kinds of damage that every
# check then in the suite reported as a clean build. They are grouped here rather than
# by layer because that is their common origin: each was a real published defect, and
# each is injected the way the audit found it -- in the artifact, not in the code that
# wrote it. `docs/CONVERSION_REMEDIATION_PLAN.md` records the incident behind each.


def _manifest(layout: WorkLayout) -> dict | None:
    import json

    path = layout.manifest_dir / "inputs.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(layout: WorkLayout, manifest: dict) -> None:
    import json

    path = layout.manifest_dir / "inputs.json"
    tmp = path.with_suffix(".mutating")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _unit_parquet(layout: WorkLayout, unit: dict) -> Path | None:
    """This tree's copy of one ingest unit's parquet.

    The manifest records an absolute path, and a clone carries the path of the tree it
    was cloned from; mutating that one would reach through the clone.
    """
    from ehr2trace.validate import artifact_in_this_tree

    path = artifact_in_this_tree(layout, unit.get("output_path"))
    return path if path is not None and layout.root in path.parents else None


def _role_column(spec, columns: list[str], role: str) -> str | None:
    """The parquet column one field role reads, matched the way the loader matches it."""
    declared = spec.fields.get(role)
    if declared is None:
        return None
    for alias in declared.from_:
        for column in columns:
            if column.startswith("col__") and column[len("col__"):].strip().lower() == alias.strip().lower():
                return column
    return None


def _another_value(text: str) -> str:
    """A different value of the same kind: the leading number doubled where there is one."""
    import re

    match = re.match(r"\s*(\d+(?:\.\d+)?)(.*)", text)
    if match:
        number = float(match.group(1))
        doubled = int(number * 2) if number.is_integer() else number * 2
        return f"{doubled}{match.group(2)}"
    return f"{text} (second strength)"


@fault(
    "ORDERS_WITH_DIFFERENT_DOSES_MERGED",
    "canonical",
    "An event identity that omits the dose makes two orders of different strengths one "
    "event, and the merge keeps whichever row it met first. The audit counted 726,710 "
    "dose disagreements inside merged groups in one export, 1,118,171 collapsed drug "
    "rows in another, and 1,327,147 prescriptions in a third.",
    SILENT,
    "Give one row of an already merged drug event a different dose.",
    expect=("DUPLICATES_AGREE",),
)
def _merge_two_doses(layout: WorkLayout, cfg: DatasetConfig) -> str:
    import pyarrow.parquet as pq

    manifest = _manifest(layout)
    if manifest is None:
        return "skipped: no ingest manifest"
    _lpath, links = _canonical(layout, "event_source")
    _epath, events = _canonical(layout, "events")
    drugs = events.filter(pl.col("event_kind").is_in([str(k) for k in DRUG_KINDS]))
    if drugs.is_empty():
        return "skipped: this dataset publishes no drug events"
    merged = (
        links.join(drugs.select("event_id", "source_id"), on="event_id", how="inner")
        .group_by("event_id", "source_id")
        .agg(pl.col("source_row_id").alias("rows"), pl.len().alias("n"))
        .filter(pl.col("n") > 1)
    )
    for row in merged.iter_rows(named=True):
        spec = cfg.sources.get(row["source_id"])
        if spec is None:
            continue
        for unit in manifest.get("inputs", []):
            if unit["source_id"] != row["source_id"]:
                continue
            path = _unit_parquet(layout, unit)
            if path is None:
                continue
            frame = pl.read_parquet(path)
            column = _role_column(spec, list(frame.columns), "dose")
            if column is None:
                continue
            target = [r for r in row["rows"] if r in set(frame["source_row_id"].to_list())]
            if not target:
                continue
            before = frame.filter(pl.col("source_row_id") == target[0])[column][0]
            if before is None:
                continue
            after = _another_value(str(before))
            _replace(path, frame.with_columns(
                pl.when(pl.col("source_row_id") == target[0])
                .then(pl.lit(after)).otherwise(pl.col(column)).alias(column)
            ).select(frame.columns))
            # The manifest must name this tree's copy, or the check reads the original.
            for entry in manifest["inputs"]:
                if entry.get("output_path") == unit.get("output_path"):
                    entry["output_path"] = str(path)
            _write_manifest(layout, manifest)
            del pq
            return (f"one of the {row['n']} rows behind a merged drug event now carries a "
                    f"different dose; the event kept the other")
    return "skipped: no merged drug event whose source maps a dose"


@fault(
    "SOURCE_PARSED_ROWS_AND_YIELDED_NOTHING",
    "canonical",
    "A wide table declared with a shape that cannot read it quarantines every row it "
    "parses and still counts as delivered: 1,564,610 emergency-department vital signs "
    "were reported present and published nothing, because presence was judged by the "
    "manifest and events by nobody.",
    SILENT,
    "Delete every event of one source, and its lineage, leaving the manifest saying the "
    "source parsed its rows.",
    expect=("SOURCE_YIELDS_EVENTS",),
)
def _source_yields_nothing(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    lpath, links = _canonical(layout, "event_source")
    counts = events.group_by("source_id").len().sort("len")
    if counts.is_empty():
        return "skipped: no events to remove"
    source_id = counts["source_id"][0]
    doomed = set(events.filter(pl.col("source_id") == source_id)["event_id"].to_list())
    _replace(epath, events.filter(pl.col("source_id") != source_id))
    _replace(lpath, links.filter(~pl.col("event_id").is_in(list(doomed))))
    return f"{len(doomed):,} events of one source deleted with their lineage"


@fault(
    "VALUE_IN_A_DIFFERENT_UNIT_THAN_ITS_LABEL",
    "canonical",
    "Values arrive in a unit their label contradicts: body temperatures with a median "
    "of 98 under a Celsius label, a respiratory rate of 196, a white cell count a "
    "thousand times its own unit. Nothing compared a value against what its unit makes "
    "possible, so all of them published as measurements.",
    SILENT,
    "Multiply measured values carrying a unit by a thousand, leaving the unit alone.",
    expect=("UNIT_VALUE_PLAUSIBLE",),
)
def _value_contradicts_unit(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    measured = (pl.col("event_kind") == str(EventKind.measurement)) & pl.col("unit_source").is_not_null()
    changed = events.filter(measured & pl.col("value_number").is_not_null()).height
    if not changed:
        return "skipped: no measured value carries a unit"
    columns = [c for c in ("value_number", "value_number_normalized") if c in events.columns]
    _replace(epath, events.with_columns([
        pl.when(measured & pl.col(c).is_not_null()).then(pl.col(c) * 1000).otherwise(pl.col(c)).alias(c)
        for c in columns
    ]).select(events.columns))
    return f"{changed:,} measured values are now a thousand times what their unit says"


@fault(
    "DOSE_UNIT_DROPPED_FROM_DRUG_ROWS",
    "omop",
    "A publisher that reads the dose unit only out of the dose text loses it wherever "
    "the source keeps it in its own column: 3,088,590 drug rows with an empty dose unit "
    "in one export, 18,567,232 in another, while every one of their sources stated it.",
    SILENT,
    "Empty dose_unit_source_value on every drug exposure.",
    expect=("DOSE_UNIT_CARRIED",),
)
def _drop_dose_unit(layout: WorkLayout, cfg: DatasetConfig) -> str:
    con = _omop_con(layout)
    try:
        before = con.execute(
            "SELECT count(*) FROM drug_exposure WHERE dose_unit_source_value IS NOT NULL"
        ).fetchone()[0]
        if not before:
            return "skipped: no drug exposure carries a dose unit"
        con.execute("UPDATE drug_exposure SET dose_unit_source_value = NULL")
    finally:
        con.close()
    return f"{int(before):,} drug exposures lost the dose unit their source stated"


@fault(
    "DEATH_DATE_AND_TIME_TREATED_AS_A_CONFLICT",
    "omop",
    "Two records of one death -- one with a date, one with a time -- were compared as "
    "timestamps, so they disagreed by construction and the publisher refused to choose. "
    "11,402 subjects lost their DEATH row that way; read as local dates, all but one of "
    "them died on the same day in both records.",
    SILENT,
    "Add a date-only death beside a timed one and withhold the person's DEATH row.",
    expect=("DEATH_PUBLISHED",),
)
def _death_date_beside_time(layout: WorkLayout, cfg: DatasetConfig) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    epath, events = _canonical(layout, "events")
    lpath, links = _canonical(layout, "event_source")
    deaths = events.filter((pl.col("event_kind") == str(EventKind.death)) & pl.col("event_time").is_not_null())
    if deaths.is_empty():
        return "skipped: no death events in this dataset"
    con = _omop_con(layout)
    try:
        try:
            published = {r[0] for r in con.execute("SELECT person_id FROM death").fetchall()}
        except Exception:
            published = set()
        if not published:
            return "skipped: no DEATH row to withhold"
        rows = con.execute(
            "SELECT event_id FROM etl_audit.lineage WHERE target_table = 'death'"
        ).fetchall()
        withheld = {r[0] for r in rows}
        chosen = deaths.filter(pl.col("event_id").is_in(list(withheld)))
        if chosen.is_empty():
            chosen = deaths
        original = chosen.row(0, named=True)
        con.execute("DELETE FROM death")
        con.execute("DELETE FROM etl_audit.lineage WHERE target_table = 'death'")
    finally:
        con.close()
    zone = cfg.time.timezone_assumption or "UTC"
    utc = original["event_time"].replace(tzinfo=ZoneInfo("UTC"))
    local = utc.astimezone(ZoneInfo(zone))
    midnight = datetime(local.year, local.month, local.day, tzinfo=ZoneInfo(zone))
    naive_utc = midnight.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    same_utc_day = naive_utc.date() == original["event_time"].date()
    fabricated = "dateonly" + str(original["event_id"])[: 64 - len("dateonly")]
    addition = (
        pl.DataFrame([original])
        .with_columns(
            pl.lit(fabricated).alias("event_id"),
            pl.lit(naive_utc).cast(events.schema["event_time"]).alias("event_time"),
        )
        .select(events.columns)
    )
    _replace(epath, pl.concat([events, addition], how="vertical_relaxed"))
    copied = links.filter(pl.col("event_id") == original["event_id"]).with_columns(
        pl.lit(fabricated).alias("event_id")
    )
    _replace(lpath, pl.concat([links, copied], how="vertical_relaxed"))
    return (
        f"a death recorded as a date ({naive_utc:%Y-%m-%d %H:%M} UTC, "
        f"{'the same' if same_utc_day else 'another'} UTC day as the timed record, the same "
        f"day in {zone}) sits beside the timed one, and every DEATH row was withheld"
    )


@fault(
    "ONE_NOTE_UNDER_TWO_ENCOUNTERS",
    "canonical",
    "A note table whose encounter id matched no other table put the same text under "
    "several of them: 491,192 groups of notes identical in patient, day, type and full "
    "text, about 936,403 surplus events, each a copy of one report.",
    SILENT,
    "Copy a note event under a second encounter id.",
    expect=("NOTE_TEXT_UNIQUE",),
)
def _note_under_two_encounters(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    lpath, links = _canonical(layout, "event_source")
    notes = events.filter((pl.col("event_kind") == str(EventKind.note)) & pl.col("value_text").is_not_null())
    if notes.is_empty():
        return "skipped: this dataset publishes no note text"
    original = notes.row(0, named=True)
    fabricated = "secondencounter" + str(original["event_id"])[: 64 - len("secondencounter")]
    other_encounter = (str(original["encounter_id"]) + "-2") if original["encounter_id"] is not None else "2"
    addition = (
        pl.DataFrame([original])
        .with_columns(pl.lit(fabricated).alias("event_id"), pl.lit(other_encounter).alias("encounter_id"))
        .select(events.columns)
    )
    _replace(epath, pl.concat([events, addition], how="vertical_relaxed"))
    copied = links.filter(pl.col("event_id") == original["event_id"]).with_columns(
        pl.lit(fabricated).alias("event_id")
    )
    _replace(lpath, pl.concat([links, copied], how="vertical_relaxed"))
    return "one note's text is now published twice under two encounter ids"


@fault(
    "RAW_COLUMN_NEVER_DECLARED",
    "ingest",
    "A delivered column nothing reads is invisible: three medication columns, a whole "
    "imaging table, an entire intensive-care module and eight other tables were never "
    "read by any conversion, and no check said so.",
    SILENT,
    "Record an extra delivered column in the manifest that no role reads.",
    expect=("RAW_COVERAGE_DECLARED",),
)
def _undeclared_raw_column(layout: WorkLayout, cfg: DatasetConfig) -> str:
    manifest = _manifest(layout)
    if manifest is None or not manifest.get("inputs"):
        return "skipped: no ingest manifest"
    extra = "an_unmapped_delivered_column"
    for unit in manifest["inputs"]:
        if unit.get("rows_parsed") and unit.get("columns"):
            unit["columns"] = list(unit["columns"]) + [extra]
            _write_manifest(layout, manifest)
            return f"one source is recorded as delivering a column nothing reads ({extra})"
    return "skipped: no parsed source to add a column to"


@fault(
    "DIAGNOSIS_TEXT_READ_AS_A_UNIT",
    "canonical",
    "A value parser that takes any text after a number as its unit turned 26 "
    "electrocardiogram diagnoses into a number with a diagnosis for a unit. The reading "
    "is not wrong about the number; it is wrong that the rest was a unit at all.",
    SILENT,
    "Store measured free text as a number whose unit is the text.",
    expect=("UNIT_KNOWN",),
)
def _text_read_as_a_unit(layout: WorkLayout, cfg: DatasetConfig) -> str:
    epath, events = _canonical(layout, "events")
    textual = (
        (pl.col("event_kind") == str(EventKind.measurement))
        & pl.col("value_text").is_not_null()
        & pl.col("value_number").is_null()
    )
    changed = events.filter(textual).height
    if not changed:
        return "skipped: no measured value is free text"
    _replace(epath, events.with_columns(
        pl.when(textual).then(pl.lit(1.0)).otherwise(pl.col("value_number")).alias("value_number"),
        pl.when(textual).then(pl.col("value_text").str.slice(0, 32)).otherwise(pl.col("unit_source")).alias("unit_source"),
        pl.when(textual).then(None).otherwise(pl.col("value_text")).alias("value_text"),
    ).select(events.columns))
    return f"{changed:,} free-text results are now a number with the text for a unit"


@fault(
    "EXCLUDED_STATUS_PUBLISHED",
    "canonical",
    "A problem list records what was entered and what was taken back. 30,069 rows whose "
    "status says the entry was deleted were published as diagnoses, because the status "
    "reached the event and nothing acted on it.",
    SILENT,
    "Give published events the status their source declares excluded.",
    expect=("EXCLUDED_STATUS_NOT_PUBLISHED",),
)
def _excluded_status_published(layout: WorkLayout, cfg: DatasetConfig) -> str:
    declaring = [(sid, spec.excluded_status[0]) for sid, spec in cfg.sources.items() if spec.excluded_status]
    if not declaring:
        return "skipped: no source declares an excluded status"
    epath, events = _canonical(layout, "events")
    for source_id, status in declaring:
        subset = pl.col("source_id") == source_id
        changed = events.filter(subset).height
        if not changed:
            continue
        _replace(epath, events.with_columns(
            pl.when(subset).then(pl.lit(status)).otherwise(pl.col("status_source")).alias("status_source")
        ).select(events.columns))
        return f"{changed:,} published events now carry a status their source excludes"
    return "skipped: the sources declaring an excluded status published no events"


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
