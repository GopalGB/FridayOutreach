"""Behavior tests for the CSV filesystem safety boundary."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lead_collector import batch_safety


class _NoFollowUnavailable:
    def __getattr__(self, name: str) -> object:
        if name == "O_NOFOLLOW":
            raise AttributeError(name)
        return getattr(os, name)


class BatchSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self._temporary.name).resolve()
        self.input_path = self.root / "businesses.csv"
        self.input_path.write_text("company,website\nAcme,\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def assert_error(self, operation: Callable[[], object], code: str) -> None:
        with self.assertRaises(batch_safety.BatchSafetyError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)

    def test_non_csv_and_protected_paths_are_refused_before_lstat(self) -> None:
        with patch.object(Path, "lstat", side_effect=AssertionError("must not inspect unsafe path")):
            self.assert_error(lambda: batch_safety.validate_output(Path("relative.txt"), False), "INPUT_UNSAFE")
        with patch.object(batch_safety, "is_protected_path", return_value=True), patch.object(
            Path, "lstat", side_effect=AssertionError("must not inspect protected path")
        ):
            self.assert_error(lambda: batch_safety.validate_output(Path("secret.csv"), False), "INPUT_UNSAFE")
            self.assert_error(lambda: batch_safety.read_input(self.root / "secret.csv"), "INPUT_UNSAFE")

    def test_output_refuses_missing_unsafe_and_colliding_targets(self) -> None:
        missing = self.root / "missing" / "out.csv"
        parent_file = self.root / "not-a-directory"
        parent_file.write_text("owned", encoding="utf-8")
        linked = self.root / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        existing = self.root / "existing.csv"
        existing.write_text("user output", encoding="utf-8")
        for path, code in (
            (missing, "INPUT_INVALID"),
            (parent_file / "out.csv", "INPUT_UNSAFE"),
            (linked / "out.csv", "INPUT_UNSAFE"),
            (existing, "OUTPUT_EXISTS"),
        ):
            with self.subTest(path=path):
                self.assert_error(lambda: batch_safety.validate_output(path, False), code)
        self.assertEqual(existing.read_text(encoding="utf-8"), "user output")

    def test_input_refuses_missing_linked_directory_and_hardlinked_files(self) -> None:
        linked = self.root / "linked.csv"
        linked.symlink_to(self.input_path)
        directory = self.root / "directory.csv"
        directory.mkdir()
        hard_link = self.root / "hard-linked.csv"
        os.link(self.input_path, hard_link)
        for path, code in (
            (self.root / "missing.csv", "INPUT_INVALID"),
            (linked, "INPUT_UNSAFE"),
            (directory, "INPUT_UNSAFE"),
            (hard_link, "INPUT_UNSAFE"),
        ):
            with self.subTest(path=path):
                self.assert_error(lambda: batch_safety.read_input(path), code)

    def test_sparse_oversize_input_is_rejected_before_open(self) -> None:
        oversized = self.root / "oversized.csv"
        with oversized.open("wb") as file:
            file.seek(batch_safety.MAX_INPUT_BYTES)
            file.write(b"x")
        with patch.object(batch_safety, "_open_input") as open_input:
            self.assert_error(lambda: batch_safety.read_input(oversized), "INPUT_INVALID")
        open_input.assert_not_called()

    def test_bounded_read_detects_growth_and_closes_its_descriptor(self) -> None:
        self.input_path.write_bytes(b"abcd")
        original_open = batch_safety._open_input
        opened: list[int] = []

        def open_then_grow(path: Path) -> int:
            descriptor = original_open(path)
            opened.append(descriptor)
            with path.open("ab") as file:
                file.write(b"x")
            return descriptor

        with patch.object(batch_safety, "MAX_INPUT_BYTES", 4), patch.object(
            batch_safety, "_open_input", side_effect=open_then_grow
        ), patch.object(os, "close", wraps=os.close) as close:
            self.assert_error(lambda: batch_safety.read_input(self.input_path), "INPUT_INVALID")
        close.assert_called_once_with(opened[0])

    def test_descriptor_closes_when_post_open_validation_rejects_it(self) -> None:
        metadata = self.input_path.stat()
        unsafe = os.stat_result(
            (metadata.st_mode, metadata.st_ino, metadata.st_dev, 2, metadata.st_uid, metadata.st_gid, metadata.st_size, metadata.st_atime, metadata.st_mtime, metadata.st_ctime)
        )
        with patch.object(os, "fstat", return_value=unsafe), patch.object(
            os, "close", wraps=os.close
        ) as close:
            self.assert_error(lambda: batch_safety.read_input(self.input_path), "INPUT_UNSAFE")
        close.assert_called_once()

    def test_unavailable_no_follow_and_unopenable_inputs_return_named_errors(self) -> None:
        with patch.dict(vars(batch_safety), {"os": _NoFollowUnavailable()}):
            self.assert_error(lambda: batch_safety.read_input(self.input_path), "CAPABILITY_UNAVAILABLE")
        with patch.object(os, "open", side_effect=OSError("denied")):
            self.assert_error(lambda: batch_safety.read_input(self.input_path), "INPUT_UNSAFE")

    def test_binding_detects_deleted_replaced_and_modified_inputs(self) -> None:
        _, deleted = batch_safety.read_input(self.input_path)
        self.input_path.unlink()
        self.assert_error(lambda: batch_safety.assert_unchanged(deleted), "INPUT_CHANGED")

        self.input_path.write_text("company,website\nReplacement,\n", encoding="utf-8")
        _, replacement = batch_safety.read_input(self.input_path)
        alternate = self.root / "alternate.csv"
        alternate.write_text("company,website\nOther,\n", encoding="utf-8")
        alternate.replace(self.input_path)
        self.assert_error(lambda: batch_safety.assert_unchanged(replacement), "INPUT_CHANGED")

        self.input_path.write_text("company,website\nOriginal,\n", encoding="utf-8")
        _, changed = batch_safety.read_input(self.input_path)
        self.input_path.write_text("company,website\nChanged,\n", encoding="utf-8")
        self.assert_error(lambda: batch_safety.assert_unchanged(changed), "INPUT_CHANGED")
