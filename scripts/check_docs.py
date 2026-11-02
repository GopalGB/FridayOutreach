"""Offline structural checks for Markdown links, examples, forms, and fences."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import html
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


DIAGRAM_STARTS = {
    "architecture-beta",
    "block-beta",
    "c4context",
    "classdiagram",
    "erdiagram",
    "flowchart",
    "graph",
    "gitgraph",
    "gantt",
    "journey",
    "kanban",
    "mindmap",
    "packet-beta",
    "pie",
    "quadrantchart",
    "requirementdiagram",
    "sankey-beta",
    "sequencediagram",
    "statediagram",
    "statediagram-v2",
    "timeline",
    "treemap-beta",
    "xychart-beta",
    "zenuml",
}
ISSUE_FIELD_TYPES = {"checkboxes", "dropdown", "input", "markdown", "textarea"}
ISSUE_FORM_FIELDS = {
    "bug_report.yml": {"version", "reproduction", "result", "environment"},
    "feature_request.yml": {"problem", "scope", "verification"},
}


@dataclass(frozen=True)
class Fence:
    marker: str
    length: int
    info: str
    content: str
    line: int


def split_fences(text: str, source: str) -> tuple[str, list[Fence], list[str]]:
    """Remove fenced blocks from Markdown and report malformed delimiters."""
    visible: list[str] = []
    fences: list[Fence] = []
    errors: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not opening:
            visible.append(line)
            index += 1
            continue
        marker = opening.group(1)[0]
        length = len(opening.group(1))
        info = opening.group(2).strip()
        start_line = index + 1
        block: list[str] = []
        index += 1
        closing = re.compile(rf"^ {{0,3}}{re.escape(marker)}{{{length},}}\s*$")
        while index < len(lines) and not closing.match(lines[index]):
            block.append(lines[index])
            index += 1
        if index == len(lines):
            errors.append(f"{source}:{start_line}: unclosed fenced code block")
        else:
            index += 1
        fences.append(Fence(marker, length, info, "\n".join(block), start_line))
        visible.append("")
    return "\n".join(visible), fences, errors


def _strip_inline_code(text: str) -> str:
    return re.sub(r"(`+).*?\1", "", text)


def _slug(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = _strip_inline_code(text)
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    text = text.replace("&", "")
    text = re.sub(r"[^\w\-\s]", "", text, flags=re.UNICODE).lower()
    return re.sub(r"\s+", "-", text.strip())


def markdown_anchors(text: str) -> set[str]:
    visible, _, _ = split_fences(text, "<markdown>")
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for line in visible.splitlines():
        explicit = re.findall(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", line, flags=re.I)
        explicit += re.findall(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", line, flags=re.I)
        anchors.update(explicit)
        heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if not heading:
            continue
        base = _slug(heading.group(2))
        if not base:
            continue
        suffix = seen.get(base, 0)
        seen[base] = suffix + 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def _markdown_targets(text: str) -> list[str]:
    """Read inline Markdown link/image destinations, excluding inline code."""
    text = _strip_inline_code(text)
    targets: list[str] = []
    index = 0
    while index < len(text):
        marker = text.find("](", index)
        if marker < 0:
            break
        start = marker + 2
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        if text[start] == "<":
            end = text.find(">", start + 1)
            if end < 0:
                index = start + 1
                continue
            targets.append(text[start + 1 : end])
            index = end + 1
            continue
        end = start
        parens = 0
        while end < len(text):
            char = text[end]
            if char == "(" and parens == 0:
                parens += 1
            elif char == ")":
                if parens:
                    parens -= 1
                else:
                    break
            if char.isspace() and parens == 0:
                break
            end += 1
        destination = text[start:end].rstrip(")")
        if destination:
            targets.append(destination)
        index = max(end, marker + 2) + 1
    return targets


def check_markdown(root: Path, markdown_files: list[Path]) -> list[str]:
    errors: list[str] = []
    contents: dict[Path, str] = {}
    for path in markdown_files:
        try:
            contents[path] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            errors.append(f"{path.relative_to(root)}: cannot read Markdown: {error}")
    anchors = {path: markdown_anchors(text) for path, text in contents.items()}
    for path, text in contents.items():
        relative = path.relative_to(root).as_posix()
        visible, fences, fence_errors = split_fences(text, relative)
        errors.extend(fence_errors)
        for fence in fences:
            language = fence.info.split(None, 1)[0].lower() if fence.info else ""
            if language != "mermaid":
                continue
            first = next(
                (line.strip() for line in fence.content.splitlines() if line.strip() and not line.lstrip().startswith("%%")),
                "",
            )
            diagram = first.split(None, 1)[0].lower() if first else ""
            if not first or diagram not in DIAGRAM_STARTS:
                errors.append(f"{relative}:{fence.line}: Mermaid fence has no recognized diagram declaration")
        for destination in _markdown_targets(visible):
            parsed = urlsplit(html.unescape(destination))
            if parsed.scheme or parsed.netloc:
                continue
            target_text = unquote(parsed.path)
            target = (root / target_text.lstrip("/")) if target_text.startswith("/") else (path.parent / target_text)
            target = target.resolve()
            if root.resolve() not in target.parents and target != root.resolve():
                errors.append(f"{relative}: local link escapes the repository: {destination}")
                continue
            if target_text and not target.is_file():
                errors.append(f"{relative}: missing local link target: {destination}")
                continue
            fragment = unquote(parsed.fragment)
            if not fragment:
                continue
            if target_text:
                target_content = contents.get(target)
                if target_content is None and target.suffix.lower() == ".md" and target.is_file():
                    try:
                        target_content = target.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        target_content = None
                    if target_content is not None:
                        anchors[target] = markdown_anchors(target_content)
                valid_anchors = anchors.get(target)
            else:
                valid_anchors = anchors.get(path)
            if valid_anchors is not None and fragment not in valid_anchors:
                errors.append(f"{relative}: missing Markdown anchor #{fragment} in {target.relative_to(root).as_posix()}")
    return errors


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid quoted scalar: {error.msg}") from error
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_parse_scalar(part) for part in inner.split(",")]
    return value


def parse_issue_yaml(text: str) -> Any:
    """Parse the mapping/list/block-scalar subset used by GitHub issue forms."""
    lines = text.splitlines()

    def meaningful(start: int) -> int:
        while start < len(lines) and (not lines[start].strip() or lines[start].lstrip().startswith("#")):
            start += 1
        return start

    def parse_block(start: int, indent: int) -> tuple[Any, int]:
        start = meaningful(start)
        if start >= len(lines):
            return {}, start
        is_list = lines[start].startswith(" " * indent + "- ")
        result: Any = [] if is_list else {}
        while start < len(lines):
            start = meaningful(start)
            if start >= len(lines):
                break
            raw = lines[start]
            current_indent = len(raw) - len(raw.lstrip(" "))
            if current_indent < indent:
                break
            if current_indent != indent:
                raise ValueError(f"unexpected indentation on line {start + 1}")
            content = raw[indent:]
            if is_list:
                if not content.startswith("- "):
                    break
                item_text = content[2:]
                if ":" not in item_text:
                    result.append(_parse_scalar(item_text))
                    start += 1
                    continue
                key, value = item_text.split(":", 1)
                item: dict[str, Any] = {}
                value = value.strip()
                start += 1
                if value == "|":
                    block, start = parse_literal(start, indent + 4)
                    item[key] = block
                elif value:
                    item[key] = _parse_scalar(value)
                else:
                    child = meaningful(start)
                    child_indent = len(lines[child]) - len(lines[child].lstrip(" ")) if child < len(lines) else indent + 2
                    if child < len(lines) and child_indent > indent:
                        item[key], start = parse_block(child, child_indent)
                    else:
                        item[key] = {}
                continuation = meaningful(start)
                if continuation < len(lines):
                    continuation_indent = len(lines[continuation]) - len(lines[continuation].lstrip(" "))
                    if continuation_indent > indent:
                        extra, start = parse_block(continuation, continuation_indent)
                        if not isinstance(extra, dict):
                            raise ValueError(f"expected mapping continuation on line {continuation + 1}")
                        item.update(extra)
                result.append(item)
            else:
                if content.startswith("- ") or ":" not in content:
                    raise ValueError(f"expected mapping entry on line {start + 1}")
                key, value = content.split(":", 1)
                value = value.strip()
                start += 1
                if value == "|":
                    result[key], start = parse_literal(start, indent + 2)
                elif value:
                    result[key] = _parse_scalar(value)
                else:
                    child = meaningful(start)
                    child_indent = len(lines[child]) - len(lines[child].lstrip(" ")) if child < len(lines) else indent + 2
                    if child < len(lines) and child_indent > indent:
                        result[key], start = parse_block(child, child_indent)
                    else:
                        result[key] = {}
        return result, start

    def parse_literal(start: int, indent: int) -> tuple[str, int]:
        collected: list[str] = []
        while start < len(lines):
            line = lines[start]
            current_indent = len(line) - len(line.lstrip(" ")) if line.strip() else indent
            if line.strip() and current_indent < indent:
                break
            collected.append(line[indent:] if line.strip() else "")
            start += 1
        return "\n".join(collected).rstrip(), start

    first = meaningful(0)
    if first >= len(lines):
        raise ValueError("empty YAML document")
    parsed, end = parse_block(first, len(lines[first]) - len(lines[first].lstrip(" ")))
    if meaningful(end) < len(lines):
        raise ValueError(f"unexpected content on line {meaningful(end) + 1}")
    return parsed


def validate_issue_form(path: Path, document: Any) -> list[str]:
    errors: list[str] = []
    name = path.name
    if not isinstance(document, dict):
        return [f"{path}: issue form must be a YAML mapping"]
    if name == "config.yml":
        if document.get("blank_issues_enabled") is not False:
            errors.append(f"{path}: blank_issues_enabled must be false")
        links = document.get("contact_links")
        if not isinstance(links, list) or not links:
            errors.append(f"{path}: contact_links must be a non-empty list")
        else:
            for index, link in enumerate(links):
                if not isinstance(link, dict) or not all(isinstance(link.get(key), str) and link[key].strip() for key in ("name", "url", "about")):
                    errors.append(f"{path}: contact_links[{index}] requires name, url, and about strings")
                elif not link["url"].startswith("https://"):
                    errors.append(f"{path}: contact_links[{index}] URL must use HTTPS")
        return errors
    for key in ("name", "description", "title"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            errors.append(f"{path}: {key} must be a non-empty string")
    labels = document.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(label, str) and label for label in labels):
        errors.append(f"{path}: labels must be a non-empty list of strings")
    body = document.get("body")
    if not isinstance(body, list):
        errors.append(f"{path}: body must be a list of issue-form fields")
        return errors
    ids: set[str] = set()
    found_fields: set[str] = set()
    for index, field in enumerate(body):
        where = f"{path}: body[{index}]"
        if not isinstance(field, dict):
            errors.append(f"{where} must be a mapping")
            continue
        kind = field.get("type")
        if kind not in ISSUE_FIELD_TYPES:
            errors.append(f"{where} has unsupported type {kind!r}")
            continue
        if kind == "markdown":
            if not isinstance(field.get("attributes"), dict) or not isinstance(field["attributes"].get("value"), str):
                errors.append(f"{where} markdown field requires attributes.value text")
            continue
        identifier = field.get("id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{where} requires a non-empty id")
        elif identifier in ids:
            errors.append(f"{where} duplicates field id {identifier!r}")
        else:
            ids.add(identifier)
            found_fields.add(identifier)
        attributes = field.get("attributes")
        if not isinstance(attributes, dict) or not isinstance(attributes.get("label"), str) or not attributes["label"].strip():
            errors.append(f"{where} requires attributes.label")
        validations = field.get("validations", {})
        if not isinstance(validations, dict) or not isinstance(validations.get("required"), bool):
            errors.append(f"{where} requires a boolean validations.required")
    expected = ISSUE_FORM_FIELDS.get(name)
    if expected is not None and found_fields != expected:
        errors.append(f"{path}: field ids differ from the documented required fields: expected {sorted(expected)}, got {sorted(found_fields)}")
    return errors


def _schema_errors(schema: Any, value: Any, location: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{location}: schema must be an object"]
    kind = schema.get("type")
    validators: dict[str, Callable[[Any], bool]] = {
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
    }
    valid_type = validators.get(kind) if isinstance(kind, str) else None
    if valid_type is None:
        return [f"{location}: unsupported JSON schema type {kind!r}"]
    if not valid_type(value):
        return [f"{location}: expected {kind}, got {type(value).__name__}"]
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{location}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{location}: unknown property {key!r}")
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_errors(properties[key], item, f"{location}.{key}"))
    if kind in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{location}: value is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{location}: value is above maximum {schema['maximum']}")
    return errors


def check_examples(root: Path) -> list[str]:
    errors: list[str] = []
    json_files = sorted(path for path in root.rglob("*.json") if ".git" not in path.parts and ".venv" not in path.parts)
    loaded: dict[str, Any] = {}
    for path in json_files:
        relative = path.relative_to(root).as_posix()
        try:
            loaded[relative] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"{relative}: invalid JSON: {error}")
    sample = loaded.get("examples/function-call.json")
    if sample is None:
        errors.append("examples/function-call.json: documented request example is missing")
    for relative, expected_name in (
        ("integrations/function-tool.json", "lead_collector_enrich"),
        ("integrations/fridayoutreach-function-tool.json", "friday_outreach_enrich"),
    ):
        tool = loaded.get(relative)
        if not isinstance(tool, dict):
            errors.append(f"{relative}: tool definition must be an object")
            continue
        if tool.get("name") != expected_name:
            errors.append(f"{relative}: expected documented function name {expected_name!r}")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            errors.append(f"{relative}: input_schema must be a closed object schema")
            continue
        required = schema.get("required", [])
        if not {"input_csv", "output_csv"}.issubset(required):
            errors.append(f"{relative}: input_csv and output_csv must be required")
        if sample is not None:
            errors.extend(_schema_errors(schema, sample, "examples/function-call.json"))
    usage_path = root / "docs" / "AGENT-USAGE.md"
    if usage_path.is_file():
        usage = usage_path.read_text(encoding="utf-8")
        for function_name in ("lead_collector_enrich", "friday_outreach_enrich"):
            if function_name not in usage:
                errors.append(f"docs/AGENT-USAGE.md: missing documented function name {function_name!r}")
    return errors


def check_issue_forms(root: Path) -> list[str]:
    errors: list[str] = []
    directory = root / ".github" / "ISSUE_TEMPLATE"
    for name in ("bug_report.yml", "feature_request.yml", "config.yml"):
        path = directory / name
        relative = path.relative_to(root).as_posix()
        if not path.is_file():
            errors.append(f"{relative}: required issue form is missing")
            continue
        try:
            document = parse_issue_yaml(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{relative}: invalid issue form YAML: {error}")
            continue
        errors.extend(validate_issue_form(Path(relative), document))
    return errors


def check_docs(root: Path) -> list[str]:
    markdown_files = sorted(
        path for path in root.rglob("*.md") if not any(part in {".git", ".venv", "node_modules"} for part in path.parts)
    )
    return check_markdown(root, markdown_files) + check_examples(root) + check_issue_forms(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check documentation links and examples without network access.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors = check_docs(root)
    if errors:
        print("Offline documentation checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    markdown_count = sum(1 for _ in root.rglob("*.md"))
    json_count = sum(1 for _ in root.rglob("*.json"))
    print(f"Offline documentation checks passed ({markdown_count} Markdown files, {json_count} JSON files, 2 issue forms).")
    print("Mermaid check validates fenced-block closure and a recognized diagram declaration; it does not render diagrams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
