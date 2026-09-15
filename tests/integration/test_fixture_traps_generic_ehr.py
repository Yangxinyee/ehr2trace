"""The fixture traps of remediation plan T1.12 that live in the generic fixture.

The generic fixture is hand-written CSV (``tests/fixtures/generic_ehr/``), so a trap here
is rows and columns written for it, settled by declarations in
``datasets/generic_ehr.yaml``. As with the shape fixture's traps
(``test_fixture_traps_ctpe_shape.py``), each is asserted on the fixture built with its own
declarations and on the same raw files without the declaration that settles it; where a
fault in ``ehr2trace.faults`` injects the same damage into a finished build, the test
names it.

A declaration that only tells the checks what to expect -- ``expected_empty``,
``ignored_columns`` -- is not part of the configuration hash, so a build without it is
this build byte for byte. Those traps judge the one build twice, with and without the
declaration, and assert that the hash agrees.
"""

from __future__ import annotations

import json

import polars as pl
import pytest

from ehr2trace.schema import QuarantineReason
from tests.integration.test_generic_ehr_pipeline import CONFIG, FIXTURE
from tests.integration.trap_builds import Build, build, edited


@pytest.fixture(scope="module")
def after(tmp_path_factory) -> Build:
    return build(CONFIG, FIXTURE, tmp_path_factory.mktemp("generic_traps_after"))


def _manifest(b: Build) -> dict:
    return json.loads((b.layout.manifest_dir / "inputs.json").read_text(encoding="utf-8"))


# -- trap 2: a wide vital-sign table declared as components ------------------------------------


def test_a_wide_table_read_as_components_yields_nothing_and_declares_it(after):
    """Trap 2 (P-M2). Fault SOURCE_PARSED_ROWS_AND_YIELDED_NOTHING covers the same ground on a built layer.

    The table holds one column per vital sign and no component name, so every parsed
    row is quarantined for lacking one and no event comes from it. The source declares
    that, so the check reports it as expected rather than silent.
    """
    parsed = after.source_rows("vitals_wide")
    assert parsed.height == 3, "the fixture's three wide rows were read"
    assert after.canonical().filter(pl.col("source_id") == "vitals_wide").is_empty()

    quarantined = after.canonical("quarantine").filter(pl.col("source_id") == "vitals_wide")
    assert set(quarantined["reason"].to_list()) == {str(QuarantineReason.UNPARSEABLE_VALUE)}
    assert set(quarantined["source_row_id"].to_list()) == set(parsed["source_row_id"].to_list())

    result = after.checks("SOURCE_YIELDS_EVENTS")["SOURCE_YIELDS_EVENTS"]
    assert result.passed, result.detail
    assert result.metrics["declared_empty"] == ["site_a/vitals_wide"]


def test_without_expected_empty_the_silent_source_fails(after):
    """Trap 2, before: the same build judged without ``expected_empty``."""
    undeclared = edited(after.cfg, "vitals_wide", expected_empty=None)
    assert undeclared.config_hash() == after.cfg.config_hash(), "a declaration changes no produced byte"

    result = after.checks("SOURCE_YIELDS_EVENTS", cfg=undeclared)["SOURCE_YIELDS_EVENTS"]
    assert not result.passed
    assert result.metrics["silent"] == ["site_a/vitals_wide (3 rows parsed)"]


# -- trap 11: a delivered column that no role reads ---------------------------------------------


def test_a_delivered_column_no_role_reads_is_declared_with_its_reason(after):
    """Trap 11 (P-C10). Fault RAW_COLUMN_NEVER_DECLARED covers the same ground on a built manifest."""
    delivered = {c for u in _manifest(after)["inputs"] if u["source_id"] == "orders" for c in u["columns"]}
    assert "order_channel" in delivered, "the fixture must deliver the column"

    result = after.checks("RAW_COVERAGE_DECLARED")["RAW_COVERAGE_DECLARED"]
    assert result.passed, result.detail
    orders = result.metrics["columns"]["orders"]
    assert orders["undeclared"] == [] and orders["ignored_with_reason"] == 1


def test_without_the_ignored_column_raw_coverage_fails(after):
    """Trap 11, before: the same build judged without the ``ignored_columns`` entry."""
    undeclared = edited(after.cfg, "orders", ignored_columns={})
    assert undeclared.config_hash() == after.cfg.config_hash(), "a declaration changes no produced byte"

    result = after.checks("RAW_COVERAGE_DECLARED", cfg=undeclared)["RAW_COVERAGE_DECLARED"]
    assert not result.passed
    assert result.metrics["columns"]["orders"]["undeclared"] == ["order_channel"]
