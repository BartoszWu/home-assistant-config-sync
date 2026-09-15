import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "import"), str(ROOT / "export")]

from dashboard_logic import NONCANONICAL_STATUS, classify_with_provenance, digest
from deployment_provenance import (
    CANONICAL_BRANCH,
    GUARD_LEGACY,
    GUARD_READY,
    GUARD_UNAVAILABLE,
    SHARED_STATE_PATH,
    ArtifactProvenance,
    InvalidProvenance,
    ProvenanceStore,
    adopt_canonical,
    atomic_write_json,
    dashboard_sync_decision,
    empty_store,
    is_canonical_live,
    load_export_store,
    load_import_store,
    load_json_store,
    parse_store,
    provenance_label,
    record_artifacts,
    save_store,
)
from git_source import DEFAULT_SOURCE_REF


FEATURE_SHA = "a" * 40
MAIN_SHA = "b" * 40
HASH_A = "1" * 64
HASH_B = "2" * 64
HASH_C = "3" * 64


def feature_entry(**kwargs):
    payload = {
        "source_ref": "feature/temp-redesign",
        "source_kind": "branch",
        "commit_sha": FEATURE_SHA,
        "content_hash": HASH_B,
        "canonical": False,
        "applied_at": "2026-09-15T00:00:00+00:00",
    }
    payload.update(kwargs)
    return ArtifactProvenance(**payload)


class ProvenanceContractTests(unittest.TestCase):
    def test_canonical_branch_matches_import_default_source(self):
        self.assertEqual(CANONICAL_BRANCH, DEFAULT_SOURCE_REF)
        self.assertEqual(CANONICAL_BRANCH, "main")

    def test_repo_cannot_self_declare_feature_canonical(self):
        raw = {
            "schema_version": 1,
            "canonical_branch": "main",
            "updated_at": "2026-09-15T00:00:00+00:00",
            "dashboards": {
                "dashboard-temp.json": {
                    "source_ref": "feature/temp-redesign",
                    "source_kind": "branch",
                    "commit_sha": FEATURE_SHA,
                    "content_hash": HASH_B,
                    "canonical": True,
                    "applied_at": "2026-09-15T00:00:00+00:00",
                }
            },
            "managed_files": {},
        }
        store = parse_store(raw)
        entry = store.dashboards["dashboard-temp.json"]
        self.assertFalse(entry.canonical)
        self.assertFalse(is_canonical_live(entry))

    def test_missing_store_is_legacy_canonical(self):
        self.assertTrue(is_canonical_live(None))
        store = empty_store()
        self.assertEqual(store.guard_state, GUARD_LEGACY)
        self.assertFalse(store.guard_initialized)
        allowed, reason = dashboard_sync_decision("dash.json", store)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_shared_provenance_is_not_under_www_or_local(self):
        self.assertEqual(
            SHARED_STATE_PATH, Path("/homeassistant/.config-sync/live-dashboards.json")
        )
        self.assertNotIn("www", SHARED_STATE_PATH.parts)
        self.assertNotIn("local", SHARED_STATE_PATH.parts)

    def test_persistence_roundtrip_and_export_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "import.json"
            shared = root / ".config-sync/live-dashboards.json"
            cache = root / "export-cache.json"
            store = record_artifacts(
                empty_store(),
                source_ref="feature/temp-redesign",
                source_kind="branch",
                commit_sha=FEATURE_SHA,
                dashboards={"dashboard-temp.json": HASH_B},
            )
            self.assertEqual(store.guard_state, GUARD_READY)
            save_store(store, local_path=local, shared_path=shared)
            self.assertTrue((shared.parent / "guard-initialized.json").exists())
            reloaded = load_import_store(local_path=local, shared_path=shared)
            self.assertFalse(reloaded.dashboards["dashboard-temp.json"].canonical)
            exported = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(
                exported.dashboards["dashboard-temp.json"].commit_sha, FEATURE_SHA
            )
            self.assertEqual(exported.guard_state, GUARD_READY)
            self.assertTrue(cache.exists())
            shared.unlink()
            cached = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(cached.guard_state, GUARD_UNAVAILABLE)
            self.assertEqual(
                cached.dashboards["dashboard-temp.json"].source_ref,
                "feature/temp-redesign",
            )
            allowed, reason = dashboard_sync_decision("dashboard-temp.json", cached)
            self.assertFalse(allowed)
            self.assertIn("feature/temp-redesign", reason)

    def test_stale_canonical_cache_does_not_authorize_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / ".config-sync/live-dashboards.json"
            cache = root / "export-cache.json"
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="main",
                    source_kind="branch",
                    commit_sha=MAIN_SHA,
                    dashboards={"dash.json": HASH_A},
                ),
                shared_path=shared,
            )
            load_export_store(shared_path=shared, cache_path=cache)
            shared.unlink()
            (shared.parent / "guard-initialized.json").unlink()
            store = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(store.guard_state, GUARD_UNAVAILABLE)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("unavailable", reason)

    def test_cached_non_canonical_skips_when_shared_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / ".config-sync/live-dashboards.json"
            cache = root / "export-cache.json"
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="feature/temp-redesign",
                    source_kind="branch",
                    commit_sha=FEATURE_SHA,
                    dashboards={"dash.json": HASH_B},
                ),
                shared_path=shared,
            )
            load_export_store(shared_path=shared, cache_path=cache)
            shared.unlink()
            (shared.parent / "guard-initialized.json").unlink()
            store = load_export_store(shared_path=shared, cache_path=cache)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("feature/temp-redesign", reason)

    def test_true_legacy_installation_keeps_previous_export_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = load_export_store(
                shared_path=root / "missing.json",
                cache_path=root / "also-missing.json",
            )
            self.assertEqual(store.guard_state, GUARD_LEGACY)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertTrue(allowed)
            self.assertIsNone(reason)

    def test_corrupt_shared_fail_closes_even_with_canonical_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "live-dashboards.json"
            cache = root / "cache.json"
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="main",
                    source_kind="branch",
                    commit_sha=MAIN_SHA,
                    dashboards={"dash.json": HASH_A},
                ),
                local_path=cache,
            )
            shared.write_text("{not-json", encoding="utf-8")
            store = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(store.guard_state, GUARD_UNAVAILABLE)
            self.assertTrue(store.invalid)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("unreadable", reason)

    def test_invalid_shared_file_fail_closes_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "live-dashboards.json"
            shared.write_text("{not-json", encoding="utf-8")
            store = load_export_store(
                shared_path=shared, cache_path=Path(tmp) / "cache.json"
            )
            self.assertTrue(store.invalid)
            self.assertEqual(store.guard_state, GUARD_UNAVAILABLE)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("unreadable", reason)

    def test_cache_non_canonical_restricts_even_when_shared_claims_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "live-dashboards.json"
            cache = root / "cache.json"
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="feature/temp-redesign",
                    source_kind="branch",
                    commit_sha=FEATURE_SHA,
                    dashboards={"dash.json": HASH_B},
                ),
                local_path=cache,
            )
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="main",
                    source_kind="branch",
                    commit_sha=MAIN_SHA,
                    dashboards={"dash.json": HASH_B},
                ),
                shared_path=shared,
            )
            store = load_export_store(shared_path=shared, cache_path=cache)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("feature/temp-redesign", reason)

    def test_interrupted_atomic_write_does_not_publish_partial_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "live-dashboards.json"
            save_store(
                record_artifacts(
                    empty_store(),
                    source_ref="main",
                    source_kind="branch",
                    commit_sha=MAIN_SHA,
                    dashboards={"dash.json": HASH_A},
                ),
                shared_path=target,
            )
            previous = json.loads(target.read_text(encoding="utf-8"))

            def fail_replace(_src, _dst):
                raise OSError("simulated crash during replace")

            with patch("deployment_provenance.os.replace", side_effect=fail_replace):
                with self.assertRaises(OSError):
                    atomic_write_json(
                        target,
                        {"schema_version": 2, "dashboards": {"broken": True}},
                    )
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), previous)
            self.assertFalse(list(Path(tmp).glob("live-dashboards.json.*")))
            loaded = load_json_store(target)
            self.assertEqual(loaded.dashboards["dash.json"].content_hash, HASH_A)

            missing = Path(tmp) / "never-created.json"
            with patch("deployment_provenance.os.replace", side_effect=fail_replace):
                with self.assertRaises(OSError):
                    atomic_write_json(missing, {"schema_version": 2})
            self.assertFalse(missing.exists())
            leftovers = [
                path for path in Path(tmp).iterdir()
                if path.name.startswith("never-created.json.")
            ]
            self.assertEqual(leftovers, [])

    def test_symlink_shared_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "outside.json"
            target.write_text("{}", encoding="utf-8")
            shared = root / "live-dashboards.json"
            shared.symlink_to(target)
            store = load_export_store(
                shared_path=shared, cache_path=root / "cache.json"
            )
            self.assertTrue(store.invalid)

    def test_adopt_requires_matching_hashes_in_classifier(self):
        main = {"views": [{"title": "B"}]}
        live = {"views": [{"title": "B"}]}
        other = {"views": [{"title": "A"}]}
        status, _, _, _, adopt = classify_with_provenance(
            main, live, HASH_A, reviewing_canonical=True, provenance=feature_entry()
        )
        self.assertTrue(adopt)
        self.assertEqual(status, "SAME")
        status, css, selectable, _, adopt = classify_with_provenance(
            other, live, HASH_A, reviewing_canonical=True, provenance=feature_entry()
        )
        self.assertFalse(adopt)
        self.assertEqual(status, NONCANONICAL_STATUS)
        self.assertEqual(css, "changed")
        self.assertFalse(selectable)

    def test_feature_manual_drift_is_changed_in_ha_not_export_prompt(self):
        git = {"views": [{"title": "B"}]}
        live = {"views": [{"title": "C"}]}
        status, _, selectable, reason, adopt = classify_with_provenance(
            git,
            live,
            HASH_A,
            reviewing_canonical=False,
            provenance=feature_entry(content_hash=digest(git)),
        )
        self.assertEqual(status, "CHANGED IN HA")
        self.assertFalse(selectable)
        self.assertFalse(adopt)
        self.assertIn("Export will not publish", reason)

    def test_label_and_adopt_helper(self):
        entry = feature_entry()
        self.assertEqual(provenance_label(entry), "NON-CANONICAL")
        store = ProvenanceStore(dashboards={"dash.json": entry})
        adopted = adopt_canonical(
            store, relative="dash.json", commit_sha=MAIN_SHA, content_hash=HASH_B
        )
        self.assertTrue(adopted.dashboards["dash.json"].canonical)
        self.assertEqual(adopted.dashboards["dash.json"].source_ref, "main")
        self.assertEqual(provenance_label(adopted.dashboards["dash.json"]), "CANONICAL MAIN")
