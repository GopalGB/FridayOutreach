import hashlib
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lead_collector import cli
from lead_collector.ai import AiError, AiResult, AiSuggestion
from lead_collector.cli import main
from lead_collector.email_verify import VerificationDetails, VerificationResult
from lead_collector.extract import preview_document
from lead_collector.model import JobSnapshot, LeadRow, ObservedValue, PageDocument, RecipeV1, Selection
import lead_collector.store as store
from lead_collector.store import load_job, open_store, save_job


ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SOURCE = "https://directory.example.test/list"
MAPPING = json.dumps({"Name": "company", "Website": "website", "Address": "address"})
CARD_SELECTION = json.dumps({"selector": ".card", "columnSelectors": {"Name": ".name", "Website": ".website", "Address": ".address", "Region": ".region"}})


_main = main


def main(argv: list[str] | None = None) -> int:
    if argv is not None and "--state" in argv:
        state = Path(argv[argv.index("--state") + 1])
        if state.is_absolute():
            with patch.object(store, "MANAGED_STATE_ROOT", state.parent.resolve()):
                return _main(argv)
    return _main(argv)


def invoke(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    launcher = "from pathlib import Path; import sys; import lead_collector.store as store; store.MANAGED_STATE_ROOT = Path(sys.argv[1]); from lead_collector.cli import main; raise SystemExit(main(sys.argv[2:]))"
    return subprocess.run([sys.executable, "-c", launcher, str(root), *args], cwd=ROOT, text=True, capture_output=True, check=False)


class CliTests(unittest.TestCase):
    def test_verify_email_uses_the_explicit_binary_and_returns_typed_result(self):
        result = VerificationResult(
            "hello@example.test",
            "safe",
            True,
            VerificationDetails(True, True, True, False, False, False),
            None,
        )
        output = StringIO()
        with redirect_stdout(output), patch("lead_collector.email_verify.verify_email", return_value=result) as verify:
            exit_code = main(["verify-email", "--email", "hello@example.test", "--reacher-binary", "/tmp/reacher-smtp-check"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["mailboxVerified"])
        request = verify.call_args.args[0]
        self.assertEqual(request.email, "hello@example.test")
        self.assertEqual(request.binary, Path("/tmp/reacher-smtp-check"))

    def test_resume_rejects_a_lease_symlink_with_a_sanitized_input_envelope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                connection = open_store(state)
                save_job(connection, JobSnapshot("lease-cli"), 0)
                connection.close()
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            (root / ".lead-collector-locks").symlink_to(outside, target_is_directory=True)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--state", str(state), "resume", "--job", "lease-cli"])
            outside_entries = tuple(outside.iterdir())

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")
        self.assertEqual(outside_entries, ())

    def test_assist_offline_extracts_rows_without_network_or_optional_ai_imports(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("socket.socket", side_effect=AssertionError("network disabled")):
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"})])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["rows"][0]["company"]["normalized"], "Acme Example")
        self.assertEqual(body["data"]["ai"]["status"], "disabled")
        self.assertEqual(body["data"]["candidateCount"], 1)

    def test_assist_cost_guard_precedes_url_fetch_and_provider_lookup(self):
        output = StringIO()
        with redirect_stdout(output), patch.object(cli, "fetch_page") as fetch:
            exit_code = main(["assist", "--url", SOURCE, "--recipe", "not-read.json", "--ai", "hybrid"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "COST_POLICY")
        fetch.assert_not_called()

    def test_assist_url_off_uses_the_recipe_scoped_fetch_and_actual_document_provenance(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        document = PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1, "maxPages": 1},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()
            with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document) as fetch:
                exit_code = main(["assist", "--url", SOURCE, "--recipe", str(recipe_path)])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertEqual(fetch.call_args.args[0].url, SOURCE)
        self.assertEqual(body["data"]["rows"][0]["company"]["sourceUrl"], SOURCE)
        self.assertEqual(body["data"]["ai"]["status"], "disabled")

    def test_assist_request_preview_is_contract_only_and_keeps_page_values_out_of_questions(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Copper Pine Demo</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("socket.socket", side_effect=AssertionError("network disabled")):
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--request-preview"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertTrue(body["data"]["contractOnly"])
        self.assertIn("Copper Pine Demo", str(body["data"]["request"]["state"]))
        self.assertNotIn("Copper Pine Demo", str(body["data"]["request"]["questions"]))
        candidate = body["data"]["request"]["state"]["candidates"][0]
        self.assertEqual(candidate["id"], "c0")
        self.assertIn("renderedText", candidate["sourceFragment"])
        self.assertIn("Copper Pine Demo", candidate["sourceFragment"])

    def test_assist_candidates_use_decoded_visible_text_and_allowlisted_href_evidence(self):
        html = "<table><tr><th>Company</th></tr><tr><td><span>Acme &amp; <strong>Sons</strong></span></td></tr></table><a href='mailto:team&#64;example.test'>Email us</a>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Company": "company"}), "--ai", "jev", "--request-preview"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        candidates = body["data"]["request"]["state"]["candidates"]
        self.assertEqual([(item["id"], item["field"], item["value"]) for item in candidates], [("c0", "company", "Acme & Sons"), ("c1", "email", "team@example.test")])
        self.assertIn("renderedText", candidates[0]["sourceFragment"])
        self.assertIn("attribute=href", candidates[1]["sourceFragment"])

    def test_allow_metered_jev_live_path_is_mocked_and_uses_anonymous_http_provenance(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        document = PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        recipe = {"version": 1, "startUrl": SOURCE, "allowedHosts": ["directory.example.test"], "allowedPaths": ["/"], "selection": None, "mapping": {"Name": "company"}, "pagination": {"mode": "none", "selector": None}, "limits": {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1, "maxPages": 1}}
        completed = AiResult("completed", (), (), "jev-1.14.0", None, None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()
            with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document), patch("lead_collector.jev.invoke_jev", return_value=completed) as invoke:
                exit_code = main(["assist", "--url", SOURCE, "--recipe", str(recipe_path), "--ai", "jev", "--allow-metered-jev"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertEqual(invoke.call_args.args[0].source.kind, "anonymous-http")
        self.assertTrue(invoke.call_args.kwargs["allow_metered"])

    def test_allow_metered_hybrid_reindexes_local_suggestions_before_mocked_jev(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        observation = ObservedValue("Acme Example", "Acme Example", SOURCE, "2026-10-31T00:00:00Z")
        local = AiResult("completed", (AiSuggestion("hash-id", "company", observation, True, None, None, None),), (), "ollama/qwen3:0.6b", None, None)
        completed = AiResult("completed", (), (), "jev-1.14.0", None, None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("lead_collector.cli.observed_now", return_value="2026-10-31T00:00:00Z"), patch("lead_collector.ai_runner.assist", return_value=local), patch("lead_collector.jev.invoke_jev", return_value=completed) as invoke:
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--ai", "hybrid", "--model", "ollama/qwen3:0.6b", "--allow-metered-jev"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertEqual([candidate.identifier for candidate in invoke.call_args.args[0].candidates], ["c0"])
        self.assertEqual(body["data"]["localAi"]["accepted"][0]["candidateId"], "c0")

    def test_allow_metered_jev_offline_still_rejects_before_key_lookup(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("lead_collector.jev.os.getenv") as getenv:
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--ai", "jev", "--allow-metered-jev"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2, body)
        self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")
        getenv.assert_not_called()

    def test_assist_sdk_capability_failure_is_an_outer_error_envelope(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        unavailable = AiResult("unavailable", (), (), None, None, AiError("SDK_UNAVAILABLE", "isolated AI runtime is unavailable"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("lead_collector.ai_runner.assist", return_value=unavailable):
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--ai", "scrapegraph", "--model", "ollama/qwen3:0.6b"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "SDK_UNAVAILABLE")

    def test_assist_replay_uses_only_owned_html_and_returns_contract_only_suggestions(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Copper Pine Demo</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            replay = root / "replay.json"
            page.write_text(html)
            preview_output = StringIO()
            with redirect_stdout(preview_output):
                preview_exit = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--request-preview"])
            request = json.loads(preview_output.getvalue())["data"]["request"]
            answers: dict[str, object] = {}
            for question_id, question in request["questions"].items():
                if question["type"] == "choice":
                    probabilities = {identifier: 0.0 for identifier in question["criteria"]}
                    probabilities["c0"] = 1.0
                    answers[question_id] = {"type": "choice", "choice": "c0", "confidence": 1.0, "probabilities": probabilities}
                else:
                    answers[question_id] = {"type": "noul", "noul": 1.0}
            replay.write_text(json.dumps({"model": "jev-1.14.0", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}))
            replay_output = StringIO()
            with redirect_stdout(replay_output), patch("socket.socket", side_effect=AssertionError("network disabled")):
                replay_exit = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--replay", str(replay)])

        body = json.loads(replay_output.getvalue())
        self.assertEqual(preview_exit, 0)
        self.assertEqual(replay_exit, 0, body)
        self.assertTrue(body["data"]["contractOnly"])
        self.assertTrue(body["data"]["ai"]["contractOnly"])
        self.assertEqual(body["data"]["ai"]["accepted"][0]["candidateId"], "c0")

    def test_assist_url_preview_is_rejected_before_fetch(self):
        output = StringIO()
        with redirect_stdout(output), patch.object(cli, "fetch_page") as fetch:
            exit_code = main(["assist", "--url", SOURCE, "--recipe", "not-read.json", "--request-preview"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(body["error"]["code"], "INPUT_INVALID")
        fetch.assert_not_called()

    def test_allow_metered_flag_does_not_activate_provider_paths_for_request_preview(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            output = StringIO()
            with redirect_stdout(output), patch("lead_collector.ai_runner.assist") as local, patch("lead_collector.jev.invoke_jev") as invoke:
                exit_code = main(["assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}), "--ai", "hybrid", "--allow-metered-jev", "--request-preview"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertTrue(body["data"]["contractOnly"])
        local.assert_not_called()
        invoke.assert_not_called()

    def test_assist_offline_subprocess_smoke_uses_the_machine_envelope(self):
        html = "<table><tr><th>Name</th></tr><tr><td>Acme Example</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            page.write_text(html)
            result = invoke(root, "assist", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}))

        body = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["rows"][0]["company"]["normalized"], "Acme Example")

    def test_url_preview_uses_one_static_fetch_without_creating_state(self):
        """URL preview must freeze a selection from one bounded page without a job write."""
        html = "<table><tr><th>Name</th></tr><tr><td>Acme</td></tr></table>"
        document = PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir).resolve() / "jobs.sqlite3"
            output = StringIO()

            with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document) as fetch:
                exit_code = main(["--state", str(state), "preview", "--url", SOURCE, "--mapping", json.dumps({"Name": "company"})])

            state_created = state.exists()
        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].allowed_paths, ("/list",))
        self.assertEqual(body["data"]["rows"][0]["company"]["normalized"], "Acme")
        self.assertFalse(state_created)

    def test_url_preview_applies_card_selection_and_returns_a_drift_guarded_recipe(self):
        html = "<article class='product_pod'><h3 class='name'>Copper Pine</h3></article>"
        document = PageDocument(
            SOURCE,
            html,
            "2026-10-31T00:00:00Z",
            hashlib.sha256(html.encode()).hexdigest(),
            "completed",
            None,
        )
        selection = json.dumps({"selector": ".product_pod", "columnSelectors": {"Name": ".name"}})
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            output = StringIO()
            with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document) as fetch:
                exit_code = main(
                    [
                        "preview",
                        "--url",
                        SOURCE,
                        "--mapping",
                        json.dumps({"Name": "company"}),
                        "--selection",
                        selection,
                    ]
                )
            body = json.loads(output.getvalue())
            recipe_path = root / "preview-recipe.json"
            recipe_path.write_text(json.dumps(body["data"]["recipe"]), encoding="utf-8")
            saved_recipe = RecipeV1.from_json(json.loads(recipe_path.read_text(encoding="utf-8")))

        self.assertEqual(exit_code, 0, body)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(body["data"]["rows"][0]["company"]["normalized"], "Copper Pine")
        self.assertEqual(saved_recipe.selection.selector, ".product_pod")
        self.assertTrue(saved_recipe.selection.fingerprint)
        drifted = "<article class='product_pod'><h2 class='name'>Copper Pine</h2></article>"
        drifted_document = PageDocument(SOURCE, drifted, "2026-10-31T00:00:00Z", hashlib.sha256(drifted.encode()).hexdigest(), "completed", None)
        with self.assertRaises(ValueError):
            preview_document(drifted_document, saved_recipe.mapping, saved_recipe.selection)

    def test_url_preview_rejects_malformed_selection_before_fetch(self):
        output = StringIO()
        with redirect_stdout(output), patch.object(cli, "fetch_page") as fetch:
            exit_code = main(
                [
                    "preview",
                    "--url",
                    SOURCE,
                    "--mapping",
                    json.dumps({"Name": "company"}),
                    "--selection",
                    json.dumps({"selector": 1}),
                ]
            )

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INPUT_INVALID")
        fetch.assert_not_called()

    def test_collect_reopens_serialized_next_recipe_and_follows_three_static_pages(self):
        """A saved next recipe must drive bounded static traversal from one URL seed."""
        urls = tuple(f"https://directory.example.test/page-{number}" for number in range(1, 4))
        recipe = {
            "version": 1,
            "startUrl": urls[0],
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "next", "selector": "a.next"},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        calls: list[str] = []

        def fetch(request):
            calls.append(request.url)
            number = urls.index(request.url) + 1
            next_link = "" if number == 3 else f"<a class='next' href='/page-{number + 1}'>Next</a>"
            html = f"<table><tr><th>Name</th></tr><tr><td>Company {number}</td></tr></table>{next_link}"
            return PageDocument(request.url, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()

            with redirect_stdout(output), patch.object(cli, "fetch_page", side_effect=fetch):
                exit_code = main(["--state", str(state), "collect", "--url", urls[0], "--recipe", str(recipe_path)])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(calls, list(urls))
        self.assertEqual({row["company"]["normalized"] for row in body["data"]["rows"]}, {"Company 1", "Company 2", "Company 3"})

    def test_url_collect_uses_persisted_ego_configuration_without_static_fallback(self):
        """Ego is explicit: the CLI stores its existing page identity and invokes no static fallback."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        html = "<table><tr><th>Name</th></tr><tr><td>Rendered</td></tr></table>"
        document = PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()

            with redirect_stdout(output), patch.object(cli, "capture_with_ego", return_value=document) as capture, patch.object(cli, "fetch_page") as fetch:
                exit_code = main(["--state", str(state), "collect", "--url", SOURCE, "--recipe", str(recipe_path), "--render", "ego", "--ego-task-space", "7", "--ego-page", "p1"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(capture.call_count, 1)
        fetch.assert_not_called()
        self.assertEqual(capture.call_args.args[0].task_space_id, 7)
        self.assertEqual(capture.call_args.args[0].page_label, "p1")
        self.assertEqual(body["data"]["checkpoint"]["renderer"], {"kind": "ego", "taskSpaceId": 7, "pageLabel": "p1"})

    def test_failed_renderer_job_is_a_named_failure_envelope_but_snapshot_remains_readable(self):
        """Reporting a persisted capability failure as ok:true would let callers mistake it for collection success."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        unavailable = PageDocument(SOURCE, "", "2026-10-31T00:00:00Z", hashlib.sha256(b"").hexdigest(), "unavailable", {"code": "CAPABILITY_UNAVAILABLE", "message": "safe", "url": None})
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            failed_output = StringIO()

            with redirect_stdout(failed_output), patch.object(cli, "capture_with_ego", return_value=unavailable):
                failed_exit = main(["--state", str(state), "collect", "--url", SOURCE, "--recipe", str(recipe_path), "--render", "ego", "--ego-task-space", "7", "--ego-page", "missing-page"])

            failed = json.loads(failed_output.getvalue())
            job_id = failed["data"]["id"]
            snapshot_output = StringIO()
            with redirect_stdout(snapshot_output):
                snapshot_exit = main(["--state", str(state), "snapshot", "--job", job_id])

        snapshot = json.loads(snapshot_output.getvalue())
        self.assertEqual(failed_exit, 3, failed)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(failed["data"]["state"], "failed")
        self.assertEqual(snapshot_exit, 0, snapshot)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["data"]["state"], "failed")

    def test_partial_layout_drift_is_a_named_failure_envelope_with_committed_rows(self):
        """A completed runner with layout drift must not become a false-success collection response."""
        first_html = "<table><tr><th>Name</th></tr><tr><td>First</td></tr></table><a class='next' href='/second'>Next</a>"
        first_document = PageDocument(SOURCE, first_html, "2026-10-31T00:00:00Z", hashlib.sha256(first_html.encode()).hexdigest(), "completed", None)
        _, selection = preview_document(first_document, {"Name": "company"})
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": selection.to_json(),
            "mapping": {"Name": "company"},
            "pagination": {"mode": "next", "selector": "a.next"},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        def fetch(request):
            html = first_html if request.url == SOURCE else "<table><tr><th>Name</th><th>Region</th></tr><tr><td>Second</td><td>West</td></tr></table>"
            return PageDocument(request.url, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()
            with redirect_stdout(output), patch.object(cli, "fetch_page", side_effect=fetch):
                exit_code = main(["--state", str(state), "collect", "--url", SOURCE, "--recipe", str(recipe_path)])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "LAYOUT_DRIFT")
        self.assertEqual(body["data"]["state"], "completed")
        self.assertEqual([row["company"]["normalized"] for row in body["data"]["rows"]], ["First"])

    def test_ego_render_requires_existing_task_space_and_page_before_state_write(self):
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(["--state", str(state), "collect", "--url", SOURCE, "--recipe", str(recipe_path), "--render", "ego"])

            state_created = state.exists()
        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(body["error"]["code"], "INPUT_INVALID")
        self.assertFalse(state_created)

    def test_remote_collect_contact_control_and_resume_commands_keep_global_state_first(self):
        """Task 3 network controls must retain the established state-file invocation shape."""
        parser = cli._parser()

        collect = parser.parse_args(["--state", "jobs.sqlite3", "collect", "--url", SOURCE, "--recipe", "recipe.json"])
        contacts = parser.parse_args(["--state", "jobs.sqlite3", "contacts", "--urls", "urls.txt", "--recipe", "recipe.json"])
        contact_rows = parser.parse_args(["--state", "jobs.sqlite3", "contacts", "--job", "source-job", "--row", "row-1", "--recipe", "recipe.json"])
        control = parser.parse_args(["--state", "jobs.sqlite3", "control", "--job", "job-1", "--action", "stop"])
        resume = parser.parse_args(["--state", "jobs.sqlite3", "resume", "--job", "job-1"])

        self.assertEqual((collect.command, collect.url), ("collect", SOURCE))
        self.assertEqual((contacts.command, contacts.urls.name), ("contacts", "urls.txt"))
        self.assertEqual((contact_rows.command, contact_rows.job, contact_rows.row), ("contacts", "source-job", ["row-1"]))
        self.assertEqual((control.command, control.action), ("control", "stop"))
        self.assertEqual((resume.command, resume.job), ("resume", "job-1"))

    def test_remote_collect_uses_static_transport_and_persists_frozen_checkpoint(self):
        """A URL collect must pass a bounded request seam and retain resume provenance."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        html = "<table><tr><th>Name</th></tr><tr><td>Acme</td></tr></table>"
        document = PageDocument(SOURCE, html, "2026-10-31T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = str(root / "jobs.sqlite3")
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe))
            output = StringIO()

            with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document) as fetch:
                exit_code = main(["--state", state, "collect", "--url", SOURCE, "--recipe", str(recipe_path)])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(fetch.call_args.args[0].url, SOURCE)
        self.assertEqual(body["data"]["state"], "completed")
        self.assertEqual(body["data"]["checkpoint"]["inputUrls"], [SOURCE])

    def test_contacts_from_source_rows_preserve_duplicate_urls_ids_and_source_snapshot(self):
        """Contact enrichment is a new job, even when distinct branches share one website URL."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        observed = "2026-10-31T00:00:00Z"
        website = ObservedValue(SOURCE, SOURCE, SOURCE, observed)
        first = LeadRow("north", ObservedValue("North", "North", SOURCE, observed), website, ObservedValue("1 Main", "1 Main", SOURCE, observed), edits={"company": "preserved"})
        second = LeadRow("south", ObservedValue("South", "South", SOURCE, observed), website, ObservedValue("2 Main", "2 Main", SOURCE, observed))
        html = "<a href='mailto:shared@example.test'>mail</a>"
        document = PageDocument(SOURCE, html, observed, hashlib.sha256(html.encode()).hexdigest(), "completed", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "jobs.sqlite3"
                recipe_path = root / "recipe.json"
                recipe_path.write_text(json.dumps(recipe))
                connection = open_store(state)
                save_job(connection, JobSnapshot("source", state="completed", rows=(first.to_json(), second.to_json())), 0)
                connection.close()
                output = StringIO()

                with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document):
                    exit_code = main(["--state", str(state), "contacts", "--job", "source", "--row", "north", "--row", "south", "--recipe", str(recipe_path)])

                connection = open_store(state)
                source = load_job(connection, "source")
                connection.close()
        body = json.loads(output.getvalue())
        assert source is not None
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual([row["id"] for row in body["data"]["rows"]], ["north", "south"])
        self.assertEqual({row["address"]["normalized"] for row in body["data"]["rows"]}, {"1 Main", "2 Main"})
        self.assertEqual(body["data"]["checkpoint"]["sourceRows"], {"north": [SOURCE], "south": [SOURCE]})
        self.assertEqual(body["data"]["progress"], {"attemptedPages": 1, "completedInputs": 2, "totalInputs": 2})
        self.assertEqual(source.rows, (first.to_json(), second.to_json()))

    def test_direct_contacts_keep_one_owner_across_redirect_and_contact_pages(self):
        """The CLI must seed one immutable row before I/O, not mint one row per crawled page."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        landing = "https://directory.example.test/landing"
        contact = "https://directory.example.test/contact"
        landing_html = "<a href='/contact'>Contact</a>"
        contact_html = "<a href='mailto:hello@example.test'>Email</a>"

        def fetch(request):
            if request.url == SOURCE:
                return PageDocument(landing, landing_html, "2026-10-31T00:00:00Z", hashlib.sha256(landing_html.encode()).hexdigest(), "completed", None)
            self.assertEqual(request.url, contact)
            return PageDocument(contact, contact_html, "2026-10-31T00:00:00Z", hashlib.sha256(contact_html.encode()).hexdigest(), "completed", None)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            urls_path = root / "urls.txt"
            recipe_path.write_text(json.dumps(recipe))
            urls_path.write_text(SOURCE + "\n")
            output = StringIO()

            with redirect_stdout(output), patch.object(cli, "fetch_page", side_effect=fetch):
                exit_code = main(["--state", str(state), "contacts", "--urls", str(urls_path), "--recipe", str(recipe_path)])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertEqual(body["data"]["progress"], {"attemptedPages": 2, "completedInputs": 1, "totalInputs": 1})
        self.assertEqual(len(body["data"]["rows"]), 1)
        row = body["data"]["rows"][0]
        self.assertEqual(row["website"]["normalized"], SOURCE)
        self.assertEqual(row["emails"][0]["normalized"], "hello@example.test")
        self.assertEqual(row["emails"][0]["sourceUrl"], contact)
        self.assertEqual(body["data"]["checkpoint"]["sourceRows"], {row["id"]: [SOURCE, landing, contact]})

    def test_direct_contact_empty_and_failure_remain_visible_on_the_seed_owner(self):
        """Each direct input remains an inspectable owner row even when its request has no contacts."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        for status, expected, expected_exit in (("empty", "empty", 0), ("timeout", "failed", 1)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir).resolve()
                state = root / "jobs.sqlite3"
                recipe_path = root / "recipe.json"
                urls_path = root / "urls.txt"
                recipe_path.write_text(json.dumps(recipe))
                urls_path.write_text(SOURCE + "\n")
                error = None if status == "empty" else {"code": "FETCH_TIMEOUT", "message": "safe", "url": SOURCE}
                document = PageDocument(SOURCE, "", "2026-10-31T00:00:00Z", hashlib.sha256(b"").hexdigest(), status, error)
                output = StringIO()

                with redirect_stdout(output), patch.object(cli, "fetch_page", return_value=document):
                    exit_code = main(["--state", str(state), "contacts", "--urls", str(urls_path), "--recipe", str(recipe_path)])

            body = json.loads(output.getvalue())
            self.assertEqual(exit_code, expected_exit, output.getvalue())
            self.assertEqual(body["ok"], status == "empty")
            self.assertEqual(len(body["data"]["rows"]), 1)
            row = body["data"]["rows"][0]
            self.assertEqual(row["status"], expected)
            self.assertEqual(set(body["data"]["checkpoint"]["sourceRows"]), {row["id"]})

    def test_contact_input_with_credentials_is_rejected_before_state_persistence(self):
        """Rejected URL input must not become a persisted failed row or reveal credentials."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            recipe_path = root / "recipe.json"
            urls_path = root / "urls.txt"
            recipe_path.write_text(json.dumps(recipe))
            urls_path.write_text("https://user:password@directory.example.test/list\n")
            output = StringIO()

            with redirect_stdout(output):
                exit_code = main(["--state", str(state), "contacts", "--urls", str(urls_path), "--recipe", str(recipe_path)])
            state_created = state.exists()

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(body["error"]["code"], "INPUT_INVALID")
        self.assertNotIn("password", body["error"]["message"])
        self.assertFalse(state_created)

    def test_doctor_reports_the_project_html_selector_version(self):
        """Hiding the selector dependency version would make parser diagnoses unreproducible."""
        result = subprocess.run(
            [sys.executable, "lead.py", "doctor"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["data"]
        self.assertEqual(data["soupsieve"], "2.10")
        self.assertTrue(data["ai"]["explicitLocalModelRequired"])
        self.assertEqual(data["ai"]["jevMetered"], "disabled")
        self.assertEqual(data["ai"]["modelCapability"], {"status": "unprobed"})

    def test_doctor_model_probe_uses_only_the_bounded_local_metadata_helper(self):
        output = StringIO()
        with redirect_stdout(output), patch("lead_collector.ai_runner._probe_local_model", return_value=None) as probe:
            exit_code = main(["doctor", "--model", "ollama/qwen3:0.6b"])

        body = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0, body)
        self.assertEqual(body["data"]["ai"]["modelCapability"], {"status": "available", "model": "ollama/qwen3:0.6b"})
        probe.assert_called_once_with("ollama/qwen3:0.6b")

    def test_table_preview_and_collect_reopen_all_evidence_in_separate_processes(self):
        """A collect that drops rows or snake-cases public evidence must fail this test."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = str(root / "jobs.sqlite3")
            preview = invoke(root, "--state", state, "preview", "--html", str(FIXTURES / "table_30.html"), "--source-url", SOURCE, "--mapping", MAPPING)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            preview_body = json.loads(preview.stdout)
            self.assertEqual(len(preview_body["data"]["rows"]), 30)
            self.assertIn("sourceUrl", preview_body["data"]["rows"][0]["company"])
            self.assertIn("observedAt", preview_body["data"]["rows"][0]["company"])
            self.assertEqual(preview_body["data"]["rows"][0]["website"]["raw"], "/company-01")

            collect = invoke(root, "--state", state, "collect", "--html", str(FIXTURES / "table_30.html"), "--source-url", SOURCE, "--mapping", MAPPING)
            self.assertEqual(collect.returncode, 0, collect.stderr)
            job_id = json.loads(collect.stdout)["data"]["id"]
            snapshot = invoke(root, "--state", state, "snapshot", "--job", job_id)
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            persisted = json.loads(snapshot.stdout)["data"]
            self.assertEqual(len(persisted["rows"]), 30)
            self.assertEqual(persisted["rows"], json.loads(collect.stdout)["data"]["rows"])
            self.assertEqual(persisted["recipe"]["selection"]["selector"], "table")
            self.assertEqual(persisted["recipe"]["mapping"], json.loads(MAPPING))
            self.assertEqual(persisted["progress"]["attemptedPages"], 1)

    def test_offline_collect_caps_default_rows_and_persists_the_limited_outcome(self):
        """An owned HTML list must retain only the documented default row budget."""
        html = "<table><tr><th>Name</th></tr>" + "".join(f"<tr><td>Business {index}</td></tr>" for index in range(1001)) + "</table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            page = root / "directory.html"
            page.write_text(html, encoding="utf-8")
            collected = invoke(root, "--state", str(state), "collect", "--html", str(page), "--source-url", SOURCE, "--mapping", json.dumps({"Name": "company"}))
            body = json.loads(collected.stdout)
            reopened = invoke(root, "--state", str(state), "snapshot", "--job", body["data"]["id"])

        persisted = json.loads(reopened.stdout)["data"]
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["data"]["rows"]), 1000)
        self.assertEqual(body["data"]["limits"]["maxRows"], 1000)
        self.assertEqual(body["data"]["progress"], {"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1})
        self.assertEqual(body["data"]["errors"][-1], {"code": "JOB_FAILED", "message": "row limit reached", "url": SOURCE, "status": "limited"})
        self.assertEqual(persisted["rows"], body["data"]["rows"])
        self.assertEqual(persisted["errors"], body["data"]["errors"])

    def test_offline_collect_recipe_uses_its_mapping_and_lower_row_limit(self):
        """A supplied HTML recipe controls mapping and its smaller persisted row budget."""
        recipe = {
            "version": 1,
            "startUrl": SOURCE,
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/"],
            "selection": None,
            "mapping": {"Company": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 2, "maxPages": 1},
        }
        html = "<table><tr><th>Company</th></tr><tr><td>One</td></tr><tr><td>Two</td></tr><tr><td>Three</td></tr></table>"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            page = root / "directory.html"
            recipe_path = root / "recipe.json"
            page.write_text(html, encoding="utf-8")
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            collected = invoke(root, "--state", str(state), "collect", "--html", str(page), "--source-url", SOURCE, "--recipe", str(recipe_path))

        body = json.loads(collected.stdout)
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual([row["company"]["normalized"] for row in body["data"]["rows"]], ["One", "Two"])
        self.assertEqual(body["data"]["limits"]["maxRows"], 2)
        self.assertEqual(body["data"]["recipe"]["mapping"], {"Company": "company"})
        self.assertEqual(body["data"]["errors"][-1]["status"], "limited")

    def test_offline_collect_recipe_rejects_an_out_of_scope_source_before_state_write(self):
        """A supplied recipe must constrain owned HTML provenance before persistence."""
        recipe = {
            "version": 1,
            "startUrl": "https://directory.example.test/approved",
            "allowedHosts": ["directory.example.test"],
            "allowedPaths": ["/approved"],
            "selection": None,
            "mapping": {"Name": "company"},
            "pagination": {"mode": "none", "selector": None},
            "limits": {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 2, "maxPages": 1},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            page = root / "directory.html"
            recipe_path = root / "recipe.json"
            page.write_text("<table><tr><th>Name</th></tr><tr><td>Out of scope</td></tr></table>", encoding="utf-8")
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            collected = invoke(root, "--state", str(state), "collect", "--html", str(page), "--source-url", SOURCE, "--recipe", str(recipe_path))
            state_created = state.exists()

        body = json.loads(collected.stdout)
        self.assertEqual(collected.returncode, 2, collected.stderr)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "INPUT_INVALID")
        self.assertFalse(state_created)

    def test_offline_collect_recipe_rejects_layout_drift_before_state_write(self):
        """A supplied recipe selection remains drift-guarded for owned HTML collection."""
        original = "<article class='card'><h2 class='name'>Original</h2></article>"
        drifted = "<article class='card'><h3 class='name'>Drifted</h3></article>"
        document = PageDocument(SOURCE, original, "2026-10-31T00:00:00Z", hashlib.sha256(original.encode()).hexdigest(), "completed", None)
        configured = Selection(".card", "", ("Name",), (), {"Name": ".name"})
        _, selection = preview_document(document, {"Name": "company"}, configured)
        recipe = RecipeV1(SOURCE, ("directory.example.test",), ("/",), selection, {"Name": "company"}, {"mode": "none", "selector": None}, {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 2, "maxPages": 1})
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "jobs.sqlite3"
            page = root / "directory.html"
            recipe_path = root / "recipe.json"
            page.write_text(drifted, encoding="utf-8")
            recipe_path.write_text(json.dumps(recipe.to_json()), encoding="utf-8")
            collected = invoke(root, "--state", str(state), "collect", "--html", str(page), "--source-url", SOURCE, "--recipe", str(recipe_path))
            state_created = state.exists()

        body = json.loads(collected.stdout)
        self.assertEqual(collected.returncode, 2, collected.stderr)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "LAYOUT_DRIFT")
        self.assertFalse(state_created)

    def test_card_collect_uses_argv_selection_and_mapping(self):
        """Ignoring card selection or argv mapping must fail this test."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = str(root / "jobs.sqlite3")
            result = invoke(root, "--state", state, "collect", "--html", str(FIXTURES / "cards_30.html"), "--source-url", SOURCE, "--mapping", MAPPING, "--selection", CARD_SELECTION)
            self.assertEqual(result.returncode, 0, result.stderr)
            body = json.loads(result.stdout)
            self.assertEqual(len(body["data"]["rows"]), 30)
            self.assertEqual(body["data"]["rows"][29]["company"]["normalized"], "Card Company 30")
            reopened = invoke(root, "--state", state, "snapshot", "--job", body["data"]["id"])
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            self.assertEqual(json.loads(reopened.stdout)["data"]["rows"], body["data"]["rows"])

    def test_foreign_sqlite_is_rejected_without_changing_its_bytes(self):
        """Relabelling a foreign database must fail this test."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "foreign.sqlite3"
            foreign = sqlite3.connect(state)
            foreign.execute("CREATE TABLE customer_data (name TEXT)")
            foreign.commit()
            foreign.close()
            before = state.read_bytes()
            result = invoke(root, "--state", str(state), "snapshot", "--job", "missing-job")
            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")
            self.assertEqual(state.read_bytes(), before)

    def test_magic_header_corrupt_sqlite_is_rejected_without_changing_its_bytes(self):
        """Treating malformed state as a runtime job failure would hide unsafe input."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            state = root / "corrupt.sqlite3"
            state.write_bytes(b"SQLite format 3\x00this-is-not-a-database")
            before = state.read_bytes()

            result = invoke(root, "--state", str(state), "snapshot", "--job", "missing-job")

            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")
            self.assertEqual(state.read_bytes(), before)

    def test_unsafe_input_path_is_rejected_before_opening(self):
        """Opening a credential-like path before validation must fail this test."""
        result = invoke(ROOT, "--state", "unsafe-input.sqlite3", "preview", "--html", "never-read-secret.env", "--source-url", SOURCE, "--mapping", MAPPING)
        body = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")
        self.assertNotIn("never-read-secret.env", body["error"]["message"])

    def test_symlink_and_oversized_html_are_rejected_before_parsing(self):
        """Following a symlink or reading past the HTML bound must fail this test."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir).resolve()
            target = temp / "source.html"
            target.write_text("<table></table>")
            symlink = temp / "alias.html"
            symlink.symlink_to(target)
            linked = invoke(temp, "--state", "symlink-input.sqlite3", "preview", "--html", str(symlink), "--source-url", SOURCE, "--mapping", MAPPING)
            self.assertEqual(json.loads(linked.stdout)["error"]["code"], "INPUT_UNSAFE")
            oversized = temp / "large.html"
            oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            large = invoke(temp, "--state", "oversized-input.sqlite3", "preview", "--html", str(oversized), "--source-url", SOURCE, "--mapping", MAPPING)
            self.assertEqual(json.loads(large.stdout)["error"]["code"], "HTML_TOO_LARGE")

    def test_parent_symlink_html_path_is_rejected_before_opening(self):
        """Opening a regular leaf through a symlinked parent would bypass offline input policy."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            source = root / "source"
            source.mkdir()
            page = source / "page.html"
            page.write_text("<table></table>")
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(source, target_is_directory=True)

            result = invoke(root, "--state", "parent-symlink.sqlite3", "preview", "--html", str(linked_parent / "page.html"), "--source-url", SOURCE, "--mapping", MAPPING)

            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")

    def test_credential_like_parent_directory_is_rejected_before_opening(self):
        """A safe-looking filename must not permit reads from credential-like directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            sensitive = root / "credentials"
            sensitive.mkdir()
            page = sensitive / "page.html"
            page.write_text("<table></table>")

            result = invoke(root, "--state", "credential-parent.sqlite3", "preview", "--html", str(page), "--source-url", SOURCE, "--mapping", MAPPING)

            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(body["error"]["code"], "INPUT_UNSAFE")

    def test_html_read_uses_the_opened_descriptor_when_leaf_path_is_swapped(self):
        """Reopening a checked path after replacement would parse attacker-controlled content."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            page = root / "page.html"
            replacement = root / "replacement.html"
            page.write_text("<table><tr><td>original</td></tr></table>")
            replacement.write_text("<table><tr><td>replacement</td></tr></table>")
            original_open = Path.open

            def swap_before_open(path: Path, *args: object, **kwargs: object):
                if path == page:
                    replacement.replace(page)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=swap_before_open):
                html = cli._read_html(page)

            self.assertIn("original", html)
            self.assertNotIn("replacement", html)

    def test_invalid_mapping_and_unsafe_state_return_sanitized_named_envelopes(self):
        """Opening an unsafe state or accepting non-object JSON must fail this test."""
        mapping = invoke("preview", "--html", str(FIXTURES / "table_30.html"), "--source-url", SOURCE, "--mapping", "[]")
        state = invoke("--state", "never-read-token.env", "snapshot", "--job", "job")
        for result in (mapping, state):
            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(body["error"]["code"] in {"INPUT_INVALID", "INPUT_UNSAFE"})
            self.assertNotIn("never-read-token.env", body["error"]["message"])

    def test_offline_preview_never_opens_a_network_socket(self):
        """Adding a network fetch to offline preview must fail this test."""
        with redirect_stdout(StringIO()), patch("socket.socket", side_effect=AssertionError("network disabled")):
            exit_code = main(["preview", "--html", str(FIXTURES / "table_30.html"), "--source-url", SOURCE, "--mapping", MAPPING])
        self.assertEqual(exit_code, 0)
