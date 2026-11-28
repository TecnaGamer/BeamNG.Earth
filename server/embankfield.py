"""A mapped road's embankment in the surface model's ground (M5 round 8, BNE_ROADSYS=surface 14 with
BNE_ROADSYS_EMBANK=on; road shape first).

Outside the national lidar the elevation is Copernicus GLO-30, a surface model, and dsmground's opening takes out what is
narrower than its window: buildings and tree stands, and also a road's own embankment, a ridge 30-80 m wide in the
30 m model along the road (0bff: a trunk's embankment cut 7-9 m; the raw model, the SRTM-era AWS terrarium and
Mapillary have the road high and gentle on it). The road's ground (the plans' and the profile squares' alike, the
ground the drawn road and the terrain beside it are made from) gets back that cut where it is the road's own:

  Field(md, dem, walls).apply(la, lo, z): z (the plans' ground, plan_elev) plus, near a road of EMBANK_CLASSES' ground
  parts (the map's own ranges), its embankment's lift there: per sample along the road (EMBANK_STEP_M) earthfast
  embank_lift (the sample's ground the filtered z12's, no lidar tile under it, the raw model bulging at the road over
  its sides' mean EMBANK_SIDE_M off, no building wall within EMBANK_BUILDING_M, a cut of EMBANK_CUT_MIN to EMBANK_MAX_M)
  made one embankment along it (embank_along), laid over the points as the profile pins are (profile_pin_field: in
  full within the road's half width, a smoothstep to nothing EMBANK_TAPER_M past it; the nearest road's alone).

Each map cell's lines are made once from that cell's roads (its home copies) and kept when read complete; a point's
lift reads the lines of the 3 x 3 cells round its own: the same whichever query asks. Nothing away from the roads
changes (within a road's half width + EMBANK_TAPER_M of its line only). (In the plans' obs at first, round 8 r1: the
drawn road follows the profile squares, whose ground stayed the filter's, and 0bff's trunk was buried up to 8 m under
its own raised terrain.)"""
import logging
import math
import threading

import numpy as np

import os

import dmath
import dnp

log = logging.getLogger("earthd.embank")

# Off unless BNE_ROADSYS_EMBANK=on (and BNE_ROADSYS=surface): on once the wide corpus shows no place worse than dev.
ON = os.environ.get("BNE_ROADSYS_EMBANK", "off").strip().lower() == "on"


def of(state):
    """state's Field (made on first use: its md, dem and widths), or None when off or not set up."""
    import roadsys
    if not ON or not roadsys.SURFACE:
        return None
    got = getattr(state, "_embank_field", None)
    if got is None:
        md, dem = getattr(state, "md", None), getattr(state, "dem", None)
        w = getattr(state, "widths", None) or getattr(state, "road_widths", None)
        if md is None or dem is None or not hasattr(dem, "sample_raw12"):
            return None
        got = Field(md, dem, w)
        try:
            state._embank_field = got
        except AttributeError:
            pass
    return got

EMBANK_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")
EMBANK_KERNEL = 649         # earthfast embank_lift / embank_along (and profile_pin_field)
EMBANK_STEP_M = 5.0         # m between a road's samples
EMBANK_MATCH_M = 0.5        # m: the sample's ground within this of the filtered z12 (the filter's ground, no lidar)
EMBANK_CUT_MIN = 0.5        # m: a cut at least this ...
EMBANK_MAX_M = 10.0         # ... and at most this (deeper: a tree stand's or a town's, no embankment)
EMBANK_SIDE_M = 35.0        # m either side the raw model's cross-section is read ...
EMBANK_CREST_MIN = 1.0      # ... its centre over its sides' mean by this (nothing to the whole cut at EMBANK_CREST_FULL)
EMBANK_CREST_FULL = 2.0
EMBANK_BUILDING_M = 30.0    # m: no building wall within this of the sample (a town's blocks, never an embankment)
EMBANK_MIN_LIFT = 0.05
EMBANK_GAP_M = 60.0         # m: a gap of no lift this long at most between lifted samples is filled (embank_along) ...
EMBANK_TAPER = 1.0          # ... and the lift rises or falls at most this per m along the road
EMBANK_TAPER_M = 12.0       # m past a road's half width its lift fades out over (grading.T_MAX)
_DIRS = np.array([[1.0, 0.0], [0.7071067811865476, 0.7071067811865476], [0.0, 1.0],
                  [-0.7071067811865476, 0.7071067811865476], [-1.0, 0.0], [-0.7071067811865476, -0.7071067811865476],
                  [0.0, -1.0], [0.7071067811865476, -0.7071067811865476]])


class Field:
    """The embankments' lift over the plans' ground (see the module's notes). md: MapData (roads), dem: dem.Dem (with
    sample_raw12 and deep_at), walls: roadwidth.RoadWidths (its buildings' walls)."""

    def __init__(self, md, dem, walls):
        self.md, self.dem, self.walls = md, dem, walls
        self.lock = threading.Lock()
        self.cells = {}         # str(cell) -> [(lat (n,), lon (n,), lift (n,), half width)]

    def apply(self, la, lo, z):
        import fast
        import mapcells
        la = np.asarray(la, np.float64)
        lo = np.asarray(lo, np.float64)
        z = np.asarray(z, np.float64)
        shape = z.shape
        if not z.size:
            return z
        fla, flo, fz = la.ravel(), lo.ravel(), z.ravel()
        out = fz.copy()
        ok = np.isfinite(fla) & np.isfinite(flo) & np.isfinite(fz)
        if not ok.any():
            return z
        xs, ys = mapcells.cells_xy(np.where(ok, fla, 0.0), np.where(ok, flo, 0.0))
        code = (xs << 32) | ys
        for c in np.unique(code[ok]).tolist():
            idx = np.flatnonzero(ok & (code == c))
            key = mapcells.CellKey(mapcells.ZOOM, c >> 32, c & 0xFFFFFFFF)
            lines = []
            n = 1 << key.z
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if 0 <= key.y + dy < n:
                        lines += self._lines(mapcells.CellKey(key.z, (key.x + dx) % n, key.y + dy))
            if not lines:
                continue
            import geodesy
            clat, clon = key.center()
            fr = geodesy.EnuFrame(clat, clon)
            e, nn, _u = fr.geodetic_to_enu(fla[idx], flo[idx], 0.0)
            pxy = np.ascontiguousarray(np.column_stack((np.asarray(e, np.float64), np.asarray(nn, np.float64))))
            qxy, qoff, dz, halves = [], [0], [], []
            for lla, llo, lift, half in lines:
                qe, qn, _u = fr.geodetic_to_enu(lla, llo, 0.0)
                qxy.append(np.column_stack((np.asarray(qe, np.float64), np.asarray(qn, np.float64))))
                qoff.append(qoff[-1] + len(lla))
                dz.append(lift)
                halves.append(float(half))
            q = np.ascontiguousarray(np.concatenate(qxy))
            d = np.ascontiguousarray(np.concatenate(dz), np.float64)
            one = np.ones(len(d))
            fast.require(EMBANK_KERNEL, "profile_pin_field")
            got = np.asarray(fast._ef.profile_pin_field(pxy, np.ascontiguousarray(fz[idx]), q, qoff, d,
                                                        np.ascontiguousarray(np.zeros(len(d))), one, one, halves,
                                                        [EMBANK_TAPER_M, 1e9, 2e9]), np.float64)
            out[idx] = fz[idx] + got
        return out.reshape(shape)

    def _lines(self, key):
        """Cell key's embankment lines [(lat (n,), lon (n,), lift (n,), half width)] (its roads' own: each road from the
        cell holding its first point), made once; [] when none or the cell cannot be read."""
        k = str(key)
        with self.lock:
            got = self.cells.get(k)
        if got is not None:
            return got
        import revisit
        with revisit.scope() as rd:
            try:
                got = self._make(key)
            except Exception as ex:             # (no embankment rather than no ground: not kept)
                (log.debug if isinstance(ex, OSError) else log.warning)("embankments %s: %s: %s", k, type(ex).__name__, ex)
                revisit.fault()
                return []
        if rd.complete:
            with self.lock:
                if len(self.cells) > 512:
                    self.cells.clear()
                self.cells[k] = got
        return got

    def _make(self, key):
        import fast
        import geodesy
        import grading
        import mapcells
        import roads
        f = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if f is None or not len(f):
            return []
        specs = roads.road_specs(f, None, getattr(self.walls, "get", None))
        clat, clon = key.center()
        fr = geodesy.EnuFrame(clat, clon)
        rows = []           # (lat (n,), lon (n,), xy (n, 2), half width)
        for i in range(len(f)):
            sp = specs[i]
            if sp is None or getattr(sp, "cls", None) not in EMBANK_CLASSES or sp.kind not in roads.BUILT:
                continue
            for ring in roads._parts_by_level_new(f, i, "ground", rngs=list(roads.tag_level_ranges(f, i))):
                ll = np.asarray(ring, np.float64)
                if len(ll) < 2 or str(mapcells.cell_of(float(ll[0, 1]), float(ll[0, 0]))) != str(key):
                    continue            # (each road part from the cell its first point lies in: one line whoever reads)
                e, nn, _u = fr.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                xy = np.column_stack((np.asarray(e, np.float64), np.asarray(nn, np.float64)))
                seg = dnp.hypot(*np.diff(xy, axis=0).T)
                s = np.r_[0.0, np.cumsum(seg)]
                if not s[-1] > 0.0:
                    continue
                st = np.r_[np.arange(0.0, s[-1], EMBANK_STEP_M), s[-1]]
                pts = np.column_stack((dnp.interp(st, s, xy[:, 0]), dnp.interp(st, s, xy[:, 1])))
                rows.append((st, pts, float(sp.width) / 2.0))
        if not rows:
            return []
        allp = np.concatenate([p for _s, p, _h in rows])
        la, lo, _u = fr.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        deep = np.asarray(self.dem.deep_at(la, lo), bool)
        if bool(deep.all()):
            return []
        fine = np.asarray(self.dem.sample(la, lo, grading.PROFILE_ZOOM), np.float64)
        raw = np.asarray(self.dem.sample_raw12(la, lo), np.float64)
        pre = np.flatnonzero(~deep & np.isfinite(raw) & (raw - fine > EMBANK_CUT_MIN - EMBANK_MATCH_M))
        if not len(pre):
            return []
        g12 = np.full(len(la), np.nan)
        g12[pre] = np.asarray(self.dem.sample(la[pre], lo[pre], 12), np.float64)
        with np.errstate(invalid="ignore"):
            cand = np.isfinite(raw) & np.isfinite(g12) & (raw - g12 > EMBANK_CUT_MIN) & \
                (np.abs(fine - g12) <= EMBANK_MATCH_M)
        if not cand.any():
            return []
        nrm = np.concatenate([grading._normals(p) for _s, p, _h in rows])
        idx = np.flatnonzero(cand)
        side = np.concatenate([allp[idx] + nrm[idx] * EMBANK_SIDE_M, allp[idx] - nrm[idx] * EMBANK_SIDE_M])
        sla, slo, _u = fr.enu_to_geodetic(side[:, 0], side[:, 1], 0.0)
        rs = np.asarray(self.dem.sample_raw12(np.asarray(sla, np.float64), np.asarray(slo, np.float64)), np.float64)
        k = len(idx)
        near = self._near_building(fr, allp[idx])
        fast.require(EMBANK_KERNEL, "embank_lift")
        lift_c = np.asarray(fast._ef.embank_lift(
            np.ascontiguousarray(fine[idx]), np.ascontiguousarray(g12[idx]), np.ascontiguousarray(raw[idx]),
            np.ascontiguousarray(rs[:k]), np.ascontiguousarray(rs[k:]), np.ascontiguousarray(near, bool),
            (EMBANK_MATCH_M, EMBANK_CUT_MIN, EMBANK_CREST_MIN, EMBANK_CREST_FULL, EMBANK_MAX_M)), np.float64)
        lift = np.zeros(len(la))
        lift[idx] = lift_c
        out, k0 = [], 0
        for s, p, half in rows:
            lf = np.ascontiguousarray(lift[k0:k0 + len(p)])
            rla, rlo = la[k0:k0 + len(p)], lo[k0:k0 + len(p)]
            k0 += len(p)
            if not (lf > EMBANK_MIN_LIFT).any():
                continue
            lf = np.asarray(fast._ef.embank_along(np.ascontiguousarray(s), lf, EMBANK_GAP_M, EMBANK_TAPER), np.float64)
            out.append((np.ascontiguousarray(rla), np.ascontiguousarray(rlo), np.round(lf, 4), half))
        return out

    def _near_building(self, fr, pts):
        """Per point (fr's frame): a building wall within EMBANK_BUILDING_M (rays in eight directions, the walls of
        every cell they reach: grading._walls_rays)."""
        import grading
        import mapcells
        near = np.zeros(len(pts), bool)
        w = self.walls
        if w is None or not hasattr(w, "_walls") or not len(pts):
            return near
        la, lo, _u = fr.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        pad = (EMBANK_BUILDING_M + 5.0) / 111320.0
        padlo = pad / max(0.05, dmath.cos(math.radians(float(np.max(np.abs(la))))))
        for key in sorted(set(mapcells.cells_for_points(la, lo, pad, padlo)), key=str):
            frame_c, walls = w._walls(key)
            if getattr(walls, "stand_in", False):
                import revisit
                revisit.fault()
            if not len(walls.segs):
                continue
            e, nn, _u = frame_c.geodetic_to_enu(la, lo, 0.0)
            p0 = np.column_stack((np.asarray(e, np.float64), np.asarray(nn, np.float64)))
            org = np.repeat(p0, len(_DIRS), axis=0)
            dirs = np.tile(_DIRS, (len(p0), 1))
            hit = grading._walls_rays(walls, org, dirs, EMBANK_BUILDING_M)
            near |= np.isfinite(hit).reshape(len(p0), len(_DIRS)).any(axis=1)
        return near
