"""Racing street furniture round every racetrack (OpenStreetMap highway=raceway), one rule for all of them.

Everything is derived from the map round the track; there are no per-track lists.

1. Mapped barriers near a raceway: barrier=fence within MAPPED_FENCE_M of a carriageway's edge becomes a catch fence
   (the stock chain-link panel at CATCH_H), barrier=wall within MAPPED_WALL_M a concrete wall (the noise blocker), and
   barrier=tyres a row of tyre stacks. Chain-link fences and noise walls are fences.py's already, and guard rails /
   jersey barriers guardrails.py's (its _road_side turns a one-sided rail to the nearest carriageway, raceways
   included: rails face the racing surface); neither is drawn again here.
2. Inferred catch fences: a service road, track, footway or path running alongside a raceway (|direction dot| >=
   PARALLEL_DOT) with its edge INFER_MIN_M .. INFER_MAX_M outside the carriageway's edge for INFER_RUN_M or more gets a
   catch fence on its raceway side, FENCE_OFF_M off its own edge; left out where a mapped barrier is already within
   MAPPED_NEAR_M (Cadwell Park has none mapped; Lime Rock and Bandimere do in places).
3. Tyre walls on the outside of tight corners (radius < TIGHT_R_M over TIGHT_RUN_M or more) where the runoff is short:
   an obstacle (another road's edge, a mapped or inferred barrier, the track's own other side) within RUNOFF_SHORT_M
   of the carriageway's edge. The stacks stand TYRE_FRONT_M in front of it, never nearer the edge than the shoulder
   CLEAR_M + MARGIN_M (the gravel / grass margin).
4. Grandstands: a building footprint of class grandstand within GRANDSTAND_REACH_M of a raceway becomes the stock
   Hirochi grandstand model (raked seats, open front), the open front toward the nearest raceway: fitted to its
   oriented box when it is near a rectangle, else short straight pieces along its curved track-facing edge; a model
   that would come within the track's band is dropped, and a footprint left with none stays a building; buildings.tile_buildings leaves those footprints out (replaces_grandstand: the same test there).

Nothing stands on the racing surface (each raceway's carriageway + CLEAR_M) or on any other road's carriageway
(+ ROAD_CLEAR_M): a prop there is dropped, which leaves the gap a service road needs where it crosses a fence's line.
Rows are fences.rows' Forest rows [group, x, y, z, yaw, scale, height, rank]; one is kept when its centre is in the
tile's box, and every line is sampled from its own start, so a line split across tiles is not drawn twice.
"""
import json
import os
import logging
import math

import numpy as np

import mapcells
import mapgeom

log = logging.getLogger("earthd.racefurniture")

G_CATCH, G_WALL, G_TYRE, G_STAND = "catchfence", "noisewall", "tyrestack", "grandstand"

# s_chainlink_old (fences.py): 2.04 m along local +x, origin at its forward end, 1.76 m tall; scaled by height
CATCH_H = 3.5
CATCH_SCALE = CATCH_H / 1.76
CATCH_LEN, CATCH_OX, CATCH_LIFT = 2.04 * CATCH_SCALE, 1.0 * CATCH_SCALE, 0.16 * CATCH_SCALE
# s_concrete_noiseblocker (fences.py): 4 m along local +y, 2.5 m thick (local +x), origin at one end
WALL_LEN, WALL_ALONG, WALL_ACROSS, WALL_H = 4.0, 2.0, 1.25, 2.4
# the track walls: the stock 3 m jersey barrier (guardrails.G_JERSEY, the bridges' and medians'), overlapping modules
JERSEY_LEN_M, JERSEY_STEP_M, JERSEY_HALF_M, JERSEY_SINK_M = 3.0, 2.8, 0.35, 0.05
# hr_tirestack_blue (west_coast_usa): 0.65 m round, 1 m tall, origin at its foot centre
TYRE_STEP, TYRE_H = 0.68, 1.0
# hirochi_raceway grandstand: x -44.2..44.2 (length), y -14.1..14.1 (depth, the seats rising toward -y: the open
# front faces +y; read from grandstand.dae 2026-11-05, after a flip to -y faced Talladega's away from the track), z -9.7..9.8 (origin at mid height)
STAND_LEN, STAND_DEPTH, STAND_H, STAND_ZOFF = 88.4, 28.2, 19.5, 9.7

CLEAR_M = 4.0               # m past a raceway carriageway's edge kept clear (the shoulder)
ROAD_CLEAR_M = 0.5          # m past any other carriageway's edge kept clear
MARGIN_M = 2.0              # m of gravel / grass between the shoulder and a tyre wall
DEFAULT_RACE_W = 10.0       # m, a raceway without a usable width
DEFAULT_ACCESS_W = 3.5
ACCESS_CLASSES = {"service", "track", "footway", "path", "cycleway", "bridleway"}
MAPPED_FENCE_M = 30.0
MAPPED_WALL_M = 20.0
MAPPED_TYRE_M = 30.0
MAPPED_NEAR_M = 8.0
PARALLEL_DOT = 0.85
INFER_MIN_M, INFER_MAX_M, INFER_RUN_M = 10.0, 40.0, 50.0
FENCE_OFF_M = 1.5
TIGHT_R_M, TIGHT_RUN_M, CURVE_HALF_M = 60.0, 8.0, 8.0
RUNOFF_SHORT_M = 30.0
TYRE_FRONT_M = 1.5
GRANDSTAND_REACH_M = 40.0
STEP_M = 2.0
PAD_M = 120.0               # m round the tile the track's context is read from

CATCH_FENCE_TYPES = {"fence"}
WALL_TYPES = {"wall"}
TYRE_TYPES = {"tyres", "tires", "tyre", "tire", "tyre_barrier", "tire_barrier"}
BARRIER_ANY = {"fence", "wall", "guard_rail", "jersey_barrier", "cable_barrier", "tyres", "tires", "noise_barrier",
               "retaining_wall", "hedge"}


# ---------------------------------------------------------------------------------------------------- geometry

def resample(line, step):
    """(points (k, 2), unit tangents (k, 2), arc (k,)) every `step` m along a polyline, both ends included."""
    line = np.asarray(line, np.float64)[:, :2]
    d = np.diff(line, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    keep = np.r_[True, ln > 1e-6]
    line = line[keep]
    if len(line) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
    d = np.diff(line, axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    arc = np.r_[0.0, np.cumsum(ln)]
    s = np.arange(0.0, arc[-1] + 1e-9, step)
    k = np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(d) - 1)
    t = (s - arc[k]) / ln[k]
    pts = line[k] + t[:, None] * d[k]
    return pts, d[k] / ln[k, None], s


class Segs:
    """Line segments with a half width each: distances from points to the nearest one's edge."""

    def __init__(self, lines, halves):
        a, u, ln, hw = [], [], [], []
        for line, h in zip(lines, halves):
            line = np.asarray(line, np.float64)[:, :2]
            if len(line) < 2:
                continue
            d = np.diff(line, axis=0)
            l_ = np.hypot(d[:, 0], d[:, 1])
            m = l_ > 1e-6
            a.append(line[:-1][m])
            u.append(d[m] / l_[m, None])
            ln.append(l_[m])
            hw.append(np.full(int(m.sum()), float(h)))
        self.n = sum(len(x) for x in a)
        if self.n:
            self.a, self.u, self.len, self.half = map(np.concatenate, (a, u, ln, hw))

    def nearest(self, pts, chunk=256):
        """(edge distance (k,), centreline distance (k,), segment unit (k, 2), foot (k, 2)) of points (k, 2) to the
        nearest segment by edge distance; inf where there are none."""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        k = len(pts)
        edge, cen = np.full(k, np.inf), np.full(k, np.inf)
        unit, foot = np.zeros((k, 2)), np.zeros((k, 2))
        if not self.n or not k:
            return edge, cen, unit, foot
        for s in range(0, k, chunk):
            p = pts[s:s + chunk]
            v = p[:, None, :] - self.a[None]
            al = np.clip((v * self.u[None]).sum(2), 0.0, self.len[None])
            f = self.a[None] + al[..., None] * self.u[None]
            d = np.hypot(*(p[:, None, :] - f).transpose(2, 0, 1))
            e = d - self.half[None]
            j = np.argmin(e, axis=1)
            r = np.arange(len(p))
            edge[s:s + chunk], cen[s:s + chunk] = e[r, j], d[r, j]
            unit[s:s + chunk], foot[s:s + chunk] = self.u[j], f[r, j]
        return edge, cen, unit, foot

    def edge(self, pts):
        return self.nearest(pts)[0]


def _runs(mask, min_count):
    """[(start, stop)] of the runs of True in mask at least min_count long."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if j - i >= min_count:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _yaw(dx, dy):
    """Degrees that turn a Forest item's local +x toward (dx, dy)."""
    return math.degrees(math.atan2(dy, dx)) % 360.0


def _hash01(*parts):
    h = 2166136261
    for c in "|".join(str(p) for p in parts).encode():
        h = ((h ^ c) * 16777619) & 0xFFFFFFFF
    return h / 4294967296.0


# ---------------------------------------------------------------------------------------------------- the map

def _tags(feats, i):
    tags = feats.attrs.get("tags")
    if tags is None:
        return {}
    try:
        values = json.loads(str(tags[i]))
    except (TypeError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in values.items()}


def barrier_type(feats, i):
    """A mapped infrastructure line's barrier type (Overture's class for subtype barrier, else OSM's barrier tag)."""
    a = feats.attrs
    cls = str(a["class"][i]).strip().lower() if a.get("class") is not None else ""
    sub = str(a["subtype"][i]).strip().lower() if a.get("subtype") is not None else ""
    if sub == "barrier" and cls:
        return cls
    return _tags(feats, i).get("barrier") or None


def _width(feats, i, default):
    for key in ("osm_width", "width"):
        col = feats.attrs.get(key)
        if col is None:
            continue
        try:
            w = float(col[i])
            if np.isfinite(w) and 2.0 <= w <= 40.0:
                return w
        except (TypeError, ValueError):
            pass
    return default


def _lines(feats, frame, i):
    out = []
    for part in feats.parts(i):
        if part and len(part[0]) >= 2:
            e, n = mapgeom.enu_xy(feats, frame, np.asarray(part[0], np.float64))
            out.append(np.column_stack((e, n)))
    return out


class Track:
    """The map round a tile: raceways, other roads, access roads, mapped barriers, grandstand footprints."""

    def __init__(self):
        self.race, self.race_half, self.race_ids = [], [], []
        self.roads, self.roads_half = [], []
        self.access, self.access_half, self.access_ids = [], [], []
        self.barriers = []                          # (id, type, fences.py's kind or None, line)
        self.stands = []                            # (id, ring (k, 2))

    def finish(self):
        self.race_segs = Segs(self.race, self.race_half)
        self.road_segs = Segs(self.roads + self.access, self.roads_half + self.access_half)
        self.barrier_segs = Segs([b[3] for b in self.barriers if b[1] in BARRIER_ANY],
                                 [0.0 for b in self.barriers if b[1] in BARRIER_ANY])
        return self

    def clear(self, pts, reach=0.3):
        """Whether points (k, 2) (a prop `reach` m round) keep off the racing surface and every other road."""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        ok = self.race_segs.edge(pts) >= CLEAR_M + reach
        if self.road_segs.n:
            ok &= self.road_segs.edge(pts) >= ROAD_CLEAR_M + reach
        return ok


def gather(frame, cell_fn, box, pad=PAD_M):
    """Track of the cells round box (x0, y0, x1, y1): cell_fn(key, layers) -> {layer: Features}."""
    import roads as roadsmod
    x0, y0, x1, y1 = map(float, box)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    tr = Track()
    try:
        keys = mapcells.cells_for_points(la, lo, pad_lat=0.0002, pad_lon=0.0002, max_cells=64)
    except (ValueError, OverflowError):
        return tr.finish()
    seen = set()
    cands = []                  # (id, class, [outer, inner rings]) of the buildings, sorted once the raceways are in
    for key in keys:
        try:
            layers = cell_fn(key, ("roads", "infrastructure", "buildings"))
        except Exception as ex:
            log.debug("racefurniture: cell %s unavailable: %s", key, ex)
            continue
        f = layers.get("roads")
        if f is not None and len(f) and f.attrs.get("class") is not None:
            try:
                specs = roadsmod.road_specs(f)
            except Exception:
                specs = [None] * len(f)
            bb = f.boxes(frame)
            for i in range(len(f)):
                fid = str(f.ids[i])
                if fid in seen or int(f.kind[i]) != mapgeom.LINE:
                    continue
                b = bb[i]
                if b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1:
                    continue
                seen.add(fid)
                cls = str(f.attrs["class"][i]).strip().lower()
                sub = str(f.attrs["subtype"][i]).strip().lower() if f.attrs.get("subtype") is not None else "road"
                if sub not in ("road", ""):
                    continue
                spec = specs[i] if i < len(specs) else None
                for ln in _lines(f, frame, i):
                    if cls == "raceway":
                        w = float(spec.width) if spec is not None else _width(f, i, DEFAULT_RACE_W)
                        tr.race.append(ln)
                        tr.race_half.append(w / 2.0)
                        tr.race_ids.append(fid)
                    elif cls in ACCESS_CLASSES:
                        w = float(spec.width) if spec is not None else _width(f, i, DEFAULT_ACCESS_W)
                        tr.access.append(ln)
                        tr.access_half.append(w / 2.0)
                        tr.access_ids.append(fid)
                    elif spec is not None:
                        tr.roads.append(ln)
                        tr.roads_half.append(float(spec.width) / 2.0)
        f = layers.get("infrastructure")
        if f is not None and len(f):
            import fences
            for i in range(len(f)):
                fid = str(f.ids[i])
                if fid in seen or int(f.kind[i]) not in (mapgeom.LINE, mapgeom.POLYGON):
                    continue
                t = barrier_type(f, i)
                if not t:
                    continue
                seen.add(fid)
                kind = fences.kind_of(f, i)
                for ln in _lines(f, frame, i):
                    tr.barriers.append((fid, t, kind, ln))
        f = layers.get("buildings")
        if f is not None and len(f) and f.attrs.get("class") is not None:
            src = f.attrs.get("src_dataset")
            for i in range(len(f)):
                fid = str(f.ids[i])
                if fid in seen or int(f.kind[i]) != mapgeom.POLYGON:
                    continue
                if src is not None and str(src[i]) != "OpenStreetMap":
                    continue            # (a machine-found footprint near a track is left out by buildings.py)
                seen.add(fid)
                pp = f.parts(i)
                if not pp or not pp[0] or len(pp[0][0]) < 4:
                    continue
                rings = [np.column_stack(mapgeom.enu_xy(f, frame, np.asarray(r, np.float64))) for r in pp[0]]
                cands.append((fid, str(f.attrs["class"][i]), rings))
    _stands(tr, cands)
    return tr.finish()


def _stands(tr, cands):
    """tr.stands from the building footprints cands: each piece (buildings.ring_pieces) that race_stand makes a
    stand, the same test buildings.tile_buildings leaves it out by."""
    if not tr.race:
        return
    import buildings
    allr = np.concatenate([np.asarray(ln)[:, :2] for ln in tr.race])
    lo, hi = allr.min(0) - 80.0, allr.max(0) + 80.0
    for fid, cls, rings in cands:
        o = rings[0]
        if (o.max(0) < lo).any() or (o.min(0) > hi).any():
            continue
        ring0 = buildings.clean_ring(o)
        if ring0 is None:
            continue
        pieces = buildings.ring_pieces(ring0, rings[1:]) if len(rings) > 1 else [ring0]
        for k, r in enumerate(pieces):
            if race_stand(r, cls, tr.race, tr.race_half) == "stand":
                tr.stands.append((fid if len(pieces) == 1 else "%s#%d" % (fid, k), r))


# ---------------------------------------------------------------------------------------------------- the rules

def _catch_panels(line, tr, owner):
    """Catch-fence rows (x, y, yaw) along a line: panel centres every CATCH_LEN, the clear ones."""
    pts, tan, _ = resample(line, CATCH_LEN)
    if len(pts) < 2:
        return []
    mids = (pts[:-1] + pts[1:]) / 2.0
    tans = tan[:-1]
    ok = tr.clear(mids, 0.3) & tr.clear(pts[:-1], 0.3) & tr.clear(pts[1:], 0.3)    # (the panel's middle and ends)
    out = []
    for p, t, k in zip(mids, tans, ok):
        if k:
            out.append((G_CATCH, p[0] + CATCH_OX * t[0], p[1] + CATCH_OX * t[1], _yaw(t[0], t[1]), CATCH_H, owner))
    return out


def _wall_panels(line, tr, owner):
    pts, tan, _ = resample(line, WALL_LEN)
    if len(pts) < 2:
        return []
    mids, tans = (pts[:-1] + pts[1:]) / 2.0, tan[:-1]
    # the block's thickness to the side away from the track
    _, _, _, foot = tr.race_segs.nearest(mids)
    out = []
    for p, t, f, k in zip(mids, tans, foot, tr.clear(mids, 1.3)):
        if not k:
            continue
        right = np.array([t[1], -t[0]])
        side = 1.0 if np.dot(p - f, right) >= 0 else -1.0       # +1: the track is on the line's left
        # local +x (the thickness) to the right of the line when the track is on its left
        if side > 0:
            x, y, yaw = p[0] - WALL_ALONG * t[0], p[1] - WALL_ALONG * t[1], _yaw(t[1], -t[0])
        else:
            x, y, yaw = p[0] + WALL_ALONG * t[0], p[1] + WALL_ALONG * t[1], _yaw(-t[1], t[0])
        out.append((G_WALL, x, y, yaw, WALL_H, owner))
    return out


def _tyre_line(pts, owner, tr):
    out = []
    for p in pts:
        out.append((G_TYRE, p[0], p[1], 360.0 * _hash01(owner, round(p[0], 1), round(p[1], 1)), TYRE_H, owner))
    ok = tr.clear(np.array([[r[1], r[2]] for r in out]).reshape(-1, 2), 0.35) if out else []
    return [r for r, k in zip(out, ok) if k]


def wall_runs(tr):
    """[(owner, line (k, 2))]: the mapped walls near the raceways (MAPPED_WALL_M, along the track), cut where they
    would reach onto the racing surface's shoulder or another road (clear, a jersey barrier's half width), each run
    continuous."""
    out = []
    for fid, t, kind, line in tr.barriers:
        if kind is not None or t not in WALL_TYPES:
            continue
        pts, tan, _ = resample(line, STEP_M)
        if len(pts) < 2:
            continue
        edge, _, unit, _ = tr.race_segs.nearest(pts)
        near = (edge <= MAPPED_WALL_M) & (np.abs((tan * unit).sum(1)) >= PARALLEL_DOT) & tr.clear(pts, JERSEY_HALF_M)
        for a, b in _runs(near, 3):
            if b - a >= 2:
                out.append((fid, pts[a:b]))
    return out


def jersey_modules(line, z):
    """The jersey barrier modules along a run: [(centre (2,), centre z, forward (3,) unit along the chord, its slope
    included)], JERSEY_LEN_M long, spread so the first starts at the run's start and the last ends at its end, each
    overlapping the next (step at most JERSEY_STEP_M): no gap along it, none under it on a grade (each module tilted
    to its chord). line (k, 2), z (k,) its ground heights. Pure."""
    line = np.asarray(line, np.float64)[:, :2]
    z = np.asarray(z, np.float64)
    arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    total = float(arc[-1])
    if total < 0.5:
        return []
    if total <= JERSEY_LEN_M:
        starts, length = [0.0], total
    else:
        count = int(math.ceil((total - JERSEY_LEN_M) / JERSEY_STEP_M)) + 1
        starts, length = list(np.linspace(0.0, total - JERSEY_LEN_M, count)), JERSEY_LEN_M
    out = []
    for sa in starts:
        sb = sa + length
        a = np.array([np.interp(sa, arc, line[:, 0]), np.interp(sa, arc, line[:, 1])])
        b = np.array([np.interp(sb, arc, line[:, 0]), np.interp(sb, arc, line[:, 1])])
        za, zb = float(np.interp(sa, arc, z)), float(np.interp(sb, arc, z))
        f = np.array([b[0] - a[0], b[1] - a[1], zb - za])
        n = float(np.linalg.norm(f))
        if n < 1e-6:
            continue
        # (its middle at the lower of its ends' and its middle's ground, sunk JERSEY_SINK_M: never floating on a
        # crest or a bump)
        zm = min(float(np.interp(0.5 * (sa + sb), arc, z)) - 0.5 * (za + zb), 0.0) + 0.5 * (za + zb) - JERSEY_SINK_M
        out.append((0.5 * (a + b), zm, f / n))
    return out


def jersey_rows(frame, cell_fn, box, elev):
    """The track walls as the guard rails' rows (guardrails.forest_rows' layout: group G_JERSEY, x, y, z, yaw, 0,
    height, rank, then the basis: the jersey model runs along its y axis), the modules whose centre is in box: jersey
    barriers continuous along each mapped wall (Jon, 2026-11-07: the noise-wall blocks stood with gaps between and
    under them)."""
    import guardrails
    x0, y0, x1, y1 = map(float, box)
    tr = gather(frame, cell_fn, box)
    if not tr.race:
        return []
    out = []
    for owner, line in wall_runs(tr):
        la, lo, _ = frame.enu_to_geodetic(line[:, 0], line[:, 1], np.zeros(len(line)))
        _, _, z = frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))
        for c, zc, f in jersey_modules(line, np.asarray(z, np.float64)):
            if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
                continue
            up = np.array([0.0, 0.0, 1.0]) - f[2] * f
            up /= max(float(np.linalg.norm(up)), 1e-9)
            back = np.cross(up, f)
            back /= max(float(np.linalg.norm(back)), 1e-9)
            c0, c1 = -back, f                   # (the jersey model runs along its y axis: guardrails.forest_rows)
            yaw = math.degrees(math.atan2(float(f[1]), float(f[0])))
            out.append([guardrails.G_JERSEY, round(float(c[0]), 3), round(float(c[1]), 3), round(float(zc), 3), yaw,
                        0.0, guardrails.HEIGHTS[guardrails.G_JERSEY], _hash01(owner, round(c[0], 1), round(c[1], 1)),
                        float(c0[0]), float(c0[1]), float(c0[2]), float(c1[0]), float(c1[1]), float(c1[2]),
                        float(up[0]), float(up[1]), float(up[2])])
    return out


def tile_jersey_rows(frame, box, md, elev):
    """jersey_rows() for one road tile from mapdata md."""
    def cell_fn(key, layers):
        return md.cell(key, layers=layers, kind="demand").layers
    return jersey_rows(frame, cell_fn, box, elev)


def mapped_rows(tr):
    """Rule 1: the mapped catch fences, walls and tyre barriers near the raceways."""
    out = []
    for fid, t, kind, line in tr.barriers:
        if kind is not None:
            continue                    # (fences.py draws mapped chain-link and noise walls)
        if t in CATCH_FENCE_TYPES:
            reach, maker = MAPPED_FENCE_M, "fence"
        elif t in WALL_TYPES:
            reach, maker = MAPPED_WALL_M, "wall"
        elif t in TYRE_TYPES:
            reach, maker = MAPPED_TYRE_M, "tyre"
        else:
            continue
        pts, tan, _ = resample(line, STEP_M)
        if len(pts) < 2:
            continue
        edge, _, unit, _ = tr.race_segs.nearest(pts)
        near = edge <= reach
        if maker != "tyre":             # a catch fence / wall runs along the track, not across a field to it
            near &= np.abs((tan * unit).sum(1)) >= PARALLEL_DOT
        for a, b in _runs(near, 3 if maker != "tyre" else 1):
            piece = pts[a:b]
            if maker == "fence":
                out += _catch_panels(piece, tr, fid)
            elif maker == "wall":
                pass                    # (jersey barrier runs: wall_runs / jersey_rows, the guard rails' rows)
            else:
                out += _tyre_line(resample(piece, TYRE_STEP)[0] if len(piece) > 1 else piece, fid, tr)
    return out


def inferred_fence_lines(tr):
    """Rule 2: [(owner, line (k, 2))] of catch fences along access roads beside a raceway."""
    out = []
    for fid, line, half in zip(tr.access_ids, tr.access, tr.access_half):
        pts, tan, _ = resample(line, STEP_M)
        if len(pts) < 2:
            continue
        edge, _, unit, foot = tr.race_segs.nearest(pts)
        gap = edge - half
        ok = (gap >= INFER_MIN_M) & (gap <= INFER_MAX_M) & (np.abs((tan * unit).sum(1)) >= PARALLEL_DOT)
        if tr.barrier_segs.n:
            ok &= tr.barrier_segs.edge(pts) > MAPPED_NEAR_M + half
        for a, b in _runs(ok, int(math.ceil(INFER_RUN_M / STEP_M)) + 1):
            p, f = pts[a:b], foot[a:b]
            to = f - p
            to /= np.maximum(np.hypot(to[:, 0], to[:, 1]), 1e-9)[:, None]
            out.append((f"{fid}@{a}", p + to * (half + FENCE_OFF_M)))
    return out


def curvature(pts):
    """(radius (k,), unit vector toward the centre (k, 2)) at points (k, 2) every STEP_M, from the points
    CURVE_HALF_M before and after; inf at the ends and on straights."""
    k = len(pts)
    h = max(1, int(round(CURVE_HALF_M / STEP_M)))
    r, c = np.full(k, np.inf), np.zeros((k, 2))
    if k < 2 * h + 1:
        return r, c
    a, b, d = pts[:-2 * h], pts[h:-h], pts[2 * h:]
    ab, bd, ad = b - a, d - b, d - a
    cross = ab[:, 0] * bd[:, 1] - ab[:, 1] * bd[:, 0]
    la, lb, lc = np.hypot(*ab.T), np.hypot(*bd.T), np.hypot(*ad.T)
    with np.errstate(divide="ignore", invalid="ignore"):
        rad = la * lb * lc / (2.0 * np.abs(cross))
    rad[~np.isfinite(rad)] = np.inf
    tang = ad / np.maximum(lc, 1e-9)[:, None]
    left = np.column_stack((-tang[:, 1], tang[:, 0]))
    inward = left * np.sign(cross)[:, None]          # (cross > 0: a left turn, the centre on the left)
    r[h:-h], c[h:-h] = rad, inward
    return r, c


JOIN_M = 1.5                # m between two raceway ways' ends that join them into one line
LOOP_MAX_M = 400.0          # m: a closed raceway loop shorter than this (skidpad, autocross ring) gets no tyre walls


def _length(line):
    return float(np.hypot(*np.diff(np.asarray(line)[:, :2], axis=0).T).sum())


def race_chains(tr):
    """[(owner, line (k, 2), half width, closed)] of the raceway ways joined end to end where exactly two ways' ends
    meet (within JOIN_M), so a corner across a way junction is measured whole."""
    lines = [np.asarray(l, np.float64)[:, :2] for l in tr.race]
    n = len(lines)
    ends = [(i, e) for i in range(n) for e in (0, 1)]

    def pt(i, e):
        return lines[i][0] if e == 0 else lines[i][-1]

    nb = {}
    for i, e in ends:
        m = [(j, f) for j, f in ends if j != i and np.hypot(*(pt(i, e) - pt(j, f))) <= JOIN_M]
        nb[(i, e)] = m[0] if len(m) == 1 else None
    used, out = set(), []

    def walk(start, i, e_out):
        """[(way, reversed)] followed from way i leaving by its end e_out, and whether it came back to start."""
        seq = []
        while True:
            nxt = nb.get((i, e_out))
            if nxt is None:
                return seq, False
            j, f = nxt
            if j == start:
                return seq, True
            if j in used:
                return seq, False
            used.add(j)
            seq.append((j, f == 1))
            i, e_out = j, 1 - f

    for s in range(n):
        if s in used:
            continue
        used.add(s)
        fwd, closed = walk(s, s, 1)
        back = []
        if not closed:
            b, _ = walk(s, s, 0)
            back = [(j, not r) for j, r in reversed(b)]
        parts = back + [(s, False)] + fwd
        line = np.vstack([lines[j][::-1] if r else lines[j] for j, r in parts])
        half = float(np.mean([tr.race_half[j] for j, _ in parts]))
        closed = closed or (len(line) > 2 and float(np.hypot(*(line[0] - line[-1]))) <= JOIN_M)
        out.append(("+".join(tr.race_ids[j] for j, _ in parts[:3]), line, half, closed))
    return out


def tyre_wall_lines(tr, fence_lines=()):
    """Rule 3: [(owner, line (k, 2))] of tyre walls outside the tight corners with short runoff."""
    obstacles = Segs([b[3] for b in tr.barriers if b[1] in BARRIER_ANY] + [ln for _, ln in fence_lines],
                     [0.0] * (sum(1 for b in tr.barriers if b[1] in BARRIER_ANY) + len(fence_lines)))
    out = []
    for fid, line, half, closed in race_chains(tr):
        if closed and _length(line) < LOOP_MAX_M:
            continue                # (a skidpad or autocross ring, not a corner of the circuit)
        pts, _, _ = resample(line, STEP_M)
        rad, inward = curvature(pts)
        tight = rad < TIGHT_R_M
        for a, b in _runs(tight, int(math.ceil(TIGHT_RUN_M / STEP_M)) + 1):
            outward = -inward[a:b]
            base = pts[a:b] + outward * half              # the carriageway's outer edge
            dist = []
            for p, o in zip(base, outward):
                ray = p + o * np.arange(0.5, RUNOFF_SHORT_M + 0.5, 0.5)[:, None]
                hit = np.zeros(len(ray), bool)
                if tr.road_segs.n:
                    hit |= tr.road_segs.edge(ray) <= 0.0
                if obstacles.n:
                    hit |= obstacles.edge(ray) <= 0.5
                # the track's own other parts (a hairpin's other leg), not this stretch's carriageway
                hit |= tr.race_segs.edge(ray) <= 0.0
                j = np.flatnonzero(hit)
                dist.append(j[0] * 0.5 + 0.5 if len(j) else np.inf)
            dist = np.array(dist)
            short = np.isfinite(dist)
            if short.sum() < max(2, 0.5 * len(dist)):
                continue
            off = float(np.median(dist[short])) - TYRE_FRONT_M
            if off < CLEAR_M + MARGIN_M:
                continue
            out.append((f"{fid}@{a}", pts[a:b] + outward * (half + off)))
    return out


STAND_FILL = 0.8            # a footprint filling this share of its oriented box is a straight stand: one row
STAND_PIECE_MIN = 0.6       # a curved stand's piece: at least this share of its model inside the footprint
STAND_MAX_SCALE, STAND_MIN_SCALE = 2.5, 0.3
STAND_RACE_HALF_M = 10.0    # m: the half width the stands keep off a raceway whose width is not given (racetypes'
                            # widest default, a drag strip's 20 m); buildings.py's RaceLines carry their spec's half


STAND_PIECES_MIN = 2         # models at least along a curved stand's edge (smaller ones if need be)
STAND_CLASSES = {"grandstand", "stadium"}
# (the user 2026-11-07, Bristol in game: one-size models round a bowl read as a sawtooth. Off: no stand model; a footprint
# race_stand claims is still left out of the buildings (never a lid or a building on the track), so its seats are bare
# ground for now. BNE_RACE_STANDS=on draws them)
STANDS_ON = os.environ.get("BNE_RACE_STANDS", "off") == "on"
STAND_AREA_M2 = 10000.0     # m2: an unclassed footprint this big near a raceway is its seating (pit garages: ~6000)


def _ring(ring):
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(ring) >= 2 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    return ring


def _area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _inside(ring, pts):
    """Even-odd point in polygon for points (k, 2)."""
    pts = np.asarray(pts, np.float64).reshape(-1, 2)
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    a, b = ring[None, :, :], np.roll(ring, -1, axis=0)[None, :, :]
    cond = (a[..., 1] > y) != (b[..., 1] > y)
    xi = a[..., 0] + (y - a[..., 1]) * (b[..., 0] - a[..., 0]) / np.where(b[..., 1] == a[..., 1], 1e-12,
                                                                          b[..., 1] - a[..., 1])
    return (cond & (x < xi)).sum(1) % 2 == 1


def _box_pts(c, ax, front, length, depth, step=4.0):
    """Points round and through a model's box (centre, long axis, front unit, length, depth)."""
    u = np.linspace(-0.5, 0.5, max(2, int(length / step) + 1))
    v = np.linspace(-0.5, 0.5, max(2, int(depth / step) + 1))
    uu, vv = np.meshgrid(u, v)
    return c + uu.reshape(-1, 1) * length * ax + vv.reshape(-1, 1) * depth * front


def stand_pieces(ring, race_lines, halves=None, as_curved=False):
    """The grandstand models on footprint ring (k, 2) near a raceway centreline (race_lines: [(k, 2)]):
    [(centre (2,), long axis unit (2,), scale, front unit (2,): toward the track)], [] when none fits. A footprint
    near a rectangle (STAND_FILL of its oriented box) gets one straight row fitted to the box; a curved or irregular
    one (Daytona's and Talladega's kilometre of seats round the trioval) short straight pieces along its track-facing
    edge, each one STAND_PIECE_MIN inside the footprint. No model comes within the track's STAND_RACE_HALF_M +
    CLEAR_M: one that would is dropped. Shared with buildings.tile_buildings (replaces_grandstand)."""
    ring = _ring(ring)
    if len(ring) < 3 or not race_lines:
        return []
    cen = Segs(race_lines, [0.0] * len(race_lines))
    if halves is None:
        halves = [getattr(ln, "half", None) for ln in race_lines]
    band = Segs(race_lines, [STAND_RACE_HALF_M if h is None else float(h) for h in halves])
    d, _, _, foot = cen.nearest(ring)
    j = int(np.argmin(d))
    if not np.isfinite(d[j]) or d[j] > GRANDSTAND_REACH_M + DEFAULT_RACE_W / 2.0:
        return []
    c = ring.mean(0)
    _, v = np.linalg.eigh(np.cov((ring - c).T))
    ax = v[:, 1]
    across = np.array([-ax[1], ax[0]])
    pa, pc = (ring - c) @ ax, (ring - c) @ across
    length, depth = float(pa.max() - pa.min()), float(pc.max() - pc.min())
    if length < 8.0 or depth < 4.0:
        return []
    area = _area(ring)
    out = []
    curved = as_curved or area < STAND_FILL * length * depth
    if not curved:
        c = c + ax * (pa.max() + pa.min()) / 2.0 + across * (pc.max() + pc.min()) / 2.0
        front = across if np.dot(foot[j] - c, across) >= 0 else -across
        n = max(1, int(round(length / (STAND_LEN * min(STAND_MAX_SCALE, max(STAND_MIN_SCALE, depth / STAND_DEPTH))))))
        s = max(STAND_MIN_SCALE, min(STAND_MAX_SCALE, depth / STAND_DEPTH, length / (n * STAND_LEN)))
        for k in range(n):
            out.append((c + ax * ((k + 0.5) / n - 0.5) * length, ax, s, front))
    else:
        # a curved band: its depth from area over half its outline, its front edge the outline's stretch nearest the
        # track, walked in order and cut into pieces of one model's length at that depth's scale
        dense = resample(np.vstack([ring, ring[:1]]), 2.0)[0]
        perim = float(np.hypot(*np.diff(np.vstack([ring, ring[:1]]), axis=0).T).sum())
        dep = 2.0 * area / max(perim, 1e-9)
        if dep < 4.0:
            return []
        s = max(STAND_MIN_SCALE, min(STAND_MAX_SCALE, dep / STAND_DEPTH))
        dd = cen.nearest(dense)[0]
        near = dd <= float(dd.min()) + dep / 2.0
        runs = _runs(np.r_[near, near], 2)
        best = max(runs, key=lambda r: r[1] - r[0]) if runs else None
        if best is None:
            return []
        idx = np.arange(best[0], min(best[1], best[0] + len(dense))) % len(dense)
        edge = dense[idx]
        L = float(np.hypot(*np.diff(edge, axis=0).T).sum()) if len(edge) > 1 else 0.0
        # (at least STAND_PIECES_MIN pieces along the edge: a bowl's piece, its seats deeper than its stretch is long)
        s = max(STAND_MIN_SCALE, min(s, L / (STAND_PIECES_MIN * STAND_LEN)))
        plen, pdep = STAND_LEN * s, STAND_DEPTH * s
        n = int(L // plen)
        if n < 1:
            return []
        pts, _, _ = resample(edge, plen / 2.0)
        for k in range(n):
            a_, b_ = pts[2 * k], pts[min(2 * k + 2, len(pts) - 1)]
            ch = b_ - a_
            cl = float(np.hypot(*ch))
            if cl < plen * 0.5:
                continue
            axk = ch / cl
            nk = np.array([-axk[1], axk[0]])
            mid = (a_ + b_) / 2.0
            _, _, _, f = cen.nearest(mid[None])
            front = nk if np.dot(f[0] - mid, nk) >= 0 else -nk
            ck = mid - front * pdep / 2.0
            out.append((ck, axk, s, front))
    keep = []
    for ck, axk, sk, front in out:
        # set back from the track until the whole model keeps off its band (a footprint mapped to the wall's edge:
        # Daytona's and Talladega's reach 7-10 m from the centreline), as long as it still stands on the footprint
        ln, dp = STAND_LEN * sk, STAND_DEPTH * sk
        c0 = ck
        for _ in range(4):
            gap = float(band.edge(_box_pts(ck, axk, front, ln, dp)).min()) - CLEAR_M
            if gap >= 0.0:
                break
            ck = ck + front * (gap - 0.5)
        else:
            continue
        # (a straight stand: set back no more than its depth; a curved one's piece: still on its footprint)
        if (float(np.hypot(*(ck - c0))) <= dp) if not curved else \
                _inside(ring, _box_pts(ck, axk, front, ln, dp)).mean() >= STAND_PIECE_MIN:
            keep.append((ck, axk, sk, front))
    if not keep and not curved:
        # (no straight row fits: a box-like piece of a bowl, its long axis across the track; pieces along its
        # track-facing edge instead)
        return stand_pieces(ring, race_lines, halves, as_curved=True)
    return keep


def on_band(ring, race_lines, halves=None):
    """Whether footprint ring (k, 2) reaches onto a raceway's carriageway or its CLEAR_M shoulder (the band no
    building may stand on, racing surface first): its outline (every 2 m) within the band, or the track's line
    (every 2 m) inside it (a bowl's roof over the whole oval)."""
    ring = _ring(ring)
    if len(ring) < 3 or not race_lines:
        return False
    if halves is None:
        halves = [getattr(ln, "half", None) for ln in race_lines]
    band = Segs(race_lines, [STAND_RACE_HALF_M if h is None else float(h) for h in halves])
    lo, hi = ring.min(0), ring.max(0)
    near = [np.asarray(ln, np.float64)[:, :2] for ln in race_lines]
    near = [ln for ln in near if len(ln) >= 2 and not ((ln.max(0) < lo - 60).any() or (ln.min(0) > hi + 60).any())]
    if not near:
        return False
    dense = resample(np.vstack([ring, ring[:1]]), 2.0)[0]
    if float(band.edge(dense).min()) < CLEAR_M:
        return True
    return any(_inside(ring, resample(ln, 2.0)[0]).any() for ln in near)


def race_stand(ring, cls, race_lines, halves=None):
    """What a footprint near a raceway becomes: "stand" (racefurniture draws grandstand models on it, open front to
    the track: a mapped grandstand, or any footprint on the band that the models fit), "drop" (on the band and no
    model fits: nothing, never a lid over the track) or None (an ordinary building)."""
    c = str(cls or "").strip().lower()
    ob = on_band(ring, race_lines, halves)
    # seating by its class, or by its size: a footprint without a class this big within reach of the track
    # (Atlanta's bleachers, 42,000 m2 along the front stretch, mapped building=yes, 2026-11-05)
    gs = c in STAND_CLASSES or (c in ("", "yes", "none") and _area(_ring(ring)) >= STAND_AREA_M2)
    if not (gs or ob):
        return None
    if stand_pieces(ring, race_lines, halves):
        return "stand"
    return "drop" if ob else None


def replaces_grandstand(ring, race_lines, halves=None):
    """Whether buildings.py leaves the grandstand footprint ring out (racefurniture draws it)."""
    return bool(stand_pieces(ring, race_lines, halves))


def stand_rows(tr):
    """Rule 4: the grandstand models (group, x, y, yaw, height, owner, scale)."""
    out = []
    for fid, ring in tr.stands:
        for p, ax, s, front in stand_pieces(ring, tr.race, tr.race_half):
            yaw = _yaw(front[1], -front[0])       # local +x along the stand, local +y (the open front) to the track
            out.append((G_STAND, p[0], p[1], yaw, STAND_H * s, fid, s))
    return out


# ---------------------------------------------------------------------------------------------------- rows

def plan(tr):
    """Every candidate (group, x, y, yaw, height, owner[, scale]) round the track, before the tile's box."""
    if not tr.race:
        return []
    fences_ = inferred_fence_lines(tr)
    out = mapped_rows(tr)
    for owner, line in fences_:
        out += _catch_panels(line, tr, owner)
    for owner, line in tyre_wall_lines(tr, fences_):
        out += _tyre_line(resample(line, TYRE_STEP)[0], owner, tr)
    if STANDS_ON:
        out += stand_rows(tr)
    return out


def rows(frame, cell_fn, box, elev):
    """Forest rows [group, x, y, z, yaw, scale, height, rank] of the racing furniture whose centre is in box."""
    x0, y0, x1, y1 = map(float, box)
    tr = gather(frame, cell_fn, box)
    pending = [p for p in plan(tr) if x0 <= p[1] < x1 and y0 <= p[2] < y1]
    if not pending:
        return []
    xy = np.array([[p[1], p[2]] for p in pending], np.float64)
    lat, lon, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    _, _, z = frame.geodetic_to_enu(lat, lon, np.asarray(elev(lat, lon), np.float64))
    out = []
    for p, zz in zip(pending, np.asarray(z, np.float64)):
        group, px, py, yaw, height = p[:5]
        lift = CATCH_LIFT if group == G_CATCH else STAND_ZOFF * p[6] if group == G_STAND else 0.0
        out.append([group, round(px, 2), round(py, 2), round(float(zz) + lift, 2), round(yaw, 1) % 360.0,
                    1.0, round(height, 2), 0.0])
    return out


def tile_rows(frame, box, md, elev):
    """rows() for one road tile from mapdata md."""
    def cell_fn(key, layers):
        return md.cell(key, layers=layers, kind="demand").layers
    return rows(frame, cell_fn, box, elev)
