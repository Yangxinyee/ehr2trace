"""Execution model and run reports (design section 3.3).

The entire parallelism model is a process pool over a task list computed by a pure
function, with content-addressed outputs and a sorted merge. There is no queue, no
lease, no heartbeat and no ledger, because with one machine and this much data none of
them buy anything -- and each would be another thing that can be wrong.

``--workers 1`` runs the same code in-process. It is the deterministic baseline every
parallel run is checked against.
"""

from __future__ import annotations

import json
import os
import platform
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TypeVar

from ehr2cdm.paths import WorkLayout
from ehr2cdm.version import CANONICAL_SCHEMA_VERSION, CODE_VERSION, HASH_RULE_VERSION

T = TypeVar("T")
R = TypeVar("R")


def execute(tasks: Sequence[T], fn: Callable[[T], R], workers: int = 1) -> list[R]:
    """Run tasks and return results in *task* order, never completion order.

    Returning in task order is not cosmetic: everything downstream merges these
    results, and a merge that depends on which worker finished first is a merge that
    changes its answer when the machine is busy.
    """
    if not tasks:
        return []
    if workers <= 1:
        return [fn(t) for t in tasks]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, tasks))


@dataclass
class StageReport:
    stage: str
    started_at: str
    finished_at: str
    counts: dict[str, Any] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunReport:
    """One run's evidence: what was read, what was produced, what went wrong."""

    run_id: str
    dataset_id: str
    config_hash: str
    code_version: str = CODE_VERSION
    hash_rule_version: str = HASH_RULE_VERSION
    canonical_schema_version: str = CANONICAL_SCHEMA_VERSION
    started_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None
    workers: int = 1
    environment: dict[str, Any] = field(default_factory=dict)
    inputs: list[dict[str, Any]] = field(default_factory=list)
    stages: list[StageReport] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    quality_summary: dict[str, Any] = field(default_factory=dict)

    def stage(self, name: str, counts: dict[str, Any], details: Iterable[Any] = ()) -> None:
        now = _now()
        self.stages.append(
            StageReport(
                stage=name,
                started_at=now,
                finished_at=now,
                counts=counts,
                details=[_as_dict(d) for d in details],
            )
        )

    def assumption(self, key: str, value: Any, why: str) -> None:
        """Record something the run had to assume. Assumptions are never silent."""
        self.assumptions.append({"key": key, "value": value, "why": why})

    def write(self, layout: WorkLayout) -> Path:
        self.finished_at = _now()
        self.environment = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        }
        path = layout.runs_dir / self.run_id / "report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.partial")
        tmp.write_text(json.dumps(_as_dict(self), indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(path)
        return path


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_dict(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _as_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _as_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_as_dict(v) for v in obj]
    return obj
