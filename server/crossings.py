"""Grade separations solved as one height problem (GitHub proposal "solve overpasses and interchanges as one height
problem", stage 1: a single-span bridge over the roads crossing under it). Off by default: BNE_INTERCHANGE=1.

Today each rule moves one road: the road under a never-raised overpass dips (grading._lower_underpasses, #126), the
roads onto a raisable one rise (_lift_approaches, never a motorway's or trunk's: APPROACH_SKIP), and #504 keeps a road
on lidar at its own ground when the deck ends stand under AT_GRADE_M over it. They read the elevation where it is least
to be trusted: lidar fills the gap under a bridge at about the deck's height (Wezep's A28: 6.3 m of "ground" under the
deck, the street 3.4-3.8 m either side), a bridge's mapped end often lies on its embankment's slope, and an old DEM
predates the structure.

Here the bridge and every road under it are decided together:

- the trusted ground: the road under, read beside the bridge's embankment on either side (not under the deck), and
  the road over, read along it past each abutment (not at the abutment's own point, which may be on the slope);
- the separation each crossing needs (roads.deck_road_clear for the span's depth, + the road's ROAD_LIFT);
- the cheapest way to make it: raise the bridge's ends (one lift, its ramps) or lower each road under (its dip),
  weighed by class (UP_COST / DOWN_COST per metre), by how tied the road under is to its street grid (TIE_COST per
  junction within TIE_M of the crossing) and by low ground (LOW_COST under LOW_M above the sea: a dug road under the
  water table is pumped; the Netherlands' motorways go over their streets, not under).

The result is a pure function of the map and the elevation around the bridge, so every cell (and both the approach
ramps and the underpass dips) gets the same answer. solve() is the arithmetic, unit-tested on its own.
"""
import math
import os

import numpy as np

ON = os.environ.get("BNE_INTERCHANGE", "0").lower() in ("1", "on", "yes", "true")

UNDER_GAP_M = 20.0      # m past the bridge's half width (its embankment's foot) the road under is first read ...
UNDER_RUN_M = 60.0      # ... and over this much further, on each side (lidar's fill under Wezep's A28 deck ran
                        # 35-45 m either side of it)
OVER_GAP_M = 8.0        # m past an abutment the road over is first read ...
OVER_RUN_M = 40.0       # ... and over this much further
LEVEL_GRADE = 0.03      # its far half this level or more: an embankment top
STEP_M = 4.0            # m between samples
SIDE_MIN = 4            # samples a side needs to count
MAX_LIFT_M = 10.0       # m a bridge's ends are raised at most (grading.APPROACH_MAX_LIFT) ...
MAX_LIFT_FIXED_M = 6.0  # ... a motorway's or trunk's (never raised before: more is bad data, a low bridge rather than a
                        # mound across the country)
MAX_DROP_M = 8.0        # m a road under is lowered at most (grading.UNDER_MAX_DROP_M)
FILL_MAX_M = 4.0        # m an abutment read under its road's trusted height is filled up at most (its slope)
TIE_M = 100.0           # m along the road under that its junctions tie it to the grid
TIE_COST = 0.5          # x its DOWN_COST per junction there
LOW_M = 10.0            # m above the sea: low ground ...
LOW_COST = 2.0          # ... where lowering costs this much more
PATHS = ("footway", "cycleway", "path", "pedestrian", "steps", "bridleway", "sidewalk", "crossing")
# per metre: what raising a bridge's two approaches costs (their length is the lift over the ramp's grade, its width
# the road's), and what lowering the road under costs (two ramps, the walls between). A motorway's or trunk's lift is a
# long ramp on a fast road (at 4 its bridges over Perth's streets rose 2-4 m each, humps along the Mitchell Freeway
# where a street dipping 1-2 m under it is all it took): it rises only over a street tied to its grid on low ground
# (Wezep's: 3 x 2 junctions-share x 2 low = 12)
UP_COST = {"motorway": 10.0, "trunk": 8.0, "primary": 3.0, "secondary": 2.5, "tertiary": 2.0, "unclassified": 1.5,
           "residential": 1.5, "living_street": 1.5, "service": 1.0, "track": 1.0}
DOWN_COST = {"motorway": 8.0, "trunk": 6.0, "primary": 4.0, "secondary": 3.0, "tertiary": 2.5, "unclassified": 2.0,
             "residential": 2.0, "living_street": 2.0, "service": 1.0, "track": 1.0}
OTHER_UP, OTHER_DOWN = 1.0, 1.0


def base_cls(cls):
    """A class without its "_link" (a slip road weighs as its road)."""
    cls = str(cls or "")
    return cls[:-5] if cls.endswith("_link") else cls


def up_cost(cls):
    return UP_COST.get(base_cls(cls), OTHER_UP)


def down_cost(cls, ties=0, low=False):
    return DOWN_COST.get(base_cls(cls), OTHER_DOWN) * (1.0 + TIE_COST * ties) * (LOW_COST if low else 1.0)


def solve(deck, low, clear, w_up, w_down, max_lift=MAX_LIFT_M, max_drop=None):
    """The cheapest separation of one bridge over the roads under it.

    deck: the bridge's trusted height over each crossing (n,); low: each road's trusted height there (n,); clear: the
    separation each needs (n,) or one value; w_up: cost per metre of raising the bridge (one lift for all of it, its
    ends rise together); w_down: cost per metre of lowering each road (n,); max_lift; max_drop: per road (n,) or one
    value, MAX_DROP_M by default.

    Returns (lift m >= 0, drops (n,) m >= 0): deck + lift - (low - drop) >= clear wherever the limits allow; where they
    don't, both are at their limits (a low bridge rather than more earthworks). Minimises w_up * lift + sum(w_down *
    drop): piecewise linear and convex in the lift, so the best lift is 0, the cap, or one road's shortfall. Pure."""
    deck = np.atleast_1d(np.asarray(deck, np.float64))
    low = np.atleast_1d(np.asarray(low, np.float64))
    n = len(deck)
    if n == 0:
        return 0.0, np.zeros(0)
    clear = np.broadcast_to(np.asarray(clear, np.float64), (n,))
    w_down = np.broadcast_to(np.asarray(w_down, np.float64), (n,))
    max_drop = np.broadcast_to(np.asarray(MAX_DROP_M if max_drop is None else max_drop, np.float64), (n,))
    short = np.maximum(clear - (deck - low), 0.0)
    if not short.any():
        return 0.0, np.zeros(n)
    cands = np.unique(np.clip(np.r_[0.0, short, short - max_drop, max_lift], 0.0, max_lift))

    def cost(L):
        d = np.minimum(np.maximum(short - L, 0.0), max_drop)
        miss = np.maximum(short - L - d, 0.0)       # (what the limits leave: weighed far over any earthwork)
        return w_up * L + float((w_down * d).sum()) + 1e3 * float(miss.sum())
    L = float(min(cands, key=lambda c: (cost(c), c)))
    return L, np.minimum(np.maximum(short - L, 0.0), max_drop)


def side_samples(p, u, near, run, step=STEP_M):
    """Points along direction u (unit) from p, from `near` m for `run` m, both ways: (forward (k, 2), back (k, 2))."""
    d = near + step * np.arange(int(run / step) + 1)
    p, u = np.asarray(p, np.float64), np.asarray(u, np.float64)
    return p + d[:, None] * u, p - d[:, None] * u


def trusted_under(p, u, half_over, z_at):
    """The trusted height of the road under a bridge where it crosses at p, running along unit u: the median of its
    ground beside the bridge's embankment on each side (past half_over + UNDER_GAP_M), the two sides averaged (one side
    alone where the other has too few samples). Lidar's fill under the deck is never read. None without either."""
    fwd, back = side_samples(p, u, half_over + UNDER_GAP_M, UNDER_RUN_M)
    zf, zb = z_at(fwd), z_at(back)
    sides = [float(np.median(z[np.isfinite(z)])) for z in (zf, zb) if np.isfinite(z).sum() >= SIDE_MIN]
    return float(np.mean(sides)) if sides else None


def trusted_over(end, u_out, z_at):
    """The trusted height of a bridge's end: the ground along its road past the abutment (u_out, unit, pointing away
    from the bridge), its straight trend carried back to the abutment. An embankment past an end on its slope reads
    level, so the end comes up to it; ground still rising away from the bridge reads as the end's own height, not as
    the higher ground past it (Perth's Mitchell Freeway: the median of rising ground filled abutments 3-4 m over the
    streets beside them, 2026-11-01). None without enough."""
    d = OVER_GAP_M + STEP_M * np.arange(int(OVER_RUN_M / STEP_M) + 1)
    pts = np.asarray(end, np.float64) + d[:, None] * np.asarray(u_out, np.float64)
    z = np.asarray(z_at(pts), np.float64)
    ok = np.isfinite(z)
    if ok.sum() < SIDE_MIN:
        return None
    far = ok & (d >= d[0] + (d[-1] - d[0]) / 2)
    if far.sum() >= 3:
        bf, _af = np.polyfit(d[far], z[far], 1)
        if abs(bf) <= LEVEL_GRADE:
            # (level past the slope: an embankment's top, the end on its slope comes up to it. The Mitchell
            # Freeway's bridge over Roe Street: its end 3 m down the slope, the fit through the slope read 1 m)
            return float(np.median(z[far]))
    b, a = np.polyfit(d[ok], z[ok], 1)
    return float(min(a, np.median(z[ok])))


def end_dirs(line):
    """Unit directions out of a polyline's first and last points (away from it), over its first / last ~10 m."""
    line = np.asarray(line, np.float64)[:, :2]
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    L = s[-1]
    a = min(10.0, L / 2)
    p0 = np.array([np.interp(a, s, line[:, 0]), np.interp(a, s, line[:, 1])])
    p1 = np.array([np.interp(L - a, s, line[:, 0]), np.interp(L - a, s, line[:, 1])])
    u0, u1 = line[0] - p0, line[-1] - p1
    return u0 / max(np.hypot(*u0), 1e-9), u1 / max(np.hypot(*u1), 1e-9)


def dir_at(line, p):
    """Unit direction of a polyline at its point nearest p."""
    line = np.asarray(line, np.float64)[:, :2]
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    j = int(np.argmin(np.hypot(*(a + t[:, None] * ab - p).T)))
    v = ab[j]
    return v / max(np.hypot(*v), 1e-9)


def ties(p, lines, own, reach=TIE_M, touch=3.0):
    """Junctions on the road under within `reach` m of its crossing p: where another of `lines` (ground road parts,
    paths left out by the caller) meets the road's own part `own` (or the parts continuing it: one part ending where
    exactly one other begins) along its middle, or three or more parts end together on it. Pure."""
    p = np.asarray(p, np.float64)
    ls = [np.asarray(ln, np.float64)[:, :2] for ln in lines if len(ln) >= 2]
    own = np.asarray(own, np.float64)[:, :2]
    ends = [(k, e) for k, ln in enumerate(ls) for e in (ln[0], ln[-1]) if np.hypot(*(e - p)) <= reach + touch]
    if not ends:
        return 0
    pts = np.array([e for _k, e in ends])
    who = np.array([k for k, _e in ends])
    # the end groups (points where parts end together)
    gid = -np.ones(len(pts), int)
    groups = []
    for i in range(len(pts)):
        if gid[i] >= 0:
            continue
        m = (np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1]) <= touch) & (gid < 0)
        gid[m] = len(groups)
        groups.append(np.flatnonzero(m))

    def dist(q, ln):
        a, b = ln[:-1], ln[1:]
        ab = b - a
        t = np.clip(((q - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        return float(np.hypot(*(a + t[:, None] * ab - q).T).min())
    # the road: own and the parts continuing it (two ends together, one of them the road's)
    own_k = next((k for k, ln in enumerate(ls) if len(ln) == len(own) and np.allclose(ln, own)), None)
    road = {own_k} if own_k is not None else set()
    road_lines = [own] if own_k is None else []
    grow = True
    while grow:
        grow = False
        for g in groups:
            ks = set(who[g].tolist())
            if len(g) == 2 and len(ks) == 2 and (ks & road) and not ks <= road:
                road |= ks
                grow = True
    road_lines += [ls[k] for k in road if k is not None]
    n = 0
    for g in groups:
        q = pts[g].mean(0)
        if np.hypot(*(q - p)) > reach:
            continue
        ks = set(who[g].tolist())
        on_end = bool(ks & road)
        if on_end:
            n += int(len(g) >= 3)
        elif any(dist(q, ln) <= touch for ln in road_lines):
            n += 1
    return n
