"""Cached polygon vetoes for GroundCover, independent of its environmental palette.

Only samples already eligible for grass can be blocked. This supplements the existing surface,
slope, roads, sidewalks, parking, pads and water-depth rules; it never makes a surface vegetated.
The map-data cache is read directly on a memory miss, so absent or damaged files cannot start a
download or delay a physics block waiting for vector data.
"""
from collections import OrderedDict
import logging
import os
import threading

import numpy as np
from PIL import Image, ImageDraw

import mapcells
import mapgeom
import surface

log = logging.getLogger("earthd.grass")

LAYERS = ("buildings", "water", "land_use", "infrastructure")
# These are explicit class / subtype / surface values, not broad land-use categories such as
# industrial or residential (both can contain grass). OSM enrichment currently covers roads only;
# use the land_use and infrastructure attributes already supplied by Overture, without a new API.
UNVEGETATED = frozenset((
    "bare_rock", "bare_ground", "bare_earth", "quarry", "scree", "shingle", "sand", "beach",
    "glacier", "snow", "ice", "water", "reservoir", "swimming_pool", "salt_flat", "salt_flats",
    "salt_pan", "dry_lake", "mudflat", "mudflats", "pedestrian", "plaza", "substation",
))
HARD_SURFACES = frozenset((
    "asphalt", "concrete", "concrete:plates", "concrete:lanes", "paved", "paving_stones",
    "sett", "cobblestone", "cobblestone:flattened", "metal", "wood", "rock", "stone",
    "bare_rock", "gravel", "fine_gravel", "compacted", "sand", "dirt", "earth", "bare_ground",
))
VEGETATED_SURFACES = frozenset(("grass", "grass_paver", "grass_pavers"))
MEM_BYTES = 8 * 1024 * 1024       # only fallback disk decodes; reuse MapData's normal memory cache first
_memo = OrderedDict()
_memo_bytes = 0
_lock = threading.Lock()


def _values(feats, name):
    a = feats.attrs.get(name)
    if a is None:
        return np.full(len(feats), "", dtype=object)
    return np.array([str(v or "").strip().lower().replace(" ", "_") for v in a], dtype=object)


def _selected(feats, layer):
    if layer in ("buildings", "water"):
        return feats.kind == 3
    cls, sub, surf = (_values(feats, k) for k in ("class", "subtype", "surface"))
    sel = np.isin(cls, tuple(UNVEGETATED)) | np.isin(sub, tuple(UNVEGETATED))
    sel |= np.isin(surf, tuple(HARD_SURFACES))
    # Preserve mapped grassy parking areas; ordinary paved or gravel parking still stays clear.
    parking = (cls == "parking") | (sub == "parking")
    sel |= parking & ~np.isin(surf, tuple(VEGETATED_SURFACES))
    return sel & (feats.kind == 3)


def _cached_layer(md, key, layer):
    """Features from the existing cache, with no call to MapData.cell (which can fetch on corruption)."""
    mem_lock = getattr(md, "_mem_lock", None)
    if mem_lock is not None:
        with mem_lock:
            got = md._mem.get((str(key), layer))
            if got is not None:
                return got[0]
    if not hasattr(md, "path"):
        return None
    path = md.path(key, layer)
    try:
        stat = os.stat(path)
        sig = (path, stat.st_mtime_ns, stat.st_size)
        with _lock:
            got = _memo.get(sig)
            if got is not None:
                _memo.move_to_end(sig)
                return got[0]
        # Read geometry and the three attributes needed here, without another local ENU copy,
        # provenance, road rules or unrelated attributes from a potentially large city layer.
        want = {"ids", "kind", "part_off", "ring_off", "coord_off", "lonlat",
                "attr.class", "attr.subtype", "attr.surface"}
        with np.load(path, allow_pickle=False) as z:
            arrays = {n: z[n] for n in z.files if n in want}
        if not {"ids", "kind", "part_off", "ring_off", "coord_off", "lonlat"} <= arrays.keys():
            return None
        feats = mapgeom.Features.from_arrays(layer, arrays, "")
        size = sum(a.nbytes for a in arrays.values()) + 1024
        if size <= MEM_BYTES:
            global _memo_bytes
            with _lock:
                old = _memo.pop(sig, None)
                if old is not None:
                    _memo_bytes -= old[1]
                _memo[sig] = (feats, size)
                _memo_bytes += size
                while _memo_bytes > MEM_BYTES:
                    _, (_, n) = _memo.popitem(last=False)
                    _memo_bytes -= n
        return feats
    except FileNotFoundError:
        return None
    except Exception as e:              # a damaged npz is absent, never rebuilt or fetched here
        log.debug("grass exclusions: cached %s %s: %s", key, layer, e)
        return None


def blocked(md, lat, lon, eligible):
    """Boolean veto grid for cached buildings, water and explicitly unvegetated polygons.

    lat/lon are the original (unwarped) surface sample grids, either row orientation. eligible is
    true only for the caller's GRASS/DUSTY/LAWN samples after its existing surface exclusions.
    Missing cache data leaves the existing decision unchanged. Polygon interior rings stay clear
    of the veto, and overlapping polygons are combined by union rather than erasing one another.
    """
    lat, lon = np.asarray(lat), np.asarray(lon)
    eligible = np.asarray(eligible, bool)
    if lat.shape != lon.shape or lat.shape != eligible.shape or lat.ndim != 2:
        raise ValueError("grass exclusion grids must have the same two-dimensional shape")
    out = np.zeros(lat.shape, bool)
    if md is None or not eligible.any():
        return out
    keys = mapcells.cells_for_points(lat[eligible], lon[eligible])
    to_px = surface.grid_affine(lat, lon)
    h, w = lat.shape
    lon0, lon1 = float(lon.min()), float(lon.max())
    lat0, lat1 = float(lat.min()), float(lat.max())
    for key in keys:
        for layer in LAYERS:
            feats = _cached_layer(md, key, layer)
            if feats is None or not len(feats):
                continue
            sel = _selected(feats, layer)
            if not sel.any():
                continue
            boxes = surface._lonlat_boxes(feats)
            sel &= (boxes[:, 0] <= lon1) & (boxes[:, 1] >= lon0) \
                & (boxes[:, 2] <= lat1) & (boxes[:, 3] >= lat0)
            for i in np.flatnonzero(sel):
                for part in feats.parts(i):
                    if not part or len(part[0]) < 3:
                        continue
                    rings = []
                    for ring in part:
                        ring = np.asarray(ring, np.float64)
                        c, r = to_px(ring[:, 0], ring[:, 1])
                        rings.append((c, r))
                    c, r = rings[0]
                    c0, c1 = max(0, int(np.floor(c.min()))), min(w, int(np.ceil(c.max())) + 1)
                    r0, r1 = max(0, int(np.floor(r.min()))), min(h, int(np.ceil(r.max())) + 1)
                    if c1 <= c0 or r1 <= r0 or not eligible[r0:r1, c0:c1].any():
                        continue
                    image = Image.new("L", (c1 - c0, r1 - r0), 0)
                    draw = ImageDraw.Draw(image)
                    for j, (c, r) in enumerate(rings):
                        if len(c) >= 3:
                            draw.polygon(list(zip((c - c0).tolist(), (r - r0).tolist())), fill=255 if j == 0 else 0)
                    out[r0:r1, c0:c1] |= np.asarray(image) > 0
    return out & eligible
