"""OpenStreetMap's mapped natural features in the generator (the user, 2026-11-22: "feel free to use as many [OSM /
Overture data points] as possible if they would improve the generator"). Data: Overture's base/land (OSM natural=*:
mapped single trees and tree rows with their genus / species / leaf type / height / girth / crown in source_tags,
forests and woods with leaf_type / leaf_cycle, cliffs, rock, bare rock, scree, shingle, sand, beaches, dunes,
glaciers, wetlands; mapdata layer "land") and base/bathymetry (CC0 depth bands from ETOPO / GLOBathy; layer
"bathymetry"). The land_use layer's forests carry leaf_type too. Each use is behind its own switch, off by default
(nothing is read, built or keyed differently while off):

- BNE_NATURAL_TREES=on: the mapped trees and tree rows where they stand (vegetation.py), their species choosing the
  stock tree (natural.SPECIES), their height / crown tags (else the canopy model's top there, else the trunk's girth,
  else the species' usual) their size; the canopy model's tops within a mapped tree's crown dropped (no double trees),
  the furniture's made-up street trees near a mapped one too; none on a building, water or a deck, a trunk on a road's
  drawn corridor moved out of it as the canopy's street trees are (else left out).
- BNE_NATURAL_LEAF=on: the forests' leaf_type / leaf_cycle choose the canopy trees' broadleaf / conifer (in place of the
  regional share) and the evergreen broadleaves (holm oaks); =season also shows the mapped deciduous broadleaf forests
  bare in their winter (trees.lua's leafoff_n / leafoff_s groups: the stock leafless oaks, by hemisphere).
- BNE_NATURAL_GROUND=on: rock, scree, sand, beach, dune, glacier, wetland polygons painted on the physics blocks'
  material maps (rock, loose, sand, snow, mud: their ground models and detail looks, surface.MATERIALS); no scattered
  bushes or rocks on mapped sand or ice, rocks over mapped bare rock and scree.
- BNE_NATURAL_CLIFFS=on: Italy's rock-face pieces (trees.lua "cliff", as roads.cliff_rows' on rural road cuts) along
  mapped natural=cliff lines, their faces toward the line's right (OSM: the bottom on the right of the way), sized to
  the ground's drop across the line; a rock band painted along the line's foot.
- BNE_NATURAL_BATHY=on: the lakes' floors from the depth bands (a lake had 4 m at most), the sea's where NOAA's mosaic
  has none (seacarve.py).

The kernels are earthfast's natural.rs (NATIVE).
"""
import json
import logging
import os

import numpy as np

import dmath
import fast

log = logging.getLogger("earthd.natural")

NATIVE = 611
VERSION = 1             # (in the vegetation cells' and the sea cells' folder names while a switch is on)


TREES = os.environ.get("BNE_NATURAL_TREES", "off").strip().lower() == "on"
LEAF = os.environ.get("BNE_NATURAL_LEAF", "off").strip().lower() in ("on", "season")
SEASON = os.environ.get("BNE_NATURAL_LEAF", "off").strip().lower() == "season"
GROUND = os.environ.get("BNE_NATURAL_GROUND", "off").strip().lower() == "on"
CLIFFS = os.environ.get("BNE_NATURAL_CLIFFS", "off").strip().lower() == "on"
BATHY = os.environ.get("BNE_NATURAL_BATHY", "off").strip().lower() == "on"
VEG_ON = TREES or LEAF or GROUND or CLIFFS          # (the vegetation cells change: their own folder, veg_key)
SWITCHES = ("BNE_NATURAL_TREES", "BNE_NATURAL_LEAF", "BNE_NATURAL_GROUND", "BNE_NATURAL_CLIFFS", "BNE_NATURAL_BATHY")

# the land classes the generator reads (overture.LAYERS["land"]'s where)
TREE_CLASSES = ("tree",)
ROW_CLASSES = ("tree_row",)
FOREST_CLASSES = ("forest", "wood")
CLIFF_CLASSES = ("cliff",)
ROCK_CLASSES = ("rock", "bare_rock", "stone")
LOOSE_CLASSES = ("scree", "shingle")
SAND_CLASSES = ("sand", "beach", "dune")
ICE_CLASSES = ("glacier",)
WET_CLASSES = ("wetland",)
# a beach of pebbles or gravel is loose stones, not sand
LOOSE_SURFACES = ("gravel", "pebblestone", "pebbles", "shingle", "stone", "stones", "rock", "rocks", "fine_gravel",
                  "cobblestone")
GRASSY_WETLANDS = ("wet_meadow",)        # (a wet meadow is grass; marsh, swamp, bog, reedbed, mangrove, tidal flats: mud)

# natural_tree_info's species slots -> vegetation group names (natural.rs S_*; trees.lua's CATALOG)
SPECIES = ("deciduous", "conifer", "oak", "beech", "birch", "poplar", "pine", "fir", "redwood", "cypress", "juniper",
           "olive", "holm_oak", "palm", "eucalyptus")
S_DECIDUOUS, S_CONIFER = 0, 1
UNKNOWN = 255
# a species' usual height (m) where neither its tags nor the canopy say (a street or park tree of that kind)
DEFAULT_H = {"deciduous": 9.0, "conifer": 14.0, "oak": 12.0, "beech": 14.0, "birch": 11.0, "poplar": 18.0,
             "pine": 13.0, "fir": 15.0, "redwood": 25.0, "cypress": 8.0, "juniper": 5.0, "olive": 5.0,
             "holm_oak": 9.0, "palm": 9.0, "eucalyptus": 16.0}
ROW_SPACING_M = 8.0         # a tree row's trees this far apart (a crown diameter where tagged; avenues: 6-10 m)
DEDUPE_M = 3.0              # a canopy top this near a mapped tree (its crown's radius at least) is that tree
CANOPY_SEARCH_M = 3.0       # a mapped tree without a height: the canopy's top within this
TREE_MIN_M = 3.0            # (vegetation.TREE_MIN_M)
STREET_TREE_CLEAR_M = 6.0   # a made-up street tree (furniture.py) this near a mapped tree is left out
# leaf codes (natural.rs LEAF_* / CYCLE_*)
LEAF_BROAD, LEAF_NEEDLE, LEAF_MIXED = 1, 2, 3
CYCLE_DECIDUOUS, CYCLE_EVERGREEN = 4, 8
TROPICS_LAT = 23.5          # no winter look, no evergreen-broadleaf swap within the tropics (regional's own trees)
# the ground codes on the canopy grid (natural_leaf_raster's codes) for the scatter
GROUND_SANDY, GROUND_ICE, GROUND_ROCKY = 1, 2, 3
# cliffs (roads.CLIFF_SHAPES: Italy's rock-face pieces)
CLIFF_STEP_M = 3.0          # stations along a cliff line
CLIFF_PROBE_M = 15.0        # the ground's drop across the line: its height this far on either side
CLIFF_MIN_DROP_M = 2.0      # a drop under this: no piece (the elevation model flattens small cliffs)
CLIFF_SINK_M = 1.0
CLIFF_OVER_M = 0.3
CLIFF_SPACING = 0.55
CLIFF_MAX_M = 33.0          # (trees.lua M.pick scales a piece 2.5x at most: the tall piece's 13.2 m)
CLIFF_DEM_ZOOM = 15
CLIFF_BAND_M = 6.0          # the rock band painted on the ground below a cliff line (its foot) ...
CLIFF_TOP_M = 1.0           # ... and above it
# bathymetry
BATHY_RAMP_PX = 32          # a band's floor ramps over this many z16 pixels from its edge (the sea raster's pad:
                            # the same depths on both sides of a cell's edge)


def require():
    fast.require(NATIVE, "natural features")
    return fast._ef


def veg_key():
    """The vegetation cells' folder suffix: "" while every switch the cells see is off (their files as before)."""
    if not VEG_ON:
        return ""
    flags = "".join(c for c, on in (("t", TREES), ("l", LEAF), ("w", SEASON), ("g", GROUND), ("c", CLIFFS)) if on)
    return f"-nat{VERSION}{flags}"


def sea_key():
    """The sea cells' folder suffix: "" while BNE_NATURAL_BATHY is off."""
    return f"-natb{VERSION}" if BATHY else ""


# --- the land layer -----------------------------------------------------------------------------------------------
def classes(f):
    return f.attrs["class"].astype(str) if f is not None and len(f.kind) and f.attrs.get("class") is not None \
        else np.zeros(0, str)


def tags(f, sel):
    t = f.attrs.get("tags")
    return ["" if t is None or t[i] is None else str(t[i]) for i in sel]


def tag_values(f, sel, key):
    out = []
    for s in tags(f, sel):
        try:
            v = json.loads(s).get(key) if s else None
        except (ValueError, AttributeError):
            v = None
        out.append(v if isinstance(v, str) else "")
    return out


def _part_arrays(f):
    return (np.ascontiguousarray(f.lonlat, np.float64), np.ascontiguousarray(f.coord_off, np.int64),
            np.ascontiguousarray(f.ring_off, np.int64), np.ascontiguousarray(f.part_off, np.int64))


def _line_xy(f, sel, to3857):
    """The selected line features' first rings as Web Mercator points (k, 2) and offsets (r + 1)."""
    pts, off = [], [0]
    for i in sel:
        for part in f.parts(i):
            for ring in part[:1]:
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                x, y = to3857(ring[:, 0], ring[:, 1])
                pts.append(np.stack([x, y], axis=1))
                off.append(off[-1] + len(ring))
    return (np.concatenate(pts) if pts else np.zeros((0, 2)), np.asarray(off, np.int64))


def tree_inputs(feats_list, to3857):
    """The mapped single trees (points) and tree rows (lines) of the land Features in feats_list (the cell's and its
    neighbours'), for natural_trees: (pts (m, 2) Web Mercator, slot, height, crown, circumference, leaf, rows (k, 2),
    row_off, row_slot, row_height, row_crown, row_leaf)."""
    ef = require()
    pxy, ptags, rtags = [], [], []
    rpts, roff = [], [0]
    seen = set()                # (a line is in every cell it crosses, a point on an edge in both: each feature once)
    for f in feats_list:
        if f is None or not len(f.kind):
            continue
        cls = classes(f)
        ids = getattr(f, "ids", None)

        def fresh(i):
            if ids is None:
                return True
            k = str(ids[i])
            if k in seen:
                return False
            seen.add(k)
            return True
        pt = np.array([i for i in np.flatnonzero(np.isin(cls, TREE_CLASSES) & (f.kind == 1)) if fresh(i)], np.int64)
        for i in pt:
            ll = np.asarray(f.parts(i)[0][0], np.float64)[0]
            pxy.append(ll)
        ptags += tags(f, pt)
        rw = [i for i in np.flatnonzero(np.isin(cls, ROW_CLASSES) & (f.kind == 2)) if fresh(i)]
        for i in rw:
            xy, off = _line_xy(f, [i], to3857)
            if len(off) < 2:
                continue
            rpts.append(xy)
            roff.append(roff[-1] + len(xy))
            rtags.append(tags(f, [i])[0])
    if pxy:
        ll = np.asarray(pxy, np.float64)
        x, y = to3857(ll[:, 0], ll[:, 1])
        pts = np.ascontiguousarray(np.stack([x, y], axis=1))
    else:
        pts = np.zeros((0, 2))
    slot, leaf, h, cr, circ = ef.natural_tree_info(ptags)
    rslot, rleaf, rh, rcr, _ = ef.natural_tree_info(rtags)
    rows = np.ascontiguousarray(np.concatenate(rpts)) if rpts else np.zeros((0, 2))
    return (pts, slot, h, cr, circ, leaf, rows, np.asarray(roff, np.int64), rslot, rh, rcr, rleaf)


def forest_codes(feats_list):
    """(features, leaf codes) of the forests / woods with a leaf_type or leaf_cycle among feats_list (land: class forest
    or wood; land_use: class forest): [(f, sel, codes)]."""
    ef = require()
    out = []
    for f in feats_list:
        if f is None or not len(f.kind):
            continue
        cls = classes(f)
        sel = np.flatnonzero(np.isin(cls, FOREST_CLASSES) & (f.kind == 3))
        if not len(sel):
            continue
        _, leaf, _, _, _ = ef.natural_tree_info(tags(f, sel))
        has = leaf != 0
        if has.any():
            out.append((f, sel[has].astype(np.int64), np.ascontiguousarray(leaf[has], np.uint8)))
    return out


def code_raster(items, shape, grid):
    """[(f, sel, codes)] on a canopy-type grid (shape (n_r, n_c); grid x0, y0, dx, dy: Web Mercator): u8, 0 none, the
    smaller polygon's code over the larger's (within each list item; later items over earlier ones)."""
    ef = require()
    out = np.zeros(shape, np.uint8)
    for f, sel, codes in items:
        r = ef.natural_leaf_raster(*_part_arrays(f), np.ascontiguousarray(sel, np.int64),
                                   np.ascontiguousarray(codes, np.uint8), int(shape[0]), int(shape[1]),
                                   *(float(v) for v in grid))
        out = np.where(r != 0, r, out)
    return out


def ground_codes(feats_list):
    """[(f, sel, codes)] of the polygons the scatter reads (GROUND_*): sand / beach / dune, glacier, bare rock / scree."""
    out = []
    for f in feats_list:
        if f is None or not len(f.kind):
            continue
        cls = classes(f)
        area = f.kind == 3
        code = np.zeros(len(cls), np.uint8)
        code[np.isin(cls, SAND_CLASSES)] = GROUND_SANDY
        code[np.isin(cls, ICE_CLASSES)] = GROUND_ICE
        code[np.isin(cls, ROCK_CLASSES + LOOSE_CLASSES)] = GROUND_ROCKY
        sel = np.flatnonzero(area & (code != 0))
        if len(sel):
            out.append((f, sel.astype(np.int64), code[sel]))
    return out


def ground_materials(f, mats):
    """(sel, materials) of land Features f's polygons the ground paint draws; mats: {"rock", "loose", "sand", "snow",
    "mud", "grass"} -> surface material index."""
    cls = classes(f)
    if not len(cls):
        return np.zeros(0, np.int64), np.zeros(0, np.uint8)
    area = f.kind == 3
    m = np.full(len(cls), 255, np.int64)
    m[np.isin(cls, ROCK_CLASSES)] = mats["rock"]
    m[np.isin(cls, LOOSE_CLASSES)] = mats["loose"]
    m[np.isin(cls, SAND_CLASSES)] = mats["sand"]
    m[np.isin(cls, ICE_CLASSES)] = mats["snow"]
    wet = np.flatnonzero(np.isin(cls, WET_CLASSES) & area)
    if len(wet):
        kinds = tag_values(f, wet, "wetland")
        m[wet] = [mats["grass"] if k in GRASSY_WETLANDS else mats["mud"] for k in kinds]
    beach = np.flatnonzero(np.isin(cls, ("beach",)) & area)
    if len(beach):
        surf = f.attrs.get("surface")
        for k, i in enumerate(beach):
            s = str(surf[i]) if surf is not None and surf[i] is not None else ""
            if not s:
                s = tag_values(f, [i], "surface")[0]
            if s in LOOSE_SURFACES:
                m[i] = mats["loose"]
    sel = np.flatnonzero(area & (m != 255))
    return sel.astype(np.int64), m[sel].astype(np.uint8)


def paint_ground(idx, lat, lon, feats_list, mats, affine_coef, cliffs=None):
    """A physics block's material map idx (on its lat / lon grid) with the natural polygons (GROUND) and the cliffs'
    rock bands (CLIFFS) painted. affine_coef: (coef (3, 2), lo0, la0) of surface.grid_affine. -> idx (a new array when
    anything changed)."""
    ef = require()
    coef, lo0, la0 = affine_coef
    out = idx
    # the band widths in grid pixels: the grid's metres a column from the affine map (columns per degree of longitude)
    k = dmath.cos(dmath.radians(float(la0)))
    cols_per_deg = abs(float(coef[0][0]))
    dx_m = 111320.0 * k / cols_per_deg if cols_per_deg > 0 else 2.0
    for f in feats_list:
        if f is None or not len(f.kind):
            continue
        sel, m = ground_materials(f, mats) if GROUND else (np.zeros(0, np.int64), np.zeros(0, np.uint8))
        if CLIFFS if cliffs is None else cliffs:
            lf = np.flatnonzero(np.isin(classes(f), CLIFF_CLASSES) & (f.kind == 2)).astype(np.int64)
        else:
            lf = np.zeros(0, np.int64)
        if not len(sel) and not len(lf):
            continue
        got, n = ef.natural_ground_paint(np.ascontiguousarray(out, np.uint8), np.ascontiguousarray(coef, np.float64),
                                         float(lo0), float(la0), *_part_arrays(f), sel, np.ascontiguousarray(m),
                                         lf, np.full(len(lf), mats["rock"], np.uint8), CLIFF_BAND_M / dx_m,
                                         CLIFF_TOP_M / dx_m)
        if n:
            out = got
    return out


def far_from_mapped(xy, mapped_xy, clear_m=STREET_TREE_CLEAR_M):
    """Per point of xy (n, 2, metres in one frame): no mapped tree (mapped_xy, same frame) within clear_m."""
    xy = np.ascontiguousarray(np.asarray(xy, np.float64).reshape(-1, 2))
    if not len(xy) or mapped_xy is None or not len(mapped_xy):
        return np.ones(len(xy), bool)
    m = np.ascontiguousarray(np.asarray(mapped_xy, np.float64).reshape(-1, 2))
    fast.require(0, "points_near_segments")
    return ~np.asarray(fast._ef.points_near_segments(xy, m, m, np.full(len(m), float(clear_m))), bool)


def mapped_tree_xy(frame, tx, ty, md, margin_m=STREET_TREE_CLEAR_M):
    """The mapped single trees (points) and tree rows (segments) near road tile (tx, ty), in its anchor's frame:
    (points (m, 2), segments a (k, 2), b (k, 2)). A cell not at hand: revisit.fault (the tile is not kept)."""
    import mapcells
    import revisit
    import roads as roadsmod
    x0, y0, x1, y1 = roadsmod.tile_bbox(tx, ty)
    x0, y0, x1, y1 = x0 - margin_m, y0 - margin_m, x1 + margin_m, y1 + margin_m
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), np.zeros(4))
    pts, a, b = [], [], []
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        try:
            f = md.cell(key, layers=("land",), kind="demand").layers.get("land")
        except Exception as e:
            log.debug("natural: land of %s: %s: %s", key, type(e).__name__, e)
            revisit.fault()
            continue
        if f is None or not len(f.kind):
            continue
        cls = classes(f)
        for i in np.flatnonzero(np.isin(cls, TREE_CLASSES) & (f.kind == 1)):
            pts.append(np.asarray(f.parts(i)[0][0], np.float64)[0, :2])
        for i in np.flatnonzero(np.isin(cls, ROW_CLASSES) & (f.kind == 2)):
            for part in f.parts(i):
                ring = np.asarray(part[0], np.float64)
                if len(ring) >= 2:
                    e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
                    q = np.stack([np.asarray(e), np.asarray(n)], axis=1)
                    a.append(q[:-1])
                    b.append(q[1:])
    if pts:
        ll = np.asarray(pts)
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        p = np.stack([np.asarray(e), np.asarray(n)], axis=1)
        a.append(p)
        b.append(p)
    if not a:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.concatenate(a), np.concatenate(b)


def off_mapped(rows, frame, tx, ty, md, groups):
    """rows (furniture.place's) without the made-up street trees (groups) within STREET_TREE_CLEAR_M of a mapped tree
    or tree row (BNE_NATURAL_TREES: the mapped ones stand there)."""
    pick = [i for i, r in enumerate(rows) if r[0] in groups]
    if not pick:
        return rows
    a, b = mapped_tree_xy(frame, tx, ty, md)
    if not len(a):
        return rows
    fast.require(0, "points_near_segments")
    q = np.ascontiguousarray(np.array([rows[i][1:3] for i in pick], np.float64))
    hit = np.asarray(fast._ef.points_near_segments(q, np.ascontiguousarray(a), np.ascontiguousarray(b),
                                                   np.full(len(a), float(STREET_TREE_CLEAR_M))), bool)
    if not hit.any():
        return rows
    drop = {pick[j] for j in np.flatnonzero(hit)}
    return [r for i, r in enumerate(rows) if i not in drop]
