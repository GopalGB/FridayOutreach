"""Bounded, deterministic CSV business-site enrichment."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .batch_safety import BatchSafetyError, InputBinding, assert_unchanged, read_input, validate_output
from .email_verify import EmailVerifyRequest, VerificationError, VerificationResult, _default_binary, _trusted_binary, verify_email
from .exports import ExportError, _formula_safe, write_export
from .extract import parse_contacts
from .model import FetchRequest, JsonObject, ObservedValue, PageDocument
from .transport import TransportError, fetch_page, validate_fetch_request


DEFAULTS = {"max_sites": 20, "max_pages_per_site": 3, "max_emails": 100}
BOUNDS = {"max_sites": 100, "max_pages_per_site": 5, "max_emails": 1000}
_ADDED = ("published_emails", "email_evidence", "email_verification", "site_status", "site_evidence")


class BatchError(ValueError):
    """A stable preflight or batch error suitable for a CLI envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BatchOptions:
    max_sites: int = DEFAULTS["max_sites"]
    max_pages_per_site: int = DEFAULTS["max_pages_per_site"]
    max_emails: int = DEFAULTS["max_emails"]
    verify_emails: bool = False
    overwrite: bool = False
    reacher_binary: Path | None = None


@dataclass(frozen=True)
class CsvInput:
    rows: tuple[dict[str, str], ...]
    headers: tuple[str, ...]
    binding: InputBinding


@dataclass(frozen=True)
class SitePlan:
    request: FetchRequest | None
    eligible: bool


@dataclass(frozen=True)
class CrawlResult:
    emails: tuple[ObservedValue, ...]
    pages: tuple[PageDocument, ...]
    status: str


@dataclass(frozen=True)
class RowCapture:
    row: dict[str, str]
    crawl: CrawlResult | None
    status: str


@dataclass(frozen=True)
class VerificationState:
    result: VerificationResult | None
    limited: bool


def _validate_options(options: BatchOptions) -> None:
    values = {
        "max_sites": options.max_sites,
        "max_pages_per_site": options.max_pages_per_site,
        "max_emails": options.max_emails,
    }
    for name, maximum in BOUNDS.items():
        value = values[name]
        if type(value) is not int or value < 1 or value > maximum:
            raise BatchError("INPUT_INVALID", f"{name} is outside its bound")
    if type(options.verify_emails) is not bool or type(options.overwrite) is not bool:
        raise BatchError("INPUT_INVALID", "boolean option is invalid")


def _read_input(path: Path) -> CsvInput:
    raw, binding = read_input(path)
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = tuple(reader.fieldnames or ())
        _validate_headers(headers)
        rows: list[dict[str, str]] = []
        for row in reader:
            validated = _validated_row(row)
            if len(rows) >= 1000:
                raise BatchError("INPUT_INVALID", "CSV rows exceed bounds")
            rows.append(validated)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BatchError("INPUT_INVALID", "malformed CSV") from exc
    return CsvInput(tuple(rows), headers, binding)


def _validate_headers(headers: tuple[str, ...]) -> None:
    normalized = [header.casefold() for header in headers]
    required = {"company", "website"}
    if not headers or len(headers) > 64 or len(set(normalized)) != len(headers):
        raise BatchError("INPUT_INVALID", "CSV headers must be unique and bounded")
    if not required.issubset(normalized) or any(name.casefold() in normalized for name in _ADDED):
        raise BatchError("INPUT_INVALID", "CSV headers are ambiguous")


def _validated_row(row: dict[str | None, str | None]) -> dict[str, str]:
    if None in row or any(value is None for value in row.values()):
        raise BatchError("INPUT_INVALID", "malformed CSV row")
    return {key: value for key, value in row.items() if key is not None and value is not None}


def _fetch_limits(options: BatchOptions) -> Mapping[str, int]:
    return {"maxInputs": 1, "maxPagesPerHost": options.max_pages_per_site, "maxRows": 1000, "maxPages": options.max_pages_per_site}


def _site_request(website: str, options: BatchOptions) -> FetchRequest:
    if website != website.strip():
        raise BatchError("INPUT_INVALID", "website must not contain surrounding whitespace")
    parsed = urlsplit(website)
    host = parsed.hostname or ""
    counterpart = host[4:] if host.startswith("www.") else f"www.{host}"
    try:
        return validate_fetch_request(FetchRequest(website, (host, counterpart), ("/",), _fetch_limits(options)))
    except (TransportError, ValueError) as exc:
        raise BatchError("INPUT_INVALID", "website must be a public HTTP(S) URL") from exc


def _plans(table: CsvInput, options: BatchOptions) -> tuple[SitePlan, ...]:
    website_key = next(header for header in table.headers if header.casefold() == "website")
    requests = tuple(None if not row[website_key] else _site_request(row[website_key], options) for row in table.rows)
    eligible: list[SitePlan] = []
    attempted = 0
    for request in requests:
        can_fetch = request is not None and attempted < options.max_sites
        if can_fetch:
            attempted += 1
        eligible.append(SitePlan(request, can_fetch))
    return tuple(eligible)


def _crawl(request: FetchRequest, options: BatchOptions) -> CrawlResult:
    pages = [fetch_page(request)]
    emails = list(parse_contacts(pages[0]).emails)
    for candidate in parse_contacts(pages[0]).candidates:
        if len(pages) >= options.max_pages_per_site:
            break
        child = _child_request(candidate, request)
        if child is None:
            continue
        document = fetch_page(child)
        pages.append(document)
        emails.extend(parse_contacts(document).emails)
    return CrawlResult(tuple(emails), tuple(pages), _crawl_status(pages, emails))


def _child_request(candidate: str, request: FetchRequest) -> FetchRequest | None:
    try:
        return validate_fetch_request(FetchRequest(candidate, request.allowed_hosts, request.allowed_paths, request.limits))
    except (TransportError, ValueError):
        return None


def _crawl_status(pages: list[PageDocument], emails: list[ObservedValue]) -> str:
    if emails:
        return "completed"
    for document in pages:
        if document.status not in {"completed", "empty"}:
            return _named_status(document)
    return "no_email"


def _named_status(document: PageDocument) -> str:
    error = document.error or {}
    if error.get("code") in {"RATE_LIMITED", "HTTP_429"}:
        return "rate_limited"
    return str(document.status)


def _captures(table: CsvInput, plans: tuple[SitePlan, ...], options: BatchOptions) -> tuple[RowCapture, ...]:
    captured: list[RowCapture] = []
    for row, plan in zip(table.rows, plans, strict=True):
        if plan.request is None:
            captured.append(RowCapture(dict(row), None, "no_website"))
        elif not plan.eligible:
            captured.append(RowCapture(dict(row), None, "skipped_limit"))
        else:
            crawl = _crawl(plan.request, options)
            captured.append(RowCapture(dict(row), crawl, crawl.status))
    return tuple(captured)


def _unique_emails(captures: tuple[RowCapture, ...]) -> tuple[ObservedValue, ...]:
    unique: dict[str, ObservedValue] = {}
    for capture in captures:
        if capture.crawl is not None:
            for observed in capture.crawl.emails:
                unique.setdefault(observed.raw, observed)
    return tuple(unique.values())


def _verification_states(emails: tuple[ObservedValue, ...], options: BatchOptions) -> dict[str, VerificationState]:
    if not options.verify_emails:
        return {}
    states: dict[str, VerificationState] = {}
    for index, observed in enumerate(emails):
        if index >= options.max_emails:
            states[observed.raw] = VerificationState(None, True)
        else:
            states[observed.raw] = VerificationState(verify_email(EmailVerifyRequest(observed.raw, options.reacher_binary)), False)
    return states


def _row_contacts(capture: RowCapture) -> tuple[ObservedValue, ...]:
    if capture.crawl is None:
        return ()
    unique: dict[tuple[str, str, str], ObservedValue] = {}
    for item in capture.crawl.emails:
        unique.setdefault((item.raw, item.source_url, item.observed_at), item)
    return tuple(unique.values())


def _verification_json(item: ObservedValue, state: VerificationState | None, requested: bool) -> JsonObject:
    error: VerificationError | None
    if not requested:
        status, verified, error = "not_checked", False, VerificationError("VERIFY_NOT_REQUESTED", "verification was not requested")
    elif state is None or state.limited:
        status, verified, error = "not_checked_limit", False, VerificationError("VERIFY_LIMIT", "verification limit reached")
    else:
        result = state.result
        if result is None or result.email != item.raw:
            status, verified, error = "unknown", False, VerificationError("VERIFY_OUTPUT_INVALID", "verifier response was invalid")
        else:
            status, verified, error = result.status, result.status == "safe" and result.mailbox_verified, result.error
    return {"input": item.raw, "sourceUrl": item.source_url, "observedAt": item.observed_at, "status": status, "mailboxVerified": verified, "error": None if error is None else error.to_json()}


def _page_json(document: PageDocument) -> JsonObject:
    return {"url": document.url, "observedAt": document.observed_at, "status": _named_status(document), "error": document.error}


def _output_row(capture: RowCapture, states: dict[str, VerificationState], requested: bool) -> dict[str, str]:
    result = dict(capture.row)
    contacts = _row_contacts(capture)
    published = tuple(dict.fromkeys(item.raw for item in contacts))
    verification = [_verification_json(item, states.get(item.raw), requested) for item in contacts]
    pages = [] if capture.crawl is None else [_page_json(page) for page in capture.crawl.pages]
    result.update({"published_emails": "; ".join(published), "email_evidence": json.dumps([item.to_json() for item in contacts], separators=(",", ":")), "email_verification": json.dumps(verification, separators=(",", ":")), "site_status": capture.status, "site_evidence": json.dumps(pages, separators=(",", ":"))})
    return result


def _csv_text(headers: tuple[str, ...], rows: tuple[dict[str, str], ...]) -> str:
    fields = (*headers, *(name for name in _ADDED if name not in headers))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([_formula_safe(field) for field in fields])
    for row in rows:
        writer.writerow([_formula_safe(row.get(field, "")) for field in fields])
    return output.getvalue()


def _verify_runtime(options: BatchOptions) -> None:
    if options.verify_emails and _trusted_binary(options.reacher_binary or _default_binary()) is None:
        raise BatchError("CAPABILITY_UNAVAILABLE", "SMTP verifier runtime is unavailable")


def enrich_csv(input_path: Path, output_path: Path, options: BatchOptions = BatchOptions()) -> JsonObject:
    """Enrich one CSV while preserving fields, rows, and source observations."""

    try:
        _validate_options(options)
        table = _read_input(input_path)
        output = validate_output(output_path, options.overwrite)
        plans = _plans(table, options)
        _verify_runtime(options)
        captures = _captures(table, plans, options)
        states = _verification_states(_unique_emails(captures), options)
        rows = tuple(_output_row(capture, states, options.verify_emails) for capture in captures)
        if table.binding.path == output:
            assert_unchanged(table.binding)
        write_export(output, _csv_text(table.headers, rows), overwrite=options.overwrite)
    except BatchSafetyError as exc:
        raise BatchError(exc.code, exc.message) from exc
    except ExportError as exc:
        raise BatchError(exc.code, str(exc)) from exc
    return {"input": str(input_path), "output": str(output_path), "rows": len(table.rows), "sitesAttempted": sum(plan.eligible for plan in plans), "emailsChecked": sum(state.result is not None and not state.limited for state in states.values()), "inputSha256": table.binding.sha256}
