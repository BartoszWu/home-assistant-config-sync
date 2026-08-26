import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

from dashboard_logic import (  # noqa: E402
    BOOTSTRAP_STATUS,
    MISSING_BASE_STATUS,
    classify,
    digest,
    is_empty_dashboard,
    matches_preview,
    parse_preview_hashes,
)


EMPTY_DASHBOARD = {
    "views": [
        {
            "type": "sections",
            "sections": [
                {
                    "type": "grid",
                    "cards": [
                        {"type": "heading", "heading": "New section"},
                    ],
                }
            ],
        }
    ]
}

DESIRED_DASHBOARD = {
    "views": [
        {
            "type": "sections",
            "title": "AGD",
            "sections": [
                {
                    "type": "grid",
                    "cards": [
                        {"type": "markdown", "content": "AGD"},
                    ],
                }
            ],
        }
    ]
}


class DashboardLogicTests(unittest.TestCase):
    def test_recognizes_semantically_empty_dashboard_shells(self):
        self.assertTrue(is_empty_dashboard(EMPTY_DASHBOARD))
        self.assertTrue(
            is_empty_dashboard(
                {
                    "views": [
                        {
                            "type": "sections",
                            "title": "Anything",
                            "sections": [{"type": "grid", "cards": []}],
                        }
                    ]
                }
            )
        )

    def test_rejects_dashboards_with_real_content_or_ambiguous_structure(self):
        self.assertFalse(is_empty_dashboard(DESIRED_DASHBOARD))
        self.assertFalse(
            is_empty_dashboard(
                {"views": [{"type": "sections"}, {"type": "sections"}]}
            )
        )
        self.assertFalse(
            is_empty_dashboard(
                {
                    "views": [
                        {
                            "type": "sections",
                            "sections": [
                                {
                                    "type": "grid",
                                    "cards": [
                                        {
                                            "type": "heading",
                                            "heading": "Status",
                                            "badges": ["sensor.example"],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )
        )

    def test_missing_base_and_empty_ha_is_bootstrap_ready(self):
        status, css, selectable, _ = classify(
            DESIRED_DASHBOARD, EMPTY_DASHBOARD, None
        )
        self.assertEqual(status, BOOTSTRAP_STATUS)
        self.assertEqual(css, "bootstrap")
        self.assertTrue(selectable)

    def test_missing_base_and_nonempty_ha_is_conflict(self):
        other = {
            "views": [
                {
                    "cards": [
                        {"type": "entity", "entity": "sensor.existing"}
                    ]
                }
            ]
        }
        status, _, selectable, _ = classify(DESIRED_DASHBOARD, other, None)
        self.assertEqual(status, "CONFLICT")
        self.assertFalse(selectable)

    def test_unsafe_configuration_wins_over_bootstrap(self):
        status, _, selectable, _ = classify(
            DESIRED_DASHBOARD,
            EMPTY_DASHBOARD,
            None,
            unsafe="Sensitive field detected",
        )
        self.assertEqual(status, "UNSAFE")
        self.assertFalse(selectable)

    def test_in_sync_without_base_can_recover_export(self):
        status, css, selectable, _ = classify(
            DESIRED_DASHBOARD, DESIRED_DASHBOARD, None
        )
        self.assertEqual(status, MISSING_BASE_STATUS)
        self.assertEqual(css, "missing-base")
        self.assertFalse(selectable)

    def test_existing_base_keeps_three_way_ready_logic(self):
        base = digest(EMPTY_DASHBOARD)
        status, _, selectable, _ = classify(
            DESIRED_DASHBOARD, EMPTY_DASHBOARD, base
        )
        self.assertEqual(status, "READY TO APPLY")
        self.assertTrue(selectable)

    def test_preview_hash_detects_any_ha_change_even_if_still_empty(self):
        preview = digest(EMPTY_DASHBOARD)
        changed = {
            "views": [
                {
                    "type": "sections",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "heading", "heading": "Another heading"}
                            ],
                        }
                    ],
                }
            ]
        }
        self.assertTrue(is_empty_dashboard(changed))
        self.assertFalse(matches_preview(changed, preview))

    def test_preview_hash_payload_is_validated(self):
        expected = digest(EMPTY_DASHBOARD)
        self.assertEqual(
            parse_preview_hashes([f"dashboard-agd.json:{expected}"]),
            {"dashboard-agd.json": expected},
        )
        self.assertEqual(
            parse_preview_hashes([f"dashboard:agd.json:{expected}"]),
            {"dashboard:agd.json": expected},
        )
        with self.assertRaises(ValueError):
            parse_preview_hashes(["dashboard-agd.json:not-a-hash"])
        with self.assertRaises(ValueError):
            parse_preview_hashes(
                [
                    f"dashboard-agd.json:{expected}",
                    f"dashboard-agd.json:{expected}",
                ]
            )


if __name__ == "__main__":
    unittest.main()
