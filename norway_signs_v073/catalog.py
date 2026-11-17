"""Sign family and variant registry. No web, game or renderer dependencies.

Future signs are added here, with their own renderer, rather than modifying NVDB
paging/caching. These IDs are *eligible families*, not proof of a renderable sign.
"""
from __future__ import annotations
import re

FAMILIES = {
    '206': 'priority', '208': 'priority',
    '701': 'diagram', '703': 'diagram', '705': 'diagram',
    '711': 'direction', '713': 'arrow', '725': 'distance', '727': 'place',
    '723': 'route',
}
# Not yet activated: 812-817 supplementary information and 902-912 hazard
# markers. Each needs its own physical-size/material/direction logic.
PLANNED = ('812', '813', '814', '816', '817', '902', '904', '906', '908', '912')
ARROW = re.compile(r'^713\.([HV])(?:45|90|135)?$', re.IGNORECASE)


def family(code):
    return FAMILIES.get(str(code or '').split('.')[0])


def arrow_side(code):
    """'H' or 'V' for supported directional 713 variants, else None."""
    m = ARROW.fullmatch(str(code or ''))
    return m.group(1).upper() if m else None


def double_sided_supported(code):
    """Only standalone route markers use double-sided face geometry in v0.7.3."""
    return family(code) == 'route'
