import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "import"), str(ROOT / "export")]

from dashboard_logic import NONCANONICAL_STATUS, classify_with_provenance, digest
from deployment_provenance import (
    CANONICAL_BRANCH,
    ArtifactProvenance,
    InvalidProvenance,
    ProvenanceStore,
    adopt_canonical,
    dashboard_sync_decision,
    empty_store,
    is_canonical_live,
    load_export_store,
    load_import_store,
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
        allowed, reason = dashboard_sync_decision("dash.json", empty_store())
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_persistence_roundtrip_and_export_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "import.json"
            shared = root / "www/.config-sync/live-dashboards.json"
            cache = root / "export-cache.json"
            store = record_artifacts(
                empty_store(),
                source_ref="feature/temp-redesign",
                source_kind="branch",
                commit_sha=FEATURE_SHA,
                dashboards={"dashboard-temp.json": HASH_B},
            )
            save_store(store, local_path=local, shared_path=shared)
            reloaded = load_import_store(local_path=local, shared_path=shared)
            self.assertFalse(reloaded.dashboards["dashboard-temp.json"].canonical)
            exported = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(
                exported.dashboards["dashboard-temp.json"].commit_sha, FEATURE_SHA
            )
            self.assertTrue(cache.exists())
            shared.unlink()
            cached = load_export_store(shared_path=shared, cache_path=cache)
            self.assertEqual(
                cached.dashboards["dashboard-temp.json"].source_ref,
                "feature/temp-redesign",
            )

    def test_invalid_shared_file_fail_closes_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "live-dashboards.json"
            shared.write_text("{not-json", encoding="utf-8")
            store = load_export_store(
                shared_path=shared, cache_path=Path(tmp) / "cache.json"
            )
            self.assertTrue(store.invalid)
            allowed, reason = dashboard_sync_decision("dash.json", store)
            self.assertFalse(allowed)
            self.assertIn("unreadable", reason)

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
