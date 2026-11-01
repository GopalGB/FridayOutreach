"""Run Lead Collector's local verification gates without changing the environment."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[1]


class VerifyError(Exception):
    """Raised when a required local verification prerequisite is unavailable."""


def _core_python() -> Path:
    runtime = PROJECT / ".venv" / "bin" / "python"
    if not runtime.is_file():
        raise VerifyError("VERIFY_CORE_RUNTIME_MISSING: run python3 scripts/setup.py --core first")
    return runtime


def _tool(name: str) -> str:
    discovered = shutil.which(name)
    bundled = Path(sys.base_prefix) / "bin" / name
    if discovered is not None:
        return discovered
    if bundled.is_file():
        return str(bundled)
    raise VerifyError(f"VERIFY_DEV_TOOL_MISSING: {name} is required on PATH or beside the base Python")


def _run(command: list[str], label: str) -> None:
    try:
        completed = subprocess.run(command, cwd=PROJECT, check=False)
    except OSError as exc:
        raise VerifyError(f"VERIFY_COMMAND_UNAVAILABLE: {label} could not start") from exc
    if completed.returncode != 0:
        raise VerifyError(f"VERIFY_COMMAND_FAILED: {label} exited {completed.returncode}")


def _mypy_command(tool: str) -> list[str]:
    scripts = [str(path) for path in sorted((PROJECT / "scripts").glob("*.py"))]
    return [tool, "--strict", "--follow-imports=skip", "lead_collector", "lead.py", *scripts]


def main() -> int:
    try:
        runtime = _core_python()
        ruff = _tool("ruff")
        mypy = _tool("mypy")
        _run([str(runtime), str(PROJECT / "lead.py"), "doctor"], "core doctor")
        _run([str(runtime), "-m", "unittest", "discover", "-s", "tests"], "unit tests")
        _run([ruff, "check", "lead_collector", "lead.py", "scripts", "tests"], "ruff")
        _run(_mypy_command(mypy), "mypy")
    except VerifyError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write("VERIFY_OK: local core, tests, Ruff, and MyPy passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
