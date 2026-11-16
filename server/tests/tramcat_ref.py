"""REFERENCE: tramcat.py as it was before its port to Rust (plan 046, native/earthfast/src/tram/): the oracle of
test_tramcat_golden.py, not used by the server.

Tram overhead line: poles and the contact wire along electrified street trams (BNE_TRAM=1 with BNE_CATENARY=1).

catenary.py's lines are the main-line kinds (railways.parts leaves the ground trams to rails.py); a tram has no messenger
wire and, in a street, no mast of its own beside the track. The way a street tram is wired (Prague, Vinnytsia: the user,
2026-11-06, "where the tracks are in the middle of the road the poles are usually on the pavements or the buildings"):
  - the track lies in the street (within tramtrack.STREET_MARGIN_M of a carriageway): a pole on the pavement each side, or
    where a building stands there a fixing on its wall (no pole), a span wire across between them and the contact wire
    hung from it over the track's axis (a hanger). One side only where the other has no room: the span wire from there;
  - the track runs on its own, beside a road or not: the railway's cantilever mast (catenary.GROUP) at its axis' side with
    room, the contact wire at the arm's tip, as the railway's.
The poles stand every SPAN_M (shorter on a curve) from a phase hashed from the line, the same in every tile; a pole near
another (SHARE_M) is that one (two tracks side by side share their poles). Spans are drawn by the tile that holds their
axis point. Pure given its inputs; the heights are one call of ground."""
import math

import numpy as np

import catenary as cat
import tramtrack

ENABLED = tramtrack.ENABLED and cat.ENABLED
SPAN_M = 34.0               # m between the pole pairs on a straight
SPAN_MIN_M = 16.0
CURVE_K = 0.3               # m: a span's sagitta on a curve (catenary.CURVE_K's idea, a tram's wire pulls off less)
REACH_M = 130.0             # m round the tile the lines are read
OUT_MAX_M = 28.0            # m a pole is looked for out from the axis, each side
START_OUT_M = 1.2           # m: not nearer than this to the axis
SIDEWALK_OFF_M = 0.9        # m: a pole's axis outside a carriageway's edge
TRACK_CLEAR_M = 1.8         # m a pole keeps off another tram track's axis
SHARE_M = 5.0               # m: a pole this close to a placed one is that one (a street narrower than this shares none)
POLE_H = 8.2                # m: a street pole's height (the catpost model is 10.4 m: scaled)
POLE_SCALE = POLE_H / cat.POST_H
SPAN_Z = 7.4                # m: the span wire's fixing over the pole's base
WALL_Z = 6.8                # m: its fixing on a building's wall over the ground
CONTACT_Z = 5.6             # m: the contact wire over the track's axis (a tram's pantograph: 5.5-6 m)
HANGER_M = 0.5              # m the span wire lies over the contact wire
SPAN_R = 0.009
SEG_M = 8.0                 # m: the contact wire's points
BRIDGE_OUT_M = 0.55        # m: a bridge pole stands this far past the road deck's edge: behind its parapet (the slab reaches
                            # roadmesh.PARAPET_W = 0.45 m past the road's edge, the parapet on it; the user, 2026-11-06)

_METHOD = "tram"


def tram_parts(frame, cells, box, reach=REACH_M):
    """[(fid, xy (k, 2), kind)] ("ground" or "bridge") of the electrified trams near box, sorted (the same in every tile). A tram's flags: none
    (disused, abandoned, unbuilt) or an OSM electrified value that is not a contact line leaves it out; no value is a
    contact line."""
    import mapgeom
    import railways
    near = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats) or "subtype" not in feats.attrs:
            continue
        sub, cls = feats.attrs["subtype"], feats.attrs.get("class")
        osm, el = feats.attrs.get("osm_railway"), feats.attrs.get("osm_electrified")
        fb = feats.boxes(frame)
        for i in range(len(feats)):
            if str(sub[i]) != "rail":
                continue
            tram = (cls is not None and str(cls[i]) == "tram") or (osm is not None and osm[i] and str(osm[i]) == "tram")
            fid = str(feats.ids[i])
            if not tram or fid in seen or not railways._box_hit(fb[i], near):
                continue
            seen.add(fid)
            if railways.wear(feats, i) != 0:
                continue
            if el is not None and el[i] and str(el[i]).split(";")[0].strip() not in cat.ELECTRIC:
                continue
            for kind, rings in railways._pieces(feats, i):
                for ring in rings:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    xy = np.c_[e, n]
                    if len(xy) >= 2 and railways._box_hit((xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(),
                                                            xy[:, 1].max()), near):
                        xy = railways._resample(xy, 2.0)
                        if len(xy) >= 2:
                            out.append((fid, xy, kind))
    out.sort(key=lambda p: (p[0], p[2], float(p[1][0, 0]), float(p[1][0, 1])))
    return out


def stations(xy, fid):
    """Arc lengths of the poles along xy: from a phase hashed from fid, the span by the curve there. (arcs, arc)"""
    arc = cat._arc(xy)
    s = cat._h01(fid, _METHOD) * SPAN_M
    out = []
    while s < arc[-1]:
        out.append(s)
        r = cat._radius(xy, arc, s)
        s += SPAN_M if not math.isfinite(r) else float(np.clip(math.sqrt(8.0 * r * CURVE_K), SPAN_MIN_M, SPAN_M))
    return out, arc


def _off_roads(q, roads, extra):
    return all(cat._dist_to_lines(q, [ln]) > hw + extra for ln, hw in roads)


def _anchor(p, n, side, roads, rings, others):
    """(point, on a building's wall) of the fixing out from axis point p along side * n: the first point 0.9 m clear of
    every carriageway (and off the other tracks), a wall where a building stands there; None within OUT_MAX_M of none."""
    for d in np.arange(START_OUT_M, OUT_MAX_M, 0.5):
        q = p + n * side * d
        if not _off_roads(q, roads, SIDEWALK_OFF_M):
            continue
        if others and cat._dist_to_lines(q, others) < TRACK_CLEAR_M:
            continue
        return q, bool(rings and cat.near_ring(q, rings, 0.5))
    return None


def deck_at(p, decks):
    """(z, half width, centre point (2,), unit direction (2,)) of the road deck under point p (2,): the nearest of decks
    [(xy, z, width)] within railways.DECK_NEAR_M of it, or None. The poles stand at the deck's edges, which are measured from
    its centre line (a tram lies to one side of it, not in the middle). Pure."""
    import railways
    p = np.asarray(p, np.float64)
    best = None
    for dxy, dz, width in decks:
        z, on = railways.road_deck_z(p[None, :], [(dxy, dz)])
        if on[0]:
            dxy = np.asarray(dxy, np.float64)[:, :2]
            _s, q, u, d = cat._nearest(dxy, cat._arc(dxy), p)
            if best is None or d < best[0]:
                best = (d, float(z[0]), float(width) / 2.0, q, u)
    return None if best is None else best[1:]


def plan(frame, cells, box, rings=(), decks=()):
    """([{"fid", "nodes"}], poles) of the trams round box: a node {"p": axis point (2,), "kind": "end" | "span" | "mast", "s": arc,
    "anchors": [pole] (span), "q" and "dir" (mast), "z": its height where it is on a road deck}; a pole {"q": (2,), "wall":
    on a building's wall (no pole drawn), "z": its base on a road deck}. decks: [(xy, z, width)] the road bridge decks near
    (a tram bridge on one gets its poles behind the deck's parapets and its wire at the deck's height; a bridge of its own: none)."""
    parts = tram_parts(frame, cells, box)
    if not parts:
        return [], []
    decks = list(decks)
    roads = cat._road_lines(frame, cells, box, REACH_M)
    ways = {(k, 0): (ln, hw) for k, (ln, hw) in enumerate(roads)}
    all_xy = [xy for _f, xy, _k in parts]
    poles, out = [], []

    def share(q, z=None):
        for pl in poles:
            if float(np.hypot(*(pl["q"] - q))) <= SHARE_M and ("z" in pl) == (z is not None):
                return pl
        pl = {"q": q, "wall": False}
        if z is not None:
            pl["z"] = z
        poles.append(pl)
        return pl

    for pi, (fid, xy, pkind) in enumerate(parts):
        stn, arc = stations(xy, fid)
        others = [o for o in all_xy if o is not xy]
        bridge = pkind == "bridge"
        if bridge and not any(deck_at(p, decks) for p in xy[::max(1, len(xy) // 4)]):
            continue                # (a bridge of its own, no road deck under it: no overhead line on it, yet)

        def end(p, s):
            nd = {"p": p, "kind": "end", "s": s}
            dk = deck_at(p, decks) if bridge else None
            if dk is not None:
                nd["z"] = dk[0]
            return nd
        nodes = [end(xy[0], 0.0)]
        for s0 in stn:
            p0, u0 = cat._frame_at(xy, s0, arc)
            n = np.array([-u0[1], u0[0]])
            if bridge:              # poles at the road deck's edges, at its height
                dk = deck_at(p0, decks)
                if dk is None:
                    continue
                zd, half, qc, uc = dk
                nc = np.array([-uc[1], uc[0]])
                got = [share(qc + nc * side * (half + BRIDGE_OUT_M), zd) for side in (1, -1)]
                nodes.append({"p": p0, "kind": "span", "anchors": got, "s": s0, "z": zd})
                continue
            street = bool(tramtrack.in_street(p0[None, :], ways, _on_ways)[0]) if ways else False
            if street:
                got = []
                for side in (1, -1):
                    a = _anchor(p0, n, side, roads, rings, others)
                    if a is not None:
                        pl = share(a[0])
                        pl["wall"] = pl["wall"] or a[1]
                        got.append(pl)
                if got:
                    nodes.append({"p": p0, "kind": "span", "anchors": got, "s": s0})
                continue
            placed = _mast(xy, arc, s0, others, roads, rings, fid)
            if placed is not None:
                nodes.append(dict(placed, kind="mast", s=placed["s"]))
        nodes.append(end(xy[-1], float(arc[-1])))
        out.append({"fid": fid, "nodes": nodes})
    return out, poles


def _on_ways(pts, ways, margin):
    import roads
    return roads._on_carriageways(pts, ways, margin)


def _mast(xy, arc, s0, others, roads, rings, fid):
    """The railway's cantilever mast for an own-way tram at arc s0 (catenary.masts_of's rule, one line); None where none
    fits."""
    side0 = 1 if cat._h01(fid, "side") < 0.5 else -1
    for shift in cat.SHIFT_M:
        s = s0 + shift
        if not 0.0 <= s <= arc[-1]:
            continue
        p, u = cat._frame_at(xy, s, arc)
        n = np.array([-u[1], u[0]])
        for side in (side0, -side0):
            q = p + n * side * cat.MAST_OFF_M
            if others and cat._dist_to_lines(q, others) < cat.BETWEEN_M:
                continue
            if not cat._clear_of_roads(q, roads) or (rings and cat.near_ring(q, rings)):
                continue
            return {"p": p, "q": q, "dir": (p - q) / max(float(np.hypot(*(p - q))), 1e-9), "s": s}
    return None


def rows_and_wires(parts, poles, ground, box_owns, style="su"):
    """(prop rows, wire polylines (xyz (k, 3), radius)) of the plan: a row for each pole / mast the tile holds, for each
    span (and contact wire piece) whose first axis point the tile holds, the wires. ground(xy (n, 2)) -> z."""
    g_mast, g_post = cat.STYLE_GROUPS[style]
    pts = [pl["q"] for pl in poles]
    for pt in parts:
        for nd in pt["nodes"]:
            pts.append(nd["p"])
            if nd["kind"] == "mast":
                pts.append(nd["q"])
    if not pts:
        return [], []
    zs = np.asarray(ground(np.array(pts, np.float64)), np.float64).ravel()
    z_of = {id(p): float(z) for p, z in zip(pts, zs)}
    rows, wires = [], []
    for pl in poles:
        if pl["wall"] or not box_owns(float(pl["q"][0]), float(pl["q"][1])):
            continue
        rows.append([g_post, round(float(pl["q"][0]), 2), round(float(pl["q"][1]), 2),
                     round(pl["z"] if "z" in pl else z_of[id(pl["q"])], 2), 0.0,
                     round(POLE_SCALE, 3), POLE_H, 0.0])
    for pt in parts:
        nodes = pt["nodes"]
        zc = []
        for nd in nodes:
            za = nd["z"] if "z" in nd else z_of[id(nd["p"])]
            if nd["kind"] == "mast":
                zq = z_of[id(nd["q"])]
                zc.append(float(np.clip(zq, za + cat.MAST_Z_BAND[0], za + cat.MAST_Z_BAND[1])) + cat.Z_CONTACT)
            else:
                zc.append(za + CONTACT_Z)
        for nd, z in zip(nodes, zc):
            if nd["kind"] == "mast" and box_owns(float(nd["q"][0]), float(nd["q"][1])):
                yaw = math.degrees(math.atan2(nd["dir"][1], nd["dir"][0])) % 360.0
                zb = float(np.clip(z_of[id(nd["q"])], z_of[id(nd["p"])] + cat.MAST_Z_BAND[0],
                                   z_of[id(nd["p"])] + cat.MAST_Z_BAND[1]))
                rows.append([g_mast, round(float(nd["q"][0]), 2), round(float(nd["q"][1]), 2), round(zb, 2),
                             round(yaw, 1), 1.0, cat.MAST_H, 0.0])
        for a, b, za, zb in zip(nodes[:-1], nodes[1:], zc[:-1], zc[1:]):
            if not box_owns(float(a["p"][0]), float(a["p"][1])):
                continue
            L = float(np.hypot(*(b["p"] - a["p"])))
            if not (1.0 <= L <= cat.MAX_SPAN_DRAWN_M):
                continue
            k = max(1, int(math.ceil(L / SEG_M)))
            t = np.linspace(0.0, 1.0, k + 1)
            line = np.c_[a["p"][0] + (b["p"][0] - a["p"][0]) * t, a["p"][1] + (b["p"][1] - a["p"][1]) * t,
                         za + (zb - za) * t]
            wires.append((line, cat.CONTACT_R))
        for nd, z in zip(nodes, zc):
            if nd["kind"] != "span" or not box_owns(float(nd["p"][0]), float(nd["p"][1])):
                continue
            mid = np.r_[nd["p"], z + HANGER_M]
            ends = []
            for pl in nd["anchors"]:
                zf = (pl["z"] if "z" in pl else z_of[id(pl["q"])]) + (WALL_Z if pl["wall"] else SPAN_Z)
                ends.append(np.r_[pl["q"], zf])
            if len(ends) == 2:
                wires.append((np.array([ends[0], mid, ends[1]]), SPAN_R))
            else:
                wires.append((np.array([ends[0], mid]), SPAN_R))
            wires.append((np.array([np.r_[nd["p"], z], mid]), cat.DROPPER_R))
    return rows, wires


def build(frame, tx, ty, cells, ground, rings=(), country=None, decks=()):
    """(prop rows, wire lines) of road tile (tx, ty): the tram overhead lines round it (catenary.build adds them).
    decks: [(xy, z, width)] the road bridge decks round it (blockworker._road_decks)."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    parts, poles = plan(frame, cells, box, rings, decks)
    if not parts:
        return [], []
    return rows_and_wires(parts, poles, ground, lambda x, y: roadsmod.box_owns(box, x, y), cat.style_of(country))
