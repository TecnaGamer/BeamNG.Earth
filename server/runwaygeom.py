"""Runway geometry and grading: the runway rectangle, shoulders, graded strip and blend zone in a
runway-local frame, a smooth longitudinal profile fitted between the data's end elevations and the
DEM under design limits, and the graded height field merged into the terrain without steps
(docs/research/2026-10-11-airports.md §3).

Pure functions (no engine, no network): a runway (airports.Runway or anything with the same fields)
plus a DEM sampler elev_fn(lat, lon) -> metres (numpy arrays in, an array of the same shape out, as
heightmap.surface uses it) give a Plan; apply(plans, lat, lon, elev) gives the graded elevations at
any points. Everything is a function of global positions (lat/lon) and the runway data, never of an
anchor, a tile or a load order (docs/ROADMAP.md "Principle: deterministic generation"); the only
input that must be pinned for bit-identical results is the DEM sampler (its source and zoom).

Frame: per runway the ENU tangent plane at its midpoint (geodesy.EnuFrame(lat_c, lon_c, 0)), then
s along the runway from the low-numbered end (le, s = 0) to the high-numbered end (he, s = L) and t
across it, positive to the left looking from le to he.

Zones (inner to outer), with the numbers in LIMITS (ICAO Annex 14 Vol I and FAA AC 150/5300-13B as
cited there; see the design doc for the table and the caveats):
  4 runway     |t| <= W/2, 0 <= s <= L: z = profile(s) - crown * (sqrt(t^2 + a^2) - a), a = 1 m
  3 shoulder   W/2 < |t| <= W/2 + S (paved runways): falling at shoulder_slope from the runway edge
  2 strip      up to G from the centreline and E beyond each end: the terrain, clamped to within
               strip_slope * d of the paved surface's height at its nearest point (d = distance from
               the paved rectangle): a smooth graded area that follows the terrain where it is gentle
  1 blend      a further B (per runway, B_MIN..B_MAX, from the largest change at the strip's edge and
               BLEND_SLOPE): z = dem + w (z_strip - dem), w = 1 - smoothstep(b / B)
  0 outside    untouched
The field is continuous everywhere (the clamp is continuous, w = 1 at the strip's edge and 0 at the
blend's), so no step reaches the physics heightmap.

Longitudinal profile: DEM samples along the centreline (the median of 5 across the runway), the
end heights = the DEM's level with the data's end-to-end difference (when the data has both end
elevations and they agree with the DEM within max(10 m, 3 % L); otherwise the DEM's ends), then a
Whittaker smoother (least squares + lambda * curvature^2, ends pinned) with the smallest lambda
whose profile keeps the vertical-curve radius >= min_radius and the local grade <= max(max_grade,
|overall grade| + 0.25 %). A runway steeper overall than its code's max_overall (altiports, bush
strips) keeps the terrain's shape: radius >= STEEP_RADIUS, no grade limit.

Several runways (an airport, or neighbours whose footprints overlap): plan_group() orders them
longest first (then by id) and fits each on the field graded by the ones before; apply() then grades
each over the previous field, except inside the earlier runways' paved areas (and fading over
PROTECT_M around them), so an intersection takes the main runway's surface.
"""
import math
from dataclasses import dataclass, field, replace

import numpy as np
import dnp

import geodesy


@dataclass(frozen=True)
class Limits:
    """Design numbers per ICAO aerodrome reference code number (1-4)."""
    code: int
    max_overall: float      # end-to-end grade (Annex 14 3.1.13)
    max_grade: float        # longitudinal grade on any portion (3.1.14)
    min_radius: float       # m, vertical curve radius (3.1.17: 0.1 % / 30 m code 4 = 30 000 m ...)
    strip_half: float       # m, graded strip half-width from the centreline (3.4.8, 2018 values)
    strip_end: float        # m, the strip beyond each runway end (3.4.2)
    strip_slope: float      # graded strip transverse slope limit (3.4.17); also used longitudinally


LIMITS = {
    4: Limits(4, 0.010, 0.0125, 30000.0, 75.0, 60.0, 0.025),
    3: Limits(3, 0.010, 0.0150, 15000.0, 75.0, 60.0, 0.025),
    2: Limits(2, 0.020, 0.0200, 7500.0, 40.0, 60.0, 0.030),
    1: Limits(1, 0.020, 0.0200, 7500.0, 30.0, 30.0, 0.030),
}
CROWN_PAVED = 0.0125        # runway transverse slope each side (FAA 1.0-1.5 %, ICAO 1-1.5 % letters C-F)
CROWN_UNPAVED = 0.02        # turf / gravel strips drain more (ICAO 2 % for letters A/B)
CROWN_ROUND = 1.0           # m: the crown's ridge is rounded over about this half-width
SHOULDER_SLOPE = 0.025      # FAA 1.5-5 %, ICAO 2.5 %
STEEP_RADIUS = 400.0        # m: vertical curve radius limit of a runway steeper than its code allows (30 m/s: 0.23 g)
RELAX_DEV = 1.5             # m: an unpaved runway whose standard profile leaves the terrain by more is relaxed
COARSE_DEM_M = 10.0         # m: a DEM coarser than this (SRTM-class, 30 m) holds steps and noise, not the
COARSE_RADIUS_PER_M = 50.0  # runway's shape: a steep runway's radius limit is then >= 50 x its pixel size
STEEP_STRIP, STEEP_STRIP_END = 7.5, 15.0    # m: a steep runway's graded strip beyond its pavement / ends
END_GRADE_SPAN = 30.0       # m: the end grades (continued into the strip beyond the ends) span this much
GRADE_MARGIN = 0.0025       # a standard runway's local grade may exceed |overall| by this much
BLEND_SLOPE = 0.25          # 1:4: the blend ramp's steepest added slope
B_MIN, B_MAX = 20.0, 250.0  # m, blend width range
PROTECT_M = 30.0            # m: a later runway fades out over this distance around an earlier one's pavement
MAX_REACH = max(l.strip_half for l in LIMITS.values()) + B_MAX + 10.0   # m beyond the runway: footprints
TARGET_TOL = (10.0, 0.03)   # data end elevations agree with the DEM: |difference| <= max(10 m, 3 % L)
TARGET_BIAS = 60.0          # m: larger data-DEM offsets are a datum or data error: DEM ends
LAMBDAS = dnp.power(10.0, np.arange(18.0, -0.01, -0.25))     # tried from the stiffest down


def runway_code(length_m):
    """ICAO aerodrome reference code number from the runway length (Annex 14 table 1-1, with the
    runway length standing in for the aeroplane reference field length): 1 < 800 m <= 2 < 1200 m <=
    3 < 1800 m <= 4."""
    return 1 if length_m < 800 else 2 if length_m < 1200 else 3 if length_m < 1800 else 4


def midpoints(a, b):
    """Midpoints (lat, lon arrays) of point pairs a, b ((n, 2) lat/lon arrays): the ECEF chord's
    midpoint dropped onto the ellipsoid (horizontal error far below a mm at runway lengths)."""
    xa = geodesy.geodetic_to_ecef(a[:, 0], a[:, 1], 0.0)
    xb = geodesy.geodetic_to_ecef(b[:, 0], b[:, 1], 0.0)
    lat, lon, _ = geodesy.ecef_to_geodetic(*((p + q) / 2 for p, q in zip(xa, xb)))
    return np.asarray(lat, np.float64), np.asarray(lon, np.float64)


def shoulder_width(width_m, paved):
    """Paved shoulder each side (FAA AC 150/5300-13B by design group, keyed here on the runway width):
    W >= 60 m 10.5 m, >= 45 m 7.5 m, >= 30 m 6 m, else 3 m; unpaved runways have none."""
    if not paved:
        return 0.0
    return 10.5 if width_m >= 60 else 7.5 if width_m >= 45 else 6.0 if width_m >= 30 else 3.0


@dataclass(frozen=True)
class Geometry:
    """A runway's static geometry (no DEM): frame, size, zones."""
    key: str
    lat_c: float
    lon_c: float
    ue: float               # unit vector le -> he in the frame's (east, north)
    un: float
    length: float           # L
    width: float            # W
    shoulder: float         # S
    strip_half: float       # G (from the centreline)
    strip_end: float        # E (beyond each end)
    crown: float
    limits: Limits
    paved: bool

    @property
    def frame(self):
        return geodesy.EnuFrame(self.lat_c, self.lon_c, 0.0)

    @property
    def paved_half(self):
        return self.width / 2 + self.shoulder

    def st(self, lat, lon, frame=None):
        """(s, t) of lat/lon arrays."""
        e, n, _ = (frame or self.frame).geodetic_to_enu(lat, lon, 0.0)
        e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
        return e * self.ue + n * self.un + self.length / 2, -e * self.un + n * self.ue

    def latlon(self, s, t):
        """lat/lon arrays of runway-local (s, t)."""
        s = np.asarray(s, np.float64) - self.length / 2
        t = np.asarray(t, np.float64)
        e = s * self.ue - t * self.un
        n = s * self.un + t * self.ue
        lat, lon, _ = self.frame.enu_to_geodetic(e, n, 0.0)
        return np.asarray(lat, np.float64), np.asarray(lon, np.float64)

    def reach_rect(self, extra):
        """(s0, s1, t_half) of the graded strip grown by `extra` metres."""
        return -self.strip_end - extra, self.length + self.strip_end + extra, self.strip_half + extra


def geometry(rw):
    """The Geometry of a runway (airports.Runway: le/he ends, length_m, width_m, paved)."""
    lat_c, lon_c = midpoints(np.array([[rw.le.lat, rw.le.lon]]), np.array([[rw.he.lat, rw.he.lon]]))
    lat_c, lon_c = float(lat_c[0]), float(lon_c[0])
    f = geodesy.EnuFrame(lat_c, lon_c, 0.0)
    e0, n0, _ = f.geodetic_to_enu(rw.le.lat, rw.le.lon, 0.0)
    e1, n1, _ = f.geodetic_to_enu(rw.he.lat, rw.he.lon, 0.0)
    de, dn = float(e1 - e0), float(n1 - n0)
    length = math.hypot(de, dn)
    lim = LIMITS[runway_code(round(length, 1))]
    sh = shoulder_width(rw.width_m, rw.paved)
    g = max(lim.strip_half, rw.width_m / 2 + sh + 10.0)
    return Geometry(key=rw.key, lat_c=lat_c, lon_c=lon_c, ue=de / length, un=dn / length, length=length,
                    width=float(rw.width_m), shoulder=sh, strip_half=g, strip_end=lim.strip_end,
                    crown=CROWN_PAVED if rw.paved else CROWN_UNPAVED, limits=lim, paved=bool(rw.paved))


def footprint_polygon(rw, frame, margin_m=None):
    """The 4 corners ((4, 2) east/north in `frame`) of a runway's influence rectangle: the graded strip
    plus B_MAX (the widest blend), or the runway rectangle grown by margin_m."""
    g = geometry(rw)
    if margin_m is None:
        s0, s1, th = g.reach_rect(B_MAX)
    else:
        s0, s1, th = -margin_m, g.length + margin_m, g.width / 2 + margin_m
    lat, lon = g.latlon(np.array([s0, s1, s1, s0]), np.array([-th, -th, th, th]))
    e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    return np.stack([np.asarray(e), np.asarray(n)], axis=1)


# --- the longitudinal profile -------------------------------------------------------------------
def _smooth(r, ds, lam):
    """Whittaker smoother of r with both ends pinned to 0: argmin |x - r|^2 + lam |D2 x|^2 over the
    interior, D2 the second difference / ds^2 (curvature, 1/m)."""
    n = len(r)
    if n < 3:
        return np.zeros(n)
    k = n - 2
    d2 = np.zeros((n - 2, n))
    i = np.arange(n - 2)
    d2[i, i], d2[i, i + 1], d2[i, i + 2] = 1.0, -2.0, 1.0
    d2 /= ds * ds
    di = d2[:, 1:-1]                    # x_0 = x_{n-1} = 0
    a = np.eye(k) + lam * (dnp.matmul(di.T, di))
    x = np.zeros(n)
    x[1:-1] = dnp.solve(a, r[1:-1])
    return x


@dataclass(frozen=True)
class Plan:
    """A runway's graded surface: its Geometry, the fitted centreline profile and the blend width."""
    geom: Geometry
    s: np.ndarray = field(repr=False)           # profile stations (m from le)
    z: np.ndarray = field(repr=False)           # graded centreline elevation (m)
    dem: np.ndarray = field(repr=False)         # the DEM (or previous field) along the centreline
    targets: tuple                               # (z_le, z_he)
    target_source: str                           # "data" / "dem" (+ why)
    steep: bool
    lam: float
    blend: float                                 # B (m)
    stats: dict
    order: int = 0
    end_grades: tuple = (0.0, 0.0)               # dz/ds at le and he, continued beyond the ends

    @property
    def key(self):
        return self.geom.key

    def profile(self, s):
        return np.interp(s, self.s, self.z)

    def extension(self, s):
        """The profile's continuation beyond the ends (0 on the runway): the end grades go on into the
        strip, so a sloping runway's strip slopes with it instead of levelling at the end height."""
        s = np.asarray(s, np.float64)
        return np.where(s < 0, self.end_grades[0] * s, 0.0) + \
            np.where(s > self.geom.length, self.end_grades[1] * (s - self.geom.length), 0.0)


def _end_level(s, d, at_le, span):
    """The DEM's height at an end: a straight line fitted over `span` metres from it, taken at the end
    (a plain mean would be the height span / 2 inside the runway)."""
    m = (s <= span) if at_le else (s >= s[-1] - span)
    x, y = s[m] - (s[0] if at_le else s[-1]), d[m]
    if len(x) < 2:
        return float(y.mean())
    xm, ym = x.mean(), y.mean()
    vx = float((dnp.sq(x - xm)).sum())
    k = float(((x - xm) * (y - ym)).sum()) / vx if vx > 0 else 0.0
    return float(ym - k * xm)


def plan(rw, elev_fn, order=0, dem_res=None):
    """The Plan of one runway over the elevation sampler elev_fn(lat, lon) (the DEM, or the field
    already graded by the runways before it: plan_group). dem_res: the DEM's native pixel size there
    (m; None = fine): above COARSE_DEM_M a steep runway's profile is kept stiffer, since the DEM's
    shape there is mostly resampling steps."""
    g = geometry(rw)
    lim = g.limits
    L = g.length
    ds = min(15.0, max(3.0, L / 300.0))
    n = int(math.ceil(L / ds)) + 1
    s = np.linspace(0.0, L, n)
    ds = L / (n - 1)
    ts = np.array([-0.5, -0.25, 0.0, 0.25, 0.5]) * g.width
    lat, lon = g.latlon(np.repeat(s, len(ts)), np.tile(ts, n))
    # each cross-section sample gives the centreline height it implies with this runway's crown (the
    # DEM of a real, crowned runway is lower at the edges); the median of the 5 resists a stray pixel
    crown_drop = (CROWN_PAVED if rw.paved else CROWN_UNPAVED) * (np.sqrt(ts * ts + CROWN_ROUND * CROWN_ROUND) - CROWN_ROUND)
    d = np.median(np.asarray(elev_fn(lat, lon), np.float64).reshape(n, len(ts)) + crown_drop, axis=1)
    span = max(ds, min(50.0, L / 10.0))
    dem_le, dem_he = _end_level(s, d, True, span), _end_level(s, d, False, span)
    src = "dem"
    z_le, z_he = dem_le, dem_he
    if rw.le.elev_source == "data" and rw.he.elev_source == "data":
        bias = ((rw.le.elev_m - dem_le) + (rw.he.elev_m - dem_he)) / 2
        diff = (rw.he.elev_m - rw.le.elev_m) - (dem_he - dem_le)
        if abs(diff) <= max(TARGET_TOL[0], TARGET_TOL[1] * L) and abs(bias) <= TARGET_BIAS:
            z_le, z_he, src = rw.le.elev_m - bias, rw.he.elev_m - bias, "data"
        else:
            src = "dem (data ends disagree)" if abs(bias) <= TARGET_BIAS else "dem (data offset)"
    else:
        src = "dem (no data ends)"
    overall = (z_he - z_le) / L
    steep = abs(overall) > lim.max_overall
    if steep:                           # altiports and bush strips: a narrow graded strip (they have no
        g = replace(g, strip_half=g.paved_half + STEEP_STRIP, strip_end=STEEP_STRIP_END)   # ICAO strip)
    r_relaxed = STEEP_RADIUS
    if dem_res is not None and dem_res > COARSE_DEM_M:
        r_relaxed = max(r_relaxed, COARSE_RADIUS_PER_M * dem_res)
    line = z_le + (z_he - z_le) * s / L
    r = d - line

    def fit(rmin, gmax):
        def ok(i):
            x = _smooth(r, ds, LAMBDAS[i])
            z = line + x
            curv = np.abs(np.diff(z, 2)) / (ds * ds) if n >= 3 else np.zeros(1)
            grade = np.abs(np.diff(z)) / ds
            return x if curv.max(initial=0.0) <= 1.0 / rmin + 1e-12 and grade.max(initial=0.0) <= gmax + 1e-12 else None

        # Bisection over LAMBDAS (stiffest first) for the softest profile within the limits: the
        # predicate is taken as monotone (a stiffer profile is straighter); the result passes either way.
        best_x, best_lam = ok(0), float(LAMBDAS[0])
        if best_x is None:              # cannot happen (the stiffest profile is the line); be safe
            return np.zeros(n), math.inf
        lo, hi = 0, len(LAMBDAS)        # LAMBDAS[lo] passes; LAMBDAS[hi] (if any) fails
        while hi - lo > 1:
            mid = (lo + hi) // 2
            x = ok(mid)
            if x is None:
                hi = mid
            else:
                lo, best_x, best_lam = mid, x, float(LAMBDAS[mid])
        return best_x, best_lam

    relaxed = steep
    if steep:
        rmin, gmax = r_relaxed, math.inf
    else:
        rmin, gmax = lim.min_radius, max(lim.max_grade, abs(overall) + GRADE_MARGIN)
    best_x, best_lam = fit(rmin, gmax)
    if not steep and not rw.paved and float(np.abs(best_x - r).max()) > RELAX_DEV:
        # an unpaved strip that the standard profile would bury or dig out by metres was not built to
        # it (backcountry strips follow their valley): keep the terrain's shape, smoothed
        relaxed, rmin, gmax = True, r_relaxed, math.inf
        best_x, best_lam = fit(rmin, gmax)
    z = line + best_x
    grade = np.diff(z) / ds
    curv = np.diff(z, 2) / (ds * ds) if n >= 3 else np.zeros(1)
    q = max(1, (n - 1) // 4)
    k = max(1, min(n - 1, int(round(min(END_GRADE_SPAN, L / 4) / ds))))
    eg = (float((z[k] - z[0]) / (s[k] - s[0])), float((z[-1] - z[-1 - k]) / (s[-1] - s[-1 - k])))
    p0 = Plan(geom=g, s=s, z=z, dem=d, targets=(z_le, z_he), target_source=src, steep=steep, lam=best_lam,
              blend=B_MIN, stats={}, order=order, end_grades=eg)
    blend, edge_max = _blend_width(p0, elev_fn)
    stats = {"length": round(L, 1), "width": g.width, "code": lim.code, "ds": round(ds, 2),
             "overall_grade_pct": round(100 * overall, 3), "max_grade_pct": round(100 * float(np.abs(grade).max()), 3),
             "max_grade_quarters_pct": round(100 * float(max(np.abs(grade[:q]).max(), np.abs(grade[-q:]).max())), 3),
             "min_radius_m": round(1.0 / float(np.abs(curv).max()), 0) if np.abs(curv).max() > 0 else None,
             "dem_rmse_m": round(float(np.sqrt(np.mean(dnp.sq(z - d)))), 3),
             "dem_max_dev_m": round(float(np.abs(z - d).max()), 3),
             "data_dem_le_m": None if rw.le.elev_source != "data" else round(rw.le.elev_m - dem_le, 2),
             "data_dem_he_m": None if rw.he.elev_source != "data" else round(rw.he.elev_m - dem_he, 2),
             "strip_edge_max_m": round(edge_max, 3), "blend_m": round(blend, 1),
             "limit_max_grade_pct": None if relaxed else round(100 * gmax, 3), "limit_min_radius_m": rmin,
             "relaxed": relaxed,
             "icao_overall_ok": abs(overall) <= lim.max_overall,
             "icao_grade_ok": float(np.abs(grade).max()) <= lim.max_grade + 1e-9}
    return Plan(geom=g, s=s, z=z, dem=d, targets=(z_le, z_he), target_source=src, steep=steep, lam=best_lam,
                blend=blend, stats=stats, order=order, end_grades=eg)


def _blend_width(p, elev_fn):
    """B from the largest change the strip makes at its own edge (sampled every ~10 m around it)."""
    g = p.geom
    s0, s1, th = g.reach_rect(0.0)
    ns = max(2, int(math.ceil((s1 - s0) / 10.0)) + 1)
    nt = max(2, int(math.ceil(2 * th / 10.0)) + 1)
    ss = np.concatenate([np.linspace(s0, s1, ns), np.linspace(s0, s1, ns), np.full(nt, s0), np.full(nt, s1)])
    tt = np.concatenate([np.full(ns, -th), np.full(ns, th), np.linspace(-th, th, nt), np.linspace(-th, th, nt)])
    lat, lon = g.latlon(ss, tt)
    dem = np.asarray(elev_fn(lat, lon), np.float64)
    zg = _strip(p, ss, tt, dem)
    m = float(np.abs(zg - dem).max())
    return float(np.clip(1.5 * m / BLEND_SLOPE, B_MIN, B_MAX)), m


# --- the surface --------------------------------------------------------------------------------
def _core(p, s, t):
    """Height of the paved surface (runway + shoulders) at its point nearest to (s, t)."""
    g = p.geom
    sc = np.clip(s, 0.0, g.length)
    tc = np.clip(np.abs(t), 0.0, g.paved_half)
    zc = p.profile(sc)
    a = CROWN_ROUND
    half = g.width / 2
    run = zc - g.crown * (np.sqrt(dnp.sq(np.minimum(tc, half)) + a * a) - a)
    return run - SHOULDER_SLOPE * np.maximum(tc - half, 0.0)


def _paved_dist(g, s, t):
    return dnp.hypot(np.maximum(np.abs(t) - g.paved_half, 0.0), np.maximum(np.maximum(-s, s - g.length), 0.0))


def _strip(p, s, t, dem):
    """The graded strip's height: the terrain clamped to within strip_slope * d of the pavement."""
    g = p.geom
    d = _paved_dist(g, s, t)
    zc = _core(p, s, t) + p.extension(s)
    k = g.limits.strip_slope * d
    return np.clip(dem, zc - k, zc + k)


def _zone_weight(p, s, t):
    """(zone code, weight) at runway-local points."""
    g = p.geom
    a = np.abs(t)
    b = dnp.hypot(np.maximum(a - g.strip_half, 0.0),
                 np.maximum(np.maximum(-s - g.strip_end, s - g.length - g.strip_end), 0.0))
    x = np.clip(b / p.blend, 0.0, 1.0)
    w = 1.0 - x * x * (3.0 - 2.0 * x)
    inside_s = (s >= 0.0) & (s <= g.length)
    zone = np.where(b >= p.blend, 0, 1)
    zone = np.where(b <= 0.0, 2, zone)
    zone = np.where(inside_s & (a <= g.paved_half), 3, zone)
    zone = np.where(inside_s & (a <= g.width / 2), 4, zone)
    return zone.astype(np.int8), w


def _protect(plans, lat, lon):
    """1 inside the paved area of any of `plans`, fading to 0 over PROTECT_M."""
    out = np.zeros(np.shape(lat))
    for q in plans:
        s, t = q.geom.st(lat, lon)
        x = np.clip(_paved_dist(q.geom, s, t) / PROTECT_M, 0.0, 1.0)
        out = np.maximum(out, 1.0 - x * x * (3.0 - 2.0 * x))
    return out


def bounds(p, extra=0.0):
    """(lat0, lon0, lat1, lon1) of a plan's influence rectangle (+ extra m)."""
    s0, s1, th = p.geom.reach_rect(p.blend + extra)
    lat, lon = p.geom.latlon(np.array([s0, s1, s1, s0]), np.array([-th, -th, th, th]))
    return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())


def apply(plans, lat, lon, elev):
    """Graded elevations at points (lat, lon arrays; elev: the DEM there, same shape). Returns
    (z, weight, zone): weight 1 where a runway's graded area sets the height, 0..1 in blends, 0 where
    untouched (z == elev exactly there); zone the highest zone code of any runway (0-4)."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    z = np.array(elev, np.float64, copy=True)
    wout = np.zeros(z.shape)
    zone = np.zeros(z.shape, np.int8)
    for k, p in enumerate(plans):
        la0, lo0, la1, lo1 = bounds(p, 1.0)
        m = (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1)
        if not m.any():
            continue
        la, lo, prev = lat[m], lon[m], z[m]
        s, t = p.geom.st(la, lo)
        zn, w = _zone_weight(p, s, t)
        hit = w > 0
        if not hit.any():
            continue
        zg = _strip(p, s, t, prev)
        earlier = [q for q in plans[:k] if _overlaps(q, p)]
        if earlier:
            w = w * (1.0 - _protect(earlier, la, lo))
        z[m] = np.where(w >= 1.0, zg, prev + w * (zg - prev))      # exact where the runway sets it
        wout[m] = np.maximum(wout[m], w)
        zone[m] = np.maximum(zone[m], np.where(w > 0, zn, 0))
    return z, wout, zone


def _overlaps(a, b):
    la0, lo0, la1, lo1 = bounds(a, PROTECT_M)
    lb0, mo0, lb1, mo1 = bounds(b)
    return not (la1 < lb0 or lb1 < la0 or lo1 < mo0 or mo1 < lo0)


def order_key(rw):
    """Grading order: longest first, then by id."""
    return (-round(float(rw.length_m), 3), rw.id)


def plan_group(runways, elev_fn, dem_res=None):
    """Plans for runways whose fields may overlap, in grading order: each fitted on the field graded
    by the ones before it."""
    plans = []
    for i, rw in enumerate(sorted(runways, key=order_key)):
        done = tuple(plans)
        fn = elev_fn if not done else (lambda la, lo, done=done: apply(done, la, lo, elev_fn(la, lo))[0])
        plans.append(plan(rw, fn, order=i, dem_res=dem_res))
    return plans


def graded_elev_fn(elev_fn, plans):
    """elev_fn with the runways graded in: the drop-in sampler for heightmap.surface / the mesh tiles."""
    plans = tuple(plans)

    def fn(lat, lon):
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        return apply(plans, lat, lon, elev_fn(lat, lon))[0]
    return fn


def groups(runways):
    """Partition runways into groups whose influence rectangles overlap (connected components),
    each sorted in grading order, the groups by their first runway's id: a pure function of the set."""
    rws = sorted(runways, key=lambda r: r.id)
    geoms = [geometry(r) for r in rws]
    boxes = []
    for g in geoms:
        s0, s1, th = g.reach_rect(B_MAX + PROTECT_M)
        lat, lon = g.latlon(np.array([s0, s1, s1, s0]), np.array([-th, -th, th, th]))
        boxes.append((lat.min(), lon.min(), lat.max(), lon.max()))
    parent = list(range(len(rws)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(rws)):
        for j in range(i + 1, len(rws)):
            a, b = boxes[i], boxes[j]
            if not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    out = {}
    for i, r in enumerate(rws):
        out.setdefault(find(i), []).append(r)
    return [sorted(v, key=order_key) for _, v in sorted(out.items(), key=lambda kv: min(r.id for r in kv[1]))]
