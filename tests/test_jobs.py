import hashlib
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import lead_collector.store as store
from lead_collector.jobs import JobRunner, RunnerDeps, control_job
from lead_collector.model import JobCheckpoint, JobSnapshot, LeadRow, ObservedValue, PageDocument, RecipeV1, Selection
from lead_collector.store import StoreUnsafe, commit_job, load_job, managed_lease, open_store, save_job


SOURCE = "https://directory.example.test/list"
LIMITS = {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100}


def page(url: str, status: str = "completed", html: str = "", code: str | None = None) -> PageDocument:
    error = None if status in {"completed", "empty"} else {"code": code or status.upper(), "message": "safe", "url": url}
    return PageDocument(url, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), status, error)


def checkpoint(mode: str, urls: tuple[str, ...]) -> JobCheckpoint:
    return JobCheckpoint(mode, urls, {f"input:{index}": (url,) for index, url in enumerate(urls)}, tuple(dict.fromkeys(urls)), {}, (), ())


def recipe() -> RecipeV1:
    return RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "none", "selector": None}, LIMITS)


class JobRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.root_patch = patch.object(store, "MANAGED_STATE_ROOT", self.root)
        self.root_patch.start()
        self.state_path = self.root / "jobs.sqlite3"
        self.connection = open_store(self.state_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.root_patch.stop()
        self.temporary_directory.cleanup()

    def save(self, snapshot: JobSnapshot) -> JobSnapshot:
        return save_job(self.connection, snapshot, 0)

    def runner(self, fetch, render=None):
        return JobRunner(self.connection, self.state_path, RunnerDeps(fetch, render))

    def lease_path(self, job_id: str) -> Path:
        return self.root / ".lead-collector-locks" / f"{hashlib.sha256(job_id.encode()).hexdigest()}.lock"

    def test_lease_refuses_a_symlinked_lock_directory_without_an_outside_write(self):
        snapshot = self.save(JobSnapshot("lease-directory", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged")
        (self.root / ".lead-collector-locks").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(StoreUnsafe):
            with self.runner(lambda _request: self.fail("unsafe lease must not fetch"))._lease(snapshot.id):
                self.fail("unsafe lease must not start")

        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertEqual(tuple(outside.iterdir()), (sentinel,))
        self.assertEqual(load_job(self.connection, snapshot.id), snapshot)

    def test_lease_rejects_unsafe_existing_lock_leaves_without_mutating_the_source(self):
        snapshot = self.save(JobSnapshot("lease-leaves", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))
        lock_directory = self.root / ".lead-collector-locks"
        lock_directory.mkdir(mode=0o700)
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged")
        cases = ("symlink", "hardlink", "fifo", "unsafe-mode")

        for kind in cases:
            with self.subTest(kind=kind):
                job_id = f"lease-{kind}"
                leaf = self.lease_path(job_id)
                if kind == "symlink":
                    leaf.symlink_to(sentinel)
                elif kind == "hardlink":
                    os.link(sentinel, leaf)
                elif kind == "fifo":
                    os.mkfifo(leaf, 0o600)
                else:
                    leaf.write_text("lock")
                    os.chmod(leaf, 0o622)
                with self.assertRaises(StoreUnsafe):
                    with self.runner(lambda _request: self.fail("unsafe lease must not fetch"))._lease(job_id):
                        self.fail("unsafe lease must not start")

        self.assertEqual(sentinel.read_text(), "unchanged")
        self.assertEqual(load_job(self.connection, snapshot.id), snapshot)

    def test_lease_preserves_exclusion_while_an_active_runner_holds_the_lock(self):
        job_id = "active-lease"
        runner = self.runner(lambda _request: self.fail("lease test must not fetch"))
        probe = "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\ntry:\n fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept BlockingIOError:\n print('locked')\nelse:\n print('writable')\nfinally:\n os.close(fd)"

        with runner._lease(job_id):
            result = subprocess.run([sys.executable, "-c", probe, str(self.lease_path(job_id))], text=True, capture_output=True, check=False, env={"PATH": os.defpath})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "locked\n", result.stderr)

    def test_managed_lease_refuses_traversal_and_absolute_leaf_names_before_sidecar_io(self):
        names = ("../outside.lock", str(self.root / "absolute.lock"))

        for name in names:
            with self.subTest(name=name), self.assertRaises(StoreUnsafe):
                with managed_lease(self.connection, name):
                    self.fail("unsafe lease name must not start")

        self.assertFalse((self.root / "outside.lock").exists())
        self.assertFalse((self.root / "absolute.lock").exists())

    def test_attempt_is_committed_before_io_even_when_fetch_fails(self):
        snapshot = self.save(JobSnapshot("attempt", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))

        def fail(_request):
            raise RuntimeError("untrusted transport failure")

        result = self.runner(fail).run(snapshot.id)

        self.assertEqual(result.progress["attemptedPages"], 1)
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.errors[0]["code"], "FETCH_FAILED")
        self.assertEqual(load_job(self.connection, snapshot.id).progress["attemptedPages"], 1)

    def test_stop_persists_new_generation_before_ack(self):
        urls = (SOURCE, "https://directory.example.test/second")
        snapshot = self.save(JobSnapshot("stop", recipe=recipe(), checkpoint=checkpoint("contacts", urls)))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            control_job(self.connection, snapshot.id, "stop")
            return page(request.url, html="<a href='mailto:keep@example.test'>mail</a>")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(calls, [SOURCE])
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.generation, 1)
        self.assertEqual(result.rows, ())

    def test_pause_commits_current_page_then_stops_scheduling(self):
        urls = (SOURCE, "https://directory.example.test/second")
        snapshot = self.save(JobSnapshot("pause", recipe=recipe(), checkpoint=checkpoint("contacts", urls)))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            control_job(self.connection, snapshot.id, "pause")
            return page(request.url, html="<a href='mailto:kept@example.test'>mail</a>")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(calls, [SOURCE])
        self.assertEqual(result.state, "paused")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0]["emails"][0]["normalized"], "kept@example.test")

    def test_control_process_does_not_interrupt_live_runner(self):
        snapshot = self.save(JobSnapshot("control", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))

        def fetch(request):
            controlled = control_job(self.connection, snapshot.id, "pause")
            self.assertEqual(controlled.state, "pausing")
            return page(request.url)

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(result.state, "paused")
        self.assertEqual(result.progress["attemptedPages"], 1)

    def test_orphaned_running_job_becomes_interrupted_without_rerun(self):
        snapshot = self.save(JobSnapshot("orphan", state="running", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))

        result = self.runner(lambda _request: self.fail("orphan must not fetch")).run(snapshot.id)

        self.assertEqual(result.state, "interrupted")

    def test_explicit_resume_continues_an_orphaned_running_checkpoint_once(self):
        snapshot = self.save(JobSnapshot("orphan-resume", state="running", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            return page(request.url, html="<a href='mailto:resumed@example.test'>mail</a>")

        result = self.runner(fetch).resume(snapshot.id)

        self.assertEqual(calls, [SOURCE])
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.rows[0]["emails"][0]["normalized"], "resumed@example.test")

    def test_resume_is_explicit_and_starts_at_checkpoint(self):
        snapshot = self.save(JobSnapshot("resume", state="paused", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            return page(request.url, html="<a href='mailto:resume@example.test'>mail</a>")

        self.assertEqual(self.runner(fetch).run(snapshot.id).state, "paused")
        result = self.runner(fetch).resume(snapshot.id)

        self.assertEqual(calls, [SOURCE])
        self.assertEqual(result.state, "completed")
        self.assertEqual(len(result.rows), 1)

    def test_contact_limits_and_candidate_scope_include_failures(self):
        candidates = "".join(f"<a href='/contact-{number}'>Contact</a>" for number in range(1, 7))
        snapshot = self.save(JobSnapshot("limits", recipe=recipe(), checkpoint=checkpoint("contacts", (SOURCE,))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            self.assertEqual(request.allowed_hosts, ("directory.example.test",))
            self.assertEqual(request.allowed_paths, ("/",))
            return page(request.url, "completed" if request.url == SOURCE else "timeout", candidates if request.url == SOURCE else "")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(len(calls), 5)
        self.assertEqual(result.progress["attemptedPages"], 5)
        self.assertEqual(result.checkpoint.attempted_by_host["directory.example.test"], 5)
        self.assertEqual(len(result.errors), 4)

    def test_distinct_source_rows_with_same_website_keep_ids_evidence_and_current_edits(self):
        observed = "2026-10-31T00:00:00Z"
        website = ObservedValue(SOURCE, SOURCE, SOURCE, observed)
        first = LeadRow("first", ObservedValue("Acme North", "Acme North", SOURCE, observed), website, ObservedValue("1 Main", "1 Main", SOURCE, observed), edits={"company": "initial edit"})
        second = LeadRow("second", ObservedValue("Acme South", "Acme South", SOURCE, observed), website, ObservedValue("2 Main", "2 Main", SOURCE, observed))
        source_rows = {"first": (SOURCE,), "second": (SOURCE,)}
        frozen = JobCheckpoint("contacts", (SOURCE, SOURCE), source_rows, (SOURCE,), {}, (), ())
        snapshot = self.save(
            JobSnapshot(
                "branches",
                progress={"attemptedPages": 0, "completedInputs": 0, "totalInputs": 2},
                recipe=recipe(),
                rows=(first.to_json(), second.to_json()),
                checkpoint=frozen,
            )
        )

        def fetch(request):
            current = load_job(self.connection, snapshot.id)
            assert current is not None
            edited = list(current.rows)
            edited[0] = replace(LeadRow.from_json(edited[0]), edits={"company": "current edit"}).to_json()
            commit_job(self.connection, current.revision, replace(current, rows=tuple(edited)))
            return page(request.url, html="<a href='mailto:shared@example.test'>mail</a>")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual([row["id"] for row in result.rows], ["first", "second"])
        self.assertEqual({row["address"]["normalized"] for row in result.rows}, {"1 Main", "2 Main"})
        self.assertEqual(result.rows[0]["edits"], {"company": "current edit"})
        self.assertTrue(all(row["emails"][0]["normalized"] == "shared@example.test" for row in result.rows))
        self.assertEqual(result.progress["completedInputs"], 2)
        self.assertEqual(result.progress["totalInputs"], 2)

    def test_three_static_pages_collect_thirty_unique_source_rows(self):
        fixture = (Path(__file__).parent / "fixtures" / "table_30.html").read_text()
        urls = tuple(f"https://directory.example.test/page-{number}" for number in range(1, 4))
        snapshot = self.save(JobSnapshot("list", recipe=recipe(), checkpoint=checkpoint("collect", urls)))

        result = self.runner(lambda request: page(request.url, html=fixture + f"<!-- {request.url} -->")).run(snapshot.id)

        self.assertEqual(result.state, "completed")
        self.assertEqual(result.progress["attemptedPages"], 3)
        self.assertEqual(len(result.rows), 30)
        self.assertEqual(result.rows[0]["company"]["sourceUrl"], urls[0])

    def test_static_next_follows_three_table_pages_from_one_seed_url(self):
        page_urls = tuple(f"https://directory.example.test/page-{number}" for number in range(1, 4))
        next_recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "next", "selector": "a.next"}, LIMITS)
        snapshot = self.save(JobSnapshot("next-table", recipe=next_recipe, checkpoint=checkpoint("collect", (page_urls[0],))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            number = page_urls.index(request.url) + 1
            next_link = "" if number == 3 else f"<a class='next' href='/page-{number + 1}'>Next</a>"
            html = f"<table><tr><th>Name</th></tr><tr><td>Company {number}</td></tr></table>{next_link}"
            return page(request.url, html=html)

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(calls, list(page_urls))
        self.assertEqual(result.progress["attemptedPages"], 3)
        self.assertEqual(result.progress["completedInputs"], 1)
        self.assertEqual(result.progress["totalInputs"], 1)
        self.assertEqual({row["company"]["normalized"] for row in result.rows}, {"Company 1", "Company 2", "Company 3"})
        self.assertTrue(all(row["status"] == "pending" for row in result.rows))

    def test_collect_stops_at_the_smaller_row_limit_with_a_distinct_limit_outcome(self):
        bounded = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "next", "selector": "a.next"}, {**LIMITS, "maxRows": 1})
        snapshot = self.save(JobSnapshot("row-cap", limits=dict(bounded.limits), recipe=bounded, checkpoint=checkpoint("collect", (SOURCE,))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            return page(request.url, html="<table><tr><th>Name</th></tr><tr><td>One</td></tr><tr><td>Two</td></tr></table><a class='next' href='/second'>Next</a>")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(calls, [SOURCE])
        self.assertEqual(result.state, "completed")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.errors[-1]["status"], "limited")

    def test_static_next_follows_cards_but_none_never_follows_next(self):
        page_urls = ("https://directory.example.test/card-1", "https://directory.example.test/card-2")
        selection = Selection(".card", "", ("Name",), (), {"Name": "h2"})
        static_recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), selection, {"Name": "company"}, {"mode": "next", "selector": "a.next"}, LIMITS)
        snapshot = self.save(JobSnapshot("next-card", recipe=static_recipe, checkpoint=checkpoint("collect", (page_urls[0],))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            number = page_urls.index(request.url) + 1
            next_link = "" if number == 2 else "<a class='next' href='/card-2'>Next</a>"
            return page(request.url, html=f"<div class='card'><h2>Card {number}</h2></div>{next_link}")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(calls, list(page_urls))
        self.assertEqual({row["company"]["normalized"] for row in result.rows}, {"Card 1", "Card 2"})

    def test_none_never_follows_next_and_js_pagination_is_unavailable_without_renderer(self):
        none_recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "none", "selector": None}, LIMITS)
        none_snapshot = self.save(JobSnapshot("none-next", recipe=none_recipe, checkpoint=checkpoint("collect", (SOURCE,))))
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            return page(request.url, html="<table><tr><th>Name</th></tr><tr><td>One</td></tr></table><a class='next' href='/second'>Next</a>")

        self.assertEqual(self.runner(fetch).run(none_snapshot.id).state, "completed")
        self.assertEqual(calls, [SOURCE])
        js_recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {}, {"mode": "loadMore", "selector": ".more"}, LIMITS)
        js_snapshot = self.save(JobSnapshot("js-only", recipe=js_recipe, checkpoint=checkpoint("collect", (SOURCE,))))

        result = self.runner(lambda _request: self.fail("static transport must not run JS pagination")).run(js_snapshot.id)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.errors[0]["code"], "CAPABILITY_UNAVAILABLE")

    def test_ego_renderer_uses_one_advance_per_child_and_stops_on_repeated_dom(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "loadMore", "selector": ".more"}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-advance", recipe=dynamic, checkpoint=rendered))
        calls: list[str] = []
        html = (
            "<table><tr><th>Name</th></tr><tr><td>One</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>Two</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>Two</td></tr></table>",
        )

        def render(request):
            calls.append(request.operation)
            return page(SOURCE, html=html[len(calls) - 1])

        result = self.runner(lambda _request: self.fail("static fetch must not run"), render).run(snapshot.id)

        self.assertEqual(calls, ["navigate", "advance", "advance"])
        self.assertEqual(result.state, "completed")
        self.assertEqual({row["company"]["normalized"] for row in result.rows}, {"One", "Two"})

    def test_ego_next_clicks_the_approved_control_then_exhausts_cleanly(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "next", "selector": "a.next"}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-next", recipe=dynamic, checkpoint=rendered))
        calls: list[str] = []

        def render(request):
            calls.append(request.operation)
            return page(SOURCE, "empty") if request.operation == "advance" else page(SOURCE, html="<table><tr><th>Name</th></tr><tr><td>One</td></tr></table>")

        result = self.runner(lambda _request: self.fail("static fetch must not run"), render).run(snapshot.id)

        self.assertEqual(calls, ["navigate", "advance"])
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.errors, ())

    def test_ego_next_url_change_with_repeated_content_reaches_repeated_page_policy(self):
        """A URL-only browser Next must reach the runner, where duplicate DOM policy remains explicit."""
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "next", "selector": "a.next"}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-next-repeat", recipe=dynamic, checkpoint=rendered))
        html = "<table><tr><th>Name</th></tr><tr><td>Same</td></tr></table>"
        second = "https://directory.example.test/page-2"
        calls: list[str] = []

        def render(request):
            calls.append(request.operation)
            return page(SOURCE if request.operation == "navigate" else second, html=html)

        result = self.runner(lambda _request: self.fail("static fetch must not run"), render).run(snapshot.id)

        self.assertEqual(calls, ["navigate", "advance"])
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.errors[-1]["status"], "repeated")

    def test_ego_scroll_requires_two_unchanged_rounds_after_the_last_change(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "scroll", "selector": None}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-scroll-stability", recipe=dynamic, checkpoint=rendered))
        calls: list[str] = []
        html = (
            "<table><tr><th>Name</th></tr><tr><td>One</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>One</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>Two</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>Two</td></tr></table>",
            "<table><tr><th>Name</th></tr><tr><td>Two</td></tr></table>",
        )

        def render(request):
            calls.append(request.operation)
            return page(SOURCE, html=html[len(calls) - 1])

        result = self.runner(lambda _request: self.fail("static fetch must not run"), render).run(snapshot.id)

        self.assertEqual(calls, ["navigate", "advance", "advance", "advance", "advance"])
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.checkpoint.unchanged_scrolls, 2)
        self.assertEqual({row["company"]["normalized"] for row in result.rows}, {"One", "Two"})

    def test_renderer_failure_is_terminal_not_a_completed_job_with_errors(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "loadMore", "selector": ".more"}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-failure", recipe=dynamic, checkpoint=rendered))

        result = self.runner(lambda _request: self.fail("static fetch must not run"), lambda _request: page(SOURCE, "timeout")).run(snapshot.id)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.errors[-1]["status"], "timeout")

    def test_each_non_green_renderer_outcome_is_terminal(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "loadMore", "selector": ".more"}, LIMITS)

        for status in ("denied", "blocked", "malformed", "timeout", "truncated", "unavailable", "failed"):
            with self.subTest(status=status):
                rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
                snapshot = self.save(JobSnapshot(f"ego-{status}", recipe=dynamic, checkpoint=rendered))
                result = self.runner(lambda _request: self.fail("static fetch must not run"), lambda _request: page(SOURCE, status)).run(snapshot.id)

                self.assertEqual(result.state, "failed")
                self.assertEqual(result.errors[-1]["status"], status)

    def test_ego_stop_discards_late_document_and_never_starts_another_renderer_call(self):
        dynamic = RecipeV1(SOURCE, ("directory.example.test",), ("/",), None, {"Name": "company"}, {"mode": "scroll", "selector": None}, LIMITS)
        rendered = replace(checkpoint("collect", (SOURCE,)), renderer={"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})
        snapshot = self.save(JobSnapshot("ego-stop", recipe=dynamic, checkpoint=rendered))
        calls: list[str] = []

        def render(request):
            calls.append(request.operation)
            control_job(self.connection, snapshot.id, "stop")
            return page(SOURCE, html="<table><tr><th>Name</th></tr><tr><td>Late</td></tr></table>")

        result = self.runner(lambda _request: self.fail("static fetch must not run"), render).run(snapshot.id)

        self.assertEqual(calls, ["navigate"])
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.rows, ())

    def test_partial_failures_preserve_committed_rows_and_distinct_statuses(self):
        urls = tuple(f"https://directory.example.test/{status}" for status in ("completed", "empty", "denied", "blocked", "malformed", "timeout"))
        snapshot = self.save(JobSnapshot("partial", recipe=recipe(), checkpoint=checkpoint("collect", urls)))

        def fetch(request):
            status = request.url.rsplit("/", 1)[1]
            return page(request.url, status, "<table><tr><th>Name</th></tr><tr><td>Kept</td></tr></table>" if status == "completed" else "")

        result = self.runner(fetch).run(snapshot.id)

        self.assertEqual(result.state, "completed")
        self.assertEqual(len(result.rows), 1)
        self.assertEqual({item["status"] for item in result.errors}, {"empty", "denied", "blocked", "malformed", "timeout"})

    def test_all_layout_extraction_failures_are_not_completed_as_empty_results(self):
        """A stale card selection must name layout drift and fail rather than look empty."""
        stale = Selection(".card", "not-a-real-fingerprint", ("Name",), (), {"Name": "h2"})
        broken_recipe = RecipeV1(
            SOURCE,
            ("directory.example.test",),
            ("/",),
            stale,
            {"Name": "company"},
            {"mode": "none", "selector": None},
            LIMITS,
        )
        snapshot = self.save(JobSnapshot("layout-drift", recipe=broken_recipe, checkpoint=checkpoint("collect", (SOURCE,))))

        result = self.runner(lambda request: page(request.url, html="<div class='card'><h2>Acme</h2></div>")).run(snapshot.id)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.rows, ())
        self.assertEqual(result.errors[-1]["code"], "LAYOUT_DRIFT")
        self.assertEqual(result.errors[-1]["status"], "layout-drift")
        self.assertEqual(result.checkpoint.seen_page_hashes, ())

    def test_checkpoint_rejects_more_than_one_hundred_contact_inputs(self):
        urls = tuple(f"https://directory.example.test/{number}" for number in range(101))

        with self.assertRaises(ValueError):
            checkpoint("contacts", urls).to_json()


if __name__ == "__main__":
    unittest.main()
