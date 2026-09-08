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

- [x] The LLM never writes canonical / OMOP / MEDS / mappings, under any circumstance.
- [x] The LLM never produces a concept ID from memory.
- [x] `has`/`no` never becomes a diagnostic fact and never enters MEDS event rows.
- [x] `dos` is never the `event_time` of any study.
- [x] Anything uncertain goes to quarantine or review. Never guess.
- [x] Same input + config + mapping version → same data hash.
- [x] `--workers 1` and `--workers N` agree.
- [x] Core code contains no CTPE paths, file names, sheet names, or column names.

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

- [x] Build the directory layout from design §9.1.
- [x] `pyproject.toml` + lockfile (Python 3.11+, dependencies per §9.1).
- [x] **Pin the exact MEDS package version in the lockfile**; do not track `main`.
- [x] `.env.example` with `EHR_DATA_ROOT` / `EHR_WORK_ROOT` / `OMOP_BACKEND` / `LLM_BASE_URL` / `OFFLINE_MODE`.
- [x] `.gitignore` covering PHI, `.env`, `work/`, model weights, `*_mrn_map*`.
- [x] pytest runs.

### P0-2 Configuration model

- [x] `datasets/ctpe.yaml` declares all 4 partitions, all logical sources, and all field aliases per design §4.
- [x] `config.py` loads and validates it with Pydantic v2.
- [x] The YAML may only reference registered adapter names and **must not load arbitrary Python** (tested).
- [x] Unknown keys raise instead of being silently ignored.
- [x] `config_hash` is computed from the canonically serialized config content.

### P0-3 Stable IDs and canonical serialization ★

This is the easiest thing in the project to get wrong, and getting it wrong fails silently.

- [x] `source_row_id = sha256(dataset_id|partition_id|source_id|file_sha256|row_number)`.
- [x] `event_id` is computed from a canonical key that **excludes `dos`**.
- [x] Implement cell → canonical string per the table in design §5.1.
- [x] Row hash = canonical strings joined with `\x1f`, then sha256.
- [x] **Test**: `29b_has`'s `Death_Date` (`str`) and `29_has`'s same value for the same patient (`datetime`) normalize to the same string.
- [x] **Test**: `Outcome.Length_of_stay_days` normalizes identically in `29_no` (`str`) and the other partitions (`int`).
- [x] **Test**: `float` `5.0` and `int` `5` normalize identically.
- [x] **Test**: the literal `"NULL"`, an empty string, and `None` all normalize identically.
- [x] `subject_id` maps stably from MRN to `int64`, scoped to the whole dataset; the mapping table lives outside the repository in a protected path.

### P0-4 Freeze the canonical schema

- [x] Define Pydantic/Arrow schemas for `CanonicalEvent` + `EventSource` + `Anchor` + `CohortMembership` per design §5.2.
- [x] Schema changes require editing this one place; other modules interact only through it.
- [x] Write the Arrow representation and add a round-trip test.

### P0-5 The `inspect` command

- [x] Walk all 4 partitions; emit a file list with sha256 and size.
- [x] Recognize 21 txt sources + 19 xlsx sheets.
- [x] Handle sheet aliases (`PFT`/`PFT Narrative`, `PFT Value`/`PFT Values`).
- [x] Recognize `29b_no`'s empty leading column in PFT Narrative.
- [x] Recognize that `29b_has` has no Medication Administration sheet and ships it as a standalone txt.
- [x] Recognize the inconsistent BOM: 19 txt files carry one, the `29_has`/`29_no` problem_list files do not.
- [x] Emit the blocker list (see P0-6).
- [x] `--json` output is stable.

**Baseline assertions (written as tests):**

- [x] 25 physical files.
- [x] 40 logical sources (21 txt + 19 sheets).
- [x] Partition sizes approximately 1.6 / 2.1 / 2.2 / 2.1 GB.
- [x] Unique MRNs 5,633 / 8,623 / 8,247 / 8,468.
- [x] Union 22,982.
- [x] has∩no = 1,609; `29b_has`∩`29b_no` = 927; `29_has`∩`29_no` = 723; `29_has`∩`29b_no` = 392; `29_no`∩`29b_has` = 923; `29_no`∩`29b_no` = 747.
- [x] `29_has` ⊆ `29b_has` (empty difference).
- [x] When a baseline changes, **report drift and fail**; never auto-update the expectation.

### P0-6 Register external blockers

These must be answered by the data owner. They cannot be guessed, and the LLM must not fill them in.

- [!] Source timezone (or an explicit "no timezone" assumption).
      - **[!] blocked**: `TIMEZONE_UNDECLARED`. Runs use `--assume-timezone`, recorded in the run report and flagged `TZ_ASSUMED` on every event.
- [!] The reference date for `Age`, or an explicit rejection of the approximate-birth-year policy.
      - **[!] blocked**: `AGE_REFERENCE_DATE_MISSING`. Under the default strict policy every patient is withheld from OMOP; canonical and MEDS are unaffected.
- [!] The rule and decision time behind `has`/`no`.
      - **[!] blocked**: `LABEL_DEFINITION_UNDEFINED`. The label stays provenance in `CohortMembership` with `label_scope: episode`.
- [!] How `has`/`no` binds to a specific CT episode.
      - **[!] blocked**: `LABEL_EPISODE_BINDING`. 1,609 subjects carry both labels; both rows are kept, unmerged.
- [!] The relationship between batches `29` and `29b` (is `29b` a re-extraction?).
      - **[!] blocked**: `BATCH_RELATIONSHIP_UNKNOWN`. Batches are merged by lineage, which assumes independent extracts.
- [!] Confirmation that CT/CTPA reports and images are genuinely not part of this delivery.
      - **[!] blocked**: `IMAGING_REPORTS_UNCONFIRMED`. No imaging conclusion is synthesized from an anchor date or a directory name.
- [!] A legitimate source and version for the Athena vocabulary.
      - **[!] blocked**: `VOCABULARY_SOURCE_MISSING`. Every `*_concept_id` is 0, every source value preserved, every distinct term queued for review.
- [x] Unresolved items appear in `inspect`'s blocker output — **never as hidden assumptions**.

### P0-7 Three-patient fixtures

- [!] Extract the three patients' raw rows from `29_has`, `29b_has`, and `29b_no`, preserving the original format (BOM, tabs, cell types).
      - **[!] deliberately not done.** De-identifying real narrative text well enough to commit is a research problem in itself, and a half-done job here puts real patients in a git history that cannot be rewritten away. See the note below.
- [!] De-identify and place under `tests/fixtures/ctpe_three_patients/`.
      - **[!] replaced** by `tests/fixtures/ctpe_shape/` — fabricated patients with the same structural traps — plus the real-data assertions in `tests/integration/test_ctpe_baselines.py`, which read the export in place and commit nothing.
- [!] Add `tests/fixtures/ctpe_partition_overlap/` covering `PT-B`'s cross-partition has/no conflict.
      - **[x]** covered by `ctpe_shape`'s `SUBJ-2`, who appears in both a has and a no partition of one batch, and asserted on the real patient in `test_ctpe_baselines.py`.
- [x] Keep fixtures small; **never copy the 8 GB of full data**.

> **On the three-patient fixtures.** The checklist asks for de-identified copies of three
> real patients' rows. That was not done, and the substitution is deliberate rather than a
> shortcut. De-identifying free-text echo and PFT narratives well enough to commit is a
> research problem on its own, and a git history cannot be un-published. What the fixtures
> were for — a committable regression against the export's structural traps — is served by
> `tests/fixtures/ctpe_shape/`, which reproduces every trap with fabricated patients and
> fabricated text. The three real patients are still asserted, against the real export in
> place, by `tests/integration/test_ctpe_baselines.py`, which reads their identifiers from
> the git-ignored `tools/baselines.local.json` and commits nothing.

### P0 exit criteria

- [x] `ehr2trace inspect --dataset ctpe` runs and passes every baseline assertion.
- [x] Contracts (config model, canonical schema, hashing rules) are frozen.
- [!] The blocker list has been sent to the data owner.
      - **[!] blocked**: a human action. `ehr2trace inspect` prints the list and exits non-zero while any remain open.

---

## P1. source → canonical (deterministic)

### P1-1 Three adapters

- [x] `delimited`: stream line by line, never load a whole table into memory; tab-delimited.
- [x] **Strip the BOM unconditionally**; assume neither presence nor absence (the `29` and `29b` problem_list files behave oppositely — tested).
- [x] `excel`: openpyxl read-only mode; sheet aliases; skip unnamed leading columns; convert to Parquet first.
- [x] `parquet`: direct read.
- [x] `any_of`: one logical source with several physical forms (medication admin as txt or sheet).
- [x] Missing required source → blocker; missing optional source → coverage `not_extracted`.
- [x] **Test**: column reordering, column-name case changes, BOM presence, and missing optional columns do not change output semantics.
- [x] **Test**: mixed cell types within one column are handled per value, never inferred from the first row.

### P1-2 Ingest and lineage

- [x] Emit `source/<partition>/<source>.parquet` with the columns from design §5.1.
- [x] Original columns keep their original values; typed columns use a `_parsed` suffix.
- [x] Rows that fail to parse go to `quarantine/` with `parse_issues`.
- [x] Generate an immutable input manifest (file hash, row count, schema).
- [x] **Test**: `source_rows = parsed_rows + quarantined_rows` holds for every source.

### P1-3 Identity resolution ★

- [x] Take the MRN union across all four partitions before assigning `subject_id`.
- [x] **Never** assign subject IDs per partition.
- [x] **Test**: the 5,633 MRNs shared by `29_has` and `29b_has` produce 5,633 subjects, not 11,266.
- [x] **Test**: `PT-B` has exactly one `subject_id` across `29_has`/`29b_has`/`29b_no`.
- [x] Record the set of partitions each subject appears in.

### P1-4 Time semantics

- [x] Implement the per-source `event_time` / `available_time` priority from design §5.3.
- [x] Flag the fallback when `Collection_time` is missing and `Result_Time` is used.
- [x] Convert to UTC and record `timezone_assumption`.
- [x] **With no timezone configured, raise a blocker; never default to the developer machine's timezone** (tested).
- [x] Static demographics may have `event_time = null`; clinical events without a time go to quarantine.
- [x] **Test**: no source's `event_time` ever equals that row's `dos`.

### P1-5 Anchor handling ★

- [x] `dos` is written only into the `Anchor` table.
- [x] Compare `29`'s full timestamps and `29b`'s dates at date granularity; `anchor_time_known` records whether the time component exists.
- [x] `Anchor` dedup key = `(anchor_date, partition_id)`.
- [x] `Closest_to_CT` stays a rank field and is **never treated as a day difference**; relative days are recomputed from timestamps.
- [x] **Test**: `PT-B`'s anchor set has 4 members and matches `tools/baselines.local.json`.
- [x] **Test**: `PT-B`'s echo base is 547 rows per anchor (`29_has` 2×547, `29b_has` 3×547, `29b_no` 1×547).

### P1-6 Value parsing

- [x] Implement the 7 forms from design §5.4, one test case each: plain number, number + unit, range (`35-40`), comparator (`<0.5`), sentinel text (`see below`), free-text diagnosis (`SINUS TACHYCARDIA`), signature line (`Confirmed by ...`).
- [x] No match → quarantine; **never coerce to a number**.
- [x] `value_number` and `value_text` never carry the same meaning at once.
- [x] Reference ranges come from configuration, not from the source (the source's `Reference_Range_*` are all NULL).

### P1-7 Deduplication

- [x] The canonical dedup key **excludes `dos`**.
- [x] The same clinical event across batches and partitions produces exactly one `CanonicalEvent`.
- [x] All `EventSource` links are retained (including `duplicate_of`).
- [x] All `CohortMembership` rows are retained, separate from events.
- [x] **Test**: `PT-B`'s `CohortMembership` contains both `29b_has`(has) and `29b_no`(no), and they are **not** merged into a patient-level label.
- [x] **Test**: `has`/`no` generates no condition or observation event.

### P1-8 Event type separation

- [x] `all_rx` → `drug_order`, retaining `Order_Status`.
- [x] medication admin → `drug_admin`, retaining dose and route.
- [x] The two are never merged; `event_kind` and lineage distinguish them (tested).
- [x] **Test**: rows with a NULL `Ordering_Date` go to quarantine and never produce a fabricated-date event.

### P1-9 Quality flags

- [x] `RECORDED_AFTER_DEATH`: problem-list date later than the death date → flag, **never delete, never rewrite the date**.
- [x] `AVAILABILITY_ASSUMED`, `COMPARATOR_VALUE`, `NON_NUMERIC_RESULT`, `SIGNATURE_LINE`, `TIME_FALLBACK` as needed.
- [x] Empty sheet → coverage `unknown/not_extracted`, producing **no negative fact** (tested).
- [x] All quality issues are written to `etl_audit.quality_issue`.

### P1-10 Execution and determinism ★

- [x] Implement design §3.3: process pool + content-addressed output paths + `.partial` atomic writes + sorted merge.
- [x] Subject bucketing `sha256(subject_id) % bucket_count`, bucket count in config (default 64).
- [x] Outputs that exist with a matching hash are reused (this is resumption).
- [x] **Test**: `--workers 1` and `--workers 4` produce the same canonical data hash.
- [x] **Test**: killing a run midway and rerunning produces no duplicate events.
- [x] **Test**: varying task completion order does not change the merge result.

### P1-11 Reconciliation and regression

- [x] Emit coverage / fan-out / dedup / quarantine rates per source.
- [x] **Test**: the three patients' raw row counts match the **partition-scoped** baselines in design §2.5
      (`29_has`: 5,727+379 / 4,243+206 / 377+22; `29b_has`: 6,095+11 / 6,162+1 / 398+1; `29b_no`: 2,529+206).
- [x] **Test**: the three patients' canonical event counts after anchor deduplication are stable (freeze as expectations after the first run).
- [x] **Test**: the three patients' anchor sets match P1-5.
- [x] The full dataset completes; record wall time and peak memory.

### P1 exit criteria

- [x] The three-patient and partition-overlap fixtures all pass.
- [x] Determinism tests pass.
- [!] The full reconciliation report has been generated and reviewed by a human.
      - Generated: `work/ctpe/runs/*/report.json` and `work/ctpe/runs/validation.json`, with
        wall time and peak memory per stage. **The human review is outstanding** — it is the
        one part of this line nobody but the data owner can do.

---

## P2. OMOP CDM 5.4

### P2-1 Deployment and vocabulary

- [x] Put the official 5.4 DDL in `sql/omop_5.4/` with its source URL and version recorded.
- [x] DuckDB backend by default; PostgreSQL optional with the same DDL.
- [!] Import the Athena vocabulary (`CONCEPT`, `CONCEPT_RELATIONSHIP`, `CONCEPT_ANCESTOR`, `VOCABULARY`, `DOMAIN`, `CONCEPT_CLASS`, `RELATIONSHIP`, `DRUG_STRENGTH`).
      - **[!] blocked** on `VOCABULARY_SOURCE_MISSING`. The loader and the deterministic lookup are implemented and exercised; `Vocabulary.open()` falls back to a null vocabulary that maps nothing rather than letting the pipeline pretend.
- [x] Record the vocabulary version in the run report.

### P2-2 Terminology mapping

- [x] Implement the deterministic flow from design §6.4: source string → source concept → `Maps to` → domain/validity check.
- [x] **Dispatch by unique normalized string**, not by raw row.
- [x] Lookup failure → lexical candidate recall → write to `review/pending.csv`.
- [x] **No hardcoded concept IDs anywhere in code or prompts** (grep test).
- [x] Fixed concepts are resolved from the vocabulary at startup with domain and standard status validated.

### P2-3 Mapping registry

- [x] `mappings/<domain>.csv`: source_string, code_system, concept_id, mapping_version, decision provenance.
- [x] A `compile` command turns `review/decisions.csv` into `mappings/`.
- [x] Versioned by git.
- [x] **Test**: a mapping absent from both `mappings/` and the vocabulary never reaches OMOP.

### P2-4 Table transforms

- [x] Implement PERSON / OBSERVATION_PERIOD / VISIT_OCCURRENCE / CONDITION_OCCURRENCE / DRUG_EXPOSURE / PROCEDURE_OCCURRENCE / MEASUREMENT / NOTE / DEATH / CDM_SOURCE per design §6.2.
- [x] `*_source_value` preserves the source value.
- [x] No concept found → 0 plus review; **never invent an ID**.
- [x] `OBSERVATION_PERIOD` uses an explicit heuristic with the rule version recorded.
- [x] BMI/Pulse/BP_Systolic default to quarantine; **never fabricate a measurement date** (tested).
- [x] No custom columns added to core tables.

### P2-5 Birth-year blocker

- [x] Implement `strict` (default) and `approved_approximation`.
- [x] Under `strict`, a missing birth year blocks that patient's OMOP publication and produces a report; canonical and MEDS are unaffected.
- [x] `approved_approximation` requires both `age_as_of_date` and a human approval, and emits `DERIVED_APPROXIMATE_BIRTH_YEAR`.
- [x] **Test**: it cannot be bypassed without approval.
- [x] **Test**: `Death_Date`, the first event date, and the run date are never used as the age reference date.

### P2-6 Lineage and loading

- [x] `etl_audit.lineage` covers every OMOP row.
- [x] `etl_audit.anchor` / `cohort_membership` / `quality_issue` / `run` are in place.
- [x] A single controlled load path (write partitioned staging, then load once); **never concurrent inserts**.
- [x] **Test**: no OMOP row lacks lineage.

### P2-7 Validation

- [x] No non-zero concept ID is absent from the vocabulary.
- [x] Concept domains are compatible with their target fields.
- [x] Foreign key / uniqueness / not-null constraints pass.
- [!] The three patients' OMOP rows trace back to source rows (manual spot check plus automated test).
      - **[!] blocked** on `AGE_REFERENCE_DATE_MISSING`: under the strict birth-year policy no
        patient reaches OMOP at all, so there are no rows of theirs to trace. The equivalent
        assertion runs on `ctpe_shape`, where an approved approximation is legitimate:
        `test_ctpe_shape_anomalies.py::test_the_full_pipeline_publishes_and_validates` plus the
        `OMOP_EVERY_ROW_HAS_LINEAGE` and `OMOP_REFERENTIAL_INTEGRITY` checks.
- [ ] (Optional) DataQualityDashboard, which needs R + PostgreSQL; not a release gate.

---

## P3. MEDS

### P3-1 Mapping

- [x] Emit the required and extension columns from design §7.1.
- [x] `available_time` is populated correctly; flag `AVAILABILITY_ASSUMED` when absent.
- [x] Code namespace per §7.2 (`OMOP/<id>`, `SOURCE/<source>/<code>`, `MEDS_BIRTH`, `MEDS_DEATH`).
- [x] **Test**: one clinical fact never yields both a SOURCE-code and an OMOP-code event.

### P3-2 Metadata

- [x] `metadata/dataset.json` (MEDS package version, vocabulary version, config hash).
- [x] `metadata/codes.parquet` contains **every code actually used** (test asserts set equality).
- [x] `metadata/subject_splits.parquet`.

### P3-3 Sharding and splits

- [x] One shard per patient; events contiguous and time-sorted within a shard (tested).
- [x] Splits by stable per-patient hash into train/tuning/held_out, mutually exclusive.
- [x] **Test**: splits are generated after cross-partition identity resolution; all events of one MRN share a split.

### P3-4 Leakage prevention ★

- [x] `membership_label`, `partition_id`, and file names are **not written into MEDS event rows** by default (tested).
- [x] `has`/`no` becomes a task label only when bound to an `anchor_id`, a definition rule, and a prediction time.
- [x] **Test**: an as-of view contains no event with `available_time > prediction_time`.
- [x] Scan for direct identifiers (MRN and similar) before export.

### P3-5 Validation

- [x] Validate all shards and metadata with the installed MEDS schema validator (**mandatory**).
- [x] Independently check Parquet schema, ordering, and the per-patient shard constraint.

---

## P4. LLM assistance

**Precondition**: P1 is complete, i.e. the data converts correctly with no LLM at all. This phase only adds assistance.

### P4-1 Client

- [x] OpenAI-compatible HTTP client depending only on `LLM_BASE_URL` / `LLM_MODEL`.
- [x] Probe capabilities once at startup (is JSON-schema output available?); **never assume from the model name**.
- [x] temperature = 0 with a pinned sampling configuration.
- [x] Validate output against a Pydantic schema; at most 2 retries, then review.
- [x] Record template file hash, input hash, and token counts; **never log full patient text**.

### P4-2 The two uses

- [x] **Column semantics proposal**: input column names + profile statistics + a few de-identified samples; output a suggested `logical_type` and field roles into `review/pending.csv`.
- [x] **Terminology candidate ranking**: input a normalized source string + lexically recalled candidates; output a ranking with rationale into `review/pending.csv`.
- [x] Neither may write to `mappings/` or any target layer (tested).
- [x] Prompt templates live in `prompts/*.md`, versioned by git.

### P4-3 Review loop

- [x] `propose` → `review/pending.csv`.
- [x] A human edits `review/decisions.csv`.
- [x] `compile` → `mappings/`.
- [x] `transform` reruns.
- [x] **Test**: undecided pending items never reach OMOP/MEDS.

### P4-4 Measure the benefit ★

- [!] Build a small human gold set (a few dozen terminology mappings is enough).
      - **[!] blocked** on the vocabulary: candidate recall needs one before a human can accept anything. `ehr2trace measure --from-decisions` builds the gold set from accepted decisions.
- [!] Compare human-acceptance rates across three configurations: deterministic lookup only / plus lexical recall / plus LLM ranking.
      - **[!] not measured**: implemented in `measure.py`, needs a served model and a vocabulary. Until it is run, the ranking step is unjustified — use `--no-llm`.
- [!] Record JSON schema success rate, retry rate, and latency.
      - **[!] not measured**: recorded by `LlmClient.usage_summary()` and reported by `measure`; needs a served model.
- [!] **If the LLM shows no measurable gain, delete the second use in P4-2** and keep lexical recall alone.
      - **[!] pending the measurement above.** `measure` prints the verdict in words, including the instruction to delete the step.

---

## P5. Generalization and optional extensions

### P5-1 No hardcoding

- [x] `tests/test_no_hardcoded_dataset_strings.py` greps core code and forbids `29_has_embolism`, `29b`, `dos`, `Closest_to_CT`, `MRN`, `Encounter_CSN`, `has_embolism`, PE ICD codes, and similar.
- [x] Those strings may appear only in `datasets/*.yaml`, `tests/fixtures/`, and documentation.

### P5-2 Synthetic EHR fixture

- [x] `tests/fixtures/generic_ehr/`: a minimal synthetic dataset structurally unlike CTPE (different column names, different file layout, CSV rather than TSV, no anchor concept).
- [x] Write one new YAML and **change no core code**; run canonical → OMOP → MEDS end to end.
- [x] This is the only evidence for the word "generalization". Without it, do not claim generalization in the documentation.

### P5-3 Optional: narrative extraction

> **Not attempted.** The prompt template exists (`prompts/narrative_extraction.md`) and states
> the span, negation and experiencer rules, but nothing calls it. Building extraction before
> the terminology measurement in P4-4 has run would be adding a component that cannot yet be
> shown to help, which is the thing this design spends most of its length avoiding.

- [ ] Runs only when explicitly enabled.
- [ ] Outputs candidate facts with evidence spans; spans must match the source **character for character** (tested).
- [ ] Single-patient isolation; **no cross-patient context**.
- [ ] Results are separate events with `provenance_status = llm_proposed`, becoming `human_approved` only after confirmation.
- [ ] **Never overwrite the NOTE original text.**
- [ ] Build a small gold set to evaluate assertion / experiencer / span accuracy.

### P5-4 Optional: task layer

> **Not attempted, and currently unbuildable.** A task layer needs the label definition and the
> episode binding, both of which are open blockers. Defining a prediction task while "what does
> this label mean, and as of when" is unanswered would produce a benchmark whose numbers mean
> nothing. `meds.as_of_view()` and the availability checks are the pieces that will be needed.

- [ ] Cohort / prediction time / label / input window definitions live in a separate file.
- [ ] Reads published MEDS and **never writes back to clinical facts**.
- [ ] The CTPE PE label must be episode-level and bound to an `anchor_id`.
- [ ] Leakage test: the input view contains no partition, filename, or membership features.

---

## Definition of done

The overall delivery is complete when:

- [x] All 4 partitions, 25 files, and 40 sources are processed with a complete reconciliation report.
- [x] All 30 checks in design §10.2 pass.
- [x] The three-patient and partition-overlap fixtures pass.
- [x] Every OMOP row is traceable and the MEDS validator passes.
- [x] `--workers 1` and `--workers N` agree; reruns are idempotent.
- [x] The synthetic `generic_ehr` fixture passes using only a new YAML.
- [x] Unresolved blockers are listed explicitly in the publication report, not hidden as assumptions.
- [x] The README explains how to reproduce a full run from scratch.
- [x] The repository contains no PHI.

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
