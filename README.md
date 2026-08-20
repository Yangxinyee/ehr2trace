# ehr-to-omop-meds

Deterministically convert local hospital EHR exports (txt + xlsx) into two standard formats,
**OMOP CDM 5.4** and **MEDS**, with an LLM proposing only where semantic judgment is genuinely
required — and a human confirming every proposal.

**A research converter, not a production system.**

## Documents

| File | Contents |
|---|---|
| [`PATIENT_CDM_AGENT_SYSTEM_DESIGN.md`](./PATIENT_CDM_AGENT_SYSTEM_DESIGN.md) | Design spec v0.5: input facts, architecture, data contracts, OMOP/MEDS mapping, validation requirements |
| [`EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md`](./EHR_CDM_AGENT_IMPLEMENTATION_TASK_CHECKLIST.md) | Implementation checklist v0.2: six phases (P0–P5) of tickable tasks |
| [`tools/verify_doc_baselines.py`](./tools/verify_doc_baselines.py) | Re-derives every number in design §2 from the raw data and compares |
| [`tools/make_local_baselines.py`](./tools/make_local_baselines.py) | Generates the local, git-ignored file holding real identifiers |

## Core constraints

1. The LLM does not parse dates, numbers, or table structure; it does not invent concept IDs;
   it does not write to any target layer.
2. Every canonical / OMOP / MEDS record traces back to a `source_row_id`.
3. Anything uncertain goes to `quarantine/` or `review.csv`. Never guess.
4. Same input + config + mapping version → same output hash.

## Verifying the numbers in the design spec

Section 2 of the design spec claims every input fact was measured. That claim is checkable:

```bash
EHR_DATA_ROOT=/path/to/ehr-export python3 tools/verify_doc_baselines.py
```

About one minute, 55 checks. A non-zero exit code means the data or the document has drifted —
in that case report the drift and investigate the data, **do not edit the expected values in
the document**.

## Data sensitivity

The reference dataset is a semi-processed limited data set that **contains real PHI**.
It is not de-identified.

- The raw directory is read-only; all outputs go to `EHR_WORK_ROOT`.
- `OFFLINE_MODE=1` by default; models and embeddings are entirely local.
- No PHI, `.env`, raw samples, or MRN mapping table may appear in this repository.

**Patient identifiers are pseudonymized.** Documents and scripts refer only to `PT-A` / `PT-B` /
`PT-C`. Real MRNs and patient-level service dates live in `tools/baselines.local.json`, which is
git-ignored. On a machine that has the raw data, generate it once before running the verifier:

```bash
python3 tools/make_local_baselines.py PT-A=<MRN> PT-B=<MRN> PT-C=<MRN>
```

Subsequent runs reuse the mapping from the generated file, so the identifiers never need to be
typed again.
