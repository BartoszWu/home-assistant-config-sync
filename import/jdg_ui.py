"""Ingress UI and HA adapter for the bounded JDG deployment module."""

import json
import time
from managed_files import resolve_live_path, read_snapshot
from pathlib import Path
from flask import render_template_string, request, redirect
from werkzeug.datastructures import MultiDict
from jdg_deployment import Backend, Deployments, Installer, release_set

DASHBOARD = "dashboard-faktury.json"
BUNDLE = "www/dashboard/faktury.mjs"
RESOURCE = "/local/dashboard/faktury.mjs"
REPOSITORY = "BartoszWu/ha-jdg-ksiegowy"
TEMPLATE = """<!doctype html><html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if job and job.status == 'running' %}<meta http-equiv="refresh" content="5">{% endif %}
<title>Import JDG</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f7;color:#202630;font:16px system-ui,sans-serif}main{max-width:850px;margin:48px auto;padding:0 24px}a{color:#43566b}h1{font-size:36px;letter-spacing:-1px;margin:24px 0 8px}p{line-height:1.6;color:#526071}.panel{background:white;border:1px solid #dce1e8;border-radius:16px;padding:24px;margin:24px 0}.row{display:flex;justify-content:space-between;gap:16px;padding:18px 0;border-bottom:1px solid #edf0f3}.row:last-child{border:0}strong{font-weight:650}.value{text-align:right}small{color:#657384}button{border:0;border-radius:9px;padding:14px 20px;background:#244e63;color:white;font:600 16px system-ui;cursor:pointer}.secondary{background:#e7ecf0;color:#354758}.actions{display:flex;gap:12px;flex-wrap:wrap}.notice{padding:16px 20px;border-radius:10px;background:#e7eff3;line-height:1.5}.error{background:#fff0df;color:#674622}.pill{font-size:13px;padding:6px 10px;border-radius:20px;background:#e7eff3}code{font-size:13px;overflow-wrap:anywhere}@media(max-width:550px){main{margin:24px auto;padding:0 16px}h1{font-size:30px}.panel{padding:18px}.actions form,.actions button{width:100%}.row{gap:10px;font-size:15px}}
</style><main><a href="../">← Import konfiguracji</a><h1>Aktualizacja JDG</h1><p>Backend, integracja Home Assistant i dashboard w jednym imporcie.</p>
{% if error %}<p class="notice error" role="alert">{{ error }}</p>{% endif %}
{% if not configured %}<div class="panel"><strong>Potrzebna jednorazowa konfiguracja</strong><p>Połącz Import z lokalnym wykonawcą aktualizacji JDG. Instrukcja instalacji opisuje dedykowany klucz i konfigurację połączenia.</p></div>
{% elif not job or job.status == 'cancelled' %}<div class="panel"><strong>Sprawdź dostępne wydania</strong><p>Import odczyta zatwierdzony zestaw z konfiguracji i przygotuje podgląd. Sprawdzenie nie aktualizuje usług.</p><form method="post" action="review"><button>Sprawdź aktualizację</button></form></div>
{% else %}<div class="panel"><span class="pill">{{ labels.get(job.status, job.status) }}</span>
<div class="row"><strong>Backend JDG</strong><span class="value"><small>{{ job.backend_before.version or "Nieznana" }} → </small>{{ job.releases.backend_version }}</span></div>
<div class="row"><strong>Integracja HA</strong><span class="value"><small>{{ job.integration_version_before or "Nieznana" }} → </small>{{ job.releases.integration_version }}</span></div>
<div class="row"><strong>Dashboard Faktury</strong><span class="value">Z konfiguracji<br><small>Wersja {{ job.source_sha[:7] }}</small></span></div>
</div>
{% if job.status == 'review' %}<p class="notice">Import wykona kopię HA, zaktualizuje backend i integrację, {% if job.restart %}zrestartuje Home Assistant, {% endif %}a następnie wgra dashboard i sprawdzi działanie. {% if job.restart %}Podczas restartu HA będzie przez chwilę niedostępny.{% endif %} Jeśli integracją zarządza HACS, Import przejmie jej aktualizacje.</p><div class="actions"><form method="post" action="start"><input type="hidden" name="id" value="{{ job.id }}"><input type="hidden" name="review_hash" value="{{ job.review_hash }}"><button>Importuj JDG</button></form><form method="post" action="cancel"><input type="hidden" name="id" value="{{ job.id }}"><button class="secondary">Anuluj</button></form></div>
{% elif job.status == 'running' %}<p class="notice" role="status">Trwa: {{ stages.get(job.stage, job.stage) }}. Możesz zamknąć ten ekran. Postęp zostanie zapisany.</p>
{% elif job.status == 'failed' %}<p class="notice error">Przerwano etap: {{ stages.get(job.stage, job.stage) }}. {{ job.message }}</p><form method="post" action="resume"><input type="hidden" name="id" value="{{ job.id }}"><button>Wznów import</button></form>
{% else %}<p class="notice">JDG został zaktualizowany. Potwierdzono działający backend, załadowaną integrację oraz dashboard. Odśwież otwarty dashboard Faktury, aby załadować nowy widok.</p><form method="post" action="review"><button>Sprawdź kolejną aktualizację</button></form>{% endif %}
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
    app, host, config_path=Path("/review/jdg.json"), directory=Path("/data/jdg")
):
    manager = None

    def changes(sha=None):
        with host.REPO_LOCK:
            revision = host.coerce_revision(
                host.refresh_repo("main", pin_sha=sha), "main"
            )
            if revision.stale or (sha and revision.commit_sha != sha):
                raise ValueError("Zestaw zmienił się; przygotuj nowe review")
            entries = host.managed_entries_for_revision()
            managed, _ = host.collect_managed_changes(
                host.WORKDIR,
                host.HA_CONFIG_ROOT,
                [e for e in entries if e.path == BUNDLE],
                host.unsafe_reason,
                stage_frontend=False,
                commit_sha=revision.commit_sha,
            )
            dashboards = [
                c
                for c in host.collect_changes(
                    revision=revision, only_relative=DASHBOARD
                )
                if c["relative"] == DASHBOARD
            ]
            resources = [
                c
                for c in host.collect_resource_changes(
                    host.MANAGED_POLICY, host.ha_ws_call, entries
                )
                if c["relative"] == RESOURCE
            ]
            if len(dashboards) != 1 or len(managed) != 1 or len(resources) != 1:
                raise ValueError("Brak pełnego zestawu dashboardu JDG")
            return revision, {
                "dashboard": dashboards[0],
                "managed": managed[0],
                "resource": resources[0],
            }

    def plan(items):
        result = {"form": {"source": ["main"]}, "expected": {}}
        for kind, item in items.items():
            prefix = "" if kind == "dashboard" else kind + "_"
            changed = item["preview_ha_hash"] != item["preview_desired_hash"]
            if item.get("warnings") or (changed and not item["selectable"]):
                raise ValueError(
                    "Konflikt lub ostrzeżenie w dashboardzie; sprawdź zwykły Import"
                )
            result["expected"][kind] = {
                "before": item["preview_ha_hash"],
                "after": item["preview_desired_hash"],
            }
            if changed:
                result["form"][prefix + "selected"] = [item["relative"]]
                result["form"][prefix + "preview_hash"] = [
                    item["relative"] + ":" + item["preview_ha_hash"]
                ]
                result["form"][prefix + "desired_hash"] = [
                    item["relative"] + ":" + item["preview_desired_hash"]
                ]
        return result

    def validate_dashboard(job, after=False):
        if after:
            current = {
                "dashboard": host.digest(host.ha_dashboard_config(DASHBOARD)),
                "managed": read_snapshot(
                    resolve_live_path(host.HA_CONFIG_ROOT, BUNDLE)
                ).sha256,
            }
            resources = host.collect_resource_changes(
                host.MANAGED_POLICY,
                host.ha_ws_call,
                host.managed_entries_for_revision(),
            )
            resource = [r for r in resources if r["relative"] == RESOURCE]
            if len(resource) != 1:
                raise ValueError("Nie potwierdzono zasobu JDG")
            current["resource"] = resource[0]["preview_ha_hash"]
            if any(
                current[k] != value["after"]
                for k, value in job["dashboard"]["expected"].items()
            ):
                raise ValueError("Nie potwierdzono dashboardu JDG")
            store = host.live_provenance_store()
            for kind, name, entries in (
                ("dashboard", DASHBOARD, store.dashboards),
                ("managed", BUNDLE, store.managed_files),
            ):
                expected = job["dashboard"]["expected"][kind]
                if expected["before"] == expected["after"]:
                    continue
                entry = entries.get(name)
                if (
                    not entry
                    or not entry.canonical
                    or entry.commit_sha != job["source_sha"]
                    or entry.content_hash != expected["after"]
                ):
                    raise ValueError("Nie potwierdzono pochodzenia dashboardu JDG")
            return
        _, items = changes(job["source_sha"])
        for kind, item in items.items():
            expected = job["dashboard"]["expected"][kind]
            if (
                item["preview_desired_hash"] != expected["after"]
                or item["preview_ha_hash"] != expected["after" if after else "before"]
            ):
                raise ValueError("Dashboard zmienił się po review")

    def dashboard(job):
        # Apply already provides conflict checks, provenance, read-back and Export.
        _, items = changes(job["source_sha"])
        form = {k: list(v) for k, v in job["dashboard"]["form"].items()}
        for kind, item in items.items():
            prefix = "" if kind == "dashboard" else kind + "_"
            expected = job["dashboard"]["expected"][kind]
            if item["preview_desired_hash"] != expected["after"]:
                raise ValueError("Zmieniona wersja dashboardu")
            if item["preview_ha_hash"] == expected["after"]:
                for key in ("selected", "preview_hash", "desired_hash"):
                    form.pop(prefix + key, None)
            elif item["preview_ha_hash"] != expected["before"]:
                raise ValueError("Dashboard zmienił się po review")
        if any(k.endswith("selected") for k in form):
            with app.test_request_context(
                "/apply",
                method="POST",
                data=MultiDict((k, v) for k, values in form.items() for v in values),
            ):
                results = host.apply_selected(jdg_job=True, request_export_after=False)
            if not isinstance(results, list) or any(not r.get("ok") for r in results):
                raise ValueError("Import dashboardu nie powiódł się")
        validate_dashboard(job, after=True)
        dashboard_changed = (
            job["dashboard"]["expected"]["dashboard"]["before"]
            != job["dashboard"]["expected"]["dashboard"]["after"]
        )
        if dashboard_changed and not job.get("export_requested"):
            host.request_export([DASHBOARD])
            job["export_requested"] = True
            get_manager().save(job)

    def hacs_repository():
        config = host.ha_ws_call("get_config")
        if "hacs" not in config.get("components", []):
            return None
        repositories = host.ha_ws_call(
            "hacs/repositories/list", categories=["integration"]
        )
        # HACS list protocol must be recognized; never guess a repository ID.
        if not isinstance(repositories, list):
            raise ValueError("Nieobsługiwany protokół HACS")
        matches = [
            r
            for r in repositories
            if r.get("full_name", "").lower() == REPOSITORY.lower()
        ]
        if len(matches) > 1:
            raise ValueError("Niejednoznaczny wpis HACS")
        if matches and (
            matches[0].get("domain") != "jdg_ksiegowy" or not matches[0].get("id")
        ):
            raise ValueError("Nieprawidłowy wpis HACS")
        return str(matches[0]["id"]) if matches else None

    def ha(action, job):
        if action == "check":
            validate_dashboard(job)
            if "jdg_ksiegowy" not in host.ha_ws_call("get_config").get(
                "components", []
            ):
                raise ValueError("Najpierw skonfiguruj działającą integrację JDG")
            hacs_repository()
        elif action == "backup":
            name = "JDG przed importem " + job["id"]
            settings = host.ha_ws_call("backup/config/info")["config"]["create_backup"]
            agents = settings.get("agent_ids")
            if (
                not isinstance(agents, list)
                or not agents
                or any(not isinstance(a, str) for a in agents)
            ):
                raise ValueError("Brak skonfigurowanego miejsca backupu HA")
            info = host.ha_ws_call("backup/info")
            if not any(b.get("name") == name for b in info.get("backups", [])):
                host.ha_ws_call(
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
                info = host.ha_ws_call("backup/info")
                backups = [b for b in info.get("backups", []) if b.get("name") == name]
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
                before = get_manager().installer.snapshot()
                host.ha_ws_call("hacs/repositories/remove", repository=repository)
                if hacs_repository() or get_manager().installer.snapshot() != before:
                    raise ValueError("Nie potwierdzono przejęcia z HACS")
        elif action == "restart":
            host.ha_ws_call(
                "call_service", domain="homeassistant", service="restart", _timeout=30
            )
        elif action == "current_version":
            try:
                return host.ha_ws_call("jdg_ksiegowy/version")
            except Exception:
                return None
        elif action == "verify":
            loaded = host.ha_ws_call("jdg_ksiegowy/version")
            if loaded != {
                "version": job["releases"]["integration_version"],
                "loaded": True,
            }:
                raise ValueError("Nie załadowano oczekiwanej integracji")

    def get_manager():
        nonlocal manager
        if manager is None:
            manager = Deployments(
                directory,
                Installer(host.HA_CONFIG_ROOT),
                Backend(config_path),
                ha,
                dashboard,
            )
            job = manager.read()
            if job and job["status"] == "running":
                manager.launch()
        return manager

    def show(error=None):
        configured = config_path.is_file()
        job = None
        if configured:
            try:
                job = get_manager().read()
            except Exception:
                error = "Nie można odczytać konfiguracji JDG. Sprawdź lokalną konfigurację wykonawcy."
        return render_template_string(
            TEMPLATE,
            configured=configured,
            job=job,
            error=error,
            labels=LABELS,
            stages=STAGES,
        )

    @app.get("/jdg/")
    def jdg_index():
        return show()

    @app.post("/jdg/<action>")
    def jdg_action(action):
        try:
            deployment = get_manager()
            if action == "review":
                revision, items = changes()
                releases = release_set(
                    json.loads((host.WORKDIR / "deployments/jdg.json").read_text())
                )
                dashboard_plan = plan(items)
                dashboard_plan["form"]["reviewed_sha"] = [revision.commit_sha]
                deployment.review(releases, revision.commit_sha, dashboard_plan)
            elif action == "start":
                deployment.approve(
                    request.form.get("id"), request.form.get("review_hash")
                )
            elif action == "resume":
                deployment.resume(request.form.get("id"))
            elif action == "cancel":
                deployment.cancel(request.form.get("id"))
            else:
                return show("Nieznana operacja"), 400
        except Exception:
            return show(
                "Operacja wstrzymana. Sprawdź połączenie, dostępność wydań i konflikty w zwykłym Imporcie."
            ), 409
        return redirect("./", code=303)

    # Recover an already approved job at worker startup; GET alone is not required.
    if config_path.is_file():
        try:
            get_manager()
        except Exception:
            pass  # UI reports unavailable local setup; never deploy without a persisted approval.

    return get_manager
