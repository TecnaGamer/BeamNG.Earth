"""A building's roof shape from its LoD2 roof surfaces, as an AdV roofType code (server/lod2roofs.SHAPES).

Most LoD2 sources outside Germany (swissBUILDINGS3D, Estonia, Vienna, Poland...) have the roof faces but no roofType.
The faces say it themselves: how many roof planes there are, which way they face, how steep they are, whether the
roof's outline has sloped edges (a gable's rakes) or only level ones (eaves all round: hipped), and whether the top is
a ridge or a point. classify() reads those and answers the code the German states would have given, so the shards and
the runtime need nothing new. 5000 (mixed) is the answer whenever the shape is not one of these: only the height is
used then.

Validated blind on German tiles that carry the code (tools/build_lod2_roofs.py validate).
Pure numpy: faces may be polygons or triangles, closed or not, with or without a thickness (swissBUILDINGS3D's roofs
are slabs with overhangs: their soffits and edges are dropped).
"""
import math

import numpy as np

FLAT, SKILLION, GABLED, HIPPED, HALF_HIPPED, MANSARD, PYRAMIDAL, DOME, ROUND, MIXED = (
    1000, 2100, 3100, 3200, 3300, 3400, 3500, 3700, 3900, 5000)

FLAT_DEG = 3.0          # a face flatter than this is a flat roof's (NRW draws flat roofs at 0° and pent roofs from 2°,
                        # Bavaria flat roofs up to ~9°; 3° made the fewest errors over both; swissBUILDINGS3D's flat
                        # roofs are level: 3° or 5° changed 1 % of Zurich's answers)
VERTICAL_NZ = 0.17      # |normal z| below this: a wall-like face (a slab's edge, a sawtooth's glazing): dropped
PLANE_DEG = 4.0         # faces within this angle and PLANE_M of one plane are one roof plane
PLANE_M = 0.3
GROUP_DEG = 25.0        # planes facing within this of each other: one side of the roof
SIDE_SHARE = 0.08       # a side under this share of the sloped area (a dormer, a bay, a hip) is not a main side
MINOR_SHARE = 0.015     # a side under this share is ignored altogether
RAKE_SHARE = 0.15       # main sides whose outline is sloped less than this share: hipped (eaves all round)
SHARED_M = 0.08         # an edge whose middle lies this close to another face's edge is inside the roof
LEVEL_DEG = 6.0         # an outline edge flatter than this is an eave, steeper a rake


def _faces(rings):
    """(normal unit (3,), area 3D, plan area, points (n,3)) of each usable face. Rings of one length are done
    together (a triangulated roof: hundreds of triangles)."""
    by = {}
    for r in rings:
        p = np.asarray(r, np.float64)
        if len(p) > 3 and (p[0] == p[-1]).all():
            p = p[:-1]
        if len(p) >= 3 and p.shape[1] >= 3:
            by.setdefault(len(p), []).append(p[:, :3])
    out = []
    for ps in by.values():
        p = np.stack(ps)                                    # (k, m, 3)
        c = p - p.mean(1, keepdims=True)
        cq = np.roll(c, -1, 1)
        nv = np.stack([np.sum((c[..., 1] - cq[..., 1]) * (c[..., 2] + cq[..., 2]), 1),
                       np.sum((c[..., 2] - cq[..., 2]) * (c[..., 0] + cq[..., 0]), 1),
                       np.sum((c[..., 0] - cq[..., 0]) * (c[..., 1] + cq[..., 1]), 1)], 1)
        a2 = np.linalg.norm(nv, axis=1)
        for i in np.flatnonzero(a2 >= 0.1):                 # (< 0.05 m²: left out)
            out.append((nv[i] / a2[i], a2[i] / 2.0, abs(nv[i, 2]) / 2.0, ps[i]))
    return out


def _orient(faces):
    """Faces turned to face up. A roof's faces point up; a building whose faces mostly point down was written
    clockwise: all turned. Downward faces among upward ones are a slab's soffits: dropped. Wall-like faces: dropped."""
    up = sum(f[2] for f in faces if f[0][2] >= VERTICAL_NZ)
    down = sum(f[2] for f in faces if f[0][2] <= -VERTICAL_NZ)
    flip = down > 2.0 * up
    out = []
    for n, a, pa, p in faces:
        if flip:
            n = -n
        if n[2] >= VERTICAL_NZ:
            out.append((n, a, pa, p))
    return out


def _planes(faces):
    """Plane index of each face: faces of one plane (a triangulated roof) joined, largest first."""
    F = len(faces)
    nrm = np.array([f[0] for f in faces]).reshape(F, 3)
    area = np.array([f[1] for f in faces])
    plan = np.array([f[2] for f in faces])
    ctr = np.array([f[3].mean(0) for f in faces]).reshape(F, 3)
    acc = np.zeros((F, 3))          # each plane's area-weighted normal sum
    unit = np.zeros((F, 3))         # ... and its direction
    dist = np.zeros(F)              # its distance from the origin along the first face's normal
    pl_plan = np.zeros(F)
    k = 0
    of = [0] * F
    cos_tol = math.cos(math.radians(PLANE_DEG))
    for i in np.argsort(-area, kind="stable"):
        j = -1
        if k:
            ok = np.flatnonzero((unit[:k] @ nrm[i] >= cos_tol) & (np.abs(unit[:k] @ ctr[i] - dist[:k]) < PLANE_M))
            j = int(ok[0]) if len(ok) else -1
        if j < 0:
            j, k = k, k + 1
            dist[j] = float(nrm[i] @ ctr[i])
        acc[j] += nrm[i] * area[i]
        unit[j] = acc[j] / np.linalg.norm(acc[j])
        pl_plan[j] += plan[i]
        of[i] = j
    return of, [(unit[j].copy(), float(pl_plan[j])) for j in range(k)]


def _outline(faces):
    """[(level, sloped)] outline length in plan of each face: its edges that no other face shares (eaves: level;
    a gable's rakes, a skew eave: sloped)."""
    out = [[0.0, 0.0] for _ in faces]
    if not faces:
        return out
    s = np.concatenate([np.stack([f[3], np.roll(f[3], -1, 0)], 1) for f in faces])
    own = np.concatenate([np.full(len(f[3]), i) for i, f in enumerate(faces)])
    a, b = s[:, 0], s[:, 1]
    d = b - a
    ln2 = np.maximum(np.einsum("ij,ij->i", d, d), 1e-12)
    mid = (a + b) / 2
    plan = np.hypot(d[:, 0], d[:, 1])
    if len(s) > 6000:          # (a huge mesh: no outline, the planes alone decide)
        return out
    idx = np.flatnonzero(plan > 0.05)
    for c0 in range(0, len(idx), 256):      # (the middle of each edge against every edge, 256 at a time)
        ii = idx[c0:c0 + 256]
        rel = mid[ii][:, None, :] - a[None, :, :]                                   # (C, E, 3)
        t = np.clip(np.einsum("cej,ej->ce", rel, d) / ln2[None, :], 0.0, 1.0)
        dist = np.linalg.norm(rel - d[None, :, :] * t[:, :, None], axis=2)
        dist[own[ii][:, None] == own[None, :]] = np.inf
        free = dist.min(1) >= SHARED_M
        for i in ii[free]:
            out[own[i]][math.degrees(math.atan2(abs(d[i, 2]), plan[i])) >= LEVEL_DEG] += float(plan[i])
    return out


def _sides(planes):
    """Sides of the roof: sloped planes grouped by the way they face, [[azimuth deg, plan area, [plane idx]]], the
    largest first, and the sloped area."""
    sides = []
    for k, (n, pa) in sorted(enumerate(planes), key=lambda t: -t[1][1]):
        if _slope(n) < FLAT_DEG:
            continue
        az = math.degrees(math.atan2(n[0], n[1])) % 360
        for s in sides:
            if _adiff(s[0], az) <= GROUP_DEG:
                s[1] += pa
                s[2].append(k)
                break
        else:
            sides.append([az, pa, [k]])
    return sides, sum(s[1] for s in sides)


def _adiff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _slope(n):
    return math.degrees(math.acos(min(1.0, float(n[2]))))


def _two_slopes(f, side):
    """Does a side break into a steep lower and a shallow upper plane (a mansard / gambrel side)?"""
    planes = f["planes"]
    sl = sorted(((_slope(planes[k][0]), planes[k][1], f["plane_z"][k]) for k in side[2]), key=lambda t: t[0])
    if len(sl) < 2:
        return False
    tot = sum(a for _, a, _ in sl)
    steep = [t for t in sl if t[0] > sl[-1][0] - 8]
    shallow = [t for t in sl if t[0] < sl[0][0] + 8]
    if sl[-1][0] - sl[0][0] < 15 or sum(a for _, a, _ in steep) < 0.15 * tot or \
            sum(a for _, a, _ in shallow) < 0.15 * tot:
        return False
    # (the steep part below, the shallow one on top of it from the break up: not a lean-to or a wing at another
    # pitch, which starts lower down)
    lo_top = max(z[1] for _, _, z in steep)
    hi_bottom = min(z[0] for _, _, z in shallow)
    return min(z[0] for _, _, z in steep) < hi_bottom and abs(lo_top - hi_bottom) < 0.5


def _curved(f, side):
    """Many planes of one side at stepped slopes: a barrel."""
    return len({round(_slope(f["planes"][k][0]) / 4) for k in side[2]}) >= 3


def features(rings):
    """The numbers decide() reads (None: no usable roof face)."""
    faces = _orient(_faces(rings))
    if not faces:
        return None
    of, planes = _planes(faces)
    total = sum(pa for _, pa in planes)
    if total < 1.0:
        return None
    flat = sum(pa for n, pa in planes if _slope(n) < FLAT_DEG) / total
    sides, sloped = _sides(planes)
    pts = np.concatenate([f[3] for f in faces])
    zmax, zmin = float(pts[:, 2].max()), float(pts[:, 2].min())
    top = pts[pts[:, 2] > zmax - 0.2][:, :2]
    plane_z = [[np.inf, -np.inf] for _ in planes]
    for fc, k in zip(faces, of):
        plane_z[k] = [min(plane_z[k][0], float(fc[3][:, 2].min())), max(plane_z[k][1], float(fc[3][:, 2].max()))]
    sloped_faces = [i for i, k in enumerate(of) if _slope(planes[k][0]) >= FLAT_DEG]
    lines = _outline([faces[i] for i in sloped_faces]) if sides else []
    plane_line = [[0.0, 0.0] for _ in planes]
    for i, ln in zip(sloped_faces, lines):
        plane_line[of[i]][0] += ln[0]
        plane_line[of[i]][1] += ln[1]
    for s in sides:
        s.append(min(plane_z[k][0] for k in s[2]))                  # 3: lowest point
        s.append(max(plane_z[k][1] for k in s[2]))                  # 4: highest point
        s.append(sum(plane_line[k][0] for k in s[2]))               # 5: level outline
        s.append(sum(plane_line[k][1] for k in s[2]))               # 6: sloped outline
    return {"planes": planes, "plane_z": plane_z, "total": total, "flat": flat, "sides": sides, "sloped": sloped,
            "zmax": zmax, "zmin": zmin, "apex": float(np.max(np.ptp(top, axis=0))) if len(top) else 0.0}


def split_untyped(rings):
    """(ground, walls, roofs): a model's faces without thematic surfaces (Estonia's lod2MultiSurface) sorted by the
    way they face. Ground: level faces at the building's foot; walls: upright faces; roofs: the rest. The rings are
    handed back as they came."""
    ground, walls, roofs = [], [], []
    if not rings:
        return ground, walls, roofs
    zmin = min(float(np.min(np.asarray(r, np.float64)[:, 2])) for r in rings)
    for r in rings:
        f = _faces([r])
        if not f:
            continue
        n, _, _, p = f[0]
        if abs(n[2]) < VERTICAL_NZ:
            walls.append(r)
        elif abs(n[2]) > 0.95 and float(p[:, 2].max()) < zmin + 0.3:
            ground.append(r)
        else:
            roofs.append(r)
    return ground, walls, roofs


def classify(rings):
    """AdV roofType code of a building (part) from its roof surfaces' rings [(n, 3) points, metres, z up]; None: no
    usable roof face."""
    f = features(rings)
    return None if f is None else decide(f)


def classify_rise(rings):
    """(code, rise m): classify()'s code and the rise of that roof shape, from the eaves of its main sides to the
    top (0 for a flat roof). Not the span of all the roof faces: a building of several levels (a low wing with a flat
    roof beside a gabled block, a stepped block) would give a roof as tall as the steps (Tallinn: 16.6 m on a 22 m
    building)."""
    f = features(rings)
    if f is None:
        return None, 0.0
    code = decide(f)
    if code in (FLAT, None):
        return code, 0.0
    sides = [s for s in f["sides"] if s[1] >= SIDE_SHARE * f["sloped"]] or f["sides"]
    if not sides:
        return code, 0.0
    return code, max(0.0, max(s[4] for s in sides) - min(s[3] for s in sides))


def decide(f):
    """The AdV code of features()."""
    if f["flat"] >= 0.8:
        return FLAT
    if f["flat"] > 0.25:
        return MIXED
    allsides = [s for s in f["sides"] if s[1] >= MINOR_SHARE * f["sloped"]]
    big = [s for s in allsides if s[1] >= SIDE_SHARE * f["sloped"]]
    if not big:
        return FLAT
    if len(big) == 1:
        return SKILLION
    size = math.sqrt(f["total"])
    rise = f["zmax"] - f["zmin"]
    if len(big) >= 5:                   # a spire, a cone, a dome, or a complex roof
        az = sorted(s[0] for s in big)
        gaps = [b - a for a, b in zip(az, az[1:])] + [az[0] + 360 - az[-1]]
        slopes = [_slope(f["planes"][k][0]) for s in big for k in s[2]]
        rake = sum(s[6] for s in big) / max(1e-6, sum(s[5] + s[6] for s in big))
        if max(gaps) <= 120 and rake < 0.2:
            if f["apex"] < max(0.6, 0.08 * size) and max(slopes) - min(slopes) < 12:
                return PYRAMIDAL
            if max(slopes) - min(slopes) >= 20:
                return DOME
        return MIXED
    main = None
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            if _adiff(big[i][0], big[j][0]) >= 180 - GROUP_DEG and \
                    (main is None or big[i][1] + big[j][1] > main[0][1] + main[1][1]):
                main = (big[i], big[j])
    if main is None:
        return MIXED
    minors = [s for s in allsides if s is not main[0] and s is not main[1]]
    # (the cleaner main side: a hipped roof on a slope or with a wing has one side reaching lower, past the hips)
    main_rake = min(s[6] / max(1e-6, s[5] + s[6]) for s in main)
    if not minors:
        if all(_curved(f, s) for s in main):
            return ROUND                # (before the mansard: a barrel's sides also break into slopes)
        if any(_two_slopes(f, s) for s in main):
            return MANSARD
        return GABLED
    eave = min(s[3] for s in main)
    if main_rake < RAKE_SHARE:
        if len(big) == 4 and f["apex"] < max(0.6, 0.08 * size):
            return PYRAMIDAL
        if any(_two_slopes(f, s) for s in main):
            return MANSARD
        return HIPPED
    if any(GROUP_DEG < _adiff(s[0], main[0][0]) < 180 - GROUP_DEG and abs(_adiff(s[0], main[0][0]) - 90) > 35
           for s in minors):
        return MIXED                    # (a side at a slant: neither a hip nor a cross gable)
    if rise > 0.5 and all(s[3] - eave > 0.3 * rise and s[4] > f["zmax"] - 0.3 for s in minors):
        return HALF_HIPPED              # (small hips at the top of both gables)
    if _rake(minors) < RAKE_SHARE and all(s[3] - eave < 0.3 * rise for s in minors):
        return HIPPED                   # (hipped at one end, a gable or a party wall at the other)
    if sum(s[1] for s in minors) < SIDE_SHARE * f["sloped"]:
        return GABLED                   # (dormers)
    if all(_rake([s]) >= RAKE_SHARE for s in big):
        return GABLED                   # (wings with gables of their own: an L, T or cross)
    return MIXED


def _rake(sides):
    """The share of the sides' outline that is sloped."""
    return sum(s[6] for s in sides) / max(1e-6, sum(s[5] + s[6] for s in sides))
