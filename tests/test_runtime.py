import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lead_collector import runtime


ROOT = Path(__file__).parents[1]
BASE_INTERPRETER = Path(getattr(sys, "_base_executable", sys.executable))


class RuntimeLauncherTests(unittest.TestCase):
    def test_base_interpreter_module_entrypoint_reexecutes_the_safe_launcher(self):
        """Re-executing package __main__.py as a file would lose relative imports and break doctor."""
        result = subprocess.run(
            [str(BASE_INTERPRETER), "-m", "lead_collector", "doctor"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["data"]["soupsieve"], "2.10")

    def test_both_supported_entry_points_report_the_vetted_selector_dependency(self):
        """Bypassing the project venv would expose a vulnerable parser dependency."""
        commands = (
            [sys.executable, "lead.py", "doctor"],
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "lead_collector", "doctor"],
        )

        for command in commands:
            with self.subTest(command=command):
                result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["data"]["soupsieve"], "2.10")

    def test_missing_project_runtime_returns_named_capability_error(self):
        """Falling through to an arbitrary interpreter when the vetted venv is missing must fail."""
        with tempfile.TemporaryDirectory() as temporary:
            output = StringIO()
            with patch.object(runtime, "_project_venv", return_value=Path(temporary).resolve() / ".venv"):
                with redirect_stdout(output):
                    code = runtime.run_cli(["doctor"])

        body = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(body["error"]["code"], "CAPABILITY_UNAVAILABLE")

    def test_wrong_project_dependency_returns_named_capability_error(self):
        """Accepting a project runtime with an unvetted parser version would weaken the security gate."""
        output = StringIO()
        with patch.object(runtime, "_project_venv", return_value=Path(sys.prefix)):
            with patch.object(runtime, "_soupsieve_version", return_value="2.8.3"):
                with redirect_stdout(output):
                    code = runtime.run_cli(["doctor"])

        body = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(body["error"]["code"], "CAPABILITY_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
