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
each map cell's plan is made once and cached, so the block and the tiles built later always agree.

Plans do not depend on the anchor (geotiles, 2026-10-29): a cell is planned in the frame of its planning region
(geotiles.PLAN_Z: the ENU plane at the region's centre; heights geodetic, as ever) by that region's _Planner, one per
region per process (PlanStore, shared by every anchor's Grader). A Grader is an anchor's view of them: apply() grades
points in their region's frame (the same heights whatever the anchor: the anchor never enters the sums), plans() and
_cell_plans() give the plans' vertices mapped exactly into the anchor's world (geotiles.world_xy_map). A region's
planner reads a neighbouring region's cells mapped into its own frame (_Planner._import).

GradedDem(dem, grader) is a drop-in for dem.Dem where only sample() is graded; use it for the block
and the levels <= 0 tiles only (coarser tiles sink under the block and cover far more ground).
"""
import logging
import math
import os
import threading
from collections import OrderedDict
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mapcells
import roads as roadsmod

log = logging.getLogger("earthd.grading")

VERSION = 2
PROFILE_STEP = 2.0          # m between profile samples
SIGMA_M = 12.0              # m, profile smoothing of built roads
# ... and of motorways and trunks: real ones keep long, gentle grades, and 12 m kept every bump of the elevation data in
# them where it is a 30 m surface model (BR-381 in Brazil: a junction's pad, forest edges; 20 % grades between them, a
# flat bench halfway down a long descent; GitHub #266). Lidar areas change little: their roads are smooth already.
SIGMA_MAJOR_M = float(os.environ.get("BNE_GRADE_MAJOR", "40"))
# (and primary roads: one through Russian forest kept every 1-3 m bump of GLO-30 over 50-100 m at 12 m, a road of hills
# and walls on flat land, GitHub #335. Not secondary yet: Otto Road's junction with I-80's ramps came out 0.29 m apart
# once its dip under I-80 (underpass_offsets) was laid on the longer smoothing; the ramps' pins need a look first)
MAJOR_CLASSES = ("motorway", "trunk", "primary")      # (not their slip roads: Overture's subclass "link", Spec.link)
DIRT_SIGMA_M = 8.0          # m, profile smoothing of gravel / dirt roads (5 followed the DEM's bumps: GitHub #414)
# longer smoothing for the mid classes too (mpsteppe's PR #289: real roads have long vertical curves, the larger the
# longer; 12 m left bumps the roads do not have); MAJOR_CLASSES keep SIGMA_MAJOR_M
CLASS_SIGMA_M = {"secondary": 25.0, "tertiary": 20.0, "unclassified": 16.0, "residential": 14.0}
# A running median before the smoothing (median_profile): a bridge deck left in the elevation data where it crosses
# over a road is a spike a few samples wide, and the Gaussian spread it into a hump in the road under (GitHub #271, an
# 8.5 m spike near Thetford: a 3.2 m hump). Slopes, crests and steps longer than the window stay.
MEDIAN_M = float(os.environ.get("BNE_GRADE_MEDIAN", "24"))
DIRT_MEDIAN_M = 10.0        # (tracks keep their small real bumps)
RAIL_GRADE_ON = os.environ.get("BNE_RAIL_GRADE", "1") != "0"    # the ground laid to the railways' bed (railways.py)
RAIL_TAG = "rail:"
SHOULDER_M = 1.0            # m of flat shoulder beyond the carriageway, each side
SIDEWALK_MARGIN_M = 0.5     # m of flat ground beyond a sidewalk (roads.SIDEWALK_CLASSES)
LONG_PART_M = 3000.0        # a road part longer than this is planned per cell, only within CLIP_MARGIN_M of the cell
CLIP_MARGIN_M = 400.0
PLAN_THREADS = 4            # cells planned at once (_plan_missing)
PLAN_SETS_MAX = 16          # cell sets whose merged plans are kept (Grader._plan_set; ~1 MB each in a town)
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
# A cutting under this deep is a wall only in a trench (a cutting on both sides: Munich's McGraw-Graben, Central Park's
# transverses); a lone bank of a metre or two beside a road is a graded slope, as real ones are (a 1.5 m concrete wall
# stood in the flat desert beside US-163 in Utah, where there is a low eroded rock bank: Mapillary, 2026-10-29).
# Hillside roads' deeper cuttings keep their walls (WALL_MAX_H_M high at most)
SHARP_LONE_MIN_M = 4.0
SEP_FROM_M = 2.0
SEP_FULL_M = 4.0
CORE_FADE_M = 2.0
SIDE_FADE_M = 2.0           # m of (side - ahead) over which a segment counts as beside the point (ahead: its own road)
RETRY_S = 60.0              # s before a cell whose road data failed is asked for again

# Junctions: a side road's end takes the height of the road it joins (snap_junctions; mpsteppe's PR #290)
JUNCTION_SNAP_ON = os.environ.get("BNE_JUNCTION_SNAP", "1") != "0"
JUNCTION_RANK = {"motorway": 9, "trunk": 8, "primary": 7, "secondary": 6, "tertiary": 5, "unclassified": 4,
                 "residential": 3, "living_street": 2, "service": 1}
JUNCTION_REACH_M = 3.0      # m beyond the joined road's carriageway edge a side road's end still counts as joining it
JUNCTION_END_M = 10.0       # m: a same-class road ending this close to the other's end continues it (no snap)
JUNCTION_TAPER_M = 40.0     # m along the side road the height change is eased in over (half the road when shorter)
JUNCTION_MAX_M = 6.0        # m: a bigger difference is left as it was (bad data; Perm, GitHub #414: 4-5 m steps where a
                            # 30 m DEM smeared an embankment into the junctions)
# Roads side by side where the elevation is only the coarse global model (dem.coarse_at: no lidar; GitHub #414, Perm on
# Copernicus' 30 m: service roads 7 m beside the primary stood 4-5 m over it, where Mapillary shows them at grade): a
# lower-ranked road within COARSE_REACH_M of a higher-ranked one's edge eased to its height (equal ranks halfway), less a
# COARSE_MAX_SLOPE cross slope between their edges (a real hillside keeps its fall); over COARSE_MAX_STEP_M apart they
# are taken as truly separated. Rust: earthfast.ease_coarse.
# (off by default: at Perm the trench turned out to be the underpass dip, which this does not touch; an A/B across
# coarse-elevation places comes first, BNE_COARSE_EASE=1 turns it on)
COARSE_EASE_ON = os.environ.get("BNE_COARSE_EASE", "0") == "1"
COARSE_REACH_M = 20.0
COARSE_MAX_STEP_M = 6.0
COARSE_MAX_SLOPE = 0.25
COARSE_SIGMA_M = 8.0
COARSE_SIDE_M = 12.0        # m: plan_roads' side samples on coarse elevation (the median of three)


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
APPROACH_SKIP = ("motorway", "trunk")   # never raised (their links are: _never_raised); the roads over them rise
WATER_LIFT_ON = os.environ.get("BNE_WATER_FILL", "on") != "off"
WATER_CURVE_M = 16.0        # m from an abutment a deck over water spends on its vertical curve before climbing ...
WATER_MAX_LIFT = 2.0        # ... and its approach fill is raised at most this (any class: a motorway's too)
OBS_HALF_M = 4.0            # m: the half width a bridge line is matched with when its abutment's lift is measured
OBS_END_M = 12.0            # m from the end the measured deck's height is taken at most (its first measured station)
APPROACH_GAP_M = 1.0        # m of flat ground kept between a ramp's fill slope and the road beside it
ENU_CACHE_MAX = 512         # cells' road parts in the anchor frame kept (Grader._cell_enu; ~0.02-0.5 MB each)
APPROACH_CELL_M = 450.0     # m: a plan's end this far from a cell cannot raise what that cell's queries see
APPROACH_CAP_GRADE = 0.10   # steepest grade a ramp falls at where a road beside it caps it (a cap alone dropped 3 m
                            # in 8 m by a side road north of the I-20 bridge near Temple, GA, 2026-10-20)

# Embankments at abutments (GitHub #38: a GPS drive near Presov, Slovakia flipped on the D1). A bridge's map line can end
# down its embankment, short of the abutment: the road's last metres followed the valley's side in the elevation data and
# the deck end, taken from that ground, sat 1.4 m under the road 2 m on (a 12 % deck and a step at 55 mph). A road ending
# at a bridge whose last ABUT_NEAR_M fall much more steeply toward it than its own grade further on keeps that grade to
# its end instead, as the fill under a real approach does (every class, motorways too).
ABUT_FILL_ON = os.environ.get("BNE_ABUT_FILL", "on") != "off"
# A road split at a junction (the map's segments end at every one) is smoothed as one: each part's profile also samples
# the ground along the straightest part that goes on from its end, so the parts meet at one height. Each was smoothed
# alone, and on a slope each end took the average of its own side: on BR-381's 20 % grades the next part began 2 m
# over where the last ended, a ramp at every junction (GitHub #266).
CHAIN_ON = os.environ.get("BNE_GRADE_CHAIN", "on") != "off"
CHAIN_JOIN_M = 1.0          # m: two parts' ends this close are one node
CHAIN_TURN_DEG = 35.0       # the part that goes on turns at most this at the node
CHAIN_LOOK_M = 10.0         # m along each part its direction at the node is taken over
# Underpasses (GitHub #138, #142, #126's rule: "the road under gives way"): a ground road crossing under a short
# overpass that is never raised (APPROACH_SKIP classes and links: a motorway's bridge over a local road) dips to the
# deck's height (its abutments' ground, interpolated) minus UNDER_CLEAR_M, falling back at UNDER_GRADE. Lidar terrain
# removes bridges and interpolates across the gap, at about the deck's height: the road under ran 0.7-2.5 m under the
# girders (A28 near Zwolle, I-84 over NE 181st Ave in Portland, I-395 in Springfield VA, 2026-10-22). Never raises.
UNDER_ON = os.environ.get("BNE_UNDERPASS", "on") != "off"
# Rounded junction corners (roadcorners.py, from PR #363): the fan of asphalt between a curbless corner's arc and its
# roads' old square corner graded at the roads' heights (Grader._make_cell)
CORNERS_ON = os.environ.get("BNE_GRADE_CORNERS", "on") != "off"
UNGRADED = {"raceway"}      # classes planned by nobody: the terrain is their surface (an oval's banking, GitHub #143) ...
UNGRADED_MARGIN_M = 2.0     # ... and nothing else grades their carriageway (+ this: kerbs, apron): a road crossing a
                            # track, or a tunnel's open approach under one, cut a flat trench through Daytona's banking
# The same structural headroom as the deck solver, including the lower road's
# physical surface lift. A stale 6.7 m target forced short motorway decks into
# clearance humps even after their underpass had been graded (Otto, Wyoming).
UNDER_CLEAR_M = roadsmod.DECK_ROAD_CLEAR + ROAD_LIFT
UNDER_GRADE = 0.06          # the dip's ramps
UNDER_MAX_BRIDGE_M = 120.0  # only single short spans (both ends on the ground): a chain's inner ends are no abutments
UNDER_MAX_DROP_M = 8.0      # never lowered more than this (bad data: rather a low bridge than a pit)
UNDER_FLAT_M = 12.0         # m either side of the crossing kept at the dip's full depth (under_dip)
UNDER_MAX_GRADE = 0.10      # a dip's ramp made steeper, up to this, to end inside its road part (under_dip)
UNDER_JOINT_FLAT_M = 12.0   # at a junction, every road takes the deepest dip within this of it ...
UNDER_JOINT_FADE_M = 20.0   # ... blended back to its own over this
# m around a cell's plans whose roads and spans its dips are solved on: the farthest a dip or a loose end reaches
# (underpass_offsets: flat + the longest ramp, a junction's share) and a span's length; every cell sees the same
UNDER_NEAR_M = 4 * VERTEX_STEP  # m past a cell's box its plans' dips are made (to their pieces' cut ends there)
UNDER_REACH_M = UNDER_FLAT_M + UNDER_MAX_DROP_M * math.pi / UNDER_GRADE + 2 * UNDER_JOINT_FLAT_M \
    + UNDER_JOINT_FADE_M + 10.0
# ... and under any other short overpass, by what its ramps could not raise it (BR-381 near Pouso Alegre: a local
# road's bridge whose ramps the frontage roads at both ends held down sat ~1 m over the motorway; the 30 m elevation
# data has no cutting under it, and the AI car drove into its abutment wall, 2026-10-25)
UNDER_ANY_ON = os.environ.get("BNE_UNDERPASS_ANY", "on") != "off"
ABUT_NEAR_M = 14.0          # m from the bridge end the road is checked ...
ABUT_FAR_M = 40.0           # ... against its grade from ABUT_NEAR_M to this far
ABUT_STEEP = 0.15           # a fall this much steeper than that grade, near the end: the valley's side, not the road
ABUT_MIN_M = 0.3            # m: a smaller fill at the end is left out
ABUT_MAX_M = 8.0            # m filled at most


def median_profile(z, step, width):
    """The running median of a profile sampled every `step` m over `width` m (the window shrunk evenly near the ends, so
    an end keeps its height). Pure."""
    import fast
    half = int(round(width / 2.0 / step))
    if half < 1 or len(z) < 3:
        return np.asarray(z, np.float64)
    fast.require(71, "median_profile")
    return fast._ef.median_profile(np.ascontiguousarray(z, np.float64), half)


def smooth_profile(z, step, sigma):
    """Gaussian smoothing of a profile sampled every `step` m (turned about its end points past the ends: the ends keep
    their heights, so the parts of a road split at a junction meet at one height). Pure."""
    import fast
    fast.require(69, "smooth_profile")      # the Rust loop (~7000 roads a 9-tile set)
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
    __slots__ = ("xy", "z", "half", "bbox", "abut", "cut", "road_half", "rid", "max_wall", "bank")

    def __init__(self, xy, z, half, abut=(False, False), cut=None, road_half=None):
        self.xy, self.z, self.half, self.abut = xy, z, float(half), (bool(abut[0]), bool(abut[1]))
        # (n - 1, 6) per segment, NaN where none: a cutting's wall offset from the centre line, its top's height and
        # the top's offset, on the left, then the right (_add_cuts)
        self.cut = cut
        self.road_half = float(road_half) if road_half is not None else float(half)    # the carriageway's half width
        self.rid = -1           # the road (map feature) it is part of: _plan_cell (each cell plans its own stretch of it)
        self.max_wall = WALL_MAX_H_M    # its cuttings' walls at most this tall (wall_max_h)
        self.bank = None                # (n,) its cross slope (m per m toward its left: a banked curve, bank) or None
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


def _normals(xy):
    """Unit normals (k, 2) of a polyline's points (the direction from the neighbours). Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2:
        return np.zeros((len(xy), 2))
    d = np.gradient(xy, axis=0)
    ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
    return np.stack([-d[:, 1] / ln, d[:, 0] / ln], axis=1)


def plan_roads(frame, parts, elev, abut=None, ext=None, pins=None, cuts=True, coarse=False, obs=None):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call); abut: per part (start, end)
    flags, the ends leading onto a bridge (fill_abutment); ext: per part (before, after), the lines (k, 2) in the frame
    from its start / end outward along the part the road goes on in (Grader._extensions), or None: their ground is in
    the part's profile smoothing; pins: per part (start, end) heights its ends are held at, or None (Grader._pins: a
    road ending on a more smoothed one meets it at its height); obs(xy (m, 2), spec) -> measured heights (m,) of a
    part's samples (NaN where none) or None: they stand in for the DEM's (roadz.py, on coarse elevation: Grader._obs).
    Pure given elev and obs."""
    if not parts:
        return []
    ll = np.concatenate([lonlat for lonlat, _ in parts])
    E, N, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    prof, k = [], 0
    for j, (lonlat, spec) in enumerate(parts):
        m = len(lonlat)
        line = np.stack([E[k:k + m], N[k:k + m]], axis=1)
        k += m
        # (an even step, PROFILE_STEP or a little under, so the samples end on both ends: a short last step made the
        # smoothing lean at that end, and the parts of a road met 0.2 m apart on a 20 % grade)
        length = float(np.hypot(*np.diff(line, axis=0).T).sum()) if m >= 2 else 0.0
        step = length / max(1, math.ceil(length / PROFILE_STEP - 1e-9)) * (1.0 + 1e-9) if length > 0 else PROFILE_STEP
        xy, _ = resample(line, step)
        if len(xy) < 2:
            prof.append(None)
            continue
        side, bank_side = [], []
        for out in (ext[j] if ext is not None else (None, None)):
            if out is None or len(out) < 2:
                side.append(np.zeros((0, 2)))
                bank_side.append(np.zeros((0, 2)))
            else:                       # (from the node outward; the node is the part's own end sample)
                bank_side.append(resample(np.asarray(out, np.float64), step)[0][1:])
                # Extra bank geometry must not change longitudinal DEM sampling
                # or the bridge solver's established approach/underpass profiles.
                reach = 3.0 * profile_sigma(spec) + PROFILE_STEP
                arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(out, axis=0), axis=1))]
                if arc[-1] > reach + 1e-8:
                    out = np.vstack([np.asarray(out)[arc < reach], _point_along(out, reach)])
                side.append(resample(np.asarray(out, np.float64), step)[0][1:])
        prof.append((xy, side[0][::-1], side[1], step, bank_side[0][::-1], bank_side[1]))
    pts = [np.concatenate([b, xy, a]) for xy, b, a, *_ in (p for p in prof if p is not None)]
    if not pts:
        return [None] * len(parts)
    allxy = np.concatenate(pts)
    if coarse:
        # coarse elevation (dem.coarse_at, GitHub #414): the median of the centre line and COARSE_SIDE_M to either side,
        # so a pit or a spike narrower than the data's own cells does not drag the road into a walled trench (Perm's
        # primary sat 4-5 m under the ground 7 m beside it, at grade in the street photos)
        nrm = np.concatenate([_normals(q) for q in pts])
        three = np.concatenate([allxy, allxy + nrm * COARSE_SIDE_M, allxy - nrm * COARSE_SIDE_M])
        la, lo, _ = frame.enu_to_geodetic(three[:, 0], three[:, 1], 0.0)
        z3 = np.asarray(elev(la, lo), np.float64).reshape(3, -1)
        zall = np.median(z3, axis=0)
    else:
        la, lo, _ = frame.enu_to_geodetic(allxy[:, 0], allxy[:, 1], 0.0)
        zall = np.asarray(elev(la, lo), np.float64)
    out, k = [], 0
    for j, ((lonlat, spec), pr) in enumerate(zip(parts, prof)):
        if pr is None:
            out.append(None)
            continue
        xy, before, after, step, bank_before, bank_after = pr
        nb, na = len(before), len(after)
        zx = zall[k:k + nb + len(xy) + na]
        k += nb + len(xy) + na
        if obs is not None:
            o = obs(xy, spec)
            if o is not None and len(o) == len(xy) and np.isfinite(o).any():
                zx = zx.copy()
                zx[nb:nb + len(xy)] = np.where(np.isfinite(o), o, zx[nb:nb + len(xy)])
        ab = (False, False)
        if abut is not None and ABUT_FILL_ON:
            ab = (bool(abut[j][0]), bool(abut[j][1]))
            if ab[0] and nb:            # (no extension past an end onto a bridge: its fill is the part's own)
                zx, nb = zx[nb:], 0
            if ab[1] and na:
                zx, na = zx[:len(zx) - na], 0
            z = zx[nb:nb + len(xy)]
            for at_end in (False, True):
                if ab[int(at_end)]:
                    z = fill_abutment(z, at_end)
            zx = np.concatenate([zx[:nb], z, zx[nb + len(xy):]])
        pin = pins[j] if pins is not None else (None, None)
        if pin[0] is not None and not nb and not ab[0]:     # (the end sample held: the smoothing keeps an end's height)
            zx = zx.copy()
            zx[0] = pin[0]
        if pin[1] is not None and not na and not ab[1]:
            zx = zx.copy()
            zx[-1] = pin[1]
        # Curvature and its runoff need the joined-road context too. Computing
        # banking on xy alone puts a partial curvature window at every map
        # segment end, even where the longitudinal profile is continuous.
        if ab[0]:
            bank_before = bank_before[:0]
        if ab[1]:
            bank_after = bank_after[:0]
        bank_xy = np.concatenate([bank_before, xy, bank_after])
        out.append(_plan(xy, zx, spec, ab, (nb, na), step, bank_xy=bank_xy,
                         bank_ext=(len(bank_before), len(bank_after))))
    if SHARP_ON and cuts:           # (cuts False: the profile alone, Grader._stronger_z)
        _add_cuts(frame, [p if p is not None and sp.kind in roadsmod.BUILT else None
                          for p, (_ll, sp) in zip(out, parts)], elev)
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


def _add_cuts(frame, plans, elev):
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
            # (a shallow climb on one side only: no cutting there, SHARP_LONE_MIN_M)
            climb = np.isfinite(cut[:, [1, 4]])
            shallow = climb & ((cut[:, [1, 4]] - zmin[:, None]) < SHARP_LONE_MIN_M)
            lone = shallow & ~climb[:, ::-1]
            for side, c0 in ((0, 0), (1, 3)):
                cut[lone[:, side], c0:c0 + 3] = np.nan
            for c in (1, 4):
                cut[:, c] = np.minimum(cut[:, c], zmin + getattr(p, "max_wall", WALL_MAX_H_M))
            if np.isfinite(cut[:, 0]).any() or np.isfinite(cut[:, 3]).any():
                p.cut = cut


def _point_along(line, d):
    """The point d m along polyline line (k, 2) from its first point (its last when shorter). Walks only as far as it
    needs (a whole line's cumulative length per road end cost Grader._extensions most of its time, 2026-10-25)."""
    acc = 0.0
    for i in range(len(line) - 1):
        ax, ay = float(line[i][0]), float(line[i][1])
        bx, by = float(line[i + 1][0]), float(line[i + 1][1])
        seg = math.hypot(bx - ax, by - ay)
        if seg > 0 and acc + seg >= d:
            t = (d - acc) / seg
            return np.array([ax + (bx - ax) * t, ay + (by - ay) * t])
        acc += seg
    return np.array([float(line[-1][0]), float(line[-1][1])])


def profile_sigma(spec):
    """m: the profile smoothing of a road of this spec."""
    if spec.kind not in roadsmod.BUILT:
        return DIRT_SIGMA_M
    if spec.cls in MAJOR_CLASSES and not getattr(spec, "link", False):
        return max(SIGMA_M, SIGMA_MAJOR_M)
    return max(SIGMA_M, CLASS_SIGMA_M.get(spec.cls, SIGMA_M)) if not getattr(spec, "link", False) else SIGMA_M


def _plan(xy, z, spec, abut=(False, False), ext=(0, 0), step=PROFILE_STEP, bank_xy=None, bank_ext=None):
    """ext: (before, after), samples of z past the part's ends (plan_roads): smoothed with it, then dropped. step: m
    between the samples. bank_xy: the matching extended geometry, trimmed after the curvature/runoff pass."""
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile(median_profile(z, step, MEDIAN_M if built else DIRT_MEDIAN_M), step,
                        profile_sigma(spec))[ext[0]:len(z) - ext[1]]
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass)
    every = max(1, int(round(VERTEX_STEP / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) if built else spec.width
    if spec.median:             # a divided road's carriageway: never over the other one
        width = max(spec.width, roadsmod.dual_cap(spec.median, width))
    plan = Plan(xy[keep], zs[keep], width / 2 + shoulder(spec), abut, road_half=width / 2)
    plan.max_wall = wall_max_h(spec)
    bext = ext if bank_ext is None else bank_ext
    b = bank(xy if bank_xy is None else bank_xy, spec, abut, bext)
    if b is not None:
        if bank_xy is not None:
            b = b[bext[0]:len(bank_xy) - bext[1]]
        plan.bank = b[keep]
    return plan


# Banked curves (the user, 2026-10-27: "improve how roads generate"; BeamNG's Hero Road editor banks its curves, our
# carriageways were graded level across): a motorway's or trunk's curve leans into its turn, the cross slope by the
# design-speed rule e = BANK_E_MAX x min(1, r_min / R), r_min = v² / (127 (e_max + f)) of the class's design speed v
# (km/h) and side friction f; a gentle bend (under 1 %) keeps level. It comes in over BANK_RUNOFF_M and fades out over
# BANK_FADE_M where the road meets a bridge (a deck is level across) or ends without a road going on (a tunnel's
# floor, a junction). The grading pass (grade_points) tilts the core across the carriageway by it.
BANK_ON = os.environ.get("BNE_BANK", "on") != "off"
BANK_E_MAX = 0.08
BANK_DESIGN = {"motorway": (110.0, 0.10), "trunk": (90.0, 0.12)}     # km/h, side friction
BANK_LINK = (60.0, 0.16)                                              # their slip roads
# Sparse motorway map lines are straight chords, often ~100 m between vertices: a 12 m stencil saw each chord as level
# and each corner as a tight bend, 8 % bank pulses (I-80, 41.41135, -105.83960; Discord 2026-10-28 and PR #410). The
# curvature is the road's mean turning over this far either side (earthfast.bank_profile), its context the joined
# roads' too (plan_roads bank_xy), then smoothed.
BANK_STENCIL_M = 100.0      # 200 m baseline
BANK_RUNOFF_M = 100.0       # full 8 % transition at <= 0.08 / 100 m away from ends
BANK_FADE_M = 100.0         # smoothstep to level at a bridge / loose end; no deck-height changes
BANK_SIGMA_M = 12.0         # remove discrete box-window steps in cross slope, not road elevation


def bank(xy, spec, abut=(False, False), ext=(0, 0)):
    """(n,) the cross slope of a road part's centreline xy (n, 2) (m per m toward its left), or None (not banked)."""
    if not BANK_ON or spec.kind not in roadsmod.BUILT or spec.cls not in BANK_DESIGN or len(xy) < 3:
        return None
    v, f = BANK_LINK if getattr(spec, "link", False) else BANK_DESIGN[spec.cls]
    r_min = v * v / (127.0 * (BANK_E_MAX + f))
    import fast
    fast.require(84, "bank_profile")
    fade = [BANK_FADE_M if (abut[k] or not ext[k]) else 0.0 for k in (0, 1)]
    b = np.asarray(fast._ef.bank_profile(np.ascontiguousarray(np.asarray(xy, np.float64)[:, :2]), float(BANK_E_MAX),
                                         float(r_min), float(BANK_STENCIL_M), float(BANK_RUNOFF_M),
                                         float(fade[0]), float(fade[1])), np.float64)
    step = float(np.median(np.linalg.norm(np.diff(np.asarray(xy)[:, :2], axis=0), axis=1)))
    if step > 0.0:
        b = smooth_profile(b, step, BANK_SIGMA_M)  # pinned ends keep bridge abutments level
    if fade[0]:
        b[0] = 0.0
    if fade[1]:
        b[-1] = 0.0
    return b if np.any(b != 0.0) else None


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
    """A side road's end at a junction takes the height of the road it joins, eased in over
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
            if p.abut[0 if end == 0 else 1]:
                continue            # a bridge abutment's end: the deck's height (dev)
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
        # crossings (GitHub #414, Perm: service roads crossing a primary at a shared node stood as 4-5 m walls across
        # it): where this road runs over a higher-ranked road's carriageway away from its own ends, it takes that
        # road's height there, eased in over JUNCTION_TAPER_M either side (with the coarse easing: BNE_COARSE_EASE=1)
        for k in ([] if not COARSE_EASE_ON else np.flatnonzero((rank > rank[j]) & (boxes[:, 0] <= p.bbox[2]) & (boxes[:, 2] >= p.bbox[0])
                                & (boxes[:, 1] <= p.bbox[3]) & (boxes[:, 3] >= p.bbox[1]))):
            q = plans[k]
            if len(q.xy) < 2:
                continue
            inner = np.flatnonzero((s > JUNCTION_END_M) & (s < length - JUNCTION_END_M))
            if not len(inner):
                continue
            a, b = q.xy[:-1, :2], q.xy[1:, :2]
            d = b - a
            ll = np.maximum((d * d).sum(1), 1e-12)
            P = p.xy[inner, :2]
            t = np.clip(((P[:, None, 0] - a[None, :, 0]) * d[None, :, 0] + (P[:, None, 1] - a[None, :, 1]) * d[None, :, 1])
                        / ll[None, :], 0.0, 1.0)
            dist = np.hypot(a[None, :, 0] + t * d[None, :, 0] - P[:, None, 0], a[None, :, 1] + t * d[None, :, 1] - P[:, None, 1])
            si = dist.argmin(1)
            dmin = dist[np.arange(len(inner)), si]
            hit = dmin <= q.road_half
            if not hit.any():
                continue
            h = int(np.argmin(np.where(hit, dmin, np.inf)))       # the crossing point (nearest the other's line)
            i_, sg = int(inner[h]), int(si[h])
            zq = float(q.z[sg] + (q.z[sg + 1] - q.z[sg]) * t[h, sg])
            delta = zq - (p.z[i_] + dz[i_])
            if abs(delta) > JUNCTION_MAX_M or abs(delta) < 0.05:
                continue
            u = np.abs(s - s[i_]) / max(min(JUNCTION_TAPER_M, 0.5 * length), 1e-6)
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


_UNSET = object()


class _Planner:
    """The road plans of one planning region (geotiles.PLAN_Z), in its frame, per map cell, made once (worker threads;
    it fetches the road cells it lacks). store: the PlanStore it belongs to (its other regions, its settings)."""

    def __init__(self, frame, mapdata, elev, widths=None, store=None, region=None):
        self.frame, self.md, self.elev, self.widths = frame, mapdata, elev, widths
        self.store, self.region = store, region
        self.cells = {}
        self.failed = {}        # cell key -> monotonic time of the last failed fetch
        self.lock = threading.Lock()
        self.key_locks = {}     # cell key -> Lock: one thread plans a cell, the others wait for it
        self.covers = {}        # cell key -> [(xy (k, 2), half width)]: the tunnels whose ground over them stays
        self.keeps = {}         # cell key -> [(xy (k, 2), half width)]: the UNGRADED roads' carriageways
        self.opens = {}         # cell key -> [(xy (k, 2), Spec)]: the tunnels' open sections (roads in the open)

    # the store's settings (set on the anchors' Graders after they are made): shared: plancache.PlanCache (the cells'
    # plans shared with the server's other processes); wet(lat, lon) -> water depth (m): tunnels under water are left
    # out, as roads.TunnelProfiles leaves them (their "open" sections graded a dam across the river: the Maastunnel's
    # bed raised from -6 m to -0.4 m, storm-ace's GitHub #85); dem: its urgent lane (_plan_missing)
    def _store_attr(name):
        def get(self):
            own = self.__dict__.get("_own_" + name, _UNSET)
            if own is not _UNSET:
                return own
            return getattr(getattr(self, "store", None), name, None)

        def put(self, v):           # (a planner of its own: tests)
            self.__dict__["_own_" + name] = v
        return property(get, put)
    shared = _store_attr("shared")
    wet = _store_attr("wet")
    dem = _store_attr("dem")
    del _store_attr

    def _foreign(self, key):
        """The planner of cell key's region when it is not this one's (None: this one plans it)."""
        if self.store is None:
            return None
        import geotiles
        r = geotiles.region_of_cell(key)
        return None if r == self.region else self.store.planner(r)

    def _import(self, src, key):
        """Cell key planned by src (another region's planner), its vertices mapped exactly into this frame."""
        plans = src._cell_plans(key)
        with src.lock:
            if key not in src.cells:            # (its fetch failed: asked again later)
                return []
            keeps, covers, opens = src.keeps.get(key, []), src.covers.get(key, []), src.opens.get(key, [])
        import geotiles

        def m(xy):
            return geotiles.world_xy_map(src.frame, self.frame, xy)
        got = ([(fid, mapped_plan(p, m)) for fid, p in plans], [(m(xy), hw) for xy, hw in keeps],
               [(m(xy), w) for xy, w in covers], [(m(xy), sp) for xy, sp in opens])
        with self.lock:
            self.cells.setdefault(key, got[0])
            self.keeps.setdefault(key, got[1])
            self.covers.setdefault(key, got[2])
            self.opens.setdefault(key, got[3])
            return self.cells[key]

    def _cell_plans(self, key):
        with self.lock:
            got = self.cells.get(key)
            kl = self.key_locks.setdefault(key, threading.Lock())
        if got is not None:
            return got
        src = self._foreign(key)
        if src is not None:
            with kl:
                return self._import(src, key)
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
            import revisit
            f0 = revisit.faults()
            got = shared if shared is not None else self._make_cell(key)
            if got is None:
                return []
            if mine:        # (kept for later visits too when complete: nothing it read failed or was missing)
                self.shared.store(key, *got, persist=revisit.faults() == f0)
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
            ext = pins = None
            if CHAIN_ON and parts:
                try:
                    self._chain_cands(key, feats, specs)        # (this cell's own: its specs are at hand)
                    ext = self._extensions(parts, ids)
                    pins = self._pins(parts, ids, ext)
                except Exception as e:      # each part smoothed alone rather than no grading
                    log.debug("grading: road chains of %s: %s: %s", key, type(e).__name__, e)
            pclip = []
            pparts = []                         # (fid, spec, lon/lat ring) of each plan (roadcorners.grade_rows)
            coarse = COARSE_EASE_ON and self._coarse(key)
            for fid, part, cl, p in zip(ids, parts, clipped, plan_roads(self.frame, parts, self.elev, abut, ext, pins,
                                                                         coarse=coarse, obs=self._obs(key))):
                if p is not None:
                    p.rid = road_id(fid)
                    plans.append((fid, p))
                    pspecs.append(part[1])
                    pclip.append(cl)
                    pparts.append((fid, part[1], part[0]))
            if JUNCTION_SNAP_ON and plans:
                try:
                    snap_junctions([p for _f, p in plans], pspecs)
                except Exception as e:      # blended as before rather than no grading
                    log.debug("grading: junctions of %s: %s: %s", key, type(e).__name__, e)
            if COARSE_EASE_ON and plans:
                try:
                    self._ease_coarse(key, plans, pspecs)
                except Exception as e:      # as they were rather than no grading
                    log.debug("grading: coarse easing of %s: %s: %s", key, type(e).__name__, e)
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
            if JUNCTION_SNAP_ON and plans and (APPROACH_ON or UNDER_ON):
                # the side roads again, at their roads' heights after the approaches' lifts and the underpasses' dips
                # (snapped before them, every alley along Baltimore's Haven Street stood up to 3.5 m over the street
                # the dip had lowered: a hump at each, the user 2026-10-28)
                try:
                    snap_junctions([p for _f, p in plans], pspecs)
                except Exception as e:
                    log.debug("grading: junctions after the dips of %s: %s: %s", key, type(e).__name__, e)
            if CORNERS_ON and plans:
                # the rounded corners of curbless junctions (roadcorners.py): the ground under each graded to its roads'
                # solved heights (trimmed to the cell's box with the roads' plans below)
                try:
                    import roadcorners
                    rings = roadcorners.ring_ids(feats)
                    crows, heights = roadcorners.grade_rows(pparts, [p for _f, p in plans], self.frame, rings)
                    corner_plans = roadcorners.grade(crows, rings, self._clip_box(key, 0.0), heights)
                    plans.extend(corner_plans)
                    pclip.extend([True] * len(corner_plans))
                except Exception as e:      # the roads' own grading rather than none
                    log.debug("grading: road corners of %s: %s: %s", key, type(e).__name__, e)
            # a long road's piece (planned with CLIP_MARGIN_M of the road either side for its profile) kept only within
            # the cell's own box: every stretch of road in one cell's plans, once (overlapping pieces from neighbouring
            # cells counted a road up to 9 times, and every batch of points graded differently: patches of ground
            # through the Stelvio's asphalt, GitHub #108's 2.5 m step at a bridge end in Meppen, 2026-10-21)
            own = self._clip_box(key, 0.0)
            sub = feats.attrs.get("subtype")
            if RAIL_GRADE_ON and own is not None and sub is not None and (np.asarray(sub).astype(str) == "rail").any():
                try:            # the railways' bed lines (railways.grade_plans): cuttings and embankments for the track
                    import railways     # (the cells around only where this one has a line: their fetches cost)
                    rcells = [self.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
                              for k in railways.cells_near(self.frame, own)]
                    for fid, xy, z in railways.grade_plans(self.frame, rcells, own, self.elev):
                        p = Plan(xy, z, railways.GRADE_HALF_M)
                        p.rid = road_id(RAIL_TAG + fid)
                        plans.append((RAIL_TAG + fid, p))
                        pclip.append(True)
                except Exception as e:      # no rail grading rather than no grading
                    log.debug("grading: railways of %s: %s: %s", key, type(e).__name__, e)
            # every plan only within the cell's own box, so each stretch of road is planned by one cell (its dips
            # made with every road around: _lower_underpasses); the tunnels' ends graded as before, a part shorter
            # than LONG_PART_M by all of it (TunnelProfiles.ends grades them by the roads' plans alone)
            tplans = plans
            if own is not None:
                # (by the cell's quad in this frame, not its box: a cell at a planning region's edge stands turned
                # ~0.16 deg in it, and boxes left slivers planned twice or not at all between neighbouring cells)
                quad = self._clip_quad(key)
                kept, tplans = [], []
                for (fid, p), cl in zip(plans, pclip):
                    pieces = trim_plan(p, own if quad is None else quad)
                    kept.extend((fid, q) for q in pieces)
                    tplans.extend((fid, q) for q in (pieces if cl else [p]))
                plans = kept
            secs = self._tunnel_sections(feats, key, [p for _f, p in tplans])
            plans.extend((fid + TUNNEL_TAG, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                         for fid, spec, xy, z, k in secs if k == 0)
            plans.extend(self._mouth_plans(secs))
            covers = [(xy, spec.width / 2 + TUNNEL_KEEP_M) for _f, spec, xy, _z, k in _twin_trimmed(secs) if k]
            opens = [(xy, spec) for _f, spec, xy, _z, k in secs if k == 0]
        else:
            covers, opens, keeps = [], [], []
        return plans, keeps, covers, opens

    def _chain_cands(self, key, feats=None, specs=None):
        """The graded ground parts of cell key's roads, as the continuations at a node in it are looked for (every cell
        that plans a part looks in its nodes' own cells, so its plan is the same in each): [(xy (k, 2) in the frame,
        smoothing sigma, road id, lon/lat ring, spec, feature id)], in the map's feature order. Kept per cell. feats, specs: the cell's roads and their
        road_specs when the caller has them."""
        memo = self.__dict__.setdefault("_chain_memo", {})
        with self.lock:
            got = memo.get(key)
        if got is not None:
            return got
        out = []
        if feats is None:
            feats = self.md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if feats is not None and len(feats):
            if specs is None:
                specs = roadsmod.road_specs(feats, None, self.widths)
            rows = []
            for i in range(len(feats)):
                if specs[i] is None or specs[i].cls in UNGRADED:
                    continue
                for ring in roadsmod.ground_parts(feats, i):
                    rows.append((i, np.asarray(ring, np.float64)))
            if rows:            # (the frame's conversion once for all: it is per call)
                allr = np.concatenate([r for _, r in rows])
                e, n, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
                en = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
                k0 = 0
                for i, ring in rows:
                    out.append((en[k0:k0 + len(ring)], profile_sigma(specs[i]), road_id(str(feats.ids[i])), ring,
                                specs[i], str(feats.ids[i])))
                    k0 += len(ring)
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[key] = out
        return out

    def _extensions(self, parts, ids):
        """plan_roads' ext of a cell's parts [(lonlat, spec)] (ids: their features'): per part (before, after), the line
        from each end outward along the part the road goes on in there (earthfast.chain_extensions), 3 sigma of its
        smoothing long, or None."""
        import fast
        fast.require(69, "road chains")
        f64 = np.float64
        q_node, q_dir, q_class, q_road, q_reach, q_cell = [], [], [], [], [], []
        if not parts:
            return []
        # (the frame's conversion once for every part: it is per call)
        alll = np.concatenate([np.asarray(ll, f64) for (ll, _sp) in parts])
        ae, an, _ = self.frame.geodetic_to_enu(alll[:, 1], alll[:, 0], 0.0)
        axy = np.stack([np.asarray(ae, f64), np.asarray(an, f64)], axis=1)
        k0 = 0
        for (lonlat, spec), fid in zip(parts, ids):
            xy = axy[k0:k0 + len(lonlat)]
            k0 += len(lonlat)
            sg = profile_sigma(spec)
            for end in (0, 1):
                line = xy if end == 0 else xy[::-1]          # from this end inward
                p = _point_along(line, CHAIN_LOOK_M)
                q_node.append(line[0])
                q_dir.append(line[0] - p)                    # the way the road goes on out of this end
                q_class.append(int(round(sg * 1000)))
                q_road.append(road_id(fid))
                reach = 3.0 * sg + PROFILE_STEP
                if BANK_ON and spec.kind in roadsmod.BUILT and spec.cls in BANK_DESIGN:
                    # Runoff averages curvature beyond the join, and that
                    # curvature needs its own stencil on either side too.
                    reach = max(reach, BANK_STENCIL_M + BANK_RUNOFF_M / 2 + 3 * BANK_SIGMA_M + PROFILE_STEP)
                q_reach.append(reach)
                q_cell.append(lonlat[0] if end == 0 else lonlat[-1])
        if q_cell:                  # (the ends' cells at once)
            qll = np.asarray(q_cell, np.float64).reshape(-1, 2)
            q_cell = mapcells.cells_of(qll[:, 1], qll[:, 0])
        out = [[None, None] for _ in parts]
        for key in sorted(set(q_cell), key=str):
            cands = self._chain_cands(key)
            qs = [i for i, c in enumerate(q_cell) if c == key]
            if not cands or not qs:
                continue
            cxy, coff, ccls, croad = self._cand_arrays(key, cands)
            pts, off = fast._ef.chain_extensions(
                cxy, coff, ccls, croad,
                np.ascontiguousarray([q_node[i] for i in qs], f64), np.ascontiguousarray([q_dir[i] for i in qs], f64),
                np.array([q_class[i] for i in qs], np.int64), np.array([q_road[i] for i in qs], np.int64),
                np.array([q_reach[i] for i in qs], f64), float(CHAIN_JOIN_M), float(CHAIN_TURN_DEG), float(CHAIN_LOOK_M))
            for j, i in enumerate(qs):
                if off[j + 1] - off[j] >= 2:
                    out[i // 2][i % 2] = np.asarray(pts[off[j]:off[j + 1]])
        return [tuple(o) for o in out]

    def _cand_arrays(self, key, cands):
        """_chain_cands(key) as the Rust kernels take them (points, offsets, sigma class, road id), made once per cell."""
        memo = self.__dict__.setdefault("_cand_arrays_memo", {})
        got = memo.get(str(key))
        if got is not None and got[0] is cands:
            return got[1]
        arr = (np.ascontiguousarray(np.concatenate([c[0] for c in cands]), np.float64),
               np.r_[0, np.cumsum([len(c[0]) for c in cands])].astype(np.int64),
               np.array([int(round(c[1] * 1000)) for c in cands], np.int64),
               np.array([c[2] for c in cands], np.int64))
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[str(key)] = (cands, arr)
        return arr

    def _pins(self, parts, ids, ext):
        """plan_roads' pins of a cell's parts: an end that no part of its own smoothing goes on from (ext None there) and
        that ends on another road's part smoothed as much or more (a slip road on a motorway, a side road on a trunk or
        on a street of its own class) is held at that road's planned height there (_stronger_z), so the two meet at one
        height (a same-class side road ended at the raw ground, the street through the junction at its smoothed height:
        0.6 m apart on a hill). The most smoothed such road, the first of equals. None when no end is held."""
        import fast
        fast.require(70, "road pins")
        f64 = np.float64
        q_node, q_cell, q_who = [], [], []
        for j, ((lonlat, spec), fid) in enumerate(zip(parts, ids)):
            for end in (0, 1):
                if ext is not None and ext[j][end] is not None:
                    continue
                ll = lonlat[-1] if end else lonlat[0]
                q_node.append((float(ll[1]), float(ll[0])))         # (lat, lon: converted below, all at once)
                q_who.append((j, end, profile_sigma(spec), road_id(fid)))
        if q_node:
            qa = np.asarray(q_node, f64)
            q_cell = mapcells.cells_of(qa[:, 0], qa[:, 1])
            qe, qn, _ = self.frame.geodetic_to_enu(qa[:, 0], qa[:, 1], 0.0)
            q_node = list(zip(np.asarray(qe, f64).tolist(), np.asarray(qn, f64).tolist()))
        out = [[None, None] for _ in parts]
        held = False
        todo = []                           # (part, end, candidate, at its last point): heights asked all at once
        for key in sorted(set(q_cell), key=str):
            cands = self._chain_cands(key)
            qs = [i for i, c in enumerate(q_cell) if c == key]
            weakest = min(q_who[i][2] for i in qs)
            strong = [c for c in cands if c[1] >= weakest]
            if not strong:
                continue
            cxy = np.concatenate([c[0] for c in strong])
            coff = np.r_[0, np.cumsum([len(c[0]) for c in strong])].astype(np.int64)
            hits, off = fast._ef.node_parts(np.ascontiguousarray(cxy, f64), coff,
                                            np.ascontiguousarray([q_node[i] for i in qs], f64), float(CHAIN_JOIN_M))
            for k, i in enumerate(qs):
                j, end, sg, rid = q_who[i]
                best = None
                for h in hits[off[k]:off[k + 1]]:
                    c = strong[int(h) // 2]
                    if c[1] >= sg and c[2] != rid and (best is None or c[1] > best[0][1]):
                        best = (c, bool(int(h) % 2))
                if best is not None:
                    todo.append((j, end, best[0], best[1]))
        for (j, end, _c, _l), z in zip(todo, self._stronger_zs([(c, last) for _j, _e, c, last in todo])):
            if z is not None:
                out[j][end] = z
                held = True
        return [tuple(o) for o in out] if held else None

    def _stronger_zs(self, asks):
        """_stronger_z of each (candidate, at_last) of asks, the ones not kept yet planned in one plan_roads call (the
        same plans: each part is planned on its own; its cost is per call, ~1 ms, 935 calls in 4 Penn Station cells,
        2026-10-25)."""
        memo = self.__dict__.setdefault("_strong_memo", {})
        keys = [self._strong_key(c, last) for c, last in asks]
        with self.lock:
            miss = [n for n, k in enumerate(keys) if k not in memo]
        jobs, seen = [], set()
        for n in miss:
            if keys[n] not in seen:
                seen.add(keys[n])
                jobs.append(n)
        if len(jobs) > 1:
            try:
                preps = [self._strong_part(*asks[n]) for n in jobs]
                parts = [(part, asks[n][0][4]) for n, (part, _last, _whole) in zip(jobs, preps)]
                ext = self._extensions(parts, [asks[n][0][5] for n in jobs])
                plans = plan_roads(self.frame, parts, self.elev, None, ext, cuts=False,
                                   coarse=COARSE_EASE_ON and self._coarse_ll(parts[0][0]),
                                   obs=self._obs(None, parts[0][0]))
                for n, (part, last, whole), p in zip(jobs, preps, plans):
                    self._strong_keep(asks[n][0], keys[n], p, last, whole)
            except Exception as e:      # one at a time, each its own failure (_stronger_z)
                log.debug("grading: pin heights at once: %s: %s", type(e).__name__, e)
        return [self._stronger_z(c, last) for c, last in asks]

    @staticmethod
    def _strong_key(cand, at_last):
        xy, rid = cand[0], cand[2]
        e = xy[-1 if at_last else 0]
        return (rid, round(float(e[0]), 1), round(float(e[1]), 1))

    def _strong_part(self, cand, at_last):
        """(the lon/lat part planned for _stronger_z, whose end is asked for: its last point?, the whole part?)"""
        xy, _sg, rid, ring, spec, fid = cand
        part = ring
        if float(np.hypot(*np.diff(xy, axis=0).T).sum()) > LONG_PART_M:
            end = xy[-1] if at_last else xy[0]
            m = CLIP_MARGIN_M
            runs = clip_polyline(xy, (end[0] - m, end[1] - m, end[0] + m, end[1] + m))
            run = min(runs, key=lambda r: min(np.hypot(*(r[0] - end)), np.hypot(*(r[-1] - end)))) if runs else None
            if run is not None:
                la, lo, _ = self.frame.enu_to_geodetic(run[:, 0], run[:, 1], 0.0)
                part = np.stack([np.asarray(lo), np.asarray(la)], axis=1)
                at_last = bool(np.hypot(*(run[-1] - end)) <= np.hypot(*(run[0] - end)))
        return part, at_last, part is ring

    def _strong_keep(self, cand, k, p, at_last, whole):
        """Keep a _stronger_z height (and the other end's, for a part planned whole)."""
        z, other = None, None
        if p is not None:
            z = float(p.z[-1] if at_last else p.z[0])
            if whole:
                xy, rid = cand[0], cand[2]
                e2 = xy[0 if at_last else -1]
                other = ((rid, round(float(e2[0]), 1), round(float(e2[1]), 1)), float(p.z[0] if at_last else p.z[-1]))
        memo = self.__dict__.setdefault("_strong_memo", {})
        with self.lock:
            if len(memo) > 4096:
                memo.clear()
            memo.setdefault(k, z)
            if other is not None:
                memo.setdefault(*other)

    def _stronger_z(self, cand, at_last):
        """The planned height of a candidate part (_chain_cands') at its first or last point: its own plan (its chains,
        no pins), within CLIP_MARGIN_M of that point when it is long. Kept per part end (a short part's plan gives both
        ends' heights: the other kept too)."""
        k = self._strong_key(cand, at_last)
        memo = self.__dict__.setdefault("_strong_memo", {})
        with self.lock:
            if k in memo:
                return memo[k]
        try:
            part, last, whole = self._strong_part(cand, at_last)
            # (the profile alone: its cutting's walls cost most of a plan and no height, 2026-10-25)
            p = plan_roads(self.frame, [(part, cand[4])], self.elev, None,
                           self._extensions([(part, cand[4])], [cand[5]]), cuts=False,
                           coarse=COARSE_EASE_ON and self._coarse_ll(part), obs=self._obs(None, part))[0]
            self._strong_keep(cand, k, p, last, whole)
        except Exception as e:      # not held rather than no grading
            log.debug("grading: pin height of %s: %s: %s", cand[5], type(e).__name__, e)
            with self.lock:
                memo[k] = None
        with self.lock:
            return memo.get(k)

    def _clip_quad(self, key):
        """The cell's quad (4, 2: its corners SW, SE, NE, NW at h = 0) in the frame, or None for a key without bounds:
        the cells' own pieces of the plans (trim_plan), each place in one cell's whatever the frame."""
        if not hasattr(key, "bounds"):
            return None
        lon0, lat0, lon1, lat1 = key.bounds()
        e, n, _ = self.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon1, lon0]),
                                             0.0)
        return np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)

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

    def _obs(self, key, lonlat=None):
        """plan_roads' obs: the measured heights of ground roads (roadz.PROVIDER, level 0) where cell key's (or the
        part's) elevation is only the coarse global model (dem.coarse_at): on lidar terrain the DTM is the better
        height (decimetres; BD TOPO's Z is 1.5-2.5 m), so None there, as when roadz is off."""
        import roadz
        prov = roadz.PROVIDER
        if prov is None or not prov.enabled:
            return None
        if not (self._coarse(key) if key is not None else self._coarse_ll(lonlat)):
            return None

        def obs(xy, spec):
            try:
                return prov.observed(self.frame, xy, spec.width / 2, 0)
            except Exception as e:      # the DEM's rather than no grading
                log.debug("grading: measured road heights: %s: %s", type(e).__name__, e)
                return None
        return obs

    def _observed_lift(self, bridge, xy, z_at):
        """(m the abutment at bridge end xy is raised, the measured deck end's height) when a source measured the deck
        (roadz.PROVIDER, level 1): the measured deck end over the ground there, 0 or more; None when none did (the
        inference: bridge_lifts)."""
        import roadz
        prov = roadz.PROVIDER
        if prov is None or not prov.enabled:
            return None
        try:
            z = prov.observed(self.frame, bridge, OBS_HALF_M, 1)
        except Exception as e:
            log.debug("grading: measured deck at %s: %s: %s", xy, type(e).__name__, e)
            return None
        if z is None:
            return None
        z = np.asarray(z, np.float64)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(bridge, axis=0).T))]
        at_end = bool(np.hypot(*(bridge[-1] - xy)) < np.hypot(*(bridge[0] - xy)))
        d = (s[-1] - s) if at_end else s
        near = np.flatnonzero(np.isfinite(z) & (d <= OBS_END_M))
        if not len(near):
            return None
        k = int(near[np.argmin(d[near])])
        return max(0.0, float(z[k]) - float(z_at(np.asarray([xy], np.float64))[0])), float(z[k])

    def _coarse(self, key):
        """Is cell key's elevation only the coarse global model (dem.coarse_at its centre)? False without a DEM."""
        dem = getattr(self, "dem", None) or getattr(self.md, "dem", None)
        if dem is None or not hasattr(dem, "coarse_at"):
            return False
        lon0, lat0, lon1, lat1 = key.bounds()
        try:
            return bool(dem.coarse_at((lat0 + lat1) / 2, (lon0 + lon1) / 2))
        except Exception:
            return False

    def _coarse_ll(self, lonlat):
        """dem.coarse_at the middle of a part's (lon, lat) points (the pins' and chains' own plans sample as the cell's)."""
        dem = getattr(self, "dem", None) or getattr(self.md, "dem", None)
        if dem is None or not hasattr(dem, "coarse_at") or not len(lonlat):
            return False
        lon, lat = np.asarray(lonlat, np.float64)[len(lonlat) // 2][:2]
        try:
            return bool(dem.coarse_at(float(lat), float(lon)))
        except Exception:
            return False

    def _ease_coarse(self, key, plans, pspecs):
        """COARSE_* above: the cell's plans eased together where its elevation is coarse (in place)."""
        if not self._coarse(key):
            return
        ps = [p for _f, p in plans]
        if len(ps) < 2:
            return
        import fast
        fast.require(94, "ease_coarse")
        off = np.r_[0, np.cumsum([len(p.xy) for p in ps])].astype(np.int64)
        xy = np.ascontiguousarray(np.concatenate([np.asarray(p.xy, np.float64)[:, :2] for p in ps]))
        z = np.ascontiguousarray(np.concatenate([np.asarray(p.z, np.float64) for p in ps]))
        rank = np.array([road_rank(sp) for sp in pspecs], np.int64)
        half = np.array([p.road_half for p in ps], np.float64)
        take = np.array([0 if (p.abut[0] or p.abut[1]) else 1 for p in ps], np.uint8)
        out = fast._ef.ease_coarse(xy, z, off, rank, half, take, COARSE_REACH_M, COARSE_MAX_STEP_M, COARSE_MAX_SLOPE,
                                   COARSE_SIGMA_M)
        for j, p in enumerate(ps):
            p.z = np.asarray(out[off[j]:off[j + 1]], np.float64)

    def _lift_approaches(self, key, plans, specs):
        """Raise, in place, the plans of the roads leading onto overpasses (APPROACH_ON). A road is raised where one of
        its ends is a bridge's abutment that must go up for the deck to clear the road under it (_end_lift); the raise
        falls off along the road (approach_add) and never comes nearer another road than its fill slope allows. Only
        what the plan's own line and the map around its ends say counts, the same in every cell that plans it (a long
        road is planned in each cell it passes; two cells raising the same road differently drew the road under a
        raised ground, 2026-10-20 I-20 in Greene County, AL). Major roads (APPROACH_SKIP) are never raised."""
        # every plan end at once: near the cell, and on a bridge end of its own cell (most touch none)
        pe = np.array([[p.xy[0][:2], p.xy[-1][:2]] for _f, p in plans], np.float64).reshape(-1, 2)
        ok = np.repeat([s is not None for s in specs], 2)       # (a motorway's or trunk's: over water only, below)
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
                if _never_raised(specs[i]) and not got[3]:
                    continue
                lift, others = got[0], got[1]
                if len(got) > 4 and got[4] is not None:
                    # (a measured deck end: up to its height from the plan's own there, not from the ground under the
                    # point, which may lie down the cut the deck spans: Charles Street's approach over I-83 rose 2.2 m
                    # over its plan to a deck end measured at the street's height, 2026-10-29)
                    zp = float(np.asarray(p.z, np.float64)[-1 if at_end else 0])
                    lift = min(float(got[4]) - zp, APPROACH_MAX_LIFT)
                    if lift < APPROACH_MIN_LIFT:
                        continue
                a = approach_add(p.xy, at_end, lift, p.half, others)
                if a is not None:
                    add = a if add is None else np.maximum(add, a)
            if add is not None:
                np_ = Plan(p.xy, p.z + add, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
                plans[i] = (fid, np_)

    def _lower_underpasses(self, key, plans):
        """Lower, in place, the plans that cross under a short never-raised overpass (UNDER_*), and the roads joined
        to them (underpass_offsets), only within the cell's box (each plan is trimmed to it after). Solved on every
        ground road and span within UNDER_REACH_M of the box, from whichever cells they lie in, each road whole, its
        depth from its ground (_under_ground) rather than its plan: every cell gets the same dips. (On its own cell's roads alone, a road ending at a junction whose other roads
        lay only in the next cell was pinned there by one cell and lowered 3.5 m by the other; their copies met as a
        step on US-101 by Los Angeles' Union Station, 2026-10-27.)"""
        f64 = np.float64
        box = self._clip_box(key, UNDER_NEAR_M)
        if box is None:
            return
        xys = [np.asarray(p.xy, f64)[:, :2] for _fid, p in plans]
        lo = np.array(box[:2]) - UNDER_REACH_M
        hi = np.array(box[2:]) + UNDER_REACH_M
        # (the spans' cells a span's length further: one joined to another past the box is no single span)
        pad = UNDER_MAX_BRIDGE_M + 10.0
        cla, clo, _ = self.frame.enu_to_geodetic(np.array([lo[0] - pad, hi[0] + pad] * 2),
                                                 np.array([lo[1] - pad] * 2 + [hi[1] + pad] * 2), 0.0)
        cla, clo = np.asarray(cla, f64), np.asarray(clo, f64)
        keys = sorted(set(mapcells.cells_for_bbox(float(cla.min()), float(clo.min()), float(cla.max()),
                                                  float(clo.max()))) | {key}, key=str)
        bridges, ground = [], {}
        for k in keys:
            c = self._cell_enu(k)
            for line, cls in zip(c["bridges"], c.get("bridge_cls", [None] * len(c["bridges"]))):
                if cls is None:
                    continue
                if not (UNDER_ANY_ON or cls in APPROACH_SKIP or cls.endswith("_link")):
                    continue
                bridges.append((line, cls in APPROACH_SKIP, False))     # (a link's "<cls>_link": raised)
            # (a line's deck: never raised; straight between its ends whatever its length or what it joins, as
            # railways.deck_heights lays it: no single-span test)
            bridges += [(line, True, True) for line in c.get("rail_bridges", ())]
            for (line, _h, cls), (fid, j, sg, med) in zip(c["ground"], c["ground_ids"]):
                if cls in UNGRADED or len(line) < 2:
                    continue
                if (line.max(0) < lo).any() or (line.min(0) > hi).any():
                    continue
                got = ground.get((fid, j))      # (the longest copy of a part, as each cell has it)
                if got is None or len(line) > len(got[0]):
                    ground[(fid, j)] = (line, sg, med)
        if not bridges or not ground:
            return
        # (single spans: neither end continues into another bridge part; deduplicated across cells)
        uniq, seen = [], set()
        for b, fixed, rail in bridges:
            k2 = (round(float(b[0, 0]), 1), round(float(b[0, 1]), 1), round(float(b[-1, 0]), 1), len(b))
            if k2 not in seen:
                seen.add(k2)
                uniq.append((b, fixed, rail))
        spans = []
        ue = np.array([[b[0, :2], b[-1, :2]] for b, _f, _r in uniq], f64)        # (n, 2 ends, 2)
        coarse = self._coarse(key)      # (there the railway rises over the road: railways.coarse_underpasses)
        for n, (b, fixed, rail) in enumerate(uniq):
            if rail and coarse:
                continue
            L = float(np.hypot(*np.diff(b, axis=0).T).sum())
            if L < 2 * APPROACH_MIN_CROSS_M or (L > UNDER_MAX_BRIDGE_M and not rail):
                continue
            d = np.hypot(ue[:, :, None, 0] - ue[n, None, None, :, 0], ue[:, :, None, 1] - ue[n, None, None, :, 1])
            d[n] = np.inf                       # (another part's end within 0.75 m of either of its ends: joined)
            if rail or not (d < 0.75).any():
                spans.append((b, L, fixed))
        if not spans:
            return
        gids = sorted(ground)
        lines = [ground[g][0] for g in gids]
        crs = roadsmod._crossings_by([b for b, _L, _f in spans], lines)
        ez, raised = {}, set()

        def deck_ends(j):           # (an overpass's abutments' ground, raised when it is: as high as its ramps could
            got = ez.get(j)         # take them; asked only for one a road crosses under: every span's took 2 s a
            if got is None:         # Canton cell, 2026-10-25)
                b, _L, fixed = spans[j]
                ends = np.array([b[0], b[-1]], f64)
                ela, elo, _ = self.frame.enu_to_geodetic(ends[:, 0], ends[:, 1], 0.0)
                got = np.asarray(self.elev(np.asarray(ela), np.asarray(elo)), f64).reshape(2).copy()
                if not fixed:
                    got[0] += self._end_raised(b[0])
                    got[1] += self._end_raised(b[-1])
                ez[j] = got
            return got
        cands = []
        for i, cr in enumerate(crs):
            if not cr:
                continue
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            ab = np.diff(xy, axis=0)
            for c in cr:
                bi, arc_b, pt = c[0], c[1], np.asarray(c[2], f64)
                b, L, fixed = spans[bi]
                if arc_b < APPROACH_MIN_CROSS_M or arc_b > L - APPROACH_MIN_CROSS_M:
                    continue                    # a junction at the bridge's end
                ze = deck_ends(bi)
                corridor = np.clip([arc_b - roadsmod.DECK_CLEAR_REACH_M, arc_b + roadsmod.DECK_CLEAR_REACH_M], 0.0, L)
                deck = float(np.min(ze[0] + (ze[1] - ze[0]) * corridor / L))
                # where along this road (its nearest vertex segment)
                t = np.clip(((pt - xy[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
                j = int(np.argmin(np.linalg.norm(xy[:-1] + t[:, None] * ab - pt, axis=1)))
                # (the deck's own clearance: its depth scales with its span, roads.deck_depth; the full-depth
                # UNDER_CLEAR_M dug the road out under a short overpass that sits lower)
                clear = roadsmod.deck_road_clear(roadsmod.deck_depth(roadsmod.geom_span([b]))) + ROAD_LIFT
                cands.append((i, float(s[j] + t[j] * (s[j + 1] - s[j])), deck - clear))
        if not cands:
            return
        zc = self._under_ground(lines, [ground[g][1:] for g in gids], cands)
        requirements = []
        for (i, sc, top), z in zip(cands, zc):
            target = max(top, z - UNDER_MAX_DROP_M)
            if target < z - 0.1:
                requirements.append((i, sc, z - target))
        if not requirements:
            return
        # each plan on its road's line (of its feature's parts, the nearest)
        by_fid = {}
        for n, (fid, _j) in enumerate(gids):
            by_fid.setdefault(fid, []).append(n)
        queries, which = [], []
        inside = []
        for n, (fid, p) in enumerate(plans):
            cand = by_fid.get(fid)
            xy = xys[n]
            near = (xy[:, 0] >= box[0]) & (xy[:, 1] >= box[1]) & (xy[:, 0] <= box[2]) & (xy[:, 1] <= box[3])
            if not cand or not near.any():
                continue
            mid = xy[near][int(near.sum()) // 2]
            best = min(cand, key=lambda m: float(np.min(np.hypot(*(lines[m] - mid).T))))
            queries.append((best, xy[near]))
            which.append(n)
            inside.append(near)
        if not queries:
            return
        for n, near, got in zip(which, inside, underpass_offsets(lines, requirements, queries)):
            if not np.any(got < -1e-8):
                continue
            fid, p = plans[n]
            offset = np.zeros(len(near))        # (past the box: the plan's piece there is another cell's)
            offset[near] = got
            np_ = Plan(p.xy, np.asarray(p.z) + offset, p.half, p.abut, p.cut, p.road_half)
            np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
            plans[n] = (fid, np_)

    def _under_ground(self, lines, smooth, cands):
        """The heights of ground roads where they cross under a bridge, cands [(line, arc m, _)]: their ground along
        them, median-filtered and smoothed as their plans are (_plan: smooth (sigma, median width) per line), at the
        crossing. A pure function of the road and the ground (its plan's own height is only in the cells planning it).
        """
        f64 = np.float64
        pts, spans = [], []
        for i, sc, _d in cands:
            sg, med = smooth[i]
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            k = int(math.ceil(3.0 * sg / PROFILE_STEP))
            a = np.clip(sc + PROFILE_STEP * np.arange(-k, k + 1), 0.0, s[-1])
            pts.append(np.stack([np.interp(a, s, xy[:, 0]), np.interp(a, s, xy[:, 1])], axis=1))
            spans.append((k, sg, med))
        allp = np.concatenate(pts)
        la, lo, _ = self.frame.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
        z = np.asarray(self.elev(np.asarray(la, f64), np.asarray(lo, f64)), f64).ravel()
        out, k0 = [], 0
        for k, sg, med in spans:
            zz = z[k0:k0 + 2 * k + 1]
            k0 += 2 * k + 1
            out.append(float(smooth_profile(median_profile(zz, PROFILE_STEP, med), PROFILE_STEP, sg)[k]))
        return out

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
        tj = np.flatnonzero(touch)
        for j, home in zip(tj.tolist(), mapcells.cells_of(la[tj], lo[tj])):
            homes.setdefault(home, []).append(j)
        for home, js in homes.items():
            ends = self._cell_enu(home, ground=False)["ends"]
            js = np.asarray(js)
            if not len(ends):
                touch[js] = False
                continue
            d = np.hypot(pe[js, None, 0] - ends[None, :, 0], pe[js, None, 1] - ends[None, :, 1])
            touch[js] = (d <= APPROACH_TOUCH_M).any(axis=1)
        return touch

    def _end_raised(self, xy):
        """m the abutment at bridge end xy is raised by its approach ramps (_lift_approaches: the most any road leading
        onto it rises there, its fill slope kept clear of the roads beside), 0 when it is not raised."""
        got = self._end_lift(xy)
        if got is None:
            return 0.0
        lift, others, approaches = got[:3]
        best = 0.0
        for line, half in approaches:
            at_end = bool(np.hypot(*(line[-1] - xy)) <= np.hypot(*(line[0] - xy)))
            a = approach_add(line, at_end, lift, half, others)
            if a is not None:
                best = max(best, float(a[-1] if at_end else a[0]))
        return best

    def _end_lift(self, xy):
        """(lift m, other road lines [(xy (k, 2), half width)], the roads leading onto it [(xy, half width)]) when the
        road end xy is a bridge abutment to raise, else None. A pure function of the map around the bridge (its cells,
        fetched as needed), cached per point."""
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
        points, "end_of": [bridge index per end], "ground": [(line, half width, cls)], "ground_ids":
        [(feature id, part index, profile sigma, median width)] per ground part} (the longest copy of each part).
        With ground False, only the bridges (most plan ends touch none: their cells' ground roads are not needed).
        Kept for the ENU_CACHE_MAX cells used last; a cell another thread is making is waited for (cleared whole at
        128 cells, while the planning threads made the same cells at once, a 36-tile downtown Los Angeles set made
        318 cells for 242, ~125 ms each, 2026-10-27)."""
        key = (str(k), ground)
        with self.lock:
            if not hasattr(self, "enu_cache"):
                self.enu_cache, self.enu_making = OrderedDict(), {}
            while True:
                got = self.enu_cache.get(key) or (self.enu_cache.get((str(k), True)) if not ground else None)
                if got is not None:
                    self.enu_cache.move_to_end(key if key in self.enu_cache else (str(k), True))
                    return got
                ev = self.enu_making.get(key)
                if ev is None:
                    ev = self.enu_making[key] = threading.Event()
                    break
                self.lock.release()
                try:
                    ev.wait(60.0)
                finally:
                    self.lock.acquire()
                if key not in self.enu_cache and self.enu_making.get(key) is ev:    # (its maker gave up: make it here)
                    break
        try:
            out = self._cell_enu_calc(k, ground)
            with self.lock:
                self.enu_cache[key] = out
                self.enu_cache.move_to_end(key)
                while len(self.enu_cache) > ENU_CACHE_MAX:
                    self.enu_cache.popitem(last=False)
            return out
        finally:
            with self.lock:
                if self.enu_making.get(key) is ev:
                    del self.enu_making[key]
            ev.set()

    def _cell_enu_calc(self, k, ground):
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
        # the rail bridges (railways.DRAWN lines' bridge pieces): the roads under them dip as under a road overpass
        # (_lower_underpasses), nothing else (no ramps, no abutments: a line's deck is never raised for a road).
        # (GitHub #311 / #345 / #364: the track lay across the road under a rail bridge)
        rail, rail_g = [], []
        if f is not None and len(f) and f.attrs.get("subtype") is not None:
            import railways
            sub, rcls = f.attrs["subtype"], f.attrs.get("class")
            for i in range(len(f)):
                if str(sub[i]) != "rail" or railways.drawn(f, i) is None:
                    continue
                for kind, rings in railways._pieces(f, i):
                    if kind == "bridge":
                        rail += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
                    elif kind == "ground" and ground:
                        # (and the tracks on the ground: a road bridge's approaches rise over them as over a road,
                        # GitHub #324)
                        rail_g += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
        bridges, ground_l, ends, end_of, bcls, ground_ids = [], [], [], [], [], []
        items = sorted(parts.items())
        if items:               # (the frame's conversion once for every part: it is per call)
            allr = np.concatenate([ring for _k, (ring, _sp) in items])
            ae, an, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
            axy = np.stack([np.asarray(ae, np.float64), np.asarray(an, np.float64)], axis=1)
        k0 = 0
        for (fid, kind, j), (ring, sp) in items:
            line = axy[k0:k0 + len(ring)]
            k0 += len(ring)
            if kind == "bridge":
                end_of += [len(bridges), len(bridges)]
                ends += [line[0], line[-1]]
                bridges.append(line)
                cls = getattr(sp, "cls", None)
                bcls.append(cls + "_link" if cls is not None and _is_link(sp) else cls)
            else:
                ground_l.append((line, sp.width / 2 + shoulder(sp), sp.cls))
                ground_ids.append((fid, j, profile_sigma(sp), MEDIAN_M if sp.kind in roadsmod.BUILT else DIRT_MEDIAN_M))
        rail_xy, rail_gxy = [], []
        if rail or rail_g:
            allr = np.concatenate(rail + rail_g)
            re_, rn, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
            rxy = np.stack([np.asarray(re_, np.float64), np.asarray(rn, np.float64)], axis=1)
            k0 = 0
            for ring, out_ in [(r, rail_xy) for r in rail] + [(r, rail_gxy) for r in rail_g]:
                out_.append(rxy[k0:k0 + len(ring)])
                k0 += len(ring)
        return {"bridges": bridges, "bridge_cls": bcls, "ground": ground_l, "ground_ids": ground_ids, "end_of": end_of,
                "ends": np.asarray(ends, np.float64).reshape(-1, 2), "rail_bridges": rail_xy, "rail_ground": rail_gxy}

    def _ground_table(self, keys, cs):
        """The ground road parts of the cells keys (cs: their _cell_enu), each once (the first copy, by its first point
        and length), in their order: (lines, half widths, boxes' lo (n, 2), hi (n, 2), first points, last points).
        Kept per cell set (every bridge end near them asks: a loop over each part per end was most of
        _lift_approaches, 2026-10-25)."""
        mk = tuple(str(k) for k in keys)
        memo = self.__dict__.setdefault("_ground_tables", {})
        with self.lock:
            got = memo.get(mk)
        if got is not None and all(a is b for a, b in zip(got[0], cs)):
            return got[1]
        lines, halves, seen = [], [], set()
        for c in cs:
            for line, half, _cls in c["ground"]:
                key = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), len(line))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(line)
                halves.append(half)
        f64 = np.float64
        if lines:
            tab = (lines, halves, np.array([ln.min(0) for ln in lines], f64), np.array([ln.max(0) for ln in lines], f64),
                   np.array([ln[0] for ln in lines], f64), np.array([ln[-1] for ln in lines], f64))
        else:
            z = np.zeros((0, 2), f64)
            tab = (lines, halves, z, z, z, z)
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[mk] = (list(cs), tab)
        return tab

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
        ground, others, approaches = [], [], []
        chain = bridge_chain(bridge, bridges)
        spans = [bridge] + chain        # (a road under any part of the chain is spanned, not beside the ramp: #138)
        blo = np.min([b.min(0) for b in spans], axis=0)
        bhi = np.max([b.max(0) for b in spans], axis=0)
        lines, halves, lo_l, hi_l, first, last = self._ground_table(keys, cs)
        near = np.flatnonzero(~((hi_l < lo_b).any(1) | (lo_l > hi_b).any(1)))   # (the rest nowhere near the bridge)
        tch = np.minimum(np.hypot(first[near, 0] - xy[0], first[near, 1] - xy[1]),
                         np.hypot(last[near, 0] - xy[0], last[near, 1] - xy[1])) <= APPROACH_TOUCH_M
        box = ~((hi_l[near] < blo).any(1) | (lo_l[near] > bhi).any(1))
        inbox = np.flatnonzero(box)     # (which of them cross under the chain: one kernel call for all)
        crossing = np.zeros(len(near), bool)
        for n, cr in zip(inbox, roadsmod._crossings_by(spans, [lines[near[n]] for n in inbox])):
            crossing[n] = bool(cr)
        for n, i in enumerate(near):
            line, half = lines[i], halves[i]
            ground.append(line)
            touches = bool(tch[n])
            under = bool(crossing[n])
            if touches:
                approaches.append((line, half))
            # the roads beside the ramp, which its fill slope must not reach (not the ones the bridge spans: its
            # abutment stands between)
            if not touches and not under:
                others.append((line, half))

        # (the tracks near it: crossed under like a road, never an approach or a road beside the ramp)
        rails = [line for c in cs for line in c.get("rail_ground", ())
                 if not ((line.max(0) < lo_b).any() or (line.min(0) > hi_b).any())]

        def z_at(p):
            la_, lo_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(la_, lo_), np.float64)
        best = None
        for end, lift in bridge_lifts(bridge, bridges, ground, z_at, chain, rails):
            if np.hypot(*(end - xy)) <= APPROACH_TOUCH_M:
                best = lift
                break
        seen = self._observed_lift(bridge, xy, z_at)
        if seen is not None:            # (a measured deck: its own end, not what the clearance rules would need)
            seen, target = seen
            return None if seen < APPROACH_MIN_LIFT else (min(seen, APPROACH_MAX_LIFT), others, approaches, False,
                                                          target)
        wl = self._water_lift(bridge, chain, xy, z_at) if bridge_chain_ends_free(bridge, bridges, xy) else 0.0
        if wl >= APPROACH_MIN_LIFT and (best is None or wl > best):
            return min(wl, WATER_MAX_LIFT), others, approaches, True
        return None if best is None else (best, others, approaches, False)

    def _water_lift(self, bridge, chain, xy, z_at):
        """m the abutment at xy of a bridge over water must be raised for its deck to reach the water's clearance
        (roads.BridgeProfiles._water, the deck's own depth) at DECK_DESIGN_GRADE after WATER_CURVE_M of vertical curve,
        0 when it need not. A causeway's 100 m channel bridge over 3.8 m of navigation clearance, its ends 0.7 m short
        of it 12 m out, humped 1.8 m over a curve squeezed under its grade cap (the Sunshine Skyway, 2026-10-27)."""
        if self.wet is None or not WATER_LIFT_ON:
            return 0.0
        try:
            import bridgetypes
            ch = self._chains_for()
            lines = [roadsmod.densify(np.asarray(c, np.float64)[:, :2], roadsmod.DECK_STEP)       # (as the deck's)
                     for c in [bridge] + list(chain or ())]
            try:
                nav = bridgetypes.nav_clearance(self.frame, lines)
            except Exception:
                nav = 0.0
            req = ch._water(lines, nav, ch._depth(lines))
            if not req:
                return 0.0
            pts, need = [], []
            for k, arc, base, c in req:
                ln = lines[k]
                s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ln, axis=0).T))]
                pts.append([np.interp(arc, s, ln[:, 0]), np.interp(arc, s, ln[:, 1])])
                need.append(base + c)
            d = np.hypot(*(np.asarray(pts) - np.asarray(xy)[:2]).T)
            reach = np.asarray(need) - roadsmod.DECK_DESIGN_GRADE * np.maximum(d - roadsmod.DECK_ROAD_HALF - WATER_CURVE_M, 0.0)
            return max(0.0, float(reach.max() - z_at(np.asarray(xy, np.float64)[None, :2])[0]))
        except Exception as e:      # no fill rather than no grading
            log.debug("grading: water lift: %s: %s", type(e).__name__, e)
            return 0.0

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

    def _drowned(self, key, ring):
        """roads.TunnelProfiles.drowned for tunnel part key (ring: its lon / lat points): the process's tunnel chain
        store's (the road tiles' and blocks', blockworker: store.tunnel_store), whose chains are resolved once for all
        of them, when it is there; else this frame's own (_chains_for)."""
        get = getattr(self.store, "tunnel_store", None) if getattr(self, "store", None) is not None else None
        cs = get() if callable(get) else None
        if cs is not None:
            try:
                st = cs.store_at(float(ring[0][1]), float(ring[0][0]))
                if st.wet is not None:
                    return st.drowned(key, ring)
            except Exception as e:      # (this frame's own then)
                log.debug("grading: the chain store's drowned: %s: %s", type(e).__name__, e)
        return self._chains_for().drowned(key, ring)

    def _tunnel_sections(self, feats, key=None, road_plans=()):
        """[(fid, spec, xy, z, kind)] of the tunnels' sections (roads.tunnel_sections on the ungraded ground, its
        ends graded by road_plans: the cell's road Plans, as roads.TunnelProfiles.ends grades them with the roads'
        plans alone): kind 0 an open road at z, 1 a low arch, 2 a full tunnel."""
        out = []
        ts = roadsmod.tunnel_specs(feats)
        pack = None                     # (the road plans packed once: per tunnel part, ~6 ms each, 0.9 s of 8 tiles)
        over = None                     # (the ground roads near the tunnels, as roads.TunnelProfiles covers them)
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
                if self.wet is not None and self._drowned((str(feats.ids[i]), j), ring):
                    continue            # (a dry piece of an underwater tunnel: left out with it, GitHub #128)
                g = np.asarray(self.elev(la, lo), np.float64)
                if road_plans:
                    g = g.copy()
                    pack = PlanPack(list(road_plans)) if pack is None else pack
                    g[[0, -1]] = grade_points(xy[[0, -1], 0], xy[[0, -1], 1], g[[0, -1]], pack)
                floor, kinds = roadsmod.tunnel_sections(s, g)
                kinds = roadsmod.portal_cover(kinds, g - floor, ts[i])
                if over is None:
                    over = self._roads_over_tunnels(feats, ts)
                kinds = roadsmod.cover_roads(xy, s, kinds, *over)
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

    def _roads_over_tunnels(self, feats, ts):
        """([ground lines (k, 2) in the frame], [carriageway half widths]) of the roads near feats' tunnels (ts:
        tunnel_specs), as roads.TunnelProfiles._cover_roads has them."""
        import mapgeom
        tun = [i for i in range(len(feats)) if ts[i] is not None]
        boxes = roadsmod._feature_boxes(feats)
        if not tun:
            return [], []
        pad = (roadsmod.TUNNEL_ROAD_REACH_M + 40.0) / 111320.0      # (lon/lat degrees, generous at high latitudes)
        tb = boxes[tun]
        rs = roadsmod.road_specs(feats)
        lines, halves = [], []
        for i in range(len(feats)):
            if rs[i] is None:
                continue
            b = boxes[i]
            k = 1.0 / max(math.cos(math.radians(0.5 * (b[1] + b[3]))), 0.05)
            if not ((tb[:, 0] - pad * k <= b[2]) & (tb[:, 2] + pad * k >= b[0]) & (tb[:, 1] - pad <= b[3]) &
                    (tb[:, 3] + pad >= b[1])).any():
                continue
            for ring in roadsmod.ground_parts(feats, i):
                e, n = mapgeom.enu_xy(feats, self.frame, ring)
                lines.append(np.stack([e, n], axis=1))
                halves.append(rs[i].width / 2)
        return lines, halves

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
        src = self._foreign(key)
        if src is not None:
            return src.planned(key)
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
        return self._plan_set(self._keys(lat, lon), roads_only)[0]

    @staticmethod
    def _keys(lat, lon):
        """The map cells whose plans can reach the points."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        pad = (T_MAX + 40.0) / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lat)))))
        keys = mapcells.cells_for_points(lat, lon, pad, padlon)
        if len(keys) > SPREAD_CELLS:        # points far apart: only the cells around them
            keys = mapcells.cells_near_points(lat, lon, pad, padlon)
        return keys

    def _plan_set(self, keys, roads_only):
        """(plans, PlanPack of them) of the cells keys: each road once, in the order of their keys. Remembered once all
        the cells are planned (a road tile asks for the same few cells' plans dozens of times: merging them cost a
        Canton tile set ~1.6 s, 2026-10-25)."""
        self._plan_missing(keys)
        mk = (tuple(sorted(str(k) for k in keys)), bool(roads_only))
        memo = self.__dict__.setdefault("_plan_sets", {})
        with self.lock:
            hit = memo.get(mk)
        if hit is not None:
            return hit
        got = {}
        for key in keys:
            for fid_part, p in self._cell_plans(key):
                if roads_only and TUNNEL_TAG in fid_part:
                    continue
                got.setdefault((fid_part, float(p.xy[0, 0]), float(p.xy[0, 1])), p)
        # in the order of their keys: grade_points' sums run in plan order, so every caller (the block, a road
        # tile, a far tile) gets the same heights at a point whatever other cells its points reached
        plans = [got[k] for k in sorted(got)]
        out = (plans, PlanPack(plans))
        with self.lock:
            if all(k in self.cells for k in keys):      # (not while a cell's fetch failed: it is asked again)
                if len(memo) >= PLAN_SETS_MAX:
                    memo.pop(next(iter(memo)))
                memo[mk] = out
        return out

    def apply(self, lat, lon, base, lift=0.0, roads_only=False, planned_only=False):
        """base (the ungraded heights at the points) graded under the roads. Same shape as base.
        lift: m added under the roads (tapering with the grading's weight): the physics block and the
        road mesh use ROAD_LIFT, the visible ground tiles 0, so the road mesh lies exactly on the
        ground the tyres touch while the visible ground stays a little below it (no z-fighting;
        2026-10-11 the user saw the tyres sink into a road mesh lifted above the physics ground).
        planned_only: graded only under the cells planned already (none planned here: the AI graph's wide window, whose
        planning held a Windows PC's road processes for minutes, 2026-10-25)."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        base = np.asarray(base, np.float64)
        shape = base.shape
        if base.size == 0:
            return base
        keys = self._keys(lat, lon)
        if planned_only:
            with self.lock:
                have = {k for k in keys if k in self.cells}
            for k in keys:              # (a neighbouring region's, planned there: read in)
                src = None if k in have else self._foreign(k)
                if src is not None:
                    with src.lock:
                        if k in src.cells:
                            have.add(k)
            keys = [k for k in keys if k in have]
            if not keys:
                return base
        plans, pack = self._plan_set(keys, roads_only)
        if not plans:
            return base
        e, n, _ = self.frame.geodetic_to_enu(lat.ravel(), lon.ravel(), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        out = grade_points(e, n, base.ravel(), pack, lift)
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


def mapped_plan(p, fn):
    """Plan p with its vertices through fn (xy (n, 2) -> (n, 2): geotiles.world_xy_map); heights, widths, cuttings and
    banking as they are (geodetic, or along / across the road)."""
    q = Plan(fn(p.xy), p.z, p.half, p.abut, p.cut, p.road_half)
    q.rid, q.max_wall, q.bank = p.rid, p.max_wall, getattr(p, "bank", None)
    return q


class PlanStore:
    """Every road plan of a process, whatever the anchor: one _Planner per planning region (geotiles.PLAN_Z), made on
    first use. mapdata, elev (lat, lon -> m at PROFILE_ZOOM, raw), widths as the Grader's; wet, dem, shared (the
    planners' settings) set after it is made, as on a Grader (which forwards them here)."""

    def __init__(self, mapdata, elev, widths=None):
        self.md, self.elev, self.widths = mapdata, elev, widths
        self.wet = None
        self.dem = None
        self.shared = None
        self.lock = threading.Lock()
        self.planners = OrderedDict()   # (rx, ry) -> _Planner, the least recently used first

    @staticmethod
    def frame_of(region):
        """The planning frame of a region (geotiles.region_frame; a test replaying samples captured in one frame
        sets its own)."""
        import geotiles
        return geotiles.region_frame(*region)

    def planner(self, region):
        region = (int(region[0]), int(region[1]))
        with self.lock:
            got = self.planners.get(region)
            if got is None:
                got = self.planners[region] = _Planner(self.frame_of(region), self.md, self.elev, self.widths,
                                                       store=self, region=region)
                # (the regions far behind go: their plans are on disk, plancache; a drive across a continent kept
                # every cell it passed)
                while len(self.planners) > PLANNERS_KEPT:
                    self.planners.popitem(last=False)
            else:
                self.planners.move_to_end(region)
            return got

    def planner_of_cell(self, key):
        import geotiles
        return self.planner(geotiles.region_of_cell(key))

    def planners_of(self, lat, lon):
        """[(planner, index array)] of the points' regions (most calls: one)."""
        import geotiles
        lat = np.asarray(lat, np.float64).ravel()
        lon = np.asarray(lon, np.float64).ravel()
        rx, ry = geotiles.region_of(lat, lon)
        rx, ry = np.atleast_1d(rx), np.atleast_1d(ry)
        if len(rx) and (rx == rx[0]).all() and (ry == ry[0]).all():
            return [(self.planner((int(rx[0]), int(ry[0]))), None)]
        out = []
        for a, b in sorted({(int(a), int(b)) for a, b in zip(rx, ry)}):
            out.append((self.planner((a, b)), np.flatnonzero((rx == a) & (ry == b))))
        return out


PLANNERS_KEPT = 6               # planning regions a PlanStore keeps in memory (~30 km each)
MAPPED_MAX = 256                # cells' plans mapped into an anchor's world kept (Grader._cell_plans)


class Grader:
    """The road plans as one anchor frame sees them (the PlanStore's, store: shared by the process's anchors; its own
    without one). Heights (apply) are the planners', in their regions' frames: the same at a place whatever the
    anchor. Plans and outlines handed out (plans, _cell_plans) are in this frame's world."""

    def __init__(self, frame, mapdata, elev, widths=None, store=None):
        self.frame = frame
        self.store = store if store is not None else PlanStore(mapdata, elev, widths)
        self.md, self.elev, self.widths = self.store.md, self.store.elev, self.store.widths
        self.lock = threading.Lock()
        self._mapped = OrderedDict()        # cell key -> its plans in this frame

    def _set(name):
        return property(lambda self: getattr(self.store, name), lambda self, v: setattr(self.store, name, v))
    wet = _set("wet")
    dem = _set("dem")
    shared = _set("shared")
    del _set

    def planned(self, key):
        """True once cell key's plans exist (made, or failed recently), by any anchor of the store."""
        return self.store.planner_of_cell(key).planned(key)

    def warm(self, key):
        """Make cell key's plans now (worker threads: the lookahead ahead of the player)."""
        self.store.planner_of_cell(key)._cell_plans(key)

    def _cell_plans(self, key):
        """[(fid, Plan)] of cell key, in this frame's world (mapped from its region's frame; kept)."""
        with self.lock:
            got = self._mapped.get(key)
            if got is not None:
                self._mapped.move_to_end(key)
                return got
        pl = self.store.planner_of_cell(key)
        plans = pl._cell_plans(key)
        with pl.lock:
            done = key in pl.cells
        import geotiles

        def m(xy):
            return geotiles.world_xy_map(pl.frame, self.frame, xy)
        got = [(fid, mapped_plan(p, m)) for fid, p in plans]
        if done:
            with self.lock:
                got = self._mapped.setdefault(key, got)
                while len(self._mapped) > MAPPED_MAX:
                    self._mapped.popitem(last=False)
        return got

    _clip_box = _Planner._clip_box          # (in this frame: self.frame only)
    _keys = staticmethod(_Planner._keys)
    _over_tunnels = _Planner._over_tunnels  # (a namespace with covers and lock: tests)
    _on_kept = _Planner._on_kept

    def plans(self, lat, lon, roads_only=False):
        """The Plans touching the points' area (each road once), in this frame's world; roads_only: not the tunnels'
        (TUNNEL_TAG). In the order of the plans' own region (as _Planner._plan_set)."""
        keys = self._keys(lat, lon)
        got = {}
        for key in keys:
            for fid_part, p in self._cell_plans(key):
                if roads_only and TUNNEL_TAG in fid_part:
                    continue
                got.setdefault((fid_part, float(p.xy[0, 0]), float(p.xy[0, 1])), p)
        return [got[k] for k in sorted(got)]

    def apply(self, lat, lon, base, lift=0.0, roads_only=False, planned_only=False):
        """_Planner.apply by the points' regions (the anchor plays no part). Same shape as base."""
        lat = np.asarray(lat, np.float64)
        lon = np.asarray(lon, np.float64)
        base = np.asarray(base, np.float64)
        if base.size == 0:
            return base
        parts = self.store.planners_of(lat, lon)
        if len(parts) == 1:
            return parts[0][0].apply(lat, lon, base, lift, roads_only, planned_only)
        la, lo, b = lat.ravel(), lon.ravel(), base.ravel()
        out = np.array(b, np.float64, copy=True)
        for pl, idx in parts:
            out[idx] = pl.apply(la[idx], lo[idx], b[idx], lift, roads_only, planned_only)
        return out.reshape(base.shape)

    def open_tunnels(self, lat, lon):
        """_Planner.open_tunnels by the points' regions: [(lat (k,), lon (k,), Spec)], each section once."""
        lat = np.asarray(lat, np.float64).ravel()
        lon = np.asarray(lon, np.float64).ravel()
        if lat.size == 0:
            return []
        out, seen = [], set()
        for pl, idx in self.store.planners_of(lat, lon):
            for la, lo, spec in pl.open_tunnels(lat if idx is None else lat[idx], lon if idx is None else lon[idx]):
                k = (round(float(la[0]), 7), round(float(lo[0]), 7), len(la))
                if k not in seen:
                    seen.add(k)
                    out.append((la, lo, spec))
        return out


def trim_plan(p, box):
    """The pieces of Plan p within box (x0, y0, x1, y1), or a convex polygon (k, 2): cut at its edges (earthfast.clip_segments), heights and
    cuttings with them; an end not at p's own end leads onto no bridge. [Plan]."""
    import fast
    xy = np.ascontiguousarray(np.asarray(p.xy, np.float64)[:, :2])
    if len(xy) < 2:
        return [p]
    if np.ndim(box) == 2:               # a convex polygon (a cell's quad: _Planner._clip_quad)
        fast.require(104, "clip_segments_convex")
        t = np.asarray(fast._ef.clip_segments_convex(xy, np.ascontiguousarray(box, np.float64)))
    else:
        fast.require(44, "clip_segments")
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
        pb = getattr(p, "bank", None)
        if pb is not None:
            pb = np.r_[pb[i] + (pb[i + 1] - pb[i]) * t0, pb[i + 1:j + 1], pb[j] + (pb[j + 1] - pb[j]) * t1]
        if np.hypot(*(pxy[-1] - pxy[0])) > 1e-6 or len(pxy) > 2:
            q = Plan(pxy, pz, p.half, (p.abut[0] and i == 0 and t0 == 0.0, p.abut[1] and j == n - 1 and t1 == 1.0),
                     None if p.cut is None else np.asarray(p.cut)[i:j + 1].copy(), p.road_half)
            q.rid = getattr(p, "rid", -1)
            q.max_wall = getattr(p, "max_wall", WALL_MAX_H_M)
            q.bank = pb
            out.append(q)
        i = j + 1
    return out


def road_id(fid):
    """A stable integer id of a map feature (grade_points: a road never gives way to itself, though each map cell
    plans its own stretch of it: the Stelvio Pass's hairpins sank under their own other stretches, 2026-10-21)."""
    import zlib
    return zlib.crc32(str(fid).encode()) & 0x7FFFFFFF


class PlanPack:
    """Plans (a list, in grade_points' order) as grade_points takes them: every segment's arrays concatenated once, with
    each plan's box; a call keeps the segments of the plans whose boxes meet its points', in the same order (built per
    call, the plans' Python loop cost a Canton tile set ~1 s, 2026-10-25)."""

    def __init__(self, plans):
        rows = [(k, p) for k, p in enumerate(plans) if len(p.xy) >= 2]
        self.n = len(rows)
        if not self.n:
            return
        f64 = np.float64
        npts = np.array([len(p.xy) for _k, p in rows], np.int64)
        nseg = npts - 1
        xy = np.concatenate([np.asarray(p.xy, f64)[:, :2] for _k, p in rows])
        z = np.concatenate([np.asarray(p.z, f64) for _k, p in rows])
        last = np.cumsum(npts) - 1
        ia = np.delete(np.arange(len(xy)), last)            # each segment's first point (not a plan's last point)
        self.a, self.b = np.ascontiguousarray(xy[ia]), np.ascontiguousarray(xy[ia + 1])
        self.za, self.zb = np.ascontiguousarray(z[ia]), np.ascontiguousarray(z[ia + 1])
        bk = np.concatenate([np.asarray(p.bank, f64) if getattr(p, "bank", None) is not None else np.zeros(len(p.xy))
                             for _k, p in rows])
        self.ba, self.bb = np.ascontiguousarray(bk[ia]), np.ascontiguousarray(bk[ia + 1])
        self.h = np.repeat(np.array([p.half for _k, p in rows], f64), nseg)
        self.rh = np.repeat(np.array([getattr(p, "road_half", p.half) for _k, p in rows], f64), nseg)
        rids = [getattr(p, "rid", -1) for _k, p in rows]
        self.rid = np.repeat(np.array([r if r is not None and r >= 0 else 2 ** 40 + k
                                       for (k, _p), r in zip(rows, rids)], np.int64), nseg)
        s0 = np.r_[0, np.cumsum(nseg)]
        ab = np.zeros(int(s0[-1]), bool)
        cut = np.full((int(s0[-1]), 6), np.nan)
        for n, (_k, p) in enumerate(rows):
            abut = getattr(p, "abut", (False, False))
            if abut[0] or abut[1]:
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p.xy[:, :2], axis=0).T))]
                if abut[0]:
                    ab[s0[n]:s0[n + 1]] |= arc[:-1] < ABUT_NEAR_M
                if abut[1]:
                    ab[s0[n]:s0[n + 1]] |= arc[-1] - arc[1:] < ABUT_NEAR_M
            c = getattr(p, "cut", None)
            if c is not None:
                cut[s0[n]:s0[n + 1]] = c
        self.ab = ab.astype(np.uint8)
        self.cut = cut
        self.box = np.array([p.bbox for _k, p in rows], f64).reshape(-1, 4)
        self.of = np.repeat(np.arange(self.n), nseg)

    def near(self, x0, y0, x1, y1):
        """The segment arrays of the plans whose boxes meet (x0, y0, x1, y1), or None."""
        if not self.n:
            return None
        bx = self.box
        keep = ~((bx[:, 2] < x0) | (bx[:, 0] > x1) | (bx[:, 3] < y0) | (bx[:, 1] > y1))
        if not keep.any():
            return None
        if keep.all():
            return self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid, self.ab, self.cut, self.ba, self.bb
        sk = keep[self.of]
        return tuple(np.ascontiguousarray(v[sk]) for v in (self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid,
                                                           self.ab, self.cut, self.ba, self.bb))


def grade_points(px, py, base, plans, lift=0.0):
    """base at the points (px, py) graded under the plans (see the module doc; a list or a PlanPack of them), by the
    Rust loop (native/earthfast grade_points: a Python loop per segment was the server's largest CPU cost while driving,
    2026-10-12). Pure."""
    px = np.asarray(px, np.float64)
    py = np.asarray(py, np.float64)
    base = np.asarray(base, np.float64)
    out = base.copy()
    if not len(px):
        return out
    pack = plans if isinstance(plans, PlanPack) else PlanPack(plans)
    # every segment of the plans near the points: ends, end heights, reach (core half width + T_MAX)
    got = pack.near(px.min(), py.min(), px.max(), py.max())
    if got is None:
        return out
    A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB = got
    import fast
    fast.require(84, "grade_points")         # (the Rust loop: native/earthfast; its banked curves: 84)
    f64 = np.float64
    return fast._ef.grade_points(np.ascontiguousarray(px, f64), np.ascontiguousarray(py, f64),
                                 np.ascontiguousarray(base, f64), A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB, float(lift),
                                 float(SLOPE_RUN), float(T_MIN), float(T_MAX), float(BLEND_R), float(BUCKET),
                                 float(WALL_CUT_M), float(WALL_T_M), float(WALL_FLAT_M), float(WALL_AHEAD_M),
                                 bool(SEP_ON), float(SEP_FROM_M), float(SEP_FULL_M), float(CORE_FADE_M),
                                 float(SIDE_FADE_M), float(SHARP_STEP_M))


def bridge_chain(line, all_bridges):
    """The bridge parts joined to line through their ends (within 0.75 m), line itself excluded. Pure."""
    line = np.asarray(line, np.float64)[:, :2]
    parts = [np.asarray(b, np.float64)[:, :2] for b in all_bridges]
    parts = [b for b in parts if not (len(b) == len(line) and np.array_equal(b, line))]
    if not parts:
        return []
    first = np.array([b[0] for b in parts])
    last = np.array([b[-1] for b in parts])
    out, frontier, used = [], [line[0], line[-1]], np.zeros(len(parts), bool)
    while frontier:
        e = frontier.pop()
        d = np.minimum(np.hypot(first[:, 0] - e[0], first[:, 1] - e[1]), np.hypot(last[:, 0] - e[0], last[:, 1] - e[1]))
        for k in np.flatnonzero((d < 0.75) & ~used):    # (in the parts' order, as a loop over them would)
            used[k] = True
            out.append(parts[k])
            frontier += [parts[k][0], parts[k][-1]]
    return out


def bridge_chain_ends_free(line, all_bridges, xy):
    """Whether the end of bridge line nearest xy is an abutment: no other bridge part ends within 0.75 m. Pure."""
    line = np.asarray(line, np.float64)
    end = line[0, :2] if np.hypot(*(line[0, :2] - xy[:2])) <= np.hypot(*(line[-1, :2] - xy[:2])) else line[-1, :2]
    for o in all_bridges:
        o = np.asarray(o, np.float64)
        if len(o) == len(line) and np.array_equal(o, line):
            continue
        if min(np.hypot(*(o[0, :2] - end)), np.hypot(*(o[-1, :2] - end))) < 0.75:
            return False
    return True


def bridge_lifts(line, all_bridges, ground_lines, z_at, chain=None, rail_lines=()):
    """[(end xy (2,), lift m)] of a bridge part (line (k, 2) ENU m): at each of its ends that no other bridge part
    continues from (an abutment), how far the ground there must be raised for the deck to clear every road crossing
    under it (roads.DECK_ROAD_CLEAR + APPROACH_MARGIN_M over the road, full height within APPROACH_FLAT_M of the end,
    then falling at roads.DECK_GRADE), within [APPROACH_MIN_LIFT, APPROACH_MAX_LIFT]. Roads crossing within
    APPROACH_MIN_CROSS_M of either end meet it at grade and do not count; railways (rail_lines) always count (a track
    never meets a road deck at grade: the deck ended 3 m past one at Vitebsk, GitHub #324). z_at(xy (n, 2)) -> ground
    m. Pure given z_at."""
    line = np.asarray(line, np.float64)[:, :2]
    if len(line) < 2:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]
    # (a short part, or one no road crosses under, still raises its free end for the roads under the rest of its chain:
    # the deck carried a few metres onto the road joined at its end (roads._skew_decks) left that end unraised and the
    # whole deck ~4.5 m low over BR-343 at Teresina, GitHub #306)
    if s[-1] < 2 * APPROACH_MIN_CROSS_M and not chain:
        return []
    cr = roadsmod._crossings([line], ground_lines)
    nr = len(cr)
    if rail_lines:
        cr = cr + roadsmod._crossings([line], list(rail_lines))
    if not cr and not chain:
        return []
    zu = z_at(np.array([c[2] for c in cr], np.float64).reshape(-1, 2)) if cr else np.zeros(0)
    arcs = np.array([c[1] for c in cr], np.float64)

    def joined_at(end, own=None):
        own = line if own is None else own
        for other in all_bridges:
            if other is own or (len(other) == len(own) and np.array_equal(np.asarray(other)[:, :2], own[:, :2])):
                continue
            o = np.asarray(other, np.float64)
            if min(np.hypot(*(o[0, :2] - end)), np.hypot(*(o[-1, :2] - end))) < 0.75:
                return True
        return False
    joined = (joined_at(line[0]), joined_at(line[-1]))
    # (not a junction at an abutment; at a joint with the chain's next part the crossing is mid-bridge: Commerce
    # Street's bridge over I-95 in Springfield VA, split 2 m from where the motorway passes under, GitHub #138)
    rail = np.arange(len(cr)) >= nr
    under = ((arcs >= APPROACH_MIN_CROSS_M) | joined[0]) & ((arcs <= s[-1] - APPROACH_MIN_CROSS_M) | joined[1]) | rail
    zu, arcs, rail = zu[under], arcs[under], rail[under]
    pts = np.array([c[2] for c in cr], np.float64)[under].reshape(-1, 2)
    # (the crossings under the rest of its chain, by their distance from the abutment: Overture splits a bridge where
    # other ways meet it, and the road under it may pass under the next part, GitHub #138)
    # (a road meeting a part's free end is a junction at grade there, as for the line itself: the trunk's own next
    # carriageway at the far abutment of its chain counted as a road under it and raised a residential bridge joined
    # to the chain 9.7 m, the trunk deck climbing 10 m in 20 m to it, US-24 near Buena Vista CO, 2026-10-27)
    more = []
    for part in (chain or ()):
        part = np.asarray(part, np.float64)[:, :2]
        sp = float(np.hypot(*np.diff(part, axis=0).T).sum())
        pj = (joined_at(part[0], part), joined_at(part[-1], part))
        for c in roadsmod._crossings([part], list(ground_lines)):
            a = float(c[1])
            if (a >= APPROACH_MIN_CROSS_M or pj[0]) and (a <= sp - APPROACH_MIN_CROSS_M or pj[1]):
                more.append(np.asarray(c[2], np.float64))
        for c in roadsmod._crossings([part], list(rail_lines)) if rail_lines else ():
            more.append(np.asarray(c[2], np.float64))
    if more:
        mp = np.array(more, np.float64)
        zu = np.r_[zu, z_at(mp)]
        pts = np.r_[pts, mp]
        rail = np.r_[rail, np.zeros(len(mp), bool)]
    if not len(zu):
        return []
    out = []
    for (end, at), jn in zip(((line[0], 0.0), (line[-1], s[-1])), joined):
        if jn:
            continue
        d = np.r_[np.abs(arcs - at), np.hypot(*(pts[len(arcs):] - end).T)] if more else np.abs(arcs - at)
        far = (d >= APPROACH_MIN_CROSS_M) | rail     # (a road meeting the abutment itself: a junction at grade)
        if not far.any():
            continue
        d, zu_e = d[far], zu[far]
        need = zu_e + roadsmod.deck_road_clear(roadsmod.deck_depth(roadsmod.geom_span([line] + list(chain or ()))))             + APPROACH_MARGIN_M \
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


def underpass_offsets(lines, requirements, queries=None):
    """Smooth underpass dips along joined ground roads, never across space (earthfast.underpass_offsets; after
    wyo-backroads' PR #374): m (<= 0) to add to the roads' heights.

    lines: the ground roads [(k, 2)]; requirements [(line, arc m, depth m)] of the overpasses they cross under.
    queries: [(line, points (k, 2) on it)] where the offsets are asked (default: every line's own points); one
    array per query (per line by default). Roads join where their ends meet; a node shared by several roads takes
    one offset in each, the eighth norm combines nearby dips without summing their depths, loose ends stay pinned
    and only joined ground junctions carry a dip past the road under the bridge. Pure."""
    import fast
    fast.require(85, "underpass_offsets")
    f64 = np.float64
    pts = [np.asarray(ln, f64)[:, :2] for ln in lines]
    if queries is None:
        queries = list(enumerate(pts))
    if not queries:
        return []
    qs = [np.asarray(q, f64).reshape(-1, 2) for _l, q in queries]
    if not pts or not requirements:
        return [np.zeros(len(q)) for q in qs]
    starts = np.r_[0, np.cumsum([len(p) for p in pts])].astype(np.int64)
    xy = np.ascontiguousarray(np.vstack(pts)) if len(starts) and starts[-1] else np.zeros((0, 2))
    req = np.ascontiguousarray(np.asarray(requirements, f64).reshape(-1, 3))
    qxy = np.ascontiguousarray(np.vstack(qs)) if sum(len(q) for q in qs) else np.zeros((0, 2))
    qline = np.repeat(np.array([int(l) for l, _q in queries], np.int64), [len(q) for q in qs])
    prm = np.array([UNDER_FLAT_M, UNDER_GRADE, UNDER_MAX_GRADE, UNDER_MAX_DROP_M, UNDER_JOINT_FLAT_M,
                    UNDER_JOINT_FADE_M, 0.5], f64)
    out = fast._ef.underpass_offsets(xy, starts, req, qxy, qline, prm)
    return np.split(out, np.cumsum([len(q) for q in qs])[:-1])


def under_dip(d, depth, room=(math.inf, math.inf)):
    """m added (<= 0) to a road's heights d m (along it, signed) from where it crosses under an overpass, for a dip
    depth m deep: flat under the deck (UNDER_FLAT_M either way), then back up on a cosine ramp whose steepest grade is
    UNDER_GRADE (a V of straight UNDER_GRADE ramps was a 6 % kink at each end and at the bottom: at motorway speed a
    jolt). room: m from the crossing to the road part's (start, end): a ramp that would run past it is made shorter,
    up to UNDER_MAX_GRADE, so the dip ends inside the part (its next part, past a junction, is not lowered: a step).
    None when that is too steep. Pure."""
    d = np.asarray(d, np.float64)
    run = depth * math.pi / (2 * UNDER_GRADE)
    runs = []
    for r in room:
        avail = r - UNDER_FLAT_M
        if avail >= run:
            runs.append(run)
        elif avail > 0 and depth * math.pi / (2 * avail) <= UNDER_MAX_GRADE:
            runs.append(avail)
        else:
            return None
    rr = np.where(d < 0, runs[0], runs[1])
    u = np.clip((np.abs(d) - UNDER_FLAT_M) / np.maximum(rr, 1e-9), 0.0, 1.0)
    return -depth * 0.5 * (1.0 + np.cos(math.pi * u))


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


def _never_raised(spec):
    """A road whose approaches are never raised: a motorway or trunk (APPROACH_SKIP), not one of their links. A link
    leaving a motorway's bridge runs down a fill to the street: kept at the ground, its deck fell 11 m within 90 m and
    dropped beside its mainline while they still overlapped, and an AI car drove into the mainline's parapet trying to
    take the exit (US-101's exit 1E over the Los Angeles River, 2026-10-27)."""
    return spec.cls in APPROACH_SKIP and not _is_link(spec)


class GradedDem:
    """dem.Dem with sample() graded under the roads; everything else passes through."""

    def __init__(self, dem, grader):
        self._dem, self._grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        base = self._dem.sample(lat, lon, zoom, progress) if progress is not None else self._dem.sample(lat, lon, zoom)
        return self._grader.apply(lat, lon, base)

    def __getattr__(self, name):
        return getattr(self._dem, name)
