"""Domain grouping and honest availability states for the compact Import review."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))
from review_ui import configuration_groups, review_summary, affected_dashboard_views


def item(relative, *, kind="managed", status="READY TO APPLY", **extra):
    return {
        "relative": relative, "kind": kind, "status": status,
        "css": "ready", "selectable": status == "READY TO APPLY",
        "warnings": [], "added": 0, "removed": 0, **extra,
    }


class ReviewPresentation(unittest.TestCase):
    def test_groups_dashboard_components_without_losing_individual_selection(self):
        dashboard = item("dashboard-temperatura.json", kind="dashboard", github={
            "views": [{"title": "Temperatura"}],
        })
        managed = [item(path, profile=profile) for path, profile in (
            ("packages/temperatura.yaml", "package"),
            ("www/temperature-card.mjs", "frontend_module"),
            ("custom_templates/temperatura.jinja", "custom_template"),
            ("www/dashboard/temperatura/controls.mjs", "frontend_module"),
        )]
        groups = configuration_groups([dashboard], managed, [
            item("/local/temperature-card.mjs", kind="resource"),
        ])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["title"], "Temperatura")
        self.assertEqual(len(group["changed"]), 6)
        self.assertEqual(
            {i["relative"] for i in group["changed"]},
            {dashboard["relative"], *(i["relative"] for i in managed), "/local/temperature-card.mjs"},
        )
        self.assertEqual({i["prefix"] for i in group["changed"]}, {"", "managed_", "resource_"})

    def test_does_not_guess_relationship_for_unrelated_module(self):
        groups = configuration_groups(
            [item("dashboard-dom.json", kind="dashboard")],
            [item("www/dashboard/shared.mjs", profile="frontend_module")], [],
        )
        self.assertEqual([g["title"] for g in groups], ["Dom", "Shared"])

    def test_dashboard_with_multiple_tabs_uses_subject_instead_of_first_tab(self):
        groups = configuration_groups([item("dashboard-dom.json", kind="dashboard", github={
            "views": [{"title": "Salon"}, {"title": "Ogród"}],
        })], [], [])
        self.assertEqual(groups[0]["title"], "Dom")
        self.assertEqual(groups[0]["changed"][0]["affected_views"], ["Salon", "Ogród"])

    def test_local_ha_changes_and_warnings_are_not_reported_as_ready(self):
        groups = configuration_groups([
            item("dashboard-dom.json", kind="dashboard", status="CHANGED IN HA"),
            item("dashboard-test.json", kind="dashboard", warnings=[{"reason": "Review"}]),
        ], [], [])
        overview = review_summary(groups, [])
        self.assertEqual(overview["attention"], 2)
        self.assertEqual(overview["ready"], 1)
        self.assertEqual(overview["tone"], "attention")

    def test_unknown_application_and_partial_ha_reads_never_claim_everything_current(self):
        groups = configuration_groups([item("dashboard-dom.json", kind="dashboard", status="SAME")], [], [])
        self.assertEqual(review_summary(groups, [{"state": "unknown"}])["title"], "Konfiguracja bez zmian")
        self.assertEqual(review_summary(groups, [], incomplete=True)["tone"], "attention")
        self.assertEqual(review_summary(groups, [{"state": "same"}])["title"], "Wszystko jest aktualne")

    def test_keeps_synced_items_in_collapsed_section(self):
        groups = configuration_groups([], [item("packages/temperatura.yaml", status="IN SYNC — BASE NOT INITIALIZED")], [])
        self.assertFalse(groups[0]["changed"])
        self.assertEqual(groups[0]["unchanged"][0]["status_label"], "Potrzebna baza")

    def test_describes_changed_tabs_and_removed_tabs_without_showing_a_diff(self):
        item = {
            "current": {"views": [{"title": "Dom"}, {"title": "Ogród", "cards": [1]}]},
            "github": {"views": [{"title": "Dom"}, {"title": "Ogród", "cards": [2]}, {"title": "Nowa"}]},
        }
        self.assertEqual(affected_dashboard_views(item), ["Ogród", "Nowa"])
        item["github"]["views"] = [{"title": "Dom"}]
        self.assertEqual(affected_dashboard_views(item), ["Ogród"])
        self.assertEqual(affected_dashboard_views({}), [])

    def test_missing_base_is_visible_in_the_summary_even_when_contents_match(self):
        groups = configuration_groups([], [item("packages/temperatura.yaml", status="IN SYNC — BASE NOT INITIALIZED")], [])
        overview = review_summary(groups, [{"state": "same"}])
        self.assertEqual(overview["missing_base"], 1)
        self.assertEqual(overview["tone"], "attention")
        self.assertNotEqual(overview["title"], "Wszystko jest aktualne")

    def test_new_available_versions_are_counted_even_with_a_saved_job(self):
        for state in ("review", "running", "failed"):
            with self.subTest(state=state):
                summary = review_summary([], [{"state": state, "has_new_versions": True}])
                self.assertEqual(summary["updates"], 1)
                self.assertNotEqual(summary["tone"], "current")
        summary = review_summary([], [{"state": "review", "has_new_versions": True}])
        self.assertEqual(summary["title"], "Są nowe rzeczy do wgrania")

    def test_invalid_dashboard_containers_preserve_error_without_crashing_grouping(self):
        for invalid in (None, [], "bad", 1, {"views": None}, {"views": {}}, {"views": "bad"}, {"views": [None]}, {"views": ["bad"]}):
            with self.subTest(invalid=invalid):
                bad = item("dashboard-bad.json", kind="dashboard", status="ERROR", github=invalid, current=invalid)
                good = item("dashboard-good.json", kind="dashboard", github={"views": [{"title": "Good"}]})
                groups = configuration_groups([bad, good], [], [])
                self.assertEqual(groups[0]["changed"][0]["affected_views"], [])
                self.assertFalse(groups[0]["changed"][0]["selectable"])
                self.assertEqual(groups[1]["title"], "Good")
                self.assertEqual(review_summary(groups, [])["attention"], 1)

    def test_application_only_update_is_visible_in_initial_summary(self):
        summary = review_summary([], [{"state": "update"}])
        self.assertEqual(summary["updates"], 1)
        self.assertEqual(summary["title"], "Są nowe rzeczy do wgrania")
