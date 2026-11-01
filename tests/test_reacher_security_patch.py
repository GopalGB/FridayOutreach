import importlib.util
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


def _patch_module() -> object:
    path = Path(__file__).parents[1] / "scripts" / "reacher_smtp_runner" / "patch_reacher_core.py"
    spec = importlib.util.spec_from_file_location("reacher_security_patch", path)
    if spec is None or spec.loader is None:
        raise AssertionError("security patch module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _official_source(relative_path: str) -> bytes:
    archive_path = Path(__file__).parents[1] / ".tools" / "reacher-v0.11.11-source.tar.gz"
    if not archive_path.is_file():
        raise unittest.SkipTest("optional Reacher source archive is unavailable")
    with tarfile.open(archive_path, "r:gz") as archive:
        entries = [entry for entry in archive.getmembers() if entry.name.endswith(relative_path)]
        if len(entries) != 1:
            raise AssertionError("official Reacher source member is unavailable")
        source = archive.extractfile(entries[0])
        if source is None:
            raise AssertionError("official Reacher source member cannot be read")
        return source.read()


def _core_fixture(root: Path) -> Path:
    core = root / "core"
    manifest = core / "Cargo.toml"
    mx_source = core / "src" / "mx" / "mod.rs"
    core_source = core / "src" / "lib.rs"
    mx_source.parent.mkdir(parents=True)
    manifest.write_bytes(_official_source("core/Cargo.toml"))
    mx_source.write_bytes(_official_source("core/src/mx/mod.rs"))
    core_source.write_bytes(_official_source("core/src/lib.rs"))
    return manifest


def _patch_command(manifest: Path) -> list[str]:
    script = Path(__file__).parents[1] / "scripts" / "reacher_smtp_runner" / "patch_reacher_core.py"
    return [sys.executable, str(script), "--manifest", str(manifest)]


class ReacherSecurityPatchTests(unittest.TestCase):
    def test_patches_only_the_recorded_official_core_manifest(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "Cargo.toml"
            manifest.write_bytes(_official_source("core/Cargo.toml"))

            changed = module.patch_core_manifest(manifest)

            self.assertTrue(changed)
            content = manifest.read_text(encoding="utf-8")
            self.assertIn('hickory-proto = "=0.26.2"', content)
            self.assertIn('hickory-resolver = "=0.26.2"', content)
            self.assertFalse(module.patch_core_manifest(manifest))

    def test_refuses_tampered_source_without_writing(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "Cargo.toml"
            manifest.write_text('[dependencies]\nhickory-proto = "0.1"\n', encoding="utf-8")

            with self.assertRaises(module.ReacherPatchError):
                module.patch_core_manifest(manifest)

            self.assertEqual(manifest.read_text(encoding="utf-8"), '[dependencies]\nhickory-proto = "0.1"\n')

    def test_patches_the_hickory_026_mx_compatibility_boundary_once(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            mx_source = Path(temp_dir) / "mod.rs"
            mx_source.write_bytes(_official_source("core/src/mx/mod.rs"))

            changed = module.patch_mx_source(mx_source)

            self.assertTrue(changed)
            content = mx_source.read_text(encoding="utf-8")
            self.assertIn("net::NetError", content)
            self.assertIn("TokioResolver", content)
            self.assertIn("mx.exchange", content)
            self.assertIn("local_nameservers", content)
            self.assertIn("scoped_ipv6", content)
            self.assertFalse(module.patch_mx_source(mx_source))

    def test_patches_the_hickory_026_core_field_access_once(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            core_source = Path(temp_dir) / "lib.rs"
            core_source.write_bytes(_official_source("core/src/lib.rs"))

            changed = module.patch_core_source(core_source)

            self.assertTrue(changed)
            content = core_source.read_text(encoding="utf-8")
            self.assertIn(".min_by_key(|a| a.preference)", content)
            self.assertIn("&host.exchange,", content)
            self.assertFalse(module.patch_core_source(core_source))

    def test_validates_all_sources_before_writing_either_patch(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            core = Path(temp_dir) / "core"
            manifest = core / "Cargo.toml"
            mx_source = core / "src" / "mx" / "mod.rs"
            mx_source.parent.mkdir(parents=True)
            manifest.write_bytes(_official_source("core/Cargo.toml"))
            mx_source.write_text("tampered", encoding="utf-8")

            with self.assertRaises(module.ReacherPatchError):
                module.patch_reacher_core(manifest)

            self.assertEqual(manifest.read_bytes(), _official_source("core/Cargo.toml"))

    def test_refuses_a_tampered_core_library_before_writing_manifest(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            core = Path(temp_dir) / "core"
            manifest = core / "Cargo.toml"
            mx_source = core / "src" / "mx" / "mod.rs"
            core_source = core / "src" / "lib.rs"
            mx_source.parent.mkdir(parents=True)
            manifest.write_bytes(_official_source("core/Cargo.toml"))
            mx_source.write_bytes(_official_source("core/src/mx/mod.rs"))
            core_source.write_text("tampered", encoding="utf-8")

            with self.assertRaises(module.ReacherPatchError):
                module.patch_reacher_core(manifest)

            self.assertEqual(manifest.read_bytes(), _official_source("core/Cargo.toml"))

    def test_patch_cli_applies_all_sources_then_reports_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = _core_fixture(Path(directory))

            first = subprocess.run(_patch_command(manifest), text=True, capture_output=True, check=False)
            second = subprocess.run(_patch_command(manifest), text=True, capture_output=True, check=False)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, "patched\n")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "already-patched\n")

    def test_partial_patch_recovers_without_rewriting_already_patched_manifest(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as directory:
            manifest = _core_fixture(Path(directory))
            self.assertTrue(module.patch_core_manifest(manifest))
            original_manifest = manifest.read_bytes()

            changed = module.patch_reacher_core(manifest)

            self.assertTrue(changed)
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertFalse(module.patch_reacher_core(manifest))

    def test_patch_cli_rejects_missing_or_tampered_sources_without_partial_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = subprocess.run(_patch_command(root / "missing" / "Cargo.toml"), text=True, capture_output=True, check=False)
            manifest = _core_fixture(root)
            original = manifest.read_bytes()
            manifest.write_bytes(original + b"\n# tampered\n")
            tampered = subprocess.run(_patch_command(manifest), text=True, capture_output=True, check=False)

            self.assertEqual(manifest.read_bytes(), original + b"\n# tampered\n")

        self.assertEqual(missing.returncode, 2)
        self.assertIn("Reacher core source is unavailable", missing.stderr)
        self.assertEqual(tampered.returncode, 2)
        self.assertIn("Reacher core source does not match v0.11.11", tampered.stderr)

    def test_atomic_replace_cleans_up_on_filesystem_failures(self):
        module = _patch_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "Cargo.toml"
            target.write_bytes(b"original")
            for attribute in ("replace", "fdopen"):
                with self.subTest(attribute=attribute), patch.object(module.os, attribute, side_effect=OSError("injected")):
                    with self.assertRaisesRegex(module.ReacherPatchError, "could not be patched"):
                        module._atomic_replace(target, b"replacement")
                self.assertEqual(target.read_bytes(), b"original")
                self.assertEqual(list(root.glob(".reacher-core-*")), [])
