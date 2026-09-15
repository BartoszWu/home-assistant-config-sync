import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

from dashboard_logic import (  # noqa: E402
    BOOTSTRAP_STATUS,
    MISSING_BASE_STATUS,
    NONCANONICAL_STATUS,
    classify,
    classify_with_provenance,
    digest,
    is_empty_dashboard,
    is_ephemeral_dashboard,
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

    def test_unsafe_configuration_does_not_block_bootstrap(self):
        status, _, selectable, _ = classify(
            DESIRED_DASHBOARD,
            EMPTY_DASHBOARD,
            None,
            unsafe="Sensitive field detected",
        )
        self.assertEqual(status, BOOTSTRAP_STATUS)
        self.assertTrue(selectable)

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

    def test_feature_live_on_main_is_not_treated_as_ha_export_candidate(self):
        from types import SimpleNamespace
        provenance = SimpleNamespace(
            canonical=False,
            content_hash=digest(DESIRED_DASHBOARD),
            source_ref="feature/redesign",
            short_sha=lambda: "aaaaaaa",
        )
        status, css, selectable, reason, adopt = classify_with_provenance(
            EMPTY_DASHBOARD,
            DESIRED_DASHBOARD,
            digest(EMPTY_DASHBOARD),
            reviewing_canonical=True,
            provenance=provenance,
        )
        self.assertEqual(status, NONCANONICAL_STATUS)
        self.assertEqual(css, "changed")
        self.assertFalse(selectable)
        self.assertFalse(adopt)
        self.assertIn("Canonical main sync is disabled", reason)

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


class EphemeralDashboardTests(unittest.TestCase):
    def test_matches_url_path_and_filename(self):
        self.assertTrue(is_ephemeral_dashboard("dashboard-preview"))
        self.assertTrue(is_ephemeral_dashboard("dashboard-preview.json"))

    def test_rejects_lookalikes_and_non_strings(self):
        for value in ("dashboard-preview2", "dashboard-preview2.json",
                      "dashboard-preview-old.json", "dashboard-temperatura.json",
                      "", None, 42, {"x": 1}):
            self.assertFalse(is_ephemeral_dashboard(value), value)


if __name__ == "__main__":
    unittest.main()
