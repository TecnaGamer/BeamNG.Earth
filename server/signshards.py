"""The highway sign shards' format, shared by their builder (tools/build_sign_tags.py) and their reader
(railtags.SignTags). Pure (numpy only: the builder runs in BeamNG.Earth-data's GitHub Action with a few files).

A shard is the z7 tile (ZOOM, as the rail and lane shards) of its records' points: {"objects": [{"id", "k", "lon",
"lat", "d"}]}, "k" one of KINDS, "d" the sign module's record (exitsigns / throughsigns / milemarkers), its PATHS
delta-coded (encode_path).
"""
import math

import numpy as np
import dmath

ZOOM = 7
FORMAT = 1
KINDS = {"exit": "exit", "through": "through", "mile": "mile_marker"}     # shard "k" -> the sign modules' key
PATHS = ("approach", "ramp", "de_approach", "de_ramp")   # [[lon, lat], ...] in "d", stored as encode_path's integers
DE_VERSION = 1                      # the German raw block "de" of an exit record inside DE_OUTLINE

# Germany, grown by about 3 km and traced from the border data (countrybounds' country_bands.bin) in 231 points:
# the records whose junction lies inside carry the German block (the server still asks the real border). lon, lat.
DE_OUTLINE = [np.array([
    (10.170, 47.240), (10.365, 47.280), (10.470, 47.360), (10.515, 47.430), (10.485, 47.510), (10.665, 47.530), (10.770,
    47.480), (10.830, 47.500), (10.995, 47.360), (11.175, 47.400), (11.280, 47.370), (11.445, 47.430), (11.445, 47.480),
    (11.610, 47.490), (11.640, 47.560), (11.850, 47.550), (11.940, 47.590), (12.195, 47.570), (12.255, 47.610), (12.240,
    47.660), (12.405, 47.660), (12.495, 47.600), (12.720, 47.650), (12.765, 47.530), (12.960, 47.440), (13.050, 47.450),
    (13.140, 47.660), (13.065, 47.740), (12.975, 47.740), (13.050, 47.860), (12.795, 48.100), (13.050, 48.240), (13.365,
    48.300), (13.470, 48.380), (13.515, 48.550), (13.740, 48.490), (13.875, 48.620), (13.890, 48.770), (13.770, 48.910),
    (13.695, 48.910), (13.590, 49.000), (13.455, 48.990), (13.380, 49.110), (13.230, 49.140), (13.005, 49.360), (12.810,
    49.370), (12.690, 49.460), (12.690, 49.530), (12.615, 49.560), (12.555, 49.710), (12.465, 49.730), (12.600, 49.920),
    (12.510, 50.010), (12.345, 50.060), (12.240, 50.150), (12.360, 50.150), (12.390, 50.260), (12.540, 50.370), (12.705,
    50.370), (12.780, 50.420), (12.960, 50.380), (13.065, 50.470), (13.230, 50.480), (13.260, 50.550), (13.350, 50.560),
    (13.380, 50.600), (13.500, 50.580), (13.590, 50.680), (13.905, 50.710), (13.980, 50.790), (14.040, 50.770), (14.415,
    50.880), (14.445, 50.940), (14.355, 51.000), (14.535, 50.980), (14.520, 50.920), (14.595, 50.890), (14.610, 50.830),
    (14.715, 50.790), (14.835, 50.800), (15.045, 51.120), (15.090, 51.280), (15.015, 51.350), (15.015, 51.450), (14.985,
    51.490), (14.760, 51.550), (14.805, 51.670), (14.640, 51.820), (14.745, 51.900), (14.805, 52.060), (14.730, 52.120),
    (14.760, 52.240), (14.625, 52.300), (14.580, 52.390), (14.685, 52.490), (14.685, 52.580), (14.190, 52.850), (14.190,
    52.950), (14.400, 53.050), (14.415, 53.190), (14.490, 53.280), (14.310, 53.660), (14.220, 54.240), (14.115, 54.290),
    (14.085, 54.580), (13.950, 54.740), (13.530, 54.910), (13.185, 54.900), (12.285, 54.600), (12.255, 54.500), (12.150,
    54.410), (11.955, 54.360), (11.655, 54.360), (11.325, 54.560), (11.145, 54.610), (10.620, 54.540), (10.365, 54.610),
    (10.185, 54.770), (9.915, 54.870), (9.735, 54.850), (9.585, 54.920), (9.480, 54.860), (9.375, 54.870), (9.285,
    54.830), (9.180, 54.900), (8.610, 54.940), (8.475, 55.090), (8.040, 55.130), (7.905, 54.950), (7.890, 54.670),
    (8.055, 54.410), (7.755, 54.420), (7.605, 54.370), (7.485, 54.260), (7.485, 54.160), (7.365, 54.140), (7.365,
    54.010), (7.410, 53.980), (6.735, 53.900), (6.345, 53.760), (6.540, 53.560), (6.900, 53.430), (6.960, 53.310),
    (7.155, 53.300), (7.170, 53.000), (7.020, 52.810), (7.020, 52.670), (6.735, 52.680), (6.675, 52.630), (6.675,
    52.460), (6.975, 52.420), (7.035, 52.370), (6.990, 52.250), (6.645, 52.070), (6.660, 52.020), (6.765, 51.990),
    (6.750, 51.940), (6.360, 51.880), (6.150, 51.940), (5.895, 51.830), (5.925, 51.720), (5.985, 51.710), (6.180,
    51.490), (6.180, 51.370), (6.030, 51.250), (6.045, 51.140), (5.820, 51.040), (5.865, 50.950), (5.955, 50.960),
    (6.030, 50.900), (5.940, 50.790), (6.000, 50.700), (6.090, 50.690), (6.180, 50.610), (6.135, 50.560), (6.165,
    50.500), (6.330, 50.440), (6.315, 50.350), (6.150, 50.260), (6.075, 50.090), (6.090, 49.990), (6.195, 49.880),
    (6.480, 49.770), (6.315, 49.580), (6.330, 49.470), (6.495, 49.420), (6.735, 49.130), (6.840, 49.120), (6.945,
    49.190), (7.065, 49.080), (7.110, 49.110), (7.305, 49.090), (7.425, 49.150), (7.515, 49.060), (7.635, 49.020),
    (7.935, 49.020), (8.145, 48.950), (8.070, 48.830), (7.935, 48.780), (7.920, 48.720), (7.800, 48.660), (7.695,
    48.330), (7.530, 48.130), (7.575, 47.960), (7.470, 47.660), (7.635, 47.510), (7.755, 47.510), (7.830, 47.550),
    (7.935, 47.510), (8.040, 47.520), (8.250, 47.590), (8.325, 47.540), (8.475, 47.540), (8.520, 47.580), (8.625,
    47.580), (8.685, 47.670), (8.835, 47.660), (8.880, 47.620), (9.030, 47.660), (9.345, 47.610), (9.465, 47.530),
    (9.600, 47.500), (9.690, 47.490), (9.780, 47.540), (9.855, 47.500), (9.945, 47.510), (9.960, 47.460), (10.050,
    47.440), (10.065, 47.340), (10.170, 47.340), (10.125, 47.290)
], np.float64)]
_DE_BOX = (min(float(r[:, 0].min()) for r in DE_OUTLINE), min(float(r[:, 1].min()) for r in DE_OUTLINE),
           max(float(r[:, 0].max()) for r in DE_OUTLINE), max(float(r[:, 1].max()) for r in DE_OUTLINE))


def tile(lat, lon):
    """The ZOOM tile (x, y) of (lat, lon) (web mercator, as build_rail_tags.cell_xy >> its shift)."""
    n = 1 << ZOOM
    lat = min(max(float(lat), -85.05112878), 85.05112878)
    r = math.radians(lat)
    x = int(math.floor((float(lon) + 180.0) / 360.0 * n))
    y = int(math.floor((1.0 - dmath.log(dmath.tan(r) + 1.0 / dmath.cos(r)) / math.pi) / 2.0 * n))
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(x, y):
    """(lon0, lat0, lon1, lat1) of ZOOM tile (x, y)."""
    n = 1 << ZOOM

    def lat(yy):
        return math.degrees(dmath.atan(dmath.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def encode_path(points, lon, lat):
    """[[lon, lat], ...] as a flat list of integers: 1e-6 degree steps, the first from the record's (lon, lat)."""
    q = np.round(np.asarray(points, np.float64).reshape(-1, 2) * 1e6).astype(np.int64)
    base = np.array([round(lon * 1e6), round(lat * 1e6)], np.int64)
    return np.diff(np.vstack([base, q]), axis=0).ravel().tolist()


def decode_path(flat, lon, lat):
    q = np.asarray(flat, np.int64).reshape(-1, 2).cumsum(axis=0) + [round(lon * 1e6), round(lat * 1e6)]
    return (q / 1e6).tolist()


def in_germany(lon, lat):
    """Bool array: inside DE_OUTLINE (even-odd, a bounding box first: the builder asks for every exit on the planet).
    Pure (numpy only), lon / lat order."""
    x, y = np.atleast_1d(np.asarray(lon, np.float64)), np.atleast_1d(np.asarray(lat, np.float64))
    out = np.zeros(x.shape, bool)
    box = (x >= _DE_BOX[0]) & (x <= _DE_BOX[2]) & (y >= _DE_BOX[1]) & (y <= _DE_BOX[3])
    if not box.any():
        return out
    xi, yi = x[box], y[box]
    hits = np.zeros(xi.shape, np.int64)
    for ring in DE_OUTLINE:
        a, b = ring, np.roll(ring, -1, axis=0)
        crosses = (a[:, 1, None] > yi) != (b[:, 1, None] > yi)
        with np.errstate(divide="ignore", invalid="ignore"):
            at = a[:, 0, None] + (yi - a[:, 1, None]) * (b[:, 0, None] - a[:, 0, None]) / (b[:, 1, None] - a[:, 1, None])
        hits += (crosses & (xi < at)).sum(0)
    out[box] = hits % 2 == 1
    return out
