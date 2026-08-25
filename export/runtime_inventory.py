import re


RUNTIME_INTEGRATIONS = frozenset({
    "home_connect",
    "lg_thinq",
})

COMMON_ATTRIBUTE_KEYS = frozenset({
    "device_class",
    "event_type",
    "event_types",
    "friendly_name",
    "icon",
    "options",
    "state_class",
    "unit_of_measurement",
})

INTEGRATION_ATTRIBUTE_KEYS = {
    "home_connect": COMMON_ATTRIBUTE_KEYS | frozenset({
        "door_state",
        "finish_time",
        "operation_state",
        "program",
        "program_progress",
        "remaining_program_time",
    }),
    "lg_thinq": COMMON_ATTRIBUTE_KEYS | frozenset({
        "current_status",
        "energy_today",
        "error",
        "remaining_time",
        "total_time",
    }),
}

LIST_ATTRIBUTE_KEYS = frozenset({
    "event_types",
    "options",
})

FLATTENED_ATTRIBUTE_KEYS = frozenset({
    "device_class",
    "event_type",
    "event_types",
    "friendly_name",
    "icon",
    "options",
    "state_class",
    "unit_of_measurement",
})

MAX_TEXT_LENGTH = 256
MAX_LIST_ITEMS = 128

MAC_RE = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"
)

HEX_MAC_RE = re.compile(
    r"(?i)\b[0-9a-f]{12}\b"
)

IPV4_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)

SECRET_URL_RE = re.compile(
    r"(?i)[?&](?:token|access_token|api[_-]?key|secret|password)="
)

ASSIGNED_SECRET_RE = re.compile(
    r"(?i)\b(?:password|passwd|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]"
)

BEARER_RE = re.compile(
    r"(?i)^bearer\s+[A-Za-z0-9._-]{20,}$"
)

CREDENTIAL_RE = re.compile(
    r"(?:"
    r"github_pat_[A-Za-z0-9_]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)

TOKEN_LIKE_RE = re.compile(
    r"^(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_-]{48,}|eyJ[A-Za-z0-9._-]{20,})$"
)

MDI_ICON_RE = re.compile(
    r"^mdi:[a-z0-9-]+$"
)

REGISTRY_ID_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,64}$"
)


def safe_runtime_text(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value

    if not isinstance(value, str):
        return None

    value = value.strip()

    if len(value) > MAX_TEXT_LENGTH:
        return "[redacted-long-value]"

    if (
        SECRET_URL_RE.search(value)
        or ASSIGNED_SECRET_RE.search(value)
        or BEARER_RE.fullmatch(value)
        or CREDENTIAL_RE.search(value)
        or TOKEN_LIKE_RE.fullmatch(value)
    ):
        return "[redacted-secret]"

    value = MAC_RE.sub("[redacted-mac]", value)
    value = HEX_MAC_RE.sub("[redacted-id]", value)
    value = IPV4_RE.sub("[redacted-ip]", value)

    return value


def safe_icon(value):
    value = safe_runtime_text(value)

    if isinstance(value, str) and MDI_ICON_RE.fullmatch(value):
        return value

    return None


def safe_registry_id(value):
    if isinstance(value, str) and REGISTRY_ID_RE.fullmatch(value):
        return value

    return None


def safe_runtime_attributes(integration, attributes):
    if not isinstance(attributes, dict):
        return {}

    allowed_keys = INTEGRATION_ATTRIBUTE_KEYS.get(
        integration,
        frozenset(),
    )
    result = {}

    for key in sorted(allowed_keys):
        if key not in attributes:
            continue

        value = attributes[key]

        if key == "icon":
            icon = safe_icon(value)

            if icon is not None:
                result[key] = icon

            continue

        if key in LIST_ATTRIBUTE_KEYS:
            if not isinstance(value, (list, tuple)):
                continue

            safe_items = []

            for item in value[:MAX_LIST_ITEMS]:
                safe_item = safe_runtime_text(item)

                if isinstance(safe_item, str):
                    safe_items.append(safe_item)

            result[key] = safe_items
            continue

        safe_value = safe_runtime_text(value)

        if safe_value is not None:
            result[key] = safe_value

    return result


def runtime_status(disabled_by, loaded, raw_state):
    if disabled_by is not None:
        return "disabled"

    if not loaded or raw_state == "unavailable":
        return "unavailable"

    if raw_state == "unknown":
        return "unknown"

    return "available"


def build_runtime_inventory(
    entities,
    states,
    devices_by_id=None,
    areas_by_id=None,
):
    devices_by_id = devices_by_id or {}
    areas_by_id = areas_by_id or {}
    states_by_entity_id = {
        item.get("entity_id"): item
        for item in states
        if isinstance(item, dict) and item.get("entity_id")
    }

    records = []

    for entity in entities:
        if not isinstance(entity, dict):
            continue

        integration = entity.get("platform")

        if integration not in RUNTIME_INTEGRATIONS:
            continue

        entity_id = entity.get("entity_id")

        if not entity_id:
            continue

        device_id = entity.get("device_id")
        device = devices_by_id.get(device_id, {})
        area_id = entity.get("area_id") or device.get("area_id")
        area = areas_by_id.get(area_id, {})
        disabled_by = safe_runtime_text(entity.get("disabled_by"))
        current = (
            None
            if disabled_by is not None
            else states_by_entity_id.get(entity_id)
        )
        loaded = current is not None
        raw_state = current.get("state") if loaded else None
        raw_attributes = current.get("attributes", {}) if loaded else {}
        attributes = safe_runtime_attributes(
            integration,
            raw_attributes,
        )
        status = runtime_status(
            disabled_by,
            loaded,
            raw_state,
        )
        icon = attributes.get("icon") or safe_icon(
            entity.get("icon") or entity.get("original_icon")
        )

        integration_attributes = {
            key: value
            for key, value in attributes.items()
            if key not in FLATTENED_ATTRIBUTE_KEYS
        }

        records.append({
            "record_type": "entity_runtime_state",
            "source": "home_assistant_websocket",
            "entity_id": entity_id,
            "integration": integration,
            "friendly_name": (
                attributes.get("friendly_name")
                or safe_runtime_text(
                    entity.get("name")
                    or entity.get("original_name")
                )
            ),
            "device": safe_runtime_text(
                device.get("name_by_user")
                or device.get("name")
            ),
            "device_id": safe_registry_id(device_id),
            "area": safe_runtime_text(area.get("name")),
            "enabled": disabled_by is None,
            "disabled_by": disabled_by,
            "loaded": loaded,
            "status": status,
            "state": safe_runtime_text(raw_state),
            "device_class": (
                attributes.get("device_class")
                or safe_runtime_text(
                    entity.get("device_class")
                    or entity.get("original_device_class")
                )
            ),
            "unit_of_measurement": attributes.get("unit_of_measurement"),
            "state_class": attributes.get("state_class"),
            "icon": icon,
            "options": attributes.get("options", []),
            "event_type": attributes.get("event_type"),
            "event_types": attributes.get("event_types", []),
            "last_changed": safe_runtime_text(
                current.get("last_changed") if loaded else None
            ),
            "last_updated": safe_runtime_text(
                current.get("last_updated") if loaded else None
            ),
            "attributes": integration_attributes,
        })

    records.sort(key=lambda item: item["entity_id"])

    return {
        "schema_version": 2,
        "record_type": "current_state_snapshot",
        "source": "home_assistant_websocket",
        "integrations": sorted(RUNTIME_INTEGRATIONS),
        "states": records,
    }
