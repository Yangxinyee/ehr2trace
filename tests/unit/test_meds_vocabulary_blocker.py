"""MEDS does not build unmapped beside an OMOP layer that mapped."""

from __future__ import annotations

from types import SimpleNamespace

import duckdb
import pytest

from ehr2trace.errors import BlockerError
from ehr2trace.meds import _refuse_to_unmap_what_omop_mapped
from ehr2trace.paths import WorkLayout


def _omop(layout: WorkLayout, vocabulary_version: str) -> None:
    layout.omop_dir.mkdir(parents=True)
    con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"))
    con.execute("CREATE TABLE cdm_source (vocabulary_version VARCHAR)")
    con.execute("INSERT INTO cdm_source VALUES (?)", [vocabulary_version])
    con.close()


def test_no_omop_layer_means_nothing_to_disagree_with(tmp_path):
    _refuse_to_unmap_what_omop_mapped(WorkLayout(root=tmp_path, dataset_id="t"), SimpleNamespace(available=False))


def test_an_omop_layer_built_without_a_vocabulary_is_no_constraint(tmp_path):
    layout = WorkLayout(root=tmp_path, dataset_id="t")
    _omop(layout, "none")
    _refuse_to_unmap_what_omop_mapped(layout, SimpleNamespace(available=False))


def test_an_omop_layer_built_with_one_blocks_a_meds_build_without(tmp_path):
    layout = WorkLayout(root=tmp_path, dataset_id="t")
    _omop(layout, "v5.0 29-AUG-26")
    with pytest.raises(BlockerError) as raised:
        _refuse_to_unmap_what_omop_mapped(layout, SimpleNamespace(available=False))
    assert raised.value.blocker_id == "MEDS_VOCABULARY_MISSING"
    assert "v5.0 29-AUG-26" in str(raised.value)


def test_with_the_vocabulary_present_nothing_is_asked(tmp_path):
    layout = WorkLayout(root=tmp_path, dataset_id="t")
    _omop(layout, "v5.0 29-AUG-26")
    _refuse_to_unmap_what_omop_mapped(layout, SimpleNamespace(available=True))
