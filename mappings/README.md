# mappings/

Human-confirmed terminology decisions, versioned in git. This directory is the **only**
thing that can turn a source string into a concept id, and the only writer is
`ehr2trace compile`, which reads `review/decisions.csv`.

Nothing else may write here. Not the vocabulary lookup, not the model, not a retry
path. That rule is what makes it possible to answer, for any published row, who
approved the mapping behind it and when.

| File | Contents |
|---|---|
| `type_concepts.csv` | The fixed OMOP type concepts this ETL needs (`TYPE_CONCEPT` keys). Empty until someone with a vocabulary fills it in; until then every type concept id is 0. |
| `<domain>.csv` | Compiled decisions for that domain, one row per source string. |

Columns are the same in every file:

```
source_string,code_system,concept_id,concept_name,domain_id,vocabulary_id,mapping_version,decided_by,decided_on,note
```

## How `compile` merges

`review/decisions.csv` is a log. A decision stays in it after the row it produced has
been corrected, and the correction may have come from another work root: one row serves
every dataset that writes the same string. Compiling must not undo the correction, so
each accepted decision is compared by `decided_on` with the row its term already has, in
whichever file holds it:

| The decision was made | `compile` |
|---|---|
| on a later day than the row | replaces the row |
| on an earlier day | leaves the row and reports the decision as superseded |
| the same day, and differs | leaves the row, reports a conflict and exits non-zero; `--replace <decision id>` applies it |

To restore a decision that a later row replaced, record it again, dated the day it is
decided. An accepted decision without a reviewer and a `YYYY-MM-DD` date is not compiled.
`compile` prints every row it adds, replaces or declines to replace, and rewrites a file
only when a row in it changed.

## Type concepts

`type_concepts.csv` uses `TYPE_CONCEPT` as its `code_system` and one of these keys as
`source_string`:

```
visit, condition_problem_list, drug_order, drug_admin, procedure,
measurement, note, note_class, death, observation_period
```

They are deliberately not literals in code. A concept id typed into a source file has
no provenance, and nobody revalidates it when the vocabulary is updated.
