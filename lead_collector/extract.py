"""Inert, offline HTML extraction. Parsed markup is always data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .model import ContactResult, JsonObject, JsonValue, LeadRow, ObservedValue, PageDocument, Selection, canonical, json_array, merge_lead_rows, row_id


def _is_hidden(tag: Tag) -> bool:
    if tag.attrs is None:
        return False
    if tag.has_attr("hidden") or tag.get("aria-hidden", "").lower() == "true":
        return True
    style = tag.get("style", "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "template", "noscript"]):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if _is_hidden(tag):
            tag.decompose()
    return soup


def _normal(raw: str) -> str:
    return " ".join(raw.split())


_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])([A-Z0-9][A-Z0-9._%+-]*@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w.-])", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?<!\w)(\+?\d[\d(). -]{5,}\d)(?!\w)")
_PHONE_LABEL_PATTERN = re.compile(r"\b(?:call|mobile|phone|tel(?:ephone)?|whatsapp)\s*[:#-]?\s*$", re.IGNORECASE)
_FORMATTED_PHONE_PATTERN = re.compile(r"(?:\+?\d{1,3}[ .-])?(?:\(?\d{2,4}\)?[ .-])\d{2,4}(?:[ .-]\d{2,4})+")
_SOCIAL_HOSTS = frozenset({"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com"})
_CONTACT_LINK_WORDS = frozenset({"about", "contact", "help", "support", "team", "company", "customer-service"})
_STRUCTURED_EMAIL_FIELDS = frozenset({"email", "emailaddress"})
_RESERVED_EMAIL_SUFFIXES = (".example", ".invalid", ".localhost", ".test")
_MAX_STRUCTURED_SCRIPT_BYTES = 128 * 1024
_MAX_STRUCTURED_DEPTH = 16
_MAX_STRUCTURED_NODES = 4096
_NEXT_FLIGHT_PATTERN = re.compile(r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*("(?:\\.|[^"\\])*")\s*\]\s*\)')
_NEXT_EMAIL_PATTERN = re.compile(r'"(?:email|emailAddress)"\s*:\s*"((?:\\.|[^"\\])*)"')
_NEXT_HREF_PATTERN = re.compile(r'"href"\s*:\s*"((?:\\.|[^"\\])*)"')


def _document_observation(raw: str, normalized: str, document: PageDocument) -> ObservedValue:
    return ObservedValue(raw, normalized, document.url, document.observed_at)


def _deduplicate_observations(values: list[ObservedValue]) -> tuple[ObservedValue, ...]:
    unique: dict[tuple[str, str, str], ObservedValue] = {}
    for value in values:
        unique.setdefault((value.raw, value.normalized, value.source_url), value)
    return tuple(unique.values())


def _email(value: str) -> str | None:
    candidate = value.strip()
    return candidate.lower() if _EMAIL_PATTERN.fullmatch(candidate) else None


def _structured_email(value: str) -> str | None:
    normalized = _email(value)
    if normalized is None:
        return None
    domain = normalized.rsplit("@", 1)[1]
    if domain in {"example.com", "example.org", "example.net", "localhost", "invalid", "test"}:
        return None
    return None if domain.endswith(_RESERVED_EMAIL_SUFFIXES) else normalized


def _phone(value: str) -> str | None:
    candidate = _normal(value)
    if re.fullmatch(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", candidate) or re.fullmatch(r"\d{3}-\d{3}-\d{3}", candidate):
        return None
    digits = "".join(char for char in candidate if char.isdigit())
    if len(digits) < 7 or len(digits) > 15 or (len(digits) == 13 and digits.startswith(("978", "979"))):
        return None
    return ("+" if candidate.startswith("+") else "") + digits


def _visible_phone(text: str, match: re.Match[str]) -> tuple[str, str] | None:
    raw = match.group(1)
    normalized = _phone(raw)
    if normalized is None:
        return None
    prefix = text[max(0, match.start(1) - 32) : match.start(1)]
    if _PHONE_LABEL_PATTERN.search(prefix) or _FORMATTED_PHONE_PATTERN.fullmatch(raw.strip()):
        return raw, normalized
    return None


def _host(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
        return None if hostname is None else hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None


def _is_social_host(hostname: str) -> bool:
    return any(hostname == social or hostname.endswith("." + social) for social in _SOCIAL_HOSTS)


def _resolved_http_url(href: str, source_url: str) -> str | None:
    resolved = urljoin(source_url, href)
    try:
        parsed = urlsplit(resolved)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _candidate_url(href: str, text: str, document: PageDocument) -> str | None:
    resolved = _resolved_http_url(href, document.url)
    hostname = None if resolved is None else _host(resolved)
    words = (href + " " + text).lower()
    source_host = _host(document.url)
    if resolved is None or hostname is None:
        return None
    if hostname != source_host or _is_social_host(hostname) or not any(word in words for word in _CONTACT_LINK_WORDS):
        return None
    return resolved


def _link_contacts(link: Tag, document: PageDocument) -> ContactResult:
    href = str(link["href"])
    text = _normal(link.get_text(" ", strip=True))
    lowered = href.lower()
    if lowered.startswith("mailto:"):
        raw = unquote(href[7:]).split("?", 1)[0].strip()
        normalized = _email(raw)
        emails = () if normalized is None else (_document_observation(raw, normalized, document),)
        return ContactResult(document.status, emails, (), (), (), None)
    if lowered.startswith("tel:"):
        raw = unquote(href[4:]).split("?", 1)[0].strip()
        normalized = _phone(raw)
        phones = () if normalized is None else (_document_observation(raw, normalized, document),)
        return ContactResult(document.status, (), phones, (), (), None)
    resolved = _resolved_http_url(href, document.url)
    hostname = None if resolved is None else _host(resolved)
    socials = () if resolved is None or hostname is None or not _is_social_host(hostname) else (_document_observation(href, resolved, document),)
    candidate = _candidate_url(href, text, document)
    candidates = () if candidate is None else (candidate,)
    return ContactResult(document.status, (), (), socials, candidates, None)


def _text_contacts(soup: BeautifulSoup, document: PageDocument) -> ContactResult:
    text = soup.get_text(" ", strip=True)
    emails: list[ObservedValue] = []
    phones: list[ObservedValue] = []
    for match in _EMAIL_PATTERN.finditer(text):
        raw = match.group(1)
        normalized = _email(raw)
        if normalized is not None:
            emails.append(_document_observation(raw, normalized, document))
    for match in _PHONE_PATTERN.finditer(text):
        visible = _visible_phone(text, match)
        if visible is not None:
            raw, normalized = visible
            phones.append(_document_observation(raw, normalized, document))
    return ContactResult(document.status, tuple(emails), tuple(phones), (), (), None)


@dataclass
class _JsonBudget:
    nodes: int = 0


def _json_payload(value: str) -> object | None:
    if not value or len(value.encode("utf-8")) > _MAX_STRUCTURED_SCRIPT_BYTES:
        return None
    try:
        parsed: object = json.loads(value)
        return parsed
    except (json.JSONDecodeError, RecursionError):
        return None


def _json_strings(value: object) -> tuple[tuple[str, str], ...]:
    return tuple(_walk_json_strings(value, 0, _JsonBudget()))


def _walk_json_strings(value: object, depth: int, budget: _JsonBudget) -> list[tuple[str, str]]:
    if depth > _MAX_STRUCTURED_DEPTH or budget.nodes >= _MAX_STRUCTURED_NODES:
        return []
    budget.nodes += 1
    if isinstance(value, str):
        return [("", value)]
    if isinstance(value, list):
        return [item for child in value for item in _walk_json_strings(child, depth + 1, budget)]
    if not isinstance(value, dict):
        return []
    items: list[tuple[str, str]] = []
    for key, child in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(child, str):
            if budget.nodes >= _MAX_STRUCTURED_NODES:
                break
            budget.nodes += 1
            items.append((key, child))
        else:
            items.extend(_walk_json_strings(child, depth + 1, budget))
    return items


def _json_ld_emails(payload: str) -> tuple[str, ...]:
    parsed = _json_payload(payload)
    if parsed is None:
        return ()
    return tuple(email for key, value in _json_strings(parsed) if key.lower() in _STRUCTURED_EMAIL_FIELDS if (email := _structured_email(value)) is not None)


def _mailto_emails(payload: str) -> tuple[str, ...]:
    if "<" not in payload or "mailto:" not in payload.lower():
        return ()
    soup = _clean_soup(payload)
    emails: list[str] = []
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        if href.lower().startswith("mailto:"):
            email = _structured_email(unquote(href[7:]).split("?", 1)[0].strip())
            if email is not None:
                emails.append(email)
    return tuple(emails)


def _application_json_emails(payload: str) -> tuple[str, ...]:
    parsed = _json_payload(payload)
    if parsed is None:
        return ()
    return tuple(email for _, value in _json_strings(parsed) for email in _mailto_emails(value))


def _next_flight_emails(payload: str) -> tuple[str, ...]:
    if len(payload.encode("utf-8")) > _MAX_STRUCTURED_SCRIPT_BYTES:
        return ()
    emails: list[str] = []
    for match in _NEXT_FLIGHT_PATTERN.finditer(payload):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_STRUCTURED_SCRIPT_BYTES:
            continue
        emails.extend(_next_field_emails(value, _NEXT_EMAIL_PATTERN, False))
        emails.extend(_next_field_emails(value, _NEXT_HREF_PATTERN, True))
    return tuple(emails)


def _next_field_emails(payload: str, pattern: re.Pattern[str], mailto: bool) -> tuple[str, ...]:
    emails: list[str] = []
    for match in pattern.finditer(payload):
        try:
            value = json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and (not mailto or value.lower().startswith("mailto:")):
            email = _structured_email(unquote(value[7:]).split("?", 1)[0] if mailto else value)
            if email is not None:
                emails.append(email)
    return tuple(emails)


def _cloudflare_email(value: str) -> str | None:
    if len(value) < 4 or len(value) > 512 or len(value) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", value):
        return None
    try:
        encoded = bytes.fromhex(value)
        decoded = bytes(byte ^ encoded[0] for byte in encoded[1:]).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    return _structured_email(decoded)


def _visible_tag(tag: Tag) -> bool:
    current: Tag | None = tag
    while current is not None:
        if _is_hidden(current):
            return False
        current = current.parent if isinstance(current.parent, Tag) else None
    return True


def _script_text(tag: Tag) -> str | None:
    value = tag.string if isinstance(tag.string, str) else tag.get_text()
    return value if len(value.encode("utf-8")) <= _MAX_STRUCTURED_SCRIPT_BYTES else None


def _structured_contacts(document: PageDocument) -> ContactResult:
    soup = BeautifulSoup(document.html, "html.parser")
    emails: list[ObservedValue] = []
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        if not _visible_tag(tag):
            continue
        email = _cloudflare_email(str(tag["data-cfemail"]))
        if email is not None:
            emails.append(_document_observation(email, email, document))
    for script in soup.find_all("script"):
        payload = _script_text(script)
        if payload is None:
            continue
        kind = str(script.get("type", "")).lower()
        found = _json_ld_emails(payload) if kind == "application/ld+json" else _application_json_emails(payload) if kind == "application/json" else _next_flight_emails(payload) if "self.__next_f.push(" in payload else ()
        emails.extend(_document_observation(email, email, document) for email in found)
    return ContactResult(document.status, tuple(emails), (), (), (), None)


def _contacts_from_markup(document: PageDocument) -> ContactResult:
    soup = _clean_soup(document.html)
    parts: list[ContactResult] = []
    for link in soup.find_all("a", href=True):
        if isinstance(link, Tag):
            parts.append(_link_contacts(link, document))
    parts.append(_text_contacts(soup, document))
    parts.append(_structured_contacts(document))
    emails = [item for part in parts for item in part.emails]
    phones = [item for part in parts for item in part.phones]
    socials = [item for part in parts for item in part.socials]
    candidates = tuple(dict.fromkeys(candidate for part in parts for candidate in part.candidates))
    return ContactResult(document.status, _deduplicate_observations(emails), _deduplicate_observations(phones), _deduplicate_observations(socials), candidates, None)


def parse_contacts(document: PageDocument) -> ContactResult:
    """Extract only published contact evidence from an already-fetched page."""

    if document.status != "completed":
        return ContactResult(document.status, (), (), (), (), document.error)
    return _contacts_from_markup(document)


def static_next_url(document: PageDocument, selector: str) -> str | None:
    """Return one inert next-page href; fetching remains the runner's responsibility."""

    try:
        matches = _clean_soup(document.html).select(selector)
    except ValueError as exc:
        raise ValueError("invalid next selector") from exc
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0], Tag):
        raise ValueError("ambiguous next selector")
    href = matches[0].get("href")
    if not isinstance(href, str) or not href:
        raise ValueError("invalid next link")
    return urljoin(document.url, href)


def _class_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _tag_name(tag: Tag) -> str:
    return tag.name if isinstance(tag.name, str) else ""


def _fingerprint(row: Tag, cells: list[Tag], columns: list[str]) -> str:
    cell_shapes: list[JsonValue] = []
    for cell in cells:
        cell_shape: JsonObject = {
            "tag": _tag_name(cell),
            "classes": json_array(sorted(_class_names(cell.get("class")))),
        }
        cell_shapes.append(cell_shape)
    shape: JsonObject = {
        "rowTag": _tag_name(row),
        "cells": cell_shapes,
        "columns": json_array(columns),
    }
    return hashlib.sha256(canonical(shape).encode("utf-8")).hexdigest()


def _table_groups(soup: BeautifulSoup) -> tuple[list[tuple[list[str], list[Tag], Tag]], Selection] | None:
    table = soup.find("table")
    if not isinstance(table, Tag):
        return None
    rows = [row for row in table.find_all("tr") if isinstance(row, Tag)]
    if not rows:
        return [], Selection("table", "", (), ())
    first_cells = [cell for cell in rows[0].find_all(["th", "td"], recursive=False) if isinstance(cell, Tag)]
    has_header = bool(first_cells) and all(cell.name == "th" for cell in first_cells)
    columns = [_normal(cell.get_text(" ", strip=True)) or f"column{index + 1}" for index, cell in enumerate(first_cells)] if has_header else [f"column{index + 1}" for index in range(len(first_cells))]
    data_rows = rows[1:] if has_header else rows
    groups: list[tuple[list[str], list[Tag], Tag]] = []
    for row in data_rows:
        cells = [cell for cell in row.find_all(["th", "td"], recursive=False) if isinstance(cell, Tag)]
        if cells:
            groups.append((columns, cells, row))
    fingerprint = _fingerprint(groups[0][2], groups[0][1], columns) if groups else ""
    samples = tuple(tuple(_normal(cell.get_text(" ", strip=True)) for cell in cells) for _, cells, _ in groups[:3])
    return groups, Selection("table", fingerprint, tuple(columns), samples)


def _card_groups(soup: BeautifulSoup, selection: Selection) -> tuple[list[tuple[list[str], list[Tag], Tag]], Selection]:
    if not selection.selector:
        return [], selection
    cards = [card for card in soup.select(selection.selector) if isinstance(card, Tag)]
    groups: list[tuple[list[str], list[Tag], Tag]] = []
    for card in cards:
        if selection.column_selectors:
            columns = list(selection.columns) or list(selection.column_selectors)
            cells: list[Tag] = []
            for column in columns:
                selector = selection.column_selectors.get(column)
                cell = None if selector is None else card.select_one(selector)
                cells.append(cell if isinstance(cell, Tag) else soup.new_tag("span"))
        else:
            cells = [child for child in card.find_all(recursive=False) if isinstance(child, Tag)]
            columns = [
                classes[0] if (classes := _class_names(child.get("class"))) else f"column{index + 1}"
                for index, child in enumerate(cells)
            ]
        groups.append((columns, cells, card))
    columns = groups[0][0] if groups else list(selection.columns)
    fingerprint = _fingerprint(groups[0][2], groups[0][1], columns) if groups else ""
    samples = tuple(tuple(_normal(cell.get_text(" ", strip=True)) for cell in cells) for _, cells, _ in groups[:3])
    return groups, Selection(selection.selector, fingerprint, tuple(columns), samples, selection.column_selectors)


def _value(cell: Tag, document: PageDocument, website: bool) -> ObservedValue | None:
    text = cell.get_text(" ", strip=True)
    if not text:
        return None
    if website:
        link = cell if cell.name == "a" and cell.has_attr("href") else cell.find("a", href=True)
        if isinstance(link, Tag):
            raw = str(link["href"])
            return ObservedValue(raw, urljoin(document.url, raw), document.url, document.observed_at)
    return ObservedValue(text, _normal(text), document.url, document.observed_at)


def _rows(groups: list[tuple[list[str], list[Tag], Tag]], document: PageDocument, mapping: Mapping[str, str]) -> list[LeadRow]:
    merged: dict[str, LeadRow] = {}
    for columns, cells, _ in groups:
        fields: dict[str, ObservedValue] = {}
        generic: dict[str, tuple[ObservedValue, ...]] = {}
        for index, cell in enumerate(cells):
            name = columns[index] if index < len(columns) else f"column{index + 1}"
            target = mapping.get(name)
            item = _value(cell, document, target == "website")
            if item is None:
                continue
            if target in {"company", "website", "address"}:
                fields[target] = item
            else:
                generic[name] = (item,)
        company, website, address = fields.get("company"), fields.get("website"), fields.get("address")
        identity = (
            None if company is None else company.normalized,
            None if website is None else website.normalized,
            None if address is None else address.normalized,
        )
        generic_identity = {key: tuple(value.normalized for value in values) for key, values in generic.items()}
        identifier = row_id(identity, generic_identity)
        row = LeadRow(identifier, company, website, address, generic=generic)
        if identifier not in merged:
            merged[identifier] = row
        else:
            for combined in merge_lead_rows(merged[identifier], row):
                merged[combined.id] = combined
    return list(merged.values())


def preview_document(document: PageDocument, mapping: Mapping[str, str], selection: Selection | None = None) -> tuple[list[LeadRow], Selection]:
    soup = _clean_soup(document.html)
    if selection is not None and selection.selector not in {None, "table"}:
        groups, actual = _card_groups(soup, selection)
    else:
        table = _table_groups(soup)
        groups, actual = ([], Selection(None, "", (), ())) if table is None else table
    if selection is not None and selection.fingerprint and selection.fingerprint != actual.fingerprint:
        raise ValueError("selection fingerprint changed")
    return _rows(groups, document, mapping), actual


def extract_rows(document: PageDocument, mapping: Mapping[str, str], selection: Mapping[str, JsonValue] | Selection | None = None) -> list[LeadRow]:
    parsed: Selection | None
    if isinstance(selection, Mapping):
        parsed = Selection.from_json(selection)
    else:
        parsed = selection
    rows, _ = preview_document(document, mapping, parsed)
    return rows
