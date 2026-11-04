"""deckedges.py LAT LON [N]: build the road tiles within N tiles of the point (as tileprobe.py) and measure the bridge
decks' edges at every joint between two deck chunks: how far each edge jumps sideways from one chunk's end to the
next one's start (a "step" in the deck's outline; the user, 2026-10-31: bridge geometry "until they're perfect").
Prints the count of steps over 0.25 m and the worst ones with lat/lon. Only joints where the two chunks meet end to
end (within 1.5 m) and run on in the same direction (within 30 degrees) count: a fork's branch is not a step."""
import os, sys, tempfile, logging
sys.path.insert(0, "/home/tecnagamer/Documents/BeamNG.Earth/server")
logging.basicConfig(level=logging.WARNING)
import numpy as np
import blockworker as bw
import roads as roadsmod
import roadmesh
import geodesy
st = bw._State(os.path.expanduser("~/.cache/beamng-earth"), False, True, True, True, False, True, True); bw._s = st
LAT, LON = float(sys.argv[1]), float(sys.argv[2]); N = int(sys.argv[3]) if len(sys.argv) > 3 else 2
out = tempfile.mkdtemp()
import atexit, shutil
atexit.register(shutil.rmtree, out, True)
got = []
orig = roadsmod.build_tile


def grab(*a, **kw):
    r = orig(*a, **kw)
    got.append(r)
    return r
roadsmod.build_tile = grab
for tx in range(-N, N):
    for ty in range(-N, N):
        try:
            bw.roads_tile("p", LAT, LON, 15, tx, ty, out, "/x", True, True, buildings=False)
        except Exception as e:
            print("tile", tx, ty, "failed:", e)
decks = [c for chunks in got for c in chunks if c.get("deck") and not c.get("ctx") and c.get("fixed")
         and c.get("w", 0) > 2.5]
rib = [(r, c) for r, c in roadmesh.ribbon_pairs(decks) if r[5] is not None]
frame = geodesy.EnuFrame(LAT, LON)
ends = []                                   # (point, dir away from the chunk, left edge, right edge, chunk)
for (left, right, v, m, layer, z), c in rib:
    mid = (left + right) / 2
    if len(mid) < 2:
        continue
    d0 = mid[0] - mid[1]
    d1 = mid[-1] - mid[-2]
    ends.append((mid[0], d0 / max(np.hypot(*d0), 1e-9), right[0], left[0], c))      # (seen leaving: its sides swap)
    ends.append((mid[-1], d1 / max(np.hypot(*d1), 1e-9), left[-1], right[-1], c))
steps = []
# (a joint where three or more chunk ends meet: a fork or merge, its own rules (shift, pad, nose))
def busy(p):
    return sum(1 for q, *_ in ends if np.hypot(*(q - p)) <= 1.5) > 2
for i, (p, d, l, r, c) in enumerate(ends):
    if busy(p):
        continue
    for j in range(i + 1, len(ends)):
        q, e, l2, r2, c2 = ends[j]
        if c2 is c or np.hypot(*(p - q)) > 1.5:
            continue
        if float(np.dot(d, e)) > -np.cos(np.radians(30.0)):
            continue                        # (not end to end, running on)
        # seen leaving end i along d, end j's chunk runs on along -e: its left edge (leaving j) is our right edge
        lat = np.array([-d[1], d[0]])       # left of the direction away from chunk i
        jl, jr = float(np.dot(l - p, lat)), float(np.dot(r - p, lat))
        kl, kr = float(np.dot(r2 - p, lat)), float(np.dot(l2 - p, lat))
        step = max(abs(jl - kl), abs(jr - kr))
        if step > 0.25:
            la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
            steps.append((round(step, 2), round(float(la[0]), 6), round(float(lo[0]), 6), c.get("w"), c2.get("w"),
                          c.get("fid", "")[:8], c2.get("fid", "")[:8]))
steps.sort(reverse=True)
print(f"{len(decks)} deck chunks, {len(ends) // 2} with ribbons; {len(steps)} edge steps over 0.25 m")
for s in steps[:30]:
    print("  step", s[0], "m at", s[1], s[2], "widths", s[3], s[4], "parts", s[5], s[6])

# Forks (a trunk end meeting two or more ends running on: a split or a merge): the trunk's outer edges against the
# outermost edges of its branches (leftmost branch's left, rightmost's right), a notch or a lip where they differ
forks = []
for i, (p, d, l, r, c) in enumerate(ends):
    lat = np.array([-d[1], d[0]])
    jl, jr = float(np.dot(l - p, lat)), float(np.dot(r - p, lat))
    on = [(float(np.dot(r2 - p, lat)), float(np.dot(l2 - p, lat)), c2) for q, e, l2, r2, c2 in ends
          if c2 is not c and np.hypot(*(p - q)) <= 1.5 and float(np.dot(d, e)) < -np.cos(np.radians(40.0))]
    if len(on) < 2:
        continue
    kl, kr = max(o[0] for o in on), min(o[1] for o in on)
    step = max(abs(jl - kl), abs(jr - kr))
    if step > 0.25:
        la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
        forks.append((round(step, 2), round(float(la[0]), 6), round(float(lo[0]), 6), round(jl - kl, 2),
                      round(jr - kr, 2), c.get("w"), [o[2].get("w") for o in on]))
forks.sort(key=lambda f: -f[0])
print(f"{len(forks)} fork outer-edge steps over 0.25 m")
for f in forks[:20]:
    print("  fork", f[0], "m at", f[1], f[2], "left/right", f[3], f[4], "trunk w", f[5], "branches", f[6])

# The slab's own edges (roadmesh.deck: past the road's edge to a median's middle, a gore nose, an open gap): teeth where
# the slab's reach past the road jumps more than 1 m from one edge point to the next inside a chunk (the zigzag
# "black teeth" of _fill_short's note; Old Keene Mill Road in Springfield, 2026-10-31)
ctx = [(r, c) for r, c in roadmesh.ribbon_pairs([c for chunks in got for c in chunks if c.get("deck") and c.get("fixed")])
       if r[5] is not None]
med = roadmesh._median_gaps(rib, ctx)
teeth = []
for ((left, right, v, m, layer, z), c), (gl, gr, ol, orr) in zip(rib, med):
    for g, o, edge in ((gl, ol, left), (gr, orr, right)):
        ext, cov = roadmesh._opposite(g, o, *roadmesh._noses(g))
        ext = roadmesh._smooth_reach(np.maximum(ext, roadmesh.PARAPET_W), edge)
        jumps = np.flatnonzero(np.abs(np.diff(ext)) > 1.0)
        for k in jumps:
            la, lo, _ = frame.enu_to_geodetic(np.array([edge[k, 0]]), np.array([edge[k, 1]]), 0.0)
            teeth.append((round(float(abs(ext[k + 1] - ext[k])), 2), round(float(la[0]), 6), round(float(lo[0]), 6),
                          c.get("fid", "")[:8]))
teeth.sort(reverse=True)
print(f"{len(teeth)} slab edge jumps over 1 m")
for t in teeth[:20]:
    print("  jump", t[0], "m at", t[1], t[2], "part", t[3])
