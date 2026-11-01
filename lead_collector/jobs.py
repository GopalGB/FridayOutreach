"""Single-process checkpointed collection runners and controls."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
from typing import Callable, Iterator, Literal
from urllib.parse import urlsplit

from .ego import EgoCaptureRequest
from .extract import parse_contacts, preview_document, static_next_url
from .model import FetchRequest, JobCheckpoint, JobSnapshot, JsonObject, LeadRow, ObservedValue, PageDocument, merge_lead_rows
from .store import StoreConflict, commit_job, load_job, managed_lease
from .transport import TransportError, validate_fetch_request

MAX_DOCUMENT_COMMIT_RETRIES = 3


@dataclass(frozen=True)
class RunnerDeps:
    fetch: Callable[[FetchRequest], PageDocument]
    render: Callable[[EgoCaptureRequest], PageDocument] | None = None


def control_job(connection: sqlite3.Connection, job_id: str, action: Literal["pause", "stop"]) -> JobSnapshot:
    snapshot = load_job(connection, job_id)
    if snapshot is None:
        raise StoreConflict(job_id)
    if action == "pause":
        if snapshot.state != "running":
            return snapshot
        return commit_job(connection, snapshot.revision, replace(snapshot, state="pausing"))
    if snapshot.state == "stopped":
        return snapshot
    return commit_job(connection, snapshot.revision, replace(snapshot, state="stopped", generation=snapshot.generation + 1))


@dataclass
class JobRunner:
    connection: sqlite3.Connection
    state_path: Path
    deps: RunnerDeps

    def run(self, job_id: str) -> JobSnapshot:
        with self._lease(job_id):
            return self._start(job_id, resume=False)

    def resume(self, job_id: str) -> JobSnapshot:
        with self._lease(job_id):
            return self._start(job_id, resume=True)

    @contextmanager
    def _lease(self, job_id: str) -> Iterator[None]:
        name = hashlib.sha256(job_id.encode("utf-8")).hexdigest() + ".lock"
        with managed_lease(self.connection, name):
            yield

    def _start(self, job_id: str, *, resume: bool) -> JobSnapshot:
        snapshot = self._snapshot(job_id)
        if snapshot.state in {"running", "pausing"}:
            snapshot = commit_job(self.connection, snapshot.revision, replace(snapshot, state="interrupted"))
            if not resume:
                return snapshot
        if resume:
            if snapshot.state not in {"paused", "interrupted"}:
                return snapshot
            if snapshot.checkpoint is None:
                return self._unresumable(snapshot)
        elif snapshot.state != "idle":
            return snapshot
        if snapshot.checkpoint is None:
            return self._unresumable(snapshot)
        started = commit_job(self.connection, snapshot.revision, replace(snapshot, state="running"))
        return self._work(started.id)

    def _unresumable(self, snapshot: JobSnapshot) -> JobSnapshot:
        error: JsonObject = {"code": "CAPABILITY_UNAVAILABLE", "message": "job has no resumable checkpoint", "url": None, "status": "interrupted"}
        next_snapshot = replace(snapshot, state="interrupted", errors=snapshot.errors + (error,))
        return commit_job(self.connection, snapshot.revision, next_snapshot)

    def _renderer_unavailable(self, snapshot: JobSnapshot) -> JobSnapshot:
        error: JsonObject = {"code": "CAPABILITY_UNAVAILABLE", "message": "browser renderer is required for this pagination", "url": None, "status": "unavailable"}
        return commit_job(self.connection, snapshot.revision, replace(snapshot, state="failed", errors=snapshot.errors + (error,)))

    def _work(self, job_id: str) -> JobSnapshot:
        while True:
            snapshot = self._snapshot(job_id)
            if snapshot.state == "pausing":
                return commit_job(self.connection, snapshot.revision, replace(snapshot, state="paused"))
            if snapshot.state != "running":
                return snapshot
            checkpoint = snapshot.checkpoint
            if checkpoint is None:
                return self._unresumable(snapshot)
            if _requires_renderer(snapshot) and (checkpoint.renderer is None or self.deps.render is None):
                return self._renderer_unavailable(snapshot)
            if not checkpoint.pending_urls:
                return commit_job(self.connection, snapshot.revision, replace(snapshot, state="completed"))
            reserved = self._reserve(snapshot)
            if reserved is None:
                continue
            before_operation = self._snapshot(job_id)
            if before_operation.generation != reserved.generation or before_operation.state == "stopped":
                return before_operation
            if before_operation.state == "pausing":
                return commit_job(self.connection, before_operation.revision, replace(before_operation, state="paused"))
            if before_operation.state != "running":
                return before_operation
            document = self._fetch(before_operation)
            saved = self._commit_document(job_id, document, reserved.generation)
            if saved is not None:
                return saved

    def _commit_document(self, job_id: str, document: PageDocument, generation: int) -> JobSnapshot | None:
        latest = self._snapshot(job_id)
        for attempt in range(MAX_DOCUMENT_COMMIT_RETRIES):
            if latest.generation != generation or latest.state == "stopped":
                return latest
            if latest.state not in {"running", "pausing"}:
                return latest
            try:
                saved = commit_job(self.connection, latest.revision, self._apply_document(latest, document))
            except StoreConflict:
                if attempt + 1 == MAX_DOCUMENT_COMMIT_RETRIES:
                    raise
                latest = self._snapshot(job_id)
                continue
            if saved.state == "paused":
                return saved
            return None
        raise StoreConflict(job_id)

    def _reserve(self, snapshot: JobSnapshot) -> JobSnapshot | None:
        checkpoint = _checkpoint(snapshot)
        url = checkpoint.pending_urls[0]
        host = _host(url)
        page_cap = snapshot.limits["maxPages"]
        host_cap = snapshot.limits["maxPagesPerHost"]
        exhausted = snapshot.progress["attemptedPages"] >= page_cap or (checkpoint.mode == "contacts" and checkpoint.attempted_by_host.get(host, 0) >= host_cap)
        if exhausted:
            self._skip_limited(snapshot, url)
            return None
        attempts = dict(checkpoint.attempted_by_host)
        attempts[host] = attempts.get(host, 0) + 1
        reserved_checkpoint = replace(checkpoint, attempted_by_host=attempts)
        progress = dict(snapshot.progress)
        progress["attemptedPages"] += 1
        return commit_job(self.connection, snapshot.revision, replace(snapshot, progress=progress, checkpoint=reserved_checkpoint))

    def _skip_limited(self, snapshot: JobSnapshot, url: str) -> None:
        checkpoint = _checkpoint(snapshot)
        error: JsonObject = {"code": "JOB_FAILED", "message": "page limit reached", "url": url, "status": "limited"}
        updated = replace(checkpoint, pending_urls=checkpoint.pending_urls[1:])
        commit_job(self.connection, snapshot.revision, replace(snapshot, checkpoint=updated, errors=snapshot.errors + (error,)))
        return None

    def _fetch(self, snapshot: JobSnapshot) -> PageDocument:
        checkpoint = _checkpoint(snapshot)
        recipe = snapshot.recipe
        if recipe is None:
            return _failed_document(checkpoint.pending_urls[0], "CAPABILITY_UNAVAILABLE", "job has no recipe")
        if checkpoint.renderer is not None:
            if self.deps.render is None:
                return _failed_document(checkpoint.pending_urls[0], "CAPABILITY_UNAVAILABLE", "browser renderer is unavailable")
            ego_request = _ego_request(snapshot)
            if ego_request is None:
                return _failed_document(checkpoint.pending_urls[0], "CAPABILITY_UNAVAILABLE", "browser renderer is unavailable")
            try:
                return self.deps.render(ego_request)
            except Exception:
                return _failed_document(ego_request.start_url, "FETCH_FAILED", "browser renderer failed")
        fetch_request = FetchRequest(checkpoint.pending_urls[0], recipe.allowed_hosts, recipe.allowed_paths, snapshot.limits)
        try:
            return self.deps.fetch(fetch_request)
        except Exception:
            return _failed_document(fetch_request.url, "FETCH_FAILED", "static fetch failed")

    def _apply_document(self, snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot:
        outcome = _renderer_document_outcome(snapshot, document)
        if outcome is not None:
            return outcome
        outcome = _repeated_document_outcome(snapshot, document)
        if outcome is not None:
            return outcome
        outcome = _failed_document_outcome(snapshot, document)
        return outcome if outcome is not None else _completed_document(snapshot, document)

    def _snapshot(self, job_id: str) -> JobSnapshot:
        snapshot = load_job(self.connection, job_id)
        if snapshot is None:
            raise StoreConflict(job_id)
        return snapshot


def _checkpoint(snapshot: JobSnapshot) -> JobCheckpoint:
    if snapshot.checkpoint is None:
        raise ValueError("missing checkpoint")
    return snapshot.checkpoint


def _host(url: str) -> str:
    parsed = urlsplit(url)
    return "" if parsed.hostname is None else parsed.hostname.rstrip(".").lower()


def _completed_seed_inputs(checkpoint: JobCheckpoint, url: str) -> int:
    if url in checkpoint.cursors:
        return 0
    return sum(1 for input_url in checkpoint.input_urls if input_url == url)


def _document_advance(snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot:
    checkpoint = _checkpoint(snapshot)
    requested_url = checkpoint.pending_urls[0]
    next_checkpoint = replace(checkpoint, pending_urls=checkpoint.pending_urls[1:], cursors=checkpoint.cursors + (document.url,))
    progress = dict(snapshot.progress)
    progress["completedInputs"] = min(progress["totalInputs"], progress["completedInputs"] + _completed_seed_inputs(checkpoint, requested_url))
    state = "paused" if snapshot.state == "pausing" else "running"
    return replace(snapshot, state=state, progress=progress, checkpoint=next_checkpoint)


def _renderer_document_outcome(snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot | None:
    if _checkpoint(snapshot).renderer is None:
        return None
    advanced = _document_advance(snapshot, document)
    checkpoint = _checkpoint(advanced)
    if document.status == "empty":
        return replace(advanced, state="completed", checkpoint=replace(checkpoint, pending_urls=()))
    if document.status == "completed":
        return None
    return replace(advanced, state="failed", checkpoint=replace(checkpoint, pending_urls=()), errors=advanced.errors + (_page_error(document),))


def _repeated_document_outcome(snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot | None:
    checkpoint = _checkpoint(snapshot)
    if document.status != "completed" or document.sha256 not in checkpoint.seen_page_hashes:
        return None
    advanced = _document_advance(snapshot, document)
    next_checkpoint = _checkpoint(advanced)
    if _is_renderer_scroll(snapshot):
        unchanged = checkpoint.unchanged_scrolls + 1
        if unchanged < 2 and advanced.progress["attemptedPages"] < snapshot.limits["maxPages"]:
            return replace(advanced, checkpoint=replace(next_checkpoint, pending_urls=(document.url,), unchanged_scrolls=unchanged))
        return replace(advanced, state="completed", checkpoint=replace(next_checkpoint, pending_urls=(), unchanged_scrolls=unchanged))
    error: JsonObject = {"code": "JOB_FAILED", "message": "repeated page", "url": document.url, "status": "repeated"}
    return replace(advanced, state="completed", checkpoint=replace(next_checkpoint, pending_urls=()), errors=advanced.errors + (error,))


def _failed_document_outcome(snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot | None:
    if document.status == "completed":
        return None
    checkpoint = _checkpoint(snapshot)
    requested_url = checkpoint.pending_urls[0]
    advanced = _document_advance(snapshot, document)
    outcomes = _contact_outcome_rows(snapshot, document, requested_url)
    return replace(advanced, rows=_merge_rows(snapshot.rows, outcomes), errors=advanced.errors + (_page_error(document),))


def _completed_checkpoint(snapshot: JobSnapshot, document: PageDocument) -> JobCheckpoint:
    checkpoint = _checkpoint(snapshot)
    unchanged = 0 if _is_renderer_scroll(snapshot) else checkpoint.unchanged_scrolls
    return replace(checkpoint, seen_page_hashes=checkpoint.seen_page_hashes + (document.sha256,), unchanged_scrolls=unchanged)


def _layout_drift_outcome(snapshot: JobSnapshot, document: PageDocument, advanced: JobSnapshot) -> JobSnapshot:
    error: JsonObject = {"code": "LAYOUT_DRIFT", "message": "page extraction failed; review selection layout", "url": document.url, "status": "layout-drift"}
    state = "failed" if not _checkpoint(snapshot).seen_page_hashes else advanced.state
    return replace(advanced, state=state, errors=advanced.errors + (error,))


def _limited_document(completed: JobSnapshot, rows: tuple[JsonObject, ...], document: PageDocument) -> JobSnapshot:
    error: JsonObject = {"code": "JOB_FAILED", "message": "row limit reached", "url": document.url, "status": "limited"}
    checkpoint = _checkpoint(completed)
    return replace(completed, state="completed", rows=rows[: completed.limits["maxRows"]], checkpoint=replace(checkpoint, pending_urls=()), errors=completed.errors + (error,))


def _completed_document(snapshot: JobSnapshot, document: PageDocument) -> JobSnapshot:
    checkpoint = _checkpoint(snapshot)
    requested_url = checkpoint.pending_urls[0]
    advanced = _document_advance(snapshot, document)
    try:
        rows, candidates = _document_rows(snapshot, document, requested_url)
    except ValueError:
        return _layout_drift_outcome(snapshot, document, advanced)
    completed = replace(advanced, checkpoint=_completed_checkpoint(advanced, document))
    combined = _merge_rows(snapshot.rows, rows)
    if checkpoint.mode == "collect" and len(combined) >= snapshot.limits["maxRows"]:
        return _limited_document(completed, combined, document)
    queued = _queue_candidates(completed, candidates, requested_url)
    return replace(completed, rows=combined, checkpoint=queued)


def _failed_document(url: str, code: str, message: str) -> PageDocument:
    return PageDocument(url, "", "1970-01-01T00:00:00Z", hashlib.sha256(b"").hexdigest(), "failed", {"code": code, "message": message, "url": None})


def _page_error(document: PageDocument) -> JsonObject:
    if document.error is not None:
        return {**document.error, "status": document.status}
    return {"code": "JOB_FAILED", "message": "empty page", "url": document.url, "status": document.status}


def _document_rows(snapshot: JobSnapshot, document: PageDocument, requested_url: str) -> tuple[list[LeadRow], tuple[str, ...]]:
    recipe = snapshot.recipe
    checkpoint = _checkpoint(snapshot)
    if recipe is None:
        raise ValueError("missing recipe")
    if checkpoint.mode == "collect":
        rows, _ = preview_document(document, recipe.mapping, recipe.selection)
        if recipe.pagination["mode"] not in {"next", "loadMore", "scroll"}:
            return rows, ()
        if checkpoint.renderer is not None and recipe.pagination["mode"] in {"next", "loadMore", "scroll"}:
            return rows, (document.url,)
        if recipe.pagination["mode"] in {"loadMore", "scroll"}:
            return rows, (document.url,)
        selector = recipe.pagination["selector"]
        if not isinstance(selector, str):
            raise ValueError("invalid next selector")
        next_url = static_next_url(document, selector)
        return rows, () if next_url is None else (next_url,)
    contacts = parse_contacts(document)
    existing = {row.id: row for row in (LeadRow.from_json(value) for value in snapshot.rows)}
    contact_rows: list[LeadRow] = []
    for owner, urls in checkpoint.source_rows.items():
        if requested_url not in urls and document.url not in urls:
            continue
        original = existing.get(owner)
        if original is None:
            website = ObservedValue(requested_url, requested_url, requested_url, document.observed_at)
            contact_rows.append(LeadRow(owner, website=website, emails=contacts.emails, phones=contacts.phones, socials=contacts.socials, status="completed"))
            continue
        updates = LeadRow(original.id, emails=contacts.emails, phones=contacts.phones, socials=contacts.socials, status="completed")
        contact_rows.extend(merge_lead_rows(original, updates))
    return contact_rows, contacts.candidates


def _contact_outcome_rows(snapshot: JobSnapshot, document: PageDocument, requested_url: str) -> list[LeadRow]:
    checkpoint = _checkpoint(snapshot)
    if checkpoint.mode != "contacts":
        return []
    status = "empty" if document.status == "empty" else "failed"
    existing = {row.id: row for row in (LeadRow.from_json(value) for value in snapshot.rows)}
    outcomes: list[LeadRow] = []
    for owner, urls in checkpoint.source_rows.items():
        if requested_url not in urls and document.url not in urls:
            continue
        original = existing.get(owner)
        if original is None:
            website = ObservedValue(requested_url, requested_url, requested_url, document.observed_at)
            outcomes.append(LeadRow(owner, website=website, status=status))
            continue
        outcomes.extend(merge_lead_rows(original, LeadRow(original.id, status=status)))
    return outcomes


def _merge_rows(saved_rows: tuple[JsonObject, ...], incoming_rows: list[LeadRow]) -> tuple[JsonObject, ...]:
    merged = {row.id: row for row in (LeadRow.from_json(value) for value in saved_rows)}
    for incoming in incoming_rows:
        existing = merged.get(incoming.id)
        if existing is None:
            merged[incoming.id] = incoming
            continue
        for row in merge_lead_rows(existing, incoming):
            merged[row.id] = row
    return tuple(row.to_json() for row in merged.values())


def _queue_candidates(snapshot: JobSnapshot, candidates: tuple[str, ...], requested_url: str) -> JobCheckpoint:
    checkpoint = _checkpoint(snapshot)
    if checkpoint.mode == "collect":
        return _queue_collect_next(snapshot, checkpoint, candidates)
    document_url = checkpoint.cursors[-1]
    pending = list(checkpoint.pending_urls)
    known = set(checkpoint.input_urls) | set(pending) | set(checkpoint.cursors)
    queued_by_host: dict[str, int] = {}
    for url in pending:
        host = _host(url)
        queued_by_host[host] = queued_by_host.get(host, 0) + 1
    for candidate in candidates:
        host = _host(candidate)
        used = checkpoint.attempted_by_host.get(host, 0) + queued_by_host.get(host, 0)
        if candidate in known or not host or used >= snapshot.limits["maxPagesPerHost"]:
            continue
        pending.append(candidate)
        known.add(candidate)
        queued_by_host[host] = queued_by_host.get(host, 0) + 1
    source_rows: dict[str, tuple[str, ...]] = {}
    for owner, urls in checkpoint.source_rows.items():
        if requested_url not in urls and document_url not in urls:
            source_rows[owner] = urls
            continue
        aliases = tuple(url for url in (document_url,) if url not in urls)
        discovered = tuple(candidate for candidate in candidates if candidate in pending and candidate not in urls and candidate not in aliases)
        source_rows[owner] = urls + aliases + discovered
    return replace(checkpoint, pending_urls=tuple(pending), source_rows=source_rows)


def _queue_collect_next(snapshot: JobSnapshot, checkpoint: JobCheckpoint, candidates: tuple[str, ...]) -> JobCheckpoint:
    if not candidates or snapshot.recipe is None:
        return checkpoint
    if checkpoint.renderer is not None and snapshot.recipe.pagination["mode"] in {"next", "loadMore", "scroll"}:
        if checkpoint.renderer is None or snapshot.progress["attemptedPages"] >= snapshot.limits["maxPages"]:
            return checkpoint
        return replace(checkpoint, pending_urls=checkpoint.pending_urls + (candidates[0],))
    try:
        request = FetchRequest(candidates[0], snapshot.recipe.allowed_hosts, snapshot.recipe.allowed_paths, snapshot.limits)
        candidate = validate_fetch_request(request).url
    except (TransportError, ValueError):
        return checkpoint
    if candidate in checkpoint.cursors or candidate in checkpoint.pending_urls:
        return checkpoint
    if snapshot.progress["attemptedPages"] + len(checkpoint.pending_urls) >= snapshot.limits["maxPages"]:
        return checkpoint
    return replace(checkpoint, pending_urls=checkpoint.pending_urls + (candidate,))


def _requires_renderer(snapshot: JobSnapshot) -> bool:
    return snapshot.recipe is not None and snapshot.recipe.pagination["mode"] in {"loadMore", "scroll"}


def _ego_request(snapshot: JobSnapshot) -> EgoCaptureRequest | None:
    checkpoint = _checkpoint(snapshot)
    recipe = snapshot.recipe
    renderer = checkpoint.renderer
    if recipe is None or renderer is None:
        return None
    task_space = renderer.get("taskSpaceId")
    page_label = renderer.get("pageLabel")
    if not isinstance(task_space, int) or isinstance(task_space, bool) or task_space <= 0 or not isinstance(page_label, str) or not page_label.strip():
        return None
    dynamic = recipe.pagination["mode"] in {"next", "loadMore", "scroll"}
    operation: Literal["navigate", "advance", "read"] = "advance" if dynamic and checkpoint.cursors else "navigate"
    return EgoCaptureRequest(
        task_space,
        page_label,
        checkpoint.pending_urls[0],
        recipe.allowed_hosts,
        recipe.allowed_paths,
        recipe.selection,
        recipe.pagination,
        snapshot.limits,
        snapshot.generation,
        operation,
    )


def _is_renderer_scroll(snapshot: JobSnapshot) -> bool:
    return snapshot.checkpoint is not None and snapshot.checkpoint.renderer is not None and snapshot.recipe is not None and snapshot.recipe.pagination["mode"] == "scroll"
