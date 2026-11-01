"""Safe CSV path handling and same-input replacement bindings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from .paths import is_protected_path


MAX_INPUT_BYTES = 8 * 1024 * 1024


class BatchSafetyError(ValueError):
    """A sanitized batch path or binding failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InputBinding:
    path: Path
    device: int
    inode: int
    sha256: str


def _absolute_csv(path: Path, label: str) -> Path:
    if path.suffix.casefold() != ".csv" or ".." in path.parts or is_protected_path(str(path)):
        raise BatchSafetyError("INPUT_UNSAFE", f"unsafe {label} path")
    absolute = path.absolute()
    if is_protected_path(str(absolute)):
        raise BatchSafetyError("INPUT_UNSAFE", f"unsafe {label} path")
    return absolute


def _safe_parent(path: Path, label: str) -> None:
    current = Path(path.anchor)
    try:
        for part in path.parts[1:-1]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BatchSafetyError("INPUT_UNSAFE", f"unsafe {label} path")
    except FileNotFoundError as exc:
        raise BatchSafetyError("INPUT_INVALID", f"{label} is unavailable") from exc


def _regular(path: Path, label: str, required: bool) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise BatchSafetyError("INPUT_INVALID", f"{label} is unavailable") from None
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise BatchSafetyError("INPUT_UNSAFE", f"unsafe {label} path")
    return metadata


def validate_output(path: Path, overwrite: bool) -> Path:
    """Reject unsafe/colliding output paths without creating any file."""

    absolute = _absolute_csv(path, "output")
    _safe_parent(absolute, "output")
    if _regular(absolute, "output", required=False) is not None and not overwrite:
        raise BatchSafetyError("OUTPUT_EXISTS", "output already exists")
    return absolute


def read_input(path: Path) -> tuple[bytes, InputBinding]:
    """Read a bounded regular CSV through a no-follow descriptor."""

    absolute = _absolute_csv(path, "input")
    _safe_parent(absolute, "input")
    metadata = _regular(absolute, "input", required=True)
    assert metadata is not None
    if metadata.st_size > MAX_INPUT_BYTES:
        raise BatchSafetyError("INPUT_INVALID", "input exceeds 8MiB")
    descriptor = _open_input(absolute)
    try:
        actual = os.fstat(descriptor)
        _validate_descriptor(actual)
        raw = _read_bounded(descriptor)
    finally:
        os.close(descriptor)
    binding = InputBinding(absolute, actual.st_dev, actual.st_ino, hashlib.sha256(raw).hexdigest())
    return raw, binding


def _open_input(path: Path) -> int:
    try:
        nofollow = os.O_NOFOLLOW
    except AttributeError:
        raise BatchSafetyError("CAPABILITY_UNAVAILABLE", "safe CSV input is unavailable")
    try:
        return os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise BatchSafetyError("INPUT_UNSAFE", "unsafe input path") from exc


def _validate_descriptor(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise BatchSafetyError("INPUT_UNSAFE", "unsafe input path")


def _read_bounded(descriptor: int) -> bytes:
    raw = bytearray()
    while len(raw) <= MAX_INPUT_BYTES:
        chunk = os.read(descriptor, min(64 * 1024, MAX_INPUT_BYTES + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > MAX_INPUT_BYTES:
        raise BatchSafetyError("INPUT_INVALID", "input exceeds 8MiB")
    return bytes(raw)


def assert_unchanged(binding: InputBinding) -> None:
    """Refuse same-file replacement after an intervening user edit."""

    try:
        raw, current = read_input(binding.path)
    except BatchSafetyError as exc:
        raise BatchSafetyError("INPUT_CHANGED", "input changed during enrichment") from exc
    if current.device != binding.device or current.inode != binding.inode or current.sha256 != binding.sha256:
        raise BatchSafetyError("INPUT_CHANGED", "input changed during enrichment")
    if hashlib.sha256(raw).hexdigest() != binding.sha256:
        raise BatchSafetyError("INPUT_CHANGED", "input changed during enrichment")


def matches_binding(path: Path, binding: InputBinding) -> bool:
    """Return whether an existing path names the already-opened input file."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return metadata.st_dev == binding.device and metadata.st_ino == binding.inode
