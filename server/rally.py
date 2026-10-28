"""Rally stages on real roads (the user, 2026-10-26: "find a real rally road, and set up a rally race for it" -> a real
US stage, timed with pacenotes, a loaned rally car, a Rally phone app).

The stages are the roads of the Susquehannock Trail Performance Rally (STPR, Wellsboro PA: 2023 regulations: Asaph,
Painter... in the Tioga State Forest). The rally's exact stage start / finish points are not public, so a stage here is
the named forest road itself, found in the OpenStreetMap road data around the place: from the point on it nearest the
place, along the named road(s) as far as it goes up to about the real stage's length.

Pacenotes from the road's shape: each corner graded by its tightest radius on the usual 6 (nearly flat) to 1
(slowest) scale, hairpins and squares named, long / tightens / opens added, and the distance to the next corner
("100", "into"). stage_path and pacenotes are pure (tests/test_rally.py).
"""
import heapq
import math

import numpy as np

import mapcells
import roads as roadsmod

M_LAT = 111320.0
MI = 1609.344

STAGES = [
    {"id": "asaph", "name": "Asaph", "event": "STPR", "area": "Tioga State Forest, PA", "lat": 41.7775,
     "lon": -77.4229, "names": ["asaph"], "miles": 8.71, "surface": "gravel"},
    {"id": "painter", "name": "Painter", "event": "STPR", "area": "Tioga State Forest, PA", "lat": 41.59972,
     "lon": -77.49528, "names": ["painter", "leetonia"], "miles": 3.91, "surface": "gravel"},
]
SKIP_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link"}
MIN_STAGE_M = 1000.0
JOIN_M = 3.0                # piece ends this close are one junction (cells cut roads at their edges)
STEP_M = 5.0                # the pacenote resampling
CORNER_R = 250.0            # tighter than this is a corner
MIN_ANGLE = 12.0            # degrees: a smaller bend is no call
MERGE_GAP_M = 15.0
GRADES = [(250, 6), (150, 5), (100, 4), (70, 3), (45, 2), (25, 1)]   # radius above -> grade; below 25: 1 / hairpin


def stage(sid):
    for s in STAGES:
        if s["id"] == sid:
            return s
    return None


def _xy(lat, lon, lat0, lon0):
    k = math.cos(math.radians(lat0))
    return (lon - lon0) * M_LAT * k, (lat - lat0) * M_LAT


def _latlon(x, y, lat0, lon0):
    k = math.cos(math.radians(lat0))
    return lat0 + y / M_LAT, lon0 + x / (M_LAT * k)


def stage_path(pieces, target_m, start=(0.0, 0.0)):
    """pieces: [[(x, y)...]...] (metres) of the stage's road(s). The path [(x, y)...] from the point nearest start along
    the joined pieces to the farthest point not beyond about target_m (Dijkstra), or []. Pure."""
    keys, pts, adj = {}, [], []

    def node(p):
        gx, gy = int(round(p[0] / JOIN_M)), int(round(p[1] / JOIN_M))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in keys.get((gx + dx, gy + dy), ()):
                    if (pts[i][0] - p[0]) ** 2 + (pts[i][1] - p[1]) ** 2 <= JOIN_M ** 2:
                        return i
        i = len(pts)
        pts.append((float(p[0]), float(p[1])))
        adj.append([])
        keys.setdefault((gx, gy), []).append(i)
        return i

    for pc in pieces:
        prev = None
        for p in pc:
            i = node(p)
            if prev is not None and prev != i:
                d = math.hypot(pts[i][0] - pts[prev][0], pts[i][1] - pts[prev][1])
                adj[prev].append((i, d))
                adj[i].append((prev, d))
            prev = i
    if not pts:
        return []
    s0 = min(range(len(pts)), key=lambda i: (pts[i][0] - start[0]) ** 2 + (pts[i][1] - start[1]) ** 2)
    dist, back = {s0: 0.0}, {s0: None}
    heap = [(0.0, s0)]
    while heap:
        d, i = heapq.heappop(heap)
        if d > dist.get(i, math.inf):
            continue
        for j, w in adj[i]:
            nd = d + w
            if nd < dist.get(j, math.inf):
                dist[j], back[j] = nd, i
                heapq.heappush(heap, (nd, j))
    cap = target_m * 1.15
    end = max((i for i in dist if dist[i] <= cap), key=lambda i: dist[i])
    out, i = [], end
    while i is not None:
        out.append(pts[i])
        i = back[i]
    return out[::-1]


def length(path):
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])))


def _resample(path, step=STEP_M):
    p = np.asarray(path, np.float64)
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
    if s[-1] < 2 * step:
        return p, s
    t = np.arange(0.0, s[-1], step)
    return np.stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])], axis=1), t


def _grade(r):
    for lim, g in GRADES:
        if r >= lim:
            return g
    return 1


def _gap_call(gap):
    if gap < 30:
        return "into"
    if gap < 60:
        return "and"
    if gap > 500:
        return "long straight"
    return str(int(round(gap / 50.0)) * 50)


def pacenotes(path):
    """[{"at" (m along), "end", "dir" ("left" / "right"), "grade" (1..6, 0 hairpin), "text"}...]. Pure."""
    p, s = _resample(path)
    if len(p) < 4:
        return []
    d = np.diff(p, axis=0)
    h = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    turn = np.diff(h)                                       # per STEP_M, + left (x east, y north)
    turn = np.convolve(turn, np.ones(3) / 3.0, mode="same")
    k = turn / STEP_M                                       # curvature
    sign = np.where(np.abs(k) > 1.0 / CORNER_R, np.sign(k), 0)
    groups, i = [], 0
    while i < len(sign):
        if sign[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < len(sign) and sign[j + 1] == sign[i]:
            j += 1
        if groups and groups[-1][2] == sign[i] and (i - groups[-1][1]) * STEP_M <= MERGE_GAP_M:
            groups[-1][1] = j
        else:
            groups.append([i, j, sign[i]])
        i = j + 1
    notes = []
    for a, b, sg in groups:
        seg = turn[a:b + 1]
        ang = abs(math.degrees(float(np.sum(seg))))
        if ang < MIN_ANGLE:
            continue
        kk = np.abs(k[a:b + 1])
        r = 1.0 / max(float(np.max(np.convolve(kk, np.ones(3) / 3.0, mode="same"))), 1e-6)
        ln = (b - a + 1) * STEP_M
        dirn = "left" if sg > 0 else "right"
        if r < 25 and ang >= 120:
            grade, name = 0, "hairpin"
        elif r < 30 and 70 <= ang <= 110 and ln <= 30:
            grade, name = 1, "square"
        else:
            grade = _grade(r)
            name = str(grade)
        long_ = bool(ln > 80 and grade)
        words = (["long"] if long_ else []) + [dirn, name]
        mod = None
        if ln >= 30 and b - a >= 3:
            half = (a + b) // 2
            r1 = 1.0 / max(float(np.max(np.abs(k[a:half + 1]))), 1e-6)
            r2 = 1.0 / max(float(np.max(np.abs(k[half:b + 1]))), 1e-6)
            if r2 < 0.7 * r1:
                mod = "tightens"
            elif r1 < 0.7 * r2:
                mod = "opens"
        if mod:
            words.append(mod)
        # (the parts as data too, for the game's co-driver voice: mod/beamng_earth/.../rally.lua clips())
        notes.append({"at": round(float(s[a]), 1), "end": round(float(s[min(b + 1, len(s) - 1)]), 1), "dir": dirn,
                      "grade": grade, "name": name, "long": long_, "mod": mod, "link": None,
                      "text": " ".join(words)})
    for n, nx in zip(notes, notes[1:]):
        gap = nx["at"] - n["end"]
        call = _gap_call(gap)
        n["text"] += " " + call
        n["link"] = call if call in ("into", "and") else (int(call) if call.isdigit() else None)
    if notes:
        notes[-1]["text"] += ", to finish"
    for n in notes:
        n["text"] = n["text"][0].upper() + n["text"][1:]
    return notes


def pieces_near(md, st, radius_m):
    """The named stage road(s) near the stage's place, as [[(x, y)...]...] metres around it."""
    lat0, lon0 = st["lat"], st["lon"]
    k = math.cos(math.radians(lat0))
    dlat, dlon = radius_m / M_LAT, radius_m / (M_LAT * max(k, 0.05))
    want = [n.lower() for n in st["names"]]
    out = []
    for key in mapcells.cells_for_bbox(lat0 - dlat, lon0 - dlon, lat0 + dlat, lon0 + dlon):
        f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if f is None or not len(f):
            continue
        names, cls = f.attrs.get("name"), f.attrs.get("class")
        if names is None:
            continue
        for i in range(len(f)):
            nm = str(names[i] or "").lower()
            if not nm or not any(w in nm for w in want):
                continue
            if cls is not None and str(cls[i]) in SKIP_CLASSES:
                continue
            for ring in roadsmod.ground_parts(f, i):
                ring = np.asarray(ring, np.float64)
                if len(ring) >= 2:
                    out.append([_xy(float(la), float(lo), lat0, lon0) for lo, la in ring[:, :2]])
    return out


def build(md, sid):
    """{"id", "name", "area", "points": [[lat, lon]...], "dist", "notes"} for stage sid, or raises ValueError."""
    st = stage(sid)
    if st is None:
        raise ValueError("no such stage")
    target = st["miles"] * MI
    radius = min(max(target * 0.9, 3000.0), 12000.0)
    path = stage_path(pieces_near(md, st, radius), target)
    dist = length(path)
    if dist < MIN_STAGE_M:
        raise ValueError("the stage road was not found in the map data")
    notes = pacenotes(path)
    pts = [[round(a, 6), round(b, 6)] for a, b in (_latlon(x, y, st["lat"], st["lon"]) for x, y in path)]
    return {"id": st["id"], "name": st["name"], "area": st["area"], "event": st["event"], "points": pts,
            "dist": round(dist, 1), "notes": notes}
