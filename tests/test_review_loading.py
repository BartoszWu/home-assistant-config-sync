"""Progressive review stays pinned and never authorizes unfinished comparisons."""

import json
import re
import threading
import unittest
from unittest.mock import Mock, patch

import test_stage1_security as security_tests
from test_stage1_security import fake_revision, REVIEW_SHA


def event(chunk):
    return json.loads(chunk.decode().removeprefix("data: ").strip())


class ProgressiveReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        security_tests.ApplyPreviewRegressionTests.setUpClass.__func__(cls)

    def setUp(self):
        security_tests.ApplyPreviewRegressionTests.setUp(self)
        self.client = self.app_module.app.test_client()
        self.ingress = {"REMOTE_ADDR": "172.30.32.2"}

    def test_opening_shell_performs_no_git_or_ha_reads_and_has_no_apply_plan(self):
        app = self.app_module
        with patch.object(app, 'refresh_repo') as refresh, patch.object(app, 'collect_changes') as changes, patch.dict(app.app.extensions, {'project_overview': Mock()}):
            response = self.client.get('/', environ_overrides=self.ingress)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('data-review-stream="review-stream"', html)
            self.assertIn('Sprawdzanie zmian…', html)
            self.assertIn('class="skeleton"', html)
            self.assertNotIn('Wszystko jest aktualne', html)
            self.assertNotIn('name="reviewed_sha"', html)
            self.assertIsNone(re.search(r'<input[^>]+name="selected"', html))
            self.assertNotIn('id="apply-button"', html)
            refresh.assert_not_called()
            changes.assert_not_called()
            app.app.extensions['project_overview'].assert_not_called()

    def test_fast_applications_arrive_while_configuration_is_still_blocked(self):
        app = self.app_module
        waiting, release = threading.Event(), threading.Event()
        original = app.collect_changes

        def slow_changes(*args, **kwargs):
            waiting.set()
            if not release.wait(5):
                raise AssertionError('test comparison did not release')
            return original(*args, **kwargs)

        with patch.object(app, 'collect_changes', side_effect=slow_changes), patch.dict(app.app.extensions, {'project_overview': Mock(return_value=[])}), patch.object(app, 'refresh_repo', return_value=fake_revision()) as refresh:
            response = self.client.get('/review-stream', environ_overrides=self.ingress, buffered=False)
            chunks = iter(response.response)
            try:
                self.assertEqual(event(next(chunks))['kind'], 'progress')
                self.assertEqual(event(next(chunks))['kind'], 'source')
                ready = event(next(chunks))
                self.assertEqual(ready['kind'], 'applications')
                self.assertTrue(waiting.is_set())
                self.assertNotIn('apply-form', ready['html'])
                release.set()
                config = event(next(chunks))
                self.assertEqual(config['kind'], 'configuration')
                self.assertIn('name="reviewed_sha" value="' + REVIEW_SHA + '"', config['html'])
                self.assertIn('value="test.json"', config['html'])
                self.assertEqual(event(next(chunks))['kind'], 'complete')
                refresh.assert_called_once()
            finally:
                release.set()
                response.close()

    def test_fast_configuration_does_not_wait_for_slow_application_status(self):
        waiting, release = threading.Event(), threading.Event()

        def slow_apps(*args, **kwargs):
            waiting.set()
            if not release.wait(5):
                raise AssertionError('test versions did not release')
            return []

        with patch.dict(self.app_module.app.extensions, {'project_overview': slow_apps}):
            response = self.client.get('/review-stream', environ_overrides=self.ingress, buffered=False)
            chunks = iter(response.response)
            try:
                self.assertEqual(event(next(chunks))['kind'], 'progress')
                self.assertEqual(event(next(chunks))['kind'], 'source')
                config = event(next(chunks))
                self.assertEqual(config['kind'], 'configuration')
                self.assertTrue(waiting.is_set())
                self.assertIn('name="reviewed_sha" value="' + REVIEW_SHA + '"', config['html'])
                release.set()
                self.assertEqual(event(next(chunks))['kind'], 'applications')
                self.assertEqual(event(next(chunks))['kind'], 'complete')
            finally:
                release.set()
                response.close()

    def test_failed_config_check_keeps_apply_disabled_and_final_summary_incomplete(self):
        app = self.app_module
        with patch.object(app, 'managed_entries_for_revision', side_effect=ValueError('synthetic failed read')), patch.dict(app.app.extensions, {'project_overview': Mock(return_value=[])}):
            response = self.client.get('/review-stream', environ_overrides=self.ingress)
            events = [event(chunk) for chunk in response.response]
        config = next(e for e in events if e['kind'] == 'configuration')['html']
        self.assertNotIn('name="selected"', config)
        self.assertIn('id="apply-button" type="submit" disabled', config)
        self.assertIn('Nie wszystko udało się sprawdzić', events[-1]['html'])

    def test_failed_source_stops_before_both_sections_and_never_leaks_error(self):
        app = self.app_module
        with patch.object(app, 'refresh_repo', side_effect=ValueError('synthetic credential-bearing error')), patch.object(app, 'collect_changes') as changes, patch.dict(app.app.extensions, {'project_overview': Mock()}):
            response = self.client.get('/review-stream', environ_overrides=self.ingress)
            events = [event(chunk) for chunk in response.response]
            self.assertEqual([e['kind'] for e in events], ['progress', 'source', 'error'])
            self.assertNotIn('credential-bearing', str(events))
            changes.assert_not_called()
            app.app.extensions['project_overview'].assert_not_called()

    def test_failed_application_status_preserves_configuration_and_never_claims_all_current(self):
        app = self.app_module
        with patch.dict(app.app.extensions, {'project_overview': Mock(side_effect=RuntimeError('synthetic private error'))}):
            response = self.client.get('/review-stream', environ_overrides=self.ingress)
            events = [event(chunk) for chunk in response.response]
        applications = next(e for e in events if e['kind'] == 'applications')['html']
        configuration = next(e for e in events if e['kind'] == 'configuration')['html']
        self.assertIn('Nie udało się sprawdzić', applications)
        self.assertNotIn('synthetic private error', applications)
        self.assertIn('name="selected"', configuration)
        self.assertNotIn('Wszystko jest aktualne', events[-1]['html'])

    def test_streamed_feature_review_uses_one_source_and_does_not_offer_main_updates(self):
        app = self.app_module
        source = app.SourceRevision('feature/testing', 'branch', REVIEW_SHA, REVIEW_SHA[:7], ('main', 'feature/testing'))
        provider = Mock(return_value=[])
        with patch.object(app, 'refresh_repo', return_value=source) as refresh, patch.dict(app.app.extensions, {'project_overview': provider}):
            response = self.client.get('/review-stream?source=feature/testing', environ_overrides=self.ingress)
            events = [event(chunk) for chunk in response.response]
        refresh.assert_called_once_with('feature/testing')
        self.assertFalse(provider.call_args.kwargs['canonical'])
        configuration = next(e for e in events if e['kind'] == 'configuration')['html']
        self.assertIn('name="source" value="feature/testing"', configuration)
        self.assertIn('name="reviewed_sha" value="' + REVIEW_SHA + '"', configuration)

    def test_stream_preserves_ingress_guard_and_rejects_invalid_source(self):
        self.assertEqual(self.client.get('/review-stream').status_code, 403)
        self.assertEqual(self.client.get('/review-stream?source=../bad', environ_overrides=self.ingress).status_code, 400)
        response = self.client.get('/review-stream', environ_overrides=self.ingress)
        self.assertEqual(response.mimetype, 'text/event-stream')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response.close()

    def test_explicit_commit_is_preserved_in_the_fast_shell(self):
        html = self.client.get('/?source_sha=' + REVIEW_SHA, environ_overrides=self.ingress).get_data(as_text=True)
        self.assertIn('source_sha=' + REVIEW_SHA, html)
        self.assertIn('name="source_sha" value="' + REVIEW_SHA + '"', html)
        self.assertNotIn('name="reviewed_sha"', html)
