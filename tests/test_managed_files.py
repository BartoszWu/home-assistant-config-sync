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
COMMIT_SHA = "7ad83f2" + "a" * 33
COMMIT_DIR = COMMIT_SHA[:12]


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
            self.root, "www/temperature-card.mjs", data, digest, COMMIT_SHA
        )
        self.assertEqual(canonical.read_text(encoding="utf-8"), "OLD")
        staged = self.root / "www" / mf.PREVIEW_DIRNAME / COMMIT_DIR / "temperature-card.mjs"
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), data)
        self.assertIn("/local/.config-sync-preview/", staging["preview_url"])
        self.assertIn(COMMIT_DIR, staging["preview_url"])

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
        allowed = {entry.path for entry in mf.load_policy(ROOT / "import" / "managed_files.yaml").exact}
        self.assertNotIn("packages/ogrzewanie.yaml", allowed)
        self.assertIn("packages/temperatura.yaml", allowed)

    def test_policy_loader_v1_entries(self):
        policy = mf.load_policy(ROOT / "import" / "managed_files.yaml")
        self.assertEqual(policy.ha_root, Path("/homeassistant"))
        self.assertEqual(
            [(e.path, e.profile) for e in policy.exact],
            [
                ("packages/temperatura.yaml", "package"),
                ("www/temperature-card.mjs", "frontend_module"),
            ],
        )
        self.assertEqual(len(policy.prefixes), 1)
        self.assertEqual(policy.prefixes[0].prefix, "www/dashboard/")
        self.assertEqual(policy.prefixes[0].extensions, (".js", ".mjs"))
        self.assertEqual(policy.prefixes[0].profile, "frontend_module")

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
            self.root, "www/temperature-card.mjs", git.read_bytes(), git_hash, COMMIT_SHA
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

    def test_unique_id_warns_but_stays_selectable_without_base(self):
        yaml_text = "template:\n  - sensor:\n      - unique_id: synthetic_unique\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "git"
            (work / "packages").mkdir(parents=True)
            (root / "packages").mkdir()
            (work / "packages" / "temperatura.yaml").write_text(yaml_text, encoding="utf-8")
            (root / "packages" / "temperatura.yaml").write_text("x: 0\n", encoding="utf-8")
            bases_path = root / "bases.json"
            with mock.patch.object(mf, "BASES_PATH", bases_path):
                changes, _ = mf.collect_managed_changes(
                    work,
                    root,
                    [mf.ManagedEntry("packages/temperatura.yaml", "package")],
                    unsafe_none,
                    stage_frontend=False,
                )
            self.assertEqual(changes[0]["status"], MANAGED_BOOTSTRAP_STATUS)
            self.assertTrue(changes[0]["selectable"])
            self.assertEqual(changes[0]["warnings"][0]["field"], "unique_id")
            self.assertEqual(changes[0]["warnings"][0]["line"], 3)
            self.assertNotIn("synthetic_unique", str(changes[0]["warnings"]))


class PrefixDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "git"
        (self.work / "www" / "dashboard" / "shared").mkdir(parents=True)
        (self.root / "www" / "dashboard" / "shared").mkdir(parents=True)
        self.policy = mf.ManagedPolicy(
            ha_root=self.root,
            exact=(mf.ManagedEntry("www/temperature-card.mjs", "frontend_module"),),
            prefixes=(
                mf.PrefixRule("www/dashboard/", (".js", ".mjs"), "frontend_module"),
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_discovers_nested_js_and_mjs_under_prefix(self):
        (self.work / "www" / "temperature-card.mjs").write_text("legacy\n", encoding="utf-8")
        (self.work / "www" / "dashboard" / "home-hero.mjs").write_text(
            'import "./shared/utils.mjs";\n', encoding="utf-8"
        )
        (self.work / "www" / "dashboard" / "room-grid.js").write_text("export const grid = 1;\n", encoding="utf-8")
        (self.work / "www" / "dashboard" / "shared" / "utils.mjs").write_text("export const x = 1;\n", encoding="utf-8")
        (self.work / "www" / "dashboard" / "notes.yaml").write_text("nope: true\n", encoding="utf-8")
        (self.work / "www" / "other.mjs").write_text("outside\n", encoding="utf-8")
        discovered = mf.discover_managed_entries(self.work, self.policy)
        paths = [entry.path for entry in discovered]
        self.assertEqual(
            paths,
            [
                "www/dashboard/home-hero.mjs",
                "www/dashboard/room-grid.js",
                "www/dashboard/shared/utils.mjs",
                "www/temperature-card.mjs",
            ],
        )

    def test_rejects_yaml_outside_prefix_and_traversal(self):
        with self.assertRaises(ValueError):
            mf.validate_prefix_member("www/dashboard/notes.yaml", self.policy.prefixes[0])
        with self.assertRaises(ValueError):
            mf.validate_prefix_member("www/other.mjs", self.policy.prefixes[0])
        with self.assertRaises(ValueError):
            mf.validate_policy_path("www/dashboard/../other.mjs")
        with self.assertRaises(ValueError):
            mf.validate_policy_path("../www/dashboard/home-hero.mjs")

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.tmp.name + "_outside")
        outside.mkdir()
        secret = outside / "secret.mjs"
        secret.write_text("nope", encoding="utf-8")
        link = self.root / "www" / "dashboard" / "escaped.mjs"
        link.symlink_to(secret)
        with self.assertRaises(ValueError):
            mf.resolve_live_path(self.root, "www/dashboard/escaped.mjs")

    def test_live_only_file_is_not_a_delete_candidate(self):
        (self.work / "www" / "dashboard" / "home-hero.mjs").write_text("git\n", encoding="utf-8")
        leftover = self.root / "www" / "dashboard" / "orphan.mjs"
        leftover.write_text("live-only\n", encoding="utf-8")
        discovered = mf.discover_managed_entries(self.work, self.policy)
        self.assertNotIn("www/dashboard/orphan.mjs", [entry.path for entry in discovered])
        self.assertEqual(leftover.read_text(encoding="utf-8"), "live-only\n")


class PrefixApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "www" / "dashboard" / "shared").mkdir(parents=True)
        self.workdir = self.root / "git"
        (self.workdir / "www" / "dashboard" / "shared").mkdir(parents=True)
        self.bases = self.root / "bases.json"
        self.backup = self.root / "backups"
        self.journal = self.root / "journal.json"
        self.last_apply = self.root / "last-apply.json"
        self.patches = [
            mock.patch.object(mf, "BASES_PATH", self.bases),
            mock.patch.object(mf, "BACKUP_ROOT", self.backup),
            mock.patch.object(mf, "JOURNAL_PATH", self.journal),
            mock.patch.object(mf, "LAST_APPLY_PATH", self.last_apply),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.tmp.cleanup()

    def test_staging_preserves_nested_structure_per_commit(self):
        data = b'import "./shared/utils.mjs";\n'
        digest = mf.file_digest(data)
        other = "8bd33aa" + "b" * 33
        staging = mf.ensure_frontend_staging(
            self.root, "www/dashboard/home-hero.mjs", data, digest, COMMIT_SHA
        )
        nested = (
            self.root / "www" / mf.PREVIEW_DIRNAME / COMMIT_DIR
            / "dashboard" / "home-hero.mjs"
        )
        self.assertTrue(nested.is_file())
        self.assertNotEqual(
            nested,
            self.root / "www" / "dashboard" / "home-hero.mjs",
        )
        self.assertFalse((self.root / "www" / "dashboard" / "home-hero.mjs").exists())
        self.assertIn(f"/local/.config-sync-preview/{COMMIT_DIR}/dashboard/home-hero.mjs", staging["preview_url"])
        mf.ensure_frontend_staging(
            self.root,
            "www/dashboard/home-hero.mjs",
            b"other-branch\n",
            mf.file_digest(b"other-branch\n"),
            other,
        )
        self.assertEqual(nested.read_bytes(), data)
        self.assertEqual(
            (self.root / "www" / mf.PREVIEW_DIRNAME / other[:12] / "dashboard" / "home-hero.mjs").read_bytes(),
            b"other-branch\n",
        )

    def test_multi_file_apply_publishes_selected_modules_only(self):
        live_hero = self.root / "www" / "dashboard" / "home-hero.mjs"
        live_grid = self.root / "www" / "dashboard" / "room-grid.mjs"
        live_utils = self.root / "www" / "dashboard" / "shared" / "utils.mjs"
        live_hero.write_text("old-hero\n", encoding="utf-8")
        live_grid.write_text("old-grid\n", encoding="utf-8")
        live_utils.write_text("old-utils\n", encoding="utf-8")
        (self.workdir / "www" / "dashboard" / "home-hero.mjs").write_text("new-hero\n", encoding="utf-8")
        (self.workdir / "www" / "dashboard" / "room-grid.mjs").write_text("new-grid\n", encoding="utf-8")
        (self.workdir / "www" / "dashboard" / "shared" / "utils.mjs").write_text("new-utils\n", encoding="utf-8")
        hashes = {
            "www/dashboard/home-hero.mjs": mf.file_digest(live_hero.read_bytes()),
            "www/dashboard/room-grid.mjs": mf.file_digest(live_grid.read_bytes()),
            "www/dashboard/shared/utils.mjs": mf.file_digest(live_utils.read_bytes()),
        }
        git_hashes = {
            "www/dashboard/home-hero.mjs": mf.file_digest(b"new-hero\n"),
            "www/dashboard/room-grid.mjs": mf.file_digest(b"new-grid\n"),
            "www/dashboard/shared/utils.mjs": mf.file_digest(b"new-utils\n"),
        }
        bases = {"schema_version": 1, "files": {}}
        for relative, digest in hashes.items():
            mf.set_base_hash(bases, relative, digest)
        mf.save_bases(bases)
        entries = [
            mf.ManagedEntry(path, "frontend_module") for path in hashes
        ]
        results, applied = mf.apply_managed_files(
            ["www/dashboard/home-hero.mjs", "www/dashboard/shared/utils.mjs"],
            hashes,
            git_hashes,
            self.workdir,
            self.root,
            entries,
            unsafe_none,
            lambda *a, **k: {},
            "token",
            source_ref="feature/dashboard-redesign",
            commit_sha=COMMIT_SHA,
        )
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(
            applied,
            ["www/dashboard/home-hero.mjs", "www/dashboard/shared/utils.mjs"],
        )
        self.assertEqual(live_hero.read_text(encoding="utf-8"), "new-hero\n")
        self.assertEqual(live_utils.read_text(encoding="utf-8"), "new-utils\n")
        self.assertEqual(live_grid.read_text(encoding="utf-8"), "old-grid\n")
        stored = mf.load_bases()["files"]["www/dashboard/home-hero.mjs"]
        self.assertEqual(stored["sha256"], git_hashes["www/dashboard/home-hero.mjs"])
        self.assertEqual(stored["last_applied"]["source_ref"], "feature/dashboard-redesign")
        self.assertEqual(stored["last_applied"]["commit_sha"], COMMIT_SHA)

    def test_apply_does_not_delete_live_file_missing_from_git(self):
        leftover = self.root / "www" / "dashboard" / "orphan.mjs"
        leftover.write_text("keep-me\n", encoding="utf-8")
        live = self.root / "www" / "dashboard" / "home-hero.mjs"
        live.write_text("old\n", encoding="utf-8")
        (self.workdir / "www" / "dashboard" / "home-hero.mjs").write_text("new\n", encoding="utf-8")
        live_hash = mf.file_digest(b"old\n")
        git_hash = mf.file_digest(b"new\n")
        bases = {"schema_version": 1, "files": {}}
        mf.set_base_hash(bases, "www/dashboard/home-hero.mjs", live_hash)
        mf.save_bases(bases)
        results, applied = mf.apply_managed_files(
            ["www/dashboard/home-hero.mjs"],
            {"www/dashboard/home-hero.mjs": live_hash},
            {"www/dashboard/home-hero.mjs": git_hash},
            self.workdir,
            self.root,
            [mf.ManagedEntry("www/dashboard/home-hero.mjs", "frontend_module")],
            unsafe_none,
            lambda *a, **k: {},
            "token",
            source_ref="main",
            commit_sha=COMMIT_SHA,
        )
        self.assertTrue(applied)
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(leftover.read_text(encoding="utf-8"), "keep-me\n")

    def test_relative_import_warning_does_not_block_apply(self):
        (self.workdir / "www" / "dashboard" / "home-hero.mjs").write_text(
            'import "./shared/utils.mjs";\nexport const hero = 1;\n',
            encoding="utf-8",
        )
        (self.workdir / "www" / "dashboard" / "shared" / "utils.mjs").write_text(
            "export const x = 1;\n", encoding="utf-8"
        )
        changes, _ = mf.collect_managed_changes(
            self.workdir,
            self.root,
            [
                mf.ManagedEntry("www/dashboard/home-hero.mjs", "frontend_module"),
                mf.ManagedEntry("www/dashboard/shared/utils.mjs", "frontend_module"),
            ],
            unsafe_none,
            stage_frontend=False,
            commit_sha=COMMIT_SHA,
        )
        hero = next(item for item in changes if item["relative"].endswith("home-hero.mjs"))
        self.assertTrue(hero["selectable"])
        self.assertTrue(any("shared/utils.mjs" in (warning.get("reason") or "") for warning in hero["warnings"]))


if __name__ == "__main__":
    unittest.main()
