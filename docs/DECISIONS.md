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
comparing one string is not, so `ehr2trace.drug_match` parses the source name and looks
the concept up by those fields. It is a deterministic pass, in the same class as the
punctuation-insensitive code lookup: exactly one standard concept with that ingredient
set, that strength and that dose form, or nothing.

Four things it is allowed to do beyond literal equality, each of which is a spelling
difference rather than a weaker claim, and each of which still has to end in a unique
concept:

- **dose-form tiers.** `Injectable Solution` and `Injection` are the same vial filed
  twice. `ehr2trace.drug_lexicon` lists the spellings in order and the first one that
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

A second pass over every name the matcher left unresolved on the JHU-CTPE export
(2026-09-11) found that most of what it missed was one of a few habits of pharmacy
English, and that some of what it settled it had guessed.
What changed, and why each is still a reading of the string rather than a weaker claim:

- **route words and diluents say bag.** `IVPB`, `INFUSION`, `BOLUS FROM BAG`, `IN 0.9
  % SODIUM CHLORIDE`, a bare `NS` or `D5W` name no dose form, but each rules out every
  form that is not an injectable. A name carrying one and no form phrase is read as an
  injectable, and the diluent is not part of the drug. The injectable spellings are
  tried before `Intravenous Solution` because that is what the physician chose for
  `CARBOPLATIN CHEMO IVPB`.
- **a percent on an ointment is weight in weight.** RxNorm states those per gram;
  `HYDROCORTISONE 2.5 % OINTMENT` is `hydrocortisone 25 MG/G`. The liquid reading is
  tried first; the dose form decides.
- **strengths agree within RxNorm's own rounding.** `LACTULOSE 20 GRAM/30 ML` is its
  `667 MG/ML` and `ALBUTEROL 2.5 MG/3 ML` its `0.83 MG/ML`; a tolerance of 1% is what
  that needs and no more, and the 54 JHU-CTPE names that resolve only within it were
  read one by one.
- **an export width is declared, not discovered.** An export that cuts names at a
  fixed width can say so (`terminology.drug_name_truncated_at`); a name of exactly that
  width is read whole first, and only if that settles nothing is the fragment dropped.
  Neither private export does: the fragments this rule was written for (`SOLUTION F`,
  `SUBCUTAN`) were read off OMOP's `drug_source_value`, which the CDM caps at 50
  characters, while the matcher reads the canonical event's whole name. The
  declarations were withdrawn once that was seen; the rule stays for an export that
  needs it.
- **a salt the source left out.** `HEPARIN (PORCINE)` is RxNorm's `heparin sodium,
  porcine`; the rule that refused plain `heparin` while that concept existed now takes
  the concept instead, when exactly one differs from the source by salt words alone.
- **a brand is a combination only if one of its products is.** `PRIMAXIN` expands to
  cilastatin and imipenem; `PRADAXA`, filed under two ids for one ingredient, does not.
- **the ingredient alone.** `Acetaminophen`, `HYDROmorphone (Dilaudid)`, `MORPHINE
  VARIABLE DOSE` state an ingredient and nothing else, and RxNorm's Ingredient concept
  is the standard concept that states exactly that; the physician mapped the last two
  the same way. Until this reading existed, such names reached a clinical drug *form*
  by the length of its name -- 17 million MIMIC-IV administration rows carried a dose
  form the string never named -- and, once that guess was withdrawn, nothing at all.
- **the set of forms measured by volume had been empty.** RxNorm writes "per one
  millilitre" with a blank denominator *value*, and the test for a liquid required the
  value to be present, so the formless rule above had never admitted anything.

And where the old rules guessed, they now abstain: a concentration that fits the drug in
several forms with no form named (`HEPARIN 100 UNIT/ML` is a flush, a vial and an
irrigation) was settled by the length of the concepts' names; the bag reading of
`40 MEQ/250 ML` as `40 MEQ` matched an oral powder when nothing said bag; `300 MG
IODINE/ML` compared against a drug mass found `Iohexol 302 MG/ML`, a different product;
`insulin` reaches regular insulin through one national vocabulary and glargine through
another; `BASAGLAR KWIKPEN` is filed under regular insulin while `Basaglar` is glargine;
a dose range in parentheses (`(0-499 MG CUSTOM DOSE)`) read as a strength matched the 500
mg product; and `insulin aspart-szjj` begins with `insulin aspart` and names a biosimilar
the source did not. Each of these is a rule with a test, and each ends in the review
queue rather than in a concept.

Measured against the 139 drug mappings a physician had already confirmed, holding those
mappings out so the matcher cannot short-cut them: 129 of 139 resolve, and of those,
93.8% are the identical concept, 5.4% are the same ingredient and strength under the
other spelling of one dose form, and 0.8% (one name, a biosimilar written with no
strength) are the same ingredient under the other reading of a concentration. **None is
a different drug and none is a different strength** — the failure that would matter is
absent, and the 10 that do not resolve abstain rather than approximate. Before the
second pass the numbers were 121 of 139 and 91.7% / 5.8% / 2.5%. The eight mappings
decided by hand on 2026-09-11 because the matcher abstains on them by design (lactated
Ringer's, iodinated contrast stated as iodine) are held out of this set with
`--gold-through 2026-09-10`. Over the export's original queue of 26,629 medication
names the pass settles 9,850 (8.66 million rows), where the first pass settled 6,950.

A second check reads the *concept name* — a different field, written by a different
process from the numbers in `DRUG_STRENGTH` — re-derives a strength from it, and
compares that with the source string, reading a percent both ways and allowing the same
1% the matcher allows. Over the 9,850 names the pass settles on this export's original
queue of 26,629 (8.66 million rows), 8,403 are comparable that way and **none
disagrees**. Before the second pass it settled 6,950, with 5,923 comparable.

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
python3 tools/measure_drug_match.py --vocabulary "$OMOP_VOCAB_DIR" --gold-through 2026-09-10   # -> results/drug_match.json
```

The disagreements are classified from `DRUG_STRENGTH` rather than by reading names,
because two concepts with the same ingredient set and the same strength are the same
drug however differently they are spelled.

Two limits worth stating. The lexicon is English drug-name convention, not a vocabulary
— a form phrase it does not know makes a term abstain, which is visible in the review
queue but is a maintenance cost. And the order in which it lists two spellings of one
dose form was calibrated against those 139 physician decisions; that is a preference
recorded in a file a reviewer can read and change, not something derived from the data.

What this does not do is replace review. Multi-ingredient solutions the vocabulary has
under no name the source uses (`LACTATED RINGERS`), strengths stated as an element
(`320 MG IODINE/ML`), a number the parser did not read, parenteral nutrition, and
non-drugs (`BLOOD SUGAR DIAGNOSTIC STRIPS`) all still abstain and still go to a person.
The queue is smaller, not gone.

## Two CU-CTPA questions were answered by the study team, and say so

The export dates each age by the first inclusion procedure, and three rows carry the
age without the procedure's timestamp; its readmission flags are derived columns whose
reference event was never stated. Both were recorded as open questions for the data
owner, and on 2026-09-11 both were answered from the delivery itself instead, because
the delivery could settle them and the owner had not.

The age: vitals in this export are a one-day snapshot taken at the scan (the earliest
flowsheet day is the CTA day for 92.0% of the 127,746 patients with both, within a day
of it for 95.2%), and for two of the three the notes written that day state the same
age. So `prepare_cu.py` dates those three ages by their earliest flowsheet day, says so
in two output columns, and counts the rows in its manifest. The CTA time stays empty:
nothing invents the scan.

The flags: measured per patient rather than over all rows, each first row reproduces
from `cta_time` alone (all five flags for 97.9%, the 30-day flag for 99.4%, boundaries
exact: 7, 30 and 90 days inclusive, six calendar months, one calendar year), and the
rows that do not are the same admission repeated with another flag vector -- never for a
patient with one T1a accession, more often the more accessions a patient has. T5 holds
one row per admission x scan and the export dropped the scan column. A readmission
label at those horizons is therefore derived from `readmission_date` and `cta_time`,
never copied from the flags; the flags for later scans cannot be reproduced because
those scans' dates were not delivered.

Both answers live in the YAML under `open_questions` as `answer` text that names the
decider, the date and the evidence. The alternative of waiting kept three patients out
of OMOP and a label unusable for reasons the data could settle; the alternative of
answering silently in code is the quiet guess this design exists to prevent.

## A code the vocabulary has retired is still the code the record carries

The resolver required the *source* concept to be current: `CONCEPT.invalid_reason IS
NULL` sat in the join that finds a source code, in the batch pass, in the unpunctuated
pass and in the per-term lookup. That is a statement about today's code set rather than
about the record. Athena keeps a withdrawn concept and its `Maps to` for exactly the
opposite reason -- an NDC leaves the market, an ICD-10-CM code is split at a fiscal-year
boundary, and the history coded with it does not change.

Measured on 2026-09-11 before the change: 1,496 discontinued NDCs carrying 3,036,972
MIMIC-IV prescription rows, 129 superseded ICD codes carrying 5,046 MIMIC-IV diagnoses
and 116 carrying 2,542 JHU-CTPE diagnoses resolved to nothing, while the same drugs
prescribed a year later resolved. The retired source concept is now admitted and only
its relationship to a *current* standard concept is followed, so nothing withdrawn is
published; the path records it (`mapped_relationship_retired`), which is what makes the
set reviewable rather than invisible.

A human mapping in `mappings/` is held to the stricter rule, unchanged: `_confirm`
still requires a current standard concept, because a person's decision that points at a
withdrawn concept is a decision to remake. Five did, and were remade the same day -- four
community LOINC codes for polymorphonuclear cells, superseded by granulocyte codes, and
`ETHNICITY`, which pointed at a concept this bundle deletes and so had silently mapped
nothing at all.

## Which of a code's several targets is published is a question about the row

`C92.01`, acute myeloid leukaemia in remission, maps to two standard concepts: the
condition and an Episode concept for the remission. The resolver published the lowest
concept id, which is deterministic and nothing else -- it is the Episode, and 2,892
MIMIC-IV diagnosis rows were published carrying a concept the condition table cannot
hold. 70 ICD-10-CM codes in that export behave this way.

The event kind already says which domain the row is headed for, so it now chooses: the
first target in that domain wins, the lowest id remains the tie-break where the kind
expects no particular domain, and every other target is still carried as an alternate
with `_ambiguous` in the path. This is not a domain *gate* -- the 2026-09-04 decision
that a code's domain is the vocabulary's to state and the publisher's to route still
holds, and a code with one target still resolves to it whatever its domain.

## A bag is not a drug exposure, and a hold tube is not a measurement

MIMIC-IV's `prescriptions` table carries 3,471,112 rows with `drug_type = 'BASE'`: the
diluent half of a compounded order, whose partner `MAIN` row names the drug. Most of
them say what the fluid is and are kept, now mapped to it -- `Iso-Osmotic Dextrose`,
`5% Dextrose` and `D5W` to glucose 50 MG/ML, `0.9% Sodium Chloride`, `NS` and
`Iso-Osmotic Sodium Chloride` to sodium chloride 9 MG/ML, `Sterile Water` and `SW` to
the water ingredient, RxNorm having no product concept for water for injection. A
patient given a litre of saline was given saline, and the same rows arriving with an
NDC already resolved that way, so leaving the named ones at concept 0 was an
inconsistency rather than a caution.

Three of those names are not fluids at all. `Bag`, `Vial` and `Soln` describe the
container -- the product strengths read `Bag 100 mL Bag`, `Vial Send Vial`, `Soln 50 mL
Vial` -- and name no substance; the drug is on the `MAIN` row of the same
`pharmacy_id`, which for `Bag` is magnesium sulfate in 87% of cases. 439,999 rows of
"the patient was exposed to a bag" were published as drug exposures. They are dropped by
a declared row filter, where `SOURCE_ROWS_ACCOUNTED` reports them as rows that moved.

Ten laboratory items are specimen handling rather than results, and were checked value
by value over every row before being dropped: the hold tubes (`Green Top Hold`,
`Blue Top Hold`, `Light Green Top Hold`, `Red Top Hold`, `Uhold`, `Urine tube, held`,
`EDTA Hold`) carry "HOLD. DISCARD ..." or a masking marker in every row, `Problem
Specimen` six values in 33,059 rows, `Assist/Control` no value at all, `XUCU` the word
DONE. Items whose labels name no analyte but whose rows do carry numbers (`STX1-6`,
`UTX1-7`, `HPE1-7`, `ARCH-1`) are kept and stay unmapped: a value is a measurement even
when its name is opaque, and that is MIMIC-IV's ceiling, not this converter's decision.

## The punctuated and unpunctuated passes now agree about a code's other targets

Unifying the two resolution passes exposed a third inconsistency between them. The first
pass records every standard concept a source code maps to -- the primary in the event's
column, the rest as alternates, which the OMOP writer publishes as one row per concept
because a combination code asserts all of them. The unpunctuated pass, which is how a
code written `Y929` rather than `Y92.9` is found, kept only one and dropped the others,
while still writing `_ambiguous` in the path to say there had been a choice.

The effect was that the same code fanned out or did not depending on how the hospital
wrote its decimal point: JHU-CTPE writes them, so `T43.292A` published both the poisoning
and the intentional self-harm; MIMIC-IV does not, so 7,519 of its 20,517 ICD-10-CM codes
asserted only their first target. Making the passes agree adds 433,666 condition rows,
30,564 observation rows and 362 procedure rows to the MIMIC-IV export -- rows that were
always implied by the codes and by the rule the first pass already followed. MEDS is
unaffected: it builds a single-pick map on purpose, one code per event.

This was not the goal of the change, and it is kept rather than reverted for the reason
the rule exists: a cohort query for either half of a combination code should not miss
patients because of a punctuation convention.

## Every published row says where the record came from

OMOP puts a `*_type_concept_id` on every clinical row to record the provenance of the
record itself: a prescription, an administration record, a problem list, an encounter.
`mappings/type_concepts.csv` existed for those eleven keys and was empty, so every
published row carried 0 -- legal, and less than this converter knows.

Comparing the export against the conversion OHDSI publishes for the MIMIC-IV demo made
the gap concrete: that conversion marks all of its drug rows `32838` (EHR prescription),
which is one fact about provenance, and ours marked none. The registry is now filled
from the vocabulary's own type concepts, so a drug row published from an order carries
`32838` and one published from an administration record carries `32818`. The distinction
between ordering and giving a drug is the one this system exists to keep, and it now
survives into the field OMOP provides for it rather than living only in the canonical
layer.

The ids are decisions in the git-tracked registry, checked against the loaded vocabulary
like every other mapping, because an id typed into code has no provenance and nobody
revalidates it when the vocabulary is updated.

## The dose someone else will set, and the tablet that was split

Two groups of CU-CTPA anticoagulant names never reached a concept, and both are about a
name that states less than a product.

A warfarin order handed to the pharmacist is written `WARFARIN PHARMACY TO DOSE`, `PER
PHARMACY` or `PER PHYSICIAN` -- 11,077 rows. Those phrases say who sets the dose, not
what the drug is, so they join the site markers in `drug_name_noise` and the matcher's
ingredient reading publishes warfarin. The dose was never in the name: it is in the
event's own field, where an order that delegates it leaves it empty.

A split tablet is written `WARFARIN 1.25 MG TAB (HALF-TAB)`. The strength names the dose
after splitting, not a product -- RxNorm has no warfarin tablet at 1.25, 1.5 or 3.75 mg,
and the tablet dispensed is twice each. `HALF-TAB` is deliberately *not* a noise word:
stripping it would let `WARFARIN 500 MCG TAB (HALF-TAB)` match the 0.5 mg tablet, which
does exist and was not what was dispensed. Doubling a number to choose a concept is an
inference this converter does not make, so those five names are mapped by hand to their
ingredient and the strength stays where the source put it.

The three anticoagulation starter packs need none of this: RxNorm carries the pack with
its own tablet counts, and 42 plus 9 rivaroxaban tablets and 74 apixaban tablets match
the source names exactly, so the pack concept states what was dispensed.

## The UTC suffix on one CU-CTPA column is a label

`CTA_timestamp` is the only column in that export naming a zone and it says UTC, while
every other date arrives bare. Either reading was defensible from the schema, so it went
to the data owner as an open question and `America/Denver` was declared meanwhile.

The delivery settles it. Pairing each scan with the same patient's vital signs, across
914,951 rows within a day of a scan, puts the offset between them at -1 to +1 hours with
a median of -32 minutes: a scan and its workup on one clock. A column in true UTC beside
Denver-local vitals would peak at -6 or -7 hours, and those bins hold about a tenth of
the peak with no second mode. Both hour-of-day distributions peak in the afternoon and
trough before dawn.

So the suffix labels the column rather than converting it, the declared zone was already
right, and nothing changes but the record of why. The question stays in the YAML with
that reading as its answer, marked as the study team's and put to the owner to confirm.
