"""Landmarks (GitHub #123: "apply details to all known monuments"): a few world-famous structures drawn as models of
their own instead of their map footprint and parts, keyed by the OpenStreetMap way of the building (Overture's
src_record "w<id>@<version>": the same everywhere, whatever the version).

Each model is made here from the structure's published dimensions (no third party's model or photos copied: the
structures below are long out of copyright), on the building's own footprint (its oriented box: the centre, the axes
and the base's size), so it stands where the map puts it and faces the way it faces. Their material glows at night
(nightlights.lua, as the street lamps' lenses): lit, not a light show.

    landmarks.of(src_record) -> name or None          (tile_buildings: draw this building as that landmark)
    landmarks.spec(name, ring, height) -> Spec        (plan)
    landmarks.build(m, spec, gmin, mat, lite)          (build, the skyline: its triangles into a buildings.Mesh)
    landmarks.collision(m, spec, gmin, mat)            (collision_mesh: the legs alone, open under the arches)

The Eiffel Tower (Tour Eiffel, 1889, OSM way 5013364): a 124.9 m square base on four legs, its first platform at
57.6 m (70.7 m square), the second at 115.7 m (37 m), the top at 276.1 m (17 m) under the lantern to 300 m and the
antenna to 330 m (OSM's building parts give the same levels). The legs curve in as one wrought-iron lattice: each
leg four corner girders braced in a criss-cross, joined above the second platform into one shaft; the four arches
under the first platform. Its paint, "Eiffel Tower brown", is drawn from vertex colours; at night its gold lighting
(sodium lamps, since 1985) as an even warm glow.
"""
import math

import numpy as np

LANDMARK = "landmark"           # the Spec class of a landmark (its name in Spec.facade)

# OSM element ("w" a way, "r" a relation: a footprint with a courtyard) and id -> name
WAYS = {("w", 5013364): "eiffel", ("w", 226413508): "arc", ("w", 123557148): "bigben", ("r", 1834818): "colosseum",
        ("w", 32965412): "liberty"}


def of(src_record):
    """The landmark a building is, from its Overture src_record ("w5013364@184", "r1834818@68"), or None."""
    s = str(src_record or "")
    if s[:1] not in ("w", "r"):
        return None
    try:
        osm_id = int(s[1:].split("@", 1)[0])
    except ValueError:
        return None
    return WAYS.get((s[0], osm_id))


# ------------------------------------------------------------------------------------------- the Eiffel Tower

EIFFEL_BASE_M = 124.9           # the base's side (between the legs' outer corners)
EIFFEL_LEG_M = 25.0             # a leg's side at the ground ...
EIFFEL_LEG_P1_M = 15.0          # ... and at the first platform (the square between the legs there ~40 m wide)
EIFFEL_P1_Z, EIFFEL_P1_M = 57.6, 70.7       # the first platform: its height and side
EIFFEL_P2_Z, EIFFEL_P2_M = 115.7, 37.0      # the second
EIFFEL_P3_Z, EIFFEL_P3_M = 276.1, 17.0      # the top platform
EIFFEL_TOP_Z = 300.0            # the lantern's top
EIFFEL_ANTENNA_Z = 330.0
EIFFEL_ARCH_Z = 39.0            # the arches' crown under the first platform
EIFFEL_COLOUR = (106, 84, 64, 255)          # "brun Tour Eiffel" (its paint, by vertex colour)
GIRDER_M = 1.1                  # a corner girder's side (the lite mesh's: LITE_GIRDER_M)
LITE_GIRDER_M = 3.0
BRACE_M = 0.45                  # a bracing bar's side
PANEL_M = 9.0                   # m between the bracing's crossings up a leg
STEP_M = 6.0                    # m between the curved girders' points
# Its lighting: warm lamps on the lattice (the gold lighting since 1985 comes from sodium lamps inside the structure),
# lit at night as points of light (nightlights.lua, bne_lm_lamp); the iron itself stays dark. Static: the hourly
# sparkle is the protected light show, not drawn.
LAMP_STEP_M = 4.0               # m between the lamps up a girder ...
LAMP_BRACES = (1 / 3, 2 / 3)    # ... along each bar of the bracing (the lattice lit through, not only its edges)
LAMP_EDGE_M = 5.0               # ... and round a platform's edge
LAMP_R = 0.35                   # a lamp's size (its octahedron's half diagonal)
LITE_LAMP_STEP_M, LITE_LAMP_R = 12.0, 1.2   # far away: fewer, bigger (the outline as a line of lights)
LAMP_COLOUR = (196, 160, 96, 255)           # a fitting's brass by day


def _half(z):
    """Half the tower's outer width at height z (m): the legs' outer edge, through the platforms' sizes, curving in
    as the real tower does (log-linear between its levels)."""
    pts = ((0.0, EIFFEL_BASE_M / 2), (EIFFEL_P1_Z, EIFFEL_P1_M / 2 + 2.0), (EIFFEL_P2_Z, EIFFEL_P2_M / 2 + 1.0),
           (200.0, 12.5), (EIFFEL_P3_Z, EIFFEL_P3_M / 2), (EIFFEL_TOP_Z, 4.0))
    zs = np.array([p[0] for p in pts])
    ws = np.log([p[1] for p in pts])
    return float(np.exp(np.interp(z, zs, ws)))


def _leg(z):
    """A leg's side at height z: EIFFEL_LEG_M at the ground narrowing to EIFFEL_LEG_P1_M at the first platform, then
    widening to the whole half width where the legs meet (the second platform: one shaft over it)."""
    if z <= EIFFEL_P1_Z:
        return EIFFEL_LEG_M + (EIFFEL_LEG_P1_M - EIFFEL_LEG_M) * max(z, 0.0) / EIFFEL_P1_Z
    t = min((z - EIFFEL_P1_Z) / (EIFFEL_P2_Z - EIFFEL_P1_Z), 1.0)
    return EIFFEL_LEG_P1_M + (_half(z) - EIFFEL_LEG_P1_M) * t * t


def beam(m, p, q, w, mat, up=(0.0, 0.0, 1.0)):
    """A square bar of side w from p to q (3D points): its four sides as quads (no end caps: the bars meet others)."""
    p, q = np.asarray(p, np.float64), np.asarray(q, np.float64)
    d = q - p
    L = float(np.linalg.norm(d))
    if L < 1e-6:
        return
    d /= L
    a = np.cross(d, up)
    if np.linalg.norm(a) < 1e-6:                    # (a vertical bar)
        a = np.cross(d, (1.0, 0.0, 0.0))
    a /= np.linalg.norm(a)
    b = np.cross(d, a)
    h = w / 2
    c = [a * h + b * h, -a * h + b * h, -a * h - b * h, a * h - b * h]
    uv = (0.0, 0.0)
    for k in range(4):
        e, f = c[k], c[(k + 1) % 4]
        m.quad(tuple(p + e), tuple(p + f), tuple(q + f), tuple(q + e), uv, uv, uv, uv, mat)


LAMP_TRIS = 4


def lamp(m, p, r, mat):
    """A lamp: a small tetrahedron of size r at p (lit at night by its material; 4 triangles, the tower has ~2,000)."""
    x, y, z = p
    a, b, c = (x + r, y, z - r * 0.35), (x - r * 0.5, y + r * 0.87, z - r * 0.35), (x - r * 0.5, y - r * 0.87, z - r * 0.35)
    t = (x, y, z + r)
    uv = (0.0, 0.0)
    for p0, p1, p2 in ((a, b, t), (b, c, t), (c, a, t), (a, c, b)):
        m.tri(p0, p1, p2, uv, uv, uv, mat)


def _slab(m, corners, z0, z1, mat):
    """A box over the quadrilateral corners (4 (x, y), counter-clockwise) from z0 to z1: its sides, top and bottom."""
    uv = (0.0, 0.0)
    for k in range(4):
        a, b = corners[k], corners[(k + 1) % 4]
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), uv, uv, uv, uv, mat)
    m.poly([(x, y, z1) for x, y in corners], [uv] * 4, mat)
    m.poly([(x, y, z0) for x, y in corners[::-1]], [uv] * 4, mat)


def _frame(spec):
    """(centre, u, v): the tower's centre and its two horizontal axes, from its footprint's oriented box."""
    import buildings
    c, u, _hl, _hw = buildings.oriented_box(np.asarray(spec.ring, np.float64)[:, :2])
    return np.asarray(c, np.float64), np.asarray(u, np.float64), np.array([-u[1], u[0]])


def _square(c, u, v, half):
    """The 4 corners of a square of half side `half` on the axes, counter-clockwise."""
    return [tuple(c + u * su * half + v * sv * half) for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def eiffel(m, spec, gmin, mat, lite=False, tokens=None):
    """The Eiffel Tower on spec's footprint, standing on gmin (its triangles into m in mat; its lamps in tokens' "lamp")."""
    lamp_mat = (tokens or {}).get("lamp")
    c, u, v = _frame(spec)
    z_top = _shaft_z = EIFFEL_P2_Z

    def at(sx, sy, ox, oy, z):              # a point: the leg's corner (ox, oy of the outer half width) at height z
        return (c[0] + (u[0] * sx * ox + v[0] * sy * oy), c[1] + (u[1] * sx * ox + v[1] * sy * oy), gmin + z)

    gw = LITE_GIRDER_M if lite else GIRDER_M
    bars = []                               # (the bracing and the arches: their lamps, _eiffel_lamps)
    zs = list(np.arange(0.0, z_top, STEP_M)) + [z_top]
    # the four legs: four corner girders each, curving in (outer corner on the outer edge, inner corner a leg's side in)
    for sx in (-1, 1):
        for sy in (-1, 1):
            for cx, cy in ((0, 0), (1, 0), (1, 1), (0, 1)):     # 0: on the outer edge, 1: a leg's side inward
                pts = [at(sx, sy, _half(z) - cx * _leg(z), _half(z) - cy * _leg(z), z) for z in zs]
                for a, b in zip(pts[:-1], pts[1:]):
                    beam(m, a, b, gw, mat)
                if lite:
                    break                                       # (far away: one girder a leg)
            if lite:
                continue
            # bracing: a cross on each of the leg's four faces every PANEL_M
            pz = list(np.arange(0.0, z_top, PANEL_M)) + [z_top]
            for z0, z1 in zip(pz[:-1], pz[1:]):
                for (ax, ay), (bx, by) in (((0, 0), (1, 0)), ((1, 0), (1, 1)), ((1, 1), (0, 1)), ((0, 1), (0, 0))):
                    p0 = at(sx, sy, _half(z0) - ax * _leg(z0), _half(z0) - ay * _leg(z0), z0)
                    q0 = at(sx, sy, _half(z0) - bx * _leg(z0), _half(z0) - by * _leg(z0), z0)
                    p1 = at(sx, sy, _half(z1) - ax * _leg(z1), _half(z1) - ay * _leg(z1), z1)
                    q1 = at(sx, sy, _half(z1) - bx * _leg(z1), _half(z1) - by * _leg(z1), z1)
                    beam(m, p0, q1, BRACE_M, mat)
                    beam(m, q0, p1, BRACE_M, mat)
                    bars += [(p0, q1), (q0, p1)]
    # the arches between the legs, under the first platform: from one leg's inner face to the next, crowned at
    # EIFFEL_ARCH_Z, on each of the four sides
    if not lite:
        for axis in (0, 1):
            for side in (-1, 1):
                pts = []
                for t in np.linspace(-1.0, 1.0, 17):
                    z = 8.0 + (EIFFEL_ARCH_Z - 8.0) * math.sqrt(max(0.0, 1.0 - t * t))
                    along, across = t * (_half(z) - _leg(z)), side * (_half(z) - 0.5)
                    x, y = (along, across) if axis == 0 else (across, along)
                    pts.append((c[0] + u[0] * x + v[0] * y, c[1] + u[1] * x + v[1] * y, gmin + z))
                for a, b in zip(pts[:-1], pts[1:]):
                    beam(m, a, b, 1.6, mat)
                    bars.append((a, b))
    # the first and second platforms: a deck round the tower (the first a square ring, its middle open) with a band
    p1h, p1in = EIFFEL_P1_M / 2, EIFFEL_P1_M / 2 - 12.0
    if lite:
        _slab(m, _square(c, u, v, p1h), gmin + EIFFEL_P1_Z, gmin + EIFFEL_P1_Z + 4.0, mat)
    else:
        for k in range(4):                  # four slabs round the open middle
            a = np.array(_square(c, u, v, p1h)[k])
            b = np.array(_square(c, u, v, p1h)[(k + 1) % 4])
            ai = np.array(_square(c, u, v, p1in)[k])
            bi = np.array(_square(c, u, v, p1in)[(k + 1) % 4])
            _slab(m, [tuple(a), tuple(b), tuple(bi), tuple(ai)], gmin + EIFFEL_P1_Z, gmin + EIFFEL_P1_Z + 4.0, mat)
    _slab(m, _square(c, u, v, EIFFEL_P2_M / 2), gmin + EIFFEL_P2_Z, gmin + EIFFEL_P2_Z + 3.0, mat)
    # the shaft over the second platform: four corner girders to the top platform, braced
    zs2 = list(np.arange(_shaft_z, EIFFEL_P3_Z, STEP_M)) + [EIFFEL_P3_Z]
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        pts = [at(sx, sy, _half(z) - 0.6, _half(z) - 0.6, z) for z in zs2]
        for a, b in zip(pts[:-1], pts[1:]):
            beam(m, a, b, gw, mat)
    if not lite:
        pz = list(np.arange(_shaft_z, EIFFEL_P3_Z, PANEL_M)) + [EIFFEL_P3_Z]
        corners = ((-1, -1), (1, -1), (1, 1), (-1, 1))
        for z0, z1 in zip(pz[:-1], pz[1:]):
            for k in range(4):
                (ax, ay), (bx, by) = corners[k], corners[(k + 1) % 4]
                h0, h1 = _half(z0) - 0.6, _half(z1) - 0.6
                beam(m, at(ax, ay, h0, h0, z0), at(bx, by, h1, h1, z1), BRACE_M, mat)
                beam(m, at(bx, by, h0, h0, z0), at(ax, ay, h1, h1, z1), BRACE_M, mat)
                bars += [(at(ax, ay, h0, h0, z0), at(bx, by, h1, h1, z1)),
                         (at(bx, by, h0, h0, z0), at(ax, ay, h1, h1, z1))]
    # the top: its platform, the lantern and the antenna
    _slab(m, _square(c, u, v, EIFFEL_P3_M / 2), gmin + EIFFEL_P3_Z, gmin + EIFFEL_P3_Z + 4.0, mat)
    _slab(m, _square(c, u, v, 5.0), gmin + EIFFEL_P3_Z + 4.0, gmin + EIFFEL_TOP_Z, mat)
    beam(m, (c[0], c[1], gmin + EIFFEL_TOP_Z), (c[0], c[1], gmin + EIFFEL_ANTENNA_Z), 1.2 if not lite else 2.5, mat,
         up=(1.0, 0.0, 0.0))
    if lamp_mat is not None:
        _eiffel_lamps(m, c, u, v, gmin, lamp_mat, lite, bars)


def _eiffel_lamps(m, c, u, v, gmin, mat, lite, bars=()):
    """The tower's lamps: up every girder of the legs and the shaft, along the bars of its bracing and its arches
    (the lattice lit through), round the platforms' edges; just outside the iron, away from the tower's axis."""
    step, r = (LITE_LAMP_STEP_M, LITE_LAMP_R) if lite else (LAMP_STEP_M, LAMP_R)
    out = (LITE_GIRDER_M if lite else GIRDER_M) / 2 + r

    def put(x, y, z):                       # (x, y on the tower's axes: pushed out from its axis by `out`)
        d = math.hypot(x, y) or 1.0
        x, y = x + out * x / d, y + out * y / d
        lamp(m, (c[0] + u[0] * x + v[0] * y, c[1] + u[1] * x + v[1] * y, gmin + z), r, mat)
    corners = ((0, 0),) if lite else ((0, 0), (1, 0), (1, 1), (0, 1))
    for sx in (-1, 1):
        for sy in (-1, 1):
            for cx, cy in corners:
                for z in np.arange(step / 2, EIFFEL_P2_Z, step):
                    put(sx * (_half(z) - cx * _leg(z)), sy * (_half(z) - cy * _leg(z)), z)
            for z in np.arange(EIFFEL_P2_Z + step / 2, EIFFEL_P3_Z, step):       # the shaft
                put(sx * (_half(z) - 0.6), sy * (_half(z) - 0.6), z)
    for a, b in bars:                       # the bracing and the arches (absolute points: back onto the axes)
        a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
        for t in LAMP_BRACES:
            p = a + (b - a) * t
            w = p[:2] - c
            x, y = float(w @ u), float(w @ v)
            d = math.hypot(x, y) or 1.0
            q = (BRACE_M / 2 + r) / d
            lamp(m, (c[0] + u[0] * x * (1 + q) + v[0] * y * (1 + q), c[1] + u[1] * x * (1 + q) + v[1] * y * (1 + q),
                     float(p[2])), r, mat)
    for z, half in ((EIFFEL_P1_Z + 4.0, EIFFEL_P1_M / 2), (EIFFEL_P2_Z + 3.0, EIFFEL_P2_M / 2),
                    (EIFFEL_P3_Z + 4.0, EIFFEL_P3_M / 2)):                       # the platforms' edges
        n = max(1, int(2 * half / (LAMP_EDGE_M * (3 if lite else 1))))
        for k in range(n):
            t = -half + (k + 0.5) * 2 * half / n
            for x, y in ((t, -half), (half, t), (-t, half), (-half, -t)):
                put(x, y, z)


def eiffel_collision(m, spec, gmin, mat):
    """The tower for physics: its four legs (each a box tapering to the first platform: driven between, under the
    arches) and the first platform's deck."""
    c, u, v = _frame(spec)
    for sx in (-1, 1):
        for sy in (-1, 1):
            for z0, z1 in ((0.0, 20.0), (20.0, 40.0), (40.0, EIFFEL_P1_Z)):
                h, lg = _half((z0 + z1) / 2), _leg((z0 + z1) / 2)
                mid = c + u * sx * (h - lg / 2) + v * sy * (h - lg / 2)
                q = _square(mid, u, v, lg / 2)
                _slab(m, q, gmin + z0 - (1.0 if z0 == 0 else 0.0), gmin + z1, mat)
    _slab(m, _square(c, u, v, EIFFEL_P1_M / 2), gmin + EIFFEL_P1_Z, gmin + EIFFEL_P1_Z + 4.0, mat)


def _local(c, u, v):
    """(x, y) on the landmark's axes -> (x, y) in the tile."""
    return lambda x, y: (c[0] + u[0] * x + v[0] * y, c[1] + u[1] * x + v[1] * y)


def _lbox(m, P, x0, x1, y0, y1, z0, z1, mat):
    """A box from x0..x1 along u, y0..y1 along v (P: _local), z0..z1."""
    if x1 - x0 < 1e-3 or y1 - y0 < 1e-3 or z1 - z0 < 1e-3:
        return
    _slab(m, [P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)], z0, z1, mat)


def _oriented(spec):
    """(centre, u, v, half length along u, half width along v) of the footprint's oriented box."""
    import buildings
    c, u, hl, hw = buildings.oriented_box(np.asarray(spec.ring, np.float64)[:, :2])
    u = np.asarray(u, np.float64)
    return np.asarray(c, np.float64), u, np.array([-u[1], u[0]]), float(hl), float(hw)


# ------------------------------------------------------------------------------------------- the Arc de Triomphe

# (1806-1836: 49.5 m high, 45 m wide, 22 m deep; the great arch 29.19 m high and 14.62 m wide through its depth, the
# side arches 18.68 m high and 8.44 m wide through its width; the entablature and attic above. Its stone: Château-Landon
# limestone, a warm white.)
ARC_H, ARC_W, ARC_D = 49.5, 45.0, 22.0
ARC_MAIN_H, ARC_MAIN_W = 29.19, 14.62
ARC_SIDE_H, ARC_SIDE_W = 18.68, 8.44
ARC_CORNICE_Z = 37.0            # the entablature's cornice (the frieze under it), the attic over it
ARC_COLOUR = (222, 212, 192, 255)
ARC_SLABS = 14                  # the vaults' curve in this many steps


def _arch_half(z, top, width):
    """Half the opening of a round-topped arch (crown at top, width) at height z: full width up to its springing."""
    r = width / 2
    spring = top - r
    if z <= spring:
        return r
    if z >= top:
        return 0.0
    return math.sqrt(max(0.0, r * r - (z - spring) ** 2))


def arc(m, spec, gmin, mat, lite=False, tokens=None):
    """The Arc de Triomphe on spec's footprint (the great arch through the footprint's short side)."""
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    W, D = ARC_W / 2, ARC_D / 2             # (along u the 45 m front, along v the 22 m sides)
    zs = [0.0, ARC_SIDE_H - ARC_SIDE_W / 2]                     # up to the side arches' springing: the four piers
    steps = 4 if lite else ARC_SLABS
    zs += list(np.linspace(ARC_SIDE_H - ARC_SIDE_W / 2, ARC_SIDE_H, steps // 2 + 1)[1:])
    zs += list(np.linspace(ARC_SIDE_H, ARC_MAIN_H, steps + 1)[1:])
    for z0, z1 in zip(zs[:-1], zs[1:]):
        zm = (z0 + z1) / 2
        a = _arch_half(zm, ARC_MAIN_H, ARC_MAIN_W)              # the great arch's half opening (along u)
        b = _arch_half(zm, ARC_SIDE_H, ARC_SIDE_W)              # the side arches' (along v)
        lo = gmin + z0 - (1.0 if z0 == 0 else 0.0)
        for sx in (-1, 1):
            for sy in (-1, 1):
                x0, x1 = sorted((sx * a, sx * W))
                y0, y1 = sorted((sy * b, sy * D))
                _lbox(m, P, x0, x1, y0, y1, lo, gmin + z1, mat)
    _lbox(m, P, -W, W, -D, D, gmin + ARC_MAIN_H, gmin + ARC_CORNICE_Z, mat)         # the entablature
    _lbox(m, P, -W - 0.8, W + 0.8, -D - 0.8, D + 0.8, gmin + ARC_CORNICE_Z, gmin + ARC_CORNICE_Z + 1.2, mat)
    _lbox(m, P, -W, W, -D, D, gmin + ARC_CORNICE_Z + 1.2, gmin + ARC_H - 1.0, mat)  # the attic
    _lbox(m, P, -W - 0.4, W + 0.4, -D - 0.4, D + 0.4, gmin + ARC_H - 1.0, gmin + ARC_H, mat)


def arc_collision(m, spec, gmin, mat):
    """Its four piers (to the great arch's springing) and the mass over the vault: driven and walked through."""
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    W, D, a, b = ARC_W / 2, ARC_D / 2, ARC_MAIN_W / 2, ARC_SIDE_W / 2
    for sx in (-1, 1):
        for sy in (-1, 1):
            x0, x1 = sorted((sx * a, sx * W))
            y0, y1 = sorted((sy * b, sy * D))
            _lbox(m, P, x0, x1, y0, y1, gmin - 1.0, gmin + ARC_SIDE_H, mat)
    _lbox(m, P, -W, W, -D, D, gmin + ARC_MAIN_H - 2.0, gmin + ARC_H, mat)


# ------------------------------------------------------------------------------------------- Big Ben

# The Elizabeth Tower (1843-1859, Pugin and Barry): 96 m, 12 m square; its clock stage (four 7 m dials) at 55 m, the
# belfry over it, then the cast-iron spire and lantern. Anston limestone (a honey stone), the spire's iron dark with
# gilding.
BB_H, BB_W = 96.0, 12.0
BB_CLOCK_Z, BB_CLOCK_H = 54.0, 10.0      # the clock stage: from, how high
BB_DIAL_R = 3.5
BB_BELFRY_Z = 66.0
BB_SPIRE_Z = 76.0                        # the spire's foot (over the belfry's roof line)
BB_COLOUR = (205, 180, 128, 255)
BB_DIAL = (238, 232, 214, 255)           # the dials' opal glass
BB_SPIRE = (70, 72, 78, 255)


def bigben(m, spec, gmin, mat, lite=False, tokens=None):
    """The Elizabeth Tower (its stone floodlit in mat, its dials in tokens' "dial": lit from behind at night)."""
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    h = BB_W / 2
    dial_mat = (tokens or {}).get("dial")
    stone, iron = mat, (mat[0], BB_SPIRE)
    dial = (dial_mat[0], BB_DIAL) if dial_mat is not None else (mat[0], BB_DIAL)
    _lbox(m, P, -h, h, -h, h, gmin - 1.0, gmin + BB_CLOCK_Z, stone)                    # the shaft
    if not lite:                                                                       # its corner buttresses
        for sx in (-1, 1):
            for sy in (-1, 1):
                _lbox(m, P, sx * h - 0.9 if sx > 0 else -h - 0.1, sx * h + 0.1 if sx > 0 else -h + 0.9,
                      sy * h - 0.9 if sy > 0 else -h - 0.1, sy * h + 0.1 if sy > 0 else -h + 0.9,
                      gmin, gmin + BB_CLOCK_Z + BB_CLOCK_H, stone)
    o = 0.6                                                                            # the clock stage stands out
    _lbox(m, P, -h - o, h + o, -h - o, h + o, gmin + BB_CLOCK_Z, gmin + BB_CLOCK_Z + BB_CLOCK_H, stone)
    # the four dials: a disc proud of each face (counter-clockwise seen from outside)
    zc = gmin + BB_CLOCK_Z + BB_CLOCK_H / 2
    n = 12 if lite else 32
    for ax, sgn in ((0, 1), (0, -1), (1, 1), (1, -1)):
        d = h + o + 0.05
        ring = []
        for k in range(n):
            t = 2 * math.pi * k / n
            s_, z_ = BB_DIAL_R * math.cos(t), BB_DIAL_R * math.sin(t)
            x, y = (sgn * d, -sgn * s_) if ax == 0 else (sgn * s_, sgn * d)
            ring.append((*P(x, y), zc + z_))
        uv = (0.0, 0.0)
        cen = (*P(*((sgn * d, 0.0) if ax == 0 else (0.0, sgn * d))), zc)
        for k in range(n):
            a, b = ring[k], ring[(k + 1) % n]
            m.tri(cen, a, b, uv, uv, uv, dial)
            m.tri(cen, b, a, uv, uv, uv, dial)                                         # (seen from either side)
    # the belfry (open arches as a lighter frame on each face) and its roof line
    zb = gmin + BB_CLOCK_Z + BB_CLOCK_H
    if lite:
        _lbox(m, P, -h, h, -h, h, zb, gmin + BB_SPIRE_Z, stone)
    else:
        for sx in (-1, 1):
            for sy in (-1, 1):                                                         # corner piers ...
                _lbox(m, P, min(sx * h, sx * (h - 2.2)), max(sx * h, sx * (h - 2.2)),
                      min(sy * h, sy * (h - 2.2)), max(sy * h, sy * (h - 2.2)), zb, gmin + BB_SPIRE_Z, stone)
        _lbox(m, P, -h, h, -h, h, gmin + BB_SPIRE_Z - 2.5, gmin + BB_SPIRE_Z, stone)  # ... under the roof line
        _lbox(m, P, -h + 2.2, h - 2.2, -h + 2.2, h - 2.2, zb, gmin + BB_SPIRE_Z - 2.5, iron)   # the bells' dark room
    # the spire: a steep pyramid to the lantern, then the lantern's spike
    top = gmin + BB_H - 6.0
    base = [P(-h, -h), P(h, -h), P(h, h), P(-h, h)]
    uv = (0.0, 0.0)
    apex = (*P(0.0, 0.0), top)
    for k in range(4):
        a, b = base[k], base[(k + 1) % 4]
        m.tri((*a, gmin + BB_SPIRE_Z), (*b, gmin + BB_SPIRE_Z), apex, uv, uv, uv, iron)
    beam(m, (*P(0.0, 0.0), top - 2.0), (*P(0.0, 0.0), gmin + BB_H), 1.0, iron, up=(1.0, 0.0, 0.0))


def bigben_collision(m, spec, gmin, mat):
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    h = BB_W / 2
    _lbox(m, P, -h, h, -h, h, gmin - 1.0, gmin + BB_SPIRE_Z, mat)


# ------------------------------------------------------------------------------------------- the Colosseum

# (70-80 AD: an ellipse 189 x 156 m, 48 m high; three arcaded storeys of 80 arches each, an attic over them, the arena
# 87 x 55 m. The outer ring stands on its north side; on the south it fell in earthquakes, leaving the inner ring, two
# storeys lower. Travertine, a warm grey-cream.)
COL_A, COL_B = 94.5, 78.0               # the outer wall's semi-axes
COL_ARENA_A, COL_ARENA_B = 43.5, 27.5
COL_TIERS = (10.5, 11.9, 11.6)          # the arcaded storeys' heights
COL_ATTIC_H = 14.0
COL_ARCHES = 80
COL_PIER_SHARE = 0.52                   # of a bay the pier between two arches takes
COL_INNER = 0.86                        # the inner ring's ellipse, of the outer's
COL_COLOUR = (206, 192, 166, 255)


def _ellipse(t, a, b):
    return a * math.cos(t), b * math.sin(t)


COL_LAMP_R = 0.6                # a lamp in the top of each arch (the arcades lit from inside at night) ...
COL_EDGE_LAMP_M, COL_EDGE_LAMP_R = 3.0, 0.4     # ... and lamps along the top edge of each storey and of the attic:
                                                # its tiers drawn in light, the walls themselves dark


def colosseum(m, spec, gmin, mat, lite=False, tokens=None):
    """The Colosseum (its travertine dark at night in mat; in tokens' "lamp" a lamp in each arch and rows of lamps
    along the top edge of each storey and of the attic: its shape drawn in light)."""
    lamp_mat = (tokens or {}).get("lamp")
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    sa, sb = max(hl, 40.0) / COL_A, max(hw, 30.0) / COL_B          # (its size from the footprint)
    A, B = COL_A * sa, COL_B * sb
    north = np.array([0.0, 1.0])            # (the anchor frame's +y)
    uv = (0.0, 0.0)
    n = 40 if lite else COL_ARCHES
    tiers = np.cumsum((0.0,) + COL_TIERS)    # 0, 10.5, 22.4, 34.0
    top = tiers[-1] + COL_ATTIC_H

    def ring_wall(scale, z0, z1, t0, t1, thick):
        """A smooth band of wall on the ellipse scale x (A, B) from z0 to z1, angles t0..t1, `thick` m deep."""
        ts = np.linspace(t0, t1, max(2, int((t1 - t0) / (2 * math.pi) * n) + 1))
        for a0, a1 in zip(ts[:-1], ts[1:]):
            for d in (0.0, -thick):
                p0 = _ellipse(a0, A * scale + d, B * scale + d)
                p1 = _ellipse(a1, A * scale + d, B * scale + d)
                q0, q1 = P(*p0), P(*p1)
                if d == 0.0:
                    m.quad((*q0, z0), (*q1, z0), (*q1, z1), (*q0, z1), uv, uv, uv, uv, mat)
                else:
                    m.quad((*q1, z0), (*q0, z0), (*q0, z1), (*q1, z1), uv, uv, uv, uv, mat)
            o0, o1 = P(*_ellipse(a0, A * scale, B * scale)), P(*_ellipse(a1, A * scale, B * scale))
            i0, i1 = P(*_ellipse(a0, A * scale - thick, B * scale - thick)), P(*_ellipse(a1, A * scale - thick, B * scale - thick))
            m.quad((*o0, z1), (*o1, z1), (*i1, z1), (*i0, z1), uv, uv, uv, uv, mat)
        if lamp_mat is not None and not lite:           # its top edge in light, just outside the wall
            perim = math.pi * (3 * (A + B) - math.sqrt((3 * A + B) * (A + 3 * B))) * scale    # (Ramanujan)
            k = max(1, int(perim * (t1 - t0) / (2 * math.pi) / COL_EDGE_LAMP_M))
            for t in t0 + (np.arange(k) + 0.5) * (t1 - t0) / k:
                x, y = _ellipse(t, A * scale + COL_EDGE_LAMP_R, B * scale + COL_EDGE_LAMP_R)
                lamp(m, (*P(x, y), z1 - COL_EDGE_LAMP_R - 0.2), COL_EDGE_LAMP_R, lamp_mat)

    def arcade(scale, z0, z1, t0, t1, thick):
        """A storey of arches: a pier per bay, the arch's top band over them."""
        k0, k1 = int(math.floor(t0 / (2 * math.pi) * n)), int(math.ceil(t1 / (2 * math.pi) * n))
        band = (z1 - z0) * 0.34
        for k in range(k0, k1):
            t = 2 * math.pi * (k + 0.5) / n
            if not (t0 <= t <= t1):
                continue
            half = math.pi / n * COL_PIER_SHARE
            pts = []
            for d in (0.0, -thick):
                pts += [P(*_ellipse(t - half, A * scale + d, B * scale + d)), P(*_ellipse(t + half, A * scale + d, B * scale + d))]
            o0, o1, i0, i1 = pts
            _slab(m, [o0, o1, i1, i0], z0, z1 - band, mat)
            if lamp_mat is not None and not lite:       # the arch after this pier: its lamp under its top, mid-wall
                to = 2 * math.pi * (k + 1) / n
                if to <= t1:
                    x, y = _ellipse(to, A * scale - thick / 2, B * scale - thick / 2)
                    lamp(m, (*P(x, y), z1 - band - COL_LAMP_R - 0.3), COL_LAMP_R, lamp_mat)
        ring_wall(scale, z1 - band, z1, t0, t1, thick)

    def standing(t):                        # the outer ring stands where it faces north
        x, y = _ellipse(t, A, B)
        w = np.asarray(P(x, y)) - c
        return float(np.dot(w / max(np.linalg.norm(w), 1e-9), north)) > -0.15

    ts = np.linspace(0.0, 2 * math.pi, n + 1)
    runs, cur = [], None                    # the angle runs where the outer ring stands
    for a0, a1 in zip(ts[:-1], ts[1:]):
        st = standing((a0 + a1) / 2)
        if cur and cur[2] == st:
            cur[1] = a1
        else:
            cur = [a0, a1, st]
            runs.append(cur)
    for t0, t1, st in runs:
        z = gmin
        if st:                              # the outer ring: three arcaded storeys and the attic
            for z0, z1 in zip(tiers[:-1], tiers[1:]):
                arcade(1.0, z + z0 - (1.0 if z0 == 0 else 0.0), z + z1, t0, t1, 3.0)
            ring_wall(1.0, z + tiers[-1], z + top, t0, t1, 3.0)
        else:                               # fallen: the outer ring's stumps, the inner ring two storeys high
            arcade(1.0, z - 1.0, z + COL_TIERS[0] * 0.55, t0, t1, 3.0)
            for z0, z1 in zip(tiers[:2], tiers[1:3]):
                arcade(COL_INNER, z + z0 - (1.0 if z0 == 0 else 0.0), z + z1, t0, t1, 3.0)
    # the seating (cavea): a cone from the inner ring's top down to the arena's wall, all round
    if True:
        kk = 24 if lite else 72
        ts2 = np.linspace(0.0, 2 * math.pi, kk + 1)
        z_hi, z_lo = gmin + tiers[2] - 1.0, gmin + 4.0
        for a0, a1 in zip(ts2[:-1], ts2[1:]):
            o0, o1 = P(*_ellipse(a0, A * COL_INNER - 3.0, B * COL_INNER - 3.0)), P(*_ellipse(a1, A * COL_INNER - 3.0, B * COL_INNER - 3.0))
            i0, i1 = P(*_ellipse(a0, COL_ARENA_A * sa, COL_ARENA_B * sb)), P(*_ellipse(a1, COL_ARENA_A * sa, COL_ARENA_B * sb))
            m.quad((*i0, z_lo), (*i1, z_lo), (*o1, z_hi), (*o0, z_hi), uv, uv, uv, uv, mat)
            m.quad((*i1, gmin - 1.0), (*i0, gmin - 1.0), (*i0, z_lo), (*i1, z_lo), uv, uv, uv, uv, mat)   # the arena wall


def colosseum_collision(m, spec, gmin, mat):
    c, u, v, hl, hw = _oriented(spec)
    P = _local(c, u, v)
    sa, sb = max(hl, 40.0) / COL_A, max(hw, 30.0) / COL_B
    A, B = COL_A * sa, COL_B * sb
    ts = np.linspace(0.0, 2 * math.pi, 41)
    uv = (0.0, 0.0)
    for a0, a1 in zip(ts[:-1], ts[1:]):
        q0, q1 = P(*_ellipse(a0, A, B)), P(*_ellipse(a1, A, B))
        m.quad((*q0, gmin - 1.0), (*q1, gmin - 1.0), (*q1, gmin + 20.0), (*q0, gmin + 20.0), uv, uv, uv, uv, mat)


# ------------------------------------------------------------------------------------------- the Statue of Liberty

# (Bartholdi and Eiffel, 1886: on the 11-pointed star of Fort Wood, the map's footprint; Hunt's granite pedestal to 47 m
# over the ground; the copper statue 46 m from its base to the torch, 93 m; she faces south-east, to the harbour's
# mouth. Its patina verdigris green, the torch's flame gilded.)
SOL_FORT_H = 9.5
SOL_PED = ((9.5, 16.0, 29.0), (16.0, 20.5, 25.0), (20.5, 41.0, 19.4), (41.0, 43.0, 21.6), (43.0, 47.0, 17.0))
SOL_BASE_Z = 47.0               # the statue's base (the pedestal's top)
SOL_TORCH_Z = 93.0
SOL_FACING = (math.sin(math.radians(135.0)), math.cos(math.radians(135.0)))     # (east, north): south-east
SOL_GRANITE = (178, 170, 160, 255)
SOL_COPPER = (104, 158, 136, 255)
SOL_FLAME = (226, 176, 70, 255)
SOL_LAMP_EDGE_M = 3.0
SOL_FLOODLIT = True             # the statue itself floodlit at night (the pedestal and the fort dark, with their lamps)
# the robe's cross-sections over the statue's base: (height, half width across, half depth front-back)
SOL_ROBE = ((0.0, 5.2, 4.4), (8.0, 4.6, 3.8), (16.0, 4.2, 3.2), (24.0, 4.4, 3.0), (28.5, 3.6, 2.6))


def _centroid(ring):
    """The area centroid of a ring (the fort's star: its pedestal in the middle)."""
    p = np.asarray(ring, np.float64)[:, :2]
    q = np.roll(p, -1, axis=0)
    cr = p[:, 0] * q[:, 1] - q[:, 0] * p[:, 1]
    a = cr.sum() / 2
    if abs(a) < 1e-9:
        return p.mean(0)
    return np.array([((p[:, 0] + q[:, 0]) * cr).sum(), ((p[:, 1] + q[:, 1]) * cr).sum()]) / (6 * a)


def _loft(m, P, sections, z0, mat, n=12):
    """A closed tube through elliptic sections [(z, half width, half depth)] (x across, y front-back), its top capped."""
    rings = []
    for z, hx, hy in sections:
        rings.append([(*P(hx * math.cos(2 * math.pi * k / n), hy * math.sin(2 * math.pi * k / n)), z0 + z)
                      for k in range(n)])
    uv = (0.0, 0.0)
    for r0, r1 in zip(rings[:-1], rings[1:]):
        for k in range(n):
            a, b = r0[k], r0[(k + 1) % n]
            c2, d = r1[(k + 1) % n], r1[k]
            m.quad(a, b, c2, d, uv, uv, uv, uv, mat)
    m.poly(rings[-1], [uv] * n, mat)


def liberty(m, spec, gmin, mat, lite=False, tokens=None):
    """The Statue of Liberty on its fort (spec's ring: the star), facing south-east. tokens' "lamp": its torch's
    flame, the windows of its crown and lamps along the fort's walls and the pedestal's balcony."""
    lamp_mat = (tokens or {}).get("lamp")
    lit = (tokens or {}).get("floodlit") if SOL_FLOODLIT else None
    ring = np.asarray(spec.ring, np.float64)[:, :2]
    c = _centroid(ring)
    fy = np.array(SOL_FACING)                       # her front ...
    fx = np.array([fy[1], -fy[0]])                  # ... and her right
    P = _local(c, fx, fy)
    granite = (mat[0], SOL_GRANITE)
    copper = (lit[0], SOL_COPPER) if lit is not None else (mat[0], SOL_COPPER)     # (the statue floodlit at night)
    uv = (0.0, 0.0)
    # the fort: the star's walls
    r = ring if buildings_area(ring) > 0 else ring[::-1]
    nr = len(r)
    for i in range(nr):
        a, b = r[i], r[(i + 1) % nr]
        m.quad((a[0], a[1], gmin - 1.0), (b[0], b[1], gmin - 1.0), (b[0], b[1], gmin + SOL_FORT_H),
               (a[0], a[1], gmin + SOL_FORT_H), uv, uv, uv, uv, granite)
    m.poly([(p[0], p[1], gmin + SOL_FORT_H) for p in r], [uv] * nr, granite)
    # the pedestal: its stepped granite blocks
    for z0, z1, w in SOL_PED:
        _lbox(m, P, -w / 2, w / 2, -w / 2, w / 2, gmin + z0, gmin + z1, granite)
    zb = gmin + SOL_BASE_Z
    # the statue: the robe, the shoulders and head, the crown's rays, the arms, the tablet, the torch
    _loft(m, P, SOL_ROBE, zb, copper, 8 if lite else 14)
    _loft(m, P, ((28.5, 3.0, 2.2), (30.0, 1.2, 1.2)), zb, copper, 8)                 # the neck
    _loft(m, P, ((30.0, 1.9, 2.1), (31.6, 2.3, 2.5), (33.4, 1.9, 2.2), (34.4, 0.6, 0.6)), zb, copper,
          8 if lite else 12)                                                          # the head
    rays = 7
    for k in range(rays):                           # the crown's seven rays, fanned over her brow
        t = math.radians(-75 + 150 * k / (rays - 1))
        base = (1.9 * math.sin(t), 2.1 * math.cos(t))
        tip = (4.6 * math.sin(t), 4.6 * math.cos(t))
        beam(m, (*P(*base), zb + 33.0), (*P(*tip), zb + 34.6), 0.35 if not lite else 0.8, copper)
    sh = (3.4, 0.4, zb + 27.5)                      # her right shoulder (x right, y front)
    elbow = (4.2, 0.9, zb + 34.5)
    wrist = (3.8, 1.4, zb + 39.0)
    beam(m, (*P(sh[0], sh[1]), sh[2]), (*P(elbow[0], elbow[1]), elbow[2]), 2.2, copper)
    beam(m, (*P(elbow[0], elbow[1]), elbow[2]), (*P(wrist[0], wrist[1]), wrist[2]), 1.8, copper)
    beam(m, (*P(wrist[0], wrist[1]), wrist[2] - 1.0), (*P(wrist[0], wrist[1]), wrist[2] + 2.0), 1.6, copper)  # the hand
    _loft(m, lambda x, y: P(wrist[0] + x, wrist[1] + y),
          ((0.0, 0.6, 0.6), (1.5, 0.9, 0.9), (2.0, 1.5, 1.5), (2.4, 1.6, 1.6)), wrist[2] + 2.0, copper, 8)   # the torch
    flame = [(0.0, 1.2, 1.2), (1.2, 1.0, 1.0), (2.4, 0.5, 0.5), (3.1, 0.05, 0.05)]
    _loft(m, lambda x, y: P(wrist[0] + x, wrist[1] + y), flame, wrist[2] + 4.4,
          (lamp_mat[0], SOL_FLAME) if lamp_mat is not None else (mat[0], SOL_FLAME), 8)
    # the left arm down her side to the tablet, held against her
    beam(m, (*P(-3.4, 0.2), zb + 27.0), (*P(-4.4, 0.8), zb + 19.0), 2.0, copper)
    _lbox(m, P, -5.0, -3.6, 0.6, 2.4, zb + 15.5, zb + 22.7, copper)                    # the tablet (on edge)
    if lamp_mat is None or lite:
        return
    # its lights: the crown's windows (a row of 25 in front of her brow), the fort's walls and the balcony's edge
    for k in range(25):
        t = math.radians(-60 + 120 * k / 24)
        lamp(m, (*P(2.15 * math.sin(t), 2.35 * math.cos(t)), zb + 32.4), 0.18, lamp_mat)
    for i in range(nr):
        a, b = r[i], r[(i + 1) % nr]
        L = float(np.hypot(*(b - a)))
        k = max(1, int(L / SOL_LAMP_EDGE_M))
        out = np.array([b[1] - a[1], a[0] - b[0]]) / max(L, 1e-9) * 0.5     # (outward of a counter-clockwise ring)
        for t in (np.arange(k) + 0.5) / k:
            p = a + (b - a) * t + out
            lamp(m, (p[0], p[1], gmin + SOL_FORT_H - 0.6), 0.4, lamp_mat)
    w = SOL_PED[3][2] / 2 + 0.4
    for t in np.linspace(-w, w, 9):
        for x, y in ((t, -w), (w, t), (-t, w), (-w, -t)):
            lamp(m, (*P(x, y), gmin + SOL_PED[3][1] - 0.5), 0.4, lamp_mat)


def buildings_area(ring):
    p = np.asarray(ring, np.float64)
    return float((p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1]).sum() / 2)


def liberty_collision(m, spec, gmin, mat):
    ring = np.asarray(spec.ring, np.float64)[:, :2]
    c = _centroid(ring)
    fy = np.array(SOL_FACING)
    P = _local(c, np.array([fy[1], -fy[0]]), fy)
    r = ring if buildings_area(ring) > 0 else ring[::-1]
    uv = (0.0, 0.0)
    for i in range(len(r)):
        a, b = r[i], r[(i + 1) % len(r)]
        m.quad((a[0], a[1], gmin - 1.0), (b[0], b[1], gmin - 1.0), (b[0], b[1], gmin + SOL_FORT_H),
               (a[0], a[1], gmin + SOL_FORT_H), uv, uv, uv, uv, mat)
    m.poly([(p[0], p[1], gmin + SOL_FORT_H) for p in r], [uv] * len(r), mat)
    _lbox(m, P, -12.5, 12.5, -12.5, 12.5, gmin + SOL_FORT_H, gmin + SOL_BASE_Z, mat)
    _lbox(m, P, -5.2, 5.2, -4.4, 4.4, gmin + SOL_BASE_Z, gmin + SOL_BASE_Z + 34.0, mat)


BUILDERS = {"eiffel": (eiffel, eiffel_collision, EIFFEL_ANTENNA_Z, EIFFEL_COLOUR, "iron"),
            "arc": (arc, arc_collision, ARC_H, ARC_COLOUR, "floodlit"),
            "bigben": (bigben, bigben_collision, BB_H, BB_COLOUR, "floodlit"),
            "colosseum": (colosseum, colosseum_collision, sum(COL_TIERS) + COL_ATTIC_H, COL_COLOUR, "stone"),
            "liberty": (liberty, liberty_collision, SOL_TORCH_Z, SOL_COPPER, "stone")}


# ------------------------------------------------------------------------------------------- the glue


def height(name):
    return BUILDERS[name][2]


def colour(name):
    return BUILDERS[name][3]


def material(name):
    """"iron", "stone" (both dark at night: lit by their lamps) or "floodlit" (buildings.LANDMARK_*)."""
    return BUILDERS[name][4]


def build(m, spec, gmin, mat, lite=False, tokens=None):
    """Its model into m in mat; tokens: its lit parts' materials, {"lamp": the Eiffel Tower's and the Colosseum's
    lamps, "dial": Big Ben's dials} (without them, all in mat)."""
    BUILDERS[spec.facade][0](m, spec, gmin, mat, lite, tokens=tokens)


def collision(m, spec, gmin, mat):
    BUILDERS[spec.facade][1](m, spec, gmin, mat)
