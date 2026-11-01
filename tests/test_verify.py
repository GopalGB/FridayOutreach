from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("verify_script", Path(__file__).parents[1] / "scripts" / "verify.py")
assert SPEC is not None and SPEC.loader is not None
verify_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_script)


class VerifyTests(unittest.TestCase):
    def test_main_requires_the_project_core_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with redirect_stderr(output), patch.object(verify_script, "PROJECT", Path(directory)):
                self.assertEqual(verify_script.main(), 2)
            self.assertIn("VERIFY_CORE_RUNTIME_MISSING", output.getvalue())

    def test_main_reports_missing_developer_tool_without_installing_it(self) -> None:
        output = StringIO()
        with redirect_stderr(output), patch.object(verify_script, "_core_python", return_value=Path("/runtime/python")), patch.object(
            verify_script, "_tool", side_effect=verify_script.VerifyError("VERIFY_DEV_TOOL_MISSING: ruff is required")
        ), patch.object(verify_script, "_run") as run:
            self.assertEqual(verify_script.main(), 2)
        run.assert_not_called()
        self.assertIn("VERIFY_DEV_TOOL_MISSING", output.getvalue())

    def test_main_runs_local_checks_with_explicit_tool_paths(self) -> None:
        commands: list[tuple[str, ...]] = []

        def record(command: list[str], label: str) -> None:
            commands.append(tuple(command))

        with patch.object(verify_script, "_core_python", return_value=Path("/runtime/python")), patch.object(
            verify_script, "_tool", side_effect=["/tools/ruff", "/tools/mypy"]
        ), patch.object(verify_script, "_run", side_effect=record):
            self.assertEqual(verify_script.main(), 0)
        self.assertEqual(commands[0], ("/runtime/python", str(verify_script.PROJECT / "lead.py"), "doctor"))
        self.assertEqual(commands[1], ("/runtime/python", "-m", "unittest", "discover", "-s", "tests"))
        self.assertEqual(commands[2], ("/tools/ruff", "check", "lead_collector", "lead.py", "scripts", "tests"))
        self.assertEqual(commands[3][:4], ("/tools/mypy", "--strict", "--follow-imports=skip", "lead_collector"))
        self.assertIn(str(verify_script.PROJECT / "scripts" / "setup.py"), commands[3])
        self.assertIn(str(verify_script.PROJECT / "scripts" / "verify.py"), commands[3])

    def test_run_maps_a_subprocess_launch_failure_to_a_named_error(self) -> None:
        with patch.object(verify_script.subprocess, "run", side_effect=OSError):
            with self.assertRaisesRegex(verify_script.VerifyError, "VERIFY_COMMAND_UNAVAILABLE"):
                verify_script._run(["missing"], "Ruff")


if __name__ == "__main__":
    unittest.main()
