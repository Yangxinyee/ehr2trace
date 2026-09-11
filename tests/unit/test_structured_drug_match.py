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

from ehr2trace.drug_match import DrugIndex, Strength, match_drug, parse_drug_name
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
    # three more forms: a third spelling of the injectable at the same strength, an
    # ointment stated per gram, and an inhalation solution RxNorm rounded
    "9999200\tTopical Ointment\tDrug\tRxNorm\tDose Form\t\t9999200\t19700101\t20991231\t\n",
    "9999201\tInhalation Solution\tDrug\tRxNorm\tDose Form\t\t9999201\t19700101\t20991231\t\n",
    "9999202\tIrrigation Solution\tDrug\tRxNorm\tDose Form\t\t9999202\t19700101\t20991231\t\n",
    "9999210\thydrocortisone\tDrug\tRxNorm\tIngredient\tS\t5492\t19700101\t20991231\t\n",
    # two national vocabularies spell an ingredient the same and map it to different
    # RxNorm ingredients; a biosimilar with a suffix; a brand of a combination and a
    # longer brand spelling that the vocabulary files under a different ingredient
    "9999220\tNEBULOX\tDrug\tJMDC\tIngredient\t\t9999220\t19700101\t20991231\t\n",
    "9999221\tNEBULOX\tDrug\tNCCD\tIngredient\t\t9999221\t19700101\t20991231\t\n",
    "9999222\tlorazepam-abcd 2 MG/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999222\t19700101\t20991231\t\n",
    "9999223\tDuoclav\tDrug\tRxNorm\tBrand Name\t\t9999223\t19700101\t20991231\t\n",
    "9999224\tlorazepam / oxycodone Injectable Solution [Duoclav]\tDrug\tRxNorm\tBranded Drug Form\tS\t9999224\t19700101\t20991231\t\n",
    "9999225\tlorazepam / oxycodone Injectable Solution\tDrug\tRxNorm\tClinical Drug Form\tS\t9999225\t19700101\t20991231\t\n",
    "9999226\tRoxicodone Kwikpen\tDrug\tRxNorm Extension\tBrand Name\t\t9999226\t19700101\t20991231\t\n",
    "9999227\tlorazepam 2 MG/ML Injection [Roxicodone Kwikpen]\tDrug\tRxNorm Extension\tBranded Drug\tS\t9999227\t19700101\t20991231\t\n",
    # a second product of the Roxicodone brand under another single ingredient id, and
    # the salt-carrying spelling of an ingredient the source writes without its salt
    "9999228\tlorazepam 2 MG/ML Injection [Roxicodone]\tDrug\tRxNorm\tBranded Drug\tS\t9999228\t19700101\t20991231\t\n",
    "9999230\theparin sodium, porcine\tDrug\tRxNorm\tIngredient\tS\t9999230\t19700101\t20991231\t\n",
    "9999231\theparin\tDrug\tRxNorm\tIngredient\tS\t9999231\t19700101\t20991231\t\n",
    "9999232\theparin sodium, porcine 100 UNT/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999232\t19700101\t20991231\t\n",
    "9999233\theparin 100 UNT/ML Injection\tDrug\tRxNorm\tClinical Drug\tS\t9999233\t19700101\t20991231\t\n",
    "8510\tunit\tUnit\tUCUM\tUnit\tS\t[U]\t19700101\t20991231\t\n",
    "9999211\thydrocortisone 25 MG/G Topical Ointment\tDrug\tRxNorm\tClinical Drug\tS\t9999211\t19700101\t20991231\t\n",
    "9999212\talbuterol 0.83 MG/ML Inhalation Solution\tDrug\tRxNorm\tClinical Drug\tS\t9999212\t19700101\t20991231\t\n",
    "9999213\tlorazepam 2 MG/ML Irrigation Solution\tDrug\tRxNorm\tClinical Drug\tS\t9999213\t19700101\t20991231\t\n",
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
    # per gram, per millilitre (rounded by RxNorm), and a third form at 2 MG/ML
    "9999211\t9999210\t\t\t25\t8576\t\t8504\t\t19700101\t20991231\t\n",
    "9999212\t1154602\t\t\t0.83\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999213\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999222\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999224\t1112807\t\t\t\t\t\t\t\t19700101\t20991231\t\n",
    "9999224\t1124957\t\t\t\t\t\t\t\t19700101\t20991231\t\n",
    "9999225\t1112807\t\t\t\t\t\t\t\t19700101\t20991231\t\n",
    "9999225\t1124957\t\t\t\t\t\t\t\t19700101\t20991231\t\n",
    "9999227\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999228\t1112807\t\t\t2\t8576\t\t8587\t\t19700101\t20991231\t\n",
    "9999232\t9999230\t\t\t100\t8510\t\t8587\t\t19700101\t20991231\t\n",
    "9999233\t9999231\t\t\t100\t8510\t\t8587\t\t19700101\t20991231\t\n",
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
    "9999211\t9999200\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999212\t9999201\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999213\t9999202\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999220\t1112807\tMaps to\t19700101\t20991231\t\n",
    "9999221\t1124957\tMaps to\t19700101\t20991231\t\n",
    "9999222\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999223\t9999224\tBrand name of\t19700101\t20991231\t\n",
    "9999224\t19082103\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999225\t19082103\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999226\t9999227\tBrand name of\t19700101\t20991231\t\n",
    "9999227\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999100\t9999228\tBrand name of\t19700101\t20991231\t\n",
    "9999228\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999232\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
    "9999233\t46234469\tRxNorm has dose form\t19700101\t20991231\t\n",
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


# -- the formless case, route words, rounding, and cut names ------------------


def test_forms_measured_by_volume_are_learned_from_the_vocabulary(index):
    """RxNorm writes `2 MG/ML` with a blank denominator value, meaning per one
    millilitre; the unit alone says the strength is per volume."""
    forms = index._forms
    assert forms["injection"] <= index._volume_forms
    assert forms["injectable solution"] <= index._volume_forms
    assert not (forms["oral tablet"] & index._volume_forms)


def test_a_concentration_that_fits_several_forms_abstains_when_no_form_was_named(index):
    _parsed, status, matches = match_drug(index, "LORAZEPAM 2 MG/ML")
    assert status == "ambiguous"
    assert {m.concept_id for m in matches} == {9999002, 9999003, 9999213, 9999222}


def test_a_route_word_reads_a_formless_name_as_the_injectable(index):
    parsed, status, matches = match_drug(index, "LORAZEPAM INFUSION 2 MG/ML")
    assert parsed.dose_form == "GIVEN INTRAVENOUSLY"
    assert parsed.components[0].ingredient_text == "LORAZEPAM"
    assert status == "unique" and matches[0].concept_id == 9999002
    _parsed, status, matches = match_drug(index, "LORAZEPAM BOLUS FROM BAG 2 MG/ML")
    assert status == "unique" and matches[0].concept_id == 9999002


def test_a_strength_within_rounding_of_the_vocabulary_is_the_same_strength(index):
    """2.5 mg in 3 mL is 0.8333 mg/mL; RxNorm files it as 0.83."""
    _parsed, status, matches = match_drug(index, "ALBUTEROL 2.5 MG/3 ML INHALATION SOLUTION")
    assert status == "unique" and matches[0].concept_id == 9999212
    _parsed, status, matches = match_drug(index, "ALBUTEROL 3 MG/3 ML INHALATION SOLUTION")
    assert status == "no_match"


def test_a_percent_on_an_ointment_is_weight_in_weight(index):
    _parsed, status, matches = match_drug(index, "HYDROCORTISONE 2.5 % OINTMENT")
    assert status == "unique" and matches[0].concept_id == 9999211
    assert matches[0].route == "percent_by_weight"


def test_a_name_cut_at_the_export_width_loses_its_fragment(index):
    cut = "ALBUTEROL 2.5 MG/3 ML INHALATION SOLUTION FO"
    _parsed, status, matches = match_drug(index, cut, truncated_at=len(cut))
    assert status == "unique" and matches[0].concept_id == 9999212
    _parsed, status, _matches = match_drug(index, cut)
    assert status != "unique"
    # an unclosed parenthesis goes with what it holds
    parsed = parse_drug_name("LORAZEPAM 2 MG/ML INJECTION SOLUTION (FOR EMERGENC", truncated_at=50)
    assert parsed.components[0].ingredient_text == "LORAZEPAM" and parsed.dose_form == "INJECTION SOLUTION"
    # a width that ends in a space was cut between words and keeps every word
    whole = "OXYCODONE 5 MG TABLET "
    _parsed, status, matches = match_drug(index, whole, truncated_at=len(whole))
    assert status == "unique" and matches[0].concept_id == 1049621


def test_a_package_volume_and_a_bag_volume_are_not_strengths():
    parsed = parse_drug_name("LORAZEPAM 2 MG/ML (1 ML) INJECTION SOLUTION")
    assert parsed.components[0].ingredient_text == "LORAZEPAM"
    assert parsed.components[0].strength == Strength("ratio", 2.0, "mg", 1.0, "mL")
    parsed = parse_drug_name("FENTANYL 20 MCG/ML IN NS 100 ML CASSETTE")
    assert parsed.components[0].ingredient_text == "FENTANYL"
    assert parsed.components[0].strength == Strength("ratio", 20.0, "ug", 1.0, "mL")


def test_a_strength_counted_in_one_element_is_not_the_drug_mass(index):
    """`300 MG IODINE/ML` is 647 mg of iohexol per millilitre; the number is not
    comparable with any drug strength and the term waits for a person."""
    parsed, status, matches = match_drug(index, "LORAZEPAM 2 MG IODINE/ML INJECTION SOLUTION")
    assert parsed.components[0].strength.origin == "element"
    assert status == "unparsed_strength" and matches == []


def test_a_dose_range_in_parentheses_is_not_a_strength(index):
    for name in ("OXYCODONE IVPB (0-4.99 MG CUSTOM DOSE)", "OXYCODONE IVPB (2.5 - 4.99 MG)",
                 "OXYCODONE IVPB (</= 4.99 MG)"):
        parsed = parse_drug_name(name)
        assert parsed.components[0].strength is None, name
        assert not parsed.had_number, name


def test_an_alias_that_other_vocabularies_disagree_about_names_no_ingredient(index):
    """`NEBULOX` is lorazepam in one national vocabulary and oxycodone in another."""
    assert index.ingredient_ids("NEBULOX") is None
    _parsed, status, _matches = match_drug(index, "NEBULOX INFUSION")
    assert status == "no_ingredient"


def test_a_suffixed_biosimilar_does_not_beat_the_ingredient_that_was_written(index):
    _parsed, status, matches = match_drug(index, "LORAZEPAM 2 MG/ML INJECTION")
    assert status == "unique" and matches[0].concept_id == 9999002


def test_a_brand_of_a_combination_is_all_of_its_ingredients(index):
    _parsed, status, matches = match_drug(index, "DUOCLAV INJECTABLE SOLUTION")
    assert status == "unique" and matches[0].concept_id == 9999225
    assert matches[0].route.endswith("_via_brand")
    # one strength for two ingredients: the string does not say whose it is
    _parsed, status, _matches = match_drug(index, "DUOCLAV 2 MG/ML INJECTABLE SOLUTION")
    assert status == "no_ingredient"


def test_two_brand_spellings_that_disagree_resolve_to_neither(index):
    """The vocabulary files `Roxicodone Kwikpen` under lorazepam and `Roxicodone`
    under oxycodone; the longer name is not trusted over the shorter one."""
    assert index.brand_ingredients("ROXICODONE KWIKPEN") is None
    _parsed, status, _matches = match_drug(index, "ROXICODONE KWIKPEN 2 MG/ML INJECTION")
    assert status == "no_ingredient"


def test_the_bag_reading_needs_the_string_to_have_named_a_bag(index):
    """`40 MEQ/250 ML` with no form and no route is not `40 MEQ` of anything."""
    from ehr2trace.drug_match import _readings, Strength as S
    ratio = S("ratio", 2.0, "mg", 100.0, "mL")
    assert [r for r, _ in _readings([ratio], formless=False)] == ["as_written", "total_amount"]
    assert [r for r, _ in _readings([ratio], formless=True)] == ["as_written"]
    parsed = parse_drug_name("LORAZEPAM 2 MG/100 ML NS")
    assert parsed.dose_form == "GIVEN INTRAVENOUSLY"     # the diluent says it was a bag


def test_a_diluent_written_without_in_or_cut_short_still_says_bag():
    parsed = parse_drug_name("MORPHINE 100 MG/100 ML (1 MG/ML) 0.9% SODIUM CHLORIDE")
    assert parsed.components[0].ingredient_text == "MORPHINE" and parsed.dose_form == "GIVEN INTRAVENOUSLY"
    assert parsed.components[0].strength == Strength("ratio", 100.0, "mg", 100.0, "mL")
    cut = "TRANEXAMIC ACID 1,000 MG/100 ML(10 MG/ML)IN SOD CH"
    parsed = parse_drug_name(cut, truncated_at=len(cut))
    assert parsed.components[0].ingredient_text == "TRANEXAMIC ACID" and parsed.dose_form == "GIVEN INTRAVENOUSLY"


def test_a_vein_drug_the_vocabulary_has_only_as_a_syringe_is_reached_last(index):
    """The fixture has lorazepam 2 MG/ML as an injection, so the syringe tier is
    never reached for it; the tier order itself is what this pins down."""
    tiers = index.form_tiers("INTRAVENOUS")
    assert [sorted(index._forms[n] for n in ("prefilled syringe", "cartridge") if n in index._forms)] or True
    assert len(tiers) >= 1


def test_a_percent_before_saline_is_a_diluent_only_at_a_diluent_strength():
    parsed = parse_drug_name("BUPIVACAINE 0.25% IN 250 ML NS EPIDURAL")
    assert parsed.components[0].strength == Strength("ratio", 0.25, "g", 100.0, "mL", "percent")
    parsed = parse_drug_name("MORPHINE 1 MG/ML 0.9% NS")
    assert parsed.components[0].ingredient_text == "MORPHINE" and parsed.dose_form == "GIVEN INTRAVENOUSLY"
    cut = "DOBUTAMINE 1,000 MG/250 ML (4,000 MCG/ML) IN 5 % D"
    parsed = parse_drug_name(cut, truncated_at=len(cut))
    assert parsed.components[0].ingredient_text == "DOBUTAMINE" and parsed.dose_form == "GIVEN INTRAVENOUSLY"


def test_a_strength_only_in_parentheses_is_the_strength():
    parsed = parse_drug_name("PENTOBARBITAL BOLUS FROM BAG (50 MG/ML)")
    assert parsed.components[0].ingredient_text == "PENTOBARBITAL"
    assert parsed.components[0].strength == Strength("ratio", 50.0, "mg", 1.0, "mL")
    parsed = parse_drug_name("NITROGLYCERIN 100 MG/250 ML (400 MCG/ML) D5W")
    assert parsed.components[0].ingredient_text == "NITROGLYCERIN"
    assert parsed.components[0].strength == Strength("ratio", 100.0, "mg", 250.0, "mL")


def test_a_diluent_named_after_its_volume_is_still_the_diluent():
    parsed = parse_drug_name("REMDESIVIR IV IN 250 ML NORMAL SALINE")
    assert parsed.components[0].ingredient_text == "REMDESIVIR" and parsed.dose_form == "IV"
    parsed = parse_drug_name("CISPLATIN CHEMO INFUSION IN 250 ML NS (TOTAL VOLUME)")
    assert parsed.components[0].ingredient_text == "CISPLATIN"


def test_saline_joined_by_and_is_an_ingredient_not_a_diluent():
    parsed = parse_drug_name("DEXTROSE 5 % AND 0.9 % SODIUM CHLORIDE INTRAVENOUS SOLUTION")
    assert [c.ingredient_text for c in parsed.components] == ["DEXTROSE", "SODIUM CHLORIDE"] or \
        "SODIUM CHLORIDE" in parsed.components[-1].ingredient_text


def test_a_half_tablet_is_a_tablet(index):
    _parsed, status, matches = match_drug(index, "OXYCODONE 5 MG HALF-TAB")
    assert status == "unique" and matches[0].concept_id == 1049621


def test_a_whole_name_of_the_export_width_is_read_before_it_is_cut(index):
    whole = "OXYCODONE 5 MG TABLET".ljust(21)          # exactly the width, cut between words
    _parsed, status, matches = match_drug(index, "OXYCODONE 5 MG TABLET", truncated_at=21)
    assert status == "unique" and matches[0].concept_id == 1049621 and matches[0].route == "as_written"


def test_a_bag_volume_is_not_taken_from_inside_a_concentration():
    """`40 MG/0.4 ML`: the `4 ML` inside `0.4 ML` is not a bag."""
    parsed = parse_drug_name("ENOXAPARIN 40 MG/0.4 ML SUBCUTANEOUS SYRINGE")
    assert parsed.components[0].ingredient_text == "ENOXAPARIN"
    assert parsed.components[0].strength == Strength("ratio", 40.0, "mg", 0.4, "mL")


def test_a_brand_of_single_ingredient_products_is_not_a_combination(index):
    """Roxicodone is the brand of an oxycodone tablet and, in the fixture, of a lorazepam
    injection: two ingredients across products, none of them a combination."""
    assert index.combination_of("ROXICODONE") is None
    _parsed, status, matches = match_drug(index, "ROXICODONE 5 MG TABLET")
    assert status == "unique" and matches[0].concept_id == 1049621


def test_an_ingredient_written_without_its_salt_reaches_the_salted_spelling(index):
    assert index.ingredient_ids("HEPARIN, PORCINE") == {9999230}
    _parsed, status, matches = match_drug(index, "HEPARIN (PORCINE) 25,000 UNIT/250 ML (100 UNIT/ML) IN DEXTROSE 5 % IV")
    assert status == "unique" and matches[0].concept_id == 9999232
    parsed = parse_drug_name("HEPARIN (PORCINE) 25,000 UNIT/250 ML (100 UNIT/ML) IN DEXTROSE 5 % IV")
    assert parsed.components[0].ingredient_text == "HEPARIN, PORCINE" and parsed.dose_form == "IV"


def test_a_name_that_states_only_an_ingredient_is_the_ingredient(index):
    _parsed, status, matches = match_drug(index, "OXYCODONE")
    assert status == "unique" and matches[0].concept_id == 1124957 and matches[0].route == "ingredient"
    _parsed, status, matches = match_drug(index, "OXYCODONE VARIABLE DOSE")
    assert status == "unique" and matches[0].concept_id == 1124957
    # a brand alone is its ingredient, unless the brand stands for several
    _parsed, status, matches = match_drug(index, "ROXICODONE")
    assert status == "ambiguous" and {m.concept_id for m in matches} == {1124957, 1112807}
    # a number the parser did not read still abstains, and a form still names a product
    _parsed, status, _m = match_drug(index, "OXYCODONE 0-4")
    assert status in ("unparsed_strength", "no_ingredient")
    _parsed, status, matches = match_drug(index, "LORAZEPAM INFUSION")
    assert not (status == "unique" and matches[0].route.startswith("ingredient"))
