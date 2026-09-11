# Fault catalogue

Forty checks passing on the pipeline that produced the data proves very little.
The question a reader should ask is the other one: when a specific corruption is
present, does anything fire?

This file records the faults `ehr2trace.faults` injects, and — for each — the incident it
is drawn from. Every one of them happened while building this converter. That
constraint matters: invented faults are the ones you already knew how to prevent, which
is precisely why they make a detector suite look better than it is.

Reproduce with:

```bash
.venv/bin/pytest tests/integration/test_fault_detection.py
```

The experiment runs against `tests/fixtures/ctpe_shape/`, which contains no patient
data, so the result is reproducible by anyone who clones the repository. Two faults
corrupt concepts and need a vocabulary installed to be applicable; the numbers below
were measured with one, and the CI run, which has none, reports those two as skipped.
Detector sensitivity is a property of the checks, not of the dataset's size.

## Result

Every fault in the catalogue below is detected: a check that passes on the clean
build fails on the corrupted one. That is asserted by the test above and gated in CI.

The number worth reading is not nineteen out of nineteen. **Six** of the nineteen got
past the suite the first time and motivated the six checks that now catch them, and a
detector written in response to a fault is guaranteed to catch it. What the six have in
common is the transferable part: each compares an artifact against independently stored
information, which no check reading a single artifact in isolation could do.

Fourteen of the nineteen never reach an OMOP database at all, so standard CDM-level
data-quality tooling cannot be pointed at them; they are faults in the canonical layer,
the anchors, the identities, or the MEDS shards, above or beside the target schema.

Detection is also not the same as being able to act on it. Comparing each mutated clone
against the tree it was cloned from establishes which artifacts a fault actually damaged;
a firing check localises the fault if it names one of them. Sixteen of nineteen do.

Both numbers come from the identical harness; the "before" figure is measured by
ignoring the four new check ids, not by checking out an older revision, so nothing else
moves between the two rows.

The honest caveat, stated up front: a detector written in response to a specific fault
is guaranteed to catch that fault. 17/17 is not evidence that the suite is complete. The
value of the experiment is in the first row — five corruptions that the suite was
supposed to cover and did not — and in the five gaps being of a kind that recur.

## What the five misses had in common

Each miss was a check looking at the wrong artifact.

| Miss | Why it was missed |
|---|---|
| `ANCHOR_USED_AS_EVENT_TIME` | `ANCHOR_NEVER_AN_EVENT_TIME` lints the *config*: it verifies no source wires an anchor column to a time role. A build whose *data* already carries anchor dates as clinical times passes it untouched, because the config it derives from is innocent. |
| `PARTITION_COLUMN_LEAKED_INTO_CANONICAL` | A unit test covered the canonical *writer*. Nothing covered the canonical *artifact*, which is what actually ships. |
| `IDENTITY_NOT_RESOLVED_ACROSS_PARTITIONS` | `IDENTITY_RESOLVED_ACROSS_PARTITIONS` reads the identity map and confirms it is internally consistent. Nothing joined the map to the events, so subject ids in the event table that the map never issued were invisible. |
| `BIRTH_YEAR_INVENTED_UNDER_STRICT_POLICY` | `OMOP_BIRTH_POLICY_ENFORCED` verifies the right *policy* was applied and that derived years were flagged. It never compares the published number to the number the source implies, so a systematic offset applied to every patient survives it — and is invisible to a distribution check too. |
| `NOTE_TEXT_SILENTLY_DROPPED` | Every check read what was published. None compared it against what the *config* said would be published, so a source that declared a text column and emitted none satisfied all of them: the events had codes, times and lineage, and the OMOP exporter's `coalesce` turned the missing text into an empty string that counted as present. |
| `MEDS_BUILT_WITHOUT_VOCABULARY` | Every check read one target at a time. None compared the two targets against each other, so a MEDS layer whose every code was unmapped sat beside an OMOP layer carrying 29,459 concepts and satisfied all of them: the shards validated, the shards' codes were documented, the lineage was complete. A digest comparison of a rebuild noticed; `MEDS_CONCEPTS_ARE_OMOPS` now asks whether each term resolves alike in both, and the MEDS stage refuses to build without the vocabulary its OMOP layer had. |

The five checks added in response — `ANCHOR_TIMES_ARE_NOT_THE_EVENT_CLOCK`,
`CANONICAL_SCHEMA_AS_DECLARED`, `EVENT_SUBJECTS_WERE_ISSUED_BY_IDENTITY`,
`OMOP_BIRTH_YEAR_IS_REPRODUCIBLE`, `TEXT_SOURCES_PUBLISH_THEIR_TEXT` — are all of the
same shape: check the artifact that ships, and check it against something derived
independently of it. The last one makes the pattern explicit, because the independent
thing it checks against is the dataset's own declaration.

## The catalogue

Every fault below is classed `silent`: it leaves row counts plausible, schemas valid,
and a spot check on a handful of patients clean. That is the selection criterion. Loud
failures are not interesting — they are caught by the pipeline crashing.

### Canonical layer

| Fault | Drawn from |
|---|---|
| `ANCHOR_USED_AS_EVENT_TIME` | The reference export repeats every ancillary result once per index study, carrying the study's date on each repeated row. Reading that column as the result's own time is the mistake the layout invites. |
| `COHORT_LABEL_BECAME_A_DIAGNOSIS` | Partition directories named after the condition that defines the cohort. Turning that name into a condition row hands a model its own target back as a feature. |
| `PARTITION_COLUMN_LEAKED_INTO_CANONICAL` | Writing canonical tables with hive partitioning silently appended a `bucket` column to every one of them. Nothing failed; the schema grew a field encoding how the data was sharded. |
| `POST_DEATH_RECORDS_DELETED` | 62,067 records in the reference export are dated after the patient's death. Deleting them looks like cleaning and destroys the evidence of which of the two dates is wrong. |
| `QUARANTINE_REASON_ERASED` | A globbing bug staged two ingest versions of every source, exactly doubling the quarantine. Nobody noticed, because events deduplicate by id and the headline counts still looked right. |
| `LINEAGE_LINKS_DANGLE` | Rebuilding one layer without the other leaves links pointing at events that no longer exist, while the published tables still look complete. |
| `IDENTITY_NOT_RESOLVED_ACROSS_PARTITIONS` | 6,784 patients appear in more than one partition. Hashing the partition into the subject key splits each into several people, inflating the cohort and truncating every timeline. |
| `NOTE_TEXT_SILENTLY_DROPPED` | `text` was a declared field role that only one shape read. A source whose rows are each a whole note mapped its text column, converted without a complaint, and published 2,652,887 MIMIC-IV notes with nothing in them. |

### OMOP layer

| Fault | Drawn from |
|---|---|
| `WITHHELD_PATIENT_STILL_PUBLISHED` | The strict birth-year policy withheld patients from `PERSON` while every clinical builder went on publishing their rows: 31 million dangling references in a database that reported a successful build. |
| `MEASUREMENT_DATE_FABRICATED` | 92,005 values in the reference export carry no time anywhere. Giving them the run date makes them look like measurements that happened. |
| `PRIMARY_KEY_COLLISION` | A surrogate key derived from too short a hash prefix collides silently; the row count is right and one fact overwrites another downstream. |
| `CONCEPT_PLACED_IN_THE_WRONG_DOMAIN` | 2,313 of the reference export's diagnosis codes map to standard concepts outside the Condition domain. Publishing them into `CONDITION_OCCURRENCE` anyway is what a mapping without a domain gate does. |
| `BIRTH_YEAR_INVENTED_UNDER_STRICT_POLICY` | The export carries an age but no birth date and no as-of date. Filling `year_of_birth` from the run year is the permissive default, and it shifts every patient's age by the gap between extraction and run. |

### MEDS layer

| Fault | Drawn from |
|---|---|
| `COHORT_LABEL_LEAKED_INTO_MEDS_EVENTS` | The partition a patient came from is perfectly correlated with the label. A partition column in the event stream is a free answer key that arrives looking like ordinary provenance. |
| `AVAILABILITY_PRECEDES_OCCURRENCE` | `available_time` is what stops a model reading a lab result before the lab reported it. Defaulting it to the event time — the obvious thing when a result time is missing — removes the protection while leaving the column populated and the schema valid. |
| `SHARD_NOT_TIME_SORTED` | A shard whose rows are out of time order still validates against the MEDS schema. Every model that reads it as a sequence reads a shuffled history. |
| `SUBJECT_IN_TWO_SPLITS` | Bucketing by anything that is not the subject key reintroduces the oldest leak there is. |
| `CODE_METADATA_INCOMPLETE` | `codes.parquet` is the only description a downstream consumer gets of what a code means. Dropping entries leaves events referring to codes nothing documents. |
| `MEDS_BUILT_WITHOUT_VOCABULARY` | The vocabulary reaches the MEDS stage through an environment variable. A stage rerun by hand without it, after an out-of-memory kill, published 311 million MIMIC-IV events with every code `SOURCE/` and every concept null while the OMOP layer beside them mapped 29,459, and thirty-nine checks passed on the pair. |

## A note on the harness

The first run of this experiment corrupted the build it was measuring. Work trees are
cloned with hard links so that a clone costs nothing at full scale, which is safe for
the parquet artifacts because every mutation writes a new file and renames it into
place. It is not safe for a DuckDB file, which is opened read-write and modified in
place: the OMOP faults reached through the clone and rewrote the original, and the
corruption then leaked across subsequent faults and inflated their detector counts.

`clone_work_tree` now copies database files and links everything else. The numbers
above are from after that fix. The incident is recorded here because it is the same
class of failure the catalogue is about — a sharing relationship that is invisible
until something writes.

## An incident this catalogue cannot hold

Faults here mutate a built artifact, which is what makes them measurable: clone, inject,
re-run the checks. One incident from the same period does not fit that shape and is
recorded here anyway, because leaving it out would make the catalogue look more complete
than the record is.

A canonical bucket's content address held the code version, the config, the mapping
version, the timezone and the bucket count — how to build a bucket, and never what from.
A run whose *data* changed while its config and code did not therefore kept every
address it already had, reported success, and republished the previous answer. Splitting
a blood pressure into its two measurements staged 5,580 new rows and produced not one
new event, and `clean` could not help, because the stale output matched the address the
current code would give it.

There is nothing to inject: the artifact is correct for the input the build actually
read. The defect is in which input it read. Its regression test lives in
`tests/integration/test_generic_ehr_pipeline.py` instead, and it fails on the previous
revision — which is the same standard every fault above is held to.
