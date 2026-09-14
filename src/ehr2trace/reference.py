"""Reference tables: unit spellings, exact conversions, plausible ranges.

Remediation plan T1.6, T1.7 and T1.9 (decisions D-R1, D-R10, D-R17). The tables are
judgements -- which spelling is which unit, what an exact conversion is, what a value
can plausibly be -- and judgements do not belong in code. They live as CSV under
``reference/`` at the repository root (``EHR_REFERENCE_DIR`` overrides the location,
the way ``EHR_MAPPINGS_DIR`` does for mappings), and this module only loads them.

``reference/units/*.csv``
    ``source_unit,ucum,basis``. Every file is merged; a spelling listed twice across
    files is an error, compared case-insensitively, because two owners disagreeing on
    what ``U`` means must be settled by a person and not by file order. The ``ucum``
    column is the case-sensitive UCUM code; it is what ``unit_normalized`` carries and
    what the OMOP publisher resolves to a concept. Where the OMOP vocabulary spells a
    code differently from UCUM (``meq/L`` is ``10*-3.eq/L`` there), the ``basis`` cell
    says so and ``mappings/unit.csv`` bridges it.
``reference/unit_conversions.csv``
    ``from_ucum,to_ucum,factor,offset,basis``; ``value_to = value_from * factor +
    offset``, and a factor may be written as a fraction (``5/9``) so it is exact.
    Exact conversions only: a temperature scale, an imperial unit, a count per cubic
    millimetre against a count per microlitre. Nothing analyte-specific.
``reference/plausible_ranges/<dataset_id>.csv``
    ``code_system,source_code,ucum,low,high,basis``; one file per dataset, owned by
    that dataset's agent, applied after normalization on the normalized unit (an empty
    ``ucum`` matches a unitless value). A value outside ``[low, high]`` keeps
    ``value_number`` and loses ``value_number_normalized``; it is flagged, never
    changed and never dropped.

The canonical task digest folds in :func:`reference_digest`, a hash over every file
here, so editing a table rebuilds the canonical layer without re-ingesting anything.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from ehr2trace.canonical.values import DEFAULT_SENTINELS, ValueParsingSpec
from ehr2trace.errors import ConfigError

DEFAULT_REFERENCE_DIR = Path(__file__).resolve().parents[2] / "reference"
UNITS_SUBDIR = "units"
CONVERSIONS_FILE = "unit_conversions.csv"
RANGES_SUBDIR = "plausible_ranges"

UNIT_COLUMNS = ("source_unit", "ucum", "basis")
CONVERSION_COLUMNS = ("from_ucum", "to_ucum", "factor", "offset", "basis")
RANGE_COLUMNS = ("code_system", "source_code", "ucum", "low", "high", "basis")


def reference_directory() -> Path:
    raw = os.environ.get("EHR_REFERENCE_DIR")
    return Path(raw) if raw else DEFAULT_REFERENCE_DIR


# --------------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitTable:
    """Source spelling -> UCUM code, compared case-insensitively after stripping.

    A spelling may also be listed with no UCUM code, which declares that it is *not* a
    unit: `*Unspecified` and `*Not Applicable` are what an order-entry screen writes
    into a unit column when the prescriber left it blank. Such a spelling is deliberately
    not :meth:`known` -- a number followed by it is text, not a measurement -- and it
    normalizes to nothing. Listing it is how "somebody looked at this and it is not a
    unit" differs from "nobody has seen this yet", which is the whole point of
    UNIT_UNKNOWN.
    """

    by_spelling: Mapping[str, str]
    #: spellings declared not to be units, lower-cased
    not_units: frozenset[str] = frozenset()

    def declared(self, spelling: object) -> bool:
        """Whether any table has seen this spelling, unit or not."""
        if spelling is None:
            return False
        key = self.key(spelling)
        return key in self.by_spelling or key in self.not_units

    @staticmethod
    def key(spelling: object) -> str:
        return str(spelling).strip().lower()

    def lookup(self, spelling: object) -> str | None:
        if spelling is None:
            return None
        return self.by_spelling.get(self.key(spelling))

    def known(self, spelling: object) -> bool:
        return self.lookup(spelling) is not None

    @property
    def spellings(self) -> frozenset[str]:
        """The lower-cased spellings, for :func:`parse_value`'s number-plus-unit rule."""
        return frozenset(self.by_spelling)


@dataclass(frozen=True)
class Conversion:
    to_ucum: str
    factor: Fraction
    offset: Fraction

    def apply(self, value: float) -> float:
        # Exact rational arithmetic on the value's shortest decimal form: 98.6 is not
        # representable in binary, but "98.6" is 493/5, and (493/5 - 32) * 5/9 is
        # exactly 37. Floating-point evaluation would publish 37.00000000000001.
        return float(Fraction(repr(value)) * self.factor + self.offset)


@dataclass(frozen=True)
class PlausibleRange:
    low: float | None
    high: float | None

    def contains(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


RangeKey = tuple[str, str, str | None]


@dataclass(frozen=True)
class ReferenceTables:
    units: UnitTable
    conversions: Mapping[str, Conversion]
    ranges: Mapping[RangeKey, PlausibleRange]
    #: sha256 over every file under the reference directory, folded into task digests
    digest: str

    @classmethod
    def empty(cls) -> "ReferenceTables":
        return cls(UnitTable({}), {}, {}, digest=reference_digest(None))

    def normalize(self, ucum: str | None, value: float | None) -> tuple[str | None, float | None]:
        """Apply the conversion registered for ``ucum``, if any.

        Returns the unit the value is now in and the value itself. With no conversion
        both pass through; a value is only ever changed by an exact rule.
        """
        if ucum is None:
            return None, value
        conversion = self.conversions.get(ucum)
        if conversion is None:
            return ucum, value
        if value is None or not math.isfinite(value):
            return conversion.to_ucum, value
        return conversion.to_ucum, conversion.apply(value)

    def implausible(self, code_system: str, source_code: str, ucum: str | None, value: float | None) -> bool:
        """Whether a declared range exists for this code and unit and excludes ``value``."""
        if value is None or not math.isfinite(value):
            return False
        bounds = self.ranges.get((code_system, source_code, ucum))
        if bounds is None:
            return False
        return not bounds.contains(value)


# --------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------


def load_reference(root: Path | None, dataset_id: str) -> ReferenceTables:
    """Every table under ``root`` (default: the repository's ``reference/``)."""
    directory = Path(root) if root is not None else reference_directory()
    return _load_cached(str(directory), reference_digest(directory), dataset_id)


@lru_cache(maxsize=32)
def _load_cached(root: str, digest: str, dataset_id: str) -> ReferenceTables:
    # Keyed on the digest as well as the path: a table edited between two loads in one
    # process must not be served from the cache in its earlier form.
    directory = Path(root)
    return ReferenceTables(
        units=_load_units(directory / UNITS_SUBDIR),
        conversions=_load_conversions(directory / CONVERSIONS_FILE),
        ranges=_load_ranges(directory / RANGES_SUBDIR / f"{dataset_id}.csv"),
        digest=digest,
    )


def _rows(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or [c.strip() for c in reader.fieldnames] != list(columns):
            raise ConfigError(
                f"{path}: expected columns {','.join(columns)}, found "
                f"{','.join(reader.fieldnames or [])}"
            )
        out = []
        for number, row in enumerate(reader, start=2):
            cleaned = {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}
            cleaned["_line"] = str(number)
            out.append(cleaned)
        return out


def _load_units(directory: Path) -> UnitTable:
    by_spelling: dict[str, str] = {}
    not_units: set[str] = set()
    origin: dict[str, str] = {}
    if not directory.is_dir():
        return UnitTable({})
    for path in sorted(directory.glob("*.csv")):
        for row in _rows(path, UNIT_COLUMNS):
            spelling, ucum = row["source_unit"], row["ucum"]
            if not spelling:
                raise ConfigError(f"{path}:{row['_line']}: source_unit is required")
            if not ucum:
                # A spelling with no code is a declaration that it is not a unit, and it
                # has to say why -- otherwise an empty cell is indistinguishable from a
                # row somebody forgot to finish.
                if not row.get("basis"):
                    raise ConfigError(
                        f"{path}:{row['_line']}: {spelling!r} has no ucum code, which declares it "
                        "is not a unit; say so in 'basis' or give it a code"
                    )
                not_units.add(UnitTable.key(spelling))
                continue
            key = UnitTable.key(spelling)
            # Two tables naming one spelling is only a problem when they disagree about
            # it. A dataset's own table is written from that export's spellings and will
            # repeat the common ones -- `mg`, `percent`, `mcg/kg/min` -- with a basis
            # citing that export's counts, which is evidence worth keeping. Refusing the
            # repetition would make every dataset's table a diff against a file its
            # author cannot see. Refusing a *contradiction* is the point, and that is
            # what stays: one spelling may mean exactly one unit.
            previous = by_spelling.get(key)
            if previous is not None and previous != ucum:
                raise ConfigError(
                    f"{path}:{row['_line']}: unit spelling {spelling!r} is {ucum!r} here and "
                    f"{previous!r} in {origin[key]} (compared case-insensitively); one "
                    "spelling means one unit, so one of the two is wrong"
                )
            if previous is None:
                by_spelling[key] = ucum
                origin[key] = f"{path.name}"
    # A spelling listed as a unit somewhere outranks a table that calls it none: the
    # code is the more specific statement, and the disagreement is visible in the files.
    return UnitTable(by_spelling, frozenset(not_units - set(by_spelling)))


def _fraction(cell: str, path: Path, line: str, column: str) -> Fraction:
    try:
        return Fraction(cell)
    except (ValueError, ZeroDivisionError) as exc:
        raise ConfigError(f"{path}:{line}: {column} must be a number or a fraction, not {cell!r}") from exc


def _load_conversions(path: Path) -> dict[str, Conversion]:
    if not path.is_file():
        return {}
    out: dict[str, Conversion] = {}
    for row in _rows(path, CONVERSION_COLUMNS):
        source, target = row["from_ucum"], row["to_ucum"]
        if not source or not target:
            raise ConfigError(f"{path}:{row['_line']}: from_ucum and to_ucum are both required")
        if source in out:
            raise ConfigError(f"{path}:{row['_line']}: {source!r} has two conversions; a unit converts one way")
        out[source] = Conversion(
            to_ucum=target,
            factor=_fraction(row["factor"], path, row["_line"], "factor"),
            offset=_fraction(row["offset"] or "0", path, row["_line"], "offset"),
        )
    return out


def _bound(cell: str, path: Path, line: str, column: str) -> float | None:
    if cell == "":
        return None
    try:
        return float(cell)
    except ValueError as exc:
        raise ConfigError(f"{path}:{line}: {column} must be a number or empty, not {cell!r}") from exc


def _load_ranges(path: Path) -> dict[RangeKey, PlausibleRange]:
    if not path.is_file():
        return {}
    out: dict[RangeKey, PlausibleRange] = {}
    for row in _rows(path, RANGE_COLUMNS):
        key: RangeKey = (row["code_system"], row["source_code"], row["ucum"] or None)
        if not key[0] or not key[1]:
            raise ConfigError(f"{path}:{row['_line']}: code_system and source_code are both required")
        low = _bound(row["low"], path, row["_line"], "low")
        high = _bound(row["high"], path, row["_line"], "high")
        if low is None and high is None:
            raise ConfigError(f"{path}:{row['_line']}: a range needs at least one bound")
        if low is not None and high is not None and low > high:
            raise ConfigError(f"{path}:{row['_line']}: low {low} exceeds high {high}")
        if key in out:
            raise ConfigError(f"{path}:{row['_line']}: {key} is declared twice")
        out[key] = PlausibleRange(low, high)
    return out


class UnitMap(dict):
    """Unit spellings, looked up without caring how they were capitalized.

    A plain mapping of lower-cased spelling to UCUM code, except that reading it
    lower-cases the key first, so both ``units["MG"]`` and ``units["mg"]`` answer. The
    OMOP publisher holds one of these to turn a dose's unit into a concept, and it has
    no business knowing that the table is keyed in lower case.
    """

    def __getitem__(self, key: object) -> str:
        return super().__getitem__(UnitTable.key(key))

    def __contains__(self, key: object) -> bool:
        return super().__contains__(UnitTable.key(key))

    def get(self, key: object, default: object = None) -> Any:
        return super().get(UnitTable.key(key), default)


def load_units(root: Path | None = None) -> UnitMap:
    """Every unit spelling any table lists -> its UCUM code.

    The one function outside the canonical build that needs the unit table, kept
    deliberately small and dependency-free: the publisher imports it inside a
    ``try/except ImportError`` and falls back to publishing the source's spelling.
    """
    return UnitMap(load_reference(root, dataset_id="").units.by_spelling)


def reference_digest(root: Path | None) -> str:
    """sha256 over every file under the reference directory, by relative path and bytes.

    Part of the canonical task address: a changed table must rebuild the canonical
    layer, and must not touch the ingest it does not affect.
    """
    directory = Path(root) if root is not None else reference_directory()
    h = hashlib.sha256()
    if directory.is_dir():
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            h.update(path.relative_to(directory).as_posix().encode("utf-8"))
            h.update(b"\x1f")
            h.update(path.read_bytes())
            h.update(b"\x1e")
    return h.hexdigest()


def parsing_spec(
    null_literals: tuple[str, ...] | list[str] = ("NULL",),
    sentinels: tuple[str, ...] | list[str] | None = None,
    root: Path | None = None,
) -> ValueParsingSpec:
    """A value-parsing spec that knows the unit table.

    The one entry point every parser of cells should use -- the canonical build, and
    the OMOP publisher's dose parsing -- so that ``10 mg`` splits into a number and a
    unit everywhere and ``10 SINUS TACHYCARDIA`` is text everywhere.
    """
    units = load_reference(root, dataset_id="").units
    return ValueParsingSpec(
        sentinels=tuple(s.lower() for s in (sentinels or DEFAULT_SENTINELS)),
        null_literals=tuple(null_literals),
        known_units=units.spellings,
    )
