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
from runtimeprofiler import step as profile_step
import logging
import math
import os
import threading
from collections import OrderedDict
import time
from runtimeprofiler import ThreadPoolExecutor

import numpy as np

import bumps
import mapcells
import railways as railwaysmod
import roads as roadsmod
import seatiles

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
DIRT_SIGMA_M = 8.0          # m, profile smoothing of gravel / dirt roads (the main ones: they stay smooth)
# Toned down 2026-11-06 after 1.8.2 (GitHub #783, #784: "like a motocross track", "easier to drive beside the road
# than on it"): as merged, a forest track had dips to 1.5 m and 66 % grades between samples, a minor dirt road 65 cm
# dips, and every dirt road leaned up to 20 % sideways. Now a worn country road everyday cars drive: potholes of 5-12
# cm, a step of at most 15 cm per 2 m, a camber of a few percent, the elevation data's own noise smoothed over 6 m.
# a minor dirt road follows the elevation data closely instead, so it is bumpy and cannot be taken flat out
DIRT_SIGMA_MINOR_M = float(os.environ.get("BNE_DIRT_SIGMA", "6.0"))   # (5 followed the DEM's bumps: GitHub #414)
# ... where the elevation is only the coarse global model (dem.coarse_at: 30 m cells, 1-3 m of noise per cell), 6 m
# follows that noise, not the ground: a dirt road there is smoothed like an unclassified road (its potholes and
# patches, dirt_bumps, are added after and stay). GitHub #835: Fermanagh's gravel roads "undriveable at speed"
DIRT_SIGMA_COARSE_M = float(os.environ.get("BNE_DIRT_SIGMA_COARSE", "16.0"))
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
MOUNTAIN_SHOULDER_M = 0.5   # ... beside a mountain pass road (roads._mountain_roads)
# A mountain pass road's sides (Plan.steep): back to the natural ground at 1:STEEP_RUN over STEEP_T_MIN m at the least,
# not SLOPE_RUN / T_MIN's long fill and cut slopes (on the Stelvio they made a 20 m bench of a 6 m road between its wall
# and its parapet, the parapet's drop never within its reach: roads.PARAPET_*)
LINK_TURN_DEG = 20.0        # (PlanPack: two plans' ends at one point within this of straight on: one road going on)
EXTRAP_M = float(os.environ.get("BNE_GRADE_EXTRAP", "4"))             # m a segment's line is carried on past an end where its plan goes on (grade_points: no
                            # washboard of the vertex step on a grade)
STEEP_RUN = 0.5
STEEP_T_MIN = 1.5
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
WALL_FLAT_M = 1.0           # ... after one terrain grid step (1 m) at the road's level behind the wall: rising at the
                            # wall itself, the grid's triangles stood as dark wedges in front of its face; the wall's
                            # top (roadmesh.WALL_THICK_M) covers the strip
TUNNEL_TAG = ":tun"          # in the fid of the tunnels' own plans (open sections, mouths): apply(roads_only) skips them
MOUTH_PLAN_M = 4.0          # m into a tunnel from a portal its floor is a grading plan too (Grader._mouth_plans)
TUNNEL_KEEP_IN_M = 3.0      # (roads.LOW_HOLE_M: a low arch's mouths, flat at its floor)
TUNNEL_KEEP_M = 5.0         # m beside a covered tunnel's road where the ground over it is left ungraded ...
OVER_TUNNEL_SMALL_M = 0.5   # ... but for a change smaller than this (a road crossing over it, graded flush)
WALL_AHEAD_M = 2.5          # ... only beside a segment or this little past its end (a joint between two: VERTEX_STEP)
YIELD_RAMP_M = 0.5          # m over which a road gives way to another's carriageway at another level (grade_points): in
                            # height difference up to WALL_CUT_M and in distance past its own carriageway's edge
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
PLAN_SLOW_S = 60.0          # s: a cell's plans still being made this long: its process's stacks to the log (stackdump)

# Junctions: a side road's end takes the height of the road it joins (snap_junctions; mpsteppe's PR #290)
JUNCTION_SNAP_ON = os.environ.get("BNE_JUNCTION_SNAP", "1") != "0"
JUNCTION_RANK = {"motorway": 9, "trunk": 8, "primary": 7, "secondary": 6, "tertiary": 5, "unclassified": 4,
                 "residential": 3, "living_street": 2, "service": 1}
JUNCTION_REACH_M = 3.0      # m beyond the joined road's carriageway edge a side road's end still counts as joining it
JUNCTION_END_M = 10.0       # m: a same-class road ending this close to the other's end continues it (no snap)
JUNCTION_TAPER_M = 40.0     # m along the side road the height change is eased in over (half the road when shorter)
JUNCTION_MAX_M = 6.0        # m: a bigger difference is left as it was (bad data; Perm, GitHub #414: 4-5 m steps where a
                            # 30 m DEM smeared an embankment into the junctions)
# The grade guard (grade_guard; docs/new/003_grade-guard): the last pass over a cell's plans. Per class: the steepest
# grade a road of it may have unless the ground itself is steeper there (GUARD_GRADE, over the ground's own + 2 %), and
# the window rule (GUARD_WINDOW: the points WIN_N m before and after standing within WIN_H of each other, the point
# between may not stand more than WIN_H outside them). The ceilings stand over the norms (ДБН В.2.3-4: 3-6 % on a
# motorway, up to 8 % in a town) because the old roads are steeper than the new ones.
GUARD_ON = os.environ.get("BNE_GRADE_GUARD", "off") == "on"     # off by default: it changes every road (play.env.d)
GUARD_GRADE = {"motorway": 0.06, "trunk": 0.06, "primary": 0.09, "secondary": 0.09, "tertiary": 0.12,
               "unclassified": 0.12, "residential": 0.12, "living_street": 0.18, "service": 0.18}
GUARD_GRADE_OTHER = 0.30    # tracks, paths, anything else
GUARD_LINK_BONUS = 0.02     # a slip road may be this much steeper than its class
GUARD_MOUNTAIN_BONUS = 0.04  # a pass road
GUARD_WINDOW = {"motorway": (50.0, 0.5), "trunk": (50.0, 0.5), "primary": (40.0, 0.75), "secondary": (40.0, 0.75),
                "tertiary": (25.0, 1.0), "unclassified": (25.0, 1.0), "residential": (25.0, 1.0),
                "living_street": (15.0, 1.5), "service": (15.0, 1.5)}
GUARD_WINDOW_OTHER = (10.0, 2.0)
GUARD_REACH_M = 3.0         # m past a road's edge an end still joins it (as JUNCTION_REACH_M)
GUARD_DESIGNED_M = 0.3      # m from the natural height from which a point counts as a solved shape (a fill, a dip)
JUNCTION_HOLD_PAD_M = 2.0   # m beyond the joined road's carriageway a side road keeps its height (_hold_junction_corridors)
JUNCTION_HOLD_GRADE = 0.12  # ... and goes back to its own profile at most this steep
# A road's change at a junction eased back to its own profile no longer than it must be (junction_pads): the cosine adds
# at most JUNCTION_EASE_GRADE to the road's own grade (over 40 m whatever the step, a track leaving up a 30 % bank ran
# in a trench 1-2.4 m deep for 30 m, the user 2026-10-29 at -32.0951, 116.03866: "a closer and a bit more aggressive"
# curve), JUNCTION_EASE_MIN_M at least (a small step over a short ease: a kink). (The side road's landing on the joined
# road's surface, and equal-ranked T junctions, are the pads': snap_junctions, in Rust since 1.7.3, is left as it was.)
JUNCTION_EASE_GRADE = float(os.environ.get("BNE_JUNCTION_EASE_GRADE", "0.04"))
JUNCTION_EASE_MINOR_GRADE = 0.15    # ... on a track, a driveway, a service road (pad_class_w under PAD_LANDING_MIN_W)
JUNCTION_EASE_MIN_M = 30.0
JUNCTION_EASE_MINOR_MIN_M = 8.0


def ease_length(step, minor=False):
    """m a height step (m) is eased out over (cosine) along a road: its added grade at most JUNCTION_EASE_GRADE
    (JUNCTION_EASE_MINOR_GRADE on a minor road), from JUNCTION_EASE_MIN_M (_MINOR_) to JUNCTION_TAPER_M. Pure."""
    g = JUNCTION_EASE_MINOR_GRADE if minor else JUNCTION_EASE_GRADE
    lo = JUNCTION_EASE_MINOR_MIN_M if minor else JUNCTION_EASE_MIN_M
    return float(np.clip(abs(step) * math.pi / (2.0 * max(g, 1e-6)), lo, JUNCTION_TAPER_M))


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
# Neighbouring roads coupled (the user, 2026-10-29: "the current model sinks or lifts roads too much, where perhaps the
# roads need to be on a similar level"): two at-grade roads side by side are each sampled and smoothed on their own, and
# the coarse model's error does not cancel between them: a frontage road 18 m beside a primary ended up 2-3x as far above
# or below it as the ground truly is, with steep connectors between. On coarse elevation (dem.coarse_at), where a road
# runs alongside a higher- (or equal-, half way) ranked one, within NEIGHBOUR_GAP_M of its edge (fully within
# NEIGHBOUR_FULL_M) and within NEIGHBOUR_PARALLEL_DEG of its direction, it takes that road's height plus the ground's own
# fall between them: the difference of a far smoother surface (NEIGHBOUR_TREND_SIGMA_M: a hillside's grade, not the
# data's noise). A difference over NEIGHBOUR_MAX_M is left: a real separation. Roads meeting at a node are left to
# snap_junctions. Before the junctions are snapped (connectors meet the coupled heights) and the approaches lifted and
# the underpasses dipped (bridges and their roads keep their separation). BNE_NEIGHBOURS=1: on. (Off by default, as the
# pads and the rail check: the three were ~20 % of a Midtown road tile set's build in Python, 2026-11-03, PR #482/#671.)
NEIGHBOUR_ON = os.environ.get("BNE_NEIGHBOURS", "0") == "1"
NEIGHBOUR_GAP_M = 20.0
NEIGHBOUR_FULL_M = 12.0
NEIGHBOUR_PARALLEL_DEG = 30.0
NEIGHBOUR_MAX_M = 4.0
NEIGHBOUR_TREND_SIGMA_M = 45.0
NEIGHBOUR_SMOOTH_M = 10.0       # m: the change smoothed along the road: no step where the neighbour ends


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
# A ramp the lidar measured (its deck end's height, roadz) rises that high beside a road at grade on a steep side (the
# lower road then cut into the raised ground, its retaining wall: WALL_CUT_M), as a real approach's walls stand: with
# the 1:2 fill slope it was capped to 0.7 m of a measured 3.3 m 27 m off the road beside it (Springfield's Old Keene
# Mill Road, approachlidar.py, 2026-10-31)
MEASURED_SIDE_RUN = 0.5
APPROACH_LIDAR_ON = os.environ.get("BNE_APPROACH_LIDAR", "on") != "off"   # (roadz APPROACH_*: Grader._obs)
TWIN_ON = os.environ.get("BNE_APPROACH_TWINS", "on") != "off"
TWIN_M = 60.0               # m: another abutment this near whose approach runs beside a ramp ...
TWIN_SHARE = 0.5            # ... and is raised at least this share as much rises with it (no cap between them)
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
# A raceway's surface (GitHub #833: a GT3 flat out through a Nordschleife kink was launched by the elevation data's
# bumps): the terrain still, so the banking stays (Daytona's 31 degrees), but smoothed along the track: the ground's
# along-track Gaussian at each offset across, on a grid of RACEWAY_STEP_M along by RACEWAY_ACROSS_M across (the surface
# itself, not a correction to the point's ground: one of 2 m steps under-shot 6 m bumps by half); each map segment's own, the line continued RACEWAY_EXTEND sigmas past its ends (straight on,
# or round a closed loop) so its ends are smoothed too and every segment's is a pure function of its line and the DEM.
# It fades out over UNGRADED_MARGIN_M (kerbs, apron) to the plain ground. BNE_RACEWAY_SMOOTH=off: the ground as it is.
RACEWAY_SMOOTH_ON = os.environ.get("BNE_RACEWAY_SMOOTH", "on") != "off"
RACEWAY_SIGMA_M = float(os.environ.get("BNE_RACEWAY_SIGMA", "10.0"))
RACEWAY_STEP_M = 2.0
RACEWAY_ACROSS_M = 1.0
RACEWAY_EXTEND = 3.0
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
# m: road ends this close are one node of the dip's network (underpass_offsets). At 0.5 m a trunk road whose end the map
# left 1.7 m short of its junction (Tonkin Highway at Morley Drive, Perth, GitHub #915) was not lowered with the
# junction's other roads, 6.1 m down for the underpass: more than snap_junctions takes (JUNCTION_MAX_M), so it stood
# as a 5 m step in 8 m (a 91 % grade) where it met them. Within a lane's width, as the junctions' own reach.
UNDER_NODE_M = float(os.environ.get("BNE_UNDER_NODE", "2.0"))
# A dip never takes a road under the sea's surface (+ UNDER_SEA_M): the sea's WaterBlock tiles flood their whole square
# (seatiles.py), so on low ground by the sea or a tidal river the dug road lay under water (Perth's Mitchell and Kwinana
# Freeways by the Swan River, GitHub #515, #518: ground 1-3 m up, dug up to UNDER_MAX_DROP_M; rebuilt offline
# 2026-10-30, 363 and 703 points of road on dry ground dug to -6.0 m and -5.4 m, none after). Held at every point of the road, not only at the crossing: a crossing on a bank under UNDER_SEA_M
# dug its flat and ramps 2-4 m under the sea on the dry ground beside (#515's interchange, rebuilt offline 2026-10-30).
# The deck keeps less headroom there instead (as UNDER_MAX_DROP_M: rather a low bridge than a flooded road); a road
# already under that is not lowered. A polder's (under UNDER_POLDER_M) dips as before: the tiles split away from it.
UNDER_SEA_ON = os.environ.get("BNE_UNDER_SEA", "on") != "off"
UNDER_SEA_M = 0.5           # m over the sea surface (seatiles.SEA_E) a dip's floor stays (the tiles' curve: <= 0.17 m)
UNDER_POLDER_M = -1.0       # m: a road this far under the sea is behind dikes (the Netherlands' polders: 2-6 m under)
# Where the elevation is lidar, a deck (its ends' ground, as raised) standing less than this over the road crossing
# under it is not at deck height in the data: the lidar has the road's own ground there, and the road keeps it (the deck
# rises instead). Sassafras Way runs on level ground under Pittsburgh's 28th Street Bridge (Street View), the bridge's
# mapped end 0.4 m under it in the lidar; dug 8 m to clear that, its dip ran on through 28th Street and lowered Liberty
# Avenue 3 m into a walled trench (the user's report, 2026-10-29). (Not on the coarse models: no cutting there, BR-381.)
UNDER_LIDAR_ON = os.environ.get("BNE_UNDER_LIDAR", "on") != "off"
UNDER_LIDAR_DECK_M = roadsmod.AT_GRADE_M
# A road's ground where a deck crosses over it is read from beside the deck (deck_capped): the elevation under a deck is
# the deck, not the road. Lidar keeps some short decks as ground (Tartu mnt's two 18-21 m decks over Järve tee at Assaku,
# Estonia, 59.3788 N 24.8441 E: a 6 m hump 40 m long in the road under them) and fills a removed bridge's gap at about
# the deck's height (the A28 / I-84 cases above). Järve tee's plan climbed the hump, and the underpass dip, measured from
# it, lowered the road and the streets joined to it 5.5 m under their ground for 560 m (GitHub #685). Under a deck the
# road's samples are capped at the straight line between its ground UNDER_DECK_SIDE_M past the deck's sides, in its plan
# (plan_roads) and its underpass depth (_under_ground) alike: never raised (a real dip under a bridge stays). Not on the
# coarse models (their smeared embankments are wider than that: UNDER_RUN_*).
UNDER_DECK_ON = os.environ.get("BNE_UNDER_DECK_GROUND", "on") != "off"
UNDER_DECK_SIDE_M = 15.0    # m past a deck's side the road's ground is read (Assaku's hump: 6-13 m past its decks)
# A walkway bridge is not a road, so it never becomes a deck. The elevation still has it: a footbridge across a
# motorway cut is a ridge of ground in the carriageway (a catwalk over I-87 at Lake George, GitHub #802). Its span
# caps the road under it the same way a road deck does, and nothing else (no ramp, no abutment).
WALK_SPAN_CLASSES = {"footway", "path", "pedestrian", "cycleway", "bridleway", "steps"}
WALK_SPAN_HALF_M = 4.0      # m: the walkway, wide enough that the side read covers the ridge the elevation leaves
UNDER_DECK_MAX_M = 30.0     # m along the road a deck covers at most either side of the crossing (an oblique one)
UNDER_DECK_PAD_M = 3.0 * SIGMA_MAJOR_M + PROFILE_STEP + UNDER_DECK_SIDE_M + UNDER_DECK_MAX_M    # m round a cell: the
                            # farthest its plans' samples reach past its box (their extensions), and a deck's window
# m around a cell's plans whose roads and spans its dips are solved on: the farthest a dip or a loose end reaches
# (underpass_offsets: flat + the longest ramp, a junction's share) and a span's length; every cell sees the same
UNDER_NEAR_M = 4 * VERTEX_STEP  # m past a cell's box its plans' dips are made (to their pieces' cut ends there)
UNDER_REACH_M = UNDER_FLAT_M + UNDER_MAX_DROP_M * math.pi / UNDER_GRADE + 2 * UNDER_JOINT_FLAT_M \
    + UNDER_JOINT_FADE_M + 10.0
# ... and under any other short overpass, by what its ramps could not raise it (BR-381 near Pouso Alegre: a local
# road's bridge whose ramps the frontage roads at both ends held down sat ~1 m over the motorway; the 30 m elevation
# data has no cutting under it, and the AI car drove into its abutment wall, 2026-10-25)
UNDER_ANY_ON = os.environ.get("BNE_UNDERPASS_ANY", "on") != "off"
# A single-span bridge and the roads under it solved as one height problem (crossings.py; BNE_INTERCHANGE=1, off by
# default): its ends raised, or the roads under it lowered, whichever costs less, from the ground they can be trusted
# for (not lidar's fill under the deck, not an abutment on its slope). Replaces the dip, its lidar exception and the
# ramps for those bridges (a motorway's too); the rules above decide for every other.
import crossings as _xs
import carriageways as _cw
CW_ON = _xs.ON and os.environ.get("BNE_CARRIAGEWAYS", "on") != "off"          # carriageways.py: a motorway's roads and decks as one profile (with BNE_INTERCHANGE)
CW_MAX_M = 8000.0       # m: a carriageway chain is cut here (a branch point or a loose end comes long before)
CW_HEADROOM_M = roadsmod.MIN_BRIDGE_HEADROOM_M + roadsmod.BRIDGE_HEADROOM_MARGIN_M   # m of headroom the carriageways
                        # keep over one another, the roads and the tracks under them (+ the deck's depth): the decks' own
                        # (roads.deck_road_clear), so their girders clear what they span
CW_NONFAM_HI = False    # other roads' decks over a carriageway bound it: off (their decks rise over it as before; from their
                        # ends' ground a deck read low and dipped the Mitchell Freeway ~1 m under each street bridge)
CW_MIN_SIN = 0.3        # two carriageways crossing at a flatter angle are not a grade separation (lines along each other)
CW_STACK_SIN = 0.03      # decks at different levels crossing more steeply than this (2 degrees) are separated
CW_OVERLAP_MAX_M = 120.0  # m either side of a crossing two decks' clearance is held along at most
CW_OVERLAP_STEP_M = 12.0  # m between the points two overlapping decks' clearance is held at


def _merge_span_chains(parts):
    """parts [(line (k, 2+), fixed, rail)] with every simple chain of them joined into one line: parts of the same
    (fixed, rail) whose ends meet within UNDER_CHAIN_JOIN_M at a node no other part's end reaches. Pure."""
    f64 = np.float64
    n = len(parts)
    if n < 2:
        return list(parts)
    ends = np.array([[np.asarray(b, f64)[0, :2], np.asarray(b, f64)[-1, :2]] for b, _f, _r in parts]).reshape(-1, 2)
    d = np.hypot(ends[:, None, 0] - ends[None, :, 0], ends[:, None, 1] - ends[None, :, 1])
    near = d < UNDER_CHAIN_JOIN_M
    np.fill_diagonal(near, False)
    link = {}                               # end index (2 * part + 0 / 1) -> the other part's end joined there
    for e in range(2 * n):
        others = np.flatnonzero(near[e])
        if len(others) == 1 and others[0] // 2 != e // 2:
            o = int(others[0])
            if parts[e // 2][1:] == parts[o // 2][1:] and np.flatnonzero(near[o]).tolist() == [e]:
                link[e] = o
    out, used = [], set()
    for start in range(n):
        if start in used:
            continue
        # walk back to the chain's first part (or round a ring once)
        p, e_in = start, 0
        seen = {start}
        while 2 * p + e_in in link:
            o = link[2 * p + e_in]
            q = o // 2
            if q in seen:
                break
            seen.add(q)
            p, e_in = q, 1 - (o % 2)
        # and forward from it, joining
        line = np.asarray(parts[p][0], f64)
        if e_in == 1:
            line = line[::-1]
        used.add(p)
        e_out = 2 * p + (1 - e_in)
        while e_out in link:
            o = link[e_out]
            q = o // 2
            if q in used:
                break
            nxt = np.asarray(parts[q][0], f64)
            nxt = nxt if o % 2 == 0 else nxt[::-1]
            w = min(line.shape[1], nxt.shape[1])
            line = np.concatenate([line[:, :w], nxt[1:, :w]])
            used.add(q)
            e_out = 2 * q + (1 - o % 2)
        out.append((line, parts[p][1], parts[p][2]))
    return out


def _cw_level(q):
    """A carriageway part's level (its OSM layer; 1 for a bridge without one, 0 on the ground)."""
    return float(q[5]) if len(q) > 5 else (1.0 if q[2] else 0.0)


def _cw_half(q):
    return float(q[6]) if len(q) > 6 else 5.0
CW_MAX_DIG_M = 2.5      # m a carriageway is lowered at most under another road's deck (the deck is low for the rest)
CW_UNDER_M = 10.0       # m from a deck's line the ground under it is not trusted, rising to trusted at twice that
CW_MARGIN_M = 1200.0    # m past its home cell a cluster's chains reach (the farthest barely move the cell's)
CW_CLUSTER_M = 30000.0  # m of carriageways solved together at most (the far ones barely move the near)
XS_ON = _xs.ON

XS_FIXED_GRADE = 0.03   # a solved motorway's or trunk's ramps rise at this (_lift_approaches)
XS_FLOOR_M = 150.0      # m round a solved crossing no road is dug under its target (_lower_underpasses)
# ... but on coarse elevation (dem.coarse_at: the 30 m global model) a street's dip stays on the street: an overpass's
# embankments are smeared away there and its ramps, held down by the streets beside them, leave the whole clearance to
# the road under; spread along every street joined to it (underpass_offsets: by distance, at half grade), the 7.2 m dip
# under Ташкентская улица's bridge sank Демократическая улица and the streets round it 4-6 m for ~200 m while the ground
# between them stayed: walled trenches, humps of untouched ground between the streets and side roads standing as walls
# (Samara, 2026-11-02; the same trench Perm's rail bridge dug, GitHub #414, where railways.py now raises the track).
# There the dip runs along the street's own pieces (street_runs: carried on through every node where only its next
# piece joins it) and is back up by its junctions at up to UNDER_MAX_GRADE, as deep as fits: rather a lower bridge than
# a sunken neighbourhood (Samara: 3.2-3.8 m under the girders, 6-7 m before). A motorway's or trunk's dip (no
# junctions at grade: APPROACH_SKIP) spreads as before, and so does one whose street has room for less than
# UNDER_RUN_MIN_M (a junction right by the bridge is part of the underpass) or is a ring.
UNDER_RUN_ON = os.environ.get("BNE_UNDERPASS_RUN", "on") != "off"
# A short overpass mapped as several parts end to end (a 3 m stub split off at its end, two ways meeting mid-span) is one
# span for the underpass: each part had another joined to it, so none was a single span and the road under kept its
# ground: the M1's bridges near Dundalk 3.3-4 m over the motorway (GitHub #960), an overpass at the trunk's height by
# Orte (#967). Parts joined at a node where only they meet (no branch), the same road or rail kind, are one line here.
UNDER_CHAIN_ON = os.environ.get("BNE_UNDERPASS_CHAIN", "on") != "off"
UNDER_CHAIN_JOIN_M = 0.75
UNDER_COVER_M = 0.75       # m: a bridge piece within this of a longer one's line all along is a copy of part of it
UNDER_CHAIN_MAX_M = UNDER_MAX_BRIDGE_M   # m: such a chain spans this at most (Orte's two parts, 194 m: their dip, 8 m
                            # deep, ran into the trunk's next structure as a 3 m step in 12 m; left as it was)
UNDER_STUB_M = 2 * APPROACH_MIN_CROSS_M     # m: a part this short (the carriageways' fork a deck ends on, 3 m stubs on the
                                            # M1's) joined to a span leaves it a single span
UNDER_RUN_MIN_M = 2.0
ABUT_UNDER_GRADE = 0.08     # past a road's abutment end, over the deck, its ground falls at this grade (grade_points)
ABUT_NEAR_M = 14.0          # m from the bridge end the road is checked ...
ABUT_FAR_M = 40.0           # ... against its grade from ABUT_NEAR_M to this far
ABUT_STEEP = 0.15           # a fall this much steeper than that grade, near the end: the valley's side, not the road
ABUT_MIN_M = 0.3            # m: a smaller fill at the end is left out
ABUT_MAX_M = 8.0            # m filled at most
SEAMS_ON = os.environ.get("BNE_SEAMS", "on") != "off"
SEAM_MIN_M = 0.05           # m: a carriageway road's abutment end this far off its deck's end is pulled back to it
SEAM_GRADE = 0.03           # ... eased out along the road at this grade (XS_FIXED_GRADE's: a freeway's ramp) ...
SEAM_RUN_MIN_M = 24.0       # ... over at least this ...
SEAM_RUN_MAX_M = 80.0       # ... and at most this
SEAM_SIDE_M = 3.0           # m: a road's end this close to a pinned end meets the same abutment: it goes down from it
SEAM_SIDE_GRADE = 0.10      # ... at this grade (a street off a freeway's bridge end), over SEAM_SIDE_RUN_M at least ...
SEAM_SIDE_RUN_M = (12.0, 40.0)
SEAM_MOUTH_M = 12.0         # m a street off a deck's abutment keeps the abutment's height: the junction's wedge of the deck's
                            # level (a wider mouth than the carriageway's own) stands over the street's road no lower than this
SEAM_MOUTH_GRADE = 0.08     # ... and goes back to its own profile at this grade
SEAM_DECK_GRADE = 0.01      # a single span between two abutments is not tilted more than this by the ground at its ends: the
                            # rest of their difference is shared between the two approaches (_grade_match) ...
SEAM_MATCH_MAX_M = 4.0      # ... at most this each (bad data: rather a steep deck)


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


def raceway_surface(frame, elev, xy, hw, sigma=None):
    """(line (k, 2) every RACEWAY_STEP_M, its stations (k,), offsets across (nt,), surface (k, nt) m) of one raceway
    segment xy (half width hw, its margin in): RACEWAY_* above. None for a line under 2 points."""
    sigma = RACEWAY_SIGMA_M if sigma is None else float(sigma)
    xy = np.asarray(xy, np.float64)[:, :2]
    line, st = resample(xy, RACEWAY_STEP_M)
    k = len(line)
    if k < 2 or st[-1] <= 0.0:
        return None
    step = float(st[-1]) / (k - 1) if k > 1 else RACEWAY_STEP_M
    m = max(1, int(math.ceil(RACEWAY_EXTEND * sigma / RACEWAY_STEP_M)))
    closed = k > 3 and math.hypot(*(line[0] - line[-1])) < 1.0
    if closed and k > 2 * m + 2:
        head, tail = line[-1 - m:-1], line[1:m + 1]
    else:
        def unit(v):
            return v / max(math.hypot(*v), 1e-9)
        t0, t1 = unit(line[0] - line[1]), unit(line[-1] - line[-2])
        d = step * np.arange(1, m + 1)[:, None]
        head, tail = (line[0] + t0 * d)[::-1], line[-1] + t1 * d
    full = np.concatenate([head, line, tail])
    tan = np.gradient(full, axis=0)
    tan /= np.maximum(np.hypot(tan[:, 0], tan[:, 1]), 1e-9)[:, None]
    nor = np.stack([-tan[:, 1], tan[:, 0]], axis=1)             # (left)
    nt = int(math.ceil(2.0 * hw / RACEWAY_ACROSS_M)) + 1
    offs = np.linspace(-hw, hw, nt)
    pts = full[:, None, :] + offs[None, :, None] * nor[:, None, :]
    la, lo, _ = frame.enu_to_geodetic(pts[..., 0].ravel(), pts[..., 1].ravel(), 0.0)
    z = np.asarray(elev(la, lo), np.float64).reshape(len(full), nt)
    zs = np.stack([smooth_profile(z[:, j], step, sigma) for j in range(nt)], axis=1)
    return line, np.arange(k) * step, offs, zs[len(head):len(head) + k]


def raceway_ground(e, n, base, zones, margin=None):
    """base (the ground at points e, n) as the raceways' smoothed surfaces (zones: [(line, stations, offsets, surface)]
    from raceway_surface): each point by its nearest zone it lies on (station on the segment, offset within), all of
    it on the carriageway, fading out over margin (UNGRADED_MARGIN_M) to base at its edge. A point on none keeps base."""
    margin = UNGRADED_MARGIN_M if margin is None else float(margin)
    e = np.asarray(e, np.float64)
    n = np.asarray(n, np.float64)
    out = np.array(base, np.float64, copy=True)
    if not len(e) or not zones:
        return out
    import fast
    fast.require(164, "polyline_project")
    pts = np.ascontiguousarray(np.stack([e, n], axis=1))
    best = np.full(len(e), np.inf)
    add = np.zeros(len(e))
    ex0, ex1, ny0, ny1 = float(e.min()), float(e.max()), float(n.min()), float(n.max())
    for line, st, offs, corr in zones:
        hw = float(offs[-1])
        if line[:, 0].min() - hw > ex1 or line[:, 0].max() + hw < ex0 or                 line[:, 1].min() - hw > ny1 or line[:, 1].max() + hw < ny0:
            continue
        station, _near, _tan, off, dist = fast._ef.polyline_project(np.ascontiguousarray(line, np.float64), pts)
        station, off, dist = np.asarray(station), np.asarray(off), np.asarray(dist)
        ok = (station >= 0.0) & (station <= st[-1]) & (np.abs(off) <= hw) & (dist < best)
        if not ok.any():
            continue
        idx = np.flatnonzero(ok)
        fs = np.clip(station[idx] / max(float(st[1] - st[0]), 1e-9), 0.0, len(st) - 1.0)
        i = np.minimum(np.floor(fs).astype(np.int64), len(st) - 2)
        a = fs - i
        ft = np.clip((off[idx] - offs[0]) / max(float(offs[1] - offs[0]), 1e-9), 0.0, len(offs) - 1.0)
        j = np.minimum(np.floor(ft).astype(np.int64), len(offs) - 2)
        b = ft - j
        c = (corr[i, j] * (1 - a) * (1 - b) + corr[i + 1, j] * a * (1 - b) + corr[i, j + 1] * (1 - a) * b
             + corr[i + 1, j + 1] * a * b)
        u = np.clip((hw - np.abs(off[idx])) / max(margin, 1e-9), 0.0, 1.0)
        add[idx] = (c - out[idx]) * u * u * (3 - 2 * u)
        best[idx] = dist[idx]
    return out + add


class Plan:
    """One road's grading: vertices (n, 2) in the frame's ENU metres, their target heights, core half width; abut: does
    it lead onto a bridge at its (first, last) vertex (grade_points)."""
    __slots__ = ("xy", "z", "half", "bbox", "abut", "cut", "road_half", "rid", "max_wall", "bank", "steep", "widen")

    def __init__(self, xy, z, half, abut=(False, False), cut=None, road_half=None):
        self.xy, self.z, self.half, self.abut = xy, z, float(half), (bool(abut[0]), bool(abut[1]))
        # (n - 1, 6) per segment, NaN where none: a cutting's wall offset from the centre line, its top's height and
        # the top's offset, on the left, then the right (_add_cuts)
        self.cut = cut
        self.road_half = float(road_half) if road_half is not None else float(half)    # the carriageway's half width
        self.rid = -1           # the road (map feature) it is part of: _plan_cell (each cell plans its own stretch of it)
        self.max_wall = WALL_MAX_H_M    # its cuttings' walls at most this tall (wall_max_h)
        self.bank = None                # (n,) its cross slope (m per m toward its left: a banked curve, bank) or None
        self.steep = False              # a mountain pass road's: steep sides (STEEP_RUN, roads._mountain_roads)
        self.widen = None               # (n,) m its core and carriageway are wider each side there (hairpin_cores) or None
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


_STENCILS = {}


def _stencil(r, sigma):
    """(offsets (19, 2) m, weights summing to 1): hexagonal rings out to r (the centre, 6 at r / 2, 12 at r), Gaussian
    weights of sigma."""
    got = _STENCILS.get((r, sigma))
    if got is None:
        pts = [(0.0, 0.0)]
        for ring, n in ((0.5, 6), (1.0, 12)):
            pts += [(r * ring * math.cos(2 * math.pi * i / n), r * ring * math.sin(2 * math.pi * i / n)) for i in range(n)]
        off = np.array(pts, np.float64)
        w = np.exp(-0.5 * (np.hypot(off[:, 0], off[:, 1]) / sigma) ** 2)
        got = _STENCILS[(r, sigma)] = (off, w / w.sum())
    return got


def field_heights(frame, elev, xy, r, sigma):
    """elev's Gaussian-weighted mean over the _stencil(r, sigma) around each point xy (n, 2) in the frame: one call."""
    off, wts = _stencil(r, sigma)
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    cloud = (xy[None, :, :] + off[:, None, :]).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(cloud[:, 0], cloud[:, 1], 0.0)
    z = np.asarray(elev(la, lo), np.float64).reshape(len(off), -1)
    return (wts[:, None] * z).sum(axis=0)


def plan_roads(frame, parts, elev, abut=None, ext=None, pins=None, cuts=True, coarse=False, obs=None, decks=None,
               rings=None):
    """plan_road for many (lonlat, spec) parts with one elev call (its cost is per call); abut: per part (start, end)
    flags, the ends leading onto a bridge (fill_abutment); ext: per part (before, after), the lines (k, 2) in the frame
    from its start / end outward along the part the road goes on in (Grader._extensions), or None: their ground is in
    the part's profile smoothing; pins: per part (start, end) heights its ends are held at, or None (Grader._pins: a
    road ending on a more smoothed one meets it at its height); obs(xy (m, 2), spec) -> measured heights (m,) of a
    part's samples (NaN where none) or None: they stand in for the DEM's (roadz.py, on coarse elevation: Grader._obs);
    decks: [(line (k, 2) in the frame, half width)] crossing over the roads, their ground read beside them
    (deck_capped; not on coarse elevation); rings: per part, True for a mapped roundabout's ring (roads.is_ring: not
    banked, RING_LEVEL_ON). Pure given elev and obs."""
    if not parts:
        return []
    ll = np.concatenate([lonlat for lonlat, _ in parts])
    E, N, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    onto_xy = np.zeros((0, 2))          # the part ends leading onto a bridge (fill_abutment's)
    if abut is not None and ABUT_FILL_ON:
        last = np.cumsum([len(lonlat) for lonlat, _ in parts]) - 1
        onto_xy = np.array([(E[i], N[i]) for j, (lonlat, _) in enumerate(parts) if len(lonlat)
                            for at_end, i in ((0, last[j] - len(lonlat) + 1), (1, last[j])) if abut[j][at_end]],
                           np.float64).reshape(-1, 2)
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
        side, bank_side, onto = [], [], []
        for out in (ext[j] if ext is not None else (None, None)):
            if out is None or len(out) < 2:
                side.append(np.zeros((0, 2)))
                bank_side.append(np.zeros((0, 2)))
                onto.append(False)
            else:                       # (from the node outward; the node is the part's own end sample)
                bank_side.append(resample(np.asarray(out, np.float64), step)[0][1:])
                # Extra bank geometry must not change longitudinal DEM sampling
                # or the bridge solver's established approach/underpass profiles.
                reach = 3.0 * profile_sigma(spec, coarse) + PROFILE_STEP
                arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(out, axis=0), axis=1))]
                if arc[-1] > reach + 1e-8:
                    out = np.vstack([np.asarray(out)[arc < reach], _point_along(out, reach)])
                    onto.append(False)
                else:                   # (does it end where another part leads onto a bridge?)
                    onto.append(bool(len(onto_xy)) and
                                float(np.hypot(*(onto_xy - np.asarray(out, np.float64)[-1]).T).min()) <= CHAIN_JOIN_M)
                side.append(resample(np.asarray(out, np.float64), step)[0][1:])
        prof.append((xy, side[0][::-1], side[1], step, bank_side[0][::-1], bank_side[1], onto))
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
        xy, before, after, step, bank_before, bank_after, onto = pr
        nb, na = len(before), len(after)
        zx = zall[k:k + nb + len(xy) + na]
        k += nb + len(xy) + na
        if decks and not coarse:        # (the part's samples and its extensions', smoothed together)
            zx = deck_capped(np.concatenate([before, xy, after]), zx, decks)
        if obs is not None:
            o = obs(xy, spec)
            if o is not None and len(o) == len(xy) and np.isfinite(o).any():
                zx = zx.copy()
                zx[nb:nb + len(xy)] = np.where(np.isfinite(o), o, zx[nb:nb + len(xy)])
        ab = (False, False)
        if abut is not None and ABUT_FILL_ON:
            ab = (bool(abut[j][0]), bool(abut[j][1]))
            if ab[0] and nb:            # (no extension past an end onto a bridge)
                zx, nb = zx[nb:], 0
            if ab[1] and na:
                zx, na = zx[:len(zx) - na], 0
            # Filled with the road it goes on in past its other end: a part split at a junction by the bridge alone had
            # too few samples for the grade, and its end stayed at the slope's foot, 6 m under the road (Kurna - Tuhala
            # over Tallinna ringtee, 59.3364 N 24.8239 E, GitHub #684). An extension running onto a bridge's end (the
            # part on the other side of that junction) is filled the same, so the two parts meet.
            for at_end, n_ext in ((False, nb), (True, na)):
                if ab[int(at_end)] or (n_ext and onto[int(at_end)]):
                    zx = fill_abutment(zx, at_end, step)
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
        p = _plan(xy, zx, spec, ab, (nb, na), step, bank_xy=bank_xy,
                  bank_ext=(len(bank_before), len(bank_after)),
                  banked=not (RING_LEVEL_ON and rings is not None and rings[j]), coarse=coarse)
        # (road roughness, PR #639 by subformatdnb: off unless BNE_BUMPS > 0; cached plans keep whatever they were
        # planned with, so a change takes effect on places planned after it)
        out.append(bumps.apply(frame, p, spec, ab) if bumps.SCALE > 0 else p)
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
    la, lo = enu_to_ll_dense(frame, allp[:, 0], allp[:, 1])
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


LL_GRID_M = 32.0       # m: enu_to_ll_dense's node spacing (the frame's conversion is smooth to < 0.1 mm over a cell)


def enu_to_ll_dense(frame, x, y, step=LL_GRID_M):
    """(lat, lon) of many points (x, y) in frame: the frame's exact conversion at the nodes of a step-m grid over
    them, interpolated between the nodes (earthfast grid_bilinear2); a scattered few (more nodes than a quarter of
    the points) converted one by one. _add_cuts' millions of probes: their conversion was most of its time."""
    import fast
    fast.require(158, "grid_bilinear2")
    x = np.ascontiguousarray(x, np.float64)
    y = np.ascontiguousarray(y, np.float64)
    if len(x) == 0:
        return np.zeros(0), np.zeros(0)
    x0 = math.floor(float(x.min()) / step) * step
    y0 = math.floor(float(y.min()) / step) * step
    nx = int(math.ceil((float(x.max()) - x0) / step)) + 2
    ny = int(math.ceil((float(y.max()) - y0) / step)) + 2
    if nx * ny * 4 > len(x):
        la, lo, _ = frame.enu_to_geodetic(x, y, 0.0)
        return np.asarray(la, np.float64), np.asarray(lo, np.float64)
    gx, gy = np.meshgrid(x0 + step * np.arange(nx), y0 + step * np.arange(ny))
    gla, glo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), 0.0)
    return fast._ef.grid_bilinear2(np.asarray(gla, np.float64).reshape(ny, nx),
                                   np.asarray(glo, np.float64).reshape(ny, nx), x0, y0, step, x, y)


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


def profile_sigma(spec, coarse=False):
    """m: the profile smoothing of a road of this spec (coarse: on the coarse global elevation, plan_roads)."""
    if spec.kind not in roadsmod.BUILT:
        if coarse:
            return max(DIRT_SIGMA_COARSE_M, DIRT_SIGMA_M)
        if spec.cls in DIRT_SOFT_CLASSES:
            return DIRT_SIGMA_M          # a main dirt road stays as smooth as it was (dirt_bumps' "main" ones too)
        return DIRT_SIGMA_MINOR_M        # a minor one keeps the ground's own bumps
    if spec.cls in MAJOR_CLASSES and not getattr(spec, "link", False):
        return max(SIGMA_M, SIGMA_MAJOR_M)
    return max(SIGMA_M, CLASS_SIGMA_M.get(spec.cls, SIGMA_M)) if not getattr(spec, "link", False) else SIGMA_M


DIRT_ROUGH = float(os.environ.get("BNE_DIRT_BUMP", "1.0"))    # strength of a minor dirt road's damage (1: as below)
DIRT_MAJOR_SHARE = float(os.environ.get("BNE_DIRT_MAJOR", "0.15"))  # a MAIN dirt road is this much of it (softer)
# which dirt roads count as "main" (the ones kept soft): the classes a country road is signed as, not just the
# motorway ones (a mapped `secondary` gravel road is a main road where you drive it at 80 km/h)
DIRT_SOFT_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary"}
# a forest track (highway=track) is worse than any of them: "you cannot even get up to speed on it" (the user, 2026-11-02)
DIRT_TRACK_SHARE = float(os.environ.get("BNE_DIRT_TRACK", "1.3"))
DIRT_TRACK_WAVE_SHARE = float(os.environ.get("BNE_DIRT_TRACK_WAVE", "0.5"))   # ... but less of the soft undulation:
                                                                              # waves read as a pattern, holes do not
DIRT_VERTEX_STEP = 2.0          # m between a dirt road's plan vertices (4 m smoothed a pothole away)
DIRT_UNDULATION_M = 0.02        # m: centimetre-scale wear, not waves along the whole carriageway
DIRT_UNDULATION_WAVES = ((10.0, 1.0), (6.0, 0.8))
DIRT_POTHOLE_EVERY_M = 5.0      # m of road per pothole, on average
DIRT_POTHOLE_W_M = (3.0, 6.0)   # m: a pothole's width
DIRT_POTHOLE_D_M = (0.025, 0.045)  # m: its depth (before the track/main share and aggregate bound)
DIRT_PATCH_EVERY_M = 10.0       # m of road per broken patch (a step of a few cm with soft edges)
DIRT_PATCH_W_M = (3.0, 7.0)
DIRT_PATCH_D_M = (0.015, 0.030)
DIRT_PIT_EVERY_M = 150.0        # m of road per deep pit
DIRT_PIT_W_M = (2.0, 4.5)
DIRT_PIT_D_M = (0.04, 0.07)
DIRT_DEPTH_MAX_M = 0.08        # total synthetic damage, not per hole: overlapping/track-scaled pits must stay local
DIRT_RISE_MAX_M = 0.025        # removing the damage's mean must not turn the ground between holes into hills
DIRT_STEP_MAX_M = float(os.environ.get("BNE_DIRT_STEP", "0.35"))   # m: steepest artificial damage change per 2 m
DIRT_BASE_M = 25.0             # m: the bump pattern's own long waves are removed over this length, so a road keeps
                               # the terrain's course instead of sinking into it (the roads sat in a ditch, further in
                               # the worse it got: the damage's mean pulled them down along their length)
DIRT_TAPER_M = 22.0            # m at each end of a dirt road where its damage fades out: a junction meets the road it
                               # joins at that road's height, not on a berm of its own potholes


def dirt_taper(s_, length):
    """(n,) 0 at a dirt part's two ends (s_ from 0 to length) rising smoothly to 1 within DIRT_TAPER_M (half a short
    part's length): its damage and lean fade out where it meets another road, whatever its length."""
    s_ = np.asarray(s_, np.float64)
    if length <= 0.0 or DIRT_TAPER_M <= 0.0:
        return np.zeros(len(s_)) if length <= 0.0 else np.ones(len(s_))
    tap = np.clip(np.minimum(s_, length - s_) / min(DIRT_TAPER_M, 0.5 * length), 0.0, 1.0)
    return tap * tap * (3.0 - 2.0 * tap)


def dirt_bumps(xy, spec, n, ext=(0, 0), step=PROFILE_STEP):
    """What a minor dirt road is really like, one value per profile sample (n samples, ext: how many lie past the
    part's ends): a soft undulation plus discrete damage -- potholes, broken patches (a step with soft edges) and the
    odd deep pit -- placed from the road's own position, so every process builds the same ones. None for a built
    road. The sizes are metres; BNE_DIRT_BUMP scales the lot (1: as here, 0: off). DEM smoothing extensions get zero
    damage: neighbouring samples must not change this part's pattern or move its junction taper."""
    if spec.kind in roadsmod.BUILT or DIRT_ROUGH <= 0.0:
        return None
    if spec.cls == "track":
        scale, wave = DIRT_TRACK_SHARE, DIRT_TRACK_WAVE_SHARE
    elif spec.cls in DIRT_SOFT_CLASSES:
        scale = wave = DIRT_MAJOR_SHARE
    else:
        scale = wave = 1.0
    if scale <= 0.0 and wave <= 0.0:
        return None
    xy = np.asarray(xy, np.float64)[:, :2]
    n_core = n - ext[0] - ext[1]
    if len(xy) < 2 or n_core <= 0:
        return None
    n = n_core
    s_ = np.arange(n, dtype=np.float64) * float(step)
    seed = (int(round(float(xy[0, 0]) * 7.0)) * 7919 + int(round(float(xy[0, 1]) * 7.0)) * 104729) & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    share2 = 0.0
    for knot_m, wshare in DIRT_UNDULATION_WAVES:           # the road's own soft rise and fall
        if s_[-1] < knot_m:
            continue
        knots = rng.normal(0.0, 1.0, int(s_[-1] / knot_m) + 3)
        t = s_ / knot_m
        i0 = np.clip(np.floor(t).astype(int), 0, len(knots) - 2)
        f = t - i0
        f = f * f * (3.0 - 2.0 * f)
        out += wshare * (knots[i0] * (1.0 - f) + knots[i0 + 1] * f)
        share2 += wshare * wshare
    if float(out.std()) > 1e-9:
        out *= DIRT_UNDULATION_M * wave / math.sqrt(share2)

    i_of = lambda pos: pos / float(step)    # (sample index of a distance along the part)

    def add(c, half, fn):                                  # a feature's few metres only (it is ~0 farther out):
        i0 = max(0, int(math.floor(i_of(c - half))))       # the whole road per pothole was O(length^2)
        i1 = min(n, int(math.ceil(i_of(c + half))) + 1)
        if i1 > i0:
            out[i0:i1] += fn(s_[i0:i1])

    def bowl(c, w, d):                                     # a rounded hole: full depth in the middle, soft rim
        add(c, 1.5 * w, lambda x: -d * np.exp(-((x - c) / (0.45 * w)) ** 2))

    length = float(s_[-1]) if len(s_) else 0.0
    if length > 10.0:
        for _ in range(int(rng.poisson(max(1.0, length / DIRT_POTHOLE_EVERY_M)))):
            bowl(float(rng.uniform(0.0, length)), float(rng.uniform(*DIRT_POTHOLE_W_M)),
                 float(rng.uniform(*DIRT_POTHOLE_D_M)) * DIRT_ROUGH * scale)
        for _ in range(int(rng.poisson(max(1.0, length / DIRT_PATCH_EVERY_M)))):   # broken patch: a step, soft edges
            c = float(rng.uniform(0.0, length))
            w = float(rng.uniform(*DIRT_PATCH_W_M))
            d = float(rng.uniform(*DIRT_PATCH_D_M)) * DIRT_ROUGH * scale * (1.0 if rng.random() < 0.5 else -1.0)
            edge = 0.35 * w
            add(c, 0.5 * w + 4.0 * edge, lambda x, c=c, w=w, d=d, edge=edge:
                d * 0.5 * (np.tanh((x - (c - 0.5 * w)) / edge) - np.tanh((x - (c + 0.5 * w)) / edge)))
        for _ in range(int(rng.poisson(max(0.3, length / DIRT_PIT_EVERY_M)))):      # the odd deep pit
            bowl(float(rng.uniform(0.0, length)), float(rng.uniform(*DIRT_PIT_W_M)),
                 float(rng.uniform(*DIRT_PIT_D_M)) * DIRT_ROUGH * scale)
        if DIRT_BASE_M > 0.0 and length > 1.5 * DIRT_BASE_M:   # keep the terrain's own course: no net rise or fall
            k = max(1, int(DIRT_BASE_M / max(step, 1e-6)))
            pad = k // 2
            padded = np.r_[np.full(pad, out[0]), out, np.full(k - pad - 1, out[-1])]
            out = out - np.convolve(padded, np.ones(k) / k, mode="valid")[:len(out)]
    # Tracks still get more damage, but not arbitrarily deep overlapping holes. Main roads keep their smaller
    # share; BNE_DIRT_BUMP still scales the bounds. Bound after removing the mean, which can itself raise berms.
    strength = DIRT_ROUGH * min(1.0, max(scale, wave))
    out = np.clip(out, -DIRT_DEPTH_MAX_M * strength, DIRT_RISE_MAX_M * strength)
    # The asymmetric bounds can leave a net depression. Balance by reducing the stronger side only, so neither
    # the depth nor the rise can exceed its bound again (and a zero sample never turns into a raised berm).
    if DIRT_BASE_M > 0.0 and length > 1.5 * DIRT_BASE_M:
        positive, negative = out > 0.0, out < 0.0
        up, down = float(out[positive].sum()), -float(out[negative].sum())
        if down > up:
            out[negative] *= up / down
        elif up > down:
            out[positive] *= down / up
    out *= dirt_taper(s_, length)       # (nothing right at a junction, short parts too: that road sets the height)
    return np.pad(out, ext)


def _plan(xy, z, spec, abut=(False, False), ext=(0, 0), step=PROFILE_STEP, bank_xy=None, bank_ext=None, banked=True,
          coarse=False):
    """ext: (before, after), samples of z past the part's ends (plan_roads): smoothed with it, then dropped. step: m
    between the samples. bank_xy: the matching extended geometry, trimmed after the curvature/runoff pass. banked
    False: level across (a roundabout's ring)."""
    built = spec.kind in roadsmod.BUILT
    zs = smooth_profile(median_profile(z, step, MEDIAN_M if built else DIRT_MEDIAN_M), step,
                        profile_sigma(spec, coarse))[ext[0]:len(z) - ext[1]]
    bumps = dirt_bumps(xy, spec, len(z), ext, step)
    if bumps is not None:                       # (not smoothed again: a pothole's edge is sharp, that is the point)
        damage = bumps[ext[0]:len(z) - ext[1]].copy()
        cap = DIRT_STEP_MAX_M
        if spec.cls == "track":
            cap *= 1.6
        cap *= step / PROFILE_STEP
        if cap > 0.0 and len(damage) > 1:
            # Limit the artificial damage alone, not the real climb. Integrating clipped TOTAL height differences
            # then smoothing the lost height back over 40 m made local pits into long hills/dips. Two local sweeps
            # bound neighbouring damage values without accumulating an offset down the road. Keep the ends pinned.
            # (native: dirtsteps.rs, the same two sweeps; Python loops per sample before)
            import fast
            fast.require(157, "dirt_step_clamp")
            damage = np.asarray(fast._ef.dirt_step_clamp(np.ascontiguousarray(damage, np.float64), float(cap)))
        zs = zs + damage
    # thin to VERTEX_STEP (the profile stays smooth between: linear in the grading pass); a minor dirt road keeps
    # every sample, or its potholes would be smoothed away by the linear stretches between the vertices
    minor_dirt = bumps is not None and spec.cls not in DIRT_SOFT_CLASSES
    vstep = DIRT_VERTEX_STEP if minor_dirt else VERTEX_STEP
    every = max(1, int(round(vstep / PROFILE_STEP)))
    keep = np.r_[np.arange(0, len(xy) - 1, every), len(xy) - 1]
    # the carriageway as a city street would have it (never from the land cover), or the measured one
    # (a mountain pass road its own: no town on a pass, roads._mountain_roads)
    width = max(spec.width, roadsmod.carriageway(spec.cls, spec.kind, True, None, spec.oneway)) \
        if built and not getattr(spec, "mountain", False) else spec.width
    if spec.median:             # a divided road's carriageway: never over the other one
        width = max(spec.width, roadsmod.dual_cap(spec.median, width))
    plan = Plan(xy[keep], zs[keep], width / 2 + shoulder(spec), abut, road_half=width / 2)
    plan.max_wall = wall_max_h(spec)
    plan.steep = bool(getattr(spec, "mountain", False))
    bext = ext if bank_ext is None else bank_ext
    b = bank(xy if bank_xy is None else bank_xy, spec, abut, bext) if banked else None
    if b is not None:
        if bank_xy is not None:
            b = b[bext[0]:len(bank_xy) - bext[1]]
        if not built:           # a dirt road's lean: level across where it meets another road (after the trim: the
            sp = np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(xy, np.float64)[:, :2], axis=0).T))]  # ramp
            b = b * dirt_taper(sp, float(sp[-1]))                                      # was trimmed away with ext)
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
# ... but not a mapped roundabout's ring (roads.is_ring): the Jüri roundabout of Tallinna ringtee (59.3535 N 24.8820 E,
# trunk, about 190 m across) was banked 8 % into the ring as a trunk's curve; the real one falls 2.4-2.9 % outward (the
# Estonian lidar). Rings are graded level across. BNE_RING_LEVEL=off: banked as before.
RING_LEVEL_ON = os.environ.get("BNE_RING_LEVEL", "on") != "off"


DIRT_BANK_M = float(os.environ.get("BNE_DIRT_BANK", "0.015"))   # cross slope a worn dirt road leans to (m per m)
DIRT_BANK_EDGE_M = 0.04         # maximum synthetic lift/drop of either edge, also on wide gravel roads
DIRT_BANK_WAVES = ((27.0, 1.0), (13.0, 0.6), (7.0, 0.35))      # how often the lean changes sides (m)


def dirt_bank(xy, spec, ext=(0, 0), step=PROFILE_STEP):
    """The cross slope a dirt road leans to along itself (m per m toward its left): now the left side is low, now the
    right, at a few scales -- a pothole on one side of the road rather than a wave across the whole of it. Seeded by the
    road's own position, so every process builds the same. None for a built road or BNE_DIRT_BANK 0."""
    if spec.kind in roadsmod.BUILT or DIRT_BANK_M <= 0.0:
        return None
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 3:
        return None
    s_ = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    seed = (int(round(float(xy[0, 0]) * 11.0)) * 2654435761 + int(round(float(xy[0, 1]) * 11.0)) * 40503) & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    out = np.zeros(len(xy))
    norm = 0.0
    for size, share in DIRT_BANK_WAVES:
        if s_[-1] < size:
            continue
        knots = rng.uniform(-1.0, 1.0, int(s_[-1] / size) + 3)
        t = s_ / size
        i0 = np.clip(np.floor(t).astype(int), 0, len(knots) - 2)
        f = t - i0
        f = f * f * (3.0 - 2.0 * f)
        out += share * (knots[i0] * (1.0 - f) + knots[i0 + 1] * f)
        norm += share * share
    if float(out.std()) > 1e-9:
        out *= DIRT_BANK_M / math.sqrt(norm)
    # A slope-only limit grew with road width: a 3.5 m track at Mering kept ~83 cm edge-to-edge waves even after
    # bounding the centreline damage. Bound the edge displacement itself, before the junction fade.
    edge_slope = DIRT_BANK_EDGE_M / max(float(spec.width) / 2.0, 1e-6)
    out = np.clip(out, -edge_slope, edge_slope)
    if ext[0] or ext[1]:                      # a junction is level across: the lean fades out at the ends
        n = len(out)
        k = max(1, int(min(n, 12)))
        ramp = np.ones(n)
        ramp[:k] = np.linspace(0.0, 1.0, k)
        ramp[-k:] = np.linspace(1.0, 0.0, k)
        out *= ramp
    return out


def bank(xy, spec, abut=(False, False), ext=(0, 0)):
    """(n,) the cross slope of a road part's centreline xy (n, 2) (m per m toward its left), or None (not banked)."""
    if spec.kind not in roadsmod.BUILT:
        return dirt_bank(xy, spec, ext)
    if not BANK_ON or spec.cls not in BANK_DESIGN or len(xy) < 3:
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


def couple_neighbours(plans, specs, trend):
    """NEIGHBOUR_* above: plans (in place) running alongside a higher- or equal-ranked one take its height plus the ground's
    fall between them. specs: their Specs; trend(xy (n, 2)) -> the far smoother surface's heights there. Plans leading
    onto a bridge (abut) are left as they are, and are no neighbour to follow either."""
    rank = np.array([road_rank(sp) for sp in specs])
    order = sorted(range(len(plans)), key=lambda j: (-rank[j], j))
    reach = NEIGHBOUR_GAP_M
    boxes = np.array([[p.xy[:, 0].min(), p.xy[:, 1].min(), p.xy[:, 0].max(), p.xy[:, 1].max()] for p in plans],
                     np.float64).reshape(-1, 4)
    cos_par = math.cos(math.radians(NEIGHBOUR_PARALLEL_DEG))
    ends = np.array([[p.xy[0, :2], p.xy[-1, :2]] for p in plans], np.float64).reshape(-1, 2, 2)
    for j in order:
        p = plans[j]
        if len(p.xy) < 2 or p.abut[0] or p.abut[1]:
            continue
        P = np.asarray(p.xy, np.float64)[:, :2]
        g = np.gradient(P, axis=0)
        tdir = g / np.maximum(np.hypot(g[:, 0], g[:, 1]), 1e-9)[:, None]
        pad = reach + p.road_half + 20.0
        cand = np.flatnonzero((rank >= rank[j]) & (boxes[:, 0] <= boxes[j, 2] + pad) & (boxes[:, 2] >= boxes[j, 0] - pad)
                              & (boxes[:, 1] <= boxes[j, 3] + pad) & (boxes[:, 3] >= boxes[j, 1] - pad))
        best_w = np.zeros(len(P))
        best_q = np.zeros((len(P), 2))
        best_zq = np.zeros(len(P))
        for k in cand:
            q = plans[k]
            if k == j or len(q.xy) < 2 or q.abut[0] or q.abut[1] or q.rid == p.rid:
                continue
            # (roads that meet at a node are a junction, snap_junctions': a road's two parts either side of a junction, a
            # slip road leaving its motorway)
            if np.hypot(*(ends[j][:, None, :] - ends[k][None, :, :]).reshape(-1, 2).T).min() <= CHAIN_JOIN_M:
                continue
            a, b = q.xy[:-1, :2], q.xy[1:, :2]
            d = b - a
            ll = np.maximum((d * d).sum(1), 1e-12)
            t = np.clip(((P[:, None, 0] - a[None, :, 0]) * d[None, :, 0] + (P[:, None, 1] - a[None, :, 1]) * d[None, :, 1])
                        / ll[None, :], 0.0, 1.0)
            qx = a[None, :, 0] + t * d[None, :, 0]
            qy = a[None, :, 1] + t * d[None, :, 1]
            dist = np.hypot(qx - P[:, None, 0], qy - P[:, None, 1])
            si = dist.argmin(1)
            r_ = np.arange(len(P))
            dmin = dist[r_, si]
            gap = dmin - q.road_half - p.road_half
            sdir = d[si] / np.sqrt(ll[si])[:, None]
            par = np.abs((tdir * sdir).sum(1)) >= cos_par
            u = np.clip((gap - NEIGHBOUR_FULL_M) / max(NEIGHBOUR_GAP_M - NEIGHBOUR_FULL_M, 1e-9), 0.0, 1.0)
            w = (1.0 - u * u * (3.0 - 2.0 * u)) * par * (gap <= NEIGHBOUR_GAP_M)
            w *= 1.0 if rank[k] > rank[j] else 0.5
            zq = q.z[si] + (q.z[si + 1] - q.z[si]) * t[r_, si]
            take = w > best_w
            best_w[take] = w[take]
            best_q[take] = np.c_[qx[r_, si], qy[r_, si]][take]
            best_zq[take] = zq[take]
        on = best_w > 0
        if not on.any():
            continue
        idx = np.flatnonzero(on)
        f = np.asarray(trend(np.vstack([P[idx], best_q[idx]])), np.float64)
        target = best_zq[idx] + (f[:len(idx)] - f[len(idx):])
        diff = target - p.z[idx]
        keep = np.abs(diff) <= NEIGHBOUR_MAX_M
        delta = np.zeros(len(P))
        wt = np.zeros(len(P))
        delta[idx[keep]] = best_w[idx[keep]] * diff[keep]
        wt[idx[keep]] = best_w[idx[keep]]
        if not wt.any():
            continue
        # smoothed along the road (a Gaussian over arc length), as a fraction of the coupled share: it fades out where the
        # neighbour ends instead of stepping
        s_ = np.r_[0.0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))]
        kern = np.exp(-0.5 * ((s_[:, None] - s_[None, :]) / NEIGHBOUR_SMOOTH_M) ** 2)
        kern /= kern.sum(1, keepdims=True)
        p.z = np.asarray(p.z, np.float64) + kern @ delta


def snap_junctions(plans, specs):
    """A side road's end at a junction takes the height of the road it joins, eased in over
    JUNCTION_TAPER_M along the side road (each road is smoothed on its own: a side road met the main road up to a metre
    off, blended over BLEND_R only: a bump at every turn-off). Plans are changed in place, the higher ranks first, so
    a residential street joining a tertiary road meets it at its already adjusted height. With the coarse easing
    (BNE_COARSE_EASE=1) also the crossings (GitHub #414, Perm: service roads crossing a primary at a shared node stood
    as 4-5 m walls across it): where a road runs over a higher-ranked road's carriageway away from its own ends, it takes
    that road's height there. plans: [Plan], specs: their Specs. In Rust (earthfast.snap_junctions: 15 CPU-s of a
    teleport's road plans in Python, 2026-10-30)."""
    if not plans:
        return
    rank = np.array([road_rank(sp) for sp in specs], np.int64)
    js = _Junctions(plans)
    changed = js.snap(rank)
    # (a road joining one that the hold changed meets it at its new height: snapped again)
    if _hold_junction_corridors(js, rank, changed):
        _hold_junction_corridors(js, rank, js.snap(rank))
    js.store()


class _Junctions:
    """snap_junctions' plans as earthfast's flat arrays, made once for all its passes (each pass made them again:
    0.2-0.4 CPU-s per 16 road tiles), their heights kept here between the passes; store() puts them back on the plans
    changed."""

    def __init__(self, plans):
        import fast
        fast.require(144, "the junctions' heights")
        self.plans = plans
        n = [len(p.xy) for p in plans]
        self.off = np.concatenate([[0], np.cumsum(n)]).astype(np.int64)
        self.xy = np.ascontiguousarray(np.concatenate([np.asarray(p.xy, np.float64)[:, :2] for p in plans])
                                       .reshape(-1, 2))
        self.z = np.ascontiguousarray(np.concatenate([np.asarray(p.z, np.float64) for p in plans]))
        self.half = np.array([p.road_half for p in plans], np.float64)
        self.abut = np.array([[bool(p.abut[0]), bool(p.abut[1])] for p in plans], np.uint8).reshape(-1, 2)
        self.boxes = np.ascontiguousarray(np.array([q.bbox for q in plans], np.float64).reshape(-1, 4))
        self.touched = np.zeros(len(plans), bool)

    def snap(self, rank):
        """One pass of earthfast.snap_junctions; the plans it changed (bool array)."""
        import fast
        self.z, changed = fast._ef.snap_junctions(self.xy, self.z, self.off, rank, self.half, self.abut, self.boxes,
                                                  JUNCTION_REACH_M, JUNCTION_END_M, JUNCTION_TAPER_M, JUNCTION_MAX_M,
                                                  bool(COARSE_EASE_ON))
        changed = np.asarray(changed, bool)
        self.touched |= changed
        return changed

    def store(self):
        off = self.off
        for j in np.flatnonzero(self.touched):
            self.plans[j].z = self.z[off[j]:off[j + 1]].copy()


def _hold_junction_corridors(js, rank, changed):
    """A side road's end snapped to a wide road (snap_junctions) holds that road's height, at the nearest point of it,
    across its carriageway and shoulder (JUNCTION_HOLD_PAD_M), then returns to the profile the snap gave it at most
    JUNCTION_HOLD_GRADE steep. The snap eases from the end's first vertex, so on a 13 m trunk the next vertices lay on
    its lanes already 45-90 cm low, and the ground under the lane was blended down to them: a trench across the lane at
    a track's junction (48.83116, 29.33644, the user 2026-11-02; a hole the car took at speed). Only ends the snap
    changed and the road itself reaches (not a bridge abutment, not a gap the snap left as it was); a road shorter than
    three corridors, or one that does not meet its own profile again within its first half, is left as the snap made
    it. A second call changes nothing (the held heights and the limited grade stay). js: _Junctions (its heights changed
    in place); True when any was. In Rust (earthfast.hold_junction_corridors: 1.2 CPU-s per 16 road tiles in numpy)."""
    import fast
    if not changed.any():
        return False
    js.z, held = fast._ef.hold_junction_corridors(js.xy, js.z, js.off, rank, js.half, js.abut, js.boxes,
                                                  np.ascontiguousarray(changed, np.uint8), JUNCTION_REACH_M,
                                                  JUNCTION_HOLD_PAD_M, JUNCTION_HOLD_GRADE)
    held = np.asarray(held, bool)
    js.touched |= held
    return bool(held.any())


# Junction pads (the user, 2026-10-29: "lower ranked roads impacting higher ranked ones ... side streets coming in from a
# sloped hill create bumps and likewise dips on the lower side"; "roads facing each other having more influence ... especially
# when the facing roads are the same class"). grade_points blends every road's flat cross-section by distance alone, so
# inside a junction each road pulls the ground toward its own surface: where they disagree (equal ranks, which never
# snap; a road crossing another with no node; a crossroads on a slope, which no set of flat cross-sections can meet) the
# middle of the junction is a compromise, a bump on one side and a dip on the other. Weighting the blend by rank only
# moves the compromise (and needs the Rust loop changed); here every road is made to agree instead, so the blend's
# weights no longer matter there:
#   - each branch at a node (a road's end; or a road crossing another's centreline away from its ends: two branches)
#     gets an authority, its class's weight (PAD_CLASS_W) times its "through" factor (a branch with another one facing
#     it, PAD_FACE_DEG: a through road; a T's stem keeps PAD_STEM_F), normalised by the node's strongest, ** PAD_GAMMA;
#   - one plane is fitted to the branches' heights at the node and their grades leaving it, weighted by authority (and
#     pulled level by PAD_RIDGE): a T on a hillside is the main road's own grade and height, with next to no tilt
#     across it; a crossroads of equal roads a plane that matches both their grades;
#   - over the pad (each branch out to the other branches' cores + PAD_LANDING_M) every road lies on that plane: its
#     centreline height and its cross slope (Plan.bank: grade_points tilts its core by it), then back to its own
#     profile and cross slope over JUNCTION_TAPER_M / PAD_BANK_TAPER_M.
# The strongest road is all but unchanged (the plane is its own surface); the others warp to it, as real junctions are
# built. BNE_JUNCTION_PAD=1: on (off by default: NEIGHBOUR_ON).
PAD_ON = os.environ.get("BNE_JUNCTION_PAD", "0") == "1"
PAD_CLASS_W = {"motorway": 1.0, "trunk": 0.9, "primary": 0.8, "secondary": 0.65, "tertiary": 0.5,
               "residential": 0.35, "unclassified": 0.35, "living_street": 0.3, "service": 0.2, "track": 0.15}
PAD_OTHER_W = 0.25          # a class not listed
PAD_LINK_F = 0.75           # a slip road: its class's weight times this
PAD_UNBUILT_F = 0.6         # a gravel / dirt road: times this
PAD_FACE_DEG = (120.0, 160.0)   # angle to the most opposite other branch: no through road under the first, fully at it
PAD_STEM_F = 0.35           # a branch with none facing it keeps this share of its class's weight
PAD_GAMMA = 2.0             # authority = (weight / the node's strongest) ** this
PAD_MIN = 0.02
PAD_FIT_POW = 2.0           # the plane fitted with the authorities ** this (the strongest road all but decides it)
PAD_GRADE_M = 15.0          # m along a branch its grade leaving the node is taken over
PAD_FLAT_W = 1.0            # how much a road wants its own cross section across it, against its grade along it
PAD_RIDGE = 0.05            # the plane's tilt pulled to level with this weight (an authority is at most 1)
PAD_MAX_TILT = 0.15         # never steeper (bad data)
PAD_LANDING_M = 3.0         # m past the other branches' cores the pad reaches ...
PAD_LANDING_MINOR_M = 0.5   # ... on a branch weighing less than PAD_LANDING_MIN_W (a track, a driveway)
PAD_LANDING_MIN_W = 0.3
PAD_MIN_SIN = 0.35          # (a branch at a shallow angle: its core reaches along the other at most 1 / this)
PAD_REACH_F = 0.4           # a pad reaches at most this share of the branch (a short part keeps the rest to climb)
PAD_BANK_TAPER_M = 15.0     # m the cross slope returns to the road's own over
PAD_TAPERS_M = (8.0, 12.0, 16.0, 24.0, 32.0)    # m: the shorter eases back to a road's own profile (pad's edge on)
PAD_SINK_M = 0.25           # m a road is let sink under (or stand over) its own profile on the ease, past the pad's step
PAD_CROSS_MAX_M = 1.5       # m: two roads crossing with no node further apart than this are not at grade (left)
PAD_MIN_DELTA_M = 0.01      # m: a smaller change is not made
PAD_GRID_M = 32.0           # m: the crossing search's buckets
PAD_MIN_PART_M = 20.0       # m: a branch shorter than this (to its part's end) is left out of the pad


def pad_class_w(spec):
    """A road's own weight at a junction by its class (PAD_CLASS_W), less for a slip road or an unbuilt one. Pure."""
    cls = spec.cls or ""
    w = PAD_CLASS_W.get(cls.removesuffix("_link"), PAD_OTHER_W)
    if cls.endswith("_link") or getattr(spec, "link", False):
        w *= PAD_LINK_F
    if spec.kind not in roadsmod.BUILT:
        w *= PAD_UNBUILT_F
    return w


def pad_authority(weights, dirs):
    """The authority (0, 1] of each branch at one node: weights (n,) by class (pad_class_w), dirs (n, 2) the way each
    leaves the node. A branch with another facing it (PAD_FACE_DEG; fully only one of its own weight) is through, a stem
    keeps PAD_STEM_F of its weight;
    then each over the node's strongest, ** PAD_GAMMA. Equal through roads all 1. Pure."""
    w = np.asarray(weights, np.float64)
    n = len(w)
    if n < 2:
        return np.ones(n)
    d = np.asarray(dirs, np.float64).reshape(n, 2)
    d = d / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-12)[:, None]
    cos = np.clip(d @ d.T, -1.0, 1.0)
    np.fill_diagonal(cos, 1.0)
    lo, hi = PAD_FACE_DEG
    u = np.clip((np.degrees(np.arccos(cos)) - lo) / (hi - lo), 0.0, 1.0)
    # (facing: by angle, and fully only by a branch of its own weight: a street across a primary from a driveway is
    # hardly a through road)
    sim = np.minimum(w[:, None], w[None, :]) / np.maximum(np.maximum(w[:, None], w[None, :]), 1e-12)
    face = (u * u * (3.0 - 2.0 * u) * sim).max(axis=1)
    raw = w * (PAD_STEM_F + (1.0 - PAD_STEM_F) * face)
    top = raw.max()
    if top <= 0:
        return np.ones(n)
    return np.clip((raw / top) ** PAD_GAMMA, PAD_MIN, 1.0)


def pad_plane(z, grades, dirs, auth, cross=None):
    """(z0, g (2,)): the plane z0 + g . (p - node) fitted to the branches' heights z (n,) at the node, their grades (n,)
    along dirs (n, 2) leaving it and their own cross slopes (cross (n,), toward each branch's left; None: level),
    weighted by auth (n,) (a road wants its own grade along it and its own cross section across it, PAD_FLAT_W), the
    tilt pulled level by PAD_RIDGE and capped at PAD_MAX_TILT. Pure."""
    z, gr, a = (np.asarray(v, np.float64) for v in (z, grades, auth))
    cr = np.zeros(len(a)) if cross is None else np.asarray(cross, np.float64)
    d = np.asarray(dirs, np.float64).reshape(-1, 2)
    d = d / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-12)[:, None]
    nl = np.c_[-d[:, 1], d[:, 0]]
    z0 = float((a * z).sum() / max(a.sum(), 1e-12))
    # least squares: a_i (g . d_i - grade_i)^2 + PAD_FLAT_W a_i (g . n_i - cross_i)^2 + PAD_RIDGE |g|^2
    M = (a[:, None, None] * (d[:, :, None] * d[:, None, :] + PAD_FLAT_W * nl[:, :, None] * nl[:, None, :])).sum(0) \
        + PAD_RIDGE * np.eye(2)
    g = np.linalg.solve(M, (a[:, None] * (d * gr[:, None] + PAD_FLAT_W * nl * cr[:, None])).sum(0))
    t = float(np.hypot(*g))
    if t > PAD_MAX_TILT:
        g *= PAD_MAX_TILT / t
    return z0, g


def _arcs(p):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(np.asarray(p.xy, np.float64)[:, :2], axis=0).T))]


def _pad_nodes(plans, rank, boxes):
    """[(node xy (2,), [(plan j, arc at the node, direction +1 / -1)])]: the plans' ends joined within CHAIN_JOIN_M
    (three branches or more, or two of different roads at an angle), and the at-grade crossings of two plans away from
    their ends (two branches each)."""
    ends = []
    for j, p in enumerate(plans):
        if len(p.xy) < 2:
            continue
        s = _arcs(p)
        for e, (arc, sgn) in enumerate(((0.0, 1), (float(s[-1]), -1))):
            if p.abut[e]:
                continue
            ends.append((j, arc, sgn, float(p.xy[-1 if e else 0, 0]), float(p.xy[-1 if e else 0, 1])))
    nodes = []
    if ends:
        xy = np.array([[e[3], e[4]] for e in ends])
        used = np.zeros(len(ends), bool)
        order = np.argsort(xy[:, 0], kind="stable")
        sx = xy[order, 0]
        for ii, i in enumerate(order):
            if used[i]:
                continue
            lo = np.searchsorted(sx, xy[i, 0] - CHAIN_JOIN_M)
            hi = np.searchsorted(sx, xy[i, 0] + CHAIN_JOIN_M, side="right")
            grp = [int(k) for k in order[lo:hi] if not used[k] and math.hypot(*(xy[k] - xy[i])) <= CHAIN_JOIN_M]
            used[grp] = True
            br = [(ends[k][0], ends[k][1], ends[k][2]) for k in grp]
            # (and a road going on through the node in one part: two branches)
            x, y = float(xy[i, 0]), float(xy[i, 1])
            mine = {b[0] for b in br}
            for k in np.flatnonzero((boxes[:, 0] <= x) & (x <= boxes[:, 2]) & (boxes[:, 1] <= y) & (y <= boxes[:, 3])):
                if int(k) in mine or len(plans[k].xy) < 2:
                    continue
                dist, _zq, from_end = _nearest_on(plans[k], x, y)
                if dist <= CHAIN_JOIN_M and from_end >= JUNCTION_END_M:
                    sk = _arcs(plans[k])
                    A = np.asarray(plans[k].xy, np.float64)[:, :2]
                    dd = A[1:] - A[:-1]
                    ll = np.maximum((dd * dd).sum(1), 1e-12)
                    t = np.clip(((x - A[:-1, 0]) * dd[:, 0] + (y - A[:-1, 1]) * dd[:, 1]) / ll, 0.0, 1.0)
                    m = int(np.argmin(np.hypot(A[:-1, 0] + t * dd[:, 0] - x, A[:-1, 1] + t * dd[:, 1] - y)))
                    at = float(sk[m] + t[m] * (sk[m + 1] - sk[m]))
                    br += [(int(k), at, 1), (int(k), at, -1)]
            if len({plans[b[0]].rid for b in br}) < 2:
                continue        # (one road's own parts: its chain)
            nodes.append((xy[i].copy(), br))
    # crossings with no node: proper intersections of two plans' centrelines, each away from both plans' ends (every
    # segment bucketed on a PAD_GRID_M grid, the pairs within a bucket tested at once)
    segs = [(j, np.asarray(p.xy, np.float64)[:, :2]) for j, p in enumerate(plans) if len(p.xy) >= 2]
    if not segs:
        return nodes
    P0 = np.concatenate([xy[:-1] for _j, xy in segs])
    P1 = np.concatenate([xy[1:] for _j, xy in segs])
    J = np.concatenate([np.full(len(xy) - 1, j) for j, xy in segs])
    K = np.concatenate([np.arange(len(xy) - 1) for _j, xy in segs])
    rid = np.array([p.rid for p in plans])
    lo = np.floor(np.minimum(P0, P1) / PAD_GRID_M).astype(np.int64)
    hi = np.floor(np.maximum(P0, P1) / PAD_GRID_M).astype(np.int64)
    nx_ = hi[:, 0] - lo[:, 0] + 1
    ny_ = hi[:, 1] - lo[:, 1] + 1
    cnt = nx_ * ny_
    sid = np.repeat(np.arange(len(P0)), cnt)
    loc = np.arange(len(sid)) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    cx = lo[sid, 0] + loc % np.repeat(nx_, cnt)
    cy = lo[sid, 1] + loc // np.repeat(nx_, cnt)
    key = cx * 2000003 + cy
    o = np.argsort(key, kind="stable")
    key, sid = key[o], sid[o]
    start = np.r_[0, np.flatnonzero(np.diff(key)) + 1]
    size = np.diff(np.r_[start, len(key)])
    gsize = np.repeat(size, size)
    lidx = np.arange(len(key)) - np.repeat(start, size)
    m = gsize - 1 - lidx                        # partners after each one in its bucket
    ia = np.repeat(np.arange(len(key)), m)
    ib = ia + (np.arange(len(ia)) - np.repeat(np.cumsum(m) - m, m)) + 1
    sa_, sb_ = sid[ia], sid[ib]
    keep = (J[sa_] != J[sb_]) & (rid[J[sa_]] != rid[J[sb_]])
    sa_, sb_ = sa_[keep], sb_[keep]
    if len(sa_):
        pair = np.unique(np.c_[np.minimum(sa_, sb_), np.maximum(sa_, sb_)], axis=0)
        sa_, sb_ = pair[:, 0], pair[:, 1]
        r = P1[sa_] - P0[sa_]
        t_ = P1[sb_] - P0[sb_]
        qp = P0[sb_] - P0[sa_]
        den = r[:, 0] * t_[:, 1] - r[:, 1] * t_[:, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (qp[:, 0] * t_[:, 1] - qp[:, 1] * t_[:, 0]) / den
            v = (qp[:, 0] * r[:, 1] - qp[:, 1] * r[:, 0]) / den
        hit = np.flatnonzero((np.abs(den) > 1e-12) & (u >= 0) & (u < 1) & (v >= 0) & (v < 1))
        arcs = {}
        for h in hit:
            j, k = int(J[sa_[h]]), int(J[sb_[h]])
            for x in (j, k):
                if x not in arcs:
                    arcs[x] = _arcs(plans[x])
            sj, sk = arcs[j], arcs[k]
            i, mm = int(K[sa_[h]]), int(K[sb_[h]])
            a_arc = float(sj[i] + u[h] * (sj[i + 1] - sj[i]))
            b_arc = float(sk[mm] + v[h] * (sk[mm + 1] - sk[mm]))
            if min(a_arc, sj[-1] - a_arc) < JUNCTION_END_M or min(b_arc, sk[-1] - b_arc) < JUNCTION_END_M:
                continue    # (at an end: a node, found above, or a near miss)
            nodes.append((P0[sa_[h]] + u[h] * r[h], [(j, a_arc, 1), (j, a_arc, -1), (k, b_arc, 1), (k, b_arc, -1)]))
    return nodes


def junction_pads(plans, specs, nodes=None):
    """PAD_* above: the plans (in place) at every node lie on one plane over the junction's pad, its height and tilt
    weighted by each branch's authority (rank, and whether a branch faces it). plans: [Plan], specs: their Specs;
    nodes: a previous call's (the same plans' lines, other heights: the pass after the dips). Returns the nodes."""
    if not PAD_ON or len(plans) < 2:
        return nodes
    rank = np.array([road_rank(sp) for sp in specs])
    if nodes is None:
        boxes = np.array([q.bbox for q in plans], np.float64).reshape(-1, 4)
        nodes = _pad_nodes(plans, rank, boxes)
    # the strongest junctions first: a short part between two takes the stronger one's pad
    nodes.sort(key=lambda nd: (-max(rank[b[0]] for b in nd[1]), float(nd[0][0]), float(nd[0][1])))
    arcs = {}
    for node, br in nodes:
        for j, _a, _s in br:
            if j not in arcs:
                arcs[j] = _arcs(plans[j])
        crossing = all(0.0 < arc < float(arcs[j][-1]) for j, arc, _s in br)
        info = []
        for j, arc, sgn in br:
            p = plans[j]
            s = arcs.get(j)
            if s is None:
                s = arcs[j] = _arcs(p)
            avail = float(s[-1] - arc) if sgn > 0 else float(arc)
            if avail < PAD_MIN_PART_M:
                continue            # (a median's crossover, a short link: its two ends' landings, snap_junctions)
            look = min(CHAIN_LOOK_M, avail)
            px_ = np.interp(arc + sgn * look, s, p.xy[:, 0]) - np.interp(arc, s, p.xy[:, 0])
            py_ = np.interp(arc + sgn * look, s, p.xy[:, 1]) - np.interp(arc, s, p.xy[:, 1])
            gl = min(PAD_GRADE_M, avail)
            z_n = float(np.interp(arc, s, p.z))
            grade = (float(np.interp(arc + sgn * gl, s, p.z)) - z_n) / gl
            b_n = 0.0 if p.bank is None else float(np.interp(arc, s, p.bank)) * sgn     # (toward the branch's left)
            info.append((j, arc, sgn, avail, np.array([px_, py_]), z_n, grade, pad_class_w(specs[j]), b_n))
        if len(info) < 2 or len({plans[i[0]].rid for i in info}) < 2:
            continue
        if len(info) == 2 and float(info[0][4] @ info[1][4]) < -math.cos(math.radians(PAD_FACE_DEG[0])) * \
                math.hypot(*info[0][4]) * math.hypot(*info[1][4]):
            continue                # (a road going on into its next part: its chain's)
        zs = np.array([i[5] for i in info])
        if zs.max() - zs.min() > (PAD_CROSS_MAX_M if crossing else JUNCTION_MAX_M):
            continue                # (a ramp to an overpass, bad data, two roads crossing at two levels: as they were)
        dirs = np.array([i[4] for i in info])
        dirs /= np.maximum(np.hypot(dirs[:, 0], dirs[:, 1]), 1e-12)[:, None]
        auth = pad_authority([i[7] for i in info], dirs)
        # each branch's tangent at the node: through a road (a branch facing it, PAD_FACE_DEG[1]) the central difference
        # of the two (a crest's or a sag's one-sided grades both lean the same way), else its own grade leaving it
        gr = np.array([i[6] for i in info])
        cosd = dirs @ dirs.T
        np.fill_diagonal(cosd, 1.0)
        tang = gr.copy()
        lim = -math.cos(math.radians(PAD_FACE_DEG[1]))
        for n in range(len(info)):
            m = int(np.argmin(cosd[n]))
            if cosd[n, m] <= lim:
                tang[n] = 0.5 * (gr[n] - gr[m])
        z0, g = pad_plane(zs, tang, dirs, auth ** PAD_FIT_POW, [i[8] for i in info])
        nx, ny = float(node[0]), float(node[1])
        for n, (j, arc, sgn, avail, _d, z_n, _g, _w, _b) in enumerate(info):
            p = plans[j]
            s = arcs[j]
            # the pad along this branch: out to the other branches' cores (at their angle) and PAD_LANDING_M past
            # (a landing past them for a street's traffic to stop on; a track or a driveway climbs from the other's
            # carriageway edge, over its verge)
            street = pad_class_w(specs[j]) >= PAD_LANDING_MIN_W
            land = PAD_LANDING_M if street else PAD_LANDING_MINOR_M
            reach = 0.0
            for m, other in enumerate(info):
                if other[0] == j:
                    continue
                sin = abs(dirs[n, 0] * dirs[m, 1] - dirs[n, 1] * dirs[m, 0])
                if float(dirs[n] @ dirs[m]) > 0 or sin >= PAD_MIN_SIN:      # (not the branch facing it)
                    q = plans[other[0]]
                    reach = max(reach, (q.half if street else q.road_half) / max(sin, PAD_MIN_SIN))
            reach = min(reach + land, PAD_REACH_F * avail)
            d_all = (s - arc) * sgn
            win = np.flatnonzero((d_all >= -1e-9) & (d_all <= reach + JUNCTION_TAPER_M))    # (all it can change)
            if not len(win):
                continue
            d = d_all[win]
            xy = np.asarray(p.xy, np.float64)[:, :2]
            zw = np.asarray(p.z, np.float64)[win]
            # the plane, less the branch's own tangent: added to its profile, its height and tangent at the node become
            # the plane's and its own vertical curve stays (a road the plane is made from is all but unchanged)
            # (by its authority: a weak branch, a side road whose own end is the landing's step, lies on the plane itself)
            flat = z0 + g[0] * (xy[win, 0] - nx) + g[1] * (xy[win, 1] - ny)
            plane = flat + auth[n] * (zw - (z_n + tang[n] * d))
            tan = np.gradient(xy, axis=0)[win] if len(xy) > 1 else np.zeros((len(win), 2))
            tan /= np.maximum(np.hypot(tan[:, 0], tan[:, 1]), 1e-12)[:, None]
            bank_plane = g[0] * -tan[:, 1] + g[1] * tan[:, 0]      # (m per m toward the plan's left)
            own_bank = np.zeros(len(win)) if p.bank is None else np.asarray(p.bank, np.float64)[win]
            pad = d <= reach
            if not pad.any():
                continue
            z = zw.copy()
            bank = own_bank.copy()
            z[pad] = plane[pad]
            bank[pad] = bank_plane[pad]
            # back to its own: from the plane (carried on past the pad) to its own profile, the share eased out
            # (cosine): the grade runs on from the pad's without a break (easing the pad edge's height difference
            # alone kept the pad's level to its edge and its own grade right after: a 5-7 % kink there on a hillside)
            # (a track's or a driveway's as short as it must be: a side road leaving up a steep bank, held to the pad's level over a long ease,
            # ran in a trench 2.4 m deep, its own grade 30 %, the user 2026-10-29 at -32.0951, 116.03866: the longest
            # ease of PAD_TAPERS_M, up to JUNCTION_TAPER_M, that sinks it (or raises it) at most PAD_SINK_M more than the
            # pad edge's step faded out would, as a real one's ramp is: short and sharp on a bank, long on a gentle slope)
            de = float(d[pad].max())
            out = d > de
            k_e = int(np.argmax(np.where(pad, d, -np.inf)))
            step_ = abs(float(plane[k_e] - zw[k_e]))     # (the pad edge's own step: kept, whatever the ease)
            longest = max(min(JUNCTION_TAPER_M, 0.5 * avail - de, ease_length(step_, not street)), VERTEX_STEP)
            # (a street keeps its long ease: a junction's grade break is what its drivers feel)
            for taper in [longest] + ([] if street else sorted((t for t in PAD_TAPERS_M if t < longest), reverse=True)):
                w = 0.5 * (1.0 + np.cos(np.pi * np.clip((d - de) / taper, 0.0, 1.0)))
                if not out.any() or float((np.abs(w[out] * (plane[out] - zw[out])) - w[out] * step_).max()) <= PAD_SINK_M:
                    break
            btaper = max(min(PAD_BANK_TAPER_M, taper), VERTEX_STEP)
            wb = 0.5 * (1.0 + np.cos(np.pi * np.clip((d - de) / btaper, 0.0, 1.0)))
            z[out] += w[out] * (plane[out] - z[out])
            bank[out] += wb[out] * (bank_plane[out] - bank[out])
            if np.abs(z - zw).max() < PAD_MIN_DELTA_M and np.abs(bank - own_bank).max() < 1e-3:
                continue
            znew = np.asarray(p.z, np.float64).copy()
            znew[win] = z
            p.z = znew
            if p.bank is not None or np.abs(bank).max() > 1e-4:
                bnew = np.zeros(len(xy)) if p.bank is None else np.asarray(p.bank, np.float64).copy()
                bnew[win] = bank
                p.bank = bnew
    return nodes


def guard_limits(spec, plan):
    """(grade ceiling, window m, window height m) of a road: its class's, a slip road's and a pass road's more."""
    cls = spec.cls or ""
    base = cls.removesuffix("_link")
    g = GUARD_GRADE.get(base, GUARD_GRADE_OTHER)
    if cls != base:
        g += GUARD_LINK_BONUS
    if plan.steep:
        g += GUARD_MOUNTAIN_BONUS
    n, h = GUARD_WINDOW.get(base, GUARD_WINDOW_OTHER)
    return g, n, h


def grade_guard(plans, specs, bases, skip=()):
    """The cell's finished plans (every pass done) put under the grade ceilings and the window rule, as one graph (see
    GUARD_*): changed in place. bases: each plan's natural profile (the heights before the passes), skip: indices of plans
    left as they are (tunnels). Returns the kernel's stats (changed points, largest change, edges over the ceiling before
    / the worst excess, after / the worst excess, the worst point's index) or None. In Rust (earthfast.grade_guard)."""
    if not plans:
        return None
    import fast
    fast.require(165, "the grade guard")
    n = [len(p.xy) for p in plans]
    off = np.concatenate([[0], np.cumsum(n)]).astype(np.int64)
    xy = np.ascontiguousarray(np.concatenate([np.asarray(p.xy, np.float64)[:, :2] for p in plans]).reshape(-1, 2))
    z = np.ascontiguousarray(np.concatenate([np.asarray(p.z, np.float64) for p in plans]))
    base = np.ascontiguousarray(np.concatenate([np.asarray(b, np.float64) for b in bases]))
    lim = np.array([guard_limits(sp, p) for sp, p in zip(specs, plans)], np.float64).reshape(-1, 3)
    gmax = np.ascontiguousarray(lim[:, 0])
    for k in skip:
        gmax[k] = 0.0
    rank = np.array([road_rank(sp) for sp in specs], np.int64)
    half = np.array([p.road_half for p in plans], np.float64)
    abut = np.array([[bool(p.abut[0]), bool(p.abut[1])] for p in plans], np.uint8).reshape(-1, 2)
    out, stats, changed = fast._ef.grade_guard(xy, z, base, off, gmax, np.ascontiguousarray(lim[:, 1]),
                                               np.ascontiguousarray(lim[:, 2]), rank, half, abut, GUARD_REACH_M,
                                               GUARD_DESIGNED_M, JUNCTION_MAX_M)
    for j in np.flatnonzero(changed):
        plans[j].z = out[off[j]:off[j + 1]].copy()
    return stats


LEG_RID_SHIFT = 44           # PlanPack: a pass road's leg (between hairpins) as rid + leg << this (rids < 2^41; earthfast's
                             # LEG_ROAD_MASK alike)
HAIRPIN_FOLD_RATIO = 0.5      # hairpin_cores: a point of the road's own line this much nearer than along it is another leg


def hairpin_cores(plans, specs, frame):
    """The pass roads' plans among plans ([(fid, Plan)], specs alike) widened, in place, round their widened hairpins
    (roads.hairpin_extra: the drawn road's own widening, on the plan's line): Plan.widen, their cuttings' walls and tops
    moved out by as much. A plan of its own over each hairpin (wider all along it, its ends held at their heights)
    reached the hairpin's other leg and stepped the lanes by up to 18 cm (the Stelvio; the user, 2026-11-01: "Fix the
    bumps"). Returns how many were widened."""
    n = 0
    for (_fid, p), sp in zip(plans, specs):
        if sp is None or not getattr(sp, "mountain", False) or len(p.xy) < 3:
            continue
        ex = roadsmod.hairpin_extra(frame, sp, np.asarray(p.xy, np.float64)[:, :2])
        if ex is None or float(np.max(ex)) < 0.05:
            continue
        ex = np.maximum(np.asarray(ex, np.float64), 0.0)
        # (never over half way to its own other leg: a point of the plan nearer than FOLD_RATIO of the way along to it,
        # across a hairpin's narrow middle; there the wider cores met and blended the legs' heights, a 22 cm dip)
        rows = np.nonzero(ex > 0.0)[0]
        import fast
        fast.require(126, "fold_clearance")
        room = np.asarray(fast._ef.fold_clearance(np.ascontiguousarray(np.asarray(p.xy, np.float64)[:, :2]),
                                                  rows.astype(np.int64), HAIRPIN_FOLD_RATIO)) / 2.0 - p.half
        ex[rows] = np.minimum(ex[rows], np.maximum(room, 0.0))
        if float(ex.max()) < 0.05:
            continue
        set_widen(p, ex)
        if p.cut is not None:
            seg = np.maximum(ex[:-1], ex[1:])
            c = np.asarray(p.cut, np.float64).copy()
            for col in (0, 2, 3, 5):        # (the walls' and the tops' offsets from the centre line)
                c[:, col] += seg
            p.cut = c
        n += 1
    return n


def set_widen(p, widen):
    """Plan p's widen (hairpin_cores; None: none), its box grown to reach as far."""
    p.widen = widen
    if widen is not None and len(widen):
        pad = p.half + float(np.max(widen)) + T_MAX
        xy = np.asarray(p.xy)
        p.bbox = (xy[:, 0].min() - pad, xy[:, 1].min() - pad, xy[:, 0].max() + pad, xy[:, 1].max() + pad)


def shoulder(spec):
    """m of flat ground beyond the carriageway edge, each side. Like the core width, never from the land
    cover: a road class that gets sidewalks in towns keeps a sidewalk-wide verge everywhere."""
    if spec.kind not in roadsmod.BUILT:
        return 0.0
    if getattr(spec, "mountain", False):        # (a pass road between its wall and its parapet: roads._mountain_roads)
        return MOUNTAIN_SHOULDER_M
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
    water = _store_attr("water")
    tunnel_raw = _store_attr("tunnel_raw")
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
            # (this thread's faults: the planning threads of a process plan cells side by side, and another's fault
            # kept this one's complete plans from later visits and baked packs: 2026-11-02)
            f0, l0 = revisit.thread_faults(), revisit.thread_lidar_faults()
            if shared is not None:
                got = shared
            else:
                import stackdump    # (a slow plan holds every process waiting for it: say where it waits)
                watch = stackdump.watch(PLAN_SLOW_S, f"cell {key}'s plans still being made after {PLAN_SLOW_S:.0f} s")
                try:
                    got = self._make_cell(key)
                finally:
                    watch.cancel()
            if got is None:
                return []
            if mine:        # (kept for later visits too when complete: nothing it read failed or was missing)
                self.shared.store(key, *got, persist=revisit.thread_faults() == f0
                                  and revisit.thread_lidar_faults() == l0)
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
        widths = getattr(self, "widths", None)
        if widths is not None and hasattr(widths, "prefetch"):
            # the cell's buildings (its roads' widths) fetched while its roads are: one after the other before
            def walls():
                try:
                    widths.prefetch([key])
                except Exception as e:
                    log.debug("grading: walls of %s: %s: %s", key, type(e).__name__, e)
            threading.Thread(target=walls, daemon=True, name="walls-ahead").start()
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
            placement = roadsmod.placement_context([feats], None, self.widths)     # (as the road tiles draw them)
            parts, ids, clipped, rings = [], [], [], []
            box = self._clip_box(key)
            keeps = []
            for i in range(len(feats)):
                if specs[i] is not None and specs[i].cls in UNGRADED:
                    for ring in roadsmod.ground_parts(feats, i):
                        ring = placement.ring(self.frame, feats, i, ring, specs[i])
                        e, n, _ = self.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                        keeps.append((np.stack([np.asarray(e), np.asarray(n)], axis=1),
                                      specs[i].width / 2 + UNGRADED_MARGIN_M))
                    continue
                if specs[i] is None:
                    continue
                for ring in roadsmod.ground_parts(feats, i):     # not bridges / tunnels
                    ring = placement.ring(self.frame, feats, i, ring, specs[i])
                    pieces = self._near_cell(np.asarray(ring, np.float64), box)
                    for r in pieces:
                        parts.append((r, specs[i]))
                        ids.append(str(feats.ids[i]))
                        clipped.append(len(pieces) > 1 or r is not ring)
                        rings.append(roadsmod.is_ring(feats, i))
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
                                                                         coarse=coarse, obs=self._obs(key),
                                                                         decks=self._decks_near(key), rings=rings)):
                if p is not None:
                    p.rid = road_id(fid)
                    plans.append((fid, p))
                    pspecs.append(part[1])
                    pclip.append(cl)
                    pparts.append((fid, part[1], part[0]))
            bases = [np.array(p.z, np.float64) for _f, p in plans] if GUARD_ON else None
            if NEIGHBOUR_ON and plans and self._coarse(key):
                try:
                    r = 2.0 * NEIGHBOUR_TREND_SIGMA_M
                    couple_neighbours([p for _f, p in plans], pspecs,
                                      lambda xy: field_heights(self.frame, self.elev, xy, r, NEIGHBOUR_TREND_SIGMA_M))
                except Exception as e:      # each road on its own rather than no grading
                    log.debug("grading: neighbours of %s: %s: %s", key, type(e).__name__, e)
            pad_nodes = None
            if JUNCTION_SNAP_ON and plans:
                try:
                    snap_junctions([p for _f, p in plans], pspecs)
                    pad_nodes = junction_pads([p for _f, p in plans], pspecs)
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
            # (the carriageways' profiles before the dips: a motorway under another road's low deck still dips)
            if CW_ON and plans:
                try:
                    self._apply_carriageways(plans, pspecs)
                except Exception as e:      # as they were rather than no grading
                    log.debug("grading: carriageways of %s: %s: %s", key, type(e).__name__, e)
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
                    junction_pads([p for _f, p in plans], pspecs, pad_nodes)
                except Exception as e:
                    log.debug("grading: junctions after the dips of %s: %s: %s", key, type(e).__name__, e)
            if SEAMS_ON and CW_ON and plans:
                try:            # the deck's height at each carriageway road's abutment end, whatever the passes did to it
                    if self._pin_cw_ends(plans, pspecs) and JUNCTION_SNAP_ON:
                        snap_junctions([p for _f, p in plans], pspecs)      # (the side roads meet it again)
                except Exception as e:      # the plans as the passes left them rather than no grading
                    log.debug("grading: carriageway ends of %s: %s: %s", key, type(e).__name__, e)
            if GUARD_ON and plans:
                try:            # the finished plans under the grade ceilings and the window rule (grade_guard)
                    st = grade_guard([p for _f, p in plans], pspecs, bases,
                                     [i for i, (f, _p) in enumerate(plans) if TUNNEL_TAG in f])
                    if st is not None and st[4] > 0:
                        log.info("grading: grade guard %s: %d edges still over their ceiling, worst %.1f m", key,
                                 int(st[4]), float(st[5]))
                except Exception as e:      # the plans as the passes left them rather than no grading
                    log.debug("grading: grade guard of %s: %s: %s", key, type(e).__name__, e)
            if plans:
                try:            # a pass road's widened hairpins (roads.HAIRPIN_*): its core wider round each
                    hairpin_cores(plans, pspecs, self.frame)
                except Exception as e:      # the road's own core rather than none
                    log.debug("grading: hairpins of %s: %s: %s", key, type(e).__name__, e)
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
                    beside = [(np.asarray(q.xy, np.float64)[:, :2], np.asarray(q.z, np.float64), q.half)
                              for _f, q in plans[:len(pspecs)] if SEAMS_ON and TUNNEL_TAG not in _f]
                    for fid, xy, z in railways.grade_plans(self.frame, rcells, own, self.elev):
                        if beside:      # (the bed beside a road: where the ground between has room for its slope)
                            import railroadseam
                            z = railroadseam.couple(xy, z, railways.GRADE_HALF_M, beside)
                        # (the carriageway of the bed's plan is the ballast's top, not the whole flat band: past it the band
                        # gives way to a road beside it. A track 3 m past a primary's edge, prospekt Heroiv in Nemiia, was
                        # blended with the road over the road's shoulder: a ditch at its edge; as upstream's #920)
                        p = Plan(xy, z, railways.GRADE_HALF_M,
                                 road_half=railways.BALLAST_TOP_M if SEAMS_ON else None)
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
            portals = {}
            secs = self._tunnel_sections(feats, key, [p for _f, p in tplans], portals)
            plans.extend((fid + TUNNEL_TAG, Plan(xy, z, spec.width / 2 + shoulder(spec)))
                         for fid, spec, xy, z, k in secs if k == 0)
            plans.extend(self._mouth_plans(secs, portals))
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
            placement = roadsmod.placement_context([feats], None, self.widths)     # (as _make_cell's parts)
            rows = []
            for i in range(len(feats)):
                if specs[i] is None or specs[i].cls in UNGRADED:
                    continue
                for ring in roadsmod.ground_parts(feats, i):
                    rows.append((i, placement.ring(self.frame, feats, i, np.asarray(ring, np.float64), specs[i])))
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
                                   obs=self._obs(None, parts[0][0]), decks=self._decks_ll(parts[0][0]))
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
                           coarse=COARSE_EASE_ON and self._coarse_ll(part), obs=self._obs(None, part),
                           decks=self._decks_ll(part))[0]
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
            if not APPROACH_LIDAR_ON or not getattr(prov, "lidar", False):
                return None

            def approach_obs(xy, spec):
                # (on lidar terrain: the lidar's measured approaches alone, the parts from a bridge's end: roadz
                # APPROACH_*; their embankments our DTM smooths away)
                if not self._touches_bridge_end(xy):
                    return None
                try:
                    return prov.observed(self.frame, xy, spec.width / 2, 0, lidar_ground=True)
                except Exception as e:  # the DEM's rather than no grading
                    log.debug("grading: measured approach: %s: %s", type(e).__name__, e)
                    return None
            return approach_obs

        def obs(xy, spec):
            try:
                return prov.observed(self.frame, xy, spec.width / 2, 0)
            except Exception as e:      # the DEM's rather than no grading
                log.debug("grading: measured road heights: %s: %s", type(e).__name__, e)
                return None
        return obs

    def _touches_bridge_end(self, xy):
        """Does a road part (xy (k, 2) in this frame) start or end at a bridge's end (APPROACH_TOUCH_M)?"""
        xy = np.asarray(xy, np.float64)
        if len(xy) < 2:
            return False
        tips = xy[[0, -1]]
        las, los, _ = self.frame.enu_to_geodetic(tips[:, 0], tips[:, 1], 0.0)     # (both ends in one call: per point)
        for p, la, lo in zip(tips, las.tolist(), los.tolist()):
            ends = np.asarray(self._cell_enu(mapcells.cell_of(la, lo), ground=False)["ends"], np.float64).reshape(-1, 2)
            if len(ends) and (np.hypot(ends[:, 0] - p[0], ends[:, 1] - p[1]) <= APPROACH_TOUCH_M).any():
                return True
        return False

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
                    gm = self._grade_match(e) if SEAMS_ON else None
                    if gm is not None and not _never_raised(specs[i]):
                        # (the deck is the chord between its abutments: the ground at the ends 3-4 m apart over 17 m, a wall
                        # in it, or a 2 m hump to meet it; the approaches take the difference half each, the lower up, the
                        # higher down: road 303 at Zdrina, Velke Petrovice, 2026-11-09)
                        a = approach_add(p.xy, at_end, abs(gm), p.half, (), SLOPE_RUN)
                        if a is not None:
                            add = (a if gm > 0 else -a) if add is None else add + (a if gm > 0 else -a)
                    continue
                if _never_raised(specs[i]) and not got[3] and not (len(got) > 5 and got[5]):
                    continue
                lift, others = got[0], got[1]
                zp = float(np.asarray(p.z, np.float64)[-1 if at_end else 0])
                if len(got) > 6 and got[5]:
                    # (crossings.ON: up to the solved end's height from the plan's own there)
                    lift = min(float(got[6]) - zp, APPROACH_MAX_LIFT)
                    if lift < APPROACH_MIN_LIFT:
                        continue
                elif len(got) > 4 and got[4] is not None:
                    # (a measured deck end: up to its height from the plan's own there, not from the ground under the
                    # point, which may lie down the cut the deck spans: Charles Street's approach over I-83 rose 2.2 m
                    # over its plan to a deck end measured at the street's height, 2026-10-29)
                    lift = min(float(got[4]) - zp, APPROACH_MAX_LIFT)
                    if lift < APPROACH_MIN_LIFT:
                        continue
                else:
                    # (the lift is over the ground at the bridge's end point (bridge_lifts): where the plan is higher
                    # there, filled up the embankment its map line ends down (fill_abutment), only what it still lacks.
                    # The fill and the whole lift on top took Kurna - Tuhala's deck end 6 m over the embankment it
                    # stands on, over Tallinna ringtee, 59.3364 N 24.8239 E, GitHub #684)
                    ela, elo, _ = self.frame.enu_to_geodetic(np.array([e[0]]), np.array([e[1]]), 0.0)
                    lift -= max(0.0, zp - float(np.asarray(self.elev(ela, elo), np.float64).reshape(-1)[0]))
                    if lift < APPROACH_MIN_LIFT:
                        continue
                measured = len(got) > 4 and got[4] is not None
                # (a solved crossing's approach walled beside the road at grade, as a measured one: the street splitting
                # into its two carriageways past Wezep's A28 deck, 19 m off its abutment, held the ramp's 1:2 fill to
                # 3.3 m of 4.7, its deck end 1 m under the ramp, a dip where the deck met it, the user 2026-11-01)
                solved = len(got) > 6 and bool(got[5])
                # (a motorway's or trunk's ramp at XS_FIXED_GRADE: a hump at 5 % on a freeway, Perth's Mitchell
                # Freeway rising 3-4 m to each of its street bridges, the user 2026-11-01)
                a = approach_add(p.xy, at_end, lift, p.half, others,
                                 MEASURED_SIDE_RUN if measured or solved else SLOPE_RUN,
                                 XS_FIXED_GRADE if solved and _never_raised(specs[i]) else None)
                if a is not None:
                    add = a if add is None else np.maximum(add, a)
            if add is not None:
                np_ = Plan(p.xy, p.z + add, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
                np_.steep = getattr(p, "steep", False)
                set_widen(np_, getattr(p, "widen", None))
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
        bridges, ground, over = [], {}, []
        for k in keys:
            c = self._cell_enu(k)
            if UNDER_DECK_ON:               # (every deck, for the ground under it: _under_ground)
                over += [(line, half) for line, half in zip(c["bridges"], c.get("bridge_half", ())) if half > 0]
                over += [(line, railwaysmod.DECK_HALF_M) for line in c.get("rail_bridges", ())]
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
                    ground[(fid, j)] = (line, sg, med, cls)
        if not bridges or not ground:
            return
        # (single spans: neither end continues into another bridge part; deduplicated across cells, a piece lying
        # along a longer one of its kind dropped: a deck clipped at one cell's box beside the whole deck from the
        # next, joined at their shared end into a chain doubling back over its own carriageway, #1020)
        if len(bridges) > 1:
            import fast
            fast.require(175, "lines_covered")
            bl = [np.ascontiguousarray(np.asarray(b, f64)[:, :2]) for b, _f, _r in bridges]
            cov = fast._ef.lines_covered(np.concatenate(bl), np.r_[0, np.cumsum([len(b) for b in bl])].astype(
                np.int64), np.array([2 * int(f) + int(r) for _b, f, r in bridges], np.int64), UNDER_COVER_M)
            bridges = [t for t, c in zip(bridges, cov) if not c]
        uniq, seen = [], set()
        for b, fixed, rail in bridges:
            k2 = (round(float(b[0, 0]), 1), round(float(b[0, 1]), 1), round(float(b[-1, 0]), 1), len(b))
            if k2 not in seen:
                seen.add(k2)
                uniq.append((b, fixed, rail))
        chained = set()
        if UNDER_CHAIN_ON:
            n0 = [len(b) for b, _f, _r in uniq]
            uniq = _merge_span_chains(uniq)
            chained = {i for i, (b, _f, _r) in enumerate(uniq) if len(b) not in n0}
        spans, rail_span = [], []
        ue = np.array([[b[0, :2], b[-1, :2]] for b, _f, _r in uniq], f64)        # (n, 2 ends, 2)
        stub = np.array([float(np.hypot(*np.diff(b[:, :2], axis=0).T).sum()) < UNDER_STUB_M for b, _f, _r in uniq])
        coarse = self._coarse(key)      # (there the railway rises over the road: railways.coarse_underpasses)
        for n, (b, fixed, rail) in enumerate(uniq):
            if rail and coarse:
                continue
            L = float(np.hypot(*np.diff(b, axis=0).T).sum())
            # (a carriageway's deck, any length, joined or not: its height is its profile's, the roads under it dip
            # to clear it)
            cw_deck = CW_ON and not rail and self._cw_z_at(b, b[:1]) is not None
            max_l = UNDER_CHAIN_MAX_M if n in chained else UNDER_MAX_BRIDGE_M
            if L < 2 * APPROACH_MIN_CROSS_M or (L > max_l and not rail and not cw_deck):
                continue
            d = np.hypot(ue[:, :, None, 0] - ue[n, None, None, :, 0], ue[:, :, None, 1] - ue[n, None, None, :, 1])
            d[n] = np.inf                       # (another part's end within 0.75 m of either of its ends: joined)
            if UNDER_CHAIN_ON and L >= UNDER_STUB_M:
                d[stub] = np.inf                # (a stub at its end, too short to span a road: no inner end)
            if rail or cw_deck or not (d < 0.75).any():
                spans.append((b, L, fixed, rail))
                rail_span.append(rail)
        if not spans:
            return
        gids = sorted(ground)
        lines = [ground[g][0] for g in gids]
        crs = roadsmod._crossings_by([b for b, _L, _f, _r in spans], lines)
        ez, raised = {}, set()

        def deck_ends(j):           # (an overpass's abutments' ground, raised when it is: as high as its ramps could
            got = ez.get(j)         # take them; asked only for one a road crosses under: every span's took 2 s a
            if got is None:         # Canton cell, 2026-10-25)
                b, _L, fixed, rail = spans[j]
                ends = np.array([b[0], b[-1]], f64)
                ela, elo, _ = self.frame.enu_to_geodetic(ends[:, 0], ends[:, 1], 0.0)
                got = np.asarray(self.elev(np.asarray(ela), np.asarray(elo)), f64).reshape(2).copy()
                if rail:            # (the track's level beyond its ends, as railways.profile lays the deck)
                    def ground_xy(q):
                        qla, qlo, _ = self.frame.enu_to_geodetic(q[:, 0], q[:, 1], 0.0)
                        return np.asarray(self.elev(np.asarray(qla), np.asarray(qlo)), f64)
                    for e, lv in enumerate(railwaysmod.approach_levels(b[:, :2], ground_xy)):
                        if lv is not None:
                            got[e] = max(got[e], lv[0])
                if not fixed or (XS_ON and not rail and self._xsolve(b) is not None):
                    got[0] += self._end_raised(b[0])
                    got[1] += self._end_raised(b[-1])
                if not rail:        # (or on its embankment's top, where its line ends down the slope: #684's fill)
                    got = np.maximum(got, [self._end_filled(b, False), self._end_filled(b, True)])
                ez[j] = got
            return got
        cands, decks, cand_xs, cand_cw = [], [], [], []
        for i, cr in enumerate(crs):
            if not cr:
                continue
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            ab = np.diff(xy, axis=0)
            for c in cr:
                bi, arc_b, pt = c[0], c[1], np.asarray(c[2], f64)
                b, L, fixed, rail = spans[bi]
                if arc_b < APPROACH_MIN_CROSS_M or arc_b > L - APPROACH_MIN_CROSS_M:
                    continue                    # a junction at the bridge's end
                corridor = np.clip([arc_b - roadsmod.DECK_CLEAR_REACH_M, arc_b + roadsmod.DECK_CLEAR_REACH_M], 0.0, L)
                cz = None
                if CW_ON and not rail_span[bi]:
                    sb = np.r_[0.0, np.cumsum(np.hypot(*np.diff(b, axis=0).T))]
                    cpts = np.stack([np.interp(corridor, sb, b[:, 0]), np.interp(corridor, sb, b[:, 1])], axis=1)
                    cz = self._cw_z_at(b, np.r_[cpts, pt[None]])
                if cz is not None:
                    deck = float(np.min(cz))
                else:
                    ze = deck_ends(bi)
                    deck = float(np.min(ze[0] + (ze[1] - ze[0]) * corridor / L))
                # where along this road (its nearest vertex segment)
                t = np.clip(((pt - xy[:-1]) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
                j = int(np.argmin(np.linalg.norm(xy[:-1] + t[:, None] * ab - pt, axis=1)))
                # (the deck's own clearance: its depth scales with its span, roads.deck_depth; the full-depth
                # UNDER_CLEAR_M dug the road out under a short overpass that sits lower)
                # (a rail deck's height is its bed: its own depth under the ballast's top, railways.DECK_D)
                depth = railwaysmod.DECK_D - railwaysmod.BALLAST_H if rail else \
                    roadsmod.deck_depth(roadsmod.geom_span([b]))
                clear = roadsmod.deck_road_clear(depth) + ROAD_LIFT
                cands.append((i, float(s[j] + t[j] * (s[j + 1] - s[j])), deck - clear))
                decks.append((deck, fixed))
                cand_xs.append((bi, pt))
                cand_cw.append(None if cz is None else deck - (CW_HEADROOM_M + roadsmod.deck_depth(
                    roadsmod.geom_span([b])) + ROAD_LIFT))
        if not cands:
            return
        # (over as _Decks: its boxes and segments made once for every road's test, not per deck per road)
        zc = self._under_ground(lines, [ground[g][1:3] for g in gids], cands, None if coarse else _Decks(over))
        dem = getattr(self, "dem", None) or getattr(self.md, "dem", None)
        lidar = UNDER_LIDAR_ON and not coarse and dem is not None and hasattr(dem, "coarse_at")
        requirements, xs_floor = [], []
        floor = seatiles.SEA_E + UNDER_SEA_M
        for (i, sc, top), (deck, fixed), z, (bi, pt), top_cw in zip(cands, decks, zc, cand_xs, cand_cw):
            if top_cw is not None and _cw.family(ground[gids[i]][3]) is not None:
                continue                        # (a carriageway under a carriageway's deck: solved together)
            if top_cw is not None:
                # (a carriageway's deck, its height solved: the road under at its trusted height (beside the deck's
                # embankment, not lidar's fill under it), lower only where the deck needs it)
                u = _xs.dir_at(lines[i], pt)
                tl = _xs.trusted_under(pt, u, 8.0, lambda p_: self._z_raw(p_))
                tl = z if tl is None else min(tl, z)
                target = max(min(tl, top_cw), z - UNDER_MAX_DROP_M)
                if UNDER_SEA_ON and z >= floor:
                    target = max(target, floor)
                if os.environ.get("BNE_CW_DEBUG"):
                    log.debug("grading: CWDIP at %s: road %.1f trusted %.1f deck %.1f -> %.1f", np.round(pt), z, tl,
                              deck, target)
                if target < z - 0.1:
                    requirements.append((i, sc, z - target))
                    xs_floor.append((np.asarray(pt, f64), target, _road_fids(i, lines, [ground[g][3] for g in gids],
                                                                            gids), 8.0 + _xs.UNDER_GAP_M))
                continue
            sol = self._xsolve(spans[bi][0]) if XS_ON and not rail_span[bi] else None
            if sol is not None and sol["cross"]:
                # (crossings.ON: the road's target from the solve: its trusted height, less its share of the
                # separation; lidar's fill under the deck taken off whatever the deck does)
                k = int(np.argmin([np.hypot(*(q - pt)) for q, _t in sol["cross"]]))
                if np.hypot(*(sol["cross"][k][0] - pt)) <= 5.0:
                    # (off lidar, no higher than what the deck as its ramps raised it clears: one held down by the
                    # roads beside its ramps leaves the rest to the road under, as before. On lidar, as #504: the deck
                    # rises over the road instead, whose ground the lidar has)
                    target = sol["cross"][k][1] if lidar else min(sol["cross"][k][1], top)
                    target = max(target, z - UNDER_MAX_DROP_M)
                    if UNDER_SEA_ON and z >= floor:
                        target = max(target, floor)
                    if target < z - 0.1:
                        requirements.append((i, sc, z - target))
                        xs_floor.append((np.asarray(pt, f64), target, _road_fids(i, lines, [ground[g][3] for g in gids],
                                                                                gids), sol["reach"]))
                    continue
            if lidar and not fixed and deck < z + UNDER_LIDAR_DECK_M:
                continue                        # (the ends' ground is not the deck: the road keeps its own, the deck
                                                # rises; a never-raised deck, a railway's or a motorway's, cannot)
            target = max(top, z - UNDER_MAX_DROP_M)
            if UNDER_SEA_ON and z >= floor:
                target = max(target, floor)     # (not under the sea's surface: its water tiles would flood it)
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
        # on coarse elevation a street's dip stays on its run (UNDER_RUN_*: street_runs), each run alone. (Every cell
        # sees the same runs: a dip reaches at most UNDER_FLAT_M + its longest ramp (~221 m) along its street, so the
        # street's nodes it can depend on lie within UNDER_REACH_M of every cell's box the dip reaches, as do all the
        # lines meeting there)
        net, runs = requirements, []
        if coarse and UNDER_RUN_ON:
            net, runs = street_runs(lines, [ground[g][3] for g in gids], requirements)
        offsets = underpass_offsets(lines, net, queries)
        for rxy, members, reqs in runs:
            on = [k for k, (ln, _q) in enumerate(queries) if ln in members]
            if on:
                for k, got in zip(on, underpass_offsets([rxy], reqs, [(0, queries[k][1]) for k in on])):
                    offsets[k] = np.minimum(offsets[k], got)
        for n, near, got in zip(which, inside, offsets):
            if not np.any(got < -1e-8):
                continue
            fid, p = plans[n]
            offset = np.zeros(len(near))        # (past the box: the plan's piece there is another cell's)
            offset[near] = got
            z0 = np.asarray(p.z, f64)
            z1 = z0 + offset
            if xs_floor:
                # (crossings.ON: a solved crossing's target is a height, not a depth: the dip, made as a depth from the
                # road's ground there, went on 1.5 m under it where the plan beside lidar's fill was already lower,
                # Wezep's street 10 m from the A28. Nothing near one goes under it, nor over its own height)
                # The road itself is held at its target across the fill (the dip, a depth from the ground at the
                # crossing, left the fill's far shoulders standing), rising away at UNDER_GRADE past it.
                xy = xys[n]
                # (the floor: the lowest target near, not each in turn: a street under a motorway and its slip road
                # side by side was held at the slip road's target, 1.3 m over the motorway's, and the motorway's deck
                # humped over it, Perth's Mitchell Freeway at Roe Street, the user 2026-11-01)
                low_t = np.full(len(z1), np.inf)
                for q, t, qfids, reach in xs_floor:
                    dq = np.hypot(xy[:, 0] - q[0], xy[:, 1] - q[1])
                    if dq.min() > XS_FLOOR_M:
                        continue
                    if fid in qfids and not (coarse and UNDER_RUN_ON):
                        # (and the parts continuing it, of its class: a dual carriageway's halves past the deck. Not on
                        # coarse elevation: there the dip stays as deep as fits on its street's run, #655's rather a
                        # lower bridge than a sunken neighbourhood; held at the solve's target it went on 8 m deep)
                        cap = t + np.maximum(dq - reach, 0.0) * UNDER_GRADE
                        z1 = np.where(dq <= XS_FLOOR_M, np.minimum(z1, np.maximum(cap, np.minimum(z0, t))), z1)
                    low_t = np.where(dq <= XS_FLOOR_M, np.minimum(low_t, t), low_t)
                z1 = np.where(np.isfinite(low_t), np.maximum(z1, np.minimum(z0, low_t)), z1)
            if UNDER_SEA_ON:                    # (every point: a dip's flat and ramps, a joined road's share)
                z1 = np.where(z0 >= UNDER_POLDER_M, np.maximum(z1, np.minimum(z0, floor)), z1)
            np_ = Plan(p.xy, z1, p.half, p.abut, p.cut, p.road_half)
            np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
            np_.steep = getattr(p, "steep", False)
            set_widen(np_, getattr(p, "widen", None))
            plans[n] = (fid, np_)

    def _under_ground(self, lines, smooth, cands, decks=None):
        """The heights of ground roads where they cross under a bridge, cands [(line, arc m, _)]: their ground along
        them, read beside the decks over them (decks: deck_capped's, None: as it is), median-filtered and smoothed as
        their plans are (_plan: smooth (sigma, median width) per line), at the crossing. A pure function of the road
        and the ground (its plan's own height is only in the cells planning it)."""
        f64 = np.float64
        pts, spans = [], []
        for i, sc, _d in cands:
            sg, med = smooth[i]
            xy = lines[i]
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            k = int(math.ceil(3.0 * sg / PROFILE_STEP))
            raw = sc + PROFILE_STEP * np.arange(-k, k + 1)
            a = np.clip(raw, 0.0, s[-1])
            pts.append(np.stack([np.interp(a, s, xy[:, 0]), np.interp(a, s, xy[:, 1])], axis=1))
            ext = None
            if decks:
                # (and on straight past the part's ends, for deck_capped's ground beside a deck near one: Järve tee's
                # part ends 12 m past Tartu mnt's deck, still on the lidar's hump)
                d0 = (xy[0] - xy[1]) / max(float(np.hypot(*(xy[0] - xy[1]))), 1e-9)
                d1 = (xy[-1] - xy[-2]) / max(float(np.hypot(*(xy[-1] - xy[-2]))), 1e-9)
                ext = pts[-1] + np.where(raw < 0.0, -raw, 0.0)[:, None] * d0 \
                    + np.where(raw > s[-1], raw - s[-1], 0.0)[:, None] * d1
            spans.append((k, sg, med, raw, a, ext))
        allp = np.concatenate(pts)
        la, lo, _ = self.frame.enu_to_geodetic(allp[:, 0], allp[:, 1], 0.0)
        z = np.asarray(self.elev(np.asarray(la, f64), np.asarray(lo, f64)), f64).ravel()
        zp = None                   # (the ground past the parts' ends, at once; capped on the parts alone without it)
        past = [raw != a for _k, _sg, _med, raw, a, _e in spans]
        if decks and any(m.any() for m in past):
            try:
                ep = np.concatenate([sp[5][m] for sp, m in zip(spans, past)])
                ela, elo, _ = self.frame.enu_to_geodetic(ep[:, 0], ep[:, 1], 0.0)
                zp = np.asarray(self.elev(np.asarray(ela, f64), np.asarray(elo, f64)), f64).ravel()
            except Exception as e:
                log.debug("grading: ground past the ends under decks: %s: %s", type(e).__name__, e)
        out, k0, j0 = [], 0, 0
        for (k, sg, med, raw, a, ext), p, m in zip(spans, pts, past):
            zz = z[k0:k0 + 2 * k + 1]
            k0 += 2 * k + 1
            if decks:
                zx, q = zz, p
                if zp is not None and m.any():
                    zx, q = zz.copy(), ext
                    zx[m] = zp[j0:j0 + int(m.sum())]
                j0 += int(m.sum())
                capped = deck_capped(q, zx, decks)
                if capped is not zx:            # (the clipped samples at most the capped ground there)
                    zz = np.minimum(zz, np.interp(a, raw, capped))
            out.append(float(smooth_profile(median_profile(zz, PROFILE_STEP, med), PROFILE_STEP, sg)[k]))
        return out

    # ---- carriageways (carriageways.py): a motorway's roads and decks as one profile ----

    def _cw_cell_parts(self, k):
        """Cell k's parts of the motorway and trunk families: [(key, line (n, 2), is_bridge, cls, feature id, level,
        half width)], each once (level: a bridge's OSM layer, 1 without one; 0 on the ground)."""
        memo = self.__dict__.setdefault("_cw_cells", {})
        sk = str(k)
        with self.lock:
            got = memo.get(sk)
        if got is not None:
            return got
        c = self._cell_enu(k)
        out, seen = [], set()
        nb = len(c["bridges"])
        items = [(ln, False, cls, gid[0], 0.0, float(h)) for (ln, h, cls), gid in zip(c["ground"], c["ground_ids"])]
        items += [(ln, True, cls, fid, float(lv), float(h))
                  for ln, cls, fid, lv, h in zip(c["bridges"], c.get("bridge_cls", [None] * nb),
                                                 c.get("bridge_ids", [None] * nb), c.get("bridge_levels", [1.0] * nb),
                                                 c.get("bridge_half", [4.0] * nb))]
        for ln, br, cls, fid, lv, half in items:
            if _cw.family(cls) is None or len(ln) < 2:
                continue
            ln = np.asarray(ln, np.float64)[:, :2]
            key = ("b" if br else "g", round(float(ln[0, 0]), 1), round(float(ln[0, 1]), 1),
                   round(float(ln[-1, 0]), 1), round(float(ln[-1, 1]), 1))
            if key in seen:
                continue
            seen.add(key)
            out.append((key, ln, br, str(cls), fid, lv, half))
        with self.lock:
            if len(memo) > 512:
                memo.clear()
            memo[sk] = out
        return out

    def _z_raw(self, p):
        """The raw elevation at points p (n, 2) of this frame."""
        p = np.asarray(p, np.float64).reshape(-1, 2)
        a, b, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
        return np.asarray(self.elev(np.asarray(a, np.float64), np.asarray(b, np.float64)), np.float64).ravel()

    def _cw_region(self, home):
        """Cell home's carriageways: the family parts within CW_MARGIN_M of it (from every cell there, each once), their
        chains (carriageways.chains) and those solved together (_cw_solve): {"parts": [...], "chains": [...],
        "prof": {part key: (chain samples xy, z)}}, cached. Every road and deck is answered by the region of the cell
        holding its middle, so every cell and process asking gets the same heights."""
        memo = self.__dict__.setdefault("_cw_regions", {})
        hk = str(home)
        with self.lock:
            got = memo.get(hk)
        if got is not None:
            return got
        # (the run's other processes: the first to need a region solves it, the rest read it; each road-tile process
        # solving the same regions for itself, a worker's tiles waited past earthd's 300 s, 2026-11-02)
        sh, mine = self.shared, False
        hkey = (int(home.z), int(home.x), int(home.y)) if hasattr(home, "z") else None
        tag = f"{float(self.frame.lat0):.6f}_{float(self.frame.lon0):.6f}"
        if sh is not None and hkey is not None and hasattr(sh, "region_claim"):
            try:
                got, mine = sh.region_claim(tag, hkey)
            except Exception as e:      # (no shared copy: solved here)
                log.debug("grading: carriageways of %s: shared: %s: %s", hk, type(e).__name__, e)
                got, mine = None, False
            if got is not None:
                with self.lock:
                    if len(memo) > 64:
                        memo.clear()
                    memo[hk] = got
                return got
        try:
            got = self._cw_region_calc(home, hk)
            if mine and not got.get("failed"):
                sh.region_store(tag, hkey, got)
        finally:
            if mine:
                sh.region_release(tag, hkey)
        with self.lock:
            if len(memo) > 64:
                memo.clear()
            memo[hk] = got
        return got

    def _cw_region_calc(self, home, hk):
        f64 = np.float64
        lon0, lat0, lon1, lat1 = (float(v) for v in home.bounds())
        ce, cn, _ = self.frame.geodetic_to_enu(np.array([lat0, lat0, lat1, lat1]), np.array([lon0, lon1, lon0, lon1]),
                                               0.0)
        box_lo = np.array([np.min(ce), np.min(cn)]) - CW_MARGIN_M
        box_hi = np.array([np.max(ce), np.max(cn)]) + CW_MARGIN_M
        pad = CW_MARGIN_M / 111320.0
        mid_la = (lat0 + lat1) / 2
        keys = sorted(set(mapcells.cells_for_bbox(lat0 - pad, lon0 - pad / max(0.05, math.cos(math.radians(mid_la))),
                                                  lat1 + pad, lon1 + pad / max(0.05, math.cos(math.radians(mid_la))))),
                      key=str)
        parts, seen = [], set()
        try:
            cell_parts = [self._cw_cell_parts(k) for k in keys]
        except Exception as e:      # (a cell that cannot be read: no carriageways here, the rules as before)
            log.debug("grading: carriageways of %s: %s: %s", hk, type(e).__name__, e)
            return {"parts": [], "prof": {}, "failed": True}
        for cp in cell_parts:
            for q in cp:
                if q[0] in seen:
                    continue
                if ((q[1] >= box_lo) & (q[1] <= box_hi)).all(1).any():
                    seen.add(q[0])
                    parts.append(q)
        parts.sort(key=lambda q: str(q[0]))
        chains = []
        for seq in _cw.chains(parts):
            chains.append(([parts[i] for i, _r in seq], [(n, r) for n, (_i, r) in enumerate(seq)]))
        chains = [ch for ch in chains if any(p_[2] for p_ in ch[0]) or len(ch[0]) > 0]
        prof = {}
        try:
            if chains:
                prof = self._cw_cluster_solve(chains)
        except Exception as e:      # the rules as before rather than no grading
            import revisit
            revisit.fault()         # incomplete profiles must not become a persistent plan/region
            log.warning("grading: carriageways of %s: %s: %s", hk, type(e).__name__, e)
            return {"parts": parts, "prof": {}, "failed": True}
        return {"parts": parts, "prof": prof}

    def _cw_cluster_solve(self, chains):
        f64 = np.float64
        data, samples = [], []
        for ch in chains:
            parts, order = ch
            xy, s, pi, br, fe = _cw.sample(parts, order)
            la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            raw = np.asarray(self.elev(np.asarray(la, f64), np.asarray(lo, f64)), f64).ravel()
            ok = np.isfinite(raw)
            if not ok.any():
                raw = np.zeros(len(raw))
                ok[:] = True
            elif not ok.all():                          # (no elevation there: the nearest that is)
                raw = np.interp(s, s[ok], raw[ok])
            g = ~br
            if g.sum() >= 2:
                t = np.interp(s, s[g], raw[g])          # (the ground across the bridges, for the filters)
                t = smooth_profile(median_profile(t, _cw.STEP_M, MEDIAN_M), _cw.STEP_M, SIGMA_MAJOR_M)
            else:
                t = raw.copy()
            w = _cw.weights(br, fe) * _cw.GROUND_W
            # (its ends, where it meets the rest of the network, on their ground: unless on a bridge, a joint of decks)
            for e in (0, len(w) - 1):
                if not br[e]:
                    w[e] = _cw.ANCHOR_W
            if g.sum() < 2:
                w = np.where(np.arange(len(w)) < 0, 0.0, 1e-3)      # (all bridge: held lightly to the ground)
            # the steepest of its parts' classes' limits (a chain is of one family; a link's limits are looser)
            gr = max(_cw.limits(p_[3])[0] for p_ in parts)
            K = min(_cw.limits(p_[3])[1] for p_ in parts)
            data.append(dict(t=t, w=w, grade=gr, K=K, lo=np.full(len(s), -np.inf), hi=np.full(len(s), np.inf)))
            samples.append((xy, s, pi, br, raw))
        # bounds: tracks under its decks; other decks (not of the family) over its roads
        allpts = np.concatenate([x[0] for x in samples])
        la, lo, _ = self.frame.enu_to_geodetic(allpts[:, 0], allpts[:, 1], 0.0)
        pad = 40.0 / 111320.0
        keys = sorted(set(mapcells.cells_for_points(np.asarray(la, f64), np.asarray(lo, f64), pad,
                                                    pad / max(0.05, math.cos(math.radians(float(np.mean(la))))))),
                      key=str)
        cs = [self._cell_enu(k) for k in keys]
        rails = [np.asarray(r, f64)[:, :2] for c in cs for r in c.get("rail_ground", ()) if len(r) >= 2]
        fam_keys = {p_[0] for ch in chains for p_ in ch[0]}
        nonfam = []
        for c in cs:
            for line, bc in zip(c["bridges"], c.get("bridge_cls", [None] * len(c["bridges"]))):
                if bc is not None and _cw.family(bc) is None and len(line) >= 2:
                    nonfam.append(np.asarray(line, f64)[:, :2])

        def zraw(q):
            a, b_, _ = self.frame.enu_to_geodetic(np.array([q[0]]), np.array([q[1]]), 0.0)
            return float(np.asarray(self.elev(np.asarray(a, f64), np.asarray(b_, f64)), f64).ravel()[0])

        def zat(p):
            p = np.asarray(p, f64).reshape(-1, 2)
            a, b_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(np.asarray(a, f64), np.asarray(b_, f64)), f64).ravel()
        # the other roads on the ground (under the decks: they dip to clear them, except on low ground, where the
        # deck rises instead: a dug road there is under the water table, Wezep's street under the A28)
        streets = [(np.asarray(line, f64)[:, :2], float(half)) for c in cs for line, half, gc in c["ground"]
                   if _cw.family(gc) is None and gc not in _xs.PATHS and gc not in UNGRADED and len(line) >= 2]

        def nearest(n, part_i, q):
            xy, s, pi, br, raw = samples[n]
            cand = np.flatnonzero(pi == part_i)
            return int(cand[np.argmin(np.hypot(*(xy[cand] - q).T))])
        # (bounding boxes: a deck meets only the streets and tracks near it, a scan of them all at each crossing was
        # most of a region's time: 43 s of a Perth cell's 110, 2026-11-02)
        def boxes_of(ls):
            if not ls:
                return np.zeros((0, 2)), np.zeros((0, 2))
            return np.array([l_.min(0) for l_ in ls]), np.array([l_.max(0) for l_ in ls])
        st_lo, st_hi = boxes_of([st[0] for st in streets])
        rl_lo, rl_hi = boxes_of(rails)

        def near_of(lo_, hi_, line, pad=2.0):
            a, b_ = line.min(0) - pad, line.max(0) + pad
            return np.flatnonzero(((lo_ <= b_) & (hi_ >= a)).all(1))
        for n, ch in enumerate(chains):
            parts = ch[0]
            for i, p_ in enumerate(parts):
                clear = CW_HEADROOM_M + roadsmod.deck_depth(roadsmod.geom_span([p_[1]])) + ROAD_LIFT
                rails_n = [rails[m] for m in near_of(rl_lo, rl_hi, p_[1])] if p_[2] and rails else []
                streets_n = [streets[m] for m in near_of(st_lo, st_hi, p_[1])] if p_[2] and streets else []
                if p_[2] and rails_n:
                    for c in roadsmod._crossings([p_[1]], rails_n):
                        j = nearest(n, i, np.asarray(c[2], f64))
                        data[n]["lo"][j] = max(data[n]["lo"][j], zraw(c[2]) + clear)
                if p_[2] and streets_n:
                    for c in roadsmod._crossings([p_[1]], [st[0] for st in streets_n]):
                        L = float(np.hypot(*np.diff(p_[1], axis=0).T).sum())
                        if c[1] < APPROACH_MIN_CROSS_M or c[1] > L - APPROACH_MIN_CROSS_M:
                            continue
                        q = np.asarray(c[2], f64)
                        sl = min(streets_n, key=lambda st: _seg_dist(st[0], q))[0]
                        tl = _xs.trusted_under(q, _xs.dir_at(sl, q), 8.0, zat)
                        if tl is None or tl >= seatiles.SEA_E + _xs.LOW_M:
                            continue
                        j = nearest(n, i, q)
                        data[n]["lo"][j] = max(data[n]["lo"][j], tl + clear)
                if not p_[2] and nonfam and CW_NONFAM_HI:
                    for c in roadsmod._crossings(nonfam, [p_[1]]):
                        b = nonfam[c[0]]
                        L = float(np.hypot(*np.diff(b, axis=0).T).sum())
                        if c[1] < APPROACH_MIN_CROSS_M or c[1] > L - APPROACH_MIN_CROSS_M:
                            continue
                        ze = np.array([zraw(b[0]) + self._end_raised(b[0]), zraw(b[-1]) + self._end_raised(b[-1])])
                        deck = float(ze[0] + (ze[1] - ze[0]) * c[1] / max(L, 1e-9))
                        bc = roadsmod.deck_road_clear(roadsmod.deck_depth(roadsmod.geom_span([b]))) + ROAD_LIFT
                        j = nearest(n, i, np.asarray(c[2], f64))
                        # (lowered under it CW_MAX_DIG_M at most: past that the deck is too low, as before)
                        data[n]["hi"][j] = min(data[n]["hi"][j], max(deck - bc, data[n]["t"][j] - CW_MAX_DIG_M))
        # the ground under any deck (and beside it, its abutments' fill) is not the road's: the elevation fills the gap
        # under a bridge at about the deck's height (a freeway read 2 m up under each street bridge over it)
        decks_all = [np.asarray(line, f64)[:, :2] for c in cs for line in c["bridges"] if len(line) >= 2]
        if decks_all:
            A = np.concatenate([d[:-1] for d in decks_all])
            B = np.concatenate([d[1:] for d in decks_all])
            AB = B - A
            L2 = np.maximum((AB * AB).sum(1), 1e-12)
            for n, (xy, s_, pi, br, raw) in enumerate(samples):
                g_ = np.flatnonzero(~br)
                if not len(g_):
                    continue
                lo_q, hi_q = xy[g_].min(0) - 30.0, xy[g_].max(0) + 30.0
                near = np.flatnonzero(~((np.maximum(A, B) < lo_q).any(1) | (np.minimum(A, B) > hi_q).any(1)))
                if not len(near):
                    continue
                q = xy[g_]
                t_ = np.clip(((q[:, None, :] - A[near][None]) * AB[near][None]).sum(2) / L2[near][None], 0.0, 1.0)
                d = np.hypot(*(q[:, None, :] - (A[near][None] + t_[..., None] * AB[near][None])).transpose(2, 0, 1)).min(1)
                u = np.clip((d - CW_UNDER_M) / CW_UNDER_M, 0.0, 1.0)
                data[n]["w"][g_] *= u * u * (3.0 - 2.0 * u)
        # couplings: ends at a branch point equal; one carriageway over another
        couplings = []
        ends = []
        for n, (xy, s, pi, br, raw) in enumerate(samples):
            ends += [(n, 0, xy[0]), (n, len(xy) - 1, xy[-1])]
        for a in range(len(ends)):
            for b in range(a + 1, len(ends)):
                na, ia, pa = ends[a]
                nb, ib, pb = ends[b]
                if na != nb and np.hypot(*(pa - pb)) <= _cw.COUPLE_M:
                    couplings.append((na, ia, nb, ib, "eq", 0.0))
        # (an end meeting the inside of another chain: a slip road at its overpass road's node, solved with it)
        joined = _cw.branch_ties([x[0] for x in samples], [x[3] for x in samples])
        couplings += [(na, ia, nb, jb, "eq", 0.0) for na, ia, nb, jb in joined]
        cross = []
        boxes = [(x[0].min(0), x[0].max(0)) for x in samples]
        for na in range(len(chains)):
            for nb in range(na + 1, len(chains)):
                if (boxes[na][1] < boxes[nb][0]).any() or (boxes[nb][1] < boxes[na][0]).any():
                    continue
                hits = roadsmod._crossings_by([p_[1] for p_ in chains[na][0]], [p_[1] for p_ in chains[nb][0]])
                for ib, lst in enumerate(hits):
                    pb = chains[nb][0][ib]
                    for c in lst:
                        ia = int(c[0])
                        pa = chains[na][0][ia]
                        if pa[2] == pb[2] and not pa[2]:
                            continue            # (two roads on the ground: a junction, not a separation)
                        q = np.asarray(c[2], f64)
                        ua, ub = _xs.dir_at(pa[1], q), _xs.dir_at(pb[1], q)
                        sin = abs(ua[0] * ub[1] - ua[1] * ub[0])
                        # (two decks mapped at different levels: a separation however shallow the angle, the ramps of a
                        # stack cross at a few degrees; else lines lying along each other are a duplicate or a merge)
                        stacked = pa[2] and pb[2] and _cw_level(pa) != _cw_level(pb)
                        if sin < CW_MIN_SIN and not (stacked and sin >= CW_STACK_SIN):
                            continue
                        if min(np.hypot(*(pa[1][0] - q)), np.hypot(*(pa[1][-1] - q)), np.hypot(*(pb[1][0] - q)),
                               np.hypot(*(pb[1][-1] - q))) < 2.0:
                            continue            # (joined there)
                        cross.append((na, ia, nb, ib, q, sin, ua))
        # the interchange built from the ground up (OSM's layer the order, 2026-11-02): each chain's level is its
        # highest part's (a bridge's layer, 1 without one; 0 for a chain all on the ground). The lowest level's chains
        # are solved first, on the elevation, and kept; then each level up, over and joined to what is below and not
        # moving it: a crossing of a kept chain is a bound (its height plus the clearance, wherever the two overlap),
        # a branch point on one is a pin. Only two parts of one level crossing within a level are ordered by a
        # solve of that level without them.
        stage = [max(_cw_level(p_) for p_ in ch[0]) for ch in chains]
        # (one road through a branch point: the chains either side continuing the same way (its parts there of one
        # feature) are built at one level, together: Graham Farmer's ground beyond its deck, solved first and kept,
        # made the deck dive to it)
        par = list(range(len(chains)))

        def root(n):
            while par[n] != n:
                par[n] = par[par[n]]
                n = par[n]
            return n
        for a_, i_, b_, j_, kind, _v in couplings:
            if kind != "eq":
                continue
            pa_ = chains[a_][0][int(samples[a_][2][i_])]
            pb_ = chains[b_][0][int(samples[b_][2][j_])]
            if pa_[4] is not None and pa_[4] == pb_[4]:
                par[root(a_)] = root(b_)
        for na, _ia, nb, _jb in joined:
            par[root(na)] = root(nb)
        # (the roads leaving a deck's end: its approaches rise with it. Solved first, on the ground, they pinned the deck to
        # it, a 95 m span that must clear a road 7 m under it left 2 m over its own approaches)
        for a_, i_, b_, j_, kind, _v in couplings:
            if kind == "eq" and bool(samples[a_][3][i_]) != bool(samples[b_][3][j_]):
                par[root(a_)] = root(b_)
        top = {}
        for n in range(len(chains)):
            top[root(n)] = max(top.get(root(n), 0.0), stage[n])
        stage = [top[root(n)] for n in range(len(chains))]
        seps = []           # (upper chain, its sample, lower chain, its sample, clearance) per overlap sample; or a tie
        ties = []           # (na, ia, nb, ib, q, sin, ua): one level's two parts, the order its solve's
        for na, ia, nb, ib, q, sin, ua in cross:
            pa, pb = chains[na][0][ia], chains[nb][0][ib]
            if pa[2] and not pb[2]:
                a_up = True
            elif pb[2] and not pa[2]:
                a_up = False
            elif _cw_level(pa) != _cw_level(pb):
                a_up = _cw_level(pa) > _cw_level(pb)
            elif stage[na] != stage[nb]:
                a_up = stage[na] > stage[nb]        # (one level's decks: the one of the higher-built chain over)
            else:
                ties.append((na, ia, nb, ib, q, sin, ua))
                continue
            seps += self._cw_overlap(chains, samples, nearest, (na, ia, nb, ib, q, sin, ua), a_up)
        zs = [None] * len(chains)
        # (one budget for the whole region, every level's solves: per solve, the High Five's six levels' took minutes
        # and every process needing the region waited on it, 2026-11-02)
        deadline = time.monotonic() + _cw.BUDGET_S
        for L in sorted(set(stage)):
            idx = [n for n in range(len(chains)) if stage[n] == L]
            loc = {n: m for m, n in enumerate(idx)}
            d_ = [dict(data[n], t=data[n]["t"].copy(), w=data[n]["w"].copy(), lo=data[n]["lo"].copy(),
                       hi=data[n]["hi"].copy()) for n in idx]
            cp = []
            for a_, i_, b_, j_, kind, v in couplings:          # (the branch points: "eq")
                if a_ in loc and b_ in loc:
                    cp.append((loc[a_], i_, loc[b_], j_, kind, v))
                elif a_ in loc and zs[b_] is not None:
                    d = d_[loc[a_]]
                    d["lo"][i_] = max(d["lo"][i_], zs[b_][j_])
                    d["hi"][i_] = min(d["hi"][i_], zs[b_][j_])
                elif b_ in loc and zs[a_] is not None:
                    d = d_[loc[b_]]
                    d["lo"][j_] = max(d["lo"][j_], zs[a_][i_])
                    d["hi"][j_] = min(d["hi"][j_], zs[a_][i_])

            def bound(lst, cp_):
                for nu, ju, nl, jl, clear in lst:
                    if nu in loc and nl in loc:
                        cp_.append((loc[nu], ju, loc[nl], jl, "ge", clear))
                    elif nu in loc and zs[nl] is not None:      # (over a kept chain)
                        d_[loc[nu]]["lo"][ju] = max(d_[loc[nu]]["lo"][ju], zs[nl][jl] + clear)
                    elif nl in loc and zs[nu] is not None:      # (under a kept chain's deck)
                        d_[loc[nl]]["hi"][jl] = min(d_[loc[nl]]["hi"][jl], zs[nu][ju] - clear)
            bound(seps, cp)
            mine = [t_ for t_ in ties if t_[0] in loc]
            def solve_stage():
                info = {}
                profiles = _cw.solve(d_, cp, deadline=deadline, diagnostics=info)
                if not info["converged"]:
                    raise RuntimeError(f"carriageway level {L}: {info['status']} (feasible={info['feasible']})")
                return profiles

            if mine:
                pre = solve_stage()
                tie_seps = []
                for na, ia, nb, ib, q, sin, ua in mine:
                    a_up = pre[loc[na]][nearest(na, ia, q)] >= pre[loc[nb]][nearest(nb, ib, q)]
                    tie_seps += self._cw_overlap(chains, samples, nearest, (na, ia, nb, ib, q, sin, ua), a_up)
                bound(tie_seps, cp)
            got = solve_stage()
            for n in idx:
                zs[n] = got[loc[n]]
        couplings = couplings + [(nu, ju, nl, jl, "ge", c_) for nu, ju, nl, jl, c_ in seps]
        if os.environ.get("BNE_CW_DUMP"):
            import pickle
            with open(os.environ["BNE_CW_DUMP"] + f".{len(chains)}.{len(couplings)}.pkl", "wb") as fh:
                pickle.dump(dict(data=data, couplings=couplings, zs=zs, samples=samples,
                                 parts=[[(p_[0], p_[2], p_[3]) for p_ in ch[0]] for ch in chains]), fh)
        out = {}
        for n, ch in enumerate(chains):
            for p_ in ch[0]:
                out[p_[0]] = (samples[n][0], zs[n])
        moved = [float(np.max(np.abs((zs[n] - d["t"])[~samples[n][3]]))) if (~samples[n][3]).any() else 0.0
                 for n, d in enumerate(data)]
        log.debug("grading: carriageway cluster of %d chains, %d couplings: moved at most %.1f m from the ground",
                  len(chains), len(couplings), max(moved) if moved else 0.0)
        if os.environ.get("BNE_CW_DEBUG") and moved:
            n = int(np.argmax(moved))
            xy, s_, pi, br, raw = samples[n]
            d = np.where(~br, np.abs(zs[n] - data[n]["t"]), 0.0)
            j = int(np.argmax(d))
            log.debug("grading: CWDBG chain %d (%s) at %s: t %.1f z %.1f raw %.1f w %.2f; lo %s hi %s; parts %s",
                      n, chains[n][0][0][3], np.round(xy[j]), data[n]["t"][j], zs[n][j], raw[j], data[n]["w"][j],
                      np.round(data[n]["lo"][np.isfinite(data[n]["lo"])], 1)[:5],
                      np.round(data[n]["hi"][np.isfinite(data[n]["hi"])], 1)[:5], [(p_[2], len(p_[1])) for p_ in chains[n][0]])
        return out

    def _cw_overlap(self, chains, samples, nearest, x, a_up):
        """The clearance couplings of crossing x = (na, ia, nb, ib, q, sin, ua), a over b when a_up: (upper chain, its
        sample, lower chain, its sample, clearance), at the crossing and along the length the two decks overlap (at a
        shallow angle tens of metres, a stack's ramps)."""
        f64 = np.float64
        na, ia, nb, ib, q, sin, ua = x
        pa, pb = chains[na][0][ia], chains[nb][0][ib]
        (nu, iu, pu), (nl, il, pl_) = ((na, ia, pa), (nb, ib, pb)) if a_up else ((nb, ib, pb), (na, ia, pa))
        clear = CW_HEADROOM_M + roadsmod.deck_depth(roadsmod.geom_span([pu[1]])) + ROAD_LIFT
        wide = _cw_half(pa) + _cw_half(pb)
        run = min(CW_OVERLAP_MAX_M, wide / max(sin, 1e-3))
        st = CW_OVERLAP_STEP_M      # (between them the curve limits keep the deck smooth: fewer couplings)
        offs = [0.0] + [d for k in range(1, 64) for d in (k * st, -k * st) if k * st <= run]
        out, seen = [], set()
        for d in offs:
            qd = q + d * np.asarray(ua, f64)
            ju, jl = nearest(nu, iu, qd), nearest(nl, il, qd)
            xu = samples[nu][0][ju]
            if d and (np.hypot(*(xu - qd)) > 3.0 or _seg_dist(pl_[1], xu) > wide):
                continue        # (past the overlap, or off these parts)
            if (ju, jl) not in seen:
                seen.add((ju, jl))
                out.append((nu, ju, nl, jl, clear))
        return out

    def _cw_z_at(self, line, pts, fid=None):
        """Heights of points pts (n, 2) on the family part line (of feature fid) from its carriageway's profile, or
        None."""
        line = np.asarray(line, np.float64)[:, :2]
        mid = line[len(line) // 2]
        la, lo, _ = self.frame.enu_to_geodetic(np.array([mid[0]]), np.array([mid[1]]), 0.0)
        try:
            reg = self._cw_region(mapcells.cell_of(float(la[0]), float(lo[0])))
        except Exception as e:      # (the rules as before)
            log.debug("grading: carriageway at %s: %s: %s", np.round(mid, 1), type(e).__name__, e)
            return None
        if reg.get("failed"):
            import revisit
            revisit.fault()         # also on later reads of the session's memoized failed region
            return None
        best = None
        for q in reg["parts"]:
            if fid is not None and q[4] != fid:
                continue
            if (q[1].max(0) < mid - 2.0).any() or (q[1].min(0) > mid + 2.0).any():
                continue
            d = _seg_dist(q[1], mid)
            if d <= 1.0 and (best is None or d < best[0]):
                best = (d, q)
        if best is None:
            return None
        prof = reg["prof"].get(best[1][0])
        if prof is None:
            return None
        xy, z = prof
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        a, b = xy[:-1], xy[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        out = np.empty(len(pts))
        for n, q in enumerate(pts):
            t = np.clip(((q - a) * ab).sum(1) / L2, 0.0, 1.0)
            d = np.hypot(*(a + t[:, None] * ab - q).T)
            j = int(np.argmin(d))
            if d[j] > 3.0:
                return None
            out[n] = z[j] + (z[j + 1] - z[j]) * t[j]
        return out

    def _apply_carriageways(self, plans, specs):
        """CW_ON: the plans of motorway- and trunk-family roads set to their carriageways' profiles (in place)."""
        for n, ((fid, p), sp) in enumerate(zip(plans, specs)):
            if sp is None or _cw.family(getattr(sp, "cls", None) + ("_link" if _is_link(sp) else "")) is None:
                continue
            xy = np.asarray(p.xy, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            z = self._cw_z_at(xy, xy, str(fid).split(":")[0])
            if z is None:
                continue
            np_ = Plan(p.xy, z, p.half, p.abut, p.cut, p.road_half)
            np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
            np_.steep = getattr(p, "steep", False)
            plans[n] = (fid, np_)

    def _pin_cw_ends(self, plans, specs):
        """SEAMS_ON: each motorway- or trunk-family plan's end that is a bridge abutment (Plan.abut) back to the height
        of its carriageway's profile there, which is the height the deck was drawn at (roads.DECK_Z_HOOK). The passes
        after _apply_carriageways (the grade guard, the junction snaps) fit a plan's end to the roads and the ground
        beside it, so the road came up to its deck's end 1.4-1.7 m under it: a step into the bridge (the M-21 over a
        street in Nemiia, 48.4656 N 27.7711 E, 2026-11-08). The profile is taken back where the plan has it within
        3 m, the plan's own offset there where not; either eased out over SEAM_GRADE (SEAM_RUN_MIN_M .. MAX_M) so no
        new step lies further on. Plans changed in place; whether any was."""
        done = False
        pins = []                           # (x, y, z) of the ends pulled back to their decks
        for n, ((fid, p), sp) in enumerate(zip(plans, specs)):
            if sp is None or _cw.family(getattr(sp, "cls", None) + ("_link" if _is_link(sp) else "")) is None:
                continue
            ab = getattr(p, "abut", None)
            if ab is None or not (ab[0] or ab[1]):
                continue
            xy = np.asarray(p.xy, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            base = str(fid).split(":")[0]
            z = np.array(p.z, np.float64)
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            prof = self._cw_z_at(xy, xy, base)      # (None where a vertex is off the profile: ends read one by one)
            out = z.copy()
            for end, flag in ((0, ab[0]), (-1, ab[1])):
                if not flag:
                    continue
                at = self._cw_z_at(xy, xy[end:end + 1] if end == 0 else xy[end:], base)
                if at is None:
                    continue
                d = float(np.asarray(at)[0]) - float(z[end])
                if abs(d) < SEAM_MIN_M:
                    pins.append((float(xy[end][0]), float(xy[end][1]), float(z[end]) + d))     # (the streets' mouths)
                    continue
                run = float(np.clip(abs(d) / SEAM_GRADE, SEAM_RUN_MIN_M, SEAM_RUN_MAX_M))
                a = s if end == 0 else s[-1] - s
                t = np.clip(1.0 - a / run, 0.0, 1.0)
                w = t * t * (3.0 - 2.0 * t)
                out = out + ((prof - z) if prof is not None else d) * w
                pins.append((float(xy[end][0]), float(xy[end][1]), float(z[end]) + d))
            if np.abs(out - z).max() > 1e-6:
                np_ = Plan(p.xy, out, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
                np_.steep = getattr(p, "steep", False)
                set_widen(np_, getattr(p, "widen", None))
                plans[n] = (fid, np_)
                done = True
        # the roads that end at the same abutment (a street off the bridge's end): down from the deck's height
        for n, ((fid, p), sp) in enumerate(zip(plans, specs)):
            if not pins or sp is None or _cw.family(getattr(sp, "cls", None) + ("_link" if _is_link(sp) else "")) is not None:
                continue
            xy = np.asarray(p.xy, np.float64)[:, :2]
            if len(xy) < 2:
                continue
            z = np.array(p.z, np.float64)
            s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
            out = z.copy()
            for end in (0, -1):
                near = [q for q in pins if math.hypot(q[0] - xy[end][0], q[1] - xy[end][1]) <= SEAM_SIDE_M]
                if not near:
                    continue
                d = float(np.mean([q[2] for q in near])) - float(z[end])
                a = s if end == 0 else s[-1] - s
                if abs(d) >= SEAM_MIN_M:
                    run = float(np.clip(abs(d) / SEAM_SIDE_GRADE, *SEAM_SIDE_RUN_M))
                    t = np.clip(1.0 - a / run, 0.0, 1.0)
                    out = out + d * t * t * (3.0 - 2.0 * t)
                out = self._hold_mouth(out, a)
            if np.abs(out - z).max() > 1e-6:
                np_ = Plan(p.xy, out, p.half, p.abut, p.cut, p.road_half)
                np_.rid, np_.max_wall, np_.bank = p.rid, p.max_wall, getattr(p, "bank", None)
                np_.steep = getattr(p, "steep", False)
                set_widen(np_, getattr(p, "widen", None))
                plans[n] = (fid, np_)
                done = True
        return done

    @staticmethod
    def _hold_mouth(z, a):
        """z (n,) of a street at the end of a deck, a (n,) the distance of each vertex from that end: the end's height held
        for SEAM_MOUTH_M (the wedge of the deck's level the junction makes beside the carriageway: 48.46547 N 27.77097 E,
        Nemiia, 2026-11-09: it stood 0.5 m over the street, as a step with a concrete edge), then back to the street's own
        profile at SEAM_MOUTH_GRADE. A street that does not meet its own profile again inside its length is left as it was."""
        n = len(z)
        order = np.argsort(a, kind="stable")
        za, aa = np.asarray(z, np.float64)[order], np.asarray(a, np.float64)[order]
        held = aa <= SEAM_MOUTH_M
        if n < 3 or held.sum() < 2 or held.all():
            return z
        out = za.copy()
        out[held] = za[0]
        prev, ap = za[0], max(float(aa[held][-1]), 0.0)
        for i in np.flatnonzero(~held):
            lim = SEAM_MOUTH_GRADE * (aa[i] - ap)
            dz = za[i] - prev
            if abs(dz) <= lim:
                res = np.asarray(z, np.float64).copy()
                res[order] = out
                return res
            prev += lim if dz > 0 else -lim
            ap = aa[i]
            out[i] = prev
        return z

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
            a = approach_add(line, at_end, lift, half, others,
                             MEASURED_SIDE_RUN if (len(got) > 4 and got[4] is not None) or (len(got) > 6 and got[5])
                             else SLOPE_RUN)
            if a is not None:
                best = max(best, float(a[-1] if at_end else a[0]))
        return best

    def _end_filled(self, line, at_end):
        """m: the ground at a bridge's first (at_end False) or last end as fill_abutment lays its approach there, read
        straight on past the end (the approach roads' own plans are not made yet): its embankment's top where the
        line ends down the slope, else the ground at the end. -inf when ABUT_FILL_ON is off."""
        if not ABUT_FILL_ON:
            return -np.inf
        b = np.asarray(line, np.float64)[:, :2]
        p, q = (b[-1], b[-2]) if at_end else (b[0], b[1])
        u = p - q
        n = float(np.hypot(*u))
        if n < 1e-9:
            return -np.inf
        d = np.arange(0.0, ABUT_FAR_M + 1e-6, PROFILE_STEP)
        pts = p[None] + d[:, None] * (u / n)[None]
        la, lo, _ = self.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
        try:
            z = np.asarray(self.elev(np.asarray(la), np.asarray(lo)), np.float64).reshape(-1)
        except Exception as e:      # (ground past the end unread: the end's own estimate, as before)
            log.debug("grading: fill past bridge end %s: %s: %s", np.round(p), type(e).__name__, e)
            return -np.inf
        return float(fill_abutment(z, False)[0])

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
        span_ll = []
        if f is not None and len(f):
            bs = roadsmod.bridge_specs(f)
            rs = roadsmod.road_specs(f, None, self.widths) if ground else [None] * len(f)
            placement = roadsmod.placement_context([f], None, self.widths)     # (as the road tiles draw them)
            classes = f.attrs.get("class")
            subtypes = f.attrs.get("subtype")
            for i in range(len(f)):
                if bs[i] is None and rs[i] is None:
                    cls = str(classes[i]) if classes is not None else ""
                    sub = str(subtypes[i]) if subtypes is not None else "road"
                    if sub == "road" and cls in WALK_SPAN_CLASSES:
                        for ring in roadsmod.bridge_parts(f, i):
                            ring = np.asarray(ring, np.float64)
                            if len(ring) >= 2:
                                span_ll.append(ring)
                    continue
                fid = str(f.ids[i])
                # (a bridge part's level: OSM's layer as Overture's level rules, 1 without one; stacked decks' order)
                blv = roadsmod.bridge_levels(f, i) if bs[i] is not None else []
                for kind, sp, ps in (("bridge", bs[i], roadsmod.bridge_parts(f, i) if bs[i] is not None else ()),
                                     ("ground", rs[i], roadsmod.ground_parts(f, i) if rs[i] is not None else ())):
                    # (keyed as in every cell's copy: roads.part_keys; a long road's copies number them differently)
                    for n, (j, ring) in enumerate(roadsmod.keyed(roadsmod.part_keys(f, i, kind) if ps else None, ps)):
                        ring = placement.ring(self.frame, f, i, np.asarray(ring, np.float64), sp, fid)
                        kk = (fid, kind, j)
                        lv = float(blv[n]) if kind == "bridge" and n < len(blv) else 0.0
                        if len(ring) >= 2 and (kk not in parts or len(ring) > len(parts[kk][0])):
                            parts[kk] = (ring, sp, lv)
        # the rail bridges (railways.DRAWN lines' bridge pieces): the roads under them dip as under a road overpass
        # (_lower_underpasses), nothing else (no ramps, no abutments: a line's deck is never raised for a road).
        # (GitHub #311 / #345 / #364: the track lay across the road under a rail bridge)
        rail, rail_g = [], []
        if f is not None and len(f) and f.attrs.get("subtype") is not None:
            import railways
            sub, rcls = f.attrs["subtype"], f.attrs.get("class")
            for i in range(len(f)):
                got = railways.drawn(f, i, bridges=True) if str(sub[i]) == "rail" else None
                if got is None:
                    continue
                for kind, rings in railways._pieces(f, i):
                    if kind == "bridge":
                        rail += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
                    elif kind == "ground" and ground and got[0] != "tram":
                        # (and the tracks on the ground: a road bridge's approaches rise over them as over a road,
                        # GitHub #324)
                        rail_g += [np.asarray(r, np.float64) for r in rings if len(r) >= 2]
        bridges, ground_l, ends, end_of, bcls, ground_ids, bids, blevels, bhalf = [], [], [], [], [], [], [], [], []
        items = sorted(parts.items())
        if items:               # (the frame's conversion once for every part: it is per call)
            allr = np.concatenate([ring for _k, (ring, _sp, _lv) in items])
            ae, an, _ = self.frame.geodetic_to_enu(allr[:, 1], allr[:, 0], 0.0)
            axy = np.stack([np.asarray(ae, np.float64), np.asarray(an, np.float64)], axis=1)
        k0 = 0
        for (fid, kind, j), (ring, sp, lv) in items:
            line = axy[k0:k0 + len(ring)]
            k0 += len(ring)
            if kind == "bridge":
                end_of += [len(bridges), len(bridges)]
                ends += [line[0], line[-1]]
                bridges.append(line)
                bids.append(fid)
                blevels.append(lv)
                # (one half width a bridge: two merged changes each appended it, and bridge i got bridge i // 2's)
                bhalf.append(float(getattr(sp, "width", None) or 8.0) / 2)
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
        span_xy = []
        if span_ll:
            alls = np.concatenate(span_ll)
            se, sn, _ = self.frame.geodetic_to_enu(alls[:, 1], alls[:, 0], 0.0)
            sxy = np.stack([np.asarray(se, np.float64), np.asarray(sn, np.float64)], axis=1)
            k0 = 0
            for ring in span_ll:
                span_xy.append(sxy[k0:k0 + len(ring)])
                k0 += len(ring)
        return {"bridges": bridges, "bridge_cls": bcls, "bridge_ids": bids, "bridge_levels": blevels,
                "bridge_half": bhalf, "ground": ground_l, "ground_ids": ground_ids,
                "end_of": end_of, "span_bridges": span_xy,
                "ends": np.asarray(ends, np.float64).reshape(-1, 2), "rail_bridges": rail_xy, "rail_ground": rail_gxy}

    def _decks_ll(self, lonlat):
        """_decks_near of the cell holding the middle of a lon/lat part (a pin height's plan: the cell's own plans
        read the same decks)."""
        if not UNDER_DECK_ON or lonlat is None or not len(lonlat):
            return []
        mid = np.asarray(lonlat, np.float64)[len(lonlat) // 2]
        return self._decks_near(mapcells.cell_of(float(mid[1]), float(mid[0])))

    def _decks_near(self, key):
        """[(line (k, 2), half width)] of the road and rail decks within UNDER_DECK_PAD_M of cell key (deck_capped's),
        each once, made once per cell; [] with UNDER_DECK_ON off."""
        if not UNDER_DECK_ON or key is None:
            return []
        memo = self.__dict__.setdefault("_decks_memo", {})
        with self.lock:
            got = memo.get(str(key))
        if got is not None:
            return got
        lon0, lat0, lon1, lat1 = (float(v) for v in key.bounds())
        pla = UNDER_DECK_PAD_M / 111320.0
        plo = pla / max(0.05, math.cos(math.radians((lat0 + lat1) / 2)))
        keys = sorted(set(mapcells.cells_for_bbox(lat0 - pla, lon0 - plo, lat1 + pla, lon1 + plo)) | {key}, key=str)
        out, seen, short = [], set(), False
        for k in keys:
            try:
                # (its bridges only: the decks and their widths are in both tables; the ground roads of every cell
                # within UNDER_DECK_PAD_M were ~0.35 s of each Manhattan road tile, 2026-11-03)
                c = self._cell_enu(k, ground=False)
            except Exception as e:      # (that cell's decks left out rather than no plans; not kept: asked again)
                log.debug("grading: decks of %s: %s: %s", k, type(e).__name__, e)
                short = True
                continue
            halves = c.get("bridge_half") or [0.0] * len(c["bridges"])
            for line, half in (list(zip(c["bridges"], halves))
                               + [(r, railwaysmod.DECK_HALF_M) for r in c.get("rail_bridges", ())]
                               + [(r, WALK_SPAN_HALF_M) for r in c.get("span_bridges", ())]):
                if len(line) < 2 or half <= 0:
                    continue
                k2 = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), round(float(line[-1, 0]), 1), len(line))
                if k2 not in seen:
                    seen.add(k2)
                    out.append((line, half))
        out = _Decks(out)
        if not short:
            with self.lock:
                memo[str(key)] = out
        return out

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
        """_end_lift_raw with the roads beside the ramp that lead onto another abutment near it, itself raised at least
        TWIN_SHARE as much, left out of its caps: the two rise together (a divided road's two carriageways onto their
        decks, side by side 10 m apart: each capped the other's ramp to nothing, both abutments 3.3 m under the decks'
        measured ends, Springfield's Old Keene Mill Road, 2026-10-31, approachlidar.py)."""
        got = self._end_lift_raw(xy)
        if got is None or not TWIN_ON or not got[1]:
            return got
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), 0.0)
        pad = (TWIN_M + APPROACH_TOUCH_M) / 111320.0
        keys = sorted(set(mapcells.cells_for_points(la, lo, pad, pad / max(0.05, math.cos(math.radians(float(la[0])))))),
                      key=str)
        ends = [np.asarray(self._cell_enu(k, ground=False)["ends"], np.float64).reshape(-1, 2) for k in keys]
        ends = np.unique(np.round(np.concatenate(ends), 3), axis=0) if ends else np.zeros((0, 2))
        if not len(ends):
            return got
        d = np.hypot(ends[:, 0] - xy[0], ends[:, 1] - xy[1])
        cand = ends[(d > APPROACH_TOUCH_M) & (d <= TWIN_M)]
        if not len(cand):
            return got
        keep, lifts = [], {}
        for line, half in got[1]:
            line_ = np.asarray(line, np.float64)
            twin = False
            for p in (line_[0], line_[-1]):
                dd = np.hypot(cand[:, 0] - p[0], cand[:, 1] - p[1])
                k = int(np.argmin(dd))
                if dd[k] <= APPROACH_TOUCH_M:
                    kk = (round(float(cand[k, 0]), 2), round(float(cand[k, 1]), 2))
                    if kk not in lifts:
                        other = self._end_lift_raw(cand[k])
                        lifts[kk] = 0.0 if other is None else float(other[0])
                    twin = twin or lifts[kk] >= TWIN_SHARE * float(got[0])
            if not twin:
                keep.append((line, half))
        return (got[0], keep) + tuple(got[2:])

    def _xsolve(self, bridge):
        """crossings.ON: the bridge (line (k, 2), a single span no other bridge part joins) and the roads under it
        solved together (crossings.solve), cached per bridge: {"ends": (2, 2) its end points, "raise": (2,) m each end
        is raised over its own ground (its trusted height, filled up to its road's past it, + the lift), "lift": m,
        "cross": [(point (2,), the road's target height there)]}. None where it does not apply (a chain, a long or a
        rail bridge, no road under it, no trusted ground), and the rules as before decide."""
        bridge = np.asarray(bridge, np.float64)[:, :2]
        k = (round(float(bridge[0, 0]), 1), round(float(bridge[0, 1]), 1), round(float(bridge[-1, 0]), 1),
             round(float(bridge[-1, 1]), 1), len(bridge))
        with self.lock:
            memo = self.__dict__.setdefault("_xsolve_cache", {})
            if k in memo:
                return memo[k]
        res = None
        try:
            res = self._xsolve_calc(bridge)
        except Exception as e:      # the rules as before rather than no grading
            log.debug("grading: crossing solve at %s: %s: %s", k, type(e).__name__, e)
        with self.lock:
            if len(memo) > 4096:
                memo.clear()
            return memo.setdefault(k, res)

    def _xsolve_calc(self, bridge):
        import crossings as xs
        f64 = np.float64
        L = float(np.hypot(*np.diff(bridge, axis=0).T).sum())
        if L < 2 * APPROACH_MIN_CROSS_M or L > UNDER_MAX_BRIDGE_M:
            return None
        la, lo, _ = self.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        la, lo = np.asarray(la, f64), np.asarray(lo, f64)
        pad = (xs.TIE_M + 20.0) / 111320.0
        keys = sorted(set(mapcells.cells_for_points(la, lo, pad, pad / max(0.05, math.cos(math.radians(float(la[0]))))))
                      | {mapcells.cell_of(float(la[0]), float(lo[0]))}, key=str)
        cs = [self._cell_enu(kk) for kk in keys]
        bridges, cls = [], None
        for c in cs:
            for line, bc in zip(c["bridges"], c.get("bridge_cls", [None] * len(c["bridges"]))):
                bridges.append(line)
                if cls is None and len(line) == len(bridge) and np.allclose(line[:, :2], bridge, atol=0.05):
                    cls = bc
        if cls is None or bridge_chain(bridge, bridges):
            return None                 # (not a road bridge we know, or a chain: stage 1 is single spans)
        if CW_ON and _cw.family(cls) is not None and self._cw_z_at(bridge, bridge[:1]) is not None:
            return None                 # (a carriageway's: its profile decides, carriageways.py)
        # the ground roads near it, each once, with their class (paths apart: they neither tie nor weigh much)
        lo_b, hi_b = bridge.min(0) - xs.TIE_M - 20.0, bridge.max(0) + xs.TIE_M + 20.0
        lines, halves, classes, seen = [], [], [], set()
        for c in cs:
            for line, half, gc in c["ground"]:
                if gc in UNGRADED or len(line) < 2:
                    continue
                if (line.max(0) < lo_b).any() or (line.min(0) > hi_b).any():
                    continue
                kk = (round(float(line[0, 0]), 1), round(float(line[0, 1]), 1), len(line))
                if kk in seen:
                    continue
                seen.add(kk)
                lines.append(np.asarray(line, f64)[:, :2])
                halves.append(float(half))
                classes.append(gc)
        if not lines:
            return None
        crs = roadsmod._crossings([bridge], lines)
        crs = [c for c in crs if APPROACH_MIN_CROSS_M <= c[1] <= L - APPROACH_MIN_CROSS_M]
        if not crs:
            return None
        # which line each crossing is on (roads._crossings reports the point; the nearest line holds it)
        cr_line = []
        for c in crs:
            pt = np.asarray(c[2], f64)
            cr_line.append(int(np.argmin([_seg_dist(ln, pt) for ln in lines])))

        def z_at(p):
            p = np.asarray(p, f64).reshape(-1, 2)
            la_, lo_, _ = self.frame.enu_to_geodetic(p[:, 0], p[:, 1], 0.0)
            return np.asarray(self.elev(np.asarray(la_, f64), np.asarray(lo_, f64)), f64).ravel()
        # the bridge: its half width (its approaches' carriageway, as wide as the widest road meeting its ends)
        ends = np.array([bridge[0], bridge[-1]], f64)
        half_over = 0.0
        for ln, h in zip(lines, halves):
            if min(np.hypot(*(ln[0] - ends).T).min(), np.hypot(*(ln[-1] - ends).T).min()) <= APPROACH_TOUCH_M:
                half_over = max(half_over, h)
        half_over = half_over or 6.0
        g_end = z_at(ends)
        u0, u1 = xs.end_dirs(bridge)
        t_end = [xs.trusted_over(ends[0], u0, z_at), xs.trusted_over(ends[1], u1, z_at)]
        base = np.array([g if t is None else max(g, min(t, g + xs.FILL_MAX_M)) for g, t in zip(g_end, t_end)], f64)
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(bridge, axis=0).T))]
        clear = roadsmod.deck_road_clear(roadsmod.deck_depth(roadsmod.geom_span([bridge]))) + ROAD_LIFT
        low_ground = bool(np.min(base) < seatiles.SEA_E + xs.LOW_M)
        fixed = str(cls) in APPROACH_SKIP
        # (a motorway or trunk off low ground is not lifted: the roads under it give way, as #126. Lifted, Perth's
        # Mitchell Freeway rose 1-4 m to each of its street bridges, humps along it where a street dipping 1-2 m under
        # it was all it took. Its ends are still brought up to its road's trend past them: left on their embankments'
        # slopes, the freeway dipped 1-2 m into each bridge and humped over the street, the user 2026-11-01)
        roads_ = [ln for ln, gc in zip(lines, classes) if gc not in xs.PATHS]
        deck, low, w_down, drop_max, pts = [], [], [], [], []
        floor = seatiles.SEA_E + UNDER_SEA_M
        for c, li in zip(crs, cr_line):
            pt = np.asarray(c[2], f64)
            arc = float(c[1])
            ln = lines[li]
            u = xs.dir_at(ln, pt)
            t_low = xs.trusted_under(pt, u, half_over, z_at)
            here = float(z_at(pt)[0])
            # (a real cutting the DEM has keeps its depth: the trusted height is never over the road's own there)
            tl = here if t_low is None else min(t_low, here)
            deck.append(base[0] + (base[1] - base[0]) * arc / max(L, 1e-9))
            low.append(tl)
            nt = 0 if classes[li] in xs.PATHS else xs.ties(pt, roads_, ln)
            w_down.append(xs.down_cost(classes[li], nt, low_ground))
            dm = UNDER_MAX_DROP_M
            if UNDER_SEA_ON and tl >= floor:
                dm = min(dm, max(0.0, tl - floor))
            drop_max.append(dm)
            pts.append(pt)
        cap = xs.MAX_LIFT_FIXED_M if str(cls) in APPROACH_SKIP else min(xs.MAX_LIFT_M, APPROACH_MAX_LIFT)
        if fixed and not low_ground:
            cap = 0.0
        lift, drops = xs.solve(deck, low, clear, xs.up_cost(cls), np.array(w_down), cap, np.array(drop_max))
        out = {"ends": ends, "raise": base - g_end + lift, "lift": lift, "top": base + lift,
               "reach": half_over + xs.UNDER_GAP_M,
               "cross": [(p, float(t - d)) for p, t, d in zip(pts, low, drops)]}
        log.debug("grading: crossing solve %s (%.0f m, ends %s, trusted %s, clear %.1f, cost %.1f) over %s: lift %.1f, "
                  "roads to %s", cls, L, np.round(g_end, 1), np.round(base, 1), clear, xs.up_cost(cls),
                  [(classes[li], round(t, 1), round(w, 1)) for li, t, w in zip(cr_line, low, w_down)], lift,
                  [round(t, 1) for _p, t in out["cross"]])
        return out

    def _grade_match(self, xy):
        """m (signed) the approach at the bridge end xy is raised (> 0) or lowered (< 0) so the single span it ends is no
        steeper than SEAM_DECK_GRADE: half of what the ground at its two ends differs by over that, the lower end up, the
        higher down, within SEAM_MATCH_MAX_M; None for a chain, a carriageway's or never-raised class's bridge, or when the
        slope is fine. A pure function of the map around the bridge, cached per point."""
        k = (round(float(xy[0]) * 2), round(float(xy[1]) * 2))
        with self.lock:
            cache = self.__dict__.setdefault("_gm_cache", {})
            if k in cache:
                return cache[k]
        res = None
        try:
            res = self._grade_match_calc(np.asarray(xy, np.float64))
        except Exception as e:      # no match rather than no grading
            log.debug("grading: grade match at %s: %s: %s", k, type(e).__name__, e)
        with self.lock:
            return cache.setdefault(k, res)

    def _grade_match_calc(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), 0.0)
        home = mapcells.cell_of(float(la[0]), float(lo[0]))
        hc = self._cell_enu(home, ground=False)
        if not len(hc["ends"]):
            return None
        d = np.hypot(hc["ends"][:, 0] - xy[0], hc["ends"][:, 1] - xy[1])
        hit = np.flatnonzero(d <= APPROACH_TOUCH_M)
        if not len(hit):
            return None
        bi = hc["end_of"][int(hit[0])]
        bridge = np.asarray(hc["bridges"][bi], np.float64)[:, :2]
        cls = (hc.get("bridge_cls") or [None] * len(hc["bridges"]))[bi]
        if cls is None or cls in APPROACH_SKIP or str(cls).endswith("_link") or (
                CW_ON and _cw.family(cls) is not None):
            return None
        lam, lom, _ = self.frame.enu_to_geodetic(bridge[:, 0], bridge[:, 1], 0.0)
        pad = 50.0 / 111320.0
        padlon = pad / max(0.05, math.cos(math.radians(float(np.mean(lam)))))
        keys = sorted(set(mapcells.cells_for_points(lam, lom, pad, padlon)) | {home}, key=str)
        bridges = [b for kk in keys for b in self._cell_enu(kk, ground=False)["bridges"]]
        if bridge_chain(bridge, bridges) or not bridge_chain_ends_free(bridge, bridges, bridge[0])                 or not bridge_chain_ends_free(bridge, bridges, bridge[-1]):
            return None             # (a chain, or a joint: its ends are no abutments)
        L = float(np.hypot(*np.diff(bridge, axis=0).T).sum())
        if L < 4.0:
            return None
        ends = np.array([bridge[0], bridge[-1]])
        ela, elo, _ = self.frame.enu_to_geodetic(ends[:, 0], ends[:, 1], 0.0)
        z = np.asarray(self.elev(ela, elo), np.float64).reshape(-1)
        deficit = abs(float(z[1] - z[0])) - SEAM_DECK_GRADE * L
        if deficit < 2 * APPROACH_MIN_LIFT:
            return None
        half = min(deficit / 2.0, SEAM_MATCH_MAX_M)
        mine = 0 if np.hypot(*(ends[0] - xy)) <= np.hypot(*(ends[1] - xy)) else 1
        return half if z[mine] < z[1 - mine] else -half

    def _end_lift_raw(self, xy):
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
        sol = self._xsolve(bridge) if XS_ON else None
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
        if sol is not None:
            # (and under the decks beside it: a dual carriageway's other deck over the same road. The street passing
            # under Wezep's other A28 deck, 10 m off, capped this one's ramp at its abutment: the deck's end 1 m under
            # its ramp, a dip where it met the ground, the user 2026-11-01)
            spans = spans + [b for b in bridges if b is not bridge and len(b) >= 2
                             and float(np.min(np.hypot(*(b[:, :2] - xy).T))) <= TWIN_M]
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
        if sol is not None:             # (crossings.ON: the bridge and the roads under it solved together)
            i = int(np.argmin(np.hypot(*(sol["ends"] - xy).T)))
            lift = float(sol["raise"][i])
            # (lift: over the abutment's own ground; the last: the height its roads reach there, the trusted end + the
            # lift, which _lift_approaches raises a plan to from its own height, as for a measured deck)
            return None if lift < APPROACH_MIN_LIFT else (min(lift, APPROACH_MAX_LIFT), others, approaches, False,
                                                          None, True, float(sol["top"][i]))
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
            # (need is in the frame's ENU heights, where the Earth's curvature has dropped them d^2 / 2R under the
            # geodetic ones z_at gives: both on one scale, or a frame 15 km from the bridge saw 18 m of headroom
            # and raised nothing: Zdrina's 17 m bridge over a river, 50.5172 N 16.2121 E, 2026-11-08)
            p0 = np.asarray(xy, np.float64)[None, :2]
            la0, lo0, _ = self.frame.enu_to_geodetic(p0[:, 0], p0[:, 1], 0.0)
            z0 = float(self.frame.geodetic_to_enu(la0, lo0, np.asarray(z_at(p0), np.float64))[2][0])
            return max(0.0, float(reach.max() - z0))
        except Exception as e:      # no fill rather than no grading
            log.debug("grading: water lift: %s: %s", type(e).__name__, e)
            return 0.0

    def _longest(self, fid, j, ring, key):
        """Part j (its roads.part_keys key) of tunnel fid as the cells at its ends have it, the longest (a part comes clipped differently in
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
            got = dict(roadsmod._keyed(roadsmod.TunnelProfiles.keys_of, roadsmod.tunnel_parts, f, i)).get(j)
            if got is not None and len(got) > len(best):
                best = got
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

    def _subsea_part(self, feats, i, j, ring, portals):
        """The canonical region's solved wet profile, including its dry approaches. None: ordinary tunnel rules.

        The grading reader uses the same raw source, region frame and native solve as the mesh reader. It never
        calls a grader from a profile endpoint: planning the tunnel must not recursively plan itself.
        """
        import geotiles
        holder = self.store if self.store is not None else self
        with holder.lock:
            cs = getattr(holder, "_subsea_chains", None)
            if cs is None:
                raw = self.tunnel_raw or self.elev
                md, wet, water = self.md, self.wet, self.water
                def setup(t):
                    t.wet, t.water, t.raw = wet, water, raw
                cs = holder._subsea_chains = roadsmod.ChainStore(roadsmod.TunnelProfiles,
                    lambda k: md.cell(k, layers=("roads",), kind="demand").layers.get("roads"), raw, setup)
        st = cs.store_at(float(ring[0][1]), float(ring[0][0]))
        k = (str(feats.ids[i]), j)
        if not st.drowned(k, ring):
            return None
        whole = roadsmod.BridgeProfiles.parts_todo(st, [(k, feats, i)])
        pid = st._part_id(k)
        with st.lock:
            supported = pid in st._subsea
            secs = list(st.sections.get(pid, ()))
        if not supported:
            return []
        out = []
        for _pid, spec, xy, z, _lon in whole:
            la, lo, h = geotiles.world_to_geodetic(st.frame, np.c_[xy, z])
            e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.)
            xy = np.c_[e, n]
            for index, (a, b, kind) in enumerate(secs):
                if b <= a: continue
                fid = f"{pid}#{index}"
                # Plans are the terrain datum: apply(..., ROAD_LIFT) supplies the physical road surface.
                # The native tunnel profile already IS that surface, so do not add the lift a second time.
                out.append((fid, spec, xy[a:b + 1], np.asarray(h)[a:b + 1] - ROAD_LIFT, kind))
                if kind and portals is not None:
                    for at, is_mouth in zip((a, b), st.mouth(fid)):
                        portals[(fid, tuple(np.round(xy[at], 3)))] = is_mouth
        return out

    def _tunnel_sections(self, feats, key=None, road_plans=(), portals=None):
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
            for j, ring in roadsmod._keyed(roadsmod.TunnelProfiles.keys_of, roadsmod.tunnel_parts, feats, i):
                ring = self._longest(str(feats.ids[i]), j, ring, key)
                ll = np.asarray(ring, np.float64)
                if len(ll) < 2:
                    continue
                if self.water is not None:
                    solved = self._subsea_part(feats, i, j, ring, portals)
                    if solved is not None:
                        out.extend(solved)
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
                kinds = roadsmod.portal_cover(kinds, g - floor, ts[i], s)
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
    def _mouth_plans(secs, portals=None):
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
                endpoint = xy[0 if at_start else -1]
                if portals is not None and portals.get((fid, tuple(np.round(endpoint, 3)))) is False:
                    continue
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

    @profile_step('Road grading')
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
        if not plans and not (RACEWAY_SMOOTH_ON and self._has_keeps()):
            return base
        e, n, _ = self.frame.geodetic_to_enu(lat.ravel(), lon.ravel(), 0.0)
        e, n = np.asarray(e), np.asarray(n)
        out = grade_points(e, n, base.ravel(), pack, lift) if plans else np.array(base.ravel(), np.float64, copy=True)
        race = self._on_kept(e, n)
        if race is not None:                # (the terrain is a racetrack's surface: GitHub #143, smoothed: #833)
            out[race] = self._raceway_ground(e[race], n[race], base.ravel()[race])
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

    def _has_keeps(self):
        with self.lock:
            return any(self.keeps.values())

    def _raceway_ground(self, e, n, base):
        """base at the points on a raceway (_on_kept) smoothed along it (raceway_ground; RACEWAY_SMOOTH_ON)."""
        if not RACEWAY_SMOOTH_ON or self.elev is None:
            return base
        with self.lock:
            lines = sorted({(round(float(xy[0, 0]), 2), round(float(xy[0, 1]), 2), round(float(xy[-1, 0]), 2),
                             round(float(xy[-1, 1]), 2), len(xy), round(float(hw), 3)): (xy, hw)
                            for v in self.keeps.values() for xy, hw in v}.items())
        memo = self.__dict__.setdefault("_raceway_memo", OrderedDict())
        zones = []
        for key, (xy, hw) in lines:
            with self.lock:
                got = memo.get(key)
            if got is None:
                got = raceway_surface(self.frame, self.elev, xy, hw) or ()
                with self.lock:
                    memo[key] = got
                    while len(memo) > 256:
                        memo.popitem(last=False)
            if got:
                zones.append(got)
        return raceway_ground(e, n, base, zones)

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
    q.steep = getattr(p, "steep", False)
    set_widen(q, getattr(p, "widen", None))
    return q


class PlanStore:
    """Every road plan of a process, whatever the anchor: one _Planner per planning region (geotiles.PLAN_Z), made on
    first use. mapdata, elev (lat, lon -> m at PROFILE_ZOOM, raw), widths as the Grader's; wet, dem, shared (the
    planners' settings) set after it is made, as on a Grader (which forwards them here)."""

    def __init__(self, mapdata, elev, widths=None):
        self.md, self.elev, self.widths = mapdata, elev, widths
        global _ACTIVE_STORE
        _ACTIVE_STORE = self
        self.wet = None
        self.water = None
        self.tunnel_raw = None
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


_ACTIVE_STORE = None    # the process's PlanStore (the last made): the carriageways' deck heights (_deck_z_hook)


def _deck_z_hook(frame, lines, fids=None):
    """roads.DECK_Z_HOOK: per deck line (frame ENU (k, 2+)), its heights (frame up m) from its carriageway's profile, or
    None where it is not one's."""
    store = _ACTIVE_STORE
    if store is None:
        return None
    out = []
    for n, ln in enumerate(lines):
        fid = None if fids is None else str(fids[n])
        ln = np.asarray(ln, np.float64)[:, :2]
        la, lo, _ = frame.enu_to_geodetic(ln[:, 0], ln[:, 1], 0.0)
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        pl, _idx = store.planners_of(la[len(la) // 2:len(la) // 2 + 1], lo[len(lo) // 2:len(lo) // 2 + 1])[0]
        e, n, _ = pl.frame.geodetic_to_enu(la, lo, 0.0)
        xy = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1)
        z = pl._cw_z_at(xy, xy, fid)
        if z is None:
            out.append(None)
            continue
        out.append(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(z, np.float64))[2], np.float64))
    return out if any(o is not None for o in out) else None


if CW_ON:
    roadsmod.DECK_Z_HOOK = _deck_z_hook


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
    water = _set("water")
    tunnel_raw = _set("tunnel_raw")
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
    _raceway_ground = _Planner._raceway_ground

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

    def prepare(self, lat, lon):
        """Plan the cells the points' area needs now (several at once, _Planner._plan_missing), returning when they are:
        a block's builder starts it beside its own work (its sea floor waited on the water cells, then its grading on
        the road cells: 4 + 9 s of a fresh place's first block, one after the other, Lincoln 2026-10-30)."""
        lat = np.asarray(lat, np.float64).ravel()
        lon = np.asarray(lon, np.float64).ravel()
        for pl, idx in self.store.planners_of(lat, lon):
            keys = pl._keys(lat[idx], lon[idx])
            pl._plan_missing(keys)      # (two or more at once; one is planned here)
            for k in keys:
                pl._cell_plans(k)

    @profile_step('Road grading')
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
        pw = getattr(p, "widen", None)
        if pw is not None:
            pw = np.r_[pw[i] + (pw[i + 1] - pw[i]) * t0, pw[i + 1:j + 1], pw[j] + (pw[j + 1] - pw[j]) * t1]
        if np.hypot(*(pxy[-1] - pxy[0])) > 1e-6 or len(pxy) > 2:
            q = Plan(pxy, pz, p.half, (p.abut[0] and i == 0 and t0 == 0.0, p.abut[1] and j == n - 1 and t1 == 1.0),
                     None if p.cut is None else np.asarray(p.cut)[i:j + 1].copy(), p.road_half)
            q.rid = getattr(p, "rid", -1)
            q.max_wall = getattr(p, "max_wall", WALL_MAX_H_M)
            q.bank = pb
            q.steep = getattr(p, "steep", False)
            set_widen(q, pw)
            out.append(q)
        i = j + 1
    return out


def road_id(fid):
    """A stable integer id of a map feature (grade_points: a road never gives way to itself, though each map cell
    plans its own stretch of it: the Stelvio Pass's hairpins sank under their own other stretches, 2026-10-21)."""
    import zlib
    return zlib.crc32(str(fid).encode()) & 0x7FFFFFFF


DUPE_XY_M = 0.05             # consecutive plan vertices this close are one vertex (a repeated OSM node, a trim
                            # cut at a vertex, a junction snap moved an end centimetres to its junction): a centimetre
                            # segment with metres of height divides by its length downstream (seen as a 430 m spike
                            # and a pit beside it from 1.5 cm for 5.8 m)


def _dedupe_plan(p):
    """A plan without its zero-length segments (None: nothing left), its other arrays carried along (a run of
    duplicates keeps the last height: a junction snap's). The input plan is never changed."""
    xy = np.asarray(p.xy, np.float64)[:, :2]
    n = len(xy)
    if n < 2:
        return None
    if bool((np.hypot(*np.diff(xy, axis=0).T) > DUPE_XY_M).all()):
        return p
    keep = [0]
    for i in range(1, n):
        if float(np.hypot(*(xy[i] - xy[keep[-1]]))) > DUPE_XY_M:
            keep.append(i)
        else:
            keep[-1] = i
    keep = np.array(keep, np.int64)
    if len(keep) < 2:
        return None
    seg_ok = np.hypot(*np.diff(xy, axis=0).T) > DUPE_XY_M
    q = Plan(np.ascontiguousarray(xy[keep]), np.ascontiguousarray(np.asarray(p.z, np.float64)[keep]), p.half,
             p.abut, None if p.cut is None else np.ascontiguousarray(np.asarray(p.cut, np.float64)[seg_ok]),
             p.road_half)
    q.rid, q.max_wall = getattr(p, "rid", -1), getattr(p, "max_wall", q.max_wall)
    bk = getattr(p, "bank", None)
    q.bank = None if bk is None else np.ascontiguousarray(np.asarray(bk, np.float64)[keep])
    q.steep = bool(getattr(p, "steep", False))
    wd = getattr(p, "widen", None)
    q.widen = None if wd is None else np.ascontiguousarray(np.asarray(wd, np.float64)[keep])
    return q


def rail_plan(fid, xy, z):
    """The Plan of a railway's bed line (railways.grade_plans' fid, xy (k, 2), z (k,)): the ground flat to the bed
    GRADE_HALF_M either side of the track, its carriageway (grade_points' rh: what never gives way) the ballast's top.
    Past the ballast the band gives way to another road's carriageway at another level, as a road's verge does: the
    whole band its own, it blended into the approach of a short bridge over it whose abutment stood inside the band, and
    the deck end, which takes that ground, sat 2.5-3.4 m under the road going on from it (GitHub #909, the A11 over the
    Gramzower Museumsbahn)."""
    p = Plan(xy, z, railwaysmod.GRADE_HALF_M, road_half=railwaysmod.BALLAST_TOP_M)
    p.rid = road_id(RAIL_TAG + fid)
    return p


class PlanPack:
    """Plans (a list, in grade_points' order) as grade_points takes them: every segment's arrays concatenated once, with
    each plan's box; a call keeps the segments of the plans whose boxes meet its points', in the same order (built per
    call, the plans' Python loop cost a Canton tile set ~1 s, 2026-10-25)."""

    def __init__(self, plans):
        rows = [(k, q) for k, p in enumerate(plans) if (q := _dedupe_plan(p)) is not None and len(q.xy) >= 2]
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
        # (a plan widened in places, Plan.widen: each segment by the more of its ends')
        wd = np.concatenate([np.asarray(p.widen, f64) if getattr(p, "widen", None) is not None else np.zeros(len(p.xy))
                             for _k, p in rows])
        if wd.any():
            ws = np.maximum(wd[ia], wd[ia + 1])
            self.h = self.h + ws
            self.rh = self.rh + ws
        rids = [getattr(p, "rid", -1) for _k, p in rows]
        self.rid = np.repeat(np.array([r if r is not None and r >= 0 else 2 ** 40 + k
                                       for (k, _p), r in zip(rows, rids)], np.int64), nseg)
        s0 = np.r_[0, np.cumsum(nseg)]
        # a pass road's legs between its hairpins' apexes (roads.hairpin_apex_rows) count as roads of their own: the
        # one leg's ground (its wall's strip, its sides) gives way over the other's carriageway at another level as
        # another road's does (grade_points), not the same road's own (a leg's strip behind its wall reached the leg
        # above's lane across a hairpin's narrow middle: a 30 cm step in it, the Stelvio, 2026-11-01)
        for n, (_k, p) in enumerate(rows):
            if getattr(p, "steep", False) and nseg[n] >= 2:
                apex = roadsmod.hairpin_apex_rows(p.xy)
                if apex:
                    leg = np.searchsorted(np.asarray(apex), np.arange(nseg[n]), side="right")
                    self.rid[s0[n]:s0[n + 1]] += leg.astype(np.int64) << LEG_RID_SHIFT
        # a plan's end where another plan's end lies and the road carries straight on through it (outward directions
        # within LINK_TURN_DEG of opposite) goes on: one road planned per map cell, a road renamed at a point. Per
        # (plan, end) the (plan, end)s it goes on into
        at = {}
        for n, (_k, p) in enumerate(rows):
            if nseg[n] <= 0:
                continue
            P = np.asarray(p.xy, f64)[:, :2]
            for e, (q, nb) in enumerate(((P[0], P[1]), (P[-1], P[-2]))):
                d = q - nb
                ln = float(np.hypot(*d))
                if ln > 1e-9:
                    at.setdefault((round(float(q[0]), 1), round(float(q[1]), 1)), []).append((n, e, d / ln))
        cos_min = -math.cos(math.radians(LINK_TURN_DEG))
        goes_on = {}
        for group in at.values():
            if len(group) < 2:
                continue
            for n, e, d in group:
                into = [(m, e2) for m, e2, d2 in group if m != n and float(d @ d2) <= cos_min]
                if into:
                    goes_on[(n, e)] = into
        flags = [getattr(p, "abut", (False, False)) for _k, p in rows]
        lens = {}

        def beyond(n, e):
            # m along the road from plan n's end e, through the plans it goes on into, to an abutment within
            # ABUT_NEAR_M, or NaN (the nearest, every way it goes on; a ring road's links come round to it)
            import heapq
            best, todo, done = math.inf, [(0.0, n, e)], set()
            while todo:
                acc, a, ea = heapq.heappop(todo)
                if acc >= best:
                    break
                if (a, ea) in done:
                    continue
                done.add((a, ea))
                for m, e2 in goes_on.get((a, ea), ()):
                    if flags[m][e2]:
                        best = min(best, acc)
                        continue
                    if m not in lens:
                        lens[m] = float(np.hypot(*np.diff(np.asarray(rows[m][1].xy, f64)[:, :2], axis=0).T).sum())
                    far = acc + lens[m]
                    if far >= ABUT_NEAR_M:
                        continue
                    if flags[m][1 - e2]:
                        best = min(best, far)
                    else:
                        heapq.heappush(todo, (far, m, 1 - e2))
            return best if best < ABUT_NEAR_M else math.nan

        ab = np.zeros(int(s0[-1]), bool)
        # per segment near a bridge abutment: the distance along its road from its start back to a start abutment
        # (column 0) and from its end on to an end abutment (column 1), NaN if none: the ground past the abutment,
        # over the deck, falls away at ABUT_UNDER_GRADE (grade_points), whichever of its road's segments reaches it.
        # The road's own abutment through the plans it goes on into too: the piece a map cell's edge cuts a few
        # metres short of an abutment (trim_plan: no abutment end of its own) reached over the deck with its
        # carriageway's round end, and a railway's bed under it gave way to it (Codex's review of PR #920, GitHub #909)
        abd = np.full((int(s0[-1]), 2), np.nan)
        cut = np.full((int(s0[-1]), 6), np.nan)
        for n, (_k, p) in enumerate(rows):
            off = [0.0 if flags[n][e] else beyond(n, e) if (n, e) in goes_on else math.nan for e in (0, 1)]
            if (math.isfinite(off[0]) or math.isfinite(off[1])) and s0[n + 1] > s0[n]:
                arc = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p.xy[:, :2], axis=0).T))]
                if math.isfinite(off[0]):
                    to = arc[:-1] + off[0]
                    near = to < ABUT_NEAR_M
                    ab[s0[n]:s0[n + 1]] |= near
                    abd[s0[n]:s0[n + 1], 0] = np.where(near, to, np.nan)
                if math.isfinite(off[1]):
                    to = arc[-1] - arc[1:] + off[1]
                    near = to < ABUT_NEAR_M
                    ab[s0[n]:s0[n + 1]] |= near
                    abd[s0[n]:s0[n + 1], 1] = np.where(near, to, np.nan)
            c = getattr(p, "cut", None)
            if c is not None:
                cut[s0[n]:s0[n + 1]] = c
        self.ab = ab.astype(np.uint8)
        self.abd = abd
        self.cut = cut
        self.st = np.repeat(np.array([bool(getattr(p, "steep", False)) for _k, p in rows], np.uint8), nseg)
        # each segment's link: bit 0 its plan goes on before it, bit 1 after it (grade_points' EXTRAP_M)
        first = np.zeros(int(nseg.sum()), bool)
        last = np.zeros(int(nseg.sum()), bool)
        ends = np.cumsum(nseg)
        first[(ends - nseg)[nseg > 0]] = True
        last[(ends - 1)[nseg > 0]] = True
        for n, e in goes_on:
            if e == 0:
                first[ends[n] - nseg[n]] = False
            else:
                last[ends[n] - 1] = False
        self.lk = ((~first).astype(np.uint8) | ((~last).astype(np.uint8) << 1)).astype(np.uint8)
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
            return self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid, self.ab, self.cut, self.ba, self.bb, self.st, \
                self.lk, self.abd
        sk = keep[self.of]
        return tuple(np.ascontiguousarray(v[sk]) for v in (self.a, self.b, self.za, self.zb, self.h, self.rh, self.rid,
                                                           self.ab, self.cut, self.ba, self.bb, self.st, self.lk,
                                                           self.abd))


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
    A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB, ST, LK, ABD = got
    import fast
    fast.require(169, "grade_points")        # (the Rust loop: native/earthfast; its segments' links: 125, yield_ramp: 126,
                                             # legs blend over each other's bands: 133, a pass road's core
                                             # given way to whole: 134, no carriageway past an abutment end: 169)
    f64 = np.float64
    return fast._ef.grade_points(np.ascontiguousarray(px, f64), np.ascontiguousarray(py, f64),
                                 np.ascontiguousarray(base, f64), A, B, ZA, ZB, H, RH, RID, AB, CUT, BA, BB, ST, LK,
                                 float(EXTRAP_M), float(lift), float(SLOPE_RUN), float(T_MIN), float(T_MAX), float(STEEP_RUN),
                                 float(STEEP_T_MIN), float(BLEND_R), float(BUCKET),
                                 float(WALL_CUT_M), float(WALL_T_M), float(WALL_FLAT_M), float(WALL_AHEAD_M),
                                 bool(SEP_ON), float(SEP_FROM_M), float(SEP_FULL_M), float(CORE_FADE_M),
                                 float(SIDE_FADE_M), float(SHARP_STEP_M), float(YIELD_RAMP_M), float(ABUT_UNDER_GRADE),
                                 np.ascontiguousarray(ABD, f64))


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
    # a road crossing a deck within roads.AT_GRADE_REACH_M of a free abutment that the deck cannot get
    # roads.AT_GRADE_M over is a junction at grade, the rule roads.deck_profile_once and deck_curve already
    # have: the deck leaves that road's height there, so it asks no clearance under it. A track meeting a
    # deck's mapped end (a node the two ways share) came under an end skewed onto its junction, and the
    # approach fill rose the road 8 m over its own ground to clear it
    dend = np.minimum(arcs, s[-1] - arcs)
    free_end = np.where(dend == arcs, not joined[0], not joined[1])
    ends_z = np.asarray(z_at(np.array([line[0], line[-1]], np.float64)), np.float64).reshape(-1)
    junction = (~rail) & free_end & (dend <= roadsmod.AT_GRADE_REACH_M) & \
        (float(np.min(ends_z)) + roadsmod.DECK_MAX_GRADE * dend < zu + roadsmod.AT_GRADE_M)
    under &= ~junction
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


class _Decks(list):
    """deck_capped's decks [(line (k, 2), half width)] with box (n, 4): each line's x0, y0, x1, y1; and flat(): every
    line's segments, made once (deck_capped tests every near deck's at once)."""

    def __init__(self, decks):
        super().__init__(decks)
        self.box = np.array([(*np.asarray(ln, np.float64)[:, :2].min(0), *np.asarray(ln, np.float64)[:, :2].max(0))
                             for ln, _h in self], np.float64).reshape(-1, 4)

    def flat(self):
        """Every line's segments in one: (off (n + 1,): line k's rows off[k]..off[k + 1] (none without a segment),
        starts (m, 2), vectors (m, 2), lengths (m,), each line's half width (n,)), made once (deck_capped's native
        test: each line's own differences and np.hypot, elementwise)."""
        got = getattr(self, "_flat", None)
        if got is None:
            lines = [np.asarray(ln, np.float64)[:, :2] for ln, _h in self]
            have = [b for b in lines if len(b) >= 2]
            off = np.r_[0, np.cumsum([max(len(b) - 1, 0) for b in lines])].astype(np.int64)
            if have:
                pts = np.concatenate(have)
                d = np.diff(pts, axis=0)
                keep = np.ones(len(d), bool)
                keep[np.cumsum([len(b) for b in have])[:-1] - 1] = False      # (from one line's end to the next)
                q, s = np.ascontiguousarray(pts[:-1][keep]), np.ascontiguousarray(d[keep])
                lb = np.hypot(*s.T)
            else:
                q, s, lb = np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)
            got = self._flat = (off, q, s, lb, np.array([float(h) for _ln, h in self], np.float64))
        return got


def deck_capped(pts, z, decks):
    """z (n,), the ground at points pts (n, 2) along a road in order, capped where decks [(line (k, 2), half width)]
    cross over it (UNDER_DECK_*): from UNDER_DECK_SIDE_M past the deck's sides, at most the straight line between the
    ground there. A crossing within APPROACH_MIN_CROSS_M of a deck's end is a junction at grade and is left. Returns z
    itself when nothing is capped. Pure. (native underdeck.rs: every near deck's segments against the road's in one
    call; numpy per road part was ~half of plan_roads on a dense tile, 2026-11-08)"""
    if not decks or len(pts) < 2:
        return z
    pts = np.ascontiguousarray(np.asarray(pts, np.float64)[:, :2])
    test_box = isinstance(decks, _Decks)    # (every deck's box at once)
    if not test_box:                        # (a plain list: each deck's own test, the near ones' segments made here)
        lo_p, hi_p = pts.min(0) - UNDER_DECK_MAX_M, pts.max(0) + UNDER_DECK_MAX_M
        lines = [(np.asarray(ln, np.float64)[:, :2], h) for ln, h in decks]
        decks = _Decks([(ln, h) for ln, h in lines
                        if len(ln) >= 2 and not ((ln.max(0) < lo_p).any() or (ln.min(0) > hi_p).any())])
        if not len(decks):
            return z
    r = np.diff(pts, axis=0)
    la = np.hypot(*r.T)
    import fast
    code, out = fast.deck_capped(pts, z, la, decks.box, test_box, decks.flat(), UNDER_DECK_MAX_M,
                                 UNDER_DECK_SIDE_M, APPROACH_MIN_CROSS_M)
    return z if code == 0 else np.asarray(z, np.float64) if code == 1 else out


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
                    UNDER_JOINT_FADE_M, UNDER_NODE_M], f64)
    out = fast._ef.underpass_offsets(xy, starts, req, qxy, qline, prm)
    return np.split(out, np.cumsum([len(q) for q in qs])[:-1])


def street_runs(lines, classes, requirements):
    """UNDER_RUN_* (coarse elevation): requirements [(line, arc m, depth m)] on the ground roads lines [(k, 2)] (classes:
    each one's road class) split into (those spread along the joined roads as before: [(line, arc, depth)], those kept
    on their street: [(run xy (k, 2), {its lines}, [(0, arc m along the run, depth m)])]). A run: a line carried on
    through every node where only its next piece joins it (the map splits a road at more than its junctions) to its
    junctions (3+ lines) and loose ends; underpass_offsets on a run alone lays each dip inside it (flat, then ramps up
    to UNDER_MAX_GRADE, as deep as fits). Kept on its run: a street's dip (not a motorway's or trunk's: APPROACH_SKIP)
    whose run has room for UNDER_RUN_MIN_M of it (or all of it, when shallower) and is no ring. Pure."""
    if not requirements:
        return list(requirements), []
    pts = [np.asarray(ln, np.float64)[:, :2] for ln in lines]
    nodes, edges, _adj = roadsmod._chain_graph(pts)
    inc = [[] for _ in nodes]
    for l, (a, b, _s) in enumerate(edges):
        inc[a].append(l)
        inc[b].append(l)
    memo = {}

    def run_of(i):
        """(xy, its arc lengths, {line: (arc of its start in the run, reversed, its own length)}, ring), the same for
        every line of the run."""
        got = memo.get(i)
        if got is not None:
            return got
        seq, ring = [(i, False)], False
        for ahead in (True, False):
            l, rev = i, False
            while True:
                a, b, _s = edges[l]
                end = (a if rev else b) if ahead else (b if rev else a)
                if len(inc[end]) != 2:
                    break
                m = inc[end][1] if inc[end][0] == l else inc[end][0]
                if any(m == k for k, _r in seq):
                    ring = True
                    break
                ma, mb, _s = edges[m]
                rev = (ma != end) if ahead else (mb != end)
                if ahead:
                    seq.append((m, rev))
                else:
                    seq.insert(0, (m, rev))
                l = m
        xy, first = [], []
        for m, rev in seq:
            p = pts[m][::-1] if rev else pts[m]
            first.append(sum(len(q) for q in xy) - (1 if xy else 0))
            xy.append(p if not xy else p[1:])
        xy = np.vstack(xy)
        sx = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        got = (xy, sx, {m: (float(sx[k]), rev, float(edges[m][2][-1])) for (m, rev), k in zip(seq, first)}, ring)
        for m, _rev in seq:
            memo[m] = got
        return got
    net, runs = [], {}
    for i, sc, depth in requirements:
        i = int(i)
        if classes[i] in APPROACH_SKIP:
            net.append((i, sc, depth))
            continue
        xy, sx, at, ring = run_of(i)
        s0, rev, length = at[i]
        arc = float(np.clip(s0 + (length - sc if rev else sc), 0.0, sx[-1]))
        fit = max(0.0, min(arc, float(sx[-1]) - arc) - UNDER_FLAT_M) * 2.0 * UNDER_MAX_GRADE / math.pi
        if ring or fit < min(depth, UNDER_RUN_MIN_M):
            net.append((i, sc, depth))
            continue
        runs.setdefault(id(xy), (xy, set(at), []))[2].append((0, arc, depth))
    return net, list(runs.values())


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


def approach_add(xy, at_end, lift, half, others=(), side_run=None, grade=None):
    """m to add to a road plan's vertices (xy (n, 2)) rising to `lift` at its first (at_end False) or last vertex (a
    bridge's abutment), falling off along the road (arc length) on a cosine ramp whose steepest grade is APPROACH_GRADE;
    at most what keeps the fill slope (side_run x the height out, SLOPE_RUN by default) clear of the other roads
    [(line (k, 2), half width)] beside it, with APPROACH_GAP_M to spare. None when nothing is raised. Pure."""
    side_run = SLOPE_RUN if side_run is None else side_run
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2 or lift < APPROACH_MIN_LIFT:
        return None
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
    d = (s[-1] - s) if at_end else s
    run = float(np.clip(lift * math.pi / (2 * (grade or APPROACH_GRADE)), APPROACH_MIN_RUN,
                        APPROACH_MAX_RUN * (APPROACH_GRADE / grade if grade else 1.0)))
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
        reach = half + oh + APPROACH_GAP_M + side_run * lift
        if (line.max(0) < lo_q - reach).any() or (line.min(0) > hi_q + reach).any():
            continue                    # too far to matter
        q = xy[on]
        a, b = line[:-1], line[1:]
        ab = b - a
        L2 = np.maximum((ab * ab).sum(1), 1e-12)
        t = np.clip(((q[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0.0, 1.0)
        dist = np.hypot(*(q[:, None, :] - (a[None] + t[..., None] * ab[None])).transpose(2, 0, 1)).min(1)
        cap = np.maximum(0.0, (dist - half - oh - APPROACH_GAP_M) / max(side_run, 1e-6))
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


def _road_fids(i, lines, classes, gids, reach=XS_FLOOR_M, touch=3.0):
    """The feature ids of ground line i and of the lines continuing it within reach m (joined end to end, of its class:
    a road split at junctions, a dual carriageway's halves meeting it). Pure."""
    base = lines[i]
    c0 = np.asarray(base, np.float64)[len(base) // 2]
    road, frontier = {i}, [i]
    ends = np.array([[ln[0, :2], ln[-1, :2]] for ln in lines], np.float64)
    while frontier:
        k = frontier.pop()
        for e in (ends[k, 0], ends[k, 1]):
            if np.hypot(*(e - c0)) > reach:
                continue
            d = np.minimum(np.hypot(*(ends[:, 0] - e).T), np.hypot(*(ends[:, 1] - e).T))
            for m in np.flatnonzero(d <= touch):
                if m not in road and classes[m] == classes[i]:
                    road.add(int(m))
                    frontier.append(int(m))
    return {gids[m][0] for m in road}


def _seg_dist(line, p):
    """m from p to the polyline line (k, 2). Pure."""
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    return float(np.hypot(*(a + t[:, None] * ab - p).T).min())


def _never_raised(spec):
    """A road whose approaches are never raised: a motorway or trunk (APPROACH_SKIP), not one of their links. A link
    leaving a motorway's bridge runs down a fill to the street: kept at the ground, its deck fell 11 m within 90 m and
    dropped beside its mainline while they still overlapped, and an AI car drove into the mainline's parapet trying to
    take the exit (US-101's exit 1E over the Los Angeles River, 2026-10-27)."""
    return spec.cls in APPROACH_SKIP and not _is_link(spec)


TERRAIN_ROUGH_M = float(os.environ.get("BNE_TERRAIN_BUMP", "0.35"))   # off-road roughness (std), about twice a minor
                                                                       # dirt road's: beside it is rougher than on it
TERRAIN_ROUGH_WAVES = ((9.0, 1.0), (4.5, 0.6), (2.4, 0.3))             # (feature size m, share)


def _hash01(i, j, salt=0):
    """A stable pseudo-random value in [0, 1) for lattice points (Python's hash() is randomised per process)."""
    i = np.asarray(i, np.int64)
    j = np.asarray(j, np.int64)
    h = (i * np.int64(73856093)) ^ (j * np.int64(19349663)) ^ np.int64(int(salt) * 83492791)
    h = h.astype(np.uint64)
    h ^= h >> np.uint64(13)
    h *= np.uint64(1274126177)
    h ^= h >> np.uint64(16)
    return (h & np.uint64(0xFFFFFF)).astype(np.float64) / float(0x1000000)


# The land off the roads roughened (PR #677): opt-in (BNE_TERRAIN_BUMPS=on) until it is one kernel applied the same way
# on every ground path (provisional blocks and far tiles too) and kept off towns, water and building sites; on, it is
# ~0.3-0.5 s of numpy per million samples on every block, road tile and far tile (2026-11-05 merge review).
TERRAIN_BUMPS_ON = os.environ.get("BNE_TERRAIN_BUMPS", "off") == "on"


def terrain_bumps(lat, lon):
    """World-locked roughness of the land off the roads (m), the same value whatever process or tile asks: value noise
    at a few sizes, about TERRAIN_ROUGH_M (std). 0.0 (nothing added) unless TERRAIN_BUMPS_ON, or when BNE_TERRAIN_BUMP
    is 0."""
    if not TERRAIN_BUMPS_ON or TERRAIN_ROUGH_M <= 0.0:
        return 0.0
    la = np.asarray(lat, np.float64)
    lo = np.asarray(lon, np.float64)
    y = la * 110574.0
    x = lo * 111320.0 * np.cos(np.radians(la))
    out = np.zeros(np.shape(la), np.float64)
    norm = 0.0
    for size, share in TERRAIN_ROUGH_WAVES:
        fx = x / size
        fy = y / size
        i0 = np.floor(fx)
        j0 = np.floor(fy)
        tx = fx - i0
        ty = fy - j0
        sx = tx * tx * (3.0 - 2.0 * tx)
        sy = ty * ty * (3.0 - 2.0 * ty)
        i0 = i0.astype(np.int64)
        j0 = j0.astype(np.int64)
        v00 = _hash01(i0, j0, size)
        v10 = _hash01(i0 + 1, j0, size)
        v01 = _hash01(i0, j0 + 1, size)
        v11 = _hash01(i0 + 1, j0 + 1, size)
        v = (v00 * (1.0 - sx) + v10 * sx) * (1.0 - sy) + (v01 * (1.0 - sx) + v11 * sx) * sy
        out += share * (v - 0.5)
        norm += share * share
    return out * (TERRAIN_ROUGH_M / (0.1875 * math.sqrt(norm)))


class GradedDem:
    """dem.Dem with sample() graded under the roads; everything else passes through."""

    def __init__(self, dem, grader):
        self._dem, self._grader = dem, grader

    def sample(self, lat, lon, zoom, progress=None):
        base = self._dem.sample(lat, lon, zoom, progress) if progress is not None else self._dem.sample(lat, lon, zoom)
        # the land off the roads is rougher than the roads themselves (the grading flattens the road's own strip)
        base = np.asarray(base, np.float64) + terrain_bumps(np.asarray(lat, np.float64), np.asarray(lon, np.float64))
        return self._grader.apply(lat, lon, base)

    def __getattr__(self, name):
        return getattr(self._dem, name)
