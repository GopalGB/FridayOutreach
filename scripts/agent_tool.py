#!/usr/bin/env python3
"""Provider-neutral stdin adapter for the bounded enrich command."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import stat
import sys
from io import StringIO


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lead_collector.cli import main
from lead_collector.model import JsonObject, JsonValue
from lead_collector.paths import is_protected_path


MAX_REQUEST_BYTES = 64 * 1024
_KEYS = {"input_csv", "output_csv", "verify_emails", "max_sites", "max_pages_per_site", "max_emails", "overwrite"}
_BOUNDS = {"max_sites": 100, "max_pages_per_site": 5, "max_emails": 1000}


class RequestError(ValueError):
    """A sanitized request rejection for a tool host."""


def _unique(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise RequestError("invalid function arguments")
        result[key] = value
    return result


def _request(raw: bytes) -> JsonObject:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("invalid function arguments")
    try:
        value: JsonValue = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RequestError("invalid function arguments") from exc
    if not isinstance(value, dict) or set(value) - _KEYS or not {"input_csv", "output_csv"}.issubset(value):
        raise RequestError("invalid function arguments")
    _validate_values(value)
    return value


def _validate_values(value: JsonObject) -> None:
    for key in ("input_csv", "output_csv"):
        item = value[key]
        if not isinstance(item, str):
            raise RequestError("invalid function arguments")
        path = Path(item)
        if not item or path.is_absolute() or ".." in path.parts or is_protected_path(item):
            raise RequestError("invalid function arguments")
    for key in ("verify_emails", "overwrite"):
        if key in value and type(value[key]) is not bool:
            raise RequestError("invalid function arguments")
    for key, maximum in _BOUNDS.items():
        if key in value:
            item = value[key]
            if type(item) is not int or not 1 <= item <= maximum:
                raise RequestError("invalid function arguments")


def _child_path(workspace: Path, name: str, required: bool) -> Path:
    candidate = workspace / name
    _safe_directory(workspace, candidate.parent)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if required:
            raise RequestError("invalid function arguments") from None
        return candidate
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RequestError("invalid function arguments")
    return candidate


def _safe_directory(workspace: Path, parent: Path) -> None:
    try:
        parent.relative_to(workspace)
        current = workspace
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RequestError("invalid function arguments")
        for part in parent.relative_to(workspace).parts:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RequestError("invalid function arguments")
    except (FileNotFoundError, ValueError) as exc:
        raise RequestError("invalid function arguments") from exc


def _command(request: JsonObject, workspace: Path) -> list[str]:
    input_name = request["input_csv"]
    output_name = request["output_csv"]
    assert isinstance(input_name, str) and isinstance(output_name, str)
    input_path = _child_path(workspace, input_name, True)
    output_path = _child_path(workspace, output_name, False)
    command = ["enrich", "--input", str(input_path), "--output", str(output_path)]
    if request.get("verify_emails", True):
        command.append("--verify-emails")
    else:
        command.append("--skip-email-verification")
    if request.get("overwrite") is True:
        command.append("--overwrite")
    for key, flag in (("max_sites", "--max-sites"), ("max_pages_per_site", "--max-pages-per-site"), ("max_emails", "--max-emails")):
        if key in request:
            command.extend((flag, str(request[key])))
    return command


def _envelope(code: str) -> str:
    return json.dumps({"version": 1, "ok": False, "data": None, "error": {"code": code, "message": "invalid function arguments"}}, separators=(",", ":"))


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lead-collector-agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        request = _request(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        workspace = args.workspace.resolve()
        command = _command(request, workspace)
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(command)
        rendered = output.getvalue().strip()
        if not isinstance(json.loads(rendered), dict):
            raise RequestError("invalid function arguments")
        print(rendered)
        return int(exit_code)
    except (OSError, RequestError, RecursionError, json.JSONDecodeError, ValueError):
        print(_envelope("INPUT_INVALID"))
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
