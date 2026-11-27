"""Step 5 of the 3D solve: what each path may do and what moving it off the terrain costs.

    lay(network, samples, ground) -> Laws

Per path, its grading law, which no answer goes past: the steepest grade and the tightest vertical curve of its class
and speed (gradeshapes.max_grade, curve_radius: AASHTO's grades plus the game's, v^2 / a; a major road's curve no
tighter than a car drives it at its class's driving speed, drive_ceiling). A road lying on ground steeper than its
grade follows that ground: the terrain's own grade round each segment, where both its samples lie on the terrain, is
allowed there.

Per sample, its earthwork: the terrain it lies on (its smoothed ground) and what a metre off it costs, its length times
its carriageway's width times (its speed / REF_KMH)^2, so the faster line moves least. A deck, a rail deck and a tunnel
cost nothing: they span or bore. Nor does a sample where two lines are stacked (a crossing at two layers, STACK_M along
each either side): the heightmap has one height there and does not say whose, the square over a tunnel's trough or the
trough under the square.

- Tracks: OSM has no speed for a track. In a city (URBAN_ROADS_M of road within URBAN_R_M) a train runs at RAIL_CITY_KMH,
  its line bending as one that slow may (the curve's radius by that speed, RAIL_CITY_GRADE). Outside a city it runs at
  RAIL_KMH with RAIL_GRADE."""
from dataclasses import dataclass

import numpy as np

import gradeshapes as _gsh
from . import network as _network
from . import problem as _problem

REF_KMH = 50.0              # km/h at which a metre of road a metre wide a metre off its terrain costs 1
STACK_M = 8.0               # m along each line either side of a crossing at two layers where the terrain is neither's
GROUND_GRADE_WIN = 5        # samples either side over which the terrain's own grade is read (a road may follow it)
RAIL_KMH = 120.0            # a track's speed outside a city
RAIL_CITY_KMH = 50.0        # ... and in one
RAIL_CITY_R_MIN_M = 300.0   # m: the tightest vertical curve of a track in a city (outside: gradeshapes.R_MIN_RAIL_M)
RAIL_GRADE = 0.025          # a track's grade outside a city (the survey's 99th percentile over 100 m)
RAIL_CITY_GRADE = 0.035     # ... and in one
URBAN_R_M = 250.0           # a track is in a city with URBAN_ROADS_M of road within this of its middle
URBAN_ROADS_M = 3000.0      # (streets 100 m apart: ~5000 m within 250 m; open country a few hundred)
DRIVE_LIFT_G = 0.20         # the vertical acceleration a crest or a dip gives at the class's speed, at most (lift_g)
DRIVE_KMH = {"motorway": 130.0, "trunk": 100.0, "primary": 90.0, "secondary": 80.0}   # (drivability.SPEED_MS)
EARTH_KINDS = ("ground", "rail")


@dataclass
class Laws:
    """Per path (P,): grade, R (its vertical curve's least radius, m), kmh, urban (a track in a city; False for roads).
    Per sample (N,): t (its terrain), w (what a metre off it costs; 0: nothing), glim (the grade allowed from it to the
    next sample of its path; 0 at a path's last)."""
    grade: np.ndarray
    R: np.ndarray
    kmh: np.ndarray
    urban: np.ndarray
    t: np.ndarray
    w: np.ndarray
    glim: np.ndarray

    def chain(self, samples, p):
        """Path p's law as the solve reads it: t, w, xy (its samples' places), ds (m between its samples), glim (per
        segment), R, step."""
        sl = samples.of(p)
        ds = np.maximum(np.diff(samples.s[sl]), 1e-3)
        return dict(t=self.t[sl], w=self.w[sl], xy=samples.xy[sl], ds=ds, glim=self.glim[sl][:len(ds)],
                    R=float(self.R[p]), step=float(samples.h[p]))


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


def member_law(m, urban=False):
    """(grade, vertical curve radius m, km/h) of member m (a track's by urban)."""
    if _network.family(m.kind) == "rail":
        kmh = float(m.kmh) if m.kmh else (RAIL_CITY_KMH if urban else RAIL_KMH)
        v = kmh / 3.6
        r_min = RAIL_CITY_R_MIN_M if urban else _gsh.R_MIN_RAIL_M
        return (RAIL_CITY_GRADE if urban else RAIL_GRADE), max(r_min, v * v / _gsh.A_VERT_RAIL), kmh
    return float(_gsh.max_grade(m)), float(_gsh.curve_radius(m)), float(_gsh.speed(m))


def drive_radius(network, path):
    """The vertical curve radius (m) a major road's path is driven over at its fastest class's driving speed (DRIVE_KMH,
    not its limit: a car drives a city's 50 km/h secondary at 80; a link as its road: Suedtiroler Platz's secondary links,
    9 m decks, rose 2 m to a crest at 22 %); None for others (a street, a track)."""
    best = None
    for i in path.members():
        m = network.members[i]
        if _network.family(m.kind) != "road":
            continue
        v = DRIVE_KMH.get(str(m.cls or "").replace("_link", ""))     # (a link driven as its road: drivability's)
        if v is not None and (best is None or v > best):
            best = v
    if best is None:
        return None
    v = best / 3.6
    return v * v / (DRIVE_LIFT_G * 9.81)


def path_law(network, path, urban=False):
    """member_law of the path's fastest member (its strictest)."""
    return max((member_law(network.members[i], urban) for i in path.members()), key=lambda law: law[2])


def stacked(network, samples):
    """bool (N,): the samples of a crossing at two layers, those its clearance holds (under or over the other's
    carriageway: problem._reach) and STACK_M along their path past them."""
    out = np.zeros(len(samples), bool)
    for pa, ia, pb, ib, lay in samples.crossings:
        if lay.upper is None:
            continue
        for (p, i, m), (o, j, mo) in (((pa, ia, lay.a), (pb, ib, lay.b)), ((pb, ib, lay.b), (pa, ia, lay.a))):
            sl = samples.of(p)
            s = samples.s[sl]
            k = _problem._reach(samples, p, i, o, j, float(network.members[mo].half) + _problem.CLEAR_PAD_M)
            out[sl.start + np.flatnonzero((s >= s[k[0]] - STACK_M) & (s <= s[k[-1]] + STACK_M))] = True
    return out


def lay(network, samples, ground):
    """The Laws of the network's paths (step 1) over their samples (step 3) and smoothed ground (step 4)."""
    P, N = len(network.paths), len(samples)
    grade, R, kmh, urban = np.zeros(P), np.zeros(P), np.zeros(P), np.zeros(P, bool)
    t = np.asarray(ground, np.float64).copy()
    w, glim = np.zeros(N), np.zeros(N)
    kind = np.array([m.kind for m in network.members], dtype=object)
    half = np.array([float(m.half) for m in network.members])
    rail = [p for p, path in enumerate(network.paths)
            if any(_network.family(kind[i]) == "rail" for i in path.members())]
    if rail:            # (a track's city: the road round its middle sample)
        mids = np.array([samples.xy[samples.of(p)][(samples.of(p).stop - samples.of(p).start) // 2] for p in rail])
        urban[rail] = road_length_near(network, mids) >= URBAN_ROADS_M
    earth = np.isin(kind[samples.member], EARTH_KINDS) & ~stacked(network, samples)
    for p, path in enumerate(network.paths):
        grade[p], rad, kmh[p] = path_law(network, path, bool(urban[p]))
        dr = drive_radius(network, path)
        R[p] = rad if dr is None else max(rad, dr)
        sl = samples.of(p)
        ds = np.diff(samples.s[sl])
        span = np.r_[ds, 0.0] / 2 + np.r_[0.0, ds] / 2 if len(ds) else np.ones(1)   # (m of the path each sample stands for)
        on = earth[sl]
        w[sl] = np.where(on, span * 2.0 * half[samples.member[sl]] * (kmh[p] / REF_KMH) ** 2, 0.0)
        n = sl.stop - sl.start
        if n < 2:
            continue
        # (the terrain's own grade round each segment, where both its samples lie on the terrain)
        s, g = samples.s[sl], t[sl]
        k = np.arange(n - 1)
        i0, i1 = np.maximum(0, k - GROUND_GRADE_WIN), np.minimum(n - 1, k + 1 + GROUND_GRADE_WIN)
        own = np.abs(g[i1] - g[i0]) / np.maximum(s[i1] - s[i0], 1e-3)
        glim[sl.start:sl.stop - 1] = np.maximum(grade[p], np.where(on[:-1] & on[1:], own, 0.0))
    return Laws(grade, R, kmh, urban, t, w, glim)


def summary(laws):
    """Counts of what the laws hold."""
    return dict(city_tracks=int(laws.urban.sum()), on_terrain=int((laws.w > 0).sum()), free=int((laws.w == 0).sum()))
