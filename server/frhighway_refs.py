"""Small reviewed reference compositions; road placement remains geometry-based."""
from functools import lru_cache
import json
import math
from pathlib import Path

import numpy as np

DATA = Path(__file__).with_name('data') / 'fr_motorway_reference_signs.json'


@lru_cache(maxsize=1)
def catalogue():
    doc = json.loads(DATA.read_text(encoding='utf-8'))
    if doc['format'] != 1:
        raise ValueError('Unsupported French motorway reference catalogue')
    return doc['junctions']


def enrich(objects):
    out = []
    for obj in objects:
        extra = obj.get('exit')
        if not extra or len(extra.get('approach', ())) < 2:
            out.append(obj)
            continue
        path = np.asarray(extra['approach'], float)
        p = path[-1]
        scale = np.array([111320*math.cos(math.radians(p[1])), 110540.])
        delta = (path[-1]-path[-2])*scale
        travel = delta/max(np.linalg.norm(delta), 1e-9)
        matches = [r for r in catalogue() if str(r['exit_ref']) == extra['label'].get('ref')
                   and np.linalg.norm((p-r['junction'])*scale) <= r.get('radius_m', 140.)
                   and travel@np.asarray(r['travel']) > .85]
        if len(matches) == 1:
            r = matches[0]
            extra = dict(extra, through_label=r['main'],
                         label=dict(extra['label'], **r['exit']), fr_reference=r['id'],
                         fr_support=r.get('support', 'gantry'),
                         main_tags=dict(extra.get('main_tags', {}), highway='motorway',
                                        ref=r['route'], lanes=str(r['lanes'])))
            obj = dict(obj, exit=extra)
        out.append(obj)
    return out
