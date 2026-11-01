import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lead_collector.model import JobSnapshot
import lead_collector.store as store
from lead_collector.store import StoreConflict, StoreUnsafe, StoreWriteFailed, create_store, load_job, open_store, save_job


class StoreTests(unittest.TestCase):
    def test_first_save_keeps_snapshot_rows_and_errors(self):
        """Hardcoding an empty insert payload must fail this test."""
        conn = sqlite3.connect(":memory:")
        create_store(conn)
        snapshot = JobSnapshot(
            id="job-1",
            rows=({"id": "lead-1", "company": {"raw": "Acme"}},),
            errors=({"code": "CONTENT_TRUNCATED", "message": "bounded"},),
        )

        saved = save_job(conn, snapshot, 0)

        self.assertEqual(saved.rows, snapshot.rows)
        self.assertEqual(saved.errors, snapshot.errors)
        self.assertEqual(saved.revision, 1)

    def test_revision_compare_and_swap_rejects_stale_write_without_losing_old_checkpoint(self):
        """A stale writer replacing completed data must fail this test."""
        conn = sqlite3.connect(":memory:")
        create_store(conn)
        first = JobSnapshot(id="job-1", rows=({"id": "lead-1"},))
        save_job(conn, first, 0)
        current = load_job(conn, "job-1")
        assert current is not None
        save_job(conn, current.with_state("completed"), current.revision)

        with self.assertRaises(StoreConflict):
            save_job(conn, first.with_state("failed"), first.revision)

        reopened = load_job(conn, "job-1")
        assert reopened is not None
        self.assertEqual(reopened.state, "completed")
        self.assertEqual(reopened.rows, ({"id": "lead-1"},))

    def test_write_failure_rolls_back_and_preserves_committed_snapshot(self):
        """A database write error replacing the old checkpoint must fail this test."""
        conn = sqlite3.connect(":memory:")
        create_store(conn)
        first = JobSnapshot(id="job-1", rows=({"id": "lead-1"},))
        saved = save_job(conn, first, 0)
        conn.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT, 'reject'); END")
        conn.commit()

        with self.assertRaises(StoreWriteFailed):
            save_job(conn, saved.with_state("completed"), saved.revision)

        reopened = load_job(conn, "job-1")
        assert reopened is not None
        self.assertEqual(reopened.revision, 1)
        self.assertEqual(reopened.state, "idle")

    def test_load_rejects_nonfinite_json_value_before_returning_checkpoint(self):
        """Permitting NaN through a stored row would break the JSON checkpoint contract."""
        conn = sqlite3.connect(":memory:")
        create_store(conn)
        conn.execute(
            "INSERT INTO jobs (id, revision, payload) VALUES (?, ?, ?)",
            (
                "job-1",
                1,
                '{"id":"job-1","revision":1,"generation":0,"state":"idle","progress":{"attemptedPages":0,"completedInputs":0,"totalInputs":1},"limits":{"maxInputs":100,"maxPagesPerHost":5,"maxRows":1000,"maxPages":100},"rows":[{"score":NaN}],"errors":[],"recipe":null}',
            ),
        )

        with self.assertRaises(StoreWriteFailed):
            load_job(conn, "job-1")

    def test_load_rejects_semantically_tampered_checkpoint_payload(self):
        """Returning a syntactically valid checkpoint with invalid bounds would defeat resume safeguards."""
        payload = {
            "id": "job-1",
            "revision": 1,
            "generation": 0,
            "state": "completed",
            "progress": {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
            "rows": [],
            "errors": [],
            "recipe": {"version": 1, "startUrl": "https://directory.example.test/list", "allowedHosts": ["directory.example.test"], "allowedPaths": ["/list"], "selection": None, "mapping": {"Name": "company"}, "pagination": {"mode": "none", "selector": None}, "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100}},
        }
        corruptions = (
            {**payload, "state": "unknown"},
            {**payload, "progress": {"attemptedPages": 1, "completedInputs": 1}},
            {**payload, "recipe": {**payload["recipe"], "version": 2}},
            {**payload, "recipe": {**payload["recipe"], "mapping": {"Name": "lead"}}},
        )

        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted):
                conn = sqlite3.connect(":memory:")
                create_store(conn)
                conn.execute("INSERT INTO jobs (id, revision, payload) VALUES (?, ?, ?)", ("job-1", 1, json.dumps(corrupted)))
                with self.assertRaises(StoreWriteFailed):
                    load_job(conn, "job-1")


class ManagedStateBoundaryTests(unittest.TestCase):
    """Filesystem state is accepted only inside a private tool-owned root."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        os.chmod(self.root, 0o700)
        self.root_patch = patch.object(store, "MANAGED_STATE_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self) -> None:
        self.root_patch.stop()
        self.directory.cleanup()

    def test_rejects_sqlite_memory_escape_without_creating_a_state_file(self) -> None:
        """Treating :memory: as a database would bypass the managed-state boundary."""
        with self.assertRaises(StoreUnsafe):
            open_store(Path(":memory:"))
        self.assertFalse((self.root / ":memory:").exists())

    def test_rejects_group_writable_managed_ancestor_before_opening_leaf(self) -> None:
        """Trusting a writable ancestor lets another account replace the private root."""
        os.chmod(self.root, 0o722)
        with self.assertRaises(StoreUnsafe):
            open_store(Path("jobs.sqlite3"))
        self.assertFalse((self.root / "jobs.sqlite3").exists())

    def test_rejects_symlinked_managed_root_and_leaf_before_state_io(self) -> None:
        """A managed-root or leaf symlink would redirect the state boundary."""
        real_root = self.root / "real-root"
        real_root.mkdir(mode=0o700)
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with patch.object(store, "MANAGED_STATE_ROOT", linked_root), self.assertRaises(StoreUnsafe):
            open_store(Path("jobs.sqlite3"))

        target = self.root / "target.sqlite3"
        target.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
        (self.root / "jobs.sqlite3").symlink_to(target)
        with self.assertRaises(StoreUnsafe):
            open_store(Path("jobs.sqlite3"))

    def test_rejects_existing_hardlinked_state_before_sqlite_can_open_it(self) -> None:
        """A second hard link would let an accepted state alias another file."""
        state = self.root / "jobs.sqlite3"
        state.write_bytes(b"SQLite format 3\x00" + b"x" * 64)
        os.link(state, self.root / "alias.sqlite3")
        before = state.read_bytes()

        with self.assertRaises(StoreUnsafe):
            open_store(state)

        self.assertEqual(state.read_bytes(), before)

    def test_rejects_foreign_owned_existing_database_without_changing_its_bytes(self) -> None:
        """An existing database owned by somebody else must remain byte-for-byte untouched."""
        state = self.root / "foreign.sqlite3"
        foreign = sqlite3.connect(state)
        foreign.execute("CREATE TABLE customer_data (name TEXT)")
        foreign.commit()
        foreign.close()
        before = state.read_bytes()
        real_lstat = store.os.lstat

        def foreign_leaf(path: str | Path) -> os.stat_result:
            metadata = real_lstat(path)
            if Path(path) == state:
                return os.stat_result((metadata.st_mode, metadata.st_ino, metadata.st_dev, metadata.st_nlink, metadata.st_uid + 1, metadata.st_gid, metadata.st_size, metadata.st_atime, metadata.st_mtime, metadata.st_ctime))
            return metadata

        with patch.object(store.os, "lstat", side_effect=foreign_leaf), self.assertRaises(StoreUnsafe):
            open_store(state)

        self.assertEqual(state.read_bytes(), before)

    def test_rejects_leaf_replaced_during_connection_before_schema_validation(self) -> None:
        """Validating only before connect would attach a connection to a replaced leaf."""
        state = self.root / "jobs.sqlite3"
        initial = open_store(state)
        initial.close()
        replacement = self.root / "replacement.sqlite3"
        replacement_conn = sqlite3.connect(replacement)
        create_store(replacement_conn)
        replacement_conn.close()
        real_connect = store.sqlite3.connect

        def replace_after_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = real_connect(*args, **kwargs)
            replacement.replace(state)
            return connection

        with patch.object(store.sqlite3, "connect", side_effect=replace_after_connect), self.assertRaises(StoreUnsafe):
            open_store(state)

    def test_rejects_leaf_replaced_before_write_without_writing_the_replacement(self) -> None:
        """Checking only at open leaves later writes attached to an untrusted pathname."""
        state = self.root / "jobs.sqlite3"
        connection = open_store(state)
        replacement = self.root / "replacement.sqlite3"
        replacement_conn = sqlite3.connect(replacement)
        create_store(replacement_conn)
        replacement_conn.close()
        replacement.replace(state)

        with self.assertRaises(StoreUnsafe):
            save_job(connection, JobSnapshot("job"), 0)
        connection.close()
        reopened = sqlite3.connect(state)
        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        reopened.close()

    def test_rejects_leaf_replaced_after_begin_immediate_before_dml(self) -> None:
        """A rename after the lock begins must be detected before the first DML statement."""
        state = self.root / "jobs.sqlite3"
        connection = open_store(state)
        replacement = self.root / "replacement.sqlite3"
        replacement_conn = sqlite3.connect(replacement)
        create_store(replacement_conn)
        replacement_conn.close()
        real_validate = getattr(store, "_validate_connection", None)
        validations = 0

        def replace_on_second_write_validation(conn: sqlite3.Connection) -> None:
            nonlocal validations
            validations += 1
            if validations == 2:
                replacement.replace(state)
            if real_validate is not None:
                real_validate(conn)

        with patch.object(store, "_validate_connection", create=True, side_effect=replace_on_second_write_validation), self.assertRaises(StoreUnsafe):
            save_job(connection, JobSnapshot("job"), 0)
        connection.close()

    def test_opening_a_second_connection_keeps_an_existing_write_lock(self) -> None:
        """A raw guard descriptor must not release another SQLite connection's POSIX lock."""
        state = self.root / "jobs.sqlite3"
        first = open_store(state)
        first.execute("BEGIN IMMEDIATE")
        second = open_store(state)
        second.close()
        uri = state.as_uri() + "?mode=rw"
        probe = "import sqlite3, sys; connection = sqlite3.connect(sys.argv[1], uri=True, timeout=0);\ntry:\n connection.execute('BEGIN IMMEDIATE')\nexcept sqlite3.OperationalError:\n print('locked')\nelse:\n connection.rollback(); print('writable')\nfinally:\n connection.close()"

        locked = subprocess.run([sys.executable, "-c", probe, uri], text=True, capture_output=True, check=False, env={"PATH": os.defpath})
        first.rollback()
        writable = subprocess.run([sys.executable, "-c", probe, uri], text=True, capture_output=True, check=False, env={"PATH": os.defpath})
        first.close()

        self.assertEqual(locked.stdout, "locked\n", locked.stderr)
        self.assertEqual(writable.stdout, "writable\n", writable.stderr)


if __name__ == "__main__":
    unittest.main()
