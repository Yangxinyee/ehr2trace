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

from ehr2trace.schema import (
    ANCHOR_SCHEMA,
    CANONICAL_EVENT_SCHEMA,
    COHORT_MEMBERSHIP_SCHEMA,
    QUARANTINE_SCHEMA,
)
from ehr2trace.validate import ENGINE_ONLY_COLUMNS, READ_COLUMNS

SOURCE = Path(__file__).resolve().parents[2] / "src" / "ehr2trace" / "validate.py"

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
    # Named, but read only in the query engine; the test below holds them to that.
    engine_only = set(ENGINE_ONLY_COLUMNS) if table == "events" else set()
    missing = sorted((named & schema_columns) - whitelisted - engine_only)
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


def test_a_column_read_only_in_the_engine_is_never_read_from_a_frame():
    """The columns left out of the load must stay out of every frame read.

    Validation once loaded eight columns no check read in memory, 7 GB of MIMIC-IV's old
    build and about 19 GB of a schema-3 one, only because this whitelist matched their
    quoted names. They are unloaded now, and a check that starts reading one from the frame
    has to move it back into READ_COLUMNS -- this test says so before a large dataset does.
    """
    text = SOURCE.read_text(encoding="utf-8")
    offenders = []
    for name in sorted(ENGINE_ONLY_COLUMNS):
        quoted = rf"""["']{re.escape(name)}["']"""
        patterns = (
            rf"pl\.col\(\s*{quoted}",
            rf"\.get_column\(\s*{quoted}",
            rf"\.(?:select|with_columns|filter|sort|group_by|unique|drop_nulls|agg|pivot|rename)\([^)]*{quoted}",
            # A frame join names its key; ``", ".join(...)`` of a formatted string does not.
            rf"\.join\([^)]*\b(?:on|left_on|right_on)\s*=\s*[\[(]?\s*{quoted}",
            rf"\[\s*{quoted}\s*\]\s*\.(?:to_list|unique|n_unique|drop_nulls|null_count|cast|str|dt|list|is_in|is_null|sum|max|min|mean)\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                offenders.append(f"{name} at line {text.count(chr(10), 0, match.start()) + 1}")
    assert not offenders, (
        f"validate.py reads engine-only columns from a frame: {offenders}. Layers.load does not "
        "load them; move the column into READ_COLUMNS or read it in the engine."
    )


def test_the_engine_only_columns_are_real_and_not_loaded():
    assert not set(ENGINE_ONLY_COLUMNS) & set(READ_COLUMNS["events"])
    assert set(ENGINE_ONLY_COLUMNS) <= set(CANONICAL_EVENT_SCHEMA.names)
