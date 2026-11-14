"""Real gravity: WGS84 normal gravity plus EGM2008 anomalies.

    python3 server/gravity.py LAT LON H [--msl] [--nmax N] [--coefficients X.npz]   # prints values

Three layers, each usable on its own:

* ``normal_gravity(lat, h)``: WGS84 normal gravity (Somigliana's closed formula on the ellipsoid
  and the second-order free-air series in h, NIMA TR8350.2 eq. 4-1 and 4-3). Pure numpy, no data.
  ``normal_gravity_exact(lat, h)`` is the closed form in ellipsoidal coordinates (TR8350.2 §4.2),
  used as the reference in the tests.
* ``GravityModel``: a spherical-harmonic geopotential model (EGM2008 by default) evaluated at one
  point: the disturbing potential T and its gradient, from which the gravity vector, the gravity
  disturbance, the free-air anomaly, the deflection of the vertical and the geoid height follow.
* ``gravity(lat, lon, h)`` / ``anomaly(lat, lon, h)`` / ``geoid_height(lat, lon)``: module-level
  helpers on a shared EGM2008 model (downloaded once, see ``load_egm2008``).
* ``Provider`` / ``Tracker``: earthd's 'gravity' message (``--gravity on``): the player's |g| on a
  worker thread, every 2 km or 200 m of height; normal gravity while EGM2008 downloads.

Heights are ellipsoidal (WGS84) unless ``msl=True``: then h is above mean sea level (the DEM's
heights) and the EGM2008 geoid height N is added (h_ell = H + N; N is -107 m .. +86 m, which
would otherwise be a 0..33 mGal error, larger than many anomalies).

Units: m/s^2 everywhere (1 mGal = 1e-5 m/s^2), deflections in arc seconds, heights in metres.

The synthesis (``GravityModel._row_sums``) follows the usual geodesy recipe: fully normalised
associated Legendre functions by the standard forward column recursion along n (Holmes and
Featherstone 2002), with a separate log-scale per order m so that the sectoral terms P_mm ~ cos^m
never underflow near the poles (degree 2190 is accurate at every latitude), and the latitude
derivative from the same row (no division by cos). All the per-degree sums are radius-independent,
so one pass gives the field at the point and, along the same radius, on the ellipsoid (geoid).
"""
import hashlib
import http.client
import json
import math
import os
import ioprofiler
import struct
import sys
import tarfile
import threading
import time
import urllib.request
from typing import NamedTuple

import numpy as np

import fast
import dmath
import dnp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geodesy import A, F, B, E2, geodetic_to_ecef  # noqa: E402
import netfetch  # noqa: E402

# --- WGS84 (NIMA TR8350.2, 3rd ed., Tables 3.1-3.4) --------------------------------------------
GM = 3.986004418e14              # m^3/s^2, incl. the atmosphere
OMEGA = 7.292115e-5              # rad/s
GAMMA_E = 9.7803253359           # normal gravity on the equator, m/s^2
GAMMA_P = 9.8321849378           # normal gravity at the poles, m/s^2
K_SOMIGLIANA = B * GAMMA_P / (A * GAMMA_E) - 1     # 0.0019318526464 (TR8350.2: ...65241)
M_RATIO = (OMEGA * OMEGA) * (A * A) * B / GM               # m = w^2 a^2 b / GM = 0.00344978650684
LIN_ECC = math.sqrt(A * A - B * B)                   # E, linear eccentricity
EP = LIN_ECC / B                                     # second eccentricity e'
MGAL = 1e-5                                          # m/s^2

# --- EGM2008 (GeographicLib's binary repackaging of NGA's EGM2008_to2190_TideFree) --------------
EGM2008_URL = "https://downloads.sourceforge.net/project/geographiclib/gravity-distrib/egm2008.tar.bz2"
EGM2008_SHA256 = "54c1f511892a3d6a1362fb91a8f65efb4bd56e1cf4ff205344e003bd5074ed91"
EGM2008_SIZE = 74456990
EGM2008_GM = 3.986004415e14      # ModelMass
EGM2008_A = 6378136.3            # ModelRadius
EGM2008_HEIGHT_OFFSET = -0.41    # m, NGA's zero-degree geoid term for WGS84
EGM2008_NMAX = 2190
DEFAULT_NMAX = 2190              # see server/README.md "Gravity" for the timings behind this
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth", "gravity")


# ================================================================================================
# Normal gravity
# ================================================================================================

def normal_gravity(lat, h=0.0):
    """WGS84 normal gravity in m/s^2 at geodetic latitude ``lat`` (deg), ellipsoidal height h (m).

    Somigliana (TR8350.2 eq. 4-1):  g0 = gE (1 + k sin^2 lat) / sqrt(1 - e^2 sin^2 lat)
    then the free-air series to second order in h (eq. 4-3):
        g(h) = g0 [1 - 2/a (1 + f + m - 2 f sin^2 lat) h + 3 h^2 / a^2]
    Exact on the ellipsoid (1e-12). The height series is first order in f and m: against the exact
    closed form it is 1.1e-10 m/s^2 per metre off (0.05 mGal at 4.4 km, 0.11 mGal at 10 km).
    """
    s2 = dnp.sq(dnp.sin(np.radians(lat)))
    g0 = GAMMA_E * (1 + K_SOMIGLIANA * s2) / np.sqrt(1 - E2 * s2)
    h = np.asarray(h, dtype=np.float64)
    return g0 * (1 - 2 / A * (1 + F + M_RATIO - 2 * F * s2) * h + 3 * h * h / (A * A))


def free_air_gradient(lat):
    """d(normal gravity)/dh on the ellipsoid, s^-2 (about -3.086e-6, i.e. -0.3086 mGal/m)."""
    s2 = dnp.sq(dnp.sin(np.radians(lat)))
    g0 = GAMMA_E * (1 + K_SOMIGLIANA * s2) / np.sqrt(1 - E2 * s2)
    return -2 * g0 / A * (1 + F + M_RATIO - 2 * F * s2)


def _q_series(x):
    """q(x) = 1/2 [(1 + 3/x^2) atan x - 3/x] without the cancellation (x = E/u < 0.09)."""
    x2, term, s = x * x, x, 0.0
    for k in range(1, 40):
        term *= x2                                       # x^(2k+1)
        s += (-1) ** (k + 1) * 2 * k * term / ((2 * k + 1) * (2 * k + 3))
    return s


def _qp_series(x):
    """q'(x) = 3 (1 + 1/x^2)(1 - atan(x)/x) - 1, same idea."""
    x2, s = x * x, 0.0
    for k in range(1, 40):
        s += (-1) ** (k + 1) * 6 * dnp.power(x2, k) / ((2 * k + 1) * (2 * k + 3))
    return s


Q0 = _q_series(EP)               # q0 = q(E/b)
QP0 = _qp_series(EP)


def normal_gravity_exact(lat, h=0.0, vector=False):
    """Exact WGS84 normal gravity (closed form in ellipsoidal coordinates, TR8350.2 eq. 4-4..4-12).

    Scalar lat/h (degrees, metres). Slower than ``normal_gravity`` but exact at any height.
    ``vector=True`` returns the (north, up) components in the local geodetic frame instead of the
    magnitude: above the ellipsoid the normal plumb line is curved, so north is not 0 there.
    """
    x, y, z = geodetic_to_ecef(lat, 0.0, h)
    p2, z2, e2 = float(x * x + y * y), float(z * z), LIN_ECC * LIN_ECC
    rr = p2 + z2 - e2
    u = math.sqrt(0.5 * rr * (1 + math.sqrt(1 + 4 * e2 * z2 / (rr * rr))))
    beta = dmath.atan2(z * math.sqrt(u * u + e2), u * math.sqrt(p2))
    sb, cb = dmath.sin(beta), dmath.cos(beta)
    w = math.sqrt((u * u + e2 * sb * sb) / (u * u + e2))
    q, qp = _q_series(LIN_ECC / u), _qp_series(LIN_ECC / u)
    ue = u * u + e2
    g_u = -(GM / ue + (OMEGA * OMEGA) * (A * A) * LIN_ECC / ue * (qp / Q0) * (0.5 * sb * sb - 1 / 6)
            - (OMEGA * OMEGA) * u * cb * cb) / w
    g_b = (-(OMEGA * OMEGA) * (A * A) / math.sqrt(ue) * (q / Q0) + (OMEGA * OMEGA) * math.sqrt(ue)) * sb * cb / w
    if not vector:
        return math.hypot(g_u, g_b)
    # unit vectors of increasing u and beta in the meridian plane (p, z), then onto (north, up)
    eu = np.array([u / math.sqrt(ue) * cb, sb])
    eb = np.array([-math.sqrt(ue) * sb, u * cb])
    eu, eb = eu / dnp.norm(eu), eb / dnp.norm(eb)
    g = g_u * eu - g_b * eb       # TR8350.2's gamma_beta is the component towards decreasing beta
    sp, cp = dmath.sin(math.radians(lat)), dmath.cos(math.radians(lat))
    return float(-sp * g[0] + cp * g[1]), float(cp * g[0] + sp * g[1])


def normal_zonal_coefficients(nmax=20):
    """Fully normalised C_n0 of the WGS84 normal gravitational potential (GM, a scaling), n <= nmax.

    J2 from the defining constants, J2n by Heiskanen & Moritz (1967) eq. 2-92:
        J2n = (-1)^(n+1) 3 e^2n / ((2n+1)(2n+3)) (1 - n + 5 n J2 / e^2),   C_2n,0 = -J2n / sqrt(4n+1)
    """
    j2 = E2 / 3 * (1 - 2 * M_RATIO * EP / (15 * Q0))
    c = np.zeros(nmax + 1)
    c[0] = 1.0
    for n in range(1, nmax // 2 + 1):
        j2n = (-1) ** (n + 1) * 3 * dnp.power(E2, n) / ((2 * n + 1) * (2 * n + 3)) * (1 - n + 5 * n * j2 / E2)
        c[2 * n] = -j2n / math.sqrt(4 * n + 1)
    return c


# ================================================================================================
# Spherical-harmonic model
# ================================================================================================

def tri_len(nmax):
    """Length of a row-major (n, then m = 0..n) coefficient triangle up to degree nmax."""
    return (nmax + 1) * (nmax + 2) // 2


class GravityField(NamedTuple):
    g: float            # |gravity| (gravitation + centrifugal), m/s^2
    east: float         # gravity vector in the local geodetic ENU frame, m/s^2 (up is negative)
    north: float
    up: float
    normal: float       # WGS84 normal gravity at the same point (exact closed form)
    disturbance: float  # g - normal (gravity disturbance), m/s^2
    anomaly: float      # free-air gravity anomaly -dT/dr - 2T/r (spherical approximation), m/s^2
    xi: float           # deflection of the vertical, north-south / east-west, arcsec
    eta: float
    geoid: float        # EGM2008 geoid height N above the ellipsoid, m
    h: float            # ellipsoidal height used, m


class GravityModel:
    """A geopotential model relative to the WGS84 normal field, evaluated point by point.

    ``c``, ``s``: fully normalised (4 pi, geodesy convention) coefficients as row-major triangles
    (n = 0..nmax, m = 0..n) in the model's own scaling ``gm``, ``a``; C00 is taken as 1 whatever
    is stored (GeographicLib stores 0). They may be read-only memmaps (only the first
    tri_len(nmax) entries are touched). ``corr_c``/``corr_s``: optional zeta-to-N correction
    triangles (metres, no radial factor) for the geoid height. ``dtype`` of the per-degree sums.
    """

    RESCALE = 1e100      # renormalise a Legendre column once it exceeds this
    NZONAL = 20          # degree of the WGS84 normal zonal series (J2..J20)

    def __init__(self, c, s, nmax=None, gm=EGM2008_GM, a=EGM2008_A, corr_c=None, corr_s=None,
                 corr_nmax=2160, height_offset=EGM2008_HEIGHT_OFFSET, name="model",
                 dtype=np.float32):
        full = int(round((math.sqrt(8 * len(c) + 1) - 3) / 2))
        assert tri_len(full) == len(c) == len(s), "coefficients must be full row-major triangles"
        nmax = full if nmax is None else min(int(nmax), full)
        L = tri_len(nmax)
        self.nmax, self.gm, self.a, self.name = nmax, float(gm), float(a), name
        self.height_offset = float(height_offset)
        self.dtype = np.dtype(dtype)
        # Built column by column: the process peaks at ~390 MB while building degree 2190.
        n_f = np.repeat(np.arange(nmax + 1, dtype=np.int32), np.arange(1, nmax + 2))
        m_f = np.arange(L, dtype=np.int32) - n_f * (n_f + 1) // 2
        # disturbing potential: subtract the normal field, rescaled to the model's GM and radius
        dc = np.array(c[:L], dtype=np.float64)
        ds = np.array(s[:L], dtype=np.float64)
        dc[0] = 1.0
        nz = normal_zonal_coefficients(self.NZONAL)
        self.zonal = nz * (GM / self.gm) * dnp.power(A / self.a, np.arange(len(nz)))
        k = min(len(nz), nmax + 1)
        zidx = np.arange(k) * (np.arange(k) + 1) // 2
        dc[zidx] -= self.zonal[:k]
        # Columns of the per-degree matmul. With v = P_nm (true scale), c_m = cos m lam, s_m = sin:
        #   0, 1  T:        sum v (C c + S s)
        #   2, 3  d/dlam:   sum v m (S c - C s)
        #   4..7  d/dtheta: sum dP/dtheta (C c + S s) = sum v [c (cl P1 + sl P2) + s (cl P3 - sl P4)]
        # where P1..P4 are the coefficients shifted by one order (dP_nm/dtheta mixes P_n,m+-1 and
        # cos (m+-1) lam = c cl -+ s sl, cl = cos lam), so no derivative row is needed in the loop.
        #   8, 9  NGA's zeta-to-N geoid correction (metres, a surface harmonic, EGM2008 to degree
        #         2160). Only the geoid height uses it; without it N is off by up to 2.6 m on
        #         Everest (0.8 mGal through the MSL height).
        # float32 (the default) halves the memory; against float64 the field moves by < 0.001 mGal
        # and the geoid by < 0.01 mm (measured at 30 points, degree 2190). The Legendre recursion
        # itself always runs in float64.
        ncol = 10 if corr_c is not None else 8
        coef = self.coef = np.zeros((L, ncol), dtype=self.dtype)
        coef[:, 0], coef[:, 1] = dc, ds
        coef[:, 2], coef[:, 3] = m_f * dc, m_f * ds
        da, db = self._tables(n_f, m_f)
        del n_f
        # dP_nm/dtheta = da P_n,m-1 - db P_n,m+1. The shifts may cross a row end: there da (m = 0)
        # and db (m = n) are zero, so nothing leaks between degrees.
        for x, col_minus, col_plus in ((dc, 4, 7), (ds, 6, 5)):
            u = np.zeros(L)
            u[:-1] = da[1:] * x[1:]                      # U_m = da_m+1 X_m+1
            w = np.zeros(L)
            w[1:] = db[:-1] * x[:-1]                     # W_m = db_m-1 X_m-1
            coef[:, col_minus] = u - w                   # P1 = U1 - W1, P3 = U2 - W2
            coef[:, col_plus] = u + w                    # P4 = U1 + W1, P2 = U2 + W2
            del u, w
        del da, db, dc, ds, m_f
        if corr_c is not None:
            nc = int(round((math.sqrt(8 * len(corr_c) + 1) - 3) / 2))
            lc = tri_len(min(corr_nmax, nmax, nc))
            coef[:lc, 8], coef[:lc, 9] = corr_c[:lc], corr_s[:lc]

    def _tables(self, n_f, m_f):
        """Recursion tables (kept) and the derivative factors da, db (returned)."""
        nmax = self.nmax
        n, m = n_f.astype(np.float64), m_f.astype(np.float64)
        valid = m <= n - 2
        with np.errstate(divide="ignore", invalid="ignore"):
            nm2 = (n - m) * (n + m)
            ra = (2 * n - 1) * (2 * n + 1)
            ra /= nm2
            np.sqrt(ra, out=ra)
            ra[~valid] = 0.0
            self.ra = ra
            rb = (2 * n + 1) * (n + m - 1)
            rb *= n - m - 1
            nm2 *= 2 * n - 3
            rb /= nm2
            np.sqrt(rb, out=rb)
            rb[~valid] = 0.0
            self.rb = rb
            del nm2, valid
        # sectoral P_mm = cos(phi)^m * prod: keep log(prod); P_m+1,m = sqrt(2m+3) t P_mm
        k = np.arange(nmax + 1, dtype=np.float64)
        steps = np.zeros(nmax + 1)
        steps[1:] = 0.5 * dnp.log((2 * k[1:] + 1) / (2 * k[1:]))
        steps[1] = 0.5 * dmath.log(3.0)
        self.log_sect = np.cumsum(steps)
        self.sect_next = np.sqrt(2 * k + 3)
        # |P_n| <= (|ra| + |rb|) max(|P_n-1|, |P_n-2|): renormalising every `check_every` degrees
        # keeps every scaled column below RESCALE^2 = 1e200
        growth = max(float(np.max(self.ra + self.rb)), float(self.sect_next[-1]), 2.0)
        self.check_every = max(1, int(100 / dmath.log10(growth)))
        self.off = (np.arange(nmax + 2) * np.arange(1, nmax + 3)) // 2
        # dP_nm/dtheta = da * P_n,m-1 - db * P_n,m+1  (theta = colatitude, geodesy normalisation)
        da = (n + m) * (n - m + 1)
        np.sqrt(da, out=da)
        da *= 0.5
        da[m == 1] *= math.sqrt(2.0)
        da[m == 0] = 0.0
        db = (n - m) * (n + m + 1)
        np.maximum(db, 0.0, out=db)
        np.sqrt(db, out=db)
        db *= 0.5
        zero = m == 0
        db[zero] = np.sqrt(n[zero] * (n[zero] + 1) / 2)
        return da, db

    def memory_bytes(self):
        return sum(x.nbytes for x in (self.coef, self.ra, self.rb))

    def _row_sums(self, t, u, lam):
        """Per-degree sums over m (radius-independent): the (nmax+1, 2, ncol) matrices
        [P cos m lam; P sin m lam] @ coef rows, and P_n0, P_n1 up to NZONAL (float64). In Rust (earthfast 163,
        gravity.rs: the same recursion and rescale points; the Python loop took 37.8 CPU-s of the server's interpreter
        per 15 minutes, 2026-11-07)."""
        fast.require(163, "gravity row sums")
        fn = fast._ef.gravity_row_sums_f32 if self.coef.dtype == np.float32 else fast._ef.gravity_row_sums_f64
        coef = self.coef if self.coef.dtype in (np.float32, np.float64) else self.coef.astype(np.float64)
        return fn(float(t), float(u), float(lam), np.ascontiguousarray(self.ra, np.float64),
                  np.ascontiguousarray(self.rb, np.float64), np.ascontiguousarray(coef),
                  np.ascontiguousarray(self.off, np.int64), np.ascontiguousarray(self.sect_next, np.float64),
                  np.ascontiguousarray(self.log_sect, np.float64), int(self.check_every), float(self.RESCALE),
                  int(self.NZONAL), int(self.nmax))

    def _evaluate(self, lat, lon, h, msl):
        """Row sums along the direction of (lat, lon, h), then the radial factors at the final
        height. With ``msl`` the final height is h + N; the sums are not redone for it (the
        direction moves by 0.01" per 100 m of N, i.e. 0.3 m on the ground)."""
        lat = float(np.clip(lat, -90 + 1e-9, 90 - 1e-9))   # 0.1 mm off the pole: ENU is defined
        x, y, z = (float(v) for v in geodetic_to_ecef(lat, lon, h))
        p = math.hypot(x, y)
        r = math.hypot(p, z)
        lam = dmath.atan2(y, x)
        t, u = z / r, p / r
        out, zon = self._row_sums(t, u, lam)
        out = out.astype(np.float64)
        cl, sl = dmath.cos(lam), dmath.sin(lam)
        n = np.arange(self.nmax + 1, dtype=np.float64)
        s = {"a": out[:, 0, 0] + out[:, 1, 1],                 # sum_m P (C cos + S sin)
             "l": out[:, 0, 3] - out[:, 1, 2],                 # sum_m m P (S cos - C sin)
             "d": -(cl * (out[:, 0, 4] + out[:, 1, 6]) + sl * (out[:, 0, 5] - out[:, 1, 7]))}
        nz = np.arange(len(zon), dtype=np.float64)
        za = zon[:, 0] * self.zonal[:len(zon)]                               # normal zonal part
        zd = np.sqrt(nz * (nz + 1) / 2) * zon[:, 1] * self.zonal[:len(zon)]  # and its d/dphi

        # geoid: T on the ellipsoid along the same radius, Bruns, plus NGA's correction and offset
        re = A * B / math.hypot(B * u, A * t)
        t0 = self.gm / re * float(dnp.matmul(dnp.power(self.a / re, n), s["a"])) - (self.gm - GM) / re  # NGA: no 1/r
        lat_e = math.degrees(dmath.atan2(t, u * (1 - E2)))            # geodetic lat of that point
        corr = float(np.sum(out[:, 0, 8] + out[:, 1, 9])) if out.shape[2] > 8 else 0.0
        geoid = t0 / normal_gravity_exact(lat_e, 0.0) + corr + self.height_offset
        if msl:
            h = h + geoid
            x, y, z = (float(v) for v in geodetic_to_ecef(lat, lon, h))
            p = math.hypot(x, y)
            r = math.hypot(p, z)
            t, u = z / r, p / r
        q = dnp.power(self.a / r, n)
        gr = self.gm / r
        pot = {"T": gr * float(dnp.matmul(q, s["a"])),
               "dT_dr": -gr / r * float(dnp.matmul(q, (n + 1) * s["a"])),
               "dT_dphi": gr * float(dnp.matmul(q, s["d"])),
               "dT_dlam": gr * float(dnp.matmul(q, s["l"]))}
        qz = q[:len(zon)]
        du_dr = -gr / r * float(dnp.matmul(qz, (nz + 1) * za))
        du_dphi = gr * float(dnp.matmul(qz, zd))
        return lat, h, r, t, u, pot, du_dr, du_dphi, geoid

    def disturbing_potential(self, lat, lon, h=0.0):
        """T (m^2/s^2) and its derivatives dT/dr, dT/dphi (geocentric latitude), dT/dlam at the
        point, plus the radius r and geocentric sin/cos latitude t, u: a dict."""
        _, _, r, t, u, pot, _, _, _ = self._evaluate(lat, lon, h, False)
        return dict(pot, r=r, t=t, u=u)

    def field(self, lat, lon, h=0.0, msl=False):
        """The full field at one point. ``h`` is ellipsoidal unless ``msl`` (then h_ell = h + N)."""
        lat, h, r, t, u, pot, du_dr, du_dphi, geoid = self._evaluate(lat, lon, h, msl)
        # gradients in the geocentric spherical frame (east, north, radial); normal = U + centrifugal
        gam = np.array([0.0, du_dphi / r - (OMEGA * OMEGA) * r * u * t, du_dr + (OMEGA * OMEGA) * r * u * u])
        dlt = np.array([pot["dT_dlam"] / (r * u), pot["dT_dphi"] / r, pot["dT_dr"]])
        d = math.radians(lat) - dmath.atan2(t, u)                    # geodetic - geocentric latitude
        cd, sd = dmath.cos(d), dmath.sin(d)
        rot = np.array([[1, 0, 0], [0, cd, -sd], [0, sd, cd]])      # (e, n_c, r) -> (e, n, up)
        gvec = dnp.matmul(rot, gam + dlt)
        dvec = dnp.matmul(rot, dlt)
        g = float(dnp.norm(gvec))
        normal = normal_gravity_exact(lat, h)
        rad2as = 180 / math.pi * 3600
        return GravityField(
            g=g, east=float(gvec[0]), north=float(gvec[1]), up=float(gvec[2]), normal=normal,
            disturbance=g - normal, anomaly=-pot["dT_dr"] - 2 * pot["T"] / r,
            xi=float(-dvec[1] / normal * rad2as), eta=float(-dvec[0] / normal * rad2as),
            geoid=geoid, h=float(h))

    @classmethod
    def from_npz(cls, path, nmax=None, **kw):
        """A model from an .npz with row-major triangles ``c``, ``s`` (+ optional ``corr_c``,
        ``corr_s``) and scalars ``gm``, ``a``, ``height_offset`` (the test fixture format)."""
        z = np.load(path)
        extra = {k: float(z[k]) for k in ("gm", "a", "height_offset") if k in z}
        corr = {k: z[k] for k in ("corr_c", "corr_s") if k in z}
        return cls(z["c"], z["s"], nmax=nmax, name=os.path.basename(path), **extra, **corr, **kw)


# ================================================================================================
# EGM2008 data: download once, verify, convert to a row-major .npy cache
# ================================================================================================

def _sha256(path):
    hsh = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            hsh.update(chunk)
    return hsh.hexdigest()


def _download(url, dest, sha256, size, attempts=10, min_rate=50e3, log=print, net=None):
    """Download with resume and retries, then verify size and SHA-256. SourceForge redirects each
    request to a random mirror; some stall or close early, so a short or slow transfer is resumed
    (HTTP Range) from another one. Each request goes through net (a netfetch.Fetcher; None = the
    process-wide one): the host's policy (one at a time), the project User-Agent (nothing else is
    sent), its circuit breaker and Retry-After."""
    net = net if net is not None else netfetch.default_fetcher()
    # Windows: the system's own certificate check, as every other request (netfetch.HttpsTransport): OpenSSL's chain
    # picked an expired intermediate on some SourceForge mirrors and every attempt failed with "certificate has
    # expired" (GitHub #567; this download opens its own connections, so the 1.7.0 fix never reached it)
    tls = netfetch.windows_ssl_context()
    part = dest + ".part"
    for i in range(attempts):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            os.remove(part)
            have = 0
        if have < size:
            try:
                with net.session(url) as s:
                    req = urllib.request.Request(url, headers=s.headers)
                    if have:
                        req.add_header("Range", f"bytes={have}-")
                    with urllib.request.urlopen(req, timeout=30, context=tls) as resp:
                        ioprofiler.headers_received()
                        s.status = resp.status
                        mode = "ab" if have and resp.status == 206 else "wb"
                        with open(part, mode) as f:
                            t0, got = time.monotonic(), 0
                            while True:
                                chunk = resp.read(1 << 16)
                                if not chunk:
                                    break
                                f.write(chunk)
                                got += len(chunk)
                                ioprofiler.received_bytes(len(chunk))
                                s.nbytes = got
                                dt = time.monotonic() - t0
                                if dt > 20 and got / dt < min_rate:   # a stalled mirror: try another
                                    raise TimeoutError(f"{got / dt / 1e3:.0f} kB/s")
            except (netfetch.Offline, netfetch.PolicyDenied, netfetch.BudgetExceeded):
                raise                   # asking again will not help
            except (OSError, http.client.HTTPException) as e:   # URLError, timeouts, resets, netfetch
                log(f"gravity: download attempt {i + 1} interrupted ({e}); resuming")
                # the host's own wait (Retry-After, circuit open) if longer, up to a minute
                time.sleep(max(min(2 ** i, 10), min(getattr(e, "retry_after", 0.0) or 0.0, 60.0)))
                continue
        if os.path.getsize(part) < size:   # the mirror closed the connection early
            log(f"gravity: download attempt {i + 1} ended short; resuming")
            continue
        if _sha256(part) == sha256:
            os.replace(part, dest)
            return
        log("gravity: checksum mismatch; starting over")
        os.remove(part)
    raise RuntimeError(f"gravity: could not download {url}")


def _col_to_row(cf, nmax, mmax, sine):
    """Column-major (m, then n = m..nmax; sine sets start at m = 1) -> row-major full triangle."""
    L = tri_len(nmax)
    n_f = np.repeat(np.arange(nmax + 1), np.arange(1, nmax + 2))
    m_f = np.arange(L) - n_f * (n_f + 1) // 2
    ok = (m_f <= mmax) & (m_f >= (1 if sine else 0))
    n, m = n_f[ok], m_f[ok]
    j = m - 1 if sine else m                 # column number in the file
    start = j * (nmax + 1) - j * (j - 1) // 2 - (j if sine else 0)
    out = np.zeros(L)
    out[ok] = cf[start + (n - m)]
    return out


def read_cof(fileobj):
    """GeographicLib .egm.cof: 8-byte id, then (N, M, C, S) for the potential and for the
    zeta-to-N correction. Returns dict of row-major triangles."""
    ident = fileobj.read(8)
    sets = []
    for _ in range(2):
        nmax, mmax = struct.unpack("<ii", fileobj.read(8))
        nc = (mmax + 1) * (2 * nmax - mmax + 2) // 2
        ns = mmax * (2 * nmax - mmax + 1) // 2
        cf = np.frombuffer(fileobj.read(8 * nc), dtype="<f8")
        sf = np.frombuffer(fileobj.read(8 * ns), dtype="<f8")
        sets.append((nmax, _col_to_row(cf, nmax, mmax, False), _col_to_row(sf, nmax, mmax, True)))
    return {"id": ident.decode("ascii"), "nmax": sets[0][0], "c": sets[0][1], "s": sets[0][2],
            "corr_nmax": sets[1][0], "corr_c": sets[1][1], "corr_s": sets[1][2]}


def ensure_egm2008(cache_dir=CACHE_DIR, log=print, net=None):
    """Path of the converted coefficient cache, downloading (through net, a netfetch.Fetcher) and
    converting on first use."""
    os.makedirs(cache_dir, exist_ok=True)
    npy = os.path.join(cache_dir, "egm2008_rowmajor.npy")
    meta = os.path.join(cache_dir, "egm2008_rowmajor.json")
    if os.path.exists(npy) and os.path.exists(meta):
        return npy
    arc = os.path.join(cache_dir, "egm2008.tar.bz2")
    if not os.path.exists(arc):
        log(f"gravity: downloading EGM2008 coefficients (74 MB, once) to {cache_dir}")
        _download(EGM2008_URL, arc, EGM2008_SHA256, EGM2008_SIZE, log=log, net=net)
    with tarfile.open(arc, "r:bz2") as tf:
        member = tf.getmember("gravity/egm2008.egm.cof")
        d = read_cof(tf.extractfile(member))
    if d["id"] != "EGM2008A" or d["nmax"] != EGM2008_NMAX:
        raise RuntimeError("gravity: unexpected coefficient file")
    L = tri_len(EGM2008_NMAX)
    arr = np.zeros((4, L))
    arr[0], arr[1] = d["c"], d["s"]
    lc = tri_len(d["corr_nmax"])
    arr[2, :lc], arr[3, :lc] = d["corr_c"], d["corr_s"]
    np.save(npy + ".tmp.npy", arr)
    os.replace(npy + ".tmp.npy", npy)
    with open(meta, "w") as f:
        json.dump({"source": EGM2008_URL, "sha256": EGM2008_SHA256, "nmax": EGM2008_NMAX,
                   "corr_nmax": d["corr_nmax"], "rows": ["c", "s", "corr_c", "corr_s"],
                   "layout": "row-major triangle, index n(n+1)/2+m, fully normalised, tide-free",
                   "gm": EGM2008_GM, "a": EGM2008_A, "height_offset": EGM2008_HEIGHT_OFFSET}, f)
    return npy


# The built model's tables kept on disk and memory-mapped (load_egm2008): kept in memory they were ~128 MB of the
# server's main process for a value asked every 2 km (and building them peaked at ~380 MB); mapped, they are file pages
# the system may drop and read again. DERIVED_V: raise it when GravityModel's tables change.
DERIVED_V = 1
_BIG = ("coef", "ra", "rb")
_SMALL = ("zonal", "log_sect", "sect_next", "off")
_SCALARS = ("nmax", "gm", "a", "name", "height_offset", "check_every")


def _derived_dir(cache_dir, nmax, dtype):
    return os.path.join(cache_dir, f"egm2008_model_v{DERIVED_V}_{nmax}_{np.dtype(dtype).name}")


def _save_derived(model, d):
    tmp = f"{d}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(tmp, exist_ok=True)
        for k in _BIG + _SMALL:
            np.save(os.path.join(tmp, k + ".npy"), getattr(model, k))
        with open(os.path.join(tmp, "meta.json"), "w") as f:
            json.dump({k: getattr(model, k) for k in _SCALARS} | {"dtype": model.dtype.name}, f)
        if os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        os.rename(tmp, d)
    except OSError:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _load_derived(d):
    """The model from _save_derived's folder (big tables memory-mapped), or None."""
    try:
        with open(os.path.join(d, "meta.json")) as f:
            meta = json.load(f)
        model = GravityModel.__new__(GravityModel)
        for k in _SCALARS:
            setattr(model, k, meta[k])
        model.dtype = np.dtype(meta["dtype"])
        for k in _BIG:
            setattr(model, k, np.load(os.path.join(d, k + ".npy"), mmap_mode="r"))
        for k in _SMALL:
            setattr(model, k, np.load(os.path.join(d, k + ".npy")))
        if model.coef.shape[0] != tri_len(model.nmax) or model.coef.dtype != model.dtype:
            return None
        return model
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_egm2008(nmax=DEFAULT_NMAX, cache_dir=CACHE_DIR, log=print, mapped=True, **kw):
    """EGM2008 truncated to degree ``nmax`` (<= 2190); mapped: its tables memory-mapped from the cache (built and
    written there the first time)."""
    dtype = kw.get("dtype", np.float32)
    d = _derived_dir(cache_dir, min(int(nmax), EGM2008_NMAX), dtype)
    if mapped and set(kw) <= {"dtype"}:
        model = _load_derived(d)
        if model is not None:
            return model
    arr = np.asarray(np.load(ensure_egm2008(cache_dir, log=log), mmap_mode="r"))   # read as needed (a plain view)
    L = tri_len(min(nmax, EGM2008_NMAX))
    model = GravityModel(arr[0, :L], arr[1, :L], nmax=nmax, corr_c=arr[2, :L], corr_s=arr[3, :L],
                         name=f"EGM2008/{nmax}", **kw)
    del arr
    if mapped and set(kw) <= {"dtype"}:
        _save_derived(model, d)
        mm = _load_derived(d)
        if mm is not None:
            return mm           # (the built tables go: the mapped ones are the same bytes)
    return model


_default = None
_default_lock = threading.Lock()


def default_model():
    global _default
    with _default_lock:
        if _default is None:
            _default = load_egm2008(int(os.environ.get("BNGE_GRAVITY_NMAX", DEFAULT_NMAX)))
        return _default


def gravity(lat, lon, h=0.0, msl=False, model=None):
    """Total gravity at (lat, lon, h): a GravityField; ``.g`` is the magnitude in m/s^2."""
    return (model or default_model()).field(lat, lon, h, msl=msl)


def anomaly(lat, lon, h=0.0, msl=False, model=None):
    """Free-air gravity anomaly at the point, m/s^2 (x 1e5 for mGal)."""
    return gravity(lat, lon, h, msl=msl, model=model).anomaly


def geoid_height(lat, lon, model=None):
    """EGM2008 geoid height N above the WGS84 ellipsoid, m (h_ellipsoid = H_msl + N)."""
    return gravity(lat, lon, 0.0, model=model).geoid


# ================================================================================================
# earthd: the 'gravity' message (the player's real g, sent to the mod's weather.lua)
# ================================================================================================

MSG_VERSION = 1             # gravity.v: the message layout
RECOMPUTE_M = 2000.0        # a new value once the player moved this far from the last one ...
RECOMPUTE_DH = 200.0        # ... or this much up or down (the mod follows the free-air gradient between)
DOWNLOAD_RETRY_S = 600.0    # after a failed EGM2008 download


def egm2008_cached(cache_dir=CACHE_DIR):
    """Are the converted EGM2008 coefficients on disk (no download, no conversion needed)?"""
    return all(os.path.exists(os.path.join(cache_dir, n)) for n in ("egm2008_rowmajor.npy", "egm2008_rowmajor.json"))


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in m (spherical Earth; plenty for the recompute rule)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (dnp.sq(dmath.sin((p2 - p1) / 2))
         + dmath.cos(p1) * dmath.cos(p2) * dnp.sq(dmath.sin(math.radians(lon2 - lon1) / 2)))
    return 2 * 6371008.8 * dmath.asin(min(1.0, math.sqrt(a)))


class Provider:
    """earthd's gravity source, shared by every connection (--gravity on).

    message() blocks (45 ms with EGM2008): run it on ``pool``, never on the event loop. It uses
    EGM2008 (normal + anomaly at the point, MSL height) once the converted coefficients are on disk,
    loading them on first use; until then the normal formula, while a daemon thread downloads and
    converts them (74 MB, once; a failure is retried after DOWNLOAD_RETRY_S). Nothing here waits
    for the network. ``model``: a ready GravityModel (tests); ``download=False``: never fetch;
    ``net``: earthd's netfetch.Fetcher (None = the process-wide one; --offline: no download)."""

    def __init__(self, cache_dir=CACHE_DIR, model=None, nmax=None, download=True, log=None, net=None):
        import logging
        from runtimeprofiler import ThreadPoolExecutor
        self.cache_dir = cache_dir
        self.nmax = nmax or int(os.environ.get("BNGE_GRAVITY_NMAX", DEFAULT_NMAX))
        self.model = model
        self.download = download
        self.net = net
        self.log = log or logging.getLogger("earthd.gravity")
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="gravity")
        self.lock = threading.Lock()
        self.fetcher = None             # the download thread while it runs
        self.failed_at = None           # monotonic time of the last failed download
        self.broken = False             # the cached coefficients could not be loaded: normal from now on

    def source(self):
        """'egm2008' when the next message() uses the model, else 'normal'."""
        if self.model is not None:
            return "egm2008"
        return "egm2008" if not self.broken and egm2008_cached(self.cache_dir) else "normal"

    def _fetch(self):
        try:
            ensure_egm2008(self.cache_dir, log=self.log.info, net=self.net)
            self.log.info("gravity: EGM2008 ready; real gravity from the next update")
        except Exception as e:      # network, checksum, disk: the normal formula stays
            self.log.warning("gravity: EGM2008 download failed (%s: %s); normal gravity meanwhile", type(e).__name__, e)
            with self.lock:
                self.failed_at = time.monotonic()
        finally:
            with self.lock:
                self.fetcher = None

    def _start_download(self):
        with self.lock:
            if not self.download or self.fetcher is not None or (self.net is not None and self.net.offline_flag):
                return
            if self.failed_at is not None and time.monotonic() - self.failed_at < DOWNLOAD_RETRY_S:
                return
            self.fetcher = threading.Thread(target=self._fetch, name="gravity-download", daemon=True)
            self.fetcher.start()

    def _get_model(self):
        if self.model is None and not self.broken:
            if egm2008_cached(self.cache_dir):
                try:
                    self.model = load_egm2008(self.nmax, cache_dir=self.cache_dir, log=self.log.info)
                except Exception as e:      # a damaged cache: say so once, keep serving normal gravity
                    self.broken = True
                    self.log.warning("gravity: EGM2008 cache unusable (%s: %s); normal gravity", type(e).__name__, e)
            else:
                self._start_download()
        return self.model

    def message(self, lat, lon, h):
        """The 'gravity' message at geodetic (lat, lon), h m above sea level (blocking)."""
        model = self._get_model()
        if model is not None:
            g, src = model.field(lat, lon, h, msl=True).g, "egm2008"
        else:
            g, src = float(normal_gravity(lat, h)), "normal"
        return {"type": "gravity", "v": MSG_VERSION, "g": round(float(g), 7), "lat": round(float(lat), 6),
                "lon": round(float(lon), 6), "h": round(float(h), 1), "source": src}


class Tracker:
    """Per connection: when to recompute (moved > RECOMPUTE_M, height changed > RECOMPUTE_DH, the
    first pose, or EGM2008 became available) and the last message sent (re-sent on hello)."""

    def __init__(self):
        self.last = None            # (lat, lon, h, source) of the last message sent
        self.inflight = False
        self.msg = None

    def due(self, prov, lat, lon, h):
        if self.inflight:
            return False
        if self.last is None:
            return True
        llat, llon, lh, src = self.last
        return (src != prov.source() or abs(h - lh) > RECOMPUTE_DH
                or distance_m(llat, llon, lat, lon) > RECOMPUTE_M)

    def on_pose(self, conn, frame, x, y, z):
        """earthd Connection.on_pose: the player at (x, y, z) in its anchor's frame (z above sea
        level at the anchor; enu_to_geodetic adds the curvature). Computes on the provider's
        thread when due; the message goes to the mod when it is ready."""
        prov = conn.s.gravity
        if prov is None:
            return
        lat, lon, h = (float(v) for v in frame.enu_to_geodetic(x, y, z))
        if not (math.isfinite(lat) and math.isfinite(lon) and math.isfinite(h)) or not self.due(prov, lat, lon, h):
            return
        self.inflight = True

        async def run():
            msg = None
            try:
                msg = await conn.loop.run_in_executor(prov.pool, prov.message, lat, lon, h)
            except Exception as e:      # a bug, not the network: keep the last value
                prov.log.warning("gravity at %.4f, %.4f failed: %s: %s", lat, lon, type(e).__name__, e)
            finally:
                self.inflight = False
            if msg is not None:
                self.last, self.msg = (lat, lon, h, msg["source"]), msg
                conn.send(msg)
        conn.spawn(run())


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Real gravity (WGS84 normal + EGM2008) at a point")
    ap.add_argument("lat", type=float, help="geodetic latitude, degrees")
    ap.add_argument("lon", type=float, help="longitude, degrees")
    ap.add_argument("h", type=float, nargs="?", default=0.0, help="height, m (ellipsoidal)")
    ap.add_argument("--msl", action="store_true", help="h is above mean sea level (the geoid)")
    ap.add_argument("--nmax", type=int, default=DEFAULT_NMAX, help="truncation degree (<= 2190)")
    ap.add_argument("--coefficients", help="an .npz model (the test fixture format) instead of EGM2008")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()
    if args.coefficients:
        model = GravityModel.from_npz(args.coefficients, nmax=args.nmax)
    else:
        model = load_egm2008(args.nmax, log=lambda s: print(s, file=sys.stderr))
    t1 = time.perf_counter()
    f = model.field(args.lat, args.lon, args.h, msl=args.msl)
    t2 = time.perf_counter()
    print(f"point        {args.lat:.6f} {args.lon:.6f}  h {f.h:.2f} m ellipsoidal = "
          f"{f.h - f.geoid:.2f} m MSL + geoid N {f.geoid:.3f} m")
    print(f"gravity      {f.g:.7f} m/s^2   ENU vector {f.east:+.7f} {f.north:+.7f} {f.up:+.7f}")
    print(f"normal       {f.normal:.7f} m/s^2   (Somigliana + 2nd-order free air: "
          f"{float(normal_gravity(args.lat, f.h)):.7f})")
    print(f"disturbance  {f.disturbance / MGAL:+.2f} mGal   (g - normal)")
    print(f"anomaly      {f.anomaly / MGAL:+.2f} mGal   (free air)")
    print(f"deflection   xi {f.xi:+.2f}\"  eta {f.eta:+.2f}\"")
    print(f"model        {model.name}  load {1e3 * (t1 - t0):.0f} ms  point {1e3 * (t2 - t1):.1f} ms",
          file=sys.stderr)


if __name__ == "__main__":
    main()
