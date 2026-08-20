"""Source rows -> canonical events (design sections 5.2, 5.3).

A *shape* describes how rows of a logical source turn into events. Shapes are generic
and registered by name; a dataset YAML picks one per source. Five cover everything in
the reference data, and adding a sixth is a code change on purpose -- an event shape is
semantics, and semantics do not belong in a config file that nobody reviews.

============================  =========================================================
Shape                         Meaning
============================  =========================================================
``point_event``               one row is one event
``narrative_lines``           many rows are the lines of one document
``component_measurements``    one row is one component of a study; the study is its own
                              event and the components hang off it
``person_attributes``         one row carries a person's static attributes
``visit``                     one row is an encounter, optionally carrying a death date
============================  =========================================================

Rules that hold across every shape:

* an anchor column never becomes an ``event_time``;
* a clinical event with no time is quarantined, never stamped with "now";
* ``event_id`` is derived from the clinical content only, so the same fact extracted
  into two batches collapses to one event while both source rows stay linked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator, Sequence

from ehr2cdm.canonical.values import ParsedValue, ValueParsingSpec, parse_value
from ehr2cdm.config import DatasetConfig, SourceSpec
from ehr2cdm.errors import QuarantineRow
from ehr2cdm.hashing import sha256_hex, stable_id
from ehr2cdm.registry import register_shape
from ehr2cdm.schema import (
    CanonicalEvent,
    EventKind,
    ProvenanceStatus,
    QualityFlag,
    QuarantineReason,
    SourceRelation,
)
from ehr2cdm.timeutil import TimeContext, parse_naive, to_utc

COL_PREFIX = "col__"


# --------------------------------------------------------------------------------
# row access
# --------------------------------------------------------------------------------


class Row:
    """One source row, addressed by canonical field role rather than column name.

    Column reordering, case changes and BOM presence all disappear here: the role map
    is built once per source from the declared aliases, case-insensitively.

    A role may list several aliases, and they are tried **per row**, not per file. That
    is how "collection time, falling back to result time" is expressed as data rather
    than as code -- and :meth:`alias_index` reports which one answered, so a fallback
    can be flagged instead of quietly pretending the preferred column was there.
    """

    __slots__ = ("data", "roles")

    def __init__(self, data: dict[str, Any], roles: dict[str, list[str]]):
        self.data = data
        self.roles = roles

    def raw(self, role: str) -> Any:
        value, _ = self.raw_with_index(role)
        return value

    def raw_with_index(self, role: str) -> tuple[Any, int]:
        """First alias of ``role`` carrying a value, with its position in the alias list."""
        for i, column in enumerate(self.roles.get(role, ())):
            value = self.data.get(column)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value, i
        return None, -1

    def alias_index(self, role: str) -> int:
        return self.raw_with_index(role)[1]

    def text(self, role: str, null_literals: Sequence[str] = ("NULL",)) -> str | None:
        value = self.raw(role)
        if value is None:
            return None
        s = str(value).strip()
        return None if not s or s in null_literals else s

    @property
    def subject_id(self) -> int:
        return int(self.data["subject_id"])

    @property
    def source_row_id(self) -> str:
        return str(self.data["source_row_id"])

    @property
    def partition_id(self) -> str:
        return str(self.data["partition_id"])

    @property
    def batch(self) -> str | None:
        return self.data.get("batch")

    @property
    def membership_label(self) -> str | None:
        return self.data.get("membership_label")

    @property
    def encounter_source_id(self) -> str | None:
        value = self.data.get("encounter_source_id")
        return str(value) if value not in (None, "") else None


def build_role_map(spec: SourceSpec, columns: Iterable[str]) -> dict[str, list[str]]:
    """Field role -> the actual parquet columns backing it, in declared alias order."""
    available = {c[len(COL_PREFIX) :].strip().lower(): c for c in columns if c.startswith(COL_PREFIX)}
    roles: dict[str, list[str]] = {}
    for role, fs in spec.fields.items():
        matched: list[str] = []
        for alias in fs.from_:
            column = available.get(alias.strip().lower())
            # Aliases differing only in case ("MRN" and "mrn") name one column, and
            # listing it twice would make a fallback look like a real second source.
            if column and column not in matched:
                matched.append(column)
        if matched:
            roles[role] = matched
    return roles


# --------------------------------------------------------------------------------
# shape context and results
# --------------------------------------------------------------------------------


@dataclass
class ShapeContext:
    cfg: DatasetConfig
    source_id: str
    spec: SourceSpec
    time: TimeContext
    values: ValueParsingSpec
    mapping_version: str

    @property
    def null_literals(self) -> tuple[str, ...]:
        return tuple(self.cfg.time.null_literals)


@dataclass
class Emission:
    events: list[CanonicalEvent] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "Emission") -> None:
        self.events.extend(other.events)
        self.links.extend(other.links)
        self.issues.extend(other.issues)
        self.quarantine.extend(other.quarantine)

    def link(self, event_id: str, row: Row, relation: SourceRelation = SourceRelation.derived_from) -> None:
        self.links.append(
            {
                "event_id": event_id,
                "source_row_id": row.source_row_id,
                "relation": str(relation),
                "partition_id": row.partition_id,
            }
        )

    def issue(self, issue_type: str, row: Row, detail: str = "", severity: str = "warning", event_id: str | None = None) -> None:
        self.issues.append(
            {
                "issue_type": issue_type,
                "severity": severity,
                "stage": "canonical",
                "subject_id": row.subject_id,
                "source_row_id": row.source_row_id,
                "event_id": event_id,
                "partition_id": row.partition_id,
                "source_id": None,
                "detail": detail,
            }
        )

    def quarantine_row(self, row: Row, reason: str, detail: str, source_id: str) -> None:
        self.quarantine.append(
            {
                "source_row_id": row.source_row_id,
                "dataset_id": None,
                "partition_id": row.partition_id,
                "source_id": source_id,
                "source_file": row.data.get("source_file"),
                "source_sheet": row.data.get("source_sheet"),
                "source_row_number": row.data.get("source_row_number"),
                "stage": "canonical",
                "reason": reason,
                "detail": detail,
                "person_source_id": row.data.get("person_source_id"),
                "raw_row": None,
            }
        )


# --------------------------------------------------------------------------------
# helpers shared by shapes
# --------------------------------------------------------------------------------


def event_identity(
    ctx: ShapeContext,
    *,
    subject_id: int,
    event_kind: str,
    code_system: str,
    source_code: str | None,
    event_time: datetime | None,
    encounter_id: str | None,
    discriminator: object = None,
    value: ParsedValue | None = None,
) -> str:
    """The canonical key of an event.

    Deliberately excludes the partition, the batch, the membership label and the
    anchor: the same clinical fact extracted twice must land on one event id, and a
    directory name must never be able to split it into two.
    """
    v = value or ParsedValue()
    return stable_id(
        ctx.cfg.dataset_id,
        ctx.source_id,
        subject_id,
        event_kind,
        code_system,
        source_code,
        event_time,
        encounter_id,
        v.number,
        v.text,
        v.low,
        v.high,
        v.unit,
        discriminator,
        prefix="event/",
    )


def resolve_times(ctx: ShapeContext, row: Row) -> tuple[datetime | None, datetime | None, datetime | None, list[str]]:
    """``event_time`` / ``available_time`` / ``end_time`` with the declared priority.

    When the preferred clinical time is missing and a fallback is configured for the
    source, the fallback is used *and flagged*; a silent substitution would make an
    availability-aware model train on times that never existed.
    """
    flags: list[str] = []
    event_raw, event_alias = row.raw_with_index("event_time")
    event_naive = parse_naive(event_raw, ctx.time)
    avail_naive = parse_naive(row.raw("available_time"), ctx.time)
    end_naive = parse_naive(row.raw("end_time"), ctx.time)

    if event_alias > 0:
        # The preferred clinical time was absent and a declared alternative answered.
        flags.append(str(QualityFlag.TIME_FALLBACK))
    if event_naive is None and avail_naive is not None:
        event_naive = avail_naive
        flags.append(str(QualityFlag.TIME_FALLBACK))
    if avail_naive is None and event_naive is not None:
        avail_naive = event_naive
        flags.append(str(QualityFlag.AVAILABILITY_ASSUMED))

    event_time, f1 = to_utc(event_naive, ctx.time)
    available_time, _ = to_utc(avail_naive, ctx.time)
    end_time, _ = to_utc(end_naive, ctx.time)
    return event_time, available_time, end_time, flags + f1


def make_event(ctx: ShapeContext, row: Row, **kwargs: Any) -> CanonicalEvent:
    kwargs.setdefault("mapping_version", ctx.mapping_version)
    kwargs.setdefault("provenance_status", ProvenanceStatus.observed)
    kwargs.setdefault("source_id", ctx.source_id)
    return CanonicalEvent(subject_id=row.subject_id, **kwargs)


def _dedup_flags(flags: Iterable[str]) -> list[str]:
    return sorted(set(f for f in flags if f))


# --------------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------------


@register_shape("point_event")
def shape_point_event(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """One row, one event: conditions, orders, administrations, single measurements."""
    out = Emission()
    kind = ctx.spec.event_kind or str(EventKind.measurement)
    for row in rows:
        try:
            event_time, available_time, end_time, flags = resolve_times(ctx, row)
            value = parse_value(
                row.raw("value"), row.raw("unit"), ctx.values, expect=_expect_for(ctx)
            )
        except QuarantineRow as q:
            out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
            continue

        if event_time is None:
            # A clinical event with no time is not publishable and is never given one.
            out.quarantine_row(
                row, str(QuarantineReason.MISSING_EVENT_TIME), f"event_kind={kind}", ctx.source_id
            )
            continue

        name = row.text("source_name", ctx.null_literals) or row.text("display_name", ctx.null_literals)
        # A row may carry a name but no code. The name then *is* the code, kept in the
        # source namespace: an event with no code at all is unusable downstream and a
        # null code would only surface later, as a silently unmappable term.
        code = row.text("source_code", ctx.null_literals) or name
        if code is None and name is None:
            out.quarantine_row(
                row, str(QuarantineReason.UNPARSEABLE_VALUE), "no source code or name", ctx.source_id
            )
            continue

        discriminator = row.text("sequence_number", ctx.null_literals)
        event_id = event_identity(
            ctx,
            subject_id=row.subject_id,
            event_kind=kind,
            code_system=ctx.spec.code_system,
            source_code=code,
            event_time=event_time,
            encounter_id=row.encounter_source_id,
            discriminator=discriminator,
            value=value,
        )
        out.events.append(
            make_event(
                ctx,
                row,
                event_id=event_id,
                encounter_id=row.encounter_source_id,
                event_kind=kind,
                event_time=event_time,
                available_time=available_time,
                end_time=end_time,
                code_system=ctx.spec.code_system,
                source_code=code,
                source_name=name,
                value_number=value.number,
                value_text=value.text,
                value_low=value.low,
                value_high=value.high,
                unit_source=value.unit,
                status_source=row.text("status", ctx.null_literals),
                route_source=row.text("route", ctx.null_literals),
                dose_source=row.text("dose", ctx.null_literals),
                quality_flags=_dedup_flags(flags + value.flags),
            )
        )
        out.link(event_id, row)
    return out


@register_shape("narrative_lines")
def shape_narrative_lines(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """Many rows are the lines of one document.

    Reports arrive one line per row, and the whole report is repeated once per
    extraction anchor. Grouping by the clinical identity of the report and keying each
    line by its line number collapses those repeats exactly, with every repeated row
    still linked to the single note event.
    """
    out = Emission()
    kind = ctx.spec.event_kind or str(EventKind.note)
    for key, group in _group(ctx, rows, out):
        subject_id, encounter_id, title, event_time, available_time, flags = key
        lines: dict[tuple[int, str], str] = {}
        line_rows: dict[tuple[int, str], list[Row]] = {}
        for row in group:
            text = row.text("text", ctx.null_literals)
            line_no = _as_int(row.text("text_line", ctx.null_literals))
            if text is None:
                continue
            slot = (line_no if line_no is not None else 0, sha256_hex(text)[:16])
            lines.setdefault(slot, text)
            line_rows.setdefault(slot, []).append(row)

        ordered = [lines[k] for k in sorted(lines)]
        body = "\n".join(ordered)
        first = group[0]
        if not body:
            # A report row with no text is a real record of an empty extraction; it is
            # kept as coverage, not turned into an empty note.
            out.issue("EMPTY_NARRATIVE", first, f"title={title}")
            continue

        event_id = event_identity(
            ctx,
            subject_id=subject_id,
            event_kind=kind,
            code_system=ctx.spec.code_system,
            source_code=title,
            event_time=event_time,
            encounter_id=encounter_id,
            discriminator=sha256_hex(body),
        )
        out.events.append(
            make_event(
                ctx,
                first,
                event_id=event_id,
                encounter_id=encounter_id,
                event_kind=kind,
                event_time=event_time,
                available_time=available_time,
                code_system=ctx.spec.code_system,
                source_code=title,
                source_name=title,
                value_text=body,
                quality_flags=_dedup_flags(flags),
            )
        )
        seen_rows: set[str] = set()
        for slot, rws in line_rows.items():
            for i, row in enumerate(rws):
                if row.source_row_id in seen_rows:
                    continue
                seen_rows.add(row.source_row_id)
                relation = SourceRelation.derived_from if i == 0 else SourceRelation.duplicate_of
                out.link(event_id, row, relation)

        if ctx.spec.study_event_kind:
            out.extend(
                _emit_study_event(
                    ctx,
                    first,
                    subject_id=subject_id,
                    encounter_id=encounter_id,
                    title=title,
                    event_time=event_time,
                    available_time=available_time,
                    child_event_id=event_id,
                    rows=group,
                    flags=flags,
                )
            )
    return out


@register_shape("component_measurements")
def shape_component_measurements(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """One row is one component of a study; the study itself becomes its own event."""
    out = Emission()
    kind = ctx.spec.event_kind or str(EventKind.measurement)
    for key, group in _group(ctx, rows, out):
        subject_id, encounter_id, title, event_time, available_time, flags = key
        study_emission = (
            _emit_study_event(
                ctx,
                group[0],
                subject_id=subject_id,
                encounter_id=encounter_id,
                title=title,
                event_time=event_time,
                available_time=available_time,
                child_event_id=None,
                rows=group,
                flags=flags,
            )
            if ctx.spec.study_event_kind
            else Emission()
        )
        parent_id = study_emission.events[0].event_id if study_emission.events else None
        out.extend(study_emission)

        emitted: dict[str, list[Row]] = {}
        for row in group:
            code = row.text("source_code", ctx.null_literals)
            if code is None:
                out.quarantine_row(
                    row, str(QuarantineReason.UNPARSEABLE_VALUE), "component has no name", ctx.source_id
                )
                continue
            try:
                value = parse_value(row.raw("value"), row.raw("unit"), ctx.values, expect=_expect_for(ctx))
            except QuarantineRow as q:
                out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
                continue
            line_no = _as_int(row.text("text_line", ctx.null_literals))
            event_id = event_identity(
                ctx,
                subject_id=subject_id,
                event_kind=kind,
                code_system=ctx.spec.code_system,
                source_code=code,
                event_time=event_time,
                encounter_id=encounter_id,
                discriminator=line_no,
                value=value,
            )
            if event_id not in emitted:
                emitted[event_id] = []
                out.events.append(
                    make_event(
                        ctx,
                        row,
                        event_id=event_id,
                        encounter_id=encounter_id,
                        event_kind=kind,
                        event_time=event_time,
                        available_time=available_time,
                        code_system=ctx.spec.code_system,
                        source_code=code,
                        source_name=row.text("source_name", ctx.null_literals) or code,
                        value_number=value.number,
                        value_text=value.text,
                        value_low=value.low,
                        value_high=value.high,
                        unit_source=value.unit,
                        quality_flags=_dedup_flags(list(flags) + value.flags),
                        parent_event_id=parent_id,
                    )
                )
                out.link(event_id, row)
            else:
                out.link(event_id, row, SourceRelation.duplicate_of)
            emitted[event_id].append(row)
    return out


@register_shape("person_attributes")
def shape_person_attributes(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """Static person attributes, plus a death date when the source carries one.

    Static attributes legitimately have no time. Attributes that *should* have one and
    do not -- a body-mass index, a pulse -- are quarantined by default rather than
    given a plausible-looking date.
    """
    out = Emission()
    for row in rows:
        for role in ("gender", "race", "ethnicity", "vital_status"):
            text = row.text(role, ctx.null_literals)
            if text is None:
                continue
            event_id = event_identity(
                ctx,
                subject_id=row.subject_id,
                event_kind=str(EventKind.demographic),
                code_system=ctx.spec.code_system,
                source_code=role.upper(),
                event_time=None,
                encounter_id=None,
                value=ParsedValue(text=text),
            )
            out.events.append(
                make_event(
                    ctx,
                    row,
                    event_id=event_id,
                    event_kind=str(EventKind.demographic),
                    event_time=None,
                    code_system=ctx.spec.code_system,
                    source_code=role.upper(),
                    source_name=role,
                    value_text=text,
                )
            )
            out.link(event_id, row)

        age_text = row.text("age", ctx.null_literals)
        if age_text is not None:
            try:
                age_value = parse_value(age_text, None, ctx.values, expect="numeric")
            except QuarantineRow as q:
                out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
                age_value = None
            if age_value is not None:
                event_id = event_identity(
                    ctx,
                    subject_id=row.subject_id,
                    event_kind=str(EventKind.demographic),
                    code_system=ctx.spec.code_system,
                    source_code="AGE",
                    event_time=None,
                    encounter_id=None,
                    value=age_value,
                )
                out.events.append(
                    make_event(
                        ctx,
                        row,
                        event_id=event_id,
                        event_kind=str(EventKind.demographic),
                        event_time=None,
                        code_system=ctx.spec.code_system,
                        source_code="AGE",
                        source_name="age",
                        value_number=age_value.number,
                        value_text=age_value.text,
                    )
                )
                out.link(event_id, row)

        out.extend(_emit_death(ctx, row))
        out.extend(_emit_untimed(ctx, row))
    return out


@register_shape("visit")
def shape_visit(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """An encounter. An end time derived from a length of stay is flagged as derived."""
    out = Emission()
    for row in rows:
        try:
            event_time, available_time, end_time, flags = resolve_times(ctx, row)
        except QuarantineRow as q:
            out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
            continue
        if event_time is None:
            out.quarantine_row(row, str(QuarantineReason.MISSING_EVENT_TIME), "visit", ctx.source_id)
            out.extend(_emit_death(ctx, row))
            continue

        los = row.text("length_of_stay", ctx.null_literals)
        if end_time is None and los is not None:
            try:
                days = parse_value(los, None, ctx.values, expect="numeric")
            except QuarantineRow:
                days = None
            if days is not None and days.number is not None:
                from datetime import timedelta

                end_time = event_time + timedelta(days=days.number)
                flags.append(str(QualityFlag.DERIVED_END_TIME))

        visit_type = row.text("visit_type", ctx.null_literals)
        event_id = event_identity(
            ctx,
            subject_id=row.subject_id,
            event_kind=str(EventKind.visit),
            code_system=ctx.spec.code_system,
            source_code=visit_type,
            event_time=event_time,
            encounter_id=row.encounter_source_id,
        )
        out.events.append(
            make_event(
                ctx,
                row,
                event_id=event_id,
                encounter_id=row.encounter_source_id,
                event_kind=str(EventKind.visit),
                event_time=event_time,
                available_time=available_time,
                end_time=end_time,
                code_system=ctx.spec.code_system,
                source_code=visit_type,
                source_name=visit_type,
                value_text=row.text("duration_masked", ctx.null_literals),
                quality_flags=_dedup_flags(flags),
                provenance_status=(
                    ProvenanceStatus.derived
                    if str(QualityFlag.DERIVED_END_TIME) in flags
                    else ProvenanceStatus.observed
                ),
            )
        )
        out.link(event_id, row)
        out.extend(_emit_death(ctx, row))
    return out


# --------------------------------------------------------------------------------
# shared emitters
# --------------------------------------------------------------------------------


def _emit_death(ctx: ShapeContext, row: Row) -> Emission:
    """A death event, when the row carries a death date.

    Several sources carry the same death date. Identical dates collapse to one event by
    key; disagreeing dates deliberately produce two events, which validation reports as
    a conflict for a human rather than picking a winner.
    """
    out = Emission()
    raw = row.raw("death_time")
    if raw is None:
        return out
    try:
        naive = parse_naive(raw, ctx.time)
    except QuarantineRow as q:
        out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
        return out
    if naive is None:
        return out
    death_time, flags = to_utc(naive, ctx.time)
    event_id = stable_id(
        ctx.cfg.dataset_id, row.subject_id, "death", death_time, prefix="event/death/"
    )
    out.events.append(
        make_event(
            ctx,
            row,
            event_id=event_id,
            event_kind=str(EventKind.death),
            event_time=death_time,
            available_time=death_time,
            code_system=ctx.spec.code_system,
            source_code="DEATH",
            source_name="death",
            quality_flags=_dedup_flags(flags),
        )
    )
    out.link(event_id, row)
    return out


def _emit_untimed(ctx: ShapeContext, row: Row) -> Emission:
    """Measurement-like columns with no time of their own.

    Quarantined by default. They are real measurements, but nothing in the export says
    when they were taken, and a measurement carrying an invented date is worse than a
    missing one.
    """
    out = Emission()
    for spec in ctx.spec.untimed_values:
        column = None
        for candidate in (f"{COL_PREFIX}{spec.column}", spec.column):
            if candidate in row.data:
                column = candidate
                break
        if column is None:
            continue
        raw = row.data.get(column)
        if raw is None or str(raw).strip() in ("",) + ctx.null_literals:
            continue
        out.quarantine_row(
            row,
            str(QuarantineReason.UNTIMED_CLINICAL_VALUE),
            f"{spec.code}={str(raw)[:64]} has no measurement time in the source",
            ctx.source_id,
        )
    return out


def _emit_study_event(
    ctx: ShapeContext,
    row: Row,
    *,
    subject_id: int,
    encounter_id: str | None,
    title: str | None,
    event_time: datetime | None,
    available_time: datetime | None,
    child_event_id: str | None,
    rows: Sequence[Row],
    flags: Sequence[str],
) -> Emission:
    """The study a set of rows belongs to (a procedure), emitted once per group."""
    out = Emission()
    if title is None or event_time is None:
        return out
    kind = ctx.spec.study_event_kind or str(EventKind.procedure)
    event_id = event_identity(
        ctx,
        subject_id=subject_id,
        event_kind=kind,
        code_system=ctx.spec.code_system,
        source_code=title,
        event_time=event_time,
        encounter_id=encounter_id,
    )
    out.events.append(
        make_event(
            ctx,
            row,
            event_id=event_id,
            encounter_id=encounter_id,
            event_kind=kind,
            event_time=event_time,
            available_time=available_time,
            code_system=ctx.spec.code_system,
            source_code=title,
            source_name=title,
            quality_flags=_dedup_flags(flags),
        )
    )
    for i, r in enumerate(rows):
        out.link(event_id, r, SourceRelation.derived_from if i == 0 else SourceRelation.duplicate_of)
    return out


GroupKey = tuple[int, str | None, str | None, datetime | None, datetime | None, tuple[str, ...]]


def _group(ctx: ShapeContext, rows: Sequence[Row], out: Emission) -> Iterator[tuple[GroupKey, list[Row]]]:
    """Group rows into the report or study they belong to.

    The grouping key is the clinical identity of the study -- subject, encounter, study
    name, result time -- and deliberately not the anchor, which is what makes the
    once-per-anchor duplication collapse.
    """
    groups: dict[GroupKey, list[Row]] = {}
    for row in rows:
        try:
            event_time, available_time, _end, flags = resolve_times(ctx, row)
        except QuarantineRow as q:
            out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
            continue
        if event_time is None:
            out.quarantine_row(
                row, str(QuarantineReason.MISSING_EVENT_TIME), "study row", ctx.source_id
            )
            continue
        title = row.text("display_name", ctx.null_literals) or row.text("source_name", ctx.null_literals)
        key: GroupKey = (
            row.subject_id,
            row.encounter_source_id,
            title,
            event_time,
            available_time,
            tuple(_dedup_flags(flags)),
        )
        groups.setdefault(key, []).append(row)
    for key in sorted(groups, key=_group_sort_key):
        yield key, groups[key]


def _group_sort_key(key: GroupKey) -> tuple:
    subject_id, encounter_id, title, event_time, available_time, flags = key
    return (
        subject_id,
        encounter_id or "",
        title or "",
        event_time or datetime.min,
        available_time or datetime.min,
        flags,
    )


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _expect_for(ctx: ShapeContext) -> str:
    return "auto"


def get_shape(name: str) -> Callable[[ShapeContext, Sequence[Row]], Emission]:
    from ehr2cdm.registry import SHAPES

    shape = SHAPES.get(name)
    if shape is None:
        raise KeyError(f"unregistered shape {name!r}")
    return shape  # type: ignore[return-value]
