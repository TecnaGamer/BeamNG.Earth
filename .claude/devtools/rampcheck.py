"""rampcheck.py LAT LON [N]: build the road tiles within N tiles of the point (as tileprobe.py; the revisit cache
skipped) and look for broken connections between bridge decks and the roads they meet, as a driver finds them (the
user, 2026-10-31, two in a row by I-395 in Baltimore: "make tools that can find issues like this"):

  hanging   a deck's end in the air: no deck goes on from it, it is on no other deck, no road goes on from it, and it
            stands over the ground (a ramp ending square at a drop)
  step      a deck's end at another height than the road or deck going on from it (a ramp held at its road's height
            to its abutment, 4.9 m over the ground road: MLK Boulevard)
  barrier   a deck's parapet across a road's line at its height (a ramp joining a deck through its railing)
  headroom  a deck's end under another deck's slab, too near under it to drive (HEADROOM_M)

Prints a count per kind and the worst of each with lat/lon (camshot.py / ortho_hi.py take them as they are).
Measures what the tiles draw: the deck chunks (their heights), the road chunks, roadmesh.parapet_runs."""
import os, sys, tempfile, logging
os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
sys.path.insert(0, "/home/tecnagamer/Documents/BeamNG.Earth/server")
logging.basicConfig(level=logging.ERROR)
import numpy as np
import blockworker as bw
import roads as roadsmod
import roadmesh
import geodesy

STEP_M = 0.5            # m: a deck end this far off the height of the road / deck going on from it
HANG_M = 1.5            # m: a free deck end this far over the ground
JOIN_M = 1.5            # m: ends this near in plan meet
BARRIER_DZ = 1.2        # m: a parapet within this of a road's height across its line blocks it
HEADROOM_M = 4.5        # m: a deck's end under another deck's slab this near over it

st = bw._State(os.path.expanduser("~/.cache/beamng-earth"), False, True, True, True, False, True, True)
bw._s = st
LAT, LON = float(sys.argv[1]), float(sys.argv[2])
N = int(sys.argv[3]) if len(sys.argv) > 3 else 2
frame = geodesy.EnuFrame(LAT, LON)
out = tempfile.mkdtemp()
import atexit, shutil
atexit.register(shutil.rmtree, out, True)

tiles, runs_all, grounds, ground_roads = [], [], [], []
orig_build, orig_runs = roadsmod.build_tile, roadmesh.parapet_runs


def grab_build(frame_, tx, ty, cells, elev, *a, **kw):
    r = orig_build(frame_, tx, ty, cells, elev, *a, **kw)
    tiles.append(r)
    if kw.get("ground") is not None:
        grounds.append((roadsmod.tile_bbox(tx, ty), kw["ground"]))
    ground_roads.extend(kw.get("roads_out") or [])
    return r


def grab_runs(*a, **kw):
    r = orig_runs(*a, **kw)
    runs_all.extend(r)
    return r


roadsmod.build_tile, roadmesh.parapet_runs = grab_build, grab_runs
for tx in range(-N, N):
    for ty in range(-N, N):
        try:
            bw.roads_tile("p", LAT, LON, 15, tx, ty, out, "/x", True, True, buildings=False)
        except Exception as e:
            print("tile", tx, ty, "failed:", e)
box = [min(b[0] for b, _ in grounds), min(b[1] for b, _ in grounds),
       max(b[2] for b, _ in grounds), max(b[3] for b, _ in grounds)] if grounds else None


def latlon(p):
    la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
    return round(float(la[0]), 6), round(float(lo[0]), 6)


def ground_z(p):
    """The terrain's drawn height at p (the ground roads' too: they are painted on it)."""
    for b, fn in grounds:
        if b[0] <= p[0] <= b[2] and b[1] <= p[1] <= b[3]:
            return float(np.asarray(fn(np.array([p[0]]), np.array([p[1]])), np.float64).ravel()[0])
    return np.nan


def at_rim(p):
    """Near the built area's edge (a deck cut there goes on in the tiles not built)."""
    return box is None or min(p[0] - box[0], box[2] - p[0], p[1] - box[1], box[3] - p[1]) < 3.0


def pts_of(c):
    n = np.asarray(c.get("n") or [], np.float64)
    return n if n.ndim == 2 and n.shape[1] >= 3 and len(n) >= 2 else None


seen, decks, roads_ = set(), [], []
for chunks in tiles:
    for c in chunks:
        n = pts_of(c)
        if n is None:
            continue
        k = (c.get("fid"), c.get("m"), len(n), round(float(n[0, 0]), 2), round(float(n[0, 1]), 2))
        if k in seen:
            continue
        seen.add(k)
        if c.get("deck") and c.get("fixed") and not c.get("ctx"):
            decks.append((n, float(c.get("w", 0)), c))
for fid, spec, pts, _ in ground_roads:      # (the ground carriageways: painted on the terrain, at its height)
    pts = np.asarray(pts, np.float64)
    if spec is None or len(pts) < 2:
        continue
    k = (str(fid), len(pts), round(float(pts[0, 0]), 2), round(float(pts[0, 1]), 2))
    if k in seen:
        continue
    seen.add(k)
    z = np.array([ground_z(q) for q in pts[:, :2]])
    if np.isfinite(z).all():
        roads_.append((np.c_[pts[:, :2], z], float(spec.width), {"fid": str(fid)}))


def near_line(p, n):
    """(plan distance, height there, t along) of point p to polyline n (k, 3)."""
    a, b = n[:-1, :2], n[1:, :2]
    ab = b - a
    t = np.clip(((p[:2] - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    q = a + t[:, None] * ab
    d = np.hypot(*(p[:2] - q).T)
    k = int(np.argmin(d))
    return float(d[k]), float(n[k, 2] + t[k] * (n[k + 1, 2] - n[k, 2])), k


found = {"hanging": [], "step": [], "barrier": [], "headroom": []}
ends, dirs = [], []
for di, (n, w, c) in enumerate(decks):
    for p, q in ((n[0], n[1]), (n[-1], n[-2])):
        v = p[:2] - q[:2]
        ends.append((p, di))
        dirs.append(v / max(float(np.hypot(*v)), 1e-9))   # (away from its deck)
for ei, (p, di) in enumerate(ends):
    if at_rim(p):
        continue
    w = decks[di][1]
    # another deck's end here: a joint (its height the same). One going on straight from it is the same road at
    # whatever height difference (stacked levels cross, they do not meet end to end: ec06ac3c / ffb70ed8 by I-395,
    # 6.9 m apart)
    others = [(q, dj, ej) for ej, (q, dj) in enumerate(ends) if dj != di and np.hypot(*(q[:2] - p[:2])) <= JOIN_M]
    if others:
        on_ = [abs(q[2] - p[2]) for q, dj, ej in others if float(np.dot(dirs[ei], dirs[ej])) < -0.7]
        dz = min(on_) if on_ else min(abs(q[2] - p[2]) for q, _, _ in others)
        if STEP_M < dz and (on_ or dz < 4.0):
            found["step"].append((round(dz, 2), latlon(p), "deck joint", decks[di][2].get("fid", "")[:8]))
        continue
    fid_ = decks[di][2].get("fid", "")[:8]
    # another deck right over it, too low to drive under (a deck end in another's slab)
    for dj, (m, wm, cm) in enumerate(decks):
        if dj != di and cm.get("fid") != decks[di][2].get("fid"):
            d, z, _ = near_line(p, m)
            if d <= wm / 2 - 0.5 and 0.5 < z - p[2] < HEADROOM_M:
                found["headroom"].append((round(z - p[2], 2), latlon(p), f"under deck {cm.get('fid', '')[:8]}", fid_))
    # on another deck's surface at about its height (a ramp ending on its road's deck)
    on = None
    for dj, (m, wm, cm) in enumerate(decks):
        if dj == di:
            continue
        d, z, _ = near_line(p, m)
        if d <= wm / 2 + 0.5 and abs(z - p[2]) < 4.0:
            on = abs(z - p[2]) if on is None else min(on, abs(z - p[2]))
    # a road going on from it
    best = None
    for m, wm, cm in roads_:
        d, z, _ = near_line(p, m)
        if d <= max(wm / 2, 2.0) + 1.0:
            dz = abs(z - p[2])
            best = dz if best is None else min(best, dz)
    if best is not None and best <= STEP_M:
        continue                                # (its abutment, the road going on at its height)
    if on is not None and on <= STEP_M:
        continue                                # (on its road's deck)
    if on is not None:
        found["step"].append((round(on, 2), latlon(p), "end on a deck", fid_))
        continue
    if best is not None:
        found["step"].append((round(best, 2), latlon(p), "end to a road", fid_))
        continue
    g = ground_z(p)
    if np.isfinite(g) and p[2] - g > HANG_M:
        found["hanging"].append((round(float(p[2] - g), 2), latlon(p), "nothing goes on", decks[di][2].get("fid", "")[:8]))


def crossings(a, b):
    """(i, j, s, t) where polyline a's segment i crosses b's segment j at fractions s, t."""
    out = []
    lo_a, hi_a = a[:, :2].min(0), a[:, :2].max(0)
    lo_b, hi_b = b[:, :2].min(0), b[:, :2].max(0)
    if (lo_a > hi_b).any() or (lo_b > hi_a).any():
        return out
    for i in range(len(a) - 1):
        p, r = a[i, :2], a[i + 1, :2] - a[i, :2]
        for j in range(len(b) - 1):
            q, s_ = b[j, :2], b[j + 1, :2] - b[j, :2]
            den = r[0] * s_[1] - r[1] * s_[0]
            if abs(den) < 1e-12:
                continue
            qp = q - p
            t = (qp[0] * s_[1] - qp[1] * s_[0]) / den
            u = (qp[0] * r[1] - qp[1] * r[0]) / den
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                out.append((i, j, t, u))
    return out


lines = [(n, "road", c) for n, w, c in roads_] + [(n, "deck", c) for n, w, c in decks]
for r in runs_all:
    e = np.asarray(r.get("edge"), np.float64)
    z = np.asarray(r.get("z"), np.float64)
    if e.ndim != 2 or len(e) < 2:
        continue
    run = np.c_[e[:, :2], z[:len(e)]]
    for n, kind, c in lines:
        for i, j, t, u in crossings(run, n):
            zr = run[i, 2] + t * (run[i + 1, 2] - run[i, 2])
            zl = n[j, 2] + u * (n[j + 1, 2] - n[j, 2])
            if abs(zr - zl) < BARRIER_DZ:
                p = run[i, :2] + t * (run[i + 1, :2] - run[i, :2])
                own = min(((near_line(np.r_[p, zr], m)[0], cm.get("fid", "")[:8], wm) for m, wm, cm in decks),
                          default=(0, "?", 0))
                g = ground_z(p)
                found["barrier"].append((round(abs(zr - zl), 2), latlon(p),
                                         f"across a {kind}; deck {own[1]} w {own[2]}, {zr - g:.1f} m over the ground",
                                         c.get("fid", "")[:8]))

# (one finding per place: the same joint seen from both tiles, or both ends of a crossing pair)
for k, v in found.items():
    seen_p, keep = [], []
    for f in sorted(v, key=lambda f: -f[0] if k != "barrier" else f[0]):
        if any(abs(f[1][0] - a) < 2e-5 and abs(f[1][1] - b) < 2e-5 for a, b in seen_p):
            continue
        seen_p.append(f[1])
        keep.append(f)
    found[k] = keep
print(f"{len(decks)} deck chunks, {len(roads_)} road chunks, {len(runs_all)} parapet runs")
for k in ("hanging", "step", "barrier", "headroom"):
    print(f"{len(found[k])} {k}")
    for f in found[k][:15]:
        print(f"  {k} {f[0]} m at {f[1][0]} {f[1][1]} ({f[2]}, part {f[3]})")

if os.environ.get("RC_NEAR"):                # RC_NEAR="lat,lon,r": every deck end near a spot and how it was read
    la0, lo0, r0 = map(float, os.environ["RC_NEAR"].split(","))
    e0, n0, _ = frame.geodetic_to_enu(np.array([la0]), np.array([lo0]), 0.0)
    c0 = np.array([float(e0[0]), float(n0[0])])
    for p, di in ends:
        if np.hypot(*(p[:2] - c0)) > r0:
            continue
        m_, w_, c_ = decks[di]
        others = [(round(float(q[2] - p[2]), 2), decks[dj][2].get("fid", "")[:8]) for q, dj in ends
                  if dj != di and np.hypot(*(q[:2] - p[:2])) <= JOIN_M]
        ons = []
        for dj, (m, wm, cm) in enumerate(decks):
            if dj != di:
                d, z, _ = near_line(p, m)
                if d <= wm / 2 + 0.5:
                    ons.append((cm.get("fid", "")[:8], round(z - p[2], 2)))
        rds = []
        for m, wm, cm in roads_:
            d, z, _ = near_line(p, m)
            if d <= max(wm / 2, 2.0) + 1.0:
                rds.append((cm.get("fid", "")[:8], round(z - p[2], 2)))
        print("end", c_.get("fid", "")[:8], "w", w_, latlon(p), "z", round(float(p[2]), 2), "ground", round(ground_z(p), 2),
              "rim", at_rim(p), "joins", others, "on", ons[:4], "roads", rds[:4])

if os.environ.get("RC_FID"):                 # RC_FID=prefix: every deck chunk of that road: its ends and heights
    for n, w, c in decks:
        if str(c.get("fid", "")).startswith(os.environ["RC_FID"]):
            L = float(np.hypot(*np.diff(n[:, :2], axis=0).T).sum())
            print("chunk", c.get("fid", "")[:8], "w", w, "n", len(n), "L", round(L), latlon(n[0]), "->", latlon(n[-1]),
                  "z", np.round(n[::max(1, len(n) // 8), 2], 1).tolist(), "last", round(float(n[-1, 2]), 1))
