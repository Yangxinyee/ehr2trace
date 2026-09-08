"""Discovery, input manifest and the blocker list (checklist P0-5, P0-6).

``inspect`` answers three questions before a single row is converted:

1. what is physically there (files, sizes, hashes, sheets, encodings, BOMs);
2. how it lines up with the declared logical sources (present / empty / not extracted);
3. what cannot be decided from the data alone.

The third one is the point of the command. A converter that quietly picks a timezone,
or invents a reference date for an age, produces numbers nobody can defend. So those
questions are listed, by id, with who has to answer them.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ehr2trace.adapters import get_adapter, has_bom, list_sheets, sniff_line_ending
from ehr2trace.config import DatasetConfig, SourceSpec
from ehr2trace.hashing import file_sha256
from ehr2trace.schema import Coverage
from ehr2trace.version import CODE_VERSION

TEXT_SUFFIXES = {".txt", ".tsv", ".csv"}
WORKBOOK_SUFFIXES = {".xlsx", ".xlsm"}
#: Columnar inputs. Reported as their own kind rather than as text: they carry a schema,
#: so the byte-order-mark and line-ending questions asked of text files do not apply.
COLUMNAR_SUFFIXES = {".parquet"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


@dataclass
class FileRecord:
    partition_id: str
    relative_path: str
    absolute_path: str
    size_bytes: int
    sha256: str | None
    kind: str
    sheets: list[str] = field(default_factory=list)
    bom: bool | None = None
    line_ending: str | None = None


@dataclass
class UnitRecord:
    file: str
    sheet: str | None
    adapter: str
    columns: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourceRecord:
    partition_id: str
    source_id: str
    adapter: str
    required: bool
    coverage: str
    units: list[UnitRecord] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass
class Blocker:
    id: str
    question: str
    needed_from: str
    blocks: str
    status: str = "open"


@dataclass
class InspectReport:
    dataset_id: str
    code_version: str
    config_hash: str
    data_root: str
    files: list[FileRecord]
    sources: list[SourceRecord]
    blockers: list[Blocker]
    counts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "code_version": self.code_version,
            "config_hash": self.config_hash,
            "data_root": self.data_root,
            "counts": self.counts,
            "files": [asdict(f) for f in self.files],
            "sources": [asdict(s) for s in self.sources],
            "blockers": [asdict(b) for b in self.blockers],
        }


# --------------------------------------------------------------------------------
# physical layout
# --------------------------------------------------------------------------------


def partition_dir(cfg: DatasetConfig, partition_id: str) -> Path:
    return cfg.data_root() / cfg.partition(partition_id).dir


def _hash_one(path_str: str) -> tuple[str, str]:
    return path_str, file_sha256(path_str)


def scan_files(cfg: DatasetConfig, compute_hashes: bool = True, workers: int = 8) -> list[FileRecord]:
    """Every input file under every declared partition, with size and content hash."""
    records: list[FileRecord] = []
    for part in cfg.partitions:
        pdir = partition_dir(cfg, part.id)
        if not pdir.is_dir():
            continue
        for path in sorted(pdir.rglob("*")):
            if not path.is_file() or path.name in IGNORED_NAMES or path.name.startswith("~$"):
                continue
            suffix = path.suffix.lower()
            if suffix in TEXT_SUFFIXES:
                kind, sheets = "text", []
                bom, ending = has_bom(path), sniff_line_ending(path)
            elif suffix in WORKBOOK_SUFFIXES:
                kind, sheets = "workbook", list_sheets(path)
                bom, ending = None, None
            elif suffix in COLUMNAR_SUFFIXES:
                kind, sheets = "columnar", []
                bom, ending = None, None
            else:
                continue
            records.append(
                FileRecord(
                    partition_id=part.id,
                    relative_path=str(path.relative_to(cfg.data_root())),
                    absolute_path=str(path),
                    size_bytes=path.stat().st_size,
                    sha256=None,
                    kind=kind,
                    sheets=sheets,
                    bom=bom,
                    line_ending=ending,
                )
            )
    if compute_hashes and records:
        with ProcessPoolExecutor(max_workers=max(1, workers)) as ex:
            for path_str, digest in ex.map(_hash_one, [r.absolute_path for r in records]):
                for r in records:
                    if r.absolute_path == path_str:
                        r.sha256 = digest
    return records


def resolve_source_units(cfg: DatasetConfig, partition_id: str, source_id: str, spec: SourceSpec):
    """Physical units backing one logical source in one partition.

    ``any_of`` tries its variants in declared order and keeps the first that resolves,
    which is how one logical source can be a standalone file in one partition and a
    sheet in another without the rest of the pipeline noticing.
    """
    pdir = partition_dir(cfg, partition_id)
    if not pdir.is_dir():
        return []
    if spec.adapter == "any_of":
        for variant in spec.variants:
            options = variant.options.model_copy(
                update={
                    "file_glob": variant.file_glob or variant.options.file_glob,
                    "sheet_aliases": variant.sheet_aliases or variant.options.sheet_aliases,
                }
            )
            units = get_adapter(variant.adapter).discover(pdir, options)
            if units:
                return units
        return []
    options = spec.options.model_copy(
        update={
            "file_glob": spec.file_glob or spec.options.file_glob,
            "sheet_aliases": spec.sheet_aliases or spec.options.sheet_aliases,
            "skip_unnamed_leading_columns": spec.options.skip_unnamed_leading_columns,
        }
    )
    return get_adapter(spec.adapter).discover(pdir, options)


def peek_columns(unit) -> UnitRecord:
    """Column names of a unit, reading as little as possible."""
    stream = get_adapter(unit.adapter).open(unit)
    try:
        return UnitRecord(
            file=str(unit.path),
            sheet=unit.sheet,
            adapter=unit.adapter,
            columns=list(stream.columns),
            warnings=list(stream.warnings),
        )
    finally:
        close = getattr(stream, "close", None)
        if hasattr(close, "close"):
            try:
                close.close()
            except Exception:
                pass


def scan_sources(cfg: DatasetConfig) -> list[SourceRecord]:
    """Line every declared logical source up against what is physically present."""
    out: list[SourceRecord] = []
    for part in cfg.partitions:
        for source_id, spec in cfg.sources_for(part.id).items():
            units = resolve_source_units(cfg, part.id, source_id, spec)
            unit_records = [peek_columns(u) for u in units]
            if not unit_records:
                # Absent is "not extracted", not "every column is missing": an optional
                # source that this partition simply did not ship is coverage
                # information, and only a *required* one is a blocker.
                coverage = Coverage.not_extracted
                missing: list[str] = []
            else:
                coverage = Coverage.present
                available = {c.lower() for rec in unit_records for c in rec.columns}
                missing = [
                    role
                    for role, fs in spec.fields.items()
                    if fs.required and not any(a.lower() in available for a in fs.from_)
                ]
            out.append(
                SourceRecord(
                    partition_id=part.id,
                    source_id=source_id,
                    adapter=spec.adapter,
                    required=spec.required,
                    coverage=str(coverage),
                    units=unit_records,
                    missing_roles=missing,
                )
            )
    return out


# --------------------------------------------------------------------------------
# blockers
# --------------------------------------------------------------------------------


def collect_blockers(cfg: DatasetConfig, sources: list[SourceRecord]) -> list[Blocker]:
    blockers: list[Blocker] = []

    if not cfg.time.timezone_assumption:
        blockers.append(
            Blocker(
                "TIMEZONE_UNDECLARED",
                "Which timezone were the source timestamps recorded in? Timestamps "
                "cannot be converted to UTC without it, and the developer machine's "
                "zone is not an acceptable substitute.",
                "data owner",
                "canonical, omop, meds",
            )
        )

    policy = cfg.omop.person_birth_policy
    # An age reference date is only needed when the birth year has to be reconstructed
    # from an age. A dataset that carries a real birth date needs no such question, and
    # asking it anyway turns the best case -- strict mode, exact birth years -- into a
    # blocker. MIMIC-IV supplies one; the reference export does not.
    has_birth_date = any("birth_date" in src.fields for src in cfg.sources.values())
    if policy.mode == "strict" and not policy.age_as_of_date and not has_birth_date:
        blockers.append(
            Blocker(
                "AGE_REFERENCE_DATE_MISSING",
                "As of which date was each patient's age recorded? Without it "
                "PERSON.year_of_birth cannot be filled, so OMOP publication is blocked "
                "for affected patients. Death date, first event date and the run date "
                "are all invalid substitutes.",
                "data owner",
                "omop.PERSON",
            )
        )

    for label in cfg.labels:
        if label.definition_status == "undefined":
            blockers.append(
                Blocker(
                    f"LABEL_DEFINITION_UNDEFINED:{label.id}",
                    f"What rule, and at what decision time, defines the {label.id!r} "
                    "label? Until this is answered the label stays provenance and can "
                    "become neither a clinical fact nor a training target.",
                    "data owner",
                    "labels, task layer",
                )
            )
        if label.requires_anchor and label.scope == "episode":
            blockers.append(
                Blocker(
                    f"LABEL_EPISODE_BINDING:{label.id}",
                    f"Which specific episode does each {label.id!r} membership row bind "
                    "to when a patient has several anchors, including anchors in "
                    "differently-labelled partitions?",
                    "data owner",
                    "labels, task layer",
                )
            )

    answers = cfg.owner_answers
    if not answers.batch_relationship:
        blockers.append(
            Blocker(
                "BATCH_RELATIONSHIP_UNKNOWN",
                "How do the extraction batches relate? Independent extracts are merged "
                "by lineage; a re-extraction that supersedes an earlier one would need "
                "a different rule.",
                "data owner",
                "canonical dedup semantics",
            )
        )
    if answers.imaging_reports_included is None:
        blockers.append(
            Blocker(
                "IMAGING_REPORTS_UNCONFIRMED",
                "Are the imaging reports and images behind the anchor dates part of "
                "this delivery? Nothing in the current extract contains them, and "
                "imaging conclusions must never be synthesized from an anchor date or "
                "a directory name.",
                "data owner",
                "note/procedure coverage",
            )
        )
    if not answers.vocabulary_source:
        blockers.append(
            Blocker(
                "VOCABULARY_SOURCE_MISSING",
                "Which terminology download (source and version) may this project use? "
                "Without it every concept id stays 0 and every term goes to review.",
                "data owner",
                "omop terminology mapping",
            )
        )

    for rec in sources:
        if rec.required and rec.coverage == str(Coverage.not_extracted):
            blockers.append(
                Blocker(
                    f"REQUIRED_SOURCE_MISSING:{rec.partition_id}/{rec.source_id}",
                    f"Required source {rec.source_id!r} was not found in partition "
                    f"{rec.partition_id!r}.",
                    "data engineer",
                    "ingest",
                )
            )
        if rec.missing_roles:
            blockers.append(
                Blocker(
                    f"REQUIRED_COLUMN_MISSING:{rec.partition_id}/{rec.source_id}",
                    f"Required field roles {rec.missing_roles} have no matching column "
                    f"alias in {rec.partition_id}/{rec.source_id}.",
                    "data engineer",
                    "ingest",
                )
            )
    return blockers


def inspect(cfg: DatasetConfig, compute_hashes: bool = True, workers: int = 8) -> InspectReport:
    files = scan_files(cfg, compute_hashes=compute_hashes, workers=workers)
    sources = scan_sources(cfg)
    blockers = collect_blockers(cfg, sources)

    logical_units = sum(len(s.units) for s in sources)
    text_units = sum(1 for s in sources for u in s.units if u.sheet is None)
    sheet_units = logical_units - text_units
    counts = {
        "physical_files": len(files),
        "text_files": sum(1 for f in files if f.kind == "text"),
        "workbooks": sum(1 for f in files if f.kind == "workbook"),
        "columnar_files": sum(1 for f in files if f.kind == "columnar"),
        "logical_sources": logical_units,
        "logical_sources_text": text_units,
        "logical_sources_sheets": sheet_units,
        "declared_sources": len(cfg.sources),
        "partitions": len(cfg.partitions),
        "total_bytes": sum(f.size_bytes for f in files),
        "bytes_per_partition": {
            p.id: sum(f.size_bytes for f in files if f.partition_id == p.id) for p in cfg.partitions
        },
        "sources_not_extracted": sum(1 for s in sources if s.coverage == str(Coverage.not_extracted)),
        "open_blockers": len(blockers),
    }
    return InspectReport(
        dataset_id=cfg.dataset_id,
        code_version=CODE_VERSION,
        config_hash=cfg.config_hash(),
        data_root=str(cfg.data_root()),
        files=files,
        sources=sources,
        blockers=blockers,
        counts=counts,
    )
