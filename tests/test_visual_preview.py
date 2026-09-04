import importlib.util
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))

from visual_preview import UNAVAILABLE, prepare_preview


BEFORE = {"views": [{"title": "Before", "cards": []}]}
AFTER = {"views": [{"title": "After", "cards": []}]}


class PreviewPreparationTests(unittest.TestCase):
    def test_dashboard_gets_visual_without_mutating_input(self):
        import copy
        original = copy.deepcopy((BEFORE, AFTER))
        preview = prepare_preview("test-dashboard.json", BEFORE, AFTER, lambda _: None)
        self.assertEqual(preview["path"], "/test-dashboard/0")
        self.assertEqual((BEFORE, AFTER), original)

    def test_non_dashboard_path_gets_no_visual(self):
        for path in ("scripts/example.json", "../test.json", "dashboard.yaml"):
            self.assertIsNone(prepare_preview(path, BEFORE, AFTER, lambda _: None))

    def test_default_dashboard_aliases(self):
        for name in ("lovelace.json", "dashboard-lovelace.json"):
            self.assertEqual(prepare_preview(name, BEFORE, AFTER, lambda _: None)["path"], "/lovelace/0")

    def test_invalid_strategy_or_sensitive_config_fails_closed(self):
        for config in ({}, {"views": []}, {"strategy": {}}):
            self.assertEqual(prepare_preview("test.json", BEFORE, config, lambda _: None), {"error": UNAVAILABLE})
        self.assertEqual(prepare_preview("test.json", BEFORE, AFTER, lambda _: "unsafe"), {"error": UNAVAILABLE})

    def test_custom_cards_become_inert_placeholders_only_in_preview_copies(self):
        custom = {"type": "custom:apexcharts-card", "series": [{"entity": "sensor.example"}],
                  "grid_options": {"columns": 12, "rows": 4},
                  "tap_action": {"action": "perform-action", "perform_action": "light.turn_on"}}
        current = {"views": [{"cards": [custom, {"type": "entities", "entities": ["sensor.example"]}]}]}
        desired = copy.deepcopy(current)
        original = copy.deepcopy((current, desired))
        preview = prepare_preview("test-dashboard.json", current, desired, lambda _: None)
        self.assertNotIn("error", preview)
        for side in ("before", "after"):
            placeholder = preview[side]["views"][0]["cards"][0]
            self.assertEqual(placeholder["type"], "button")
            self.assertIn("custom:apexcharts-card", placeholder["name"])
            self.assertEqual(placeholder["grid_options"], {"columns": 12, "rows": 4})
            for action in ("tap_action", "hold_action", "double_tap_action"):
                self.assertEqual(placeholder[action], {"action": "none"})
            self.assertNotIn("series", placeholder)
            self.assertEqual(preview[side]["views"][0]["cards"][1], current["views"][0]["cards"][1])
        self.assertEqual(preview["placeholder_counts"], {"before": 1, "after": 1})
        self.assertEqual(preview["placeholder_types"], ["custom:apexcharts-card"])
        preview["after"]["views"][0]["cards"][1]["entities"].append("sensor.other")
        self.assertEqual((current, desired), original)

    def test_nested_cards_in_sections_stacks_and_conditional_wrapper(self):
        desired = {"views": [{"sections": [{"type": "grid", "cards": [
            {"type": "vertical-stack", "cards": [
                {"type": "conditional", "conditions": [], "card": {"type": "custom:test"}},
                {"type": "custom:test", "cards": [{"type": "custom:internal"}]},
            ]},
        ]}]}]}
        preview = prepare_preview("test.json", BEFORE, desired, lambda _: None)
        self.assertNotIn("error", preview)
        cards = preview["after"]["views"][0]["sections"][0]["cards"][0]["cards"]
        self.assertEqual(cards[0]["card"]["type"], "button")
        self.assertEqual(cards[1]["type"], "button")
        self.assertEqual(preview["placeholder_counts"], {"before": 0, "after": 2})

    def test_custom_views_badges_and_entity_rows_are_not_replaced_with_cards(self):
        for view in ({"type": "custom:view", "cards": []},
                     {"badges": [{"type": "custom:badge"}], "cards": []},
                     {"cards": [{"type": "entities", "entities": [{"type": "custom:row"}]}]}):
            self.assertEqual(prepare_preview("test.json", BEFORE, {"views": [view]}, lambda _: None),
                             {"error": UNAVAILABLE})

    def test_placeholder_does_not_bypass_sensitive_config_scan(self):
        desired = {"views": [{"cards": [{"type": "custom:test", "private_payload": "blocked"}]}]}
        self.assertEqual(prepare_preview("test.json", BEFORE, desired,
                                        lambda config: "unsafe" if config == desired else None),
                         {"error": UNAVAILABLE})

    def test_placeholder_drops_custom_payload_but_preserves_visibility(self):
        visibility = [{"condition": "screen", "media_query": "(min-width: 600px)"}]
        custom = {"type": "custom:test", "visibility": visibility, "content": "not previewed",
                  "styles": {}, "grid_options": {"columns": "full", "rows": "auto", "extra": "ignored"}}
        preview = prepare_preview("test.json", BEFORE, {"views": [{"cards": [custom]}]}, lambda _: None)
        placeholder = preview["after"]["views"][0]["cards"][0]
        self.assertEqual(placeholder["visibility"], visibility)
        self.assertIsNot(placeholder["visibility"], visibility)
        self.assertEqual(placeholder["grid_options"], {"columns": "full", "rows": "auto"})
        self.assertNotIn("content", placeholder)
        self.assertNotIn("styles", placeholder)

    def test_untrusted_custom_type_label_is_not_interpreted_as_markup(self):
        custom = {"type": "custom:<img src=x>"}
        preview = prepare_preview("test.json", BEFORE, {"views": [{"cards": [custom]}]}, lambda _: None)
        self.assertEqual(preview["placeholder_types"], ["custom card"])
        self.assertEqual(preview["after"]["views"][0]["cards"][0]["name"], "Preview omitted: custom card")

    def test_optional_preparation_failure_is_contained(self):
        def broken(_):
            raise RuntimeError("private error must not reach UI")
        self.assertEqual(prepare_preview("test.json", BEFORE, AFTER, broken), {"error": UNAVAILABLE})


HAS_RUNTIME = all(importlib.util.find_spec(module) for module in ("flask", "websocket"))


@unittest.skipUnless(HAS_RUNTIME, "Install Import's Flask/websocket-client dependencies for HTTP tests")
class PreviewHttpTests(unittest.TestCase):
    def setUp(self):
        import app
        from dashboard_logic import digest
        self.module = app
        self.client = app.app.test_client()
        self.change = {
            "name": "test-dashboard", "relative": "test-dashboard.json",
            "github": AFTER, "current": BEFORE, "base": digest(BEFORE),
            "preview_ha_hash": digest(BEFORE), "status": "READY TO APPLY",
            "css": "ready", "selectable": True, "reason": "",
            "rows": app.side_by_side(BEFORE, AFTER)[0], "added": 1, "removed": 1,
            "visual": prepare_preview("test-dashboard.json", BEFORE, AFTER, lambda _: None),
        }
        self.patches = [patch.object(app, "refresh_repo", return_value="test-commit"),
                        patch.object(app, "collect_changes", return_value=[self.change])]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)

    def request(self, path="/", **kwargs):
        return self.client.open(path, environ_overrides={"REMOTE_ADDR": "172.30.32.2"}, **kwargs)

    def test_dashboard_tabs_and_original_diff_are_present(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-preview-tab="visual"', html)
        self.assertIn('YAML diff', html)
        self.assertIn('HA current', html)
        self.assertIn('GitHub HEAD', html)
        # Progressive enhancement: diff is usable if JavaScript fails to load.
        self.assertIn('<div class="yaml-panel">', html)

    def test_resource_without_visual_retains_diff(self):
        self.change["visual"] = None
        html = self.request().get_data(as_text=True)
        self.assertNotIn('data-preview-tab="visual"', html)
        self.assertIn('HA current', html)

    def test_preview_error_does_not_disable_apply(self):
        self.change["visual"] = {"error": UNAVAILABLE}
        with patch.object(self.module, "save_dashboard") as save, \
             patch.object(self.module, "ha_dashboard_config", return_value=AFTER), \
             patch.object(self.module, "request_export") as export:
            response = self.request("/apply", method="POST", data={
                "selected": self.change["relative"],
                "preview_hash": self.change["relative"] + ":" + self.change["preview_ha_hash"],
            })
        self.assertIn("Applied and verified.", response.get_data(as_text=True))
        save.assert_called_once_with("test-dashboard.json", AFTER)
        export.assert_called_once_with(["test-dashboard.json"])

    def test_apply_and_diff_use_original_custom_card_not_preview_placeholder(self):
        desired = {"views": [{"cards": [{"type": "custom:apexcharts-card", "series": []}]}]}
        self.change["github"] = desired
        self.change["rows"] = self.module.side_by_side(BEFORE, desired)[0]
        self.change["visual"] = prepare_preview("test-dashboard.json", BEFORE, desired, lambda _: None)
        self.assertNotIn("error", self.change["visual"])
        html = self.request().get_data(as_text=True)
        self.assertIn("Partial preview", html)
        self.assertIn('"type": "custom:apexcharts-card"', "\n".join(row["right"] for row in self.change["rows"]))
        with patch.object(self.module, "save_dashboard") as save, \
             patch.object(self.module, "ha_dashboard_config", return_value=desired), \
             patch.object(self.module, "request_export"):
            response = self.request("/apply", method="POST", data={
                "selected": self.change["relative"],
                "preview_hash": self.change["relative"] + ":" + self.change["preview_ha_hash"],
            })
        self.assertIn("Applied and verified.", response.get_data(as_text=True))
        save.assert_called_once_with("test-dashboard.json", desired)
        self.assertEqual(desired["views"][0]["cards"][0]["type"], "custom:apexcharts-card")

    def test_review_does_not_save_production_or_create_temporary_resources(self):
        with patch.object(self.module, "ha_ws_call") as api, \
             patch.object(self.module, "save_dashboard") as save:
            self.request()
        api.assert_not_called()
        save.assert_not_called()

    def test_script_is_ingress_only(self):
        self.assertEqual(self.client.get("/static/visual-preview.mjs").status_code, 403)
        response = self.request("/static/visual-preview.mjs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.content_type)
        response.close()

    def test_embedded_json_cannot_close_script(self):
        self.change["visual"]["after"] = {"views": [{"title": "</script><img src=x>"}]}
        html = self.request().get_data(as_text=True)
        self.assertNotIn("</script><img src=x>", html)


if __name__ == "__main__":
    unittest.main()
