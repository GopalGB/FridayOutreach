"""Optional one-operation Ego capture adapter with a strict receipt protocol."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from typing import Literal, Mapping

from .model import FetchRequest, JsonObject, JsonValue, PageDocument, Selection, canonical, json_object, observed_now
from .transport import TransportError, validate_fetch_request


EGO_COMMAND = ("ego-browser", "nodejs")
CAPTURE_TIMEOUT_SECONDS = 120
MAX_STDOUT_BYTES = 64 * 1024
MAX_DOM_BYTES = 2 * 1024 * 1024
SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "ego-capture.mjs"
EGO_SUBRESOURCE_LIMITATION = "Browser subresources are not isolated by the static transport policy."


@dataclass(frozen=True)
class EgoCaptureRequest:
    task_space_id: int
    page_label: str
    start_url: str
    allowed_hosts: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    selection: Selection | None
    pagination: Mapping[str, JsonValue]
    limits: Mapping[str, int]
    generation: int
    operation: Literal["navigate", "advance", "read"]


def capture_with_ego(request: EgoCaptureRequest) -> PageDocument:
    try:
        payload = _request_payload(request)
    except (TransportError, ValueError):
        return _failure("denied", "TARGET_DENIED", "browser target was rejected")
    with tempfile.NamedTemporaryFile(prefix="lead-ego-", suffix=".jsonl", delete=False) as output:
        output_path = Path(output.name).absolute()
    try:
        script = _script({**payload, "outputPath": str(output_path)})
        process = subprocess.Popen(list(EGO_COMMAND), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(script.encode("utf-8"), timeout=CAPTURE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_child(process)
            return _failure("timeout", "FETCH_TIMEOUT", "browser capture timed out and may have late effects")
        if process.returncode is None:
            return _failure("unavailable", "CAPABILITY_UNAVAILABLE", "browser capture is unavailable")
        completed = subprocess.CompletedProcess(list(EGO_COMMAND), process.returncode, stdout, stderr)
        return _result(completed, output_path, request)
    except OSError:
        return _failure("unavailable", "CAPABILITY_UNAVAILABLE", "browser capture is unavailable")
    finally:
        output_path.unlink(missing_ok=True)


def _request_payload(request: EgoCaptureRequest) -> JsonObject:
    if not isinstance(request.task_space_id, int) or isinstance(request.task_space_id, bool) or request.task_space_id <= 0:
        raise ValueError("invalid task space")
    if not request.page_label.strip() or request.operation not in {"navigate", "advance", "read"} or request.generation < 0:
        raise ValueError("invalid browser request")
    selection = None if request.selection is None else Selection.from_json(request.selection.to_json())
    target = validate_fetch_request(FetchRequest(request.start_url, request.allowed_hosts, request.allowed_paths, request.limits))
    pagination = _pagination(request.pagination)
    return {
        "taskSpaceId": request.task_space_id,
        "pageLabel": request.page_label,
        "startUrl": target.url,
        "allowedHosts": list(target.allowed_hosts),
        "allowedPaths": list(request.allowed_paths),
        "selection": None if selection is None else selection.to_json(),
        "pagination": pagination,
        "limits": dict(request.limits),
        "generation": request.generation,
        "operation": request.operation,
    }


def _pagination(value: Mapping[str, JsonValue]) -> JsonObject:
    if set(value) != {"mode", "selector"}:
        raise ValueError("invalid pagination")
    mode = value["mode"]
    selector = value["selector"]
    if mode not in {"none", "next", "loadMore", "scroll"}:
        raise ValueError("invalid pagination")
    if mode == "none" and selector is not None:
        raise ValueError("invalid pagination")
    if mode in {"next", "loadMore"} and (not isinstance(selector, str) or not selector):
        raise ValueError("invalid pagination")
    if mode == "scroll" and selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("invalid pagination")
    return {"mode": mode, "selector": selector}


def _script(payload: JsonObject) -> str:
    template = SCRIPT_PATH.read_text(encoding="utf-8")
    marker = "/*__REQUEST_JSON__*/"
    if template.count(marker) != 1:
        raise OSError("trusted Ego script is unavailable")
    return template.replace(marker, json.dumps(canonical(payload), ensure_ascii=True))


def _result(completed: subprocess.CompletedProcess[bytes], output_path: Path, request: EgoCaptureRequest) -> PageDocument:
    stdout = completed.stdout if isinstance(completed.stdout, bytes) else b""
    stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
    if completed.returncode != 0 or len(stdout) + len(stderr) > MAX_STDOUT_BYTES:
        return _failure("unavailable", "CAPABILITY_UNAVAILABLE", "browser capture is unavailable")
    receipt = _receipt(stdout, stderr)
    if receipt is None:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser receipt was invalid")
    if receipt.get("status") == "unavailable" and receipt.get("code") == "CAPABILITY_UNAVAILABLE":
        return _failure("unavailable", "CAPABILITY_UNAVAILABLE", "browser space or page is unavailable")
    if set(receipt) != {"path", "sha256", "records"} or receipt.get("path") != str(output_path) or receipt.get("records") != 1:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser receipt was invalid")
    sha256 = receipt.get("sha256")
    if not isinstance(sha256, str) or len(sha256) != 64:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser receipt was invalid")
    try:
        raw = output_path.read_bytes()
    except OSError:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser result was unavailable")
    if len(raw) > MAX_DOM_BYTES or hashlib.sha256(raw).hexdigest() != sha256:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser result was invalid")
    lines = raw.splitlines()
    if len(lines) != 1:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser result was invalid")
    try:
        document = PageDocument.from_json(json_object(json.loads(lines[0])))
        checked = validate_fetch_request(FetchRequest(document.url, request.allowed_hosts, request.allowed_paths, request.limits))
    except (TransportError, ValueError, json.JSONDecodeError):
        return _failure("malformed", "RESPONSE_MALFORMED", "browser result was invalid")
    if document.url != checked.url or len(document.html.encode("utf-8")) > MAX_DOM_BYTES:
        return _failure("malformed", "RESPONSE_MALFORMED", "browser result was invalid")
    if document.status == "completed" and document.error is None:
        return document
    if document.status == "empty" and document.error is None and not document.html:
        return document
    if document.status not in {"completed", "empty"} and document.error is not None and not document.html:
        return document
    return _failure("malformed", "RESPONSE_MALFORMED", "browser result was invalid")


def _receipt(stdout: bytes, stderr: bytes) -> JsonObject | None:
    stdout_present, stdout_receipt = _channel_receipt(stdout)
    stderr_present, stderr_receipt = _channel_receipt(stderr)
    if stdout_present == stderr_present:
        return None
    return stdout_receipt if stdout_present else stderr_receipt


def _channel_receipt(channel: bytes) -> tuple[bool, JsonObject | None]:
    if not channel:
        return False, None
    try:
        lines = channel.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return True, None
    if not any(line.strip() for line in lines):
        return False, None
    if not lines or not lines[0].startswith("EGO_RECEIPT ") or any(line.strip() for line in lines[1:]):
        return True, None
    try:
        return True, json_object(json.loads(lines[0][len("EGO_RECEIPT ") :]))
    except (ValueError, json.JSONDecodeError):
        return True, None


def _failure(status: str, code: str, message: str) -> PageDocument:
    return PageDocument("", "", observed_now(), hashlib.sha256(b"").hexdigest(), status, {"code": code, "message": message, "url": None})


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return
