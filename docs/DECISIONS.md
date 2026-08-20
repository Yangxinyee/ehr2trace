# Decisions taken while implementing this design

The design spec says what to build and, more usefully, what not to. This file records
the calls that had to be made anyway — where the spec was silent, where the data
disagreed with it, and where following it literally would have made the output worse.
Each one is a place a reviewer should push back if they disagree.

## The source layer keeps two representations of a value, on purpose

Section 5.1 defines a canonical string per cell so that a date typed as text in one
workbook hashes identically to a date typed as a date in another. Applying that same
form to *storage* looked like a simplification and was tried, and it broke something:
a bare `2019-03-04` and a `2019-03-04T00:00:00` are different facts. The first says the
source had no time to give; the second says it recorded midnight. One extraction batch
writes anchors the first way and the other writes them the second, and section 2.3
requires that difference to be recorded.

So storage keeps the written shape and hashing normalizes. `source_cell` and
`canonical_cell` have different jobs, and the shape fixture is what made that concrete.

## `--assume-timezone` exists, and it is not a default

The source timezone is an open blocker, and the design is explicit that the developer
machine's zone is never an acceptable substitute. Taken literally that means the
canonical stage cannot run at all on this export, which would leave the whole pipeline
undemonstrable.

The compromise: the stage refuses to run without a zone, an operator may supply one
explicitly on the command line, and doing so records the assumption in the run report
and flags every converted event `TZ_ASSUMED`. The distinction that matters is between
a silent default and a recorded decision, not between running and not running.

## A patient blocked from PERSON is blocked from OMOP entirely

Section 6.3 says strict mode blocks OMOP publication for a patient with no derivable
year of birth. The first implementation blocked only the PERSON row and published the
patient's thirty million clinical rows anyway. A database accepts that; it is not a CDM
instance, it is dangling references with a patient's data attached.

Under strict mode on this export that leaves OMOP empty. That is the accurate report of
what the export supports, and the blocker names exactly what would change it.

## Two ages for one patient block that patient

Not in the spec, and it turns up immediately in real data: two extraction batches
recorded ages a year apart. With no reference date for either, no year of birth is
derivable from either, so the patient is withheld and the disagreement recorded. The
alternative — picking one — is the kind of quiet guess this design exists to prevent.

## `EventSource.anchored_to` is defined and unused

The relation enum in section 5.2 includes `anchored_to`, but the design also decouples
anchors from events entirely and says day differences are recomputed from timestamps.
Given that, there is no event-to-anchor relationship left to record, and inventing one
to justify the enum value would be backwards. The value stays in the schema for a
dataset whose source carries an explicit link; nothing emits it here.

## Order status has no home in OMOP, and is not forced into one

`DRUG_EXPOSURE` has no column meaning "the order was dispensed / discontinued".
`stop_reason` means something else. The status stays on the canonical event, which the
lineage points at from every published drug row. Section 6.2 says to retain it; it is
retained, just not in the core table.

## Study-level procedures can duplicate across sources

A pulmonary function study appears twice: once through its narrative and once through
its component values, with different result times in the source. Both become procedure
events. Merging them would require deciding that two different recorded times are the
same event, which is a clinical judgement the source does not support.

## Three-patient fixtures were replaced, not skipped

Section 10.3 asks for de-identified copies of three real patients. De-identifying free
text well enough to commit is a research problem in itself, and a git history cannot be
un-published. `tests/fixtures/ctpe_shape/` reproduces every structural trap with
fabricated patients and fabricated text; the three real patients are asserted against
the real export in place, reading their identifiers from a git-ignored file.

While auditing for this, a real patient identifier was found in two test files, having
been copied from a data sample early on. It is gone from the working tree and still
present in two unpushed commits; see the README's data-sensitivity section.

## MEDS shards hold one patient each

Section 7.3 says each patient lives in exactly one shard and the checklist says one
shard per patient. Those are different claims. `meds.shard_size` makes it configurable
and the default is one patient per shard, which satisfies both readings; the
contiguity and time-ordering checks hold either way.
