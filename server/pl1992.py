"""EPSG:2180 (ETRF2000-PL / CS92) <-> WGS84 lon/lat, pure numpy (no pyproj).
Transverse Mercator on GRS80: lon0 19E, k0 0.9993, FE 500000, FN -5300000. Kruger series, mm-level."""
import numpy as np

_A, _F = 6378137.0, 1 / 298.257222101
_N = _F / (2 - _F)
_K0, _LON0, _FE, _FN = 0.9993, np.radians(19.0), 500000.0, -5300000.0
_AA = _A / (1 + _N) * (1 + _N**2 / 4 + _N**4 / 64)
_ALPHA = (_N / 2 - 2 * _N**2 / 3 + 5 * _N**3 / 16 + 41 * _N**4 / 180,
          13 * _N**2 / 48 - 3 * _N**3 / 5 + 557 * _N**4 / 1440,
          61 * _N**3 / 240 - 103 * _N**4 / 140,
          49561 * _N**4 / 161280)
_BETA = (_N / 2 - 2 * _N**2 / 3 + 37 * _N**3 / 96 - _N**4 / 360,
         _N**2 / 48 + _N**3 / 15 - 437 * _N**4 / 1440,
         17 * _N**3 / 480 - 37 * _N**4 / 840,
         4397 * _N**4 / 161280)
_E = np.sqrt(_F * (2 - _F))


def to_lonlat(x, y):
    """x easting, y northing (metres, EPSG:2180 as GUGiK files: x east) -> (lon, lat) degrees."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    xi = (y - _FN) / (_K0 * _AA)
    eta = (x - _FE) / (_K0 * _AA)
    xi_p, eta_p = xi.copy(), eta.copy()
    for j, b in enumerate(_BETA, 1):
        xi_p -= b * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
        eta_p -= b * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
    chi = np.arcsin(np.sin(xi_p) / np.cosh(eta_p))
    lat = chi.copy()
    for j, d in enumerate((_N*2 - 2*_N**2/3 - 2*_N**3 + 116*_N**4/45, 7*_N**2/3 - 8*_N**3/5 - 227*_N**4/45,
                           56*_N**3/15 - 136*_N**4/35, 4279*_N**4/630), 1):
        lat += d * np.sin(2 * j * chi)
    lon = _LON0 + np.arctan2(np.sinh(eta_p), np.cos(xi_p))
    return np.degrees(lon), np.degrees(lat)


def from_lonlat(lon, lat):
    lon, lat = np.radians(np.asarray(lon, np.float64)), np.radians(np.asarray(lat, np.float64))
    t = np.sinh(np.arctanh(np.sin(lat)) - _E * np.arctanh(_E * np.sin(lat)))
    dl = lon - _LON0
    xi_p = np.arctan2(t, np.cos(dl))
    eta_p = np.arctanh(np.sin(dl) / np.sqrt(1 + t * t))
    xi, eta = xi_p.copy(), eta_p.copy()
    for j, a in enumerate(_ALPHA, 1):
        xi += a * np.sin(2 * j * xi_p) * np.cosh(2 * j * eta_p)
        eta += a * np.cos(2 * j * xi_p) * np.sinh(2 * j * eta_p)
    return _FE + _K0 * _AA * eta, _FN + _K0 * _AA * xi
