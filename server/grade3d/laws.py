"""Step 5 of the 3D solve: what each path may do and what it costs.

    lay(network, samples, ground) -> Laws

Per path (the kernel's chain): a comfortable grade and vertical curve, past which each metre pays for its excess
squared, and the ceilings no answer goes past. Per sample: the height it is drawn to (its smoothed ground), what a metre
off it costs, and the bounds of its kind.

- Roads: the comfortable grade and curve are the build's by class and speed (gradeshapes.max_grade, curve_radius:
  AASHTO's grades plus the game's, v^2 / a). The ceilings are CEIL_GRADE_X and CEIL_CURVE_X times as steep and as sharp.
  What the excess costs grows with the speed squared, so a motorway bends least.
- Tracks: OSM has no speed for a track. In a city (URBAN_ROADS_M of road within URBAN_R_M) a train runs at RAIL_CITY_KMH,
  and its line may bend as one that slow may (the curve's radius by that speed, RAIL_CITY_GRADE). Outside a city it runs
  at RAIL_KMH with RAIL_GRADE. A track costs RAIL_GROUND_X times a road to move off its ground: moving a track is the
  dearest change there is.
- Kinds: a road on the ground and a track pay to leave their ground. A deck pays nothing (it spans; its laws keep its
  line) but stands over its ground by its depth and DECK_AIR_M, away from its ends (ABUT_M: its abutments). A tunnel pays
  nothing and stays under its ground by its cover and headroom, away from its mouths. Floors and roofs are costs too
  (W_BOUND per m^2 short, earthfast 644): only the joins and the ceilings are hard, and those a flat network always meets,
  so every problem has an answer.

The weights share one unit, W_GROUND: a road sample a metre off its ground. At REF_KMH, a grade 1 % past the comfortable
over one STEP_M segment pays what that segment's sample a metre off its ground does (W_GRADE * h * 0.01^2 = 1)."""
from dataclasses import dataclass

import numpy as np

import carriageways as _cw
import gradeshapes as _gsh
from . import network as _network

W_GROUND = 1.0              # a road's sample a metre off its ground (per m^2)
RAIL_GROUND_X = 20.0        # a track's: this times a road's
W_BOUND = 1.0e4             # a deck's sample a metre under its floor, a tunnel's over its roof
W_GRADE = 1.0e4 / _gsh.STEP_M   # per m of path, per (grade past the comfortable)^2, at REF_KMH
W_CURVE = 1.0e3             # per m, per (curve past the comfortable, the kernel's measure)^2, at REF_KMH
REF_KMH = 60.0
SPEED_X_MAX = 6.0           # the speed's factor at most, and 1 / this at least
CEIL_GRADE_X = 2.0          # the hard ceiling: this times the comfortable grade ...
CEIL_CURVE_X = 4.0          # ... and this times as sharp a curve
LAM_ROAD = 1.0e-3           # the kernel's smoothness (second difference squared, per sample): the laws decide the line
LAM_DECK = 10.0             # a deck's: its line kept where nothing else decides
ABUT_M = 15.0               # m from a deck's (a tunnel's) end where it meets its ground
DECK_AIR_M = _gsh.DECK_AIR_M
RAIL_KMH = 120.0            # a track's speed outside a city
RAIL_CITY_KMH = 50.0        # ... and in one
RAIL_CITY_R_MIN_M = 300.0   # m: the tightest vertical curve of a track in a city (outside: gradeshapes.R_MIN_RAIL_M)
RAIL_GRADE = 0.025          # a track's comfortable grade outside a city (the survey's 99th percentile over 100 m)
RAIL_CITY_GRADE = 0.035     # ... and in one
URBAN_R_M = 250.0           # a track is in a city with URBAN_ROADS_M of road within this of its middle
URBAN_ROADS_M = 3000.0      # (streets 100 m apart: ~5000 m within 250 m; open country a few hundred)


@dataclass
class Laws:
    """Per path (P,): soft_grade, grade (ceiling), w_grade; soft_K, K (ceiling; the kernel's K: radius / 100), w_curve;
    lam; kmh; urban (a track in a city; False for roads). Per sample (N,): t (drawn to), w (per m^2 off it), lo, hi."""
    soft_grade: np.ndarray
    grade: np.ndarray
    w_grade: np.ndarray
    soft_K: np.ndarray
    K: np.ndarray
    w_curve: np.ndarray
    lam: np.ndarray
    kmh: np.ndarray
    urban: np.ndarray
    t: np.ndarray
    w: np.ndarray
    lo: np.ndarray
    hi: np.ndarray

    def chain(self, samples, p):
        """The kernel's chain dict of path p (carriageways.solve)."""
        sl = samples.of(p)
        ds = np.maximum(np.diff(samples.s[sl]), 1e-3)
        return dict(t=self.t[sl], w=self.w[sl], lo=self.lo[sl], hi=self.hi[sl], grade=float(self.grade[p]),
                    K=float(self.K[p]), lam=float(self.lam[p]), step=max(float(samples.h[p]), 1e-3), length=0.0, ds=ds,
                    soft_grade=float(self.soft_grade[p]), w_grade=float(self.w_grade[p]),
                    soft_K=float(self.soft_K[p]), w_curve=float(self.w_curve[p]), w_bound=W_BOUND)


def road_length_near(network, xy, r=URBAN_R_M):
    """m of road (ground, decks and tunnels; not tracks, not footpaths) within r of each point xy (k, 2): the members'
    segments by their midpoints, binned on a grid of r."""
    mids, lens = [], []
    for i, m in enumerate(network.members):
        line = network.net.lines[i]
        if _network.family(m.kind) != "road" or (m.cls or "") in _gsh.PATHS or len(line) < 2:
            continue
        mids.append((line[1:] + line[:-1]) / 2.0)
        lens.append(np.hypot(*np.diff(line, axis=0).T))
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    out = np.zeros(len(xy))
    if not mids:
        return out
    mid, ln = np.concatenate(mids), np.concatenate(lens)
    bins = {}
    for k, key in enumerate(map(tuple, np.floor(mid / r).astype(np.int64).tolist())):
        bins.setdefault(key, []).append(k)
    bins = {k: np.asarray(v) for k, v in bins.items()}
    for q, (a, b) in enumerate(np.floor(xy / r).astype(np.int64).tolist()):
        for da in (-1, 0, 1):
            for db in (-1, 0, 1):
                ks = bins.get((a + da, b + db))
                if ks is not None:
                    out[q] += float(ln[ks][np.hypot(*(mid[ks] - xy[q]).T) <= r].sum())
    return out


def speed_x(kmh):
    """The factor of what a law's excess costs at kmh: (kmh / REF_KMH)^2, within SPEED_X_MAX either way."""
    return float(np.clip((float(kmh) / REF_KMH) ** 2, 1.0 / SPEED_X_MAX, SPEED_X_MAX))


def member_law(m, urban=False):
    """(comfortable grade, comfortable vertical curve radius m, km/h) of member m (a track's by urban)."""
    if _network.family(m.kind) == "rail":
        kmh = float(m.kmh) if m.kmh else (RAIL_CITY_KMH if urban else RAIL_KMH)
        v = kmh / 3.6
        r_min = RAIL_CITY_R_MIN_M if urban else _gsh.R_MIN_RAIL_M
        return (RAIL_CITY_GRADE if urban else RAIL_GRADE), max(r_min, v * v / _gsh.A_VERT_RAIL), kmh
    return float(_gsh.max_grade(m)), float(_gsh.curve_radius(m)), float(_gsh.speed(m))


DRIVE_GRADE = 0.15          # the steepest a major road is driven (tests/drivability.py: grade)
DRIVE_LIFT_G = 0.20         # the vertical acceleration a crest or a dip gives at the class's speed, at most (lift_g)
DRIVE_KMH = {"motorway": 130.0, "trunk": 100.0, "primary": 90.0, "secondary": 80.0}   # (drivability.SPEED_MS)


def drive_ceiling(network, path):
    """(grade, vertical curve radius m) a major road's path may not go past, whatever its comfortable laws: the drivable
    ones at its fastest class's driving speed (DRIVE_KMH, not its limit: a car drives a city's 50 km/h secondary at 80);
    (None, None) for others (a link, a street, a track)."""
    best = None
    for i in path.members():
        m = network.members[i]
        if _network.family(m.kind) != "road":
            continue
        v = DRIVE_KMH.get(str(m.cls or ""))
        if v is not None and (best is None or v > best):
            best = v
    if best is None:
        return None, None
    v = best / 3.6
    return DRIVE_GRADE, v * v / (DRIVE_LIFT_G * 9.81)


def path_law(network, path, urban=False):
    """member_law of the path's fastest member (its strictest)."""
    return max((member_law(network.members[i], urban) for i in path.members()), key=lambda law: law[2])


def inner(samples, p, near_m=ABUT_M):
    """bool (n,) of path p's samples more than near_m (along the path) from both ends of the member each is on."""
    sl = samples.of(p)
    s, mem = samples.s[sl], samples.member[sl]
    out = np.zeros(len(s), bool)
    for i in np.unique(mem):
        on = mem == i
        a, b = s[on].min(), s[on].max()
        out[on] = (s[on] - a > near_m) & (b - s[on] > near_m)
    return out


def lay(network, samples, ground):
    """The Laws of the network's paths (step 1) over their samples (step 3) and smoothed ground (step 4)."""
    P, N = len(network.paths), len(samples)
    soft_grade, grade, w_grade = np.zeros(P), np.zeros(P), np.zeros(P)
    soft_K, K, w_curve = np.zeros(P), np.zeros(P), np.zeros(P)
    lam, kmh, urban = np.full(P, LAM_ROAD), np.zeros(P), np.zeros(P, bool)
    t = np.asarray(ground, np.float64).copy()
    w, lo, hi = np.zeros(N), np.full(N, -np.inf), np.full(N, np.inf)
    kind = np.array([m.kind for m in network.members], dtype=object)
    rail = [p for p, path in enumerate(network.paths)
            if any(_network.family(kind[i]) == "rail" for i in path.members())]
    if rail:            # (a track's city: the road round its middle sample)
        mids = np.array([samples.xy[samples.of(p)][(samples.of(p).stop - samples.of(p).start) // 2] for p in rail])
        urban[rail] = road_length_near(network, mids) >= URBAN_ROADS_M
    depth = {}
    for p, path in enumerate(network.paths):
        g, rad, kmh[p] = path_law(network, path, bool(urban[p]))
        x = speed_x(kmh[p])
        soft_grade[p], w_grade[p] = g, W_GRADE * x
        grade[p] = g * CEIL_GRADE_X if CEIL_GRADE_X else _cw.NO_GRADE_CEILING        # (0: no ceiling, the cost alone)
        soft_K[p], w_curve[p] = rad / 100.0, W_CURVE * x
        K[p] = rad / 100.0 / CEIL_CURVE_X if CEIL_CURVE_X else _cw.NO_CURVE_CEILING
        dg, dr = drive_ceiling(network, path)
        if dg is not None:          # (no ceiling past what a car drives: the drivable laws)
            grade[p] = min(grade[p], max(dg, soft_grade[p]))
            K[p] = max(K[p], dr / 100.0)
            if soft_K[p] < K[p]:
                soft_K[p] = K[p]    # (the comfortable curve no sharper than the ceiling: the ceiling alone)
        sl = samples.of(p)
        mem = samples.member[sl]
        kk = kind[mem]
        w[sl] = np.where(kk == "ground", W_GROUND, np.where(kk == "rail", W_GROUND * RAIL_GROUND_X, 0.0))
        away = inner(samples, p)
        deck = away & ((kk == "deck") | (kk == "rail_deck"))
        if deck.any():
            for i in np.unique(mem[deck]):
                if i not in depth:
                    depth[i] = (_gsh.deck_depth(network.net.L[i]) if kind[i] == "deck" else _gsh.RAIL_DECK_DEPTH_M)
            lo[sl.start + np.flatnonzero(deck)] = t[sl][deck] + np.array([depth[i] for i in mem[deck]]) + DECK_AIR_M
            lam[p] = LAM_DECK
        tun = away & (kk == "tunnel")
        if tun.any():
            hi[sl.start + np.flatnonzero(tun)] = t[sl][tun] - _gsh.COVER_M - _gsh.HEADROOM_M
        free = w[sl] == 0.0
        if free.any() and not free.all():
            # (the kernel draws every sample to its t at W_MIN at least: a tunnel's to the mountain over it, a deck's
            # to the valley under it. Casagranda's road climbed 55 m into its tunnel at the soft grade, its bridge 25 m
            # over the valley. A sample off the ground is drawn to the line between the ground on either side.)
            s = samples.s[sl]
            t[sl.start + np.flatnonzero(free)] = np.interp(s[free], s[~free], t[sl][~free])
    return Laws(soft_grade, grade, w_grade, soft_K, K, w_curve, lam, kmh, urban, t, w, lo, hi)


def summary(laws):
    """Counts of what the laws hold."""
    return dict(track_samples=int(np.count_nonzero(laws.w >= W_GROUND * RAIL_GROUND_X)),
                city_tracks=int(laws.urban.sum()),
                deck_floor=int(np.isfinite(laws.lo).sum()), tunnel_roof=int(np.isfinite(laws.hi).sum()),
                free=int((laws.w == 0).sum()))
