"""Conservative URL canonicalization and DNS address vetting for A2 transport."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
from typing import Literal, NoReturn, Sequence, TypeGuard
from urllib.parse import quote, unquote, urlsplit

from .model import FetchRequest, JsonObject, PageDocument, json_object, observed_now


class TransportError(ValueError):
    """A sanitized, stable transport-policy failure."""

    def __init__(self, code: Literal["INPUT_UNSAFE", "DNS_UNSAFE"], message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CanonicalTarget:
    url: str
    scheme: Literal["http", "https"]
    hostname: str
    port: int
    request_target: str
    host_header: str
    path_segments: tuple[str, ...]


@dataclass(frozen=True)
class VettedAddress:
    family: int
    sockaddr: tuple[str, int] | tuple[str, int, int, int]
    printable_ip: str


_CONSERVATIVE_NETWORKS = tuple(
    ipaddress.ip_network(item)
    for item in ("192.0.0.0/24", "64:ff9b::/96", "64:ff9b:1::/48", "2001::/23", "2002::/16")
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
FETCH_DEADLINE_NS = 15 * 1_000_000_000
MAX_CHILD_OUTPUT_BYTES = 12 * 1024 * 1024
CHILD_GRACE_SECONDS = 0.2
_PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def _reject(message: str) -> NoReturn:
    raise TransportError("INPUT_UNSAFE", message)


def _dns_reject(message: str) -> NoReturn:
    raise TransportError("DNS_UNSAFE", message)


def _contains_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _is_non_boolean_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _normal_host(value: object) -> str:
    if not isinstance(value, str):
        _reject("target hostname is invalid")
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeEncodeError):
        _reject("target hostname is invalid")
    if not host or _contains_controls(host) or any(not (char.isascii() and (char.isalnum() or char in ".-:")) for char in host):
        _reject("target hostname is invalid")
    return host


def _is_local_only_host(hostname: str) -> bool:
    return "." not in hostname or hostname == "localhost" or hostname.endswith((".localhost", ".local", ".localdomain"))


def _is_numeric_host(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            socket.inet_aton(hostname)
        except OSError:
            return False
    return True


def canonical_allowed_hosts(values: Sequence[str]) -> tuple[str, ...]:
    """Normalize every approved host and reject local/literal browser escape hatches."""

    normalized: list[str] = []
    for value in values:
        host = _normal_host(value)
        if _is_numeric_host(host) or _is_local_only_host(host):
            _reject("allowed host is unsafe")
        if host not in normalized:
            normalized.append(host)
    if not normalized:
        _reject("allowed host is invalid")
    return tuple(normalized)


def _validate_percent_escapes(value: str, label: str) -> None:
    for index, char in enumerate(value):
        if char == "%" and (index + 2 >= len(value) or value[index + 1] not in _HEX_DIGITS or value[index + 2] not in _HEX_DIGITS):
            _reject(f"{label} is invalid")


def _decode_component(value: object, label: str) -> str:
    if not isinstance(value, str):
        _reject(f"{label} is invalid")
    _validate_percent_escapes(value, label)
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject(f"{label} is invalid")
    if "%" in decoded or "\\" in decoded or _contains_controls(decoded):
        _reject(f"{label} is unsafe")
    return decoded


def _canonical_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        _reject(f"{label} is invalid")
    if any(token in value.lower() for token in ("%2f", "%5c", "%2e")):
        _reject(f"{label} is ambiguous")
    path = _decode_component(value, label)
    if any(segment in {".", ".."} for segment in path.split("/")):
        _reject(f"{label} is unsafe")
    return path


def _scope_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _request_target(path: str, query: str, has_query: bool) -> str:
    request_path = quote(path, safe="/!$&'()*+,;=:@-._~")
    request_query = quote(query, safe="!$&'()*+,;=:/?@-._~")
    return request_path + (f"?{request_query}" if has_query else "")


def _canonicalize_target(url: str, allowed_hosts: Sequence[str], allowed_paths: Sequence[str]) -> CanonicalTarget:
    if not isinstance(url, str) or _contains_controls(url):
        _reject("target URL is invalid")
    if "\\" in url or "#" in url:
        _reject("target URL is unsafe")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, UnicodeError):
        _reject("target URL is invalid")
    scheme_text = parsed.scheme.lower()
    if scheme_text not in {"http", "https"} or not parsed.netloc or "%" in parsed.netloc:
        _reject("target URL is unsafe")
    hostname_raw = parsed.hostname
    if hostname_raw is None or parsed.username is not None or parsed.password is not None:
        _reject("target URL is unsafe")
    default_port = 443 if scheme_text == "https" else 80
    if port is None:
        port = default_port
    if port != default_port:
        _reject("target port is invalid")
    hostname = _normal_host(hostname_raw)
    if _is_numeric_host(hostname):
        _reject("literal target address is unsafe")
    allowed = set(canonical_allowed_hosts(allowed_hosts))
    if hostname not in allowed:
        _reject("target host is not allowed")
    path = _canonical_path(parsed.path or "/", "target path")
    query = _decode_component(parsed.query, "target query")
    segments = tuple(path.split("/"))
    scoped_path = _scope_path(path)
    normalized_allowed = tuple(_scope_path(_canonical_path(item, "allowed path")) for item in allowed_paths)
    if not any(scoped_path == prefix or (prefix != "/" and scoped_path.startswith(prefix + "/")) or prefix == "/" for prefix in normalized_allowed):
        _reject("target path is not allowed")
    authority = f"[{hostname}]" if ":" in hostname else hostname
    host_header = f"{authority}:{port}" if (scheme_text == "http" and port != 80) or (scheme_text == "https" and port != 443) else authority
    request_target = _request_target(path, query, bool(parsed.query))
    scheme: Literal["http", "https"] = "https" if scheme_text == "https" else "http"
    return CanonicalTarget(f"{scheme}://{host_header}{request_target}", scheme, hostname, port, request_target, host_header, segments)


def _safe_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if value.version == 6 and value.ipv4_mapped is not None:
        return _safe_ip(value.ipv4_mapped)
    if value.is_private or value.is_loopback or value.is_link_local or value.is_multicast or value.is_reserved or value.is_unspecified or not value.is_global:
        return False
    if value.version == 6 and value.is_site_local:
        return False
    return not any(value in network for network in _CONSERVATIVE_NETWORKS)


def _parse_public_address(host: str, expected_version: int) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise TransportError("DNS_UNSAFE", "DNS returned an invalid address") from exc
    if address.version != expected_version or not _safe_ip(address):
        _dns_reject("DNS returned an unsafe address")
    return address


def _vetted_ipv4(sockaddr: object, target: CanonicalTarget) -> VettedAddress:
    if not isinstance(sockaddr, tuple) or len(sockaddr) != 2:
        _dns_reject("DNS returned an invalid address")
    host, port = sockaddr
    if not isinstance(host, str) or not _is_non_boolean_int(port) or port != target.port:
        _dns_reject("DNS returned an invalid address")
    address = _parse_public_address(host, 4)
    return VettedAddress(socket.AF_INET, (str(address), port), str(address))


def _vetted_ipv6(sockaddr: object, target: CanonicalTarget) -> VettedAddress:
    if not isinstance(sockaddr, tuple) or len(sockaddr) != 4:
        _dns_reject("DNS returned an invalid address")
    host, port, flowinfo, scopeid = sockaddr
    if not isinstance(host, str) or not all(_is_non_boolean_int(value) for value in (port, flowinfo, scopeid)):
        _dns_reject("DNS returned an invalid address")
    if port != target.port or not 0 <= flowinfo <= 0xFFFFF or not 0 <= scopeid <= 0xFFFFFFFF:
        _dns_reject("DNS returned an invalid address")
    address = _parse_public_address(host, 6)
    return VettedAddress(socket.AF_INET6, (str(address), port, flowinfo, scopeid), str(address))


def _vetted_answer(answer: object, target: CanonicalTarget) -> VettedAddress:
    if not isinstance(answer, tuple) or len(answer) != 5:
        _dns_reject("DNS returned an invalid address")
    family, socktype, protocol, _canonname, sockaddr = answer
    if not all(_is_non_boolean_int(value) for value in (family, socktype, protocol)):
        _dns_reject("DNS returned an invalid address")
    if socktype != socket.SOCK_STREAM or protocol not in {0, socket.IPPROTO_TCP}:
        _dns_reject("DNS returned an invalid address")
    if family == socket.AF_INET:
        return _vetted_ipv4(sockaddr, target)
    if family == socket.AF_INET6:
        return _vetted_ipv6(sockaddr, target)
    _dns_reject("DNS returned an invalid address")


def _resolve_all_public(target: CanonicalTarget, deadline_ns: int) -> tuple[VettedAddress, ...]:
    if time.monotonic_ns() >= deadline_ns:
        _dns_reject("DNS resolution timed out")
    try:
        answers = socket.getaddrinfo(target.hostname, target.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TransportError("DNS_UNSAFE", "DNS resolution failed") from exc
    if time.monotonic_ns() >= deadline_ns:
        _dns_reject("DNS resolution timed out")
    if not answers:
        _dns_reject("DNS returned no addresses")
    vetted: dict[tuple[int, str, tuple[str, int] | tuple[str, int, int, int]], VettedAddress] = {}
    for answer in answers:
        item = _vetted_answer(answer, target)
        vetted[(item.family, item.printable_ip, item.sockaddr)] = item
    return tuple(sorted(vetted.values(), key=lambda item: (item.printable_ip, item.family, item.sockaddr)))


def _error(url: str | None, code: str, message: str) -> JsonObject:
    return {"code": code, "message": message, "url": url}


def _fetch_error(status: str, error: JsonObject) -> PageDocument:
    url = error["url"]
    return PageDocument(url if isinstance(url, str) else "", "", observed_now(), hashlib.sha256(b"").hexdigest(), status, error)


def _validate_fetch_request(request: object) -> FetchRequest:
    if not isinstance(request, FetchRequest):
        raise ValueError("invalid fetch request")
    return FetchRequest.from_json(request.to_json())


def validate_fetch_request(request: FetchRequest) -> FetchRequest:
    """Return a canonical static request or raise a sanitized policy error."""

    checked = _validate_fetch_request(request)
    allowed_hosts = canonical_allowed_hosts(checked.allowed_hosts)
    target = _canonicalize_target(checked.url, allowed_hosts, checked.allowed_paths)
    return FetchRequest(target.url, allowed_hosts, checked.allowed_paths, checked.limits)


def _remaining_seconds(deadline_ns: int) -> float:
    remaining_ns = deadline_ns - time.monotonic_ns()
    if remaining_ns <= 0:
        raise TimeoutError
    return remaining_ns / 1_000_000_000


def _child_environment() -> dict[str, str]:
    return {"HOME": str(_PRODUCT_ROOT), "NO_COLOR": "1", "PATH": os.defpath}


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        return
    try:
        process.communicate(timeout=CHILD_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return
        process.communicate()


def _status_for_code(code: str) -> str:
    if code == "FETCH_TIMEOUT":
        return "timeout"
    if code == "CONTENT_TRUNCATED":
        return "truncated"
    if code == "TARGET_DENIED" or code == "REDIRECT_DENIED":
        return "denied"
    if code == "TARGET_BLOCKED":
        return "blocked"
    if code == "RESPONSE_MALFORMED":
        return "malformed"
    if code == "CAPABILITY_UNAVAILABLE":
        return "unavailable"
    return "failed"


def _parse_child_error(value: object, target: CanonicalTarget) -> PageDocument:
    try:
        error = json_object(value)
        if set(error) != {"code", "message", "url"}:
            raise ValueError("invalid child error")
        code = error["code"]
        message = error["message"]
        url = error["url"]
        if not isinstance(code, str) or not isinstance(message, str) or (url is not None and not isinstance(url, str)):
            raise ValueError("invalid child error")
    except ValueError:
        return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
    return _fetch_error(_status_for_code(code), _error(target.url, code, message))


def _parse_child_result(stdout: object, target: CanonicalTarget, request: FetchRequest) -> PageDocument:
    if not isinstance(stdout, bytes) or len(stdout) > MAX_CHILD_OUTPUT_BYTES:
        return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
    try:
        parsed = json_object(json.loads(stdout.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
    version = parsed.get("version")
    ok = parsed.get("ok")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1 or not isinstance(ok, bool):
        return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
    if ok:
        if set(parsed) != {"version", "ok", "document"}:
            return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
        try:
            document = PageDocument.from_json(json_object(parsed["document"]))
        except ValueError:
            return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
        try:
            final_target = _canonicalize_target(document.url, request.allowed_hosts, request.allowed_paths)
        except TransportError:
            return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
        return replace(document, url=final_target.url)
    if set(parsed) != {"version", "ok", "error"}:
        return _fetch_error("failed", _error(target.url, "RESPONSE_MALFORMED", "child response was invalid"))
    return _parse_child_error(parsed["error"], target)


def fetch_page(request: FetchRequest) -> PageDocument:
    try:
        checked = validate_fetch_request(request)
        target = _canonicalize_target(checked.url, checked.allowed_hosts, checked.allowed_paths)
    except (TransportError, ValueError):
        return _fetch_error("denied", _error(None, "TARGET_DENIED", "target was rejected"))
    deadline_ns = time.monotonic_ns() + FETCH_DEADLINE_NS
    payload = json.dumps({"version": 1, "deadlineNs": deadline_ns, "request": checked.to_json()}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        process = subprocess.Popen(
            [str(Path(sys.executable).resolve()), "-E", "-s", "-m", "lead_collector.fetch_child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=_PRODUCT_ROOT,
            env=_child_environment(),
            shell=False,
            start_new_session=True,
        )
        stdout, _stderr = process.communicate(input=payload, timeout=_remaining_seconds(deadline_ns))
    except TimeoutError:
        _stop_child(process)
        return _fetch_error("timeout", _error(target.url, "FETCH_TIMEOUT", "fetch deadline expired"))
    except subprocess.TimeoutExpired:
        _stop_child(process)
        return _fetch_error("timeout", _error(target.url, "FETCH_TIMEOUT", "fetch deadline expired"))
    except OSError:
        return _fetch_error("failed", _error(target.url, "FETCH_FAILED", "fetch child was unavailable"))
    if process.returncode not in {0, None}:
        return _fetch_error("failed", _error(target.url, "FETCH_FAILED", "fetch child failed"))
    return _parse_child_result(stdout, target, checked)
