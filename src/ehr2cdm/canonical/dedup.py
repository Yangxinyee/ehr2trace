"""Deduplication and deterministic merging (design sections 3.3, 5.2).

The same clinical fact is extracted more than once: once per anchor within a partition,
and again in the other extraction batch. All of those rows describe one event, so they
collapse to a single ``CanonicalEvent`` while every source row stays linked.

Determinism is the point of this module. Events arrive from a process pool in whatever
order tasks happened to finish, so every merge sorts first and every choice between
equal candidates is made by a stable key rather than by arrival order.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ehr2cdm.schema import QualityFlag, SourceRelation


def merge_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse events sharing an ``event_id``.

    Same id means same clinical content by construction, so merging is limited to the
    fields that legitimately differ between extracts: quality flags are unioned, and an
    event seen through more than one source row keeps the union of what each observed.
    """
    merged: dict[str, dict[str, Any]] = {}
    for event in events:
        eid = event["event_id"]
        current = merged.get(eid)
        if current is None:
            row = dict(event)
            row["quality_flags"] = sorted(set(row.get("quality_flags") or []))
            merged[eid] = row
            continue
        current["quality_flags"] = sorted(
            set(current["quality_flags"]) | set(event.get("quality_flags") or [])
        )
        for key, value in event.items():
            if key == "quality_flags":
                continue
            if current.get(key) is None and value is not None:
                current[key] = value
    return [merged[k] for k in sorted(merged)]


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
