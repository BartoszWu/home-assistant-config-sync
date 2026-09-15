import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'export'))
from export_status import publish, publish_managed
from dashboard_manifest import is_ephemeral_dashboard
from sync_dashboards import write_json, load_json


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.source = self.root / 'source'
        self.path = self.repo / 'config/storage/automations/automation.fixture.json'
        write_json(self.path, {'alias': 'Fixture', 'triggers': []})

    def managed(self, state):
        write_json(self.source / 'index.json', {'schema_version': 2, 'objects': [
            {'entity_id': 'automation.fixture', 'domain': 'automation',
             'exported': False, 'status': state}]})
        return publish_managed(self.repo, self.source)

    def test_read_error_preserves_previous_object(self):
        before = self.path.read_bytes()
        status = self.managed('read_error')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(status['complete'])
        index = load_json(self.repo / 'config/storage/index.json')
        self.assertTrue(index['objects'][0]['retained_previous'])

    def test_unsupported_is_not_removed(self):
        self.managed('unsupported')
        self.assertTrue(self.path.exists())

    def test_security_exclusion_is_explicit_and_not_retained(self):
        status = self.managed('security_excluded')
        self.assertFalse(self.path.exists())
        self.assertEqual(status['statuses'], ['security_excluded'])

    def test_missing_registry_object_is_not_automatic_deletion(self):
        write_json(self.source / 'index.json', {'objects': []})
        publish_managed(self.repo, self.source)
        self.assertTrue(self.path.exists())
        self.assertTrue(load_json(self.repo / 'config/storage/index.json')['objects'][0]['retained_previous'])

    def test_failed_sections_do_not_publish_stale_staging(self):
        write_json(self.source / 'export-status.json', {'sections': {
            key: {'status': 'read_error', 'complete': False}
            for key in ('inventory', 'managed_config', 'dashboards')}})
        write_json(self.repo / 'inventory/entities.json', {'previous': True})
        write_json(self.source / 'entities.json', {'stale': True})
        publish(self.repo, self.source)
        self.assertEqual(load_json(self.repo / 'inventory/entities.json'), {'previous': True})
        status = load_json(self.repo / 'inventory/export-status.json')
        self.assertTrue(all(x['retained_previous'] for x in status['sections'].values()))


class EphemeralPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.source = self.root / 'source'
        self.dashboards = self.root / 'dashboards'
        self.dashboards.mkdir()

    def test_helper_matches_filename_and_url_path(self):
        self.assertTrue(is_ephemeral_dashboard('dashboard-preview'))
        self.assertTrue(is_ephemeral_dashboard('dashboard-preview.json'))
        for value in ('dashboard-preview2.json', 'dashboard-temperatura.json', '', None):
            self.assertFalse(is_ephemeral_dashboard(value), value)

    def test_publish_treats_ephemeral_skip_as_complete(self):
        write_json(self.source / 'export-status.json', {'sections': {
            'inventory': {'status': 'read_error', 'complete': False},
            'managed_config': {'status': 'read_error', 'complete': False},
            'dashboards': {'status': 'success', 'complete': True}}})
        write_json(self.dashboards / 'index.json', {
            'schema_version': 2, 'resources_status': 'success', 'scope_exclusions': [],
            'dashboards': [{'url_path': 'dashboard-preview', 'title': 'Preview',
                            'exported': False, 'status': 'ephemeral_skipped',
                            'reason': 'ephemeral preview dashboard is outside Export scope'}]})
        publish(self.repo, self.source, self.dashboards)
        status = load_json(self.repo / 'inventory/export-status.json')
        self.assertTrue(status['sections']['dashboards']['complete'])
        self.assertIn('ephemeral_skipped', status['sections']['dashboards']['statuses'])
        manifest = load_json(self.repo / 'inventory/dashboards.json')
        self.assertEqual(manifest['dashboards'][0]['git_ha_status'], 'UNKNOWN')
        self.assertFalse((self.repo / 'dashboards/dashboard-preview.json').exists())
