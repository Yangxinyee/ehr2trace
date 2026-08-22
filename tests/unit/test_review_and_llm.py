"""The review loop and the model boundary (checklist P4-1 to P4-3).

The point of both is what they refuse to do. A proposal nobody accepted must never
become a mapping, and a concept id the model was not given must never survive.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ehr2cdm.llm import CandidateRanking, LlmCall, LlmClient, is_safe_sample, load_template
from ehr2cdm.paths import WorkLayout
from ehr2cdm.review import (
    compile_decisions,
    item_id,
    read_decisions,
    read_pending,
    undecided_ids,
    write_pending,
)
from ehr2cdm.terminology import Candidate, MappingRegistry


@pytest.fixture()
def layout(tmp_path: Path) -> WorkLayout:
    return WorkLayout(root=tmp_path / "work" / "t", dataset_id="t").ensure()


def proposal(source_string: str, **over) -> dict:
    base = {
        "kind": "terminology",
        "code_system": "ICD10CM",
        "source_string": source_string,
        "source_name": source_string.lower(),
        "event_kind": "condition",
        "occurrences": 3,
        "candidates": "[]",
        "context": "domain=Condition",
    }
    base.update(over)
    return base


def test_pending_ids_are_stable_across_reproposal(layout: WorkLayout):
    """Re-proposing after new data must not renumber decisions a human already made."""
    write_pending(layout, [proposal("I50.9")])
    first = read_pending(layout)[0]["id"]
    write_pending(layout, [proposal("I50.9", occurrences=99), proposal("E11.9")])
    rows = {r["source_string"]: r for r in read_pending(layout)}
    assert rows["I50.9"]["id"] == first
    assert rows["I50.9"]["occurrences"] == "99"
    assert len(rows) == 2


def test_item_id_ignores_case_and_spacing_but_not_the_string():
    assert item_id("terminology", "ICD10CM", "Heart Failure") == item_id(
        "terminology", "ICD10CM", "heart  failure"
    )
    assert item_id("terminology", "ICD10CM", "I50.9") != item_id("terminology", "ICD10CM", "I50.8")


def test_proposing_creates_an_empty_decisions_file_for_the_reviewer(layout: WorkLayout):
    write_pending(layout, [proposal("I50.9")])
    assert (layout.review_dir / "decisions.csv").exists()
    assert read_decisions(layout) == {}


def write_decision(layout: WorkLayout, item: str, **fields) -> None:
    path = layout.review_dir / "decisions.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []
    from ehr2cdm.review import DECISION_FIELDS

    rows.append({**{k: "" for k in DECISION_FIELDS}, "id": item, **fields})
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DECISION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_only_accepted_decisions_compile_into_mappings(layout: WorkLayout, tmp_path: Path):
    write_pending(layout, [proposal("I50.9"), proposal("E11.9"), proposal("J45.909")])
    rows = {r["source_string"]: r["id"] for r in read_pending(layout)}
    write_decision(layout, rows["I50.9"], decision="accept", concept_id="316139",
                   concept_name="Heart failure", domain_id="Condition",
                   vocabulary_id="SNOMED", reviewer="clinician", decided_on="2026-08-20")
    write_decision(layout, rows["E11.9"], decision="reject", note="not a good match")
    # J45.909 is left undecided entirely

    mappings = tmp_path / "mappings"
    assert compile_decisions(layout, mappings) == 1
    compiled = list(csv.DictReader((mappings / "condition.csv").open(encoding="utf-8")))
    assert [r["source_string"] for r in compiled] == ["I50.9"]
    assert compiled[0]["decided_by"] == "clinician"


def test_an_accepted_decision_without_a_concept_id_is_not_compiled(layout: WorkLayout, tmp_path: Path):
    write_pending(layout, [proposal("I50.9")])
    write_decision(layout, read_pending(layout)[0]["id"], decision="accept", concept_id="")
    assert compile_decisions(layout, tmp_path / "mappings") == 0


def test_undecided_items_are_reported_as_such(layout: WorkLayout):
    write_pending(layout, [proposal("I50.9"), proposal("E11.9")])
    rows = {r["source_string"]: r["id"] for r in read_pending(layout)}
    write_decision(layout, rows["I50.9"], decision="accept", concept_id="316139", domain_id="Condition")
    assert undecided_ids(layout) == {rows["E11.9"]}


def test_compiled_mappings_are_what_the_registry_reads_back(layout: WorkLayout, tmp_path: Path):
    write_pending(layout, [proposal("I50.9")])
    write_decision(layout, read_pending(layout)[0]["id"], decision="accept", concept_id="316139",
                   concept_name="Heart failure", domain_id="Condition", vocabulary_id="SNOMED")
    mappings = tmp_path / "mappings"
    compile_decisions(layout, mappings)
    registry = MappingRegistry.load(mappings)
    match = registry.get("ICD10CM", "I50.9")
    assert match is not None and match.concept_id == 316139
    assert registry.get("ICD10CM", "E11.9") is None


# -- the model boundary ---------------------------------------------------------


def client() -> LlmClient:
    return LlmClient(base_url="http://127.0.0.1:9/v1", model="test-model")


def test_a_concept_id_the_model_was_not_given_is_rejected(monkeypatch):
    """The mechanical guarantee that a concept id cannot come from the model's memory."""
    c = client()
    candidates = [Candidate(1, "Heart failure", "Condition", "SNOMED", 1.0)]
    monkeypatch.setattr(
        LlmClient,
        "_ask",
        lambda self, *a, **k: CandidateRanking(
            ranking=[{"concept_id": 999999, "rank": 1, "rationale": "recalled"}], rationale=""
        ),
    )
    c.calls.append(LlmCall(template="terminology_ranking", template_sha256="h", input_sha256="i"))
    assert c.rank_candidates("heart failure", "Condition", candidates) is None
    assert c.calls[-1].ok is False
    assert c.calls[-1].error == "concept_id_not_in_candidates"


def test_a_ranking_over_supplied_candidates_is_accepted(monkeypatch):
    c = client()
    candidates = [Candidate(1, "Heart failure", "Condition", "SNOMED", 1.0)]
    monkeypatch.setattr(
        LlmClient,
        "_ask",
        lambda self, *a, **k: CandidateRanking(
            ranking=[{"concept_id": 1, "rank": 1, "rationale": "same condition"}], rationale="ok"
        ),
    )
    result = c.rank_candidates("heart failure", "Condition", candidates)
    assert result is not None and result.as_payload()[0]["concept_id"] == 1


def test_offline_mode_refuses_a_non_local_endpoint(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    with pytest.raises(RuntimeError, match="not local"):
        LlmClient.from_env()


def test_local_endpoints_are_allowed(monkeypatch):
    monkeypatch.setenv("OFFLINE_MODE", "1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    assert LlmClient.from_env().model == "m"


def test_identifier_shaped_samples_are_never_sent():
    assert is_safe_sample("7.31")
    assert is_safe_sample("SINUS TACHYCARDIA")
    assert not is_safe_sample("ZQ99000001")
    assert not is_safe_sample("2018-03-03")
    assert not is_safe_sample("123-45-6789")
    assert not is_safe_sample("x" * 60)


def test_prompt_templates_are_versioned_by_content():
    text, digest = load_template("terminology_ranking")
    assert "never" in text.lower()
    assert digest == load_template("terminology_ranking")[1]


# -- vocabulary loading ---------------------------------------------------------


def write_vocab(directory: Path) -> Path:
    """A three-concept vocabulary in the shape Athena ships: tab-delimited .csv."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CONCEPT.csv").write_text(
        "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\t"
        "standard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
        "316139\tHeart failure\tCondition\tSNOMED\tClinical Finding\tS\t84114007\t"
        "19700101\t20991231\t\n"
        "45571738\tHeart failure unspecified\tCondition\tICD10CM\t4-char billing code\t\t"
        "I50.9\t19700101\t20991231\t\n"
        "40213260\tOther test\tMeasurement\tLOINC\tLab Test\tS\t12345-6\t"
        "19700101\t20991231\t\n",
        encoding="utf-8",
    )
    (directory / "CONCEPT_RELATIONSHIP.csv").write_text(
        "concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\t"
        "invalid_reason\n"
        "45571738\t316139\tMaps to\t19700101\t20991231\t\n",
        encoding="utf-8",
    )
    (directory / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\t"
        "vocabulary_concept_id\n"
        "None\tOMOP Standardized Vocabularies\tOMOP\tv5.0 01-JAN-26\t44819096\n"
        "SNOMED\tSNOMED CT\thttp://snomed.info\t2026-01-31\t44819097\n",
        encoding="utf-8",
    )
    for table in ("DOMAIN", "CONCEPT_CLASS", "RELATIONSHIP"):
        (directory / f"{table}.csv").write_text("a\tb\nx\ty\n", encoding="utf-8")
    return directory


def test_a_vocabulary_directory_actually_loads(tmp_path: Path):
    """The loader is only ever exercised when a real vocabulary exists.

    It used to pass the file path as a bound parameter to a CREATE TABLE, which DuckDB
    refuses to prepare — so it worked in every test that had no vocabulary and failed
    the instant one was supplied. This test supplies one.
    """
    from ehr2cdm.terminology import Vocabulary

    vocab = Vocabulary.open(write_vocab(tmp_path / "vocab"))
    try:
        assert vocab.available, "a directory with CONCEPT.csv must not fall back to null"
        assert vocab.concept_exists(316139)
        assert not vocab.concept_exists(999999999)
        assert vocab.domain_of(316139) == "Condition"
        assert vocab.version == "v5.0 01-JAN-26", "the bundle version is recorded"
    finally:
        vocab.close()


def test_a_non_standard_source_code_is_followed_to_its_standard_concept(tmp_path: Path):
    """The deterministic path: source code -> source concept -> 'Maps to' -> standard."""
    from ehr2cdm.terminology import Vocabulary

    vocab = Vocabulary.open(write_vocab(tmp_path / "vocab"))
    try:
        match = vocab.lookup_code("ICD10CM", "I50.9")
        assert match is not None
        assert match.concept_id == 316139, "must land on the standard SNOMED concept"
        assert match.source_concept_id == 45571738, "and remember where it came from"
        assert match.path == "mapped_relationship"
        assert match.domain_id == "Condition"
    finally:
        vocab.close()


def test_an_unknown_code_maps_to_nothing_rather_than_something_close(tmp_path: Path):
    from ehr2cdm.terminology import Vocabulary

    vocab = Vocabulary.open(write_vocab(tmp_path / "vocab"))
    try:
        assert vocab.lookup_code("ICD10CM", "Z99.999") is None
        assert vocab.lookup_code("SNOMED", "I50.9") is None, "wrong vocabulary is a miss"
    finally:
        vocab.close()


def test_an_unreadable_version_string_does_not_fail_the_load(tmp_path: Path):
    """The version is metadata. A bundle that omits it is still perfectly usable."""
    from ehr2cdm.terminology import Vocabulary

    directory = write_vocab(tmp_path / "vocab")
    (directory / "VOCABULARY.csv").write_text("a\tb\nx\ty\n", encoding="utf-8")
    vocab = Vocabulary.open(directory)
    try:
        assert vocab.available
        assert vocab.version == "unknown"
        assert vocab.lookup_code("ICD10CM", "I50.9") is not None
    finally:
        vocab.close()


def test_lexical_recall_surfaces_candidates_without_adopting_them(tmp_path: Path):
    from ehr2cdm.terminology import Vocabulary

    vocab = Vocabulary.open(write_vocab(tmp_path / "vocab"))
    try:
        candidates = vocab.candidates("heart failure", "Condition", limit=5)
        assert candidates and candidates[0].concept_id == 316139
        # recall only returns standard concepts; the non-standard source concept is not
        # a legitimate mapping target
        assert all(c.concept_id != 45571738 for c in candidates)
    finally:
        vocab.close()
