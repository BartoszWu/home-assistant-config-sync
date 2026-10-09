"""Home Journal shares deployment ordering, recovery and exact installer policy."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
from project_profiles import HOME_JOURNAL, PROJECTS
from project_deployment import Coordinator, Deployments, Installer, unpack
from test_jdg_deployment import FakeBackend, archive

try:
    from project_ui import register
    from flask import Flask
except ImportError:
    Flask = None


class JournalDeployment(unittest.TestCase):
    def test_review_approval_recovery_and_project_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            backend = FakeBackend()
            backend.target["version"] = "0.2.0"
            backend.integration = lambda release, version: archive(
                version, project=HOME_JOURNAL
            )
            ha = lambda action, job: events.append(action)
            dashboard = type(
                "Dashboard",
                (),
                {
                    "apply": lambda *args: events.append("dashboard"),
                    "finish": lambda *args: None,
                },
            )()
            coordinator = Coordinator(root / "state")
            manager = Deployments(
                root / "state",
                Installer(root, HOME_JOURNAL),
                backend,
                ha,
                dashboard,
                HOME_JOURNAL,
                coordinator,
                sleep=lambda _: None,
            )
            manager.launch = lambda: None
            job = manager.review(
                {
                    "schema_version": 2,
                    "project": "home_journal",
                    "backend_version": "0.2.0",
                    "integration_version": "0.2.0",
                },
                "c" * 40,
                {},
            )
            self.assertEqual(events, ["current_version", "check"])
            self.assertFalse(any(path == "/jobs" for path, _ in backend.calls))
            manager.approve(job["id"], job["review_hash"])
            with self.assertRaises(ValueError):
                coordinator.claim("jdg", "d" * 32)
            recovered = Deployments(
                root / "state",
                manager.installer,
                backend,
                ha,
                dashboard,
                HOME_JOURNAL,
                coordinator,
                sleep=lambda _: None,
            )
            recovered.run()
            self.assertEqual(recovered.read()["status"], "success")
            self.assertEqual(
                events,
                [
                    "current_version",
                    "check",
                    "check",
                    "backup",
                    "takeover",
                    "restart",
                    "verify",
                    "dashboard",
                    "verify",
                ],
            )
            self.assertIsNone(coordinator.owner())
            self.assertEqual(
                {p.name for p in manager.installer.target.iterdir()}, HOME_JOURNAL.files
            )
            self.assertFalse((root / "custom_components/jdg_ksiegowy").exists())

    def test_rejects_another_project_package(self):
        data, checksum = archive()
        with self.assertRaises(ValueError):
            unpack(data, checksum, "0.2.0", HOME_JOURNAL)

    @unittest.skipIf(Flask is None, "Flask unavailable")
    def test_application_visible_before_bootstrap_and_live_version_after(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Flask(__name__)
            backend = FakeBackend()
            legacy = {"missing_version": False}

            def ws(*args, **kwargs):
                if args[0] == "home_journal/version" and legacy["missing_version"]:
                    raise ValueError("Unknown legacy WS command")
                return {"version": "0.1.0", "loaded": True}

            manager = register(
                app,
                object(),
                ws,
                root,
                Coordinator(root / "state"),
                config_directory=root,
                directory=root / "state",
                backend_factory=lambda _: backend,
            )
            overview = app.extensions["project_overview"]
            targets = {
                "home_journal": {
                    "backend_version": "0.2.0",
                    "integration_version": "0.2.0",
                }
            }
            self.assertEqual(set(PROJECTS), {"jdg", "home_journal"})
            row = next(r for r in overview(targets) if r["id"] == "home_journal")
            self.assertEqual(row["state"], "unconfigured")
            self.assertIn(
                "Dziennik domu", app.test_client().get("/projects/home_journal/").text
            )
            (root / "home_journal.json").write_text("{}")
            row = next(r for r in overview(targets) if r["id"] == "home_journal")
            self.assertEqual(row["state"], "update")
            self.assertEqual(row["components"][0]["current"], "0.1.0")
            self.assertIsNone(manager("home_journal").read())
            legacy["missing_version"] = True
            row = next(r for r in overview(targets) if r["id"] == "home_journal")
            self.assertEqual(row["state"], "update")
            self.assertIsNone(row["components"][1]["current"])

            self.assertNotIn(
                "HACS", app.test_client().get("/projects/home_journal/").text
            )
