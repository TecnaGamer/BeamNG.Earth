"""Tunnel entrances: the glue round earthfast's portal kernels (native/earthfast/src/portal.rs; 2026-11-12, the user:
"focus on tunnel entrances ... Many entrances are broken or ugly and look nothing like the real version").

fit(): per tunnel chunk of a road tile (roads._tunnel_chunks) the ground the entrance is built from: over the tube's
rows ("ground": where it does not cover the tube, roadmesh.tunnel builds the tube's outside), and at each end that is
a portal ("fit"): the ground along the mouth's plane (the headwall's top), beside the approach (the wing walls), the
approach's own heights, how far the headwall may reach either side before another road, and the mouth's look
(earthfast.portal_style: a mountain bore, a cut-and-cover mouth, a lesser road's shorter hood).
structure(): the mesh pieces of one mouth for roadmesh.tunnel.

The ground is the tile's own (the graded ground the terrain shows); nothing here changes a tunnel's floor, sections
or holes. BNE_PORTALS=0: the old portal face (one frame round the opening).
"""
import os

import numpy as np

import fast
import dnp

ON = os.environ.get("BNE_PORTALS", "1") != "0"
VERSION = 259               # earthfast's portal kernels
FACE_IN = (1.0, 3.0, 5.0)   # m inside the mouth's plane the ground behind the headwall is read (the highest of them:
                            # the grading flattens the approach a few metres into the mapped mouth, the hill's face
                            # stands 3 - 5 m in; the headwall holds that face, 2026-11-12)
FACE_SIDE = 3.0             # m of headwall beside the opening (over the terrain hole's ragged edge) ...
FACE_SIDE_MIN = 0.7         # ... at least, where another road runs beside the mouth
FACE_STEP = 0.5             # m between the tries of a shorter side
FACE_X = (0.5, 1.5, 3.0)    # m beyond the tube's wall where the ground along the mouth's plane is read ...
ROAD_GAP = 0.4              # m the headwall and the wings keep from another road's carriageway
WING_S = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0)   # m out of the mouth: a wing wall's stations
WING_BACK = 1.2             # m behind a wing wall's face its ground is read
RISE_IN = 12.0              # m inside the mouth where the hill over the tube is read (portal_style's rise)
SIDES_OUT = (5.0, 10.0)     # m out of the mouth where the approach's sides are read (portal_style's sides) ...
SIDES_OFF = 3.0             # ... this far beside the tube's wall
COVER_NEED = 1.0            # m of ground over the crown that covers a tube (less: its outside is built; the terrain's
                            # holes reach TUNNEL_HOLE_MARGIN over the vault, and a square's rise)
COVER_OVER = 0.3            # m the cover's top lies over the crown
COVER_SIDE = 1.5            # m the cover's sides stand outside the tube's wall (past the terrain hole's edge)
COVER_FOOT = 1.0            # m its sides reach under the floor
COVER_REACH = 1.0           # the ground over the tube: the lowest of its axis and this far beyond its walls either side
                            # (the terrain's holes reach TUNNEL_HOLE_MARGIN and a square past the wall)
TWIN_GAP = 0.5              # m a headwall keeps from another tube's wall (a twin bore's collar)
MAJOR = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link"}

# per look: collar_out, rake, collar_t, wall_t, parapet, top_min, top_max, foot, end_min, wing_t, wing_flare, wing_low
# 0 a mountain bore on a major road: a raked concrete hood (the E39's and the A2's bores, Mapillary 2026-11-12)
# 1 a cut-and-cover mouth: a square-set collar, a lintel with a parapet over the ground, walls straight along the road
# 2 a lesser road's portal in a hillside: a shorter raked concrete hood in a low wall (Rv83 Kilbotn, GitHub #1066: the
#   entrance survey of 2026-11-12 found raked concrete hoods at 5 of its 8 mountain mouths whatever the road, Loibl,
#   Laerdal, Kilbotn, Lantos, Damsgard; masonry only at old ones, Malibu Canyon, the N85, Central Park's arches)
# 3 a low arch (an underpass, a park's transverse): a ring a hand deep in a wall with a parapet, its foot at the floor
# (the hoods: the structure one sees is the hood standing 2 - 4 m out of the slope, the headwall behind it a low ring;
# the first cut, a 0.9 m hood in a headwall 1.8 m over the crown and 3 m beside the opening, read as a flat slab next
# to the Kilbotn / Laerdal photos, 2026-11-12)
LOOKS = {
    0: (2.0, 2.5, 0.55, 0.6, 0.3, 0.6, 1.2, 1.0, 0.8, 0.5, 0.35, 0.5),
    1: (0.25, 0.0, 0.5, 0.6, 1.0, 0.9, 2.5, 1.0, 1.0, 0.5, 0.0, 0.6),
    2: (1.5, 2.0, 0.45, 0.6, 0.3, 0.5, 1.0, 1.0, 0.7, 0.5, 0.3, 0.5),
    3: (0.15, 0.0, 0.4, 0.5, 0.9, 0.6, 2.5, 0.3, 0.9, 0.45, 0.0, 0.6),
}
HOOD_FACE = 0.6             # the hill's face: the ground over the floor this share of the opening's height
LOOK_SIDE = {0: 2.2, 1: FACE_SIDE, 2: 2.0, 3: FACE_SIDE}    # m of headwall beside the opening at most, per look
ROCK_BACKING_TOP = 2.0      # m above a bare-rock opening before the cliff itself takes over
ROCK_BACKING_WALL = 0.15    # m behind the section before the backing can turn into the hill
ROCK_COVER_OVER = 0.55      # overlap the lip even where the terrain rises within a grid square
ROCK_SHOULDER_BACK = 0.30


def rock_cover_side():
    """Overlap the outer corner of a terrain square removed beside the bore."""
    from heightmap import SQUARE
    from roads import TUNNEL_HOLE_MARGIN

    return max(COVER_SIDE, TUNNEL_HOLE_MARGIN + np.sqrt(2.) * SQUARE + .20)


def _shoulder_stations():
    from heightmap import SQUARE

    return np.array([-ROCK_SHOULDER_BACK, np.sqrt(2.) * SQUARE + .15])


def _others(roads, cells, frame, parts):
    """fast.Others of the roads a mouth's walls keep off: the ground roads and the tunnel parts round the tile."""
    import mapgeom
    lines = []
    for feats in cells:
        if feats is None or len(feats) == 0:
            continue
        rs = roads.road_specs(feats)
        for i, sp in enumerate(rs):
            if sp is None or sp.kind not in roads.BUILT:
                continue
            for ring in roads.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, frame, ring)
                xy = np.c_[e, n]
                if len(xy) >= 2:
                    lines.append((len(lines), sp.width / 2, xy))
    import roadmesh
    for _fid, spec, xy, _z, _lon in parts:
        xy = np.asarray(xy, np.float64)[:, :2]
        if len(xy) >= 2:
            # (a twin bore's tube and its collar, not only its carriageway: the two headwalls stopped on each other's
            # roads and overlapped by the shoulders between)
            half = roads.tunnel_max_width(spec) / 2 + roadmesh.tunnel_shoulder(spec)
            lines.append((len(lines), half + TWIN_GAP, xy))
    return lines


def _through(lines, p, reach):
    """The indices of the lines passing within reach of p: the mouth's own carriageways (the tunnel's parts, its
    approach, a twin's two roads): not kept off."""
    own = set()
    for k, _hw, xy in lines:
        a, b = xy[:-1], xy[1:]
        d = b - a
        t = np.clip(((p - a) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        if float(dnp.hypot(*(a + t[:, None] * d - p).T).min()) < reach:
            own.add(k)
    return own


def fit(roads, tunnels, cells, chunks, parts):
    """chunks with "ground" and "fit" on the tunnel tubes' (the module's head). roads: the roads module."""
    import roadmesh
    if not ON or getattr(tunnels, "_ground", None) is None:
        return chunks
    todo = [c for c in chunks if c.get("tunnel") and not c.get("no_tube") and len(c["n"]) >= 2]
    if not todo:
        return chunks
    fast.require(VERSION, "portal_style")
    others = None
    for c in todo:
        n = np.asarray(c["n"], np.float64)
        xy, z = n[:, :2], n[:, 2]
        measured = roadmesh.tunnel_width_samples(c)
        widths = np.full(len(n), c["w"], np.float64) if measured is None else measured
        half_widths = widths / 2 + roadmesh.tunnel_shoulder(c)
        th = float(c.get("tunnel_h") or roadmesh.TUNNEL_H)
        d = np.gradient(xy, axis=0)
        d /= np.maximum(dnp.hypot(*d.T), 1e-9)[:, None]
        left = np.c_[-d[:, 1], d[:, 0]]
        extent = left * (half_widths + COVER_REACH)[:, None]
        pts = [xy, xy + extent, xy - extent]
        ends = []
        for e, at in enumerate((0, len(xy) - 1)):
            if not (c.get("portal") or (False, False))[e]:
                ends.append(None)
                continue
            hw = float(half_widths[at])
            t = xy[1] - xy[0] if e == 0 else xy[-1] - xy[-2]
            t = t / max(float(dnp.hypot(*t)), 1e-9)
            ul = np.array([-t[1], t[0]])
            out = -t if e == 0 else t
            p = xy[at]
            if others is None:
                others = _others(roads, cells, tunnels.frame, parts)
            own = _through(others, p, hw)
            oth = fast.Others([ln for ln in others if ln[0] not in own])
            sides = []
            for sg in (1.0, -1.0):          # how far the headwall reaches before another road
                reach = FACE_SIDE
                for dd in np.arange(FACE_STEP, FACE_SIDE + 1e-9, FACE_STEP):
                    if not oth.clear(p + ul * sg * (hw + dd), None, ROAD_GAP):
                        reach = max(float(dd) - FACE_STEP, FACE_SIDE_MIN)
                        break
                sides.append(reach)
            fx = np.array([-(hw + v) for v in FACE_X[::-1]] + [-0.5 * hw, 0.0, 0.5 * hw] + [hw + v for v in FACE_X])
            look = None
            ws = np.asarray(WING_S, np.float64)
            wing_ok = []
            for sg, reach in zip((1.0, -1.0), sides):
                ok = reach >= FACE_SIDE - 1e-9
                if ok:
                    lp = [p + ul * sg * (hw + reach + 0.5) + out * s for s in ws]
                    ok = all(oth.clear(q, None, ROAD_GAP) for q in lp)
                wing_ok.append(ok)
            shoulder_room = None
            if "tunnel_shoulder" in c:
                pad = rock_cover_side()
                shoulder_room = []
                shoulder_probes = []
                stations = _shoulder_stations()[:, None] * out
                for sg, room in zip((1., -1.), sides):
                    width = min(room, pad)
                    edge = p + ul * sg * (hw + width) + stations
                    shoulder_probes.append(edge)
                    shoulder_room.append(width if width > .5 and all(oth.clear(q, None, ROAD_GAP) for q in edge) else 0.)
            ends.append({"p": p, "ul": ul, "out": out, "fx": fx, "sides": sides, "wing_ok": wing_ok, "at": at,
                         "i0": sum(len(q) for q in pts), "look": look, "shoulder_room": shoulder_room})
            for s_in in FACE_IN:
                pts.append(p[None] + ul[None] * fx[:, None] - out[None] * s_in)
            for sg, reach in zip((1.0, -1.0), sides):
                pts.append(p[None] + ul[None] * sg * (hw + reach + WING_BACK) + out[None] * ws[:, None])
            pts.append(p[None] + out[None] * ws[:, None])
            pts.append((p - out * RISE_IN)[None])
            pts.append(np.array([p + ul * sg * (hw + SIDES_OFF) + out * s for sg in (1.0, -1.0) for s in SIDES_OUT]))
            if shoulder_room is not None:
                pts.append(np.vstack(shoulder_probes))
        try:
            g = np.asarray(tunnels._ground(np.vstack(pts)), np.float64)
        except Exception:               # (no ground at hand: the plain structure, the tube as it was)
            continue
        k = len(xy)
        c["ground"] = [round(float(v), 2) for v in np.minimum(np.minimum(g[:k], g[k:2 * k]), g[2 * k:3 * k])]
        fits = []
        for en in ends:
            if en is None:
                fits.append(None)
                continue
            i, nx, nw = en["i0"], len(en["fx"]), len(WING_S)
            zf = float(z[en["at"]])
            sampled_face = g[i:i + len(FACE_IN) * nx].reshape(len(FACE_IN), nx) - zf
            face = sampled_face.max(axis=0)
            face_depth = np.asarray(FACE_IN)[sampled_face.argmax(axis=0)]
            # the hill's face over the axis: how far in the graded approach goes on before it (a hood reaches out of
            # the face to the mouth: Laerdal's stands ~5 m out of its slope)
            setback = 0.0
            for q, s_in in enumerate(FACE_IN):
                if float(g[i + q * nx + nx // 2] - zf) >= HOOD_FACE * th:
                    break
                setback = s_in
            i += len(FACE_IN) * nx
            wl, wr, rz = g[i:i + nw] - zf, g[i + nw:i + 2 * nw] - zf, g[i + 2 * nw:i + 3 * nw] - zf
            i += 3 * nw
            sd = g[i + 1:i + 5] - zf
            # (the approach's sides over the road out there: the lower side's, the deeper of its two stations)
            rz_s = np.interp(SIDES_OUT, WING_S, rz)
            sides_h = min(float((sd[:2] - rz_s).max()), float((sd[2:] - rz_s).max()))
            # how far the hill over the tube climbs over the approach's sides (a bore into a slope: metres; a box under
            # a street level with its trough's walls: none). Over the mouth's own face ground it read nothing once
            # the face is read 5 m in, at the hill's face (Kilbotn, Laerdal: flat lintels, 2026-11-12)
            rise = float(g[i] - zf) - max(sides_h, 0.0)
            st = int(fast._ef.portal_style(c.get("cls") in MAJOR or bool(c.get("concrete")), th, rise, sides_h))
            r2 = lambda v: [round(float(x), 2) for x in v]      # noqa: E731
            fits.append({"st": st, "fx": r2(en["fx"]), "fg": r2(face), "fd": r2(face_depth),
                         "fc": [r2(row) for row in sampled_face], "ws": list(WING_S),
                         "wl": r2(wl) if en["wing_ok"][0] else None, "wr": r2(wr) if en["wing_ok"][1] else None,
                         "rz": r2(rz), "sl": round(en["sides"][0], 2), "sr": round(en["sides"][1], 2),
                         "sb": round(setback, 2) if st in (0, 2) else 0.0})
            if en["shoulder_room"] is not None:
                fits[-1]["shoulder_room"] = r2(en["shoulder_room"])
                fits[-1]["shoulder_ground"] = r2(g[i + 5:i + 9] - zf)
        if any(f is not None for f in fits):
            c["fit"] = fits
    return chunks


def _look(fitd, low):
    return 3 if low else int(fitd["st"])


def parapet(fitd, low=False):
    return LOOKS[_look(fitd, low)][4]


def crown_top(fitd, th, low=False):
    """The headwall's top over the floor at the opening's middle (earthfast.portal's headwall_top there)."""
    lk = LOOKS[_look(fitd, low)]
    g = float(np.interp(0.0, fitd["fx"], fitd["fg"]))
    return min(max(g + lk[4], th + lk[5]), th + lk[6])


def prm(st, th, hw, side_l, side_r, uv_m):
    """earthfast.portal's Prm for look st."""
    co, rake, ct, wt, par, tmin, tmax, foot, emin, wgt, flare, wlow = LOOKS[st]
    side_l, side_r = min(side_l, LOOK_SIDE[st]), min(side_r, LOOK_SIDE[st])
    return np.array([th, hw, co, rake, ct, wt, par, side_l, tmin, tmax, foot, emin, wgt, flare, wlow, uv_m, side_r,
                     COVER_SIDE + 0.1], np.float64)


def setback(fitd, low=False):
    """m the mouth's headwall stands back from its mouth (a hood's, at the hill's face)."""
    return 0.0 if low or fitd is None else float(fitd.get("sb") or 0.0)


def _rake_rock_face(mesh, p, ul, out, crown, fitd, samples=None, hw=None):
    """Shape a tall backing into the surveyed cut while leaving the collar at its original plane."""
    points, tris, uvs, codes = mesh
    if not len(tris) or (samples is None and points[:, 2].max() < p[2] + crown + 1.5):
        return mesh

    # Split shared edges together. The face, its top and its returns must deform as one mesh.
    for _ in range(2):
        ends = np.sort(tris[:, [[0, 1], [1, 2], [2, 0]]], axis=2).reshape(-1, 2)
        edges, inverse = np.unique(ends, axis=0, return_inverse=True)
        d, e, f = (inverse.reshape(-1, 3) + len(points)).T
        a, b, c = tris.T
        points = np.r_[points, points[edges].mean(axis=1)]
        uvs = np.r_[uvs, uvs[edges].mean(axis=1)]
        tris = np.r_[np.c_[a, d, f], np.c_[d, b, e], np.c_[f, e, c], np.c_[d, e, f]]
        codes = np.tile(codes, 4)

    rel = points - p
    across = dnp.matmul(rel[:, :2], ul)
    along = dnp.matmul(rel[:, :2], out)
    over = np.maximum(rel[:, 2] - crown - 0.25, 0.0)
    support = np.interp(across, fitd["fx"], fitd.get("fd", [3.0] * len(fitd["fx"])))
    # The upper rock leans back toward the ground sample. Shallow, staggered ledges
    # break up the surface without moving its arch or pushing rock into the approach.
    t = np.clip(over / 3.0, 0.0, 1.0)
    ledges = 0.35 * dnp.sin(rel[:, 2] * 1.55 + across * 0.48) + 0.22 * dnp.sin(across * 1.8 - rel[:, 2] * 0.66)
    drift = np.minimum(0.53 * over, 0.9 * support)
    drift = np.clip(drift + t * ledges * np.minimum(1.0, 0.35 * support), 0.0, support)
    drift *= np.clip(1.0 - np.maximum(along, 0.0) / 2.0, 0.0, 1.0)
    if samples is not None:
        # The short exposed face still joins the cut. At its rear, extend the
        # rock over the crown until a measured depth lies inside the hillside.
        # Keep the join outside the vault and its full driveable width at bends.
        sampled = np.array([np.interp(across, fitd["fx"], row) for row in samples])
        covered = sampled >= rel[:, 2] + COVER_OVER
        depth = np.where(covered.any(axis=0), np.asarray(FACE_IN)[covered.argmax(axis=0)], 0.0)
        rear = np.clip(-along / ROCK_BACKING_WALL, 0.0, 1.0)
        crown_band = np.clip(over / 0.25, 0.0, 1.0)
        crown_return = np.maximum(depth + along - drift, 0.0) * crown_band
        if hw is not None:
            side_band = np.clip((np.abs(across) - hw - .75) / .65, 0.0, 1.0)
            side_return = np.maximum(np.minimum(depth, 1.5) + along - drift, 0.0) * side_band
            crown_return = np.maximum(crown_return, side_return)
        drift += rear * crown_return
    points[:, :2] -= drift[:, None] * out
    return points, tris, uvs, codes


def structure(fitd, p, ul, out, sec, th, hw, uv_m, walls_near=(False, False), low=False, *, collar=True,
              compact_rock=False, rock_backing=False):
    """The mouth's structure for roadmesh.tunnel: (positions (n, 3), triangles (m, 3), uvs (n, 2), codes (m,): 0 the
    structure, 1 soil). p: the mouth's floor centre in the mesh's coordinates; walls_near: (left, right) a cutting's
    retaining wall already stands there (no wing wall on that side). With collar=False, fit a retaining face at the
    opening of a tube whose collar is already modeled. A compact rock face follows the near contour and joins the
    terrain behind the vault."""
    fast.require(VERSION, "portal_mesh")
    st = _look(fitd, low)
    none = np.zeros(0)
    # a hood: its headwall back at the hill's face, the hood reaching out from there to the mouth
    sb = setback(fitd, low) if collar else 0.0
    p = np.asarray(p, np.float64) - np.r_[np.asarray(out, np.float64) * sb, 0.0]
    ws = np.asarray(fitd["ws"], np.float64) + sb
    wl = np.asarray(fitd["wl"], np.float64) if fitd.get("wl") is not None and not walls_near[0] else none
    wr = np.asarray(fitd["wr"], np.float64) if fitd.get("wr") is not None and not walls_near[1] else none
    pr = prm(st, th, hw, float(fitd["sl"]), float(fitd["sr"]), uv_m)
    pr[2] += sb
    terrain_top = np.asarray(fitd["fg"], np.float64)
    face_samples = None
    if not collar:
        pr[2:4] = 0.0  # omit the native hood; the custom rim follows this same opening
        pr[5] = ROCK_BACKING_WALL   # keep the back face within the tube wall at a bend
        if rock_backing:
            pad = rock_cover_side()
            pr[8] = max(pr[8], ROCK_COVER_OVER + .30)
            for index, side, crowded in zip((7, 16), ("sl", "sr"), walls_near):
                if not crowded:
                    pr[index] = max(pr[index], min(pad, float(fitd[side])))
            if fitd.get("fc") is not None:
                face_samples = np.asarray(fitd["fc"], np.float64)
        if compact_rock:
            pr[6] = min(pr[6], COVER_OVER)
            pr[9] = ROCK_BACKING_TOP
            if fitd.get("fc") is not None:
                face_samples = np.asarray(fitd["fc"], np.float64)
                terrain_top = np.where(np.isfinite(face_samples[0]), face_samples[0], terrain_top)
        elif np.isfinite(terrain_top).any():
            pr[9] = np.clip(np.max(terrain_top[np.isfinite(terrain_top)]) + pr[6] - th, pr[9], 12.0)
    pr = np.r_[pr, sb]          # (inner_from: the tube's own walls are the hood's inside up to the mouth)
    mesh = fast._ef.portal_mesh([float(v) for v in p], [float(v) for v in ul], [float(v) for v in out], pr,
                                np.ascontiguousarray(sec, np.float64), np.asarray(fitd["fx"], np.float64),
                                terrain_top, ws, wl, wr, np.asarray(fitd["rz"], np.float64))
    if not collar:
        return _rake_rock_face(mesh, p, ul, out, float(sec[:, 1].max()), fitd, face_samples,
                               hw if rock_backing else None)
    return mesh


def taper_frame(mid):
    """An untwisted version of the centreline, with one identifiable station per original row."""
    length = dnp.hypot(*np.diff(mid, axis=0).T)
    arc = np.r_[0.0, np.cumsum(np.maximum(length, 1e-8))]
    return np.c_[arc, np.zeros(len(arc))], np.tile((0.0, 1.0), (len(arc), 1))


def shoulder_return(fit, p, left, out, half, near):
    """Rock over the outside corners of the terrain cut immediately in front of the mouth."""
    if "shoulder_ground" not in fit:
        return None
    ground = np.asarray(fit["shoulder_ground"], float).reshape(2, 2)
    positions, triangles, uvs = [], [], []
    stations = _shoulder_stations()
    for i, (side, width) in enumerate(zip((1., -1.), fit["shoulder_room"])):
        if near[i] or width <= 0.0 or not np.isfinite(ground[i]).all():
            continue
        across = side * np.array([half - .03, half + width])
        x, s = np.meshgrid(across, stations)
        z = np.c_[np.full(2, -.08), np.clip(ground[i], -1.5, 1.)]
        xy = p[:2] + x.ravel()[:, None] * left + s.ravel()[:, None] * out
        q = np.c_[xy, p[2] + z.ravel()]
        faces = np.array([[0, 1, 3], [0, 3, 2]])
        ab, ac = q[1] - q[0], q[3] - q[0]
        if ab[0] * ac[1] - ab[1] * ac[0] < 0.0:
            faces = faces[:, ::-1]
        triangles.append(faces + 4 * len(positions))
        positions.append(q)
        uvs.append(np.c_[x.ravel(), s.ravel()] / 2.)
    if not positions:
        return None
    tris = np.concatenate(triangles)
    return np.concatenate(positions), tris, np.concatenate(uvs), np.zeros(len(tris), np.int64)


def place_taper(mesh, flat, mid, left, widths, origin=(0.0, 0.0, 0.0)):
    """Place an untwisted mesh on its original rows, fitting the width without stretching the cover margin."""
    points, tris, uv, codes = mesh
    if not len(points):
        return mesh
    at = np.searchsorted(flat[:, 0], points[:, 0])
    across = points[:, 1]
    inside = np.clip(across, -widths[0], widths[0])
    scaled = across + inside * (widths[at] / widths[0] - 1.0)
    placed = np.c_[mid[at] + left[at] * scaled[:, None], points[:, 2]] - origin
    return placed, tris, uv, codes


def cover(mid, left, z, ground, hw, th, caps, uv_m, soil_uv_m, sec=None, *, side=COVER_SIDE, rock=False):
    """The tube's outside over its bare rows for roadmesh.tunnel (earthfast.tunnel_cover_mesh); sec: the tube's section
    (roadmesh._tunnel_section), the opening its end caps inside the hill leave clear."""
    tapered = np.ndim(hw) != 0
    path, across = taper_frame(mid) if tapered else (mid, left)
    initial_width = float(hw[0] if tapered else hw)
    fast.require(VERSION, "tunnel_cover_mesh")
    mesh = fast._ef.tunnel_cover_mesh(np.ascontiguousarray(path, np.float64), np.ascontiguousarray(across, np.float64),
                                      np.ascontiguousarray(z, np.float64), np.ascontiguousarray(ground, np.float64),
                                      initial_width + side, float(th), ROCK_COVER_OVER if rock else COVER_OVER,
                                      COVER_NEED, COVER_FOOT,
                                      bool(caps[0]), bool(caps[1]), float(uv_m), float(soil_uv_m),
                                      np.ascontiguousarray(np.zeros((0, 2)) if sec is None else sec, np.float64))
    if rock:
        mesh = (*mesh[:3], np.zeros_like(mesh[3]))
    return place_taper(mesh, path, mid, left, hw) if tapered else mesh
