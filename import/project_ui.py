"""Ingress UI and HA adapter for the reviewed project deployment module."""

import threading
import time
from pathlib import Path

from flask import redirect, render_template_string, request
from project_deployment import Backend, Deployments, Installer, VERSION, SHA, release_set
from review_ui import STYLE
from project_profiles import PROJECTS

TEMPLATE = """<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if job and job.status == 'running' %}<meta http-equiv="refresh" content="5">{% endif %}
<title>Import {{ project.title }}</title><style>{{ ui_style|safe }}</style><main class="project-page"><a href="../../">Wróć do Importu</a><h1>Aktualizacja {{ project.title }}</h1><p>Backend, integracja Home Assistant i dashboard w jednym imporcie.</p>
{% if error %}<p class="notice error" role="alert">{{ error }}</p>{% endif %}
{% if not configured %}<div class="panel"><strong>Potrzebna jednorazowa konfiguracja</strong><p>Połącz Import z lokalnym wykonawcą aktualizacji projektu. Instrukcja instalacji opisuje dedykowany klucz i konfigurację połączenia.</p></div>
{% elif not job or job.status == 'cancelled' %}<div class="panel"><strong>Sprawdź dostępne wydania</strong><p>Import odczyta zatwierdzony zestaw z konfiguracji i przygotuje podgląd. Sprawdzenie nie aktualizuje usług.</p><form method="post" action="review"><button>Sprawdź aktualizację</button></form></div>
{% else %}<div class="panel"><span class="pill">{{ labels.get(job.status, job.status) }}</span>
<div class="row"><strong>Backend {{ project.title }}</strong><span class="value"><small>{{ job.backend_before.version or "Nieznana" }} → </small>{{ job.releases.backend_version }}</span></div>
<div class="row"><strong>Integracja HA</strong><span class="value"><small>{{ job.integration_version_before or "Nieznana" }} → </small>{{ job.releases.integration_version }}</span></div>
<div class="row"><strong>Dashboardy ({{ project.dashboards|length }})</strong><span class="value">Z konfiguracji<br><small>Wersja {{ job.source_sha[:7] }}</small></span></div>
</div>
{% if job.status == 'review' %}<p class="notice">Import wykona kopię HA, zaktualizuje backend i integrację, {% if job.restart %}zrestartuje Home Assistant, {% endif %}a następnie wgra dashboard i sprawdzi działanie. {% if job.restart %}Podczas restartu HA będzie przez chwilę niedostępny.{% endif %} Jeśli integracją zarządza HACS, Import przejmie jej aktualizacje.</p><div class="actions"><form method="post" action="start"><input type="hidden" name="id" value="{{ job.id }}"><input type="hidden" name="review_hash" value="{{ job.review_hash }}"><button>Importuj {{ project.title }}</button></form><form method="post" action="cancel"><input type="hidden" name="id" value="{{ job.id }}"><button class="secondary">Anuluj</button></form></div>
{% elif job.status == 'running' %}<p class="notice" role="status">Trwa: {{ stages.get(job.stage, job.stage) }}. Możesz zamknąć ten ekran. Postęp zostanie zapisany.</p>
{% elif job.status == 'failed' %}<p class="notice error">Przerwano etap: {{ stages.get(job.stage, job.stage) }}. {{ job.message }}</p><form method="post" action="resume"><input type="hidden" name="id" value="{{ job.id }}"><button>Wznów import</button></form>
{% else %}<p class="notice">{{ project.title }} został zaktualizowany. Potwierdzono działający backend, załadowaną integrację oraz dashboard. Odśwież otwarte dashboardy, aby załadować nowy widok.</p><form method="post" action="review"><button>Sprawdź kolejną aktualizację</button></form>{% endif %}
{% endif %}</main></html>"""
LABELS = {
    "review": "Gotowe do importu",
    "running": "Aktualizacja w toku",
    "failed": "Wymaga uwagi",
    "success": "Zakończono",
}
STAGES = {
    "backup": "kopia i sprawdzenie HA",
    "backend": "aktualizacja backendu",
    "integration": "instalacja integracji",
    "restart": "restart HA",
    "wait_ha": "oczekiwanie na HA",
    "dashboard": "import dashboardu",
    "verify": "kontrola działania",
}


def register(
    app,
    dashboards,
    ha_call,
    ha_root,
    coordinator,
    *,
    config_directory=Path("/review"),
    directory=Path("/data/projects"),
    projects=PROJECTS,
    backend_factory=Backend,
):
    if any(key != project.id for key, project in projects.items()) or len(
        {p.domain for p in projects.values()}
    ) != len(projects):
        raise ValueError(
            "Projects must have unique identities and integration directories"
        )
    manager_lock = threading.RLock()
    managers = {}
    adapters = {}

    def ha_adapter(project):
        def hacs_repository():
            config = ha_call("get_config")
            if not project.legacy_hacs_repository or "hacs" not in config.get(
                "components", []
            ):
                return None
            repositories = ha_call("hacs/repositories/list", categories=["integration"])
            # HACS list protocol must be recognized; never guess a repository ID.
            if not isinstance(repositories, list):
                raise TypeError("Nieobsługiwany protokół HACS")
            matches = [
                r
                for r in repositories
                if r.get("full_name", "").lower()
                == project.legacy_hacs_repository.lower()
            ]
            if len(matches) > 1:
                raise ValueError("Niejednoznaczny wpis HACS")
            if matches and (
                matches[0].get("domain") != project.domain or not matches[0].get("id")
            ):
                raise ValueError("Nieprawidłowy wpis HACS")
            return str(matches[0]["id"]) if matches else None

        def ha(action, job):
            if action == "check":
                dashboards.verify(job, after=False)
                if project.domain not in ha_call("get_config").get("components", []):
                    raise ValueError(
                        "Najpierw skonfiguruj działającą integrację projektu"
                    )
                hacs_repository()
            elif action == "backup":
                name = project.title + " przed importem " + job["id"]
                settings = ha_call("backup/config/info")["config"]["create_backup"]
                agents = settings.get("agent_ids")
                if (
                    not isinstance(agents, list)
                    or not agents
                    or any(not isinstance(a, str) for a in agents)
                ):
                    raise ValueError("Brak skonfigurowanego miejsca backupu HA")
                info = ha_call("backup/info")
                if not any(b.get("name") == name for b in info.get("backups", [])):
                    ha_call(
                        "backup/generate",
                        agent_ids=agents,
                        include_addons=[],
                        include_all_addons=False,
                        include_database=True,
                        include_folders=[],
                        include_homeassistant=True,
                        name=name,
                        password=settings.get("password"),
                        _timeout=60,
                    )
                for _ in range(120):
                    info = ha_call("backup/info")
                    backups = [
                        b for b in info.get("backups", []) if b.get("name") == name
                    ]
                    if info.get("state") == "idle":
                        if len(backups) != 1:
                            raise ValueError("Nie potwierdzono zakończonej kopii HA")
                        backup = backups[0]
                        if (
                            not backup.get("homeassistant_included")
                            or not backup.get("database_included")
                            or backup.get("failed_agent_ids")
                            or backup.get("failed_folders")
                            or backup.get("failed_addons")
                            or not set(agents) <= set(backup.get("agents", {}))
                        ):
                            raise ValueError("Niepełna kopia HA")
                        job["backup_id"] = backup["backup_id"]
                        break
                    time.sleep(5)
                else:
                    raise TimeoutError("Backup HA nie zakończył się")
            elif action == "takeover":
                repository = hacs_repository()
                if repository:
                    before = get_manager(project.id).installer.snapshot()
                    ha_call("hacs/repositories/remove", repository=repository)
                    if (
                        hacs_repository()
                        or get_manager(project.id).installer.snapshot() != before
                    ):
                        raise ValueError("Nie potwierdzono przejęcia z HACS")
            elif action == "restart":
                ha_call(
                    "call_service",
                    domain="homeassistant",
                    service="restart",
                    _timeout=30,
                )
            elif action == "current_version":
                try:
                    return ha_call(project.domain + "/version")
                except Exception:  # noqa: BLE001 - UI and recovery never disclose credential-bearing errors.
                    return None
            elif action == "verify":
                loaded = ha_call(project.domain + "/version")
                if loaded != {
                    "version": job["releases"]["integration_version"],
                    "loaded": True,
                }:
                    raise ValueError("Nie załadowano oczekiwanej integracji")

        return ha

    def get_manager(project_id):
        with manager_lock:
            project = projects[project_id]
            if project_id not in managers:
                manager = Deployments(
                    directory,
                    Installer(ha_root, project),
                    backend_factory(config_directory / (project.id + ".json")),
                    adapters[project_id],
                    dashboards,
                    project,
                    coordinator,
                )
                managers[project_id] = manager
                job = manager.read()
                if job and job["status"] == "running":
                    manager.launch()
            return managers[project_id]

    def show(project, error=None):
        configured = (config_directory / (project.id + ".json")).is_file()
        job = None
        if configured:
            try:
                job = get_manager(project.id).read()
            except Exception:  # noqa: BLE001 - UI and recovery never disclose credential-bearing errors.
                error = "Nie można odczytać konfiguracji projektu. Sprawdź lokalną konfigurację wykonawcy."
        return render_template_string(
            TEMPLATE,
            project=project,
            configured=configured,
            job=job,
            error=error,
            labels=LABELS,
            stages=STAGES,
            ui_style=STYLE,
        )

    def overview(targets, *, canonical=True):
        """Fresh read-only availability, independent of stored deployment reviews."""
        result = []
        for project in projects.values():
            target = targets.get(project.id) if canonical else None
            config = config_directory / (project.id + ".json")
            configured = config.is_file()
            backend_version = integration_version = None
            job = None
            pinned_target = None
            read_error = False
            if canonical:
                # Do not use get_manager: opening this list must never launch work.
                try:
                    job = Deployments(
                        directory, Installer(ha_root, project), None, None, None,
                        project, coordinator,
                    ).read()
                    if job and (
                        job.get("status") not in {"review", "running", "failed", "success", "cancelled"}
                        or not isinstance(job.get("source_sha"), str)
                        or not SHA.fullmatch(job["source_sha"])
                    ):
                        raise ValueError("Invalid saved project plan")
                    if job and job["status"] in {"review", "running", "failed"}:
                        pinned_target = release_set(job["releases"], project)
                except Exception:
                    job = None
                    read_error = True
                if configured:
                    try:
                        active = backend_factory(config).request("/status", timeout=4)
                        version = active.get("version")
                        if active.get("healthy") is True and isinstance(version, str) and VERSION.fullmatch(version):
                            backend_version = version
                    except Exception:
                        pass
                try:
                    loaded = ha_call(project.domain + "/version", _timeout=4)
                except Exception:
                    loaded = None
                if isinstance(loaded, dict) and loaded.get("loaded") is True:
                    version = loaded.get("version")
                    if isinstance(version, str) and VERSION.fullmatch(version):
                        integration_version = version
            displayed_target = pinned_target or target
            target_label = "przygotowane" if pinned_target else "w konfiguracji"
            components = []
            for label, key, current in (
                ("Usługa", "backend_version", backend_version),
                ("Integracja HA", "integration_version", integration_version),
            ):
                version = displayed_target.get(key) if displayed_target else None
                available = target.get(key) if pinned_target and target else None
                components.append({
                    "label": label, "current": current, "target_label": target_label,
                    "target": version,
                    "available_target": available if available != version else None,
                })
            has_new_versions = any(c["available_target"] for c in components)
            state, label, action = "same", "Bez zmian", "Otwórz"
            description = "Wersje zgodne z konfiguracją"
            if not canonical:
                state, label = "source", "Sprawdź na main"
                description = "Aktualizacje aplikacji korzystają z main"
            elif read_error:
                state, label = "unknown", "Nie udało się sprawdzić"
                description = "Nie można odczytać zapisanego planu"
            elif job and job["status"] in {"running", "failed", "review"}:
                state = job["status"]
                label = LABELS[state]
                action = "Wznów import" if state == "failed" else "Otwórz plan"
                description = "Przygotowany zestaw / " + job["source_sha"][:7]
            elif not configured:
                state, label = "unconfigured", "Połącz wykonawcę"
                description = "Potrzebna jednorazowa konfiguracja"
            elif not target or any(not c["current"] for c in components):
                state, label = "unknown", "Nie udało się sprawdzić"
                description = "Sprawdź połączenie i zestaw wersji"
            elif any(c["current"] != c["target"] for c in components):
                state, label, action = "update", "Dostępna zmiana", "Sprawdź aktualizację"
                description = "Wersje do zmiany"
            result.append({
                "id": project.id, "title": project.title, "state": state,
                "status_label": label, "action_label": action,
                "description": description, "components": components,
                "has_new_versions": has_new_versions,
                "availability_unknown": bool(pinned_target and not target),
            })
        return result

    app.extensions["project_overview"] = overview

    @app.get("/jdg/")
    def legacy_jdg_link():
        return redirect("../projects/jdg/", code=302)

    @app.get("/projects/")
    def project_list():
        return render_template_string(
            '<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aktualizacje projektów</title><style>{{ style|safe }}</style><main><a href="../">Wróć do Importu</a><h1>Aktualizacje projektów</h1><p>Backend na serwerze, integracja HA i dashboardy w jednym imporcie.</p><div class="panel">{% for p in projects.values() %}<div class="row"><strong>{{ p.title }}</strong><a href="{{ p.id }}/">Sprawdź aktualizację →</a></div>{% endfor %}</div></main></html>',
            projects=projects,
            style=STYLE,
        )

    @app.get("/projects/<project_id>/")
    def project_index(project_id):
        if project_id not in projects:
            return "Nieznany projekt", 404
        return show(projects[project_id])

    @app.post("/projects/<project_id>/<action>")
    def project_action(project_id, action):
        if project_id not in projects:
            return "Nieznany projekt", 404
        project = projects[project_id]
        try:
            deployment = get_manager(project_id)
            if action == "review":
                sha, releases, plan = dashboards.preview(project)
                deployment.review(releases, sha, plan)
            elif action == "start":
                deployment.approve(
                    request.form.get("id"), request.form.get("review_hash")
                )
            elif action == "resume":
                deployment.resume(request.form.get("id"))
            elif action == "cancel":
                deployment.cancel(request.form.get("id"))
            else:
                return show(project, "Nieznana operacja"), 400
        except Exception:  # noqa: BLE001 - UI and recovery never disclose credential-bearing errors.
            return show(
                project,
                "Operacja wstrzymana. Sprawdź połączenie, wydania, konflikty i inne trwające aktualizacje.",
            ), 409
        return redirect("./", code=303)

    # Only already-approved jobs may recover at worker startup.
    for project in projects.values():
        adapters[project.id] = ha_adapter(project)
        if (config_directory / (project.id + ".json")).is_file():
            try:
                manager = get_manager(project.id)
                job = manager.read()
                if (
                    job
                    and job["status"] == "success"
                    and coordinator.owner() == {"project": project.id, "id": job["id"]}
                ):
                    coordinator.release(project.id, job["id"])
            except Exception:  # noqa: BLE001, S110 - unavailable setup is reported by the UI.
                pass
    return get_manager
