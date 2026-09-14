"""Denormalize the CU CTPA export into the flat shape ehr2trace ingests.

Same contract as tools/prepare_mimiciv.py: projection and lookup joins only, no
imputation, sha256 manifest across the boundary. The CU-specific gaps closed here exist
because the dataset YAML has no expression language and must not grow one:

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
  * T1 dates each age by the first inclusion procedure, and three rows carry the age
    but not the procedure's timestamp. For those the earliest flowsheet day stands in
    as the day the age was current at: the vitals are a one-day snapshot taken at the
    scan, and that day is the CTA day for 92.0% of the 127,746 patients who have both
    (within one day of it for 95.2%). This is a decision of the study team, not an
    answer from the data owner, and the YAML's open_questions record it as such (D12,
    2026-09-11). The CTA timestamp itself is not invented: `cta_time` stays empty for
    the three, so they anchor nothing. `age_reference_day` and `age_reference_source`
    say which rule each row took, and the manifest counts the rows under each;
  * a note's `arb_encounter_id` is mostly an id no other table knows (2.6% of the
    notes' patient-encounter pairs occur in diagnoses, medications, flowsheets or
    procedures; 65% of the medications' do), and the same note text is filed under
    several of them. Each note row gets `encounter_linked` = '1' when its
    (patient, encounter) pair occurs in one of those four tables, else '0'. A lookup,
    not a judgement: the converter's `prefer_linked` merge rule reads the column and
    decides which encounter id, if any, a merged note keeps (D-R2, 2026-09-13);
  * T1a lists 168,046 accession numbers with no date. They do not enter the event
    stream (D-R16): `imaging_studies.parquet` is a study manifest, one row per
    accession, that says whether image metadata exists for it and, from the imaging
    directory's `links.csv`, the original study date and the series count;
    `imaging_series.parquet` is one row per accession x series with the series
    description, number and de-identified DICOM path. Only those columns are read from
    the imaging metadata: the patient name, birth date and original patient id
    columns those files carry are never projected (IDENTIFYING_IMAGING_COLUMNS);
  * everything is one flat directory, and partitions want a directory each.

The manifest records the sha256 of every raw file read, and per step the rows read,
the rows written, and every row dropped with its reason. The rows this export loses
are the ones whose `arb_person_id` is absent from T1 (T1a 3, T5 6, T6 70, T7 50 on the
full extract): a patient the demographics table does not know cannot get a PERSON
row, so their rows are dropped here, counted, and named. Every file under the
delivery the script does not read is listed under `unread_inputs` with the reason,
so RAW_COVERAGE_DECLARED can tell "unread on purpose" from "forgotten".

--sample N takes a deterministic every-Nth slice of the patient list so the whole
pipeline can be exercised in minutes rather than hours; rows outside the slice are
counted as `outside_sample`, which is not a loss.
"""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from pathlib import Path
import duckdb

CSV = (
    "header=true, all_varchar=true, delim=',', quote='\"', escape='\"', "
    "strict_mode=false, max_line_size=64000000"
)

#: Columns of the imaging metadata files that identify a person, or carry a path that
#: was written before de-identification. Never projected; the imaging step asserts it.
IDENTIFYING_IMAGING_COLUMNS = (
    "Patient ID", "Patient Name", "Patient Birth Date",
    "New Patient ID", "New Patient Name", "New Patient Birth Date",
    "Downloaded Original Series Path",
)

RAW_FILES = {
    "T1":  "C4225_T1_PatientDemographics_20260318.csv",
    "T1a": "C4225_T1a_CTScans_20260310.csv",
    "T2":  "C4225_T2_Flowsheets_20260318.csv",
    "T3":  "C4225_T3_Procedures_20260318.csv",
    "T4":  "C4225_T4_Diagnoses_20260318.csv",
    "T5":  "C4225_T5_ClinicalOutcomes_20260310.csv",
    "T7":  "C4225_T7_Medications_20260310.csv",
}
NOTE_FILES = (
    "C4225_T6_ClinicalNotes_20260310.csv",
    "C4225_T6_ClinicalNotes_20260323.csv",
    "C4225_T6_ClinicalNotes_additional_20260323.csv",
)


def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while b := fh.read(chunk):
            h.update(b)
    return h.hexdigest()


def input_record(p: Path, rows: int | None) -> dict:
    return {"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p), "rows": rows}


def unread_reason(rel: Path) -> str:
    """Why a delivered file is not read; ``rel`` is its path under the delivery."""
    name = rel.name
    parts = set(rel.parts)
    if "__MACOSX" in parts or name.startswith("._") or name == ".DS_Store":
        return "macOS archive metadata (AppleDouble resource forks, Finder state); not data"
    if ".idea" in parts:
        return "IDE project settings left by whoever unpacked the delivery; not data"
    if ".ipynb_checkpoints" in parts or rel.suffix == ".ipynb":
        return "the data owner's processing notebook: code that produced the summary CSVs, not records"
    if name.startswith(".~lock"):
        return "an office-suite lock file; not data"
    if rel.suffix == ".zip":
        return "the archive the delivery was unpacked from; its members are listed individually"
    if rel.suffix in {".pptx", ".docx", ".pdf", ".xlsx"}:
        return "project documentation delivered with the data (summary deck, ICD/CPT list, requirements, assignment letter); no records"
    if name.startswith("patient_level_") and name.endswith("_binary_flags.csv"):
        return "per-patient 0/1 flags the owner's notebook derived from the T4 diagnosis codes; a derivation of T4, which is read in full"
    if name.endswith("_missingness_relative_to_table1.csv") or name == "Table7_medication_name_missing_rate_vs_T1.csv":
        return "coverage statistics the owner's notebook computed from T1, T4 and T7; summaries of tables that are read in full, not records"
    if name == "not_found_series.csv":
        return "series the copy step did not find on disk; the same fact is copy_status = 'uid_not_found_on_disk' in copy_summary.csv"
    if name == "patient_copy_status.csv":
        return "per-accession copy status of the image copy step; superseded by the per-series copy_status in copy_summary.csv"
    return "not a table this conversion reads"


def list_unread(root: Path, read_paths: set[Path]) -> list[dict]:
    """Every file under ``root`` the script did not read, each with its reason."""
    out: list[dict] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.resolve() not in read_paths:
                out.append({"path": str(p), "reason": unread_reason(p.relative_to(root))})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cu-root", required=True, type=Path, help="directory holding the T1..T7 CSVs")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sample", type=int, default=0, help="keep 1 patient in N (0 = all)")
    ap.add_argument("--skip-notes", action="store_true")
    ap.add_argument("--images-root", type=Path, default=None,
                    help="CU_Images directory (links_files/ and Images/); omitted = no imaging manifest")
    ap.add_argument("--delivery-root", type=Path, default=None,
                    help="directory to scan for unread files; default is --cu-root")
    a = ap.parse_args()

    src = {k: a.cu_root / v for k, v in RAW_FILES.items()}
    notes = [a.cu_root / n for n in NOTE_FILES]
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

    manifest: dict = {
        "inputs": {}, "outputs": {}, "steps": {},
        "cohort_patients": n_cohort, "sample": a.sample,
    }

    def count_rows(relation: str) -> dict:
        """Rows of a raw relation, and how many of them the cohort filter keeps."""
        r = con.execute(f"""
            SELECT count(*),
                   count(*) FILTER (WHERE arb_person_id IS NULL),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM T1)),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM cohort))
            FROM {relation}""").fetchone()
        total, null_pid, in_t1, in_cohort = r
        dropped = {}
        if null_pid:
            dropped["person_id_missing"] = null_pid
        if total - null_pid - in_t1:
            dropped["person_not_in_T1"] = total - null_pid - in_t1
        if in_t1 - in_cohort:
            dropped["outside_sample"] = in_t1 - in_cohort
        return {"rows_read": total, "rows_kept": in_cohort, "dropped": dropped}

    raw_counts = {k: count_rows(k) for k in src}
    for k, p in src.items():
        manifest["inputs"][p.name] = input_record(p, raw_counts[k]["rows_read"])

    # (patient, encounter) pairs any of the four encounter-bearing clinical tables
    # know. The notes step looks its own pairs up here; nothing else reads this.
    con.execute("""
        CREATE TABLE linked_encounters AS
        SELECT DISTINCT arb_person_id, arb_encounter_id FROM (
          SELECT arb_person_id, arb_encounter_id FROM T4
          UNION ALL SELECT arb_person_id, arb_encounter_id FROM T7
          UNION ALL SELECT arb_person_id, arb_encounter_id FROM T2
          UNION ALL SELECT arb_person_id, arb_encounter_id FROM T3
        ) WHERE arb_person_id IS NOT NULL AND arb_encounter_id IS NOT NULL""")

    J = "SEMI JOIN cohort USING (arb_person_id)"
    steps: list[tuple[str, str, str]] = [
        ("demographics", "T1", f"""
            WITH first_vitals AS (
              -- The extract's own proxy for the day of the inclusion procedure, used
              -- only where T1 lacks the procedure's timestamp (see the module docstring).
              SELECT arb_person_id, min(observation_date) AS first_flowsheet_day
              FROM T2 WHERE observation_date IS NOT NULL GROUP BY arb_person_id
            )
            SELECT t.arb_person_id, t.mrn,
                   t.age_at_first_inclusion_procedure AS age,
                   -- ' UTC' is stripped, not converted: the suffix labels a local
                   -- clock (D15, 2026-09-12), and the YAML declares one zone for the
                   -- whole dataset.
                   replace(t.CTA_timestamp, ' UTC', '') AS cta_time,
                   -- The day the age was current at: the procedure's own timestamp, or
                   -- the earliest flowsheet day where the timestamp is missing.
                   CASE WHEN t.CTA_timestamp IS NOT NULL THEN substr(t.CTA_timestamp, 1, 10)
                        ELSE v.first_flowsheet_day END AS age_reference_day,
                   CASE WHEN t.CTA_timestamp IS NOT NULL THEN 'cta_timestamp'
                        WHEN v.first_flowsheet_day IS NOT NULL THEN 'earliest_flowsheet_day'
                   END AS age_reference_source,
                   -- CU gives an age together with the day it was current at, so a
                   -- birth year is derived exactly rather than approximated -- the same
                   -- move prepare_mimiciv.py makes with anchor_age/anchor_year. The day
                   -- is not known and is not invented: it stays 01-01 and the converter
                   -- flags every year derived this way.
                   CASE WHEN t.age_at_first_inclusion_procedure IS NULL
                             OR coalesce(t.CTA_timestamp, v.first_flowsheet_day) IS NULL THEN NULL
                        ELSE CAST(CAST(substr(coalesce(t.CTA_timestamp, v.first_flowsheet_day), 1, 4) AS INTEGER)
                                  - CAST(t.age_at_first_inclusion_procedure AS INTEGER)
                                  AS VARCHAR) || '-01-01'
                   END AS birth_date,
                   t.death_date,
                   CASE WHEN t.death_date IS NULL OR t.death_date = ''
                        THEN 'Alive' ELSE 'Deceased' END AS vital_status,
                   t.sex, t.race, t.ethnicity,
                   t.BMI_closet_to_first_inclusion_procedure AS bmi
            FROM T1 t SEMI JOIN cohort USING (arb_person_id)
            LEFT JOIN first_vitals v ON v.arb_person_id = t.arb_person_id"""),
        ("ct_studies", "T1a", f"SELECT * FROM T1a {J}"),
        ("flowsheets", "T2", f"""
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
        ("flowsheets_undated", "T2", f"""
            SELECT * FROM T2 {J}
            WHERE observation_date IS NULL OR observation_time IS NULL"""),
        ("procedures", "T3", f"SELECT * FROM T3 {J}"),
        ("diagnoses", "T4", f"SELECT * FROM T4 {J}"),
        # T7 carries no order id. `order_record_key` is a digest of the row's own
        # columns, declared in the YAML as the record's sequence number: two rows that
        # are identical in every delivered column are one order the export repeated,
        # and two that differ anywhere -- frequency, dispensed quantity -- are two
        # orders. Nothing is judged; the manifest counts the repeats.
        ("medications", "T7", f"""
            SELECT *, md5(concat_ws(chr(31),
                       coalesce(arb_person_id, chr(0)), coalesce(arb_encounter_id, chr(0)),
                       coalesce(medication_name, chr(0)), coalesce(therapeutic_class, chr(0)),
                       coalesce(pharmaceutical_class, chr(0)), coalesce(pharmaceutical_subclass, chr(0)),
                       coalesce(form, chr(0)), coalesce(route, chr(0)), coalesce(ordered_dose, chr(0)),
                       coalesce(dose_unit, chr(0)), coalesce(number_of_doses, chr(0)),
                       coalesce(dispensed_quantity, chr(0)), coalesce(quantity_unit, chr(0)),
                       coalesce(ordered_frequency, chr(0)), coalesce(order_status, chr(0)),
                       coalesce(start_date, chr(0)), coalesce(end_date, chr(0)))) AS order_record_key
            FROM T7 {J}"""),
        ("icu_stays", "T5", f"""
            SELECT DISTINCT arb_person_id, icu_start_date, icu_los,
                   'ICU stay' AS visit_type
            FROM T5 {J} WHERE icu_start_date IS NOT NULL"""),
        ("readmissions", "T5", f"""
            SELECT DISTINCT arb_person_id, readmission_date,
                   readmission_flag_7day, readmission_flag_30day, readmission_flag_90day,
                   readmission_flag_6months, readmission_flag_1year
            FROM T5 {J} WHERE readmission_date IS NOT NULL"""),
    ]

    def write(name: str, sql: str) -> int:
        dest = out / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT parquet)")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        manifest["outputs"][dest.name] = {"rows": rows, "sha256": sha256_file(dest)}
        print(f"  {name:22} {rows:>10,} rows")
        return rows

    for name, raw, sql in steps:
        rows = write(name, sql)
        c = raw_counts[raw]
        manifest["steps"][name] = {
            "from": src[raw].name, "rows_read": c["rows_read"], "rows_written": rows,
            "dropped": dict(c["dropped"]),
        }

    # -- the steps whose written count is not read minus dropped, explained ------------
    fs = manifest["steps"]["flowsheets"]
    undated, split = con.execute(f"""
        SELECT count(*) FILTER (WHERE observation_date IS NULL OR observation_time IS NULL),
               count(*) FILTER (WHERE observation_date IS NOT NULL AND observation_time IS NOT NULL
                                  AND value_type = 'Blood Pressure'
                                  AND regexp_matches(value, '^[0-9]+/[0-9]+$'))
        FROM T2 {J}""").fetchone()
    fs["rerouted"] = {"undated_to_flowsheets_undated": undated}
    fs["added"] = {"blood_pressure_split_into_systolic_and_diastolic": split}
    manifest["steps"]["flowsheets_undated"]["rerouted"] = {"from_flowsheets": undated}
    # T5 is one cross join feeding two outputs, so each half accounts for its own rows:
    # the rows of the other half are the other output's, not losses.
    halves = {
        "icu_stays": ("icu_start_date", "(arb_person_id, icu_start_date, icu_los)"),
        "readmissions": ("readmission_date", "(arb_person_id, readmission_date, readmission_flag_7day, "
                         "readmission_flag_30day, readmission_flag_90day, readmission_flag_6months, "
                         "readmission_flag_1year)"),
    }
    for name, (col, key) in halves.items():
        half, not_in_t1, outside, kept, distinct = con.execute(f"""
            SELECT count(*),
                   count(*) FILTER (WHERE arb_person_id NOT IN (SELECT arb_person_id FROM T1)),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM T1)
                                      AND arb_person_id NOT IN (SELECT arb_person_id FROM cohort)),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM cohort)),
                   count(DISTINCT {key}) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM cohort))
            FROM T5 WHERE {col} IS NOT NULL""").fetchone()
        st = manifest["steps"][name]
        st["rows_of_this_half"] = half
        st["dropped"] = {k: v for k, v in {
            "other_half_of_the_cross_join": st["rows_read"] - half,
            "person_not_in_T1": not_in_t1, "outside_sample": outside}.items() if v}
        st["collapsed"] = {"cross_join_repeats_taken_distinct": kept - distinct}

    rows, keys = con.execute(f"""
        SELECT count(*), count(DISTINCT order_record_key)
        FROM read_parquet('{out / 'medications.parquet'}')""").fetchone()
    manifest["steps"]["medications"]["repeated"] = {
        "rows_identical_in_every_column_beyond_the_first": rows - keys,
        "note": "kept here; the converter merges rows that share order_record_key",
    }

    # Which rule dated each age, so the three that took the stand-in are counted and
    # a future export that drops more timestamps shows up as a larger number here.
    manifest["age_reference_source"] = dict(con.execute(f"""
        SELECT coalesce(age_reference_source, 'none'), count(*)
        FROM read_parquet('{out / 'demographics.parquet'}') GROUP BY 1 ORDER BY 1""").fetchall())
    print(f"  age reference: {manifest['age_reference_source']}")

    # -- notes -------------------------------------------------------------------------
    if a.skip_notes:
        manifest["steps"]["notes"] = {"skipped": "--skip-notes"}
        for p in notes:
            manifest["inputs"][p.name] = {"path": str(p), "read": False, "reason": "--skip-notes"}
    else:
        present = [p for p in notes if p.exists()]
        union = " UNION ALL ".join(
            f"SELECT *, '{p.name}' AS source_file FROM read_csv('{p}', {CSV})" for p in present
        )
        con.execute(f"CREATE VIEW T6 AS {union}")
        dest = out / "notes.parquet"
        con.execute(f"""
            COPY (
              SELECT n.*,
                     CASE WHEN le.arb_encounter_id IS NOT NULL THEN '1' ELSE '0' END AS encounter_linked
              FROM (SELECT * FROM T6 SEMI JOIN cohort USING (arb_person_id)) n
              LEFT JOIN linked_encounters le
                ON le.arb_person_id = n.arb_person_id AND le.arb_encounter_id = n.arb_encounter_id
            ) TO '{dest}' (FORMAT parquet)""")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{dest}')").fetchone()[0]
        manifest["outputs"][dest.name] = {"rows": rows, "sha256": sha256_file(dest)}
        print(f"  {'notes':22} {rows:>10,} rows")
        per_file = con.execute("""
            SELECT source_file, count(*),
                   count(*) FILTER (WHERE arb_person_id IS NULL),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM T1)),
                   count(*) FILTER (WHERE arb_person_id IN (SELECT arb_person_id FROM cohort))
            FROM T6 GROUP BY 1 ORDER BY 1""").fetchall()
        total = sum(r[1] for r in per_file)
        dropped = {
            "person_id_missing": sum(r[2] for r in per_file),
            "person_not_in_T1": sum(r[1] - r[2] - r[3] for r in per_file),
            "outside_sample": sum(r[3] - r[4] for r in per_file),
        }
        linked, unlinked = con.execute(f"""
            SELECT count(*) FILTER (WHERE encounter_linked = '1'),
                   count(*) FILTER (WHERE encounter_linked = '0')
            FROM read_parquet('{dest}')""").fetchone()
        manifest["steps"]["notes"] = {
            "from": [p.name for p in present], "rows_read": total, "rows_written": rows,
            "dropped": {k: v for k, v in dropped.items() if v},
            "per_file": {r[0]: {"rows_read": r[1], "rows_written": r[4]} for r in per_file},
            # A lookup against diagnoses, medications, flowsheets and procedures: how
            # many notes carry an encounter id one of those tables also carries.
            "encounter_linked": linked, "encounter_unlinked": unlinked,
        }
        print(f"  notes encounter_linked: {linked:,} linked, {unlinked:,} unlinked")
        for p in present:
            n = manifest["steps"]["notes"]["per_file"][p.name]["rows_read"]
            manifest["inputs"][p.name] = input_record(p, n)
        for p in notes:
            if p not in present:
                manifest["inputs"][p.name] = {"path": str(p), "read": False, "reason": "absent"}

    # -- imaging study manifest (D-R16): not a source, not events ----------------------
    # The note files are inputs whether or not this run read them: the inputs section
    # already says which, so they are not "unread" in the sense of "forgotten".
    read_paths = {p.resolve() for p in src.values()} | {p.resolve() for p in notes}
    unread: list[dict] = []
    if a.images_root is not None:
        manifest["imaging"] = imaging_manifest(con, a.images_root, out, manifest, write)
        read_paths |= {Path(p).resolve() for p in manifest["imaging"]["files_read"]}
        unread += list_unread(a.images_root / "links_files", read_paths)
        # The image payload (DICOM series and NIfTI volumes, 553 GB) is not walked:
        # only the metadata CSVs next to it are candidates for reading.
        for csv in sorted((a.images_root / "Images").glob("*/*/*/*.csv")):
            if csv.resolve() not in read_paths:
                unread.append({"path": str(csv), "reason": unread_reason(csv.relative_to(a.images_root))})
    else:
        manifest["imaging"] = {"skipped": "no --images-root"}

    scan_root = a.delivery_root or a.cu_root
    unread = list_unread(scan_root, read_paths) + unread
    manifest["unread_inputs"] = unread
    print(f"  unread inputs listed: {len(unread)}")

    (a.out / "prepare_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {a.out / 'prepare_manifest.json'}")


def imaging_manifest(con, images_root: Path, out: Path, manifest: dict, write) -> dict:
    """T1a's accessions joined to the imaging directory's metadata, minus every identifier.

    ``links.csv`` is read per batch and only five of its columns are taken: the
    accession number, the original study date, the series description and number, and
    the de-identified series path (empty throughout this delivery, and kept so that the
    emptiness is recorded rather than papered over). ``copy_summary.csv`` supplies the
    DICOM destination path and copy status per series, ``conversion_summary.csv`` the
    NIfTI path per accession. The join key to ``copy_summary`` is the series UID, used
    and not written.
    """
    icsv = CSV + ", union_by_name=true"
    links = sorted(images_root.glob("links_files/*/*/links.csv"))
    copies = sorted(images_root.glob("Images/CTPA_Dicom/*/*/copy_summary.csv"))
    convs = sorted(images_root.glob("Images/CTPA_nii/*/*/conversion_summary.csv"))
    if not links:
        sys.exit(f"no links.csv under {images_root}/links_files")

    def rel(p: Path) -> str:
        return str(p.relative_to(images_root))

    con.execute("CREATE TABLE links AS " + " UNION ALL ".join(
        f"""SELECT "Accession Number" AS accession_number, "Study Date" AS study_date_source,
                   "Series Description" AS series_description, "Series Number" AS series_number,
                   "Downloaded DeIdentified Series Path" AS deidentified_series_path,
                   "Series Instance UID" AS _series_uid, '{rel(p)}' AS links_file
            FROM read_csv('{p}', {icsv})""" for p in links))
    # Nineteen (accession, series) pairs are listed by two batches; the copy rows are
    # folded to one per pair first, so the join below cannot multiply a series.
    con.execute("CREATE TABLE copy_rows AS " + (" UNION ALL ".join(
        f"""SELECT "Accession Number" AS accession_number, "Series Instance UID" AS _series_uid,
                   copy_status, dicom_count, dst_path AS dicom_path
            FROM read_csv('{p}', {icsv})""" for p in copies) if copies else
        "SELECT NULL AS accession_number, NULL AS _series_uid, NULL AS copy_status, NULL AS dicom_count, NULL AS dicom_path WHERE false"))
    con.execute("""
        CREATE TABLE copies AS
        SELECT accession_number, _series_uid,
               string_agg(DISTINCT copy_status, '|' ORDER BY copy_status) AS copy_status,
               max(dicom_count) AS dicom_count, min(dicom_path) AS dicom_path
        FROM copy_rows GROUP BY 1, 2""")
    con.execute("CREATE TABLE convs AS " + " UNION ALL ".join(
        f"""SELECT accession_number, series_description_folder AS nii_series_description,
                   nii_path, status AS nii_status
            FROM read_csv('{p}', {icsv})""" for p in convs) if convs else
        "SELECT NULL AS accession_number, NULL AS nii_series_description, NULL AS nii_path, NULL AS nii_status WHERE false")
    for table in ("links", "copies", "convs"):
        cols = {c[0] for c in con.execute(f"DESCRIBE {table}").fetchall()}
        leaked = cols & set(IDENTIFYING_IMAGING_COLUMNS)
        assert not leaked, f"identifying column projected into {table}: {sorted(leaked)}"

    # One row per accession x series: the series rows of every accession T1a lists.
    con.execute(f"""
        CREATE TABLE series AS
        SELECT DISTINCT t.arb_person_id, l.accession_number, l.links_file, l.study_date_source,
               CASE WHEN regexp_matches(l.study_date_source, '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}$')
                    THEN strftime(strptime(l.study_date_source, '%m/%d/%Y'), '%Y-%m-%d') END AS study_date,
               l.series_number, l.series_description, l.deidentified_series_path,
               c.dicom_path, c.copy_status, c.dicom_count
        FROM links l
        JOIN read_parquet('{out / 'ct_studies.parquet'}') t ON t.ACCESSIONNUMBER = l.accession_number
        LEFT JOIN copies c ON c.accession_number = l.accession_number AND c._series_uid = l._series_uid""")
    con.execute(f"""
        CREATE TABLE studies AS
        WITH per_acc AS (
          SELECT accession_number,
                 arg_min(study_date_source, study_date) AS study_date_source,
                 min(study_date) AS study_date,
                 count(DISTINCT study_date_source) AS study_date_count,
                 count(*) AS series_count,
                 count(dicom_path) AS series_with_dicom_path,
                 string_agg(DISTINCT links_file, '|' ORDER BY links_file) AS links_files
          FROM series GROUP BY 1
        ), nii AS (
          SELECT accession_number, arg_min(nii_series_description, nii_path) AS nii_series_description,
                 min(nii_path) AS nii_path, arg_min(nii_status, nii_path) AS nii_status,
                 count(*) AS nii_rows
          FROM convs GROUP BY 1
        )
        SELECT t.arb_person_id, t.ACCESSIONNUMBER AS accession_number,
               CASE WHEN p.accession_number IS NOT NULL THEN '1' ELSE '0' END AS has_image_metadata,
               p.study_date, p.study_date_source, p.study_date_count, p.series_count,
               p.series_with_dicom_path, p.links_files,
               n.nii_path, n.nii_series_description, n.nii_status, n.nii_rows
        FROM read_parquet('{out / 'ct_studies.parquet'}') t
        LEFT JOIN per_acc p ON p.accession_number = t.ACCESSIONNUMBER
        LEFT JOIN nii n ON n.accession_number = t.ACCESSIONNUMBER""")
    n_studies = write("imaging_studies", "SELECT * FROM studies ORDER BY arb_person_id, accession_number")
    n_series = write("imaging_series", "SELECT * FROM series ORDER BY arb_person_id, accession_number, series_number, series_description")

    counts = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE has_image_metadata = '1'),
               count(study_date), count(nii_path),
               count(*) FILTER (WHERE study_date_count > 1)
        FROM studies""").fetchone()
    series_counts = con.execute("""
        SELECT count(*), count(dicom_path), count(deidentified_series_path),
               count(*) FILTER (WHERE study_date IS NULL)
        FROM series""").fetchone()
    links_rows, links_acc, not_in_t1a = con.execute(f"""
        SELECT count(*), count(DISTINCT accession_number),
               count(DISTINCT accession_number) FILTER (WHERE accession_number NOT IN
                    (SELECT ACCESSIONNUMBER FROM read_parquet('{out / 'ct_studies.parquet'}')))
        FROM links""").fetchone()
    files_read = [str(p) for p in links + copies + convs]
    for p in links + copies + convs:
        manifest["inputs"][rel(p)] = input_record(
            p, con.execute(f"SELECT count(*) FROM read_csv('{p}', {icsv})").fetchone()[0])
    manifest["steps"]["imaging_studies"] = {
        "from": ["ct_studies.parquet (T1a)", "links.csv", "conversion_summary.csv"],
        "rows_read": manifest["outputs"]["ct_studies.parquet"]["rows"], "rows_written": n_studies,
        "dropped": {},
    }
    manifest["steps"]["imaging_series"] = {
        "from": ["links.csv", "copy_summary.csv"], "rows_read": links_rows, "rows_written": n_series,
        "dropped": {"accession_not_in_T1a_or_outside_cohort": links_rows - con.execute(
            "SELECT count(*) FROM links WHERE accession_number IN (SELECT accession_number FROM series)").fetchone()[0]},
        "collapsed": {"identical_rows_repeated_across_batches": con.execute(
            "SELECT count(*) FROM links WHERE accession_number IN (SELECT accession_number FROM series)").fetchone()[0] - n_series},
    }
    return {
        "images_root": str(images_root),
        "files_read": files_read,
        "links_files": len(links), "copy_summary_files": len(copies), "conversion_summary_files": len(convs),
        "links_rows": links_rows, "links_accessions": links_acc,
        "links_accessions_not_in_T1a_cohort": not_in_t1a,
        "accessions_total": counts[0],
        "accessions_with_image_metadata": counts[1],
        "accessions_with_study_date": counts[2],
        "accessions_with_nifti_path": counts[3],
        "accessions_with_several_study_dates": counts[4],
        "series_rows": series_counts[0],
        "series_with_dicom_path": series_counts[1],
        "series_with_deidentified_series_path_in_links": series_counts[2],
        "series_with_unparsed_study_date": series_counts[3],
        "study_date_source_format": "MM/DD/YYYY as delivered; study_date is its ISO form",
        "identifying_columns_never_projected": list(IDENTIFYING_IMAGING_COLUMNS),
    }


if __name__ == "__main__":
    main()
