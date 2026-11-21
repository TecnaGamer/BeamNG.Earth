"""How good the graded roads' heights are: the numbers a change to grading is judged by (docs/VERTICAL_SOLVER_RFC.md,
step 1; tools/grade_metrics.py runs them on real places).

Everything here reads finished plans ([(fid, grading.Plan)], one planning region's frame) and changes nothing. Pure.

- junction steps: where a road's end meets another road (its end or its middle), how far apart their heights are;
- seam steps: where a road leaves one cell's plans and goes on in the next cell's, the same;
- grade: the steepest 10 m of each road against its class's ceiling (grading.guard_limits), and whether the plans made
  it steeper than its own ground (a real steep street is not a fault);
- bumps: the second difference per 2 m (the car's pitch change), against the ground's own;
- ground: how far the plans stand from the elevation they were made from (fills and cuts);
- clearance: under a deck, the road's distance below the deck's surface against roads.DECK_ROAD_CLEAR (under
  THROUGH_DECK_M, the road runs through the deck's body);
- corridors: roads side by side (grading.share_corridors' pairs), how much more their heights differ than the gap
  between their carriageways holds as a bank (a step across the lanes where the pavements overlap).
"""
import math

import numpy as np

import dnp

JUNCTION_R_M = 1.0      # m: a road's end this close to another road's centre line meets it there
SEAM_EDGE_M = 0.5       # m: a plan's end this close to its cell's box edge is where the cell cut it (a seam, no node)
SEAM_R_M = 1.0          # m: the next cell's piece of the same road this close to a seam end continues it
GRADE_WINDOW_M = 10.0   # m over which a grade is taken (2 m samples: a sample's noise is no grade)
MADE_STEEP_DG = 0.02    # a road steeper than its ground by more than this is made steep by the plans
STEP_BAD_M = 0.3        # m: a junction or seam step this tall is a break (grading.BREAK_STEP_M)
NEAR_M = 8.0            # m: the grid cells of the segment index (_Segments)
WORST_N = 10            # the worst places kept for each measure (to look at)
THROUGH_DECK_M = 2.5    # m: a road this little under a deck's surface meets the deck's body (they cross at one level,
                        # one of them at the wrong height), rather than passing under it with too little headroom


def _arc(xy):
    return np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]


class _Segments:
    """The segments of many polylines on a grid of NEAR_M cells: the nearest point of each line near a query point."""

    def __init__(self, lines):
        self.lines = [np.asarray(xy, np.float64)[:, :2] for xy in lines]
        grid = {}
        segs = []                       # (line, segment) per global segment number
        for i, xy in enumerate(self.lines):
            if len(xy) < 2:
                continue
            lo = np.floor(np.minimum(xy[:-1], xy[1:]) / NEAR_M).astype(np.int64)
            hi = np.floor(np.maximum(xy[:-1], xy[1:]) / NEAR_M).astype(np.int64)
            for j in range(len(xy) - 1):
                g = len(segs)
                segs.append((i, j))
                for gx in range(lo[j, 0], hi[j, 0] + 1):
                    for gy in range(lo[j, 1], hi[j, 1] + 1):
                        grid.setdefault((gx, gy), []).append(g)
        # (each cell's segments as an array of global numbers, every segment's ends and its line once: near computes a
        # query's candidates at once, the same operations as one by one: 9.9 of a Fells Point solve's 44 s were here)
        self.grid = {c: np.array(v, np.int64) for c, v in grid.items()}
        sg = np.array(segs, np.int64).reshape(-1, 2)
        self._line, self._seg = sg[:, 0], sg[:, 1]
        if len(sg):
            a = np.concatenate([xy[:-1] for xy in self.lines if len(xy) >= 2])
            b = np.concatenate([xy[1:] for xy in self.lines if len(xy) >= 2])
        else:
            a = b = np.zeros((0, 2))
        self._ax, self._ay = a[:, 0].copy(), a[:, 1].copy()
        self._dx, self._dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
        self._L2 = self._dx * self._dx + self._dy * self._dy   # (dnp.dot of a 2-vector: d0 * d0 + d1 * d1)

    def near(self, p, r):
        """{line index: (distance, segment index, fraction along it)} of the lines within r of point p, each's nearest
        (between equals the first met, the grid's cells in order; in the order the lines are first met within r)."""
        gx, gy = int(math.floor(p[0] / NEAR_M)), int(math.floor(p[1] / NEAR_M))
        k = int(math.ceil(r / NEAR_M))
        cells = [c for dx in range(-k, k + 1) for dy in range(-k, k + 1)
                 for c in (self.grid.get((gx + dx, gy + dy)),) if c is not None]
        if not cells:
            return {}
        g = cells[0] if len(cells) == 1 else np.concatenate(cells)
        px, py = float(p[0]), float(p[1])
        ax, ay, dx, dy, L2 = self._ax[g], self._ay[g], self._dx[g], self._dy[g], self._L2[g]
        num = (px - ax) * dx + (py - ay) * dy
        pos = L2 > 0
        t = np.zeros(len(g))
        t[pos] = np.minimum(1.0, np.maximum(0.0, num[pos] / L2[pos]))
        dist = dnp.hypot(ax + t * dx - px, ay + t * dy - py)
        ok = np.flatnonzero(dist <= r)
        if not len(ok):
            return {}
        line = self._line[g[ok]]
        # (per line its smallest distance, the first met between equals: sorted by line, distance, then place met)
        o = np.lexsort((ok, dist[ok], line))
        first = np.r_[True, line[o][1:] != line[o][:-1]]
        best = ok[o[first]]
        # (the lines in the order they were first met within r, as the dict filled one by one had them)
        lines_sorted = line[o]
        met = np.minimum.reduceat(ok[o], np.flatnonzero(first)) if len(o) else ok[o]
        out = {}
        for q in np.argsort(met, kind="stable").tolist():
            c = int(best[q])
            out[int(lines_sorted[np.flatnonzero(first)[q]])] = (float(dist[c]), int(self._seg[g[c]]), float(t[c]))
        return out

    def near_many(self, ps, r):
        """[near(p, r) for p in ps], the queries' candidates computed together: the same operations on each pair of a
        query and a segment, so the same answers to the bit, in the same order (a solve's joins asked near 98,000
        times one by one: 25 of Tokyo's 366 s)."""
        ps = np.asarray(ps, np.float64).reshape(-1, 2)
        nq = len(ps)
        k = int(math.ceil(r / NEAR_M))
        gq, gs = [], []
        for i in range(nq):
            gx, gy = int(math.floor(ps[i, 0] / NEAR_M)), int(math.floor(ps[i, 1] / NEAR_M))
            for dx in range(-k, k + 1):
                for dy in range(-k, k + 1):
                    c = self.grid.get((gx + dx, gy + dy))
                    if c is not None:
                        gs.append(c)
                        gq.append(np.full(len(c), i, np.int64))
        out = [{} for _ in range(nq)]
        if not gs:
            return out
        g = np.concatenate(gs)
        qi = np.concatenate(gq)
        # (each pair's place in its query's candidates, as near's ok indexes them: the first met between equals)
        start = np.r_[0, np.flatnonzero(qi[1:] != qi[:-1]) + 1]
        cnt = np.diff(np.r_[start, len(qi)])
        place = np.arange(len(qi)) - np.repeat(start, cnt)
        px, py = ps[qi, 0], ps[qi, 1]
        ax, ay, dx, dy, L2 = self._ax[g], self._ay[g], self._dx[g], self._dy[g], self._L2[g]
        num = (px - ax) * dx + (py - ay) * dy
        pos = L2 > 0
        t = np.zeros(len(g))
        t[pos] = np.minimum(1.0, np.maximum(0.0, num[pos] / L2[pos]))
        dist = dnp.hypot(ax + t * dx - px, ay + t * dy - py)
        ok = np.flatnonzero(dist <= r)
        if not len(ok):
            return out
        line = self._line[g[ok]]
        q = qi[ok]
        o = np.lexsort((place[ok], dist[ok], line, q))
        first = np.r_[True, (line[o][1:] != line[o][:-1]) | (q[o][1:] != q[o][:-1])]
        heads = np.flatnonzero(first)
        best = ok[o[heads]]
        met = np.minimum.reduceat(place[ok][o], heads)
        gl, gqq = line[o][heads], q[o][heads]
        for h in np.lexsort((met, gqq)).tolist():
            c = int(best[h])
            out[int(gqq[h])][int(gl[h])] = (float(dist[c]), int(self._seg[g[c]]), float(t[c]))
        return out

    def near_scalar(self, p, r):
        """near, one segment at a time (the reference tests/test_grademetrics compares near with)."""
        gx, gy = int(math.floor(p[0] / NEAR_M)), int(math.floor(p[1] / NEAR_M))
        k = int(math.ceil(r / NEAR_M))
        out = {}
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                for g in self.grid.get((gx + dx, gy + dy), ()).tolist() if (gx + dx, gy + dy) in self.grid else ():
                    i, j = int(self._line[g]), int(self._seg[g])
                    a, b = self.lines[i][j], self.lines[i][j + 1]
                    d = b - a
                    L2 = float(dnp.dot(d, d))
                    t = 0.0 if L2 <= 0 else min(1.0, max(0.0, float(dnp.dot(p - a, d)) / L2))
                    dist = float(dnp.hypot(*(a + t * d - p)))
                    if dist <= r and (i not in out or dist < out[i][0]):
                        out[i] = (dist, j, t)
        return out


def _z_at(z, j, t):
    return float(z[j] + t * (z[j + 1] - z[j]))


def _on_edge(p, box, tol=SEAM_EDGE_M):
    x0, y0, x1, y1 = box
    return min(abs(p[0] - x0), abs(p[0] - x1), abs(p[1] - y0), abs(p[1] - y1)) <= tol


def _worst(rows, n=WORST_N):
    return sorted(rows, key=lambda r: -r["dz"])[:n]


def junction_steps(plans, box=None):
    """[{x, y, dz, a, b}] at every place a road's end meets another road (each pair of ends once). plans: [(fid, Plan)]
    of roads only; box: the cell's own box, whose edges are seams, not nodes (None: every end is a node)."""
    xys = [np.asarray(p.xy, np.float64)[:, :2] for _f, p in plans]
    zs = [np.asarray(p.z, np.float64) for _f, p in plans]
    seg = _Segments(xys)
    out, seen = [], set()
    for i, ((fid, _p), xy, z) in enumerate(zip(plans, xys, zs)):
        if len(xy) < 2:
            continue
        for e in (0, -1):
            q = xy[e]
            if box is not None and _on_edge(q, box):
                continue
            for j, (_d, sj, t) in seg.near(q, JUNCTION_R_M).items():
                if j == i:
                    continue
                key = (min(i, j), max(i, j), round(float(q[0]), 0), round(float(q[1]), 0))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"x": float(q[0]), "y": float(q[1]), "dz": abs(float(z[e]) - _z_at(zs[j], sj, t)),
                            "a": fid, "b": plans[j][0]})
    return out


def corridor_steps(plans, specs):
    """[{x, y, dz, a, b}] per vertex of a road beside another (grading.corridor_pairs: share_corridors' pairs, two
    through roads not among them): dz how much more the two heights differ than their gap holds at
    grading.CORRIDOR_BANK, where it does, and they are within grading.CORRIDOR_MAX_DIFF_M (more: two levels). plans:
    [(fid, Plan)] of roads only, specs their Specs. [] without the kernel's pairs."""
    import grading
    pr = grading.corridor_pairs(plans, specs) if plans else None
    if pr is None or not len(pr[0]):
        return []
    j, i, k, si, t, gap = pr[:6]
    zs = [np.asarray(p.z, np.float64) for _f, p in plans]
    out = []
    for a, b, c, s, u, g in zip(j.tolist(), i.tolist(), k.tolist(), si.tolist(), t.tolist(), gap.tolist()):
        diff = abs(_z_at(zs[c], s, u) - float(zs[a][b]))
        excess = diff - max(g, 0.0) * grading.CORRIDOR_BANK
        if excess > 0.0 and diff <= grading.CORRIDOR_MAX_DIFF_M:
            q = np.asarray(plans[a][1].xy, np.float64)[b]
            out.append({"x": float(q[0]), "y": float(q[1]), "dz": float(excess), "a": plans[a][0], "b": plans[c][0]})
    return out


def seam_steps(cells):
    """[{x, y, dz, a, b}] where a road's piece ends at its cell's edge and the same road (Plan.rid) goes on in another
    cell's plans. cells: {cell key: (plans [(fid, Plan)], box)} in one frame."""
    pieces = [(k, fid, p) for k, (plans, _b) in cells.items() for fid, p in plans if len(p.xy) >= 2]
    seg = _Segments([p.xy for _k, _f, p in pieces])
    out, seen = [], set()
    for i, (k, fid, p) in enumerate(pieces):
        box = cells[k][1]
        xy = np.asarray(p.xy, np.float64)[:, :2]
        for e in (0, -1):
            if box is None or not _on_edge(xy[e], box):
                continue
            for j, (_d, sj, t) in seg.near(xy[e], SEAM_R_M).items():
                kj, _fj, pj = pieces[j]
                if kj == k or pj.rid != p.rid:
                    continue
                key = (min(i, j), max(i, j))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"x": float(xy[e][0]), "y": float(xy[e][1]),
                            "dz": abs(float(np.asarray(p.z)[e]) - _z_at(np.asarray(pj.z, np.float64), sj, t)),
                            "a": f"{k}:{fid}", "b": f"{kj}:{pieces[j][1]}"})
    return out


def _windowed_grade(s, z, w=GRADE_WINDOW_M):
    """(start index, grade) of every window of w m along (s, z): |dz| / ds from each sample to the first at least w on."""
    j = np.searchsorted(s, s + w)
    ok = j < len(s)
    i = np.flatnonzero(ok)
    j = j[ok]
    if not len(i):
        if len(s) >= 2 and s[-1] > 0:
            return np.array([0]), np.array([abs(z[-1] - z[0]) / s[-1]])
        return np.zeros(0, np.int64), np.zeros(0)
    return i, np.abs(z[j] - z[i]) / np.maximum(s[j] - s[i], 1e-6)


def _second_diff(s, z):
    """|second difference| per (2 m)^2 at each inner sample: the change of slope over 2 m, in m."""
    if len(z) < 3:
        return np.zeros(0)
    ds = np.diff(s)
    slope = np.diff(z) / np.maximum(ds, 1e-6)
    return np.abs(np.diff(slope)) * 2.0


def profiles(plans, ceilings, ground):
    """Per road: [{fid, n, grade, ceiling, over, made_steep, bump, bump_ground, dev}]. ceilings: {fid: grade ceiling} (the
    roads to look at: grading.guard_limits); ground(xy (n, 2)) -> the elevation the plans were made from there (n,)."""
    out = []
    todo = [(fid, p) for fid, p in plans if fid in ceilings and len(p.xy) >= 2]
    if not todo:
        return out
    xys = [np.asarray(p.xy, np.float64)[:, :2] for _f, p in todo]
    g_all = np.asarray(ground(np.concatenate(xys)), np.float64)
    k = 0
    for (fid, p), xy in zip(todo, xys):
        z = np.asarray(p.z, np.float64)
        g = g_all[k:k + len(xy)]
        k += len(xy)
        s = _arc(xy)
        i, gr = _windowed_grade(s, z)
        _i, gg = _windowed_grade(s, g)
        n = min(len(gr), len(gg))
        gr, gg = gr[:n], gg[:n]
        ceil = float(ceilings[fid])
        over = gr > ceil
        made = over & (gr > gg + MADE_STEEP_DG)
        d2, d2g = _second_diff(s, z), _second_diff(s, g)
        dev = z - g
        jb = int(np.argmax(d2)) + 1 if len(d2) else 0        # (the middle sample of the worst pitch change)
        jd = int(np.argmax(np.abs(dev)))
        out.append({"bump_at": {"x": float(xy[jb, 0]), "y": float(xy[jb, 1])},
                    "dev_at": {"x": float(xy[jd, 0]), "y": float(xy[jd, 1]), "dz": float(dev[jd])},"fid": fid, "n": int(len(z)), "grade": float(gr.max()) if n else 0.0, "ceiling": ceil,
                    "over": int(over.sum()), "made_steep": int(made.sum()),
                    "made_steep_at": [float(v) for v in xy[i[:n][made][:1]].ravel()] if made.any() else None,
                    "bump": float(d2.max()) if len(d2) else 0.0, "bump_p99": float(np.percentile(d2, 99)) if len(d2) else 0.0,
                    "bump_ground_p99": float(np.percentile(d2g, 99)) if len(d2g) else 0.0,
                    "dev_abs": np.abs(dev), "dev": dev})
    return out


def clearance(plans, decks, names):
    """[{x, y, dz, a, b}] of the road samples under a deck closer to its surface than the deck's clearance
    (roads.deck_road_clear of its depth: the headroom, its margin and its slab and girders, which the deck was solved
    with; roads.DECK_ROAD_CLEAR, a long span's, when the deck gives none): dz the shortfall (m). plans: [(fid, Plan)];
    decks: [(fid, xy (k, 2), z (k,), half width[, depth])]; names: the roads to look at. (Lagos' thin slabs, 1.4 m,
    cleared their roads by 6.4 m and counted as low against a long span's 2.2 m depth, 2026-11-17.)"""
    import roads as roadsmod
    if not decks:
        return []
    seg = _Segments([d[1] for d in decks])
    reach = max(float(d[3]) for d in decks)
    boxes = np.array([(*(np.asarray(d[1], np.float64)[:, :2].min(0) - d[3]),
                       *(np.asarray(d[1], np.float64)[:, :2].max(0) + d[3])) for d in decks])
    out = []
    for fid, p in plans:
        if fid not in names:
            continue
        xy = np.asarray(p.xy, np.float64)[:, :2]
        z = np.asarray(p.z, np.float64)
        under = np.zeros(len(xy), bool)     # (the samples in some deck's box: the others are under none)
        for b in boxes:
            under |= (xy[:, 0] >= b[0]) & (xy[:, 0] <= b[2]) & (xy[:, 1] >= b[1]) & (xy[:, 1] <= b[3])
        worst = {}
        for q, zq in zip(xy[under], z[under]):
            for j, (dist, sj, t) in seg.near(q, reach).items():
                dfid, dxy, dz, half = decks[j][:4]
                need = float(roadsmod.deck_road_clear(decks[j][4]) if len(decks[j]) > 4 and decks[j][4] is not None
                             else roadsmod.DECK_ROAD_CLEAR)
                if dist > half:
                    continue
                gap = _z_at(np.asarray(dz, np.float64), sj, t) - float(zq)
                # (a deck below the road or level with it is the road's own bridge or one it runs onto, not over it)
                if gap < 1.0 or gap >= need:
                    continue
                short = need - gap
                if j not in worst or short > worst[j]["dz"]:
                    cum = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(np.asarray(dxy, np.float64)[:, :2], axis=0).T))]
                    at = cum[sj] + t * (cum[sj + 1] - cum[sj])
                    worst[j] = {"x": float(q[0]), "y": float(q[1]), "dz": short, "gap": gap, "a": fid, "b": dfid,
                                "from_end": float(min(at, cum[-1] - at)), "deck_len": float(cum[-1])}
        out.extend(worst.values())
    return out


def summary(junctions, seams, profs, clear, cpu_s=None, corridors=None):
    """One place's numbers (the JSON tools/grade_metrics.py writes)."""
    def steps(rows):
        dz = np.array([r["dz"] for r in rows]) if rows else np.zeros(0)
        return {"n": int(len(dz)), "bad": int((dz >= STEP_BAD_M).sum()), "over_10cm": int((dz >= 0.1).sum()),
                "p95": float(np.percentile(dz, 95)) if len(dz) else 0.0, "max": float(dz.max()) if len(dz) else 0.0,
                "worst": _worst(rows)}
    dev = np.concatenate([p["dev_abs"] for p in profs]) if profs else np.zeros(0)
    sdev = np.concatenate([p["dev"] for p in profs]) if profs else np.zeros(0)
    bumps = np.array([p["bump_p99"] for p in profs]) if profs else np.zeros(0)
    bumps_g = np.array([p["bump_ground_p99"] for p in profs]) if profs else np.zeros(0)
    made = [p for p in profs if p["made_steep"]]
    import roads as roadsmod
    need = float(roadsmod.DECK_ROAD_CLEAR)
    return {
        "roads": len(profs), "samples": int(sum(p["n"] for p in profs)),
        "junction": steps(junctions), "seam": steps(seams),
        "grade": {"over_ceiling": int(sum(1 for p in profs if p["over"])),
                  "made_steep": len(made),
                  "worst": [{"fid": p["fid"], "grade": p["grade"], "ceiling": p["ceiling"], "at": p["made_steep_at"]}
                            for p in sorted(made, key=lambda p: -(p["grade"] - p["ceiling"]))[:WORST_N]]},
        "bump": {"p99_median": float(np.median(bumps)) if len(bumps) else 0.0,
                 "ground_p99_median": float(np.median(bumps_g)) if len(bumps_g) else 0.0,
                 "max": float(max((p["bump"] for p in profs), default=0.0)),
                 "worst": [{"fid": p["fid"], "bump": p["bump"], **p["bump_at"]}
                           for p in sorted(profs, key=lambda p: -p["bump"])[:WORST_N] if "bump_at" in p]},
        "ground": {"median": float(np.median(dev)) if len(dev) else 0.0,
                   "p95": float(np.percentile(dev, 95)) if len(dev) else 0.0,
                   "max": float(dev.max()) if len(dev) else 0.0,
                   "fill_share": float((sdev > 0.3).mean()) if len(sdev) else 0.0,
                   "cut_share": float((sdev < -0.3).mean()) if len(sdev) else 0.0,
                   "worst": [{"fid": p["fid"], **p["dev_at"]}
                             for p in sorted(profs, key=lambda p: -abs(p["dev_at"]["dz"]))[:WORST_N] if "dev_at" in p]},
        "clearance": {**steps(clear), "through_deck": int(sum(1 for r in clear if r.get("gap", need) < THROUGH_DECK_M))},
        "corridor": steps(corridors or []),
        "cpu_s": cpu_s,
    }
