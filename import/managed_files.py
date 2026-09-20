"""Managed Files engine: policy, path guards, profiles, staging, Apply."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from dashboard_logic import (
    APPLYABLE_STATUSES,
    MISSING_BASE_STATUS,
    HASH_RE,
    digest,
    matches_preview,
)
from diff_view import compare_json, compare_text, empty as empty_diff, file_anchor, hunk_rows
from review_warnings import format_scan_warnings

POLICY_PATH = Path(__file__).with_name("managed_files.yaml")
BASES_PATH = Path("/data/managed-file-bases.json")
LAST_APPLY_PATH = Path("/data/last-apply.json")
BACKUP_ROOT = Path("/data/managed-file-backups")
JOURNAL_PATH = Path("/data/managed-file-journal.json")
PREVIEW_DIRNAME = ".config-sync-preview"
STAGING_SHA_LEN = 12
FRONTEND_PREFIX_EXTENSIONS = frozenset({".js", ".mjs"})
CACHE_BUST_CONTENT_HASH = "content_hash"
CACHE_BUST_MODES = frozenset({CACHE_BUST_CONTENT_HASH})
CACHE_BUST_HASH_LEN = 8
RESOURCE_URL_RE = re.compile(r"^/local/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.(?:js|mjs)$")
RELATIVE_IMPORT_RE = re.compile(
    r"""(?<![A-Za-z0-9_])(?:import\s+(?:[^'"\n]+?\s+from\s+)?|export\s+[^'"\n]*?\s+from\s+)['"](\.[^'"]+)['"]"""
)

# First Apply when BASE is missing and LIVE already differs from Git.
# Requires explicit checkbox selection; Initialize bases never adopts this drift.
MANAGED_BOOTSTRAP_STATUS = "READY TO APPLY — NO BASE"
MANAGED_APPLYABLE_STATUSES = frozenset(APPLYABLE_STATUSES | {MANAGED_BOOTSTRAP_STATUS})
RESOURCE_CREATE_STATUS = "READY TO APPLY — CREATE RESOURCE"
RESOURCE_OK_STATUS = "OK"
RESOURCE_APPLYABLE_STATUSES = frozenset({RESOURCE_CREATE_STATUS})
RESOURCE_CREATE_REASON = (
    "HA has no Lovelace resource at this URL. "
    "Apply will create it as type module."
)
RESOURCE_OK_REASON = "Lovelace resource already exists with the desired type."
RESOURCE_TYPE_CONFLICT_REASON = (
    "A Lovelace resource exists at this URL with a different type. "
    "Import will not change it."
)
RESOURCE_AMBIGUOUS_REASON = (
    "Multiple Lovelace resources match this URL; Import will not guess resource_id."
)
ALLOWED_RESOURCE_TYPES = frozenset({"module"})

APPLY_LOCK = threading.RLock()

PROFILES = frozenset({"package", "frontend_module", "custom_template"})

# Hard denylist — independent of allowlist (defense in depth).
FORBIDDEN_NAMES = frozenset({
    "secrets.yaml",
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
})
FORBIDDEN_PARTS = frozenset({
    ".storage",
    "custom_components",
    "deps",
    "tts",
    ".cloud",
    "backups",
    ".ssh",
    ".config-sync",
    ".config-sync-preview",
})
FORBIDDEN_SUFFIXES = (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3")

RELATIVE_PATH_RE = re.compile(
    r"^(?:packages|www|custom_templates)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)
PREFIX_RE = re.compile(
    r"^(?:packages|www|custom_templates)/(?:[A-Za-z0-9._-]+/)+$"
)


@dataclass(frozen=True)
class ManagedEntry:
    path: str
    profile: str
    resource_url: str | None = None
    cache_bust: str | None = None


@dataclass(frozen=True)
class PrefixRule:
    prefix: str
    extensions: tuple[str, ...]
    profile: str
    cache_bust: str | None = None


@dataclass(frozen=True)
class LovelaceResourceSpec:
    url: str
    type: str


@dataclass(frozen=True)
class ManagedPolicy:
    ha_root: Path
    exact: tuple[ManagedEntry, ...]
    prefixes: tuple[PrefixRule, ...]
    resources: tuple[LovelaceResourceSpec, ...] = ()

    @property
    def entries(self) -> list[ManagedEntry]:
        return list(self.exact)


@dataclass
class FileSnapshot:
    exists: bool
    data: bytes | None
    sha256: str | None


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_policy(path: Path | None = None) -> ManagedPolicy:
    policy_file = path or POLICY_PATH
    raw = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("managed_files.yaml must be a mapping.")
    root = raw.get("ha_config_root", "/homeassistant")
    if not isinstance(root, str) or not root.startswith("/") or root == "/":
        raise RuntimeError("ha_config_root must be an absolute non-root path.")
    entries_raw = raw.get("managed_files")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise RuntimeError("managed_files must be a non-empty list.")
    entries: list[ManagedEntry] = []
    prefixes: list[PrefixRule] = []
    seen: set[str] = set()
    seen_prefixes: set[str] = set()
    for item in entries_raw:
        if not isinstance(item, dict):
            raise RuntimeError("Each managed_files entry must be a mapping.")
        profile = item.get("profile")
        if not isinstance(profile, str) or profile not in PROFILES:
            raise RuntimeError(f"Unknown managed file profile: {profile}")
        has_path = "path" in item
        has_prefix = "prefix" in item
        if has_path == has_prefix:
            raise RuntimeError("Each managed_files entry must have either path or prefix.")
        if has_prefix:
            prefixes.append(_parse_prefix_rule(item, profile, seen_prefixes))
            continue
        relative = item.get("path")
        if not isinstance(relative, str):
            raise RuntimeError("managed_files path entries require a string path.")
        if item.get("extensions") is not None:
            raise RuntimeError("extensions are only valid on prefix entries.")
        validate_policy_path(relative)
        if relative in seen:
            raise RuntimeError(f"Duplicate managed file path: {relative}")
        seen.add(relative)
        resource_url, cache_bust = _parse_cache_bust_fields(item, profile, relative=relative)
        entries.append(ManagedEntry(
            path=relative,
            profile=profile,
            resource_url=resource_url,
            cache_bust=cache_bust,
        ))
    return ManagedPolicy(
        ha_root=Path(root),
        exact=tuple(entries),
        prefixes=tuple(prefixes),
        resources=_parse_resources(raw.get("resources")),
    )


def _parse_prefix_rule(item: dict, profile: str, seen_prefixes: set[str]) -> PrefixRule:
    prefix = item.get("prefix")
    extensions_raw = item.get("extensions")
    if not isinstance(prefix, str) or not prefix:
        raise RuntimeError("managed_files prefix entries require a string prefix.")
    if profile != "frontend_module":
        raise RuntimeError("Prefix discovery is only supported for frontend_module.")
    if not prefix.endswith("/"):
        raise RuntimeError("managed_files prefix must end with '/'.")
    if prefix.startswith("/") or prefix.startswith("~") or "\\" in prefix:
        raise RuntimeError("Absolute or backslash prefixes are forbidden.")
    if any(part in {"", ".", ".."} for part in prefix.split("/")[:-1]):
        raise RuntimeError("Path traversal is forbidden in prefix.")
    if not PREFIX_RE.fullmatch(prefix):
        raise RuntimeError(f"Prefix outside managed roots or invalid: {prefix}")
    if not prefix.startswith("www/") or prefix.startswith(f"www/{PREVIEW_DIRNAME}/"):
        raise RuntimeError("Frontend prefix must live under www/ and outside preview staging.")
    if any(part.lower() in FORBIDDEN_PARTS for part in PurePosixPath(prefix).parts):
        raise RuntimeError(f"Forbidden path segment in prefix {prefix}")
    if prefix in seen_prefixes:
        raise RuntimeError(f"Duplicate managed file prefix: {prefix}")
    if not isinstance(extensions_raw, list) or not extensions_raw:
        raise RuntimeError("Prefix entries require a non-empty extensions list.")
    extensions: list[str] = []
    for ext in extensions_raw:
        if not isinstance(ext, str) or not ext.startswith(".") or "/" in ext or "\\" in ext:
            raise RuntimeError(f"Invalid managed file extension: {ext}")
        lowered = ext.lower()
        if lowered not in FRONTEND_PREFIX_EXTENSIONS:
            raise RuntimeError(f"Unsupported prefix extension: {ext}")
        if lowered not in extensions:
            extensions.append(lowered)
    seen_prefixes.add(prefix)
    _resource_url, cache_bust = _parse_cache_bust_fields(item, profile, prefix=True)
    return PrefixRule(
        prefix=prefix,
        extensions=tuple(extensions),
        profile=profile,
        cache_bust=cache_bust,
    )


def default_resource_url(relative: str) -> str:
    return f"/local/{www_relative(relative)}"


def validate_resource_url(url: str) -> None:
    if not isinstance(url, str) or not url:
        raise ValueError("Empty Lovelace resource URL.")
    if "\\" in url or ".." in url.split("/"):
        raise ValueError("Path traversal is forbidden in resource_url.")
    if "?" in url or "#" in url:
        raise ValueError("resource_url must be a path without a query string.")
    if not RESOURCE_URL_RE.fullmatch(url):
        raise ValueError(f"resource_url must be a /local frontend module path: {url}")


def _parse_cache_bust_fields(
    item: dict,
    profile: str,
    *,
    relative: str | None = None,
    prefix: bool = False,
) -> tuple[str | None, str | None]:
    cache_bust = item.get("cache_bust")
    resource_url = item.get("resource_url")
    if cache_bust is None and resource_url is None:
        return None, None
    if profile != "frontend_module":
        raise RuntimeError("cache_bust and resource_url are only valid for frontend_module.")
    if cache_bust is not None and cache_bust not in CACHE_BUST_MODES:
        raise RuntimeError(f"Unknown cache_bust mode: {cache_bust}")
    if resource_url is not None:
        if prefix:
            raise RuntimeError(
                "resource_url is only valid on exact path entries; "
                "prefix rules derive /local URLs from discovered files."
            )
        if not isinstance(resource_url, str):
            raise RuntimeError("resource_url must be a string.")
        try:
            validate_resource_url(resource_url)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        if relative is not None and resource_url != default_resource_url(relative):
            raise RuntimeError(
                f"resource_url {resource_url} does not match "
                f"{default_resource_url(relative)} for {relative}."
            )
    elif cache_bust is not None and relative is not None:
        resource_url = default_resource_url(relative)
    if cache_bust is None:
        raise RuntimeError("resource_url requires cache_bust.")
    return resource_url, cache_bust


def _parse_resources(raw) -> tuple[LovelaceResourceSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RuntimeError("resources must be a list.")
    specs: list[LovelaceResourceSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("Each resources entry must be a mapping.")
        url = item.get("url")
        resource_type = item.get("type")
        extra = set(item) - {"url", "type"}
        if extra:
            raise RuntimeError(f"Unknown resources fields: {sorted(extra)}")
        if not isinstance(url, str):
            raise RuntimeError("resources url must be a string.")
        try:
            validate_resource_url(url)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        if resource_type not in ALLOWED_RESOURCE_TYPES:
            raise RuntimeError(
                f"resources type must be one of {sorted(ALLOWED_RESOURCE_TYPES)}."
            )
        if url in seen:
            raise RuntimeError(f"Duplicate resources url: {url}")
        seen.add(url)
        specs.append(LovelaceResourceSpec(url=url, type=resource_type))
    return tuple(specs)


def validate_policy_path(relative: str) -> None:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Empty managed path.")
    if relative.startswith("/") or relative.startswith("~"):
        raise ValueError("Absolute managed paths are forbidden.")
    if "\\" in relative:
        raise ValueError("Backslash paths are forbidden.")
    raw_parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("Path traversal is forbidden.")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("Path traversal is forbidden.")
    if not RELATIVE_PATH_RE.fullmatch(relative):
        raise ValueError(f"Path outside managed roots or invalid: {relative}")
    name = path.name.lower()
    if name in FORBIDDEN_NAMES:
        raise ValueError(f"Forbidden file name: {name}")
    lowered_parts = {part.lower() for part in path.parts}
    if lowered_parts & FORBIDDEN_PARTS:
        raise ValueError(f"Forbidden path segment in {relative}")
    if name.endswith(FORBIDDEN_SUFFIXES):
        raise ValueError(f"Forbidden database-like suffix: {name}")
    if path.parts[0] == "www" and PREVIEW_DIRNAME in path.parts:
        raise ValueError("Preview staging paths cannot be managed targets.")
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden path segments are forbidden.")


def validate_prefix_member(relative: str, rule: PrefixRule) -> None:
    validate_policy_path(relative)
    if not relative.startswith(rule.prefix):
        raise ValueError(f"Path is outside prefix {rule.prefix}")
    suffix = Path(relative).suffix.lower()
    if suffix not in rule.extensions:
        raise ValueError(f"Extension {suffix or '(none)'} is not allowed under {rule.prefix}")


def discover_managed_entries(workdir: Path, policy: ManagedPolicy) -> list[ManagedEntry]:
    """Exact allowlist plus Git-tree discovery under prefix rules. Delete is not a candidate."""
    found: list[ManagedEntry] = list(policy.exact)
    seen = {entry.path for entry in found}
    for rule in policy.prefixes:
        prefix_dir = workdir.joinpath(*PurePosixPath(rule.prefix.rstrip("/")).parts)
        if not prefix_dir.exists():
            continue
        if prefix_dir.is_symlink() or not prefix_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(prefix_dir, followlinks=False):
            dirnames[:] = [
                name for name in dirnames
                if name != PREVIEW_DIRNAME
                and name not in FORBIDDEN_PARTS
                and not name.startswith(".")
            ]
            current = Path(dirpath)
            if current.is_symlink():
                continue
            for filename in filenames:
                candidate = current / filename
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    rel = candidate.relative_to(workdir).as_posix()
                    validate_prefix_member(rel, rule)
                except ValueError:
                    continue
                if rel in seen:
                    continue
                seen.add(rel)
                is_entry_module = (
                    PurePosixPath(rel).parent
                    == PurePosixPath(rule.prefix.rstrip("/"))
                )
                resource_url = (
                    default_resource_url(rel)
                    if rule.cache_bust and is_entry_module else None
                )
                found.append(ManagedEntry(
                    path=rel,
                    profile=rule.profile,
                    resource_url=resource_url,
                    cache_bust=rule.cache_bust if is_entry_module else None,
                ))
    found.sort(key=lambda entry: entry.path)
    return found


def resource_specs_for_entries(
    policy: ManagedPolicy,
    entries: list[ManagedEntry] | tuple[ManagedEntry, ...],
) -> tuple[LovelaceResourceSpec, ...]:
    """Combine explicit resources with modules from the App-owned allowlist."""
    specs = list(policy.resources)
    seen = {spec.url for spec in specs}
    for entry in entries:
        if entry.profile != "frontend_module" or not entry.resource_url:
            continue
        if entry.resource_url in seen:
            continue
        seen.add(entry.resource_url)
        specs.append(LovelaceResourceSpec(url=entry.resource_url, type="module"))
    return tuple(specs)


def relative_import_hints(relative: str, text: str, known_paths: set[str]) -> list[str]:
    """Best-effort warning only. Does not parse JS or block Apply."""
    if not relative.startswith("www/"):
        return []
    hints = []
    parent = PurePosixPath(relative).parent
    seen: set[str] = set()
    for match in RELATIVE_IMPORT_RE.finditer(text):
        spec = match.group(1)
        if spec in seen:
            continue
        seen.add(spec)
        try:
            resolved = (parent / spec).as_posix()
            if ".." in PurePosixPath(os.path.normpath(resolved)).parts:
                hints.append(
                    f"Relative import {spec} walks above the module directory; staging keeps 1:1 paths."
                )
                continue
            normalized = os.path.normpath(resolved).replace("\\", "/")
            if normalized in known_paths:
                hints.append(
                    f"Relative import {spec} resolves to {normalized}. "
                    "Select that file too if the preview should load it."
                )
            else:
                hints.append(
                    f"Relative import {spec} was not found among discovered managed files."
                )
        except (ValueError, OSError):
            hints.append(f"Relative import {spec} could not be resolved.")
    return hints


def resolve_live_path(ha_root: Path, relative: str) -> Path:
    """Resolve allowlisted relative path under HA config; reject escapes/symlinks."""
    validate_policy_path(relative)
    root = ha_root.resolve(strict=False)
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Resolved path escapes HA config root.") from error
    # Walk parents and the final path: no symlink may leave the root.
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            target = current.resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError as error:
                raise ValueError("Symlink escapes HA config root.") from error
            if any(piece.lower() in FORBIDDEN_PARTS for piece in target.relative_to(root).parts):
                raise ValueError("Symlink points at a forbidden location.")
        if current.exists() and current.is_dir() and current.is_symlink():
            # Directory symlink already checked above; continue.
            pass
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("Refusing to operate on a symlink target path.")
    # Final hard denylist on resolved relative form.
    rel = candidate.relative_to(root).as_posix()
    validate_policy_path(rel) if rel == relative else validate_policy_path(relative)
    if any(part.lower() in FORBIDDEN_PARTS for part in candidate.parts):
        raise ValueError("Resolved path hits a forbidden segment.")
    if candidate.name.lower() in FORBIDDEN_NAMES:
        raise ValueError("Resolved path hits a forbidden file name.")
    return candidate


def read_snapshot(path: Path) -> FileSnapshot:
    if not path.exists():
        return FileSnapshot(exists=False, data=None, sha256=None)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Refusing non-regular file: {path}")
    data = path.read_bytes()
    return FileSnapshot(exists=True, data=data, sha256=file_digest(data))


def classify_file(github_hash: str, live_hash: str | None, base: str | None, unsafe=None):
    """Three-way classify using content SHA-256 hashes (None = LIVE absent)."""
    if base is None:
        if live_hash is None:
            return (
                "READY TO APPLY",
                "ready",
                True,
                "File is absent in HA. Apply will create it from Git after a fresh preview check.",
            )
        if github_hash == live_hash:
            return (
                MISSING_BASE_STATUS,
                "missing-base",
                False,
                "GitHub and HA match, but the managed-file base is missing. Initialize the base.",
            )
        return (
            MANAGED_BOOTSTRAP_STATUS,
            "bootstrap",
            True,
            "No managed-file base exists and HA already differs from Git. "
            "Apply will overwrite LIVE with Git after a fresh preview hash check and set the base only on success. "
            "Initialize bases will not adopt this drift.",
        )
    if live_hash is None:
        if github_hash == base:
            return (
                "CHANGED IN HA",
                "changed",
                False,
                "HA no longer has this file while Git still matches the base.",
            )
        return (
            "CONFLICT",
            "conflict",
            False,
            "File missing in HA while Git also moved from the base.",
        )
    if github_hash == live_hash:
        return "SAME", "same", False, "Git source matches HA current."
    github_changed = github_hash != base
    ha_changed = live_hash != base
    if github_changed and not ha_changed:
        return (
            "READY TO APPLY",
            "ready",
            True,
            "GitHub changed while HA still matches the last applied base.",
        )
    if not github_changed and ha_changed:
        return (
            "CHANGED IN HA",
            "changed",
            False,
            "HA changed locally while GitHub still matches the base.",
        )
    return (
        "CONFLICT",
        "conflict",
        False,
        "Both GitHub and HA changed from the last applied base.",
    )


def load_bases(path: Path | None = None) -> dict:
    target = path or BASES_PATH
    if not target.exists():
        return {"schema_version": 1, "files": {}}
    value = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        return {"schema_version": 1, "files": {}}
    files = value.get("files", {})
    if not isinstance(files, dict):
        files = {}
    return {"schema_version": 1, "files": files}


def save_bases(bases: dict, path: Path | None = None) -> None:
    target = path or BASES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "files": bases.get("files", {})}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def base_hash_for(bases: dict, relative: str) -> str | None:
    entry = bases.get("files", {}).get(relative)
    if isinstance(entry, str) and HASH_RE.fullmatch(entry):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
        value = entry["sha256"]
        if HASH_RE.fullmatch(value):
            return value
    return None


def set_base_hash(
    bases: dict,
    relative: str,
    sha256: str,
    *,
    source_ref: str | None = None,
    commit_sha: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "sha256": sha256,
        "updated_at": now,
    }
    if source_ref or commit_sha:
        entry["last_applied"] = {
            "source_ref": source_ref,
            "commit_sha": commit_sha,
            "sha256": sha256,
            "applied_at": now,
        }
    bases.setdefault("files", {})[relative] = entry


def load_last_apply(path: Path | None = None) -> dict | None:
    target = path or LAST_APPLY_PATH
    if not target.exists():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def record_last_apply(
    *,
    source_ref: str,
    commit_sha: str,
    dashboards: list[str] | None = None,
    managed_files: list[str] | None = None,
    path: Path | None = None,
) -> None:
    target = path or LAST_APPLY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "source_ref": source_ref,
        "commit_sha": commit_sha,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "dashboards": list(dashboards or []),
        "managed_files": list(managed_files or []),
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def side_by_side_text(left: str, right: str):
    diff = compare_text(left, right)
    return diff, diff["added"], diff["removed"]


def _managed_review(entry: ManagedEntry, **fields) -> dict:
    diff = fields.pop("diff", None) or empty_diff()
    return {
        "kind": "managed",
        "relative": entry.path,
        "profile": entry.profile,
        "anchor": file_anchor("managed", entry.path),
        "diff": diff,
        "rows": hunk_rows(diff),
        "added": diff["added"],
        "removed": diff["removed"],
        **fields,
    }


def decode_text(data: bytes) -> str:
    return data.decode("utf-8")


def staging_commit_dir(commit_sha: str) -> str:
    if not re.fullmatch(r"^[0-9a-f]{7,40}$", commit_sha):
        raise ValueError("Staging requires a hexadecimal commit SHA.")
    return commit_sha[:STAGING_SHA_LEN]


def www_relative(relative: str) -> str:
    validate_policy_path(relative)
    if not relative.startswith("www/"):
        raise ValueError("Staging is only for www frontend modules.")
    return relative[len("www/"):]


def resource_url_path(url: str) -> str:
    """Strip query/fragment so /local/foo.mjs?v=5 matches /local/foo.mjs."""
    if not isinstance(url, str) or not url:
        return ""
    return url.split("#", 1)[0].split("?", 1)[0]


def resource_urls_match(left: str, right: str) -> bool:
    return resource_url_path(left) == resource_url_path(right)


def cache_bust_token(content_sha256: str) -> str:
    if not HASH_RE.fullmatch(content_sha256):
        raise ValueError("cache-bust token requires a SHA-256 hex digest.")
    return content_sha256[:CACHE_BUST_HASH_LEN]


def cache_busted_resource_url(resource_url: str, content_sha256: str) -> str:
    validate_resource_url(resource_url)
    return f"{resource_url}?v={cache_bust_token(content_sha256)}"


def staging_relative(commit_sha: str, relative: str) -> str:
    return f"www/{PREVIEW_DIRNAME}/{staging_commit_dir(commit_sha)}/{www_relative(relative)}"


def staging_public_url(commit_sha: str, relative: str, content_hash: str) -> str:
    return (
        f"/local/{PREVIEW_DIRNAME}/{staging_commit_dir(commit_sha)}/"
        f"{www_relative(relative)}?v={content_hash}"
    )


def ensure_frontend_staging(
    ha_root: Path,
    relative: str,
    data: bytes,
    content_hash: str,
    commit_sha: str,
) -> dict:
    """Write Git desired bytes under www/.config-sync-preview/<commit>/ — never canonical."""
    under_www = www_relative(relative)
    root = ha_root.resolve(strict=False)
    preview_root = (root / "www" / PREVIEW_DIRNAME).resolve(strict=False)
    preview_root.relative_to(root / "www")
    commit_root = (preview_root / staging_commit_dir(commit_sha)).resolve(strict=False)
    commit_root.relative_to(preview_root)
    target = (commit_root / under_www).resolve(strict=False)
    try:
        target.relative_to(commit_root)
    except ValueError as error:
        raise ValueError("Staging path escapes the commit preview directory.") from error
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.is_symlink():
        raise ValueError("Refusing symlink in preview staging.")
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".stage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    verified = read_snapshot(target)
    if verified.sha256 != content_hash:
        raise RuntimeError("Staging read-back hash mismatch.")
    return {
        "staged_relative": staging_relative(commit_sha, relative),
        "preview_url": staging_public_url(commit_sha, relative, content_hash),
        "content_hash": content_hash,
        "commit_sha": commit_sha,
    }


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError("Refusing to overwrite non-regular file.")
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def backup_live(relative: str, snapshot: FileSnapshot, stamp: str) -> dict:
    backup_dir = BACKUP_ROOT / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_name = relative.replace("/", "__")
    meta = {"path": relative, "existed": snapshot.exists, "sha256": snapshot.sha256}
    if snapshot.exists and snapshot.data is not None:
        backup_file = backup_dir / safe_name
        backup_file.write_bytes(snapshot.data)
        meta["backup_file"] = str(backup_file)
    else:
        marker = backup_dir / f"{safe_name}.absent"
        marker.write_text("absent\n", encoding="utf-8")
        meta["backup_file"] = str(marker)
        meta["absent"] = True
    return meta


def restore_backup(ha_root: Path, meta: dict) -> None:
    relative = meta["path"]
    target = resolve_live_path(ha_root, relative)
    if meta.get("absent") or not meta.get("existed"):
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValueError("Cannot rollback non-regular file.")
            target.unlink()
        return
    data = Path(meta["backup_file"]).read_bytes()
    atomic_write_bytes(target, data)


def write_journal(payload: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_journal() -> None:
    if JOURNAL_PATH.exists():
        JOURNAL_PATH.unlink()


def ha_check_config(token: str) -> dict:
    request = urllib.request.Request(
        "http://supervisor/core/api/config/core/check_config",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def ha_call_service(ha_ws_call, domain: str, service: str, service_data: dict | None = None):
    payload = {"domain": domain, "service": service}
    if service_data:
        payload["service_data"] = service_data
    return ha_ws_call("call_service", **payload)


def activate_package(ha_ws_call, token: str) -> dict:
    """Validate YAML config then reload all reloadable domains. Never restart Core."""
    check = ha_check_config(token)
    if check.get("result") != "valid":
        return {
            "ok": False,
            "activation": "invalid_config",
            "check": check,
            "message": check.get("errors") or "Configuration check failed.",
        }
    try:
        ha_call_service(ha_ws_call, "homeassistant", "reload_all")
    except Exception as error:
        return {
            "ok": False,
            "activation": "reload_failed",
            "check": check,
            "message": str(error),
        }
    return {
        "ok": True,
        "activation": "reloaded",
        "check": check,
        "message": (
            "Configuration valid and reload_all completed. "
            "Some package platforms may still require a manual Core restart."
        ),
        "restart_required": False,
        "restart_hint": (
            "If expected entities are missing after reload, a manual Core restart may still be required."
        ),
    }


def activate_frontend_module(*, cache_bust: str | None = None) -> dict:
    if cache_bust == CACHE_BUST_CONTENT_HASH:
        message = "Frontend module written. No Core reload required."
    else:
        message = (
            "Frontend module written. No Core reload required; "
            "browsers may cache /local assets."
        )
    return {
        "ok": True,
        "activation": "static",
        "message": message,
        "restart_required": False,
    }


def list_lovelace_resources(ha_ws_call) -> list:
    result = ha_ws_call("lovelace/resources/list")
    if not isinstance(result, list):
        raise RuntimeError("Home Assistant returned an invalid Lovelace resource list.")
    return result


def find_matching_lovelace_resources(resources: list, resource_url: str) -> list[dict]:
    matches = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and resource_urls_match(url, resource_url):
            matches.append(item)
    return matches


def live_resource_type(item: dict) -> str | None:
    value = item.get("type")
    if not isinstance(value, str):
        value = item.get("res_type")
    return value if isinstance(value, str) else None


def resource_desired_fingerprint(spec: LovelaceResourceSpec) -> dict:
    return {"url": spec.url, "type": spec.type}


def resource_live_fingerprint(spec: LovelaceResourceSpec, matches: list[dict]):
    if not matches:
        return None
    if len(matches) > 1:
        return {"url": spec.url, "matches": len(matches)}
    current = matches[0]
    current_url = current.get("url")
    return {
        "url": resource_url_path(current_url) if isinstance(current_url, str) else spec.url,
        "type": live_resource_type(current),
    }


def classify_resource(spec: LovelaceResourceSpec, matches: list[dict]):
    if not matches:
        return RESOURCE_CREATE_STATUS, "bootstrap", True, RESOURCE_CREATE_REASON
    if len(matches) > 1:
        return "CONFLICT", "conflict", False, RESOURCE_AMBIGUOUS_REASON
    current_type = live_resource_type(matches[0])
    if current_type == spec.type:
        return RESOURCE_OK_STATUS, "same", False, RESOURCE_OK_REASON
    return "CONFLICT", "conflict", False, RESOURCE_TYPE_CONFLICT_REASON


def collect_resource_changes(
    policy: ManagedPolicy | None,
    ha_ws_call,
    entries: list[ManagedEntry] | tuple[ManagedEntry, ...] = (),
) -> list[dict]:
    """Read-only review of allowlisted Lovelace resources. Never mutates HA."""
    if policy is None:
        return []
    specs = resource_specs_for_entries(policy, entries)
    if not specs:
        return []
    live = list_lovelace_resources(ha_ws_call)
    changes = []
    for spec in specs:
        matches = find_matching_lovelace_resources(live, spec.url)
        status, css, selectable, reason = classify_resource(spec, matches)
        live_fp = resource_live_fingerprint(spec, matches)
        desired_fp = resource_desired_fingerprint(spec)
        diff = compare_json(live_fp, desired_fp)
        changes.append({
            "kind": "resource",
            "relative": spec.url,
            "name": spec.url,
            "url": spec.url,
            "resource_type": spec.type,
            "status": status,
            "css": css,
            "selectable": selectable,
            "reason": reason,
            "anchor": file_anchor("resource", spec.url),
            "diff": diff,
            "rows": hunk_rows(diff),
            "added": diff["added"],
            "removed": diff["removed"],
            "preview_ha_hash": digest(live_fp),
            "preview_desired_hash": digest(desired_fp),
            "current": live_fp,
            "github": desired_fp,
            "warnings": [],
        })
    return changes


def create_lovelace_resource(ha_ws_call, url: str, resource_type: str) -> dict:
    validate_resource_url(url)
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        raise ValueError(f"Unsupported Lovelace resource type: {resource_type}")
    result = ha_ws_call(
        "lovelace/resources/create",
        url=url,
        res_type=resource_type,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Resource create for {url} returned an invalid result.")
    return result


def delete_lovelace_resource(ha_ws_call, resource_id: str) -> None:
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError("Lovelace resource delete requires a resource_id.")
    ha_ws_call("lovelace/resources/delete", resource_id=resource_id)


def rollback_created_resources(ha_ws_call, created_ids: list[str]) -> None:
    for resource_id in reversed(created_ids):
        delete_lovelace_resource(ha_ws_call, resource_id)


def _created_resource_id(created: dict, matches: list[dict]) -> str | None:
    resource_id = created.get("id")
    if isinstance(resource_id, str) and resource_id:
        return resource_id
    if len(matches) == 1:
        fallback = matches[0].get("id")
        if isinstance(fallback, str) and fallback:
            return fallback
    return None


def apply_declared_resources(
    selected: list[str],
    previews_ha: dict,
    previews_desired: dict,
    policy: ManagedPolicy,
    ha_ws_call,
    *,
    entries: list[ManagedEntry] | tuple[ManagedEntry, ...] = (),
    workdir: Path | None = None,
):
    """Create missing allowlisted Lovelace resources. Idempotent by base URL."""
    results = []
    created_ids: list[str] = []
    specs = resource_specs_for_entries(policy, entries)
    spec_map = {spec.url: spec for spec in specs}
    entry_map = {
        entry.resource_url: entry
        for entry in entries
        if entry.profile == "frontend_module" and entry.resource_url
    }
    live = list_lovelace_resources(ha_ws_call)
    try:
        for url in selected:
            spec = spec_map.get(url)
            if spec is None:
                raise RuntimeError(f"{url}: not in declared resources.")
            matches = find_matching_lovelace_resources(live, url)
            status, _css, _selectable, _reason = classify_resource(spec, matches)
            live_fp = resource_live_fingerprint(spec, matches)
            desired_fp = resource_desired_fingerprint(spec)
            if (
                url not in previews_ha
                or url not in previews_desired
                or not matches_preview(live_fp, previews_ha[url])
                or not matches_preview(desired_fp, previews_desired[url])
            ):
                raise RuntimeError(
                    f"{url}: HA resource changed since preview. Refresh and review before Apply."
                )
            if status == RESOURCE_OK_STATUS:
                results.append({
                    "ok": True,
                    "message": f"{url}: already present.",
                })
                continue
            if status != RESOURCE_CREATE_STATUS:
                raise RuntimeError(
                    f"{url}: blocked by fresh conflict-check ({status})."
                )
            created = create_lovelace_resource(ha_ws_call, spec.url, spec.type)
            resource_id = created.get("id") if isinstance(created.get("id"), str) else None
            if resource_id:
                created_ids.append(resource_id)
            live = list_lovelace_resources(ha_ws_call)
            matches = find_matching_lovelace_resources(live, url)
            resource_id = _created_resource_id(created, matches)
            if not resource_id:
                raise RuntimeError(f"{url}: create did not return a resource_id.")
            if resource_id not in created_ids:
                created_ids.append(resource_id)
            if (
                len(matches) != 1
                or live_resource_type(matches[0]) != spec.type
                or not resource_urls_match(matches[0].get("url") or "", spec.url)
            ):
                raise RuntimeError(f"{url}: create returned, but read-back verification failed.")
            result = {
                "ok": True,
                "message": f"{url}: Resource created and verified.",
            }
            entry = entry_map.get(url)
            if entry is not None and workdir is not None:
                module_path = workdir / entry.path
                if module_path.is_symlink() or not module_path.is_file():
                    raise RuntimeError(f"{url}: managed frontend module is missing.")
                bust = apply_frontend_cache_bust(
                    ha_ws_call, entry, file_digest(module_path.read_bytes())
                )
                _merge_cache_bust_result(result, bust)
            results.append(result)
    except Exception as error:
        try:
            rollback_created_resources(ha_ws_call, created_ids)
        except Exception as rollback_error:
            results.append({
                "ok": False,
                "message": (
                    f"Resource Apply failed ({error}); rollback also failed "
                    f"({rollback_error})."
                ),
            })
            return results, []
        created_ids = []
        results.append({
            "ok": False,
            "message": f"Resource Apply failed and rolled back: {error}",
        })
    return results, created_ids


def apply_frontend_cache_bust(ha_ws_call, entry: ManagedEntry, content_sha256: str) -> dict:
    """Update the matching Lovelace resource URL after a verified frontend deploy.

    Never creates missing resources. Failures are warnings; they must not roll
    back the already-verified file write. lovelace.reload_resources is YAML-mode
    only and is not called here.
    """
    if entry.profile != "frontend_module" or entry.cache_bust != CACHE_BUST_CONTENT_HASH:
        return {
            "status": "skipped",
            "ok": True,
            "reason": "not configured",
            "message": "",
        }
    if not entry.resource_url:
        return {
            "status": "error",
            "ok": False,
            "message": (
                "WARNING: frontend module deployed successfully, "
                "but cache_bust is configured without a resource_url."
            ),
        }
    desired = cache_busted_resource_url(entry.resource_url, content_sha256)
    try:
        resources = list_lovelace_resources(ha_ws_call)
    except Exception as error:
        return {
            "status": "error",
            "ok": False,
            "url": desired,
            "message": (
                "WARNING: frontend module deployed successfully, "
                "but the Lovelace resource cache-buster was not updated "
                f"({error})."
            ),
        }
    matches = find_matching_lovelace_resources(resources, entry.resource_url)
    if not matches:
        return {
            "status": "missing_resource",
            "ok": False,
            "url": desired,
            "message": (
                "WARNING: frontend module deployed successfully, "
                "but matching Lovelace resource was not found."
            ),
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "ok": False,
            "url": desired,
            "message": (
                "WARNING: frontend module deployed successfully, "
                f"but {len(matches)} Lovelace resources match {entry.resource_url}; "
                "refusing to guess resource_id."
            ),
        }
    resource = matches[0]
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        return {
            "status": "error",
            "ok": False,
            "url": desired,
            "message": (
                "WARNING: frontend module deployed successfully, "
                "but the matching Lovelace resource has no id."
            ),
        }
    current_url = resource.get("url") if isinstance(resource.get("url"), str) else ""
    if current_url == desired:
        return {
            "status": "skipped",
            "ok": True,
            "reason": "content hash unchanged",
            "resource_id": resource_id,
            "url": desired,
            "message": "resource update: skipped; reason: content hash unchanged",
        }
    try:
        ha_ws_call("lovelace/resources/update", resource_id=resource_id, url=desired)
    except Exception as error:
        return {
            "status": "error",
            "ok": False,
            "resource_id": resource_id,
            "url": desired,
            "message": (
                "WARNING: frontend module deployed successfully, "
                "but the Lovelace resource cache-buster was not updated "
                f"({error})."
            ),
        }
    return {
        "status": "updated",
        "ok": True,
        "resource_id": resource_id,
        "url": desired,
        "previous_url": current_url,
        "message": (
            f"Lovelace resource URL updated to {desired}. "
            "An already open dashboard may still require a normal page refresh."
        ),
    }


def _merge_cache_bust_result(result: dict, bust: dict) -> None:
    extra = bust.get("message") or ""
    if extra:
        result["message"] = f"{result['message']} {extra}".strip()
    result["cache_bust"] = {
        key: bust[key]
        for key in ("status", "reason", "url", "resource_id")
        if bust.get(key) not in (None, "")
    }
    if not bust.get("ok", True):
        result["ok"] = False


def activate_custom_template(ha_ws_call) -> dict:
    try:
        ha_call_service(ha_ws_call, "homeassistant", "reload_custom_templates")
    except Exception as error:
        return {"ok": False, "activation": "reload_failed", "message": str(error)}
    return {
        "ok": True,
        "activation": "reloaded",
        "message": "Custom templates reloaded.",
        "restart_required": False,
    }


def activate(profile: str, ha_ws_call, token: str, *, cache_bust: str | None = None) -> dict:
    if profile == "package":
        return activate_package(ha_ws_call, token)
    if profile == "frontend_module":
        return activate_frontend_module(cache_bust=cache_bust)
    if profile == "custom_template":
        return activate_custom_template(ha_ws_call)
    return {"ok": False, "activation": "unknown_profile", "message": profile}


def collect_managed_changes(
    workdir: Path,
    ha_root: Path,
    entries: list[ManagedEntry],
    unsafe_reason,
    *,
    stage_frontend: bool = True,
    commit_sha: str = "",
):
    bases = load_bases()
    changes = []
    known_paths = {entry.path for entry in entries}
    for entry in entries:
        github_path = workdir / entry.path
        if github_path.is_symlink() or not github_path.is_file():
            changes.append(_managed_review(
                entry,
                status="ERROR",
                css="error",
                selectable=False,
                reason="File missing from Git source.",
                diff=empty_diff(),
                warnings=[],
                preview_ha_hash="",
                preview_desired_hash="",
                github_hash=None,
                live_hash=None,
                base=base_hash_for(bases, entry.path),
                staging=None,
                source_sha=commit_sha,
            ))
            continue
        github_data = github_path.read_bytes()
        github_hash = file_digest(github_data)
        live_path = resolve_live_path(ha_root, entry.path)
        live = read_snapshot(live_path)
        try:
            github_text = decode_text(github_data)
            live_text = decode_text(live.data) if live.data is not None else ""
        except UnicodeDecodeError:
            changes.append(_managed_review(
                entry,
                status="ERROR",
                css="error",
                selectable=False,
                reason="File is not valid UTF-8.",
                diff=empty_diff(),
                warnings=[],
                preview_ha_hash=live.sha256 or "",
                preview_desired_hash=github_hash,
                github_hash=github_hash,
                live_hash=live.sha256,
                base=base_hash_for(bases, entry.path),
                staging=None,
                source_sha=commit_sha,
            ))
            continue
        if entry.profile == "package":
            try:
                parsed = yaml.safe_load(github_text)
            except yaml.YAMLError:
                scan_value = None
                parse_error = "invalid YAML"
            else:
                scan_value = parsed
                parse_error = None
        else:
            scan_value = github_text
            parse_error = None
        warnings = format_scan_warnings(scan_value, github_text) if scan_value is not None else []
        if entry.profile == "frontend_module":
            for hint in relative_import_hints(entry.path, github_text, known_paths):
                warnings.append({"reason": hint, "field": None, "path": entry.path, "line": None})
        status, css, selectable, reason = classify_file(
            github_hash,
            live.sha256,
            base_hash_for(bases, entry.path),
        )
        if parse_error:
            status, css, selectable, reason = "ERROR", "error", False, parse_error
        diff, _added, _removed = side_by_side_text(live_text, github_text)
        staging = None
        if stage_frontend and entry.profile == "frontend_module" and status in (
            MANAGED_APPLYABLE_STATUSES | {"SAME", MISSING_BASE_STATUS}
        ):
            # Stage desired Git bytes for optional manual/Visual verification without touching canonical.
            try:
                if not commit_sha:
                    raise ValueError("Frontend staging requires an immutable source commit SHA.")
                staging = ensure_frontend_staging(
                    ha_root, entry.path, github_data, github_hash, commit_sha
                )
            except Exception as error:
                staging = {"error": str(error)}
        changes.append(_managed_review(
            entry,
            status=status,
            css=css,
            selectable=selectable,
            reason=reason,
            diff=diff,
            warnings=warnings,
            preview_ha_hash=live.sha256 or ("0" * 64),
            preview_desired_hash=github_hash,
            github_hash=github_hash,
            live_hash=live.sha256,
            base=base_hash_for(bases, entry.path),
            github_data=github_data,
            live_exists=live.exists,
            staging=staging,
            absent_live_token="absent" if not live.exists else None,
            source_sha=commit_sha,
        ))
    return changes, bases


def apply_managed_files(
    selected: list[str],
    previews_ha: dict,
    previews_desired: dict,
    workdir: Path,
    ha_root: Path,
    entries: list[ManagedEntry],
    unsafe_reason,
    ha_ws_call,
    token: str,
    *,
    source_ref: str | None = None,
    commit_sha: str | None = None,
    defer_cache_bust_urls: frozenset[str] = frozenset(),
):
    """Apply selected managed files with backup, atomic write, validate, activate, rollback."""
    entry_map = {entry.path: entry for entry in entries}
    results = []
    applied = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with APPLY_LOCK:
        fresh_changes, bases = collect_managed_changes(
            workdir,
            ha_root,
            entries,
            unsafe_reason,
            stage_frontend=False,
            commit_sha=commit_sha or "",
        )
        fresh = {change["relative"]: change for change in fresh_changes}
        journal = {
            "id": stamp,
            "phase": "planning",
            "selected": selected,
            "written": [],
            "backups": [],
        }
        write_journal(journal)

        planned = []
        for relative in selected:
            entry = entry_map.get(relative)
            change = fresh.get(relative)
            if not entry or not change:
                results.append({"ok": False, "message": f"{relative}: not in managed policy or missing."})
                continue
            if change["status"] not in MANAGED_APPLYABLE_STATUSES:
                results.append({
                    "ok": False,
                    "message": f"{relative}: blocked by fresh conflict-check ({change['status']}).",
                })
                continue
            expected_ha = previews_ha[relative]
            expected_git = previews_desired[relative]
            live_hash = change["live_hash"]
            # Absent LIVE is represented in the form as 64 zeros when missing.
            form_ha = expected_ha
            actual_ha_token = live_hash or ("0" * 64)
            if actual_ha_token != form_ha or change["github_hash"] != expected_git:
                results.append({
                    "ok": False,
                    "message": (
                        f"{relative}: HA or Git desired changed since preview. "
                        "Refresh and review before Apply."
                    ),
                })
                continue
            # Fresh LIVE hash immediately before write.
            live_path = resolve_live_path(ha_root, relative)
            live_now = read_snapshot(live_path)
            now_token = live_now.sha256 or ("0" * 64)
            if now_token != form_ha:
                results.append({
                    "ok": False,
                    "message": f"{relative}: HA changed before write. Review again.",
                })
                continue
            planned.append((entry, change, live_now, live_path))

        if not planned:
            clear_journal()
            return results, applied

        journal["phase"] = "writing"
        write_journal(journal)
        written_metas = []
        try:
            for entry, change, live_now, live_path in planned:
                backup_meta = backup_live(entry.path, live_now, stamp)
                journal["backups"].append(backup_meta)
                write_journal(journal)
                atomic_write_bytes(live_path, change["github_data"])
                verified = read_snapshot(live_path)
                if verified.sha256 != change["github_hash"]:
                    raise RuntimeError(f"{entry.path}: read-back verification failed.")
                written_metas.append({
                    "entry": entry,
                    "change": change,
                    "backup": backup_meta,
                    "live_path": live_path,
                })
                journal["written"].append(entry.path)
                write_journal(journal)

            journal["phase"] = "activating"
            write_journal(journal)

            # Activate by profile groups: packages need one check/reload for the batch.
            package_written = [item for item in written_metas if item["entry"].profile == "package"]
            other_written = [item for item in written_metas if item["entry"].profile != "package"]

            result_by_path: dict[str, dict] = {}
            if package_written:
                activation = activate("package", ha_ws_call, token)
                if not activation.get("ok"):
                    raise RuntimeError(
                        "Package activation failed: " + str(activation.get("message"))
                    )
                for item in package_written:
                    set_base_hash(
                        bases,
                        item["entry"].path,
                        item["change"]["github_hash"],
                        source_ref=source_ref,
                        commit_sha=commit_sha,
                    )
                    applied.append(item["entry"].path)
                    result = {
                        "ok": True,
                        "message": (
                            f"{item['entry'].path}: Applied, verified, config valid, reloaded. "
                            f"{activation.get('restart_hint', '')}"
                        ).strip(),
                    }
                    results.append(result)
                    result_by_path[item["entry"].path] = result

            for item in other_written:
                # reload_all already reloads custom Jinja templates; skip a second call.
                if item["entry"].profile == "custom_template" and package_written:
                    activation = {
                        "ok": True,
                        "activation": "reloaded",
                        "message": "Custom templates already reloaded by reload_all.",
                        "restart_required": False,
                    }
                else:
                    activation = activate(
                        item["entry"].profile,
                        ha_ws_call,
                        token,
                        cache_bust=item["entry"].cache_bust,
                    )
                if not activation.get("ok"):
                    raise RuntimeError(
                        f"{item['entry'].path}: activation failed: {activation.get('message')}"
                    )
                set_base_hash(
                    bases,
                    item["entry"].path,
                    item["change"]["github_hash"],
                    source_ref=source_ref,
                    commit_sha=commit_sha,
                )
                applied.append(item["entry"].path)
                result = {
                    "ok": True,
                    "message": f"{item['entry'].path}: Applied and verified. {activation.get('message', '')}",
                }
                results.append(result)
                result_by_path[item["entry"].path] = result

            # Cache-bust after every file is written, verified and activated.
            # Failures are reported; they must not roll back the deployed files.
            for item in written_metas:
                entry = item["entry"]
                if entry.profile != "frontend_module" or entry.cache_bust != CACHE_BUST_CONTENT_HASH:
                    continue
                if entry.resource_url in defer_cache_bust_urls:
                    continue
                bust = apply_frontend_cache_bust(
                    ha_ws_call, entry, item["change"]["github_hash"]
                )
                result = result_by_path.get(entry.path)
                if result is not None:
                    _merge_cache_bust_result(result, bust)

            save_bases(bases)
            journal["phase"] = "done"
            write_journal(journal)
            clear_journal()
        except Exception as error:
            journal["phase"] = "rolling_back"
            journal["error"] = str(error)
            write_journal(journal)
            rolled_package = False
            for item in reversed(written_metas):
                try:
                    restore_backup(ha_root, item["backup"])
                    if item["entry"].profile == "package":
                        rolled_package = True
                except Exception as rollback_error:
                    results.append({
                        "ok": False,
                        "message": (
                            f"{item['entry'].path}: rollback failed after error "
                            f"({error}): {rollback_error}"
                        ),
                    })
            if rolled_package:
                try:
                    # Disk restored; ask HA to drop in-memory package config when possible.
                    activate("package", ha_ws_call, token)
                except Exception:
                    results.append({
                        "ok": False,
                        "message": (
                            "Files were restored on disk, but post-rollback reload failed. "
                            "A manual configuration check / reload may be required."
                        ),
                    })
            results.append({"ok": False, "message": f"Managed Apply failed and rolled back: {error}"})
            journal["phase"] = "failed"
            write_journal(journal)
            applied = []
    return results, applied


def initialize_missing_bases(workdir: Path, ha_root: Path, entries: list[ManagedEntry], unsafe_reason):
    changes, bases = collect_managed_changes(
        workdir, ha_root, entries, unsafe_reason, stage_frontend=False
    )
    initialized = []
    for change in changes:
        if change["status"] != MISSING_BASE_STATUS:
            continue
        if not change.get("github_hash"):
            continue
        set_base_hash(bases, change["relative"], change["github_hash"])
        initialized.append(change["relative"])
    if initialized:
        save_bases(bases)
    return initialized
