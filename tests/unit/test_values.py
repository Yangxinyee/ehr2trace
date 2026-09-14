"""The seven result forms (design section 5.4, checklist P1-6).

Every one of these is a real form observed in the export. The point of the tests is
the failure path: a value that matches nothing must be quarantined, because a forced
numeric cast turns "<0.5" into 0.5 and nothing downstream can tell.
"""

from __future__ import annotations

import pytest

from ehr2trace.canonical.values import ValueParsingSpec, parse_value
from ehr2trace.errors import QuarantineRow
from ehr2trace.schema import QualityFlag


def test_plain_number():
    v = parse_value("4.2")
    assert (v.number, v.text, v.form) == (4.2, None, "number")


def test_number_with_separate_unit_column():
    v = parse_value("38.62", "K/cu mm")
    assert v.number == 38.62 and v.unit == "K/cu mm"


def test_number_with_unit_in_the_same_cell():
    v = parse_value("38.62 K/cu mm")
    assert v.number == 38.62 and v.unit == "K/cu mm" and v.form == "number_unit"


def test_range_keeps_both_bounds_and_no_point_value():
    v = parse_value("35-40")
    assert (v.low, v.high, v.number) == (35.0, 40.0, None)
    assert str(QualityFlag.RANGE_VALUE) in v.flags


def test_comparator_is_never_turned_into_a_number():
    for text in ("<0.5", ">150", "<=2", ">= 3.5"):
        v = parse_value(text)
        assert v.number is None, text
        assert v.text == text.strip()
        assert str(QualityFlag.COMPARATOR_VALUE) in v.flags


def test_sentinel_text():
    v = parse_value("see below")
    assert v.text == "see below" and v.number is None
    assert str(QualityFlag.NON_NUMERIC_RESULT) in v.flags


def test_free_text_diagnosis():
    v = parse_value("SINUS TACHYCARDIA")
    assert v.text == "SINUS TACHYCARDIA" and v.form == "text"
    assert v.flags == []


def test_a_tail_that_is_not_a_unit_makes_the_whole_cell_text():
    """`2 SINUS TACHYCARDIA` is an ECG diagnosis, not two of something (T1.9, P-C8).

    Without a unit table the old rule read any word after a number as a unit, which is
    how twenty-six diagnoses became measurements. The table is what says no.
    """
    spec = ValueParsingSpec(known_units=frozenset({"mg", "k/cu mm"}))
    v = parse_value("2 SINUS TACHYCARDIA", spec=spec)
    assert v.number is None and v.unit is None and v.form == "text"
    assert v.text == "2 SINUS TACHYCARDIA"
    assert str(QualityFlag.NON_NUMERIC_RESULT) in v.flags, "a numbered line is worth marking"
    known = parse_value("38.62 K/cu mm", spec=spec)
    assert known.number == 38.62 and known.unit == "K/cu mm" and known.form == "number_unit"
    # a cell that is plain text was never a candidate and earns no marker
    assert parse_value("SINUS TACHYCARDIA", spec=spec).flags == []


def test_a_numbered_line_is_quarantined_where_a_number_is_required():
    spec = ValueParsingSpec(known_units=frozenset({"mg"}))
    with pytest.raises(QuarantineRow):
        parse_value("2 SINUS TACHYCARDIA", spec=spec, expect="numeric")


def test_signature_line_is_flagged_not_treated_as_a_result():
    v = parse_value("Confirmed by SMITH, J on 2018-03-04")
    assert str(QualityFlag.SIGNATURE_LINE) in v.flags
    assert v.number is None


def test_unparseable_value_is_quarantined_when_a_number_is_expected():
    with pytest.raises(QuarantineRow):
        parse_value("approximately three-ish", expect="numeric")


def test_numeric_expectation_still_accepts_the_documented_forms():
    for text in ("4.2", "<0.5", "35-40", "see below"):
        parse_value(text, expect="numeric")


def test_null_and_blank_are_absent_not_zero():
    for raw in (None, "", "   ", "NULL"):
        v = parse_value(raw)
        assert v.is_absent and v.number is None and v.text is None


def test_number_and_text_never_carry_the_same_meaning():
    for text in ("4.2", "<0.5", "35-40", "see below", "SINUS TACHYCARDIA", "38.62 mg"):
        v = parse_value(text)
        assert not (v.number is not None and v.form in {"comparator", "sentinel", "text"})


def test_native_numeric_cells_pass_through():
    assert parse_value(7).number == 7.0
    assert parse_value(7.5).number == 7.5


def test_sentinels_are_configurable_per_dataset():
    spec = ValueParsingSpec(sentinels=("nicht bestimmt",))
    assert str(QualityFlag.NON_NUMERIC_RESULT) in parse_value("nicht bestimmt", spec=spec).flags
    assert parse_value("see below", spec=spec).form == "text"


def test_negative_and_scientific_numbers():
    assert parse_value("-3.5").number == -3.5
    assert parse_value("1.2e3").number == 1200.0


def test_reversed_range_is_not_read_as_a_range():
    """40-35 is not a range; treating it as one would invent an ordering."""
    v = parse_value("40-35")
    assert v.low is None and v.form in {"text", "number_unit"}


def test_a_dose_too_large_for_the_target_column_becomes_no_quantity():
    """MIMIC-IV's emar_detail carries doses that are identifiers, not amounts.

    Sixteen rows give a dose_given of eighteen digits. Parsed faithfully they overflow
    OMOP's ``quantity NUMERIC``, and the choice is between inventing a rounded quantity,
    losing the build, and publishing no quantity while keeping the string. Only the
    third preserves what the source said without asserting a dose.
    """
    import duckdb

    from ehr2trace.omop import QUANTITY_LIMIT, _build_dose_map

    con = duckdb.connect()
    con.execute("CREATE TABLE evt (dose_source VARCHAR)")
    con.executemany("INSERT INTO evt VALUES (?)",
                    [("500110360505613004 mcg",), ("400 mg",)])
    _build_dose_map(con)
    got = dict((d, q) for d, q, _ in con.execute(
        "SELECT dose_source, quantity, dose_unit FROM dose_map").fetchall())
    assert got["400 mg"] == 400
    assert got["500110360505613004 mcg"] is None

    # The bound is the column's, not a round number picked to pass this test.
    con.execute("CREATE TABLE probe (q NUMERIC)")
    con.execute(f"INSERT INTO probe VALUES ({QUANTITY_LIMIT - 1})")
    try:
        con.execute(f"INSERT INTO probe VALUES ({QUANTITY_LIMIT})")
    except duckdb.ConversionException:
        pass
    else:
        raise AssertionError("QUANTITY_LIMIT is looser than the column it describes")


def test_routing_clause_survives_being_anded_with_an_exclusion():
    """`WHERE {routes} AND NOT excluded` must exclude from every branch, not the last.

    The routing clause is a disjunction. Unparenthesised, SQL reads the caller's
    conjunction as `A OR (B AND NOT excluded)`, so rows arriving through A keep their
    exemption. Nothing reports it: the rows are well formed and the query is valid.
    This asserts the behaviour rather than the string, by running it.
    """
    import duckdb

    from ehr2trace.omop import _routes_here
    from ehr2trace.schema import EventKind

    con = duckdb.connect()
    con.execute("CREATE TABLE e (event_kind VARCHAR, flags VARCHAR[])")
    con.execute("CREATE TABLE m (domain_id VARCHAR)")
    # One row per branch of the disjunction, both excluded by the flag.
    con.execute("INSERT INTO e VALUES ('drug_admin', ['NOT_ADMINISTERED'])")   # unmapped branch
    con.execute("INSERT INTO m VALUES (NULL)")
    routes = _routes_here("drug_exposure", (str(EventKind.drug_order), str(EventKind.drug_admin)))
    n = con.execute(
        f"SELECT count(*) FROM e, m WHERE {routes} "
        f"AND NOT list_contains(e.flags, 'NOT_ADMINISTERED')"
    ).fetchone()[0]
    assert n == 0, "an excluded row reached the table through the unmapped branch"


def test_surrogate_ids_are_totally_ordered_when_a_code_fans_out():
    """One event mapping to two concepts must number its rows the same way every run.

    ``ORDER BY event_id`` leaves such rows tied, and a tie lets the engine number them
    either way. The rows' content still agrees, so only a digest of the whole table
    disagrees -- which is how this was found, by the tables with no fan-out reproducing
    and the two with it not.
    """
    import duckdb

    from ehr2trace.omop import SURROGATE_ORDER

    con = duckdb.connect()
    con.execute("CREATE TABLE e (event_id VARCHAR)")
    con.execute("CREATE TABLE m (event_id VARCHAR, concept_id BIGINT)")
    con.execute("INSERT INTO e VALUES ('evt1'), ('evt2')")
    # evt1 maps to two standard concepts; evt2 to one.
    con.executemany("INSERT INTO m VALUES (?, ?)",
                    [("evt1", 200), ("evt1", 100), ("evt2", 300)])
    sql = (f"SELECT row_number() OVER (ORDER BY {SURROGATE_ORDER}) AS id, e.event_id, m.concept_id "
           "FROM e JOIN m ON m.event_id = e.event_id")
    first = con.execute(sql).fetchall()
    # The same query under a different plan must assign the same ids.
    con.execute("SET threads=1")
    assert con.execute(sql).fetchall() == first
    # And the tie is broken by concept id rather than left to the engine.
    assert [(r[1], r[2]) for r in sorted(first)] == [
        ("evt1", 100), ("evt1", 200), ("evt2", 300)]
