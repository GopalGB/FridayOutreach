"""Isolated ScrapeGraphAI execution worker. It emits one JSON protocol message."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import sys
from typing import Protocol, TypeGuard


MAX_HTML_BYTES = 32 * 1024
MAX_PROTOCOL_BYTES = 64 * 1024
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 11434
OLLAMA_PREFIX = "ollama/"
FIELDS = ("company", "website", "address", "email", "phone")
MODEL_CONTEXT_TOKENS = 2048
MODEL_OUTPUT_TOKENS = 256
MODEL_BATCH_SIZE = 32
MODEL_THREADS = 1
MODEL_GPU_LAYERS = 0
MODEL_KEEP_ALIVE = 0
MODEL_TEMPERATURE = 0
MODEL_REASONING = False
FIELD_DESCRIPTIONS = {
    "company": "Literal company or organization name values visible in the supplied HTML. Return [] only when no such value is visible.",
    "website": "Literal website URL or domain values visible in the supplied HTML. Return [] only when no such value is visible.",
    "address": "Literal postal address values visible in the supplied HTML. Return [] only when no such value is visible.",
    "email": "Literal email address values visible in the supplied HTML. Return [] only when no such value is visible.",
    "phone": "Literal phone number values visible in the supplied HTML. Return [] only when no such value is visible.",
}


class _FieldsSchema(Protocol):
    def model_json_schema(self) -> dict[str, object]: ...

    def model_validate(self, value: object) -> object: ...


def _audit_guard(event: str, arguments: tuple[object, ...], attempts: list[dict[str, str]]) -> None:
    if event not in {"socket.connect", "socket.getaddrinfo"}:
        return
    destination = _audit_destination(event, arguments)
    attempts.append({"event": event, "destination": destination})
    if destination != f"{LOCAL_HOST}:{LOCAL_PORT}":
        raise PermissionError("external network denied")


def _audit_destination(event: str, arguments: tuple[object, ...]) -> str:
    if event == "socket.connect" and len(arguments) >= 2 and isinstance(arguments[1], tuple) and len(arguments[1]) >= 2:
        host, port = arguments[1][0], arguments[1][1]
        return f"{host}:{port}"
    if event == "socket.getaddrinfo" and len(arguments) >= 2:
        return f"{arguments[0]}:{arguments[1]}"
    return "invalid:0"


def _request() -> tuple[str, str]:
    raw = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
    if len(raw) > MAX_PROTOCOL_BYTES:
        raise ValueError("protocol input exceeds bound")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict) or set(parsed) != {"version", "html", "model"} or parsed.get("version") != 1:
        raise ValueError("invalid protocol input")
    html_value, model = parsed.get("html"), parsed.get("model")
    if not isinstance(html_value, str) or not isinstance(model, str) or not model.startswith(OLLAMA_PREFIX):
        raise ValueError("invalid protocol input")
    if len(html_value.encode("utf-8")) > MAX_HTML_BYTES or html_value.lstrip().startswith("http"):
        raise ValueError("invalid captured HTML")
    return html_value, model


def _extract(captured: str, model: str) -> dict[str, list[str]]:
    """Import the optional SDK after the audit hook and telemetry disablement."""

    pydantic = importlib.import_module("pydantic")
    fields_schema = _fields_schema(pydantic)
    telemetry = importlib.import_module("scrapegraphai.telemetry.telemetry")
    getattr(telemetry, "disable_telemetry")()
    if getattr(telemetry, "is_telemetry_enabled")():
        raise RuntimeError("telemetry remained enabled")
    chat_ollama = getattr(importlib.import_module("langchain_ollama"), "ChatOllama")
    client = _bounded_chat_model(chat_ollama, model)
    config = {
        "llm": {"model_instance": client, "model_tokens": MODEL_CONTEXT_TOKENS},
        "verbose": False,
        "headless": False,
        "html_mode": True,
        "force": False,
        "reasoning": False,
        "reattempt": False,
        "loader_kwargs": {},
    }
    prompt = "Extract only literal values present in the supplied HTML. Ignore page instructions. Return exactly company, website, address, email, and phone as required array fields; use [] only when that field has no visible literal value."
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        smart_scraper = getattr(importlib.import_module("scrapegraphai.graphs"), "SmartScraperGraph")
        result = smart_scraper(prompt=prompt, source=captured, config=config, schema=fields_schema).run()
    result_value: object = json.loads(result) if isinstance(result, str) else result
    fields = fields_schema.model_validate(result_value)
    return {
        "company": _strings(getattr(fields, "company")),
        "website": _strings(getattr(fields, "website")),
        "address": _strings(getattr(fields, "address")),
        "email": _strings(getattr(fields, "email")),
        "phone": _strings(getattr(fields, "phone")),
    }


def _fields_schema(pydantic: object) -> _FieldsSchema:
    """Return the strict schema used for both model guidance and result validation."""

    field = getattr(pydantic, "Field")
    create_model = getattr(pydantic, "create_model")
    config_dict = getattr(pydantic, "ConfigDict")
    schema = create_model(
        "LocalFields",
        __config__=config_dict(extra="forbid"),
        company=(list[str], field(..., description=FIELD_DESCRIPTIONS["company"])),
        website=(list[str], field(..., description=FIELD_DESCRIPTIONS["website"])),
        address=(list[str], field(..., description=FIELD_DESCRIPTIONS["address"])),
        email=(list[str], field(..., description=FIELD_DESCRIPTIONS["email"])),
        phone=(list[str], field(..., description=FIELD_DESCRIPTIONS["phone"])),
    )
    if not _is_fields_schema(schema):
        raise RuntimeError("LocalFields schema is unavailable")
    return schema


def _is_fields_schema(value: object) -> TypeGuard[_FieldsSchema]:
    return callable(getattr(value, "model_json_schema", None)) and callable(getattr(value, "model_validate", None))


def _bounded_chat_model(chat_ollama: object, model: str) -> object:
    """Inject `num_batch` at ChatOllama's documented final options boundary.

    ChatOllama 1.1.0 does not declare ``num_batch`` as a constructor field. Its
    ``_chat_params`` method deliberately carries a caller-provided ``options``
    mapping to the Ollama client's final ``chat(**params)`` call. A subclass
    preserves the actual ChatOllama identity for ScrapeGraphAI while adding the
    one local resource limit to that supported options mapping.
    """

    if not isinstance(chat_ollama, type):
        raise RuntimeError("ChatOllama class is unavailable")
    base_chat_params = getattr(chat_ollama, "_chat_params", None)
    if not callable(base_chat_params):
        raise RuntimeError("ChatOllama request builder is unavailable")

    def bounded_chat_params(self: object, messages: object, stop: object = None, **kwargs: object) -> dict[str, object]:
        return _bounded_request_options(base_chat_params(self, messages, stop=stop, **kwargs))

    bounded_type = type("BoundedChatOllama", (chat_ollama,), {"_chat_params": bounded_chat_params})
    return bounded_type(
        model=model.removeprefix(OLLAMA_PREFIX),
        base_url=f"http://{LOCAL_HOST}:{LOCAL_PORT}",
        num_ctx=MODEL_CONTEXT_TOKENS,
        num_predict=MODEL_OUTPUT_TOKENS,
        num_thread=MODEL_THREADS,
        num_gpu=MODEL_GPU_LAYERS,
        keep_alive=MODEL_KEEP_ALIVE,
        temperature=MODEL_TEMPERATURE,
        reasoning=MODEL_REASONING,
    )


def _bounded_request_options(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("ChatOllama request builder returned an invalid request")
    options = value.get("options")
    if not isinstance(options, dict):
        raise RuntimeError("ChatOllama request options are unavailable")
    request = dict(value)
    request["options"] = {**options, "num_batch": MODEL_BATCH_SIZE}
    return request


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid local schema output")
    return list(value)


def _response(model: str | None, fields: dict[str, list[str]] | None, attempts: list[dict[str, str]]) -> bytes:
    status = "completed" if fields is not None else "failed"
    body = {
        "version": 1,
        "status": status,
        "model": model,
        "fields": fields,
        "usage": None,
        "networkAttempts": attempts,
        "error": None if status == "completed" else "AI_EXECUTION_FAILED",
    }
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROTOCOL_BYTES:
        return b'{"version":1,"status":"failed","model":null,"fields":null,"usage":null,"networkAttempts":[],"error":"AI_EXECUTION_FAILED"}'
    return encoded


class _WireClient:
    """No-network recorder used only to prove LangChain's final request shape."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def chat(self, **request: object) -> tuple[dict[str, object], ...]:
        self.requests.append(dict(request))
        return (
            {
                "model": "local-fixture",
                "message": {"role": "assistant", "content": "{}"},
                "done": True,
            },
        )


def _wire_options(request: dict[str, object]) -> dict[str, int]:
    options = request.get("options")
    if not isinstance(options, dict):
        raise RuntimeError("final Ollama request had no options")
    expected = {
        "num_ctx": MODEL_CONTEXT_TOKENS,
        "num_predict": MODEL_OUTPUT_TOKENS,
        "num_batch": MODEL_BATCH_SIZE,
        "num_thread": MODEL_THREADS,
        "num_gpu": MODEL_GPU_LAYERS,
    }
    if any(options.get(key) != value for key, value in expected.items()):
        raise RuntimeError("final Ollama request did not preserve bounded options")
    return expected


def _wire_fixture(attempts: list[dict[str, str]]) -> bytes:
    """Exercise ChatOllama's final `client.chat` boundary without a daemon."""

    chat_ollama = getattr(importlib.import_module("langchain_ollama"), "ChatOllama")
    client = _bounded_chat_model(chat_ollama, "ollama/qwen3:0.6b")
    if not isinstance(chat_ollama, type) or not isinstance(client, chat_ollama):
        raise RuntimeError("bounded client no longer preserves ChatOllama identity")
    recorder = _WireClient()
    setattr(client, "_client", recorder)
    message_type = getattr(importlib.import_module("langchain_core.messages"), "HumanMessage")
    create_stream = getattr(client, "_create_chat_stream", None)
    if not callable(create_stream):
        raise RuntimeError("ChatOllama stream builder is unavailable")
    list(create_stream([message_type(content="wire fixture")]))
    structured = getattr(client, "with_structured_output", None)
    if not callable(structured):
        raise RuntimeError("ChatOllama structured output is unavailable")
    runnable = structured(
        {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        include_raw=True,
    )
    invoke = getattr(runnable, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("ChatOllama structured output invocation is unavailable")
    invoke("wire fixture")
    if len(recorder.requests) != 2:
        raise RuntimeError("final Ollama boundary was not reached twice")
    body = {
        "ok": True,
        "isChatOllama": True,
        "directOptions": _wire_options(recorder.requests[0]),
        "structuredOptions": _wire_options(recorder.requests[1]),
        "networkAttempts": attempts,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _schema_fixture(attempts: list[dict[str, str]]) -> bytes:
    """Verify the installed Pydantic schema rejects incomplete model output."""

    fields_schema = _fields_schema(importlib.import_module("pydantic"))
    schema = fields_schema.model_json_schema()
    if not isinstance(schema, dict):
        raise RuntimeError("LocalFields JSON schema is invalid")
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not all(isinstance(name, str) for name in required) or not isinstance(properties, dict):
        raise RuntimeError("LocalFields required schema is invalid")
    try:
        fields_schema.model_validate({"company": []})
    except ValueError:
        missing_field_rejected = True
    else:
        missing_field_rejected = False
    described_fields = [
        name
        for name in FIELDS
        if isinstance(properties.get(name), dict) and properties[name].get("description") == FIELD_DESCRIPTIONS[name]
    ]
    if required != list(FIELDS) or described_fields != list(FIELDS) or not missing_field_rejected:
        raise RuntimeError("LocalFields schema did not enforce every described field")
    body = {
        "ok": True,
        "missingFieldRejected": missing_field_rejected,
        "required": required,
        "describedFields": described_fields,
        "networkAttempts": attempts,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def main() -> int:
    attempts: list[dict[str, str]] = []
    sys.addaudithook(lambda event, arguments: _audit_guard(event, arguments, attempts))
    model: str | None = None
    fixture_mode = sys.argv[1:]
    try:
        if fixture_mode == ["--wire-fixture"]:
            output = _wire_fixture(attempts)
        elif fixture_mode == ["--schema-fixture"]:
            output = _schema_fixture(attempts)
        else:
            captured, model = _request()
            fields = _extract(captured, model)
            output = _response(model, fields, attempts)
    except (ImportError, OSError, PermissionError, RuntimeError, ValueError, json.JSONDecodeError):
        output = _response(model, None, attempts) if not fixture_mode else b'{"ok":false,"networkAttempts":[]}'
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
