import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'export'))
from inventory import build_inventory, render_markdown, retain_unobserved_metadata


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.devices = [{'id': 'a' * 32, 'name': 'Fixture device', 'area_id': 'room',
                         'manufacturer': 'Example', 'model': 'Fixture', 'serial_number': 'PRIVATE'}]
        self.areas = [{'area_id': 'room', 'name': 'Room', 'floor_id': 'ground'}]
        self.floors = [{'floor_id': 'ground', 'name': 'Ground'}]
        self.entities = [{'entity_id': 'sensor.temperature', 'device_id': 'a' * 32,
                          'platform': 'example', 'disabled_by': 'user', 'hidden_by': 'integration'}]
        self.states = [{'entity_id': 'sensor.temperature', 'state': '22.4', 'attributes': {
            'device_class': 'temperature', 'state_class': 'measurement', 'unit_of_measurement': '°C',
            'friendly_name': 'Temperature', 'localKey': 'PRIVATE'}}]

    def build(self):
        return build_inventory(self.entities, self.devices, self.areas, self.floors, self.states)[0]

    def test_metadata_and_relations(self):
        payload = self.build()
        record = payload['entities'][0]
        for key, value in {'device_class': 'temperature', 'state_class': 'measurement',
                           'unit_of_measurement': '°C', 'device_id': 'a' * 32,
                           'area': 'Room', 'floor': 'Ground', 'disabled': True, 'hidden': True}.items():
            self.assertEqual(record[key], value)
        self.assertEqual(payload['devices'][0]['entities'], ['sensor.temperature'])
        self.assertNotIn('PRIVATE', json.dumps(payload))
        self.assertNotIn('22.4', json.dumps(payload))

    def test_climate_schema_has_no_runtime_values(self):
        self.entities[0]['entity_id'] = 'climate.fixture'
        self.states = [{'entity_id': 'climate.fixture', 'state': 'heat', 'attributes': {
            'current_temperature': 22.4, 'temperature': 23.5, 'hvac_action': 'heating',
            'hvac_modes': ['heat', 'off'], 'min_temp': 5, 'max_temp': 35,
            'target_temp_step': 1, 'supported_features': 385}}]
        record = self.build()['entities'][0]
        self.assertEqual(record['capabilities']['hvac_modes'], ['heat', 'off'])
        self.assertEqual(record['capabilities']['target_temp_step'], 1)
        self.assertIn('hvac_action', record['attribute_keys'])
        for value in ('22.4', '23.5', 'heating'):
            self.assertNotIn(value, json.dumps(record))

    def test_runtime_presence_is_explicit_allowlist(self):
        self.states += [{'entity_id': 'zone.home', 'state': '1', 'attributes': {'latitude': 99}},
                        {'entity_id': 'sensor.unregistered', 'attributes': {}}]
        payload = self.build()
        self.assertEqual(payload['entities'][-1], {'entity_id': 'zone.home', 'domain': 'zone',
                         'runtime_only': True, 'source': 'state_machine_presence'})
        self.assertEqual(payload['counts']['runtime_only'], 1)
        self.assertNotIn('sensor.unregistered', json.dumps(payload))

    def test_deterministic_sorting(self):
        self.entities += [{'entity_id': 'sensor.alpha'}, {'entity_id': 'sensor.zeta'}]
        before = self.build()
        self.entities.reverse()
        self.states.reverse()
        self.assertEqual(before, self.build())

    def test_markdown_uses_only_canonical_data(self):
        canonical = json.loads(json.dumps(self.build()))
        first = render_markdown(canonical)
        self.devices[0]['name'] = 'Raw data changed'
        self.assertEqual(first, render_markdown(canonical))
        canonical['devices'][0]['name'] = 'Canonical changed'
        self.assertIn('Canonical changed', render_markdown(canonical)['DEVICES.md'])
        self.assertNotIn('Raw data changed', render_markdown(canonical)['DEVICES.md'])

    def test_security_exclusion_explicit(self):
        self.entities.append({'entity_id': 'invalid entity'})
        result = self.build()
        self.assertEqual(result['completeness']['status'], 'security_excluded')
        self.assertFalse(result['completeness']['complete'])

    def test_unavailable_metadata_retains_previous_without_schema_guessing(self):
        previous = self.build()
        self.states = []
        payload, observations = build_inventory(self.entities, self.devices, self.areas, self.floors, self.states)
        retain_unobserved_metadata(payload, previous, observations)
        self.assertEqual(payload['entities'][0]['device_class'], 'temperature')
        previous['schema_version'] = 2
        clean = self.build()
        retain_unobserved_metadata(clean, previous, {})
        self.assertIsNone(clean['entities'][0]['device_class'])

    def test_security_module_mirrors_match(self):
        self.assertEqual((ROOT / 'export/security.py').read_bytes(),
                         (ROOT / 'import/security.py').read_bytes())

class PublicationGateTests(unittest.TestCase):
    def test_unknown_fields_and_markdown_drift_block_publication(self):
        import tempfile
        from sync_dashboards import write_json
        from validate_export import validate
        payload, _ = build_inventory([], [], [], [], [])
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'docs').mkdir()
            write_json(repo / 'inventory/entities.json', payload)
            for name, content in render_markdown(payload).items():
                (repo / 'docs' / name).write_text(content)
            validate(repo)
            (repo / 'docs/DEVICES.md').write_text('extra data')
            with self.assertRaises(ValueError):
                validate(repo)
            for name, content in render_markdown(payload).items():
                (repo / 'docs' / name).write_text(content)
            payload['unreviewed_field'] = 'value'
            write_json(repo / 'inventory/entities.json', payload)
            with self.assertRaises(ValueError):
                validate(repo)

    def test_final_snapshot_gate_blocks_nested_credentials(self):
        import tempfile
        from sync_dashboards import write_json
        from validate_export import validate
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_json(repo / 'dashboards/fixture.json', {'views': [{'localKey': 'synthetic'}]})
            with self.assertRaises(ValueError):
                validate(repo)

    def test_unknown_inventory_schema_cannot_bypass_final_gate(self):
        import tempfile
        from sync_dashboards import write_json
        from validate_export import validate
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            write_json(repo / 'inventory/entities.json', {'schema_version': 999, 'unclassified': 'value'})
            with self.assertRaises(ValueError):
                validate(repo)
