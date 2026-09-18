import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "import"))

from dashboard_logic import (  # noqa: E402
    BOOTSTRAP_STATUS,
    CREATE_STATUS,
    CREATE_REASON,
    MISSING_BASE_STATUS,
    NONCANONICAL_STATUS,
    NO_HYPHEN_REASON,
    UNSAVED_REASON,
    can_create_dashboard_path,
    classify,
    classify_missing_ha,
    classify_with_provenance,
    dashboard_registration_payload,
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

    def test_stale_export_base_does_not_block_canonical_git_only_change(self):
        from types import SimpleNamespace

        applied = DESIRED_DASHBOARD
        github = {
            "views": [
                {
                    "type": "sections",
                    "title": "AGD",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "markdown", "content": "AGD plus weather"},
                            ],
                        }
                    ],
                }
            ]
        }
        stale_export_base = digest(EMPTY_DASHBOARD)
        provenance = SimpleNamespace(
            canonical=True,
            content_hash=digest(applied),
            source_ref="main",
        )
        status, _, selectable, _, adopt = classify_with_provenance(
            github,
            applied,
            stale_export_base,
            reviewing_canonical=True,
            provenance=provenance,
        )
        self.assertEqual(status, "READY TO APPLY")
        self.assertTrue(selectable)
        self.assertFalse(adopt)

    def test_stale_export_base_still_reports_ha_only_canonical_drift(self):
        from types import SimpleNamespace

        applied = EMPTY_DASHBOARD
        live = DESIRED_DASHBOARD
        stale_export_base = digest({"views": []})
        provenance = SimpleNamespace(
            canonical=True,
            content_hash=digest(applied),
            source_ref="main",
        )
        status, _, selectable, _, adopt = classify_with_provenance(
            applied,
            live,
            stale_export_base,
            reviewing_canonical=True,
            provenance=provenance,
        )
        self.assertEqual(status, "CHANGED IN HA")
        self.assertFalse(selectable)
        self.assertFalse(adopt)

    def test_export_base_wins_when_it_matches_live_after_ha_round_trip(self):
        from types import SimpleNamespace

        live = DESIRED_DASHBOARD
        github = {
            "views": [
                {
                    "type": "sections",
                    "title": "AGD",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "markdown", "content": "newer git"},
                            ],
                        }
                    ],
                }
            ]
        }
        older_apply = EMPTY_DASHBOARD
        provenance = SimpleNamespace(
            canonical=True,
            content_hash=digest(older_apply),
            source_ref="main",
        )
        status, _, selectable, _, adopt = classify_with_provenance(
            github,
            live,
            digest(live),
            reviewing_canonical=True,
            provenance=provenance,
        )
        self.assertEqual(status, "READY TO APPLY")
        self.assertTrue(selectable)
        self.assertFalse(adopt)

    def test_canonical_true_conflict_when_git_and_ha_both_left_last_apply(self):
        from types import SimpleNamespace

        applied = EMPTY_DASHBOARD
        github = DESIRED_DASHBOARD
        live = {
            "views": [
                {
                    "type": "sections",
                    "title": "manual",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "markdown", "content": "manual HA"},
                            ],
                        }
                    ],
                }
            ]
        }
        provenance = SimpleNamespace(
            canonical=True,
            content_hash=digest(applied),
            source_ref="main",
        )
        status, _, selectable, _, adopt = classify_with_provenance(
            github,
            live,
            digest({"views": [{"title": "even-older-export"}]}),
            reviewing_canonical=True,
            provenance=provenance,
        )
        self.assertEqual(status, "CONFLICT")
        self.assertFalse(selectable)
        self.assertFalse(adopt)

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


class MissingHaDashboardTests(unittest.TestCase):
    def test_unregistered_hyphenated_path_is_create_ready(self):
        status, css, selectable, reason = classify_missing_ha(
            "dashboard-diagnostyka", registered=False
        )
        self.assertEqual(status, CREATE_STATUS)
        self.assertEqual(css, "bootstrap")
        self.assertTrue(selectable)
        self.assertEqual(reason, CREATE_REASON)

    def test_registered_without_saved_config_is_bootstrap_ready(self):
        status, css, selectable, reason = classify_missing_ha(
            "dashboard-diagnostyka", registered=True
        )
        self.assertEqual(status, BOOTSTRAP_STATUS)
        self.assertTrue(selectable)
        self.assertEqual(reason, UNSAVED_REASON)

    def test_default_lovelace_without_config_does_not_create(self):
        status, _, selectable, reason = classify_missing_ha(None, registered=False)
        self.assertEqual(status, BOOTSTRAP_STATUS)
        self.assertTrue(selectable)
        self.assertEqual(reason, UNSAVED_REASON)

    def test_unregistered_path_without_hyphen_is_blocked(self):
        status, css, selectable, reason = classify_missing_ha("map", registered=False)
        self.assertEqual(status, "CONFLICT")
        self.assertEqual(css, "conflict")
        self.assertFalse(selectable)
        self.assertEqual(reason, NO_HYPHEN_REASON)

    def test_create_path_requires_hyphenated_slug(self):
        self.assertTrue(can_create_dashboard_path("dashboard-diagnostyka"))
        for value in ("map", "Diagnostyka", "dashboard/diagnostyka", "", None, 1):
            self.assertFalse(can_create_dashboard_path(value), value)

    def test_registration_payload_uses_first_view_title_and_icon(self):
        self.assertEqual(
            dashboard_registration_payload(
                "dashboard-diagnostyka",
                {
                    "views": [
                        {
                            "title": "Diagnostyka",
                            "icon": "mdi:heart-pulse",
                            "path": "diagnostyka",
                        }
                    ]
                },
            ),
            {
                "url_path": "dashboard-diagnostyka",
                "title": "Diagnostyka",
                "icon": "mdi:heart-pulse",
                "show_in_sidebar": True,
                "require_admin": False,
            },
        )

    def test_registration_payload_falls_back_to_humanized_url_path(self):
        self.assertEqual(
            dashboard_registration_payload("dashboard-foo-bar", {"views": []}),
            {
                "url_path": "dashboard-foo-bar",
                "title": "Foo Bar",
                "show_in_sidebar": True,
                "require_admin": False,
            },
        )

    def test_missing_ha_preview_hash_is_stable(self):
        preview = digest(None)
        self.assertTrue(matches_preview(None, preview))
        self.assertFalse(matches_preview(EMPTY_DASHBOARD, preview))


if __name__ == "__main__":
    unittest.main()
