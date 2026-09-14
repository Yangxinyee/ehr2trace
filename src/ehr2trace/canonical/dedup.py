"""Deduplication and deterministic merging (design sections 3.3, 5.2; remediation T1.2, T1.3).

The same clinical fact is extracted more than once: once per anchor within a partition,
and again in the other extraction batch. All of those rows describe one event, so they
collapse to a single ``CanonicalEvent`` while every source row stays linked.

Same id does not mean the rows agree on every column of the fact. Two billings of one
procedure differ in who billed; a lab result repeated in two batches differs in when it
became visible; a note filed under several encounter ids differs in the encounter. The
audit of 2026-09-13 found the merge keeping whichever value it met first (P-C2). Now a
disagreement is settled by the rule the dataset declared for that field
(:class:`ehr2trace.config.MergeRuleSpec`) or, with no rule, recorded as a
``MERGE_CONFLICT`` that validation reports -- never by arrival order.

Determinism is the point of this module. Events arrive from a process pool in whatever
order tasks happened to finish, so every merge sorts first and every choice between
equal candidates is made by a stable key rather than by arrival order.

The payload
-----------

Each pre-merge event dict carries, under :data:`INSTANCE_KEY`, an :class:`Instance`: the
source row it came from and the values of the roles and kept columns its source's merge
rules name (canonical fields need no copy -- the event dict *is* the instance's values).
The shapes attach it through ``Emission.emit`` and :func:`merge_events` strips it, so
nothing downstream of the merge ever sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from ehr2trace.config import MergeRuleSpec
from ehr2trace.schema import CANONICAL_EVENT_SCHEMA, QualityFlag, QuarantineReason, SourceRelation

#: the transient key under which a pre-merge event dict carries its :class:`Instance`
INSTANCE_KEY = "_instance"

#: cell values that mean "yes" in a marker column (``flag_when``, ``linked_by``)
TRUTHY_CELLS: frozenset[str] = frozenset({"1", "true", "yes", "y", "t"})


def is_truthy_cell(value: object) -> bool:
    return value is not None and str(value).strip().lower() in TRUTHY_CELLS


@dataclass(slots=True)
class Instance:
    """What one source row contributed to an event, beyond the event's own fields."""

    source_row_id: str
    partition_id: str
    #: role name or kept column -> the row's value, for the names the source's merge
    #: rules compare or read (``linked_by``); None when the source declares no such rule
    extra: dict[str, Any] | None = None


#: Canonical fields whose disagreement between merged rows is a fact about the data.
#: The rest of the row is bookkeeping -- ids, versions, links, flags -- and follows
#: the survivor.
_BOOKKEEPING: frozenset[str] = frozenset({
    "event_id", "subject_id", "source_id", "mapping_version", "quality_flags",
    "provenance_status", "parent_event_id", "caused_by_event_id",
})
COMPARED_FIELDS: tuple[str, ...] = tuple(
    f.name for f in CANONICAL_EVENT_SCHEMA if f.name not in _BOOKKEEPING
)

#: A rule keyed by a field role applies to the canonical field that role feeds, so a
#: YAML may say ``merge_rules: {status: priority}`` about the column it declared as
#: ``status`` without knowing the event stores it as ``status_source``.
ROLE_TO_FIELD: dict[str, str] = {
    "status": "status_source",
    "route": "route_source",
    "dose": "dose_source",
    "unit": "unit_source",
    "rate": "rate_source",
    "text": "value_text",
}
FIELD_TO_ROLE: dict[str, str] = {v: k for k, v in ROLE_TO_FIELD.items()}


def rule_field(name: str) -> str | None:
    """The canonical field a merge-rule key addresses, or None for a role or column
    that only the instance payload carries."""
    if name in COMPARED_FIELDS:
        return name
    return ROLE_TO_FIELD.get(name)


@dataclass
class MergeResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)


def merge_events(
    events: Iterable[dict[str, Any]],
    rules_by_source: Mapping[str, Mapping[str, MergeRuleSpec]] | None = None,
    dataset_id: str | None = None,
) -> MergeResult:
    """Collapse events sharing an ``event_id``, settling disagreements by rule.

    ``rules_by_source`` maps a source id to that source's ``merge_rules``; the rules of
    the surviving instance's source apply to the group. Quality flags are unioned. A
    field null on the survivor and set on another instance is filled -- absence is not
    a disagreement. Two different non-null values are, and are resolved as the rule for
    that key says; ``available_time`` with no rule keeps the earliest and is flagged
    ``AVAILABILITY_MERGED``; any other field with no rule keeps the survivor's value,
    flags the event ``MERGE_CONFLICT`` and records a quality issue naming the field
    and how many values it saw (never the values: a text field may be a note).

    The survivor is the instance with the smallest ``source_row_id``, so the result is
    the same whatever order the rows arrived in.
    """
    rules_by_source = rules_by_source or {}
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        groups.setdefault(event["event_id"], []).append(event)

    result = MergeResult()
    for event_id in sorted(groups):
        instances = sorted(groups[event_id], key=_instance_order)
        if len(instances) == 1:
            survivor = dict(instances[0])
            survivor.pop(INSTANCE_KEY, None)
            survivor["quality_flags"] = sorted(set(survivor.get("quality_flags") or []))
            result.events.append(survivor)
            continue
        survivor = _merge_group(event_id, instances, rules_by_source, dataset_id, result)
        result.events.append(survivor)
    return result


def _instance_order(event: dict[str, Any]) -> tuple[str, str]:
    instance = event.get(INSTANCE_KEY)
    if instance is None:
        return ("", "")
    return (instance.source_row_id, instance.partition_id)


def _merge_group(
    event_id: str,
    instances: Sequence[dict[str, Any]],
    rules_by_source: Mapping[str, Mapping[str, MergeRuleSpec]],
    dataset_id: str | None,
    result: MergeResult,
) -> dict[str, Any]:
    survivor = dict(instances[0])
    survivor.pop(INSTANCE_KEY, None)
    flags: set[str] = set()
    for instance in instances:
        flags.update(instance.get("quality_flags") or [])
    rules = rules_by_source.get(survivor.get("source_id") or "", {})

    for name in COMPARED_FIELDS:
        present = [(e.get(name), e) for e in instances if e.get(name) is not None]
        if not present:
            continue
        if survivor.get(name) is None:
            survivor[name] = present[0][0]
        if len(_distinct(v for v, _ in present)) <= 1:
            continue
        rule = rules.get(name)
        if rule is None and name in FIELD_TO_ROLE:
            rule = rules.get(FIELD_TO_ROLE[name])
        value, new_flags = _resolve(event_id, name, rule, present, survivor, instances, dataset_id, result)
        survivor[name] = value
        flags.update(new_flags)

    # Roles and kept columns the rules name are compared from the instance payload.
    # Nothing on the event changes for them; only the flags, issues and quarantine do.
    extra_names: set[str] = set()
    for e in instances:
        instance = e.get(INSTANCE_KEY)
        if instance is not None and instance.extra:
            extra_names.update(instance.extra)
    for name in sorted(extra_names):
        rule = rules.get(name)
        if rule is None or rule_field(name) is not None:
            continue
        present = []
        for e in instances:
            instance = e.get(INSTANCE_KEY)
            value = instance.extra.get(name) if instance is not None and instance.extra else None
            if value is not None:
                present.append((value, e))
        if len(_distinct(v for v, _ in present)) <= 1:
            continue
        _value, new_flags = _resolve(event_id, name, rule, present, survivor, instances, dataset_id, result)
        flags.update(new_flags)

    survivor["quality_flags"] = sorted(flags)
    return survivor


def _distinct(values: Iterable[Any]) -> list[Any]:
    seen: list[Any] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def _resolve(
    event_id: str,
    name: str,
    rule: MergeRuleSpec | None,
    present: Sequence[tuple[Any, dict[str, Any]]],
    survivor: dict[str, Any],
    instances: Sequence[dict[str, Any]],
    dataset_id: str | None,
    result: MergeResult,
) -> tuple[Any, set[str]]:
    """One disagreeing field -> the value the survivor keeps and the flags it earns."""
    current = survivor.get(name)
    distinct = _distinct(v for v, _ in present)

    if rule is None:
        if name == "available_time":
            # D-R5: the earliest time a result was visible is the conservative one for
            # a field whose job is to keep the future out of a training window.
            return min(distinct), {str(QualityFlag.AVAILABILITY_MERGED)}
        _conflict(event_id, name, len(distinct), survivor, instances, result)
        return current, {str(QualityFlag.MERGE_CONFLICT)}

    if rule.rule == "earliest":
        return min(distinct), {rule.flag_name}
    if rule.rule == "latest":
        return max(distinct), {rule.flag_name}
    if rule.rule == "null_and_flag":
        for value, event in present:
            instance = event.get(INSTANCE_KEY)
            result.quarantine.append({
                "source_row_id": instance.source_row_id if instance else None,
                "dataset_id": dataset_id,
                "partition_id": instance.partition_id if instance else None,
                "source_id": event.get("source_id"),
                "source_file": None,
                "source_sheet": None,
                "source_row_number": None,
                "stage": "canonical",
                "reason": str(QuarantineReason.VALUE_CONFLICT),
                "detail": f"{name}={_text(value)}",
                "person_source_id": None,
                "raw_row": None,
            })
        return None, {rule.flag_name}
    if rule.rule == "priority":
        lowered = {_key(v): v for v in reversed(distinct)}  # first spelling wins for equal keys
        flags: set[str] = set()
        for a, b in rule.conflict_when:
            if _key(a) in lowered and _key(b) in lowered:
                flags.add(rule.flag_name)
        for wanted in rule.order:
            if _key(wanted) in lowered:
                return lowered[_key(wanted)], flags
        # None of the declared values is present: the rule cannot decide, and an
        # undeclared disagreement is what MERGE_CONFLICT is for.
        _conflict(event_id, name, len(distinct), survivor, instances, result,
                  note="none of the values is in the declared priority order")
        return current, flags | {str(QualityFlag.MERGE_CONFLICT)}
    if rule.rule == "prefer_linked":
        linked = []
        for value, event in present:
            instance = event.get(INSTANCE_KEY)
            marker = instance.extra.get(rule.linked_by) if instance is not None and instance.extra else None
            if is_truthy_cell(marker):
                linked.append(value)
        linked_values = _distinct(linked)
        if len(linked_values) == 1:
            return linked_values[0], {str(QualityFlag.ENCOUNTER_FROM_LINKED_ROW)}
        return None, {rule.flag_name}
    if rule.rule == "keep_all_flag":
        return current, {rule.flag_name}
    raise ValueError(f"unknown merge rule {rule.rule!r}")  # unreachable: the config validates the name


def _conflict(
    event_id: str,
    name: str,
    distinct: int,
    survivor: dict[str, Any],
    instances: Sequence[dict[str, Any]],
    result: MergeResult,
    note: str = "",
) -> None:
    detail = f"{name}: {distinct} distinct values across {len(instances)} merged rows"
    if note:
        detail = f"{detail}; {note}"
    result.issues.append({
        "issue_type": str(QualityFlag.MERGE_CONFLICT),
        "severity": "error",
        "stage": "canonical",
        "subject_id": survivor.get("subject_id"),
        "source_row_id": None,
        "event_id": event_id,
        "partition_id": None,
        "source_id": survivor.get("source_id"),
        "detail": detail,
    })


def _key(value: Any) -> str:
    return str(value).strip().lower()


def _text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def merge_links(links: Iterable[dict[str, Any]], cross_partition_flag: bool = True) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """Deduplicate event/source links and assign relations deterministically.

    Which of several identical source rows counts as *the* origin is arbitrary, so it
    is decided by sorting rather than by which worker finished first: the smallest
    ``source_row_id`` is ``derived_from`` and the rest are ``duplicate_of``. The result
    is byte-identical whatever the worker count.
    """
    by_event: dict[str, dict[str, dict[str, Any]]] = {}
    for link in links:
        by_event.setdefault(link["event_id"], {})[link["source_row_id"]] = dict(link)

    out: list[dict[str, Any]] = []
    partitions: dict[str, set[str]] = {}
    for event_id in sorted(by_event):
        rows = by_event[event_id]
        partitions[event_id] = {r.get("partition_id") for r in rows.values() if r.get("partition_id")}
        for i, source_row_id in enumerate(sorted(rows)):
            row = rows[source_row_id]
            if row.get("relation") != str(SourceRelation.anchored_to):
                row["relation"] = str(
                    SourceRelation.derived_from if i == 0 else SourceRelation.duplicate_of
                )
            out.append(row)
    return out, partitions


def apply_duplicate_flags(events: Sequence[dict[str, Any]], partitions: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Flag events whose source rows span more than one partition.

    Not a defect -- it is the expected consequence of two extraction batches -- but it
    is the fact that proves cross-batch deduplication actually happened, so it is
    recorded rather than inferred.
    """
    out: list[dict[str, Any]] = []
    for event in events:
        parts = partitions.get(event["event_id"], set())
        row = dict(event)
        if len(parts) > 1:
            row["quality_flags"] = sorted(
                set(row.get("quality_flags") or []) | {str(QualityFlag.DUPLICATE_ACROSS_PARTITIONS)}
            )
        out.append(row)
    return out


def merge_anchors(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse anchors on ``(subject, type, date, partition)``, keeping the best time.

    Within one partition the same anchor date can arrive twice: one source writes it as
    a full timestamp and another as a bare date. They are the same anchor, and the
    version that still has its time component is the one worth keeping -- a generic
    "first wins" or "smallest wins" rule would throw the time away roughly half the
    time, silently.
    """
    best: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        key = (
            rec.get("subject_id"),
            rec.get("anchor_type"),
            rec.get("anchor_date"),
            rec.get("partition_id"),
        )
        rank = (not bool(rec.get("anchor_time_known")), str(rec.get("source_row_id") or ""))
        current = best.get(key)
        if current is None or rank < current[0]:
            best[key] = (rank, dict(rec))
    return [best[k][1] for k in sorted(best, key=lambda k: tuple(_sortable(v) for v in k))]


def dedup_records(records: Iterable[dict[str, Any]], key_fields: Sequence[str]) -> list[dict[str, Any]]:
    """Generic deduplication for anchors, memberships and quality issues.

    Records sharing a key usually differ in which source row they happened to be built
    from. Keeping "the first one seen" would make the output depend on task completion
    order, so the survivor is chosen by a total order over the record's own values.
    """
    seen: dict[tuple, tuple[tuple, dict[str, Any]]] = {}
    for rec in records:
        key = tuple(_sortable(rec.get(f)) for f in key_fields)
        rank = tuple(str(_sortable(v)) for _k, v in sorted(rec.items()))
        current = seen.get(key)
        if current is None or rank < current[0]:
            seen[key] = (rank, dict(rec))
    return [seen[k][1] for k in sorted(seen)]


def _sortable(value: Any) -> Any:
    return "" if value is None else value


def sort_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable publication order: by subject, then time, then event id."""
    return sorted(
        events,
        key=lambda e: (
            e.get("subject_id") or 0,
            e.get("event_time") is not None,
            e.get("event_time") or 0,
            e.get("event_id") or "",
        ),
    )
