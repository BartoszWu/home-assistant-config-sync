import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

import managed_files as mf  # noqa: E402
from dashboard_logic import MISSING_BASE_STATUS  # noqa: E402

MANAGED_BOOTSTRAP_STATUS = mf.MANAGED_BOOTSTRAP_STATUS


def unsafe_none(_value):
    return None


class PathGuardTests(unittest.TestCase):
    def test_rejects_traversal_and_absolute(self):
        for bad in (
            "../secrets.yaml",
            "/etc/passwd",
            "packages/../../secrets.yaml",
            "packages/./x.yaml",
            "www\\\\x.mjs",
            ".storage/core.config",
            "packages/secrets.yaml",
            "custom_components/foo/manifest.json",
            "configuration.yaml",
            "packages/foo.db",
        ):
            with self.assertRaises(ValueError):
                mf.validate_policy_path(bad)

    def test_accepts_allowlisted_shapes(self):
        for good in (
            "packages/temperatura.yaml",
            "www/temperature-card.mjs",
            "custom_templates/temperatura.jinja",
            "packages/nested/file.yaml",
        ):
            mf.validate_policy_path(good)

    def test_resolve_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "packages").mkdir()
            outside = Path(tmp + "_outside")
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("nope", encoding="utf-8")
            link = root / "packages" / "temperatura.yaml"
            link.symlink_to(secret)
            with self.assertRaises(ValueError):
                mf.resolve_live_path(root, "packages/temperatura.yaml")


class ClassifyTests(unittest.TestCase):
    def test_ready_when_git_changed_and_live_matches_base(self):
        status, css, selectable, _ = mf.classify_file("aaa", "bbb", "bbb")
        self.assertEqual(status, "READY TO APPLY")
        self.assertTrue(selectable)
        self.assertEqual(css, "ready")

    def test_conflict_and_changed_in_ha(self):
        status, _, selectable, _ = mf.classify_file("a", "b", "c")
        self.assertEqual(status, "CONFLICT")
        self.assertFalse(selectable)
        status, _, selectable, _ = mf.classify_file("base", "live", "base")
        self.assertEqual(status, "CHANGED IN HA")
        self.assertFalse(selectable)

    def test_missing_base_and_create(self):
        status, _, selectable, _ = mf.classify_file("same", "same", None)
        self.assertEqual(status, MISSING_BASE_STATUS)
        self.assertFalse(selectable)
        status, _, selectable, _ = mf.classify_file("new", None, None)
        self.assertEqual(status, "READY TO APPLY")
        self.assertTrue(selectable)

    def test_missing_base_with_drift_is_explicit_bootstrap(self):
        status, css, selectable, reason = mf.classify_file("git", "live", None)
        self.assertEqual(status, MANAGED_BOOTSTRAP_STATUS)
        self.assertEqual(css, "bootstrap")
        self.assertTrue(selectable)
        self.assertIn("Initialize bases will not adopt", reason)


class StagingAndApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "packages").mkdir()
        (self.root / "www").mkdir()
        self.workdir = self.root / "git"
        (self.workdir / "packages").mkdir(parents=True)
        (self.workdir / "www").mkdir(parents=True)
        self.bases = self.root / "bases.json"
        self.backup = self.root / "backups"
        self.journal = self.root / "journal.json"
        self.patches = [
            mock.patch.object(mf, "BASES_PATH", self.bases),
            mock.patch.object(mf, "BACKUP_ROOT", self.backup),
            mock.patch.object(mf, "JOURNAL_PATH", self.journal),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.tmp.cleanup()

    def test_staging_does_not_touch_canonical(self):
        canonical = self.root / "www" / "temperature-card.mjs"
        canonical.write_text("OLD", encoding="utf-8")
        data = b"NEW-MODULE"
        digest = mf.file_digest(data)
        staging = mf.ensure_frontend_staging(
            self.root, "www/temperature-card.mjs", data, digest
        )
        self.assertEqual(canonical.read_text(encoding="utf-8"), "OLD")
        staged = self.root / "www" / mf.PREVIEW_DIRNAME / digest[:12] / "temperature-card.mjs"
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), data)
        self.assertIn("/local/.config-sync-preview/", staging["preview_url"])

    def test_apply_updates_base_and_rejects_stale_preview(self):
        live = self.root / "packages" / "temperatura.yaml"
        live.write_text("old: 1\n", encoding="utf-8")
        git = self.workdir / "packages" / "temperatura.yaml"
        git.write_text("new: 2\n", encoding="utf-8")
        live_hash = mf.file_digest(live.read_bytes())
        git_hash = mf.file_digest(git.read_bytes())
        bases = {"schema_version": 1, "files": {}}
        mf.set_base_hash(bases, "packages/temperatura.yaml", live_hash)
        mf.save_bases(bases)

        entries = [mf.ManagedEntry("packages/temperatura.yaml", "package")]
        calls = []

        def fake_ws(message_type, **payload):
            calls.append((message_type, payload))
            return {}

        with mock.patch.object(mf, "ha_check_config", return_value={"result": "valid", "errors": None}):
            results, applied = mf.apply_managed_files(
                ["packages/temperatura.yaml"],
                {"packages/temperatura.yaml": live_hash},
                {"packages/temperatura.yaml": git_hash},
                self.workdir,
                self.root,
                entries,
                unsafe_none,
                fake_ws,
                "token",
            )
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(applied, ["packages/temperatura.yaml"])
        self.assertEqual(live.read_text(encoding="utf-8"), "new: 2\n")
        stored = mf.load_bases()
        self.assertEqual(
            stored["files"]["packages/temperatura.yaml"]["sha256"], git_hash
        )

        # Stale LIVE hash must refuse and leave content unchanged.
        live.write_text("new: 2\n", encoding="utf-8")
        results, applied = mf.apply_managed_files(
            ["packages/temperatura.yaml"],
            {"packages/temperatura.yaml": live_hash},  # stale
            {"packages/temperatura.yaml": git_hash},
            self.workdir,
            self.root,
            entries,
            unsafe_none,
            fake_ws,
            "token",
        )
        self.assertFalse(applied)
        self.assertTrue(any(not item["ok"] for item in results))

    def test_invalid_config_rolls_back(self):
        live = self.root / "packages" / "temperatura.yaml"
        live.write_text("old: 1\n", encoding="utf-8")
        git = self.workdir / "packages" / "temperatura.yaml"
        git.write_text("broken: true\n", encoding="utf-8")
        live_hash = mf.file_digest(live.read_bytes())
        git_hash = mf.file_digest(git.read_bytes())
        bases = {"schema_version": 1, "files": {}}
        mf.set_base_hash(bases, "packages/temperatura.yaml", live_hash)
        mf.save_bases(bases)
        entries = [mf.ManagedEntry("packages/temperatura.yaml", "package")]

        with mock.patch.object(
            mf, "ha_check_config", return_value={"result": "invalid", "errors": "bad yaml"}
        ):
            results, applied = mf.apply_managed_files(
                ["packages/temperatura.yaml"],
                {"packages/temperatura.yaml": live_hash},
                {"packages/temperatura.yaml": git_hash},
                self.workdir,
                self.root,
                entries,
                unsafe_none,
                lambda *a, **k: {},
                "token",
            )
        self.assertFalse(applied)
        self.assertEqual(live.read_text(encoding="utf-8"), "old: 1\n")
        self.assertEqual(
            mf.base_hash_for(mf.load_bases(), "packages/temperatura.yaml"), live_hash
        )
        self.assertTrue(any("rolled back" in item["message"] for item in results))

    def test_partial_failure_rolls_back_written_files(self):
        pkg = self.root / "packages" / "temperatura.yaml"
        www = self.root / "www" / "temperature-card.mjs"
        pkg.write_text("old-pkg\n", encoding="utf-8")
        www.write_text("old-www\n", encoding="utf-8")
        (self.workdir / "packages" / "temperatura.yaml").write_text("new-pkg\n", encoding="utf-8")
        (self.workdir / "www" / "temperature-card.mjs").write_text("new-www\n", encoding="utf-8")
        pkg_hash = mf.file_digest(pkg.read_bytes())
        www_hash = mf.file_digest(www.read_bytes())
        git_pkg = mf.file_digest(b"new-pkg\n")
        git_www = mf.file_digest(b"new-www\n")
        bases = {"schema_version": 1, "files": {}}
        mf.set_base_hash(bases, "packages/temperatura.yaml", pkg_hash)
        mf.set_base_hash(bases, "www/temperature-card.mjs", www_hash)
        mf.save_bases(bases)
        entries = [
            mf.ManagedEntry("packages/temperatura.yaml", "package"),
            mf.ManagedEntry("www/temperature-card.mjs", "frontend_module"),
        ]

        def boom_check(_token):
            raise RuntimeError("supervisor down")

        with mock.patch.object(mf, "ha_check_config", side_effect=boom_check):
            results, applied = mf.apply_managed_files(
                ["packages/temperatura.yaml", "www/temperature-card.mjs"],
                {
                    "packages/temperatura.yaml": pkg_hash,
                    "www/temperature-card.mjs": www_hash,
                },
                {
                    "packages/temperatura.yaml": git_pkg,
                    "www/temperature-card.mjs": git_www,
                },
                self.workdir,
                self.root,
                entries,
                unsafe_none,
                lambda *a, **k: {},
                "token",
            )
        self.assertFalse(applied)
        self.assertEqual(pkg.read_text(encoding="utf-8"), "old-pkg\n")
        self.assertEqual(www.read_text(encoding="utf-8"), "old-www\n")

    def test_policy_membership_enforced_separately_from_path_shape(self):
        # resolve_live_path only enforces shape/guards; Apply uses policy membership.
        path = mf.resolve_live_path(self.root, "packages/temperatura.yaml")
        self.assertEqual(path, (self.root / "packages" / "temperatura.yaml").resolve())
        allowed = {entry.path for entry in mf.load_policy(ROOT / "import" / "managed_files.yaml")[1]}
        self.assertNotIn("packages/ogrzewanie.yaml", allowed)
        self.assertIn("packages/temperatura.yaml", allowed)

    def test_policy_loader_v1_entries(self):
        root, entries = mf.load_policy(ROOT / "import" / "managed_files.yaml")
        self.assertEqual(root, Path("/homeassistant"))
        self.assertEqual(
            [(e.path, e.profile) for e in entries],
            [
                ("packages/temperatura.yaml", "package"),
                ("www/temperature-card.mjs", "frontend_module"),
            ],
        )

    def test_initialize_missing_bases(self):
        content = "same\n"
        (self.root / "packages" / "temperatura.yaml").write_text(content, encoding="utf-8")
        (self.workdir / "packages" / "temperatura.yaml").write_text(content, encoding="utf-8")
        entries = [mf.ManagedEntry("packages/temperatura.yaml", "package")]
        initialized = mf.initialize_missing_bases(
            self.workdir, self.root, entries, unsafe_none
        )
        self.assertEqual(initialized, ["packages/temperatura.yaml"])
        self.assertEqual(
            mf.base_hash_for(mf.load_bases(), "packages/temperatura.yaml"),
            mf.file_digest(content.encode()),
        )

    def test_initialize_does_not_adopt_drift(self):
        (self.root / "packages" / "temperatura.yaml").write_text("live\n", encoding="utf-8")
        (self.workdir / "packages" / "temperatura.yaml").write_text("git\n", encoding="utf-8")
        entries = [mf.ManagedEntry("packages/temperatura.yaml", "package")]
        changes, _ = mf.collect_managed_changes(
            self.workdir, self.root, entries, unsafe_none, stage_frontend=False
        )
        self.assertEqual(changes[0]["status"], MANAGED_BOOTSTRAP_STATUS)
        initialized = mf.initialize_missing_bases(
            self.workdir, self.root, entries, unsafe_none
        )
        self.assertEqual(initialized, [])
        self.assertIsNone(mf.base_hash_for(mf.load_bases(), "packages/temperatura.yaml"))

    def test_frontend_apply_publishes_canonical_not_only_staging(self):
        canonical = self.root / "www" / "temperature-card.mjs"
        canonical.write_text("OLD-MODULE\n", encoding="utf-8")
        git = self.workdir / "www" / "temperature-card.mjs"
        git.write_text("NEW-MODULE\n", encoding="utf-8")
        live_hash = mf.file_digest(canonical.read_bytes())
        git_hash = mf.file_digest(git.read_bytes())
        bases = {"schema_version": 1, "files": {}}
        mf.set_base_hash(bases, "www/temperature-card.mjs", live_hash)
        mf.save_bases(bases)
        entries = [mf.ManagedEntry("www/temperature-card.mjs", "frontend_module")]

        # Review staging must leave production untouched.
        staging = mf.ensure_frontend_staging(
            self.root, "www/temperature-card.mjs", git.read_bytes(), git_hash
        )
        self.assertEqual(canonical.read_text(encoding="utf-8"), "OLD-MODULE\n")
        self.assertIn(".config-sync-preview", staging["staged_relative"])

        results, applied = mf.apply_managed_files(
            ["www/temperature-card.mjs"],
            {"www/temperature-card.mjs": live_hash},
            {"www/temperature-card.mjs": git_hash},
            self.workdir,
            self.root,
            entries,
            unsafe_none,
            lambda *a, **k: {},
            "token",
        )
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(applied, ["www/temperature-card.mjs"])
        self.assertEqual(canonical.read_text(encoding="utf-8"), "NEW-MODULE\n")
        self.assertEqual(
            mf.base_hash_for(mf.load_bases(), "www/temperature-card.mjs"), git_hash
        )


class CollectTests(unittest.TestCase):
    def test_collect_marks_ready_and_strips_need_no_github_blob_in_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "git"
            (work / "packages").mkdir(parents=True)
            (root / "packages").mkdir()
            (work / "packages" / "temperatura.yaml").write_text("x: 1\n", encoding="utf-8")
            (root / "packages" / "temperatura.yaml").write_text("x: 0\n", encoding="utf-8")
            bases_path = root / "bases.json"
            with mock.patch.object(mf, "BASES_PATH", bases_path):
                live_hash = mf.file_digest(b"x: 0\n")
                mf.save_bases({
                    "schema_version": 1,
                    "files": {
                        "packages/temperatura.yaml": {"sha256": live_hash},
                    },
                })
                changes, _ = mf.collect_managed_changes(
                    work,
                    root,
                    [mf.ManagedEntry("packages/temperatura.yaml", "package")],
                    unsafe_none,
                    stage_frontend=False,
                )
            self.assertEqual(changes[0]["status"], "READY TO APPLY")
            self.assertTrue(changes[0]["selectable"])
            self.assertIn("github_data", changes[0])
            self.assertTrue(changes[0]["diff"]["blocks"])
            self.assertGreater(changes[0]["added"] + changes[0]["removed"], 0)


if __name__ == "__main__":
    unittest.main()
