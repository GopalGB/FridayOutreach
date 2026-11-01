"""Pure captured-HTML preparation shared by the optional assist command."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Mapping
from urllib.parse import unquote

from .ai import AiCandidate, AiResult, AiSource, AiSuggestion, AssistInput
from .extract import _clean_soup, parse_contacts, preview_document
from .model import JsonObject, LeadRow, ObservedValue, PageDocument, Selection


_ROW_FIELDS = ("company", "website", "address")
_CONTACT_FIELDS = (("email", "emails"), ("phone", "phones"))
_EVIDENCE_ATTRIBUTES = ("aria-label", "content", "href", "title", "value")


@dataclass(frozen=True)
class AssistCapture:
    source: AiSource
    rows: tuple[LeadRow, ...]
    selection: Selection
    candidates: tuple[AiCandidate, ...]

    def to_json(self) -> JsonObject:
        return {
            "rows": [row.to_json() for row in self.rows],
            "selection": self.selection.to_json(),
            "candidateCount": len(self.candidates),
        }


def captured_assist(
    input_value: AssistInput,
    mapping: Mapping[str, str],
    selection: Selection | None,
) -> AssistCapture:
    """Extract deterministic rows and source-spanned review candidates from HTML."""

    html = input_value.html
    source = input_value.source
    document = PageDocument(
        source.url,
        html,
        source.observed_at,
        hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "completed",
        None,
    )
    rows, actual_selection = preview_document(document, mapping, selection)
    contacts = parse_contacts(document)
    values = _candidate_values(rows, contacts.emails, contacts.phones)
    candidates = _candidates(_visible_evidence(html), values)
    return AssistCapture(source, tuple(rows), actual_selection, candidates)


def reindex_suggestions(result: AiResult) -> AiResult:
    """Replace provider-specific identifiers with deterministic global cN IDs."""

    counter = 0

    def indexed(items: tuple[AiSuggestion, ...]) -> tuple[AiSuggestion, ...]:
        nonlocal counter
        rebuilt: list[AiSuggestion] = []
        for item in items:
            rebuilt.append(replace(item, candidate_id=f"c{counter}"))
            counter += 1
        return tuple(rebuilt)

    return replace(result, accepted=indexed(result.accepted), rejected=indexed(result.rejected))


def suggestion_candidates(result: AiResult, evidence_candidates: tuple[AiCandidate, ...]) -> tuple[AiCandidate, ...]:
    """Bind reindexed local suggestions back to captured evidence for Jev review."""

    bound: list[AiCandidate] = []
    suggestions = (*result.accepted, *result.rejected)
    for suggestion in suggestions:
        match = next(
            (
                candidate
                for candidate in evidence_candidates
                if candidate.field == suggestion.field
                and candidate.observation.raw == suggestion.observation.raw
                and candidate.observation.source_url == suggestion.observation.source_url
            ),
            None,
        )
        if match is None:
            raise ValueError("local suggestion has no captured evidence candidate")
        bound.append(AiCandidate(suggestion.candidate_id, suggestion.field, suggestion.observation, match.source_fragment))
    if not bound:
        raise ValueError("local extraction returned no review candidates")
    return tuple(bound)


def _candidate_values(
    rows: list[LeadRow], emails: tuple[ObservedValue, ...], phones: tuple[ObservedValue, ...]
) -> tuple[tuple[str, ObservedValue], ...]:
    values: list[tuple[str, ObservedValue]] = []
    for row in rows:
        for field in _ROW_FIELDS:
            observation = getattr(row, field)
            if isinstance(observation, ObservedValue):
                values.append((field, observation))
        for field, attribute in _CONTACT_FIELDS:
            for observation in getattr(row, attribute):
                values.append((field, observation))
    values.extend(("email", observation) for observation in emails)
    values.extend(("phone", observation) for observation in phones)
    return tuple(values)


def _candidates(evidence: tuple[tuple[str, str], ...], values: tuple[tuple[str, ObservedValue], ...]) -> tuple[AiCandidate, ...]:
    candidates: list[AiCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for field, observation in values:
        identity = (field, observation.raw, observation.source_url)
        if identity in seen:
            continue
        seen.add(identity)
        fragment = _source_fragment(evidence, observation.raw)
        if fragment is None:
            continue
        candidates.append(AiCandidate(f"c{len(candidates)}", field, observation, fragment))
    return tuple(candidates)


def _visible_evidence(html: str) -> tuple[tuple[str, str], ...]:
    """Return decoded visible text and allowlisted attributes with truthful labels."""

    evidence: list[tuple[str, str]] = []
    for tag in _clean_soup(html).find_all(True):
        name = tag.name if isinstance(tag.name, str) else "element"
        rendered = " ".join(tag.stripped_strings)
        if rendered:
            evidence.append((rendered, f"renderedText tag={name}: {rendered}"))
        for attribute in _EVIDENCE_ATTRIBUTES:
            value = tag.attrs.get(attribute)
            if not isinstance(value, str) or not value:
                continue
            decoded = unquote(value)
            context = rendered or "(no visible text)"
            evidence.append((decoded, f"attribute={attribute} tag={name}: {decoded}; renderedText: {context}"))
    return tuple(evidence)


def _source_fragment(evidence: tuple[tuple[str, str], ...], raw: str) -> str | None:
    for visible_value, fragment in evidence:
        if raw == visible_value:
            return fragment
    for visible_value, fragment in evidence:
        if raw in visible_value:
            return fragment
    return None
