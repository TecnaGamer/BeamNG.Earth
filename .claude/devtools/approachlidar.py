"""approachlidar.py LAT LON [N]: how far the drawn approaches of the bridges near a point stand off the lidar's road
surface (US 3DEP via roadz.py's own reads; the user, 2026-10-31: "use the lidar data to improve how ramps connect").
Builds the road tiles within N tiles (as rampcheck.py), takes every deck end a ground road goes on from (an abutment),
measures that road's surface along its first APPROACH_M from the lidar (roadz's deck_lidar profile on the road's own
line) and the deck's last DECK_M, and prints per abutment: the drawn deck end against the lidar's deck end, and the
drawn road (the terrain it is painted on) against the lidar's road at 10 / 30 / 60 m and at worst. Network: reads the
EPT nodes along the lines (not kept)."""
import os, sys, tempfile, logging, math
os.environ.setdefault("BNE_REVISIT_SKIP", "roads")
sys.path.insert(0, "/home/tecnagamer/Documents/BeamNG.Earth/server")
logging.basicConfig(level=logging.ERROR)
import numpy as np
import blockworker as bw
import roads as roadsmod
import roadz
import geodesy
import fast

APPROACH_M = 80.0
DECK_M = 30.0

st = bw._State(os.path.expanduser("~/.cache/beamng-earth"), False, True, True, True, False, True, True)
bw._s = st
LAT, LON = float(sys.argv[1]), float(sys.argv[2])
N = int(sys.argv[3]) if len(sys.argv) > 3 else 2
frame = geodesy.EnuFrame(LAT, LON)
out = tempfile.mkdtemp()
import atexit, shutil
atexit.register(shutil.rmtree, out, True)

tiles, grounds, ground_roads = [], [], []
orig_build = roadsmod.build_tile


def grab_build(frame_, tx, ty, cells, elev, *a, **kw):
    r = orig_build(frame_, tx, ty, cells, elev, *a, **kw)
    tiles.append(r)
    if kw.get("ground") is not None:
        grounds.append((roadsmod.tile_bbox(tx, ty), kw["ground"]))
    ground_roads.extend(kw.get("roads_out") or [])
    return r


roadsmod.build_tile = grab_build
for tx in range(-N, N):
    for ty in range(-N, N):
        try:
            bw.roads_tile("p", LAT, LON, 15, tx, ty, out, "/x", True, True, buildings=False)
        except Exception as e:
            print("tile", tx, ty, "failed:", e)
box = [min(b[0] for b, _ in grounds), min(b[1] for b, _ in grounds),
       max(b[2] for b, _ in grounds), max(b[3] for b, _ in grounds)]


def ground_z(x, y):
    for b, fn in grounds:
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            return float(np.asarray(fn(np.array([x]), np.array([y])), np.float64).ravel()[0])
    return np.nan


def latlon(p):
    la, lo, _ = frame.enu_to_geodetic(np.array([p[0]]), np.array([p[1]]), 0.0)
    return round(float(la[0]), 6), round(float(lo[0]), 6)


decks, seen = [], set()
for chunks in tiles:
    for c in chunks:
        n = np.asarray(c.get("n") or [], np.float64)
        if not (c.get("deck") and c.get("fixed") and not c.get("ctx")) or n.ndim != 2 or len(n) < 2:
            continue
        k = (c.get("fid"), len(n), round(float(n[0, 0]), 2), round(float(n[0, 1]), 2))
        if k not in seen:
            seen.add(k)
            decks.append((n, float(c.get("w", 0)), c))
lines = []
for fid, spec, pts, _ in ground_roads:
    pts = np.asarray(pts, np.float64)
    if spec is not None and len(pts) >= 2:
        k = (str(fid), len(pts), round(float(pts[0, 0]), 2))
        if k not in seen:
            seen.add(k)
            lines.append((pts[:, :2], float(spec.width), str(fid)))


def along(xy, start, length):
    """The polyline xy from its point nearest start, onward along its nearer end's far side, for length m."""
    d = np.hypot(*(xy - start).T)
    k = int(np.argmin(d))
    fwd, back = xy[k:], xy[:k + 1][::-1]
    seg = fwd if (len(fwd) >= len(back) and np.hypot(*(xy[-1] - start)) >= np.hypot(*(xy[0] - start))) else back
    seg = np.vstack([start[None], seg[1:]]) if len(seg) > 1 else seg
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(seg, axis=0).T))]
    keep = s <= length
    if keep.sum() < len(s):
        e = np.interp(length, s, np.arange(len(s)))
        i = int(e)
        p = seg[i] + (seg[min(i + 1, len(seg) - 1)] - seg[i]) * (e - i)
        seg = np.vstack([seg[keep], p[None]])
    return seg


# the abutments: deck ends with no other deck end at them, a ground road going on within 1 m of its line
ends = [(n[0], n[1], di) for di, (n, w, c) in enumerate(decks)] + [(n[-1], n[-2], di) for di, (n, w, c) in enumerate(decks)]
abut = []
for p, q, di in ends:
    if min(p[0] - box[0], box[2] - p[0], p[1] - box[1], box[3] - p[1]) < 5.0:
        continue
    if any(dj != di and np.hypot(*(p2[:2] - p[:2])) <= 1.5 for p2, _q, dj in ends):
        continue
    best = None
    for xy, w, fid in lines:
        a, b = xy[:-1], xy[1:]
        ab = b - a
        t = np.clip(((p[:2] - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
        d = np.hypot(*(p[:2] - (a + t[:, None] * ab)).T).min()
        if d < 1.0 and (best is None or d < best[0]):
            best = (d, xy, w, fid)
    if best is None:
        continue
    road = along(best[1], p[:2], APPROACH_M)
    if len(road) < 2 or np.hypot(*np.diff(road, axis=0).T).sum() < 20.0:
        continue
    n, w, c = decks[di]
    deck_line = along(n[:, :2], p[:2], DECK_M)
    abut.append((p, c.get("fid", "")[:8], w, road, best[2], deck_line))
print(f"{len(decks)} deck chunks, {len(abut)} abutments with a ground road going on")
if not abut:
    sys.exit(0)

prov = roadz.PROVIDER
names = roadz.lidar_datasets(LAT, LON)
if prov is None or not names:
    print("no lidar here:", names)
    sys.exit(0)
fast.require(roadz.NATIVE, "deck_lidar")
parts, local = [], []
for p, fid, w, road, rw, deck_line in abut:
    for xy, half in ((road, max(2.5, rw / 2)), (deck_line, max(2.5, w / 2))):
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        parts.append((np.stack([lo, la], 1), half))
        local.append((xy, half))
result = None
for name in names[:roadz.LIDAR_DATASETS]:
    ept = prov._ept_of(name)
    boxes = prov._boxes(frame, parts, local)
    b0 = boxes[len(boxes) // 2]
    depth = ept.depth_at((b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
    nodes = ept.nodes(boxes, depth)
    if not nodes:
        continue
    xyz, cls = prov._read(name, nodes, boxes)
    if not len(xyz):
        continue
    la, lo = roadz.merc_ll(xyz[:, 0], xyz[:, 1])
    gi = np.flatnonzero(cls == 2)
    if len(gi) > 4000:
        gi = gi[:: int(math.ceil(len(gi) / 4000))]
    fit = roadz.datum_fit(xyz[gi, 2], prov._dem_raw(la[gi], lo[gi])) if len(gi) else None
    if fit is None:
        continue
    scale, off = fit
    e, n_, up = frame.geodetic_to_enu(la, lo, xyz[:, 2] * scale - off)        # (into the frame: curvature too)
    pts = np.ascontiguousarray(np.stack([np.asarray(e), np.asarray(n_), np.asarray(up)], 1))
    result = (name, len(nodes), pts, np.ascontiguousarray(cls, np.uint8))
    break
if result is None:
    print("no usable lidar read:", names)
    sys.exit(0)
name, nn, pts, cls = result
print(f"dataset {name}, {nn} nodes, {len(pts)} points")
rows = []
for k, (p, fid, w, road, rw, deck_line) in enumerate(abut):
    gr = prov._profile(frame, road, max(2.5, rw / 2), pts, cls)
    dk = prov._profile(frame, deck_line, max(2.5, w / 2), pts, cls)
    if gr is None:
        continue
    s, z, sx, sy = gr[0], gr[1], gr[2], gr[3]
    drawn = np.array([ground_z(x, y) for x, y in zip(sx, sy)])
    diff = drawn - z
    ok = np.isfinite(diff)
    if ok.sum() < 3:
        continue
    at = {m: (float(np.interp(m, s[ok], diff[ok])) if s[ok][0] <= m <= s[ok][-1] else np.nan) for m in (0, 10, 30, 60)}
    dz_deck = np.nan
    if dk is not None and np.isfinite(dk[1]).any():
        okd = np.isfinite(dk[1])
        dz_deck = float(p[2] - dk[1][okd][0]) if dk[0][okd][0] <= 2 * roadz.LIDAR_STEP_M else np.nan
    zk = z[np.isfinite(z)]
    if dk is not None and np.isfinite(dk[1]).any() and len(zk) and abs(float(zk[0]) - float(dk[1][np.isfinite(dk[1])][0])) > 1.5:
        continue                                # (the lidar on the road's line found another surface: a deck over it)
    worst = float(np.abs(diff[ok]).max())
    rows.append((worst, latlon(p), fid, dz_deck, at, float(s[ok][-1])))
rows.sort(key=lambda r: -r[0])
print(f"{len(rows)} approaches measured (drawn - lidar, m; + drawn higher)")
for worst, ll, fid, dz_deck, at, reach in rows[:30]:
    print(f"  {ll[0]} {ll[1]} deck {fid} deck end {dz_deck:+.1f} | road at 0 {at[0]:+.1f} 10 {at[10]:+.1f} "
          f"30 {at[30]:+.1f} 60 {at[60]:+.1f} worst {worst:.1f} (measured {reach:.0f} m)")
w = np.array([r[0] for r in rows])
if len(w):
    print(f"worst over 1 m: {(w > 1).sum()} of {len(w)}, over 2 m: {(w > 2).sum()}; median {np.median(w):.2f} m")
