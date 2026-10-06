"""Two different projects cross the same deployment interface and share HA ownership."""

import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_jdg_deployment import archive, FakeBackend
from project_deployment import Backend, Coordinator, Deployments, Installer, unpack
from project_profiles import JDG, Project

OTHER = Project(
    id="demo",
    title="Synthetic demo",
    domain="demo_logic",
    files=frozenset({"__init__.py", "manifest.json", "version.py"}),
    dashboards=("dashboard-demo.json", "dashboard-demo-details.json"),
    bundles=("www/dashboard/demo.mjs",),
    resources=("/local/dashboard/demo.mjs",),
    minimum_backend=(1, 0, 0),
    minimum_integration=(1, 0, 0),
)


class ProjectBackend(FakeBackend):
    def __init__(self, project):
        super().__init__()
        self.project = project
        self.target["version"] = "0.3.0" if project == JDG else "1.4.0"

    def integration(self, backend_release, integration_version):
        return archive(integration_version, project=self.project)


class Dashboard:
    def __init__(self):
        self.fail = False
        self.applied = []

    def apply(self, job, save):
        if self.fail:
            raise ValueError("Synthetic failure")
        self.applied.append(job["project"])

    def finish(self, job, save):
        pass


class Projects(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = Coordinator(self.root / "state")
        self.events = []
        self.dashboard = Dashboard()
        self.backends = {}

    def manager(self, project):
        backend = self.backends.setdefault(project.id, ProjectBackend(project))

        def ha(action, job):
            self.events.append((project.id, action))

        manager = Deployments(
            self.root / "state",
            Installer(self.root, project),
            backend,
            ha,
            self.dashboard,
            project,
            self.coordinator,
            sleep=lambda _: None,
        )
        manager.launch = lambda: None
        return manager

    def review(self, manager):
        return manager.review(
            {
                "schema_version": 2,
                "project": manager.project.id,
                "backend_version": manager.backend.target["version"],
                "integration_version": "0.7.0" if manager.project == JDG else "1.2.0",
            },
            "c" * 40,
            {},
        )

    def test_two_projects_install_different_packages_without_flow_forks(self):
        first, second = self.manager(JDG), self.manager(OTHER)
        jobs = [self.review(first), self.review(second)]
        first.approve(jobs[0]["id"], jobs[0]["review_hash"])
        with self.assertRaises(ValueError):
            second.approve(jobs[1]["id"], jobs[1]["review_hash"])
        with self.assertRaises(ValueError):
            with self.coordinator.operation():
                self.fail("Ordinary Apply must not run during a project update")
        first.run()
        self.assertIsNone(self.coordinator.owner())
        second.approve(jobs[1]["id"], jobs[1]["review_hash"])
        second.run()
        self.assertEqual(
            [first.read()["status"], second.read()["status"]], ["success", "success"]
        )
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(self.dashboard.applied, ["jdg", "demo"])
        for manager in (first, second):
            manifest = json.loads(
                (manager.installer.target / "manifest.json").read_text()
            )
            self.assertEqual(manifest["domain"], manager.project.domain)
        with self.assertRaises(ValueError):
            unpack(*archive(project=OTHER, version="1.2.0"), "1.2.0", JDG)

    def test_failed_project_retains_ownership_across_process_recovery(self):
        manager = self.manager(OTHER)
        job = self.review(manager)
        manager.approve(job["id"], job["review_hash"])
        self.dashboard.fail = True
        manager.run()
        self.assertEqual(manager.read()["status"], "failed")
        # New coordinator reads the durable owner, rather than an in-memory flag.
        self.coordinator = Coordinator(self.root / "state")
        other = self.manager(JDG)
        other_job = self.review(other)
        with self.assertRaises(ValueError):
            other.approve(other_job["id"], other_job["review_hash"])
        recovered = self.manager(OTHER)
        self.dashboard.fail = False
        recovered.resume(job["id"])
        recovered.run()
        self.assertEqual(recovered.read()["status"], "success")
        self.assertEqual(self.events.count(("demo", "restart")), 1)
        self.assertEqual(
            sum(path == "/jobs" for path, _ in self.backends["demo"].calls), 1
        )
        self.assertIsNone(self.coordinator.owner())

    def test_changed_profile_cannot_resume_an_old_plan(self):
        manager = self.manager(OTHER)
        self.review(manager)
        changed = self.manager(replace(OTHER, files=OTHER.files | {"extra.py"}))
        with self.assertRaises(ValueError):
            changed.read()

    def test_private_package_transport_checks_release_sha_and_decodes_bounded_payload(
        self,
    ):
        config = self.root / "demo.json"
        token = self.root / "demo-token"
        token.write_text("synthetic" * 5)
        config.write_text(
            json.dumps(
                {"backend_url": "http://executor.test:3001", "token_file": str(token)}
            )
        )
        backend = Backend(config)
        data, checksum = archive(project=OTHER, version="1.2.0")
        payload = {
            "sha": "a" * 40,
            "version": "1.2.0",
            "sha256": checksum,
            "archive": base64.b64encode(data).decode(),
        }
        with patch.object(backend, "request", return_value=payload) as request:
            self.assertEqual(
                backend.integration({"version": "1.4.0", "sha": "a" * 40}, "1.2.0"),
                (data, checksum),
            )
            self.assertEqual(request.call_args.kwargs["maximum"], 3 * 1024 * 1024)
            with self.assertRaises(ValueError):
                backend.integration({"version": "1.4.0", "sha": "b" * 40}, "1.2.0")

    def test_corrupt_owner_fails_closed_for_other_projects_and_ordinary_apply(self):
        self.coordinator.path.parent.mkdir()
        for content in ("null", "{}", '{"project":"demo","id":"bad"}'):
            self.coordinator.path.write_text(content)
            with self.assertRaises(ValueError):
                self.coordinator.claim("jdg", "a" * 32)
            with self.assertRaises(ValueError):
                with self.coordinator.operation():
                    self.fail("Corrupt owner must not authorize Apply")
