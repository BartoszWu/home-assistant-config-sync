"""User-owned decisions and explainable suggestions, never HA configuration."""
from collections import Counter
from copy import deepcopy
from semantic import revision, structure

METRICS = {'temperature', 'humidity', 'power', 'energy', 'gas', 'water', 'volume',
           'carbon_dioxide', 'carbon_monoxide', 'pm1', 'pm25', 'pm10', 'aqi',
           'volatile_organic_compounds', 'volatile_organic_compounds_parts', 'precipitation'}
TECHNICAL = {'battery', 'signal_strength', 'timestamp', 'duration', 'data_size', 'data_rate'}
CLIMATE = {'current_temperature_decimal', 'current_temperature', 'temperature', 'target_temp_high', 'target_temp_low',
           'hvac_action', 'current_humidity', 'humidity'}


def fingerprint(entity):
    return revision(structure({'schema_version': 3, 'entities': [entity]})['entities'][0])


def reconcile_policy(policy, inventory, identities):
    policy = deepcopy(policy)
    if policy.get('schema_version') != 1 or not isinstance(policy.get('decisions'), list):
        raise ValueError('Unsupported Influx policy')
    current = {x['entity_id']: x for x in inventory['entities']}
    seen = set()
    for row in policy['decisions']:
        if row.get('decision') not in {'INCLUDE', 'EXCLUDE', 'PENDING'} or not isinstance(row.get('reason'), str) or not row['reason'].strip():
            raise ValueError('Each policy decision requires INCLUDE/EXCLUDE/PENDING and reason')
        key = (row.get('entity_ref'), row.get('attribute'))
        if not isinstance(key[0], str) or key in seen or not isinstance(row.get('entity_id'), str):
            raise ValueError('Invalid or duplicate policy reference')
        seen.add(key)
        eid = row['entity_id']
        entity = current.get(eid)
        identity = identities.get(eid, {})
        row['inactive'] = not entity or row['entity_ref'] != identity.get('reference') or not identity.get('active')
        if entity and not row['inactive'] and row.get('attribute'):
            row['inactive'] = row['attribute'] not in entity.get('attribute_keys', [])
        # Capture the first observed metadata when a human adds a decision. This
        # is lifecycle metadata, not a new decision. Never refresh it thereafter.
        if entity and not row['inactive']:
            row.setdefault('metadata_revision', fingerprint(entity))
    policy['decisions'].sort(key=lambda x: (x['entity_ref'], x.get('attribute') or ''))
    return policy


def assess(entity, attribute=None, used_by=()):
    domain, cls = entity.get('domain'), entity.get('device_class')
    if entity.get('disabled'):
        return 'NOT_RECOMMENDED', 'Entity disabled in registry; enable and review metadata before collecting.'
    if attribute == 'current_temperature_decimal':
        return 'NEEDS_REVIEW', 'Observed fractional temperature component; review combination with current_temperature, not an independent temperature series.'
    if attribute == 'current_temperature' and 'current_temperature_decimal' in entity.get('attribute_keys', []):
        return 'NEEDS_REVIEW', 'A separate decimal component is observed; compare the combined temperature helper before choosing a series.'
    if attribute in CLIMATE:
        return 'RECOMMENDED', 'Observed climate attribute: environmental measurement, target or heating/cooling activity.'
    if domain == 'climate':
        return 'NEEDS_REVIEW', 'Climate state is a mode; review observed attributes separately for measurement series.'
    if domain in {'button', 'update'}:
        return 'NOT_RECOMMENDED', 'Command or firmware lifecycle, not a long-term household measurement.'
    if domain == 'sensor' and entity.get('integration') == 'history_stats':
        return 'NEEDS_REVIEW', 'Derived historical duration/count/ratio; compare source activity series and overlapping windows to avoid redundancy.'
    if cls in TECHNICAL or entity.get('entity_category') in {'diagnostic', 'config'}:
        return 'NOT_RECOMMENDED', 'Technical diagnostic/configuration metadata; limited household metrics value.'
    if domain == 'sensor' and cls in METRICS:
        return 'RECOMMENDED', 'Measurement device_class=' + cls + '; state_class=' + str(entity.get('state_class')) + '; unit=' + str(entity.get('unit_of_measurement')) + '.'
    if domain == 'sensor' and entity.get('state_class') in {'measurement', 'total', 'total_increasing'} and entity.get('unit_of_measurement'):
        return 'NEEDS_REVIEW', 'Numeric measurement metadata present, but physical meaning needs review.'
    if domain == 'binary_sensor' and entity.get('integration') == 'template' and 'used_by_dashboard' in used_by:
        return 'NEEDS_REVIEW', 'Derived binary activity already used by a dashboard; inspect source semantics and overlap with climate activity before collection.'
    if domain == 'select':
        return 'NEEDS_REVIEW', 'Selectable mode; relevance to heating or other long-term behavior needs a human decision.'
    return 'NOT_RECOMMENDED', 'No observed long-term measurement schema; no inference from entity name.'


def candidates(inventory, diff, dependencies, policy, state):
    entities = {x['entity_id']: x for x in inventory['entities']}
    decisions = {(x['entity_ref'], x.get('attribute')): x for x in policy['decisions'] if not x['inactive']}
    seeds = state.setdefault('candidate_seeds', {})
    usage = {eid: sorted({e['relation'] for e in dependencies['edges'] if e['entity_id'] == eid and e['relation'].startswith('used_by_')}) for eid in entities}
    initial = not state.get('bootstrap_complete')
    added = {x['entity_id'] for x in diff['sections']['entities']['added']}
    changed = {x['entity_id'] for x in diff['sections']['entities']['changed']}
    for eid, entity in sorted(entities.items()):
        if entity.get('runtime_only'):
            continue
        ref = state['identities'][eid]['reference']
        rating, _ = assess(entity, used_by=usage[eid])
        if eid in added:
            seeds.setdefault(ref, 'NEW RECORDS')
        elif initial and not entity.get('disabled') and (rating == 'RECOMMENDED' or entity.get('domain') == 'climate' or (rating == 'NEEDS_REVIEW' and entity.get('domain') in {'sensor', 'binary_sensor'})):
            seeds.setdefault(ref, 'INITIAL LONG-TERM METRICS REVIEW')
        elif eid in changed:
            seeds.setdefault(ref, 'METADATA REVIEW')
    state['bootstrap_complete'] = True
    items = []
    for eid, entity in sorted(entities.items()):
        identity = state['identities'][eid]
        ref = identity['reference']
        if ref not in seeds or not identity['active']:
            continue
        attrs = [None] + sorted(set(entity.get('attribute_keys', [])) & CLIMATE) if entity.get('domain') == 'climate' else [None]
        for attr in attrs:
            decision = decisions.get((ref, attr)) or decisions.get((ref, None))
            if decision and (decision['decision'] != 'PENDING' or decision.get('metadata_revision') == fingerprint(entity)):
                continue
            rating, reason = assess(entity, attr, usage[eid])
            peers = sorted(x['entity_id'] for x in entities.values() if x['entity_id'] != eid and entity.get('device_id') and x.get('device_id') == entity['device_id'] and entity.get('device_class') and x.get('device_class') == entity['device_class'])
            if peers:
                reason += ' Same device/class also has: ' + ', '.join(peers) + '; check redundancy before including both.'
            uses = usage[eid]
            items.append({'entity_ref': ref, 'entity_id': eid, 'attribute': attr,
                          'scope': seeds[ref], 'review_status': 'PENDING_METADATA_CHANGED' if decision else 'UNREVIEWED',
                          'recommendation': rating, 'reason': reason,
                          'metadata_revision': fingerprint(entity), 'device_id': entity.get('device_id'),
                          'device': entity.get('device_name'), 'area': entity.get('area'),
                          'integration': entity.get('integration'), 'domain': entity.get('domain'),
                          'device_class': entity.get('device_class'), 'state_class': entity.get('state_class'),
                          'unit': entity.get('unit_of_measurement'), 'attribute_keys': entity.get('attribute_keys', []),
                          'value_type': 'categorical' if attr == 'hvac_action' or entity.get('domain') == 'climate' and attr is None else 'numeric metadata' if attr or entity.get('state_class') else 'unknown',
                          'used_by': uses, 'redundant_peers': peers})
    return {'schema_version': 1, 'policy_path': 'policy/influxdb.json',
            'counts': {k: sum(x['recommendation'] == k for x in items) for k in ('RECOMMENDED', 'NOT_RECOMMENDED', 'NEEDS_REVIEW')},
            'entity_count': len({x['entity_ref'] for x in items}), 'candidate_count': len(items),
            'device_count': len({x['device_id'] for x in items if x['device_id']}),
            'ungrouped_entity_count': len({x['entity_id'] for x in items if not x['device_id']}), 'candidates': items}
