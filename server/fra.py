"""The US Federal Railroad Administration's Highway-Rail Crossing Inventory (Form 71, current; public domain, a work of
the US government): each open at-grade crossing's place and warning devices, so a level crossing gets the gates, lamps
or crossbucks it really has (railways.crossing_props; the user, 2026-10-27, OK'd "1 and 2": OSM rail tags and the US
rail data).

Read from BeamNG.Earth-data's GitHub Pages (tools/build_fra_shards.py, rebuilt weekly with the rail file by its GitHub
Action: the user, "could we automate some of the data with github actions?"): one small shard per ~300 km instead of a
query per map cell to data.transportation.gov from every player. Outside the US box nothing is asked. Unreachable:
None, and the crossings take the road class's kit.
"""
import logging
import threading

import numpy as np

import railtags

log = logging.getLogger("earthd.fra")

MATCH_M = 40.0          # m between a map crossing and the inventory's point (its places are approximate)
# the US and its territories, roughly (no request elsewhere: the inventory has nothing there)
US_BOXES = ((24.0, -125.5, 49.6, -66.5), (51.0, -180.0, 72.0, -129.0), (18.5, -161.0, 22.5, -154.5),
            (17.6, -68.0, 18.6, -65.2))


def in_us(lat, lon):
    return any(a <= lat <= c and b <= lon <= d for a, b, c, d in US_BOXES)


class Crossings:
    """The inventory's crossings by map cell (thread-safe)."""

    def __init__(self, cache_dir, fetcher):
        self.data = railtags.Shards(cache_dir, fetcher, "fra")
        self.lock = threading.Lock()
        self.pool = None
        self.pending = {}                   # cell -> Future of a prefetch in flight

    def prefetch(self, keys):
        """Start reading cells keys in the background (a road tile's at its start: alongside its other fetches)."""
        from concurrent.futures import ThreadPoolExecutor
        with self.lock:
            if self.pool is None:
                self.pool = ThreadPoolExecutor(1, thread_name_prefix="fra")
            for k in keys:
                if str(k) not in self.pending:
                    self.pending[str(k)] = self.pool.submit(self.cell, k)

    def cell(self, key):
        """[(lat, lon, kit)] of map cell key's at-grade crossings, or None (not the US, or unreachable)."""
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        if not in_us(0.5 * (lat0 + lat1), 0.5 * (lon0 + lon1)):
            return None
        sh = self.data.of_cell(key)
        if sh is None:
            return None
        pad = 0.001
        return [(la, lo, kit) for la, lo, kit in sh.get("crossings", ())
                if lat0 - pad <= la <= lat1 + pad and lon0 - pad <= lo <= lon1 + pad]

    def kits(self, frame, keys):
        """fn(xy (2,), road class) -> the nearest inventory crossing's kit within MATCH_M, or None, over cells keys."""
        pts = [r for k in keys for r in (self.cell(k) or ())]
        if not pts:
            return None
        a = np.array([[r[0], r[1]] for r in pts], np.float64)
        e, n, _ = frame.geodetic_to_enu(a[:, 0], a[:, 1], np.zeros(len(a)))
        xy = np.stack([np.asarray(e), np.asarray(n)], -1)
        kit = [r[2] for r in pts]

        def look(p, _cls):
            d = np.hypot(*(xy - np.asarray(p)[:2]).T)
            j = int(np.argmin(d))
            return kit[j] if d[j] <= MATCH_M else None
        return look
