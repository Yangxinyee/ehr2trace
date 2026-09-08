"""Adapters must not let a formatting accident change meaning (checklist P1-1).

Every case here is a real inconsistency in the export: a BOM present in one file of a
source and absent in another, a sheet under two different names, a leading column with
an empty header, and cell types that differ row by row within one column.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ehr2trace.adapters import get_adapter, has_bom, resolve_sheet, sniff_line_ending
from ehr2trace.adapters.base import PhysicalUnit, is_unnamed, strip_bom
from ehr2trace.config import AdapterOptions

TSV = AdapterOptions(delimiter="\t", encoding="utf-8-sig", file_glob="*.txt")


def write_tsv(path: Path, header: list[str], rows: list[list[str]], bom: bool, crlf: bool = True) -> Path:
    eol = "\r\n" if crlf else "\n"
    body = eol.join(["\t".join(header)] + ["\t".join(r) for r in rows]) + eol
    data = ("﻿" if bom else "") + body
    path.write_bytes(data.encode("utf-8"))
    return path


def read_all(path: Path, options: AdapterOptions = TSV, adapter: str = "delimited"):
    unit = PhysicalUnit(path=path, adapter=adapter, options=options)
    stream = get_adapter(adapter).open(unit)
    rows = [(n, v) for n, v in stream.rows]
    handle = getattr(stream, "close", None)
    if hasattr(handle, "close"):
        handle.close()
    return stream.columns, rows


def test_bom_presence_does_not_change_the_first_column_name(tmp_path: Path):
    """Two files of one logical source disagree about the BOM; the reader must not."""
    with_bom = write_tsv(tmp_path / "a.txt", ["MRN", "V"], [["p1", "1"]], bom=True)
    without = write_tsv(tmp_path / "b.txt", ["MRN", "V"], [["p1", "1"]], bom=False)
    assert has_bom(with_bom) and not has_bom(without)
    assert read_all(with_bom)[0] == read_all(without)[0] == ["MRN", "V"]


def test_strip_bom_is_unconditional():
    assert strip_bom("﻿MRN") == "MRN"
    assert strip_bom("MRN") == "MRN"


def test_crlf_endings_do_not_leak_into_the_last_field(tmp_path: Path):
    path = write_tsv(tmp_path / "c.txt", ["A", "B"], [["x", "y"]], bom=True, crlf=True)
    assert sniff_line_ending(path) == "crlf"
    _cols, rows = read_all(path)
    assert rows[0][1] == ["x", "y"]


def test_column_reordering_and_case_do_not_change_role_resolution(tmp_path: Path):
    from ehr2trace.canonical.normalize import build_role_map
    from ehr2trace.config import SourceSpec

    spec = SourceSpec(
        adapter="delimited",
        shape="point_event",
        event_kind="measurement",
        fields={"person_id": {"from": ["MRN", "mrn"]}, "value": {"from": ["Value"]}},
    )
    assert build_role_map(spec, ["col__MRN", "col__Value"]) == {
        "person_id": ["col__MRN"],
        "value": ["col__Value"],
    }
    # reordered, lowercased, extra columns present
    assert build_role_map(spec, ["col__value", "col__extra", "col__mrn"]) == {
        "person_id": ["col__mrn"],
        "value": ["col__value"],
    }


def test_missing_optional_column_simply_has_no_role(tmp_path: Path):
    from ehr2trace.canonical.normalize import build_role_map
    from ehr2trace.config import SourceSpec

    spec = SourceSpec(
        adapter="delimited",
        shape="point_event",
        fields={"person_id": {"from": ["MRN"]}, "unit": {"from": ["Units"]}},
    )
    roles = build_role_map(spec, ["col__MRN"])
    assert "unit" not in roles and roles["person_id"] == ["col__MRN"]


def test_field_count_mismatch_is_visible_to_the_caller(tmp_path: Path):
    path = tmp_path / "ragged.txt"
    path.write_bytes("A\tB\tC\r\n1\t2\r\n1\t2\t3\r\n".encode("utf-8"))
    cols, rows = read_all(path)
    assert len(cols) == 3
    assert [len(v) for _n, v in rows] == [2, 3]


def test_row_numbers_count_data_rows_from_one(tmp_path: Path):
    path = write_tsv(tmp_path / "n.txt", ["A"], [["1"], ["2"], ["3"]], bom=True)
    _cols, rows = read_all(path)
    assert [n for n, _v in rows] == [1, 2, 3]


# -- workbooks ------------------------------------------------------------------


def make_workbook(path: Path, sheets: dict[str, list[list]]) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def test_sheet_aliases_resolve_in_declared_order(tmp_path: Path):
    a = make_workbook(tmp_path / "a.xlsx", {"PFT Narrative": [["MRN"], ["p1"]]})
    b = make_workbook(tmp_path / "b.xlsx", {"PFT": [["MRN"], ["p1"]]})
    assert resolve_sheet(a, ["PFT Narrative", "PFT"]) == "PFT Narrative"
    assert resolve_sheet(b, ["PFT Narrative", "PFT"]) == "PFT"
    assert resolve_sheet(b, ["Nope"]) is None


def test_sheet_alias_match_is_case_insensitive(tmp_path: Path):
    wb = make_workbook(tmp_path / "c.xlsx", {"pft values": [["MRN"], ["p1"]]})
    assert resolve_sheet(wb, ["PFT Values"]) == "pft values"


def test_unnamed_leading_column_is_skipped_positionally(tmp_path: Path):
    """One partition ships an extra leading column with an empty header and no values."""
    path = make_workbook(
        tmp_path / "d.xlsx", {"S": [[None, "MRN", "V"], [None, "p1", "7"], [None, "p2", "8"]]}
    )
    options = AdapterOptions(sheet_aliases=["S"], skip_unnamed_leading_columns=True)
    unit = PhysicalUnit(path=path, adapter="excel", options=options, sheet="S")
    stream = get_adapter("excel").open(unit)
    rows = list(stream.rows)
    assert stream.columns == ["MRN", "V"]
    assert rows[0][1] == ["p1", "7"]
    assert stream.warnings and "unnamed" in stream.warnings[0]


def test_unnamed_column_is_kept_when_not_asked_to_skip(tmp_path: Path):
    path = make_workbook(tmp_path / "e.xlsx", {"S": [[None, "MRN"], [None, "p1"]]})
    options = AdapterOptions(sheet_aliases=["S"])
    stream = get_adapter("excel").open(PhysicalUnit(path=path, adapter="excel", options=options, sheet="S"))
    assert is_unnamed(stream.columns[0])
    assert stream.columns[1] == "MRN"


def test_mixed_cell_types_in_one_column_are_handled_per_value(tmp_path: Path):
    """openpyxl types cells, not columns; inferring from row one would be wrong."""
    path = make_workbook(
        tmp_path / "f.xlsx",
        {"S": [["MRN", "Death_Date"], ["p1", "NULL"], ["p2", datetime(2031, 2, 3, 4, 5)], ["p3", 4]]},
    )
    options = AdapterOptions(sheet_aliases=["S"])
    stream = get_adapter("excel").open(PhysicalUnit(path=path, adapter="excel", options=options, sheet="S"))
    values = [v[1] for _n, v in stream.rows]
    assert [type(v).__name__ for v in values] == ["str", "datetime", "int"]

    from ehr2trace.hashing import canonical_cell

    assert canonical_cell(values[0]) == ""
    assert canonical_cell(values[1]) == "2031-02-03T04:05:00"
    assert canonical_cell(values[2]) == "4"


def test_fully_empty_rows_are_not_data(tmp_path: Path):
    path = make_workbook(tmp_path / "g.xlsx", {"S": [["MRN"], ["p1"], [None], ["p2"]]})
    options = AdapterOptions(sheet_aliases=["S"])
    stream = get_adapter("excel").open(PhysicalUnit(path=path, adapter="excel", options=options, sheet="S"))
    assert [v[0] for _n, v in stream.rows] == ["p1", "p2"]


def test_empty_sheet_yields_no_rows_and_no_facts(tmp_path: Path):
    """An empty sheet means "not extracted", never "the patient had none"."""
    path = make_workbook(tmp_path / "h.xlsx", {"S": [["MRN", "V"]]})
    options = AdapterOptions(sheet_aliases=["S"])
    stream = get_adapter("excel").open(PhysicalUnit(path=path, adapter="excel", options=options, sheet="S"))
    assert list(stream.rows) == []
    assert stream.columns == ["MRN", "V"]
