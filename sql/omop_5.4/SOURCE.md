# OMOP CDM 5.4 DDL provenance

Downloaded verbatim from the OHDSI CommonDataModel repository and never edited. The
core table structure is not this project's to rewrite; anything this converter needs to
say beyond it goes in the `etl_audit` schema.

| Field | Value |
|---|---|
| Repository | https://github.com/OHDSI/CommonDataModel |
| Commit | `0dd2175a77db0ae2e087e9138db193e60704a1e7` |
| Path | `inst/ddl/5.4/duckdb/` |
| Retrieved | 2026-08-20 |
| CDM version | 5.4 |

Pinned by commit rather than by tag: the DuckDB dialect files are not present in the
v5.4.0 / v5.4.1 release tags, only on the default branch, so a tag would not be
reproducible.

## Hashes

```
320310b92865d3ba9b6bdfadcb32f74890f25dbc452f51e5cfb7764552269979  OMOPCDM_duckdb_5.4_constraints.sql
40257d6a4fbb34adb080539f1aec323be24942df57937714f804f4219883920b  OMOPCDM_duckdb_5.4_ddl.sql
0e95faeb7c73f48ca5b4054c25cd9c8f539910c6265268fd680f71f975f91b24  OMOPCDM_duckdb_5.4_primary_keys.sql
```
