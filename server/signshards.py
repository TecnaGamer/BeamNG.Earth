"""The highway sign shards' format, shared by their builder (tools/build_sign_tags.py) and their reader
(railtags.SignTags). Pure (numpy only: the builder runs in BeamNG.Earth-data's GitHub Action with a few files).

A shard is the z7 tile (ZOOM, as the rail and lane shards) of its records' points: {"objects": [{"id", "k", "lon",
"lat", "d"}]}, "k" one of KINDS, "d" the sign module's record (exitsigns / throughsigns / milemarkers), its PATHS
delta-coded (encode_path).
"""
import math

import numpy as np

ZOOM = 7
FORMAT = 1
KINDS = {"exit": "exit", "through": "through", "mile": "mile_marker", "shield": "roadside"}
PATHS = ("approach", "ramp")        # [[lon, lat], ...] in "d", stored as encode_path's integers


def tile(lat, lon):
    """The ZOOM tile (x, y) of (lat, lon) (web mercator, as build_rail_tags.cell_xy >> its shift)."""
    n = 1 << ZOOM
    lat = min(max(float(lat), -85.05112878), 85.05112878)
    r = math.radians(lat)
    x = int(math.floor((float(lon) + 180.0) / 360.0 * n))
    y = int(math.floor((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n))
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(x, y):
    """(lon0, lat0, lon1, lat1) of ZOOM tile (x, y)."""
    n = 1 << ZOOM

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def encode_path(points, lon, lat):
    """[[lon, lat], ...] as a flat list of integers: 1e-6 degree steps, the first from the record's (lon, lat)."""
    q = np.round(np.asarray(points, np.float64).reshape(-1, 2) * 1e6).astype(np.int64)
    base = np.array([round(lon * 1e6), round(lat * 1e6)], np.int64)
    return np.diff(np.vstack([base, q]), axis=0).ravel().tolist()


def decode_path(flat, lon, lat):
    q = np.asarray(flat, np.int64).reshape(-1, 2).cumsum(axis=0) + [round(lon * 1e6), round(lat * 1e6)]
    return (q / 1e6).tolist()
