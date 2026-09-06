"""Deterministic structural snapshots. Exact identifiers only; no rename inference."""
import hashlib
import json

SECTIONS = {'entities': 'entity_id', 'devices': 'device_id', 'areas': 'area_id', 'floors': 'floor_id'}
RUNTIME = {'state', 'current_state', 'current_temperature', 'temperature', 'humidity',
           'hvac_action', 'target_temperature', 'availability', 'last_changed',
           'last_updated', 'generated_at', 'observed_at'}


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def revision(value):
    return hashlib.sha256(stable(value).encode()).hexdigest()


def structure(snapshot):
    return {'schema_version': snapshot.get('schema_version'),
            'identity_policy': snapshot.get('identity_policy'),
            **{section: sorted([{k: v for k, v in row.items() if k not in RUNTIME}
                               for row in snapshot.get(section, [])], key=lambda x: x[key])
               for section, key in SECTIONS.items()}}


def complete(snapshot, section):
    flags = snapshot.get('completeness', {})
    flag = flags.get('sections', {}).get(section, flags)
    return flag.get('complete') is True and flag.get('status', 'success') == 'success'


def compare(previous, current):
    new = structure(current)
    old = structure(previous) if previous else None
    compatible = old is not None and old['schema_version'] == new['schema_version'] == 3 and old['identity_policy'] == new['identity_policy']
    result = {'schema_version': 1, 'inventory_schema_version': new['schema_version'],
              'baseline_revision': revision(old) if old else None, 'current_revision': revision(new),
              'status': 'compared' if compatible else 'initial_baseline' if old is None and new['schema_version'] == 3 else 'migration_required',
              'completeness': {}, 'sections': {}, 'structural_changes': False}
    for section, key in SECTIONS.items():
        ok = compatible and complete(previous, section) and complete(current, section)
        result['completeness'][section] = {'baseline': bool(previous and complete(previous, section)), 'current': complete(current, section), 'compared': ok}
        changes = {'added': [], 'removed': [], 'changed': []}
        if ok:
            before = {x[key]: x for x in old[section]}
            after = {x[key]: x for x in new[section]}
            changes['added'] = [after[k] for k in sorted(after.keys() - before.keys())]
            changes['removed'] = [before[k] for k in sorted(before.keys() - after.keys())]
            for identifier in sorted(before.keys() & after.keys()):
                a, b = before[identifier], after[identifier]
                fields = {k: {'before': a.get(k), 'after': b.get(k)} for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k) or (k in a) != (k in b)}
                if fields:
                    changes['changed'].append({key: identifier, 'fields': fields})
        result['sections'][section] = changes
    result['structural_changes'] = any(any(s.values()) for s in result['sections'].values())
    result['changes_revision'] = revision(result) if result['structural_changes'] else None
    return result
