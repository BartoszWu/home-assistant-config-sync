"""Shared checkpoint, conservative profile policy and durable-plan regressions."""

import base64
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
import test_jdg_deployment as deployment_tests
import test_jdg_ui as ui_tests
from project_profiles import HOME_JOURNAL, JDG, RecoveryPolicy
from project_recovery import digest


class Recovery(unittest.TestCase):
    def fixture(self):
        fixture = deployment_tests.DeploymentTest(
            "test_review_has_no_deployment_and_requires_matching_approval"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_old_integration_bytes_survive_success_without_full_backup(self):
        f = self.fixture()
        f.installer.target.mkdir(parents=True)
        (f.installer.target / "__init__.py").write_bytes(b"old\xff")
        job = f.review()
        f.deployment.approve(job["id"], job["review_hash"])
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "success")
        self.assertNotIn("backup", f.events)
        path = f.deployment.directory / "recovery" / (job["id"] + ".json")
        checkpoint = json.loads(path.read_text())
        self.assertEqual(
            base64.b64decode(checkpoint["integration"]["__init__.py"]), b"old\xff"
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(f.installer.previous.exists())

    def test_capture_failure_stops_before_backend_and_retains_owner(self):
        f = self.fixture()
        job = f.review()
        f.deployment.approve(job["id"], job["review_hash"])
        f.deployment.dashboard.snapshot = lambda _: (_ for _ in ()).throw(
            ValueError("capture failed")
        )
        f.deployment._run()
        self.assertEqual(f.deployment.read()["stage"], "backup")
        self.assertEqual(f.deployment.read()["status"], "failed")
        self.assertFalse(any(path == "/jobs" for path, _ in f.backend.calls))
        self.assertIsNotNone(f.coordinator.owner())

    def test_resume_reuses_original_snapshot_and_rejects_corruption(self):
        f = self.fixture()
        job = f.review()
        f.deployment.approve(job["id"], job["review_hash"])
        f.fail_dashboard = True
        f.deployment._run()
        path = f.deployment.directory / "recovery" / (job["id"] + ".json")
        original = path.read_bytes()
        f.deployment.dashboard.snapshot = lambda _: self.fail(
            "Must not capture changed live state"
        )
        f.fail_dashboard = False
        f.deployment.resume(job["id"])
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "success")
        self.assertEqual(path.read_bytes(), original)
        saved = f.deployment.read()
        saved["status"] = "running"
        saved["stage"] = "backend"
        f.deployment._save(saved)
        checkpoint = json.loads(original)
        checkpoint["integration"]["__init__.py"] = base64.b64encode(b"changed").decode()
        path.write_text(json.dumps(checkpoint))
        f.backend.calls.clear()
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "failed")
        self.assertFalse(any(p == "/jobs" for p, _ in f.backend.calls))

    def test_legacy_plan_keeps_backup_and_original_profile_identity(self):
        f = self.fixture()
        job = f.review()
        del job["recovery_policy"]
        job["profile_hash"] = digest(JDG.legacy_identity())
        job["review_hash"] = digest(
            {k: v for k, v in job.items() if k != "review_hash"}
        )
        f.deployment._save(job)
        f.deployment.approve(job["id"], job["review_hash"])
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "success")
        self.assertIn("backup", f.events)

    def test_dashboard_only_update_does_not_redeploy_or_back_up_backend_data(self):
        f = self.fixture()
        f.backend.active.update(f.backend.target)
        job = f.review()
        self.assertFalse(job["backend_changed"])
        f.deployment.approve(job["id"], job["review_hash"])
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "success")
        self.assertFalse(any(p == "/jobs" for p, _ in f.backend.calls))

    def test_recovery_policy_is_reviewed_not_selected_by_git(self):
        for project in (JDG, HOME_JOURNAL):
            self.assertEqual(project.recovery.ha_backup, "targeted")
            self.assertEqual(project.recovery.backend_data, "executor")
        with self.assertRaises(ValueError):
            RecoveryPolicy(ha_backup="skip_everything")
        f = self.fixture()
        job = f.review()
        job["recovery_policy"]["ha_backup"] = "full_ha"
        f.deployment._save(job)
        with self.assertRaises(ValueError):
            f.deployment.read()


@unittest.skipIf(ui_tests.Flask is None, "Flask unavailable")
class ProfileBackups(unittest.TestCase):
    def fixture(self, mode):
        f = ui_tests.UI("test_complete_review_approval_and_readback")
        f.setUp()
        self.addCleanup(f.doCleanups)
        manager = f.manager()
        manager.project = replace(
            JDG, recovery=RecoveryPolicy(ha_backup=mode, backend_data="executor")
        )
        f.client.post("/projects/jdg/review")
        job = manager.read()
        manager._launch = lambda: None
        manager.approve(job["id"], job["review_hash"])
        return f, manager, job

    def test_configuration_backup_excludes_history(self):
        f, manager, _ = self.fixture("ha_configuration")
        manager._run()
        self.assertEqual(manager.read()["status"], "success")
        generated = [data for kind, data in f.actions if kind == "backup/generate"]
        self.assertEqual(len(generated), 1)
        self.assertFalse(generated[0]["include_database"])
        self.assertFalse(generated[0]["include_all_addons"])
        self.assertEqual(manager.read()["backup_id"], "synthetic-backup")

    def test_full_backup_is_reserved_for_explicit_profile_requirement(self):
        f, manager, _ = self.fixture("full_ha")
        manager._run()
        self.assertEqual(manager.read()["status"], "success")
        generated = [data for kind, data in f.actions if kind == "backup/generate"]
        self.assertTrue(generated[0]["include_database"])

    def test_incomplete_configuration_backup_blocks_backend(self):
        f, manager, _ = self.fixture("ha_configuration")
        f.backup = {
            "name": JDG.title + " przed importem " + manager.read()["id"],
            "backup_id": "incomplete",
            "homeassistant_included": False,
            "database_included": False,
            "agents": {"synthetic-store": {}},
        }
        manager._run()
        self.assertEqual(manager.read()["status"], "failed")
        self.assertFalse(any(p == "/jobs" for p, _ in f.backend.calls))
