"""Does the model actually help? (checklist P4-4)

The honest way to keep an LLM in a pipeline is to be able to remove it. This module
measures both of its uses against a human-labelled answer key.

**Terminology ranking** compares three configurations:

1. deterministic lookup only -- vocabulary code match plus approved mappings;
2. plus lexical candidate recall -- did the right concept even make the shortlist?
3. plus model ranking -- did it put the right concept first?

**Column semantics** compares two, against the dataset YAML as the answer key. Someone
sat down and decided what every column means; that decision is exactly what the model
is being asked to reproduce, so it is the right thing to score against.

1. name matching -- a generic synonym table over the column name alone;
2. the model, given the column name plus a de-identified profile.

The metric that matters is **top-1 accuracy**, because that is what a reviewer's time is
actually spent on. Recall@k is reported for terminology too, since a recall step that
never surfaces the answer cannot be rescued by any ranker.

If the model does not beat the deterministic arm by enough to be worth the latency and
the failure modes, the design says to delete the step. This module exists to make that a
measurement rather than an argument.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.terminology import (DOMAIN_FOR_KIND, Candidate, MappingRegistry, Vocabulary,
                                 mappings_directory)

GOLD_FIELDS = ["code_system", "source_string", "event_kind", "concept_id", "concept_name", "note"]


@dataclass
class GoldItem:
    code_system: str
    source_string: str
    event_kind: str
    concept_id: int
    concept_name: str = ""


@dataclass
class ArmResult:
    """One configuration's performance over the gold set."""

    name: str
    n: int = 0
    top1: int = 0
    recall_at_5: int = 0
    recall_at_k: int = 0
    no_candidates: int = 0
    seconds: float = 0.0
    schema_failures: int = 0
    retries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm": self.name,
            "n": self.n,
            "top1_accuracy": round(self.top1 / self.n, 4) if self.n else 0.0,
            "recall_at_5": round(self.recall_at_5 / self.n, 4) if self.n else 0.0,
            "recall_at_k": round(self.recall_at_k / self.n, 4) if self.n else 0.0,
            "items_with_no_candidates": self.no_candidates,
            "seconds_per_item": round(self.seconds / self.n, 4) if self.n else 0.0,
            "schema_failures": self.schema_failures,
            "retried_calls": self.retries,
        }


def load_gold(path: Path) -> list[GoldItem]:
    """Read the human gold set. A few dozen items is enough to answer the question."""
    if not path.exists():
        raise FileNotFoundError(
            f"no gold set at {path}. Build one by exporting decided review items: "
            "columns are " + ",".join(GOLD_FIELDS)
        )
    items: list[GoldItem] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not (row.get("concept_id") or "").strip().isdigit():
                continue
            items.append(
                GoldItem(
                    code_system=row.get("code_system", "").strip(),
                    source_string=row.get("source_string", "").strip(),
                    event_kind=row.get("event_kind", "").strip(),
                    concept_id=int(row["concept_id"]),
                    concept_name=row.get("concept_name", "").strip(),
                )
            )
    return items


def gold_from_decisions(layout: WorkLayout, out_path: Path) -> int:
    """Export accepted review decisions as a gold set.

    The gold standard is what a human accepted -- not what the vocabulary happens to
    contain, and certainly not what the model proposed.
    """
    from ehr2cdm.review import read_decisions, read_pending

    pending = {row["id"]: row for row in read_pending(layout)}
    decisions = read_decisions(layout)
    rows = []
    for item_id, decision in sorted(decisions.items()):
        if decision.get("decision", "").strip().lower() != "accept":
            continue
        source = pending.get(item_id)
        if not source or not (decision.get("concept_id") or "").strip().isdigit():
            continue
        rows.append(
            {
                "code_system": source.get("code_system", ""),
                "source_string": source.get("source_string", ""),
                "event_kind": source.get("event_kind", ""),
                "concept_id": decision["concept_id"],
                "concept_name": decision.get("concept_name", ""),
                "note": decision.get("note", ""),
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=GOLD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def measure(
    cfg: DatasetConfig,
    layout: WorkLayout,
    gold_path: Path,
    vocabulary_dir: Path | None = None,
    candidate_limit: int = 10,
    use_llm: bool = True,
) -> dict[str, Any]:
    gold = load_gold(gold_path)
    if not gold:
        raise ValueError(f"{gold_path} has no usable rows")

    vocabulary = Vocabulary.open(vocabulary_dir)
    mappings = MappingRegistry.load(mappings_directory())

    deterministic = ArmResult("deterministic_lookup")
    lexical = ArmResult("plus_lexical_recall")
    ranked = ArmResult("plus_model_ranking")

    client = None
    if use_llm:
        from ehr2cdm.llm import LlmClient

        client = LlmClient.from_env()
        client.probe()

    for item in gold:
        domain = DOMAIN_FOR_KIND.get(item.event_kind)

        started = time.perf_counter()
        # Arm 1: what the pipeline does with no assistance at all.
        approved = mappings.get(item.code_system, item.source_string)
        match = approved or vocabulary.lookup_code(item.code_system, item.source_string)
        deterministic.n += 1
        deterministic.seconds += time.perf_counter() - started
        if match and int(match.concept_id) == item.concept_id:
            deterministic.top1 += 1
            deterministic.recall_at_5 += 1
            deterministic.recall_at_k += 1

        started = time.perf_counter()
        candidates: list[Candidate] = vocabulary.candidates(
            item.source_string, domain, limit=candidate_limit
        )
        lexical.n += 1
        lexical.seconds += time.perf_counter() - started
        ids = [c.concept_id for c in candidates]
        if not candidates:
            lexical.no_candidates += 1
        if ids[:1] == [item.concept_id]:
            lexical.top1 += 1
        if item.concept_id in ids[:5]:
            lexical.recall_at_5 += 1
        if item.concept_id in ids:
            lexical.recall_at_k += 1

        if client is None:
            continue
        started = time.perf_counter()
        ranking = client.rank_candidates(item.source_string, domain, candidates) if candidates else None
        ranked.n += 1
        ranked.seconds += time.perf_counter() - started
        if not candidates:
            ranked.no_candidates += 1
        if ranking is None:
            ranked.schema_failures += 1
            # A failed ranking falls back to lexical order, which is what the pipeline
            # would do; scoring it as a loss would overstate the cost of the failure.
            ordered = ids
        else:
            ordered = [r.concept_id for r in sorted(ranking.ranking, key=lambda r: r.rank)]
        if ordered[:1] == [item.concept_id]:
            ranked.top1 += 1
        if item.concept_id in ordered[:5]:
            ranked.recall_at_5 += 1
        if item.concept_id in ordered:
            ranked.recall_at_k += 1

    if client is not None:
        usage = client.usage_summary()
        ranked.retries = usage["retried"]

    arms = [deterministic.as_dict(), lexical.as_dict()]
    if client is not None:
        arms.append(ranked.as_dict())

    verdict = _verdict(lexical, ranked, client is not None)
    result = {
        "dataset": cfg.dataset_id,
        "gold_items": len(gold),
        "vocabulary": vocabulary.version,
        "candidate_limit": candidate_limit,
        "arms": arms,
        "llm": client.usage_summary() if client is not None else None,
        "verdict": verdict,
    }
    vocabulary.close()
    path = layout.runs_dir / "llm_benefit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _verdict(lexical: ArmResult, ranked: ArmResult, ran_llm: bool) -> str:
    """State plainly whether the ranking step earned its place."""
    if not ran_llm:
        return (
            "not measured: no model was available. Until this is run, the ranking step "
            "is unjustified and the pipeline should be used with --no-llm."
        )
    if not ranked.n:
        return "not measured: no items reached the ranking step"
    gain = (ranked.top1 / ranked.n) - (lexical.top1 / lexical.n if lexical.n else 0)
    ceiling = ranked.recall_at_k / ranked.n if ranked.n else 0
    if gain <= 0.0:
        return (
            f"the ranking step did not help (top-1 change {gain:+.1%}). The design says "
            "to delete it and keep lexical recall alone."
        )
    if gain < 0.05:
        return (
            f"the ranking step gained {gain:+.1%} top-1, which is within noise for a gold "
            "set this size. Enlarge the gold set before keeping it."
        )
    return (
        f"the ranking step gained {gain:+.1%} top-1 against a recall ceiling of "
        f"{ceiling:.1%}. Worth keeping while the ceiling holds."
    )


# --------------------------------------------------------------------------------
# column semantics (checklist P4-2 use 1, measured against the dataset config)
# --------------------------------------------------------------------------------

#: Generic clinical-English synonyms for the deterministic arm. Deliberately contains
#: nothing dataset-specific: the point of this baseline is that a name-matching
#: heuristic knows what "sex" means and cannot know what a particular hospital's
#: patient-key abbreviation means. Where the model earns its place, if it does, is
#: exactly there.
ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "person_id": ("patient", "patient id", "patient ref", "subject", "subject id", "person", "person id"),
    "encounter_id": ("encounter", "encounter id", "visit", "visit id", "visit ref", "contact"),
    "event_time": ("event time", "datetime", "date", "time", "performed", "collected", "collection time", "recorded"),
    "available_time": ("result time", "resulted", "released", "reported", "available"),
    "end_time": ("end time", "end date", "stop", "discharge", "discharged"),
    "anchor_time": ("anchor", "anchor time", "index date", "reference date"),
    "anchor_rank": ("rank", "closest", "nearest", "order"),
    "source_code": ("code", "concept code", "component", "analyte", "test code", "base name"),
    "source_name": ("name", "description", "label", "display", "test name"),
    "display_name": ("study", "study name", "procedure", "procedure name", "exam", "description"),
    "result_category": ("result type", "category", "kind", "type"),
    "value": ("value", "result", "result value", "measurement", "reading"),
    "unit": ("unit", "units", "uom"),
    "value_low": ("low", "lower", "range low", "minimum"),
    "value_high": ("high", "upper", "range high", "maximum"),
    "status": ("status", "state", "order status", "disposition"),
    "route": ("route", "administration route"),
    "dose": ("dose", "dosage", "amount", "quantity", "strength"),
    "text": ("text", "narrative", "note", "comment", "report text", "line text"),
    "text_line": ("line", "line number", "sequence", "line no"),
    "text_title": ("title", "heading", "subject line"),
    "age": ("age", "years old", "age years"),
    "birth_date": ("birth date", "date of birth", "dob", "born"),
    "gender": ("gender", "sex", "birth sex"),
    "race": ("race", "ancestry"),
    "ethnicity": ("ethnicity", "ethnic group", "ethnic"),
    "vital_status": ("vital status", "alive", "deceased flag", "living status"),
    "death_time": ("death date", "died", "deceased", "date of death", "death"),
    "visit_type": ("visit type", "encounter type", "visit class", "admission type"),
    "length_of_stay": ("length of stay", "stay days", "los", "days"),
    "duration_masked": ("time difference", "duration", "elapsed"),
    "sequence_number": ("sequence", "row number", "index", "seq"),
}


@dataclass
class ColumnItem:
    """One column whose role a human already decided, in the dataset YAML."""

    source_id: str
    column: str
    role: str
    profile: dict[str, Any]


def _normalize_column(name: str) -> str:
    return " ".join(name.replace("_", " ").replace("-", " ").lower().split())


def name_match_role(column: str) -> str:
    """The deterministic arm: what the column name alone says, and nothing else."""
    normalized = _normalize_column(column)
    best, best_score = "unknown", 0.0
    for role, synonyms in ROLE_SYNONYMS.items():
        for candidate in (_normalize_column(role), *synonyms):
            if normalized == candidate:
                score = 1.0
            elif normalized.startswith(candidate) or normalized.endswith(candidate):
                score = 0.8
            elif candidate in normalized:
                score = 0.6
            else:
                continue
            # Ties break toward the longer synonym: "result time" should beat "time".
            score += len(candidate) / 1000.0
            if score > best_score:
                best, best_score = role, score
    return best


def collect_column_items(cfg: DatasetConfig, layout: WorkLayout, limit_per_source: int = 0) -> list[ColumnItem]:
    """Every column the dataset YAML assigns a role, with a profile from the source layer.

    The answer key is the config, so this deliberately looks only at columns a human
    already decided about. Columns nobody has classified are what `propose` is for.
    """
    import polars as pl

    from ehr2cdm.canonical.normalize import COL_PREFIX
    from ehr2cdm.review import _profile

    items: list[ColumnItem] = []
    for part in cfg.partitions:
        for source_id, spec in cfg.sources_for(part.id).items():
            if any(item.source_id == source_id for item in items):
                continue
            files = sorted((layout.source_dir / part.id / source_id).glob("*.parquet"))
            if not files:
                continue
            frame = pl.read_parquet(files[0], n_rows=500)
            available = {c[len(COL_PREFIX):].strip().lower(): c for c in frame.columns if c.startswith(COL_PREFIX)}
            for role, field_spec in spec.fields.items():
                for alias in field_spec.from_:
                    column = available.get(alias.strip().lower())
                    if column is None:
                        continue
                    items.append(
                        ColumnItem(
                            source_id=source_id,
                            column=alias,
                            role=role,
                            profile=_profile(frame, column),
                        )
                    )
                    break
    return items


def measure_columns(
    cfg: DatasetConfig, layout: WorkLayout, use_llm: bool = True, limit: int = 0
) -> dict[str, Any]:
    """Score both arms of the column-semantics use against the dataset YAML."""
    items = collect_column_items(cfg, layout)
    if limit:
        items = items[:limit]
    if not items:
        raise ValueError("no assigned columns found; run ingest first")

    heuristic = ArmResult("column_name_matching")
    model = ArmResult("model_proposal")
    client = None
    if use_llm:
        from ehr2cdm.llm import LlmClient

        client = LlmClient.from_env()
        client.probe()

    details: list[dict[str, Any]] = []
    for item in items:
        started = time.perf_counter()
        guessed = name_match_role(item.column)
        heuristic.n += 1
        heuristic.seconds += time.perf_counter() - started
        heuristic_hit = guessed == item.role
        heuristic.top1 += int(heuristic_hit)

        proposed, confidence, rationale = None, None, ""
        model_hit = None
        if client is not None:
            started = time.perf_counter()
            proposal = client.propose_column_role(item.source_id, item.column, item.profile)
            model.n += 1
            model.seconds += time.perf_counter() - started
            if proposal is None:
                model.schema_failures += 1
            else:
                proposed = proposal.role.strip()
                confidence = proposal.confidence
                rationale = proposal.rationale
                model_hit = proposed == item.role
                model.top1 += int(model_hit)

        details.append(
            {
                "source": item.source_id,
                "column": item.column,
                "expected_role": item.role,
                "name_matching": guessed,
                "name_matching_correct": heuristic_hit,
                "model": proposed,
                "model_correct": model_hit,
                "model_confidence": confidence,
                "model_rationale": rationale,
            }
        )

    arms = [heuristic.as_dict()]
    if client is not None:
        arms.append(model.as_dict())
        model.retries = client.usage_summary()["retried"]

    result = {
        "dataset": cfg.dataset_id,
        "model": client.model if client is not None else None,
        "columns_scored": len(items),
        "answer_key": "the field roles declared in the dataset YAML",
        "asymmetry": (
            "the model sees the source name and a de-identified value profile; the "
            "heuristic sees only the column name. That is deliberate -- the extra "
            "context is the thing being paid for -- but it means the arms are not "
            "given identical inputs."
        ),
        "arms": arms,
        "llm": client.usage_summary() if client is not None else None,
        "verdict": _column_verdict(heuristic, model, client is not None),
        "details": details,
    }
    # The filename carries the model, so comparing two models is a matter of running
    # this twice rather than remembering which result was which.
    tag = (client.model if client is not None else "no-model").replace("/", "_")
    path = layout.runs_dir / f"llm_benefit_columns.{tag}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return result


def _column_verdict(heuristic: ArmResult, model: ArmResult, ran_llm: bool) -> str:
    if not ran_llm or not model.n:
        return (
            "not measured: no model was available. Column roles come from the dataset YAML "
            "either way; the question is only whether a model shortens writing the next one."
        )
    base = heuristic.top1 / heuristic.n if heuristic.n else 0.0
    with_model = model.top1 / model.n
    gain = with_model - base
    if model.schema_failures:
        note = f" ({model.schema_failures} replies failed schema validation and were not counted as hits)"
    else:
        note = ""
    if gain <= 0:
        return (
            f"name matching alone scores {base:.1%} and the model {with_model:.1%}{note}. "
            "The model is not earning its place on this dataset; onboarding by hand is "
            "cheaper than reviewing its proposals."
        )
    if gain < 0.10:
        return (
            f"the model gains {gain:+.1%} over name matching ({base:.1%} to {with_model:.1%}){note}. "
            "Real but small: worth it only if a human reviews every proposal anyway, which "
            "they do."
        )
    return (
        f"the model gains {gain:+.1%} over name matching ({base:.1%} to {with_model:.1%}){note}, "
        "which is where a name-matching heuristic cannot help: institution-specific "
        "abbreviations. Worth keeping for onboarding."
    )
