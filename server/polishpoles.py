"""Polish PRG boundary and active BDOT10k power supports, bundled for offline tiles."""
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import mapgeom
import mapillarypoles
from polygon import contains_points


SNAPSHOT = Path(__file__).parent / "data/polish_poles.npz"


@lru_cache(maxsize=1)
def _signature():
    return "pl-poles-v1-" + hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()[:16]


SOUTH, WEST, NORTH, EAST = 48.75, 14., 55.75, 24.25
LIVE_MARGIN = 2060.0  # powerlines.NEAR_M plus its 10 m matching margin


def cache_parts(frame, box, buildings=True):
    extent = bounds(frame, box, LIVE_MARGIN)
    if not extent:
        return ()
    if buildings and mapillarypoles.enabled():
        return (_signature(), *mapillarypoles.signature(extent))
    return (_signature(),)


@lru_cache(maxsize=1)
def _data():
    with np.load(SNAPSHOT) as data:
        return json.loads(str(data["cells"])), data["offsets"], data["border"] / 1e7, data["poles"], data["mapillary"]


def inside(lat, lon):
    if not (SOUTH <= lat < NORTH and WEST <= lon < EAST):
        return False
    cells, offsets, border = _data()[:3]
    cell = cells.get(f"{math.floor(lon*4)},{math.floor(lat*4)}")
    if isinstance(cell, list):
        hit = False
        for i in range(*cell):
            hit ^= bool(contains_points(border[offsets[i]:offsets[i+1]], [(lon, lat)])[0])
        return hit
    return bool(cell)


def bounds(frame, box, margin=0):
    x0, y0, x1, y1 = box[:4]
    lat, lon, _ = frame.enu_to_geodetic(
        np.array([x0-margin, x0-margin, x1+margin, x1+margin]),
        np.array([y0-margin, y1+margin, y0-margin, y1+margin]), np.zeros(4))
    if not (np.isfinite(lat).all() and np.isfinite(lon).all()):
        return None
    west, east = min(lon), max(lon)
    # A local tile straddling +/-180 has corners on both sides of the date line.
    # Its occupied arc is the short one across the Pacific, far from Poland.
    if east - west >= 180:
        return None
    south, west = max(min(lat), SOUTH), max(west, WEST)
    north, east = min(max(lat), NORTH), min(east, EAST)
    if south > north or west > east:
        return None
    cells = _data()[0]
    if any(f"{x},{y}" in cells for x in range(math.floor(4*west), math.floor(4*east)+1)
           for y in range(math.floor(4*south), math.floor(4*north)+1)):
        return south, west, north, east
    return None


def at_position(frame, x, y):
    lat, lon, _ = frame.enu_to_geodetic(x, y, 0.)
    return inside(lat, lon)


def survey_points(extent, points):
    south, west, north, east = extent
    a, b = np.searchsorted(points[:, 0], [math.floor(south*1e7), math.ceil(north*1e7)+1])
    band = points[a:b]
    return band[(band[:, 1] >= west*1e7) & (band[:, 1] <= east*1e7), ::-1] / 1e7


def _osm_supports(layer):
    """Keep the active OSM support coordinates on the decoded infrastructure cell."""
    cached = getattr(layer, "_pl_osm_supports", None)
    if cached is not None:
        return cached
    import mappedpoles

    classes = layer.attrs["class"]
    tags_column = layer.attrs.get("tags")
    candidates = (layer.kind == mapgeom.POINT) & np.isin(classes, ("power_pole", "utility_pole", "power_tower"))
    locations, towers = [], []
    for i in np.flatnonzero(candidates):
        cls = classes[i]
        tags = mappedpoles._tags(tags_column[i]) if tags_column is not None else {}
        if not mappedpoles._active(tags) or (cls == "utility_pole" and tags.get("utility") != "power"):
            continue
        lon, lat = layer.parts(i)[0][0][0]
        if inside(lat, lon):
            locations.append((lon, lat))
            towers.append(cls == "power_tower")
    cached = np.asarray(locations, dtype=np.float64).reshape(-1, 2), np.asarray(towers, dtype=bool)
    layer._pl_osm_supports = cached
    return cached


class Supports:
    """Tile-local evidence. OSM point geometry takes precedence in overlapping surveys.

    Projection, polygon containment and span layout already use earthfast. Keep
    source precedence and OSM feature identity here with the layer records.
    The grid checks only nine buckets per match and compares squared distances.
    """

    def __init__(self, frame, extent, layers):
        self.frame = frame
        self.survey = set()
        self.drawn = set()
        self.points = {}
        self.buckets = {}
        self.standalone = set()
        south, west, north, east = extent
        for layer in layers:
            locations, towers = _osm_supports(layer)
            lon, lat = locations.T
            selected = (south <= lat) & (lat <= north) & (west <= lon) & (lon <= east)
            if not selected.any():
                continue
            lon, lat = lon[selected], lat[selected]
            x, y, _ = frame.geodetic_to_enu(lat, lon, 0.)
            for lo, la, tower, px, py in zip(lon, lat, towers[selected], x, y):
                self.add(lo, la, bool(tower), bool(tower), xy=(px, py))
        sources = [(tower, survey_points(extent, survey)) for tower, survey in zip((True, False), _data()[3:])]
        if mapillarypoles.enabled():
            sources.append((False, mapillarypoles.points(extent)))
        for tower, locations in sources:
            # Merge across sources only. Two surveyed poles may stand side by side.
            additions = [(lon, lat) for lon, lat in locations
                         if inside(lat, lon) and self.nearest(lon, lat, 8.) is None]
            for lon, lat in additions:
                self.add(lon, lat, tower, True)
                self.survey.add((lon, lat))

    def add(self, lon, lat, tower, standalone, *, xy=None):
        if xy is None:
            x, y, _ = self.frame.geodetic_to_enu(lat, lon, 0.)
        else:
            x, y = xy
        key = (float(lon), float(lat))
        self.points[key] = (x, y, tower)
        self.buckets.setdefault((math.floor(x/10), math.floor(y/10)), []).append(key)
        if standalone:
            self.standalone.add(key)

    def nearest(self, lon, lat, radius):
        x, y, _ = self.frame.geodetic_to_enu(lat, lon, 0.)
        col, row = math.floor(x/10), math.floor(y/10)
        best = None
        for i in range(col-1, col+2):
            for j in range(row-1, row+2):
                for key in self.buckets.get((i, j), ()):
                    px, py, _ = self.points[key]
                    dist = (px-x)**2 + (py-y)**2
                    if dist <= radius**2 and (best is None or (dist, key) < best):
                        best = dist, key
        return best[1] if best else None


    def align(self, lonlat, east, north):
        """Snap supported Polish vertices to their point and flag other proposed tower positions."""
        excluded, confirmed = set(), {}
        for i, (lon, lat) in enumerate(lonlat):
            if not inside(lat, lon):
                # A clearance shift across the border must not introduce an unverified pole.
                if at_position(self.frame, east[i], north[i]):
                    excluded.add((east[i], north[i]))
                continue
            key = self.nearest(lon, lat, 8.)
            if key is not None:
                x, y, tower = self.points[key]
                px, py, _ = self.frame.geodetic_to_enu(lat, lon, 0.)
                # A low-voltage pole belongs to mappedpoles unless that stage defers
                # to a high-line vertex (its MATCH_M).
                if tower or key in self.survey or (px-x)**2 + (py-y)**2 <= 1.5**2:
                    east[i], north[i] = x, y
                    confirmed[x, y] = key
                    self.standalone.add(key)
                    continue
            excluded.add((east[i], north[i]))
        return excluded - confirmed.keys(), confirmed
