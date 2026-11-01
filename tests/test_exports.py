import csv
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from lead_collector.exports import (
    CSV_COLUMNS,
    ExportError,
    csv_text,
    json_text,
    parse_json_text,
    write_export,
)
from lead_collector.model import LeadRow, ObservedValue


SOURCE = "https://directory.example.test/list"
OBSERVED = "2026-10-31T00:00:00Z"


def observed(raw: str, normalized: str | None = None, source: str = SOURCE) -> ObservedValue:
    return ObservedValue(raw, normalized if normalized is not None else raw, source, OBSERVED)


class ExportContractTests(unittest.TestCase):
    def test_csv_projects_unicode_formula_prefix_without_losing_original_evidence(self):
        """Removing formula neutralization or source evidence must fail this export contract."""
        row = LeadRow(
            "lead-1",
            company=observed("\u200b=SUM(1,1)"),
            website=observed("https://acme.example.test"),
            address=observed("One\nMain, City"),
            emails=(observed("hello@example.test", "hello@example.test", "https://acme.example.test/contact"),),
            socials=(observed("https://linkedin.com/company/acme"),),
        )

        projected = next(csv.DictReader(StringIO(csv_text((row,)))))

        self.assertEqual(tuple(projected), CSV_COLUMNS)
        self.assertEqual(projected["company"], "'\u200b=SUM(1,1)")
        self.assertEqual(projected["address"], "One\nMain, City")
        self.assertEqual(projected["emails"], "hello@example.test")
        evidence = json.loads(projected["evidence"])
        self.assertEqual(evidence["company"][0]["raw"], "\u200b=SUM(1,1)")
        self.assertEqual(evidence["emails"][0]["sourceUrl"], "https://acme.example.test/contact")

    def test_json_round_trip_keeps_row_metadata_and_rejects_duplicate_identifiers(self):
        """A lossy import or a duplicate lead ID would make review edits unsafe."""
        row = LeadRow(
            "lead-1",
            company=observed("Acme"),
            emails=(observed("sales@example.test", "sales@example.test", "https://acme.example.test/contact"),),
            edits={"company": "Acme Holdings"},
            excluded=True,
            status="completed",
        )

        restored = parse_json_text(json_text((row,)))

        self.assertEqual(restored[0].to_json(), row.to_json())
        duplicate = json.dumps({"version": 1, "rows": [row.to_json(), row.to_json()]})
        with self.assertRaises(ExportError):
            parse_json_text(duplicate)
        with self.assertRaises(ExportError):
            parse_json_text('{"version":2,"rows":[]}')
        with self.assertRaises(ExportError):
            parse_json_text('{"version":1.0,"rows":[]}')

    def test_json_import_rejects_unknown_row_schema_and_empty_identifier_but_keeps_legacy_evidence_optional(self):
        """Silently dropping row fields or accepting an unaddressable ID must fail strict version-one import."""
        row = LeadRow("lead-1", company=observed("Acme"))
        legacy = row.to_json()
        legacy.pop("evidence")

        self.assertEqual(parse_json_text(json.dumps({"version": 1, "rows": [legacy]}))[0].id, "lead-1")
        unknown = row.to_json()
        unknown["futureMetadata"] = "silently lost"
        with self.assertRaises(ExportError):
            parse_json_text(json.dumps({"version": 1, "rows": [unknown]}))
        empty_identifier = row.to_json()
        empty_identifier["id"] = ""
        with self.assertRaises(ExportError):
            parse_json_text(json.dumps({"version": 1, "rows": [empty_identifier]}))

    def test_atomic_writer_preserves_an_existing_output_without_overwrite(self):
        """Replacing a user's existing export without --overwrite must fail this test."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "leads.csv"
            output.write_text("do not replace", encoding="utf-8")

            with self.assertRaises(ExportError):
                write_export(output, "new export", overwrite=False)

            self.assertEqual(output.read_text(encoding="utf-8"), "do not replace")
            write_export(output, "new export", overwrite=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "new export")

    def test_atomic_writer_allows_exactly_one_concurrent_non_overwrite_writer(self):
        """Replacing a concurrently-created output must fail rather than clobbering its contents."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "leads.csv"
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def write(content: str) -> None:
                barrier.wait()
                try:
                    write_export(output, content, overwrite=False)
                    outcomes.append(content)
                except ExportError:
                    outcomes.append("rejected")

            first = threading.Thread(target=write, args=("first",))
            second = threading.Thread(target=write, args=("second",))
            first.start()
            second.start()
            barrier.wait()
            first.join()
            second.join()

            self.assertEqual(outcomes.count("rejected"), 1)
            self.assertIn(output.read_text(encoding="utf-8"), {"first", "second"})

    def test_atomic_writer_rejects_a_symlinked_ancestor_without_following_it(self):
        """Resolving before checking parents would turn this untrusted symlink into a writable directory."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            safe_parent = root / "safe"
            safe_parent.mkdir()
            linked_parent = root / "linked"
            linked_parent.symlink_to(safe_parent, target_is_directory=True)

            with self.assertRaises(ExportError):
                write_export(linked_parent / "leads.csv", "must not write", overwrite=False)

            self.assertFalse((safe_parent / "leads.csv").exists())

    def test_atomic_writer_keeps_a_held_parent_when_ancestor_is_swapped_to_symlink(self):
        """Re-resolving a checked parent after a directory swap must not overwrite the symlink destination."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            selected = root / "selected"
            selected.mkdir()
            other = root / "other"
            other.mkdir()
            other_output = other / "out.csv"
            other_output.write_text("keep", encoding="utf-8")
            parked = root / "parked"
            original_open = __import__("os").open
            swapped = False

            def swap_parent() -> None:
                nonlocal swapped
                if not swapped:
                    selected.rename(parked)
                    selected.symlink_to(other, target_is_directory=True)
                    swapped = True

            def swap_after_parent_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if not swapped and path == "selected" and dir_fd is not None:
                    swap_parent()
                return descriptor

            with patch("lead_collector.exports.os.open", new=swap_after_parent_open):
                write_export(selected / "out.csv", "new", overwrite=True)

            self.assertTrue(swapped)
            self.assertEqual(other_output.read_text(encoding="utf-8"), "keep")
            self.assertEqual((parked / "out.csv").read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main()
