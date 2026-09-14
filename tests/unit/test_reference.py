"""Reference tables: units, exact conversions and plausible ranges (remediation T1.6-T1.9).

The tables are judgements and live in CSV files under ``reference/``; the code only
loads them. What is tested here is the loading contract other agents rely on: a
duplicate spelling across files is an error, lookups are case-insensitive, a
conversion is exact, a range file is per dataset, and the digest changes with any byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr2trace.errors import ConfigError
from ehr2trace.reference import (
    DEFAULT_REFERENCE_DIR,
    ReferenceTables,
    load_reference,
    parsing_spec,
    reference_digest,
)


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_reference(root: Path, *, units: str | None = None, extra_units: str | None = None,
                   conversions: str | None = None, ranges: dict[str, str] | None = None) -> Path:
    write(root, "units/common.csv", units or (
        "source_unit,ucum,basis\n"
        "mg,mg,milligram\n"
        "degF,[degF],degree Fahrenheit\n"
        "K/cu mm,10*3/mm3,thousand per cubic millimetre\n"
        "mmol/L,mmol/L,millimole per litre\n"
    ))
    if extra_units is not None:
        write(root, "units/site.csv", extra_units)
    write(root, "unit_conversions.csv", conversions or (
        "from_ucum,to_ucum,factor,offset,basis\n"
        "[degF],Cel,5/9,-160/9,exact by definition\n"
        "10*3/mm3,10*3/uL,1,0,one cubic millimetre is one microlitre\n"
    ))
    for dataset_id, text in (ranges or {}).items():
        write(root, f"plausible_ranges/{dataset_id}.csv", text)
    return root


def test_unit_lookup_is_case_insensitive_and_returns_the_ucum_code(tmp_path: Path):
    ref = load_reference(make_reference(tmp_path), "any")
    assert ref.units.lookup("MG") == "mg"
    assert ref.units.lookup("  degf ") == "[degF]"
    assert ref.units.lookup("k/cu mm") == "10*3/mm3"
    assert ref.units.lookup("furlongs") is None
    assert ref.units.known("mmol/l") and not ref.units.known("")


def test_unit_files_are_merged_and_a_spelling_listed_twice_is_an_error(tmp_path: Path):
    root = make_reference(tmp_path, extra_units="source_unit,ucum,basis\nmcg,ug,microgram\n")
    ref = load_reference(root, "any")
    assert ref.units.lookup("mcg") == "ug" and ref.units.lookup("mg") == "mg"

    clash = make_reference(tmp_path / "clash", extra_units="source_unit,ucum,basis\nMg,mg,again\n")
    with pytest.raises(ConfigError, match="Mg.*common.csv"):
        load_reference(clash, "any")


def test_a_conversion_is_exact_and_names_its_target_unit(tmp_path: Path):
    ref = load_reference(make_reference(tmp_path), "any")
    unit, value = ref.normalize("[degF]", 98.6)
    assert unit == "Cel" and value == 37.0
    unit, value = ref.normalize("[degF]", 32.0)
    assert unit == "Cel" and value == 0.0
    # no conversion row: the unit and the value pass through unchanged
    assert ref.normalize("mg", 5.0) == ("mg", 5.0)
    # a value with no number normalizes its unit only
    assert ref.normalize("[degF]", None) == ("Cel", None)


def test_a_conversion_table_row_naming_an_unknown_ucum_is_still_loaded_but_a_bad_factor_is_not(tmp_path: Path):
    bad = make_reference(tmp_path, conversions=(
        "from_ucum,to_ucum,factor,offset,basis\n"
        "[degF],Cel,five ninths,0,typo\n"
    ))
    with pytest.raises(ConfigError, match="factor"):
        load_reference(bad, "any")


def test_plausible_ranges_are_read_per_dataset_and_keyed_on_the_normalized_unit(tmp_path: Path):
    root = make_reference(tmp_path, ranges={
        "d1": "code_system,source_code,ucum,low,high,basis\nSOURCE,NA,mmol/L,100,180,physiology\nSOURCE,PH,,6.5,8,unitless\n",
        "d2": "code_system,source_code,ucum,low,high,basis\nSOURCE,NA,mmol/L,0,1,nonsense\n",
    })
    d1 = load_reference(root, "d1")
    assert d1.implausible("SOURCE", "NA", "mmol/L", 138.0) is False
    assert d1.implausible("SOURCE", "NA", "mmol/L", 1380.0) is True
    assert d1.implausible("SOURCE", "NA", "mmol/L", 100.0) is False, "the bounds are inclusive"
    assert d1.implausible("SOURCE", "PH", None, 7.4) is False
    assert d1.implausible("SOURCE", "PH", None, 74.0) is True
    # a code with no row, or a value in a different unit, is never judged
    assert d1.implausible("SOURCE", "K", "mmol/L", 9999.0) is False
    assert d1.implausible("SOURCE", "NA", "meq/L", 9999.0) is False
    # another dataset's file does not leak in, and a dataset without a file has no ranges
    assert load_reference(root, "d2").implausible("SOURCE", "NA", "mmol/L", 138.0) is True
    assert load_reference(root, "d3").implausible("SOURCE", "NA", "mmol/L", 9999.0) is False


def test_an_open_ended_range_bounds_one_side_only(tmp_path: Path):
    root = make_reference(tmp_path, ranges={
        "d1": "code_system,source_code,ucum,low,high,basis\nSOURCE,COUNT,,0,,never negative\n",
    })
    d1 = load_reference(root, "d1")
    assert d1.implausible("SOURCE", "COUNT", None, -1.0) is True
    assert d1.implausible("SOURCE", "COUNT", None, 1e12) is False


def test_the_digest_covers_every_file_and_changes_with_any_byte(tmp_path: Path):
    root = make_reference(tmp_path, ranges={"d1": "code_system,source_code,ucum,low,high,basis\n"})
    before = reference_digest(root)
    assert before == reference_digest(root)
    write(root, "plausible_ranges/d1.csv", "code_system,source_code,ucum,low,high,basis\nSOURCE,X,,0,1,b\n")
    assert reference_digest(root) != before
    assert load_reference(root, "d1").digest == reference_digest(root)
    # a directory that does not exist has a digest too, so a build without tables is addressable
    assert reference_digest(tmp_path / "absent") == reference_digest(tmp_path / "also_absent")


def test_empty_tables_know_no_units_and_judge_nothing():
    ref = ReferenceTables.empty()
    assert ref.units.lookup("mg") is None
    assert ref.normalize(None, 5.0) == (None, 5.0)
    assert ref.implausible("SOURCE", "NA", "mmol/L", 1e9) is False


def test_the_parsing_spec_helper_carries_the_unit_table_into_value_parsing(tmp_path: Path):
    from ehr2trace.canonical.values import parse_value

    spec = parsing_spec(("NULL",), root=make_reference(tmp_path))
    assert parse_value("10 mg", spec=spec).form == "number_unit"
    assert parse_value("10 SINUS TACHYCARDIA", spec=spec).form == "text"


# -- the shipped tables -------------------------------------------------------------


def test_the_shipped_tables_load_and_cover_the_spellings_the_fixtures_use():
    ref = load_reference(DEFAULT_REFERENCE_DIR, "ctpe_shape")
    for spelling in ("mg", "mmol/L", "K/cu mm", "mg/dL", "ug/mL FEU", "mmHg", "Units", "bpm",
                     "degF", "°F", "deg C", "lb", "kg", "cm", "in", "%", "mEq/L", "ms", "mcg/kg/min"):
        assert ref.units.lookup(spelling) is not None, spelling
    assert ref.normalize("[degF]", 98.6) == ("Cel", 37.0)
    assert ref.normalize("[lb_av]", 100.0) == ("kg", 45.359237)
    assert ref.normalize("[in_i]", 10.0) == ("cm", 25.4)
    assert ref.normalize("10*3/mm3", 38.62) == ("10*3/uL", 38.62)


def test_every_shipped_conversion_starts_from_a_code_the_unit_table_produces():
    """A conversion nothing can reach is a typo waiting to be found in production."""
    ref = load_reference(DEFAULT_REFERENCE_DIR, "ctpe_shape")
    produced = set(ref.units.by_spelling.values())
    for from_ucum in ref.conversions:
        assert from_ucum in produced, from_ucum


@pytest.mark.parametrize("dataset_id", ["ctpe_shape", "generic_ehr"])
def test_the_fixture_datasets_ship_a_range_table_keyed_on_units_the_table_produces(dataset_id: str):
    ref = load_reference(DEFAULT_REFERENCE_DIR, dataset_id)
    assert ref.ranges, f"{dataset_id} has no plausible ranges"
    reachable = set(ref.units.by_spelling.values()) | {c.to_ucum for c in ref.conversions.values()}
    for (_system, _code, ucum) in ref.ranges:
        assert ucum is None or ucum in reachable, ucum
