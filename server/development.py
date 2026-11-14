"""Local built-environment context for the optional furniture on existing sidewalks (PR #670, wyo-backroads).

A road tile's buildings and land use, with a halo, rasterised once per tile at PIXEL_M: footprint union within a
metric radius, same-side developed frontage, commercial / residential / industrial land use. furniture.place samples
it at its stations to keep street trees, hydrants, benches and bins where a street is really built up, instead of the
town flag alone.

Deterministic: the same cells as the tile's own, read as they are (md.cell, kind "demand": the places too, which the
gas stations read for every road tile anyway), never only what happens to be on disk (the PR's way: a POI file
arriving later changed tiles already built). Buildings are labelled by class and subtype only (a city cell holds
~100k of them); land use by its tags too.

The work is earthfast's (devctx.rs, 2026-11-10: the labels' str().strip().lower() over ~100k values per city cell was
40 % of furniture.place): the label tables and the tags' JSON, the raster (one call per tile) and the samples. This
module only hands it the Features' arrays.
"""
from dataclasses import dataclass
import logging
import math
import os

import numpy as np

import fast
import mapgeom
import mapdata
from mapdata import BUILDING_MAX_M2

log = logging.getLogger("earthd.development")
RADIUS_M = max(25., min(250., float(os.environ.get("BNE_DECOR_RADIUS_M", "100"))))
PIXEL_M = 2.0
FRONTAGE_M = 120.0
LAYERS = ("buildings", "land_use", "places")
NATIVE = 188            # earthfast's devctx (the label tables, the raster and the samples live there)


def _ucs4(feats, name, as_str=True):
    """An attribute as (n, w) UCS-4 rows for the kernels (each value's str(v or ""): numpy's unicode arrays as they
    are), None where the Features has no such attribute."""
    a = feats.attrs.get(name)
    if a is None:
        return None
    a = np.asarray(a)
    if a.dtype.kind != "U" and as_str:
        a = np.array([str(v or "") for v in a.tolist()], dtype=str).reshape(-1)
    return fast.ucs4(a)


def _bits(feats, layer):
    """(n,) uint8 channel bits of each feature (polygons only, others 0), once per Features (a cell is shared by its
    tiles): earthfast dev_building_bits / dev_land_bits, the labels normalised and matched there."""
    memo = feats.__dict__.get("_development_bits")
    if memo is not None:
        return memo
    n = len(feats)
    if not n:
        out = np.zeros(0, np.uint8)
    else:
        fast.require(NATIVE, "development labels")
        poly = np.ascontiguousarray(np.asarray(feats.kind) == mapgeom.POLYGON)
        cls, sub = _ucs4(feats, "class"), _ucs4(feats, "subtype")
        if layer == "buildings":
            under = feats.attrs.get("is_underground")
            out = fast._ef.dev_building_bits(poly, cls, sub, None if under is None else
                                             np.ascontiguousarray(np.asarray(under, bool)))
        else:       # (OSM's landuse / building / office / shop tags where the class is not set)
            out = fast._ef.dev_land_bits(poly, cls, sub, _ucs4(feats, "tags", as_str=False))
    feats.__dict__["_development_bits"] = out
    return out


def _pois(feats):
    """(m,) indices of the pedestrian places (shops, cafes, offices ...: open, confidence >= 0.5), once per
    Features (earthfast dev_pois)."""
    memo = feats.__dict__.get("_development_pois")
    if memo is not None:
        return memo
    n = len(feats)
    if not n:
        out = np.zeros(0, np.int64)
    else:
        fast.require(NATIVE, "development places")
        conf = feats.attrs.get("confidence")
        out = fast._ef.dev_pois(n, _ucs4(feats, "operating_status"), _ucs4(feats, "basic_category"),
                                _ucs4(feats, "taxonomy"),
                                None if conf is None else np.ascontiguousarray(np.asarray(conf, np.float64)))
    feats.__dict__["_development_pois"] = out
    return out


def _f64(a):
    return np.ascontiguousarray(a, np.float64)


def _offsets(f):
    return tuple(np.ascontiguousarray(o, np.int64) for o in (f.part_off, f.ring_off, f.coord_off))


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
                # (the places as the road tiles read them: their pedestrian places only, mapdata.places_slim)
                got = dict(md.cell(key, layers=tuple(n for n in LAYERS if n != "places"), kind="demand").layers)
                got["places"] = mapdata.places_of(md, key)
            except Exception as e:          # (no context: every sample tier 0)
                log.debug("development: cell %s: %s", key, e)
                return None
            for name in LAYERS:
                f = got.get(name)
                if f is not None and len(f):
                    layers[name].append(f)
        return layers
    return Context(frame, roads.tile_bbox(tx, ty), load)


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
        fast.require(NATIVE, "development context")
        halo = _halo(self.radius)
        lo = np.floor((np.asarray(self.box[:2], np.float64) - halo) / PIXEL_M) * PIXEL_M
        hi = np.ceil((np.asarray(self.box[2:], np.float64) + halo) / PIXEL_M) * PIXEL_M
        self.origin = lo
        self.shape = tuple(((hi - lo) / PIXEL_M).astype(int)[::-1])
        layers = self._layers() if callable(self._layers) else self._layers
        self.unavailable = layers is None       # (the cells could not be read: sample() is None, the old rule)
        polys, pois = [], []
        for name in LAYERS:
            for f in (layers or {}).get(name, ()):
                if name == "places":
                    idx = _pois(f)
                    if len(idx):
                        pois.append((_f64(f.xy_in(self.frame)), *_offsets(f), idx, fast.ucs4(f.ids)))
                    continue
                bits = _bits(f, name)
                if not bits.any():
                    continue
                polys.append((name == "buildings", _f64(f.xy_in(self.frame)), *_offsets(f), bits,
                              _f64(f.boxes(self.frame)), fast.ucs4(f.ids)))
        h, w = self.shape
        self._grid = fast._ef.DevContext((float(lo[0]), float(lo[1])), (float(lo[0]), float(lo[1])),
                                         (float(hi[0]), float(hi[1])), int(h), int(w), self.radius,
                                         self.radius * self.radius, float(BUILDING_MAX_M2), polys, pois)
        self._layers = None             # (no decoded cells kept in each tile's index)

    def samples(self, points_xy, outward, road_half_width):
        """[Sample] at road centreline points (n, 2), outward (n, 2) unit vectors picking each sidewalk's side,
        road_half_width (n,) or one; None when the tile's cells could not be read (furniture.place then keeps the
        town rule). One kernel call for all."""
        self._build()
        if self.unavailable:
            return None
        p = np.ascontiguousarray(np.asarray(points_xy, np.float64).reshape(-1, 2))
        out = np.ascontiguousarray(np.asarray(outward, np.float64).reshape(-1, 2))
        half = np.ascontiguousarray(np.broadcast_to(np.asarray(road_half_width, np.float64), (len(p),)))
        rows = self._grid.samples(p, out, half).tolist()
        return [Sample(*r[:5], int(r[5]), *r[6:]) for r in rows]

    def sample(self, point_xy, outward_unit, road_half_width):
        """The context at a road centreline point; outward_unit picks the sidewalk's side. None when the tile's
        cells could not be read (furniture.place then keeps the town rule)."""
        got = self.samples(np.asarray(point_xy, np.float64)[None, :2], np.asarray(outward_unit, np.float64)[None, :2],
                           road_half_width)
        return None if got is None else got[0]
