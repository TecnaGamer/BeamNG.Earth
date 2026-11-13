"""areaprops.py: stock props that fill mapped places (the user, 2026-11-10: "lets use as many assets as needed to give this world
detail and variety"), as Forest rows [group, x, y, z, yaw, scale, height, rank] of a road tile (trees.lua CATALOG):

- solar farms: OpenStreetMap's power=generator areas with generator:source=solar (not on a roof) and the power=plant
  areas with plant:source=solar (where no generator area is mapped: a plant's outline round mapped arrays is left to
  them), filled with West Coast USA's panel table in rows, east-west and facing the equator (fixed tilt, the common
  kind; the stock table is 16 m long, its face looks along local +y), blocks of rows between service aisles;
- container terminals (man_made=container_terminal, industrial=container_*): stacks of 40 ft boxes 1-4 high in
  blocks between lanes, an apron kept free along the quay, and ship-to-shore gantry cranes along the quay (where the
  terminal's outline meets water), their booms out over the water;
- marinas (leisure=marina): boats moored along the piers (man_made=pier lines and outlines) where the water is;
- trailer parks (residential=trailer_park, landuse=residential): Utah's caravan in rows between the park's streets,
  scaled to a single-wide home in North America; caravan sites (tourism=caravan_site, and camp sites with
  caravans=yes) sparser, at its own size; a park or site whose homes are mapped as buildings is left to them;
- construction sites (landuse=construction): a hoarding of chain-link panels along the outline, gaps where a road
  goes in, and a tower crane on a big site.
The layouts are earthfast's (places.rs: area_grid, edge_spots, berths, outline_panels); this module picks the places
from the map data and makes the rows. Every tile lays the same grid over a place and keeps what is in its own square,
so a place across tiles is neither doubled nor cut (a clipped land use's pieces each get their own grid).
Deterministic: hashes of the feature ids and grid indices.
"""
import json
import logging
import math
import os

import numpy as np

import fast
import dmath
import dnp
import mapgeom

log = logging.getLogger("earthd.places")

NATIVE = 200
ENABLED = os.environ.get("BNE_PLACES", "on") != "off"       # (off: none of them; A/B measurements)

# --- solar farms: levels/west_coast_usa/art/shapes/objects/solarpanel: x -8..8 (its length), y -1.80..1.85, z
# -0.35..3.19 (its legs into the ground); the panel's high edge at -y, so its face looks along local +y.
G_SOLAR, SOLAR_H = "solarpanel", 3.54
SOLAR_SIZE = (16.0, 3.65)
# rows end to end (0.3 m between tables), 7.6 m apart (ground cover ~0.48: a fixed-tilt farm's; Topaz Solar Farm's
# aerial 2026-11-10: blocks of ~20 rows between 6 m dirt aisles, ~150 m wide)
SOLAR_PITCH = (16.3, 7.6)
SOLAR_BLOCK = (9, 18)
SOLAR_AISLE = (6.0, 5.0)
SOLAR_MARGIN = 2.0
SOLAR_MIN_M2 = 60.0             # a smaller array is a garden's (one mapped row of tables: ~100-300 m²); one whose
                                # centre is in a building's footprint is on its roof
SOLAR_ROOF = {"roof", "rooftop"}
SOLAR_ROAD_M = 1.5              # m kept between a table and a road's edge
SOLAR_BLD_M = 3.0               # ... and a building (the inverters)
SOLAR_ODD_RANK = 0.45           # every other row ranks here: far tiles (trees.lua RINGS) show every other row

# --- container terminals: West Coast USA's 40 ft boxes (x across 2.44, y along 12.21, z 0..2.59)
G_STACK, STACK_H = "stack", 2.59
STACK_SIZE = (12.2, 2.5)
STACK_PITCH = (12.6, 2.8)
STACK_BLOCK = (8, 6)            # a block: 8 boxes long, 6 rows wide (a rubber-tyred gantry's span) ...
STACK_AISLE = (10.0, 14.0)      # ... between the lanes the straddle carriers and trucks use
STACK_MARGIN = 3.0
STACK_ROAD_M = 2.0
STACK_BLD_M = 4.0
STACK_APRON_M = 45.0            # m kept free behind the quay's edge (the cranes' apron)
STACK_LEVELS = ((0.10, 0), (0.30, 1), (0.60, 2), (0.85, 3), (1.01, 4))   # (cumulative share, boxes high)
STACK_RANK = (0.0, 0.12)        # the bottom boxes' ranks; the upper ones' 0.12 .. 0.3 (fewer far away)
CONTAINER_IND = {"container_terminal", "container_stacking", "container_storage", "container_yard", "container"}
CONTAINER_MIN_M2 = 3000.0
# Industrial's ind_portcrane_a (global materials): legs at y -7 .. +5, its boom out to y -31 (over the water) and back
# to +17; it stands 0.9 m over the quay its boxes stand on in the stock level; legs to z -5 (into the quay)
G_CRANE, CRANE_H, CRANE_LIFT = "portcrane", 35.47, 0.9
CRANE_SPACING = 95.0            # m between cranes along a quay
CRANE_PROBE = 30.0              # m out from the outline that must be water (and in that must not)
CRANE_INSET = 9.0               # m from the quay's edge to the crane's origin (the waterside legs ~2 m in)
CRANE_MIN_M2 = 20000.0
WATER_CLASSES = None            # (every water polygon counts)

# --- marinas: Italy's boats (italy_boat_small_a .. e, global materials; their lengths along local x): (group, length,
# beam, height), trees.lua CATALOG boat_*
BOATS = (("boat_a", 7.73, 3.00, 5.46), ("boat_b", 10.59, 5.29, 9.40), ("boat_c", 10.63, 5.29, 7.89),
         ("boat_d", 12.31, 4.79, 9.17), ("boat_e", 12.23, 4.04, 13.93))
BOAT_SLOT = (11.0, 4.3)         # a berth: length along the pier, beam
BOAT_GAP = 1.2                  # m between boats along a pier
BOAT_OFF = 0.7                  # m between a boat and its pier's edge
BOAT_CLEAR = 0.4                # m a boat keeps from every other pier
BOAT_SINK = 0.35                # m the hull's bottom sits under the water's surface
PIER_HW = 1.0                   # a pier line's half width
BOAT_KEEP = 0.8                 # share of the berths with a boat
BERTH_REACH_M = 40.0            # m round a tile whose piers are looked at

# --- trailer parks: Utah's caravan (x -1.68..1.45, y -4.92..3.55 along its length, z 0..2.85)
G_CARAVAN, CARAVAN_H = "caravan", 2.85
CARAVAN_C = (-0.115, -0.685)    # its footprint's centre in its own axes
CARAVAN_LEN, CARAVAN_W = 8.47, 3.13
NA_HOME_SCALE = 1.35            # a North American park's single-wide (~11.4 x 4.2 m) from the same caravan
TRAILER_PITCH = (4.0, 11.0)     # m beside each other (lots ~12 m wide) and m between rows (with its yard)
CARAVAN_SITE_KEEP = 0.45        # a caravan site's pitches with a caravan on them
TRAILER_ROAD_M = 2.0
TRAILER_BLD_M = 1.5
TRAILER_MIN_M2 = 2000.0
NA = {"US", "CA"}

# --- construction sites: the stock chain-link panel (2.04 m, origin at its forward end); a tower crane (G_TC)
G_HOARD, HOARD_LEN, HOARD_H, HOARD_LIFT = "chainlink", 2.04, 1.76, 0.16
# (2026-11-11, the user in game: "do we have a better crane model or textures?": West Coast USA's s_crane_tower, a
# yellow lattice tower crane, 39 m to its jib, the jib 64 m, origin at the mast's foot; Italy's two low-poly pieces
# stood as a thin grey tripod with an angled arm)
G_TC, TC_H = "towercrane", 38.98
TC_SCALE = (0.85, 1.15)         # (by the hash: cranes of a site not all one height)
SITE_MIN_M2 = 400.0
SITE_MAX_M2 = 200000.0          # bigger "construction" areas are a whole new district (fenced in parts)
TC_MIN_M2 = 2500.0              # a site this big gets a crane
TC_EVERY_M2 = 12000.0           # ... and one more for each this much more, up to TC_MAX
TC_MAX = 3
HOARD_GATE_M = 2.0              # a panel this near a road's edge is left out (the site's entrance)


def _tags(feats, i):
    """source_tags of feature i as a lower-case dict (shelters._tags' memo on the layer, shared: not to be changed)."""
    tags = feats.attrs.get("tags")
    if tags is None or i >= len(tags):
        return {}
    memo = feats.__dict__.setdefault("_lc_tags", {})
    got = memo.get(i)
    if got is not None:
        return got
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        values = None
    got = {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()} \
        if isinstance(values, dict) else {}
    memo[i] = got
    return got


# a feature's raw tags must hold one of these before they are parsed (most of a cell's infrastructure is kerbs and
# crossings: ~30k json.loads a Munich cell block otherwise)
KEYWORDS = ("solar", "container", "marina", "trailer_park", "caravan", "construction")
PLACE_CLASSES = {"construction", "marina"}


def _maybe(feats, i):
    """Could feature i be a place (its class or its raw tags say so)?"""
    if _cls(feats, i) in PLACE_CLASSES:
        return True
    tags = feats.attrs.get("tags")
    if tags is None or i >= len(tags):
        return False
    raw = str(tags[i]).lower()
    return any(k in raw for k in KEYWORDS)


def _cls(feats, i):
    c = feats.attrs.get("class")
    return str(c[i]).strip().lower() if c is not None and i < len(c) else ""


def _h01(*vals):
    import hashlib
    h = hashlib.blake2b(repr(vals).encode(), digest_size=8).digest()
    return int.from_bytes(h, "little") / 2.0 ** 64


def _multi(values):
    return {v.strip() for v in str(values or "").split(";") if v.strip()}


def kind_of(feats, i, layer):
    """Which place feature i of a layer is: 'solar_gen', 'solar_plant', 'terminal', 'marina', 'trailer',
    'caravan_site', 'construction' or None. Pure."""
    t = _tags(feats, i)
    cls = _cls(feats, i)
    if "solar" in _multi(t.get("generator:source")) and t.get("power", "generator") == "generator" \
            and t.get("location", "") not in SOLAR_ROOF and "roof" not in t.get("generator:place", ""):
        return "solar_gen"
    if "solar" in _multi(t.get("plant:source")):
        return "solar_plant"
    if t.get("man_made") == "container_terminal" or t.get("industrial") in CONTAINER_IND:
        return "terminal"
    if cls == "marina" or t.get("leisure") == "marina":
        return "marina"
    if t.get("residential") == "trailer_park" or t.get("landuse") == "trailer_park":
        return "trailer"
    if t.get("tourism") == "caravan_site" or (t.get("tourism") == "camp_site" and (
            t.get("caravans") == "yes" or t.get("static_caravans") in ("yes", "only"))):
        return "caravan_site"
    if layer == "land_use" and (cls == "construction" and t.get("landuse", "construction") == "construction"):
        return "construction"
    return None


def _rings(feats, frame, i):
    """(pts (n, 2), ring offsets (k + 1)) of polygon i's rings in frame (every part's), or None."""
    if int(feats.kind[i]) != mapgeom.POLYGON:
        return None
    pts, off = [], [0]
    for part in feats.parts(int(i)):
        for ring in part:
            ring = np.asarray(ring, np.float64)
            if len(ring) < 3:
                continue
            e, n = mapgeom.enu_xy(feats, frame, ring)
            xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
            if len(xy) >= 2 and np.allclose(xy[0], xy[-1]):
                xy = xy[:-1]
            if len(xy) < 3:
                continue
            pts.append(xy)
            off.append(off[-1] + len(xy))
    if not pts:
        return None
    return np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64)


def _area(pts, off):
    a = 0.0
    for k in range(len(off) - 1):
        r = pts[off[k]:off[k + 1]]
        x, y = r[:, 0], r[:, 1]
        a += (float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) * 0.5
    return abs(a)


def _layers(md, keys, name):
    """[feats] of the cells' layers (each cell once)."""
    out = []
    for key in keys:
        try:
            feats = md.cell(key, layers=(name,), kind="demand").layers.get(name)
        except Exception:
            continue
        if feats is not None and len(getattr(feats, "kind", ())):
            out.append(feats)
    return out


def _places(frame, md, keys):
    """{kind: [(fid, pts, off, box (4,), tags)]} of the places in the cells (an unclipped layer's feature once; a clipped
    land use's pieces each; a map feature in two layers from the infrastructure, whole)."""
    import overture
    got = {}
    taken = set()                       # (the source records of the infrastructure's places)
    for layer in ("infrastructure", "land_use", "water"):
        clipped = bool(overture.LAYERS.get(layer, {}).get("clip"))
        seen = set()
        for feats in _layers(md, keys, layer):
            memo = feats.__dict__.setdefault("_pl_places", {})
            fkey = (frame.lat0, frame.lon0, frame.h0)
            mine = memo.get(fkey)
            if mine is None:
                mine = []
                ids = getattr(feats, "ids", None)
                src = feats.attrs.get("src_record")
                for i in range(len(feats.kind)):
                    if int(feats.kind[i]) != mapgeom.POLYGON or not _maybe(feats, i):
                        continue
                    kind = kind_of(feats, i, layer)
                    if kind is None:
                        continue
                    r = _rings(feats, frame, i)
                    if r is None:
                        continue
                    fid = str(ids[i]) if ids is not None else f"{layer}:{i}"
                    rec = str(src[i]) if src is not None and i < len(src) else ""
                    pts, off = r
                    box = np.array([pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()])
                    mine.append((kind, fid, rec, pts, off, box, _tags(feats, i)))
                if len(memo) >= 2:
                    memo.pop(next(iter(memo)), None)
                memo[fkey] = mine
            for kind, fid, rec, pts, off, box, tags in mine:
                if not clipped:
                    if fid in seen:
                        continue
                    seen.add(fid)
                if layer == "infrastructure":
                    if rec:
                        taken.add(rec)
                elif rec and rec in taken:
                    continue
                got.setdefault(kind, []).append((fid, pts, off, box, tags))
    return got


def _piers(frame, md, keys, near_box):
    """[(id#ring, xy (k, 2), half width)] of the pier lines and outlines (infrastructure class pier) meeting near_box."""
    out, seen = [], set()
    for feats in _layers(md, keys, "infrastructure"):
        memo = feats.__dict__.setdefault("_pl_piers", {})
        fkey = (frame.lat0, frame.lon0, frame.h0)
        mine = memo.get(fkey)
        if mine is None:
            mine = []
            cls = feats.attrs.get("class")
            ids = getattr(feats, "ids", None)
            if cls is not None:
                for i in np.flatnonzero(np.asarray(cls).astype(str) == "pier").tolist():
                    kind = int(feats.kind[i])
                    fid = str(ids[i]) if ids is not None else str(i)
                    rk = 0
                    for part in feats.parts(int(i)):
                        for ring in part:
                            ring = np.asarray(ring, np.float64)
                            rk += 1
                            if len(ring) < 2:
                                continue
                            e, n = mapgeom.enu_xy(feats, frame, ring)
                            xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
                            if kind == mapgeom.POLYGON and not np.allclose(xy[0], xy[-1]):
                                xy = np.vstack([xy, xy[:1]])
                            b = np.array([xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()])
                            mine.append((f"{fid}#{rk}", xy, PIER_HW if kind == mapgeom.LINE else 0.0, b))
            if len(memo) >= 2:
                memo.pop(next(iter(memo)), None)
            memo[fkey] = mine
        for fid, xy, hw, b in mine:
            if fid in seen or not _meets(near_box, b, 30.0):
                continue
            out.append((fid, xy, hw))
        seen.update(f for f, _x, _h, _b in mine)
    return out


def _meets(box, b, pad=0.0):
    return not (b[2] < box[0] - pad or b[0] >= box[2] + pad or b[3] < box[1] - pad or b[1] >= box[3] + pad)


def _segments(roads, extra=0.0, rails=()):
    """(m, 5) [ax, ay, bx, by, half width + extra] of the carriageways (roads.build_tile's roads_out rows: (fid, spec,
    centreline (k, 3), lon)) and of rails ((k, 2+) lines, half width 2.5)."""
    rows = []
    for item in roads or ():
        try:
            pts = np.asarray(item[2], np.float64)
            hw = float(getattr(item[1], "width", 0.0) or 0.0) * 0.5
        except Exception:
            continue
        if len(pts) < 2:
            continue
        rows.append(np.column_stack([pts[:-1, :2], pts[1:, :2], np.full(len(pts) - 1, hw + extra)]))
    for line in rails or ():
        line = np.asarray(line, np.float64)
        if len(line) >= 2:
            rows.append(np.column_stack([line[:-1, :2], line[1:, :2], np.full(len(line) - 1, 2.5 + extra)]))
    if not rows:
        return np.zeros((0, 5))
    return np.ascontiguousarray(np.concatenate(rows))


def _ring_edges(pts, off, hw):
    """(m, 5) segments of rings' edges, each with half width hw."""
    rows = []
    for k in range(len(off) - 1):
        r = pts[off[k]:off[k + 1]]
        if len(r) < 2:
            continue
        rows.append(np.column_stack([r, np.roll(r, -1, axis=0), np.full(len(r), hw)]))
    return np.concatenate(rows) if rows else np.zeros((0, 5))


def _poly_set(polys):
    """(pts, off) of a list of (pts, off) polygons, each ring its own."""
    pts, off = [], [0]
    for p, o in polys:
        for k in range(len(o) - 1):
            r = p[o[k]:o[k + 1]]
            if len(r) >= 3:
                pts.append(r)
                off.append(off[-1] + len(r))
    if not pts:
        return np.zeros((0, 2)), np.zeros(1, np.int64)
    return np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64)


def _buildings(frame, md, keys, box, pad=40.0):
    """(pts, off) of the building footprints whose boxes meet box (padded)."""
    import worldprops
    xys, own, c0, c1, bb = worldprops._building_rings(frame, worldprops._layer(md, keys, "buildings"))
    pts, off = [], [0]
    if len(bb):
        near = np.flatnonzero(~((bb[:, 1] < box[0] - pad) | (bb[:, 0] > box[2] + pad)
                                | (bb[:, 3] < box[1] - pad) | (bb[:, 2] > box[3] + pad)))
        for k in near.tolist():
            r = xys[own[k]][c0[k]:c1[k]]
            if len(r) >= 3:
                pts.append(r)
                off.append(off[-1] + len(r))
    if not pts:
        return np.zeros((0, 2)), np.zeros(1, np.int64)
    return np.ascontiguousarray(np.concatenate(pts)), np.asarray(off, np.int64)


def _class_polys(frame, md, keys, box, layer, want, pad, memo_name):
    """(pts, off) of the outer rings of a layer's polygons near box for which want(feats, i) holds (each ring its own;
    kept on the layer per frame)."""
    polys = []
    for feats in _layers(md, keys, layer):
        memo = feats.__dict__.setdefault(memo_name, {})
        fkey = (frame.lat0, frame.lon0, frame.h0)
        mine = memo.get(fkey)
        if mine is None:
            mine = []
            for i in range(len(feats.kind)):
                if int(feats.kind[i]) != mapgeom.POLYGON or not want(feats, i):
                    continue
                r = _rings(feats, frame, i)
                if r is None:
                    continue
                pts, off = r
                pts0 = pts[off[0]:off[1]]        # (the outer ring)
                b = np.array([pts0[:, 0].min(), pts0[:, 1].min(), pts0[:, 0].max(), pts0[:, 1].max()])
                mine.append((pts0, np.array([0, len(pts0)], np.int64), b))
            if len(memo) >= 2:
                memo.pop(next(iter(memo)), None)
            memo[fkey] = mine
        polys.extend((p, o) for p, o, b in mine if _meets(box, b, pad))
    return _poly_set(polys)


def _is_water(feats, i):
    tags = feats.attrs.get("tags")
    return _cls(feats, i) not in ("swimming_pool", "reflecting_pool", "fountain") \
        and (tags is None or "swimming_pool" not in str(tags[i]))


def _is_parking(feats, i):
    return _cls(feats, i) in ("parking", "parking_space")


def _water(frame, md, keys, box, pad=200.0):
    """(pts, off) of the water polygons near box (each outer ring its own: an island's ring is not water then, the few
    cranes and boats it might misplace are left to the other tests)."""
    return _class_polys(frame, md, keys, box, "water", _is_water, pad, "_pl_water")


def _parking(frame, md, keys, box, pad=20.0):
    """(pts, off) of the mapped car parks near box (a port's car terminal is no container stack)."""
    return _class_polys(frame, md, keys, box, "infrastructure", _is_parking, pad, "_pl_parking")


def _z(frame, elev, x, y):
    """Ground z (frame) at points."""
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    if not len(x):
        return np.zeros(0)
    la, lo, _ = frame.enu_to_geodetic(x, y, np.zeros(len(x)))
    _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    return np.asarray(z, np.float64)


def _z_low(frame, elev, xy, yaw_deg, half):
    """The lowest ground z under each item's centre and its two ends (half m along its yaw)."""
    if not len(xy):
        return np.zeros(0)
    t = np.radians(np.asarray(yaw_deg, np.float64))
    ux, uy = dnp.cos(t), dnp.sin(t)
    xs = np.concatenate([xy[:, 0], xy[:, 0] + ux * half, xy[:, 0] - ux * half])
    ys = np.concatenate([xy[:, 1], xy[:, 1] + uy * half, xy[:, 1] - uy * half])
    return _z(frame, elev, xs, ys).reshape(3, len(xy)).min(axis=0)


def _offset(yaw_deg, ox, oy):
    """World offset of an item's local (ox, oy) at yaw (trees.lua: local +x toward yaw)."""
    t = math.radians(yaw_deg)
    c, s = dmath.cos(t), dmath.sin(t)
    return ox * c - oy * s, ox * s + oy * c


def _grid(pts, off, axis_deg, size, pitch, block, aisle, margin, seg, avoid, clear, box):
    fast.require(NATIVE, "places.area_grid")
    return fast._ef.area_grid(np.ascontiguousarray(pts, np.float64), np.ascontiguousarray(off, np.int64),
                              float(axis_deg), [float(v) for v in size], [float(v) for v in pitch],
                              [int(v) for v in block], [float(v) for v in aisle], float(margin),
                              np.ascontiguousarray(seg, np.float64).reshape(-1, 5),
                              np.ascontiguousarray(avoid[0], np.float64).reshape(-1, 2),
                              np.ascontiguousarray(avoid[1], np.int64), float(clear), [float(v) for v in box])


# --- the places ---

_PLANT_ARRAYS = {}          # plant id -> does a mapped array lie in it (decided over the plant's whole box)
PLANT_CELLS_MAX = 36


def _plant_has_arrays(frame, md, fid, pts, off):
    """Does a solar generator area (SOLAR_MIN_M2 or more) have its centre inside the plant? Read over the cells of the
    plant's whole box, so every tile decides the same; remembered per plant."""
    got = _PLANT_ARRAYS.get(fid)
    if got is not None:
        return got
    import mapcells
    from polygon import Path
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    try:
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()),
                                       mapcells.ZOOM, PLANT_CELLS_MAX)
    except ValueError:
        keys = []
    got = False
    if keys:
        gens = [p for p in _places(frame, md, keys).get("solar_gen", ()) if _area(p[1], p[2]) >= SOLAR_MIN_M2]
        if gens:
            cs = np.array([p[1].mean(axis=0) for p in gens])
            inside = np.zeros(len(cs), bool)
            for k in range(len(off) - 1):
                inside ^= np.asarray(Path(pts[off[k]:off[k + 1]]).contains_points(cs), bool)
            got = bool(inside.any())
    else:
        got = True              # (too big to read: left to its arrays, if any, or empty)
    if len(_PLANT_ARRAYS) > 4096:
        _PLANT_ARRAYS.clear()
    _PLANT_ARRAYS[fid] = got
    return got


def _on_building(pts, off, blds):
    """Is the area's centre inside a building footprint (blds: (pts, off), each ring its own)?"""
    if not len(blds[0]):
        return False
    from polygon import Path
    c = pts[off[0]:off[1]].mean(axis=0)
    bp, bo = blds
    for k in range(len(bo) - 1):
        r = bp[bo[k]:bo[k + 1]]
        if r[:, 0].min() <= c[0] <= r[:, 0].max() and r[:, 1].min() <= c[1] <= r[:, 1].max():
            if bool(Path(r).contains_points(c[None])[0]):
                return True
    return False


def solar_rows(frame, places, box, elev, seg, blds, lat, md=None):
    """Panel tables of the solar farms meeting box: the mapped arrays (generator areas), and the plants with none
    mapped inside them (an outline round mapped arrays holds service roads, substations and bare land as well:
    Topaz Solar Farm's 22 km² outline round its arrays, 2026-11-10)."""
    gens = [p for p in places.get("solar_gen", ()) if _area(p[1], p[2]) >= SOLAR_MIN_M2]
    plants = [p for p in places.get("solar_plant", ()) if _area(p[1], p[2]) >= SOLAR_MIN_M2]
    if not gens and not plants:
        return []
    south = lat < 0.0
    axis = 0.0 if south else 180.0      # local +y (the face) toward the equator
    seg_r = seg.copy()
    seg_r[:, 4] += SOLAR_ROAD_M
    out = []
    for fid, pts, off, b, tags in gens:
        if not _meets(box, b) or _on_building(pts, off, blds):
            continue
        got = _grid(pts, off, axis, SOLAR_SIZE, SOLAR_PITCH, SOLAR_BLOCK, SOLAR_AISLE, SOLAR_MARGIN, seg_r, blds,
                    SOLAR_BLD_M, box)
        if len(got) == 0:
            # a narrow area (one mapped row of tables, often drawn ~2.4 m wide: the Senftenberg solar complex's
            # rows): along its own long side, facing the equator, only its middle line inside
            got = _grid(pts, off, float("nan"), (SOLAR_SIZE[0], 0.5), SOLAR_PITCH, (0, 0), (0.0, 0.0), 0.0, seg_r,
                        blds, SOLAR_BLD_M, box)
            if len(got):
                v = -dmath.sin(math.radians(got[0, 2]))       # (local +y's north component at this yaw: cos(yaw+90))
                if (v > 0.0) != south:
                    got[:, 2] = (got[:, 2] + 180.0) % 360.0
        out.append((fid, got))
    for fid, pts, off, b, tags in plants:
        if not _meets(box, b) or md is None or _plant_has_arrays(frame, md, fid, pts, off):
            continue
        got = _grid(pts, off, axis, SOLAR_SIZE, SOLAR_PITCH, SOLAR_BLOCK, SOLAR_AISLE, SOLAR_MARGIN, seg_r, blds,
                    SOLAR_BLD_M, box)
        out.append((fid, got))
    rows = []
    for fid, got in out:
        if not len(got):
            continue
        z = _z_low(frame, elev, got[:, :2], got[:, 2], SOLAR_SIZE[0] / 2 - 1.0)
        for (x, y, yaw, i, j), zz in zip(got.tolist(), z.tolist()):
            rank = (SOLAR_ODD_RANK if int(j) % 2 else 0.0) + 0.04 * _h01(fid, int(i), int(j))
            rows.append([G_SOLAR, round(x, 2), round(y, 2), round(zz, 2), round(yaw, 1), 1.0, SOLAR_H, round(rank, 4)])
    return rows


CRANE_APART_M = 45.0            # m: a crane this near another one (two rings, two terminals) is left out


def _quay_axis(spots):
    """The quay's direction (degrees, mod 180) from cranes' yaws toward the water, or None."""
    if not len(spots):
        return None
    a = np.radians(spots[:, 2] + 90.0) * 2.0
    c, s_ = float(dnp.cos(a).mean()), float(dnp.sin(a).mean())
    if math.hypot(c, s_) < 0.3:
        return None
    return math.degrees(dmath.atan2(s_, c) / 2.0) % 180.0


def terminal_rows(frame, places, box, elev, seg, blds, water, parking=None):
    """Container stacks and quay cranes of the container terminals meeting box: the stacks in blocks along the quay (the
    terminal's water edge; without one, along its outline's long side), off its buildings, roads, car parks and the
    quay's apron; the cranes along the quay."""
    terms = [p for p in places.get("terminal", ()) if _area(p[1], p[2]) >= CONTAINER_MIN_M2]
    rows = []
    if not terms:
        return rows
    seg_r = seg.copy()
    seg_r[:, 4] += STACK_ROAD_M
    apron = _ring_edges(water[0], water[1], STACK_APRON_M) if len(water[0]) else np.zeros((0, 5))
    seg_all = np.concatenate([seg_r, apron]) if len(apron) else seg_r
    avoid = _poly_set([blds, parking]) if parking is not None and len(parking[0]) else blds
    placed = []
    for fid, pts, off, b, tags in terms:
        if not _meets(box, b, 60.0):
            continue
        spots = np.zeros((0, 3))
        if len(water[0]):
            fast.require(NATIVE, "places.edge_spots")
            spots = fast._ef.edge_spots(pts, off, np.ascontiguousarray(water[0]), np.ascontiguousarray(water[1]),
                                        CRANE_SPACING, CRANE_PROBE, CRANE_INSET, [-1e12, -1e12, 1e12, 1e12])
        axis = _quay_axis(spots)
        got = _grid(pts, off, float("nan") if axis is None else axis, STACK_SIZE, STACK_PITCH, STACK_BLOCK,
                    STACK_AISLE, STACK_MARGIN, seg_all, avoid, STACK_BLD_M, box)
        if len(got):
            z = _z_low(frame, elev, got[:, :2], got[:, 2], STACK_SIZE[0] / 2 - 0.5)
            for (x, y, yaw, i, j), zz in zip(got.tolist(), z.tolist()):
                u = _h01(fid, int(i), int(j), "h")
                high = next(n for share, n in STACK_LEVELS if u < share)
                # (the box's length is its local y: the grid's axis is its +y, so its yaw is a quarter turn less)
                yaw_b = (yaw - 90.0) % 360.0
                for k in range(high):
                    tone = _h01(fid, int(i), int(j), k, "tone")
                    rank = STACK_RANK[0] + tone * (STACK_RANK[1] - STACK_RANK[0]) if k == 0 \
                        else 0.12 + 0.18 * tone
                    rows.append([G_STACK, round(x, 2), round(y, 2), round(zz + k * STACK_H, 2), round(yaw_b, 1), 1.0,
                                 STACK_H, round(rank, 4)])
        if _area(pts, off) >= CRANE_MIN_M2:
            for x, y, w in spots.tolist():
                placed.append((_h01(fid, round(x, 0), round(y, 0)), x, y, w))
    # the cranes: one of any two closer than CRANE_APART_M (the lower hash), each tile keeping its own
    placed.sort()
    kept = []
    for hsh, x, y, w in placed:
        if any(dnp.sq(x - a) + dnp.sq(y - b) < CRANE_APART_M * CRANE_APART_M for a, b in kept):
            continue
        kept.append((x, y))
        if not (box[0] <= x < box[2] and box[1] <= y < box[3]):
            continue
        zz = float(_z(frame, elev, [x], [y])[0])
        # local -y (the boom's long reach) toward the water: local +x a quarter turn on from it
        rows.append([G_CRANE, round(x, 2), round(y, 2), round(zz + CRANE_LIFT, 2), round((w + 90.0) % 360.0, 1),
                     1.0, CRANE_H, 0.0])
    return rows


def marina_rows(frame, places, box, elev, md, keys, water):
    """Boats along the piers of the marinas meeting box."""
    marinas = places.get("marina", ())
    if not marinas or not len(water[0]):
        return []
    near = [p for p in marinas if _meets(box, p[3], 30.0)]
    if not near:
        return []
    keep = _poly_set([(p[1], p[2]) for p in near])
    # the piers: infrastructure class pier lines and outlines near the tile (_piers adds 30 m: a boat of the tile only
    # meets boats of piers that near, and theirs a few more: BERTH_REACH_M round the box; the whole harbour's ~1,600
    # piers at Marina del Rey were ~80 ms a tile)
    mb = np.array([box[0] - BERTH_REACH_M, box[1] - BERTH_REACH_M, box[2] + BERTH_REACH_M, box[3] + BERTH_REACH_M])
    lines, hws, keyv = [], [], []
    for fid, xy, hw in _piers(frame, md, keys, mb):
        lines.append(xy)
        hws.append(hw)
        keyv.append(_h01(fid))
    if not lines:
        return []
    off = np.concatenate([[0], np.cumsum([len(x) for x in lines])]).astype(np.int64)
    seg = np.concatenate([np.column_stack([x[:-1], x[1:], np.full(len(x) - 1, h)]) for x, h in zip(lines, hws)])
    fast.require(NATIVE, "places.berths")
    got = fast._ef.berths(np.ascontiguousarray(np.concatenate(lines)), off, np.asarray(hws, np.float64),
                          np.asarray(keyv, np.float64), list(BOAT_SLOT), BOAT_GAP, BOAT_OFF,
                          np.ascontiguousarray(water[0]), np.ascontiguousarray(water[1]),
                          np.ascontiguousarray(keep[0]), np.ascontiguousarray(keep[1]), np.ascontiguousarray(seg),
                          BOAT_CLEAR, [float(v) for v in box])
    rows = []
    if not len(got):
        return rows
    z = _z(frame, elev, got[:, 0], got[:, 1])
    for (x, y, yaw, key), zz in zip(got.tolist(), z.tolist()):
        if _h01(key, "keep") >= BOAT_KEEP:
            continue
        group, length, beam, h = BOATS[int(_h01(key, "model") * len(BOATS)) % len(BOATS)]
        s = min(1.0, BOAT_SLOT[0] / length, BOAT_SLOT[1] / beam)
        flip = 180.0 if _h01(key, "bow") < 0.5 else 0.0
        # (the water's surface: the elevation there, never below the sea's)
        zz = max(zz, float(_z(frame, lambda la, lo: np.zeros(len(np.atleast_1d(la))), [x], [y])[0]))
        rows.append([group, round(x, 2), round(y, 2), round(zz - BOAT_SINK, 2), round((yaw + flip) % 360.0, 1), 1.0,
                     round(h * s, 3), round(0.3 * _h01(key, "r"), 4)])
    return rows


TRAILER_BUILT = 0.06            # a park whose buildings cover this share of it has its homes mapped: left to them
_PARK_BUILT = {}


def _park_built(frame, md, fid, pts, off):
    """Do the park's mapped buildings (centres inside it) cover TRAILER_BUILT of it? Read over the cells of the park's
    box (every tile decides the same); remembered per park. Most parks in the US have every home in the buildings
    (Microsoft's footprints: Mesa, Arizona's parks, 2026-11-10), others none."""
    got = _PARK_BUILT.get(fid)
    if got is not None:
        return got
    import mapcells
    from polygon import Path
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
    try:
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()),
                                       mapcells.ZOOM, PLANT_CELLS_MAX)
    except ValueError:
        keys = []
    got = True
    if keys:
        box = [pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()]
        bp, bo = _buildings(frame, md, keys, box, pad=0.0)
        covered = 0.0
        if len(bo) > 1:
            cs = np.array([bp[bo[k]:bo[k + 1]].mean(axis=0) for k in range(len(bo) - 1)])
            inside = np.zeros(len(cs), bool)
            for k in range(len(off) - 1):
                inside ^= np.asarray(Path(pts[off[k]:off[k + 1]]).contains_points(cs), bool)
            covered = sum(_area(bp[bo[k]:bo[k + 1]], np.array([0, bo[k + 1] - bo[k]])) for k in np.flatnonzero(inside))
        got = covered >= TRAILER_BUILT * _area(pts, off)
    if len(_PARK_BUILT) > 4096:
        _PARK_BUILT.clear()
    _PARK_BUILT[fid] = got
    return got


def trailer_rows(frame, places, box, elev, seg, blds, country, md=None):
    """Caravans in the trailer parks and caravan sites meeting box, where their homes are not mapped."""
    rows = []
    na = _country(frame, box, country) in NA
    seg_r = seg.copy()
    seg_r[:, 4] += TRAILER_ROAD_M
    for kind in ("trailer", "caravan_site"):
        scale = NA_HOME_SCALE if na and kind == "trailer" else 1.0
        size = (CARAVAN_LEN * scale, CARAVAN_W * scale)
        pitch = (size[1] + TRAILER_PITCH[0], size[0] + TRAILER_PITCH[1])
        for fid, pts, off, b, tags in places.get(kind, ()):
            if not _meets(box, b) or _area(pts, off) < TRAILER_MIN_M2:
                continue
            if md is None or _park_built(frame, md, fid, pts, off):
                continue
            # (the homes side by side along the area's long side, each its length across it)
            got = _grid(pts, off, float("nan"), (size[1], size[0]), pitch, (0, 0), (0.0, 0.0), 1.0, seg_r, blds,
                        TRAILER_BLD_M, box)
            if not len(got):
                continue
            z = _z_low(frame, elev, got[:, :2], got[:, 2] + 90.0, size[0] / 2 - 0.5)
            for (x, y, yaw, i, j), zz in zip(got.tolist(), z.tolist()):
                if kind == "caravan_site" and _h01(fid, int(i), int(j), "keep") >= CARAVAN_SITE_KEEP:
                    continue
                # the caravan's length (local y) across the grid's axis: local +x along it, either way round
                yaw_c = (yaw + (180.0 if _h01(fid, int(i), int(j), "turn") < 0.5 else 0.0)) % 360.0
                ox, oy = _offset(yaw_c, CARAVAN_C[0] * scale, CARAVAN_C[1] * scale)
                rows.append([G_CARAVAN, round(x - ox, 2), round(y - oy, 2), round(zz, 2), round(yaw_c, 1), 1.0,
                             round(CARAVAN_H * scale, 3), round(0.3 * _h01(fid, int(i), int(j), "r"), 4)])
    return rows


def construction_rows(frame, places, box, elev, seg):
    """Hoardings and tower cranes of the construction sites meeting box."""
    rows = []
    for fid, pts, off, b, tags in places.get("construction", ()):
        if not _meets(box, b, 40.0):
            continue
        area = _area(pts, off)
        if not (SITE_MIN_M2 <= area <= SITE_MAX_M2):
            continue
        fast.require(NATIVE, "places.outline_panels")
        got = fast._ef.outline_panels(pts, off, HOARD_LEN, np.ascontiguousarray(seg), HOARD_GATE_M,
                                      [float(v) for v in box])
        if len(got):
            z = _z_low(frame, elev, got[:, :2] - np.column_stack([dnp.cos(np.radians(got[:, 2])),
                                                                  dnp.sin(np.radians(got[:, 2]))]) * HOARD_LEN / 2,
                       got[:, 2], HOARD_LEN / 2)
            for (x, y, yaw), zz in zip(got.tolist(), z.tolist()):
                rows.append([G_HOARD, round(x, 2), round(y, 2), round(zz + HOARD_LIFT, 2), round(yaw, 1), 1.0,
                             HOARD_H, 0.0])
        if area >= TC_MIN_M2:
            n = min(TC_MAX, 1 + int((area - TC_MIN_M2) / TC_EVERY_M2))
            spots = _grid(pts, off, float("nan"), (8.0, 8.0), (40.0, 40.0), (0, 0), (0.0, 0.0), 6.0, seg, (
                np.zeros((0, 2)), np.zeros(1, np.int64)), 0.0, [-1e12, -1e12, 1e12, 1e12])
            if not len(spots):
                continue
            # the n spots spread over the site: hashed picks, the same in every tile
            order = sorted(range(len(spots)), key=lambda k: _h01(fid, int(spots[k, 3]), int(spots[k, 4]), "tc"))
            for k in order[:n]:
                x, y = float(spots[k, 0]), float(spots[k, 1])
                if not (box[0] <= x < box[2] and box[1] <= y < box[3]):
                    continue
                zz = float(_z(frame, elev, [x], [y])[0])
                yaw = 360.0 * _h01(fid, k, "jib")
                sc = TC_SCALE[0] + (TC_SCALE[1] - TC_SCALE[0]) * _h01(fid, k, "tc h")
                rows.append([G_TC, round(x, 2), round(y, 2), round(zz, 2), round(yaw, 1), 1.0, round(TC_H * sc, 3), 0.0])
    return rows


# --- mapped street furniture (OpenStreetMap's amenity=bench / waste_basket / post_box / bicycle_parking / recycling and
# emergency=fire_hydrant, Overture's infrastructure points): the stock models by region, standing where mapped (moved
# off a carriageway they were drawn on), turned to the nearest road. Where a tile has these mapped, furniture.place's
# guessed ones of the same kind give way (drop_inferred).
STREET_KINDS = {"bench": "bench", "waste_basket": "bin", "fire_hydrant": "hydrant", "post_box": "postbox",
                "bicycle_parking": "bikestand", "recycling": "recyclebin"}
STREET_MODELS = {   # kind: (group in North America, group elsewhere, height (None: STREET_H's), m off a carriageway's
                    # edge, yaw added to the direction toward the road (None: hashed))
    "bench": ("bench", "bench", 0.9, 0.8, 90.0),
    "bin": ("bin", "bin", 1.1, 0.5, None),
    "hydrant": ("hydrant", "hydrant_eu", None, 0.4, 0.0),
    "postbox": ("postbox_us", "postbox_eu", None, 0.5, 0.0),
    "bikestand": ("bikestand", "bikestand", 1.16, 1.0, 90.0),
    "recyclebin": ("recyclebin", "recyclebin", 1.48, 0.8, 90.0),
}
STREET_H = {"hydrant": 1.5, "hydrant_eu": 0.79, "postbox_us": 1.56, "postbox_eu": 1.60}
STREET_REACH_M = 25.0           # a road this near turns the item toward it
SIDEWALK_M = 6.0                # an item this near a carriageway's edge stands on the sidewalk (CURB_H up)
CURB_H = 0.15
BIKES_PER_STAND = 6             # a stand (4.2 m of hoops) per this many bikes of a mapped capacity ...
BIKE_STANDS_MAX = 4             # ... at most this many side by side
BIKE_STAND_LEN = 4.4
HYDRANT_HIDDEN = {"underground", "pipe", "wall", "dry_barrel_underground"}
INFERRED_DROP = {"hydrant": 60.0, "bench": 30.0, "bin": 30.0}   # m round a mapped one a guessed one gives way ...
INFERRED_TILE_MIN = 3           # ... and none at all in a tile with this many of its kind mapped


def street_kind(feats, i):
    """(kind, hidden) of an infrastructure point: kind one of STREET_MODELS' or None; hidden: mapped but not drawn
    (an underground hydrant, a post box in a wall, bicycle parking in a building: it still counts as mapped)."""
    kind = STREET_KINDS.get(_cls(feats, i))
    if kind is None:
        return None, False
    t = _tags(feats, i)
    if t.get("location") in ("underground", "indoor", "roof") or t.get("indoor") == "yes" \
            or t.get("level", "0") not in ("0", ""):
        return kind, True
    if kind == "hydrant" and (t.get("fire_hydrant:type") in HYDRANT_HIDDEN
                              or t.get("fire_hydrant:position") == "wall"):
        return kind, True
    if kind == "postbox" and t.get("post_box:type") in ("wall", "lamp"):
        return kind, True
    if kind == "bikestand" and t.get("bicycle_parking") in ("building", "shed", "lockers", "floor", "ground_slots",
                                                            "underground", "garage"):
        return kind, True
    if kind == "recyclebin" and t.get("recycling_type") == "centre":
        return kind, True
    return kind, False


def _street_points(frame, md, keys, box):
    """[(kind, hidden, fid, x, y, tags)] of the mapped street furniture with its point in box (a small polygon's
    centre)."""
    out, seen = [], set()
    for feats in _layers(md, keys, "infrastructure"):
        memo = feats.__dict__.setdefault("_pl_street", {})
        fkey = (frame.lat0, frame.lon0, frame.h0)
        mine = memo.get(fkey)
        if mine is None:
            mine = []
            cls = feats.attrs.get("class")
            ids = getattr(feats, "ids", None)
            if cls is not None:
                want = np.isin(np.asarray(cls).astype(str), list(STREET_KINDS))
                for i in np.flatnonzero(want).tolist():
                    kind, hidden = street_kind(feats, i)
                    if kind is None or int(feats.kind[i]) not in (mapgeom.POINT, mapgeom.POLYGON):
                        continue
                    parts = feats.parts(int(i))
                    if not parts or not parts[0] or not len(parts[0][0]):
                        continue
                    e, n = mapgeom.enu_xy(feats, frame, np.asarray(parts[0][0], np.float64))
                    x, y = float(np.mean(e)), float(np.mean(n))
                    fid = str(ids[i]) if ids is not None else f"{x:.1f},{y:.1f}"
                    mine.append((kind, hidden, fid, x, y, _tags(feats, i)))
            if len(memo) >= 2:
                memo.pop(next(iter(memo)), None)
            memo[fkey] = mine
        for item in mine:
            if item[2] in seen or not (box[0] <= item[3] < box[2] and box[1] <= item[4] < box[3]):
                continue
            seen.add(item[2])
            out.append(item)
    return out


def street_rows(frame, box, md, elev, roads=(), country=None, keys=None, mapped_out=None):
    """Forest rows of the mapped street furniture in box. mapped_out: a dict filled with kind -> [(x, y)] of every
    mapped one (hidden ones too), for drop_inferred."""
    if md is None or not ENABLED:
        return []
    if keys is None:
        keys = _keys_of(frame, box)
        if not keys:
            return []
    pts = _street_points(frame, md, keys, box)
    if mapped_out is not None:
        for kind, hidden, fid, x, y, tags in pts:
            mapped_out.setdefault(kind, []).append((x, y))
    shown = [p for p in pts if not p[1]]
    if not shown:
        return []
    na = _country(frame, box, country) in NA
    seg = _segments(roads)
    fast.require(NATIVE, "places.street_points")
    rows = []
    for kind, (g_na, g_eu, h, pad, yaw_add) in STREET_MODELS.items():
        mine = [p for p in shown if p[0] == kind]
        if not mine:
            continue
        xy = np.array([[p[3], p[4]] for p in mine], np.float64)
        got = fast._ef.street_points(xy, seg, float(pad), STREET_REACH_M)
        group = g_na if na else g_eu
        height = h if h is not None else STREET_H[group]
        z = _z(frame, elev, got[:, 0], got[:, 1])
        for (_k, _h, fid, _x, _y, tags), (x, y, toward, edge), zz in zip(mine, got.tolist(), z.tolist()):
            if toward == toward and yaw_add is not None:
                yaw = (toward + yaw_add) % 360.0
            else:
                yaw = 360.0 * _h01(fid, "yaw")
            lift = CURB_H if edge <= SIDEWALK_M else 0.0
            n = 1
            if kind == "bikestand":
                try:
                    n = int(max(1, min(BIKE_STANDS_MAX, -(-int(float(tags.get("capacity", "0"))) // BIKES_PER_STAND))))
                except ValueError:
                    n = 1
            for k in range(n):
                # (several stands side by side along the stand's own length: local x)
                ox, oy = _offset(yaw, (k - (n - 1) / 2.0) * BIKE_STAND_LEN, 0.0)
                rows.append([group, round(x + ox, 2), round(y + oy, 2), round(zz + lift, 2), round(yaw, 1), 1.0,
                             height, round(0.3 * _h01(fid, k, "r"), 4)])
    return rows


def drop_inferred(props, mapped):
    """props without furniture.place's guessed hydrants, benches and bins where those are mapped: all of a kind when the
    tile has INFERRED_TILE_MIN of it mapped, else the ones within INFERRED_DROP of a mapped one. mapped: street_rows'
    mapped_out. Call before the mapped rows are added (they use the same groups)."""
    if not props or not mapped:
        return props
    groups = {k: np.asarray(v, np.float64).reshape(-1, 2) for k, v in mapped.items() if k in INFERRED_DROP}
    if not groups:
        return props
    out = []
    for r in props:
        pts = groups.get(r[0]) if isinstance(r, (list, tuple)) and r else None
        if pts is not None and len(pts):
            if len(pts) >= INFERRED_TILE_MIN:
                continue
            d2 = dnp.sq(pts[:, 0] - float(r[1])) + dnp.sq(pts[:, 1] - float(r[2]))
            if float(d2.min()) < dnp.sq(INFERRED_DROP[r[0]]):
                continue
        out.append(r)
    return out


# --- obstruction lights on tall buildings: West Coast USA's red marker light (a 1.29 m glowing ball round its origin,
# its material emissive: lit day and night) at the corners of a building's topmost part when its top is WARN_MIN_H or
# more over its base (Midtown's 100 m towers all lit were ~60 lights a tile, more than the real skyline shows: most
# carry them from ~150 m), one at the middle of a slim top (a spire)
G_WARN, WARN_H = "warnlight", 1.29
WARN_MIN_H = 150.0
WARN_INSET = 0.8                # m in from the roof's edge
WARN_MIN_AREA = 250.0           # m²: a smaller top gets one light at its middle
WARN_LIFT = 0.4                 # m the light's centre stands over the roof


def warning_rows(items, x0, y0):
    """Forest rows of the obstruction lights of a road tile's building items ((spec, gmin, gmax), buildings.
    road_tile_items: rings relative to the tile's origin x0, y0): a tall part under a taller one (its middle inside
    the other's footprint) has none."""
    import buildings
    from polygon import Path
    if not ENABLED:
        return []
    tall = []
    for spec, gmin, gmax in items or ():
        top = float(getattr(spec, "min_h", 0.0) or 0.0) + float(getattr(spec, "height", 0.0) or 0.0)
        if not (top >= WARN_MIN_H):
            continue
        ring = np.ascontiguousarray(np.asarray(spec.ring, np.float64)[:, :2])
        if len(ring) >= 3:
            tall.append((float(buildings.base_z(spec, gmin, gmax)) + top, ring))
    rows = []
    for k, (ztop, ring) in enumerate(tall):
        c = ring.mean(axis=0)
        if any(z2 > ztop + 0.5 and r2[:, 0].min() <= c[0] <= r2[:, 0].max() and r2[:, 1].min() <= c[1] <= r2[:, 1].max()
               and bool(Path(r2).contains_points(c[None])[0]) for j, (z2, r2) in enumerate(tall) if j != k):
            continue
        fast.require(NATIVE, "places.roof_corners")
        for x, y in fast._ef.roof_corners(ring, WARN_INSET, WARN_MIN_AREA).tolist():
            rows.append([G_WARN, round(x + x0, 2), round(y + y0, 2), round(ztop + WARN_LIFT, 2), 0.0, 1.0, WARN_H,
                         0.0])
    return rows


# --- jet fans in long road tunnels: West Coast USA's tunnel fan housing (x 0.9, y 2.25 along its axis, z -1.07..0: hung
# under its origin) and its rotor (centred), FAN_SCALE up (a road tunnel's jet fan is ~1.4 m across, 3-5 m long), in
# pairs under the vault every FAN_GRID_M (where the tube crosses the frame's grid lines: every tile places the same)
G_FAN, FAN_H = "jetfan", 1.07
G_ROTOR, ROTOR_H = "jetfan_rotor", 0.73
FAN_SCALE = 1.5
FAN_GRID_M = 120.0
FAN_MIN_TUNNEL_M = 400.0        # a shorter tube is ventilated by its portals
FAN_PAIR_OFF = 1.8              # m either side of the tube's middle
FAN_HANG_M = 0.35               # m between the vault and the housing's top


def tunnel_fan_rows(chunks):
    """Forest rows of the jet fans in a road tile's tunnel tubes (roads' tunnel chunks: "tunnel", not "no_tube", their
    "tunnel_len" FAN_MIN_TUNNEL_M or more, a full tube's height; points "n" in the frame, at the floor)."""
    if not ENABLED:
        return []
    import roadmesh
    rows = []
    for ch in chunks or ():
        if not ch.get("tunnel") or ch.get("no_tube") or float(ch.get("tunnel_len") or 0.0) < FAN_MIN_TUNNEL_M:
            continue
        th = float(ch.get("tunnel_h") or roadmesh.TUNNEL_H)
        if th < roadmesh.TUNNEL_H - 0.05:
            continue                # (a low arch)
        pts = np.asarray(ch.get("n") or (), np.float64)
        if pts.ndim != 2 or len(pts) < 2 or pts.shape[1] < 3:
            continue
        hw = float(ch.get("w") or 0.0) / 2.0 + roadmesh.TUNNEL_SHOULDER
        off = min(FAN_PAIR_OFF, 0.35 * hw)
        wall = roadmesh.TUNNEL_WALL_H * th / roadmesh.TUNNEL_H
        vault = wall + math.sqrt(max(0.0, 1.0 - dnp.sq(off / hw))) * (th - wall)      # the vault's height over `off`
        fast.require(NATIVE, "places.grid_marks")
        marks = fast._ef.grid_marks(np.ascontiguousarray(pts[:, :3]), FAN_GRID_M)
        for x, y, z, tx, ty in marks.tolist():
            yaw = (math.degrees(dmath.atan2(ty, tx)) - 90.0) % 360.0      # (the housing's axis is its local y)
            ztop = z + vault - FAN_HANG_M
            for side in (-1.0, 1.0):
                px, py = x - ty * side * off, y + tx * side * off
                rows.append([G_FAN, round(px, 2), round(py, 2), round(ztop, 2), round(yaw, 1), 1.0,
                             round(FAN_H * FAN_SCALE, 3), 0.0])
                rows.append([G_ROTOR, round(px, 2), round(py, 2), round(ztop - FAN_H * FAN_SCALE / 2.0, 2),
                             round(yaw, 1), 1.0, round(ROTOR_H * FAN_SCALE, 3), 0.0])
    return rows


def _country(frame, box, country):
    """The tile's country: the caller's, else the bundled borders' at the tile's centre (countrybounds)."""
    if country:
        return str(country).upper()
    try:
        import countrybounds
        la, lo, _ = frame.enu_to_geodetic(np.array([(box[0] + box[2]) / 2.0]), np.array([(box[1] + box[3]) / 2.0]),
                                          np.zeros(1))
        return str(countrybounds.bundled().at(float(la[0]), float(lo[0])) or "").upper()
    except Exception:
        return ""


def _keys_of(frame, box):
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        return mapcells.cells_for_points(la, lo, pad_lat=0.0005, pad_lon=0.0005, max_cells=64)
    except (ValueError, OverflowError):
        return []


def tile_rows(frame, box, md, elev, roads=(), country=None, keys=None):
    """Forest rows of the places in a road tile's box (x0, y0, x1, y1) in frame. roads: build_tile's roads_out rows
    (the carriageways near the tile); country: ISO alpha-2 of the tile. md None: none."""
    if md is None or not ENABLED:
        return []
    if keys is None:
        keys = _keys_of(frame, box)
        if not keys:
            return []
    places = _places(frame, md, keys)
    if not places:
        return []
    lat = float(frame.lat0)
    seg = _segments(roads)
    rows = []
    need_b = any(k in places for k in ("solar_gen", "solar_plant", "terminal", "trailer", "caravan_site"))
    blds = _buildings(frame, md, keys, box) if need_b else (np.zeros((0, 2)), np.zeros(1, np.int64))
    need_w = "terminal" in places or "marina" in places
    water = _water(frame, md, keys, box) if need_w else (np.zeros((0, 2)), np.zeros(1, np.int64))
    parking = _parking(frame, md, keys, box) if "terminal" in places else None
    for name, fn in (("solar", lambda: solar_rows(frame, places, box, elev, seg, blds, lat, md)),
                     ("terminal", lambda: terminal_rows(frame, places, box, elev, seg, blds, water, parking)),
                     ("marina", lambda: marina_rows(frame, places, box, elev, md, keys, water)),
                     ("trailer", lambda: trailer_rows(frame, places, box, elev, seg, blds, country, md)),
                     ("construction", lambda: construction_rows(frame, places, box, elev, seg))):
        try:
            rows.extend(fn())
        except fast.NativeMissing:
            raise
        except Exception as ex:
            log.warning("places: %s: %s: %s", name, type(ex).__name__, ex)
    return rows
