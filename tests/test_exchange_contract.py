from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lead_collector.store as store
from lead_collector.cli import main
from lead_collector.exports import json_text, parse_json_text
from lead_collector.model import JobSnapshot, LeadRow, ObservedValue
from lead_collector.store import open_store, save_job


SOURCE = "https://directory.example.test/list"
OBSERVED = "2026-10-31T00:00:00Z"


def observed(value: str, source: str = SOURCE) -> ObservedValue:
    return ObservedValue(value, value, source, OBSERVED)


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        code = main(list(arguments))
    return code, json.loads(output.getvalue())


class ExchangeContractTests(unittest.TestCase):
    def test_observed_value_rejects_unknown_nested_metadata_everywhere(self) -> None:
        row = LeadRow("one", company=observed("Acme"), emails=(observed("a@example.test"),), generic={"x": (observed("generic"),)}, evidence={"company": (observed("evidence"),)})
        for location in ("company", "emails", "generic", "evidence"):
            payload = row.to_json()
            if location == "company":
                payload["company"]["extra"] = "reject"
            elif location == "emails":
                payload["emails"][0]["extra"] = "reject"
            else:
                payload[location][next(iter(payload[location]))][0]["extra"] = "reject"
            with self.subTest(location=location):
                with self.assertRaises(ValueError):
                    LeadRow.from_json(payload)

    def test_valid_roundtrip_preserves_unicode_newlines_sources_edits_and_exclusion(self) -> None:
        row = LeadRow("one", company=observed("Acme\n☃"), emails=(observed("a@example.test", "https://other.example.test"),), edits={"company": "Edited\n☃"}, excluded=True, status="completed")
        self.assertEqual(parse_json_text(json_text((row,)))[0].to_json(), row.to_json())

    def test_cli_export_collision_reports_named_code_and_preserves_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(store, "MANAGED_STATE_ROOT", root):
                state = root / "state.sqlite3"
                connection = open_store(state)
                save_job(connection, JobSnapshot("job", state="completed", rows=(LeadRow("row", company=observed("Acme")).to_json(),)), 0)
                connection.close()
                output = root / "existing.csv"
                output.write_text("original", encoding="utf-8")
                code, envelope = invoke("--state", str(state), "export", "--job", "job", "--format", "csv", "--output", str(output), "--row", "row")
            self.assertEqual(code, 2)
            self.assertEqual(envelope["error"]["code"], "OUTPUT_EXISTS")
            self.assertEqual(output.read_text(encoding="utf-8"), "original")

    def test_cli_import_classifies_schema_and_malformed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root / "state.sqlite3"
            for name, content, expected in (("version.json", '{"version":2,"rows":[]}', "SCHEMA_UNSUPPORTED"), ("bad.json", "not json", "INPUT_INVALID"), ("types.json", '{"version":"1","rows":[]}', "INPUT_INVALID"), ("bool.json", '{"version":true,"rows":[]}', "INPUT_INVALID"), ("float.json", '{"version":1.0,"rows":[]}', "INPUT_INVALID")):
                path = root / name
                path.write_text(content, encoding="utf-8")
                with patch.object(store, "MANAGED_STATE_ROOT", root):
                    code, envelope = invoke("--state", str(state), "import", "--input", str(path), "--job", name)
                self.assertEqual(code, 2)
                self.assertEqual(envelope["error"]["code"], expected)


if __name__ == "__main__":
    unittest.main()
