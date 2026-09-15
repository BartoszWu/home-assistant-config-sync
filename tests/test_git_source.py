import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

from git_source import (  # noqa: E402
    DEFAULT_SOURCE_REF,
    InvalidSourceRef,
    checkout_source,
    parse_requested_source,
    validate_branch_name,
    validate_commit_sha,
)


def run_git(command, cwd=None):
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
    ).stdout.strip()


def commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    run_git(["git", "add", "--", relative], cwd=repo)
    run_git(
        [
            "git",
            "-c", "user.email=test@example.test",
            "-c", "user.name=Test",
            "commit",
            "-m",
            message,
        ],
        cwd=repo,
    )
    return run_git(["git", "rev-parse", "HEAD"], cwd=repo)


class RefValidationTests(unittest.TestCase):
    def test_default_source_is_main(self):
        ref, kind = parse_requested_source(None, None)
        self.assertEqual(ref, DEFAULT_SOURCE_REF)
        self.assertEqual(kind, "branch")
        ref, kind = parse_requested_source("", "  ")
        self.assertEqual(ref, "main")

    def test_rejects_malicious_and_option_injection_refs(self):
        for bad in (
            "--upload-pack=evil",
            "-u",
            "; rm -rf /",
            "main;id",
            "refs/heads/main",
            "../foo",
            "foo/../bar",
            "HEAD",
            "feature/$(touch x)",
            "main@{0}",
            "origin:main",
            "*",
            "foo bar",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidSourceRef):
                    validate_branch_name(bad)
                with self.assertRaises(InvalidSourceRef):
                    parse_requested_source(bad, None)

    def test_accepts_feature_branches(self):
        for name in ("main", "feature/dashboard-redesign", "feature/temperature-v2"):
            self.assertEqual(validate_branch_name(name), name)

    def test_commit_sha_must_be_40_hex(self):
        sha = "7ad83f2" + "a" * 33
        self.assertEqual(validate_commit_sha(sha), sha)
        for bad in ("7ad83f2", "G" * 40, "7ad83f2aaaa", "--upload-pack=x" + "a" * 20):
            with self.assertRaises(InvalidSourceRef):
                validate_commit_sha(bad)


class CheckoutSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        self.origin = self.root / "origin.git"
        self.workdir = self.root / "review"
        self.src.mkdir()
        run_git(["git", "init", "-b", "main"], cwd=self.src)
        self.main_sha = commit_file(
            self.src,
            "dashboards/dashboard-dom.json",
            '{"views":[{"title":"main"}]}\n',
            "main dashboard",
        )
        commit_file(self.src, "www/temperature-card.mjs", "legacy\n", "legacy module")
        self.main_sha = run_git(["git", "rev-parse", "HEAD"], cwd=self.src)
        run_git(["git", "checkout", "-b", "feature/dashboard-redesign"], cwd=self.src)
        self.feature_sha = commit_file(
            self.src,
            "www/dashboard/home-hero.mjs",
            'import "./shared/utils.mjs";\n',
            "hero",
        )
        commit_file(
            self.src,
            "www/dashboard/shared/utils.mjs",
            "export const x = 1;\n",
            "utils",
        )
        self.feature_tip = run_git(["git", "rev-parse", "HEAD"], cwd=self.src)
        run_git(["git", "checkout", "main"], cwd=self.src)
        run_git(["git", "clone", "--bare", str(self.src), str(self.origin)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_resolves_to_sha_and_pins_preview_tree(self):
        revision = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
            source_ref="feature/dashboard-redesign",
        )
        self.assertEqual(revision.source_ref, "feature/dashboard-redesign")
        self.assertEqual(revision.commit_sha, self.feature_tip)
        self.assertEqual(revision.short_sha, self.feature_tip[:7])
        self.assertFalse(revision.stale)
        self.assertEqual(
            (self.workdir / "www/dashboard/home-hero.mjs").read_text(encoding="utf-8"),
            'import "./shared/utils.mjs";\n',
        )
        self.assertTrue((self.workdir / "dashboards/dashboard-dom.json").is_file())
        self.assertTrue((self.workdir / "www/dashboard/shared/utils.mjs").is_file())

    def test_default_branch_is_main(self):
        revision = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
        )
        self.assertEqual(revision.source_ref, "main")
        self.assertEqual(revision.commit_sha, self.main_sha)
        self.assertFalse((self.workdir / "www/dashboard/home-hero.mjs").exists())

    def test_explicit_commit_sha_checkouts_that_tree(self):
        revision = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
            source_ref=self.feature_sha,
        )
        self.assertEqual(revision.source_kind, "commit")
        self.assertEqual(revision.commit_sha, self.feature_sha)
        self.assertTrue((self.workdir / "www/dashboard/home-hero.mjs").is_file())
        self.assertFalse((self.workdir / "www/dashboard/shared/utils.mjs").exists())

    def test_branch_head_change_marks_reviewed_sha_stale(self):
        first = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
            source_ref="feature/dashboard-redesign",
        )
        run_git(["git", "checkout", "feature/dashboard-redesign"], cwd=self.src)
        new_tip = commit_file(self.src, "www/dashboard/room-grid.mjs", "grid\n", "grid")
        run_git(["git", "push", str(self.origin), "feature/dashboard-redesign"], cwd=self.src)
        second = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
            source_ref="feature/dashboard-redesign",
            pin_sha=first.commit_sha,
        )
        self.assertTrue(second.stale)
        self.assertEqual(second.reviewed_sha, first.commit_sha)
        self.assertEqual(second.commit_sha, new_tip)
        self.assertNotEqual(second.commit_sha, first.commit_sha)
        self.assertEqual(
            (self.workdir / "www/dashboard/room-grid.mjs").read_text(encoding="utf-8"),
            "grid\n",
        )

    def test_unknown_branch_is_rejected(self):
        with self.assertRaises(InvalidSourceRef):
            checkout_source(
                repo=str(self.origin),
                workdir=self.workdir,
                runner=run_git,
                source_ref="feature/not-a-real-branch",
            )

    def test_review_artifacts_share_one_sha(self):
        revision = checkout_source(
            repo=str(self.origin),
            workdir=self.workdir,
            runner=run_git,
            source_ref="feature/dashboard-redesign",
        )
        worktree_sha = run_git(["git", "rev-parse", "HEAD"], cwd=self.workdir)
        self.assertEqual(worktree_sha, revision.commit_sha)
        self.assertEqual(worktree_sha, self.feature_tip)


if __name__ == "__main__":
    unittest.main()
