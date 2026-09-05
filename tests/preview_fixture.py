"""Synthetic browser harness, never connected to Home Assistant or GitHub."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import"))

import app
from dashboard_logic import digest
from visual_preview import prepare_preview

before = {"views": [{"title": "Before", "cards": []}]}
after = {"views": [{"title": "After", "cards": []}]}
app.refresh_repo = lambda: "synthetic-fixture"
app.collect_changes = lambda: [{
    "name": "test-dashboard", "relative": "test-dashboard.json",
    "preview_desired_hash": digest(after),
    "current": before, "github": after, "preview_ha_hash": digest(before),
    "selectable": True, "status": "READY TO APPLY", "css": "ready", "reason": "",
    "rows": app.side_by_side(before, after)[0], "added": 1, "removed": 1,
    "visual": prepare_preview("test-dashboard.json", before, after, lambda _: None),
}]


@app.app.route("/test-dashboard/0")
def frontend():
    return (Path(__file__).parent / "fixtures/native-frontend.html").read_text()


# Exercise the real Ingress check with a local-only synthetic proxy address.
class FixtureProxy:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        environ["REMOTE_ADDR"] = "172.30.32.2"
        return self.application(environ, start_response)


if __name__ == "__main__":
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, FixtureProxy(app.app))
    print(server.server_port, flush=True)
    server.serve_forever()
