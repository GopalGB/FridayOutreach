"""Validated SQLite checkpoint persistence for Task 2."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterator

from .model import JobSnapshot, json_object
from .paths import is_protected_path


APPLICATION_ID = 0x4C444331
SCHEMA_VERSION = 1
SCHEMA_SQL = "CREATE TABLE jobs (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)"
MANAGED_STATE_ROOT = Path.home() / ".local" / "share" / "lead-collector"
_LEASE_DIRECTORY = ".lead-collector-locks"
_LEASE_NAME = re.compile(r"[0-9a-f]{64}\.lock")


def managed_state_path(path: Path) -> Path:
    """Return the sole permitted managed-state leaf without touching its target."""

    root = MANAGED_STATE_ROOT.expanduser().absolute()
    candidate = Path(path)
    if candidate == Path(":memory:"):
        raise StoreUnsafe("unsafe state path")
    if candidate.is_absolute():
        if candidate.parent != root:
            raise StoreUnsafe("state path is outside managed root")
        leaf = candidate
    elif len(candidate.parts) == 1 and candidate.name not in {"", ".", ".."}:
        leaf = root / candidate.name
    else:
        raise StoreUnsafe("state path is outside managed root")
    if is_protected_path(leaf.name):
        raise StoreUnsafe("unsafe state path")
    return leaf


@dataclass(frozen=True)
class _StateBinding:
    root: Path
    leaf: Path
    root_identity: tuple[int, int]
    leaf_identity: tuple[int, int]
    created: bool


class _ManagedConnection(sqlite3.Connection):
    binding: _StateBinding


def _trusted_directory(root: Path, *, create: bool) -> int:
    """Open every state-root ancestor without following a symlink."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow == 0 or directory == 0:
        raise StoreUnsafe("safe state storage is unavailable")
    absolute = root.absolute()
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, os.O_RDONLY | directory | nofollow)
        anchor_metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(anchor_metadata.st_mode) or anchor_metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(anchor_metadata.st_mode) & 0o022:
            raise StoreUnsafe("unsafe managed root")
        for index, component in enumerate(absolute.parts[1:]):
            try:
                next_descriptor = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            is_root = index == len(absolute.parts) - 2
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()} or mode & 0o022 or (is_root and (metadata.st_uid != os.geteuid() or mode != 0o700)):
                raise StoreUnsafe("unsafe managed root")
        if descriptor is None:
            raise StoreUnsafe("unsafe managed root")
        return descriptor
    except (OSError, StoreUnsafe) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, StoreUnsafe):
            raise
        raise StoreUnsafe("unsafe managed root") from exc


def _valid_leaf(metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and metadata.st_nlink == 1 and not stat.S_IMODE(metadata.st_mode) & 0o022


def _prepare_state(leaf: Path) -> _StateBinding:
    root = MANAGED_STATE_ROOT.expanduser().absolute()
    descriptor = _trusted_directory(root, create=True)
    state_descriptor: int | None = None
    created = False
    try:
        try:
            metadata = os.lstat(leaf)
        except FileNotFoundError:
            state_descriptor = os.open(leaf.name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            created = True
            metadata = os.fstat(state_descriptor)
        if not _valid_leaf(metadata) or (created and stat.S_IMODE(metadata.st_mode) != 0o600):
            raise StoreUnsafe("unsafe state path")
        root_metadata = os.fstat(descriptor)
        return _StateBinding(root, leaf, (root_metadata.st_dev, root_metadata.st_ino), (metadata.st_dev, metadata.st_ino), created)
    except (OSError, StoreUnsafe) as exc:
        if isinstance(exc, StoreUnsafe):
            raise
        raise StoreUnsafe("unsafe state path") from exc
    finally:
        if state_descriptor is not None:
            os.close(state_descriptor)
        os.close(descriptor)


def _validate_binding(binding: _StateBinding) -> None:
    descriptor = _trusted_directory(binding.root, create=False)
    try:
        root_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        leaf_metadata = os.lstat(binding.leaf)
    except OSError as exc:
        raise StoreUnsafe("unsafe state path") from exc
    if (root_metadata.st_dev, root_metadata.st_ino) != binding.root_identity or (leaf_metadata.st_dev, leaf_metadata.st_ino) != binding.leaf_identity or not _valid_leaf(leaf_metadata):
        raise StoreUnsafe("unsafe state path")


def _validate_connection(conn: sqlite3.Connection) -> None:
    if isinstance(conn, _ManagedConnection):
        _validate_binding(conn.binding)


def _validate_existing_database(binding: _StateBinding) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(binding.leaf.as_uri() + "?mode=ro", uri=True)
        _validate_binding(binding)
        create_store(connection, initialize=False)
        _validate_binding(binding)
    except (OSError, sqlite3.DatabaseError, StoreUnsafe) as exc:
        if isinstance(exc, StoreUnsafe):
            raise
        raise StoreUnsafe("unrecognized state database") from exc
    finally:
        if connection is not None:
            connection.close()


class StoreConflict(Exception):
    """Raised when a transactional compare-and-swap observes a new revision."""


class StoreUnsafe(Exception):
    """Raised before an unknown state file is ever modified."""


class StoreWriteFailed(Exception):
    """Raised after rolling back a failed SQLite write."""


@dataclass(frozen=True)
class _LeaseBinding:
    root_descriptor: int
    directory_descriptor: int
    leaf_descriptor: int
    name: str


def _valid_lease_directory(metadata: os.stat_result) -> bool:
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o700


def _valid_lease_leaf(metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and metadata.st_nlink == 1 and stat.S_IMODE(metadata.st_mode) == 0o600


def _close_descriptor(descriptor: int | None) -> None:
    if descriptor is not None:
        try:
            os.close(descriptor)
        except OSError:
            return


def _close_lease_descriptors(root: int | None, directory: int | None, leaf: int | None) -> None:
    _close_descriptor(leaf)
    _close_descriptor(directory)
    _close_descriptor(root)


def _lease_root(connection: sqlite3.Connection) -> int:
    if not isinstance(connection, _ManagedConnection):
        raise StoreUnsafe("unsafe lease storage")
    _validate_connection(connection)
    descriptor = _trusted_directory(connection.binding.root, create=False)
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        _close_descriptor(descriptor)
        raise StoreUnsafe("unsafe lease storage") from exc
    if (metadata.st_dev, metadata.st_ino) == connection.binding.root_identity:
        return descriptor
    _close_descriptor(descriptor)
    raise StoreUnsafe("unsafe lease storage")


def _open_lease_directory(root: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(_LEASE_DIRECTORY, flags, dir_fd=root)
    except FileNotFoundError:
        try:
            os.mkdir(_LEASE_DIRECTORY, 0o700, dir_fd=root)
        except FileExistsError:
            pass
        return os.open(_LEASE_DIRECTORY, flags, dir_fd=root)


def _lease_directory_matches(root: int, directory: int) -> bool:
    try:
        entry = os.stat(_LEASE_DIRECTORY, dir_fd=root, follow_symlinks=False)
        opened = os.fstat(directory)
    except OSError:
        return False
    return _valid_lease_directory(entry) and (entry.st_dev, entry.st_ino) == (opened.st_dev, opened.st_ino)


def _open_lease_leaf(directory: int, name: str) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    if not _valid_lease_leaf(existing):
        raise StoreUnsafe("unsafe lease storage")
    return os.open(name, flags, dir_fd=directory)


def _lease_leaf_matches(directory: int, leaf: int, name: str) -> bool:
    try:
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        opened = os.fstat(leaf)
    except OSError:
        return False
    return _valid_lease_leaf(entry) and (entry.st_dev, entry.st_ino) == (opened.st_dev, opened.st_ino)


def _lease_descriptors(connection: sqlite3.Connection, name: str) -> _LeaseBinding:
    if _LEASE_NAME.fullmatch(name) is None:
        raise StoreUnsafe("unsafe lease storage")
    root = directory = leaf = None
    try:
        root = _lease_root(connection)
        directory = _open_lease_directory(root)
        if not _lease_directory_matches(root, directory):
            raise StoreUnsafe("unsafe lease storage")
        leaf = _open_lease_leaf(directory, name)
        if not _lease_leaf_matches(directory, leaf, name):
            raise StoreUnsafe("unsafe lease storage")
        _validate_connection(connection)
        return _LeaseBinding(root, directory, leaf, name)
    except (OSError, StoreUnsafe) as exc:
        _close_lease_descriptors(root, directory, leaf)
        if isinstance(exc, StoreUnsafe):
            raise
        raise StoreUnsafe("unsafe lease storage") from exc


@contextmanager
def managed_lease(connection: sqlite3.Connection, name: str) -> Iterator[None]:
    """Hold a descriptor-bound, owner-private lease for one managed job."""

    binding = _lease_descriptors(connection, name)
    locked = False
    try:
        try:
            fcntl.flock(binding.leaf_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise StoreConflict(name) from exc
        except OSError as exc:
            raise StoreUnsafe("unsafe lease storage") from exc
        if not _lease_directory_matches(binding.root_descriptor, binding.directory_descriptor) or not _lease_leaf_matches(binding.directory_descriptor, binding.leaf_descriptor, binding.name):
            raise StoreUnsafe("unsafe lease storage")
        _validate_connection(connection)
        yield
    finally:
        if locked:
            try:
                fcntl.flock(binding.leaf_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        _close_lease_descriptors(binding.root_descriptor, binding.directory_descriptor, binding.leaf_descriptor)


def _schema_matches(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    if rows != [("jobs",)]:
        return False
    columns = conn.execute("PRAGMA table_info(jobs)").fetchall()
    return [(row[1], row[2], row[3], row[5]) for row in columns] == [("id", "TEXT", 0, 1), ("revision", "INTEGER", 1, 0), ("payload", "TEXT", 1, 0)]


def create_store(conn: sqlite3.Connection, *, initialize: bool = True) -> None:
    application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if application_id == APPLICATION_ID and version == SCHEMA_VERSION and _schema_matches(conn):
        return
    empty = application_id == 0 and version == 0 and not conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchone()
    if not initialize or not empty:
        raise StoreUnsafe("unrecognized state database")
    conn.execute("PRAGMA application_id=0x4C444331")
    conn.execute("PRAGMA user_version=1")
    conn.execute(SCHEMA_SQL)
    conn.commit()


def open_store(path: Path) -> sqlite3.Connection:
    leaf = managed_state_path(path)
    binding = _prepare_state(leaf)
    if not binding.created:
        _validate_existing_database(binding)
    uri = binding.leaf.as_uri() + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, factory=_ManagedConnection)
    except (OSError, sqlite3.DatabaseError) as exc:
        raise StoreUnsafe("unrecognized state database") from exc
    assert isinstance(conn, _ManagedConnection)
    conn.binding = binding
    try:
        _validate_connection(conn)
        create_store(conn, initialize=binding.created)
        _validate_connection(conn)
    except (OSError, sqlite3.DatabaseError, StoreUnsafe) as exc:
        conn.close()
        if isinstance(exc, StoreUnsafe):
            raise
        raise StoreUnsafe("unrecognized state database") from exc
    return conn


def _payload(snapshot: JobSnapshot, revision: int) -> str:
    body = snapshot.to_json()
    body["revision"] = revision
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _from_payload(payload: str) -> JobSnapshot:
    try:
        decoded: object = json.loads(payload)
        return JobSnapshot.from_json(json_object(decoded))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StoreWriteFailed("malformed saved snapshot") from exc


def commit_job(conn: sqlite3.Connection, expected_revision: int, next_snapshot: JobSnapshot) -> JobSnapshot:
    try:
        _validate_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        _validate_connection(conn)
        current = conn.execute("SELECT revision FROM jobs WHERE id = ?", (next_snapshot.id,)).fetchone()
        revision = expected_revision + 1
        if current is None:
            if expected_revision != 0:
                raise StoreConflict(next_snapshot.id)
            conn.execute("INSERT INTO jobs (id, revision, payload) VALUES (?, ?, ?)", (next_snapshot.id, revision, _payload(next_snapshot, revision)))
        else:
            if int(current[0]) != expected_revision:
                raise StoreConflict(next_snapshot.id)
            cursor = conn.execute("UPDATE jobs SET revision = ?, payload = ? WHERE id = ? AND revision = ?", (revision, _payload(next_snapshot, revision), next_snapshot.id, expected_revision))
            if cursor.rowcount != 1:
                raise StoreConflict(next_snapshot.id)
        conn.commit()
    except StoreConflict:
        conn.rollback()
        raise
    except StoreUnsafe:
        conn.rollback()
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        conn.rollback()
        raise StoreWriteFailed("checkpoint write failed") from exc
    saved = load_job(conn, next_snapshot.id)
    if saved is None:
        raise StoreWriteFailed("checkpoint was not saved")
    return saved


def save_job(conn: sqlite3.Connection, snapshot: JobSnapshot, expected_revision: int) -> JobSnapshot:
    return commit_job(conn, expected_revision, snapshot)


def load_job(conn: sqlite3.Connection, job_id: str) -> JobSnapshot | None:
    _validate_connection(conn)
    row = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else _from_payload(str(row[0]))


def clear_jobs(conn: sqlite3.Connection) -> int:
    """Delete all recognized local jobs only after the CLI confirmation boundary."""

    try:
        _validate_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        _validate_connection(conn)
        count = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        conn.execute("DELETE FROM jobs")
        conn.commit()
        return count
    except StoreUnsafe:
        conn.rollback()
        raise
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise StoreWriteFailed("checkpoint clear failed") from exc
