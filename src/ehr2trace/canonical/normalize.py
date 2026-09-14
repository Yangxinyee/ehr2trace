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
``visit``                     one row is an encounter (or, as ``visit_detail``, a stay inside
                              one), optionally carrying a death date
============================  =========================================================

Rules that hold across every shape:

* an anchor column never becomes an ``event_time``;
* a clinical event with no time is quarantined, never stamped with "now";
* ``event_id`` is derived from the clinical content only, so the same fact extracted
  into two batches collapses to one event while both source rows stay linked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Iterator, Sequence

from ehr2trace.canonical.dedup import Instance, is_truthy_cell, rule_field
from ehr2trace.canonical.values import ParsedValue, ValueParsingSpec, parse_value
from ehr2trace.config import DatasetConfig, SourceSpec
from ehr2trace.errors import QuarantineRow
from ehr2trace.hashing import canonical_cell, sha256_hex, stable_id
from ehr2trace.reference import ReferenceTables
from ehr2trace.registry import register_shape
from ehr2trace.schema import (
    CanonicalEvent,
    EventKind,
    ProvenanceStatus,
    QualityFlag,
    QuarantineReason,
    SourceRelation,
)
from ehr2trace.timeutil import TimeContext, parse_naive, to_utc

COL_PREFIX = "col__"

#: Field roles that hold a time; an identity extra naming one is folded in as the
#: parsed instant rather than as the source's spelling of it.
TIME_ROLES: frozenset[str] = frozenset(
    {"event_time", "available_time", "end_time", "anchor_time", "death_time", "birth_date"}
)

#: Kinds whose identity includes the dose, unit, route, status and end time (T1.1).
#: Two orders of one drug in one minute with different doses are two orders.
DRUG_KINDS: frozenset[str] = frozenset(
    {str(EventKind.drug_order), str(EventKind.drug_admin), str(EventKind.drug_dispense)}
)


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
            # Aliases differing only in case name one and the same column, and
            # listing it twice would make a fallback look like a real second source.
            if column and column not in matched:
                matched.append(column)
        if matched:
            roles[role] = matched
    return roles


def filter_rows(spec: SourceSpec, rows: Sequence["Row"], columns: Iterable[str]) -> list["Row"]:
    """Keep only the rows a declared ``row_filter`` admits.

    A dropped row produces no event, which is already an allowed outcome -- a
    demographics row carrying nothing but a patient key produces none either. What the
    design forbids is that being invisible, and it is not: SOURCE_ROWS_ACCOUNTED reports
    every parsed row's destination, so rows removed here show up as a number that moved.

    ``excluded_status`` is the same mechanism keyed on the ``status`` role instead of a
    column: a problem list's deleted entries are not diagnoses (D-R6), and dropping them
    here keeps them out of every downstream table at once.
    """
    out = list(rows)
    excluded = {v.strip().lower() for v in spec.excluded_status}
    if excluded:
        out = [row for row in out if (row.text("status") or "").strip().lower() not in excluded]
    filters = spec.row_filters
    if not filters:
        return out
    available = {c[len(COL_PREFIX):].strip().lower(): c for c in columns if c.startswith(COL_PREFIX)}
    for rf in filters:
        column = available.get(rf.column.strip().lower())
        if column is None:
            # A filter naming a column the source does not have would silently keep
            # everything, which is the opposite of what was asked for.
            raise ValueError(
                f"row_filter names column {rf.column!r}, which this source does not have; "
                f"available: {sorted(available)[:20]}"
            )
        keep = {v.strip().lower() for v in rf.keep}
        drop = {v.strip().lower() for v in rf.drop}
        passed = []
        for row in out:
            value = row.data.get(column)
            text = "" if value is None else str(value).strip().lower()
            if keep and text not in keep:
                continue
            if text in drop:
                continue
            passed.append(row)
        out = passed
    return out


def kind_resolver(ctx: "ShapeContext", rows: Sequence["Row"], fixed: str) -> Callable[["Row"], str]:
    """How to decide one row's event kind.

    Fixed for most sources: the table is one kind of thing. When ``event_kind_from`` is
    declared the kind is read from a column instead, because the table is not -- an
    order entry table holds medication orders next to laboratory and radiology ones.

    A column the source does not have raises rather than letting every row fall to the
    default. A typo would otherwise relabel a whole table in one direction, silently,
    and the result would still validate.
    """
    spec = ctx.spec.event_kind_from
    if spec is None:
        return lambda row: fixed
    available = {
        c[len(COL_PREFIX):].strip().lower(): c
        for c in (rows[0].data if rows else {})
        if c.startswith(COL_PREFIX)
    }
    column = available.get(spec.column.strip().lower())
    if column is None:
        raise ValueError(
            f"event_kind_from names column {spec.column!r}, which this source does not "
            f"have; every row would fall through to {spec.default!r}. "
            f"available: {sorted(available)[:20]}"
        )
    mapping = {k.strip().lower(): v for k, v in spec.map.items()}

    def resolve(row: "Row") -> str:
        value = row.data.get(column)
        key = str(value).strip().lower() if value is not None else ""
        return mapping.get(key, spec.default)

    return resolve


def record_action_keys(out: "Emission", ctx: "ShapeContext", row: "Row", event_id: str) -> None:
    """Note this event's own action key, and the key of the action that caused it.

    Both are source business keys rather than event ids. The causing row is read by a
    different source -- an administration names its order, and the order is a row in the
    order entry table -- so the id it will be given is not knowable here.
    """
    own = row.text("action_key", ctx.null_literals)
    if own is not None:
        out.action_keys.append({"event_id": event_id, "key": own, "role": "declares"})
    cause = row.text("caused_by", ctx.null_literals)
    if cause is not None:
        out.action_keys.append({"event_id": event_id, "key": cause, "role": "caused_by"})


def split_codes(ctx: "ShapeContext", row: "Row") -> list["Row"]:
    """One row holding several codes -> one row per code, or the row unchanged.

    Each piece keeps every other column, so the three diagnoses in `R78.81, B95.7,
    Z16.29` become three events sharing a time, an encounter and a source row. They stay
    linked to that one row: the lineage says one source row produced three events, which
    is what happened.
    """
    spec = ctx.spec.code_split
    if spec is None:
        return [row]
    pieces: dict[str, list[str]] = {}
    for role in spec.roles:
        text = row.text(role, ctx.null_literals)
        if text and spec.separator in text:
            parts = [p.strip() for p in text.split(spec.separator)]
            parts = [p for p in parts if p]
            if len(parts) > 1:
                pieces[role] = parts
    if not pieces:
        return [row]
    width = max(len(v) for v in pieces.values())
    out: list[Row] = []
    for i in range(width):
        data = dict(row.data)
        for role, parts in pieces.items():
            column = (row.roles.get(role) or [None])[0]
            if column is None:
                continue
            data[column] = parts[i] if i < len(parts) else parts[-1]
        out.append(Row(data, row.roles))
    return out


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
    #: unit spellings, exact conversions and plausible ranges (see ehr2trace.reference)
    reference: ReferenceTables = field(default_factory=ReferenceTables.empty)

    @property
    def null_literals(self) -> tuple[str, ...]:
        return tuple(self.cfg.time.null_literals)


@dataclass
class Emission:
    events: list[CanonicalEvent] = field(default_factory=list)
    #: one per event, in step with ``events``: the row behind it and the values the
    #: merge rules read. Attached by :meth:`emit`; read by ``merge_events``.
    instances: list[Instance] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    #: source business keys naming actions, resolved to event ids once the build has
    #: every source in view. See ``resolve_causes``.
    action_keys: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "Emission") -> None:
        self.events.extend(other.events)
        self.instances.extend(other.instances)
        self.links.extend(other.links)
        self.issues.extend(other.issues)
        self.quarantine.extend(other.quarantine)
        self.action_keys.extend(other.action_keys)

    def emit(self, event: CanonicalEvent, row: Row, extra: dict[str, Any] | None = None) -> None:
        """Record an event together with the row it came from."""
        self.events.append(event)
        self.instances.append(Instance(row.source_row_id, row.partition_id, extra or None))

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
    extra: Sequence[object] = (),
) -> str:
    """The canonical key of an event.

    Deliberately excludes the partition, the batch, the membership label and the
    anchor: the same clinical fact extracted twice must land on one event id, and a
    directory name must never be able to split it into two.

    ``extra`` is what a shape adds for its kind -- a drug's normalized dose, unit,
    route, status and end time; a visit's declared ``identity_extra_fields``. The
    encounter leaves the key when the source says its encounter ids are noise
    (``encounter_in_identity: false``), so the copies of one note collapse.
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
        encounter_id if ctx.spec.encounter_in_identity else None,
        v.number,
        v.text,
        v.low,
        v.high,
        v.unit,
        discriminator,
        *extra,
        prefix="event/",
    )


def identity_extras(ctx: ShapeContext, row: Row) -> tuple[object, ...]:
    """The values of ``identity_extra_fields``, in declared order.

    A time role contributes the parsed instant, a number the number, anything else its
    stripped text -- so ``3`` and ``3.0`` name one length of stay and a bare date and
    its midnight timestamp name one end time, exactly as they would anywhere else in
    the identity.
    """
    out: list[object] = []
    for role in ctx.spec.identity_extra_fields:
        if role in TIME_ROLES:
            try:
                naive = parse_naive(row.raw(role), ctx.time)
            except QuarantineRow:
                out.append(row.text(role, ctx.null_literals))
                continue
            out.append(to_utc(naive, ctx.time)[0])
            continue
        text = row.text(role, ctx.null_literals)
        if text is None:
            out.append(None)
            continue
        parsed = parse_value(text, None, ctx.values)
        out.append(parsed.number if parsed.number is not None else text)
    return tuple(out)


def dose_identity(ctx: ShapeContext, dose: str | None) -> str | None:
    """A dose as it enters a drug event's identity: ``"<number> <unit>"``.

    ``10 mg``, ``10 MG`` and ``10.0 mg`` are one dose; the text is kept as written on
    the event and only the identity reads through the spelling. A dose that does not
    parse as a number (``1 tablet``, ``1-2 tabs``) is compared as lower-cased text.
    """
    if dose is None:
        return None
    parsed = parse_value(dose, None, ctx.values)
    if parsed.number is not None:
        unit = (parsed.unit or "").strip().lower()
        return f"{canonical_cell(parsed.number)} {unit}".strip()
    return dose.strip().lower()


@dataclass
class UnitOutcome:
    """What unit normalization decided for one value (T1.6, T1.7; D-R1, D-R10, D-R17)."""

    source_code: str | None
    unit_source: str | None
    unit_normalized: str | None
    value_number_normalized: float | None
    flags: list[str]


def normalize_units(ctx: ShapeContext, code: str | None, value: ParsedValue) -> UnitOutcome:
    """Resolve the unit a value is in, normalize it, and judge the value against its range.

    Resolution order: the unit the source gave (a unit column, or embedded in the
    cell) -> ``declared_units`` when the source gave none -> ``unit_override``, which
    replaces whatever was resolved because the source's own label is known to be wrong.
    ``unit_source`` always keeps the source's string; the override and the declaration
    only reach ``unit_normalized``. The value is converted only by an exact rule, and a
    converted value outside its declared plausible range is withheld from the
    normalized column and flagged; ``value_number`` is never touched.
    """
    spec = ctx.spec
    flags: list[str] = []
    unit_source = value.unit
    resolving = unit_source
    if resolving is None and code is not None and code in spec.declared_units:
        resolving = spec.declared_units[code]
        flags.append(str(QualityFlag.UNIT_DECLARED))
    if code is not None and code in spec.unit_override:
        # Flagged as overridden only when it replaced something; a row of that code
        # with no unit at all has had one declared for it, which is a different claim.
        flags.append(str(QualityFlag.UNIT_OVERRIDDEN if resolving is not None else QualityFlag.UNIT_DECLARED))
        resolving = spec.unit_override[code]

    unit_normalized: str | None
    value_normalized: float | None
    if resolving is None:
        unit_normalized, value_normalized = None, value.number
    else:
        ucum = ctx.reference.units.lookup(resolving)
        if ucum is None:
            flags.append(str(QualityFlag.UNIT_UNKNOWN))
            unit_normalized, value_normalized = None, None
        else:
            unit_normalized, value_normalized = ctx.reference.normalize(ucum, value.number)

    if code is not None and unit_source is not None and code in spec.split_code_by_unit:
        # D-R10: the source mixes units under one code that cannot be converted into
        # each other, so the unit becomes part of the code and of the identity.
        code = f"{code}|{unit_source}"
        flags.append(str(QualityFlag.CODE_SPLIT_BY_UNIT))

    if value_normalized is not None and code is not None:
        base = code.split("|", 1)[0]
        for lookup in (code, base) if base != code else (code,):
            if ctx.reference.implausible(spec.code_system, lookup, unit_normalized, value_normalized):
                flags.append(str(QualityFlag.IMPLAUSIBLE))
                value_normalized = None
                break

    return UnitOutcome(code, unit_source, unit_normalized, value_normalized, flags)


def parse_rate(ctx: ShapeContext, row: Row) -> tuple[str | None, float | None, str | None, list[str]]:
    """``rate_source`` verbatim, ``rate`` as a number, ``rate_unit`` from its role or the cell."""
    source = row.text("rate", ctx.null_literals)
    unit = row.text("rate_unit", ctx.null_literals)
    if source is None:
        return None, None, unit, []
    parsed = parse_value(source, None, ctx.values)
    if parsed.number is None:
        return source, None, unit, [str(QualityFlag.RATE_UNPARSED)]
    return source, parsed.number, unit or parsed.unit, []


def flag_resolver(ctx: ShapeContext, rows: Sequence[Row]) -> Callable[[Row], list[str]]:
    """``flag_when``: a truthy marker cell adds the declared flag to the row's events.

    A column the source does not have raises, as a ``row_filter`` naming one does: a
    typo would otherwise silently un-flag every row.
    """
    if not ctx.spec.flag_when:
        return lambda row: []
    available = _available_columns(rows)
    plan: list[tuple[str, str]] = []
    for flag, column in sorted(ctx.spec.flag_when.items()):
        found = available.get(column.strip().lower())
        if found is None:
            raise ValueError(
                f"flag_when names column {column!r} for {flag}, which this source does not "
                f"have; available: {sorted(available)[:20]}"
            )
        plan.append((flag, found))

    def resolve(row: Row) -> list[str]:
        return [flag for flag, column in plan if is_truthy_cell(row.data.get(column))]

    return resolve


def merge_extra_resolver(ctx: ShapeContext, rows: Sequence[Row]) -> Callable[[Row], dict[str, Any] | None]:
    """The per-row values a source's ``merge_rules`` read that the event itself lacks.

    A rule keyed by a canonical field, or by the role that feeds one, compares the
    event; only a rule keyed by a kept column or another role, and the ``linked_by``
    marker of a ``prefer_linked`` rule, needs the row's value carried beside the event.
    """
    spec = ctx.spec
    names = {name for name in spec.merge_rules if rule_field(name) is None}
    names |= {rule.linked_by for rule in spec.merge_rules.values() if rule.rule == "prefer_linked"}
    if not names:
        return lambda row: None
    available = _available_columns(rows)
    plan: list[tuple[str, str | None]] = []
    for name in sorted(names):
        if name in spec.fields:
            plan.append((name, None))
            continue
        column = available.get(name.strip().lower())
        if column is not None:
            plan.append((name, column))

    def resolve(row: Row) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, column in plan:
            if column is None:
                out[name] = row.text(name, ctx.null_literals)
            else:
                raw = row.data.get(column)
                text = None if raw is None else str(raw).strip()
                out[name] = text if text and text not in ctx.null_literals else None
        return out

    return resolve


def _available_columns(rows: Sequence[Row]) -> dict[str, str]:
    return {
        c[len(COL_PREFIX):].strip().lower(): c
        for c in (rows[0].data if rows else {})
        if c.startswith(COL_PREFIX)
    }


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

    if event_time is not None and available_time is not None and available_time < event_time:
        # The source contradicts itself: a result cannot have been visible before the
        # specimen was taken. Which of the two timestamps is wrong is unknowable from
        # here, so availability moves to the later one -- the conservative direction for
        # a field whose whole purpose is to keep the future out of a training window --
        # and the contradiction is flagged rather than quietly accepted.
        available_time = event_time
        flags.append(str(QualityFlag.AVAILABILITY_BEFORE_EVENT))

    if end_time is not None and event_time is not None and end_time < event_time:
        # The other way a source contradicts itself, and the one that is not repairable
        # here. Availability above can be moved to the conservative side because its
        # whole job is to keep the future out of a window; an end time has no such safe
        # direction -- clamping it to the start invents a zero duration, swapping the two
        # invents an interval, and dropping it destroys the only record that the source
        # said something impossible. So the times are kept exactly as written and the
        # contradiction is carried on the event, the way a post-death record is.
        #
        # This is not rare enough to treat as noise: MIMIC-IV's `prescriptions` has
        # `stoptime` before `starttime` on 816,994 of 20,292,611 rows.
        flags.append(str(QualityFlag.END_BEFORE_START))

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
    kind_of = kind_resolver(ctx, rows, ctx.spec.event_kind or str(EventKind.measurement))
    flags_of = flag_resolver(ctx, rows)
    extra_of = merge_extra_resolver(ctx, rows)
    administered = {v.strip().lower() for v in ctx.spec.administered_when}
    for row in rows:
        kind = kind_of(row)
        try:
            event_time, available_time, end_time, flags = resolve_times(ctx, row)
            value = parse_value(
                row.raw("value"), row.raw("unit"), ctx.values, expect=_expect_for(ctx)
            )
        except QuarantineRow as q:
            out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
            continue
        flags = flags + flags_of(row)

        # A source may carry its content as free text rather than as a result value, and
        # a whole note per row is the case that matters. `text` is a declared role and
        # this shape used to drop it, which published every such note as an empty shell
        # that all thirty-eight checks accepted -- a note event with a code, a time and
        # lineage is structurally valid, so nothing had a reason to object. The content
        # goes where `narrative_lines` puts a report body, `value_text`, which is what
        # the OMOP note exporter reads and what makes it part of the event's identity.
        body = row.text("text", ctx.null_literals)
        if body is not None:
            value = ParsedValue(text=body, unit=value.unit, form="text")

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

        status_text = row.text("status", ctx.null_literals)
        if administered and kind == str(EventKind.drug_admin):
            # An administration record logs more than administrations. Two thirds of
            # MIMIC-IV's say the drug was given; the rest are refusals, held doses, and
            # nursing actions -- flushes, assessments, infusion reconciliations -- that
            # are real events but not evidence of exposure.
            if status_text is None or status_text.strip().lower() not in administered:
                flags = flags + [str(QualityFlag.NOT_ADMINISTERED)]

        units = normalize_units(ctx, code, value)
        code = units.source_code
        route_text = row.text("route", ctx.null_literals)
        dose_text = row.text("dose", ctx.null_literals)
        rate_source, rate, rate_unit, rate_flags = parse_rate(ctx, row)

        extra: tuple[object, ...] = identity_extras(ctx, row)
        identity_value = value
        if kind in DRUG_KINDS:
            # T1.1: the same drug ordered twice in one minute at two doses is two
            # orders, and an order continued to a different end date is a different
            # order. The dose and its unit are read through their spelling (`10 mg`,
            # `10 MG`), so the unit leaves the value's part of the key and enters here
            # lower-cased. A condition's status stays out: a problem first Active and
            # later Resolved is one problem (D-R6 settles the status by merge rule).
            identity_value = ParsedValue(value.number, value.text, value.low, value.high, None)
            extra = (dose_identity(ctx, dose_text), (units.unit_source or "").strip().lower(),
                     route_text, status_text, end_time) + extra
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
            value=identity_value,
            extra=extra,
        )
        out.emit(
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
                unit_source=units.unit_source,
                status_source=status_text,
                route_source=route_text,
                dose_source=dose_text,
                value_number_normalized=units.value_number_normalized,
                unit_normalized=units.unit_normalized,
                rate_source=rate_source,
                rate=rate,
                rate_unit=rate_unit,
                action=row.text("action", ctx.null_literals),
                discharged_to=row.text("discharged_to", ctx.null_literals),
                quality_flags=_dedup_flags(flags + value.flags + units.flags + rate_flags),
            ),
            row,
            extra_of(row),
        )
        out.link(event_id, row)
        record_action_keys(out, ctx, row, event_id)
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
    flags_of = flag_resolver(ctx, rows)
    extra_of = merge_extra_resolver(ctx, rows)
    for (key, group_flags), group in _group(ctx, rows, out):
        subject_id, encounter_id, title, event_time, available_time = key
        flags = tuple(group_flags) + tuple(f for row in group for f in flags_of(row))
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
        out.emit(
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
            ),
            first,
            extra_of(first),
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
    flags_of = flag_resolver(ctx, rows)
    extra_of = merge_extra_resolver(ctx, rows)
    for (key, flags), group in _group(ctx, rows, out):
        subject_id, encounter_id, title, event_time, available_time = key
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
            units = normalize_units(ctx, code, value)
            code = units.source_code
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
                out.emit(
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
                        unit_source=units.unit_source,
                        value_number_normalized=units.value_number_normalized,
                        unit_normalized=units.unit_normalized,
                        quality_flags=_dedup_flags(list(flags) + flags_of(row) + value.flags + units.flags),
                        parent_event_id=parent_id,
                    ),
                    row,
                    extra_of(row),
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
        for role in ("gender", "race", "ethnicity"):
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
            out.emit(
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
                ),
                row,
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
                out.emit(
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
                    ),
                    row,
                )
                out.link(event_id, row)

        out.extend(_emit_birth_date(ctx, row))
        death = _emit_death(ctx, row)
        out.extend(death)
        out.extend(_emit_vital_status(ctx, row, timed=bool(death.events)))
        out.extend(_emit_untimed(ctx, row))
    return out


@register_shape("visit")
def shape_visit(ctx: ShapeContext, rows: Sequence[Row]) -> Emission:
    """An encounter, or with ``event_kind: visit_detail`` a stay inside one.

    An end time derived from a length of stay is flagged as derived. The end time is
    not part of the identity unless ``identity_extra_fields`` names it: two rows for
    one admission that disagree on when it ended are one visit with a merge rule to
    settle the end, while two ICU stays that began the same day are told apart by
    their declared length of stay (D-R4).
    """
    out = Emission()
    kind = ctx.spec.event_kind or str(EventKind.visit)
    if kind not in (str(EventKind.visit), str(EventKind.visit_detail)):
        raise ValueError(
            f"the visit shape publishes 'visit' or 'visit_detail' events, not {kind!r}"
        )
    flags_of = flag_resolver(ctx, rows)
    extra_of = merge_extra_resolver(ctx, rows)
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
        flags = flags + flags_of(row)

        los = row.text("length_of_stay", ctx.null_literals)
        if end_time is None and los is not None:
            try:
                days = parse_value(los, None, ctx.values, expect="numeric")
            except QuarantineRow:
                days = None
            if days is not None and days.number is not None:
                end_time = event_time + timedelta(days=days.number)
                flags.append(str(QualityFlag.DERIVED_END_TIME))

        visit_type = row.text("visit_type", ctx.null_literals)
        event_id = event_identity(
            ctx,
            subject_id=row.subject_id,
            event_kind=kind,
            code_system=ctx.spec.code_system,
            source_code=visit_type,
            event_time=event_time,
            encounter_id=row.encounter_source_id,
            extra=identity_extras(ctx, row),
        )
        out.emit(
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
                source_code=visit_type,
                source_name=visit_type,
                value_text=row.text("duration_masked", ctx.null_literals),
                discharged_to=row.text("discharged_to", ctx.null_literals),
                quality_flags=_dedup_flags(flags),
                provenance_status=(
                    ProvenanceStatus.derived
                    if str(QualityFlag.DERIVED_END_TIME) in flags
                    else ProvenanceStatus.observed
                ),
            ),
            row,
            extra_of(row),
        )
        out.link(event_id, row)
        out.extend(_emit_death(ctx, row))
    return out


# --------------------------------------------------------------------------------
# shared emitters
# --------------------------------------------------------------------------------


def _emit_birth_date(ctx: ShapeContext, row: Row) -> Emission:
    """A birth date, when the row carries one.

    ``birth_date`` was a declared field role that nothing consumed: a dataset supplying
    a real date of birth was treated exactly like one supplying nothing, and the strict
    birth policy withheld every patient from PERSON. That is the opposite of what strict
    mode is for -- it exists to publish when the data supports it and refuse when it
    does not, and here the data supported it.

    The date is stored as text rather than a time, because a birth date is an attribute
    of a person and not something that happened during their record. It carries no
    event time for the same reason.
    """
    out = Emission()
    raw = row.raw("birth_date")
    if raw is None:
        return out
    try:
        naive = parse_naive(raw, ctx.time)
    except QuarantineRow as q:
        out.quarantine_row(row, q.issue, q.detail, ctx.source_id)
        return out
    if naive is None:
        return out
    iso = naive.date().isoformat()
    event_id = event_identity(
        ctx,
        subject_id=row.subject_id,
        event_kind=str(EventKind.demographic),
        code_system=ctx.spec.code_system,
        source_code="BIRTH_DATE",
        event_time=None,
        encounter_id=None,
        value=None,
    )
    out.emit(
        make_event(
            ctx,
            row,
            event_id=event_id,
            event_kind=str(EventKind.demographic),
            event_time=None,
            code_system=ctx.spec.code_system,
            source_code="BIRTH_DATE",
            source_name="birth date",
            value_text=iso,
        ),
        row,
    )
    out.link(event_id, row)
    return out


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
    out.emit(
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
        ),
        row,
    )
    out.link(event_id, row)
    return out


def _emit_vital_status(ctx: ShapeContext, row: Row, *, timed: bool) -> Emission:
    """A person's vital status, which is an outcome and not a baseline attribute.

    The source states it as a static column, and it was published as one: a timeless
    ``demographic`` event, which an as-of view returns whatever cutoff it is asked for.
    Every history a record can produce therefore carried the patient's final status,
    including the histories that end long before they died.

    There is no honest time to give it here. When the row carries a death date the
    ``death`` event above already states the same fact at a real time, so the status is
    redundant and dropped. When it does not, the status is withheld rather than
    published: quarantined with its source row, the way an untimed measurement is. That
    also catches the rows whose death date failed to parse, which until now produced a
    timeless "deceased" and no death event at all.

    Deciding this on whether a death event was emitted, rather than on what the status
    column says, keeps the core free of any particular export's word for "alive".
    """
    out = Emission()
    if timed:
        return out
    text = row.text("vital_status", ctx.null_literals)
    if text is None:
        return out
    out.quarantine_row(
        row,
        str(QuarantineReason.UNTIMED_VITAL_STATUS),
        f"vital status {text[:32]!r} has no time in the source and no death date to place it",
        ctx.source_id,
    )
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
    out.emit(
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
        ),
        row,
    )
    for i, r in enumerate(rows):
        out.link(event_id, r, SourceRelation.derived_from if i == 0 else SourceRelation.duplicate_of)
    return out


GroupKey = tuple[int, str | None, str | None, datetime | None, datetime | None]


def _group(
    ctx: ShapeContext, rows: Sequence[Row], out: Emission
) -> Iterator[tuple[tuple[GroupKey, tuple[str, ...]], list[Row]]]:
    """Group rows into the report or study they belong to.

    The grouping key is the clinical identity of the study -- subject, encounter, study
    name, result time -- and deliberately not the anchor, which is what makes the
    once-per-anchor duplication collapse.

    Quality flags are unioned across the group rather than being part of the key. One
    row of a report earning a fallback flag must not split the report in two; the flag
    describes the group, not a different study.
    """
    groups: dict[GroupKey, list[Row]] = {}
    group_flags: dict[GroupKey, set[str]] = {}
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
        key: GroupKey = (row.subject_id, row.encounter_source_id, title, event_time, available_time)
        groups.setdefault(key, []).append(row)
        group_flags.setdefault(key, set()).update(flags)
    for key in sorted(groups, key=_group_sort_key):
        yield (key, tuple(sorted(group_flags[key]))), groups[key]


def _group_sort_key(key: GroupKey) -> tuple:
    subject_id, encounter_id, title, event_time, available_time = key
    return (
        subject_id,
        encounter_id or "",
        title or "",
        event_time or datetime.min,
        available_time or datetime.min,
    )


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _expect_for(ctx: ShapeContext) -> str:
    """What this source's result column is expected to hold.

    ``auto`` accepts free text, which is correct for a column that genuinely mixes
    numbers and phrases. A source that declares ``numeric`` gets the stricter rule the
    design asks for: a value matching none of the documented forms is quarantined
    rather than quietly kept as text.
    """
    return ctx.spec.value_expect


def get_shape(name: str) -> Callable[[ShapeContext, Sequence[Row]], Emission]:
    from ehr2trace.registry import SHAPES

    shape = SHAPES.get(name)
    if shape is None:
        raise KeyError(f"unregistered shape {name!r}")
    return shape  # type: ignore[return-value]
