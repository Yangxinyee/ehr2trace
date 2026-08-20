"""Error taxonomy.

Three kinds, and the difference matters:

* :class:`BlockerError` -- something only the data owner can answer (timezone, the
  reference date for an age, the definition behind a cohort label). Never guessed,
  never defaulted; the run stops and ``inspect`` lists it.
* :class:`ConfigError` -- the dataset YAML is wrong or references something unknown.
* :class:`QuarantineRow` -- one row could not be parsed. Not fatal: the row is written
  to ``quarantine/`` with its reason and the run continues.
"""

from __future__ import annotations


class Ehr2CdmError(Exception):
    """Base class."""


class ConfigError(Ehr2CdmError):
    """The dataset configuration is invalid or refers to an unregistered name."""


class BlockerError(Ehr2CdmError):
    """A question only the data owner can answer. Must not be defaulted away."""

    def __init__(self, blocker_id: str, message: str, needed_from: str = "data owner"):
        super().__init__(f"[{blocker_id}] {message} (needed from: {needed_from})")
        self.blocker_id = blocker_id
        self.message = message
        self.needed_from = needed_from


class QuarantineRow(Ehr2CdmError):
    """Raised by a parser to send a single row to quarantine with a reason."""

    def __init__(self, issue: str, detail: str = ""):
        super().__init__(f"{issue}: {detail}" if detail else issue)
        self.issue = issue
        self.detail = detail
