import json
import os
import re
from pathlib import Path

from websockets.sync.client import connect


WS_URL = "ws://supervisor/core/websocket"
OUTPUT_DIR = Path("/export")

MAC_RE = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
)

HEX_MAC_RE = re.compile(
    r"(?i)\b[0-9a-f]{12}\b"
)

IPV4_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)


def safe_text(value):
    """Sanitize human-readable text without modifying entity_id."""
    if not isinstance(value, str):
        return value

    value = value.strip()
    value = MAC_RE.sub("[redacted-mac]", value)
    value = HEX_MAC_RE.sub("[redacted-id]", value)
    value = IPV4_RE.sub("[redacted-ip]", value)

    return value


def md(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def ws_call(ws, message_id, message_type):
    ws.send(
        json.dumps(
            {
                "id": message_id,
                "type": message_type,
            }
        )
    )

    while True:
        response = json.loads(ws.recv())

        if response.get("id") != message_id:
            continue

        if response.get("success") is not True:
            raise RuntimeError(
                f"{message_type} failed: {response.get('error')}"
            )

        return response["result"]


token = os.environ.get("SUPERVISOR_TOKEN")

if not token:
    raise RuntimeError("SUPERVISOR_TOKEN is missing")


print("Connecting to Home Assistant WebSocket API...")

with connect(WS_URL, open_timeout=15) as ws:

    hello = json.loads(ws.recv())

    if hello.get("type") != "auth_required":
        raise RuntimeError(
            f"Unexpected WebSocket response: {hello.get('type')}"
        )

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
        raise RuntimeError("Home Assistant authentication failed")

    print("Authenticated.")

    entities = ws_call(
        ws,
        1,
        "config/entity_registry/list",
    )

    devices = ws_call(
        ws,
        2,
        "config/device_registry/list",
    )

    areas = ws_call(
        ws,
        3,
        "config/area_registry/list",
    )

    floors = ws_call(
        ws,
        4,
        "config/floor_registry/list",
    )


devices_by_id = {
    device["id"]: device
    for device in devices
}

areas_by_id = {
    area["area_id"]: area
    for area in areas
}

floors_by_id = {
    floor["floor_id"]: floor
    for floor in floors
}


safe_entities = []

for entity in entities:

    entity_id = entity["entity_id"]

    device = devices_by_id.get(
        entity.get("device_id"),
        {},
    )

    area_id = (
        entity.get("area_id")
        or device.get("area_id")
    )

    area = areas_by_id.get(
        area_id,
        {},
    )

    floor = floors_by_id.get(
        area.get("floor_id"),
        {},
    )

    record = {
        # entity_id pozostaje dokładny.
        # To właśnie jest identyfikator potrzebny LLM.
        "entity_id": entity_id,

        "name": safe_text(
            entity.get("name")
            or entity.get("original_name")
        ),

        "domain": entity_id.split(".", 1)[0],

        "area": safe_text(
            area.get("name")
        ),

        "floor": safe_text(
            floor.get("name")
        ),

        "device_class": safe_text(
            entity.get("device_class")
            or entity.get("original_device_class")
        ),

        "disabled": (
            entity.get("disabled_by") is not None
        ),

        "device_name": safe_text(
            device.get("name_by_user")
            or device.get("name")
        ),

        "manufacturer": safe_text(
            device.get("manufacturer")
        ),

        "model": safe_text(
            device.get("model")
        ),

        "integration": safe_text(
            entity.get("platform")
        ),
    }

    safe_entities.append(record)


safe_entities.sort(
    key=lambda item: (
        item.get("floor") or "",
        item.get("area") or "",
        item.get("device_name") or "",
        item["entity_id"],
    )
)


#
# DEVICES
#
# Budujemy listę na podstawie prawdziwego Device Registry,
# ale świadomie NIE zapisujemy:
#
# - device_id
# - identifiers
# - connections
# - MAC
# - serial_number
# - config_entry_id
# - sw_version / hw_version
#

entity_integrations_by_device = {}
entity_counts_by_device = {}

for entity in entities:

    device_id = entity.get("device_id")

    if not device_id:
        continue

    entity_counts_by_device[device_id] = (
        entity_counts_by_device.get(device_id, 0) + 1
    )

    platform = entity.get("platform")

    if platform:
        entity_integrations_by_device.setdefault(
            device_id,
            set(),
        ).add(platform)


safe_devices = []

for device in devices:

    area = areas_by_id.get(
        device.get("area_id"),
        {},
    )

    floor = floors_by_id.get(
        area.get("floor_id"),
        {},
    )

    record = {
        "name": safe_text(
            device.get("name_by_user")
            or device.get("name")
        ),

        "manufacturer": safe_text(
            device.get("manufacturer")
        ),

        "model": safe_text(
            device.get("model")
        ),

        "area": safe_text(
            area.get("name")
        ),

        "floor": safe_text(
            floor.get("name")
        ),

        "integrations": sorted(
            entity_integrations_by_device.get(
                device["id"],
                set(),
            )
        ),

        "entity_count": entity_counts_by_device.get(
            device["id"],
            0,
        ),
    }

    safe_devices.append(record)


safe_devices.sort(
    key=lambda item: (
        item.get("floor") or "",
        item.get("area") or "",
        item.get("name") or "",
        item.get("manufacturer") or "",
        item.get("model") or "",
    )
)


#
# Security validation
#
# Eksport jest allowlistą pól.
# Nie kopiujemy surowych obiektów HA.
#

ENTITY_KEYS = {
    "entity_id",
    "name",
    "domain",
    "area",
    "floor",
    "device_class",
    "disabled",
    "device_name",
    "manufacturer",
    "model",
    "integration",
}

DEVICE_KEYS = {
    "name",
    "manufacturer",
    "model",
    "area",
    "floor",
    "integrations",
    "entity_count",
}

for item in safe_entities:
    if set(item.keys()) != ENTITY_KEYS:
        raise RuntimeError(
            "Unexpected key in entity export"
        )

for item in safe_devices:
    if set(item.keys()) != DEVICE_KEYS:
        raise RuntimeError(
            "Unexpected key in device export"
        )


OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


#
# JSON
#
# Celowo NIE zapisujemy generated_at.
# Dzięki temu przyszły git commit powstanie tylko,
# gdy konfiguracja faktycznie się zmieni.
#

payload = {
    "schema_version": 1,

    "source": (
        "Home Assistant registries "
        "via WebSocket API"
    ),

    "counts": {
        "entities": len(safe_entities),
        "devices": len(safe_devices),
        "areas": len(areas),
        "floors": len(floors),
    },

    "entities": safe_entities,
}

json_path = OUTPUT_DIR / "entities.json"

json_path.write_text(
    json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )
    + "\n",
    encoding="utf-8",
)


#
# ENTITIES.md
#

lines = [
    "# Home Assistant — Entities",
    "",
    (
        "> Generated automatically from a sanitized "
        "Home Assistant registry export."
    ),
    "",
    f"Entities: **{len(safe_entities)}**",
    "",
    (
        "| Entity ID | Name | Device | Area | Floor | "
        "Domain | Device class | Manufacturer | Model | "
        "Integration | Disabled |"
    ),
    (
        "|---|---|---|---|---|---|---|---|---|---|---|"
    ),
]

for item in safe_entities:

    lines.append(
        "| "
        + " | ".join(
            [
                f"`{md(item['entity_id'])}`",
                md(item["name"]),
                md(item["device_name"]),
                md(item["area"]),
                md(item["floor"]),
                md(item["domain"]),
                md(item["device_class"]),
                md(item["manufacturer"]),
                md(item["model"]),
                md(item["integration"]),
                (
                    "yes"
                    if item["disabled"]
                    else "no"
                ),
            ]
        )
        + " |"
    )

(OUTPUT_DIR / "ENTITIES.md").write_text(
    "\n".join(lines) + "\n",
    encoding="utf-8",
)


#
# DEVICES.md
#

lines = [
    "# Home Assistant — Devices",
    "",
    (
        "> Generated automatically from a sanitized "
        "Home Assistant registry export."
    ),
    "",
    f"Devices: **{len(safe_devices)}**",
    "",
    (
        "| Device | Manufacturer | Model | Area | Floor | "
        "Integrations | Entities |"
    ),
    (
        "|---|---|---|---|---|---|---:|"
    ),
]

for item in safe_devices:

    lines.append(
        "| "
        + " | ".join(
            [
                md(item["name"]),
                md(item["manufacturer"]),
                md(item["model"]),
                md(item["area"]),
                md(item["floor"]),
                md(", ".join(item["integrations"])),
                str(item["entity_count"]),
            ]
        )
        + " |"
    )

(OUTPUT_DIR / "DEVICES.md").write_text(
    "\n".join(lines) + "\n",
    encoding="utf-8",
)


print("")
print("✅ Sanitized export completed")
print("")
print(f"Entities: {len(safe_entities)}")
print(f"Devices:  {len(safe_devices)}")
print(f"Areas:    {len(areas)}")
print(f"Floors:   {len(floors)}")
print("")
print("Generated:")
print("  /export/entities.json")
print("  /export/ENTITIES.md")
print("  /export/DEVICES.md")
print("")
print("No states or history were requested.")
print("No .storage files were accessed.")
print("No secrets were intentionally exported.")
