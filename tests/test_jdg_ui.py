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
                releases = json.loads((self.root / "deployments/jdg.json").read_text())
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
                "database_included": True,
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
        self.assertLess(
            next(
                i
                for i, (kind, data) in enumerate(self.actions)
                if kind == "backup/generate"
            ),
            next(
                i
                for i, (kind, _) in enumerate(self.actions)
                if kind == "hacs/repositories/remove"
            ),
        )

    def test_warnings_block_shortcut_without_writes(self):
        self.items["dashboard"]["warnings"] = [{"reason": "synthetic warning"}]
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())
        self.assertFalse(any(kind == "call_service" for kind, _ in self.actions))

    def test_failed_backup_does_not_start_backend(self):
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 303)
        job = self.manager().read()
        self.manager().ha = lambda action, _: (
            (_ for _ in ()).throw(ValueError("backup failed"))
            if action == "backup"
            else None
        )
        self.manager().launch = lambda: None
        self.manager().approve(job["id"], job["review_hash"])
        self.manager().run()
        self.assertEqual(self.manager().read()["status"], "failed")
        self.assertFalse(any(path == "/jobs" for path, _ in self.backend.calls))

    def test_missing_provenance_blocks_completion_and_export(self):
        self.assertEqual(self.client.post("/projects/jdg/review").status_code, 303)
        job = self.manager().read()
        self.access.provenance_available = False
        self.manager().launch = lambda: None
        self.manager().approve(job["id"], job["review_hash"])
        self.manager().run()
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
