# -*- coding: utf-8 -*-
"""Generate tests/fixtures/ctpe_shape — a synthetic export with the *shape* of the
reference data and none of its content.

Why this exists: the design asks for fixtures covering the export's structural traps,
and those traps are genuinely subtle — a byte-order mark present in one file of a
logical source and absent in another, a whole table repeated once per extraction
anchor, one sheet named differently in one partition, a workbook that types a date as
text where its sibling types it as a date. Every one of them fails silently.

What it does not do is copy patient data. Every value here is fabricated: four made-up
patients, invented codes, invented narrative text. The fixture is safe to commit, which
is exactly what makes it usable as a regression test.

Regenerate with:
    python3 tools/make_shape_fixture.py
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ctpe_shape"

# Four partitions across two batches, with the directory-naming inconsistency and the
# has/no cohort split of the real export.
PARTITIONS = {
    "b1_has": ("batch1/has_finding", "1", "has"),
    "b1_no": ("batch1/no_finding", "1", "no"),
    "b2_has": ("batch2/has_the_finding", "2", "has"),
    "b2_no": ("batch2/no_the_finding", "2", "no"),
}

# Which patients appear where. SUBJ-2 is in both a "has" and a "no" partition of the
# same batch: the case that proves a cohort label is episode-level, not patient-level.
# Age as each batch recorded it. SUBJ-1's two extracts disagree, which is routine when
# an export is taken twice -- and, with no reference date for either age, makes a year
# of birth genuinely underivable. That patient must be withheld from OMOP, not guessed.
AGES = {
    ("1", "SUBJ-1"): 60,
    ("2", "SUBJ-1"): 62,
    ("1", "SUBJ-2"): 71,
    ("2", "SUBJ-2"): 71,
    ("1", "SUBJ-3"): 38,
    ("2", "SUBJ-3"): 38,
    ("1", "SUBJ-4"): 55,
    ("2", "SUBJ-4"): 55,
}

MEMBERSHIP = {
    "b1_has": ["SUBJ-1", "SUBJ-2"],
    "b1_no": ["SUBJ-3"],
    "b2_has": ["SUBJ-1", "SUBJ-2", "SUBJ-4"],
    "b2_no": ["SUBJ-2"],
}

# Anchors per (partition, patient). Batch 1 carries full timestamps, batch 2 dates only.
ANCHORS = {
    ("b1_has", "SUBJ-1"): ["2019-03-04 11:20:00.0000000"],
    ("b1_has", "SUBJ-2"): ["2019-03-04 11:20:00.0000000", "2019-07-19 08:05:00.0000000"],
    ("b1_no", "SUBJ-3"): ["2019-05-02 16:40:00.0000000"],
    ("b2_has", "SUBJ-1"): ["2019-03-04"],
    ("b2_has", "SUBJ-2"): ["2019-03-04", "2019-07-19", "2020-01-08"],
    ("b2_has", "SUBJ-4"): ["2020-06-11"],
    ("b2_no", "SUBJ-2"): ["2020-11-30"],
}

# The two files that carry no byte-order mark, mirroring the real inconsistency.
NO_BOM = {("b1_has", "problem_list"), ("b1_no", "problem_list")}

ECHO_LINES = [
    "Transthoracic Echo Report",
    "Indications: dyspnea",
    "",
    "Left ventricle: normal size, ejection fraction 55 to 60 percent.",
    "Right ventricle: mildly dilated with reduced systolic function.",
    "Estimated RVSP:  35-40 mmHg",
    "Confirmed by TEST, READER on 2019-03-05",
]

EKG_COMPONENTS = [
    ("VENTRICULAR RATE", "1", "96"),
    ("QRS DURATION", "1", "88"),
    ("Q-T  INTERVAL", "1", "392"),
    ("DIAGNOSIS", "1", "SINUS TACHYCARDIA"),
    ("DIAGNOSIS", "2", "RIGHT AXIS DEVIATION"),
]

LAB_RESULTS = [
    ("PHART", "PH, ARTERIAL", "7.31", "NULL"),
    ("NA", "SODIUM", "138", "mmol/L"),
    ("WBC", "WBC COUNT", "38.62", "K/cu mm"),
    ("K", "POTASSIUM", "see below", "mmol/L"),
    ("CREAT", "CREATININE", "<0.50", "mg/dL"),
]


def write_text(path: Path, header: list[str], rows: list[list[str]], bom: bool) -> None:
    """Tab-delimited, CRLF, optional byte-order mark — exactly as the real files are."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\r\n".join(["\t".join(header)] + ["\t".join(r) for r in rows]) + "\r\n"
    path.write_bytes((("﻿" if bom else "") + body).encode("utf-8"))


def event_times(seed: str, n: int) -> list[str]:
    rng = random.Random(seed)
    start = datetime(2019, 1, 1, 8, 0)
    out = []
    for i in range(n):
        moment = start + timedelta(days=rng.randrange(0, 700), minutes=rng.randrange(0, 600))
        out.append(moment.strftime("%Y-%m-%d %H:%M:%S.000"))
    return sorted(out)


def build_partition(partition_id: str) -> None:
    directory, batch, label = PARTITIONS[partition_id]
    root = OUT / directory
    patients = MEMBERSHIP[partition_id]
    prefix = f"{partition_id}"

    # -- all_rx: ordering intent, including rows with no ordering date at all --------
    rx_rows = []
    for patient in patients:
        for i, moment in enumerate(event_times(f"rx{patient}", 4)):
            # every fourth order has no date and must be quarantined, never dated
            ordering = "NULL" if i == 3 else moment
            rx_rows.append([patient, f"ENC-{patient}-1", "TESTDRUG 10 mg tablet", "10 mg", ordering, "Dispensed"])
    write_text(
        root / f"{prefix}_all_rx.txt",
        ["MRN", "Encounter_CSN", "Medication_Name", "HV_Discrete_Dose", "Ordering_Date", "Order_Status"],
        rx_rows,
        bom=True,
    )

    # -- problem_list: byte-order mark deliberately inconsistent ---------------------
    problem_rows = []
    for patient in patients:
        problem_rows.append([patient, "J45.909", "Unspecified asthma uncomplicated", "2017-04-02 00:00:00.000", "Active"])
        # dated after the death date below: must be flagged, never deleted or re-dated
        problem_rows.append([patient, "E11.9", "Type 2 diabetes mellitus", "2022-09-14 00:00:00.000", "Active"])
    write_text(
        root / f"{prefix}_problem_list.txt",
        ["MRN", "Diagnosis_Code", "Diagnosis_Name", "First_Noted_Date", "Status"],
        problem_rows,
        bom=(partition_id, "problem_list") not in NO_BOM,
    )

    # -- echo and ekg: the whole table repeated once per anchor ----------------------
    echo_rows, ekg_rows, lab_rows = [], [], []
    for patient in patients:
        anchors = ANCHORS.get((partition_id, patient), ["NULL"])
        result_time = event_times(f"echo{patient}", 1)[0]
        ekg_time = event_times(f"ekg{patient}", 1)[0]
        lab_times = event_times(f"lab{patient}", len(LAB_RESULTS))
        for rank, anchor in enumerate(anchors, start=1):
            # One report, one line per row, repeated in full under every anchor.
            # Closest_to_CT is a rank against that anchor, never a day difference.
            for line, text in enumerate(ECHO_LINES, start=1):
                echo_rows.append(
                    [patient, f"ENC-{patient}-1", "ECHO TRANSTHORACIC COMPLETE (TTE)", anchor,
                     result_time, "narrative", str(line), text or "NULL", str(rank)]
                )
            for component, line, value in EKG_COMPONENTS:
                ekg_rows.append(
                    [patient, f"ENC-{patient}-1", "ECG 12-LEAD", anchor, ekg_time,
                     component, line, value, str(rank)]
                )
            for i, (code, name, value, unit) in enumerate(LAB_RESULTS):
                collection = lab_times[i]
                result = (datetime.strptime(collection, "%Y-%m-%d %H:%M:%S.%f") + timedelta(minutes=22)).strftime(
                    "%Y-%m-%d %H:%M:%S.000"
                )
                # one lab per patient has no collection time: the flagged fallback path
                lab_rows.append(
                    [patient, anchor, f"ENC-{patient}-1", result,
                     "NULL" if i == 1 else collection, name, code, value, unit, "NULL", "NULL", str(i + 1)]
                )
                _ = rank

    write_text(
        root / f"{prefix}_echo.txt",
        ["mrn", "CSN", "DESCRIPTION", "dos", "result_time", "Rad_Result_type", "LINE", "NARRATIVE", "Closest_to_CT"],
        echo_rows,
        bom=True,
    )
    write_text(
        root / f"{prefix}_ekg.txt",
        ["MRN", "CSN", "Procedure_Name", "dos", "Result_Time", "Component_Name", "Line", "Result_Value", "Closest_to_CT"],
        ekg_rows,
        bom=True,
    )
    write_text(
        root / f"{prefix}_labs.txt",
        ["MRN", "dos", "CSN", "Result_Time", "Collection_time", "Lab_Name", "Base_Name", "Value",
         "Units", "Reference_Range_Low", "Reference_Range_High", "rn"],
        lab_rows,
        bom=True,
    )

    # -- medication admin: a standalone file in one partition, a sheet in the rest ---
    if partition_id == "b2_has":
        admin_rows = [
            [patient, f"ENC-{patient}-1", event_times(f"adm{patient}", 1)[0],
             "TESTDRUG 10 MG TABLET", "10 mg", "Oral"]
            for patient in patients
        ]
        write_text(
            root / f"{prefix}_medication_admin.txt",
            ["MRN", "CSN", "Date_Administered", "Medication_Name", "Dose", "Route"],
            admin_rows,
            bom=True,
        )

    build_workbook(root / f"{prefix}.xlsx", partition_id, patients)


def build_workbook(path: Path, partition_id: str, patients: list[str]) -> None:
    """The workbook, including the sheet-alias variation and the cell-type drift."""
    wb = Workbook()
    wb.remove(wb.active)

    batch = PARTITIONS[partition_id][1]
    demographics = wb.create_sheet("Demographics")
    demographics.append(["MRN", "Age", "Gender", "Race", "Ethnicity", "Status", "Death_Date", "BMI", "Pulse", "BP_Systolic"])
    for i, patient in enumerate(patients):
        # The type drift: one partition writes the death date as text, the others as a
        # date cell, and both must hash identically.
        died = datetime(2031, 2, 3, 4, 5)
        death_cell = "2031-02-03 04:05:00" if partition_id == "b2_has" else died
        demographics.append(
            [patient, AGES[(batch, patient)], "Female" if i % 2 else "Male", "White", "Not Hispanic or Latino",
             "Deceased" if patient == "SUBJ-2" else "Alive",
             death_cell if patient == "SUBJ-2" else "NULL",
             27.4 + i, 72 + i, 118 + i]
        )

    if partition_id != "b2_has":
        admin = wb.create_sheet("Medication Administration")
        admin.append(["MRN", "CSN", "Date_Administered", "Medication_Name", "Dose", "Route"])
        for patient in patients:
            admin.append([patient, f"ENC-{patient}-1", datetime(2019, 4, 9, 11, 47),
                          "TESTDRUG 10 MG TABLET", "10 mg", "Oral"])

    # One partition names both PFT sheets differently from the others.
    narrative_name = "PFT" if partition_id == "b1_no" else "PFT Narrative"
    values_name = "PFT Value" if partition_id == "b1_no" else "PFT Values"

    narrative = wb.create_sheet(narrative_name)
    narrative_header = ["mrn", "CSN", "DESCRIPTION", "dos", "result_time", "Rad_Result_type", "LINE", "NARRATIVE", "Closest_to_CT"]
    # One partition ships an extra leading column with an empty header and no values.
    lead = partition_id == "b2_no"
    narrative.append(([None] if lead else []) + narrative_header)
    for patient in patients:
        for line, text in enumerate(["Spirometry performed pre and post bronchodilator.", "FEV1 within normal limits."], start=1):
            narrative.append(
                ([None] if lead else [])
                + [patient, f"ENC-{patient}-1", "PULMONARY FUNCTION TEST", "2019-03-04",
                   datetime(2019, 3, 6, 9, 15), "narrative", line, text, 1]
            )

    values = wb.create_sheet(values_name)
    values.append(["MRN", "CSN", "Procedure_Name", "dos", "Result_Time", "Component_Name", "Line", "Result_Value", "Closest_to_CT"])
    for patient in patients:
        values.append([patient, f"ENC-{patient}-1", "PULMONARY FUNCTION TEST", "2019-03-04",
                       datetime(2019, 3, 6, 9, 15), "FEV1 PCT PRED", 1, 78, 1])

    outcome = wb.create_sheet("Outcome")
    outcome.append(["MRN", "CSN", "Date_of_Service", "Hosp_Admsn_Time", "Length_of_stay_days",
                    "Time_Difference", "Death_Date", "Hospital_Visit_Type"])
    for i, patient in enumerate(patients):
        # The other type drift: length of stay as text in one partition, integer in the
        # rest. And a masked duration that must never be parsed as a duration.
        los = str(3 + i) if partition_id == "b1_no" else 3 + i
        outcome.append([patient, f"ENC-{patient}-1", datetime(2019, 3, 1), datetime(2019, 3, 1, 9, 30),
                        los, "-11:0*:0*:00",
                        (datetime(2031, 2, 3, 4, 5) if patient == "SUBJ-2" else "NULL"),
                        "Inpatient Hospital Stay"])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main() -> int:
    import shutil

    if OUT.exists():
        shutil.rmtree(OUT)
    for partition_id in PARTITIONS:
        build_partition(partition_id)
    files = sorted(p for p in OUT.rglob("*") if p.is_file())
    print(f"wrote {len(files)} files under {OUT}")
    for path in files:
        print(f"  {path.relative_to(OUT)}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
