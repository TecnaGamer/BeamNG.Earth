"""Local built-environment context for the optional furniture on existing sidewalks (PR #670, wyo-backroads).

A road tile's buildings and land use, with a halo, rasterised once per tile at PIXEL_M (earthfast fill_bits: one
call): footprint union within a metric radius, same-side developed frontage, commercial / residential / industrial
land use. furniture.place samples it at its stations to keep street trees, hydrants, benches and bins where a street
is really built up, instead of the town flag alone.

Deterministic: the same cells as the tile's own, read as they are (md.cell, kind "demand": the places too, which the
gas stations read for every road tile anyway), never only what happens to be on disk (the PR's way: a POI file
arriving later changed tiles already built). Buildings are labelled by class and subtype only (a city cell holds
~100k of them); land use by its tags too.
"""
from dataclasses import dataclass
import json
import logging
import math
import os

import numpy as np

import mapgeom
from mapdata import BUILDING_MAX_M2

log = logging.getLogger("earthd.development")
RADIUS_M = max(25., min(250., float(os.environ.get("BNE_DECOR_RADIUS_M", "100"))))
PIXEL_M = 2.0
FRONTAGE_M = 120.0
LAYERS = ("buildings", "land_use", "places")
COMMERCIAL = ("commercial", "retail", "office", "offices", "shop", "supermarket", "government", "civic", "public",
              "hotel")
RESIDENTIAL = ("residential", "house", "detached", "semidetached_house", "terrace", "apartments", "dormitory",
               "bungalow")
# These still count as raw footprint, but are not occupied street development.
OUTBUILDINGS = ("agricultural", "barn", "farm", "farm_auxiliary", "shed", "garage", "garages", "greenhouse", "roof",
                "ruins", "construction", "abandoned")
INDUSTRIAL = ("industrial", "warehouse", "manufacture", "depot")
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

# channel bits: raw footprint, occupied footprint, commercial footprint, commercial / residential / industrial land
B_RAW, B_OCC, B_COM, L_COM, L_RES, L_IND = (1 << k for k in range(6))


def _col(feats, name):
    a = feats.attrs.get(name)
    if a is None:
        return np.full(len(feats), "", object)
    return np.array([str(v or "").strip().lower().replace(" ", "_") for v in a], object)


def _bits(feats, layer):
    """(n,) uint8 channel bits of each feature (polygons only, others 0), once per Features (a cell is shared by its
    tiles)."""
    memo = feats.__dict__.get("_development_bits")
    if memo is not None:
        return memo
    n = len(feats)
    out = np.zeros(n, np.uint8)
    if n:
        cls, sub = _col(feats, "class"), _col(feats, "subtype")
        has = lambda labels: np.isin(cls, labels) | np.isin(sub, labels)     # noqa: E731
        poly = np.asarray(feats.kind) == mapgeom.POLYGON
        if layer == "buildings":
            under = feats.attrs.get("is_underground")
            ok = poly & ~(np.asarray(under, bool) if under is not None else np.zeros(n, bool))
            occ = ok & ~has(OUTBUILDINGS)
            out[ok] |= B_RAW
            out[occ] |= B_OCC
            out[occ & has(COMMERCIAL)] |= B_COM
        else:
            tags = feats.attrs.get("tags")
            if tags is not None:            # (OSM's landuse / shop / office tags where the class is not set)
                extra = np.full(n, "", object)
                for i in np.flatnonzero(poly & (cls == "")):
                    try:
                        t = json.loads(str(tags[i]) or "{}")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(t, list):     # ([{"key", "value"}]: Overture's source tags form)
                        t = {str(d.get("key")): d.get("value") for d in t if isinstance(d, dict)}
                    if isinstance(t, dict):
                        for k in ("landuse", "building", "office", "shop"):
                            v = str(t.get(k, "") or "").strip().lower()
                            if v:
                                extra[i] = v
                                break
                cls = np.where(cls == "", extra, cls)
            out[poly & has(COMMERCIAL)] |= L_COM
            out[poly & has(RESIDENTIAL)] |= L_RES
            out[poly & has(INDUSTRIAL)] |= L_IND
    feats.__dict__["_development_bits"] = out
    return out


def _pois(feats):
    """(m,) indices of the pedestrian places (shops, cafes, offices ...: open, confidence >= 0.5), once per
    Features."""
    memo = feats.__dict__.get("_development_pois")
    if memo is not None:
        return memo
    n = len(feats)
    status, cat, tax = _col(feats, "operating_status"), _col(feats, "basic_category"), _col(feats, "taxonomy")
    conf = feats.attrs.get("confidence")
    ok = np.array(["closed" not in s for s in status], bool) if n else np.zeros(0, bool)
    if conf is not None:
        c = np.asarray(conf, np.float64)
        ok &= ~(np.isfinite(c) & (c < .5))
    ped = np.array([any(v in PEDESTRIAN or v.split(":")[0] in PEDESTRIAN or v.endswith("_store") for v in (a, b))
                    for a, b in zip(cat, tax)], bool) if n else np.zeros(0, bool)
    out = np.flatnonzero(ok & ped)
    feats.__dict__["_development_pois"] = out
    return out


def _ranges(starts, ends):
    """(concatenated arange(s, e) of each pair, each pair's length), vectorised."""
    starts, ends = np.asarray(starts, np.int64), np.asarray(ends, np.int64)
    lens = np.maximum(ends - starts, 0)
    total = int(lens.sum())
    if total == 0:
        return np.zeros(0, np.int64), lens
    first = np.repeat(starts - np.r_[0, np.cumsum(lens)[:-1]], lens)
    return first + np.arange(total, dtype=np.int64), lens


def _halo(radius):
    return max(radius, FRONTAGE_M / 2 + 80.) + 2 * PIXEL_M


def for_tile(md, frame, tx, ty):
    """A lazy tile index (built at its first sample), whether or not the building meshes are on."""
    import roads

    def load():
        layers = {name: [] for name in LAYERS}
        if md is None:
            return layers
        for key in roads.cells_of_tile(frame, tx, ty, _halo(RADIUS_M)):
            try:
                got = md.cell(key, layers=LAYERS, kind="demand").layers
            except Exception as e:          # (no context: every sample tier 0)
                log.debug("development: cell %s: %s", key, e)
                return None
            for name in LAYERS:
                f = got.get(name)
                if f is not None and len(f):
                    layers[name].append(f)
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
    """The tile's metric raster index; samples share it across road ids and splits. layers: {name: [Features]} or a
    function returning it (None: no data, every sample tier 0). The raster's origin is on the frame's PIXEL_M
    lattice, so neighbouring tiles sample the same pixels."""

    def __init__(self, frame, box, layers, radius=RADIUS_M):
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError("development radius must be positive and finite")
        self.frame, self.box, self.radius = frame, tuple(box), float(radius)
        self._layers = layers
        self._grid = None

    def _build(self):
        if self._grid is not None:
            return
        import fast
        fast.require(149, "fill_bits")
        halo = _halo(self.radius)
        lo = np.floor((np.asarray(self.box[:2], np.float64) - halo) / PIXEL_M) * PIXEL_M
        hi = np.ceil((np.asarray(self.box[2:], np.float64) + halo) / PIXEL_M) * PIXEL_M
        self.origin = lo
        self.shape = tuple(((hi - lo) / PIXEL_M).astype(int)[::-1])
        layers = self._layers() if callable(self._layers) else self._layers
        self.unavailable = layers is None       # (the cells could not be read: sample() is None, the old rule)
        xs, ring_offs, part_offs, bits_all = [], [], [], []
        n_coords = n_rings = 0
        seen, poi, seen_poi = set(), [], set()
        for name in LAYERS:
            for f in (layers or {}).get(name, ()):
                if name == "places":
                    idx = _pois(f)
                    if len(idx):
                        xy = f.xy_in(self.frame)
                        at = xy[f.coord_off[f.ring_off[f.part_off[idx]]]]
                        inside = (at[:, 0] >= lo[0]) & (at[:, 0] <= hi[0]) & (at[:, 1] >= lo[1]) & (at[:, 1] <= hi[1])
                        for i, q in zip(idx[inside], at[inside]):
                            fid = str(f.ids[i])
                            if fid not in seen_poi:
                                seen_poi.add(fid)
                                poi.append(q)
                    continue
                bits = _bits(f, name)
                boxes = f.boxes(self.frame)
                sel = (bits > 0) & (boxes[:, 0] <= hi[0]) & (boxes[:, 2] >= lo[0]) \
                    & (boxes[:, 1] <= hi[1]) & (boxes[:, 3] >= lo[1])
                idx = np.flatnonzero(sel)
                if name == "buildings" and len(idx):        # (a building in two cells: once)
                    ids = f.ids[idx]
                    keep = np.array([i not in seen for i in ids], bool)
                    seen.update(ids[keep].tolist())
                    idx = idx[keep]
                if not len(idx):
                    continue
                xy = (f.xy_in(self.frame) - lo) / PIXEL_M
                parts, pcount = _ranges(f.part_off[idx], f.part_off[idx + 1])
                rings, rcount = _ranges(f.ring_off[parts], f.ring_off[parts + 1])
                coords, ccount = _ranges(f.coord_off[rings], f.coord_off[rings + 1])
                pbits = np.repeat(bits[idx], pcount)
                if name == "buildings" and len(coords):     # MapData's oversized-footprint veto (each outline)
                    starts = np.r_[0, np.cumsum(ccount)[:-1]]
                    p = xy[coords] * PIXEL_M
                    nxt = np.arange(1, len(coords) + 1)
                    nxt[starts + ccount - 1] = starts       # (each ring closes on itself)
                    cross = p[:, 0] * p[nxt, 1] - p[nxt, 0] * p[:, 1]
                    area = 0.5 * np.abs(np.add.reduceat(cross, starts))
                    outline = np.r_[0, np.cumsum(rcount)[:-1]]      # each part's first ring
                    pbits = np.where(area[outline] > BUILDING_MAX_M2, 0, pbits)
                xs.append(xy[coords])
                ring_offs.append(n_coords + np.r_[0, np.cumsum(ccount)][:-1])
                part_offs.append(n_rings + np.r_[0, np.cumsum(rcount)][:-1])
                bits_all.append(np.asarray(pbits, np.uint8))
                n_coords += len(coords)
                n_rings += len(rings)
        h, w = self.shape
        if xs:
            grid = fast._ef.fill_bits(int(h), int(w), np.ascontiguousarray(np.concatenate(xs), np.float64),
                                      np.r_[np.concatenate(ring_offs), n_coords].astype(np.int64),
                                      np.r_[np.concatenate(part_offs), n_rings].astype(np.int64),
                                      np.ascontiguousarray(np.concatenate(bits_all), np.uint8))
        else:
            grid = np.zeros((h, w), np.uint8)
        grids = np.stack([((grid >> k) & 1).astype(np.uint8) for k in range(6)])
        self._grid = grids
        self._pois = np.array(poi, float).reshape((-1, 2))
        # row prefix sums: circular coverage in O(radius / resolution), whatever the feature count
        self._sums = np.pad(np.cumsum(grids, axis=2, dtype=np.int32), ((0, 0), (0, 0), (1, 0)))
        r = self.radius / PIXEL_M
        self._dy = np.arange(-int(r), int(r) + 1)
        self._dx = np.floor(np.sqrt(np.maximum(0., r * r - self._dy * self._dy))).astype(int)
        self._area = float(np.sum(2 * self._dx + 1))
        self._layers = None             # (no decoded cells kept in each tile's index)

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
        # consecutive occupied samples: continuity, without one small gap splitting a street wall
        return .5 * (float(hit.mean()) + float((hit[:-1] & hit[1:]).mean()))

    def sample(self, point_xy, outward_unit, road_half_width):
        """The context at a road centreline point; outward_unit picks the sidewalk's side. None when the tile's
        cells could not be read (furniture.place then keeps the town rule)."""
        self._build()
        if self.unavailable:
            return None
        p = np.asarray(point_xy, float)
        out = np.asarray(outward_unit, float)
        out = out / max(float(np.linalg.norm(out)), 1e-9)
        coverage, built, commercial_built, commercial_land, residential, industrial = self._circle(p)
        frontage = self._frontage(p, out, road_half_width)
        d2 = np.sum((self._pois - p) ** 2, axis=1)
        poi = float(np.maximum(0., 1. - d2 / self.radius ** 2).sum())
        commercial = min(1., max(commercial_built / max(built, .04), commercial_land / .35, poi / 4.))
        # a broad residential polygon is context, never proof of occupation
        score = min(1., .5 * min(built / .28, 1.) + .3 * frontage + .15 * commercial + .05 * min(poi / 4., 1.))
        limited = _smooth(built, .025, .14) * _smooth(score, .13, .32)
        urban = limited * _smooth(built, .10, .22) * _smooth(frontage, .25, .65) \
            * _smooth(commercial, .15, .65) * _smooth(score, .45, .70)
        urban *= 1. - _smooth(industrial, .3, .7) * (1. - _smooth(poi, 1., 4.))   # an industrial estate without a
        tier = 2 if urban >= .5 else (1 if limited > 0. else 0)                  # pedestrian cluster: limited at most
        return Sample(float(coverage), frontage, commercial, poi, score, tier, limited, urban, float(residential))
