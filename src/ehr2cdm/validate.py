"""Reconciliation and compliance checks (design section 10.2).

Two things this module refuses to do.

It does not require source row count to equal target row count. Relations here are
genuinely one-to-many and many-to-one -- one report is many rows, one row is many
measurements, one fact appears in two batches -- so an equality check would either
fail forever or be quietly weakened until it passed. Coverage, fan-out, deduplication
and quarantine rates are reported together instead, which is what actually tells you
whether a conversion is complete.

It does not soften a failure into a warning. A check that fails means the output is not
publishable, and the command exits non-zero.

The checks here cover design section 10.2. Where a requirement is better expressed as a
test than as a runtime check -- the measured baselines, determinism across worker
counts, resumption after a crash -- it lives in ``tests/`` instead, and the mapping is:

=====================================  =========================================
Design section 10.2                    Where it is checked
=====================================  =========================================
1-5   input completeness, anomalies     ``INPUT_MANIFEST_COMPLETE``,
                                        ``SOURCE_COVERAGE_REPORTED``,
                                        ``tests/integration/test_ctpe_baselines.py``
6-8   type drift and per-cell typing    ``tests/unit/test_hashing.py``,
                                        ``tests/unit/test_adapters.py``
9-18  semantic correctness              ``ANCHOR_NEVER_AN_EVENT_TIME`` and the
                                        other semantic checks below
19-24 target-layer compliance           the ``OMOP_*`` and ``MEDS_*`` checks below
25-27 determinism and resumption        ``tests/integration/test_generic_ehr_pipeline.py``
28-30 generalization                    ``tests/test_no_hardcoded_dataset_strings.py``,
                                        ``tests/integration/test_generic_ehr_pipeline.py``
=====================================  =========================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import polars as pl

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.schema import EventKind, QualityFlag


@dataclass
class CheckResult:
    check_id: str
    passed: bool
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False


CHECKS: list[tuple[str, Callable]] = []

#: Below this length a cohort label is an ordinary English word, and matching it against
#: clinical free text produces false alarms rather than findings.
MIN_DISTINCTIVE_LABEL = 4


def check(check_id: str, slow: bool = False):
    def deco(fn):
        CHECKS.append((check_id, (fn, slow)))
        return fn

    return deco


#: The columns the checks in this module actually read, per canonical table.
#:
#: Loading the whole canonical layer eagerly worked until a dataset arrived where it
#: did not: on MIMIC-IV, `validate` was killed by the kernel at 159 GB resident, holding
#: 296 million events and 301 million lineage links complete with every column neither
#: it nor any check ever looked at -- the raw source text of each quarantined row, and a
#: source row id per link, being the two largest.
#:
#: `test_validate_columns.py` asserts this stays in step with the checks, so a check
#: that starts reading a new column fails the test rather than failing at runtime on
#: whichever dataset is big enough to notice.
READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "events": (
        "event_id", "subject_id", "event_kind", "event_time", "available_time",
        "end_time", "code_system", "source_code", "value_number", "value_text",
        "quality_flags", "source_id",
    ),
    "event_source": ("event_id", "partition_id"),
    "anchors": ("subject_id", "anchor_date", "anchor_time", "anchor_time_known", "partition_id"),
    "cohort_membership": ("subject_id", "partition_id", "membership_label", "label_scope"),
    "quality_issue": (),
    "quarantine": ("partition_id", "source_id", "reason", "person_source_id"),
}


@dataclass
class Layers:
    """Whatever has been built so far. Checks for missing layers skip, not fail."""

    cfg: DatasetConfig
    layout: WorkLayout
    events: pl.DataFrame | None
    links: pl.DataFrame | None
    anchors: pl.DataFrame | None
    memberships: pl.DataFrame | None
    issues: pl.DataFrame | None
    quarantine: pl.DataFrame | None
    manifest: dict[str, Any] | None

    @classmethod
    def load(cls, cfg: DatasetConfig, layout: WorkLayout) -> "Layers":
        def read(name: str) -> pl.DataFrame | None:
            path = layout.canonical_path(name)
            if not path.exists():
                return None
            wanted = READ_COLUMNS.get(name)
            if wanted is None:
                return pl.read_parquet(path)
            # Only the columns the checks read. Projection happens in the parquet
            # reader, so the columns left out are never decompressed at all.
            present = set(pl.scan_parquet(path).collect_schema().names())
            return pl.read_parquet(path, columns=[c for c in wanted if c in present])

        manifest_path = layout.manifest_dir / "inputs.json"
        return cls(
            cfg=cfg,
            layout=layout,
            events=read("events"),
            links=read("event_source"),
            anchors=read("anchors"),
            memberships=read("cohort_membership"),
            issues=read("quality_issue"),
            quarantine=read("quarantine"),
            manifest=json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
        )


def run_checks(cfg: DatasetConfig, layout: WorkLayout, include_slow: bool = False) -> list[CheckResult]:
    layers = Layers.load(cfg, layout)
    results: list[CheckResult] = []
    for check_id, (fn, slow) in CHECKS:
        if slow and not include_slow:
            continue
        try:
            result = fn(layers)
        except Exception as exc:  # a check that crashes is a failing check
            result = CheckResult(check_id, False, f"check raised {type(exc).__name__}: {exc}")
        if result is None:
            continue
        result.check_id = check_id
        results.append(result)
    path = layout.runs_dir / "validation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([r.__dict__ for r in results], indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return results


def _skip(reason: str) -> CheckResult:
    return CheckResult("", True, f"skipped: {reason}", skipped=True)


# --------------------------------------------------------------------------------
# input completeness and reconciliation
# --------------------------------------------------------------------------------


@check("INPUT_MANIFEST_COMPLETE")
def _manifest_complete(l: Layers) -> CheckResult:
    if l.manifest is None:
        return _skip("no ingest manifest")
    inputs = l.manifest["inputs"]
    missing_hash = [i["file_path"] for i in inputs if not i.get("file_sha256")]
    return CheckResult(
        "",
        not missing_hash,
        f"{len(inputs)} logical units recorded, all hashed"
        if not missing_hash
        else f"{len(missing_hash)} units missing a content hash",
        {"units": len(inputs), "files": len({i["file_path"] for i in inputs})},
    )


@check("RECONCILIATION_ROWS")
def _reconciliation(l: Layers) -> CheckResult:
    if l.manifest is None:
        return _skip("no ingest manifest")
    bad = [
        f"{i['partition_id']}/{i['source_id']}"
        for i in l.manifest["inputs"]
        if i["rows_read"] != i["rows_parsed"] + i["rows_quarantined"]
    ]
    total_read = sum(i["rows_read"] for i in l.manifest["inputs"])
    total_q = sum(i["rows_quarantined"] for i in l.manifest["inputs"])
    return CheckResult(
        "",
        not bad,
        f"every source reconciles: {total_read:,} read = parsed + quarantined"
        if not bad
        else f"rows unaccounted for in: {bad[:5]}",
        {
            "rows_read": total_read,
            "rows_quarantined": total_q,
            "quarantine_rate": round(total_q / total_read, 6) if total_read else 0.0,
        },
    )


@check("SOURCE_COVERAGE_REPORTED")
def _coverage(l: Layers) -> CheckResult:
    """Absent and empty sources are reported as coverage, never as a negative fact.

    An empty sheet means this extract carried no such data. It does not mean the
    patient had none, and nothing downstream may treat it that way -- so what matters
    is that the distinction is recorded at all.
    """
    if l.manifest is None:
        return _skip("no ingest manifest")
    from ehr2cdm.discover import resolve_source_units

    present = {(i["partition_id"], i["source_id"]) for i in l.manifest["inputs"]}
    empty = {
        (i["partition_id"], i["source_id"])
        for i in l.manifest["inputs"]
        if i["rows_parsed"] == 0
    }
    not_extracted: list[str] = []
    for part in l.cfg.partitions:
        for source_id, spec in l.cfg.sources_for(part.id).items():
            if (part.id, source_id) in present:
                continue
            if spec.required:
                return CheckResult(
                    "", False, f"required source {source_id} missing from {part.id}"
                )
            not_extracted.append(f"{part.id}/{source_id}")
    return CheckResult(
        "",
        True,
        f"{len(present)} sources present ({len(empty)} empty), "
        f"{len(not_extracted)} not extracted and recorded as such",
        {"present": len(present), "empty": sorted(f"{p}/{s}" for p, s in empty), "not_extracted": sorted(not_extracted)},
    )


@check("EVENT_LINEAGE_COMPLETE")
def _lineage_complete(l: Layers) -> CheckResult:
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    # An anti-join, not two Python sets. Building `set(...to_list())` over a link table
    # materialises one Python string object per row; at MIMIC-IV's 301 million links
    # that is what killed this command at 159 GB, while the Arrow frames it was built
    # from cost a fraction of it.
    orphans = l.events.select("event_id").join(
        l.links.select("event_id").unique(), on="event_id", how="anti"
    ).height
    return CheckResult(
        "",
        not orphans,
        "every canonical event traces to at least one source row"
        if not orphans
        else f"{orphans:,} events have no source row",
        {"events": l.events.height, "links": l.links.height},
    )


@check("LINK_TARGETS_EXIST")
def _links_resolve(l: Layers) -> CheckResult:
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    dangling = l.links.select("event_id").unique().join(
        l.events.select("event_id"), on="event_id", how="anti"
    ).height
    return CheckResult(
        "", not dangling, "no dangling lineage links" if not dangling else f"{dangling:,} dangling links"
    )


@check("FAN_OUT_AND_DEDUP")
def _fan_out(l: Layers) -> CheckResult:
    """Reported, never asserted equal: these relations are legitimately not one-to-one."""
    if l.events is None or l.links is None:
        return _skip("canonical layer not built")
    per_event = l.links.group_by("event_id").len()
    duplicated = int((per_event["len"] > 1).sum())
    return CheckResult(
        "",
        True,
        f"{l.events.height:,} events from {l.links.height:,} links; "
        f"{duplicated:,} events have more than one source row",
        {
            "events": l.events.height,
            "links": l.links.height,
            "events_with_duplicates": duplicated,
            "fan_out": round(l.links.height / l.events.height, 4) if l.events.height else 0.0,
        },
    )


# --------------------------------------------------------------------------------
# semantic correctness
# --------------------------------------------------------------------------------


@check("SOURCE_ROWS_ACCOUNTED")
def _rows_accounted(l: Layers) -> CheckResult:
    """Where every parsed source row ended up.

    A row is allowed to produce no event: a demographics row carrying nothing but a
    patient key states no fact, and inventing one would be worse. What is not allowed
    is for that to be invisible. This reports the destinations, so a shape that
    silently drops rows shows up as a number that moved rather than as nothing at all.

    The destinations overlap, which is the subtlety. One demographics row can yield a
    gender event *and* have its untimed body-mass index withheld, so it is both linked
    and quarantined. Counting the two separately and subtracting would claim more rows
    were accounted for than existed -- which is how this check failed the first time it
    ran.
    """
    if l.manifest is None or l.links is None:
        return _skip("canonical layer not built")
    parsed = sum(i["rows_parsed"] for i in l.manifest["inputs"])
    if not parsed:
        return _skip("nothing ingested")

    import duckdb

    links_path = l.layout.canonical_path("event_source")
    quarantine_path = l.layout.canonical_path("quarantine")
    con = duckdb.connect()
    try:
        con.execute("PRAGMA preserve_insertion_order = false")
        con.execute(f"CREATE VIEW lnk AS SELECT * FROM read_parquet('{links_path}')")
        if quarantine_path.exists():
            con.execute(f"CREATE VIEW qtn AS SELECT * FROM read_parquet('{quarantine_path}')")
        else:
            con.execute("CREATE VIEW qtn AS SELECT NULL AS source_row_id WHERE false")
        linked, quarantined, both, accounted = con.execute(
            """
            WITH l AS (SELECT DISTINCT source_row_id FROM lnk),
                 q AS (SELECT DISTINCT source_row_id FROM qtn WHERE source_row_id IS NOT NULL)
            SELECT (SELECT count(*) FROM l),
                   (SELECT count(*) FROM q),
                   (SELECT count(*) FROM l SEMI JOIN q USING (source_row_id)),
                   (SELECT count(*) FROM (SELECT source_row_id FROM l
                                          UNION SELECT source_row_id FROM q))
            """
        ).fetchone()
    finally:
        con.close()

    carried_nothing = parsed - int(accounted)
    return CheckResult(
        "",
        carried_nothing >= 0,
        f"{int(linked):,} rows became events, {int(quarantined):,} were quarantined with a "
        f"reason ({int(both):,} both), {carried_nothing:,} carried no fact "
        f"({carried_nothing / parsed:.4%} of parsed)"
        if carried_nothing >= 0
        else f"more rows accounted for ({int(accounted):,}) than were parsed ({parsed:,})",
        {
            "parsed": parsed,
            "became_events": int(linked),
            "quarantined": int(quarantined),
            "both": int(both),
            "carried_no_fact": int(carried_nothing),
        },
    )


@check("ANCHOR_NEVER_AN_EVENT_TIME")
def _anchor_not_event_time(l: Layers) -> CheckResult:
    """Structural, not statistical: no source may wire an anchor column to a time role."""
    offenders = []
    for name, spec in l.cfg.sources.items():
        anchor = spec.fields.get("anchor_time")
        if not anchor:
            continue
        anchor_aliases = {a.lower() for a in anchor.from_}
        for role in ("event_time", "available_time", "end_time"):
            other = spec.fields.get(role)
            if other and anchor_aliases & {a.lower() for a in other.from_}:
                offenders.append(f"{name}.{role}")
    return CheckResult(
        "",
        not offenders,
        "no source maps its anchor column onto a clinical time"
        if not offenders
        else f"anchor column reused as a clinical time in: {offenders}",
    )


@check("CANONICAL_SCHEMA_AS_DECLARED")
def _canonical_schema_as_declared(l: Layers) -> CheckResult:
    """The canonical tables carry the declared columns and nothing else.

    Added after fault injection found nothing firing when a physical bucketing key was
    appended to the event table. Writing canonical output with hive partitioning had
    once done exactly that: every table silently grew a column encoding how the data
    was sharded, no check noticed, and a downstream consumer would have read a storage
    detail as a clinical field. A unit test covered the writer; nothing covered the
    artifact, which is what actually ships.
    """
    from ehr2cdm.schema import (
        ANCHOR_SCHEMA,
        CANONICAL_EVENT_SCHEMA,
        COHORT_MEMBERSHIP_SCHEMA,
        EVENT_SOURCE_SCHEMA,
        QUARANTINE_SCHEMA,
    )

    declared = {
        "events": CANONICAL_EVENT_SCHEMA,
        "event_source": EVENT_SOURCE_SCHEMA,
        "anchors": ANCHOR_SCHEMA,
        "cohort_membership": COHORT_MEMBERSHIP_SCHEMA,
        "quarantine": QUARANTINE_SCHEMA,
    }
    drift = []
    checked = 0
    for name, schema in declared.items():
        path = l.layout.canonical_path(name)
        if not path.exists():
            continue
        checked += 1
        import pyarrow.parquet as pq

        actual = list(pq.ParquetFile(path).schema_arrow.names)
        expected = list(schema.names)
        extra = [c for c in actual if c not in expected]
        missing = [c for c in expected if c not in actual]
        if extra or missing:
            drift.append(f"{name}: extra={extra} missing={missing}")
    if not checked:
        return _skip("canonical layer not built")
    return CheckResult(
        "",
        not drift,
        f"{checked} canonical tables carry exactly their declared columns"
        if not drift
        else f"canonical schema drift: {drift}",
        {"tables_checked": checked},
    )


@check("EVENT_SUBJECTS_WERE_ISSUED_BY_IDENTITY")
def _event_subjects_issued(l: Layers) -> CheckResult:
    """Every subject id in the canonical layer came from the identity map.

    Added after fault injection: reassigning subject ids in the event table so that one
    patient became several went undetected, because the identity check reads the
    identity map and the map was still internally consistent. Nothing joined the two.
    A patient split across partitions inflates the cohort and truncates every timeline,
    and it is invisible to any check that only looks at one artifact at a time.
    """
    path = l.layout.identity_dir / "subject_map.parquet"
    if not path.exists() or l.events is None:
        return _skip("identity or events not built")
    issued = pl.read_parquet(path, columns=["subject_id"])
    strays: dict[str, int] = {}
    for name, frame in (("events", l.events), ("anchors", l.anchors), ("cohort_membership", l.memberships)):
        if frame is None or frame.height == 0 or "subject_id" not in frame.columns:
            continue
        # Distinct first, then anti-join. There are a few hundred thousand subjects and
        # a few hundred million events, so materialising one Python object per event to
        # find them would cost three orders of magnitude more than the answer is worth.
        unknown = (
            frame.select("subject_id").drop_nulls().unique().join(issued, on="subject_id", how="anti").height
        )
        if unknown:
            strays[name] = unknown
    return CheckResult(
        "",
        not strays,
        f"every subject id in the canonical layer is one of the {issued.height:,} the identity map issued"
        if not strays
        else f"subject ids never issued by identity resolution: {strays}",
        {"issued": issued.height, "strays": strays},
    )


@check("ANCHOR_TIMES_ARE_NOT_THE_EVENT_CLOCK")
def _anchor_not_the_clock(l: Layers) -> CheckResult:
    """No subject's event times collapse onto that subject's anchor times.

    The structural check above reads the config and catches a source that wires an
    anchor column to a time role. Fault injection showed that is not enough: a build
    whose *data* already carries anchor dates as clinical times passes it untouched,
    because the config it is derived from is innocent. This is the companion that looks
    at what was actually written.

    The signature is set containment, not a threshold. Events legitimately fall on an
    anchor date -- that is why the anchor exists. What cannot happen is a subject with
    many events across several sources whose every distinct timestamp is one of their
    handful of anchor timestamps: at that point the anchor is the clock.
    """
    if l.events is None or l.anchors is None or l.anchors.height == 0:
        return _skip("no anchors in this dataset")
    #: Below this a subject's timeline is too short for containment to mean anything.
    MIN_EVENTS = 8
    anchors = (
        l.anchors.select("subject_id", pl.col("anchor_date").cast(pl.Datetime("us")).alias("t"))
        .drop_nulls()
        .group_by("subject_id")
        .agg(pl.col("t").unique().alias("anchor_times"))
    )
    events = (
        l.events.select("subject_id", pl.col("event_time").alias("t"))
        .drop_nulls()
        .group_by("subject_id")
        .agg(pl.col("t").unique().alias("event_times"), pl.len().alias("n_events"))
        .filter(pl.col("n_events") >= MIN_EVENTS)
    )
    joined = events.join(anchors, on="subject_id", how="inner")
    if joined.height == 0:
        return _skip("no subject has both anchors and enough events")
    collapsed = joined.filter(
        pl.col("event_times").list.set_difference(pl.col("anchor_times")).list.len() == 0
    )
    return CheckResult(
        "",
        collapsed.height == 0,
        f"{joined.height:,} subjects have both anchors and a timeline; none of them "
        f"has event times drawn only from their anchors"
        if collapsed.height == 0
        else f"{collapsed.height:,} subjects have no event time that is not an anchor time: "
        "the extraction anchor has been used as the clinical clock",
        {"subjects_examined": joined.height, "collapsed": collapsed.height},
    )


@check("ANCHORS_ARE_NOT_EVENTS")
def _anchors_not_events(l: Layers) -> CheckResult:
    if l.events is None or l.anchors is None or l.anchors.height == 0:
        return _skip("no anchors in this dataset")
    anchor_type = l.cfg.anchors.anchor_type if l.cfg.anchors else ""
    leaked = l.events.filter(pl.col("source_code").str.to_lowercase() == anchor_type.lower())
    known = int(l.anchors.filter(pl.col("anchor_time_known")).height)
    return CheckResult(
        "",
        leaked.height == 0,
        f"{l.anchors.height:,} anchors recorded separately from events "
        f"({known:,} with a known time component)"
        if leaked.height == 0
        else f"{leaked.height} events look like fabricated anchor procedures",
        {"anchors": l.anchors.height, "anchor_time_known": known},
    )


@check("COHORT_LABEL_NEVER_A_CLINICAL_FACT")
def _label_not_a_fact(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    labels = {p.membership_label for p in l.cfg.partitions if p.membership_label}
    if not labels:
        return _skip("dataset has no membership labels")
    lowered = {s.lower() for s in labels}
    hits = l.events.filter(
        pl.col("event_kind").is_in([str(EventKind.condition), str(EventKind.demographic)])
        & (
            pl.col("source_code").str.to_lowercase().is_in(list(lowered))
            | pl.col("value_text").str.to_lowercase().is_in(list(lowered))
        )
    )
    return CheckResult(
        "",
        hits.height == 0,
        "no cohort label became a condition or observation"
        if hits.height == 0
        else f"{hits.height} events carry a cohort label as a clinical fact",
    )


@check("MEMBERSHIP_KEPT_PER_PARTITION")
def _membership_kept(l: Layers) -> CheckResult:
    """A patient in two differently-labelled partitions keeps both rows, unmerged."""
    if l.memberships is None or l.memberships.height == 0:
        return _skip("dataset has no cohort membership")
    per_subject = l.memberships.group_by("subject_id").agg(
        pl.col("membership_label").n_unique().alias("labels")
    )
    conflicted = int((per_subject["labels"] > 1).sum())
    scopes = set(l.memberships["label_scope"].to_list())
    patient_level = scopes == {"patient"}
    return CheckResult(
        "",
        not patient_level,
        f"{conflicted:,} subjects carry more than one cohort label, all kept separately "
        f"(scope: {sorted(scopes)})",
        {"subjects_with_conflicting_labels": conflicted, "scopes": sorted(scopes)},
    )


@check("NO_UNTIMED_CLINICAL_EVENTS")
def _no_untimed_clinical(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    untimed = l.events.filter(
        pl.col("event_time").is_null() & (pl.col("event_kind") != str(EventKind.demographic))
    )
    return CheckResult(
        "",
        untimed.height == 0,
        "every clinical event has a real time; only static attributes are timeless"
        if untimed.height == 0
        else f"{untimed.height} clinical events have no time and were still published",
    )


@check("ORDERS_AND_ADMINISTRATIONS_STAY_SEPARATE")
def _orders_vs_admin(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    orders = l.events.filter(pl.col("event_kind") == str(EventKind.drug_order))
    admins = l.events.filter(pl.col("event_kind") == str(EventKind.drug_admin))
    overlap = orders.select("event_id").join(admins.select("event_id"), on="event_id", how="semi").height
    return CheckResult(
        "",
        not overlap,
        f"{orders.height:,} orders and {admins.height:,} administrations, no shared events"
        if not overlap
        else f"{overlap:,} events are both an order and an administration",
        {"drug_order": orders.height, "drug_admin": admins.height},
    )


@check("POST_DEATH_RECORDS_FLAGGED_NOT_DELETED")
def _post_death(l: Layers) -> CheckResult:
    if l.events is None:
        return _skip("canonical layer not built")
    flagged = l.events.filter(
        pl.col("quality_flags").list.contains(str(QualityFlag.RECORDED_AFTER_DEATH))
    )
    return CheckResult(
        "",
        True,
        f"{flagged.height:,} records dated after death, flagged and kept with their original dates",
        {"recorded_after_death": flagged.height},
    )


@check("QUARANTINE_IS_EXPLAINED")
def _quarantine_explained(l: Layers) -> CheckResult:
    if l.quarantine is None or l.quarantine.height == 0:
        return CheckResult("", True, "nothing quarantined at the canonical stage")
    unexplained = l.quarantine.filter(pl.col("reason").is_null() | (pl.col("reason") == ""))
    by_reason = dict(
        zip(
            l.quarantine.group_by("reason").len()["reason"].to_list(),
            l.quarantine.group_by("reason").len()["len"].to_list(),
        )
    )
    return CheckResult(
        "",
        unexplained.height == 0,
        f"{l.quarantine.height:,} rows quarantined, each with a reason"
        if unexplained.height == 0
        else f"{unexplained.height} quarantined rows have no reason recorded",
        {"by_reason": by_reason},
    )


# --------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------


@check("IDENTITY_RESOLVED_ACROSS_PARTITIONS")
def _identity(l: Layers) -> CheckResult:
    path = l.layout.identity_dir / "subject_map.parquet"
    if not path.exists():
        return _skip("identity not built")
    subjects = pl.read_parquet(path)
    duplicate_keys = subjects.height - subjects["person_source_id"].n_unique()
    duplicate_ids = subjects.height - subjects["subject_id"].n_unique()
    multi = int((subjects["partitions"].list.len() > 1).sum())
    per_partition = {
        p.id: int(subjects.filter(pl.col("partitions").list.contains(p.id)).height)
        for p in l.cfg.partitions
    }
    return CheckResult(
        "",
        duplicate_keys == 0 and duplicate_ids == 0,
        f"{subjects.height:,} subjects; {multi:,} appear in more than one partition "
        f"and resolve to a single subject each"
        if duplicate_keys == 0 and duplicate_ids == 0
        else "patient keys or subject ids are not unique",
        {"subjects": subjects.height, "multi_partition": multi, "per_partition": per_partition},
    )


# --------------------------------------------------------------------------------
# OMOP
# --------------------------------------------------------------------------------


def _omop_connection(l: Layers):
    """A connection to a *populated* OMOP database, or None.

    An empty database is not a passing OMOP build. A partial run can leave the file
    behind with its tables created and nothing in them, and every check that counts
    violations then finds none and reports success -- "every clinical row resolves to
    one of the 0 published persons" is true and worthless. Treating empty as absent
    turns that whole class of vacuous pass into a skip, which is what it is.
    """
    path = l.layout.omop_dir / "omop.duckdb"
    if not path.exists():
        return None
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        populated = con.execute("SELECT count(*) FROM person").fetchone()[0]
    except Exception:
        con.close()
        return None
    if not populated:
        con.close()
        return None
    return con


@check("OMOP_EVERY_ROW_HAS_LINEAGE")
def _omop_lineage(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        tables = {
            "person": "person_id",
            "visit_occurrence": "visit_occurrence_id",
            "condition_occurrence": "condition_occurrence_id",
            "drug_exposure": "drug_exposure_id",
            "procedure_occurrence": "procedure_occurrence_id",
            "measurement": "measurement_id",
            "note": "note_id",
            "death": "person_id",
        }
        missing: dict[str, int] = {}
        counts: dict[str, int] = {}
        for table, pk in tables.items():
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            counts[table] = n
            if not n:
                continue
            orphan = con.execute(
                f"""
                SELECT count(*) FROM {table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM etl_audit.lineage l
                    WHERE l.target_table = '{table}' AND l.target_pk = t.{pk}
                )
                """
            ).fetchone()[0]
            if orphan:
                missing[table] = orphan
        return CheckResult(
            "",
            not missing,
            "every OMOP row traces back to a source row" if not missing else f"rows without lineage: {missing}",
            counts,
        )
    finally:
        con.close()


@check("OMOP_CONCEPTS_EXIST_AND_FIT_THEIR_DOMAIN")
def _omop_concepts(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        from ehr2cdm.terminology import Vocabulary

        import os

        vocab = Vocabulary.open(Path(os.environ["OMOP_VOCAB_DIR"]) if os.environ.get("OMOP_VOCAB_DIR") else None)
        checks = [
            ("condition_occurrence", "condition_concept_id", "Condition"),
            ("drug_exposure", "drug_concept_id", "Drug"),
            ("procedure_occurrence", "procedure_concept_id", "Procedure"),
            ("measurement", "measurement_concept_id", "Measurement"),
        ]
        # Checked with one join per table rather than two queries per concept. On this
        # export that is ~10,000 concepts against a 4.16-million-row vocabulary; asking
        # about them one at a time turned a check into a coffee break.
        bad: list[str] = []
        nonzero = 0
        for table, column, domain in checks:
            ids = [
                int(r[0])
                for r in con.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} <> 0"
                ).fetchall()
            ]
            nonzero += len(ids)
            if not ids or not vocab.available:
                bad.extend(
                    f"{table}.{column}={i} not in vocabulary" for i in ids if not vocab.concept_exists(i)
                )
                continue
            vocab.con.execute("CREATE OR REPLACE TEMP TABLE _ids (concept_id BIGINT)")
            vocab.con.executemany("INSERT INTO _ids VALUES (?)", [(i,) for i in ids])
            rows = vocab.con.execute(
                """
                SELECT i.concept_id, c.domain_id
                FROM _ids i
                LEFT JOIN CONCEPT c ON CAST(c.concept_id AS BIGINT) = i.concept_id
                """
            ).fetchall()
            vocab.con.execute("DROP TABLE IF EXISTS _ids")
            for concept_id, found_domain in rows:
                if found_domain is None:
                    bad.append(f"{table}.{column}={concept_id} not in vocabulary")
                elif found_domain != domain:
                    bad.append(f"{table}.{column}={concept_id} is not a {domain} concept")
        vocab.close()
        return CheckResult(
            "",
            not bad,
            f"{nonzero} distinct non-zero concept ids, all present with a compatible domain"
            if not bad
            else f"invalid concept usage: {bad[:5]}",
            {"distinct_nonzero_concepts": nonzero, "vocabulary": vocab.version},
        )
    finally:
        con.close()


@check("OMOP_REFERENTIAL_INTEGRITY")
def _omop_foreign_keys(l: Layers) -> CheckResult:
    """No clinical row may reference a person or visit that was never published.

    This is the check that catches a birth-year policy applied to PERSON only: the
    database would happily hold thirty million rows pointing at nobody.
    """
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        dangling: dict[str, int] = {}
        for table in (
            "visit_occurrence",
            "condition_occurrence",
            "drug_exposure",
            "procedure_occurrence",
            "measurement",
            "note",
            "death",
            "observation_period",
        ):
            n = con.execute(
                f"SELECT count(*) FROM {table} t "
                "WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = t.person_id)"
            ).fetchone()[0]
            if n:
                dangling[f"{table}.person_id"] = int(n)
        for table in ("condition_occurrence", "drug_exposure", "procedure_occurrence", "measurement", "note"):
            n = con.execute(
                f"SELECT count(*) FROM {table} t WHERE t.visit_occurrence_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM visit_occurrence v "
                "WHERE v.visit_occurrence_id = t.visit_occurrence_id)"
            ).fetchone()[0]
            if n:
                dangling[f"{table}.visit_occurrence_id"] = int(n)
        people = con.execute("SELECT count(*) FROM person").fetchone()[0]
        return CheckResult(
            "",
            not dangling,
            f"every clinical row resolves to one of the {people:,} published persons"
            if not dangling
            else f"dangling references: {dangling}",
            {"person": int(people)},
        )
    finally:
        con.close()


@check("OMOP_PRIMARY_KEYS_UNIQUE")
def _omop_primary_keys(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        duplicates: dict[str, int] = {}
        for table, pk in (
            ("person", "person_id"),
            ("visit_occurrence", "visit_occurrence_id"),
            ("condition_occurrence", "condition_occurrence_id"),
            ("drug_exposure", "drug_exposure_id"),
            ("procedure_occurrence", "procedure_occurrence_id"),
            ("measurement", "measurement_id"),
            ("note", "note_id"),
            ("death", "person_id"),
            ("observation_period", "observation_period_id"),
        ):
            n = con.execute(
                f"SELECT count(*) - count(DISTINCT {pk}) FROM {table}"
            ).fetchone()[0]
            if n:
                duplicates[table] = int(n)
        return CheckResult(
            "",
            not duplicates,
            "primary keys are unique in every table"
            if not duplicates
            else f"duplicate keys: {duplicates}",
        )
    finally:
        con.close()


@check("OMOP_BIRTH_YEAR_IS_REPRODUCIBLE")
def _omop_birth_year_reproducible(l: Layers) -> CheckResult:
    """Every published year_of_birth recomputes from the source age and the declared date.

    Added after fault injection: shifting every birth year by seven years was detected
    by nothing. The policy check below verifies that the right *policy* was applied and
    that derived years were flagged -- it never compares the published number to the
    number the source implies, so any systematic offset survives it. Age is a covariate
    in effectively every clinical model built on this data, and an offset applied to
    everyone is invisible to a distribution check as well.

    This is a reproduction, not a heuristic: under an approved approximation the year is
    year(age_as_of_date) - age by definition, so a mismatch is arithmetic, not judgement.

    Getting from a person_id back to a subject_id goes through the lineage table, which
    is the only thing that connects the two -- another reason every published row is
    required to carry one.
    """
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        policy = l.cfg.omop.person_birth_policy
        if policy.mode != "approved_approximation" or not policy.age_as_of_date:
            return _skip("no age-derived birth years to reproduce")
        if l.events is None:
            return _skip("canonical events unavailable")
        reference_year = int(str(policy.age_as_of_date)[:4])

        rows = con.execute(
            """
            SELECT p.person_id, p.year_of_birth, ln.event_id
            FROM person p
            JOIN etl_audit.lineage ln
              ON ln.target_table = 'person' AND ln.target_pk = p.person_id
            """
        ).fetchall()
        if not rows:
            return _skip("no person rows carry lineage")
        published = pl.DataFrame(
            rows, schema=["person_id", "year_of_birth", "event_id"], orient="row"
        ).unique()

        # Only persons whose year was actually derived from an age. One with a recorded
        # date of birth is not reproducible from a reference year and must not be
        # compared against one.
        with_dates = set(
            l.events.filter(pl.col("source_code") == "BIRTH_DATE")["subject_id"].to_list()
        )
        ages = (
            l.events.filter(pl.col("source_code") == "AGE")
            .filter(~pl.col("subject_id").is_in(list(with_dates)) if with_dates else pl.lit(True))
            .select("event_id", "subject_id", pl.col("value_number").alias("age"))
            .drop_nulls("age")
        )
        if ages.height == 0:
            return _skip("no source ages recorded in canonical events")

        # Lineage links a person to every demographic event it was built from; only the
        # age-bearing one can be reproduced against.
        joined = published.join(ages, on="event_id", how="inner").unique(subset=["person_id"])
        if joined.height == 0:
            return _skip("no published person traces back to a recorded source age")

        mismatched = joined.filter(
            (pl.lit(reference_year) - pl.col("age").cast(pl.Int64)) != pl.col("year_of_birth")
        )
        return CheckResult(
            "",
            mismatched.height == 0,
            f"{joined.height:,} birth years recompute exactly from the source age "
            f"and the declared reference date {policy.age_as_of_date}"
            if mismatched.height == 0
            else f"{mismatched.height:,} of {joined.height:,} published birth years do not "
            f"recompute from the source age and {policy.age_as_of_date}",
            {"checked": joined.height, "mismatched": mismatched.height},
        )
    finally:
        con.close()


@check("OMOP_BIRTH_POLICY_ENFORCED")
def _omop_birth_policy(l: Layers) -> CheckResult:
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        policy = l.cfg.omop.person_birth_policy
        people = con.execute("SELECT count(*) FROM person").fetchone()[0]
        blocked = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue WHERE issue_type = 'OMOP_PERSON_BLOCKED'"
        ).fetchone()[0]
        if policy.mode == "strict":
            ok = people == 0 or blocked == 0
            detail = (
                f"strict policy: {blocked:,} patients withheld from PERSON for lack of a "
                f"derivable year of birth, {people:,} published"
            )
            # Under strict, a published person can only exist if a year of birth came
            # from the data itself, never from an approximation.
            return CheckResult("", ok or people == 0, detail, {"person": people, "blocked": blocked})
        approximated = con.execute(
            "SELECT count(*) FROM etl_audit.quality_issue "
            "WHERE issue_type = 'DERIVED_APPROXIMATE_BIRTH_YEAR'"
        ).fetchone()[0]
        return CheckResult(
            "",
            bool(policy.age_as_of_date and policy.approval_note),
            f"approved approximation in force: {approximated:,} birth years derived and flagged",
            {"person": people, "approximated": approximated},
        )
    finally:
        con.close()


@check("OMOP_NO_FABRICATED_MEASUREMENT_DATES")
def _omop_untimed(l: Layers) -> CheckResult:
    """Untimed values stay quarantined; nothing gives them a plausible-looking date."""
    if l.quarantine is None:
        return _skip("canonical layer not built")
    untimed = l.quarantine.filter(pl.col("reason") == "UNTIMED_CLINICAL_VALUE")
    con = _omop_connection(l)
    if con is None:
        return CheckResult(
            "", True, f"{untimed.height:,} untimed values quarantined", {"untimed": untimed.height}
        )
    try:
        codes = {
            spec.code.lower()
            for source in l.cfg.sources.values()
            for spec in source.untimed_values
        }
        if not codes:
            return CheckResult("", True, "dataset declares no untimed values")
        placeholders = ", ".join("?" for _ in codes)
        leaked = con.execute(
            f"SELECT count(*) FROM measurement WHERE lower(measurement_source_value) IN ({placeholders})",
            list(codes),
        ).fetchone()[0]
        return CheckResult(
            "",
            leaked == 0,
            f"{untimed.height:,} untimed values quarantined and none published with an invented date"
            if leaked == 0
            else f"{leaked} untimed values reached MEASUREMENT with a fabricated date",
            {"untimed_quarantined": untimed.height},
        )
    finally:
        con.close()


# --------------------------------------------------------------------------------
# MEDS
# --------------------------------------------------------------------------------


def _meds_files(l: Layers) -> list[str]:
    import meds as meds_spec

    data_dir = l.layout.meds_dir / meds_spec.data_subdirectory
    return sorted(str(p) for p in data_dir.rglob("*.parquet"))


def _meds_columns(files: list[str]) -> list[str]:
    import pyarrow.parquet as pq

    return list(pq.read_schema(files[0]).names)


def _sql_list(values) -> str:
    """A quoted, lowercased SQL list. Values come from the dataset config, not a user."""
    return ", ".join("'" + str(v).lower().replace("'", "''") + "'" for v in values)


def _meds_query(files: list[str], sql: str, params: dict | None = None) -> list[tuple]:
    """Query the published shards without materializing them.

    The point of the sharding contract is that nobody ever needs the whole dataset in
    memory. A validator that needed it would be violating the property it checks.
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("PRAGMA preserve_insertion_order = false")
        # The relation API rather than a parameterized CREATE VIEW: DuckDB refuses to
        # prepare a CREATE statement, and hive partitioning stays off so a shard's
        # directory name cannot become a column.
        con.read_parquet(files, hive_partitioning=False).create_view("meds")
        return con.execute(sql, params or {}).fetchall()
    finally:
        con.close()


@check("MEDS_SCHEMA_VALID")
def _meds_schema(l: Layers) -> CheckResult:
    """The installed MEDS version decides what is valid, not this project's opinion."""
    import meds as meds_spec
    import pyarrow.parquet as pq

    files = [Path(f) for f in _meds_files(l)]
    if not files:
        return _skip("MEDS not built")
    errors: list[str] = []
    for path in files:
        try:
            meds_spec.DataSchema.validate(pq.read_table(path))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            if len(errors) > 5:
                break
    codes_path = l.layout.meds_dir / meds_spec.code_metadata_filepath
    if codes_path.exists():
        try:
            meds_spec.CodeMetadataSchema.validate(pq.read_table(codes_path))
        except Exception as exc:
            errors.append(f"codes.parquet: {exc}")
    return CheckResult(
        "",
        not errors,
        f"{len(files):,} shards validate against the installed MEDS schema"
        if not errors
        else f"schema violations: {errors[:3]}",
        {"shards": len(files)},
    )


@check("MEDS_SHARDS_CONTIGUOUS_AND_SORTED")
def _meds_sharding(l: Layers) -> CheckResult:
    """Each subject whole, in one shard, in time order.

    Checked shard by shard rather than over a concatenation: this is exactly the
    property that lets a reader stream one patient without a global index.
    """
    files = [Path(f) for f in _meds_files(l)]
    if not files:
        return _skip("MEDS not built")
    problems: list[str] = []
    owners: dict[int, str] = {}
    for path in files:
        frame = pl.read_parquet(path, columns=["subject_id", "time"])
        subjects = frame["subject_id"].to_list()
        times = frame["time"].to_list()
        blocks = [s for i, s in enumerate(subjects) if i == 0 or s != subjects[i - 1]]
        if len(blocks) != len(set(blocks)):
            problems.append(f"{path.name}: a subject's events are not contiguous")
        for subject_id in blocks:
            if subject_id in owners:
                problems.append(f"a subject spans {owners[subject_id]} and {path.name}")
            owners[subject_id] = path.name
        for i in range(1, len(subjects)):
            if subjects[i] != subjects[i - 1]:
                continue
            if times[i - 1] is not None and times[i] is not None and times[i] < times[i - 1]:
                problems.append(f"{path.name}: events are not time-sorted")
                break
        if len(problems) > 10:
            break
    return CheckResult(
        "",
        not problems,
        f"{len(owners):,} subjects, each in exactly one shard, contiguous and time-sorted"
        if not problems
        else f"sharding problems: {problems[:3]}",
        {"subjects": len(owners), "shards": len(files)},
    )


@check("MEDS_LINEAGE_COMPLETE")
def _meds_lineage(l: Layers) -> CheckResult:
    """Every published event row names the source rows behind it."""
    files = _meds_files(l)
    if not files:
        return _skip("MEDS not built")
    if "source_row_ids" not in _meds_columns(files):
        return CheckResult("", False, "source_row_ids is missing: MEDS rows are untraceable")
    total, orphans = _meds_query(
        files,
        "SELECT count(*), count(*) FILTER (WHERE source_row_ids IS NULL "
        "OR length(source_row_ids) = 0) FROM meds",
    )[0]
    return CheckResult(
        "",
        int(orphans or 0) == 0,
        f"all {int(total):,} MEDS rows trace back to at least one source row"
        if not orphans
        else f"{orphans} MEDS rows have no source row",
        {"rows": int(total)},
    )


@check("MEDS_NO_LABEL_LEAKAGE")
def _meds_leakage(l: Layers) -> CheckResult:
    """The cohort label and its proxies must not appear anywhere in event rows.

    Both halves matter: no column that carries the label, and no *value* that is the
    label smuggled in through a code or a source table name.
    """
    files = _meds_files(l)
    if not files:
        return _skip("MEDS not built")
    from ehr2cdm.meds import FORBIDDEN_EVENT_COLUMNS

    columns = _meds_columns(files)
    leaked_columns = sorted(set(columns) & FORBIDDEN_EVENT_COLUMNS)

    label_values = [p.membership_label for p in l.cfg.partitions if p.membership_label]
    labels = _sql_list(label_values)
    partitions = _sql_list(p.id for p in l.cfg.partitions)
    conditions: list[str] = []
    if labels:
        # `code` and `source_table` are namespaces this ETL builds, so a cohort label
        # appearing there is unambiguously leakage.
        for column in ("code", "source_table"):
            if column in columns:
                conditions.append(f"lower(CAST({column} AS VARCHAR)) IN ({labels})")
        # `text_value` and `source_code` are copied from the source. A cohort label of
        # "no" collides with a real ECG result of "No", and a check that cries wolf on
        # clinical text is a check people learn to ignore. Distinctive labels are still
        # worth catching there; two-letter ones are not.
        distinctive = _sql_list(v for v in label_values if len(v) >= MIN_DISTINCTIVE_LABEL)
        if distinctive:
            for column in ("source_code", "text_value"):
                if column in columns:
                    conditions.append(f"lower(CAST({column} AS VARCHAR)) IN ({distinctive})")
    if partitions and "source_table" in columns:
        conditions.append(f"lower(source_table) IN ({partitions})")

    leaked_values = 0
    if conditions:
        leaked_values = int(
            _meds_query(files, f"SELECT count(*) FROM meds WHERE {' OR '.join(conditions)}")[0][0]
        )
    ok = not leaked_columns and not leaked_values
    return CheckResult(
        "",
        ok,
        "no cohort label, partition id or file name in any event row"
        if ok
        else f"leakage: columns={leaked_columns} values={leaked_values}",
        {"shards": len(files)},
    )


@check("MEDS_CODES_METADATA_COMPLETE")
def _meds_codes(l: Layers) -> CheckResult:
    import meds as meds_spec

    files = _meds_files(l)
    codes_path = l.layout.meds_dir / meds_spec.code_metadata_filepath
    if not files or not codes_path.exists():
        return _skip("MEDS not built")
    used = {r[0] for r in _meds_query(files, "SELECT DISTINCT code FROM meds")}
    documented = set(pl.read_parquet(codes_path)["code"].to_list())
    missing, extra = used - documented, documented - used
    return CheckResult(
        "",
        not missing and not extra,
        f"codes.parquet documents exactly the {len(used):,} codes in use"
        if not missing and not extra
        else f"{len(missing)} undocumented, {len(extra)} documented but unused",
        {"codes_used": len(used)},
    )


@check("MEDS_SPLITS_DISJOINT_AND_COMPLETE")
def _meds_splits(l: Layers) -> CheckResult:
    import meds as meds_spec

    files = _meds_files(l)
    splits_path = l.layout.meds_dir / meds_spec.subject_splits_filepath
    if not files or not splits_path.exists():
        return _skip("MEDS not built")
    splits = pl.read_parquet(splits_path)
    duplicated = splits.height - splits["subject_id"].n_unique()
    subjects = {int(r[0]) for r in _meds_query(files, "SELECT DISTINCT subject_id FROM meds")}
    covered = {int(s) for s in splits["subject_id"].to_list()}
    counts = splits.group_by("split").len()
    ok = duplicated == 0 and subjects == covered
    return CheckResult(
        "",
        ok,
        f"{splits.height:,} subjects, one split each, covering every subject in the data"
        if ok
        else f"{duplicated} subjects in more than one split; {len(subjects - covered)} unassigned",
        {"by_split": dict(zip(counts["split"].to_list(), counts["len"].to_list()))},
    )


@check("MEDS_AVAILABILITY_PREVENTS_LEAKAGE")
def _meds_availability(l: Layers) -> CheckResult:
    """Availability must never precede occurrence, and must never be missing.

    An as-of view filters on ``available_time``; if a row claimed to be visible before
    it happened, that filter would admit the future.
    """
    files = _meds_files(l)
    if not files:
        return _skip("MEDS not built")
    if "available_time" not in _meds_columns(files):
        return CheckResult("", False, "available_time is missing: leakage cannot be prevented")
    timed, missing, backwards, assumed = _meds_query(
        files,
        """
        SELECT count(*) FILTER (WHERE time IS NOT NULL),
               count(*) FILTER (WHERE time IS NOT NULL AND available_time IS NULL),
               count(*) FILTER (WHERE available_time < time),
               count(*) FILTER (WHERE list_contains(quality_flags, 'AVAILABILITY_ASSUMED'))
        FROM meds
        """,
    )[0]
    ok = int(missing or 0) == 0 and int(backwards or 0) == 0
    return CheckResult(
        "",
        ok,
        f"{int(timed or 0):,} timed events all carry an availability that never precedes "
        f"occurrence ({int(assumed or 0):,} of them assumed and flagged)"
        if ok
        else f"{missing} rows have no availability and {backwards} claim to predate their own event",
        {"availability_assumed": int(assumed or 0), "timed_events": int(timed or 0)},
    )


# --------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------


@check("UNDECIDED_PROPOSALS_NEVER_PUBLISHED")
def _undecided_not_published(l: Layers) -> CheckResult:
    """A proposal nobody accepted must not have become a mapping."""
    from ehr2cdm.review import read_decisions, read_pending
    from ehr2cdm.terminology import MappingRegistry

    pending = read_pending(l.layout)
    if not pending:
        return _skip("no review queue")
    # Resolved rows are still checked -- one must not have acquired a mapping without a
    # decision either -- but only open ones are a backlog worth reporting.
    still_open = [r for r in pending if r["status"] == "open"]
    decisions = read_decisions(l.layout)
    registry = MappingRegistry.load(Path.cwd() / "mappings")
    leaked = []
    for row in pending:
        decision = decisions.get(row["id"], {}).get("decision", "").strip().lower()
        if decision == "accept":
            continue
        if registry.get(row.get("code_system", ""), row.get("source_string", "")):
            leaked.append(row["source_string"])
    return CheckResult(
        "",
        not leaked,
        f"{len(still_open):,} items pending review "
        f"({len(pending) - len(still_open):,} since resolved), none compiled into mappings"
        if not leaked
        else f"undecided items reached mappings/: {leaked[:5]}",
        {"pending": len(still_open), "resolved": len(pending) - len(still_open), "decided": len(decisions)},
    )
