"""The reference tables and the unit-family heuristic the remediation checks judge by.

Two of the fifteen checks added by the conversion audit turn on questions that look
trivial and are not: are two unit spellings the same quantity, and which copy of an
artifact is this build's? Both are answered by small pure functions, and both are the
kind of thing that is wrong in a way nobody notices until a check reports a clean
dataset. So they are pinned here rather than only through a pipeline run.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ehr2trace.paths import WorkLayout
from ehr2trace.validate import (
    artifact_in_this_tree,
    load_plausible_ranges,
    load_unit_table,
    unit_families,
    unit_family,
)

FIXTURE_REFERENCE = Path(__file__).resolve().parents[1] / "fixtures" / "reference"


def test_the_fixture_unit_table_declares_no_spelling_twice():
    """Files merge at load, so one spelling in two files is a silent contradiction."""
    seen: dict[str, str] = {}
    for path in sorted((FIXTURE_REFERENCE / "units").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = " ".join(row["source_unit"].split()).lower()
                assert key not in seen, f"{key!r} is declared in {seen[key]} and {path.name}"
                assert row["ucum"].strip(), f"{key!r} has no UCUM code in {path.name}"
                assert row["basis"].strip(), f"{key!r} has no basis in {path.name}"
                seen[key] = path.name


def test_a_spelling_resolves_case_insensitively_and_a_ucum_code_resolves_to_itself():
    table = load_unit_table(FIXTURE_REFERENCE)
    assert table is not None
    assert table.ucum("MMOL/L") == table.ucum("mmol/L") == "mmol/L"
    assert table.ucum(" mg/dL ") == "mg/dL"
    # The normalized column already holds UCUM, so a check comparing it must find it.
    assert table.ucum("mm[Hg]") == "mm[Hg]"
    assert table.ucum("no such unit") is None


def test_units_of_the_same_dimension_are_one_family_and_others_are_not():
    table = load_unit_table(FIXTURE_REFERENCE)
    # Prefixes are not dimensions: millimoles and micromoles per litre are one quantity.
    assert unit_family("mmol/L", table, use_dimension=False) == unit_family("umol/L", table)
    assert unit_family("mg/dL", table) != unit_family("mmol/L", table)
    # An annotation is part of the quantity: fibrinogen equivalent units are not the
    # same number as the same mass concentration without them (the D-dimer case).
    assert unit_family("ug/mL FEU", table) != unit_family("mg/dL", table)
    assert len(unit_families({"mmol/L": 90, "umol/L": 10}, table, [])) == 1
    assert len(unit_families({"mmol/L": 90, "mg/dL": 10}, table, [])) == 2
    assert len(unit_families({"ug/mL FEU": 90, "mg/dL": 10}, table, [])) == 2


def test_a_conversion_makes_two_families_one():
    """Two spellings an exact factor links are one unit, however they are written."""
    counts = {"[lb_av]": 90, "kg": 10}
    table = load_unit_table(FIXTURE_REFERENCE)
    assert len(unit_families(counts, table, [])) == 2
    assert len(unit_families(counts, table, [("[lb_av]", "kg")])) == 1


def test_a_plausible_range_table_is_keyed_on_code_and_normalized_unit():
    ranges = load_plausible_ranges("ctpe_shape", FIXTURE_REFERENCE)
    assert ranges, "the fixture declares ranges"
    for (system, code, ucum), (low, high) in ranges.items():
        assert system and code and ucum == ucum.lower()
        assert low < high
    assert load_plausible_ranges("a dataset with no table", FIXTURE_REFERENCE) is None


def test_an_artifact_is_read_from_the_tree_being_validated(tmp_path: Path):
    """A manifest carries absolute paths; a copied work tree must not read the original.

    This is not hypothetical: the fault-injection harness clones a build, and every
    source-layer check would have gone on reading the build it was cloned from.
    """
    original = tmp_path / "first" / "ds"
    clone = tmp_path / "second" / "ds-clone"
    for root in (original, clone):
        (root / "source" / "p1" / "s1").mkdir(parents=True)
        (root / "source" / "p1" / "s1" / "abc.parquet").write_bytes(b"")
    recorded = str(original / "source" / "p1" / "s1" / "abc.parquet")

    layout = WorkLayout(root=clone, dataset_id="ds")
    assert artifact_in_this_tree(layout, recorded) == clone / "source" / "p1" / "s1" / "abc.parquet"

    # Where this tree holds no such file, the recorded path stands, and a path that
    # names nothing at all is None rather than a crash in the middle of a check.
    lonely = WorkLayout(root=tmp_path / "empty", dataset_id="ds")
    assert artifact_in_this_tree(lonely, recorded) == Path(recorded)
    assert artifact_in_this_tree(lonely, str(tmp_path / "nowhere.parquet")) is None
    assert artifact_in_this_tree(lonely, None) is None
