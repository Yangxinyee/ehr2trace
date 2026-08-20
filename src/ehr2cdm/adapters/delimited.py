"""Delimited text adapter (tab- or comma-separated).

Streams line by line: single files here reach 650 MB and must never be materialized.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from ehr2cdm.adapters.base import PhysicalUnit, RowStream, normalize_header, strip_bom
from ehr2cdm.config import AdapterOptions
from ehr2cdm.registry import register_adapter


@register_adapter("delimited")
class DelimitedAdapter:
    name = "delimited"

    def discover(self, partition_dir: Path, options: AdapterOptions) -> list[PhysicalUnit]:
        if not options.file_glob:
            return []
        found = sorted(p for p in partition_dir.glob(options.file_glob) if p.is_file())
        return [PhysicalUnit(path=p, adapter=self.name, options=options) for p in found]

    def open(self, unit: PhysicalUnit) -> RowStream:
        opts = unit.options
        # newline="" lets the csv module own line splitting, so a quoted field
        # containing a newline stays one row; the files here also use CRLF endings.
        handle = open(unit.path, "r", encoding=opts.encoding, newline="", errors="strict")
        quoting = csv.QUOTE_NONE if opts.quoting == "none" else csv.QUOTE_MINIMAL
        reader = csv.reader(handle, delimiter=opts.delimiter, quoting=quoting)

        columns: list[str] = []
        for _ in range(opts.header_row):
            try:
                columns = next(reader)
            except StopIteration:
                columns = []
                break
        columns = [normalize_header(strip_bom(c), i) for i, c in enumerate(columns)]

        def gen() -> Iterator[tuple[int, list[object]]]:
            n = 0
            for values in reader:
                n += 1
                # csv keeps a trailing \r when the file is CRLF and quoting is off.
                yield n, [v[:-1] if isinstance(v, str) and v.endswith("\r") else v for v in values]

        return RowStream(columns=columns, rows=gen(), close=handle)


@register_adapter("parquet")
class ParquetAdapter:
    name = "parquet"

    def discover(self, partition_dir: Path, options: AdapterOptions) -> list[PhysicalUnit]:
        if not options.file_glob:
            return []
        found = sorted(p for p in partition_dir.glob(options.file_glob) if p.is_file())
        return [PhysicalUnit(path=p, adapter=self.name, options=options) for p in found]

    def open(self, unit: PhysicalUnit) -> RowStream:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(unit.path)
        columns = [normalize_header(c, i) for i, c in enumerate(pf.schema_arrow.names)]

        def gen() -> Iterator[tuple[int, list[object]]]:
            n = 0
            for batch in pf.iter_batches():
                for row in batch.to_pylist():
                    n += 1
                    yield n, [row.get(c) for c in pf.schema_arrow.names]

        return RowStream(columns=columns, rows=gen(), close=None)


def read_text_head(path: Path, n_bytes: int = 4096) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n_bytes)


def has_bom(path: Path) -> bool:
    """Whether this file starts with a UTF-8 BOM. Reported by ``inspect``, never assumed."""
    return read_text_head(path, 3).startswith(b"\xef\xbb\xbf")


def sniff_line_ending(path: Path) -> str:
    head = read_text_head(path, 65536)
    if b"\r\n" in head:
        return "crlf"
    if b"\n" in head:
        return "lf"
    return "unknown"
