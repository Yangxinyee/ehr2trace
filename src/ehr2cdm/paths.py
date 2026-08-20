"""Work-root layout, content addressing and atomic writes (design section 3.3).

Resumption needs no ledger. A task's output path embeds a hash of everything that could
change its bytes -- the input file hashes, the config hash, the code version -- so an
existing file with that name *is* the answer, and a crashed run leaves ``.partial``
files that no merge ever sees.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

PARTIAL_SUFFIX = ".partial"


def task_hash(*parts: object) -> str:
    """Content address of a task: same inputs, same name, same bytes."""
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class WorkLayout:
    """Where everything a run produces lives. Never inside the repository."""

    root: Path
    dataset_id: str

    @classmethod
    def from_env(cls, dataset_id: str, env: str = "EHR_WORK_ROOT") -> "WorkLayout":
        raw = os.environ.get(env)
        if not raw:
            raise RuntimeError(
                f"{env} is not set; it must point at a writable directory outside the "
                "repository (outputs derive from PHI)"
            )
        return cls(root=Path(raw).expanduser() / dataset_id, dataset_id=dataset_id)

    # -- layers ---------------------------------------------------------------

    @property
    def manifest_dir(self) -> Path:
        return self.root / "manifest"

    @property
    def source_dir(self) -> Path:
        return self.root / "source"

    @property
    def staged_dir(self) -> Path:
        return self.root / "staged"

    @property
    def identity_dir(self) -> Path:
        return self.root / "identity"

    @property
    def canonical_dir(self) -> Path:
        return self.root / "canonical"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "quarantine"

    @property
    def omop_dir(self) -> Path:
        return self.root / "omop"

    @property
    def meds_dir(self) -> Path:
        return self.root / "meds"

    @property
    def review_dir(self) -> Path:
        return self.root / "review"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    def source_task_path(self, partition_id: str, source_id: str, digest: str) -> Path:
        return self.source_dir / partition_id / source_id / f"{digest}.parquet"

    def quarantine_task_path(self, stage: str, partition_id: str, source_id: str, digest: str) -> Path:
        return self.quarantine_dir / stage / partition_id / source_id / f"{digest}.parquet"

    def staged_path(self, bucket: int, partition_id: str, source_id: str) -> Path:
        return self.staged_dir / f"bucket={bucket:04d}" / f"{partition_id}__{source_id}.parquet"

    def bucket_dir(self, bucket: int) -> Path:
        return self.canonical_dir / "buckets" / f"bucket={bucket:04d}"

    def canonical_path(self, name: str) -> Path:
        return self.canonical_dir / f"{name}.parquet"

    def ensure(self) -> "WorkLayout":
        for d in (
            self.manifest_dir,
            self.source_dir,
            self.staged_dir,
            self.identity_dir,
            self.canonical_dir,
            self.quarantine_dir,
            self.omop_dir,
            self.meds_dir,
            self.review_dir,
            self.runs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        # The identity directory holds the only table that can turn a subject id back
        # into a patient. Keep it owner-only.
        try:
            os.chmod(self.identity_dir, 0o700)
        except OSError:
            pass
        return self


def write_table_atomic(table: pa.Table, path: Path, compression: str = "zstd") -> Path:
    """Write a parquet file that either exists complete or does not exist.

    Anything that crashes mid-write leaves a ``.partial`` file, which no reader globs.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + PARTIAL_SUFFIX)
    pq.write_table(table, tmp, compression=compression)
    os.replace(tmp, path)
    return path


class StreamingParquetWriter:
    """Row-group-at-a-time writer for files too large to hold in memory."""

    def __init__(self, path: Path, schema: pa.Schema, compression: str = "zstd"):
        self.path = path
        self.tmp = path.with_suffix(path.suffix + PARTIAL_SUFFIX)
        self.schema = schema
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(self.tmp, schema, compression=compression)
        self.rows_written = 0

    def write_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = {f.name: [r.get(f.name) for r in rows] for f in self.schema}
        self._writer.write_table(pa.table(cols, schema=self.schema))
        self.rows_written += len(rows)

    def close(self) -> Path:
        self._writer.close()
        os.replace(self.tmp, self.path)
        return self.path

    def __enter__(self) -> "StreamingParquetWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is not None:
            self._writer.close()
            self.tmp.unlink(missing_ok=True)
        else:
            self.close()


def clear_partials(root: Path) -> int:
    """Remove leftovers from an interrupted run. Never touches a complete file."""
    count = 0
    for p in root.rglob(f"*{PARTIAL_SUFFIX}"):
        p.unlink(missing_ok=True)
        count += 1
    return count


def rows_to_table(rows: Iterable[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    rows = list(rows)
    cols = {f.name: [r.get(f.name) for r in rows] for f in schema}
    return pa.table(cols, schema=schema)


def read_parquet_dir(root: Path, columns: Sequence[str] | None = None) -> pa.Table | None:
    """Read every complete parquet under ``root`` in a deterministic file order."""
    files = sorted(p for p in root.rglob("*.parquet") if not p.name.endswith(PARTIAL_SUFFIX))
    if not files:
        return None
    return pq.read_table(files, columns=list(columns) if columns else None)


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
