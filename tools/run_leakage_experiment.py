"""Does the leakage the contract blocks actually change a model's answer?

Every other experiment in this paper measures the detector. Fault injection asks whether
a check fires; it does not ask what happens if nobody looks. That leaves the central
claim -- that these conversion faults damage the model trained on the output -- as an
argument rather than a measurement, and an argument is not evidence.

This measures it. One cohort, one task, one model, three feature matrices that differ in
exactly one rule about *when a fact became knowable*:

  respects_availability   a fact is a feature only if both its time and its
                          `available_time` fall at or before the prediction point. This
                          is what the converter publishes.

  ignores_availability    a fact is a feature if its own time falls at or before the
                          prediction point. This is what a converter that never carried
                          `available_time` produces. In MIMIC-IV the two differ because
                          the source distinguishes them: a laboratory result is timed at
                          specimen collection but becomes visible at `storetime`.

  diagnoses_at_admission  as above, plus every diagnosis coded for the index admission,
                          dated to the admission. Nobody invented this fault. It is what
                          an OMOP CONDITION_OCCURRENCE built from an admission-level
                          diagnosis table looks like, because that table carries no date
                          of its own and the admission date is the only one to hand.

Nothing else moves between arms: same cohort, same labels, same split, same regulariser.
A difference in held-out AUROC is therefore attributable to the rule. The horizon is
swept because the size of a leak depends on it -- an availability lag of an hour is
invisible at a two-day horizon and material at a six-hour one -- and a single horizon
would let one arbitrary choice decide the answer.

Usage::

    python tools/run_leakage_experiment.py --meds $WORK/mimiciv/meds \\
        --scratch /path/to/scratch --out results/leakage_downstream.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.paths import portable_work_path  # noqa: E402

# The prediction point and the cohort floor. Twenty-four hours is the conventional
# horizon for this task; the 48-hour floor keeps the prediction point well inside the
# stay, so the label is not decided before the window closes.
HORIZONS_HOURS = (6, 12, 24, 48)
MIN_STAY_HOURS = 72
ARMS = ("respects_availability", "ignores_availability", "diagnoses_at_admission")


def connect(scratch: Path, threads: int):
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    con.execute("PRAGMA preserve_insertion_order = false")
    con.execute("SET temp_directory = ?", [str(scratch)])
    con.execute("SET memory_limit = '60GB'")
    con.execute(f"SET threads = {threads}")
    return con


def build_cohort(con, meds: Path, scratch: Path) -> Path:
    """First qualifying hospital admission per subject, with its label.

    The label is death during the admission. It is derived from the death event rather
    than from a discharge disposition so that it comes through the same conversion the
    features do -- a label read from the source and features read from the output would
    be measuring two pipelines.
    """
    out = scratch / "cohort.parquet"
    glob = str(meds / "data" / "*" / "*.parquet")
    con.execute(
        f"""
        COPY (
            WITH admission AS (
                SELECT subject_id, encounter_id, time AS admit, end_time AS discharge,
                       row_number() OVER (PARTITION BY subject_id ORDER BY time) AS seq
                FROM read_parquet('{glob}')
                WHERE source_table = 'admissions'
                  AND time IS NOT NULL AND end_time IS NOT NULL
                  AND date_diff('hour', time, end_time) >= {MIN_STAY_HOURS}
            ),
            first_admission AS (SELECT * FROM admission WHERE seq = 1),
            death AS (
                SELECT subject_id, min(time) AS died
                FROM read_parquet('{glob}')
                WHERE event_kind = 'death' AND time IS NOT NULL
                GROUP BY 1
            )
            SELECT a.subject_id,
                   a.admit,
                   a.discharge,
                   a.encounter_id,
                   CASE WHEN d.died IS NOT NULL AND d.died <= a.discharge + INTERVAL 1 DAY
                        THEN 1 ELSE 0 END AS label,
                   s.split
            FROM first_admission a
            LEFT JOIN death d USING (subject_id)
            JOIN read_parquet('{meds / "metadata" / "subject_splits.parquet"}') s USING (subject_id)
        ) TO '{out}' (FORMAT parquet)
        """
    )
    return out


def build_features(con, meds: Path, cohort: Path, scratch: Path, horizon: int) -> Path:
    """Per (subject, arm, code) counts under each rule, from a single scan.

    All three arms are emitted from one scan rather than three, so there is no chance of
    them seeing different data because of a filter typo in one of them.

    A static fact -- sex, birth date -- carries no time. It is knowable at every
    prediction point under every rule, so it is in all three arms; excluding it would
    make the arms differ in something other than the rule under test.
    """
    out = scratch / f"features_{horizon}h.parquet"
    glob = str(meds / "data" / "*" / "*.parquet")
    con.execute(
        f"""
        COPY (
            WITH c AS (
                SELECT subject_id, encounter_id AS index_encounter,
                       admit, admit + INTERVAL {horizon} HOUR AS predict_at
                FROM read_parquet('{cohort}')
            ),
            e AS (
                SELECT m.subject_id, m.code, m.time, m.available_time, m.event_kind,
                       m.encounter_id, c.predict_at, c.admit, c.index_encounter
                FROM read_parquet('{glob}') m JOIN c USING (subject_id)
            ),
            marked AS (
                SELECT subject_id, code,
                       (time IS NULL OR time <= predict_at) AS in_window,
                       -- Absent availability means the fact was knowable when it
                       -- happened, which is the converter's own convention.
                       (time IS NULL OR coalesce(available_time, time) <= predict_at) AS knowable,
                       -- The third arm's rule: a diagnosis recorded for this admission is
                       -- backdated to the admission itself. This is not a corruption
                       -- anyone invented -- it is what an OMOP CONDITION_OCCURRENCE built
                       -- from an admission-level diagnosis table looks like, because the
                       -- diagnosis table carries no date of its own and the admission date
                       -- is the one thing available to put in the column.
                       (event_kind = 'condition' AND encounter_id IS NOT NULL
                        AND encounter_id = index_encounter AND admit <= predict_at) AS backdated
                FROM e
            )
            SELECT subject_id, code, 'ignores_availability' AS arm, count(*) AS n
            FROM marked WHERE in_window GROUP BY 1, 2
            UNION ALL
            SELECT subject_id, code, 'respects_availability' AS arm, count(*) AS n
            FROM marked WHERE in_window AND knowable GROUP BY 1, 2
            UNION ALL
            SELECT subject_id, code, 'diagnoses_at_admission' AS arm, count(*) AS n
            FROM marked WHERE (in_window AND knowable) OR backdated GROUP BY 1, 2
        ) TO '{out}' (FORMAT parquet)
        """
    )
    return out


def fit_and_score(cohort_path: Path, features_path: Path, arm: str, seed: int) -> dict:
    import numpy as np
    import polars as pl
    from scipy.sparse import csr_matrix
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score

    cohort = pl.read_parquet(cohort_path).sort("subject_id")
    feats = pl.read_parquet(features_path).filter(pl.col("arm") == arm)

    subjects = cohort["subject_id"].to_list()
    row_of = {s: i for i, s in enumerate(subjects)}
    # The feature vocabulary is fixed by the training split of *this* arm. Sharing one
    # vocabulary across arms would let the leaked arm's extra codes appear as always-zero
    # columns in the clean arm, which is a different experiment.
    codes = sorted(feats["code"].unique().to_list())
    col_of = {c: i for i, c in enumerate(codes)}

    rows = np.fromiter((row_of[s] for s in feats["subject_id"]), dtype=np.int64, count=len(feats))
    cols = np.fromiter((col_of[c] for c in feats["code"]), dtype=np.int64, count=len(feats))
    # log1p on counts: a patient with two hundred of one lab should not be two hundred
    # times the evidence of a patient with one.
    vals = np.log1p(feats["n"].to_numpy().astype(np.float64))
    matrix = csr_matrix((vals, (rows, cols)), shape=(len(subjects), len(codes)))

    split = cohort["split"].to_numpy()
    label = cohort["label"].to_numpy()
    train, test = split == "train", split == "held_out"

    model = LogisticRegression(max_iter=2000, C=1.0, solver="liblinear", random_state=seed)
    model.fit(matrix[train], label[train])
    score = model.decision_function(matrix[test])
    return {
        "arm": arm,
        "n_features": len(codes),
        "n_train": int(train.sum()),
        "n_held_out": int(test.sum()),
        "held_out_auroc": round(float(roc_auc_score(label[test], score)), 4),
        "held_out_auprc": round(float(average_precision_score(label[test], score)), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--meds", type=Path, required=True, help="a built MEDS root (data/ and metadata/)")
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--seed", type=int, default=20260826)
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS_HOURS))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    args.scratch.mkdir(parents=True, exist_ok=True)
    con = connect(args.scratch, args.threads)

    t0 = time.time()
    cohort = build_cohort(con, args.meds, args.scratch)
    stats = con.execute(
        f"""SELECT count(*), sum(label), sum(CASE WHEN split='train' THEN 1 ELSE 0 END),
                   sum(CASE WHEN split='held_out' THEN 1 ELSE 0 END)
            FROM read_parquet('{cohort}')"""
    ).fetchone()
    print(f"cohort: {stats[0]:,} subjects, {stats[1]:,} deaths ({time.time() - t0:.0f}s)", flush=True)

    horizons = []
    for hours in [int(h) for h in args.horizons.split(",")]:
        features = build_features(con, args.meds, cohort, args.scratch, hours)
        counts = dict(con.execute(
            f"SELECT arm, count(*) FROM read_parquet('{features}') GROUP BY 1"
        ).fetchall())
        arms = [fit_and_score(cohort, features, arm, args.seed) for arm in ARMS]
        by = {a["arm"]: a for a in arms}
        base = by["respects_availability"]["held_out_auroc"]
        horizons.append({
            "horizon_hours": hours,
            "subject_code_pairs": counts,
            "arms": arms,
            "auroc_inflation": {
                arm: round(by[arm]["held_out_auroc"] - base, 4)
                for arm in ARMS if arm != "respects_availability"
            },
        })
        print(f"[{hours}h] " + "  ".join(
            f"{a['arm']}={a['held_out_auroc']}" for a in arms), flush=True)
        features.unlink(missing_ok=True)
    con.close()

    summary = {
        "meds_root": portable_work_path(args.meds),
        "task": "in-hospital death, predicted at a fixed horizon after admission",
        "cohort": {
            "min_stay_hours": MIN_STAY_HOURS,
            "n_subjects": stats[0],
            "n_positive": stats[1],
            "prevalence_pct": round(100.0 * stats[1] / stats[0], 2),
            "n_train": stats[2],
            "n_held_out": stats[3],
        },
        "horizons": horizons,
        "seed": args.seed,
        "seconds": round(time.time() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))

    print(f"\n{stats[0]:,} subjects, {stats[1]:,} deaths ({summary['cohort']['prevalence_pct']}%)")
    print(f"{'horizon':>8} " + "".join(f"{a:>26}" for a in ARMS))
    for h in horizons:
        by = {a["arm"]: a for a in h["arms"]}
        print(f"{h['horizon_hours']:>7}h " + "".join(
            f"{by[a]['held_out_auroc']:>26}" for a in ARMS))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
