"""Speed-limit signs along the roads (the user, 2026-10-28: "speed limit panels could be a good fallback in case osm is
missing data"): the glue of the native kernels earthfast.speed_sign_place (where they stand: changes of the limit,
past major junctions, past motorway entrances, reminders on long stretches) and speed_sign_mesh (MUTCD R2-1 in the
US, Rb-1 in Canada, the Vienna Convention's red ring elsewhere); the rules and their sources are there
(native/earthfast/src/speedsigns.rs). Sweden's C31 stands where the Vienna signs do but is drawn here (sweden_mesh):
the agency's own artwork of the sign on a disc. Germany's zones (the German junction signs on, near Germany:
dezones.py): Zeichen 274.1 / 274.2 where a mapped zone begins and ends, in place of the round 274 there.

The value is the AI's (navgraph.road_mph: OpenStreetMap's / Overture's limit, else the class default the traffic
drives), in the units posted there (mph rounded to 5, km/h rounded to 5). Built in the sign tiles (signstream.py,
field "speeds", everywhere, not only in the US), render-only, under the highwaySigns switch.
"""
from functools import lru_cache
import threading

import numpy as np

import exitsigns
import fast
import mapcells
import navgraph
import roads
import signroads

KERNEL = 98                     # earthfast VERSION with speed_sign_place / speed_sign_mesh
GENERAL_KERNEL = 171            # ... with speed_sign_general and speed_sign_place's kind, limit before and rank
# m ahead of a speed-limit sign a German town makes a 50 its Ortstafel's (general: the mapped Ortstafel may stand
# up to MAPPED_REACH_M before the estimate's town)
GENERAL_AHEAD_M = (100.0, 200.0, 300.0)
RANK = {"motorway": 7, "trunk": 6, "primary": 5, "secondary": 4, "tertiary": 3, "unclassified": 2, "residential": 1}
MARGIN_M = 900.0                # the roads this far round a tile: its junctions and the nearby signs that thin it out
REPEAT_M = (1609.344, 3218.688)  # a reminder about every mile in town and every two out of it (PR #363's 2 miles)
CELL_FRAMES = 4                 # anchors a cell keeps its rows for
STYLE_MUTCD, STYLE_VIENNA, STYLE_CANADA, STYLE_SWEDEN = 0, 1, 2, 3     # (the kernels draw STYLE_SWEDEN as STYLE_VIENNA)
# speed_sign_mesh's material indices: white, red, metal, lettering, black (exit.materials.json's)
MATERIALS = ['bne_exit_white', 'bne_guide_red', 'bne_exit_metal', 'bne_guide_letters_black', 'bne_guide_black']
# C31 (sweden_mesh): Transportstyrelsen's artwork of the sign, tools/build_swedish_signs.py's sheet of 4 x 4 cells of
# 256 px of 1024, 30 km/h first; the disc's radius 122 px
MATERIALS_C31 = ['bne_se_signs', 'bne_exit_metal']
C31_CELL, C31_R, C31_SHEET = 256.0, 122.0, 1024.0
C31_SEGS = 32
C31_DEPTH = 0.02                # m: the plate's folded edge
C31_POLE = 0.03                 # m: as the Vienna mesh's, whose pole stands 0.05 m behind the face
C31_POLE_X = -0.05
# size class -> disc: TSFS 2019:74 4 kap. 1 § has 0.45, 0.60, 0.90, >= 1.20 m, at least normal (0.60) on ordinary roads
# and stor (0.90) on motorways, so no 0.75
C31_D = (0.60, 0.60, 0.90, 0.90)
# m from the road to the lower edge: the Vienna mesh's mount(), within 1.0-2.0 m (TSFS 2019:74 1 kap. 13 §)
C31_MOUNT_RURAL, C31_MOUNT_TOWN, C31_MIN_GROUND = 1.52, 2.0, 1.2
_lock = threading.Lock()


NO_SIGN_CLASSES = {"track"}     # in the AI's graph (navgraph.DRIVE, GitHub #756) but never signed

def posted(mph_value, mph=True, face=STYLE_VIENNA):
    """The number on the sign of a limit of mph_value mph: mph (to 5) where mph are posted, else km/h (to 5), in Sweden
    to 10 from 30 to 120 (slower: no sign); 0 for none."""
    if mph_value is None or not np.isfinite(mph_value) or mph_value <= 0:
        return 0.0
    v = mph_value if mph else mph_value * 1.609344
    if face == STYLE_SWEDEN:
        return min(120.0, float(int(v / 10.0 + 0.5) * 10)) if v >= 25.0 else 0.0
    return float(max(5, int(v / 5.0 + 0.5) * 5))


def style(country, us):
    """The sign face: MUTCD where a US state is (signstates), Canada's Rb-1, Sweden's C31, else the Vienna's."""
    if us:
        return STYLE_MUTCD
    return {"CA": STYLE_CANADA, "SE": STYLE_SWEDEN}.get((country or "").upper(), STYLE_VIENNA)


def _limits(feats, i, spec, mph, face):
    """[(fraction, posted value)] of feature i from 0 on: road_mph (the AI's) on each piece of its speed rules."""
    cuts = {0.0}
    table = feats.rules.get("max_speed_kmh")
    if table is not None:
        for r in table.rows(i):
            for f in (float(table.start[r]), float(table.end[r])):
                if 0.0 < f < 1.0:
                    cuts.add(round(f, 6))
    cuts = sorted(cuts) + [1.0]
    out = []
    country = "SE" if face == STYLE_SWEDEN else None
    for a, b in zip(cuts[:-1], cuts[1:]):
        v = posted(navgraph.road_mph(feats, i, spec, mph, at=(a + b) / 2, country=country), mph, face)
        if not out or out[-1][1] != v:
            out.append((a, v))
    return out


def _cell_rows(feats, frame, urban, widths, left_hand, mph, face=STYLE_VIENNA):
    """{fid: (lon/lat line, xy line, meta [half width, dir, rank, urban, link], [(fraction, connector)],
    [(fraction, value)], [(start, end)] off the ground, (zone km/h or 0, class without _link))} of a decoded map cell in
    frame (kept on the cell, made again when the lane shards join it later: their zones, mapdata._lanes)."""
    key = (frame.lat0, frame.lon0, frame.h0, bool(left_hand), urban, widths, bool(mph), face,
           feats.__dict__.get("_lanes_built"))
    with _lock:
        memo = feats.__dict__.setdefault('_speed_rows', {})
        got = memo.get(key)
    if got is not None:
        return got
    specs = signroads.cell_specs(feats, urban, widths, left_hand)
    bspecs = roads.bridge_specs(feats)
    con = feats.rules.get("connectors")
    sub = feats.rules.get("subclass")
    zone = feats.attrs.get("osm_zone_maxspeed")
    zone_kmh = {}
    if zone is not None:        # (Germany's zones: the lane shards' osm_zone_maxspeed, dezones.py)
        from dezones import ZONE_KMH as zone_kmh
    picked, lines = [], []
    for i in range(len(feats)):
        sp = specs[i] if specs[i] is not None else bspecs[i]
        if (sp is None or sp.cls not in navgraph.DRIVE or sp.cls in NO_SIGN_CLASSES
                or roads._rule_value(sub, i) in navgraph.SKIP_SUBCLASSES):
            continue
        parts = feats.parts(i)
        if not parts or not len(parts[0]) or len(parts[0][0]) < 2:
            continue
        picked.append((i, sp, specs[i] is None))
        lines.append(np.asarray(parts[0][0], np.float64)[:, :2])
    got = {}
    if picked:
        ll = np.concatenate(lines)
        e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
        xy = np.column_stack([e, n])
        k = 0
        for (i, sp, bridge), line in zip(picked, lines):
            pts = xy[k:k + len(line)]
            k += len(line)
            cons = [(float(con.start[r]), str(con.value[r])) for r in con.rows(i)] if con is not None else []
            skips = [(0.0, 1.0)] if bridge else [(a, b) for a, b, _ in roads.level_ranges(feats, i)]
            meta = (sp.width / 2, (int(sp.dir) or 1) if sp.oneway else 0, RANK.get(sp.cls, 0),
                    1.0 if sp.urban else 0.0, 1.0 if getattr(sp, "link", False) else 0.0)
            z = zone_kmh.get(zone[i]) if zone is not None and isinstance(zone[i], str) else None
            got[str(feats.ids[i])] = (line, pts, meta, cons, _limits(feats, i, sp, mph, face), skips,
                                      (z or 0, str(sp.cls).removesuffix("_link")))
    with _lock:
        if len(memo) >= CELL_FRAMES:
            memo.pop(next(iter(memo)))
        memo[key] = got
    return got


def _arrays(rows, box):
    """speed_sign_place's road arrays of rows (each feature once) near box (x0, y0, x1, y1, MARGIN_M round it)."""
    x0, y0, x1, y1 = box
    ll, xy, off, meta = [], [], [0], []
    con_at, con_id, con_off = [], [], [0]
    lim_at, lim_val, lim_off = [], [], [0]
    skip, skip_off = [], [0]
    ids = {}
    for line, pts, m, cons, lims, skips, *_ in rows:
        if (pts[:, 0].max() < x0 - MARGIN_M or pts[:, 0].min() > x1 + MARGIN_M or
                pts[:, 1].max() < y0 - MARGIN_M or pts[:, 1].min() > y1 + MARGIN_M):
            continue
        ll.append(line)
        xy.append(pts)
        off.append(off[-1] + len(pts))
        meta.append(m)
        for f, c in cons:
            con_at.append(f)
            con_id.append(ids.setdefault(c, len(ids)))
        con_off.append(len(con_at))
        for f, v in lims:
            lim_at.append(f)
            lim_val.append(v)
        lim_off.append(len(lim_at))
        skip.extend(skips)
        skip_off.append(len(skip))
    if not xy:
        return None
    f64 = lambda a, *shape: np.ascontiguousarray(np.asarray(a, np.float64).reshape(*shape))    # noqa: E731
    i64 = lambda a: np.ascontiguousarray(np.asarray(a, np.int64))                              # noqa: E731
    return (f64(np.concatenate(xy), -1, 2), f64(np.concatenate(ll), -1, 2), i64(off), f64(meta, -1, 5),
            f64(con_at, -1), i64(con_id), i64(con_off), f64(lim_at, -1), f64(lim_val, -1), i64(lim_off),
            f64(skip, -1, 2), i64(skip_off))


def _outlines(pavements):
    """(points (n, 2), offsets) of the pavement outlines (kept on the signroads.Pavements)."""
    got = pavements.plans.get('speed_outlines')
    if got is None:
        polys = list(pavements.outlines or [])
        pts = np.concatenate(polys) if polys else np.zeros((0, 2))
        off = np.r_[0, np.cumsum([len(p) for p in polys])].astype(np.int64)
        got = pavements.plans['speed_outlines'] = (np.ascontiguousarray(pts[:, :2], np.float64), off)
    return got


def _tile_rows(frame, box, context, left_hand, mph, face, margin=MARGIN_M):
    """The rows (_cell_rows') of the map cells within margin (m) of box, each feature once."""
    x0, y0, x1, y1 = box
    m = margin
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m]), np.array([y0 - m, y1 + m]), 0.0)
    rows, seen = [], set()
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = context.cell(key)
        if feats is None or not len(feats):
            continue
        for fid, row in _cell_rows(feats, frame, context.urban, context.widths, left_hand, mph, face).items():
            if fid not in seen:         # (a line is whole in every cell it crosses)
                seen.add(fid)
                rows.append(row)
    return rows


def place(frame, box, context, left_hand=False, mph=True, face=STYLE_VIENNA):
    """The speed-limit signs of box (x0, y0, x1, y1): (k, 12) [x, y, travel x, travel y, value, size class, road x,
    road y, urban, kind (0 a change, 1 a merge, 2 past a junction, 3 a reminder), a change's limit before it (0 not
    known), the road's rank] (speed_sign_place's)."""
    fast.require(KERNEL, "speed-limit signs")
    x0, y0, x1, y1 = box
    rows = _tile_rows(frame, box, context, left_hand, mph, face)
    arrays = _arrays(rows, box)
    if arrays is None:
        return np.zeros((0, 12))
    cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    poly, poly_off = _outlines(context.pavements(frame, np.c_[clo, cla], left_hand))
    import roundabouts
    got = fast._ef.speed_sign_place(*arrays, poly, poly_off, [float(v) for v in box], _kernel_style(face),
                                    bool(left_hand), [float(v) for v in REPEAT_M])
    return roundabouts.drop_on_islands(got, rows)          # (none on a roundabout's island, plan 038)


def _kernel_style(face):
    return STYLE_VIENNA if face == STYLE_SWEDEN else int(face)


@lru_cache(maxsize=1)
def _glyphs():
    data = exitsigns.lettering()
    chars = sorted(data['glyphs'])
    rows = [[g['advance'], *g['bbox'], *g['uv']] for g in (data['glyphs'][c] for c in chars)]
    return (np.array([ord(c) for c in chars], np.int64), np.ascontiguousarray(np.asarray(rows, np.float64)),
            float(data['capHeight']))


def c31_uv(value, cos, sin):
    """Where on the sheet a point of the sign of value km/h is, (cos, sin) of the disc's radius from its centre, seen
    from the front; v up, as cdae.encode's."""
    k = int(value) // 10 - 3
    u = ((k % 4) + 0.5) * C31_CELL + cos * C31_R
    v = ((k // 4) + 0.5) * C31_CELL - sin * C31_R
    return u / C31_SHEET, 1.0 - v / C31_SHEET


def sweden_mesh(table, origin):
    """(positions, triangles, uvs, materials, material of each triangle) of C31 signs, or None: rows as
    speed_sign_mesh's (x, y, ground z, road z, travel x, travel y, value, size class, urban). In a sign's frame, as
    the kernel's: x towards the driver, y to the driver's right, z up."""
    ang = np.arange(C31_SEGS) * 2.0 * np.pi / C31_SEGS
    cos, sin = np.cos(ang), np.sin(ang)
    P, T, UV, M = [], [], [], []
    n = 0

    def add(pts, uvs, tris, mat, row):
        nonlocal n
        pts = np.asarray(pts, np.float64)
        x, y, zg, tx, ty = row
        world = np.column_stack([x - origin[0] - tx * pts[:, 0] + ty * pts[:, 1],
                                 y - origin[1] - ty * pts[:, 0] - tx * pts[:, 1], zg - origin[2] + pts[:, 2]])
        P.append(world)
        UV.append(uvs)
        T.append(np.asarray(tris, np.int64) + n)
        M.append(np.full(len(tris), mat, np.int64))
        n += len(pts)

    for x, y, zg, zr, tx, ty, value, size, urban in table:
        value = int(round(value))
        if not (30 <= value <= 120 and np.isfinite([x, y, zg, zr]).all()):
            continue
        tl = max(np.hypot(tx, ty), 1e-9)
        row = (x, y, zg, tx / tl, ty / tl)
        size = int(np.clip(size, 0, 3))
        d = C31_D[size]
        r = d / 2.0
        mount = C31_MOUNT_TOWN if urban or size >= 2 else C31_MOUNT_RURAL
        zc = max(zr + mount, zg + C31_MIN_GROUND) - zg + r
        ring = np.column_stack([np.zeros(C31_SEGS), r * cos, zc + r * sin])
        fan = [(0, k, k + 1) for k in range(1, C31_SEGS - 1)]
        none = np.zeros((C31_SEGS, 2))
        add(ring, np.column_stack(c31_uv(value, cos, sin)), fan, 0, row)
        back = ring + (-C31_DEPTH, 0.0, 0.0)
        add(back, none, [(a, c, b) for a, b, c in fan], 1, row)
        quads = np.zeros((C31_SEGS * 4, 3))         # the edge, each facet its own corners
        nxt = (np.arange(C31_SEGS) + 1) % C31_SEGS
        quads[0::4], quads[1::4], quads[2::4], quads[3::4] = back, back[nxt], ring[nxt], ring
        add(quads, np.zeros((C31_SEGS * 4, 2)), [(i, j, k + 4 * q) for q in range(C31_SEGS)
                                                 for i, j, k in ((4 * q, 4 * q + 1, 2), (4 * q, 4 * q + 2, 3))], 1, row)
        a = np.arange(9) * np.pi / 4.0
        around = np.column_stack([C31_POLE_X + C31_POLE * np.cos(a), C31_POLE * np.sin(a)])
        top = zc + r - 0.05
        pole = np.vstack([[(px, py, -0.3), (px, py, top)] for px, py in around])
        add(pole, np.zeros((18, 2)), [t for k in range(8) for t in ((2 * k, 2 * k + 2, 2 * k + 3),
                                                                    (2 * k, 2 * k + 3, 2 * k + 1))], 1, row)
    if not n:
        return None
    return np.vstack(P), np.vstack(T), np.vstack(UV), MATERIALS_C31, np.concatenate(M)


def general(signs, towns):
    """Which signs (place's rows) to keep where Germany's general limits are not posted (earthfast
    speed_sign_general: no 50 in or before a town, no 100 out of town but where a lower limit ends; with the town
    signs on, whose Ortstafeln start and end the 50); towns: townentry.Towns.town_state's."""
    fast.require(GENERAL_KERNEL, "Germany's general speed limits")
    xy, travel = signs[:, 0:2], signs[:, 2:4]
    here = np.asarray(towns(xy), bool).reshape(-1, 2)
    ahead = np.zeros(len(signs), bool)
    for d in GENERAL_AHEAD_M:
        ahead |= np.asarray(towns(xy + travel * d), bool).reshape(-1, 2)[:, 0]
    town = np.ascontiguousarray(np.c_[here[:, 0], ahead, here[:, 1]])
    return np.asarray(fast._ef.speed_sign_general(np.ascontiguousarray(signs, np.float64), town), bool)


def build_tile(frame, tx, ty, elev, context, decks=(), left_hand=False, mph=True, face=STYLE_VIENNA, de_on=False,
               towns=None):
    """The mesh (signstream.write_tile's) of road tile (tx, ty)'s speed-limit signs, or None (none). de_on (the German
    junction signs, designs.enabled's): near Germany its zone signs too, in one mesh, and not the 274s they replace
    (dezones.tile); otherwise today's. towns: the town signs' towns (townentry.Towns.town_state) when they are on:
    Germany's general limits are then not posted (general)."""
    box = roads.tile_bbox(tx, ty)
    signs = place(frame, box, context, left_hand, mph, face)
    if towns is not None and len(signs) and not mph:
        signs = signs[general(signs, towns)]
    extra = None
    if de_on:                   # (Germany's zone signs: dezones.py)
        import dezones
        signs, extra = dezones.tile(frame, box, elev, context, signs, decks, left_hand, mph)
    mesh = _speed_mesh(frame, box, elev, signs, decks, face)
    return mesh if extra is None else dezones._merged(mesh, extra)


def _speed_mesh(frame, box, elev, signs, decks, face):
    """The mesh of place()'s signs of box, or None."""
    keep = [k for k in range(len(signs)) if not exitsigns._on_deck(signs[k, :2], decks, 1.5)]
    if not keep:
        return None
    signs = signs[keep]
    pts = np.vstack([signs[:, 0:2], signs[:, 6:8]])
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    h = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).ravel()
    n = len(signs)
    table = np.column_stack([signs[:, 0:2], z[:n], z[n:], signs[:, 2:4], signs[:, 4:6], signs[:, 8]])
    if face == STYLE_SWEDEN:
        return sweden_mesh(table, (float(box[0]), float(box[1]), 0.0))
    cp, glyph, cap = _glyphs()
    p, t, uv, mi = fast._ef.speed_sign_mesh(np.ascontiguousarray(table), _kernel_style(face), cp, glyph, cap,
                                            [float(box[0]), float(box[1]), 0.0])
    if not len(t):
        return None
    return p, t, uv, MATERIALS, mi
