import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

import diff_view  # noqa: E402


class HunkDiffTests(unittest.TestCase):
    def test_identical_files_have_no_hunks(self):
        diff = diff_view.compare_text("a\nb\nc\n", "a\nb\nc\n")
        self.assertEqual(diff["added"], 0)
        self.assertEqual(diff["removed"], 0)
        self.assertEqual(diff["blocks"], [])

    def test_omits_distant_unchanged_lines(self):
        left = [f"keep-{i}" for i in range(40)]
        right = list(left)
        left[20] = "old-value"
        right[20] = "new-value"
        diff = diff_view.compare_lines(left, right)
        self.assertEqual(diff["added"], 1)
        self.assertEqual(diff["removed"], 1)
        kinds = [block["type"] for block in diff["blocks"]]
        self.assertEqual(kinds, ["gap", "hunk", "gap"])
        hunk = diff["blocks"][1]
        texts = [row["left"] for row in hunk["rows"]] + [row["right"] for row in hunk["rows"]]
        self.assertIn("old-value", texts)
        self.assertIn("new-value", texts)
        self.assertNotIn("keep-0", texts)
        self.assertNotIn("keep-39", texts)
        self.assertGreater(diff["blocks"][0]["count"], 3)
        self.assertIn("@@", hunk["header"])

    def test_nearby_changes_merge_into_one_hunk(self):
        left = [f"line-{i}" for i in range(20)]
        right = list(left)
        left[8] = "old-a"
        right[8] = "new-a"
        left[10] = "old-b"
        right[10] = "new-b"
        diff = diff_view.compare_lines(left, right)
        hunks = [block for block in diff["blocks"] if block["type"] == "hunk"]
        self.assertEqual(len(hunks), 1)

    def test_new_file_is_one_insert_hunk(self):
        diff = diff_view.compare_text("", "alpha\nbeta\n")
        self.assertEqual(diff["added"], 2)
        self.assertEqual(diff["removed"], 0)
        self.assertEqual([block["type"] for block in diff["blocks"]], ["hunk"])
        self.assertEqual(
            [row["right"] for row in diff["blocks"][0]["rows"]],
            ["alpha", "beta"],
        )

    def test_json_pretty_print_matches_previous_dashboard_diff(self):
        current = {"views": [{"title": "Before", "cards": []}]}
        github = {"views": [{"title": "After", "cards": []}]}
        diff = diff_view.compare_json(current, github)
        self.assertGreaterEqual(diff["added"], 1)
        self.assertGreaterEqual(diff["removed"], 1)
        rows = diff_view.hunk_rows(diff)
        self.assertTrue(any("Before" in row["left"] for row in rows))
        self.assertTrue(any("After" in row["right"] for row in rows))


class FileSummaryTests(unittest.TestCase):
    def test_lists_only_changed_review_items(self):
        dashboards = [
            {
                "relative": "home.json",
                "status": "READY TO APPLY",
                "css": "ready",
                "added": 4,
                "removed": 1,
            },
            {
                "relative": "same.json",
                "status": "SAME",
                "css": "same",
                "added": 0,
                "removed": 0,
            },
        ]
        managed = [
            {
                "relative": "packages/temperatura.yaml",
                "status": "READY TO APPLY",
                "css": "ready",
                "added": 12,
                "removed": 3,
            },
        ]
        summary = diff_view.summarize_changed_files(dashboards, managed)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["added"], 16)
        self.assertEqual(summary["removed"], 4)
        self.assertEqual(
            [item["path"] for item in summary["files"]],
            ["dashboards/home.json", "packages/temperatura.yaml"],
        )
        self.assertTrue(all(item["anchor"].startswith("file-") for item in summary["files"]))

    def test_in_sync_missing_base_is_not_a_changed_file(self):
        change = {
            "relative": "home.json",
            "status": "IN SYNC — BASE NOT INITIALIZED",
            "css": "missing-base",
            "added": 0,
            "removed": 0,
        }
        self.assertFalse(diff_view.is_changed_review(change))


if __name__ == "__main__":
    unittest.main()
