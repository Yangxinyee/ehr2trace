"""The dataset config is data, not code (checklist P0-2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr2trace.config import DatasetConfig, load_dataset_config
from ehr2trace.errors import BlockerError, ConfigError

MINIMAL = """
dataset_id: tiny
identity: {person_key: PID}
partitions:
  - {id: p1, dir: p1}
sources:
  labs:
    adapter: delimited
    file_glob: "*.tsv"
    shape: point_event
    event_kind: measurement
    fields:
      person_id: {from: [PID]}
      event_time: {from: [T]}
      source_code: {from: [C]}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "d.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_loads(tmp_path: Path):
    cfg = load_dataset_config(write(tmp_path, MINIMAL))
    assert cfg.dataset_id == "tiny"
    assert cfg.sources["labs"].fields["person_id"].from_ == ["PID"]


def test_unknown_key_raises_instead_of_being_ignored(tmp_path: Path):
    with pytest.raises(ConfigError) as exc:
        load_dataset_config(write(tmp_path, MINIMAL + "\nunexpected_key: 1\n"))
    assert "unexpected_key" in str(exc.value)


def test_unknown_nested_key_raises(tmp_path: Path):
    text = MINIMAL.replace("    shape: point_event", "    shape: point_event\n    typoed_option: 3")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_unknown_adapter_is_rejected(tmp_path: Path):
    text = MINIMAL.replace("adapter: delimited", "adapter: sqlalchemy")
    with pytest.raises(ConfigError) as exc:
        load_dataset_config(write(tmp_path, text))
    assert "unknown adapter" in str(exc.value)


def test_unknown_shape_is_rejected(tmp_path: Path):
    text = MINIMAL.replace("shape: point_event", "shape: run_arbitrary_python")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_unknown_field_role_is_rejected(tmp_path: Path):
    text = MINIMAL.replace("      person_id: {from: [PID]}", "      patientId: {from: [PID]}")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_config_cannot_construct_python_objects(tmp_path: Path):
    """A config that could import would be code review's problem forever after."""
    text = MINIMAL + "\ndescription: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_duplicate_partition_ids_rejected(tmp_path: Path):
    text = MINIMAL.replace("  - {id: p1, dir: p1}", "  - {id: p1, dir: p1}\n  - {id: p1, dir: p2}")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_source_restricted_to_unknown_partition_rejected(tmp_path: Path):
    text = MINIMAL.replace("    shape: point_event", "    shape: point_event\n    partitions: [nope]")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_config_hash_is_content_addressed(tmp_path: Path):
    a = load_dataset_config(write(tmp_path, MINIMAL))
    b = load_dataset_config(write(tmp_path, MINIMAL + "\n# a comment changes nothing\n"))
    c = load_dataset_config(write(tmp_path, MINIMAL.replace("bucket_count", "bucket_count")))
    assert a.config_hash() == b.config_hash() == c.config_hash()

    changed = load_dataset_config(write(tmp_path, MINIMAL.replace("dataset_id: tiny", "dataset_id: small")))
    assert changed.config_hash() != a.config_hash()


def test_missing_timezone_is_a_blocker_not_a_default(tmp_path: Path):
    cfg = load_dataset_config(write(tmp_path, MINIMAL))
    with pytest.raises(BlockerError) as exc:
        cfg.timezone_or_blocker()
    assert exc.value.blocker_id == "TIMEZONE_UNDECLARED"


def test_declared_timezone_is_returned(tmp_path: Path):
    cfg = load_dataset_config(write(tmp_path, MINIMAL + "\ntime: {timezone_assumption: America/New_York}\n"))
    assert cfg.timezone_or_blocker() == "America/New_York"


def test_birth_year_approximation_requires_an_approval_record(tmp_path: Path):
    text = MINIMAL + """
omop:
  person_birth_policy:
    mode: approved_approximation
"""
    with pytest.raises(ConfigError) as exc:
        load_dataset_config(write(tmp_path, text))
    assert "approval" in str(exc.value).lower()

    ok = load_dataset_config(
        write(
            tmp_path,
            text + "    age_as_of_date: '2024-01-01'\n    approval_note: 'approved by data owner 2024-01-05'\n",
        )
    )
    assert ok.omop.person_birth_policy.mode == "approved_approximation"


def test_a_source_may_not_claim_its_content_twice(tmp_path: Path):
    """`value` and `text` both claim the event's content, and no shape publishes both.

    Declaring both used to be accepted and one of them silently dropped, which is how
    MIMIC-IV published 2,652,887 notes with no text.
    """
    text = MINIMAL.replace("      source_code: {from: [C]}", "      source_code: {from: [C]}\n      value: {from: [V]}\n      text: {from: [X]}")
    with pytest.raises(ConfigError) as exc:
        load_dataset_config(write(tmp_path, text))
    assert "'value' or 'text'" in str(exc.value)


def test_any_of_requires_variants(tmp_path: Path):
    text = MINIMAL.replace("adapter: delimited", "adapter: any_of")
    with pytest.raises(ConfigError):
        load_dataset_config(write(tmp_path, text))


def test_ctpe_config_declares_every_partition_and_source(ctpe_config: DatasetConfig):
    assert [p.id for p in ctpe_config.partitions] == ["29_has", "29_no", "29b_has", "29b_no"]
    assert {p.membership_label for p in ctpe_config.partitions} == {"has", "no"}
    assert set(ctpe_config.sources) == {
        "all_rx",
        "medication_admin",
        "labs",
        "problem_list",
        "echo",
        "ekg",
        "demographics",
        "outcome",
        "pft_narrative",
        "pft_values",
    }


def test_ctpe_orders_and_administrations_are_separate_event_kinds(ctpe_config: DatasetConfig):
    """Prescribing and giving a drug are different facts and must stay different."""
    assert ctpe_config.sources["all_rx"].event_kind == "drug_order"
    assert ctpe_config.sources["medication_admin"].event_kind == "drug_admin"


def test_ctpe_label_is_episode_scoped_and_undefined(ctpe_config: DatasetConfig):
    label = ctpe_config.labels[0]
    assert label.scope == "episode"
    assert label.definition_status == "undefined"


def test_ctpe_anchor_is_not_wired_to_any_event_time(ctpe_config: DatasetConfig):
    """No source may map its anchor column onto a clinical time role."""
    for name, spec in ctpe_config.sources.items():
        anchor = spec.fields.get("anchor_time")
        if not anchor:
            continue
        for role in ("event_time", "available_time", "end_time"):
            other = spec.fields.get(role)
            if other:
                assert not (set(a.lower() for a in anchor.from_) & set(a.lower() for a in other.from_)), (
                    f"{name}.{role} reuses the anchor column"
                )


def test_a_row_filter_may_name_values_to_drop_instead_of_keep():
    from ehr2trace.config import RowFilterSpec
    assert RowFilterSpec.model_validate({"column": "ndc", "drop": ["0", ""]}).drop == ["0", ""]
    with pytest.raises(ValueError):
        RowFilterSpec.model_validate({"column": "ndc"})
