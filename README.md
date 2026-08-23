# ehr-to-omop-meds

Deterministically convert local hospital EHR exports (txt + xlsx) into two standard
formats, **OMOP CDM 5.4** and **MEDS**, with an LLM proposing only where semantic
judgment is genuinely required — and a human confirming every proposal.

**A research converter, not a production system.**

## What it does

```text
raw txt/xlsx (read-only)
    ↓ deterministic parsing + row-level lineage
source/            one Parquet per logical source, original values preserved
    ↓ deterministic normalization + deduplication
canonical/         canonical event store — the single source of truth
    ↓                    ↓
omop/              meds/
```

Measured on the reference export (8 GB, 4 partitions, 25 files, 40 logical sources),
from a clean work root, with 30/30 validation checks passing:

| | |
|---|---:|
| Source rows read | 73,558,874 |
| Canonical events | 31,669,480 |
| Event ↔ source-row links | 100,103,655 |
| Events built from more than one source row | 15,412,302 |
| Subjects, resolved across all partitions | 22,982 |
| Subjects appearing in more than one partition | 6,784 |
| Extraction anchors (kept out of the event stream) | 35,247 |
| Rows quarantined rather than guessed at | 637,665 |
| Rows that carried no fact at all | 1,949 (0.0026%) |
| Records dated after death, flagged and kept | 62,067 |
| MEDS shards / distinct codes | 22,980 / 50,352 |
| Distinct terms resolved by the vocabulary | 24,754 of 59,447 (41.6%) |
| Distinct standard concepts actually used | 9,630 |
| OMOP clinical rows published | 31,555,608 |

Wall time on 48 cores: ingest 136s at 0.8 GB peak, canonical 424s at 32.5 GB peak.
No GPU is used anywhere in that path.

With an OMOP vocabulary installed and the age reference date approved, all three layers
publish. Without either, the pipeline still runs and says exactly what it is missing —
see the blocker table below.

## Four rules that are never violated

1. The LLM does not parse dates, numbers, or table structure; it does not invent
   concept IDs; it does not write to any target layer.
2. Every canonical / OMOP / MEDS record traces back to a `source_row_id`.
3. Anything uncertain goes to `quarantine/` or `review.csv`. Never guess.
4. Same input + config + mapping version → same output hash.

Each of these is a test, not a promise: see `tests/test_no_hardcoded_dataset_strings.py`,
the lineage checks in `src/ehr2cdm/validate.py`, and
`tests/integration/test_generic_ehr_pipeline.py::test_one_worker_and_four_workers_agree`.

## Documents

| File | Contents |
|---|---|
| [`PATIENT_CDM_AGENT_SYSTEM_DESIGN.md`](./PATIENT_CDM_AGENT_SYSTEM_DESIGN.md) | Design spec v0.5: input facts, architecture, data contracts, OMOP/MEDS mapping, validation requirements |
| [`EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md`](./EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md) | Implementation checklist v0.2: six phases (P0–P5) of tickable tasks |
| [`datasets/ctpe.yaml`](./datasets/ctpe.yaml) | The entire dataset contract. Every hospital-specific string lives here and nowhere else |
| [`tools/verify_doc_baselines.py`](./tools/verify_doc_baselines.py) | Re-derives every number in design §2 from the raw data and compares |

## Reproducing a full run from scratch

### 1. Environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -e ".[dev]"
cp .env.example .env    # then edit it
```

`requirements.lock` is the exact resolved environment. The MEDS pin is the one that
matters most: its schema is a data contract, and tracking `main` would mean the
definition of a valid output could change under a rerun that is supposed to be
byte-identical.

`EHR_DATA_ROOT` is the read-only raw export. `EHR_WORK_ROOT` is where everything is
written; it holds PHI and must live outside this repository. Reserve 5–10× the raw
size — the reference export produces about 20 GB.

Hardware: the deterministic path needs 8–16 cores and 32 GB RAM and no GPU at all. The
optional LLM assistance needs one GPU serving a local OpenAI-compatible endpoint.

### 2. Look before converting

```bash
.venv/bin/ehr2cdm inspect --dataset ctpe
```

Lists every file with its hash, lines the physical layout up against the declared
sources, and prints the **blockers** — the questions that cannot be answered from the
data and must not be guessed. It exits non-zero while any remain open. That is
intentional: an open blocker is information, not an error to route around.

### 3. Convert

```bash
.venv/bin/ehr2cdm ingest    --dataset ctpe --workers 12
.venv/bin/ehr2cdm identity  --dataset ctpe
.venv/bin/ehr2cdm canonical --dataset ctpe --workers 12 --assume-timezone America/New_York
.venv/bin/ehr2cdm omop      --dataset ctpe
.venv/bin/ehr2cdm meds      --dataset ctpe
.venv/bin/ehr2cdm validate  --dataset ctpe --all
```

`--assume-timezone` exists because the source timezone is an open blocker. It records
an explicit operator assumption in the run report and flags every converted event with
`TZ_ASSUMED`. Without it the canonical stage refuses to run rather than silently
adopting the developer machine's zone. Once the data owner answers, put the zone in
`datasets/ctpe.yaml` and the flag disappears.

Re-running is safe and cheap: outputs are content-addressed by input hash + config hash
+ code version, so anything already computed from identical inputs is reused. That is
the whole resumption mechanism — there is no ledger.

### 4. Ask where a record came from

```bash
.venv/bin/ehr2cdm trace --dataset ctpe --patient <key>
```

Prints one patient's path through every layer: source rows per partition, what was
quarantined and why, canonical events by kind, anchors, cohort membership — and for a
few sampled events, every source row behind them by file and line number. On the
reference export a single lab measurement typically resolves to six source rows across
three partitions, which is anchor duplication and cross-batch duplication collapsing
into one event with all six still traceable.

It prints a real patient key, so use it on a terminal you would be willing to show the
data owner.

### 5. Install a vocabulary (optional, but it is what makes concept ids real)

Without one, every `*_concept_id` is 0, every source value is preserved, and all 59,447
distinct terms sit in the review queue. Getting one is a licensing exercise, not a
technical one:

1. Apply for a **UMLS licence** at <https://uts.nlm.nih.gov/uts/signup-login> — free for
   research, but approval takes time, so start here.
2. Register at <https://athena.ohdsi.org/> and use its Download tab to request a bundle
   containing at least **SNOMED, ICD10CM, LOINC, RxNorm, RxNorm Extension, UCUM** plus
   the default type/gender/race vocabularies. Athena emails a link when the build is
   ready. CPT4 is not needed here and costs an extra Java reconstitution step.
3. Unzip it (the `.csv` files are tab-delimited despite the extension), then check it
   before trusting it:

```bash
EHR_WORK_ROOT=... python3 tools/check_vocabulary.py /path/to/unzipped/vocab
export OMOP_VOCAB_DIR=/path/to/unzipped/vocab
.venv/bin/ehr2cdm omop --dataset ctpe
```

`check_vocabulary.py` answers the three questions that matter before a single row is
mapped: are the required tables present and readable, which vocabularies did the bundle
actually include, and how much of *this* dataset's terminology would map with it. A
truncated file and a bundle missing a vocabulary both look identical downstream — like
having no vocabulary at all — so they are worth catching up front.

### 6. Terminology and review

```bash
.venv/bin/ehr2cdm propose --dataset ctpe --kind terminology   # -> review/pending.csv
# a human edits review/decisions.csv in any tool
.venv/bin/ehr2cdm compile --dataset ctpe                      # -> mappings/*.csv
.venv/bin/ehr2cdm omop    --dataset ctpe                      # rerun with the new mappings
```

`mappings/` is the only thing that can turn a source string into a concept id, its only
writer is `compile`, and `compile` only reads decisions a human accepted.

The queue shrinks as well as grows. When a rerun maps a term without human help, the
`omop` build marks that row `resolved` and it drops out of the open queue -- the row
itself stays, so ids remain stable and an earlier decision is still traceable. Only
`omop` may retire items, because it is the one caller that sees the complete unmapped
set; `propose --limit` looks at a subset and must leave the rest alone.

Add `--llm` to have a local model rank the recalled candidates. Whether that is worth
doing is a measurement, not an opinion:

```bash
.venv/bin/ehr2cdm measure --dataset ctpe --from-decisions
```

It compares deterministic lookup / plus lexical recall / plus model ranking against the
human gold set and prints a verdict. If the ranking step shows no gain, delete it.

## Known blockers on the reference export

These are reported by `inspect`, recorded in every run report, and none of them are
worked around:

| Blocker | Consequence |
|---|---|
| ~~Source timezone undeclared~~ | **Answered 2026-08-22**: US Eastern, recorded as `America/New_York` so daylight-saving transitions apply per timestamp. `TZ_ASSUMED` is gone from all 31.7M events |
| ~~No reference date for `Age`~~ | **Answered 2026-08-22**: 2025-03-01, with an approval note. Every derived birth year is flagged `DERIVED_APPROXIMATE_BIRTH_YEAR`. Note the data ends 2024-07-08 and was exported 2024-09-19, both before that date — recorded in the config next to the policy |
| Cohort label rule and episode binding undefined | The label stays provenance in the audit layer; it is not a clinical fact and not a training target |
| ~~Batch relationship unconfirmed~~ | **Answered 2026-08-21**: one cohort exported twice, take the union — which is what the pipeline already did |
| Imaging reports not in the delivery | No imaging conclusion is synthesized from an anchor date or a directory name |
| ~~No licensed vocabulary~~ | **Installed 2026-08-22** (Athena v5.0 27-FEB-26). 41.6% of distinct terms map deterministically; the rest are in the review queue with their source values preserved |

The second one is worth being explicit about: under the default `strict` birth-year
policy, a patient with no derivable `year_of_birth` is withheld from OMOP **entirely** —
not just from `PERSON`. Clinical rows pointing at a person who was never published are
not a CDM instance. The synthetic fixture (which legitimately has an age reference date
and an approval note) exercises the full OMOP path end to end.

## Generalization

A structurally different EHR should cost one new YAML and no core code. That claim is
tested rather than asserted: `tests/fixtures/generic_ehr/` is a synthetic two-site
export sharing no structure with the reference data — comma-separated, different column
names for every role, no anchor concept at all — and it converts end to end through
`datasets/generic_ehr.yaml`. A grep test forbids hospital-specific strings and hardcoded
concept ids anywhere in `src/`.

## Verifying the numbers in the design spec

Section 2 of the design spec claims every input fact was measured. That claim is
checkable:

```bash
EHR_DATA_ROOT=/path/to/ehr-export python3 tools/verify_doc_baselines.py
```

About one minute, 55 checks. A non-zero exit code means the data or the document has
drifted — in that case report the drift and investigate the data, **do not edit the
expected values in the document**. The same baselines are asserted from the converter's
own output in `tests/integration/test_ctpe_baselines.py`.

## Tests

```bash
.venv/bin/python -m pytest              # unit + fixture integration, seconds
EHR_DATA_ROOT=... EHR_WORK_ROOT=... .venv/bin/python -m pytest   # adds the real-data baselines
```

Real-data tests skip cleanly when the export is not present.

## Data sensitivity

The reference dataset is a semi-processed limited data set that **contains real PHI**.
It is not de-identified.

- The raw directory is read-only; all outputs go to `EHR_WORK_ROOT`.
- `OFFLINE_MODE=1` by default; the LLM client refuses a non-local endpoint.
- No PHI, `.env`, raw samples, or MRN mapping table may appear in this repository.
- `subject_id` is a one-way hash of the patient key, salted by `EHR_SUBJECT_SALT` if
  set. The mapping table lives under `EHR_WORK_ROOT/identity/` with owner-only
  permissions. Neither OMOP nor MEDS carries a direct identifier.

**Patient identifiers are pseudonymized.** Documents and scripts refer only to `PT-A` /
`PT-B` / `PT-C`. Real MRNs and patient-level service dates live in
`tools/baselines.local.json`, which is git-ignored. On a machine that has the raw data,
generate it once before running the verifier:

```bash
python3 tools/make_local_baselines.py PT-A=<MRN> PT-B=<MRN> PT-C=<MRN>
```
