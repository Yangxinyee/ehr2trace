"""The fixture trap of remediation plan T1.12 that depends on the dataset's clock (trap 3).

Both older fixtures declare UTC, where a local day is the UTC day, so a death recorded as a
date in one table and as an evening hour in another cannot land on two UTC days there.
``tests/fixtures/zoned_site/`` (``datasets/zoned_site.yaml``) is a third, deliberately
small fixture on New York's clock that exists only for this. ZS-1 died on 10 March 2021
by the registry's date and at 21:30 that evening by the admission record -- 02:30 UTC on
the 11th. ZS-2's two records name two different local days. It is a new fixture rather
than a new zone for the generic one because nothing else reads it: no expectation
elsewhere had to move for it.

T1.8 has two halves. The canonical layer merges a same-day pair into one death; that is
converter code, no declaration turns it off, and so its "before" cannot be a missing
declaration. The OMOP publisher compares local dates as well, for a layer written before
that merge existed, and that half reads the dataset's declared zone. The before test takes
the layer such a build wrote -- the pair unmerged -- and publishes it without the zone and
with it. Fault DEATH_DATE_AND_TIME_TREATED_AS_A_CONFLICT injects the same damage into a
finished build.
"""

from __future__ import annotations

import csv
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

import meds as meds_spec
from ehr2trace.faults import clone_work_tree
from ehr2trace.omop import build_omop
from ehr2trace.paths import WorkLayout
from ehr2trace.schema import EventKind, QualityFlag
from ehr2trace.validate import run_checks
from tests.integration.trap_builds import Build, build, run_named_checks

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "zoned_site"
CONFIG = Path(__file__).resolve().parents[2] / "datasets" / "zoned_site.yaml"

#: the patient whose two records of one death straddle midnight UTC, the one whose two
#: records name two local days, and one who is alive
SAME_DAY, TWO_DAYS, ALIVE = "ZS-1", "ZS-2", "ZS-3"
#: 21:30 on 10 March 2021 in New York, which had not yet moved to daylight saving time
TIMED_DEATH_UTC = datetime(2021, 3, 11, 2, 30)


@pytest.fixture(scope="module")
def after(tmp_path_factory) -> Build:
    return build(CONFIG, FIXTURE, tmp_path_factory.mktemp("zoned_traps_after"))


def _deaths(events: pl.DataFrame, b: Build, patient: str) -> pl.DataFrame:
    return events.filter(
        (pl.col("event_kind") == str(EventKind.death)) & (pl.col("subject_id") == b.subject(patient))
    )


def _death_rows(b: Build, patient: str) -> list[datetime]:
    return [row[0] for row in b.omop(
        "SELECT d.death_datetime FROM death d JOIN pmap p ON p.person_id = d.person_id WHERE p.subject_id = ?",
        [b.subject(patient)],
    )]


def _local_day(b: Build, instant: datetime) -> date:
    zone = ZoneInfo(b.cfg.time.timezone_assumption)
    return instant.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).date()


def _recorded(b: Build, patient: str) -> dict[str, tuple[str, datetime]]:
    """The patient's two records of their death: source row id and the instant each names."""
    out = {}
    for source_id, column in (("patients", "col__died_on"), ("admissions", "col__died_at")):
        rows = b.source_rows(source_id).filter(pl.col("person_source_id") == patient)
        assert rows.height == 1, (source_id, patient)
        out[source_id] = (rows["source_row_id"][0], b.instant(rows[column][0]))
    return out


def _meds_deaths(b: Build, patient: str) -> int:
    meds = b.meds()
    return meds.filter((pl.col("subject_id") == b.subject(patient)) & (pl.col("code") == meds_spec.death_code)).height


def test_a_date_and_an_evening_hour_on_one_local_day_are_one_death(after):
    """Trap 3, one day (T1.8, D-R11). Fault DEATH_DATE_AND_TIME_TREATED_AS_A_CONFLICT covers the same ground.

    One death, at the recorded hour, flagged DEATH_TIME_MERGED, with both records linked;
    one OMOP DEATH row and one MEDS death.
    """
    recorded = _recorded(after, SAME_DAY)
    (registry_row, as_a_date), (admission_row, timed) = recorded["patients"], recorded["admissions"]
    assert timed == TIMED_DEATH_UTC
    assert as_a_date.date() != timed.date(), "the pair must straddle midnight UTC, or the trap tests nothing"
    assert _local_day(after, as_a_date) == _local_day(after, timed) == date(2021, 3, 10)

    deaths = _deaths(after.canonical(), after, SAME_DAY)
    assert deaths.height == 1
    death = deaths.row(0, named=True)
    assert death["event_time"] == timed, "a recorded hour beats local midnight"
    assert str(QualityFlag.DEATH_TIME_MERGED) in death["quality_flags"]
    assert str(QualityFlag.DEATH_DATE_CONFLICT) not in death["quality_flags"]
    links = after.canonical("event_source").filter(pl.col("event_id") == death["event_id"])
    assert set(links["source_row_id"].to_list()) == {registry_row, admission_row}

    assert _death_rows(after, SAME_DAY) == [timed]
    assert _meds_deaths(after, SAME_DAY) == 1


def test_deaths_on_two_local_days_stay_two_events_and_publish_no_death(after):
    """Trap 3, two days (T1.8, D-R11). No fault injects this.

    Which record is wrong about the day cannot be read off the data, so both deaths stay,
    both are flagged DEATH_DATE_CONFLICT, no DEATH row is published, and the pair is
    listed in ``review/death_conflicts.csv`` for a person to settle.
    """
    deaths = _deaths(after.canonical(), after, TWO_DAYS)
    assert deaths.height == 2
    assert all(str(QualityFlag.DEATH_DATE_CONFLICT) in flags for flags in deaths["quality_flags"].to_list())
    assert {_local_day(after, t) for t in deaths["event_time"].to_list()} == {date(2021, 7, 2), date(2021, 7, 4)}

    assert _death_rows(after, TWO_DAYS) == []
    assert _meds_deaths(after, TWO_DAYS) == 2

    subject = after.subject(TWO_DAYS)
    issues = after.canonical("quality_issue").filter(pl.col("issue_type") == str(QualityFlag.DEATH_DATE_CONFLICT))
    assert issues["subject_id"].to_list() == [subject]
    with open(after.layout.review_dir / "death_conflicts.csv", newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh)) == [["subject_id", "dates"], [str(subject), "2021-07-02; 2021-07-04"]]


def test_the_zoned_build_passes_every_check(after):
    results = run_checks(after.cfg, after.layout, include_slow=True)
    failed = [f"{r.check_id}: {r.detail}" for r in results if not r.passed]
    assert not failed, failed
    (death,) = [r for r in results if r.check_id == "DEATH_PUBLISHED"]
    assert not death.skipped
    assert death.metrics["subjects_with_death"] == 2 and death.metrics["conflicting_subjects"] == 1


def test_before_the_merge_the_publisher_needs_the_declared_zone(after, tmp_path, monkeypatch):
    """Trap 3, before: ZS-1's two records as the two deaths a build before T1.8 left.

    Published without the declared zone, the publisher compares the stored UTC dates, finds
    the 10th and the 11th, withholds the DEATH row and reports a conflict, and
    DEATH_PUBLISHED -- which reads dates on the dataset's clock -- fails. Published with the
    zone, the same layer gives one DEATH row at the recorded hour.
    """
    monkeypatch.delenv("OMOP_VOCAB_DIR", raising=False)
    clone_work_tree(after.layout.root, tmp_path / "unmerged")
    layout = WorkLayout(root=tmp_path / "unmerged", dataset_id=after.cfg.dataset_id)
    # Judged on OMOP alone: MEDS publishes whatever canonical holds, zone or not.
    shutil.rmtree(layout.meds_dir)

    recorded = _recorded(after, SAME_DAY)
    registry_row, as_a_date = recorded["patients"]
    events = pl.read_parquet(layout.canonical_path("events"))
    survivor = _deaths(events, after, SAME_DAY).row(0, named=True)
    unmerged_id = "unmerged-death-recorded-as-a-date"
    as_recorded = {**survivor, "event_id": unmerged_id, "source_id": "patients",
                   "event_time": as_a_date, "available_time": as_a_date, "quality_flags": []}
    events = pl.concat([
        events.with_columns(
            pl.when(pl.col("event_id") == survivor["event_id"])
            .then(pl.col("quality_flags").list.eval(pl.element().filter(pl.element() != str(QualityFlag.DEATH_TIME_MERGED))))
            .otherwise(pl.col("quality_flags"))
            .alias("quality_flags")
        ),
        pl.DataFrame([as_recorded], schema=events.schema),
    ])
    # Only the death's link moves: the same registry row also carries the patient's sex and
    # age, and their lineage is what publishes the person.
    links = pl.read_parquet(layout.canonical_path("event_source"))
    links = links.with_columns(
        pl.when((pl.col("source_row_id") == registry_row) & (pl.col("event_id") == survivor["event_id"]))
        .then(pl.lit(unmerged_id)).otherwise(pl.col("event_id")).alias("event_id")
    )
    for name, frame in (("events", events), ("event_source", links)):
        path = layout.canonical_path(name)
        frame.write_parquet(path.with_suffix(".parquet.mutating"))
        path.with_suffix(".parquet.mutating").replace(path)
    unmerged = Build(after.cfg, layout)
    assert _deaths(unmerged.canonical(), unmerged, SAME_DAY).height == 2

    zoneless = after.cfg.model_copy(update={"time": after.cfg.time.model_copy(update={"timezone_assumption": None})})
    build_omop(zoneless, layout, vocabulary_dir=None)
    assert _death_rows(unmerged, SAME_DAY) == []
    conflicts = unmerged.omop(
        "SELECT count(*) FROM etl_audit.quality_issue WHERE issue_type = 'DEATH_DATE_CONFLICT' AND subject_id = ?",
        [after.subject(SAME_DAY)],
    )
    assert conflicts == [(1,)]
    results = run_named_checks(after.cfg, layout, "OMOP_EVERY_ROW_HAS_LINEAGE", "DEATH_PUBLISHED")
    assert results["OMOP_EVERY_ROW_HAS_LINEAGE"].passed, "the layer is sound apart from the unmerged death"
    result = results["DEATH_PUBLISHED"]
    assert not result.passed and result.metrics["omop_subjects_missing_death"] == 1

    build_omop(after.cfg, layout, vocabulary_dir=None)
    assert _death_rows(unmerged, SAME_DAY) == [TIMED_DEATH_UTC]
    result = run_named_checks(after.cfg, layout, "DEATH_PUBLISHED")["DEATH_PUBLISHED"]
    assert result.passed, result.detail
