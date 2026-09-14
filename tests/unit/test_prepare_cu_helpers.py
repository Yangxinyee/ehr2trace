"""The bookkeeping helpers of the CU-CTPA preparation step (P-CU11, D-R16).

The audit of 2026-09-13 found the prepare manifest recording a null hash for every
raw input, 129 rows leaving the extract unrecorded, and no statement anywhere about
the files under the delivery the script never opens. What keeps those three from
coming back is not the manifest -- a manifest reports whatever it is handed -- but
these helpers, which are the part of ``tools/prepare_cu.py`` that runs without the
export in front of it and can therefore be tested at all.

Nothing here reads the delivery. The fixtures are empty files with the delivery's
names, because a reason and a hash are properties of the path and the bytes, not of
the records inside.
"""

from __future__ import annotations

import hashlib

import pytest

from tools.prepare_cu import (
    IDENTIFYING_IMAGING_COLUMNS,
    input_record,
    list_unread,
    sha256_file,
    unread_reason,
)

#: One path per family the delivery actually contains, with the word that must appear
#: in its reason. A family whose reason falls through to the catch-all is a file
#: nobody decided about, which is the state the audit found.
DELIVERY_FAMILIES = [
    ("__MACOSX/._C4225_T1_PatientDemographics_20260318.csv", "macOS"),
    ("._C4225_T2_Flowsheets_20260318.csv", "macOS"),
    (".DS_Store", "macOS"),
    (".idea/workspace.xml", "IDE"),
    (".ipynb_checkpoints/analysis-checkpoint.ipynb", "notebook"),
    ("analysis.ipynb", "notebook"),
    (".~lock.Table1.xlsx#", "lock"),
    ("Building an AI radiologist using vision-language models.zip", "archive"),
    ("summary_deck.pptx", "documentation"),
    ("assignment_letter.docx", "documentation"),
    ("requirements.xlsx", "documentation"),
    ("icd_cpt_list.pdf", "documentation"),
    ("patient_level_diagnosis_binary_flags.csv", "derivation"),
    ("Table4_missingness_relative_to_table1.csv", "summaries"),
    ("Table7_medication_name_missing_rate_vs_T1.csv", "summaries"),
    ("not_found_series.csv", "copy_summary.csv"),
    ("patient_copy_status.csv", "copy_summary.csv"),
]


@pytest.mark.parametrize("relative_path,expected_word", DELIVERY_FAMILIES)
def test_every_delivered_family_has_its_own_reason(tmp_path, relative_path, expected_word):
    reason = unread_reason(tmp_path.joinpath(relative_path).relative_to(tmp_path))
    assert expected_word in reason, f"{relative_path}: {reason}"
    assert reason != unread_reason(tmp_path.joinpath("mystery.bin").relative_to(tmp_path)), (
        f"{relative_path} falls through to the catch-all reason"
    )


def test_an_unrecognised_file_still_gets_a_reason(tmp_path):
    """The catch-all says "not read", never nothing: silence is what is being fixed."""
    reason = unread_reason(tmp_path.joinpath("something_new.csv").relative_to(tmp_path))
    assert reason and "not a table this conversion reads" in reason


def test_a_macos_resource_fork_is_named_by_its_directory_not_its_extension(tmp_path):
    """``__MACOSX/.../T1.csv`` ends in .csv and is not a table.

    The fork carries the name of the file it shadows, so a reason chosen by suffix
    would call it a delivered table and a coverage check would look for its rows.
    """
    forked = tmp_path / "__MACOSX" / "sub" / "C4225_T1_PatientDemographics_20260318.csv"
    assert "macOS" in unread_reason(forked.relative_to(tmp_path))


def test_list_unread_names_every_file_no_step_opened(tmp_path):
    read = tmp_path / "C4225_T1_PatientDemographics_20260318.csv"
    read.write_text("arb_person_id\n")
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "__MACOSX" / "._C4225_T1_PatientDemographics_20260318.csv").write_bytes(b"\x00")
    (tmp_path / "summary_deck.pptx").write_bytes(b"PK\x03\x04")
    nested = tmp_path / "CU_Images" / "links_files" / "2016"
    nested.mkdir(parents=True)
    (nested / "not_found_series.csv").write_text("accession\n")

    unread = list_unread(tmp_path, {read.resolve()})

    paths = {entry["path"] for entry in unread}
    assert str(read) not in paths, "a file the script read must not be listed as unread"
    assert len(unread) == 3
    assert all(entry["reason"] for entry in unread), "every unread file needs a reason"
    assert {entry["path"] for entry in unread} == {
        str(tmp_path / "__MACOSX" / "._C4225_T1_PatientDemographics_20260318.csv"),
        str(tmp_path / "summary_deck.pptx"),
        str(nested / "not_found_series.csv"),
    }


def test_list_unread_is_empty_when_every_file_was_read(tmp_path):
    one = tmp_path / "C4225_T5_ClinicalOutcomes_20260310.csv"
    one.write_text("arb_person_id\n")
    assert list_unread(tmp_path, {one.resolve()}) == []


def test_sha256_file_is_the_files_own_digest_across_chunk_boundaries(tmp_path):
    """The audit's finding was a null hash, so the digest is checked against hashlib.

    A chunk size smaller than the file exercises the streaming loop: the CSVs this
    reads are up to 22 GB and are never held in memory.
    """
    blob = bytes(range(256)) * 97
    path = tmp_path / "C4225_T7_Medications_20260310.csv"
    path.write_bytes(blob)
    expected = hashlib.sha256(blob).hexdigest()
    assert sha256_file(path) == expected
    assert sha256_file(path, chunk=7) == expected


def test_input_record_carries_the_hash_the_size_and_the_row_count(tmp_path):
    path = tmp_path / "C4225_T4_Diagnoses_20260318.csv"
    path.write_bytes(b"code,code_description\n")
    record = input_record(path, 5398425)
    assert record["path"] == str(path)
    assert record["bytes"] == path.stat().st_size
    assert record["sha256"] == sha256_file(path)
    assert record["rows"] == 5398425


def test_input_record_keeps_a_row_count_it_was_not_given_explicitly_null(tmp_path):
    """None means "not counted", which must not silently become zero."""
    path = tmp_path / "links.csv"
    path.write_bytes(b"Accession Number\n")
    assert input_record(path, None)["rows"] is None


def test_the_imaging_step_never_names_an_identifying_column(repo_root):
    """The imaging metadata carries names and birth dates; the manifest must not.

    ``imaging_manifest`` asserts at run time that no table it builds carries one of
    these columns, which only fires when the imaging directory is present. This is
    the same guarantee read off the source: the function's body may not mention an
    identifying column at all, so a projection added later fails here rather than
    in a published manifest (D-R16).
    """
    source = (repo_root / "tools" / "prepare_cu.py").read_text(encoding="utf-8")
    body = source.split("\ndef imaging_manifest(", 1)[1]
    named = [column for column in IDENTIFYING_IMAGING_COLUMNS if column in body]
    assert named == [], f"imaging_manifest names identifying columns {named}"


def test_the_identifying_column_list_covers_both_the_original_and_the_new_spellings():
    """links.csv carries the patient's identifiers twice, before and after mapping."""
    for stem in ("Patient ID", "Patient Name", "Patient Birth Date"):
        assert stem in IDENTIFYING_IMAGING_COLUMNS
        assert f"New {stem}" in IDENTIFYING_IMAGING_COLUMNS
    assert "Downloaded Original Series Path" in IDENTIFYING_IMAGING_COLUMNS
