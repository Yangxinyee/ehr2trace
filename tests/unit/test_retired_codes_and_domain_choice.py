"""A code the vocabulary has retired is still the code the record was written with.

Athena marks a concept `invalid_reason` when the code leaves the code set -- an NDC is
discontinued, an ICD-10-CM code is split at a fiscal-year boundary -- and keeps its
`Maps to` precisely so that history coded with it still maps. The resolver used to
require the *source* concept to be current, which is a statement about today's code set
rather than about the record: it left 3.04 million MIMIC-IV prescriptions written on
discontinued NDCs, and 7,588 diagnoses on superseded ICD codes, with no concept at all.

The second rule here is about which target wins when a code has several. `C92.01`
(acute myeloid leukaemia in remission) maps to a Condition *and* to an Episode; the
lowest concept id is the Episode, so a diagnosis row was published carrying a concept
the condition table cannot hold. The event kind says which domain the row is headed
for, and that is what chooses among the targets.
"""

from __future__ import annotations

from pathlib import Path

from ehr2trace.terminology import MappingRegistry, TermRequest, Vocabulary, resolve_terms_batch

from tests.unit.test_unpunctuated_codes import write_vocab


def resolve(directory: Path, code: str, kind: str = "condition", system: str = "ICD10CM"):
    vocabulary = Vocabulary.open(directory)
    try:
        term = TermRequest(system, code, None, kind, 1)
        resolved, unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
        return resolved.get(term.key), unresolved
    finally:
        vocabulary.close()


RETIRED = (
    "45571738\tNicotine dependence\tCondition\tICD10CM\tbilling\t\tF17.210\t19700101\t20180930\tD\n"
    "316139\tNicotine dependence\tCondition\tSNOMED\tfinding\tS\t56294008\t19700101\t20991231\t\n"
)
MAPS_TO = "45571738\t316139\tMaps to\t19700101\t20991231\t\n"


def test_a_retired_code_still_maps_through_the_relationship_it_kept(tmp_path):
    match, unresolved = resolve(write_vocab(tmp_path / "v", RETIRED, MAPS_TO), "F17.210")
    assert match is not None, f"a retired source code resolved to nothing: {unresolved}"
    assert match.concept_id == 316139


def test_the_retirement_is_recorded_in_the_path(tmp_path):
    """So a reviewer can list every mapping that leant on a withdrawn code."""
    retired, _ = resolve(write_vocab(tmp_path / "retired", RETIRED, MAPS_TO), "F17.210")
    current, _ = resolve(
        write_vocab(
            tmp_path / "current",
            RETIRED.replace("20180930\tD", "20991231\t"),
            MAPS_TO,
        ),
        "F17.210",
    )
    assert retired.path == "mapped_relationship_retired"
    assert current.path == "mapped_relationship"


def test_a_retired_code_with_nothing_to_map_to_still_does_not_resolve(tmp_path):
    """Admitting the source concept is not the same as publishing a withdrawn concept."""
    match, unresolved = resolve(write_vocab(tmp_path / "v", RETIRED), "F17.210")
    assert match is None
    assert len(unresolved) == 1


def test_a_retired_code_resolves_through_the_unpunctuated_pass_too(tmp_path):
    match, _ = resolve(write_vocab(tmp_path / "v", RETIRED, MAPS_TO), "F17210")
    assert match is not None and match.concept_id == 316139
    assert match.path == "unpunctuated_mapped_relationship_retired"


TWO_DOMAINS = (
    "35206494\tAcute myeloid leukaemia in remission\tCondition\tICD10CM\tbilling\t\tC92.01\t19700101\t20991231\t\n"
    "32945\tRemission\tEpisode\tOMOP\tstatus\tS\tOMOP1\t19700101\t20991231\t\n"
    "138708\tAcute leukemia\tCondition\tSNOMED\tfinding\tS\t91861009\t19700101\t20991231\t\n"
)
BOTH = (
    "35206494\t32945\tMaps to\t19700101\t20991231\t\n"
    "35206494\t138708\tMaps to\t19700101\t20991231\t\n"
)


def test_the_target_in_the_event_kind_s_domain_is_the_one_published(tmp_path):
    match, _ = resolve(write_vocab(tmp_path / "v", TWO_DOMAINS, BOTH), "C92.01")
    assert match.concept_id == 138708, "the lowest id won over the domain the row is for"
    assert match.domain_id == "Condition"


def test_the_other_target_is_carried_rather_than_dropped(tmp_path):
    match, _ = resolve(write_vocab(tmp_path / "v", TWO_DOMAINS, BOTH), "C92.01")
    assert (32945, "Episode") in match.alternates
    assert match.path.endswith("_ambiguous"), "a fork in the road went unrecorded"


def test_a_kind_with_no_domain_of_its_own_still_picks_deterministically(tmp_path):
    """`note` expects no domain; the lowest concept id remains the tie-break."""
    match, _ = resolve(write_vocab(tmp_path / "v", TWO_DOMAINS, BOTH), "C92.01", kind="note")
    assert match.concept_id == 32945


def test_the_domain_preference_does_not_depend_on_the_row_order(tmp_path):
    """One term map entry per code, so two kinds of event share one answer.

    The query does not sort by event kind, so taking the kind off whichever row came
    back first made the published concept depend on the thread count. Where the kinds
    disagree about the domain there is nothing to prefer and the lowest id decides --
    the point is that the same input always gives the same concept.
    """
    vocabulary = Vocabulary.open(write_vocab(tmp_path / "v", TWO_DOMAINS, BOTH))
    try:
        terms = [TermRequest("ICD10CM", "C92.01", None, kind, 1)
                 for kind in ("condition", "measurement")]
        resolved, _ = resolve_terms_batch(terms, vocabulary, MappingRegistry())
        both = resolved[terms[0].key]
        one, _ = resolve_terms_batch([terms[0]], vocabulary, MappingRegistry())
        assert both.concept_id == 32945, "disagreeing kinds should fall back to the tie-break"
        assert one[terms[0].key].concept_id == 138708
    finally:
        vocabulary.close()
