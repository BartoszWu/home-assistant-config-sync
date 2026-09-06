"""Conservative static references; never evaluates templates or scripts."""
import re
from collections import Counter
from semantic import complete, stable

ENTITY = re.compile(r'^[a-z_][a-z0-9_]*\.[a-z0-9_]+$')
LITERAL = re.compile(r"(?:states|is_state|is_state_attr|state_attr|expand)\(\s*['\"]([a-z_][a-z0-9_]*\.[a-z0-9_]+)['\"]")
DOT = re.compile(r'\bstates\.([a-z_][a-z0-9_]*\.[a-z0-9_]+)\b')
REF_KEYS = {'entity', 'entity_id', 'entities', 'zone'}


def graph(inventory, sources):
    known = {x['entity_id']: x for x in inventory.get('entities', [])}
    edges = []
    def add(target, relation, source, path, source_complete=True, dynamic=False):
        if dynamic:
            status = 'dynamic/unknown'
        elif target in known:
            status = 'runtime-only resolved' if known[target].get('runtime_only') else 'resolved'
        else:
            status = 'confirmed broken' if source_complete and complete(inventory, 'entities') else 'unresolved'
        edges.append({'entity_id': target, 'relation': relation, 'source': source,
                      'json_path': path, 'status': status})
    for index, (eid, entity) in enumerate(sorted(known.items())):
        for field, relation in [('device_id', 'belongs_to_device'), ('area_id', 'belongs_to_area'), ('floor_id', 'belongs_to_floor'), ('integration', 'integration')]:
            if entity.get(field):
                edges.append({'entity_id': eid, 'relation': relation, 'target': entity[field],
                              'source': 'inventory/entities.json', 'json_path': '/entities/' + str(index) + '/' + field, 'status': 'resolved'})
    def walk(value, source, path, relation, source_complete, reference=False):
        if isinstance(value, dict):
            for key, child in sorted(value.items()):
                escaped = key.replace('~', '~0').replace('/', '~1')
                walk(child, source, path + '/' + escaped, relation, source_complete, key in REF_KEYS)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, source, path + '/' + str(i), relation, source_complete, reference)
        elif isinstance(value, str):
            template = any(x in value for x in ('{{', '{%', '[[['))
            if template:
                # Keep a dynamic marker even when some literal arguments resolve.
                # Never copy template text into the graph.
                add(None, relation, source, path, source_complete, dynamic=True)
                if '[[[' not in value:
                    for target in sorted(set(LITERAL.findall(value) + DOT.findall(value))):
                        add(target, relation, source, path, False)
            elif reference:
                for target in sorted(set(x.strip() for x in value.split(','))):
                    if ENTITY.fullmatch(target):
                        add(target, relation, source, path, source_complete)
                    elif target not in {'all', 'none', ''}:
                        add(None, relation, source, path, False, dynamic=True)
    for source in sources:
        walk(source['content'], source['file'], '', 'used_by_' + source['kind'], source['complete'])
    edges = sorted({stable(e): e for e in edges}.values(), key=stable)
    counts = dict(sorted(Counter(e['status'] for e in edges).items()))
    return {'schema_version': 1, 'scope': 'Canonical relationships and static references in exported dashboard/automation/script snapshots; helper internals unavailable',
            'source_completeness': [{'file': x['file'], 'complete': x['complete']} for x in sorted(sources, key=lambda x: x['file'])],
            'counts': counts, 'edge_count': len(edges), 'edges': edges}
