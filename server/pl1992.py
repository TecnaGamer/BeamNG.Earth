"""EPSG:2180 (ETRF2000-PL / CS92) <-> WGS84 lon/lat, pure numpy (no pyproj).
Transverse Mercator on GRS80: lon0 19E, k0 0.9993, FE 500000, FN -5300000. Kruger series, mm-level.

The transcendental functions are dnp's (the native module's libm-crate ones), not numpy's: these coordinates feed the
building meshes and the streets' widths and so the plans players share, and they must come out bit-identical on every
platform (GitHub #1169, tests/test_determinism.py). The series' powers of n are products, not `**` (the C library's pow).
"""
import numpy as np

import dnp

_A, _F = 6378137.0, 1 / 298.257222101
_N = _F / (2 - _F)
_N2 = _N * _N
_N3 = _N2 * _N
_N4 = _N2 * _N2
_K0, _LON0, _FE, _FN = 0.9993, np.radians(19.0), 500000.0, -5300000.0
_AA = _A / (1 + _N) * (1 + _N2 / 4 + _N4 / 64)
_ALPHA = (_N / 2 - 2 * _N2 / 3 + 5 * _N3 / 16 + 41 * _N4 / 180,
          13 * _N2 / 48 - 3 * _N3 / 5 + 557 * _N4 / 1440,
          61 * _N3 / 240 - 103 * _N4 / 140,
          49561 * _N4 / 161280)
_BETA = (_N / 2 - 2 * _N2 / 3 + 37 * _N3 / 96 - _N4 / 360,
         _N2 / 48 + _N3 / 15 - 437 * _N4 / 1440,
         17 * _N3 / 480 - 37 * _N4 / 840,
         4397 * _N4 / 161280)
_DELTA = (_N * 2 - 2 * _N2 / 3 - 2 * _N3 + 116 * _N4 / 45,
          7 * _N2 / 3 - 8 * _N3 / 5 - 227 * _N4 / 45,
          56 * _N3 / 15 - 136 * _N4 / 35,
          4279 * _N4 / 630)
_E = np.sqrt(_F * (2 - _F))


def to_lonlat(x, y):
    """x easting, y northing (metres, EPSG:2180 as GUGiK files: x east) -> (lon, lat) degrees."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    xi = (y - _FN) / (_K0 * _AA)
    eta = (x - _FE) / (_K0 * _AA)
    xi_p, eta_p = xi.copy(), eta.copy()
    for j, b in enumerate(_BETA, 1):
        xi_p -= b * dnp.sin(2 * j * xi) * dnp.cosh(2 * j * eta)
        eta_p -= b * dnp.cos(2 * j * xi) * dnp.sinh(2 * j * eta)
    chi = dnp.arcsin(dnp.sin(xi_p) / dnp.cosh(eta_p))
    lat = chi.copy()
    for j, d in enumerate(_DELTA, 1):
        lat += d * dnp.sin(2 * j * chi)
    lon = _LON0 + dnp.arctan2(dnp.sinh(eta_p), dnp.cos(xi_p))
    return np.degrees(lon), np.degrees(lat)


def from_lonlat(lon, lat):
    lon, lat = np.radians(np.asarray(lon, np.float64)), np.radians(np.asarray(lat, np.float64))
    s = dnp.sin(lat)
    t = dnp.sinh(dnp.arctanh(s) - _E * dnp.arctanh(_E * s))
    dl = lon - _LON0
    xi_p = dnp.arctan2(t, dnp.cos(dl))
    eta_p = dnp.arctanh(dnp.sin(dl) / np.sqrt(1 + t * t))
    xi, eta = xi_p.copy(), eta_p.copy()
    for j, a in enumerate(_ALPHA, 1):
        xi += a * dnp.sin(2 * j * xi_p) * dnp.cosh(2 * j * eta_p)
        eta += a * dnp.cos(2 * j * xi_p) * dnp.sinh(2 * j * eta_p)
    return _FE + _K0 * _AA * eta, _FN + _K0 * _AA * xi
