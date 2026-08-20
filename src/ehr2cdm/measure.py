"""Does the model actually help? (checklist P4-4)

The honest way to keep an LLM in a pipeline is to be able to remove it. This module
compares three configurations against the same human-labelled gold set:

1. deterministic lookup only -- vocabulary code match plus approved mappings;
2. plus lexical candidate recall -- did the right concept even make the shortlist?
3. plus model ranking -- did it put the right concept first?

The metric that matters is **top-1 accuracy against what a human accepted**, because
that is what a reviewer's time is actually spent on. Recall@k is reported too, since a
recall step that never surfaces the answer cannot be rescued by any ranker.

If configuration 3 does not beat configuration 2 by enough to be worth the latency and
the failure modes, the design says to delete the ranking step. This module exists to
make that a measurement rather than an argument.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ehr2cdm.config import DatasetConfig
from ehr2cdm.paths import WorkLayout
from ehr2cdm.terminology import (
    DOMAIN_FOR_KIND,
    Candidate,
    MappingRegistry,
    Vocabulary,
    normalize_term,
)

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
    mappings = MappingRegistry.load(Path.cwd() / "mappings")

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
