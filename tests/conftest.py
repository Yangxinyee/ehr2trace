from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def data_root() -> Path:
    """The real read-only export. Tests needing it are marked ``realdata``."""
    raw = os.environ.get("EHR_DATA_ROOT")
    if not raw or not Path(raw).is_dir():
        pytest.skip("EHR_DATA_ROOT is not set to the real export")
    return Path(raw)


@pytest.fixture(scope="session")
def ctpe_config(repo_root: Path):
    from ehr2cdm.config import load_dataset_config

    return load_dataset_config(repo_root / "datasets" / "ctpe.yaml")


@pytest.fixture()
def work_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("EHR_WORK_ROOT", str(root))
    return root
