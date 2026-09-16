# EHR2Trace

Deterministically convert local hospital EHR exports (txt + xlsx) into two standard
formats, **OMOP CDM 5.4** and **MEDS**, with an LLM proposing only where semantic
judgment is genuinely required — and a human confirming every proposal.

**A research converter, not a production system.**

This is source-linked conversion plus executable checks, for data intended for patient
world models, clinical agents, and offline reinforcement learning. Task-specific
episodes, rewards, world models and agent policies are downstream work and are not here.
The [readiness audit and development priorities](docs/WORLD_MODEL_READINESS.md) record
the visibility and action-semantics gaps in the current outputs.

The repository carries the software, its configuration and synthetic fixtures. It
carries no patient data, no conversion output and no clinical vocabulary; see
[Data sensitivity](#data-sensitivity).


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

Historical reference run (8 GB, 4 partitions, 25 files, 40 logical sources),
from a clean work root, with the then-current 34/34 validation checks passing:

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

The same delivery after the 2026-09 conversion remediation, with the now-current 55
checks (53 pass, 1 skip, and `VISIT_CONCEPT_COVERAGE` left failing on purpose while the
ADT department names are reviewed):

| | |
|---|---:|
| Source rows read | 75,113,066 |
| Canonical events | 33,396,779 |
| Event ↔ source-row links | 101,522,252 |
| Events built from more than one source row | 15,691,827 |
| Subjects, resolved across all partitions | 22,982 |
| Subjects appearing in more than one partition | 6,784 |
| Extraction anchors (kept out of the event stream) | 35,247 |
| Source rows quarantined rather than guessed at | 629,543 |
| Rows that carried no fact at all | 240,789 |
| Records dated after death, flagged and kept | 66,477 |

The event count rises because the remediation stopped merging orders that differ in
dose or status, published ICU transfers as visit details, and read sources the earlier
run left unread. Wall times are deliberately omitted: the three datasets were rebuilt
concurrently on one machine, so their clock times are not a benchmark.

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
the lineage checks in `src/ehr2trace/validate.py`, and
`tests/integration/test_generic_ehr_pipeline.py::test_one_worker_and_four_workers_agree`.

## What a record becomes, and where that is decided

The conversion remediation of September 2026 changed how records become events. The
[plan](docs/CONVERSION_REMEDIATION_PLAN.md) records the problems, and
[DECISIONS](docs/DECISIONS.md) records the eighteen decisions. None of these rules names
a dataset in `src/`: each dataset's YAML declares what applies to it.

- **Two records are one event only if they state the same fact.** The identity of a
  drug order, dispensing or administration includes its dose (read as a number and a
  unit), route, status and end time. When the rows behind one event still disagree on
  a field, the YAML names the rule in `merge_rules` (`earliest`, `latest`,
  `null_and_flag`, `priority`, `prefer_linked` or `keep_all_flag`), and every row stays
  in the lineage. A disagreement that no rule covers is flagged `MERGE_CONFLICT` and
  fails `DUPLICATES_AGREE`. Two availability times for one result merge to the earlier
  one, flagged `AVAILABILITY_MERGED`.
- **Units are normalized beside the source values, never over them.** Canonical events
  and MEDS carry `value_number_normalized` and `unit_normalized`, converted only by the
  exact rules in `reference/unit_conversions.csv`.
  - A unit declared or overridden for a source code is flagged `UNIT_DECLARED` or
    `UNIT_OVERRIDDEN`.
  - A value outside the range in `reference/plausible_ranges/` keeps its source value,
    has no normalized value, and is flagged `IMPLAUSIBLE`.
  - A code that the YAML says mixes units with no exact conversion becomes one code per
    unit.
  - OMOP's `unit_concept_id` is looked up rather than written as 0, and
    `dose_unit_source_value` falls back to the event's own unit.
- **A stay inside a visit is a visit detail.** Transfers, service changes and ICU stays
  are published to `VISIT_DETAIL` under the visit that contains them. A detail with no
  parent visit stays in canonical and MEDS and is withheld from OMOP as
  `VISIT_DETAIL_UNPARENTED`, because the CDM cannot hold a detail of no visit.
- **Death dates are compared in the dataset's time zone.** Two records of a death on the
  same local day become one death with the more precise time (`DEATH_TIME_MERGED`).
  Records on different days are both kept, flagged `DEATH_DATE_CONFLICT`, and not
  published to `DEATH`.
- **Rates, actions and destinations have columns.** MEDS carries `rate`, `rate_unit`,
  `action` and `discharged_to`. OMOP has no rate column, so `drug_exposure.sig` carries an
  infusion rate as `<dose text>; rate <rate> <unit>`.
- **Nothing delivered is silently unread.** Every file and column is read, kept, or
  declared unread in the YAML with a reason (`ignored_columns`, `out_of_scope`), and
  `RAW_COVERAGE_DECLARED` checks that.

## Documents

| File | Contents |
|---|---|
| [`PATIENT_CDM_AGENT_SYSTEM_DESIGN.md`](./PATIENT_CDM_AGENT_SYSTEM_DESIGN.md) | Design spec v0.5: input facts, architecture, data contracts, OMOP/MEDS mapping, validation requirements |
| [`EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md`](./EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md) | Implementation checklist v0.2: six phases (P0–P5) of tickable tasks |
| [`datasets/ctpe.yaml`](./datasets/ctpe.yaml) | The entire dataset contract. Every hospital-specific string lives here and nowhere else |
| [`tools/verify_doc_baselines.py`](./tools/verify_doc_baselines.py) | Re-derives every number in design §2 from the raw data and compares |

## Try it on open data first

The MIMIC-IV demonstration subset is 100 patients under the Open Database Licence and
needs no PhysioNet credential. It carries every `hosp` table the full release does,
`emar` and `emar_detail` included, so `datasets/mimiciv.yaml` runs against it unchanged
— the ED and note sources are optional and simply report as not extracted.

```bash
wget -r -N -c -np -nH --cut-dirs=1 -P data https://physionet.org/files/mimic-iv-demo/2.2/
python3 tools/prepare_mimiciv.py --mimic-root data/mimic-iv-demo/2.2 --out prepared/mimiciv
export MIMICIV_DATA_ROOT=$PWD/prepared EHR_WORK_ROOT=$PWD/work OFFLINE_MODE=1
for step in inspect ingest identity canonical omop meds validate; do
  ehr2trace $step -d datasets/mimiciv.yaml
done
```

About a minute and 16 MB of download for 227,614 canonical events over 100 subjects, and
`validate` reports 32 passed, 6 skipped, 0 failed. The six skips are checks with nothing
in this dataset to examine — anchors, cohort membership, approximate birth years and,
without a vocabulary installed, terminology coverage. This is also a CI job, so the
MIMIC dataset contract is tested against real MIMIC on every push rather than only
against a fabricated fixture.

`tools/make_trace_table.py --work work/mimiciv --out trace.tex` then draws two subjects'
published events out of that build.

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
.venv/bin/ehr2trace inspect --dataset ctpe
```

Lists every file with its hash, lines the physical layout up against the declared
sources, and prints the **blockers** — the questions that cannot be answered from the
data and must not be guessed. It exits non-zero while any remain open. That is
intentional: an open blocker is information, not an error to route around.

### 3. Convert

Two of the tables `datasets/ctpe.yaml` reads come from the second delivery,
`All_kinds/`: the most recent follow-up contact, and the ADT department stays, which
become visit details (D-R12, D-R14). `tools/prepare_ctpe.py` projects them into the
partition layout first. Before writing anything, it compares a format fingerprint of the
four cohort groups, so that a difference in how the groups were exported cannot stand in
for the label, and it stops if they differ:

```bash
python3 tools/prepare_ctpe.py --all-kinds-root /path/to/All_kinds --out /path/to/ctpe_prepared
export CTPE_PREPARED_ROOT=/path/to/ctpe_prepared
```

Then convert:

```bash
.venv/bin/ehr2trace ingest    --dataset ctpe --workers 12
.venv/bin/ehr2trace identity  --dataset ctpe
.venv/bin/ehr2trace canonical --dataset ctpe --workers 12 --assume-timezone America/New_York
.venv/bin/ehr2trace omop      --dataset ctpe
.venv/bin/ehr2trace meds      --dataset ctpe
.venv/bin/ehr2trace validate  --dataset ctpe --all
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
.venv/bin/ehr2trace trace --dataset ctpe --patient <key>
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
   containing at least **SNOMED, ICD10CM, ICD9CM, ICD10PCS, ICD9Proc, LOINC, RxNorm,
   RxNorm Extension, NDC, UCUM** plus the default type/gender/race vocabularies. Athena
   emails a link when the build is ready. Add **CPT4** if you are converting the
   Colorado export: 815,466 of its 816,628 procedure rows carry one, and without it that
   whole domain maps to nothing. It is the one vocabulary Athena cannot ship complete,
   because the AMA licenses the code *names* separately, so the bundle arrives with
   18,403 nameless CPT4 rows plus a `cpt4.jar` that fills them in from your UMLS account:

   ```bash
   cp CONCEPT.csv CONCEPT.csv.bak     # the jar rewrites it in place
   ./cpt.sh <your UMLS API key>
   ```

   The reference export and MIMIC-IV need none of this; neither carries a CPT4 code.

   Ask for the whole list even when the dataset in front of you declares fewer code
   systems than that. An absent vocabulary is indistinguishable downstream from a code
   that is genuinely unmappable: both leave `concept_id` unset and both land in the
   review queue, so the cost of omitting one is paid by a person reading terms no
   person should have been shown. Two concrete cases from MIMIC-IV: it spans both ICD
   eras and codes its procedures, so a bundle carrying ICD10CM alone put **24,054**
   billing codes into the queue purely because ICD9CM, ICD9Proc and ICD10PCS were not
   in it; and its `prescriptions` table carries an 11-digit NDC on 87% of rows, which
   without the NDC vocabulary stays a free-text drug name and leaves the entire drug
   domain at zero mapped concepts.
3. Unzip it (the `.csv` files are tab-delimited despite the extension), then check it
   before trusting it:

```bash
EHR_WORK_ROOT=... python3 tools/check_vocabulary.py /path/to/unzipped/vocab
export OMOP_VOCAB_DIR=/path/to/unzipped/vocab
.venv/bin/ehr2trace omop --dataset ctpe
```

`check_vocabulary.py` answers the three questions that matter before a single row is
mapped: are the required tables present and readable, which vocabularies did the bundle
actually include, and how much of *this* dataset's terminology would map with it. A
truncated file and a bundle missing a vocabulary both look identical downstream — like
having no vocabulary at all — so they are worth catching up front.

### 6. Terminology and review

```bash
.venv/bin/ehr2trace propose --dataset ctpe --kind terminology   # -> review/pending.csv
# a human edits review/decisions.csv in any tool
.venv/bin/ehr2trace compile --dataset ctpe                      # -> mappings/*.csv
.venv/bin/ehr2trace omop    --dataset ctpe                      # rerun with the new mappings
```

`mappings/` is the only thing that can turn a source string into a concept id, its only
writer is `compile`, and `compile` only reads decisions a human accepted.

The queue shrinks as well as grows. When a rerun maps a term without human help, the
`omop` build marks that row `resolved` and it drops out of the open queue -- the row
itself stays, so ids remain stable and an earlier decision is still traceable. Only
`omop` may retire items, because it is the one caller that sees the complete unmapped
set; `propose --limit` looks at a subset and must leave the rest alone.

OMOP asks every clinical row for a `*_type_concept_id` saying what kind of record it
came from. `mappings/type_concepts.csv` carries those decisions, so a drug row published
from an order is marked `EHR prescription` and one published from an administration
record is marked `EHR administration record` -- the order/administration distinction
survives into the CDM field meant for it, not only into the canonical layer.

A code whose concept the vocabulary has since retired still resolves, through the
`Maps to` the vocabulary keeps for exactly that purpose: an NDC leaves the market and an
ICD-10-CM code is split at a fiscal-year boundary, but the record was written with the
code it was written with. Nothing withdrawn is published -- only the relationship to a
current standard concept is followed -- and `term_map.path` says `..._retired` so the
set is reviewable. Requiring the source concept to be current had left 3,036,972
MIMIC-IV prescriptions and 7,588 diagnoses across two datasets with no concept at all.
Where a code maps to several standard concepts the event's domain picks the one that
goes in its column, and the others are published as the additional rows OMOP expects of
a combination code.

Drug names are the exception to needing a person at all, because they are not free text.
A hospital writes `OXYCODONE 5 MG TABLET`, and the vocabulary says the same three things
about `oxycodone hydrochloride 5 MG Oral Tablet` -- with the strength as a *number* in
`DRUG_STRENGTH`. So `ehr2trace.drug_match` matches by ingredient, strength and dose form
rather than by text similarity, deterministically and only when exactly one standard
concept fits all three -- or, for a name that states an ingredient and nothing else, the
ingredient. On this export that settles 9,850 of 26,629 medication names and takes drug
coverage from 23.7% to 86.3%; measured against 139 mappings a physician had already
confirmed, it reproduces 93.8% of them exactly and the rest as the same drug at the same
strength under another spelling, with no case of a different drug. The names it cannot
settle -- compounded infusions, multi-ingredient solutions the vocabulary has under no
name the source uses, strengths stated as an element, a number it did not read -- still
go to a person.

```bash
python3 tools/measure_drug_match.py --vocabulary "$OMOP_VOCAB_DIR" \
    --audit "$EHR_WORK_ROOT/ctpe/review/pending.csv"       # -> results/drug_match.json
```

Add `--llm` to have a local model rank the recalled candidates. Whether that is worth
doing is a measurement, not an opinion:

```bash
.venv/bin/ehr2trace measure --dataset ctpe --from-decisions
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
| ~~No licensed vocabulary~~ | **Installed 2026-08-22** (Athena v5.0 27-FEB-26). 81.9% of published rows carry a standard concept; the rest are in the review queue with their source values preserved |

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

It is also tested against two more real datasets. `datasets/mimiciv.yaml` converts
MIMIC-IV v3.1 and v2.2 notes with no MIMIC-specific conversion code. Since the 2026-09
remediation it reads the `icu` module beside `hosp` and `ed`, 33 sources in all. The
figures below are from that remediated build:

| | |
|---|---:|
| Source rows | 836,795,494 |
| Canonical events | 799,153,396 |
| Event ↔ source-row links | 801,049,552 |
| Subjects | 364,673 |
| Persons published to OMOP | 364,627 |
| MEDS shards | 364,673 |
| Rows quarantined rather than guessed at | 1,074,435 |
| Validation | 48 passed, 6 skipped, 1 failed |

The six skips are honest ones: MIMIC has no extraction anchors and no cohort
partitions, so four checks have nothing to examine; its birth years come from the data
rather than from an age, so the fifth has nothing to recompute; and no source declares
which of its statuses are excluded, so the sixth has nothing to judge. The one failure
is left failing on purpose: twelve laboratory codes mix unit families, six of them
genuinely — international units beside arbitrary units, `g/dL` beside `%` — and every
repair needs either a reference table the whole canonical layer is addressed by or a
configuration change that re-ingests the dataset, so the finding is reported rather
than tuned away. Because MIMIC is a normalized relational database and a
hospital extract is not, `tools/prepare_mimiciv.py` denormalizes it first, using
projections and lookup joins, asserting that no join changes cardinality, and
writing a manifest of input and output hashes so lineage is unbroken across that step.
Two exceptions are declared per output rather than buried. A wide row of
emergency-department vital signs or triage values becomes one row per value, counted as
`rows_added_by_split`. The microbiology table drives two outputs, cultures and their
susceptibilities. An administration or pharmacy row that names no drug takes the name
of the prescription with the same `pharmacy_id`, and is marked as having done so
(D-R18). The manifest also lists every delivered file and column the script did not
carry, with the reason. The `icu/` module is read from under `--mimic-root`.

```bash
python tools/prepare_mimiciv.py --mimic-root .../mimiciv/3.1 \
  --ed-root .../mimic-iv-ed/2.2/ed --note-root .../mimic-iv-note/2.2/note \
  --out $MIMICIV_DATA_ROOT/mimiciv
MIMICIV_DATA_ROOT=... ehr2trace inspect --dataset datasets/mimiciv.yaml
```

The prepared files derive from PhysioNet credentialed data and must not be
redistributed. The YAML is a recipe, not data.

`datasets/cu_ctpa.yaml` converts the University of Colorado CT pulmonary angiography
extract: nine tables exported for a study rather than a database, 127,955 patients,
again with no site-specific conversion code. The figures below are from the remediated
build:

| | |
|---|---:|
| Source rows | 16,682,059 |
| Canonical events | 13,880,372 |
| Event ↔ source-row links | 16,385,410 |
| Subjects | 127,955 |
| Persons published to OMOP | 127,955 |
| MEDS shards | 127,955 |
| Rows quarantined rather than guessed at | 135,200 |
| Validation | 50 passed, 5 skipped, 0 failed |

The five skips are of the same kind: no cohort labels, so two checks have nothing to
examine; no source that says which of its statuses mean administered; no source that
declares an excluded status; and no approximated birth years to recompute. The export dates
each patient's age by the CT it was current at, so the year of birth is derived exactly;
three patients have an age and no CT time, and under `person_birth_policy: strict` no
year was invented for them: they were withheld from PERSON, and with them every clinical
row of theirs, until the day their age was current at was established from the extract
itself (each has a single day of vital signs, the scan day for the rest of the cohort)
and recorded as a decision in the YAML's `open_questions`, with `tools/prepare_cu.py`
counting the rows it dated that way. The same record holds the second question the
delivery raised, what its readmission flags were computed against, answered by
measurement rather than by the owner (see `docs/DECISIONS.md`). `tools/prepare_cu.py`
flattens the export the way `prepare_mimiciv.py` does, with one deliberate exception: a
blood pressure delivered as one cell, `135/76`, becomes the two measurements OMOP
records, so that step writes more rows than it reads and its manifest says so.
Since the 2026-09 remediation the script also does three more things:

- It records the sha256 of every raw input and the rows each step drops.
- It marks each note whose encounter id another table knows (`encounter_linked`, which
  the `prefer_linked` merge rule reads; D-R2).
- It writes a manifest of every CT accession beside the event stream rather than in it
  (D-R16).

```bash
python tools/prepare_cu.py --cu-root .../CU_Data --out $CU_CTPA_DATA_ROOT/cu_ctpa
CU_CTPA_DATA_ROOT=... ehr2trace inspect --dataset datasets/cu_ctpa.yaml
```

Adding the second dataset found defects that development against one dataset could not:
a blocker that fired on the *best* case, a discovery path that could not see columnar
inputs, a validator that reported every check passed on a build whose canonical, OMOP
and MEDS layers had all failed, two out-of-core promises that had never been true, and
the one below.
The third found two that had survived both: a note adapter that read every column but
the text, so 2.6 million notes were published empty, and a canonical cache keyed on code
and configuration but not on its input, which answered a changed source from the previous
build. The first became the fifth check and the eighteenth fault below; the second cannot
be injected as a fault, so it is a regression test instead.

### A conversion that passed everything and mapped nothing

MIMIC-IV writes ICD-10-CM without the decimal point (`F17210`); the vocabulary writes
`F17.210`. Literal matching mapped **182 of 19,440** diagnosis codes — the
three-character ones, which have no dot to disagree about — and turned the other 99%
into `concept_id = 0`.

Every check passed, because `concept_id = 0` is valid OMOP for "no matching concept".
The ids existed, the domains fitted, the source values survived, and the unmapped terms
were queued for review. The build reported, truthfully, that "69,907 distinct terms had
no concept and went to review rather than becoming 0 silently" — a sentence that reads
as diligence and describes nineteen thousand mappable diagnoses lost to punctuation.

`TERMINOLOGY_COVERAGE_PLAUSIBLE` now catches the class: a code system naming an
*installed* vocabulary that maps almost none of its codes has had a lookup failure, not
a discovery that its data is unmappable. Local lab names, which belong to no standard
vocabulary, are excluded on exactly that criterion. The lookup gained a second pass that
ignores punctuation, accepted only where exactly one vocabulary code reduces to the same
string — checked at query time, not assumed. Coverage went to **87.9%**, and recovered
mappings record that they depended on ignoring punctuation.

## Does the check suite detect anything?

Checks passing on the pipeline that produced the data is weak evidence. The other
direction is built in: `src/ehr2trace/faults.py` holds twenty-eight corruptions, each
drawn from an incident that actually happened here. Nineteen date from building the
converter, and nine were found by a read-only audit of three finished conversions on
2026-09-13. Each is silent by construction — row counts plausible, schemas valid, a spot
check on a few patients clean. Detection means a check
that passed on the clean build fails on the corrupted one, and `docs/FAULT_CATALOGUE.md`
says what each fault is for.

Twenty-one checks in the registry exist because a fault in that catalogue got past the
suite first: six after this experiment missed one, and fifteen after the audit. A
detector written in response to a fault is guaranteed to catch it, so the catalogue
records that order rather than only a score. The checks that were added share one
shape: a check that compares an artifact against independently stored information,
which the ones reading a single artifact could not do. Of the first six, the fifth makes
the shape explicit: `TEXT_SOURCES_PUBLISH_THEIR_TEXT` compares what a source *published*
against what its configuration *said it would publish*, which is how 2,652,887 MIMIC-IV
notes were shipped with no text in them while every other check passed. The sixth,
`MEDS_CONCEPTS_ARE_OMOPS`, compares the two targets against each other: a MEDS stage
rerun by hand without the vocabulary the OMOP stage had published 311 million events with
every code unmapped, all thirty-nine checks passed, and a digest comparison of a rebuild
was what noticed. The stage now refuses to build that way, and the check would fail it. The fifteen
written after the audit compare in the same way:

- merged rows against the source rows behind them;
- values and units against reference tables;
- a published death against the deaths the canonical layer holds;
- the delivery against what the YAML says it reads.

It runs on the PHI-free fixture, so it reproduces from a clone with no data access, and
it is a CI gate; the two faults that corrupt concepts need a vocabulary to apply and are
skipped without one:

```bash
.venv/bin/pytest tests/integration/test_fault_detection.py
```


## Is the conversion reproducible, and does the suite cry wolf?

The same inputs under four legal concurrency settings, compared artifact by artifact on
a digest of row content rather than bytes — parquet embeds a writer version, so two
identical builds differ on disk. Determinism is designed for rather than hoped for:
every merge sorts before it chooses, and output paths are content addressed from the
input hashes, the configuration and the code version, so a file that already carries a
given name is the answer for those inputs.

The same run answers the other half. A suite that fires on a correct build detects every
fault and is worthless, so the false-alarm count is asserted beside the agreement. Both
are CI gates:

```bash
.venv/bin/pytest tests/integration/test_reproducibility.py
```


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

### Licensed vocabularies

Concept names in OMOP's standard Condition, Measurement and Drug domains come from
SNOMED CT, LOINC and RxNorm, and are licensed separately by their owners. Nothing here
carries them: the repository ships no vocabulary download and no conversion output, and
`tools/check_vocabulary.py` inspects a build you obtained yourself from
<https://athena.ohdsi.org> under its own terms. [NOTICE](NOTICE) is the full third-party
inventory.

**Patient identifiers are pseudonymized.** Documents and scripts refer only to `PT-A` /
`PT-B` / `PT-C`. Real MRNs and patient-level service dates live in
`tools/baselines.local.json`, which is git-ignored. On a machine that has the raw data,
generate it once before running the verifier:

```bash
python3 tools/make_local_baselines.py PT-A=<MRN> PT-B=<MRN> PT-C=<MRN>
```
