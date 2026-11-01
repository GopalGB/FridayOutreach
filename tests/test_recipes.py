import csv
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
from lead_collector.store import open_store, save_job


SOURCE = "https://directory.example.test/list"
OBSERVED = "2026-10-31T00:00:00Z"


def observed(raw: str, normalized: str | None = None, source: str = SOURCE) -> ObservedValue:
    return ObservedValue(raw, raw if normalized is None else normalized, source, OBSERVED)


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(arguments))
    return exit_code, json.loads(output.getvalue())


class RecipeAndExportCommandTests(unittest.TestCase):
    def test_selected_exports_import_losslessly_and_clear_requires_confirmation(self):
        """Exporting excluded/editable branches or importing them with new IDs must fail this local workflow."""
        kept = LeadRow(
            "keep",
            company=observed("\u200b=Acme"),
            website=observed("https://acme.example.test"),
            emails=(observed("sales@example.test", "sales@example.test", "https://acme.example.test/contact"),),
            edits={"company": "Acme Holdings"},
            status="completed",
        )
        excluded = LeadRow("skip", company=observed("Skip"), excluded=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "jobs.sqlite3"
                connection = open_store(state)
                save_job(connection, JobSnapshot("source", state="completed", progress={"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1}, rows=(kept.to_json(), excluded.to_json())), 0)
                connection.close()
                csv_output = root / "leads.csv"
                json_output = root / "leads.json"

                csv_exit, csv_result = invoke("--state", str(state), "export", "--job", "source", "--format", "csv", "--output", str(csv_output), "--row", "keep")
                json_exit, json_result = invoke("--state", str(state), "export", "--job", "source", "--format", "json", "--output", str(json_output), "--row", "keep", "--row", "skip", "--include-excluded")
                import_exit, imported = invoke("--state", str(state), "import", "--input", str(json_output), "--job", "local-copy")
                missing_confirm_exit, missing_confirm = invoke("--state", str(state), "clear")
                clear_exit, cleared = invoke("--state", str(state), "clear", "--confirm-clear")
                snapshot_exit, snapshot = invoke("--state", str(state), "snapshot", "--job", "local-copy")

                with csv_output.open(newline="", encoding="utf-8") as source:
                    csv_row = next(csv.DictReader(source))

        self.assertEqual(csv_exit, 0, csv_result)
        self.assertEqual(json_exit, 0, json_result)
        self.assertEqual(import_exit, 0, imported)
        self.assertEqual(csv_row["company"], "Acme Holdings")
        self.assertEqual(json.loads(csv_row["evidence"])["emails"][0]["sourceUrl"], "https://acme.example.test/contact")
        self.assertEqual(imported["data"]["rows"], [kept.to_json(), excluded.to_json()])
        self.assertEqual(missing_confirm_exit, 2, missing_confirm)
        self.assertEqual(clear_exit, 0, cleared)
        self.assertFalse(snapshot["ok"])
        self.assertEqual(snapshot_exit, 1, snapshot)

    def test_recipe_save_reopens_with_owned_html_and_rejects_layout_drift(self):
        """Silently rebinding a saved selection after its layout changes must fail this offline recipe flow."""
        original = "<table><tr><th>Name</th><th>Site</th></tr><tr><td>Acme</td><td>https://acme.example.test</td></tr></table>"
        drifted = "<table><tr><th>Name</th><th>Site</th><th>Region</th></tr><tr><td>Acme</td><td>https://acme.example.test</td><td>West</td></tr></table>"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "jobs.sqlite3"
                source = root / "source.html"
                source.write_text(original, encoding="utf-8")
                drift = root / "drift.html"
                drift.write_text(drifted, encoding="utf-8")
                recipe = root / "recipe.json"

                save_exit, saved = invoke("--state", str(state), "recipe", "save", "--html", str(source), "--source-url", SOURCE, "--mapping", '{"Name":"company","Site":"website"}', "--output", str(recipe))
                preview_exit, preview = invoke("--state", str(state), "recipe", "preview", "--recipe", str(recipe), "--html", str(source), "--source-url", SOURCE)
                drift_exit, drift_result = invoke("--state", str(state), "recipe", "preview", "--recipe", str(recipe), "--html", str(drift), "--source-url", SOURCE)

        self.assertEqual(save_exit, 0, saved)
        self.assertEqual(preview_exit, 0, preview)
        self.assertEqual(preview["data"]["rows"][0]["company"]["normalized"], "Acme")
        self.assertEqual(drift_exit, 2, drift_result)
        self.assertEqual(drift_result["error"]["code"], "LAYOUT_DRIFT")


if __name__ == "__main__":
    unittest.main()
