"""The unit-family heuristic and the artifact re-rooting the remediation checks rely on.

Two of the checks added by the conversion audit turn on questions that look trivial and
are not: are two unit spellings the same quantity, and which copy of an artifact is this
build's? Both are answered by small pure functions, and both are the kind of thing that
is wrong in a way nobody notices until a check reports a clean dataset. So they are
pinned here rather than only through a pipeline run. The tables themselves are loaded by
``ehr2trace.reference`` -- the loader the canonical build uses -- and tested beside it.
"""

from __future__ import annotations

from pathlib import Path

from ehr2trace.paths import WorkLayout
from ehr2trace.reference import load_reference
from ehr2trace.validate import (
    artifact_in_this_tree,
    conversion_pairs,
    reportable_spelling,
    unit_families,
    unit_family,
)


def make_reference(root: Path) -> Path:
    (root / "units").mkdir(parents=True)
    (root / "units" / "test.csv").write_text(
        "source_unit,ucum,basis\n"
        "mmol/L,mmol/L,test\n"
        "umol/L,umol/L,test\n"
        "mg/dL,mg/dL,test\n"
        "ng/mL,ng/mL,test\n"
        "ng/mL FEU,ng/mL{FEU},test: fibrinogen equivalent units are a different quantity\n"
        "F,[degF],test\n"
        "C,Cel,test\n"
        "*Unspecified,,what an order screen writes when no unit was chosen\n",
        encoding="utf-8",
    )
    (root / "unit_conversions.csv").write_text(
        "from_ucum,to_ucum,factor,offset,basis\n[degF],Cel,5/9,-160/9,exact\n", encoding="utf-8"
    )
    return root


def test_units_of_one_dimension_are_one_family_and_others_are_not(tmp_path: Path):
    units = load_reference(make_reference(tmp_path), "test").units
    # Prefixes are not dimensions: millimoles and micromoles per litre are one quantity.
    assert unit_family("mmol/L", units) == unit_family("umol/L", units)
    assert unit_family("mg/dL", units) != unit_family("mmol/L", units)
    # An annotation is part of the quantity: a D-dimer in fibrinogen equivalent units is
    # not the same number as the same sample without them.
    assert unit_family("ng/mL FEU", units) != unit_family("ng/mL", units)
    assert len(unit_families({"mmol/L": 90, "umol/L": 10}, units, [])) == 1
    assert len(unit_families({"ng/mL FEU": 90, "ng/mL": 10}, units, [])) == 2


def test_a_conversion_makes_two_families_one(tmp_path: Path):
    """Two spellings an exact factor links are one unit, however they are written."""
    tables = load_reference(make_reference(tmp_path), "test")
    counts = {"F": 90, "C": 10}
    assert len(unit_families(counts, tables.units, [])) == 2
    assert conversion_pairs(tables) == [("[degF]", "Cel")]
    assert len(unit_families(counts, tables.units, conversion_pairs(tables))) == 1


def test_a_report_carries_a_unit_spelling_but_never_a_line_of_text():
    """Free text reached unit columns in the audit, and a report is written to disk."""
    for unit in ("mg/dL", "K/cu mm", "ng/mL FEU", "mcg/kg/min", "*Unspecified"):
        assert reportable_spelling(unit) == unit
    line = "Confirmed by READER, SOMEONE on 2019-03-05"
    shown = reportable_spelling(line)
    assert shown.startswith("text#") and "READER" not in shown and str(len(line)) in shown
    assert reportable_spelling("SMITH, J").startswith("text#"), "a comma is how a name is written"


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
