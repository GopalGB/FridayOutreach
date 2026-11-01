"""User-space setup for the core runtime and explicitly optional integrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
MIN_CORE_PYTHON = (3, 11)
AI_PYTHON = (3, 12)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare Lead Collector in user space.")
    parser.add_argument("--core", action="store_true", help="create or update the project core virtual environment")
    parser.add_argument("--ai", action="store_true", help="install the optional AI lock into .venv-ai")
    parser.add_argument("--reacher", action="store_true", help="build the explicit SMTP-only Reacher runtime")
    parser.add_argument("--install", action="store_true", help="install the launcher and skill into user space")
    return parser


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"SETUP_COMMAND_FAILED: {_failure_detail(completed.stderr + completed.stdout)}")


def _failure_detail(output: str) -> str:
    for line in reversed(output.splitlines()):
        detail = line.strip()
        if detail.lower().startswith(("error:", "failed", "no solution")):
            return detail[:240]
    return "dependency command failed"


def _clean_environment() -> dict[str, str]:
    return {"HOME": str(Path.home()), "PATH": os.defpath, "UV_NO_CONFIG": "1", "UV_NO_CACHE": "1", "UV_PYTHON_DOWNLOADS": "never"}


def _python_version(python: Path | str) -> tuple[int, int]:
    completed = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("SETUP_RUNTIME_INVALID: Python runtime is not executable")
    major, minor = completed.stdout.strip().split(".")
    return int(major), int(minor)


def _require_python(name: str, minimum: tuple[int, int], code: str) -> str:
    python = shutil.which(name)
    if python is None or _python_version(python) < minimum:
        raise RuntimeError(f"{code}: Python {minimum[0]}.{minimum[1]} or newer is required")
    return python


def _runtime(environment: Path, minimum: tuple[int, int], code: str) -> Path:
    runtime = environment / "bin" / "python"
    if not runtime.is_file() or _python_version(runtime) < minimum:
        raise RuntimeError(f"{code}: expected project runtime is unavailable")
    return runtime


def _regular_directory(path: Path, code: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        raise RuntimeError(f"{code}: refusing unsafe installation path")


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _target_matches(path: Path, expected: bytes, exists_code: str) -> bool:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise RuntimeError("SETUP_TARGET_UNSAFE: refusing symlinked or linked installation target")
    if path.read_bytes() != expected:
        raise RuntimeError(f"{exists_code}: refusing to overwrite an unknown installation target")
    return True


def _core_artifacts() -> tuple[Path, Path]:
    wheels = PROJECT / "wheels"
    lock = PROJECT / "requirements-core.lock"
    if not wheels.is_dir() or not lock.is_file():
        raise RuntimeError("SETUP_CORE_ARTIFACTS_MISSING: requirements-core.lock and wheels/ are required")
    return wheels, lock


def _core() -> None:
    wheels, lock = _core_artifacts()
    environment = PROJECT / ".venv"
    uv = shutil.which("uv")
    if _path_present(environment):
        _regular_directory(environment, "SETUP_CORE_ENV_UNSAFE")
    else:
        python = sys.executable
        if _python_version(python) < MIN_CORE_PYTHON:
            python = _require_python("python3", MIN_CORE_PYTHON, "SETUP_PYTHON_MISSING")
        if uv is None:
            _run([python, "-m", "venv", str(environment)], env=_clean_environment())
        else:
            _run([uv, "venv", "--no-config", "--no-python-downloads", "--offline", "--python", python, str(environment)], env=_clean_environment())
    runtime = _runtime(environment, MIN_CORE_PYTHON, "SETUP_CORE_RUNTIME_MISSING")
    if uv is None:
        _run([str(runtime), "-m", "ensurepip", "--upgrade"], env=_clean_environment())
        _run([str(runtime), "-m", "pip", "install", "--no-index", "--find-links", str(wheels), "--require-hashes", "-r", str(lock)], env=_clean_environment())
        return
    _run([uv, "pip", "install", "--python", str(runtime), "--no-config", "--no-cache", "--no-python-downloads", "--offline", "--no-index", "--find-links", str(wheels), "--require-hashes", "-r", str(lock)], env=_clean_environment())


def _ai_environment() -> Path:
    environment = PROJECT / ".venv-ai"
    if _path_present(environment):
        _regular_directory(environment, "SETUP_AI_ENV_UNSAFE")
        return _runtime(environment, AI_PYTHON, "SETUP_AI_RUNTIME_MISSING")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("SETUP_UV_MISSING: install uv in user space before --ai")
    python = _require_python("python3.12", AI_PYTHON, "SETUP_PYTHON312_MISSING")
    _run([uv, "venv", "--no-config", "--no-python-downloads", "--python", python, str(environment)], env=_clean_environment())
    return _runtime(environment, AI_PYTHON, "SETUP_AI_RUNTIME_MISSING")


def _ai() -> None:
    lock = PROJECT / "requirements-ai.lock"
    if not lock.is_file():
        raise RuntimeError("SETUP_AI_LOCK_MISSING: requirements-ai.lock is required")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("SETUP_UV_MISSING: install uv in user space before --ai")
    runtime = _ai_environment()
    _run([uv, "pip", "install", "--python", str(runtime), "--no-config", "--no-cache", "--no-python-downloads", "--index-url", "https://pypi.org/simple", "--require-hashes", "--only-binary", ":all:", "--strict", "-r", str(lock)], env=_clean_environment())


def _reacher_artifacts() -> tuple[Path, Path, Path]:
    runner = PROJECT / "scripts" / "reacher_smtp_runner"
    patch = runner / "patch_reacher_core.py"
    manifest = PROJECT / ".tools" / "reacher-v0.11.11" / "core" / "Cargo.toml"
    sources = (runner / "Cargo.toml", patch, manifest, manifest.parent / "src" / "lib.rs", manifest.parent / "src" / "mx" / "mod.rs")
    if not all(path.is_file() for path in sources):
        raise RuntimeError("SETUP_REACHER_ARTIFACTS_MISSING: acquire the pinned Reacher core first")
    return runner / "Cargo.toml", patch, manifest


def _reacher_cargo() -> str:
    local = PROJECT / ".tools" / "cargo" / "bin" / "cargo"
    if local.is_file():
        return str(local)
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("SETUP_REACHER_CARGO_MISSING: install Rust in user space before --reacher")
    return cargo


def _reacher_environment() -> dict[str, str]:
    environment = _clean_environment()
    toolchain = PROJECT / ".tools"
    rustup = toolchain / "rustup"
    cargo = toolchain / "cargo"
    if rustup.is_dir() and cargo.is_dir():
        environment["RUSTUP_HOME"] = str(rustup)
        environment["CARGO_HOME"] = str(cargo)
        environment["PATH"] = str(cargo / "bin") + os.pathsep + environment["PATH"]
    environment["CARGO_BUILD_JOBS"] = "2"
    environment["CARGO_TARGET_DIR"] = str(toolchain / "reacher-target")
    return environment


def _owned_regular(path: Path, code: str) -> None:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.getuid():
        raise RuntimeError(f"{code}: refusing unsafe Reacher binary path")


def _bind_reacher_binary() -> None:
    source = PROJECT / ".tools" / "reacher-target" / "release" / "lead-collector-reacher-smtp"
    destination = PROJECT / ".tools" / "reacher-smtp-check"
    _owned_regular(source, "SETUP_REACHER_BUILD_INVALID")
    _regular_directory(destination.parent, "SETUP_REACHER_TARGET_UNSAFE")
    if _path_present(destination):
        _owned_regular(destination, "SETUP_REACHER_TARGET_UNSAFE")
    descriptor, temporary = tempfile.mkstemp(prefix=".reacher-smtp-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output_file, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    except OSError as exc:
        raise RuntimeError("SETUP_REACHER_BIND_FAILED: release binary could not be installed") from exc
    finally:
        try:
            Path(temporary).unlink()
        except OSError:
            pass


def _reacher() -> None:
    runner, patch, core_manifest = _reacher_artifacts()
    environment = _reacher_environment()
    _run([sys.executable, str(patch), "--manifest", str(core_manifest)], env=environment)
    _run([_reacher_cargo(), "build", "--locked", "--release", "--manifest-path", str(runner)], env=environment)
    _bind_reacher_binary()


def _launcher() -> str:
    runtime = PROJECT / ".venv" / "bin" / "python"
    entrypoint = PROJECT / "lead.py"
    return f"#!/usr/bin/env python3\nimport os\nimport sys\nos.execv({str(runtime)!r}, [{str(runtime)!r}, {str(entrypoint)!r}, *sys.argv[1:]])\n"


def _install_paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    launcher = home / ".local" / "bin" / "lead-collector"
    skill_root = home / ".codex" / "skills" / "lead-collector"
    return launcher, skill_root, skill_root / "SKILL.md"


def _friday_install_paths() -> tuple[Path, Path, Path]:
    home = Path.home()
    launcher = home / ".local" / "bin" / "fridayoutreach"
    skill_root = home / ".codex" / "skills" / "fridayoutreach"
    return launcher, skill_root, skill_root / "SKILL.md"


def _preflight_install() -> tuple[Path, Path, Path, bytes, str]:
    source = PROJECT / "skills" / "lead-collector" / "SKILL.md"
    if not source.is_file():
        raise RuntimeError("SETUP_SKILL_SOURCE_MISSING: skills/lead-collector/SKILL.md is required")
    _runtime(PROJECT / ".venv", MIN_CORE_PYTHON, "SETUP_CORE_RUNTIME_MISSING")
    launcher, skill_root, destination = _install_paths()
    for directory in (launcher.parent.parent, launcher.parent, skill_root.parent.parent, skill_root.parent, skill_root):
        _regular_directory(directory, "SETUP_TARGET_UNSAFE")
    skill = source.read_bytes()
    _target_matches(launcher, _launcher().encode("utf-8"), "SETUP_LAUNCHER_EXISTS")
    _target_matches(destination, skill, "SETUP_SKILL_EXISTS")
    friday_source = PROJECT / "skills" / "fridayoutreach" / "SKILL.md"
    if friday_source.is_file():
        friday_launcher, friday_root, friday_destination = _friday_install_paths()
        for directory in (friday_launcher.parent.parent, friday_launcher.parent, friday_root.parent.parent, friday_root.parent, friday_root):
            _regular_directory(directory, "SETUP_TARGET_UNSAFE")
        friday_skill = friday_source.read_bytes()
        friday_text = _launcher()
        _target_matches(friday_launcher, friday_text.encode("utf-8"), "SETUP_LAUNCHER_EXISTS")
        _target_matches(friday_destination, friday_skill, "SETUP_SKILL_EXISTS")
    return launcher, skill_root, destination, skill, _launcher()


def _write_new(path: Path, content: bytes, mode: int) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
    return True


def _install() -> None:
    launcher, skill_root, destination, skill, launcher_text = _preflight_install()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    skill_root.mkdir(parents=True, exist_ok=True)
    if not _write_new(launcher, launcher_text.encode("utf-8"), 0o755):
        _target_matches(launcher, launcher_text.encode("utf-8"), "SETUP_LAUNCHER_EXISTS")
    if not _write_new(destination, skill, 0o644):
        _target_matches(destination, skill, "SETUP_SKILL_EXISTS")
    friday_source = PROJECT / "skills" / "fridayoutreach" / "SKILL.md"
    if not friday_source.is_file():
        return
    friday_launcher, friday_root, friday_destination = _friday_install_paths()
    for directory in (friday_launcher.parent.parent, friday_launcher.parent, friday_root.parent.parent, friday_root.parent, friday_root):
        _regular_directory(directory, "SETUP_TARGET_UNSAFE")
    friday_skill = friday_source.read_bytes()
    friday_text = _launcher()
    _target_matches(friday_launcher, friday_text.encode("utf-8"), "SETUP_LAUNCHER_EXISTS")
    _target_matches(friday_destination, friday_skill, "SETUP_SKILL_EXISTS")
    friday_launcher.parent.mkdir(parents=True, exist_ok=True)
    friday_root.mkdir(parents=True, exist_ok=True)
    if not _write_new(friday_launcher, friday_text.encode("utf-8"), 0o755):
        _target_matches(friday_launcher, friday_text.encode("utf-8"), "SETUP_LAUNCHER_EXISTS")
    if not _write_new(friday_destination, friday_skill, 0o644):
        _target_matches(friday_destination, friday_skill, "SETUP_SKILL_EXISTS")


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if not (args.core or args.ai or args.reacher or args.install):
            raise RuntimeError("SETUP_ACTION_REQUIRED: choose --core, --ai, --reacher, or --install")
        if args.core:
            _core()
        if args.ai:
            _ai()
        if args.reacher:
            _reacher()
        if args.install:
            _install()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
