"""Behavior tests for bounded CSV enrichment."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lead_collector import batch
from lead_collector.email_verify import VerificationDetails, VerificationResult
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
                batch.enrich_csv(self.input_path, self.input_path, batch.BatchOptions(overwrite=True))
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
        batch.enrich_csv(self.input_path, self.output_path)
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
            batch.enrich_csv(self.input_path, self.output_path)
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
