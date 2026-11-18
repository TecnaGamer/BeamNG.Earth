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
  THROUGH_DECK_M, the road runs through the deck's body).
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
        self.grid = {}
        for i, xy in enumerate(self.lines):
            if len(xy) < 2:
                continue
            lo = np.floor(np.minimum(xy[:-1], xy[1:]) / NEAR_M).astype(np.int64)
            hi = np.floor(np.maximum(xy[:-1], xy[1:]) / NEAR_M).astype(np.int64)
            for j in range(len(xy) - 1):
                for gx in range(lo[j, 0], hi[j, 0] + 1):
                    for gy in range(lo[j, 1], hi[j, 1] + 1):
                        self.grid.setdefault((gx, gy), []).append((i, j))

    def near(self, p, r):
        """{line index: (distance, segment index, fraction along it)} of the lines within r of point p, each's nearest."""
        gx, gy = int(math.floor(p[0] / NEAR_M)), int(math.floor(p[1] / NEAR_M))
        k = int(math.ceil(r / NEAR_M))
        out = {}
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                for i, j in self.grid.get((gx + dx, gy + dy), ()):
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
    """[{x, y, dz, a, b}] of the road samples under a deck closer to its surface than roads.DECK_ROAD_CLEAR: dz the
    shortfall (m). plans: [(fid, Plan)]; decks: [(fid, xy (k, 2), z (k,), half width)]; names: the roads to look at."""
    import roads as roadsmod
    need = float(roadsmod.DECK_ROAD_CLEAR)
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
                dfid, dxy, dz, half = decks[j]
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


def summary(junctions, seams, profs, clear, cpu_s=None):
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
        "cpu_s": cpu_s,
    }
