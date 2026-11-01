"""Immutable public data contracts for offline lead collection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Iterable, Mapping, TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
OutcomeStatus: TypeAlias = str
RowIdentity: TypeAlias = tuple[str | None, str | None, str | None]
RECIPE_LIMIT_CAPS: Mapping[str, int] = {
    "maxInputs": 100,
    "maxPagesPerHost": 5,
    "maxRows": 1000,
    "maxPages": 100,
}
OUTCOME_STATUSES = frozenset({"completed", "empty", "denied", "blocked", "malformed", "timeout", "truncated", "unavailable", "failed"})
PROGRESS_KEYS = frozenset({"attemptedPages", "completedInputs", "totalInputs"})
JOB_STATES = frozenset({"idle", "running", "pausing", "paused", "stopped", "interrupted", "completed", "failed"})
CHECKPOINT_MODES = frozenset({"collect", "contacts"})
ROW_STATUSES = frozenset({"pending", "completed", "empty", "failed", "skipped"})
SINGULAR_FIELDS = frozenset({"company", "website", "address"})


def json_array(values: Iterable[JsonValue]) -> list[JsonValue]:
    return list(values)


def json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON value")
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return json_object(value)
    raise ValueError("invalid JSON value")


def json_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    parsed: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("JSON object keys must be strings")
        parsed[key] = json_value(item)
    return parsed


def _required(value: Mapping[str, JsonValue], key: str) -> JsonValue:
    if key not in value:
        raise ValueError(f"missing {key}")
    return value[key]


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    return value


def _integer(value: JsonValue, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid {label}")
    return value


def _string_tuple(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {label}")
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"invalid {label}")
        parsed.append(item)
    return tuple(parsed)


def _sample_rows(value: JsonValue) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError("invalid sample rows")
    parsed: list[tuple[str, ...]] = []
    for row in value:
        parsed.append(_string_tuple(row, "sample rows"))
    return tuple(parsed)


def _string_map(value: JsonValue, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}")
    parsed: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ValueError(f"invalid {label}")
        parsed[key] = item
    return parsed


def _integer_map(value: JsonValue, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}")
    parsed: dict[str, int] = {}
    for key, item in value.items():
        parsed[key] = _integer(item, label)
    return parsed


def _bounded_limits(value: JsonValue, label: str) -> dict[str, int]:
    parsed = _integer_map(value, label)
    if set(parsed) != set(RECIPE_LIMIT_CAPS):
        raise ValueError(f"invalid {label}")
    if any(item <= 0 or item > RECIPE_LIMIT_CAPS[key] for key, item in parsed.items()):
        raise ValueError(f"invalid {label}")
    return parsed


def _progress(value: JsonValue) -> dict[str, int]:
    parsed = _integer_map(value, "progress")
    if set(parsed) != PROGRESS_KEYS or any(item < 0 for item in parsed.values()):
        raise ValueError("invalid progress")
    return parsed


def _recipe_mapping(value: JsonValue) -> dict[str, str]:
    parsed = _string_map(value, "recipe mapping")
    if any(item not in SINGULAR_FIELDS for item in parsed.values()):
        raise ValueError("invalid recipe mapping")
    return parsed


def _pagination(value: JsonValue) -> JsonObject:
    parsed = _json_object(value, "pagination")
    if set(parsed) != {"mode", "selector"}:
        raise ValueError("invalid pagination")
    mode = parsed["mode"]
    selector = parsed["selector"]
    if mode not in {"none", "next", "loadMore", "scroll"}:
        raise ValueError("invalid pagination")
    if mode == "none" and selector is not None:
        raise ValueError("invalid pagination")
    if mode in {"next", "loadMore"} and (not isinstance(selector, str) or not selector):
        raise ValueError("invalid pagination")
    if mode == "scroll" and selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("invalid pagination")
    return parsed


def _json_object(value: JsonValue, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}")
    return value


def _json_object_list(value: JsonValue, label: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {label}")
    return tuple(_json_object(item, label) for item in value)


def _snapshot_rows(value: JsonValue) -> tuple[JsonObject, ...]:
    rows = _json_object_list(value, "snapshot")
    for row in rows:
        status = row.get("status")
        if status is not None and (not isinstance(status, str) or status not in ROW_STATUSES):
            raise ValueError("invalid snapshot row")
    return rows


def canonical(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observed_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ObservedValue:
    raw: str
    normalized: str
    source_url: str
    observed_at: str

    def to_json(self) -> JsonObject:
        return {"raw": self.raw, "normalized": self.normalized, "sourceUrl": self.source_url, "observedAt": self.observed_at}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "ObservedValue":
        if set(value) != {"raw", "normalized", "sourceUrl", "observedAt"}:
            raise ValueError("invalid observed value")
        return cls(
            _string(_required(value, "raw"), "observed value"),
            _string(_required(value, "normalized"), "observed value"),
            _string(_required(value, "sourceUrl"), "observed value"),
            _string(_required(value, "observedAt"), "observed value"),
        )


def _optional_observed(value: JsonValue, label: str) -> ObservedValue | None:
    return None if value is None else ObservedValue.from_json(_json_object(value, label))


def _observed_values(value: JsonValue, label: str) -> tuple[ObservedValue, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {label}")
    return tuple(ObservedValue.from_json(_json_object(item, label)) for item in value)


def _observed_map(value: JsonValue, label: str) -> dict[str, tuple[ObservedValue, ...]]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}")
    return {key: _observed_values(item, label) for key, item in value.items()}


@dataclass(frozen=True)
class LeadRow:
    id: str
    company: ObservedValue | None = None
    website: ObservedValue | None = None
    address: ObservedValue | None = None
    emails: tuple[ObservedValue, ...] = ()
    phones: tuple[ObservedValue, ...] = ()
    socials: tuple[ObservedValue, ...] = ()
    generic: Mapping[str, tuple[ObservedValue, ...]] = field(default_factory=dict)
    edits: Mapping[str, str] = field(default_factory=dict)
    excluded: bool = False
    status: str = "pending"
    ai_suggestions: Mapping[str, tuple[ObservedValue, ...]] = field(default_factory=dict)
    evidence: Mapping[str, tuple[ObservedValue, ...]] = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        def value(item: ObservedValue | None) -> JsonValue:
            return None if item is None else item.to_json()

        def evidence(items: tuple[ObservedValue, ...]) -> list[JsonValue]:
            return [item.to_json() for item in items]

        return {
            "id": self.id,
            "company": value(self.company),
            "website": value(self.website),
            "address": value(self.address),
            "emails": evidence(self.emails),
            "phones": evidence(self.phones),
            "socials": evidence(self.socials),
            "generic": {key: evidence(items) for key, items in self.generic.items()},
            "edits": dict(self.edits),
            "excluded": self.excluded,
            "status": self.status,
            "aiSuggestions": {key: evidence(items) for key, items in self.ai_suggestions.items()},
            "evidence": {key: evidence(items) for key, items in _singular_evidence(self).items()},
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "LeadRow":
        allowed = {"id", "company", "website", "address", "emails", "phones", "socials", "generic", "edits", "excluded", "status", "aiSuggestions", "evidence"}
        if not set(value).issubset(allowed) or not (allowed - {"evidence"}).issubset(value):
            raise ValueError("invalid lead row")
        identifier = _string(_required(value, "id"), "lead row")
        if not identifier:
            raise ValueError("invalid lead row")
        company = _optional_observed(_required(value, "company"), "lead row")
        website = _optional_observed(_required(value, "website"), "lead row")
        address = _optional_observed(_required(value, "address"), "lead row")
        excluded = _required(value, "excluded")
        status = _string(_required(value, "status"), "lead row")
        if not isinstance(excluded, bool) or status not in ROW_STATUSES:
            raise ValueError("invalid lead row")
        evidence = _observed_map(value.get("evidence", {}), "evidence")
        if not set(evidence).issubset(SINGULAR_FIELDS):
            raise ValueError("invalid evidence")
        row = cls(
            id=identifier,
            company=company,
            website=website,
            address=address,
            emails=_observed_values(_required(value, "emails"), "lead row"),
            phones=_observed_values(_required(value, "phones"), "lead row"),
            socials=_observed_values(_required(value, "socials"), "lead row"),
            generic=_observed_map(_required(value, "generic"), "lead row"),
            edits=_string_map(_required(value, "edits"), "lead row"),
            excluded=excluded,
            status=status,
            ai_suggestions=_observed_map(_required(value, "aiSuggestions"), "lead row"),
            evidence=evidence,
        )
        return replace(row, evidence=_seed_evidence(evidence, row))


@dataclass(frozen=True)
class ListObservation:
    source_url: str
    observed_at: str
    fields: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Selection:
    selector: str | None
    fingerprint: str
    columns: tuple[str, ...]
    sample_rows: tuple[tuple[str, ...], ...]
    column_selectors: Mapping[str, str] = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        return {"selector": self.selector, "fingerprint": self.fingerprint, "columns": list(self.columns), "sampleRows": [list(row) for row in self.sample_rows], "columnSelectors": dict(self.column_selectors)}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "Selection":
        selector, fingerprint = value.get("selector"), value.get("fingerprint", "")
        columns, samples, selectors = value.get("columns", []), value.get("sampleRows", []), value.get("columnSelectors", {})
        if selector is not None and not isinstance(selector, str):
            raise ValueError("invalid selector")
        return cls(
            selector,
            _string(fingerprint, "selection"),
            _string_tuple(columns, "selection"),
            _sample_rows(samples),
            _string_map(selectors, "column selectors"),
        )


@dataclass(frozen=True)
class RecipeV1:
    start_url: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    selection: Selection | None
    mapping: Mapping[str, str]
    pagination: Mapping[str, JsonValue]
    limits: Mapping[str, int]
    version: int = 1

    def to_json(self) -> JsonObject:
        return {"version": self.version, "startUrl": self.start_url, "allowedHosts": list(self.allowed_hosts), "allowedPaths": list(self.allowed_paths), "selection": None if self.selection is None else self.selection.to_json(), "mapping": dict(self.mapping), "pagination": dict(self.pagination), "limits": dict(self.limits)}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "RecipeV1":
        selection_value = _required(value, "selection")
        selection = None if selection_value is None else Selection.from_json(_json_object(selection_value, "selection"))
        return cls(
            start_url=_string(_required(value, "startUrl"), "recipe"),
            allowed_hosts=_string_tuple(_required(value, "allowedHosts"), "recipe"),
            allowed_paths=_string_tuple(_required(value, "allowedPaths"), "recipe"),
            selection=selection,
            mapping=_recipe_mapping(_required(value, "mapping")),
            pagination=_pagination(_required(value, "pagination")),
            limits=_bounded_limits(_required(value, "limits"), "recipe limits"),
            version=_recipe_version(_required(value, "version")),
        )


def _fetch_error(value: JsonValue) -> JsonObject | None:
    if value is None:
        return None
    parsed = _json_object(value, "fetch error")
    if set(parsed) != {"code", "message", "url"}:
        raise ValueError("invalid fetch error")
    code = _string(parsed["code"], "fetch error")
    message = _string(parsed["message"], "fetch error")
    url = parsed["url"]
    if not code or not message or (url is not None and not isinstance(url, str)):
        raise ValueError("invalid fetch error")
    return {"code": code, "message": message, "url": url}


@dataclass(frozen=True)
class FetchRequest:
    url: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    limits: Mapping[str, int]

    def to_json(self) -> JsonObject:
        return {
            "url": self.url,
            "allowedHosts": list(self.allowed_hosts),
            "allowedPaths": list(self.allowed_paths),
            "limits": dict(self.limits),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "FetchRequest":
        if set(value) != {"url", "allowedHosts", "allowedPaths", "limits"}:
            raise ValueError("invalid fetch request")
        url = _string(_required(value, "url"), "fetch request")
        hosts = _string_tuple(_required(value, "allowedHosts"), "fetch request")
        paths = _string_tuple(_required(value, "allowedPaths"), "fetch request")
        limits = _bounded_limits(_required(value, "limits"), "fetch limits")
        if not url or not hosts or not paths:
            raise ValueError("invalid fetch request")
        return cls(url, hosts, paths, limits)


@dataclass(frozen=True)
class PageDocument:
    url: str
    html: str
    observed_at: str
    sha256: str
    status: OutcomeStatus
    error: JsonObject | None

    def to_json(self) -> JsonObject:
        return {
            "url": self.url,
            "html": self.html,
            "observedAt": self.observed_at,
            "sha256": self.sha256,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "PageDocument":
        if set(value) != {"url", "html", "observedAt", "sha256", "status", "error"}:
            raise ValueError("invalid page document")
        url = _string(_required(value, "url"), "page document")
        html = _string(_required(value, "html"), "page document")
        observed_at = _string(_required(value, "observedAt"), "page document")
        sha256 = _string(_required(value, "sha256"), "page document")
        status = _string(_required(value, "status"), "page document")
        error = _fetch_error(_required(value, "error"))
        if (status in {"completed", "empty"} and not url) or not observed_at or len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256) or status not in OUTCOME_STATUSES:
            raise ValueError("invalid page document")
        if status in {"completed", "empty"} and error is not None:
            raise ValueError("invalid page document")
        if status not in {"completed", "empty"} and error is None:
            raise ValueError("invalid page document")
        return cls(url, html, observed_at, sha256, status, error)


@dataclass(frozen=True)
class ContactResult:
    status: OutcomeStatus
    emails: tuple[ObservedValue, ...]
    phones: tuple[ObservedValue, ...]
    socials: tuple[ObservedValue, ...]
    candidates: tuple[str, ...]
    error: JsonObject | None

    def to_json(self) -> JsonObject:
        return {
            "status": self.status,
            "emails": [item.to_json() for item in self.emails],
            "phones": [item.to_json() for item in self.phones],
            "socials": [item.to_json() for item in self.socials],
            "candidates": list(self.candidates),
            "error": self.error,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "ContactResult":
        if set(value) != {"status", "emails", "phones", "socials", "candidates", "error"}:
            raise ValueError("invalid contact result")
        status = _string(_required(value, "status"), "contact result")
        candidates = _string_tuple(_required(value, "candidates"), "contact result")
        error = _fetch_error(_required(value, "error"))
        if status not in OUTCOME_STATUSES or (status in {"completed", "empty"}) != (error is None):
            raise ValueError("invalid contact result")
        return cls(
            status,
            _observed_values(_required(value, "emails"), "contact result"),
            _observed_values(_required(value, "phones"), "contact result"),
            _observed_values(_required(value, "socials"), "contact result"),
            candidates,
            error,
        )


@dataclass(frozen=True)
class JobCheckpoint:
    mode: str
    input_urls: tuple[str, ...]
    source_rows: Mapping[str, tuple[str, ...]]
    pending_urls: tuple[str, ...]
    attempted_by_host: Mapping[str, int]
    seen_page_hashes: tuple[str, ...]
    cursors: tuple[str, ...]
    renderer: Mapping[str, JsonValue] | None = None
    unchanged_scrolls: int = 0

    def to_json(self) -> JsonObject:
        _validate_checkpoint(self)
        return {
            "mode": self.mode,
            "inputUrls": list(self.input_urls),
            "sourceRows": {owner: list(urls) for owner, urls in self.source_rows.items()},
            "pendingUrls": list(self.pending_urls),
            "attemptedByHost": dict(self.attempted_by_host),
            "seenPageHashes": list(self.seen_page_hashes),
            "cursors": list(self.cursors),
            "renderer": None if self.renderer is None else dict(self.renderer),
            "unchangedScrolls": self.unchanged_scrolls,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "JobCheckpoint":
        required = {"mode", "inputUrls", "sourceRows", "pendingUrls", "attemptedByHost", "seenPageHashes", "cursors"}
        keys = set(value)
        optional = {"renderer", "unchangedScrolls"}
        if not required.issubset(keys) or keys - required - optional:
            raise ValueError("invalid job checkpoint")
        source_rows_value = _json_object(_required(value, "sourceRows"), "job checkpoint")
        checkpoint = cls(
            _string(_required(value, "mode"), "job checkpoint"),
            _string_tuple(_required(value, "inputUrls"), "job checkpoint"),
            {owner: _string_tuple(urls, "job checkpoint") for owner, urls in source_rows_value.items()},
            _string_tuple(_required(value, "pendingUrls"), "job checkpoint"),
            _integer_map(_required(value, "attemptedByHost"), "job checkpoint"),
            _string_tuple(_required(value, "seenPageHashes"), "job checkpoint"),
            _string_tuple(_required(value, "cursors"), "job checkpoint"),
            _renderer_config(value.get("renderer")),
            _nonnegative_integer(value.get("unchangedScrolls", 0), "job checkpoint"),
        )
        _validate_checkpoint(checkpoint)
        return checkpoint


def _validate_checkpoint(checkpoint: JobCheckpoint) -> None:
    if checkpoint.mode not in CHECKPOINT_MODES or not checkpoint.input_urls or any(not url for url in checkpoint.input_urls):
        raise ValueError("invalid job checkpoint")
    if checkpoint.mode == "contacts" and len(checkpoint.input_urls) > RECIPE_LIMIT_CAPS["maxInputs"]:
        raise ValueError("invalid job checkpoint")
    if any(not owner or not urls or any(not url for url in urls) for owner, urls in checkpoint.source_rows.items()):
        raise ValueError("invalid job checkpoint")
    if any(not url for url in checkpoint.pending_urls) or any(not host or count < 0 for host, count in checkpoint.attempted_by_host.items()):
        raise ValueError("invalid job checkpoint")
    if any(len(page_hash) != 64 or any(char not in "0123456789abcdef" for char in page_hash) for page_hash in checkpoint.seen_page_hashes):
        raise ValueError("invalid job checkpoint")
    if any(not cursor for cursor in checkpoint.cursors):
        raise ValueError("invalid job checkpoint")
    if checkpoint.renderer is not None and checkpoint.mode != "collect":
        raise ValueError("invalid job checkpoint")
    if checkpoint.unchanged_scrolls < 0:
        raise ValueError("invalid job checkpoint")


def _renderer_config(value: JsonValue | None) -> JsonObject | None:
    if value is None:
        return None
    parsed = _json_object(value, "renderer")
    if set(parsed) != {"kind", "taskSpaceId", "pageLabel"}:
        raise ValueError("invalid renderer")
    task_space = parsed["taskSpaceId"]
    page_label = parsed["pageLabel"]
    if parsed["kind"] != "ego" or not isinstance(task_space, int) or isinstance(task_space, bool) or task_space <= 0 or not isinstance(page_label, str) or not page_label.strip():
        raise ValueError("invalid renderer")
    return parsed


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    revision: int = 0
    generation: int = 0
    state: str = "idle"
    progress: Mapping[str, int] = field(default_factory=lambda: {"attemptedPages": 0, "completedInputs": 0, "totalInputs": 1})
    limits: Mapping[str, int] = field(default_factory=lambda: {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100})
    rows: tuple[JsonObject, ...] = ()
    errors: tuple[JsonObject, ...] = ()
    recipe: RecipeV1 | None = None
    checkpoint: JobCheckpoint | None = None

    @classmethod
    def new(cls, job_id: str) -> "JobSnapshot":
        return cls(job_id)

    def with_state(self, state: str) -> "JobSnapshot":
        return replace(self, state=state)

    def to_json(self) -> JsonObject:
        return {"id": self.id, "revision": self.revision, "generation": self.generation, "state": self.state, "progress": dict(self.progress), "limits": dict(self.limits), "rows": list(self.rows), "errors": list(self.errors), "recipe": None if self.recipe is None else self.recipe.to_json(), "checkpoint": None if self.checkpoint is None else self.checkpoint.to_json()}

    @classmethod
    def from_json(cls, value: Mapping[str, JsonValue]) -> "JobSnapshot":
        recipe_value = _required(value, "recipe")
        recipe = None if recipe_value is None else RecipeV1.from_json(_json_object(recipe_value, "recipe"))
        checkpoint_value = value.get("checkpoint")
        checkpoint = None if checkpoint_value is None else JobCheckpoint.from_json(_json_object(checkpoint_value, "checkpoint"))
        state = _string(_required(value, "state"), "snapshot")
        if state not in JOB_STATES:
            raise ValueError("invalid snapshot state")
        return cls(
            id=_string(_required(value, "id"), "snapshot"),
            revision=_nonnegative_integer(_required(value, "revision"), "snapshot"),
            generation=_nonnegative_integer(_required(value, "generation"), "snapshot"),
            state=state,
            progress=_progress(_required(value, "progress")),
            limits=_bounded_limits(_required(value, "limits"), "snapshot limits"),
            rows=_snapshot_rows(_required(value, "rows")),
            errors=_json_object_list(_required(value, "errors"), "snapshot"),
            recipe=recipe,
            checkpoint=checkpoint,
        )


def row_id(identity: RowIdentity, generic: Mapping[str, tuple[str, ...]]) -> str:
    generic_payload: JsonObject = {}
    for key, values in sorted(generic.items()):
        generic_payload[key] = json_array(sorted(values))
    payload: JsonObject = {"company": identity[0], "website": identity[1], "address": identity[2], "generic": generic_payload}
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def map_observation(observation: ListObservation, mapping: Mapping[str, str]) -> LeadRow:
    fields: dict[str, ObservedValue] = {}
    generic: dict[str, tuple[ObservedValue, ...]] = {}
    for name, values in observation.fields.items():
        evidence = tuple(ObservedValue(raw, " ".join(raw.split()), observation.source_url, observation.observed_at) for raw in values)
        target = mapping.get(name)
        if target in {"company", "website", "address"} and evidence:
            fields[target] = evidence[0]
        else:
            generic[name] = evidence
    company, website, address = fields.get("company"), fields.get("website"), fields.get("address")
    identity = (
        None if company is None else company.normalized,
        None if website is None else website.normalized,
        None if address is None else address.normalized,
    )
    generic_identity = {key: tuple(item.normalized for item in items) for key, items in generic.items()}
    identifier = row_id(identity, generic_identity)
    return LeadRow(identifier, company, website, address, generic=generic)


def _recipe_version(value: JsonValue) -> int:
    version = _integer(value, "recipe version")
    if version != 1:
        raise ValueError("invalid recipe version")
    return version


def _nonnegative_integer(value: JsonValue, label: str) -> int:
    parsed = _integer(value, label)
    if parsed < 0:
        raise ValueError(f"invalid {label}")
    return parsed


def _observed_time(value: ObservedValue) -> datetime | None:
    if not value.observed_at.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo == timezone.utc else None


def _merge_observations(left: tuple[ObservedValue, ...], right: tuple[ObservedValue, ...]) -> tuple[ObservedValue, ...]:
    selected: dict[tuple[str, str, str], ObservedValue] = {}
    for item in left + right:
        key = (item.raw, item.normalized, item.source_url)
        prior = selected.get(key)
        if prior is None or _observation_order(item) < _observation_order(prior):
            selected[key] = item
    return tuple(sorted(selected.values(), key=_observation_order))


def _observation_order(value: ObservedValue) -> tuple[bool, datetime, str, str, str]:
    observed = _observed_time(value)
    return (
        observed is None,
        datetime.max.replace(tzinfo=timezone.utc) if observed is None else observed,
        value.normalized,
        value.source_url,
        value.raw,
    )


def _singular_evidence(row: LeadRow) -> dict[str, tuple[ObservedValue, ...]]:
    fields = {"company": row.company, "website": row.website, "address": row.address}
    merged: dict[str, tuple[ObservedValue, ...]] = {}
    for name, value in fields.items():
        observations = row.evidence.get(name, ())
        if value is not None:
            observations = _merge_observations(observations, (value,))
        if observations:
            merged[name] = observations
    return merged


def _seed_evidence(
    evidence: Mapping[str, tuple[ObservedValue, ...]],
    row: LeadRow,
) -> dict[str, tuple[ObservedValue, ...]]:
    seeded: dict[str, tuple[ObservedValue, ...]] = dict(evidence)
    for name, value in {"company": row.company, "website": row.website, "address": row.address}.items():
        if value is not None:
            seeded[name] = _merge_observations(seeded.get(name, ()), (value,))
    return seeded


def _first_observation(values: tuple[ObservedValue, ...]) -> ObservedValue | None:
    return min(values, key=_observation_order) if values else None


def _merge_maps(
    left: Mapping[str, tuple[ObservedValue, ...]],
    right: Mapping[str, tuple[ObservedValue, ...]],
) -> dict[str, tuple[ObservedValue, ...]]:
    return {
        key: _merge_observations(left.get(key, ()), right.get(key, ()))
        for key in set(left) | set(right)
    }


def merge_lead_rows(existing: LeadRow, incoming: LeadRow) -> list[LeadRow]:
    if existing.id != incoming.id:
        return [existing, incoming]
    evidence = _merge_maps(_singular_evidence(existing), _singular_evidence(incoming))
    return [
        LeadRow(
            id=existing.id,
            company=_first_observation(evidence.get("company", ())),
            website=_first_observation(evidence.get("website", ())),
            address=_first_observation(evidence.get("address", ())),
            emails=_merge_observations(existing.emails, incoming.emails),
            phones=_merge_observations(existing.phones, incoming.phones),
            socials=_merge_observations(existing.socials, incoming.socials),
            generic=_merge_maps(existing.generic, incoming.generic),
            edits={**incoming.edits, **existing.edits},
            excluded=existing.excluded or incoming.excluded,
            status=incoming.status if incoming.status != "pending" else existing.status,
            ai_suggestions=_merge_maps(existing.ai_suggestions, incoming.ai_suggestions),
            evidence=evidence,
        )
    ]
