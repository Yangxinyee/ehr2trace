"""Dataset configuration: one YAML per dataset (design section 4).

Everything dataset-specific lives here -- partition directories, file globs, sheet
aliases, column aliases, anchor semantics, label definitions. The core package must
stay readable without knowing which hospital produced the data.

Two properties are enforced rather than documented:

* unknown keys raise (``extra="forbid"``) -- a typo in a config is a bug, not a silent
  default;
* adapter, shape and field-role names are checked against :mod:`ehr2cdm.registry`, so a
  YAML can never introduce executable behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ehr2cdm.errors import BlockerError, ConfigError
from ehr2cdm.registry import FIELD_ROLES, adapter_names, shape_names

Strict = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class FieldSpec(BaseModel):
    """Where one canonical field role comes from, as an ordered alias list."""

    model_config = Strict

    from_: list[str] = Field(alias="from")
    required: bool = False

    @field_validator("from_")
    @classmethod
    def _nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("'from' must list at least one source column alias")
        return v


class UntimedValueSpec(BaseModel):
    """A measurement-like column that carries no time of its own (design section 6.2)."""

    model_config = Strict

    column: str
    code: str
    unit: str | None = None


class RowFilterSpec(BaseModel):
    """Keep only the rows of a source whose column holds one of ``keep`` (design 4.3).

    Some source tables are a mixed bag that a `code_system` and an `event_kind` cannot
    describe honestly. MIMIC-IV's provider order entry is one table holding medication
    orders, laboratory orders, radiology orders and consults; declaring the whole of it
    as ``drug_order`` makes 55% of its rows assert something untrue, and a filter is the
    only thing that lets one logical source be a subset of one physical table.

    Deliberately exact matching on a named column rather than an expression language: a
    config that can compute is a config nobody can review.
    """

    model_config = Strict

    column: str
    #: values to keep, compared case-insensitively after stripping
    keep: list[str] = Field(default_factory=list)

    @field_validator("keep")
    @classmethod
    def _nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("'keep' must list at least one value; omit row_filter to keep every row")
        return v


class EventKindFromSpec(BaseModel):
    """The event kind is a property of the row, not of the whole table.

    Provider order entry is one table holding medication orders, laboratory orders,
    radiology orders and consults. A single ``event_kind`` makes most of its rows assert
    something untrue, and a ``row_filter`` that keeps only medications answers that by
    discarding 28.9 million recorded decisions -- which are exactly the actions an agent
    would have to learn.

    ``default`` is the point of this spec rather than a convenience: an order type
    nobody mapped becomes a generic request instead of vanishing, so a new value in the
    source shows up as an unmapped kind rather than as a number that quietly dropped.
    """

    model_config = Strict

    column: str
    #: source value -> event kind, compared case-insensitively after stripping
    map: dict[str, str] = Field(default_factory=dict)
    #: the kind for any value the map does not name
    default: str

    @field_validator("map")
    @classmethod
    def _nonempty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("'map' must name at least one value; use 'event_kind' for a fixed kind")
        return v


class CodeSplitSpec(BaseModel):
    """One cell holding several codes, and the separator that divides them.

    A problem-list column that reads ``R78.81, B95.7, Z16.29`` records three diagnoses.
    Matching the whole string finds nothing, and there is no way to know from the string
    alone whether a comma is a separator or part of a code -- so it is declared, per
    source, rather than guessed.
    """

    model_config = Strict

    separator: str = ","
    #: the field roles whose value is split; each piece becomes its own event
    roles: list[str] = Field(default_factory=lambda: ["source_code"])

    @field_validator("roles")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(FIELD_ROLES)
        if unknown:
            raise ValueError(f"unknown field roles {sorted(unknown)}")
        return v


class AdapterOptions(BaseModel):
    model_config = Strict

    delimiter: str = "\t"
    encoding: str = "utf-8-sig"
    quoting: Literal["none", "minimal"] = "none"
    header_row: int = 1
    strip_bom: bool = True
    skip_unnamed_leading_columns: bool = False
    sheet_aliases: list[str] = Field(default_factory=list)
    file_glob: str | None = None


class SourceVariant(BaseModel):
    """One physical form of a logical source (used by the ``any_of`` adapter)."""

    model_config = Strict

    adapter: str
    file_glob: str | None = None
    sheet_aliases: list[str] = Field(default_factory=list)
    options: AdapterOptions = Field(default_factory=AdapterOptions)

    @field_validator("adapter")
    @classmethod
    def _known_adapter(cls, v: str) -> str:
        if v not in adapter_names():
            raise ValueError(f"unknown adapter {v!r}; registered: {sorted(adapter_names())}")
        if v == "any_of":
            raise ValueError("'any_of' cannot be nested inside a variant")
        return v


class SourceSpec(BaseModel):
    """One logical source: how to find it, how to read it, what events it emits."""

    model_config = Strict

    adapter: str
    file_glob: str | None = None
    sheet_aliases: list[str] = Field(default_factory=list)
    options: AdapterOptions = Field(default_factory=AdapterOptions)
    variants: list[SourceVariant] = Field(default_factory=list)

    shape: str
    event_kind: str | None = None
    code_system: str = "SOURCE"
    #: what a result column is expected to hold. ``numeric`` makes a value that matches
    #: none of the documented forms a quarantine case instead of free text.
    value_expect: Literal["auto", "numeric", "text"] = "auto"
    required: bool = True
    #: restrict this source to a subset of partitions; empty means all
    partitions: list[str] = Field(default_factory=list)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    untimed_values: list[UntimedValueSpec] = Field(default_factory=list)
    #: emit an additional study-level event grouping the rows of one report
    study_event_kind: str | None = None
    study_code_from: str | None = None
    #: for shapes that group rows, the roles forming the group key
    group_by: list[str] = Field(default_factory=list)
    #: extra columns kept verbatim in the source layer and carried as attributes
    keep_columns: list[str] = Field(default_factory=list)
    #: restrict this source to a subset of its physical table's rows
    row_filter: "RowFilterSpec | None" = None
    #: the kind varies by row: one table holding several kinds of action
    event_kind_from: "EventKindFromSpec | None" = None
    #: which values of the ``status`` role mean the drug actually reached the patient.
    #:
    #: Stated affirmatively on purpose. Listing the refusals instead would put the risk
    #: on the wrong side: a status nobody enumerated would become a drug exposure, which
    #: is exactly the claim -- a treatment that did not happen -- that the rest of this
    #: converter exists to avoid making. Unlisted means not evidence, so a value added
    #: by a future release is conservative rather than silently affirmative.
    administered_when: list[str] = Field(default_factory=list)
    #: a cell holding several codes, and how to divide it
    code_split: "CodeSplitSpec | None" = None
    notes: str | None = None

    @model_validator(mode="after")
    def _one_way_to_declare_a_kind(self) -> "SourceSpec":
        if self.event_kind is not None and self.event_kind_from is not None:
            raise ValueError(
                "declare either 'event_kind' or 'event_kind_from', not both: with both "
                "present the fixed one is silently ignored"
            )
        return self

    @field_validator("adapter")
    @classmethod
    def _known_adapter(cls, v: str) -> str:
        if v not in adapter_names():
            raise ValueError(f"unknown adapter {v!r}; registered: {sorted(adapter_names())}")
        return v

    @field_validator("shape")
    @classmethod
    def _known_shape(cls, v: str) -> str:
        if v not in shape_names():
            raise ValueError(f"unknown shape {v!r}; registered: {sorted(shape_names())}")
        return v

    @field_validator("fields")
    @classmethod
    def _known_roles(cls, v: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        unknown = set(v) - set(FIELD_ROLES)
        if unknown:
            raise ValueError(f"unknown field roles {sorted(unknown)}; allowed: {sorted(FIELD_ROLES)}")
        return v

    @model_validator(mode="after")
    def _variants_only_for_any_of(self) -> "SourceSpec":
        if self.adapter == "any_of" and not self.variants:
            raise ValueError("adapter 'any_of' requires at least one variant")
        if self.adapter != "any_of" and self.variants:
            raise ValueError("'variants' is only valid with adapter 'any_of'")
        return self

    def alias_map(self) -> dict[str, list[str]]:
        return {role: list(spec.from_) for role, spec in self.fields.items()}


class PartitionSpec(BaseModel):
    model_config = Strict

    id: str
    dir: str
    #: provenance only. Never a clinical fact, never a model input for its own label.
    membership_label: str | None = None
    batch: str | None = None


class IdentitySpec(BaseModel):
    model_config = Strict

    person_key: str
    encounter_key: str | None = None
    #: env var holding an optional secret salt for patient key -> subject_id
    subject_salt_env: str = "EHR_SUBJECT_SALT"


class TimeSpec(BaseModel):
    model_config = Strict

    #: IANA name. ``None`` is a blocker: never fall back to the machine's zone.
    timezone_assumption: str | None = None
    #: what to do when no timezone is declared
    naive_time_policy: Literal["block", "store_naive_flagged"] = "block"
    #: accepted timestamp layouts, tried in order; anything else is quarantined
    formats: list[str] = Field(
        default_factory=lambda: [
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]
    )
    #: cell strings that mean "absent"
    null_literals: list[str] = Field(default_factory=lambda: ["NULL"])


class AnchorSpec(BaseModel):
    """Extraction-time anchors: provenance, never an event time."""

    model_config = Strict

    anchor_type: str
    from_field: str = "anchor_time"
    granularity: Literal["date", "datetime"] = "date"
    dedup_key: list[str] = Field(default_factory=lambda: ["anchor_date", "partition_id"])
    rank_field: str | None = "anchor_rank"


class LabelSpec(BaseModel):
    model_config = Strict

    id: str
    scope: Literal["episode", "patient", "unknown"] = "unknown"
    from_: str = Field(alias="from")
    requires_anchor: bool = True
    #: 'undefined' means the data owner has not supplied the rule -> blocker
    definition_status: Literal["undefined", "defined"] = "undefined"
    definition_note: str | None = None


class PersonBirthPolicy(BaseModel):
    model_config = Strict

    mode: Literal["strict", "approved_approximation"] = "strict"
    age_as_of_date: str | None = None
    approval_note: str | None = None

    @model_validator(mode="after")
    def _approval_required(self) -> "PersonBirthPolicy":
        if self.mode == "approved_approximation" and not (self.age_as_of_date and self.approval_note):
            raise ValueError(
                "person_birth_policy.mode='approved_approximation' requires both "
                "age_as_of_date and approval_note (a human approval record)"
            )
        return self


class OmopSpec(BaseModel):
    model_config = Strict

    backend: Literal["duckdb", "postgres"] = "duckdb"
    cdm_version: str = "5.4"
    person_birth_policy: PersonBirthPolicy = Field(default_factory=PersonBirthPolicy)
    #: how OBSERVATION_PERIOD is derived, recorded on every row
    observation_period_rule: str = "first_to_last_trustworthy_event_date/v1"
    source_name: str | None = None
    #: CDM_SOURCE requires both; neither is derivable from the data, so both are
    #: declared by whoever publishes rather than invented at build time.
    cdm_holder: str | None = None
    source_release_date: str | None = None


class TerminologySpec(BaseModel):
    """What this export's own strings mean, where the vocabulary cannot say.

    The package knows English drug-name convention -- that `TABLET` is an oral tablet
    and `MG/ML` is a concentration -- because that is true of every US EHR. It must not
    know which three letters this hospital system abbreviates its sites to, or what it
    calls its order-entry system; those strings differ at the next site, and a
    medication name carrying one fails to resolve until it is removed. So they are
    declared here rather than accumulating in the core, which is the same rule that
    keeps column names out of it.
    """

    model_config = Strict

    #: Site-local words to strip from a medication name before matching it: ward and
    #: system abbreviations, order-set names, workflow markers. Matched whole-word and
    #: case-insensitively.
    drug_name_noise: list[str] = Field(default_factory=list)


class MedsSpec(BaseModel):
    model_config = Strict

    splits: list[tuple[str, float]] = Field(
        default_factory=lambda: [("train", 0.8), ("tuning", 0.1), ("held_out", 0.1)]
    )
    split_salt: str = "meds-split/v1"
    shard_size: int = 1
    #: never write cohort provenance into event rows (leakage)
    include_membership_label: bool = False


class OwnerAnswers(BaseModel):
    """Questions only the data owner can answer (design section 12 / checklist P0-6).

    Every field defaults to ``None``, and ``None`` means "unanswered" -- which surfaces
    as a blocker in ``inspect`` rather than as a hidden assumption somewhere downstream.
    """

    model_config = Strict

    #: how the extraction batches relate (independent extracts? a re-extraction?)
    batch_relationship: str | None = None
    #: whether the imaging reports behind the anchors are part of this delivery
    imaging_reports_included: bool | None = None
    #: what exactly is and is not delivered where imaging is concerned -- a bare False
    #: does not distinguish "no images" from "images exist but live somewhere else"
    imaging_note: str | None = None
    #: provenance and version of the terminology download
    vocabulary_source: str | None = None
    #: free-form record of who answered what, kept in git next to the config
    answered_by: str | None = None
    answered_on: str | None = None


class ExecutionSpec(BaseModel):
    model_config = Strict

    bucket_count: int = 64
    workers: int = 4
    #: rows per parquet row group when streaming large delimited files
    chunk_rows: int = 200_000


class ReferenceRange(BaseModel):
    model_config = Strict

    low: float | None = None
    high: float | None = None
    unit: str | None = None


class DatasetConfig(BaseModel):
    """The whole dataset contract."""

    model_config = Strict

    dataset_id: str
    root_env: str = "EHR_DATA_ROOT"
    description: str | None = None

    identity: IdentitySpec
    time: TimeSpec = Field(default_factory=TimeSpec)
    partitions: list[PartitionSpec]
    sources: dict[str, SourceSpec]
    anchors: AnchorSpec | None = None
    labels: list[LabelSpec] = Field(default_factory=list)
    omop: OmopSpec = Field(default_factory=OmopSpec)
    meds: MedsSpec = Field(default_factory=MedsSpec)
    terminology: TerminologySpec = Field(default_factory=TerminologySpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    owner_answers: OwnerAnswers = Field(default_factory=OwnerAnswers)
    reference_ranges: dict[str, ReferenceRange] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_checks(self) -> "DatasetConfig":
        ids = [p.id for p in self.partitions]
        if len(ids) != len(set(ids)):
            raise ValueError("partition ids must be unique")
        known = set(ids)
        for name, src in self.sources.items():
            unknown = set(src.partitions) - known
            if unknown:
                raise ValueError(f"source {name!r} restricted to unknown partitions {sorted(unknown)}")
        return self

    # -- derived ---------------------------------------------------------------

    def partition(self, partition_id: str) -> PartitionSpec:
        for p in self.partitions:
            if p.id == partition_id:
                return p
        raise ConfigError(f"unknown partition {partition_id!r}")

    def sources_for(self, partition_id: str) -> dict[str, SourceSpec]:
        return {
            name: src
            for name, src in self.sources.items()
            if not src.partitions or partition_id in src.partitions
        }

    def data_root(self) -> Path:
        raw = os.environ.get(self.root_env)
        if not raw:
            raise ConfigError(
                f"environment variable {self.root_env} is not set; it must point at the "
                "read-only raw export"
            )
        root = Path(raw).expanduser()
        if not root.is_dir():
            raise ConfigError(f"{self.root_env}={root} is not a directory")
        return root

    def subject_salt(self) -> str:
        return os.environ.get(self.identity.subject_salt_env, "")

    #: Settings that say *how* to run, never *what* to produce. They are excluded from
    #: the content address because merging is order-independent by construction, so no
    #: combination of them can change a produced byte -- and including them meant that
    #: lowering the worker count to survive an out-of-memory kill invalidated a
    #: fifty-three-minute ingest whose output would have been identical either way.
    #:
    #: ``bucket_count`` is the subtle one. It cannot change the merged canonical layer,
    #: but it does decide which subject lands in which bucket file, so it must still
    #: enter the per-task digests -- otherwise a resumed run would happily reuse a
    #: bucket computed under a different partitioning. See ``StageTask.digest``.
    OPERATIONAL_FIELDS: ClassVar[tuple[str, ...]] = ("execution",)

    def canonical_json(self) -> str:
        """Config content in a stable textual form; the basis of ``config_hash``."""
        payload = self.model_dump(mode="json", by_alias=True)
        for field_name in self.OPERATIONAL_FIELDS:
            payload.pop(field_name, None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def timezone_or_blocker(self) -> str:
        """The declared source timezone, or a blocker. Never the developer's zone."""
        if self.time.timezone_assumption:
            return self.time.timezone_assumption
        raise BlockerError(
            "TIMEZONE_UNDECLARED",
            "the source timezone is not declared in the dataset config; timestamps "
            "cannot be converted to UTC. Set time.timezone_assumption, or pass "
            "--assume-timezone to record an explicit operator assumption",
        )


def load_dataset_config(path: str | Path) -> DatasetConfig:
    """Load and validate ``datasets/<id>.yaml``.

    ``yaml.safe_load`` is deliberate: a config must not be able to construct Python
    objects or import anything.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"dataset config not found: {p}")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # Includes an attempt to use a Python-object tag: safe_load refuses, and the
        # refusal is reported as a config error rather than crashing the CLI.
        raise ConfigError(f"cannot parse dataset config {p}:\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"dataset config must be a mapping: {p}")
    try:
        return DatasetConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> a readable config error
        raise ConfigError(f"invalid dataset config {p}:\n{exc}") from exc


def find_dataset_config(dataset: str, search: list[Path] | None = None) -> Path:
    """Resolve ``--dataset ctpe`` to a YAML path."""
    candidates: list[Path] = []
    p = Path(dataset)
    if p.suffix in {".yaml", ".yml"}:
        candidates.append(p)
    else:
        roots = search or [Path.cwd() / "datasets", Path(__file__).resolve().parents[2] / "datasets"]
        for root in roots:
            candidates.append(root / f"{dataset}.yaml")
            candidates.append(root / f"{dataset}.yml")
    for c in candidates:
        if c.exists():
            return c
    raise ConfigError(f"no dataset config for {dataset!r}; looked at: {[str(c) for c in candidates]}")
