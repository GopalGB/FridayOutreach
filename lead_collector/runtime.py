"""Fail-closed runtime launcher for the project-local offline collector."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


VETTED_SOUPSIEVE_VERSION = "2.10"


class RuntimeUnavailable(Exception):
    """Raised when the checked project runtime cannot safely execute the collector."""


def _project_venv() -> Path:
    return Path(__file__).parent.parent.absolute() / ".venv"


def _launcher_path() -> Path:
    return Path(__file__).parent.parent.absolute() / "lead.py"


def _soupsieve_version() -> str:
    try:
        import soupsieve
    except ImportError as exc:
        raise RuntimeUnavailable("selector dependency is unavailable") from exc
    return str(soupsieve.__version__)


def _ensure_project_runtime(argv: list[str] | None) -> None:
    project_venv = _project_venv()
    if Path(sys.prefix) != project_venv:
        candidate = project_venv / "bin" / "python"
        launcher = _launcher_path()
        if not candidate.is_file() or not launcher.is_file():
            raise RuntimeUnavailable("project runtime is unavailable")
        try:
            arguments = sys.argv[1:] if argv is None else argv
            os.execv(str(candidate), [str(candidate), str(launcher), *arguments])
        except OSError as exc:
            raise RuntimeUnavailable("project runtime is unavailable") from exc
    if _soupsieve_version() != VETTED_SOUPSIEVE_VERSION:
        raise RuntimeUnavailable("project selector dependency is unavailable")


def _runtime_error() -> None:
    print(
        json.dumps(
            {
                "version": 1,
                "ok": False,
                "data": None,
                "error": {
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "project runtime is unavailable",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def run_cli(argv: list[str] | None = None) -> int:
    try:
        _ensure_project_runtime(argv)
    except RuntimeUnavailable:
        _runtime_error()
        return 3
    from .cli import main

    return main(argv)
