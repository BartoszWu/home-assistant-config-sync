import json
import os
from pathlib import Path

from websockets.sync.client import connect

from runtime_inventory import build_runtime_inventory


WS_URL = "ws://supervisor/core/websocket"
OUTPUT_DIR = Path("/export")

from inventory import build_inventory, render_markdown, md


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
        response = json.loads(ws.recv(timeout=30))

        if response.get("id") != message_id:
            continue

        if response.get("success") is not True:
            raise RuntimeError(
                f"{message_type} read failed"
            )

        return response["result"]


token = os.environ.get("SUPERVISOR_TOKEN")

if not token:
    raise RuntimeError("SUPERVISOR_TOKEN is missing")


print("Connecting to Home Assistant WebSocket API...")

with connect(WS_URL, open_timeout=15) as ws:

    hello = json.loads(ws.recv(timeout=30))

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

    auth = json.loads(ws.recv(timeout=30))

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

    states = ws_call(
        ws,
        5,
        "get_states",
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


payload, observations = build_inventory(entities, devices, areas, floors, states)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "metadata-observations.json").write_text(
    json.dumps(observations, sort_keys=True), encoding="utf-8")
(OUTPUT_DIR / "entities.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
# Render the persisted canonical representation, never parallel raw registry data.
canonical = json.loads((OUTPUT_DIR / "entities.json").read_text(encoding="utf-8"))
for filename, content in render_markdown(canonical).items():
    (OUTPUT_DIR / filename).write_text(content, encoding="utf-8")


#
# Sanitized current state snapshot for integrations needed by household
# dashboards. Raw attributes and history are never exported.
#

runtime_payload = build_runtime_inventory(
    entities,
    states,
    devices_by_id,
    areas_by_id,
)

(OUTPUT_DIR / "states.json").write_text(
    json.dumps(
        runtime_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )
    + "\n",
    encoding="utf-8",
)


#
# STATES.md
#

runtime_lines = [
    "# Home Assistant — Current appliance states",
    "",
    (
        "> Generated automatically from a sanitized current-state "
        "snapshot for Home Connect and LG ThinQ."
    ),
    "",
    f"States: **{len(runtime_payload['states'])}**",
    "",
    (
        "| Entity ID | Domain | Device | Area | Integration | Status | State | "
        "Device class | Unit | Options | Last updated |"
    ),
    "|---|---|---|---|---|---|---|---|---|---|---|",
]

for item in runtime_payload["states"]:
    options = item["options"] or item["event_types"]

    runtime_lines.append(
        "| "
        + " | ".join(
            [
                f"`{md(item['entity_id'])}`",
                md(item["domain"]),
                md(item["device_name"]),
                md(item["area"]),
                md(item["integration"]),
                md(item["status"]),
                md(item["state"]),
                md(item["device_class"]),
                md(item["unit_of_measurement"]),
                md(", ".join(str(value) for value in options)),
                md(item["last_updated"]),
            ]
        )
        + " |"
    )

(OUTPUT_DIR / "STATES.md").write_text(
    "\n".join(runtime_lines) + "\n",
    encoding="utf-8",
)


print("Sanitized inventory export completed")
for section, count in payload["counts"].items():
    print(f"{section}: {count}")
