"""The MIMIC-IV preparation helpers, on synthetic rows.

``tools/prepare_mimiciv.py`` runs outside the trusted boundary: nothing downstream
validates it, because everything downstream starts from what it wrote. So the three
things it is allowed to do -- sample a delivery, turn a wide row into several, and
lend a name from a linked order -- are tested here on rows small enough to reason
about, against the same DuckDB the script uses.

The SQL is executed rather than string-matched. A test that asserts on the text of a
query passes when the query is wrong in a way that still parses, which is every way
that matters.
"""
from __future__ import annotations

import json

import duckdb
import pytest

from tools.prepare_mimiciv import (
    LOOKUP_REASON,
    long_rows_adds_sql,
    long_rows_sql,
    name_lookup_sql,
    recovered_name_columns,
    sample_predicate,
    unread_inputs,
)

MEASURES = [("temperature", "ED_TEMPERATURE", "[degF]"),
            ("heartrate", "ED_HEARTRATE", "/min"),
            ("rhythm", "ED_RHYTHM", None)]


@pytest.fixture()
def con():
    c = duckdb.connect()
    yield c
    c.close()


# -- --sample ---------------------------------------------------------------------


def test_sample_predicate_is_off_by_default():
    assert sample_predicate(0) is None
    assert sample_predicate(-1) is None


def test_sample_keeps_one_subject_in_n_and_the_same_ones_every_time(con):
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('100'),('150'),('200'),('7')) v(subject_id)")
    kept = con.execute(f"SELECT subject_id FROM t WHERE {sample_predicate(50)} ORDER BY 1").fetchall()
    assert [r[0] for r in kept] == ["100", "150", "200"]
    # Divisibility, not a hash: the same N names the same subjects with nothing seeded,
    # which is what lets a sample of one table join a sample of another.
    again = con.execute(f"SELECT subject_id FROM t WHERE {sample_predicate(50)} ORDER BY 1").fetchall()
    assert kept == again


# -- wide vital signs -> one row per value ----------------------------------------


def test_wide_row_becomes_one_row_per_stated_value(con):
    con.execute(
        "CREATE TABLE ed AS SELECT * FROM (VALUES "
        "('s1','st1','2110-01-01 00:00:00','98.6','88','SR'),"
        # a row where two of the three cells say nothing: an empty string and a space
        "('s2','st2','2110-01-02 00:00:00','','  ','AF')"
        ") v(subject_id, stay_id, charttime, temperature, heartrate, rhythm)"
    )
    sql = long_rows_sql("ed", ["subject_id", "stay_id"], "charttime", "charttime", MEASURES)
    rows = con.execute(
        f"SELECT vital_name, value, unit FROM ({sql}) ORDER BY vital_name, value"
    ).fetchall()
    assert rows == [
        ("ED_HEARTRATE", "88", "/min"),
        ("ED_RHYTHM", "AF", None),
        ("ED_RHYTHM", "SR", None),
        ("ED_TEMPERATURE", "98.6", "[degF]"),
    ]
    # A text measure carries no unit; inventing one would be the converter deciding
    # what the source did not say.
    assert {u for name, _, u in rows if name == "ED_RHYTHM"} == {None}


def test_a_constant_column_reaches_every_split_row(con):
    con.execute(
        "CREATE TABLE tri AS SELECT * FROM (VALUES "
        "('s1','st1','2110-01-01 00:00:00','98.6','88','SR')) "
        "v(subject_id, stay_id, stay_intime, temperature, heartrate, rhythm)"
    )
    sql = long_rows_sql("tri", ["subject_id", "stay_id"], "stay_intime", "stay_intime",
                        MEASURES, constants={"time_is_fallback": "1"})
    flags = con.execute(f"SELECT DISTINCT time_is_fallback FROM ({sql})").fetchall()
    assert flags == [(1,)]


def test_the_split_declares_exactly_the_rows_it_adds(con):
    con.execute(
        "CREATE TABLE ed AS SELECT * FROM (VALUES "
        "('s1','st1','2110-01-01 00:00:00','98.6','88','SR'),"
        "('s2','st2','2110-01-02 00:00:00','','  ','AF')) "
        "v(subject_id, stay_id, charttime, temperature, heartrate, rhythm)"
    )
    sql = long_rows_sql("ed", ["subject_id", "stay_id"], "charttime", "charttime", MEASURES)
    written = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
    read = con.execute("SELECT count(*) FROM ed").fetchone()[0]
    added = con.execute(long_rows_adds_sql("ed", MEASURES)).fetchone()[0]
    # This identity is what `emit` asserts on every output: rows written equals rows
    # read plus the number the split says it adds. If they ever disagree a fact has
    # been duplicated or lost, and the run stops.
    assert written == read + added


# -- the name an unnamed administration borrows from its order (D-R18) ------------


def _orders(con, rows: list[tuple[str, str, str]]) -> None:
    values = ", ".join(f"('{p}','{t}','{d}')" for p, t, d in rows)
    con.execute(f"CREATE OR REPLACE TABLE rx AS SELECT * FROM (VALUES {values}) "
                "v(pharmacy_id, drug_type, drug)")


def test_the_main_row_names_the_order(con):
    _orders(con, [("p1", "BASE", "Bag"), ("p1", "MAIN", "Vancomycin"),
                  ("p1", "ADDITIVE", "Sodium Chloride")])
    got = con.execute(name_lookup_sql("rx")).fetchall()
    assert got == [("p1", "Vancomycin", 1)]


def test_an_order_with_no_main_row_is_named_by_what_it_has(con):
    _orders(con, [("p2", "BASE", "0.9% Sodium Chloride")])
    got = con.execute(name_lookup_sql("rx")).fetchall()
    assert got == [("p2", "0.9% Sodium Chloride", 1)]


def test_several_names_at_one_tier_are_counted_not_hidden(con):
    _orders(con, [("p3", "BASE", "Sterile Water"), ("p3", "BASE", "Dextrose")])
    (pharmacy_id, drug, candidates), = con.execute(name_lookup_sql("rx")).fetchall()
    assert (pharmacy_id, drug) == ("p3", "Dextrose")  # alphabetically first
    # The manifest reports this count, so a guess is a number somebody can look at
    # rather than a silent choice.
    assert candidates == 2


def test_the_lookup_has_one_row_per_order(con):
    _orders(con, [("p1", "MAIN", "A"), ("p1", "BASE", "B"), ("p2", "MAIN", "C"),
                  ("p2", "MAIN", "C")])
    got = con.execute(name_lookup_sql("rx")).fetchall()
    # Uniqueness is the whole safety of joining it onto eMAR: a second row for one
    # pharmacy_id would multiply every administration of that order.
    assert len({r[0] for r in got}) == len(got) == 2


def test_only_a_row_with_no_name_borrows_one(con):
    _orders(con, [("p1", "MAIN", "Vancomycin")])
    con.execute(f"CREATE TABLE lookup AS {name_lookup_sql('rx')}")
    con.execute(
        "CREATE TABLE emar AS SELECT * FROM (VALUES "
        "('e1','p1','Heparin'), ('e2','p1',''), ('e3','p1',NULL), ('e4','p9',NULL)) "
        "v(emar_id, pharmacy_id, medication)"
    )
    cols = recovered_name_columns("e", "n")
    rows = con.execute(
        f"SELECT e.emar_id, {cols} FROM emar e "
        "LEFT JOIN lookup n ON n.pharmacy_id = e.pharmacy_id ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("e1", None, 0),          # already named: the order's name is not imposed on it
        ("e2", "Vancomycin", 1),  # empty string counts as no name
        ("e3", "Vancomycin", 1),
        ("e4", None, 0),          # no order to borrow from: still nameless, still honest
    ]


# -- what the delivery holds and this conversion does not read --------------------


def test_every_unread_file_is_named_with_a_reason_or_reported_as_lacking_one(tmp_path):
    hosp = tmp_path / "hosp"
    hosp.mkdir()
    for name in ("drgcodes.csv.gz", "d_labitems.csv.gz", "labevents.csv.gz",
                 "something_new.csv.gz"):
        (hosp / name).write_bytes(b"")
    read = {
        str(hosp / "labevents.csv.gz"): {"source"},
        str(hosp / "d_labitems.csv.gz"): {"lookup"},
    }
    got = {row["path"].rsplit("/", 1)[-1]: row for row in unread_inputs({"hosp": hosp}, read)}

    # A source is not "unread" and does not appear.
    assert "labevents.csv.gz" not in got
    # A lookup was read but nothing downstream sees it as a table, so it is listed as
    # one: a coverage check comparing the delivery against the config finds every file.
    assert got["d_labitems.csv.gz"]["kind"] == "lookup"
    assert got["d_labitems.csv.gz"]["reason"] == LOOKUP_REASON
    # A file the plan decided not to read carries its reason.
    assert "payment classification" in got["drgcodes.csv.gz"]["reason"]
    # And a file that appears in a future release is reported rather than omitted,
    # which is the case this list exists to catch.
    assert got["something_new.csv.gz"]["reason"] == "NO REASON RECORDED"


def test_the_manifest_of_a_prepared_delivery_is_json_serialisable():
    # The manifest is the lineage record across the preparation boundary; it is written
    # with json.dumps, so a value that will not serialise loses the whole file.
    rows = unread_inputs({"hosp": None}, {})
    assert json.dumps(rows) == "[]"
