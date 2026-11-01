from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lead_collector.store as store
from lead_collector.cli import main
from lead_collector.model import JobSnapshot, LeadRow, ObservedValue
from lead_collector.store import load_job, open_store, save_job


SOURCE = "https://directory.example.test/list"
OBSERVED = "2026-10-31T00:00:00Z"


def observed(value: str) -> ObservedValue:
    return ObservedValue(value, value, SOURCE, OBSERVED)


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(arguments))
    return exit_code, json.loads(output.getvalue())


class ReviewCommandTests(unittest.TestCase):
    def test_review_queries_sorts_and_selects_from_one_thousand_persisted_rows(self):
        """Ignoring a query, sort, or selected row ID must fail this persisted review flow."""
        rows = tuple(
            LeadRow(
                f"lead-{index:04d}",
                company=observed(f"Company {1000 - index:04d}"),
                website=observed(f"https://company-{index:04d}.example.test"),
                address=observed(f"{index} Main Street"),
            ).to_json()
            for index in range(1000)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "jobs.sqlite3"
                connection = open_store(state)
                save_job(connection, JobSnapshot("bulk", state="completed", progress={"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1}, rows=rows), 0)
                connection.close()

                exit_code, response = invoke("--state", str(state), "review", "--job", "bulk", "--query", "company", "--sort", "company", "--row", "lead-0001", "--row", "lead-0999")

        self.assertEqual(exit_code, 0, response)
        data = response["data"]
        assert isinstance(data, dict)
        listed = data["rows"]
        assert isinstance(listed, list)
        self.assertEqual([row["id"] for row in listed], ["lead-0999", "lead-0001"])

    def test_edit_and_exclude_keep_original_evidence_and_other_same_domain_branch(self):
        """Mutating raw observations or collapsing a same-domain branch must fail this review flow."""
        shared = observed("https://acme.example.test")
        first = LeadRow("north", company=observed("North Acme"), website=shared, address=observed("1 Main"))
        second = LeadRow("south", company=observed("South Acme"), website=shared, address=observed("2 Main"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "jobs.sqlite3"
                connection = open_store(state)
                save_job(connection, JobSnapshot("branches", state="completed", progress={"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1}, rows=(first.to_json(), second.to_json())), 0)
                connection.close()

                edit_code, edited = invoke("--state", str(state), "edit", "--job", "branches", "--row", "north", "--field", "company", "--value", "North Acme Holdings")
                exclude_code, excluded = invoke("--state", str(state), "exclude", "--job", "branches", "--row", "north")
                connection = open_store(state)
                saved = load_job(connection, "branches")
                connection.close()

        self.assertEqual(edit_code, 0, edited)
        self.assertEqual(exclude_code, 0, excluded)
        assert saved is not None
        restored = {row["id"]: LeadRow.from_json(row) for row in saved.rows}
        self.assertEqual(restored["north"].edits["company"], "North Acme Holdings")
        self.assertEqual(restored["north"].company.raw, "North Acme")
        self.assertTrue(restored["north"].excluded)
        self.assertEqual(restored["south"].address.normalized, "2 Main")
        self.assertFalse(restored["south"].excluded)


if __name__ == "__main__":
    unittest.main()
