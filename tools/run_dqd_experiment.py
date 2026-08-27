"""Run the real OHDSI Data Quality Dashboard over the same injected faults.

The claim this experiment exists to test is one this project makes about itself: that
the standard, mature tool for validating an OMOP CDM instance cannot see the class of
conversion fault the check suite in ``ehr2cdm.validate`` is built for. Stated as an
argument that is just an assertion with a plausible mechanism behind it. Stated as a
number -- DQD ran N checks over the same seventeen corrupted builds and detected M of
them -- it is a measurement, and a reader can disagree with it on evidence.

The mechanism the argument rests on is that DQD assesses a *built database*: it asks
whether the CDM instance in front of it is internally coherent and conformant, not
whether it is a faithful rendering of the export it came from. A fault that lands
inside the CDM's own rules -- a date that is real but wrong, a concept that exists but
belongs to a different patient's timeline, a cohort label wearing a diagnosis code --
leaves a database DQD has no reason to complain about. The expected result is
therefore a low number, and a low number is the finding, not a failure of setup. No
threshold, check list or fault was tuned in either direction.

Three details decide whether the comparison is fair, and all three go the way that
gives DQD its best shot:

  real DQD          The upstream R package against real PostgreSQL over JDBC, not a
                    reimplementation of its checks. See ``tools/dqd/Dockerfile``.
  real vocabulary   The full Athena download loaded into the CDM schema, so the
                    concept-level and vocabulary-conformance checks have something to
                    work with rather than being trivially not-applicable.
  faults propagate  Canonical-layer faults are injected and *then* the OMOP layer is
                    rebuilt from the corrupted canonical, so the corruption actually
                    reaches the database DQD reads. Handing DQD a CDM built before the
                    injection would guarantee it saw nothing and prove only that the
                    experiment was rigged.

Detection is defined exactly as in ``run_fault_experiment.py``: a fault is detected if
some DQD check fails that does not fail on the clean baseline. Checks failing on the
untouched build are not detectors and are subtracted.

MEDS-layer faults are run and reported like the others. DQD has no view of MEDS at
all, so their outcome is structurally determined rather than measured -- which is
itself part of the point, and is flagged in the results rather than quietly dropped.

Prerequisites (see ``tools/dqd/Dockerfile``)::

    docker build -t ehr2cdm-dqd tools/dqd
    docker network create dqdlab
    docker run -d --name dqdlab-pg --network dqdlab \\
        -e POSTGRES_USER=dqd -e POSTGRES_PASSWORD=dqd -e POSTGRES_DB=dqd \\
        -v dqdlab-pgdata:/var/lib/postgresql/data \\
        -v /path/to/omop_vocab:/vocab:ro -v /path/to/xfer:/xfer postgres:16

Usage::

    python tools/run_dqd_experiment.py --dataset datasets/ctpe_shape.yaml \\
        --built $WORK/ctpe_shape --work $WORK/_dqdlab --out results/dqd_baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ehr2cdm.config import DatasetConfig, load_dataset_config  # noqa: E402
from ehr2cdm.faults import FAULTS, clone_work_tree  # noqa: E402
from ehr2cdm.omop import build_omop  # noqa: E402
from ehr2cdm.paths import WorkLayout  # noqa: E402

#: Loaded once and left alone between runs. Three gigabytes of Athena CSV takes
#: minutes to copy in and is identical for every fault; reloading it per run would
#: dominate the experiment's runtime and change nothing about its result.
VOCABULARY_FILES = {
    "concept": "CONCEPT.csv",
    "vocabulary": "VOCABULARY.csv",
    "domain": "DOMAIN.csv",
    "concept_class": "CONCEPT_CLASS.csv",
    "relationship": "RELATIONSHIP.csv",
    "concept_synonym": "CONCEPT_SYNONYM.csv",
    "concept_ancestor": "CONCEPT_ANCESTOR.csv",
    "drug_strength": "DRUG_STRENGTH.csv",
    "concept_relationship": "CONCEPT_RELATIONSHIP.csv",
}

#: Athena ships tab-separated text with no quoting, and concept names contain bare
#: double quotes. Pointing QUOTE at a byte that cannot occur stops PostgreSQL reading
#: those as field delimiters.
_COPY_OPTS = r"FORMAT csv, DELIMITER E'\t', HEADER true, QUOTE E'\b'"

_VOCAB_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_concept_id ON {s}.concept (concept_id)",
    "CREATE INDEX IF NOT EXISTS ix_concept_code ON {s}.concept (concept_code, vocabulary_id)",
    "CREATE INDEX IF NOT EXISTS ix_ca_anc ON {s}.concept_ancestor (ancestor_concept_id)",
    "CREATE INDEX IF NOT EXISTS ix_ca_desc ON {s}.concept_ancestor (descendant_concept_id)",
    "CREATE INDEX IF NOT EXISTS ix_cr_1 ON {s}.concept_relationship (concept_id_1)",
    "CREATE INDEX IF NOT EXISTS ix_cs_id ON {s}.concept_synonym (concept_id)",
    "CREATE INDEX IF NOT EXISTS ix_ds_drug ON {s}.drug_strength (drug_concept_id)",
)


# --------------------------------------------------------------------------------
# postgres, driven through the container so the host needs no client
# --------------------------------------------------------------------------------


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        # The default CalledProcessError swallows stderr, and every interesting failure
        # here is a PostgreSQL message that only exists in stderr.
        raise RuntimeError(f"{' '.join(cmd[:6])} ... failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


class Postgres:
    """The CDM database DQD reads. One schema, reloaded between runs."""

    def __init__(self, container: str, schema: str, user: str, database: str, password: str):
        self.container = container
        self.schema = schema
        self.user = user
        self.database = database
        self.password = password

    def sql(self, statement: str, timeout: int = 3600) -> str:
        proc = _run(
            [
                "docker", "exec", "-e", f"PGPASSWORD={self.password}", self.container,
                "psql", "-U", self.user, "-d", self.database,
                "-v", "ON_ERROR_STOP=1", "-qAt", "-c", statement,
            ],
            timeout=timeout,
        )
        return proc.stdout.strip()

    def rows(self, statement: str) -> list[list[str]]:
        out = self.sql(statement)
        return [line.split("|") for line in out.splitlines() if line]

    def tables(self) -> list[str]:
        return [
            r[0]
            for r in self.rows(
                f"SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{self.schema}' ORDER BY table_name"
            )
        ]

    def columns(self, table: str) -> list[str]:
        return [
            r[0]
            for r in self.rows(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = '{self.schema}' AND table_name = '{table}' "
                f"ORDER BY ordinal_position"
            )
        ]

    def count(self, table: str) -> int:
        return int(self.sql(f"SELECT count(*) FROM {self.schema}.{table}"))


def load_vocabulary(pg: Postgres, mount: str, reload: bool) -> dict[str, int]:
    """Copy the Athena download into the CDM schema, once.

    A vocabulary that is present but empty is worse than no vocabulary: DQD's
    concept-level checks would report *not applicable* rather than fail, and the
    experiment would understate what DQD can do for a reason that has nothing to do
    with DQD.
    """
    if not reload and pg.count("concept") > 0:
        return {t: pg.count(t) for t in VOCABULARY_FILES}
    for table, filename in VOCABULARY_FILES.items():
        print(f"  loading {table} ...", flush=True)
        pg.sql(f"TRUNCATE {pg.schema}.{table}")
        pg.sql(f"COPY {pg.schema}.{table} FROM '{mount}/{filename}' WITH ({_COPY_OPTS})")
    for statement in _VOCAB_INDEXES:
        pg.sql(statement.format(s=pg.schema))
    pg.sql("ANALYZE")
    return {t: pg.count(t) for t in VOCABULARY_FILES}


def load_cdm(pg: Postgres, duckdb_path: Path, xfer_host: Path, xfer_mount: str) -> dict[str, str]:
    """Replace the clinical tables with this run's build. Vocabulary is left alone.

    The transfer is a CSV round trip rather than a direct copy because the two engines
    share no wire format. It is exact for this fixture -- every value survives -- but
    on a build with embedded newlines or non-UTF-8 text it is the first thing to
    suspect.

    Returns a digest per table, taken over the sorted rows on the way past. Comparing
    those against the baseline's answers the question that has to be settled before
    "DQD did not detect this" means anything: did the corruption reach the database
    DQD was pointed at? A miss on a table DQD never saw change is not a miss.
    """
    import duckdb

    clinical = [t for t in pg.tables() if t not in VOCABULARY_FILES]
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        present = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        digests: dict[str, str] = {}
        for table in clinical:
            pg.sql(f"TRUNCATE {pg.schema}.{table}")
            if table not in present:
                continue
            target = pg.columns(table)
            source = {c[1] for c in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
            # A column the build never published is loaded as NULL rather than skipped:
            # DQD's requiredness checks should see the absence, not miss the column.
            projection = ", ".join(f'"{c}"' if c in source else f'NULL AS "{c}"' for c in target)
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if not n:
                continue
            csv_path = xfer_host / f"{table}.csv"
            # ORDER BY ALL so the digest is of the table's contents and not of whatever
            # order this particular rebuild happened to materialise them in.
            con.execute(
                f"COPY (SELECT {projection} FROM {table} ORDER BY ALL) TO '{csv_path}' "
                "(FORMAT CSV, HEADER, DELIMITER E'\\t', NULL '')"
            )
            cols = ", ".join(f'"{c}"' for c in target)
            pg.sql(
                f"COPY {pg.schema}.{table} ({cols}) FROM '{xfer_mount}/{table}.csv' "
                r"WITH (FORMAT csv, DELIMITER E'\t', HEADER true)"
            )
            digests[table] = f"{pg.count(table)}:{hashlib.md5(csv_path.read_bytes()).hexdigest()[:12]}"
            csv_path.unlink(missing_ok=True)
    finally:
        con.close()
    pg.sql(f"ANALYZE {pg.schema}.person")
    return digests


# --------------------------------------------------------------------------------
# DQD
# --------------------------------------------------------------------------------


def run_dqd(args: argparse.Namespace, label: str, out_host: Path) -> dict[str, Any]:
    """One full DQD pass. Returns the parsed upstream result document."""
    cmd = [
        "docker", "run", "--rm", "--network", args.network,
        "-e", f"PGHOST={args.pg_container}", "-e", "PGPORT=5432",
        "-e", f"PGDATABASE={args.pg_database}", "-e", f"PGUSER={args.pg_user}",
        "-e", f"PGPASSWORD={args.pg_password}",
        "-v", f"{out_host}:/out",
        args.image, "Rscript", "/opt/dqd/run_dqd.R", label, args.schema, "/out",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.dqd_timeout)
    log = out_host / f"{label}.log"
    log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    result_path = out_host / f"{label}.json"
    if not result_path.exists():
        raise RuntimeError(f"DQD produced no result for {label}; see {log}")
    return json.loads(result_path.read_text(encoding="utf-8"))


def _as_bool(value: Any) -> bool:
    # DQD writes these as 0/1, sometimes wrapped in a single-element array by
    # jsonlite's default vector handling.
    if isinstance(value, list):
        value = value[0] if value else 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def check_state(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the DQD result by check id.

    An errored or not-applicable check is neither a pass nor a fail, and is recorded
    as such: counting an error as a detection would credit DQD with catching a fault
    when what actually happened is that a query fell over.
    """
    state: dict[str, dict[str, Any]] = {}
    for row in document.get("CheckResults", []):
        check_id = str(_scalar(row.get("checkId")))
        state[check_id] = {
            "check_id": check_id,
            "check_name": _scalar(row.get("checkName")),
            "check_level": _scalar(row.get("checkLevel")),
            "category": _scalar(row.get("category")),
            "subcategory": _scalar(row.get("subcategory")),
            "cdm_table": _scalar(row.get("cdmTableName")),
            "cdm_field": _scalar(row.get("cdmFieldName")),
            "concept_id": _scalar(row.get("conceptId")),
            "failed": _as_bool(row.get("failed")),
            "passed": _as_bool(row.get("passed")),
            "error": _as_bool(row.get("isError")),
            "not_applicable": _as_bool(row.get("notApplicable")),
            "threshold": _scalar(row.get("thresholdValue")),
            "pct_violated": _scalar(row.get("pctViolatedRows")),
            "n_violated": _scalar(row.get("numViolatedRows")),
        }
    return state


def _failing(state: dict[str, dict[str, Any]]) -> set[str]:
    return {k for k, v in state.items() if v["failed"] and not v["error"]}


def _describe(state: dict[str, dict[str, Any]], ids: Iterable[str]) -> list[dict[str, Any]]:
    out = []
    for check_id in sorted(ids):
        row = state[check_id]
        out.append(
            {
                "check_id": check_id,
                "check_name": row["check_name"],
                "check_level": row["check_level"],
                "cdm_table": row["cdm_table"],
                "cdm_field": row["cdm_field"],
                "n_violated": row["n_violated"],
                "pct_violated": row["pct_violated"],
                "threshold": row["threshold"],
            }
        )
    return out


# --------------------------------------------------------------------------------
# one build under test
# --------------------------------------------------------------------------------


def prepare_build(
    cfg: DatasetConfig,
    built: Path,
    scratch: Path,
    fault: Any | None,
    vocabulary: Path | None,
    rebuild: bool,
) -> tuple[WorkLayout, str]:
    """Clone the built tree, inject, and leave an OMOP database for DQD to read.

    Injection order is decided by the layer, and it matters. A canonical-layer fault
    has to be injected *before* the OMOP rebuild or the corruption never reaches the
    CDM; an OMOP-layer fault has to be injected *after* it, because the rebuild drops
    and recreates the database it would otherwise have been written into.
    """
    clone_work_tree(built, scratch)
    layout = WorkLayout(root=scratch, dataset_id=cfg.dataset_id)
    if fault is None:
        if rebuild:
            build_omop(cfg, layout, vocabulary_dir=vocabulary)
        return layout, "clean baseline"

    if fault.layer == "canonical":
        effect = fault.apply(layout, cfg)
        if rebuild:
            build_omop(cfg, layout, vocabulary_dir=vocabulary)
        return layout, effect

    if rebuild:
        build_omop(cfg, layout, vocabulary_dir=vocabulary)
    return layout, fault.apply(layout, cfg)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--built", type=Path, required=True, help="an already-built work tree for this dataset")
    ap.add_argument("--work", type=Path, required=True, help="scratch directory for the mutated clones")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--layers", default="canonical,omop,meds", help="which fault layers to run")
    ap.add_argument("--vocabulary", type=Path, default=os.environ.get("OMOP_VOCAB_DIR"))
    ap.add_argument("--image", default="ehr2cdm-dqd", help="the DQD image built from tools/dqd")
    ap.add_argument("--network", default="dqdlab")
    ap.add_argument("--pg-container", default="dqdlab-pg")
    ap.add_argument("--pg-user", default="dqd")
    ap.add_argument("--pg-database", default="dqd")
    ap.add_argument("--pg-password", default="dqd")
    ap.add_argument("--schema", default="cdm")
    ap.add_argument("--vocab-mount", default="/vocab", help="where the Athena CSVs are mounted in the postgres container")
    ap.add_argument("--xfer-mount", default="/xfer", help="where --work/out is mounted in the postgres container")
    ap.add_argument("--reload-vocabulary", action="store_true", help="reload the vocabulary even if it is already there")
    ap.add_argument(
        "--no-rebuild-omop",
        action="store_true",
        help="hand DQD the OMOP database as it was built before injection. Canonical-layer "
        "faults then cannot reach it, so this measures nothing except that fact -- kept "
        "only so the difference can be shown rather than asserted.",
    )
    ap.add_argument("--dqd-timeout", type=int, default=7200)
    args = ap.parse_args()

    cfg = load_dataset_config(args.dataset)
    wanted = {s.strip() for s in args.layers.split(",") if s.strip()}
    rebuild = not args.no_rebuild_omop
    vocabulary = Path(args.vocabulary) if args.vocabulary else None

    clones = args.work / "clones"
    dqd_out = args.work / "out"
    clones.mkdir(parents=True, exist_ok=True)
    dqd_out.mkdir(parents=True, exist_ok=True)

    pg = Postgres(args.pg_container, args.schema, args.pg_user, args.pg_database, args.pg_password)
    print(f"vocabulary -> {args.schema}")
    vocab_counts = load_vocabulary(pg, args.vocab_mount, args.reload_vocabulary)
    print(f"  concept={vocab_counts['concept']:,} concept_ancestor={vocab_counts['concept_ancestor']:,}")

    def one(label: str, fault: Any | None) -> tuple[dict[str, dict[str, Any]], str, dict[str, str]]:
        scratch = clones / label
        try:
            layout, effect = prepare_build(cfg, args.built, scratch, fault, vocabulary, rebuild)
            digests = load_cdm(pg, layout.omop_dir / "omop.duckdb", dqd_out, args.xfer_mount)
            return check_state(run_dqd(args, label, dqd_out)), effect, digests
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    t0 = time.time()
    baseline, _, baseline_digests = one("baseline", None)
    versions = json.loads((dqd_out / "versions.json").read_text(encoding="utf-8"))
    baseline_failing = _failing(baseline)
    errored = {k for k, v in baseline.items() if v["error"]}
    not_applicable = {k for k, v in baseline.items() if v["not_applicable"]}
    print(
        f"baseline: {len(baseline) - len(baseline_failing) - len(errored):,}/{len(baseline):,} DQD checks pass"
        f" ({len(not_applicable):,} not applicable, {len(errored):,} errored)"
        f"  [{round(time.time() - t0, 1)}s]"
    )
    if baseline_failing:
        print(f"  NOTE: already failing before injection: {sorted(baseline_failing)}")

    results = []
    for f in FAULTS:
        if f.layer not in wanted:
            continue
        t1 = time.time()
        try:
            after, effect, digests = one(f.id.lower(), f)
        except Exception as exc:  # a run that cannot be completed is recorded, not hidden
            results.append(
                {
                    "fault_id": f.id,
                    "layer": f.layer,
                    "silent": f.silent,
                    "description": f.description,
                    "effect": f"run failed: {type(exc).__name__}: {exc}",
                    "skipped": True,
                    "detected": False,
                    "detectors": [],
                    "n_detectors": 0,
                    "seconds": round(time.time() - t1, 1),
                }
            )
            print(f"  [ERROR   ] {f.id:44} {type(exc).__name__}: {str(exc)[:60]}")
            continue

        skipped = effect.startswith("skipped")
        detectors = sorted(_failing(after) - baseline_failing) if not skipped else []
        # A concept-level check is instantiated per concept found in the data, so a
        # fault that introduces a concept can produce a failing check that had no
        # counterpart on the clean run. It is still a detection -- it is not in the
        # baseline's failing set -- but the distinction is worth keeping visible.
        novel = sorted(c for c in detectors if c not in baseline)
        # cdm_source carries the build's own metadata and moves on every rebuild, so it
        # says nothing about whether the fault landed.
        changed = sorted(
            t for t in set(digests) | set(baseline_digests) if digests.get(t) != baseline_digests.get(t)
        )
        results.append(
            {
                "fault_id": f.id,
                "layer": f.layer,
                "silent": f.silent,
                "description": f.description,
                "origin": f.origin,
                "effect": effect,
                "skipped": skipped,
                "detected": bool(detectors),
                "detectors": _describe(after, detectors),
                "n_detectors": len(detectors),
                "detectors_absent_from_baseline": novel,
                "checks_run": len(after),
                "checks_failing": len(_failing(after)),
                "checks_errored": sum(1 for v in after.values() if v["error"]),
                "cdm_tables_changed": changed,
                "reached_the_cdm": bool(set(changed) - {"cdm_source"}),
                "seconds": round(time.time() - t1, 1),
            }
        )
        mark = "SKIP" if skipped else ("DETECTED" if detectors else "MISSED  ")
        print(f"  [{mark}] {f.id:44} {len(detectors):>2} detector(s)  {effect[:60]}")

    ran = [r for r in results if not r["skipped"]]
    detected = [r for r in ran if r["detected"]]
    summary = {
        "tool": "OHDSI DataQualityDashboard",
        "versions": versions,
        "image": args.image,
        "database": "postgresql 16",
        "dataset": cfg.dataset_id,
        "cdm_version": "5.4",
        "check_levels": ["TABLE", "FIELD", "CONCEPT"],
        "omop_rebuilt_from_injected_canonical": rebuild,
        "vocabulary_rows": vocab_counts,
        "not_run": {
            "COHORT": "DQD's cohort-scoped checks need a cohort definition and a populated "
            "COHORT table; this conversion publishes neither, so no cohort was passed and "
            "those checks were not instantiated.",
            "MEDS": "DQD reads an OMOP CDM database. The MEDS layer is outside what it can "
            "be pointed at, so the five MEDS-layer faults are reported as run but their "
            "outcome is structural rather than measured.",
        },
        "checks_total": len(baseline),
        "baseline_passing": len(baseline) - len(baseline_failing) - len(errored),
        "baseline_failing": _describe(baseline, baseline_failing),
        "baseline_errored": sorted(errored),
        "baseline_not_applicable": len(not_applicable),
        "baseline_cdm_digests": baseline_digests,
        "faults_total": len(results),
        "faults_run": len(ran),
        "faults_skipped": len(results) - len(ran),
        "faults_detected": len(detected),
        "faults_detected_ids": sorted(r["fault_id"] for r in detected),
        "faults_missed": sorted(r["fault_id"] for r in ran if not r["detected"]),
        # The two ways of missing are not the same finding. A fault that changed the CDM
        # and still drew no complaint is DQD looking at the corruption and passing it. A
        # fault that never reached the CDM is DQD being shown nothing -- which is the
        # more damning of the two, because it means the corruption lives in a layer no
        # CDM-level tool is pointed at.
        "faults_reaching_the_cdm": sum(1 for r in ran if r["reached_the_cdm"]),
        "faults_missed_though_present_in_the_cdm": sorted(
            r["fault_id"] for r in ran if not r["detected"] and r["reached_the_cdm"]
        ),
        "faults_never_reaching_the_cdm": sorted(r["fault_id"] for r in ran if not r["reached_the_cdm"]),
        "detection_rate": round(len(detected) / len(ran), 3) if ran else None,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(
        f"\nDQD {versions['DataQualityDashboard']}: {summary['faults_detected']}/{summary['faults_run']}"
        f" injected faults detected, {summary['checks_total']:,} checks per run"
    )
    if summary["faults_missed"]:
        print(f"MISSED: {summary['faults_missed']}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
