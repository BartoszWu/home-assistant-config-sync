import json
import os
import re
import shutil
from pathlib import Path

from websockets.sync.client import connect


WS_URL = "ws://supervisor/core/websocket"
OUTPUT_DIR = Path("/tmp/ha-current/dashboards")

from security import unsafe_reason, safe_text
from dashboard_manifest import resource_record, custom_dependencies


def ws_call(ws, message_id, message_type, **extra):
    message = {
        "id": message_id,
        "type": message_type,
        **extra,
    }

    ws.send(json.dumps(message))

    while True:
        response = json.loads(ws.recv(timeout=30))

        if response.get("id") != message_id:
            continue

        if response.get("success") is not True:
            raise RuntimeError(
                f"{message_type} read failed"
            )

        return response["result"]


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

    hello = json.loads(ws.recv(timeout=30))

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

    auth = json.loads(ws.recv(timeout=30))

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
        "schema_version": 2,
        "source": "Home Assistant Lovelace WebSocket API",
        "dashboards": [],
        "scope_exclusions": [{"scope": "non_storage_dashboards", "status": "intentionally_excluded",
                              "reason": "Built-in, default and YAML configurations are outside Export scope; the API list may omit them."}],
    }

    for dashboard in dashboards:
        if not dashboard.get("id"):
            index["dashboards"].append({
                "url_path": safe_text(dashboard.get("url_path")),
                "title": safe_text(dashboard.get("title")),
                "exported": False, "status": "intentionally_excluded",
                "reason": "outside storage dashboard scope"})
    try:
        resources = ws_call(ws, 2, "lovelace/resources")
        index["resources"] = []
        index["resources"] = [resource_record(resource) for resource in resources]
        index["resources"].sort(key=lambda x: x.get("url", ""))
        index["resources_status"] = "security_excluded" if any(x["status"] != "success" for x in index["resources"]) else "success"
    except Exception:
        index["resources_status"] = "read_error"
    message_id = 2

    for dashboard in storage_dashboards:

        url_path = dashboard.get("url_path")
        title = safe_text(dashboard.get("title") or url_path or "Lovelace")
        if not isinstance(url_path, str) or not re.fullmatch(r"[a-z0-9_-]+", url_path) or unsafe_reason(url_path):
            index["dashboards"].append({"exported": False, "status": "security_excluded"})
            continue

        message_id += 1
        try:
            config = ws_call(
                ws,
                message_id,
                "lovelace/config",
                url_path=url_path,
                force=False,
            )


        except Exception:
            print(
                f"⚠️  Dashboard '{title}' skipped: "
                "cannot read config"
            )

            index["dashboards"].append(
                {
                    "url_path": url_path,
                    "title": title,
                    "exported": False,
                    "reason": "config read failed",
                    "status": "read_error",
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
                    "status": "security_excluded",
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
                "icon": safe_text(dashboard.get("icon")),
                "show_in_sidebar": dashboard.get("show_in_sidebar") is True,
                "require_admin": dashboard.get("require_admin") is True,
                "exported": True,
                "status": "success",
                "mode": "storage",
                "strategy": safe_text(config.get("strategy", {}).get("type")),
                "views": [{key: safe_text(view.get(key)) for key in ("title", "path", "type", "icon")}
                          for view in config.get("views", []) if isinstance(view, dict)],
                "custom_dependencies": custom_dependencies(config),
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
