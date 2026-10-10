"""Exercise the complete reviewed Import UI with synthetic HA/backend adapters."""

import copy
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
try:
    from flask import Flask
    from project_ui import register
    from project_deployment import Coordinator
    from project_profiles import JDG
    from dashboard_sync import DashboardSync
    from git_source import SourceRevision
    from dashboard_logic import digest
except ImportError:
    Flask = None
from test_jdg_deployment import FakeBackend, archive


@unittest.skipIf(Flask is None, "Flask unavailable")
class UI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sha = "c" * 40
        self.backend = FakeBackend()
        self.actions = []
        self.hacs = True
        self.backup = None
        self.config = self.root / "jdg.json"
        self.config.write_text("{}")
        (self.root / "deployments").mkdir()
        (self.root / "deployments/jdg.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "project": "jdg",
                    "backend_version": "0.3.0",
                    "integration_version": "0.7.0",
                }
            )
        )
        self.bundle = self.root / "www/dashboard/faktury.mjs"
        self.bundle.parent.mkdir(parents=True)
        self.bundle.write_bytes(b"old")
        self.items = {
            "dashboard": {
                "relative": "dashboard-faktury.json",
                "preview_ha_hash": digest({"old": True}),
                "preview_desired_hash": digest({"new": True}),
                "selectable": True,
                "warnings": [],
            },
            "managed": {
                "relative": "www/dashboard/faktury.mjs",
                "preview_ha_hash": hashlib.sha256(b"old").hexdigest(),
                "preview_desired_hash": hashlib.sha256(b"new").hexdigest(),
                "selectable": True,
                "warnings": [],
            },
            "resource": {
                "relative": "/local/dashboard/faktury.mjs",
                "preview_ha_hash": digest(
                    {"url": "/local/dashboard/faktury.mjs", "type": "module"}
                ),
                "preview_desired_hash": digest(
                    {"url": "/local/dashboard/faktury.mjs", "type": "module"}
                ),
                "selectable": False,
                "warnings": [],
            },
        }
        self.live_dashboard = {"old": True}

        class Access:
            provenance_available = True

            def review(inner, project, sha=None):
                self.assertTrue(sha in (None, self.sha))
                revision = SourceRevision(
                    "main", "branch", self.sha, self.sha[:7], ("main",)
                )
                manifest = self.root / "deployments/jdg.json"
                releases = json.loads(manifest.read_text()) if manifest.exists() else None
                return (
                    revision,
                    releases,
                    [
                        {**copy.deepcopy(item), "kind": kind}
                        for kind, item in self.items.items()
                    ],
                )

            def read(inner, kind, relative):
                if kind == "dashboard":
                    return digest(self.live_dashboard)
                if kind == "managed":
                    return hashlib.sha256(self.bundle.read_bytes()).hexdigest()
                return self.items[kind]["preview_ha_hash"]

            def snapshot(inner, kind, relative):
                if kind == "dashboard":
                    return {"encoding": "json", "data": copy.deepcopy(self.live_dashboard)}
                if kind == "managed":
                    import base64
                    return {"encoding": "base64", "data": base64.b64encode(self.bundle.read_bytes()).decode()}
                raise AssertionError(kind)

            def provenance(inner, kind, relative):
                if not inner.provenance_available:
                    return None
                return SimpleNamespace(
                    canonical=True,
                    commit_sha=self.sha,
                    content_hash=self.items[kind]["preview_ha_hash"],
                )

            def apply(inner, form, owner):
                self.assertEqual(owner, ("jdg", self.manager().read()["id"]))
                self.assertEqual(form["reviewed_sha"], [self.sha])
                return self.apply()

            def export(inner, dashboards):
                self.actions.append(("export", dashboards))

        self.access = Access()
        self.app = Flask(__name__)
        manager = register(
            self.app,
            DashboardSync(self.access),
            self.ws,
            self.root,
            Coordinator(self.root / "data"),
            config_directory=self.root,
            directory=self.root / "data",
            backend_factory=lambda _: self.backend,
            projects={JDG.id: JDG},
        )
        self.manager = lambda: manager("jdg")
        self.client = self.app.test_client()

    def ws(self, kind, **kwargs):
        self.actions.append((kind, kwargs))
        if kind == "get_config":
            return {"components": ["jdg_ksiegowy", "hacs"]}
        if kind == "hacs/repositories/list":
            return (
                [
                    {
                        "full_name": "BartoszWu/ha-jdg-ksiegowy",
                        "domain": "jdg_ksiegowy",
                        "id": "synthetic-jdg",
                    }
                ]
                if self.hacs
                else []
            ) + [{"full_name": "example/other", "domain": "other", "id": "other"}]
        if kind == "hacs/repositories/remove":
            self.assertEqual(kwargs, {"repository": "synthetic-jdg"})
            self.hacs = False
            return {}
        if kind == "backup/config/info":
            return {
                "config": {
                    "create_backup": {
                        "agent_ids": ["synthetic-store"],
                        "password": None,
                    }
                }
            }
        if kind == "backup/generate":
            self.assertFalse(kwargs["include_all_addons"])
            self.assertEqual(kwargs["include_addons"], [])
            self.backup = {
                "name": kwargs["name"],
                "backup_id": "synthetic-backup",
                "homeassistant_included": True,
                "database_included": kwargs["include_database"],
                "agents": {"synthetic-store": {}},
                "failed_agent_ids": [],
            }
            return {"backup_job_id": "synthetic-job"}
        if kind == "backup/info":
            return {
                "state": "idle",
                "backups": [self.backup] if self.backup else [],
                "agent_errors": {},
            }
        if kind == "call_service":
            if kwargs["domain"] == "backup":
                self.backup = "new"
            return {}
        if kind == "jdg_ksiegowy/version":
            return {
                "version": "0.7.0",
                "loaded": self.manager().installer.target.exists(),
            }
        raise AssertionError(kind)

    def apply(self):
        self.live_dashboard = {"new": True}
        self.bundle.write_bytes(b"new")
        for item in self.items.values():
            item["preview_ha_hash"] = item["preview_desired_hash"]
        return [{"ok": True}]

    def overview(self, releases=None, **kwargs):
        return self.app.extensions["project_overview"](
            {"jdg": releases or {"backend_version": "0.3.0", "integration_version": "0.7.0"}},
            **kwargs,
        )[0]

    def test_availability_reads_live_versions_without_preparing_or_launching_job(self):
        self.manager().installer.target.mkdir(parents=True)
        project = self.overview()
        self.assertEqual(project["state"], "update")
        self.assertEqual(project["components"][0]["current"], "0.1.0")
        self.assertEqual(project["components"][0]["target"], "0.3.0")
        self.assertEqual(project["components"][1]["current"], "0.7.0")
        self.assertEqual(self.backend.calls, [("/status", None), ("/releases/latest", None)])
        self.assertEqual([kind for kind, _ in self.actions], ["jdg_ksiegowy/version"])
        self.assertIsNone(self.manager().read())
        self.assertFalse((self.root / "data/jdg").exists())

    def test_published_release_is_visible_and_prepared_without_changing_configuration(self):
        before = (self.root / "deployments/jdg.json").read_bytes()
        self.manager().installer.target.mkdir(parents=True)
        self.backend.published = {"version": "0.4.0", "sha": "d" * 40, "integration_version": "0.8.0"}
        project = self.overview()
        self.assertEqual(project["state"], "update")
        self.assertEqual([c["target"] for c in project["components"]], ["0.4.0", "0.8.0"])
        self.assertEqual(project["components"][0]["target_label"], "dostępne")
        self.assertIsNone(self.manager().read())
        self.assertFalse(any(path.startswith("/integration") or path == "/jobs" for path, _ in self.backend.calls))
        self.backend.target = {"version": "0.4.0", "sha": "d" * 40}
        response = self.client.post("/projects/jdg/review")
        self.assertEqual(response.status_code, 303)
        original = self.manager().read()
        self.assertEqual(original["releases"]["backend_version"], "0.4.0")
        self.assertEqual(original["backend"]["sha"], "d" * 40)
        self.assertEqual(original["source_sha"], self.sha)
        self.assertEqual((self.root / "deployments/jdg.json").read_bytes(), before)
        self.backend.published = {"version": "0.5.0", "sha": "e" * 40, "integration_version": "0.9.0"}
        project = self.overview()
        self.assertEqual([c["target"] for c in project["components"]], ["0.4.0", "0.8.0"])
        self.assertEqual([c["available_target"] for c in project["components"]], ["0.5.0", "0.9.0"])
        self.assertEqual(self.manager().read(), original)

    def test_discovery_failure_never_claims_current_or_prepares_stale_configured_release(self):
        self.backend.published = {"version": "0.4.0", "sha": "invalid", "integration_version": "0.8.0"}
        project = self.overview()
        self.assertEqual(project["state"], "unknown")
        self.assertTrue(project["availability_unknown"])
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())
        self.assertFalse(any(path == "/jobs" for path, _ in self.backend.calls))

    def test_discovery_commit_change_between_check_and_preparation_blocks_review(self):
        self.backend.published = {**self.backend.target, "integration_version": "0.7.0", "sha": "d" * 40}
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())

    def test_current_release_is_determined_by_publication_even_without_version_manifest(self):
        self.backend.active["version"] = "0.3.0"
        self.backend.published = {**self.backend.target, "integration_version": "0.7.0"}
        self.manager().installer.target.mkdir(parents=True)
        project = self.app.extensions["project_overview"]({"jdg": None})[0]
        self.assertEqual(project["state"], "same")
        self.assertFalse(project["availability_unknown"])
        self.assertIn("opublikowane", project["description"])
        (self.root / "deployments/jdg.json").unlink()
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 303)
        self.assertEqual(self.manager().read()["releases"]["backend_version"], "0.3.0")

    def test_newer_installed_version_is_not_offered_a_downgrade(self):
        self.backend.active["version"] = "0.5.0"
        self.backend.published = {**self.backend.target, "integration_version": "0.7.0"}
        self.manager().installer.target.mkdir(parents=True)
        project = self.overview()
        self.assertEqual(project["state"], "same")
        self.assertEqual(project["components"][0]["target"], "0.5.0")
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())

    def test_saved_success_does_not_hide_new_application_version(self):
        self.client.post("/projects/jdg/review")
        job = self.manager().read()
        job["status"] = "success"
        self.manager()._save(job)
        self.manager().installer.target.mkdir(parents=True)
        self.backend.active["version"] = "0.3.0"
        self.backend.calls.clear()
        project = self.overview({"backend_version": "0.4.0", "integration_version": "0.8.0"})
        self.assertEqual(project["state"], "update")
        self.assertEqual(project["components"][0]["target"], "0.4.0")
        self.assertEqual(project["components"][1]["target"], "0.8.0")
        self.assertEqual(self.manager().read()["status"], "success")
        self.assertEqual(self.backend.calls, [("/status", None), ("/releases/latest", None)])

    def test_unreachable_service_keeps_known_integration_and_redacts_error(self):
        self.manager().installer.target.mkdir(parents=True)
        self.backend.request = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("sensitive synthetic URL"))
        project = self.overview()
        self.assertEqual(project["state"], "unknown")
        self.assertIsNone(project["components"][0]["current"])
        self.assertEqual(project["components"][1]["current"], "0.7.0")
        self.assertNotIn("sensitive", str(project))
        self.assertIsNone(self.manager().read())

    def test_feature_source_does_not_offer_main_application_versions(self):
        project = self.overview(canonical=False)
        self.assertEqual(project["state"], "source")
        self.assertTrue(all(c["target"] is None for c in project["components"]))
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.actions)

    def test_corrupt_saved_plan_does_not_break_the_overview_or_launch_work(self):
        self.client.post("/projects/jdg/review")
        job = self.manager().read()
        del job["status"]
        self.manager()._save(job)
        self.backend.calls.clear()
        self.assertEqual(self.overview()["state"], "unknown")
        self.assertEqual(self.backend.calls, [("/status", None), ("/releases/latest", None)])
        self.assertFalse(any(path == "/jobs" for path, _ in self.backend.calls))

    def test_pending_plan_does_not_display_new_main_versions_as_approved(self):
        self.client.post("/projects/jdg/review")
        project = self.overview({"backend_version": "0.4.0", "integration_version": "0.8.0"})
        self.assertEqual(project["state"], "review")
        self.assertEqual(project["components"][0]["target"], "0.3.0")
        self.assertEqual(project["components"][1]["target_label"], "przygotowane")

    def test_new_versions_remain_visible_beside_each_active_pinned_job(self):
        self.client.post("/projects/jdg/review")
        original = self.manager().read()
        for status in ("review", "running", "failed"):
            with self.subTest(status=status):
                original["status"] = status
                self.manager()._save(original)
                self.backend.calls.clear()
                self.actions.clear()
                project = self.overview({"backend_version": "0.4.0", "integration_version": "0.8.0"})
                self.assertEqual(project["state"], status)
                self.assertTrue(project["has_new_versions"])
                self.assertEqual([c["target"] for c in project["components"]], ["0.3.0", "0.7.0"])
                self.assertEqual([c["available_target"] for c in project["components"]], ["0.4.0", "0.8.0"])
                self.assertEqual(self.manager().read(), original)
                self.assertEqual(self.backend.calls, [("/status", None), ("/releases/latest", None)])
                self.assertEqual([kind for kind, _ in self.actions], ["jdg_ksiegowy/version"])

    def test_pending_job_does_not_invent_available_versions_when_targets_are_missing(self):
        self.client.post("/projects/jdg/review")
        project = self.app.extensions["project_overview"]({"jdg": None})[0]
        self.assertEqual(project["state"], "review")
        self.assertTrue(project["availability_unknown"])
        self.assertFalse(project["has_new_versions"])
        self.assertTrue(all(c["available_target"] is None for c in project["components"]))
        self.assertEqual(project["components"][0]["target"], "0.3.0")

    def test_cancel_confirms_success_and_does_not_launch_deployment(self):
        self.client.post("/projects/jdg/review")
        job = self.manager().read()
        self.backend.calls.clear()
        self.actions.clear()
        response = self.client.post("/projects/jdg/cancel", data={"id": job["id"]})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.manager().read()["status"], "cancelled")
        self.assertEqual(self.manager().read()["review_hash"], job["review_hash"])
        html = self.client.get("/projects/jdg/").get_data(as_text=True)
        self.assertIn("Przygotowana aktualizacja została anulowana.", html)
        self.assertIn("Sprawdź aktualizację", html)
        self.assertNotIn("Importuj JDG", html)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.actions)

    def test_stale_cancel_does_not_cancel_the_current_review(self):
        self.client.post("/projects/jdg/review")
        job = self.manager().read()
        response = self.client.post("/projects/jdg/cancel", data={"id": "obsolete-review"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.manager().read(), job)
        self.assertIn("Operacja wstrzymana", response.get_data(as_text=True))

    def test_pending_plan_keeps_its_recovery_link(self):
        self.client.post("/projects/jdg/review")
        self.assertEqual(self.overview()["state"], "review")
        job = self.manager().read()
        job["status"] = "failed"
        self.manager()._save(job)
        project = self.overview()
        self.assertEqual(project["state"], "failed")
        self.assertEqual(project["action_label"], "Wznów import")

    def test_complete_review_approval_and_readback(self):
        response = self.client.post("/projects/jdg/review")
        self.assertEqual(response.status_code, 303)
        job = self.manager().read()
        html = self.client.get("/projects/jdg/").get_data(as_text=True)
        self.assertIn("Importuj JDG", html)
        self.assertIn("0.3.0", html)
        self.assertNotIn("backend_url", html)
        self.assertFalse(self.manager().installer.target.exists())
        response = self.client.post(
            "/projects/jdg/start",
            data={"id": job["id"], "review_hash": job["review_hash"]},
        )
        self.assertEqual(response.status_code, 303)
        for _ in range(100):
            if self.manager().read()["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(self.manager().read()["status"], "success")
        self.assertFalse(self.hacs)
        self.assertEqual(sum(kind == "export" for kind, _ in self.actions), 1)
        self.assertFalse(any(kind.startswith("backup/") for kind, _ in self.actions))
        checkpoint = self.root / "data/jdg/recovery" / (job["id"] + ".json")
        saved = json.loads(checkpoint.read_text())
        self.assertEqual(saved["dashboard"]["dashboard:dashboard-faktury.json"]["data"], {"old": True})
        self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)

    def test_progress_and_recovery_scope_are_visible_without_javascript(self):
        self.client.post("/projects/jdg/review")
        job = self.manager().read()
        job["status"] = "running"
        job["stage"] = "integration"
        self.manager()._save(job)
        self.manager()._launch = lambda: None
        html = self.client.get("/projects/jdg/").get_data(as_text=True)
        self.assertIn('aria-current="step"', html)
        self.assertIn("Postęp aktualizacji", html)
        self.assertIn("Bez kopii całego HA i jego historii", html)
        self.assertIn("Gotowe", html)
        self.assertIn("W toku", html)
        self.assertIn("Oczekuje", html)
        self.assertNotIn("backup_id", html)

    def test_warnings_block_shortcut_without_writes(self):
        self.items["dashboard"]["warnings"] = [{"reason": "synthetic warning"}]
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())
        self.assertFalse(any(kind == "call_service" for kind, _ in self.actions))

    def test_failed_checkpoint_does_not_start_backend(self):
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 303)
        job = self.manager().read()
        self.access.snapshot = lambda *args: (_ for _ in ()).throw(ValueError("capture failed"))
        self.manager()._launch = lambda: None
        self.manager().approve(job["id"], job["review_hash"])
        self.manager()._run()
        self.assertEqual(self.manager().read()["status"], "failed")
        self.assertFalse(any(path == "/jobs" for path, _ in self.backend.calls))

    def test_missing_provenance_blocks_completion_and_export(self):
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 303)
        job = self.manager().read()
        self.access.provenance_available = False
        self.manager()._launch = lambda: None
        self.manager().approve(job["id"], job["review_hash"])
        self.manager()._run()
        self.assertEqual(self.manager().read()["status"], "failed")
        self.assertFalse(any(kind == "export" for kind, _ in self.actions))

    def test_catalog_and_routes_accept_another_reviewed_profile(self):
        from test_projects import OTHER

        app = Flask("synthetic-catalog")
        register(
            app,
            DashboardSync(self.access),
            self.ws,
            self.root,
            Coordinator(self.root / "other-state"),
            config_directory=self.root,
            directory=self.root / "other-state",
            projects={"jdg": JDG, "demo": OTHER},
            backend_factory=lambda _: self.backend,
        )
        client = app.test_client()
        catalog = client.get("/projects/").get_data(as_text=True)
        self.assertIn('href="demo/"', catalog)
        self.assertIn(OTHER.title, catalog)
        self.assertIn(OTHER.title, client.get("/projects/demo/").get_data(as_text=True))
        self.assertEqual(client.get("/projects/unknown/").status_code, 404)
        self.assertEqual(client.post("/projects/unknown/start").status_code, 404)
        self.assertEqual(client.get("/jdg/").status_code, 302)
