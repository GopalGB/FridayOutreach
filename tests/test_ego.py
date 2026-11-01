import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lead_collector.ego import CAPTURE_TIMEOUT_SECONDS, EGO_SUBRESOURCE_LIMITATION, MAX_STDOUT_BYTES, SCRIPT_PATH, EgoCaptureRequest, _receipt, _request_payload, capture_with_ego
from lead_collector.model import Selection


SOURCE = "https://directory.example.test/list"
LIMITS = {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100}
NODE_BINARY = Path("/opt/homebrew/bin/node")
SCOPE_FIXTURE = Path(__file__).parent / "fixtures" / "ego-preaction-scope.mjs"
WAIT_FIXTURE = Path(__file__).parent / "fixtures" / "ego-wait-timeout.mjs"
UNAVAILABLE_FIXTURE = Path(__file__).parent / "fixtures" / "ego-unavailable.mjs"
NEXT_URL_FIXTURE = Path(__file__).parent / "fixtures" / "ego-next-url-change.mjs"
NEXT_READY_FIXTURE = Path(__file__).parent / "fixtures" / "ego-next-selection-ready.mjs"
UNSAFE_HOST_FIXTURE = Path(__file__).parent / "fixtures" / "ego-preaction-unsafe-host.mjs"
DOM_BOUNDARY_FIXTURE = Path(__file__).parent / "fixtures" / "ego-dom-boundaries.mjs"


def request(operation: str = "navigate") -> EgoCaptureRequest:
    return EgoCaptureRequest(7, "p1", SOURCE, ("directory.example.test",), ("/",), Selection("table", "", (), ()), {"mode": "none", "selector": None}, LIMITS, 0, operation)


class EgoCaptureTests(unittest.TestCase):
    def test_request_validation_rejects_invalid_scope_before_launch(self):
        invalid = EgoCaptureRequest(0, "", "https://user:password@directory.example.test/list", ("directory.example.test",), ("/",), Selection("table", "", (), ()), {"mode": "none", "selector": None}, LIMITS, 0, "navigate")

        with patch("lead_collector.ego.subprocess.Popen") as popen:
            result = capture_with_ego(invalid)

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.error["code"], "TARGET_DENIED")
        popen.assert_not_called()

    def test_payload_canonicalizes_public_idn_allowlist_and_rejects_unsafe_secondary_hosts(self):
        """Forwarding raw local or literal allowlist entries to a persistent page must fail this boundary."""
        idn = EgoCaptureRequest(7, "p1", "https://BÜCHER.example/list", ("BÜCHER.example.",), ("/",), None, {"mode": "none", "selector": None}, LIMITS, 0, "navigate")

        payload = _request_payload(idn)

        self.assertEqual(payload["startUrl"], "https://xn--bcher-kva.example/list")
        self.assertEqual(payload["allowedHosts"], ["xn--bcher-kva.example"])
        for unsafe in ("localhost", "127.0.0.1", "intranet"):
            with self.subTest(unsafe=unsafe), patch("lead_collector.ego.subprocess.Popen") as popen:
                rejected = EgoCaptureRequest(7, "p1", SOURCE, ("directory.example.test", unsafe), ("/",), None, {"mode": "none", "selector": None}, LIMITS, 0, "read")
                result = capture_with_ego(rejected)

                self.assertEqual(result.status, "denied")
                popen.assert_not_called()

    def test_ego_command_uses_argv_stdin_and_no_shell(self):
        process = MagicMock()
        scripts: list[str] = []

        def communicate(script, timeout):
            scripts.append(script.decode("utf-8"))
            match = re.search(r'\\"outputPath\\":\\"([^"\\]+)\\"', scripts[0])
            assert match is not None
            output = Path(match.group(1))
            html = "<table><tr><th>Name</th></tr><tr><td>Acme</td></tr></table>"
            record = {"url": SOURCE, "html": html, "observedAt": "2026-10-31T00:00:00Z", "sha256": hashlib.sha256(html.encode()).hexdigest(), "status": "completed", "error": None}
            output.write_text(json.dumps(record) + "\n")
            receipt = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "records": 1}
            return (b"EGO_RECEIPT " + json.dumps(receipt).encode() + b"\n", b"")

        process.communicate.side_effect = communicate
        process.returncode = 0
        with patch("lead_collector.ego.subprocess.Popen", return_value=process) as popen:
            result = capture_with_ego(request())

        args, kwargs = popen.call_args
        script = scripts[0]
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(args[0], ["ego-browser", "nodejs"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs["start_new_session"])
        self.assertIn("taskSpace(request.taskSpaceId)", script)
        self.assertNotIn("newPage", script)

    def test_stderr_receipt_with_blank_trailing_lines_is_accepted(self):
        """Ego's native CLI writes its one receipt on stderr after valid result-file output."""
        process = MagicMock()

        def communicate(script, timeout):
            match = re.search(r'\\"outputPath\\":\\"([^"\\]+)\\"', script.decode("utf-8"))
            assert match is not None
            output = Path(match.group(1))
            html = "<table><tr><th>Name</th></tr><tr><td>Native Shape</td></tr></table>"
            record = {"url": SOURCE, "html": html, "observedAt": "2026-10-31T00:00:00Z", "sha256": hashlib.sha256(html.encode()).hexdigest(), "status": "completed", "error": None}
            output.write_text(json.dumps(record) + "\n")
            receipt = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "records": 1}
            return b"", b"EGO_RECEIPT " + json.dumps(receipt).encode() + b"\n\n"

        process.communicate.side_effect = communicate
        process.returncode = 0
        with patch("lead_collector.ego.subprocess.Popen", return_value=process):
            result = capture_with_ego(request())

        self.assertEqual(result.status, "completed", result.error)
        self.assertIn("Native Shape", result.html)

    def test_terminal_browser_outcome_is_preserved_for_the_runner(self):
        """A browser action failure is a named terminal result, not malformed success."""
        process = MagicMock()

        def communicate(script, timeout):
            match = re.search(r'\\"outputPath\\":\\"([^"\\]+)\\"', script.decode("utf-8"))
            assert match is not None
            output = Path(match.group(1))
            record = {
                "url": SOURCE,
                "html": "",
                "observedAt": "2026-10-31T00:00:00Z",
                "sha256": hashlib.sha256(b"").hexdigest(),
                "status": "timeout",
                "error": {"code": "FETCH_TIMEOUT", "message": "browser action timed out and may have late effects", "url": None},
            }
            output.write_text(json.dumps(record) + "\n")
            receipt = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "records": 1}
            return b"", b"EGO_RECEIPT " + json.dumps(receipt).encode() + b"\n\n"

        process.communicate.side_effect = communicate
        process.returncode = 0
        with patch("lead_collector.ego.subprocess.Popen", return_value=process):
            result = capture_with_ego(request("advance"))

        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.error["code"], "FETCH_TIMEOUT")

    def test_template_classifies_missing_or_unowned_resources_without_create_or_claim(self):
        """The production template must distinguish unavailable TaskSpace/Page errors from action failures."""
        for missing in ("space", "page"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result.jsonl"
                completed = subprocess.run([str(NODE_BINARY), str(UNAVAILABLE_FIXTURE), str(SCRIPT_PATH), str(output), missing], capture_output=True, check=False)
                record = json.loads(output.read_text(encoding="utf-8"))

                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
                self.assertEqual(record["status"], "unavailable")
                self.assertEqual(record["error"]["code"], "CAPABILITY_UNAVAILABLE")
        script = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("claimTaskSpace", script)
        self.assertNotIn("finish(", script)

    def test_timeout_stops_the_owned_child_before_returning_timeout(self):
        process = MagicMock()
        process.communicate.side_effect = subprocess.TimeoutExpired(["ego-browser", "nodejs"], CAPTURE_TIMEOUT_SECONDS)

        with patch("lead_collector.ego.subprocess.Popen", return_value=process), patch("lead_collector.ego._stop_child") as stop:
            result = capture_with_ego(request())

        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.error["code"], "FETCH_TIMEOUT")
        stop.assert_called_once_with(process)

    def test_script_has_no_autonomous_loop_forms_or_page_creation(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("taskSpace(request.taskSpaceId)", script)
        self.assertIn("page.goto", script)
        self.assertIn("page.click", script)
        self.assertNotIn("newPage", script)
        self.assertNotIn("claimTaskSpace", script)
        self.assertNotIn(".fill(", script)
        self.assertNotIn("while (", script)
        self.assertNotIn("for (", script)
        self.assertIn('waitUntil: "domcontentloaded"', script)
        self.assertIn("page.waitForFunction", script)
        self.assertIn("TARGET_DENIED", script)
        self.assertIn('outcome("empty", "EXHAUSTED"', script)
        self.assertIn("not isolated", EGO_SUBRESOURCE_LIMITATION)

    def test_preaction_scope_fixture_denies_a_nondefault_port_before_dom_access(self):
        """A persistent page outside the canonical target scope must not be inspected or acted on."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.jsonl"
            completed = subprocess.run([str(NODE_BINARY), str(SCOPE_FIXTURE), str(SCRIPT_PATH), str(output)], capture_output=True, check=False)

            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
            record = json.loads(output.read_text(encoding="utf-8"))

        self.assertIn(b"EGO_RECEIPT ", completed.stdout)
        self.assertEqual(record["status"], "denied")
        self.assertEqual(record["error"]["code"], "TARGET_DENIED")

    def test_template_denies_local_and_literal_preaction_hosts_before_dom_access(self):
        """A direct malformed template payload must still deny local hosts before reading the page."""
        for hostname in ("localhost", "127.0.0.1"):
            with self.subTest(hostname=hostname), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result.jsonl"
                completed = subprocess.run([str(NODE_BINARY), str(UNSAFE_HOST_FIXTURE), str(SCRIPT_PATH), str(output), hostname], capture_output=True, check=False)
                record = json.loads(output.read_text(encoding="utf-8"))

                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
                self.assertEqual(record["status"], "denied")
                self.assertEqual(record["error"]["code"], "TARGET_DENIED")

    def test_native_wait_timeout_message_is_classified_for_actions_and_scroll(self):
        """Ego's documented timeout spelling must stay distinct from an action failure."""
        for mode, expected_status in (("loadMore", "timeout"), ("scroll", "completed")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result.jsonl"
                completed = subprocess.run([str(NODE_BINARY), str(WAIT_FIXTURE), str(SCRIPT_PATH), str(output), mode], capture_output=True, check=False)
                record = json.loads(output.read_text(encoding="utf-8"))

                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
                self.assertEqual(record["status"], expected_status)
                self.assertEqual(record["error"], None if mode == "scroll" else {"code": "FETCH_TIMEOUT", "message": "browser action timed out and may have late effects", "url": None})

    def test_template_executes_dom_callbacks_for_navigation_and_action_boundaries(self):
        """The fixture executes production evaluate callbacks against owned DOM state, not canned results."""
        for scenario, status in (("read", "completed"), ("navigate", "completed"), ("anchor", "completed"), ("button", "completed"), ("scroll", "completed"), ("missing", "empty"), ("denied", "denied"), ("oversize", "truncated"), ("timeout", "timeout")):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result.jsonl"
                completed = subprocess.run([str(NODE_BINARY), str(DOM_BOUNDARY_FIXTURE), str(SCRIPT_PATH), str(output), scenario], capture_output=True, check=False)
                record = json.loads(output.read_text(encoding="utf-8"))

                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
                self.assertEqual(record["status"], status)

    def test_next_accepts_a_scoped_url_change_when_selected_content_repeats(self):
        """Treating URL-only Next navigation as unchanged must fail this template-level regression."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.jsonl"
            completed = subprocess.run([str(NODE_BINARY), str(NEXT_URL_FIXTURE), str(SCRIPT_PATH), str(output)], capture_output=True, check=False)
            record = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["url"], "https://directory.example.test/page-2")

    def test_next_waits_for_selected_dom_after_url_progress_before_capture(self):
        """Capturing immediately after URL-only progress must fail when AJAX rows are not yet present."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.jsonl"
            completed = subprocess.run([str(NODE_BINARY), str(NEXT_READY_FIXTURE), str(SCRIPT_PATH), str(output)], capture_output=True, check=False)
            record = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(record["status"], "completed")
        self.assertIn("ready", record["html"])

    def test_noisy_duplicate_or_bad_receipts_fail_without_returning_dom(self):
        for stdout in (b"noise\nEGO_RECEIPT {}\n", b"EGO_RECEIPT {}\nEGO_RECEIPT {}\n", b"EGO_RECEIPT {not-json}\n"):
            with self.subTest(stdout=stdout):
                process = MagicMock()
                process.returncode = 0
                process.communicate.return_value = (stdout, b"")
                with patch("lead_collector.ego.subprocess.Popen", return_value=process):
                    result = capture_with_ego(request())
                self.assertEqual(result.status, "malformed")
                self.assertEqual(result.html, "")

    def test_receipt_channels_reject_noise_duplicates_and_two_receipts(self):
        receipt = b'EGO_RECEIPT {"path":"/safe/result.jsonl","sha256":"0000000000000000000000000000000000000000000000000000000000000000","records":1}\n'

        self.assertIsNone(_receipt(receipt, receipt))
        self.assertIsNone(_receipt(receipt + b"noise\n", b""))
        self.assertIsNone(_receipt(receipt, b"noise\n"))
        self.assertIsNotNone(_receipt(b"\n", receipt))

    def test_combined_diagnostic_output_limit_is_enforced(self):
        process = MagicMock()
        process.returncode = 0
        process.communicate.return_value = (b"x" * (MAX_STDOUT_BYTES // 2 + 1), b"y" * (MAX_STDOUT_BYTES // 2 + 1))

        with patch("lead_collector.ego.subprocess.Popen", return_value=process):
            result = capture_with_ego(request())

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.error["code"], "CAPABILITY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
