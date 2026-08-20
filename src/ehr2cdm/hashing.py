"""Canonical serialization and stable identifiers (design section 5.1).

This is the easiest module in the project to get wrong, and getting it wrong fails
silently: the same record read from two workbooks would hash differently and
cross-batch deduplication would quietly stop working. The rules below are therefore
fixed, versioned (``HASH_RULE_VERSION``) and tested directly.

Cell -> canonical string:

===============  ===========================================================
Python type      Canonical string
===============  ===========================================================
``None``         ``""``
``str``          ``strip()``ed; a null literal (default ``"NULL"``) becomes ``""``;
                 a timestamp-shaped string renders exactly as the equivalent
                 ``datetime`` would, so that a workbook storing a date as text
                 hashes identically to one storing it as a date
``int``          ``str(v)``
``float``        integral values render as integers (``5.0`` -> ``"5"``),
                 otherwise the shortest round-trip ``repr``
``datetime``     ISO 8601, second precision, no timezone suffix
``date``         ISO 8601 at midnight, second precision
``bool``         ``"true"`` / ``"false"``
===============  ===========================================================

A row hash is the canonical strings joined with ``\\x1f`` and sha256'd.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime, time as _time
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from ehr2cdm.version import HASH_RULE_VERSION

FIELD_SEP = "\x1f"

#: Timestamp-shaped strings are rendered exactly as the equivalent ``datetime`` is.
#: Without this, one workbook storing a date as text and another storing it as a date
#: would hash differently and cross-batch deduplication would fail silently -- the
#: single most expensive failure mode in this pipeline, and a completely quiet one.
_ISO_LIKE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[ T](?P<time>\d{2}:\d{2}(?::\d{2})?)(?:\.(?P<frac>\d+))?)?$"
)
DEFAULT_NULL_LITERALS: tuple[str, ...] = ("NULL",)

# Positive int64 space for subject ids: 63 bits, so the value never goes negative
# in Arrow/DuckDB int64 columns.
_INT63_MASK = (1 << 63) - 1


def canonical_cell(value: object, null_literals: Sequence[str] = DEFAULT_NULL_LITERALS) -> str:
    """Render one cell to its canonical string.

    ``bool`` is checked before ``int`` and ``datetime`` before ``date`` because each is
    a subclass of the other. Type is inspected per value: openpyxl returns types per
    cell, so two rows of one column may legitimately differ (design section 2.3 #16).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        s = value.strip()
        if s in null_literals:
            return ""
        m = _ISO_LIKE.match(s)
        if m:
            hhmmss = m.group("time") or "00:00"
            if len(hhmmss) == 5:
                hhmmss += ":00"
            return f"{m.group('date')}T{hhmmss}"
        return s
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        if value.is_integer():
            # 5.0 and 5 must be indistinguishable: a length-of-stay column can be
            # typed as an integer in three partitions and as text in the fourth.
            return str(int(value))
        return repr(value)
    if isinstance(value, Decimal):
        return canonical_cell(float(value), null_literals)
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, date):
        return datetime.combine(value, _time()).isoformat(timespec="seconds")
    if isinstance(value, (bytes, bytearray)):
        return hashlib.sha256(bytes(value)).hexdigest()
    # Anything else is an unexpected cell type. Rendering it via str() would be a
    # silent guess, so refuse loudly instead.
    raise TypeError(f"no canonical serialization for cell type {type(value)!r}")


def source_cell(value: object, null_literals: Sequence[str] = DEFAULT_NULL_LITERALS) -> str:
    """Render one cell for *storage* in the source layer.

    Deliberately weaker than :func:`canonical_cell`: text is kept in the form the
    source wrote it, so a value can still be compared against the raw file by eye. The
    stronger normalization is applied only where it is needed, when hashing.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.strip()
        return "" if s in null_literals else s
    return canonical_cell(value, null_literals)


def canonical_row(
    values: Iterable[object], null_literals: Sequence[str] = DEFAULT_NULL_LITERALS
) -> str:
    """Join a row's canonical cell strings with the unit separator."""
    return FIELD_SEP.join(canonical_cell(v, null_literals) for v in values)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def row_sha256(
    values: Iterable[object], null_literals: Sequence[str] = DEFAULT_NULL_LITERALS
) -> str:
    """``source_row_sha256``: content hash of one row, type-drift immune."""
    return sha256_hex(canonical_row(values, null_literals))


def source_row_id(
    dataset_id: str, partition_id: str, source_id: str, file_sha256: str, row_number: int
) -> str:
    """``sha256(dataset_id|partition_id|source_id|file_sha256|row_number)``."""
    return sha256_hex(
        "|".join([dataset_id, partition_id, source_id, file_sha256, str(row_number)])
    )


def stable_id(*parts: object, prefix: str = "") -> str:
    """A stable hex id over canonicalized parts. Used for event / anchor / note ids."""
    body = canonical_row(parts)
    return sha256_hex(f"{prefix}{HASH_RULE_VERSION}{FIELD_SEP}{body}")


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def subject_id_from_person_key(dataset_id: str, person_source_id: str, salt: str = "") -> int:
    """Patient key -> int64 subject id: stable, dataset-scoped, irreversible.

    Stable means a given key yields the same subject id on any machine and in any run
    order, so no global sort or counter is needed. Irreversible means the key cannot be
    recovered from the id; with ``salt`` set it cannot be confirmed by guessing either.
    The key itself never leaves the protected mapping table.
    """
    digest = hashlib.sha256(
        FIELD_SEP.join([salt, dataset_id, person_source_id.strip()]).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") & _INT63_MASK


def bucket_of(subject_id: int, bucket_count: int) -> int:
    """Which shard a subject belongs to. A subject always lands in the same bucket."""
    if bucket_count <= 0:
        raise ValueError("bucket_count must be positive")
    digest = hashlib.sha256(str(subject_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % bucket_count


def split_of(subject_id: int, weights: Sequence[tuple[str, float]], salt: str = "") -> str:
    """Assign a subject to a split by stable hash. All of a subject's events follow it."""
    total = sum(w for _, w in weights)
    if total <= 0:
        raise ValueError("split weights must sum to a positive number")
    digest = hashlib.sha256(f"{salt}{FIELD_SEP}split{FIELD_SEP}{subject_id}".encode()).digest()
    point = (int.from_bytes(digest[:8], "big") / float(1 << 64)) * total
    upto = 0.0
    for name, weight in weights:
        upto += weight
        if point < upto:
            return name
    return weights[-1][0]
