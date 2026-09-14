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
  that fans out is a crash rather than a silent duplication. The two exceptions are
  declared per output rather than buried: a wide row of vital signs becomes one row per
  value (``rows_added_by_split``), and one physical table can drive two outputs when
  its rows are two kinds of fact (a culture and a susceptibility), each output naming
  the subset that drives it.
* **no imputation.** Where MIMIC has no timestamp, none is invented here. Discharge
  diagnoses genuinely carry no time of their own; the admission's ``dischtime`` is
  carried alongside as a separate column so that *using* it is a decision the dataset
  config has to state out loud, not something this script does quietly. The same goes
  for the ED triage row, whose only time is the stay's arrival: it is carried as
  ``stay_intime`` next to a constant ``time_is_fallback`` column the config turns into a
  flag on every event that uses it.
* **lineage across the boundary.** A manifest records the sha256 of every input and
  every output, every raw file under the delivery this script did not read and why,
  every raw column it did not carry and why, so the chain original CSV -> prepared
  parquet -> canonical -> published is unbroken even though the first hop happens here.

``--sample N`` keeps the subjects whose id is divisible by N, in every table alike, so a
small deterministic slice of the whole delivery can be prepared and built end to end
before the full run is paid for. The manifest records the sample.

Usage::

    python tools/prepare_mimiciv.py --mimic-root /path/to/mimiciv/3.1 \\
        --ed-root /path/to/mimic-iv-ed/2.2/ed \\
        --note-root /path/to/mimic-iv-note/2.2/note \\
        --out /path/to/work/mimiciv_source [--sample 100]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

import duckdb

CSV_OPTS = "header=true, all_varchar=true, compression='gzip'"
TOOL_VERSION = "3"

# The container names a compounded order's BASE row carries when it names no
# substance. mimiciv.yaml drops them from the named prescriptions; the name recovery
# below counts how often one is the only name an order has, so the config can say
# what happens to those rows rather than discover it.
CONTAINER_NAMES = ("Bag", "Vial", "Soln")

PROVIDER = "provider identifier: who ordered or entered the row, never a clinical fact"
CAREGIVER = "caregiver identifier: D-R13 keeps caregiver identity out of the conversion"

#: Raw columns the projections below deliberately do not carry, by output. Every
#: column of a driving table is either projected or listed here; the manifest reports
#: any that is neither, so a new column in a future MIMIC release surfaces as a number
#: rather than disappearing.
NOT_PROJECTED: dict[str, dict[str, str]] = {
    "admissions": {"admit_provider_id": PROVIDER},
    "labevents": {
        "specimen_id": "groups the results drawn in one specimen; each result is its own event",
        "order_provider_id": PROVIDER,
        "comments": "free-text lab comment; the result is in `value`, and free text is not read here",
    },
    "micro_organisms": {
        "order_provider_id": PROVIDER,
        "spec_itemid": "numeric id behind spec_type_desc, which is carried",
        "test_seq": "order of the test within the specimen; the test name and specimen id distinguish it",
        "ab_itemid": "susceptibility columns belong to micro_susceptibility",
        "ab_name": "susceptibility columns belong to micro_susceptibility",
        "dilution_text": "susceptibility columns belong to micro_susceptibility",
        "dilution_comparison": "susceptibility columns belong to micro_susceptibility",
        "dilution_value": "susceptibility columns belong to micro_susceptibility",
        "interpretation": "susceptibility columns belong to micro_susceptibility",
    },
    "micro_susceptibility": {
        "order_provider_id": PROVIDER,
        "spec_itemid": "numeric id behind spec_type_desc, which is carried",
        "test_seq": "order of the test within the specimen; microevent_id distinguishes the row",
        "quantity": "a property of the culture, carried by micro_organisms",
        "comments": "a property of the culture, carried by micro_organisms",
    },
    "prescriptions": {
        "poe_seq": "the order's sequence inside the provider order entry; poe_id links the order",
        "order_provider_id": PROVIDER,
        "form_rx": "dosage form of the product (TAB, VIAL); the strength is in prod_strength",
        "form_val_disp": "amount dispensed per dose in product units; dose_val_rx is the dose",
        "form_unit_disp": "unit of form_val_disp",
    },
    "pharmacy": {
        "disp_sched": "hours of the day the doses are scheduled; frequency is carried",
        "infusion_type": "infusion classification; route and the eMAR rate carry what was given",
        "sliding_scale": "whether the dose follows a sliding scale; the administered dose is in eMAR",
        "lockout_interval": "patient-controlled analgesia parameter, not a dispensation fact",
        "basal_rate": "patient-controlled analgesia parameter; the eMAR rate is the given rate",
        "one_hr_max": "patient-controlled analgesia parameter",
        "doses_per_24_hrs": "prescriptions carries the same field for the order",
        "duration": "intended duration of the order; start and stop times are carried",
        "duration_interval": "unit of duration",
        "expiration_value": "order expiry, not a dispensation fact",
        "expiration_unit": "unit of expiration_value",
        "expirationdate": "order expiry, not a dispensation fact",
    },
    "emar": {
        "enter_provider_id": PROVIDER,
    },
    "emar_detail": {
        "subject_id": "carried by emar",
        "emar_seq": "carried by emar",
        "parent_field_ordinal": "orders the components of one administration; used to pick the earliest, then dropped",
        "administration_type": "how the dose was administered (bolus, drip); event_txt on emar carries the action",
        "pharmacy_id": "carried by emar",
        "barcode_type": "scanning workflow detail",
        "reason_for_no_barcode": "scanning workflow detail",
        "dose_due": "the scheduled dose; dose_given is what reached the patient",
        "dose_due_unit": "unit of dose_due",
        "will_remainder_of_dose_be_given": "workflow answer, not a fact about exposure",
        "product_unit": "unit of product_amount_given, a product quantity rather than a dose",
        "product_code": "product identifier; the medication name is the code here",
        "product_description": "product description; the medication name is the code here",
        "product_description_other": "free text",
        "prior_infusion_rate": "the rate before this row; infusion_rate is the rate this row states",
        "infusion_rate_adjustment": "how the rate changed; the rate itself is carried",
        "infusion_rate_adjustment_amount": "size of the change; the rate itself is carried",
        "infusion_complete": "workflow flag",
        "completion_interval": "workflow detail",
        "new_iv_bag_hung": "workflow flag",
        "continued_infusion_in_other_location": "workflow flag",
        "restart_interval": "workflow detail",
        "side": "body side of a topical or injection site",
        "site": "injection or application site",
        "non_formulary_visual_verification": "workflow flag",
    },
    "poe": {
        "discontinue_of_poe_id": "the order this one discontinues; the action is in transaction_type",
        "discontinued_by_poe_id": "the order that discontinued this one",
        "order_provider_id": PROVIDER,
    },
    "ed_pyxis": {"gsn_rn": "ordinal of the GSN within one dispensation; med_rn distinguishes the dispensation"},
    # The wide-to-long split is the one place where a column legitimately vanishes from
    # the output: each of these becomes a *row* carrying its own code, value and unit
    # (T2.M2, T2.M3). Without saying so here the coverage report calls eight real
    # measurements unexplained, which is the noise that hides a column nobody meant to
    # drop.
    "ed_vitalsign": {
        c: "wide column carried as one row per value by the declared split (T2.M2)"
        for c in ("temperature", "heartrate", "resprate", "o2sat", "sbp", "dbp", "rhythm", "pain")
    },
    "ed_triage": {
        c: "wide column carried as one row per value by the declared split (T2.M3)"
        for c in ("temperature", "heartrate", "resprate", "o2sat", "sbp", "dbp", "pain",
                  "acuity", "chiefcomplaint")
    },
    "chartevents": {"caregiver_id": CAREGIVER},
    "datetimeevents": {"caregiver_id": CAREGIVER},
    "outputevents": {"caregiver_id": CAREGIVER},
    "procedureevents": {"caregiver_id": CAREGIVER},
    "inputevents": {"caregiver_id": CAREGIVER},
    "ingredientevents": {"caregiver_id": CAREGIVER},
}

#: Why a delivered file is not read. Keyed by "<module>/<file>", where module is the
#: last directory component (hosp, icu, ed, note). mimiciv.yaml declares the same
#: tables under `out_of_scope` with the same reasons; the manifest lists them so the
#: coverage check can tell "deliberately unread" from "forgotten".
UNREAD_REASONS: dict[str, str] = {
    "hosp/drgcodes.csv.gz": "diagnosis-related groups: a payment classification assigned after discharge, not a clinical fact (T2.M11)",
    "hosp/hcpcsevents.csv.gz": "HCPCS billing codes per admission; whether they are procedures or billing artefacts is a review question, so they stay out until it is answered (T2.M11)",
    "hosp/d_hcpcs.csv.gz": "dimension for hcpcsevents, which is not read (T2.M11)",
    "hosp/poe_detail.csv.gz": "free-form key/value fields per order (admit-to, code status, consult service); the order itself is published from poe, the fields have no event shape (T2.M11)",
    "hosp/provider.csv.gz": "provider identifiers only, no clinical content (T2.M11)",
    "ed/medrecon.csv.gz": "medication reconciliation: what the patient reported taking before arrival, neither an order nor an administration; it needs a reported-history event kind before it can be published honestly (T2.M11)",
    "note/discharge_detail.csv.gz": "metadata fields of the discharge notes (author, service); the note text is published, the fields are not events (T2.M11)",
    "note/radiology_detail.csv.gz": "metadata fields of the radiology notes (exam name, CPT codes, addenda); the note text is published, the fields are not events (T2.M11)",
    "icu/caregiver.csv.gz": "caregiver identifiers, not clinical facts (D-R13, T4.4)",
}

LOOKUP_REASON = "lookup only: joined for its labels, never emitted as a source"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def sample_predicate(sample: int, column: str = "subject_id") -> str | None:
    """SQL keeping one subject in ``sample``, chosen by the id itself.

    Divisibility rather than a hash or a random draw: the same N always names the same
    subjects, in every table, on every machine, with nothing to seed.
    """
    if sample <= 0:
        return None
    return f"CAST({column} AS BIGINT) % {sample} = 0"


def long_rows_sql(
    table: str,
    ids: list[str],
    time_expr: str,
    time_alias: str,
    measures: list[tuple[str, str, str | None]],
    constants: dict[str, str] | None = None,
) -> str:
    """One wide row of values -> one row per non-empty cell.

    ``measures`` lists ``(column, name, unit)``; the unit is the one the source
    documentation states for the column, written as UCUM, or None for a text column.
    A constant column (``time_is_fallback``) is added verbatim.
    """
    consts = "".join(f", {expr} AS {name}" for name, expr in (constants or {}).items())
    parts = []
    for column, name, unit in measures:
        unit_sql = f"'{unit}'" if unit is not None else "CAST(NULL AS VARCHAR)"
        parts.append(
            f"SELECT {', '.join(ids)}, {time_expr} AS {time_alias}, '{name}' AS vital_name, "
            f"{column} AS value, {unit_sql} AS unit{consts} "
            f"FROM {table} WHERE {column} IS NOT NULL AND trim({column}) <> ''"
        )
    return "\nUNION ALL\n".join(parts)


def long_rows_adds_sql(table: str, measures: list[tuple[str, str, str | None]]) -> str:
    """Long rows minus wide rows: how many rows the split writes beyond what it read."""
    cells = " + ".join(f"count(*) FILTER (WHERE {c} IS NOT NULL AND trim({c}) <> '')" for c, _, _ in measures)
    return f"SELECT ({cells}) - count(*) FROM {table}"


def name_lookup_sql(prescriptions: str, main_type: str = "MAIN") -> str:
    """pharmacy_id -> the drug name its prescription rows carry (D-R18).

    An order has one MAIN row naming the drug and, for a compounded product, BASE and
    ADDITIVE rows naming the fluid and the additives. The MAIN name wins; an order with
    no MAIN row (a plain fluid bag) is named by its only other row; when several names
    remain at the same tier the alphabetically first is taken and ``candidate_names``
    says so, so the manifest can count the guesses. Nothing is dropped and nothing is
    joined by anything but the order id.
    """
    return f"""
    WITH named AS (
      SELECT pharmacy_id, drug, CASE WHEN drug_type = '{main_type}' THEN 0 ELSE 1 END AS tier
      FROM {prescriptions}
      WHERE pharmacy_id IS NOT NULL AND trim(pharmacy_id) <> ''
        AND drug IS NOT NULL AND trim(drug) <> ''
    ), best AS (
      SELECT pharmacy_id, min(tier) AS tier FROM named GROUP BY pharmacy_id
    ), candidates AS (
      SELECT n.pharmacy_id, n.drug FROM named n JOIN best b USING (pharmacy_id, tier)
      GROUP BY n.pharmacy_id, n.drug
    )
    SELECT pharmacy_id, min(drug) AS drug_recovered, count(*) AS candidate_names
    FROM candidates GROUP BY pharmacy_id
    """


def recovered_name_columns(alias: str, lookup: str) -> str:
    """The two columns the recovery adds to a row: the name, and that it was borrowed."""
    empty = f"({alias}.medication IS NULL OR trim({alias}.medication) = '')"
    return (
        f"CASE WHEN {empty} THEN {lookup}.drug_recovered END AS medication_recovered, "
        f"CASE WHEN {empty} AND {lookup}.drug_recovered IS NOT NULL THEN 1 ELSE 0 END AS name_from_linked_order"
    )


def unread_inputs(roots: dict[str, Path | None], read: dict[str, set[str]]) -> list[dict]:
    """Every delivered csv.gz this run did not emit a source from, with its reason.

    Lookups are listed too -- they were read, but nothing downstream sees them as a
    table -- so a coverage check comparing the delivery against the config finds every
    file accounted for. A file with no recorded reason is reported as such rather than
    omitted: that is the case the list exists to catch.
    """
    out: list[dict] = []
    for module, root in roots.items():
        if root is None or not root.is_dir():
            continue
        for path in sorted(root.glob("*.csv.gz")):
            roles = read.get(str(path), set())
            if "source" in roles:
                continue
            key = f"{module}/{path.name}"
            if "lookup" in roles:
                out.append({"path": str(path), "kind": "lookup", "reason": LOOKUP_REASON})
            else:
                out.append({"path": str(path), "kind": "unread", "reason": UNREAD_REASONS.get(key, "NO REASON RECORDED")})
    return out


class Prep:
    """Runs one denormalization step and records what it touched."""

    def __init__(self, con: duckdb.DuckDBPyConnection, out_dir: Path, sample: int = 0):
        self.con = con
        self.out_dir = out_dir
        self.sample = sample
        self.records: list[dict] = []
        self.inputs: dict[str, str] = {}
        self.input_roles: dict[str, set[str]] = {}
        self.raw_columns: dict[str, list[str]] = {}

    def columns_of(self, path: Path) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE SELECT * FROM read_csv('{path}', {CSV_OPTS})").fetchall()]

    def view(self, name: str, path: Path, *, lookup: bool = False) -> bool:
        """Register a gzipped CSV as a view. Missing inputs are reported, not fatal."""
        if not path.exists():
            print(f"  [skip] {name}: {path} not found", file=sys.stderr)
            return False
        columns = self.columns_of(path)
        predicate = sample_predicate(self.sample)
        where = f" WHERE {predicate}" if predicate and "subject_id" in columns else ""
        self.con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_csv('{path}', {CSV_OPTS}){where}"
        )
        key = str(path)
        if key not in self.inputs:
            self.inputs[key] = sha256_file(path)
        self.input_roles.setdefault(key, set()).add("lookup" if lookup else "source")
        self.raw_columns[name] = columns
        return True

    def emit(
        self,
        source_id: str,
        sql: str,
        driving: str,
        adds: str | None = None,
        raw: str | None = None,
        detail_raw: str | None = None,
    ) -> dict:
        """Materialize one prepared source, asserting the join did not change cardinality.

        ``adds`` is the one exception, stated rather than buried: a query counting the
        rows the preparation deliberately writes beyond what it read, because a cell
        held two facts (a blood pressure) or a row held eight (a vital-sign row). The
        manifest records the count so the excess is a declared number.

        ``raw`` names the view whose columns the output is a projection of, when it is
        not ``driving`` itself; ``detail_raw`` a second view folded in by a lookup.
        The columns of those views that the output does not carry are recorded with
        their reasons, or as unexplained.
        """
        target = self.out_dir / f"{source_id}.parquet"
        started = time.monotonic()
        expected = self.con.execute(f"SELECT count(*) FROM {driving}").fetchone()[0]
        added = self.con.execute(adds).fetchone()[0] if adds else 0
        self.con.execute(f"COPY ({sql}) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        actual = self.con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
        if actual != expected + added:
            raise SystemExit(
                f"{source_id}: {actual:,} rows written from {expected:,} in {driving}"
                f"{f' plus {added:,} declared' if added else ''}. "
                "A lookup join changed cardinality -- that is a duplicated or dropped fact, "
                "not a formatting difference."
            )
        columns = [r[0] for r in self.con.execute(f"DESCRIBE SELECT * FROM read_parquet('{target}')").fetchall()]
        not_projected: dict[str, str] = {}
        for view_name, reason_key in ((raw or driving, source_id), (detail_raw, detail_raw)):
            if view_name is None:
                continue
            reasons = NOT_PROJECTED.get(reason_key, {})
            for column in self.raw_columns.get(view_name, []):
                if column not in columns:
                    not_projected[column] = reasons.get(column, "NO REASON RECORDED")
        seconds = round(time.monotonic() - started, 1)
        print(
            f"  {source_id:22} {actual:>12,} rows  {seconds:>7.1f}s"
            + (f"  ({added:,} added by a declared split)" if added else "")
        )
        record = {
            "source_id": source_id,
            "rows": actual,
            "rows_added_by_split": added,
            "driving_table": driving,
            "columns": columns,
            "raw_columns_not_projected": not_projected,
            "seconds": seconds,
            "output_path": str(target),
            "output_sha256": sha256_file(target),
        }
        self.records.append(record)
        return record

    def recovery_counts(self, source_id: str, lookup: str) -> dict:
        """How the name recovery went for one output: from its parquet, not a guess."""
        target = self.out_dir / f"{source_id}.parquet"
        empty = "(o.medication IS NULL OR trim(o.medication) = '')"
        containers = ", ".join(f"'{c}'" for c in CONTAINER_NAMES)
        row = self.con.execute(
            f"""
            SELECT count(*) FILTER (WHERE {empty}),
                   count(*) FILTER (WHERE o.name_from_linked_order = 1),
                   count(*) FILTER (WHERE {empty} AND o.name_from_linked_order = 0),
                   count(*) FILTER (WHERE o.name_from_linked_order = 1 AND l.candidate_names > 1),
                   count(*) FILTER (WHERE o.name_from_linked_order = 1 AND o.medication_recovered IN ({containers}))
            FROM read_parquet('{target}') o
            LEFT JOIN {lookup} l ON l.pharmacy_id = o.pharmacy_id
            """
        ).fetchone()
        counts = {
            "rows_without_name": row[0],
            "recovered": row[1],
            "still_missing": row[2],
            "recovered_from_several_names": row[3],
            "recovered_name_is_a_container": row[4],
        }
        for rec in self.records:
            if rec["source_id"] == source_id:
                rec["name_recovery"] = counts
        print(f"    {source_id}: {row[0]:,} rows without a name, {row[1]:,} recovered, {row[2]:,} still missing, "
              f"{row[3]:,} from several names, {row[4]:,} name only a container")
        return counts


ED_VITALS: list[tuple[str, str, str | None]] = [
    # column, source code, UCUM unit as MIMIC-IV-ED documents the column (temperature
    # in degrees Fahrenheit, rates per minute, saturation in percent, pressures in mmHg).
    # Rhythm and pain are text: pain is a 0-10 score most of the time but also words.
    ("temperature", "ED_TEMPERATURE", "[degF]"),
    ("heartrate", "ED_HEARTRATE", "/min"),
    ("resprate", "ED_RESPRATE", "/min"),
    ("o2sat", "ED_O2SAT", "%"),
    ("sbp", "ED_SBP", "mm[Hg]"),
    ("dbp", "ED_DBP", "mm[Hg]"),
    ("rhythm", "ED_RHYTHM", None),
    ("pain", "ED_PAIN", None),
]

ED_TRIAGE: list[tuple[str, str, str | None]] = [m for m in ED_VITALS if m[0] != "rhythm"] + [
    # acuity is the Emergency Severity Index level 1-5, a number with no unit; the
    # chief complaint is free text and its own row, published as a text event (D-R7)
    ("acuity", "ED_ACUITY", None),
    ("chiefcomplaint", "ED_CHIEF_COMPLAINT", None),
]

ICU_EVENT_TABLES: list[tuple[str, str, str]] = [
    (
        "inputevents", "inputevents.csv.gz",
        "starttime, endtime, storetime, itemid, amount, amountuom, rate, rateuom, "
        "orderid, linkorderid, ordercategoryname, secondaryordercategoryname, "
        "ordercomponenttypedescription, ordercategorydescription, patientweight, "
        "totalamount, totalamountuom, isopenbag, continueinnextdept, statusdescription, "
        "originalamount, originalrate",
    ),
    (
        "ingredientevents", "ingredientevents.csv.gz",
        "starttime, endtime, storetime, itemid, amount, amountuom, rate, rateuom, "
        "orderid, linkorderid, statusdescription, originalamount, originalrate",
    ),
    ("outputevents", "outputevents.csv.gz", "charttime, storetime, itemid, value, valueuom"),
    (
        "procedureevents", "procedureevents.csv.gz",
        "starttime, endtime, storetime, itemid, value, valueuom, location, locationcategory, "
        "orderid, linkorderid, ordercategoryname, ordercategorydescription, patientweight, "
        "isopenbag, continueinnextdept, statusdescription, originalamount, originalrate",
    ),
    ("datetimeevents", "datetimeevents.csv.gz", "charttime, storetime, itemid, value, valueuom, warning"),
    ("chartevents", "chartevents.csv.gz", "charttime, storetime, itemid, value, valuenum, valueuom, warning"),
]


def build(mimic: Path, ed: Path | None, note: Path | None, out: Path, sample: int = 0,
          threads: int = 16, memory_limit: str = "48GB") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    con = duckdb.connect()
    con.execute(f"PRAGMA threads={threads}")
    con.execute(f"SET memory_limit='{memory_limit}'")
    p = Prep(con, out, sample=sample)

    hosp, icu = mimic / "hosp", mimic / "icu"

    # ---- dimensions (lookup only; never emitted on their own) --------------------
    for name, path in (
        ("d_labitems", hosp / "d_labitems.csv.gz"),
        ("d_icd_diagnoses", hosp / "d_icd_diagnoses.csv.gz"),
        ("d_icd_procedures", hosp / "d_icd_procedures.csv.gz"),
        ("d_items", icu / "d_items.csv.gz"),
    ):
        p.view(name, path, lookup=True)

    # ---- person ------------------------------------------------------------------
    # MIMIC gives an age together with the year it was current in, per patient. That is
    # strictly more than the hospital export had, so year_of_birth is derived exactly
    # rather than approximated -- the same code path, with the ambiguity removed.
    if p.view("patients", hosp / "patients.csv.gz"):
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

    # Every row, including the `discharge` rows: dropping them is a judgement, and it
    # is mimiciv.yaml's row_filter that makes it (D-R8), in the open.
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
                raw="diagnoses_icd",
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
                raw="procedures_icd",
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

    # One physical table, two kinds of fact (T2.M4). A row with an antibiotic name is a
    # susceptibility result; a row without one is what a culture grew, or that it grew
    # nothing. Measured over every row on 2026-09-14: every susceptibility row also
    # names its organism, and an organism that was tested against antibiotics has *no*
    # row of its own -- its 153,147 isolates exist only as susceptibility rows. So the
    # organism output is driven by the whole table, and the converter collapses the
    # rows of one isolate (same specimen and isolate number, same organism) into one
    # event with every row linked; the susceptibility output is driven by the rows
    # that carry an antibiotic. `specimen_isolate` is the discriminator that keeps two
    # isolates of one species in one specimen, or one species in two specimens drawn
    # at the same minute, apart.
    if p.view("microbiologyevents", hosp / "microbiologyevents.csv.gz"):
        con.execute(
            "CREATE OR REPLACE VIEW _micro_ab AS SELECT * FROM microbiologyevents "
            "WHERE ab_name IS NOT NULL AND trim(ab_name) <> ''"
        )
        p.emit(
            "micro_organisms",
            """
            SELECT microevent_id, subject_id, hadm_id, micro_specimen_id,
                   chartdate, charttime, storedate, storetime,
                   spec_type_desc, test_itemid, test_name, org_itemid, org_name, isolate_num,
                   quantity, comments,
                   micro_specimen_id || '/' || coalesce(isolate_num, '') AS specimen_isolate
            FROM microbiologyevents
            """,
            "microbiologyevents",
        )
        # `=>` is how the source spells "at least"; the converter's comparator form
        # reads `>=`. The original text stays in dilution_text, the readable spelling
        # is a new column (rule: normalization adds, never overwrites).
        p.emit(
            "micro_susceptibility",
            """
            SELECT microevent_id, subject_id, hadm_id, micro_specimen_id,
                   chartdate, charttime, storedate, storetime,
                   spec_type_desc, test_itemid, test_name, org_itemid, org_name, isolate_num,
                   ab_itemid, ab_name, dilution_text, dilution_comparison, dilution_value,
                   interpretation,
                   replace(dilution_text, '=>', '>=') AS dilution_result,
                   -- One susceptibility is one antibiotic tested against one isolate of
                   -- one organism in one specimen. The antibiotic is the event's code;
                   -- this is the rest, as the single column a discriminator can read.
                   -- Measured on the 1-in-100 sample of 2026-09-14: keyed on the
                   -- antibiotic alone 1,239 of 15,161 results merged, on the isolate
                   -- number 480, on specimen and isolate 386 -- isolate numbers repeat
                   -- across organisms within a specimen -- and on all three none.
                   micro_specimen_id || '/' || coalesce(isolate_num, '') || '/'
                     || coalesce(org_itemid, org_name, '') AS susceptibility_key
            FROM _micro_ab
            """,
            "_micro_ab",
            raw="microbiologyevents",
        )

    # omr carries a date but no time -- the untimed-measurement path, on public data.
    if p.view("omr", hosp / "omr.csv.gz"):
        # A blood pressure arrives as one cell, `120/80`, and OMOP records systolic and
        # diastolic as two measurements. As with the CU export, only rows whose name
        # says blood pressure and whose value is exactly `N/N` are divided; anything
        # else is carried through untouched. The unit is the one the result name
        # states, and a bare `Weight` or `Height` states none: those units are declared
        # in mimiciv.yaml (`declared_units`), with the evidence, and flagged on the
        # event, because a unit the source did not write is a decision, not a projection.
        p.emit(
            "omr",
            """
            WITH o AS (
              SELECT subject_id, chartdate, seq_num, result_name, result_value FROM omr
            ), bp AS (
              SELECT *, result_name LIKE 'Blood Pressure%'
                        AND regexp_matches(result_value, '^[0-9]+/[0-9]+$') AS splittable
              FROM o
            )
            SELECT subject_id, chartdate, seq_num, result_name, result_value,
                   CASE result_name WHEN 'Weight (Lbs)' THEN 'lb' WHEN 'Height (Inches)' THEN 'in'
                                    WHEN 'BMI (kg/m2)' THEN 'kg/m2' END AS unit
              FROM bp WHERE NOT splittable
            UNION ALL
            SELECT subject_id, chartdate, seq_num, result_name || ' systolic',
                   split_part(result_value, '/', 1), 'mmHg'
              FROM bp WHERE splittable
            UNION ALL
            SELECT subject_id, chartdate, seq_num, result_name || ' diastolic',
                   split_part(result_value, '/', 2), 'mmHg'
              FROM bp WHERE splittable
            """,
            "omr",
            adds="SELECT count(*) FROM omr WHERE result_name LIKE 'Blood Pressure%' "
                 "AND regexp_matches(result_value, '^[0-9]+/[0-9]+$')",
        )

    # ---- medications -------------------------------------------------------------
    if p.view("prescriptions", hosp / "prescriptions.csv.gz"):
        p.emit(
            "prescriptions",
            """
            SELECT subject_id, hadm_id, pharmacy_id, poe_id, starttime, stoptime,
                   drug_type, drug, formulary_drug_cd, gsn, ndc, prod_strength,
                   dose_val_rx, dose_unit_rx, route, doses_per_24_hrs,
                   -- the name with its strength and form, which is what the structured
                   -- drug reading needs: `Potassium Chloride` names nothing RxNorm can
                   -- pin down, `Potassium Chloride 10mEq ER Tablet` names one concept
                   CASE WHEN prod_strength IS NOT NULL AND trim(prod_strength) <> ''
                        THEN drug || ' ' || prod_strength ELSE drug END AS drug_full
            FROM prescriptions
            """,
            "prescriptions",
        )
        # The name an administration or dispensation without one can borrow from its
        # order (D-R18). Materialized once: it is a lookup keyed by the order id, and
        # the cardinality assertion on the joins below is what proves the key is unique.
        con.execute(f"CREATE OR REPLACE TABLE rx_names AS {name_lookup_sql('prescriptions')}")
    else:
        con.execute(
            "CREATE OR REPLACE TABLE rx_names (pharmacy_id VARCHAR, drug_recovered VARCHAR, candidate_names BIGINT)"
        )
    lookup_join = "LEFT JOIN rx_names n ON n.pharmacy_id = {alias}.pharmacy_id"

    if p.view("emar", hosp / "emar.csv.gz"):
        # `poe_id` and `pharmacy_id` were not projected before, so nothing downstream
        # could tell which order an administration carried out. They are the whole
        # reason an action stream can answer "was this ordered thing actually given".
        base = """SELECT e.subject_id, e.hadm_id, e.emar_id, e.emar_seq, e.poe_id,
                         e.pharmacy_id, e.charttime, e.medication, e.event_txt,
                         e.scheduletime, e.storetime"""
        recovered = recovered_name_columns("e", "n")
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
                                coalesce(d.dose_given_unit, '')), '') AS dose_given_text,
                    {recovered}
            FROM emar e LEFT JOIN detail d USING (emar_id) {lookup_join.format(alias='e')}
            """
            detail_raw = "emar_detail"
        else:
            print("  [skip] emar_detail: dose and route will be empty", file=sys.stderr)
            sql = f"{base}, {recovered} FROM emar e {lookup_join.format(alias='e')}"
            detail_raw = None
        p.emit("emar", sql, "emar", detail_raw=detail_raw)
        p.recovery_counts("emar", "rx_names")

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
            f"""
            SELECT ph.subject_id, ph.hadm_id, ph.pharmacy_id, ph.poe_id, ph.starttime, ph.stoptime,
                   ph.medication, ph.proc_type, ph.status, ph.entertime, ph.verifiedtime, ph.route,
                   ph.frequency, ph.dispensation, ph.fill_quantity,
                   {recovered_name_columns('ph', 'n')}
            FROM pharmacy ph {lookup_join.format(alias='ph')}
            """,
            "pharmacy",
        )
        p.recovery_counts("pharmacy", "rx_names")

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
        have_stays = p.view("edstays", ed / "edstays.csv.gz")
        if have_stays:
            # The visit type of an ED stay is fixed by the table it is in (D-R9): the
            # constant is projected here so the config maps a column like any other,
            # and the disposition stays what it is, where the stay discharged to.
            p.emit(
                "edstays",
                """
                SELECT subject_id, hadm_id, stay_id, intime, outtime, gender, race,
                       arrival_transport, disposition,
                       'Emergency' AS visit_type_constant
                FROM edstays
                """,
                "edstays",
            )
        # A wide row of vital signs is one row per sign here (T2.M2), with the unit the
        # ED module documents for each column; the split is declared, not silent.
        if p.view("ed_vitalsign", ed / "vitalsign.csv.gz"):
            p.emit(
                "ed_vitalsign",
                long_rows_sql("ed_vitalsign", ["subject_id", "stay_id"], "charttime", "charttime", ED_VITALS),
                "ed_vitalsign",
                adds=long_rows_adds_sql("ed_vitalsign", ED_VITALS),
            )
        # triage has no charttime at all. The stay's arrival time is carried as
        # `stay_intime`, and `time_is_fallback` says on every row that it is a stand-in;
        # the config turns that into the TIME_FALLBACK flag (D-R7, T2.M3).
        if p.view("ed_triage", ed / "triage.csv.gz") and have_stays:
            con.execute(
                "CREATE OR REPLACE VIEW _triage AS "
                "SELECT t.*, e.intime AS stay_intime FROM ed_triage t LEFT JOIN edstays e ON e.stay_id = t.stay_id"
            )
            p.emit(
                "ed_triage",
                long_rows_sql(
                    "_triage", ["subject_id", "stay_id"], "stay_intime", "stay_intime", ED_TRIAGE,
                    constants={"time_is_fallback": "1"},
                ),
                "ed_triage",
                adds=long_rows_adds_sql("_triage", ED_TRIAGE),
            )
        if p.view("ed_pyxis", ed / "pyxis.csv.gz"):
            p.emit(
                "ed_pyxis",
                "SELECT subject_id, stay_id, charttime, med_rn, name, gsn FROM ed_pyxis",
                "ed_pyxis",
            )
        if p.view("ed_diagnosis", ed / "diagnosis.csv.gz") and have_stays:
            p.emit(
                "ed_diagnosis",
                """
                SELECT d.subject_id, d.stay_id, d.seq_num, d.icd_code, d.icd_version,
                       d.icd_title, e.outtime AS stay_outtime
                FROM ed_diagnosis d
                LEFT JOIN edstays e ON e.stay_id = d.stay_id
                """,
                "ed_diagnosis",
            )

    # ---- intensive care (D-R13, T4.1-T4.3) ---------------------------------------
    # Every ICU table is read; d_items supplies the label behind each itemid the way
    # d_labitems does for the labs, and the caregiver id is the one column not carried.
    # chartevents is the largest table in MIMIC-IV (3.5 GB compressed); DuckDB streams
    # it, and `emit` reads it twice -- once to count, once to write -- which is the price
    # of the cardinality assertion and is paid knowingly.
    if p.view("icustays", icu / "icustays.csv.gz"):
        p.emit(
            "icustays",
            "SELECT subject_id, hadm_id, stay_id, first_careunit, last_careunit, intime, outtime, los FROM icustays",
            "icustays",
        )
    for sid, fname, cols in ICU_EVENT_TABLES:
        if p.view(sid, icu / fname):
            projected = ", ".join(f"x.{c.strip()}" for c in cols.split(",") if c.strip())
            p.emit(
                sid,
                f"""
                SELECT x.subject_id, x.hadm_id, x.stay_id, {projected},
                       di.label AS item_label
                FROM {sid} x LEFT JOIN d_items di ON di.itemid = x.itemid
                """,
                sid,
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

    unexplained = sorted(
        f"{r['source_id']}.{c}" for r in p.records for c, why in r["raw_columns_not_projected"].items()
        if why == "NO REASON RECORDED"
    )
    unread = unread_inputs({"hosp": hosp, "icu": icu, "ed": ed, "note": note}, p.input_roles)
    manifest = {
        "tool": "prepare_mimiciv.py",
        "tool_version": TOOL_VERSION,
        "sample": sample,
        "sample_rule": "subject_id % sample == 0 in every table with a subject_id; 0 means every subject",
        "inputs": [
            {"path": k, "sha256": v, "roles": sorted(p.input_roles.get(k, ()))}
            for k, v in sorted(p.inputs.items())
        ],
        "unread_inputs": unread,
        "outputs": p.records,
        "total_rows": sum(r["rows"] for r in p.records),
        "raw_columns_without_reason": unexplained,
        "wall_seconds": round(time.monotonic() - started, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "duckdb": {"threads": threads, "memory_limit": memory_limit, "version": duckdb.__version__},
    }
    (out / "prepare_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mimic-root", type=Path, required=True, help="mimiciv/3.1 (holds hosp/ and icu/)")
    ap.add_argument("--ed-root", type=Path, default=None, help="mimic-iv-ed/2.2/ed")
    ap.add_argument("--note-root", type=Path, default=None, help="mimic-iv-note/2.2/note")
    ap.add_argument("--out", type=Path, required=True, help="partition directory to write")
    ap.add_argument("--sample", type=int, default=0, help="keep the subjects whose id is divisible by N (0 = all)")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--memory-limit", default="48GB", help="DuckDB memory ceiling")
    args = ap.parse_args()

    m = build(args.mimic_root, args.ed_root, args.note_root, args.out,
              sample=args.sample, threads=args.threads, memory_limit=args.memory_limit)
    print(f"\n{len(m['outputs'])} sources, {m['total_rows']:,} rows -> {args.out}"
          + (f"  (sample 1 in {m['sample']})" if m["sample"] else ""))
    print(f"{len(m['unread_inputs'])} delivered files not emitted (lookups and unread), "
          f"{len(m['raw_columns_without_reason'])} unprojected columns without a reason")
    print(f"wall {m['wall_seconds']}s, peak RSS {m['peak_rss_mb']} MB")
    print(f"manifest: {args.out / 'prepare_manifest.json'}")


if __name__ == "__main__":
    main()
