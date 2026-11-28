"""Parked aircraft: airliners nose-in at an airport's terminal gates and light aircraft in rows along a general-aviation
apron, as forest rows of the mod's static prop groups ac_light / ac_turboprop / ac_narrow / ac_wide (origin on the ground
under the aircraft's centre, nose toward local +Y, scale 1, the row's height the tail's top: CATALOG). Pure: lat / lon
points (tile_rows takes the ground and the frame).

stands(feats, buildings) -> [Craft]: from the infrastructure Features (Overture subtype "airport") of a cell and its
buildings, kept on the Features (and the buildings it was asked with).
Gates: class "airport_gate" or "parking_position" points (also a tag aeroway=gate / parking_position). A gate lies just
inside the terminal's facade (or in a pier drawn into the terminal's polygon: then the nearest ways out of the polygon are tried, a wall or a finger's tip first, to the first that clears), so the terminal is the nearest building of at least TERMINAL_MIN_M2 within NEAR_M (the
gate's own building when it stands inside one: its cell's and its neighbours' buildings), the facade's direction that of the
building edge nearest the gate (an edge of at least EDGE_MIN_M; a shorter one: the length-weighted principal direction of
the edges within FACADE_R_M of it), and the aircraft parks nose-in, its axis along the facade's normal (so perpendicular to
the wall): nose NOSE_GAP_M off the facade, centre on from there by half its length, the tail toward the open apron. A
wall's direction is its general line (wall_normal: a line fitted to the outline over the widest straight window of MACRO_HALF_M,
a crenellated pier's edges' principal direction where the edge is a step), tried before the nearest edge's own: a pier drawn with a
protrusion at each gate, a diagonal or a curved wall takes the aircraft nose-in to the wall, not to a step. A
numeric tag direction / heading / bearing on the gate (the nose's compass bearing) is preferred where it points
within HEADING_MAX_DEG of the facade's normal. A mapped jet bridge (airportroles: the gate's nearest bridge with an end within GATE_BRIDGE_M of it; the cell's buildings and
parts classified) places the aircraft from the bridge: the axis the wall's normal at the bridge's root (nose-in; at a round satellite
radial), or, where the bridge is more than BRIDGE_AXIS_DEG off that normal, the bridge's own (the nose still toward the
building). The pose is solved (_bridge_pose): of those where the whole plan (fuselage, wings, tailplane) keeps SKIN_GAP_M off every building
and bridge (the gate's own bridge and its tails (airportroles: Roles.tails), less the last BRIDGE_CAB_M of each: the cab at the
door; every mapped bridge is an obstacle to the fuselage only, the wings pass under) and meets nothing else, the one that puts the forward LEFT door (DOOR_K of its
length back from the nose) nearest the bridge's outer end (within BRIDGE_CAB_M + SKIN_GAP_M of its end face, else the next kind is tried), sliding along
the axis and across the bridge and turning the axis by up to 5 degrees; the door is the left one (no mirror). A small building touching the
gate that airportroles does not take for a bridge (under BRIDGE_MAX_M2, within ATTACH_M, reaching BRIDGE_MIN_M or more out) fixes
the nose BACK_M (the door's setback from the nose, by type) short of its tip as before; such buildings are no obstacle.
A gate in, or within CENTRE_M of, a terminal's or pier's footprint (mapped on its centreline) tries both sides' facades
(the sides within SIDE_REACH_M and SIDE_SLACK_M of the nearest wall): the side whose stand is clear is kept, where both are
the side away from the nearest stand kept (a pair of gates alternates sides).
A gate at a pier's tip or a corner of the wall (radial_centre: the building's outline turns convex by over RADIAL_TURN within 40 m and goes on
turning over 80 m, round an arc the circle fitted to it describes: a notch or a step, out and back, is none) parks radially, tried
before the facades' normals: its axis through the centre of that arc, the nose NOSE_GAP_M off the outline, so a tip's gates fan out like a
round satellite's.
Gate pitch: a stand needs its span plus PITCH_GAP_M free beside the stands kept so far, along the wall (World.pitch_ok); the
kind is the first of the type's order (below) that fits, before the clearance test. No terminal within NEAR_M (a remote stand): a gate
with such a tag is placed on its point (nose wheel stop) by it; otherwise it lines up with the nearest taxiway or taxilane
within REMOTE_LANE_M (across it, the tail toward the line, where the aircraft is pushed back onto it; along it, nose
toward its end, where the line ends within REMOTE_END_M of the stand), a parking_position without one nose toward the
nearest edge of the apron it stands in, any other gate nose toward the nearest terminal within REMOTE_M, else none.
Type by the aerodrome's class (the polygon around the gate, else the nearest aerodrome feature, else regional):
international_airport -> narrow, a wide one a quarter of the gates (by hash) where it fits, else a narrow, else a turboprop;
regional / domestic / plain airport -> turboprop (3 in 5) or narrow (the other as the fallback); military, heliports, seaplane bases and the like -> none; the rest (airstrips,
private, other) -> light. Occupancy: a gate's id's crc32 under OCCUPIED_PCT (85: a hub's stands are mostly full; never random: a rebuilt tile is the same).
Paving (a cell that maps aprons): the nose gear and both main legs on it, or, for a terminal's stand, off it by up to PAVING_GAP_M
where the leg is nearer the terminal's wall than that paving is (the strip between a wall and an apron mapped short of it, or a hole
between patches); never a leg beyond the paving, away from the wall (grass: no land cover reaches the stands, so this is the sound test
there is).
General aviation: each apron (a polygon of at least APRON_MIN_M2) of an aerodrome that is neither international nor
military, or named / tagged general aviation (GA, aero club ...), gets ac_light every GA_SPACING_M along its edge, nose
to the edge, each slot kept by hash (GA_PCT), at most GA_MAX per apron (the lowest hashes), inside the apron.
Clearance (whole aircraft, fuselage + wing + tailplane rectangles, not a disc): dropped where it meets any building or
building part (the buildings Features and the building_parts Features alike: canopies and covered walkways count whatever
their height; OBSTACLE_MIN_M2 skips slivers; BUILDING_CLEAR_M margin, SKIN_GAP_M at a bridged gate); a gate's
aircraft that meets one is pulled back along its axis (tail-ward, PULL_BACK_STEP_M at a time up to PULL_BACK_MAX_M) before
it is dropped; a runway or
stopway (half its width + RUNWAY_CLEAR_M), a taxiway (its half width), a taxilane's centreline (one the nose points along is the stand's lead-in: none), or
another placed aircraft (AIRCRAFT_CLEAR_M), then a final pass (drop_overlaps) drops any aircraft whose footprint
overlaps one kept already, the longer kinds kept first. Gates are placed first (in position order, so closely mapped gates keep every
other one), then the aprons' rows; tile_rows drops an aircraft of one cell that overlaps another cell's. STATS counts why
gates got none (debug).

kept_stands(keys, infra_fn, buildings_fn) -> [Craft]: the stands of those cells after the cross-cell pass: what tile_rows
places (a Craft's mode "gate" / "remote" / "ga"; its `ref`, the gate's `ref` tag, "" without one).
lead_stands(keys, infra_fn, buildings_fn) -> [Craft]: every valid stand, with or without an aircraft (the gates the occupancy
hash leaves empty keep their stand: empty_stands), for the painted lead-ins and stand numbers of taxiwaymarks and the stand
signs of airfieldsigns; each cell's stands are judged from the cells round its own, so every tile reads the same list.
An aircraft at a gate (mode "gate") also has navigation lights (_nav_rows: red, green, white glow rows in airfieldlights'
groups, in tile_rows beside its row).

Liveries: each group has LIVERIES models (the mod's bne_ac_<kind>_<livery>: plain white, or a coloured tail fin, some with a
cheatline or a coloured belly; tools/build_aircraft.py), and a Craft's `livery` is the one it wears: an airport (the aerodrome
polygon around the stand, else the nearest, its centre to 0.01 degree; else the stand's own 0.01-degree box) has two or three
"airlines" (liveries chosen by a hash of that), and a stand takes one of them by a hash of its position; a general-aviation
row's light aircraft one of GA_FLEET (a private mix). The row's rank is rank_for(livery), which trees.lua's pick turns into
that model. All hashes (never random: a rebuilt tile is the same).

tile_rows(frame, box, infra_fn, buildings_fn, elev) -> forest rows [group, x, y, z, yaw, 1, H, rank] of the aircraft a tile
(box: x0, y0, x1, y1 in frame, or a geotile's OwnedBox) owns (each by its centre in one tile). infra_fn(key) -> the
cell's infrastructure Features (or None), buildings_fn(key) -> its buildings Features, or a list of Features (the building_parts
as well; or None; called only for a cell with gates or aprons), elev(lat, lon) -> the (graded) ground in metres (arrays). Yaw: the server's (counter-clockwise
from +x; the shape's +Y at yaw 0 is north), as windsocks'. A cell without airport gates or aprons costs one test.
"""
import collections
import hashlib
import math
import re
import zlib

import numpy as np

import aeroways
import airfieldlights as afl
import airportroles as ar
import dmath
import dnp
import geodesy
import geotiles

GROUPS = {"light": "ac_light", "turboprop": "ac_turboprop", "narrow": "ac_narrow", "wide": "ac_wide"}
# length, span, tail top (the CATALOG heights), fuselage width, wing chord, wing's centre ahead of the middle (m)
SPECS = {"light": (8.3, 11.0, 2.7, 1.3, 1.5, 0.3), "turboprop": (27.2, 27.1, 7.6, 2.8, 2.6, 1.0),
         "narrow": (37.6, 35.8, 11.8, 3.95, 4.5, 1.8), "wide": (63.7, 60.1, 17.5, 6.0, 7.5, 3.0)}
BACK_M = {"light": 2.0, "turboprop": 4.0, "narrow": 5.0, "wide": 6.5}      # a door's setback from the nose
# Navigation lights of an aircraft at a gate: kind -> (the wing tip's centre along the nose from the aircraft's centre (the
# model's: tools/build_aircraft.py's wing, swept back), its height over the ground, the tail cone's height)
NAV = {"light": (0.7, 1.6, 1.4), "turboprop": (1.2, 3.0, 3.6), "narrow": (-3.0, 4.5, 4.8), "wide": (-12.1, 6.5, 6.0)}
NAV_TAIL_IN_M = 0.4             # the white light this far ahead of the tail's tip
TAIL_SPAN_K, TAIL_CHORD_K, TAIL_AT_K = 0.34, 0.55, 0.40                   # the tailplane: of the span, chord, and its
                                                                           # centre behind the middle in lengths
GATE_CLASSES = ("airport_gate", "parking_position")
APRON = aeroways.APRON
SUBTYPE = aeroways.SUBTYPE
NOSE_GAP_M = 6.0                # m from the facade to the nose
NEAR_M = 60.0                   # m: a terminal further than this from a gate is none
TERMINAL_MIN_M2 = 1000.0
REMOTE_M = 260.0                # a stand off the terminals (no gate-side building within NEAR_M): the nose to the nearest one this near
FACADE_R_M = 30.0
ATTACH_M = 6.0                  # a jet bridge's nearest vertex this near the gate
BRIDGE_MAX_M2 = 300.0
BRIDGE_MIN_M, BRIDGE_CAP_M = 12.0, 50.0
BRIDGE_SIDE_M = 1.5             # m between a bridge's side and the fuselage (a small building by the gate)
OBSTACLE_MIN_M2 = 1.0           # a building or part smaller than this is a sliver
NARROW_W_M = 6.0                # a building or part this narrow and NARROW_RATIO times as long is a bridge or a finger:
NARROW_RATIO = 2.0              # ... no obstacle to the aircraft of a gate within NARROW_NEAR_M of it
NARROW_LEN_MAX_M = 60.0         # (a longer one is a wall)
NARROW_NEAR_M = 40.0
NARROW_WING_M = 60.0            # ... and one this near any gate (a neighbour's) is none to the wings and the tailplane
RAISED_MIN_M = 3.0              # a part raised this far (min_height) is no obstacle to the wings and the tailplane
NOSE_GEAR_K = 0.12              # the nose gear this many lengths behind the nose,
MAIN_GEAR_K = 0.58              # the main gear this many behind it,
MAIN_TRACK_K = 0.10             # each leg this many spans off the axis: all on the paving (aprons and taxiways)
PAVING_GAP_M = 25.0             # a terminal stand's gear may stand this far off the mapped paving, on the wall's side of it (mappers stop an apron short of the building's
                                # wall, or map it in scattered patches: unmapped paving between)
PAVING_SLACK_M = 3.0            # (a leg is on the wall's side of the paving where it is no further from the wall than the paving's nearest point, and this)
BUILDING_CLEAR_M = 1.5
PULL_BACK_MAX_M = 15.0          # a gate's aircraft that meets a building moves tail-ward by up to this far
PULL_BACK_STEP_M = 1.5
EDGE_MIN_M = 6.0                # a facade edge shorter than this gives its normal to the smoothed one
HEADING_MAX_DEG = 60.0          # a tagged heading within this of the facade's normal is used
HEADING_COS = dmath.cos(math.radians(HEADING_MAX_DEG))
REMOTE_LANE_M = 150.0           # a remote stand lines up with a taxiway / taxilane this near
REMOTE_END_M = 30.0             # ... and along one that ends this near
RUNWAY_CLEAR_M = 5.0
AIRCRAFT_CLEAR_M = 2.0
CENTRE_HIT_M = 1.6              # a point this near a centreline touches it (the sampling's step)
STEP_M = 1.5                    # the sampling of an aircraft's rectangles
OCCUPIED_PCT = 85                # a busy hub's stands are mostly full by day
GA_PCT = 50
GA_SPACING_M = 15.0
GA_MAX = 12
GA_INSET_M = 1.5                # the nose this far inside the apron's edge
APRON_MIN_M2 = 400.0
AERODROME_NEAR_M = 5000.0
NEIGHBOUR_M = 150.0            # m: a gate or apron this near a cell's edge asks the neighbour cell's buildings too
LEAD_IN_COS = dmath.cos(math.radians(20.0))
RAY_MAX_M = 400.0
WALL_MIN_M = 8.0                # m of solid behind a wall (less the double of the way out) for a gate inside a terminal
FACADES_MAX, FACADE_SEP_DEG = 6, 25.0
DOOR_K = 0.12                   # the forward left door: this many lengths back from the nose, half the fuselage's width out
CAB_MAX_M2 = 60.0               # a footprint this small within CAB_NEAR_M of a gate's bridge is its cab: no obstacle to that gate
CAB_NEAR_M = 4.0
SKIN_GAP_M = 0.3                # m: the skin gap a bridged gate's fuselage keeps off any building or bridge (the door's bridge-end contact aside)
BRIDGE_CAB_M = 2.0              # m: the last of the gate's own bridge (the cab at the door) is no obstacle to its aircraft
BRIDGE_YAWS_DEG = (0.0, -2.5, 2.5, -5.0, 5.0)      # the search for a bridged gate's pose: the axis turns by up to 5 degrees either way,
                                                   # the door within BRIDGE_CAB_M + SKIN_GAP_M of the bridge's end face (a cab's reach)
SEARCH_STEP_M, REFINE_STEP_M = 0.5, 0.1             # (a coarse grid, then a fine one round the best pose)
YAW_COST_M = 0.1                # a degree of yaw is worth this much door distance (the axis turns only where it gains more)
BRIDGE_AXIS_DEG = 35.0          # a bridge this far off the wall's normal turns the aircraft's axis to the bridge's (the nose
BRIDGE_AXIS_COS = dmath.cos(math.radians(BRIDGE_AXIS_DEG))      # still toward the building: at most BRIDGE_TURN_DEG off the normal)
BRIDGE_TURN_COS = dmath.cos(math.radians(75.0))
CENTRE_M = 3.0                  # a gate this near (or in) a pier's or a terminal's footprint is on its centreline: both sides tried
SIDE_REACH_M = 40.0             # ... the sides within this far of the gate, and no more than SIDE_SLACK_M
MACRO_HALF_M = (40.0, 24.0, 12.0)   # a wall's line is fitted to the outline this far each way of the facade point (the widest that is straight)
MACRO_STEP_M = 1.0              # (the outline sampled this finely)
MACRO_LINEAR = 0.2              # a window is straight where its spread across the fitted line is under this of its spread along it (a
                                # stepped pier's, a gently curved wall's: yes; a corner: no)
MACRO_EDGE_M = 12.0             # (the edge at the facade point shorter than this is a step; a longer one is the wall: the fitted line
MACRO_EDGE_COS = dmath.cos(math.radians(30.0))   # lies within 30 degrees of it)
MACRO_RADII_M = (24.0, 40.0, 60.0)  # ... else (a crenellated pier: a notch every 20 m) the edges' length-weighted principal direction (doubled
MACRO_COHERENCE = 0.1           # angles) within each of these of q, where each is this coherent and all lie within MACRO_AGREE_COS of the middle one
MACRO_AGREE_COS = dmath.cos(math.radians(15.0))
MACRO_SNAP_COS = dmath.cos(math.radians(50.0))   # a normal found by a ray (a gate inside its terminal) turns to the wall's line within this
SIDE_COS = dmath.cos(math.radians(35.0))
RADIAL_HALF_M = 20.0            # a wall's outline over this far each way of the facade point (40 m in all) that turns convex by more than
RADIAL_TURN = math.radians(30.0)   # this (its direction at the two ends: the principal direction of the last RADIAL_END_M) is a pier's tip or
RADIAL_END_M = 12.0             # a corner, and one that goes on turning (over RADIAL_FAR_M each way: a step or a notch, out and back, does not)
RADIAL_FAR_M = 40.0             # is where a gate parks radially: its axis from the gate to the centre of the circle fitted to the outline,
RADIAL_HAIRPIN = 0.35            # (a turn within this of a half turn: a tip narrower than the stretch: convex wherever its circle is inside)
RADIAL_R_MIN_M = 9.0            # (the circle's radius between these; a rounder or straighter outline has no tip: its normal is the wall's)
RADIAL_R_MAX_M = 80.0
RADIAL_FIT = 0.16               # ... and the circle fits the outline: its rms distance from it under this of the radius
SIDE_SLACK_M = 8.0              # ... further than the nearest wall (a gate just inside a wide terminal's facade is on one side)
PITCH_GAP_M = 4.0               # m: a stand needs its span plus this free between the stands kept beside it
PITCH_PARALLEL_COS = 0.5        # (beside: an axis within 60 degrees of the other's)
GA_NAME = re.compile(r"general aviation|\bga\b|aero ?club|light aircraft|flying club|aeroclub", re.I)
_KEY = "_parked_aircraft"
_SHAPES = {}
STATS = collections.Counter()   # debug: why gates got no aircraft (cleared by the caller; see _gate_craft)


LIVERIES = 7                    # the models of each group (trees.lua AIRCRAFT_LIVERIES, tools/build_aircraft.py LIVERIES); 0 is plain white
AIRLINES_MIN = 2                # an airport's "airlines": this many to AIRLINES_MAX distinct liveries
AIRLINES_MAX = 3
GA_FLEET = 4                    # a general-aviation row's light aircraft: this many liveries to the airport (a private mix)
AIRPORT_BOX_DEG = 0.01          # an airport's seed: its centre rounded to this

Craft = collections.namedtuple("Craft", "kind lat lon ne nn mode livery ref", defaults=("", 0, ""))   # ne, nn: the nose's east / north
                                                # direction; mode: "gate" (nose to a terminal), "remote" (a stand off the
                                                # terminals), "ga" (a general-aviation row); livery: 0 .. LIVERIES - 1;
                                                # ref: the gate's `ref` tag ("21", "A12"), "" without one


def _crc(text):
    return zlib.crc32(str(text).encode("utf-8", "replace"))


def _mix(text):
    """A well mixed 32-bit hash of a text (crc32 is linear: the seeds of near-equal texts order alike)."""
    return int.from_bytes(hashlib.md5(str(text).encode("utf-8", "replace")).digest()[:4], "big")


def rank_for(livery):
    """The row's rank that makes trees.lua's pick choose the group's model number `livery` (floor(rank * 7919) % LIVERIES;
    airsideprops.rank_for's way: a small rank, so every ring shows it)."""
    return round((3 * LIVERIES + int(livery) % LIVERIES + 0.5) / 7919.0, 6)


# ------------------------------------------------------------------------------------------------- shapes
def _rects(kind):
    """The aircraft's rectangles as (u0, u1, v0, v1): u along the nose from the centre, v to its left."""
    length, span, _h, fus, chord, wing_at = SPECS[kind]
    tail_at = -TAIL_AT_K * length
    return [(-length / 2.0, length / 2.0, -fus / 2.0, fus / 2.0),
            (wing_at - chord / 2.0, wing_at + chord / 2.0, -span / 2.0, span / 2.0),
            (tail_at - TAIL_CHORD_K * chord, tail_at + TAIL_CHORD_K * chord, -TAIL_SPAN_K * span / 2.0,
             TAIL_SPAN_K * span / 2.0)]


def _fuselage_n(kind):
    """How many of _local_points(kind)'s first points are the fuselage's."""
    u0, u1, v0, v1 = _rects(kind)[0]
    return (max(2, int(math.ceil((u1 - u0) / STEP_M)) + 1)) * (max(2, int(math.ceil((v1 - v0) / STEP_M)) + 1))


def _local_points(kind):
    """The (u, v) sample points (n, 2) of the aircraft's rectangles (edges included), kept."""
    got = _SHAPES.get(kind)
    if got is None:
        out = []
        for u0, u1, v0, v1 in _rects(kind):
            us = np.linspace(u0, u1, max(2, int(math.ceil((u1 - u0) / STEP_M)) + 1))
            vs = np.linspace(v0, v1, max(2, int(math.ceil((v1 - v0) / STEP_M)) + 1))
            out.append(np.stack(np.meshgrid(us, vs), -1).reshape(-1, 2))
        got = _SHAPES[kind] = np.concatenate(out)
    return got


def _world(pts, cx, cy, nx, ny):
    """Local (u, v) points in the plane: centre (cx, cy), nose direction (nx, ny), v to the left of it."""
    return np.stack([cx + pts[:, 0] * nx - pts[:, 1] * ny, cy + pts[:, 0] * ny + pts[:, 1] * nx], 1)


def _in_rects(kind, cx, cy, nx, ny, pts, margin, first=None):
    """Which plane points lie within `margin` of the aircraft's rectangles (first: only the first so many: the fuselage's)."""
    d = pts - (cx, cy)
    u = d[:, 0] * nx + d[:, 1] * ny
    v = -d[:, 0] * ny + d[:, 1] * nx
    hit = np.zeros(len(pts), bool)
    for u0, u1, v0, v1 in _rects(kind)[:first]:
        hit |= (u >= u0 - margin) & (u <= u1 + margin) & (v >= v0 - margin) & (v <= v1 + margin)
    return hit


def _radius(kind):
    return math.hypot(SPECS[kind][0] / 2.0, SPECS[kind][1] / 2.0)


# ------------------------------------------------------------------------------------------------- geometry
def _pip(ring, pts):
    """Which of pts (m, 2) are inside the polygon ring (k, 2) (even-odd)."""
    a, b = ring, np.roll(ring, -1, axis=0)
    y = pts[:, 1:2]
    cross = (a[:, 1] <= y) != (b[:, 1] <= y)
    dy = np.where(b[:, 1] == a[:, 1], 1.0, b[:, 1] - a[:, 1])
    xi = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / dy
    return ((cross & (pts[:, 0:1] < xi)).sum(1) % 2) == 1


_DENSE = {}


def _dense(ring):
    """The points along the polygon `ring`'s outline every SKIN_GAP_M (kept, per ring object): a rectangle that has none within its
    margin does not cross or touch the outline."""
    got = _DENSE.get(id(ring))
    if got is None or got[0] is not ring:
        if len(_DENSE) > 4096:
            _DENSE.clear()
        a, b = ring, np.roll(ring, -1, axis=0)
        out = []
        for i in range(len(a)):
            n = max(1, int(math.ceil(float(dnp.hypot(*(b[i] - a[i]))) / SKIN_GAP_M)))
            t = np.arange(n) / n
            out.append(a[i] + t[:, None] * (b[i] - a[i]))
        got = _DENSE[id(ring)] = (ring, np.concatenate(out))
    return got[1]


def _seg_dist(pts, a, b):
    """(m, k) distances from pts (m, 2) to the segments a -> b (k, 2)."""
    ab = b - a
    ap = pts[:, None, :] - a[None]
    t = np.clip(dnp.vecdot(ap, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12)[None], 0.0, 1.0)
    d = ap - t[..., None] * ab[None]
    return dnp.hypot(d[..., 0], d[..., 1])


def _area(ring):
    return 0.5 * abs(float(dnp.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - dnp.dot(np.roll(ring[:, 0], -1), ring[:, 1])))


def _nearest_seg(a, b, p):
    """(distance, nearest point, index, parameter 0..1) of the segments a -> b (k, 2) to p."""
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    q = a + t[:, None] * ab
    d = dnp.hypot(*(q - p).T)
    j = int(np.argmin(d))
    return float(d[j]), q[j], j, float(t[j])


def _nearest_on(ring, p):
    """(distance, nearest point, edge index) of the ring's edges to p."""
    d, q, j, _t = _nearest_seg(ring, np.roll(ring, -1, axis=0), p)
    return d, q, j


def _flat(items):
    """The Features (not None) of a Features or of nested lists of them, in order."""
    out = []
    for b in (items if isinstance(items, (list, tuple)) else [items]):
        if isinstance(b, (list, tuple)):
            out += _flat(b)
        elif b is not None:
            out.append(b)
    return out


def _snap_normal(ring, q, d):
    """The unit normal (out of the building) of the ring edge nearest the exit point q where that edge is at least
    EDGE_MIN_M long and within HEADING_MAX_DEG of the ray's direction d (a ray in 5 degree steps is not quite across
    the wall), else d."""
    _dist, _q, j = _nearest_on(ring, q)
    e = ring[(j + 1) % len(ring)] - ring[j]
    ln = float(dnp.hypot(*e))
    if ln < EDGE_MIN_M:
        return d
    n = np.array([-e[1], e[0]]) / ln
    if float(dnp.matmul(n, d)) < 0.0:
        n = -n
    return n if float(dnp.matmul(n, d)) >= HEADING_COS else d


def _facade(ring, p):
    """(point q on the facade nearest p, unit outward normal) of a building ring: the normal is across the length-weighted
    principal direction of the edges within FACADE_R_M of q (a doubled angle: an edge has no sense), pointing out."""
    d0, q, j0 = _nearest_on(ring, p)
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ln = np.maximum(dnp.hypot(ab[:, 0], ab[:, 1]), 1e-9)
    near = dnp.hypot(*(((a + b) / 2.0) - q).T) <= FACADE_R_M + ln / 2.0
    w = np.minimum(ln, FACADE_R_M)[near]
    ang = dnp.arctan2(ab[near, 1], ab[near, 0])
    sy, sx = float((w * dnp.sin(2 * ang)).sum()), float((w * dnp.cos(2 * ang)).sum())
    if dnp.hypot(sx, sy) < 0.3 * float(w.sum()):          # (edges that cancel, a corner or a zig-zag: the nearest edge's)
        t = dmath.atan2(float(ab[j0, 1]), float(ab[j0, 0]))
    else:
        t = 0.5 * dmath.atan2(sy, sx)
    n = np.array([-dmath.sin(t), dmath.cos(t)])
    inside = bool(_pip(ring, (q + n * 2.0)[None])[0])
    if inside:
        n = -n
    return q, n


def _outward(ring, q, n):
    """The unit normal n or its opposite, whichever points out of the building ring at q: the one with less of the ring on its
    side over several distances (a notch of a crenellated wall does not fool it)."""
    ds = np.array([2.0, 5.0, 10.0, 20.0])
    ahead = int(_pip(ring, q[None] + ds[:, None] * n[None]).sum())
    behind = int(_pip(ring, q[None] - ds[:, None] * n[None]).sum())
    return -n if ahead > behind or (ahead == behind and bool(_pip(ring, (q + n * 3.0)[None])[0])) else n


def _outline(ring, q):
    """(points (m, 2) of the ring sampled every MACRO_STEP_M, each one's signed arc distance (m,) along the ring from q (the
    shorter way round), the index of q's edge, the ring's length) or None where the ring is too short or q is off its outline."""
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ln = dnp.hypot(ab[:, 0], ab[:, 1])
    total = float(ln.sum())
    if total < 4.0 * MACRO_STEP_M:
        return None
    d, _q, j = _nearest_on(ring, q)
    if d > 2.0:
        return None
    cnt = np.maximum(1, np.ceil(ln / MACRO_STEP_M).astype(int))
    start = np.cumsum(ln) - ln
    idx = np.repeat(np.arange(len(a)), cnt)
    first = np.cumsum(cnt) - cnt
    frac = (np.arange(int(cnt.sum())) - first[idx]) / cnt[idx]
    pts = a[idx] + ab[idx] * frac[:, None]
    arc = start[idx] + ln[idx] * frac
    sq = start[j] + float(dnp.hypot(*(q - a[j])))
    ds = arc - sq
    ds = np.where(ds > total / 2.0, ds - total, np.where(ds < -total / 2.0, ds + total, ds))       # (signed, along the ring)
    return pts, ds, j, total


def _run_axis(w):
    """The unit principal direction of the points w (k, 2) in order, pointing the way they run."""
    c = w - w.mean(0)
    sxx, syy, sxy = float((c[:, 0] * c[:, 0]).sum()), float((c[:, 1] * c[:, 1]).sum()), float((c[:, 0] * c[:, 1]).sum())
    t = 0.5 * dmath.atan2(2.0 * sxy, sxx - syy)
    v = np.array([dmath.cos(t), dmath.sin(t)])
    return v if float(dnp.matmul(w[-1] - w[0], v)) >= 0.0 else -v


def _fit_circle(w):
    """(centre (2,), radius, rms distance of the points from the circle) of the circle fitted to the points w (k, 2) (the algebraic fit);
    (None, 0, 0) where they are on a line."""
    m = w.mean(0)
    c = w - m
    sol = dnp.lstsq(np.stack([c[:, 0], c[:, 1], np.ones(len(c))], 1), -(c[:, 0] * c[:, 0] + c[:, 1] * c[:, 1]))[0]
    cx, cy = -sol[0] / 2.0, -sol[1] / 2.0
    r2 = float(cx * cx + cy * cy - sol[2])
    if r2 <= 0.0:
        return None, 0.0, 0.0
    r = math.sqrt(r2)
    d = dnp.hypot(c[:, 0] - cx, c[:, 1] - cy) - r
    return m + np.array([cx, cy]), r, math.sqrt(float((d * d).mean()))


def _end_turn(pts, ds, h):
    """The signed turn (radians) of the outline's direction from its stretch's start (-h) to its end (+h) about the point the arc
    distances `ds` are from: the principal directions of the first and the last RADIAL_END_M of the stretch (None: too few points)."""
    left = np.flatnonzero((ds >= -h) & (ds <= -h + RADIAL_END_M))
    right = np.flatnonzero((ds >= h - RADIAL_END_M) & (ds <= h))
    if len(left) < 8 or len(right) < 8:
        return None
    dl, dr = _run_axis(pts[left[np.argsort(ds[left])]]), _run_axis(pts[right[np.argsort(ds[right])]])
    return dmath.atan2(float(dl[0] * dr[1] - dl[1] * dr[0]), float(dl[0] * dr[0] + dl[1] * dr[1]))


def radial_centre(ring, q, p=None):
    """The centre (2,) of the arc a gate near the outline's point q of a building ring parks radially to, where the outline turns convex
    by more than RADIAL_TURN within 2 * RADIAL_HALF_M about q and by that much still within 2 * RADIAL_FAR_M (a pier's tip, a corner of
    the wall; the outline's direction at the two ends of the stretch is the principal direction of its first and last RADIAL_END_M,
    so a step in a wall, or a notch, or a finger, out and back, is none): the centre of the circle fitted to the nearer
    stretch (radius RADIAL_R_MIN_M to RADIAL_R_MAX_M), else its centroid, in either case inside the ring. p (a nose): None where it
    lies on the far side of the centre from q (the radial would point into the building). None where q is on a wall that does not turn
    that way (a straight or a concave one, a rounder arc: the wall's normal then)."""
    got = _outline(ring, q)
    if got is None:
        return None
    pts, ds, _j, total = got
    h = RADIAL_HALF_M
    if total <= 2.0 * RADIAL_FAR_M + 8.0:
        return None
    sign = float(dnp.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - dnp.dot(np.roll(ring[:, 0], -1), ring[:, 1]))   # (twice the signed area)
    for half in (h, RADIAL_FAR_M):
        turn = _end_turn(pts, ds, half)
        if turn is None or abs(turn) <= RADIAL_TURN:
            return None
        if half == h and abs(turn) < math.pi - RADIAL_HAIRPIN and turn * sign <= 0.0:       # (a hairpin has no sense: the circle's centre decides)
            return None
    w = pts[(ds >= -h) & (ds <= h)]
    centre, radius, rms = _fit_circle(w)
    if centre is None or not RADIAL_R_MIN_M <= radius <= RADIAL_R_MAX_M or rms > RADIAL_FIT * radius             or not bool(_pip(ring, centre[None])[0]):
        return None
    if p is not None and float(dnp.matmul(p - centre, q - centre)) <= 0.0:
        return None
    return centre


def wall_normal(ring, q):
    """The unit outward normal of the building ring's wall at its point q, from a line fitted to the outline (principal axis
    of the sampled points) over the widest window of MACRO_HALF_M about q that is straight (MACRO_LINEAR), centred on q or, near
    a corner, running one way from it: a pier drawn with a notch or a protrusion at each gate (Frankfurt's: steps of 4 m every
    30 m), a diagonal wall or a curved one gets the normal of its general line, not of the step it happens to stand at. Where
    the edge at q is short (a step: under MACRO_EDGE_M) and no window is straight, the edges' length-weighted principal
    direction (a crenellated pier). None where neither holds (a corner) or q is off the outline."""
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ln = dnp.hypot(ab[:, 0], ab[:, 1])
    got = _outline(ring, q)
    if got is None:
        return None
    pts, ds, j, total = got
    for half in MACRO_HALF_M:
        if 2.0 * half >= total:
            continue
        for lo, hi in ((-half, half), (0.0, half), (-half, 0.0)):
            w = pts[(ds >= lo) & (ds <= hi)]
            if len(w) < 8:
                continue
            c = w - w.mean(0)
            sxx, syy, sxy = float((c[:, 0] * c[:, 0]).sum()), float((c[:, 1] * c[:, 1]).sum()), float((c[:, 0] * c[:, 1]).sum())
            mid, rad = (sxx + syy) / 2.0, math.sqrt(((sxx - syy) / 2.0) * ((sxx - syy) / 2.0) + sxy * sxy)
            big, small = mid + rad, max(mid - rad, 0.0)
            if big <= 0.0 or math.sqrt(small / big) > MACRO_LINEAR:
                continue
            t = 0.5 * dmath.atan2(2.0 * sxy, sxx - syy)
            along = np.array([dmath.cos(t), dmath.sin(t)])
            if float(ln[j]) >= MACRO_EDGE_M and abs(float(dnp.matmul(along, ab[j]))) < MACRO_EDGE_COS * float(ln[j]):
                continue                                        # (a long edge at q is the wall: a window that turns a corner is not)
            return _outward(ring, q, np.array([-along[1], along[0]]))
    if float(ln[j]) >= MACRO_EDGE_M:
        return None
    ang = dnp.arctan2(ab[:, 1], ab[:, 0])
    centre = (a + b) / 2.0
    got = []
    for r in MACRO_RADII_M:
        near = dnp.hypot(*(centre - q).T) <= r + ln / 2.0
        w = np.minimum(ln, r)[near]
        sy, sx = float((w * dnp.sin(2 * ang[near])).sum()), float((w * dnp.cos(2 * ang[near])).sum())
        if not len(w) or dnp.hypot(sx, sy) < MACRO_COHERENCE * float(w.sum()):
            return None
        t = 0.5 * dmath.atan2(sy, sx)
        got.append(np.array([-dmath.sin(t), dmath.cos(t)]))
    if any(abs(float(dnp.matmul(v, got[1]))) < MACRO_AGREE_COS for v in got):
        return None
    return _outward(ring, q, got[1])


# ------------------------------------------------------------------------------------------------- the scene
class _Scene:
    """A Features' airport in a metre plane about (lat0, lon0): gates, aprons, aerodromes, pavement lines."""

    def __init__(self, feats):
        self.feats = feats
        self.lat0 = self.lon0 = None
        self.gates, self.aprons, self.aerodromes = [], [], []
        self.refs = {}                          # gate id -> its `ref` tag (a stand's number)
        self.lines = []                         # (xy (n, 2), half width, mode): "pavement" or "centre"
        self.lanes = []                         # the taxiways' and taxilanes' lines (xy (n, 2))
        self.runways = []                       # the runways' lines (xy (n, 2))
        self.intl = False

    def xy(self, ring):
        ring = np.asarray(ring, np.float64)
        return np.stack([(ring[:, 0] - self.lon0) * self.kx, (ring[:, 1] - self.lat0) * aeroways.M_PER_DEG], 1)

    def latlon(self, x, y):
        return self.lat0 + y / aeroways.M_PER_DEG, self.lon0 + x / self.kx


def _tier(cls):
    """An aerodrome class's tier: intl / regional / light / military / none (None: not an aerodrome)."""
    c = str(cls)
    if c == aeroways.AERODROME:
        return "intl"
    if c in ("regional_airport", "domestic_airport", "airport", "public_airport", "medium_airport", "large_airport"):
        return "regional"
    if c in ("military_airport", "military", "military_airfield", "air_base"):
        return "military"
    if "heliport" in c or "seaplane" in c or "closed" in c:
        return "none"
    if c.endswith("airport") or c.endswith("airfield") or c.endswith("airstrip") or c in ("aerodrome", "airfield"):
        return "light"
    return None


def _scene(feats):
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is None or cls is None or not len(feats):
        return None
    air = np.asarray(sub).astype(str) == SUBTYPE
    cls = np.asarray(cls).astype(str)
    tags_col = feats.attrs.get("tags")
    name_col = feats.attrs.get("name")
    gate_like = air & np.isin(cls, GATE_CLASSES)
    if tags_col is not None:                                    # (a gate tagged only, under another class)
        tags_s = np.asarray(tags_col).astype(str)
        gate_like |= air & ((np.char.find(tags_s, '"gate"') >= 0) | (np.char.find(tags_s, "parking_position") >= 0))
    aprons = air & (cls == APRON) & (np.asarray(feats.kind) == 3)
    if not (gate_like.any() or aprons.any()):
        return None
    sc = _Scene(feats)
    first = int(np.flatnonzero(gate_like | aprons)[0])
    pt = np.asarray(feats.parts(first)[0][0], np.float64)[0]
    sc.lon0, sc.lat0 = float(pt[0]), float(pt[1])
    sc.kx = aeroways.M_PER_DEG * dmath.cos(math.radians(sc.lat0))
    sc.intl = bool((air & (cls == aeroways.AERODROME)).any())
    for i in np.flatnonzero(air):
        kind = int(feats.kind[i])
        tags = aeroways._tags(feats, i)
        c = cls[i]
        parts = feats.parts(int(i))
        if not parts or not parts[0] or not len(parts[0][0]):
            continue
        if gate_like[i] and kind == 1:
            x, y = sc.xy(parts[0][0][:1])[0]
            d = None
            for k in ("direction", "heading", "bearing"):
                try:
                    d = float(str(tags.get(k, "")).strip()) % 360.0
                    break
                except ValueError:
                    pass
            sc.gates.append((str(feats.ids[i]), float(x), float(y), d, c == "parking_position"
                             or tags.get("aeroway") == "parking_position"))
            ref = str(tags.get("ref", "")).strip()
            if ref:
                sc.refs[str(feats.ids[i])] = ref
            continue
        tier = _tier(c) if c != APRON else None
        if tier is not None:
            for part in parts:
                if kind == 3 and len(part[0]) >= 3:
                    sc.aerodromes.append((tier, sc.xy(part[0])))
                elif kind == 1:
                    sc.aerodromes.append((tier, sc.xy(part[0][:1])))
            continue
        if c == APRON and kind == 3:
            name = str(name_col[i]) if name_col is not None else ""
            ga = bool(GA_NAME.search(name + " " + " ".join(str(v) for v in tags.values())))
            for p, part in enumerate(parts):
                if len(part[0]) >= 3:
                    sc.aprons.append((f"{feats.ids[i]}#{p}", sc.xy(part[0]), ga))
        elif c in ("runway", "stopway", "taxiway", "taxilane") and kind == 2 and not aeroways._unpaved(feats, i, tags):
            if c in ("runway", "stopway"):
                half = (aeroways.width_tag(tags.get("width")) or (
                    aeroways.RUNWAY_INTL_M if sc.intl else aeroways.RUNWAY_M)) / 2.0 + RUNWAY_CLEAR_M
                mode = "pavement"
            else:
                w = aeroways.width_tag(tags.get("width")) or (
                    (aeroways.TAXIWAY_INTL_M if sc.intl else aeroways.TAXIWAY_M) if c == "taxiway"
                    else (aeroways.TAXILANE_INTL_M if sc.intl else aeroways.TAXILANE_M))
                half, mode = (w / 2.0, "pavement") if c == "taxiway" else (CENTRE_HIT_M, "centre")
            for part in parts:
                for line in part:
                    if len(line) >= 2:
                        xy = sc.xy(line)
                        sc.lines.append((xy, half, mode))
                        if c in ("taxiway", "taxilane"):
                            sc.lanes.append(xy)
                        elif c == "runway":
                            sc.runways.append(xy)
    return sc


def _aerodrome(sc, x, y):
    """The tier of the aerodrome around (x, y): the polygon holding it, else the nearest feature within
    AERODROME_NEAR_M, else None."""
    p = np.array([[x, y]])
    best, bd = None, AERODROME_NEAR_M
    for tier, geom in sc.aerodromes:
        if len(geom) >= 3 and _pip(geom, p)[0]:
            return tier
        d = float(dnp.hypot(*(geom - (x, y)).T).min())
        if d < bd:
            best, bd = tier, d
    return best


def _airport_seed(sc, x, y):
    """A number naming the airport around (x, y): the centre of the aerodrome polygon holding it (else the nearest feature
    within AERODROME_NEAR_M, else the point), rounded to AIRPORT_BOX_DEG, hashed."""
    p = np.array([[x, y]])
    best, bd = None, AERODROME_NEAR_M
    for _tier_, geom in sc.aerodromes:
        if len(geom) >= 3 and _pip(geom, p)[0]:
            best = geom
            break
        d = float(dnp.hypot(*(geom - (x, y)).T).min())
        if d < bd:
            best, bd = geom, d
    cx, cy = (float(v) for v in (best.mean(0) if best is not None else (x, y)))
    la, lo = sc.latlon(cx, cy)
    return _mix(f"{round(la / AIRPORT_BOX_DEG)}:{round(lo / AIRPORT_BOX_DEG)}")


def _fleet(seed, n=None):
    """An airport's liveries, in a seeded order: AIRLINES_MIN to AIRLINES_MAX of them (n of them where given)."""
    if n is None:
        n = AIRLINES_MIN + (seed >> 7) % (AIRLINES_MAX - AIRLINES_MIN + 1)
    return sorted(range(LIVERIES), key=lambda i: (_mix(f"{seed}:livery:{i}"), i))[:n]


def livery_for(seed, lat, lon, mode):
    """The livery (0 .. LIVERIES - 1) of the aircraft at (lat, lon) of an airport (its seed): one of the airport's airlines,
    by a hash of the position (to 1 m); a general-aviation row's from the wider GA_FLEET."""
    fleet = _fleet(seed, GA_FLEET if mode == "ga" else None)
    return fleet[_mix(f"{seed}:{lat:.5f}:{lon:.5f}") % len(fleet)]


# ------------------------------------------------------------------------------------------------- clearance
class _World:
    """What an aircraft must keep off: buildings, pavement lines and the aircraft already placed."""

    def __init__(self, sc, rings):
        self.sc = sc
        self.rings = rings                      # obstacle buildings: (ring, (x0, y0, x1, y1))
        self.raised = set(getattr(rings, "raised", ()))     # indexes of the rings raised over the wings and the tailplane
        self.narrow = tuple(sorted(getattr(rings, "narrow", ())))   # ... of the narrow, elongated ones
        self.paving = None                      # [(ring, bbox)] the aprons' and taxiways' pads (None: not tested)
        self.placed = []                        # (kind, x, y, nx, ny)
        self.bridge_obs = set()                 # indexes of the rings that airportroles calls jet bridges
        segs = [(ln[:-1], ln[1:], np.full(len(ln) - 1, h), np.full(len(ln) - 1, m == "centre"))
                for ln, h, m in sc.lines]
        if segs:
            self.a, self.b = np.concatenate([s[0] for s in segs]), np.concatenate([s[1] for s in segs])
            self.half = np.concatenate([s[2] for s in segs])
            self.centre = np.concatenate([s[3] for s in segs])
        else:
            self.a = self.b = np.zeros((0, 2))
            self.half = np.zeros(0)
            self.centre = np.zeros(0, bool)

    def clear(self, kind, x, y, nx, ny, inside=None):
        """Does an aircraft of `kind` at (x, y) heading (nx, ny) keep clear (and, with `inside` (a ring), lie in it)?"""
        return self.why(kind, x, y, nx, ny, inside) is None

    def narrow_near(self, x, y, near=NARROW_NEAR_M):
        """The indexes of the narrow, elongated rings (bridges, fingers) within `near` m of (x, y)."""
        return tuple(oi for oi in self.narrow if float(dnp.hypot(*(self.rings[oi][0] - (x, y)).T).min()) <= near)

    def paved(self, kind, x, y, nx, ny, strip=0.0, wall=None):
        """Are the aircraft's nose gear and both main legs on the paving (always, where it is not tested)? strip (with `wall`, the ring of
        the gate's terminal): each may be off it by this far where it is nearer the wall than the paving is (the unmapped strip between a
        terminal's wall and its apron, or between patches), never beyond the paving, away from the wall: that is grass."""
        if not self.paving:
            return True
        length, span = SPECS[kind][0], SPECS[kind][1]
        local = np.array([[length / 2.0 - NOSE_GEAR_K * length, 0.0],
                          [length / 2.0 - MAIN_GEAR_K * length, MAIN_TRACK_K * span],
                          [length / 2.0 - MAIN_GEAR_K * length, -MAIN_TRACK_K * span]])
        pts = _world(local, x, y, nx, ny)
        for p in pts:
            q = p[None]
            near = None                                     # (the nearest paving within strip: distance, its nearest point)
            on = False
            for ring, (x0, y0, x1, y1) in self.paving:
                if not (x0 - strip <= p[0] <= x1 + strip and y0 - strip <= p[1] <= y1 + strip):
                    continue
                if bool(_pip(ring, q)[0]):
                    on = True
                    break
                if strip > 0.0:
                    d, nq, _j = _nearest_on(ring, p)
                    if d <= strip and (near is None or d < near[0]):
                        near = (d, nq)
            if on:
                continue
            if near is None or (wall is not None and _nearest_on(wall, p)[0] >= _nearest_on(wall, near[1])[0] + PAVING_SLACK_M):
                return False
        return True

    def why(self, kind, x, y, nx, ny, inside=None, ignore=(), repl=None, strip=0.0, wall=None, skin=None):
        """None where an aircraft of `kind` at (x, y) heading (nx, ny) keeps clear (and lies in the ring `inside`), else
        the first thing it meets: "outside", "building", "taxiway" (runways, stopways, taxiways), "taxilane", "aircraft",
        "unpaved" (a gear leg off the paving by more than `strip`, or beyond it from the terminal `wall`). ignore: indexes into self.rings of buildings that are no obstacle (a narrow
        piece by the gate); repl: {index: (ring, bbox) or None}, the ring to test instead (a gate's own jet bridge less its
        cab), None for none; a raised part is one to the fuselage only; skin: the margin kept off every building (default
        BUILDING_CLEAR_M)."""
        pts = _world(_local_points(kind), x, y, nx, ny)
        n_fus = _fuselage_n(kind)
        r = _radius(kind) + BUILDING_CLEAR_M
        if inside is not None and not _pip(inside, pts).all():
            return "outside"
        for oi, (ring, (x0, y0, x1, y1)) in enumerate(self.rings):
            if oi in ignore:
                continue
            if repl and oi in repl:
                if repl[oi] is None:
                    continue
                ring, (x0, y0, x1, y1) = repl[oi]
            if x0 - r > x or x1 + r < x or y0 - r > y or y1 + r < y:
                continue
            hit = pts[:n_fus] if oi in self.raised else pts
            if skin is not None:                # (the plan's rectangles against the outline, sampled finer than the skin gap: exact)
                if _pip(ring, hit).any() or _in_rects(kind, x, y, nx, ny, _dense(ring), skin, 1 if oi in self.raised else None).any():
                    return "building"
                continue
            if _pip(ring, hit).any() or _seg_dist(hit, ring, np.roll(ring, -1, axis=0)).min() < BUILDING_CLEAR_M:
                return "building"
        if len(self.a):
            lo, hi = np.minimum(self.a, self.b) - r, np.maximum(self.a, self.b) + r
            keep = (lo[:, 0] <= x) & (hi[:, 0] >= x) & (lo[:, 1] <= y) & (hi[:, 1] >= y)
            if keep.any():                      # (a taxilane the nose points along is the stand's own lead-in: no clash)
                ab = self.b[keep] - self.a[keep]
                along = np.abs(ab[:, 0] * nx + ab[:, 1] * ny) > LEAD_IN_COS * np.maximum(dnp.hypot(ab[:, 0], ab[:, 1]), 1e-9)
                keep[np.flatnonzero(keep)[along & self.centre[keep]]] = False
            if keep.any():
                hit = _seg_dist(pts, self.a[keep], self.b[keep]).min(0) < self.half[keep]
                if hit.any():
                    return "taxilane" if self.centre[keep][hit].any() else "taxiway"
        for k2, x2, y2, nx2, ny2 in self.placed:
            if math.hypot(x2 - x, y2 - y) > _radius(kind) + _radius(k2) + AIRCRAFT_CLEAR_M:
                continue
            if _in_rects(k2, x2, y2, nx2, ny2, pts, AIRCRAFT_CLEAR_M).any() or \
                    _in_rects(kind, x, y, nx, ny, _world(_local_points(k2), x2, y2, nx2, ny2), AIRCRAFT_CLEAR_M).any():
                return "aircraft"
        return None if self.paved(kind, x, y, nx, ny, strip, wall) else "unpaved"

    def add(self, kind, x, y, nx, ny):
        self.placed.append((kind, x, y, nx, ny))

    def pitch_ok(self, kind, x, y, nx, ny):
        """Is the stand's span, PITCH_GAP_M more, free beside the stands kept so far (those side by side along the wall: an
        axis within 60 degrees of this one's, the centres no further apart along the axis than their lengths' half sum)?
        The gate's pitch."""
        length, span = SPECS[kind][0], SPECS[kind][1]
        tx, ty = -ny, nx
        for k2, x2, y2, nx2, ny2 in self.placed:
            if abs(nx * nx2 + ny * ny2) < PITCH_PARALLEL_COS:
                continue
            dx, dy = x2 - x, y2 - y
            if abs(dx * nx + dy * ny) >= (length + SPECS[k2][0]) / 2.0:
                continue
            if abs(dx * tx + dy * ty) - SPECS[k2][1] / 2.0 < span / 2.0 + PITCH_GAP_M / 2.0:
                return False
        return True


class _Rings(list):
    """The obstacle rings [(ring, bbox)], with the indexes of the raised ones (`raised`: a part's min_height of at least
    RAISED_MIN_M) and of the narrow, elongated ones (`narrow`: bridges and fingers)."""

    def __init__(self):
        super().__init__()
        self.raised, self.narrow = set(), set()


def _extent(ring):
    """(length, width) m of the ring's smallest bounding rectangle (along one of its edges)."""
    best = None
    e = np.roll(ring, -1, axis=0) - ring
    n = dnp.hypot(e[:, 0], e[:, 1])
    for k in np.flatnonzero(n > 1e-6):
        u = e[k] / n[k]
        du, dv = dnp.matmul(ring, u), dnp.matmul(ring, np.array([-u[1], u[0]]))
        a, b = float(du.max() - du.min()), float(dv.max() - dv.min())
        if best is None or a * b < best[0] * best[1]:
            best = (a, b)
    return (0.0, 0.0) if best is None else (max(best), min(best))


def _num(col, i):
    """A numeric column's value at i, 0 where it is missing."""
    try:
        v = float(col[i])
    except (TypeError, ValueError, IndexError):
        return 0.0
    return v if v == v else 0.0


def _buildings(sc, bld):
    """(obstacle rings [(ring, bbox)] (a _Rings), small buildings [(ring, area, index in the obstacles)]) of the buildings
    and the building parts Features (one, or a list), in the plane: every footprint is an obstacle, whatever its height."""
    obstacles, small = _Rings(), []
    blds = [b for b in _flat(bld) if len(b)]
    if not blds:
        return obstacles, small
    # (only the rings near the airport's features)
    xs = [g[1] for g in sc.gates]
    ys = [g[2] for g in sc.gates]
    for _i, ring, _a in sc.aprons:
        xs += [float(ring[:, 0].min()), float(ring[:, 0].max())]
        ys += [float(ring[:, 1].min()), float(ring[:, 1].max())]
    if not xs:
        return obstacles, small
    box = (min(xs) - 200.0, min(ys) - 200.0, max(xs) + 200.0, max(ys) + 200.0)
    seen = set()                                # (a building spanning cells is in each)
    for bld, i in ((b, i) for b in blds for i in range(len(b))):
        under = bld.attrs.get("is_underground")
        if int(bld.kind[i]) != 3 or (under is not None and bool(under[i])):
            continue
        lift = _num(bld.attrs.get("min_height"), i) if bld.attrs.get("min_height") is not None else 0.0
        for pi, part in enumerate(bld.parts(i)):
            if not part or len(part[0]) < 3:
                continue
            ring = part[0]
            if (str(bld.ids[i]), pi) in seen:
                continue
            seen.add((str(bld.ids[i]), pi))
            if ring[:, 0].max() < sc.lon0 + box[0] / sc.kx or ring[:, 0].min() > sc.lon0 + box[2] / sc.kx or \
                    ring[:, 1].max() < sc.lat0 + box[1] / aeroways.M_PER_DEG or \
                    ring[:, 1].min() > sc.lat0 + box[3] / aeroways.M_PER_DEG:
                continue
            r = sc.xy(ring)
            area = _area(r)
            if area >= OBSTACLE_MIN_M2:
                if area < BRIDGE_MAX_M2:
                    small.append((r, area, len(obstacles)))
                if lift >= RAISED_MIN_M:
                    obstacles.raised.add(len(obstacles))
                length, width = _extent(r)
                if width <= NARROW_W_M and NARROW_RATIO * width <= length <= NARROW_LEN_MAX_M:
                    obstacles.narrow.add(len(obstacles))
                obstacles.append((r, (float(r[:, 0].min()), float(r[:, 1].min()), float(r[:, 0].max()),
                                      float(r[:, 1].max()))))
    return obstacles, small


# ------------------------------------------------------------------------------------------------- the stands
def _exit_out(ring, pts, d):
    """The distances along the unit directions d (k, 2) from the points pts (k, 2), inside the ring, to its first edge
    (RAY_MAX_M where none)."""
    a, b = ring, np.roll(ring, -1, axis=0)
    e = b - a
    w = a[None] - pts[:, None, :]                                    # (k, m, 2)
    den = d[:, 0:1] * e[None, :, 1] - d[:, 1:2] * e[None, :, 0]      # d x e, (k, m)
    ok = np.abs(den) > 1e-9
    den = np.where(ok, den, 1.0)
    t = (w[..., 0] * e[None, :, 1] - w[..., 1] * e[None, :, 0]) / den
    u = (w[..., 0] * d[:, 1:2] - w[..., 1] * d[:, 0:1]) / den
    hit = ok & (t > 1e-6) & (u >= 0.0) & (u <= 1.0)
    return np.where(hit, t, RAY_MAX_M).min(1)


def _inside_facades(ring, p):
    """[(point, outward normal)] ways out for a gate inside its terminal's polygon, where the nearest edge is no facade
    (a jet bridge or a pier drawn into the building, a gate at a finger's root): the directions whose ray leaves the polygon
    soonest, the ones with solid behind them (a wall; a finger's tip along its axis) first, at most FACADES_MAX apart by
    FACADE_SEP_DEG."""
    ang = np.radians(np.arange(0.0, 360.0, 5.0))
    d = np.stack([dnp.cos(ang), dnp.sin(ang)], 1)
    pp = np.tile(p, (len(d), 1))
    fwd, back = _exit_out(ring, pp, d), _exit_out(ring, pp, -d)
    order = sorted(range(len(d)), key=lambda i: (back[i] - 2.0 * fwd[i] < WALL_MIN_M, fwd[i]))
    out, got = [], []
    for i in order:
        if fwd[i] >= RAY_MAX_M or any(float(dnp.matmul(d[i], d[j])) > dmath.cos(math.radians(FACADE_SEP_DEG)) for j in got):
            continue
        got.append(i)
        q = p + d[i] * fwd[i]
        out.append((q, _snap_normal(ring, q, d[i])))
        if len(out) >= FACADES_MAX:
            break
    return out


def _macro_snap(ring, q, n):
    """The normal n (a ray's, out of the building at q) turned to the wall's general line (wall_normal) where that is within
    MACRO_SNAP_COS of it, else n."""
    m = wall_normal(ring, q)
    return n if m is None or float(dnp.matmul(m, n)) < MACRO_SNAP_COS else m


def _radial(ring, q, n):
    """(point on the facade, outward unit normal) of the radial stand for the facade (q, n) of a building ring where it is at a pier's tip
    or a corner of the wall (radial_centre): the nose NOSE_GAP_M off the facade along n, the axis from there to the arc's centre, the
    facade point where that line leaves the ring. None otherwise."""
    nose = q + n * NOSE_GAP_M
    c = radial_centre(ring, q, nose)
    if c is None:
        return None
    v = nose - c
    r = float(dnp.hypot(*v))
    if r < 1.0:
        return None
    m = v / r
    t = float(_exit_out(ring, c[None], m[None])[0])
    return None if t >= RAY_MAX_M else (c + m * t, m)


def _terminal(obstacles, x, y, near=None):
    """(ring, [(facade point, outward normal) ...], on its centreline) of the terminal of a gate at (x, y): the nearest large
    building within NEAR_M (the one it stands in first), None without; the facades by preference; True where the gate is in
    the footprint or within CENTRE_M of it (a pier's gate mapped on its centreline: both sides are tried).
    A gate outside, within CENTRE_M: the far wall's facade (a thin pier) comes after the near one's."""
    near = NEAR_M if near is None else near
    p = np.array([x, y])
    best = None
    for ring, (x0, y0, x1, y1) in obstacles:
        if _area(ring) < TERMINAL_MIN_M2 or x0 - near > x or x1 + near < x or y0 - near > y or y1 + near < y:
            continue
        inside = bool(_pip(ring, p[None])[0])
        d = 0.0 if inside else _nearest_on(ring, p)[0]
        if d <= near and (best is None or d < best[0] - 1e-6):
            best = (d, ring, inside)
    if best is None:
        return None
    ring = best[1]
    centre = best[0] <= CENTRE_M                  # (in the footprint or against it: maybe on a pier's centreline)
    if best[2]:
        out = [(q, _macro_snap(ring, q, n)) for q, n in _inside_facades(ring, p)] + [_facade(ring, p)]
    else:
        _d, q, j = _nearest_on(ring, p)
        e = np.roll(ring, -1, axis=0)[j] - ring[j]
        ln = max(float(dnp.hypot(*e)), 1e-9)
        n = np.array([-e[1], e[0]]) / ln
        if float(dnp.matmul(n, p - q)) < 0.0 or (float(dnp.matmul(n, p - q)) == 0.0 and bool(_pip(ring, (q + n * 2.0)[None])[0])):
            n = -n                                              # (toward the gate's side: out of the building)
        near, smooth = (q, n), _facade(ring, p)
        out = [near, smooth] if ln >= EDGE_MIN_M else [smooth, near]     # (the nearest edge's own normal first)
        macro = wall_normal(ring, q)
        if macro is not None:                                   # (the wall's general line before a step's edge)
            out.insert(0, (q, macro))
        if centre:                                              # (the far side of a pier: its wall's normal the other way)
            for q2, n2 in _inside_facades(ring, q - n * 1.0):
                if float(n[0] * n2[0] + n[1] * n2[1]) <= -0.7 and float((q2 - p)[0] * n2[0] + (q2 - p)[1] * n2[1]) <= SIDE_SLACK_M + CENTRE_M:
                    out.append((q2, n2))
    radial = [r + (True,) for r in (_radial(ring, q, n) for q, n in out) if r is not None]     # (at a tip or a corner: radial, first)
    return ring, radial + out, centre


def _bridge(small, gx, gy, out):
    """The jet bridge of a gate: (tip distance along `out` from the gate, lateral centre, half width, indexes of the
    obstacles that are its bridges) of the small building touching it that reaches farthest out (at least BRIDGE_MIN_M;
    every such one is no obstacle to the gate's aircraft), None without."""
    p = np.array([gx, gy])
    tan = np.array([-out[1], out[0]])
    best, ids = None, []
    for ring, _a, oi in small:
        if dnp.hypot(*(ring - p).T).min() > ATTACH_M:
            continue
        d = ring - p
        tip, lat = dnp.matmul(d, out), dnp.matmul(d, tan)
        if tip.max() >= BRIDGE_MIN_M:
            ids.append(oi)
            if best is None or tip.max() > best[0]:
                best = (float(min(tip.max(), BRIDGE_CAP_M)), float((lat.min() + lat.max()) / 2.0),
                        float((lat.max() - lat.min()) / 2.0))
    return None if best is None else best + (tuple(ids),)


def _kinds_for(tier, gid):
    """The types to try at a gate, first choice first, by its aerodrome's tier and the hash (): none for military and
    the like. A stand too tight for the first choice takes a smaller one."""
    h = _crc(gid + ":type")
    if tier == "intl":
        return ("wide", "narrow", "turboprop") if h % 4 == 0 else ("narrow", "turboprop")
    if tier in ("regional", None):
        return ("turboprop", "narrow") if h % 5 < 3 else ("narrow", "turboprop")
    if tier == "light":
        return ("light",)
    return ()


def _lane_nose(sc, x, y):
    """The unit nose direction of a remote stand at (x, y) lined up with the nearest taxiway or taxilane line within
    REMOTE_LANE_M, None without one: across the line, on the stand's side of it (the tail toward the line: the aircraft
    is pushed back onto it); along it, the nose toward its end, where the line ends within REMOTE_END_M of the stand
    (a stand's own lead-in)."""
    p = np.array([x, y])
    best = None
    for xy in sc.lanes:
        d, q, j, t = _nearest_seg(xy[:-1], xy[1:], p)
        if d <= REMOTE_LANE_M and (best is None or d < best[0] - 1e-6):
            best = (d, q, j, t, xy)
    if best is None:
        return None
    d, q, j, t, xy = best
    e = xy[j + 1] - xy[j]
    u = e / max(float(dnp.hypot(*e)), 1e-9)
    v = p - q
    if 1e-6 < d <= REMOTE_END_M:
        for end, out in ((j == 0 and t <= 1e-6, -u), (j == len(xy) - 2 and t >= 1.0 - 1e-6, u)):
            if end and float(dnp.matmul(v, out)) >= 0.5 * d:            # (beyond the line's end)
                return out
    n = np.array([-u[1], u[0]])
    return -n if float(dnp.matmul(n, v)) < 0.0 else n


_Link = collections.namedtuple("_Link", "end dir normal half cut cabs")     # a gate's jet bridge, in the plane: the outer end, the
                                            # unit direction out of the terminal, the wall's outward normal at its root, its half
                                            # width, the gate's own bridges' obstacle rings less their last BRIDGE_CAB_M ({index:
                                            # (ring, bbox) or None}), the indexes of the cabs (no obstacle at all)


def _plane(sc, frame, xy):
    """Points (k, 2) in the metres of the ENU `frame` -> the scene's plane."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    return np.stack([(np.asarray(lo, np.float64) - sc.lon0) * sc.kx, (np.asarray(la, np.float64) - sc.lat0) * aeroways.M_PER_DEG], 1)


def _cut(ring, end, d):
    """(ring, bbox) of the polygon `ring` of a jet bridge less its last BRIDGE_CAB_M, measured back from `end` (the bridge's
    outer end) against `d` (out of the terminal); None where nothing worth testing is left."""
    s = (ring[:, 0] - end[0]) * d[0] + (ring[:, 1] - end[1]) * d[1] + BRIDGE_CAB_M      # > 0 in the part cut away
    out = []
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        if s[i] <= 0.0:
            out.append(ring[i])
        if (s[i] < 0.0 < s[j]) or (s[j] < 0.0 < s[i]):
            out.append(ring[i] + (ring[j] - ring[i]) * (s[i] / (s[i] - s[j])))
    if len(out) < 3:
        return None
    pts = np.array(out, np.float64)
    if _area(pts) < OBSTACLE_MIN_M2:
        return None
    return pts, (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))


def _ring_gap(a, b):
    """The smallest distance between two rings' outlines."""
    return min(min(_nearest_on(b, p)[0] for p in a), min(_nearest_on(a, p)[0] for p in b))


def _links(sc, feats, bld, obstacles):
    """({gate id: _Link}, {indexes of the obstacles that are jet bridges}) of a cell's gates, from airportroles' reading of the
    buildings and parts: the gate's nearest bridge (the end, its direction out of the terminal), the normal of the terminal's
    or pier's wall at the bridge's root (the bridge's own direction where no wall is near), and the bridges that are no
    obstacle to the gate's aircraft but for their last BRIDGE_CAB_M (the cab): the gate's own bridge and its tails (a bridge mapped
    in two pieces)."""
    flat = _flat(bld)
    blds = [b for b in flat if b.layer != "building_parts"]
    parts = [b for b in flat if b.layer == "building_parts"]
    if not obstacles or not sc.gates or not flat:
        return {}, set()
    frame = geodesy.EnuFrame(sc.lat0, sc.lon0, 0.0)
    res = ar.classify(blds, parts, [feats], frame, 0.0, 0.0)
    if not res.bridges:
        return {}, set()
    rings = {}

    def ring_of(uid):
        if uid not in rings:
            rings[uid] = _plane(sc, frame, res.units[uid].ring)
        return rings[uid]

    def obstacle_of(ring):
        lo, hi, area = ring.min(0), ring.max(0), _area(ring)
        for oi, (r, (x0, y0, x1, y1)) in enumerate(obstacles):
            if abs(x0 - lo[0]) <= 0.6 and abs(y0 - lo[1]) <= 0.6 and abs(x1 - hi[0]) <= 0.6 and abs(y1 - hi[1]) <= 0.6 \
                    and abs(_area(r) - area) <= 0.05 * area + 1.0:
                return oi
        return None

    jb = sorted(uid for uid, role in res.items() if role == ar.JET_BRIDGE)
    obs_of = {uid: obstacle_of(ring_of(uid)) for uid in jb}
    bobs = {oi for oi in obs_of.values() if oi is not None}
    terms = sorted(uid for uid, role in res.items() if role in (ar.TERMINAL, ar.PIER))
    ours = {g[0] for g in sc.gates}
    links = {}
    for gid, info in sorted(res.gates.items()):
        if info["bridge"] is None or gid not in ours or info["bridge"] not in res.bridges:
            continue
        own = {info["bridge"]} | {uid for uid, bids in res.tails.items() if info["bridge"] in bids}     # (its tails: a neighbour's is an obstacle all along)
        def end_of(bid):
            _d, r, o, _v = res.bridges[bid]
            r_p, o_p = _plane(sc, frame, [r, o])
            v = o_p - r_p
            return o_p, v / max(float(dnp.hypot(*v)), 1e-9)

        root_p = _plane(sc, frame, [res.bridges[info["bridge"]][1]])[0]
        outer_p, d = end_of(info["bridge"])
        n, best = d, None
        for uid in terms:
            r = ring_of(uid)
            key = (round(_nearest_on(r, root_p)[0], 1), _area(r), uid)
            if best is None or key < best[0]:
                best = (key, r)
        if best is not None and best[0][0] <= 2.0 * ar.ROOT_M:
            n2 = _facade(best[1], root_p)[1]
            if float(n2[0] * d[0] + n2[1] * d[1]) > 0.0:
                n = n2
        ends = {bid: end_of(bid) for bid in own if bid in res.bridges}
        cut = {}
        for uid in sorted(own):
            oi = obs_of.get(uid)
            if oi is None:
                continue
            e, de = ends[uid] if uid in ends else min(
                ends.values(), key=lambda t: float(dnp.hypot(*(ring_of(uid) - t[0]).T).min()))     # (a tail: its bridge's end)
            cut[oi] = _cut(obstacles[oi][0], e, de)
        mine = set(cut)
        cabs = set()
        for oi, (ring, (x0, y0, x1, y1)) in enumerate(obstacles):       # (a bridge's cab, a small piece at its end: no obstacle)
            if oi in mine or oi in bobs or _area(ring) > CAB_MAX_M2:
                continue
            if any(x0 - CAB_NEAR_M <= bx1 and x1 + CAB_NEAR_M >= bx0 and y0 - CAB_NEAR_M <= by1 and y1 + CAB_NEAR_M >= by0
                   and _ring_gap(ring, obstacles[o][0]) <= CAB_NEAR_M for o in mine for bx0, by0, bx1, by1 in (obstacles[o][1],)):
                cabs.add(oi)
        links[gid] = _Link(outer_p, d, n, res.units[info["bridge"]].width / 2.0, cut, tuple(sorted(cabs)))
    return links, bobs


def _bridge_pose(world, kind, link, **why_kw):
    """(None, (x, y, nx, ny)) of a gate's aircraft of `kind` at its mapped jet bridge, else (why, None): the pose that puts its
    forward left door (DOOR_K of the length back from the nose, half the fuselage's width out) nearest the bridge's outer end,
    of those where the aircraft's plan (fuselage, wings, tailplane: World.why) keeps SKIN_GAP_M off every building and bridge but the
    gate's own bridge's cab (its last BRIDGE_CAB_M: World.why's repl) and meets nothing else, and the door is within a cab's reach
    (BRIDGE_CAB_M + SKIN_GAP_M) of the bridge's end face (the aircraft of a kind that cannot meet the bridge is the next kind's). The
    search starts from the ideal: the axis the wall's normal at the bridge's root, nose-in (the bridge more than BRIDGE_AXIS_DEG off
    it: along the bridge, the nose still toward the building, so a round satellite's is radial), the door at the end; it slides the
    door along the axis and across the bridge (a grid of SEARCH_STEP_M, tried nearest the end first, then a fine one of REFINE_STEP_M round the first pose
    that clears) and turns the axis (BRIDGE_YAWS_DEG). A real aircraft's forward door is its left: the bridge is on the left,
    the fuselage right of it (nose toward the building); there is no mirror."""
    length, fus = SPECS[kind][0], SPECS[kind][3]
    d, n = link.dir, link.normal
    u0 = -n
    if BRIDGE_TURN_COS < float(d[0] * n[0] + d[1] * n[1]) < BRIDGE_AXIS_COS:
        u0 = -d
    left0 = np.array([-u0[1], u0[0]])
    perp = np.array([-d[1], d[0]])
    fwd = length / 2.0 - DOOR_K * length
    reach = BRIDGE_CAB_M + SKIN_GAP_M
    lmax = link.half + fus + SKIN_GAP_M                         # (the door off the bridge's axis by at most the bridge's half width, the fuselage's width and a gap)
    yaws = [(dmath.cos(math.radians(y)), dmath.sin(math.radians(y)), y) for y in BRIDGE_YAWS_DEG]

    def door(a, l):
        return link.end + u0 * a + left0 * l                    # (a along the axis from the end, l across it)

    def face(t):                                                # (the door's distance to the bridge's end face)
        s = float(np.clip(float(dnp.dot(t - link.end, perp)), -link.half, link.half))
        return float(dnp.hypot(*(t - (link.end + perp * s))))

    def pose(a, l, yc, ys):
        ux, uy = u0[0] * yc - u0[1] * ys, u0[0] * ys + u0[1] * yc
        t = door(a, l)
        return (float(t[0] - ux * fwd + uy * fus / 2.0), float(t[1] - uy * fwd - ux * fus / 2.0), float(ux), float(uy))

    def ok(a, l, yc, ys):
        if face(door(a, l)) > reach:
            return False
        x, y, nx, ny = pose(a, l, yc, ys)
        return world.pitch_ok(kind, x, y, nx, ny) and world.why(kind, x, y, nx, ny, skin=SKIN_GAP_M, **why_kw) is None

    along = np.arange(-reach, BRIDGE_CAB_M + 1e-9, SEARCH_STEP_M)
    across = np.arange(-lmax, 1e-9, SEARCH_STEP_M)              # (the door on the bridge's right of its axis or on it: the bridge is on the aircraft's left)
    fine = np.arange(-SEARCH_STEP_M, SEARCH_STEP_M + 1e-9, REFINE_STEP_M)
    best = None                                                 # (door distance + the yaw's cost, a, l, yaw's cos, sin)
    for yc, ys, y in yaws:                                      # (each axis turn: its nearest clearing pose, the coarse grid then a fine one)
        for _dist, a, l in sorted((float(dnp.hypot(a, l)), a, l) for a in along for l in across):
            if best is not None and _dist - SEARCH_STEP_M + YAW_COST_M * abs(y) >= best[0]:   # (the fine grid gains less than a step)
                break
            if not ok(a, l, yc, ys):
                continue
            got = (_dist, a, l)
            for dd, a2, l2 in sorted((float(dnp.hypot(a + da, l + dl)), a + da, l + dl) for da in fine for dl in fine):
                if dd >= got[0] - 1e-9:
                    break
                if ok(a2, l2, yc, ys):
                    got = (dd, a2, l2)
                    break
            if best is None or got[0] + YAW_COST_M * abs(y) < best[0]:
                best = (got[0] + YAW_COST_M * abs(y), got[1], got[2], yc, ys)
            break
    if best is not None:
        return None, pose(best[1], best[2], best[3], best[4])
    return "building", None


def _gate_tries(kind, gx, gy, bearing, term, small):
    """[(centre x, y, nose direction x, y, ignored obstacles, reach)] of a gate's aircraft of `kind` at its terminal's facades,
    the one by the gate's tagged heading first (where it points at the facade), then the facade's normal (reach: the
    gate's distance to that facade along its normal; None for a tagged heading or a jet bridge)."""
    length = SPECS[kind][0]
    g = np.array([gx, gy])
    d = None
    if bearing is not None:
        d = np.array([dmath.sin(math.radians(bearing)), dmath.cos(math.radians(bearing))])
    tagged, tries = [], []
    for q, n, *radial in term[1]:
        base = float(dnp.matmul(q - g, n)) + NOSE_GAP_M
        br = _bridge(small, gx, gy, n)
        if br is not None:                                  # (the bridge fixes the door and the side)
            lat = br[1] + br[2] + SPECS[kind][3] / 2.0 + BRIDGE_SIDE_M
            c = g + n * (br[0] - BACK_M[kind] + length / 2.0) + np.array([-n[1], n[0]]) * lat
            tries.append((float(c[0]), float(c[1]), -float(n[0]), -float(n[1]), br[3], None))
            continue
        c = (q + n * (NOSE_GAP_M + length / 2.0)) if radial else g + n * (base + length / 2.0)     # (radial: the axis through the arc's centre)
        tries.append((float(c[0]), float(c[1]), -float(n[0]), -float(n[1]), (), base - NOSE_GAP_M) + ((True,) if radial else ()))
        if d is not None and base > 0.0:
            dn = float(dnp.matmul(d, n))
            if dn <= -HEADING_COS:                          # (the tag's nose line through the gate, NOSE_GAP_M off the wall)
                c = g - d * (base / -dn + length / 2.0)
                tagged.append((float(c[0]), float(c[1]), float(d[0]), float(d[1]), (), None))
    return tagged + tries


def _first_clear(world, kind, tries, extra=(), sides=False, at=(0.0, 0.0), repl=None, back=None, strip=0.0, wall=None):
    """(None, (x, y, nx, ny)) of the first of the tries (centre, nose direction, ignored obstacles, reach) that clears, one
    that meets a building tried again pulled back along its axis (not where a jet bridge fixes the door), else (why, None).
    A try whose span (PITCH_GAP_M more) does not fit beside the stands kept is "pitch", before anything else is tested.
    extra: obstacles that are none to any of the tries (the narrow bridges and fingers by the gate); repl: rings tested in
    place of some obstacles (World.why); strip, wall: the gear's reach off the paving and the terminal's ring (World.paved); back: the most a try is pulled back (default PULL_BACK_MAX_M, and none where it
    ignores obstacles).
    sides (a gate on a pier's or a terminal's centreline): the tries with a reach of at most SIDE_REACH_M, within
    SIDE_SLACK_M of the nearest facade's, and across or along its normal are the sides, tried after a tagged heading and
    before any other; of the sides that clear, the one whose aircraft is furthest from the stand kept nearest the gate
    `at` (so a pair of gates alternates sides), the first without a stand kept."""
    why = "no_terminal"
    clear = []                                              # ((x, y, nx, ny), is a side)
    faced = [t for t in tries if t[5] is not None and len(t) == 6]
    ref = min(faced, key=lambda t: t[5]) if faced else None     # (the nearest facade's normal: the sides are across or along it)
    near = ref[5] if ref else 0.0

    def is_side(t):
        return bool(sides and t[5] is not None and t[5] <= min(SIDE_REACH_M, near + SIDE_SLACK_M)
                    and abs(t[2] * ref[2] + t[3] * ref[3]) >= SIDE_COS)

    if sides:                                               # (a tagged heading first, then the radial ones, the sides, then the rest)
        tries = sorted(tries, key=lambda t: 0 if t[5] is None else (1 if len(t) > 6 else 2 if is_side(t) else 3))
    seen = set()
    for try_ in tries:
        cx, cy, nx, ny, ign, reach = try_[:6]
        key = (round(cx, 2), round(cy, 2), round(nx, 3), round(ny, 3))
        if key in seen:                                     # (rays that snapped to one wall's normal)
            continue
        seen.add(key)
        side = is_side(try_) and len(try_) == 6
        if not world.pitch_ok(kind, cx, cy, nx, ny):
            why = "pitch"
            continue
        for k in range(int(PULL_BACK_MAX_M / PULL_BACK_STEP_M + 1e-9) + 1):
            if k and (ign if back is None else k * PULL_BACK_STEP_M > back + 1e-9):
                break
            x, y = cx - nx * k * PULL_BACK_STEP_M, cy - ny * k * PULL_BACK_STEP_M
            why = world.why(kind, x, y, nx, ny, ignore=tuple(ign) + tuple(extra), repl=repl, strip=strip, wall=wall)
            if why != "building":
                break
        if why is None:
            if not sides or not side:
                if not clear:
                    return None, (x, y, nx, ny)
                break
            clear.append(((x, y, nx, ny), True))
    if not clear:
        return why, None
    if len(clear) > 1 and world.placed:
        _k, x2, y2, _a, _b = min(world.placed, key=lambda p: (math.hypot(p[1] - at[0], p[2] - at[1]), p[1], p[2]))
        clear.sort(key=lambda c: -math.hypot(c[0][0] - x2, c[0][1] - y2))     # (stable: ties keep the try order)
    return None, clear[0][0]


def _gate_craft(sc, world, small, obstacles, links=None, empty=False):
    """[(kind, x, y, nx, ny, mode, gate id)] of the gates' aircraft: those the occupancy hash fills (empty False), or
    (empty True: after every aircraft is placed, so none of them moves) the stands of the gates it leaves empty, in the
    same places, each kept in the world (STATS is not counted)."""
    links = links or {}
    stats = collections.Counter() if empty else STATS
    out = []
    gates = sorted(sc.gates, key=lambda g: (round(g[1], 1), round(g[2], 1), g[0]))   # (along a row: alternates stay)
    for gid, gx, gy, _b, _p in gates:                   # (a neighbour's bridge by a gate: the wings pass under it)
        world.raised.update(world.narrow_near(gx, gy, NARROW_WING_M))
    for gid, gx, gy, bearing, is_pos in gates:
        if empty != (_crc(gid) % 100 >= OCCUPIED_PCT):
            if not empty:
                stats["gates"] += 1
                stats["not_occupied"] += 1
            continue
        stats["gates"] += 1
        tier = _aerodrome(sc, gx, gy)
        if tier in ("military", "none"):
            stats["tier_none"] += 1
            continue
        kinds = _kinds_for(tier, gid)
        if not kinds:
            stats["tier_none"] += 1
            continue
        link = links.get(gid)
        term = _terminal(obstacles, gx, gy) if link is None else None
        lane = _lane_nose(sc, gx, gy) if term is None and bearing is None and link is None else None
        remote = None
        if term is None and bearing is None and lane is None and link is None and not (
                is_pos and _apron_nose(sc, gx, gy) is not None):
            remote = _terminal(obstacles, gx, gy, REMOTE_M)
        why = "no_terminal"
        extra = tuple(oi for oi in world.narrow_near(gx, gy) if oi not in world.bridge_obs)      # (the mapped bridges are obstacles)
        repl = None
        if link is not None:
            extra += link.cabs                          # (the gate's own bridges are obstacles but for their cab)
            repl = link.cut
            world.raised.update(link.cut)               # ... to the fuselage only
        wall = None if term is None and link is None else (term or _terminal(obstacles, gx, gy) or (None,))[0]   # (the unmapped strip is by its wall)
        for kind in kinds:
            length = SPECS[kind][0]
            if link is not None:                        # (a mapped jet bridge: the pose that puts the door nearest its end)
                why, got = _bridge_pose(world, kind, link, ignore=extra, repl=repl,
                                        strip=PAVING_GAP_M, wall=wall)
                if why is None:
                    world.add(kind, *got)
                    out.append((kind,) + tuple(float(v) for v in got) + ("gate", gid))
                    stats["placed_" + kind] += 1
                    break
                continue
            elif term is not None:
                tries = _gate_tries(kind, gx, gy, bearing, term, small)
            else:                                       # (a stand off the terminals: the nose wheel stop on the gate's point)
                nose = None
                if bearing is not None:
                    nose = np.array([dmath.sin(math.radians(bearing)), dmath.cos(math.radians(bearing))])
                elif lane is not None:
                    nose = lane
                elif is_pos:
                    nose = _apron_nose(sc, gx, gy)
                elif remote is not None:                # (the nose toward the nearest facade)
                    v = remote[1][-1][0] - (gx, gy)
                    nose = v / max(float(dnp.hypot(*v)), 1e-6)
                tries = [] if nose is None else [(gx - float(nose[0]) * 0.4 * length, gy - float(nose[1]) * 0.4 * length,
                                                  float(nose[0]), float(nose[1]), (), None)]
            if not tries:
                why = "no_terminal"
                break
            why, got = _first_clear(world, kind, tries, extra, sides=term is not None and term[2], at=(gx, gy), repl=repl,
                                    back=None if link is None else 0.0,
                                    strip=PAVING_GAP_M if term is not None or link is not None else 0.0, wall=wall)
            if why is None:
                world.add(kind, *got)
                out.append((kind,) + tuple(float(v) for v in got) + ("gate" if term is not None or link is not None else "remote", gid))
                stats["placed_" + kind] += 1
                break
        if why is not None:
            stats["no_" + why] += 1
    return out


def _apron_nose(sc, x, y):
    """The unit direction toward the nearest edge of the apron holding (x, y), None outside any."""
    p = np.array([[x, y]])
    for _i, ring, _ga in sc.aprons:
        if _pip(ring, p)[0]:
            _d, q, _j = _nearest_on(ring, p[0])
            v = q - p[0]
            n = float(dnp.hypot(*v))
            return (v / n) if n > 1e-6 else None
    return None


def _ga_craft(sc, world):
    out = []
    for aid, ring, tagged in sorted(sc.aprons, key=lambda a: a[0]):
        c = ring.mean(0)
        tier = _aerodrome(sc, float(c[0]), float(c[1]))
        if tier in ("military", "none") or _area(ring) < APRON_MIN_M2:
            continue
        if not (tagged or (tier != "intl" and not (tier is None and sc.intl))):
            continue
        closed = np.r_[ring, ring[:1]] if not np.allclose(ring[0], ring[-1]) else ring
        seg = np.diff(closed, axis=0)
        ln = dnp.hypot(seg[:, 0], seg[:, 1])
        cum = np.r_[0.0, np.cumsum(ln)]
        # the ring's sense: the inward normal of an edge is its left when it runs counter-clockwise
        sign = 1.0 if float(dnp.dot(closed[:-1, 0], closed[1:, 1]) - dnp.dot(closed[1:, 0], closed[:-1, 1])) > 0 else -1.0
        slots = []
        for k in range(int(cum[-1] // GA_SPACING_M)):
            h = _crc(f"{aid}:{k}")
            if h % 100 < GA_PCT:
                slots.append((h, k))
        keep = sorted(slots)[:GA_MAX]
        n_ok = 0
        for _h, k in sorted(keep, key=lambda t: t[1]):
            s = (k + 0.5) * GA_SPACING_M
            j = min(int(np.searchsorted(cum, s, side="right")) - 1, len(seg) - 1)
            if ln[j] < 1e-6:
                continue
            t = seg[j] / ln[j]
            edge = closed[j] + t * (s - cum[j])
            inward = sign * np.array([-t[1], t[0]])
            length = SPECS["light"][0]
            cx, cy = edge + inward * (length / 2.0 + GA_INSET_M)
            nx, ny = -inward[0], -inward[1]
            if n_ok < GA_MAX and world.clear("light", cx, cy, nx, ny, inside=ring):
                world.add("light", cx, cy, nx, ny)
                out.append(("light", float(cx), float(cy), float(nx), float(ny), "ga"))
                n_ok += 1
    return out


def stands(feats, buildings=None):
    """[Craft] of a cell's infrastructure Features (see the module docstring), kept on the Features (by the buildings
    it was asked with: a buildings Features, or a list of them, the cell's and its neighbours'); [] for Features
    without airport gates or aprons."""
    if feats is None or len(feats) == 0:
        return []
    buildings = _flat(buildings)
    got = feats.__dict__.get(_KEY)
    if got is not None and _same(got[0], buildings):
        return got[1]
    return _compute(feats, buildings)[1]


def empty_stands(feats, buildings=None):
    """[Craft] of the stands of a cell's gates that hold no aircraft (the occupancy hash leaves them empty), each where an
    aircraft would stand (stands(): the paving and building clearance, narrow-body when a wide one does not fit, none of
    a gate that clips a building), clear of the aircraft stands() places and of each other. [] for Features without gates."""
    if feats is None or len(feats) == 0:
        return []
    buildings = _flat(buildings)
    got = feats.__dict__.get(_KEY)
    if got is None or not _same(got[0], buildings):
        got = _compute(feats, buildings)
    return got[2]


def _same(a, b):
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


def _compute(feats, buildings):
    """(buildings, [Craft] aircraft, [Craft] empty stands) of stands() / empty_stands(), kept on the Features."""
    sc = feats.__dict__.get(_KEY + "_scene", False)
    if sc is False:
        sc = feats.__dict__[_KEY + "_scene"] = _scene(feats)
    out, spare = [], []
    if sc is not None:
        obstacles, small = _buildings(sc, buildings)
        world = _World(sc, obstacles)
        links, world.bridge_obs = _links(sc, feats, buildings, obstacles)
        if sc.aprons:                                           # (a cell that maps aprons: the gear stays on the paving)
            pads = [sc.xy(np.stack([lon, lat], 1)) for lat, lon in aeroways.pads([feats], runways=False)]
            world.paving = [(r, (float(r[:, 0].min()), float(r[:, 1].min()), float(r[:, 0].max()), float(r[:, 1].max())))
                            for r in pads if len(r) >= 3]
        placed = _gate_craft(sc, world, small, obstacles, links) + _ga_craft(sc, world)
        placed = [placed[j] for j in drop_overlaps(placed)]     # (the final pass: none overlaps another)
        for k, x, y, nx, ny, mode, *gid in placed:
            la, lo = sc.latlon(x, y)
            out.append(Craft(k, la, lo, nx, ny, mode, livery_for(_airport_seed(sc, x, y), la, lo, mode),
                             sc.refs.get(gid[0], "") if gid else ""))
        for k, x, y, nx, ny, mode, gid in _gate_craft(sc, world, small, obstacles, links, empty=True):
            la, lo = sc.latlon(x, y)
            spare.append(Craft(k, la, lo, nx, ny, mode, 0, sc.refs.get(gid, "")))
    got = feats.__dict__[_KEY] = (buildings, out, spare)
    return got


def has_stands(feats):
    """Could the Features hold any (gates or aprons of an airport)? A test of the class column, kept."""
    if feats is None or len(feats) == 0:
        return False
    got = feats.__dict__.get(_KEY + "_scene", False)
    if got is False:
        got = feats.__dict__[_KEY + "_scene"] = _scene(feats)
    return got is not None


# ------------------------------------------------------------------------------------------------- the tile's rows
def _yaw(east, north):
    return (math.degrees(dmath.atan2(north, east)) - 90.0) % 360.0


def _near_cells(key, feats):
    """The neighbouring cells within NEIGHBOUR_M of the cell's gates and aprons (their buildings may hold a gate's terminal)."""
    sc = feats.__dict__.get(_KEY + "_scene")
    if sc is None or not hasattr(key, "bounds") or not hasattr(key, "neighbours"):
        return []
    pts = [np.array([[g[1], g[2]]]) for g in sc.gates] + [ring for _i, ring, _ga in sc.aprons]
    if not pts:
        return []
    xy = np.concatenate(pts)
    lon = sc.lon0 + xy[:, 0] / sc.kx
    lat = sc.lat0 + xy[:, 1] / aeroways.M_PER_DEG
    dlat = NEIGHBOUR_M / aeroways.M_PER_DEG
    dlon = NEIGHBOUR_M / (aeroways.M_PER_DEG * dmath.cos(math.radians(sc.lat0)))
    out = []
    for nk in key.neighbours():
        w, s, e, n = nk.bounds()
        if ((lon >= w - dlon) & (lon <= e + dlon) & (lat >= s - dlat) & (lat <= n + dlat)).any():
            out.append(nk)
    return out


def _overlap(a, b, margin=AIRCRAFT_CLEAR_M):
    """Do the aircraft a, b ((kind, x, y, nx, ny) in a plane) come within `margin` of each other (footprints: the oriented
    rectangles of their kinds' dimensions)?"""
    if math.hypot(a[1] - b[1], a[2] - b[2]) > _radius(a[0]) + _radius(b[0]) + margin:
        return False
    pa_, pb = _world(_local_points(a[0]), *a[1:5]), _world(_local_points(b[0]), *b[1:5])
    return bool(_in_rects(a[0], *a[1:5], pb, margin).any() or _in_rects(b[0], *b[1:5], pa_, margin).any())


def drop_overlaps(items):
    """The final pass: the indices (ascending) of the aircraft kept of items [(kind, x, y, nx, ny, ...)] (a plane in metres,
    nose direction (nx, ny)): the longer kinds first, then by position, each dropped where its footprint (its kind's
    dimensions, AIRCRAFT_CLEAR_M margin) overlaps one kept already. Deterministic whatever the order."""
    order = sorted(range(len(items)), key=lambda j: (-SPECS[items[j][0]][0], round(float(items[j][2]), 3),
                                                     round(float(items[j][1]), 3), items[j][0]))
    kept, placed = [], []
    for j in order:
        a = tuple(items[j][:5])
        if any(_overlap(a, b) for b in placed):
            continue
        kept.append(j)
        placed.append(a)
    return sorted(kept)


def _items_plane(items, lat, lon):
    """The items (Craft; lat, lon: arrays of their positions) as (kind, x, y, nx, ny) in a local plane in metres, its origin
    the first by position."""
    kx = aeroways.M_PER_DEG * dnp.cos(np.radians(lat))
    j0 = min(range(len(items)), key=lambda j: (round(float(lat[j]), 6), round(float(lon[j]), 6), items[j].kind))
    return [(c.kind, float(lon[j] - lon[j0]) * float(kx[j]), float(lat[j] - lat[j0]) * aeroways.M_PER_DEG, c.ne, c.nn)
            for j, c in enumerate(items)]


def _dedupe(items, lat, lon):
    """Indices of the items (Craft) kept: drop_overlaps in a local plane. An aircraft drops where it overlaps a kept one of
    another cell (the cells place their own gates' aircraft without each other's)."""
    if not items:
        return []
    kept = drop_overlaps(_items_plane(items, lat, lon))
    STATS["cross_cell"] += len(items) - len(kept)
    return kept


def building_layers_fn(md):
    """The buildings_fn of tile_rows / kept_stands for a MapData: a cell's buildings and building_parts Features (the
    parts hold the canopies and covered walkways an aircraft must keep off too)."""
    def fn(key):
        layers = md.cell(key, layers=("buildings", "building_parts"), kind="demand").layers
        return [layers.get("buildings"), layers.get("building_parts")]
    return fn


def _cells_of(frame, box):
    """The cells under a box (a gate's aircraft stands within ~70 m of it: padded 0.001 degrees), [] off the map."""
    import mapcells
    x0, y0, x1, y1 = (float(v) for v in box[:4])
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
    try:
        return mapcells.cells_for_points(la, lo, pad_lat=0.001, pad_lon=0.001, max_cells=64)
    except (ValueError, OverflowError):
        return []


def kept_stands(keys, infra_fn, buildings_fn):
    """[Craft] of the cells `keys` (stands() of each, with its neighbours' buildings), the cross-cell pass applied: what
    tile_rows places (a gate near a cell's edge: its neighbour's aircraft on it). buildings_fn(key) -> a buildings
    Features, or a list of Features (buildings and building_parts), or None."""
    items = []
    for k in keys:
        feats = infra_fn(k)
        if not has_stands(feats):
            continue
        blds = _flat([buildings_fn(k)] + [buildings_fn(nk) for nk in _near_cells(k, feats)])
        items.extend(stands(feats, blds))
    if not items:
        return []
    lat = np.array([c.lat for c in items], np.float64)
    lon = np.array([c.lon for c in items], np.float64)
    return [items[j] for j in _dedupe(items, lat, lon)]


def _lead_cell(feats, near, buildings_fn, key):
    """The stands of one cell (its Features `feats`, `key`) that get a lead-in, judged only from the cells round them: the
    cell's own and the neighbour cells' (`near`: [(key, Features)] of those near its gates and aprons, _near_cells) aircraft
    and empty stands, whatever else the caller reads. The aircraft are the cross-cell pass of kept_stands (drop_overlaps); an
    empty stand that overlaps an aircraft kept, or an empty one kept before it (the longer kinds first, then by position),
    is passed over. Only the cell's own stands are returned."""
    air, spare = [], []                                         # (Craft, is the cell's own)
    for i, (ck, cf) in enumerate([(key, feats)] + near):
        blds = _flat([buildings_fn(ck)] + [buildings_fn(nk) for nk in _near_cells(ck, cf)])
        air += [(c, i == 0) for c in stands(cf, blds)]
        spare += [(c, i == 0) for c in empty_stands(cf, blds)]
    items = [c for c, _own in air + spare]
    if not items:
        return []
    lat = np.array([c.lat for c in items], np.float64)
    lon = np.array([c.lon for c in items], np.float64)
    plane = _items_plane(items, lat, lon)
    na = len(air)
    kept = drop_overlaps(plane[:na]) if na else []
    placed = [plane[j] for j in kept]
    out = [air[j][0] for j in kept if air[j][1]]
    for j in sorted(range(na, len(items)), key=lambda j: (-SPECS[plane[j][0]][0], round(float(lat[j]), 7),
                                                           round(float(lon[j]), 7), plane[j][0])):
        if any(_overlap(plane[j], b) for b in placed):
            continue
        placed.append(plane[j])
        if spare[j - na][1]:
            out.append(items[j])
    return out


def lead_stands(keys, infra_fn, buildings_fn):
    """[Craft] of the stands of the cells `keys` that get a painted lead-in (taxiwaymarks) and a stand sign (airfieldsigns):
    every valid stand, empty or not: the aircraft kept_stands places (stands()) and the stands of the gates the occupancy hash
    leaves empty (empty_stands(): where an aircraft would stand, clear of the buildings and the paving, a gate that clips a
    building has none). Each cell's stands are judged from the cells round that cell's gates and aprons (_lead_cell), never
    from `keys`: every tile that reads a stand gets the same list, whichever other cells it reads. Sorted (position,
    kind). infra_fn(key) -> the cell's infrastructure Features (or None), buildings_fn(key) as kept_stands'."""
    out, seen = [], set()
    for k in keys:
        feats = infra_fn(k)
        if not has_stands(feats):
            continue
        near = [(nk, f) for nk in _near_cells(k, feats) for f in [infra_fn(nk)] if has_stands(f)]
        for c in _lead_cell(feats, near, buildings_fn, k):
            at = (c.kind, round(c.lat, 7), round(c.lon, 7))
            if at not in seen:                                  # (a stand twice over: keys listed twice)
                seen.add(at)
                out.append(c)
    return sorted(out, key=lambda c: (round(c.lat, 7), round(c.lon, 7), c.kind))


def tile_rows(frame, box, infra_fn, buildings_fn, elev, keys=None):
    """Forest rows [group, x, y, z, yaw, 1, H, rank] of the parked aircraft a tile owns (see the module docstring).
    keys: the box's cells (default: under it)."""
    if keys is None:
        keys = _cells_of(frame, box)
    items = kept_stands(keys, infra_fn, buildings_fn)
    if not items:
        return []
    lat = np.array([c.lat for c in items], np.float64)
    lon = np.array([c.lon for c in items], np.float64)
    e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    own = np.flatnonzero(np.asarray(geotiles.box_owns(box, e, n), bool))
    if not len(own):
        return []
    # (the nose direction in this frame: a point 10 m on, with the meridians' convergence)
    kx = aeroways.M_PER_DEG * dnp.cos(np.radians(lat[own]))
    ne = np.array([items[j].ne for j in own], np.float64)
    nn = np.array([items[j].nn for j in own], np.float64)
    la2, lo2 = lat[own] + nn * 10.0 / aeroways.M_PER_DEG, lon[own] + ne * 10.0 / kx
    e2, n2, _ = frame.geodetic_to_enu(la2, lo2, 0.0)
    h = np.asarray(elev(lat[own], lon[own]), np.float64)
    z = np.asarray(frame.geodetic_to_enu(lat[own], lon[own], h)[2], np.float64)
    rows = []
    for j, x, y, zz, xe, yn in zip(own.tolist(), e[own], n[own], z, np.asarray(e2, np.float64), np.asarray(n2, np.float64)):
        kind = items[j].kind
        rows.append([GROUPS[kind], round(float(x), 2), round(float(y), 2), round(float(zz), 2),
                     round(_yaw(float(xe - x), float(yn - y)), 1), 1.0, SPECS[kind][2], rank_for(items[j].livery)])
        if items[j].mode == "gate":
            rows += _nav_rows(kind, float(x), float(y), float(zz), float(xe - x), float(yn - y))
    return rows


def _nav_rows(kind, x, y, z, dx, dy):
    """Glow rows (airfieldlights' groups: they light at night) of the navigation lights of a gate's aircraft centred at (x, y)
    on the ground z, the nose toward (dx, dy): red at the left wing tip, green at the right, white at the tail cone. No red
    beacon (the anti-collision light is on only with the engines running: a parked aircraft's are off); remote stands and
    general-aviation rows have none."""
    r = math.hypot(dx, dy) or 1.0
    ux, uy = dx / r, dy / r
    length, span = SPECS[kind][0], SPECS[kind][1]
    tip_u, tip_h, tail_h = NAV[kind]
    out = []
    for colour, u, v, h in (("red", tip_u, span / 2.0, tip_h), ("green", tip_u, -span / 2.0, tip_h),
                            ("white", -length / 2.0 + NAV_TAIL_IN_M, 0.0, tail_h)):
        out.append([afl.GROUPS[colour], round(x + u * ux - v * uy, 2), round(y + u * uy + v * ux, 2), round(z + h, 2),
                    0.0, 1.0, afl.GROUP_H, 0.0])
    return out
