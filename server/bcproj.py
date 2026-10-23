"""Forward map projections of British Columbia's and Canada's elevation data, pure numpy (no PROJ in the app).

    x, y = forward(epsg, lat, lon)      # WGS84 degrees (arrays) -> the CRS's easting / northing (m)

Supported: UTM zones 8-11 on NAD83(CSRS) / NAD83 (EPSG 3155-3158, 26908-26911; with CGVD2013 heights: 6651-6654)
and WGS84 (32608-32611),
BC Albers (3005, 3153), Canada Atlas Lambert (3979). All on GRS80. The NAD83 datums are taken as WGS84 (they differ
by ~1-2 m on the BC coast). UTM: Krueger's series to n^6 (Karney 2011; sub-millimetre in the zone); Albers and the
Lambert conic: Snyder's ellipsoidal formulas. Checked against PROJ in tests/test_bcproj.py.
"""
import math

import numpy as np

A = 6378137.0
F = 1 / 298.257222101
E2 = F * (2 - F)
E = math.sqrt(E2)

UTM_ZONE = {3155: 8, 3156: 9, 3157: 10, 3158: 11, 26908: 8, 26909: 9, 26910: 10, 26911: 11,
            6651: 8, 6652: 9, 6653: 10, 6654: 11,
            32608: 8, 32609: 9, 32610: 10, 32611: 11}     # WGS84 UTM (WGS84 vs GRS80 ellipsoid: < 0.1 mm here)
ALBERS = {3005, 3153}
LAMBERT = {3979}

# --- Transverse Mercator (Krueger series) -------------------------------------------------------------------------
_n = F / (2 - F)
_A1 = A / (1 + _n) * (1 + _n ** 2 / 4 + _n ** 4 / 64 + _n ** 6 / 256)
_ALPHA = (
    _n / 2 - 2 * _n ** 2 / 3 + 5 * _n ** 3 / 16 + 41 * _n ** 4 / 180 - 127 * _n ** 5 / 288 + 7891 * _n ** 6 / 37800,
    13 * _n ** 2 / 48 - 3 * _n ** 3 / 5 + 557 * _n ** 4 / 1440 + 281 * _n ** 5 / 630 - 1983433 * _n ** 6 / 1935360,
    61 * _n ** 3 / 240 - 103 * _n ** 4 / 140 + 15061 * _n ** 5 / 26880 + 167603 * _n ** 6 / 181440,
    49561 * _n ** 4 / 161280 - 179 * _n ** 5 / 168 + 6601661 * _n ** 6 / 7257600,
    34729 * _n ** 5 / 80640 - 3418889 * _n ** 6 / 1995840,
    212378941 * _n ** 6 / 319334400,
)


def utm(lat, lon, zone, k0=0.9996, fe=500000.0, fn=0.0):
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64) - (zone * 6 - 183))
    t = np.sinh(np.arctanh(np.sin(phi)) - E * np.arctanh(E * np.sin(phi)))
    xi = np.arctan2(t, np.cos(lam))
    eta = np.arctanh(np.sin(lam) / np.sqrt(1 + t * t))
    x, y = eta.copy(), xi.copy()
    for j, a in enumerate(_ALPHA, 1):
        x += a * np.cos(2 * j * xi) * np.sinh(2 * j * eta)
        y += a * np.sin(2 * j * xi) * np.cosh(2 * j * eta)
    return fe + k0 * _A1 * x, fn + k0 * _A1 * y


# --- Albers equal-area conic (BC Albers) --------------------------------------------------------------------------
def _q(phi):
    s = np.sin(phi)
    return (1 - E2) * (s / (1 - E2 * s * s) - (1 / (2 * E)) * np.log((1 - E * s) / (1 + E * s)))


def _m(phi):
    return np.cos(phi) / np.sqrt(1 - E2 * np.sin(phi) ** 2)


def albers(lat, lon, lat1=50.0, lat2=58.5, lat0=45.0, lon0=-126.0, fe=1000000.0, fn=0.0):
    p1, p2, p0 = map(math.radians, (lat1, lat2, lat0))
    m1, m2 = float(_m(p1)), float(_m(p2))
    q1, q2, q0 = float(_q(p1)), float(_q(p2)), float(_q(p0))
    n = (m1 * m1 - m2 * m2) / (q2 - q1)
    c = m1 * m1 + n * q1
    r0 = A * math.sqrt(c - n * q0) / n
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64))
    r = A * np.sqrt(c - n * _q(phi)) / n
    th = n * (lam - math.radians(lon0))
    return fe + r * np.sin(th), fn + r0 - r * np.cos(th)


# --- Lambert conformal conic, 2 standard parallels (Canada Atlas Lambert) ----------------------------------------
def _t(phi):
    s = np.sin(phi)
    return np.tan(np.pi / 4 - phi / 2) / ((1 - E * s) / (1 + E * s)) ** (E / 2)


def lambert(lat, lon, lat1=49.0, lat2=77.0, lat0=49.0, lon0=-95.0, fe=0.0, fn=0.0):
    p1, p2, p0 = map(math.radians, (lat1, lat2, lat0))
    m1, m2 = float(_m(p1)), float(_m(p2))
    t1, t2, t0 = float(_t(p1)), float(_t(p2)), float(_t(p0))
    n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
    ff = m1 / (n * t1 ** n)
    r0 = A * ff * t0 ** n
    phi = np.radians(np.asarray(lat, np.float64))
    lam = np.radians(np.asarray(lon, np.float64))
    r = A * ff * _t(phi) ** n
    th = n * (lam - math.radians(lon0))
    return fe + r * np.sin(th), fn + r0 - r * np.cos(th)


def supported(epsg):
    return epsg in UTM_ZONE or epsg in ALBERS or epsg in LAMBERT


def forward(epsg, lat, lon):
    """(x, y) in EPSG:epsg of WGS84 lat / lon (degrees)."""
    if epsg in UTM_ZONE:
        return utm(lat, lon, UTM_ZONE[epsg])
    if epsg in ALBERS:
        return albers(lat, lon)
    if epsg in LAMBERT:
        return lambert(lat, lon)
    raise ValueError(f"unsupported CRS EPSG:{epsg}")


def forward_grid(epsg, lat, lon, step=16):
    """forward() of a 2-D grid of points (rows / columns of a regular lat / lon raster, any spacing) from every step-th
    row and column, bilinear in between: a few km of UTM / conic is linear to well under a millimetre over a step of
    ~20 m, and projecting 4 million canopy points one by one took ~6 s. Other shapes: forward()."""
    lat = np.asarray(lat, np.float64)
    lon = np.asarray(lon, np.float64)
    if lat.ndim != 2 or lat.shape != lon.shape or min(lat.shape) < 4 * step:
        return forward(epsg, lat, lon)
    h, w = lat.shape
    ri = np.unique(np.r_[np.arange(0, h, step), h - 1])
    ci = np.unique(np.r_[np.arange(0, w, step), w - 1])
    X, Y = forward(epsg, lat[np.ix_(ri, ci)], lon[np.ix_(ri, ci)])

    def interp(G):
        rows = np.empty((h, len(ci)))
        for j in range(len(ci)):
            rows[:, j] = np.interp(np.arange(h), ri, G[:, j])
        out = np.empty((h, w))
        cols = np.arange(w)
        for i in range(h):
            out[i] = np.interp(cols, ci, rows[i])
        return out
    return interp(X), interp(Y)
