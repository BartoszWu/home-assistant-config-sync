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
    "webhook_id",
}

SECRET_URL_RE = re.compile(
    r"(?i)[?&](?:token|access_token|api[_-]?key|secret|password)="
)

PRIVATE_URL_RE = re.compile(
    r"(?i)https?://(?:"
    r"10\."
    r"|192\.168\."
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\."
    r")"
)


def ws_call(ws, message_id, message_type):
    ws.send(json.dumps({
        "id": message_id,
        "type": message_type,
    }))

    while True:
        result = json.loads(ws.recv())

        if result.get("id") != message_id:
            continue

        if result.get("success") is not True:
            raise RuntimeError(
                f"{message_type} failed: {result.get('error')}"
            )

        return result["result"]


def unsafe_reason(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")

            if normalized in SENSITIVE_KEYS and child not in (None, "", False):
                return f"sensitive field: {path}.{key}"

            result = unsafe_reason(child, f"{path}.{key}")

            if result:
                return result

    elif isinstance(value, list):
        for index, child in enumerate(value):
            result = unsafe_reason(child, f"{path}[{index}]")

            if result:
                return result

    elif isinstance(value, str):
        if SECRET_URL_RE.search(value):
            return f"credential-like URL at {path}"

        if PRIVATE_URL_RE.search(value):
            return f"private network URL at {path}"

    return None


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
    hello = json.loads(ws.recv())

    if hello.get("type") != "auth_required":
        raise RuntimeError("Unexpected WebSocket response")

    ws.send(json.dumps({
        "type": "auth",
        "access_token": token,
    }))

    auth = json.loads(ws.recv())

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
    "schema_version": 1,
    "source": "Home Assistant Config API",
    "objects": [],
}


for entity in entities:
    entity_id = entity.get("entity_id", "")

    if "." not in entity_id:
        continue

    domain = entity_id.split(".", 1)[0]

    if domain not in DOMAINS:
        continue

    config_key = entity.get("unique_id")

    if not config_key:
        continue

    try:
        config = api_get(
            domain,
            config_key,
            token,
        )

    except urllib.error.HTTPError as err:
        # Np. scene dostarczona przez Hue, a nie scenes.yaml.
        if err.code in (400, 404):
            continue

        raise

    except Exception as err:
        print(
            f"⚠️  {entity_id} skipped: "
            f"read failed: {err}"
        )
        continue

    reason = unsafe_reason(config)

    if reason:
        print(
            f"⚠️  {entity_id} NOT exported: {reason}"
        )

        index["objects"].append({
            "domain": domain,
            "entity_id": entity_id,
            "config_key": config_key,
            "exported": False,
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
        "config_key": config_key,
        "exported": True,
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
