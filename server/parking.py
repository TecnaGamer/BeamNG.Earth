"""Parking spots for BeamNG's own parked cars (gameplay_parking, the player's traffic settings): curbside
spots along town and city streets, from the sidewalk runs of a road tile (roads.build_tile), and rows of
spots in the mapped parking lots (Overture infrastructure polygons of class "parking").

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
        if spec.roundabout or spec.roundabout_entry or not spec.urban or spec.cls not in PARK_CLASSES or spec.width < MIN_WIDTH_M:
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
            if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
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
                if x0 <= q[0] < x1 and y0 <= q[1] < y1:
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


def lot_rings(frame, feats_list):
    """The parking lot polygons (outer rings) of Overture infrastructure Features, in the anchor frame."""
    out, seen = [], set()
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        cls = f.attrs.get("class")
        if cls is None:
            continue
        for i in np.flatnonzero(np.isin(cls.astype(str), list(LOT_CLASSES)) & (f.kind == 3)):
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
                if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
                    continue
                if not furniture._clear(np.array([q[0], q[1], p[2]]), others, fid):
                    continue
                if any(pa.contains_point(q) for pa in paths):
                    continue
                out.append([round(float(q[0]), 2), round(float(q[1]), 2), round(float(p[2]), 2),
                            round(math.degrees(math.atan2(nn[1], nn[0])) % 360.0, 1), 1])
    return out
