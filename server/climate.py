"""The Koppen-Geiger climate class at a point (the forest ambience's set, vegetation._ambience): Beck et al. (2023),
High-resolution (1 km) Koppen-Geiger maps for 1901-2099, Scientific Data 10, 724 (CC BY 4.0), its 1991-2020 map at
0.1 degrees (koppen_1991_2020_0p1.png beside the modules: 3600 x 1800 from 180 W, 90 N; 0 the sea, 1-30 as their
legend: 1-3 A tropical, 4-7 B arid, 8-16 C temperate, 17-28 D cold, 29-30 E polar)."""
import os
import threading

import numpy as np

import fast

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "koppen_1991_2020_0p1.png")
RES = 0.1
NEAR_PX = 3             # a point on the sea's pixels (a coast at 11 km): the most common class within this many

_grid = None
_lock = threading.Lock()


def _load():
    global _grid
    with _lock:
        if _grid is None:
            from PIL import Image
            with Image.open(PATH) as im:
                _grid = np.asarray(im, np.uint8)
        return _grid


def koppen_many(lat, lon):
    """koppen_at's class of each point (arrays), 0 on the sea's pixels (no coast search: for items on land)."""
    g = _load()
    lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
    j = np.clip(((90.0 - lat) / RES).astype(np.int64), 0, g.shape[0] - 1)
    i = (((lon + 180.0) % 360.0) / RES).astype(np.int64) % g.shape[1]
    return g[j, i].astype(np.int64)


def koppen_at(lat, lon):
    """The class (1-30) at (lat, lon), the most common one within NEAR_PX pixels on the sea's (a coast), else 0."""
    g = _load()
    j = min(max(int((90.0 - lat) / RES), 0), g.shape[0] - 1)
    i = int(((lon + 180.0) % 360.0) / RES) % g.shape[1]
    k = int(g[j, i])
    if k:
        return k
    win = g[max(j - NEAR_PX, 0):j + NEAR_PX + 1, np.arange(i - NEAR_PX, i + NEAR_PX + 1) % g.shape[1]]
    vals = win[win > 0]
    return int(np.bincount(vals).argmax()) if vals.size else 0


# Australia (au_state(): the state outlines; Timor, New Guinea and the NZ islands lie outside them)
AU_LAT = (-44.0, -9.0)         # (au_state's prefilter box: Torres Strait to Tasmania, Lord Howe to Shark Bay)
AU_LON = (112.0, 160.0)
# its classes with green lawns and grass all year: rainforest (Af, Am), cool oceanic (Cfb, Cfc)
AU_GREEN = (1, 2, 15, 16)


# the state outlines (data/au_states.json.gz from tools/build_au_states.py: Natural Earth admin_1, public domain)
AU_STATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "au_states.json.gz")
AU_OFFSHORE_KM = 5.0        # a point this near a state's coast (the sea, a river mouth the outline lacks) is its
AU_GRID = 1000              # points snap to 1/1000 degree (about 110 m) for the cache
_au = None                  # ([state code], earthfast.AuStates of their outlines)
_au_lock = threading.Lock()


def _au_load():
    global _au
    with _au_lock:
        if _au is None:
            import gzip
            import json
            with gzip.open(AU_STATES_FILE, "rt", encoding="utf-8") as f:
                states = json.load(f)["states"]
            codes, segs, boxes = [], [], []
            for code, polys in states.items():
                seg = []
                for poly in polys:
                    for flat in poly:
                        d = np.asarray(flat, np.float64)
                        p = np.cumsum(np.vstack([d[:2], d[2:].reshape(-1, 2)]), axis=0) / AU_GRID    # lon, lat
                        seg.append(np.hstack([p, np.roll(p, -1, axis=0)]))      # a closed ring: x1, y1, x2, y2
                e = np.vstack(seg)
                boxes.append((min(e[:, 0].min(), e[:, 2].min()), min(e[:, 1].min(), e[:, 3].min()),
                              max(e[:, 0].max(), e[:, 2].max()), max(e[:, 1].max(), e[:, 3].max())))
                codes.append(code)
                segs.append(np.ascontiguousarray(e.T))
            fast.require(405, "AuStates")
            _au = (codes, fast._ef.AuStates(segs, np.array(boxes, np.float64)))
    return _au


def _au_index(lat, lon):
    """the index into _au_load()'s codes of the state at each point of two arrays (-1 none, as their shape): outside
    the AU_LAT x AU_LON box none; else the one whose outline holds the point's 1/AU_GRID degree cell (even-odd over
    every ring, so holes count), else the nearest within AU_OFFSHORE_KM (Rust: earthfast.AuStates.index)."""
    codes, states = _au_load()
    la, lo = np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
    idx = states.index(np.ascontiguousarray(la.ravel()), np.ascontiguousarray(lo.ravel()), float(AU_GRID), AU_LAT,
                       AU_LON, AU_OFFSHORE_KM / 111.0 * 1.3, AU_OFFSHORE_KM)
    return idx.reshape(la.shape), codes


def au_state(lat, lon):
    """the Australian state or territory of (lat, lon): "QLD", "NSW", "VIC", "SA", "WA", "TAS", "NT", "ACT",
    "JBT" (Jervis Bay) and so on; None outside Australia (scalars give a string or None, arrays an object array).
    Natural Earth's outlines (simplified to about 200 m), a point within AU_OFFSHORE_KM of a coast the nearest
    state's; a lat / lon box first (non-Australian places cost one comparison), on points snapped to 0.001 degree."""
    if np.ndim(lat) == 0 and np.ndim(lon) == 0:
        if lat is None or lon is None or not (AU_LAT[0] <= lat <= AU_LAT[1] and AU_LON[0] <= lon <= AU_LON[1]):
            return None
        i, codes = _au_index(np.array([float(lat)]), np.array([float(lon)]))
        return codes[i[0]] if i[0] >= 0 else None
    idx, codes = _au_index(lat, lon)
    if not (idx >= 0).any():
        return np.full(idx.shape, None, object)     # (the whole block outside: no per-point object lookup)
    return np.array([None] + codes, object)[idx + 1]


def in_australia(lat, lon):
    """True where (lat, lon) (scalars or arrays) lies in an Australian state or territory (au_state is not None)."""
    if np.ndim(lat) == 0 and np.ndim(lon) == 0:
        return np.bool_(au_state(lat, lon) is not None)
    return _au_index(lat, lon)[0] >= 0


def koppen_grid(lat, lon):
    """the class (1-30; 0 the sea) at every (lat, lon) of two arrays, without koppen_at's coast search."""
    g = _load()
    j = np.clip(((90.0 - np.asarray(lat, np.float64)) / RES).astype(np.int64), 0, g.shape[0] - 1)
    i = (((np.asarray(lon, np.float64) + 180.0) % 360.0) / RES).astype(np.int64) % g.shape[1]
    return g[j, i]


def dry_australia(lat, lon):
    """True in Australia except its rainforest and cool oceanic climates (AU_GREEN): the country's dry,
    summer-brown grass and thin lawns (the Cfa of Brisbane, the Aw of Townsville and the arid interior included)."""
    box = in_australia(lat, lon)
    out = np.zeros(box.shape, bool)
    if box.any():
        out[box] = ~np.isin(koppen_grid(np.asarray(lat)[box], np.asarray(lon)[box]), AU_GREEN)
    return out
