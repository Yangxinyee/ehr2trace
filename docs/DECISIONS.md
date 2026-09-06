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

## Drug names are matched structurally, not by text similarity

A hospital's medication file names the drug instead of coding it. `Medication_Name` is
the only identifier the CTPE export gives, so the two deterministic passes that exist —
look the code up, then look it up ignoring punctuation — have nothing to look up, and
every distinct name lands in the review queue: 26,490 names carrying 8.2 million rows,
against 2.6 million rows that mapped. Drug coverage sat at 23.7%.

The queue was then ranked by dense retrieval over the whole string, and that is where
the real failure was. `CEFAZOLIN 2 GRAM/100 ML IN 0.9% SODIUM CHLORIDE INTRAVENOUS
PIGGYBACK` and `Cefazolin 2000 MG Intravenous Solution` share almost no surface, while
`OXYCODONE 5 MG` sits close to `acetaminophen 300 MG / oxycodone 5 MG Oral Tablet`,
which is a different drug. The exact concepts were in the vocabulary the whole time and
ranked outside the top 32; a person confirming candidates cannot confirm what they were
never shown.

But a drug name is not free text. It states an ingredient, a strength and a dose form,
and the vocabulary states the same three things about the concept — the strength as a
number in `DRUG_STRENGTH`, not as words in a name. Comparing three fields is exact where
comparing one string is not, so `ehr2cdm.drug_match` parses the source name and looks
the concept up by those fields. It is a deterministic pass, in the same class as the
punctuation-insensitive code lookup: exactly one standard concept with that ingredient
set, that strength and that dose form, or nothing.

Four things it is allowed to do beyond literal equality, each of which is a spelling
difference rather than a weaker claim, and each of which still has to end in a unique
concept:

- **dose-form tiers.** `Injectable Solution` and `Injection` are the same vial filed
  twice. `ehr2cdm.drug_lexicon` lists the spellings in order and the first one that
  exists is taken, so two names for one form resolve instead of tying.
- **the total-dose reading.** `2 GRAM/100 ML` on an IV bag is what RxNorm Extension
  calls `2000 MG`. When the source states a whole-container volume both readings are
  tried; a percent and a `1:1,000` are concentrations by definition and are not.
- **the salt suffix.** `OXYCODONE HCL` is RxNorm's `oxycodone` — but the full name is
  tried first, and the fallback is refused outright when the vocabulary has a concept
  that keeps the dropped word. That is what stops `HEPARIN (PORCINE)` from quietly
  becoming plain `heparin` while `heparin sodium, porcine` exists.
- **qualified variants.** `Once-Daily gabapentin 600 MG Oral Tablet` names a product
  line the source did not. Among candidates that already agree on ingredient, strength
  and form, the one carrying the most of the source's own words wins, then the shortest
  name — the same rule, and the same reason, as the lexical candidate ranking.

One rule runs the other way, and it exists because leaving it out produced a real error.
When a source string names no dose form, every form is a candidate, and `FOLIC ACID
1 MG/3 ML` matched `folic acid 1 MG Oral Tablet` — a unique match, on 35,408 rows, and
a tablet. A strength written per millilitre describes something poured, so a source
concentration now restricts the formless case to dose forms the vocabulary measures by
volume. Which those are is asked of `DRUG_STRENGTH` rather than listed here: `Oral
Tablet` and `Oral Capsule` have no drug in it with a millilitre denominator and
`Injection` has 73%, and the gap is not close. It cost three of the confirmed mappings
and removed a class of silent errors, which is the right trade.

Measured against the 139 drug mappings a physician had already confirmed, holding those
mappings out so the matcher cannot short-cut them: 121 of 139 resolve, and of those,
91.7% are the identical concept, 5.8% are the same ingredient and strength under the
other spelling of one dose form, and 2.5% are the same ingredient under the other
reading of a concentration. **None is a different drug and none is a different
strength** — the failure that would matter is absent, and the 16 that do not resolve
abstain rather than approximate.

A second check reads the *concept name* — a different field, written by a different
process from the numbers in `DRUG_STRENGTH` — re-derives a strength from it, and
compares that with the source string. Over the 6,950 names the pass settles on this
export, 5,923 are comparable that way and **none disagrees**.

It found one, and the fix is worth recording because it is the same mistake twice. A
denominator is not always a volume: an inhaler is dosed per actuation and a patch per
hour, and `DRUG_STRENGTH` says so in 18,371 and 13,849 rows respectively. Accepting only
millilitres left every inhaler in this export unmapped — 64,148 rows on one albuterol
product — while `albuterol 0.09 MG/ACTUAT Metered Dose Inhaler` sat in the vocabulary,
and it then made the audit itself report a correctly matched nicotine patch as a
disagreement. The strength key now carries the family of *both* halves, because
`0.09 MG/ACTUAT` and `0.09 MG/ML` are the same two numbers and not the same drug.

Those numbers are re-derivable rather than remembered, and the tool exits non-zero if a
disagreement ever turns out to be a different drug:

```bash
python3 tools/measure_drug_match.py --vocabulary "$OMOP_VOCAB_DIR"   # -> results/drug_match.json
```

The disagreements are classified from `DRUG_STRENGTH` rather than by reading names,
because two concepts with the same ingredient set and the same strength are the same
drug however differently they are spelled.

Two limits worth stating. The lexicon is English drug-name convention, not a vocabulary
— a form phrase it does not know makes a term abstain, which is visible in the review
queue but is a maintenance cost. And the order in which it lists two spellings of one
dose form was calibrated against those 139 physician decisions; that is a preference
recorded in a file a reviewer can read and change, not something derived from the data.

What this does not do is replace review. Names that state no strength (`ONDANSETRON
IVPB`), multi-ingredient solutions (`LACTATED RINGERS`), compounded infusions and
non-drugs (`BLOOD SUGAR DIAGNOSTIC STRIPS`) all still abstain and still go to a person.
The queue is smaller, not gone.
