"""Offline analysis and automatic post-sanitization phase of existing Export."""
import argparse
import json
from copy import deepcopy
from pathlib import Path

from semantic import SECTIONS, compare, complete, revision, structure
from dependencies import graph
from influx_candidates import candidates, reconcile_policy
from analysis_reports import RENDERERS, DOCS


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def sources(repo, status):
    result = []
    manifest = read(repo / 'inventory/dashboards.json', {'dashboards': []})
    for row in manifest['dashboards']:
        name = row.get('file')
        if not name or Path(name).name != name:
            continue
        path = repo / 'dashboards' / name
        if path.exists():
            result.append({'file': str(path.relative_to(repo)), 'kind': 'dashboard', 'content': read(path),
                           'complete': row.get('status') == 'success' and status.get('dashboards', {}).get('complete') is True})
    index = read(repo / 'config/storage/index.json', {'objects': []})
    for row in index['objects']:
        name = row.get('file')
        if not name or row.get('domain') not in {'automation', 'script'}:
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid snapshot path')
        path = repo / 'config/storage' / relative
        if path.exists():
            result.append({'file': str(path.relative_to(repo)), 'kind': row['domain'], 'content': read(path),
                           'complete': row.get('status') == 'success' and status.get('managed_config', {}).get('status') == 'success'})
    return result


def analyze(repo, previous=None):
    current = read(repo / 'inventory/entities.json')
    if not current or current.get('schema_version') != 3:
        raise ValueError('Analysis requires canonical schema 3; migration required')
    status = read(repo / 'inventory/export-status.json', {'sections': {}})['sections']
    current = deepcopy(current)
    if status.get('inventory', {}).get('status') == 'read_error':
        current['completeness'] = {'complete': False, 'status': 'read_error'}
    state_path = repo / 'inventory/analysis-state.json'
    state = read(state_path, {'schema_version': 1, 'identities': {}})
    if state.get('schema_version') != 1:
        raise ValueError('Analysis state migration required')
    baseline = state.get('baseline', previous)
    diff = compare(baseline, current)
    # Initial baseline never marks the whole home as newly added.
    latest = read(repo / 'inventory/changes.json')
    if latest is None or diff['structural_changes']:
        latest = diff
    full = all(complete(current, s) for s in SECTIONS) and diff['status'] != 'migration_required'
    if full:
        ids = {x['entity_id'] for x in current['entities']}
        for eid in sorted(ids):
            identity = state['identities'].get(eid)
            if identity is None or not identity['active']:
                generation = identity['generation'] + 1 if identity else 1
                state['identities'][eid] = {'generation': generation, 'reference': eid + '#generation-' + str(generation), 'active': True}
        for eid, identity in state['identities'].items():
            identity['active'] = eid in ids
        policy = reconcile_policy(read(repo / 'policy/influxdb.json', {'schema_version': 1, 'identity_policy': 'exact_entity_id_lifetime_v1', 'decisions': []}), current, state['identities'])
        dependencies = graph(current, sources(repo, status))
        proposals = candidates(current, diff, dependencies, policy, state)
        state['baseline'] = {**structure(current), 'completeness': current['completeness']}
        write(repo / 'policy/influxdb.json', policy)
        write(state_path, state)
    else:
        dependencies = graph(current, sources(repo, status))
        proposals = read(repo / 'inventory/influxdb-candidates.json', {'schema_version': 1, 'counts': {}, 'entity_count': 0, 'device_count': 0, 'candidate_count': 0, 'candidates': []})
    for name, value in [('changes', latest), ('dependencies', dependencies), ('influxdb-candidates', proposals)]:
        write(repo / 'inventory' / (name + '.json'), value)
        # Read canonical file back: Markdown has precisely one source.
        (repo / 'docs').mkdir(parents=True, exist_ok=True)
        (repo / 'docs' / DOCS[name]).write_text(RENDERERS[name](read(repo / 'inventory' / (name + '.json'))), encoding='utf-8')
    summary = {'schema_version': 1, 'inventory_schema_version': 3,
               'export_revision': revision({'inventory': structure(current), 'sources': sources(repo, status), 'completeness': status}),
               'revision_kind': 'sha256_structural_export_content_not_git_commit',
               'baseline_revision': revision(structure(state['baseline'])) if state.get('baseline') else None,
               'completeness': status, 'analysis_status': 'ready' if full else 'migration_required' if diff['status'] == 'migration_required' else 'incomplete',
               'counts': current['counts'], 'structural_changes': latest['structural_changes'],
               'changes_revision': latest['changes_revision'],
               'new_devices_count': len(latest['sections']['devices']['added']),
               'new_entities_count': len(latest['sections']['entities']['added']),
               'unresolved_dependency_count': dependencies['counts'].get('unresolved', 0) + dependencies['counts'].get('dynamic/unknown', 0),
               'broken_reference_count': dependencies['counts'].get('confirmed broken', 0),
               'influx_unreviewed_count': proposals['candidate_count'],
               'influx_recommended_count': proposals['counts'].get('RECOMMENDED', 0),
               'candidates_revision': revision(proposals),
               'paths': {'changes': 'docs/CHANGES.md', 'dependencies': 'inventory/dependencies.json',
                         'broken_references': 'docs/DEPENDENCIES.md', 'influx_candidates': 'docs/INFLUXDB-CANDIDATES.md',
                         'inventory': 'inventory/entities.json', 'policy': 'policy/influxdb.json'}}
    write(repo / 'inventory/summary.json', summary)
    return diff


def agent_summary(repo, surfaced=False, expected_revision=None):
    summary = read(repo / 'inventory/summary.json')
    summary_revision = revision(summary)
    marker = repo / '.agent-local/surfaced.json'
    if surfaced:
        if expected_revision != summary_revision:
            raise ValueError('Summary changed or expected revision missing; read summary again before acknowledgement')
        # Only the explicit local CLI writes session state; Export never does.
        write(marker, {k: summary[k] for k in ('changes_revision', 'candidates_revision')})
    summary['summary_revision'] = summary_revision
    seen = read(marker, {})
    summary['structural_changes'] = bool(summary['changes_revision'] and seen.get('changes_revision') != summary['changes_revision'])
    if seen.get('candidates_revision') == summary['candidates_revision']:
        summary['influx_unreviewed_count'] = 0
        summary['influx_recommended_count'] = 0
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['analyze', 'summary', 'mark-surfaced'])
    parser.add_argument('repo', type=Path)
    parser.add_argument('--expected-summary-revision')
    args = parser.parse_args()
    result = analyze(args.repo) if args.command == 'analyze' else agent_summary(args.repo, args.command == 'mark-surfaced', args.expected_summary_revision)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
