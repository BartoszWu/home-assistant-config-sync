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
    from jdg_ui import register
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
                    "schema_version": 1,
                    "backend_version": "0.2.0",
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
        self.host = SimpleNamespace(
            REPO_LOCK=threading.RLock(),
            WORKDIR=self.root,
            HA_CONFIG_ROOT=self.root,
            MANAGED_POLICY=object(),
            unsafe_reason=lambda _: None,
            coerce_revision=lambda value, _: value,
            refresh_repo=lambda *_, **__: SourceRevision(
                "main", "branch", self.sha, self.sha[:7], ("main",)
            ),
            managed_entries_for_revision=lambda: [
                SimpleNamespace(path="www/dashboard/faktury.mjs")
            ],
            collect_managed_changes=lambda *_, **__: (
                [copy.deepcopy(self.items["managed"])],
                {},
            ),
            collect_changes=lambda **_: [copy.deepcopy(self.items["dashboard"])],
            collect_resource_changes=lambda *_, **__: [
                copy.deepcopy(self.items["resource"])
            ],
            ha_ws_call=self.ws,
            ha_dashboard_config=lambda _: self.live_dashboard,
            digest=digest,
            apply_selected=self.apply,
            live_provenance_store=lambda: SimpleNamespace(
                dashboards={
                    "dashboard-faktury.json": SimpleNamespace(
                        canonical=True,
                        commit_sha=self.sha,
                        content_hash=self.items["dashboard"]["preview_ha_hash"],
                    )
                },
                managed_files={
                    "www/dashboard/faktury.mjs": SimpleNamespace(
                        canonical=True,
                        commit_sha=self.sha,
                        content_hash=self.items["managed"]["preview_ha_hash"],
                    )
                },
            ),
            request_export=lambda dashboards: self.actions.append(
                ("export", dashboards)
            ),
        )
        self.app = Flask(__name__)
        with (
            patch("jdg_ui.Backend", return_value=self.backend),
            patch(
                "jdg_deployment.download_integration", side_effect=lambda _: archive()
            ),
        ):
            # Constructor default is a function object, so patch the manager's downloader below.
            self.manager = register(
                self.app, self.host, self.config, self.root / "data"
            )
            self.manager().downloader = lambda _: archive()
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

    def apply(self, **kwargs):
        self.assertEqual(kwargs, {"jdg_job": True, "request_export_after": False})
        self.live_dashboard = {"new": True}
        self.bundle.write_bytes(b"new")
        for item in self.items.values():
            item["preview_ha_hash"] = item["preview_desired_hash"]
        return [{"ok": True}]

    def test_complete_review_approval_and_readback(self):
        response = self.client.post("/jdg/review")
        self.assertEqual(response.status_code, 303)
        job = self.manager().read()
        html = self.client.get("/jdg/").get_data(as_text=True)
        self.assertIn("Importuj JDG", html)
        self.assertIn("0.2.0", html)
        self.assertNotIn("backend_url", html)
        self.assertFalse(self.manager().installer.target.exists())
        response = self.client.post(
            "/jdg/start", data={"id": job["id"], "review_hash": job["review_hash"]}
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
        self.assertEqual(self.client.post("/jdg/review").status_code, 409)
        self.assertIsNone(self.manager().read())
        self.assertFalse(any(kind == "call_service" for kind, _ in self.actions))

    def test_failed_backup_does_not_start_backend(self):
        self.assertEqual(self.client.post("/jdg/review").status_code, 303)
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
        self.assertEqual(self.client.post("/jdg/review").status_code, 303)
        job = self.manager().read()
        self.host.live_provenance_store = lambda: SimpleNamespace(
            dashboards={}, managed_files={}
        )
        self.manager().launch = lambda: None
        self.manager().approve(job["id"], job["review_hash"])
        self.manager().run()
        self.assertEqual(self.manager().read()["status"], "failed")
        self.assertFalse(any(kind == "export" for kind, _ in self.actions))
