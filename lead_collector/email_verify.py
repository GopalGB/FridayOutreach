"""Bounded adapter for the optional local Reacher SMTP-only runner."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import tempfile
import time
from typing import Literal

from .model import JsonObject, json_object


MAX_OUTPUT_BYTES = 32 * 1024
VERIFY_DEADLINE_SECONDS = 30
_STATUS_VALUES = frozenset({"safe", "risky", "invalid", "unknown"})
_RESPONSE_KEYS = frozenset({"version", "input", "status", "syntaxValid", "mxAcceptsMail", "smtpDeliverable", "catchAll", "disabled", "fullInbox"})
_FAILURE_CATEGORY_KEY = "failureCategory"
_FAILURE_CATEGORIES = frozenset({"mx", "smtp"})
_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")


class _OutputLimitExceeded(Exception):
    """Raised before an untrusted child can fill the parent memory."""


@dataclass(frozen=True)
class EmailVerifyRequest:
    email: str
    binary: Path | None = None


@dataclass(frozen=True)
class VerificationDetails:
    syntax_valid: bool
    mx_accepts_mail: bool
    smtp_deliverable: bool
    catch_all: bool
    disabled: bool
    full_inbox: bool

    def to_json(self) -> JsonObject:
        return {"syntaxValid": self.syntax_valid, "mxAcceptsMail": self.mx_accepts_mail, "smtpDeliverable": self.smtp_deliverable, "catchAll": self.catch_all, "disabled": self.disabled, "fullInbox": self.full_inbox}


@dataclass(frozen=True)
class VerificationError:
    code: str
    message: str

    def to_json(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class VerificationResult:
    email: str
    status: Literal["safe", "risky", "invalid", "unknown"]
    mailbox_verified: bool
    details: VerificationDetails
    error: VerificationError | None

    def to_json(self) -> JsonObject:
        return {"email": self.email, "status": self.status, "mailboxVerified": self.mailbox_verified, "details": self.details.to_json(), "error": None if self.error is None else self.error.to_json()}


def verify_email(request: EmailVerifyRequest) -> VerificationResult:
    """Run only the explicitly supplied local SMTP verifier."""

    if not _valid_email(request.email):
        return _unknown(request.email, "INPUT_INVALID", "email address is invalid")
    binary = _trusted_binary(request.binary or _default_binary())
    if binary is None:
        return _unknown(request.email, "CAPABILITY_UNAVAILABLE", "SMTP verifier runtime is unavailable")
    return _run_binary(request.email, binary)


def _valid_email(value: str) -> bool:
    return bool(_EMAIL_PATTERN.fullmatch(value)) and value == value.strip()


def _default_binary() -> Path:
    return Path(__file__).resolve().parents[1] / ".tools" / "reacher-smtp-check"


def _trusted_binary(path: Path) -> Path | None:
    selected = path if path.is_absolute() else Path.cwd() / path
    try:
        metadata = selected.lstat()
    except OSError:
        return None
    unsafe_mode = stat.S_IWGRP | stat.S_IWOTH
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        return None
    if metadata.st_mode & unsafe_mode or not os.access(selected, os.X_OK):
        return None
    return selected


def _run_binary(email: str, binary: Path) -> VerificationResult:
    deadline = time.monotonic() + VERIFY_DEADLINE_SECONDS
    with tempfile.TemporaryDirectory(prefix="lead-collector-reacher-") as home:
        try:
            process = _open_binary(binary, email, home)
        except OSError:
            return _unknown(email, "CAPABILITY_UNAVAILABLE", "SMTP verifier runtime is unavailable")
        try:
            output = _read_output(process, deadline)
            returncode = _wait_for_exit(process, deadline)
        except _OutputLimitExceeded:
            _stop_child(process)
            return _unknown(email, "VERIFY_OUTPUT_INVALID", "native verifier response was invalid")
        except TimeoutError:
            _stop_child(process)
            return _unknown(email, "VERIFY_TIMEOUT", "native verifier exceeded its deadline")
        except (OSError, subprocess.SubprocessError):
            _stop_child(process)
            return _unknown(email, "VERIFY_EXECUTION_FAILED", "native verifier failed")
        finally:
            _close_stdout(process)
    if returncode != 0:
        return _unknown(email, "VERIFY_EXECUTION_FAILED", "native verifier failed")
    return _parse_result(output, email)


def _open_binary(binary: Path, email: str, home: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(binary), "--email", email],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=home,
        env=_child_environment(home),
        shell=False,
        start_new_session=True,
    )


def _child_environment(home: str) -> dict[str, str]:
    return {"HOME": home, "TMPDIR": home, "PATH": os.defpath, "LANG": "C", "NO_COLOR": "1"}


def _read_output(process: subprocess.Popen[bytes], deadline: float) -> bytes:
    if process.stdout is None:
        raise OSError("missing stdout pipe")
    output = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native verifier deadline exceeded")
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            continue
        chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT_BYTES + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > MAX_OUTPUT_BYTES:
            raise _OutputLimitExceeded


def _wait_for_exit(process: subprocess.Popen[bytes], deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("native verifier deadline exceeded")
    try:
        return process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("native verifier deadline exceeded") from exc


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    _signal_child(process, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_child(process, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _signal_child(process: subprocess.Popen[bytes], signal_code: int) -> None:
    try:
        os.killpg(process.pid, signal_code)
    except OSError:
        pass


def _close_stdout(process: subprocess.Popen[bytes]) -> None:
    if process.stdout is not None:
        process.stdout.close()


def _parse_result(output: object, email: str) -> VerificationResult:
    try:
        body = _response_object(output)
        status = _response_status(body, email)
        details = _response_details(body)
        error = _response_error(body, status)
        if status == "safe" and not _verified_details(details):
            raise ValueError("contradictory safe result")
    except (KeyError, RecursionError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _unknown(email, "VERIFY_OUTPUT_INVALID", "native verifier response was invalid")
    return VerificationResult(email, status, status == "safe", details, error)


def _response_object(output: object) -> JsonObject:
    if not isinstance(output, bytes) or not output or len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("invalid native response")
    return json_object(json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _response_status(body: JsonObject, email: str) -> Literal["safe", "risky", "invalid", "unknown"]:
    if not _RESPONSE_KEYS <= set(body) or set(body) - (_RESPONSE_KEYS | {_FAILURE_CATEGORY_KEY}):
        raise ValueError("invalid native response")
    version = body["version"]
    input_value = body["input"]
    status = body["status"]
    if type(version) is not int or version != 1 or input_value != email:
        raise ValueError("invalid native response")
    if not isinstance(status, str) or status not in _STATUS_VALUES:
        raise ValueError("invalid native response")
    if status == "safe":
        return "safe"
    if status == "risky":
        return "risky"
    if status == "invalid":
        return "invalid"
    return "unknown"


def _response_error(body: JsonObject, status: str) -> VerificationError | None:
    if _FAILURE_CATEGORY_KEY not in body:
        return None
    category = body[_FAILURE_CATEGORY_KEY]
    if status != "unknown" or not isinstance(category, str) or category not in _FAILURE_CATEGORIES:
        raise ValueError("invalid native failure category")
    if category == "mx":
        return VerificationError("VERIFY_MX_FAILED", "native MX resolution failed")
    return VerificationError("VERIFY_SMTP_FAILED", "native SMTP verification failed")


def _response_details(body: JsonObject) -> VerificationDetails:
    return VerificationDetails(
        _response_bool(body, "syntaxValid"),
        _response_bool(body, "mxAcceptsMail"),
        _response_bool(body, "smtpDeliverable"),
        _response_bool(body, "catchAll"),
        _response_bool(body, "disabled"),
        _response_bool(body, "fullInbox"),
    )


def _response_bool(body: JsonObject, key: str) -> bool:
    value = body[key]
    if type(value) is not bool:
        raise ValueError("invalid native booleans")
    return value


def _verified_details(details: VerificationDetails) -> bool:
    return details.syntax_valid and details.mx_accepts_mail and details.smtp_deliverable and not details.catch_all and not details.disabled and not details.full_inbox


def _unknown(email: str, code: str, message: str) -> VerificationResult:
    return VerificationResult(email, "unknown", False, VerificationDetails(False, False, False, False, False, False), VerificationError(code, message))
