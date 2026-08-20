"""Adapter interface.

An adapter answers two questions and nothing else:

* which physical units (a file, or a sheet inside a workbook) belong to this logical
  source in this partition?
* what are its column names, and what are its rows?

Adapters never interpret meaning. They do not know what a date is, what a patient is,
or which column matters. Values come out as they were stored -- ``str`` for delimited
text, native cell types for spreadsheets -- because the canonical serialization rule
(section 5.1) is what makes those differences harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

from ehr2cdm.config import AdapterOptions

BOM = "﻿"


@dataclass(frozen=True)
class PhysicalUnit:
    """One readable unit of a logical source."""

    path: Path
    adapter: str
    options: AdapterOptions
    sheet: str | None = None

    @property
    def label(self) -> str:
        return f"{self.path.name}" + (f"::{self.sheet}" if self.sheet else "")


@dataclass
class RowStream:
    """Column names plus a row iterator.

    ``rows`` yields ``(row_number, values)`` with ``row_number`` counting data rows from
    1. A row whose length differs from ``columns`` is yielded unchanged; deciding what
    to do about it is the ingest stage's job, not the adapter's.
    """

    columns: list[str]
    rows: Iterator[tuple[int, list[object]]]
    close: object = None
    warnings: list[str] = field(default_factory=list)


class Adapter(Protocol):
    name: str

    def discover(self, partition_dir: Path, options: AdapterOptions) -> list[PhysicalUnit]: ...

    def open(self, unit: PhysicalUnit) -> RowStream: ...


def strip_bom(text: str) -> str:
    """Strip a BOM unconditionally.

    Presence is inconsistent even within one logical source, so the parser must assume
    neither. Without this the first column name silently becomes a different string in
    some files and the alias lookup misses.
    """
    return text[1:] if text.startswith(BOM) else text


def normalize_header(name: object, index: int) -> str:
    """Header cell -> column name. Blank headers become positional placeholders."""
    if name is None:
        return f"__unnamed_{index}"
    text = strip_bom(str(name)).strip()
    return text or f"__unnamed_{index}"


def is_unnamed(column: str) -> bool:
    return column.startswith("__unnamed_")
