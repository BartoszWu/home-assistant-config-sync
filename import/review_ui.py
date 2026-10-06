"""Presentation of the Import review; never authorizes or executes Apply."""

from pathlib import Path, PurePosixPath

from diff_view import is_changed_review

STATUS_LABELS = {
    "SAME": "Bez zmian",
    "OK": "Bez zmian",
    "IN SYNC — BASE NOT INITIALIZED": "Potrzebna baza",
    "READY TO APPLY": "Gotowe",
    "READY TO APPLY — NO BASE": "Nowa konfiguracja",
    "READY TO APPLY — NEW DASHBOARD": "Nowy dashboard",
    "READY TO APPLY — CREATE DASHBOARD": "Nowy dashboard",
    "READY TO APPLY — CREATE RESOURCE": "Nowa karta",
    "CHANGED IN HA": "Zmiany w Home Assistant",
    "CONFLICT": "Konflikt zmian",
    "UNSAFE": "Zablokowane",
    "ERROR": "Błąd odczytu",
}


def topic_slug(relative):
    path = PurePosixPath(relative.split("?", 1)[0])
    parts = path.parts
    if "dashboard" in parts and len(parts) > parts.index("dashboard") + 2:
        stem = parts[parts.index("dashboard") + 1]
    else:
        stem = path.stem
    stem = stem.removeprefix("dashboard-").removesuffix("-card")
    # The existing temperature-card module belongs to the Temperatura dashboard.
    return "temperatura" if stem == "temperature" else stem


def affected_dashboard_views(item):
    """Describe changed tabs from compared JSON, without interpreting card code."""
    current = (item.get("current") or {}).get("views") or []
    desired = (item.get("github") or {}).get("views") or []
    names = []
    for views, other in ((desired, current), (current, desired)):
        for view in views:
            title = view.get("title")
            if view not in other and isinstance(title, str) and title.strip() and title not in names:
                names.append(title)
    return names


def configuration_groups(dashboards, managed, resources):
    groups = {}
    for kind, items in (("dashboard", dashboards), ("managed", managed), ("resource", resources)):
        for item in items:
            slug = topic_slug(item["relative"])
            group = groups.setdefault(slug, {
                "title": slug.replace("-", " ").replace("_", " ").capitalize(),
                "changed": [], "unchanged": [],
            })
            if kind == "dashboard":
                value = item.get("github") or {}
                views = value.get("views") or []
                title = value.get("title") or (views[0].get("title") if len(views) == 1 else None)
                if isinstance(title, str) and title.strip():
                    group["title"] = title
            label = {
                "dashboard": "Układ dashboardu",
                "resource": "Rejestracja karty",
            }.get(kind) or {
                "frontend_module": "Wygląd i działanie kart",
                "package": "Czujniki i reguły",
                "custom_template": "Szablony obliczeń",
            }.get(item.get("profile"), "Konfiguracja")
            row = {
                **item, "kind": kind, "label": label,
                "status_label": STATUS_LABELS.get(item["status"], item["status"]),
                "prefix": "" if kind == "dashboard" else kind + "_",
                "affected_views": affected_dashboard_views(item) if kind == "dashboard" else [],
                "path": "dashboards/" + item["relative"] if kind == "dashboard" else item["relative"],
            }
            if row["affected_views"] == [group["title"]]:
                row["affected_views"] = []
            group["changed" if is_changed_review(item) else "unchanged"].append(row)
    return list(groups.values())


def review_summary(groups, projects, *, incomplete=False):
    items = [item for group in groups for item in group["changed"]]
    ready = sum(bool(i["selectable"]) for i in items)
    attention = sum(not i["selectable"] or bool(i.get("warnings")) for i in items)
    updates = sum(p["state"] == "update" for p in projects)
    missing_base = sum(
        i["status"] == "IN SYNC — BASE NOT INITIALIZED"
        for g in groups for i in g["unchanged"]
    )
    project_attention = any(p["state"] in {"unknown", "failed", "unconfigured", "source", "review", "running"} for p in projects)
    if incomplete:
        title, tone = "Nie wszystko udało się sprawdzić", "attention"
    elif attention or any(p["state"] == "failed" for p in projects):
        title, tone = "Zmiany wymagają uwagi", "attention"
    elif any(p["state"] == "running" for p in projects):
        title, tone = "Aktualizacja w toku", "update"
    elif any(p["state"] == "review" for p in projects):
        title, tone = "Plan aktualizacji czeka na decyzję", "update"
    elif ready or updates:
        title, tone = "Są nowe rzeczy do wgrania", "update"
    elif missing_base:
        title, tone = "Konfiguracja zgodna, uzupełnij bazę", "attention"
    elif project_attention:
        title, tone = "Konfiguracja bez zmian", "attention"
    elif not groups and not projects:
        title, tone = "Brak elementów do sprawdzenia", "neutral"
    else:
        title, tone = "Wszystko jest aktualne", "current"
    return {"title": title, "tone": tone, "ready": ready, "attention": attention, "updates": updates, "missing_base": missing_base}


STYLE = Path(__file__).with_name("review.css").read_text(encoding="utf-8")
TEMPLATE = Path(__file__).with_name("review.html").read_text(encoding="utf-8")
SCRIPT = Path(__file__).with_name("review.js").read_text(encoding="utf-8")
