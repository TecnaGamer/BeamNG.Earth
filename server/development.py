"""Local built-environment context for procedural furniture on existing sidewalks.

No road policy or network access lives here. Cached polygons are rasterized once per tile,
including a halo: footprint union within a metric radius, same-side developed frontage,
commercial/residential land use, and existing pedestrian POIs. Missing evidence stays sparse.
The index uses Pillow/numpy already required by the server; optional places are never fetched.
"""
from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import math
import os
import threading

import numpy as np
from PIL import Image, ImageDraw

import mapgeom
from mapdata import BUILDING_MAX_M2

log = logging.getLogger("earthd.development")
RADIUS_M = max(25., min(250., float(os.environ.get("BNE_DECOR_RADIUS_M", "100"))))
PIXEL_M = 2.0
FRONTAGE_M = 120.0
LAYERS = ("buildings", "land_use", "places")
COMMERCIAL = frozenset(("commercial", "retail", "office", "offices", "shop", "supermarket",
                        "government", "civic", "public", "hotel"))
RESIDENTIAL = frozenset(("residential", "house", "detached", "semidetached_house", "terrace",
                         "apartments", "dormitory", "bungalow"))
# These still contribute to raw footprint coverage, but are not occupied street development.
OUTBUILDINGS = frozenset(("agricultural", "barn", "farm", "farm_auxiliary", "shed", "garage",
                          "garages", "greenhouse", "roof", "ruins", "construction", "abandoned"))
INDUSTRIAL = frozenset(("industrial", "warehouse", "manufacture", "depot"))
PEDESTRIAN = frozenset(("restaurant", "cafe", "coffee_shop", "bar", "pub", "bakery", "shop",
                        "retail", "office", "offices", "bank", "pharmacy", "library", "museum",
                        "theatre", "theater", "cinema", "post_office", "townhall", "community_centre",
                        "university", "college", "school", "convenience_store", "grocery_store",
                        "supermarket", "clothing_store", "bookstore", "hair_salon", "beauty_salon",
                        "store", "professional_service", "financial_service", "technical_service",
                        "corporate_or_business_office", "real_estate_service", "personal_or_beauty_service",
                        "attorney_or_law_firm", "casual_eatery", "bank_or_credit_union", "art_gallery",
                        "government_office", "educational_facility", "social_or_community_service",
                        "civic_organization", "music_venue", "public_plaza", "non_alcoholic_beverage_venue"))

_GEOMETRY = {"ids", "kind", "part_off", "ring_off", "coord_off", "lonlat"}
_ATTRS = {"class", "subtype", "tags", "is_underground", "basic_category", "taxonomy",
          "operating_status", "confidence"}
_memo = OrderedDict()
_memo_bytes = 0
_lock = threading.Lock()
MEM_BYTES = 24 * 1024 * 1024


def _text(feats, name, i):
    col = feats.attrs.get(name)
    return str(col[i] or "").strip().lower().replace(" ", "_") if col is not None else ""


def _labels(feats, i):
    labels = {_text(feats, "class", i), _text(feats, "subtype", i)}
    tags_col = feats.attrs.get("tags")
    tags = str(tags_col[i]) if tags_col is not None else ""
    if tags:
        try:
            pairs = json.loads(tags)
            if isinstance(pairs, dict):
                labels.update(str(pairs.get(k, "")).lower() for k in ("landuse", "building", "office", "shop"))
            elif isinstance(pairs, list):
                labels.update(str(p.get("value", "")).lower() for p in pairs if isinstance(p, dict)
                              and p.get("key") in ("landuse", "building", "office", "shop"))
        except (TypeError, ValueError):
            pass
    return labels


def _pedestrian(feats, i):
    status = _text(feats, "operating_status", i)
    if "closed" in status:
        return False
    confidence = feats.attrs.get("confidence")
    if confidence is not None and np.isfinite(confidence[i]) and confidence[i] < .5:
        return False
    category = _text(feats, "basic_category", i)
    taxonomy = _text(feats, "taxonomy", i)
    # Taxonomy names such as restaurant:american or shop:clothes retain their primary category.
    return any(v in PEDESTRIAN or v.split(":")[0] in PEDESTRIAN or v.endswith("_store")
               for v in (category, taxonomy))


def _valid(feats):
    """Reject malformed local cache geometry before optional scoring can break a road build."""
    if "_development_valid" in feats.__dict__:
        return feats.__dict__["_development_valid"]
    try:
        n = len(feats.ids)
        valid = feats.ids.ndim == 1 and feats.kind.shape == (n,) \
            and feats.lonlat.ndim == 2 and feats.lonlat.shape[1] == 2 \
            and np.isfinite(feats.lonlat).all() and np.isin(feats.kind, (1, 2, 3)).all()
        for off, size, end in ((feats.part_off, n + 1, len(feats.ring_off) - 1),
                               (feats.ring_off, len(feats.ring_off), len(feats.coord_off) - 1),
                               (feats.coord_off, len(feats.coord_off), len(feats.lonlat))):
            valid = valid and off.shape == (size,) and size > 0 and off.dtype.kind in "iu" \
                and off[0] == 0 and off[-1] == end and bool(np.all(np.diff(off) >= 0))
        valid = valid and all(np.shape(a) == (n,) for a in feats.attrs.values())
        confidence = feats.attrs.get("confidence")
        valid = valid and (confidence is None or confidence.dtype.kind in "fiu")
    except (AttributeError, IndexError, TypeError, ValueError):
        valid = False
    feats.__dict__["_development_valid"] = bool(valid)
    return bool(valid)


def _cached(md, key, layer):
    """Read existing memory/NPZ data only, including when a cached NPZ is damaged."""
    mem_lock = getattr(md, "_mem_lock", None)
    if mem_lock is not None:
        with mem_lock:
            got = md._mem.get((str(key), layer))
            if got is not None:
                f = got[0]
                return f if f is not None and _valid(f) else None
    if not hasattr(md, "path"):
        return None
    path = md.path(key, layer)
    try:
        st = os.stat(path)
        sig = (path, st.st_mtime_ns, st.st_size)
        with _lock:
            got = _memo.get(sig)
            if got is not None:
                _memo.move_to_end(sig)
                return got[0]
        with np.load(path, allow_pickle=False) as z:
            arrays = {n: z[n] for n in z.files if n in _GEOMETRY or n in {"attr." + a for a in _ATTRS}}
        if not _GEOMETRY <= arrays.keys():
            return None
        f = mapgeom.Features.from_arrays(layer, arrays, "")
        if not _valid(f):
            return None
        # Features caches coordinates and boxes for two frames. Budget those derived arrays
        # too, rather than retaining an unaccounted copy of a large city's geometry.
        size = sum(a.nbytes for a in arrays.values()) + 32 * len(f.lonlat) + 64 * len(f) + 1024
        if size <= MEM_BYTES:
            global _memo_bytes
            with _lock:
                old = _memo.pop(sig, None)
                if old is not None:
                    _memo_bytes -= old[1]
                _memo[sig] = (f, size)
                _memo_bytes += size
                while _memo_bytes > MEM_BYTES:
                    _, (_, n) = _memo.popitem(last=False)
                    _memo_bytes -= n
        return f
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("cached development %s %s: %s", key, layer, e)
        return None


def _halo(radius):
    return max(radius, FRONTAGE_M / 2 + 80.) + 2 * PIXEL_M


def _keys(frame, tx, ty, radius=RADIUS_M):
    import roads
    return roads.cells_of_tile(frame, tx, ty, _halo(radius))


def cache_key(md, frame, tx, ty):
    """Local source stamps for generated-content keys, including optional POIs arriving later."""
    if md is None or not hasattr(md, "path"):
        return ()
    stamps = []
    for key in _keys(frame, tx, ty):
        for layer in LAYERS:
            try:
                st = os.stat(md.path(key, layer))
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            stamps.append((str(key), layer, stamp))
    return tuple(stamps)


def for_tile(md, frame, tx, ty):
    """A lazy tile index, independent of whether building meshes are enabled."""
    import roads
    def load():
        layers = {name: [] for name in LAYERS}
        if md is None:
            return layers
        for key in _keys(frame, tx, ty):
            for name in LAYERS:
                f = _cached(md, key, name)
                if f is not None:
                    layers[name].append(f)
                # Missing sources stay sparse. Their arrival changes cache_key, so incomplete
                # optional data never forces network access or disables generated-content reuse.
        return layers
    return Context(frame, roads.tile_bbox(tx, ty), load)


def _smooth(value, lo, hi):
    t = max(0., min(1., (value - lo) / (hi - lo)))
    return t * t * (3. - 2. * t)


@dataclass(frozen=True)
class Sample:
    coverage: float
    frontage: float
    commercial: float
    poi: float
    score: float
    tier: int
    limited: float
    urban: float
    residential: float = 0.


class Context:
    """Reusable metric raster index; sample positions share context across road IDs and splits.

    layers maps names to lists of Features, or is a lazy function returning that mapping.
    Coverage is a 2 m approximation to polygon union, with holes and multipart preserved.
    Raster origins use the frame's metric lattice, so adjacent tiles use identical samples.
    """
    def __init__(self, frame, box, layers, radius=RADIUS_M):
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError("development radius must be positive and finite")
        self.frame, self.box, self.radius = frame, tuple(box), float(radius)
        self._layers = layers
        self._grid = None

    def _paint(self, target, part):
        """Union one polygon part through its own mask: holes cannot erase another footprint."""
        if not part or len(part[0]) < 3:
            return
        rings = [(np.asarray(r) - self.origin) / PIXEL_M - .5 for r in part]
        r = rings[0]
        if not np.isfinite(r).all():
            return
        x0, y0 = np.maximum(np.floor(r.min(axis=0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(r.max(axis=0)).astype(int) + 1, self.shape[::-1])
        if x1 <= x0 or y1 <= y0:
            return
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        draw = ImageDraw.Draw(mask)
        for j, ring in enumerate(rings):
            if len(ring) >= 3 and np.isfinite(ring).all():
                draw.polygon([tuple(p) for p in ring - [x0, y0]], fill=1 if j == 0 else 0)
        target[y0:y1, x0:x1] |= np.asarray(mask, dtype=np.uint8)

    def _build(self):
        if self._grid is not None:
            return
        halo = _halo(self.radius)
        lo = np.floor((np.asarray(self.box[:2]) - halo) / PIXEL_M) * PIXEL_M
        hi = np.ceil((np.asarray(self.box[2:]) + halo) / PIXEL_M) * PIXEL_M
        self.origin = lo
        self.shape = tuple(((hi - lo) / PIXEL_M).astype(int)[::-1])
        # raw footprint, occupied footprint, commercial footprint, commercial land, residential land, industrial land
        grids = np.zeros((6, *self.shape), np.uint8)
        poi, seen_poi, seen_buildings = [], set(), set()
        layers = self._layers() if callable(self._layers) else self._layers
        for name in LAYERS:
            for f in layers.get(name, ()):
                if f is None or not len(f) or not _valid(f):
                    continue
                boxes = f.boxes(self.frame)
                sel = (boxes[:, 0] <= hi[0]) & (boxes[:, 2] >= lo[0]) \
                    & (boxes[:, 1] <= hi[1]) & (boxes[:, 3] >= lo[1])
                xy = f.xy_in(self.frame)
                for i in np.flatnonzero(sel):
                    if name == "places":
                        fid = str(f.ids[i])
                        if fid not in seen_poi and _pedestrian(f, i):
                            seen_poi.add(fid)
                            a = f.coord_off[f.ring_off[f.part_off[i]]]
                            b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
                            if b > a:
                                poi.append(xy[a:b].mean(axis=0))
                        continue
                    if f.kind[i] != mapgeom.POLYGON:
                        continue
                    labels = _labels(f, i)
                    if name == "buildings":
                        fid = str(f.ids[i])
                        if fid in seen_buildings:
                            continue
                        seen_buildings.add(fid)
                        underground = f.attrs.get("is_underground")
                        if underground is not None and underground[i]:
                            continue
                        channels = [0]
                        if not labels & OUTBUILDINGS:
                            channels.append(1)
                            if labels & COMMERCIAL:
                                channels.append(2)
                    else:
                        channels = []
                        if labels & COMMERCIAL:
                            channels.append(3)
                        if labels & RESIDENTIAL:
                            channels.append(4)
                        if labels & INDUSTRIAL:
                            channels.append(5)
                    for part in f.parts(i, xy):
                        if name == "buildings" and part and len(part[0]) >= 3:
                            # Match MapData's oversized-footprint veto, only for polygons in
                            # this tile's halo. Scanning every building in a source cell here
                            # would repeat a Python loop over an entire city on cold reads.
                            ring = part[0] - part[0][0]
                            x, y = ring.T
                            area = .5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
                            if area > BUILDING_MAX_M2:
                                continue
                        for channel in channels:
                            self._paint(grids[channel], part)
        self._pois = np.array(poi, float).reshape((-1, 2))
        self._grid = grids
        # Row prefix sums give circular coverage in O(radius / resolution), independent of feature count.
        self._sums = np.pad(np.cumsum(grids, axis=2, dtype=np.int32), ((0, 0), (0, 0), (1, 0)))
        r = self.radius / PIXEL_M
        self._dy = np.arange(-int(r), int(r) + 1)
        self._dx = np.floor(np.sqrt(np.maximum(0., r * r - self._dy * self._dy))).astype(int)
        self._area = float(np.sum(2 * self._dx + 1))
        self._layers = None  # do not retain decoded cell vectors in each tile index

    def _circle(self, p):
        col, row = np.floor((p - self.origin) / PIXEL_M).astype(int)
        rr = row + self._dy
        inside = (rr >= 0) & (rr < self.shape[0])
        x0 = np.clip(col - self._dx[inside], 0, self.shape[1])
        x1 = np.clip(col + self._dx[inside] + 1, 0, self.shape[1])
        return (self._sums[:, rr[inside], x1] - self._sums[:, rr[inside], x0]).sum(axis=1) / self._area

    def _frontage(self, p, out, half):
        along = np.array([-out[1], out[0]])
        stations = np.arange(-FRONTAGE_M / 2, FRONTAGE_M / 2 + .1, PIXEL_M)
        offsets = half + np.array([6., 14., 22., 30.])
        q = p + stations[:, None, None] * along + offsets[None, :, None] * out
        ij = np.floor((q - self.origin) / PIXEL_M).astype(int)
        cols, rows = ij[..., 0], ij[..., 1]
        valid = (cols >= 0) & (cols < self.shape[1]) & (rows >= 0) & (rows < self.shape[0])
        hits = np.zeros(valid.shape, bool)
        hits[valid] = self._grid[1, rows[valid], cols[valid]] > 0
        hit = hits.any(axis=1)
        # Consecutive occupied samples measure continuity without a longest-run jump when
        # one small gap divides a street wall. The same spatial window spans road splits.
        return .5 * (float(hit.mean()) + float((hit[:-1] & hit[1:]).mean()))

    def sample(self, point_xy, outward_unit, road_half_width):
        """Context at a road centreline point; outward_unit selects the sidewalk's side."""
        self._build()
        p = np.asarray(point_xy, float)
        out = np.asarray(outward_unit, float)
        out = out / max(float(np.linalg.norm(out)), 1e-9)
        coverage, built, commercial_built, commercial_land, residential, industrial = self._circle(p)
        frontage = self._frontage(p, out, road_half_width)
        d2 = np.sum((self._pois - p) ** 2, axis=1)
        poi = float(np.maximum(0., 1. - d2 / self.radius ** 2).sum())
        commercial = min(1., max(commercial_built / max(built, .04), commercial_land / .35, poi / 4.))
        # A broad residential polygon is context, never proof of occupation. Dense residential
        # frontage stays limited unless there is local commercial or pedestrian evidence.
        score = min(1., .5 * min(built / .28, 1.) + .3 * frontage + .15 * commercial + .05 * min(poi / 4., 1.))
        limited = _smooth(built, .025, .14) * _smooth(score, .13, .32)
        urban = limited * _smooth(built, .10, .22) * _smooth(frontage, .25, .65) \
            * _smooth(commercial, .15, .65) * _smooth(score, .45, .70)
        # Industrial estates without a pedestrian cluster are at most limited decoration.
        urban *= 1. - _smooth(industrial, .3, .7) * (1. - _smooth(poi, 1., 4.))
        tier = 2 if urban >= .5 else (1 if limited > 0. else 0)
        return Sample(float(coverage), frontage, commercial, poi, score, tier, limited, urban, float(residential))
