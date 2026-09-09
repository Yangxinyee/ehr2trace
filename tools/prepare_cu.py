"""Denormalize the CU CTPA export into the flat shape ehr2trace ingests.

Same contract as tools/prepare_mimiciv.py: projection and lookup joins only, no
imputation, sha256 manifest across the boundary. Three CU-specific gaps are closed
here because the dataset YAML has no expression language and must not grow one:

  * flowsheets carry `observation_date` and `observation_time` in two columns;
  * a blood pressure arrives as one cell, `135/76`, and OMOP records systolic and
    diastolic as two measurements. This is the one place the script emits more rows
    than it read, so it is stated rather than buried: the source's own `value_type`
    column labels these rows `Blood Pressure`, all 223,259 of them match `N/N` exactly
    and no other value_type uses that shape, so dividing them is reading a documented
    format rather than inventing a fact. A `Blood Pressure` row that does not match is
    carried through untouched instead of dropped, so a future export in a new shape
    surfaces as an unmapped value rather than as silence. The nine rows whose systolic
    is not above their diastolic are kept exactly as recorded;
  * T5 arrives as an un-deduplicated cross join of ICU stays x readmissions
    (463,618 rows for 62,068 patients, up to 3,818 rows for one patient), so the
    two facts are separated and each is taken DISTINCT. The ICU half also gains a
    constant `visit_type`, which is the table's own identity projected into a column
    rather than a fact invented for it: a row exists there because `icu_start_date`
    is set, and that column name is the source saying the patient was in an ICU. The
    `icu_los` distribution agrees -- mode one day, median two to three, long tail --
    which is an ICU length of stay and not a hospital one. The readmission half gains
    nothing: `readmission_date` says a readmission happened, never that it was an
    inpatient one, so its visit concept stays unmapped;
  * everything is one flat directory, and partitions want a directory each.

--sample N takes a deterministic every-Nth slice of the patient list so the whole
pipeline can be exercised in minutes rather than hours.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
import duckdb

CSV = (
    "header=true, all_varchar=true, delim=',', quote='\"', escape='\"', "
    "strict_mode=false, max_line_size=64000000"
)

def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cu-root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sample", type=int, default=0, help="keep 1 patient in N (0 = all)")
    ap.add_argument("--skip-notes", action="store_true")
    a = ap.parse_args()

    src = {
        "T1":  a.cu_root / "C4225_T1_PatientDemographics_20260318.csv",
        "T1a": a.cu_root / "C4225_T1a_CTScans_20260310.csv",
        "T2":  a.cu_root / "C4225_T2_Flowsheets_20260318.csv",
        "T3":  a.cu_root / "C4225_T3_Procedures_20260318.csv",
        "T4":  a.cu_root / "C4225_T4_Diagnoses_20260318.csv",
        "T5":  a.cu_root / "C4225_T5_ClinicalOutcomes_20260310.csv",
        "T7":  a.cu_root / "C4225_T7_Medications_20260310.csv",
    }
    notes = [a.cu_root / n for n in (
        "C4225_T6_ClinicalNotes_20260310.csv",
        "C4225_T6_ClinicalNotes_20260323.csv",
        "C4225_T6_ClinicalNotes_additional_20260323.csv",
    )]
    for p in src.values():
        if not p.exists():
            sys.exit(f"missing input: {p}")

    out = a.out / "cu_ctpa"
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("PRAGMA threads=16")

    for k, p in src.items():
        con.execute(f"CREATE VIEW {k} AS SELECT * FROM read_csv('{p}', {CSV})")

    # The cohort. A deterministic every-Nth slice, ordered by the export's own id, so
    # the same --sample always yields the same patients.
    if a.sample:
        con.execute(f"""
            CREATE TABLE cohort AS
            SELECT arb_person_id FROM (
              SELECT arb_person_id, row_number() OVER (ORDER BY arb_person_id) AS rn FROM T1
            ) WHERE rn % {a.sample} = 0
        """)
    else:
        con.execute("CREATE TABLE cohort AS SELECT arb_person_id FROM T1")
    n_cohort = con.execute("SELECT count(*) FROM cohort").fetchone()[0]
    print(f"cohort: {n_cohort:,} patients")

    J = "SEMI JOIN cohort USING (arb_person_id)"
    steps: list[tuple[str, str]] = [
        ("demographics", f"""
            SELECT t.arb_person_id, t.mrn,
                   t.age_at_first_inclusion_procedure AS age,
                   -- ' UTC' is stripped, not converted: whether the other tables are
                   -- also UTC is an open question for the data owner, and the YAML
                   -- declares one zone for the whole dataset.
                   replace(t.CTA_timestamp, ' UTC', '') AS cta_time,
                   -- CU gives an age together with the procedure it was current at, so
                   -- a birth year is derived exactly rather than approximated -- the
                   -- same move prepare_mimiciv.py makes with anchor_age/anchor_year.
                   -- The day is not known and is not invented: it stays 01-01 and the
                   -- converter flags every year derived this way.
                   CASE WHEN t.CTA_timestamp IS NULL
                             OR t.age_at_first_inclusion_procedure IS NULL THEN NULL
                        ELSE CAST(CAST(substr(t.CTA_timestamp, 1, 4) AS INTEGER)
                                  - CAST(t.age_at_first_inclusion_procedure AS INTEGER)
                                  AS VARCHAR) || '-01-01'
                   END AS birth_date,
                   t.death_date,
                   CASE WHEN t.death_date IS NULL OR t.death_date = ''
                        THEN 'Alive' ELSE 'Deceased' END AS vital_status,
                   t.sex, t.race, t.ethnicity,
                   t.BMI_closet_to_first_inclusion_procedure AS bmi
            FROM T1 t SEMI JOIN cohort USING (arb_person_id)"""),
        ("ct_studies", f"SELECT * FROM T1a {J}"),
        ("flowsheets", f"""
            WITH timed AS (
              SELECT arb_person_id, arb_encounter_id,
                     observation_date || ' ' || observation_time AS observed_at,
                     flowsheet_row_name, value_type, value, unit
              FROM T2 {J}
              WHERE observation_date IS NOT NULL AND observation_time IS NOT NULL
            ), bp AS (
              SELECT *, value_type = 'Blood Pressure'
                        AND regexp_matches(value, '^[0-9]+/[0-9]+$') AS splittable
              FROM timed
            )
            SELECT arb_person_id, arb_encounter_id, observed_at,
                   flowsheet_row_name, value_type, value, unit
              FROM bp WHERE NOT splittable
            UNION ALL
            SELECT arb_person_id, arb_encounter_id, observed_at,
                   flowsheet_row_name || ' systolic', value_type,
                   split_part(value, '/', 1), unit
              FROM bp WHERE splittable
            UNION ALL
            SELECT arb_person_id, arb_encounter_id, observed_at,
                   flowsheet_row_name || ' diastolic', value_type,
                   split_part(value, '/', 2), unit
              FROM bp WHERE splittable"""),
        ("flowsheets_undated", f"""
            SELECT * FROM T2 {J}
            WHERE observation_date IS NULL OR observation_time IS NULL"""),
        ("procedures", f"SELECT * FROM T3 {J}"),
        ("diagnoses", f"SELECT * FROM T4 {J}"),
        ("medications", f"SELECT * FROM T7 {J}"),
        ("icu_stays", f"""
            SELECT DISTINCT arb_person_id, icu_start_date, icu_los,
                   'ICU stay' AS visit_type
            FROM T5 {J} WHERE icu_start_date IS NOT NULL"""),
        ("readmissions", f"""
            SELECT DISTINCT arb_person_id, readmission_date,
                   readmission_flag_7day, readmission_flag_30day, readmission_flag_90day,
                   readmission_flag_6months, readmission_flag_1year
            FROM T5 {J} WHERE readmission_date IS NOT NULL"""),
    ]

    manifest = {"inputs": {}, "outputs": {}, "cohort_patients": n_cohort, "sample": a.sample}
    for name, sql in steps:
        dest = out / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT parquet)")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        manifest["outputs"][dest.name] = {"rows": rows, "sha256": sha256_file(dest)}
        print(f"  {name:22} {rows:>10,} rows")

    if not a.skip_notes:
        present = [p for p in notes if p.exists()]
        union = " UNION ALL ".join(
            f"SELECT *, '{p.name}' AS source_file FROM read_csv('{p}', {CSV})" for p in present
        )
        dest = out / "notes.parquet"
        con.execute(f"""
            COPY (SELECT * FROM ({union}) SEMI JOIN cohort USING (arb_person_id))
            TO '{dest}' (FORMAT parquet)""")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        manifest["outputs"][dest.name] = {"rows": rows, "sha256": sha256_file(dest)}
        print(f"  {'notes':22} {rows:>10,} rows")
        for p in present:
            manifest["inputs"][p.name] = None

    for p in src.values():
        manifest["inputs"].setdefault(p.name, None)
    (a.out / "prepare_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {a.out / 'prepare_manifest.json'}")

if __name__ == "__main__":
    main()
