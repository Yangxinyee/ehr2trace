# Does the LLM actually help? (checklist P4-4)

The design says the LLM stays only if it measurably helps, and that if it does not, the
step should be deleted. This is that measurement, run on the reference dataset with
models served locally on the workstation GPU.

Reproduce with:

```bash
ehr2cdm measure --dataset ctpe --kind columns          # column semantics
ehr2cdm measure --dataset ctpe --from-decisions        # terminology (needs a vocabulary)
```

## Column semantics — measured

The answer key is `datasets/ctpe.yaml`: someone decided what all 82 mapped columns
mean, and reproducing that decision is exactly what the model is being asked to do.

The deterministic arm matches the column name against a table of generic
clinical-English synonyms — nothing dataset-specific, which the grep test enforces and
which is also what makes the comparison fair.

| Arm | Prompt | top-1 | s/item | schema failures |
|---|---|---:|---:|---:|
| column name matching | — | **61.0%** | 0.00 | 0 |
| medgemma-27b-text-it | v1 | 52.4% | 2.11 | 0 |
| medgemma-27b-text-it | **v2** | **65.8%** | 2.32 | 0 |
| qwen3-32b | v1 | 48.8% | 2.42 | 0 |
| qwen3-32b | v2 | 59.8% | 2.64 | 0 |

**The prompt mattered more than the model.** Under the first prompt both models scored
*below* a forty-line synonym table, and the honest conclusion would have been to delete
the step. Looking at where they disagreed changed that: both were making the same kind
of error, picking a different member of a near-synonymous role pair — `source_code`
against `source_name` against `display_name`, `text_line` against `sequence_number`,
`event_time` against `available_time`. Those are disagreements with this project's role
vocabulary, not clinical mistakes, and the prompt had listed the role names without ever
saying what distinguished them.

Defining the distinctions moved medgemma by +13.4 points and qwen by +11.0. Only after
that does the medical model clear the baseline, and only by 4.9 points.

### What each arm is good at

The two are good at different things, which is the useful part:

- **Name matching cannot know institutional abbreviations.** It scores `unknown` on the
  patient-key column and on the extraction-anchor column, because a generic synonym
  table has no way to learn them.
- **The models get exactly those.** Both map the patient key correctly, and both read
  from context that a "Status" column in a demographics sheet means alive-or-deceased
  rather than an order status — a judgement that needs the source name and the sample
  values, not the column name.
- **The models lose on this converter's conventions**, which is what the sharper prompt
  addressed and what a human reviewer would catch anyway.

### Verdict

Keep the step, with the human confirmation it already requires. A 4.9-point gain over a
free heuristic is not enough to trust unreviewed, and the design never proposed to: the
proposal lands in `review/pending.csv` and a person decides. What the measurement
justifies is the proposal being *worth reading*, not the proposal being right.

Two caveats stated rather than buried:

- The arms are not given identical inputs. The model sees the source name and a
  de-identified value profile; the heuristic sees only the column name. That extra
  context is the thing being paid for, but it is not a like-for-like comparison.
- 82 columns is a small sample, and a 4.9-point gain is about four columns. This should
  be re-run on the next dataset onboarded rather than treated as settled.

## Terminology ranking — not measured, and blocked

Candidate recall reads the OMOP vocabulary, and no licensed vocabulary is installed:
Athena requires an account and licence acceptance (its API returns `errorCode 3` and the
download endpoint 403s without credentials). With no candidates there is nothing to
rank, so arms 2 and 3 have no input.

The path is implemented and exercised end to end against the local model — a ranking
over supplied candidates returns valid schema-constrained JSON with per-candidate
rationales, and the validator rejects any concept id that was not supplied, which is the
mechanical guarantee that a model cannot introduce one from memory.

Until it is measured, use `--no-llm` for terminology. An unmeasured ranking step is an
unjustified one.

## Operating a local model on this hardware

- vLLM's FlashInfer sampler crashes during CUDA graph capture on this Blackwell card,
  claiming it requires sm75 on an sm120 GPU. `VLLM_USE_FLASHINFER_SAMPLER=0` avoids it.
- `--enforce-eager` skips several minutes of graph capture that buys nothing for short
  structured calls.
- JSON-schema-constrained output is **probed** at startup rather than inferred from the
  model name, per design section 8.2. Both models supported it, and neither produced a
  single schema failure across 328 calls.
- Nothing leaves the machine: the client refuses a non-local endpoint while
  `OFFLINE_MODE=1`, and sample values are dropped by an identifier-shaped filter before
  a payload is built.
