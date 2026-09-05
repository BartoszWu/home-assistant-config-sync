"""Pure allowlisted schema v3 and Markdown renderers for the existing Export."""
import math
import re

from security import safe_entity_id, safe_internal_id, safe_text, text_reason

SCHEMA_VERSION = 3
SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
METADATA_KEYS = frozenset({'friendly_name', 'device_class', 'state_class', 'unit_of_measurement'})
CLIMATE_RUNTIME_KEYS = frozenset({
    'current_temperature', 'current_temperature_decimal', 'temperature',
    'target_temp_high', 'target_temp_low', 'hvac_action', 'current_humidity',
    'humidity', 'fan_mode', 'swing_mode', 'preset_mode',
})
CLIMATE_CONFIG_KEYS = frozenset({'supported_features', 'min_temp', 'max_temp', 'target_temp_step', 'hvac_modes'})
HVAC_MODES = frozenset({'off', 'heat', 'cool', 'heat_cool', 'auto', 'dry', 'fan_only'})


def safe_slug(value):
    return value if isinstance(value, str) and SLUG_RE.fullmatch(value) and not text_reason(value) else None


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def metadata(attributes, domain):
    result = {key: safe_text(attributes.get(key)) for key in sorted(METADATA_KEYS)}
    result['attribute_keys'] = sorted(set(attributes) & METADATA_KEYS)
    result['capabilities'] = {}
    if domain == 'climate':
        result['attribute_keys'] = sorted(set(attributes) & (METADATA_KEYS | CLIMATE_RUNTIME_KEYS | CLIMATE_CONFIG_KEYS))
        for key in sorted(CLIMATE_CONFIG_KEYS):
            if key not in attributes:
                continue
            value = attributes[key]
            if key == 'hvac_modes':
                if isinstance(value, list) and all(isinstance(x, str) and x in HVAC_MODES for x in value):
                    result['capabilities'][key] = sorted(set(value))
            elif number(value) is not None:
                result['capabilities'][key] = value
    return result


def build_inventory(entities, devices, areas, floors, states):
    for values, key in ((entities, 'entity_id'), (devices, 'id'), (areas, 'area_id'),
                        (floors, 'floor_id'), (states, 'entity_id')):
        if not isinstance(values, list) or any(not isinstance(x, dict) or not isinstance(x.get(key), str) for x in values):
            raise ValueError('Incomplete or malformed registry response')
        if len({x[key] for x in values}) != len(values):
            raise ValueError('Duplicate registry identifiers')
    # Only trusted registry fields establish internal identity. Never fall back
    # to name, connections, serial, unique_id or config entry IDs.
    ds = {x['id']: x for x in devices if safe_internal_id(x.get('id'))}
    ars = {x['area_id']: x for x in areas if safe_slug(x.get('area_id'))}
    fs = {x['floor_id']: x for x in floors if safe_slug(x.get('floor_id'))}
    ss = {x['entity_id']: x for x in states if safe_entity_id(x.get('entity_id'))}
    records = []
    excluded = {'entities': 0, 'devices': len(devices) - len(ds), 'areas': len(areas) - len(ars), 'floors': len(floors) - len(fs)}
    observations = {}
    for entity in entities:
        eid = safe_entity_id(entity.get('entity_id'))
        if not eid:
            excluded['entities'] += 1
            continue
        domain = eid.split('.')[0]
        device_id = entity.get('device_id') if entity.get('device_id') in ds else None
        device = ds.get(device_id, {})
        aid = entity.get('area_id') or device.get('area_id')
        aid = aid if aid in ars else None
        area = ars.get(aid, {})
        fid = area.get('floor_id') if area.get('floor_id') in fs else None
        state = ss.get(eid, {})
        attrs = state.get('attributes', {})
        attrs = attrs if isinstance(attrs, dict) else {}
        # Never inspect or copy person/location attributes, even friendly_name.
        if domain in {'person', 'device_tracker', 'zone'}:
            attrs = {}
        observed = metadata(attrs, domain)
        observations[eid] = sorted(set(attrs) & (METADATA_KEYS | CLIMATE_CONFIG_KEYS | CLIMATE_RUNTIME_KEYS))
        rec = {
            'entity_id': eid, 'domain': domain,
            'name': safe_text(entity.get('name') or entity.get('original_name')),
            'friendly_name': observed['friendly_name'] or safe_text(entity.get('name') or entity.get('original_name')),
            'integration': safe_slug(entity.get('platform')),
            'disabled': entity.get('disabled_by') is not None,
            'disabled_by': safe_text(entity.get('disabled_by')),
            'hidden': entity.get('hidden_by') is not None,
            'hidden_by': safe_text(entity.get('hidden_by')),
            'entity_category': safe_text(entity.get('entity_category')),
            'translation_key': safe_text(entity.get('translation_key')),
            'device_id': device_id,
            'device_name': safe_text(device.get('name_by_user') or device.get('name')),
            'manufacturer': safe_text(device.get('manufacturer')),
            'model': safe_text(device.get('model')),
            'area_id': aid, 'area': safe_text(area.get('name')),
            'area_source': 'entity' if entity.get('area_id') and aid else 'device' if aid else None,
            'floor_id': fid, 'floor': safe_text(fs.get(fid, {}).get('name')),
            'device_class': safe_text(entity.get('device_class')) or observed['device_class'] or safe_text(entity.get('original_device_class')),
            'state_class': observed['state_class'],
            'unit_of_measurement': observed['unit_of_measurement'],
            'attribute_keys': observed['attribute_keys'],
            'capabilities': observed['capabilities'],
            'runtime_only': False, 'source': 'entity_registry',
        }
        records.append(rec)
    # Explicit built-in reference policy v1: existence only, no attributes,
    # location, occupancy, friendly name or state. No arbitrary runtime union.
    if 'zone.home' in ss and not any(x['entity_id'] == 'zone.home' for x in records):
        records.append({'entity_id': 'zone.home', 'domain': 'zone',
                        'source': 'state_machine_presence', 'runtime_only': True})
    records.sort(key=lambda x: x['entity_id'])
    safe_devices = []
    for did, device in sorted(ds.items()):
        aid = device.get('area_id') if device.get('area_id') in ars else None
        area = ars.get(aid, {})
        fid = area.get('floor_id') if area.get('floor_id') in fs else None
        members = [x for x in records if x.get('device_id') == did]
        safe_devices.append({
            'device_id': did, 'name': safe_text(device.get('name_by_user') or device.get('name')),
            'manufacturer': safe_text(device.get('manufacturer')), 'model': safe_text(device.get('model')),
            'area_id': aid, 'area': safe_text(area.get('name')),
            'floor_id': fid, 'floor': safe_text(fs.get(fid, {}).get('name')),
            'integrations': sorted({x['integration'] for x in members if x['integration']}),
            'entities': [x['entity_id'] for x in members], 'entity_count': len(members),
        })
    payload = {
        'schema_version': SCHEMA_VERSION,
        'source': 'Home Assistant registries and allowlisted State Machine metadata via WebSocket API',
        'identity_policy': 'entity_id_and_ha_device_id_v1',
        'counts': {'entities': len(records), 'registry_entities': len(records) - sum(x['runtime_only'] for x in records),
                   'runtime_only': sum(x['runtime_only'] for x in records), 'devices': len(ds), 'areas': len(ars), 'floors': len(fs)},
        'entities': records, 'devices': safe_devices,
        'areas': [{'area_id': aid, 'name': safe_text(a['name']), 'floor_id': a.get('floor_id') if a.get('floor_id') in fs else None} for aid, a in sorted(ars.items())],
        'floors': [{'floor_id': fid, 'name': safe_text(f['name'])} for fid, f in sorted(fs.items())],
        'completeness': {'status': 'security_excluded' if any(excluded.values()) else 'success',
                         'complete': not any(excluded.values()), 'security_excluded_counts': excluded},
    }
    return payload, observations


def retain_unobserved_metadata(payload, previous, observations):
    """Missing runtime metadata is unknown, not a capability removal.

    Do not guess schema migrations. Persist last observed metadata only within
    schema v3 and the same exact entity_id. Runtime values never enter this path.
    """
    if not previous or previous.get('schema_version') != SCHEMA_VERSION:
        return
    old = {x['entity_id']: x for x in previous['entities']}
    for rec in payload['entities']:
        prior = old.get(rec['entity_id'])
        if not prior or rec['runtime_only']:
            continue
        observed = observations.get(rec['entity_id'], [])
        for key in METADATA_KEYS:
            if key not in observed and rec.get(key) is None:
                rec[key] = safe_text(prior.get(key))
        # Attribute presence is observed capability, not absence on an offline
        # state. Capability removals require a future explicit schema policy.
        rec['attribute_keys'] = sorted(set(rec['attribute_keys']) | (set(prior.get('attribute_keys', [])) & (METADATA_KEYS | CLIMATE_RUNTIME_KEYS | CLIMATE_CONFIG_KEYS)))
        rec['capabilities'] = {**metadata(prior.get('capabilities', {}), rec['domain'])['capabilities'], **rec['capabilities']}


def md(value):
    if value is None:
        return ''
    if isinstance(value, (list, dict)):
        import json
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')


def table(title, records, columns):
    lines = ['# ' + title, '', '> Generated automatically from canonical JSON.', '',
             '| ' + ' | '.join(columns) + ' |', '|' + '|'.join('---' for _ in columns) + '|']
    lines += ['| ' + ' | '.join(md(rec.get(k)) for k in columns) + ' |' for rec in records]
    return '\n'.join(lines) + '\n'


def render_markdown(payload):
    if payload.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported inventory schema; run Export, do not guess migration')
    entities = table('Home Assistant — Entities', payload['entities'], [
        'entity_id', 'friendly_name', 'domain', 'integration', 'device_id', 'device_name',
        'area', 'floor', 'device_class', 'state_class', 'unit_of_measurement',
        'disabled', 'disabled_by', 'hidden', 'hidden_by', 'runtime_only', 'attribute_keys', 'capabilities'])
    devices = table('Home Assistant — Devices', payload['devices'], [
        'device_id', 'name', 'manufacturer', 'model', 'area', 'floor', 'integrations', 'entities', 'entity_count'])
    devices += '\n' + table('Areas', payload['areas'], ['area_id', 'name', 'floor_id'])
    devices += '\n' + table('Floors', payload['floors'], ['floor_id', 'name'])
    return {'ENTITIES.md': entities, 'DEVICES.md': devices}
