import hashlib
import json
import re


BOOTSTRAP_STATUS = "READY TO APPLY — NEW DASHBOARD"
CREATE_STATUS = "READY TO APPLY — CREATE DASHBOARD"
MISSING_BASE_STATUS = "IN SYNC — BASE NOT INITIALIZED"
NONCANONICAL_STATUS = "LIVE FROM FEATURE"
APPLYABLE_STATUSES = {"READY TO APPLY", BOOTSTRAP_STATUS, CREATE_STATUS}

CREATE_REASON = (
    "HA has no dashboard with this URL path. "
    "Apply will create it, then save the Git configuration."
)
UNSAVED_REASON = (
    "HA has the dashboard but no saved Lovelace config yet. "
    "Apply will save the Git configuration."
)
NO_HYPHEN_REASON = (
    "HA has no dashboard with this URL path. "
    "Home Assistant requires a hyphen in the URL, so Import cannot create it."
)
DASHBOARD_PATH_RE = re.compile(r"^[a-z0-9_-]+$")
MDI_ICON_RE = re.compile(r"^mdi:[a-z0-9-]+$")

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


def _canonical_effective_base(base, github_hash, current_hash, provenance):
    """Prefer last canonical Apply as BASE when Export's Git BASE is stale.

    Export `state/dashboard-bases.json` moves only after a successful dashboard
    sync. A verified Import Apply from `main` already recorded the last common
    Git↔LIVE hash in provenance. If that hash still matches Git or LIVE and the
    exported BASE matches neither, using the stale BASE would label a Git-only
    (or HA-only) change as CONFLICT.
    """
    applied_hash = getattr(provenance, "content_hash", None)
    if not isinstance(applied_hash, str) or not HASH_RE.fullmatch(applied_hash):
        return base
    export_agrees = isinstance(base, str) and base in {github_hash, current_hash}
    apply_agrees = applied_hash in {github_hash, current_hash}
    if apply_agrees and not export_agrees:
        return applied_hash
    return base


def classify_with_provenance(
    github,
    current,
    base,
    *,
    live_hash=None,
    reviewing_canonical=False,
    provenance=None,
    unsafe=None,
):
    """Three-way classify, using feature deployment hash instead of Git BASE when LIVE is non-canonical."""
    if provenance is not None and not getattr(provenance, "canonical", True):
        applied_hash = getattr(provenance, "content_hash", None)
        github_hash = digest(github)
        current_hash = live_hash or digest(current)
        source_ref = getattr(provenance, "source_ref", "feature")
        short = provenance.short_sha() if hasattr(provenance, "short_sha") else "?"
        if reviewing_canonical:
            if github_hash == current_hash:
                return (
                    "SAME",
                    "same",
                    False,
                    (
                        f"LIVE matches {getattr(provenance, 'canonical_branch', 'main')} "
                        "and can be marked canonical."
                    ),
                    True,
                )
            return (
                NONCANONICAL_STATUS,
                "changed",
                False,
                (
                    f"LIVE is deployed from {source_ref} @ {short}. "
                    "Canonical main sync is disabled until LIVE matches main."
                ),
                False,
            )
        effective_base = applied_hash if isinstance(applied_hash, str) else base
        status, css, selectable, reason = classify(
            github, current, effective_base, unsafe=unsafe
        )
        if status == "CHANGED IN HA":
            reason = (
                f"HA changed after the {source_ref} @{short} deployment. "
                "Export will not publish this dashboard to main."
            )
        elif status == "SAME":
            reason = f"Git source matches LIVE ({source_ref} @ {short})."
        return status, css, selectable, reason, False
    effective_base = base
    if provenance is not None and getattr(provenance, "canonical", False):
        effective_base = _canonical_effective_base(
            base,
            digest(github),
            live_hash or digest(current),
            provenance,
        )
    status, css, selectable, reason = classify(
        github, current, effective_base, unsafe=unsafe
    )
    return status, css, selectable, reason, False


def classify(github, current, base, unsafe=None):
    github_hash = digest(github)
    current_hash = digest(current)
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


def can_create_dashboard_path(url_path):
    """Home Assistant rejects storage dashboards whose url_path has no hyphen."""
    return isinstance(url_path, str) and "-" in url_path and bool(
        DASHBOARD_PATH_RE.fullmatch(url_path)
    )


def classify_missing_ha(url_path, *, registered):
    """Status when Git has a dashboard JSON but Lovelace config cannot be read."""
    if not url_path or registered:
        return BOOTSTRAP_STATUS, "bootstrap", True, UNSAVED_REASON
    if can_create_dashboard_path(url_path):
        return CREATE_STATUS, "bootstrap", True, CREATE_REASON
    return "CONFLICT", "conflict", False, NO_HYPHEN_REASON


def dashboard_registration_payload(url_path, github):
    """Fields for lovelace/dashboards/create. Title/icon come from the Git views."""
    if not can_create_dashboard_path(url_path):
        raise ValueError("Dashboard URL path cannot be created in Home Assistant.")
    title = None
    icon = None
    views = github.get("views") if isinstance(github, dict) else None
    if isinstance(views, list):
        for view in views:
            if not isinstance(view, dict):
                continue
            if title is None:
                value = view.get("title")
                if isinstance(value, str) and value.strip():
                    title = value.strip()
            if icon is None:
                value = view.get("icon")
                if isinstance(value, str) and MDI_ICON_RE.fullmatch(value):
                    icon = value
            if title and icon:
                break
    if not title:
        stem = url_path.removeprefix("dashboard-")
        title = stem.replace("-", " ").strip().title() or url_path
    payload = {
        "url_path": url_path,
        "title": title,
        "show_in_sidebar": True,
        "require_admin": False,
    }
    if icon:
        payload["icon"] = icon
    return payload


# Ephemeral agent scratch dashboard. Never exported, synced, reviewed or
# applied. Mirrored in export/dashboard_manifest.py (separate container, no
# shared module); keep both copies in sync.
EPHEMERAL_DASHBOARD_URL_PATHS = frozenset({"dashboard-preview"})


def is_ephemeral_dashboard(value):
    """True for the ephemeral preview dashboard, given as url_path or filename."""
    if not isinstance(value, str):
        return False
    name = value.rsplit("/", 1)[-1]
    if name.endswith(".json"):
        name = name[: -len(".json")]
    return name in EPHEMERAL_DASHBOARD_URL_PATHS
