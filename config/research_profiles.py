"""Portable research presets; deliberately exclude credentials and delivery settings."""
import json
import re
from pathlib import Path

from core.search_service import SEARCH_BACKEND_NAMES


TRACKERS = {'investment', 'investment_china'}
BOUNDS = {'max_queries': 50, 'max_results': 20, 'max_workers': 64}
OPTION_FIELDS = set(BOUNDS) | {'timelimit', 'search_backends', 'no_cache', 'sec_holdings', 'no_checkpoints',
                             'allow_domains', 'deny_domains', 'evidence_only', 'report_style'}


def normalize_domains(values):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError('Domain filters require 1–100 domain names')
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError('Domain filters require domain names, not URLs')
        domain = value.strip().casefold().removesuffix('.')
        try:
            domain = domain.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise ValueError('Invalid domain name') from exc
        if (len(domain) > 253 or not re.fullmatch(
                r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', domain)):
            raise ValueError('Domain filters require domain names, not URLs, ports or wildcards')
        result.append(domain)
    return list(dict.fromkeys(result))


def load_profile(path):
    """Validate a bounded JSON document before any services are constructed."""
    path = Path(path)
    if path.stat().st_size > 128 * 1024:
        raise ValueError('Research profile exceeds 128 KiB')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Research profile must be an object')
    unknown = set(data) - {'schema_version', 'name', 'description', 'tracker', 'topics', 'options'}
    if unknown:
        raise ValueError('Unknown research profile fields: ' + ', '.join(sorted(unknown)))
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise ValueError('Research profile schema_version must be 1')
    for field in ('name', 'description'):
        if field in data and (not isinstance(data[field], str) or len(data[field]) > 2000):
            raise ValueError(f'Profile {field} must be text of at most 2000 characters')
    tracker = data.get('tracker', 'all')
    if not isinstance(tracker, str) or tracker not in TRACKERS | {'all'}:
        raise ValueError('Unknown profile tracker')
    topics = data.get('topics', {})
    if not isinstance(topics, dict) or set(topics) - TRACKERS:
        raise ValueError('Profile topics must map tracker names to topic lists')
    cleaned = {}
    for market, values in topics.items():
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise ValueError(f'Profile {market} topics must contain 1–200 entries')
        if any(not isinstance(t, str) or not t.strip() or len(t) > 300 for t in values):
            raise ValueError('Profile topics must be non-empty text of at most 300 characters')
        cleaned[market] = list(dict.fromkeys(t.strip() for t in values))
    options = data.get('options', {})
    if not isinstance(options, dict) or set(options) - OPTION_FIELDS:
        raise ValueError('Unknown or invalid profile options')
    for field, upper in BOUNDS.items():
        if field in options and (type(options[field]) is not int or not 1 <= options[field] <= upper):
            raise ValueError(f'Profile {field} must be between 1 and {upper}')
    for field in ('no_cache', 'sec_holdings', 'no_checkpoints', 'evidence_only'):
        if field in options and type(options[field]) is not bool:
            raise ValueError(f'Profile {field} must be boolean')
    if 'timelimit' in options and options['timelimit'] not in ('d', 'w', 'm', 'y', 'none'):
        raise ValueError('Invalid profile timelimit')
    if 'report_style' in options and options['report_style'] not in ('full', 'brief'):
        raise ValueError('Profile report_style must be full or brief')
    if 'search_backends' in options:
        values = options['search_backends']
        if (not isinstance(values, list) or not values or
                any(not isinstance(v, str) or v not in SEARCH_BACKEND_NAMES for v in values)):
            raise ValueError('Profile search_backends must contain supported backend names')
        options['search_backends'] = list(dict.fromkeys(values))
    for field in ('allow_domains', 'deny_domains'):
        if field in options:
            options[field] = normalize_domains(options[field])
    return {**data, 'tracker': tracker, 'topics': cleaned, 'options': options}
