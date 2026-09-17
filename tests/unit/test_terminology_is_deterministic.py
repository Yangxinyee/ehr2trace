"""The same code must resolve to the same concept on every run.

An ICD-10-CM code often carries more than one `Maps to` row: 26,562 of them map to
several standard SNOMED concepts. The resolver publishes one concept per event, and it
used to take whichever of those rows the query returned first -- with no `ORDER BY`, so
the winner depended on how DuckDB happened to parallelise the join. Two builds of the
same data mapped the same diagnoses to different concepts, and nothing said so: both
were valid OMOP, both had identical row counts, and the difference was only visible when
the reproducibility experiment compared a rebuild against the original at full scale.

These tests need a real vocabulary and skip without one. The property they assert is not
that the resolver picks the *right* concept when the vocabulary offers several -- it
cannot know that, which is why such mappings are now recorded as ambiguous -- but that it
picks the same one every time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ehr2trace.terminology import MappingRegistry, TermRequest, Vocabulary, resolve_terms_batch

from tests.unit.test_unpunctuated_codes import write_vocab

VOCAB = os.environ.get("OMOP_VOCAB_DIR")

CSV_OPTIONS = "delim='\\t', header=true, all_varchar=true, quote=''"


def sample_codes(limit: int = 1500) -> list[str]:
    """Non-standard ICD-10-CM codes, written without their decimal point.

    Non-standard codes are the ones that go through the `Maps to` join, which is where
    the ambiguity is; writing them unpunctuated also exercises the second pass.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    rows = con.execute(
        f"""SELECT concept_code
            FROM read_csv('{Path(VOCAB) / "CONCEPT.csv"}', {CSV_OPTIONS})
            WHERE vocabulary_id = 'ICD10CM' AND standard_concept IS NULL
            ORDER BY concept_code LIMIT {limit}"""
    ).fetchall()
    con.close()
    return [r[0].replace(".", "") for r in rows]


def resolve(codes: list[str], threads: int) -> dict:
    vocabulary = Vocabulary.open(Path(VOCAB))
    try:
        # The thread count is what changed the join's output order, so it is what the
        # test varies. Anything that reorders the scan would do.
        vocabulary.con.execute(f"SET threads = {threads}")
        requests = [
            TermRequest(code_system="ICD10CM", source_code=code, source_name="",
                        event_kind="condition", occurrences=1)
            for code in codes
        ]
        resolved, _unresolved = resolve_terms_batch(requests, vocabulary, MappingRegistry())
        return {key: (match.concept_id, match.path) for key, match in resolved.items()}
    finally:
        vocabulary.close()


# -- the invariant, on a vocabulary small enough to ship ------------------------------


FORKED = (
    "45571738\tType 2 diabetes with hyperglycaemia\tCondition\tICD10CM\tbilling\t\tE11.65"
    "\t19700101\t20991231\t\n"
    "201826\tType 2 diabetes mellitus\tCondition\tSNOMED\tfinding\tS\t44054006"
    "\t19700101\t20991231\t\n"
    "4184637\tHyperglycaemia\tCondition\tSNOMED\tfinding\tS\t80394007"
    "\t19700101\t20991231\t\n"
)
FORKED_LINKS = (
    "45571738\t201826\tMaps to\t19700101\t20991231\t\n"
    "45571738\t4184637\tMaps to\t19700101\t20991231\t\n"
)


def resolve_once(directory: Path, code: str):
    vocabulary = Vocabulary.open(directory)
    try:
        term = TermRequest("ICD10CM", code, None, "condition", 1)
        resolved, _unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
        return resolved.get(term.key)
    finally:
        vocabulary.close()


def test_a_code_with_two_targets_always_yields_the_same_one(tmp_path):
    directory = write_vocab(tmp_path / "v", FORKED, FORKED_LINKS)
    picks = {resolve_once(directory, "E11.65").concept_id for _ in range(5)}
    assert picks == {201826}, "the mapping is not stable across runs"


def test_the_choice_between_two_targets_is_recorded_rather_than_hidden(tmp_path):
    directory = write_vocab(tmp_path / "v", FORKED, FORKED_LINKS)
    match = resolve_once(directory, "E11.65")
    assert match.path == "mapped_relationship_ambiguous"


def test_a_code_with_one_target_is_not_marked_ambiguous(tmp_path):
    single = FORKED.replace(
        "4184637\tHyperglycaemia\tCondition\tSNOMED\tfinding\tS\t80394007"
        "\t19700101\t20991231\t\n", ""
    )
    links = "45571738\t201826\tMaps to\t19700101\t20991231\t\n"
    directory = write_vocab(tmp_path / "v", single, links)
    assert resolve_once(directory, "E11.65").path == "mapped_relationship"


def test_the_unpunctuated_pass_is_ambiguity_aware_too(tmp_path):
    directory = write_vocab(tmp_path / "v", FORKED, FORKED_LINKS)
    match = resolve_once(directory, "E1165")
    assert match.concept_id == 201826
    assert match.path == "unpunctuated_mapped_relationship_ambiguous"


# -- the same property against a real vocabulary, where the ambiguity is common -------


@pytest.fixture(scope="module")
def both() -> tuple[dict, dict]:
    if not VOCAB:
        pytest.skip("needs a local OMOP vocabulary")
    codes = sample_codes()
    return resolve(codes, threads=1), resolve(codes, threads=16)


def test_the_thread_count_does_not_change_a_single_mapping(both):
    one, many = both
    assert one, "nothing resolved, so agreement would be vacuous"
    differing = sorted(k for k in set(one) | set(many) if one.get(k) != many.get(k))
    assert differing == [], f"{len(differing)} codes resolved differently: {differing[:5]}"


def test_a_code_with_several_targets_is_recorded_as_ambiguous(both):
    one, _ = both
    ambiguous = [k for k, (_id, path) in one.items() if path.endswith("_ambiguous")]
    # Not a fixed count -- it depends on the vocabulary release -- but a vocabulary in
    # which no ICD-10-CM code has competing targets would mean the flag is never
    # exercised and this test is not testing anything.
    assert ambiguous, "no ambiguous mapping in the sample; the flag is untested"


def test_every_path_names_how_the_mapping_was_reached(both):
    one, _ = both
    known = {
        "exact_code", "mapped_relationship",
        "unpunctuated_code", "unpunctuated_mapped_relationship",
    }
    for _key, (_concept_id, path) in one.items():
        base = path[: -len("_ambiguous")] if path.endswith("_ambiguous") else path
        assert base in known, f"unknown resolution path {path!r}"


def test_a_build_refuses_a_missing_mapping_registry(tmp_path, monkeypatch):
    from ehr2trace.errors import BlockerError
    from ehr2trace.terminology import load_build_mappings

    monkeypatch.setenv("EHR_MAPPINGS_DIR", str(tmp_path / "absent"))
    with pytest.raises(BlockerError, match="EHR_MAPPINGS_DIR"):
        load_build_mappings()
    (tmp_path / "present").mkdir()
    monkeypatch.setenv("EHR_MAPPINGS_DIR", str(tmp_path / "present"))
    assert load_build_mappings().entries == {}
