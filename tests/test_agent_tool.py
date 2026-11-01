"""Black-box tests for the provider-neutral agent adapter."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import cast
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1].resolve()
RUNNER = ROOT / ".venv" / "bin" / "python"
SCRIPT = ROOT / "scripts" / "agent_tool.py"
SCHEMA = ROOT / "integrations" / "function-tool.json"
SPEC = importlib.util.spec_from_file_location("agent_tool_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
agent_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_tool)


class _BinaryStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = BytesIO(payload)


class AgentToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.workspace = Path(self._temporary.name).resolve()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def invoke(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(RUNNER), str(SCRIPT), "--workspace", str(self.workspace)], input=payload, text=True, capture_output=True, cwd=self.workspace, check=False, timeout=10)

    def invoke_in_process(self, payload: bytes, workspace: Path | None = None) -> tuple[int, dict[str, object]]:
        output = StringIO()
        with patch.object(agent_tool.sys, "stdin", _BinaryStdin(payload)), redirect_stdout(output):
            status = agent_tool.run(["--workspace", str(workspace or self.workspace)])
        return status, json.loads(output.getvalue())

    def error_code(self, body: dict[str, object]) -> str:
        candidate = body["error"]
        if not isinstance(candidate, dict):
            self.fail("runner did not emit an error object")
        error = cast(dict[str, object], candidate)
        code = error["code"]
        if not isinstance(code, str):
            self.fail("runner did not emit a string error code")
        return code

    def test_omitted_verification_fails_before_fetch_or_output_when_runtime_is_missing(self) -> None:
        (self.workspace / "businesses.csv").write_text("company,website\nAcme,https://acme.example\n", encoding="utf-8")
        patcher = "from unittest.mock import Mock; import lead_collector.batch as b; b._trusted_binary=lambda _: None; fetch=Mock(side_effect=AssertionError('fetch_page called before runtime preflight')); write=Mock(side_effect=AssertionError('write_export called before runtime preflight')); b.fetch_page=fetch; b.write_export=write; from scripts.agent_tool import run; import sys; code=run(['--workspace', sys.argv[2]]); assert not fetch.called; assert not write.called; raise SystemExit(code)"
        result = subprocess.run([str(RUNNER), "-c", patcher, "--workspace", str(self.workspace)], input=json.dumps({"input_csv": "businesses.csv", "output_csv": "enriched.csv"}), text=True, capture_output=True, cwd=ROOT, check=False, timeout=10)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertFalse((self.workspace / "enriched.csv").exists())

    def test_runs_from_outside_checkout_with_a_blank_website(self) -> None:
        (self.workspace / "businesses.csv").write_text("company,website\nAcme,\n", encoding="utf-8")
        result = self.invoke(json.dumps({"input_csv": "businesses.csv", "output_csv": "enriched.csv", "verify_emails": False}))
        body = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(body["ok"])
        self.assertTrue((self.workspace / "enriched.csv").is_file())

    def test_rejects_path_escape_and_symlinks_without_output(self) -> None:
        (self.workspace / "businesses.csv").write_text("company,website\nAcme,\n", encoding="utf-8")
        (self.workspace / "linked.csv").symlink_to(self.workspace / "businesses.csv")
        for request in (
            {"input_csv": "../secret.csv", "output_csv": "out.csv"},
            {"input_csv": "linked.csv", "output_csv": "out.csv"},
            {"input_csv": "businesses.csv", "output_csv": "../out.csv"},
        ):
            with self.subTest(request=request):
                result = self.invoke(json.dumps(request))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "INPUT_INVALID")
        self.assertFalse((self.workspace / "out.csv").exists())

    def test_deep_duplicate_and_unknown_json_return_a_named_error_without_traceback(self) -> None:
        deep = '{"extra":' + "[" * 1500 + "0" + "]" * 1500 + "}"
        for payload in (deep, '{"input_csv":"a.csv","input_csv":"b.csv","output_csv":"out.csv"}', '{"input_csv":"a.csv","output_csv":"out.csv","unknown":true}'):
            with self.subTest(payload=payload[:30]):
                result = self.invoke(payload)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["error"]["code"], "INPUT_INVALID")
                self.assertNotIn("Traceback", result.stderr)

    def test_in_process_runner_enriches_an_owned_workspace_file(self) -> None:
        self.workspace.joinpath("businesses.csv").write_text("company,website\nAcme,\n", encoding="utf-8")
        self.workspace.joinpath("enriched.csv").write_text("old output", encoding="utf-8")
        status, body = self.invoke_in_process(
            json.dumps({"input_csv": "businesses.csv", "output_csv": "enriched.csv", "verify_emails": False, "overwrite": True, "max_sites": 1, "max_pages_per_site": 1, "max_emails": 1}).encode()
        )
        self.assertEqual(status, 0)
        self.assertTrue(body["ok"])
        self.assertTrue(self.workspace.joinpath("enriched.csv").is_file())

    def test_in_process_runner_rejects_malformed_requests_without_writing_output(self) -> None:
        deep = b'{"input_csv":"businesses.csv","output_csv":"out.csv","extra":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
        duplicate = b'{"input_csv":"one.csv","input_csv":"two.csv","output_csv":"out.csv"}'
        requests = (
            b"",
            b"\xff",
            b"x" * (agent_tool.MAX_REQUEST_BYTES + 1),
            deep,
            duplicate,
            b'{"input_csv":"businesses.csv","output_csv":"out.csv","unknown":true}',
            b'{"input_csv":"businesses.csv"}',
            b'{"input_csv":true,"output_csv":"out.csv"}',
            b'{"input_csv":"businesses.csv","output_csv":"out.csv","max_sites":true}',
            b'{"input_csv":"businesses.csv","output_csv":"out.csv","max_pages_per_site":6}',
            b'{"input_csv":"businesses.csv","output_csv":"out.csv","max_emails":1001}',
        )
        for request in requests:
            with self.subTest(request=request[:40]):
                status, body = self.invoke_in_process(request)
                self.assertEqual(status, 2)
                self.assertEqual(body["error"], {"code": "INPUT_INVALID", "message": "invalid function arguments"})
                self.assertFalse(self.workspace.joinpath("out.csv").exists())

    def test_in_process_runner_rejects_unsafe_files_and_invalid_workspaces(self) -> None:
        self.workspace.joinpath("businesses.csv").write_text("company,website\nAcme,\n", encoding="utf-8")
        self.workspace.joinpath("linked.csv").symlink_to(self.workspace / "businesses.csv")
        os.link(self.workspace / "businesses.csv", self.workspace / "hard-linked.csv")
        non_directory = self.workspace / "not-a-workspace"
        non_directory.write_text("not a directory", encoding="utf-8")
        requests: tuple[tuple[dict[str, object], Path], ...] = (
            ({"input_csv": "../outside.csv", "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": str(self.workspace / "businesses.csv"), "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": ".ssh/input.csv", "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": "businesses.csv", "output_csv": ".ssh/out.csv"}, self.workspace),
            ({"input_csv": "linked.csv", "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": "hard-linked.csv", "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": "missing.csv", "output_csv": "out.csv"}, self.workspace),
            ({"input_csv": "businesses.csv", "output_csv": "out.csv"}, non_directory),
        )
        for request, workspace in requests:
            with self.subTest(request=request, workspace=workspace.name):
                status, body = self.invoke_in_process(json.dumps(request).encode(), workspace)
                self.assertEqual(status, 2)
                self.assertEqual(self.error_code(body), "INPUT_INVALID")
                self.assertFalse(self.workspace.joinpath("out.csv").exists())

    def test_schema_and_parser_accept_the_same_documented_arguments(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["input_schema"]
        expected = {"input_csv", "output_csv", "verify_emails", "max_sites", "max_pages_per_site", "max_emails", "overwrite"}
        request = {
            "input_csv": "businesses.csv",
            "output_csv": "enriched.csv",
            "verify_emails": True,
            "max_sites": 100,
            "max_pages_per_site": 5,
            "max_emails": 1000,
            "overwrite": False,
        }
        self.assertEqual(set(schema["properties"]), expected)
        self.assertTrue(schema["properties"]["verify_emails"]["default"])
        self.assertEqual(agent_tool._request(json.dumps(request).encode()), request)

    def test_agent_command_defaults_to_verification_and_supports_exact_opt_out(self) -> None:
        (self.workspace / "businesses.csv").write_text("company,website\nAcme,\n", encoding="utf-8")
        default = agent_tool._command({"input_csv": "businesses.csv", "output_csv": "enriched.csv"}, self.workspace)
        opted_out = agent_tool._command({"input_csv": "businesses.csv", "output_csv": "enriched.csv", "verify_emails": False}, self.workspace)
        self.assertIn("--verify-emails", default)
        self.assertNotIn("--skip-email-verification", default)
        self.assertIn("--skip-email-verification", opted_out)
        self.assertNotIn("--verify-emails", opted_out)


if __name__ == "__main__":
    unittest.main()
