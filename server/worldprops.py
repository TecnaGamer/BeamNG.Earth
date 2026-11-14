"""Stock props on a road tile (the user, 2026-10-29: cell towers, water towers, containers, blank
billboards, barns): Forest rows in furniture.place's form, [group, x, y, z, yaw, scale, height, rank],
drawn by trees.lua. Heights are the meshes' vertical size, so a row of that height stands at 1:1
(trees.pick scales height / catalog height).

Towers are Overture infrastructure points (or a polygon's centre). Containers fill an industrial /
works / farmyard polygon, lined up with its long side. A blank billboard is placed only where the
source tags say advertising=billboard. That tag is not filled in the pinned Overture release, so the
boards also come from OpenStreetMap (billboardsource.py). The mesh is West Coast's drag-strip board,
which carries the poster; its face looks along local +y. A barn replaces the
procedural one when the footprint is close to the stock mesh; the building id comes back for the
road tile to skip.

A feature is emitted by the tile that owns its centre, except containers: every tile that sees the
yard places the same grid and keeps the boxes inside its own square, so a yard on a boundary is not
doubled and not cut in half. Deterministic (hashed yaw, colour and which boxes stay).
"""
import json
import logging
import math
import os
import re

import numpy as np
import dmath
import dnp

log = logging.getLogger("earthd.worldprops")

import mapgeom

# Measured vertex bounds (Z up, metres). Lift is how far the mesh origin sits above the ground.
# The catalog height is the mesh's vertical size, and the row sends that height so the Forest scale is 1.
CELL_H, CELL_LIFT = 28.93, 0.02
# industrial_watertower: the legs rise out of concrete pads whose tops are at z=0.60. A centre pipe
# reaches -1.29; lifting by that pipe left the feet in the air. Dropping to the pad tops puts the
# visible feet on the ground and buries the pads.
WATER_H, WATER_LIFT = 26.16, -0.60
# A water tower with a mapped height (Overture's height, else OSM's height=; GitHub #887: the one model stood for the
# 45-60 m towers of big towns too) is the model scaled to it, within WATER_SCALE (whole: a taller tank is wider too).
WATER_SCALE = (0.5, 2.4)
CONT_H = 2.59
# dragbillboard: z 0..11.95, the poster centred near the origin and facing local +y.
BILL_H, BILL_LIFT = 11.95, 0.0
BILL_OX, BILL_OY = 0.02, 0.14          # the poster's centre, in the mesh's local axes
# farm_barn: the walls sit on z=0. A foundation reaches -0.45; lifting by it left the walls a few feet up.
BARN_H, BARN_LEN, BARN_LIFT = 8.41, 10.36, 0.0
BARN_SCALE = (0.7, 1.75)               # outside this the procedural barn fits better than a scaled mesh

CELL_CLASSES = {"communication_tower", "mobile_phone_tower", "radar"}
WATER_CLASSES = {"water_tower"}
YARD_CLASSES = {"industrial", "works"}
FARMYARD_CLASS = "farmyard"

CONT_ALONG_M, CONT_ACROSS_M = 16.0, 6.5
CONT_KEEP = 0.55
CONT_YARD_MAX = {"industrial": 8, "works": 8, "farmyard": 3}
CONT_AREA = {"industrial": (800.0, 80000.0), "works": (800.0, 80000.0), "farmyard": (400.0, 15000.0)}
CONT_TILE_MAX = 30
CONT_ROAD_M = 8.0
BILL_ROAD_M = 120.0
ROAD_MARGIN_M = 140.0


def tile_props(frame, tx, ty, md, elev, lamps=None):
    """(rows, barn ids) of road tile (tx, ty). barn ids are the footprints replaced by the stock barn
    (buildings.road_tile_items skip). elev(lat, lon) is the ground. md None, or a cell that cannot be
    read, yields nothing of that layer. lamps: a list that gets the rows of the mapped street lamps on and within
    LAMP_NEAR_M of the tile (_street_lamps; furniture.place's mapped_lamps draws the tile's own), None: none made."""
    import buildings
    import roads
    if md is None:
        return [], set()
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    keys = roads.cells_of_tile(frame, tx, ty)
    infra = _layer(md, keys, "infrastructure")
    land = _layer(md, keys, "land_use")
    blds = _layer(md, keys, "buildings")
    # (made only when a billboard or a yard asks for them: every building ring and road line of a Manhattan tile was
    # ~0.15 s of each of its road tiles, for the few with either, 2026-11-03)
    roads_xy = _Once(lambda: _polylines(frame, _layer(md, roads.cells_of_tile(frame, tx, ty, margin=ROAD_MARGIN_M),
                                                      "roads")))
    bld_rings = _Once(lambda: _building_rings(frame, blds))
    placed, skip, boxes = [], set(), []
    mapped = [] if lamps is not None else None
    for feats, rows in infra:
        if mapped is not None:
            _street_lamps(frame, feats, rows, (x0 - LAMP_NEAR_M, y0 - LAMP_NEAR_M, x1 + LAMP_NEAR_M, y1 + LAMP_NEAR_M),
                          mapped)
        _towers(frame, feats, rows, (x0, y0, x1, y1), placed)
        _billboards(frame, feats, rows, (x0, y0, x1, y1), roads_xy, placed)
    for feats, rows in land:
        _containers(frame, feats, rows, (x0, y0, x1, y1), bld_rings, roads_xy, boxes)
        _billboards(frame, feats, rows, (x0, y0, x1, y1), roads_xy, placed)
    osm = getattr(md, "billboard_osm", None)
    if osm is not None:
        try:
            for feats in osm.cells(keys) or ():
                if feats is not None and len(feats):
                    _billboards(frame, feats, None, (x0, y0, x1, y1), roads_xy, placed)
        except Exception as ex:
            log.warning("billboards: %s: %s", type(ex).__name__, ex)
    for feats, rows in blds:
        _barns(frame, feats, rows, (x0, y0, x1, y1), placed, skip)
    boxes.sort(key=lambda b: (b["x"], b["y"]))
    placed.extend(boxes[:CONT_TILE_MAX])
    if mapped:
        reach = LAMP_NEAR_M + LAMP_ROAD_M
        _aim_lamps(mapped, lambda: _lamp_roads(frame, _layer(md, roads.cells_of_tile(frame, tx, ty, margin=reach), "roads"),
                                               (x0 - reach, y0 - reach, x1 + reach, y1 + reach)))
        placed.extend(mapped)
    if not placed:
        return [], skip
    lat = np.array([p["lat"] for p in placed], np.float64)
    lon = np.array([p["lon"] for p in placed], np.float64)
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    z = np.asarray(z, np.float64)
    rows = []
    for p, zz in zip(placed, z):
        row = [p["group"], round(p["x"], 2), round(p["y"], 2), round(float(zz) + p["lift"], 2),
               round(p["yaw"], 1), 1.0, round(p["h"], 2), round(p["rank"], 4)]
        (lamps if "lamp_id" in p else rows).append(row)
    return rows, skip


# Mapped street lamps (OpenStreetMap highway=street_lamp nodes, everywhere; PR #981 by wyo-backroads): each at its
# mapped point, the stock post its lamp_mount names (the others the ordinary street post), the arm turned toward the
# nearest drivable road (earthfast lamp_road_yaw), the tag's direction where no road is within LAMP_ROAD_M. A road with
# mapped lamps along it guesses none (furniture.place). BNE_MAPPED_LAMPS=off: none (the guessed lamps only).
MAPPED_LAMPS = os.environ.get("BNE_MAPPED_LAMPS", "on").strip().lower() not in ("off", "0", "no", "false")
HIGH_MAST_H = 18.03             # Driver Experience s_light_pole_high_mast visible bounds: z=0..18.032 m
STRAIGHT_MAST_H = 12.25         # Driver Experience s_light_pole, authored cm converted to metres
DOUBLE_ARM_H = 13.76            # the stock pole_light_double (art/shapes/objects): -0.715..13.046 m; its foundation buried
STREET_LAMP_H = 9.0             # the ordinary post (furniture.G_LAMP's catalog height)
MAPPED_LAMP_MODELS = {
    "high_mast": ("streetlight_high_mast", HIGH_MAST_H),
    "straight_mast": ("streetlight_straight_mast", STRAIGHT_MAST_H),
    "double_arm": ("streetlight_double_arm", DOUBLE_ARM_H),
}
# lamp_road_yaw's kinds: an arm along the model's +x, along its -y (the straight mast), a double arm across the road
LAMP_ARM_KIND = {"streetlight_straight_mast": 1, "streetlight_double_arm": 2}
LAMP_ROAD_M = 80.0
LAMP_NEAR_M = 40.0              # m round the tile whose lamps also tell its roads they have mapped ones (furniture.place)
LAMP_ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified", "service",
                     "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
_OSM_NODE = re.compile(r"n(\d+)(?:@\S+)?")
_LAMP_OFF = ("disused", "abandoned", "demolished", "razed", "construction", "proposed")


def _street_lamps(frame, feats, rows, box, placed):
    """Add the mapped lamp nodes (OpenStreetMap nodes only, no interpolation along lit ways) in box to placed, as
    worldprops records with "lamp_id" and their arm's kind / layer for _aim_lamps."""
    cls = _cls(feats)
    if cls is not None and isinstance(feats, mapgeom.Features):
        # (the cell's lamp points and their places in the frame, once per layer and frame: a city cell has thousands
        # of lamps and other points; a tile looks at the few in its box)
        def make():
            at = np.flatnonzero((cls == "street_lamp") & (np.asarray(feats.kind) == mapgeom.POINT))
            xy = feats.xy_in(frame)
            first = feats.coord_off[feats.ring_off[feats.part_off[at]]] if len(at) else np.zeros(0, np.int64)
            return at, (xy[first] if len(at) else np.zeros((0, 2)))
        at, xy = _memo(feats, "lamp_pts", frame, make)
        keep = (xy[:, 0] >= box[0]) & (xy[:, 0] < box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] < box[3])
        if rows is not None:
            keep &= np.isin(at, np.asarray(rows, np.int64))
        at = at[keep]
    else:
        at = np.arange(len(feats.kind)) if rows is None else np.asarray(rows, np.int64)
        if cls is not None:
            at = at[cls[at] == "street_lamp"]
    src = feats.attrs.get("src_record")
    seen = {p["lamp_id"] for p in placed}
    for i in at.tolist():
        if int(feats.kind[i]) != mapgeom.POINT:
            continue
        match = _OSM_NODE.fullmatch(str(src[i])) if src is not None else None
        if match is None:
            continue
        fid = "osm:node:" + match[1]
        tags = _tags_of(feats, i)
        if cls is None and tags.get("highway") != "street_lamp":
            continue
        if str(tags.get("location", "")).lower() in {"underground", "underwater", "indoor", "tunnel"} or any(
                str(tags.get(k, "no")).lower() not in {"no", "false", "0", "", "none"} for k in _LAMP_OFF):
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None:
            continue
        x, y = _centre(xy)
        if not _inside(x, y, box) or fid in seen:
            continue
        seen.add(fid)
        la, lo = _ll(frame, x, y)
        group, height = MAPPED_LAMP_MODELS.get(tags.get("lamp_mount"), ("streetlight", STREET_LAMP_H))
        yaw = 0.
        try:
            bearing = float(tags.get("direction", ""))
            if math.isfinite(bearing):
                # OSM bearings: north=0, east=90; model arms: +X, except the straight mast's -Y.
                yaw = ((180. if group == "streetlight_straight_mast" else 90.) - bearing) % 360.
        except (TypeError, ValueError):
            pass
        try:
            layer = float(tags["layer"]) if "layer" in tags else math.nan
        except (TypeError, ValueError):
            layer = math.nan
        placed.append({"group": group, "x": x, "y": y, "lat": la, "lon": lo, "lift": 0., "h": height, "yaw": yaw,
                       "rank": 0., "lamp_id": fid, "arm": LAMP_ARM_KIND.get(group, 0), "layer": layer})


def _aim_lamps(lamps, lines_fn):
    """Turn each mapped lamp's arm toward its nearest drivable road (lines_fn(): _lamp_roads' arrays, made only when
    a lamp has an arm: a high mast has none); one with no road within LAMP_ROAD_M keeps its tag's direction."""
    aim = [p for p in lamps if p["group"] != "streetlight_high_mast"]
    if not aim:
        return
    line_pts, off, level = lines_fn()
    if len(off) < 2:
        return
    import fast
    fast.require(199, "mapped street lamps")
    yaw = fast._ef.lamp_road_yaw(np.array([[p["x"], p["y"]] for p in aim], np.float64),
                                 np.array([p["layer"] for p in aim], np.float64),
                                 np.array([p["arm"] for p in aim], np.int64), line_pts, off, level, LAMP_ROAD_M)
    for p, y in zip(aim, yaw.tolist()):
        if math.isfinite(y):
            p["yaw"] = y


def _lamp_roads(frame, layers, near=None):
    """(points (m, 2), offsets (k + 1), layer (k,) NaN: none) of the drivable centrelines (no footways, no tunnels)
    whose boxes meet near (x0, y0, x1, y1; None: all), in a stable order (their ids, then their ends)."""
    result = []
    for feats, rows in layers:
        def make():
            lines, at = _memo(feats, "lines", frame, lambda: _layer_lines(frame, feats))     # (shared: _polylines')
            cls = _cls(feats)
            table = feats.rules.get("layer") if hasattr(feats, "rules") else None
            found, indices, boxes = [], [], []
            for line, i in zip(lines, at):
                tags = _tags_of(feats, i)
                kind = tags.get("highway") or (str(cls[i]) if cls is not None else "")
                if kind not in LAMP_ROAD_CLASSES or tags.get("tunnel") in ("yes", "building_passage"):
                    continue
                level = tags.get("layer")
                if level is None and table is not None:
                    take = table.rows(i)
                    if len(take):
                        level = table.value[take[0]]
                try:
                    level = float(level) if level is not None else math.nan
                except (ValueError, TypeError):
                    level = math.nan
                found.append((str(feats.ids[i]), line, level))
                indices.append(i)
                boxes.append((*line.min(0), *line.max(0)))
            return found, np.asarray(indices, np.int64), np.asarray(boxes, np.float64).reshape(-1, 4)
        found, indices, boxes = _memo(feats, "lamp_roads", frame, make)
        pick = _picked(feats, rows, indices)
        keep = np.ones(len(found), bool) if pick is None else np.asarray(pick, bool)
        if near is not None and len(found):
            keep &= ((boxes[:, 2] >= near[0]) & (boxes[:, 0] <= near[2]) & (boxes[:, 3] >= near[1])
                     & (boxes[:, 1] <= near[3]))
        result.extend(found[k] for k in np.flatnonzero(keep).tolist())
    result.sort(key=lambda r: (r[0], tuple(r[1][0]), tuple(r[1][-1])))
    off = np.zeros(len(result) + 1, np.int64)
    if result:
        off[1:] = np.cumsum([len(r[1]) for r in result])
    pts = np.concatenate([r[1] for r in result]) if result else np.zeros((0, 2))
    return (np.ascontiguousarray(pts, np.float64), off, np.array([r[2] for r in result], np.float64))


class _Once:
    """fn() made on the first call, the same value after."""
    __slots__ = ("fn", "got")

    def __init__(self, fn):
        self.fn, self.got = fn, None

    def __call__(self):
        if self.fn is not None:
            self.got, self.fn = self.fn(), None
        return self.got


def _memo(feats, name, frame, make):
    """make() kept on a layer (per frame when frame is given, two at a time like mapgeom.Features.xy_in): the decoded
    cells serve many tiles, and each tile went through every feature of its cells for the few it owns (rings, centres
    and yards: ~1.8 s over 16 Penn Station tiles, 2026-11-03). Shared, not to be changed."""
    memo = feats.__dict__.setdefault("_wp_memo", {})
    if frame is None:
        got = memo.get(name)
        if got is None:
            got = memo[name] = make()
        return got
    per = memo.setdefault(name, {})
    key = (frame.lat0, frame.lon0, frame.h0)
    got = per.get(key)
    if got is None:
        got = make()
        if len(per) >= 2:
            per.pop(next(iter(per), None), None)
        per[key] = got
    return got


def _layer(md, keys, name):
    """[(feats, rows)] of the cells' layers: rows the ones not in an earlier cell (ascending), None for all."""
    out, seen = [], []
    for key in keys:
        try:
            feats = md.cell(key, layers=(name,), kind="demand").layers.get(name)
        except Exception:
            continue
        if feats is None or len(getattr(feats, "kind", ())) == 0:
            continue
        ids = getattr(feats, "ids", None)
        if ids is None:
            out.append((feats, None))
            continue
        # (each id once over the cells, the first cell's: as a numpy test, not a set per row: a Manhattan tile's
        # buildings were ~0.04 s of it, 2026-11-03)
        ids = np.asarray([str(x) for x in ids.tolist()]) if not isinstance(ids, np.ndarray) or ids.dtype.kind != "U" \
            else ids
        n = len(feats.kind)
        ids = ids[:n]
        fresh = ~np.isin(ids, np.concatenate(seen)) if seen else np.ones(n, bool)
        keep = np.flatnonzero(fresh)
        seen.append(ids[keep])
        if len(keep) == n or (len(keep) and not isinstance(feats, mapgeom.Features)):
            out.append((feats, None))
        elif len(keep):
            # (the cell's own layer and its rows, not a copy of them: the copy was made again each tile, and the rings,
            # centres and yards kept on the layer serve every tile, 2026-11-03)
            out.append((feats, keep))
    return out


def _picked(feats, rows, at):
    """Which entries (their rows `at`) are among rows; None: all."""
    if rows is None:
        return None
    sel = np.zeros(len(feats.kind), bool)
    sel[rows] = True
    return sel[np.asarray(at, np.int64)]


def _cls(feats):
    cls = feats.attrs.get("class")
    return None if cls is None else _memo(feats, "cls", None, lambda: np.asarray(cls).astype(str))


def _tags_of(feats, i):
    tags = feats.attrs.get("tags")
    if tags is None or i >= len(tags):
        return {}
    val = tags[i]
    if isinstance(val, dict):
        return val
    if val is None or (isinstance(val, float) and not np.isfinite(val)):
        return {}
    memo = feats.__dict__.setdefault("_wp_tags", {})     # (parsed once per layer, shared: not to be changed)
    got = memo.get(i)
    if got is not None:
        return got
    text = str(val).strip()
    got = {}
    if text and text != "nan":
        try:
            got = json.loads(text)
        except Exception:
            got = {}
    got = memo[i] = got if isinstance(got, dict) else {}
    return got


def _advertising(tags):
    for k, v in tags.items():
        if str(k).strip().lower() == "advertising" and str(v).strip().lower() == "billboard":
            return True
    return False


def _hash(fid, *more):
    import buildings
    return buildings._hash01(str(fid), *more)


def _ring_xy(feats, i, frame):
    parts = feats.parts(int(i))
    if not parts or not parts[0] or len(parts[0][0]) == 0:
        return None
    ring = np.asarray(parts[0][0], np.float64)
    e, n = mapgeom.enu_xy(feats, frame, ring)
    xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
    if len(xy) >= 2 and np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    return xy if len(xy) else None


def _centre(xy):
    return float(xy[:, 0].mean()), float(xy[:, 1].mean())


def _inside(x, y, box):
    x0, y0, x1, y1 = box
    return x0 <= x < x1 and y0 <= y < y1


def _ll(frame, x, y):
    la, lo, _ = frame.enu_to_geodetic(np.array([x], np.float64), np.array([y], np.float64), np.zeros(1))
    return float(la[0]), float(lo[0])


def water_scale(feats, i):
    """The water tower model's scale for feature i: its mapped height (Overture's height attribute, else the height=
    tag, metres) over WATER_H within WATER_SCALE, 1 without one."""
    h = float("nan")
    hs = feats.attrs.get("height")
    if hs is not None and i < len(hs):
        try:
            h = float(hs[i])
        except (TypeError, ValueError):
            pass
    if not h == h:
        try:
            h = float(str(_tags_of(feats, i).get("height", "")).replace("m", "").strip())
        except ValueError:
            pass
    if not (h == h and h > 0):
        return 1.0
    return float(min(max(h / WATER_H, WATER_SCALE[0]), WATER_SCALE[1]))


def _towers(frame, feats, rows, box, placed):
    cls = _cls(feats)
    if cls is None:
        return
    ids = getattr(feats, "ids", None)
    for i in (range(len(feats.kind)) if rows is None else rows.tolist()):
        kind = str(cls[i])
        if kind in CELL_CLASSES:
            group, h, lift = "celltower", CELL_H, CELL_LIFT
        elif kind in WATER_CLASSES:
            s = water_scale(feats, i)
            group, h, lift = "watertower", WATER_H * s, WATER_LIFT * s
        else:
            continue
        if int(feats.kind[i]) not in (mapgeom.POINT, mapgeom.POLYGON):
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None:
            continue
        x, y = _centre(xy)
        if not _inside(x, y, box):
            continue
        fid = ids[i] if ids is not None else (round(x, 1), round(y, 1))
        la, lo = _ll(frame, x, y)
        placed.append({"group": group, "x": x, "y": y, "lat": la, "lon": lo, "lift": lift, "h": h,
                       "yaw": _hash(fid, "yaw") * 360.0, "rank": 0.0})


def _billboards(frame, feats, rows, box, roads_xy, placed):
    ids = getattr(feats, "ids", None)
    if feats.attrs.get("tags") is None:
        return
    got = _memo(feats, "boards", frame, lambda: _centres(frame, feats, _board_rows(feats)))
    pick = _picked(feats, rows, [g[0] for g in got])
    for k, (i, x, y) in enumerate(got):
        if pick is not None and not pick[k]:
            continue
        if not _inside(x, y, box):
            continue
        fid = ids[i] if ids is not None else (round(x, 1), round(y, 1))
        delta = _road_delta(np.array([x, y]), roads_xy(), BILL_ROAD_M)
        if delta is None:
            yaw = _hash(fid, "bill") * 360.0
        else:
            import furniture
            # local +x turns toward yaw, and this poster's face is local +y, so aim +y at the road
            yaw = furniture._yaw(float(delta[0]), float(delta[1])) - 90.0
        ox, oy = _local_offset(yaw, BILL_OX, BILL_OY)
        x, y = x - ox, y - oy
        if any(p["group"] == "billboard" and dnp.sq(p["x"] - x) + dnp.sq(p["y"] - y) < 9.0 for p in placed):
            continue
        la, lo = _ll(frame, x, y)
        placed.append({"group": "billboard", "x": x, "y": y, "lat": la, "lon": lo, "lift": BILL_LIFT,
                       "h": BILL_H, "yaw": yaw, "rank": _hash(fid, "face") * 0.3})


def _board_rows(feats):
    return [i for i in range(len(feats.kind)) if int(feats.kind[i]) in (mapgeom.POINT, mapgeom.POLYGON)
            and _advertising(_tags_of(feats, i))]


def _centres(frame, feats, rows, least=1):
    """[(row, x, y)] of these rows with a first ring of at least `least` points, its centre."""
    out = []
    for i in rows:
        xy = _ring_xy(feats, i, frame)
        if xy is None or len(xy) < least:
            continue
        x, y = _centre(xy)
        out.append((i, x, y))
    return out


def _local_offset(yaw_deg, ox, oy):
    """World offset of local (ox, oy) at yaw (trees.lua: local +x turns toward yaw, +y is +x rotated 90° CCW)."""
    t = math.radians(yaw_deg)
    c, s = dmath.cos(t), dmath.sin(t)
    return ox * c + oy * -s, ox * s + oy * c


def _barns(frame, feats, rows, box, placed, skip):
    import buildings
    cls = _cls(feats)
    if cls is None:
        return
    ids = getattr(feats, "ids", None)
    want = buildings.BARN_CLASSES
    got = _memo(feats, "barns", frame, lambda: _centres(frame, feats, [
        i for i in range(len(feats.kind)) if str(cls[i]) in want and int(feats.kind[i]) == mapgeom.POLYGON], 3))
    pick = _picked(feats, rows, [g[0] for g in got])
    for k, (i, x, y) in enumerate(got):
        if pick is not None and not pick[k]:
            continue
        if not _inside(x, y, box):
            continue
        xy = _ring_xy(feats, i, frame)
        try:
            _, u, hl, hw = buildings.oriented_box(xy)
        except Exception:
            continue
        scale = float(2.0 * hl) / BARN_LEN
        if not (BARN_SCALE[0] <= scale <= BARN_SCALE[1]):
            continue
        import furniture
        la, lo = _ll(frame, x, y)
        placed.append({"group": "barn", "x": x, "y": y, "lat": la, "lon": lo, "lift": BARN_LIFT * scale,
                       "h": BARN_H * scale, "yaw": furniture._yaw(float(u[0]), float(u[1])), "rank": 0.0})
        if ids is not None:
            skip.add(ids[i])
            skip.add(str(ids[i]))


def _containers(frame, feats, rows, box, bld_rings, roads_xy, placed):
    x0, y0, x1, y1 = box
    yards, at, bb = _memo(feats, "yards", frame, lambda: _yards(frame, feats))
    if not yards:
        return
    near = ~((bb[:, 1] < x0) | (bb[:, 0] >= x1) | (bb[:, 3] < y0) | (bb[:, 2] >= y1))
    pick = _picked(feats, rows, at)
    if pick is not None:
        near &= pick
    for k in np.flatnonzero(near).tolist():
        kind, fid, c, yaw, pxy, inside = yards[k]
        keep = inside.copy()
        keep &= ~_in_rings(pxy, bld_rings())
        keep &= ~_near_roads(pxy, roads_xy(), CONT_ROAD_M)
        keep &= (pxy[:, 0] >= x0) & (pxy[:, 0] < x1) & (pxy[:, 1] >= y0) & (pxy[:, 1] < y1)
        pxy = pxy[keep]
        if len(pxy) == 0:
            continue
        order = np.argsort(np.sum(dnp.sq(pxy - c), axis=1))
        pxy = pxy[order[:CONT_YARD_MAX[kind]]]
        for p in pxy:
            la, lo = _ll(frame, float(p[0]), float(p[1]))
            placed.append({"group": "container", "x": float(p[0]), "y": float(p[1]), "lat": la, "lon": lo,
                           "lift": 0.0, "h": CONT_H, "yaw": yaw,
                           "rank": _hash(fid, round(float(p[0]), 1), round(float(p[1]), 1), "tone") * 0.3})


def _yards(frame, feats):
    """([(kind, fid, centre, yaw, grid points, inside the yard)], their rows, (k, 4) ring boxes [x min, x max, y min,
    y max]) of the layer's yards that can hold a container, in row order: a tile keeps the boxes in its square."""
    import buildings
    import furniture
    from polygon import Path
    yards, at, bb = [], [], []
    cls = _cls(feats)
    if cls is None:
        return yards, at, np.zeros((0, 4))
    ids = getattr(feats, "ids", None)
    for i in range(len(feats.kind)):
        kind = str(cls[i])
        if kind not in CONT_AREA or int(feats.kind[i]) != mapgeom.POLYGON:
            continue
        xy = _ring_xy(feats, i, frame)
        if xy is None or len(xy) < 3:
            continue
        area = _area(xy)
        lo, hi = CONT_AREA[kind]
        if not (lo <= area <= hi):
            continue
        try:
            c, u, hl, hw = buildings.oriented_box(xy)
        except Exception:
            continue
        fid = ids[i] if ids is not None else (round(float(c[0]), 1), round(float(c[1]), 1))
        v = np.array([-float(u[1]), float(u[0])])
        yaw = furniture._yaw(float(v[0]), float(v[1]))
        pts = []
        ia = range(-int(hl / CONT_ALONG_M), int(hl / CONT_ALONG_M) + 1)
        ib = range(-int(hw / CONT_ACROSS_M), int(hw / CONT_ACROSS_M) + 1)
        for a in ia:
            for b in ib:
                if _hash(fid, int(a), int(b), "keep") >= CONT_KEEP:
                    continue
                p = c + u * (a * CONT_ALONG_M) + v * (b * CONT_ACROSS_M)
                pts.append(p)
        if not pts:
            continue
        pxy = np.asarray(pts, np.float64)
        inside = np.asarray(Path(xy).contains_points(pxy), bool)
        if not inside.any():
            continue
        yards.append((kind, fid, c, yaw, pxy, inside))
        at.append(i)
        bb.append((xy[:, 0].min(), xy[:, 0].max(), xy[:, 1].min(), xy[:, 1].max()))
    return yards, at, np.asarray(bb, np.float64).reshape(-1, 4)


def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return abs(float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) * 0.5


def _building_rings(frame, layers):
    """(coordinate arrays, (k,) which array, (k,) start, (k,) end, (k, 4) boxes [x min, x max, y min, y max]) of the
    building footprints of these layers, in order: footprint k is arrays[which[k]][start[k]:end[k]]."""
    xys, own, c0s, c1s, bbs = [], [], [], [], []
    for feats, rows in layers:
        xy, c0, c1, at, bb = _memo(feats, "rings", frame, lambda: _layer_rings(frame, feats))
        pick = _picked(feats, rows, at)
        if pick is not None:
            c0, c1, bb = c0[pick], c1[pick], bb[pick]
        own.append(np.full(len(c0), len(xys), np.int64))
        xys.append(xy)
        c0s.append(c0)
        c1s.append(c1)
        bbs.append(bb)
    if not xys:
        return [], np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros((0, 4))
    return xys, np.concatenate(own), np.concatenate(c0s), np.concatenate(c1s), np.concatenate(bbs)


def _layer_rings(frame, feats):
    """(coordinates, starts, ends, rows, boxes) of a layer's footprints (polygons but roofs, the first ring of 3+ points
    without its closing one): ranges of feats.xy_in(frame), _ring_xy's numbers, found for all rows at once (one at a
    time it was ~0.3 s a Penn Station buildings layer, 2026-11-03)."""
    L = feats.lonlat if isinstance(feats, mapgeom.Features) else None
    if L is None or L.dtype != np.float64 or L.ndim != 2:
        rings, at = [], []
        cls = _cls(feats)
        for i in range(len(feats.kind)):
            if int(feats.kind[i]) != mapgeom.POLYGON:
                continue
            if cls is not None and str(cls[i]) in ("roof",):
                continue
            xy = _ring_xy(feats, i, frame)
            if xy is not None and len(xy) >= 3:
                rings.append(xy)
                at.append(i)
        n = np.array([len(r) for r in rings], np.int64)
        c1 = np.cumsum(n)
        c0 = c1 - n
        xy = np.concatenate(rings) if rings else np.zeros((0, 2))
    else:
        n = len(feats.kind)
        ok = np.asarray(feats.kind[:n]) == mapgeom.POLYGON
        cls = _cls(feats)
        if cls is not None:
            ok &= cls[:n] != "roof"
        po, ro, co = (np.asarray(v, np.int64) for v in (feats.part_off, feats.ring_off, feats.coord_off))
        at = np.flatnonzero(ok)
        at = at[po[at + 1] > po[at]]                          # a part
        p0 = po[at]
        has = ro[p0 + 1] > ro[p0]                             # its first ring
        at, p0 = at[has], p0[has]
        c0, c1 = co[ro[p0]], co[ro[p0] + 1]
        has = c1 > c0
        at, c0, c1 = at[has], c0[has], c1[has]
        xy = feats.xy_in(frame)
        a, b = xy[c0], xy[c1 - 1]
        # (np.allclose(first, last) of each: isclose's own test, rtol 1e-5, atol 1e-8)
        close = (((np.abs(a - b) <= 1e-08 + 1e-05 * np.abs(b)) & np.isfinite(b)) | (a == b)).all(axis=1)
        c1 = c1 - (close & (c1 - c0 >= 2))
        has = c1 - c0 >= 3
        at, c0, c1 = at[has], c0[has], c1[has]
    bb = np.empty((len(c0), 4))
    if len(c0):
        bb[:, 0] = mapgeom._reduce(np.minimum, xy[:, 0], c0, c1)
        bb[:, 1] = mapgeom._reduce(np.maximum, xy[:, 0], c0, c1)
        bb[:, 2] = mapgeom._reduce(np.minimum, xy[:, 1], c0, c1)
        bb[:, 3] = mapgeom._reduce(np.maximum, xy[:, 1], c0, c1)
    return xy, c0, c1, np.asarray(at, np.int64), bb


def _in_rings(pts, rings):
    from polygon import Path
    hit = np.zeros(len(pts), bool)
    xys, own, c0, c1, bb = rings
    if not len(pts) or not len(bb):
        return hit
    # (only the rings whose box meets the points' box, tested on the boxes at once)
    px0, px1, py0, py1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
    near = np.flatnonzero(~((bb[:, 1] < px0) | (bb[:, 0] > px1) | (bb[:, 3] < py0) | (bb[:, 2] > py1)))
    for k in near.tolist():
        hit |= np.asarray(Path(xys[own[k]][c0[k]:c1[k]]).contains_points(pts), bool)
        if hit.all():
            return hit
    return hit


def _polylines(frame, layers):
    lines = []
    for feats, rows in layers:
        got, at = _memo(feats, "lines", frame, lambda: _layer_lines(frame, feats))
        pick = _picked(feats, rows, at)
        lines.extend(got if pick is None else (got[k] for k in np.flatnonzero(pick).tolist()))
    return lines


def _layer_lines(frame, feats):
    lines, at = [], []
    for i in range(len(feats.kind)):
        if int(feats.kind[i]) != mapgeom.LINE:
            continue
        for part in feats.parts(int(i)):
            for ring in part:
                ring = np.asarray(ring, np.float64)
                if len(ring) < 2:
                    continue
                e, n = mapgeom.enu_xy(feats, frame, ring)
                lines.append(np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)]))
                at.append(i)
    return lines, at


def _near_roads(pts, lines, dist):
    hit = np.zeros(len(pts), bool)
    if not len(pts) or not lines:
        return hit
    lim = dist * dist
    px0, px1, py0, py1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
    for poly in lines:
        if len(poly) < 2:
            continue
        if poly[:, 0].max() < pts[:, 0].min() - dist or poly[:, 0].min() > pts[:, 0].max() + dist:
            continue
        if poly[:, 1].max() < pts[:, 1].min() - dist or poly[:, 1].min() > pts[:, 1].max() + dist:
            continue
        a, b = poly[:-1], poly[1:]
        # (only the segments within dist + 1 m of the points' box, tested at once: past that no rounding reaches dist;
        # each segment of a rural road was a loop step, ~0.05 s a yard on the A11, 2026-11-03)
        m = dist + 1.0
        near = ~((np.maximum(a[:, 0], b[:, 0]) < px0 - m) | (np.minimum(a[:, 0], b[:, 0]) > px1 + m)
                 | (np.maximum(a[:, 1], b[:, 1]) < py0 - m) | (np.minimum(a[:, 1], b[:, 1]) > py1 + m))
        for i in np.flatnonzero(near).tolist():
            ab = b[i] - a[i]
            L2 = float(dnp.dot(ab, ab)) or 1e-9
            t = np.clip((dnp.matmul(pts - a[i], ab)) / L2, 0.0, 1.0)
            proj = a[i] + t[:, None] * ab
            hit |= np.sum(dnp.sq(pts - proj), axis=1) < lim
            if hit.all():
                return hit
    return hit


def _road_delta(pt, lines, max_m):
    """The vector from pt to the nearest point of a road within max_m, or None."""
    best = max_m * max_m
    delta = None
    for poly in lines:
        if len(poly) < 2:
            continue
        a, b = poly[:-1], poly[1:]
        ab = b - a
        L2 = np.maximum(np.sum(ab * ab, axis=1), 1e-9)
        t = np.clip(np.sum((pt - a) * ab, axis=1) / L2, 0.0, 1.0)
        proj = a + t[:, None] * ab
        d = proj - pt
        d2 = np.sum(d * d, axis=1)
        k = int(np.argmin(d2))
        if d2[k] < best and d2[k] > 0.25:
            best = float(d2[k])
            delta = d[k]
    return delta
