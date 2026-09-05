"""Structured drug matching: ingredient + strength + dose form, not text similarity.

A drug name is the one place in an EHR extract where the source string is already
structured. `OXYCODONE 5 MG TABLET` names an ingredient, a number, a unit and a form,
and RxNorm's `oxycodone 5 MG Oral Tablet` names the same four things -- the strength
in `DRUG_STRENGTH` as a number, not as text. Comparing the numbers is exact; comparing
the strings is not, which is why dense retrieval put `acetaminophen 300 MG / oxycodone
5 MG Oral Tablet` in front of the right answer and why 8.4 million drug rows stayed
unmapped with the correct concept sitting in the vocabulary the whole time.

This module is a **deterministic pass**, in the same class as the punctuation-
insensitive code lookup: it either finds exactly one standard concept whose ingredient
set, strength and dose form all equal what the source string says, or it finds nothing
and the term stays in the review queue. It never picks the nearest concept, never
drops the strength to reach an ingredient-level concept, and never breaks a tie by
score. Every rule it applies is one a reviewer can check by reading two names.

What it is allowed to do beyond exact equality, and why each is not a loosening of the
claim:

* **dose-form tiers** -- `Injectable Solution` (RxNorm) and `Injection` (RxNorm
  Extension) are the same vial. See :mod:`ehr2cdm.drug_lexicon`.
* **the total-amount reading** -- `2 GRAM/100 ML` in a hospital's name for an IV bag
  is the same product RxNorm calls `2000 MG Intravenous Solution`; when the source
  gives a whole-container volume both readings are tried.
* **the salt suffix** -- `OXYCODONE HCL` is RxNorm's `oxycodone`; the full name is
  always tried first.
* **qualified variants** -- `Once-Daily gabapentin 600 MG Oral Tablet` is a different
  product from `gabapentin 600 MG Oral Tablet` and its name says so. A candidate whose
  name begins with words before the first ingredient is set aside, so the unqualified
  concept wins instead of the pair being called ambiguous.

Anything that survives none of that, or survives twice, abstains.

One constraint runs the other way, and it is there because leaving it out produced a
real error: when the source names no dose form at all, the candidate set is every form,
and `FOLIC ACID 1 MG/3 ML` matched `folic acid 1 MG Oral Tablet` -- a unique match, and
a wrong one, on 35,408 rows. A strength written per millilitre describes something
poured. So a source concentration restricts the formless case to dose forms the
vocabulary itself measures by volume, which it is asked rather than told: `Oral Tablet`
and `Oral Capsule` have no drug in `DRUG_STRENGTH` with a millilitre denominator, and
`Injection` has 73% of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .drug_lexicon import (
    DOSE_FORMS,
    INGREDIENT_ABBREVIATIONS,
    NOISE_WORDS,
    RELEASE_ABBREVIATIONS,
    STRENGTH_ELEMENTS,
    SALT_SUFFIXES,
    UNIT_FAMILIES,
    UNITS,
)

# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_NUM = r"\d[\d,]*(?:\.\d+)?"
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))

#: Parenthesised text that qualifies how a product is supplied or who it is for, never
#: what it is. `(PF)` is preservative-free; `(FOR ADULTS)` is a dosing note. Site-local
#: parentheticals are handled by the dataset's own noise list, not here.
_NOISE_PARENS = re.compile(
    r"\((?:PF|TOTAL VOLUME|PER DROP|ADULTS|PEDS|DEFAULT[^)]*|"
    r"FOR [^)]*|ISO-OSMOTIC|ISO-OSMOT|PRESERVATIVE FREE|FLUSH|\d+\s*MIN|"
    r"U-\d+|PART/CRYST)\)", re.I)
#: "(2.5 MG BASE)" restates the salt-free strength. It is a gloss on the number before
#: it, not a second component, and reading it as one invents an ingredient.
_BASE_GLOSS = re.compile(r"\([^()]*\bBASE\b[^()]*\)", re.I)
#: What the drug is dissolved in. RxNorm names the drug, not the bag it arrived in.
_DILUENT = re.compile(
    r"\bIN\s+(?:STERILE\s+)?(?:\d[\d.,]*\s*%\s*)?"
    r"(?:SODIUM CHLORIDE|NORMAL SALINE|SALINE|STERILE WATER|WATER|DEXTROSE|NACL|D5W|D50W)\b", re.I)
#: "IN 50 ML" -- the volume it was diluted into, not a strength.
_DILUENT_VOLUME = re.compile(rf"\bIN\s+{_NUM}\s*(?:ML|L)\b", re.I)
_NOISE_WORDS = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in NOISE_WORDS), re.I)

_ELEMENT = "|".join(sorted((re.escape(e) for e in STRENGTH_ELEMENTS), key=len, reverse=True))
_RATIO = re.compile(
    rf"({_NUM})\s*({_UNIT_ALT})\b(?:\s+(?:{_ELEMENT}))?\s*/\s*({_NUM})?\s*({_UNIT_ALT})\b", re.I)
_AMOUNT = re.compile(rf"({_NUM})\s*({_UNIT_ALT})\b(?:\s+(?:{_ELEMENT}))?", re.I)
#: "(0.083 %)", "(10 MG/ML)", "(500 MG)" -- the strength said a second way. Kept in the
#: string it becomes part of the ingredient name and nothing resolves.
_RESTATED_STRENGTH = re.compile(
    rf"\(\s*{_NUM}\s*(?:{_UNIT_ALT})(?:\s*/\s*{_NUM}?\s*(?:{_UNIT_ALT}))?\s*"
    rf"(?:{_ELEMENT})?\s*\)", re.I)
_PERCENT = re.compile(rf"({_NUM})\s*%")
#: "1:1,000" on an epinephrine ampoule means one gram in a thousand millilitres.
_COLON = re.compile(rf"1\s*:\s*({_NUM})")
_SHARED_DENOM = re.compile(rf"/\s*({_NUM})\s*(ML|L)\s*$", re.I)


def _number(text: str) -> float:
    return float(text.replace(",", ""))


@dataclass(frozen=True)
class Strength:
    """A strength as the source wrote it, before any vocabulary is consulted."""

    kind: str                       # "amount" | "ratio"
    value: float
    unit: str                       # UCUM code
    denominator: float | None = None
    denominator_unit: str | None = None
    #: How the source wrote it. A percent and a `1:1,000` are concentrations by
    #: definition -- there is no container volume in them to reinterpret as a total
    #: dose, and reading `0.9 %` as "900 MG" is how a saline flush became a 900 mg
    #: sodium chloride product.
    origin: str = "explicit"


@dataclass(frozen=True)
class Component:
    ingredient_text: str
    strength: Strength | None


@dataclass(frozen=True)
class ParsedDrug:
    components: tuple[Component, ...]
    dose_form: str | None           # source phrase, not yet a concept
    had_number: bool
    source: str


_FORM_PHRASES = sorted(DOSE_FORMS, key=lambda k: (-len(k.split()), -len(k)))


def _local_noise(words: Sequence[str]) -> re.Pattern | None:
    """A whole-word matcher for this site's own words, or None if it declared none."""
    cleaned = [w.strip() for w in words if w and w.strip()]
    if not cleaned:
        return None
    alternation = "|".join(re.escape(w) for w in sorted(cleaned, key=len, reverse=True))
    return re.compile(rf"(?:\((?:{alternation})[^)]*\)|\b(?:{alternation})\b)", re.I)


def _strip_noise(text: str, local: re.Pattern | None = None) -> str:
    text = _BASE_GLOSS.sub(" ", text)
    if local is not None:
        text = local.sub(" ", text)
    text = _RESTATED_STRENGTH.sub(" ", text)
    text = _NOISE_PARENS.sub(" ", text)
    text = _DILUENT_VOLUME.sub(" ", text)
    text = _DILUENT.sub(" ", text)
    text = _NOISE_WORDS.sub(" ", text)
    # "TABLET, EXTENDED RELEASE" and "TABLET,EXTENDED RELEASE" are one phrase.
    text = re.sub(r"\s*,\s*", ",", text)
    return re.sub(r"\s+", " ", text).strip(" ,-")


def _take_dose_form(text: str) -> tuple[str, str | None]:
    """Longest dose-form phrase anchored at the end of the string."""
    upper = text.upper().rstrip(" .,")
    for phrase in _FORM_PHRASES:
        if upper == phrase or upper.endswith(" " + phrase):
            return upper[: len(upper) - len(phrase)].rstrip(" ,-"), phrase
    return upper, None


def _take_strength(text: str) -> tuple[str, Strength | None]:
    match = _RATIO.search(text)
    if match:
        denominator = _number(match.group(3)) if match.group(3) else 1.0
        rest = text[: match.start()] + " " + text[match.end():]
        return rest, Strength("ratio", _number(match.group(1)), UNITS[match.group(2).upper()],
                              denominator, UNITS[match.group(4).upper()])
    match = _COLON.search(text)
    if match:
        rest = text[: match.start()] + " " + text[match.end():]
        return rest, Strength("ratio", 1.0, "g", _number(match.group(1)), "mL", "colon")
    match = _PERCENT.search(text)
    if match:
        rest = text[: match.start()] + " " + text[match.end():]
        # A percent on a liquid is weight in volume: 1 % is one gram in 100 mL.
        return rest, Strength("ratio", _number(match.group(1)), "g", 100.0, "mL", "percent")
    match = _AMOUNT.search(text)
    if match:
        rest = text[: match.start()] + " " + text[match.end():]
        return rest, Strength("amount", _number(match.group(1)), UNITS[match.group(2).upper()])
    return text, None


def _clean_name(text: str) -> str:
    """Normalise an ingredient name, keeping any parenthesised qualifier inline.

    `HEPARIN (PORCINE)` is heparin from a pig, and RxNorm files it as `heparin sodium,
    porcine` -- a different concept from plain `heparin`. Dropping the parenthesis to
    reach an ingredient that resolves is exactly the kind of quiet loss of meaning this
    converter is not allowed to make, so the qualifier is kept and
    :meth:`DrugIndex.ingredient_ids` tries the spellings in order of specificity.
    """
    text = re.sub(r"\s*\(([^)]*)\)\s*", r", \1 ", text)
    text = re.sub(r"[()\[\]]", " ", text)
    words = [INGREDIENT_ABBREVIATIONS.get(w.upper(), w)
             for w in text.split() if w.upper() not in RELEASE_ABBREVIATIONS]
    return re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", " ".join(words))).strip(" ,-/")


_PIECE = re.compile(rf"^(?P<name>.*?[A-Z)])\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT_ALT})\s*$", re.I)


def _split_components(head: str) -> tuple[Component, ...] | None:
    """Split "A 875 MG-B 125 MG" into one component per ingredient.

    Accepted only when *every* piece carries its own strength. A hyphen inside a single
    ingredient's name leaves a piece with no number, and the whole split is then
    rejected rather than half-applied -- half-applying it invents an ingredient.
    """
    pieces = [p for p in re.split(r"\s*[-/]\s*", head) if p.strip()]
    if len(pieces) < 2:
        return None
    out = []
    for piece in pieces:
        match = _PIECE.match(piece.strip())
        if not match:
            return None
        out.append(Component(
            _clean_name(match.group("name")),
            Strength("amount", _number(match.group("num")), UNITS[match.group("unit").upper()]),
        ))
    return tuple(out)


def parse_drug_name(source: str, local_noise: Sequence[str] = ()) -> ParsedDrug:
    text = _strip_noise(source.upper(), _local_noise(local_noise))
    had_number = bool(re.search(r"\d", text))
    for abbreviation, phrase in RELEASE_ABBREVIATIONS.items():
        if re.search(rf"\b{abbreviation}\b", text) and phrase not in text:
            if re.search(r"\bTABLET\b", text):
                text = re.sub(r"\bTABLET\b", f"TABLET,{phrase}", text, count=1)
            elif re.search(r"\bCAPSULE\b", text):
                text = re.sub(r"\bCAPSULE\b", f"CAPSULE,{phrase}", text, count=1)
            break
    head, form = _take_dose_form(text)

    shared: tuple[float, str] | None = None
    match = _SHARED_DENOM.search(head)
    if match:
        shared = (_number(match.group(1)), UNITS[match.group(2).upper()])
        head = head[: match.start()].rstrip()

    parts = _split_components(head)
    if parts is not None:
        if shared:
            parts = tuple(
                Component(c.ingredient_text,
                          Strength("ratio", c.strength.value, c.strength.unit, shared[0], shared[1]))
                for c in parts
            )
        return ParsedDrug(parts, form, had_number, source)

    rest, strength = _take_strength(head)
    if strength is not None and strength.kind == "amount" and shared:
        strength = Strength("ratio", strength.value, strength.unit, shared[0], shared[1])
    return ParsedDrug((Component(_clean_name(rest), strength),), form, had_number, source)


# --------------------------------------------------------------------------
# the vocabulary side
# --------------------------------------------------------------------------

def _canonical(value: float, unit: str) -> tuple[str, float] | None:
    """(family, value in the family's base unit), or None for an unusable unit."""
    family = UNIT_FAMILIES.get(unit)
    return None if family is None else (family[0], value * family[1])


def _round(value: float) -> float:
    """Round to nine significant digits, so 0.16666666666 and 0.166666667 agree."""
    return float(f"{value:.9g}")


def _signature(strength: Strength | None) -> tuple | None:
    """The source strength as the key the vocabulary index is built under."""
    if strength is None:
        return ("none",)
    if strength.kind == "amount":
        canonical = _canonical(strength.value, strength.unit)
        return None if canonical is None else ("amount", canonical[0], _round(canonical[1]))
    numerator = _canonical(strength.value, strength.unit)
    denominator = _canonical(strength.denominator or 1.0, strength.denominator_unit or "mL")
    if numerator is None or denominator is None or denominator[0] != "volume":
        return None
    return ("ratio", numerator[0], _round(numerator[1] / denominator[1]))


#: RxNorm's classes for a drug named as ingredient + strength + form, and for one named
#: as ingredient + form with no strength. Branded and packaged classes are excluded:
#: a source string with no brand in it does not assert a brand.
_STRENGTH_CLASSES = ("Clinical Drug",)
_FORM_ONLY_CLASSES = ("Clinical Drug Form",)


@dataclass(frozen=True)
class DrugMatch:
    concept_id: int
    concept_name: str
    vocabulary_id: str
    #: which rule reached it, for the audit trail
    route: str


class DrugIndex:
    """The vocabulary's drug concepts, keyed the way a parsed source string is keyed.

    Built once per run from `DRUG_STRENGTH` and `RxNorm has dose form`, in memory: the
    keyed subset is about 165,000 concepts, and a dictionary lookup per distinct source
    string is the difference between seconds and hours over 26,000 of them.
    """

    def __init__(self, connection):
        self.con = connection
        self._ingredients: dict[str, tuple[set[int], int]] = {}
        self._forms: dict[str, set[int]] = {}
        self._by_key: dict[tuple, list[int]] = {}
        self._by_first_word: dict[str, list[str]] = {}
        #: Dose forms that anything is ever measured by volume in. Derived, not listed.
        self._volume_forms: set[int] = set()
        self._concepts: dict[int, tuple[str, str]] = {}
        self._names: dict[int, str] = {}
        self._build()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        con = self.con
        con.execute("""
            CREATE OR REPLACE TEMP TABLE _std_ing AS
            SELECT CAST(concept_id AS BIGINT) AS concept_id, lower(trim(concept_name)) AS name
            FROM CONCEPT
            WHERE concept_class_id = 'Ingredient' AND standard_concept = 'S'
              AND vocabulary_id IN ('RxNorm', 'RxNorm Extension')
              AND (invalid_reason IS NULL OR invalid_reason = '')
        """)
        # An ingredient may be written under a name RxNorm files as a Precise
        # Ingredient (`glycopyrrolate` for `glycopyrronium`) or under another
        # vocabulary's spelling. Both reach the standard ingredient by a relationship
        # the vocabulary states, never by resemblance.
        con.execute("""
            CREATE OR REPLACE TEMP TABLE _ing_alias AS
            SELECT lower(trim(c.concept_name)) AS name, s.concept_id, 0 AS tier
            FROM CONCEPT c JOIN _std_ing s ON s.concept_id = CAST(c.concept_id AS BIGINT)
            UNION
            SELECT lower(trim(c.concept_name)), s.concept_id, 1
            FROM CONCEPT c
            JOIN CONCEPT_RELATIONSHIP r
              ON r.concept_id_1 = c.concept_id
             AND r.relationship_id IN ('Maps to', 'Form of')
             AND (r.invalid_reason IS NULL OR r.invalid_reason = '')
            JOIN _std_ing s ON s.concept_id = CAST(r.concept_id_2 AS BIGINT)
            WHERE c.concept_class_id IN ('Ingredient', 'Precise Ingredient')
        """)
        for name, concept_id, tier in con.execute(
            "SELECT name, concept_id, min(tier) FROM _ing_alias GROUP BY 1, 2"
        ).fetchall():
            existing = self._ingredients.get(name)
            if existing is None or tier < existing[1]:
                self._ingredients[name] = ({int(concept_id)}, int(tier))
            elif tier == existing[1]:
                existing[0].add(int(concept_id))
        for name in self._ingredients:
            first = name.split()[0].rstrip(",") if name.split() else ""
            self._by_first_word.setdefault(first, []).append(name)

        for concept_id, name in con.execute("""
            SELECT CAST(concept_id AS BIGINT), concept_name FROM CONCEPT
            WHERE concept_class_id = 'Dose Form' AND vocabulary_id LIKE 'RxNorm%'
              AND (invalid_reason IS NULL OR invalid_reason = '')
        """).fetchall():
            self._forms.setdefault(name.lower(), set()).add(int(concept_id))

        classes = _STRENGTH_CLASSES + _FORM_ONLY_CLASSES
        rows = con.execute(f"""
            SELECT CAST(d.drug_concept_id AS BIGINT), CAST(d.ingredient_concept_id AS BIGINT),
                   TRY_CAST(d.amount_value AS DOUBLE), au.concept_code,
                   TRY_CAST(d.numerator_value AS DOUBLE), nu.concept_code,
                   TRY_CAST(d.denominator_value AS DOUBLE), du.concept_code,
                   c.concept_name, c.vocabulary_id, c.concept_class_id
            FROM DRUG_STRENGTH d
            JOIN CONCEPT c ON c.concept_id = d.drug_concept_id
             AND c.standard_concept = 'S' AND c.domain_id = 'Drug'
             AND c.vocabulary_id IN ('RxNorm', 'RxNorm Extension')
             AND c.concept_class_id IN ({','.join('?' * len(classes))})
             AND (c.invalid_reason IS NULL OR c.invalid_reason = '')
            LEFT JOIN CONCEPT au ON au.concept_id = d.amount_unit_concept_id
            LEFT JOIN CONCEPT nu ON nu.concept_id = d.numerator_unit_concept_id
            LEFT JOIN CONCEPT du ON du.concept_id = d.denominator_unit_concept_id
            WHERE d.invalid_reason IS NULL OR d.invalid_reason = ''
        """, list(classes)).fetchall()

        per_drug: dict[int, list[tuple]] = {}
        for (drug, ingredient, amount, amount_unit, numerator, numerator_unit,
             denominator, denominator_unit, name, vocabulary, _class) in rows:
            self._concepts[drug] = (name, vocabulary)
            self._names[drug] = (name or "").lower()
            per_drug.setdefault(drug, []).append(
                (ingredient, amount, amount_unit, numerator, numerator_unit,
                 denominator, denominator_unit))

        form_of: dict[int, set[int]] = {}
        for drug, form in con.execute("""
            SELECT CAST(concept_id_1 AS BIGINT), CAST(concept_id_2 AS BIGINT)
            FROM CONCEPT_RELATIONSHIP
            WHERE relationship_id = 'RxNorm has dose form'
              AND (invalid_reason IS NULL OR invalid_reason = '')
        """).fetchall():
            form_of.setdefault(int(drug), set()).add(int(form))

        # Which dose forms are dispensed by volume, asked of the vocabulary rather than
        # decided here: a form is counted if any meaningful share of the drugs carrying
        # it state their strength per millilitre. Oral Tablet and Oral Capsule come out
        # at 0.0%, Injection at 73%, and the gap is not close. This is what stops
        # `FOLIC ACID 1 MG/3 ML` -- a strength that only a liquid can have -- from
        # matching `folic acid 1 MG Oral Tablet` when the source named no form at all.
        by_volume: dict[int, list[int]] = {}
        for drug, parts in per_drug.items():
            liquid = any(p[5] is not None and p[6] in ("mL", "L") for p in parts)
            for form in form_of.get(drug, ()):
                counts = by_volume.setdefault(form, [0, 0])
                counts[0] += 1
                counts[1] += 1 if liquid else 0
        self._volume_forms = {form for form, (total, liquid) in by_volume.items()
                              if total and liquid / total >= 0.01}

        for drug, parts in per_drug.items():
            signature = []
            for (ingredient, amount, amount_unit, numerator, numerator_unit,
                 denominator, denominator_unit) in parts:
                signature.append((int(ingredient), _vocabulary_signature(
                    amount, amount_unit, numerator, numerator_unit,
                    denominator, denominator_unit)))
            key_parts = frozenset(signature)
            for form in form_of.get(drug, {0}):
                self._by_key.setdefault((key_parts, form), []).append(drug)
        con.execute("DROP TABLE IF EXISTS _ing_alias")
        con.execute("DROP TABLE IF EXISTS _std_ing")

    # -- lookups ----------------------------------------------------------
    def ingredient_ids(self, text: str) -> set[int] | None:
        """Resolve an ingredient name, most specific spelling first.

        Order matters: the full name, then the name with its last word joined by a
        comma (RxNorm's `heparin sodium, porcine`), then the name with a salt or
        hydrate suffix removed, and only ever one step at a time. The first spelling
        that exists in the vocabulary wins, so a more specific concept is never passed
        over for a less specific one that also happens to match.
        """
        name = " ".join(text.strip().lower().split())
        if not name:
            return None
        for candidate in self._spellings(name):
            found = self._ingredients.get(candidate)
            if found is None:
                continue
            if candidate != name and self._more_specific_exists(candidate, name):
                # Falling back this far would drop a word the source wrote while the
                # vocabulary has a concept that keeps it: `HEPARIN (PORCINE)` would
                # become plain `heparin` even though `heparin sodium, porcine` exists.
                # Losing that is not a match, so the term goes to a person instead.
                return None
            return found[0]
        return None

    def _more_specific_exists(self, base: str, full: str) -> bool:
        dropped = [w for w in re.split(r"[,\s]+", full) if w and w not in base.split()]
        if not dropped:
            return False
        prefix = base.split()[0]
        for key in self._by_first_word.get(prefix, ()):
            if key == base or not key.startswith(base.split()[0]):
                continue
            tokens = set(re.split(r"[,\s]+", key))
            if all(word in tokens for word in dropped) and set(base.split()) <= tokens:
                return True
        return False

    @staticmethod
    def _spellings(name: str):
        yield name
        if "," in name:
            head, _, tail = name.partition(",")
            yield f"{head}{tail}".replace("  ", " ").strip()   # "heparin porcine"
            yield head.strip()                                  # "heparin"
            name = head.strip()
        else:
            words = name.split()
            if len(words) > 1:
                yield " ".join(words[:-1]) + ", " + words[-1]
        words = name.split()
        while len(words) > 1 and words[-1].upper() in SALT_SUFFIXES:
            words = words[:-1]
            yield " ".join(words)

    def form_tiers(self, phrase: str | None) -> list[list[set[int]]] | None:
        """The phrase's dose forms, as widening steps of ordered alternatives.

        Order is the point. `sodium chloride 9 MG/ML Injectable Solution` and
        `sodium chloride 9 MG/ML Injection` are the same product filed twice, and a
        source string saying `IV BOLUS` matches both. Trying the alternatives one name
        at a time, in the order the lexicon lists them, picks the first spelling that
        exists instead of calling the pair a tie -- the choice is a stated preference
        between two spellings of one form, not a score between two drugs.
        """
        if phrase is None:
            return None
        tiers = DOSE_FORMS.get(phrase)
        if tiers is None:
            return None
        out: list[list[set[int]]] = []
        for tier in tiers:
            step = [self._forms[name.lower()] for name in tier if name.lower() in self._forms]
            if step:
                out.append(step)
        return out or None

    def concept(self, concept_id: int) -> tuple[str, str]:
        return self._concepts[concept_id]

    def lookup(self, signature: frozenset, form_id: int) -> list[int]:
        return self._by_key.get((signature, form_id), [])


def _vocabulary_signature(amount, amount_unit, numerator, numerator_unit,
                          denominator, denominator_unit) -> tuple | None:
    if amount is not None and amount_unit:
        canonical = _canonical(float(amount), amount_unit)
        return None if canonical is None else ("amount", canonical[0], _round(canonical[1]))
    if numerator is not None and numerator_unit and denominator_unit:
        # A blank denominator value is RxNorm's way of writing "per one": `10 MG/ML` is
        # stored as numerator 10 mg, denominator unit mL, denominator value empty.
        # Reading that as "no strength" is what left `Acetaminophen 10 MG/ML
        # Intravenous Solution` unreachable while it sat in the vocabulary.
        top = _canonical(float(numerator), numerator_unit)
        bottom = _canonical(float(denominator) if denominator else 1.0, denominator_unit)
        if top is None or bottom is None or bottom[0] != "volume" or bottom[1] == 0:
            return None
        return ("ratio", top[0], _round(top[1] / bottom[1]))
    return ("none",)


# --------------------------------------------------------------------------
# the match
# --------------------------------------------------------------------------

def _readings(parsed: ParsedDrug) -> list[tuple[str, list[tuple | None]]]:
    """The strength readings to try, most literal first.

    A hospital writes an IV bag as `2 GRAM/100 ML`; RxNorm Extension files the same bag
    as `2000 MG`, because the bag is the unit that gets hung. Both are readings of one
    string, so both are tried -- the concentration first, since that is what the string
    literally says.
    """
    literal = [_signature(component.strength) for component in parsed.components]
    out = [("as_written", literal)]
    whole = []
    for component in parsed.components:
        strength = component.strength
        if (strength is None or strength.kind != "ratio" or strength.origin != "explicit"
                or strength.denominator in (None, 1.0)
                or strength.denominator_unit not in ("mL", "L")):
            whole = []
            break
        whole.append(_signature(Strength("amount", strength.value, strength.unit)))
    if whole:
        out.append(("total_amount", whole))
    return out


_WORD = re.compile(r"[a-z]{3,}")


def _preferred(index: DrugIndex, concept_ids: list[int], ingredient_names: list[str],
               source_words: set[str]) -> list[int]:
    """Narrow candidates that already agree on ingredient, strength and dose form.

    Everything reaching here denotes the same drug at the same strength in the same
    form; what differs is how much extra the concept's *name* claims.
    `insulin aspart-szjj 100 UNT/ML Pen Injector` names a particular biosimilar and
    `Once-Daily gabapentin 600 MG Oral Tablet` a particular product line -- neither of
    which the source string said. So, in order:

    1. keep the candidates whose name begins with the ingredient, dropping the ones
       that put a qualifier in front of it;
    2. keep those carrying the most words the source itself wrote, so `HEPARIN
       (PORCINE)` keeps `heparin sodium, porcine` over plain `heparin`;
    3. among what is left, take the shortest name -- the same rule, and the same
       reason, as the lexical candidate ranking: specificity nobody wrote down is the
       failure that matters here.

    None of this chooses between different drugs. If step 3 is still needed to separate
    two candidates that differ in meaning, the earlier steps have already failed and
    the term should not have got this far.
    """
    if len(concept_ids) < 2:
        return concept_ids
    plain = [c for c in concept_ids
             if any(index._names.get(c, "").startswith(n) for n in ingredient_names)]
    concept_ids = plain or concept_ids
    if len(concept_ids) < 2:
        return concept_ids
    scored = [(len(source_words & set(_WORD.findall(index._names.get(c, "")))), c)
              for c in concept_ids]
    best = max(score for score, _ in scored)
    concept_ids = [c for score, c in scored if score == best]
    if len(concept_ids) < 2:
        return concept_ids
    shortest = min(len(index._names.get(c, "")) for c in concept_ids)
    return [c for c in concept_ids if len(index._names.get(c, "")) == shortest]


def match_drug(index: DrugIndex, source: str,
               local_noise: Sequence[str] = ()) -> tuple[ParsedDrug, str, list[DrugMatch]]:
    """Match one source string. Returns (parse, status, matches).

    ``status`` is ``unique`` only when exactly one concept satisfied a whole reading.
    Everything else -- ``ambiguous``, ``no_match``, ``no_ingredient``, ``no_dose_form``,
    ``unparsed_strength`` -- means the term is not resolved and stays in review.
    """
    parsed = parse_drug_name(source, local_noise)
    ingredient_sets: list[set[int]] = []
    ingredient_names: list[str] = []
    for component in parsed.components:
        ids = index.ingredient_ids(component.ingredient_text)
        if not ids:
            return parsed, "no_ingredient", []
        ingredient_sets.append(ids)
        ingredient_names.append(component.ingredient_text.lower())

    tiers = index.form_tiers(parsed.dose_form)
    if parsed.dose_form is not None and tiers is None:
        return parsed, "no_dose_form", []
    if tiers is None:
        # No form in the string: the strength alone has to identify the concept, so the
        # candidate set is every form -- except that a strength the source wrote per
        # millilitre describes something poured, and matching it to a tablet would
        # assert a form the source did not state *and* one the strength rules out.
        every = set(f for ids in index._forms.values() for f in ids)
        if any(c.strength is not None and c.strength.kind == "ratio"
               and c.strength.origin == "explicit"
               and c.strength.denominator_unit in ("mL", "L")
               for c in parsed.components):
            every &= index._volume_forms
        tiers = [[every]]

    have_strength = all(c.strength is not None for c in parsed.components)
    if not have_strength and parsed.had_number:
        # A number was written and not understood. Matching a concept that carries no
        # strength would silently drop it, so the term goes to a person instead.
        return parsed, "unparsed_strength", []

    source_words = set(_WORD.findall(parsed.source.lower()))
    ambiguous: list[DrugMatch] = []
    for reading, signatures in _readings(parsed):
        if any(s is None for s in signatures):
            continue
        combinations = _combinations(ingredient_sets, signatures)
        stop = False
        for depth, step in enumerate(tiers):
            for alternative in step:
                found: set[int] = set()
                for signature in combinations:
                    for form in alternative:
                        found.update(index.lookup(signature, form))
                if not found:
                    continue
                candidates = _preferred(index, sorted(found), ingredient_names, source_words)
                route = reading if depth == 0 else f"{reading}_widened_form"
                matches = [DrugMatch(c, *index.concept(c), route) for c in candidates]
                if len(matches) == 1:
                    return parsed, "unique", matches
                ambiguous = ambiguous or matches
                stop = True
                break
            if stop:
                break
    if ambiguous:
        return parsed, "ambiguous", ambiguous
    return parsed, "no_match", []


def _combinations(ingredient_sets: list[set[int]], signatures: list[tuple]) -> list[frozenset]:
    """Every way of assigning the parsed ingredients to concrete ingredient ids."""
    out: list[list[tuple]] = [[]]
    for ids, signature in zip(ingredient_sets, signatures):
        out = [prefix + [(i, signature)] for prefix in out for i in sorted(ids)]
        if len(out) > 256:            # a name this ambiguous is not worth resolving
            return []
    return [frozenset(parts) for parts in out]
