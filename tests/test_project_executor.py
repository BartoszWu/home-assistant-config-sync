"""Deployment outcomes through the shared import interface, with synthetic HTTP data."""

import unittest

import test_jdg_deployment as fixtures


class ImportRecoveryContract(unittest.TestCase):
    def fixture(self):
        f = fixtures.DeploymentTest(
            "test_review_has_no_deployment_and_requires_matching_approval"
        )
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_unsupported_executor_blocks_plan_before_approval(self):
        for capabilities in (
            {},
            {"recovery_receipt": True, "minimum_version": "0.1.0"},
            {"recovery_receipt": 1, "minimum_version": "9.0.0"},
        ):
            with self.subTest(capabilities=capabilities):
                f = self.fixture()
                original = f.backend.request
                f.backend.request = (
                    lambda path, *a, capabilities=capabilities, original=original, **kw: (
                        capabilities
                        if path == "/capabilities"
                        else original(path, *a, **kw)
                    )
                )
                with self.assertRaises(ValueError):
                    f.review()
                self.assertIsNone(f.deployment.read())
                self.assertFalse(any(p == "/jobs" for p, _ in f.backend.calls))

    def test_unverified_success_cannot_install_integration(self):
        for change in (
            "missing",
            "wrong_job",
            "wrong_release",
            "unverified",
            "private_path",
        ):
            with self.subTest(change=change):
                f = self.fixture()
                job = f.review()
                f.deployment.approve(job["id"], job["review_hash"])
                original = f.backend.request

                def request(path, *a, original=original, change=change, **kw):
                    result = original(path, *a, **kw)
                    if path.startswith("/jobs/"):
                        if change == "missing":
                            result.pop("recovery")
                        elif change == "wrong_job":
                            result["id"] = "d" * 32
                        elif change == "wrong_release":
                            result["recovery"]["sha"] = "d" * 40
                        elif change == "unverified":
                            result["recovery"]["verified"] = False
                        else:
                            result["recovery"]["path"] = "/private/synthetic"
                    return result

                f.backend.request = request
                f.deployment._run()
                self.assertEqual(f.deployment.read()["status"], "failed")
                self.assertEqual(f.deployment.read()["stage"], "backend")
                self.assertFalse(f.installer.target.exists())
                self.assertIsNotNone(f.coordinator.owner())

    def test_verified_receipt_is_persisted_and_required_on_resume(self):
        f = self.fixture()
        job = f.review()
        f.fail_dashboard = True
        f.deployment.approve(job["id"], job["review_hash"])
        f.deployment._run()
        saved = f.deployment.read()
        self.assertEqual(saved["backend_recovery"]["job_id"], job["id"])
        saved["backend_recovery"]["sha256"] = "invalid"
        f.deployment._save(saved)
        f.fail_dashboard = False
        f.deployment.resume(job["id"])
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "failed")

    def test_previous_approved_plan_keeps_its_executor_contract(self):
        f = self.fixture()
        job = f.review()
        del job["executor_contract"]
        f.deployment._save(job)
        f.deployment.approve(job["id"], job["review_hash"])
        original = f.backend.request
        f.backend.request = lambda path, *a, **kw: (
            {"status": "success"}
            if path.startswith("/jobs/")
            else original(path, *a, **kw)
        )
        f.deployment._run()
        self.assertEqual(f.deployment.read()["status"], "success")

    def test_public_prepare_owns_dashboard_selection(self):
        f = self.fixture()
        f.deployment.dashboard.preview = lambda project: (
            "c" * 40,
            {
                "schema_version": 2,
                "project": project.id,
                "backend_version": "0.3.0",
                "integration_version": "0.7.0",
            },
            {"expected": {}},
        )
        job = f.deployment.prepare()
        self.assertEqual(job["status"], "review")
        self.assertEqual(job["source_sha"], "c" * 40)
        self.assertFalse(any(p == "/jobs" for p, _ in f.backend.calls))
