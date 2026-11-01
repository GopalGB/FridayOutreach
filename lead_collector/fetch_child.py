"""One-shot, killable anonymous HTTP(S) fetch child."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import socket
import ssl
import sys
import time
from typing import Mapping
from urllib.parse import urljoin

from .model import FetchRequest, JsonObject, PageDocument, json_object, observed_now
from .transport import CanonicalTarget, TransportError, VettedAddress, _canonicalize_target, _resolve_all_public


MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_CHILD_INPUT_BYTES = 1024 * 1024
CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 5
SAFE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Encoding": "identity",
    "User-Agent": "LeadCollector/1.0",
    "Connection": "close",
}


class FetchFailure(Exception):
    """Fixed child-side failure safe to return to the supervisor."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _HopResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: CanonicalTarget, vetted: VettedAddress, timeout: float) -> None:
        super().__init__(target.hostname, target.port, timeout=timeout)
        self._vetted = vetted

    def connect(self) -> None:
        self.sock = socket.socket(self._vetted.family, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._vetted.sockaddr)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self, target: CanonicalTarget, vetted: VettedAddress, timeout: float) -> None:
        super().__init__(target, vetted, timeout)
        self._target = target
        self._context = ssl.create_default_context()

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise OSError("pinned socket was unavailable")
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._target.hostname)


def _remaining_seconds(deadline_ns: int) -> float:
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise FetchFailure("FETCH_TIMEOUT", "fetch deadline expired")
    return remaining_ns / 1_000_000_000


def _outcome(url: str, status: str, failure: FetchFailure) -> PageDocument:
    return PageDocument(url, "", observed_now(), hashlib.sha256(b"").hexdigest(), status, {"code": failure.code, "message": failure.message, "url": url})


def _document(url: str, body: bytes) -> PageDocument:
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FetchFailure("RESPONSE_MALFORMED", "response body was not UTF-8") from exc
    status = "empty" if not body else "completed"
    return PageDocument(url, html, observed_now(), hashlib.sha256(body).hexdigest(), status, None)


def _header(response: http.client.HTTPResponse, name: str) -> str | None:
    value = response.getheader(name)
    return value if isinstance(value, str) else None


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized.isdecimal():
        raise FetchFailure("RESPONSE_MALFORMED", "response length was invalid")
    return int(normalized)


def _read_body(transport_socket: socket.socket, response: http.client.HTTPResponse, deadline_ns: int) -> bytes:
    declared_length = _content_length(_header(response, "Content-Length"))
    if declared_length is not None and declared_length > MAX_BODY_BYTES:
        raise FetchFailure("CONTENT_TRUNCATED", "response exceeded the body limit")
    chunks: list[bytes] = []
    received = 0
    while True:
        if declared_length is not None and received == declared_length:
            return b"".join(chunks)
        if response.isclosed():
            if declared_length is not None:
                raise FetchFailure("RESPONSE_MALFORMED", "response body ended early")
            return b"".join(chunks)
        transport_socket.settimeout(_remaining_seconds(deadline_ns))
        try:
            chunk = response.read(min(CHUNK_BYTES, MAX_BODY_BYTES + 1 - received))
        except http.client.IncompleteRead as exc:
            raise FetchFailure("RESPONSE_MALFORMED", "response body ended early") from exc
        if not chunk:
            if declared_length is not None and received != declared_length:
                raise FetchFailure("RESPONSE_MALFORMED", "response body ended early")
            return b"".join(chunks)
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise FetchFailure("CONTENT_TRUNCATED", "response exceeded the body limit")
        chunks.append(chunk)


def _request_once(target: CanonicalTarget, vetted: VettedAddress, deadline_ns: int) -> _HopResponse:
    timeout = _remaining_seconds(deadline_ns)
    connection: http.client.HTTPConnection
    if target.scheme == "https":
        connection = _PinnedHTTPSConnection(target, vetted, timeout)
    else:
        connection = _PinnedHTTPConnection(target, vetted, timeout)
    response: http.client.HTTPResponse | None = None
    try:
        connection.putrequest("GET", target.request_target, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", target.host_header)
        for name, value in SAFE_HEADERS.items():
            connection.putheader(name, value)
        connection.endheaders()
        transport_socket = connection.sock
        if transport_socket is None:
            raise OSError("connection was not established")
        transport_socket.settimeout(_remaining_seconds(deadline_ns))
        response = connection.getresponse()
        if not isinstance(response.status, int) or isinstance(response.status, bool):
            raise FetchFailure("RESPONSE_MALFORMED", "response status was invalid")
        headers = {
            "content-encoding": _header(response, "Content-Encoding") or "",
            "content-length": _header(response, "Content-Length") or "",
            "content-type": _header(response, "Content-Type") or "",
            "location": _header(response, "Location") or "",
        }
        body = b"" if response.status in {204, 301, 302, 303, 307, 308} else _read_body(transport_socket, response, deadline_ns)
        return _HopResponse(response.status, headers, body)
    finally:
        if response is not None:
            response.close()
        connection.close()


def _fetch_hop(target: CanonicalTarget, deadline_ns: int) -> _HopResponse:
    try:
        addresses = _resolve_all_public(target, deadline_ns)
    except TransportError as exc:
        raise FetchFailure(exc.code, "target resolution was rejected") from exc
    last_error: OSError | http.client.HTTPException | ssl.SSLError | None = None
    for address in addresses:
        _remaining_seconds(deadline_ns)
        try:
            return _request_once(target, address, deadline_ns)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
    if last_error is not None:
        raise FetchFailure("FETCH_FAILED", "all vetted connection attempts failed") from last_error
    raise FetchFailure("FETCH_FAILED", "no vetted connection attempts were available")


def _response_document(target: CanonicalTarget, response: _HopResponse) -> PageDocument | None:
    if response.status == 204:
        return _document(target.url, b"")
    if response.status in {401, 403, 407}:
        return _outcome(target.url, "denied", FetchFailure("TARGET_DENIED", "target denied anonymous access"))
    if response.status == 429:
        return _outcome(target.url, "blocked", FetchFailure("TARGET_BLOCKED", "target rate limited the request"))
    if not 200 <= response.status < 300:
        return _outcome(target.url, "failed", FetchFailure("FETCH_FAILED", "target returned an unsupported status"))
    if response.headers["content-encoding"].strip().lower() not in {"", "identity"}:
        return _outcome(target.url, "malformed", FetchFailure("RESPONSE_MALFORMED", "response encoding was unsupported"))
    media_type = response.headers["content-type"].split(";", 1)[0].strip().lower()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return _outcome(target.url, "malformed", FetchFailure("RESPONSE_MALFORMED", "response content type was unsupported"))
    try:
        return _document(target.url, response.body)
    except FetchFailure as exc:
        return _outcome(target.url, "malformed", exc)


def _fetch_target(target: CanonicalTarget, deadline_ns: int, request: FetchRequest | None = None) -> PageDocument:
    current = target
    redirects = 0
    try:
        _remaining_seconds(deadline_ns)
        while True:
            response = _fetch_hop(current, deadline_ns)
            if response.status not in {301, 302, 303, 307, 308}:
                document = _response_document(current, response)
                if document is None:
                    return _outcome(current.url, "failed", FetchFailure("FETCH_FAILED", "response was unavailable"))
                return document
            if redirects >= MAX_REDIRECTS or not response.headers["location"] or request is None:
                return _outcome(current.url, "denied", FetchFailure("REDIRECT_DENIED", "redirect target was rejected"))
            try:
                current = _canonicalize_target(urljoin(current.url, response.headers["location"]), request.allowed_hosts, request.allowed_paths)
            except TransportError:
                return _outcome(current.url, "denied", FetchFailure("REDIRECT_DENIED", "redirect target was rejected"))
            redirects += 1
    except FetchFailure as exc:
        if exc.code == "FETCH_TIMEOUT":
            status = "timeout"
        elif exc.code == "CONTENT_TRUNCATED":
            status = "truncated"
        elif exc.code == "RESPONSE_MALFORMED":
            status = "malformed"
        else:
            status = "failed"
        return _outcome(current.url, status, exc)


def _error_payload(document: PageDocument) -> JsonObject:
    if document.error is None:
        return {"code": "FETCH_FAILED", "message": "fetch failed", "url": document.url}
    return document.error


def _read_request() -> tuple[FetchRequest, int]:
    raw = sys.stdin.buffer.read(MAX_CHILD_INPUT_BYTES + 1)
    if len(raw) > MAX_CHILD_INPUT_BYTES:
        raise FetchFailure("INPUT_INVALID", "child request was too large")
    try:
        parsed = json_object(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FetchFailure("INPUT_INVALID", "child request was invalid") from exc
    if set(parsed) != {"version", "deadlineNs", "request"}:
        raise FetchFailure("INPUT_INVALID", "child request was invalid")
    version = parsed["version"]
    deadline_ns = parsed["deadlineNs"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1 or not isinstance(deadline_ns, int) or isinstance(deadline_ns, bool):
        raise FetchFailure("INPUT_INVALID", "child request was invalid")
    try:
        request = FetchRequest.from_json(json_object(parsed["request"]))
    except ValueError as exc:
        raise FetchFailure("INPUT_INVALID", "child request was invalid") from exc
    return request, deadline_ns


def _child_response() -> JsonObject:
    try:
        request, deadline_ns = _read_request()
        target = _canonicalize_target(request.url, request.allowed_hosts, request.allowed_paths)
        document = _fetch_target(target, deadline_ns, request)
        if document.status in {"completed", "empty"}:
            return {"version": 1, "ok": True, "document": document.to_json()}
        return {"version": 1, "ok": False, "error": _error_payload(document)}
    except TransportError as exc:
        return {"version": 1, "ok": False, "error": {"code": exc.code, "message": "target was rejected", "url": None}}
    except FetchFailure as exc:
        return {"version": 1, "ok": False, "error": {"code": exc.code, "message": exc.message, "url": None}}
    except Exception:
        return {"version": 1, "ok": False, "error": {"code": "FETCH_FAILED", "message": "fetch failed", "url": None}}


def main() -> int:
    sys.stdout.write(json.dumps(_child_response(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
