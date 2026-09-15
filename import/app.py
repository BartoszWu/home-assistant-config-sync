import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path, PurePosixPath

from flask import Flask, abort, render_template_string, request
from websocket import create_connection

from dashboard_logic import (
    APPLYABLE_STATUSES,
    MISSING_BASE_STATUS,
    classify,
    digest,
    matches_preview,
    parse_preview_hashes,
)
from diff_view import (
    compare_json,
    file_anchor,
    hunk_rows,
    is_changed_review,
    json_lines,
    summarize_changed_files,
)
from managed_files import (
    apply_managed_files,
    collect_managed_changes,
    initialize_missing_bases,
    load_policy,
)
from review_warnings import format_scan_warnings
from visual_preview import prepare_preview


app = Flask(__name__)
VISUAL_PREVIEW_ASSET = Path(__file__).with_name("static") / "visual-preview.mjs"
VISUAL_PREVIEW_VERSION = hashlib.sha256(VISUAL_PREVIEW_ASSET.read_bytes()).hexdigest()[:12]

REPO = "ssh://git@ssh.github.com:443/BartoszWu/home-assistant-config.git"
BRANCH = "main"
KEY = Path("/review/ssh/github_ed25519")
KNOWN_HOSTS = Path("/review/ssh/known_hosts_443")
WORKDIR = Path("/tmp/home-assistant-config")
STATE_FILE = Path("state/dashboard-bases.json")
IMPORT_APPLIED_EVENT = "ha_config_sync_import_applied"
REPO_LOCK = threading.RLock()

from security import unsafe_reason

try:
    HA_CONFIG_ROOT, MANAGED_ENTRIES = load_policy()
except Exception as policy_error:  # Fail closed at import time with a clear message.
    HA_CONFIG_ROOT, MANAGED_ENTRIES = Path("/homeassistant"), []
    MANAGED_POLICY_ERROR = str(policy_error)
else:
    MANAGED_POLICY_ERROR = None


TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HA Config Sync — Import</title>
<style>
:root {
  color-scheme: light dark;
  --page-background:#f4f6f8; --text-color:#202124; --card-background:white;
  --muted-color:#687078; --same-background:#e8eaed; --same-text:#202124;
  --ready-background:#d8f3dc; --ready-text:#165a2e;
  --changed-background:#fff0c2; --changed-text:#624b00;
  --error-background:#ffd6d6; --error-text:#7a1717;
  --result-background:#d8f3dc; --result-text:#165a2e;
  --result-bad-background:#ffd6d6; --result-bad-text:#7a1717;
  --reason-background:#fff4d6; --reason-text:#624b00;
}
body { font-family: system-ui,-apple-system,sans-serif; margin:0; background:var(--page-background); color:var(--text-color); }
main { max-width:1400px; margin:auto; padding:24px; }
header { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:20px; }
h1 { margin:0 0 4px; }
button { padding:10px 18px; border:0; border-radius:8px; cursor:pointer; background:#03a9f4; color:white; font-weight:650; }
button[disabled] { opacity:.45; cursor:not-allowed; }
.card { background:var(--card-background); border-radius:12px; padding:18px; margin-bottom:16px; box-shadow:0 1px 4px #0002; }
.meta { display:grid; grid-template-columns:max-content 1fr; gap:5px 12px; }
.small { color:var(--muted-color); font-size:13px; }
.status { display:inline-block; padding:4px 9px; border-radius:12px; font-size:12px; font-weight:750; margin-left:7px; }
.same { background:var(--same-background); color:var(--same-text); }
.ready { background:var(--ready-background); color:var(--ready-text); }
.bootstrap { background:var(--ready-background); color:var(--ready-text); }
.changed { background:var(--changed-background); color:var(--changed-text); }
.missing-base { background:var(--changed-background); color:var(--changed-text); }
.conflict,.unsafe,.error { background:var(--error-background); color:var(--error-text); }
.result { padding:12px 14px; border-radius:8px; margin-bottom:10px; background:var(--result-background); color:var(--result-text); }
.result.bad { background:var(--result-bad-background); color:var(--result-bad-text); }
.reason { padding:10px 12px; border-radius:8px; background:var(--reason-background); color:var(--reason-text); }
.change-head { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
input[type=checkbox] { transform:scale(1.25); }
details { margin-top:14px; }
summary { cursor:pointer; font-weight:650; }
.diff-wrap { margin-top:10px; overflow:auto; border:1px solid #d9dde1; border-radius:8px; max-height:650px; }
table.diff { width:100%; border-collapse:collapse; table-layout:fixed; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; background:#fbfbfb; color:#202124; }
.diff th { position:sticky; top:0; z-index:1; background:#edf1f4; text-align:left; padding:8px; }
.diff td { vertical-align:top; white-space:pre-wrap; overflow-wrap:anywhere; border-top:1px solid #eceff1; }
.ln { width:42px; text-align:right; padding:2px 7px; color:#8a929a; user-select:none; background:#f4f6f8; }
.code { padding:2px 8px; }
.left-del,.right-add { background:#ffe5e5; }
.right-add { background:#dcf8e3; }
.blank { background:#f6f7f8; }
.card[id] { scroll-margin-top: 16px; }
.counts { margin-left:auto; font-size:13px; color:var(--muted-color); }
.counts .add { color:#1a7f37; font-weight:650; }
.counts .del { color:#d1242f; font-weight:650; }
.files-changed h2 { margin:0 0 6px; }
.file-list { list-style:none; margin:12px 0 0; padding:0; border-top:1px solid #d9dde1; }
.file-list a { display:flex; gap:12px; align-items:center; flex-wrap:nowrap; padding:10px 0; text-decoration:none; color:inherit; border-bottom:1px solid #eceff1; }
.file-list a:hover { background:#eef3f7; }
.file-list .path { font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.diff-gap td { text-align:center; background:#f0f4f8; color:#57606a; font-size:12px; padding:6px 8px; }
.diff-hunk td { background:#ddf4ff; color:#0550ae; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; padding:4px 8px; text-align:left; }
.unchanged-files { margin-top:20px; }
.actions { position:sticky; bottom:0; display:flex; gap:8px; justify-content:flex-end; padding-top:12px; }
.preview-tabs { display:flex; gap:8px; margin:12px 0; }
.preview-tabs button[aria-selected="false"] { background:#687078; }
.preview-pair { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:12px; }
.preview-viewport { position:relative; overflow:hidden; background:#f4f6f8; }
.preview-viewport iframe { position:absolute; top:0; left:0; border:0; transform-origin:top left; pointer-events:none; }
.preview-viewport::after { content:""; position:absolute; inset:0; }
[hidden] { display:none !important; }
.apply-progress { display:none; align-items:center; gap:9px; margin-right:12px; padding:9px 12px; border-radius:8px; background:#e3f2fd; color:#174f78; font-weight:650; }
.apply-progress.visible { display:flex; }
.spinner { width:16px; height:16px; border:3px solid #8bcdf1; border-top-color:#0277bd; border-radius:50%; animation:spin .75s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-color-scheme:dark) {
  :root {
    --page-background:#111418; --text-color:#e8eaed; --card-background:#202428;
    --muted-color:#aab0b6; --same-background:#30363d; --same-text:#e8eaed;
    --ready-background:#1f5130; --ready-text:#d8f3dc;
    --changed-background:#5b4811; --changed-text:#fff0c2;
    --error-background:#57272a; --error-text:#ffd6d6;
    --result-background:#1f5130; --result-text:#d8f3dc;
    --result-bad-background:#57272a; --result-bad-text:#ffd6d6;
    --reason-background:#5b4811; --reason-text:#fff0c2;
  }
  table.diff { background:#171a1e; color:#e8eaed; }
  .diff th,.ln { background:#252a2f; }
  .diff td { border-color:#30353a; }
  .left-del { background:#57272a; } .right-add { background:#1f5130; } .blank { background:#202428; }
  .file-list { border-color:#30353a; }
  .file-list a { border-color:#30353a; }
  .file-list a:hover { background:#252a2f; }
  .counts .add { color:#3fb950; }
  .counts .del { color:#f85149; }
  .diff-gap td { background:#252a2f; color:#aab0b6; }
  .diff-hunk td { background:#1c3d5a; color:#79c0ff; }
  .preview-viewport { background:#1a1e22; }
}
</style>
</head>
<body><main>
<header>
  <div><h1>HA Config Sync — Import</h1><div class="small">Dashboards + managed files · GitHub read-only · Apply via HA API / allowlisted FS · Export after verified dashboard Apply</div></div>
  <form method="get"><button id="refresh-button" type="submit">Refresh GitHub</button></form>
</header>

{% if error %}<div class="card error"><strong>Error:</strong> {{ error }}</div>{% else %}
<div class="card meta">
  <strong>Repository:</strong><span>BartoszWu/home-assistant-config</span>
  <strong>Branch:</strong><span>{{ branch }}</span>
  <strong>Commit:</strong><span>{{ commit }}</span>
  <strong>Base state:</strong><span>state/dashboard-bases.json (dashboards) · /data/managed-file-bases.json (managed files)</span>
</div>

{% if managed_policy_error %}
<div class="card error"><strong>Managed files policy error:</strong> {{ managed_policy_error }}</div>
{% endif %}

{% for result in results or [] %}
<div class="result {% if not result.ok %}bad{% endif %}">{{ result.message }}</div>
{% endfor %}

{% if has_missing_base or has_managed_missing_base %}
<section class="card">
  <h3 style="margin-top:0">Base initialization needed</h3>
  {% if has_missing_base %}
  <p>Dashboard: GitHub and Home Assistant already match, but no exported base hash exists.</p>
  <form method="post" action="export" style="margin-bottom:12px"><button type="submit">Request Export to initialize dashboard base</button></form>
  {% endif %}
  {% if has_managed_missing_base %}
  <p>Managed files: GitHub and HA already match, but no Import base hash exists.</p>
  <form method="post" action="managed-base"><button type="submit">Initialize managed-file bases</button></form>
  {% endif %}
</section>
{% endif %}

{% macro render_diff(change) -%}
<div class="diff-wrap">
  {% if not change.diff or not change.diff.blocks %}
    <p class="small" style="margin:12px">No line changes.</p>
  {% else %}
  <table class="diff">
    <colgroup>
      <col class="ln">
      <col>
      <col class="ln">
      <col>
    </colgroup>
    <thead><tr><th colspan="2">HA current</th><th colspan="2">GitHub HEAD</th></tr></thead>
    <tbody>
    {% for block in change.diff.blocks %}
      {% if block.type == "gap" %}
        <tr class="diff-gap"><td colspan="2">{{ block.count }} unchanged line{% if block.count != 1 %}s{% endif %}</td><td colspan="2">{{ block.count }} unchanged line{% if block.count != 1 %}s{% endif %}</td></tr>
      {% else %}
        <tr class="diff-hunk"><td colspan="2">{{ block.header }}</td><td colspan="2">{{ block.header }}</td></tr>
        {% for row in block.rows %}
        <tr>
          <td class="ln {{ row.left_css }}">{{ row.left_no or '' }}</td><td class="code {{ row.left_css }}">{{ row.left }}</td>
          <td class="ln {{ row.right_css }}">{{ row.right_no or '' }}</td><td class="code {{ row.right_css }}">{{ row.right }}</td>
        </tr>
        {% endfor %}
      {% endif %}
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{%- endmacro %}

{% macro dashboard_card(change) -%}
<section class="card" id="{{ change.anchor }}">
  <div class="change-head">
    {% if change.selectable %}
    <input type="checkbox" name="selected" value="{{ change.relative }}" aria-label="Select {{ change.name }}">
    <input type="hidden" name="preview_hash" value="{{ change.relative }}:{{ change.preview_ha_hash }}">
      <input type="hidden" name="desired_hash" value="{{ change.relative }}:{{ change.preview_desired_hash }}">
    {% endif %}
    <h3 style="margin:0">{{ change.name }} <span class="status {{ change.css }}">{{ change.status }}</span>{% if change.warnings %} <span class="status changed">WARNING</span>{% endif %}</h3>
    <span class="counts"><span class="add">+{{ change.added }}</span> · <span class="del">−{{ change.removed }}</span></span>
  </div>
  <div class="small">Dashboard · dashboards/{{ change.relative }}</div>
  {% if change.reason %}<p class="reason">{{ change.reason }}</p>{% endif %}
  {% if change.warnings %}
  <p class="reason">Security scan warning — this does not block Apply. Review the flagged lines, then select manually if you still want Git → HA.
    {% for warning in change.warnings %}<br>{% if warning.line %}Line {{ warning.line }}: {% endif %}{{ warning.reason }}{% if warning.field %} (<code>{{ warning.field }}</code>){% endif %}{% if warning.path and warning.path != '$' %} at <code>{{ warning.path }}</code>{% endif %}{% endfor %}
  </p>
  {% endif %}
  <details {% if change.status != 'SAME' %}open{% endif %}>
    <summary>Review changes</summary>
    {% if change.visual %}
    <div class="visual-review">
      <script type="application/json" class="preview-data">{{ change.visual | tojson }}</script>
      <div class="preview-tabs" role="tablist" aria-label="Preview {{ change.name }}">
        <button type="button" role="tab" aria-selected="false" data-preview-tab="visual">Visual</button>
        <button type="button" role="tab" aria-selected="true" data-preview-tab="yaml">YAML diff</button>
      </div>
      <div class="visual-panel" role="tabpanel" hidden>
        <p class="small">Native HA frontend · first view · read-only · states at render time</p>
        {% if change.visual.placeholder_types %}
        <p class="reason">Partial preview — custom cards replaced with placeholders.
          Before: {{ change.visual.placeholder_counts.before }} · After: {{ change.visual.placeholder_counts.after }}.
          Types: {{ change.visual.placeholder_types | join(', ') }}.
          Layout is approximate. YAML diff and Apply use the original configuration.</p>
        {% endif %}
        <button type="button" class="preview-load">Generate visual preview</button>
        <p class="preview-status" role="status" aria-live="polite"></p>
        <div class="preview-renders"></div>
      </div>
    </div>
    {% endif %}
    <div class="yaml-panel">{{ render_diff(change)|safe }}</div>
  </details>
</section>
{%- endmacro %}

{% macro managed_card(change) -%}
<section class="card" id="{{ change.anchor }}">
  <div class="change-head">
    {% if change.selectable %}
    <input type="checkbox" name="managed_selected" value="{{ change.relative }}" aria-label="Select {{ change.relative }}">
    <input type="hidden" name="managed_preview_hash" value="{{ change.relative }}:{{ change.preview_ha_hash }}">
    <input type="hidden" name="managed_desired_hash" value="{{ change.relative }}:{{ change.preview_desired_hash }}">
    {% endif %}
    <h3 style="margin:0">{{ change.relative }} <span class="status {{ change.css }}">{{ change.status }}</span>{% if change.warnings %} <span class="status changed">WARNING</span>{% endif %}</h3>
    <span class="counts"><span class="add">+{{ change.added }}</span> · <span class="del">−{{ change.removed }}</span></span>
  </div>
  <div class="small">Profile · {{ change.profile }} · LIVE {{ change.live_hash or 'absent' }} · Git {{ change.github_hash or '-' }} · BASE {{ change.base or 'none' }}</div>
  {% if change.reason %}<p class="reason">{{ change.reason }}</p>{% endif %}
  {% if change.warnings %}
  <p class="reason">Security scan warning — this does not block Apply. Review the flagged lines, then select manually if you still want Git → HA.
    {% for warning in change.warnings %}<br>{% if warning.line %}Line {{ warning.line }}: {% endif %}{{ warning.reason }}{% if warning.field %} (<code>{{ warning.field }}</code>){% endif %}{% if warning.path and warning.path != '$' %} at <code>{{ warning.path }}</code>{% endif %}{% endfor %}
  </p>
  {% endif %}
  {% if change.staging and change.staging.preview_url %}
  <p class="small">Staged frontend preview (does not replace production): <code>{{ change.staging.preview_url }}</code></p>
  {% endif %}
  {% if change.staging and change.staging.error %}
  <p class="reason">Staging unavailable: {{ change.staging.error }}</p>
  {% endif %}
  <details {% if change.status != 'SAME' %}open{% endif %}>
    <summary>Review changes</summary>
    {{ render_diff(change)|safe }}
  </details>
</section>
{%- endmacro %}

<form id="apply-form" method="post" action="apply">
{% if not changes and not managed_changes %}<div class="card"><h2>No reviewable items</h2><p>Add dashboard JSON under <code>dashboards/</code> or managed files listed in Import policy.</p></div>{% endif %}
{% if file_summary.count %}
<nav class="card files-changed" aria-label="Changed files">
  <h2>Changed files</h2>
  <p class="small">{{ file_summary.count }} file{% if file_summary.count != 1 %}s{% endif %}
    · <span class="counts"><span class="add">+{{ file_summary.added }}</span> · <span class="del">−{{ file_summary.removed }}</span></span>
    · hunks with 3 lines of context, like GitHub</p>
  <ul class="file-list">
    {% for item in file_summary.files %}
    <li>
      <a href="#{{ item.anchor }}">
        <span class="path">{{ item.path }}</span>
        <span class="counts"><span class="add">+{{ item.added }}</span> <span class="del">−{{ item.removed }}</span></span>
        <span class="status {{ item.css }}">{{ item.status }}</span>
      </a>
    </li>
    {% endfor %}
  </ul>
</nav>
{% endif %}
{% if changed_dashboards %}<h2>Dashboards</h2>{% endif %}
{% for change in changed_dashboards %}{{ dashboard_card(change)|safe }}{% endfor %}

{% if changed_managed %}
<h2>Managed files</h2>
<p class="small">Desired state is Git → HA only. Bases live in Import <code>/data</code>. No automatic Export of these files.</p>
{% for change in changed_managed %}{{ managed_card(change)|safe }}{% endfor %}
{% endif %}

{% if unchanged_dashboards or unchanged_managed %}
<details class="unchanged-files">
  <summary>Unchanged files ({{ unchanged_count }})</summary>
  {% if unchanged_dashboards %}<h2>Dashboards</h2>{% endif %}
  {% for change in unchanged_dashboards %}{{ dashboard_card(change)|safe }}{% endfor %}
  {% if unchanged_managed %}
  <h2>Managed files</h2>
  {% for change in unchanged_managed %}{{ managed_card(change)|safe }}{% endfor %}
  {% endif %}
</details>
{% endif %}
<div class="actions">
  <button id="cancel-button" type="reset">Cancel</button>
  <div id="apply-progress" class="apply-progress" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><span>Applying and verifying…</span></div>
  <button id="apply-button" type="submit" {% if not has_ready %}disabled{% endif %}>Apply selected</button>
</div>
</form>
{% endif %}
</main>
<script>
const form = document.getElementById('apply-form');
if (form) {
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (form.dataset.submitting === 'true') return;
    const selected = form.querySelectorAll('input[name="selected"]:checked, input[name="managed_selected"]:checked');
    if (!selected.length) {
      window.alert('Select at least one dashboard or managed file ready to apply.');
      return;
    }
    form.dataset.submitting = 'true';
    const applyButton = document.getElementById('apply-button');
    const refreshButton = document.getElementById('refresh-button');
    applyButton.disabled = true;
    applyButton.textContent = 'Applying…';
    if (refreshButton) refreshButton.disabled = true;
    document.getElementById('apply-progress').classList.add('visible');
    window.setTimeout(() => form.submit(), 50);
  });
}
</script>
<script type="module" src="static/visual-preview.mjs?v={{ visual_preview_version }}"></script>
</body></html>
"""


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
        timeout=30,
        check=True,
    ).stdout.strip()


def refresh_repo():
    with REPO_LOCK:
        if not KEY.exists():
            raise RuntimeError("Read-only GitHub deploy key is not configured.")
        if not KNOWN_HOSTS.exists():
            raise RuntimeError("GitHub known_hosts file is not configured.")
        shutil.rmtree(WORKDIR, ignore_errors=True)
        run([
            "git", "clone", "--depth", "1", "--branch", BRANCH,
            REPO, str(WORKDIR),
        ])
        return run(["git", "rev-parse", "--short", "HEAD"], cwd=WORKDIR)


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
    websocket = create_connection("ws://supervisor/core/websocket", timeout=15)
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


def ha_dashboard_config(relative):
    url_path = dashboard_url_path(relative)
    payload = {"url_path": url_path} if url_path else {}
    result = ha_ws_call("lovelace/config", **payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"Dashboard {url_path or 'default'} returned invalid config.")
    return result


def save_dashboard(relative, desired):
    url_path = dashboard_url_path(relative)
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


def side_by_side(current, github):
    diff = compare_json(current, github)
    return hunk_rows(diff), diff["added"], diff["removed"]


def dashboard_review_fields(relative, current, github):
    diff = compare_json(current, github)
    return {
        "kind": "dashboard",
        "anchor": file_anchor("dashboard", relative),
        "diff": diff,
        "rows": hunk_rows(diff),
        "added": diff["added"],
        "removed": diff["removed"],
    }


def valid_relative(value):
    path = PurePosixPath(value)
    return len(path.parts) == 1 and path.suffix == ".json" and ".." not in path.parts


def collect_changes():
    root = WORKDIR / "dashboards"
    bases = load_bases()
    changes = []
    if not root.exists():
        return changes
    for github_path in sorted(root.glob("*.json")):
        relative = github_path.name
        if not valid_relative(relative):
            continue
        github = load_json(github_path)
        current = ha_dashboard_config(relative)
        base = base_hash(bases.get(relative))
        status, css, selectable, reason = classify(
            github,
            current,
            base,
        )
        warnings = format_scan_warnings(github, "\n".join(pretty_lines(github)))
        changes.append({
            "visual": prepare_preview(relative, current, github, unsafe_reason),
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
            **dashboard_review_fields(relative, current, github),
        })
    return changes


def public_managed_change(change):
    """Drop in-memory blobs before template render."""
    return {
        key: value for key, value in change.items()
        if key not in {"github_data"}
    }


def render_review(results=None):
    error = None
    commit = "-"
    changes = []
    managed_changes = []
    with REPO_LOCK:
        try:
            commit = refresh_repo()
            changes = collect_changes()
            if MANAGED_ENTRIES and not MANAGED_POLICY_ERROR:
                managed_changes, _bases = collect_managed_changes(
                    WORKDIR,
                    HA_CONFIG_ROOT,
                    MANAGED_ENTRIES,
                    unsafe_reason,
                    stage_frontend=True,
                )
                managed_changes = [public_managed_change(item) for item in managed_changes]
        except Exception as exception:
            error = str(exception)
    has_ready = any(change["selectable"] for change in changes) or any(
        change["selectable"] for change in managed_changes
    )
    return render_template_string(
        TEMPLATE,
        changes=changes,
        managed_changes=managed_changes,
        changed_dashboards=[item for item in changes if is_changed_review(item)],
        unchanged_dashboards=[item for item in changes if not is_changed_review(item)],
        changed_managed=[item for item in managed_changes if is_changed_review(item)],
        unchanged_managed=[item for item in managed_changes if not is_changed_review(item)],
        unchanged_count=sum(
            1 for item in (*changes, *managed_changes) if not is_changed_review(item)
        ),
        file_summary=summarize_changed_files(changes, managed_changes),
        managed_policy_error=MANAGED_POLICY_ERROR,
        error=error,
        commit=commit,
        branch=BRANCH,
        has_ready=has_ready,
        has_missing_base=any(
            change["status"] == MISSING_BASE_STATUS for change in changes
        ),
        has_managed_missing_base=any(
            change["status"] == MISSING_BASE_STATUS for change in managed_changes
        ),
        results=results or [],
        visual_preview_version=VISUAL_PREVIEW_VERSION,
    )


@app.route("/")
def index():
    return render_review()


@app.route("/apply", methods=["POST"])
def apply_selected():
    selected = request.form.getlist("selected")
    managed_selected = request.form.getlist("managed_selected")
    if not selected and not managed_selected:
        return render_review([{"ok": False, "message": "No READY item selected."}])
    if (
        len(selected) > 20
        or len(set(selected)) != len(selected)
        or any(not valid_relative(value) for value in selected)
    ):
        abort(400)
    allowed_managed = {entry.path for entry in MANAGED_ENTRIES}
    if (
        len(managed_selected) > 20
        or len(set(managed_selected)) != len(managed_selected)
        or any(value not in allowed_managed for value in managed_selected)
    ):
        abort(400)

    previews = {}
    desired_previews = {}
    managed_previews = {}
    managed_desired = {}
    try:
        if selected:
            previews = parse_preview_hashes(request.form.getlist("preview_hash"))
            desired_previews = parse_preview_hashes(request.form.getlist("desired_hash"))
            if any(relative not in previews or relative not in desired_previews for relative in selected):
                abort(400)
        if managed_selected:
            managed_previews = parse_preview_hashes(request.form.getlist("managed_preview_hash"))
            managed_desired = parse_preview_hashes(request.form.getlist("managed_desired_hash"))
            if any(
                relative not in managed_previews or relative not in managed_desired
                for relative in managed_selected
            ):
                abort(400)
    except ValueError:
        abort(400)

    results = []
    applied = []
    managed_applied = []
    with REPO_LOCK:
        try:
            refresh_repo()
            if selected:
                fresh = {change["relative"]: change for change in collect_changes()}
                for relative in selected:
                    change = fresh.get(relative)
                    if change and (not matches_preview(change["current"], previews[relative])
                                   or not matches_preview(change["github"], desired_previews[relative])):
                        results.append({
                            "ok": False,
                            "message": (
                                f"{relative}: HA or Git desired changed since preview. "
                                "Refresh and review the new diff before Apply."
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
                    save_dashboard(relative, change["github"])
                    verified = ha_dashboard_config(relative)
                    if digest(verified) != digest(change["github"]):
                        results.append({
                            "ok": False,
                            "message": f"{relative}: save returned, but read-back verification failed.",
                        })
                        continue
                    results.append({
                        "ok": True,
                        "message": f"{relative}: Applied and verified.",
                    })
                    applied.append(relative)
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
                    MANAGED_ENTRIES,
                    unsafe_reason,
                    ha_ws_call,
                    token,
                )
                results.extend(managed_results)
        except Exception as exception:
            results.append({"ok": False, "message": f"Apply failed: {exception}"})
    if applied:
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
    return render_review(results)


@app.route("/managed-base", methods=["POST"])
def managed_initialize_bases():
    results = []
    with REPO_LOCK:
        try:
            refresh_repo()
            initialized = initialize_missing_bases(
                WORKDIR, HA_CONFIG_ROOT, MANAGED_ENTRIES, unsafe_reason
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
            refresh_repo()
            missing = [
                change["relative"]
                for change in collect_changes()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
