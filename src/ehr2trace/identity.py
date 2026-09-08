"""Cross-partition patient identity (design section 5.1, checklist P1-3).

One patient key means one subject, across every partition and every extraction batch.
This is the pipeline's only true barrier: deduplication cannot start until the whole
dataset agrees on who is who, because assigning ids per partition would turn one
patient appearing in two batches into two patients and silently double the cohort.

The mapping table is the only artefact that can turn a subject id back into a patient.
It lives under the work root with owner-only permissions and is never committed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import polars as pl
import pyarrow as pa

from ehr2trace.config import DatasetConfig
from ehr2trace.hashing import bucket_of, subject_id_from_person_key
from ehr2trace.paths import WorkLayout, write_table_atomic
from ehr2trace.schema import SUBJECT_MAP_SCHEMA


@dataclass
class IdentityResult:
    subjects: int
    person_keys: int
    per_partition: dict[str, int]
    collisions: list[str]
    path: str


def source_files(layout: WorkLayout) -> list[Path]:
    return sorted(p for p in layout.source_dir.rglob("*.parquet") if not p.name.endswith(".partial"))


def build_identity(cfg: DatasetConfig, layout: WorkLayout) -> IdentityResult:
    """Assign a subject id to every patient key seen anywhere in the dataset."""
    files = source_files(layout)
    if not files:
        raise RuntimeError("no source parquet found; run ingest first")

    # Read file by file rather than as one scan: each source has its own set of
    # original columns, so the source layer is deliberately not one uniform schema.
    # Only the two lineage columns are read, and each file is reduced to its distinct
    # pairs before anything is accumulated.
    by_person: dict[str, set[str]] = {}
    for path in files:
        pairs = (
            pl.read_parquet(path, columns=["person_source_id", "partition_id"])
            .filter(pl.col("person_source_id").is_not_null() & (pl.col("person_source_id") != ""))
            .unique()
        )
        for person, partition in zip(
            pairs["person_source_id"].to_list(), pairs["partition_id"].to_list()
        ):
            by_person.setdefault(person, set()).add(partition)

    salt = cfg.subject_salt()
    bucket_count = cfg.execution.bucket_count
    rows: list[dict] = []
    seen_ids: dict[int, str] = {}
    collisions: list[str] = []
    for person in sorted(by_person):
        subject_id = subject_id_from_person_key(cfg.dataset_id, person, salt)
        previous = seen_ids.get(subject_id)
        if previous is not None and previous != person:
            # Two patient keys hashing to one id would silently merge two people.
            # Refuse rather than continue; 63 bits over this population makes it
            # effectively impossible, so it would mean something else is wrong.
            collisions.append(f"{previous} / {person}")
            continue
        seen_ids[subject_id] = person
        rows.append(
            {
                "subject_id": subject_id,
                "person_source_id": person,
                "bucket": bucket_of(subject_id, bucket_count),
                "partitions": sorted(by_person[person]),
            }
        )

    if collisions:
        raise RuntimeError(f"subject id collisions detected: {collisions[:5]}")

    table = pa.table(
        {f.name: [r[f.name] for r in rows] for f in SUBJECT_MAP_SCHEMA}, schema=SUBJECT_MAP_SCHEMA
    )
    layout.identity_dir.mkdir(parents=True, exist_ok=True)
    path = layout.identity_dir / "subject_map.parquet"
    write_table_atomic(table, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    per_partition: dict[str, int] = {}
    for r in rows:
        for part in r["partitions"]:
            per_partition[part] = per_partition.get(part, 0) + 1

    summary = IdentityResult(
        subjects=len(rows),
        person_keys=len(by_person),
        per_partition=dict(sorted(per_partition.items())),
        collisions=collisions,
        path=str(path),
    )
    # The summary carries counts only -- no patient keys -- so it is safe to keep next
    # to the other run artefacts.
    (layout.identity_dir / "summary.json").write_text(
        json.dumps(
            {
                "subjects": summary.subjects,
                "person_keys": summary.person_keys,
                "per_partition": summary.per_partition,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return summary


def load_subject_map(layout: WorkLayout) -> pl.DataFrame:
    path = layout.identity_dir / "subject_map.parquet"
    if not path.exists():
        raise RuntimeError(f"identity not built yet: {path} missing")
    return pl.read_parquet(path)


def subject_lookup(layout: WorkLayout) -> dict[str, int]:
    df = load_subject_map(layout)
    return dict(zip(df["person_source_id"].to_list(), df["subject_id"].to_list()))


def partition_overlap(layout: WorkLayout, partitions: Iterable[str]) -> dict[str, int]:
    """Subject counts per partition and for the union -- the cross-partition check."""
    df = load_subject_map(layout)
    out: dict[str, int] = {}
    for part in partitions:
        out[part] = int(df.filter(pl.col("partitions").list.contains(part)).height)
    out["__union__"] = int(df.height)
    return out
