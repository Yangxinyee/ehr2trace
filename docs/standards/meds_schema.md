# MEDS — schema reference

Derived from the installed `meds` package, version 0.4.1,
pinned in `requirements.lock`. Regenerate with `python3 tools/make_standards_reference.py`.

The installed package is the authority on what a valid MEDS dataset is; this file exists
so that authority can be grepped without importing anything.

## DataSchema

| column | type |
|---|---|
| `subject_id` | int64 |
| `time` | timestamp[us] |
| `code` | string |
| `numeric_value` | float |
| `text_value` | large_string |

## CodeMetadataSchema

| column | type |
|---|---|
| `code` | string |
| `description` | string |
| `parent_codes` | list<item: string> |

## SubjectSplitSchema

| column | type |
|---|---|
| `subject_id` | int64 |
| `split` | string |

## LabelSchema

| column | type |
|---|---|
| `subject_id` | int64 |
| `prediction_time` | timestamp[us] |
| `boolean_value` | bool |
| `integer_value` | int64 |
| `float_value` | float |
| `categorical_value` | string |

## Conventions

- data files live under `data/`
- code metadata at `metadata/codes.parquet`
- dataset metadata at `metadata/dataset.json`
- subject splits at `metadata/subject_splits.parquet`
- reserved codes: `MEDS_BIRTH`, `MEDS_DEATH`
- split names: `train`, `tuning`, `held_out`

This project adds extension columns beyond the required five; see `MEDS_SCHEMA` in
`src/ehr2cdm/meds.py`. `available_time` is the one that matters: an as-of view filters
on it, not on `time`.