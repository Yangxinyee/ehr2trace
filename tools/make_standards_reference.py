# -*- coding: utf-8 -*-
"""Regenerate the grep-able standards references under docs/standards/.

Design section 8.3 says specification lookups should be ripgrep over local documents
rather than a retrieval index. This derives those documents from the two authorities
this project already pins — the OMOP DDL and the installed MEDS package — so they
cannot drift from what the code actually builds against.

    python3 tools/make_standards_reference.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "standards"


def omop_reference() -> str:
    ddl = (ROOT / "sql" / "omop_5.4" / "OMOPCDM_duckdb_5.4_ddl.sql").read_text(encoding="utf-8")
    lines = [
        "# OMOP CDM 5.4 — field reference",
        "",
        "Derived from the pinned DDL in `sql/omop_5.4/` (see `SOURCE.md` for the commit).",
        "Regenerate with `python3 tools/make_standards_reference.py`. Never hand-edited: if this",
        "disagrees with the DDL, the DDL is right.",
        "",
        "`NOT NULL` matters more than it looks. Half the decisions in `src/ehr2trace/omop.py` are",
        "about what to do when the source cannot supply a required field, and the answer is never",
        "to invent one.",
        "",
    ]
    for match in re.finditer(r"CREATE TABLE @cdmDatabaseSchema\.(\w+)\s*\((.*?)\);", ddl, re.S):
        table, body = match.group(1), match.group(2)
        lines += [f"## {table}", "", "| column | type | null |", "|---|---|---|"]
        for raw in body.strip().splitlines():
            raw = raw.strip().rstrip(",")
            if not raw:
                continue
            parts = raw.split()
            nullable = "NOT NULL" not in raw.upper()
            dtype = " ".join(p for p in parts[1:] if p.upper() not in {"NOT", "NULL"})
            lines.append(f"| `{parts[0]}` | {dtype} | {'yes' if nullable else '**no**'} |")
        lines.append("")
    return "\n".join(lines)


def meds_reference() -> str:
    import meds

    lines = [
        "# MEDS — schema reference",
        "",
        f"Derived from the installed `meds` package, version {getattr(meds, '__version__', '0.4.1')},",
        "pinned in `requirements.lock`. Regenerate with `python3 tools/make_standards_reference.py`.",
        "",
        "The installed package is the authority on what a valid MEDS dataset is; this file exists",
        "so that authority can be grepped without importing anything.",
        "",
    ]
    for name in ("DataSchema", "CodeMetadataSchema", "SubjectSplitSchema", "LabelSchema"):
        schema = getattr(meds, name, None)
        if schema is None:
            continue
        lines += [f"## {name}", "", "| column | type |", "|---|---|"]
        for field in schema.schema():
            lines.append(f"| `{field.name}` | {field.type} |")
        lines.append("")
    lines += [
        "## Conventions",
        "",
        f"- data files live under `{meds.data_subdirectory}/`",
        f"- code metadata at `{meds.code_metadata_filepath}`",
        f"- dataset metadata at `{meds.dataset_metadata_filepath}`",
        f"- subject splits at `{meds.subject_splits_filepath}`",
        f"- reserved codes: `{meds.birth_code}`, `{meds.death_code}`",
        f"- split names: `{meds.train_split}`, `{meds.tuning_split}`, `{meds.held_out_split}`",
        "",
        "This project adds extension columns beyond the required five; see `MEDS_SCHEMA` in",
        "`src/ehr2trace/meds.py`. `available_time` is the one that matters: an as-of view filters",
        "on it, not on `time`.",
    ]
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "omop_cdm_5.4_fields.md").write_text(omop_reference(), encoding="utf-8")
    (OUT / "meds_schema.md").write_text(meds_reference(), encoding="utf-8")
    print(f"wrote {OUT / 'omop_cdm_5.4_fields.md'}")
    print(f"wrote {OUT / 'meds_schema.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
