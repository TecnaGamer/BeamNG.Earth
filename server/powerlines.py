"""High-voltage power lines (the user, 2026-10-26, on "a map of all power lines and how they connect": "start with
1"): OpenStreetMap's power=line ways (Overture infrastructure class power_line; underground ones left out) as the
stock lattice tower (Automation Test Track's / West Coast USA's pylon, 35.8 m: trees.lua group "pylon") at each of
their vertices (the mapped towers) with six conductors (its double circuit: under the insulators of its three cross-arms)
hung between them (the road tile's wire mesh: furniture.wire_mesh), ending at the gantries of a yard kit where they
reach a substation (Overture infrastructure class substation areas; no towers inside them); the substations' yards filled with
West Coast USA's switchgear (its yard's parts as a kit, a smaller one where it does not fit) and fenced with chain link.
The layouts in Rust (earthfast.power_line_layout, earthfast.substation_yard).
"""
import numpy as np

import fast

G_PYLON = "pylon"
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


def _margin(pts, lines):
    """Per point (k, 2) its distance to the nearest road's carriageway edge (drive_lines' (centreline, half width))
    less TOWER_ROAD_CLEAR_M: negative too close to some road."""
    out = np.full(len(pts), np.inf)
    for line, hw in lines:
        a, ab = line[:-1], line[1:] - line[:-1]
        ap = pts[:, None, :] - a[None]
        t = np.clip((ap * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-12), 0.0, 1.0)
        d = np.hypot(*(ap - t[..., None] * ab).transpose(2, 0, 1)).min(1)
        out = np.minimum(out, d - hw - TOWER_ROAD_CLEAR_M)
    return out


def _off_roads(frame, md, e, n, ll):
    """The line vertices (e, n: frame m; ll: their lon/lat) moved off the public roads' carriageways: one closer than
    half the carriageway + TOWER_ROAD_CLEAR_M to a road's centreline goes straight away from the nearest to that
    distance; where that is too close to another road (between a dual carriageway's two, in a junction's corner), to
    the nearest spot within CLEAR_SEARCH_M clear of them all (rings CLEAR_STEP_M apart, CLEAR_DIRS spots each), else
    to the one farthest off them. Each vertex reads the roads of its own map cell and the 8 around it, so every road
    tile (and every process) moves it alike: the same towers and wires on both sides of a tile edge."""
    import gasstations
    import mapcells
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
        p = np.array([e[i], n[i]])
        if not lines or _margin(p[None], lines)[0] >= 0.0:
            continue
        # straight away from the nearest road
        best = None
        for line, hw in lines:
            a, b = line[:-1], line[1:]
            ab = b - a
            t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
            foot = a + t[:, None] * ab
            d = np.hypot(*(p - foot).T)
            k = int(np.argmin(d))
            if best is None or d[k] - hw < best[0]:
                away = (p - foot[k]) / d[k] if d[k] > 1e-6 else np.array([-ab[k, 1], ab[k, 0]]) / max(
                    float(np.hypot(*ab[k])), 1e-9)
                best = (d[k] - hw, foot[k] + away * (hw + TOWER_ROAD_CLEAR_M))
        cand = np.vstack([best[1][None], p + around])
        m = _margin(cand, lines)
        ok = np.flatnonzero(m >= -1e-6)
        e[i], n[i] = cand[ok[0] if len(ok) else int(np.argmax(m))]
    return e, n


def tile_lines(frame, tx, ty, md, elev, props, wires):
    """Add road tile (tx, ty)'s towers, gantries, substation yards (props rows, furniture.place's form) and conductors
    (wires polylines) of the power lines near it; elev(lat, lon): the ground."""
    import roads
    from polygon import Path
    if md is None:
        return
    x0, y0, x1, y1 = roads.tile_bbox(tx, ty)
    cells = roads.cells_of_tile(frame, tx, ty, margin=MAX_SPAN_M / 4)
    lines, seen = [], set()
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
            for part in f.parts(i):
                for ring in part:
                    if len(ring) >= 2:
                        lines += [(fid, r) for r in _near(frame, np.asarray(ring, np.float64), (x0, y0, x1, y1))]
    subs = _substations(frame, cells, md)
    ends = np.zeros((0, 4))
    fast.require(82, "power_line_layout")
    if lines:
        lines.sort(key=lambda q: q[0])          # (the same order in every tile: shared towers decided alike)
        ll = np.concatenate([r for _, r in lines])
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
        e, n = _off_roads(frame, md, np.asarray(e, np.float64), np.asarray(n, np.float64), ll)
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
        towers, ends, wp, wo = fast._ef.power_line_layout(pts, off, stop, ARMS, SAG, SEG_M, MIN_SPAN_M, MAX_SPAN_M,
                                                          DEDUPE_M, END_MERGE_M, GANTRY_FRONT, [x0, y0, x1, y1])
        for x, y, zz, yaw in towers:
            props.append([G_PYLON, round(float(x), 2), round(float(y), 2), round(float(zz), 2), round(float(yaw), 1),
                          1.0, PYLON_H, 0.0])
        for a, b in zip(wo[:-1], wo[1:]):
            wires.append((np.asarray(wp[a:b]), WIRE_R))
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
