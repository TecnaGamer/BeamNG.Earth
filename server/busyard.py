"""The ground of a bus station (task 020: "fix the asphalt underlay": the yard was a mesh over the photo's sand, in the
wrong asphalt and with cut edges): the station's yard painted into the ground itself, as the parking lots, the gas
stations' forecourts and the runways are (surface.Surface.extra_pads: the ground's asphalt layer and the photo).

    yard_polys(st) -> [ring (k, 2)]                 pure: the shapes of the yard in the station's frame metres
    tile_pads(frame, tx, ty, md, pads)              the yards of a road tile's stations, as the gas stations' pads

The shapes: the mapped boundary, every bay's outline with a margin, the roofs, the platforms, the building with a margin,
and, with no mapped boundary, the hull of all that with an apron (the open ground in front of a hall is yard).
"""
import logging
import math
import threading

import numpy as np
import dmath
import dnp

import busbays
import busstations as bs

log = logging.getLogger("earthd.busstations")

BAY_PAD_ALONG_M, BAY_PAD_ACROSS_M = 1.5, 2.0    # the paving reaches this far past a bay's outline (end, far side)
BUILDING_PAD_M = 4.0
PLATFORM_PAD_M = 3.0
HULL_PAD_M = 3.0
MOUTH_FUNNEL_M, MOUTH_WIDTH_M = 12.0, 10.0           # an exit's funnel: length back from the street, width
YARD_ROAD_M = 60.0                              # service-road points this near the hub count as the yard's


def _rect(c, t, n, half_along, half_across):
    return np.array([c - t * half_along - n * half_across, c + t * half_along - n * half_across,
                     c + t * half_along + n * half_across, c - t * half_along + n * half_across])


CELL_M = 2.0
REACH_M = 12.0                                  # unmapped ground this far from a part of the station is yard, no farther
GREEN_CLASSES = ("grass", "park", "forest", "meadow", "garden", "wood", "shrub", "flowerbed", "scrub", "orchard")


def _shapes(st):
    """([ring], [sample points]) of what the station is surely made of: the bays' outlines with a margin, the roofs, the
    platforms, the building with a margin; and sample points along those and the service roads round the hub."""
    rings, pts = [], []
    for bay in busbays.layout_bays(st):
        r = _rect(bay.c, bay.t, bay.n, bay.length / 2 + BAY_PAD_ALONG_M, bay.width / 2 + BAY_PAD_ACROSS_M)
        rings.append(r)
        pts.append(r)
    for c in st.canopies:
        rings.append(bs._open(c["ring"]))
        pts.append(bs._open(c["ring"]))
    for pl in st.platforms:
        xy = np.asarray(pl["xy"], float)
        if pl["kind"] == "polygon":
            rings.append(bs._open(xy))
        pts.append(xy)
    for b in st.buildings:
        ring = bs._open(b["xy"])
        pts.append(ring)
        rings.append(bs.pad_ring(bs.hull(ring), BUILDING_PAD_M) if len(ring) >= 3 else ring)
    for ln in st.lanes:                         # the exits widen to a funnel at the street (plan 028)
        path = np.asarray(ln["xy"], float)
        for end, nxt in ((path[0], path[1]), (path[-1], path[-2])):
            if any(float(dnp.hypot(end[0] - mx, end[1] - my)) < 1.0 for mx, my in st.mouths):
                t = nxt - end
                ln_ = float(dnp.hypot(*t))
                if ln_ < 1e-6:
                    continue
                t = t / ln_
                n = np.array([-t[1], t[0]])
                r = _rect(end + t * (MOUTH_FUNNEL_M / 2.0 - 1.5), t, n, MOUTH_FUNNEL_M / 2.0, MOUTH_WIDTH_M / 2.0)
                rings.append(r)
                pts.append(r)
    for r in st.roads:
        xy = np.asarray(r["xy"], float)
        near = [p for p in _resample(xy, 4.0) if float(dnp.hypot(*(p - st.hub))) <= YARD_ROAD_M]
        if near:
            pts.append(np.array(near))
    return [r for r in rings if len(r) >= 3], pts


def _resample(path, step):
    path = np.asarray(path, float)
    out = [path[0]]
    for a, b in zip(path[:-1], path[1:]):
        k = max(1, int(dnp.hypot(*(b - a)) // step))
        out += [a + (b - a) * (i / k) for i in range(1, k + 1)]
    return out


def _runs(mask, xs, ys):
    """Rectangles (rings) covering the True cells of mask (len(ys), len(xs)): runs along x, equal runs of the rows
    below merged (a yard of 200 x 100 m is a hundred rectangles, not ten thousand cells)."""
    out, open_runs = [], {}
    half = CELL_M / 2.0
    for iy in range(len(ys)):
        row = mask[iy]
        runs = {}
        ix = 0
        n = len(row)
        while ix < n:
            if row[ix]:
                j = ix
                while j + 1 < n and row[j + 1]:
                    j += 1
                runs[(ix, j)] = open_runs.pop((ix, j), iy)           # (the row it started in)
                ix = j + 1
            else:
                ix += 1
        for (i0, i1), y0 in open_runs.items():
            out.append(_cells_ring(xs, ys, i0, i1, y0, iy - 1, half))
        open_runs = runs
    for (i0, i1), y0 in open_runs.items():
        out.append(_cells_ring(xs, ys, i0, i1, y0, len(ys) - 1, half))
    return out


def _cells_ring(xs, ys, i0, i1, j0, j1, half):
    xa, xb, ya, yb = xs[i0] - half, xs[i1] + half, ys[j0] - half, ys[j1] + half
    return np.array([[xa, ya], [xb, ya], [xb, yb], [xa, yb]])


def yard_polys(st):
    """The yard as rectangles (counter-clockwise rings, metres of the station's frame): the cells of the mapped boundary,
    or, with none, of the hull of the station's parts and its service roads, that are within REACH_M of one of those
    parts; without the mapped lawns and the neighbours' buildings; and with every part of the station itself (a bay on a
    mapped lawn is yard). The hull alone asphalted the lawn in front of Vinnytsia West's hall. Memoised on the station."""
    cached = getattr(st, "_yard", None)
    if cached is not None:
        return cached
    import polygon
    rings, samples = _shapes(st)
    allp = [np.asarray(p, float) for p in samples] + [st.hub[None]]
    if st.boundary is not None:
        allp.append(bs._open(st.boundary))
    pts = np.vstack(allp)
    if st.boundary is not None:
        fill = bs._open(st.boundary)
    else:
        hull = bs.hull(pts)
        fill = bs.pad_ring(hull, HULL_PAD_M) if len(hull) >= 3 else None
    if fill is None or len(fill) < 3:
        st._yard = [r for r in rings]
        return st._yard
    lo = np.minimum(fill.min(0), pts.min(0)) - 4.0
    hi = np.maximum(fill.max(0), pts.max(0)) + 4.0
    xs = np.arange(lo[0], hi[0] + CELL_M, CELL_M)
    ys = np.arange(lo[1], hi[1] + CELL_M, CELL_M)
    gx, gy = np.meshgrid(xs, ys)
    grid = np.column_stack((gx.ravel(), gy.ravel()))
    mask = polygon.contains_points(fill, grid)
    if st.boundary is None and mask.any():                      # near a part of the station, not the whole hull
        sample = np.vstack([np.asarray(p, float) for p in samples]) if samples else st.hub[None]
        cand = np.flatnonzero(mask)
        reach = np.zeros(len(grid), bool)
        for i in range(0, len(cand), 4000):
            idx = cand[i:i + 4000]
            d2 = (dnp.sq(grid[idx][:, None, :] - sample[None, :, :])).sum(2).min(1)
            reach[idx] = d2 <= REACH_M * REACH_M
        mask &= reach
    for ring in list(getattr(st, "greens", ())) + list(st.obstacles):
        if mask.any():
            sel = np.flatnonzero(mask)
            hit = polygon.contains_points(ring, grid[sel])
            mask[sel[hit]] = False
    for ring in rings:
        mask |= polygon.contains_points(ring, grid)
    st._yard = _runs(mask.reshape(len(ys), len(xs)), xs, ys)
    return st._yard


# --- the ground's side ----------------------------------------------------------------------------------------------

def station_rings(st, frame):
    """[(lat (k,), lon (k,))] of one station's yard shapes."""
    out = []
    for ring in yard_polys(st):
        la, lo, _ = frame.enu_to_geodetic(ring[:, 0], ring[:, 1], np.zeros(len(ring)))
        out.append((np.asarray(la, np.float64), np.asarray(lo, np.float64)))
    return out


def tile_pads(frame, tx, ty, md, pads):
    """Append the yards of the stations near road tile (tx, ty) to pads [(id, rings)] as gasstations.tile_sites does its
    forecourts: the server keeps them (earthd.keep_fuel_pads) and hands them to the terrain blocks' processes with their
    jobs, so no block process reads the map layers for them (it did: yard_rings, ~60 MB a city cell; Surface.add_fuel_pads
    says why that must not happen)."""
    if not bs.ON or md is None:
        return
    import roads
    try:
        for st in bs.stations_near(md, frame, roads.tile_bbox(tx, ty), shared=True):    # (lat / lon out: any frame)
            pads.append(("bus:" + st.fid, station_rings(st, getattr(st, "group_frame", None) or frame)))
    except Exception as ex:                             # no yard paint rather than no road tile
        log.warning("bus station yards of tile (%s, %s): %s: %s", tx, ty, type(ex).__name__, ex)


# --- a level yard (plan 028) ---------------------------------------------------------------------------------------

FLAT_SLOPE_MAX = 0.03       # the yard's plane falls at most this (3 %)
FLAT_BLEND_M = 10.0         # the ground returns to the terrain over this far outside the yard
FLAT_SAMPLE_M = 4.0         # the plane is fitted on the DEM every this inside the yard
FLAT_ZOOM_M = 4.0           # the DEM pixel the plane is fitted on
FLAT_MAX_SAMPLES = 3000


def _local(rings, lat0, lon0):
    kx = 111320.0 * dmath.cos(math.radians(lat0))
    return [np.column_stack(((np.asarray(lo, float) - lon0) * kx, (np.asarray(la, float) - lat0) * 110574.0))
            for la, lo in rings], kx


def _ring_dist(ring, pts):
    """Distance (k,) from points to a ring's edges."""
    a = ring
    b = np.roll(ring, -1, axis=0)
    ab = b - a
    ab2 = np.maximum((ab * ab).sum(1), 1e-9)
    t = np.clip(((pts[:, None, 0] - a[None, :, 0]) * ab[None, :, 0] + (pts[:, None, 1] - a[None, :, 1]) * ab[None, :, 1])
                / ab2[None, :], 0.0, 1.0)
    qx = a[None, :, 0] + t * ab[None, :, 0]
    qy = a[None, :, 1] + t * ab[None, :, 1]
    return dnp.hypot(qx - pts[:, None, 0], qy - pts[:, None, 1]).min(1)


class YardPlan:
    """One station's level yard: its rings (local metres round lat0, lon0) and the plane z = z0 + gx x + gy y."""

    def __init__(self, rings, dem_sample):
        allla = np.concatenate([np.asarray(la, float) for la, _ in rings])
        alllo = np.concatenate([np.asarray(lo, float) for _, lo in rings])
        self.lat0, self.lon0 = float(allla.mean()), float(alllo.mean())
        self.rings, self.kx = _local(rings, self.lat0, self.lon0)
        self.lo = np.min([r.min(0) for r in self.rings], axis=0)
        self.hi = np.max([r.max(0) for r in self.rings], axis=0)
        self.bbs = [(r.min(0), r.max(0)) for r in self.rings]
        step = FLAT_SAMPLE_M
        while ((self.hi[0] - self.lo[0]) / step) * ((self.hi[1] - self.lo[1]) / step) > FLAT_MAX_SAMPLES:
            step *= 1.5
        gx, gy = np.meshgrid(np.arange(self.lo[0], self.hi[0], step), np.arange(self.lo[1], self.hi[1], step))
        pts = np.column_stack((gx.ravel(), gy.ravel()))
        pts = pts[self.inside(pts)]
        if len(pts) < 3:
            self.plane = None
            return
        la = self.lat0 + pts[:, 1] / 110574.0
        lo = self.lon0 + pts[:, 0] / self.kx
        z = np.asarray(dem_sample(la, lo), float).ravel()
        mx, my = pts.mean(0)
        A = np.column_stack((pts[:, 0] - mx, pts[:, 1] - my, np.ones(len(pts))))
        coef, *_ = dnp.lstsq(A, z, rcond=None)
        res = z - dnp.matmul(A, coef)
        keep = np.abs(res) <= max(2.0 * float(res.std()), 0.05)             # (a tree, a wall in the DEM: left out once)
        if keep.sum() >= 3:
            coef, *_ = dnp.lstsq(A[keep], z[keep], rcond=None)
        gxy = np.array(coef[:2])
        g = float(dnp.hypot(*gxy))
        if g > FLAT_SLOPE_MAX:
            gxy *= FLAT_SLOPE_MAX / g
        z0 = float(np.mean(z[keep] - (pts[keep, 0] - mx) * gxy[0] - (pts[keep, 1] - my) * gxy[1]))
        self.plane = (float(mx), float(my), z0, float(gxy[0]), float(gxy[1]))

    def inside(self, pts):
        import polygon
        out = np.zeros(len(pts), bool)
        for r, (lo, hi) in zip(self.rings, self.bbs):
            m = (pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0]) & (pts[:, 1] >= lo[1]) & (pts[:, 1] <= hi[1]) & ~out
            if m.any():
                idx = np.flatnonzero(m)
                out[idx[polygon.contains_points(r, pts[idx])]] = True
        return out

    def weight(self, pts):
        """(k,) 1 inside the yard, falling to 0 over FLAT_BLEND_M outside it."""
        w = np.zeros(len(pts))
        ins = self.inside(pts)
        w[ins] = 1.0
        rest = np.flatnonzero(~ins)
        if len(rest):
            d = np.full(len(rest), 1e9)
            q = pts[rest]
            for r, (lo, hi) in zip(self.rings, self.bbs):
                m = (q[:, 0] >= lo[0] - FLAT_BLEND_M) & (q[:, 0] <= hi[0] + FLAT_BLEND_M) & \
                    (q[:, 1] >= lo[1] - FLAT_BLEND_M) & (q[:, 1] <= hi[1] + FLAT_BLEND_M)
                if m.any():
                    d[m] = np.minimum(d[m], _ring_dist(r, q[m]))
            t = np.clip(1.0 - d / FLAT_BLEND_M, 0.0, 1.0)
            w[rest] = t * t * (3.0 - 2.0 * t)
        return w


class YardGrader:
    """The yards' ground (like airportgrade.AirportGrader's runways): apply(lat, lon, z) gives z with every bus station's
    yard near the points levelled. pad_items(lat, lon) -> [(station id, rings)] (surface.Surface.bus_pad_items: the pads
    the road tiles keep, so no map data is read here); dem.sample(lat, lon, zoom) is the terrain."""

    def __init__(self, dem, pad_items):
        self.dem, self.pad_items = dem, pad_items
        self._plans = {}
        self._lock = threading.Lock()

    def _plan(self, sid, rings):
        key = (sid, len(rings), round(float(np.asarray(rings[0][0], float).sum()), 6),
               round(float(np.asarray(rings[-1][1], float).sum()), 6))
        with self._lock:
            p = self._plans.get(key)
            if p is not None:
                return p
        import dem as demmod
        lat0 = float(np.mean(np.asarray(rings[0][0], float)))
        zoom = demmod.zoom_for(lat0, FLAT_ZOOM_M)
        p = YardPlan(rings, lambda la, lo: self.dem.sample(la, lo, zoom))
        with self._lock:
            self._plans[key] = p
            while len(self._plans) > 256:
                self._plans.pop(next(iter(self._plans)))
        return p

    def apply(self, lat, lon, z):
        """z with the yards levelled (z itself when no yard is near; any shape, or a scalar)."""
        la = np.asarray(lat, np.float64)
        lo = np.asarray(lon, np.float64)
        if la.size == 0:
            return z
        try:
            items = self.pad_items(la, lo)
        except Exception:
            return z
        if not items:
            return z
        shape = np.broadcast(la, lo).shape
        out = np.array(np.broadcast_to(np.asarray(z, np.float64), shape), np.float64).ravel()
        la1 = np.broadcast_to(la, shape).ravel()
        lo1 = np.broadcast_to(lo, shape).ravel()
        for sid, rings in items:
            if not rings:
                continue
            p = self._plan(sid, rings)
            if p.plane is None:
                continue
            x = (lo1 - p.lon0) * p.kx
            y = (la1 - p.lat0) * 110574.0
            near = (x >= p.lo[0] - FLAT_BLEND_M) & (x <= p.hi[0] + FLAT_BLEND_M) & \
                   (y >= p.lo[1] - FLAT_BLEND_M) & (y <= p.hi[1] + FLAT_BLEND_M)
            if not near.any():
                continue
            idx = np.flatnonzero(near)
            pts = np.column_stack((x[idx], y[idx]))
            w = p.weight(pts)
            mx, my, z0, gx, gy = p.plane
            zp = z0 + (pts[:, 0] - mx) * gx + (pts[:, 1] - my) * gy
            hit = w > 0
            out[idx[hit]] = out[idx[hit]] + w[hit] * (zp[hit] - out[idx[hit]])
        res = out.reshape(shape)
        return float(res) if res.ndim == 0 else res
