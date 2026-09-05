"""The core package must not know which hospital produced the data (checklist P5-1).

Generality here is not an aspiration, it is a property that can be checked: if a
partition directory, a file name, a sheet name, a column name or a cohort label appears
anywhere in ``src/``, then onboarding a new export means editing the core, and the
design's central claim is false.

Those strings are allowed in exactly three places: ``datasets/*.yaml``,
``tests/fixtures/`` and documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Patterns drawn from the reference export. Word boundaries matter: "dos" is a column
#: name in that export and also a substring of "dose", which is a legitimate generic
#: word this package uses.
FORBIDDEN = {
    "partition directory": re.compile(r"has_embolism|no_embolism|has_pulmonary_embolism|no_pulmonary_embolism"),
    "batch id": re.compile(r"\b29b?_(?:has|no)\b"),
    "patient key column": re.compile(r"\bMRN\b|\bEncounter_CSN\b"),
    "anchor column": re.compile(r"\bdos\b|\bClosest_to_CT\b"),
    "source column": re.compile(
        r"\bBase_Name\b|\bLab_Name\b|\bMedication_Name\b|\bHV_Discrete_Dose\b|"
        r"\bOrdering_Date\b|\bOrder_Status\b|\bDate_Administered\b|\bFirst_Noted_Date\b|"
        r"\bDiagnosis_Code\b|\bResult_Time\b|\bCollection_time\b|\bComponent_Name\b|"
        r"\bProcedure_Name\b|\bHosp_Admsn_Time\b|\bLength_of_stay_days\b|\bBP_Systolic\b"
    ),
    "sheet name": re.compile(r"PFT Narrative|PFT Values|Medication Administration"),
    "cohort label": re.compile(r"pulmonary[ _]embolism|\bCTPE\b|\bCTPA\b|\bctpa\b"),
    "pe diagnosis code": re.compile(r"\bI26\.\d|\bI26\d{2}\b|\b415\.1\d?\b"),
    # Site abbreviations reach the core through medication names rather than columns:
    # a drug is written `MIDAZOLAM 1 MG/ML INJECTION SOLUTION JHM`, and stripping `JHM`
    # in the converter is the same mistake as hardcoding a column, one step further in.
    "site abbreviation": re.compile(r"\bJHH\b|\bJHM\b|\bHCGH\b|\bBMC\b|\bSMH\b|\bUBER\b|\bMBP\b"),
}

ALLOWED_PATHS = ("datasets/", "tests/fixtures/", "docs/", "sql/")


def source_files() -> list[Path]:
    src = Path(__file__).resolve().parents[1] / "src"
    return sorted(p for p in src.rglob("*.py") if "__pycache__" not in str(p))


@pytest.mark.parametrize("label,pattern", sorted(FORBIDDEN.items()))
def test_core_code_contains_no_dataset_specific_strings(label: str, pattern: re.Pattern):
    offenders: list[str] = []
    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:100]}")
    assert not offenders, f"{label} leaked into core code:\n" + "\n".join(offenders)


def test_the_guard_would_actually_catch_a_violation():
    """A grep test that cannot fail is worse than no test."""
    assert FORBIDDEN["anchor column"].search("anchor_time = row['dos']")
    assert FORBIDDEN["patient key column"].search("person = row['MRN']")
    assert not FORBIDDEN["anchor column"].search("dose_source = row.text('dose')")
    assert FORBIDDEN["site abbreviation"].search('NOISE = ("UBER", "MBP")')
    assert not FORBIDDEN["site abbreviation"].search("uber_generic = False")
    assert not FORBIDDEN["patient key column"].search("person_source_id")


def test_dataset_yaml_is_where_those_strings_belong():
    text = (Path(__file__).resolve().parents[1] / "datasets" / "ctpe.yaml").read_text(encoding="utf-8")
    assert FORBIDDEN["patient key column"].search(text)
    assert FORBIDDEN["anchor column"].search(text)
    assert FORBIDDEN["sheet name"].search(text)


def test_no_concept_ids_are_hardcoded():
    """Concept ids belong in the vocabulary or a reviewed CSV, never in code."""
    suspicious = re.compile(r"concept_id\s*=\s*(\d+)")
    offenders = []
    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in suspicious.finditer(line):
                # 0 is the OMOP convention for "no matching concept" and is not an id.
                if match.group(1) != "0":
                    offenders.append(f"{path.name}:{number}: {line.strip()[:100]}")
    assert not offenders, "hardcoded concept ids:\n" + "\n".join(offenders)


def test_prompts_contain_no_concept_ids_either():
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    for path in sorted(prompts.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{6,}\b", text), f"{path.name} contains something id-shaped"
