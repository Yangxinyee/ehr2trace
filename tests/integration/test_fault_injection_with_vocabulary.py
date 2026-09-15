"""The vocabulary fault injects on a layer that has concepts to strip, and a broken injection is not a detection.

MEDS_BUILT_WITHOUT_VOCABULARY skips on the shape fixture, which is built without a
vocabulary, so the injection path ran only where the paper's experiments ran with one --
and there it raised half way: the shards were rewritten, the code metadata was not, and the
checks that fired on that wreck were counted as detecting the fault. This builds a small
layer with a made-up vocabulary so the path runs in every test run.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

import meds as meds_spec
from ehr2trace.faults import FAULTS, Fault, clone_work_tree, inject
from ehr2trace.meds import build_meds
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind
from ehr2trace.validate import CHECKS, Layers
from tests.integration.hand_built_layer import DATASET, WARD, T, config, demographics, event, write_canonical
from tests.unit.test_unpunctuated_codes import write_vocab


def mapped_layer(root: Path):
    vocab = write_vocab(
        root / "vocabulary",
        "3016723\tCreatinine [Mass/volume] in Serum or Plasma\tMeasurement\tLOINC\tLab Test\tS\t2160-0\t19700101\t20991231\t\n",
        "3016723\t3016723\tMaps to\t19700101\t20991231\t\n",
    )
    base = config()
    cfg = base.model_copy(update={"sources": {"hand": base.sources["hand"].model_copy(update={"code_system": "LOINC"})}})
    layout = write_canonical(root / "work", [
        *demographics(WARD),
        event("creat-1", WARD, EventKind.measurement, code_system="LOINC", source_code="2160-0",
              source_name="Creatinine", event_time=T(2020, 3, 1, 12), value_number=1.0),
        event("creat-2", WARD, EventKind.measurement, code_system="LOINC", source_code="2160-0",
              source_name="Creatinine", event_time=T(2020, 3, 2, 12), value_number=1.1),
    ])
    build_omop(cfg, layout, vocabulary_dir=vocab)
    build_meds(cfg, layout, vocabulary_dir=vocab)
    return cfg, layout


def shards(layout: WorkLayout) -> pl.DataFrame:
    return pl.concat([pl.read_parquet(p, columns=["code", "omop_concept_id", "source_code"])
                      for p in sorted((layout.meds_dir / "data").rglob("*.parquet"))])


def concepts_check(cfg, layout: WorkLayout):
    return dict(CHECKS)["MEDS_CONCEPTS_ARE_OMOPS"][0](Layers.load(cfg, layout))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return mapped_layer(tmp_path_factory.mktemp("vocab_fault"))


def test_the_vocabulary_fault_strips_concepts_and_leaves_consistent_metadata(built, tmp_path: Path):
    cfg, source = built
    assert shards(source)["omop_concept_id"].drop_nulls().to_list(), "the layer must carry a concept to strip"
    assert concepts_check(cfg, source).passed

    (fault,) = [f for f in FAULTS if f.id == "MEDS_BUILT_WITHOUT_VOCABULARY"]
    clone_work_tree(source.root, tmp_path / DATASET)
    layout = WorkLayout(root=tmp_path / DATASET, dataset_id=DATASET)
    injection = inject(fault, layout, cfg)

    assert injection.error is None, injection.error
    assert injection.injected, injection.effect
    rows = shards(layout)
    assert rows["omop_concept_id"].null_count() == rows.height
    assert not [c for c in rows["code"].to_list() if c.startswith("OMOP/")]

    codes = pl.read_parquet(layout.meds_dir / meds_spec.code_metadata_filepath)
    assert set(codes["code"].to_list()) == set(rows["code"].unique().to_list())
    (description,) = codes.filter(pl.col("code") == "SOURCE/hand/2160-0")["description"].to_list()
    assert description == "Creatinine", "a stage without a vocabulary still names a source code by its source name"

    result = concepts_check(cfg, layout)
    assert not result.passed, "the checks must see the MEDS layer that lost its concepts"
    # The original build is untouched.
    assert shards(source)["omop_concept_id"].drop_nulls().to_list()


def test_an_injection_that_raises_is_not_injected(built, tmp_path: Path):
    cfg, source = built

    def breaks(layout, cfg):
        raise RuntimeError("the injection could not finish")

    broken = Fault("BROKEN_ON_PURPOSE", "meds", "a test", "silent", "raises", breaks)
    injection = inject(broken, WorkLayout(root=tmp_path, dataset_id=DATASET), cfg)
    assert not injection.injected
    assert injection.error == "RuntimeError: the injection could not finish"
    assert injection.effect.startswith("not injected")
