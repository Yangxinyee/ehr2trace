"""Workbook adapter (openpyxl, read-only).

Two facts about these workbooks shape this code:

* the same logical sheet appears under different names in different partitions, so a
  source declares an ordered alias list and the first match wins;
* one partition's sheet carries an extra leading column with an empty header and no
  values, so a source may declare that unnamed leading columns are skipped
  positionally. The column is still visible in the source layer's raw record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ehr2cdm.adapters.base import PhysicalUnit, RowStream, is_unnamed, normalize_header
from ehr2cdm.config import AdapterOptions
from ehr2cdm.registry import register_adapter


def list_sheets(path: Path) -> list[str]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def resolve_sheet(path: Path, aliases: list[str]) -> str | None:
    """First declared alias that exists in this workbook (case-insensitive)."""
    names = list_sheets(path)
    lowered = {n.strip().lower(): n for n in names}
    for alias in aliases:
        hit = lowered.get(alias.strip().lower())
        if hit is not None:
            return hit
    return None


@register_adapter("excel")
class ExcelAdapter:
    name = "excel"

    def discover(self, partition_dir: Path, options: AdapterOptions) -> list[PhysicalUnit]:
        pattern = options.file_glob or "*.xlsx"
        units: list[PhysicalUnit] = []
        for path in sorted(p for p in partition_dir.glob(pattern) if p.is_file()):
            if path.name.startswith("~$"):
                continue
            sheet = resolve_sheet(path, options.sheet_aliases) if options.sheet_aliases else None
            if options.sheet_aliases and sheet is None:
                continue
            units.append(PhysicalUnit(path=path, adapter=self.name, options=options, sheet=sheet))
        return units

    def open(self, unit: PhysicalUnit) -> RowStream:
        from openpyxl import load_workbook

        opts = unit.options
        wb = load_workbook(unit.path, read_only=True, data_only=True)
        ws = wb[unit.sheet] if unit.sheet else wb[wb.sheetnames[0]]
        raw_rows = ws.iter_rows(values_only=True)

        header: tuple = ()
        for _ in range(opts.header_row):
            try:
                header = next(raw_rows)
            except StopIteration:
                header = ()
                break
        columns = [normalize_header(c, i) for i, c in enumerate(header)]

        skip = 0
        warnings: list[str] = []
        if opts.skip_unnamed_leading_columns:
            while skip < len(columns) and is_unnamed(columns[skip]):
                skip += 1
            if skip:
                warnings.append(f"skipped {skip} unnamed leading column(s)")

        kept = columns[skip:]

        def gen() -> Iterator[tuple[int, list[object]]]:
            n = 0
            for values in raw_rows:
                # openpyxl pads short rows with None and yields fully-empty trailing rows
                if values is None or all(v is None for v in values):
                    continue
                n += 1
                yield n, list(values[skip:])
            wb.close()

        return RowStream(columns=kept, rows=gen(), close=wb, warnings=warnings)


@register_adapter("any_of")
class AnyOfAdapter:
    """One logical source with several physical forms.

    Used where the same data ships as a standalone text file in one partition and as a
    workbook sheet in the others. Variants are tried in declared order; the first that
    finds anything wins, and if none does the source is simply absent from that
    partition -- absent means "not extracted", never "the patient had none".
    """

    name = "any_of"

    def discover(self, partition_dir: Path, options: AdapterOptions) -> list[PhysicalUnit]:
        raise NotImplementedError("any_of is resolved by discover.resolve_source_units")

    def open(self, unit: PhysicalUnit) -> RowStream:
        return get_adapter(unit.adapter).open(unit)


def get_adapter(name: str):
    from ehr2cdm.registry import ADAPTERS

    cls = ADAPTERS.get(name)
    if cls is None:
        raise KeyError(f"unregistered adapter {name!r}")
    return cls()
