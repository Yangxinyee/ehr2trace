"""Validation's plain DuckDB connections honour the operator's memory cap.

``EHR_DUCKDB_MEMORY_GB`` bounds ``analytic_connection`` and the OMOP publisher. Validation
also opens three connections of its own -- the source-row accounting, the read-only OMOP
database and the MEDS shards -- and a cap that missed them would still let a validation
promise a shared machine more memory than it has. Set, they take it; unset, they keep
DuckDB's default, as the publisher does.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import duckdb
import polars as pl
import pytest

from ehr2trace.paths import WorkLayout
from ehr2trace.validate import _apply_operator_memory_cap, _meds_query, _omop_connection

SOURCE = Path(__file__).resolve().parents[2] / "src" / "ehr2trace" / "validate.py"


def gib(setting: str) -> float:
    number, unit = re.fullmatch(r"([0-9.]+)\s*([KMGT]i?B)", setting.strip()).groups()
    scale = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[unit]
    return float(number) * scale / 2**30


def test_a_set_cap_bounds_a_plain_connection_and_an_unset_one_leaves_the_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EHR_DUCKDB_MEMORY_GB", raising=False)
    default = duckdb.connect().execute("SELECT current_setting('memory_limit')").fetchone()[0]
    con = duckdb.connect()
    _apply_operator_memory_cap(con)
    assert con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == default

    monkeypatch.setenv("EHR_DUCKDB_MEMORY_GB", "3")
    con = duckdb.connect()
    _apply_operator_memory_cap(con)
    assert gib(con.execute("SELECT current_setting('memory_limit')").fetchone()[0]) <= 3 * 1e9 / 2**30 + 0.01

    monkeypatch.setenv("EHR_DUCKDB_MEMORY_GB", "lots")
    with pytest.raises(ValueError):
        _apply_operator_memory_cap(duckdb.connect())


def test_the_omop_and_meds_connections_take_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EHR_DUCKDB_MEMORY_GB", "2")
    layout = WorkLayout(root=tmp_path / "work" / "cap_check", dataset_id="cap_check").ensure()
    db = duckdb.connect(str(layout.omop_dir / "omop.duckdb"))
    db.execute("CREATE TABLE person (person_id INTEGER); INSERT INTO person VALUES (1)")
    db.close()
    con = _omop_connection(SimpleNamespace(layout=layout))
    try:
        assert gib(con.execute("SELECT current_setting('memory_limit')").fetchone()[0]) <= 2 * 1e9 / 2**30 + 0.01
    finally:
        con.close()

    shard = tmp_path / "shard.parquet"
    pl.DataFrame({"subject_id": [1], "code": ["X"]}).write_parquet(shard)
    (limit,) = _meds_query([str(shard)], "SELECT current_setting('memory_limit')")[0]
    assert gib(limit) <= 2 * 1e9 / 2**30 + 0.01


def test_every_plain_connection_in_validation_applies_the_cap():
    """The source-row accounting's connection is internal to its check; hold all three to it."""
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    opened = [i for i, line in enumerate(lines) if "duckdb.connect(" in line]
    assert len(opened) == 3, f"expected three plain connections, found {len(opened)}: update this test with the new one"
    for i in opened:
        following = "\n".join(lines[i:i + 4])
        assert "_apply_operator_memory_cap(con)" in following, f"validate.py line {i + 1} opens a connection without the cap"
