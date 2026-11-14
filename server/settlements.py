"""A cached, offline gazetteer for adaptive traffic settlement influence.

The source is the project's bundled Natural Earth 1:10m populated-place layer
(`maplayers.js`). Its scalerank is a cartographic prominence proxy, not a
population estimate. No network request or API credential is needed.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys

import numpy as np

log = logging.getLogger("earthd.settlements")
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACES_JS = os.path.join(ROOT, "mod", "beamng_earth", "ui", "ui-vue", "mods", "beamngEarth", "maplayers.js")
EARTH_R_M = 6371008.8

_INDEX = None
_ATTEMPTED = False
_WARNED = False


class SettlementIndex:
    """Vectorised nearest populated-place lookup over a small, immutable catalogue."""

    def __init__(self, places):
        # places: (name, longitude, latitude, scalerank, capital)
        clean = []
        for item in places:
            try:
                name, lon, lat, rank = item[:4]
                capital = bool(item[4]) if len(item) > 4 else False
                lon, lat, rank = float(lon), float(lat), int(rank)
                if not name or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    continue
                clean.append((" ".join(str(name).split()), lon, lat, max(0, min(10, rank)), capital))
            except (TypeError, ValueError, IndexError):
                continue
        self.names = tuple(x[0] for x in clean)
        self.lon = np.radians(np.asarray([x[1] for x in clean], dtype=np.float64))
        self.lat = np.radians(np.asarray([x[2] for x in clean], dtype=np.float64))
        self.rank = np.asarray([x[3] for x in clean], dtype=np.int8)
        self.capital = np.asarray([x[4] for x in clean], dtype=bool)

    def __len__(self):
        return len(self.names)

    def nearest(self, lat, lon):
        """Return the nearest place with haversine distance and cartographic size rank."""
        if not len(self.names):
            return None
        try:
            la, lo = math.radians(float(lat)), math.radians(float(lon))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(la) or not math.isfinite(lo):
            return None
        dlat = self.lat - la
        dlon = self.lon - lo
        h = np.sin(dlat / 2.0) ** 2 + math.cos(la) * np.cos(self.lat) * np.sin(dlon / 2.0) ** 2
        k = int(np.argmin(h))
        distance = EARTH_R_M * 2.0 * math.asin(math.sqrt(float(np.clip(h[k], 0.0, 1.0))))
        rank = int(self.rank[k])
        if rank <= 1:
            size = "metropolitan"
        elif rank <= 3:
            size = "large-city"
        elif rank <= 5:
            size = "regional-city"
        elif rank == 6:
            size = "regional-town"
        elif rank == 7:
            size = "small-town"
        else:
            size = "small-settlement"
        return {"name": self.names[k], "distanceM": distance, "rank": rank,
                "capital": bool(self.capital[k]), "sizeClass": size,
                # Keep the reference point in geographic coordinates. The game can then recompute
                # settlement distance from its live player pose without waiting for a full graph rebuild.
                "lat": math.degrees(float(self.lat[k])), "lon": math.degrees(float(self.lon[k]))}


def load_index(path=PLACES_JS):
    """Load the gazetteer once. Returns None, with a single warning, when the bundled file is absent."""
    global _INDEX, _ATTEMPTED, _WARNED
    if _INDEX is not None or _ATTEMPTED:
        return _INDEX
    _ATTEMPTED = True
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        prefix = "export default "
        data = json.loads(source[source.index(prefix) + len(prefix):].rstrip().rstrip(";"))
        _INDEX = SettlementIndex(data.get("places", []))
        if not len(_INDEX):
            raise ValueError("the populated-place layer is empty")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if not _WARNED:
            log.warning("adaptive traffic: settlement gazetteer unavailable (%s: %s); using road-network fallback",
                        type(exc).__name__, exc)
            _WARNED = True
        return None
    log.info("adaptive traffic: loaded %d offline populated places", len(_INDEX))
    return _INDEX


def nearest(lat, lon):
    """Convenience lookup; the parsed catalogue is cached for the process lifetime."""
    index = load_index()
    return index.nearest(lat, lon) if index is not None else None
