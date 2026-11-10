"""Short-range road tracing for estimated town approaches, independent of tiles.

Features keep their own station origin. Connected pieces supply the +/-120 m
context only; they do not change candidate locations or create synthetic roads.
"""
import math
import numpy as np


class Context:
    def __init__(self, roads):
        import furniture as f
        self.rows = list(roads)
        self.lengths = [float(f._arclen(r[2])[-1]) for r in self.rows]
        self.ends = {}
        for i, row in enumerate(self.rows):
            for end in (0, -1):
                p = np.asarray(row[2][end])
                key = (math.floor(p[0]/2), math.floor(p[1]/2))
                self.ends.setdefault(key, []).append((i, end, p))

    def point(self, index, station):
        """Position/tangent at a signed station, following unambiguous continuations."""
        import furniture as f
        seen = set()
        for _ in range(32):
            if index in seen:
                return None
            seen.add(index)
            fid, spec, line, _ = self.rows[index]
            length = self.lengths[index]
            if 0 <= station <= length:
                p, d = f._at(np.asarray(line), np.array([station]))
                return p[0], d[0]
            end = 0 if station < 0 else -1
            p = np.asarray(line[end])
            u = p[:2] - np.asarray(line[1 if end == 0 else -2])[:2]
            u /= max(np.linalg.norm(u), 1e-9)
            remaining = -station if station < 0 else station-length
            key = (math.floor(p[0]/2), math.floor(p[1]/2))
            candidates = []
            for x in range(key[0]-1, key[0]+2):
                for y in range(key[1]-1, key[1]+2):
                    for j, e, q in self.ends.get((x, y), ()):
                        if j in seen or np.linalg.norm(q[:2]-p[:2]) > 1.5:
                            continue
                        other = self.rows[j]
                        v = np.asarray(other[2][1 if e == 0 else -2])[:2]-q[:2]
                        v /= max(np.linalg.norm(v), 1e-9)
                        alignment = float(u@v)
                        if alignment > .55:
                            candidates.append((alignment, str(other[0]), j, e))
            candidates.sort(key=lambda a: (-a[0], a[1], a[2]))
            if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0]<.12):
                return None
            _, _, index, end = candidates[0]
            station = remaining if end == 0 else self.lengths[index]-remaining
        return None
