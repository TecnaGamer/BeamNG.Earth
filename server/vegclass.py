"""fork: the vegetation palette at a point: which trees, shrubs and ground cover the land carries, from the finest
vegetation map that covers it (vegetation.apply_palette, surface.au_cover, season.grass_dry_months).

LAYERS is a list of maps, finest first; a point takes the palette of the first layer that has a class for it, and none
(palette 0, "") when no layer does: callers then keep their own behaviour. Today's only layer is Australia's: NVIS
Version 7.0 Major Vegetation Groups (c) Commonwealth of Australia (DCCEEW), CC BY 4.0, resampled to a 0.01 degree
grid (data/au_mvg_v7.npz, tools/build_au_vegetation.py). Another region or a world layer (ecoregions) is one more
object with a `name`, `covers(lat, lon)` (whether it can hold any of the points) and `codes(lat, lon)` (a class code
0-255 a point, 0 where it has none), and its "<name>:<code>" keys in data/veg_palettes.json's "classes". The palettes are data (data/veg_palettes.json): a tree and a shrub
mix (group, share) over vegetation.GROUPS, a ground cover set (GROUND), a density multiplier (thinning, at most 1), a
height multiplier range and the wet months of the grass. Pure lookups: the same answer for the same point.
"""
import hashlib
import json
import os
import threading

import numpy as np

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PALETTES_FILE = os.path.join(DATA, "veg_palettes.json")
GROUND = ("", "dry_long", "dry_bush", "green", "none")     # ground cover sets (surface.au_cover; "" leaves the ground as is)

_lock = threading.Lock()


class GridLayer:
    """A lat / lon grid of class codes (uint8, 0 none), rows from the north: npz with grid, lat0, lon0, res."""

    def __init__(self, name, path):
        self.name, self.path = name, path
        self._g = None
        self.box = None                 # (lat south, lat north, lon west, lon east), known once loaded

    def _load(self):
        with _lock:
            if self._g is None:
                with np.load(self.path) as z:
                    g, self.lat0, self.lon0, self.res = z["grid"], float(z["lat0"]), float(z["lon0"]), float(z["res"])
                self.box = (self.lat0 - g.shape[0] * self.res, self.lat0, self.lon0, self.lon0 + g.shape[1] * self.res)
                self._g = g
        return self._g

    def codes(self, lat, lon):
        """Class code per point (0 outside the grid and on the sea)."""
        g = self._load()
        j = np.floor((self.lat0 - lat) / self.res).astype(np.int64)
        i = np.floor((lon - self.lon0) / self.res).astype(np.int64)
        ok = (j >= 0) & (j < g.shape[0]) & (i >= 0) & (i < g.shape[1])
        out = np.zeros(lat.shape, np.uint8)
        out[ok] = g[j[ok], i[ok]]
        return out

    def covers(self, lat, lon):
        """Whether any point of the arrays can lie in the layer (its box; the grid read on first use)."""
        self._load()
        s, n, w, e = self.box
        return bool(((lat >= s) & (lat < n) & (lon >= w) & (lon < e)).any())


LAYERS = [GridLayer("au_mvg", os.path.join(DATA, "au_mvg_v7.npz"))]

_tables = None


def _load_palettes():
    global _tables
    with _lock:
        if _tables is None:
            with open(PALETTES_FILE, encoding="utf-8") as f:
                d = json.load(f)
            names = [""] + sorted(d["palettes"])
            by_name = {n: d["palettes"][n] for n in names[1:]}
            ids = {n: i for i, n in enumerate(names)}
            bad = sorted({p for p in d["classes"].values() if p not in by_name})
            if bad:
                raise ValueError("veg_palettes.json: classes name unknown palettes %s" % bad)
            ground = np.array([GROUND.index(by_name[n].get("ground", "")) if n else 0 for n in names], np.uint8)
            looks = np.array([[*by_name[n].get("scale", (1.0, 1.0)), min(by_name[n].get("density", 1.0), 1.0)] if n
                              else (1.0, 1.0, 1.0) for n in names], np.float64)
            _tables = (d, names, ids, by_name, ground, looks)
    return _tables


def palette_names():
    """The palettes' names; the index of a name is its palette id (0, "" is none)."""
    return list(_load_palettes()[1])


def palette(name):
    """The palette dict of a name (see the module doc), {} for "" / unknown."""
    return _load_palettes()[3].get(name, {})


def palette_ids(lat, lon):
    """The palette id (0 none) at each point of lat / lon (scalars or arrays, broadcast together), the finest layer first."""
    lat, lon = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
    shape = lat.shape
    la, lo = lat.reshape(-1), lon.reshape(-1)
    d, _, ids = _load_palettes()[:3]
    out = np.zeros(la.shape, np.int64)
    todo = np.ones(la.shape, bool)
    for layer in LAYERS:
        if not todo.any() or not layer.covers(la[todo], lo[todo]):
            continue
        k = np.flatnonzero(todo)
        codes = layer.codes(la[k], lo[k])
        lut = np.array([ids.get(d["classes"].get("%s:%d" % (layer.name, c), ""), 0) for c in range(256)], np.int64)
        out[k] = lut[codes]
        todo[k[codes > 0]] = False      # (a class with no palette, cleared land, still ends the search)
    return out.reshape(shape)


def at(lat, lon):
    """The palette name at each point ("" where no layer has a class: callers keep their behaviour); a str for scalars."""
    names = np.array(_load_palettes()[1], object)
    out = names[palette_ids(lat, lon)]
    return out if isinstance(out, np.ndarray) else str(out)


def looks():
    """(palettes, 3) float64 by palette id: the height multiplier's low and high end, the density (at most 1)."""
    return _load_palettes()[5]


def ground_codes(lat, lon):
    """The ground cover set at each point as an index of GROUND (0 none or unchanged)."""
    return _load_palettes()[4][palette_ids(lat, lon)]


def wet_months(lat, lon):
    """The months (1-12) the grass is green at a place: its palette's "wet", else the file's defaults (the summer-wet
    north of default "north_lat", the winter-wet south of it); [] where no palette applies."""
    d = _load_palettes()[0]
    name = at(float(lat), float(lon))
    if not name:
        return []
    own = palette(name).get("wet")
    if own is not None:
        return list(own)
    return list(d["wet_north"] if lat > d["north_lat"] else d["wet_south"])


def digest():
    """A short hash of the palettes file and every layer's file: the vegetation cache's key (vegetation.Vegetation)."""
    h = hashlib.sha1()
    for p in [PALETTES_FILE] + [l.path for l in LAYERS]:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]
