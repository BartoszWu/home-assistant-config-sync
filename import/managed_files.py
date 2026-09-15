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
)

POLICY_PATH = Path(__file__).with_name("managed_files.yaml")
BASES_PATH = Path("/data/managed-file-bases.json")
BACKUP_ROOT = Path("/data/managed-file-backups")
JOURNAL_PATH = Path("/data/managed-file-journal.json")
PREVIEW_DIRNAME = ".config-sync-preview"

# First Apply when BASE is missing and LIVE already differs from Git.
# Requires explicit checkbox selection; Initialize bases never adopts this drift.
MANAGED_BOOTSTRAP_STATUS = "READY TO APPLY — NO BASE"
MANAGED_APPLYABLE_STATUSES = frozenset(APPLYABLE_STATUSES | {MANAGED_BOOTSTRAP_STATUS})

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
})
FORBIDDEN_SUFFIXES = (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3")

RELATIVE_PATH_RE = re.compile(
    r"^(?:packages|www|custom_templates)/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$"
)


@dataclass(frozen=True)
class ManagedEntry:
    path: str
    profile: str


@dataclass
class FileSnapshot:
    exists: bool
    data: bytes | None
    sha256: str | None


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_policy(path: Path | None = None) -> tuple[Path, list[ManagedEntry]]:
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
    seen: set[str] = set()
    for item in entries_raw:
        if not isinstance(item, dict):
            raise RuntimeError("Each managed_files entry must be a mapping.")
        relative = item.get("path")
        profile = item.get("profile")
        if not isinstance(relative, str) or not isinstance(profile, str):
            raise RuntimeError("managed_files entries require string path and profile.")
        if profile not in PROFILES:
            raise RuntimeError(f"Unknown managed file profile: {profile}")
        validate_policy_path(relative)
        if relative in seen:
            raise RuntimeError(f"Duplicate managed file path: {relative}")
        seen.add(relative)
        entries.append(ManagedEntry(path=relative, profile=profile))
    return Path(root), entries


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
    if unsafe:
        return "UNSAFE", "unsafe", False, unsafe
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
        return "SAME", "same", False, "GitHub HEAD matches HA current."
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


def set_base_hash(bases: dict, relative: str, sha256: str) -> None:
    bases.setdefault("files", {})[relative] = {
        "sha256": sha256,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def side_by_side_text(left: str, right: str):
    import difflib

    left_lines = left.splitlines()
    right_lines = right.splitlines()
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines)
    rows = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_part = left_lines[i1:i2]
        right_part = right_lines[j1:j2]
        width = max(len(left_part), len(right_part))
        if tag in {"replace", "delete"}:
            removed += len(left_part)
        if tag in {"replace", "insert"}:
            added += len(right_part)
        for offset in range(width):
            has_left = offset < len(left_part)
            has_right = offset < len(right_part)
            rows.append({
                "left_no": i1 + offset + 1 if has_left else None,
                "left": left_part[offset] if has_left else "",
                "left_css": "left-del" if has_left and tag != "equal" else ("blank" if not has_left else ""),
                "right_no": j1 + offset + 1 if has_right else None,
                "right": right_part[offset] if has_right else "",
                "right_css": "right-add" if has_right and tag != "equal" else ("blank" if not has_right else ""),
            })
    return rows, added, removed


def decode_text(data: bytes) -> str:
    return data.decode("utf-8")


def staging_relative(content_hash: str, filename: str) -> str:
    short = content_hash[:12]
    return f"www/{PREVIEW_DIRNAME}/{short}/{filename}"


def staging_public_url(content_hash: str, filename: str) -> str:
    short = content_hash[:12]
    return f"/local/{PREVIEW_DIRNAME}/{short}/{filename}?v={content_hash}"


def ensure_frontend_staging(ha_root: Path, relative: str, data: bytes, content_hash: str) -> dict:
    """Write Git desired bytes under www/.config-sync-preview/ — never the canonical file."""
    validate_policy_path(relative)
    if not relative.startswith("www/"):
        raise ValueError("Staging is only for www frontend modules.")
    filename = PurePosixPath(relative).name
    staged_rel = staging_relative(content_hash, filename)
    # Bypass policy path regex for preview dirname by resolving under www only.
    root = ha_root.resolve(strict=False)
    preview_root = (root / "www" / PREVIEW_DIRNAME).resolve(strict=False)
    preview_root.relative_to(root / "www")
    target_dir = preview_root / content_hash[:12]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    if target.exists() and target.is_symlink():
        raise ValueError("Refusing symlink in preview staging.")
    # Atomic replace inside staging only.
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=".stage-", suffix=".tmp")
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
        "staged_relative": staged_rel,
        "preview_url": staging_public_url(content_hash, filename),
        "content_hash": content_hash,
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


def activate_frontend_module() -> dict:
    return {
        "ok": True,
        "activation": "static",
        "message": "Frontend module written. No Core reload required; browsers may cache /local assets.",
        "restart_required": False,
    }


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


def activate(profile: str, ha_ws_call, token: str) -> dict:
    if profile == "package":
        return activate_package(ha_ws_call, token)
    if profile == "frontend_module":
        return activate_frontend_module()
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
):
    bases = load_bases()
    changes = []
    for entry in entries:
        github_path = workdir / entry.path
        if not github_path.is_file():
            changes.append({
                "relative": entry.path,
                "profile": entry.profile,
                "status": "ERROR",
                "css": "error",
                "selectable": False,
                "reason": "File missing from GitHub HEAD.",
                "rows": [],
                "added": 0,
                "removed": 0,
                "preview_ha_hash": "",
                "preview_desired_hash": "",
                "github_hash": None,
                "live_hash": None,
                "base": base_hash_for(bases, entry.path),
                "staging": None,
            })
            continue
        github_data = github_path.read_bytes()
        github_hash = file_digest(github_data)
        live_path = resolve_live_path(ha_root, entry.path)
        live = read_snapshot(live_path)
        try:
            github_text = decode_text(github_data)
            live_text = decode_text(live.data) if live.data is not None else ""
        except UnicodeDecodeError:
            changes.append({
                "relative": entry.path,
                "profile": entry.profile,
                "status": "ERROR",
                "css": "error",
                "selectable": False,
                "reason": "File is not valid UTF-8.",
                "rows": [],
                "added": 0,
                "removed": 0,
                "preview_ha_hash": live.sha256 or "",
                "preview_desired_hash": github_hash,
                "github_hash": github_hash,
                "live_hash": live.sha256,
                "base": base_hash_for(bases, entry.path),
                "staging": None,
            })
            continue
        if entry.profile == "package":
            try:
                parsed = yaml.safe_load(github_text)
            except yaml.YAMLError:
                unsafe = "invalid YAML"
            else:
                unsafe = unsafe_reason(parsed)
        else:
            unsafe = unsafe_reason(github_text)
        status, css, selectable, reason = classify_file(
            github_hash,
            live.sha256,
            base_hash_for(bases, entry.path),
            unsafe=unsafe,
        )
        rows, added, removed = side_by_side_text(live_text, github_text)
        staging = None
        if stage_frontend and entry.profile == "frontend_module" and status in (
            MANAGED_APPLYABLE_STATUSES | {"SAME", MISSING_BASE_STATUS}
        ):
            # Stage desired Git bytes for optional manual/Visual verification without touching canonical.
            try:
                staging = ensure_frontend_staging(ha_root, entry.path, github_data, github_hash)
            except Exception as error:
                staging = {"error": str(error)}
        changes.append({
            "relative": entry.path,
            "profile": entry.profile,
            "status": status,
            "css": css,
            "selectable": selectable,
            "reason": reason,
            "rows": rows,
            "added": added,
            "removed": removed,
            "preview_ha_hash": live.sha256 or ("0" * 64),
            "preview_desired_hash": github_hash,
            "github_hash": github_hash,
            "live_hash": live.sha256,
            "base": base_hash_for(bases, entry.path),
            "github_data": github_data,
            "live_exists": live.exists,
            "staging": staging,
            "absent_live_token": "absent" if not live.exists else None,
        })
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
):
    """Apply selected managed files with backup, atomic write, validate, activate, rollback."""
    entry_map = {entry.path: entry for entry in entries}
    results = []
    applied = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with APPLY_LOCK:
        fresh_changes, bases = collect_managed_changes(
            workdir, ha_root, entries, unsafe_reason, stage_frontend=False
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
            if unsafe_reason(decode_text(change["github_data"])):
                results.append({"ok": False, "message": f"{relative}: blocked by security scan."})
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

            if package_written:
                activation = activate("package", ha_ws_call, token)
                if not activation.get("ok"):
                    raise RuntimeError(
                        "Package activation failed: " + str(activation.get("message"))
                    )
                for item in package_written:
                    set_base_hash(bases, item["entry"].path, item["change"]["github_hash"])
                    applied.append(item["entry"].path)
                    results.append({
                        "ok": True,
                        "message": (
                            f"{item['entry'].path}: Applied, verified, config valid, reloaded. "
                            f"{activation.get('restart_hint', '')}"
                        ).strip(),
                    })

            for item in other_written:
                activation = activate(item["entry"].profile, ha_ws_call, token)
                if not activation.get("ok"):
                    raise RuntimeError(
                        f"{item['entry'].path}: activation failed: {activation.get('message')}"
                    )
                set_base_hash(bases, item["entry"].path, item["change"]["github_hash"])
                applied.append(item["entry"].path)
                results.append({
                    "ok": True,
                    "message": f"{item['entry'].path}: Applied and verified. {activation.get('message', '')}",
                })

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
