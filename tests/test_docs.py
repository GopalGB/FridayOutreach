"""Tests for the offline documentation structure checker."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import importlib.util
import sys


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("check_docs", ROOT / "scripts" / "check_docs.py")
assert SPEC is not None and SPEC.loader is not None
check_docs_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_docs_module
SPEC.loader.exec_module(check_docs_module)


class DocumentationCheckTests(unittest.TestCase):
    def test_reports_broken_local_markdown_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("[missing](absent.md)\n", encoding="utf-8")
            errors = check_docs_module.check_markdown(root, [root / "README.md"])
        self.assertTrue(any("missing local link target" in error for error in errors))

    def test_resolves_root_community_file_and_heading_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CONTRIBUTING.md").write_text("# Local Development\n", encoding="utf-8")
            (root / "docs").mkdir()
            readme = root / "docs" / "guide.md"
            readme.write_text("[contributing](../CONTRIBUTING.md#local-development)\n", encoding="utf-8")
            errors = check_docs_module.check_markdown(root, [readme, root / "CONTRIBUTING.md"])
        self.assertEqual(errors, [])

    def test_ignores_link_like_text_inside_fenced_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            readme.write_text("```markdown\n[example](missing.md)\n```\n", encoding="utf-8")
            errors = check_docs_module.check_markdown(root, [readme])
        self.assertEqual(errors, [])

    def test_accepts_state_diagram_after_mermaid_directive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            readme.write_text("```mermaid\n%%{init: {}}%%\nstateDiagram-v2\nA --> B\n```\n", encoding="utf-8")
            errors = check_docs_module.check_markdown(root, [readme])
        self.assertEqual(errors, [])

    def test_rejects_malformed_json_example(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            examples = root / "examples"
            examples.mkdir()
            (examples / "function-call.json").write_text('{"input_csv": }', encoding="utf-8")
            errors = check_docs_module.check_examples(root)
        self.assertTrue(any("invalid JSON" in error for error in errors))

    def test_checks_example_against_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "examples").mkdir()
            (root / "integrations").mkdir()
            (root / "docs").mkdir()
            (root / "examples" / "function-call.json").write_text(json.dumps({"input_csv": "in.csv"}), encoding="utf-8")
            schema = {"type": "object", "additionalProperties": False, "required": ["input_csv", "output_csv"], "properties": {"input_csv": {"type": "string"}, "output_csv": {"type": "string"}}}
            for path, name in (("function-tool.json", "lead_collector_enrich"), ("fridayoutreach-function-tool.json", "friday_outreach_enrich")):
                (root / "integrations" / path).write_text(json.dumps({"name": name, "input_schema": schema}), encoding="utf-8")
            (root / "docs" / "AGENT-USAGE.md").write_text("lead_collector_enrich friday_outreach_enrich", encoding="utf-8")
            errors = check_docs_module.check_examples(root)
        self.assertTrue(any("missing required property 'output_csv'" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
