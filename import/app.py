import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path, PurePosixPath

from flask import Flask, abort, render_template_string, request
from websocket import create_connection


app = Flask(__name__)

REPO = "ssh://git@ssh.github.com:443/BartoszWu/home-assistant-config.git"
BRANCH = "main"
KEY = Path("/review/ssh/github_ed25519")
KNOWN_HOSTS = Path("/review/ssh/known_hosts_443")
WORKDIR = Path("/tmp/home-assistant-config")
STATE_FILE = Path("state/dashboard-bases.json")
REPO_LOCK = threading.RLock()

SENSITIVE_KEYS = {
    "password", "passwd", "token", "access_token", "refresh_token",
    "api_key", "apikey", "client_secret", "secret", "authorization",
    "credential", "credentials", "local_key", "bindkey", "private_key",
}
SECRET_URL_RE = re.compile(
    r"(?i)[?&](?:token|access_token|api[_-]?key|secret|password)="
)


TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HA Config Sync — Import</title>
<style>
:root { color-scheme: light dark; }
body { font-family: system-ui,-apple-system,sans-serif; margin:0; background:#f4f6f8; color:#202124; }
main { max-width:1400px; margin:auto; padding:24px; }
header { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:20px; }
h1 { margin:0 0 4px; }
button { padding:10px 18px; border:0; border-radius:8px; cursor:pointer; background:#03a9f4; color:white; font-weight:650; }
button[disabled] { opacity:.45; cursor:not-allowed; }
.card { background:white; border-radius:12px; padding:18px; margin-bottom:16px; box-shadow:0 1px 4px #0002; }
.meta { display:grid; grid-template-columns:max-content 1fr; gap:5px 12px; }
.small { color:#687078; font-size:13px; }
.status { display:inline-block; padding:4px 9px; border-radius:12px; font-size:12px; font-weight:750; margin-left:7px; }
.same { background:#e8eaed; }
.ready { background:#d8f3dc; color:#165a2e; }
.changed { background:#fff0c2; color:#624b00; }
.conflict,.unsafe,.error { background:#ffd6d6; color:#7a1717; }
.result { padding:12px 14px; border-radius:8px; margin-bottom:10px; background:#d8f3dc; }
.result.bad { background:#ffd6d6; }
.reason { padding:10px 12px; border-radius:8px; background:#fff4d6; }
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
.counts { margin-left:auto; font-size:13px; color:#687078; }
.actions { position:sticky; bottom:0; display:flex; justify-content:flex-end; padding-top:12px; }
.apply-progress { display:none; align-items:center; gap:9px; margin-right:12px; padding:9px 12px; border-radius:8px; background:#e3f2fd; color:#174f78; font-weight:650; }
.apply-progress.visible { display:flex; }
.spinner { width:16px; height:16px; border:3px solid #8bcdf1; border-top-color:#0277bd; border-radius:50%; animation:spin .75s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-color-scheme:dark) {
  body { background:#111418; color:#e8eaed; }
  .card { background:#202428; }
  .small,.counts { color:#aab0b6; }
  table.diff { background:#171a1e; color:#e8eaed; }
  .diff th,.ln { background:#252a2f; }
  .diff td { border-color:#30353a; }
  .left-del { background:#57272a; } .right-add { background:#1f5130; } .blank { background:#202428; }
}
</style>
</head>
<body><main>
<header>
  <div><h1>HA Config Sync — Import</h1><div class="small">One GitHub file per dashboard · GitHub read-only · Apply via Home Assistant API</div></div>
  <form method="get"><button id="refresh-button" type="submit">Refresh GitHub</button></form>
</header>

{% if error %}<div class="card error"><strong>Error:</strong> {{ error }}</div>{% else %}
<div class="card meta">
  <strong>Repository:</strong><span>BartoszWu/home-assistant-config</span>
  <strong>Branch:</strong><span>{{ branch }}</span>
  <strong>Commit:</strong><span>{{ commit }}</span>
  <strong>Base state:</strong><span>state/dashboard-bases.json (SHA-256 only)</span>
</div>

{% for result in results or [] %}
<div class="result {% if not result.ok %}bad{% endif %}">{{ result.message }}</div>
{% endfor %}

<form id="apply-form" method="post" action="apply">
{% if not changes %}<div class="card"><h2>No dashboard files</h2><p>Add JSON files directly under <code>dashboards/</code>.</p></div>{% endif %}
{% for change in changes %}
<section class="card">
  <div class="change-head">
    {% if change.selectable %}<input type="checkbox" name="selected" value="{{ change.relative }}" aria-label="Select {{ change.name }}">{% endif %}
    <h3 style="margin:0">{{ change.name }} <span class="status {{ change.css }}">{{ change.status }}</span></h3>
    <span class="counts">{{ change.added }} added · {{ change.removed }} removed</span>
  </div>
  <div class="small">Dashboard · dashboards/{{ change.relative }}</div>
  {% if change.reason %}<p class="reason">{{ change.reason }}</p>{% endif %}
  <details {% if change.status != 'SAME' %}open{% endif %}>
    <summary>Side-by-side diff</summary>
    <div class="diff-wrap"><table class="diff">
      <thead><tr><th colspan="2">HA current</th><th colspan="2">GitHub HEAD</th></tr></thead>
      <tbody>{% for row in change.rows %}<tr>
        <td class="ln {{ row.left_css }}">{{ row.left_no or '' }}</td><td class="code {{ row.left_css }}">{{ row.left }}</td>
        <td class="ln {{ row.right_css }}">{{ row.right_no or '' }}</td><td class="code {{ row.right_css }}">{{ row.right }}</td>
      </tr>{% endfor %}</tbody>
    </table></div>
  </details>
</section>
{% endfor %}
<div class="actions">
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
    const selected = form.querySelectorAll('input[name="selected"]:checked');
    if (!selected.length) {
      window.alert('Select at least one READY TO APPLY dashboard.');
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


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


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


def unsafe_reason(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in SENSITIVE_KEYS and child not in (None, "", False):
                return f"Sensitive field detected: {path}.{key}"
            reason = unsafe_reason(child, f"{path}.{key}")
            if reason:
                return reason
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reason = unsafe_reason(child, f"{path}[{index}]")
            if reason:
                return reason
    elif isinstance(value, str) and SECRET_URL_RE.search(value):
        return f"Credential-like URL detected at {path}"
    return None


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


def pretty_lines(value):
    return json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ).splitlines()


def side_by_side(current, github):
    left = pretty_lines(current)
    right = pretty_lines(github)
    matcher = difflib.SequenceMatcher(a=left, b=right)
    rows = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_part = left[i1:i2]
        right_part = right[j1:j2]
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


def classify(relative, github, current, base):
    github_hash = digest(github)
    current_hash = digest(current)
    reason = unsafe_reason(github)
    if reason:
        return "UNSAFE", "unsafe", False, reason
    if github_hash == current_hash:
        return "SAME", "same", False, "GitHub HEAD matches HA current."
    if base is None:
        return (
            "CONFLICT", "conflict", False,
            "No exported base hash exists and GitHub differs from HA. Run HA Config Sync — Export only after resolving this manually.",
        )
    github_changed = github_hash != base
    ha_changed = current_hash != base
    if github_changed and not ha_changed:
        return (
            "READY TO APPLY", "ready", True,
            "GitHub changed while HA still matches the last exported base.",
        )
    if not github_changed and ha_changed:
        return (
            "CHANGED IN HA", "changed", False,
            "HA changed locally while GitHub still matches the base. Run HA Config Sync — Export to publish the HA change.",
        )
    return (
        "CONFLICT", "conflict", False,
        "Both GitHub and HA changed from the last exported base. Nothing can be applied automatically.",
    )


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
        status, css, selectable, reason = classify(relative, github, current, base)
        rows, added, removed = side_by_side(current, github)
        changes.append({
            "name": github_path.stem,
            "relative": relative,
            "github": github,
            "current": current,
            "base": base,
            "status": status,
            "css": css,
            "selectable": selectable,
            "reason": reason,
            "rows": rows,
            "added": added,
            "removed": removed,
        })
    return changes


def render_review(results=None):
    error = None
    commit = "-"
    changes = []
    with REPO_LOCK:
        try:
            commit = refresh_repo()
            changes = collect_changes()
        except Exception as exception:
            error = str(exception)
    return render_template_string(
        TEMPLATE,
        changes=changes,
        error=error,
        commit=commit,
        branch=BRANCH,
        has_ready=any(change["selectable"] for change in changes),
        results=results or [],
    )


@app.route("/")
def index():
    return render_review()


@app.route("/apply", methods=["POST"])
def apply_selected():
    selected = request.form.getlist("selected")
    if not selected:
        return render_review([{"ok": False, "message": "No READY dashboard selected."}])
    if len(selected) > 20 or any(not valid_relative(value) for value in selected):
        abort(400)

    results = []
    with REPO_LOCK:
        try:
            refresh_repo()
            fresh = {change["relative"]: change for change in collect_changes()}
            for relative in selected:
                change = fresh.get(relative)
                if not change or change["status"] != "READY TO APPLY":
                    status = change["status"] if change else "missing"
                    results.append({
                        "ok": False,
                        "message": f"{relative}: blocked by fresh conflict-check ({status}).",
                    })
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
        except Exception as exception:
            results.append({"ok": False, "message": f"Apply failed: {exception}"})
    return render_review(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
