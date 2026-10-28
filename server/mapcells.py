"""The global data-cell grid of the map-data pipeline (docs/ARCHITECTURE.md "Map data").

Map data (roads, buildings, water, land use, land cover, canopy height) is fetched, cached and
handed out per *cell*: a Web-Mercator (EPSG:3857) XYZ tile at zoom ZOOM = 14, the same tiling the
imagery and elevation sources already use (USGS, EOX, Mapterhorn, AWS Terrain Tiles are XYZ
tiles), that the Meta/WRI canopy height files are cut in (quadkey level 10: one file holds exactly
16 x 16 cells), and that vector tile services use (z14 is their densest level). A cell key is
(z, x, y) with x east from -180 deg and y south from +85.0511 deg; it depends on nothing but a
position on the Earth (never on the anchor, the player or the load order), so the deterministic
content generators and v1.4 M6's global far tiles key on it directly: a tile of any shape asks
cells_for_bbox() for the cells under its footprint and merges their content by feature id.

Why z14 Web-Mercator and not a geodetic (lat/lon) grid or S2:
  * one source file never needs a partial cell on the canopy height side, and the imagery/DEM
    caches line up with it (a z14 cell = 16 x 16 z18 imagery tiles);
  * ~2.4 km at the equator, 1.9 km at 39 N, 1.2 km at 60 N: a few thousand buildings and road
    segments in a dense city, a few MB of compact arrays, one Overture row group or two per theme;
  * the area shrinks with cos(lat) like a geodetic grid's; S2 cells are more even in area but no
    data source is cut in them;
  * cells stop at +-85.0511 deg (MAX_LAT), as the imagery and the DEM do: no roads or buildings
    worth streaming lie beyond, and positions there have no cell (cell_of raises ValueError).

Cells never cross the antimeridian (x wraps modulo 2**z). The local frame of a cell is the ENU
tangent plane at its centre (geodesy.EnuFrame(lat_c, lon_c, 0)), in metres: east, north.
"""
import math
from typing import NamedTuple

import numpy as np

ZOOM = 14
MAX_LAT = 85.0511287798066          # Web-Mercator latitude limit (y in [0, 2**z))
EARTH_CIRC = 40075016.686           # m, equatorial circumference (Web-Mercator scale)


def _lat_of(yf, n):
    """Latitude of the fractional tile row yf (0 = north edge) at n tiles per axis."""
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yf / n))))


def tile_xy(lat, lon, z=ZOOM):
    """Fractional Web-Mercator tile coordinates (x east, y south) of lat/lon at zoom z (arrays ok)."""
    n = 2.0 ** z
    lat = np.clip(np.asarray(lat, np.float64), -MAX_LAT, MAX_LAT)
    x = (np.asarray(lon, np.float64) / 360.0 + 0.5) * n
    y = (0.5 - np.arcsinh(np.tan(np.radians(lat))) / (2 * np.pi)) * n
    return x, y


class CellKey(NamedTuple):
    """A global data cell: Web-Mercator tile (z, x, y). str() is 'z/x/y' (also its cache path)."""
    z: int
    x: int
    y: int

    def __str__(self):
        return f"{self.z}/{self.x}/{self.y}"

    @property
    def n(self):
        return 1 << self.z

    def bounds(self):
        """(lon0, lat0, lon1, lat1): west, south, east, north edges in degrees."""
        n = self.n
        return (self.x / n * 360.0 - 180.0, _lat_of(self.y + 1, n),
                (self.x + 1) / n * 360.0 - 180.0, _lat_of(self.y, n))

    def center(self):
        """(lat, lon) of the cell's Web-Mercator centre: the origin of its local ENU frame."""
        n = self.n
        return _lat_of(self.y + 0.5, n), (self.x + 0.5) / n * 360.0 - 180.0

    def quadkey(self):
        """Bing quadkey (one digit per level), as the Meta/WRI canopy files are named."""
        d = []
        for i in range(self.z, 0, -1):
            m = 1 << (i - 1)
            d.append(str((1 if self.x & m else 0) + (2 if self.y & m else 0)))
        return "".join(d)

    def parent(self, z):
        """The cell containing this one at zoom z <= self.z."""
        if z > self.z:
            raise ValueError(f"zoom {z} is finer than {self.z}")
        s = self.z - z
        return CellKey(z, self.x >> s, self.y >> s)

    def size_m(self):
        """(width, height) in metres on the ground, at the cell's centre latitude."""
        lat, _ = self.center()
        e = EARTH_CIRC / self.n * math.cos(math.radians(lat))
        return e, e

    def neighbours(self):
        """The 8 cells around this one (x wraps; none beyond the poles' rows), in a fixed order."""
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if (dx or dy) and 0 <= self.y + dy < self.n:
                    out.append(CellKey(self.z, (self.x + dx) % self.n, self.y + dy))
        return out


def check(key):
    """The key, if it is a valid cell (0 <= x, y < 2**z); ValueError otherwise."""
    z, x, y = key
    if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise ValueError(f"cell key out of range: {tuple(key)!r}")
    return key


def parse(text):
    """'z/x/y' (or 'z_x_y') -> CellKey, checked."""
    parts = str(text).replace("_", "/").split("/")
    if len(parts) != 3:
        raise ValueError(f"not a cell key: {text!r}")
    z, x, y = (int(p) for p in parts)
    return check(CellKey(z, x, y))


# Edges are exact: column x holds lon0 <= lon < lon1 and row y holds lat0 < lat <= lat1, with the
# edges as CellKey.bounds() computes them (a point on an edge belongs to the cell east / south of it).
# The tile formula alone is off by one for ~1 in 7 points that lie exactly on a north edge (its
# atan / asinh round trip is not exact), so a floor within EDGE_EPS of an integer is checked against
# the edges themselves.
EDGE_EPS = 1e-6                     # tile units (~2 mm at z14)


def _lon_of(x, n):
    return x / n * 360.0 - 180.0


def _exact_col(xf, lon, n):
    x = int(math.floor(xf))
    if xf - x < EDGE_EPS and lon < _lon_of(x, n):
        x -= 1
    elif x + 1 - xf < EDGE_EPS and lon >= _lon_of(x + 1, n):
        x += 1
    return x


def _exact_row(yf, lat, n):
    y = min(n - 1, max(0, int(math.floor(yf))))
    if yf - y < EDGE_EPS and y > 0 and lat > _lat_of(y, n):
        y -= 1
    elif y + 1 - yf < EDGE_EPS and y < n - 1 and lat <= _lat_of(y + 1, n):
        y += 1
    return y


def cell_of(lat, lon, z=ZOOM):
    """The cell containing lat/lon (a point on an edge belongs to the cell east / south of it, exactly
    as CellKey.bounds() puts the edge; 180 E wraps to x = 0)."""
    if not (-MAX_LAT <= lat <= MAX_LAT):
        raise ValueError(f"latitude {lat} is beyond the Web-Mercator limit {MAX_LAT}")
    n = 1 << z
    x, y = tile_xy(lat, lon, z)
    return CellKey(z, _exact_col(float(x), float(lon), n) % n, _exact_row(float(y), float(lat), n))


def cells_of(lat, lon, z=ZOOM):
    """[cell_of(lat[i], lon[i])] for arrays of points, at once: the tile formula for all, cell_of's exact edge rules for
    the few within EDGE_EPS of an edge (the same cells; road planning asked cell_of point by point ~60000 times per 16
    city road tiles, 2026-10-26)."""
    lat = np.asarray(lat, np.float64).ravel()
    lon = np.asarray(lon, np.float64).ravel()
    if not len(lat):
        return []
    if not ((lat >= -MAX_LAT) & (lat <= MAX_LAT)).all():
        bad = lat[~((lat >= -MAX_LAT) & (lat <= MAX_LAT))][0]
        raise ValueError(f"latitude {bad} is beyond the Web-Mercator limit {MAX_LAT}")
    n = 1 << z
    x, y = tile_xy(lat, lon, z)
    xi = np.floor(x)
    yi = np.clip(np.floor(y), 0, n - 1)
    edge = (x - xi < EDGE_EPS) | (xi + 1 - x < EDGE_EPS) | (y - yi < EDGE_EPS) | (yi + 1 - y < EDGE_EPS)
    xs = (xi.astype(np.int64) % n).tolist()
    ys = yi.astype(np.int64).tolist()
    out = [CellKey(z, cx, cy) for cx, cy in zip(xs, ys)]
    for i in np.flatnonzero(edge).tolist():
        out[i] = cell_of(float(lat[i]), float(lon[i]), z)
    return out


def cells_for_bbox(lat0, lon0, lat1, lon1, z=ZOOM, max_cells=4096):
    """Every cell that intersects the lat/lon box (west lon0 to east lon1; lon1 < lon0 crosses the
    antimeridian), in row-major order (north to south, west to east), each once. Latitudes are
    clamped to MAX_LAT; a box entirely beyond it has no cells. Raises ValueError for more than
    max_cells cells."""
    n = 1 << z
    la0, la1 = sorted((lat0, lat1))
    if la0 > MAX_LAT or la1 < -MAX_LAT:
        return []
    la0, la1 = max(-MAX_LAT, la0), min(MAX_LAT, la1)
    _, y0 = tile_xy(la1, 0.0, z)
    _, y1 = tile_xy(la0, 0.0, z)
    x0 = (lon0 / 360.0 + 0.5) * n
    x1 = (lon1 / 360.0 + 0.5) * n
    if x1 < x0:
        x1 += n
    ix0, ix1 = int(math.floor(x0)), int(math.ceil(x1)) - 1
    ix1 = min(max(ix1, ix0), ix0 + n - 1)                   # a box wrapping the world: each column once
    iy0 = min(n - 1, max(0, int(math.floor(float(y0)))))   # y0 can be -1e-12 at MAX_LAT
    iy1 = max(iy0, min(n - 1, int(math.ceil(float(y1))) - 1))
    count = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
    if count > max_cells:
        raise ValueError(f"{count} cells in the box (max {max_cells})")
    return [CellKey(z, ix % n, iy) for iy in range(iy0, iy1 + 1) for ix in range(ix0, ix1 + 1)]


def cells_for_points(lat, lon, pad_lat=0.0, pad_lon=0.0, z=ZOOM, max_cells=4096):
    """The cells of the points' box (cells_for_bbox, padded), or, for points spread farther apart than max_cells
    cells (a long road; one that runs to a pole, its longitudes all round the globe: the South Pole Traverse), the
    cells around each point (cells_near_points). Points past MAX_LAT have none."""
    lat = np.asarray(lat, np.float64).ravel()
    lon = np.asarray(lon, np.float64).ravel()
    if not lat.size:
        return []
    try:
        return cells_for_bbox(float(lat.min()) - pad_lat, float(lon.min()) - pad_lon,
                              float(lat.max()) + pad_lat, float(lon.max()) + pad_lon, z, max_cells)
    except ValueError:
        m = np.abs(lat) <= MAX_LAT + pad_lat
        return cells_near_points(lat[m], lon[m], pad_lat, pad_lon, z)


def cells_near_points(lat, lon, pad_lat, pad_lon, z=ZOOM):
    """The cells that intersect the box [lat - pad_lat, lat + pad_lat] x [lon - pad_lon, lon + pad_lon] of any
    point, in row-major order like cells_for_bbox (a subset of the points' bbox cells, perhaps a cell more on an
    exact edge). For points spread far apart: a straight 27 km track has two vertices, and their bbox ~240 cells."""
    n = 1 << z
    lat = np.asarray(lat, np.float64).ravel()
    lon = np.asarray(lon, np.float64).ravel()
    if not lat.size:
        return []
    x0, y1 = tile_xy(lat - pad_lat, lon - pad_lon, z)
    x1, y0 = tile_xy(lat + pad_lat, lon + pad_lon, z)
    ix0, ix1 = np.floor(x0).astype(np.int64), np.floor(x1).astype(np.int64)
    iy0 = np.clip(np.floor(y0).astype(np.int64), 0, n - 1)
    iy1 = np.clip(np.floor(y1).astype(np.int64), 0, n - 1)
    keys = set()
    for dx in range(int((ix1 - ix0).max()) + 1):
        for dy in range(int((iy1 - iy0).max()) + 1):
            ok = (ix0 + dx <= ix1) & (iy0 + dy <= iy1)
            keys.update(zip(((ix0[ok] + dx) % n).tolist(), (iy0[ok] + dy).tolist()))
    return [CellKey(z, x, y) for y, x in sorted((y, x) for x, y in keys)]


def owner(lon_c, lat_c, z=ZOOM):
    """Cell x, y arrays of the cells that own points (lon_c, lat_c): a feature that spans several
    cells is listed in each of them, and owned by the one containing its bbox centre (so a
    generator that builds only the owned features of each cell builds every feature once). Edges
    as cell_of(), except that a centre at exactly 180 E belongs to the last column: only that cell
    lists such a feature (its box ends at 180; the first column's starts at -180)."""
    n = 1 << z
    lon = np.asarray(lon_c, np.float64)
    lat = np.asarray(lat_c, np.float64)
    x, y = tile_xy(lat, lon, z)
    ox = np.floor(x).astype(np.int64)
    oy = np.clip(np.floor(y).astype(np.int64), 0, n - 1)
    near = np.flatnonzero((np.abs(x - np.round(x)) < EDGE_EPS) | (np.abs(y - np.round(y)) < EDGE_EPS))
    for i in near:                  # on an edge (rare): the exact rule
        ox.flat[i] = _exact_col(float(x.flat[i]), float(lon.flat[i]), n)
        oy.flat[i] = _exact_row(float(y.flat[i]), float(np.clip(lat.flat[i], -MAX_LAT, MAX_LAT)), n)
    ox = np.where(ox == n, n - 1, ox)                       # 180 E: the last column
    return ox % n, oy
