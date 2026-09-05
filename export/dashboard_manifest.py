"""Allowlisted frontend metadata for the existing dashboard exporter."""
import re
from security import unsafe_reason


def resource_record(resource):
    """Only local frontend resource paths. Drop HACS cache-busting metadata."""
    if not isinstance(resource, dict):
        return {'status': 'security_excluded'}
    url = resource.get('url')
    kind = resource.get('type')
    if not isinstance(url, str) or kind not in {'module', 'js'}:
        return {'status': 'security_excluded'}
    if not re.fullmatch(r'/(?:hacsfiles|local)/[A-Za-z0-9_./-]+\.js(?:\?hacstag=[0-9]+)?', url) or '..' in url:
        return {'status': 'security_excluded'}
    path = url.split('?', 1)[0]
    if unsafe_reason(path):
        return {'status': 'security_excluded'}
    return {'url': path, 'type': kind, 'status': 'success'}


def custom_dependencies(value):
    found = set()
    def walk(node):
        if isinstance(node, dict):
            kind = node.get("type")
            if isinstance(kind, str) and re.fullmatch(r"custom:[a-zA-Z0-9_-]+", kind):
                found.add(kind)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    return sorted(found)


