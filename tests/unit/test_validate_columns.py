"""The column whitelist must keep step with what the checks read.

`Layers.load` projects each canonical table down to the columns the checks use, because
loading everything got `validate` killed at 159 GB on MIMIC-IV. A projection is only
safe while it is complete: a check that starts reading a new column would otherwise
raise on whichever dataset is large enough for anyone to be running it against.

So the whitelist is checked against the source of the checks themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ehr2cdm.schema import (
    ANCHOR_SCHEMA,
    CANONICAL_EVENT_SCHEMA,
    COHORT_MEMBERSHIP_SCHEMA,
    QUARANTINE_SCHEMA,
)
from ehr2cdm.validate import READ_COLUMNS

SOURCE = Path(__file__).resolve().parents[2] / "src" / "ehr2cdm" / "validate.py"

#: event_source is deliberately absent: it is never materialised at all. Its `event_id`
#: column alone is 18 GB on MIMIC-IV, so the checks that read it do so in the query
#: engine, over the parquet file, where the join can spill.
SCHEMAS = {
    "events": CANONICAL_EVENT_SCHEMA,
    "anchors": ANCHOR_SCHEMA,
    "cohort_membership": COHORT_MEMBERSHIP_SCHEMA,
    "quarantine": QUARANTINE_SCHEMA,
}


def quoted_names() -> set[str]:
    text = SOURCE.read_text(encoding="utf-8")
    # Everything this module names as a string. Broad on purpose: a false positive here
    # costs one column of memory, a false negative costs a crash on a big dataset.
    return set(re.findall(r'''["']([a-z][a-z0-9_]*)["']''', text))


@pytest.mark.parametrize("table", sorted(SCHEMAS))
def test_every_column_the_checks_name_is_loaded(table: str):
    named = quoted_names()
    schema_columns = set(SCHEMAS[table].names)
    whitelisted = set(READ_COLUMNS.get(table, ()))
    missing = sorted((named & schema_columns) - whitelisted)
    assert not missing, (
        f"{table}: validate.py names {missing} but Layers.load does not load them. "
        "Add them to READ_COLUMNS, or the check will raise on a dataset large enough "
        "to need the projection."
    )


@pytest.mark.parametrize("table", sorted(SCHEMAS))
def test_the_whitelist_only_names_real_columns(table: str):
    unknown = sorted(set(READ_COLUMNS.get(table, ())) - set(SCHEMAS[table].names))
    assert not unknown, f"{table}: whitelist names columns that do not exist: {unknown}"


def test_the_largest_tables_are_not_materialised_at_all():
    """The point of the exercise, pinned so it cannot quietly regress."""
    assert "event_source" not in READ_COLUMNS, (
        "the link table must not be loaded: 301 million rows whose event_id column is "
        "18 GB, joined against an events column of the same size"
    )
    assert "raw_row" not in READ_COLUMNS["quarantine"], "the entire raw text of every quarantined row"
    assert "source_name" not in READ_COLUMNS["events"]
