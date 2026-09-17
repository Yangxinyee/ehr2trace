# experiments/

The scripts behind the numbers in the EHR2Trace paper, and the records they wrote.
Each script imports the `ehr2trace` package from this checkout and runs against a work
root you have built yourself; nothing here contains patient data. `results/` holds the
aggregate records the paper's tables and macros were generated from, so a reader can
check a number without rebuilding, and rebuild when they want to check the record.

## Scripts

| Script | What it measures | Needs | Writes |
|---|---|---|---|
| `run_fault_experiment.py` | Whether the validation suite catches each of the 28 injected faults (Section 4.4); `--exclude-group` scores the reduced suite | a built work root (the synthetic fixture suffices) | `results/faults_fixture.json`, `results/faults_fixture_before.json` |
| `run_reproducibility_experiment.py` | Whether four legal concurrency settings produce identical artifacts, and whether the suite raises false alarms on a correct build (Section 4.6) | the synthetic fixture | `results/reproducibility.json` |
| `run_dqd_experiment.py` | What the OHDSI Data Quality Dashboard detects of the faults that reach OMOP (Section 4.4) | Docker and `tools/dqd/` | `results/dqd_baseline.json` |
| `run_converter_comparison.py` | What three conversions of the MIMIC-IV demonstration subset retain: source rows, availability times, medication actions, concepts (Section 4.3, Appendix A) | this system's demo conversion and the two published baselines | `results/converter_comparison.json` |
| `run_leakage_experiment.py` | Held-out performance of one model under three time rules (Section 4.5) | a MIMIC-IV MEDS build | `results/leakage_downstream.json` |
| `run_leakage_transfer.py` | Each time-rule model scored under every other rule: evaluation inflation and training damage (Table 6) | the same build, plus the record above | `results/leakage_transfer.json` |
| `measure_scale_cost.py` | Wall time and peak memory of each stage on a hard-link clone of a built tree, and whether the rebuild reproduces the original layers (Section 4.6) | a built work root | `results/cost.json` |
| `run_paper_validation.py` | Reruns the validation suite on a built dataset and records the report with the software commit | a built work root | `results/mimiciv_validation_rerun.json` |
| `measure_retrieval.py`, `measure_terminology_llm.py`, `make_loinc_synonym_terms.py` | The terminology-ranking measurements: lexical retrieval, dense retrieval and local-model ranking against confirmed decisions | a vocabulary build and, for the model arms, a local OpenAI-compatible endpoint | `results/retrieval_*.json`, `results/terminology_*.json`, `results/loinc_*.json` |
| `strip_vocabulary_strings.py` | Removes SNOMED, LOINC and RxNorm concept names from the terminology records before they are committed; `--check` fails if any remain | nothing | edits `results/` in place |

Every script prints its usage with `--help`. The fault, reproducibility and DQD
experiments run on `tests/fixtures/` and need no data access; the others need
MIMIC-IV, which PhysioNet provides to credentialed users, and a vocabulary from Athena.

## Records

The records in `results/` were written from the builds the paper describes. Where a tool
records provenance it is in the record itself: the software commit, the dataset
configuration hash, the vocabulary version, the run identifiers. Concept names from
licensed vocabularies have been stripped; the identifiers, ranks and scores remain, and
anyone with the same vocabulary build can regenerate the full records.

Two of the fixture experiments also run as tests on every push:

```bash
.venv/bin/pytest tests/integration/test_fault_detection.py tests/integration/test_reproducibility.py
```
