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
import os

import numpy as np

import fast
import dnp

log = logging.getLogger("earthd.powerlines")

G_PYLON = "pylon"
# Lines under HV_MIN_V stand on poles, not the 36 m lattice tower: a 66 kV line runs on poles along the street in town
# (Shepparton, Victoria: MNA-STN on concrete poles down the main road, drawn as pylons over the houses; GitHub #787),
# 69 kV in North America likewise; left out, they left empty cuttings hundreds of miles long through northern Michigan's
# woods (GitHub #797). A line without a mapped voltage keeps its towers. So does one whose mapped supports (its way's
# nodes, OpenStreetMap power=pole / power=tower points: Overture power_pole / power_tower) are mostly poles: a 115 kV
# line across the plains on wooden poles was drawn as steel lattice (PR #919 by wyo-backroads, Warren AFB).
HV_MIN_V = 100000.0
# The substations' yards kept to their fences (BNE_POWER_YARDS=on, the Revamped roads setting; the user's report 95,
# 2026-11-17, and Codex's reviews of its fix): a line-end kit only where it stands inside its yard, its gantries moved
# in with their whole footprints (the end and its conductors with them, none where they fit nowhere), each gantry's
# ground read once for its conductors and its row, the lines ending in every yard read laid out in every tile drawing
# a span into it, each span carrying the supports at its ends across tiles (racedig). Off: as before, byte for byte.
YARDS_ON = os.environ.get("BNE_POWER_YARDS", "off") == "on"
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
# m the lowest conductor keeps over the ground (the elevation's, every SEG_M) all along a span: its sag cut where the
# ground leaves less, its towers scaled up (to 1.75 x) where the chord itself is under it (power_line_layout's
# span_clearance: a 418 m span over a ridge at Breno buried its conductors in the hill, the player's report 0XESWVG2)
SPAN_CLEAR_M = 3.0
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
# its two gantries in its frame (KITS[0]'s): the conductors' ends, kept inside the yard (power_line_layout)
GANTRIES = [[x, y] for g, x, y in KITS[0][1] if g == "gantry"]
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
    return [ring[i:j + 1] for i, j in _near_ranges(frame, ring, box)]


def _near_ranges(frame, ring, box):
    """_near's runs as index ranges (first, last) into ring."""
    e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
    e, n = np.asarray(e), np.asarray(n)
    near = (e >= box[0] - NEAR_M) & (e <= box[2] + NEAR_M) & (n >= box[1] - NEAR_M) & (n <= box[3] + NEAR_M)
    if near.all():
        return [(0, len(ring) - 1)]
    return _ranges(near)


def _ranges(hit):
    """The (first, last) index ranges of the runs of hit (n,) with the point either side of each, two points at least."""
    keep = np.asarray(hit, bool).copy()
    keep[:-1] |= hit[1:]
    keep[1:] |= hit[:-1]
    out, i = [], 0
    while i < len(keep):
        if not keep[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(keep) and keep[j + 1]:
            j += 1
        if j > i:
            out.append((i, j))
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
    around = (rings[:, None, None] * np.stack([dnp.cos(ang), dnp.sin(ang)], -1)[None]).reshape(-1, 2)
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


def _support_keys(f):
    """(pole keys, tower keys): the coordinate keys (_coord_keys) of a layer's mapped power_pole / power_tower points,
    sorted (kept on the layer: shared, not to be changed)."""
    got = f.__dict__.get("_pl_supports")
    if got is None:
        cls = f.attrs["class"].astype(str)
        out = []
        for name in ("power_pole", "power_tower"):
            at = np.flatnonzero((cls == name) & (f.kind == 1))
            pts = [f.parts(int(i))[0][0][0] for i in at]
            out.append(np.unique(_coord_keys(np.asarray(pts, np.float64).reshape(-1, 2))))
        got = f.__dict__["_pl_supports"] = tuple(out)
    return got


def _coord_keys(lonlat):
    """int64 keys of lon/lat points at 1e-7 degree (a way's node and the point mapped at it: the same OSM node)."""
    q = np.round(np.asarray(lonlat, np.float64) * 1e7).astype(np.int64)
    return (q[:, 0] << 32) ^ (q[:, 1] & 0xFFFFFFFF)


def on_poles(rings, pole_keys, tower_keys):
    """Do a power line's mapped supports (its vertices that are mapped power_pole / power_tower points) run mostly on
    poles?"""
    if not len(pole_keys):
        return False
    keys = _coord_keys(np.concatenate([np.asarray(r, np.float64).reshape(-1, 2) for r in rings]))
    n_pole = int(np.isin(keys, pole_keys).sum())
    return n_pole > 0 and n_pole > int(np.isin(keys, tower_keys).sum())


def _yard_cells(frame, rings):
    """The map cells a yard's rings (frame xy) reach ([] when its box cannot be had: logged)."""
    import mapcells
    xy = np.concatenate([np.asarray(r, np.float64)[:, :2] for r in rings])
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
    # (its longitudes unwrapped round one of its own vertices, not their mean: a yard across +-180 deg had a mean near
    # 0 and a box round the world; Codex's review of 0cf9fd9b)
    ref = float(lo[0])
    rel = (lo - ref + 180.0) % 360.0 - 180.0
    west, east = ((ref + float(v) + 180.0) % 360.0 - 180.0 for v in (rel.min(), rel.max()))
    try:
        return mapcells.cells_for_bbox(float(la.min()), west, float(la.max()), east)
    except ValueError as ex:
        log.warning("power lines: a yard's cells left out: %s", ex)
        return []


FEED_REACH_M = 400.0        # m round a yard a mapped vertex may lie and still be placed into it (off a road: CLEAR_SEARCH_M;
                            # out of a station's ground: its size)


CLASS_MAX_CELLS = 64            # map cells along a line its class reads at most (evenly along it beyond: ~150 km)


def _line_class(md, f, i, t, memo):
    """Does line i of layer f (tags t) run on poles (the pole lines' group), the same answer in every tile: its voltage
    (low_voltage), else its mapped supports (on_poles) as every map cell its vertices lie in holds them (CLASS_MAX_CELLS
    of them at most, evenly along it), those cells read whichever tile asks (memo: cell -> its support keys). The
    tile's own cells' supports made a feeder whose poles are mapped only near its yard a pylon line in the span's tile
    and a pole line in the yard's (Codex's review of 732f777f); its two end cells, then five sampled vertices' cells
    missed poles mapped between them (Codex's reviews of 8ac4b974 and 07eab0c0). A cell that cannot be read counts no
    supports and marks the tile short (revisit.fault: not kept), never losing the tile's power lines."""
    import mapcells
    if low_voltage(t):
        return True
    rings = [np.asarray(r, np.float64) for part in f.parts(i) for r in part if len(r)]
    if not rings:
        return False
    every = np.concatenate([r[:, :2] for r in rings])
    along = list(dict.fromkeys(mapcells.cell_of(float(q[1]), float(q[0])) for q in every))   # (in order along it)
    if len(along) > CLASS_MAX_CELLS:
        along = [along[int(round(k * (len(along) - 1) / (CLASS_MAX_CELLS - 1)))] for k in range(CLASS_MAX_CELLS)]
    keys = sorted(set(along), key=str)
    pk, tk = [], []
    for key in keys:
        got = memo.get(key)
        if got is None:
            empty = (np.zeros(0, np.int64), np.zeros(0, np.int64))
            try:
                g = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                ok = g is not None and len(g.kind) and g.attrs.get("class") is not None
                got = _support_keys(g) if ok else empty
            except Exception as ex:     # (OSError, a decode error)
                import revisit
                revisit.fault(here=True)
                log.warning("power lines: cell %s for a line's class unread: %s: %s", key, type(ex).__name__, ex)
                got = empty
            memo[key] = got
        pk.append(got[0])
        tk.append(got[1])
    return bool(on_poles(rings, np.unique(np.concatenate(pk)), np.unique(np.concatenate(tk))))


def _merged(ranges):
    """Index ranges (first, last) merged where they overlap, share a vertex or follow on (the span between two that
    follow on is neither's otherwise), in order."""
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _feeders(frame, md, subs, layers, cells, seen, plan, memo=None):
    """Every power line ending in one of subs (its vertices in a yard, with one neighbour each side), from the cells
    the yards reach, added to lines / poles where not laid out yet (seen: their ids): a line end merges into an earlier
    one of its yard, so the tile drawing a span into a yard and the yard's own tile must know the same lines ending
    there (Codex's review of 119bb4d7: a span tile that knew only one of two feeders hung its conductors where the
    yard tile's merged gantries were not). Into plan ({(id, part, ring): [ring, ranges, on poles]}, the tile's own runs
    already there): a line laid out already gains its yard approach too (a run clipped round the tile must not hide it:
    Codex's review of 732f777f), in its one class (_line_class)."""
    from polygon import Path
    if not subs:
        return
    keys = sorted({k for _, rings in subs for k in _yard_cells(frame, rings)} - set(cells), key=str)
    extra = []
    for key in keys:
        f = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        if f is not None and len(f.kind) and f.attrs.get("class") is not None:
            extra.append(f)
    every = list(layers) + extra
    memo = {} if memo is None else memo
    done = set()                    # (each line once: its first layer's copy)
    yards = [[Path(np.asarray(r, np.float64)[:, :2]) for r in rings] for _, rings in subs]
    envelopes = []
    for _, rings in subs:
        xy = np.concatenate([np.asarray(r, np.float64)[:, :2] for r in rings])
        envelopes.append((float(xy[:, 0].min()) - FEED_REACH_M, float(xy[:, 1].min()) - FEED_REACH_M,
                          float(xy[:, 0].max()) + FEED_REACH_M, float(xy[:, 1].max()) + FEED_REACH_M))
    for f in every:
        cls, tags = f.attrs["class"].astype(str), f.attrs.get("tags")
        for i in np.flatnonzero((cls == "power_line") & (f.kind == 2)):
            fid = str(f.ids[i])
            t = str(tags[i]) if tags is not None else ""
            if fid in done or any(u in t for u in UNDERGROUND):
                continue
            done.add(fid)
            pole = None
            runs = []
            for pk, part in enumerate(f.parts(i)):
                for rk, ring in enumerate(part):
                    ring = np.asarray(ring, np.float64)
                    if len(ring) < 2:
                        continue
                    # (only the vertices a placement could bring into a yard: a long way's others never read its roads
                    # and stations; Codex's review of 08f31f7c)
                    e0, n0, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
                    e0, n0 = np.asarray(e0, np.float64), np.asarray(n0, np.float64)
                    near = np.zeros(len(ring), bool)
                    for bx0, by0, bx1, by1 in envelopes:
                        near |= (e0 >= bx0) & (e0 <= bx1) & (n0 >= by0) & (n0 <= by1)
                    if not near.any():
                        continue
                    if pole is None:
                        pole = _line_class(md, f, i, t, memo)
                    idx = np.flatnonzero(near)
                    # (its vertices where the layout stands them: the yards it ends in read alike in every tile;
                    # Codex's review of 0cf9fd9b: the raw mapped points disagreed with the moved ones)
                    e, n = _placed(frame, md, ring[idx], POLE_ROAD_CLEAR_M if pole else TOWER_ROAD_CLEAR_M,
                                   (0, 0, 0, 0))
                    xy = np.column_stack([np.asarray(e, np.float64), np.asarray(n, np.float64)])
                    inside = np.zeros(len(idx), bool)
                    for paths in yards:
                        inn = np.zeros(len(idx), bool)
                        for path in paths:
                            inn ^= path.contains_points(xy)
                        inside |= inn
                    here = np.zeros(len(ring), bool)
                    here[idx] = inside
                    if not here.any():
                        continue
                    for j, k in _ranges(here):
                        plan.setdefault((fid, pk, rk), [ring, [], pole])[1].append((j, k))
                        runs.append((j, k))
            if runs:
                seen.add(fid)


def _tile_lines(frame, tx, ty, md, elev, props, wires, clearance):
    """tile_lines' layout, into props / wires (fresh lists: tile_lines adds them on success)."""
    import roads
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cells = roads.cells_of_tile(frame, tx, ty, margin=MAX_SPAN_M / 4)
    lines, poles, seen = [], [], set()
    plan, memo = {}, {}             # (BNE_POWER_YARDS: each line ring's runs to lay out; the classes' support cells)
    layers = []
    for key in cells:
        f = md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
        if f is None or not len(f.kind) or f.attrs.get("class") is None:
            continue
        layers.append(f)
    # (every cell's supports: a way crossing cells has its nodes' points in each)
    sup = [_support_keys(f) for f in layers]
    pole_keys = np.unique(np.concatenate([p for p, _ in sup])) if sup else np.zeros(0, np.int64)
    tower_keys = np.unique(np.concatenate([t for _, t in sup])) if sup else np.zeros(0, np.int64)
    for f in layers:
        cls, tags = f.attrs["class"].astype(str), f.attrs.get("tags")
        for i in np.flatnonzero((cls == "power_line") & (f.kind == 2)):
            fid = str(f.ids[i])
            t = str(tags[i]) if tags is not None else ""
            if fid in seen or any(u in t for u in UNDERGROUND):
                continue
            seen.add(fid)
            rings = [r for part in f.parts(i) for r in part if len(r)]
            runs = [(pk, rk, ring, _near_ranges(frame, ring, (x0, y0, x1, y1)))
                    for pk, part in enumerate(f.parts(i)) for rk, ring in enumerate(part)
                    for ring in (np.asarray(ring, np.float64),) if len(ring) >= 2]
            if YARDS_ON:                # (one class per line, whichever tile asks: _line_class; only a line laid out)
                if not any(ranges for *_, ranges in runs):
                    continue
                pole = _line_class(md, f, i, t, memo)
                for pk, rk, ring, ranges in runs:
                    if ranges:
                        plan.setdefault((fid, pk, rk), [ring, [], pole])[1].extend(ranges)
                continue
            into = poles if low_voltage(t) or (rings and on_poles(rings, pole_keys, tower_keys)) else lines
            for pk, rk, ring, ranges in runs:
                into += [(fid, ring[a:b + 1]) for a, b in ranges]
    # (the substations of the cells round the tile and of every cell a laid-out line vertex is in: a span drawn here
    # may end at a yard 1 km off, beyond the cells round the tile; Codex's review of 9618c426)
    if YARDS_ON:
        import mapcells
        reach = {mapcells.cell_of(float(q[1]), float(q[0])) for ring, ranges, _ in plan.values()
                 for a, b in ranges for q in ring[a:b + 1]}
        subs = _substations(frame, list(cells) + sorted(reach - set(cells), key=str), md)
        _feeders(frame, md, subs, layers, cells, seen, plan, memo)
        # (each ring's runs and yard approaches merged where they overlap or meet: one run through the join, so the
        # layout thins and heads it as a tile that saw it whole; Codex's review of 07eab0c0)
        for (fid, _, _), (ring, ranges, pole) in plan.items():
            (poles if pole else lines).extend((fid, ring[a:b + 1]) for a, b in _merged(ranges))
    else:
        subs = _substations(frame, cells, md)
    fast.require(496, "power_line_layout")
    ends = np.zeros((0, 7))
    for group, h, arms, clear_m, wire_r, these in ((G_PYLON, PYLON_H, ARMS, TOWER_ROAD_CLEAR_M, WIRE_R, lines),
                                                  (G_POLE, POLE_H, POLE_ARMS, POLE_ROAD_CLEAR_M, POLE_WIRE_R, poles)):
        got = _lay(frame, md, elev, subs, these, group, h, arms, clear_m, wire_r, (x0, y0, x1, y1), props, wires,
                   clearance)
        if len(got):
            ends = np.vstack([ends, got])
    _yards(frame, elev, subs, ends, (x0, y0, x1, y1), props)


def _placed(frame, md, ll, clear_m, box):
    """(e, n): the frame places of line vertices ll (lon/lat) as laid out: moved off the roads (_off_roads, each vertex
    alike in every tile) and out of the bus stations' ground. BNE_POWER_YARDS: the stations round each vertex's own map
    cell, so a vertex stands at one place whichever tile asks (the yards' membership read from it: _feeders); else
    those round the tile's box (as before)."""
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
    e, n = _off_roads(frame, md, np.asarray(e, np.float64), np.asarray(n, np.float64), ll, clear_m)
    import busstationkeep
    if YARDS_ON:
        # (each vertex out of the stations round its own map cell: the same keepout whichever vertices a tile asks
        # about together, never a box so large the station lookup gives up; Codex's review of 1ac6e583)
        import mapcells
        e, n = np.array(e, np.float64), np.array(n, np.float64)
        keys = [mapcells.cell_of(float(la), float(lo)) for lo, la in ll[:, :2]]
        for key in sorted(set(keys), key=str):
            idx = np.array([i for i, k in enumerate(keys) if k == key], np.int64)
            keep = busstationkeep.keepout(md, frame, _cell_box(frame, key, 200.0))
            if keep:
                xy = keep.push_out(np.column_stack((e[idx], n[idx])))
                e[idx], n[idx] = xy[:, 0], xy[:, 1]
        return e, n
    x0, y0, x1, y1 = box
    keep = busstationkeep.keepout(md, frame, (x0 - 200.0, y0 - 200.0, x1 + 200.0, y1 + 200.0))
    if keep:                                # (a bus station's ground: no pole on its platforms or in its yard)
        xy = keep.push_out(np.column_stack((e, n)))
        e, n = xy[:, 0], xy[:, 1]
    return e, n


def _cell_box(frame, key, margin):
    """A map cell's box (x0, y0, x1, y1) in frame, margin m round it."""
    import mapcells
    k = 1 << key.z
    la = [mapcells._lat_of(key.y, k), mapcells._lat_of(key.y + 1, k)]
    lo = [mapcells._lon_of(key.x, k), mapcells._lon_of(key.x + 1, k)]
    cla = np.array([la[0], la[0], la[1], la[1]], np.float64)
    clo = np.array([lo[0], lo[1], lo[0], lo[1]], np.float64)
    e, n, _ = frame.geodetic_to_enu(cla, clo, np.zeros(4))
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    return (float(e.min()) - margin, float(n.min()) - margin, float(e.max()) + margin, float(n.max()) + margin)


def _lay(frame, md, elev, subs, lines, group, h, arms, clear_m, wire_r, box, props, wires, clearance):
    """The towers (group, h m tall; conductors at arms) and conductors of lines [(fid, lon/lat ring)] (tile_lines);
    returns their ends (n, 7: power_line_layout's, the last their yard: the index into subs) at the substations."""
    from polygon import Path
    ends = np.zeros((0, 7))
    x0, y0, x1, y1 = box
    if lines:
        lines.sort(key=lambda q: q[0])          # (the same order in every tile: shared towers decided alike)
        ll = np.concatenate([r for _, r in lines])
        e, n = _placed(frame, md, ll, clear_m, box)
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
        # (the yards for the gantries' fit: BNE_POWER_YARDS; none: every end where it is mapped, as before)
        rings = [np.asarray(r, np.float64)[:, :2] for _, rs in subs for r in rs] if YARDS_ON else []
        yard_id = np.array([k for k, (_, rs) in enumerate(subs) for _ in rs] if YARDS_ON else [], np.int64)
        yard_pts = np.ascontiguousarray(np.concatenate(rings) if rings else np.zeros((0, 2)), np.float64)
        yard_off = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
        towers, ends, wp, wo, sup = fast._ef.power_line_layout(
            pts, off, stop, arms, SAG, SEG_M, MIN_SPAN_M, MAX_SPAN_M, DEDUPE_M, END_MERGE_M, GANTRY_FRONT,
            [x0, y0, x1, y1], yard_pts, yard_off, yard_id, list(ANCHOR_AT), GANTRIES,
            ground_at(frame, elev) if YARDS_ON else None, ground_at(frame, elev), SPAN_CLEAR_M)
        for x, y, zz, yaw, s in towers:
            row = tower_row(group, round(h * s, 2), x, y, zz, yaw)
            if clearance is None or clearance.clear(row):
                props.append(row)
        if YARDS_ON:
            wires.extend(span_wires(wp, wo, sup, group, h, wire_r))
        else:
            wires.extend((np.asarray(wp[a:b]), wire_r) for a, b in zip(wo[:-1], wo[1:]))
    return np.asarray(ends, np.float64).reshape(-1, 7)


# degrees added to a support's yaw (power_line_layout's: the line's direction, a corner's the bisector of its spans;
# its conductors' lateral offsets across it) for the model to face that way: the lattice tower's cross-arms run along
# its own y, the wooden pole's along its own x (electric_pole_wood_old_01's anchorPoint_6 / _10 / _11 at x -0.63 ..
# 0.67), so the pole stood turned a quarter, its cross-arm along the line and its insulators off the conductors, plain
# at a corner (the player's report BTZMCHC4, Drumheller, 2026-11-18)
YAW_ADD = {G_POLE: 90.0}


def tower_row(group, h, x, y, z, yaw):
    """A tower's props row (its model turned by YAW_ADD to the layout's yaw)."""
    yaw = (float(yaw) + YAW_ADD.get(group, 0.0)) % 360.0
    return [group, round(float(x), 2), round(float(y), 2), round(float(z), 2), round(yaw, 1), 1.0, h, 0.0]


def span_wires(wp, wo, sup, group, h, wire_r):
    """power_line_layout's conductors (points wp, offsets wo), each with the rows of the towers at its ends (sup: its
    supports: a tower's row, or at a line end the yard's row of the gantry it hangs on): a span is drawn by one tile,
    the support at its other end by the next, and the wire goes with that support wherever it is culled
    (furniture.with_ends; Codex's reviews of 7bafc807 and 119bb4d7)."""
    import furniture

    def row(q):
        if q[0] == 1:                   # (a tower: scaled as its own row, q[5])
            return tower_row(group, round(h * float(q[5]), 2), *q[1:5])
        if q[0] == 2:                   # (the gantry it hangs on: the yard's row of it)
            return tower_row("gantry", PARTS["gantry"], *q[1:5])
        return None
    out = []
    for j, (a, b) in enumerate(zip(wo[:-1], wo[1:])):
        out.extend(furniture.with_ends([(np.asarray(wp[a:b]), wire_r)], row(sup[j, 0:6]), row(sup[j, 6:12])))
    return out


def ground_at(frame, elev):
    """(x (n,), y (n,)) -> the frame z of the ground there (elev: geodetic): the towers', the gantries' and the yards'."""
    def at(x, y):
        x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
        la, lo, _ = frame.enu_to_geodetic(x, y, np.zeros(len(x)))
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    return at


def _yards(frame, elev, subs, ends, box, props):
    """The substations' yards (props rows) with the lines' ends (n, 7: _lay's) at their gantries, each in its own yard
    only (its last column: the index into subs; tile_lines); the
    gantries at the ground the conductors were hung from (power_line_layout's), the rest at the ground under each."""
    x0, y0, x1, y1 = box
    if not subs:
        return
    groups = sorted(PARTS)
    kits = np.array([k[0] for k in KITS], np.float64)
    parts = np.array([[k, groups.index(g), x, y] for k, (_, ps) in enumerate(KITS) for g, x, y in ps], np.float64)
    # the line ends of every line near (not only the tile's), each in its substation
    rows = []
    for k, (_, rings) in enumerate(subs):
        rp = np.ascontiguousarray(np.concatenate(rings), np.float64)
        ro = np.cumsum([0] + [len(r) for r in rings]).astype(np.int64)
        if YARDS_ON:
            # (each end in the yard layout chose for it: overlapping yards anchored one end twice)
            here = np.asarray(ends[:, 6], np.float64) == k if len(ends) else np.zeros(0, bool)
            anchors = np.ascontiguousarray(ends[here][:, [0, 1, 3, 4, 5]], np.float64).reshape(-1, 5)
        else:                           # (as before: every end in the yard, its whole kit)
            from polygon import Path
            here = np.zeros(len(ends), bool)
            if len(ends):
                for ring in rings:
                    here ^= Path(ring).contains_points(ends[:, :2])
            anchors = np.ascontiguousarray(ends[here][:, [0, 1, 3]], np.float64).reshape(-1, 3)
        got, fence = fast._ef.substation_yard(rp, ro, kits, parts, YARD_GAP, anchors, 0, list(ANCHOR_AT),
                                              YARD_MAX_KITS, FENCE_LEN, [x0, y0, x1, y1])
        rows += [(groups[int(g)], x, y, yaw, PARTS[groups[int(g)]], gz) for g, x, y, yaw, gz in got]
        rows += [("sub_fence", x, y, yaw, 1.76, np.nan) for x, y, yaw in fence]
    if not rows:
        return
    xy = np.array([[r[1], r[2]] for r in rows], np.float64)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
    for (g, x, y, yaw, h, gz), zz in zip(rows, np.asarray(z)):
        zz = gz if np.isfinite(gz) else zz
        props.append([g, round(float(x), 2), round(float(y), 2), round(float(zz), 2), round(float(yaw), 1), 1.0, h,
                      0.0])
