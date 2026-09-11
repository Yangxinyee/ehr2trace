"""A drug name is structured, so it should be matched structurally.

The source string a hospital stores is not free text: `OXYCODONE 5 MG TABLET` states an
ingredient, a strength and a dose form, and the vocabulary states the same three things
about `oxycodone hydrochloride 5 MG Oral Tablet` -- the strength as a number in
`DRUG_STRENGTH` rather than as words in a name. Ranking the whole string by text
similarity put combination products and other pack sizes in front of the exact concept
and left 8.2 million drug rows unmapped with the right answer sitting in the vocabulary.

What these tests pin down is not only that the right concept is found, but the two
things that stop the pass from being a guess: it abstains when a name fits more than
one drug, and it never trades the strength or a word the source wrote for a match.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr2trace.drug_match import DrugIndex, match_drug, parse_drug_name
from ehr2trace.terminology import MappingRegistry, TermRequest, Vocabulary, resolve_terms_batch

CONCEPT_HEADER = (
    "concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\t"
    "standard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
)
STRENGTH_HEADER = (
    "drug_concept_id\tingredient_concept_id\tamount_value\tamount_unit_concept_id\t"
    "numerator_value\tnumerator_unit_concept_id\tdenominator_value\t"
    "denominator_unit_concept_id\tbox_size\tvalid_start_date\tvalid_end_date\tinvalid_reason\n"
)

# A miniature RxNorm: two ingredients, three units, three dose forms, and the drugs
# built from them -- including the two spellings of one injectable vial that make the
# dose-form tiers necessary, and the qualified product that makes the narrowing rule
# necessary.
CONCEPTS = "".join([
    "8576\tmilligram\tUnit\tUCUM\tUnit\tS\tmg\t19700101\t20991231\t\n",
    "8587\tmilliliter\tUnit\tUCUM\tUnit\tS\tmL\t19700101\t20991231\t\n",
    "8504\tgram\tUnit\tUCUM\tUnit\tS\tg\t19700101\t20991231\t\n",
    "9655\tmicrogram\tUnit\tUCUM\tUnit\tS\tug\t19700101\t20991231\t\n",
    "45744809\tactuation\tUnit\tUCUM\tUnit\tS\t{actuat}\t19700101\t20991231\t\n",
    "1124957\toxycodone\tDrug\tRxNorm\tIngredient\tS\t7804\t19700101\t20991231\t\n",
    "1112807\tlorazepam\tDrug\tRxNorm\tIngredient\tS\t6470\t19700101\t20991231\t\n",
    "963353\toxycodone hydrochloride\tDrug\tRxNorm\tPrecise Ingredient\t\t7805\t19700101\t20991231\t\n",
    "19082573\tOral Tablet\tDrug\tRxNorm\tDose Form\t\t317541\t19700101\t20991231\t\n",
    "19082103\tInjectable Solution\tDrug\tRxNorm\tDose Form\t\t316949\t19700101\t20991231\t\n",
    "46234469\tInjection\tDrug\tRxNorm\tDose Form\t\t1649574\t19700101\t20991231\t\n",
    "1049621\toxycodone 5 MG Oral Tablet\tDrug\tRxNorm\tClinical Drug\tS\t1049621\t19700101\t20991231\t\n",
    "1049622\toxycodone 10 MG Oral Tablet\tDrug\tRxNorm\tClinical Drug\tS\t1049622\t19700101\t20991231\t\n",
    "9999001\tOnce-Daily oxycodone 5 MG Oral Tablet\tDrug\tRxNorm\tClinical Drug\tS\t9999001\t19700101\t20991231\t\n",
    "9999002\tlorazepam 2 MG/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999002\t19700101\t20991231\t\n",
    "9999003\tlorazepam 2 MG/ML Injectable Solution\tDrug\tRxNorm\tClinical Drug\tS\t9999003\t19700101\t20991231\t\n",
    "9999004\tlorazepam 4 MG/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999004\t19700101\t20991231\t\n",
    "1154602\talbuterol\tDrug\tRxNorm\tIngredient\tS\t435\t19700101\t20991231\t\n",
    "19126918\tMetered Dose Inhaler\tDrug\tRxNorm\tDose Form\t\t316987\t19700101\t20991231\t\n",
    "9999005\talbuterol 0.09 MG/ACTUAT Metered Dose Inhaler\tDrug\tRxNorm\tClinical Drug\tS\t9999005\t19700101\t20991231\t\n",
    "9999006\talbuterol 0.09 MG/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999006\t19700101\t20991231\t\n",
    # a brand, and the branded product the vocabulary says it is the brand name of
    "9999100\tRoxicodone\tDrug\tRxNorm\tBrand Name\t\t9999100\t19700101\t20991231\t\n",
    "9999101\toxycodone 5 MG Oral Tablet [Roxicodone]\tDrug\tRxNorm\tBranded Drug\tS\t9999101\t19700101\t20991231\t\n",
])
STRENGTHS = "".join([
    "1049621\t1124957\t5\t8576\t\t\t\t\t\t19700101\t20991231\t\n",
    "1049622\t1124957\t10\t8576\t\t\t\t\t\t19700101\t20991231\t\n",
    "9999001\t1124957\t5\t8576\t\t\t\t\t\t19700101\t20991231\t\n",
    # a blank denominator value is RxNorm's way of writing "per one millilitre"
    "9999002\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999003\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999004\t1112807\t\t\t4\t8576\t\t8587\t\t19700101\t20991231\t\n",
    # an inhaler is dosed per actuation, not per millilitre
    "9999005\t1154602\t\t\t0.09\t8576\t\t45744809\t\t19700101\t20991231\t\n",
    # the same two numbers over a volume: a different strength, and it must stay one
    "9999006\t1154602\t\t\t0.09\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999101\t1124957\t5\t8576\t\t\t\t\t\t19700101\t20991231\t\n",
])
RELATIONSHIPS = "".join([
    "1049621\t19082573\tRxNorm has dose form\t19700101\t20991231\t\n",
    "1049622\t19082573\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999001\t19082573\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999002\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999003\t19082103\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999004\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "963353\t1124957\tForm of\t19700101\t20991231\t\n",
    "9999005\t19126918\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999006\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999101\t19082573\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999100\t9999101\tBrand name of\t19700101\t20991231\t\n",
])


@pytest.fixture()
def vocabulary(tmp_path: Path):
    directory = tmp_path / "vocabulary"
    directory.mkdir()
    (directory / "CONCEPT.csv").write_text(CONCEPT_HEADER + CONCEPTS, encoding="utf-8")
    (directory / "DRUG_STRENGTH.csv").write_text(STRENGTH_HEADER + STRENGTHS, encoding="utf-8")
    (directory / "CONCEPT_RELATIONSHIP.csv").write_text(
        "concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\t"
        "invalid_reason\n" + RELATIONSHIPS, encoding="utf-8")
    (directory / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\t"
        "vocabulary_concept_id\nNone\tOMOP\tOMOP\tv5.0 01-JAN-26\t44819096\n", encoding="utf-8")
    for table in ("DOMAIN", "CONCEPT_CLASS", "RELATIONSHIP", "CONCEPT_ANCESTOR"):
        (directory / f"{table}.csv").write_text("a\tb\nx\ty\n", encoding="utf-8")
    opened = Vocabulary.open(directory)
    yield opened
    opened.close()


@pytest.fixture()
def index(vocabulary):
    return DrugIndex(vocabulary.con)


# -- parsing ---------------------------------------------------------------

def test_a_name_is_read_as_ingredient_strength_and_form():
    parsed = parse_drug_name("OXYCODONE 5 MG TABLET")
    assert parsed.dose_form == "TABLET"
    assert [c.ingredient_text for c in parsed.components] == ["OXYCODONE"]
    assert parsed.components[0].strength.value == 5.0
    assert parsed.components[0].strength.unit == "mg"


def test_a_combination_splits_into_one_component_per_ingredient():
    parsed = parse_drug_name("SULFAMETHOXAZOLE 800 MG-TRIMETHOPRIM 160 MG TABLET")
    assert [c.ingredient_text for c in parsed.components] == ["SULFAMETHOXAZOLE", "TRIMETHOPRIM"]
    assert [c.strength.value for c in parsed.components] == [800.0, 160.0]


def test_a_hyphen_inside_one_ingredient_name_is_not_a_split():
    # Only one of the two pieces carries a strength, so the split is refused whole
    # rather than applied halfway and inventing an ingredient.
    parsed = parse_drug_name("BEVACIZUMAB-AWWB 25 MG/ML INJECTION SOLUTION")
    assert [c.ingredient_text for c in parsed.components] == ["BEVACIZUMAB-AWWB"]


def test_a_percent_on_a_liquid_is_weight_in_volume():
    strength = parse_drug_name("SODIUM CHLORIDE 0.9 % IV BOLUS").components[0].strength
    assert (strength.kind, strength.value, strength.unit) == ("ratio", 0.9, "g")
    assert (strength.denominator, strength.denominator_unit) == (100.0, "mL")
    assert strength.origin == "percent"


def test_the_bag_it_was_diluted_into_is_not_the_drug():
    parsed = parse_drug_name("CEFAZOLIN 2 GRAM/100 ML IN 0.9 % SODIUM CHLORIDE INTRAVENOUS SOLUTION")
    assert [c.ingredient_text for c in parsed.components] == ["CEFAZOLIN"]
    assert parsed.dose_form == "INTRAVENOUS SOLUTION"


# -- matching --------------------------------------------------------------

def test_an_exact_ingredient_strength_and_form_resolves(index):
    _parsed, status, matches = match_drug(index, "OXYCODONE 5 MG TABLET")
    assert status == "unique"
    assert matches[0].concept_id == 1049621


def test_the_strength_is_compared_as_a_number_not_as_text(index):
    # 0.005 GRAM and 5 MG are the same strength; nothing about the two strings is.
    _parsed, status, matches = match_drug(index, "OXYCODONE 0.005 GRAM TABLET")
    assert status == "unique"
    assert matches[0].concept_id == 1049621


def test_a_different_strength_does_not_resolve_to_the_nearest_one(index):
    _parsed, status, matches = match_drug(index, "OXYCODONE 7.5 MG TABLET")
    assert status == "no_match"
    assert matches == []


def test_a_qualified_product_does_not_beat_the_drug_that_was_written(index):
    # Both concepts have this ingredient, strength and form; only one of them claims a
    # product line the source never mentioned.
    _parsed, status, matches = match_drug(index, "OXYCODONE 5 MG TABLET")
    assert status == "unique"
    assert matches[0].concept_name == "oxycodone 5 MG Oral Tablet"


def test_a_second_spelling_of_one_dose_form_is_a_preference_not_a_tie(index):
    # `Injection` and `Injectable Solution` are the same vial filed twice. The lexicon
    # states which spelling to take first, so this resolves instead of abstaining.
    _parsed, status, matches = match_drug(index, "LORAZEPAM 2 MG/ML INJECTION SOLUTION")
    assert status == "unique"
    assert matches[0].concept_id == 9999002


def test_an_unknown_ingredient_abstains(index):
    _parsed, status, matches = match_drug(index, "NOTADRUG 5 MG TABLET")
    assert status == "no_ingredient"
    assert matches == []


def test_a_number_that_was_not_understood_abstains(index):
    # Falling through to a concept that carries no strength would drop the number the
    # source wrote, which is exactly the silent loss this pass exists to avoid.
    _parsed, status, _matches = match_drug(index, "OXYCODONE 5 SCRUPLES TABLET")
    assert status in ("unparsed_strength", "no_ingredient", "no_match")


# -- the pipeline pass -----------------------------------------------------

def test_the_pass_resolves_a_drug_name_and_records_how(vocabulary):
    term = TermRequest("SOURCE", "OXYCODONE 5 MG TABLET", "OXYCODONE 5 MG TABLET", "drug_order", 3)
    resolved, unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
    assert unresolved == []
    match = resolved[term.key]
    assert match.concept_id == 1049621
    assert match.domain_id == "Drug"
    assert match.path.startswith("structured_drug_")


def test_the_pass_leaves_a_name_it_cannot_settle_in_the_queue(vocabulary):
    term = TermRequest("SOURCE", "OXYCODONE 7.5 MG TABLET", "OXYCODONE 7.5 MG TABLET",
                       "drug_order", 1)
    resolved, unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
    assert resolved == {}
    assert [t.key for t in unresolved] == [term.key]


def test_the_pass_does_not_touch_terms_from_other_domains(vocabulary):
    term = TermRequest("SOURCE", "OXYCODONE 5 MG TABLET", "OXYCODONE 5 MG TABLET",
                       "measurement", 1)
    resolved, unresolved = resolve_terms_batch([term], vocabulary, MappingRegistry())
    assert resolved == {}
    assert [t.key for t in unresolved] == [term.key]


def test_a_vocabulary_without_drug_strength_abstains_rather_than_failing(tmp_path: Path):
    """A partial Athena bundle is a missing table, not a mapping failure.

    `DRUG_STRENGTH` is what makes this pass possible; a bundle downloaded without it
    should leave drug names in the review queue and let `ehr2trace vocabulary` report the
    gap, not stop the build with a SQL error halfway through publishing.
    """
    directory = tmp_path / "partial"
    directory.mkdir()
    (directory / "CONCEPT.csv").write_text(CONCEPT_HEADER + CONCEPTS, encoding="utf-8")
    (directory / "CONCEPT_RELATIONSHIP.csv").write_text(
        "concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\t"
        "invalid_reason\n" + RELATIONSHIPS, encoding="utf-8")
    (directory / "VOCABULARY.csv").write_text(
        "vocabulary_id\tvocabulary_name\tvocabulary_reference\tvocabulary_version\t"
        "vocabulary_concept_id\nNone\tOMOP\tOMOP\tv5.0 01-JAN-26\t44819096\n", encoding="utf-8")
    opened = Vocabulary.open(directory)
    try:
        term = TermRequest("SOURCE", "OXYCODONE 5 MG TABLET", "OXYCODONE 5 MG TABLET",
                           "drug_order", 1)
        resolved, unresolved = resolve_terms_batch([term], opened, MappingRegistry())
        assert resolved == {}
        assert [t.key for t in unresolved] == [term.key]
    finally:
        opened.close()


def test_a_site_local_word_is_declared_by_the_dataset_not_the_converter(index):
    """`... INJECTION SOLUTION JHM` only resolves once the dataset says `JHM` is noise."""
    _parsed, status, _matches = match_drug(index, "LORAZEPAM 2 MG/ML INJECTION SOLUTION JHM")
    assert status != "unique"
    _parsed, status, matches = match_drug(
        index, "LORAZEPAM 2 MG/ML INJECTION SOLUTION JHM", ["JHM"])
    assert status == "unique"
    assert matches[0].concept_id == 9999002


def test_a_strength_per_actuation_resolves(index):
    """An inhaler states its strength per puff, and the vocabulary agrees.

    Recognising only millilitre denominators left every inhaler in this export
    unmapped -- 64,148 rows on one albuterol product alone -- while
    `albuterol 0.09 MG/ACTUAT Metered Dose Inhaler` sat in the vocabulary. RxNorm has
    18,371 per-actuation strengths and 13,849 per-hour ones.
    """
    _parsed, status, matches = match_drug(
        index, "ALBUTEROL SULFATE HFA 90 MCG/ACTUATION AEROSOL INHALER")
    assert status == "unique"
    assert matches[0].concept_id == 9999005


def test_per_actuation_and_per_millilitre_are_not_the_same_strength(index):
    """`0.09 MG/ACTUAT` and `0.09 MG/ML` are the same two numbers and different drugs.

    The strength key carries the family of both halves for exactly this reason; keying
    on the numerator alone would let a puff match a millilitre.
    """
    _parsed, status, matches = match_drug(index, "ALBUTEROL 0.09 MG/ML INJECTION SOLUTION")
    assert status == "unique"
    assert matches[0].concept_id == 9999006


def test_a_brand_name_is_read_as_its_ingredient_and_the_route_says_so(index):
    # `ELIQUIS 5 MG TABLET` is apixaban 5 MG Oral Tablet; the vocabulary says so through
    # `Brand name of` and DRUG_STRENGTH, and 185 of a second site's anticoagulant names
    # were written that way. The brand is dropped, which the route records.
    parsed, status, matches = match_drug(index, "ROXICODONE 5 MG TABLET")
    assert status == "unique", (status, matches)
    assert matches[0].concept_id == 1049621
    assert matches[0].route.endswith("_via_brand")


def test_a_brand_does_not_override_an_ingredient_spelling(index):
    # An ingredient name that also happens to be a brand somewhere stays an ingredient.
    assert not index.is_brand("oxycodone")
    parsed, status, matches = match_drug(index, "OXYCODONE 5 MG TABLET")
    assert status == "unique" and not matches[0].route.endswith("_via_brand")
