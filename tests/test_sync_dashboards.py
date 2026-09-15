import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "export"))

from deployment_provenance import empty_store, record_artifacts
from sync_dashboards import digest, load_json, sync, write_json


FEATURE_SHA = "a" * 40
MAIN_SHA = "b" * 40
DASH_1 = "dashboard-one.json"
DASH_2 = "dashboard-two.json"


def dashboard(title):
    return {"views": [{"title": title}]}


class SyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.current = self.root / "current"
        (self.repo / "dashboards").mkdir(parents=True)
        (self.repo / "state").mkdir()
        self.current.mkdir()
        self.main_a = dashboard("A")
        self.live_b = dashboard("B")
        self.live_c = dashboard("C")
        write_json(self.repo / "dashboards" / DASH_1, self.main_a)
        write_json(
            self.repo / "state/dashboard-bases.json",
            {"schema": 1, "dashboards": {DASH_1: {"sha256": digest(self.main_a)}}},
        )

    def write_live(self, name, value):
        write_json(self.current / name, value)

    def feature_store(self, *names, content_hash=None):
        hashes = {}
        for name in names:
            hashes[name] = content_hash or digest(self.live_b)
        return record_artifacts(
            empty_store(),
            source_ref="feature/temp-redesign",
            source_kind="branch",
            commit_sha=FEATURE_SHA,
            dashboards=hashes,
        )

    def test_canonical_main_manual_ha_change_still_exports(self):
        self.write_live(DASH_1, self.live_b)
        store = record_artifacts(
            empty_store(),
            source_ref="main",
            source_kind="branch",
            commit_sha=MAIN_SHA,
            dashboards={DASH_1: digest(self.main_a)},
        )
        outcomes = sync(self.repo, self.current, provenance=store)
        self.assertEqual(outcomes[DASH_1]["action"], "HA_CHANGE")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.live_b)
        bases = load_json(self.repo / "state/dashboard-bases.json")
        self.assertEqual(bases["dashboards"][DASH_1]["sha256"], digest(self.live_b))

    def test_feature_deployment_does_not_write_main(self):
        self.write_live(DASH_1, self.live_b)
        before_base = load_json(self.repo / "state/dashboard-bases.json")
        outcomes = sync(self.repo, self.current, provenance=self.feature_store(DASH_1))
        self.assertEqual(outcomes[DASH_1]["action"], "SKIPPED")
        self.assertIn("feature/temp-redesign", outcomes[DASH_1]["reason"])
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.main_a)
        self.assertEqual(
            load_json(self.repo / "state/dashboard-bases.json"), before_base
        )

    def test_mixed_dashboards_sync_only_canonical(self):
        write_json(self.repo / "dashboards" / DASH_2, self.main_a)
        bases = load_json(self.repo / "state/dashboard-bases.json")
        bases["dashboards"][DASH_2] = {"sha256": digest(self.main_a)}
        write_json(self.repo / "state/dashboard-bases.json", bases)
        self.write_live(DASH_1, self.live_b)
        self.write_live(DASH_2, self.live_b)
        store = record_artifacts(
            empty_store(),
            source_ref="main",
            source_kind="branch",
            commit_sha=MAIN_SHA,
            dashboards={DASH_1: digest(self.main_a)},
        )
        store = record_artifacts(
            store,
            source_ref="feature/temp-redesign",
            source_kind="branch",
            commit_sha=FEATURE_SHA,
            dashboards={DASH_2: digest(self.live_b)},
        )
        outcomes = sync(self.repo, self.current, provenance=store)
        self.assertEqual(outcomes[DASH_1]["action"], "HA_CHANGE")
        self.assertEqual(outcomes[DASH_2]["action"], "SKIPPED")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.live_b)
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_2), self.main_a)

    def test_feature_plus_manual_drift_still_skips(self):
        self.write_live(DASH_1, self.live_c)
        outcomes = sync(
            self.repo,
            self.current,
            provenance=self.feature_store(DASH_1, content_hash=digest(self.live_b)),
        )
        self.assertEqual(outcomes[DASH_1]["action"], "SKIPPED")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.main_a)

    def test_feature_does_not_shift_canonical_base_on_same_or_skip(self):
        write_json(self.repo / "dashboards" / DASH_1, self.live_b)
        write_json(
            self.repo / "state/dashboard-bases.json",
            {"schema": 1, "dashboards": {DASH_1: {"sha256": digest(self.main_a)}}},
        )
        self.write_live(DASH_1, self.live_b)
        outcomes = sync(self.repo, self.current, provenance=self.feature_store(DASH_1))
        self.assertEqual(outcomes[DASH_1]["action"], "SKIPPED")
        bases = load_json(self.repo / "state/dashboard-bases.json")
        self.assertEqual(bases["dashboards"][DASH_1]["sha256"], digest(self.main_a))

    def test_scheduled_export_reuses_cached_non_canonical_decision(self):
        self.write_live(DASH_1, self.live_b)
        first = sync(self.repo, self.current, provenance=self.feature_store(DASH_1))
        second = sync(self.repo, self.current, provenance=self.feature_store(DASH_1))
        self.assertEqual(first[DASH_1]["action"], "SKIPPED")
        self.assertEqual(second[DASH_1]["action"], "SKIPPED")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.main_a)

    def test_legacy_missing_provenance_keeps_previous_export_behavior(self):
        self.write_live(DASH_1, self.live_b)
        outcomes = sync(self.repo, self.current, provenance=empty_store())
        self.assertEqual(outcomes[DASH_1]["action"], "HA_CHANGE")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.live_b)

    def test_stale_canonical_cache_skips_when_shared_unavailable(self):
        from deployment_provenance import load_export_store, save_store

        self.write_live(DASH_1, self.live_b)
        shared = self.root / ".config-sync/live-dashboards.json"
        cache = self.root / "export-cache.json"
        save_store(
            record_artifacts(
                empty_store(),
                source_ref="main",
                source_kind="branch",
                commit_sha=MAIN_SHA,
                dashboards={DASH_1: digest(self.main_a)},
            ),
            shared_path=shared,
        )
        load_export_store(shared_path=shared, cache_path=cache)
        shared.unlink()
        (shared.parent / "guard-initialized.json").unlink()
        store = load_export_store(shared_path=shared, cache_path=cache)
        outcomes = sync(self.repo, self.current, provenance=store)
        self.assertEqual(outcomes[DASH_1]["action"], "SKIPPED")
        self.assertIn("unavailable", outcomes[DASH_1]["reason"])
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.main_a)

    def test_unavailable_guard_skips_all_dashboards(self):
        from deployment_provenance import unavailable_store

        write_json(self.repo / "dashboards" / DASH_2, self.main_a)
        self.write_live(DASH_1, self.live_b)
        self.write_live(DASH_2, self.live_b)
        outcomes = sync(
            self.repo, self.current, provenance=unavailable_store(unreadable=True)
        )
        self.assertEqual(outcomes[DASH_1]["action"], "SKIPPED")
        self.assertEqual(outcomes[DASH_2]["action"], "SKIPPED")
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_1), self.main_a)
        self.assertEqual(load_json(self.repo / "dashboards" / DASH_2), self.main_a)
