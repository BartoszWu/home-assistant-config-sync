import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'export'))
from inventory import build_inventory, render_markdown
from semantic import compare
from dependencies import graph
from analyze import analyze, write, read, agent_summary
from influx_candidates import assess
from analysis_reports import validate_analysis


def inventory(ids=('sensor.temperature',)):
    return build_inventory([{'entity_id': eid, 'platform': 'example', 'device_id': 'a' * 32} for eid in ids],
        [{'id': 'a' * 32, 'name': 'Fixture'}], [], [],
        [{'entity_id': eid, 'attributes': {'device_class': 'temperature', 'state_class': 'measurement', 'unit_of_measurement': '°C'}} for eid in ids])[0]


class SemanticTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(compare(inventory(), inventory(('sensor.temperature', 'sensor.new')))['sections']['entities']['added'][0]['entity_id'], 'sensor.new')

    def test_remove(self):
        self.assertEqual(len(compare(inventory(), inventory(()))['sections']['entities']['removed']), 1)

    def test_metadata(self):
        old = inventory(); new = copy.deepcopy(old)
        new['entities'][0]['area'] = 'Other'
        self.assertIn('area', compare(old, new)['sections']['entities']['changed'][0]['fields'])

    def test_runtime(self):
        old = inventory(); new = copy.deepcopy(old)
        new['entities'][0].update(state='22', temperature=22, hvac_action='heating', last_updated='later', availability=False)
        self.assertFalse(compare(old, new)['structural_changes'])

    def test_incomplete(self):
        new = inventory(()); new['completeness']['complete'] = False
        self.assertFalse(compare(inventory(), new)['structural_changes'])

    def test_incomplete_baseline(self):
        old = inventory(); old['completeness']['complete'] = False
        self.assertFalse(compare(old, inventory(()))['structural_changes'])

    def test_schema_mismatch(self):
        old = inventory(); old['schema_version'] = 2
        self.assertEqual(compare(old, inventory(()))['status'], 'migration_required')

    def test_deterministic(self):
        self.assertEqual(compare(inventory(), inventory()), compare(inventory(), inventory()))

    def test_no_rename_guess(self):
        diff = compare(inventory(), inventory(('sensor.temperature_new',)))
        self.assertEqual(len(diff['sections']['entities']['removed']), 1)
        self.assertEqual(len(diff['sections']['entities']['added']), 1)

    def test_registry_sections(self):
        old = inventory(); new = copy.deepcopy(old)
        new['devices'][0]['name'] = 'Other'
        new['areas'] = [{'area_id': 'room', 'name': 'Room', 'floor_id': None}]
        new['floors'] = [{'floor_id': 'ground', 'name': 'Ground'}]
        result = compare(old, new)['sections']
        self.assertTrue(result['devices']['changed'])
        self.assertTrue(result['areas']['added'])
        self.assertTrue(result['floors']['added'])


class DependencyTests(unittest.TestCase):
    def scan(self, content, kind='dashboard', inv=None):
        return graph(inv or inventory(), [{'file': 'fixture.json', 'kind': kind, 'complete': True, 'content': content}])['edges']

    def test_dashboard(self):
        self.assertTrue(any(x['relation'] == 'used_by_dashboard' and x['json_path'] == '/views/0/cards/0/entity' for x in self.scan({'views': [{'cards': [{'entity': 'sensor.temperature'}]}]})))

    def test_automation(self):
        self.assertTrue(any(x['relation'] == 'used_by_automation' for x in self.scan({'triggers': [{'entity_id': 'sensor.temperature'}]}, 'automation')))

    def test_script(self):
        self.assertTrue(any(x['relation'] == 'used_by_script' for x in self.scan({'sequence': [{'target': {'entity_id': ['sensor.temperature']}}]}, 'script')))

    def test_zone_home(self):
        inv = inventory(); inv['entities'].append({'entity_id': 'zone.home', 'runtime_only': True})
        self.assertTrue(any(x['status'] == 'runtime-only resolved' for x in self.scan({'zone': 'zone.home'}, inv=inv)))

    def test_service_event_trigger_not_entity(self):
        refs = self.scan({'action': 'light.turn_on', 'service': 'notify.mobile_app', 'event_type': 'app.event', 'trigger': 'state', 'type': 'custom.card', 'description': 'sensor.absent'})
        self.assertFalse(any(x['relation'].startswith('used_by') for x in refs))

    def test_dynamic(self):
        refs = self.scan({'entity_id': '{{ states(variable) }}'})
        self.assertTrue(any(x['status'] == 'dynamic/unknown' for x in refs))
        self.assertFalse(any(x['status'] == 'confirmed broken' for x in refs))

    def test_literal_template_not_confirmed(self):
        refs = self.scan({'value_template': "{{ states('sensor.absent') }}"})
        self.assertTrue(any(x['status'] == 'unresolved' for x in refs))

    def test_confirmed_broken(self):
        self.assertTrue(any(x['status'] == 'confirmed broken' for x in self.scan({'entity': 'sensor.absent'})))

    def test_incomplete_unknown(self):
        inv = inventory(); inv['completeness']['complete'] = False
        self.assertTrue(any(x['status'] == 'unresolved' for x in self.scan({'entity': 'sensor.absent'}, inv=inv)))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.save(inventory())
        write(self.repo / 'inventory/export-status.json', {'sections': {'inventory': {'complete': True, 'status': 'success'}}})

    def save(self, data):
        write(self.repo / 'inventory/entities.json', data)

    def run_analysis(self):
        return analyze(self.repo)

    def policy(self, decision):
        ref = read(self.repo / 'inventory/influxdb-candidates.json')['candidates'][0]['entity_ref']
        write(self.repo / 'policy/influxdb.json', {'schema_version': 1, 'identity_policy': 'exact_entity_id_lifetime_v1', 'decisions': [{'entity_ref': ref, 'entity_id': 'sensor.temperature', 'decision': decision, 'reason': 'User choice'}]})

    def test_initial_review(self):
        self.run_analysis()
        self.assertFalse(read(self.repo / 'inventory/summary.json')['structural_changes'])
        self.assertEqual(read(self.repo / 'policy/influxdb.json')['decisions'], [])
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidates'][0]['scope'], 'INITIAL LONG-TERM METRICS REVIEW')

    def test_include(self):
        self.run_analysis(); self.policy('INCLUDE'); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 0)

    def test_exclude(self):
        self.run_analysis(); self.policy('EXCLUDE'); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 0)

    def test_pending(self):
        self.run_analysis(); self.policy('PENDING'); self.run_analysis(); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 0)

    def test_pending_metadata_change(self):
        self.run_analysis(); self.policy('PENDING'); self.run_analysis()
        inv = inventory(); inv['entities'][0]['unit_of_measurement'] = 'K'; self.save(inv); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 1)

    def test_inactive_and_return_no_transfer(self):
        self.run_analysis(); self.policy('INCLUDE'); self.run_analysis()
        self.save(inventory(())); self.run_analysis()
        self.assertTrue(read(self.repo / 'policy/influxdb.json')['decisions'][0]['inactive'])
        self.save(inventory()); self.run_analysis()
        self.assertTrue(read(self.repo / 'policy/influxdb.json')['decisions'][0]['inactive'])
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 1)

    def test_no_unsafe_policy_rename(self):
        self.run_analysis(); self.policy('INCLUDE'); self.run_analysis()
        self.save(inventory(('sensor.temperature_new',))); self.run_analysis()
        self.assertTrue(read(self.repo / 'policy/influxdb.json')['decisions'][0]['inactive'])
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 1)

    def test_latest_survives_no_change(self):
        self.run_analysis(); self.save(inventory(('sensor.temperature', 'sensor.new'))); self.run_analysis()
        before = (self.repo / 'inventory/changes.json').read_bytes()
        self.assertFalse(self.run_analysis()['structural_changes'])
        self.assertEqual(before, (self.repo / 'inventory/changes.json').read_bytes())
        self.assertTrue(agent_summary(self.repo)['structural_changes'])
        agent_summary(self.repo, True, agent_summary(self.repo)['summary_revision'])
        self.assertFalse(agent_summary(self.repo)['structural_changes'])

    def test_idempotent_all_files(self):
        self.run_analysis()
        before = {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        self.run_analysis()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()})

    def test_summary_counts_paths(self):
        self.run_analysis(); summary = read(self.repo / 'inventory/summary.json')
        self.assertEqual(summary['counts']['entities'], 1)
        self.assertEqual(summary['influx_unreviewed_count'], 1)
        self.assertEqual(summary['paths']['changes'], 'docs/CHANGES.md')

    def test_bootstrap_461_not_all_candidates(self):
        inv = inventory(tuple('button.fixture_' + str(i) for i in range(460)) + ('sensor.temperature',))
        self.save(inv); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 1)

    def test_new_records_only(self):
        self.run_analysis(); self.policy('EXCLUDE'); self.run_analysis()
        self.save(inventory(('sensor.temperature', 'sensor.new'))); self.run_analysis()
        self.assertEqual([x['entity_id'] for x in read(self.repo / 'inventory/influxdb-candidates.json')['candidates']], ['sensor.new'])

    def test_device_grouping(self):
        self.run_analysis()
        self.assertIn('Device: Fixture', (self.repo / 'docs/INFLUXDB-CANDIDATES.md').read_text())

    def test_climate_attributes(self):
        inv = inventory(('climate.fixture',)); inv['entities'][0]['attribute_keys'] = ['temperature', 'current_temperature', 'hvac_action']
        self.save(inv); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 4)

    def test_publication_gate(self):
        self.run_analysis(); validate_analysis(self.repo)
        (self.repo / 'docs/CHANGES.md').write_text('wrong')
        with self.assertRaises(ValueError): validate_analysis(self.repo)

    def test_incomplete_keeps_policy_and_baseline(self):
        self.run_analysis(); self.policy('INCLUDE'); self.run_analysis()
        before = (self.repo / 'inventory/analysis-state.json').read_bytes()
        inv = inventory(()); inv['completeness']['complete'] = False
        self.save(inv); self.run_analysis()
        self.assertEqual(before, (self.repo / 'inventory/analysis-state.json').read_bytes())
        self.assertFalse(read(self.repo / 'policy/influxdb.json')['decisions'][0]['inactive'])


class RecommendationTests(unittest.TestCase):
    def test_temperature(self): self.assertEqual(assess({'domain': 'sensor', 'device_class': 'temperature'})[0], 'RECOMMENDED')
    def test_humidity(self): self.assertEqual(assess({'domain': 'sensor', 'device_class': 'humidity'})[0], 'RECOMMENDED')
    def test_battery(self): self.assertEqual(assess({'domain': 'sensor', 'device_class': 'battery'})[0], 'NOT_RECOMMENDED')
    def test_update(self): self.assertEqual(assess({'domain': 'update'})[0], 'NOT_RECOMMENDED')
    def test_button(self): self.assertEqual(assess({'domain': 'button'})[0], 'NOT_RECOMMENDED')


class AdditionalWorkflowTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    save = WorkflowTests.save
    run_analysis = WorkflowTests.run_analysis
    policy = WorkflowTests.policy
    def test_long_json_pointer_gate(self):
        write(self.repo / 'inventory/dashboards.json', {'dashboards': [{'file': 'fixture.json', 'status': 'success'}]})
        write(self.repo / 'dashboards/fixture.json', {'views': [{'sections': [{'cards': [{'cards': [{'entity': 'sensor.temperature'}]}]}]}]})
        self.run_analysis(); validate_analysis(self.repo)

    def test_stale_acknowledgement_rejected(self):
        self.run_analysis(); old = agent_summary(self.repo)['summary_revision']
        self.save(inventory(('sensor.temperature', 'sensor.new'))); self.run_analysis()
        with self.assertRaises(ValueError):
            agent_summary(self.repo, True, old)
        self.assertFalse((self.repo / '.agent-local/surfaced.json').exists())

    def test_attribute_override(self):
        inv = inventory(('climate.fixture',)); inv['entities'][0]['attribute_keys'] = ['temperature', 'hvac_action']
        self.save(inv); self.run_analysis()
        ref = read(self.repo / 'inventory/influxdb-candidates.json')['candidates'][0]['entity_ref']
        write(self.repo / 'policy/influxdb.json', {'schema_version': 1, 'identity_policy': 'exact_entity_id_lifetime_v1', 'decisions': [
            {'entity_ref': ref, 'entity_id': 'climate.fixture', 'decision': 'EXCLUDE', 'reason': 'Skip other attributes'},
            {'entity_ref': ref, 'entity_id': 'climate.fixture', 'attribute': 'temperature', 'decision': 'INCLUDE', 'reason': 'Keep target'}]})
        self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['candidate_count'], 0)

    def test_new_device(self):
        self.run_analysis(); self.policy('EXCLUDE'); self.run_analysis()
        inv = inventory(('sensor.temperature', 'sensor.new'))
        new = next(e for e in inv['entities'] if e['entity_id'] == 'sensor.new')
        new['device_id'] = 'b' * 32; new['device_name'] = 'New device'
        inv['devices'][0]['entities'] = ['sensor.temperature']; inv['devices'][0]['entity_count'] = 1
        inv['devices'].append({**inv['devices'][0], 'device_id': 'b' * 32, 'name': 'New device', 'entities': ['sensor.new']})
        inv['counts']['devices'] = 2
        self.save(inv); self.run_analysis()
        self.assertEqual(read(self.repo / 'inventory/influxdb-candidates.json')['entity_count'], 1)
        self.assertEqual(read(self.repo / 'inventory/summary.json')['new_devices_count'], 1)

    def test_real_publish_extension(self):
        from export_status import publish
        staging = self.repo / 'staging'
        current = inventory(('sensor.temperature', 'sensor.new'))
        write(staging / 'entities.json', current)
        write(staging / 'metadata-observations.json', {})
        write(staging / 'states.json', {'states': []})
        (staging / 'STATES.md').write_text('Fixture')
        write(staging / 'export-status.json', {'observed_at': 'first', 'sections': {
            'inventory': {'status': 'success', 'complete': True},
            'dashboards': {'status': 'read_error', 'complete': False},
            'managed_config': {'status': 'read_error', 'complete': False}}})
        publish(self.repo, staging)
        self.assertEqual(read(self.repo / 'inventory/summary.json')['new_entities_count'], 1)
        self.assertEqual(next(x for x in read(self.repo / 'inventory/influxdb-candidates.json')['candidates'] if x['entity_id'] == 'sensor.new')['scope'], 'NEW RECORDS')
        before = read(self.repo / 'inventory/summary.json')
        write(staging / 'states.json', {'states': [{'state': 'runtime-only'}]})
        status = read(staging / 'export-status.json'); status['observed_at'] = 'second'; write(staging / 'export-status.json', status)
        publish(self.repo, staging)
        self.assertEqual(before, read(self.repo / 'inventory/summary.json'))
        self.assertEqual(read(self.repo / 'inventory/export-status.json')['observed_at'], 'first')
