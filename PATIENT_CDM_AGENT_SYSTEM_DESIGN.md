# Local EHR → OMOP CDM / MEDS Converter: Design Specification

> Document type: implementation spec for a coding agent
> Version: 0.5 (changes from 0.4 are listed in "Revision history" at the end)
> Date: 2026-08-20
> Reference data: 4 partitions under `$EHR_DATA_ROOT` (CTPE extract)
> First acceptance patients: `PT-A`, `PT-B`, `PT-C` (pseudonyms; see the note in §2.5)
> Positioning: **a research data converter**, not a production system

---

## 0. TL;DR

Turn 8 GB of messy hospital exports (txt + xlsx) into two standard formats: OMOP CDM 5.4 and MEDS.

```text
raw txt/xlsx (read-only)
    ↓ deterministic parsing + row-level lineage
source/            one Parquet per logical source, original values preserved
    ↓ deterministic normalization + deduplication
canonical/         canonical event store — the single source of truth
    ↓                    ↓
omop/              meds/
```

The LLM appears in exactly two places, and in both it only proposes — it never writes data:

1. Column-semantics proposals when onboarding a new source (a human glances and confirms).
2. Terminology candidate ranking, invoked only when deterministic lookup fails.

**Four rules that must never be violated:**

1. The LLM does not parse dates, numbers, or table structure; it does not invent concept IDs; it does not write to any target layer.
2. Every canonical / OMOP / MEDS record traces back to a `source_row_id`.
3. Anything uncertain goes to `quarantine/` or `review.csv`. Never guess.
4. Same input + same config + same mapping version → same output hash.

**What this is not:** not a Kubernetes microservice, not a distributed task queue, not a general-purpose EHR comprehension engine, not a clinical decision support product.

---

## 1. Goals and scope

### 1.1 In scope

- Read all 25 physical files / 40 logical sources across the 4 partitions.
- Resolve patient identity across the whole dataset (one MRN → one subject, across batches and partitions).
- Preserve row-level lineage and time semantics; produce a canonical event table.
- Perform deterministic terminology mapping against a local OMOP vocabulary.
- Emit two publication views: OMOP CDM 5.4 and MEDS.
- Provide regression fixtures for three sample patients.
- Run fully offline; patient data never leaves the machine.
- Onboard a structurally similar EHR by writing one new dataset YAML, with no core code changes.

### 1.2 Out of scope

- No long-running services, web UI, container orchestration, message brokers, or distributed backends.
- No PostgreSQL work ledger, task leases, heartbeats, or fault-injection framework.
- No multi-layer pack system (three-tier Dataset/Domain/Task Pack with semver + CHANGELOG + conformance suite).
- No vector database or multi-corpus RAG governance.
- No LLM fine-tuning, online learning, or automatic modification of approved mappings.
- The LLM never executes shell commands or arbitrary SQL.
- Never impute values for missing fields; never read an empty sheet as "this patient had no such event".
- No diagnostic recommendations.

### 1.3 Definition of success

- The same input produces the same output.
- Uncertain content is explicitly isolated rather than silently guessed.
- Anyone can answer "where did this OMOP/MEDS record come from, and why was it mapped this way?"
- The three-patient fixtures pass, and the same rules hold on the full dataset.
- One person can read and understand the entire core in a week.

### 1.4 What "generalization" means here

Generalization = swap the data source by changing configuration, not core code. Three levels:

1. **Structural**: changes in column names/order, file names, sheet names, encoding, or missing columns are handled by the dataset YAML.
2. **Source format**: the same logical event may arrive as txt, xlsx, or Parquet, handled by three adapters.
3. **Semantic**: identity keys, timezone, time priority, and code systems are declared in configuration.

**Anything that cannot be generalized must stop with an error**: unknown patient primary key, unparseable date format, missing code system. These are blockers, not places for the LLM to guess.

---

## 2. Input facts (all empirically verified)

Every number in this section is re-derived from the raw data and compared item by item by
`tools/verify_doc_baselines.py`. Last full pass: 2026-08-20, 55/55 checks green.

```bash
python3 tools/verify_doc_baselines.py
```

These are regression baselines: **if a number changes, report the drift and investigate the data — do not edit the expected values here.**

### 2.1 Layout and size

Input root: `$EHR_DATA_ROOT`

| `partition_id` | Directory | Measured size | Physical sources |
|---|---|---:|---|
| `29_has` | `29 - pulmonary embolism/has_embolism` | 1.6 GB | 5 txt + 1 xlsx (5 sheets) |
| `29_no` | `29 - pulmonary embolism/no_embolism` | 2.1 GB | 5 txt + 1 xlsx (5 sheets) |
| `29b_has` | `29b - pulmonary embolism/has_pulmonary_embolism` | 2.2 GB | 6 txt + 1 xlsx (4 sheets) |
| `29b_no` | `29b - pulmonary embolism/no_pulmonary_embolism` | 2.1 GB | 5 txt + 1 xlsx (5 sheets) |

Totals: **25 physical files**, **21 txt logical sources + 19 xlsx sheets = 40 logical sources**, about 8.0 GB.
The four directory names are inconsistent (`has_embolism` vs `has_pulmonary_embolism`), so the config must declare each partition path explicitly.
`.DS_Store` and analysis outputs are not inputs.

All txt files are tab-delimited UTF-8. **BOM presence is inconsistent**: 19 of the 21 txt files carry a BOM (`EF BB BF`), but `29_has_embolism_problem_list.txt` and `29_no_embolism_problem_list.txt` **do not** (the two `29b` problem_list files do). The parser must attempt BOM stripping unconditionally — it can assume neither presence nor absence. Otherwise the first column name becomes `﻿MRN` in some files of the same logical source.

### 2.2 Logical sources and schemas

**txt (21 sources; the schema of a given logical source is identical across all 4 partitions — verified column by column):**

| Logical source | Columns | Partitions |
|---|---|---|
| `all_rx` | `MRN, Encounter_CSN, Medication_Name, HV_Discrete_Dose, Ordering_Date, Order_Status` | all 4 |
| `echo` | `mrn, CSN, DESCRIPTION, dos, result_time, Rad_Result_type, LINE, NARRATIVE, Closest_to_CT` | all 4 |
| `ekg` | `MRN, CSN, Procedure_Name, dos, Result_Time, Component_Name, Line, Result_Value, Closest_to_CT` | all 4 |
| `labs` | `MRN, dos, CSN, Result_Time, Collection_time, Lab_Name, Base_Name, Value, Units, Reference_Range_Low, Reference_Range_High, rn` | all 4 |
| `problem_list` | `MRN, Diagnosis_Code, Diagnosis_Name, First_Noted_Date, Status` | all 4 |
| `medication_admin` | `MRN, CSN, Date_Administered, Medication_Name, Dose, Route` | **`29b_has` only** |

**xlsx (19 sheets):**

| Sheet | Columns | Notes |
|---|---|---|
| `Demographics` | `MRN, Age, Gender, Race, Ethnicity, Status, Death_Date, BMI, Pulse, BP_Systolic` | all 4 partitions |
| `Medication Administration` | same as the txt variant | present in `29_has`/`29_no`/`29b_no`; **absent in `29b_has`** (shipped as a standalone txt instead) |
| PFT narrative | `mrn, CSN, DESCRIPTION, dos, result_time, Rad_Result_type, LINE, NARRATIVE, Closest_to_CT` | sheet name is `PFT` in `29_no`, `PFT Narrative` elsewhere |
| PFT values | `MRN, CSN, Procedure_Name, dos, Result_Time, Component_Name, Line, Result_Value, Closest_to_CT` | sheet name is `PFT Value` in `29_no`, `PFT Values` elsewhere |
| `Outcome` | `MRN, CSN, Date_of_Service, Hosp_Admsn_Time, Length_of_stay_days, Time_Difference, Death_Date, Hospital_Visit_Type` | all 4 partitions |

Verified schema anomalies that must be encoded in configuration:

- **`29b_no`'s PFT Narrative has an extra leading column with an empty header**, all values `None`. Keep it in the source layer; skip it positionally in the canonical layer.
- The two PFT sheet aliases above.
- `29b_has`'s medication admin is a standalone txt, not a sheet.
- **BOM presence differs within the same logical source** (see §2.1); first-column name normalization must handle it.

### 2.3 Verified semantics and traps

This is the most important section in the document. Every item was empirically verified.

**About `dos` (the CT anchor)**

1. `dos` is the **CT anchor** used at extraction time to compute "closest to CT". It is *not* the true event time of the echo/ECG/lab/PFT record.
2. **The `dos` format differs between batches**: `29` carries a full timestamp (shaped `YYYY-MM-DD hh:mm:ss.0000000`, 7 fractional digits); `29b` carries a **date only** (`YYYY-MM-DD`, time component absent). Anchors cannot be joined across batches by equality — normalize to date granularity first, and record that the time component is lost in `29b`.
3. **Tables carrying `dos` are duplicated in full, once per anchor.** Measured: `PT-B`'s echo rows in `29b_has` are 3 anchors × 547 rows = 1,641 rows, with an identical row count per anchor. The canonical dedup key must exclude `dos`, while every anchor relationship is retained.
4. **The anchor set varies by partition.** Measured: `PT-B` has 4 distinct anchors — 2 seen in `29_has`, 3 in `29b_has` (including one absent from `29_has`), and 1 seen only in `29b_no`. The anchor dedup key is therefore `(anchor_date, partition_id)`, not `anchor_time` alone.
5. `Closest_to_CT` is a **rank** ("the Nth closest to that anchor"), not a day difference. Day differences must be recomputed from timestamps.

**About the has/no label**

6. `has`/`no` is a source cohort partition label, not a patient-level clinical fact. It must never auto-generate a CONDITION_OCCURRENCE.
7. **The label is CT-episode level, and this is empirically demonstrated**: `PT-B` appears in both `29b_has` and `29b_no` — the latest CT falls in the `no` partition while the earlier ones fall in `has`. Treating it as a lifetime patient-level label produces outright wrong annotations.
8. The same MRN can appear across batches and across has/no (1,609 measured). Identity must be resolved across all four partitions before deduplication and splitting.
9. Partition names, file names, and membership labels are provenance. **They must never be model inputs for predicting that same label.**

**About time and event types**

10. Clinical event time priority: `Collection_time` for labs, `result_time` for echo, `Result_Time` for ECG/PFT. Result-visible time is stored separately as `available_time`.
11. `all_rx` is ordering intent; `Medication Administration` is actual administration. Different facts — never merge them.
12. `First_Noted_Date` is when the problem was first recorded, not a guaranteed disease onset date.
13. Problem-list entries may be back-filled after the death date. Flag them `RECORDED_AFTER_DEATH`; do not delete them and do not rewrite the date.
14. `29` and `29b` are two extraction batches, not a replacement relationship. Read both and deduplicate via lineage.

**About data types (new in v0.5, found empirically)**

15. **The same logical column has different cell types across workbooks.** Two measured instances:
    - `Demographics.Death_Date`: `datetime` in `29_has`/`29_no`/`29b_no`, but **`str` in `29b_has`**.
    - `Outcome.Length_of_stay_days`: `int` in `29_has`/`29b_has`/`29b_no`, but **`str` in `29_no`**.

    This directly threatens cross-batch deduplication: without normalizing cells to strings before hashing, the same record yields different `source_row_sha256` values in the two batches and dedup fails silently. See §5.1 for the canonical serialization rule.
16. openpyxl returns types per cell, so **rows within the same column can differ in type**. Type handling must be per value; never infer from the first row of a column.
17. `Outcome.Time_Difference` is a masked string (e.g. `-11:0*:0*:00`) where `*` hides a digit. Do not parse it as a duration; keep it verbatim.

**About missingness**

18. An empty PFT/Outcome sheet means this extract carried no data. Record coverage as `unknown/not_extracted`; **it must never be read as the patient being negative.** Measured: the `Outcome` sheet covers only 2,070 of the 5,633 people in `29_has`.
19. Demographics carries only `Age` — no birth date and no age-as-of date. This blocks a strictly compliant OMOP `PERSON` (see §6.3).
20. The current data contains **no CT/CTPA report text**. Measured: the `DESCRIPTION` column of the echo tables across all 4 partitions has 14 distinct values, all transthoracic echo (TTE) variants, with no CT/CTPA entry. Never synthesize imaging conclusions from `dos` or from the partition label.
21. Labs cover a limited panel (measured across the three patients: Na, K, BUN, creatinine, Hct, WBC, total bilirubin, arterial pH, SaO2 — 9 analytes). There is **no D-dimer, troponin, BNP, or platelet count**. Absent is not negative.
22. `Reference_Range_Low/High` are all `NULL` in the measured data; reference ranges must come from outside the source.

### 2.4 Partition and patient overlap baselines (measured)

| Partition | Unique MRNs |
|---|---:|
| `29_has` | 5,633 |
| `29_no` | 8,623 |
| `29b_has` | 8,247 |
| `29b_no` | 8,468 |
| **Union of all four** | **22,982** |

Pairwise intersections:

| | `29_no` | `29b_has` | `29b_no` |
|---|---:|---:|---:|
| `29_has` | 723 | 5,633 | 392 |
| `29_no` | — | 923 | 747 |
| `29b_has` | — | — | 927 |

Derived (all measured): all 5,633 people in `29_has` are contained in `29b_has` (empty difference); the union of the has partitions = 8,247 = `29b_has`; the has/no intersection is 1,609 MRNs.

### 2.5 Three-patient baselines (measured, partition-scoped)

**These numbers are partition-scoped, not per-patient totals.** The same patient has different row counts in different partitions, scaling linearly with the number of CT anchors recorded in that partition.

**On patient identifiers**: this repository's documents, tests, and scripts use the pseudonyms `PT-A` / `PT-B` / `PT-C` throughout. The real MRNs and patient-level service dates (CT anchor dates) live in `tools/baselines.local.json`, generated by `tools/make_local_baselines.py` on a machine that has the raw data, and excluded by `.gitignore`. This keeps the documents shareable while the verification script still produces complete results locally.

Which partitions each patient appears in:

| Patient | `29_has` | `29_no` | `29b_has` | `29b_no` |
|---|---|---|---|---|
| `PT-A` | yes | — | yes | — |
| `PT-B` | yes | — | yes | **yes** |
| `PT-C` | yes | — | yes | — |

Row counts in `29_has`:

| Patient | txt total | xlsx total | CT anchors in this partition |
|---|---:|---:|---:|
| `PT-A` | 5,727 | 379 | 1 |
| `PT-B` | 4,243 | 206 | 2 |
| `PT-C` | 377 | 22 | 1 |

Row counts in `29b_has`:

| Patient | txt total | xlsx total | CT anchors in this partition |
|---|---:|---:|---:|
| `PT-A` | 6,095 | 11 | — |
| `PT-B` | 6,162 | 1 | 3 |
| `PT-C` | 398 | 1 | — |

`29b_no` (only `PT-B` of the three appears): 2,529 txt rows, 206 xlsx rows, 1 anchor.

**Direct evidence of anchor duplication**: `PT-B`'s echo rows are 1,094 in `29_has` = 2 anchors × 547; 1,641 in `29b_has` = 3 anchors × 547; 547 in `29b_no` = 1 anchor × 547. The per-anchor base is identical across all three partitions.

**Therefore raw row counts are a poor regression target.** Regression should assert:

- raw row count per partition per source (a parsing-completeness check);
- canonical event count after anchor deduplication (a semantic-correctness check);
- the anchor set per patient (an anchor-handling check).

Earlier exploratory scripts under the CTPE analysis directories may be useful references for field understanding and fixture generation, but their hand-curated per-patient decision chains are an annotation layer and **must never become ETL rules or clinical facts**.

---

## 3. Architecture

### 3.1 Three layers, and that is all

```text
raw (read-only, untouched)
  ↓  adapter: parse, type, row-level lineage
source/<partition>/<source>.parquet      original values + typed columns
  ↓  normalize: identity, time semantics, dedup
canonical/events.parquet                 single source of truth
  ↓                                ↓
omop/ (DuckDB or Postgres)         meds/ (Parquet)
```

**Why a canonical layer is required**: OMOP is designed for analysis, MEDS for event-stream modeling, and neither converts losslessly into the other. OMOP core event tables have no uniform `available_time`; MEDS has no complete relational visit model. So canonical is the source of truth, and OMOP and MEDS are two views that can be rebuilt at any time. **Never build OMOP first and derive MEDS from it.**

Human-written reports, agent reasoning output, and retrospective diagnosis chains never enter canonical.

### 3.2 Responsibility boundary: LLM vs code

| Work | Who | Why |
|---|---|---|
| File discovery, hashing, encoding detection | code | reproducible |
| Date/number/unit parsing | code | testable, must not be speculated |
| Dedup, identity resolution, counting | code | requires strict consistency |
| Known ICD/LOINC/RxNorm lookup | vocabulary SQL | authoritative and traceable |
| Interpreting a new column's semantics | LLM proposes + human confirms | genuinely ambiguous |
| Ranking terminology candidates | retrieval + LLM ranking + human confirms | needs semantics, must not invent codes |
| Narrative fact extraction (optional) | LLM + evidence span + human confirms | unstructured task |
| Writing to any target layer | code | the LLM has no write access |
| Quality checks | deterministic rules | regressable |

### 3.3 Execution model: process pool + content addressing

8 GB of data, one machine. **No task queue, leases, heartbeats, or distributed backend are needed.**

```python
# this is the entire parallelism model
tasks = plan(config)                       # pure function: config → task list
with ProcessPoolExecutor(workers) as ex:
    results = list(ex.map(run_one, tasks))
merged = merge(sorted(results, key=stable_key))   # sort before merging: order-independent
```

Four constraints give correctness and resumability:

1. **Content addressing**: each task's output path embeds `hash(input file hashes + config hash + code version)`. If it already exists, skip it — that *is* resumption, no ledger required.
2. **Atomic writes**: write `<name>.partial`, verify, then `os.replace`. Leftover `.partial` files from a crash never enter the merge.
3. **Deterministic merge**: sort by a stable primary key before merging. Task completion order does not affect the result.
4. **Subject bucketing**: `bucket = sha256(subject_id) % bucket_count`; a patient always lands in the same bucket. Bucket count is config (default 64); worker count is a runtime parameter.

**Must be tested**: `--workers 1` and `--workers 4` produce byte-identical canonical/OMOP/MEDS data hashes.

Stages have ordering dependencies (barriers), expressible as plain sequential calls:

```text
discover → ingest → identity → canonical → {omop, meds} → validate
```

`identity` must complete globally before bucketed deduplication starts — that is the only true barrier.

---

## 4. Configuration: one YAML

This replaces v0.4's three-tier Dataset/Domain/Task Pack system. One file per dataset in `datasets/<id>.yaml`, versioned by git.

```yaml
dataset_id: ctpe
root_env: EHR_DATA_ROOT

identity:
  person_key: mrn
  encounter_key: csn

time:
  timezone_assumption: null           # must be supplied by the data owner, otherwise a blocker
  # never default to the developer machine's timezone

partitions:
  - id: 29_has
    dir: "29 - pulmonary embolism/has_embolism"
    membership_label: has             # provenance, not a clinical fact
    batch: "29"
  - id: 29_no
    dir: "29 - pulmonary embolism/no_embolism"
    membership_label: no
    batch: "29"
  - id: 29b_has
    dir: "29b - pulmonary embolism/has_pulmonary_embolism"
    membership_label: has
    batch: "29b"
  - id: 29b_no
    dir: "29b - pulmonary embolism/no_pulmonary_embolism"
    membership_label: no
    batch: "29b"

sources:
  labs:
    adapter: delimited
    file_glob: "*labs.txt"
    options: {delimiter: "\t", encoding: utf-8-sig}
    logical_type: measurement
    required: true
    fields:
      person_id:      {from: [MRN, mrn]}
      encounter_id:   {from: [CSN]}
      event_time:     {from: [Collection_time]}
      available_time: {from: [Result_Time]}
      anchor_time:    {from: [dos]}          # anchor, NOT an event time
      code_name:      {from: [Base_Name]}
      display_name:   {from: [Lab_Name]}
      value:          {from: [Value]}
      unit:           {from: [Units]}

  medication_admin:
    adapter: any_of                    # one logical source, two physical forms
    variants:
      - {adapter: delimited, file_glob: "*medication_admin.txt"}
      - {adapter: excel, sheet_aliases: ["Medication Administration"]}
    logical_type: drug_administration
    required: false

  pft_narrative:
    adapter: excel
    sheet_aliases: ["PFT Narrative", "PFT"]
    skip_unnamed_leading_columns: true  # 29b_no's empty first column
    logical_type: note
    required: false
  # ... remaining sources follow the same shape

anchors:
  anchor_type: ctpa
  from_field: anchor_time
  granularity: date                    # 29b has date only; compare at date granularity
  dedup_key: [anchor_date, partition_id]

labels:
  - id: pulmonary_embolism
    scope: episode                     # not patient
    from: membership_label
    requires_anchor: true
    definition_status: undefined       # data owner has not supplied the rule → this is a blocker
```

Rules:

- The YAML may only reference adapter names registered in code. **It must not load arbitrary Python.**
- A config change is a new `config_hash` and automatically produces a new output directory.
- Core code must not contain `29_has_embolism`, `dos`, `Closest_to_CT`, `MRN`, PE ICD lists, or any sheet name — a grep test enforces this.

---

## 5. Data contracts

### 5.1 Columns the source layer must preserve

```text
source_row_id          str    # sha256(dataset_id|partition_id|source_id|file_sha256|row_number)
partition_id           str
batch                  str    # 29 / 29b
membership_label       str    # has / no — provenance, not a fact
source_file            str
source_sheet           str?
source_row_number      int64
source_file_sha256     str
source_row_sha256      str    # see the canonical serialization rule below
person_source_id       str    # original MRN (protected field)
encounter_source_id    str?
parse_status           enum[ok, quarantined]
parse_issues           list[str]
<all original columns, values never overwritten>
<typed columns, distinguished by a _parsed suffix>
```

**Canonical serialization rule (new in v0.5, required because of the type drift in §2.3 #15)**

Before computing `source_row_sha256`, every cell must be converted to a string by the rules below and joined with `\x1f`:

| Python type | Canonical string |
|---|---|
| `None` | `""` |
| `str` | `strip()`ed as-is; the literal `"NULL"` normalizes to `""` |
| `int` | `str(v)` |
| `float` | integral values render as integers (`5.0` → `"5"`); otherwise the shortest round-trip `repr` |
| `datetime` / `date` | ISO 8601, second precision, no timezone suffix (shaped `YYYY-MM-DDThh:mm:ss`) |
| `bool` | `"true"` / `"false"` |

There must be a test asserting that `29b_has`'s `Death_Date` (a `str`) and `29_has`'s same value for the same patient (a `datetime`) **normalize to the same string**.

`subject_id` is derived from the MRN by a stable irreversible mapping to `int64`, scoped to the **entire dataset** rather than a partition. The mapping table is stored separately, protected, and never committed. Neither OMOP nor MEDS carries the MRN.

### 5.2 Canonical event schema

```yaml
CanonicalEvent:
  event_id: str                    # stable hash, primary key
  subject_id: int64
  encounter_id: str | null
  event_kind: enum                 # demographic/visit/condition/drug_order/drug_admin/
                                   # procedure/measurement/note/death
  event_time: datetime | null      # clinical occurrence / collection time
  available_time: datetime | null  # when the result first became visible
  end_time: datetime | null
  code_system: str                 # SOURCE / ICD10CM / LOINC / RxNorm / OMOP
  source_code: str | null
  source_name: str | null
  standard_concept_id: int | null
  value_number: float | null
  value_text: str | null
  value_low: float | null          # lower bound of a ranged value, see §5.4
  value_high: float | null
  unit_source: str | null
  unit_concept_id: int | null
  status_source: str | null        # Order_Status etc.
  route_source: str | null
  dose_source: str | null
  provenance_status: enum          # observed / derived / llm_proposed / human_approved
  mapping_version: str
  quality_flags: list[str]
```

Three relation tables:

```yaml
EventSource:            # one event ↔ many source rows (cross-partition duplicates)
  event_id, source_row_id, relation: enum[derived_from, duplicate_of, anchored_to]

Anchor:                 # CT anchors, decoupled from events
  anchor_id, subject_id, anchor_type, anchor_date, anchor_time_known: bool
  partition_id, source_row_id

CohortMembership:       # has/no membership, decoupled from events
  subject_id, partition_id, batch, membership_label
  anchor_id | null, label_scope: episode|patient|unknown, source_row_id
```

Hard rules:

- One canonical event may link to many duplicate source rows; one source row may produce several events (e.g. "one study + several measurements").
- The same clinical event across batches/partitions produces exactly one `CanonicalEvent`, while all `EventSource` links and `CohortMembership` rows are retained.
- `dos` only enters `Anchor` and never overwrites `event_time`.
- `CohortMembership` is separate from events; a directory label must never become a condition or observation.
- Static demographics without a time may have `event_time = null`; a **clinical** event without a time goes to quarantine. **Never substitute the current time.**

### 5.3 Time priority

| Source | `event_time` | `available_time` | Notes |
|---|---|---|---|
| labs | `Collection_time` (fall back to `Result_Time` with a flag) | `Result_Time` | keep both |
| echo | `result_time` | `result_time` | no true performed time exists; never use `dos` |
| ECG / PFT values | `Result_Time` | `Result_Time` | `dos` is anchor only |
| PFT / echo narrative | `result_time` | `result_time` | text goes to NOTE |
| all_rx | `Ordering_Date` | same | null date → quarantine |
| medication admin | `Date_Administered` | same | actual administration |
| problem_list | `First_Noted_Date` | same | not marked as onset |
| Outcome / visit | `Hosp_Admsn_Time`, fall back to `Date_of_Service` | same | — |
| death | `Death_Date` | same | conflicting sources → review |
| BMI / Pulse / BP_Systolic | **no time** | — | see §6.2; not published by default |

All times are converted to UTC using the timezone declared in configuration. When the source has no timezone, store a `timezone_assumption` field. **Never default to the developer machine's timezone.**

### 5.4 Value parsing rules (new in v0.5)

Lab and study results are not clean numbers. Forms observed in the real data:

| Input form | Example (real data) | Destination |
|---|---|---|
| plain number | `4.2` | `value_number` |
| number + text unit | `38.62` with `K/cu mm` | `value_number` + `unit_source` |
| range | `35-40` (echo RVSP, mmHg) | `value_low` + `value_high`; `value_number` stays empty |
| comparator | `<0.5`, `>150` | `value_number` empty, `value_text` verbatim, flag `COMPARATOR_VALUE` |
| sentinel text | `see below` (as a potassium result) | `value_text`, flag `NON_NUMERIC_RESULT` |
| free-text diagnosis | `SINUS TACHYCARDIA` (an ECG DIAGNOSIS component) | `value_text` |
| signature line | `Confirmed by ... on <date>` | flag `SIGNATURE_LINE`, not a result event by default |

Rules:

- The parser tries the forms in the order above; **anything that matches none goes to quarantine**, never a forced numeric cast.
- `value_number` and `value_text` must not carry the same meaning simultaneously.
- Writing OMOP `MEASUREMENT`: `value_number` → `value_as_number`; a ranged value keeps the original in `value_source_value` with `value_as_number = NULL`; a text result attempts `value_as_concept_id`, falling back to 0 with `value_source_value` preserved.

---

## 6. Canonical → OMOP CDM 5.4

### 6.1 General rules

- Target OMOP CDM 5.4, using the official DDL; never rewrite the core table structure.
- Core tables hold confirmed results only; candidates and evidence live in a separate `etl_audit` schema.
- Every OMOP row has at least one `source_row_id` in `etl_audit.lineage`.
- `*_source_value` preserves the source value; `*_concept_id` accepts only concepts that exist in the local vocabulary with a valid domain.
- When no suitable concept exists, use 0 per OMOP convention, preserve the source value, and send it to review. **The LLM must never invent an ID.**
- When both a source concept and a standard concept exist, store both and record the `Maps to` path.

**Storage choice**: DuckDB by default (single file, zero deployment, sufficient for research). When the OHDSI tool chain is needed, export to PostgreSQL using the same DDL. This is a config option and does not affect conversion logic.

### 6.2 Table-level mapping

| Source | OMOP target | Rules |
|---|---|---|
| Demographics | `PERSON` | Gender/Race/Ethnicity map to standard concepts with source values preserved. `Age` alone cannot satisfy `year_of_birth`; see §6.3 |
| all valid clinical dates | `OBSERVATION_PERIOD` | no enrollment data, so use an explicit "first to last trustworthy event date" heuristic; record the rule version; do not widen the interval |
| Outcome | `VISIT_OCCURRENCE` | `CSN` is the source key; start prefers `Hosp_Admsn_Time`; end may be derived from LOS but must be flagged derived |
| `Death_Date` in Demographics / Outcome | `DEATH` | merge when consistent, send conflicts to review, keep provenance |
| problem_list | `CONDITION_OCCURRENCE` | look up `Diagnosis_Code` as a source concept then `Maps to`; the type concept says "problem list entry" — **do not claim onset** |
| all_rx | `DRUG_EXPOSURE` | order/prescription type; `Ordering_Date` must be valid; retain `Order_Status`; **never treat as administered** |
| medication admin | `DRUG_EXPOSURE` | administered type; store dose/route; may link to the order but never merge |
| echo / ECG / PFT studies | `PROCEDURE_OCCURRENCE` | build a study-level event first; remove anchor-induced duplicates by the canonical key |
| echo / PFT narrative | `NOTE` | original text in NOTE; note date is the result time; LLM extractions never overwrite the original |
| lab rows | `MEASUREMENT` | map the name to LOINC; parse values per §5.4 |
| ECG / PFT components | `MEASUREMENT` | `Component_Name` is the measurement name; values per §5.4; link to the corresponding procedure |
| BMI / Pulse / BP_Systolic | **quarantine by default** | no measurement time exists. Publish only when configuration supplies a trustworthy time-association rule. **Never fabricate a date** |
| `dos` / `Closest_to_CT` | not an OMOP clinical fact | store in `etl_audit.anchor`; never generate a fake CT procedure |
| `has`/`no` | not an OMOP fact | store in `etl_audit.cohort_membership` |

### 6.3 The `Age` → `year_of_birth` blocker

OMOP 5.4 requires `PERSON.year_of_birth`, but the current data has only `Age` with no age-as-of date.

```yaml
omop:
  person_birth_policy:
    mode: strict | approved_approximation
    age_as_of_date: null        # must be supplied by the data owner
    approval_note: null
```

- `strict` (default): block OMOP publication for that patient and emit a blocker report; canonical and MEDS can still emit a static `AGE` event.
- `approved_approximation`: only when both `age_as_of_date` and a human approval are present, estimate the birth year by a versioned rule and emit a `DERIVED_APPROXIMATE_BIRTH_YEAR` flag (the estimate may be off by one year since it is unknown whether the birthday has passed).
- **Forbidden**: defaulting the age-as-of date to `Death_Date`, the first event date, or the program run date.
- "The database accepts a number" is not semantic compliance. The publication report must state this limitation.

### 6.4 Terminology mapping flow

```text
normalized source string
  → exact source code lookup
  → OMOP CONCEPT for the source concept
  → CONCEPT_RELATIONSHIP for "Maps to"
  → domain / validity check
  ├─ pass → adopt automatically
  └─ fail → lexical candidate recall → LLM ranks and explains only
            → human confirms → written to mappings/
```

Import locally at minimum: `CONCEPT`, `CONCEPT_RELATIONSHIP`, `CONCEPT_ANCESTOR`, `VOCABULARY`, `DOMAIN`, `CONCEPT_CLASS`, `RELATIONSHIP`, `DRUG_STRENGTH`.

Preferred vocabularies: diagnoses → SNOMED (via ICD); labs → LOINC; drugs → RxNorm; units → UCUM.

**Never scatter concept IDs through code or prompts.** Even fixed concepts are resolved by a vocabulary query at startup and validated for domain and standard status.

**Dispatch terminology work by unique normalized string, not by raw row.** The three patients' 1,599 `all_rx` rows collapse to a few dozen distinct drug names. The same holds at full scale — this is what turns millions of LLM calls into thousands.

### 6.5 Audit tables

The `etl_audit` schema needs only five tables:

```text
etl_audit.run            run_id, config hash, code version, timestamps, input file hash list
etl_audit.lineage        target_table, target_pk, event_id, source_row_id, mapping_version
etl_audit.anchor         see §5.2
etl_audit.cohort_membership  see §5.2
etl_audit.quality_issue  issue_type, severity, subject_id?, source_row_id?, detail
```

(v0.4's `work_item`, `task_attempt`, `artifact`, and `mapping_decision` tables are removed along with the execution engine; mapping decisions live in git-tracked `mappings/*.csv`.)

---

## 7. Canonical → MEDS

### 7.1 Output columns

```text
subject_id       int64            # MEDS required
time             timestamp[us]    # MEDS required; may be null for static events
code             str              # MEDS required
numeric_value    float32?
text_value       large_string?
--- extension columns below ---
event_id         str
encounter_id     str?
available_time   timestamp[us]?   # critical for preventing time leakage
end_time         timestamp[us]?
source_table     str
source_row_ids   list[str]
omop_concept_id  int64?
unit             str?
event_kind       str
quality_flags    list[str]
```

### 7.2 Code conventions

```text
OMOP/<standard_concept_id>                      # mapped
SOURCE/<source_id>/<normalized_source_code>     # unmapped but retained
MEDS_BIRTH
MEDS_DEATH
```

- `metadata/codes.parquet` must list **every code actually used**, with description, source vocabulary, OMOP concept, and mapping status.
- Do not emit both a SOURCE code and an OMOP code for the same clinical fact (it creates a duplicate training signal); use the best code for the main event and keep the source code in an extension column.
- A narrative becomes the `text_value` of a NOTE event; an LLM extraction is a separate event with its own lineage and never replaces the original text.

### 7.3 Training safety

- **`available_time` is the critical field for building a patient world model.** Any as-of sample may only use information with `available_time <= prediction_time`.
- When only the event time is known, conservatively use the same time and flag `AVAILABILITY_ASSUMED`.
- `metadata/subject_splits.parquet` splits `train` / `tuning` / `held_out` by stable per-patient hash; all events of one MRN land in one split.
- Splits must be generated **after** identity is resolved across all four partitions, never by splitting directories first and merging later.
- Each patient lives in exactly one shard; within a shard a patient's events are contiguous and time-sorted.
- **`membership_label` is not written into MEDS event rows by default**, to avoid direct leakage of the PE label; cohort membership stays in the audit layer.
- `has`/`no` may become a MEDS task label only once bound to a specific `anchor_id`, a definition rule, and a prediction time.
- The three regression patients should not double as an independent evaluation set for model performance.

**Version pinning**: pin the exact MEDS package version in the lockfile and record it in the run manifest; do not track `main`.

---

## 8. Where the LLM fits

### 8.1 Two necessary uses, plus one optional

| Use | Trigger | Input | Output | Human step |
|---|---|---|---|---|
| Column semantics proposal | once, when onboarding a new source | column names + profile statistics + a few de-identified samples | suggested `logical_type` and field roles | required |
| Terminology candidate ranking | when deterministic lookup fails | normalized source string + lexically recalled candidates | ranking + rationale | required |
| Narrative extraction (optional, last) | only when explicitly enabled | a single narrative | candidate facts with evidence spans | required |

**There is no sub-agent role system, no skill registry, and no prompt version management system.** Three prompt templates live in `prompts/*.md`, versioned by git, with the template file hash recorded at runtime.

### 8.2 Service and constraints

- A local OpenAI-compatible HTTP API (vLLM or llama.cpp); business code depends only on `LLM_BASE_URL` and `LLM_MODEL`.
- Must support JSON-Schema-constrained output; probe capabilities once at startup rather than assuming tool calling from the model name.
- temperature = 0; pin the model revision and sampling configuration.
- JSON parse failures retry at most twice, then go to review.
- **Data minimization**: send only the column descriptions, a few de-identified samples, or a single narrative needed for the task. Logs never store full patient text — only input hashes, template hashes, and token counts.
- The model service binds to localhost only, with telemetry and auto-download disabled.

### 8.3 Retrieval: only as much as needed

v0.4's three-index design with pgvector, hybrid retrieval, and 15-field chunk governance is **removed**.

- Terminology recall uses **SQL over the OMOP vocabulary tables plus simple lexical matching** (trigram / normalized strings). No vector store required.
- For OMOP/MEDS specification lookups, put the documents in `docs/standards/` and use ripgrep.
- Raw patient facts **never enter any retrieval index**. When the agent needs patient data it calls a structured read function with patient, column, and row-count limits.

If narrative extraction later genuinely needs semantic retrieval, add it then — an embedding model and one table.

### 8.4 Human review is a CSV

```text
review/pending.csv     awaiting confirmation: id, kind, source_string, candidates(json), context
review/decisions.csv   human decisions: id, decision, concept_id, reviewer, date, note
mappings/<domain>.csv  compiled mappings, versioned by git
```

Flow: `propose` writes `pending.csv` → a human edits `decisions.csv` with any tool → `compile` produces `mappings/` → `transform` reruns.

No review UI, no workflow engine, no reviewer permission model. Revisit when there is a real multi-reviewer need.

---

## 9. Project layout and CLI

### 9.1 Layout

```text
ehr-to-omop-meds/
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── datasets/
│   └── ctpe.yaml                  # the config from §4
├── mappings/                      # confirmed mappings, git-tracked
├── prompts/                       # 3 prompt templates
├── docs/standards/                # OMOP/MEDS specs, for ripgrep
├── sql/omop_5.4/                  # official DDL, with source and version recorded
├── src/ehr2cdm/
│   ├── cli.py
│   ├── config.py                  # YAML → Pydantic models
│   ├── hashing.py                 # §5.1 canonical serialization + stable IDs
│   ├── adapters/                  # delimited / excel / parquet
│   ├── ingest.py                  # raw → source parquet + lineage
│   ├── identity.py                # cross-partition MRN → subject_id
│   ├── canonical/
│   │   ├── normalize.py
│   │   ├── values.py              # §5.4 value parsing
│   │   ├── anchors.py
│   │   └── dedup.py
│   ├── terminology.py             # vocabulary lookup + candidate recall
│   ├── llm.py                     # client + JSON schema validation
│   ├── review.py                  # pending/decisions/compile
│   ├── omop.py
│   ├── meds.py
│   ├── validate.py
│   └── run.py                     # process pool + content addressing (§3.3)
└── tests/
    ├── unit/
    ├── fixtures/ctpe_three_patients/
    ├── fixtures/generic_ehr/       # synthetic, structurally different minimal EHR
    └── test_no_hardcoded_dataset_strings.py
```

Dependencies: Python 3.11+, Pydantic v2, Typer, Polars, PyArrow, DuckDB, openpyxl, httpx, pytest.
**No LangChain or LlamaIndex** — simple retrieval and explicit control flow are easier to audit.

### 9.2 CLI

```bash
ehr2cdm inspect      --dataset ctpe            # discover files, schemas, blocker report
ehr2cdm ingest       --dataset ctpe [--workers N]
ehr2cdm canonical    --dataset ctpe [--workers N]
ehr2cdm propose      --dataset ctpe            # writes review/pending.csv
ehr2cdm compile                                # decisions.csv → mappings/
ehr2cdm omop         --dataset ctpe
ehr2cdm meds         --dataset ctpe
ehr2cdm validate     --dataset ctpe [--all]
ehr2cdm report       --dataset ctpe --run-id <id>
```

Conventions:

- A non-zero exit code means incomplete.
- `--json` emits a stable machine format.
- `--patient <id>` is for debugging and fixture generation only; mappings still come from the global `mappings/`.
- `--workers 1` must work (the deterministic baseline).
- Outputs that already exist with a matching hash are reused — content addressing *is* resumption.
- `--dry-run` shows what would be read and written.

### 9.3 Environment and deployment

```bash
EHR_DATA_ROOT=/path/to/read-only/ehr-export
EHR_WORK_ROOT=/path/to/work/ehr2cdm
OMOP_BACKEND=duckdb                # or postgres
LLM_BASE_URL=http://127.0.0.1:8000/v1
OFFLINE_MODE=1
```

No compose file, no containers, no health-check service. When PostgreSQL is needed, use a system package or a single `docker run`, documented in the README.

Hardware: the ETL path needs 8–16 cores and 32 GB RAM; it completes every deterministic transformation without a GPU. LLM assistance needs one GPU. Reserve 5–10× the raw data size in disk.

---

## 10. Validation and testing

### 10.1 Reconciliation

For every source, produce:

```text
source_rows = parsed_rows + quarantined_rows
every canonical event has at least one source_row_id
every OMOP / MEDS row has at least one source_row_id
```

Because of one-to-many and many-to-one relations, **source row count must not be required to equal target row count**. The report must show source coverage, event fan-out rate, dedup rate, and quarantine rate together.

### 10.2 Checks that must pass

**Input completeness**

1. All 25 physical files and 40 logical sources enter the manifest with consistent hashes and shapes.
2. Partition MRN counts 5,633 / 8,623 / 8,247 / 8,468, union 22,982, has∩no 1,609, `29b_has`∩`29b_no` 927 all match the baselines.
3. All 5,633 people in `29_has` are inside `29b_has` (empty difference).
4. PFT sheet aliases, `29b_no`'s empty first column, `29b_has`'s medication admin as txt, and the inconsistent problem_list BOM — all four anomalies handled correctly.
5. Per-partition raw row counts for the three patients match the **partition-scoped** baselines in §2.5.

**Types and hashing (new in v0.5)**

6. `Demographics.Death_Date` normalizes to the same string in `29b_has` (`str`) and the other partitions (`datetime`).
7. `Outcome.Length_of_stay_days` normalizes to the same string in `29_no` (`str`) and the other partitions (`int`).
8. Cells with mixed types within one column are handled per value, not inferred from the first row.

**Semantic correctness**

9. `dos` is never written as the `event_time` of any lab/echo/ECG/PFT record.
10. `29b`'s date-only `dos` aligns correctly with `29`'s full timestamp at date granularity, and the lost time component is recorded.
11. `PT-B`'s anchor set has 4 members matching the local baseline file; cross-anchor duplicates produce exactly one canonical event while retaining all anchors and source links.
12. `PT-B` belongs to both `29b_has` and `29b_no`; both `CohortMembership` rows are retained and **not** merged into a patient-level label.
13. `has`/`no` never becomes an OMOP condition and never enters MEDS event rows.
14. `all_rx` and medication admin remain distinguishable by `event_kind`, type concept, and lineage.
15. Rows with a NULL `Ordering_Date` never produce a `DRUG_EXPOSURE` with a fabricated date.
16. Problem-list entries recorded after death are flagged `RECORDED_AFTER_DEATH` and not deleted.
17. Empty sheets yield coverage `unknown/not_extracted` and produce no negative facts.
18. Each of the 7 value forms in §5.4 has a test case; unparseable values go to quarantine rather than being coerced.

**Target-layer compliance**

19. No canonical / OMOP / MEDS row lacks lineage.
20. No non-zero concept ID is absent from the local vocabulary; concept domains are compatible with their target fields.
21. The birth-year policy cannot be bypassed without approval.
22. MEDS: one shard per patient, contiguous, time-sorted; every code appears in `codes.parquet`.
23. Splits are generated after cross-partition identity resolution and are mutually exclusive.
24. As-of datasets contain no event with `available_time > prediction_time`.

**Determinism**

25. Rerunning with the same input / config / mapping version yields the same data hash (excluding runtime-timestamp fields).
26. `--workers 1` and `--workers 4` yield the same data hash.
27. Killing a run midway and rerunning produces no duplicate events (content addressing + atomic writes).

**Generalization**

28. Core code contains no CTPE paths, file names, sheet names, column names, or PE label strings (grep test).
29. The synthetic `generic_ehr` fixture completes canonical → OMOP → MEDS without loading any CTPE configuration.
30. Column reordering, case changes, BOM presence, declared aliases, and missing optional columns do not change output semantics.

### 10.3 Three-patient fixtures

Store de-identified, small fixtures in the original format covering: field parsing, time priority, anchor deduplication, order/admin separation, post-death recording flags, representative ICD/lab/drug mappings, MEDS codes and times, and the corresponding OMOP rows with lineage.

Add a `ctpe_partition_overlap` fixture specifically covering `PT-B`'s cross-partition has/no conflict.

Fixtures cover rules only; **never copy the 8 GB of full data into the repository.**

### 10.4 External validation (optional, non-blocking)

- MEDS: validate all shards and metadata with the installed version's schema validator — **do this**, it is cheap.
- OMOP: the OHDSI DataQualityDashboard needs an R environment and PostgreSQL. Treat it as an optional later check, not a release gate.

---

## 11. Development process

A research project still needs discipline, but only the parts that pay for themselves:

- **git**: one branch per stage; commit messages state which contract changed.
- **Contracts first**: write and freeze the Pydantic models for `config.py` and the canonical schema before anything else; other modules interact only through them.
- **Tests**: every parsing rule has a unit test; every stage has a fixture-level integration test; write the failure-path test before the happy path.
- **Never commit**: PHI, `.env`, raw EHR samples, model weights, the MRN mapping table.
- **Semantic changes leave a trace**: changing identity keys, time priority, dedup rules, or label definitions means editing `datasets/*.yaml`, explaining it in the commit, and updating the affected fixture expectations.
- **Every stage leaves a run report**: `work/runs/<run_id>/report.json` with config hash, input file hashes, per-stage counts, and a quality-issue summary.

Not needed: a semver release process, CHANGELOG conventions, a conformance suite, multi-reviewer code review, or CI/CD pipelines.

---

## 12. Implementation order

Six stages; each ends with something runnable.

### Phase 0 — Contracts and skeleton

Freeze the schema of `datasets/ctpe.yaml`, the canonical event schema, and the `source_row_id` / canonical serialization rules. Build the repository skeleton and the three-patient fixtures. Turn every baseline in §2 into a test.

**Done when**: `ehr2cdm inspect --dataset ctpe` lists 25 files and 40 sources and reports the known blockers (timezone, `age_as_of_date`, has/no definition, whether CT reports exist).

### Phase 1 — Deterministic source → canonical

Three adapters; ingest + lineage; cross-partition identity; time semantics; value parsing; anchors and deduplication; quarantine.

**Done when**: all three-patient fixtures pass; `--workers 1` and `--workers 4` hash identically; the full dataset completes with a reconciliation report.

### Phase 2 — Minimal OMOP publication

Import the vocabulary; deterministic terminology mapping; PERSON/OBSERVATION_PERIOD/VISIT/CONDITION/DRUG/PROCEDURE/MEASUREMENT/NOTE/DEATH; lineage tables; the birth-year blocker policy.

**Done when**: every OMOP row for the three patients traces to source rows; unmapped items sit in the review queue rather than being silently zeroed.

### Phase 3 — MEDS publication

canonical → MEDS; code namespace and metadata; sharding; splits; leakage checks.

**Done when**: the MEDS schema validator passes and `available_time` semantics are covered by tests.

### Phase 4 — LLM assistance

The two uses (column semantics, terminology ranking); the review CSV loop; **and a measurement of whether the LLM actually helps** (compare human-acceptance rates across "deterministic baseline", "lexical recall", and "recall + LLM ranking"). If there is no measurable gain, delete the step.

### Phase 5 — Generalization proof and optional extensions

Run the synthetic `generic_ehr` fixture end to end; the no-hardcoding grep test; add narrative extraction and a task layer if needed.

---

## 13. References

Pin and archive the version and hash of every page/package/DDL used:

- [OHDSI OMOP Common Data Model](https://ohdsi.github.io/CommonDataModel/)
- [OMOP CDM FAQ (required tables, birth year, vocabulary mapping)](https://ohdsi.github.io/CommonDataModel/faq.html)
- [Medical Event Data Standard (MEDS)](https://github.com/Medical-Event-Data-Standard/meds)
- [OHDSI DataQualityDashboard](https://ohdsi.github.io/DataQualityDashboard/) (optional)
- [vLLM Structured Outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/)
- [DuckDB Parquet](https://duckdb.org/docs/stable/data/parquet/overview)
- [LOINC](https://loinc.org/about/) / [SNOMED CT](https://www.snomed.org/what-is-snomed-ct) / [RxNorm](https://www.nlm.nih.gov/research/umls/rxnorm/index.html)

---

## 14. Final judgment

Priority order:

```text
correct data contracts
  > row-level lineage and time semantics
  > deterministic terminology mapping
  > OMOP/MEDS compliance
  > the human confirmation loop
  > degree of LLM automation
```

Remove the LLM and the system must still convert data correctly. Add the LLM and it must still be able to state the provenance and approval record behind every conclusion. Onboarding a structurally similar EHR should be a matter of writing one new YAML; if it requires copying the core ETL, the design has failed.

**This is a research tool.** Its success criteria are "the results are trustworthy, someone else can reproduce them, and one person can understand it" — not "it survives production traffic". Any component added in the name of production-grade robustness must first answer: which real problem does it solve on these 8 GB?

---

## 15. Revision history

### v0.5 (2026-08-20)

**Factual corrections**

- The three-patient row counts in §2.5 were presented as patient-level baselines in v0.4 but are actually `29_has`-scoped values, contradicting the "read all four partitions" input definition. They are now split by partition, with measured `29b_has` / `29b_no` numbers added.
- Added the measured finding that `dos` is a full timestamp in `29` and a date only in `29b`, so anchors cannot be joined across batches by equality.
- Added the measured finding that `PT-B` has 4 CT anchors and belongs to both `29b_has` and `29b_no` — direct evidence that has/no is an episode-level label.
- Added the measured finding that `Demographics.Death_Date` and `Outcome.Length_of_stay_days` exhibit cross-workbook cell-type drift, which would break cross-batch deduplication. The canonical serialization rule in §5.1 was added because of it.
- Added the measured finding that 2 of the 21 txt files carry no BOM, correcting the earlier claim that all did.
- Added §5.4, the value parsing rules (ranges, comparators, sentinel text, signature lines).
- Fixed the broken path reference to the exploratory chain-building script.

**Simplifications (removed components and why)**

| Removed | Why |
|---|---|
| PostgreSQL work ledger, leases, heartbeats, fault injection, distributed backend | 8 GB on one machine; a process pool plus content-addressed artifacts is sufficient and gives simpler resumption semantics |
| Resource pool and backpressure framework | replaced by three integer config values |
| Three-tier Dataset/Domain/Task Pack with semver, CHANGELOG, conformance suite, scaffold | replaced by a single `datasets/<id>.yaml` versioned in git |
| Five sub-agent roles, skill registry, prompt version management | replaced by 2 necessary LLM uses and 3 git-tracked prompt files |
| Three-index RAG, pgvector, hybrid retrieval, 15-field chunk governance | terminology recall uses vocabulary SQL + lexical matching; document lookup uses ripgrep |
| Capability levels, onboarding state machine, Capability Report | replaced by the blocker list emitted by `inspect` |
| Container orchestration, health-check service, DB role model, secret management, security scanning process | single-machine research environment; document it in the README |
| `etl_audit`'s work_item / task_attempt / artifact / mapping_decision tables | removed with the execution engine; mapping decisions live in git-tracked CSVs |
| DataQualityDashboard as a release gate | downgraded to optional (needs R + PostgreSQL); the MEDS validator stays mandatory |
| Eleven milestones M0–M10 | collapsed to 6 phases |

**Core kept unchanged**: canonical as the single source of truth, row-level lineage, `dos` as anchor only, order ≠ administration, has/no is not a clinical fact, the LLM proposes but never writes, quarantine instead of guessing, deterministic reproducibility, and the three-patient regression.

### Patient identifiers

All patient references use the pseudonyms `PT-A` / `PT-B` / `PT-C`. Real MRNs and patient-level service dates are kept only in `tools/baselines.local.json`, which is git-ignored. See §2.5.
