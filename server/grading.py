"""Ground graded under roads (docs/ROADMAP.md v1.6; docs/research/2026-10-11-official-road-study.md §3, §5.3):
the physics block and the near mesh tiles sample one graded elevation, so tyres, DecalRoads and the
visible ground agree.

Per road (roads.road_specs; bridges, tunnels and off-ground segments are left out by it):
- along: the centreline height is the DEM sampled every PROFILE_STEP m and smoothed (Gaussian,
  SIGMA_M m; dirt roads DIRT_SIGMA_M), so grades stay real and small bumps go (the official maps:
  |2nd difference| per 10 m 0.02-0.1 m);
- across: flat over the core (the carriageway as a city street would have it, parking included, plus
  SHOULDER_M, or the sidewalk plus SIDEWALK_MARGIN_M on the classes that get sidewalks in towns, so a
  raised sidewalk slab sits on flat ground), then back to the natural ground over a transition of 2x the height difference (a
  1:2 cut or fill slope), clamped to [T_MIN, T_MAX] m;
- several roads at one point (junctions, divided carriageways) blend their targets by inverse distance
  from each centreline (BLEND_R), so they meet in a gradual slope.
The core width never depends on the land cover (it is taken as urban for every built road), and
each map cell's plan is made once per anchor and cached, so the block and the tiles built later from
the same anchor always agree.

GradedDem(dem, grader) is a drop-in for dem.Dem where only sample() is graded; use it for the block
and the levels <= 0 tiles only (coarser tiles sink under the block and cover far more ground).
"""
import logging
import math
import os
import threading
import time

import numpy as np

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.grading")

VERSION = 2
PROFILE_STEP = 2.0          # m between profile samples
SIGMA_M = 12.0              # m, profile smoothing of built roads
DIRT_SIGMA_M = 5.0          # m, profile smoothing of gravel / dirt roads
SHOULDER_M = 1.0            # m of flat shoulder beyond the carriageway, each side
SIDEWALK_MARGIN_M = 0.5     # m of flat ground beyond a sidewalk (roads.SIDEWALK_CLASSES)
LONG_PART_M = 3000.0        # a road part longer than this is planned per cell, only within CLIP_MARGIN_M of the cell
CLIP_MARGIN_M = 400.0
SPREAD_CELLS = 16           # plans(): more map cells in the points' bbox: only the cells around the points
T_MIN, T_MAX = 5.0, 12.0    # m, transition back to the natural ground (5: roads close together meet gently)
SLOPE_RUN = 2.0             # transition = SLOPE_RUN x the height difference (1:2 slope)
VERTEX_STEP = 4.0           # m between the plan's vertices (segments for the grading pass; linear between: at 16 m a
                            # crest or a sag was a kink every 16 m, 'bumpy roads, worst uphill and at speed' on r/BeamNG,
                            # 2026-10-17: a San Francisco street's slope changed 4.4 % rms per 2 m, 1.2 % at 4 m;
                            # grading a 1 km block 0.06 -> 0.14 s)
BUCKET = 32.0               # m, query-point buckets
PAIR_BATCH = 2_000_000      # (segment, point) pairs graded at once (bounds the temporaries, ~200 MB)
ROAD_LIFT = 0.04            # m the physics ground is raised under roads (see Grader.apply)
BLEND_R = 4.0               # m: roads near each other blend their targets by 1 / (1 + (d / BLEND_R)^2)
PROFILE_ZOOM = 15           # DEM zoom of the profiles (one zoom for every caller)
WALL_CUT_M = 2.0            # a road cut deeper than this into the ground: a retaining wall at the core's edge
WALL_T_M = 0.8              # (roadmesh.retaining_walls) and the ground at its natural height right behind it, over
                            # this; on a 1:2 slope Central Park's sunken transverse was a grassy ditch between its
                            # stone walls (the user, 2026-10-17)
WALL_FLAT_M = 2.0           # ... after one terrain grid step at the road's level behind the wall: rising at the wall
                            # itself, the 2 m grid's triangles stood as dark wedges in front of its face; the wall's
                            # top (roadmesh.WALL_THICK_M) covers the strip
TUNNEL_TAG = ":tun"          # in the fid of the tunnels' own plans (open sections, mouths): apply(roads_only) skips them
MOUTH_PLAN_M = 4.0          # m into a tunnel from a portal its floor is a grading plan too (Grader._mouth_plans)
TUNNEL_KEEP_IN_M = 3.0      # (roads.LOW_HOLE_M: a low arch's mouths, flat at its floor)
TUNNEL_KEEP_M = 5.0         # m beside a covered tunnel's road where the ground over it is left ungraded ...
OVER_TUNNEL_SMALL_M = 0.5   # ... but for a change smaller than this (a road crossing over it, graded flush)
WALL_AHEAD_M = 2.5          # ... only beside a segment or this little past its end (a joint between two: VERTEX_STEP)
RETRY_S = 60.0              # s before a cell whose road data failed is asked for again

# Overpass approach ramps (from KyzoFox's 1.5.4, PR #64). A short bridge over a road had its ends on the ground on both
# sides (the DEM is too coarse for the real approach embankments), and roads.deck_profile_once caps how steeply a deck
# climbs from its ends: the deck over I-20 near Temple, GA reached ~2.5 m over the lanes (trucks hit it, 2026-10-19).
# The roads leading onto such a bridge are now graded up to the height the deck needs, over a smooth ramp, as real
# approach fills are: the bridge's ends (sampled from this graded ground) then sit high enough.
APPROACH_ON = os.environ.get("BNE_APPROACH_RAMPS", "on") != "off"
APPROACH_GRADE = float(os.environ.get("BNE_APPROACH_GRADE", "0.05"))   # steepest grade of a ramp (its middle)
APPROACH_MAX_LIFT = 10.0    # m an abutment is raised at most
APPROACH_MIN_LIFT = 0.5     # m: less is left as it is
APPROACH_MIN_RUN = 30.0     # m: the shortest ramp
APPROACH_MAX_RUN = 320.0    # m: the longest ramp
APPROACH_TOUCH_M = 2.5      # m: a road whose end is this close to an abutment leads onto the bridge
APPROACH_MIN_CROSS_M = 6.0  # m: a road crossing a deck closer than this to its end is a junction at grade (not
                            # under it)
APPROACH_FLAT_M = 20.0      # m from an abutment the deck needs its full clearance (beyond: it may fall at DECK_GRADE)
APPROACH_MARGIN_M = 0.3     # m over the clearance (the deck profile's smoothing takes a little off near its ends)
APPROACH_SKIP = ("motorway", "trunk")   # never raised (nor their links): the overpasses over them are what is
APPROACH_GAP_M = 1.0        # m of flat ground kept between a ramp's fill slope and the road beside it
APPROACH_CELL_M = 450.0     # m: a plan's end this far from a cell cannot raise what that cell's queries see
APPROACH_UNDER_FREE_M = 30.0   # m from an abutment along its ramp where a road passing under the bridge does not cap
                            # the ramp (the abutment stands between); beyond, it does: a ramp curving back over the frontage
                            # road under its bridge built its fill across that road, a ribbon of ground over the lanes
                            # (KyzoFox 1.5.5: West Monroe, LA; Meridian and Newton, MS, 2026-10-20)
APPROACH_CAP_RUN = 1.0      # a ramp's fill falls 1:1 to a road beside it (its edge stands on the ramp's core: Grader.apply)
APPROACH_FLOOR_M = 0.25     # m: where a ramp raises its road more than this, the ground under the road's core is never
                            # lower than the road (Grader.apply: blended with the roads around, the ground at Nutland
                            # Road's abutment in Monroe, LA sat 3.5 m under its raised road, KyzoFox 1.5.5)
UNDERPASS_MAX_M = 6.0       # m a road passing under a major road's bridge is lowered at most (_lower_underpasses) ...
UNDERPASS_GRADE = 0.06      # ... on cosine ramps no steeper than this
UNDERPASS_FLAT_M = 8.0      # ... at its full depth this far either side of the bridge's edges
UNDERPASS_HOPS = 3
UNDERPASS_ON_COS = 0.82     # a piece goes on as the same road within ~35 degrees (not a road met at a junction)
UNDERPASS_KEEP_M = 40.0     # m from the bridge the major road over it keeps its own height (Grader._floor_ramps)
APPROACH_TWIN_M = 40.0      # m: a road onto another bridge's end this close to an abutment (a divided road's twin
                            # bridge) is raised with it and does not cap its ramp (KyzoFox 1.5.5)
APPROACH_CAP_GRADE = 0.10   # steepest grade a ramp falls at where a road beside it caps it (a cap alone dropped 3 m
                            # in 8 m by a side road north of the I-20 bridge near Temple, GA, 2026-10-20)


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (reflected at the ends). Pure."""
    z = np.asarray(z, np.float64)
    import fast
    if fast.AVAILABLE:          # the Rust loop (~7000 roads a 9-tile set; equal to ~1e-12 m)
        return fast._ef.smooth_profile(np.ascontiguousarray(z), float(step), float(sigma))
    if len(z) < 3 or sigma <= 0:
        return z.copy()
    r = int(math.ceil(3 * sigma / step))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) * step / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(z, r, mode="reflect") if len(z) > r else np.pad(z, r, mode="edge")
    return np.convolve(pad, k, mode="valid")


def resample(xy, step):
    """Points every `step` m along a polyline (xy (k, 2)), the last point kept, and their arc lengths."""
    import fast
    if fast.AVAILABLE and len(xy):
        return fast._ef.resample(np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), float(step))
    seg = np.hypot(*np.diff(xy, axis=0).T)
    s = np.r_[0.0, np.cumsum(seg)]
    if s[-1] <= 0:
        return xy[:1].copy(), np.zeros(1)
    t = np.r_[np.arange(0.0, s[-1], step), s[-1]]
    return np.stack([np.interp(t, s, xy[:, 0]), np.interp(t, s, xy[:, 1])], axis=1), t


class Plan:
    """One road's grading: vertices (n, 2) in the frame's ENU metres, their target heights, core half width."""
    __slots__ = ("xy", "z", "half", "bbox")

    def __init__(self, xy, z, half):
        self.xy, self.z, self.half = xy, z, float(half)
        pad = half + T_MAX
        self.bbox = (xy[:, 0].min() - pad, xy[:, 1].min() - pad, xy[:, 0].max() + pad, xy[:, 1].max() + pad)


def clip_polyline(xy, box):
    """The runs (each (k, 2), k >= 2) of polyline xy (k, 2) inside box (x0, y0, x1, y1), segments cut at its edges
    (Liang-Barsky). Pure."""
    x0, y0, x1, y1 = box
    runs, cur = [], None
    for i in range(len(xy) - 1):
        px, py = float(xy[i, 0]), float(xy[i, 1])
        dx, dy = float(xy[i + 1, 0]) - px, float(xy[i + 1, 1]) - py
        ta, tb, ok = 0.0, 1.0, True
        for p, q in ((-dx, px - x0), (dx, x1 - px), (-dy, py - y0), (dy, y1 - py)):
            if p == 0.0:
                if q < 0.0:
                    ok = False
                    break
                continue
            r = q / p
            if p < 0.0:
                if r > tb:
                    ok = False
                    break
                ta = max(ta, r)
            else:
                if r < ta:
                    ok = False
                    break
                tb = min(tb, r)
        if not ok or tb <= ta:
            if cur is not None:
                runs.append(cur)
                cur = None
            continue
        a = (px + dx * ta, py + dy * ta) if ta > 0.0 else (px, py)
        b = (px + dx * tb, py + dy * tb) if tb < 1.0 else (float(xy[i + 1, 0]), float(xy[i + 1, 1]))
        if cur is not None and ta == 0.0:
            cur.append(b)
        else:
            if cur is not None:
                runs.append(cur)
            cur = [a, b]
        if tb < 1.0:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    return [np.array(r, np.float64) for r in runs if len(r) >= 2]


def plan_road(frame, lonlat, spec, elev):
    """The Plan of one road part (lonlat (k, 2)); elev(lat, lon) -> m at PROFILE_ZOOM. Pure given elev."""
    return plan_roads(frame, [(lonlat, spec)], elev)[0]


def plan_roads(frame, parts, elev):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call). Pure given elev."""
    if not parts:
        return []
    ll = np.concatenate([lonlat for lonlat, _ in parts])
    E, N, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    prof, k = [], 0
    for lonlat, spec in parts:
        m = len(lonlat)
        xy, _ = resample(np.stack([E[k:k + m], N[k:k + m]], axis=1), PROFILE_STEP)
        k += m
        prof.append(xy if len(xy) >= 2 else None)
    pts = [xy for xy in prof if xy is not None]
    if not pts:
        return [None] * len(parts)
    allxy = np.concatenate(pts)
    la, lo, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], 0.0)
    zall = np.asarray(elev(la, lo), np.float64)
    out, k = [], 0
    for (lonlat, spec), xy in zip(parts, prof):
        if xy is None:
            out.append(None)
            continue
        z = zall[k:k + len(xy)]
        k += len(xy)
        out.append(_plan(xy, z, spec))
    return out


def _plan(xy, z, spec):
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile(z, PROFILE_STEP, SIGMA_M if built else DIRT_SIGMA_M)
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass)
    every = max(1, int(round(VERTEX_STEP / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) if built else spec.width
    return Plan(xy[keep], zs[keep], width / 2 + shoulder(spec))


def shoulder(spec):
    """m of flat ground beyond the carriageway edge, each side. Like the core width, never from the land
    cover: a road class that gets sidewalks in towns keeps a sidewalk-wide verge everywhere."""
    if spec.kind not in roadsmod.BUILT:
        return 0.0
    if spec.cls in roadsmod.SIDEWALK_CLASSES or (spec.cls == "trunk" and (not spec.oneway
                                                                          or roadsmod._arterial(spec))):
        return spec.walk + SIDEWALK_MARGIN_M
    return SHOULDER_M


class Grader:
    """The road plans of one anchor frame, per map cell, made once (worker threads; it fetches the road
    cells it lacks)."""

    def __init__(self, frame, mapdata, elev, widths=None):
        self.frame, self.md, self.elev, self.widths = frame, mapdata, elev, widths
        self.cells = {}
        self.failed = {}        # cell key -> monotonic time of the last failed fetch
        # (KyzoFox 1.5.5) cell key -> monotonic time its plans were made while a neighbouring cell's roads could not be
        # fetched (_cell_enu): kept for now, made again after RETRY_S. Before, one failed neighbour threw away every
        # ramp of the cell for the session: overpasses flat on the ground over I-20 near Eutaw, AL
        self.partial = {}
        self._tl = threading.local()    # (per thread: how many neighbour fetches failed, _cell_enu)
        self.lock = threading.Lock()
        self.key_locks = {}     # cell key -> Lock: one thread plans a cell, the others wait for it
        self.covers = {}        # cell key -> [(xy (k, 2), half width)]: the tunnels whose ground over them stays
        self.opens = {}         # cell key -> [(xy (k, 2), Spec)]: the tunnels' open sections (roads in the open)

    def _misses(self):
        return getattr(self._tl, "misses", 0)

    def _cell_plans(self, key):
        with self.lock:
            got = self.cells.get(key)
            t = self.partial.get(key)
            if got is not None and t is not None and time.monotonic() - t >= RETRY_S:
                del self.cells[key]         # (made without a neighbour's roads: again, now)
                self.partial.pop(key, None)
                self.covers.pop(key, None)
                self.opens.pop(key, None)
                got = None
            kl = self.key_locks.setdefault(key, threading.Lock())
        if got is not None:
            return got
        with kl:
            return self._plan_cell(key)

    def _plan_cell(self, key):
        with self.lock:
            got = self.cells.get(key)
            failed = self.failed.get(key)
        if got is not None:
            return got
        if failed is not None and time.monotonic() - failed < RETRY_S:
            return []               # offline: no block waits on the same failing fetch again
        miss0 = self._misses()
        try:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception as e:      # no road data: ungraded (asked again after RETRY_S)
            log.debug("grading: roads of %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.monotonic()
            return []
        plans = []
        if feats is not None and len(feats):
            specs = roadsmod.road_specs(feats, None, self.widths)
            parts, ids = [], []
            box = self._clip_box(key)
            for i in range(len(feats)):
                if specs[i] is None:
                    continue
                for ring in roadsmod.ground_parts(feats, i):     # not bridges / tunnels
                    for r in self._near_cell(np.asarray(ring, np.float64), box):
                        parts.append((r, specs[i]))
                        ids.append(str(feats.ids[i]))
            pspecs = []
            for fid, part, p in zip(ids, parts, plan_roads(self.frame, parts, self.elev)):
                if p is not None:
                    plans.append((fid, p))
                    pspecs.append(part[1])
            if APPROACH_ON and plans:
                try:
                    self._lift_approaches(key, plans, pspecs)
                except Exception as e:      # ungraded approaches rather than no grading
                    log.debug("grading: approaches of %s: %s: %s", key, type(e).__name__, e)
                try:
                    self._lower_underpasses(key, plans, pspecs)
                except Exception as e:      # (as planned rather than no grading)
                    log.debug("grading: underpasses of %s: %s: %s", key, type(e).__name__, e)
            secs = self._tunnel_sections(feats, key, [p for _f, p in plans])
            plans.extend((fid + TUNNEL_TAG, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                         for fid, spec, xy, z, k in secs if k == 0)
            plans.extend(self._mouth_plans(secs))
            covers = [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, k in secs if k]
            opens = [(xy, spec) for _f, spec, xy, _z, k in secs if k == 0]
        else:
            covers, opens = [], []
        with self.lock:
            if self._misses() > miss0 and key not in self.cells:
                self.partial[key] = time.monotonic()
            self.cells.setdefault(key, plans)
            self.covers.setdefault(key, covers)
            self.opens.setdefault(key, opens)
            return self.cells[key]

    def _clip_box(self, key):
        """The cell's box in the frame (east / north), CLIP_MARGIN_M wider each side; None for a key without bounds."""
        if not hasattr(key, "bounds"):
            return None
        lon0, lat0, lon1, lat1 = key.bounds()
        e, n, _ = self.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                             0.0)
        e, n = np.asarray(e), np.asarray(n)
        m = CLIP_MARGIN_M
        return float(e.min()) - m, float(n.min()) - m, float(e.max()) + m, float(n.max()) + m

    def _near_cell(self, ring, box):
        """A road part (lon/lat ring) as planned for a cell: as it is, or, longer than LONG_PART_M, the pieces of it
        within the cell's box (each its own plan). A cell's plans sampled the ground along every road through it
        to its ends: on the Bonneville salt flats, tracks one segment 27 km long, ~2,500 DEM tiles for one road
        tile's four cells (2026-10-20). The profile's smoothing (SIGMA_M) reaches a few tens of metres: the plan
        within the cell is the same."""
        if box is None or len(ring) < 2:
            return [ring]
        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
        if float(np.hypot(*np.diff(xy, axis=0).T).sum()) <= LONG_PART_M:
            return [ring]
        out = []
        for run in clip_polyline(xy, box):
            la, lo, _ = self.frame.enu_to_geodetic(run[:, 0], run[:, 1], 0.0)
            out.append(np.stack([np.asarray(lo, np.float64), np.asarray(la, np.float64)], axis=1))
        return out

    def _lift_approaches(self, key, plans, specs):
        """Raise, in place, the plans of the roads leading onto overpasses (APPROACH_ON). A road is raised where one of
        its ends is a bridge's abutment that must go up for the deck to clear the road under it (_end_lift); the raise
        falls off along the road (approach_add) and never comes nearer another road than its fill slope allows. Only
        what the plan's own line and the map around its ends say counts, the same in every cell that plans it (a long
        road is planned in each cell it passes; two cells raising the same road differently drew the road under a
        raised ground, 2026-10-20 I-20 in Greene County, AL). Major roads (APPROACH_SKIP) are never raised."""
        # every plan end at once: near the cell, and on a bridge end of its own cell (most touch none)
        pe = np.array([[p.xy[0][:2], p.xy[-1][:2]] for _f, p in plans], np.float64).reshape(-1, 2)
        la, lo, _ = self.frame.enu_to_geodetic(pe[:, 0], pe[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        dla = np.maximum(np.maximum(lat0 - la, la - lat1), 0.0) * 111320.0
        dlo = np.maximum(np.maximum(lon0 - lo, lo - lon1), 0.0) * 111320.0 * np.cos(np.radians(la))
        ok = np.repeat([s is not None and s.cls not in APPROACH_SKIP and not _is_link(s) for s in specs], 2)
        touch = ok & (np.hypot(dla, dlo) <= APPROACH_CELL_M)
        homes = {}
        for j in np.flatnonzero(touch):
            homes.setdefault(mapcells.cell_of(float(la[j]), float(lo[j])), []).append(j)
        for home, js in homes.items():
            ends = self._cell_enu(home, ground=False)["ends"]
            js = np.asarray(js)
            if not len(ends):
                touch[js] = False
                continue
            d = np.hypot(pe[js, None, 0] - ends[None, :, 0], pe[js, None, 1] - ends[None, :, 1])
            touch[js] = (d <= APPROACH_TOUCH_M).any(axis=1)
        touch = touch.reshape(-1, 2)
        ramps = []
        for i, (fid, p) in enumerate(plans):
            if not touch[i].any():
                continue
            add = None
            for at_end in (False, True):
                e = p.xy[-1] if at_end else p.xy[0]
                if not touch[i, int(at_end)]:
                    continue
                got = self._end_lift(e)
                if got is None:
                    continue
                need, others = got
                # (KyzoFox 1.5.5) over the road's own planned height there, not the raw ground: the plan's smoothing
                # had cut Nutland Road's embankment in Monroe, LA 3.8 m down, so a ramp measured on the ground fell
                # that much short (2.1 m under the girders over I-20, a truck crashed into it)
                zend = float(p.z[-1] if at_end else p.z[0])
                lift = min(need - zend, APPROACH_MAX_LIFT)
                if lift < APPROACH_MIN_LIFT:
                    continue
                a = approach_add(p.xy, at_end, lift, p.half, others, p.z)
                if a is not None:
                    add = a if add is None else np.maximum(add, a)
            if add is not None:
                plans[i] = (fid, Plan(p.xy, p.z + add, p.half))
                if add.max() > APPROACH_FLOOR_M:
                    ramps.append((p.xy[:, :2].copy(), p.z + add, p.half, add > APPROACH_FLOOR_M))
        with self.lock:
            if not hasattr(self, "ramps"):
                self.ramps = {}
            self.ramps[str(key)] = ramps

    def _lower_underpasses(self, key, plans, specs):
        """(KyzoFox 1.5.5) Lower, in place, the plans of roads passing under a bridge of a major road (APPROACH_SKIP:
        never raised onto it) whose deck cannot clear them: a short motorway bridge has its ends on the ground either
        side, so its deck lies there too; I-20's bridges over a local road in Ruston, LA stood 1.3 m over it (the
        user's truck hit them, 2026-10-21). As real interchanges are built, the road dips under: down to the deck line
        (the straight line between the bridge's ends) less roads.DECK_ROAD_CLEAR, flat across the bridge and
        UNDERPASS_FLAT_M either side, then up on a cosine ramp of UNDERPASS_GRADE (along the road and the pieces it
        goes on as, the map splitting it at the bridge); never cut into a road beside it (approach_add's cap). Like
        the ramps, a pure function of the road and the map around it."""
        dips = []               # (plan index, distance along it from the crossing per vertex, depth, flat)
        cand = []
        for i, (fid, p) in enumerate(plans):
            sp = specs[i]
            if sp is None or sp.cls in APPROACH_SKIP or _is_link(sp) or len(p.xy) < 2:
                continue
            cand.append(i)
        if not cand:
            return
        bl, seen_c = [], set()
        for i in cand:
            xy = np.asarray(plans[i][1].xy, np.float64)[:, :2]
            la, lo, _ = self.frame.enu_to_geodetic(xy[[0, -1], 0], xy[[0, -1], 1], 0.0)
            for a_, b_ in zip(np.asarray(la), np.asarray(lo)):
                h = mapcells.cell_of(float(a_), float(b_))
                if str(h) in seen_c:
                    continue
                seen_c.add(str(h))
                c = self._cell_enu(h, ground=False)
                for line, (cls, w) in zip(c["bridges"], c.get("bcls", ())):
                    if cls in APPROACH_SKIP and not any(np.array_equal(line, x) for x, _w in bl):
                        bl.append((line, w))
        if not bl:
            return
        arcs, xys, lo_b, hi_b = {}, {}, {}, {}
        ends_at = {}            # (rounded end point) -> [(plan index, 0 | 1 its end)]: the pieces meeting there
        for i in cand:
            xy = np.asarray(plans[i][1].xy, np.float64)[:, :2]
            xys[i] = xy
            arcs[i] = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            lo_b[i], hi_b[i] = xy.min(0), xy.max(0)
            for e_, q in ((0, xy[0]), (1, xy[-1])):
                ends_at.setdefault((int(round(q[0])), int(round(q[1]))), []).append((i, e_))

        def meeting(q):
            # the pieces with an end within 1 m of q (the 3 x 3 rounded neighbourhood)
            kx, ky = int(round(q[0])), int(round(q[1]))
            out = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for m, m_end in ends_at.get((kx + dx, ky + dy), ()):
                        if np.hypot(*(xys[m][-1 if m_end else 0] - q)) < 1.0:
                            out.append((m, m_end))
            return out
        # (KyzoFox 1.5.5: every candidate against every bridge, and every piece searched at each hop, took 9.4 s of the
        # grading of Monroe's I-20 / US 165 interchange cell, and the terrain under the player 19-47 s in game: only the
        # pieces whose box reaches the bridge's, and the ends looked up)
        for line, w in bl:
            bs = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
            ze = None
            blo, bhi = line.min(0), line.max(0)
            for i in cand:
                if (hi_b[i] < blo).any() or (lo_b[i] > bhi).any():
                    continue
                p = plans[i][1]
                xy = xys[i]
                cr = roadsmod._crossings([line], [xy])
                if not cr:
                    continue
                s = arcs[i]
                seg = np.diff(s)
                if ze is None:
                    ze = self._z_at(line[[0, -1]])
                for _pi, sb, pt in cr:
                    deck = float(ze[0] + (ze[1] - ze[0]) * sb / max(bs[-1], 1e-9))
                    a, b = xy[:-1], xy[1:]
                    ab = b - a
                    L2 = np.maximum((ab * ab).sum(1), 1e-12)
                    t = np.clip(((pt - a) * ab).sum(1) / L2, 0.0, 1.0)
                    j = int(np.argmin(np.hypot(*(a + t[:, None] * ab - pt).T)))
                    sc = s[j] + t[j] * seg[j]
                    need = float(np.interp(sc, s, p.z)) - (deck - roadsmod.DECK_ROAD_CLEAR - APPROACH_MARGIN_M)
                    if need < APPROACH_MIN_LIFT:
                        continue
                    need = min(need, UNDERPASS_MAX_M)
                    flat = w / 2 + UNDERPASS_FLAT_M
                    dips.append((i, np.abs(s - sc), need, flat))
                    # the pieces it goes on as (sharing an end), up to UNDERPASS_HOPS pieces on
                    front = [(i, 0, sc), (i, 1, s[-1] - sc)]
                    done = {i}
                    for _hop in range(UNDERPASS_HOPS):
                        nxt = []
                        for k, at_end, off in front:
                            xk = xys[k]
                            e = xk[-1 if at_end else 0]
                            dk = (xk[-1] - xk[-2]) if at_end else (xk[0] - xk[1])     # out of piece k
                            for m, m_end in meeting(e):
                                if m in done:
                                    continue
                                xm = xys[m]
                                dm = (xm[1] - xm[0]) if m_end == 0 else (xm[-2] - xm[-1])   # into piece m
                                straight = float(dk @ dm) > UNDERPASS_ON_COS * float(np.hypot(*dk) * np.hypot(*dm))
                                if straight and specs[m].cls == specs[k].cls:
                                    sm = arcs[m]
                                    dd = off + (sm if m_end == 0 else sm[-1] - sm)
                                    dips.append((m, dd, need, flat))
                                    done.add(m)
                                    nxt.append((m, 1 - m_end, off + sm[-1]))
                        front = nxt
        if not dips:
            return
        # the major road over the cut stays at its height near it (its abutments): blended with the dipped road, I-20's
        # ends sank 0.9 m and its deck with them
        keep_pts = np.array([np.asarray(l, np.float64)[[0, -1]].mean(0) for l, _w in bl], np.float64)
        with self.lock:
            if not hasattr(self, "ramps"):
                self.ramps = {}
            kept = self.ramps.setdefault(str(key) + "#u", [])
        for i, (fid, p) in enumerate(plans):
            sp = specs[i]
            if sp is None or sp.cls not in APPROACH_SKIP or len(p.xy) < 2:
                continue
            xy = np.asarray(p.xy, np.float64)[:, :2]
            near = np.hypot(*(xy[:, None, :] - keep_pts[None]).transpose(2, 0, 1)).min(1) <= UNDERPASS_KEEP_M
            if near.any():
                kept.append((xy.copy(), np.asarray(p.z, np.float64).copy(), p.half, near))
        drop = {}
        for i, dist, need, flat in dips:
            run = need * math.pi / (2 * UNDERPASS_GRADE)
            u = np.clip((dist - flat) / run, 0.0, 1.0)
            d = need * 0.5 * (1.0 + np.cos(math.pi * u))
            drop[i] = d if i not in drop else np.maximum(drop[i], d)
        dpts = np.array([np.asarray(plans[k][1].xy, np.float64)[[0, -1], :2] for k in drop]).reshape(-1, 2)
        memo = {}               # (cell -> its ground roads with their boxes: _underpass_others, once per cell)
        for i, d in drop.items():
            fid, p = plans[i]
            xy = np.asarray(p.xy, np.float64)[:, :2]
            seg = np.diff(arcs[i])
            for o_line, oh in self._underpass_others(xy, p.half, fid, bl, dpts, memo):
                aa, bb = o_line[:-1], o_line[1:]
                dd = bb - aa
                LL = np.maximum((dd * dd).sum(1), 1e-12)
                tt = np.clip(((xy[:, None, :] - aa[None]) * dd[None]).sum(2) / LL[None], 0.0, 1.0)
                dist = np.hypot(*(xy[:, None, :] - (aa[None] + tt[..., None] * dd[None])).transpose(2, 0, 1)).min(1)
                d = np.minimum(d, np.maximum(0.0, (dist - p.half - oh - APPROACH_GAP_M) / APPROACH_CAP_RUN))
            for k in range(1, len(d)):           # (a cap eases the dip in, not a step)
                d[k] = min(d[k], d[k - 1] + APPROACH_CAP_GRADE * seg[k - 1])
            for k in range(len(d) - 2, -1, -1):
                d[k] = min(d[k], d[k + 1] + APPROACH_CAP_GRADE * seg[k])
            if d.max() >= APPROACH_MIN_LIFT:
                plans[i] = (fid, Plan(p.xy, p.z - d, p.half))

    def _z_at(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(self.elev(la, lo), np.float64)

    def _underpass_others(self, xy, half, fid, bridges, dipped=None, memo=None):
        """The ground roads near xy that an underpass's dip must not cut into: not the road itself, nor the major road
        whose bridge it passes under (its ground parts end at the bridge's ends, over the cut). dipped: the end points
        (n, 2) of the pieces dipped with it. [(line, half width)]."""
        dipped = np.zeros((0, 2)) if dipped is None else np.asarray(dipped, np.float64).reshape(-1, 2)
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        keys = set()
        for a, b in zip(np.asarray(la)[::4], np.asarray(lo)[::4]):
            keys.add(mapcells.cell_of(float(a), float(b)))
        ends = np.concatenate([np.asarray(b[[0, -1]]) for b, _w in bridges]) if bridges else np.zeros((0, 2))
        out, seen = [], set()
        lo_q, hi_q = xy.min(0) - 60.0, xy.max(0) + 60.0
        for k in sorted(keys, key=str):
            got = memo.get(str(k)) if memo is not None else None
            if got is None:
                gl = self._cell_enu(k)["ground"]
                got = (gl, np.array([l.min(0) for l, _o, _c in gl]).reshape(-1, 2),
                       np.array([l.max(0) for l, _o, _c in gl]).reshape(-1, 2))
                if memo is not None:
                    memo[str(k)] = got
            gl, glo, ghi = got
            near = np.flatnonzero(~((ghi < lo_q).any(1) | (glo > hi_q).any(1))) if len(gl) else ()
            for j in near:
                line, oh, cls = gl[j]
                kk = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), len(line))
                if kk in seen:
                    continue
                seen.add(kk)
                if cls in APPROACH_SKIP:
                    continue                    # (the major road over it, and its carriageways: above the cut)
                if len(ends) and min(np.hypot(*(ends - line[0]).T).min(), np.hypot(*(ends - line[-1]).T).min()) < 2.5:
                    continue
                if len(dipped) and np.hypot(*(line[[0, -1]][:, None, :] - dipped[None]).transpose(2, 0, 1)).min() < 1.0:
                    continue                    # (the road's own pieces, dipped with it)
                out.append((line, oh))
        return out

    def _floor_ramps(self, e, n, out, lift):
        """out raised to the raised approach roads' surfaces (+ lift) on their cores (APPROACH_FLOOR_M): a ramp's road
        stands on its fill, whatever the roads around pull the blended ground to. In place."""
        with self.lock:
            ramps = [r for v in getattr(self, "ramps", {}).values() for r in v]
        if not ramps or not len(e):
            return
        x0, y0, x1, y1 = e.min(), n.min(), e.max(), n.max()
        for xy, z, half, on in ramps:
            if xy[:, 0].max() + half < x0 or xy[:, 0].min() - half > x1 or xy[:, 1].max() + half < y0 or \
                    xy[:, 1].min() - half > y1:
                continue
            for j in np.flatnonzero(on[:-1] | on[1:]):
                a, b = xy[j], xy[j + 1]
                lo_x, hi_x = min(a[0], b[0]) - half, max(a[0], b[0]) + half
                lo_y, hi_y = min(a[1], b[1]) - half, max(a[1], b[1]) + half
                m = (e >= lo_x) & (e <= hi_x) & (n >= lo_y) & (n <= hi_y)
                if not m.any():
                    continue
                idx = np.flatnonzero(m)
                ab = b - a
                L2 = max(float(ab @ ab), 1e-12)
                t0 = ((e[idx] - a[0]) * ab[0] + (n[idx] - a[1]) * ab[1]) / L2
                t = np.clip(t0, 0.0, 1.0)
                d = np.hypot(e[idx] - a[0] - t * ab[0], n[idx] - a[1] - t * ab[1])
                k = d <= half
                # square at the line's own ends, not round: an end is a bridge's abutment, and a round end reached
                # half a road's width out under the deck, over the road dipped beneath it (I-20, Ruston, LA)
                if j == 0:
                    k &= t0 >= 0.0
                if j == len(xy) - 2:
                    k &= t0 <= 1.0
                if k.any():
                    zz = z[j] + (z[j + 1] - z[j]) * t[k] + lift
                    out[idx[k]] = np.maximum(out[idx[k]], zz)

    def _end_lift(self, xy):
        """(height m the ground there must reach, other road lines [(xy (k, 2), half width)]) when the road end xy is a
        bridge abutment, else None. A pure function of the map around the bridge (its cells, fetched as needed), cached per point."""
        k = (round(float(xy[0]) * 2), round(float(xy[1]) * 2))
        with self.lock:
            if not hasattr(self, "end_cache"):
                self.end_cache = {}
            if k in self.end_cache:
                return self.end_cache[k]
        res = None
        miss0 = self._misses()
        try:
            res = self._end_lift_calc(np.asarray(xy, np.float64))
        except Exception as e:      # no ramp rather than no grading
            log.debug("grading: approach at %s: %s: %s", k, type(e).__name__, e)
            self._tl.misses = self._misses() + 1        # (not kept: asked again)
        if self._misses() > miss0:
            return res              # (made without a cell's roads: not cached, asked again)
        with self.lock:
            return self.end_cache.setdefault(k, res)

    def _cell_enu(self, k, ground=True):
        """Cell k's road parts in the anchor frame, made once: {"bridges": [line (k, 2)], "ends": (n, 2) bridge end
        points, "end_of": [bridge index per end], "ground": [(line, half width, cls)]} (the longest copy of each part).
        With ground False, only the bridges (most plan ends touch none: their cells' ground roads are not needed)."""
        with self.lock:
            if not hasattr(self, "enu_cache"):
                self.enu_cache = {}
            got = self.enu_cache.get((str(k), ground)) or (self.enu_cache.get((str(k), True)) if not ground else None)
        if got is not None:
            return got
        parts = {}
        try:
            f = self.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
        except Exception as e:
            # (KyzoFox 1.5.5) a neighbouring cell that cannot be fetched now: none of its roads, not cached, and the
            # caller's results not kept either (_misses); raised, it threw away every ramp of the cell planned
            log.debug("grading: roads of %s for a neighbour: %s: %s", k, type(e).__name__, e)
            self._tl.misses = self._misses() + 1
            return {"bridges": [], "ground": [], "end_of": [], "bcls": [], "ends": np.zeros((0, 2), np.float64)}
        if f is not None and len(f):
            bs = roadsmod.bridge_specs(f)
            rs = roadsmod.road_specs(f, None, self.widths) if ground else [None] * len(f)
            for i in range(len(f)):
                if bs[i] is None and rs[i] is None:
                    continue
                fid = str(f.ids[i])
                for kind, sp, ps in (("bridge", bs[i], roadsmod.bridge_parts(f, i) if bs[i] is not None else ()),
                                     ("ground", rs[i], roadsmod.ground_parts(f, i) if rs[i] is not None else ())):
                    for j, ring in enumerate(ps):
                        ring = np.asarray(ring, np.float64)
                        kk = (fid, kind, j)
                        if len(ring) >= 2 and (kk not in parts or len(ring) > len(parts[kk][0])):
                            parts[kk] = (ring, sp)
        bridges, ground_l, ends, end_of, bcls = [], [], [], [], []
        for (fid, kind, j), (ring, sp) in sorted(parts.items()):
            e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
            line = np.stack([np.asarray(e), np.asarray(n)], axis=1)
            if kind == "bridge":
                end_of += [len(bridges), len(bridges)]
                ends += [line[0], line[-1]]
                bridges.append(line)
                bcls.append((sp.cls, sp.width))
            else:
                ground_l.append((line, sp.width / 2 + shoulder(sp), sp.cls))
        out = {"bridges": bridges, "ground": ground_l, "end_of": end_of, "bcls": bcls,
               "ends": np.asarray(ends, np.float64).reshape(-1, 2)}
        with self.lock:
            if len(self.enu_cache) > 128:
                self.enu_cache.clear()
            self.enu_cache[(str(k), ground)] = out
        return out

    def _end_lift_calc(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), 0.0)
        home = mapcells.cell_of(float(la[0]), float(lo[0]))
        hc = self._cell_enu(home, ground=False)
        if not len(hc["ends"]):
            return None
        d = np.hypot(hc["ends"][:, 0] - xy[0], hc["ends"][:, 1] - xy[1])
        hit = np.flatnonzero(d <= APPROACH_TOUCH_M)
        if not len(hit):
            return None
        bridge = hc["bridges"][hc["end_of"][int(hit[0])]]
        # the bridge's own cells (a few, it is short): the roads crossing under it and the parts it joins
        lam, lom, _ = self.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        pad = 50.0 / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lam)))))
        keys = sorted(set(mapcells.cells_for_bbox(float(lam.min()) - pad, float(lom.min()) - padlon,
                                                  float(lam.max()) + pad, float(lom.max()) + padlon)) | {home}, key=str)
        cs = [self._cell_enu(k) for k in keys]
        bridges = [b for c in cs for b in c["bridges"]]
        lo_b, hi_b = bridge.min(0) - APPROACH_MAX_RUN - 50.0, bridge.max(0) + APPROACH_MAX_RUN + 50.0
        # (KyzoFox 1.5.5) every abutment around: a road leading onto any bridge is raised with it, so it is no "road
        # beside" whose fill slope caps this ramp (a divided road's other carriageway onto its twin bridge kept Thomas
        # Road's ramp in West Monroe, LA to 1.8 m of its 5.7 m: 3 m under the girders over I-20, a truck hit it)
        all_ends = np.concatenate([c["ends"] for c in cs if len(c["ends"])]) if any(len(c["ends"]) for c in cs) \
            else np.zeros((0, 2))
        all_ends = all_ends[np.hypot(*(all_ends - xy).T) <= APPROACH_TWIN_M]     # (a twin's, beside this one)
        ground, others, seen = [], [], set()
        blo, bhi = bridge.min(0), bridge.max(0)
        for c in cs:
            for line, half, _cls in c["ground"]:
                key = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), len(line))
                if key in seen:
                    continue
                seen.add(key)
                if (line.max(0) < lo_b).any() or (line.min(0) > hi_b).any():
                    continue                # nowhere near the bridge or its ramps
                ground.append(line)
                touches = min(np.hypot(*(line[0] - xy)), np.hypot(*(line[-1] - xy))) <= APPROACH_TOUCH_M
                leads = len(all_ends) and min(np.hypot(*(all_ends - line[0]).T).min(),
                                              np.hypot(*(all_ends - line[-1]).T).min()) <= APPROACH_TOUCH_M
                under = not ((line.max(0) < blo).any() or (line.min(0) > bhi).any()) and \
                    bool(roadsmod._crossings([bridge], [line]))
                # the roads beside the ramp, which its fill slope must not reach (the ones the bridge spans only
                # beyond APPROACH_UNDER_FREE_M of the abutment: by the abutment, it stands between)
                if not touches and not leads:
                    others.append((line, half, APPROACH_UNDER_FREE_M) if under else (line, half))

        def z_at(p):
            la_, lo_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(la_, lo_), np.float64)
        near = [o for o in others if not ((o[0].max(0) < xy - APPROACH_MAX_RUN - 50.0).any()
                                          or (o[0].min(0) > xy + APPROACH_MAX_RUN + 50.0).any())]
        if near:                    # their heights (one sample of all their vertices)
            zs_ = z_at(np.concatenate([o[0][:, :2] for o in near]))
            k0, out_ = 0, []
            for o in near:
                n_ = len(o[0])
                out_.append((o[0], o[1], o[2] if len(o) > 2 else 0.0, zs_[k0:k0 + n_]))
                k0 += n_
            others = out_
        else:
            others = []
        for end, need in bridge_needs(bridge, bridges, ground, z_at):
            if np.hypot(*(end - xy)) <= APPROACH_TOUCH_M:
                return need, others
        return None

    def _longest(self, fid, j, ring, key):
        """Part j of tunnel fid as the cells at its ends have it, the longest (a part comes clipped differently in
        the cells around: graded as the 30 m piece, 97th Street's 45 m arch got an open road graded in it; the
        same rule as roads.BridgeProfiles._load, 2026-10-17)."""
        best = ring
        for p in (ring[0], ring[-1]):
            ck = mapcells.cell_of(float(p[1]), float(p[0]))
            if key is not None and str(ck) == str(key):
                continue
            try:
                f = self.md.cell(ck, layers=("roads",), kind="demand").layers.get("roads")
            except Exception:
                continue
            if f is None or not len(f):
                continue
            ids = [str(x) for x in f.ids]
            if fid not in ids:
                continue
            i = ids.index(fid)
            parts = roadsmod.tunnel_parts(f, i)
            if j < len(parts) and len(parts[j]) > len(best):
                best = parts[j]
        return best

    def _tunnel_sections(self, feats, key=None, road_plans=()):
        """[(fid, spec, xy, z, kind)] of the tunnels' sections (roads.tunnel_sections on the ungraded ground, its
        ends graded by road_plans: the cell's road Plans, as roads.TunnelProfiles.ends grades them with the roads'
        plans alone): kind 0 an open road at z, 1 a low arch, 2 a full tunnel."""
        out = []
        ts = roadsmod.tunnel_specs(feats)
        for i in range(len(feats)):
            if ts[i] is None:
                continue
            for j, ring in enumerate(roadsmod.tunnel_parts(feats, i)):
                ring = self._longest(str(feats.ids[i]), j, ring, key)
                ll = np.asarray(ring, np.float64)
                if len(ll) < 2:
                    continue
                E, N, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                xy, s = resample(np.stack([np.asarray(E), np.asarray(N)], axis=1), VERTEX_STEP)
                if len(xy) < 2 or s[-1] <= 0:
                    continue
                la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
                g = np.asarray(self.elev(la, lo), np.float64)
                if road_plans:
                    g = g.copy()
                    g[[0, -1]] = grade_points(xy[[0, -1], 0], xy[[0, -1], 1], g[[0, -1]], list(road_plans))
                floor, kinds = roadsmod.tunnel_sections(s, g)
                i0 = 0
                while i0 < len(kinds):
                    i1 = i0
                    while i1 + 1 < len(kinds) and kinds[i1 + 1] == kinds[i0]:
                        i1 += 1
                    a = max(0, i0 - 1)          # sharing the end sample with the section before
                    if i1 > a:
                        out.append((str(feats.ids[i]), ts[i], xy[a:i1 + 1], floor[a:i1 + 1], int(kinds[i0])))
                    i0 = i1 + 1
        return out

    @staticmethod
    def _mouth_plans(secs):
        """(fid, Plan) of the covered sections' mouths: the floor MOUTH_PLAN_M into the tube from a portal (where an
        open section or nothing comes before / after it): the ground at the portal graded to the road on both sides
        of it (the grading faded from the road's plan into the ungraded hill there, and the road tile and the
        terrain block, sampled at different zooms, came out a few cm apart: the block poked through the road
        outside 97th Street's arch, 2026-10-17)."""
        out = []
        for n, (fid, spec, xy, z, kind) in enumerate(secs):
            if not kind or len(xy) < 2:
                continue
            same = lambda m: 0 <= m < len(secs) and secs[m][0] == fid     # noqa: E731
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            hw = spec.width / 2 + shoulder(spec)
            for at_start in (True, False):
                nb = n - 1 if at_start else n + 1
                if same(nb) and secs[nb][4] != 0:
                    continue                    # inside the hill: a full tunnel goes on
                if at_start:
                    m = s <= MOUTH_PLAN_M
                    t = np.interp(MOUTH_PLAN_M, s, np.arange(len(s)))
                else:
                    m = s >= s[-1] - MOUTH_PLAN_M
                    t = np.interp(s[-1] - MOUTH_PLAN_M, s, np.arange(len(s)))
                i0 = int(np.floor(t))
                p = xy[i0] + (t - i0) * (xy[min(i0 + 1, len(xy) - 1)] - xy[i0])
                pz = z[i0] + (t - i0) * (z[min(i0 + 1, len(z) - 1)] - z[i0])
                if at_start:
                    pxy, pzz = np.r_[xy[m], p[None]], np.r_[z[m], pz]
                else:
                    pxy, pzz = np.r_[p[None], xy[m]], np.r_[pz, z[m]]
                if len(pxy) >= 2:
                    out.append((fid + TUNNEL_TAG + ("m0" if at_start else "m1"), Plan(pxy, pzz, hw)))
        return out

    def _covered_tunnels(self, feats):
        """[(xy, half width)] of the tunnels' covered sections (not open roads): the grading leaves the ground over
        them as it is (apply): the approaches' grading reached up to T_MAX past a portal and cut a park bridge's soil
        down under its arch's vault, the terrain then cut away over it (2026-10-17)."""
        return [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, kind in self._tunnel_sections(feats) if kind]

    def _open_tunnels(self, feats):
        """(fid, Plan) of the tunnels' open sections (roads.TUNNEL_OPEN_COVER): the ground graded to the road's floor
        (the lower hull of the ground), as the road is drawn (roads._tunnel_chunks): over a footbridge's hump in the
        DEM the road climbed it, the ground over the road (Central Park's transverses, 2026-10-17)."""
        return [(fid, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                for fid, spec, xy, z, kind in self._tunnel_sections(feats) if kind == 0]

    def planned(self, key):
        """True once cell key's plans exist (made, or failed recently)."""
        with self.lock:
            f = self.failed.get(key)
            return key in self.cells or (f is not None and time.monotonic() - f < RETRY_S)

    def warm(self, key):
        """Make cell key's plans now (worker threads: the lookahead ahead of the player)."""
        self._cell_plans(key)

    def plans(self, lat, lon, roads_only=False):
        """The Plans touching the points' area (each road once); roads_only: not the tunnels' (TUNNEL_TAG)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        pad = (T_MAX + 40.0) / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lat)))))
        keys = mapcells.cells_for_bbox(float(lat.min()) - pad, float(lon.min()) - padlon,
                                       float(lat.max()) + pad, float(lon.max()) + padlon)
        if len(keys) > SPREAD_CELLS:        # points far apart: only the cells around them
            keys = mapcells.cells_near_points(lat, lon, pad, padlon)
        got = {}
        for key in keys:
            for fid_part, p in self._cell_plans(key):
                if roads_only and TUNNEL_TAG in fid_part:
                    continue
                got.setdefault((fid_part, float(p.xy[0, 0]), float(p.xy[0, 1])), p)
        # in the order of their keys: grade_points' sums run in plan order, so every caller (the block, a road
        # tile, a far tile) gets the same heights at a point whatever other cells its points reached
        return [got[k] for k in sorted(got)]

    def apply(self, lat, lon, base, lift=0.0, roads_only=False):
        """base (the ungraded heights at the points) graded under the roads. Same shape as base.
        lift: m added under the roads (tapering with the grading's weight): the physics block and the
        road mesh use ROAD_LIFT, the visible ground tiles 0, so the road mesh lies exactly on the
        ground the tyres touch while the visible ground stays a little below it (no z-fighting;
        2026-10-11 the user saw the tyres sink into a road mesh lifted above the physics ground)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        base = np.asarray(base, np.float64)
        shape = base.shape
        if base.size == 0:
            return base
        plans = self.plans(lat, lon, roads_only)
        if not plans:
            return base
        e, n, _ = self.frame.geodetic_to_enu(lat.ravel(), lon.ravel(), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        out = grade_points(e, n, base.ravel(), plans, lift)
        if APPROACH_ON:
            self._floor_ramps(e, n, out, lift)
        keep = self._over_tunnels(e, n)
        if keep is not None:
            # (a small change stays: a road over the tunnel lies on its own graded profile, the ground over the tunnel
            # left ungraded under it stood a few cm over the E6's lanes as patches of bare ground, GitHub #2)
            keep &= np.abs(out - base.ravel()) >= OVER_TUNNEL_SMALL_M
            out[keep] = base.ravel()[keep]
        return out.reshape(shape)

    def open_tunnels(self, lat, lon):
        """[(lat (k,), lon (k,), Spec)] of the tunnels' open sections planned near the points: roads on the ground
        that the map calls tunnels (surface.paint_roads paints them: grass grew over the open road of Central Park's
        79th Street Transverse, 2026-10-17)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        if lat.size == 0:
            return []
        for key in mapcells.cells_for_bbox(float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())):
            self._cell_plans(key)
        with self.lock:
            secs = [o for v in self.opens.values() for o in v]
        out, seen = [], set()
        for xy, spec in secs:
            k = (round(float(xy[0, 0]), 2), round(float(xy[0, 1]), 2), len(xy))
            if k in seen:
                continue
            seen.add(k)
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            la, lo = np.asarray(la), np.asarray(lo)
            if la.max() < lat.min() - 0.001 or la.min() > lat.max() + 0.001 or \
                    lo.max() < lon.min() - 0.0015 or lo.min() > lon.max() + 0.0015:
                continue
            out.append((la, lo, spec))
        return out

    def _over_tunnels(self, e, n):
        """Which points lie over a covered tunnel (its half width + TUNNEL_KEEP_M), or None."""
        with self.lock:
            zones = [z for v in self.covers.values() for z in v]
        if not zones or not len(e):
            return None
        x0, y0, x1, y1 = e.min(), n.min(), e.max(), n.max()
        import fast
        if fast.AVAILABLE and hasattr(fast._ef, "over_tunnel"):     # (Rust: the same sums, without the GIL)
            mask = np.zeros(len(e), bool)
            ee, nn = np.ascontiguousarray(e, np.float64), np.ascontiguousarray(n, np.float64)
            for xy, hw in zones:
                fast._ef.over_tunnel(mask, ee, nn, np.ascontiguousarray(xy[:, :2], np.float64), float(hw),
                                     float(TUNNEL_KEEP_IN_M))
            return mask if mask.any() else None
        mask = None
        for xy, hw in zones:
            if xy[:, 0].max() + hw < x0 or xy[:, 0].min() - hw > x1 or xy[:, 1].max() + hw < y0 or xy[:, 1].min() - hw > y1:
                continue
            a, b = xy[:-1], xy[1:]
            ab = b - a
            L2 = np.maximum((ab * ab).sum(1), 1e-12)
            sj = np.r_[0.0, np.cumsum(np.sqrt(L2))]
            # only the segments near the points (the Gotthard road tunnel's 4,200 segments each took a pass over a
            # block's points: 140 s of grading per block, 2026-10-18)
            near = ((np.minimum(a[:, 0], b[:, 0]) - hw <= x1) & (np.maximum(a[:, 0], b[:, 0]) + hw >= x0)
                    & (np.minimum(a[:, 1], b[:, 1]) - hw <= y1) & (np.maximum(a[:, 1], b[:, 1]) + hw >= y0))
            for j in np.flatnonzero(near):
                t = ((e - a[j, 0]) * ab[j, 0] + (n - a[j, 1]) * ab[j, 1]) / L2[j]
                # over it, from TUNNEL_KEEP_IN_M inside its portals: its mouths graded to the road (a road's last row
                # at a portal took the hill's height, the road mesh a wedge up into the opening, 2026-10-17)
                s_ = sj[j] + t * np.sqrt(L2[j])
                inside = (t > 0.0) & (t < 1.0) & (s_ > TUNNEL_KEEP_IN_M) & (s_ < sj[-1] - TUNNEL_KEEP_IN_M)
                d = np.abs((e - a[j, 0]) * ab[j, 1] - (n - a[j, 1]) * ab[j, 0]) / np.sqrt(L2[j])
                m = inside & (d <= hw)
                mask = m if mask is None else (mask | m)
        return mask


def grade_points(px, py, base, plans, lift=0.0):
    """base at the points (px, py) graded under the plans (see the module doc). Pure.
    All segments at once, in batches of about PAIR_BATCH (segment, point) pairs (a Python loop per
    segment was the server's largest CPU cost while driving, 2026-10-12)."""
    px = np.asarray(px, np.float64)
    py = np.asarray(py, np.float64)
    base = np.asarray(base, np.float64)
    out = base.copy()
    if not len(px):
        return out
    x0, y0, x1, y1 = px.min(), py.min(), px.max(), py.max()
    bx = np.floor((px - x0) / BUCKET).astype(np.int64)
    by = np.floor((py - y0) / BUCKET).astype(np.int64)
    nbx = int(bx.max()) + 1
    key = by * nbx + bx
    order = np.argsort(key, kind="stable")
    sk = key[order]
    # every segment of the plans near the points: ends, end heights, reach (core half width + T_MAX)
    A, B, ZA, ZB, H = [], [], [], [], []
    for p in plans:
        if p.bbox[2] < x0 or p.bbox[0] > x1 or p.bbox[3] < y0 or p.bbox[1] > y1 or len(p.xy) < 2:
            continue
        A.append(p.xy[:-1])
        B.append(p.xy[1:])
        ZA.append(p.z[:-1])
        ZB.append(p.z[1:])
        H.append(np.full(len(p.xy) - 1, p.half))
    if not A:
        return out
    A, B, ZA, ZB, H = (np.concatenate(v) for v in (A, B, ZA, ZB, H))
    import fast
    if fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= 27:   # the Rust loop: the same pairs in the same order
        f64 = np.float64
        return fast._ef.grade_points(np.ascontiguousarray(px, f64), np.ascontiguousarray(py, f64),
                                     np.ascontiguousarray(base, f64), np.ascontiguousarray(A[:, :2], f64),
                                     np.ascontiguousarray(B[:, :2], f64), np.ascontiguousarray(ZA, f64),
                                     np.ascontiguousarray(ZB, f64), np.ascontiguousarray(H, f64), float(lift),
                                     float(SLOPE_RUN), float(T_MIN), float(T_MAX), float(BLEND_R), float(BUCKET),
                                     float(WALL_CUT_M), float(WALL_T_M), float(WALL_FLAT_M), float(WALL_AHEAD_M))
    reach = H + T_MAX
    lo_x = np.minimum(A[:, 0], B[:, 0]) - reach
    hi_x = np.maximum(A[:, 0], B[:, 0]) + reach
    lo_y = np.minimum(A[:, 1], B[:, 1]) - reach
    hi_y = np.maximum(A[:, 1], B[:, 1]) + reach
    near = ~((hi_x < x0) | (lo_x > x1) | (hi_y < y0) | (lo_y > y1))
    A, B, ZA, ZB, H = A[near], B[near], ZA[near], ZB[near], H[near]
    lo_x, hi_x, lo_y, hi_y = lo_x[near], hi_x[near], lo_y[near], hi_y[near]
    cx0 = np.maximum(0, np.floor((lo_x - x0) / BUCKET)).astype(np.int64)
    cx1 = np.minimum(nbx - 1, np.floor((hi_x - x0) / BUCKET)).astype(np.int64)
    cy0 = np.maximum(0, np.floor((lo_y - y0) / BUCKET)).astype(np.int64)
    cy1 = np.floor((hi_y - y0) / BUCKET).astype(np.int64)
    # (segment, bucket row) pairs -> the sorted points' range of that row's buckets cx0..cx1
    nrow = np.maximum(cy1 - cy0 + 1, 0)
    seg = np.repeat(np.arange(len(A)), nrow)
    row = np.repeat(cy0, nrow) + (np.arange(nrow.sum()) - np.repeat(np.cumsum(nrow) - nrow, nrow))
    i0 = np.searchsorted(sk, row * nbx + cx0[seg], "left")
    i1 = np.searchsorted(sk, row * nbx + cx1[seg], "right")
    cnt = np.maximum(i1 - i0, 0)
    keep = cnt > 0
    seg, i0, cnt = seg[keep], i0[keep], cnt[keep]
    num = np.zeros(len(px))
    den = np.zeros(len(px))
    wmax = np.zeros(len(px))
    ends = np.cumsum(cnt)
    start = 0
    while start < len(seg):
        stop = int(np.searchsorted(ends, (ends[start - 1] if start else 0) + PAIR_BATCH, "right"))
        stop = max(stop, start + 1)
        c = cnt[start:stop]
        s_ = np.repeat(seg[start:stop], c)
        idx = order[np.repeat(i0[start:stop], c) + (np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c))]
        a = A[s_]
        ab = B[s_] - a
        qx, qy = px[idx] - a[:, 0], py[idx] - a[:, 1]
        L2 = ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1]
        long_ = L2 > 1e-12
        t = np.where(long_, np.clip((qx * ab[:, 0] + qy * ab[:, 1]) / np.where(long_, L2, 1.0), 0.0, 1.0), 0.0)
        d = np.hypot(qx - t * ab[:, 0], qy - t * ab[:, 1])
        target = ZA[s_] + (ZB[s_] - ZA[s_]) * t
        Lab = np.sqrt(np.maximum(L2, 1e-24))
        side = np.abs(qx * ab[:, 1] - qy * ab[:, 0]) / Lab
        along = (qx * ab[:, 0] + qy * ab[:, 1]) / Lab
        ahead = np.maximum(np.maximum(-along, along - Lab), 0.0)
        wall = (base[idx] - target > WALL_CUT_M) & (ahead < side) & (ahead < WALL_AHEAD_M)
        T = np.where(wall, WALL_T_M, np.clip(SLOPE_RUN * np.abs(target - base[idx]), T_MIN, T_MAX))
        u = np.clip((d - H[s_] - np.where(wall, WALL_FLAT_M, 0.0)) / T, 0.0, 1.0)
        w = 1.0 - u * u * (3.0 - 2.0 * u)           # 1 on the core, smooth to 0 over T
        m = w > 0
        i = idx[m]
        # the blend between roads by inverse distance from each centreline (times w), so two roads
        # near each other (divided carriageways, junctions) meet in a gradual slope; with w alone
        # the target jumped at a core's edge (2026-10-11, the user's car flipped at speed:
        # centreline 2nd difference p99 0.17 m per metre, worst 0.5 m)
        wb = w[m] / (1.0 + (d[m] / BLEND_R) ** 2)
        num += np.bincount(i, wb * target[m], len(px))
        den += np.bincount(i, wb, len(px))
        np.maximum.at(wmax, i, w[m])
        start = stop
    g = den > 0
    out[g] = base[g] + wmax[g] * (num[g] / den[g] - base[g] + lift)
    return out


def bridge_lifts(line, all_bridges, ground_lines, z_at):
    """[(end xy (2,), lift m)]: bridge_needs as lifts over the ground at each end, within [APPROACH_MIN_LIFT,
    APPROACH_MAX_LIFT]. Pure given z_at."""
    out = []
    for end, need in bridge_needs(line, all_bridges, ground_lines, z_at):
        lift = float(need - z_at(end[None])[0])
        if lift >= APPROACH_MIN_LIFT:
            out.append((end, min(lift, APPROACH_MAX_LIFT)))
    return out


def bridge_needs(line, all_bridges, ground_lines, z_at):
    """[(end xy (2,), height m)] of a bridge part (line (k, 2) ENU m): at each of its ends that no other bridge part
    continues from (an abutment), how high the ground there must be for the deck to clear every road crossing
    under it (roads.DECK_ROAD_CLEAR + APPROACH_MARGIN_M over the road, full height within APPROACH_FLAT_M of the end,
    then falling at roads.DECK_GRADE), within [APPROACH_MIN_LIFT, APPROACH_MAX_LIFT]. Roads crossing within
    APPROACH_MIN_CROSS_M of either end meet it at grade and do not count. z_at(xy (n, 2)) -> ground m. Pure given
    z_at."""
    line = np.asarray(line, np.float64)[:, :2]
    if len(line) < 2:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    if s[-1] < 2 * APPROACH_MIN_CROSS_M:
        return []
    cr = roadsmod._crossings([line], ground_lines)
    if not cr:
        return []
    zu = z_at(np.array([c[2] for c in cr], np.float64))
    arcs = np.array([c[1] for c in cr], np.float64)
    under = (arcs >= APPROACH_MIN_CROSS_M) & (arcs <= s[-1] - APPROACH_MIN_CROSS_M)    # (not a junction at an end)
    if not under.any():
        return []
    zu, arcs = zu[under], arcs[under]
    out = []
    for end, at in ((line[0], 0.0), (line[-1], s[-1])):
        joined = False
        for other in all_bridges:
            if other is line or (len(other) == len(line) and np.array_equal(other, line)):
                continue
            o = np.asarray(other, np.float64)
            if min(np.hypot(*(o[0, :2] - end)), np.hypot(*(o[-1, :2] - end))) < 0.75:
                joined = True
                break
        if joined:
            continue
        d = np.abs(arcs - at)
        need = zu + roadsmod.DECK_ROAD_CLEAR + APPROACH_MARGIN_M \
            - roadsmod.DECK_GRADE * np.maximum(d - APPROACH_FLAT_M, 0.0)
        out.append((np.asarray(end, np.float64), float(need.max())))
    return out


def approach_add(xy, at_end, lift, half, others=(), z=None):
    """m to add to a road plan's vertices (xy (n, 2)) rising to `lift` at its first (at_end False) or last vertex (a
    bridge's abutment), falling off along the road (arc length) on a cosine ramp whose steepest grade is APPROACH_GRADE;
    at most what keeps the fill slope (SLOPE_RUN) clear of the other roads [(line (k, 2), half width)] beside it, with
    APPROACH_GAP_M to spare. None when nothing is raised. Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2 or lift < APPROACH_MIN_LIFT:
        return None
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    d = (s[-1] - s) if at_end else s
    run = float(np.clip(lift * math.pi / (2 * APPROACH_GRADE), APPROACH_MIN_RUN, APPROACH_MAX_RUN))
    u = np.clip(d / run, 0.0, 1.0)
    add = lift * 0.5 * (1.0 + np.cos(math.pi * u))
    on = add > 1e-3
    if not on.any():
        return None
    q0 = xy[on]
    lo_q, hi_q = q0.min(0), q0.max(0)
    for other in others:
        line, oh = other[0], other[1]
        free = other[2] if len(other) > 2 else 0.0      # m from the abutment this road does not cap
        zl = other[3] if len(other) > 3 else None       # its heights at its vertices (with z: the ramp's own)
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = half + oh + APPROACH_GAP_M + APPROACH_CAP_RUN * lift
        if (line.max(0) < lo_q - reach).any() or (line.min(0) > hi_q + reach).any():
            continue                    # too far to matter
        q = xy[on]
        a, b = line[:-1], line[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        dd = np.hypot(*(q[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
        jn = dd.argmin(1)
        dist = dd[np.arange(len(q)), jn]
        cap = np.maximum(0.0, (dist - half - oh - APPROACH_GAP_M) / APPROACH_CAP_RUN)
        if z is not None and zl is not None:
            # (KyzoFox 1.5.5) over that road's own height, not the ramp's: frontage roads meeting Nutland Road at grade
            # by its abutment in Monroe, LA (as high as it) held its ramp down to nothing
            zl = np.asarray(zl, np.float64)
            zo = zl[jn] + (zl[jn + 1] - zl[jn]) * t[np.arange(len(q)), jn]
            cap = np.maximum(0.0, cap + zo - np.asarray(z, np.float64)[on])
        cap = np.where(d[on] < free, np.inf, cap)
        add[on] = np.minimum(add[on], cap)
    # a cap lowers the ramp before it at APPROACH_CAP_GRADE at most, instead of a step at the cap
    for i in range(1, len(add)):
        add[i] = min(add[i], add[i - 1] + APPROACH_CAP_GRADE * (s[i] - s[i - 1]))
    for i in range(len(add) - 2, -1, -1):
        add[i] = min(add[i], add[i + 1] + APPROACH_CAP_GRADE * (s[i + 1] - s[i]))
    if add.max() < 1e-3:
        return None
    return add


def _is_link(spec):
    return bool(getattr(spec, "link", False))


class GradedDem:
    """dem.Dem with sample() graded under the roads; everything else passes through."""

    def __init__(self, dem, grader):
        self._dem, self._grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        base = self._dem.sample(lat, lon, zoom, progress) if progress is not None else self._dem.sample(lat, lon, zoom)
        return self._grader.apply(lat, lon, base)

    def __getattr__(self, name):
        return getattr(self._dem, name)
