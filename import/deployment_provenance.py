"""Per-artifact LIVE deployment provenance for Import and Export.

Import writes this after a verified Apply. Export reads it from the Home
Assistant config mount and never from the Git data repository. A Git tree
cannot declare itself canonical.

CANONICAL_BRANCH must stay aligned with Import's default source ref and
Export's Git branch (`main`).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
SCHEMA_VERSIONS = frozenset({1, 2})
CANONICAL_BRANCH = "main"
SOURCE_KIND_BRANCH = "branch"
SOURCE_KIND_COMMIT = "commit"
GUARD_LEGACY = "legacy"
GUARD_READY = "ready"
GUARD_UNAVAILABLE = "unavailable"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")
DASHBOARD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.json$")
MANAGED_PATH_RE = re.compile(
    r"^(?:packages|www|custom_templates)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)

# Import-local copy survives Import restarts even if the shared file is later cleaned.
IMPORT_STATE_PATH = Path("/data/dashboard-provenance.json")
# Shared with Export: internal HA config state, never a frontend /local asset.
SHARED_STATE_PATH = Path("/homeassistant/.config-sync/live-dashboards.json")
SHARED_GUARD_MARKER_NAME = "guard-initialized.json"
SHARED_GUARD_MARKER_PATH = Path("/homeassistant/.config-sync/guard-initialized.json")
# Export caches the last valid shared snapshot plus a guard-initialized marker.
# Cache may only restrict dashboard writes; it must never authorize them.
EXPORT_CACHE_PATH = Path("/data/live-dashboards.json")
EXPORT_GUARD_MARKER_NAME = "provenance-guard-initialized.json"
EXPORT_GUARD_MARKER_PATH = Path("/data") / EXPORT_GUARD_MARKER_NAME


class InvalidProvenance(ValueError):
    """On-disk provenance exists but is not a trusted, well-formed store."""


def _validate_branch_name(name: str) -> str:
    if not isinstance(name, str) or not name or not BRANCH_NAME_RE.fullmatch(name):
        raise InvalidProvenance("Invalid source_ref.")
    if name.startswith("-") or ".." in name or name.startswith("refs/"):
        raise InvalidProvenance("Invalid source_ref.")
    return name


def _validate_commit_sha(value: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_RE.fullmatch(value):
        raise InvalidProvenance("Invalid commit_sha.")
    return value


@dataclass(frozen=True)
class ArtifactProvenance:
    source_ref: str
    source_kind: str
    commit_sha: str
    content_hash: str
    canonical: bool
    applied_at: str

    def as_dict(self) -> dict:
        return {
            "applied_at": self.applied_at,
            "canonical": self.canonical,
            "commit_sha": self.commit_sha,
            "content_hash": self.content_hash,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
        }

    def short_sha(self) -> str:
        return self.commit_sha[:7] if len(self.commit_sha) >= 7 else self.commit_sha


@dataclass(frozen=True)
class ProvenanceStore:
    canonical_branch: str = CANONICAL_BRANCH
    updated_at: str = ""
    dashboards: dict | None = None
    managed_files: dict | None = None
    invalid: bool = False
    guard_initialized: bool = False
    guard_state: str = GUARD_LEGACY
    cache_restrictions: dict | None = None

    def __post_init__(self):
        if self.dashboards is None:
            object.__setattr__(self, "dashboards", {})
        if self.managed_files is None:
            object.__setattr__(self, "managed_files", {})
        if self.cache_restrictions is None:
            object.__setattr__(self, "cache_restrictions", {})

    def as_dict(self) -> dict:
        return {
            "canonical_branch": self.canonical_branch,
            "dashboards": {
                name: entry.as_dict() for name, entry in sorted(self.dashboards.items())
            },
            "guard_initialized": True,
            "managed_files": {
                name: entry.as_dict() for name, entry in sorted(self.managed_files.items())
            },
            "schema_version": SCHEMA_VERSION,
            "updated_at": self.updated_at,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_canonical_source(
    source_ref: str,
    source_kind: str,
    canonical_branch: str = CANONICAL_BRANCH,
) -> bool:
    return source_kind == SOURCE_KIND_BRANCH and source_ref == canonical_branch


def is_canonical_live(
    entry: ArtifactProvenance | None,
    canonical_branch: str = CANONICAL_BRANCH,
) -> bool:
    if entry is None:
        return True
    if not is_canonical_source(entry.source_ref, entry.source_kind, canonical_branch):
        return False
    return bool(entry.canonical)


def skip_reason(relative: str, entry: ArtifactProvenance) -> str:
    return (
        f"SKIP {relative}: LIVE is deployed from {entry.source_ref} "
        f"@ {entry.short_sha()}. Canonical main dashboard sync disabled."
    )


def unavailable_reason(relative: str, *, unreadable: bool = False) -> str:
    detail = "unreadable" if unreadable else "unavailable"
    return (
        f"SKIP {relative}: LIVE provenance is {detail}. "
        "Canonical main dashboard sync disabled."
    )


def _validate_artifact(raw: object) -> ArtifactProvenance:
    if not isinstance(raw, dict):
        raise InvalidProvenance("Artifact provenance must be an object.")
    source_kind = raw.get("source_kind")
    source_ref = raw.get("source_ref")
    commit_sha = raw.get("commit_sha")
    content_hash = raw.get("content_hash")
    applied_at = raw.get("applied_at")
    claimed_canonical = raw.get("canonical")
    if source_kind not in {SOURCE_KIND_BRANCH, SOURCE_KIND_COMMIT}:
        raise InvalidProvenance("Invalid source_kind.")
    if not isinstance(source_ref, str) or not source_ref:
        raise InvalidProvenance("Invalid source_ref.")
    if source_kind == SOURCE_KIND_BRANCH:
        source_ref = _validate_branch_name(source_ref)
    else:
        source_ref = _validate_commit_sha(source_ref)
    commit_sha = _validate_commit_sha(commit_sha if isinstance(commit_sha, str) else "")
    if not isinstance(content_hash, str) or not HASH_RE.fullmatch(content_hash):
        raise InvalidProvenance("Invalid content_hash.")
    if not isinstance(applied_at, str) or not applied_at:
        raise InvalidProvenance("Invalid applied_at.")
    if not isinstance(claimed_canonical, bool):
        raise InvalidProvenance("canonical must be a boolean.")
    canonical = claimed_canonical and is_canonical_source(source_ref, source_kind)
    return ArtifactProvenance(
        source_ref=source_ref,
        source_kind=source_kind,
        commit_sha=commit_sha,
        content_hash=content_hash,
        canonical=canonical,
        applied_at=applied_at,
    )


def _validate_key(name: str, *, dashboard: bool) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ".." in name:
        raise InvalidProvenance("Invalid artifact key.")
    if dashboard:
        if "/" in name or not DASHBOARD_NAME_RE.fullmatch(name) or name == "index.json":
            raise InvalidProvenance("Invalid dashboard provenance key.")
    elif not MANAGED_PATH_RE.fullmatch(name):
        raise InvalidProvenance("Invalid managed-file provenance key.")
    return name


def parse_store(raw: object) -> ProvenanceStore:
    if not isinstance(raw, dict):
        raise InvalidProvenance("Provenance root must be a JSON object.")
    version = raw.get("schema_version")
    if version not in SCHEMA_VERSIONS:
        raise InvalidProvenance("Unsupported provenance schema.")
    branch = raw.get("canonical_branch", CANONICAL_BRANCH)
    if not isinstance(branch, str) or branch != CANONICAL_BRANCH:
        raise InvalidProvenance("canonical_branch does not match configured canonical branch.")
    updated_at = raw.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        raise InvalidProvenance("Invalid updated_at.")
    dashboards_raw = raw.get("dashboards") or {}
    managed_raw = raw.get("managed_files") or {}
    if not isinstance(dashboards_raw, dict) or not isinstance(managed_raw, dict):
        raise InvalidProvenance("Artifact maps must be objects.")
    dashboards = {
        _validate_key(name, dashboard=True): _validate_artifact(entry)
        for name, entry in dashboards_raw.items()
    }
    managed = {
        _validate_key(name, dashboard=False): _validate_artifact(entry)
        for name, entry in managed_raw.items()
    }
    return ProvenanceStore(
        canonical_branch=branch,
        updated_at=updated_at or "",
        dashboards=dashboards,
        managed_files=managed,
        invalid=False,
        guard_initialized=True,
        guard_state=GUARD_READY,
    )


def empty_store() -> ProvenanceStore:
    return ProvenanceStore(
        canonical_branch=CANONICAL_BRANCH,
        updated_at="",
        invalid=False,
        guard_initialized=False,
        guard_state=GUARD_LEGACY,
    )


def unavailable_store(
    *,
    dashboards: dict | None = None,
    unreadable: bool = False,
    reason: str = "",
) -> ProvenanceStore:
    detail = reason or (
        "LIVE provenance is unreadable." if unreadable else "LIVE provenance is unavailable."
    )
    return ProvenanceStore(
        canonical_branch=CANONICAL_BRANCH,
        updated_at=detail,
        dashboards=dashboards or {},
        invalid=unreadable,
        guard_initialized=True,
        guard_state=GUARD_UNAVAILABLE,
    )


def _resolved_regular_file(path: Path, required_parent: Path | None = None) -> Path:
    if path.exists() and path.is_symlink():
        raise InvalidProvenance("Provenance path must not be a symlink.")
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_file():
        raise InvalidProvenance("Provenance path must be a regular file.")
    resolved = path.resolve()
    if required_parent is not None:
        parent = required_parent.resolve()
        if parent != resolved and parent not in resolved.parents:
            raise InvalidProvenance("Provenance path escaped its allowed directory.")
    return resolved


def load_json_store(path: Path, *, required_parent: Path | None = None) -> ProvenanceStore:
    target = _resolved_regular_file(path, required_parent=required_parent)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidProvenance("Provenance file could not be parsed.") from error
    return parse_store(raw)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def shared_marker_path(shared_path: Path) -> Path:
    return shared_path.parent / SHARED_GUARD_MARKER_NAME


def export_marker_path(cache_path: Path) -> Path:
    return cache_path.parent / EXPORT_GUARD_MARKER_NAME


def marker_payload() -> dict:
    return {
        "guard_initialized": True,
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
    }


def write_guard_marker(path: Path) -> None:
    atomic_write_json(path, marker_payload())


def marker_present(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return False


def arm_fail_closed_guard(*, shared_path: Path | None = None) -> None:
    """Mark the guard initialized even when the provenance JSON cannot be written.

    Export then fail-closes dashboard desired-state sync instead of treating the
    installation as legacy canonical-main.
    """
    target = shared_path if shared_path is not None else SHARED_STATE_PATH
    write_guard_marker(shared_marker_path(target))


def _clone_store(
    store: ProvenanceStore,
    *,
    dashboards: dict | None = None,
    managed_files: dict | None = None,
    updated_at: str | None = None,
) -> ProvenanceStore:
    return ProvenanceStore(
        canonical_branch=store.canonical_branch or CANONICAL_BRANCH,
        updated_at=updated_at if updated_at is not None else store.updated_at,
        dashboards=dict(store.dashboards if dashboards is None else dashboards),
        managed_files=dict(store.managed_files if managed_files is None else managed_files),
        invalid=False,
        guard_initialized=True,
        guard_state=GUARD_READY,
    )


def save_store(
    store: ProvenanceStore,
    *,
    local_path: Path | None = None,
    shared_path: Path | None = None,
) -> ProvenanceStore:
    stamped = _clone_store(store, updated_at=utc_now())
    payload = stamped.as_dict()
    if shared_path is not None:
        atomic_write_json(shared_path, payload)
        write_guard_marker(shared_marker_path(shared_path))
        verified = load_json_store(shared_path, required_parent=shared_path.parent)
        if not verified.guard_initialized or verified.guard_state != GUARD_READY:
            raise OSError("Shared provenance read-back did not initialize the export guard.")
    if local_path is not None:
        atomic_write_json(local_path, payload)
    if local_path is None and shared_path is None:
        raise OSError("Could not persist dashboard provenance: no path configured")
    return stamped


def load_import_store(
    *,
    local_path: Path | None = None,
    shared_path: Path | None = None,
) -> ProvenanceStore:
    local = local_path if local_path is not None else IMPORT_STATE_PATH
    shared = shared_path if shared_path is not None else SHARED_STATE_PATH
    store = None
    if local.exists():
        try:
            store = load_json_store(local)
        except (FileNotFoundError, InvalidProvenance):
            store = None
    if store is None and shared.exists():
        try:
            store = load_json_store(shared, required_parent=shared.parent)
        except (FileNotFoundError, InvalidProvenance):
            store = None
    if store is None:
        return empty_store()
    if not shared.exists():
        try:
            save_store(store, local_path=None, shared_path=shared)
        except OSError:
            pass
    return store


def _try_load_export_file(
    path: Path, *, required_parent: Path | None = None
) -> tuple[str, ProvenanceStore | None]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return "invalid", unavailable_store(unreadable=True)
    if not path.exists():
        return "missing", None
    try:
        return "ok", load_json_store(path, required_parent=required_parent)
    except FileNotFoundError:
        return "missing", None
    except InvalidProvenance as error:
        return "invalid", unavailable_store(unreadable=True, reason=str(error))


def _cache_restrictions(cache: ProvenanceStore | None) -> dict:
    if cache is None:
        return {}
    return {
        name: entry
        for name, entry in cache.dashboards.items()
        if not is_canonical_live(entry, cache.canonical_branch)
    }


def _with_cache_restrictions(store: ProvenanceStore, cache: ProvenanceStore | None) -> ProvenanceStore:
    restrictions = _cache_restrictions(cache)
    if not restrictions:
        return store
    return ProvenanceStore(
        canonical_branch=store.canonical_branch,
        updated_at=store.updated_at,
        dashboards=dict(store.dashboards),
        managed_files=dict(store.managed_files),
        invalid=store.invalid,
        guard_initialized=store.guard_initialized,
        guard_state=store.guard_state,
        cache_restrictions=restrictions,
    )


def load_export_store(
    *,
    shared_path: Path | None = None,
    cache_path: Path | None = None,
) -> ProvenanceStore:
    shared = shared_path if shared_path is not None else SHARED_STATE_PATH
    cache = cache_path if cache_path is not None else EXPORT_CACHE_PATH
    shared_status, shared_store = _try_load_export_file(
        shared, required_parent=shared.parent
    )
    cache_status, cache_store = _try_load_export_file(cache)
    initialized = (
        marker_present(shared_marker_path(shared))
        or marker_present(export_marker_path(cache))
        or shared_status in {"ok", "invalid"}
        or cache_status in {"ok", "invalid"}
    )

    if shared_status == "ok" and shared_store is not None:
        try:
            atomic_write_json(cache, shared_store.as_dict())
            write_guard_marker(export_marker_path(cache))
        except OSError:
            pass
        return _with_cache_restrictions(shared_store, cache_store if cache_status == "ok" else None)

    if shared_status == "invalid" or initialized:
        cached_dashboards = dict(cache_store.dashboards) if cache_store is not None else {}
        return unavailable_store(
            dashboards=cached_dashboards,
            unreadable=shared_status == "invalid",
        )

    return empty_store()


def record_artifacts(
    store: ProvenanceStore,
    *,
    source_ref: str,
    source_kind: str,
    commit_sha: str,
    dashboards: dict[str, str] | None = None,
    managed_files: dict[str, str] | None = None,
) -> ProvenanceStore:
    canonical = is_canonical_source(source_ref, source_kind, store.canonical_branch)
    applied_at = utc_now()
    dashboards_out = dict(store.dashboards)
    managed_out = dict(store.managed_files)
    commit_sha = _validate_commit_sha(commit_sha)
    if source_kind == SOURCE_KIND_BRANCH:
        source_ref = _validate_branch_name(source_ref)
    else:
        source_ref = _validate_commit_sha(source_ref)
    for name, content_hash in (dashboards or {}).items():
        _validate_key(name, dashboard=True)
        if not HASH_RE.fullmatch(content_hash):
            raise InvalidProvenance("Invalid applied content hash.")
        dashboards_out[name] = ArtifactProvenance(
            source_ref=source_ref,
            source_kind=source_kind,
            commit_sha=commit_sha,
            content_hash=content_hash,
            canonical=canonical,
            applied_at=applied_at,
        )
    for name, content_hash in (managed_files or {}).items():
        _validate_key(name, dashboard=False)
        if not HASH_RE.fullmatch(content_hash):
            raise InvalidProvenance("Invalid applied content hash.")
        managed_out[name] = ArtifactProvenance(
            source_ref=source_ref,
            source_kind=source_kind,
            commit_sha=commit_sha,
            content_hash=content_hash,
            canonical=canonical,
            applied_at=applied_at,
        )
    return _clone_store(
        store,
        dashboards=dashboards_out,
        managed_files=managed_out,
        updated_at=applied_at,
    )


def adopt_canonical(
    store: ProvenanceStore,
    *,
    relative: str,
    commit_sha: str,
    content_hash: str,
    kind: str = "dashboard",
) -> ProvenanceStore:
    if not HASH_RE.fullmatch(content_hash):
        raise InvalidProvenance("Invalid adopted content hash.")
    entry = ArtifactProvenance(
        source_ref=store.canonical_branch,
        source_kind=SOURCE_KIND_BRANCH,
        commit_sha=_validate_commit_sha(commit_sha),
        content_hash=content_hash,
        canonical=True,
        applied_at=utc_now(),
    )
    dashboards = dict(store.dashboards)
    managed = dict(store.managed_files)
    if kind == "dashboard":
        _validate_key(relative, dashboard=True)
        dashboards[relative] = entry
    else:
        _validate_key(relative, dashboard=False)
        managed[relative] = entry
    return _clone_store(
        store,
        dashboards=dashboards,
        managed_files=managed,
        updated_at=entry.applied_at,
    )


def dashboard_sync_decision(relative: str, store: ProvenanceStore) -> tuple[bool, str | None]:
    """Return (allowed, skip_log).

    Cache/non-canonical records may only restrict writes. A stale cached
    CANONICAL MAIN record never authorizes a write to main. After the guard
    has been initialized, missing or corrupt shared provenance fail-closes.
    True legacy (guard never initialized) keeps the previous canonical-main
    Export behaviour.
    """
    if store.guard_state == GUARD_UNAVAILABLE or store.invalid:
        entry = store.dashboards.get(relative)
        if entry is not None and not is_canonical_live(entry, store.canonical_branch):
            return False, skip_reason(relative, entry)
        return False, unavailable_reason(relative, unreadable=store.invalid)

    restricted = store.cache_restrictions.get(relative)
    if restricted is not None and not is_canonical_live(restricted, store.canonical_branch):
        return False, skip_reason(relative, restricted)

    entry = store.dashboards.get(relative)
    if is_canonical_live(entry, store.canonical_branch):
        return True, None
    return False, skip_reason(relative, entry)


def provenance_label(entry: ArtifactProvenance | None, canonical_branch: str = CANONICAL_BRANCH) -> str:
    if entry is None or is_canonical_live(entry, canonical_branch):
        return "CANONICAL MAIN"
    return "NON-CANONICAL"
