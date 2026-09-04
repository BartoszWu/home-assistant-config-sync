"""Prepare optional, session-local native frontend previews. No HA writes."""

import re
from urllib.parse import quote


UNAVAILABLE = "Visual preview unavailable. YAML diff is still available."


def supported_config(value):
    if isinstance(value, dict):
        if "strategy" in value:
            return False
        if str(value.get("type", "")).startswith("custom:"):
            return False
        return all(supported_config(child) for child in value.values())
    if isinstance(value, list):
        return all(supported_config(child) for child in value)
    return True


def prepare_preview(relative, current, desired, unsafe_reason):
    """Never let optional preview validation break collection or Apply."""
    if not re.fullmatch(r"[\w-]+\.json", relative):
        return None
    try:
        for config in (current, desired):
            if (
                not isinstance(config, dict)
                or not isinstance(config.get("views"), list)
                or not config["views"]
                or unsafe_reason(config)
                or not supported_config(config)
            ):
                return {"error": UNAVAILABLE}
        stem = relative[:-5]
        url_path = "lovelace" if stem in {"lovelace", "dashboard-lovelace"} else stem
        return {
            "path": "/" + quote(url_path, safe="") + "/0",
            "before": current,
            "after": desired,
        }
    except Exception:
        return {"error": UNAVAILABLE}
