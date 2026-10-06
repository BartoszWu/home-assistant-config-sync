"""Synthetic deployment/recovery and installer boundary regression cases."""

import fnmatch
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
from project_deployment import (
    Deployments,
    Installer,
    Coordinator,
    unpack as unpack_project,
    release_set as release_project,
)
from project_profiles import JDG, JDG_FILES as FILES


def unpack(data, checksum, version):
    return unpack_project(data, checksum, version, JDG)


def release_set(value):
    return release_project(value, JDG)


from project_apparmor import NAMES, RECOVERY_NAMES, denied_names


def archive(version="0.7.0", extra=None, project=JDG):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zipped:
        for name in project.files:
            content = (
                json.dumps(
                    {"domain": project.domain, "version": version, "requirements": []}
                ).encode()
                if name == "manifest.json"
                else b"synthetic"
            )
            zipped.writestr(name, content)
        if extra:
            zipped.writestr(extra, b"synthetic")
    value = data.getvalue()
    return value, hashlib.sha256(value).hexdigest()


class FakeBackend:
    def __init__(self):
        self.active = {"sha": "a" * 40, "healthy": True, "version": "0.1.0"}
        self.target = {"version": "0.3.0", "sha": "b" * 40}
        self.calls = []

    def integration(self, backend_release, integration_version):
        return archive(integration_version)

    def request(self, path, value=None):
        self.calls.append((path, value))
        if path == "/status":
            return dict(self.active)
        if path.startswith("/release"):
            return dict(self.target)
        if path == "/jobs":
            self.active["sha"] = value["sha"]
            self.active["version"] = value["version"]
            return {"status": "running"}
        return {"status": "success"}


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.installer = Installer(self.root, JDG)
        self.backend = FakeBackend()
        self.coordinator = Coordinator(self.root / "data")
        self.events = []
        self.fail_dashboard = False
        self.deployment = Deployments(
            self.root / "data",
            self.installer,
            self.backend,
            self.ha,
            type(
                "Dashboard",
                (),
                {
                    "apply": lambda _, job, save: self.dashboard(job),
                    "finish": lambda *args: None,
                },
            )(),
            JDG,
            self.coordinator,
            sleep=lambda _: None,
        )
        # Drive the durable worker deterministically, without network or HA.
        self.deployment.launch = lambda: None

    def ha(self, action, job):
        self.events.append(action)
        if action == "restart":
            raise ConnectionError("Core closed socket during restart")

    def dashboard(self, job):
        self.events.append("dashboard")
        if self.fail_dashboard:
            raise ValueError("synthetic conflict")

    def review(self):
        return self.deployment.review(
            {
                "schema_version": 2,
                "project": "jdg",
                "backend_version": "0.3.0",
                "integration_version": "0.7.0",
            },
            "c" * 40,
            {},
        )

    def test_review_has_no_deployment_and_requires_matching_approval(self):
        job = self.review()
        self.assertFalse(self.installer.target.exists())
        self.assertEqual(self.events, ["current_version", "check"])
        self.assertFalse(any(path == "/jobs" for path, _ in self.backend.calls))
        with self.assertRaises(ValueError):
            self.deployment.approve(job["id"], "bad")
        self.backend.active["sha"] = "d" * 40
        with self.assertRaises(ValueError):
            self.deployment.approve(job["id"], job["review_hash"])

    def test_backup_then_backend_install_restart_dashboard_and_verify(self):
        job = self.review()
        self.deployment.approve(job["id"], job["review_hash"])
        self.deployment.run()
        self.assertEqual(self.deployment.read()["status"], "success")
        self.assertEqual(
            self.events,
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
        self.assertEqual(
            set(
                p.relative_to(self.installer.target).as_posix()
                for p in self.installer.target.rglob("*")
                if p.is_file()
            ),
            FILES,
        )
        self.assertFalse(self.installer.previous.exists())

    def test_resuming_dashboard_does_not_restart_or_redeploy_backend(self):
        job = self.review()
        self.deployment.approve(job["id"], job["review_hash"])
        self.fail_dashboard = True
        self.deployment.run()
        self.assertEqual(self.deployment.read()["stage"], "dashboard")
        self.assertEqual(self.deployment.read()["status"], "failed")
        # Recreate the process, preserving only persisted state.
        recovered = Deployments(
            self.root / "data",
            self.installer,
            self.backend,
            self.ha,
            self.deployment.dashboard,
            JDG,
            self.coordinator,
        )
        recovered.launch = lambda: None
        self.fail_dashboard = False
        recovered.resume(job["id"])
        recovered.run()
        self.assertEqual(recovered.read()["status"], "success")
        self.assertEqual(self.events.count("restart"), 1)
        self.assertEqual(sum(path == "/jobs" for path, _ in self.backend.calls), 1)

    def test_installer_preserves_old_code_and_recovers_between_renames(self):
        files = unpack(*archive(), "0.7.0")
        self.installer.parent.mkdir()
        self.installer.target.mkdir()
        (self.installer.target / "__init__.py").write_bytes(b"old")
        before = self.installer.snapshot()
        self.installer.stage.mkdir(parents=True)
        for name, data in files.items():
            path = self.installer.stage / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        self.installer.target.rename(self.installer.previous)
        self.installer.install(files, before)
        self.assertEqual((self.installer.previous / "__init__.py").read_bytes(), b"old")
        self.assertEqual(
            (self.installer.target / "__init__.py").read_bytes(), b"synthetic"
        )
        self.assertFalse(self.installer.install(files, before))

    def test_rejects_traversal_symlink_checksum_unknown_files_and_old_contract(self):
        data, checksum = archive(extra="../escape.py")
        with self.assertRaises(ValueError):
            unpack(data, checksum, "0.7.0")
        with self.assertRaises(ValueError):
            unpack(archive()[0], "0" * 64, "0.7.0")
        self.installer.parent.mkdir()
        self.installer.target.symlink_to(
            self.root / "outside", target_is_directory=True
        )
        with self.assertRaises(ValueError):
            self.installer.snapshot()
        with self.assertRaises(ValueError):
            release_set(
                {
                    "schema_version": 2,
                    "project": "jdg",
                    "backend_version": "0.1.0",
                    "integration_version": "0.6.2",
                }
            )

    def test_previous_manifest_cannot_shadow_current_integration(self):
        files = unpack(*archive(), "0.7.0")
        self.installer.parent.mkdir()
        self.installer.target.mkdir()
        (self.installer.target / "manifest.json").write_text(
            json.dumps({"domain": "jdg_ksiegowy", "version": "0.6.2"})
        )
        self.installer.install(files, self.installer.snapshot())
        # Core discovers directories by manifest, including hidden names.
        discovered = [
            json.loads((p / "manifest.json").read_text())
            for p in self.installer.parent.iterdir()
            if p.is_dir() and (p / "manifest.json").is_file()
        ]
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["version"], "0.7.0")
        self.assertEqual(
            json.loads((self.installer.previous / "manifest.json").read_text())[
                "version"
            ],
            "0.6.2",
        )

    def test_recovery_root_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.installer.recovery.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.installer.install(
                unpack(*archive(), "0.7.0"), self.installer.snapshot()
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_apparmor_names_allow_only_exact_profile_targets(self):
        for allowed in (NAMES, RECOVERY_NAMES):
            self.assert_exact_names(allowed)

    def assert_exact_names(self, allowed):
        patterns = [p.replace("[^", "[!") for p in denied_names(allowed)]
        blocked = lambda name: any(fnmatch.fnmatchcase(name, p) for p in patterns)
        for name in allowed:
            self.assertFalse(blocked(name), name)
            for suffix in ("x", "-extra", ".old"):
                self.assertTrue(blocked(name + suffix), name + suffix)
            for length in range(1, len(name)):
                self.assertTrue(blocked(name[:length]), name[:length])
        for name in (
            "hacs",
            "tuya_local",
            ".",
            "jdg",
            ".stage",
            "version.py",
            "a",
            "z",
        ):
            self.assertTrue(blocked(name), name)


if __name__ == "__main__":
    unittest.main()
