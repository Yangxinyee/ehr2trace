"""A canonical layer written by hand, for the publisher tests of the remediation plan.

Each publisher test states one thing the OMOP or MEDS builder does with a canonical
event, so the layer is assembled event by event rather than run through the pipeline:
the traps -- a date-only death beside a timed one, a transfer with no encounter id, a
dose whose unit lives in another column, a code named two ways -- have to be exactly
where a test says they are. The builders read the canonical schema, not the pipeline,
so a file with the version-2 columns filled in by hand is the same input a rebuilt
layer will be.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl

import meds as meds_spec
from ehr2trace.config import DatasetConfig
from ehr2trace.paths import WorkLayout, rows_to_table, write_table_atomic
from ehr2trace.schema import CANONICAL_EVENT_SCHEMA, EVENT_SOURCE_SCHEMA, EventKind

ZONE = "America/New_York"
DATASET = "hand_built"
#: subjects: one with visits and orders, one with two same-day deaths, one with a
#: cross-day death conflict, one with a single death
WARD, SAME_DAY, CROSS_DAY, SINGLE = 101, 102, 103, 104

T = datetime


def config(zone: str | None = ZONE) -> DatasetConfig:
    time = {"timezone_assumption": zone} if zone else {"naive_time_policy": "store_naive_flagged"}
    return DatasetConfig.model_validate(
        {
            "dataset_id": DATASET,
            "identity": {"person_key": "PID"},
            "partitions": [{"id": "p1", "dir": "p1"}],
            "time": time,
            "sources": {
                "hand": {
                    "adapter": "parquet", "shape": "point_event", "event_kind": "measurement",
                    "fields": {"person_id": {"from": ["PID"]}, "event_time": {"from": ["T"]},
                               "source_code": {"from": ["K"]}},
                },
            },
            "omop": {
                "person_birth_policy": {
                    "mode": "approved_approximation", "age_as_of_date": "2022-01-01",
                    "approval_note": "hand-built fixture: ages are as of 2022-01-01",
                },
                "source_release_date": "2026-01-01",
                "cdm_release_date": "2026-01-01",
            },
        }
    )


def event(event_id: str, subject_id: int, kind: EventKind, **over) -> dict:
    row = {name: None for name in CANONICAL_EVENT_SCHEMA.names}
    row.update(
        event_id=event_id, subject_id=subject_id, event_kind=str(kind), code_system="SOURCE",
        provenance_status="observed", mapping_version="0", quality_flags=[], source_id="hand",
    )
    row.update(over)
    return row


def demographics(subject_id: int) -> list[dict]:
    return [
        event(f"age-{subject_id}", subject_id, EventKind.demographic, source_code="AGE", value_number=60.0),
        event(f"sex-{subject_id}", subject_id, EventKind.demographic, source_code="GENDER", value_text="F"),
    ]


def write_canonical(root: Path, events: list[dict], drop_columns: tuple[str, ...] = ()) -> WorkLayout:
    """Write ``events`` and one source row per event; ``drop_columns`` mimics an older file."""
    layout = WorkLayout(root=root / DATASET, dataset_id=DATASET).ensure()
    table = rows_to_table(events, CANONICAL_EVENT_SCHEMA)
    if drop_columns:
        table = table.drop_columns(list(drop_columns))
    write_table_atomic(table, layout.canonical_path("events"))
    links = [
        {"event_id": e["event_id"], "source_row_id": f"row:{e['event_id']}",
         "relation": "derived_from", "partition_id": "p1"}
        for e in events
    ]
    write_table_atomic(rows_to_table(links, EVENT_SOURCE_SCHEMA), layout.canonical_path("event_source"))
    return layout


def the_layer() -> list[dict]:
    """Every trap the publisher tests need, on four subjects."""
    ward = [
        *demographics(WARD),
        # an admission with a nested ICU stay; the admission says where it discharged to
        event("visit-adm", WARD, EventKind.visit, encounter_id="ENC-1", source_code="INPATIENT",
              event_time=T(2020, 3, 1, 10), end_time=T(2020, 3, 5, 10), discharged_to="HOME"),
        event("visit-icu", WARD, EventKind.visit, encounter_id="ENC-2", source_code="ICU",
              event_time=T(2020, 3, 2, 0), end_time=T(2020, 3, 2, 12)),
        # details: by encounter, by containment (two candidates), encounter beats
        # containment, an unknown encounter falls back to containment, and one that
        # nothing contains
        event("detail-by-encounter", WARD, EventKind.visit_detail, encounter_id="ENC-1",
              source_code="WARD_A", event_time=T(2020, 3, 1, 12), end_time=T(2020, 3, 2, 0),
              discharged_to="ICU"),
        event("detail-nested", WARD, EventKind.visit_detail, source_code="PROC_ROOM",
              event_time=T(2020, 3, 2, 6), end_time=T(2020, 3, 2, 8)),
        event("detail-encounter-wins", WARD, EventKind.visit_detail, encounter_id="ENC-1",
              source_code="WARD_B", event_time=T(2020, 3, 2, 6), end_time=T(2020, 3, 2, 7)),
        event("detail-unknown-encounter", WARD, EventKind.visit_detail, encounter_id="ENC-9",
              source_code="WARD_C", event_time=T(2020, 3, 1, 11), end_time=T(2020, 3, 1, 13)),
        event("detail-orphan", WARD, EventKind.visit_detail, source_code="WARD_D",
              event_time=T(2020, 7, 1, 9), end_time=T(2020, 7, 1, 10)),
        # orders: the unit in the dose text, the unit in its own column, a rate, a rate
        # on a dose text longer than the column, a rate with no dose, and a dose that is
        # not a number
        event("order-unit-in-dose", WARD, EventKind.drug_order, source_code="apixaban",
              event_time=T(2020, 3, 1, 12), dose_source="5 mg"),
        event("order-unit-in-column", WARD, EventKind.drug_order, source_code="apixaban",
              event_time=T(2020, 3, 1, 13), dose_source="5", unit_source="mg"),
        event("order-with-rate", WARD, EventKind.drug_order, source_code="heparin",
              event_time=T(2020, 3, 1, 14), dose_source="5 mg", rate=10.0, rate_source="10",
              rate_unit="mL/hr"),
        event("order-long-with-rate", WARD, EventKind.drug_order, source_code="heparin",
              event_time=T(2020, 3, 1, 15), dose_source="x" * 300, rate=2.0, rate_source="2",
              rate_unit="units/hr"),
        event("order-rate-only", WARD, EventKind.drug_order, source_code="heparin",
              event_time=T(2020, 3, 1, 16), rate=1.5, rate_source="1.5"),
        event("order-text-dose", WARD, EventKind.drug_order, source_code="apixaban",
              event_time=T(2020, 3, 1, 17), dose_source="one tablet"),
        # a measurement with a unit, and one code named two ways
        event("creat", WARD, EventKind.measurement, source_code="CREAT", source_name="Creatinine",
              event_time=T(2020, 3, 1, 12), value_number=1.0, unit_source="mg/dL",
              unit_normalized="mg/dL", value_number_normalized=1.0),
        *[
            event(f"spo2-{i}", WARD, EventKind.measurement, source_code="SPO2",
                  source_name="Post SpO2" if i == 0 else "SpO2",
                  event_time=T(2020, 3, 1, 12, i), value_number=97.0, unit_source="%")
            for i in range(4)
        ],
        # a fact that is neither condition, drug, procedure nor measurement
        event("followup", WARD, EventKind.observation, source_code="FOLLOWUP", value_text="alive",
              event_time=T(2020, 4, 1, 9)),
    ]
    same_day = [
        *demographics(SAME_DAY),
        # local midnight on 1 January (a date-only registry entry) and 21:30 that
        # evening (an admission record), which is 02:30 UTC on 2 January
        event("death-date-only", SAME_DAY, EventKind.death, event_time=T(2020, 1, 1, 5, 0)),
        event("death-timed", SAME_DAY, EventKind.death, event_time=T(2020, 1, 2, 2, 30)),
        # a detail for a person who has no visit at all
        event("detail-no-visits", SAME_DAY, EventKind.visit_detail, source_code="WARD_E",
              event_time=T(2020, 3, 1, 12), end_time=T(2020, 3, 1, 13)),
    ]
    cross_day = [
        *demographics(CROSS_DAY),
        event("death-jan-1", CROSS_DAY, EventKind.death, event_time=T(2020, 1, 1, 5, 0)),
        event("death-jan-5", CROSS_DAY, EventKind.death, event_time=T(2020, 1, 5, 15, 0)),
    ]
    single = [
        *demographics(SINGLE),
        event("death-single", SINGLE, EventKind.death, event_time=T(2020, 2, 1, 12, 0)),
    ]
    return ward + same_day + cross_day + single


# -- reading back ------------------------------------------------------------------------


def query(layout: WorkLayout, sql: str, params: list | None = None) -> list[tuple]:
    con = duckdb.connect(str(layout.omop_dir / "omop.duckdb"), read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def one(layout: WorkLayout, sql: str, params: list | None = None) -> tuple:
    rows = query(layout, sql, params)
    assert len(rows) == 1, rows
    return rows[0]


def lineage_of(layout: WorkLayout, table: str, event_id: str) -> list[int]:
    return [r[0] for r in query(
        layout,
        "SELECT target_pk FROM etl_audit.lineage WHERE target_table = ? AND event_id = ?",
        [table, event_id],
    )]


def drug_row(layout: WorkLayout, event_id: str) -> tuple:
    return one(
        layout,
        "SELECT d.quantity, d.dose_unit_source_value, d.sig FROM drug_exposure d "
        "JOIN etl_audit.lineage l ON l.target_table = 'drug_exposure' AND l.target_pk = d.drug_exposure_id "
        "WHERE l.event_id = ?",
        [event_id],
    )


def meds_frame(layout: WorkLayout) -> pl.DataFrame:
    return pl.read_parquet(sorted((layout.meds_dir / meds_spec.data_subdirectory).rglob("*.parquet")))
