"""Offline, position-based country lookup, independent of roadside model selection.

The worldwide bundle follows the source territories (including dependencies).
No hit, an overlap of different countries, or a point on an edge
returns None. There is deliberately no nearest-airport fallback. Rings retain
holes and separate islands; longitude is unwrapped per ring at the date line.
"""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import zipfile

import numpy as np

DATA_PATH = Path(__file__).with_name("data") / "country_bounds.zip"
DATA_SHA256 = "9ef7540081e29fbb72dd69b74a44ec37cf87f5814866f7a97b85330ba9f8d846"
ALGORITHM = "point-country-v2"
EPS = 1e-10                 # numerical edge tolerance in degrees, not a data accuracy claim
BANDS = 4                   # quarter-degree latitude buckets of each ring's edges


class _Ring:
    def __init__(self, coordinates):
        p = np.asarray(coordinates, np.float64)
        if p.ndim != 2 or p.shape[1] != 2 or len(p) < 4 or not np.isfinite(p).all():
            raise ValueError("Invalid country ring")
        if not np.array_equal(p[0], p[-1]) or np.any(np.abs(p[:, 1]) > 90):
            raise ValueError("Unclosed/invalid country ring")
        p = p.copy()
        p[:, 0] = np.degrees(np.unwrap(np.radians(p[:, 0])))
        self.lo, self.hi = p.min(axis=0), p.max(axis=0)
        if not np.allclose(p[0], p[-1], atol=EPS, rtol=0):
            raise ValueError("Country ring winds around the pole: use an explicitly cut source polygon")
        self.centre = (self.lo[0] + self.hi[0]) / 2
        a, b = p[:-1], p[1:]
        self.edges = np.column_stack((a, b - a))
        buckets = {}
        for i, (low, high) in enumerate(zip(np.minimum(a[:, 1], b[:, 1]), np.maximum(a[:, 1], b[:, 1]))):
            for k in range(math.floor((low - EPS) * BANDS), math.floor((high + EPS) * BANDS) + 1):
                buckets.setdefault(k, []).append(i)
        self.buckets = {k: np.asarray(v, np.int32) for k, v in buckets.items()}

    def locate(self, lon, lat):
        """-1 outside, 0 on boundary, 1 inside."""
        lon += 360 * round((self.centre - lon) / 360)
        if not (self.lo[0] - EPS <= lon <= self.hi[0] + EPS and
                self.lo[1] - EPS <= lat <= self.hi[1] + EPS):
            return -1
        ids = self.buckets.get(math.floor(lat * BANDS))
        if ids is None:
            return -1
        x, y, dx, dy = self.edges[ids].T
        # Distance to the segment, including horizontal and zero-length edges.
        length2 = dx * dx + dy * dy
        t = np.clip(np.divide((lon - x) * dx + (lat - y) * dy, length2,
                              out=np.zeros_like(dx), where=length2 > 0), 0., 1.)
        if np.any((lon - x - t * dx) ** 2 + (lat - y - t * dy) ** 2 <= EPS ** 2):
            return 0
        crossing = (y > lat) != (y + dy > lat)
        at = x[crossing] + (lat - y[crossing]) * dx[crossing] / dy[crossing]
        return 1 if np.count_nonzero(at > lon) % 2 else -1


class CountryIndex:
    def __init__(self, geojson):
        self.parts = []
        self.buckets = {}
        for feature in geojson["features"]:
            country = feature["properties"]["country"]
            if not isinstance(country, str) or len(country) != 2 or not country.isupper():
                raise ValueError("Invalid country code")
            geometry = feature["geometry"]
            if geometry["type"] == "Polygon":
                parts = [geometry["coordinates"]]
            elif geometry["type"] == "MultiPolygon":
                parts = geometry["coordinates"]
            else:
                raise ValueError("Country areas must be polygons")
            for part in parts:
                if not part:
                    raise ValueError("Empty country polygon")
                rings = tuple(_Ring(r) for r in part)
                i = len(self.parts)
                self.parts.append((country, rings))
                for k in range(math.floor(rings[0].lo[1] - EPS), math.floor(rings[0].hi[1] + EPS) + 1):
                    self.buckets.setdefault(k, []).append(i)

    def at(self, lat, lon):
        """ISO alpha-2 code or None; no inference beyond the supplied polygons."""
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        found = set()
        boundary = False
        for i in self.buckets.get(math.floor(lat), ()):
            code, rings = self.parts[i]
            outer = rings[0].locate(lon, lat)
            if outer < 0:
                continue
            boundary |= outer == 0
            holes = [r.locate(lon, lat) for r in rings[1:]]
            boundary |= 0 in holes
            if outer == 1 and not any(h >= 0 for h in holes):
                found.add(code)
        return next(iter(found)) if len(found) == 1 and not boundary else None


class WorldIndex:
    """Small bounding-box index; only nearby polygon rings enter the bounded LRU.

    Each archive member is a complete, unmodified source polygon (no artificial
    tile seams). The archive checksum authenticates the index and all members.
    A lock is unnecessary: separate ZipFile handles, immutable cached results.
    """
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.metadata = json.loads(z.read("index.json"))
        self.parts = self.metadata["parts"]
        self.buckets = {}
        for i, part in enumerate(self.parts):
            lo, hi = part["bounds"][1], part["bounds"][3]
            for k in range(math.floor(lo - EPS), math.floor(hi + EPS) + 1):
                self.buckets.setdefault(k, []).append(i)
        self._load = lru_cache(maxsize=8)(self._load_part)

    def _load_part(self, i):
        with zipfile.ZipFile(self.path) as z:
            rings = json.loads(z.read(self.parts[i]["member"]))
        return tuple(_Ring(r) for r in rings)

    def at(self, lat, lon):
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        found, boundary = set(), False
        for i in self.buckets.get(math.floor(lat), ()):
            part = self.parts[i]
            x0, y0, x1, y1 = part["bounds"]
            x = lon + 360 * round(((x0 + x1) / 2 - lon) / 360)
            if not (x0 - EPS <= x <= x1 + EPS and y0 - EPS <= lat <= y1 + EPS):
                continue
            rings = self._load(i)
            outer = rings[0].locate(lon, lat)
            if outer < 0:
                continue
            holes = [r.locate(lon, lat) for r in rings[1:]]
            boundary |= outer == 0 or 0 in holes
            if outer == 1 and not any(h >= 0 for h in holes):
                found.add(part["country"])
        return next(iter(found)) if len(found) == 1 and not boundary else None


@lru_cache(maxsize=1)
def bundled():
    """Loaded once per process. A missing/corrupt bundle is an explicit error."""
    with DATA_PATH.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    if digest != DATA_SHA256:
        raise ValueError("Country boundary data checksum mismatch: " + str(DATA_PATH))
    return WorldIndex(DATA_PATH)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lat", type=float)
    parser.add_argument("lon", type=float)
    args = parser.parse_args()
    print(bundled().at(args.lat, args.lon) or "unknown")
    print(ALGORITHM, DATA_SHA256)
