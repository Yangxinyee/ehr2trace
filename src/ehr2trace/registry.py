"""Name registries.

The dataset YAML may name only things registered here: adapters, row shapes and field
roles. It can never name a Python path or import anything -- that is what keeps a
config file data rather than code.
"""

from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

ADAPTERS: dict[str, Callable[..., object]] = {}
SHAPES: dict[str, Callable[..., object]] = {}

#: Field roles a source may map its columns onto. Deliberately generic: no role name
#: may hint at any particular EHR export.
FIELD_ROLES: frozenset[str] = frozenset(
    {
        # identity
        "person_id",
        "encounter_id",
        # time
        "event_time",
        "available_time",
        "end_time",
        "anchor_time",
        "anchor_rank",
        # what happened
        "source_code",
        "source_name",
        "display_name",
        "result_category",
        # values
        "value",
        "unit",
        "value_low",
        "value_high",
        # medication / order attributes
        "status",
        "route",
        "dose",
        # text
        "text",
        "text_line",
        "text_title",
        # person attributes
        "age",
        "birth_date",
        "gender",
        "race",
        "ethnicity",
        "vital_status",
        "death_time",
        # visit attributes
        "visit_type",
        "length_of_stay",
        "duration_masked",
        # action lifecycle: this row's own identifier, and the action that caused it
        "action_key",
        "caused_by",
        # bookkeeping
        "sequence_number",
    }
)


def register_adapter(name: str) -> Callable[[T], T]:
    def deco(obj: T) -> T:
        ADAPTERS[name] = obj  # type: ignore[assignment]
        return obj

    return deco


def register_shape(name: str) -> Callable[[T], T]:
    def deco(obj: T) -> T:
        SHAPES[name] = obj  # type: ignore[assignment]
        return obj

    return deco


def load_registries() -> None:
    """Import the modules that populate the registries.

    Imported for their side effect -- the decorators run at import time -- and kept
    lazy so that config validation can check names without a circular import.
    """
    import ehr2trace.adapters  # noqa: F401  (registers the adapters)
    import ehr2trace.canonical.normalize  # noqa: F401  (registers the shapes)


def adapter_names() -> set[str]:
    load_registries()
    return set(ADAPTERS)


def shape_names() -> set[str]:
    load_registries()
    return set(SHAPES)
