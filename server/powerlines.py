"""High-voltage power lines (the user, 2026-10-26, on "a map of all power lines and how they connect": "start with
1"): OpenStreetMap's power=line ways (Overture infrastructure class power_line; underground ones left out) as the
stock lattice tower (Automation Test Track's / West Coast USA's pylon, 35.8 m: trees.lua group "pylon") at each of
their vertices (the mapped towers) with six conductors (its double circuit: under the insulators of its three cross-arms)
hung between them (the road tile's wire mesh: furniture.wire_mesh), ending at the gantries of a yard kit where they
reach a substation (Overture infrastructure class substation areas; no towers inside them); the substations' yards filled with
West Coast USA's switchgear (its yard's parts as a kit, a smaller one where it does not fit) and fenced with chain link.
The layouts in Rust (earthfast.power_line_layout, earthfast.substation_yard).
"""
import logging

import numpy as np

import fast

log = logging.getLogger("earthd.powerlines")

G_PYLON = "pylon"
# Lines under HV_MIN_V stand on poles, not the 36 m lattice tower: a 66 kV line runs on poles along the street in town
# (Shepparton, Victoria: MNA-STN on concrete poles down the main road, drawn as pylons over the houses; GitHub #787),
# 69 kV in North America likewise; left out, they left empty cuttings hundreds of miles long through northern Michigan's
# woods (GitHub #797). A line without a mapped voltage keeps its towers.
HV_MIN_V = 100000.0
G_POLE = "hvpole"           # the game's own wooden power pole (art/shapes/common/power_lines_procedural, trees.lua)
POLE_SHAPE_H = 9.96         # m: its height as modelled ...
POLE_H = 16.0               # ... drawn this tall (a sub-transmission pole is 15-25 m; the stock one is a 10 m one)
_PS = POLE_H / POLE_SHAPE_H
# its three conductors: the top insulator and the two at the ends of the cross-arm (the shape's anchorPoint_11 / _6 /
# _10), to one side's three strain insulators at a substation's gantry
POLE_ARMS = np.array([[-0.523 * _PS, 9.21 * _PS, -5.8, 7.5], [0.669 * _PS, 7.245 * _PS, -4.05, 7.5],
                      [-0.63 * _PS, 7.245 * _PS, -7.55, 7.5]], np.float64)
POLE_ROAD_CLEAR_M = 1.0     # m off a carriageway edge a pole stands (a pylon: TOWER_ROAD_CLEAR_M)
POLE_WIRE_R = 0.012


def low_voltage(tags):
    """Is a power line's mapped voltage (OSM "voltage", ';'-separated circuits) all under HV_MIN_V? False when unknown."""
    import json
    try:
        v = json.loads(tags).get("voltage") if isinstance(tags, str) and tags.startswith("{") else None
    except ValueError:
        v = None
    if not v:
        return False
    kv = []
    for part in str(v).replace(",", ";").split(";"):
        try:
            kv.append(float(part.strip()))
        except ValueError:
            return False
    return bool(kv) and max(kv) < HV_MIN_V
PYLON_H = 35.8              # m (the shape's height: trees.lua scales to it)
# the conductors' attachment: the bottoms of the shape's insulator strings (lateral m, height m over its origin, which
# sits on the ground with the legs 5.9 m below it; read from pylon.dae 2026-10-26: 3.27 m out at 12.43 / 16.20 / 20.01,
# its base node scaled by 1.259; ARMS below)
SAG = 0.03                  # the sag at mid-span per metre of span
SEG_M = 10.0
MIN_SPAN_M, MAX_SPAN_M = 15.0, 2000.0
DEDUPE_M = 6.0
TOWER_ROAD_CLEAR_M = 6.0    # m off a public road's carriageway edge a tower's centre stands (its base ~10 m square):
                            # a line's mapped tower node a few metres off put a pylon on a highway's shoulder, in the way
                            # (TGForever Discord, 2026-10-28)
CLEAR_SEARCH_M = 60.0       # m round a tower's mapped spot a clear one is looked for when moving it straight off the
CLEAR_STEP_M = 2.0          # nearest road puts it on another (GitHub #550: a tower 0.8 m off one of the D1's
CLEAR_DIRS = 24             # carriageways and 12 m off the other, pushed from one onto the other and back)
WIRE_R = 0.022              # the conductors' half width (West Coast USA's read about twice a street wire's)
# The yards: West Coast USA's substation yard (-238, 20) as a kit, in its parts' own frame (its forest4 files'
# rotationMatrix rows are the parts' axes; read across, every part sat turned 90 degrees and the busbars ended in the
# air: the user, 2026-10-26, "alot of disconnected poles/pipes"): two bays 11.6 m apart, each a gantry
# (substation_truss: an 8 m beam ~9 m up, the line's conductors on its strain insulators 2.78 m in front of it, 1.75 m
# apart, 7.5 m up) whose busbars run 29.5 m back over a column of six supports to the transformers, the breaker
# between the bays, two lightning masts behind. Its smaller yard (-75, 645): a transformer and a mast. [group, x, y]
# around each kit's footprint's middle, the footprint [w along x, h along y] from the parts' boxes; the lines come in
# from the big kit's +y.
PARTS = {
    "gantry": 12.73, "sub_breaker": 5.48, "sub_mast": 25.29, "sub_support": 4.94, "sub_transformer": 6.18,
}
_CX, _CY = 1.025, 0.865
KITS = [
    ((22.2, 38.3), [(g, round(x - _CX, 3), round(y - _CY, 3)) for g, x, y in (
        ("gantry", -4.22, 16.30), ("gantry", 7.37, 16.21), ("sub_breaker", 1.20, 1.83),
        ("sub_mast", -9.01, -15.31), ("sub_mast", 11.06, -15.51),
        ("sub_support", -4.42, 19.68), ("sub_support", -4.21, 12.41), ("sub_support", -4.25, 7.98),
        ("sub_support", -4.21, 2.22), ("sub_support", -4.26, -2.86), ("sub_support", -4.23, -7.81),
        ("sub_support", 6.71, 19.75), ("sub_support", 7.36, 12.30), ("sub_support", 7.36, 7.92),
        ("sub_support", 7.33, 3.21), ("sub_support", 7.34, -2.77), ("sub_support", 7.34, -7.69),
        ("sub_transformer", -2.22, -14.53), ("sub_transformer", 5.39, -14.56))]),
    ((18.7, 17.4), [("sub_transformer", 6.15, -4.94), ("sub_mast", -8.26, 7.73)]),
    ((6.4, 7.5), [("sub_transformer", 0.0, 0.0)]),
]
# a line's end: the big kit with its gantries' middle on it (ANCHOR_AT in its frame), the conductors on their strain
# insulators (each side of the line to its own gantry, 5.8 m out); ends closer than END_MERGE_M share one
ANCHOR_AT = (round((-4.22 + 7.37) / 2 - _CX, 3), round((16.30 + 16.21) / 2 - _CY, 3))
GANTRY_LAT = (7.37 + 4.22) / 2
GANTRY_H = 7.5
GANTRY_FRONT = 2.78
END_MERGE_M = 20.0
# ARMS: per conductor [lateral, height] at a tower, [lateral, height] at the gantries
ARMS = np.array([[s * 4.12, h, s * (GANTRY_LAT + d), GANTRY_H]
                 for h, d in ((15.65, 1.75), (20.40, 0.0), (25.19, -1.75)) for s in (-1.0, 1.0)], np.float64)
YARD_GAP = 4.0              # m between kits, and half of it to the fence
YARD_MAX_KITS = 12
FENCE_LEN = 2.04            # s_chainlink_old's panel
UNDERGROUND = ('"location":"underground"', '"location":"underwater"')


def _substations(frame, cells, md):
    """The substation areas (Overture infrastructure class substation) in the cells: [(id, rings)] (frame xy)."""
    out, seen = [], set()
    for key in cells:
        f = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        if f is None or not len(f.kind) or f.attrs.get("class") is None:
            continue
        xy = None
        for i in np.flatnonzero((f.attrs["class"].astype(str) == "substation") & (f.kind == 3)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            xy = f.xy_in(frame) if xy is None else xy
            for part in f.parts(i, xy):
                rings = [np.asarray(r, np.float64)[:, :2] for r in part if len(r) >= 3]
                if rings:
                    out.append((fid, rings))
    out.sort(key=lambda q: q[0])
    return out


NEAR_M = MAX_SPAN_M + 50.0  # m round a tile a line's towers are laid out (a span reaching into it, and one past)


def _near(frame, ring, box):
    """The runs of a line's points (lon/lat ring) within NEAR_M of box, each with the point either side of it: the
    tile's spans and their towers' neighbours. A line's every tower had its ground read for each tile it touched: a
    100 km line's 2,000 towers, ~6,000 DEM tiles downloaded for one road tile in rural Pennsylvania (5 min),
    2026-10-27."""
    e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
    e, n = np.asarray(e), np.asarray(n)
    near = (e >= box[0] - NEAR_M) & (e <= box[2] + NEAR_M) & (n >= box[1] - NEAR_M) & (n <= box[3] + NEAR_M)
    if near.all():
        return [ring]
    keep = near.copy()
    keep[:-1] |= near[1:]
    keep[1:] |= near[:-1]
    out, i = [], 0
    while i < len(keep):
        if not keep[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(keep) and keep[j + 1]:
            j += 1
        if j > i:
            out.append(ring[i:j + 1])
        i = j + 1
    return out


def _off_roads(frame, md, e, n, ll, clear_m=None):
    """The line vertices (e, n: frame m; ll: their lon/lat) moved off the public roads' carriageways: one closer than
    half the carriageway + TOWER_ROAD_CLEAR_M to a road's centreline goes straight away from the nearest to that
    distance; where that is too close to another road (between a dual carriageway's two, in a junction's corner), to
    the nearest spot within CLEAR_SEARCH_M clear of them all (rings CLEAR_STEP_M apart, CLEAR_DIRS spots each), else
    to the one farthest off them. Each vertex reads the roads of its own map cell and the 8 around it, so every road
    tile (and every process) moves it alike: the same towers and wires on both sides of a tile edge."""
    import gasstations
    import mapcells
    fast.require(168, "power-line road avoidance")
    clear_m = TOWER_ROAD_CLEAR_M if clear_m is None else clear_m
    e, n = e.copy(), n.copy()
    layers = {}

    def roads_of(key):
        got = layers.get(key)
        if got is None:
            got = []
            for k in [key] + key.neighbours():
                try:
                    got.append(md.cell(k, layers=("roads",), kind="demand").layers.get("roads"))
                except Exception:       # (a cell not at hand: the vertex stays where it is mapped)
                    got.append(None)
            layers[key] = got
        return got
    ang = 2.0 * np.pi * np.arange(CLEAR_DIRS) / CLEAR_DIRS
    rings = np.arange(CLEAR_STEP_M, CLEAR_SEARCH_M + 1e-6, CLEAR_STEP_M)
    around = (rings[:, None, None] * np.stack([np.cos(ang), np.sin(ang)], -1)[None]).reshape(-1, 2)
    for i in range(len(e)):
        lines = gasstations.drive_lines(frame, roads_of(mapcells.cell_of(float(ll[i, 1]), float(ll[i, 0]))),
                                        float(e[i]), float(n[i]), 30.0 + CLEAR_SEARCH_M)
        if not lines:
            continue
        # One native call per tower: pack its nearby segments once, then test the ordered search without
        # allocating a candidate-by-segment matrix for every road (a warm city tile's measured Python hot path).
        ends = np.cumsum([len(line) - 1 for line, _ in lines], dtype=np.int64)
        segments = np.empty((int(ends[-1]), 5), np.float64)
        at = 0
        for line, hw in lines:
            count = len(line) - 1
            segments[at:at + count, :2] = line[:-1]
            segments[at:at + count, 2:4] = line[1:] - line[:-1]
            segments[at:at + count, 4] = hw
            at += count
        e[i], n[i] = fast._ef.power_off_roads(segments, ends, float(e[i]), float(n[i]), around, float(clear_m))
    return e, n


def tile_lines(frame, tx, ty, md, elev, props, wires, clearance=None):
    """Add road tile (tx, ty)'s towers, gantries, substation yards (props rows, furniture.place's form) and conductors
    (wires polylines) of the power lines near it; elev(lat, lon): the ground. clearance is build_tile's shared
    RoadClearance: sampled tapers and actual shifted / sidewalk-expanded decks, never tunnel vaults. Full stock
    colliders are checked AFTER native layout (which decides tower yaw). Unlike moving a point away from one road,
    this also covers crossroads, a rotated tower's corners and services. A tower with no safe footprint is omitted;
    its overhead conductors remain, not cut at a tile boundary.

    A cache read that fails (an OSError: PermissionError opening an Overture infrastructure file on Windows, PR #861's
    review at 50.8468, 7.0975) leaves this tile without power lines instead of dropping it: the caller has already
    built its decks, and an exception here lost the whole road tile (a viaduct never arrived). The rows are added
    only once the whole layout succeeded, never half a line's towers."""
    if md is None:
        return
    got_props, got_wires = [], []
    try:
        _tile_lines(frame, tx, ty, md, elev, got_props, got_wires, clearance)
    except OSError as e:
        log.warning("power lines: tile %s_%s left out: %s: %s", tx, ty, type(e).__name__, e)
        return
    props.extend(got_props)
    wires.extend(got_wires)


def _tile_lines(frame, tx, ty, md, elev, props, wires, clearance):
    """tile_lines' layout, into props / wires (fresh lists: tile_lines adds them on success)."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cells = roads.cells_of_tile(frame, tx, ty, margin=MAX_SPAN_M / 4)
    lines, poles, seen = [], [], set()
    for key in cells:
        f = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        if f is None or not len(f.kind) or f.attrs.get("class") is None:
            continue
        cls, tags = f.attrs["class"].astype(str), f.attrs.get("tags")
        for i in np.flatnonzero((cls == "power_line") & (f.kind == 2)):
            fid = str(f.ids[i])
            t = str(tags[i]) if tags is not None else ""
            if fid in seen or any(u in t for u in UNDERGROUND):
                continue
            seen.add(fid)
            into = poles if low_voltage(t) else lines
            for part in f.parts(i):
                for ring in part:
                    if len(ring) >= 2:
                        into += [(fid, r) for r in _near(frame, np.asarray(ring, np.float64), (x0, y0, x1, y1))]
    subs = _substations(frame, cells, md)
    fast.require(82, "power_line_layout")
    ends = np.zeros((0, 4))
    for group, h, arms, clear_m, wire_r, these in ((G_PYLON, PYLON_H, ARMS, TOWER_ROAD_CLEAR_M, WIRE_R, lines),
                                                  (G_POLE, POLE_H, POLE_ARMS, POLE_ROAD_CLEAR_M, POLE_WIRE_R, poles)):
        got = _lay(frame, md, elev, subs, these, group, h, arms, clear_m, wire_r, (x0, y0, x1, y1), props, wires,
                   clearance)
        if len(got):
            ends = np.vstack([ends, got])
    _yards(frame, elev, subs, ends, (x0, y0, x1, y1), props)


def _lay(frame, md, elev, subs, lines, group, h, arms, clear_m, wire_r, box, props, wires, clearance):
    """The towers (group, h m tall; conductors at arms) and conductors of lines [(fid, lon/lat ring)] (tile_lines);
    returns their ends (n, 4) at the substations."""
    from polygon import Path
    ends = np.zeros((0, 4))
    x0, y0, x1, y1 = box
    if lines:
        lines.sort(key=lambda q: q[0])          # (the same order in every tile: shared towers decided alike)
        ll = np.concatenate([r for _, r in lines])
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        e, n = _off_roads(frame, md, np.asarray(e, np.float64), np.asarray(n, np.float64), ll, clear_m)
        la2, lo2, _ = frame.enu_to_geodetic(e, n, np.zeros(len(e)))
        la2, lo2 = np.asarray(la2, np.float64), np.asarray(lo2, np.float64)
        _, _, z = frame.geodetic_to_enu(la2, lo2, np.asarray(elev(la2, lo2), np.float64))
        pts = np.ascontiguousarray(np.stack([e, n, np.asarray(z)], -1), np.float64)
        off = np.cumsum([0] + [len(r) for _, r in lines]).astype(np.int64)
        stop = np.zeros(len(pts), bool)
        for _, rings in subs:
            here = np.zeros(len(pts), bool)
            for ring in rings:
                here ^= Path(ring).contains_points(pts[:, :2])
            stop |= here
        towers, ends, wp, wo = fast._ef.power_line_layout(pts, off, stop, arms, SAG, SEG_M, MIN_SPAN_M, MAX_SPAN_M,
                                                          DEDUPE_M, END_MERGE_M, GANTRY_FRONT, [x0, y0, x1, y1])
        for x, y, zz, yaw in towers:
            row = [group, round(float(x), 2), round(float(y), 2), round(float(zz), 2), round(float(yaw), 1),
                   1.0, h, 0.0]
            if clearance is None or clearance.clear(row):
                props.append(row)
        for a, b in zip(wo[:-1], wo[1:]):
            wires.append((np.asarray(wp[a:b]), wire_r))
    return np.asarray(ends, np.float64).reshape(-1, 4)


def _yards(frame, elev, subs, ends, box, props):
    """The substations' yards (props rows) with the lines' ends (n, 4) at their gantries (tile_lines)."""
    from polygon import Path
    x0, y0, x1, y1 = box
    if not subs:
        return
    groups = sorted(PARTS)
    kits = np.array([k[0] for k in KITS], np.float64)
    parts = np.array([[k, groups.index(g), x, y] for k, (_, ps) in enumerate(KITS) for g, x, y in ps], np.float64)
    # the line ends of every line near (not only the tile's), each in its substation
    rows = []
    for _, rings in subs:
        rp = np.ascontiguousarray(np.concatenate(rings), np.float64)
        ro = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
        here = np.zeros(len(ends), bool)
        if len(ends):
            for ring in rings:
                here ^= Path(ring).contains_points(ends[:, :2])
        anchors = np.ascontiguousarray(ends[here][:, [0, 1, 3]], np.float64).reshape(-1, 3)
        got, fence = fast._ef.substation_yard(rp, ro, kits, parts, YARD_GAP, anchors, 0, list(ANCHOR_AT),
                                              YARD_MAX_KITS, FENCE_LEN, [x0, y0, x1, y1])
        rows += [(groups[int(g)], x, y, yaw, PARTS[groups[int(g)]]) for g, x, y, yaw in got]
        rows += [("sub_fence", x, y, yaw, 1.76) for x, y, yaw in fence]
    if not rows:
        return
    xy = np.array([[r[1], r[2]] for r in rows], np.float64)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    for (g, x, y, yaw, h), zz in zip(rows, np.asarray(z)):
        props.append([g, round(float(x), 2), round(float(y), 2), round(float(zz), 2), round(float(yaw), 1), 1.0, h,
                      0.0])
