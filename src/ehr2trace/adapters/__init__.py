"""Adapter registry: importing this module registers every built-in adapter."""

from ehr2trace.adapters.base import Adapter, PhysicalUnit, RowStream, is_unnamed, strip_bom
from ehr2trace.adapters.delimited import DelimitedAdapter, ParquetAdapter, has_bom, sniff_line_ending
from ehr2trace.adapters.excel import AnyOfAdapter, ExcelAdapter, get_adapter, list_sheets, resolve_sheet

__all__ = [
    "Adapter",
    "AnyOfAdapter",
    "DelimitedAdapter",
    "ExcelAdapter",
    "ParquetAdapter",
    "PhysicalUnit",
    "RowStream",
    "get_adapter",
    "has_bom",
    "is_unnamed",
    "list_sheets",
    "resolve_sheet",
    "sniff_line_ending",
    "strip_bom",
]
