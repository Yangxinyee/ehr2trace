"""In-memory analytical connections that can actually spill to disk.

An in-memory DuckDB connection has no temporary directory configured, and without one
it cannot spill a sort or an aggregation -- it grows until the kernel intervenes. Two
functions in this package carried comments promising the opposite ("runs in the database
engine rather than in memory", "out of core") and neither had ever been true, because
the dataset they were written against fit in RAM. The first was killed at 195 GB
merging a canonical layer; the second at 178 GB building MEDS.

So the setup lives in one place, and the places that need it ask for it by name rather
than each remembering to configure a connection correctly.

The memory ceiling is derived from the machine rather than left at DuckDB's default
fraction of it. That default suits a process that owns the machine; these run alongside
everything else, and 80% of the host was enough for the kernel to pick them.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

#: Fraction of physical memory an analytical query may use before it spills.
MEMORY_FRACTION = 0.5
MIN_LIMIT_GB = 2

#: Threads for a heavy grouped aggregation. Not the core count: a hash aggregate keeps
#: per-thread state, so on a 48-core machine the same query needs several times the
#: memory it would on eight, and the operators that cannot spill -- a grouped ``list()``
#: is the one that bit us -- hit the ceiling that much sooner. DuckDB's own advice when
#: it runs out is to reduce this first.
HEAVY_THREADS = 8


#: An operator's cap, in GB, on what one process's analytical queries may hold before they
#: spill. Operational only: it changes no output and no address. It exists for a machine
#: that several builds share, where half of physical memory per process adds up to more
#: than the machine has.
OPERATOR_MEMORY_ENV = "EHR_DUCKDB_MEMORY_GB"


def operator_memory_limit_gb() -> int | None:
    """The operator's cap from ``EHR_DUCKDB_MEMORY_GB``, or None when it is not set.

    A value that is not a positive number is refused rather than ignored: a cap that
    silently did nothing is exactly the failure it exists to prevent.
    """
    raw = os.environ.get(OPERATOR_MEMORY_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{OPERATOR_MEMORY_ENV}={raw!r} is not a number of GB") from None
    if value <= 0:
        raise ValueError(f"{OPERATOR_MEMORY_ENV}={raw!r} must be a positive number of GB")
    return max(MIN_LIMIT_GB, int(value))


def memory_limit_gb() -> int | None:
    """The operator's cap when one is set, else roughly half of physical memory, or None
    where that cannot be determined."""
    override = operator_memory_limit_gb()
    if override is not None:
        return override
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    total_gb = pages * page_size / (1024**3)
    return max(MIN_LIMIT_GB, int(total_gb * MEMORY_FRACTION))


@contextmanager
def analytic_connection(scratch_dir: Path, threads: int | None = None) -> Iterator["object"]:
    """An in-memory connection with somewhere to spill, cleaned up on exit.

    ``scratch_dir`` should sit inside the work root rather than the system temp
    directory: these spills are the size of the dataset, and a work root is the one
    place the operator has already sized for that.
    """
    import duckdb

    scratch_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("PRAGMA preserve_insertion_order = false")
        con.execute("SET temp_directory = ?", [str(scratch_dir)])
        limit = memory_limit_gb()
        if limit:
            con.execute(f"SET memory_limit = '{limit}GB'")
        if threads:
            con.execute(f"SET threads = {int(threads)}")
        yield con
    finally:
        con.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
