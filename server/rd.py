"""Rijksdriehoek (RD New, EPSG:28992) and WGS84.

The Kadaster approximation: centimetres inside the Netherlands, and the same formulas the Dutch
feeds publish their coordinates in. A number in, a number out. An array of points in, an array
out, so a tile converts in one call.

    x, y = wgs_to_rd(lat, lon)
    lat, lon = rd_to_wgs(x, y)
    x0, y0, x1, y1 = bounds(south, west, north, east)
"""
import numpy as np

# Amersfoort. dX, dY are the offset from here in 10 km; dphi, dlam in 0.36 degrees.
_X0, _Y0 = 155000.0, 463000.0
_PHI0, _LAM0 = 52.15517440, 5.38720621

# (power of dX, power of dY, coefficient), seconds of arc. The published reverse series.
_RD_LAT = ((0, 1, 3235.65389), (2, 0, -32.58297), (0, 2, -0.24750), (2, 1, -0.84978), (0, 3, -0.06550),
           (2, 2, -0.01709), (1, 0, -0.00738), (4, 0, 0.00530), (2, 3, -0.00039), (4, 1, 0.00033), (1, 1, -0.00012))
_RD_LON = ((1, 0, 5260.52916), (1, 1, 105.94684), (1, 2, 2.45656), (3, 0, -0.81885), (1, 3, 0.05594),
           (3, 1, -0.05607), (0, 1, 0.01199), (3, 2, -0.00256), (1, 4, 0.00128), (0, 2, 0.00022),
           (2, 0, -0.00022), (5, 0, 0.00026))

# (power of dphi, power of dlam, coefficient), metres. The published forward series.
_WGS_X = ((0, 1, 190094.945), (1, 1, -11832.228), (2, 1, -114.221), (0, 3, -32.391), (1, 0, -0.705),
          (3, 1, -2.340), (1, 3, -0.608), (0, 2, -0.008), (2, 3, 0.148))
_WGS_Y = ((1, 0, 309056.544), (0, 2, 3638.893), (2, 0, 73.077), (1, 2, -157.984), (3, 0, 59.788),
          (0, 1, 0.433), (2, 2, -6.439), (1, 1, -0.032), (0, 4, 0.092), (1, 4, -0.054))


def _pair(a, b):
    aa, bb = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return aa, bb, aa.ndim == 0 and bb.ndim == 0


def _series(u, v, terms):
    acc = np.zeros(np.broadcast(u, v).shape, np.float64)
    for p, q, k in terms:
        acc = acc + k * u ** p * v ** q
    return acc


def _out(x, y, scalar):
    if scalar:
        return float(x), float(y)
    return x, y


def wgs_to_rd(lat, lon):
    """RD New metres (x, y) of WGS84 lat, lon. Pure."""
    lat, lon, scalar = _pair(lat, lon)
    dp, dl = 0.36 * (lat - _PHI0), 0.36 * (lon - _LAM0)
    return _out(_X0 + _series(dp, dl, _WGS_X), _Y0 + _series(dp, dl, _WGS_Y), scalar)


def rd_to_wgs(x, y):
    """WGS84 (lat, lon) of RD New metres. Pure."""
    x, y, scalar = _pair(x, y)
    dx, dy = (x - _X0) * 1e-5, (y - _Y0) * 1e-5
    return _out(_PHI0 + _series(dx, dy, _RD_LAT) / 3600.0, _LAM0 + _series(dx, dy, _RD_LON) / 3600.0, scalar)


def bounds(south, west, north, east):
    """RD metres (x0, y0, x1, y1) covering the four corners of a WGS84 rectangle. Pure."""
    x, y = wgs_to_rd((south, south, north, north), (west, east, west, east))
    return float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))
