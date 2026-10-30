"""Parking spots for BeamNG's own parked cars (gameplay_parking, the player's traffic settings): curbside
spots along town and city streets, from the sidewalk runs of a road tile (roads.build_tile), and rows of
spots in the mapped parking lots (Overture infrastructure polygons of class "parking").

Stall markings (lot_markings, aisle_markings): the same rows, drawn as white lines on the ground the way
lane lines are (a line_white ribbon in the road mesh). A mapped lot (class "parking", unless its surface
is grass or gravel) gets a divider and a line across the head of each stall. A lot that the map gives
only as its aisles (road subclass parking_aisle) gets the same lines along both sides of each aisle.
The asphalt under them is the ground itself (surface.paint_lots, surface.photo_lots).

The stock system spawns its parked cars onto the spots of a sites object and moves them to spots near the
player as the player drives (the mod's parking.lua feeds it these). Each spot is a row [x, y, z, heading]:
the anchor frame's position of the spot's centre on the road surface, and the direction the parked car
faces (degrees, counter-clockwise from +x); lot spots carry a fifth value, 1 (the mod tags them as lot
spots: either way in, no "street" tracking). Cars park along the curb facing the traffic on their side
(right-hand traffic). Deterministic: the stations are arc lengths along the road, as furniture.py's.
"""
import math

import numpy as np
from polygon import Path

import furniture

PARK_CLASSES = {"residential", "tertiary", "secondary", "unclassified", "living_street"}
SPOT_LEN_M = 6.5            # a spot's length along the curb (US parallel parking: 6-7 m)
SPOT_W_M = 2.5              # the stock spot size (city.sites.json: 2.5 x 6 x 3)
CURB_OFF_M = 1.25           # the spot's centre this far from the curb into the road
MIN_WIDTH_M = 7.0           # narrower streets: no parking lane
BOTH_SIDES_M = 9.0          # wider streets park on both sides, narrower ones on one (hashed per street)
STREET_SHARE = 0.85         # the share of eligible streets with curbside parking (hashed per street)
CORNER_CLEAR_M = 9.0        # m kept clear of a run's ends (corners, crosswalks; US: 20 ft from a crosswalk)
HYDRANT_CLEAR_M = 4.6       # m kept clear of a hydrant (US: 15 ft)
DRIVE_CLEAR_M = 4.2         # m from a driveway's mouth to a spot's centre




def _owns(box, x, y):
    """Is (x, y) the tile's own point (geotiles.box_owns: a geotile's exactly, an anchor tile's half-open box)?"""
    import geotiles
    return geotiles.box_owns(box, x, y)

def curb_spots(roads, walks, box, left_hand=False, directions=None):
    """Rows [x, y, z, heading] of the curbside spots of a tile box (x0, y0, x1, y1). roads: build_tile's
    (fid, spec, centreline (k, 3), lon); walks: its sidewalk runs (dicts with fid, pts, side, w). left_hand:
    traffic drives on the left (the car's left side to the curb). directions: fid -> +1 / -1, a one-way street's
    traffic along / against its geometry (Overture's one-way rule; +1 when unknown)."""
    x0, y0, x1, y1 = box
    by_fid = {fid: (spec, pts) for fid, spec, pts, _ in roads}
    out = []
    for wk in walks:
        fid = wk.get("fid")
        if fid is None or "pts" not in wk or fid not in by_fid:
            continue
        spec, line = by_fid[fid]
        if not spec.urban or spec.cls not in PARK_CLASSES or spec.width < MIN_WIDTH_M:
            continue
        if furniture._h01(fid, "park") >= STREET_SHARE:
            continue
        side = wk["side"]
        if spec.width < BOTH_SIDES_M and side != (1 if furniture._h01(fid, "parkside") < 0.5 else -1):
            continue
        run = np.asarray(wk["pts"], np.float64)
        if len(run) < 2:
            continue
        w = wk.get("w", 2.5)
        phase = furniture._h01(fid, side, "parkph") * SPOT_LEN_M
        for p, away in furniture._stations(run, line, SPOT_LEN_M, phase, CORNER_CLEAR_M, side):
            q = p[:2] - away * (w / 2 + CURB_OFF_M)          # from the sidewalk's middle past the curb
            if not (_owns(box, q[0], q[1])):
                continue
            # the car's right side to the curb: heading h with right(h) = (h_y, -h_x) = away
            if spec.oneway:
                _, d = furniture._at(line, furniture._project(line, q[None]))
                h = d[0] * (-1.0 if (directions or {}).get(fid, 1) < 0 else 1.0)
            elif left_hand:             # left(h) = (-h_y, h_x) = away
                h = np.array([away[1], -away[0]])
            else:
                h = np.array([-away[1], away[0]])
            out.append([round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                        round(math.degrees(math.atan2(h[1], h[0])) % 360.0, 1)])
    return out


def clear_of(spots, props, drives):
    """The spots not in front of a hydrant (props rows of group furniture.G_HYDRANT) or a driveway's mouth
    (drives: buildings.doors' driveway chunks, "n" points from the house to the road's edge)."""
    if not spots:
        return spots
    block = [(r[1], r[2], HYDRANT_CLEAR_M) for r in (props or ()) if r[0] == furniture.G_HYDRANT]
    block += [(d["n"][-1][0], d["n"][-1][1], DRIVE_CLEAR_M) for d in (drives or ()) if d.get("n")]
    if not block:
        return spots
    b = np.array(block, np.float64)
    s = np.array([r[:2] for r in spots], np.float64)
    dist = np.hypot(s[:, None, 0] - b[None, :, 0], s[:, None, 1] - b[None, :, 1])
    keep = (dist >= b[None, :, 2]).all(1)
    return [r for r, k in zip(spots, keep) if k]


LOT_SPOT_W_M = 2.6          # a lot's stall: width along its row ...
LOT_SPOT_D_M = 5.5          # ... and depth (US: 8.5-9 ft x 18 ft)
LOT_AISLE_M = 7.0           # the driving aisle between rows (two-way, 90-degree stalls: ~24 ft)
LOT_MARGIN_M = 0.8          # kept in from the lot's edge
LOT_MAX_PER_TILE = 500
LOT_CLASSES = {"parking"}   # (surface.PARKING_CLASSES: the lots painted asphalt)
# a mapped surface that is not a paved lot (OSM surface= on amenity=parking): no asphalt, no paint
LOT_UNPAVED = {"unpaved", "ground", "dirt", "earth", "mud", "grass", "sand", "soil", "gravel",
               "fine_gravel", "compacted", "woodchips", "grass_paver", "pebblestone"}
# stall paint, the road mesh's line layer (roads.EDGE_LINE's material: solid white, over the asphalt)
STALL_LINE = {"m": "line_white", "tl": 6.4, "p": 2, "w": 0.10}
MIN_MARK_M = 0.4            # a clipped stall line shorter than this is dropped
MARK_EDGE_M = 1e-3          # the tile box is half-open: a line on the far edge belongs to the next tile


def _lot_rows(hw):
    """Offsets across a lot (from its -v edge, which is at -hw) of its stall rows' centres and the side their
    noses point (-1: toward -v), for a lot 2 * hw wide: a single row along the -v edge, then double rows
    back to back, an aisle before each."""
    out = []
    o = LOT_MARGIN_M + LOT_SPOT_D_M / 2
    width = 2 * hw - LOT_MARGIN_M
    if o + LOT_SPOT_D_M / 2 > width:
        return out
    out.append((o, -1))
    o += LOT_SPOT_D_M / 2 + LOT_AISLE_M + LOT_SPOT_D_M / 2
    while o + LOT_SPOT_D_M / 2 <= width:
        out.append((o, 1))
        if o + LOT_SPOT_D_M * 1.5 <= width:
            out.append((o + LOT_SPOT_D_M, -1))
        o += 2 * LOT_SPOT_D_M + LOT_AISLE_M
    return out


def lot_spots(rings, box, z_at, blockers=()):
    """Rows [x, y, z, heading, 1] of the stalls of parking lots (rings: (k, 2) arrays in the anchor frame)
    whose centres are in the tile box: rows along each lot's long side, every stall wholly inside the lot and
    its centre in none of the blockers (building footprints: a garage's ground floor). z_at(x, y) -> the
    ground (arrays). Deterministic."""
    import buildings
    x0, y0, x1, y1 = box
    cand = []
    for ring in rings:
        ring = np.asarray(ring, np.float64)
        if len(ring) < 3:
            continue
        if ring[:, 0].max() < x0 or ring[:, 0].min() >= x1 or ring[:, 1].max() < y0 or ring[:, 1].min() >= y1:
            continue
        c, u, hl, hw = buildings.oriented_box(ring)
        if hw < LOT_SPOT_D_M / 2 or hl < LOT_SPOT_W_M:
            continue
        v = np.array([-u[1], u[0]])
        path = Path(ring)
        n_along = int((2 * hl - 2 * LOT_MARGIN_M) // LOT_SPOT_W_M)
        if n_along < 1:
            continue
        along = (np.arange(n_along) - (n_along - 1) / 2) * LOT_SPOT_W_M
        for off, nose in _lot_rows(hw):
            centres = c[None] + along[:, None] * u[None] + (off - hw) * v[None]
            hx, hy = u * (LOT_SPOT_W_M / 2 - 0.1), v * (LOT_SPOT_D_M / 2 - 0.1)
            ok = np.ones(len(centres), bool)
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                ok &= path.contains_points(centres + sx * hx + sy * hy)
            heading = math.degrees(math.atan2(nose * v[1], nose * v[0])) % 360.0
            for q in centres[ok]:
                if _owns(box, q[0], q[1]):
                    cand.append((float(q[0]), float(q[1]), heading))
    if not cand:
        return []
    pts = np.array([(x, y) for x, y, _ in cand])
    keep = np.ones(len(cand), bool)
    for b in blockers:
        b = np.asarray(b, np.float64)
        if len(b) >= 3:
            keep &= ~Path(b).contains_points(pts)
    cand = [cnd for cnd, k in zip(cand, keep) if k][:LOT_MAX_PER_TILE]
    if not cand:
        return []
    z = np.asarray(z_at(np.array([x for x, _, _ in cand]), np.array([y for _, y, _ in cand])), np.float64)
    return [[round(x, 2), round(y, 2), round(float(zz), 2), round(h, 1), 1] for (x, y, h), zz in zip(cand, z)]


def lot_unpaved(feats, i):
    """A parking polygon whose mapped surface is not a hard lot (grass, gravel, dirt): no asphalt, no stall
    lines. No surface mapped: a paved lot, as the ones the map leaves blank."""
    surf = feats.attrs.get("surface")
    if surf is None:
        return False
    return str(surf[i] or "").strip().lower() in LOT_UNPAVED


def lot_rings(frame, feats_list, paved_only=False):
    """The parking lot polygons (outer rings) of Overture infrastructure Features, in the anchor frame.
    paved_only: grass and gravel lots left out (the stall lines)."""
    out, seen = [], set()
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        for i in np.flatnonzero(np.isin(cls.astype(str), list(LOT_CLASSES)) & (f.kind == 3)):
            if paved_only and lot_unpaved(f, i):
                continue
            fid = f.ids[i]
            if fid in seen:
                continue
            seen.add(fid)
            for part in f.parts(i):
                if part and len(part[0]) >= 3:
                    ll = np.asarray(part[0], np.float64)
                    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                    out.append(np.stack([e, n], 1))
    return out


def tile_lot_spots(frame, tx, ty, md, elev, items=None, existing=None):
    """existing (the tile's other spots) with its lot stalls (aisle_spots' rows, kind 1) inside a building or a
    mapped lot dropped, plus lot_spots of road tile (tx, ty): the lots of the map cells under it (md:
    mapdata.MapData), the tile's buildings (items: buildings.road_tile_items' result, rings tile-local) as blockers,
    elev(lat, lon) the ground."""
    import roads
    existing = list(existing or [])
    box = roads.tile_bbox(tx, ty)
    blockers = [spec.ring + np.array(box[:2]) for spec, *_ in (items or ())]
    try:
        feats = [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                 for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
        rings = lot_rings(frame, feats)
    except Exception:
        rings = []
    paths = [Path(np.asarray(b, np.float64)) for b in blockers + rings if len(b) >= 3]
    if paths and existing:
        pts = np.array([r[:2] for r in existing], np.float64)
        drop = np.zeros(len(existing), bool)
        for pa in paths:
            drop |= pa.contains_points(pts)
        existing = [r for r, d in zip(existing, drop) if not (d and len(r) > 4 and r[4] == 1)]
    if not rings:
        return existing

    def z_at(x, y):                 # the anchor frame's up, as the ground drawn (the Earth's curve: raw heights stood a
        la, lo, _ = frame.enu_to_geodetic(x, y, 0.0)    # spot ~6 m up 10 km from the anchor, 18 m at 15 km)
        return frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]
    return existing + lot_spots(rings, box, z_at, blockers)


AISLE_END_M = 6.0           # stalls kept this far from an aisle's ends (its junctions)


def aisle_spots(roads, aisles, box, blockers=(), lots=()):
    """Rows [x, y, z, heading, 1] of 90-degree stalls along both sides of the parking aisles (aisles: their
    fids; Overture maps many lots by their aisles alone), LOT_SPOT_W_M apart at stations of the aisle, clear
    of its ends, of other roads, of buildings (blockers) and of mapped lots (lots: lot_spots covers those);
    nose in, away from the aisle."""
    x0, y0, x1, y1 = box
    import fast
    others = fast.Others([(fid, spec.width / 2, pts) for fid, spec, pts, _ in roads if fid not in aisles])
    paths = [Path(np.asarray(b, np.float64)) for b in list(blockers) + list(lots) if len(b) >= 3]
    out = []
    for fid, spec, line, _ in roads:
        if fid not in aisles or len(line) < 2:
            continue
        L = furniture._arclen(line)[-1]
        if L < 2 * AISLE_END_M + LOT_SPOT_W_M:
            continue
        phase = furniture._h01(fid, "aisle") * LOT_SPOT_W_M
        st = np.arange(AISLE_END_M + phase, L - AISLE_END_M, LOT_SPOT_W_M)
        if not len(st):
            continue
        P, D = furniture._at(line, st)
        for side in (1.0, -1.0):
            n = np.stack([-D[:, 1], D[:, 0]], 1) * side
            Q = P[:, :2] + n * (spec.width / 2 + LOT_SPOT_D_M / 2)
            for q, nn, p in zip(Q, n, P):
                if not (_owns(box, q[0], q[1])):
                    continue
                if not furniture._clear(np.array([q[0], q[1], p[2]]), others, fid):
                    continue
                if _hit(paths, q):
                    continue
                out.append([round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                            round(math.degrees(math.atan2(nn[1], nn[0])) % 360.0, 1), 1])
    return out


def _hit(paths, q):
    """Does q lie in any of the polygons (Path.contains_points; one point)?"""
    if not paths:
        return False
    pt = np.atleast_2d(np.asarray(q, np.float64))
    return any(bool(np.asarray(pa.contains_points(pt)).reshape(-1)[0]) for pa in paths)


def _groups(ok):
    """Inclusive index ranges of consecutive True values in ok."""
    idx = np.flatnonzero(ok)
    if not len(idx):
        return []
    groups, start, prev = [], int(idx[0]), int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i == prev + 1:
            prev = i
        else:
            groups.append((start, prev))
            start = prev = i
    groups.append((start, prev))
    return groups


def _clip_seg(a, b, box):
    """The part of segment a..b inside box (x0, y0, x1, y1), or None. a and b are (x, y) or (x, y, z);
    z is lerped. Cohen-Sutherland."""
    x0, y0, x1, y1 = box
    ax, ay, bx, by = float(a[0]), float(a[1]), float(b[0]), float(b[1])
    za = float(a[2]) if len(a) > 2 else 0.0
    zb = float(b[2]) if len(b) > 2 else 0.0
    oax, oay, obx, oby, oza, ozb = ax, ay, bx, by, za, zb

    def code(x, y):
        c = 0
        if x < x0:
            c |= 1
        elif x > x1:
            c |= 2
        if y < y0:
            c |= 4
        elif y > y1:
            c |= 8
        return c

    ca, cb = code(ax, ay), code(bx, by)
    for _ in range(8):
        if not (ca | cb):
            dx, dy = obx - oax, oby - oay
            den = dx * dx + dy * dy

            def at(x, y, z0):
                if den < 1e-12:
                    return z0
                t = float(np.clip(((x - oax) * dx + (y - oay) * dy) / den, 0.0, 1.0))
                return oza + t * (ozb - oza)

            return (ax, ay, at(ax, ay, oza)), (bx, by, at(bx, by, ozb))
        if ca & cb:
            return None
        c = ca or cb
        if c & 8:
            x = ax + (bx - ax) * (y1 - ay) / (by - ay or 1e-12)
            y = y1
        elif c & 4:
            x = ax + (bx - ax) * (y0 - ay) / (by - ay or 1e-12)
            y = y0
        elif c & 2:
            y = ay + (by - ay) * (x1 - ax) / (bx - ax or 1e-12)
            x = x1
        else:
            y = ay + (by - ay) * (x0 - ax) / (bx - ax or 1e-12)
            x = x0
        if c == ca:
            ax, ay, ca = x, y, code(x, y)
        else:
            bx, by, cb = x, y, code(x, y)
    return None


def _line_chunk(a, b):
    return {"m": STALL_LINE["m"], "w": round(STALL_LINE["w"], 2), "tl": STALL_LINE["tl"], "p": STALL_LINE["p"],
            "n": [[round(float(a[0]), 3), round(float(a[1]), 3), round(float(a[2]), 3)],
                  [round(float(b[0]), 3), round(float(b[1]), 3), round(float(b[2]), 3)]]}


def _boxed_chunks(segs, box):
    """2-point line chunks of segs [((x, y, z), (x, y, z))] clipped to the half-open tile box."""
    x0, y0, x1, y1 = box
    if x1 - x0 <= MARK_EDGE_M or y1 - y0 <= MARK_EDGE_M:
        return []
    closed = (x0, y0, x1 - MARK_EDGE_M, y1 - MARK_EDGE_M)
    out = []
    for a, b in segs:
        got = _clip_seg(a, b, closed)
        if got is None:
            continue
        p, q = got
        if math.hypot(q[0] - p[0], q[1] - p[1]) < MIN_MARK_M:
            continue
        out.append(_line_chunk(p, q))
    return out


def _row_segs(c, u, v, along, off, hw, nose, ok, back):
    """Divider segments and (back) the line across the heads of one stall row. ok: which stalls stand."""
    nose_vec = nose * v
    depth, side = LOT_SPOT_D_M / 2, LOT_SPOT_W_M / 2
    segs = []

    def edge(i, which):
        base = c + (along[i] + which * side) * u + (off - hw) * v
        p0 = base - nose_vec * depth
        p1 = base + nose_vec * depth
        return (float(p0[0]), float(p0[1]), 0.0), (float(p1[0]), float(p1[1]), 0.0)

    for a, b in _groups(ok):
        ends = [edge(a, -1)[1]]
        segs.append(edge(a, -1))
        for i in range(a, b + 1):
            e = edge(i, 1)
            segs.append(e)
            ends.append(e[1])
        if back and len(ends) >= 2:
            segs.append((ends[0], ends[-1]))
    return segs


def lot_markings(rings, box, z_at, blockers=()):
    """line_white chunks of the stall lines of parking lots (the rows lot_spots parks cars in): a divider
    between stalls and a line across their heads, each stall wholly inside its lot and its centre in none of
    the blockers (a garage). A back-to-back pair shares one head line. Clipped to the tile box. z_at(x, y)
    the ground. Deterministic."""
    import buildings
    x0, y0, x1, y1 = box
    pad = LOT_SPOT_D_M
    ex0, ey0, ex1, ey1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    segs, n_stalls = [], 0
    blocks = [Path(np.asarray(b, np.float64)) for b in blockers if len(np.asarray(b)) >= 3]
    for ring in rings:
        if n_stalls >= LOT_MAX_PER_TILE:
            break
        ring = np.asarray(ring, np.float64)
        if len(ring) < 3:
            continue
        if ring[:, 0].max() < x0 or ring[:, 0].min() >= x1 or ring[:, 1].max() < y0 or ring[:, 1].min() >= y1:
            continue
        c, u, hl, hw = buildings.oriented_box(ring)
        if hw < LOT_SPOT_D_M / 2 or hl < LOT_SPOT_W_M:
            continue
        v = np.array([-u[1], u[0]])
        path = Path(ring)
        n_along = int((2 * hl - 2 * LOT_MARGIN_M) // LOT_SPOT_W_M)
        if n_along < 1:
            continue
        along = (np.arange(n_along) - (n_along - 1) / 2) * LOT_SPOT_W_M
        rows = _lot_rows(hw)
        paired = {round(off + LOT_SPOT_D_M, 3) for off, nose in rows if nose > 0}
        for off, nose in rows:
            if n_stalls >= LOT_MAX_PER_TILE:
                break
            centres = c[None] + along[:, None] * u[None] + (off - hw) * v[None]
            hx, hy = u * (LOT_SPOT_W_M / 2 - 0.1), v * (LOT_SPOT_D_M / 2 - 0.1)
            ok = np.ones(len(centres), bool)
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                ok &= path.contains_points(centres + sx * hx + sy * hy)
            ok &= (centres[:, 0] >= ex0) & (centres[:, 0] < ex1) & (centres[:, 1] >= ey0) & (centres[:, 1] < ey1)
            for pa in blocks:
                ok &= ~pa.contains_points(centres)
            room = LOT_MAX_PER_TILE - n_stalls
            if int(ok.sum()) > room:
                idx = np.flatnonzero(ok)
                ok[:] = False
                ok[idx[:room]] = True
            n_stalls += int(ok.sum())
            if not ok.any():
                continue
            segs.extend(_row_segs(c, u, v, along, off, hw, nose, ok, round(off, 3) not in paired))
    chunks = _boxed_chunks(segs, box)
    if not chunks:
        return []
    pts = np.array([(n[0], n[1]) for c in chunks for n in c["n"]], np.float64)
    z = np.asarray(z_at(pts[:, 0], pts[:, 1]), np.float64)
    for i, c in enumerate(chunks):
        c["n"][0][2] = round(float(z[2 * i]), 3)
        c["n"][1][2] = round(float(z[2 * i + 1]), 3)
    return chunks


def _aisle_side_segs(line, spec, st, keep, side):
    """Divider and head-line segments along one side of an aisle, for the stations keep marks."""
    reach0, reach1 = spec.width / 2, spec.width / 2 + LOT_SPOT_D_M
    half = LOT_SPOT_W_M / 2
    segs = []
    for a, b in _groups(keep):
        ends = np.array([st[a] - half] + [st[i] + half for i in range(a, b + 1)], np.float64)
        ends = np.clip(ends, 0.0, furniture._arclen(line)[-1])
        P, D = furniture._at(line, ends)
        nrm = np.stack([-D[:, 1], D[:, 0]], 1) * side
        inner = P[:, :2] + nrm * reach0
        outer = P[:, :2] + nrm * reach1
        for i in range(len(ends)):
            z = float(P[i, 2])
            segs.append(((float(inner[i, 0]), float(inner[i, 1]), z), (float(outer[i, 0]), float(outer[i, 1]), z)))
        for i in range(len(ends) - 1):
            segs.append(((float(outer[i, 0]), float(outer[i, 1]), float(P[i, 2])),
                         (float(outer[i + 1, 0]), float(outer[i + 1, 1]), float(P[i + 1, 2]))))
    return segs


def aisle_markings(roads, aisles, box, blockers=(), lots=()):
    """line_white chunks of the stall lines along both sides of the parking aisles (aisles: their fids),
    the rows aisle_spots parks in: clear of the aisle's ends, of other roads, of buildings and of mapped
    lots. Nose away from the aisle. Clipped to the tile box. Deterministic."""
    x0, y0, x1, y1 = box
    import fast
    others = fast.Others([(fid, spec.width / 2, pts) for fid, spec, pts, _ in roads if fid not in aisles])
    paths = [Path(np.asarray(b, np.float64)) for b in list(blockers) + list(lots) if len(np.asarray(b)) >= 3]
    pad = LOT_SPOT_D_M
    ex0, ey0, ex1, ey1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    segs, n_stalls = [], 0
    for fid, spec, line, _ in roads:
        if n_stalls >= LOT_MAX_PER_TILE or fid not in aisles or len(line) < 2:
            continue
        L = furniture._arclen(line)[-1]
        if L < 2 * AISLE_END_M + LOT_SPOT_W_M:
            continue
        phase = furniture._h01(fid, "aisle") * LOT_SPOT_W_M
        st = np.arange(AISLE_END_M + phase, L - AISLE_END_M, LOT_SPOT_W_M)
        if not len(st):
            continue
        P, D = furniture._at(line, st)
        for side in (1.0, -1.0):
            if n_stalls >= LOT_MAX_PER_TILE:
                break
            n = np.stack([-D[:, 1], D[:, 0]], 1) * side
            Q = P[:, :2] + n * (spec.width / 2 + LOT_SPOT_D_M / 2)
            keep = np.ones(len(st), bool)
            for i, (q, p) in enumerate(zip(Q, P)):
                if not (ex0 <= q[0] < ex1 and ey0 <= q[1] < ey1):
                    keep[i] = False
                    continue
                if not furniture._clear(np.array([q[0], q[1], p[2]]), others, fid):
                    keep[i] = False
                    continue
                if _hit(paths, q):
                    keep[i] = False
            room = LOT_MAX_PER_TILE - n_stalls
            if int(keep.sum()) > room:
                idx = np.flatnonzero(keep)
                keep[:] = False
                keep[idx[:room]] = True
            n_stalls += int(keep.sum())
            if keep.any():
                segs.extend(_aisle_side_segs(line, spec, st, keep, side))
    return _boxed_chunks(segs, box)


def drop_covered(chunks, rings):
    """The line chunks whose midpoint lies in none of the rings (a mapped lot or a building: that ground
    is marked by lot_markings, or is indoors)."""
    paths = [Path(np.asarray(r, np.float64)) for r in rings if len(np.asarray(r)) >= 3]
    if not paths:
        return list(chunks or [])
    kept = []
    for ch in chunks or ():
        n = ch.get("n") or []
        if len(n) < 2:
            continue
        mid = ((n[0][0] + n[1][0]) / 2.0, (n[0][1] + n[1][1]) / 2.0)
        if _hit(paths, mid):
            continue
        kept.append(ch)
    return kept


def finish_markings(frame, tx, ty, md, elev, items=None, marks=()):
    """A road tile's stall-line chunks: the aisle lines (marks, from build_tile) that are not inside a
    building or a mapped lot, plus lot_markings of the paved lots under the tile. elev(lat, lon) the ground."""
    import roads
    box = roads.tile_bbox(tx, ty)
    blockers = [spec.ring + np.array(box[:2]) for spec, *_ in (items or ()) if getattr(spec, "ring", None) is not None]
    try:
        feats = [md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
                 for k in roads.cells_of_tile(frame, tx, ty, margin=0.0)]
        rings = lot_rings(frame, feats, paved_only=True)
    except Exception:
        rings = []

    def z_at(x, y):
        la, lo, _ = frame.enu_to_geodetic(np.asarray(x, np.float64), np.asarray(y, np.float64), 0.0)
        return frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]

    return drop_covered(marks, blockers + rings) + lot_markings(rings, box, z_at, blockers)
