import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from websockets.sync.client import connect


WS_URL = "ws://supervisor/core/websocket"
API_BASE = "http://supervisor/core/api"
OUTPUT_DIR = Path("/export/config/storage")

DOMAINS = ("automation", "script", "scene")

from security import unsafe_reason, safe_entity_id


def ws_call(ws, message_id, message_type):
    ws.send(json.dumps({
        "id": message_id,
        "type": message_type,
    }))

    while True:
        result = json.loads(ws.recv(timeout=30))

        if result.get("id") != message_id:
            continue

        if result.get("success") is not True:
            raise RuntimeError(
                f"{message_type} read failed"
            )

        return result["result"]


def safe_filename(entity_id):
    return re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        entity_id,
    ) + ".json"


def api_get(domain, config_key, token):
    url = f"{API_BASE}/config/{domain}/config/{config_key}"

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


token = os.environ.get("SUPERVISOR_TOKEN")

if not token:
    raise RuntimeError("SUPERVISOR_TOKEN is missing")


print("Connecting for managed config export...")

with connect(WS_URL, open_timeout=15) as ws:
    hello = json.loads(ws.recv(timeout=30))

    if hello.get("type") != "auth_required":
        raise RuntimeError("Unexpected WebSocket response")

    ws.send(json.dumps({
        "type": "auth",
        "access_token": token,
    }))

    auth = json.loads(ws.recv(timeout=30))

    if auth.get("type") != "auth_ok":
        raise RuntimeError("Authentication failed")

    entities = ws_call(
        ws,
        1,
        "config/entity_registry/list",
    )


if OUTPUT_DIR.exists():
    shutil.rmtree(OUTPUT_DIR)

for domain in DOMAINS:
    (OUTPUT_DIR / f"{domain}s").mkdir(
        parents=True,
        exist_ok=True,
    )


index = {
    "schema_version": 2,
    "source": "Home Assistant Config API",
    "objects": [],
}


for entity in entities:
    entity_id = safe_entity_id(entity.get("entity_id")) or ""

    if "." not in entity_id:
        continue

    domain = entity_id.split(".", 1)[0]

    if domain not in DOMAINS:
        continue

    config_key = entity.get("unique_id")

    if not config_key:
        index["objects"].append({"domain": domain, "entity_id": entity_id,
            "exported": False, "status": "unsupported"})
        continue

    try:
        config = api_get(
            domain,
            config_key,
            token,
        )

    except urllib.error.HTTPError as err:
        index["objects"].append({"domain": domain, "entity_id": entity_id,
            "exported": False, "status": "unsupported" if err.code in (400, 404) else "read_error"})
        continue
    except Exception:
        index["objects"].append({"domain": domain, "entity_id": entity_id,
            "exported": False, "status": "read_error"})
        continue

    reason = unsafe_reason(config)

    if reason:
        print(
            f"⚠️  {entity_id} NOT exported: {reason}"
        )

        index["objects"].append({
            "domain": domain,
            "entity_id": entity_id,
            "exported": False,
            "status": "security_excluded",
            "reason": reason,
        })

        continue

    folder = f"{domain}s"
    filename = safe_filename(entity_id)

    relative_file = f"{folder}/{filename}"
    path = OUTPUT_DIR / relative_file

    path.write_text(
        json.dumps(
            config,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    index["objects"].append({
        "domain": domain,
        "entity_id": entity_id,
        "exported": True,
        "status": "success",
        "file": relative_file,
    })

    print(
        f"✅ {domain}: {entity_id} -> {relative_file}"
    )


index["objects"].sort(
    key=lambda item: (
        item["domain"],
        item["entity_id"],
    )
)

(OUTPUT_DIR / "index.json").write_text(
    json.dumps(
        index,
        ensure_ascii=False,
        indent=2,
    ) + "\n",
    encoding="utf-8",
)

exported = sum(
    1 for item in index["objects"]
    if item["exported"]
)

skipped = len(index["objects"]) - exported

print("")
print("✅ Managed config export completed")
print(f"Exported: {exported}")
print(f"Skipped for security: {skipped}")
print(f"Output: {OUTPUT_DIR}")
