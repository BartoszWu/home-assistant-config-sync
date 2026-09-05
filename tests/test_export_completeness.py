import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'export'))
from export_status import publish, publish_managed
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
