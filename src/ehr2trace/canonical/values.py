"""Result value parsing (design section 5.4).

Lab and study results are not clean numbers. The forms below are tried in order and a
value that matches none is quarantined -- a forced numeric cast would turn "<0.5" into
0.5 and "35-40" into 35, and nothing downstream could tell.

===========================  ==========================================
Form                         Destination
===========================  ==========================================
plain number                 ``value_number``
number + unit in one cell    ``value_number`` + ``unit_source``
range (``35-40``)            ``value_low`` / ``value_high``
comparator (``<0.5``)        ``value_text`` verbatim + ``COMPARATOR_VALUE``
sentinel text                ``value_text`` + ``NON_NUMERIC_RESULT``
free text                    ``value_text``
signature line               ``value_text`` + ``SIGNATURE_LINE`` (not a result)
===========================  ==========================================

``value_number`` and ``value_text`` never carry the same meaning at once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

from ehr2trace.errors import QuarantineRow
from ehr2trace.schema import QualityFlag, QuarantineReason

Expect = Literal["auto", "numeric", "text"]

_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
RE_NUMBER = re.compile(rf"^{_NUMBER}$")
RE_NUMBER_UNIT = re.compile(rf"^(?P<num>{_NUMBER})\s+(?P<unit>[^\s].*)$")
RE_RANGE = re.compile(rf"^(?P<low>{_NUMBER})\s*(?:-|–|—|to)\s*(?P<high>[+-]?\d+\.?\d*)$")
RE_COMPARATOR = re.compile(rf"^(?P<op><=|>=|<|>)\s*(?P<num>{_NUMBER})$")

#: Generic result sentinels. Deliberately not dataset-specific; a dataset may extend
#: the list from its YAML.
DEFAULT_SENTINELS: tuple[str, ...] = (
    "see below",
    "see comment",
    "see comments",
    "see note",
    "see report",
    "see previous",
    "pending",
    "not calculated",
    "unable to calculate",
    "cancelled",
    "canceled",
    "qns",
    "tnp",
    "no result",
)

#: Generic attestation lines that appear inside result text and are not results.
DEFAULT_SIGNATURE_PATTERNS: tuple[str, ...] = (
    r"^\s*(?:electronically\s+)?(?:confirmed|verified|signed|reviewed|finalized|edited)\b.*\bby\b",
    r"^\s*(?:confirmed|verified|signed)\s+by\b",
)


@dataclass(frozen=True)
class ValueParsingSpec:
    sentinels: tuple[str, ...] = DEFAULT_SENTINELS
    signature_patterns: tuple[str, ...] = DEFAULT_SIGNATURE_PATTERNS
    null_literals: tuple[str, ...] = ("NULL",)


@dataclass
class ParsedValue:
    number: float | None = None
    text: str | None = None
    low: float | None = None
    high: float | None = None
    unit: str | None = None
    flags: list[str] = field(default_factory=list)
    #: which form matched: absent / number / number_unit / range / comparator /
    #: sentinel / signature / text
    form: str = "absent"

    @property
    def is_absent(self) -> bool:
        return self.form == "absent"


def _to_float(text: str) -> float:
    return float(text)


def parse_value(
    raw: object,
    unit_hint: object = None,
    spec: ValueParsingSpec = ValueParsingSpec(),
    expect: Expect = "auto",
) -> ParsedValue:
    """Parse one result cell. Raises :class:`QuarantineRow` rather than guessing."""
    unit = _clean(unit_hint, spec)
    if raw is None:
        return ParsedValue(unit=unit, form="absent")
    if isinstance(raw, bool):
        return ParsedValue(text=("true" if raw else "false"), unit=unit, form="text")
    if isinstance(raw, (int, float)):
        return ParsedValue(number=float(raw), unit=unit, form="number")

    text = _clean(raw, spec)
    if text is None:
        return ParsedValue(unit=unit, form="absent")

    if RE_NUMBER.match(text):
        return ParsedValue(number=_to_float(text), unit=unit, form="number")

    m = RE_NUMBER_UNIT.match(text)
    if m and not RE_RANGE.match(text):
        embedded = m.group("unit").strip()
        return ParsedValue(
            number=_to_float(m.group("num")),
            unit=unit or embedded,
            flags=[] if (unit is None or unit == embedded) else [str(QualityFlag.UNIT_UNPARSED)],
            form="number_unit",
        )

    m = RE_RANGE.match(text)
    if m:
        low, high = _to_float(m.group("low")), _to_float(m.group("high"))
        if low <= high:
            return ParsedValue(
                low=low,
                high=high,
                text=text,
                unit=unit,
                flags=[str(QualityFlag.RANGE_VALUE)],
                form="range",
            )

    m = RE_COMPARATOR.match(text)
    if m:
        # The threshold is deliberately not written to value_number: "<0.5" is not 0.5.
        return ParsedValue(
            text=text, unit=unit, flags=[str(QualityFlag.COMPARATOR_VALUE)], form="comparator"
        )

    for pattern in spec.signature_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return ParsedValue(
                text=text, unit=unit, flags=[str(QualityFlag.SIGNATURE_LINE)], form="signature"
            )

    if text.strip().lower() in spec.sentinels:
        return ParsedValue(
            text=text, unit=unit, flags=[str(QualityFlag.NON_NUMERIC_RESULT)], form="sentinel"
        )

    if expect == "numeric":
        raise QuarantineRow(QuarantineReason.UNPARSEABLE_VALUE, text[:200])

    return ParsedValue(text=text, unit=unit, form="text")


def _clean(value: object, spec: ValueParsingSpec) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in spec.null_literals:
        return None
    return text


def spec_from_config(sentinels: Sequence[str] | None, null_literals: Sequence[str]) -> ValueParsingSpec:
    return ValueParsingSpec(
        sentinels=tuple(s.lower() for s in (sentinels or DEFAULT_SENTINELS)),
        null_literals=tuple(null_literals),
    )
