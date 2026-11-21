"""Resolve direct OSM lighting at a station on the original, unsplit Overture line."""
from functools import cached_property

import numpy as np

import mapgeom


class LightingRanges:
    def __init__(self, lines, spans, *, feats=None, frame=None):
        self.spans = [(value, start, end) for value, start, end in spans if value in {"yes", "no"}]
        self.has_yes = any(value == "yes" for value, _, _ in self.spans)
        self.lines, self.feats, self.frame = lines, feats, frame

    @cached_property
    def _projection(self):
        # Most road spans are queried for their tags without ever receiving a lamp station.
        lines = mapgeom.enu_rings(self.feats, self.frame, self.lines) if self.frame is not None else self.lines
        starts = np.concatenate([line[:-1] for line in lines])
        delta = np.concatenate([np.diff(line, axis=0) for line in lines])
        lengths = np.linalg.norm(delta, axis=1)
        length2 = np.maximum(lengths ** 2, 1e-20)
        chain = np.r_[0., np.cumsum(lengths)]
        total = max(chain[-1], 1e-10)
        return starts, delta, length2, chain[:-1] / total, lengths / total

    def at(self, point):
        if not self.spans:
            return ""
        starts, delta, length2, start, width = self._projection
        rel = point[:2] - starts
        t = np.clip(np.sum(rel * delta, axis=1) / length2, 0., 1.)
        k = np.argmin(np.sum((rel - t[:, None] * delta) ** 2, axis=1))
        fraction = start[k] + width[k] * t[k]
        yes = False
        for value, start, end in self.spans:
            if start <= fraction < end or (end == 1 and fraction >= 1 - 1e-12):
                if value == "no":
                    return "no"
                yes |= value == "yes"
        return "yes" if yes else ""


def ranges(f, i, frame):
    rules = f.rules.get("osm_lighting")
    if rules is None or not len(rules.rows(i)):
        return None
    rings = [ring for part in f.parts(i) for ring in part if len(ring) >= 2]
    if rings:
        return LightingRanges(rings, rules.for_feature(i), feats=f, frame=frame)
