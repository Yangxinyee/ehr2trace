"""The fixture traps of remediation plan T1.12 that live in the shape fixture.

Each trap is a raw shape one of the audited exports delivered and the converter
mishandled, rebuilt on fabricated rows by ``tools/make_shape_fixture.py`` (the constants
there name the rows). Every trap is asserted twice: on the fixture built with its own
declarations, where the output is right and the checks pass, and on the same raw files
built without the declaration that settles it, where the wrong output appears or the check
watching for it fails.

Where a fault in ``ehr2trace.faults`` injects the same damage into a finished build, the
test says which. The two prove different things: a fault that a check catches on a
corrupted artifact, a trap that the converter makes the right artifact from a raw source.
"""

from __future__ import annotations

import polars as pl
import pytest

import tools.make_shape_fixture as shape
from ehr2trace.canonical.values import ValueParsingSpec, parse_value
from ehr2trace.config import load_dataset_config
from ehr2trace.faults import clone_work_tree
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.reference import parsing_spec
from ehr2trace.schema import QualityFlag
from tests.integration.test_ctpe_shape_anomalies import CONFIG, FIXTURE
from tests.integration.trap_builds import Build, build, edited, run_named_checks
from tests.unit.test_unpunctuated_codes import write_vocab


@pytest.fixture(scope="module")
def after(tmp_path_factory) -> Build:
    return build(CONFIG, FIXTURE, tmp_path_factory.mktemp("shape_traps_after"))


@pytest.fixture(scope="module")
def before(tmp_path_factory) -> Build:
    """The same raw files, built without the declarations the traps are settled by.

    One build carries every removal. Each touches a different source, and each test below
    reads only its own trap's source.
    """
    cfg = load_dataset_config(CONFIG)

    # Trap 1: the order table with no dose role. The dose column is kept rather than
    # dropped, which is how a build reads a column its identity does not.
    rx = cfg.sources["all_rx"]
    cfg = edited(
        cfg, "all_rx",
        fields={role: spec for role, spec in rx.fields.items() if role != "dose"},
        keep_columns=[*rx.keep_columns, *rx.fields["dose"].from_],
    )

    # Trap 8: the problem list with neither its excluded status nor its status rule.
    cfg = edited(cfg, "problem_list", excluded_status=[], merge_rules={})

    # Trap 9: the component tables with no declared units.
    cfg = edited(cfg, "ekg", declared_units={})
    cfg = edited(cfg, "pft_values", declared_units={})

    # Trap 12: availability falls back to its default rule when nothing is declared, so
    # there is no declaration to take away. The other choice is declared instead, which is
    # what shows that a rule, and not the order rows arrive in, decides the value.
    labs = cfg.sources["labs"]
    cfg = edited(cfg, "labs", merge_rules={**labs.merge_rules, "available_time": {"rule": "latest"}})

    return build(cfg, FIXTURE, tmp_path_factory.mktemp("shape_traps_before"), publish=False)


def _partitions_of(patient: str) -> int:
    return sum(patient in members for members in shape.MEMBERSHIP.values())


def _flagged(frame: pl.DataFrame, flag: QualityFlag) -> list[bool]:
    return [str(flag) in flags for flags in frame["quality_flags"].to_list()]


# -- trap 1: two orders in one minute at two doses (T1.1) -----------------------------------

#: Partitions that extract the trap's patient; each carries its own copy of both orders.
#: The patient is one whose ages agree, so OMOP publishes them and their drug exposures.
COPIES = _partitions_of(shape.SECOND_DOSE_PATIENT)


def _second_dose_minute(b: Build):
    rows = b.source_rows("all_rx").filter(pl.col("col__HV_Discrete_Dose") == shape.SECOND_DOSE)
    assert rows["col__Ordering_Date"].n_unique() == 1, "the fixture writes the second dose at one minute"
    return b.instant(rows["col__Ordering_Date"][0])


def _orders_at(b: Build, minute) -> pl.DataFrame:
    return b.canonical().filter(
        (pl.col("source_id") == "all_rx")
        & (pl.col("subject_id") == b.subject(shape.SECOND_DOSE_PATIENT))
        & (pl.col("event_time") == minute)
    )


def test_two_orders_in_one_minute_at_two_doses_are_two_events(after):
    """Trap 1 (T1.1). Fault ORDERS_WITH_DIFFERENT_DOSES_MERGED covers the same ground on a built layer.

    One patient's order is written twice in the same minute for the same drug, once at
    each of two doses, and every partition that extracts the patient carries both. The
    identity reads the dose, so they are two orders, each still collapsing its copies
    from both batches into one event.
    """
    orders = _orders_at(after, _second_dose_minute(after))
    assert sorted(orders["dose_source"].to_list()) == ["10 mg", shape.SECOND_DOSE]

    links = after.canonical("event_source").filter(pl.col("event_id").is_in(orders["event_id"].to_list()))
    assert links.group_by("event_id").len()["len"].to_list() == [COPIES, COPIES], "every copy behind its own order"

    result = after.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert result.passed, result.detail
    assert not result.metrics["per_source"]["all_rx"]["disagreements"]

    published = after.omop(
        "SELECT DISTINCT d.drug_exposure_id, d.quantity FROM drug_exposure d "
        "JOIN etl_audit.lineage l ON l.target_table = 'drug_exposure' AND l.target_pk = d.drug_exposure_id "
        "WHERE list_contains(?, l.event_id)",
        [orders["event_id"].to_list()],
    )
    assert sorted(quantity for _pk, quantity in published) == [10.0, 20.0]


def test_without_the_dose_in_the_identity_the_two_orders_are_one_that_disagrees(before):
    """Trap 1, before: the order table built with its dose kept as a plain column.

    That is the identity the audit found -- one without the dose -- and the two orders
    collapse into one event whose rows disagree about the dose. DUPLICATES_AGREE, reading
    the kept column, fails on exactly that event.
    """
    orders = _orders_at(before, _second_dose_minute(before))
    assert orders.height == 1, "two orders of different strengths became one event"
    links = before.canonical("event_source").filter(pl.col("event_id") == orders["event_id"][0])
    assert links.height == 2 * COPIES, "both doses, from every partition, behind one event"

    result = before.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert not result.passed
    assert result.metrics["per_source"]["all_rx"]["disagreements"] == {"column:HV_Discrete_Dose": 1}


# -- trap 8: a deleted problem-list entry, and one entry's status told two ways (D-R6) -------


def _problem(b: Build, patient: str, code: str) -> pl.DataFrame:
    return b.canonical().filter(
        (pl.col("source_id") == "problem_list")
        & (pl.col("subject_id") == b.subject(patient))
        & (pl.col("source_code") == code)
    )


def _status_patient(status: str) -> str:
    (patient,) = {p for (_batch, p), s in shape.PROBLEM_STATUS.items() if s == status}
    return patient


def test_a_deleted_problem_list_entry_reaches_no_layer(after):
    """Trap 8, deleted entry (P-J11, D-R6). Fault EXCLUDED_STATUS_PUBLISHED covers the same ground on a built layer.

    The row is delivered and ingested in every partition that extracts the patient, and
    dropped before shaping: no event, no lineage, no quarantine, no condition in OMOP --
    which does publish this patient's other problems -- and no MEDS row.
    """
    patient, code = shape.DELETED_PROBLEM[:2]
    rows = after.source_rows("problem_list").filter(pl.col("col__Status") == "Deleted")
    assert rows.height == _partitions_of(patient), "the fixture must deliver the deleted entry"

    assert _problem(after, patient, code).is_empty()
    reached = set(rows["source_row_id"].to_list())
    assert not reached & set(after.canonical("event_source")["source_row_id"].to_list())
    assert not reached & set(after.canonical("quarantine")["source_row_id"].to_list())

    assert after.omop("SELECT count(*) FROM condition_occurrence WHERE condition_source_value = ?", [code]) == [(0,)]
    (published,) = after.omop(
        "SELECT count(*) FROM condition_occurrence c JOIN pmap p ON p.person_id = c.person_id WHERE p.subject_id = ?",
        [after.subject(patient)],
    )
    assert published[0] > 0, "the patient's other problems are published, so the absence is the exclusion"
    assert after.meds().filter(pl.col("code").str.to_lowercase().str.contains(code.lower(), literal=True)).is_empty()

    result = after.checks("EXCLUDED_STATUS_NOT_PUBLISHED")["EXCLUDED_STATUS_NOT_PUBLISHED"]
    assert result.passed and not result.skipped, result.detail


def test_a_recorded_status_outranks_an_empty_one_and_active_against_resolved_is_flagged(after):
    """Trap 8, two statuses (P-J2, D-R6). No fault injects this.

    One patient's asthma entry is empty in batch 1 and Active in batch 2: one recorded
    status, kept, and no conflict. Another's is Active in batch 1 and Resolved in batch
    2: Resolved is kept by the declared order and the event is flagged STATUS_CONFLICT.
    """
    quiet = _problem(after, _status_patient(""), "J45.909")
    assert quiet.height == 1 and quiet["status_source"].to_list() == ["Active"]
    assert _flagged(quiet, QualityFlag.STATUS_CONFLICT) == [False]

    contradicted = _problem(after, _status_patient("Resolved"), "J45.909")
    assert contradicted.height == 1 and contradicted["status_source"].to_list() == ["Resolved"]
    assert _flagged(contradicted, QualityFlag.STATUS_CONFLICT) == [True]

    result = after.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert result.passed, result.detail
    assert result.metrics["per_source"]["problem_list"]["ruled_disagreements"] == {"status_source": 1}


def test_built_without_the_exclusion_the_deleted_entry_is_published_and_the_check_says_so(after, before):
    """Trap 8, before: the problem list built without ``excluded_status``.

    The deleted entry becomes a diagnosis. EXCLUDED_STATUS_NOT_PUBLISHED needs the
    declaration to know what to look for, so it judges this build with the fixture's
    configuration: a build made before the status was declared excluded, read by the
    configuration that now declares it.
    """
    patient, code = shape.DELETED_PROBLEM[:2]
    published = _problem(before, patient, code)
    assert published["status_source"].to_list() == ["Deleted"]

    result = before.checks("EXCLUDED_STATUS_NOT_PUBLISHED", cfg=after.cfg)["EXCLUDED_STATUS_NOT_PUBLISHED"]
    assert not result.passed
    assert result.metrics["leaked"] == {"problem_list": 1}


def test_built_without_the_status_rule_the_two_statuses_are_a_merge_conflict(before):
    """Trap 8, before: the problem list built without its ``priority`` rule for the status."""
    contradicted = _problem(before, _status_patient("Resolved"), "J45.909")
    assert _flagged(contradicted, QualityFlag.MERGE_CONFLICT) == [True]
    assert _flagged(_problem(before, _status_patient(""), "J45.909"), QualityFlag.MERGE_CONFLICT) == [False]

    result = before.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert not result.passed
    assert result.metrics["per_source"]["problem_list"]["disagreements"] == {"status": 1}


# -- trap 9: components with no unit column (T1.7) -------------------------------------------

#: (source, component) -> the UCUM code its declared unit normalizes to
DECLARED = {
    ("ekg", "VENTRICULAR RATE"): "/min",
    ("ekg", "QRS DURATION"): "ms",
    ("ekg", "Q-T  INTERVAL"): "ms",
    ("pft_values", "FEV1 PCT PRED"): "%",
}
#: Unit concepts of a vocabulary made up for the test: the declared units, and the units
#: the laboratory rows state, so that every published unit has a concept to find.
UNIT_CONCEPTS = {"/min": 900001, "ms": 900002, "%": 900003, "mmol/L": 900004, "10*3/uL": 900005, "mg/dL": 900006}


def _component(b: Build, source_id: str, code: str) -> pl.DataFrame:
    rows = b.canonical().filter(
        (pl.col("source_id") == source_id) & (pl.col("event_kind") == "measurement") & (pl.col("source_code") == code)
    )
    assert rows.height > 0, (source_id, code)
    return rows


def test_a_component_with_no_unit_column_carries_the_unit_its_source_declares(after):
    """Trap 9 (P-C5, P-J7, T1.7). No fault injects this.

    The source states no unit and none is invented for ``unit_source``; the declaration
    reaches the normalized unit, the value passes through unchanged, and each event says
    the unit was declared. The diagnosis lines, which are text, declare nothing.
    """
    for (source_id, code), ucum in DECLARED.items():
        rows = _component(after, source_id, code)
        assert rows["unit_source"].null_count() == rows.height, code
        assert set(rows["unit_normalized"].to_list()) == {ucum}, code
        assert rows["value_number_normalized"].to_list() == rows["value_number"].to_list(), code
        assert all(_flagged(rows, QualityFlag.UNIT_DECLARED)), code
    text = _component(after, "ekg", "DIAGNOSIS")
    assert text["unit_normalized"].null_count() == text.height
    assert not any(_flagged(text, QualityFlag.UNIT_DECLARED))


def test_a_declared_unit_reaches_a_unit_concept(after, tmp_path, monkeypatch):
    """Trap 9: the unit concept path, on a copy of the build published with a vocabulary."""
    monkeypatch.delenv("OMOP_VOCAB_DIR", raising=False)
    vocabulary = write_vocab(tmp_path / "vocabulary", "".join(
        f"{concept}\tunit {code}\tUnit\tUCUM\tUnit\tS\t{code}\t19700101\t20991231\t\n"
        for code, concept in UNIT_CONCEPTS.items()
    ))
    clone_work_tree(after.layout.root, tmp_path / "published")
    layout = WorkLayout(root=tmp_path / "published", dataset_id=after.cfg.dataset_id)
    build_omop(after.cfg, layout, vocabulary_dir=vocabulary)
    published = Build(after.cfg, layout)

    declared = after.canonical().filter(pl.col("quality_flags").list.contains(str(QualityFlag.UNIT_DECLARED)))
    unit_of = dict(zip(declared["event_id"].to_list(), declared["unit_normalized"].to_list()))
    rows = published.omop(
        "SELECT DISTINCT m.measurement_id, l.event_id, m.unit_source_value, m.unit_concept_id FROM measurement m "
        "JOIN etl_audit.lineage l ON l.target_table = 'measurement' AND l.target_pk = m.measurement_id "
        "WHERE list_contains(?, l.event_id)",
        [list(unit_of)],
    )
    assert rows, "declared measurements were published"
    for _pk, event_id, unit_source_value, concept in rows:
        assert unit_source_value is None
        assert concept == UNIT_CONCEPTS[unit_of[event_id]]

    result = run_named_checks(after.cfg, layout, "UNIT_CONCEPT_COVERAGE")["UNIT_CONCEPT_COVERAGE"]
    assert result.passed and not result.skipped, result.detail
    assert result.metrics["declared_units"] == len({pk for pk, *_ in rows})


def test_without_declared_units_the_same_components_carry_no_unit_at_all(before):
    """Trap 9, before: the component tables built without ``declared_units``."""
    for (source_id, code), _ucum in DECLARED.items():
        rows = _component(before, source_id, code)
        assert rows["unit_normalized"].null_count() == rows.height, code
        assert not any(_flagged(rows, QualityFlag.UNIT_DECLARED)), code


# -- trap 10: a diagnosis line that begins with a number (T1.9) ----------------------------


def test_a_numbered_diagnosis_line_is_text_not_a_number_with_a_unit(after):
    """Trap 10 (P-C8, P-J8, T1.9). Fault DIAGNOSIS_TEXT_READ_AS_A_UNIT covers the same ground on a built layer."""
    lines = after.canonical().filter(
        (pl.col("source_id") == "ekg") & (pl.col("value_text") == shape.NUMBERED_DIAGNOSIS)
    )
    patients = {p for members in shape.MEMBERSHIP.values() for p in members}
    assert lines.height == len(patients), "one diagnosis line on each patient's ECG"
    assert lines["value_number"].null_count() == lines.height
    assert lines["unit_source"].null_count() == lines.height
    assert all(_flagged(lines, QualityFlag.NON_NUMERIC_RESULT))

    result = after.checks("UNIT_KNOWN")["UNIT_KNOWN"]
    assert result.passed and not result.skipped, result.detail
    assert result.metrics["undeclared_spellings"] == 0


def test_read_without_the_unit_table_the_same_cell_is_two_of_something(after):
    """Trap 10, before: the cell read the way the value parser reads it with no unit table.

    The unit table is not a declaration a source can leave out: a build always reads it,
    and with no table at all it accepts no unit rather than any. What T1.9 replaced is the
    parser's rule for a caller with no table, which took any words after a number for its
    unit -- so that is the reading shown here, on the fixture's own cell.
    """
    cells = after.source_rows("ekg").filter(pl.col("col__Result_Value") == shape.NUMBERED_DIAGNOSIS)
    assert cells.height > 0
    cell = cells["col__Result_Value"][0]

    loose = parse_value(cell, None, ValueParsingSpec())
    assert (loose.number, loose.unit) == (2.0, "SINUS TACHYCARDIA")

    read = parse_value(cell, None, parsing_spec())
    assert (read.number, read.unit, read.text) == (None, None, shape.NUMBERED_DIAGNOSIS)


# -- trap 12: one result seen by two extracts at two times (T1.3) -----------------------------


def _late_results(b: Build) -> dict[str, dict]:
    """Per trap patient: the white cell count event and the result times of the rows behind it."""
    events = b.canonical().filter(
        (pl.col("source_id") == "labs") & (pl.col("source_code") == shape.LATE_RESULT_CODE)
    )
    rows = b.source_rows("labs").select("source_row_id", "col__Result_Time")
    behind = b.canonical("event_source").join(rows, on="source_row_id")
    out: dict[str, dict] = {}
    for patient in sorted({p for _batch, p in shape.LATE_RESULT}):
        mine = events.filter(pl.col("subject_id") == b.subject(patient))
        assert mine.height == 1, f"{patient}: one result, however many extracts carry it"
        event = mine.row(0, named=True)
        cells = behind.filter(pl.col("event_id") == event["event_id"])["col__Result_Time"].unique().to_list()
        out[patient] = {"event": event, "seen": sorted(b.instant(c) for c in cells)}
    return out


def test_a_result_two_extracts_saw_at_two_times_is_available_from_the_earlier(after):
    """Trap 12 (P-J3, T1.3, D-R5). No fault injects this; AVAILABILITY_PRECEDES_OCCURRENCE is a different defect.

    The same white cell count reached one batch 35 minutes after the other -- later in
    batch 2 for one patient and in batch 1 for the other. Each is one event, available
    from the earlier time, flagged AVAILABILITY_MERGED, whichever batch saw it first.
    """
    for patient, trap in _late_results(after).items():
        first, last = trap["seen"]
        assert last - first == shape.LATE_BY, f"{patient}: the rows must disagree, or nothing was merged"
        assert trap["event"]["available_time"] == first, patient
        assert str(QualityFlag.AVAILABILITY_MERGED) in trap["event"]["quality_flags"], patient

    merged = after.canonical().filter(
        (pl.col("source_id") == "labs")
        & pl.col("quality_flags").list.contains(str(QualityFlag.AVAILABILITY_MERGED))
    )
    assert merged.height == len(shape.LATE_RESULT), "only the trap's results disagree about availability"

    result = after.checks("DUPLICATES_AGREE")["DUPLICATES_AGREE"]
    assert result.passed, result.detail
    assert result.metrics["per_source"]["labs"]["availability_disagreements"] == len(shape.LATE_RESULT)


def test_declared_the_other_way_the_same_result_is_available_from_the_later(before):
    """Trap 12, before: the same rows under ``available_time: latest``.

    The default rule has no declaration to remove. Declaring the opposite choice moves
    both events to the later time, so the value above came from the rule and not from
    which batch's row the merge happened to meet first.
    """
    for patient, trap in _late_results(before).items():
        first, last = trap["seen"]
        assert trap["event"]["available_time"] == last != first, patient


# -- all of them, whatever the worker count ---------------------------------------------------


def test_four_workers_build_these_traps_exactly_as_one_does(after, tmp_path):
    """The merges these traps exercise -- two doses, a status priority, availability -- must
    not depend on which process built which bucket. The generic fixture's traps are held to
    the same by ``test_reproducibility.py``; this fixture is not built there.
    """
    from ehr2trace.digest import changed, fingerprint

    reference = fingerprint(after.layout)
    assert reference, "nothing was built, so agreement would be vacuous"
    parallel = build(CONFIG, FIXTURE, tmp_path / "four_workers", workers=4)
    assert changed(reference, fingerprint(parallel.layout)) == []
