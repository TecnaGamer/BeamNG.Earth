"""WGS84 geodesy in float64: geodetic <-> ECEF <-> local ENU.

All functions take scalars or numpy arrays (degrees for lat/lon, metres for the
rest) and broadcast like numpy ufuncs.
"""
import math

import numpy as np

import fast     # the conversions are Rust kernels (earthfast): ~2x numpy, without the interpreter lock
import dmath
import dnp

A = 6378137.0                    # semi-major axis
F = 1 / 298.257223563            # flattening
B = A * (1 - F)                  # semi-minor axis
E2 = F * (2 - F)                 # first eccentricity squared
EP2 = E2 / (1 - E2)              # second eccentricity squared


_ZERO = np.zeros(3)
_EYE = np.eye(3)


def _rust3(kernel, a, b, c, origin, rot):
    """kernel (earthfast's geodetic_to_enu / enu_to_geodetic) on broadcast scalars or arrays, shapes kept."""
    fast.require(194, "geodesy")
    f64 = np.float64
    if (type(a) is np.ndarray and type(b) is np.ndarray and a.ndim == 1 and a.dtype == f64 and b.dtype == f64
            and a.shape == b.shape and type(c) is float):
        # (the usual call: two point arrays and one height for all: the kernel takes the one value, no broadcast and
        # no array of it; the same numbers. Most calls are a few points, their cost the wrapping: 2026-11-10)
        return getattr(fast._ef, kernel + "_c")(a, b, c, origin, rot)
    A_, B_, C_ = np.broadcast_arrays(np.asarray(a, f64), np.asarray(b, f64), np.asarray(c, f64))
    shape = A_.shape
    got = getattr(fast._ef, kernel)(np.ascontiguousarray(A_.ravel()), np.ascontiguousarray(B_.ravel()),
                                    np.ascontiguousarray(C_.ravel()), origin, rot)
    if shape == ():
        return tuple(f64(v[0]) for v in got)
    return tuple(v.reshape(shape) for v in got)


def geodetic_to_ecef(lat, lon, h=0.0):
    return _rust3("geodetic_to_enu", lat, lon, h, _ZERO, _EYE)


def ecef_to_geodetic(x, y, z):
    """Heikkinen's closed form (exact, no iteration; the polar axis handled). Returns (lat, lon, h)."""
    return _rust3("enu_to_geodetic", x, y, z, _ZERO, _EYE)


def segment_lengths(lonlat):
    """WGS84 geodesic metres between consecutive lon/lat vertices (Vincenty's inverse; the mean sphere where it does not
    converge, near antipodal: no road edge), the metric lanesections.py locates a merged highway's per-source lane
    stretches in (from PR #880 by DerEifrige-f; earthfast.geodesic_lengths)."""
    import fast
    ll = np.ascontiguousarray(np.asarray(lonlat, np.float64)[:, :2])
    if len(ll) < 2:
        return np.empty(0, np.float64)
    fast.require(233, "geodesic_lengths")
    return fast._ef.geodesic_lengths(ll)


class EnuFrame:
    """Local East-North-Up frame tangent to the ellipsoid at (lat0, lon0, h0)."""

    def __init__(self, lat0, lon0, h0=0.0):
        self.lat0, self.lon0, self.h0 = float(lat0), float(lon0), float(h0)
        self.origin = np.array(geodetic_to_ecef(lat0, lon0, h0), np.float64)
        la, lo = np.radians(lat0), np.radians(lon0)
        sla, cla, slo, clo = dnp.sin(la), dnp.cos(la), dnp.sin(lo), dnp.cos(lo)
        # Rows are the E, N, U unit vectors expressed in ECEF.
        self.rot = np.array([
            [-slo, clo, 0.0],
            [-sla * clo, -sla * slo, cla],
            [cla * clo, cla * slo, sla],
        ])

    def enu_to_ecef(self, e, n, u=0.0):
        r = self.rot
        o = self.origin
        return (o[0] + r[0, 0] * e + r[1, 0] * n + r[2, 0] * u,
                o[1] + r[0, 1] * e + r[1, 1] * n + r[2, 1] * u,
                o[2] + r[0, 2] * e + r[1, 2] * n + r[2, 2] * u)

    def ecef_to_enu(self, x, y, z):
        dx, dy, dz = x - self.origin[0], y - self.origin[1], z - self.origin[2]
        r = self.rot
        return (r[0, 0] * dx + r[0, 1] * dy + r[0, 2] * dz,
                r[1, 0] * dx + r[1, 1] * dy + r[1, 2] * dz,
                r[2, 0] * dx + r[2, 1] * dy + r[2, 2] * dz)

    def enu_to_geodetic(self, e, n, u=0.0):
        return _rust3("enu_to_geodetic", e, n, u, self.origin, self.rot)

    def geodetic_to_enu(self, lat, lon, h=0.0):
        return _rust3("geodetic_to_enu", lat, lon, h, self.origin, self.rot)

    def surface_enu(self, e, n, elev_fn):
        """Spec steps 1-5: drop the plane point (e, n, 0) onto the DEM surface.

        Returns (lat, lon, elev, E, N, U): the geodetic position under the plane
        point, its DEM elevation, and the true ENU position of the surface point.
        """
        lat, lon, _ = self.enu_to_geodetic(e, n, 0.0)
        elev = elev_fn(lat, lon)
        return (lat, lon, elev) + self.geodetic_to_enu(lat, lon, elev)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance (m) on the WGS84 mean sphere between two points in degrees."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = dnp.sq(dmath.sin(dp / 2)) + dmath.cos(p1) * dmath.cos(p2) * dnp.sq(dmath.sin(dl / 2))
    return 2 * 6371008.8 * dmath.asin(min(1.0, math.sqrt(a)))
