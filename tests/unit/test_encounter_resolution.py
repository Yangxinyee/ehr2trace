"""What an encounter id is resolved against, and how often it resolves.

ENCOUNTER_RESOLVES was written for datasets whose visits are keyed by encounter, where an
event's encounter names a visit or it names nothing. One delivery has no such table: its
readmissions and intensive-care stays carry no encounter id at all, so every source
resolved 0% and the check failed everything for a fact about the delivery. There, an
encounter id is only ever shared between the tables that carry it, and the rate that
means anything -- the one its own configuration declares for notes -- is how often
another source knows the same (patient, encounter). These tests pin both readings.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from ehr2trace.config import load_dataset_config
from ehr2trace.validate import encounter_link_rates, encounter_reference

CONFIG = Path(__file__).resolve().parents[2] / "datasets" / "ctpe_shape.yaml"


def engine(rows: list[tuple[str, int, str | None, str]]):
    frame = pl.DataFrame(rows, schema=["source_id", "subject_id", "encounter_id", "event_kind"], orient="row")
    con = duckdb.connect()
    con.register("evt_frame", frame.to_arrow())
    con.execute("CREATE VIEW evt AS SELECT * FROM evt_frame")
    return con


def test_a_dataset_with_encounter_keyed_visits_resolves_against_them():
    cfg = load_dataset_config(CONFIG)
    assert encounter_reference(cfg) == "visits"


def test_a_dataset_whose_visits_carry_no_encounter_resolves_against_its_other_sources():
    cfg = load_dataset_config(CONFIG)
    sources = dict(cfg.sources)
    for sid, spec in cfg.sources.items():
        if spec.shape == "visit":
            fields = {role: f for role, f in spec.fields.items() if role != "encounter_id"}
            sources[sid] = spec.model_copy(update={"fields": fields})
    assert encounter_reference(cfg.model_copy(update={"sources": sources})) == "sources"


def test_against_visits_an_encounter_must_name_a_visit_of_the_same_subject():
    con = engine([
        ("visits", 1, "E1", "visit"),
        ("labs", 1, "E1", "measurement"),    # resolves
        ("labs", 2, "E1", "measurement"),    # same id, another subject: does not
        ("labs", 1, "E9", "measurement"),    # no such visit: does not
        ("notes", 1, None, "note"),          # carries no encounter: not counted
    ])
    rates = encounter_link_rates(con, "visits")
    assert rates == {"labs": {"with_encounter": 3, "linked": 1, "rate": 0.3333}}


def test_against_sources_an_encounter_must_be_known_to_another_source():
    con = engine([
        ("notes", 1, "E1", "note"),
        ("notes", 1, "E1", "note"),          # a second note under the same encounter
        ("notes", 1, "E2", "note"),          # only the notes know E2
        ("diagnoses", 1, "E1", "condition"),
        ("diagnoses", 2, "E2", "condition"),  # E2 of another subject is not the same encounter
        ("stays", 1, "E2", "visit_detail"),  # visits are not a source to be shared with here
    ])
    rates = encounter_link_rates(con, "sources")
    assert rates["notes"] == {"with_encounter": 3, "linked": 2, "rate": 0.6667}
    assert rates["diagnoses"] == {"with_encounter": 2, "linked": 1, "rate": 0.5}
    assert "stays" not in rates
