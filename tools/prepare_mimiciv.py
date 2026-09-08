"""Denormalize MIMIC-IV into the shape a hospital export actually arrives in.

MIMIC-IV is a normalized relational database: ``labevents`` carries an ``itemid`` and
the human-readable label lives in ``d_labitems``. A hospital extract does not look like
that -- it is a pile of flat, already-joined files, which is what ``ehr2trace`` targets.
This script closes that gap, and it does so *outside* the trusted boundary on purpose:
everything the converter validates happens after this point, so this step must be small,
deterministic and auditable.

Three rules it holds itself to:

* **projection and lookup joins only.** No aggregation, no filtering, no row invention.
  Every output is asserted to have exactly as many rows as its driving input, so a join
  that fans out is a crash rather than a silent duplication.
* **no imputation.** Where MIMIC has no timestamp, none is invented here. Discharge
  diagnoses genuinely carry no time of their own; the admission's ``dischtime`` is
  carried alongside as a separate column so that *using* it is a decision the dataset
  config has to state out loud, not something this script does quietly.
* **lineage across the boundary.** A manifest records the sha256 of every input and
  every output, so the chain original CSV -> prepared parquet -> canonical -> published
  is unbroken even though the first hop happens here.

Usage::

    python tools/prepare_mimiciv.py --mimic-root /path/to/mimiciv/3.1 \\
        --ed-root /path/to/mimic-iv-ed/2.2/ed \\
        --note-root /path/to/mimic-iv-note/2.2/note \\
        --out /path/to/work/mimiciv_source
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import duckdb

CSV_OPTS = "header=true, all_varchar=true, compression='gzip'"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


class Prep:
    """Runs one denormalization step and records what it touched."""

    def __init__(self, con: duckdb.DuckDBPyConnection, out_dir: Path):
        self.con = con
        self.out_dir = out_dir
        self.records: list[dict] = []
        self.inputs: dict[str, str] = {}

    def view(self, name: str, path: Path) -> bool:
        """Register a gzipped CSV as a view. Missing inputs are reported, not fatal."""
        if not path.exists():
            print(f"  [skip] {name}: {path} not found", file=sys.stderr)
            return False
        self.con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_csv('{path}', {CSV_OPTS})")
        if str(path) not in self.inputs:
            self.inputs[str(path)] = sha256_file(path)
        return True

    def emit(self, source_id: str, sql: str, driving: str) -> None:
        """Materialize one prepared source, asserting the join did not change cardinality."""
        target = self.out_dir / f"{source_id}.parquet"
        expected = self.con.execute(f"SELECT count(*) FROM {driving}").fetchone()[0]
        self.con.execute(f"COPY ({sql}) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        actual = self.con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
        if actual != expected:
            raise SystemExit(
                f"{source_id}: {actual:,} rows written from {expected:,} in {driving}. "
                "A lookup join changed cardinality -- that is a duplicated or dropped fact, "
                "not a formatting difference."
            )
        print(f"  {source_id:24} {actual:>12,} rows")
        self.records.append(
            {
                "source_id": source_id,
                "rows": actual,
                "driving_table": driving,
                "output_path": str(target),
                "output_sha256": sha256_file(target),
            }
        )


def build(mimic: Path, ed: Path | None, note: Path | None, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("PRAGMA threads=16")
    p = Prep(con, out)

    hosp, icu = mimic / "hosp", mimic / "icu"

    # ---- dimensions (lookup only; never emitted on their own) --------------------
    for name, path in (
        ("d_labitems", hosp / "d_labitems.csv.gz"),
        ("d_icd_diagnoses", hosp / "d_icd_diagnoses.csv.gz"),
        ("d_icd_procedures", hosp / "d_icd_procedures.csv.gz"),
    ):
        p.view(name, path)

    # ---- person ------------------------------------------------------------------
    # MIMIC gives an age together with the year it was current in, per patient. That is
    # strictly more than the hospital export had, so year_of_birth is derived exactly
    # rather than approximated -- the same code path, with the ambiguity removed.
    if p.view("patients", hosp / "patients.csv.gz"):
        p.view("admissions_for_person", hosp / "admissions.csv.gz")
        p.emit(
            "patients",
            """
            SELECT pt.subject_id,
                   pt.gender,
                   pt.anchor_age,
                   pt.anchor_year,
                   pt.anchor_year_group,
                   CAST(CAST(pt.anchor_year AS INTEGER) - CAST(pt.anchor_age AS INTEGER) AS VARCHAR)
                     || '-01-01' AS birth_date,
                   pt.dod,
                   CASE WHEN pt.dod IS NULL OR pt.dod = '' THEN 'Alive' ELSE 'Deceased' END AS vital_status
            FROM patients pt
            """,
            "patients",
        )

    # ---- visits ------------------------------------------------------------------
    if p.view("admissions", hosp / "admissions.csv.gz"):
        p.emit(
            "admissions",
            """
            SELECT subject_id, hadm_id, admittime, dischtime, deathtime,
                   admission_type, admission_location, discharge_location,
                   insurance, language, marital_status, race,
                   edregtime, edouttime, hospital_expire_flag,
                   date_diff('day', CAST(admittime AS TIMESTAMP), CAST(dischtime AS TIMESTAMP))
                     AS los_days
            FROM admissions
            """,
            "admissions",
        )

    if p.view("transfers", hosp / "transfers.csv.gz"):
        p.emit(
            "transfers",
            "SELECT subject_id, hadm_id, transfer_id, eventtype, careunit, intime, outtime FROM transfers",
            "transfers",
        )

    if p.view("services", hosp / "services.csv.gz"):
        p.emit(
            "services",
            "SELECT subject_id, hadm_id, transfertime, prev_service, curr_service FROM services",
            "services",
        )

    # ---- conditions --------------------------------------------------------------
    # Split by ICD version: code_system is a property of a source, and an ICD-9 code and
    # an ICD-10 code that happen to spell the same are not the same code.
    #
    # These rows carry no time of their own. `hadm_dischtime` is carried alongside so the
    # dataset config can decide, in the open, whether to use it. Nothing is imputed here.
    if p.view("diagnoses_icd", hosp / "diagnoses_icd.csv.gz"):
        for version, sid in (("9", "diagnoses_icd9"), ("10", "diagnoses_icd10")):
            con.execute(
                f"CREATE OR REPLACE VIEW _dx AS SELECT * FROM diagnoses_icd WHERE icd_version = '{version}'"
            )
            p.emit(
                sid,
                """
                SELECT d.subject_id, d.hadm_id, d.seq_num, d.icd_code, d.icd_version,
                       dd.long_title, a.dischtime AS hadm_dischtime, a.admittime AS hadm_admittime
                FROM _dx d
                LEFT JOIN d_icd_diagnoses dd
                       ON dd.icd_code = d.icd_code AND dd.icd_version = d.icd_version
                LEFT JOIN admissions a ON a.hadm_id = d.hadm_id
                """,
                "_dx",
            )

    if p.view("procedures_icd", hosp / "procedures_icd.csv.gz"):
        for version, sid in (("9", "procedures_icd9"), ("10", "procedures_icd10")):
            con.execute(
                f"CREATE OR REPLACE VIEW _px AS SELECT * FROM procedures_icd WHERE icd_version = '{version}'"
            )
            p.emit(
                sid,
                """
                SELECT p.subject_id, p.hadm_id, p.seq_num, p.chartdate, p.icd_code, p.icd_version,
                       dp.long_title
                FROM _px p
                LEFT JOIN d_icd_procedures dp
                       ON dp.icd_code = p.icd_code AND dp.icd_version = p.icd_version
                """,
                "_px",
            )

    # ---- measurements ------------------------------------------------------------
    # charttime is when the specimen was taken; storetime is when anyone could see the
    # result. Keeping both is what lets MEDS carry a real available_time instead of
    # assuming the result was knowable the moment it existed.
    if p.view("labevents", hosp / "labevents.csv.gz"):
        p.emit(
            "labevents",
            """
            SELECT l.labevent_id, l.subject_id, l.hadm_id, l.itemid,
                   dl.label AS lab_name, dl.fluid, dl.category,
                   l.charttime, l.storetime, l.value, l.valuenum, l.valueuom,
                   l.ref_range_lower, l.ref_range_upper, l.flag, l.priority
            FROM labevents l
            LEFT JOIN d_labitems dl ON dl.itemid = l.itemid
            """,
            "labevents",
        )

    if p.view("microbiologyevents", hosp / "microbiologyevents.csv.gz"):
        p.emit(
            "microbiologyevents",
            """
            SELECT microevent_id, subject_id, hadm_id, chartdate, charttime, storedate, storetime,
                   spec_type_desc, test_name, org_name, ab_name, dilution_text, interpretation
            FROM microbiologyevents
            """,
            "microbiologyevents",
        )

    # omr carries a date but no time -- the untimed-measurement path, on public data.
    if p.view("omr", hosp / "omr.csv.gz"):
        p.emit(
            "omr",
            "SELECT subject_id, chartdate, seq_num, result_name, result_value FROM omr",
            "omr",
        )

    # ---- medications -------------------------------------------------------------
    if p.view("prescriptions", hosp / "prescriptions.csv.gz"):
        p.emit(
            "prescriptions",
            """
            SELECT subject_id, hadm_id, pharmacy_id, poe_id, starttime, stoptime,
                   drug_type, drug, formulary_drug_cd, gsn, ndc, prod_strength,
                   dose_val_rx, dose_unit_rx, route, doses_per_24_hrs
            FROM prescriptions
            """,
            "prescriptions",
        )

    if p.view("emar", hosp / "emar.csv.gz"):
        # `poe_id` and `pharmacy_id` were not projected before, so nothing downstream
        # could tell which order an administration carried out. They are the whole
        # reason an action stream can answer "was this ordered thing actually given".
        base = """SELECT e.subject_id, e.hadm_id, e.emar_id, e.emar_seq, e.poe_id,
                         e.pharmacy_id, e.charttime, e.medication, e.event_txt,
                         e.scheduletime, e.storetime"""
        if p.view("emar_detail", hosp / "emar_detail.csv.gz"):
            # emar_detail holds one row per component of an administration, so a plain
            # join multiplies the dose. It is collapsed to one row per emar_id first,
            # taking each field from the earliest component that states it; `emit`'s
            # cardinality assertion is what proves the collapse actually happened.
            sql = f"""
            WITH detail AS (
              SELECT emar_id,
                     arg_min(dose_given, ord)               AS dose_given,
                     arg_min(dose_given_unit, ord)          AS dose_given_unit,
                     arg_min(route, ord)                    AS route,
                     arg_min(product_amount_given, ord)     AS product_amount_given,
                     arg_min(infusion_rate, ord)            AS infusion_rate,
                     arg_min(infusion_rate_unit, ord)       AS infusion_rate_unit,
                     arg_min(complete_dose_not_given, ord)  AS complete_dose_not_given,
                     count(*)                               AS detail_rows
              FROM (SELECT *, coalesce(try_cast(parent_field_ordinal AS INTEGER), 0) AS ord
                    FROM emar_detail)
              GROUP BY emar_id
            )
            {base}, d.dose_given, d.dose_given_unit, d.route, d.product_amount_given,
                    d.infusion_rate, d.infusion_rate_unit, d.complete_dose_not_given,
                    d.detail_rows,
                    -- The canonical layer carries a dose as one text field, the way the
                    -- other export's administrations already arrive, so the amount and
                    -- its unit are joined here rather than losing the unit.
                    nullif(trim(coalesce(d.dose_given, '') || ' ' ||
                                coalesce(d.dose_given_unit, '')), '') AS dose_given_text
            FROM emar e LEFT JOIN detail d USING (emar_id)
            """
        else:
            print("  [skip] emar_detail: dose and route will be empty", file=sys.stderr)
            sql = f"{base} FROM emar e"
        p.emit("emar", sql, "emar")

    # The pharmacy's record of a medication order, which is where a dispensation is
    # documented. MIMIC-IV on FHIR maps this table to MedicationDispense, and that is
    # the reading taken here. It is worth stating what it is not: `status` describes the
    # order's state rather than a confirmed hand-over, and `starttime` opens the
    # dispensing schedule rather than timing a single act. Calling it a dispense is
    # already far closer than calling it an administration, which is what the ED
    # cabinet records were called before.
    if p.view("pharmacy", hosp / "pharmacy.csv.gz"):
        p.emit(
            "pharmacy",
            """
            SELECT subject_id, hadm_id, pharmacy_id, poe_id, starttime, stoptime,
                   medication, proc_type, status, entertime, verifiedtime, route,
                   frequency, dispensation, fill_quantity
            FROM pharmacy
            """,
            "pharmacy",
        )

    if p.view("poe", hosp / "poe.csv.gz"):
        p.emit(
            "poe",
            """
            SELECT poe_id, poe_seq, subject_id, hadm_id, ordertime, order_type,
                   order_subtype, transaction_type, order_status
            FROM poe
            """,
            "poe",
        )

    # ---- emergency department ----------------------------------------------------
    if ed is not None:
        if p.view("edstays", ed / "edstays.csv.gz"):
            p.emit(
                "edstays",
                """
                SELECT subject_id, hadm_id, stay_id, intime, outtime, gender, race,
                       arrival_transport, disposition
                FROM edstays
                """,
                "edstays",
            )
        if p.view("ed_vitalsign", ed / "vitalsign.csv.gz"):
            p.emit(
                "ed_vitalsign",
                """
                SELECT subject_id, stay_id, charttime, temperature, heartrate, resprate,
                       o2sat, sbp, dbp, rhythm, pain
                FROM ed_vitalsign
                """,
                "ed_vitalsign",
            )
        # triage has no charttime at all: a wide row of values with no time of its own.
        if p.view("ed_triage", ed / "triage.csv.gz"):
            p.view("edstays_for_triage", ed / "edstays.csv.gz")
            p.emit(
                "ed_triage",
                """
                SELECT t.subject_id, t.stay_id, t.temperature, t.heartrate, t.resprate,
                       t.o2sat, t.sbp, t.dbp, t.pain, t.acuity, t.chiefcomplaint,
                       e.intime AS stay_intime
                FROM ed_triage t
                LEFT JOIN edstays_for_triage e ON e.stay_id = t.stay_id
                """,
                "ed_triage",
            )
        if p.view("ed_pyxis", ed / "pyxis.csv.gz"):
            p.emit(
                "ed_pyxis",
                "SELECT subject_id, stay_id, charttime, med_rn, name, gsn FROM ed_pyxis",
                "ed_pyxis",
            )
        if p.view("ed_diagnosis", ed / "diagnosis.csv.gz"):
            p.emit(
                "ed_diagnosis",
                """
                SELECT d.subject_id, d.stay_id, d.seq_num, d.icd_code, d.icd_version,
                       d.icd_title, e.outtime AS stay_outtime
                FROM ed_diagnosis d
                LEFT JOIN edstays_for_triage e ON e.stay_id = d.stay_id
                """,
                "ed_diagnosis",
            )

    # ---- notes -------------------------------------------------------------------
    if note is not None:
        for sid, fname in (("note_radiology", "radiology.csv.gz"), ("note_discharge", "discharge.csv.gz")):
            if p.view(sid, note / fname):
                p.emit(
                    sid,
                    f"SELECT note_id, subject_id, hadm_id, note_type, note_seq, charttime, storetime, text FROM {sid}",
                    sid,
                )

    manifest = {
        "tool": "prepare_mimiciv.py",
        "tool_version": "1",
        "inputs": [{"path": k, "sha256": v} for k, v in sorted(p.inputs.items())],
        "outputs": p.records,
        "total_rows": sum(r["rows"] for r in p.records),
    }
    (out / "prepare_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mimic-root", type=Path, required=True, help="mimiciv/3.1")
    ap.add_argument("--ed-root", type=Path, default=None, help="mimic-iv-ed/2.2/ed")
    ap.add_argument("--note-root", type=Path, default=None, help="mimic-iv-note/2.2/note")
    ap.add_argument("--out", type=Path, required=True, help="partition directory to write")
    args = ap.parse_args()

    m = build(args.mimic_root, args.ed_root, args.note_root, args.out)
    print(f"\n{len(m['outputs'])} sources, {m['total_rows']:,} rows -> {args.out}")
    print(f"manifest: {args.out / 'prepare_manifest.json'}")


if __name__ == "__main__":
    main()
