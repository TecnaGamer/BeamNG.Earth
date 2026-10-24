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
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.grading")

VERSION = 2
PROFILE_STEP = 2.0          # m between profile samples
SIGMA_M = 12.0              # m, profile smoothing of built roads
DIRT_SIGMA_M = 8.0          # m, profile smoothing of gravel / dirt roads (fork: 5)
# the Finnish fork: longer smoothing for the larger classes (profile_sigma; others SIGMA_M)
CLASS_SIGMA_M = {"motorway": 45.0, "trunk": 40.0, "primary": 30.0, "secondary": 25.0, "tertiary": 20.0,
                 "unclassified": 16.0, "residential": 14.0}
MEDIAN_M = 20.0             # m, running median before the smoothing (median_profile): spikes out, not spread
DIRT_MEDIAN_M = 10.0
# the Finnish fork: side roads meet the road they join at its height (snap_junctions)
CUT_VERGE_M = 3.0           # m of level verge between a Finnish road's edge and the rock of its cutting (in_finland)
JUNCTION_SNAP_ON = os.environ.get("BNE_JUNCTION_SNAP", "1") != "0"
JUNCTION_RANK = {"motorway": 9, "trunk": 8, "primary": 7, "secondary": 6, "tertiary": 5, "unclassified": 4,
                 "residential": 3, "living_street": 2, "service": 1}
JUNCTION_REACH_M = 3.0      # m beyond the joined road's carriageway edge a side road's end still counts as joining it
JUNCTION_END_M = 10.0       # m: a same-class road ending this close to the other's end continues it (no snap)
JUNCTION_TAPER_M = 40.0     # m along the side road the height change is eased in over (half the road when shorter)
JUNCTION_MAX_M = 3.0        # m: a bigger difference is left as it was (an overpass ramp, bad data)
SHOULDER_M = 1.0            # m of flat shoulder beyond the carriageway, each side
SIDEWALK_MARGIN_M = 0.5     # m of flat ground beyond a sidewalk (roads.SIDEWALK_CLASSES)
LONG_PART_M = 3000.0        # a road part longer than this is planned per cell, only within CLIP_MARGIN_M of the cell
CLIP_MARGIN_M = 400.0
PLAN_THREADS = 4            # cells planned at once (_plan_missing)
SPREAD_CELLS = 16           # plans(): more map cells in the points' bbox: only the cells around the points
T_MIN, T_MAX = 5.0, 12.0    # m, transition back to the natural ground (5: roads close together meet gently)
SLOPE_RUN = 2.0             # transition = SLOPE_RUN x the height difference (1:2 slope)
VERTEX_STEP = 4.0           # m between the plan's vertices (segments for the grading pass; linear between: at 16 m a
                            # crest or a sag was a kink every 16 m, 'bumpy roads, worst uphill and at speed' on r/BeamNG,
                            # 2026-10-17: a San Francisco street's slope changed 4.4 % rms per 2 m, 1.2 % at 4 m;
                            # grading a 1 km block 0.06 -> 0.14 s)
BUCKET = 32.0               # m, query-point buckets
ROAD_LIFT = 0.04            # m the physics ground is raised under roads (see Grader.apply)
BLEND_R = 4.0               # m: roads near each other blend their targets by 1 / (1 + (d / BLEND_R)^2)
PROFILE_ZOOM = int(os.environ.get("BNE_DEM_ZOOM", "16"))   # DEM zoom of the profiles (one zoom for every caller)
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
# Roads at other heights beside a road's core (GitHub #92: a street in a cutting had its half under a service road on
# the bank 7.5 m above it pulled 2-4 m up its slope; hairpins blended into each other; an upper road's edge dragged into
# a pit by the road under it). Beside a point in a core, another road's segments outside their own core count less the
# further their height is from that core's: not at all from SEP_FULL_M, as before up to SEP_FROM_M, and their weight
# comes back over CORE_FADE_M past the core (no step at its edge).
SEP_ON = os.environ.get("BNE_GRADE_SEP", "on") != "off"
# Sharp cuttings (the user, 2026-10-21: the elevation data is smoothed, "sharper terrain features get lost": a street in
# a walled cutting read as a 4 m slope its asphalt climbed, McGraw-Graben, Munich). Beside a built road whose ground
# rises SHARP_MIN_M or more within SHARP_REACH_M of its core at SHARP_GRADE or steeper on average (a wall the data
# smoothed into a ramp), the ground is raised to that top from the core's edge out: the road's wall rule (WALL_CUT_M)
# then flattens a strip, steps up and roads.retaining_walls stands a wall there.
SHARP_ON = os.environ.get("BNE_SHARP", "on") != "off"
SHARP_REACH_M = 10.0
SHARP_STEP_M = 1.0
SHARP_MIN_M = 2.0
SHARP_GRADE = 0.6
SHARP_RUN_N = 3             # consecutive plan vertices (VERTEX_STEP apart) a cutting holds for at least
SEP_FROM_M = 2.0
SEP_FULL_M = 4.0
CORE_FADE_M = 2.0
SIDE_FADE_M = 2.0           # m of (side - ahead) over which a segment counts as beside the point (ahead: its own road)
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
APPROACH_CAP_GRADE = 0.10   # steepest grade a ramp falls at where a road beside it caps it (a cap alone dropped 3 m
                            # in 8 m by a side road north of the I-20 bridge near Temple, GA, 2026-10-20)

# Embankments at abutments (GitHub #38: a GPS drive near Presov, Slovakia flipped on the D1). A bridge's map line can end
# down its embankment, short of the abutment: the road's last metres followed the valley's side in the elevation data and
# the deck end, taken from that ground, sat 1.4 m under the road 2 m on (a 12 % deck and a step at 55 mph). A road ending
# at a bridge whose last ABUT_NEAR_M fall much more steeply toward it than its own grade further on keeps that grade to
# its end instead, as the fill under a real approach does (every class, motorways too).
ABUT_FILL_ON = os.environ.get("BNE_ABUT_FILL", "on") != "off"
# Underpasses (GitHub #138, #142, #126's rule: "the road under gives way"): a ground road crossing under a short
# overpass that is never raised (APPROACH_SKIP classes and links: a motorway's bridge over a local road) dips to the
# deck's height (its abutments' ground, interpolated) minus UNDER_CLEAR_M, falling back at UNDER_GRADE. Lidar terrain
# removes bridges and interpolates across the gap, at about the deck's height: the road under ran 0.7-2.5 m under the
# girders (A28 near Zwolle, I-84 over NE 181st Ave in Portland, I-395 in Springfield VA, 2026-10-22). Never raises.
UNDER_ON = os.environ.get("BNE_UNDERPASS", "on") != "off"
UNGRADED = {"raceway"}      # classes planned by nobody: the terrain is their surface (an oval's banking, GitHub #143) ...
UNGRADED_MARGIN_M = 2.0     # ... and nothing else grades their carriageway (+ this: kerbs, apron): a road crossing a
                            # track, or a tunnel's open approach under one, cut a flat trench through Daytona's banking
UNDER_CLEAR_M = 6.7         # deck surface to the road under: slab + girders (roadmesh DECK_DEPTH + GIRDER_D) + 4.5 m
UNDER_GRADE = 0.06          # the dip's ramps
UNDER_MAX_BRIDGE_M = 120.0  # only single short spans (both ends on the ground): a chain's inner ends are no abutments
UNDER_MAX_DROP_M = 8.0      # never lowered more than this (bad data: rather a low bridge than a pit)
ABUT_NEAR_M = 14.0          # m from the bridge end the road is checked ...
ABUT_FAR_M = 40.0           # ... against its grade from ABUT_NEAR_M to this far
ABUT_STEEP = 0.15           # a fall this much steeper than that grade, near the end: the valley's side, not the road
ABUT_MIN_M = 0.3            # m: a smaller fill at the end is left out
ABUT_MAX_M = 8.0            # m filled at most


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (reflected at the ends). Pure."""
    import fast
    fast.require(0, "smooth_profile")       # the Rust loop (~7000 roads a 9-tile set)
    return fast._ef.smooth_profile(np.ascontiguousarray(z, np.float64), float(step), float(sigma))


def resample(xy, step):
    """Points every `step` m along a polyline (xy (k, 2)), the last point kept, and their arc lengths."""
    import fast
    if not len(xy):
        return xy[:1].copy(), np.zeros(1)
    fast.require(0, "resample")
    return fast._ef.resample(np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), float(step))


class Plan:
    """One road's grading: vertices (n, 2) in the frame's ENU metres, their target heights, core half width; abut: does
    it lead onto a bridge at its (first, last) vertex (grade_points)."""
    __slots__ = ("xy", "z", "half", "bbox", "abut", "cut", "road_half", "rid", "max_wall")

    def __init__(self, xy, z, half, abut=(False, False), cut=None, road_half=None):
        self.xy, self.z, self.half, self.abut = xy, z, float(half), (bool(abut[0]), bool(abut[1]))
        # (n - 1, 6) per segment, NaN where none: a cutting's wall offset from the centre line, its top's height and
        # the top's offset, on the left, then the right (_add_cuts)
        self.cut = cut
        self.road_half = float(road_half) if road_half is not None else float(half)    # the carriageway's half width
        self.rid = -1           # the road (map feature) it is part of: _plan_cell (each cell plans its own stretch of it)
        self.max_wall = WALL_MAX_H_M    # its cuttings' walls at most this tall (wall_max_h)
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


def plan_roads(frame, parts, elev, abut=None, verge=0.0):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call); abut: per part (start, end)
    flags, the ends leading onto a bridge (fill_abutment). Pure given elev."""
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
    for j, ((lonlat, spec), xy) in enumerate(zip(parts, prof)):
        if xy is None:
            out.append(None)
            continue
        z = zall[k:k + len(xy)]
        k += len(xy)
        ab = (False, False)
        if abut is not None and ABUT_FILL_ON:
            ab = (bool(abut[j][0]), bool(abut[j][1]))
            for at_end in (False, True):
                if ab[int(at_end)]:
                    z = fill_abutment(z, at_end)
        out.append(_plan(xy, z, spec, ab))
    if SHARP_ON:
        _add_cuts(frame, [p if p is not None and sp.kind in roadsmod.BUILT else None
                          for p, (_ll, sp) in zip(out, parts)], elev, verge)
    return out


WALL_MAX_H_M = 4.0      # m a sharp cutting's wall stands over the road at most (the slope above it is graded) ...
WALL_MAX_H_CITY_M = 10.0  # ... a major road's in town (concrete): the full depth of its trench (Munich's McGraw-Graben:
                        # vertical concrete walls the whole way up, Mapillary 2026-10-21; with the 4 m cap the bank above
                        # stood as a bare cliff over the wall once the near ground was drawn sharp, 2026-10-22)


def wall_max_h(spec):
    """How tall a sharp cutting's wall beside road spec (roads.Spec) may stand: WALL_MAX_H_CITY_M for a major road in
    town (roads.CONCRETE_WALL_CLASSES, the concrete walls), else WALL_MAX_H_M (stone walls, mountain passes)."""
    return WALL_MAX_H_CITY_M if (spec.urban and spec.cls in roadsmod.CONCRETE_WALL_CLASSES) else WALL_MAX_H_M
HAIRPIN_SPAN_M = 20.0   # a road's direction this far back and on ...
HAIRPIN_TURN_DEG = 90.0  # ... turned this much: a tight bend (earthfast.tight_bends) ...
HAIRPIN_PAD_M = 15.0    # ... and this far either side of it: no sharp cutting, no retaining wall


def bend_zone(xy):
    """Which points of a polyline (k, 2) lie at or within HAIRPIN_PAD_M (along it) of a tight bend."""
    import fast
    fast.require(42, "tight_bends")
    xy = np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2])
    m = np.asarray(fast._ef.tight_bends(xy, float(HAIRPIN_SPAN_M), float(HAIRPIN_TURN_DEG)))
    if not m.any():
        return m
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    sb = s[m]
    return np.abs(s[:, None] - sb[None, :]).min(axis=1) <= HAIRPIN_PAD_M


def in_finland(lat, lon):
    """The Finnish fork: is (lat, lon) in mainland Finland (a box with Sweden's Tornio valley side and Estonia out;
    the eastern border's Russian side is in it: an anchor there is rare)?"""
    return 59.75 <= lat <= 70.1 and 20.5 <= lon <= 31.6 and not (lat > 65.8 and lon < 23.9)


def _verge(cut, road_half, verge):
    """cut (n - 1, 6) with each side's wall set back to road_half + verge at least, its top with it (_add_cuts). Pure."""
    cut = cut.copy()
    for c in (0, 3):
        shift = np.clip(road_half + verge - cut[:, c], 0.0, None)
        shift = np.where(np.isfinite(shift), shift, 0.0)
        cut[:, c] += shift
        cut[:, c + 2] += shift
    return cut


def _add_cuts(frame, plans, elev, verge=0.0):
    """Sets Plan.cut on the plans (None entries skipped): per segment and side a cutting's wall (SHARP_*), from the raw
    ground elev(lat, lon) sampled outward from the carriageway's edge: the ground rising SHARP_MIN_M or more at
    SHARP_GRADE or steeper on average. The data spreads a vertical wall evenly round where it stands: its wall is put
    where the climb is half done (never on the carriageway; a fall: never inside the core, its sidewalk on the level),
    its top where it is done (the Rust kernels earthfast.sharp_probes / sharp_cuts; the ground lookup between them)."""
    import fast
    fast.require(42, "sharp cuttings")
    f64 = np.float64
    keep = [p for p in plans if p is not None and len(p.xy) >= 2]
    jobs = []
    for p in keep:
        q, o = fast._ef.sharp_probes(np.ascontiguousarray(p.xy[:, :2], f64), float(p.road_half), float(p.half),
                                     float(SHARP_REACH_M), float(SHARP_STEP_M))
        jobs.append((p, q, o))
    if not jobs:
        return
    allp = np.concatenate([q for _, q, _ in jobs])
    la, lo, _ = frame.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
    g = np.asarray(elev(np.asarray(la), np.asarray(lo)), f64)       # (as plan_roads' heights)
    k = 0
    for p, q, o in jobs:
        cut = fast._ef.sharp_cuts(np.ascontiguousarray(g[k:k + len(q)]), np.ascontiguousarray(p.z, f64), o,
                                  float(p.road_half), float(p.half), float(SHARP_MIN_M), float(SHARP_GRADE),
                                  float(SHARP_STEP_M), int(SHARP_RUN_N))
        k += len(q)
        if cut is not None:
            # (none at a tight bend: a hairpin's apex and approaches are graded smoothly, no wall into the turn)
            bend = bend_zone(p.xy[:, :2])
            cut = cut.copy()
            if bend.any():
                cut[bend[:-1] | bend[1:]] = np.nan
            # (a cutting's wall at most WALL_MAX_H_M tall, the ground above it sloping back to the hill as real pass
            # walls do; a 12 m wall stood between the Stelvio's hairpin legs, 2026-10-21)
            zmin = np.minimum(p.z[:-1], p.z[1:])
            for c in (1, 4):
                cut[:, c] = np.minimum(cut[:, c], zmin + getattr(p, "max_wall", WALL_MAX_H_M))
            if verge > 0:           # the Finnish fork: a grass verge between the road and the rock (CUT_VERGE_M)
                cut = _verge(cut, p.road_half, verge)
            if np.isfinite(cut[:, 0]).any() or np.isfinite(cut[:, 3]).any():
                p.cut = cut


def median_profile(z, step, window_m):
    """Running median over window_m m (edges padded with their end values): takes out the DEM's short spikes and pits
    (a tree, a car, a culvert, a building edge in the surface model) that a Gaussian only spreads into a bump. Pure."""
    h = int(window_m / step) // 2
    if h < 1 or len(z) < 3:
        return z
    zp = np.pad(np.asarray(z, np.float64), h, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(zp, 2 * h + 1), axis=1)


def profile_sigma(spec):
    """m of profile smoothing for the road's class (the Finnish fork: real roads are engineered with long vertical
    curves, the larger the road the longer; 12 m everywhere left bumps the roads do not have)."""
    if spec.kind not in roadsmod.BUILT:
        return DIRT_SIGMA_M
    return CLASS_SIGMA_M.get(spec.cls.removesuffix("_link"), SIGMA_M)


def smooth_profile_ends(z, step, sigma):
    """smooth_profile with the ends extended along their grade (odd reflection about the end points) rather than
    mirrored: a mirrored end flattens a sloped road towards its end, 2 m off at a junction with 30 m+ smoothing. Pure."""
    n = len(z)
    k = min(n - 1, int(3 * sigma / step))
    if k < 1:
        return smooth_profile(z, step, sigma)
    z = np.asarray(z, np.float64)
    a, b = 2 * z[0] - z[k:0:-1], 2 * z[-1] - z[-2:-k - 2:-1]
    return smooth_profile(np.concatenate([a, z, b]), step, sigma)[k:k + n]


def _plan(xy, z, spec, abut=(False, False)):
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile_ends(median_profile(z, PROFILE_STEP, MEDIAN_M if built else DIRT_MEDIAN_M), PROFILE_STEP,
                             profile_sigma(spec))
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass)
    every = max(1, int(round(VERTEX_STEP / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) if built else spec.width
    if spec.median:             # a divided road's carriageway: never over the other one
        width = max(spec.width, roadsmod.dual_cap(spec.median, width))
    plan = Plan(xy[keep], zs[keep], width / 2 + shoulder(spec), abut, road_half=width / 2)
    plan.max_wall = wall_max_h(spec)
    return plan


def road_rank(spec):
    """Higher: the road that keeps its profile at a junction (snap_junctions); a link ranks just under its class."""
    cls = spec.cls or ""
    base = cls.removesuffix("_link")
    r = JUNCTION_RANK.get(base, 0) * 2
    return r - 1 if cls != base else r


def _nearest_on(p, x, y):
    """(distance, height, arc position from p's nearer end) of the point of Plan p's centreline nearest (x, y)."""
    a, b = p.xy[:-1, :2], p.xy[1:, :2]
    d = b - a
    ll = np.maximum((d * d).sum(1), 1e-12)
    t = np.clip(((x - a[:, 0]) * d[:, 0] + (y - a[:, 1]) * d[:, 1]) / ll, 0.0, 1.0)
    q = a + d * t[:, None]
    dist = np.hypot(q[:, 0] - x, q[:, 1] - y)
    i = int(dist.argmin())
    seg = np.sqrt(ll)
    s = float(seg[:i].sum() + seg[i] * t[i])
    return float(dist[i]), float(p.z[i] + (p.z[i + 1] - p.z[i]) * t[i]), min(s, float(seg.sum()) - s)


def snap_junctions(plans, specs):
    """The Finnish fork: a side road's end at a junction takes the height of the road it joins, eased in over
    JUNCTION_TAPER_M along the side road (each road is smoothed on its own: a side road met the main road up to a metre
    off, blended over BLEND_R only: a bump at every turn-off). Plans are changed in place, the higher ranks first, so
    a residential street joining a tertiary road meets it at its already adjusted height. plans: [Plan], specs:
    their Specs."""
    order = sorted(range(len(plans)), key=lambda j: -road_rank(specs[j]))
    rank = np.array([road_rank(sp) for sp in specs])
    boxes = np.array([q.bbox for q in plans], np.float64).reshape(-1, 4)
    for j in order:
        p = plans[j]
        if len(p.xy) < 2:
            continue
        seg = np.hypot(*np.diff(p.xy[:, :2], axis=0).T)
        s = np.r_[0.0, np.cumsum(seg)]
        length = s[-1]
        dz = np.zeros(len(p.z))
        for end in (0, -1):
            x, y = float(p.xy[end, 0]), float(p.xy[end, 1])
            best = None
            near = np.flatnonzero((rank >= rank[j]) & (boxes[:, 0] <= x) & (x <= boxes[:, 2]) & (boxes[:, 1] <= y)
                                  & (y <= boxes[:, 3]))
            for k in near:
                if k == j:
                    continue
                q = plans[k]
                dist, zq, from_end = _nearest_on(q, x, y)
                if dist > q.road_half + JUNCTION_REACH_M:
                    continue
                if rank[k] == rank[j] and from_end < JUNCTION_END_M:
                    continue        # the same class end to end: a road's continuation, not a junction
                if best is None or dist < best[0]:
                    best = (dist, zq)
            if best is None:
                continue
            delta = best[1] - p.z[end]
            if abs(delta) > JUNCTION_MAX_M:
                continue            # a ramp up to an overpass, or bad data: as it was
            taper = min(JUNCTION_TAPER_M, 0.5 * length)
            u = (s if end == 0 else length - s) / max(taper, 1e-6)
            dz += delta * np.where(u < 1.0, 0.5 * (1.0 + np.cos(np.pi * np.clip(u, 0.0, 1.0))), 0.0)
        if dz.any():
            p.z = p.z + dz


def shoulder(spec):
    """m of flat ground beyond the carriageway edge, each side. Like the core width, never from the land
    cover: a road class that gets sidewalks in towns keeps a sidewalk-wide verge everywhere."""
    if spec.kind not in roadsmod.BUILT:
        return 0.0
    if spec.cls in roadsmod.SIDEWALK_CLASSES or (spec.cls == "trunk" and (not spec.oneway
                                                                          or roadsmod._arterial(spec))):
        return spec.walk + SIDEWALK_MARGIN_M
    return SHOULDER_M


def _twin_trimmed(secs):
    """secs ([(fid, spec, xy, z, kind)]) with the covered sections of twin tunnels (roads.twin_tunnels: side by side,
    one tube round both) cut to the stretch they share: one twin's portal a few metres deeper than the other's, the
    other's ground kept as it is over it (TUNNEL_KEEP_M wide) left the deeper one's approach on the hill there, its road
    mesh a wedge up into their shared mouth (Réunion's Route des Tamarins, GitHub #84). Pure."""
    cov = [n for n, sec in enumerate(secs) if sec[4] and len(sec[2]) >= 2]
    if len(cov) < 2:
        return secs
    parts = [(secs[n][0], secs[n][1], np.asarray(secs[n][2], np.float64), np.asarray(secs[n][3], np.float64), 0.0)
             for n in cov]
    out = list(secs)
    for i, j, _mid, _w in roadsmod.twin_tunnels(parts):
        for a, b in ((i, j), (j, i)):
            xa, xb = parts[a][2], parts[b][2]
            ub = xb[-1] - xb[0]
            ub = ub / max(float(np.hypot(*ub)), 1e-9)
            t = (xa - xb[0]) @ ub                   # along b, from its start
            keep = (t >= 0.0) & (t <= float((xb[-1] - xb[0]) @ ub))
            if keep.sum() >= 2 and not keep.all():
                fid, spec, xy, z, kind = secs[cov[a]]
                k = np.flatnonzero(keep)
                out[cov[a]] = (fid, spec, np.asarray(xy)[k[0]:k[-1] + 1], np.asarray(z)[k[0]:k[-1] + 1], kind)
    return out


class Grader:
    """The road plans of one anchor frame, per map cell, made once (worker threads; it fetches the road
    cells it lacks)."""

    def __init__(self, frame, mapdata, elev, widths=None):
        self.frame, self.md, self.elev, self.widths = frame, mapdata, elev, widths
        self.finnish = in_finland(frame.lat0, frame.lon0)     # the Finnish fork's verges in cuttings
        self.cells = {}
        self.failed = {}        # cell key -> monotonic time of the last failed fetch
        self.lock = threading.Lock()
        self.key_locks = {}     # cell key -> Lock: one thread plans a cell, the others wait for it
        self.covers = {}        # cell key -> [(xy (k, 2), half width)]: the tunnels whose ground over them stays
        self.keeps = {}         # cell key -> [(xy (k, 2), half width)]: the UNGRADED roads' carriageways
        self.opens = {}         # cell key -> [(xy (k, 2), Spec)]: the tunnels' open sections (roads in the open)
        self.shared = None      # plancache.PlanCache: the cells' plans shared with the server's other processes
        self.wet = None         # wet(lat, lon) -> water depth (m): tunnels under water are left out, as
                                # roads.TunnelProfiles leaves them (their "open" sections graded a dam across the river:
                                # the Maastunnel's bed raised from -6 m to -0.4 m, storm-ace's GitHub #85)

    def _cell_plans(self, key):
        with self.lock:
            got = self.cells.get(key)
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
        shared, mine = (self.shared.claim(key) if self.shared is not None else (None, False))
        try:
            got = shared if shared is not None else self._make_cell(key)
            if got is None:
                return []
            if mine:
                self.shared.store(key, *got)
        finally:
            if mine:
                self.shared.release(key)
        plans, keeps, covers, opens = got
        with self.lock:
            self.cells.setdefault(key, plans)
            self.keeps.setdefault(key, keeps)
            self.covers.setdefault(key, covers)
            self.opens.setdefault(key, opens)
            return self.cells[key]

    def _make_cell(self, key):
        """(plans, keeps, covers, opens) of cell key, or None when its roads could not be fetched."""
        try:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        except Exception as e:      # no road data: ungraded (asked again after RETRY_S)
            log.debug("grading: roads of %s: %s: %s", key, type(e).__name__, e)
            with self.lock:
                self.failed[key] = time.monotonic()
            return None
        plans = []
        if feats is not None and len(feats):
            specs = roadsmod.road_specs(feats, None, self.widths)
            parts, ids, clipped = [], [], []
            box = self._clip_box(key)
            keeps = []
            for i in range(len(feats)):
                if specs[i] is not None and specs[i].cls in UNGRADED:
                    for ring in roadsmod.ground_parts(feats, i):
                        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        keeps.append((np.stack([np.asarray(e), np.asarray(n)], axis=1),
                                      specs[i].width / 2 + UNGRADED_MARGIN_M))
                    continue
                if specs[i] is None:
                    continue
                for ring in roadsmod.ground_parts(feats, i):     # not bridges / tunnels
                    pieces = self._near_cell(np.asarray(ring, np.float64), box)
                    for r in pieces:
                        parts.append((r, specs[i]))
                        ids.append(str(feats.ids[i]))
                        clipped.append(len(pieces) > 1 or r is not ring)
            pspecs = []
            abut = None
            if (ABUT_FILL_ON or APPROACH_ON) and parts:
                try:
                    pe = np.array([[r[0], r[-1]] for r, _sp in parts], np.float64).reshape(-1, 2)
                    e, n, _ = self.frame.geodetic_to_enu(pe[:, 1], pe[:, 0], 0.0)
                    abut = self._abut_ends(key, np.stack([np.asarray(e), np.asarray(n)], axis=1)).reshape(-1, 2)
                except Exception as e:      # no fills rather than no grading
                    log.debug("grading: abutments of %s: %s: %s", key, type(e).__name__, e)
            pclip = []
            for fid, part, cl, p in zip(ids, parts, clipped, plan_roads(self.frame, parts, self.elev, abut,
                                                                         CUT_VERGE_M if self.finnish else 0.0)):
                if p is not None:
                    p.rid = road_id(fid)
                    plans.append((fid, p))
                    pspecs.append(part[1])
                    pclip.append(cl)
            if JUNCTION_SNAP_ON and plans:
                try:
                    snap_junctions([p for _f, p in plans], pspecs)
                except Exception as e:      # blended as upstream rather than no grading
                    log.debug("grading: junctions of %s: %s: %s", key, type(e).__name__, e)
            if APPROACH_ON and plans:
                try:
                    self._lift_approaches(key, plans, pspecs)
                except Exception as e:      # ungraded approaches rather than no grading
                    log.debug("grading: approaches of %s: %s: %s", key, type(e).__name__, e)
            if UNDER_ON and plans:
                try:
                    self._lower_underpasses(key, plans)
                except Exception as e:      # as the data has it rather than no grading
                    log.debug("grading: underpasses of %s: %s: %s", key, type(e).__name__, e)
            # a long road's piece (planned with CLIP_MARGIN_M of the road either side for its profile) kept only within
            # the cell's own box: every stretch of road in one cell's plans, once (overlapping pieces from neighbouring
            # cells counted a road up to 9 times, and every batch of points graded differently: patches of ground
            # through the Stelvio's asphalt, GitHub #108's 2.5 m step at a bridge end in Meppen, 2026-10-21)
            own = self._clip_box(key, 0.0)
            if own is not None:
                kept = []
                for (fid, p), cl in zip(plans, pclip):
                    kept.extend((fid, q) for q in (trim_plan(p, own) if cl else [p]))
                plans = kept
            secs = self._tunnel_sections(feats, key, [p for _f, p in plans])
            plans.extend((fid + TUNNEL_TAG, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                         for fid, spec, xy, z, k in secs if k == 0)
            plans.extend(self._mouth_plans(secs))
            covers = [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, k in _twin_trimmed(secs) if k]
            opens = [(xy, spec) for _f, spec, xy, _z, k in secs if k == 0]
        else:
            covers, opens, keeps = [], [], []
        return plans, keeps, covers, opens

    def _clip_box(self, key, margin=CLIP_MARGIN_M):
        """The cell's box in the frame (east / north), margin wider each side; None for a key without bounds."""
        if not hasattr(key, "bounds"):
            return None
        lon0, lat0, lon1, lat1 = key.bounds()
        e, n, _ = self.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                             0.0)
        e, n = np.asarray(e), np.asarray(n)
        m = margin
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
        ok = np.repeat([s is not None and s.cls not in APPROACH_SKIP and not _is_link(s) for s in specs], 2)
        touch = self._abut_ends(key, pe, ok).reshape(-1, 2)
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
                lift, others = got
                a = approach_add(p.xy, at_end, lift, p.half, others)
                if a is not None:
                    add = a if add is None else np.maximum(add, a)
            if add is not None:
                np_ = Plan(p.xy, p.z + add, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall = p.rid, p.max_wall
                plans[i] = (fid, np_)

    def _lower_underpasses(self, key, plans):
        """Lower, in place, the plans that cross under a short never-raised overpass (UNDER_*). The bridges are cell
        key's and its neighbours' (a plan is planned in every cell it passes; each sees the same bridges)."""
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        pad = 0.002
        keys = set(mapcells.cells_for_bbox(lat0 - pad, lon0 - pad, lat1 + pad, lon1 + pad)) | {key}
        bridges = []
        for k in sorted(keys, key=str):
            c = self._cell_enu(k, ground=False)
            for line, cls in zip(c["bridges"], c.get("bridge_cls", [None] * len(c["bridges"]))):
                if cls is None:
                    continue
                if not (cls in APPROACH_SKIP or cls.endswith("_link")):
                    continue
                bridges.append(line)
        if not bridges:
            return
        # (single spans: neither end continues into another bridge part; deduplicated across cells)
        uniq, seen = [], set()
        for b in bridges:
            k2 = (round(float(b[0, 0]), 1), round(float(b[0, 1]), 1), round(float(b[-1, 0]), 1), len(b))
            if k2 not in seen:
                seen.add(k2)
                uniq.append(b)
        spans = []
        for b in uniq:
            L = float(np.hypot(*np.diff(b, axis=0).T).sum())
            if L > UNDER_MAX_BRIDGE_M or L < 2 * APPROACH_MIN_CROSS_M:
                continue
            joined = any(o is not b and min(np.hypot(*(o[0] - e)), np.hypot(*(o[-1] - e))) < 0.75
                         for o in uniq for e in (b[0], b[-1]))
            if not joined:
                spans.append((b, L))
        if not spans:
            return
        ends = np.array([[b[0], b[-1]] for b, _L in spans], np.float64).reshape(-1, 2)
        ela, elo, _ = self.frame.enu_to_geodetic(ends[:, 0], ends[:, 1], 0.0)
        ez = np.asarray(self.elev(np.asarray(ela), np.asarray(elo)), np.float64).reshape(-1, 2)
        for i, (fid, p) in enumerate(plans):
            xy = np.asarray(p.xy, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            cr = roadsmod._crossings([b for b, _L in spans], [xy])
            if not cr:
                continue
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            z = np.asarray(p.z, np.float64).copy()
            changed = False
            for c in cr:
                bi, arc_b, pt = c[0], c[1], np.asarray(c[2], np.float64)
                b, L = spans[bi]
                if arc_b < APPROACH_MIN_CROSS_M or arc_b > L - APPROACH_MIN_CROSS_M:
                    continue                    # a junction at the bridge's end
                deck = float(ez[bi, 0] + (ez[bi, 1] - ez[bi, 0]) * arc_b / L)
                # where along this road (its nearest vertex segment)
                d = np.hypot(xy[:, 0] - pt[0], xy[:, 1] - pt[1])
                sc = float(s[int(np.argmin(d))])
                zc = float(np.interp(sc, s, z))
                target = max(deck - UNDER_CLEAR_M, zc - UNDER_MAX_DROP_M)
                if target >= zc - 0.1:
                    continue
                z = np.minimum(z, target + UNDER_GRADE * np.abs(s - sc))
                changed = True
            if changed:
                np_ = Plan(p.xy, z, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall = p.rid, p.max_wall
                plans[i] = (fid, np_)

    def _abut_ends(self, key, pe, ok=None):
        """Which of the points pe (n, 2) (road ends; ok: those to look at) near cell key are within APPROACH_TOUCH_M
        of a bridge's end in their own cell."""
        la, lo, _ = self.frame.enu_to_geodetic(pe[:, 0], pe[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        dla = np.maximum(np.maximum(lat0 - la, la - lat1), 0.0) * 111320.0
        dlo = np.maximum(np.maximum(lon0 - lo, lo - lon1), 0.0) * 111320.0 * np.cos(np.radians(la))
        touch = np.hypot(dla, dlo) <= APPROACH_CELL_M
        if ok is not None:
            touch &= ok
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
        return touch

    def _end_lift(self, xy):
        """(lift m, other road lines [(xy (k, 2), half width)]) when the road end xy is a bridge abutment to raise, else
        None. A pure function of the map around the bridge (its cells, fetched as needed), cached per point."""
        k = (round(float(xy[0]) * 2), round(float(xy[1]) * 2))
        with self.lock:
            if not hasattr(self, "end_cache"):
                self.end_cache = {}
            if k in self.end_cache:
                return self.end_cache[k]
        res = None
        try:
            res = self._end_lift_calc(np.asarray(xy, np.float64))
        except Exception as e:      # no ramp rather than no grading
            log.debug("grading: approach at %s: %s: %s", k, type(e).__name__, e)
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
        f = self.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
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
                bcls.append(getattr(sp, "cls", None))
            else:
                ground_l.append((line, sp.width / 2 + shoulder(sp), sp.cls))
        out = {"bridges": bridges, "bridge_cls": bcls, "ground": ground_l, "end_of": end_of,
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
        keys = sorted(set(mapcells.cells_for_points(lam, lom, pad, padlon)) | {home}, key=str)
        cs = [self._cell_enu(k) for k in keys]
        bridges = [b for c in cs for b in c["bridges"]]
        lo_b, hi_b = bridge.min(0) - APPROACH_MAX_RUN - 50.0, bridge.max(0) + APPROACH_MAX_RUN + 50.0
        ground, others, seen = [], [], set()
        chain = bridge_chain(bridge, bridges)
        spans = [bridge] + chain        # (a road under any part of the chain is spanned, not beside the ramp: #138)
        blo = np.min([b.min(0) for b in spans], axis=0)
        bhi = np.max([b.max(0) for b in spans], axis=0)
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
                under = not ((line.max(0) < blo).any() or (line.min(0) > bhi).any()) and \
                    bool(roadsmod._crossings(spans, [line]))
                # the roads beside the ramp, which its fill slope must not reach (not the ones the bridge spans: its
                # abutment stands between)
                if not touches and not under:
                    others.append((line, half))

        def z_at(p):
            la_, lo_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(la_, lo_), np.float64)
        for end, lift in bridge_lifts(bridge, bridges, ground, z_at, chain):
            if np.hypot(*(end - xy)) <= APPROACH_TOUCH_M:
                return lift, others
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

    def _chains_for(self):
        """A roads.TunnelProfiles of this frame for its chain questions (drowned): the same chains, from the same map
        cells, as the road tiles' and terrain blocks' own."""
        if getattr(self, "_chains", None) is None:
            md = self.md
            t = roadsmod.TunnelProfiles(self.frame, lambda key: md.cell(key, layers=("roads",), kind="demand")
                                        .layers.get("roads"), self.elev)
            t.wet = self.wet
            self._chains = t
        return self._chains

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
                if self.wet is not None and \
                        (np.asarray(self.wet(la, lo)) > 0.5).mean() > roadsmod.TUNNEL_WET_SHARE:
                    continue
                if self.wet is not None and self._chains_for().drowned((str(feats.ids[i]), j), ring):
                    continue            # (a dry piece of an underwater tunnel: left out with it, GitHub #128)
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
        return [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, kind in _twin_trimmed(self._tunnel_sections(feats))
                if kind]

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

    def _plan_missing(self, keys):
        """The cells of keys not planned yet, planned at once on up to PLAN_THREADS threads (their map data and
        elevation downloads overlap: a fresh place's first block planned its 3-4 cells one after another, 27-35 s of
        its wait, 2026-10-22); the caller's urgent elevation lane (a teleport's first block) carried over."""
        with self.lock:
            missing = [k for k in keys if k not in self.cells]
        if len(missing) < 2:
            return
        dem = getattr(self, "dem", None)
        urgent = dem is not None and getattr(dem, "is_urgent", lambda: False)()

        def one(k):
            if urgent:
                with dem.urgent():
                    return self._cell_plans(k)
            return self._cell_plans(k)
        with ThreadPoolExecutor(min(len(missing), PLAN_THREADS), thread_name_prefix="plans") as ex:
            list(ex.map(one, missing))

    def plans(self, lat, lon, roads_only=False):
        """The Plans touching the points' area (each road once); roads_only: not the tunnels' (TUNNEL_TAG)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        pad = (T_MAX + 40.0) / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lat)))))
        keys = mapcells.cells_for_points(lat, lon, pad, padlon)
        if len(keys) > SPREAD_CELLS:        # points far apart: only the cells around them
            keys = mapcells.cells_near_points(lat, lon, pad, padlon)
        self._plan_missing(keys)
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
        race = self._on_kept(e, n)
        if race is not None:                # (the terrain is a racetrack's surface: GitHub #143)
            out[race] = base.ravel()[race]
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
        for key in mapcells.cells_for_points(lat, lon):
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

    def _on_kept(self, e, n):
        """Which points lie on an UNGRADED road's carriageway (+ UNGRADED_MARGIN_M), or None."""
        with self.lock:
            zones = [z for v in self.keeps.values() for z in v]
        if not zones or not len(e):
            return None
        import fast
        fast.require(0, "over_tunnel")
        mask = np.zeros(len(e), bool)
        ee, nn = np.ascontiguousarray(e, np.float64), np.ascontiguousarray(n, np.float64)
        for xy, hw in zones:
            fast._ef.over_tunnel(mask, ee, nn, np.ascontiguousarray(xy[:, :2], np.float64), float(hw), 0.0)
        return mask if mask.any() else None

    def _over_tunnels(self, e, n):
        """Which points lie over a covered tunnel (its half width + TUNNEL_KEEP_M), or None."""
        with self.lock:
            zones = [z for v in self.covers.values() for z in v]
        if not zones or not len(e):
            return None
        import fast
        fast.require(0, "over_tunnel")      # (Rust: per segment near the points, without the GIL)
        mask = np.zeros(len(e), bool)
        ee, nn = np.ascontiguousarray(e, np.float64), np.ascontiguousarray(n, np.float64)
        for xy, hw in zones:
            fast._ef.over_tunnel(mask, ee, nn, np.ascontiguousarray(xy[:, :2], np.float64), float(hw),
                                 float(TUNNEL_KEEP_IN_M))
        return mask if mask.any() else None


def trim_plan(p, box):
    """The pieces of Plan p within box (x0, y0, x1, y1): cut at its edges (earthfast.clip_segments), heights and
    cuttings with them; an end not at p's own end leads onto no bridge. [Plan]."""
    import fast
    fast.require(44, "clip_segments")
    xy = np.ascontiguousarray(np.asarray(p.xy, np.float64)[:, :2])
    if len(xy) < 2:
        return [p]
    t = np.asarray(fast._ef.clip_segments(xy, tuple(float(v) for v in box)))
    ok = np.isfinite(t[:, 0])
    if ok.all() and (t[:, 0] == 0.0).all() and (t[:, 1] == 1.0).all():
        return [p]
    out, i, n = [], 0, len(ok)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and ok[j + 1] and t[j, 1] == 1.0 and t[j + 1, 0] == 0.0:
            j += 1
        t0, t1 = t[i, 0], t[j, 1]
        a = xy[i] + (xy[i + 1] - xy[i]) * t0
        b = xy[j] + (xy[j + 1] - xy[j]) * t1
        za = p.z[i] + (p.z[i + 1] - p.z[i]) * t0
        zb = p.z[j] + (p.z[j + 1] - p.z[j]) * t1
        pxy = np.vstack([a, xy[i + 1:j + 1], b])
        pz = np.r_[za, p.z[i + 1:j + 1], zb]
        if np.hypot(*(pxy[-1] - pxy[0])) > 1e-6 or len(pxy) > 2:
            q = Plan(pxy, pz, p.half, (p.abut[0] and i == 0 and t0 == 0.0, p.abut[1] and j == n - 1 and t1 == 1.0),
                     None if p.cut is None else np.asarray(p.cut)[i:j + 1].copy(), p.road_half)
            q.rid = getattr(p, "rid", -1)
            q.max_wall = getattr(p, "max_wall", WALL_MAX_H_M)
            out.append(q)
        i = j + 1
    return out


def road_id(fid):
    """A stable integer id of a map feature (grade_points: a road never gives way to itself, though each map cell
    plans its own stretch of it: the Stelvio Pass's hairpins sank under their own other stretches, 2026-10-21)."""
    import zlib
    return zlib.crc32(str(fid).encode()) & 0x7FFFFFFF


def grade_points(px, py, base, plans, lift=0.0):
    """base at the points (px, py) graded under the plans (see the module doc), by the Rust loop (native/earthfast
    grade_points: a Python loop per segment was the server's largest CPU cost while driving, 2026-10-12). Pure."""
    px = np.asarray(px, np.float64)
    py = np.asarray(py, np.float64)
    base = np.asarray(base, np.float64)
    out = base.copy()
    if not len(px):
        return out
    x0, y0, x1, y1 = px.min(), py.min(), px.max(), py.max()
    # every segment of the plans near the points: ends, end heights, reach (core half width + T_MAX)
    A, B, ZA, ZB, H, RH, RID, AB, CUT = [], [], [], [], [], [], [], [], []
    for k, p in enumerate(plans):
        if p.bbox[2] < x0 or p.bbox[0] > x1 or p.bbox[3] < y0 or p.bbox[1] > y1 or len(p.xy) < 2:
            continue
        A.append(p.xy[:-1])
        B.append(p.xy[1:])
        ZA.append(p.z[:-1])
        ZB.append(p.z[1:])
        H.append(np.full(len(p.xy) - 1, p.half))
        RH.append(np.full(len(p.xy) - 1, getattr(p, "road_half", p.half)))
        rid = getattr(p, "rid", -1)
        RID.append(np.full(len(p.xy) - 1, rid if rid is not None and rid >= 0 else 2 ** 40 + k, np.int64))
        ab = np.zeros(len(p.xy) - 1, bool)
        abut = getattr(p, "abut", (False, False))
        if abut[0] or abut[1]:
            arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p.xy[:, :2], axis=0).T))]
            if abut[0]:
                ab |= arc[:-1] < ABUT_NEAR_M
            if abut[1]:
                ab |= arc[-1] - arc[1:] < ABUT_NEAR_M
        AB.append(ab)
        cut = getattr(p, "cut", None)
        CUT.append(cut if cut is not None else np.full((len(p.xy) - 1, 6), np.nan))
    if not A:
        return out
    A, B, ZA, ZB, H, RH, RID, AB, CUT = (np.concatenate(v) for v in (A, B, ZA, ZB, H, RH, RID, AB, CUT))
    import fast
    fast.require(41, "grade_points")         # (the Rust loop: native/earthfast)
    f64 = np.float64
    return fast._ef.grade_points(np.ascontiguousarray(px, f64), np.ascontiguousarray(py, f64),
                                 np.ascontiguousarray(base, f64), np.ascontiguousarray(A[:, :2], f64),
                                 np.ascontiguousarray(B[:, :2], f64), np.ascontiguousarray(ZA, f64),
                                 np.ascontiguousarray(ZB, f64), np.ascontiguousarray(H, f64),
                                 np.ascontiguousarray(RH, f64), np.ascontiguousarray(RID, np.int64),
                                 np.ascontiguousarray(AB, np.uint8), np.ascontiguousarray(CUT, f64), float(lift),
                                 float(SLOPE_RUN), float(T_MIN), float(T_MAX), float(BLEND_R), float(BUCKET),
                                 float(WALL_CUT_M), float(WALL_T_M), float(WALL_FLAT_M), float(WALL_AHEAD_M),
                                 bool(SEP_ON), float(SEP_FROM_M), float(SEP_FULL_M), float(CORE_FADE_M),
                                 float(SIDE_FADE_M), float(SHARP_STEP_M))


def bridge_chain(line, all_bridges):
    """The bridge parts joined to line through their ends (within 0.75 m), line itself excluded. Pure."""
    line = np.asarray(line, np.float64)[:, :2]
    parts = [np.asarray(b, np.float64)[:, :2] for b in all_bridges]
    parts = [b for b in parts if not (len(b) == len(line) and np.array_equal(b, line))]
    out, frontier, used = [], [line[0], line[-1]], set()
    while frontier:
        e = frontier.pop()
        for k, b in enumerate(parts):
            if k in used:
                continue
            if min(np.hypot(*(b[0] - e)), np.hypot(*(b[-1] - e))) < 0.75:
                used.add(k)
                out.append(b)
                frontier += [b[0], b[-1]]
    return out


def bridge_lifts(line, all_bridges, ground_lines, z_at, chain=None):
    """[(end xy (2,), lift m)] of a bridge part (line (k, 2) ENU m): at each of its ends that no other bridge part
    continues from (an abutment), how far the ground there must be raised for the deck to clear every road crossing
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

    def joined_at(end):
        for other in all_bridges:
            if other is line or (len(other) == len(line) and np.array_equal(other, line)):
                continue
            o = np.asarray(other, np.float64)
            if min(np.hypot(*(o[0, :2] - end)), np.hypot(*(o[-1, :2] - end))) < 0.75:
                return True
        return False
    joined = (joined_at(line[0]), joined_at(line[-1]))
    # (not a junction at an abutment; at a joint with the chain's next part the crossing is mid-bridge: Commerce
    # Street's bridge over I-95 in Springfield VA, split 2 m from where the motorway passes under, GitHub #138)
    under = ((arcs >= APPROACH_MIN_CROSS_M) | joined[0]) & ((arcs <= s[-1] - APPROACH_MIN_CROSS_M) | joined[1])
    zu, arcs = zu[under], arcs[under]
    pts = np.array([c[2] for c in cr], np.float64)[under].reshape(-1, 2)
    # (the crossings under the rest of its chain, by their distance from the abutment: Overture splits a bridge where
    # other ways meet it, and the road under it may pass under the next part, GitHub #138)
    more = []
    for part in (chain or ()):
        for c in roadsmod._crossings([part], ground_lines):
            more.append(np.asarray(c[2], np.float64))
    if more:
        mp = np.array(more, np.float64)
        zu = np.r_[zu, z_at(mp)]
        pts = np.r_[pts, mp]
    if not len(zu):
        return []
    out = []
    for (end, at), jn in zip(((line[0], 0.0), (line[-1], s[-1])), joined):
        if jn:
            continue
        d = np.r_[np.abs(arcs - at), np.hypot(*(pts[len(arcs):] - end).T)] if more else np.abs(arcs - at)
        far = d >= APPROACH_MIN_CROSS_M     # (a road meeting the abutment itself: a junction at grade)
        if not far.any():
            continue
        d, zu_e = d[far], zu[far]
        need = zu_e + roadsmod.DECK_ROAD_CLEAR + APPROACH_MARGIN_M \
            - roadsmod.DECK_GRADE * np.maximum(d - APPROACH_FLAT_M, 0.0)
        lift = float(need.max() - z_at(end[None])[0])
        if lift >= APPROACH_MIN_LIFT:
            out.append((np.asarray(end, np.float64), min(lift, APPROACH_MAX_LIFT)))
    return out


def fill_abutment(z, at_end, step=PROFILE_STEP):
    """The raw profile z (a sample every step m) of a road leading onto a bridge at its first (at_end False) or last
    sample, its last ABUT_NEAR_M filled up to the straight grade of the stretch from there to ABUT_FAR_M where they
    fall at least ABUT_STEEP more steeply than it toward the bridge (ABUT_FILL_ON). Returns z itself when nothing is
    filled. Pure."""
    z = np.asarray(z, np.float64)
    p = z[::-1] if at_end else z
    d = np.arange(len(p)) * step
    far = (d >= ABUT_NEAR_M) & (d <= ABUT_FAR_M)
    if far.sum() < 4:
        return z
    b, a = np.polyfit(d[far], p[far], 1)
    n = int((d < ABUT_NEAR_M).sum())
    e = a + b * d[:n]
    if e[0] - p[0] < ABUT_MIN_M or (np.diff(p[:n + 1]) / step - b).max() < ABUT_STEEP:
        return z
    q = p.copy()
    q[:n] = np.minimum(np.maximum(p[:n], e), p[:n] + ABUT_MAX_M)
    return q[::-1] if at_end else q


def approach_add(xy, at_end, lift, half, others=()):
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
    for line, oh in others:
        line = np.asarray(line, np.float64)[:, :2]
        if len(line) < 2:
            continue
        reach = half + oh + APPROACH_GAP_M + SLOPE_RUN * lift
        if (line.max(0) < lo_q - reach).any() or (line.min(0) > hi_q + reach).any():
            continue                    # too far to matter
        q = xy[on]
        a, b = line[:-1], line[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        dist = np.hypot(*(q[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1)).min(1)
        cap = np.maximum(0.0, (dist - half - oh - APPROACH_GAP_M) / SLOPE_RUN)
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
