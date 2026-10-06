"""Ingress UI and HA adapter for the reviewed project deployment module."""

import threading
import time
from pathlib import Path

from flask import redirect, render_template_string, request
from project_deployment import Backend, Deployments, Installer
from project_profiles import PROJECTS

TEMPLATE = """<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if job and job.status == 'running' %}<meta http-equiv="refresh" content="5">{% endif %}
<title>Import {{ project.title }}</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f7;color:#202630;font:16px system-ui,sans-serif}main{max-width:850px;margin:48px auto;padding:0 24px}a{color:#43566b}h1{font-size:36px;letter-spacing:-1px;margin:24px 0 8px}p{line-height:1.6;color:#526071}.panel{background:white;border:1px solid #dce1e8;border-radius:16px;padding:24px;margin:24px 0}.row{display:flex;justify-content:space-between;gap:16px;padding:18px 0;border-bottom:1px solid #edf0f3}.row:last-child{border:0}strong{font-weight:650}.value{text-align:right}small{color:#657384}button{border:0;border-radius:9px;padding:14px 20px;background:#244e63;color:white;font:600 16px system-ui;cursor:pointer}.secondary{background:#e7ecf0;color:#354758}.actions{display:flex;gap:12px;flex-wrap:wrap}.notice{padding:16px 20px;border-radius:10px;background:#e7eff3;line-height:1.5}.error{background:#fff0df;color:#674622}.pill{font-size:13px;padding:6px 10px;border-radius:20px;background:#e7eff3}code{font-size:13px;overflow-wrap:anywhere}@media(max-width:550px){main{margin:24px auto;padding:0 16px}h1{font-size:30px}.panel{padding:18px}.actions form,.actions button{width:100%}.row{gap:10px;font-size:15px}}
</style><main><a href="../../">← Import konfiguracji</a><h1>Aktualizacja {{ project.title }}</h1><p>Backend, integracja Home Assistant i dashboard w jednym imporcie.</p>
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
        )

    @app.get("/jdg/")
    def legacy_jdg_link():
        return redirect("../projects/jdg/", code=302)

    @app.get("/projects/")
    def project_list():
        return render_template_string(
            '<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aktualizacje projektów</title><style>{{ style|safe }}</style><main><a href="../">← Import konfiguracji</a><h1>Aktualizacje projektów</h1><p>Backend na serwerze, integracja HA i dashboardy w jednym imporcie.</p><div class="panel">{% for p in projects.values() %}<div class="row"><strong>{{ p.title }}</strong><a href="{{ p.id }}/">Sprawdź aktualizację →</a></div>{% endfor %}</div></main></html>',
            projects=projects,
            style=TEMPLATE.split("<style>")[1].split("</style>")[0],
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
