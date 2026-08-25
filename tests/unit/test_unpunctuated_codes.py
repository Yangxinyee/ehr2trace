"""A code that differs from the vocabulary only in punctuation is the same code.

MIMIC-IV writes ICD-10-CM as `F17210`; the OMOP vocabulary writes `F17.210`. Matching
on the literal string mapped 197 of 19,440 codes -- the three-character ones, which have
no decimal point to disagree about -- and turned the other 99% into `concept_id = 0`.
That is legal OMOP, it passes every structural check, and it is not a true statement
about the data.

The fallback is deliberately narrow: it only accepts a match where exactly one
vocabulary code reduces to the same string, and it records the fact that punctuation was
ignored so a reviewer can tell those mappings apart from literal ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr2cdm.terminology import MappingRegistry, TermRequest, Vocabulary, resolve_terms_batch

HEADER = (
    "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\t"
    "standard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
)


def write_vocab(directory: Path, concepts: str, relationships: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CONCEPT.csv").write_text(HEADER + concepts, encoding="utf-8")
    (directory / "CONCEPT_RELATIONSHIP.csv").write_text(
        "concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
        + relationships,
        encoding="utf-8",
    )
    (directory / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\tvocabulary_concept_id\n"
        "None\tOMOP\tOMOP\tv5.0 01-JAN-26\t44819096\n",
        encoding="utf-8",
    )
    for table in ("DOMAIN", "CONCEPT_CLASS", "RELATIONSHIP"):
        (directory / f"{table}.csv").write_text("a\tb\nx\ty\n", encoding="utf-8")
    return directory


def resolve(directory: Path, code: str):
    vocabulary = Vocabulary.open(directory)
    try:
        term = TermRequest("ICD10CM", code, None, "condition", 1)
        resolved, unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
        return resolved.get(term.key), unresolved
    finally:
        vocabulary.close()


def test_a_code_written_without_its_decimal_point_still_resolves(tmp_path):
    write_vocab(
        tmp_path / "v",
        "45571738\tNicotine dependence\tCondition\tICD10CM\tbilling\t\tF17.210\t19700101\t20991231\t\n"
        "316139\tNicotine dependence\tCondition\tSNOMED\tfinding\tS\t56294008\t19700101\t20991231\t\n",
        "45571738\t316139\tMaps to\t19700101\t20991231\t\n",
    )
    match, unresolved = resolve(tmp_path / "v", "F17210")
    assert match is not None, "F17210 did not resolve to F17.210"
    assert match.concept_id == 316139
    assert not unresolved


def test_the_path_records_that_punctuation_was_ignored(tmp_path):
    """A reviewer has to be able to tell these apart from a literal match."""
    write_vocab(
        tmp_path / "v",
        "45571738\tNicotine dependence\tCondition\tICD10CM\tbilling\t\tF17.210\t19700101\t20991231\t\n"
        "316139\tNicotine dependence\tCondition\tSNOMED\tfinding\tS\t56294008\t19700101\t20991231\t\n",
        "45571738\t316139\tMaps to\t19700101\t20991231\t\n",
    )
    exact, _ = resolve(tmp_path / "v", "F17.210")
    loose, _ = resolve(tmp_path / "v", "F17210")
    assert exact.path == "mapped_relationship"
    assert loose.path == "unpunctuated_mapped_relationship"


def test_an_ambiguous_reduction_is_refused(tmp_path):
    """Two codes reducing to the same string must not silently pick one.

    This does not happen in ICD-10-CM today -- 100,035 codes reduce to 100,035 distinct
    strings -- but the guard is checked at runtime rather than assumed, because a
    vocabulary where it failed would otherwise map a diagnosis to the wrong concept.
    """
    write_vocab(
        tmp_path / "v",
        "1\tOne\tCondition\tICD10CM\tbilling\tS\tA1.23\t19700101\t20991231\t\n"
        "2\tTwo\tCondition\tICD10CM\tbilling\tS\tA12.3\t19700101\t20991231\t\n",
    )
    match, unresolved = resolve(tmp_path / "v", "A123")
    assert match is None, "an ambiguous reduction was resolved anyway"
    assert len(unresolved) == 1


def test_a_code_that_is_simply_absent_still_does_not_resolve(tmp_path):
    """The fallback must not turn 'not in the vocabulary' into a match."""
    write_vocab(
        tmp_path / "v",
        "1\tOne\tCondition\tICD10CM\tbilling\tS\tF17.210\t19700101\t20991231\t\n",
    )
    match, unresolved = resolve(tmp_path / "v", "Z9999")
    assert match is None
    assert len(unresolved) == 1
