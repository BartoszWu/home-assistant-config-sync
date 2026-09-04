"""Prepare optional, session-local native frontend previews. No HA writes."""

import copy
import re
from urllib.parse import quote


UNAVAILABLE = "Visual preview unavailable. YAML diff is still available."


def preview_config(config):
    """Replace custom *cards* in a deep copy, never in the Apply/diff input.

    Only traverse known card slots. Custom views, badges and entity rows cannot
    accept a native button, so supported_config still rejects those afterward.
    """
    result = copy.deepcopy(config)
    omitted = []

    def card_preview(card):
        if not isinstance(card, dict):
            return card
        card_type = str(card.get("type", ""))
        if card_type.startswith("custom:"):
            label = card_type if re.fullmatch(r"custom:[\w-]{1,100}", card_type) else "custom card"
            omitted.append(label)
            placeholder = {
                "type": "button",
                "name": f"Preview omitted: {label}",
                "icon": "mdi:puzzle-outline",
                "show_name": True,
                "show_icon": True,
                "tap_action": {"action": "none"},
                "hold_action": {"action": "none"},
                "double_tap_action": {"action": "none"},
            }
            # Preserve supported section dimensions, not card-specific behavior,
            # content, entities, styles, URLs or actions. Heights remain approximate.
            grid = card.get("grid_options")
            if isinstance(grid, dict):
                dimensions = {
                    key: value for key, value in grid.items()
                    if key in {"columns", "rows"}
                    and ((type(value) is int and 0 < value <= 100)
                         or (isinstance(value, str) and value in {"full", "auto"}))
                }
                if dimensions:
                    placeholder["grid_options"] = dimensions
            if isinstance(card.get("visibility"), list):
                placeholder["visibility"] = copy.deepcopy(card["visibility"])
            return placeholder
        if isinstance(card.get("cards"), list):
            card["cards"] = [card_preview(child) for child in card["cards"]]
        if isinstance(card.get("card"), dict):
            card["card"] = card_preview(card["card"])
        return card

    for view in result["views"]:
        if isinstance(view.get("cards"), list):
            view["cards"] = [card_preview(card) for card in view["cards"]]
        for section in view.get("sections", []):
            if isinstance(section.get("cards"), list):
                section["cards"] = [card_preview(card) for card in section["cards"]]
    return result, omitted


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
        configs = []
        placeholders = []
        for config in (current, desired):
            if (
                not isinstance(config, dict)
                or not isinstance(config.get("views"), list)
                or not config["views"]
                or unsafe_reason(config)
            ):
                return {"error": UNAVAILABLE}
            prepared, omitted = preview_config(config)
            if not supported_config(prepared):
                return {"error": UNAVAILABLE}
            configs.append(prepared)
            placeholders.append(omitted)
        stem = relative[:-5]
        url_path = "lovelace" if stem in {"lovelace", "dashboard-lovelace"} else stem
        return {
            "path": "/" + quote(url_path, safe="") + "/0",
            "before": configs[0],
            "after": configs[1],
            "placeholder_counts": {"before": len(placeholders[0]), "after": len(placeholders[1])},
            "placeholder_types": sorted(set(placeholders[0] + placeholders[1])),
        }
    except Exception:
        return {"error": UNAVAILABLE}
