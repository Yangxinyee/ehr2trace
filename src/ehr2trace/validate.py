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

The remediation checks at the end of this module come from the conversion audit of
2026-09-13 (``docs/CONVERSION_REMEDIATION_PLAN.md``, phase 0): each names the problem
ids it detects, and ``tools/audit_conversion.py`` reuses their summaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import polars as pl

from ehr2trace.analytics import HEAVY_THREADS, analytic_connection
from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import TIMELESS_BASELINE_CODES, EventKind, QualityFlag


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
        # A metric key elsewhere in this module spells the same as this column, and the
        # whitelist test cannot tell the two apart; loading one float column is cheaper
        # than teaching it to.
        "rate",
        # The normalized value and unit (canonical schema 2): absent from older builds,
        # where the loader simply leaves them out.
        "value_number_normalized", "unit_normalized",
        # Named by the merge-disagreement helper as the role a source may keep out of
        # its identity; loaded so the whitelist stays complete.
        "encounter_id",
    ),
    "anchors": ("subject_id", "anchor_date", "anchor_time", "anchor_time_known", "partition_id"),
    "cohort_membership": ("subject_id", "partition_id", "membership_label", "label_scope"),
    "quality_issue": ("issue_type", "source_id"),
    "quarantine": ("partition_id", "source_id", "reason", "person_source_id"),
}


@dataclass
class Layers:
    """Whatever has been built so far. Checks for missing layers skip, not fail."""

    cfg: DatasetConfig
    layout: WorkLayout
    events: pl.DataFrame | None
    #: The link table is not materialised. Its `event_id` column alone is 18 GB on
    #: MIMIC-IV -- 301 million forty-character hashes -- and every check that reads it
    #: joins it against an events column of the same size. Those joins happen in the
    #: query engine, over the parquet file, where they can spill; what is kept here is
    #: only what a check needs to decide whether to skip and what to report.
    links_path: Path | None
    link_count: int | None
    #: The events parquet itself, for the checks that scan it in the engine rather than
    #: holding it: the `event_id` column alone is 18 GB here.
    events_path: Path | None
    anchors: pl.DataFrame | None
    memberships: pl.DataFrame | None
    issues: pl.DataFrame | None
    quarantine: pl.DataFrame | None
    manifest: dict[str, Any] | None
    #: What one check computed and another can reuse -- the lineage join per partition
    #: and source, the events parquet's column list -- so a scan over the artifacts
    #: happens once per run rather than once per check.
    cache: dict[str, Any] = field(default_factory=dict)

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

        import pyarrow.parquet as pq

        links_path = layout.canonical_path("event_source")
        events_path = layout.canonical_path("events")
        manifest_path = layout.manifest_dir / "inputs.json"
        return cls(
            cfg=cfg,
            layout=layout,
            events=read("events"),
            links_path=links_path if links_path.exists() else None,
            link_count=pq.ParquetFile(links_path).metadata.num_rows if links_path.exists() else None,
            events_path=events_path if events_path.exists() else None,
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
    from ehr2trace.discover import resolve_source_units

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


def _lineage_counts(l: Layers) -> tuple[int, int, int]:
    """Orphaned events, dangling links, and events with more than one source row.

    One pass in the query engine rather than three joins in memory. Doing this with
    frames meant holding two columns of 300 million forty-character hashes plus a hash
    table over them, which is what put this command past 150 GB.
    """
    with analytic_connection(l.layout.root / "_validate_scratch", threads=HEAVY_THREADS) as con:
        con.execute(f"CREATE VIEW evt AS SELECT event_id FROM read_parquet('{l.events_path}')")
        con.execute(f"CREATE VIEW lnk AS SELECT event_id FROM read_parquet('{l.links_path}')")
        con.execute("CREATE OR REPLACE TEMP TABLE per_event AS "
                    "SELECT event_id, count(*) AS n FROM lnk GROUP BY event_id")
        orphans = con.execute(
            "SELECT count(*) FROM evt ANTI JOIN per_event USING (event_id)"
        ).fetchone()[0]
        dangling = con.execute(
            "SELECT count(*) FROM per_event ANTI JOIN evt USING (event_id)"
        ).fetchone()[0]
        duplicated = con.execute("SELECT count(*) FROM per_event WHERE n > 1").fetchone()[0]
    return int(orphans), int(dangling), int(duplicated)


@check("EVENT_LINEAGE_COMPLETE")
def _lineage_complete(l: Layers) -> CheckResult:
    if l.events is None or l.links_path is None:
        return _skip("canonical layer not built")
    orphans, _dangling, _dup = _lineage_counts(l)
    return CheckResult(
        "",
        not orphans,
        "every canonical event traces to at least one source row"
        if not orphans
        else f"{orphans:,} events have no source row",
        {"events": l.events.height, "links": l.link_count},
    )


@check("LINK_TARGETS_EXIST")
def _links_resolve(l: Layers) -> CheckResult:
    if l.events is None or l.links_path is None:
        return _skip("canonical layer not built")
    _orphans, dangling, _dup = _lineage_counts(l)
    return CheckResult(
        "", not dangling, "no dangling lineage links" if not dangling else f"{dangling:,} dangling links"
    )


@check("FAN_OUT_AND_DEDUP")
def _fan_out(l: Layers) -> CheckResult:
    """Reported, never asserted equal: these relations are legitimately not one-to-one."""
    if l.events is None or l.links_path is None:
        return _skip("canonical layer not built")
    _orphans, _dangling, duplicated = _lineage_counts(l)
    return CheckResult(
        "",
        True,
        f"{l.events.height:,} events from {l.link_count:,} links; "
        f"{duplicated:,} events have more than one source row",
        {
            "events": l.events.height,
            "links": l.link_count,
            "events_with_duplicates": duplicated,
            "fan_out": round(l.link_count / l.events.height, 4) if l.events.height else 0.0,
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
    if l.manifest is None or l.links_path is None:
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
    from ehr2trace.schema import (
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
    """Only a declared baseline attribute may be timeless.

    Exempting the whole ``demographic`` kind was too coarse. Vital status is written as
    a static column by both exports and was published as a timeless demographic event,
    so it passed a check whose own sentence says static attributes are the only
    timeless ones. An outcome is not a static attribute: read through an as-of view it
    answers every cutoff, including the ones before the patient died. The exemption is
    now the explicit allowlist rather than the event kind, so a new untimed attribute
    has to be added to it deliberately.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    allowed = sorted(TIMELESS_BASELINE_CODES)
    untimed = l.events.filter(
        pl.col("event_time").is_null()
        & (
            (pl.col("event_kind") != str(EventKind.demographic))
            | ~pl.col("source_code").is_in(allowed)
        )
    )
    offenders = (
        untimed.group_by("source_code").len().sort("len", descending=True).head(5)
        if untimed.height
        else None
    )
    return CheckResult(
        "",
        untimed.height == 0,
        f"every clinical event has a real time; only {len(allowed)} declared baseline "
        f"attributes are timeless"
        if untimed.height == 0
        else f"{untimed.height:,} events have no time and are not declared baseline "
        f"attributes: {dict(zip(offenders['source_code'], offenders['len']))}",
        {"untimed_not_baseline": untimed.height, "baseline_codes": allowed},
    )


@check("ORDERS_AND_ADMINISTRATIONS_STAY_SEPARATE")
def _orders_vs_admin(l: Layers) -> CheckResult:
    """Ordering a drug, handing it over and giving it are three facts, not one.

    Two of them used to be one: a dispensing cabinet's records were published as
    administrations, which asserts a treatment the source never claims happened. The
    check now covers all three stages, because the failure it is meant to catch is a
    stage collapsing into the one next to it.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    stages = {
        str(EventKind.drug_order): "order",
        str(EventKind.drug_dispense): "dispense",
        str(EventKind.drug_admin): "administration",
    }
    ids = {
        kind: l.events.filter(pl.col("event_kind") == kind).select("event_id")
        for kind in stages
    }
    counts = {stages[k]: v.height for k, v in ids.items()}
    shared = []
    for a, b in ((str(EventKind.drug_order), str(EventKind.drug_dispense)),
                 (str(EventKind.drug_order), str(EventKind.drug_admin)),
                 (str(EventKind.drug_dispense), str(EventKind.drug_admin))):
        overlap = ids[a].join(ids[b], on="event_id", how="semi").height
        if overlap:
            shared.append(f"{overlap:,} are both a {stages[a]} and a {stages[b]}")
    return CheckResult(
        "",
        not shared,
        ", ".join(f"{n:,} {name}s" for name, n in counts.items()) + "; no event is two stages"
        if not shared
        else "; ".join(shared),
        counts,
    )


@check("EVENT_KIND_IS_ONE_ITS_SOURCE_DECLARES")
def _kind_matches_source(l: Layers) -> CheckResult:
    """An event may only be a kind its own source said it could produce.

    This is the regression guard for the mislabelling that motivated the three drug
    stages. A cabinet's dispensing records were administrations for as long as one line
    of configuration said so, and nothing downstream disagreed: the events were well
    formed, they referenced real people, and they validated. What was wrong was that the
    kind did not match what the source documents itself to be.

    Comparing published kinds against the declaration turns that from a reading of the
    configuration into a property of the artifact, so a source relabelled in one place
    and rebuilt in another shows up here rather than in a training set.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    allowed: dict[str, set[str]] = {}
    for source_id, spec in l.cfg.sources.items():
        kinds: set[str] = set()
        if spec.event_kind_from is not None:
            kinds |= set(spec.event_kind_from.map.values())
            kinds.add(spec.event_kind_from.default)
        elif spec.event_kind is not None:
            kinds.add(spec.event_kind)
        # Several shapes emit companion events a source never names: a study event for a
        # report, a death for a row carrying a date. Those are the shape's contract
        # rather than the source's, so they are always admissible.
        kinds |= {str(EventKind.death), str(EventKind.demographic), str(EventKind.procedure)}
        if spec.study_event_kind is not None:
            kinds.add(spec.study_event_kind)
        allowed[source_id] = kinds

    seen = l.events.group_by("source_id", "event_kind").len()
    offenders = [
        (row["source_id"], row["event_kind"], row["len"])
        for row in seen.iter_rows(named=True)
        if row["source_id"] in allowed and row["event_kind"] not in allowed[row["source_id"]]
    ]
    unknown = sorted({row["source_id"] for row in seen.iter_rows(named=True)
                      if row["source_id"] not in allowed and row["source_id"] is not None})
    return CheckResult(
        "",
        not offenders,
        f"every event's kind is one its source declares, across {len(allowed)} sources"
        + (f"; {len(unknown)} source ids are not in the configuration: {unknown[:3]}" if unknown else "")
        if not offenders
        else "; ".join(f"{n:,} {kind} events from {sid}, which does not declare it"
                       for sid, kind, n in sorted(offenders, key=lambda o: -o[2])[:5]),
        {"sources_checked": len(allowed), "offending_pairs": len(offenders)},
    )


@check("TEXT_SOURCES_PUBLISH_THEIR_TEXT")
def _declared_text_reaches_the_output(l: Layers) -> CheckResult:
    """A source that declares a text role must publish text.

    `text` is a declared field role and, for a long time, only one shape read it. A
    source whose rows each carry a whole note could therefore map its text column,
    convert without a single complaint, and publish notes with no content: the events
    had codes, times and lineage, so every other check in this file was satisfied, and
    the OMOP exporter's `coalesce(value_text, '')` turned the absence into an empty
    string that counted as present. 2,652,887 MIMIC-IV notes were published that way,
    and the build reported success.

    Nothing else here compares a source's *declaration* against the content of what it
    produced. That is the gap the fault got through, and this is the check that closes
    it: declaring a text column is a claim that text was published, so it is verified
    against the artifact rather than trusted.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    declared = sorted(sid for sid, spec in l.cfg.sources.items() if "text" in spec.fields)
    if not declared:
        return _skip("no source declares a text role")

    published = (
        l.events.filter(pl.col("source_id").is_in(declared))
        .group_by("source_id")
        .agg(
            pl.len().alias("events"),
            (pl.col("value_text").is_not_null() & (pl.col("value_text").str.len_chars() > 0))
            .sum()
            .alias("with_text"),
        )
    )
    counts = {r["source_id"]: (r["events"], r["with_text"]) for r in published.iter_rows(named=True)}
    # A source that produced no events at all is a coverage question, reported by
    # SOURCE_COVERAGE_REPORTED. Only a source that published events without text is a
    # failure here: it declared text and its output has none.
    offenders = [(sid, counts[sid][0]) for sid in declared if counts.get(sid, (0, 0))[0] and counts[sid][1] == 0]
    total_events = sum(counts.get(sid, (0, 0))[0] for sid in declared)
    total_text = sum(counts.get(sid, (0, 0))[1] for sid in declared)
    if not total_events:
        # Every text-carrying source is absent from this build -- the MIMIC-IV
        # demonstration subset ships no note tables, for instance. Passing here would
        # report that those sources publish their text, on a build where they published
        # nothing at all, which is the shape of claim this check exists to refuse.
        return _skip(f"no events from the {len(declared)} sources that declare a text column")
    return CheckResult(
        "",
        not offenders,
        f"{len(declared)} sources declare a text column and publish it: "
        f"{total_text:,} of {total_events:,} of their events carry text"
        if not offenders
        else "; ".join(f"{sid} declares a text column and published {n:,} events with no text at all"
                       for sid, n in sorted(offenders, key=lambda o: -o[1])),
        {"sources_declaring_text": len(declared), "sources_publishing_none": len(offenders),
         "events": total_events, "events_with_text": total_text},
    )


@check("ONLY_ADMINISTRATIONS_BECOME_DRUG_EXPOSURE")
def _not_administered_excluded(l: Layers) -> CheckResult:
    """A record that is not evidence of administration must not be published as one.

    An administration record logs more than administrations: alongside refusals and
    held doses it carries line flushes, confirmations, assessments and pain
    reassessments. Those are real events and they stay in the canonical layer. What
    they are not is a claim that a patient received a drug, and DRUG_EXPOSURE is
    exactly that claim.

    Read from the published table through its lineage rather than from the query that
    fills it, because the failure this guards against is a WHERE clause lost in a later
    edit -- which a reading of the code would still describe as correct.
    """
    declared = [sid for sid, spec in l.cfg.sources.items() if spec.administered_when]
    if not declared:
        return _skip("no source declares which of its statuses mean administered")
    if l.events_path is None:
        return _skip("canonical layer not built")
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        leaked, total = con.execute(
            f"""
            WITH e AS (
                SELECT event_id FROM read_parquet('{l.events_path}')
                WHERE list_contains(quality_flags, '{QualityFlag.NOT_ADMINISTERED}')
            )
            SELECT (SELECT count(*) FROM etl_audit.lineage g
                    JOIN e ON e.event_id = g.event_id
                    WHERE g.target_table = 'drug_exposure'),
                   (SELECT count(*) FROM e)
            """
        ).fetchone()
    finally:
        con.close()
    return CheckResult(
        "",
        not leaked,
        f"{int(total):,} records are not evidence of administration, from "
        f"{len(declared)} source(s) declaring the distinction, and none became a drug "
        f"exposure"
        if not leaked
        else f"{int(leaked):,} records that are not evidence of administration were "
        f"published as drug exposure",
        {"not_administered": int(total), "in_drug_exposure": int(leaked)},
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


@check("END_TIME_NEVER_PRECEDES_START")
def _end_before_start(l: Layers) -> CheckResult:
    """An interval that ends before it starts is a source contradiction, not a duration.

    Nothing downstream is protected from it: the schema accepts it, OMOP accepts it, and
    a consumer computing a drug exposure gets a negative number with nothing to warn
    them. The converter does not repair it -- there is no safe direction to repair it in
    -- so the one thing that has to hold is that every such event says so.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    inverted = l.events.filter(
        pl.col("end_time").is_not_null()
        & pl.col("event_time").is_not_null()
        & (pl.col("end_time") < pl.col("event_time"))
    )
    unflagged = inverted.filter(
        ~pl.col("quality_flags").list.contains(str(QualityFlag.END_BEFORE_START))
    )
    return CheckResult(
        "",
        unflagged.height == 0,
        f"{inverted.height:,} events end before they start, each flagged and kept with "
        f"the times the source gave"
        if unflagged.height == 0
        else f"{unflagged.height:,} of {inverted.height:,} events that end before they "
        f"start carry no {QualityFlag.END_BEFORE_START} flag",
        {"end_before_start": inverted.height, "unflagged": unflagged.height},
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
            "visit_detail": "visit_detail_id",
            "condition_occurrence": "condition_occurrence_id",
            "drug_exposure": "drug_exposure_id",
            "procedure_occurrence": "procedure_occurrence_id",
            "measurement": "measurement_id",
            "observation": "observation_id",
            "note": "note_id",
            "death": "person_id",
        }
        missing: dict[str, int] = {}
        counts: dict[str, int] = {}
        for table, pk in tables.items():
            try:
                n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except Exception:
                continue  # a table this build's CDM release does not have
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
        from ehr2trace.terminology import Vocabulary

        import os

        vocab = Vocabulary.open(Path(os.environ["OMOP_VOCAB_DIR"]) if os.environ.get("OMOP_VOCAB_DIR") else None)
        checks = [
            ("condition_occurrence", "condition_concept_id", "Condition"),
            ("drug_exposure", "drug_concept_id", "Drug"),
            ("procedure_occurrence", "procedure_concept_id", "Procedure"),
            ("measurement", "measurement_concept_id", "Measurement"),
            ("observation", "observation_concept_id", "Observation"),
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


@check("TERMINOLOGY_COVERAGE_PLAUSIBLE")
def _terminology_coverage(l: Layers) -> CheckResult:
    """A real code system that maps almost nothing is broken, not empty.

    `concept_id = 0` is legal OMOP and means "no matching concept", so a conversion in
    which *every* diagnosis failed to map passes every structural check ever written:
    the ids exist, the domains fit, the source values survive, and the unmapped terms
    are honestly queued for review. Nothing said the queue should not have been that
    long.

    MIMIC-IV is the case in point. It writes ICD-10-CM as `F17210`; the vocabulary
    writes `F17.210`. 197 of 19,440 codes matched -- the three-character ones, which
    have no decimal point to disagree about -- and the other 99% became zero. That is a
    punctuation mismatch presented as nineteen thousand clinically unmappable diagnoses.

    A local laboratory name belongs to no standard vocabulary and legitimately maps to
    nothing, so this applies only to code systems naming a vocabulary that is installed.
    For those, a rate near zero is a lookup failure. The threshold sits far below any
    plausible real coverage: this detects catastrophe, not imperfection.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    from ehr2trace.review import read_pending

    vocabularies = _known_vocabularies(l)
    if not vocabularies:
        return _skip("no vocabulary installed to judge coverage against")
    pending = read_pending(l.layout, open_only=True)
    if not pending:
        return _skip("no review queue: nothing to compare against")

    unmapped: dict[str, int] = {}
    for row in pending:
        system = row.get("code_system") or ""
        if system in vocabularies:
            unmapped[system] = unmapped.get(system, 0) + 1

    totals = (
        l.events.filter(pl.col("code_system").is_in(sorted(vocabularies)))
        .group_by("code_system")
        .agg(pl.col("source_code").n_unique().alias("codes"))
    )
    if totals.height == 0:
        return _skip("no source code carries an installed code system")

    failures, report = [], {}
    for row in totals.iter_rows(named=True):
        system, total = row["code_system"], row["codes"]
        mapped = max(0, total - unmapped.get(system, 0))
        rate = mapped / total if total else 0.0
        report[system] = {"codes": total, "mapped": mapped, "rate": round(rate, 4)}
        if total >= MIN_CODES_TO_JUDGE and rate < MIN_PLAUSIBLE_COVERAGE:
            failures.append(f"{system}: {mapped:,}/{total:,} ({rate:.1%})")
    return CheckResult(
        "",
        not failures,
        "every installed code system maps a plausible share of its codes: "
        + ", ".join(f"{k} {v['rate']:.0%}" for k, v in sorted(report.items()))
        if not failures
        else "a standard code system mapped almost nothing, which is a lookup failure "
        f"rather than unmappable data: {failures}",
        report,
    )


#: Below this many distinct codes the rate is noise rather than evidence.
MIN_CODES_TO_JUDGE = 100
#: Far below any plausible real coverage. This detects catastrophe, not imperfection.
MIN_PLAUSIBLE_COVERAGE = 0.10


def _known_vocabularies(l: Layers) -> set[str]:
    """Code systems this dataset declares that name a vocabulary actually installed."""
    import os

    from ehr2trace.terminology import Vocabulary

    raw = os.environ.get("OMOP_VOCAB_DIR")
    if not raw:
        return set()
    vocabulary = Vocabulary.open(Path(raw))
    try:
        if not getattr(vocabulary, "available", False):
            return set()
        declared = {s.code_system for s in l.cfg.sources.values() if s.code_system}
        installed = {
            r[0] for r in vocabulary.con.execute("SELECT DISTINCT vocabulary_id FROM CONCEPT").fetchall()
        }
        return declared & installed
    finally:
        vocabulary.close()


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

        def count(sql: str) -> int | None:
            """None when the table is not in this build's CDM release."""
            try:
                return int(con.execute(sql).fetchone()[0])
            except Exception:
                return None

        for table in (
            "visit_occurrence",
            # A visit detail belongs to a person and to a visit, and the audit found a
            # publisher that could parent it to neither; both references are checked.
            "visit_detail",
            "condition_occurrence",
            "drug_exposure",
            "procedure_occurrence",
            "measurement",
            "observation",
            "note",
            "death",
            "observation_period",
        ):
            n = count(
                f"SELECT count(*) FROM {table} t "
                "WHERE NOT EXISTS (SELECT 1 FROM person p WHERE p.person_id = t.person_id)"
            )
            if n:
                dangling[f"{table}.person_id"] = n
        for table in ("visit_detail", "condition_occurrence", "drug_exposure", "procedure_occurrence",
                      "measurement", "observation", "note"):
            n = count(
                f"SELECT count(*) FROM {table} t WHERE t.visit_occurrence_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM visit_occurrence v "
                "WHERE v.visit_occurrence_id = t.visit_occurrence_id)"
            )
            if n:
                dangling[f"{table}.visit_occurrence_id"] = n
        # visit_detail.visit_occurrence_id is NOT NULL in the CDM: a detail with none
        # is a detail parented to nothing, which the clause above cannot see.
        n = count("SELECT count(*) FROM visit_detail WHERE visit_occurrence_id IS NULL")
        if n:
            dangling["visit_detail.visit_occurrence_id_null"] = n
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
            ("visit_detail", "visit_detail_id"),
            ("condition_occurrence", "condition_occurrence_id"),
            ("drug_exposure", "drug_exposure_id"),
            ("procedure_occurrence", "procedure_occurrence_id"),
            ("measurement", "measurement_id"),
            ("observation", "observation_id"),
            ("note", "note_id"),
            ("death", "person_id"),
            ("observation_period", "observation_period_id"),
        ):
            try:
                n = con.execute(f"SELECT count(*) - count(DISTINCT {pk}) FROM {table}").fetchone()[0]
            except Exception:
                continue  # a table this build's CDM release does not have
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
    from ehr2trace.meds import FORBIDDEN_EVENT_COLUMNS

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


@check("MEDS_CONCEPTS_ARE_OMOPS")
def _meds_concepts(l: Layers) -> CheckResult:
    """A term resolves to the same concept in both targets, or in neither.

    Both layers come from one canonical layer through one resolver, so the only way
    they can disagree is that one was built differently -- which is what happened when
    the MEDS stage was rerun without the vocabulary the OMOP stage had: every code
    SOURCE/, every concept null, and every check green beside an OMOP layer carrying
    29,459 concepts. OMOP keeps one row per concept a combination code asserts and MEDS
    picks one, so the test is membership, not equality; a term neither layer resolves
    is agreement too. code_system is a property of the source, not of the row, so the
    MEDS row's source and the config's declaration for it name the term.
    """
    files = _meds_files(l)
    if not files:
        return _skip("MEDS not built")
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        omop: dict[tuple[str, str], set[int]] = {}
        for system, code, concept in con.execute(
            "SELECT code_system, source_code, concept_id FROM term_map"
        ).fetchall():
            omop.setdefault((system, code), set()).add(int(concept))
    finally:
        con.close()
    system_of = {sid: spec.code_system or "SOURCE" for sid, spec in l.cfg.sources.items()}
    rows = _meds_query(
        files,
        "SELECT DISTINCT source_table, source_code, omop_concept_id FROM meds "
        "WHERE source_code IS NOT NULL",
    )
    agreeing = 0
    disagreeing: list[str] = []
    for table, code, concept in rows:
        targets = omop.get((system_of.get(table, "SOURCE"), code))
        alike = (concept is None and targets is None) or (
            concept is not None and bool(targets) and int(concept) in targets
        )
        if alike:
            agreeing += 1
        else:
            disagreeing.append(f"{table}/{code}: meds={concept} omop={sorted(targets) if targets else None}")
    mapped = sum(1 for _t, _c, concept in rows if concept is not None)
    if not disagreeing:
        detail = f"{agreeing:,} terms resolve alike in MEDS and OMOP, {mapped:,} of them to a concept"
    else:
        detail = (f"{len(disagreeing):,} of {len(rows):,} terms resolve differently in MEDS and OMOP, "
                  "e.g. " + "; ".join(disagreeing[:3]))
    return CheckResult(
        "", not disagreeing, detail,
        {"terms": len(rows), "mapped_in_meds": mapped, "agreeing": agreeing, "disagreeing": len(disagreeing)},
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
    from ehr2trace.review import read_decisions, read_pending
    from ehr2trace.terminology import MappingRegistry, mappings_directory

    pending = read_pending(l.layout)
    if not pending:
        return _skip("no review queue")
    # Resolved rows are still checked -- one must not have acquired a mapping without a
    # decision either -- but only open ones are a backlog worth reporting.
    still_open = [r for r in pending if r["status"] == "open"]
    decisions = read_decisions(l.layout)
    registry = MappingRegistry.load(mappings_directory())
    leaked = []
    for row in pending:
        decision = decisions.get(row["id"], {}).get("decision", "").strip().lower()
        if decision == "accept":
            continue
        entry = registry.get(row.get("code_system", ""), row.get("source_string", ""))
        # A curated registry row records its own reviewer and date, and that is an
        # acceptance. Demanding a second record in this work root's decisions file
        # made every mapping shipped with the repository fail on a first conversion,
        # which is bookkeeping rather than a defect. An entry nobody signed still is.
        if entry and not entry.decided_by:
            leaked.append(row["source_string"])
    return CheckResult(
        "",
        not leaked,
        f"{len(still_open):,} items pending review "
        f"({len(pending) - len(still_open):,} since resolved), none compiled into mappings"
        if not leaked
        else f"mappings/ carries entries nobody accepted: {leaked[:5]}",
        {"pending": len(still_open), "resolved": len(pending) - len(still_open), "decided": len(decisions)},
    )


# --------------------------------------------------------------------------------
# remediation checks (docs/CONVERSION_REMEDIATION_PLAN.md, phase 0, T0.2)
# --------------------------------------------------------------------------------
#
# The audit of 2026-09-13 found a class of problems the checks above could not see:
# rows collapsed into one event because the identity omitted the field they differed
# on, a source quarantined wholesale that still counted as present, units that never
# reached a concept, deaths lost to a timestamp comparison, raw tables and columns that
# nothing had ever read. Each of those is a fact about the artifacts, and each check
# below reads it from the artifacts -- the source parquet, the lineage, the published
# tables -- against what the configuration declares. Judgements (which share of a source
# may be quarantined, which columns are deliberately unread, which unit a value is really
# in) live in the dataset YAML and the reference tables under ``reference/``; nothing
# here names a dataset.
#
# The heavy ones scan the parquet files in the query engine rather than holding columns
# in memory: `READ_COLUMNS` above stays what the in-memory checks read, and the columns
# named in SQL below are read by the engine with projection, out of core.

#: A unit whose share of a code's rows is below this is a stray spelling, not a
#: second dimension. Above it, the code mixes units nothing converts between.
MIN_SECOND_UNIT_FAMILY_SHARE = 0.01

#: Visit type values that name nothing. A zero-length visit carrying one of these is a
#: row without a fact -- the transfer table's discharge markers were published that way.
PLACEHOLDER_VALUES = frozenset({"", "unknown", "none", "null", "n/a", "na", "?", "-"})

#: Spellings that say a value is a temperature, used only to *report* value ranges per
#: temperature-like code; the plausible-range table is what judges them.
TEMPERATURE_UNIT_SPELLINGS = frozenset(
    {"c", "°c", "degc", "deg c", "cel", "f", "°f", "degf", "deg f", "[degf]"}
)

#: Field roles that are part of an event's identity, so rows that collapsed to one
#: event agree on them by construction, or that legitimately differ between the rows of
#: one event (a table repeated once per extraction anchor differs in the anchor; the
#: lines of one report differ in their line number).
IDENTITY_ROLES = frozenset({"person_id", "event_time", "source_code", "value", "unit", "text", "sequence_number"})
REPEATING_ROLES = frozenset({"anchor_time", "anchor_rank", "encounter_linked", "text_line"})

#: Shapes whose events are one row each, or duplicates of one row. The grouping shapes
#: build one event from many rows that differ by design, and the person shape keys
#: every event on its own value, so neither can carry a silent merge.
MERGEABLE_SHAPES = frozenset({"point_event", "visit"})

#: Canonical field names a merge rule may be keyed on, and the role they come from.
FIELD_TO_ROLE = {f"{role}_source": role for role in ("status", "route", "dose", "rate", "unit")}
FIELD_TO_ROLE["value_text"] = "text"

DRUG_KINDS = (str(EventKind.drug_order), str(EventKind.drug_admin), str(EventKind.drug_dispense))
VISIT_KINDS = (str(EventKind.visit), str(EventKind.visit_detail))

#: The prepared-source manifest a preparation step writes beside its output. It is a
#: record of the preparation, not a delivered table, so it is never an unclaimed file.
PREPARE_MANIFEST_NAME = "prepare_manifest.json"


def _skip_with(reason: str, metrics: dict[str, Any]) -> CheckResult:
    return CheckResult("", True, f"skipped: {reason}", metrics, skipped=True)


def reference_root() -> Path:
    """The reference directory the canonical layer reads (``EHR_REFERENCE_DIR``, else ``reference/``)."""
    from ehr2trace.reference import reference_directory

    return reference_directory()


def reference_tables(l: Layers):
    """The unit, conversion and range tables this dataset's canonical layer was built with.

    Loaded through ``ehr2trace.reference``, the loader the canonical build itself uses, so
    a check can never judge a build by a table the build did not read. Remembered for
    the run, because four checks ask. A table that does not load raises, and the check
    that asked fails with the loader's reason: a unit table that contradicts itself is
    not one a build could have used either.
    """
    cached = l.cache.get("reference_tables")
    if cached is None:
        from ehr2trace.reference import load_reference

        cached = load_reference(None, l.cfg.dataset_id)
        l.cache["reference_tables"] = cached
    return cached


def conversion_pairs(tables) -> list[tuple[str, str]]:
    """The ``(from, to)`` UCUM pairs the conversion table links exactly."""
    return [(source, conversion.to_ucum) for source, conversion in tables.conversions.items()]


def _declared_unit_spellings(l: Layers) -> frozenset[str] | None:
    """What a dose's trailing word must be for the publisher to read it as the unit.

    The publisher accepts a number followed by a word as a quantity only when the word
    is a spelling the unit table lists; with no table at all, every tail counts, which
    None says.
    """
    units = reference_tables(l).units
    return units.spellings if units.by_spelling else None


#: A unit spelling longer than this, or of more words, is reported by a hash of its text
#: rather than verbatim. The audit found free text parsed into unit columns -- a report's
#: closing line is text, and can carry a reader's name -- while every real unit spelling
#: in three exports fits comfortably inside both limits.
MAX_REPORTED_SPELLING_CHARS = 20
MAX_REPORTED_SPELLING_WORDS = 2


def reportable_spelling(spelling: object) -> str:
    """A unit or status spelling as a report may carry it: verbatim when it is short and
    unit-shaped, otherwise a hash and a length."""
    import hashlib
    import re

    text = str(spelling)
    # A date or a clock time is how a report's closing line reads and never how a unit
    # does: `on 2/16/21` is ten characters and two words, and it is not a unit.
    dated = re.search(r"\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b", text)
    if (len(text) <= MAX_REPORTED_SPELLING_CHARS and len(text.split()) <= MAX_REPORTED_SPELLING_WORDS
            and "," not in text and not dated):
        return text
    return f"text#{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}({len(text)} chars)"


def _normalize_unit_spelling(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text.strip()).lower()


#: UCUM atoms a metric prefix can be stripped from, for the family heuristic below.
_UCUM_BASES = frozenset({
    "mol", "eq", "g", "l", "m", "s", "min", "h", "d", "wk", "mo", "a", "u", "iu", "[iu]",
    "hz", "pa", "cal", "j", "w", "v", "ohm", "cel", "[degf]", "k", "bq", "gy", "sv", "osm",
    "kat", "%", "bar",
})
_UCUM_PREFIXES = ("da", "y", "z", "a", "f", "p", "n", "u", "m", "c", "d", "h", "k", "g", "t")
_UCUM_ALIASES = {"eq": "mol", "iu": "u", "[iu]": "u", "hr": "h", "hrs": "h", "hour": "h", "hours": "h", "sec": "s", "day": "d", "days": "d"}


def unit_family(unit: str, units=None) -> str:
    """The dimension a unit spelling belongs to, approximately.

    The spelling is resolved to its UCUM code through the unit table where the table
    lists it, and the code (or, for a spelling nobody listed, the spelling itself) is
    reduced to its base atoms: multipliers and metric prefixes are dropped, so `mmol/L`
    and `umol/L` share a family while `mmol/L` and `mg/dL` do not, and an annotation
    stays part of the family, so a code whose rows mix `ng/mL` with `ng/mL{FEU}` is
    reported as mixing two.
    """
    import re

    spelling = _normalize_unit_spelling(unit)
    ucum = units.lookup(spelling) if units is not None else None
    key = (ucum or spelling).lower()
    parts = []
    for part in key.split("/"):
        atoms = []
        # `10*-3.eq` is a multiplier and an atom; splitting on the `*` before the sign
        # would leave `-3` behind as an atom of its own and make mEq/L a second family.
        part = re.sub(r"10[*^][+-]?\d+|x?10e[+-]?\d+", "", part.strip())
        for token in re.split(r"[.*](?!\d)", part):
            token = token.strip()
            if not token:
                continue
            if re.fullmatch(r"(10\*-?\d+|10\^-?\d+|x?10e-?\d+|\d+(\.\d+)?)", token):
                continue  # a multiplier
            token = re.sub(r"(?<=[a-z\]%])\d+$", "", token)  # an exponent
            token = _UCUM_ALIASES.get(token, token)
            if token not in _UCUM_BASES:
                for prefix in _UCUM_PREFIXES:
                    rest = token[len(prefix):]
                    if token.startswith(prefix) and rest in _UCUM_BASES:
                        token = _UCUM_ALIASES.get(rest, rest)
                        break
            atoms.append(token)
        parts.append(".".join(atoms) or "1")
    return "/".join(parts)


def unit_families(counts: dict[str, int], units, conversions: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Rows per family for one code, given its rows per unit spelling.

    Units the conversion table links are one family whatever their spelling: a value
    in one is exactly a value in the other.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for a, b in conversions:
        parent[find(unit_family(a, units))] = find(unit_family(b, units))
    out: dict[str, int] = {}
    for spelling, n in counts.items():
        root = find(unit_family(spelling, units))
        out[root] = out.get(root, 0) + n
    return out


def _unit_expression(columns: set[str]) -> str:
    """SQL for the unit an event is judged by: the normalized one where the build has
    the column, else the source spelling."""
    if "unit_normalized" in columns:
        return "coalesce(unit_normalized, unit_source)"
    return "coalesce(unit_source)"


def _events_columns(l: Layers) -> set[str]:
    """The columns the events parquet actually carries; older builds lack the newer ones."""
    if l.events_path is None:
        return set()
    cached = l.cache.get("events_columns")
    if cached is None:
        import pyarrow.parquet as pq

        cached = set(pq.read_schema(l.events_path).names)
        l.cache["events_columns"] = cached
    return cached


def _engine(l: Layers, *, omop: bool = False, meds: bool = False):
    """A spilling engine over the artifacts: ``evt`` and ``lnk`` views, optionally the
    OMOP database attached read-only as ``omop`` and the MEDS shards as ``meds``.

    Callers check that what they attach exists; the timezone is pinned to UTC so a
    date taken from a naive timestamp is the UTC date, and any local date is asked for
    explicitly with the dataset's declared zone.
    """
    from contextlib import contextmanager

    @contextmanager
    def open_engine():
        with analytic_connection(l.layout.root / "_validate_scratch", threads=HEAVY_THREADS) as con:
            con.execute("SET TimeZone='UTC'")
            if l.events_path is not None:
                con.execute(f"CREATE VIEW evt AS SELECT * FROM read_parquet('{l.events_path}')")
            if l.links_path is not None:
                con.execute(f"CREATE VIEW lnk AS SELECT * FROM read_parquet('{l.links_path}')")
            if omop:
                con.execute(f"ATTACH '{l.layout.omop_dir / 'omop.duckdb'}' AS omop (READ_ONLY)")
            if meds:
                con.read_parquet(_meds_files(l), hive_partitioning=False).create_view("meds")
            yield con

    return open_engine()


def _local_date_sql(column: str, zone: str | None) -> str:
    """SQL for the calendar date of a naive-UTC timestamp in the dataset's zone."""
    if not zone:
        return f"CAST({column} AS DATE)"
    return f"CAST(timezone('{zone}', timezone('UTC', {column})) AS DATE)"


def _omop_vocabulary_version(con, prefix: str = "") -> str | None:
    """The vocabulary the OMOP layer was built with, or None when it had none."""
    try:
        row = con.execute(f"SELECT vocabulary_version FROM {prefix}cdm_source LIMIT 1").fetchone()
    except Exception:
        return None
    if row is None or not row[0] or str(row[0]).lower() == "none":
        return None
    return str(row[0])


def _linked_rows_by_partition_source(l: Layers) -> dict[tuple[str, str], int]:
    """Lineage links per (partition, source): the join of the link table to the events'
    source ids, done once in the engine and remembered for every check that asks."""
    cached = l.cache.get("linked_by_partition_source")
    if cached is not None:
        return cached
    if l.links_path is None or l.events_path is None:
        return {}
    with _engine(l) as con:
        rows = con.execute(
            "SELECT k.partition_id, e.source_id, count(*) FROM lnk k JOIN evt e USING (event_id) "
            "GROUP BY 1, 2"
        ).fetchall()
    cached = {(str(p), str(s)): int(n) for p, s, n in rows}
    l.cache["linked_by_partition_source"] = cached
    return cached


# -- what the raw delivery holds versus what the configuration reads --------------------


def artifact_in_this_tree(layout: WorkLayout, recorded: str | None) -> Path | None:
    """The copy of a manifest-recorded artifact that *this* work tree holds.

    The ingest manifest records absolute paths, so a work tree that was copied,
    moved, or cloned for a fault-injection run carries a manifest pointing at the tree
    it came from. Reading that one would validate somebody else's artifacts and report
    the answer as this build's. The recorded path is therefore re-rooted onto this
    layout when a file of the same relative path exists under it, and used as recorded
    only when it does not.
    """
    if not recorded:
        return None
    path = Path(recorded)
    if layout.root in path.parents:
        return path if path.exists() else None
    parts = path.parts
    for i in range(1, len(parts)):  # longest suffix first
        candidate = layout.root.joinpath(*parts[i:])
        if candidate.exists():
            return candidate
    return path if path.exists() else None


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _role_columns(spec, columns: Sequence[str]) -> dict[str, list[str]]:
    """Role -> every parquet column an alias names, across the files of one source.

    The same matching ``build_role_map`` uses -- an alias names a column case-insensitively
    after stripping -- but keeping every spelling, because the files of one source can
    differ in case (a byte-order mark once made them) and a union over them holds both.
    """
    prefix = "col__"
    out: dict[str, list[str]] = {}
    for role, fs in spec.fields.items():
        matched: list[str] = []
        for alias in fs.from_:
            wanted = alias.strip().lower()
            for column in columns:
                if column.startswith(prefix) and column[len(prefix):].strip().lower() == wanted and column not in matched:
                    matched.append(column)
        if matched:
            out[role] = matched
    return out


def _normalized_cell_sql(column: str, null_literals: Sequence[str]) -> str:
    """One source cell in the form two extracts of the same record agree in.

    A workbook that types a date as text and one that types it as a date store
    different strings for one value; a length of stay is `5` in one and `5.0` in
    another. Rows that differ only in that are the same record, so a time renders as a
    time, a number as a number, and the rest compares lower-cased with its whitespace
    collapsed -- the same rules the row hash applies.
    """
    c = _sql_ident(column)
    literals = ", ".join(_sql_str(v) for v in null_literals) or "''"
    return (
        f"CASE WHEN nullif(trim({c}), '') IS NULL OR trim({c}) IN ({literals}) THEN NULL "
        f"ELSE coalesce(strftime(try_cast({c} AS TIMESTAMP), '%Y-%m-%dT%H:%M:%S'), "
        f"CAST(try_cast({c} AS DOUBLE) AS VARCHAR), "
        f"lower(regexp_replace(trim({c}), '\\s+', ' ', 'g'))) END"
    )


def merge_disagreements(cfg: DatasetConfig, manifest: dict[str, Any], links_path: Path, con,
                        per_partition: bool = False, layout: WorkLayout | None = None) -> dict[str, dict[str, Any]]:
    """Per source, how many events collapsed rows that disagree on a mapped field.

    Joins each source's own parquet (the files the ingest manifest names) to the lineage
    on the source row id, groups by event, and counts the events whose rows carry more
    than one distinct value of a field. Compared: every mapped role that is neither part
    of every build's identity nor allowed to vary, and every kept column -- including a
    dataset's identity extras, which a build predating them may have merged on. Excluded:
    roles under a declared merge rule (the disagreement is resolved by rule and flagged).
    ``available_time`` falls under the default rule and is counted apart. With ``per_partition`` the grouping also splits by the partition a row came
    from, which separates a merge inside one extract from the true duplicate between
    two.
    """
    import pyarrow.parquet as pq

    from ehr2trace.registry import FIELD_ROLES

    report: dict[str, dict[str, Any]] = {}
    files_by_source: dict[str, list[str]] = {}
    for unit in manifest.get("inputs", []):
        recorded = unit.get("output_path")
        path = artifact_in_this_tree(layout, recorded) if layout is not None else (
            Path(recorded) if recorded and Path(recorded).exists() else None)
        if path is not None and unit.get("rows_parsed", 0):
            files_by_source.setdefault(unit["source_id"], []).append(str(path))

    for source_id, spec in cfg.sources.items():
        entry: dict[str, Any] = {"shape": spec.shape, "compared": [], "skipped": None}
        report[source_id] = entry
        files = files_by_source.get(source_id, [])
        if spec.shape not in MERGEABLE_SHAPES:
            entry["skipped"] = "rows of one event differ by construction in this shape"
            continue
        if not files:
            entry["skipped"] = "no parsed rows"
            continue
        columns: list[str] = []
        for path in files:
            for name in pq.read_schema(path).names:
                if name not in columns:
                    columns.append(name)
        ruled = set()
        for key in spec.merge_rules:
            ruled.add(FIELD_TO_ROLE.get(key, key))
            ruled.add(key)
        # A dataset's identity extras and its encounter id are compared, not assumed to
        # agree: a build made with today's identity agrees on them by construction, so
        # comparing costs nothing there, and a build made before a field joined the
        # identity is exactly the build whose rows collapsed on it.
        excluded = IDENTITY_ROLES | REPEATING_ROLES | ruled
        expressions: dict[str, str] = {}
        for role, cols in _role_columns(spec, columns).items():
            if role in excluded or role not in FIELD_ROLES:
                continue
            expressions[role] = "coalesce(" + ", ".join(
                _normalized_cell_sql(c, cfg.time.null_literals) for c in cols) + ")"
        lowered = {c[len("col__"):].strip().lower(): c for c in columns if c.startswith("col__")}
        for kept in spec.keep_columns:
            column = lowered.get(kept.strip().lower())
            if column is None or kept in ruled or kept.strip().lower() in {k.lower() for k in ruled}:
                continue
            expressions[f"column:{kept}"] = _normalized_cell_sql(column, cfg.time.null_literals)
        entry["compared"] = sorted(k for k in expressions if k != "available_time")
        if not expressions:
            entry["skipped"] = "no mapped field outside the identity to compare"
            continue
        names = list(expressions)
        select = ", ".join(f"{expr} AS {_sql_ident(f'f{i}')}" for i, expr in enumerate(expressions.values()))
        distinct = ", ".join(f"count(DISTINCT {_sql_ident(f'f{i}')}) AS {_sql_ident(f'd{i}')}" for i in range(len(names)))
        flagged = ", ".join(f"count(*) FILTER (WHERE {_sql_ident(f'd{i}')} > 1) AS {_sql_ident(f'n{i}')}" for i in range(len(names)))
        file_list = "[" + ", ".join(_sql_str(f) for f in files) + "]"
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE src AS SELECT source_row_id, {select} "
            f"FROM read_parquet({file_list}, union_by_name=true)"
        )
        groupings = [("all", "")]
        if per_partition and len(cfg.partitions) > 1:
            groupings.append(("within_partition", ", k.partition_id"))
        for label, extra in groupings:
            row = con.execute(
                f"""
                WITH g AS (
                    SELECT k.event_id{extra}, count(*) AS n_rows, {distinct}
                    FROM read_parquet('{links_path}') k JOIN src s USING (source_row_id)
                    GROUP BY k.event_id{extra}
                )
                SELECT count(*), count(*) FILTER (WHERE n_rows > 1), {flagged} FROM g
                """
            ).fetchone()
            counts = {names[i]: int(row[2 + i] or 0) for i in range(len(names))}
            availability = counts.pop("available_time", 0)
            block = {
                "events": int(row[0] or 0),
                "merged_events": int(row[1] or 0),
                "disagreements": {k: v for k, v in sorted(counts.items()) if v},
                "availability_disagreements": availability,
            }
            if label == "all":
                entry.update(block)
            else:
                entry[label] = block
        con.execute("DROP TABLE IF EXISTS src")
    return report


def raw_coverage(cfg: DatasetConfig, manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Every delivered column and file, against what the configuration reads or declares.

    A column is covered when a role alias names it, a filter or kind-switch reads it, an
    untimed value or provenance flag comes from it, a merge rule or ``keep_columns``
    names it, it is the identity key, or ``ignored_columns`` says why it is left alone.
    A file (or a workbook sheet) is covered when a source resolves to it, or an
    ``out_of_scope`` entry matches its name. An ``unread_inputs`` list in a preparation
    manifest is held to the same declaration.
    """
    import fnmatch
    import json

    from ehr2trace.adapters import list_sheets
    from ehr2trace.discover import IGNORED_NAMES, resolve_source_units
    from ehr2trace.errors import ConfigError

    out: dict[str, Any] = {"columns": {}, "files": {}, "prepare_manifest": {}}

    # (a) columns
    identity_keys = {cfg.identity.person_key.strip().lower()}
    if cfg.identity.encounter_key:
        identity_keys.add(cfg.identity.encounter_key.strip().lower())
    columns_by_source: dict[str, set[str]] = {}
    for unit in (manifest or {}).get("inputs", []):
        columns_by_source.setdefault(unit["source_id"], set()).update(unit.get("columns", []))
    for source_id, columns in sorted(columns_by_source.items()):
        spec = cfg.sources.get(source_id)
        if spec is None:
            out["columns"][source_id] = {"undeclared": sorted(columns), "note": "source not in configuration"}
            continue
        covered = {a.strip().lower() for fs in spec.fields.values() for a in fs.from_}
        covered |= {c.strip().lower() for c in spec.keep_columns}
        covered |= {rf.column.strip().lower() for rf in spec.row_filters}
        if spec.event_kind_from is not None:
            covered.add(spec.event_kind_from.column.strip().lower())
        covered |= {u.column.strip().lower() for u in spec.untimed_values}
        covered |= {c.strip().lower() for c in spec.flag_when.values()}
        covered |= {k.strip().lower() for k in spec.merge_rules}
        if spec.study_code_from:
            covered.add(spec.study_code_from.strip().lower())
        covered |= identity_keys
        ignored = {c.strip().lower() for c in spec.ignored_columns}
        undeclared = sorted(c for c in columns if c.strip().lower() not in covered and c.strip().lower() not in ignored)
        out["columns"][source_id] = {
            "columns": len(columns),
            "mapped_or_used": sum(1 for c in columns if c.strip().lower() in covered),
            "ignored_with_reason": sum(1 for c in columns if c.strip().lower() in ignored and c.strip().lower() not in covered),
            "undeclared": undeclared,
        }

    # (b) files and sheets under every partition directory of every source root
    patterns = [m for entry in cfg.out_of_scope.values() for m in entry.matches]

    def declared(*names: str) -> bool:
        return any(fnmatch.fnmatch(n, p) for n in names for p in patterns)

    unclaimed: list[str] = []
    examined: list[str] = []
    claimed_count = 0
    # Every root that resolves is walked; a root whose variable is unset or wrong skips
    # only the files under it, and says so. One prepared root missing from an
    # environment once meant no file of the whole delivery was examined.
    roots: list[Path] = []
    not_examined: dict[str, str] = {}
    try:
        roots.append(cfg.data_root())
    except ConfigError as exc:
        not_examined[cfg.root_env] = str(exc)
    for spec in cfg.sources.values():
        if spec.root_env is None or spec.root_env in not_examined:
            continue
        try:
            root = cfg.source_root(spec)
        except ConfigError as exc:
            not_examined[spec.root_env] = str(exc)
            continue
        if root not in roots:
            roots.append(root)
    if not_examined:
        out["files"]["not_examined"] = not_examined
    for root in roots:
        for part in cfg.partitions:
            pdir = root / part.dir
            if not pdir.is_dir():
                continue
            examined.append(f"{part.id}")
            claimed: dict[Path, set[str | None]] = {}
            for source_id, spec in cfg.sources_for(part.id).items():
                try:
                    units = resolve_source_units(cfg, part.id, source_id, spec)
                except ConfigError:
                    continue
                for unit in units:
                    claimed.setdefault(Path(unit.path).resolve(), set()).add(unit.sheet)
            for path in sorted(p for p in pdir.rglob("*") if p.is_file()):
                if path.name in IGNORED_NAMES or path.name.startswith("~$") or path.name == PREPARE_MANIFEST_NAME:
                    continue
                relative = str(path.relative_to(root))
                sheets = claimed.get(path.resolve())
                if sheets is None:
                    if declared(relative, path.name):
                        claimed_count += 1
                    else:
                        unclaimed.append(relative)
                    continue
                claimed_count += 1
                if None in sheets:
                    continue
                try:
                    every_sheet = list_sheets(path)
                except Exception:
                    every_sheet = []
                for sheet in every_sheet:
                    if sheet in sheets:
                        continue
                    if declared(sheet, f"{relative}#{sheet}", f"{path.name}#{sheet}"):
                        continue
                    unclaimed.append(f"{relative}#{sheet}")
    out["files"].update({"partitions_examined": examined, "claimed_or_declared": claimed_count, "unclaimed": unclaimed})

    # (c) inputs a preparation step recorded as unread
    undeclared_unread: list[str] = []
    seen_manifests: list[str] = []
    candidates: list[Path] = []
    for root in roots:
        candidates.append(root / PREPARE_MANIFEST_NAME)
        for part in cfg.partitions:
            candidates.append(root / part.dir / PREPARE_MANIFEST_NAME)
    for candidate in candidates:
        if not candidate.exists() or str(candidate) in seen_manifests:
            continue
        seen_manifests.append(str(candidate))
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("unread_inputs", []) or []:
            name = str(item.get("path", "")) if isinstance(item, dict) else str(item)
            if name and not declared(name, Path(name).name):
                undeclared_unread.append(name)
    out["prepare_manifest"] = {"manifests": len(seen_manifests), "undeclared_unread_inputs": undeclared_unread}
    return out


# -- aggregate summaries shared with tools/audit_conversion.py ----------------------------


def note_duplicate_groups(con, zone: str | None) -> dict[str, int]:
    """Notes repeated within one subject: same local date, code and text (exact), and
    the same text on another date (near). Texts are hashed, never carried."""
    local = _local_date_sql("event_time", zone)
    row = con.execute(
        f"""
        WITH n AS (
            SELECT subject_id, source_code, hash(value_text) AS h, {local} AS d, count(*) AS c
            FROM evt WHERE event_kind = '{EventKind.note}' AND value_text IS NOT NULL
            GROUP BY 1, 2, 3, 4
        )
        SELECT count(*) FILTER (WHERE c > 1),
               coalesce(sum(c - 1) FILTER (WHERE c > 1), 0),
               (SELECT count(*) FROM (SELECT subject_id, source_code, h FROM n GROUP BY 1, 2, 3 HAVING count(*) > 1)),
               coalesce(sum(c), 0)
        FROM n
        """
    ).fetchone()
    return {
        "exact_duplicate_groups": int(row[0] or 0),
        "surplus_note_events": int(row[1] or 0),
        "same_text_other_date_groups": int(row[2] or 0),
        "notes_with_text": int(row[3] or 0),
    }


def encounter_reference(cfg: DatasetConfig) -> str:
    """What an encounter id is resolved against in this dataset: its visits, or its other sources.

    A dataset whose visits or visit details are keyed by encounter -- some visit-shaped
    source maps an ``encounter_id`` -- promises that an event's encounter names one of
    them. A dataset that delivered no such table cannot keep that promise: there an
    encounter id is only ever shared between the tables that carry it, and the rate that
    means anything is how often another source knows the same (patient, encounter).
    """
    for spec in cfg.sources.values():
        if (spec.shape == "visit" or spec.event_kind in VISIT_KINDS) and "encounter_id" in spec.fields:
            return "visits"
    return "sources"


def encounter_link_rates(con, against: str = "visits") -> dict[str, dict[str, Any]]:
    """Per source, the share of events with an encounter id that resolve.

    ``against="visits"``: to a visit or visit detail of the same subject carrying that
    encounter id. ``against="sources"``: to the same (subject, encounter) carried by the
    events of at least one other source.
    """
    kinds = ", ".join(_sql_str(k) for k in VISIT_KINDS)
    if against == "visits":
        sql = f"""
        WITH v AS (
            SELECT DISTINCT subject_id, encounter_id FROM evt
            WHERE event_kind IN ({kinds}) AND encounter_id IS NOT NULL
        )
        SELECT e.source_id, count(*), count(*) FILTER (WHERE v.encounter_id IS NOT NULL)
        FROM evt e LEFT JOIN v ON v.subject_id = e.subject_id AND v.encounter_id = e.encounter_id
        WHERE e.encounter_id IS NOT NULL AND e.event_kind NOT IN ({kinds})
        GROUP BY 1
        """
    else:
        sql = f"""
        WITH pairs AS (
            SELECT DISTINCT source_id, subject_id, encounter_id FROM evt
            WHERE encounter_id IS NOT NULL AND event_kind NOT IN ({kinds})
        ), shared AS (
            SELECT subject_id, encounter_id FROM pairs GROUP BY 1, 2 HAVING count(*) > 1
        )
        SELECT e.source_id, count(*), count(*) FILTER (WHERE s.encounter_id IS NOT NULL)
        FROM evt e LEFT JOIN shared s ON s.subject_id = e.subject_id AND s.encounter_id = e.encounter_id
        WHERE e.encounter_id IS NOT NULL AND e.event_kind NOT IN ({kinds})
        GROUP BY 1
        """
    return {
        str(s): {"with_encounter": int(n), "linked": int(k), "rate": round(int(k) / int(n), 4) if n else 0.0}
        for s, n, k in con.execute(sql).fetchall()
    }


def unit_spellings_per_code(con, columns: set[str], top: int = 50) -> list[dict[str, Any]]:
    """The unit spellings each of the most frequent measured codes carries."""
    kinds = ", ".join(_sql_str(k) for k in (str(EventKind.measurement), str(EventKind.observation)))
    unit = _unit_expression(columns)
    rows = con.execute(
        f"""
        WITH u AS (
            SELECT code_system, source_code, regexp_replace(trim({unit}), '\\s+', ' ', 'g') AS spelling, count(*) AS n
            FROM evt WHERE event_kind IN ({kinds}) AND {unit} IS NOT NULL
            GROUP BY 1, 2, 3
        ), top_codes AS (
            SELECT code_system, source_code, sum(n) AS rows FROM u GROUP BY 1, 2 ORDER BY rows DESC, 1, 2 LIMIT {int(top)}
        )
        SELECT u.code_system, u.source_code, t.rows, u.spelling, u.n
        FROM u JOIN top_codes t USING (code_system, source_code)
        ORDER BY t.rows DESC, u.code_system, u.source_code, u.n DESC
        """
    ).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for system, code, total, spelling, n in rows:
        entry = out.setdefault((system, code), {"code_system": system, "source_code": code, "rows": int(total), "units": {}})
        entry["units"][reportable_spelling(spelling)] = int(n)
    return list(out.values())


def temperature_like_summary(con, columns: set[str]) -> list[dict[str, Any]]:
    """Value ranges of the codes whose unit spells a temperature: reported, not judged."""
    spellings = ", ".join(_sql_str(s) for s in sorted(TEMPERATURE_UNIT_SPELLINGS))
    value = "coalesce(value_number_normalized, value_number)" if "value_number_normalized" in columns else "value_number"
    rows = con.execute(
        f"""
        SELECT code_system, source_code, lower(regexp_replace(trim(unit_source), '\\s+', ' ', 'g')) AS u,
               count(*), min({value}), quantile_cont({value}, 0.01), median({value}),
               quantile_cont({value}, 0.99), max({value})
        FROM evt
        WHERE unit_source IS NOT NULL AND {value} IS NOT NULL
          AND (lower(trim(unit_source)) IN ({spellings})
               OR lower(unit_source) LIKE '%celsius%' OR lower(unit_source) LIKE '%fahrenheit%')
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """
    ).fetchall()
    return [
        {"code_system": s, "source_code": c, "unit": reportable_spelling(u), "rows": int(n),
         "min": lo, "p01": p1, "median": med, "p99": p99, "max": hi}
        for s, c, u, n, lo, p1, med, p99, hi in rows
    ]


def death_local_dates(con, zone: str | None) -> dict[str, int]:
    """Subjects with a death event, and how many of them put it on more than one local date."""
    local = _local_date_sql("event_time", zone)
    row = con.execute(
        f"""
        WITH d AS (
            SELECT subject_id, count(DISTINCT {local}) AS dates, count(*) AS events
            FROM evt WHERE event_kind = '{EventKind.death}' AND event_time IS NOT NULL GROUP BY 1
        )
        SELECT count(*), count(*) FILTER (WHERE dates > 1), count(*) FILTER (WHERE events > 1) FROM d
        """
    ).fetchone()
    return {
        "subjects_with_death_event": int(row[0] or 0),
        "subjects_with_conflicting_local_dates": int(row[1] or 0),
        "subjects_with_several_death_events": int(row[2] or 0),
    }


# -- the checks ---------------------------------------------------------------------------


@check("DUPLICATES_AGREE", slow=True)
def _duplicates_agree(l: Layers) -> CheckResult:
    """Rows that collapsed into one event agree on every mapped field, or a rule said how.

    From the audit (P-C1, P-C2; P-CU1, P-CU6, P-CU7, P-J1, P-J2, P-J10, P-M1, P-M6,
    P-M19): the identity omitted dose, route, status and end time, so orders that
    differed in them became one event, and the merge kept whichever value it met first
    -- 726,710 dose disagreements in one export, 45,348 status disagreements in another.

    Reads each source's parquet (the files the ingest manifest names), the lineage
    table, the configuration's roles, merge rules and kept columns, and the quality
    issues. Fails on any disagreement outside a declared rule, or on any MERGE_CONFLICT
    issue the build itself recorded. Skips without a manifest, lineage or events. Slow:
    every source is joined to the whole lineage table.
    """
    if l.manifest is None or l.links_path is None or l.events_path is None:
        return _skip("canonical layer not built")
    with _engine(l) as con:
        report = merge_disagreements(l.cfg, l.manifest, l.links_path, con, layout=l.layout)
    disagreeing = {
        sid: entry["disagreements"]
        for sid, entry in report.items() if entry.get("disagreements")
    }
    conflicts = 0
    if l.issues is not None and "issue_type" in l.issues.columns:
        conflicts = int(l.issues.filter(pl.col("issue_type") == str(QualityFlag.MERGE_CONFLICT)).height)
    compared = sum(1 for e in report.values() if not e.get("skipped"))
    merged = sum(int(e.get("merged_events", 0)) for e in report.values())
    availability = sum(int(e.get("availability_disagreements", 0)) for e in report.values())
    ok = not disagreeing and not conflicts
    return CheckResult(
        "",
        ok,
        f"{merged:,} events merged more than one source row across {compared} sources; every "
        f"mapped field agrees or falls under a declared rule ({availability:,} availability "
        f"disagreements resolved by the default rule)"
        if ok
        else "rows merged into one event disagree on fields nobody declared a rule for: "
        + "; ".join(f"{sid}: " + ", ".join(f"{k}={v:,}" for k, v in sorted(d.items())) for sid, d in sorted(disagreeing.items())[:5])
        + (f"; {conflicts:,} MERGE_CONFLICT issues recorded" if conflicts else ""),
        {"per_source": report, "merge_conflict_issues": conflicts, "sources_disagreeing": sorted(disagreeing)},
    )


@check("SOURCE_YIELDS_EVENTS")
def _source_yields_events(l: Layers) -> CheckResult:
    """A source with parsed rows produces events, unless it is declared expected empty.

    From the audit (P-C11b, P-M2): a wide table declared with the wrong shape had every
    one of its 1,564,610 rows quarantined and still counted as present, because presence
    was judged by the manifest and events by nothing.

    Reads the ingest manifest and the lineage joined to events, per partition and source.
    Fails for any (partition, source) with parsed rows and no linked row whose source
    does not declare ``expected_empty``. Skips without a manifest or lineage.
    """
    if l.manifest is None or l.links_path is None or l.events_path is None:
        return _skip("canonical layer not built")
    parsed: dict[tuple[str, str], int] = {}
    for unit in l.manifest["inputs"]:
        key = (unit["partition_id"], unit["source_id"])
        parsed[key] = parsed.get(key, 0) + int(unit["rows_parsed"])
    linked = _linked_rows_by_partition_source(l)
    silent, declared, stale = [], [], []
    for (partition, source), rows in sorted(parsed.items()):
        if not rows:
            continue
        spec = l.cfg.sources.get(source)
        n = linked.get((partition, source), 0)
        if n:
            if spec is not None and spec.expected_empty:
                stale.append(f"{partition}/{source}")
            continue
        if spec is not None and spec.expected_empty:
            declared.append(f"{partition}/{source}")
        else:
            silent.append(f"{partition}/{source} ({rows:,} rows parsed)")
    return CheckResult(
        "",
        not silent,
        f"every source with parsed rows yields events ({len(declared)} declared empty on purpose)"
        + (f"; declared empty but yielding events: {stale[:3]}" if stale else "")
        if not silent
        else f"{len(silent)} source(s) parsed rows and produced no event: {silent[:5]}",
        {"silent": silent, "declared_empty": declared, "declared_empty_but_yielding": stale,
         "linked_rows": {f"{p}/{s}": n for (p, s), n in sorted(linked.items())}},
    )


@check("QUARANTINE_SHARE_DECLARED")
def _quarantine_share_declared(l: Layers) -> CheckResult:
    """A source quarantined beyond the threshold for one reason says so in its YAML.

    From the audit (P-C11c, P-M3, P-M21, W1, W2): any share of quarantine counted as
    explained as long as each row had a reason -- 2,849,786 triage values with no time,
    534,610 orders with no date, a body-mass index with no measurement time -- and
    nothing distinguished a known property of the export from a converter defect.

    Reads the canonical quarantine (distinct source rows per source and reason), the
    ingest-stage quarantine files the manifest names, and the manifest's row counts.
    Fails when a (source, reason) share exceeds ``validation.quarantine_share_threshold``
    and the source's ``expected_quarantine`` does not declare that reason with a
    ``max_share`` at or above it. Reports every share. Skips without a manifest.
    """
    if l.manifest is None:
        return _skip("no ingest manifest")
    parsed: dict[str, int] = {}
    read: dict[str, int] = {}
    ingest_files: dict[str, list[str]] = {}
    for unit in l.manifest["inputs"]:
        sid = unit["source_id"]
        parsed[sid] = parsed.get(sid, 0) + int(unit["rows_parsed"])
        read[sid] = read.get(sid, 0) + int(unit["rows_read"])
        q = artifact_in_this_tree(l.layout, unit.get("quarantine_path"))
        if q is not None:
            ingest_files.setdefault(sid, []).append(str(q))
    counts: dict[tuple[str, str], tuple[int, int]] = {}  # (source, reason) -> (rows, denominator)
    quarantine_path = l.layout.canonical_path("quarantine")
    with _engine(l) as con:
        if quarantine_path.exists():
            for sid, reason, n in con.execute(
                f"SELECT source_id, reason, count(DISTINCT source_row_id) FROM read_parquet('{quarantine_path}') "
                "GROUP BY 1, 2"
            ).fetchall():
                counts[(str(sid), str(reason))] = (int(n), parsed.get(str(sid), 0))
        for sid, files in ingest_files.items():
            file_list = "[" + ", ".join(_sql_str(f) for f in files) + "]"
            for reason, n in con.execute(
                f"SELECT reason, count(*) FROM read_parquet({file_list}) GROUP BY 1"
            ).fetchall():
                key = (sid, str(reason))
                previous = counts.get(key, (0, read.get(sid, 0)))[0]
                counts[key] = (previous + int(n), read.get(sid, 0))
    threshold = l.cfg.validation.quarantine_share_threshold
    shares: dict[str, dict[str, float]] = {}
    undeclared: list[str] = []
    for (sid, reason), (n, denominator) in sorted(counts.items()):
        share = round(n / denominator, 4) if denominator else 0.0
        shares.setdefault(sid, {})[reason] = share
        if share <= threshold:
            continue
        spec = l.cfg.sources.get(sid)
        expected = spec.expected_quarantine.get(reason) if spec is not None else None
        if expected is None or expected.max_share < share:
            undeclared.append(f"{sid}/{reason} {share:.1%}" + (f" (declared up to {expected.max_share:.0%})" if expected else ""))
    return CheckResult(
        "",
        not undeclared,
        f"{len(counts)} (source, reason) quarantine shares, none above {threshold:.0%} without a declaration"
        if not undeclared
        else f"quarantine shares above {threshold:.0%} that no YAML declares: {undeclared[:6]}",
        {"threshold": threshold, "shares": shares, "undeclared": undeclared},
    )


@check("DEATH_PUBLISHED")
def _death_published(l: Layers) -> CheckResult:
    """Every subject with a death event has one published death, unless the dates conflict.

    From the audit (P-C7, P-C11d, P-M5): the death publisher compared timestamps, so a
    date-only death beside a timed one on the same day counted as a conflict; 11,402
    subjects lost their DEATH row and MEDS carried two death events for each, while the
    conflict was only ever written as a quality issue.

    Reads the death events (local dates in the dataset's declared zone, the
    DEATH_DATE_CONFLICT flag), the OMOP lineage of the death and person tables, and the
    MEDS death rows. A subject whose deaths fall on one local date must have a DEATH row
    when published to PERSON, and exactly one MEDS death row; a subject on several dates
    or flagged is exempt and reported. Skips without death events, and when neither OMOP
    nor MEDS is built.
    """
    if l.events is None:
        return _skip("canonical layer not built")
    deaths = l.events.filter(
        (pl.col("event_kind") == str(EventKind.death)) & pl.col("event_time").is_not_null()
    )
    if deaths.height == 0:
        return _skip("no death events in this dataset")
    zone = l.cfg.time.timezone_assumption or "UTC"
    per_subject = (
        deaths.with_columns(
            pl.col("event_time").dt.replace_time_zone("UTC").dt.convert_time_zone(zone).dt.date().alias("local_day"),
            pl.col("quality_flags").list.contains(str(QualityFlag.DEATH_DATE_CONFLICT)).alias("flagged"),
        )
        .group_by("subject_id")
        .agg(pl.col("local_day").n_unique().alias("days"), pl.col("flagged").any().alias("flagged"))
    )
    conflicting = set(per_subject.filter((pl.col("days") > 1) | pl.col("flagged"))["subject_id"].to_list())
    expected = set(per_subject["subject_id"].to_list()) - conflicting
    subject_of_event = dict(zip(deaths["event_id"].to_list(), deaths["subject_id"].to_list()))

    metrics: dict[str, Any] = {
        "subjects_with_death": per_subject.height, "conflicting_subjects": len(conflicting),
    }
    problems: list[str] = []
    examined = 0
    con = _omop_connection(l)
    if con is not None:
        try:
            death_events = {r[0] for r in con.execute(
                "SELECT DISTINCT event_id FROM etl_audit.lineage WHERE target_table = 'death'").fetchall()}
            person_events = {r[0] for r in con.execute(
                "SELECT DISTINCT event_id FROM etl_audit.lineage WHERE target_table = 'person'").fetchall()}
            death_rows = int(con.execute("SELECT count(*) FROM death").fetchone()[0])
        finally:
            con.close()
        with_row = {subject_of_event[e] for e in death_events if e in subject_of_event}
        published = set(l.events.filter(pl.col("event_id").is_in(list(person_events)))["subject_id"].to_list()) if person_events else set()
        missing = (expected & published) - with_row
        metrics.update({"omop_death_rows": death_rows, "omop_subjects_missing_death": len(missing)})
        examined += 1
        if missing:
            problems.append(f"{len(missing):,} published persons with a death on one local date have no DEATH row")
    files = _meds_files(l)
    if files:
        import meds as meds_spec

        rows = _meds_query(files, "SELECT subject_id, count(*) FROM meds WHERE code = $code GROUP BY subject_id",
                           {"code": meds_spec.death_code})
        per_meds = {int(s): int(n) for s, n in rows}
        wrong = {s for s in expected if per_meds.get(s, 0) != 1}
        metrics.update({"meds_death_rows": sum(per_meds.values()), "meds_subjects_not_exactly_one": len(wrong)})
        examined += 1
        if wrong:
            problems.append(f"{len(wrong):,} subjects have {'no' if all(per_meds.get(s, 0) == 0 for s in wrong) else 'not exactly one'} MEDS death row")
    if not examined:
        return _skip_with("neither OMOP nor MEDS is built", metrics)
    return CheckResult(
        "",
        not problems,
        f"{len(expected):,} subjects with a death on one local date ({zone}) are published as dead "
        f"in every built target; {len(conflicting):,} carry a genuine date conflict and are reported"
        if not problems
        else "; ".join(problems),
        metrics,
    )


@check("UNIT_CONCEPT_COVERAGE")
def _unit_concept_coverage(l: Layers) -> CheckResult:
    """Measurements that state a unit carry a unit concept.

    From the audit (P-C3, P-C5, P-J7, P-J9, P-M17): ``unit_concept_id`` was the literal
    0 in the publisher, so 161,725,013 rows in one export and every row in the others
    carried a unit string and no concept; values the source states without a unit had
    no way to be given one.

    Reads OMOP MEASUREMENT (rows with a unit source value) and, for events flagged
    UNIT_DECLARED, their published rows through the lineage. The share with a non-zero
    concept must reach ``validation.unit_concept_coverage_min``. Skips when OMOP is not
    built, was built without a vocabulary, or no measurement carries a unit.
    """
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")
    try:
        if _omop_vocabulary_version(con) is None:
            return _skip("OMOP was built without a vocabulary: no unit concept could be assigned")
        total, covered = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE unit_concept_id <> 0) FROM measurement "
            "WHERE unit_source_value IS NOT NULL"
        ).fetchone()
    finally:
        con.close()
    total, covered = int(total or 0), int(covered or 0)
    declared_total = declared_covered = 0
    if l.events is not None:
        declared = l.events.filter(
            pl.col("quality_flags").list.contains(str(QualityFlag.UNIT_DECLARED))
        ).select("event_id")
        if declared.height:
            with _engine(l, omop=True) as engine:
                engine.register("declared", declared.to_arrow())
                row = engine.execute(
                    """
                    SELECT count(DISTINCT m.measurement_id),
                           count(DISTINCT m.measurement_id) FILTER (WHERE m.unit_concept_id <> 0)
                    FROM declared d
                    JOIN omop.etl_audit.lineage g ON g.event_id = d.event_id AND g.target_table = 'measurement'
                    JOIN omop.main.measurement m ON m.measurement_id = g.target_pk
                    WHERE m.unit_source_value IS NULL
                    """
                ).fetchone()
            declared_total, declared_covered = int(row[0] or 0), int(row[1] or 0)
    denominator = total + declared_total
    if not denominator:
        return _skip("no published measurement states a unit")
    share = (covered + declared_covered) / denominator
    minimum = l.cfg.validation.unit_concept_coverage_min
    return CheckResult(
        "",
        share >= minimum,
        f"{covered + declared_covered:,} of {denominator:,} measurements with a stated or declared unit "
        f"carry a unit concept ({share:.1%}, threshold {minimum:.0%})",
        {"with_unit": denominator, "with_concept": covered + declared_covered, "share": round(share, 4),
         "declared_units": declared_total, "threshold": minimum},
    )


@check("UNIT_KNOWN")
def _unit_known(l: Layers) -> CheckResult:
    """Every unit spelling on an event is one a table declares, or the event says it is not.

    From the audit (P-C8, P-J8): the value parser read any text after a number as a
    unit, so an electrocardiogram diagnosis became a number with a diagnosis for its
    unit. Once the parser accepts only spellings the table declares, a spelling nobody
    has declared carries UNIT_UNKNOWN on its event and nothing is normalized from it. A
    spelling the table declares *not* to be a unit -- a row with an empty ``ucum`` -- has
    been read by somebody, so it is not unknown, and is reported apart.

    Reads the distinct unit spellings of the events with their UNIT_UNKNOWN flags, and
    the unit table through ``ehr2trace.reference``. Fails on any undeclared spelling on
    an unflagged event; reports the most frequent undeclared spellings (verbatim only
    when unit-shaped, see ``reportable_spelling``). Skips when the reference directory
    holds no unit table or no event carries a unit.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    units = reference_tables(l).units
    if not units.by_spelling and not units.not_units:
        return _skip(f"no unit table under {reference_root() / 'units'}")
    with _engine(l) as con:
        rows = con.execute(
            f"""
            SELECT trim(unit_source) AS u, count(*),
                   count(*) FILTER (WHERE NOT list_contains(quality_flags, '{QualityFlag.UNIT_UNKNOWN}'))
            FROM evt WHERE unit_source IS NOT NULL GROUP BY 1
            """
        ).fetchall()
    if not rows:
        return _skip("no event carries a unit")
    undeclared = sorted(
        ((int(unflagged), int(n), str(u)) for u, n, unflagged in rows if not units.declared(u)),
        reverse=True,
    )
    not_a_unit = sum(int(n) for u, n, _ in rows if units.declared(u) and not units.known(u))
    unflagged_total = sum(f for f, _, _ in undeclared)
    events = sum(int(n) for _, n, _ in rows)
    top = {reportable_spelling(s): n for _, n, s in sorted(undeclared, key=lambda t: -t[1])[:10]}
    top_unflagged = {reportable_spelling(s): f for f, _, s in undeclared[:10] if f}
    return CheckResult(
        "",
        unflagged_total == 0,
        f"{len(rows):,} distinct unit spellings on {events:,} events; {len(undeclared):,} spellings no "
        "table declares, and every event carrying one says so"
        + (f"; {not_a_unit:,} events carry a spelling declared not to be a unit" if not_a_unit else "")
        if unflagged_total == 0
        else f"{unflagged_total:,} events carry a unit spelling no table declares, without UNIT_UNKNOWN; "
        f"most frequent: {top_unflagged}",
        {"distinct_spellings": len(rows), "undeclared_spellings": len(undeclared),
         "undeclared_unflagged_events": unflagged_total, "declared_not_a_unit_events": not_a_unit,
         "top_undeclared": top, "unit_table": str(reference_root() / "units")},
    )


@check("UNIT_VALUE_PLAUSIBLE")
def _unit_value_plausible(l: Layers) -> CheckResult:
    """A value outside the plausible range declared for its code and unit is flagged.

    From the audit (P-C4, P-CU2, P-CU9, P-CU10, P-M2, D-R17): body temperatures labelled
    Celsius with a median of 98, a respiratory rate of 196, blood pressures of zero,
    drop-down option numbers read as measurements -- all published as values. The
    decision is to keep the value, flag it IMPLAUSIBLE, and leave the normalized column
    empty; the ranges live in ``reference/plausible_ranges/<dataset>.csv``.

    Reads the events' code, unit, value and flags against the dataset's range table,
    through ``ehr2trace.reference``. A build that normalized its units is judged on the
    normalized unit and value. A build that predates normalization is judged on its
    source spelling resolved through the unit table and converted by the conversion
    table, so a temperature written in Fahrenheit is held to the Celsius range it would
    have been normalized to, and one written as Celsius is held to that range whatever
    its values say. A value with no unit meets a range declared with an empty unit.
    Fails on any value outside its range without the IMPLAUSIBLE flag. Always reports
    the value ranges of temperature-like codes. Skips without a range table.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    columns = _events_columns(l)
    tables = reference_tables(l)
    normalized = "unit_normalized" in columns and "value_number_normalized" in columns
    with _engine(l) as con:
        temperatures = temperature_like_summary(con, columns)
        if not tables.ranges:
            return _skip_with(
                f"no plausible-range table for {l.cfg.dataset_id} under {reference_root() / 'plausible_ranges'}; "
                f"value ranges of {len(temperatures)} temperature-like codes reported",
                {"temperature_like": temperatures},
            )
        con.execute("CREATE TEMP TABLE rng (code_system VARCHAR, source_code VARCHAR, ucum VARCHAR, low DOUBLE, high DOUBLE)")
        con.executemany("INSERT INTO rng VALUES (?, ?, ?, ?, ?)",
                        [(s, c, u or "", r.low, r.high) for (s, c, u), r in tables.ranges.items()])
        con.execute("CREATE TEMP TABLE umap (spelling VARCHAR, ucum VARCHAR)")
        if tables.units.by_spelling:
            con.executemany("INSERT INTO umap VALUES (?, ?)", list(tables.units.by_spelling.items()))
        con.execute("CREATE TEMP TABLE conv (from_ucum VARCHAR, to_ucum VARCHAR, factor DOUBLE, shift DOUBLE)")
        if tables.conversions:
            con.executemany("INSERT INTO conv VALUES (?, ?, ?, ?)",
                            [(k, c.to_ucum, float(c.factor), float(c.offset)) for k, c in tables.conversions.items()])
        rows = con.execute(
            f"""
            WITH spelled AS (
                SELECT code_system, source_code, quality_flags, value_number,
                       {"value_number_normalized" if normalized else "CAST(NULL AS DOUBLE)"} AS vn,
                       lower(trim(unit_source)) AS u,
                       {"unit_normalized" if normalized else "CAST(NULL AS VARCHAR)"} AS un
                FROM evt
                WHERE value_number IS NOT NULL{" OR value_number_normalized IS NOT NULL" if normalized else ""}
            ), resolved AS (
                SELECT s.code_system, s.source_code, s.quality_flags,
                       CASE WHEN s.un IS NOT NULL THEN coalesce(s.vn, s.value_number)
                            WHEN c.from_ucum IS NOT NULL THEN s.value_number * c.factor + c.shift
                            ELSE s.value_number END AS v,
                       CASE WHEN s.un IS NOT NULL THEN s.un
                            WHEN s.u IS NULL OR s.u = '' THEN ''
                            ELSE coalesce(c.to_ucum, m.ucum) END AS ucum
                FROM spelled s
                LEFT JOIN umap m ON m.spelling = s.u
                LEFT JOIN conv c ON c.from_ucum = m.ucum AND s.un IS NULL
            )
            SELECT r.code_system, r.source_code, r.ucum, count(*),
                   count(*) FILTER (WHERE e.v < r.low OR e.v > r.high),
                   count(*) FILTER (WHERE (e.v < r.low OR e.v > r.high)
                                      AND NOT list_contains(e.quality_flags, '{QualityFlag.IMPLAUSIBLE}'))
            FROM resolved e JOIN rng r ON r.code_system = e.code_system AND r.source_code = e.source_code AND r.ucum = e.ucum
            WHERE e.v IS NOT NULL
            GROUP BY 1, 2, 3 ORDER BY 6 DESC, 5 DESC, 4 DESC
            """
        ).fetchall()
    judged = [{"code_system": s, "source_code": c, "unit": u, "rows": int(n), "outside": int(o), "unflagged": int(f)}
              for s, c, u, n, o, f in rows]
    unflagged = sum(j["unflagged"] for j in judged)
    outside = sum(j["outside"] for j in judged)
    return CheckResult(
        "",
        unflagged == 0,
        f"{sum(j['rows'] for j in judged):,} values judged against {len(tables.ranges)} declared ranges "
        f"({'normalized' if normalized else 'source'} units); {outside:,} lie outside and every one is flagged"
        if unflagged == 0
        else f"{unflagged:,} values lie outside their declared plausible range and are not flagged: "
        + "; ".join(f"{j['source_code']} [{j['unit']}] {j['unflagged']:,}" for j in judged[:5] if j["unflagged"]),
        {"ranges_declared": len(tables.ranges), "judged_on": "normalized" if normalized else "source",
         "judged": judged[:20], "outside": outside, "unflagged": unflagged, "temperature_like": temperatures},
    )


@check("UNIT_HOMOGENEOUS_PER_CODE")
def _unit_homogeneous_per_code(l: Layers) -> CheckResult:
    """One code does not mix units that no exact conversion links.

    From the audit (P-C4, P-M9, D-R10): one D-dimer item carried both `ng/mL` and
    `ng/mL FEU`, quantities no factor converts between, under one code; the decision is
    to split such a code by its unit rather than convert.

    Reads rows per (code, unit) over the measured events -- the normalized unit where the
    build has one, else the source spelling -- with the unit and conversion tables
    through ``ehr2trace.reference``. Reports every code whose units fall in more than one
    family; fails when a code's second family exceeds MIN_SECOND_UNIT_FAMILY_SHARE of its
    rows. Skips when no measured event carries a unit.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    columns = _events_columns(l)
    tables = reference_tables(l)
    conversions = conversion_pairs(tables)
    kinds = ", ".join(_sql_str(k) for k in (str(EventKind.measurement), str(EventKind.observation)))
    unit = _unit_expression(columns)
    with _engine(l) as con:
        rows = con.execute(
            f"""
            SELECT code_system, source_code, regexp_replace(trim({unit}), '\\s+', ' ', 'g'), count(*)
            FROM evt WHERE event_kind IN ({kinds}) AND {unit} IS NOT NULL GROUP BY 1, 2, 3
            """
        ).fetchall()
    if not rows:
        return _skip("no measured event carries a unit")
    per_code: dict[tuple[str, str], dict[str, int]] = {}
    for system, code, spelling, n in rows:
        per_code.setdefault((str(system), str(code)), {})[str(spelling)] = int(n)
    mixed: list[dict[str, Any]] = []
    for (system, code), spellings in per_code.items():
        families = unit_families(spellings, tables.units, conversions)
        if len(families) < 2:
            continue
        total = sum(families.values())
        ordered = sorted(families.items(), key=lambda kv: -kv[1])
        second_share = ordered[1][1] / total if total else 0.0
        mixed.append({
            "code_system": system, "source_code": code, "rows": total,
            "families": {reportable_spelling(f): n for f, n in ordered[:6]},
            "second_family_share": round(second_share, 4),
            "fails": second_share > MIN_SECOND_UNIT_FAMILY_SHARE,
        })
    mixed.sort(key=lambda m: (-m["fails"], -m["second_family_share"], -m["rows"]))
    failing = [m for m in mixed if m["fails"]]
    return CheckResult(
        "",
        not failing,
        f"{len(per_code):,} measured codes carry a unit; {len(mixed)} mix unit families and none beyond "
        f"{MIN_SECOND_UNIT_FAMILY_SHARE:.0%} of its rows"
        if not failing
        else f"{len(failing)} code(s) mix units no conversion links: "
        + "; ".join(f"{m['source_code']} {list(m['families'])[:2]} ({m['second_family_share']:.1%})" for m in failing[:5]),
        {"codes_with_units": len(per_code), "mixed": mixed[:20], "failing": len(failing),
         "unit_table": bool(tables.units.by_spelling), "conversions": len(conversions)},
    )


def source_unit_statements(l: Layers) -> dict[str, dict[str, int]]:
    """Per drug source that maps a unit role: source rows stating a unit, events carrying one.

    Reads the source parquet the manifest names (this tree's copy), counting rows whose
    unit cell is neither empty nor a null literal, and the canonical drug events of the
    same source that carry ``unit_source``. A source whose rows state units while none of
    its events carries one lost the unit before the canonical layer -- the mapping never
    read the column -- and no check reading only the canonical layer can see that.
    """
    import pyarrow.parquet as pq

    if l.manifest is None or l.events_path is None:
        return {}
    drug_sources = {sid: spec for sid, spec in l.cfg.sources.items()
                    if "unit" in spec.fields and spec.event_kind in DRUG_KINDS}
    if not drug_sources:
        return {}
    files_by_source: dict[str, list[str]] = {}
    for unit in l.manifest.get("inputs", []):
        path = artifact_in_this_tree(l.layout, unit.get("output_path"))
        if path is not None and unit.get("rows_parsed") and unit["source_id"] in drug_sources:
            files_by_source.setdefault(unit["source_id"], []).append(str(path))
    literals = ", ".join(_sql_str(v) for v in l.cfg.time.null_literals) or "''"
    kinds = ", ".join(_sql_str(k) for k in DRUG_KINDS)
    out: dict[str, dict[str, int]] = {}
    with _engine(l) as con:
        carried = {str(s): int(n) for s, n in con.execute(
            f"SELECT source_id, count(*) FILTER (WHERE unit_source IS NOT NULL) FROM evt "
            f"WHERE event_kind IN ({kinds}) GROUP BY 1"
        ).fetchall()}
        for sid, files in sorted(files_by_source.items()):
            columns: list[str] = []
            for path in files:
                for name in pq.read_schema(path).names:
                    if name not in columns:
                        columns.append(name)
            unit_columns = _role_columns(drug_sources[sid], columns).get("unit")
            if not unit_columns:
                continue
            stated = " OR ".join(
                f"(nullif(trim(CAST({_sql_ident(c)} AS VARCHAR)), '') IS NOT NULL "
                f"AND trim(CAST({_sql_ident(c)} AS VARCHAR)) NOT IN ({literals}))"
                for c in unit_columns
            )
            file_list = "[" + ", ".join(_sql_str(f) for f in files) + "]"
            rows = con.execute(
                f"SELECT count(*) FILTER (WHERE {stated}) FROM read_parquet({file_list}, union_by_name=true)"
            ).fetchone()[0]
            out[sid] = {"source_rows_stating_a_unit": int(rows or 0), "events_carrying_a_unit": carried.get(sid, 0)}
    return out


@check("DOSE_UNIT_CARRIED")
def _dose_unit_carried(l: Layers) -> CheckResult:
    """A drug record whose source states a dose unit publishes it in OMOP and in MEDS.

    From the audit (P-C6, P-CU3, P-M10): the publisher took the dose unit only from the
    dose text, so an export that keeps the unit in its own column published 18,567,232
    drug exposures with no unit, and another whose unit column was never mapped left
    1,897,802 bare numbers in MEDS.

    Reads the drug events' unit and dose text (each distinct dose parsed once, with the
    parser the publisher uses), OMOP DRUG_EXPOSURE through the lineage, and the MEDS drug
    rows by event id. Fails when such a row has a null OMOP dose unit, or a MEDS row with
    neither a unit nor its dose text. Reports bare doses (no unit anywhere).

    Also reads each drug source's own parquet for the unit its rows state (see
    ``source_unit_statements``), and fails when a source states units on its rows while
    none of its events carries one: the unit was lost before the canonical layer, which
    is how the unmapped unit column of the audit looks from inside the build. Skips when
    no drug event or source row states a unit or a parseable dose unit, or nothing is
    published.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    from ehr2trace.canonical.values import parse_value
    from ehr2trace.errors import QuarantineRow

    kinds = ", ".join(_sql_str(k) for k in DRUG_KINDS)
    omop_built = (l.layout.omop_dir / "omop.duckdb").exists()
    meds_files = _meds_files(l)
    if not omop_built and not meds_files:
        return _skip("neither OMOP nor MEDS is built")
    statements = source_unit_statements(l)
    lost = {sid: s for sid, s in statements.items() if s["source_rows_stating_a_unit"] and not s["events_carrying_a_unit"]}
    with _engine(l, omop=omop_built, meds=bool(meds_files)) as con:
        doses = [r[0] for r in con.execute(
            f"SELECT DISTINCT dose_source FROM evt WHERE event_kind IN ({kinds}) AND dose_source IS NOT NULL"
        ).fetchall()]
        known = _declared_unit_spellings(l)
        with_unit = []
        for dose in doses:
            try:
                parsed = parse_value(dose)
            except QuarantineRow:
                continue
            # The publisher's rule: a number followed by a word is a quantity with a unit
            # only when the word is a spelling the unit table lists.
            if parsed.unit and (known is None or parsed.unit.strip().lower() in known):
                with_unit.append((dose,))
        con.execute("CREATE TEMP TABLE dosed (dose_source VARCHAR)")
        if with_unit:
            con.executemany("INSERT INTO dosed VALUES (?)", with_unit)
        con.execute(
            f"""
            CREATE TEMP TABLE carried AS
            SELECT event_id, unit_source IS NOT NULL AS has_unit
            FROM evt WHERE event_kind IN ({kinds})
              AND (unit_source IS NOT NULL OR dose_source IN (SELECT dose_source FROM dosed))
            """
        )
        candidates = int(con.execute("SELECT count(*) FROM carried").fetchone()[0])
        metrics: dict[str, Any] = {"drug_events_with_source_unit": candidates, "parseable_dose_texts": len(with_unit)}
        metrics["source_unit_statements"] = statements
        if not candidates and not lost:
            return _skip_with("no drug event states a unit or a parseable dose unit", metrics)
        problems: list[str] = [
            f"{sid} states a dose unit on {s['source_rows_stating_a_unit']:,} source rows and none of its events carries one"
            for sid, s in sorted(lost.items())
        ]
        if omop_built:
            row = con.execute(
                """
                SELECT count(DISTINCT d.drug_exposure_id),
                       count(DISTINCT d.drug_exposure_id) FILTER (WHERE d.dose_unit_source_value IS NULL)
                FROM omop.main.drug_exposure d
                JOIN omop.etl_audit.lineage g ON g.target_table = 'drug_exposure' AND g.target_pk = d.drug_exposure_id
                JOIN carried c ON c.event_id = g.event_id
                """
            ).fetchone()
            metrics.update({"omop_rows": int(row[0] or 0), "omop_missing_unit": int(row[1] or 0)})
            if row[1]:
                problems.append(f"{int(row[1]):,} of {int(row[0]):,} drug exposures whose source states a unit have none in OMOP")
        if meds_files:
            row = con.execute(
                f"""
                SELECT count(*), count(*) FILTER (WHERE m.unit IS NULL AND m.dose IS NULL),
                       (SELECT count(*) FROM meds WHERE event_kind IN ({kinds}) AND dose IS NOT NULL AND unit IS NULL
                        AND dose NOT IN (SELECT dose_source FROM dosed))
                FROM meds m JOIN carried c ON c.event_id = m.event_id
                WHERE m.event_kind IN ({kinds})
                """
            ).fetchone()
            metrics.update({"meds_rows": int(row[0] or 0), "meds_missing_unit": int(row[1] or 0), "meds_bare_doses": int(row[2] or 0)})
            if row[1]:
                problems.append(f"{int(row[1]):,} of {int(row[0]):,} MEDS drug rows whose source states a unit carry neither a unit nor their dose text")
    return CheckResult(
        "",
        not problems,
        f"{candidates:,} drug events state a unit or a parseable dose unit and every published row carries it"
        + (f"; {metrics.get('meds_bare_doses', 0):,} MEDS doses have no unit anywhere (reported)" if metrics.get("meds_bare_doses") else "")
        if not problems
        else "; ".join(problems),
        metrics,
    )


@check("VISIT_CONCEPT_COVERAGE")
def _visit_concept_coverage(l: Layers) -> CheckResult:
    """Published visits carry a visit concept, and no zero-length visit names nothing.

    From the audit (P-C12, P-J5, P-M7, P-M8, P-M20, D-R8, D-R14): 34% of one export's
    visits and 86% of another's had concept 0 -- unmapped visit types, transfers and
    service changes published as visits, and 546,024 discharge markers with no unit and
    no end time published as zero-length visits.

    VISIT_OCCURRENCE is judged against ``validation.visit_concept_coverage_min``.
    VISIT_DETAIL is judged against its own ``validation.visit_detail_concept_coverage_min``:
    a detail names a place of care rather than a type of visit, places are mapped by
    review one department at a time, and an export whose ward names are still being
    reviewed has a queue, not a converter fault. Both are judged only when the build had
    a vocabulary, and a dataset that lowers either says why in ``validation.note``.

    A visit detail that no visit of its person carries or contains cannot be published
    at all: CDM 5.4 declares ``visit_detail.visit_occurrence_id`` NOT NULL. The publisher
    withholds it with a VISIT_DETAIL_UNPARENTED issue and the canonical layer and MEDS
    keep it, so the gap between canonical details and published rows is reported with
    that reason and never counted as loss -- on one export most ICU transfers name an
    encounter that was never delivered as a visit.

    Zero-length visits whose type is empty or a placeholder are counted and reported,
    and fail nothing. A readmission the source dated and never typed is a visit that
    source recorded, and saying "the source did not say" with concept 0 is a decision a
    dataset may make; the discharge markers the audit found published as visits are
    caught by the coverage threshold instead, which they drag far below any default.

    Reads OMOP VISIT_OCCURRENCE, VISIT_DETAIL, the publisher's lineage and quality
    issues, and the canonical visit and visit-detail events. Skips when OMOP is not
    built.
    """
    con = _omop_connection(l)
    if con is None:
        return _skip("OMOP not built or empty")

    def scalar(sql: str) -> int | None:
        try:
            return int(con.execute(sql).fetchone()[0])
        except Exception:
            return None  # a table or column this build's publisher did not write

    try:
        has_vocabulary = _omop_vocabulary_version(con) is not None
        coverage: dict[str, dict[str, Any]] = {}
        for table, column in (("visit_occurrence", "visit_concept_id"), ("visit_detail", "visit_detail_concept_id")):
            try:
                total, covered = con.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE {column} <> 0) FROM {table}"
                ).fetchone()
            except Exception:
                continue
            if not total:
                continue
            coverage[table] = {"rows": int(total), "with_concept": int(covered), "share": round(int(covered) / int(total), 4)}
        zero_unmapped = scalar(
            "SELECT count(*) FROM visit_occurrence WHERE visit_start_datetime = visit_end_datetime AND visit_concept_id = 0"
        ) or 0
        unparented = scalar(
            "SELECT count(DISTINCT event_id) FROM etl_audit.quality_issue WHERE issue_type = 'VISIT_DETAIL_UNPARENTED'"
        )
        published_details = scalar(
            "SELECT count(DISTINCT event_id) FROM etl_audit.lineage WHERE target_table = 'visit_detail'"
        )
    finally:
        con.close()
    placeholders = 0
    canonical_details = None
    if l.events is not None:
        visits = l.events.filter(pl.col("event_kind") == str(EventKind.visit))
        placeholders = int(visits.filter(
            (pl.col("end_time").is_null() | (pl.col("end_time") == pl.col("event_time")))
            & (pl.col("source_code").is_null()
               | pl.col("source_code").str.strip_chars().str.to_lowercase().is_in(sorted(PLACEHOLDER_VALUES)))
        ).height)
        canonical_details = int(l.events.filter(
            (pl.col("event_kind") == str(EventKind.visit_detail)) & pl.col("event_time").is_not_null()
        ).height)
    thresholds = {
        "visit_occurrence": l.cfg.validation.visit_concept_coverage_min,
        "visit_detail": l.cfg.validation.visit_detail_concept_coverage_min,
    }
    problems: list[str] = []
    for table, c in coverage.items():
        c["threshold"] = thresholds[table]
        if has_vocabulary and c["share"] < thresholds[table]:
            problems.append(f"{table}: {c['with_concept']:,} of {c['rows']:,} rows carry a concept "
                            f"({c['share']:.1%} < {thresholds[table]:.0%})")
    withheld = {
        "canonical_visit_details": canonical_details,
        "published_visit_details": published_details,
        "unparented_issues": unparented,
        "reason": "CDM 5.4 declares visit_detail.visit_occurrence_id NOT NULL; a detail no visit of its person "
                  "carries or contains is withheld from OMOP and kept in the canonical layer and MEDS",
    }
    metrics = {"coverage": coverage, "thresholds": thresholds, "judged": has_vocabulary, "note": l.cfg.validation.note,
               "zero_length_unmapped_visits": zero_unmapped, "placeholder_zero_length_visits": placeholders,
               "visit_detail_withheld": withheld}
    if not coverage:
        return _skip_with("no visit published", metrics)
    reported = f"; {unparented:,} visit details withheld as unparented (reported, not a loss)" if unparented else ""
    if placeholders:
        reported += f"; {placeholders:,} zero-length visits carry no type (reported)"
    return CheckResult(
        "",
        not problems,
        ", ".join(f"{t} {c['share']:.1%} of {c['rows']:,} rows carry a concept (threshold {c['threshold']:.0%})"
                  for t, c in coverage.items())
        + ("" if has_vocabulary else " (built without a vocabulary, so not judged)")
        + f"; {zero_unmapped:,} zero-length visits without a concept" + reported
        if not problems
        else "; ".join(problems) + reported,
        metrics,
    )


@check("NOTE_TEXT_UNIQUE")
def _note_text_unique(l: Layers) -> CheckResult:
    """One subject does not carry the same note text twice on one day under one code.

    From the audit (P-CU4, W6, D-R2): 491,192 groups of notes identical in patient, day,
    type and full text differed only in an encounter id that matched no other table, and
    each became its own event; another export repeats 8,237 report texts on other dates,
    which is reported and left alone.

    Reads the note events' subject, local date (dataset zone), code and a hash of the
    text -- the text itself is never held. Fails on any exact group of more than one;
    reports groups that share text across dates. Skips without note events.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    with _engine(l) as con:
        summary = note_duplicate_groups(con, l.cfg.time.timezone_assumption)
    if not summary["notes_with_text"]:
        return _skip("no note carries text")
    exact = summary["exact_duplicate_groups"]
    return CheckResult(
        "",
        exact == 0,
        f"{summary['notes_with_text']:,} notes with text; no subject carries one text twice on a day "
        f"({summary['same_text_other_date_groups']:,} texts recur on another date, reported)"
        if exact == 0
        else f"{exact:,} groups of notes share subject, day, code and full text; {summary['surplus_note_events']:,} "
        "events are copies that differ only in what the identity still keys on",
        summary,
    )


@check("ENCOUNTER_RESOLVES")
def _encounter_resolves(l: Layers) -> CheckResult:
    """An event's encounter id names an encounter the dataset knows, at the declared rate.

    From the audit (P-CU12, P-M14, P-M15): a note table whose encounter ids matched
    other tables 2.6% of the time; emergency visits keyed on one identifier while their
    vital signs used another and their medications none, so nothing linked.

    Where a visit-shaped source maps an encounter id, an event's encounter must name a
    visit or visit detail of the same subject, and each source's linked share must reach
    its ``expected_encounter_link_rate.min_rate`` or else
    ``validation.encounter_link_rate_min``. Where no visit source maps one -- no encounter
    table was delivered -- an encounter id can only be shared between the tables that
    carry it: the share of each source's events whose (subject, encounter) another source
    also carries is reported, and judged only where the source declares the rate it
    expects, because the default was set for resolving against visits.

    Reads the events' source, subject, kind and encounter id, and the configuration.
    Skips when no event carries an encounter id, or when visits are the reference and
    none was built.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    against = encounter_reference(l.cfg)
    if against == "visits" and l.events is not None and l.events.filter(pl.col("event_kind").is_in(list(VISIT_KINDS))).height == 0:
        return _skip("no visit events to resolve encounters against")
    with _engine(l) as con:
        rates = encounter_link_rates(con, against)
    if not rates:
        return _skip("no event carries an encounter id")
    default = l.cfg.validation.encounter_link_rate_min
    failing: list[str] = []
    judged = 0
    for sid, r in sorted(rates.items()):
        spec = l.cfg.sources.get(sid)
        expected = spec.expected_encounter_link_rate if spec is not None else None
        r["declared"] = expected is not None
        if expected is None and against == "sources":
            r["min_rate"] = None
            continue
        minimum = expected.min_rate if expected is not None else default
        r["min_rate"] = minimum
        judged += 1
        if r["rate"] < minimum:
            failing.append(f"{sid} {r['rate']:.1%} < {minimum:.1%}")
    where = ("a visit of the same subject" if against == "visits"
             else "another source's events, since no visit carries an encounter id")
    return CheckResult(
        "",
        not failing,
        f"{len(rates)} sources carry encounter ids, resolved against {where}; {judged} judged and none "
        "falls short: " + ", ".join(f"{sid} {r['rate']:.1%}" for sid, r in sorted(rates.items()))
        if not failing
        else f"encounter ids that do not resolve to {where}: {failing[:6]}",
        {"resolved_against": against, "per_source": rates, "judged": judged, "default_min_rate": default},
    )


@check("CODE_DESCRIPTION_IS_REPRESENTATIVE")
def _code_description_representative(l: Layers) -> CheckResult:
    """codes.parquet describes each code by its concept name, or by its commonest source name.

    From the audit (P-C9): the description was the alphabetically smallest source string,
    so a concept carrying 127,700 rows of one name and 15 of another was described by
    the other.

    Reads MEDS codes.parquet, the events' (source, code, name) frequencies, the MEDS
    ``dataset.json``, and the vocabulary when one is installed. An OMOP code's description
    must be its concept name (accepted as any non-empty text without a vocabulary); a
    SOURCE code's description must be the most frequent source name of its events. The
    SOURCE rule is enforced on a MEDS layer written under extension column version 2 and
    reported on an older one, whose publisher did not yet promise it. Skips when MEDS is
    not built.
    """
    import json

    import meds as meds_spec

    from ehr2trace.terminology import normalize_term

    files = _meds_files(l)
    codes_path = l.layout.meds_dir / meds_spec.code_metadata_filepath
    if not files or not codes_path.exists() or l.events_path is None:
        return _skip("MEDS not built")
    metadata_path = l.layout.meds_dir / meds_spec.dataset_metadata_filepath
    version = 1
    if metadata_path.exists():
        try:
            version = int(json.loads(metadata_path.read_text(encoding="utf-8")).get("extension_columns_version", 1))
        except (ValueError, TypeError):
            version = 1
    if version < 2 and "unit_normalized" in _meds_columns(files):
        version = 2
    codes = pl.read_parquet(codes_path)
    with _engine(l) as con:
        rows = con.execute(
            "SELECT source_id, source_code, source_name, count(*) FROM evt "
            "WHERE source_code IS NOT NULL GROUP BY 1, 2, 3"
        ).fetchall()
    from collections import Counter

    names: dict[tuple[str, str], Counter] = {}
    for source_id, code, name, n in rows:
        key = (str(source_id), normalize_term(code) or "unspecified")
        counter = names.setdefault(key, Counter())
        if name is not None and str(name).strip():
            counter[str(name).strip()] += int(n)

    def fold(text: str | None) -> str:
        return " ".join(str(text or "").split()).lower()

    concept_names: dict[int, str] = {}
    omop_ids = [int(c) for c in codes["omop_concept_id"].drop_nulls().to_list()] if "omop_concept_id" in codes.columns else []
    vocabulary_used = False
    if omop_ids:
        import os

        from ehr2trace.terminology import Vocabulary

        vocabulary = Vocabulary.open(Path(os.environ["OMOP_VOCAB_DIR"]) if os.environ.get("OMOP_VOCAB_DIR") else None)
        try:
            if getattr(vocabulary, "available", False):
                vocabulary_used = True
                vocabulary.con.execute("CREATE OR REPLACE TEMP TABLE _ids (concept_id BIGINT)")
                vocabulary.con.executemany("INSERT INTO _ids VALUES (?)", [(i,) for i in set(omop_ids)])
                for concept_id, name in vocabulary.con.execute(
                    "SELECT i.concept_id, c.concept_name FROM _ids i JOIN CONCEPT c ON CAST(c.concept_id AS BIGINT) = i.concept_id"
                ).fetchall():
                    concept_names[int(concept_id)] = str(name)
                vocabulary.con.execute("DROP TABLE IF EXISTS _ids")
        finally:
            vocabulary.close()

    wrong_omop: list[str] = []
    wrong_source: list[str] = []
    examined = 0
    for row in codes.iter_rows(named=True):
        code, description = str(row["code"]), row.get("description")
        if code == meds_spec.death_code:
            continue
        examined += 1
        if code.startswith("OMOP/"):
            concept_id = row.get("omop_concept_id")
            expected = concept_names.get(int(concept_id)) if concept_id is not None else None
            if expected is not None:
                if fold(description) != fold(expected):
                    wrong_omop.append(f"{code}: described as {str(description)[:40]!r}, concept name {expected[:40]!r}")
            elif not fold(description):
                wrong_omop.append(f"{code}: empty description")
            continue
        parts = code.split("/", 2)
        if len(parts) != 3:
            continue
        counter = names.get((parts[1], parts[2]))
        if not counter:
            if not fold(description):
                wrong_source.append(f"{code}: empty description")
            continue
        commonest = counter.most_common(1)[0][0]
        if fold(description) != fold(commonest):
            wrong_source.append(f"{code}: described as {str(description)[:40]!r}, commonest name {commonest[:40]!r}")
    enforced_source = version >= 2
    failures = wrong_omop + (wrong_source if enforced_source else [])
    return CheckResult(
        "",
        not failures,
        f"{examined:,} codes described by their concept name or commonest source name"
        + ("" if vocabulary_used or not omop_ids else " (no vocabulary installed: OMOP descriptions accepted as given)")
        + (f"; {len(wrong_source):,} SOURCE codes described by something other than their commonest name, "
           "reported only because this MEDS layer predates the rule" if wrong_source and not enforced_source else "")
        if not failures
        else f"{len(failures):,} of {examined:,} code descriptions are not representative, e.g. {failures[:3]}",
        {"codes": examined, "wrong_omop": len(wrong_omop), "wrong_source": len(wrong_source),
         "source_rule_enforced": enforced_source, "vocabulary_used": vocabulary_used, "examples": failures[:10]},
    )


@check("RAW_COVERAGE_DECLARED")
def _raw_coverage_declared(l: Layers) -> CheckResult:
    """Every delivered column, file and sheet is read, kept, or declared unread with a reason.

    From the audit (P-C10, P-C13, P-CU5, P-CU8, P-J4, P-J6, P-J12, P-M4, P-M11, P-M12,
    P-M13, P-M18, W7): a whole imaging table, three medication columns, a directory of
    follow-up and intensive-care tables, an entire ICU module and eight other tables
    were never read, and no check said so.

    Reads the manifest's column lists per source against the configuration (roles,
    filters, kept columns, untimed values, flags, merge rules, identity keys and
    ``ignored_columns``), every file and workbook sheet under the partition directories
    of every source root against the sources' resolved units and ``out_of_scope``, and
    any preparation manifest's ``unread_inputs``. Fails on anything undeclared; reports
    counts per source. Skips without a manifest; the file walk is skipped, with its
    reason, when a root is not reachable.
    """
    if l.manifest is None:
        return _skip("no ingest manifest")
    report = raw_coverage(l.cfg, l.manifest)
    undeclared_columns = {sid: c["undeclared"] for sid, c in report["columns"].items() if c["undeclared"]}
    unclaimed = report["files"].get("unclaimed", [])
    unread = report["prepare_manifest"].get("undeclared_unread_inputs", [])
    problems: list[str] = []
    if undeclared_columns:
        problems.append(
            f"{sum(len(v) for v in undeclared_columns.values())} columns neither read nor declared ignored: "
            + "; ".join(f"{sid}: {cols[:4]}" for sid, cols in sorted(undeclared_columns.items())[:5])
        )
    if unclaimed:
        problems.append(f"{len(unclaimed)} delivered files or sheets no source reads and no out_of_scope entry names: {unclaimed[:4]}")
    if unread:
        problems.append(f"{len(unread)} inputs a preparation step left unread without an out_of_scope entry: {unread[:4]}")
    columns_total = sum(c.get("columns", 0) for c in report["columns"].values())
    not_examined = report["files"].get("not_examined")
    return CheckResult(
        "",
        not problems,
        f"{columns_total} delivered columns across {len(report['columns'])} sources are read, kept or declared; "
        f"{report['files'].get('claimed_or_declared', 0)} files claimed or declared"
        + (f" (files not examined: {not_examined})" if not_examined else "")
        if not problems
        else "; ".join(problems),
        report,
    )


@check("EXCLUDED_STATUS_NOT_PUBLISHED")
def _excluded_status_not_published(l: Layers) -> CheckResult:
    """No published event carries a status its source declared excluded.

    From the audit (P-J11, D-R6): 30,069 problem-list rows whose status says the entry
    was deleted were published as diagnoses.

    Reads the events' status per source against each source's ``excluded_status``
    (case-insensitive, stripped). Fails on any match. When no source declares an
    excluded status, skips and reports each status-bearing source's most frequent
    status values instead, so a status worth excluding is visible.
    """
    if l.events_path is None:
        return _skip("canonical layer not built")
    declaring = {sid: [s.strip().lower() for s in spec.excluded_status]
                 for sid, spec in l.cfg.sources.items() if spec.excluded_status}
    with_status = [sid for sid, spec in l.cfg.sources.items() if "status" in spec.fields]
    with _engine(l) as con:
        distribution: dict[str, dict[str, int]] = {}
        if with_status:
            for sid, status, n in con.execute(
                f"""
                SELECT source_id, lower(trim(status_source)), count(*) FROM evt
                WHERE source_id IN ({', '.join(_sql_str(s) for s in with_status)}) AND status_source IS NOT NULL
                GROUP BY 1, 2 ORDER BY 3 DESC
                """
            ).fetchall():
                per = distribution.setdefault(str(sid), {})
                if len(per) < 5:
                    per[reportable_spelling(status)] = int(n)
        if not declaring:
            return _skip_with("no source declares an excluded status; status distributions reported",
                              {"status_values": distribution})
        conditions = " OR ".join(
            f"(source_id = {_sql_str(sid)} AND lower(trim(status_source)) IN ({', '.join(_sql_str(v) for v in values)}))"
            for sid, values in declaring.items()
        )
        leaked = {str(sid): int(n) for sid, n in con.execute(
            f"SELECT source_id, count(*) FROM evt WHERE {conditions} GROUP BY 1"
        ).fetchall()}
    return CheckResult(
        "",
        not leaked,
        f"{len(declaring)} source(s) declare excluded statuses and no published event carries one"
        if not leaked
        else f"events published with a status their source excludes: {leaked}",
        {"declaring_sources": sorted(declaring), "leaked": leaked, "status_values": distribution},
    )
