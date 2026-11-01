"""Behavior tests for bounded CSV enrichment."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lead_collector import batch
from lead_collector.email_verify import VerificationDetails, VerificationError, VerificationResult
from lead_collector.model import JsonObject, PageDocument


def _page(url: str, html: str, status: str = "completed") -> PageDocument:
    error: JsonObject | None = None if status == "completed" else {"code": "FETCH_FAILED", "message": "failed", "url": url}
    return PageDocument(url, html, "2026-11-01T00:00:00Z", hashlib.sha256(html.encode()).hexdigest(), status, error)


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self._temporary.name).resolve()
        self.input_path = self.root / "businesses.csv"
        self.output_path = self.root / "enriched.csv"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_rows(self, rows: list[dict[str, str]]) -> None:
        with self.input_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=("company", "website", "email", "note"))
            writer.writeheader()
            writer.writerows(rows)

    def safe_rows(self) -> list[dict[str, str]]:
        with (self.root / "enriched.safe.csv").open(encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def test_default_verification_reaches_verifier_and_writes_typed_result(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        verified = VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None)
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "team@acme.example")), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=verified) as verify:
            summary = batch.enrich_csv(self.input_path, self.output_path)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(verify.call_args.args[0].email, "team@acme.example")
        self.assertEqual(summary["emailsChecked"], 1)
        with self.output_path.open(encoding="utf-8") as file:
            row = next(csv.DictReader(file))
        result = json.loads(row["email_verification"])[0]
        self.assertEqual(result["status"], "safe")
        self.assertTrue(result["mailboxVerified"])

    def test_safe_companion_contains_only_fully_verified_observed_contacts(self) -> None:
        self.write_rows([{"company": "=Acme", "website": "https://acme.example", "email": "unsafe-original@fixture.invalid", "note": ""}])
        verified = VerificationResult("Team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None)
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "Team@acme.example")), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=verified):
            summary = batch.enrich_csv(self.input_path, self.output_path)
        safe_path = self.root / "enriched.safe.csv"
        self.assertEqual(summary["safeContacts"], 1)
        self.assertEqual(summary["safeContactsOutput"], str(safe_path))
        row = self.safe_rows()[0]
        self.assertEqual(row["email"], "Team@acme.example")
        self.assertEqual(row["company"], "'=Acme")
        self.assertEqual(row["verification_status"], "safe")
        self.assertEqual(row["mailbox_verified"], "true")
        self.assertEqual(row["syntax_valid"], "true")
        self.assertEqual(row["catch_all"], "false")
        self.assertNotIn("published_emails", row)
        self.assertNotIn("unsafe-original@fixture.invalid", safe_path.read_text(encoding="utf-8"))

    def test_safe_companion_is_header_only_when_verification_is_skipped(self) -> None:
        self.write_rows([{"company": "Acme", "website": "", "email": "", "note": ""}])
        batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=False))
        safe_path = self.root / "enriched.safe.csv"
        with safe_path.open(encoding="utf-8") as file:
            reader = csv.DictReader(file)
            self.assertEqual(reader.fieldnames, list(batch.SAFE_COLUMNS))
            self.assertEqual(list(reader), [])

    def test_safe_companion_rejects_all_non_safe_verification_outcomes(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        page = _page("https://acme.example/", "team@acme.example")
        cases = (
            VerificationResult("team@acme.example", "risky", False, VerificationDetails(True, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "invalid", False, VerificationDetails(True, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "unknown", False, VerificationDetails(True, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "safe", False, VerificationDetails(True, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(False, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, False, True, False, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, False, False, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, True, True, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, True, False, True, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, True), None),
            VerificationResult("other@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None),
            VerificationResult("team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), VerificationError("VERIFY_FAILED", "failed")),
        )
        for result in cases:
            with self.subTest(result=result):
                with patch.object(batch, "fetch_page", return_value=page), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=result):
                    batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(overwrite=True))
                self.assertEqual(self.safe_rows(), [])

    def test_safe_companion_excludes_verification_limit(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        safe = VerificationResult("a@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None)
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "a@acme.example b@acme.example")), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=safe):
            summary = batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(max_emails=1))
        self.assertEqual(summary["safeContacts"], 1)
        self.assertEqual([row["email"] for row in self.safe_rows()], ["a@acme.example"])

    def test_safe_companion_combines_exact_business_email_observations(self) -> None:
        self.write_rows([
            {"company": "Acme", "website": "https://acme.example", "email": "", "note": ""},
            {"company": "Acme", "website": "https://acme.example", "email": "", "note": ""},
        ])
        pages = (_page("https://acme.example/one", "Team@acme.example"), _page("https://acme.example/two", "Team@acme.example"))
        safe = VerificationResult("Team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None)
        with patch.object(batch, "fetch_page", side_effect=pages), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=safe):
            summary = batch.enrich_csv(self.input_path, self.output_path)
        self.assertEqual(summary["safeContacts"], 1)
        row = self.safe_rows()[0]
        self.assertEqual(row["email"], "Team@acme.example")
        self.assertEqual(row["email_source_urls"], "https://acme.example/one; https://acme.example/two")

    def test_safe_companion_preserves_distinct_business_identity(self) -> None:
        self.write_rows([
            {"company": "Acme", "website": "https://acme.example", "email": "", "note": ""},
            {"company": "Beta", "website": "https://acme.example", "email": "", "note": ""},
        ])
        safe = VerificationResult("Team@acme.example", "safe", True, VerificationDetails(True, True, True, False, False, False), None)
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "Team@acme.example")), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=safe):
            summary = batch.enrich_csv(self.input_path, self.output_path)
        self.assertEqual(summary["safeContacts"], 2)
        self.assertEqual({row["company"] for row in self.safe_rows()}, {"Acme", "Beta"})

    def test_safe_output_input_alias_is_rejected_before_fetch(self) -> None:
        for spelling in ("alias.safe.csv", "Alias.safe.csv"):
            with self.subTest(spelling=spelling), tempfile.TemporaryDirectory(dir=self.root) as directory:
                folder = Path(directory)
                self.input_path, self.output_path = folder / spelling, folder / "alias.csv"
                self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
                original = self.input_path.read_bytes()
                aliases_input = (folder / "alias.safe.csv").exists()
                with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "No published email")) as fetch:
                    if aliases_input:
                        with self.assertRaises(batch.BatchError):
                            batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=False, overwrite=True))
                        fetch.assert_not_called()
                    else:
                        result = batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=False, overwrite=True))
                        self.assertEqual(result["safeContacts"], 0)
                        fetch.assert_called_once()
                self.assertEqual(self.input_path.read_bytes(), original)

    def test_safe_target_hazards_are_rejected_before_fetch(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        safe = self.root / "enriched.safe.csv"
        outside = self.root / "outside.csv"
        outside.write_text("owned", encoding="utf-8")
        for kind in ("exists", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                if safe.exists() or safe.is_symlink():
                    safe.unlink()
                if kind == "exists":
                    safe.write_text("owned", encoding="utf-8")
                elif kind == "symlink":
                    safe.symlink_to(outside)
                else:
                    os.link(outside, safe)
                with patch.object(batch, "fetch_page") as fetch:
                    with self.assertRaises(batch.BatchError):
                        batch.enrich_csv(self.input_path, self.output_path)
                fetch.assert_not_called()

    def test_safe_target_protected_parent_is_rejected_before_fetch(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        with patch.object(batch, "fetch_page") as fetch:
            with self.assertRaises(batch.BatchError):
                batch.enrich_csv(self.input_path, self.root / ".ssh" / "enriched.csv")
        fetch.assert_not_called()

    def test_case_alias_main_output_keeps_unchanged_binding_check(self) -> None:
        alias = self.root / "BUSINESSES.CSV"
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        if not alias.exists():
            self.skipTest("target filesystem is case-sensitive")

        def edit_input(_request: object) -> PageDocument:
            self.write_rows([{"company": "User edit", "website": "https://acme.example", "email": "", "note": ""}])
            return _page("https://acme.example/", "")

        with patch.object(batch, "fetch_page", side_effect=edit_input):
            with self.assertRaisesRegex(batch.BatchError, "input changed"):
                batch.enrich_csv(self.input_path, alias, batch.BatchOptions(verify_emails=False, overwrite=True))

    def test_collision_is_rejected_before_any_fetch(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        self.output_path.write_text("user output", encoding="utf-8")
        with patch.object(batch, "fetch_page") as fetch:
            with self.assertRaisesRegex(batch.BatchError, "output already exists"):
                batch.enrich_csv(self.input_path, self.output_path)
        fetch.assert_not_called()
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), "user output")

    def test_all_sites_are_validated_before_the_first_fetch(self) -> None:
        self.write_rows([
            {"company": "Acme", "website": "https://acme.example", "email": "", "note": ""},
            {"company": "Bad", "website": "file:///not-a-site", "email": "", "note": ""},
        ])
        with patch.object(batch, "fetch_page") as fetch:
            with self.assertRaises(batch.BatchError):
                batch.enrich_csv(self.input_path, self.output_path)
        fetch.assert_not_called()

    def test_same_file_preserves_an_edit_made_during_fetch(self) -> None:
        original = {"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}
        changed = {**original, "company": "USER EDIT"}
        self.write_rows([original])

        def edit_input(_request: object) -> PageDocument:
            self.write_rows([changed])
            return _page("https://acme.example/", "team@acme.example")

        with patch.object(batch, "fetch_page", side_effect=edit_input):
            with self.assertRaisesRegex(batch.BatchError, "input changed"):
                batch.enrich_csv(self.input_path, self.input_path, batch.BatchOptions(verify_emails=False, overwrite=True))
        with self.input_path.open(encoding="utf-8") as file:
            self.assertEqual(next(csv.DictReader(file))["company"], "USER EDIT")

    def test_duplicate_raw_email_is_checked_once_and_evidence_is_owned_by_both_rows(self) -> None:
        self.write_rows([
            {"company": "Alpha", "website": "https://acme.example", "email": "old@example.test", "note": ""},
            {"company": "Beta", "website": "https://acme.example", "email": "", "note": ""},
        ])
        verified = VerificationResult("Team@acme.example", "risky", False, VerificationDetails(True, True, True, True, False, False), None)
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "Team@acme.example")), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=verified) as verify:
            summary = batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=True))
        with self.output_path.open(encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(summary["emailsChecked"], 1)
        self.assertEqual(rows[0]["email"], "old@example.test")
        self.assertEqual([row["published_emails"] for row in rows], ["Team@acme.example", "Team@acme.example"])
        self.assertTrue(all(json.loads(row["email_verification"])[0]["status"] == "risky" for row in rows))
        self.assertTrue(all("https://acme.example/" in row["email_evidence"] for row in rows))

    def test_formula_prefixes_are_neutralized_in_all_export_cells(self) -> None:
        self.write_rows([{"company": "=2+2", "website": "", "email": "+unsafe@example.test", "note": "@formula"}])
        batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=False))
        with self.output_path.open(encoding="utf-8") as file:
            row = next(csv.DictReader(file))
        self.assertEqual(row["company"], "'=2+2")
        self.assertEqual(row["email"], "'+unsafe@example.test")
        self.assertEqual(row["note"], "'@formula")

    def test_blank_and_failed_sites_remain_distinct(self) -> None:
        self.write_rows([
            {"company": "Blank", "website": "", "email": "", "note": ""},
            {"company": "Failed", "website": "https://acme.example", "email": "", "note": ""},
        ])
        with patch.object(batch, "fetch_page", return_value=_page("https://acme.example/", "", "failed")):
            batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(verify_emails=False))
        with self.output_path.open(encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([row["site_status"] for row in rows], ["no_website", "failed"])

    def test_verification_cap_keeps_observed_contacts_with_limit_status(self) -> None:
        self.write_rows([{"company": "Acme", "website": "https://acme.example", "email": "", "note": ""}])
        page = _page("https://acme.example/", "a@acme.example b@acme.example")
        verified = VerificationResult("a@acme.example", "risky", False, VerificationDetails(True, True, True, True, False, False), None)
        with patch.object(batch, "fetch_page", return_value=page), patch.object(batch, "_trusted_binary", return_value=self.root / "runtime"), patch.object(batch, "verify_email", return_value=verified) as verify:
            summary = batch.enrich_csv(self.input_path, self.output_path, batch.BatchOptions(max_emails=1, verify_emails=True))
        with self.output_path.open(encoding="utf-8") as file:
            row = next(csv.DictReader(file))
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(summary["emailsChecked"], 1)
        self.assertIn("a@acme.example", row["published_emails"])
        self.assertEqual([item["status"] for item in json.loads(row["email_verification"])], ["risky", "not_checked_limit"])

    def test_row_limit_stops_validating_after_the_first_excess_row(self) -> None:
        self.write_rows([
            {"company": f"Company {index}", "website": "", "email": "", "note": ""}
            for index in range(2000)
        ])
        with patch.object(batch, "_validated_row", wraps=batch._validated_row) as validate:
            with self.assertRaisesRegex(batch.BatchError, "CSV rows exceed bounds"):
                batch.enrich_csv(self.input_path, self.output_path)
        self.assertEqual(validate.call_count, 1001)


if __name__ == "__main__":
    unittest.main()
