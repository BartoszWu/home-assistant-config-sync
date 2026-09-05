import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'import'), str(ROOT / 'export')]
import app
from runtime_inventory import safe_runtime_text


def unsafe_samples():
    # Synthetic only; build credential-shaped values so the source secret scan
    # does not mistake regression inputs for accidentally committed credentials.
    return [
        {'localKey': 'synthetic'}, {'serial_number': 123456789},
        {'userId': 'synthetic'}, {'nested': [{'clientSecret': 'synthetic'}]},
        {'text': 'prefix Bear' + 'er ' + 'x' * 30},
        {'url': 'https://user:synthetic@example.test/path'},
        {'text': 'fd00::1234'}, {'text': '2001:db8::1'},
        {'text': 'AA:BB:CC:DD:EE:FF'}, {'text': 'AA-BB-CC-DD-EE-FF'},
        {'text': 'aabb.ccdd.eeff'}, {'text': 'aabbccddeeff'},
        {'text': 'gh' + 'p_' + 'x' * 32},
        {'text': 'localKey=synthetic'}, {'text': 'serial number: synthetic'},
    ]


class SecurityRegressionTests(unittest.TestCase):
    def test_import_rejects_audited_leaks_recursively(self):
        for sample in unsafe_samples():
            with self.subTest(sample_keys=list(sample)):
                self.assertIsNotNone(app.unsafe_reason(sample))

    def test_runtime_redacts_audited_text_leaks(self):
        for sample in unsafe_samples():
            value = sample.get('text') or sample.get('url')
            if value:
                with self.subTest(value_type='synthetic text'):
                    self.assertNotEqual(safe_runtime_text(value), value)


class ApplyPreviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'dashboards').mkdir()
        (self.repo / 'state').mkdir()
        self.live = {'views': [{'title': 'Base'}]}
        self.desired = {'views': [{'title': 'Reviewed'}]}
        self.path = self.repo / 'dashboards/test.json'
        self.path.write_text(json.dumps(self.desired))
        (self.repo / 'state/dashboard-bases.json').write_text(json.dumps({
            'dashboards': {'test.json': {'sha256': app.digest(self.live)}}}))
        self.form = {'selected': 'test.json',
                     'preview_hash': 'test.json:' + app.digest(self.live),
                     'desired_hash': 'test.json:' + app.digest(self.desired)}
        for p in [patch.object(app, 'WORKDIR', self.repo),
                  patch.object(app, 'refresh_repo', return_value='fixture'),
                  patch.object(app, 'ha_dashboard_config', side_effect=lambda _: copy.deepcopy(self.live))]:
            p.start()
            self.addCleanup(p.stop)

    def submit(self):
        return app.app.test_client().post('/apply', data=self.form,
            environ_overrides={'REMOTE_ADDR': '172.30.32.2'})

    def test_changed_git_after_preview_blocks_apply(self):
        self.path.write_text(json.dumps({'views': [{'title': 'Unreviewed'}]}))
        with patch.object(app, 'save_dashboard') as save, patch.object(app, 'request_export') as export:
            self.submit()
            save.assert_not_called()
            export.assert_not_called()

    def test_changed_ha_after_preview_blocks_apply(self):
        self.live = {'views': [{'title': 'Changed in HA'}]}
        with patch.object(app, 'save_dashboard') as save:
            self.submit()
            save.assert_not_called()

    def test_unchanged_preview_applies_and_verifies(self):
        def save(_, value):
            self.live = copy.deepcopy(value)
        with patch.object(app, 'save_dashboard', side_effect=save) as saved, patch.object(app, 'request_export') as export:
            self.submit()
            saved.assert_called_once_with('test.json', self.desired)
            export.assert_called_once_with(['test.json'])

    def test_missing_desired_hash_fails_closed(self):
        self.form.pop('desired_hash')
        with patch.object(app, 'save_dashboard') as save:
            self.assertEqual(self.submit().status_code, 400)
            save.assert_not_called()

class ResourcePolicyTests(unittest.TestCase):
    def test_hacs_resource_drops_version_without_false_mac_exclusion(self):
        from dashboard_manifest import resource_record
        record = resource_record({'url': '/hacsfiles/example/example.js?hacstag=123456789012', 'type': 'module'})
        self.assertEqual(record, {'url': '/hacsfiles/example/example.js', 'type': 'module', 'status': 'success'})

    def test_resource_unknown_queries_and_private_urls_fail_closed(self):
        from dashboard_manifest import resource_record
        for url in ('/hacsfiles/example/example.js?token=synthetic', 'https://user:synthetic@example.test/a.js',
                    '/local/../private.js', '/local/a.js?unclassified=value'):
            self.assertEqual(resource_record({'url': url, 'type': 'module'}), {'status': 'security_excluded'})
