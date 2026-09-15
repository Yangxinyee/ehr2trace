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
from ehr2trace.config import load_dataset_config
from ehr2trace.schema import QualityFlag
from tests.integration.test_ctpe_shape_anomalies import CONFIG, FIXTURE
from tests.integration.trap_builds import Build, build, edited


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

    # Trap 12: availability falls back to its default rule when nothing is declared, so
    # there is no declaration to take away. The other choice is declared instead, which is
    # what shows that a rule, and not the order rows arrive in, decides the value.
    labs = cfg.sources["labs"]
    cfg = edited(cfg, "labs", merge_rules={**labs.merge_rules, "available_time": {"rule": "latest"}})

    return build(cfg, FIXTURE, tmp_path_factory.mktemp("shape_traps_before"), publish=False)


# -- trap 1: two orders in one minute at two doses (T1.1) -----------------------------------

#: Partitions that extract the trap's patient; each carries its own copy of both orders.
#: The patient is one whose ages agree, so OMOP publishes them and their drug exposures.
COPIES = sum(shape.SECOND_DOSE_PATIENT in members for members in shape.MEMBERSHIP.values())


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
    """Trap 12 (T1.3, D-R5). No fault injects this; AVAILABILITY_BEFORE_EVENT is a different defect.

    The same white cell count reached one batch 35 minutes after the other -- later in
    batch 2 for one patient and in batch 1 for the other. Each is one event, available
    from the earlier time, flagged AVAILABILITY_MERGED, whichever batch saw it first.
    """
    for patient, trap in _late_results(after).items():
        first, last = trap["seen"]
        assert last - first == shape.LATE_BY, f"{patient}: the rows must disagree, or nothing was merged"
        assert trap["event"]["available_time"] == first, patient
        assert str(QualityFlag.AVAILABILITY_MERGED) in trap["event"]["quality_flags"], patient

    untouched = after.canonical().filter(
        (pl.col("source_id") == "labs")
        & pl.col("quality_flags").list.contains(str(QualityFlag.AVAILABILITY_MERGED))
    )
    assert untouched.height == len(shape.LATE_RESULT), "only the trap's results disagree about availability"

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
