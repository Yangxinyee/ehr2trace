# Contributing

## Adding a new hospital export

You should not need to write Python. Everything dataset-specific lives in one file
under `datasets/`, and a test (`tests/test_no_hardcoded_dataset_strings.py`) fails the
build if a column name, sheet name, partition directory or cohort label from any
particular hospital appears anywhere under `src/`.

The path is:

1. Copy `datasets/generic_ehr.yaml` and describe your export: partitions, file globs,
   sheet aliases, and for each source the column aliases for each canonical field role.
2. `ehr2trace inspect --dataset datasets/yours.yaml`. This reads no data. It reports what
   it found, what it could not find, and — importantly — the **blockers**: questions
   only the data owner can answer.
3. Answer them in the YAML. Do not work around them. A blocker exists because guessing
   produces a corpus that trains without error and is wrong.
4. `ingest` → `identity` → `canonical` → `omop` / `meds` → `validate`.

If your export needs a field role or a row shape that does not exist, that is a change
to `src/ehr2trace/registry.py` and the relevant shape — a genuine core change, and worth
opening an issue about first.

## Blockers are not obstacles

The converter refuses to run without a declared timezone. It withholds a patient from
OMOP entirely — not just from `PERSON` — when the birth year cannot be derived. It will
not produce cohort labels until the rule binding a label to an episode is written down.

These are the design, not friction. If you find yourself adding a default to get past
one, the answer is almost always to record the assumption in the dataset config, where
it is visible and reviewable, rather than in code where it is not.

## Adding a check

Checks live in `src/ehr2trace/validate.py` behind the `@check("ID")` decorator. A good one:

- **reads the artifact that ships**, not the code or config that produced it. Every one
  of the four gaps found by fault injection was a check looking at the wrong artifact —
  a config lint standing in for a data check, a unit test covering a writer but not what
  it wrote, an identity map never joined to the events, a policy verified without
  comparing the published value to what the source implies;
- **compares against something derived independently**, so the check can disagree;
- **skips rather than fails** when the layer it needs is not built, so a partial pipeline
  reports honestly.

## Adding a fault

`src/ehr2trace/faults.py`. Two rules, and they are what make the experiment mean anything:

1. **The fault must be drawn from something that actually happened.** Invented faults are
   the ones you already knew how to prevent, which is exactly why they flatter a detector
   suite. Record the incident in the `origin` field and in `docs/FAULT_CATALOGUE.md`.
2. **The fault must be silent** — it leaves row counts plausible, schemas valid, and a
   spot check on a handful of patients clean. Loud failures are caught by the pipeline
   crashing and are not interesting.

Mutations must write a new file and rename it into place, never truncate an existing one:
work trees are cloned with hard links so the experiment stays cheap at full scale, and
truncating a shared inode reaches back into the build being measured. Database files are
real-copied for the same reason.

Set `expect` to the checks you predict will fire. It is recorded and compared, never
enforced — a fault caught by a check nobody predicted is a more interesting result than
one caught by the check named after it.

## Tests

```bash
pytest tests/ -q                      # everything that runs without patient data
pytest tests/ -q -m realdata          # needs EHR_DATA_ROOT
```

No test may contain a real MRN, a real patient date, or any other identifier. The
fixtures under `tests/fixtures/` are fabricated and reproduce the structural traps of a
real export without reproducing any of its content.
