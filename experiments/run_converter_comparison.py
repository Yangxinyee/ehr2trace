"""What a converted record still carries, measured on one public subset.

Three converters, one input: the MIMIC-IV demonstration subset of 100 patients, which
is open access and therefore the only part of this paper a reader can re-run in full.
Two of the three outputs are the conversions their own authors published for that
subset, so nothing here depends on us operating somebody else's tool well:

  ehr2trace        this system, OMOP and MEDS from one conversion
  mimic-iv-demo-omop   OMOP CDM 5.4, published by MIT-LCP/OHDSI from their ETL
  mimic-iv-demo-meds   MEDS 0.3.3, published by the MEDS project from MEDS-Transforms

The comparison is deliberately not about which tool is *better* at the parts the CDMs
already agree on. It asks what survives conversion that a downstream model needs and a
schema does not require: whether an exported record still names the source row it came
from, whether the moment a fact became knowable is separable from the moment it
happened, and whether an order, a dispensation and an administration remain three
different facts. Each is answered by reading the published files, not the documentation.

Usage::

    python tools/run_converter_comparison.py --work <demo work root> \\
        --baselines <download dir> --out results/converter_comparison.json
"""
from __future__ import annotations

import argparse
import glob
import json
import time
from pathlib import Path

import duckdb

#: OMOP clinical tables compared across the two CDM outputs.
CLINICAL = {
    "condition_occurrence": "condition_concept_id",
    "drug_exposure": "drug_concept_id",
    "measurement": "measurement_concept_id",
    "procedure_occurrence": "procedure_concept_id",
    "observation": "observation_concept_id",
}


def omop_from_csv(con, directory: Path) -> dict:
    """Measure a published OMOP CDM directory of CSV files."""
    out = {"tables": {}, "rows": 0, "mapped_rows": 0}
    for table, concept in CLINICAL.items():
        path = directory / f"{table}.csv"
        if not path.exists():
            continue
        n, mapped = con.execute(
            f"""SELECT count(*), sum(CASE WHEN try_cast({concept} AS BIGINT) > 0 THEN 1 ELSE 0 END)
                FROM read_csv('{path}', header=true, all_varchar=true)"""
        ).fetchone()
        out["tables"][table] = {"rows": int(n), "mapped_rows": int(mapped or 0)}
        out["rows"] += int(n)
        out["mapped_rows"] += int(mapped or 0)
    drug = directory / "drug_exposure.csv"
    if drug.exists():
        rows = con.execute(
            f"""SELECT drug_type_concept_id, count(*) FROM read_csv('{drug}', header=true, all_varchar=true)
                GROUP BY 1 ORDER BY 2 DESC"""
        ).fetchall()
        out["drug_type_concepts"] = {str(k): int(v) for k, v in rows}
    return out


def omop_from_duckdb(con, database: Path) -> dict:
    """Measure this system's OMOP export, including the lineage the CDM has no column for."""
    db = duckdb.connect(str(database), read_only=True)
    try:
        out = {"tables": {}, "rows": 0, "mapped_rows": 0}
        for table, concept in CLINICAL.items():
            n, mapped = db.execute(
                f"SELECT count(*), sum(CASE WHEN {concept} > 0 THEN 1 ELSE 0 END) FROM {table}"
            ).fetchone()
            out["tables"][table] = {"rows": int(n), "mapped_rows": int(mapped or 0)}
            out["rows"] += int(n)
            out["mapped_rows"] += int(mapped or 0)
        rows = db.execute(
            "SELECT drug_type_concept_id, count(*) FROM drug_exposure GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        out["drug_type_concepts"] = {str(k): int(v) for k, v in rows}
        # Every published row reaches its source rows through the lineage table, which is
        # what `ehr2trace trace` reads. The CDM has no column for this, so it is stored
        # beside the tables rather than inside them.
        out["lineage_rows"] = int(db.execute("SELECT count(*) FROM lnk").fetchone()[0])
        out["source_linked_events"] = int(
            db.execute("SELECT count(DISTINCT event_id) FROM lnk").fetchone()[0])
        out["canonical_events"] = int(db.execute("SELECT count(*) FROM evt").fetchone()[0])
        return out
    finally:
        db.close()


def meds(con, root: Path, code_is_standard: str, source_row_columns: list[str]) -> dict:
    """Measure a MEDS root: events, how many carry a concept, a source row, an availability."""
    data = f"{root}/data/*/*.parquet"
    if not glob.glob(f"{root}/data/*/*.parquet"):
        data = f"{root}/data/**/*.parquet"
    columns = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{data}')").fetchall()]
    events, subjects = con.execute(
        f"SELECT count(*), count(DISTINCT subject_id) FROM read_parquet('{data}')").fetchone()
    out = {
        "events": int(events),
        "subjects": int(subjects),
        "columns": columns,
        "standard_concept_events": int(con.execute(
            f"SELECT count(*) FROM read_parquet('{data}') WHERE {code_is_standard}").fetchone()[0]),
    }
    present = [c for c in source_row_columns if c in columns]
    out["source_row_columns"] = present
    if present:
        clause = " OR ".join(f"{c} IS NOT NULL" for c in present)
        out["events_with_a_source_row"] = int(con.execute(
            f"SELECT count(*) FROM read_parquet('{data}') WHERE {clause}").fetchone()[0])
    else:
        out["events_with_a_source_row"] = 0
    # A converter that carries availability separately can be asked how often it differs;
    # one that does not carry it cannot be asked at all, and that is the measurement.
    if "available_time" in columns:
        out["availability_column"] = "available_time"
        out["events_with_later_availability"] = int(con.execute(
            f"SELECT count(*) FROM read_parquet('{data}') WHERE available_time > time").fetchone()[0])
        out["timed_events"] = int(con.execute(
            f"SELECT count(*) FROM read_parquet('{data}') WHERE time IS NOT NULL").fetchone()[0])
    else:
        out["availability_column"] = None
    if "event_kind" in columns:
        out["medication_action_kinds"] = {
            k: int(v) for k, v in con.execute(
                f"""SELECT event_kind, count(*) FROM read_parquet('{data}')
                    WHERE event_kind LIKE 'drug%' GROUP BY 1 ORDER BY 2 DESC""").fetchall()}
    else:
        out["medication_action_kinds"] = {
            k: int(v) for k, v in con.execute(
                f"""SELECT split_part(code, '//', 1) AS prefix, count(*) FROM read_parquet('{data}')
                    WHERE lower(code) LIKE '%medication%' OR lower(code) LIKE '%infusion%'
                    GROUP BY 1 ORDER BY 2 DESC""").fetchall()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work", type=Path, required=True, help="this system's demo work root")
    ap.add_argument("--baselines", type=Path, required=True, help="directory holding the two published conversions")
    ap.add_argument("--mimic-demo", type=Path, default=None,
                    help="the demo source tree, to say which modules each output covers")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute("SET threads=4")
    started = time.time()

    ours_omop = omop_from_duckdb(con, args.work / "mimiciv" / "omop" / "omop.duckdb")
    ours_meds = meds(con, args.work / "mimiciv" / "meds",
                     code_is_standard="code LIKE 'OMOP/%'",
                     source_row_columns=["source_row_ids"])
    ours_meds["source_table_named"] = "source_table" in ours_meds["columns"]
    # Which source modules this conversion actually read on the subset, taken from the
    # events rather than from the configuration: the configuration also reads emergency
    # and note modules, and the demonstration release has neither.
    tables = dict(con.execute(
        f"""SELECT source_table, count(*) FROM read_parquet('{args.work}/mimiciv/meds/data/*/*.parquet')
            WHERE source_table IS NOT NULL GROUP BY 1""").fetchall())
    ours_meds["events_by_source_table"] = {k: int(v) for k, v in tables.items()}

    def module(table: str) -> str:
        if table.startswith("ed"):
            return "emergency"
        if table.startswith("note"):
            return "notes"
        if table in ("chartevents", "inputevents", "outputevents", "procedureevents", "icustays",
                     "datetimeevents", "ingredientevents"):
            return "ICU"
        return "hospital"
    ours_meds["modules_read"] = sorted({module(t) for t in tables})

    base_omop_dir = args.baselines / "mimic-iv-demo-omop" / "0.9" / "1_omop_data_csv"
    base_omop = omop_from_csv(con, base_omop_dir)
    # The OHDSI conversion ships a Data Quality Dashboard run beside the tables. That is
    # a validation report on the saved output, and it counts as one.
    dqd = sorted((args.baselines / "mimic-iv-demo-omop" / "0.9" / "3_data_quality_dashboard_files").glob("*.json"))
    base_omop["validation_reports"] = [p.name for p in dqd]
    # The conversion's own record of the CDM and vocabulary it was built against.
    cdm = con.execute(
        f"SELECT cdm_version, vocabulary_version FROM read_csv('{base_omop_dir}/cdm_source.csv', header=true, all_varchar=true)"
    ).fetchone()
    base_omop["cdm_version"], base_omop["vocabulary_version"] = cdm[0], cdm[1]
    base_omop["visit_detail_rows"] = int(con.execute(
        f"SELECT count(*) FROM read_csv('{base_omop_dir}/visit_detail.csv', header=true, all_varchar=true)").fetchone()[0])

    base_meds_dir = args.baselines / "mimic-iv-demo-meds" / "0.0.1"
    base_meds = meds(con, base_meds_dir,
                     code_is_standard="false",
                     source_row_columns=["hadm_id", "emar_id", "poe_id", "order_id", "icustay_id"])
    base_meds["source_table_named"] = False
    # Its codes reach a vocabulary through metadata rather than through the event row.
    codes = f"{base_meds_dir}/metadata/codes.parquet"
    linked = con.execute(
        f"""SELECT count(*) FROM read_parquet('{base_meds_dir}/data/*/*.parquet') e
            WHERE e.code IN (SELECT code FROM read_parquet('{codes}')
                             WHERE parent_codes IS NOT NULL AND len(parent_codes) > 0)"""
    ).fetchone()[0]
    base_meds["events_whose_code_links_to_a_vocabulary"] = int(linked)
    # Its medication codes are one prefix, but the row keeps the order and
    # administration keys of the tables it came from, so the action is still
    # recoverable. Measuring that is fairer than counting code prefixes.
    total, emar, poe, neither = con.execute(
        f"""SELECT count(*), sum(CASE WHEN emar_id IS NOT NULL THEN 1 ELSE 0 END),
                   sum(CASE WHEN poe_id IS NOT NULL THEN 1 ELSE 0 END),
                   sum(CASE WHEN emar_id IS NULL AND poe_id IS NULL THEN 1 ELSE 0 END)
            FROM read_parquet('{base_meds_dir}/data/*/*.parquet') WHERE code LIKE 'MEDICATION%'"""
    ).fetchone()
    base_meds["medication_events"] = {
        "total": int(total), "with_an_administration_key": int(emar),
        "with_an_order_key": int(poe), "with_neither": int(neither)}
    base_meds["metadata_json"] = json.loads((Path(base_meds_dir) / "metadata" / "dataset.json").read_text())

    # Scale is not comparable unless the configurations cover the same source modules,
    # so the modules are measured rather than assumed: the demo's two item dictionaries
    # say whether a laboratory event came from the hospital module or the ICU one.
    if args.mimic_demo:
        icu = {r[0] for r in con.execute(
            f"SELECT itemid FROM read_csv('{args.mimic_demo}/icu/d_items.csv.gz', header=true, all_varchar=true)").fetchall()}
        lab = {r[0] for r in con.execute(
            f"SELECT itemid FROM read_csv('{args.mimic_demo}/hosp/d_labitems.csv.gz', header=true, all_varchar=true)").fetchall()}
        rows = con.execute(
            f"""SELECT split_part(code, '//', 2), count(*) FROM read_parquet('{base_meds_dir}/data/*/*.parquet')
                WHERE code LIKE 'LAB//%' GROUP BY 1""").fetchall()
        base_meds["lab_events_by_module"] = {
            "icu": sum(int(n) for i, n in rows if i in icu and i not in lab),
            "hosp": sum(int(n) for i, n in rows if i in lab),
        }
        base_meds["modules_read"] = sorted(
            {"hospital"} | ({"ICU"} if base_meds["lab_events_by_module"]["icu"] else set()))
        rows = con.execute(
            f"""SELECT measurement_source_value, count(*) FROM read_csv('{base_omop_dir}/measurement.csv', header=true, all_varchar=true)
                GROUP BY 1""").fetchall()
        base_omop["measurement_rows_by_module"] = {
            "icu": sum(int(n) for v, n in rows if v in icu and v not in lab),
            "hosp": sum(int(n) for v, n in rows if v in lab),
        }
        base_omop["modules_read"] = sorted(
            {"hospital"} | ({"ICU"} if base_omop["measurement_rows_by_module"]["icu"] else set()))
        demo_modules = sorted(d.name for d in args.mimic_demo.iterdir() if d.is_dir())
        base_meds["subset_modules"] = base_omop["subset_modules"] = demo_modules

    validation = json.loads((args.work / "mimiciv" / "runs" / "validation.json").read_text())
    con.close()

    record = {
        "subset": "MIMIC-IV demonstration subset, 100 patients",
        "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seconds": round(time.time() - started, 1),
        "ehr2trace": {
            "omop": ours_omop, "meds": ours_meds,
            "validation": {
                "checks": len(validation),
                "passed": sum(c["passed"] and not c.get("skipped") for c in validation),
                "skipped": sum(bool(c.get("skipped")) for c in validation),
                "failed": sum(not c["passed"] for c in validation),
            },
        },
        "mimic_iv_demo_omop": base_omop,
        "mimic_iv_demo_meds": base_meds,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k != "mimic_iv_demo_meds"}, indent=2)[:1200])
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
