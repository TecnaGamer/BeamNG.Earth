"""Paved or not from the regional orthophoto's colour, for roads whose map data says nothing about their surface (the user,
2026-11-05: Suchy Dul, Czechia, an asphalt lane mapped as a bare highway=track drawn as a dirt track).

A road of PHOTO_CLASSES with no surface value (OpenStreetMap's, Overture's) is read along its centre line (and, for
contrast, its verges) in the z17 tiles of a regional tile-cache source (imagery_regional.REGIONAL kind "xyz": ~0.75 m a
pixel in Czechia) that are on the disk already (never fetched here; a tile not there yet is "not known yet", and the
caller says so with revisit.fault so nothing built from it is kept). Grey, unsaturated, not green, neither dark nor
glaring: asphalt. A verdict is "paved", "grass" (a lane as green as its verges: the ground's grass layer, no dirt strip), "gravel" (pale and
neutral) or None (the road stays as it was: dirt); Sentinel-2's 10 m pixels, a
source that is not a tile cache and a road in no regional box give None without a fault.

The colour is measured (statistics of the samples, stats()) and classified (classify()) apart, so the rule is tuned
on roads whose tags are known (tools/photo_surface_eval.py). BNE_PHOTO_SURFACE=1 turns it on.
"""
import math
import os
import threading
from collections import OrderedDict

import numpy as np
import dmath
import dnp

import imagery_regional

ON = os.environ.get("BNE_PHOTO_SURFACE", "").lower() in ("1", "on", "true")
TAG = "ps2"                     # in the road specs' cache key while on (roads.road_specs): bump with a rule change
ZOOM = 17
PHOTO_CLASSES = {"track", "service", "unclassified"}     # (a residential street is built up: the land cover's call)
SAMPLE_M = 1.0                  # m between samples along the road
CENTRE_OFFSETS = (-0.8, 0.0, 0.8)
VERGE_OFFSETS = (-4.5, 4.5)
MIN_SAMPLES = 20                # valid centre samples needed (a road of ~7 m at three across)
MIN_VALID_SHARE = 0.6           # of the centre samples (the rest: off the photo, in shadow or under trees)
DARK_L = 70.0                   # a sample darker than this is shade / canopy, not the road
# paved: median saturation, |excess green| and brightness of the centre samples. Tuned 2026-11-05 on 269 tracks, service
# and unclassified roads of the ČÚZK photo around Suchy Dul whose surface IS mapped (tools/photo_surface_eval.py): the
# paved ones' saturation is 0.04-0.16 (old, patched, shaded asphalt is warm), gravel 0.08-0.21, dirt 0.14-0.25, so a
# loose limit calls gravel paved. At 0.05: 22 of 103 mapped asphalt roads, no gravel or dirt road (5 cobble roads: grey
# too, drawn as asphalt then); with the verge rule below 28 of 103. Precision first: what it cannot tell stays as it was.
PAVED_MAX_SAT = 0.05
PAVED_MAX_EXG = 0.03
PAVED_L = (105.0, 205.0)
# or a little warmer (to 0.06) when it stands out from green verges: the lane's excess green (centre) is at least this
# much below the verges' (the 4.5 m strips either side). +6 of the 103, still no gravel or dirt road.
VERGE_MAX_SAT = 0.06
VERGE_MAX_EXG = 0.015
VERGE_CONTRAST = 0.03
# grass: a lane as green as the verges (task 010, "draw the grass lanes", the user 2026-11-05): excess green of the
# centre samples at least this, within GRASS_MATCH of the verges' (the road is the field's own colour), not a forest
# track's deep saturation. Of 271 mapped roads, 27 pass: 22 mapped dirt (a grown-over track), 1 gravel, 4 mapped
# grass; 2 paved and 2 cobble roads too (green-tinted: the paved rule is asked first). 0.08 missed the lane at the
# junction in Suchy Dul that started this (excess green 0.066).
GRASS_MIN_EXG = 0.06
GRASS_MATCH = 0.035
GRASS_MAX_SAT = 0.25
# gravel (task 012): what is left pale and neutral: not green, not brown, light, a little warm. Of 271 mapped roads 50 are
# (L >= 140, saturation 0.05-0.12, |excess green| <= 0.03) beside the paved and grass rules: 30 paved, 17 gravel, 2
# cobble, 1 dirt. Colour cannot tell worn asphalt from gravel, but it tells either from a brown dirt track with ruts,
# which is what these roads were drawn as; gravel (a light strip, the loose layer) is the near miss for both.
GRAVEL_MIN_L = 140.0
GRAVEL_SAT = (0.05, 0.12)
GRAVEL_MAX_EXG = 0.03
MEM_TILES = 96

_LOCK = threading.Lock()
_TILES = OrderedDict()          # (source, x, y) -> decoded tile (256, 256, 3) uint8, or None (a 404 on disk)


def cache_root():
    """The imagery cache folder (beside the map data's: the server's cache directory / imagery)."""
    import mapdata
    return os.path.join(os.environ.get("BNGE_CACHE_DIR") or mapdata.DEFAULT_CACHE, "imagery")


def sources_at(lat, lon):
    """The tile-cache regional sources whose box holds the point: [(name, url template)]."""
    out = []
    for name, (url, _attr, b, kind, _leaf) in imagery_regional.REGIONAL.items():
        if kind == "xyz" and b[0] <= lat <= b[2] and b[1] <= lon <= b[3]:
            out.append(name)
    return out


def tile_of(lat, lon, z=ZOOM):
    """Fractional web mercator tile coordinates (x, y) of lat / lon arrays at zoom z."""
    n = 2.0 ** z
    lat = np.asarray(lat, np.float64)
    x = (np.asarray(lon, np.float64) + 180.0) / 360.0 * n
    y = (1.0 - dnp.arcsinh(dnp.tan(np.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _read_tile(root, name, x, y):
    """(tile array or None, known): known False when the file is not on disk (nor a recorded 404)."""
    key = (root, name, x, y)
    with _LOCK:
        if key in _TILES:
            _TILES.move_to_end(key)
            return _TILES[key], True
    d = os.path.join(root, name, str(ZOOM), str(x))
    path = os.path.join(d, f"{y}.jpg")
    arr = None
    if os.path.exists(path):
        try:
            from PIL import Image
            with Image.open(path) as im:
                arr = np.asarray(im.convert("RGB"), np.uint8)
            if arr.shape[:2] != (256, 256):
                arr = None
        except (OSError, ValueError):
            return None, False          # (half written: not known yet)
    elif not os.path.exists(os.path.join(d, f"{y}.404")):
        return None, False
    with _LOCK:
        _TILES[key] = arr
        while len(_TILES) > MEM_TILES:
            _TILES.popitem(last=False)
    return arr, True


def sample_rgb(root, name, lat, lon):
    """(rgb (k, 3) float32, valid (k,) bool, missing bool) of lat / lon arrays in source name's z17 tiles on disk:
    missing when some point's tile is neither on disk nor a recorded 404 (valid False there)."""
    fx, fy = tile_of(lat, lon)
    tx, ty = np.floor(fx).astype(np.int64), np.floor(fy).astype(np.int64)
    px = np.clip(((fx - tx) * 256).astype(np.int64), 0, 255)
    py = np.clip(((fy - ty) * 256).astype(np.int64), 0, 255)
    rgb = np.zeros((len(fx), 3), np.float32)
    valid = np.zeros(len(fx), bool)
    missing = False
    keys = tx * 4_000_000 + ty
    for k in np.unique(keys):
        sel = keys == k
        i0 = int(np.flatnonzero(sel)[0])
        arr, known = _read_tile(root, name, int(tx[i0]), int(ty[i0]))
        if not known:
            missing = True
        elif arr is not None:
            rgb[sel] = arr[py[sel], px[sel]]
            valid[sel] = True
    return rgb, valid, missing


def _along(ring, step=SAMPLE_M):
    """Points every `step` m along a lon/lat polyline (k, 2) with each one's unit normal (east, north)."""
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(ring) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2))
    lat0 = float(ring[:, 1].mean())
    sc = np.array([111320.0 * dmath.cos(math.radians(lat0)), 110540.0])
    xy = (ring - ring[0]) * sc
    seg = np.diff(xy, axis=0)
    ln = dnp.hypot(seg[:, 0], seg[:, 1])
    keep = ln > 1e-6
    if not keep.any():
        return np.zeros((0, 2)), np.zeros((0, 2))
    seg, ln, a = seg[keep], ln[keep], xy[:-1][keep]
    cum = np.concatenate([[0.0], np.cumsum(ln)])
    s = np.arange(step / 2, cum[-1], step)
    i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(ln) - 1)
    t = (s - cum[i]) / ln[i]
    p = a[i] + seg[i] * t[:, None]
    nrm = np.stack([-seg[i, 1], seg[i, 0]], 1) / ln[i][:, None]
    ll = p / sc + ring[0]
    return ll, nrm / sc * 1.0       # (nrm in degrees per metre: ll + nrm * offset_m)


def stats(rgb, valid):
    """Median saturation, excess green (2g - r - b over r + g + b) and brightness of the valid samples (k, 3)."""
    v = rgb[valid]
    if not len(v):
        return None
    mx, mn = v.max(1), v.min(1)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    exg = (2 * v[:, 1] - v[:, 0] - v[:, 2]) / np.maximum(v.sum(1), 1.0)
    lum = 0.299 * v[:, 0] + 0.587 * v[:, 1] + 0.114 * v[:, 2]
    mad = float(np.median(np.abs(lum - np.median(lum))))
    return {"sat": float(np.median(sat)), "exg": float(np.median(exg)), "lum": float(np.median(lum)), "n": int(len(v)),
            "mad": mad}


def classify(st):
    """"paved" for a road whose centre samples are grey asphalt, "grass" for one as green as its verges, "gravel" for a
    pale neutral one that is neither, else None (st: stats())."""
    if st is None or st["n"] < MIN_SAMPLES:
        return None
    v = st.get("vexg")
    if PAVED_L[0] <= st["lum"] <= PAVED_L[1] and _paved(st, v):
        return "paved"
    if v is not None and st["exg"] >= GRASS_MIN_EXG and st["sat"] <= GRASS_MAX_SAT             and abs(v - st["exg"]) <= GRASS_MATCH:
        return "grass"
    if st["lum"] >= GRAVEL_MIN_L and GRAVEL_SAT[0] <= st["sat"] <= GRAVEL_SAT[1] and abs(st["exg"]) <= GRAVEL_MAX_EXG:
        return "gravel"
    return None


def _paved(st, v):
    if st["sat"] <= PAVED_MAX_SAT and abs(st["exg"]) <= PAVED_MAX_EXG:
        return True
    return v is not None and st["sat"] <= VERGE_MAX_SAT and abs(st["exg"]) <= VERGE_MAX_EXG         and v - st["exg"] >= VERGE_CONTRAST


def measure(ring, root=None):
    """(stats of the road's centre samples or None, missing) for a lon/lat polyline: the first regional source of its
    first point whose tiles are on disk. missing: a tile that covers it is not on disk yet; (None, False) for a road in
    no regional tile-cache box."""
    ll, nrm = _along(ring)
    if not len(ll):
        return None, False
    root = root or cache_root()
    names = sources_at(float(ll[len(ll) // 2, 1]), float(ll[len(ll) // 2, 0]))
    if not names:
        return None, False
    missing_any = False
    for name in names:
        pts = np.concatenate([ll + nrm * o for o in CENTRE_OFFSETS])
        rgb, valid, missing = sample_rgb(root, name, pts[:, 1], pts[:, 0])
        if missing:
            missing_any = True
            continue
        lum = 0.299 * rgb[:, 0] + 0.587 * rgb[:, 1] + 0.114 * rgb[:, 2]
        ok = valid & (lum >= DARK_L)
        if valid.sum() < MIN_VALID_SHARE * len(valid):
            continue
        st = stats(rgb, ok)
        if st is not None:
            # the verges (two strips either side): what the road is set against
            vp = np.concatenate([ll + nrm * o for o in VERGE_OFFSETS])
            vrgb, vvalid, _ = sample_rgb(root, name, vp[:, 1], vp[:, 0])
            vs = stats(vrgb, vvalid)
            st["vexg"] = vs["exg"] if vs else None
            st["vlum"] = vs["lum"] if vs else None
            st["vsat"] = vs["sat"] if vs else None
        return st, False
    return None, missing_any


def verdicts(feats, surface_of, root=None):
    """({feature index: "paved", "grass" or "gravel"}, missing): the candidates of feats (a roads layer: a class of
    PHOTO_CLASSES and surface_of(i) None) the photo says are paved, a grass lane or gravel. missing: a candidate's tile is not on disk yet (not known: the
    caller keeps nothing built from this)."""
    cls = feats.attrs.get("class")
    out, missing = {}, False
    if cls is None or not ON:
        return out, missing
    for i in range(len(feats)):
        if str(cls[i]) not in PHOTO_CLASSES or surface_of(i) is not None:
            continue
        best = None
        # (the raw geometry: roads.ground_parts needs the level ranges, which read the surface: a loop. A bridge's or
        # tunnel's own piece is a few samples of a road that carries on)
        for part in feats.parts(i):
            for ring in part:
                st, miss = measure(ring, root)
                missing |= miss
                if st is not None and (best is None or st["n"] > best["n"]):
                    best = st
        c = classify(best)
        if c is not None:
            out[i] = c
    return out, missing
