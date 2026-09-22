import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'import'), str(ROOT / 'export')]
from security import unsafe_reason
from runtime_inventory import safe_runtime_text
from git_source import SourceRevision


REVIEW_SHA = "c" * 40


def fake_revision(**kwargs):
    sha = kwargs.get("commit_sha", REVIEW_SHA)
    return SourceRevision(
        source_ref=kwargs.get("source_ref", "main"),
        source_kind=kwargs.get("source_kind", "branch"),
        commit_sha=sha,
        short_sha=sha[:7],
        available_branches=("main",),
        branch_tip_sha=kwargs.get("branch_tip_sha", sha),
        stale=kwargs.get("stale", False),
        reviewed_sha=kwargs.get("reviewed_sha"),
    )


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
                self.assertIsNotNone(unsafe_reason(sample))

    def test_runtime_redacts_audited_text_leaks(self):
        for sample in unsafe_samples():
            value = sample.get('text') or sample.get('url')
            if value:
                with self.subTest(value_type='synthetic text'):
                    self.assertNotEqual(safe_runtime_text(value), value)

    def test_long_lovelace_navigation_paths_are_not_opaque_tokens(self):
        # Long hyphenated dashboard paths share characters with base64-ish
        # tokens once '/' is allowed; they must not block Import Apply.
        for path in (
            '/dashboard-temperatura/pokoj-michasia-90dni-archiwum',
            '/dashboard-temperatura/pokoj-adasia-90dni-archiwum',
            '/dashboard-temperatura/hol-parter-90dni-archiwum',
            {
                'tap_action': {
                    'action': 'navigate',
                    'navigation_path': '/dashboard-temperatura/pokoj-michasia-90dni-archiwum',
                }
            },
        ):
            with self.subTest(path=path if isinstance(path, str) else 'nested'):
                self.assertIsNone(unsafe_reason(path))

    def test_opaque_tokens_without_slash_still_rejected(self):
        token = 'A' * 48
        self.assertEqual(unsafe_reason({'text': token}), 'credential-like identifier')


class ApplyPreviewRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as import_app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(
                "Install Import's Flask/websocket-client dependencies for Apply tests"
            ) from exc
        cls.app_module = import_app

    def setUp(self):
        app = self.app_module
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
                     'desired_hash': 'test.json:' + app.digest(self.desired),
                     'source': 'main',
                     'reviewed_sha': REVIEW_SHA}
        for p in [patch.object(app, 'WORKDIR', self.repo),
                  patch.object(app, 'refresh_repo', return_value=fake_revision()),
                  patch.object(app, 'ha_dashboard_config', side_effect=lambda _: copy.deepcopy(self.live)),
                  patch.object(app, 'collect_resource_changes', return_value=[]),
                  patch.object(app, 'IMPORT_STATE_PATH', Path(self.tmp.name) / 'prov-local.json'),
                  patch.object(app, 'SHARED_STATE_PATH', Path(self.tmp.name) / '.config-sync/live-dashboards.json')]:
            p.start()
            self.addCleanup(p.stop)

    def submit(self):
        app = self.app_module
        return app.app.test_client().post('/apply', data=self.form,
            environ_overrides={'REMOTE_ADDR': '172.30.32.2'})

    def test_review_bulk_selection_excludes_security_warnings(self):
        app = self.app_module
        warning_path = self.repo / 'dashboards/warned-dashboard.json'
        warning_path.write_text(json.dumps({
            'views': [{'title': 'Warned'}], 'localKey': 'synthetic',
        }))
        bases = {'dashboards': {
            name: {'sha256': app.digest(self.live)}
            for name in ('test.json', 'warned-dashboard.json')
        }}
        (self.repo / 'state/dashboard-bases.json').write_text(json.dumps(bases))

        html = app.app.test_client().get(
            '/', environ_overrides={'REMOTE_ADDR': '172.30.32.2'}
        ).get_data(as_text=True)
        safe_input = re.search(r'<input[^>]+name="selected"[^>]+value="test.json"[^>]*>', html)
        warned_input = re.search(r'<input[^>]+name="selected"[^>]+value="warned-dashboard.json"[^>]*>', html)
        self.assertIsNotNone(safe_input)
        self.assertIsNotNone(warned_input)
        self.assertIn('data-bulk-selectable', safe_input.group())
        self.assertNotIn('data-bulk-selectable', warned_input.group())
        self.assertIn('id="select-all-ready"', html)
        self.assertIn('Items with security warnings need individual selection.', html)
        self.assertIn('Changed files', html)
        self.assertNotIn('HA current', html)
        self.assertNotIn('Review changes', html)
        self.assertNotIn('class="diff"', html)
        self.assertNotIn('Generate visual preview', html)
        self.assertNotIn('visual-preview.mjs', html)

    def test_changed_git_after_preview_blocks_apply(self):
        app = self.app_module
        self.path.write_text(json.dumps({'views': [{'title': 'Unreviewed'}]}))
        with patch.object(app, 'save_dashboard') as save, patch.object(app, 'request_export') as export:
            self.submit()
            save.assert_not_called()
            export.assert_not_called()

    def test_changed_ha_after_preview_blocks_apply(self):
        app = self.app_module
        self.live = {'views': [{'title': 'Changed in HA'}]}
        with patch.object(app, 'save_dashboard') as save:
            self.submit()
            save.assert_not_called()

    def test_unchanged_preview_applies_and_verifies(self):
        app = self.app_module
        def save(_, value):
            self.live = copy.deepcopy(value)
        with patch.object(app, 'save_dashboard', side_effect=save) as saved, patch.object(app, 'request_export') as export:
            self.submit()
            saved.assert_called_once_with('test.json', self.desired)
            export.assert_called_once_with(['test.json'])

    def test_stale_source_refuses_new_tip(self):
        app = self.app_module
        moved = fake_revision(commit_sha="d" * 40, stale=True, reviewed_sha=REVIEW_SHA)
        with patch.object(app, "refresh_repo", return_value=moved), \
             patch.object(app, "save_dashboard") as save, \
             patch.object(app, "request_export") as export:
            response = self.submit()
        html = response.get_data(as_text=True)
        self.assertIn("SOURCE UPDATED", html)
        save.assert_not_called()
        export.assert_not_called()

    def test_missing_reviewed_sha_fails_closed(self):
        app = self.app_module
        self.form.pop("reviewed_sha")
        with patch.object(app, "save_dashboard") as save:
            self.assertEqual(self.submit().status_code, 400)
            save.assert_not_called()

    def test_missing_desired_hash_fails_closed(self):
        app = self.app_module
        self.form.pop('desired_hash')
        with patch.object(app, 'save_dashboard') as save:
            self.assertEqual(self.submit().status_code, 400)
            save.assert_not_called()

    def test_main_apply_records_canonical_provenance(self):
        app = self.app_module
        def save(_, value):
            self.live = copy.deepcopy(value)
        with patch.object(app, 'save_dashboard', side_effect=save), patch.object(app, 'request_export'):
            self.submit()
        from deployment_provenance import load_json_store
        store = load_json_store(app.IMPORT_STATE_PATH)
        entry = store.dashboards['test.json']
        self.assertTrue(entry.canonical)
        self.assertEqual(entry.source_ref, 'main')
        self.assertEqual(entry.commit_sha, REVIEW_SHA)
        self.assertTrue(app.SHARED_STATE_PATH.exists())

    def test_feature_apply_records_non_canonical_and_still_requests_export(self):
        app = self.app_module
        feature_sha = 'e' * 40
        self.form['source'] = 'feature/temp-redesign'
        self.form['reviewed_sha'] = feature_sha
        def save(_, value):
            self.live = copy.deepcopy(value)
        with patch.object(app, 'refresh_repo', return_value=fake_revision(
                source_ref='feature/temp-redesign', commit_sha=feature_sha)), \
             patch.object(app, 'save_dashboard', side_effect=save) as saved, \
             patch.object(app, 'request_export') as export:
            response = self.submit()
        saved.assert_called_once()
        export.assert_called_once_with(['test.json'])
        from deployment_provenance import load_json_store
        entry = load_json_store(app.IMPORT_STATE_PATH).dashboards['test.json']
        self.assertFalse(entry.canonical)
        self.assertEqual(entry.source_ref, 'feature/temp-redesign')
        html = response.get_data(as_text=True)
        self.assertIn('NON-CANONICAL', html)

    def test_main_refresh_adopts_when_live_equals_main(self):
        app = self.app_module
        from deployment_provenance import empty_store, record_artifacts, save_store, load_json_store
        self.live = copy.deepcopy(self.desired)
        save_store(
            record_artifacts(
                empty_store(),
                source_ref='feature/temp-redesign',
                source_kind='branch',
                commit_sha='e' * 40,
                dashboards={'test.json': app.digest(self.desired)},
            ),
            local_path=app.IMPORT_STATE_PATH,
            shared_path=app.SHARED_STATE_PATH,
        )
        html = app.app.test_client().get(
            '/', environ_overrides={'REMOTE_ADDR': '172.30.32.2'}
        ).get_data(as_text=True)
        entry = load_json_store(app.IMPORT_STATE_PATH).dashboards['test.json']
        self.assertTrue(entry.canonical)
        self.assertEqual(entry.source_ref, 'main')
        self.assertIn('CANONICAL MAIN', html)
        self.assertIn('marked CANONICAL MAIN', html)

    def test_main_refresh_does_not_adopt_when_main_still_differs(self):
        app = self.app_module
        from deployment_provenance import empty_store, record_artifacts, save_store, load_json_store
        self.path.write_text(json.dumps(self.live))
        drifted = {'views': [{'title': 'Feature LIVE'}]}
        self.live = drifted
        save_store(
            record_artifacts(
                empty_store(),
                source_ref='feature/temp-redesign',
                source_kind='branch',
                commit_sha='e' * 40,
                dashboards={'test.json': app.digest(drifted)},
            ),
            local_path=app.IMPORT_STATE_PATH,
            shared_path=app.SHARED_STATE_PATH,
        )
        html = app.app.test_client().get(
            '/', environ_overrides={'REMOTE_ADDR': '172.30.32.2'}
        ).get_data(as_text=True)
        entry = load_json_store(app.IMPORT_STATE_PATH).dashboards['test.json']
        self.assertFalse(entry.canonical)
        self.assertEqual(entry.source_ref, 'feature/temp-redesign')
        self.assertIn('NON-CANONICAL', html)
        self.assertIn('LIVE FROM FEATURE', html)

    def test_feature_apply_rolls_back_when_provenance_persist_fails(self):
        app = self.app_module
        original = copy.deepcopy(self.live)
        feature_sha = 'e' * 40
        self.form['source'] = 'feature/temp-redesign'
        self.form['reviewed_sha'] = feature_sha

        def save(_, value):
            self.live = copy.deepcopy(value)

        with patch.object(app, 'refresh_repo', return_value=fake_revision(
                source_ref='feature/temp-redesign', commit_sha=feature_sha)), \
             patch.object(app, 'save_dashboard', side_effect=save) as saved, \
             patch.object(app, 'persist_provenance', side_effect=OSError('disk full')), \
             patch.object(app, 'request_export') as export:
            response = self.submit()
        html = response.get_data(as_text=True)
        self.assertIn('Apply FAILED', html)
        self.assertIn('rolled back', html)
        self.assertEqual(self.live, original)
        self.assertGreaterEqual(saved.call_count, 2)
        saved.assert_any_call('test.json', original)
        export.assert_not_called()
        self.assertFalse(app.SHARED_STATE_PATH.exists())

    def test_feature_apply_arms_fail_closed_guard_when_rollback_fails(self):
        app = self.app_module
        feature_sha = 'e' * 40
        self.form['source'] = 'feature/temp-redesign'
        self.form['reviewed_sha'] = feature_sha
        saves = []

        def save(relative, value):
            saves.append(copy.deepcopy(value))
            if len(saves) == 1:
                self.live = copy.deepcopy(value)
            else:
                raise RuntimeError('rollback failed')

        with patch.object(app, 'refresh_repo', return_value=fake_revision(
                source_ref='feature/temp-redesign', commit_sha=feature_sha)), \
             patch.object(app, 'save_dashboard', side_effect=save), \
             patch.object(app, 'persist_provenance', side_effect=OSError('disk full')), \
             patch.object(app, 'request_export') as export:
            response = self.submit()
        html = response.get_data(as_text=True)
        self.assertIn('rollback failed', html)
        self.assertIn('Dashboard export to main is blocked', html)
        self.assertEqual(self.live, self.desired)
        self.assertTrue(
            (app.SHARED_STATE_PATH.parent / 'guard-initialized.json').exists()
        )
        export.assert_not_called()

    def test_partial_apply_keeps_only_persisted_dashboard_provenance(self):
        app = self.app_module
        second = self.repo / 'dashboards/other.json'
        second.write_text(json.dumps(self.desired))
        bases_path = self.repo / 'state/dashboard-bases.json'
        bases = json.loads(bases_path.read_text())
        bases['dashboards']['other.json'] = {'sha256': app.digest(self.live)}
        bases_path.write_text(json.dumps(bases))
        self.form = {
            'selected': ['test.json', 'other.json'],
            'preview_hash': [
                'test.json:' + app.digest(self.live),
                'other.json:' + app.digest(self.live),
            ],
            'desired_hash': [
                'test.json:' + app.digest(self.desired),
                'other.json:' + app.digest(self.desired),
            ],
            'source': 'feature/temp-redesign',
            'reviewed_sha': 'e' * 40,
        }
        persists = {'n': 0}
        lives = {
            'test.json': copy.deepcopy(self.live),
            'other.json': copy.deepcopy(self.live),
        }

        def persist(store, **kwargs):
            persists['n'] += 1
            if persists['n'] >= 2:
                raise OSError('second persist failed')
            return app.save_store(
                store,
                local_path=app.IMPORT_STATE_PATH,
                shared_path=app.SHARED_STATE_PATH,
            )

        def ha_cfg(relative):
            return copy.deepcopy(lives[relative])

        def save(relative, value):
            lives[relative] = copy.deepcopy(value)

        with patch.object(app, 'refresh_repo', return_value=fake_revision(
                source_ref='feature/temp-redesign', commit_sha='e' * 40)), \
             patch.object(app, 'ha_dashboard_config', side_effect=ha_cfg), \
             patch.object(app, 'save_dashboard', side_effect=save), \
             patch.object(app, 'persist_provenance', side_effect=persist), \
             patch.object(app, 'request_export') as export:
            response = self.submit()
        from deployment_provenance import load_json_store
        html = response.get_data(as_text=True)
        self.assertIn('test.json: Applied and verified', html)
        self.assertIn('other.json: provenance persist failed', html)
        store = load_json_store(app.IMPORT_STATE_PATH)
        self.assertIn('test.json', store.dashboards)
        self.assertNotIn('other.json', store.dashboards)
        export.assert_called_once_with(['test.json'])

class CreateMissingDashboardApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as import_app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(
                "Install Import's Flask/websocket-client dependencies for Apply tests"
            ) from exc
        cls.app_module = import_app

    def setUp(self):
        app = self.app_module
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'dashboards').mkdir()
        (self.repo / 'state').mkdir()
        self.desired = {
            'views': [{
                'title': 'Diagnostyka',
                'icon': 'mdi:heart-pulse',
                'path': 'diagnostyka',
            }]
        }
        self.relative = 'dashboard-diagnostyka.json'
        (self.repo / 'dashboards' / self.relative).write_text(json.dumps(self.desired))
        (self.repo / 'state/dashboard-bases.json').write_text(json.dumps({
            'dashboards': {}}))
        self.live = None
        self.registered = set()
        self.created = []
        self.deleted = []
        self.form = {
            'selected': self.relative,
            'preview_hash': self.relative + ':' + app.digest(None),
            'desired_hash': self.relative + ':' + app.digest(self.desired),
            'source': 'main',
            'reviewed_sha': REVIEW_SHA,
        }
        for p in [patch.object(app, 'WORKDIR', self.repo),
                  patch.object(app, 'refresh_repo', return_value=fake_revision()),
                  patch.object(app, 'ha_dashboard_config', side_effect=self._ha_cfg),
                  patch.object(app, 'ha_registered_url_paths', side_effect=lambda: set(self.registered)),
                  patch.object(app, 'collect_resource_changes', return_value=[]),
                  patch.object(app, 'IMPORT_STATE_PATH', Path(self.tmp.name) / 'prov-local.json'),
                  patch.object(app, 'SHARED_STATE_PATH', Path(self.tmp.name) / '.config-sync/live-dashboards.json')]:
            p.start()
            self.addCleanup(p.stop)

    def _ha_cfg(self, _relative):
        return None if self.live is None else copy.deepcopy(self.live)

    def _create(self, relative, desired):
        self.created.append((relative, copy.deepcopy(desired)))
        self.registered.add('dashboard-diagnostyka')
        return {'id': 'dashboard-diagnostyka', 'url_path': 'dashboard-diagnostyka'}

    def _save(self, _relative, value):
        self.live = copy.deepcopy(value)

    def _delete(self, dashboard_id):
        self.deleted.append(dashboard_id)
        self.registered.discard(dashboard_id)
        self.live = None

    def submit(self):
        app = self.app_module
        return app.app.test_client().post(
            '/apply',
            data=self.form,
            environ_overrides={'REMOTE_ADDR': '172.30.32.2'},
        )

    def test_unknown_config_websocket_error_is_missing_dashboard(self):
        app = self.app_module
        with patch.object(
            app,
            'ha_ws_call',
            side_effect=RuntimeError(
                'Home Assistant WebSocket command failed: '
                'Unknown config specified: dashboard-diagnostyka'
            ),
        ):
            self.assertIsNone(app.ha_dashboard_config(self.relative))

    def test_review_does_not_fail_closed_for_unknown_lovelace_config(self):
        app = self.app_module
        html = app.app.test_client().get(
            '/', environ_overrides={'REMOTE_ADDR': '172.30.32.2'}
        ).get_data(as_text=True)
        self.assertNotIn('Unknown config specified', html)
        self.assertIn('READY TO APPLY — CREATE DASHBOARD', html)
        self.assertIn('dashboard-diagnostyka', html)

    def test_apply_creates_then_saves_unregistered_dashboard(self):
        app = self.app_module
        with patch.object(app, 'create_dashboard', side_effect=self._create) as created, \
             patch.object(app, 'save_dashboard', side_effect=self._save) as saved, \
             patch.object(app, 'delete_dashboard', side_effect=self._delete) as deleted, \
             patch.object(app, 'request_export') as export:
            html = self.submit().get_data(as_text=True)
        created.assert_called_once_with(self.relative, self.desired)
        saved.assert_called_once_with(self.relative, self.desired)
        deleted.assert_not_called()
        export.assert_called_once_with([self.relative])
        self.assertIn('Created, applied and verified', html)
        from deployment_provenance import load_json_store
        entry = load_json_store(app.IMPORT_STATE_PATH).dashboards[self.relative]
        self.assertTrue(entry.canonical)

    def test_apply_deletes_created_dashboard_when_provenance_fails(self):
        app = self.app_module
        with patch.object(app, 'create_dashboard', side_effect=self._create), \
             patch.object(app, 'save_dashboard', side_effect=self._save), \
             patch.object(app, 'delete_dashboard', side_effect=self._delete) as deleted, \
             patch.object(app, 'persist_provenance', side_effect=OSError('disk full')), \
             patch.object(app, 'request_export') as export:
            html = self.submit().get_data(as_text=True)
        deleted.assert_called_once_with('dashboard-diagnostyka')
        self.assertIsNone(self.live)
        self.assertIn('rolled back', html)
        export.assert_not_called()


class LovelaceResourceApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import app as import_app
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(
                "Install Import's Flask/websocket-client dependencies for Apply tests"
            ) from exc
        cls.app_module = import_app

    def setUp(self):
        app = self.app_module
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / 'dashboards').mkdir()
        (self.repo / 'state').mkdir()
        (self.repo / 'www' / 'dashboard').mkdir(parents=True)
        self.desired = {
            'views': [{
                'title': 'Diagnostyka',
                'icon': 'mdi:heart-pulse',
                'path': 'diagnostyka',
            }]
        }
        self.relative = 'dashboard-diagnostyka.json'
        self.resource_url = '/local/dashboard/diagnostyka.mjs'
        (self.repo / 'dashboards' / self.relative).write_text(json.dumps(self.desired))
        (self.repo / 'www' / 'dashboard' / 'diagnostyka.mjs').write_text('export {}\n')
        (self.repo / 'state/dashboard-bases.json').write_text(json.dumps({'dashboards': {}}))
        self.live = None
        self.registered = set()
        self.order = []
        from managed_files import LovelaceResourceSpec, resource_desired_fingerprint
        spec = LovelaceResourceSpec(url=self.resource_url, type='module')
        self.form = {
            'selected': self.relative,
            'preview_hash': self.relative + ':' + app.digest(None),
            'desired_hash': self.relative + ':' + app.digest(self.desired),
            'resource_selected': self.resource_url,
            'resource_preview_hash': (
                self.resource_url + ':' + app.digest(None)
            ),
            'resource_desired_hash': (
                self.resource_url + ':' + app.digest(resource_desired_fingerprint(spec))
            ),
            'managed_selected': 'www/dashboard/diagnostyka.mjs',
            'managed_preview_hash': 'www/dashboard/diagnostyka.mjs:' + ('0' * 64),
            'managed_desired_hash': 'www/dashboard/diagnostyka.mjs:' + ('a' * 64),
            'source': 'main',
            'reviewed_sha': REVIEW_SHA,
        }
        from managed_files import ManagedEntry
        self.entry = ManagedEntry(
            'www/dashboard/diagnostyka.mjs',
            'frontend_module',
            resource_url=self.resource_url,
            cache_bust='content_hash',
        )
        for p in [
            patch.object(app, 'WORKDIR', self.repo),
            patch.object(app, 'refresh_repo', return_value=fake_revision()),
            patch.object(app, 'ha_dashboard_config', side_effect=self._ha_cfg),
            patch.object(app, 'ha_registered_url_paths', side_effect=lambda: set(self.registered)),
            patch.object(app, 'managed_entries_for_revision', return_value=[self.entry]),
            patch.dict('os.environ', {'SUPERVISOR_TOKEN': 'test-token'}, clear=False),
            patch.object(app, 'IMPORT_STATE_PATH', Path(self.tmp.name) / 'prov-local.json'),
            patch.object(app, 'SHARED_STATE_PATH', Path(self.tmp.name) / '.config-sync/live-dashboards.json'),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _ha_cfg(self, _relative):
        return None if self.live is None else copy.deepcopy(self.live)

    def submit(self):
        app = self.app_module
        return app.app.test_client().post(
            '/apply',
            data=self.form,
            environ_overrides={'REMOTE_ADDR': '172.30.32.2'},
        )

    def test_apply_order_is_managed_then_resource_then_dashboard(self):
        app = self.app_module

        def managed(*_args, **_kwargs):
            self.order.append('managed')
            return [{"ok": True, "message": "file"}], [self.entry.path]

        def resources(*_args, **_kwargs):
            self.order.append('resource')
            return [{"ok": True, "message": "resource"}], ['res-1']

        def create(_relative, _desired):
            self.order.append('dashboard-create')
            self.registered.add('dashboard-diagnostyka')
            return {'id': 'dashboard-diagnostyka'}

        def save(_relative, value):
            self.order.append('dashboard-save')
            self.live = copy.deepcopy(value)

        with patch.object(app, 'apply_managed_files', side_effect=managed), \
             patch.object(app, 'apply_declared_resources', side_effect=resources), \
             patch.object(app, 'create_dashboard', side_effect=create), \
             patch.object(app, 'save_dashboard', side_effect=save), \
             patch.object(app, 'collect_resource_changes', return_value=[]), \
             patch.object(app, 'request_export'):
            html = self.submit().get_data(as_text=True)
        self.assertEqual(self.order, ['managed', 'resource', 'dashboard-create', 'dashboard-save'])
        self.assertIn('Created, applied and verified', html)

    def test_failed_dashboard_rolls_back_created_resource_not_existing(self):
        app = self.app_module
        deleted = []

        def resources(*_args, **_kwargs):
            return [{"ok": True, "message": "resource"}], ['res-new']

        def create(_relative, _desired):
            raise RuntimeError('dashboard create failed')

        with patch.object(app, 'apply_managed_files', return_value=([{"ok": True, "message": "file"}], [self.entry.path])), \
             patch.object(app, 'apply_declared_resources', side_effect=resources), \
             patch.object(app, 'create_dashboard', side_effect=create), \
             patch.object(app, 'rollback_created_resources', side_effect=lambda _ws, ids: deleted.extend(ids)), \
             patch.object(app, 'collect_resource_changes', return_value=[]), \
             patch.object(app, 'request_export') as export:
            html = self.submit().get_data(as_text=True)
        self.assertEqual(deleted, ['res-new'])
        self.assertIn('rolled back', html)
        export.assert_not_called()

    def test_review_does_not_create_or_delete_resources(self):
        app = self.app_module
        calls = []

        def ws(message_type, **_payload):
            calls.append(message_type)
            if message_type == 'lovelace/resources/list':
                return []
            if message_type == 'lovelace/dashboards/list':
                return []
            raise AssertionError(f'review must not call {message_type}')

        with patch.object(app, 'ha_ws_call', side_effect=ws), \
             patch.object(app, 'managed_entries_for_revision', return_value=[self.entry]):
            html = app.app.test_client().get(
                '/', environ_overrides={'REMOTE_ADDR': '172.30.32.2'}
            ).get_data(as_text=True)
        self.assertNotIn('lovelace/resources/create', calls)
        self.assertNotIn('lovelace/resources/delete', calls)
        self.assertIn('lovelace/resources/list', calls)
        self.assertIn('READY TO APPLY — CREATE RESOURCE', html)
        self.assertIn(self.resource_url, html)


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
