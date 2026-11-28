"""The German road bridge's edge: a concrete cap with a guard rail on it and a steel railing at its outer side
(BNE_BRIDGE_RAILINGS=on, off by default).

A road bridge's free edge gets a 0.84 m New Jersey parapet (roadmesh.deck) unless a guard rail is mapped along it
(guardrails.forest_rows: a low curb and the stock rail modules). German road bridges almost never have their rails
mapped, but nearly all have the same edge (the user, 2026-11-28, with photos of the L 125 bridge between Laufen and
Ballrechten-Dottingen, OSM way 24848686, and of the Gutachtalbruecke of the B 31 near Titisee-Neustadt, way
27018914): a raised concrete cap (Kappe) about 1.5 m wide beside the road; on it, near the road, a steel guard rail
(a W beam on spacers and posts); at its outer side a steel railing (posts, a handrail and two rails below it). The
concrete parapet looked American there. Unmapped detail, not wrong data, so a country rule stands in for it, as
euguardrails' country profiles do for the roadside rails (that module leaves bridges out).

With the switch on, every parapet run of a deck in COUNTRIES (the country at the run's middle point, countrybounds)
is drawn as that edge instead of its parapet (mesh): the cap in the deck's concrete, the rail and the railing in
galvanized steel (STEEL), all closed solids in the deck's colliding mesh. The cap reaches from the road's edge
CAP_W_M out (past the slab's own PARAPET_W: the deck reads wider by the cap, as the real ones are), its outer face
carried CORNICE_D_M down. A deck whose edge has a mapped guard rail keeps it (the map's rail wins). Nothing changes
with the switch off. The railing's posts and rails follow World-to-BeamNG's bridge railing
(world_to_beamng/bridges/bridge_mesh.py), with the guard rail and the cap added."""
import os

import numpy as np

import dnp

VERSION = "bridge-railings-3"
COUNTRIES = {"DE"}          # ISO codes whose road bridges get this edge (each added with its own evidence)

# Across the edge, in metres outward from the road's edge (the parapet run's "edge" lies SLAB_W_M past it), and up
# from the road's surface there. After the user's two photos (2026-11-28) and the usual German cap (RiZ-ING Kap):
SLAB_W_M = 0.45             # roadmesh.PARAPET_W: where the run's edge (the slab's outer edge) lies past the road's edge
CAP_W_M = 1.45              # the cap's width (its outer face 1 m past the slab's edge)
CAP_H_M = 0.20              # its top over the road (its curb's height)
CORNICE_D_M = 0.40          # its outer face reaches this far below the road (a cornice over the slab's edge)
# the guard rail (a W beam, its face toward the road), heights over the cap's top
RAIL_FACE_M = 0.45          # across: the beam's face
RAIL_LOW_M, RAIL_HIGH_M = 0.30, 0.60    # up: the beam's lower and upper edge
RAIL_DEPTH_M = 0.08         # the W's depth across (its ridges at the face, its valley and edges this far back)
RAIL_SHEET_M = 0.012        # the steel's thickness
SPACER_M = 0.14             # the spacer between the beam and its post (across)
GPOST_W_M, GPOST_L_M = 0.06, 0.10       # a guard rail post's section (across, along)
GPOST_H_M = 0.58            # its height over the cap
GPOST_EVERY_M = 2.0         # m between guard rail posts
# the railing, heights over the cap's top
RAILING_AT_M = 1.30         # across: the railing's centre line (0.15 m in from the cap's outer face)
RAILING_H_M = 1.00          # the handrail's top
HANDRAIL_W_M, HANDRAIL_H_M = 0.07, 0.05
BARS_AT_M = (0.32, 0.65)    # up: the two rails' centres below the handrail (the Gutachtalbruecke's three)
BAR_M = 0.04                # their square section
POST_M = 0.06               # the railing's posts' square section
POST_EVERY_M = 2.5          # m between railing posts

# In town (the user's photo of a village bridge, 2026-11-28: the Meiergasse over the Hohlenbach in St. Ilgen, Sulzburg-
# Laufen, OSM way 268148236, 5.4 m: a narrow cap and the railing, no guard rail): the bridge's approach road (town_of)
# is a street (TOWN_CLASSES), 50 km/h or less (spec.kmh), or built up (spec.urban). spec.urban alone is not enough:
# it means built-up land on most of the road's sides, which a village's streets between gardens do not reach (all
# of St. Ilgen's are urban=False; its streets residential at 30 km/h, the L 125's approaches 70-100 km/h).
TOWN_CLASSES = {"residential", "living_street"}
TOWN_KMH = 50.0             # km/h: an approach this fast or slower is in town (Germany's built-up limit)
TOWN_CAP_W_M = 0.75         # the cap's width in town
TOWN_RAILING_AT_M = 0.60    # the railing's centre line in town (0.15 m in from the cap's outer face)
TOWN_REACH_M = 25.0         # m round a run's ends and middle its approaches are looked for
TOWN_PARALLEL = 0.8         # |cos| between a road segment and the run at least (not the street crossing under it)

STEEL = "bne_bridge_railing_steel"      # (bridge_railing.materials.json in art/shapes/beamngEarth/bridge_railing)


def enabled():
    return os.environ.get("BNE_BRIDGE_RAILINGS", "off").strip().lower() in {"1", "on", "true"}


def key():
    """The road tiles' revisit key part: VERSION while on, nothing otherwise (every key built without it unchanged)."""
    return (VERSION,) if enabled() else ()


def railing_decks(frame, parapets, mapped=(), country_fn=None, roads=()):
    """{deck id: in town} for the parapet runs (roadmesh.parapet_runs) that take this edge: switch on, the run's middle
    point in COUNTRIES, its deck not one a mapped guard rail supports (`mapped`: guardrails' supported_decks_out).
    In town (town_of on `roads`, the tile's ground carriageways: build_tile roads_out) the edge has no guard rail.
    country_fn(frame, (x, y, z)) -> ISO code (default euguardrails.country_at). Empty with the switch off."""
    if not enabled() or not parapets:
        return {}
    if country_fn is None:
        import euguardrails
        country_fn = euguardrails.country_at
    mapped = set(mapped or ())
    out, seen = {}, {}
    for run in parapets:
        d = run.get("deck")
        if d in mapped or d in out:
            continue
        edge = np.asarray(run["edge"], np.float64)
        if len(edge) < 2:
            continue
        mid = edge[len(edge) // 2]
        z = float(np.asarray(run["z"], np.float64)[len(edge) // 2])
        cell = (round(float(mid[0]) / 500.0), round(float(mid[1]) / 500.0))    # (one border lookup per 500 m square)
        if cell not in seen:
            try:
                seen[cell] = country_fn(frame, (float(mid[0]), float(mid[1]), z))
            except Exception:
                seen[cell] = None
        if seen[cell] in COUNTRIES:
            out[d] = town_of(edge, roads)
    return out


def in_town(spec):
    """Whether a road (roads.Spec) is a town's: a street class, TOWN_KMH or slower, or built up."""
    kmh = getattr(spec, "kmh", None)
    return bool(getattr(spec, "urban", False) or getattr(spec, "cls", None) in TOWN_CLASSES
                or (kmh is not None and float(kmh) <= TOWN_KMH))


def town_of(edge, roads):
    """Whether the road a parapet run's deck carries is in town (in_town): that of the nearest ground carriageway
    segment (roads: (fid, spec, centreline (k, >= 2), ...)) within TOWN_REACH_M of the run's two ends or middle and
    near parallel to the run there (|cos| >= TOWN_PARALLEL: its approaches, not a street crossing under it). False
    when none is (a rural rule: the guard rail stays)."""
    edge = np.asarray(edge, np.float64)
    best, town = TOWN_REACH_M, False
    k = len(edge)
    for m in sorted({0, k // 2, k - 1}):
        p = edge[m]
        d = edge[min(m + 1, k - 1)] - edge[max(m - 1, 0)]
        d = d / max(float(dnp.hypot(d[0], d[1])), 1e-9)
        for item in roads or ():
            spec, line = item[1], np.asarray(item[2], np.float64)
            if spec is None or line.ndim != 2 or len(line) < 2:
                continue
            a, v = line[:-1, :2], np.diff(line[:, :2], axis=0)
            ln = dnp.hypot(v[:, 0], v[:, 1])
            ok = ln > 1e-6
            if not ok.any():
                continue
            a, v, ln = a[ok], v[ok], ln[ok]
            u = v / ln[:, None]
            t = np.clip(((p - a) * u).sum(axis=1), 0.0, ln)
            q = a + u * t[:, None]
            dist = dnp.hypot(q[:, 0] - p[0], q[:, 1] - p[1])
            par = np.abs(u[:, 0] * d[0] + u[:, 1] * d[1]) >= TOWN_PARALLEL
            if not par.any():
                continue
            j = int(np.argmin(np.where(par, dist, np.inf)))
            if float(dist[j]) < best:
                best, town = float(dist[j]), in_town(spec)
    return town


def _unit(v):
    v = np.asarray(v, np.float64)
    return v / np.maximum(dnp.hypot(v[:, 0], v[:, 1]), 1e-9)[:, None]


def sweep(line, out, z, front, back):
    """A closed solid swept along a line (k, 2): its section given as two polylines, front and back, of the same
    length (points (across along `out` (k, 2), up from z (k,))), the outline front then back reversed; both ends
    capped (strips between front and back: any section that is a thickened polyline, a box or a W beam). Every face
    looks out of the solid. (positions (n, 3), triangles (m, 3), uv (n, 2): along by arc length, across by the
    outline's own length). Pure."""
    line, z = np.asarray(line, np.float64), np.asarray(z, np.float64)
    o3 = np.column_stack((_unit(out), np.zeros(len(line))))
    up3 = np.array([0.0, 0.0, 1.0])
    poly = list(front) + list(back)[::-1]
    area = sum(a0 * u1 - a1 * u0 for (a0, u0), (a1, u1) in zip(poly, poly[1:] + poly[:1]))
    if area < 0:            # (counter-clockwise in (across, up): each edge's outward normal is (du, -da))
        poly = poly[::-1]
    k, m = len(line), len(poly)
    base = np.column_stack((line, z))
    R = np.stack([base + o3 * a + up3 * u for a, u in poly], axis=1)        # (k, m, 3)
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(line, axis=0).T))]
    ring = list(zip(poly, poly[1:] + poly[:1]))
    per = np.r_[0.0, np.cumsum([float(dnp.hypot(a1 - a0, u1 - u0)) for (a0, u0), (a1, u1) in ring])]
    P, UV, T, want, nv = [], [], [], [], 0
    i = np.arange(k - 1)
    for c in range(m):
        (a0, u0), (a1, u1) = poly[c], poly[(c + 1) % m]
        P.append(np.concatenate([R[:, c], R[:, (c + 1) % m]]))
        UV.append(np.column_stack((np.r_[s, s], np.r_[np.full(k, per[c]), np.full(k, per[c + 1])])))
        tri = np.concatenate([np.stack([nv + i, nv + i + 1, nv + k + i + 1], 1),
                              np.stack([nv + i, nv + k + i + 1, nv + k + i], 1)])
        T.append(tri)
        n_out = o3[:-1] * (u1 - u0) - up3 * (a1 - a0)                       # (k - 1, 3), per segment
        want.append(np.concatenate([n_out, n_out]))
        nv += 2 * k
    fr, bk = np.arange(len(front)), np.arange(len(back))
    tang = np.diff(base, axis=0)
    for e, sign, d in ((0, -1.0, tang[0]), (k - 1, 1.0, tang[-1])):
        Fp = np.stack([base[e] + o3[e] * a + up3 * u for a, u in front])
        Bp = np.stack([base[e] + o3[e] * a + up3 * u for a, u in back])
        P.append(np.concatenate([Fp, Bp]))
        UV.append(np.concatenate([np.array(front, np.float64), np.array(back, np.float64)]))
        nf = len(front)
        j = fr[:-1]
        T.append(np.concatenate([np.stack([nv + j, nv + j + 1, nv + nf + bk[1:]], 1),
                                 np.stack([nv + j, nv + nf + bk[1:], nv + nf + bk[:-1]], 1)]))
        want.append(np.tile(sign * d / max(float(dnp.norm(d)), 1e-9), (2 * (nf - 1), 1)))
        nv += 2 * nf
    P, T, UV, want = np.concatenate(P), np.concatenate(T), np.concatenate(UV), np.concatenate(want)
    nrm = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    flip = (nrm * want).sum(axis=1) < 0
    T[flip] = T[flip][:, ::-1]
    return P, T, UV


def _box(a0, a1, u0, u1):
    """A box section from across a0 to a1, up u0 to u1: (front, back) for sweep."""
    return [(a0, u0), (a0, u1)], [(a1, u0), (a1, u1)]


def _stations(s, every, margin):
    """Arc positions of posts: every `every` m or a little less, the first and last `margin` in from the ends."""
    total = float(s[-1])
    if total <= 2 * margin:
        return np.array([total / 2])
    n = max(1, int(np.ceil((total - 2 * margin) / every)))
    return np.linspace(margin, total - margin, n + 1)


def _at(line, s, z, out, sp):
    """At arc position sp: the point, its height, the unit direction along and the unit way out (square to it)."""
    j = int(min(np.searchsorted(s, sp, side="right") - 1, len(s) - 2))
    f = 0.0 if s[j + 1] <= s[j] else (sp - s[j]) / (s[j + 1] - s[j])
    t = _unit((line[j + 1] - line[j])[None])[0]
    o = np.array([t[1], -t[0]])
    if float((o * (out[j] + out[j + 1])).sum()) < 0:
        o = -o
    return line[j] + f * (line[j + 1] - line[j]), z[j] + f * (z[j + 1] - z[j]), t, o


def mesh(edge, out, z, town=False):
    """A deck's edge as the German cap, guard rail and railing: edge (k, 2) the slab's outer edge (a parapet run's),
    out (k, 2) the way out from the road, z (k,) the road's surface there; town: the narrow cap and the railing alone
    (TOWN_CAP_W_M, TOWN_RAILING_AT_M, no guard rail). ((positions, triangles, uv) of the cap in concrete, (positions,
    triangles, uv) of the guard rail and the railing in steel). Pure."""
    edge = np.asarray(edge, np.float64)
    out = _unit(out)
    z = np.asarray(z, np.float64)
    road = edge - out * SLAB_W_M                       # the road's edge: every offset below is from it
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(road, axis=0).T))]
    cap_w, at = (TOWN_CAP_W_M, TOWN_RAILING_AT_M) if town else (CAP_W_M, RAILING_AT_M)
    cap = sweep(road, out, z, [(0.0, 0.0), (0.0, CAP_H_M)], [(cap_w, -CORNICE_D_M), (cap_w, CAP_H_M)])
    top = z + CAP_H_M
    parts = []
    if not town:
        parts += _guard_rail(road, out, top, s)
    parts += _railing(road, out, top, s, at)
    P, T, UV, nv = [], [], [], 0
    for pp, tt, uu in parts:
        P.append(pp)
        T.append(tt + nv)
        UV.append(uu / 0.3)                # (the steel's texture every 0.3 m)
        nv += len(pp)
    return (cap[0], cap[1], cap[2] / 2.0), (np.concatenate(P), np.concatenate(T), np.concatenate(UV))


def _guard_rail(road, out, top, s):
    """The guard rail on the cap: a W beam, spacers and posts (sweep parts)."""
    parts = []
    # the guard rail: a W beam (ridges at its face, the valley and the edges RAIL_DEPTH_M back), spacers, posts
    mid = (RAIL_LOW_M + RAIL_HIGH_M) / 2
    q = (RAIL_HIGH_M - RAIL_LOW_M) / 4
    face = [(RAIL_FACE_M + RAIL_DEPTH_M, RAIL_LOW_M), (RAIL_FACE_M, mid - q), (RAIL_FACE_M + RAIL_DEPTH_M, mid),
            (RAIL_FACE_M, mid + q), (RAIL_FACE_M + RAIL_DEPTH_M, RAIL_HIGH_M)]
    parts.append(sweep(road, out, top, face, [(a + RAIL_SHEET_M, u) for a, u in face]))
    behind = RAIL_FACE_M + RAIL_DEPTH_M + RAIL_SHEET_M
    for sp in _stations(s, GPOST_EVERY_M, GPOST_L_M):
        p, zz, t, o = _at(road, s, top, out, sp)
        a = np.array([p - t * GPOST_L_M / 2, p + t * GPOST_L_M / 2])
        oo, zz2 = np.array([o, o]), np.array([zz, zz])
        parts.append(sweep(a, oo, zz2, *_box(behind, behind + SPACER_M, mid - q, mid + q)))
        parts.append(sweep(a, oo, zz2, *_box(behind + SPACER_M, behind + SPACER_M + GPOST_W_M, 0.0, GPOST_H_M)))
    return parts


def _railing(road, out, top, s, at):
    """The railing with its centre line `at` m out from the road's edge: posts, the handrail and two rails."""
    parts = []
    parts.append(sweep(road, out, top, *_box(at - HANDRAIL_W_M / 2, at + HANDRAIL_W_M / 2,
                                            RAILING_H_M - HANDRAIL_H_M, RAILING_H_M)))
    for up in BARS_AT_M:
        parts.append(sweep(road, out, top, *_box(at - BAR_M / 2, at + BAR_M / 2,
                                                up - BAR_M / 2, up + BAR_M / 2)))
    for sp in _stations(s, POST_EVERY_M, POST_M / 2):
        p, zz, t, o = _at(road, s, top, out, sp)
        a = np.array([p - t * POST_M / 2, p + t * POST_M / 2])
        parts.append(sweep(a, np.array([o, o]), np.array([zz, zz]),
                           *_box(at - POST_M / 2, at + POST_M / 2, 0.0, RAILING_H_M - HANDRAIL_H_M)))
    return parts
