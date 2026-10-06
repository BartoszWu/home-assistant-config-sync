import json
import os
import subprocess
import threading
from pathlib import Path, PurePosixPath

from flask import Flask, abort, render_template_string, request
from websocket import create_connection
from project_ui import register as register_projects
from review_ui import TEMPLATE, STYLE, SCRIPT, configuration_groups, review_summary
from project_deployment import Coordinator, DeploymentBusy, release_set
from project_profiles import PROJECTS
from dashboard_sync import DashboardSync
from managed_files import read_snapshot, resolve_live_path
from werkzeug.datastructures import MultiDict

from dashboard_logic import (
    APPLYABLE_STATUSES,
    CREATE_STATUS,
    MISSING_BASE_STATUS,
    classify_missing_ha,
    classify_with_provenance,
    dashboard_registration_payload,
    digest,
    is_ephemeral_dashboard,
    matches_preview,
    parse_preview_hashes,
    valid_dashboard_structure,
)
from diff_view import (
    compare_json,
    file_anchor,
    json_lines,
)
from git_source import (
    DEFAULT_SOURCE_REF,
    InvalidSourceRef,
    SourceRevision,
    checkout_source,
    parse_requested_source,
)
from deployment_provenance import (
    IMPORT_STATE_PATH,
    InvalidProvenance,
    SHARED_STATE_PATH,
    adopt_canonical,
    arm_fail_closed_guard,
    is_canonical_source,
    load_import_store,
    load_json_store,
    provenance_label,
    record_artifacts,
    save_store,
)
from managed_files import (
    apply_declared_resources,
    apply_managed_files,
    collect_managed_changes,
    collect_resource_changes,
    discover_managed_entries,
    initialize_missing_bases,
    load_policy,
    record_last_apply,
    resource_specs_for_entries,
    rollback_created_resources,
    validate_policy_path,
)
from review_warnings import format_scan_warnings


app = Flask(__name__)

REPO = "ssh://git@ssh.github.com:443/BartoszWu/home-assistant-config.git"
DEFAULT_BRANCH = DEFAULT_SOURCE_REF
KEY = Path("/review/ssh/github_ed25519")
KNOWN_HOSTS = Path("/review/ssh/known_hosts_443")
WORKDIR = Path("/tmp/home-assistant-config")
STATE_FILE = Path("state/dashboard-bases.json")
IMPORT_APPLIED_EVENT = "ha_config_sync_import_applied"
REPO_LOCK = threading.RLock()
COORDINATOR = Coordinator(Path("/data/projects"))
MAX_DASHBOARD_SELECTED = 20
MAX_MANAGED_SELECTED = 50
MAX_RESOURCE_SELECTED = 20

from security import unsafe_reason

try:
    MANAGED_POLICY = load_policy()
    HA_CONFIG_ROOT = MANAGED_POLICY.ha_root
    MANAGED_ENTRIES = list(MANAGED_POLICY.exact)
except Exception as policy_error:  # Fail closed at import time with a clear message.
    MANAGED_POLICY = None
    HA_CONFIG_ROOT, MANAGED_ENTRIES = Path("/homeassistant"), []
    MANAGED_POLICY_ERROR = str(policy_error)
else:
    MANAGED_POLICY_ERROR = None




@app.before_request
def ingress_only():
    if request.remote_addr != "172.30.32.2":
        abort(403)


def git_environment():
    command = (
        f"ssh -i {KEY} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={KNOWN_HOSTS} "
        "-o StrictHostKeyChecking=yes -p 443"
    )
    environment = os.environ.copy()
    environment["GIT_SSH_COMMAND"] = command
    return environment


def run(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        env=git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=True,
    ).stdout.strip()


def refresh_repo(source_ref=None, pin_sha=None, *, allow_main_ancestor=False):
    with REPO_LOCK:
        if not KEY.exists():
            raise RuntimeError("Read-only GitHub deploy key is not configured.")
        if not KNOWN_HOSTS.exists():
            raise RuntimeError("GitHub known_hosts file is not configured.")
        return checkout_source(
            repo=REPO,
            workdir=WORKDIR,
            runner=run,
            source_ref=source_ref or DEFAULT_BRANCH,
            pin_sha=pin_sha,
            allow_main_ancestor=allow_main_ancestor,
        )


def requested_source_from_request():
    try:
        return parse_requested_source(
            request.values.get("source"),
            request.values.get("source_sha"),
        )
    except InvalidSourceRef as error:
        raise InvalidSourceRef(str(error)) from error


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def base_hash(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
        return entry["sha256"]
    return None


def load_bases():
    path = WORKDIR / STATE_FILE
    if not path.exists():
        return {}
    value = load_json(path)
    dashboards = value.get("dashboards", {}) if isinstance(value, dict) else {}
    return dashboards if isinstance(dashboards, dict) else {}


def dashboard_url_path(relative):
    stem = Path(relative).stem
    if stem in {"lovelace", "dashboard-lovelace"}:
        return None
    return stem


def ha_ws_call(message_type, **payload):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable.")
    timeout = payload.pop("_timeout", 15)
    websocket = create_connection("ws://supervisor/core/websocket", timeout=timeout)
    try:
        hello = json.loads(websocket.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("Unexpected Home Assistant WebSocket handshake.")
        websocket.send(json.dumps({"type": "auth", "access_token": token}))
        authentication = json.loads(websocket.recv())
        if authentication.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket authentication failed.")
        websocket.send(json.dumps({"id": 1, "type": message_type, **payload}))
        while True:
            response = json.loads(websocket.recv())
            if response.get("id") != 1:
                continue
            if not response.get("success"):
                error = response.get("error") or {}
                raise RuntimeError(
                    "Home Assistant WebSocket command failed: "
                    + str(error.get("message") or "unknown error")
                )
            return response.get("result")
    finally:
        websocket.close()


MISSING_DASHBOARD_MARKERS = (
    "unknown config specified",
    "no config found",
)


def ha_dashboard_config(relative):
    url_path = dashboard_url_path(relative)
    payload = {"url_path": url_path} if url_path else {}
    try:
        result = ha_ws_call("lovelace/config", **payload)
    except RuntimeError as error:
        message = str(error).lower()
        if any(marker in message for marker in MISSING_DASHBOARD_MARKERS):
            return None
        raise
    if not isinstance(result, dict):
        raise RuntimeError(f"Dashboard {url_path or 'default'} returned invalid config.")
    return result


def ha_registered_url_paths():
    result = ha_ws_call("lovelace/dashboards/list")
    paths = set()
    if not isinstance(result, list):
        raise RuntimeError("Dashboard list returned invalid result.")
    for item in result:
        if isinstance(item, dict) and isinstance(item.get("url_path"), str):
            paths.add(item["url_path"])
    return paths


def create_dashboard(relative, desired):
    url_path = dashboard_url_path(relative)
    result = ha_ws_call(
        "lovelace/dashboards/create",
        **dashboard_registration_payload(url_path, desired),
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError(
            f"Dashboard create for {url_path} did not return an id."
        )
    return result


def delete_dashboard(dashboard_id):
    ha_ws_call("lovelace/dashboards/delete", dashboard_id=dashboard_id)


def restore_dashboard(relative, *, created_id, before):
    """Undo a just-attempted Apply. Delete only a dashboard created in this Apply."""
    if created_id:
        delete_dashboard(created_id)
        return ha_dashboard_config(relative) is None
    if before is None:
        url_path = dashboard_url_path(relative)
        payload = {"url_path": url_path} if url_path else {}
        ha_ws_call("lovelace/config/delete", **payload)
        return ha_dashboard_config(relative) is None
    save_dashboard(relative, before)
    restored = ha_dashboard_config(relative)
    return restored is not None and digest(restored) == digest(before)


def save_dashboard(relative, desired):
    url_path = dashboard_url_path(relative)
    if is_ephemeral_dashboard(relative) or is_ephemeral_dashboard(url_path):
        raise RuntimeError("Refusing Apply to ephemeral preview dashboard.")
    payload = {"config": desired}
    if url_path:
        payload["url_path"] = url_path
    ha_ws_call("lovelace/config/save", **payload)


def request_export(applied):
    ha_ws_call(
        "fire_event",
        event_type=IMPORT_APPLIED_EVENT,
        event_data={"dashboards": applied, "count": len(applied)},
    )


def pretty_lines(value):
    return json_lines(value)


def dashboard_review_fields(relative, current, github):
    diff = compare_json(current, github)
    return {
        "kind": "dashboard",
        "anchor": file_anchor("dashboard", relative),
        "added": diff["added"],
        "removed": diff["removed"],
    }


def valid_relative(value):
    path = PurePosixPath(value)
    return len(path.parts) == 1 and path.suffix == ".json" and ".." not in path.parts


def persist_provenance(store, *, required_dashboard=None, required_hash=None):
    saved = save_store(
        store,
        local_path=IMPORT_STATE_PATH,
        shared_path=SHARED_STATE_PATH,
    )
    verified = load_json_store(
        SHARED_STATE_PATH, required_parent=SHARED_STATE_PATH.parent
    )
    if required_dashboard:
        entry = verified.dashboards.get(required_dashboard)
        if entry is None:
            raise OSError(
                f"Shared provenance missing {required_dashboard} after persist."
            )
        if required_hash and entry.content_hash != required_hash:
            raise OSError(
                f"Shared provenance hash mismatch for {required_dashboard}."
            )
    return saved


def live_provenance_store():
    return load_import_store(
        local_path=IMPORT_STATE_PATH,
        shared_path=SHARED_STATE_PATH,
    )


def collect_changes(commit_sha="", revision=None, only_relative=None):
    if revision is not None:
        commit_sha = revision.commit_sha
        reviewing_canonical = is_canonical_source(
            revision.source_ref, revision.source_kind
        )
    else:
        reviewing_canonical = False
    root = WORKDIR / "dashboards"
    bases = load_bases()
    store = live_provenance_store()
    pending_adopt = []
    changes = []
    registered_paths = None
    if not root.exists():
        return changes
    for github_path in sorted(root.glob("*.json")):
        relative = github_path.name
        if only_relative is not None and relative != only_relative:
            continue
        if not valid_relative(relative):
            continue
        if is_ephemeral_dashboard(relative):
            continue
        github = load_json(github_path)
        current = ha_dashboard_config(relative)
        base = base_hash(bases.get(relative))
        live_entry = store.dashboards.get(relative)
        invalid_source = (
            "Git" if not valid_dashboard_structure(github)
            else "Home Assistant" if current is not None and not valid_dashboard_structure(current)
            else None
        )
        if invalid_source:
            status, css, selectable = "ERROR", "error", False
            reason = (
                f"Nieprawidłowa struktura dashboardu w {invalid_source}. "
                "Dashboard musi być obiektem JSON, a views listą obiektów."
            )
            should_adopt = False
        elif current is None:
            if registered_paths is None:
                registered_paths = ha_registered_url_paths()
            url_path = dashboard_url_path(relative)
            status, css, selectable, reason = classify_missing_ha(
                url_path, registered=url_path in registered_paths
            )
            should_adopt = False
        else:
            status, css, selectable, reason, should_adopt = classify_with_provenance(
                github,
                current,
                base,
                reviewing_canonical=reviewing_canonical,
                provenance=live_entry,
            )
        if should_adopt and revision is not None:
            pending_adopt.append((relative, digest(current)))
        warnings = format_scan_warnings(github, "\n".join(pretty_lines(github)))
        changes.append({
            "name": github_path.stem,
            "relative": relative,
            "github": github,
            "current": current,
            "base": base,
            "preview_ha_hash": digest(current),
            "preview_desired_hash": digest(github),
            "status": status,
            "css": css,
            "selectable": selectable,
            "reason": reason,
            "warnings": warnings,
            "source_sha": commit_sha,
            "provenance_label": provenance_label(live_entry),
            "live_source_ref": None if live_entry is None else live_entry.source_ref,
            "live_short_sha": None if live_entry is None else live_entry.short_sha(),
            **dashboard_review_fields(relative, current, github),
        })
    if pending_adopt and revision is not None:
        try:
            for relative, content_hash in pending_adopt:
                store = adopt_canonical(
                    store,
                    relative=relative,
                    commit_sha=revision.commit_sha,
                    content_hash=content_hash,
                )
            persist_provenance(store)
            adopted = {name for name, _hash in pending_adopt}
            for change in changes:
                if change["relative"] not in adopted:
                    continue
                live_entry = store.dashboards[change["relative"]]
                change["status"] = "SAME"
                change["css"] = "same"
                change["selectable"] = False
                change["reason"] = "LIVE matches main — marked CANONICAL MAIN."
                change["provenance_label"] = provenance_label(live_entry)
                change["live_source_ref"] = live_entry.source_ref
                change["live_short_sha"] = live_entry.short_sha()
        except OSError:
            pass
    return changes


def public_managed_change(change):
    """Drop in-memory blobs before template render."""
    return {
        key: value for key, value in change.items()
        if key not in {"github_data", "diff", "rows"}
    }


def managed_entries_for_revision():
    if MANAGED_POLICY is None or MANAGED_POLICY_ERROR:
        return []
    return discover_managed_entries(WORKDIR, MANAGED_POLICY)


def coerce_revision(value, requested_ref=DEFAULT_BRANCH) -> SourceRevision:
    if isinstance(value, SourceRevision):
        return value
    if isinstance(value, str):
        digest = value.lower()
        if len(digest) == 40 and all(char in "0123456789abcdef" for char in digest):
            sha = digest
            short = sha[:7]
        else:
            sha = "0" * 40
            short = value[:32] or "-"
        return SourceRevision(
            source_ref=requested_ref,
            source_kind="branch",
            commit_sha=sha,
            short_sha=short,
            available_branches=(DEFAULT_BRANCH,),
            branch_tip_sha=sha,
            stale=False,
        )
    raise TypeError("Git source refresh returned an unexpected revision type.")


def stale_source_message(revision: SourceRevision) -> str:
    reviewed = (revision.reviewed_sha or "")[:7] or "?"
    current = revision.short_sha or "?"
    return (
        "SOURCE UPDATED — REFRESH REVIEW. "
        f"Branch HEAD changed: reviewed {reviewed}, current {current}. "
        "Apply was not performed."
    )


def render_review(results=None, *, source=None, pin_sha=None):
    error = None
    resource_error = None
    changes = []
    managed_changes = []
    resource_changes = []
    revision = source
    project_targets = {}
    with REPO_LOCK:
        try:
            requested_ref, _kind = requested_source_from_request()
            if revision is None:
                revision = coerce_revision(
                    refresh_repo(requested_ref, pin_sha=pin_sha),
                    requested_ref,
                )
            else:
                revision = coerce_revision(revision, requested_ref)
            for project in PROJECTS.values():
                try:
                    project_targets[project.id] = release_set(
                        load_json(WORKDIR / "deployments" / (project.id + ".json")), project
                    )
                except Exception:
                    project_targets[project.id] = None
            changes = collect_changes(revision=revision)
            entries = managed_entries_for_revision()
            if entries and not MANAGED_POLICY_ERROR:
                managed_changes, _bases = collect_managed_changes(
                    WORKDIR,
                    HA_CONFIG_ROOT,
                    entries,
                    unsafe_reason,
                    stage_frontend=True,
                    commit_sha=revision.commit_sha,
                )
                managed_store = live_provenance_store()
                for item in managed_changes:
                    live_entry = managed_store.managed_files.get(item["relative"])
                    item["provenance_label"] = provenance_label(live_entry)
                    item["live_source_ref"] = (
                        None if live_entry is None else live_entry.source_ref
                    )
                    item["live_short_sha"] = (
                        None if live_entry is None else live_entry.short_sha()
                    )
                managed_changes = [public_managed_change(item) for item in managed_changes]
            if MANAGED_POLICY is not None and not MANAGED_POLICY_ERROR:
                try:
                    resource_changes = collect_resource_changes(
                        MANAGED_POLICY, ha_ws_call, entries
                    )
                    resource_changes = [
                        public_managed_change(item) for item in resource_changes
                    ]
                except Exception as exception:
                    resource_error = str(exception)
        except InvalidSourceRef as exception:
            error = str(exception)
            revision = revision if isinstance(revision, SourceRevision) else SourceRevision.fallback()
        except Exception as exception:
            error = str(exception)
            if not isinstance(revision, SourceRevision):
                revision = SourceRevision.fallback()
    has_ready = (
        not error
        and not (revision and revision.stale)
        and (
            any(change["selectable"] for change in changes)
            or any(change["selectable"] for change in managed_changes)
            or any(change["selectable"] for change in resource_changes)
        )
    )
    groups = configuration_groups(changes, managed_changes, resource_changes)
    projects = app.extensions["project_overview"](
        {} if error or (revision and revision.stale) else project_targets,
        canonical=bool(revision and is_canonical_source(
            revision.source_ref, revision.source_kind,
        )),
    )
    return render_template_string(
        TEMPLATE,
        ui_style=STYLE,
        ui_script=SCRIPT,
        configuration_groups=groups,
        changed_groups=[g for g in groups if g["changed"]],
        unchanged_count=sum(len(g["unchanged"]) for g in groups),
        project_overview=projects,
        overview=review_summary(groups, projects, incomplete=bool(
            error or MANAGED_POLICY_ERROR or resource_error or (revision and revision.stale)
        )),
        managed_policy_error=MANAGED_POLICY_ERROR,
        resource_error=resource_error,
        error=error,
        commit=revision.short_sha if revision else "-",
        git_source=revision,
        has_ready=has_ready,
        has_missing_base=any(
            change["status"] == MISSING_BASE_STATUS for change in changes
        ),
        has_managed_missing_base=any(
            change["status"] == MISSING_BASE_STATUS for change in managed_changes
        ),
        results=results or [],
    )


@app.route("/")
def index():
    return render_review()


@app.route("/apply", methods=["POST"])
def apply_selected():
    try:
        results = apply_plan(request.form)
    except DeploymentBusy:
        return render_review([{"ok": False, "message": "HA jest zarezerwowany przez aktualizację projektu. Dokończ lub wznów ją przed Apply."}]), 409
    except ValueError:
        abort(400)
    return render_review(results)


def apply_plan(form, *, request_export_after=True, owner=None):
    """Shared non-HTTP Apply interface; routes and project workers use the same guards."""
    with COORDINATOR.operation(*(owner or (None, None))):
        if owner is not None and COORDINATOR.owner() != {"project": owner[0], "id": owner[1]}:
            raise DeploymentBusy("Canonical project pin requires its approved owner")
        return _apply_plan(
            form, request_export_after=request_export_after,
            allow_main_ancestor=owner is not None,
        )


def _apply_plan(form, *, request_export_after=True, allow_main_ancestor=False):
    selected = form.getlist("selected")
    managed_selected = form.getlist("managed_selected")
    resource_selected = form.getlist("resource_selected")
    if not selected and not managed_selected and not resource_selected:
        return [{"ok": False, "message": "No READY item selected."}]
    if (
        len(selected) > MAX_DASHBOARD_SELECTED
        or len(set(selected)) != len(selected)
        or any(not valid_relative(value) for value in selected)
        or any(is_ephemeral_dashboard(value) for value in selected)
    ):
        raise ValueError("Invalid Apply plan")
    if (
        len(managed_selected) > MAX_MANAGED_SELECTED
        or len(set(managed_selected)) != len(managed_selected)
    ):
        raise ValueError("Invalid Apply plan")
    if (
        len(resource_selected) > MAX_RESOURCE_SELECTED
        or len(set(resource_selected)) != len(resource_selected)
    ):
        raise ValueError("Invalid Apply plan")
    try:
        for value in managed_selected:
            validate_policy_path(value)
    except ValueError:
        raise ValueError("Invalid Apply plan")
    reviewed_sha = (form.get("reviewed_sha") or "").strip()
    if not reviewed_sha:
        raise ValueError("Invalid Apply plan")

    previews = {}
    desired_previews = {}
    managed_previews = {}
    managed_desired = {}
    resource_previews = {}
    resource_desired = {}
    try:
        requested_ref, _kind = parse_requested_source(form.get("source"), form.get("source_sha"))
        if selected:
            previews = parse_preview_hashes(form.getlist("preview_hash"))
            desired_previews = parse_preview_hashes(form.getlist("desired_hash"))
            if any(relative not in previews or relative not in desired_previews for relative in selected):
                raise ValueError("Invalid Apply plan")
        if managed_selected:
            managed_previews = parse_preview_hashes(form.getlist("managed_preview_hash"))
            managed_desired = parse_preview_hashes(form.getlist("managed_desired_hash"))
            if any(
                relative not in managed_previews or relative not in managed_desired
                for relative in managed_selected
            ):
                raise ValueError("Invalid Apply plan")
        if resource_selected:
            resource_previews = parse_preview_hashes(
                form.getlist("resource_preview_hash")
            )
            resource_desired = parse_preview_hashes(
                form.getlist("resource_desired_hash")
            )
            if any(
                url not in resource_previews or url not in resource_desired
                for url in resource_selected
            ):
                raise ValueError("Invalid Apply plan")
    except InvalidSourceRef:
        raise ValueError("Invalid Apply plan")
    except ValueError:
        raise ValueError("Invalid Apply plan")

    results = []
    applied = []
    managed_applied = []
    revision = None
    with REPO_LOCK:
        try:
            revision = coerce_revision(
                refresh_repo(
                    requested_ref, pin_sha=reviewed_sha,
                    **({"allow_main_ancestor": True} if allow_main_ancestor else {}),
                ),
                requested_ref,
            )
            if revision.stale or revision.commit_sha != reviewed_sha:
                revision = SourceRevision(
                    source_ref=revision.source_ref,
                    source_kind=revision.source_kind,
                    commit_sha=revision.commit_sha,
                    short_sha=revision.short_sha,
                    available_branches=revision.available_branches,
                    branch_tip_sha=revision.branch_tip_sha,
                    stale=True,
                    reviewed_sha=reviewed_sha,
                )
                results.append({"ok": False, "message": stale_source_message(revision)})
                return results
            entries = managed_entries_for_revision()
            allowed_managed = {entry.path for entry in entries}
            if any(value not in allowed_managed for value in managed_selected):
                raise ValueError("Invalid Apply plan")
            allowed_resources = {
                spec.url
                for spec in (
                    resource_specs_for_entries(MANAGED_POLICY, entries)
                    if MANAGED_POLICY else ()
                )
            }
            if any(value not in allowed_resources for value in resource_selected):
                raise ValueError("Invalid Apply plan")
            store = live_provenance_store()
            created_resource_ids = []
            resource_failed = False
            if managed_selected:
                token = os.environ.get("SUPERVISOR_TOKEN")
                if not token:
                    raise RuntimeError("SUPERVISOR_TOKEN is unavailable.")
                managed_results, managed_applied = apply_managed_files(
                    managed_selected,
                    managed_previews,
                    managed_desired,
                    WORKDIR,
                    HA_CONFIG_ROOT,
                    entries,
                    unsafe_reason,
                    ha_ws_call,
                    token,
                    source_ref=revision.source_ref,
                    commit_sha=revision.commit_sha,
                    defer_cache_bust_urls=frozenset(resource_selected),
                )
                results.extend(managed_results)
            if managed_applied:
                try:
                    store = record_artifacts(
                        store,
                        source_ref=revision.source_ref,
                        source_kind=revision.source_kind,
                        commit_sha=revision.commit_sha,
                        managed_files={
                            relative: managed_desired[relative]
                            for relative in managed_applied
                        },
                    )
                    persist_provenance(store)
                except (OSError, InvalidProvenance):
                    results.append({
                        "ok": False,
                        "message": (
                            "Managed files were applied, but provenance metadata "
                            "could not be stored."
                        ),
                    })
            if resource_selected:
                resource_results, created_resource_ids = apply_declared_resources(
                    resource_selected,
                    resource_previews,
                    resource_desired,
                    MANAGED_POLICY,
                    ha_ws_call,
                    entries=entries,
                    workdir=WORKDIR,
                )
                results.extend(resource_results)
                resource_failed = any(not item.get("ok") for item in resource_results)
                if resource_failed:
                    created_resource_ids = []
            if selected and not resource_failed:
                fresh = {
                    change["relative"]: change
                    for change in collect_changes(revision=revision)
                }
                store = live_provenance_store()
                for relative in selected:
                    change = fresh.get(relative)
                    if change and (not matches_preview(change["current"], previews[relative])
                                   or not matches_preview(change["github"], desired_previews[relative])):
                        results.append({
                            "ok": False,
                            "message": (
                                f"{relative}: HA or Git desired changed since review. "
                                "Refresh and review the updated items before Apply."
                            ),
                        })
                        continue
                    if not change or change["status"] not in APPLYABLE_STATUSES:
                        status = change["status"] if change else "missing"
                        results.append({
                            "ok": False,
                            "message": f"{relative}: blocked by fresh conflict-check ({status}).",
                        })
                        continue
                    if not matches_preview(ha_dashboard_config(relative), previews[relative]):
                        results.append({"ok": False, "message": f"{relative}: HA changed before save. Review again."})
                        continue
                    before = change["current"]
                    created_id = None
                    try:
                        if change["status"] == CREATE_STATUS:
                            created_id = create_dashboard(relative, change["github"])["id"]
                        save_dashboard(relative, change["github"])
                        verified = ha_dashboard_config(relative)
                    except Exception as apply_error:
                        if created_id:
                            try:
                                restore_dashboard(
                                    relative, created_id=created_id, before=before
                                )
                            except Exception:
                                pass
                        results.append({
                            "ok": False,
                            "message": f"{relative}: Apply failed: {apply_error}",
                        })
                        continue
                    if verified is None or digest(verified) != digest(change["github"]):
                        try:
                            restore_dashboard(
                                relative, created_id=created_id, before=before
                            )
                        except Exception:
                            pass
                        results.append({
                            "ok": False,
                            "message": f"{relative}: save returned, but read-back verification failed.",
                        })
                        continue
                    try:
                        next_store = record_artifacts(
                            store,
                            source_ref=revision.source_ref,
                            source_kind=revision.source_kind,
                            commit_sha=revision.commit_sha,
                            dashboards={relative: desired_previews[relative]},
                        )
                        persist_provenance(
                            next_store,
                            required_dashboard=relative,
                            required_hash=desired_previews[relative],
                        )
                    except (OSError, InvalidProvenance) as persist_error:
                        rolled_back = False
                        try:
                            rolled_back = restore_dashboard(
                                relative, created_id=created_id, before=before
                            )
                        except Exception:
                            rolled_back = False
                        if not rolled_back:
                            try:
                                arm_fail_closed_guard(shared_path=SHARED_STATE_PATH)
                            except OSError:
                                pass
                            results.append({
                                "ok": False,
                                "message": (
                                    f"{relative}: LIVE was written but provenance could not "
                                    "be persisted and rollback failed. Dashboard export to "
                                    "main is blocked until provenance is restored."
                                ),
                            })
                        else:
                            results.append({
                                "ok": False,
                                "message": (
                                    f"{relative}: provenance persist failed; LIVE rolled back. "
                                    f"Apply FAILED ({persist_error})."
                                ),
                            })
                        continue
                    store = next_store
                    applied_label = (
                        "Created, applied and verified."
                        if created_id
                        else "Applied and verified."
                    )
                    results.append({
                        "ok": True,
                        "message": f"{relative}: {applied_label}",
                    })
                    applied.append(relative)
            if created_resource_ids and selected and not applied:
                try:
                    rollback_created_resources(ha_ws_call, created_resource_ids)
                    results.append({
                        "ok": False,
                        "message": (
                            "Created Lovelace resources were rolled back after "
                            "dashboard Apply failed."
                        ),
                    })
                except Exception as rollback_error:
                    results.append({
                        "ok": False,
                        "message": (
                            "Dashboard Apply failed, and rolling back created "
                            f"Lovelace resources also failed: {rollback_error}"
                        ),
                    })
            if applied or managed_applied:
                try:
                    record_last_apply(
                        source_ref=revision.source_ref,
                        commit_sha=revision.commit_sha,
                        dashboards=applied,
                        managed_files=managed_applied,
                    )
                except OSError:
                    results.append({
                        "ok": True,
                        "message": "Apply succeeded; last-apply metadata could not be stored.",
                    })
        except Exception as exception:
            results.append({"ok": False, "message": f"Apply failed: {exception}"})
    if applied and request_export_after:
        try:
            request_export(applied)
            results.append({
                "ok": True,
                "message": "Automatic Export requested through Home Assistant.",
            })
        except Exception as exception:
            results.append({
                "ok": False,
                "message": (
                    "Dashboards were applied, but automatic Export could not "
                    f"be requested: {exception}"
                ),
            })
    if managed_applied and not applied:
        results.append({
            "ok": True,
            "message": (
                "Managed files applied. No automatic Export was requested "
                "(managed files are Git → HA only)."
            ),
        })
    return results


@app.route("/managed-base", methods=["POST"])
def managed_initialize_bases():
    results = []
    with REPO_LOCK:
        try:
            requested_ref, _kind = requested_source_from_request()
            refresh_repo(requested_ref)
            initialized = initialize_missing_bases(
                WORKDIR, HA_CONFIG_ROOT, managed_entries_for_revision(), unsafe_reason
            )
            if initialized:
                results.append({
                    "ok": True,
                    "message": "Initialized managed-file bases: " + ", ".join(initialized),
                })
            else:
                results.append({
                    "ok": False,
                    "message": "No in-sync managed file with a missing base was found.",
                })
        except Exception as exception:
            results.append({
                "ok": False,
                "message": f"Managed base initialization failed: {exception}",
            })
    return render_review(results)


@app.route("/export", methods=["POST"])
def export_missing_bases():
    results = []
    missing = []
    with REPO_LOCK:
        try:
            requested_ref, _kind = requested_source_from_request()
            revision = coerce_revision(refresh_repo(requested_ref), requested_ref)
            missing = [
                change["relative"]
                for change in collect_changes(revision=revision)
                if change["status"] == MISSING_BASE_STATUS
            ]
            if missing:
                request_export(missing)
                results.append({
                    "ok": True,
                    "message": (
                        "Export requested to initialize missing dashboard bases."
                    ),
                })
            else:
                results.append({
                    "ok": False,
                    "message": "No in-sync dashboard with a missing base was found.",
                })
        except Exception as exception:
            results.append({
                "ok": False,
                "message": f"Export request failed: {exception}",
            })
    return render_review(results)


class DashboardAccess:
    """Concrete adapter to the existing Git, provenance and HA implementation."""

    def review(self, project, sha=None):
        project = PROJECTS[project] if isinstance(project, str) else project
        with REPO_LOCK:
            revision = coerce_revision(
                refresh_repo(
                    "main", pin_sha=sha,
                    **({"allow_main_ancestor": True} if sha else {}),
                ), "main",
            )
            if revision.stale or (sha and revision.commit_sha != sha):
                raise ValueError("Reviewed configuration revision changed")
            releases = release_set(
                load_json(WORKDIR / "deployments" / (project.id + ".json")), project
            )
            entries = managed_entries_for_revision()
            managed, _ = collect_managed_changes(
                WORKDIR,
                HA_CONFIG_ROOT,
                [e for e in entries if e.path in project.bundles],
                unsafe_reason,
                stage_frontend=False,
                commit_sha=revision.commit_sha,
            )
            dashboards = [
                c
                for name in project.dashboards
                for c in collect_changes(revision=revision, only_relative=name)
                if c["relative"] == name
            ]
            resources = [
                c
                for c in collect_resource_changes(MANAGED_POLICY, ha_ws_call, entries)
                if c["relative"] in project.resources
            ]
            items = []
            for kind, values, names in (
                ("dashboard", dashboards, project.dashboards),
                ("managed", managed, project.bundles),
                ("resource", resources, project.resources),
            ):
                if len(values) != len(names) or {i["relative"] for i in values} != set(
                    names
                ):
                    raise ValueError("Incomplete project dashboard set")
                items.extend({**i, "kind": kind} for i in values)
            return revision, releases, items

    def read(self, kind, relative):
        if kind == "dashboard":
            return digest(ha_dashboard_config(relative))
        if kind == "managed":
            return read_snapshot(resolve_live_path(HA_CONFIG_ROOT, relative)).sha256
        matches = [
            i
            for i in collect_resource_changes(
                MANAGED_POLICY, ha_ws_call, managed_entries_for_revision()
            )
            if i["relative"] == relative
        ]
        if len(matches) != 1:
            raise ValueError("Resource not found")
        return matches[0]["preview_ha_hash"]

    def provenance(self, kind, relative):
        store = live_provenance_store()
        return (store.dashboards if kind == "dashboard" else store.managed_files).get(
            relative
        )

    def apply(self, form, owner):
        data = MultiDict((k, v) for k, values in form.items() for v in values)
        return apply_plan(data, request_export_after=False, owner=owner)

    def export(self, dashboards):
        request_export(dashboards)

register_projects(app, DashboardSync(DashboardAccess()), ha_ws_call, HA_CONFIG_ROOT, COORDINATOR)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
