"""Offline review projections and safe local export serialization."""

from __future__ import annotations

import csv
from io import StringIO
import json
import os
from pathlib import Path
import stat
from typing import Iterable
import unicodedata
import uuid
from dataclasses import dataclass, replace

from .model import JsonObject, JsonValue, LeadRow, ObservedValue, canonical, json_object
from .paths import is_protected_path


CSV_COLUMNS = (
    "company",
    "website",
    "address",
    "emails",
    "phones",
    "social_links",
    "status",
    "source_urls",
    "evidence",
    "collected_at",
)


class ExportError(ValueError):
    """Raised for an invalid local export or import payload."""

    def __init__(self, message: str, code: str = "INPUT_INVALID") -> None:
        super().__init__(message)
        self.code = code


REVIEW_FIELDS = frozenset({"id", "company", "website", "address", "status"})


@dataclass(frozen=True)
class ReviewRequest:
    query: str
    sort: str
    selected_ids: tuple[str, ...]


def _effective(row: LeadRow, field: str, value: ObservedValue | None) -> str:
    edited = row.edits.get(field)
    if edited is not None:
        return edited
    return "" if value is None else value.normalized


def _all_observed(row: LeadRow) -> tuple[ObservedValue, ...]:
    values: list[ObservedValue] = []
    for singular in (row.company, row.website, row.address):
        if singular is not None:
            values.append(singular)
    values.extend(row.emails)
    values.extend(row.phones)
    values.extend(row.socials)
    for mapping in (row.generic, row.ai_suggestions, row.evidence):
        for observations in mapping.values():
            values.extend(observations)
    return tuple(values)


def _review_value(row: LeadRow, field: str) -> str:
    if field == "id":
        return row.id
    if field == "company":
        return _effective(row, field, row.company)
    if field == "website":
        return _effective(row, field, row.website)
    if field == "address":
        return _effective(row, field, row.address)
    return row.status


def review_rows(rows: Iterable[LeadRow], request: ReviewRequest) -> tuple[LeadRow, ...]:
    """Filter user-visible row values without collapsing immutable lead identities."""

    if request.sort not in REVIEW_FIELDS:
        raise ExportError("invalid review sort field")
    selected = frozenset(request.selected_ids)
    needle = request.query.casefold()
    visible: list[LeadRow] = []
    for row in rows:
        values = (row.id, *(_review_value(row, field) for field in ("company", "website", "address", "status")), *(item.normalized for item in _all_observed(row)))
        if selected and row.id not in selected:
            continue
        if needle and not any(needle in value.casefold() for value in values):
            continue
        visible.append(row)
    return tuple(sorted(visible, key=lambda row: (_review_value(row, request.sort).casefold(), row.id)))


def edit_row(row: LeadRow, field: str, value: str) -> LeadRow:
    """Store a human correction separately from collection evidence."""

    if field not in {"company", "website", "address"}:
        raise ExportError("invalid editable field")
    return replace(row, edits={**row.edits, field: value})


def exclude_row(row: LeadRow) -> LeadRow:
    """Mark one immutable row as excluded without dropping its observations."""

    return replace(row, excluded=True)


def _observed_json(values: Iterable[ObservedValue]) -> list[JsonValue]:
    return [value.to_json() for value in values]


def _evidence(row: LeadRow) -> str:
    singular: dict[str, list[JsonValue]] = {}
    for name, value in (("company", row.company), ("website", row.website), ("address", row.address)):
        observations = list(row.evidence.get(name, ()))
        if value is not None and value not in observations:
            observations.append(value)
        singular[name] = _observed_json(observations)
    body: JsonObject = {
        **singular,
        "emails": _observed_json(row.emails),
        "phones": _observed_json(row.phones),
        "socialLinks": _observed_json(row.socials),
        "generic": {name: _observed_json(values) for name, values in row.generic.items()},
        "aiSuggestions": {name: _observed_json(values) for name, values in row.ai_suggestions.items()},
        "edits": dict(row.edits),
        "excluded": row.excluded,
    }
    return canonical(body)


def _formula_safe(value: str) -> str:
    for character in value:
        if character.isspace() or unicodedata.category(character).startswith("C"):
            continue
        return "'" + value if character in "=+-@" else value
    return value


def _csv_row(row: LeadRow) -> tuple[str, ...]:
    observations = _all_observed(row)
    source_urls = tuple(dict.fromkeys(item.source_url for item in observations))
    collected_at = min((item.observed_at for item in observations), default="")
    values = (
        _effective(row, "company", row.company),
        _effective(row, "website", row.website),
        _effective(row, "address", row.address),
        "; ".join(item.normalized for item in row.emails),
        "; ".join(item.normalized for item in row.phones),
        "; ".join(item.normalized for item in row.socials),
        row.status,
        "; ".join(source_urls),
        _evidence(row),
        collected_at,
    )
    return tuple(_formula_safe(value) for value in values)


def csv_text(rows: Iterable[LeadRow]) -> str:
    """Return an RFC-style CSV projection without mutating collected observations."""

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow(_csv_row(row))
    return output.getvalue()


def json_text(rows: Iterable[LeadRow]) -> str:
    """Return the versioned lossless local exchange document."""

    return canonical({"version": 1, "rows": [row.to_json() for row in rows]})


def _without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("duplicate JSON key")
        result[key] = value
    return result


def parse_json_text(value: str) -> tuple[LeadRow, ...]:
    """Validate a lossless export before it can be committed as a local job."""

    try:
        decoded: object = json.loads(value, object_pairs_hook=_without_duplicate_keys)
        document = json_object(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExportError("invalid JSON export") from exc
    version = document.get("version")
    if set(document) != {"version", "rows"} or not isinstance(version, int) or isinstance(version, bool):
        raise ExportError("invalid JSON export")
    if version != 1:
        raise ExportError("unsupported JSON export version", "SCHEMA_UNSUPPORTED")
    rows = document["rows"]
    if not isinstance(rows, list):
        raise ExportError("invalid JSON export rows")
    parsed: list[LeadRow] = []
    identifiers: set[str] = set()
    for item in rows:
        try:
            row = LeadRow.from_json(json_object(item))
        except ValueError as exc:
            raise ExportError("invalid JSON export row") from exc
        if row.id in identifiers:
            raise ExportError("duplicate JSON export row")
        identifiers.add(row.id)
        parsed.append(row)
    return tuple(parsed)


def _unsafe_component(component: str) -> bool:
    return is_protected_path(component)


def _open_output_parent(path: Path, overwrite: bool) -> tuple[int, str]:
    if ".." in path.parts:
        raise ExportError("unsafe output path")
    absolute = path.absolute()
    if not absolute.name or any(_unsafe_component(part) for part in absolute.parts):
        raise ExportError("unsafe output path")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow == 0 or directory == 0:
        raise ExportError("safe output is unavailable")
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, os.O_RDONLY | directory)
        for part in absolute.parts[1:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            target_metadata = os.stat(absolute.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None and (stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(target_metadata.st_mode)):
            raise ExportError("unsafe output path")
        if target_metadata is not None and not overwrite:
            raise ExportError("output already exists", "OUTPUT_EXISTS")
        return descriptor, absolute.name
    except (OSError, ExportError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, ExportError):
            raise
        raise ExportError("unsafe output path") from exc


def _open_staging(parent_descriptor: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        name = f".lead-collector-{uuid.uuid4().hex}.tmp"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_descriptor), name
        except FileExistsError:
            continue
        except OSError as exc:
            raise ExportError("export write failed") from exc
    raise ExportError("export write failed")


def write_export(path: Path, content: str, *, overwrite: bool) -> None:
    """Atomically replace an explicitly approved safe output path."""

    parent_descriptor, target_name = _open_output_parent(path, overwrite)
    descriptor: int | None = None
    temporary = ""
    try:
        descriptor, temporary = _open_staging(parent_descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = None
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if overwrite:
            os.replace(temporary, target_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
            temporary = ""
        else:
            try:
                os.link(temporary, target_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor, follow_symlinks=False)
            except FileExistsError as exc:
                raise ExportError("output already exists", "OUTPUT_EXISTS") from exc
            os.unlink(temporary, dir_fd=parent_descriptor)
            temporary = ""
    except OSError as exc:
        raise ExportError("export write failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)
