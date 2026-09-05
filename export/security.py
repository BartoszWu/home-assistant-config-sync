"""Shared export policy. import/security.py is an identical build-context mirror.

Keep the existing field allowlists. This module strengthens value validation;
it does not authorize copying raw registry objects or arbitrary state attributes.
"""
import ipaddress
import math
import re
from urllib.parse import unquote, urlsplit

SENSITIVE_KEYS = frozenset({
    'password', 'passwd', 'token', 'accesstoken', 'refreshtoken', 'apikey',
    'clientsecret', 'secret', 'authorization', 'auth', 'authheaders',
    'credential', 'credentials', 'localkey', 'bindkey', 'privatekey',
    'webhookid', 'serial', 'serialnumber', 'userid', 'userids', 'uniqueid',
    'identifiers', 'connections', 'connectionidentifiers', 'mac', 'macaddress',
    'ssid', 'wifipassword', 'latitude', 'longitude', 'gpscoordinates',
    'configentryid', 'configentryids', 'connectionid', 'serialno', 'devid',
})
MAC_RE = re.compile(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])')
DOTTED_MAC_RE = re.compile(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}(?![0-9a-f])')
HEX_MAC_RE = re.compile(r'(?i)\b[0-9a-f]{12}\b')
IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
IPV6_CANDIDATE_RE = re.compile(r'(?<![\w:])[a-fA-F0-9:]*:[a-fA-F0-9:.:%]*(?:%[\w-]+)?')
ASSIGNED_SECRET_RE = re.compile(
    r'(?i)\b(?:password|passwd|(?:api|local|private)[ _-]?key|'
    r'(?:access|refresh)[ _-]?token|client[ _-]?secret|token|secret|'
    r'authorization|serial(?:[ _-]?number)?|user[ _-]?id|bindkey)\b\s*[:=]')
BEARER_RE = re.compile(r'(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+')
CREDENTIAL_RE = re.compile(r'(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')
TOKEN_RE = re.compile(r'(?<![\w.])(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_+/=-]{48,})(?![\w.])')
ENTITY_RE = re.compile(r'^[a-z_][a-z0-9_]*\.[a-z0-9_]{1,240}$')
INTERNAL_ID_RE = re.compile(r'^[a-f0-9]{32}$')


def normalized_key(value):
    return re.sub(r'[^a-z0-9]', '', unquote(str(value)).lower())


def ipv6_present(value):
    for match in IPV6_CANDIDATE_RE.finditer(value):
        try:
            if ipaddress.ip_address(match.group().split('%')[0]).version == 6:
                return True
        except ValueError:
            pass
    return False


def text_reason(value, identifiers=True):
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    if (ASSIGNED_SECRET_RE.search(decoded) or BEARER_RE.search(decoded)
            or CREDENTIAL_RE.search(decoded) or 'PRIVATE KEY-----' in decoded):
        return 'credential-like text'
    if MAC_RE.search(decoded) or DOTTED_MAC_RE.search(decoded) or HEX_MAC_RE.search(decoded):
        return 'MAC or external identifier'
    if IPV4_RE.search(decoded) or ipv6_present(decoded):
        return 'network address'
    for match in re.finditer(r'(?:https?:)?//[^\s<>\"\']+', decoded):
        try:
            url = urlsplit(match.group() if not match.group().startswith('//') else 'https:' + match.group())
            host = url.hostname or ''
            if url.username is not None or url.password is not None:
                return 'URL userinfo'
            if not host or '.' not in host or host.endswith(('.local', '.internal', '.lan', '.ts.net')):
                return 'private or unclassified URL'
        except ValueError:
            return 'invalid URL'
    if identifiers and TOKEN_RE.search(decoded):
        return 'credential-like identifier'
    return None


def safe_entity_id(value):
    # Exact entity IDs are the existing private API contract. Do not replace
    # substrings: a redacted ID would become a fictitious entity reference.
    if isinstance(value, str) and ENTITY_RE.fullmatch(value) and not text_reason(value, identifiers=False):
        return value
    return None


def safe_internal_id(value):
    # Only call on HA registry device.id, never on integration unique_id.
    return value if isinstance(value, str) and INTERNAL_ID_RE.fullmatch(value) else None


def safe_text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 256:
        return '[redacted-long-value]'
    reason = text_reason(value)
    if reason:
        if reason == 'MAC or external identifier':
            value = MAC_RE.sub('[redacted-mac]', value)
            value = DOTTED_MAC_RE.sub('[redacted-mac]', value)
            value = HEX_MAC_RE.sub('[redacted-id]', value)
            return '[redacted-secret]' if text_reason(value) else value
        if reason == 'network address':
            return '[redacted-ip]'
        return '[redacted-secret]'
    return value


def unsafe_reason(value, path='$'):
    """Reject unsafe objects without echoing their data or secret-bearing keys."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or text_reason(key):
                return 'unsafe field name'
            if normalized_key(key) in SENSITIVE_KEYS and child not in (None, '', False):
                return 'sensitive field'
            reason = unsafe_reason(child)
            if reason:
                return reason
    elif isinstance(value, list):
        for child in value:
            reason = unsafe_reason(child)
            if reason:
                return reason
    elif isinstance(value, str):
        if safe_entity_id(value):
            return None
        return text_reason(value)
    elif isinstance(value, float) and not math.isfinite(value):
        return 'non-finite number'
    elif value is not None and not isinstance(value, (int, float, bool)):
        return 'unsupported value type'
    return None
