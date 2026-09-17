"""Cross-rule evaluation: train under one time rule, evaluate under another.

Answers the question raised in review (Lin, 2026-09-14): a model trained with diagnoses
back-dated to admission -- how does it fare once deployed where those diagnoses only
arrive at discharge? `run_leakage_experiment.py` scores each model only on its own rule,
so only the diagonal of the train x test matrix exists. This fills the other six cells
per horizon without changing what the diagonal is built from: the cohort, features,
split, seed, model and bootstrap are imported from that script and used as they are.

The feature vocabulary always comes from the *training* arm: a model's columns are the
columns it was fitted on, so a test arm's features are laid out over the training
vocabulary. A test arm whose rule never sees some of those codes simply presents
all-zero columns, which is the deployment condition being measured.

    python tools/run_leakage_transfer.py --meds $EHR_WORK_ROOT/mimiciv/meds \
        --scratch /path/to/scratch --out results/leakage_transfer.json

Writes to a new file; results/leakage_downstream.json and the figure built from it are
left alone.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from run_leakage_experiment import (  # noqa: E402
    ARMS, BASE_ARM, BOOTSTRAP_RESAMPLES, HORIZONS_HOURS, MIN_STAY_HOURS,
    build_cohort, build_features, connect,
)
from ehr2trace.paths import portable_work_path  # noqa: E402

SHORT = {"respects_availability": "A", "ignores_availability": "B", "diagnoses_at_admission": "C"}
LEAKED_ARM = "diagnoses_at_admission"


def cell(train_arm: str, test_arm: str) -> str:
    return f"{train_arm}->{test_arm}"


def build_matrix(cohort, feats, arm: str, codes: list[str] | None = None):
    """The design matrix of one arm over a vocabulary, and the vocabulary used.

    With ``codes`` unset the vocabulary is the codes seen in this arm's training split,
    exactly as ``run_leakage_experiment.fit_and_score`` derives it. With ``codes`` given
    it is reused unchanged, so a test matrix has the training model's columns.
    """
    import numpy as np
    import polars as pl
    from scipy.sparse import csr_matrix

    subjects = cohort["subject_id"].to_list()
    row_of = {s: i for i, s in enumerate(subjects)}
    feats = feats.filter(pl.col("arm") == arm)
    if codes is None:
        train_subjects = set(cohort.filter(pl.col("split") == "train")["subject_id"].to_list())
        codes = sorted(feats.filter(pl.col("subject_id").is_in(train_subjects))["code"].unique().to_list())
    col_of = {c: i for i, c in enumerate(codes)}
    feats = feats.filter(pl.col("code").is_in(codes))
    rows = np.fromiter((row_of[s] for s in feats["subject_id"]), dtype=np.int64, count=len(feats))
    cols = np.fromiter((col_of[c] for c in feats["code"]), dtype=np.int64, count=len(feats))
    vals = np.log1p(feats["n"].to_numpy().astype(np.float64))
    return csr_matrix((vals, (rows, cols)), shape=(len(subjects), len(codes))), codes


def fit(matrix, cohort, seed: int):
    from sklearn.linear_model import LogisticRegression

    train = cohort["split"].to_numpy() == "train"
    label = cohort["label"].to_numpy()
    model = LogisticRegression(max_iter=2000, C=1.0, solver="liblinear", random_state=seed)
    model.fit(matrix[train], label[train])
    return model


def score(model, matrix, cohort):
    from sklearn.metrics import average_precision_score, roc_auc_score

    test = cohort["split"].to_numpy() == "held_out"
    truth = cohort["label"].to_numpy()[test]
    s = model.decision_function(matrix[test])
    return truth, s, round(float(roc_auc_score(truth, s)), 4), round(float(average_precision_score(truth, s)), 4)


def paired_intervals(truth, scores: dict[str, "np.ndarray"], differences: dict[str, tuple[str, str]],
                     seed: int, resamples: int = BOOTSTRAP_RESAMPLES):
    """Percentile bootstrap over held-out patients, every cell scored on the same resample.

    Same procedure as ``run_leakage_experiment._intervals``; the keys are cells rather
    than arms, and the paired differences are the ones named in ``differences``
    (``name -> (minuend cell, subtrahend cell)``).
    """
    import numpy as np
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    n = len(truth)
    draws = {k: [] for k in scores}
    diffs = {k: [] for k in differences}
    for _ in range(resamples):
        idx = rng.integers(0, n, n)
        sample = truth[idx]
        if sample.min() == sample.max():
            continue
        scored = {k: roc_auc_score(sample, s[idx]) for k, s in scores.items()}
        for k, v in scored.items():
            draws[k].append(v)
        for k, (a, b) in differences.items():
            diffs[k].append(scored[a] - scored[b])

    def pct(values):
        values = np.sort(np.asarray(values))
        return [round(float(values[int(0.025 * len(values))]), 4),
                round(float(values[int(0.975 * len(values))]), 4)]

    return {k: pct(v) for k, v in draws.items()}, {k: pct(v) for k, v in diffs.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--meds", type=Path, required=True, help="the MEDS build the existing results were run on")
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS_HOURS))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--baseline", type=Path, default=Path(__file__).resolve().parent / "results" / "leakage_downstream.json",
                    help="existing diagonal results to reproduce (acceptance check 1)")
    args = ap.parse_args()

    import numpy as np
    import polars as pl

    args.scratch.mkdir(parents=True, exist_ok=True)
    con = connect(args.scratch, args.threads)
    t0 = time.time()
    cohort_path = build_cohort(con, args.meds, args.scratch)
    stats = con.execute(
        f"""SELECT count(*), sum(label), sum(CASE WHEN split='train' THEN 1 ELSE 0 END),
                   sum(CASE WHEN split='held_out' THEN 1 ELSE 0 END)
            FROM read_parquet('{cohort_path}')"""
    ).fetchone()
    print(f"cohort: {stats[0]:,} subjects, {stats[1]:,} deaths ({time.time() - t0:.0f}s)", flush=True)
    cohort = pl.read_parquet(cohort_path).sort("subject_id")

    baseline = {}
    if args.baseline.exists():
        for h in json.loads(args.baseline.read_text())["horizons"]:
            baseline[h["horizon_hours"]] = {a["arm"]: a["held_out_auroc"] for a in h["arms"]}

    A, C = BASE_ARM, LEAKED_ARM
    horizons = []
    for hours in [int(h) for h in args.horizons.split(",")]:
        features_path = build_features(con, args.meds, cohort_path, args.scratch, hours)
        feats = pl.read_parquet(features_path)

        vocab: dict[str, list[str]] = {}
        models: dict[str, object] = {}
        for train_arm in ARMS:
            matrix, codes = build_matrix(cohort, feats, train_arm)
            vocab[train_arm] = codes
            models[train_arm] = fit(matrix, cohort, args.seed)

        cells, scores, truth = [], {}, None
        zero_columns: dict[str, dict[str, int]] = {}
        for train_arm in ARMS:
            for test_arm in ARMS:
                matrix, codes = build_matrix(cohort, feats, test_arm, vocab[train_arm])
                assert codes == vocab[train_arm] and matrix.shape[1] == len(vocab[train_arm])
                truth, s, auroc, auprc = score(models[train_arm], matrix, cohort)
                key = cell(train_arm, test_arm)
                scores[key] = s
                held = matrix[cohort["split"].to_numpy() == "held_out"]
                nonzero_cols = int((held.getnnz(axis=0) > 0).sum())
                icd = [i for i, c in enumerate(codes) if "ICD" in c.upper()]
                icd_nonzero = int((held[:, icd].getnnz(axis=0) > 0).sum()) if icd else 0
                zero_columns[key] = {"columns": len(codes), "all_zero_in_held_out": len(codes) - nonzero_cols,
                                     "icd_columns": len(icd), "icd_columns_all_zero": len(icd) - icd_nonzero}
                cells.append({"train_arm": train_arm, "test_arm": test_arm, "cell": f"{SHORT[train_arm]}->{SHORT[test_arm]}",
                              "n_features": len(codes), "n_held_out": int(len(truth)),
                              "held_out_auroc": auroc, "held_out_auprc": auprc})

        differences = {
            "evaluation_inflation (C->C minus C->A)": (cell(C, C), cell(C, A)),
            "training_damage (A->A minus C->A)": (cell(A, A), cell(C, A)),
            "B: evaluation_inflation (B->B minus B->A)": (cell("ignores_availability", "ignores_availability"),
                                                          cell("ignores_availability", A)),
            "B: training_damage (A->A minus B->A)": (cell(A, A), cell("ignores_availability", A)),
        }
        cell_ci, diff_ci = paired_intervals(truth, scores, differences, args.seed)
        by = {c["cell"]: c for c in cells}
        for c in cells:
            c["held_out_auroc_ci"] = cell_ci[cell(c["train_arm"], c["test_arm"])]
        point = {k: round(by[f"{SHORT[a.split('->')[0]]}->{SHORT[a.split('->')[1]]}"]["held_out_auroc"]
                          - by[f"{SHORT[b.split('->')[0]]}->{SHORT[b.split('->')[1]]}"]["held_out_auroc"], 4)
                 for k, (a, b) in differences.items()}
        diag_check = {SHORT[arm]: {"here": by[f"{SHORT[arm]}->{SHORT[arm]}"]["held_out_auroc"],
                                   "existing": baseline.get(hours, {}).get(arm)}
                      for arm in ARMS}
        for v in diag_check.values():
            v["reproduced"] = (v["existing"] is not None and abs(v["here"] - v["existing"]) <= 0.001)
        horizons.append({
            "horizon_hours": hours,
            "cells": cells,
            "headline": {"A->A": by["A->A"]["held_out_auroc"], "C->C": by["C->C"]["held_out_auroc"],
                         "C->A": by["C->A"]["held_out_auroc"]},
            "differences": point, "differences_ci": diff_ci,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "acceptance": {
                "diagonal_reproduces_existing": diag_check,
                "columns_fixed_by_training_vocabulary": all(
                    by[f"{SHORT[t]}->{SHORT[e]}"]["n_features"] == len(vocab[t]) for t in ARMS for e in ARMS),
                "C_vocabulary_superset_of_A": set(vocab[A]) <= set(vocab[C]),
                "vocabulary_sizes": {SHORT[a]: len(v) for a, v in vocab.items()},
                "held_out_patients_per_cell": sorted({c["n_held_out"] for c in cells}),
                "zero_columns": zero_columns,
            },
        })
        h = horizons[-1]
        print(f"[{hours}h] A->A={h['headline']['A->A']}  C->C={h['headline']['C->C']}  C->A={h['headline']['C->A']}  "
              f"inflation={point['evaluation_inflation (C->C minus C->A)']}  damage={point['training_damage (A->A minus C->A)']}"
              f"  diagonal reproduced: {[v['reproduced'] for v in diag_check.values()]}", flush=True)
        features_path.unlink(missing_ok=True)
    con.close()

    summary = {
        "meds_root": portable_work_path(args.meds),
        "task": "in-hospital death, predicted at a fixed horizon after admission; models trained under one time rule and scored under each",
        "arms": {SHORT[a]: a for a in ARMS},
        "cohort": {"min_stay_hours": MIN_STAY_HOURS, "n_subjects": stats[0], "n_positive": stats[1],
                   "prevalence_pct": round(100.0 * stats[1] / stats[0], 2), "n_train": stats[2], "n_held_out": stats[3]},
        "horizons": horizons,
        "seed": args.seed,
        "seconds": round(time.time() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(f"\n{'horizon':>8} {'A->A':>8} {'C->C':>8} {'C->A':>8} {'inflation':>10} {'damage':>8}")
    for h in horizons:
        d = h["differences"]
        print(f"{h['horizon_hours']:>7}h {h['headline']['A->A']:>8} {h['headline']['C->C']:>8} {h['headline']['C->A']:>8} "
              f"{d['evaluation_inflation (C->C minus C->A)']:>10} {d['training_damage (A->A minus C->A)']:>8}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
