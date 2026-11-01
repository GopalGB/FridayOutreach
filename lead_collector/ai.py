"""Typed, review-only values shared by optional AI adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Mapping, TypeAlias

from .model import JsonObject, ObservedValue


AiMode: TypeAlias = Literal["off", "scrapegraph", "jev", "hybrid"]
AiSourceKind: TypeAlias = Literal["anonymous-http", "offline", "browser", "synthetic"]
AI_FIELDS = frozenset({"company", "website", "address", "email", "phone"})


def validate_ai_mode(value: str) -> AiMode:
    """Return an explicit supported mode; never select a provider implicitly."""

    if value == "off":
        return "off"
    if value == "scrapegraph":
        return "scrapegraph"
    if value == "jev":
        return "jev"
    if value == "hybrid":
        return "hybrid"
    raise ValueError("invalid AI mode")


@dataclass(frozen=True)
class AiSource:
    url: str
    observed_at: str
    kind: AiSourceKind


@dataclass(frozen=True)
class AssistInput:
    source: AiSource
    html: str
    mode: str
    model: str | None
    cancelled: Callable[[], bool] | None = None


@dataclass(frozen=True)
class AiCandidate:
    identifier: str
    field: str
    observation: ObservedValue
    source_fragment: str


@dataclass(frozen=True)
class AiSuggestion:
    candidate_id: str
    field: str
    observation: ObservedValue
    selected: bool
    choice_confidence: float | None
    probabilities: Mapping[str, float] | None
    support_probability: float | None

    def to_json(self) -> JsonObject:
        return {
            "candidateId": self.candidate_id,
            "field": self.field,
            "observation": self.observation.to_json(),
            "selected": self.selected,
            "choiceConfidence": self.choice_confidence,
            "probabilities": None if self.probabilities is None else dict(self.probabilities),
            "supportProbability": self.support_probability,
        }


@dataclass(frozen=True)
class AiUsage:
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: str

    def to_json(self) -> JsonObject:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "estimatedCostUsd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class AiError:
    code: str
    message: str

    def to_json(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class AiResult:
    status: str
    accepted: tuple[AiSuggestion, ...]
    rejected: tuple[AiSuggestion, ...]
    model: str | None
    usage: AiUsage | None
    error: AiError | None
    contract_only: bool = False

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "accepted": [item.to_json() for item in self.accepted],
            "rejected": [item.to_json() for item in self.rejected],
            "model": self.model,
            "usage": None if self.usage is None else self.usage.to_json(),
            "error": None if self.error is None else self.error.to_json(),
            "contractOnly": self.contract_only,
        }
