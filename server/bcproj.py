"""Forward map projections of British Columbia's and Canada's elevation data, native kernels (no PROJ in the app).

    x, y = forward(epsg, lat, lon)      # WGS84 degrees (arrays) -> the CRS's easting / northing (m)

Supported: UTM zones 8-11 on NAD83(CSRS) / NAD83 (EPSG 3155-3158, 26908-26911; with CGVD2013 heights: 6651-6654)
and WGS84 (32608-32611),
BC Albers (3005, 3153), Canada Atlas Lambert (3979). All on GRS80. The NAD83 datums are taken as WGS84 (they differ
by ~1-2 m on the BC coast). UTM: Krueger's series to n^6 (Karney 2011; sub-millimetre in the zone); Albers and the
Lambert conic: Snyder's ellipsoidal formulas. Checked against PROJ in tests/test_bcproj.py.
"""
import math

import numpy as np
import fast
import dnp

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
_A1 = A / (1 + _n) * (1 + (_n * _n) / 4 + dnp.power(_n, 4) / 64 + dnp.power(_n, 6) / 256)
_ALPHA = (
    _n / 2 - 2 * (_n * _n) / 3 + 5 * dnp.power(_n, 3) / 16 + 41 * dnp.power(_n, 4) / 180 - 127 * dnp.power(_n, 5) / 288 + 7891 * dnp.power(_n, 6) / 37800,
    13 * (_n * _n) / 48 - 3 * dnp.power(_n, 3) / 5 + 557 * dnp.power(_n, 4) / 1440 + 281 * dnp.power(_n, 5) / 630 - 1983433 * dnp.power(_n, 6) / 1935360,
    61 * dnp.power(_n, 3) / 240 - 103 * dnp.power(_n, 4) / 140 + 15061 * dnp.power(_n, 5) / 26880 + 167603 * dnp.power(_n, 6) / 181440,
    49561 * dnp.power(_n, 4) / 161280 - 179 * dnp.power(_n, 5) / 168 + 6601661 * dnp.power(_n, 6) / 7257600,
    34729 * dnp.power(_n, 5) / 80640 - 3418889 * dnp.power(_n, 6) / 1995840,
    212378941 * dnp.power(_n, 6) / 319334400,
)


def _points(lat, lon):
    return np.broadcast_arrays(np.asarray(lat, np.float64), np.asarray(lon, np.float64))


def utm(lat, lon, zone, k0=0.9996, fe=500000.0, fn=0.0):
    fast.require(565, "BC UTM projection")
    x, y = fast._ef.dem_utm(*_points(lat, lon), zone * 6 - 183, k0 * _A1, fe, fn, E, _ALPHA)
    return x[()], y[()]


def _conic(lat, lon, parallels, origin, offset, albers=False, hrdem=False):
    fast.require(565, "Canadian conic projection")
    x, y = fast._ef.dem_conic(*_points(lat, lon), parallels, origin, offset, albers, hrdem)
    return x[()], y[()]


def albers(lat, lon, lat1=50.0, lat2=58.5, lat0=45.0, lon0=-126.0, fe=1000000.0, fn=0.0):
    return _conic(lat, lon, (lat1, lat2), (lat0, lon0), (fe, fn), albers=True)


def lambert(lat, lon, lat1=49.0, lat2=77.0, lat0=49.0, lon0=-95.0, fe=0.0, fn=0.0):
    return _conic(lat, lon, (lat1, lat2), (lat0, lon0), (fe, fn))


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

    fast.require(565, "regional DEM projection grid")
    return tuple(fast._ef.dem_projected_grid(g, ri, ci, h, w) for g in (X, Y))
