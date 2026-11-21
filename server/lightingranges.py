"""Resolve direct OSM lighting at a station on the original, unsplit Overture line."""
import numpy as np

import mapgeom


class LightingRanges:
    def __init__(self, lines, spans):
        self.spans = spans
        self.has_yes = any(value == "yes" for value, _, _ in spans)
        self.starts = np.concatenate([line[:-1] for line in lines])
        self.delta = np.concatenate([np.diff(line, axis=0) for line in lines])
        lengths = np.linalg.norm(self.delta, axis=1)
        self.length2 = np.maximum(lengths ** 2, 1e-20)
        chain = np.r_[0., np.cumsum(lengths)]
        total = max(chain[-1], 1e-10)
        self.start = chain[:-1] / total
        self.width = lengths / total

    def at(self, point):
        rel = point[:2] - self.starts
        t = np.clip(np.sum(rel * self.delta, axis=1) / self.length2, 0., 1.)
        k = np.argmin(np.sum((rel - t[:, None] * self.delta) ** 2, axis=1))
        fraction = self.start[k] + self.width[k] * t[k]
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
        return LightingRanges(mapgeom.enu_rings(f, frame, rings), rules.for_feature(i))
