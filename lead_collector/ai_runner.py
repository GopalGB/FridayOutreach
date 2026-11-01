"""Parent boundary for the optional, local-only ScrapeGraphAI worker."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import http.client
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import TypeGuard

from .ai import AI_FIELDS, AiError, AiResult, AiSource, AiSuggestion, AssistInput as AssistInput
from .extract import _clean_soup
from .model import JsonObject, JsonValue, ObservedValue, json_object


MAX_HTML_BYTES = 32 * 1024
MAX_PROTOCOL_BYTES = 64 * 1024
MAX_VALUE_BYTES = 512
WORKER_DEADLINE_SECONDS = 120
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 11434
OLLAMA_PREFIX = "ollama/"
OPENBLAS_THREADS = "1"
OMP_THREADS = "1"
VECLIB_THREADS = "1"
TOKENIZERS_PARALLELISM = "false"
_RESPONSE_KEYS = frozenset({"version", "status", "model", "fields", "usage", "networkAttempts", "error"})
_SOURCE_ATTRIBUTES = frozenset({"aria-label", "content", "href", "title", "value"})


@dataclass(frozen=True)
class _WorkerOutcome:
    output: bytes | None
    error: AiError | None


@dataclass(frozen=True)
class _WorkerRequest:
    payload: bytes
    deadline: float


def sanitize_html(value: str) -> str:
    """Return bounded captured markup for the local worker, never a URL."""

    without_active = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", value, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<!--.*?-->|\x00", "", without_active, flags=re.DOTALL).strip()
    wrapped = f"<html><body>{clean}</body></html>"
    visible = html.unescape(re.sub(r"<[^>]+>", " ", wrapped))
    if not visible.strip() or len(wrapped.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("invalid captured HTML")
    return wrapped


def assist(input_value: AssistInput) -> AiResult:
    """Return source-backed local suggestions without importing the optional SDK here."""

    if input_value.mode == "off":
        return AiResult("disabled", (), (), None, None, None)
    if input_value.mode != "scrapegraph":
        return _blocked("COST_POLICY", "this AI adapter only supports explicit local ScrapeGraphAI")
    if input_value.cancelled is not None and input_value.cancelled():
        return _blocked("CANCELLED", "AI request was cancelled")
    model = input_value.model
    if not _is_local_model(model):
        return _blocked("INPUT_INVALID", "an explicit local Ollama model is required")
    try:
        captured = sanitize_html(input_value.html)
    except ValueError:
        return _blocked("INPUT_INVALID", "captured HTML is empty or exceeds the local bound")
    runtime = _venv_python()
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        return AiResult("unavailable", (), (), None, None, AiError("SDK_UNAVAILABLE", "isolated AI runtime is unavailable"))
    unavailable = _probe_local_model(model)
    if unavailable is not None:
        return AiResult("unavailable", (), (), None, None, unavailable)
    outcome = _run_worker(runtime, input_value, captured)
    if outcome.error is not None:
        status = "timeout" if outcome.error.code == "TIMEOUT" else "blocked"
        return AiResult(status, (), (), None, None, outcome.error)
    if outcome.output is None:
        return _blocked("AI_OUTPUT_INVALID", "AI worker returned no protocol response")
    return _result_from_worker(outcome.output, input_value, captured)


def _blocked(code: str, message: str) -> AiResult:
    return AiResult("blocked", (), (), None, None, AiError(code, message))


def _is_local_model(model: str | None) -> TypeGuard[str]:
    if model is None or not model.startswith(OLLAMA_PREFIX):
        return False
    name = model.removeprefix(OLLAMA_PREFIX)
    return bool(name) and "/" not in name and not name.startswith("cloud/") and all(part.lower() != "cloud" for part in name.split(":"))


def _venv_python() -> Path:
    return Path(__file__).resolve().parents[1] / ".venv-ai" / "bin" / "python"


def _probe_local_model(model: str) -> AiError | None:
    """Inspect only local model metadata. This never starts or pulls a model."""

    try:
        tags = _ollama_json("GET", "/api/tags", None)
        show = _ollama_json("POST", "/api/show", {"name": model.removeprefix(OLLAMA_PREFIX)})
        _validate_local_model_metadata(model, tags, show)
    except (OSError, ValueError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return AiError("CAPABILITY_UNAVAILABLE", "explicit local Ollama model is unavailable")
    return None


def _ollama_json(method: str, path: str, body: Mapping[str, str] | None) -> JsonObject:
    encoded = None if body is None else json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection(LOCAL_HOST, LOCAL_PORT, timeout=2)
    try:
        connection.request(method, path, body=encoded, headers={"Content-Type": "application/json"} if encoded is not None else {})
        response = connection.getresponse()
        data = response.read(MAX_PROTOCOL_BYTES + 1)
        if response.status != 200 or len(data) > MAX_PROTOCOL_BYTES:
            raise ValueError("local model metadata unavailable")
    finally:
        connection.close()
    return json_object(json.loads(data.decode("utf-8")))


def _validate_local_model_metadata(model: str, tags: JsonObject, show: JsonObject) -> None:
    """Reject cloud-backed metadata even when a loopback Ollama daemon answered."""

    name = model.removeprefix(OLLAMA_PREFIX)
    models = tags.get("models")
    if not isinstance(models, list):
        raise ValueError("invalid tags response")
    matches = [item for item in models if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or _has_remote_metadata(matches[0]) or _has_remote_metadata(show):
        raise ValueError("remote model metadata")


def _has_remote_metadata(value: JsonValue) -> bool:
    if isinstance(value, list):
        return any(_has_remote_metadata(item) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        normalized = key.lower()
        if normalized in {"remote_model", "remote_host", "cloud_model", "cloud_host"}:
            if item is not None and (not isinstance(item, str) or bool(item.strip())):
                return True
        if normalized in {"cloud", "is_cloud"}:
            if isinstance(item, bool):
                if item:
                    return True
            elif item is not None and (not isinstance(item, str) or bool(item.strip())):
                return True
        if _has_remote_metadata(item):
            return True
    return False


def _child_environment(home: str) -> dict[str, str]:
    """Build an allowlist environment without inherited keys, tracing, or proxies."""

    return {
        "HOME": home,
        "TMPDIR": home,
        "PATH": os.defpath,
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "SCRAPEGRAPHAI_TELEMETRY_ENABLED": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_TRACING": "false",
        "OTEL_SDK_DISABLED": "true",
        "OPENBLAS_NUM_THREADS": OPENBLAS_THREADS,
        "OMP_NUM_THREADS": OMP_THREADS,
        "VECLIB_MAXIMUM_THREADS": VECLIB_THREADS,
        "TOKENIZERS_PARALLELISM": TOKENIZERS_PARALLELISM,
    }


def _worker_command(runtime: Path) -> tuple[str, str, str]:
    worker = Path(__file__).resolve().parents[1] / "scripts" / "ai_runner_worker.py"
    return (str(runtime), "-I", str(worker))


def _worker_payload(captured: str, model: str | None) -> bytes | None:
    if not isinstance(model, str):
        return None
    payload = json.dumps({"version": 1, "html": captured, "model": model}, separators=(",", ":")).encode("utf-8")
    return payload if len(payload) <= MAX_PROTOCOL_BYTES else None


def _run_worker(runtime: Path, input_value: AssistInput, captured: str) -> _WorkerOutcome:
    if input_value.cancelled is not None and input_value.cancelled():
        return _WorkerOutcome(None, AiError("CANCELLED", "AI request was cancelled"))
    payload = _worker_payload(captured, input_value.model)
    if payload is None:
        return _WorkerOutcome(None, AiError("INPUT_INVALID", "AI worker request exceeds the protocol bound"))
    with tempfile.TemporaryDirectory(prefix="lead-collector-ai-") as home:
        request = _WorkerRequest(payload, time.monotonic() + WORKER_DEADLINE_SECONDS)
        process = _open_worker(runtime, home)
        if process is None:
            return _WorkerOutcome(None, AiError("SDK_UNAVAILABLE", "isolated AI worker could not start"))
        return _communicate_worker(process, input_value, request)


def _open_worker(runtime: Path, home: str) -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            _worker_command(runtime),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=home,
            env=_child_environment(home),
        )
    except OSError:
        return None


def _communicate_worker(process: subprocess.Popen[bytes], input_value: AssistInput, request: _WorkerRequest) -> _WorkerOutcome:
    first_input: bytes | None = request.payload
    try:
        while True:
            if input_value.cancelled is not None and input_value.cancelled():
                return _WorkerOutcome(None, AiError("CANCELLED", "AI request was cancelled"))
            remaining = request.deadline - time.monotonic()
            if remaining <= 0:
                return _WorkerOutcome(None, AiError("TIMEOUT", "AI worker exceeded its deadline"))
            try:
                output, _ = process.communicate(input=first_input, timeout=min(remaining, 0.25))
                break
            except subprocess.TimeoutExpired:
                first_input = None
        if process.returncode != 0 or not isinstance(output, bytes):
            return _WorkerOutcome(None, AiError("AI_EXECUTION_FAILED", "isolated AI worker failed"))
        return _WorkerOutcome(output, None)
    except OSError:
        return _WorkerOutcome(None, AiError("AI_EXECUTION_FAILED", "isolated AI worker communication failed"))
    except KeyboardInterrupt:
        return _WorkerOutcome(None, AiError("CANCELLED", "AI request was interrupted"))
    finally:
        _terminate_owned(process)


def _terminate_owned(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def _result_from_worker(output: bytes, input_value: AssistInput, captured: str) -> AiResult:
    """Accept only one bounded protocol message with literal captured values."""

    model = input_value.model
    if not isinstance(model, str):
        return _blocked("AI_OUTPUT_INVALID", "AI worker returned an invalid review-only response")
    try:
        body = _protocol_object(output)
        _validate_worker_protocol(body, model)
        fields = _fields(body["fields"])
        suggestions = _suggestions(fields, input_value.source, captured)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _blocked("AI_OUTPUT_INVALID", "AI worker returned an invalid review-only response")
    return AiResult("completed", tuple(suggestions), (), model, None, None)


def _protocol_object(output: bytes) -> JsonObject:
    if not output or len(output) > MAX_PROTOCOL_BYTES or output != output.strip():
        raise ValueError("invalid worker output")
    return json_object(json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_worker_protocol(body: JsonObject, model: str) -> None:
    version = body.get("version")
    if set(body) != _RESPONSE_KEYS or type(version) is not int or version != 1 or body["status"] != "completed":
        raise ValueError("invalid worker response")
    if body["model"] != model or body["usage"] is not None or body["error"] is not None:
        raise ValueError("invalid worker metadata")
    attempts = body["networkAttempts"]
    if not isinstance(attempts, list):
        raise ValueError("invalid network audit")
    for item in attempts:
        if not isinstance(item, dict) or set(item) != {"event", "destination"}:
            raise ValueError("invalid network audit")
        if item["event"] not in {"socket.connect", "socket.getaddrinfo"} or item["destination"] != f"{LOCAL_HOST}:{LOCAL_PORT}":
            raise ValueError("network boundary violated")


def _fields(value: JsonValue) -> Mapping[str, list[str]]:
    if not isinstance(value, dict) or set(value) != AI_FIELDS:
        raise ValueError("invalid output fields")
    parsed: dict[str, list[str]] = {}
    for field, entries in value.items():
        if not isinstance(entries, list) or len(entries) > 16:
            raise ValueError("invalid output values")
        values: list[str] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry or len(entry.encode("utf-8")) > MAX_VALUE_BYTES:
                raise ValueError("invalid output values")
            values.append(entry)
        parsed[field] = values
    return parsed


def _suggestions(fields: Mapping[str, list[str]], source: AiSource, captured: str) -> list[AiSuggestion]:
    suggestions: list[AiSuggestion] = []
    seen: set[tuple[str, str]] = set()
    evidence = _source_literals(captured)
    for field in sorted(fields):
        for raw in fields[field]:
            identity = (field, raw)
            if identity in seen or not any(raw in value for value in evidence):
                raise ValueError("unsupported output value")
            seen.add(identity)
            identifier = hashlib.sha256(f"{field}\0{raw}".encode("utf-8")).hexdigest()[:16]
            normalized = " ".join(raw.split())
            observation = ObservedValue(raw, normalized, source.url, source.observed_at)
            suggestions.append(AiSuggestion(identifier, field, observation, True, None, None, None))
    return suggestions


def _source_literals(captured: str) -> tuple[str, ...]:
    soup = _clean_soup(captured)
    literals = [value for value in soup.stripped_strings if value]
    for tag in soup.find_all(True):
        for name in _SOURCE_ATTRIBUTES:
            value = tag.attrs.get(name)
            if isinstance(value, str) and value:
                literals.append(value)
            elif isinstance(value, list):
                literals.extend(item for item in value if isinstance(item, str) and item)
    return tuple(literals)
