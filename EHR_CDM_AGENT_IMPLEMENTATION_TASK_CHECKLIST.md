# EHR → OMOP CDM / MEDS Converter: Implementation Checklist

> Document type: actionable checklist for a coding agent
> Version: 0.2 (matches design spec v0.5)
> Date: 2026-08-20
> Design spec: [PATIENT_CDM_AGENT_SYSTEM_DESIGN.md](./PATIENT_CDM_AGENT_SYSTEM_DESIGN.md)
> Reference dataset: `ctpe` (4 partitions under `$EHR_DATA_ROOT`)

---

## 0. How to use this

### 0.1 Status markers

- `[ ]` not done · `[x]` done with evidence · `[!]` blocked (state the blocker and who must supply what)

### 0.2 Minimum bar for ticking a box

- Code exists at the expected path with a matching test.
- **The failure path is tested too**, not just the happy path.
- The relevant command or test report is recorded in the task note.
- No PHI, `.env`, raw EHR samples, model weights, or MRN mapping table committed.
- Read-only raw data untouched.
- If semantics changed (identity keys, time priority, dedup rules, label definitions), `datasets/*.yaml` and the affected fixture expectations were updated in the same commit, with an explanation.

### 0.3 Global red lines

- [ ] The LLM never writes canonical / OMOP / MEDS / mappings, under any circumstance.
- [ ] The LLM never produces a concept ID from memory.
- [ ] `has`/`no` never becomes a diagnostic fact and never enters MEDS event rows.
- [ ] `dos` is never the `event_time` of any study.
- [ ] Anything uncertain goes to quarantine or review. Never guess.
- [ ] Same input + config + mapping version → same data hash.
- [ ] `--workers 1` and `--workers N` agree.
- [ ] Core code contains no CTPE paths, file names, sheet names, or column names.

### 0.4 Phases

| Phase | Goal | Depends on | Done when |
|---|---|---|---|
| P0 | Contracts and skeleton | — | `inspect` lists all inputs and reports blockers |
| P1 | source → canonical | P0 | three-patient fixtures pass + full reconciliation |
| P2 | OMOP 5.4 | P1 | every row traceable, unmapped items in review |
| P3 | MEDS | P1 | MEDS validator passes |
| P4 | LLM assistance | P1 | kept only if it measurably helps |
| P5 | Generalization and optional extensions | P1–P4 | synthetic fixture passes, no hardcoding |

P2 and P3 may run in parallel. P4 is independent and does not block a P2/P3 release.

---

## P0. Contracts and skeleton

### P0-1 Repository skeleton

- [ ] Build the directory layout from design §9.1.
- [ ] `pyproject.toml` + lockfile (Python 3.11+, dependencies per §9.1).
- [ ] **Pin the exact MEDS package version in the lockfile**; do not track `main`.
- [ ] `.env.example` with `EHR_DATA_ROOT` / `EHR_WORK_ROOT` / `OMOP_BACKEND` / `LLM_BASE_URL` / `OFFLINE_MODE`.
- [ ] `.gitignore` covering PHI, `.env`, `work/`, model weights, `*_mrn_map*`.
- [ ] pytest runs.

### P0-2 Configuration model

- [ ] `datasets/ctpe.yaml` declares all 4 partitions, all logical sources, and all field aliases per design §4.
- [ ] `config.py` loads and validates it with Pydantic v2.
- [ ] The YAML may only reference registered adapter names and **must not load arbitrary Python** (tested).
- [ ] Unknown keys raise instead of being silently ignored.
- [ ] `config_hash` is computed from the canonically serialized config content.

### P0-3 Stable IDs and canonical serialization ★

This is the easiest thing in the project to get wrong, and getting it wrong fails silently.

- [ ] `source_row_id = sha256(dataset_id|partition_id|source_id|file_sha256|row_number)`.
- [ ] `event_id` is computed from a canonical key that **excludes `dos`**.
- [ ] Implement cell → canonical string per the table in design §5.1.
- [ ] Row hash = canonical strings joined with `\x1f`, then sha256.
- [ ] **Test**: `29b_has`'s `Death_Date` (`str`) and `29_has`'s same value for the same patient (`datetime`) normalize to the same string.
- [ ] **Test**: `Outcome.Length_of_stay_days` normalizes identically in `29_no` (`str`) and the other partitions (`int`).
- [ ] **Test**: `float` `5.0` and `int` `5` normalize identically.
- [ ] **Test**: the literal `"NULL"`, an empty string, and `None` all normalize identically.
- [ ] `subject_id` maps stably from MRN to `int64`, scoped to the whole dataset; the mapping table lives outside the repository in a protected path.

### P0-4 Freeze the canonical schema

- [ ] Define Pydantic/Arrow schemas for `CanonicalEvent` + `EventSource` + `Anchor` + `CohortMembership` per design §5.2.
- [ ] Schema changes require editing this one place; other modules interact only through it.
- [ ] Write the Arrow representation and add a round-trip test.

### P0-5 The `inspect` command

- [ ] Walk all 4 partitions; emit a file list with sha256 and size.
- [ ] Recognize 21 txt sources + 19 xlsx sheets.
- [ ] Handle sheet aliases (`PFT`/`PFT Narrative`, `PFT Value`/`PFT Values`).
- [ ] Recognize `29b_no`'s empty leading column in PFT Narrative.
- [ ] Recognize that `29b_has` has no Medication Administration sheet and ships it as a standalone txt.
- [ ] Recognize the inconsistent BOM: 19 txt files carry one, the `29_has`/`29_no` problem_list files do not.
- [ ] Emit the blocker list (see P0-6).
- [ ] `--json` output is stable.

**Baseline assertions (written as tests):**

- [ ] 25 physical files.
- [ ] 40 logical sources (21 txt + 19 sheets).
- [ ] Partition sizes approximately 1.6 / 2.1 / 2.2 / 2.1 GB.
- [ ] Unique MRNs 5,633 / 8,623 / 8,247 / 8,468.
- [ ] Union 22,982.
- [ ] has∩no = 1,609; `29b_has`∩`29b_no` = 927; `29_has`∩`29_no` = 723; `29_has`∩`29b_no` = 392; `29_no`∩`29b_has` = 923; `29_no`∩`29b_no` = 747.
- [ ] `29_has` ⊆ `29b_has` (empty difference).
- [ ] When a baseline changes, **report drift and fail**; never auto-update the expectation.

### P0-6 Register external blockers

These must be answered by the data owner. They cannot be guessed, and the LLM must not fill them in.

- [ ] Source timezone (or an explicit "no timezone" assumption).
- [ ] The reference date for `Age`, or an explicit rejection of the approximate-birth-year policy.
- [ ] The rule and decision time behind `has`/`no`.
- [ ] How `has`/`no` binds to a specific CT episode.
- [ ] The relationship between batches `29` and `29b` (is `29b` a re-extraction?).
- [ ] Confirmation that CT/CTPA reports and images are genuinely not part of this delivery.
- [ ] A legitimate source and version for the Athena vocabulary.
- [ ] Unresolved items appear in `inspect`'s blocker output — **never as hidden assumptions**.

### P0-7 Three-patient fixtures

- [ ] Extract the three patients' raw rows from `29_has`, `29b_has`, and `29b_no`, preserving the original format (BOM, tabs, cell types).
- [ ] De-identify and place under `tests/fixtures/ctpe_three_patients/`.
- [ ] Add `tests/fixtures/ctpe_partition_overlap/` covering `PT-B`'s cross-partition has/no conflict.
- [ ] Keep fixtures small; **never copy the 8 GB of full data**.

### P0 exit criteria

- [ ] `ehr2cdm inspect --dataset ctpe` runs and passes every baseline assertion.
- [ ] Contracts (config model, canonical schema, hashing rules) are frozen.
- [ ] The blocker list has been sent to the data owner.

---

## P1. source → canonical (deterministic)

### P1-1 Three adapters

- [ ] `delimited`: stream line by line, never load a whole table into memory; tab-delimited.
- [ ] **Strip the BOM unconditionally**; assume neither presence nor absence (the `29` and `29b` problem_list files behave oppositely — tested).
- [ ] `excel`: openpyxl read-only mode; sheet aliases; skip unnamed leading columns; convert to Parquet first.
- [ ] `parquet`: direct read.
- [ ] `any_of`: one logical source with several physical forms (medication admin as txt or sheet).
- [ ] Missing required source → blocker; missing optional source → coverage `not_extracted`.
- [ ] **Test**: column reordering, column-name case changes, BOM presence, and missing optional columns do not change output semantics.
- [ ] **Test**: mixed cell types within one column are handled per value, never inferred from the first row.

### P1-2 Ingest and lineage

- [ ] Emit `source/<partition>/<source>.parquet` with the columns from design §5.1.
- [ ] Original columns keep their original values; typed columns use a `_parsed` suffix.
- [ ] Rows that fail to parse go to `quarantine/` with `parse_issues`.
- [ ] Generate an immutable input manifest (file hash, row count, schema).
- [ ] **Test**: `source_rows = parsed_rows + quarantined_rows` holds for every source.

### P1-3 Identity resolution ★

- [ ] Take the MRN union across all four partitions before assigning `subject_id`.
- [ ] **Never** assign subject IDs per partition.
- [ ] **Test**: the 5,633 MRNs shared by `29_has` and `29b_has` produce 5,633 subjects, not 11,266.
- [ ] **Test**: `PT-B` has exactly one `subject_id` across `29_has`/`29b_has`/`29b_no`.
- [ ] Record the set of partitions each subject appears in.

### P1-4 Time semantics

- [ ] Implement the per-source `event_time` / `available_time` priority from design §5.3.
- [ ] Flag the fallback when `Collection_time` is missing and `Result_Time` is used.
- [ ] Convert to UTC and record `timezone_assumption`.
- [ ] **With no timezone configured, raise a blocker; never default to the developer machine's timezone** (tested).
- [ ] Static demographics may have `event_time = null`; clinical events without a time go to quarantine.
- [ ] **Test**: no source's `event_time` ever equals that row's `dos`.

### P1-5 Anchor handling ★

- [ ] `dos` is written only into the `Anchor` table.
- [ ] Compare `29`'s full timestamps and `29b`'s dates at date granularity; `anchor_time_known` records whether the time component exists.
- [ ] `Anchor` dedup key = `(anchor_date, partition_id)`.
- [ ] `Closest_to_CT` stays a rank field and is **never treated as a day difference**; relative days are recomputed from timestamps.
- [ ] **Test**: `PT-B`'s anchor set has 4 members and matches `tools/baselines.local.json`.
- [ ] **Test**: `PT-B`'s echo base is 547 rows per anchor (`29_has` 2×547, `29b_has` 3×547, `29b_no` 1×547).

### P1-6 Value parsing

- [ ] Implement the 7 forms from design §5.4, one test case each: plain number, number + unit, range (`35-40`), comparator (`<0.5`), sentinel text (`see below`), free-text diagnosis (`SINUS TACHYCARDIA`), signature line (`Confirmed by ...`).
- [ ] No match → quarantine; **never coerce to a number**.
- [ ] `value_number` and `value_text` never carry the same meaning at once.
- [ ] Reference ranges come from configuration, not from the source (the source's `Reference_Range_*` are all NULL).

### P1-7 Deduplication

- [ ] The canonical dedup key **excludes `dos`**.
- [ ] The same clinical event across batches and partitions produces exactly one `CanonicalEvent`.
- [ ] All `EventSource` links are retained (including `duplicate_of`).
- [ ] All `CohortMembership` rows are retained, separate from events.
- [ ] **Test**: `PT-B`'s `CohortMembership` contains both `29b_has`(has) and `29b_no`(no), and they are **not** merged into a patient-level label.
- [ ] **Test**: `has`/`no` generates no condition or observation event.

### P1-8 Event type separation

- [ ] `all_rx` → `drug_order`, retaining `Order_Status`.
- [ ] medication admin → `drug_admin`, retaining dose and route.
- [ ] The two are never merged; `event_kind` and lineage distinguish them (tested).
- [ ] **Test**: rows with a NULL `Ordering_Date` go to quarantine and never produce a fabricated-date event.

### P1-9 Quality flags

- [ ] `RECORDED_AFTER_DEATH`: problem-list date later than the death date → flag, **never delete, never rewrite the date**.
- [ ] `AVAILABILITY_ASSUMED`, `COMPARATOR_VALUE`, `NON_NUMERIC_RESULT`, `SIGNATURE_LINE`, `TIME_FALLBACK` as needed.
- [ ] Empty sheet → coverage `unknown/not_extracted`, producing **no negative fact** (tested).
- [ ] All quality issues are written to `etl_audit.quality_issue`.

### P1-10 Execution and determinism ★

- [ ] Implement design §3.3: process pool + content-addressed output paths + `.partial` atomic writes + sorted merge.
- [ ] Subject bucketing `sha256(subject_id) % bucket_count`, bucket count in config (default 64).
- [ ] Outputs that exist with a matching hash are reused (this is resumption).
- [ ] **Test**: `--workers 1` and `--workers 4` produce the same canonical data hash.
- [ ] **Test**: killing a run midway and rerunning produces no duplicate events.
- [ ] **Test**: varying task completion order does not change the merge result.

### P1-11 Reconciliation and regression

- [ ] Emit coverage / fan-out / dedup / quarantine rates per source.
- [ ] **Test**: the three patients' raw row counts match the **partition-scoped** baselines in design §2.5
      (`29_has`: 5,727+379 / 4,243+206 / 377+22; `29b_has`: 6,095+11 / 6,162+1 / 398+1; `29b_no`: 2,529+206).
- [ ] **Test**: the three patients' canonical event counts after anchor deduplication are stable (freeze as expectations after the first run).
- [ ] **Test**: the three patients' anchor sets match P1-5.
- [ ] The full dataset completes; record wall time and peak memory.

### P1 exit criteria

- [ ] The three-patient and partition-overlap fixtures all pass.
- [ ] Determinism tests pass.
- [ ] The full reconciliation report has been generated and reviewed by a human.

---

## P2. OMOP CDM 5.4

### P2-1 Deployment and vocabulary

- [ ] Put the official 5.4 DDL in `sql/omop_5.4/` with its source URL and version recorded.
- [ ] DuckDB backend by default; PostgreSQL optional with the same DDL.
- [ ] Import the Athena vocabulary (`CONCEPT`, `CONCEPT_RELATIONSHIP`, `CONCEPT_ANCESTOR`, `VOCABULARY`, `DOMAIN`, `CONCEPT_CLASS`, `RELATIONSHIP`, `DRUG_STRENGTH`).
- [ ] Record the vocabulary version in the run report.

### P2-2 Terminology mapping

- [ ] Implement the deterministic flow from design §6.4: source string → source concept → `Maps to` → domain/validity check.
- [ ] **Dispatch by unique normalized string**, not by raw row.
- [ ] Lookup failure → lexical candidate recall → write to `review/pending.csv`.
- [ ] **No hardcoded concept IDs anywhere in code or prompts** (grep test).
- [ ] Fixed concepts are resolved from the vocabulary at startup with domain and standard status validated.

### P2-3 Mapping registry

- [ ] `mappings/<domain>.csv`: source_string, code_system, concept_id, mapping_version, decision provenance.
- [ ] A `compile` command turns `review/decisions.csv` into `mappings/`.
- [ ] Versioned by git.
- [ ] **Test**: a mapping absent from both `mappings/` and the vocabulary never reaches OMOP.

### P2-4 Table transforms

- [ ] Implement PERSON / OBSERVATION_PERIOD / VISIT_OCCURRENCE / CONDITION_OCCURRENCE / DRUG_EXPOSURE / PROCEDURE_OCCURRENCE / MEASUREMENT / NOTE / DEATH / CDM_SOURCE per design §6.2.
- [ ] `*_source_value` preserves the source value.
- [ ] No concept found → 0 plus review; **never invent an ID**.
- [ ] `OBSERVATION_PERIOD` uses an explicit heuristic with the rule version recorded.
- [ ] BMI/Pulse/BP_Systolic default to quarantine; **never fabricate a measurement date** (tested).
- [ ] No custom columns added to core tables.

### P2-5 Birth-year blocker

- [ ] Implement `strict` (default) and `approved_approximation`.
- [ ] Under `strict`, a missing birth year blocks that patient's OMOP publication and produces a report; canonical and MEDS are unaffected.
- [ ] `approved_approximation` requires both `age_as_of_date` and a human approval, and emits `DERIVED_APPROXIMATE_BIRTH_YEAR`.
- [ ] **Test**: it cannot be bypassed without approval.
- [ ] **Test**: `Death_Date`, the first event date, and the run date are never used as the age reference date.

### P2-6 Lineage and loading

- [ ] `etl_audit.lineage` covers every OMOP row.
- [ ] `etl_audit.anchor` / `cohort_membership` / `quality_issue` / `run` are in place.
- [ ] A single controlled load path (write partitioned staging, then load once); **never concurrent inserts**.
- [ ] **Test**: no OMOP row lacks lineage.

### P2-7 Validation

- [ ] No non-zero concept ID is absent from the vocabulary.
- [ ] Concept domains are compatible with their target fields.
- [ ] Foreign key / uniqueness / not-null constraints pass.
- [ ] The three patients' OMOP rows trace back to source rows (manual spot check plus automated test).
- [ ] (Optional) DataQualityDashboard, which needs R + PostgreSQL; not a release gate.

---

## P3. MEDS

### P3-1 Mapping

- [ ] Emit the required and extension columns from design §7.1.
- [ ] `available_time` is populated correctly; flag `AVAILABILITY_ASSUMED` when absent.
- [ ] Code namespace per §7.2 (`OMOP/<id>`, `SOURCE/<source>/<code>`, `MEDS_BIRTH`, `MEDS_DEATH`).
- [ ] **Test**: one clinical fact never yields both a SOURCE-code and an OMOP-code event.

### P3-2 Metadata

- [ ] `metadata/dataset.json` (MEDS package version, vocabulary version, config hash).
- [ ] `metadata/codes.parquet` contains **every code actually used** (test asserts set equality).
- [ ] `metadata/subject_splits.parquet`.

### P3-3 Sharding and splits

- [ ] One shard per patient; events contiguous and time-sorted within a shard (tested).
- [ ] Splits by stable per-patient hash into train/tuning/held_out, mutually exclusive.
- [ ] **Test**: splits are generated after cross-partition identity resolution; all events of one MRN share a split.

### P3-4 Leakage prevention ★

- [ ] `membership_label`, `partition_id`, and file names are **not written into MEDS event rows** by default (tested).
- [ ] `has`/`no` becomes a task label only when bound to an `anchor_id`, a definition rule, and a prediction time.
- [ ] **Test**: an as-of view contains no event with `available_time > prediction_time`.
- [ ] Scan for direct identifiers (MRN and similar) before export.

### P3-5 Validation

- [ ] Validate all shards and metadata with the installed MEDS schema validator (**mandatory**).
- [ ] Independently check Parquet schema, ordering, and the per-patient shard constraint.

---

## P4. LLM assistance

**Precondition**: P1 is complete, i.e. the data converts correctly with no LLM at all. This phase only adds assistance.

### P4-1 Client

- [ ] OpenAI-compatible HTTP client depending only on `LLM_BASE_URL` / `LLM_MODEL`.
- [ ] Probe capabilities once at startup (is JSON-schema output available?); **never assume from the model name**.
- [ ] temperature = 0 with a pinned sampling configuration.
- [ ] Validate output against a Pydantic schema; at most 2 retries, then review.
- [ ] Record template file hash, input hash, and token counts; **never log full patient text**.

### P4-2 The two uses

- [ ] **Column semantics proposal**: input column names + profile statistics + a few de-identified samples; output a suggested `logical_type` and field roles into `review/pending.csv`.
- [ ] **Terminology candidate ranking**: input a normalized source string + lexically recalled candidates; output a ranking with rationale into `review/pending.csv`.
- [ ] Neither may write to `mappings/` or any target layer (tested).
- [ ] Prompt templates live in `prompts/*.md`, versioned by git.

### P4-3 Review loop

- [ ] `propose` → `review/pending.csv`.
- [ ] A human edits `review/decisions.csv`.
- [ ] `compile` → `mappings/`.
- [ ] `transform` reruns.
- [ ] **Test**: undecided pending items never reach OMOP/MEDS.

### P4-4 Measure the benefit ★

- [ ] Build a small human gold set (a few dozen terminology mappings is enough).
- [ ] Compare human-acceptance rates across three configurations: deterministic lookup only / plus lexical recall / plus LLM ranking.
- [ ] Record JSON schema success rate, retry rate, and latency.
- [ ] **If the LLM shows no measurable gain, delete the second use in P4-2** and keep lexical recall alone.

---

## P5. Generalization and optional extensions

### P5-1 No hardcoding

- [ ] `tests/test_no_hardcoded_dataset_strings.py` greps core code and forbids `29_has_embolism`, `29b`, `dos`, `Closest_to_CT`, `MRN`, `Encounter_CSN`, `has_embolism`, PE ICD codes, and similar.
- [ ] Those strings may appear only in `datasets/*.yaml`, `tests/fixtures/`, and documentation.

### P5-2 Synthetic EHR fixture

- [ ] `tests/fixtures/generic_ehr/`: a minimal synthetic dataset structurally unlike CTPE (different column names, different file layout, CSV rather than TSV, no anchor concept).
- [ ] Write one new YAML and **change no core code**; run canonical → OMOP → MEDS end to end.
- [ ] This is the only evidence for the word "generalization". Without it, do not claim generalization in the documentation.

### P5-3 Optional: narrative extraction

- [ ] Runs only when explicitly enabled.
- [ ] Outputs candidate facts with evidence spans; spans must match the source **character for character** (tested).
- [ ] Single-patient isolation; **no cross-patient context**.
- [ ] Results are separate events with `provenance_status = llm_proposed`, becoming `human_approved` only after confirmation.
- [ ] **Never overwrite the NOTE original text.**
- [ ] Build a small gold set to evaluate assertion / experiencer / span accuracy.

### P5-4 Optional: task layer

- [ ] Cohort / prediction time / label / input window definitions live in a separate file.
- [ ] Reads published MEDS and **never writes back to clinical facts**.
- [ ] The CTPE PE label must be episode-level and bound to an `anchor_id`.
- [ ] Leakage test: the input view contains no partition, filename, or membership features.

---

## Definition of done

The overall delivery is complete when:

- [ ] All 4 partitions, 25 files, and 40 sources are processed with a complete reconciliation report.
- [ ] All 30 checks in design §10.2 pass.
- [ ] The three-patient and partition-overlap fixtures pass.
- [ ] Every OMOP row is traceable and the MEDS validator passes.
- [ ] `--workers 1` and `--workers N` agree; reruns are idempotent.
- [ ] The synthetic `generic_ehr` fixture passes using only a new YAML.
- [ ] Unresolved blockers are listed explicitly in the publication report, not hidden as assumptions.
- [ ] The README explains how to reproduce a full run from scratch.
- [ ] The repository contains no PHI.

---

## Appendix A. Task note template

```markdown
### <task id>: <title>
Status: [ ] / [x] / [!]
Changed: <file paths>
Command: <command to reproduce>
Tests: <test file :: case name, and result>
Failure path: <which failure cases were tested>
Evidence: <run_id / report path / key output>
Open items: <known limitations or blockers>
```

## Appendix B. Suggested execution order

```text
P0-1 → P0-2 → P0-3 → P0-4 → P0-5 → P0-7 → (send P0-6 out in parallel and wait for answers)
     → P1-1 → P1-2 → P1-3 → P1-4 → P1-5 → P1-6 → P1-7 → P1-8 → P1-9
     → P1-10 → P1-11
     → P2-* and P3-* in parallel
     → P4-* (keep or delete based on the measurement)
     → P5-1 → P5-2 → (P5-3 / P5-4 as needed)
```

P0-3 (canonical serialization) and P1-5 (anchor handling) are the two places most likely to be wrong without raising an error. Write their tests first.

## Appendix C. Differences from checklist v0.1

- Milestones collapsed from eleven (M0–M10) to six phases (P0–P5).
- Removed the entire M2 parallel execution engine (work ledger / leases / heartbeats / resource pools / fault injection / distributed backend), replaced by the four constraints in P1-10.
- Removed M9's capability levels and onboarding state machine, replaced by P0-5's blocker output and P5-2's synthetic fixture.
- Removed M10's containers, DB roles, secret management, and security scanning process.
- Removed the three-tier pack system with semver/CHANGELOG/conformance suite, replaced by a single YAML.
- Removed the five sub-agent roles and skill version management, replaced by P4-2's two uses.
- Removed the three-index RAG build-out tasks.
- **Correction**: v0.1's M3-010 treated the three-patient row counts as patient-level baselines when they are `29_has`-scoped values. P1-11 now asserts them per partition and adds the measured `29b_has` / `29b_no` numbers.
- **Added**: P0-3's canonical serialization tests (because of cross-workbook cell-type drift).
- **Added**: P1-5's `29`/`29b` anchor format difference and the 4-anchor assertion.
- **Added**: P1-6's seven value forms.
- **Added**: P1-7's cross-partition has/no conflict assertion for `PT-B`.
- **Added**: the inconsistent-BOM handling in P0-5 and P1-1.
- **Fixed**: the design-spec link now uses a same-directory relative path.
