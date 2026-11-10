"""French destination legends and conservative green-pole classification.

The official 2025 inventory is a fallback, not a reconstruction of each signed
itinerary: explicit mapped/reviewed colour registers always take precedence.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

DATA = Path(__file__).with_name('data')/'fr_green_poles_2025.json'


def key(value):
    text = unicodedata.normalize('NFKC', str(value)).upper()
    text = unicodedata.normalize('NFKD', text).replace('Œ', 'OE').replace('Æ', 'AE')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = ' '.join(re.findall(r'[A-Z0-9]+', text))
    text = re.sub(r'\bST\b', 'SAINT', text)
    return re.sub(r'\bSTE\b', 'SAINTE', text)


@lru_cache(maxsize=1)
def green_poles():
    doc = json.loads(DATA.read_text(encoding='utf-8'))
    if doc['format'] != 1:
        raise ValueError('Unsupported French green-pole inventory')
    names = set()
    for row in doc['rows']:
        names.add(key(row['name']))
        names.update(key(n) for n in row['name'].split('|'))
    return names


ALIASES = {
    'CLERMONT FD': 'CLERMONT FERRAND',
    'CLERMONT F D': 'CLERMONT FERRAND',
}


def is_green(value):
    name = key(value)
    return ALIASES.get(name, name) in green_poles()


def legend_chars(value):
    """Expand typographic superscripts into existing normal glyphs and scales.

    For example Fᵈ uses the ordinary D in a raised, smaller quad: it never asks
    the shared atlas for a missing modifier-letter glyph or changes its UVs.
    """
    out = []
    for char in unicodedata.normalize('NFC', str(value)):
        super_letter = unicodedata.decomposition(char).startswith('<super>')
        chars = unicodedata.normalize('NFKC', char).upper() if super_letter else char
        for c in chars:
            out.append((c, .60 if super_letter else 1.))
    return out
