"""Content digests for published artifacts.

Two claims in this project need a way to ask whether two builds produced the same thing.
Determinism is one: the same inputs and the same configuration must yield the same
output whatever the worker count. Fault localisation is the other: to say whether a check
pointed at the artifact a fault damaged, something has to establish which artifact that
was, and comparing a mutated tree against the tree it was cloned from answers it without
each fault having to declare its own blast radius.

A byte comparison answers neither question. Parquet embeds a writer version and chooses
its own block layout, so two files can hold identical rows and differ on disk; and two
builds that partition work differently have no obligation to agree on row order. These
digests are therefore taken over row *content*, order-independently.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

#: Bumping this invalidates every recorded digest, so it changes only when the digest
#: rule itself changes -- never to force a rebuild.
DIGEST_RULE_VERSION = "1"


def digest_frame(frame) -> str:
    """A digest of a frame's rows, independent of row order and column order.

    `hash_rows` handles nested and temporal dtypes, which a cast to string does not: a
    `list[str]` column raises rather than serialising, and the first version of this
    function died on `quality_flags` for exactly that reason.
    """
    import polars as pl

    columns = sorted(frame.columns)
    header = hashlib.sha256(f"{DIGEST_RULE_VERSION}\x1f{','.join(columns)}".encode()).hexdigest()
    if frame.height == 0:
        return f"0:{header}"
    hashes = frame.select(columns).hash_rows(seed=0)
    # Sorted, so two builds that emitted the same rows in a different order agree.
    # Serialised a word at a time with the byte order named rather than inherited:
    # `to_numpy().tobytes()` produces the same bytes on x86 but reads the machine's
    # endianness, which is not something a digest that certifies determinism should
    # depend on -- and it pulled in numpy, which nothing else here needs.
    payload = b"".join(int(h).to_bytes(8, "little") for h in hashes.sort())
    return f"{frame.height}:{hashlib.sha256(header.encode() + payload).hexdigest()}"


def digest_parquet(path: Path) -> str:
    import polars as pl

    return digest_frame(pl.read_parquet(path))


def digest_duckdb(path: Path) -> dict[str, str]:
    """Per-table digests, computed in the engine so the rows never leave it."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
        out: dict[str, str] = {}
        for table in tables:
            columns = sorted(r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall())
            if not columns:
                continue
            expr = " || '\x1f' || ".join(
                f'coalesce(CAST("{c}" AS VARCHAR), \'\\x00\')' for c in columns
            )
            # Summed row hashes: an aggregate that does not depend on the order the
            # engine happens to return rows in, which is not stable across thread counts.
            count, total = con.execute(
                f'SELECT count(*), coalesce(sum(hash({expr})::HUGEINT), 0) FROM "{table}"'
            ).fetchone()
            out[table] = f"{count}:{total}"
        return out
    finally:
        con.close()


def fingerprint(layout, include_shards: bool = True) -> dict[str, str]:
    """Every published artifact of one work tree, keyed by a stable name.

    Keys are what a check's detail string would have to mention for it to be pointing at
    the artifact: `canonical/events`, `omop/person`, `meds/data/<shard>`.
    """
    out: dict[str, str] = {}
    canonical = Path(layout.canonical_dir) if hasattr(layout, "canonical_dir") else None
    if canonical and canonical.exists():
        for path in sorted(canonical.glob("*.parquet")):
            out[f"canonical/{path.stem}"] = digest_parquet(path)
    omop_db = Path(layout.omop_dir) / "omop.duckdb"
    if omop_db.exists():
        for table, value in digest_duckdb(omop_db).items():
            out[f"omop/{table}"] = value
    meds = Path(layout.meds_dir)
    for path in sorted((meds / "metadata").glob("*.parquet")):
        out[f"meds/metadata/{path.stem}"] = digest_parquet(path)
    if include_shards:
        for path in sorted((meds / "data").rglob("*.parquet")):
            out[f"meds/data/{path.stem}"] = digest_parquet(path)
    return out


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Artifacts that differ, including ones that appeared or vanished."""
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def mentions(text: str, artifacts: Iterable[str]) -> list[str]:
    """Which of these artifacts a check identifies, by name.

    The text passed in is the check's identifier together with its message, because both
    are what an engineer reads: ``MEDS_SPLITS_DISJOINT_AND_COMPLETE`` points at
    `subject_splits` whether or not the message repeats the file name.

    Matching is on the artifact's name split into words, singularised, and only words of
    four characters or more count -- so `canonical/event_source` is named by anything
    mentioning ``event`` or ``source``. This is deliberately generous and therefore an
    upper bound: it credits a check with localising a fault whenever the words line up,
    which they sometimes will by coincidence. The number is reported as the ceiling it is
    rather than as a precise measurement.
    """
    haystack = text.lower()
    hit = []
    for artifact in artifacts:
        words = artifact.rsplit("/", 1)[-1].lower().split("_")
        stems = {w[:-1] if w.endswith("s") else w for w in words}
        if any(len(stem) >= 4 and stem in haystack for stem in stems):
            hit.append(artifact)
    return sorted(set(hit))


def digest_parquet_files(files: dict[str, Path], temp_dir: Path | None = None) -> dict[str, str]:
    """Per-file digests of parquet tables, computed in the engine.

    `digest_parquet` reads a file through polars, which is fine for a fixture and not
    for MIMIC-IV's canonical layer: 311 million events and 317 million links do not
    fit in memory as a frame. This is `digest_duckdb`'s aggregate over files instead of
    tables, so the same order-independent digest is measurable at full scale.
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("SET enable_progress_bar = false")
        con.execute("PRAGMA preserve_insertion_order = false")
        if temp_dir:
            con.execute("SET temp_directory = ?", [str(temp_dir)])
        out: dict[str, str] = {}
        for name, path in sorted(files.items()):
            if not Path(path).exists():
                out[name] = "absent"
                continue
            source = f"read_parquet('{path}')"
            columns = sorted(r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall())
            expr = " || '\x1f' || ".join(
                f'coalesce(CAST("{c}" AS VARCHAR), \'\\x00\')' for c in columns
            )
            count, total = con.execute(
                f"SELECT count(*), coalesce(sum(hash({expr})::HUGEINT), 0) FROM {source}"
            ).fetchone()
            out[name] = f"{count}:{total}"
        return out
    finally:
        con.close()


def digest_meds_data(meds_dir: Path, temp_dir: Path | None = None) -> str:
    """One digest over every MEDS shard, computed in the engine.

    `fingerprint(include_shards=True)` reads each shard through polars, which is fine for
    a fixture and hopeless for a real dataset -- MIMIC-IV publishes 364,673 of them. This
    pushes the whole scan into DuckDB and aggregates order-independently, so the same
    property is measurable at full scale.

    The shard set is part of the output, so the file name is hashed alongside the rows: a
    build that loses a subject must not compare equal to one that kept it. It is hashed
    *relative to the MEDS root*, because DuckDB's `filename` is absolute -- and comparing
    a tree against a clone of it in another directory then reports a difference on every
    row, which is what the first full-scale run of this function did. An instrument that
    cannot tell a moved file from a changed one is not measuring reproducibility.
    """
    import duckdb

    glob = str(Path(meds_dir) / "data" / "*" / "*.parquet")
    con = duckdb.connect()
    try:
        con.execute("SET enable_progress_bar = false")
        con.execute("PRAGMA preserve_insertion_order = false")
        if temp_dir:
            con.execute("SET temp_directory = ?", [str(temp_dir)])
        columns = sorted(
            r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}')").fetchall()
        )
        expr = " || '\x1f' || ".join(
            f'coalesce(CAST("{c}" AS VARCHAR), \'\\x00\')' for c in columns
        )
        # `+ 1` drops the separator as well as the prefix, leaving `train/000123.parquet`.
        prefix = len(str(Path(meds_dir) / "data")) + 1
        count, total = con.execute(
            f"""SELECT count(*),
                       coalesce(sum(hash(substr(filename, {prefix + 1}) || '\x1e' || {expr})
                                    ::HUGEINT), 0)
                FROM read_parquet('{glob}', filename = true)"""
        ).fetchone()
        header = hashlib.sha256(
            f"{DIGEST_RULE_VERSION}\x1f{','.join(columns)}".encode()
        ).hexdigest()
        return f"{count}:{total}:{header}"
    finally:
        con.close()
