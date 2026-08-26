import hashlib
import json
import re


BOOTSTRAP_STATUS = "READY TO APPLY — NEW DASHBOARD"
MISSING_BASE_STATUS = "IN SYNC — BASE NOT INITIALIZED"
APPLYABLE_STATUSES = {"READY TO APPLY", BOOTSTRAP_STATUS}

HASH_RE = re.compile(r"^[0-9a-f]{64}$")

ROOT_KEYS = {"views"}
VIEW_KEYS = {
    "type",
    "title",
    "path",
    "icon",
    "theme",
    "background",
    "max_columns",
    "dense_section_placement",
    "visible",
    "subview",
    "back_path",
    "sections",
    "cards",
    "badges",
}
SECTION_KEYS = {
    "type",
    "title",
    "cards",
    "column_span",
    "visibility",
}
EMPTY_HEADING_KEYS = {"type", "heading", "heading_style", "icon"}


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _contains_only_empty_heading_cards(cards):
    if cards in (None, []):
        return True
    if not isinstance(cards, list):
        return False
    for card in cards:
        if not isinstance(card, dict):
            return False
        if card.get("type") != "heading":
            return False
        if set(card) - EMPTY_HEADING_KEYS:
            return False
    return True


def is_empty_dashboard(config):
    """Recognize a new dashboard shell without relying on one exact JSON hash."""
    if not isinstance(config, dict) or set(config) - ROOT_KEYS:
        return False
    views = config.get("views")
    if not isinstance(views, list) or len(views) != 1:
        return False
    view = views[0]
    if not isinstance(view, dict) or set(view) - VIEW_KEYS:
        return False
    if view.get("badges") not in (None, []):
        return False
    if not _contains_only_empty_heading_cards(view.get("cards")):
        return False
    sections = view.get("sections")
    if sections in (None, []):
        return True
    if not isinstance(sections, list) or len(sections) != 1:
        return False
    section = sections[0]
    if not isinstance(section, dict) or set(section) - SECTION_KEYS:
        return False
    return _contains_only_empty_heading_cards(section.get("cards"))


def classify(github, current, base, unsafe=None):
    github_hash = digest(github)
    current_hash = digest(current)
    if unsafe:
        return "UNSAFE", "unsafe", False, unsafe
    if base is None:
        if github_hash == current_hash:
            return (
                MISSING_BASE_STATUS,
                "missing-base",
                False,
                "GitHub and HA match, but the exported base is missing. Request Export to initialize it.",
            )
        if is_empty_dashboard(current):
            return (
                BOOTSTRAP_STATUS,
                "bootstrap",
                True,
                "HA contains only an empty dashboard shell. Apply will bootstrap it after a fresh preview hash check.",
            )
        return (
            "CONFLICT",
            "conflict",
            False,
            "No exported base exists and HA already contains dashboard content. Automatic adoption is blocked.",
        )
    if github_hash == current_hash:
        return "SAME", "same", False, "GitHub HEAD matches HA current."
    github_changed = github_hash != base
    ha_changed = current_hash != base
    if github_changed and not ha_changed:
        return (
            "READY TO APPLY",
            "ready",
            True,
            "GitHub changed while HA still matches the last exported base.",
        )
    if not github_changed and ha_changed:
        return (
            "CHANGED IN HA",
            "changed",
            False,
            "HA changed locally while GitHub still matches the base. Run HA Config Sync — Export to publish the HA change.",
        )
    return (
        "CONFLICT",
        "conflict",
        False,
        "Both GitHub and HA changed from the last exported base. Nothing can be applied automatically.",
    )


def parse_preview_hashes(values):
    previews = {}
    for value in values:
        relative, separator, preview_hash = value.rpartition(":")
        if not separator or not relative or not HASH_RE.fullmatch(preview_hash):
            raise ValueError("Invalid preview hash payload.")
        if relative in previews:
            raise ValueError("Duplicate preview hash payload.")
        previews[relative] = preview_hash
    return previews


def matches_preview(current, preview_hash):
    return bool(HASH_RE.fullmatch(preview_hash)) and digest(current) == preview_hash
