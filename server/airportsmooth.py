"""The ground round an airport smoothed before its runways are graded in (airportgrade.AirportGrader).

An aerodrome is graded flat in life: its aprons and taxiways are paved on a few gentle grades, not on the DEM's
ripples (a lidar or photogrammetry DEM keeps hangars, parked aircraft, trees and its own noise as humps of a few
metres: Los Angeles' and Brisbane's aprons rolled under a car, 2026-11-15). Each runway group's zone (FLAT_K of its
longest runway past any runway's pavement, FLAT_MIN_M..FLAT_MAX_M, then a RAMP_M ramp back to the DEM) takes a
low-passed copy of the DEM: sampled on a GRID_M grid of its own frame (the group's first runway's), box-blurred
three times to about SIGMA_M (a Gaussian), read back bilinearly. The runways' own plans are fitted on the smoothed
ground (their strips blend into it), so a taxiway meets its runway level. Points outside every zone keep the DEM
bit for bit. Pure, deterministic: one grid a group, whoever asks first (as the plans)."""
import numpy as np

import dnp
import runwaygeom

GRID_M = 25.0                   # m: the smoothed grid's spacing
SIGMA_M = 120.0                 # m: about the blur's Gaussian sigma (three box passes)
FLAT_K = 0.25                   # the full zone: this x the longest runway past any runway's pavement ...
FLAT_MIN_M, FLAT_MAX_M = 150.0, 700.0       # ... within these (a farm strip's yard to a hub's terminal aprons)
RAMP_M = 250.0                  # m: then back to the DEM over this


def _box(a, r, axis):
    """a box-blurred along axis by a window of 2 r + 1 cells (edges held)."""
    if r < 1:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return (hi - lo) / (2 * r + 1)


class Surface:
    """The smoothed ground of one runway group (see the module docstring). sample(lat, lon) -> the DEM."""

    def __init__(self, group, sample):
        self.geoms = [runwaygeom.geometry(r) for r in sorted(group, key=lambda r: r.id)]
        self.full = min(max(FLAT_K * max(g.length for g in self.geoms), FLAT_MIN_M), FLAT_MAX_M)
        reach = self.full + RAMP_M + 2.0 * SIGMA_M
        self.frame = self.geoms[0].frame
        corners = []
        for g in self.geoms:
            s = np.array([-reach, g.length + reach, -reach, g.length + reach])
            t = np.array([-g.paved_half - reach, -g.paved_half - reach, g.paved_half + reach, g.paved_half + reach])
            la, lo = g.latlon(s, t)
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
            corners.append(np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1))
        c = np.concatenate(corners)
        self.e0 = float(np.floor(c[:, 0].min() / GRID_M) * GRID_M)
        self.n0 = float(np.floor(c[:, 1].min() / GRID_M) * GRID_M)
        nx = int(np.ceil((c[:, 0].max() - self.e0) / GRID_M)) + 1
        ny = int(np.ceil((c[:, 1].max() - self.n0) / GRID_M)) + 1
        ee, nn = np.meshgrid(self.e0 + GRID_M * np.arange(nx), self.n0 + GRID_M * np.arange(ny))
        la, lo, _ = self.frame.enu_to_geodetic(ee.ravel(), nn.ravel(), np.zeros(ee.size))
        z = np.asarray(sample(np.asarray(la, np.float64), np.asarray(lo, np.float64)), np.float64).reshape(ny, nx)
        z = np.where(np.isfinite(z), z, np.nanmedian(z) if np.isfinite(z).any() else 0.0)
        r = max(1, int(round(SIGMA_M / GRID_M)))
        for _ in range(3):
            z = _box(_box(z, r, 1), r, 0)
        self.z = z

    def weight(self, lat, lon):
        """The smoothed ground's share at lat/lon: 1 in the zone, ramping to 0 RAMP_M past it."""
        d = None
        for g in self.geoms:
            s, t = g.st(lat, lon)
            ds = np.maximum(np.maximum(-s, s - g.length), 0.0)
            dt = np.maximum(np.abs(t) - g.paved_half, 0.0)
            dd = dnp.hypot(ds, dt)
            d = dd if d is None else np.minimum(d, dd)
        w = np.clip((self.full + RAMP_M - d) / RAMP_M, 0.0, 1.0)
        return w * w * (3.0 - 2.0 * w)

    def ground(self, lat, lon):
        """The smoothed ground at lat/lon (bilinear in the grid)."""
        e, n, _ = self.frame.geodetic_to_enu(lat, lon, 0.0)
        x = (np.asarray(e, np.float64) - self.e0) / GRID_M
        y = (np.asarray(n, np.float64) - self.n0) / GRID_M
        ny, nx = self.z.shape
        x = np.clip(x, 0.0, nx - 1.000001)
        y = np.clip(y, 0.0, ny - 1.000001)
        i, j = np.floor(y).astype(np.int64), np.floor(x).astype(np.int64)
        fy, fx = y - i, x - j
        z = self.z
        return (z[i, j] * (1 - fx) * (1 - fy) + z[i, j + 1] * fx * (1 - fy)
                + z[i + 1, j] * (1 - fx) * fy + z[i + 1, j + 1] * fx * fy)

    def apply(self, lat, lon, z):
        """z with the zone's ground smoothed in; z itself (bit for bit) where the weight is 0."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        z = np.asarray(z, np.float64)
        w = self.weight(lat.ravel(), lon.ravel()).reshape(lat.shape)
        on = w > 0.0
        if not on.any():
            return z
        out = z.copy()
        zs = self.ground(lat[on], lon[on])
        out[on] = z[on] + w[on] * (zs - z[on])
        return out
