"""Last fail-closed JSON and canonical Markdown gate before Git staging."""
import json
from pathlib import Path

from inventory import render_markdown, CLIMATE_CONFIG_KEYS, CLIMATE_RUNTIME_KEYS, METADATA_KEYS
from security import safe_entity_id, safe_internal_id, unsafe_reason, normalized_key, SENSITIVE_KEYS

INVENTORY_ROOT = {'schema_version', 'source', 'identity_policy', 'counts', 'entities',
                  'devices', 'areas', 'floors', 'completeness'}
ENTITY_KEYS = {'entity_id', 'domain', 'name', 'friendly_name', 'integration', 'disabled',
               'disabled_by', 'hidden', 'hidden_by', 'entity_category', 'translation_key',
               'device_id', 'device_name', 'manufacturer', 'model', 'area_id', 'area',
               'area_source', 'floor_id', 'floor', 'device_class', 'state_class',
               'unit_of_measurement', 'attribute_keys', 'capabilities', 'runtime_only', 'source'}
DEVICE_KEYS = {'device_id', 'name', 'manufacturer', 'model', 'area_id', 'area', 'floor_id',
               'floor', 'integrations', 'entities', 'entity_count'}


def inventory_values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if normalized_key(key) in SENSITIVE_KEYS or unsafe_reason(key):
                raise ValueError('Forbidden inventory field')
            if key == 'device_id' and child is not None:
                if not safe_internal_id(child):
                    raise ValueError('Unclassified registry identifier')
            elif key == 'entity_id':
                if not safe_entity_id(child):
                    raise ValueError('Invalid entity identifier')
            else:
                inventory_values(child)
    elif isinstance(value, list):
        for child in value:
            inventory_values(child)
    elif unsafe_reason(value):
        raise ValueError('Unsafe inventory value')


def validate(repo):
    inventory = repo / 'inventory/entities.json'
    if inventory.exists():
        payload = json.loads(inventory.read_text())
        if payload.get('schema_version') == 3:
            if set(payload) != INVENTORY_ROOT:
                raise ValueError('Inventory root has no export policy')
            for record in payload['entities']:
                if set(record) - ENTITY_KEYS:
                    raise ValueError('Entity field has no export policy')
                if set(record.get('capabilities', {})) - CLIMATE_CONFIG_KEYS:
                    raise ValueError('Capability field has no export policy')
                if set(record.get('attribute_keys', [])) - (CLIMATE_CONFIG_KEYS | CLIMATE_RUNTIME_KEYS | METADATA_KEYS):
                    raise ValueError('Attribute presence has no export policy')
            for section, keys in (('areas', {'area_id', 'name', 'floor_id'}),
                                  ('floors', {'floor_id', 'name'})):
                if any(set(record) != keys for record in payload[section]):
                    raise ValueError('Registry field has no export policy')
            for record in payload['devices']:
                if set(record) != DEVICE_KEYS:
                    raise ValueError('Device field has no export policy')
            for name, content in render_markdown(payload).items():
                if (repo / 'docs' / name).read_text() != content:
                    raise ValueError('Markdown differs from canonical inventory')
        inventory_values(payload)
    runtime = repo / 'inventory/states.json'
    if runtime.exists():
        inventory_values(json.loads(runtime.read_text()))
    for folder in ('dashboards', 'config/storage'):
        for path in (repo / folder).rglob('*.json'):
            if unsafe_reason(json.loads(path.read_text())):
                raise ValueError('Unsafe configuration snapshot; publication refused')
    for name in ('dashboards.json', 'export-status.json'):
        path = repo / 'inventory' / name
        if path.exists() and unsafe_reason(json.loads(path.read_text())):
            raise ValueError('Unsafe export manifest')


if __name__ == '__main__':
    import sys
    validate(Path(sys.argv[1]))
