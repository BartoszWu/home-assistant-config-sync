"""Turn security scan hits into review warnings with source line numbers.

Never include matched values. Field names from the closed sensitive-key set
are allowed so the reviewer can see *what* was flagged.
"""

from __future__ import annotations

import re

import yaml

from security import text_reason, unsafe_findings

WARNING_LIMIT = 20
_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def format_scan_warnings(value, source_text: str) -> list[dict]:
    warnings = []
    for finding in unsafe_findings(value):
        line = locate_finding_line(source_text, finding)
        warnings.append({
            "reason": finding["reason"],
            "path": finding.get("path") or "$",
            "field": finding.get("field"),
            "line": line,
        })
        if len(warnings) >= WARNING_LIMIT:
            break
    return warnings


def locate_finding_line(source_text: str, finding: dict) -> int | None:
    if not source_text:
        return None
    parts = parse_json_path(finding.get("path") or "$")
    if parts:
        line = _yaml_path_line(source_text, parts)
        if line:
            return line
    field = finding.get("field")
    if field:
        return _first_field_line(source_text, field)
    return _first_text_reason_line(source_text)


def parse_json_path(path: str) -> list:
    if not path or path == "$":
        return []
    rest = path[1:] if path.startswith("$") else path
    parts = []
    for match in _PATH_TOKEN.finditer(rest):
        if match.group(1):
            parts.append(match.group(1))
        else:
            parts.append(int(match.group(2)))
    return parts


def _yaml_path_line(source_text: str, parts: list) -> int | None:
    try:
        node = yaml.compose(source_text)
    except yaml.YAMLError:
        return None
    return _walk_yaml_node(node, parts)


def _walk_yaml_node(node, parts: list) -> int | None:
    if node is None or not parts:
        return None
    head, *rest = parts
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else None
            if key != head:
                continue
            if not rest:
                return key_node.start_mark.line + 1
            return _walk_yaml_node(value_node, rest)
    elif isinstance(node, yaml.SequenceNode) and isinstance(head, int):
        if 0 <= head < len(node.value):
            child = node.value[head]
            if not rest:
                return child.start_mark.line + 1
            return _walk_yaml_node(child, rest)
    return None


def _first_field_line(source_text: str, field: str) -> int | None:
    needles = (f"{field}:", f'"{field}"', f"'{field}'")
    for index, line in enumerate(source_text.splitlines(), 1):
        if any(needle in line for needle in needles):
            return index
    return None


def _first_text_reason_line(source_text: str) -> int | None:
    for index, line in enumerate(source_text.splitlines(), 1):
        if text_reason(line):
            return index
    return 1 if source_text.strip() else None
