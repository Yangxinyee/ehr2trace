"""Timestamp parsing and timezone handling (design section 5.3).

Two rules drive this module:

* an unparseable timestamp is quarantined, never coerced and never replaced by "now";
* a naive local timestamp is converted to UTC only with a timezone the data owner
  declared, or one an operator passed explicitly and that is then recorded on every
  affected row. The developer machine's zone is never consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from ehr2cdm.errors import QuarantineRow
from ehr2cdm.schema import QualityFlag, QuarantineReason

#: More fractional digits than %f accepts turn up in one of the batches; trimming is a
#: documented normalization, not a guess -- sub-microsecond precision is meaningless here.
_FRACTION = re.compile(r"(\.\d{7,})")


#: Formats the source layer itself writes. A workbook date cell is stored in canonical
#: ISO form (section 5.1), so these are always accepted regardless of what a dataset
#: declares -- a config should describe the *source's* formats, and forgetting to also
#: list our own storage format would quarantine every date that arrived as a date.
BUILTIN_FORMATS: tuple[str, ...] = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class TimeContext:
    """Everything time parsing needs, resolved once per run."""

    formats: tuple[str, ...]
    null_literals: tuple[str, ...]
    timezone_name: str | None
    #: True when the zone came from an operator flag rather than the dataset config
    timezone_assumed: bool = False

    @property
    def zone(self) -> ZoneInfo | None:
        return ZoneInfo(self.timezone_name) if self.timezone_name else None


def _trim_fraction(text: str) -> str:
    match = _FRACTION.search(text)
    if not match:
        return text
    return text[: match.start()] + match.group(1)[:7] + text[match.end() :]


def parse_naive(value: object, ctx: TimeContext) -> datetime | None:
    """Source cell -> naive local ``datetime``. ``None`` means "absent"."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=value.microsecond)
    if isinstance(value, date):
        return datetime.combine(value, time())
    text = str(value).strip()
    if not text or text in ctx.null_literals:
        return None
    candidate = _trim_fraction(text)
    for fmt in tuple(ctx.formats) + BUILTIN_FORMATS:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    raise QuarantineRow(QuarantineReason.UNPARSEABLE_TIME, text)


def to_utc(naive: datetime | None, ctx: TimeContext) -> tuple[datetime | None, list[str]]:
    """Naive local -> naive UTC instant, plus any flags the conversion earned."""
    if naive is None:
        return None, []
    zone = ctx.zone
    if zone is None:
        # Only reachable under the explicit store_naive_flagged policy; the value is
        # kept as recorded and marked, never silently treated as UTC.
        return naive.replace(microsecond=0), [str(QualityFlag.TZ_ASSUMED)]
    flags: list[str] = [str(QualityFlag.TZ_ASSUMED)] if ctx.timezone_assumed else []
    localized = naive.replace(tzinfo=zone, fold=0)
    utc = localized.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    return utc, flags


def parse_utc(value: object, ctx: TimeContext) -> tuple[datetime | None, list[str]]:
    return to_utc(parse_naive(value, ctx), ctx)


def as_date(value: datetime | None) -> date | None:
    return value.date() if value is not None else None


def looks_date_only(value: object) -> bool:
    """Whether a source cell carries a date with no time component.

    One batch ships anchors as full timestamps and the other as bare dates; recording
    which is which is how the lost time component stays visible instead of becoming a
    silent midnight.
    """
    if isinstance(value, datetime):
        return (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
    if isinstance(value, date):
        return True
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))


def days_between(a: datetime | None, b: datetime | None) -> float | None:
    """Signed day difference, recomputed from timestamps -- never read off a rank column."""
    if a is None or b is None:
        return None
    delta: timedelta = a - b
    return delta.total_seconds() / 86400.0
