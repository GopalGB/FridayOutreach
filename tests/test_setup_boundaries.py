"""Installer gates are checked at subprocess and filesystem boundaries."""
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_setup import setup_script as setup


class SetupBoundaryTests(unittest.TestCase):
    def test_dependency_errors_are_named_bounded_and_do_not_dump_output(self):
        for output, expected in [('', 'dependency command failed'), ('ordinary log', 'dependency command failed'), ('error: first\nnoise\nfailed second', 'failed second'), ('error: ' + 'x' * 300, ('error: ' + 'x' * 300)[:240])]:
            with self.subTest(output=output[:20]):
                self.assertEqual(setup._failure_detail(output), expected)
        failed = subprocess.CompletedProcess(['fixture'], 1, stdout='log', stderr='error: unavailable')
        with patch.object(setup.subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'SETUP_COMMAND_FAILED: error: unavailable'):
                setup._run(['fixture'])

    def test_python_metadata_and_missing_or_old_runtimes_fail_visibly(self):
        success = subprocess.CompletedProcess(['python'], 0, stdout='3.12\n', stderr='')
        with patch.object(setup.subprocess, 'run', return_value=success):
            self.assertEqual(setup._python_version('/owned/python'), (3, 12))
        failed = subprocess.CompletedProcess(['python'], 1, stdout='', stderr='')
        with patch.object(setup.subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'SETUP_RUNTIME_INVALID'):
                setup._python_version('/missing/python')
        for found, version in [(None, (3, 12)), ('/owned/python', (3, 10))]:
            with patch.object(setup.shutil, 'which', return_value=found), patch.object(setup, '_python_version', return_value=version):
                with self.assertRaisesRegex(RuntimeError, 'FIXTURE_PYTHON_MISSING'):
                    setup._require_python('python3', (3, 11), 'FIXTURE_PYTHON_MISSING')

    def test_new_core_uses_only_local_hash_locked_wheels_in_both_backends(self):
        for uv in (None, '/owned/uv'):
            with self.subTest(uv=uv), tempfile.TemporaryDirectory() as directory:
                project = Path(directory).resolve()
                (project / 'wheels').mkdir()
                (project / 'requirements-core.lock').write_text('# fixture command policy\n')
                commands = []
                with patch.object(setup, 'PROJECT', project), patch.object(setup.shutil, 'which', return_value=uv), patch.object(setup, '_require_python', return_value='/owned/python3'), patch.object(setup, '_runtime', return_value=project / '.venv/bin/python'), patch.object(setup, '_run', side_effect=lambda command, **options: commands.append(command)):
                    setup._core()
                install = commands[-1]
                self.assertIn('--no-index', install)
                self.assertIn('--require-hashes', install)
                self.assertIn(str(project / 'wheels'), install)
                self.assertNotIn('https://', ' '.join(install))
                if uv is None:
                    self.assertIn('venv', commands[0])
                    self.assertIn('ensurepip', commands[1])
                else:
                    self.assertIn('--offline', commands[0])
                    self.assertIn('--no-config', install)

    def test_optional_ai_missing_capabilities_do_not_start_an_install(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            output = StringIO()
            with patch.object(setup, 'PROJECT', project), patch.object(setup, '_run') as run:
                with self.assertRaisesRegex(RuntimeError, 'SETUP_AI_LOCK_MISSING'):
                    setup._ai()
                (project / 'requirements-ai.lock').write_text('# fixture\n')
                with patch.object(setup.shutil, 'which', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'SETUP_UV_MISSING'):
                        setup._ai()
                    with self.assertRaisesRegex(RuntimeError, 'SETUP_UV_MISSING'):
                        setup._ai_environment()
                    with redirect_stderr(output):
                        self.assertEqual(setup.main(['--ai']), 2)
                with patch.object(setup.shutil, 'which', return_value='/owned/uv'), patch.object(setup, '_require_python', side_effect=RuntimeError('SETUP_PYTHON312_MISSING')):
                    with self.assertRaisesRegex(RuntimeError, 'SETUP_PYTHON312_MISSING'):
                        setup._ai_environment()
                run.assert_not_called()

    def test_new_ai_environment_keeps_explicit_python_and_no_download_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            commands = []
            runtime = project / '.venv-ai/bin/python'
            with patch.object(setup, 'PROJECT', project), patch.object(setup.shutil, 'which', return_value='/owned/uv'), patch.object(setup, '_require_python', return_value='/owned/python3.12'), patch.object(setup, '_runtime', return_value=runtime), patch.object(setup, '_run', side_effect=lambda command, **options: commands.append(command)):
                self.assertEqual(setup._ai_environment(), runtime)
            self.assertEqual(commands[0][0:2], ['/owned/uv', 'venv'])
            self.assertIn('--no-python-downloads', commands[0])
            self.assertIn('--no-config', commands[0])
            self.assertIn('/owned/python3.12', commands[0])

    def test_explicit_combined_setup_actions_run_once_in_order(self):
        actions = []
        with patch.object(setup, '_core', side_effect=lambda: actions.append('core')), patch.object(setup, '_ai', side_effect=lambda: actions.append('ai')), patch.object(setup, '_install', side_effect=lambda: actions.append('install')):
            self.assertEqual(setup.main(['--core', '--ai', '--install']), 0)
        self.assertEqual(actions, ['core', 'ai', 'install'])

    def test_reacher_setup_is_explicit_and_builds_the_patched_release_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            runner = project / 'scripts' / 'reacher_smtp_runner'
            manifest = project / '.tools' / 'reacher-v0.11.11' / 'core' / 'Cargo.toml'
            runner.mkdir(parents=True)
            manifest.parent.mkdir(parents=True)
            (manifest.parent / 'src' / 'mx').mkdir(parents=True)
            (runner / 'patch_reacher_core.py').write_text('# fixture\n', encoding='utf-8')
            (runner / 'Cargo.toml').write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding='utf-8')
            manifest.write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding='utf-8')
            (manifest.parent / 'src' / 'lib.rs').write_text('// fixture\n', encoding='utf-8')
            (manifest.parent / 'src' / 'mx' / 'mod.rs').write_text('// fixture\n', encoding='utf-8')
            commands = []
            with patch.object(setup, 'PROJECT', project), patch.object(setup.shutil, 'which', return_value='/owned/cargo'), patch.object(setup, '_run', side_effect=lambda command, **options: commands.append((command, options))), patch.object(setup, '_bind_reacher_binary') as bind:
                setup._reacher()
            self.assertEqual(commands[0][0][1], str(runner / 'patch_reacher_core.py'))
            self.assertEqual(commands[1][0][0], '/owned/cargo')
            self.assertIn('--locked', commands[1][0])
            self.assertIn('--release', commands[1][0])
            self.assertEqual(commands[1][1]['env']['CARGO_BUILD_JOBS'], '2')
            self.assertNotIn('HTTP_PROXY', commands[1][1]['env'])
            bind.assert_called_once()

    def test_reacher_setup_is_not_an_implicit_core_or_install_action(self):
        actions = []
        with patch.object(setup, '_core', side_effect=lambda: actions.append('core')), patch.object(setup, '_ai', side_effect=lambda: actions.append('ai')), patch.object(setup, '_install', side_effect=lambda: actions.append('install')), patch.object(setup, '_reacher', side_effect=lambda: actions.append('reacher')):
            self.assertEqual(setup.main(['--core', '--install']), 0)
            self.assertEqual(setup.main(['--reacher']), 0)
        self.assertEqual(actions, ['core', 'install', 'reacher'])

    def test_reacher_binding_replaces_only_an_owned_regular_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            source = project / '.tools' / 'reacher-target' / 'release' / 'lead-collector-reacher-smtp'
            destination = project / '.tools' / 'reacher-smtp-check'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'fixed release binary')
            destination.write_bytes(b'stale release binary')
            os.chmod(source, 0o700)
            os.chmod(destination, 0o700)
            with patch.object(setup, 'PROJECT', project):
                setup._bind_reacher_binary()
            self.assertEqual(destination.read_bytes(), b'fixed release binary')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual(destination.stat().st_nlink, 1)

    def test_fresh_core_bootstraps_offline_without_uv_or_optional_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / 'stdlib release with spaces'
            release.mkdir()
            owned_bin = root / 'bin'
            owned_bin.mkdir()
            (owned_bin / 'python3').symlink_to(Path(sys.executable).resolve())
            path = str(owned_bin) + os.pathsep + os.defpath
            self.assertIsNone(shutil.which('uv', path=path))
            for name in ('lead_collector', 'scripts', 'wheels'):
                shutil.copytree(setup.PROJECT / name, release / name, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('lead.py', 'requirements-core.lock'):
                shutil.copyfile(setup.PROJECT / name, release / name)
            environment = {'HOME': str(root / 'home'), 'PATH': path}
            command = [str(owned_bin / 'python3'), str(release / 'scripts/setup.py'), '--core']
            result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            doctor = subprocess.run([str(release / '.venv/bin/python'), str(release / 'lead.py'), 'doctor'], cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            self.assertTrue(json.loads(doctor.stdout)['ok'])
            self.assertFalse((release / '.venv-ai').exists())

    def test_fresh_core_uses_the_valid_invoking_python_before_path_python3(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / 'stdlib release with spaces'
            release.mkdir()
            owned_bin = root / 'bin'
            owned_bin.mkdir()
            (owned_bin / 'python3').symlink_to('/usr/bin/python3')
            path = str(owned_bin) + os.pathsep + os.defpath
            for name in ('lead_collector', 'scripts', 'wheels'):
                shutil.copytree(setup.PROJECT / name, release / name, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('lead.py', 'requirements-core.lock'):
                shutil.copyfile(setup.PROJECT / name, release / name)
            environment = {'HOME': str(root / 'home'), 'PATH': path}
            command = [sys.executable, str(release / 'scripts/setup.py'), '--core']
            result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, timeout=60)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((release / '.venv/bin/python').is_file())

    def test_missing_skill_and_existing_file_collisions_preserve_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(setup, 'PROJECT', root):
                with self.assertRaisesRegex(RuntimeError, 'SETUP_SKILL_SOURCE_MISSING'):
                    setup._preflight_install()
            output = root / 'owned-output'
            self.assertTrue(setup._write_new(output, b'first', 0o600))
            self.assertFalse(setup._write_new(output, b'second', 0o600))
            self.assertEqual(output.read_bytes(), b'first')


if __name__ == '__main__':
    unittest.main()
