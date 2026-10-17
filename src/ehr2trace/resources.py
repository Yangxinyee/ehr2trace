"""Where the data files that ship with the converter live.

The reference tables, the OMOP DDL, the model prompts and the dataset configurations sit
at the repository root, beside ``src/``. A wheel cannot carry files from there, so the
build copies them into the package as ``ehr2trace/_data/<name>`` (see ``pyproject.toml``).
An installed package reads that copy; a source checkout, editable or not, has no
``_data`` and reads the repository root, so editing a file there takes effect at once.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _PACKAGE_DIR.parents[1]


def resource_dir(name: str) -> Path:
    """The directory of one shipped resource: ``reference``, ``sql``, ``prompts`` or ``datasets``."""
    packaged = _PACKAGE_DIR / "_data" / name
    return packaged if packaged.is_dir() else _REPOSITORY_ROOT / name
