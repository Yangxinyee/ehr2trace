"""Local model client (design section 8).

The model is asked two questions and is allowed to answer only in a schema:

1. what does this column appear to mean?
2. of these candidate concepts, which best matches this string, and why?

It never parses a date, never invents a concept id, and never writes to any target
layer. Its output lands in ``review/pending.csv`` and a human decides. Removing this
module entirely must leave a working converter -- that is the test of whether the
boundary is real.

Operational constraints, all of them deliberate:

* the endpoint is whatever ``LLM_BASE_URL`` says, so business code depends on no
  vendor. Capability is *probed* once, not inferred from a model name;
* temperature 0 and a pinned sampling configuration, because a proposal that changes
  between runs cannot be reviewed;
* two retries on invalid JSON, then the item goes to review unproposed;
* **data minimization**: column names, aggregate statistics and a few short sample
  values that pass a de-identification filter. Logs record hashes, template versions
  and token counts -- never patient text.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field, ValidationError

from ehr2cdm.hashing import sha256_hex
from ehr2cdm.terminology import Candidate

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
MAX_RETRIES = 2

#: Patterns that must never leave the machine inside a sample value. Conservative on
#: purpose: a sample that trips any of these is dropped rather than redacted, because
#: a partial redaction still leaks the shape of an identifier.
_IDENTIFIER_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # national id shaped
    re.compile(r"\b[A-Z]{2}\d{6,}\b"),              # record-number shaped
    re.compile(r"\b\d{7,}\b"),                      # long bare numbers
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),           # dates
    re.compile(r"@"),                                # addresses
)


class ColumnProposal(BaseModel):
    """What the model thinks a column is. A suggestion, never a configuration."""

    role: str = Field(description="suggested canonical field role, or 'unknown'")
    logical_type: str = Field(default="unknown")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="")


class RankedCandidate(BaseModel):
    concept_id: int
    rank: int
    rationale: str = ""


class CandidateRanking(BaseModel):
    """A ranking over candidates that were *given* to the model.

    Validation rejects any concept id that was not in the supplied list, which is the
    mechanical guarantee that the model cannot introduce one from memory.
    """

    ranking: list[RankedCandidate]
    rationale: str = ""

    def as_payload(self) -> list[dict[str, Any]]:
        return [
            {"concept_id": c.concept_id, "rank": c.rank, "rationale": c.rationale}
            for c in sorted(self.ranking, key=lambda c: c.rank)
        ]


@dataclass
class LlmCall:
    """What is safe to keep about a call: hashes and counts, never content."""

    template: str
    template_sha256: str
    input_sha256: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 1
    ok: bool = True
    error: str | None = None


@dataclass
class LlmClient:
    base_url: str
    model: str
    api_key: str = "not-needed"
    timeout: float = 120.0
    temperature: float = 0.0
    seed: int = 0
    supports_json_schema: bool | None = None
    calls: list[LlmCall] = field(default_factory=list)
    #: raw text of the most recent reply, so a retry can hand the model back what it
    #: actually said rather than only telling it that something was wrong
    _last_content: str = ""

    @classmethod
    def from_env(cls) -> "LlmClient":
        base_url = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
        model = os.environ.get("LLM_MODEL", "")
        if not model:
            raise RuntimeError("LLM_MODEL is not set; the proposal steps need a local model")
        if os.environ.get("OFFLINE_MODE", "1") == "1" and not _is_local(base_url):
            raise RuntimeError(
                f"OFFLINE_MODE=1 but LLM_BASE_URL={base_url} is not local; patient-derived "
                "text must not leave this machine"
            )
        return cls(base_url=base_url, model=model, api_key=os.environ.get("LLM_API_KEY", "not-needed"))

    # -- transport ------------------------------------------------------------

    def probe(self) -> bool:
        """Ask the endpoint whether it can constrain output to a JSON schema.

        Probed rather than assumed: model names say nothing reliable about which
        sampling features a given server build actually exposes.
        """
        if self.supports_json_schema is not None:
            return self.supports_json_schema
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        try:
            reply = self._post(
                [{"role": "user", "content": "Reply with {\"ok\": true}."}], schema, "probe"
            )
            self.supports_json_schema = isinstance(reply, dict) and "ok" in reply
        except Exception:
            self.supports_json_schema = False
        return bool(self.supports_json_schema)

    def _post(
        self, messages: list[dict[str, str]], schema: dict[str, Any], name: str, max_tokens: int = 512
    ) -> Any:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "seed": self.seed,
            # Generous enough for a schema-shaped answer with a one-sentence rationale,
            # and small enough that a model inclined to ramble is cut off rather than
            # spending a minute per call doing it.
            "max_tokens": max_tokens,
        }
        if self.supports_json_schema is not False:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        self._last_usage = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        self._last_content = content
        return json.loads(content)

    def _ask(
        self,
        template: str,
        prompt: str,
        schema: dict[str, Any],
        model_cls: type[BaseModel],
        max_tokens: int = 512,
    ):
        """One question, validated against a schema, with a bounded retry budget."""
        template_text, template_hash = load_template(template)
        messages = [
            {"role": "system", "content": template_text},
            {"role": "user", "content": prompt},
        ]
        record = LlmCall(
            template=template, template_sha256=template_hash, input_sha256=sha256_hex(prompt)
        )
        self._last_usage = (None, None)
        self._last_content = ""
        for attempt in range(1, MAX_RETRIES + 2):
            record.attempts = attempt
            try:
                raw = self._post(messages, schema, template, max_tokens=max_tokens)
                parsed = model_cls.model_validate(raw)
                record.prompt_tokens, record.completion_tokens = self._last_usage
                self.calls.append(record)
                return parsed
            except (ValidationError, ValueError, KeyError) as exc:
                record.error = type(exc).__name__
                if attempt > MAX_RETRIES:
                    break
                # The model's own reply goes back as an assistant turn before the
                # correction. Appending only the correction leaves two user turns in a
                # row, which several chat templates -- Gemma's among them -- reject
                # outright with "Conversation roles must alternate". That made every
                # retry a hard failure instead of a retry, and it went unnoticed because
                # the first task measured never needed one: the recorded metrics for
                # those runs read `retried: 0`. The first task that did need retries
                # lost 265 of 300 calls to it.
                messages.append({"role": "assistant", "content": self._last_content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "The previous reply did not match the required schema. "
                        "Reply with valid JSON only.",
                    }
                )
            except Exception as exc:  # transport failure: give up, do not loop
                record.error = type(exc).__name__
                break
        record.ok = False
        self.calls.append(record)
        return None

    # -- the two uses ---------------------------------------------------------

    def propose_column_role(self, source_id: str, column: str, profile: dict[str, Any]) -> ColumnProposal | None:
        """Suggest what a column means, from its name and a de-identified profile."""
        payload = {
            "source": source_id,
            "column": column,
            "non_null": profile.get("non_null"),
            "distinct": profile.get("distinct"),
            "samples": [s for s in (profile.get("samples") or []) if is_safe_sample(str(s))],
        }
        schema = {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "logical_type": {"type": "string"},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["role", "logical_type", "confidence", "rationale"],
            "additionalProperties": False,
        }
        # A role and one sentence; there is nothing here worth a long answer.
        return self._ask(
            "column_semantics", json.dumps(payload, sort_keys=True), schema, ColumnProposal, max_tokens=192
        )

    def rank_candidates(
        self, source_string: str, domain: str | None, candidates: Sequence[Candidate]
    ) -> CandidateRanking | None:
        """Rank supplied candidates. Any id not supplied is rejected on validation."""
        allowed = {c.concept_id for c in candidates}
        payload = {
            "source_string": source_string,
            "domain": domain or "",
            "candidates": [
                {"concept_id": c.concept_id, "concept_name": c.concept_name, "vocabulary": c.vocabulary_id}
                for c in candidates
            ],
        }
        schema = {
            "type": "object",
            "properties": {
                "ranking": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "concept_id": {"type": "integer"},
                            "rank": {"type": "integer"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["concept_id", "rank", "rationale"],
                        "additionalProperties": False,
                    },
                },
                "rationale": {"type": "string"},
            },
            "required": ["ranking", "rationale"],
            "additionalProperties": False,
        }
        result = self._ask(
            "terminology_ranking", json.dumps(payload, sort_keys=True), schema, CandidateRanking, max_tokens=768
        )
        if result is None:
            return None
        invented = [r.concept_id for r in result.ranking if r.concept_id not in allowed]
        if invented:
            # The model produced an id nobody offered it. That is exactly the failure
            # this boundary exists to catch, so the whole ranking is discarded.
            self.calls[-1].ok = False
            self.calls[-1].error = "concept_id_not_in_candidates"
            return None
        return result

    def usage_summary(self) -> dict[str, Any]:
        return {
            "calls": len(self.calls),
            "failed": sum(1 for c in self.calls if not c.ok),
            "retried": sum(1 for c in self.calls if c.attempts > 1),
            "prompt_tokens": sum(c.prompt_tokens or 0 for c in self.calls),
            "completion_tokens": sum(c.completion_tokens or 0 for c in self.calls),
            "templates": sorted({f"{c.template}@{c.template_sha256[:8]}" for c in self.calls}),
        }


def load_template(name: str) -> tuple[str, str]:
    """Load a prompt template and its hash. Templates are git-tracked, not generated."""
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    return text, sha256_hex(text)


def is_safe_sample(value: str) -> bool:
    """Whether a sample value may be sent. Anything identifier-shaped is dropped."""
    if len(value) > 48:
        return False
    return not any(p.search(value) for p in _IDENTIFIER_PATTERNS)


def _is_local(url: str) -> bool:
    return any(host in url for host in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))
