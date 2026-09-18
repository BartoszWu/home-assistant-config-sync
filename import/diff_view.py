"""GitHub-style hunked side-by-side diffs for Import review."""

from __future__ import annotations

import difflib
import json
import re

CONTEXT_LINES = 3

IN_SYNC_STATUSES = frozenset({
    "SAME",
    "IN SYNC — BASE NOT INITIALIZED",
    "OK",
})

_ANCHOR_RE = re.compile(r"[^A-Za-z0-9]+")


def empty() -> dict:
    return {"added": 0, "removed": 0, "blocks": []}


def json_lines(value) -> list[str]:
    return json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ).splitlines()


def file_anchor(kind: str, relative: str) -> str:
    slug = _ANCHOR_RE.sub("-", f"{kind}-{relative}").strip("-").lower()
    return f"file-{slug}"


def is_changed_review(change: dict) -> bool:
    if change.get("added") or change.get("removed"):
        return True
    return change.get("status") not in IN_SYNC_STATUSES


def summarize_changed_files(
    dashboards: list[dict],
    managed: list[dict],
    resources: list[dict] | None = None,
) -> dict:
    files = []
    for change in dashboards:
        if not is_changed_review(change):
            continue
        files.append(_summary_item("dashboard", f"dashboards/{change['relative']}", change))
    for change in managed:
        if not is_changed_review(change):
            continue
        files.append(_summary_item("managed", change["relative"], change))
    for change in resources or []:
        if not is_changed_review(change):
            continue
        files.append(_summary_item("resource", change["relative"], change))
    return {
        "count": len(files),
        "added": sum(item["added"] for item in files),
        "removed": sum(item["removed"] for item in files),
        "files": files,
    }


def _summary_item(kind: str, path: str, change: dict) -> dict:
    return {
        "kind": kind,
        "path": path,
        "anchor": change.get("anchor") or file_anchor(kind, change["relative"]),
        "added": change.get("added") or 0,
        "removed": change.get("removed") or 0,
        "status": change["status"],
        "css": change["css"],
    }


def hunk_rows(diff: dict) -> list[dict]:
    rows = []
    for block in diff.get("blocks") or []:
        if block.get("type") == "hunk":
            rows.extend(block.get("rows") or [])
    return rows


def compare_json(current, github, context: int = CONTEXT_LINES) -> dict:
    return compare_lines(json_lines(current), json_lines(github), context)


def compare_text(left: str, right: str, context: int = CONTEXT_LINES) -> dict:
    return compare_lines(left.splitlines(), right.splitlines(), context)


def compare_lines(
    left: list[str],
    right: list[str],
    context: int = CONTEXT_LINES,
) -> dict:
    matcher = difflib.SequenceMatcher(a=left, b=right)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    groups = list(matcher.get_grouped_opcodes(context))
    return {
        "added": added,
        "removed": removed,
        "blocks": _blocks(groups, left, right),
    }


def _blocks(groups: list, left: list[str], right: list[str]) -> list[dict]:
    if not groups:
        return []
    blocks = []
    previous_i2 = 0
    for group in groups:
        i1 = group[0][1]
        gap = i1 - previous_i2
        if gap > 0:
            blocks.append({"type": "gap", "count": gap})
        rows = []
        for tag, gi1, gi2, gj1, gj2 in group:
            rows.extend(_rows_for_opcode(tag, gi1, gi2, gj1, gj2, left, right))
        last = group[-1]
        blocks.append({
            "type": "hunk",
            "header": _hunk_header(group[0][1], last[2], group[0][3], last[4]),
            "rows": rows,
        })
        previous_i2 = last[2]
    trailing = len(left) - previous_i2
    if trailing > 0:
        blocks.append({"type": "gap", "count": trailing})
    return blocks


def _hunk_header(i1: int, i2: int, j1: int, j2: int) -> str:
    return f"@@ -{_span(i1, i2)} +{_span(j1, j2)} @@"


def _span(start: int, end: int) -> str:
    length = end - start
    if length == 0:
        return f"{start},0"
    if length == 1:
        return str(start + 1)
    return f"{start + 1},{length}"


def _rows_for_opcode(tag, i1, i2, j1, j2, left, right):
    left_part = left[i1:i2]
    right_part = right[j1:j2]
    width = max(len(left_part), len(right_part), 0)
    rows = []
    for offset in range(width):
        has_left = offset < len(left_part)
        has_right = offset < len(right_part)
        rows.append({
            "left_no": i1 + offset + 1 if has_left else None,
            "left": left_part[offset] if has_left else "",
            "left_css": (
                "left-del" if has_left and tag != "equal"
                else ("blank" if not has_left else "")
            ),
            "right_no": j1 + offset + 1 if has_right else None,
            "right": right_part[offset] if has_right else "",
            "right_css": (
                "right-add" if has_right and tag != "equal"
                else ("blank" if not has_right else "")
            ),
        })
    return rows
