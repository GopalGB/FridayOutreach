from contextlib import redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import importlib.util


SPEC = importlib.util.spec_from_file_location("setup_script", Path(__file__).parents[1] / "scripts" / "setup.py")
assert SPEC is not None and SPEC.loader is not None
setup_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_script)
ROOT = Path(__file__).parents[1]


class SetupTests(unittest.TestCase):
    def test_requires_an_explicit_action(self) -> None:
        self.assertEqual(setup_script.main([]), 2)

    def test_core_reports_missing_offline_artifacts_without_network(self) -> None:
        output = StringIO()
        with redirect_stderr(output), patch.object(setup_script, "PROJECT", Path(tempfile.mkdtemp())):
            result = setup_script.main(["--core"])
        self.assertEqual(result, 2)
        self.assertIn("SETUP_CORE_ARTIFACTS_MISSING", output.getvalue())

    def test_core_rejects_a_dangling_runtime_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "release"
            (project / "wheels").mkdir(parents=True)
            (project / "requirements-core.lock").write_text("# locked\n", encoding="utf-8")
            (project / ".venv").symlink_to(project / "missing-runtime")
            with patch.object(setup_script, "PROJECT", project):
                with self.assertRaises(RuntimeError) as error:
                    setup_script._core()
            self.assertIn("SETUP_CORE_ENV_UNSAFE", str(error.exception))

    def test_existing_core_uses_stdlib_ensurepip_when_uv_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "release"
            (project / "wheels").mkdir(parents=True)
            (project / "requirements-core.lock").write_text("# locked\n", encoding="utf-8")
            self._runtime(project)
            commands: list[list[str]] = []
            with patch.object(setup_script, "PROJECT", project), patch.object(setup_script.shutil, "which", return_value=None), patch.object(setup_script, "_python_version", return_value=(3, 11)), patch.object(setup_script, "_run", side_effect=lambda command, **_: commands.append(command)):
                setup_script._core()
            self.assertEqual(commands[0][1:3], ["-m", "ensurepip"])
            self.assertIn("--no-index", commands[1])

    def test_unknown_launcher_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / ".local" / "bin" / "lead-collector"
            target.parent.mkdir(parents=True)
            target.write_text("user-owned", encoding="utf-8")
            with patch.object(setup_script.Path, "home", return_value=Path(home)):
                with self.assertRaises(RuntimeError) as error:
                    setup_script._install()
            self.assertIn("SETUP_LAUNCHER_EXISTS", str(error.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), "user-owned")

    def test_install_requires_a_ready_core_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "release"
            self._skill(project)
            output = StringIO()
            with redirect_stderr(output), patch.object(setup_script, "PROJECT", project), patch.object(
                setup_script.Path, "home", return_value=Path(directory) / "home"
            ):
                self.assertEqual(setup_script.main(["--install"]), 2)
            self.assertIn("SETUP_CORE_RUNTIME_MISSING", output.getvalue())

    def test_install_preflights_skill_before_creating_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "release"
            home = root / "home"
            self._runtime(project)
            self._skill(project)
            destination = home / ".codex" / "skills" / "lead-collector" / "SKILL.md"
            destination.parent.mkdir(parents=True)
            destination.write_text("user-owned", encoding="utf-8")
            with patch.object(setup_script, "PROJECT", project), patch.object(
                setup_script.Path, "home", return_value=home
            ):
                with self.assertRaises(RuntimeError) as error:
                    setup_script._install()
            self.assertIn("SETUP_SKILL_EXISTS", str(error.exception))
            self.assertFalse((home / ".local" / "bin" / "lead-collector").exists())

    def test_install_rejects_symlink_and_hardlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "release"
            home = root / "home"
            self._runtime(project)
            self._skill(project)
            launcher = home / ".local" / "bin" / "lead-collector"
            launcher.parent.mkdir(parents=True)
            source = root / "owned-launcher"
            source.write_text("user-owned", encoding="utf-8")
            launcher.symlink_to(source)
            with patch.object(setup_script, "PROJECT", project), patch.object(
                setup_script.Path, "home", return_value=home
            ):
                with self.assertRaises(RuntimeError) as error:
                    setup_script._install()
            self.assertIn("SETUP_TARGET_UNSAFE", str(error.exception))
            launcher.unlink()
            os.link(source, launcher)
            with patch.object(setup_script, "PROJECT", project), patch.object(
                setup_script.Path, "home", return_value=home
            ):
                with self.assertRaises(RuntimeError) as error:
                    setup_script._install()
            self.assertIn("SETUP_TARGET_UNSAFE", str(error.exception))
            self.assertEqual(source.read_text(encoding="utf-8"), "user-owned")

    def test_existing_ai_runtime_is_not_recreated_and_uses_sanitized_uv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "release"
            (project / "requirements-ai.lock").parent.mkdir(parents=True)
            (project / "requirements-ai.lock").write_text("# locked\n", encoding="utf-8")
            runtime = project / ".venv-ai" / "bin" / "python"
            runtime.parent.mkdir(parents=True)
            runtime.write_text("runtime", encoding="utf-8")
            commands: list[tuple[list[str], dict[str, str] | None]] = []

            def capture(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
                commands.append((command, env))

            with patch.object(setup_script, "PROJECT", project), patch.object(
                setup_script.shutil, "which", side_effect=lambda name: {"uv": "/usr/bin/uv", "python3.12": "/usr/bin/python3.12"}.get(name)
            ), patch.object(setup_script, "_python_version", return_value=(3, 12)), patch.object(
                setup_script, "_run", side_effect=capture
            ):
                setup_script._ai()
            self.assertFalse(any(command[1] == "venv" for command, _ in commands))
            command, environment = commands[-1]
            self.assertIn("https://pypi.org/simple", command)
            self.assertIn("--no-config", command)
            self.assertIn("--no-cache", command)
            self.assertIn("--no-python-downloads", command)
            self.assertIn("--only-binary", command)
            self.assertIn("--strict", command)
            self.assertNotIn("HTTP_PROXY", environment or {})

    def test_copied_release_bootstraps_offline_and_launcher_runs_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "release with spaces"
            shutil.copytree(ROOT, release, ignore=shutil.ignore_patterns(".git", ".planning", ".venv", ".venv-ai", "__pycache__", "dist"))
            home = (root / "home").resolve()
            environment = {"HOME": str(home), "PATH": os.environ["PATH"], "UV_OFFLINE": "1"}
            source = root / "fixture.html"
            shutil.copy2(ROOT / "example.html", source)
            setup = [sys.executable, str(release / "scripts" / "setup.py")]
            self._command(setup + ["--core"], release, environment)
            pip = subprocess.run(
                [str(release / ".venv" / "bin" / "python"), "-m", "pip", "--version"],
                cwd=release,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if shutil.which("uv", path=environment["PATH"]) is None:
                self.assertEqual(pip.returncode, 0)
            else:
                self.assertNotEqual(pip.returncode, 0)
            self._command(setup + ["--core", "--install"], release, environment)
            launcher = Path(environment["HOME"]) / ".local" / "bin" / "lead-collector"
            output = self._command([str(launcher), "doctor"], root, environment)
            self.assertIn('"ok":true', output)
            preview = self._command(
                [str(launcher), "preview", "--html", str(source), "--source-url", "https://source.example.test/directory", "--mapping", '{"Name":"company"}'],
                root,
                environment,
            )
            self.assertIn('"ok":true', preview)
            collect = self._command(
                [str(launcher), "collect", "--html", str(source), "--source-url", "https://source.example.test/directory", "--mapping", '{"Name":"company"}'],
                root,
                environment,
            )
            self.assertIn('"ok":true', collect)
            collected = json.loads(collect)["data"]
            exported = root / "export.json"
            export = self._command(
                [str(launcher), "export", "--job", collected["id"], "--format", "json", "--output", str(exported), "--row", collected["rows"][0]["id"]],
                root,
                environment,
            )
            self.assertIn('"ok":true', export)
            self.assertTrue(exported.is_file())
            self._command(setup + ["--install"], release, environment)
            self.assertEqual(
                (Path(environment["HOME"]) / ".codex" / "skills" / "lead-collector" / "SKILL.md").read_bytes(),
                (release / "skills" / "lead-collector" / "SKILL.md").read_bytes(),
            )

    def test_friday_launcher_cold_installs_and_is_idempotent_from_lead_collector_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / "lead-collector"
            shutil.copytree(ROOT, release, ignore=shutil.ignore_patterns(".git", ".planning", ".venv", ".venv-ai", "__pycache__", "dist"))
            home = (root / "home").resolve()
            environment = {"HOME": str(home), "PATH": os.environ["PATH"], "UV_OFFLINE": "1"}
            setup = [sys.executable, str(release / "scripts" / "setup.py")]
            self._command(setup + ["--core", "--install"], release, environment)
            friday = home / ".local" / "bin" / "fridayoutreach"
            self.assertIn('"ok":true', self._command([str(friday), "doctor"], root, environment))
            self._command(setup + ["--install"], release, environment)
            self.assertEqual(
                (home / ".codex" / "skills" / "fridayoutreach" / "SKILL.md").read_bytes(),
                (release / "skills" / "fridayoutreach" / "SKILL.md").read_bytes(),
            )

    def test_unknown_friday_target_blocks_all_installation_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "release"
            home = root / "home"
            self._runtime(project)
            self._skill(project)
            friday = project / "skills" / "fridayoutreach" / "SKILL.md"
            friday.parent.mkdir(parents=True)
            friday.write_text("friday", encoding="utf-8")
            target = home / ".local" / "bin" / "fridayoutreach"
            target.parent.mkdir(parents=True)
            target.write_text("user-owned", encoding="utf-8")
            with patch.object(setup_script, "PROJECT", project), patch.object(setup_script.Path, "home", return_value=home):
                with self.assertRaisesRegex(RuntimeError, "SETUP_LAUNCHER_EXISTS"):
                    setup_script._install()
            self.assertFalse((home / ".local" / "bin" / "lead-collector").exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "user-owned")

    def test_installer_keeps_real_paths_and_known_targets_for_both_aliases(self) -> None:
        for include_friday in (False, True):
            with self.subTest(include_friday=include_friday), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project, home = root / "lead-collector", root / "home"
                self._runtime(project)
                self._skill(project)
                if include_friday:
                    source = project / "skills" / "fridayoutreach" / "SKILL.md"
                    source.parent.mkdir(parents=True)
                    source.write_text("FridayOutreach", encoding="utf-8")
                with patch.object(setup_script, "PROJECT", project), patch.object(setup_script.Path, "home", return_value=home):
                    for _ in range(2):
                        setup_script._install()
                        expected = setup_script._launcher()
                        self.assertEqual((home / ".local/bin/lead-collector").read_text(), expected)
                        friday = home / ".local/bin/fridayoutreach"
                        self.assertEqual(friday.exists(), include_friday)
                        if include_friday:
                            self.assertEqual(friday.read_text(), expected)
                            self.assertEqual((home / ".codex/skills/fridayoutreach/SKILL.md").read_text(), "FridayOutreach")

    def test_unknown_friday_skill_targets_preserve_all_user_files(self) -> None:
        for kind in ("file", "symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project, home = root / "lead-collector", root / "home"
                self._runtime(project)
                self._skill(project)
                source = project / "skills/fridayoutreach/SKILL.md"
                source.parent.mkdir(parents=True)
                source.write_text("FridayOutreach", encoding="utf-8")
                target = home / ".codex/skills/fridayoutreach/SKILL.md"
                target.parent.mkdir(parents=True)
                owned = root / "owned-skill"
                owned.write_text("user-owned", encoding="utf-8")
                if kind == "file":
                    target.write_text("user-owned", encoding="utf-8")
                elif kind == "symlink":
                    target.symlink_to(owned)
                else:
                    os.link(owned, target)
                with patch.object(setup_script, "PROJECT", project), patch.object(setup_script.Path, "home", return_value=home):
                    with self.assertRaises(RuntimeError):
                        setup_script._install()
                self.assertEqual(owned.read_text(), "user-owned")
                self.assertEqual(target.read_text(), "user-owned")
                self.assertFalse((home / ".local/bin").exists())
                self.assertFalse((home / ".codex/skills/lead-collector").exists())

    def _command(self, command: list[str], cwd: Path, environment: dict[str, str]) -> str:
        result = subprocess.run(command, cwd=cwd, env=environment, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, " ".join(command) + "\n" + result.stderr + result.stdout)
        return result.stdout

    def _runtime(self, project: Path) -> None:
        runtime = project / ".venv" / "bin" / "python"
        runtime.parent.mkdir(parents=True)
        runtime.symlink_to(Path(sys.executable))

    def _skill(self, project: Path) -> None:
        source = project / "skills" / "lead-collector" / "SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nname: lead-collector\ndescription: Use when testing.\n---\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
