"""The MEDS half of the remediation plan of 2026-09-13 (T1.10 code descriptions, T1.11
extension columns), on the hand-built canonical layer.

See :mod:`tests.integration.hand_built_layer` for why the layer is written by hand.
"""

from __future__ import annotations

import json

import polars as pl
import pyarrow.parquet as pq
import pytest

import meds as meds_spec
from ehr2trace.meds import EXTENSION_COLUMNS_ADDED, MEDS_SCHEMA, REQUIRED_MEDS_COLUMNS, build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind
from tests.integration.hand_built_layer import (
    T,
    WARD,
    config,
    demographics,
    event,
    meds_frame,
    the_layer,
    write_canonical,
)
from tests.unit.test_unpunctuated_codes import write_vocab


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> WorkLayout:
    layout = write_canonical(tmp_path_factory.mktemp("hand"), the_layer())
    cfg = config()
    build_omop(cfg, layout, vocabulary_dir=None)
    build_meds(cfg, layout)
    return layout


# -- T1.11 extension columns ------------------------------------------------------------------


def test_meds_rows_carry_the_version_two_extension_columns(published):
    frame = meds_frame(published)
    assert set(EXTENSION_COLUMNS_ADDED["2"]) <= set(frame.columns)
    by_id = {r["event_id"]: r for r in frame.to_dicts()}
    rated = by_id["order-with-rate"]
    assert (rated["rate"], rated["rate_source"], rated["rate_unit"]) == (10.0, "10", "mL/hr")
    assert (by_id["creat"]["unit_normalized"], by_id["creat"]["value_number_normalized"]) == ("mg/dL", 1.0)
    assert by_id["visit-adm"]["discharged_to"] == "HOME"
    assert by_id["spo2-1"]["rate"] is None and by_id["spo2-1"]["action"] is None


def test_the_shards_still_validate_against_the_installed_meds_schema(published):
    for path in sorted((published.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")):
        meds_spec.DataSchema.validate(pq.read_table(path))


def test_dataset_metadata_declares_extension_columns_version_two(published):
    metadata = json.loads((published.meds_dir / meds_spec.dataset_metadata_filepath).read_text())
    assert metadata["extension_columns_version"] == 2
    assert metadata["extension_columns"] == [f.name for f in MEDS_SCHEMA][REQUIRED_MEDS_COLUMNS:]
    assert metadata["extension_columns_added"]["2"] == list(EXTENSION_COLUMNS_ADDED["2"])
    assert set(metadata["extension_columns_added"]["2"]) <= set(metadata["extension_columns"])


def test_a_canonical_file_without_the_version_two_columns_still_publishes(tmp_path):
    """A layer written under schema version 1 is a valid input: the new columns read as nulls."""
    dropped = tuple(EXTENSION_COLUMNS_ADDED["2"])
    layout = write_canonical(tmp_path, [e for e in the_layer() if e["subject_id"] == WARD], drop_columns=dropped)
    assert not set(dropped) & set(pq.read_schema(layout.canonical_path("events")).names)
    cfg = config()
    build_omop(cfg, layout, vocabulary_dir=None)
    build_meds(cfg, layout)
    frame = meds_frame(layout)
    assert set(dropped) <= set(frame.columns)
    assert all(frame[column].null_count() == frame.height for column in dropped)


# -- T1.10 code descriptions --------------------------------------------------------------------


def test_a_source_code_is_described_by_its_most_frequent_name(published):
    """Three rows say SpO2 and one says Post SpO2; the alphabet used to pick the one."""
    codes = pl.read_parquet(published.meds_dir / meds_spec.code_metadata_filepath)
    row = codes.filter(pl.col("code").str.ends_with("/spo2")).to_dicts()
    assert len(row) == 1 and row[0]["description"] == "SpO2" and row[0]["n_events"] == 4
    assert row[0]["event_kind"] == "measurement"


def test_a_mapped_code_is_described_by_its_concept_name(tmp_path):
    vocab = write_vocab(
        tmp_path / "v",
        "3016723\tCreatinine [Mass/volume] in Serum or Plasma\tMeasurement\tLOINC\tLab Test\tS\t2160-0\t"
        "19700101\t20991231\t\n",
    )
    layout = write_canonical(tmp_path, [
        *demographics(WARD),
        event("c1", WARD, EventKind.measurement, code_system="LOINC", source_code="2160-0",
              source_name="Creat, serum", event_time=T(2020, 3, 1), value_number=1.0),
        event("c2", WARD, EventKind.measurement, code_system="LOINC", source_code="2160-0",
              source_name="CREATININE", event_time=T(2020, 3, 2), value_number=1.1),
    ])
    cfg = config()
    build_omop(cfg, layout, vocabulary_dir=vocab)
    build_meds(cfg, layout, vocabulary_dir=vocab)
    codes = pl.read_parquet(layout.meds_dir / meds_spec.code_metadata_filepath).to_dicts()
    mapped = [c for c in codes if c["code"] == "OMOP/3016723"]
    assert len(mapped) == 1
    assert mapped[0]["description"] == "Creatinine [Mass/volume] in Serum or Plasma"
    assert mapped[0]["mapping_status"] == "mapped" and mapped[0]["n_events"] == 2
