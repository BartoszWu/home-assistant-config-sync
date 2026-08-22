import json
import os
import re
import shutil
from pathlib import Path

from websockets.sync.client import connect


WS_URL = "ws://supervisor/core/websocket"
OUTPUT_DIR = Path("/tmp/ha-current/dashboards")

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "client_secret",
    "secret",
    "authorization",
    "credential",
    "credentials",
    "local_key",
    "bindkey",
    "private_key",
}

SECRET_IN_URL_RE = re.compile(
    r"(?i)[?&](?:token|access_token|api[_-]?key|secret|password)="
)

PRIVATE_URL_RE = re.compile(
    r"(?i)https?://(?:"
    r"10\."
    r"|192\.168\."
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\."
    r")"
)


def ws_call(ws, message_id, message_type, **extra):
    message = {
        "id": message_id,
        "type": message_type,
        **extra,
    }

    ws.send(json.dumps(message))

    while True:
        response = json.loads(ws.recv())

        if response.get("id") != message_id:
            continue

        if response.get("success") is not True:
            raise RuntimeError(
                f"{message_type} failed: {response.get('error')}"
            )

        return response["result"]


def unsafe_reason(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")

            if normalized in SENSITIVE_KEYS:
                if child not in (None, "", False):
                    return f"sensitive field: {path}.{key}"

            result = unsafe_reason(
                child,
                f"{path}.{key}",
            )

            if result:
                return result

    elif isinstance(value, list):
        for index, child in enumerate(value):
            result = unsafe_reason(
                child,
                f"{path}[{index}]",
            )

            if result:
                return result

    elif isinstance(value, str):
        if SECRET_IN_URL_RE.search(value):
            return f"credential-like URL at {path}"

        if PRIVATE_URL_RE.search(value):
            return f"private network URL at {path}"

    return None


def filename_for(url_path):
    value = url_path or "lovelace"

    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    ).strip("._")

    return (value or "lovelace") + ".json"


token = os.environ.get("SUPERVISOR_TOKEN")

if not token:
    raise RuntimeError("SUPERVISOR_TOKEN is missing")


print("Connecting for dashboard export...")

with connect(WS_URL, open_timeout=15) as ws:

    hello = json.loads(ws.recv())

    if hello.get("type") != "auth_required":
        raise RuntimeError("Unexpected WebSocket response")

    ws.send(
        json.dumps(
            {
                "type": "auth",
                "access_token": token,
            }
        )
    )

    auth = json.loads(ws.recv())

    if auth.get("type") != "auth_ok":
        raise RuntimeError("Authentication failed")

    dashboards = ws_call(
        ws,
        1,
        "lovelace/dashboards/list",
    )

    # Dashboardy utworzone przez UI/storage mają własne id.
    storage_dashboards = [
        dashboard
        for dashboard in dashboards
        if dashboard.get("id")
    ]

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    index = {
        "schema_version": 1,
        "source": "Home Assistant Lovelace WebSocket API",
        "dashboards": [],
    }

    message_id = 2

    for dashboard in storage_dashboards:

        url_path = dashboard.get("url_path")
        title = dashboard.get("title") or url_path or "Lovelace"

        try:
            config = ws_call(
                ws,
                message_id,
                "lovelace/config",
                url_path=url_path,
                force=False,
            )

            message_id += 1

        except Exception as err:
            print(
                f"⚠️  Dashboard '{title}' skipped: "
                f"cannot read config: {err}"
            )

            index["dashboards"].append(
                {
                    "url_path": url_path,
                    "title": title,
                    "exported": False,
                    "reason": "config read failed",
                }
            )

            continue

        reason = unsafe_reason(config)

        if reason:
            print(
                f"⚠️  Dashboard '{title}' NOT exported: "
                f"{reason}"
            )

            index["dashboards"].append(
                {
                    "url_path": url_path,
                    "title": title,
                    "exported": False,
                    "reason": reason,
                }
            )

            continue

        filename = filename_for(url_path)

        (OUTPUT_DIR / filename).write_text(
            json.dumps(
                config,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        index["dashboards"].append(
            {
                "url_path": url_path,
                "title": title,
                "icon": dashboard.get("icon"),
                "show_in_sidebar": dashboard.get(
                    "show_in_sidebar"
                ),
                "require_admin": dashboard.get(
                    "require_admin"
                ),
                "exported": True,
                "file": filename,
            }
        )

        print(
            f"✅ Dashboard: {title} -> {filename}"
        )


(OUTPUT_DIR / "index.json").write_text(
    json.dumps(
        index,
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)

exported = sum(
    1
    for dashboard in index["dashboards"]
    if dashboard["exported"]
)

skipped = len(index["dashboards"]) - exported

print("")
print("✅ Dashboard export completed")
print(f"Storage dashboards found: {len(storage_dashboards)}")
print(f"Exported: {exported}")
print(f"Skipped:  {skipped}")
print(f"Output:   {OUTPUT_DIR}")
