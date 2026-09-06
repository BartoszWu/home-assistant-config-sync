"""Markdown is a presentation of persisted analysis JSON only."""
from inventory import md


def changes_markdown(data):
    lines = ['# Inventory changes', '', '> Generated automatically from inventory/changes.json.', '', '## Summary', '',
             'Status: ' + data['status'] + '. Latest structural revision: ' + str(data['changes_revision']) + '.',
             'Baseline: ' + str(data['baseline_revision']) + '. Current: ' + data['current_revision'] + '.', '']
    sections = data['sections']
    for title, section, operation, key in [('New Devices', 'devices', 'added', 'device_id'), ('Removed Devices', 'devices', 'removed', 'device_id'), ('New Entities', 'entities', 'added', 'entity_id'), ('Removed Entities', 'entities', 'removed', 'entity_id'), ('Changed Entities', 'entities', 'changed', 'entity_id'), ('Changed Devices', 'devices', 'changed', 'device_id')]:
        rows = sections[section][operation]
        lines += ['## ' + title, '']
        lines += ['- ' + md(x.get('friendly_name') or x.get('name') or x[key]) + ' (`' + md(x[key]) + '`)' + (': ' + ', '.join(x['fields']) if 'fields' in x else '') for x in rows] or ['None.']
        lines.append('')
    lines += ['## Areas/Floors', '']
    for section in ('areas', 'floors'):
        for operation, rows in sections[section].items():
            for row in rows:
                lines.append('- ' + section + ' ' + operation + ': ' + md(row.get('name') or row.get('area_id') or row.get('floor_id')))
    lines += ['', '## Completeness warnings', '']
    lines += ['- ' + k + ': baseline/current complete = ' + str(v['baseline']) + '/' + str(v['current']) for k, v in data['completeness'].items() if not v['baseline'] or not v['current']] or ['None.']
    return '\n'.join(lines) + '\n'


def dependencies_markdown(data):
    lines = ['# Dependencies', '', '> Generated automatically from inventory/dependencies.json.', '', data['scope'], '',
             'Edges: ' + str(data['edge_count']) + '. ' + md(data['counts']), '']
    for title, statuses in [('CONFIRMED BROKEN', {'confirmed broken'}), ('UNRESOLVED / DYNAMIC', {'unresolved', 'dynamic/unknown'}), ('RUNTIME-ONLY RESOLVED', {'runtime-only resolved'})]:
        lines += ['## ' + title, '']
        lines += ['- ' + md(e['entity_id'] or '(dynamic)') + ' — ' + md(e['source']) + '#' + md(e['json_path']) for e in data['edges'] if e['status'] in statuses] or ['None.']
        lines.append('')
    lines += ['Resolved relationships and exact view/card JSON paths are available on demand in JSON.', '']
    return '\n'.join(lines)


def candidates_markdown(data):
    lines = ['# InfluxDB candidates', '', '> Generated automatically from inventory/influxdb-candidates.json. Suggestions only; no decisions or connection settings.', '',
             md(data['counts']) + '; ' + str(data['entity_count']) + ' entities.', '']
    for scope in sorted({x['scope'] for x in data['candidates']}):
        lines += ['## ' + scope, '']
        rows = [x for x in data['candidates'] if x['scope'] == scope]
        groups = sorted({(x['device'] or 'No device', x['device_id'] or '') for x in rows})
        for name, did in groups:
            group = [x for x in rows if (x['device_id'] or '') == did]
            lines += ['### Device: ' + md(name), '', 'Entities: ' + str(len({x['entity_id'] for x in group})) + '. Area: ' + md(group[0]['area']), '']
            for rating in ('RECOMMENDED', 'NOT_RECOMMENDED', 'NEEDS_REVIEW'):
                lines += ['**' + rating + '**', '']
                lines += ['- `' + md(x['entity_id'] + ('.' + x['attribute'] if x['attribute'] else '')) + '` — ' + md(x['reason']) for x in group if x['recommendation'] == rating] or ['None.']
                lines.append('')
            lines += ['Summary: ' + ' / '.join(str(sum(x['recommendation'] == r for x in group)) + ' ' + r for r in ('RECOMMENDED', 'NOT_RECOMMENDED', 'NEEDS_REVIEW')), '']
    return '\n'.join(lines)


RENDERERS = {'changes': changes_markdown, 'dependencies': dependencies_markdown, 'influxdb-candidates': candidates_markdown}
DOCS = {'changes': 'CHANGES.md', 'dependencies': 'DEPENDENCIES.md', 'influxdb-candidates': 'INFLUXDB-CANDIDATES.md'}


def validate_analysis(repo):
    """Extend the publication gate to derived artifacts and the user policy."""
    import json
    import re
    from security import unsafe_reason, safe_entity_id, safe_internal_id, normalized_key, SENSITIVE_KEYS
    from influx_candidates import reconcile_policy
    def check(value, field=None):
        if isinstance(value, dict):
            for key, child in value.items():
                if unsafe_reason(key) or normalized_key(key) in SENSITIVE_KEYS:
                    raise ValueError('Unsafe analysis field')
                if key == 'target' and value.get('relation') == 'belongs_to_device':
                    if not safe_internal_id(child):
                        raise ValueError('Invalid dependency device identifier')
                    continue
                check(child, field if key in {'before', 'after'} else key)
        elif isinstance(value, list):
            for child in value:
                check(child, field)
        elif field == 'json_path':
            if not isinstance(value, str) or value and not value.startswith('/') or any(unsafe_reason(part) for part in value.split('/') if part):
                raise ValueError('Unsafe JSON pointer')
        elif field == 'device_id' and value is not None:
            if not safe_internal_id(value):
                raise ValueError('Invalid analysis device identifier')
        elif field and field.endswith('revision') and value is not None:
            if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                raise ValueError('Invalid content revision')
        elif field in {'entity_ref', 'reference'}:
            match = re.fullmatch(r'(.+)#generation-([1-9][0-9]*)', value or '')
            if not match or not safe_entity_id(match[1]):
                raise ValueError('Invalid lifetime entity reference')
        elif unsafe_reason(value):
            raise ValueError('Unsafe analysis value in ' + str(field))
    for name in ('changes', 'dependencies', 'influxdb-candidates', 'summary', 'analysis-state'):
        path = repo / 'inventory' / (name + '.json')
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        if name == 'analysis-state':
            # Seed keys are reference strings, checked as references separately.
            payload = dict(payload)
            seeds = payload.pop('candidate_seeds', {})
            for key, value in seeds.items():
                check(key, 'entity_ref')
                check(value)
        check(payload)
        if name in RENDERERS and (repo / 'docs' / DOCS[name]).read_text() != RENDERERS[name](payload):
            raise ValueError('Analysis Markdown differs from canonical JSON')
    path = repo / 'policy/influxdb.json'
    if path.exists():
        policy = json.loads(path.read_text())
        check(policy)
        allowed = {'entity_ref', 'entity_id', 'attribute', 'decision', 'reason', 'reviewed_at', 'revision', 'metadata_revision', 'inactive'}
        if set(policy) != {'schema_version', 'identity_policy', 'decisions'} or any(set(x) - allowed for x in policy['decisions']):
            raise ValueError('Policy contains unsupported fields')
        reconcile_policy(policy, {'entities': []}, {})
