"""OpenStreetMap over machine-learned building outlines (the user, 2026-11-18: where OSM maps buildings well, OSM is the
truth).

Overture's buildings theme fills OSM's gaps with machine-learned outlines (Microsoft's, Google's, Shi et al.'s East Asia
set). Where OSM is sparse they are the buildings (a small town nobody traced: Hugo, Oklahoma has 1368 Microsoft
outlines and 142 OSM ones in its centre cell, the houses on the aerial). Where OSM maps the buildings well, an ML-only
outline is old: Overture 2026-11-18.0 added ~500 Microsoft outlines to Fells Point (Baltimore), rowhouses demolished
years ago standing on vacant lots and building sites (0.08 m aerials, 2026-11-18). Each map cell's buildings layer is
judged on its own rows when it is decoded (mapdata, after drop_huge), so every reader (the building tiles, the far
buildings, the street walls, the sidewalk furniture) sees the same buildings whatever is loaded and in whatever order:
the rule is earthfast osm_over_ml (osmml.rs): an ML outline whose 325 m square holds at least 5 OSM outlines and OSM's
share of the building area there is at least 70 % is left out, and an ML outline at least half of whose footprint lies
on OSM ones (a duplicate Overture's conflation left: of the ML outline's area, counted on a 32 x 32 lattice over its
box, so a small OSM shed at a large ML warehouse's edge does not drop the warehouse) wherever it is. An outline
reaching into two cells is in both layers: only its owner (the cell its box centre is in, Features.own) judges it, its
other copies are left out, so no copy brings back an outline its owner dropped. Other non-OSM sources (Esri Community
Maps, USGS Lidar: surveyed, not learned) are neither dropped nor counted.

Measured on the corpus (2026-11-18.0, ML outlines the planned cells own, before -> after): Fells Point 497 -> 44 (the
building-site ghosts gone; what stays is at the harbour's industrial edge and in windows a cell edge cuts), Phoenix
1464 -> 409 (an unmapped apartment complex and a school kept, with partly mapped streets of houses), Laramie 208 -> 31
(mostly RVs parked in an RV park), Vienna 800 -> 42 and Tromso 559 -> 11 (garden sheds and outbuildings OSM leaves
out). Small outlines stay (the user, 2026-11-19: keep small outbuildings): an ML outline of at most 40 m^2 (shed, garage,
garden house) with an OSM outline's box within 20 m of its own is kept (it stands behind a house); the demolished
rowhouses and building-site ghosts are all over 90 m^2, and a small one on a cleared lot or a duplicate of any size is
still dropped. Corpus ML outlines kept (before -> after): Vienna 42 -> 458, Tromso 11 -> 421, Phoenix 409 -> 718, Fells Point 44 -> 160, Manhattan 9 -> 55, Laramie 31 -> 42, Zermatt 26 -> 54.
A window is cut at its cell's edge (a cell reads no neighbour): near an edge fewer outlines decide, and an outline
the cut window cannot judge stays.

BNE_OSM_OVER_ML=0 keeps every outline (MapData.versions() keys it then: the caches made from either never mix)."""
import os

import numpy as np

import fast
import mapgeom

NATIVE = 501                    # earthfast.VERSION with osm_over_ml and its small-outline rescue
ON = os.environ.get("BNE_OSM_OVER_ML", "1").strip() not in ("0", "false", "off", "no")
VERSION = 1                     # (in MapData.versions() while off; roads.FACTS_FORMAT / roadwidth.WALLS_VERSION bumped)
OSM_DATASET = "OpenStreetMap"
ML_DATASETS = ("Microsoft ML Buildings", "Google Open Buildings", "doi:10.5281/zenodo.8174931")
KEPT, COVERED, DUPLICATE, ELSEWHERE = 0, 1, 2, 3

_ef = fast.native(NATIVE, "osmml")


def sources(f):
    """int8 per feature of a buildings layer: 1 OpenStreetMap, 2 machine-learned, 0 any other source."""
    ds = f.attrs.get("src_dataset")
    if ds is None:
        return np.zeros(len(f), np.int8)
    ds = np.asarray(ds).astype(str)
    out = np.zeros(len(ds), np.int8)
    out[ds == OSM_DATASET] = 1
    out[np.isin(ds, ML_DATASETS)] = 2
    return out


def verdicts(f, key):
    """KEPT / COVERED / DUPLICATE / ELSEWHERE (int8) per feature of map cell key's buildings layer f (osmml.rs)."""
    src = sources(f)
    if not (src == 2).any():
        return np.zeros(len(f), np.int8)
    own = np.ones(len(f), bool) if f.own is None else np.ascontiguousarray(f.own, bool)
    return np.asarray(_ef.osm_over_ml(np.ascontiguousarray(f.lonlat, np.float64).reshape(-1, 2),
                                      np.ascontiguousarray(f.part_off, np.int64),
                                      np.ascontiguousarray(f.ring_off, np.int64),
                                      np.ascontiguousarray(f.coord_off, np.int64), src, own,
                                      tuple(float(v) for v in key.bounds())), np.int8)


def apply(f, key):
    """Cell key's buildings layer f without the ML outlines OSM's buildings make stale (verdicts), or f itself when it
    keeps them all (or the switch is off)."""
    if not ON or f is None or not len(f) or "src_dataset" not in f.attrs:
        return f
    v = verdicts(f, key)
    return f if not v.any() else mapgeom.take(f, v == KEPT)
