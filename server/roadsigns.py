"""Ordinary road signs from what the road data states (task 017): the glue of the native kernels earthfast.road_sign_place
(which rules hold where: native/earthfast/src/roadsigns.rs) and road_sign_mesh (one mesh on one atlas). Built in the
sign tiles (signstream.py, field "rsigns"), render-only, where sign_systems.json knows the country.

Off unless BNE_ROADSIGNS=on or the settings page's Road signs is on with the feature enabled on the server.
"""
from functools import lru_cache
import json
import os
import threading
import types

import numpy as np

import exitsigns
import fast
import mapcells
import mapgeom
import islands
import roads
import signroads
import speedsigns

KERNEL = 162                    # earthfast VERSION with road_sign_place / road_sign_mesh
ON = os.environ.get("BNE_ROADSIGNS", "off") == "on"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SHAPES = {"circle": 0, "tri_up": 1, "tri_down": 2, "diamond": 3, "octagon": 4, "square": 5, "rect": 6}
METAL = "bne_exit_metal"
FLAG_RING, FLAG_LIVING, FLAG_FOOT, FLAG_CYCLE = 1, 2, 4, 8     # roadsigns.rs
POI_GIVE_WAY, POI_STOP, POI_BUS_STOP, POI_PARKING, POI_TUNNEL = 0, 1, 2, 3, 4
POI_CLASSES = {"give_way": POI_GIVE_WAY, "stop": POI_STOP, "bus_stop": POI_BUS_STOP, "parking_entrance": POI_PARKING}
PATH_CLASSES = {"pedestrian": FLAG_FOOT, "cycleway": FLAG_CYCLE}     # roads cars do not drive, kept for the signs that enter them
PRIVATE = {"private", "no", "customers", "permit", "delivery"}        # a car park with such access gets no sign
TUNNEL_MIN_M = 80.0            # a shorter tunnel is not signed
PATH_HALF_M = 1.5
PARAM_KEYS = ("warn_m", "curve_turn_deg", "curve_window_m", "curve_min_rank", "double_gap_m", "motorway_end_m",
              "living_min_m", "min_before_m", "chevron_turn_deg", "chevron_window_m", "narrow_ratio")
_lock = threading.Lock()
OPEN_CLASSES = {"track", "unknown"}     # a road ending on one of these goes on
CROSSING_KINDS = {"zebra", "marked", "uncontrolled"}


def enabled(server):
    """Whether the sign tiles carry road signs: the server's flag and the settings page's switch."""
    return ON and bool(getattr(server, "rsigns_on", True))


@lru_cache(maxsize=1)
def tables():
    with open(os.path.join(DATA, "sign_roles.json"), encoding="utf-8") as f:
        roles = json.load(f)
    with open(os.path.join(DATA, "sign_systems.json"), encoding="utf-8") as f:
        systems = json.load(f)
    return roles, systems, {r["id"]: (i, r) for i, r in enumerate(roles["roles"])}


def system(country):
    """The country's sign system (sign_systems.json), or None: no road signs there yet."""
    return tables()[1]["countries"].get((country or "").upper())


def widest(sizes):
    return max(max(v) for v in sizes.values())


def is_marked_crossing(tags):
    """Whether a crossing node's OSM tags state a marked crossing without signals."""
    if not tags:
        return False
    try:
        t = json.loads(tags)
    except ValueError:
        return False
    if t.get("highway") != "crossing" or t.get("crossing:markings") == "no" or t.get("crossing:signals") == "yes":
        return False
    if t.get("foot") == "no":
        return False
    return t.get("crossing") in CROSSING_KINDS or t.get("crossing_ref") == "zebra"


def points(frame, cells, wanted):
    """(n, 2) of the infrastructure points of class `wanted` (a set) that pass the filter wanted[class](tags), in frame, once."""
    ll = []
    for feats in cells:
        if feats is None or not len(feats):
            continue
        cls, tags, own = feats.attrs["class"], feats.attrs["tags"], feats.own
        for i in np.flatnonzero((feats.kind == mapgeom.POINT) & np.isin(cls, sorted(wanted)) & own):
            if wanted[str(cls[i])](str(tags[i])):
                ll.append(np.asarray(feats.parts(int(i))[0][0], np.float64)[0, :2])
    if not ll:
        return np.zeros((0, 2))
    ll = np.array(ll, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return np.ascontiguousarray(np.column_stack([e, n]), np.float64)


def crossings(frame, cells):
    """(n, 2) of the marked crossings of infrastructure cells, in frame, each once."""
    return points(frame, cells, {"crossing": is_marked_crossing})


def signals(frame, cells):
    """(n, 2) of the traffic signals of infrastructure cells, in frame, each once."""
    return points(frame, cells, {"traffic_signals": lambda _tags: True})


def _tags(text):
    try:
        t = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return t if isinstance(t, dict) else {}


def poi_of(cls, tags):
    """(kind, a) of an infrastructure point of class cls with these tags, or None: a give way / stop node with the way it states
    (a: +1 along the road's geometry, -1 against, 0 not stated), a bus stop (highway=bus_stop), a car park entrance that is not
    private."""
    kind = POI_CLASSES.get(cls)
    if kind is None:
        return None
    t = _tags(tags)
    if kind in (POI_GIVE_WAY, POI_STOP):
        return kind, {"forward": 1.0, "backward": -1.0}.get(str(t.get("direction", "")).lower(), 0.0)
    if kind == POI_BUS_STOP:
        return (kind, 0.0) if t.get("highway") == "bus_stop" or t.get("amenity") == "bus_stop" else None
    return (kind, 0.0) if str(t.get("access", "")).lower() not in PRIVATE else None


def pois(frame, cells):
    """(n, 5) x, y, kind, a, 0 of the points of interest of infrastructure cells (poi_of), in frame, each once."""
    rows = []
    for feats in cells:
        if feats is None or not len(feats):
            continue
        cls, tags, own = feats.attrs["class"], feats.attrs["tags"], feats.own
        for i in np.flatnonzero((feats.kind == mapgeom.POINT) & np.isin(cls, sorted(POI_CLASSES)) & own):
            got = poi_of(str(cls[i]), str(tags[i]))
            if got:
                ll = np.asarray(feats.parts(int(i))[0][0], np.float64)[0, :2]
                rows.append((ll[0], ll[1], got[0], got[1]))
    if not rows:
        return np.zeros((0, 5))
    a = np.array(rows, np.float64)
    e, n, _ = frame.geodetic_to_enu(a[:, 1], a[:, 0], 0.0)
    return np.ascontiguousarray(np.column_stack([e, n, a[:, 2], a[:, 3], np.zeros(len(a))]), np.float64)


def tunnel_portals(feats, own):
    """(n, 5) x, y, POI_TUNNEL, travel x, travel y (into the tunnel) of the portals of the tunnels of a cell's road rows `own`
    ({fid: speed row}), the tunnels of TUNNEL_MIN_M and more."""
    ids, out = None, []
    for fid, row in own.items():
        if not row[5] or row[5] == [(0.0, 1.0)]:
            continue
        pts = row[1]
        s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
        if s[-1] < TUNNEL_MIN_M:
            continue
        if ids is None:
            ids = {str(x): i for i, x in enumerate(feats.ids)}
        i = ids.get(fid)
        if i is None:
            continue
        at = lambda m, c: float(np.interp(min(max(m, 0.0), s[-1]), s, pts[:, c]))     # noqa: E731
        for a, b, kind in roads.level_ranges(feats, i):
            if kind != "tunnel" or (b - a) * s[-1] < TUNNEL_MIN_M:
                continue
            for f, sgn in ((a, 1.0), (b, -1.0)):
                m = f * s[-1]
                tx, ty = at(m + 3.0, 0) - at(m - 3.0, 0), at(m + 3.0, 1) - at(m - 3.0, 1)
                l = float(np.hypot(tx, ty))
                if l > 1e-6:
                    out.append((at(m, 0), at(m, 1), POI_TUNNEL, sgn * tx / l, sgn * ty / l))
    return np.array(out, np.float64).reshape(-1, 5)


def path_rows(feats, frame):
    """{fid: (speed row, meta, flags)} of the pedestrian zones and cycle paths of a cell: roads cars do not drive, so not in the
    sign network, but they join it, and the sign for entering one stands on the road that leads to it. Kept on the cell."""
    key = (frame.lat0, frame.lon0)
    with _lock:
        memo = feats.__dict__.setdefault('_rsign_paths', {})
        got = memo.get(key)
    if got is None:
        got = {}
        cls = feats.attrs.get("class")
        con = feats.rules.get("connectors")
        if cls is not None:
            for i in np.flatnonzero(np.isin(cls, sorted(PATH_CLASSES))):
                parts = feats.parts(int(i))
                if not parts or not len(parts[0]) or len(parts[0][0]) < 2:
                    continue
                line = np.asarray(parts[0][0], np.float64)[:, :2]
                e, n, _ = frame.geodetic_to_enu(line[:, 1], line[:, 0], 0.0)
                cons = [(float(con.start[r]), str(con.value[r])) for r in con.rows(int(i))] if con is not None else []
                row = (line, np.ascontiguousarray(np.column_stack([e, n]), np.float64), (PATH_HALF_M, 0.0, 0.0, 0.0, 0.0), cons, [], [])
                got[str(feats.ids[i])] = (row, list(row[2]), PATH_CLASSES[str(cls[i])])
        with _lock:
            if len(memo) >= speedsigns.CELL_FRAMES:
                memo.pop(next(iter(memo)))
            memo[key] = got
    return got


def open_ends(frame, cells):
    """(n, 2) of the ends of the roads not in the sign network that traffic can go on along."""
    ll = []
    for feats in cells:
        cls = feats.attrs["class"]
        for i in np.flatnonzero(np.isin(cls, sorted(OPEN_CLASSES))):
            parts = feats.parts(int(i))
            if parts and len(parts[0]) and len(parts[0][0]) >= 2:
                line = np.asarray(parts[0][0], np.float64)
                ll += [line[0, :2], line[-1, :2]]
    if not ll:
        return np.zeros((0, 2))
    ll = np.array(ll, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return np.ascontiguousarray(np.column_stack([e, n]), np.float64)


def living_ids(feats, frame, urban, widths, left_hand):
    """The ids of the cell's living streets (road_specs' class), kept on the cell."""
    key = (frame.lat0, frame.lon0, urban, widths, bool(left_hand))
    with _lock:
        memo = feats.__dict__.setdefault('_rsign_living', {})
        got = memo.get(key)
    if got is None:
        specs = signroads.cell_specs(feats, urban, widths, left_hand)
        got = frozenset(str(feats.ids[i]) for i, sp in enumerate(specs) if sp is not None and sp.cls == "living_street")
        with _lock:
            if len(memo) >= speedsigns.CELL_FRAMES:
                memo.pop(next(iter(memo)))
            memo[key] = got
    return got


def cell_rings(feats, frame, urban, widths, left_hand, mph, rows):
    """{fid: way round} of the roundabout rings made of the roads of one map cell (islands.ring_directions: the same loops the
    road tiles and the graph see), kept on the cell. A ring whose roads are in two cells is not seen: every road is whole in
    each cell it crosses, so it is lost only when no cell holds all of it. (Asked per tile for the roads round it, it was 60 %
    of a tile's time, 2026-11-07.)"""
    key = (frame.lat0, frame.lon0, urban, widths, bool(left_hand), bool(mph))
    with _lock:
        memo = feats.__dict__.setdefault('_rsign_rings', {})
        got = memo.get(key)
    if got is None:
        roads_list = [(fid, types.SimpleNamespace(oneway=bool(r[2][1]), dir=int(r[2][1])), r[1], 0.0) for fid, r in rows.items()]
        got = dict(islands.ring_directions(roads_list, left_hand)) if roads_list else {}
        with _lock:
            if len(memo) >= speedsigns.CELL_FRAMES:
                memo.pop(next(iter(memo)))
            memo[key] = got
    return got


def with_flags(rows, living, ring):
    """[(row, meta, flags)] of {fid: speedsigns row}: the roundabout rings (ring: {fid: way round}) and the living streets, a
    living street ranked as a street."""
    out = []
    for fid, row in rows.items():
        meta = list(row[2])
        flags = 0
        if fid in ring:
            flags |= FLAG_RING
            meta[1] = float(ring[fid])
        if fid in living:
            flags |= FLAG_LIVING
            meta[2] = max(meta[2], 1.0)
        out.append((row, meta, flags))
    return out


def arrays(rows, box):
    """road_sign_place's road arrays of rows [(row, meta, flags)] near box (x0, y0, x1, y1; MARGIN_M round it)."""
    x0, y0, x1, y1 = box
    m = speedsigns.MARGIN_M
    ll, xy, off, meta, flags = [], [], [0], [], []
    con_at, con_id, con_off = [], [], [0]
    skip, skip_off = [], [0]
    ids = {}
    for (line, pts, _m, cons, _lims, skips), mt, fl in rows:
        if (pts[:, 0].max() < x0 - m or pts[:, 0].min() > x1 + m or pts[:, 1].max() < y0 - m or pts[:, 1].min() > y1 + m):
            continue
        ll.append(line)
        xy.append(pts)
        off.append(off[-1] + len(pts))
        meta.append(list(mt) + [float(fl)])
        for f, c in cons:
            con_at.append(f)
            con_id.append(ids.setdefault(c, len(ids)))
        con_off.append(len(con_at))
        skip.extend(skips)
        skip_off.append(len(skip))
    if not xy:
        return None
    f64 = lambda a, *shape: np.ascontiguousarray(np.asarray(a, np.float64).reshape(*shape))    # noqa: E731
    i64 = lambda a: np.ascontiguousarray(np.asarray(a, np.int64))                              # noqa: E731
    return (f64(np.concatenate(xy), -1, 2), f64(np.concatenate(ll), -1, 2), i64(off), f64(meta, -1, 6), f64(con_at, -1),
            i64(con_id), i64(con_off), f64(skip, -1, 2), i64(skip_off))


def params(sys_row):
    """The kernel's thresholds: the shared ones with the country's own over them."""
    p = {**tables()[1]["params"], **sys_row.get("params", {})}
    return [float(p[k]) for k in PARAM_KEYS]


def pole_params(sys_row):
    """The kernel's pole settings: per rule its group's place in the country's order (-1: alone), most signs on a pole, the
    distance within which signs share one."""
    roles, systems, by_id = tables()
    order = list(sys_row["order"])
    ranks = []
    for rule in systems["rules"]:
        group = by_id[sys_row.get("roles", {}).get(rule, rule)][1]["group"]
        ranks.append(float(order.index(group)) if group in order else -1.0)
    return ranks + [float(sys_row.get("max_on_pole", 3)), float(systems["pole"]["join_m"])]


def speed_signs(frame, box, context, left_hand, mph):
    """(n, 4) x, y, travel x, travel y of the speed-limit signs in box and 10 m round it: the road signs keep off them."""
    x0, y0, x1, y1 = box
    try:
        rows = speedsigns.place(frame, (x0 - 10.0, y0 - 10.0, x1 + 10.0, y1 + 10.0), context, left_hand, mph)
    except Exception:       # (optional scenery: the road signs go on without it)
        return np.zeros((0, 4))
    # (speed_sign_place's rows are 9 or, since the German town signs (#892), 12 wide: the first 4 columns whatever the
    # width; a fixed reshape to 9 broke on 12)
    a = np.asarray(rows, np.float64)
    if a.ndim != 2:
        a = a.reshape(-1, 9)
    return np.ascontiguousarray(a[:, :4])


def place(md, frame, box, context, left_hand, mph, sys_row):
    """(k, 11) of road_sign_place's rows in box (x0, y0, x1, y1)."""
    fast.require(KERNEL, "road signs")
    x0, y0, x1, y1 = box
    m = speedsigns.MARGIN_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m]), np.array([y0 - m, y1 + m]), 0.0)
    rows, cells, living, ring, paths, portals = {}, [], set(), {}, {}, []
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = context.cell(key)
        if feats is None or not len(feats):
            continue
        cells.append(feats)
        living |= living_ids(feats, frame, context.urban, context.widths, left_hand)
        own = speedsigns._cell_rows(feats, frame, context.urban, context.widths, left_hand, mph)
        for fid, row in own.items():
            rows.setdefault(fid, row)           # (a line is whole in every cell it crosses)
        ring.update(cell_rings(feats, frame, context.urban, context.widths, left_hand, mph, own))
        paths.update(path_rows(feats, frame))
        portals.append(tunnel_portals(feats, own))
    got = arrays(with_flags(rows, living, ring) + [v for k, v in paths.items() if k not in rows], box)
    if got is None:
        return np.zeros((0, 11))
    pts, ll, off, meta, con_at, con_id, con_off, skip, skip_off = got
    b_la, b_lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m]), np.array([y0 - m, y1 + m]), 0.0)
    infra = [md.cell(k, layers=("infrastructure",), kind="prefetch").layers.get("infrastructure")
             for k in mapcells.cells_for_bbox(float(b_la.min()), float(b_lo.min()), float(b_la.max()), float(b_lo.max()))]
    cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    poly, poly_off = speedsigns._outlines(context.pavements(frame, np.c_[clo, cla], left_hand))
    sizes = tables()[1]["sizes"]["default"]
    poi = np.vstack([pois(frame, infra)] + portals)
    return fast._ef.road_sign_place(pts, ll, off, meta, con_at, con_id, con_off, skip, skip_off, crossings(frame, infra),
                                    signals(frame, infra), open_ends(frame, cells), poly, poly_off, [float(v) for v in box],
                                    bool(left_hand), [float(v) for v in sys_row["lateral"]], float(widest(sizes)),
                                    params(sys_row), speed_signs(frame, box, context, left_hand, mph), pole_params(sys_row),
                                    np.ascontiguousarray(poi, np.float64))


def table(signs, z_ground, z_road, sys_row):
    """road_sign_mesh's rows of road_sign_place's, heights given: the signs of one pole stacked from the lowest up, the top one
    first in slot order, `gap_m` between them."""
    roles, systems, by_id = tables()
    sizes = systems["sizes"]["default"]
    gap = float(systems["pole"]["gap_m"])
    width = np.zeros(len(signs))
    cells = []
    for k, row in enumerate(signs):
        rule = systems["rules"][int(row[4])]
        cell, role = by_id[sys_row.get("roles", {}).get(rule, rule)]
        cells.append((cell, role))
        width[k] = sizes[role["shape"]][int(row[5])]
    out = np.zeros((len(signs), 10))
    for k, row in enumerate(signs):
        below = [width[j] + gap for j in range(len(signs)) if signs[j, 9] == row[9] and signs[j, 10] > row[10]]
        cell, role = cells[k]
        urban = row[8] != 0.0
        out[k] = (row[0], row[1], z_ground[k], z_road[k], row[2], row[3], cell, SHAPES[role["shape"]], width[k],
                  sys_row["mount"][0 if urban else 1] + sum(below))
    return np.ascontiguousarray(out)


def build_tile(md, frame, tx, ty, elev, context, decks=(), left_hand=False, mph=True, country=None):
    """The mesh (signstream.write_tile's) of road tile (tx, ty)'s road signs, or None (none)."""
    sys_row = system(country)
    if sys_row is None:
        return None
    box = roads.tile_bbox(tx, ty)
    signs = place(md, frame, box, context, left_hand, mph, sys_row)
    signs = signs[[tables()[1]["rules"][int(r)] not in sys_row.get("off", ()) for r in signs[:, 4]]] if len(signs) else signs
    keep = [k for k in range(len(signs)) if not exitsigns._on_deck(signs[k, :2], decks, 1.5)]
    if not keep:
        return None
    signs = signs[keep]
    pts = np.vstack([signs[:, 0:2], signs[:, 6:8]])
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    h = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).ravel()
    n = len(signs)
    roles = tables()[0]
    p, t, uv, mi = fast._ef.road_sign_mesh(table(signs, z[:n], z[n:], sys_row), roles["cols"], roles["rows"],
                                           [float(box[0]), float(box[1]), 0.0])
    if not len(t):
        return None
    return p, t, uv, ["bne_signs_" + sys_row["atlas"], METAL], mi
