"""What a preparation step read, what it left beside its inputs, and whether it hashed them.

Two of the audit's datasets reach ingest through a preparation step, which reads the raw
delivery and writes parquet. Everything the step did not read was invisible afterwards:
a whole intensive-care module and eight tables of one export never reached a file any
check could see (P-M4, P-M18), and another step recorded every input's hash as null
(P-CU11). These tests build a tiny delivery and a preparation manifest and hold
RAW_COVERAGE_DECLARED and INPUT_MANIFEST_COMPLETE to both.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout
from ehr2trace.validate import CHECKS, Layers, preparation_inputs, raw_coverage

ROOT_ENV = "EHR2TRACE_TEST_PREPARED_ROOT"


def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, out_of_scope: dict | None = None,
             inputs: object = None) -> tuple[DatasetConfig, Path]:
    raw = tmp_path / "raw"
    (raw / "hosp").mkdir(parents=True)
    (raw / "icu").mkdir()
    for name in ("hosp/admissions.csv", "hosp/drgcodes.csv", "hosp/123456789_extract.csv", "icu/chartevents.csv"):
        (raw / name).write_text("a,b\n", encoding="utf-8")
    prepared = tmp_path / "prepared"
    (prepared / "p1").mkdir(parents=True)
    pl.DataFrame({"PID": ["x"], "T": ["2020-01-01"], "K": ["k"]}).write_parquet(prepared / "p1" / "admissions.parquet")
    if inputs is None:
        inputs = [{"path": str(raw / "hosp" / "admissions.csv"), "sha256": "ab" * 32}]
    (prepared / "p1" / "prepare_manifest.json").write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV, str(prepared))
    cfg = DatasetConfig.model_validate({
        "dataset_id": "prep_check",
        "root_env": ROOT_ENV,
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "time": {"timezone_assumption": "UTC"},
        "sources": {"admissions": {
            "adapter": "parquet", "file_glob": "admissions.parquet", "shape": "point_event", "event_kind": "measurement",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]}, "source_code": {"from": ["K"]}},
        }},
        "out_of_scope": out_of_scope or {},
    })
    return cfg, raw


def test_preparation_inputs_are_read_in_every_shape_a_step_writes():
    assert preparation_inputs({"inputs": [{"path": "/d/a.csv", "sha256": "h"}]}) == [("/d/a.csv", "h")]
    assert preparation_inputs({"inputs": {"/d/a.xlsx": {"sha256": "h", "bytes": 3}}}) == [("/d/a.xlsx", "h")]
    assert preparation_inputs({"inputs": {"T6_notes.csv": None, "T1.csv": "h"}}) == [("T6_notes.csv", None), ("T1.csv", "h")]
    assert preparation_inputs({}) == []


def test_tables_beside_what_a_preparation_step_read_are_reported_unless_declared(tmp_path, monkeypatch):
    cfg, _raw = delivery(tmp_path, monkeypatch)
    report = raw_coverage(cfg, {"inputs": []})["prepare_manifest"]
    unread = report["unread_beside_inputs"]

    # The table beside the one it read, and the whole directory beside the one it read from.
    assert "hosp/drgcodes.csv" in unread and "icu/chartevents.csv" in unread
    assert "hosp/admissions.csv" not in unread
    # A name with a record-number-shaped run of digits is hashed, never carried.
    assert not any("123456789" in item for item in unread)
    assert report["unread_beside_inputs_count"] == 3

    declared, _raw = delivery(tmp_path / "declared", monkeypatch, out_of_scope={
        "icu_module": {"matches": ["icu/*"], "reason": "not converted yet"},
        "billing": {"matches": ["hosp/drgcodes*", "hosp/*_extract.csv"], "reason": "billing codes are not clinical facts"},
    })
    report = raw_coverage(declared, {"inputs": []})["prepare_manifest"]
    assert report["unread_beside_inputs_count"] == 0 and report["declared_beside_inputs"] == 3


def test_a_step_that_lists_what_it_left_is_held_to_its_list(tmp_path, monkeypatch):
    cfg, raw = delivery(tmp_path, monkeypatch)
    manifest = cfg.source_root(cfg.sources["admissions"]) / "p1" / "prepare_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unread_inputs"] = [{"path": "icu/chartevents.csv", "reason": "later"}]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = raw_coverage(cfg, {"inputs": []})["prepare_manifest"]
    assert report["undeclared_unread_inputs"] == ["icu/chartevents.csv"]
    assert report["unread_beside_inputs_count"] == 0, "the list replaces the walk"


def test_a_preparation_input_recorded_without_a_hash_fails_the_manifest_check(tmp_path, monkeypatch):
    cfg, _raw = delivery(tmp_path, monkeypatch, inputs={"T6_notes.csv": None, "T1_people.csv": "ab" * 32})
    layout = WorkLayout(root=tmp_path / "work" / cfg.dataset_id, dataset_id=cfg.dataset_id).ensure()
    (layout.manifest_dir / "inputs.json").write_text(json.dumps({"inputs": [
        {"source_id": "admissions", "partition_id": "p1", "file_path": "admissions.parquet", "file_sha256": "cd" * 32,
         "rows_parsed": 1, "rows_read": 1, "columns": ["PID", "T", "K"]},
    ]}), encoding="utf-8")
    check = dict(CHECKS)["INPUT_MANIFEST_COMPLETE"][0]

    result = check(Layers.load(cfg, layout))
    assert not result.passed
    assert "1 of 2 inputs a preparation step read" in result.detail
    assert result.metrics["preparation_inputs_without_hash"] == 1


def test_the_manifests_found_are_named_and_none_found_is_a_stated_skip(tmp_path, monkeypatch):
    """A preparation manifest can sit at a root or inside a partition directory; either way
    the report says where it was found, so a manifest that is missing is visible too."""
    cfg, _raw = delivery(tmp_path, monkeypatch)
    report = raw_coverage(cfg, {"inputs": []})["prepare_manifest"]
    assert [Path(f["manifest"]).relative_to(tmp_path).as_posix() for f in report["found"]] == ["prepared/p1/prepare_manifest.json"]
    assert report["found"][0]["unread_inputs_listed"] is None

    (cfg.data_root() / "p1" / "prepare_manifest.json").unlink()
    report = raw_coverage(cfg, {"inputs": []})["prepare_manifest"]
    assert report["manifests"] == 0 and report["found"] == []
    assert "no prepare_manifest.json" in report["skipped"]


def test_a_pattern_with_a_slash_matches_any_trailing_sub_path_and_one_without_only_the_base_name():
    """The shapes a real preparation step leaves: image volumes several directories deep,
    an editor's settings and resource forks in nested directories, scripts at the top."""
    from ehr2trace.validate import out_of_scope_matcher

    cfg = DatasetConfig.model_validate({
        "dataset_id": "matcher",
        "identity": {"person_key": "PID"},
        "partitions": [{"id": "p1", "dir": "p1"}],
        "sources": {"admissions": {
            "adapter": "parquet", "file_glob": "admissions.parquet", "shape": "point_event", "event_kind": "measurement",
            "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]}, "source_code": {"from": ["K"]}},
        }},
        "out_of_scope": {
            "editor": {"matches": [".idea/*"], "reason": "an editor's project settings"},
            "forks": {"matches": ["__MACOSX/*"], "reason": "resource forks an archiver wrote"},
            "volumes": {"matches": ["volumes_nii/*"], "reason": "image volumes are not events"},
            "scripts": {"matches": ["*.py"], "reason": "the sender's own helper scripts"},
            "loose": {"matches": ["archive*"], "reason": "a base-name pattern"},
            "sheets": {"matches": ["book.xlsx::old_*"], "reason": "another cohort's sheets"},
        },
    })
    declared = out_of_scope_matcher(cfg)
    top = "/mnt/delivery/Images"

    assert declared(f"{top}/project/.idea/workspace.xml")
    assert declared(f"{top}/a/b/__MACOSX/c/._scan.nii.gz")
    assert declared(f"{top}/volumes_nii/2016/batch_3/series_7/scan.nii.gz")
    assert declared(f"{top}/0_remove_series.py")
    assert declared(f"{top}/tools/deep/nested/helper.py"), "a base name matches at any depth"
    assert declared(f"{top}/archive_2019.zip")

    assert not declared(f"{top}/notes/.idea_backup/workspace.xml")
    assert not declared(f"{top}/volumes_nii_old/scan.nii.gz"), "a slash pattern names that exact directory"
    assert not declared(f"{top}/archive/scan.nii.gz"), "a pattern without a slash never matches a directory"
    assert not declared(f"{top}/helper.py.bak")

    assert declared(f"{top}/book.xlsx", "old_cohort")
    assert not declared(f"{top}/book.xlsx", "this_cohort")


def test_a_relative_unread_path_is_matched_from_the_root_the_step_read(tmp_path, monkeypatch):
    """A step lists what it left relative to the tree it read; the declaration names that tree."""
    cfg, raw = delivery(tmp_path, monkeypatch, out_of_scope={
        "icu_module": {"matches": ["raw/icu/*"], "reason": "the delivery's intensive-care module, not converted yet"},
    })
    manifest = cfg.data_root() / "p1" / "prepare_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unread_inputs"] = [{"path": "icu/chartevents.csv", "reason": "later"}]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    # Without saying which tree it read, the path cannot be placed, and nothing matches it.
    assert raw_coverage(cfg, {"inputs": []})["prepare_manifest"]["undeclared_unread_inputs"] == ["icu/chartevents.csv"]

    payload["delivery_root"] = str(raw)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = raw_coverage(cfg, {"inputs": []})["prepare_manifest"]
    assert report["undeclared_unread_inputs"] == [] and report["unread_inputs_listed"] == 1
