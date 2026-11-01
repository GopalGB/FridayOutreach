import hashlib
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lead_collector.jobs as jobs
import lead_collector.store as store
from lead_collector.jobs import JobRunner, RunnerDeps
from lead_collector.model import JobCheckpoint, JobSnapshot, LeadRow, ObservedValue, PageDocument, RecipeV1
from lead_collector.store import load_job, open_store, save_job


SOURCE = "https://directory.example.test/list"
LIMITS = {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100}


def page(html: str = "<a href='mailto:captured@example.test'>mail</a>") -> PageDocument:
    return PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)


def snapshot(job_id: str, state: str = "idle") -> JobSnapshot:
    recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "none", "selector": None}, LIMITS)
    checkpoint = JobCheckpoint("contacts", (SOURCE,), {"input:0": (SOURCE,)}, (SOURCE,), {}, (), ())
    return JobSnapshot(job_id, state=state, recipe=recipe, checkpoint=checkpoint)


class CommitRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.root_patch = patch.object(store, "MANAGED_STATE_ROOT", self.root)
        self.root_patch.start()
        self.state_path = self.root / "jobs.sqlite3"
        self.connection = open_store(self.state_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.root_patch.stop()
        self.directory.cleanup()

    def test_conflict_recommits_same_document_without_refetch_and_preserves_edit(self) -> None:
        original = save_job(self.connection, snapshot("race"), 0)
        fetches = 0
        injected = False
        real_commit = jobs.commit_job

        def fetch(_request):
            nonlocal fetches
            fetches += 1
            return page()

        def commit(connection, expected_revision, next_snapshot):
            nonlocal injected
            if not injected and next_snapshot.rows:
                injected = True
                current = load_job(connection, original.id)
                assert current is not None
                manual = LeadRow("manual", ObservedValue("Manual", "Manual", SOURCE, "2026-10-31T00:00:00Z"), edits={"company": "kept"})
                edited = replace(current, rows=(manual.to_json(),))
                real_commit(connection, current.revision, edited)
            return real_commit(connection, expected_revision, next_snapshot)

        jobs.commit_job = commit
        try:
            result = JobRunner(self.connection, self.state_path, RunnerDeps(fetch)).run(original.id)
        finally:
            jobs.commit_job = real_commit

        self.assertEqual(fetches, 1)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.rows[0]["edits"], {"company": "kept"})
        self.assertIn("captured@example.test", {email["normalized"] for row in result.rows for email in row["emails"]})

    def test_stop_after_fetch_discards_captured_document(self) -> None:
        original = save_job(self.connection, snapshot("stop"), 0)
        fetches = 0
        real_commit = jobs.commit_job

        def fetch(_request):
            nonlocal fetches
            fetches += 1
            current = load_job(self.connection, original.id)
            assert current is not None
            real_commit(self.connection, current.revision, replace(current, state="stopped", generation=current.generation + 1))
            return page()

        result = JobRunner(self.connection, self.state_path, RunnerDeps(fetch)).run(original.id)
        self.assertEqual(fetches, 1)
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.rows, ())

    def test_permanent_conflict_raises_after_bounded_retries_without_refetch(self) -> None:
        original = save_job(self.connection, snapshot("permanent"), 0)
        fetches = 0
        real_commit = jobs.commit_job
        attempts = 0

        def fetch(_request):
            nonlocal fetches
            fetches += 1
            return page()

        def conflict(connection, expected_revision, next_snapshot):
            nonlocal attempts
            if next_snapshot.rows:
                attempts += 1
                raise jobs.StoreConflict(next_snapshot.id)
            return real_commit(connection, expected_revision, next_snapshot)

        jobs.commit_job = conflict
        try:
            with self.assertRaises(jobs.StoreConflict):
                JobRunner(self.connection, self.state_path, RunnerDeps(fetch)).run(original.id)
        finally:
            jobs.commit_job = real_commit
        self.assertEqual(fetches, 1)
        self.assertEqual(attempts, jobs.MAX_DOCUMENT_COMMIT_RETRIES)

    def test_renderer_advance_conflict_keeps_first_page_and_same_id_edit(self) -> None:
        recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "loadMore", "selector": ".more"}, {**LIMITS, "maxPages": 2})
        second_url = "https://directory.example.test/page-2"
        first_html = "<table><tr><th>Name</th></tr><tr><td>First</td></tr></table>"
        first = page(first_html)
        first_row = LeadRow("first", ObservedValue("First", "First", SOURCE, "2026-10-31T00:00:00Z"), emails=(ObservedValue("captured@example.test", "captured@example.test", SOURCE, "2026-10-31T00:00:00Z"),)).to_json()
        checkpoint = JobCheckpoint("collect", (second_url,), {"input:0": (second_url,)}, (second_url,), {}, (first.sha256,), (SOURCE,), {"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        original = save_job(self.connection, JobSnapshot("renderer-race", state="paused", progress={"attemptedPages": 1, "completedInputs": 0, "totalInputs": 1}, limits={**LIMITS, "maxPages": 2}, recipe=recipe, rows=(first_row,), checkpoint=checkpoint), 0)
        advances = 0
        injected = False
        rendered = False
        real_commit = jobs.commit_job

        def render(request):
            nonlocal advances, rendered
            self.assertEqual(request.operation, "advance")
            advances += 1
            rendered = True
            return PageDocument(second_url, "<table><tr><th>Name</th></tr><tr><td>Second</td></tr></table><a href='mailto:captured@example.test'>mail</a>", "2026-10-31T00:00:00Z", hashlib.sha256(b"second").hexdigest(), "completed", None)

        def commit(connection, expected_revision, next_snapshot):
            nonlocal injected
            if rendered and not injected and next_snapshot.progress["attemptedPages"] == 2 and next_snapshot.rows != (first_row,):
                injected = True
                current = load_job(connection, original.id)
                assert current is not None
                rows = [replace(LeadRow.from_json(row), edits={"company": "reviewed"}).to_json() if row["emails"] else row for row in current.rows]
                real_commit(connection, current.revision, replace(current, rows=tuple(rows)))
            return real_commit(connection, expected_revision, next_snapshot)

        jobs.commit_job = commit
        try:
            result = JobRunner(self.connection, self.state_path, RunnerDeps(lambda _request: self.fail("static fetch"), render)).resume(original.id)
        finally:
            jobs.commit_job = real_commit
        self.assertEqual(advances, 1)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.progress["attemptedPages"], 2)
        self.assertEqual(result.progress["completedInputs"], 1)
        self.assertEqual(len(result.rows), 2)
        captured = next(row for row in result.rows if row["emails"])
        self.assertEqual(captured["edits"], {"company": "reviewed"})
        self.assertEqual(captured["emails"][0]["normalized"], "captured@example.test")

    def test_pause_during_conflicting_result_commit_commits_captured_document(self) -> None:
        original = save_job(self.connection, snapshot("pause-race"), 0)
        real_commit = jobs.commit_job
        injected = False

        def commit(connection, expected_revision, next_snapshot):
            nonlocal injected
            if not injected and next_snapshot.rows:
                injected = True
                current = load_job(connection, original.id)
                assert current is not None
                edited = replace(current, rows=(LeadRow("manual", edits={"company": "kept"}).to_json(),))
                real_commit(connection, current.revision, edited)
                jobs.control_job(connection, original.id, "pause")
                raise jobs.StoreConflict(original.id)
            return real_commit(connection, expected_revision, next_snapshot)

        jobs.commit_job = commit
        try:
            result = JobRunner(self.connection, self.state_path, RunnerDeps(lambda _request: page())).run(original.id)
        finally:
            jobs.commit_job = real_commit
        self.assertEqual(result.state, "paused")
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0]["edits"], {"company": "kept"})

    def test_stop_during_conflicting_result_commit_discards_captured_document(self) -> None:
        original = save_job(self.connection, snapshot("stop-race"), 0)
        real_commit = jobs.commit_job
        injected = False

        def commit(connection, expected_revision, next_snapshot):
            nonlocal injected
            if not injected and next_snapshot.rows:
                injected = True
                jobs.control_job(connection, original.id, "stop")
                raise jobs.StoreConflict(original.id)
            return real_commit(connection, expected_revision, next_snapshot)

        jobs.commit_job = commit
        try:
            result = JobRunner(self.connection, self.state_path, RunnerDeps(lambda _request: page())).run(original.id)
        finally:
            jobs.commit_job = real_commit
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.generation, 1)
        self.assertEqual(result.rows, ())

