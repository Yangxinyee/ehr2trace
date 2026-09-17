# EHR2Trace

EHR2Trace converts hospital EHR exports into two standard formats, **OMOP CDM 5.4** and
**MEDS 0.4**, from one canonical layer of patient events. Every published row links
back to the source rows it came from; event time and information-availability time are
stored separately; medication orders, dispensing and administration stay distinct
records; and a suite of 55 checks runs on the saved outputs rather than on the code
that produced them. Where the data cannot answer a question, the converter stops and
says so instead of substituting a default.

It is research software under the Apache-2.0 licence, built for preparing patient
histories for patient world models, clinical agents and offline reinforcement learning.
Episodes, rewards and models are downstream and not here. The companion paper is
*EHR2Trace: Auditable EHR Data Infrastructure for Patient World Models and Clinical
Agents*; see [CITATION.cff](CITATION.cff).

The repository carries the software, dataset configurations and synthetic fixtures. It
carries no patient data, no conversion output and no clinical vocabulary.

## How it works

```text
raw export (read-only)          delimited text, spreadsheets, or prepared Parquet
    ↓ ingest                    deterministic parsing, one lineage record per row
source/                         one Parquet per logical source, original values kept
    ↓ identity, canonical       patient identity, time semantics, values, units, dedup
canonical/                      the single event store both targets are derived from
    ↓                    ↓
omop/ (DuckDB)          meds/ (Parquet shards + metadata)
    ↓ validate                  55 checks on what was written, with lineage
```

Everything dataset-specific lives in one YAML file under `datasets/`. A test fails the
build if a hospital-specific string appears anywhere under `src/`, so a new export costs
a new configuration and no core code.

Four rules hold everywhere, each enforced by a test rather than promised:

1. A language model never parses dates, numbers or table structure, never invents a
   concept id, and never writes to a target layer. It may propose; a person confirms.
2. Every canonical, OMOP and MEDS record traces to a `source_row_id`.
3. Anything uncertain goes to `quarantine/` or the review queue. Nothing is guessed.
4. Same input, configuration and mapping version give the same output hash.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -e ".[dev]"
cp .env.example .env        # paths and options; .env is git-ignored
```

Python 3.11 or later. `requirements.lock` is the exact resolved environment; the MEDS
version is pinned because its schema is a data contract. The deterministic path needs
CPU cores and memory in proportion to the export (MIMIC-IV converts on 48 cores and
251 GB; the demonstration subset below runs on a laptop) and no GPU.

## Try it on open data

The MIMIC-IV demonstration subset (100 patients) is openly licensed and needs no
credential. `datasets/mimiciv.yaml` runs against it unchanged; the emergency-department
and note sources it declares simply report as not present.

```bash
wget -r -N -c -np -nH --cut-dirs=1 -P data https://physionet.org/files/mimic-iv-demo/2.2/
python3 tools/prepare_mimiciv.py --mimic-root data/mimic-iv-demo/2.2 --out prepared/mimiciv
export MIMICIV_DATA_ROOT=$PWD/prepared EHR_WORK_ROOT=$PWD/work OFFLINE_MODE=1
for step in inspect ingest identity canonical omop meds validate; do
  .venv/bin/ehr2trace $step -d datasets/mimiciv.yaml
done
```

This takes a few minutes. `validate` prints one line per check and a summary; a check
with nothing to examine is reported as skipped, not passed. Without a vocabulary every
concept id is 0 and every term is queued for review, which the report says. With one
installed (below), a single check fails on MIMIC-IV by design: a few laboratory codes
mix unit spellings, and the finding is reported rather than tuned away. The same
conversion runs in CI on every push.

## Converting your own export

1. **Describe the export** in a YAML file under `datasets/`. Start from
   `datasets/generic_ehr.yaml` (a synthetic two-site export) and see
   [CONTRIBUTING.md](CONTRIBUTING.md). The three real configurations, `mimiciv.yaml`,
   `ctpe.yaml` and `cu_ctpa.yaml`, show a relational database, a multi-partition
   extract and a study export; the last two describe private data and are included as
   worked examples only.
2. **Prepare if needed.** A normalized database is flattened first so that lineage
   stays row-level: `tools/prepare_mimiciv.py`, `tools/prepare_cu.py` and
   `tools/prepare_ctpe.py` write one Parquet per source plus a manifest of input and
   output hashes, rows added or dropped, and every delivered file they did not carry.
3. **Look before converting.** `ehr2trace inspect` reads no data; it lists the files it
   found, lines them up against the configuration, and prints the *blockers*: questions
   only the data owner can answer, such as the source timezone or the reference date
   for an age. It exits non-zero while any remain open. Answer them in the YAML.
4. **Convert and check.**

```bash
export EHR_WORK_ROOT=/path/outside/the/repo      # holds PHI; never inside the checkout
.venv/bin/ehr2trace ingest    -d datasets/yours.yaml --workers 12
.venv/bin/ehr2trace identity  -d datasets/yours.yaml
.venv/bin/ehr2trace canonical -d datasets/yours.yaml --workers 12
.venv/bin/ehr2trace omop      -d datasets/yours.yaml     # OMOP_VOCAB_DIR for real concept ids
.venv/bin/ehr2trace meds      -d datasets/yours.yaml
.venv/bin/ehr2trace validate  -d datasets/yours.yaml
.venv/bin/ehr2trace trace     -d datasets/yours.yaml --patient <key>   # one patient, every layer
```

Outputs are content-addressed by input hash, configuration hash and code version, so a
rerun reuses whatever already exists for identical inputs; `ehr2trace clean` removes
artifacts whose address no longer matches. `ehr2trace report` prints a run report.

Under `EHR_WORK_ROOT/<dataset>/`:

| Directory | Contents |
|---|---|
| `manifest/` | input files with hashes, the blocker list |
| `source/` | one Parquet per logical source, original values, `source_row_id` |
| `identity/` | patient key to `subject_id` map (one-way hash, owner-only permissions) |
| `canonical/` | events, event-to-source links, anchors, quality issues, quarantine |
| `omop/` | `omop.duckdb`, CDM 5.4 tables plus `etl_audit` lineage |
| `meds/` | `data/<split>/*.parquet` shards and `metadata/` |
| `review/` | `pending.csv` proposals and `decisions.csv` |
| `runs/` | run reports and `validation.json` |

## Validation

The 55 checks cover source rows and links, patient identity and time, the OMOP and MEDS
outputs, and the review records. Cross-layer checks compare what was published with
information stored apart from it: merged rows against their source rows, values and
units against reference tables, published deaths against the deaths the canonical layer
holds, MEDS concepts against OMOP concepts, and the delivery against what the
configuration says it reads. Thresholds and expected exceptions are declared in the
configuration, so a known gap is reported with its measurement instead of failing.

Two experiments guard the suite itself and run in CI on a PHI-free fixture:

```bash
.venv/bin/pytest tests/integration/test_fault_detection.py    # 28 silent faults, each must be caught
.venv/bin/pytest tests/integration/test_reproducibility.py    # four concurrency settings, one digest
```

[docs/FAULT_CATALOGUE.md](docs/FAULT_CATALOGUE.md) says what each fault is for.

## Terminology

Concept ids come from an OMOP vocabulary you obtain yourself from
<https://athena.ohdsi.org> (SNOMED, LOINC, RxNorm, ICD, NDC, UCUM and the standard type
vocabularies; add CPT4, which needs a UMLS account, only if your export codes procedures
with it). Unzip it, then:

```bash
python3 tools/check_vocabulary.py /path/to/vocab      # tables present, vocabularies included, expected coverage
export OMOP_VOCAB_DIR=/path/to/vocab
```

Lookup is deterministic: exact codes, a punctuation-insensitive pass accepted only when
it is unambiguous, retired codes followed to their current standard concept, and drug
names matched by ingredient, strength and dose form. Terms that do not resolve enter
`review/pending.csv`:

```bash
.venv/bin/ehr2trace propose -d datasets/yours.yaml --kind terminology   # -> review/pending.csv
# a person records decisions in review/decisions.csv
.venv/bin/ehr2trace compile -d datasets/yours.yaml                      # -> mappings/*.csv
.venv/bin/ehr2trace omop    -d datasets/yours.yaml                      # republish with them
```

`mappings/` is the only thing that turns a source string into a concept id, and its
only writer is `compile`, which reads accepted decisions; see
[mappings/README.md](mappings/README.md). An optional local model can rank candidates
for a reviewer (`--llm`); `ehr2trace measure` reports whether that ranking helps against
a set of confirmed decisions, and [docs/LLM_BENEFIT.md](docs/LLM_BENEFIT.md) records one
such measurement.

## Experiments

The scripts behind the paper's measurements live in [`experiments/`](experiments/README.md)
with the aggregate records they wrote: fault detection and reproducibility on the
fixture, the OHDSI DQD comparison, the converter comparison on the MIMIC-IV
demonstration subset, the temporal-leakage experiments, the stage timings, and the
terminology-ranking measurements. Each script says what it needs and where it writes.

## Tests

```bash
.venv/bin/python -m pytest -q                       # unit and fixture tests, no patient data
EHR_DATA_ROOT=... EHR_WORK_ROOT=... .venv/bin/python -m pytest -q   # adds real-data baselines
```

Real-data tests skip when the export is absent. No test contains a real identifier or
patient-level date; the fixtures under `tests/fixtures/` are fabricated and reproduce the
structural traps of real exports without their content. CI runs the tests, the
hospital-string guard, fault detection, reproducibility, and the MIMIC-IV demo
conversion.

## Documents

| File | Contents |
|---|---|
| [`PATIENT_CDM_AGENT_SYSTEM_DESIGN.md`](PATIENT_CDM_AGENT_SYSTEM_DESIGN.md) | Design specification: architecture, data contracts, OMOP and MEDS mapping, validation, model boundary |
| [`EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md`](EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md) | The implementation checklist the code and tests cite by item number |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Decisions taken where the specification was silent or the data disagreed with it |
| [`docs/FAULT_CATALOGUE.md`](docs/FAULT_CATALOGUE.md) | The injected faults and the incident behind each |
| [`docs/LLM_BENEFIT.md`](docs/LLM_BENEFIT.md) | Whether model-assisted ranking helps, measured |
| [`docs/standards/`](docs/standards/README.md) | Searchable copies of the OMOP CDM 5.4 field list and the MEDS schema |
| [`mappings/README.md`](mappings/README.md) | The rules for the terminology registry |
| [`experiments/README.md`](experiments/README.md) | The paper's experiments and their recorded results |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Adding an export, a check or a fault |

## Data sensitivity

- The raw export is read-only; everything is written under `EHR_WORK_ROOT`, which holds
  PHI and must live outside the repository.
- `OFFLINE_MODE=1` is the default; the model client refuses a non-local endpoint.
- `subject_id` is a one-way hash of the patient key, salted by `EHR_SUBJECT_SALT` if set.
  Neither OMOP nor MEDS carries a direct identifier.
- No PHI, `.env`, raw sample, output or MRN mapping may be committed. `.gitignore` names
  the files that would hold them.
- Concept names from SNOMED CT, LOINC and RxNorm are licensed by their owners; the
  repository ships no vocabulary and no output. [NOTICE](NOTICE) lists third-party
  components.

## Citation

See [CITATION.cff](CITATION.cff).
