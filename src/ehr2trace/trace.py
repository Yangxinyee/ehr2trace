"""Answer "where did this record come from?" for one patient (design section 9.2).

The design's third success criterion is that anyone can answer, for any published row,
where it came from and why it was mapped that way. Everything needed is already in the
lineage tables; this module is the shortest path from a patient key to that answer.

It is a debugging and fixture-generation aid: it reads, never writes, and it deals in
one patient at a time. Terminology still comes from the global ``mappings/``, so what
you see here is exactly what a full run would produce for that patient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from ehr2trace.config import DatasetConfig
from ehr2trace.hashing import subject_id_from_person_key
from ehr2trace.paths import WorkLayout


@dataclass
class PatientTrace:
    person_source_id: str
    subject_id: int
    partitions: list[str] = field(default_factory=list)
    source_rows: dict[str, int] = field(default_factory=dict)
    quarantined: dict[str, int] = field(default_factory=dict)
    events: dict[str, int] = field(default_factory=dict)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    memberships: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, int] = field(default_factory=dict)
    sample: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        # The patient key is the one thing here that identifies a person. It is echoed
        # back only because the caller just typed it.
        return payload


def trace_patient(
    cfg: DatasetConfig, layout: WorkLayout, person_source_id: str, samples: int = 3
) -> PatientTrace:
    """Everything the pipeline knows about one patient, layer by layer."""
    subject_id = subject_id_from_person_key(cfg.dataset_id, person_source_id, cfg.subject_salt())
    trace = PatientTrace(person_source_id=person_source_id, subject_id=subject_id)

    subject_map = layout.identity_dir / "subject_map.parquet"
    if subject_map.exists():
        row = pl.read_parquet(subject_map).filter(pl.col("person_source_id") == person_source_id)
        if row.height:
            trace.partitions = sorted(row["partitions"][0].to_list())
            trace.subject_id = int(row["subject_id"][0])

    for path in sorted(layout.source_dir.rglob("*.parquet")):
        frame = pl.read_parquet(path, columns=["person_source_id", "partition_id", "source_id"])
        mine = frame.filter(pl.col("person_source_id") == person_source_id)
        if mine.height:
            key = f"{mine['partition_id'][0]}/{mine['source_id'][0]}"
            trace.source_rows[key] = trace.source_rows.get(key, 0) + mine.height

    quarantine = layout.canonical_path("quarantine")
    if quarantine.exists():
        frame = pl.read_parquet(quarantine, columns=["person_source_id", "source_id", "reason"])
        mine = frame.filter(pl.col("person_source_id") == person_source_id)
        for row in mine.group_by(["source_id", "reason"]).len().iter_rows(named=True):
            trace.quarantined[f"{row['source_id']}: {row['reason']}"] = row["len"]

    events_path = layout.canonical_path("events")
    if events_path.exists():
        events = pl.read_parquet(events_path).filter(pl.col("subject_id") == trace.subject_id)
        counts = events.group_by("event_kind").len()
        trace.events = dict(zip(counts["event_kind"].to_list(), counts["len"].to_list()))
        flat = [flag for flags in events["quality_flags"].to_list() for flag in (flags or [])]
        for flag in flat:
            trace.flags[flag] = trace.flags.get(flag, 0) + 1
        trace.sample = _sample_with_lineage(layout, events, samples)

    for name, target in (("anchors", trace.anchors), ("cohort_membership", trace.memberships)):
        path = layout.canonical_path(name)
        if path.exists():
            frame = pl.read_parquet(path).filter(pl.col("subject_id") == trace.subject_id)
            target.extend(frame.to_dicts())
    return trace


def _sample_with_lineage(layout: WorkLayout, events: pl.DataFrame, samples: int) -> list[dict[str, Any]]:
    """A few events followed all the way back to a file and a row number."""
    if events.height == 0 or samples <= 0:
        return []
    links_path = layout.canonical_path("event_source")
    if not links_path.exists():
        return []
    chosen = events.sort("event_id").head(samples)
    links = pl.read_parquet(links_path).join(chosen.select("event_id"), on="event_id")

    origins: dict[str, list[dict[str, Any]]] = {}
    wanted = set(links["source_row_id"].to_list())
    if wanted:
        for path in sorted(layout.source_dir.rglob("*.parquet")):
            frame = pl.read_parquet(
                path, columns=["source_row_id", "source_file", "source_sheet", "source_row_number"]
            ).filter(pl.col("source_row_id").is_in(list(wanted)))
            for row in frame.iter_rows(named=True):
                origins[row["source_row_id"]] = row

    out: list[dict[str, Any]] = []
    for event in chosen.iter_rows(named=True):
        rows = links.filter(pl.col("event_id") == event["event_id"])
        out.append(
            {
                "event_id": event["event_id"],
                "event_kind": event["event_kind"],
                "event_time": event["event_time"],
                "code": f"{event['code_system']}/{event['source_code']}",
                "value": event["value_number"] if event["value_number"] is not None else event["value_text"],
                "quality_flags": event["quality_flags"],
                "source_rows": [
                    {
                        "relation": link["relation"],
                        "partition": link["partition_id"],
                        "file": Path(origins.get(link["source_row_id"], {}).get("source_file", "?")).name,
                        "sheet": origins.get(link["source_row_id"], {}).get("source_sheet"),
                        "row": origins.get(link["source_row_id"], {}).get("source_row_number"),
                    }
                    for link in rows.iter_rows(named=True)
                ][:8],
                "source_row_count": rows.height,
            }
        )
    return out


def render(trace: PatientTrace) -> str:
    """A readable rendering. Prints identifiers, so it belongs on a trusted terminal."""
    lines = [
        f"patient    {trace.person_source_id}  ->  subject {trace.subject_id}",
        f"partitions {', '.join(trace.partitions) or '(none)'}",
        "",
        "source rows",
    ]
    for key in sorted(trace.source_rows):
        lines.append(f"  {key:<28} {trace.source_rows[key]:>8,}")
    if trace.quarantined:
        lines += ["", "quarantined"]
        for key in sorted(trace.quarantined):
            lines.append(f"  {key:<40} {trace.quarantined[key]:>6,}")
    lines += ["", "canonical events"]
    for key in sorted(trace.events):
        lines.append(f"  {key:<28} {trace.events[key]:>8,}")
    if trace.flags:
        lines += ["", "quality flags"]
        for key in sorted(trace.flags):
            lines.append(f"  {key:<40} {trace.flags[key]:>6,}")
    if trace.anchors:
        lines += ["", f"anchors ({len(trace.anchors)})"]
        for anchor in sorted(trace.anchors, key=lambda a: (str(a['anchor_date']), a['partition_id'])):
            known = "with time" if anchor["anchor_time_known"] else "date only"
            lines.append(f"  {anchor['anchor_date']}  {anchor['partition_id']:<10} {known}")
    if trace.memberships:
        lines += ["", "cohort membership (provenance, not a clinical fact)"]
        # One row per episode, which is the point: a label that binds to an anchor
        # rather than to a lifetime. Grouped for reading, never merged in the data.
        grouped: dict[tuple[str, str, str], int] = {}
        for m in trace.memberships:
            key = (m["partition_id"], m["membership_label"], m["label_scope"])
            grouped[key] = grouped.get(key, 0) + 1
        for (partition, label, scope) in sorted(grouped):
            episodes = grouped[(partition, label, scope)]
            lines.append(
                f"  {partition:<10} label={label:<4} scope={scope:<8} {episodes} episode(s)"
            )
        if len({label for _p, label, _s in grouped}) > 1:
            lines.append(
                "  ^ this patient carries more than one cohort label. Both are kept, "
                "unmerged: the label is episode-level."
            )
    if trace.sample:
        lines += ["", "sample events, traced back to the raw file"]
        for event in trace.sample:
            lines.append(f"  {event['event_kind']} {event['event_time']} {event['code']} = {event['value']}")
            if event["quality_flags"]:
                lines.append(f"      flags: {', '.join(event['quality_flags'])}")
            for origin in event["source_rows"]:
                sheet = f"::{origin['sheet']}" if origin["sheet"] else ""
                lines.append(
                    f"      {origin['relation']:<13} {origin['partition']:<10} "
                    f"{origin['file']}{sheet} row {origin['row']}"
                )
            if event["source_row_count"] > len(event["source_rows"]):
                lines.append(f"      ... and {event['source_row_count'] - len(event['source_rows'])} more source rows")
    return "\n".join(lines)
