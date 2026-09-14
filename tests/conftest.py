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
    from ehr2trace.config import load_dataset_config

    return load_dataset_config(repo_root / "datasets" / "ctpe.yaml")


@pytest.fixture(scope="session", autouse=True)
def isolated_mappings(tmp_path_factory: pytest.TempPathFactory):
    """Every test builds against an empty `mappings/` unless it writes one itself.

    Without this a build picks up whatever is in the developer's checkout, and the
    integration tests started failing the moment real human decisions were compiled
    into the repo: concepts from one hospital's vocabulary appearing in a fixture
    build, and fixture strings colliding with real approved mappings. What a test
    asserts must not depend on which mappings happen to be sitting beside it.

    Session-scoped because the builds it has to cover are: several integration tests
    build once per module, before any function-scoped fixture has run.
    """
    directory = tmp_path_factory.mktemp("mappings")
    previous = os.environ.get("EHR_MAPPINGS_DIR")
    os.environ["EHR_MAPPINGS_DIR"] = str(directory)
    yield directory
    if previous is None:
        os.environ.pop("EHR_MAPPINGS_DIR", None)
    else:
        os.environ["EHR_MAPPINGS_DIR"] = previous


@pytest.fixture()
def work_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("EHR_WORK_ROOT", str(root))
    return root
