"""Extraction anchors (design section 2.3, 5.2).

An anchor is the reference event an extract was built around -- rows were pulled
because they were near it, and the whole table is repeated once per anchor. It is
provenance, and it is not a clinical event:

* it never becomes an ``event_time``;
* it never becomes a procedure, however tempting the column name is;
* the "closest to" column is a **rank**, not a day difference, so it is left in the
  source layer and any day difference is recomputed from real timestamps.

One batch ships anchors as full timestamps and another as bare dates, so anchors are
compared at date granularity and ``anchor_time_known`` records which is which.
"""

from __future__ import annotations

from typing import Any, Sequence

from ehr2trace.canonical.normalize import Row, ShapeContext
from ehr2trace.errors import QuarantineRow
from ehr2trace.hashing import stable_id
from ehr2trace.schema import QualityFlag
from ehr2trace.timeutil import looks_date_only, parse_naive, to_utc


def anchor_identity(dataset_id: str, subject_id: int, anchor_type: str, anchor_date, partition_id: str) -> str:
    """Anchor key = ``(anchor_date, partition_id)`` per subject.

    Partition is part of the key on purpose: the same calendar date appearing in two
    partitions is two extraction facts, and collapsing them would erase the evidence
    that a patient's episodes were split across differently-labelled cohorts.
    """
    return stable_id(dataset_id, subject_id, anchor_type, anchor_date, partition_id, prefix="anchor/")


def emit_anchors(ctx: ShapeContext, rows: Sequence[Row]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Anchor rows and cohort-membership rows for one source's rows.

    Both tables are deliberately separate from events: a directory label is not a
    diagnosis, and an extraction anchor is not a procedure.
    """
    if ctx.cfg.anchors is None:
        return [], []
    anchors: dict[str, dict[str, Any]] = {}
    memberships: dict[tuple, dict[str, Any]] = {}
    label_scope = _label_scope(ctx)

    for row in rows:
        raw = row.raw("anchor_time")
        anchor_id = None
        if raw is not None:
            try:
                naive = parse_naive(raw, ctx.time)
            except QuarantineRow:
                naive = None
            if naive is not None:
                time_known = not looks_date_only(raw)
                utc, _flags = to_utc(naive, ctx.time)
                anchor_date = (utc or naive).date()
                anchor_id = anchor_identity(
                    ctx.cfg.dataset_id, row.subject_id, ctx.cfg.anchors.anchor_type, anchor_date, row.partition_id
                )
                existing = anchors.get(anchor_id)
                if existing is None:
                    anchors[anchor_id] = {
                        "anchor_id": anchor_id,
                        "subject_id": row.subject_id,
                        "anchor_type": ctx.cfg.anchors.anchor_type,
                        "anchor_date": anchor_date,
                        "anchor_time": utc if time_known else None,
                        "anchor_time_known": time_known,
                        "partition_id": row.partition_id,
                        "source_row_id": row.source_row_id,
                    }
                elif time_known and not existing["anchor_time_known"]:
                    # A batch that carries the time upgrades one that does not; the
                    # date, which is the key, is unchanged either way.
                    existing["anchor_time"] = utc
                    existing["anchor_time_known"] = True

        if row.membership_label is not None:
            key = (row.subject_id, row.partition_id, anchor_id)
            memberships.setdefault(
                key,
                {
                    "subject_id": row.subject_id,
                    "partition_id": row.partition_id,
                    "batch": row.batch,
                    "membership_label": row.membership_label,
                    "anchor_id": anchor_id,
                    "label_scope": label_scope,
                    "source_row_id": row.source_row_id,
                },
            )

    return list(anchors.values()), list(memberships.values())


def _label_scope(ctx: ShapeContext) -> str:
    """Scope of the cohort label, straight from the config.

    ``unknown`` is the honest default and stays until the data owner supplies the rule:
    a label whose scope nobody has defined must not be published as patient-level.
    """
    for label in ctx.cfg.labels:
        if label.from_ == "membership_label":
            return label.scope
    return "unknown"


def anchor_flags(anchor: dict[str, Any]) -> list[str]:
    return [] if anchor.get("anchor_time_known") else [str(QualityFlag.ANCHOR_TIME_UNKNOWN)]
