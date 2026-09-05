"""Section completeness and conservative publication for the existing Export."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from security import unsafe_reason
from inventory import retain_unobserved_metadata, render_markdown
from sync_dashboards import digest, load_json, write_json, sync


def collect():
    status = {'schema_version': 1, 'source': 'HA Config Sync Export',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'sections': {}}
    for section, script in [('inventory', 'exporter.py'), ('dashboards', 'dashboard_exporter.py'),
                            ('managed_config', 'managed_config_exporter.py')]:
        # Never include API exception bodies, headers or raw HA objects in logs.
        try:
            result = subprocess.run(['python3', '/' + script], capture_output=True, timeout=900)
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            success = False
        status['sections'][section] = {'status': 'success' if success else 'read_error',
                                       'complete': success}
        print(f"{section}: {status['sections'][section]['status']}")
    write_json(Path('/export/export-status.json'), status)


def publish_managed(repo, source):
    """Unknown/unsupported reads preserve snapshots; only explicit security exclusion removes one."""
    destination = repo / 'config/storage'
    index = load_json(source / 'index.json')
    for record in index['objects']:
        eid = record['entity_id']
        relative = Path(record['domain'] + 's') / (eid + '.json')
        target = destination / relative
        state = record['status']
        if state == 'success':
            value = load_json(source / relative)
            if unsafe_reason(value):
                raise ValueError('Unsafe managed snapshot refused')
            write_json(target, value)
        elif state == 'security_excluded':
            target.unlink(missing_ok=True)
        else:
            record['retained_previous'] = target.exists()
    # Objects missing from the registry remain unknown until a future explicit
    # deletion workflow. This exporter never infers removal from a failed read.
    indexed = {str(Path(x['domain'] + 's') / (x['entity_id'] + '.json')) for x in index['objects']}
    for path in sorted(destination.glob('*/*.json')):
        relative = str(path.relative_to(destination))
        if relative not in indexed:
            index['objects'].append({'file': relative, 'exported': False,
                                     'status': 'unsupported', 'retained_previous': True})
    for path in destination.glob('*/*.json'):
        if unsafe_reason(load_json(path)):
            raise ValueError('Unsafe retained managed snapshot refused')
    write_json(destination / 'index.json', index)
    return {'complete': all(x['status'] == 'success' for x in index['objects']),
            'statuses': sorted({x['status'] for x in index['objects']})}


def publish(repo, source=Path('/export'), dashboards=Path('/tmp/ha-current/dashboards')):
    status = load_json(source / 'export-status.json')
    sections = status['sections']
    if sections['inventory']['complete']:
        canonical = load_json(source / 'entities.json')
        previous_path = repo / 'inventory/entities.json'
        previous = load_json(previous_path) if previous_path.exists() else None
        observations = load_json(source / 'metadata-observations.json')
        retain_unobserved_metadata(canonical, previous, observations)
        write_json(source / 'entities.json', canonical)
        for filename, content in render_markdown(canonical).items():
            (source / filename).write_text(content, encoding='utf-8')
        for name, folder in [('entities.json', 'inventory'), ('states.json', 'inventory'),
                             ('ENTITIES.md', 'docs'), ('DEVICES.md', 'docs'), ('STATES.md', 'docs')]:
            target = repo / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        sections['inventory'].update(load_json(source / 'entities.json')['completeness'])
    else:
        sections['inventory']['retained_previous'] = True
    if sections['managed_config']['complete']:
        sections['managed_config'].update(publish_managed(repo, source / 'config/storage'))
    else:
        sections['managed_config']['retained_previous'] = True
    if sections['dashboards']['complete']:
        manifest = load_json(dashboards / 'index.json')
        if any(x.get('exported') for x in manifest['dashboards']):
            sync(repo, dashboards)
        for record in manifest['dashboards']:
            filename = record.get('file')
            if filename:
                desired = repo / 'dashboards' / filename
                record['git_ha_status'] = 'SAME' if desired.exists() and digest(load_json(desired)) == digest(load_json(dashboards / filename)) else 'DIFFERENT'
            else:
                record['git_ha_status'] = 'UNKNOWN'
        known = {x.get('file') for x in manifest['dashboards']}
        known_paths = {x.get('url_path') for x in manifest['dashboards']}
        for path in sorted((repo / 'dashboards').glob('*.json')):
            if path.name not in known and path.stem not in known_paths:
                if unsafe_reason(path.name):
                    manifest['dashboards'].append({'status': 'security_excluded', 'exported': False, 'git_ha_status': 'UNKNOWN'})
                else:
                    manifest['dashboards'].append({'file': path.name, 'status': 'unsupported', 'exported': False,
                        'git_ha_status': 'GIT_ONLY_OR_UNREAD', 'reason': 'No successful HA snapshot; deletion is not inferred'})
        manifest['dashboards'].sort(key=lambda x: x.get('url_path') or '')
        write_json(repo / 'inventory/dashboards.json', manifest)
        sections['dashboards']['complete'] = all(x['status'] in {'success', 'intentionally_excluded'} for x in manifest['dashboards']) and manifest['resources_status'] == 'success'
        sections['dashboards']['statuses'] = sorted({x['status'] for x in manifest['dashboards']} | {manifest['resources_status']} | {x['status'] for x in manifest.get('scope_exclusions', [])})
    else:
        sections['dashboards']['retained_previous'] = True
    write_json(repo / 'inventory/export-status.json', status)


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 1:
        collect()
    else:
        publish(Path(sys.argv[1]))
