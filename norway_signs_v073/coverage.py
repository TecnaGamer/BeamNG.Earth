"""Read-only NVDB 96 data access, bounded pagination, and coverage audit.

Uses the same official endpoint as v0.7, adding type-95 parent relationships.
The renderer is passed in as a callback, so sign artwork can change without
changing the downloaded-data layer.
"""
from __future__ import annotations
from collections import Counter
import json
import math
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def build_url(base, cell_key, cursor=None):
    url = base.build_url(cell_key, cursor)
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query['inkluder'] = 'metadata,egenskaper,geometri,lokasjon,relasjoner'
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))


def reason(obj, normalizer, accepted):
    """Classify excluded records conservatively; none are silently rendered."""
    if accepted is not None:
        return 'accepted'
    if not isinstance(obj, dict):
        return 'malformed_object'
    from .catalog import family
    props = {p.get('id'): p.get('verdi') for p in obj.get('egenskaper', []) if isinstance(p, dict)}
    raw = props.get(5530)
    if not isinstance(raw, str):
        return 'missing_sign_number'
    from re import match
    m = match(r'^\s*(\d{3}(?:\.\d+)?)(?:\s*\.\s*([HV](?:45|90|135)?))?', raw, flags=2)
    if not m:
        return 'missing_sign_number'
    code = m.group(1) + ('.' + m.group(2) if m.group(2) else '')
    fam = family(code)
    if fam is None:
        return 'unsupported_family'
    if fam == 'diagram':
        return 'diagram_not_rendered'
    if fam == 'arrow':
        from .catalog import arrow_side
        if arrow_side(code) is None:
            return 'unsupported_arrow_variant'
    facing = str(props.get(1894, '')).lower().strip()
    if facing not in ('parallelt fra veg', 'parallelt mot veg', 'trafikk i metreringsretning',
                      'trafikk mot metreringsretning', 'tosidig'):
        return 'facing_unsupported_or_missing'
    if facing == 'tosidig' and fam != 'route':
        return 'double_sided_not_yet_rendered'
    geom = obj.get('geometri') or {}
    if geom.get('egengeometri') is not True or geom.get('srid') != 4326:
        return 'physical_geometry_missing_or_wrong_srid'
    if (obj.get('lokasjon') or {}).get('geometri', {}).get('srid') != 4326:
        return 'road_reference_missing_or_wrong_srid'
    if fam not in ('priority', 'route') and not props.get(1910):
        return 'text_missing'
    if fam in ('direction', 'arrow', 'distance') and not props.get(1294):
        return 'color_missing'
    return 'other_render_filter'


def scan(base, key, normalize, fetch=None):
    """Complete scan or raises; returned stats can explain sparse coverage."""
    if fetch is None:
        fetch = base.request_page
    cursor = None
    seen = set()
    records = {}
    stat = Counter()
    kinds = Counter()
    for _ in range(base.MAX_PAGES):
        url = build_url(base, key, cursor)
        if url in seen:
            raise ValueError('Repeated NVDB start cursor')
        seen.add(url)
        page = fetch(url)
        rows = page.get('objekter')
        if not isinstance(rows, list):
            raise ValueError('Invalid NVDB page')
        stat['pages'] += 1
        stat['raw_records'] += len(rows)
        if stat['raw_records'] > base.MAX_OBJECTS:
            raise ValueError('NVDB object budget exceeded, do not cache incomplete cell')
        for raw in rows:
            try:
                parsed = normalize(raw)
            except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                parsed = None
            why = reason(raw, normalize, parsed)
            stat[why] += 1
            if parsed is not None:
                records[parsed['id']] = parsed
                kinds[parsed['kind']] += 1
        if not rows:
            break
        nxt = (page.get('metadata') or {}).get('neste')
        if not nxt:
            break
        cursor = nxt.get('start') if isinstance(nxt, dict) else None
        if not isinstance(cursor, str) or not (1 <= len(cursor) <= 1024) or any(c.isspace() for c in cursor):
            raise ValueError('Invalid official NVDB pagination cursor')
    else:
        raise ValueError('NVDB page limit; refusing partial cache')
    stat['unique_accepted'] = len(records)
    return sorted(records.values(), key=lambda item: item['id']), {
        'cell': list(key), 'statistics': dict(stat), 'accepted_by_family': dict(kinds),
        'note': 'Missing sign models vs missing NVDB records are distinguished. Not all NVDB records are renderable.'
    }
