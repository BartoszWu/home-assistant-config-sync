import importlib.util
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

    def test_invalid_custom_strategy_or_sensitive_config_fails_closed(self):
        for config in ({}, {"views": []}, {"strategy": {}},
                       {"views": [{"cards": [{"type": "custom:test"}]}]}):
            self.assertEqual(prepare_preview("test.json", BEFORE, config, lambda _: None), {"error": UNAVAILABLE})
        self.assertEqual(prepare_preview("test.json", BEFORE, AFTER, lambda _: "unsafe"), {"error": UNAVAILABLE})

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
