"""Painted taxiway markings: the yellow centreline along every paved taxiway and taxilane, and the runway holding
position marking where a taxiway meets a runway (the ground of an airfield read as bare asphalt: only the runways
carried paint, runwaymarks).

strokes(feats_list) -> [(lat0, lon0, lat1, lon1, width, kind, color)]: the Overture "infrastructure" Features (subtype
"airport") of the cells around a block, as the strokes runwaymarks.to_world gives a runway's (AirportGrader._marks):
  * "taxi_centreline" (yellow, CENTRE_W_M): along each way's mapped line, cut away within the half width plus
    RUNWAY_GAP_M of a runway centreline the way CROSSES; a way that ENDS on the runway's centreline (the mapped runway lines are laid
    on the OurAirports runways first: runwaysnap, with the ways' ends on them) runs on to it, finishing with a tangent arc of
    RUNWAY_LEAD_R_M into the centreline where it meets it at under RUNWAY_LEAD_RAD (_lead_on: lead-on and lead-off lines),
  * "hold_solid" / "hold_dashed" (yellow, HOLD_LINE_W_M): ICAO pattern A across the way's width where it enters a
    runway, four lines HOLD_LINE_W_M + HOLD_LINE_GAP_M apart, the two dashed nearer the runway (its first at HOLD_DIST_M
    from the runway's centreline, along the way) and the two solid ones on the taxiway side. A mapped
    aeroway=holding_position point on the way (class or tag) puts the marking there instead.
The widths are aeroways' (the width tag, else a default by class and aerodrome); a way or runway line is read as
aeroways reads it (airport subtype, class, unpaved surfaces skipped, kind-2 lines, JOIN_TOL_M). A feature is in the
Features of every cell it touches: those are read once (by id) and the strokes of a list of Features are memoized on its
first (bounded), so every tile of a block shares them. Pure: no ground, no frame.

Stand lead-ins (strokes / tile_chunks take `stands`, parkedaircraft's Craft list of the cells, parkedaircraft.lead_stands:
every valid stand, with or without an aircraft): for each gate or remote stand (not a general-aviation row) a yellow line (CENTRE_KIND,
CENTRE_W_M: the same paint) from a taxiway or taxilane to the nose, laid out by standpaths.build (its docstring is the spec): the lanes
first (the mapped centrelines, and a spine of our own behind a row of stands whose axes reach none), then for each stand a half lens off
the first lane its axis meets, one for each way along the lane an aircraft can arrive from (a tangent arc of the kind's radius, MIN_RADIUS_M
at the least, 8 m for a light aircraft, leaving the lane and arriving along the axis), and a straight run along the axis to the nose,
and a stop bar STOP_BAR_M across the axis at the nose. A stand whose axis meets no lane is joined to the nearest by one smooth curve,
and a stand with no lane within reach gets a straight stub along its axis from LEAD_STUB_M behind the tail to the nose: every stand has
a line reaching its nose.
A stand that overlaps another of the list is passed over, and no line is painted twice (every tile reads the same list
of stands, and paints the parts of the lines inside its own box: see tile_chunks). The mapped lines are cut only at
runways (not at an apron's edge). A mapped way that runs head on into a stand along its axis is the start of its lead-in (standpaths: the slit).
Stand marks (_lead_lines; the paint is black, yellow, white and red: airportgrade.MARK_LOOK):
  * every yellow lead-in, spine and bar has a black border BORDER_M wider each side, painted underneath (roadmesh layer "edge");
  * stop bars across the axis (STOP_BAR_M x STOP_BAR_W_M) for the stand's own kind at the nose and the next smaller kinds that
    fit (_ladder: each at its own nose, LADDER_K times the door setback), joined by a STOP_BAR_W_M centreline, each with a
    type label beside it (LABEL: generic codes, never an airline): black lettering on a LABEL_BOX_M yellow box;
  * the stand number (the gate's `ref`: A-Z, 0-9, at most NUM_MAX characters; the font texture, standmarks.text_quads, capitals
    NUM_TEXT tall) black on a yellow box (about 5 x 2 m for "B13"), past the stop bars on the axis, upright for the approaching pilot;
  * two wing-tip clearance lines (WING_LINE_OFF_M outside the wing tips, WING_LINE_LEN_M long, ending level with the stop bar;
    _wing_lines: clipped where they would run under a neighbour's aircraft, dropped near another stand's lead-in, the later of two
    lines that come within WING_CLEAR_M of each other too), with the black border as every yellow line on an apron has, the mapped
    centrelines' parts on one (_build) included;
  * the red equipment restraint line (RESTRAINT_OFF_M outside the aircraft's envelope, round the sides and behind the tail);
  * with a Site (the terminals' walls and the jet bridges' ends: site_of): the red hatched box under each mapped bridge's cab
    (BRIDGE_BOX_M) at a stand, and, along each building's frontage, ONE service road (_service_roads: a ring round a satellite, else an arc along
    the wall, cut only where a jet bridge's footprint or another building is in the way; two lanes ROAD_W_M wide, solid
    white edges, a dashed centre, ROAD_WALL_M from the wall, none where a nose is under ROAD_MIN_GAP_M from it; a row of white
    give-way triangles each side of every lead-in that crosses it; boxes for equipment, white outlines with a number, on its wall side).
Taxiway edges (_edge_lines): double yellow lines, two EDGE_W_M lines EDGE_GAP_M apart, along the outline of every taxiway pad
(aeroways.ways), only along the OUTER boundary of the paving (the union of the taxiway, apron and runway pads, each grown by PAVE_BUFFER_M: not
where one pad's edge lies in another's), a break under EDGE_JOIN_M run across, the pieces ending within it of each other one line, and none
under EDGE_MIN_M long; in a tile they are quads (EDGE_QUAD_M).
The filled marks (boxes, lettering, hatching, dashes, triangles, the edge lines) are convex polygons (polygons(): [(lat, lon, colour,
layer)]): in a tile ONE chunk of quads a colour and layer (roadmesh's "poly" chunks with "quads"); layers "edge" (black borders),
"gutter" (red), "mark" (yellow, white) and "glyph" (the lettering) are the road mesh's heights, so what lies over what does not rest
on the order of the mesh alone; and the paint writes no depth, so the materials' order (roadmesh.PAINT_ORDER) decides: black, red,
yellow and white, "text" (solid black: the taxiway names' boxes, over the centreline they stand on), then the lettering.
The lettering is a texture (tools/make_font_atlas.py: DejaVu Sans Bold, opacity only, standmarks.text_quads): a character is ONE quad
with UVs into the sheet, in the material of its colour (line_runway_font_black on the yellow stand boxes, _yellow on the names' black
boxes, _white on the equipment boxes); a polygon of polygons() is then (lat, lon, colour, layer, UVs (4, 2)) and a tile's chunk of
them carries the UVs ("uvq"). The sheet has a solid cell too: a ring or rule in the lettering's material takes its UVs from it.

Taxiway names (_name_marks; ICAO location markings): on each named way ("ref": the font's characters, at most NAME_MAX) before every junction
with a way of another name (_approaches) and before each runway holding position, for each direction of travel: a black box (NAME_TEXT
capitals and NAME_MARGIN_M round them) with a yellow ring and the name in yellow, centred ON the way's centreline (cut out of it, and its
border, by NAME_CUT_M round the box: _cut_lines; the user, 2026-11-17: the yellow ran visibly through the black in game, so the draw
order of the materials is not relied on), upright for the pilot looking along the way, 20 to 30 m before the
junction (NAME_BEFORE_M: the first distance at which the box lies on paving, clear of other paint, a runway's pad, a building and a
parked aircraft). The same name is not painted twice for one direction within NAME_REPEAT_M.

Spines and lenses (standpaths): the stands that stand in a row (noses within half their spans and SPINE_LINK_M of each other, axes
within 72 degrees: SPINE_LINK_COS) none of whose axes reach a lane share a spine of their own, a taxilane along the frontage through
the points behind each tail by the largest half wingspan and SPINE_CLEAR_M (round the centre of a fan or ring: _fan_centre), its
corners rounded and painted as a centreline, joined at one end to the network by a curve tangent to the lane and trimmed at its free end
to the first lens leaving it; the lenses leave that polyline, so each ends exactly on the paint. Each lead-in ends in a straight run
along its axis to the stop bar of at least lead_straight(length) (the nose to the main gear and a quarter of the length, 20 m at
least: an aircraft is lined up straight before it stops) and its arcs (the radius of the kind or more) start behind it; the straight is
never shortened. The rows are made of the stand list alone, so every tile that reads the same stands paints the same.

The mapped centrelines are painted rounded (_mapped_runs: _smooth_ways; SMOOTH_MAPPED_M): a way that goes back on another is two ways
that end on it, every corner an arc, each join tangent (a fillet), an end in an apron carried on to its edge, a gap closed, and the lead-ins
leave exactly those polylines (_lanes). audit(lines) / network() find what is left that is wrong: dangling ends, kinks and S-bends
(tools/airport_map.py draws and counts them).

scene(feats_list) -> Scene: the same read (the ways with their `ref`, the runways with theirs, the runway holding
positions) in one local metre frame, for the airfield signs (airfieldsigns: a runway's signs stand at those positions).

tile_chunks(feats_list, frame, tx, ty, elev, tile_bbox) -> road chunks ({"m", "w", "tl", "p", "n"}) of the tile, as
AirportGrader.tile_chunks draws the runway markings (airportgrade.MARK_LOOK, MARK_PIECE_M, MARK_NODE_M): every line
(a mapped centreline, a lead-in, a stop bar, a hold marking) that meets the tile's box has its parts inside that box
painted, clipped to it (so a line across two tiles is painted by both, the two pieces ending at their shared edge and
joining there), each part cut into pieces (so its bends are one road).
"""
import logging
import weakref

import numpy as np

import aeroways
import airportgrade
import dmath
import dnp
import geodesy
import geotiles
import parkedaircraft as pa
import polygon
import standmarks

log = logging.getLogger(__name__)

CENTRE_W_M = 0.3            # m: the centreline's width (ICAO Annex 14: 0.15 m on a taxiway; 0.3 m: 0.15 m is under a pixel at range and the line
                            # breaks into dashes, so every yellow centreline, spine and lead-in is 0.3 m, with a black border BORDER_M each side)
CENTRE_KIND = "taxi_centreline"
STAND_KIND = "stand_mark"   # the stand details' strokes (wing-tip clearance lines, stand numbers): the same paint
COLOR = "yellow"
RUNWAY_GAP_M = 1.0          # a way's centreline that crosses a runway stops this far outside its edge (one that ends on the runway runs on to its centreline)
RUNWAY_LEAD_RAD = dmath.radians(60.0)   # a way ending on a runway's centreline at less than this angle to it finishes with a tangent arc into it
RUNWAY_LEAD_R_M = 30.0              # ... of this radius (the tangent length and so the radius less where the way's last straight is shorter)
RUNWAY_LEAD_MIN_M = 2.0             # ... none where the way is straight for less than this before the runway
STRAIGHT_TOL_M = 0.15               # (a polyline's last vertices lie within this of the line through its end: straight)
HOLD_DIST_M = 60.0          # the holding position's distance from the runway centreline (ICAO code 3 / 4: 75 m to 90 m)
HOLD_LINE_W_M = 0.15        # pattern A: four lines this wide, this far apart (two solid, two dashed)
HOLD_LINE_GAP_M = 0.15
HOLD_DASH_M = 1.0           # the dashed lines' dash and gap
HOLD_DASH_GAP_M = 1.0
HOLD_SNAP_M = 4.0           # a holding_position point this far off a way's edge is on it
HOLD_MAX_M = 150.0          # ... and one further than this from the runway's centreline holds something else (an ILS)
HOLD_KIND = {True: "hold_solid", False: "hold_dashed"}
MIN_RUN_M = 0.5             # a centreline fragment shorter than this (a sliver beside a cut) is dropped
REACH_M = 280.0             # the margin round a tile whose cells the hook reads (a runway's and a way's join; a stand's lead-in ends in
                            # the tile: its stand and lane are in the cells read)
CACHE_MAX = 8               # strokes kept per first Features (one per list of cells asked)
HOLD_POINT = "holding_position"
LEAD_MODES = ("gate", "remote")     # the stands (parkedaircraft's Craft.mode) that get a lead-in
PAD_EDGE_M = 0.5                    # a lead-in may graze a taxiway's or runway's pad this far in (a junction's corner)
SAMPLE_M = 1.0                      # the lead-in's sampling for the pads
LEAD_STRAIGHT_MIN_M = 20.0          # a stand's lead-in is lined up straight along its axis for at least this far before the stop bar,
LEAD_STRAIGHT_K = 0.25              # ... or, if more, the aircraft's nose-to-main-gear distance and this of its length
SPINE_CLEAR_M = 7.5                 # a row's spine (standpaths): through the points behind each stand's tail by the row's largest half wingspan
                                    # and this (ICAO's taxilane clearance)
SPINE_LINK_M = 25.0                 # stands stand in a row: their noses within half the spans and this of each other,
SPINE_LINK_COS = 0.3                # ... and their axes within 72 degrees (cosine)
SPINE_RING_MIN = 4                  # a ring round a satellite (the service road's): at least this many stands
SPINE_FAN_GAP_DEG = 120.0           # a fan whose stands leave no gap wider than this round its centre is a closed ring
MIN_RADIUS_M = 15.0                 # every curve of the painted yellow lines (a lens, a spine's corners, a connector) a circular arc of
MIN_RADIUS_LIGHT_M = 8.0            # ... at least this radius (light aircraft: this)
ARC_STEP_RAD = 0.1                  # an arc is chords turning at most this (no kink: under 25 degrees)
TURN_MAX_RAD = 3.3                  # an arc, a straight and an arc (_dubins) turning more than this in all is a loop: none
APRON_STEP_M = 1.0                  # a mapped line's parts on an apron found at this step (their black border)
LEAD_STUB_M = 25.0                  # a stand with no lane: a straight run from this far behind its tail to the nose
STOP_BAR_M = 3.6                    # the stop bars' length across the axis
WING_LINE_OFF_M = 3.0               # the wing-tip clearance lines: this far outside the wing tips,
WING_LINE_LEN_M = 20.0              # ... this long, ending at the stop bar's level
WING_CLEAR_M = 3.0                  # ... none within this of another stand's lead-in or envelope or of another stand's line,
WING_MIN_M = 6.0                    # ... a piece left after clipping shorter than this is none (_wing_lines),
WING_NEAR_M, WING_STEP_M = 150.0, 0.25          # ... the stands looked at, the clipping's step
NUM_MAX = 4                         # a ref longer than this is not painted
NUM_TEXT = 1.5                      # the stand number's characters (standmarks.text_quads): the capitals' height
NUM_MARGIN = (0.4, 0.25)            # ... on a yellow box this much larger each side (across, along): "B13" in 4.05 x 2.0 m
NUM_AFTER_M = 1.2                   # ... its box this far past the front stop bar's far edge
LABEL_TEXT = 0.45                   # a stop bar's type label: black on a yellow box LABEL_BOX_M, beside the bar, its capitals this tall
LABEL_BOX_M = (2.2, 0.9)
LABEL_SIDE_M = 0.45                 # ... its box this far off the bar's end
LABEL = {"wide": "WIDE", "narrow": "A320", "turboprop": "AT72", "light": "C172"}    # generic type codes, never an airline's
LADDER_N = 3                        # the stop bars: the stand's own kind's and the next smaller ones' (at most this many)
LADDER_K = 2.0                      # ... each at its nose (the door setback, parkedaircraft.BACK_M) times this from the first's
STOP_BAR_W_M = 0.6                  # a stop bar's depth; the ladder's centreline this wide too (the lead-in 0.3 and the border
LEAD_W_M = 0.3                      # ... BORDER_M wider each side, black, under every yellow lead-in, spine and bar)
BORDER_M = 0.15
UNDER_KIND = "mark_border"
RESTRAINT_KIND = "equip_restraint"
RESTRAINT_OFF_M = 6.0               # the red equipment restraint legs: this far off the fuselage's side (about), each side
RESTRAINT_W_M = 0.2
BRIDGE_BOX_M = (6.0, 8.0)           # the red hatched box under a jet bridge's cab: across x along the bridge, centred this far
BRIDGE_BOX_AT_M = 1.0               # ... past its outer end
HATCH_PITCH_M, HATCH_W_M, BOX_LINE_W_M = 1.2, 0.35, 0.3
ROAD_W_M = 7.0                      # the service road along the terminal: two lanes between solid white edges, a dashed centre,
ROAD_MIN_GAP_M = 9.0                # ... where a stand's nose is at least this far from the wall (the road's centre 9.5 m from
ROAD_WALL_M = 9.5                   # ... the wall, nearer where the gap is short: 3.8 m from the nose), none where it is under
ROAD_NOSE_M = 3.8
ROAD_MAX_GAP_M = 70.0               # a wall further than this is none
ROAD_LINE_W_M = 0.15
ROAD_DASH_M, ROAD_DASH_GAP_M = 3.0, 6.0
WALL_TIE_M = 1.5                    # (two walls' edges this near along a ray are one edge)
ROAD_STEP_M = 2.0                   # the road is walked along the wall's outline at this step,
ROAD_TAPER = 0.6                    # ... easing back to the full distance this much a metre along the wall from a stand that is near,
ROAD_RING_FRAC = 0.4                # ... a ring all round the building where no two neighbouring stands are further apart (of its perimeter),
ROAD_ROUND_MIN = 0.6                # ... or a round building (4 pi area / perimeter squared this or more: a circle is 1) with a ring of stands,
ROAD_BREAK_M = 120.0                # ... else an arc, split where two neighbours are further apart than this,
ROAD_MIN_RADIUS_M = 8.0             # ... cut where the offset path bends tighter than this (a concave corner of the wall),
ROAD_PIECE_MIN_M = 6.0             # ... a piece (between the bridges and the buildings that cut it) shorter than this is none,
BRIDGE_ROAD_CLEAR_M = 4.8           # ... cut this far from a jet bridge's axis (the road's half width and the bridge's 1.3 m), wall to cab
BRIDGE_FOOT_M, BRIDGE_FOOT_MAX_M = 30.0, 80.0   # (a bridge's length where no wall is behind its cab: this; at most that)
ROAD_KIND = "service_road"
GIVEWAY_N, GIVEWAY_BASE_M, GIVEWAY_H_M, GIVEWAY_AT_M = 6, 0.9, 0.75, 1.8    # the give-way triangles at a lead-in's crossing
EQUIP_BOX_M = (6.0, 3.0)            # the equipment parking boxes: along the road x deep, on the wall side of the road,
EQUIP_PITCH_M = 6.8                 # ... this far apart, up to EQUIP_MAX to a gap between two stands (clear of both axes by
EQUIP_CLEAR_M = 7.0                 # ... this much); only where the road is EQUIP_MIN_C_M or more from the wall
EQUIP_MAX = 3
EQUIP_MIN_C_M = 7.3
EQUIP_TEXT = 0.7                    # (the equipment boxes' numbers: white)
EDGE_KIND = "taxi_edge"             # the taxiways' double yellow edge lines (two EDGE_W_M lines EDGE_GAP_M apart, EDGE_IN_M inside the pad's edge)
EDGE_W_M, EDGE_GAP_M, EDGE_IN_M = 0.15, 0.15, 0.45
EDGE_STEP_M = 1.0                   # (the outlines sampled this finely, simplified to EDGE_TOL_M)
EDGE_TOL_M = 0.05
EDGE_MIN_M = 15.0                   # a line shorter than this is none (a stray piece in a junction),
EDGE_JOIN_M = 5.0                   # ... a break shorter than this is run across, and pieces that end this near each other are one line,
PAVE_BUFFER_M = 0.5                 # ... the paving is the union of its pads each grown by this (an edge inside it is no outer edge)
EDGE_QUAD_M = 3.0                   # (in a tile the edge lines are quads of at most this long: one chunk, the corners draped)
PIECE_MIN_M = 0.05                  # a part of a line inside a tile shorter than this (a corner's sliver) is not painted
BRIDGE_STAND_M = 45.0               # a jet bridge's red box only where a stand's nose is this near its outer end
EDGE_RUNWAY_M = 1.5                 # the taxiway edge lines stop this far outside a runway's edge
SMOOTH_MAPPED_M = MIN_RADIUS_M      # the mapped centrelines are painted rounded (corners, joins, ends: _smooth_ways) at this radius (0: as mapped)
RELAX_K, RELAX_MAX_M, RELAX_ITERS = 0.97, 2.5, 300  # (a mapped curve tighter than this of the radius is eased, by up to this much, in so many passes)
EXTEND_M = 16.0                     # (the mapped ways) an end that touches nothing is carried on this far to the edge of the pavement,
JOIN_NODE_M = 0.1                   # (the mapped ways) two ways share a node: a vertex of one this near the other
FILLET_STEPS_M = (2.0, 3.0, 4.5, 6.0, 8.0, 10.0, 12.5, 15.0, 20.0, 25.0, 30.0)     # (a mapped join's fillet: how far back along each line it may start,
FILLET_CAP = 0.6                    # ... at most this of the line,
FILLET_RADII_M = (12.0, 10.0, 8.0)  # ... else at these smaller radii,
FILLET_EXTRA_RAD = 0.6              # ... turning no more than the lines' directions differ by and this
AUDIT_GAP_M = 8.0                   # (the network audit) an end within this of another line but not on it is a gap,
AUDIT_ON_M = 0.25                   # ... on it: within this,
AUDIT_FREE_M = 1.5                  # ... an end this near a stop bar, a runway hold or an apron's or taxiway's edge is meant to stop there,
AUDIT_KINK_RAD = dmath.radians(20.0)  # ... a direction change of more than 20 degrees
AUDIT_JOIN_RAD = AUDIT_KINK_RAD     # (at a join: a line meets another at more than this from tangent and not straight through it),
AUDIT_WINDOW_M = 3.0                # ... within 3 m along a line is a kink,
AUDIT_S_LINK_M = 30.0               # ... two curves of opposite sense within this are an S-bend
NAME_MAX = 3                        # (the taxiways' names, _name_marks) a ref longer than this is not painted
NAME_TEXT = 2.0                     # ... its letters (standmarks.text_quads): capitals 2 m tall (3 m read too big in game, the user, 2026-11-17), yellow on a black box this much larger every side,
NAME_MARGIN_M = 0.4                 # ... (the box's ring: a yellow line NAME_RING_M[1] wide, NAME_RING_M[0] inside its edge, as the location marking of the photographs)
NAME_RING_M = (0.12, 0.18)
NAME_BEFORE_M = (25.0, 22.0, 28.0, 20.0, 30.0)  # ... its centre this far before the junction (the first that is clear: 20 to 30 m),
NAME_JOIN_M = 1.0                   # ... a way's end this near another way's line is a junction on it (a crossing is one too),
NAME_REPEAT_M = 60.0                # ... the same name is not painted twice for one direction of travel within this,
NAME_CLEAR_M = 0.25                 # ... clear of any other paint by this (the way's own centreline, within NAME_OWN_M, excepted: it runs under the box),
NAME_OWN_M = 0.05
NAME_CUT_M = 0.1                    # ... and every line under it (the way's centreline, its black border) is cut out of the box grown by this: the paint's draw order alone left the yellow showing through the black in game (the user, 2026-11-17)
NAME_PAVE_M = 1.0                   # ... the box grown by this on paving (the double edge line lies 0.45 to 0.75 m inside the pad's edge),
NAME_RUNWAY_M = 3.0                 # ... and this far off a runway's pad
_SITE = "_taxiway_site"
_RAW = "_taxiway_raw"
_MEMO = "_taxiway_marks"
_SCENE = "_taxiway_scene"
_EPS = 1e-9


def lead_straight(length):
    """The least length (m) of a lead-in's final straight run along the stand's axis, ending at the stop bar, for an aircraft of
    `length` m: the nose gear to the main gear (parkedaircraft's gear positions) plus LEAD_STRAIGHT_K of its length, at least
    LEAD_STRAIGHT_MIN_M: the aircraft is lined up straight before it reaches its stop position."""
    return max(LEAD_STRAIGHT_MIN_M, (pa.MAIN_GEAR_K - pa.NOSE_GEAR_K + LEAD_STRAIGHT_K) * float(length))


def _rmin(kind):
    """The smallest radius of a painted line's curve (lane to spine, spine to lead-in, a spine's corners) for a stand of `kind`."""
    return MIN_RADIUS_LIGHT_M if kind == "light" else MIN_RADIUS_M


def _arc_pts(c, p0, sense, angle):
    """The points (m, 2) of the circular arc round c from p0, `angle` radians in the sense +1 (counter-clockwise) or -1, in chords
    of at most ARC_STEP_RAD (so no vertex turns by more than that); p0 first, the arc's end last."""
    v = np.asarray(p0, np.float64) - c
    m = max(1, int(np.ceil(angle / ARC_STEP_RAD)))
    out = []
    for k in range(m + 1):
        a = sense * angle * k / m
        ca, sa = dmath.cos(a), dmath.sin(a)
        out.append(c + np.array([ca * v[0] - sa * v[1], sa * v[0] + ca * v[1]]))
    return np.array(out)


def _fillet(x, tin, tout, tl):
    """The circular arc (m, 2) that turns a line travelling along the unit vector tin into one along tout round the corner x: its
    tangent length tl (a radius of tl / tan(half the turn)), from x - tin tl to x + tout tl."""
    cross = float(tin[0] * tout[1] - tin[1] * tout[0])
    ang = dmath.atan2(abs(cross), float(tin[0] * tout[0] + tin[1] * tout[1]))
    p0, p2 = x - tin * tl, x + tout * tl
    if ang < 1e-6 or tl < 1e-9:
        return np.array([p0, p2])
    radius = tl / dmath.tan(ang / 2.0)
    sense = 1.0 if cross > 0.0 else -1.0
    c = p0 + np.array([-tin[1], tin[0]]) * (sense * radius)
    pts = _arc_pts(c, p0, sense, ang)
    pts[-1] = p2
    return pts


def _round_arcs(pts, radius, closed=False):
    """The polyline pts (k, 2) with every corner a circular arc of `radius` (tangent length radius tan(half the turn); less, so a
    tighter arc, where a side is short: at most half of each side, so that neighbouring corners do not meet). A closed polyline is
    rounded at its first point too; its result ends where it began."""
    P = np.asarray(pts, np.float64)
    if len(P) > 2 and bool(np.all(P[0] == P[-1])):
        closed, P = True, P[:-1]
    n = len(P)
    if n < 3:
        return P.copy()
    out = [] if closed else [P[0]]
    for i in (range(n) if closed else range(1, n - 1)):
        p0, p1, p2 = P[i - 1], P[i], P[(i + 1) % n]
        d0, d2 = float(dnp.hypot(*(p1 - p0))), float(dnp.hypot(*(p2 - p1)))
        if d0 < 1e-9 or d2 < 1e-9:
            out.append(p1)
            continue
        tin, tout = (p1 - p0) / d0, (p2 - p1) / d2
        ang = dmath.atan2(abs(float(tin[0] * tout[1] - tin[1] * tout[0])), float(tin[0] * tout[0] + tin[1] * tout[1]))
        if ang < 0.02:
            out.append(p1)
            continue
        tl = min(radius * dmath.tan(ang / 2.0), d0 / 2.0, d2 / 2.0)
        out.extend(_fillet(p1, tin, tout, tl))
    out.append(out[0] if closed else P[-1])
    return np.array(out)


def _dubins(p0, h0, p1, h1, radius):
    """The shortest path from the pose (p0, unit heading h0) to (p1, h1) that is an arc of `radius`, a straight line and an arc
    (Dubins' CSC: LSL, RSR, LSR, RSL), as (length, plan) for _dubins_pts; None where no such path turns by at most TURN_MAX_RAD in
    all (a loop is none)."""
    best = None
    left = lambda h: np.array([-h[1], h[0]])
    a0, a1 = dmath.atan2(float(h0[1]), float(h0[0])), dmath.atan2(float(h1[1]), float(h1[0]))
    for s0 in (1.0, -1.0):
        for s1 in (1.0, -1.0):
            c0, c1 = p0 + left(h0) * (s0 * radius), p1 + left(h1) * (s1 * radius)
            v = c1 - c0
            dist = float(dnp.hypot(*v))
            if s0 == s1:
                if dist < 1e-9:
                    a, straight = a0, 0.0
                else:
                    a, straight = dmath.atan2(float(v[1]), float(v[0])), dist
            else:
                if dist < 2.0 * radius + 1e-9:
                    continue
                straight = dmath.sqrt(dist * dist - 4.0 * radius * radius)
                a = dmath.atan2(float(v[1]), float(v[0])) + s0 * dmath.atan2(2.0 * radius, straight)
            t0 = (s0 * (a - a0)) % dmath.tau            # (the first arc turns from h0 to the straight's heading a ...)
            t0 = 0.0 if t0 > dmath.tau - 1e-6 else t0
            t1 = (s1 * (a1 - a)) % dmath.tau            # (... the second from a to h1)
            t1 = 0.0 if t1 > dmath.tau - 1e-6 else t1
            if t0 + t1 > TURN_MAX_RAD:
                continue
            length = radius * (t0 + t1) + straight
            if best is None or length < best[0] - 1e-9:
                best = (length, (p0, c0, s0, t0, c1, s1, t1, p1, h0, a, radius, straight))
    return best


def _dubins_pts(plan):
    """The polyline (k, 2) of a _dubins result: from p0 round the first arc, along the straight, round the second arc to p1."""
    p0, c0, s0, t0, c1, s1, t1, p1, _h0, a, radius, straight = plan[1]
    d = np.array([dmath.cos(a), dmath.sin(a)])
    first = _arc_pts(c0, p0, s0, t0) if t0 > 1e-9 else np.array([p0])
    mid = first[-1]
    end = mid + d * straight
    second = _arc_pts(c1, end, s1, t1) if t1 > 1e-9 else np.array([end])
    pts = np.vstack([first, second])
    pts[0], pts[-1] = p0, p1
    keep = np.r_[True, dnp.hypot(*np.diff(pts, axis=0).T) > 1e-6]
    return pts[keep]


def _raw(feats):
    """What of feats' airport features the markings read, kept on it: [(id, lonlat (n, 2), class, width tag or None,
    ref)] of its way lines, [(id, lonlat, width tag or None, ref)] of the runway lines (ref: the `ref` tag, "" when
    none: the airfield signs' designators), [(id, lon, lat)] of the holding positions, and whether an international
    aerodrome is in it."""
    slot = _RAW + str(aeroways.runwaysnap.token())      # (per provider of the OurAirports runways the lines are moved onto)
    got = feats.__dict__.get(slot)
    if got is not None:
        return got
    ways, runways, holds, intl = [], [], [], False
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is not None and cls is not None and len(feats):
        air = np.asarray(sub).astype(str) == aeroways.SUBTYPE
        cls = np.asarray(cls).astype(str)
        intl = bool((air & (cls == aeroways.AERODROME)).any())
        kinds = np.asarray(feats.kind)
        snap = aeroways.snapped(feats)                  # (the mapped runway lines on the OurAirports runways they are, the ways' ends with them)
        for i in np.flatnonzero(air & (np.isin(cls, (aeroways.RUNWAY,) + aeroways.WAYS) | (kinds == 1))):
            tags = aeroways._tags(feats, i)
            kind = int(feats.kind[i])
            c = cls[i]
            if kind == 1 and (c == HOLD_POINT or tags.get("aeroway") == HOLD_POINT):
                if "runway" in str(tags.get("holding_position:type") or "runway"):       # (not an ILS or intermediate)
                    holds += [(str(feats.ids[i]), float(ln[0][0]), float(ln[0][1]))
                              for part in feats.parts(i) for ln in part if len(ln)]
            elif kind == 2 and c == aeroways.RUNWAY:
                w = aeroways.width_tag(tags.get("width"))
                for k, ln in enumerate(ln for part in feats.parts(i) for ln in part if len(ln) >= 2):
                    moved = snap[0].get((int(i), k))
                    if moved is None:
                        runways.append((str(feats.ids[i]), np.asarray(ln, np.float64), w, str(tags.get("ref") or "").strip()))
                    elif moved[0] is not None:
                        runways.append((str(feats.ids[i]), moved[0], moved[1], str(tags.get("ref") or "").strip()))
            elif kind == 2 and c in aeroways.WAYS and not aeroways._unpaved(feats, i, tags):
                w = aeroways.width_tag(tags.get("width"))
                ways += [(str(feats.ids[i]), snap[1].get((int(i), k), np.asarray(ln, np.float64)), c, w, str(tags.get("ref") or "").strip())
                         for k, ln in enumerate(ln for part in feats.parts(i) for ln in part if len(ln) >= 2)]
    got = feats.__dict__[slot] = (ways, runways, holds, intl)
    return got


def _pts_dist(pts, segs):
    """The distance from each point of pts (k, 2) to the nearest of the segments segs (m, 2, 2): (k,)."""
    a = segs[:, 0]
    ab = segs[:, 1] - a
    ap = pts[:, None, :] - a[None]
    t = np.clip((ap * ab[None]).sum(axis=2) / np.maximum((ab * ab).sum(axis=1), 1e-12)[None], 0.0, 1.0)
    d = ap - t[..., None] * ab[None]
    return np.sqrt((d * d).sum(axis=2)).min(axis=1)


def _slab(q, dq, lo, hi):
    """The parameters t where lo <= q + t dq <= hi, as arrays (t0, t1) (empty: t0 > t1): q, dq arrays."""
    flat = np.abs(dq) < _EPS
    with np.errstate(divide="ignore", invalid="ignore"):
        ta, tb = (lo - q) / dq, (hi - q) / dq
    t0, t1 = np.minimum(ta, tb), np.maximum(ta, tb)
    inside = (q >= lo) & (q <= hi)
    return (np.where(flat, np.where(inside, -np.inf, np.inf), t0), np.where(flat, np.where(inside, np.inf, -np.inf), t1))


def _stadium(a, b, r0, r1, d):
    """Where the segments a -> b (m, 2) are within d of the segment r0 -> r1: the parameter interval (lo, hi) of each
    (lo >= hi: nowhere). The stadium is convex: the hull of the parts (the rectangle along the segment, a disc at each
    end) is the whole."""
    dv = b - a
    r = r1 - r0
    length = float(dnp.hypot(*r))
    los, his = [], []
    for c in (r0, r1):                          # the discs: |a + t dv - c|^2 <= d^2
        ac = a - c
        qa = dnp.vecdot(dv, dv)
        qb = 2.0 * dnp.vecdot(dv, ac)
        qc = dnp.vecdot(ac, ac) - d * d
        disc = qb * qb - 4.0 * qa * qc
        ok = (disc >= 0) & (qa > _EPS)
        sq = np.sqrt(np.where(ok, disc, 0.0))
        with np.errstate(divide="ignore", invalid="ignore"):
            lo, hi = (-qb - sq) / (2.0 * qa), (-qb + sq) / (2.0 * qa)
        still = (qa <= _EPS) & (qc <= 0)        # (a point: inside or not)
        los.append(np.where(ok, lo, np.where(still, -np.inf, np.inf)))
        his.append(np.where(ok, hi, np.where(still, np.inf, -np.inf)))
    if length > _EPS:                           # the rectangle: along 0..length, across +-d
        ur = r / length
        nr = np.array([-ur[1], ur[0]])
        ua, du = dnp.matmul(a - r0, ur), dnp.matmul(dv, ur)
        va, dvv = dnp.matmul(a - r0, nr), dnp.matmul(dv, nr)
        s0, s1 = _slab(ua, du, 0.0, length)
        c0, c1 = _slab(va, dvv, -d, d)
        lo, hi = np.maximum(s0, c0), np.minimum(s1, c1)
        los.append(lo)
        his.append(hi)
    lo, hi = np.stack(los), np.stack(his)
    good = lo < hi
    lo = np.where(good, lo, np.inf).min(axis=0)
    hi = np.where(good, hi, -np.inf).max(axis=0)
    return np.maximum(lo, 0.0), np.minimum(hi, 1.0)


def _runway_blocks(xy_list, runways, gap):
    """Per way (xy (n, 2)) the blocked parameter intervals of each of its segments: [{segment: [(lo, hi)]}], the parts
    within a runway's half width + gap of its centreline. runways: [(xy, half)]. Only the way segments near a runway
    are looked at (a bounding box test), the rest of a cell's taxiways are not touched."""
    out = [dict() for _ in xy_list]
    if not runways or not xy_list:
        return out
    a = np.concatenate([xy[:-1] for xy in xy_list])
    b = np.concatenate([xy[1:] for xy in xy_list])
    owner = np.concatenate([np.full(len(xy) - 1, i) for i, xy in enumerate(xy_list)])
    first = np.concatenate([[0], np.cumsum([len(xy) - 1 for xy in xy_list])])
    lo_, hi_ = np.minimum(a, b), np.maximum(a, b)
    for rxy, half in runways:
        d = half + gap
        for r0, r1 in zip(rxy[:-1], rxy[1:]):
            near = np.flatnonzero(np.all((hi_ >= np.minimum(r0, r1) - d) & (lo_ <= np.maximum(r0, r1) + d), axis=1))
            if not len(near):
                continue
            lo, hi = _stadium(a[near], b[near], r0, r1, d)
            for j, l, h in zip(near[lo < hi], lo[lo < hi], hi[lo < hi]):
                out[owner[j]].setdefault(int(j - first[owner[j]]), []).append((float(l), float(h)))
    return out


def _free_runs(xy, blocked):
    """The polylines (k, 2) of the way xy with its blocked intervals ({segment: [(lo, hi)]}) cut out."""
    runs, cur = [], None
    for j in range(len(xy) - 1):
        a, d = xy[j], xy[j + 1] - xy[j]
        free, at = [], 0.0
        for lo, hi in sorted(blocked.get(j, ())):
            if lo > at + _EPS:
                free.append((at, lo))
            at = max(at, hi)
        if at < 1.0 - _EPS:
            free.append((at, 1.0))
        for lo, hi in free:
            if cur is not None and lo <= _EPS:
                cur.append(a + hi * d)
            else:
                if cur is not None:
                    runs.append(cur)
                cur = [a + lo * d, a + hi * d]
            if hi < 1.0 - _EPS:
                runs.append(cur)
                cur = None
        if not free and cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    out = []
    for r in runs:
        r = np.asarray(r)
        if float(dnp.hypot(*np.diff(r, axis=0).T).sum()) >= MIN_RUN_M:
            out.append(r)
    return out


def _joins(xy, rsegs):
    """Where the way xy meets the runway whose centreline segments are rsegs (m, 2, 2): [(point, [head, ...])], a head
    the way's line (k, 2) from the point away from the runway. The way's end within JOIN_TOL_M of the centreline
    (one head), a vertex there inside the way and a crossing of the centreline (two)."""
    dist = _pts_dist(xy, rsegs)
    out = []
    n = len(xy)
    for k in np.flatnonzero(dist <= aeroways.JOIN_TOL_M):
        if k == 0:
            out.append((xy[0], [xy]))
        elif k == n - 1:
            out.append((xy[-1], [xy[::-1]]))
        else:
            out.append((xy[k], [xy[k:], xy[k::-1]]))
    a, d = xy[:-1][:, None, :], np.diff(xy, axis=0)[:, None, :]          # (way segments, runway segments) crossings
    c, f = rsegs[:, 0][None], (rsegs[:, 1] - rsegs[:, 0])[None]
    den = d[..., 0] * f[..., 1] - d[..., 1] * f[..., 0]
    ac = c - a
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (ac[..., 0] * f[..., 1] - ac[..., 1] * f[..., 0]) / den
        s = (ac[..., 0] * d[..., 1] - ac[..., 1] * d[..., 0]) / den
    for j, r in zip(*np.nonzero((np.abs(den) > _EPS) & (t > 0) & (t < 1) & (s >= 0) & (s <= 1))):
        p = xy[j] + t[j, r] * (xy[j + 1] - xy[j])
        out.append((p, [np.r_[[p], xy[j + 1:]], np.r_[[p], xy[j::-1]]]))
    return out


def _walk(head, rsegs, target):
    """The point (2,) and the direction (2,) of the polyline head (k, 2) where it first gets target m from the runway
    centreline (rsegs), None if it never does."""
    dist = _pts_dist(head, rsegs)
    idx = np.flatnonzero(dist >= target)
    if not len(idx) or idx[0] == 0:
        return None
    a, b = head[idx[0] - 1], head[idx[0]]
    lo, hi = 0.0, 1.0
    for _ in range(16):                         # (a segment's 1/65536th: millimetres)
        mid = (lo + hi) / 2.0
        if _pts_dist((a + mid * (b - a))[None], rsegs)[0] < target:
            lo = mid
        else:
            hi = mid
    u = (b - a) / max(float(dnp.hypot(*(b - a))), _EPS)
    return a + hi * (b - a), u


def _mapped_hold(head, rsegs, half, holds):
    """The mapped holding position on the head (a way's line from the runway out, half its half width): the point of the
    line under the nearest one and the line's direction there, None when none lies on it (HOLD_SNAP_M, HOLD_MAX_M)."""
    best = None
    seg = np.stack([head[:-1], head[1:]], axis=1)
    cum = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(head, axis=0).T))]
    for hp in holds:
        a, b = seg[:, 0], seg[:, 1]
        ab = b - a
        t = np.clip(dnp.vecdot(hp - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
        pr = a + t[:, None] * ab
        dd = dnp.hypot(*(pr - hp).T)
        j = int(np.argmin(dd))
        if dd[j] > half + HOLD_SNAP_M:
            continue
        away = float(_pts_dist(pr[j][None], rsegs)[0])
        if not half < away <= HOLD_MAX_M:
            continue
        at = cum[j] + float(dnp.hypot(*(pr[j] - a[j])))
        if best is None or at < best[0]:
            best = (at, pr[j], ab[j] / max(float(dnp.hypot(*ab[j])), _EPS))
    return None if best is None else (best[1], best[2])


def _hold_strokes(c, u, width):
    """The pattern A marking's strokes ((x0, y0, x1, y1, w, kind) in metres) across a way of `width` at c, u the unit
    direction away from the runway: the two dashed lines first (at c and one pitch on), then the two solid ones."""
    nv = np.array([-u[1], u[0]])
    half = width / 2.0
    pitch = HOLD_LINE_W_M + HOLD_LINE_GAP_M
    out = []
    for k in range(4):
        p = c + u * (k * pitch)
        solid = k >= 2
        if solid:
            spans = [(-half, half)]
        else:
            n = max(1, int((width + HOLD_DASH_GAP_M) // (HOLD_DASH_M + HOLD_DASH_GAP_M)))
            start = -(n * HOLD_DASH_M + (n - 1) * HOLD_DASH_GAP_M) / 2.0       # (the dashes centred on the way)
            spans = [(start + i * (HOLD_DASH_M + HOLD_DASH_GAP_M),) for i in range(n)]
            spans = [(t, t + HOLD_DASH_M) for t, in spans]
        for s0, s1 in spans:
            a, b = p + nv * s0, p + nv * s1
            out.append((a[0], a[1], b[0], b[1], HOLD_LINE_W_M, HOLD_KIND[solid]))
    return out


def _way_width(cls, tag, intl):
    """A way's width (aeroways' rule: the width tag, else the default of its class and aerodrome)."""
    if tag is not None:
        return tag
    if cls == "taxiway":
        return aeroways.TAXIWAY_INTL_M if intl else aeroways.TAXIWAY_M
    return aeroways.TAXILANE_INTL_M if intl else aeroways.TAXILANE_M


class Scene:
    """What the markings and the airfield signs (airfieldsigns) read of a list of Features, in one local metre frame:
    frame (geodesy.EnuFrame), ways [(xy (k, 2), width, ref, class)], runways [(xy, half width, ref)] and sites
    [(runway, way, point (2,), unit direction (2,))]: the runway holding positions (indexes into runways and ways;
    the direction runs along the way away from the runway). memo: a dict for what a reader keeps on the scene."""
    __slots__ = ("frame", "ways", "runways", "sites", "memo")

    def __init__(self, frame, ways, runways, sites):
        self.frame, self.ways, self.runways, self.sites, self.memo = frame, ways, runways, sites, {}


def _scene(feats_list):
    """The Scene of the Features in feats_list (None for Features with no airport way)."""
    seen, ways, runways, holds, intl = set(), [], [], [], False
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        w, r, h, i = _raw(f)
        intl = intl or i
        ways += [x for x in w if x[0] not in seen]
        runways += [x for x in r if x[0] not in seen]
        holds += [x for x in h if x[0] not in seen]
        seen.update(x[0] for x in w + r + h)
    if not ways:
        return None
    lon0, lat0 = ways[0][1][0]
    frame = geodesy.EnuFrame(float(lat0), float(lon0), 0.0)       # (metres: the tile's own ellipsoid, not a sphere's)

    def to_m(ln):
        e, n, _ = frame.geodetic_to_enu(ln[:, 1], ln[:, 0], np.zeros(len(ln)))
        return np.c_[e, n]
    cuts = np.cumsum([len(x[1]) for x in ways])[:-1]
    wxy = np.split(to_m(np.concatenate([x[1] for x in ways])), cuts)
    wwidth = [_way_width(x[2], x[3], intl) for x in ways]
    rw = [(to_m(x[1]), (x[2] or (aeroways.RUNWAY_INTL_M if intl else aeroways.RUNWAY_M)) / 2.0) for x in runways]
    hp = list(to_m(np.array([[x[1], x[2]] for x in holds]))) if holds else []
    # (the markings across a way where it enters a runway)
    starts = np.r_[0, cuts]
    allm = np.concatenate(wxy)
    wlo, whi = np.minimum.reduceat(allm, starts, axis=0), np.maximum.reduceat(allm, starts, axis=0)
    sites = []
    for ri, (rxy, half) in enumerate(rw):
        lo_, hi_ = rxy.min(axis=0) - aeroways.JOIN_TOL_M, rxy.max(axis=0) + aeroways.JOIN_TOL_M
        rsegs = np.stack([rxy[:-1], rxy[1:]], axis=1)
        found = []                              # (a way's end and its crossing find one site: one marking)
        for wi in np.flatnonzero(np.all((whi >= lo_) & (wlo <= hi_), axis=1)):
            xy, w = wxy[wi], wwidth[wi]
            for _p, heads in _joins(xy, rsegs):
                for head in heads:
                    site = _mapped_hold(head, rsegs, w / 2.0, hp) or _walk(head, rsegs, HOLD_DIST_M)
                    if site is None or any(float(dnp.hypot(*(site[0] - s))) < 3.0 for s in found):
                        continue
                    found.append(site[0])
                    sites.append((ri, int(wi), site[0], site[1]))
    sc = Scene(frame, [(xy, w, x[4], x[2]) for xy, w, x in zip(wxy, wwidth, ways)],
               [(xy, half, x[3]) for (xy, half), x in zip(rw, runways)], sites)
    sc.memo["apron_rings"], sc.memo["pad_rings"] = _pavement_rings(sc, feats_list)
    return sc


def _pavement_rings(sc, feats_list):
    """([apron outline (k, 2)], [taxiway pad outline (k, 2)]) of the Features in feats_list, in the scene's plane: the places a
    centreline may end (an apron's edge, a taxiway's)."""
    aprons, pads = [], []
    try:
        aprons = _apron_rings(sc, feats_list)
        for la, lo in (r for r in aeroways.ways(feats_list) if len(r[0]) >= 4):
            e, n, _ = sc.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            pads.append(np.stack([np.asarray(e), np.asarray(n)], 1))
    except Exception as ex:                     # (no rings: no line is carried on to one)
        log.warning("pavement edges: %s: %s", type(ex).__name__, ex)
    return aprons, pads


def _lanes(sc):
    """The scene's way segments for the lead-ins, kept on it: (a (m, 2), unit direction t (m, 2), length (m,), first (m,)
    and last (m,) of a way's, ends [(point, unit direction out of the way, way index)], the way's index of each segment (m,)):
    the polylines that are painted (_mapped_runs: rounded, cut at runways), so a lead-in always leaves the line that is there."""
    got = sc.memo.get("lanes")
    if got is None:
        a, b, first, last, ends, owner = [np.zeros((0, 2))], [np.zeros((0, 2))], [np.zeros(0, bool)], [np.zeros(0, bool)], [], []
        for xy, _cls, wi, at_first, at_last in _mapped_runs(sc):
            if len(xy) < 2:
                continue
            a.append(xy[:-1])
            b.append(xy[1:])
            f, l = np.zeros(len(xy) - 1, bool), np.zeros(len(xy) - 1, bool)
            f[0], l[-1] = at_first, at_last
            first.append(f)
            last.append(l)
            owner.append(np.full(len(xy) - 1, wi))
            for p, q in ((xy[0], xy[1]), (xy[-1], xy[-2])):
                e = p - q
                ends.append((p, e / max(float(dnp.hypot(*e)), _EPS), wi))
        a, b = np.concatenate(a), np.concatenate(b)
        ln = dnp.hypot(*(b - a).T)
        got = sc.memo["lanes"] = (a, (b - a) / np.maximum(ln, _EPS)[:, None], ln, np.concatenate(first),
                                  np.concatenate(last), ends, np.concatenate([np.zeros(0, np.int64)] + owner))
    return got


def _pads(sc):
    """The pads a lead-in must not cross, kept on the scene: (a (m, 2), b (m, 2), half width (m,), way index (m,): -1 for a
    runway) of the segments of its taxiways (not its taxilanes: those are the aprons' own) and runways."""
    got = sc.memo.get("pads")
    if got is None:
        a, b, h, w = [np.zeros((0, 2))], [np.zeros((0, 2))], [np.zeros(0)], [np.zeros(0, np.int64)]
        pads = [(xy, wd / 2.0, wi) for wi, (xy, wd, _ref, cls) in enumerate(sc.ways) if cls == "taxiway"]
        pads += [(xy, half, -1) for xy, half, _ref in sc.runways]
        for xy, half, wi in pads:
            if len(xy) >= 2:
                a.append(xy[:-1])
                b.append(xy[1:])
                h.append(np.full(len(xy) - 1, half))
                w.append(np.full(len(xy) - 1, wi))
        got = sc.memo["pads"] = (np.concatenate(a), np.concatenate(b), np.concatenate(h), np.concatenate(w))
    return got


def _runway_pads(pads):
    """The runways' part of _pads' (a, b, half width, way index): the segments of way index -1."""
    m = pads[3] == -1
    return tuple(x[m] for x in pads)


def _crosses(pads, pts, own):
    """Does the polyline pts (k, 2) enter (by more than PAD_EDGE_M) the pad of a taxiway or runway other than the way `own`?"""
    a, b, half, way = pads
    if not len(a):
        return False
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    m = (np.maximum(a, b) >= lo - half[:, None]).all(axis=1) & (np.minimum(a, b) <= hi + half[:, None]).all(axis=1) & (way != own)
    if not m.any():
        return False
    seg = pts[1:] - pts[:-1]
    n = np.maximum(1, np.ceil(dnp.hypot(*seg.T) / SAMPLE_M).astype(int))
    smp = np.concatenate([pts[i] + seg[i] * (np.arange(k) / k)[:, None] for i, k in enumerate(n)] + [pts[-1:]])
    a, b, half = a[m], b[m], half[m]
    ab = b - a
    ap = smp[:, None, :] - a[None]
    t = np.clip(dnp.vecdot(ap, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12)[None], 0.0, 1.0)
    d = ap - t[..., None] * ab[None]
    return bool((dnp.hypot(d[..., 0], d[..., 1]) < (half - PAD_EDGE_M)[None]).any())


def _length(pts):
    d = pts[1:] - pts[:-1]
    return float(dnp.hypot(*d.T).sum())


def _project(pts, p):
    """(point, unit direction, distance) of the polyline pts (k, 2) nearest to p."""
    P = np.asarray(pts, np.float64)
    a, d = P[:-1], P[1:] - P[:-1]
    ln2 = np.maximum(dnp.vecdot(d, d), 1e-12)
    t = np.clip(dnp.vecdot(p - a, d) / ln2, 0.0, 1.0)
    q = a + d * t[:, None]
    dist = dnp.hypot(*(q - p).T)
    k = int(np.argmin(dist))
    return q[k], d[k] / max(float(dnp.hypot(*d[k])), _EPS), float(dist[k])


def _min_radius(P, closed=False):
    """The smallest radius of curvature of the polyline P (k, 2) (a vertex's: half the two sides over the turn there; a closed one has
    its first point repeated at its end and turns there too): inf for a straight line."""
    P = np.asarray(P, np.float64)
    if closed:
        P = np.vstack([P, P[1:2]])
    d = np.diff(P, axis=0)
    ln = dnp.hypot(d[:, 0], d[:, 1])
    best = np.inf
    for i in range(1, len(d)):
        if ln[i - 1] < 1e-9 or ln[i] < 1e-9:
            continue
        ang = dmath.atan2(abs(float(d[i - 1][0] * d[i][1] - d[i - 1][1] * d[i][0])), float(d[i - 1][0] * d[i][0] + d[i - 1][1] * d[i][1]))
        if ang > 0.02:
            best = min(best, 0.5 * float(ln[i - 1] + ln[i]) / ang)
    return best


def _under_aircraft(pts, items, margin):
    """Does the polyline pts (k, 2) come within `margin` m of the envelope (its length by its wingspan) of an aircraft of `items`
    [(nose, unit direction, length, span)]?"""
    P = np.asarray(pts, np.float64)
    seg = np.diff(P, axis=0)
    cnt = np.maximum(1, np.ceil(dnp.hypot(seg[:, 0], seg[:, 1]) / 2.0).astype(np.int64))
    smp = np.concatenate([P[j] + seg[j] * (np.arange(c) / c)[:, None] for j, c in enumerate(cnt)] + [P[-1:]])
    for nose, u, length, span in items:
        v = smp - (nose - u * (length / 2.0))
        along = np.abs(v[:, 0] * u[0] + v[:, 1] * u[1]) - length / 2.0
        across = np.abs(v[:, 0] * -u[1] + v[:, 1] * u[0]) - span / 2.0
        if bool((dnp.hypot(np.maximum(along, 0.0), np.maximum(across, 0.0)) < margin).any()):
            return True
    return False


def _fan_centre(items, members):
    """(the point the stands' axes point at, closed): the least-squares meeting point of the lines along their headings through their
    noses (a satellite's centre, a pier tip's), and whether the stands leave no gap over SPINE_FAN_GAP_DEG round it (a full ring)."""
    a11 = a12 = a22 = b1 = b2 = 0.0
    for i in members:
        (nx, ny), (ux, uy) = items[i][0], items[i][1]
        m11, m12, m22 = 1.0 - ux * ux, -ux * uy, 1.0 - uy * uy
        a11, a12, a22 = a11 + m11, a12 + m12, a22 + m22
        b1, b2 = b1 + m11 * nx + m12 * ny, b2 + m12 * nx + m22 * ny
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-6:
        c = np.array([items[i][0] for i in members]).mean(axis=0)
    else:
        c = np.array([(a22 * b1 - a12 * b2) / det, (a11 * b2 - a12 * b1) / det])
    ang = sorted(dmath.atan2(float(items[i][0][1] - c[1]), float(items[i][0][0] - c[0])) for i in members)
    gaps = [ang[k + 1] - ang[k] for k in range(len(ang) - 1)] + [ang[0] + 2.0 * np.pi - ang[-1]]
    return c, max(gaps) <= dmath.radians(SPINE_FAN_GAP_DEG)


def _fan_order(ang, members):
    """The indices of the angles in order round the centre, starting after the widest gap (an open fan's two ends)."""
    order = sorted(range(len(ang)), key=lambda k: (round(ang[k], 6), members[k]))
    a = [ang[k] for k in order]
    gaps = [a[k + 1] - a[k] for k in range(len(a) - 1)] + [a[0] + 2.0 * np.pi - a[-1]]
    g = max(range(len(gaps)), key=lambda k: (gaps[k], -k))
    return order[g + 1:] + order[:g + 1]


def _crossing(a, b, tol=0.02):
    """Does the polyline a (k, 2) cross the polyline b (m, 2): a segment of one strictly across a segment of the other (the
    ends clear of the other's line by more than tol m: a touch, a shared end or a run along it is no crossing)?"""
    p, q, r, w = a[:-1][:, None], a[1:][:, None], b[:-1][None], b[1:][None]
    d1, d2 = q - p, w - r
    n1 = np.maximum(dnp.hypot(d1[..., 0], d1[..., 1]), _EPS)
    n2 = np.maximum(dnp.hypot(d2[..., 0], d2[..., 1]), _EPS)
    cr = lambda u, v: u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]
    o1, o2 = cr(d1, r - p) / n1, cr(d1, w - p) / n1
    o3, o4 = cr(d2, p - r) / n2, cr(d2, q - r) / n2
    split = lambda x, y: ((x > tol) & (y < -tol)) | ((x < -tol) & (y > tol))
    return bool((split(o1, o2) & split(o3, o4)).any())


def _pt_segs(p, s, e):
    """The distance from the point p to each of the segments s -> e (m, 2): (m,)."""
    d = e - s
    t = np.clip(dnp.vecdot(p - s, d) / np.maximum(dnp.vecdot(d, d), 1e-12), 0.0, 1.0)
    q = s + d * t[:, None]
    return dnp.hypot(q[:, 0] - p[0], q[:, 1] - p[1])


def _poly_seg_dist(P, a, b):
    """The distance between the polyline P (m, 2) and the segment a -> b (0 where they cross)."""
    s, e = P[:-1], P[1:]
    if not len(s):
        return float(_pt_segs(P[0], a[None], b[None])[0])
    cr = lambda o, p, q: (p[..., 0] - o[..., 0]) * (q[..., 1] - o[..., 1]) - (p[..., 1] - o[..., 1]) * (q[..., 0] - o[..., 0])
    d1, d2 = cr(a, b, s), cr(a, b, e)
    d3, d4 = cr(s, e, a), cr(s, e, b)
    if bool(((d1 * d2 < 0.0) & (d3 * d4 < 0.0)).any()):
        return 0.0
    ab = b - a
    t = np.clip(dnp.vecdot(P - a, ab[None]) / max(float(ab[0] * ab[0] + ab[1] * ab[1]), 1e-12), 0.0, 1.0)
    q = a + ab * t[:, None]
    near = float(dnp.hypot(P[:, 0] - q[:, 0], P[:, 1] - q[:, 1]).min())
    return min(near, float(_pt_segs(a, s, e).min()), float(_pt_segs(b, s, e).min()))


def _wing_lines(stands_, leads):
    """{stand index: [(a (2,), b (2,))]} of the wing-tip clearance lines of the stands (_lead_lines' tuples (Craft, unit direction,
    length, span, nose); leads: each stand's kept lead-in or None): WING_LINE_OFF_M outside the wing tips, WING_LINE_LEN_M long,
    ending level with the nose; where stands are close they would overlap or cross: a line is clipped so that nothing of it is
    within WING_CLEAR_M of another stand's envelope (the aircraft's rectangle: it does not run under an adjacent aircraft; a
    piece under WING_MIN_M is none), dropped where it comes within WING_CLEAR_M of another stand's lead-in, and of two lines that
    come within WING_CLEAR_M of each other (or cross) the one of the later stand is dropped (the earlier keeps the line they
    share)."""
    n = len(stands_)
    out = {i: [] for i in range(n)}
    if not n:
        return out
    noses = np.array([x[4] for x in stands_])
    raw = {}
    for i, (_c, u, _length, span, nose) in enumerate(stands_):
        left = np.array([-u[1], u[0]])
        for sgn in (-1.0, 1.0):
            side = left * sgn * (span / 2.0 + WING_LINE_OFF_M)
            raw[(i, sgn)] = (nose + side - u * WING_LINE_LEN_M, nose + side)
    for i in range(n):
        near = [j for j in np.flatnonzero(dnp.hypot(*(noses - noses[i]).T) <= WING_NEAR_M) if j != i]
        for sgn in (-1.0, 1.0):
            a, b = raw[(i, sgn)]
            k = max(2, int(np.ceil(WING_LINE_LEN_M / WING_STEP_M)) + 1)
            f = np.linspace(0.0, 1.0, k)[:, None]
            smp = a + (b - a) * f
            free = np.ones(k, bool)
            for j in near:                                  # (not under an adjacent aircraft: its envelope's distance)
                _cj, uj, lj, sj, nj = stands_[j]
                v = smp - (nj - uj * (lj / 2.0))
                along = np.abs(v[:, 0] * uj[0] + v[:, 1] * uj[1]) - lj / 2.0
                across = np.abs(v[:, 0] * -uj[1] + v[:, 1] * uj[0]) - sj / 2.0
                free &= dnp.hypot(np.maximum(along, 0.0), np.maximum(across, 0.0)) >= WING_CLEAR_M
            pieces, at = [], 0
            while at < k:
                if not free[at]:
                    at += 1
                    continue
                end = at
                while end + 1 < k and free[end + 1]:
                    end += 1
                if (end - at) * WING_LINE_LEN_M / (k - 1) >= WING_MIN_M:
                    pieces.append((smp[at], smp[end]))
                at = end + 1
            if not pieces:
                continue
            clash = False
            for j in near:
                if leads[j] is not None and any(_poly_seg_dist(leads[j], p, q) < WING_CLEAR_M for p, q in pieces):
                    clash = True
                for sg in (-1.0, 1.0):
                    if j < i and not clash:
                        c, d = raw[(j, sg)]
                        clash = _seg_dist(a, b, c, d) < WING_CLEAR_M
            if not clash:
                out[i] += pieces
    return out


def _seg_dist(a, b, c, d):
    """The distance between the segments a -> b and c -> d (0 where they cross)."""
    return _poly_seg_dist(np.array([c, d]), a, b)


def _subpath(P, lo, hi):
    """The part of the polyline P (k, 2) from `lo` to `hi` metres along it."""
    cum = standmarks.cumulative(P)
    inner = [P[i] for i in range(1, len(P) - 1) if lo + 1e-6 < cum[i] < hi - 1e-6]
    return np.array([standmarks.at(P, lo)[0]] + inner + [standmarks.at(P, hi)[0]])


def stand_key(stands):
    """A hashable key of the stands' (parkedaircraft Craft) places, kinds and refs: what the memos of the lead-ins and the stand
    signs are kept by."""
    return tuple((c.kind, c.mode, c.ref, round(c.lat, 7), round(c.lon, 7), round(c.ne, 4), round(c.nn, 4)) for c in stands)


_NO_MARKS = ([], np.zeros((0, 4)), [], np.zeros((0, 4)))


def _ladder(kind):
    """[(kind, offset)] of the stop bars of a stand of `kind`: its own at the nose (offset 0) and the next smaller kinds' (the
    ones that fit), at most LADDER_N, each at its own nose, (the door's setback, parkedaircraft.BACK_M) times LADDER_K from it."""
    own = pa.SPECS[kind][0]
    fit = sorted((k for k in pa.SPECS if pa.SPECS[k][0] <= own), key=lambda k: -pa.SPECS[k][0])[:LADDER_N]
    return [(k, (pa.BACK_M[k] - pa.BACK_M[kind]) * LADDER_K) for k in fit]


def _wall_hit(nose, u, walls):
    """(distance, index) of the first edge of the rings `walls` ([(k, 2)]) the ray from the nose along u meets: (inf, -1) where it meets
    none. Where two rings' edges are within WALL_TIE_M along the ray (a satellite is part of its terminal's outline) the smaller ring."""
    best, which = np.inf, -1
    found = []
    for wi, ring in enumerate(walls):
        d = np.roll(ring, -1, axis=0) - ring
        w = ring - nose
        den = u[0] * d[:, 1] - u[1] * d[:, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (w[:, 0] * d[:, 1] - w[:, 1] * d[:, 0]) / den
            s = (w[:, 0] * u[1] - w[:, 1] * u[0]) / den
        ok = (np.abs(den) > _EPS) & (t > 0.0) & (s >= 0.0) & (s <= 1.0)
        if ok.any():
            found.append((float(t[ok].min()), wi))
    if found:
        best = min(t for t, _wi in found)
        size = lambda wi: float(np.prod(np.ptp(walls[wi], axis=0)))
        which = min((wi for t, wi in found if t <= best + WALL_TIE_M), key=lambda wi: (size(wi), wi))
        best = next(t for t, wi in found if wi == which)
    return best, which


def _station(poly, p):
    """The distance along the polyline poly (k, 2) of its point nearest to p."""
    P = np.asarray(poly, np.float64)
    d = P[1:] - P[:-1]
    ln = dnp.hypot(d[:, 0], d[:, 1])
    t = np.clip(dnp.vecdot(p - P[:-1], d) / np.maximum(ln * ln, 1e-12), 0.0, 1.0)
    q = P[:-1] + d * t[:, None]
    k = int(np.argmin(dnp.hypot(*(q - p).T)))
    return float(ln[:k].sum() + t[k] * ln[k])


def _wall_samples(ring, step=ROAD_STEP_M):
    """(points (n, 2), outward unit normals (n, 2), stations (n,), perimeter) of the ring (k, 2) walked counter-clockwise, a point
    about every `step` m (the normals smoothed over the neighbours: a corner is rounded)."""
    P = np.asarray(ring, np.float64)
    if len(P) > 2 and float(dnp.hypot(*(P[0] - P[-1]))) < 1e-6:
        P = P[:-1]
    area = float((P[:, 0] * np.roll(P[:, 1], -1) - np.roll(P[:, 0], -1) * P[:, 1]).sum())
    if area < 0.0:
        P = P[::-1]
    seg = np.roll(P, -1, axis=0) - P
    ln = dnp.hypot(seg[:, 0], seg[:, 1])
    cnt = np.maximum(1, np.ceil(ln / step).astype(np.int64))
    pts = np.concatenate([P[j] + seg[j] * (np.arange(c) / c)[:, None] for j, c in enumerate(cnt)])
    tan = np.roll(pts, -2, axis=0) - np.roll(pts, 2, axis=0)
    tan = tan / np.maximum(dnp.hypot(tan[:, 0], tan[:, 1]), _EPS)[:, None]
    st = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(np.vstack([pts, pts[:1]]), axis=0).T))]
    return pts, np.stack([tan[:, 1], -tan[:, 0]], 1), st[:-1], float(st[-1])


def _offset_path(pts, d, closed):
    """standmarks.offset of the polyline pts (a closed one has its first point repeated at its end, and is mitred there too)."""
    if not closed:
        return standmarks.offset(pts, d)
    return standmarks.offset(np.vstack([pts[-2:-1], pts, pts[1:2]]), d)[1:-1]


def _runs(blocked, closed):
    """The runs of consecutive unblocked indices of the flags `blocked` (n,): [(indices, closed)]; a closed list's run goes on across
    its end; the whole list as one closed run where none is blocked."""
    n = len(blocked)
    if blocked.all():
        return []
    if not blocked.any():
        return [(np.arange(n), closed)]
    start = int(np.flatnonzero(blocked)[0]) + 1 if closed else 0
    order = [(start + k) % n for k in range(n)] if closed else list(range(n))
    out, cur = [], []
    for k in order:
        if blocked[k]:
            if cur:
                out.append(np.array(cur))
            cur = []
        else:
            cur.append(k)
    if cur:
        out.append(np.array(cur))
    return [(r, False) for r in out]


def _pt_segs_many(P, a, b):
    """The distance from each point of P (k, 2) to the segment a -> b: (k,)."""
    d = b - a
    t = np.clip(dnp.vecdot(P - a, d[None]) / max(float(d[0] * d[0] + d[1] * d[1]), 1e-12), 0.0, 1.0)
    q = a + d * t[:, None]
    return dnp.hypot(q[:, 0] - P[:, 0], q[:, 1] - P[:, 1])


def _service_roads(items, rows, walls, leads, bridges=()):
    """([(xy (k, 2), width, kind, colour)], [(polygon (k, 2), colour, layer)]) of the service roads of the stands (items:
    [(nose, unit direction, length, span)]; rows: standpaths.build's; bridges: [(outer end (2,), unit direction out of the terminal)]):
    ONE path along each building's frontage, offset from its outline (`walls`: [(k, 2)] rings). The stands whose axis meets a
    wall (ROAD_MIN_GAP_M to ROAD_MAX_GAP_M from the nose: a nearer wall has none) set the road's distance from it, ROAD_WALL_M,
    nearer (ROAD_NOSE_M clear of the nose, the offset easing back ROAD_TAPER per metre along the wall) where the gap is short.
    A wall the stands stand all round (no two neighbours further apart than ROAD_RING_FRAC of its perimeter, or a satellite's
    ring of stands) has a ring all round it; any other wall the arc from its first stand to its last (half a span beyond each),
    split where two neighbours are over ROAD_BREAK_M apart. The path is cut only where a jet bridge's footprint (the bridge from
    its wall to its outer end, BRIDGE_ROAD_CLEAR_M clear), or a building that is not the wall it follows, is in the way, and goes on
    beyond. Two lanes ROAD_W_M wide in all: solid white edges, a dashed centre, a row of white give-way triangles each side of
    every lead-in (`leads`: polylines) that crosses it; and boxes for equipment (3 x 6 m, white outlines, numbered) on the wall
    side between two stands where the road is far enough from the wall."""
    ribbons, polys = [], []
    sm = standmarks
    ringed = set()
    for members, kind in rows:
        if kind == "ring":
            ringed.update(members)
    hits = {}                                           # wall index: [(item, wall point, road distance)]
    for i, (nose, u, _length_, _span) in enumerate(items):
        gap, wi = _wall_hit(nose, u, walls)
        if wi >= 0 and ROAD_MIN_GAP_M <= gap <= ROAD_MAX_GAP_M:
            hits.setdefault(wi, []).append((i, nose + u * gap, min(ROAD_WALL_M, gap - ROAD_NOSE_M)))
    feet = []                                           # the bridges' footprints: (a, b) from the wall to the cab
    for pt, d in bridges:
        back, _wi = _wall_hit(pt, -d, walls)
        feet.append((pt - d * (back if back <= BRIDGE_FOOT_MAX_M else BRIDGE_FOOT_M), pt))
    for wi in sorted(hits):
        mine = hits[wi]
        if len(mine) < 2:
            continue
        pts, nrm, st, perim = _wall_samples(walls[wi])
        n = len(pts)
        at = []                                         # (station, item, sample index, road distance) of each stand, in order
        for i, hp, cs in mine:
            k = int(np.argmin(dnp.hypot(pts[:, 0] - hp[0], pts[:, 1] - hp[1])))
            at.append((float(st[k]), i, k, cs))
        at.sort()
        stn = np.array([a[0] for a in at])
        dist = np.full(n, ROAD_WALL_M)
        for sa, _i, _k, cs in at:
            dd = np.abs(st - sa)
            dist = np.minimum(dist, cs + ROAD_TAPER * np.minimum(dd, perim - dd))
        gaps = np.diff(np.r_[stn, stn[0] + perim])      # (the gap after each stand, the last across the start)
        area = 0.5 * abs(float((pts[:, 0] * np.roll(pts[:, 1], -1) - np.roll(pts[:, 0], -1) * pts[:, 1]).sum()))
        round_ = 4.0 * dmath.pi * area / max(perim * perim, _EPS) >= ROAD_ROUND_MIN and len(at) >= SPINE_RING_MIN   # (a satellite)
        closed = bool(gaps.max() <= ROAD_RING_FRAC * perim or round_ or sum(a[1] in ringed for a in at) >= SPINE_RING_MIN)
        centre = pts + nrm * dist[:, None]
        spans = []                                      # (sample indices, closed) of the paths along the wall
        if closed:
            spans.append((np.arange(n), True))
        else:
            cut = [j for j in range(len(at)) if gaps[j] > ROAD_BREAK_M]
            first = (cut[-1] + 1) % len(at) if cut else 0
            groups, cur = [], []
            for j in range(len(at)):
                cur.append((first + j) % len(at))
                if (first + j) % len(at) in cut:
                    groups.append(cur)
                    cur = []
            if cur:
                groups.append(cur)
            for g in groups:
                if len(g) < 2:
                    continue
                half0, half1 = items[at[g[0]][1]][3] / 2.0, items[at[g[-1]][1]][3] / 2.0
                lo = at[g[0]][0] - half0
                width = (at[g[-1]][0] - at[g[0]][0]) % perim + half0 + half1
                off = (st - lo) % perim
                idx = np.flatnonzero(off <= width)
                spans.append((idx[np.argsort(off[idx], kind="stable")], False))
        inner = pts - nrm                               # (a building that holds the wall: its outline has the wall's points just inside)
        others = [j for j in range(len(walls)) if j != wi and float(np.mean(polygon.contains_points(walls[j], inner))) < 0.5]
        number = 0
        for idx, loop in spans:
            c0, tg = centre[idx], np.stack([-nrm[idx][:, 1], nrm[idx][:, 0]], 1)
            keep = [0]                                  # (a concave corner folds the offset back on itself: those points go)
            for k in range(1, len(idx)):
                v = c0[k] - c0[keep[-1]]
                if float(v[0] * tg[k][0] + v[1] * tg[k][1]) > 0.3 * float(dnp.hypot(*v)) or (loop and k == len(idx) - 1):
                    keep.append(k)
            idx = idx[keep]
            c0 = centre[idx]
            blocked = np.zeros(len(idx), bool)
            if len(c0) > 2:                             # (a corner of the wall the road cannot follow: tighter than ROAD_MIN_RADIUS_M)
                v = np.diff(c0, axis=0)
                ln = np.maximum(dnp.hypot(v[:, 0], v[:, 1]), _EPS)
                for k in range(1, len(c0) - 1):
                    ang = dmath.atan2(abs(float(v[k - 1][0] * v[k][1] - v[k - 1][1] * v[k][0])), float(v[k - 1][0] * v[k][0] + v[k - 1][1] * v[k][1]))
                    if ang > 0.02 and 0.5 * float(ln[k - 1] + ln[k]) / ang < ROAD_MIN_RADIUS_M:
                        blocked[max(0, k - 1):k + 2] = True
            for a, b in feet:                           # (a jet bridge: its footprint)
                blocked |= _pt_segs_many(c0, a, b) < BRIDGE_ROAD_CLEAR_M
            for j in others:                            # (a building that is not the wall the road follows, nor one that holds it)
                lo_, hi_ = walls[j].min(axis=0), walls[j].max(axis=0)
                for side in (0.0, 1.0, -1.0):
                    q = c0 + nrm[idx] * (side * ROAD_W_M / 2.0)
                    m = np.all((q >= lo_) & (q <= hi_), axis=1) & ~blocked
                    if m.any():
                        blocked[np.flatnonzero(m)[polygon.contains_points(walls[j], q[m])]] = True
            for run, closed_run in _runs(blocked, loop):
                road = c0[run]
                if closed_run:
                    road = np.vstack([road, road[:1]])
                if _length(road) < ROAD_PIECE_MIN_M:
                    continue
                if closed_run:                          # (the walk's 2 m steps are only where the road bends; a ring in two halves)
                    m = len(road) // 2
                    road = np.vstack([_simplify(road[:m + 1], EDGE_TOL_M), _simplify(road[m:], EDGE_TOL_M)[1:]])
                else:
                    road = _simplify(road, EDGE_TOL_M)
                for side in (1.0, -1.0):
                    ribbons.append((_offset_path(road, side * ROAD_W_M / 2.0, closed_run), ROAD_LINE_W_M, ROAD_KIND, "white"))
                polys += [(p, "white", "paint") for p in sm.dashes(road, ROAD_DASH_M, ROAD_DASH_GAP_M, ROAD_LINE_W_M)]
                seen = []
                for lead in leads:
                    for pt, _s, d in sm.crossings(road, lead):
                        if any(float(dnp.hypot(*(pt - q))) < 1.0 for q in seen):
                            continue
                        seen.append(pt)
                        for sgn in (1.0, -1.0):
                            polys += [(p, "white", "paint") for p in sm.triangle_row(
                                pt + d * (sgn * (LEAD_W_M / 2.0 + GIVEWAY_AT_M)), d * sgn, ROAD_W_M - 1.0, GIVEWAY_N,
                                GIVEWAY_BASE_M, GIVEWAY_H_M)]
                here = []                               # (the stands whose road point is on this piece, in order along it)
                for _sa, i, k, cs in at:
                    if _project(road, centre[k])[2] < 1.0:
                        here.append((_station(road, centre[k]), i, k, cs))
                here.sort()
                mean_u = np.array([items[h[1]][1] for h in here]).mean(axis=0) if here else np.array([1.0, 0.0])
                mean_u = mean_u / max(float(dnp.hypot(*mean_u)), _EPS)
                for (si, _i, ki, ci), (sk, _k, _kk, ck) in zip(here, here[1:]):
                    span = sk - si - 2.0 * EQUIP_CLEAR_M
                    if span < EQUIP_BOX_M[0] or (ci + ck) / 2.0 < EQUIP_MIN_C_M:
                        continue
                    count = min(EQUIP_MAX, int((span + EQUIP_PITCH_M - EQUIP_BOX_M[0]) // EQUIP_PITCH_M))
                    for j in range(count):
                        p, _r = sm.at(road, (si + sk) / 2.0 + (j - (count - 1) / 2.0) * EQUIP_PITCH_M)
                        nu = -nrm[ki]                   # (towards the wall)
                        at_ = p + nu * (ROAD_W_M / 2.0 + 0.5 + EQUIP_BOX_M[1] / 2.0)
                        polys += [(q, "white", "paint") for q in sm.outline(at_, nu, EQUIP_BOX_M[0], EQUIP_BOX_M[1], ROAD_LINE_W_M)]
                        number += 1
                        polys += [(q, "font_white", "glyph", uv) for q, uv in sm.text_quads(str(number), at_, mean_u, EQUIP_TEXT)]
    return ribbons, polys


def _site_xy(sc, site):
    """(walls [(k, 2)], bridges [(point (2,), unit direction (2,))], blocks [(k, 2)]) of a Site in the scene's plane, kept on the scene."""
    key = ("site", site.key)
    got = sc.memo.get(key)
    if got is None:
        def xy(la, lo):
            e, n, _ = sc.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            return np.stack([np.asarray(e), np.asarray(n)], 1)
        walls = [xy(la, lo) for la, lo in site.walls if len(la) >= 3]
        bridges = []
        for la, lo, de, dn in site.bridges:
            bridges.append((xy([la], [lo])[0], _unit2(de, dn)))
        got = sc.memo[key] = (walls, bridges, [xy(la, lo) for la, lo in site.blocks if len(la) >= 3])
    return got


def _unit2(x, y):
    n = max(float(dnp.hypot(x, y)), _EPS)
    return np.array([x / n, y / n])


def _hits_blocks(poly, blocks):
    """Does the convex polygon `poly` (k, 2) overlap any of `blocks` [(ring (m, 2), (x0, y0, x1, y1))]: an edge across a ring's, or one inside the other?"""
    lo, hi = poly.min(axis=0), poly.max(axis=0)
    closed = np.vstack([poly, poly[:1]])
    for ring, (x0, y0, x1, y1) in blocks:
        if x1 < lo[0] or x0 > hi[0] or y1 < lo[1] or y0 > hi[1]:
            continue
        if pa._pip(ring, poly).any() or pa._pip(poly, ring).any() or _crossing(closed, np.vstack([ring, ring[:1]]), 0.0):
            return True
    return False


def _lead_lines(sc, stands, site=None):
    """(lines, boxes, polys, pboxes) of the lead-ins (standpaths.build: the lanes, then each stand's lens off its lane), stop bars and stand
    details (module docstring) of the stands (parkedaircraft
    Craft), memoized on the scene. lines: _build's form (lat, lon, width, kind, colour); polys: [(lat (k,), lon (k,), colour,
    layer)], convex polygons (the boxes, the lettering, the bars, the hatching, the dashes and the triangles: one chunk a
    colour and layer in a tile), layer "under" (the black borders, painted first), "paint" or "glyph" (the black lettering,
    painted last); boxes and pboxes their lat0, lat1, lon0, lon1 extents. A stand that overlaps a longer one (or one of the same
    kind) in the list is passed over (the list is parkedaircraft.lead_stands's: none do, but a stand twice over, from two lists
    of cells, paints one line): the lines are painted once whatever the tile, and only for a stand that is kept. site: the walls
    of the terminals and the jet bridges' outer ends (Site), for the service roads and the red boxes."""
    st = [c for c in stands if c.mode in LEAD_MODES]
    if not st:
        return _NO_MARKS
    key = ("lead", stand_key(st), None if site is None else site.key)
    got = sc.memo.get(key)
    if got is not None:
        return got
    pads = _pads(sc)
    e, nn, _ = sc.frame.geodetic_to_enu(np.array([c.lat for c in st]), np.array([c.lon for c in st]), np.zeros(len(st)))
    e, nn = np.asarray(e, np.float64), np.asarray(nn, np.float64)
    plane = [(c.kind, float(x), float(y), float(c.ne), float(c.nn)) for c, x, y in zip(st, e, nn)]
    out, polys, seen = [], [], set()                    # (xy (k, 2), width, kind, colour), (polygon (k, 2), colour, layer)
    net, stops = [], []                                 # the network's lines [(xy, role, rmin)] and the stops [(nose, unit direction, kind)]
    sm = standmarks

    def add(xy, kind=CENTRE_KIND, width=LEAD_W_M, under=True, role=None, rmin=MIN_RADIUS_M):
        k = tuple(round(float(q) * 2.0) for q in (xy[0, 0], xy[0, 1], xy[-1, 0], xy[-1, 1]))
        if k not in seen:
            seen.add(k)
            if role is not None:
                net.append((xy, role, rmin))
            if under:                                   # (the black border under every yellow lead-in and spine)
                out.append((xy, width + 2.0 * BORDER_M, UNDER_KIND, "black"))
            out.append((xy, width, kind, COLOR))

    blocks = [] if site is None else [(r, (float(r[:, 0].min()), float(r[:, 1].min()), float(r[:, 0].max()), float(r[:, 1].max())))
                                      for r in _site_xy(sc, site)[2]]       # (every footprint: no nose marking lies under one)

    def box(centre, up, w, h, colour="yellow", border=True, acc=None):
        acc = polys if acc is None else acc
        if border:
            acc.append((sm.box(centre, up, w + 2.0 * BORDER_M, h + 2.0 * BORDER_M), "black", "under"))
        acc.append((sm.box(centre, up, w, h), colour, "paint"))

    def lettered(text, centre, up, h, acc=None):
        (polys if acc is None else acc).extend((q, "font_black", "glyph", uv) for q, uv in sm.text_quads(text, centre, up, h))

    def put(*builds):
        """The chunks of the first of the builds (functions taking the list to fill) that lies under no footprint, painted; False where
        every one does (a marking that cannot be moved clear is clipped away whole)."""
        for build in builds:
            acc = []
            build(acc)
            if not any(_hits_blocks(x[0], blocks) for x in acc):
                polys.extend(acc)
                return True
        return False

    stands_ = []                                        # (Craft, unit direction, length, span, nose) of the stands kept
    for j in pa.drop_overlaps(plane):
        c, ce, cn = st[j], float(e[j]), float(nn[j])
        u = np.array([c.ne, c.nn])
        u = u / max(float(dnp.hypot(*u)), _EPS)
        length, span = pa.SPECS[c.kind][0], pa.SPECS[c.kind][1]
        stands_.append((c, u, length, span, np.array([ce, cn]) + u * (length / 2.0)))
    items = [(x[4], x[1], x[2], x[3]) for x in stands_]
    import standpaths                                    # (lanes first, then each stand's lens off its lane: standpaths)
    runs = [(xy, wi) for xy, _cls, wi, _f, _l in _mapped_runs(sc) if len(xy) >= 2]
    aprons = list(sc.memo.get("apron_rings") or ())
    paint, found, rows = standpaths.build(items, [x[0].kind for x in stands_], runs, pads, blocks,
                                          aprons + list(sc.memo.get("pad_rings") or ()), aprons)
    stops = [(x[4], x[1], x[0].kind) for x in stands_]
    keep = {i for i, f in enumerate(found) if f is not None}
    leads = []
    for xy, role, rm in paint:
        add(xy, role=role, rmin=rm)
        leads.append(xy)
    wings = _wing_lines(stands_, [found[i] if i in keep else None for i in range(len(stands_))])
    for i, (c, u, length, span, nose) in enumerate(stands_):
        line = found[i] if i in keep else None
        left = np.array([-u[1], u[0]])
        if line is not None:
            add(line, role="lead", rmin=_rmin(c.kind))
            leads.append(line)
        ladder = _ladder(c.kind)
        back = min(off for _k, off in ladder)
        if back < -0.5:                                 # (the thick centreline through the ladder)
            put(lambda acc: box(nose + u * (back / 2.0), u, STOP_BAR_W_M, -back + STOP_BAR_W_M, acc=acc))
        for kind, off in ladder:                        # (a stop bar for each kind, a type label beside it: on the pilot's right, else his left)
            at = nose + u * off
            put(lambda acc: box(at, u, STOP_BAR_M, STOP_BAR_W_M, acc=acc))
            builds = []
            for side in (-1.0, 1.0):
                lab = at + left * (side * (STOP_BAR_M / 2.0 + LABEL_SIDE_M + LABEL_BOX_M[0] / 2.0))

                def build(acc, lab=lab, kind=kind):
                    box(lab, u, LABEL_BOX_M[0], LABEL_BOX_M[1], acc=acc)
                    lettered(LABEL[kind], lab, u, LABEL_TEXT, acc=acc)
                builds.append(build)
            put(*builds)
        for p, q in wings[i]:                           # (the wing-tip clearance lines, ending level with the stop bar)
            add(np.array([p, q]), STAND_KIND, CENTRE_W_M)
        text = str(c.ref or "").upper().replace(" ", "")
        if len(text) <= NUM_MAX and sm.supported(text):  # (the number: black on a yellow box past the stop bars)
            bw, bh = sm.text_width(text, NUM_TEXT) + 2.0 * NUM_MARGIN[0], NUM_TEXT + 2.0 * NUM_MARGIN[1]
            ahead = STOP_BAR_W_M / 2.0 + NUM_AFTER_M + bh / 2.0
            behind = back - STOP_BAR_W_M / 2.0 - NUM_AFTER_M - bh / 2.0
            slide = max(STOP_BAR_M, bw) / 2.0 + bw / 2.0 + NUM_AFTER_M
            builds = []
            for along, across in ((ahead, 0.0), (ahead, -slide), (ahead, slide), (behind, 0.0), (behind, -slide), (behind, slide)):
                at = nose + u * along + left * across   # (past the stop bars: centred, then to a side, then behind the ladder: the first clear)

                def build(acc, at=at):
                    box(at, u, bw, bh, acc=acc)
                    lettered(text, at, u, NUM_TEXT, acc=acc)
                builds.append(build)
            put(*builds)
        # (the equipment restraint line: two legs either side of the forward fuselage, RESTRAINT_OFF_M off its skin and
        # 0.4 of the length back; a U round the whole envelope crossed the next stands' at Brisbane)
        a = RESTRAINT_OFF_M + 0.06 * length
        back = 0.4 * length
        for s in (1.0, -1.0):
            out.append((np.array([nose + left * (s * a), nose + left * (s * a) - u * back]), RESTRAINT_W_M,
                        RESTRAINT_KIND, "red"))
    if site is not None:
        walls, bridges, _blocks = _site_xy(sc, site)
        for pt, d in bridges:                           # (the red hatched box under each jet bridge's cab, at a stand)
            if not any(float(dnp.hypot(*(x[4] - pt))) < BRIDGE_STAND_M for x in stands_):
                continue
            centre = pt + d * BRIDGE_BOX_AT_M
            w, h = BRIDGE_BOX_M
            polys += [(p, "red", "red") for p in sm.hatch(centre, d, w, h, HATCH_PITCH_M, HATCH_W_M)]
            polys += [(p, "red", "red") for p in sm.outline(centre, d, w, h, BOX_LINE_W_M)]
        if walls:
            ribbons, more = _service_roads(items, rows, walls, leads, bridges)
            out += ribbons
            polys += more
    lines, boxes = [], []
    for xy, width, kind, colour in out:
        la, lo, _ = sc.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        la, lo = np.asarray(la), np.asarray(lo)
        lines.append((la, lo, width, kind, colour))
        boxes.append((la.min(), la.max(), lo.min(), lo.max()))
    plist, pboxes = [], []
    for pts, colour, layer, *uv in polys:
        la, lo, _ = sc.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
        la, lo = np.asarray(la), np.asarray(lo)
        plist.append((la, lo, colour, layer, *uv))
        pboxes.append((la.min(), la.max(), lo.min(), lo.max()))
    sc.memo[("net", key)] = (net, stops)
    got = sc.memo[key] = (lines, np.array(boxes, np.float64).reshape(-1, 4), plist, np.array(pboxes, np.float64).reshape(-1, 4))
    return got


# ---- the taxiways' names: ICAO location markings -----------------------------------------------------------------------------

def _name_of(ref):
    """A way's `ref` as its name: upper case, no spaces ("" for none)."""
    return str(ref or "").upper().replace(" ", "")


def _name_text(name):
    """The name as it is painted: itself, or "" for one the font lacks (A-Z, 0-9) or over NAME_MAX characters (a long name is still a
    name: it tells one way from another, but nothing is painted for it)."""
    return name if len(name) <= NAME_MAX and standmarks.supported(name) else ""


def _meets(R, S):
    """[point (2,)] where the polylines R (k, 2) and S (m, 2) meet: a segment of one across a segment of the other, or an end of one
    within NAME_JOIN_M of the other (a way that ends on another)."""
    d, e = (R[1:] - R[:-1])[:, None], (S[1:] - S[:-1])[None]
    w = S[:-1][None] - R[:-1][:, None]
    den = d[..., 0] * e[..., 1] - d[..., 1] * e[..., 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (w[..., 0] * e[..., 1] - w[..., 1] * e[..., 0]) / den
        u = (w[..., 0] * d[..., 1] - w[..., 1] * d[..., 0]) / den
    across = (np.abs(den) > 1e-9) & (t >= 0.0) & (t <= 1.0) & (u >= 0.0) & (u <= 1.0)
    out = [R[i] + d[i, 0] * t[i, j] for i, j in zip(*np.nonzero(across))]
    for X, Y in ((R, S), (S, R)):
        out += [end for end in (X[0], X[-1]) if float(_pt_segs(end, Y[:-1], Y[1:]).min()) <= NAME_JOIN_M]
    return out


def _approaches(runs, sc):
    """[(run index, station of the junction (m along the run), direction +1 / -1 of travel toward it)] of the places a name is
    wanted: on each run that has a name to paint (runs: [(xy, way index, name, text painted)]), every junction with a run of another name
    (named, whether it can be painted or not; _meets: both directions
    along it where it passes through the junction, the one toward it where it ends there; a box that would not fit between the
    run's end and the junction is none, _name_marks) and every runway holding position on the way (Scene.sites), toward the
    runway. Unnamed ways have none and meet no name; two approaches of one run and direction within 3 m are one."""
    named = [i for i, r in enumerate(runs) if r[2]]
    lo = np.array([runs[i][0].min(axis=0) for i in named]).reshape(-1, 2) - NAME_JOIN_M
    hi = np.array([runs[i][0].max(axis=0) for i in named]).reshape(-1, 2) + NAME_JOIN_M
    out = []

    def add(i, s, dr):
        if runs[i][3] and not any(o[0] == i and o[2] == dr and abs(o[1] - s) < 3.0 for o in out):
            out.append((i, s, dr))
    for a, i in enumerate(named):
        for b in np.flatnonzero((lo[a + 1:] <= hi[a]).all(axis=1) & (hi[a + 1:] >= lo[a]).all(axis=1)) + a + 1:
            j = named[int(b)]
            if runs[i][2] == runs[j][2]:
                continue
            pts = []
            for p in _meets(runs[i][0], runs[j][0]):
                if not any(float(dnp.hypot(*(p - q))) < 2.0 for q in pts):
                    pts.append(p)
            for p in pts:
                for k in (i, j):
                    s = _station(runs[k][0], p)
                    add(k, s, 1)
                    add(k, s, -1)
    for _ri, wi, p, u in sc.sites:                      # (the holding positions: on the way's own run, toward the runway)
        best = None
        for k in named:
            if runs[k][1] == wi:
                q, tan, dist = _project(runs[k][0], p)
                if dist <= 3.0 and (best is None or dist < best[0]):
                    best = (dist, k, tan)
        if best is not None:
            add(best[1], _station(runs[best[1]][0], p), 1 if float(dnp.dot(best[2], -u)) > 0.0 else -1)
    return out


def _cut_out(Q, centre, tv, hx, hy):
    """The pieces (k, 2) of the polyline Q (m, 2) outside the rectangle `centre` +- hx across and +- hy along the unit vector tv (touching
    it is not inside), in order, each at least PIECE_MIN_M long (so none is a zero-length sliver for tile_chunks' pieces): [] for a line
    wholly inside."""
    left = np.array([-tv[1], tv[0]])
    d = Q - centre
    x, y = dnp.vecdot(d, left[None]), dnp.vecdot(d, tv[None])
    tx0, tx1 = _slab(x[:-1], x[1:] - x[:-1], -hx, hx)
    ty0, ty1 = _slab(y[:-1], y[1:] - y[:-1], -hy, hy)
    lo, hi = np.maximum(np.maximum(tx0, ty0), 0.0), np.minimum(np.minimum(tx1, ty1), 1.0)
    out, cur = [], []
    for i in range(len(Q) - 1):
        a, b = Q[i], Q[i + 1]
        if not hi[i] > lo[i]:                           # (this segment is outside: it goes on the piece)
            cur = cur or [a]
            cur.append(b)
            continue
        if lo[i] > 0.0:                                 # (it enters the rectangle: the piece ends at the edge)
            cur = cur or [a]
            cur.append(a + (b - a) * float(lo[i]))
        if len(cur) >= 2:
            out.append(np.array(cur))
        cur = [a + (b - a) * float(hi[i]), b] if hi[i] < 1.0 else []       # (it leaves it: a new piece from the edge)
    if len(cur) >= 2:
        out.append(np.array(cur))
    return [p for p in out if float(dnp.hypot(*np.diff(p, axis=0).T).sum()) >= PIECE_MIN_M]


def _cut_lines(sc, lines, boxes, cuts):
    """(lines, boxes) with the parts of each line inside one of the rectangles `cuts` ((centre, unit direction, half width, half height) in
    metres: the name boxes grown by NAME_CUT_M) cut out (_cut_out): a line across one is two, on both sides of it, in its place in the
    list; the others are the same objects. Deterministic: the lines in their order, each cut by the rectangles in theirs."""
    if not cuts or not lines:
        return lines, boxes
    e, n, _ = sc.frame.geodetic_to_enu(np.concatenate([x[0] for x in lines]), np.concatenate([x[1] for x in lines]), np.zeros(sum(len(x[0]) for x in lines)))
    pts = np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], 1)
    cnt = np.array([len(x[0]) for x in lines])
    ends = np.cumsum(cnt)
    lo = np.array([pts[a:b].min(axis=0) for a, b in zip(ends - cnt, ends)])
    hi = np.array([pts[a:b].max(axis=0) for a, b in zip(ends - cnt, ends)])
    hits = {}                                           # (line index -> the rectangles over it)
    for k, (centre, tv, hx, hy) in enumerate(cuts):
        r = np.abs(tv) * hy + np.abs(np.array([-tv[1], tv[0]])) * hx          # (the rectangle's extent along each axis)
        for i in np.flatnonzero((hi >= centre - r).all(axis=1) & (lo <= centre + r).all(axis=1)):
            hits.setdefault(int(i), []).append(k)
    if not hits:
        return lines, boxes
    out, obox = [], []
    for i, ln in enumerate(lines):
        if i not in hits:
            out.append(ln)
            obox.append(boxes[i])
            continue
        pieces = [pts[ends[i] - cnt[i]:ends[i]]]
        for k in hits[i]:
            centre, tv, hx, hy = cuts[k]
            pieces = [q for p in pieces for q in _cut_out(p, centre, tv, hx, hy)]
        for p in pieces:
            la, lo_, _ = sc.frame.enu_to_geodetic(p[:, 0], p[:, 1], np.zeros(len(p)))
            la, lo_ = np.asarray(la), np.asarray(lo_)
            out.append((la, lo_) + tuple(ln[2:]))
            obox.append((la.min(), la.max(), lo_.min(), lo_.max()))
    return out, np.array(obox, np.float64).reshape(-1, 4)


def _name_marks(sc, lines, boxes, polys, stands, site=None):
    """([(lat (k,), lon (k,), colour, layer)], boxes (n, 4), lines, line boxes) of the taxiways' location markings (ICAO: the name of the taxiway the
    pilot is on: a black box, a yellow ring in it, yellow capitals NAME_TEXT tall) in polygons()'s form, memoized on the scene: on every
    named way (one whose `ref` the font can draw: at most NAME_MAX characters of A-Z, 0-9) before each junction with a way of another
    name and before each runway holding position (_approaches), for each direction of travel, the letters upright as the pilot reads them
    looking along the way, the box centred on the way's centreline (its black fill, the "text" material, drawn over the line), its
    centre NAME_BEFORE_M before the junction (the first of them for which all of the following hold):
      * the box grown by NAME_PAVE_M lies on paving (a taxiway's or an apron's pad: the scene's rings; the way's own width too),
      * it is NAME_RUNWAY_M clear of a runway's pad and not under a stand's aircraft or a building,
      * it is NAME_CLEAR_M clear of every other mark (`lines`, `polys`: the other ways' centrelines, lead-ins, bars, boxes, edge lines;
        the way's own centreline, within NAME_OWN_M, runs under it) and of the names already placed,
      and none where the same name is already placed for that direction of travel within NAME_REPEAT_M.
    The approaches are taken in the order of their positions, so every list of cells that holds a place makes the same marks there.
    The lines come back with the parts inside a box grown by NAME_CUT_M cut out (_cut_lines: the centreline and its border run under the
    box on paper, but the paint writes no depth and the engine's order of the translucent materials in a mesh is not ours to rely on: in game
    the yellow showed through the black); the ring and the letters stay."""
    st = [c for c in stands if c.mode in LEAD_MODES]
    key = ("names", stand_key(st), None if site is None else site.key)
    got = sc.memo.get(key)
    if got is not None:
        return got
    runs = [(xy, wi, _name_of(sc.ways[wi][2])) for xy, _cls, wi, _f, _l in _mapped_runs(sc) if len(xy) >= 2]
    runs = [r + (_name_text(r[2]),) for r in runs]       # (xy, way index, name, the text painted: "" for none)
    cands = _approaches(runs, sc) if any(r[3] for r in runs) else []
    placed = []                                         # (name, centre, unit direction of travel, box (4, 2))
    if cands:
        def xy_of(la, lo):
            e, n, _ = sc.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            return np.stack([np.asarray(e), np.asarray(n)], 1)
        # (every other mark: the ribbons as segments with their half widths, the polygons and the footprints as rings with extents)
        sa = sb = np.zeros((0, 2))
        shw = np.zeros(0)
        if lines:
            pts = xy_of(np.concatenate([x[0] for x in lines]), np.concatenate([x[1] for x in lines]))
            cnt = np.array([len(x[0]) for x in lines])
            last = np.cumsum(cnt) - 1
            keep = np.ones(len(pts), bool)
            keep[last] = False                          # (a line's last point starts no segment)
            idx = np.flatnonzero(keep)
            sa, sb = pts[idx], pts[idx + 1]
            shw = np.repeat(np.array([x[2] for x in lines], np.float64) / 2.0, cnt - 1)
        slo, shi = np.minimum(sa, sb), np.maximum(sa, sb)
        rings = [xy_of(x[0], x[1]) for x in polys if len(x[0]) >= 3]
        if site is not None:
            rings += list(_site_xy(sc, site)[2])
        rlo = np.array([r.min(axis=0) for r in rings]).reshape(-1, 2)
        rhi = np.array([r.max(axis=0) for r in rings]).reshape(-1, 2)
        pave = list(sc.memo.get("apron_rings") or ()) + list(sc.memo.get("pad_rings") or ())
        plo = np.array([r.min(axis=0) for r in pave]).reshape(-1, 2)
        phi = np.array([r.max(axis=0) for r in pave]).reshape(-1, 2)
        rsegs = [(np.stack([xy[:-1], xy[1:]], axis=1), half) for xy, half, _ref in sc.runways if len(xy) >= 2]
        envelopes = []                                  # (nose, unit direction, length, span) of the stands' aircraft
        if st:
            e, nn, _ = sc.frame.geodetic_to_enu(np.array([c.lat for c in st]), np.array([c.lon for c in st]), np.zeros(len(st)))
            for c, x, y in zip(st, np.asarray(e, np.float64), np.asarray(nn, np.float64)):
                u = _unit2(c.ne, c.nn)
                envelopes.append((np.array([x, y]) + u * (pa.SPECS[c.kind][0] / 2.0), u, pa.SPECS[c.kind][0], pa.SPECS[c.kind][1]))
        h = NAME_TEXT
        at = [standmarks.at(runs[i][0], min(max(s - dr * NAME_BEFORE_M[0], 0.0), _length(runs[i][0])))[0] for i, s, dr in cands]
        la, lo, _ = sc.frame.enu_to_geodetic(np.array([q[0] for q in at]), np.array([q[1] for q in at]), np.zeros(len(at)))
        for k in sorted(range(len(cands)), key=lambda k: (round(float(la[k]) * 1e6), round(float(lo[k]) * 1e6), runs[cands[k][0]][3], cands[k][2])):
            i, s, dr = cands[k]
            P, wi, _name, text = runs[i]
            total, half = _length(P), sc.ways[wi][1] / 2.0
            bw, bh = standmarks.text_width(text, h) + 2.0 * NAME_MARGIN_M, h + 2.0 * NAME_MARGIN_M
            for before in NAME_BEFORE_M:
                mid = s - dr * before                   # (the box's centre along the run)
                if mid - bh / 2.0 < 0.0 or mid + bh / 2.0 > total:
                    continue
                tan = (standmarks.at(P, mid + bh / 2.0)[0] - standmarks.at(P, mid - bh / 2.0)[0]) * dr
                if float(dnp.hypot(*tan)) < _EPS:
                    continue
                tv = tan / float(dnp.hypot(*tan))
                left = np.array([-tv[1], tv[0]])
                centre = standmarks.at(P, mid)[0]               # (on the centreline: cut out of the box, _cut_lines)
                if any(t == text and float(dnp.dot(d, tv)) > 0.5 and float(dnp.hypot(*(centre - q))) < NAME_REPEAT_M for t, q, d, _b in placed):
                    break                               # (the name is there for this direction already: not here at all)
                box = standmarks.box(centre, tv, bw, bh)
                grown = standmarks.box(centre, tv, bw + 2.0 * NAME_PAVE_M, bh + 2.0 * NAME_PAVE_M)
                probe = np.vstack([grown, (grown + np.roll(grown, -1, axis=0)) / 2.0, centre[None]])
                ok = _pts_dist(probe, np.stack([P[:-1], P[1:]], axis=1)) <= half - NAME_PAVE_M       # (the way's own width ...
                for r in np.flatnonzero((plo <= probe.max(axis=0)).all(axis=1) & (phi >= probe.min(axis=0)).all(axis=1)):    # ... or a pad)
                    if not ok.all():
                        ok[np.flatnonzero(~ok)[polygon.contains_points(pave[r], probe[~ok])]] = True
                if not ok.all():
                    continue
                if any(float(_pts_dist(box, segs).min()) < half_r + NAME_RUNWAY_M for segs, half_r in rsegs):
                    continue
                if envelopes and _under_aircraft(np.vstack([box, box[:1]]), envelopes, NAME_CLEAR_M):
                    continue
                clear = standmarks.box(centre, tv, bw + 2.0 * NAME_CLEAR_M, bh + 2.0 * NAME_CLEAR_M)
                near = np.flatnonzero((shi >= clear.min(axis=0) - shw[:, None]).all(axis=1) &
                                      (slo <= clear.max(axis=0) + shw[:, None]).all(axis=1))
                if len(near):                           # (the way's own centreline and its border run under the box: not paint in the way)
                    segs = np.stack([P[:-1], P[1:]], axis=1)
                    near = near[(_pts_dist(sa[near], segs) > NAME_OWN_M) | (_pts_dist(sb[near], segs) > NAME_OWN_M)]
                if len(near):                           # (a ribbon across the box grown by its half width: in the box's own axes)
                    ac, ec = sa[near] - centre, sb[near] - centre
                    qa = np.stack([dnp.vecdot(ac, -left[None]), dnp.vecdot(ac, tv[None])], 1)
                    qe = np.stack([dnp.vecdot(ec, -left[None]), dnp.vecdot(ec, tv[None])], 1)
                    ex, ey = bw / 2.0 + NAME_CLEAR_M + shw[near], bh / 2.0 + NAME_CLEAR_M + shw[near]
                    tx0, tx1 = _slab(qa[:, 0], qe[:, 0] - qa[:, 0], -ex, ex)
                    ty0, ty1 = _slab(qa[:, 1], qe[:, 1] - qa[:, 1], -ey, ey)
                    if bool((np.maximum(np.maximum(tx0, ty0), 0.0) <= np.minimum(np.minimum(tx1, ty1), 1.0)).any()):
                        continue
                hit = np.flatnonzero((rhi >= clear.min(axis=0)).all(axis=1) & (rlo <= clear.max(axis=0)).all(axis=1))
                if _hits_blocks(clear, [(rings[r], (rlo[r][0], rlo[r][1], rhi[r][0], rhi[r][1])) for r in hit]):
                    continue
                if _hits_blocks(clear, [(b[3], (b[3][:, 0].min(), b[3][:, 1].min(), b[3][:, 0].max(), b[3][:, 1].max())) for b in placed]):
                    continue
                placed.append((text, centre, tv, box))
                break
    plist, pboxes = [], []                              # (the black box in the "under" layer, its yellow ring and the yellow letters over it)
    solid = np.array([standmarks.solid()] * 4)           # (the ring: quads of the font sheet's opaque cell)
    for text, centre, tv, box in placed:
        inset, ring = NAME_RING_M
        w, hh = float(dnp.hypot(*(box[1] - box[0]))), float(dnp.hypot(*(box[3] - box[0])))
        for pts, colour, layer, *uv in ([(box, "text", "glyph")] +
                                        [(q, "font_yellow", "glyph", solid) for q in standmarks.outline(centre, tv, w - 2.0 * inset, hh - 2.0 * inset, ring)] +
                                        [(q, "font_yellow", "glyph", uv) for q, uv in standmarks.text_quads(text, centre, tv, NAME_TEXT)]):
            la, lo, _ = sc.frame.enu_to_geodetic(pts[:, 0], pts[:, 1], np.zeros(len(pts)))
            la, lo = np.asarray(la), np.asarray(lo)
            plist.append((la, lo, colour, layer, *uv))
            pboxes.append((la.min(), la.max(), lo.min(), lo.max()))
    cuts = []                                           # (the boxes grown by NAME_CUT_M: centre, unit direction, half width, half height)
    for _text, centre, tv, box in placed:
        cuts.append((centre, tv, float(dnp.hypot(*(box[1] - box[0]))) / 2.0 + NAME_CUT_M, float(dnp.hypot(*(box[3] - box[0]))) / 2.0 + NAME_CUT_M))
    lines, boxes = _cut_lines(sc, lines, boxes, cuts)
    got = sc.memo[key] = (plist, np.array(pboxes, np.float64).reshape(-1, 4), lines, boxes)
    return got


def _all_marks(feats_list, stands, site=None):
    """(lines, boxes, polys, pboxes): _marks plus the stands' lead-ins and marks (_lead_lines) and the taxiways' names (_name_marks)."""
    lines, boxes = _marks(feats_list)
    sc = scene(feats_list) if len(lines) else None
    if sc is None:
        return lines, boxes, [], np.zeros((0, 4))
    stands = list(stands or ())
    extra, xboxes, polys, pboxes = _lead_lines(sc, stands, site)
    if extra:
        lines, boxes = lines + extra, np.concatenate([boxes, xboxes])
    names, nboxes, lines, boxes = _name_marks(sc, lines, boxes, polys, stands, site)
    if names:
        polys, pboxes = polys + names, np.concatenate([pboxes, nboxes])
    return lines, boxes, polys, pboxes


def _cached(feats, slot, build):
    """build(feats), memoized on the first of feats under `slot` (CACHE_MAX lists, each checked to still hold the same
    live Features)."""
    memo = feats[0].__dict__.setdefault(slot, {})
    key = tuple(id(f) for f in feats)
    got = memo.get(key)
    if got is not None and all(r() is f for r, f in zip(got[0], feats)):
        return got[1]
    if len(memo) >= CACHE_MAX:
        memo.clear()
    value = build(feats)
    memo[key] = ([weakref.ref(f) for f in feats], value)
    return value


def scene(feats_list):
    """The Scene of the airport Features in feats_list (the ways, the runways and the runway holding positions the
    markings are drawn from), memoized on the first Features; None for Features with no airport way in them."""
    feats = [f for f in feats_list if f is not None and len(f)]
    if not any(_raw(f)[0] for f in feats):
        return None
    return _cached(feats, _SCENE, _scene)


class Site:
    """What the stand marks read of the buildings of an airport: walls [(lat (k,), lon (k,))], the outlines of its terminals and
    piers (airportroles), and bridges [(lat, lon, east, north)], the outer end of each mapped jet bridge with its unit
    direction out of the terminal, and blocks [(lat (k,), lon (k,))], the outlines of every footprint (buildings, parts, bridges, canopies:
    nothing is painted under one); key, a hashable summary of them (what the memos of the stands' marks are kept by)."""
    __slots__ = ("walls", "bridges", "blocks", "key")

    def __init__(self, walls, bridges, blocks=()):
        self.walls, self.bridges, self.blocks = walls, bridges, blocks
        self.key = (tuple((len(la), round(float(np.mean(la)), 6), round(float(np.mean(lo)), 6)) for la, lo in walls),
                    tuple((round(float(b[0]), 6), round(float(b[1]), 6)) for b in bridges),
                    tuple((len(la), round(float(np.mean(la)), 6), round(float(np.mean(lo)), 6)) for la, lo in blocks))


def _build_site(infra, blds, parts):
    import airportroles
    roles = airportroles.classify([f for f in blds if f is not None], [f for f in parts if f is not None],
                                  [f for f in infra if f is not None])
    if roles.frame is None or not len(roles.units):
        return None
    fr, (x0, y0) = roles.frame, roles.origin
    walls, blocks = [], []
    for uid, u in roles.units.items():
        if len(u.ring) < 3:
            continue
        la, lo, _ = fr.enu_to_geodetic(u.ring[:, 0] + x0, u.ring[:, 1] + y0, np.zeros(len(u.ring)))
        la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
        blocks.append((la, lo))
        if roles.get(uid) in (airportroles.TERMINAL, airportroles.PIER):
            walls.append((la, lo))
    bridges, seen = [], set()
    for gid in sorted(roles.gates):
        g = roles.gates[gid]
        if g.get("end") is None or g["bridge"] in seen:
            continue
        seen.add(g["bridge"])
        la, lo, _ = fr.enu_to_geodetic(np.array([g["end"][0] + x0]), np.array([g["end"][1] + y0]), np.zeros(1))
        bridges.append((float(la[0]), float(lo[0]), float(g["dir"][0]), float(g["dir"][1])))
    return Site(walls, bridges, blocks) if walls or bridges else None


def site_of(infra, blds, parts):
    """The Site (the walls and the jet bridges' ends) of the airport in the Features of the cells (lists of infrastructure,
    buildings and building_parts Features), memoized on the first; None without a terminal or a bridge."""
    allf = [f for f in list(infra) + list(blds) + list(parts) if f is not None and len(f)]
    if not allf:
        return None
    return _cached(allf, _SITE, lambda _fl: _build_site(infra, blds, parts))


def _simplify(P, tol):
    """The polyline P (k, 2) without the vertices that lie within tol of the line between the kept ones (Douglas-Peucker)."""
    n = len(P)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, d = P[i], P[j] - P[i]
        ln = max(float(dnp.hypot(*d)), _EPS)
        dist = np.abs((P[i + 1:j, 0] - a[0]) * d[1] - (P[i + 1:j, 1] - a[1]) * d[0]) / ln
        k = int(np.argmax(dist))
        if dist[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return P[keep]


def _covered(pts, rings, buffer):
    """Which of pts (k, 2) lie inside one of `rings` [(m, 2)] or within `buffer` of its outline: (k,) booleans."""
    got = np.zeros(len(pts), bool)
    if not len(pts):
        return got
    lo, hi = pts.min(axis=0) - buffer, pts.max(axis=0) + buffer
    for r in rings:
        rlo, rhi = r.min(axis=0), r.max(axis=0)
        if (rhi < lo).any() or (rlo > hi).any():
            continue
        m = np.all((pts >= rlo - buffer) & (pts <= rhi + buffer), axis=1) & ~got
        if not m.any():
            continue
        idx = np.flatnonzero(m)
        inside = polygon.contains_points(r, pts[idx])
        got[idx[inside]] = True
        rest = idx[~inside]
        if len(rest):
            segs = np.stack([r, np.roll(r, -1, axis=0)], axis=1)
            got[rest[_pts_dist(pts[rest], segs) < buffer]] = True
    return got


def _bridge_gaps(keep, step, gap):
    """keep (k,) booleans of the samples round a closed outline with each run of dropped samples shorter than `gap` m (a sample
    `step` m apart) between kept ones kept too: the edge line runs on across a tiny break."""
    n = len(keep)
    if keep.all() or not keep.any():
        return keep
    out = keep.copy()
    first = int(np.flatnonzero(keep)[0])
    order = np.roll(np.arange(n), -first)
    run = []
    for k in order:
        if keep[k]:
            if run and len(run) * step < gap:
                out[run] = True
            run = []
        else:
            run.append(k)
    if run and len(run) * step < gap:
        out[run] = True                                   # (the break that wraps round to the first kept sample)
    return out


def _chain_runs(runs, gap):
    """The polylines `runs` [(k, 2)] with each end joined to the nearest start within `gap` m (a run's end and another's start; its own
    start closes a loop): [(m, 2)]. The nearest pairs first, each end and each start once."""
    n = len(runs)
    if n == 0:
        return []
    starts = np.array([r[0] for r in runs], np.float64)
    ends = np.array([r[-1] for r in runs], np.float64)
    cell = {}
    for j, p in enumerate(starts):
        cell.setdefault((int(p[0] // gap), int(p[1] // gap)), []).append(j)
    pairs = []
    for i, p in enumerate(ends):
        cx, cy = int(p[0] // gap), int(p[1] // gap)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in cell.get((cx + dx, cy + dy), ()):
                    d = float(dnp.hypot(*(p - starts[j])))
                    if d < gap and (i != j or len(runs[i]) >= 3):
                        pairs.append((d, i, j))
    nxt, prv, closed = {}, {}, set()
    for _d, i, j in sorted(pairs):
        if i in nxt or j in prv:
            continue
        head = i
        while head in prv:
            head = prv[head]
        if head == j:                                   # (it would close the chain on itself: a loop)
            closed.add(head)
            nxt[i] = -1
            continue
        nxt[i], prv[j] = j, i
    out = []
    for h in range(n):
        if h in prv:
            continue
        chain, k = [h], h
        while nxt.get(k, -1) >= 0:
            k = nxt[k]
            chain.append(k)
        pts = np.vstack([runs[c] for c in chain])
        out.append(np.vstack([pts, pts[:1]]) if h in closed else pts)
    return out


def _edge_lines(sc, feats_list):
    """[(xy (k, 2), width, kind, colour)] of the taxiways' double yellow edge lines: along the OUTER boundary of the paving, the union of
    the taxiway pads (aeroways.ways: the buffered ways with their fillets), the aprons and the runway pads (the mapped runway and
    stopway lines' and the runway strips), each grown by PAVE_BUFFER_M: the outline of a taxiway pad where it is not inside, or within
    PAVE_BUFFER_M of, another pad (a junction, an apron, a runway: the edge there lies in the paving, whichever pad's), two EDGE_W_M lines EDGE_GAP_M
    apart, the nearer EDGE_IN_M inside the pad's edge. A break shorter than EDGE_JOIN_M is run across, the pieces of one line (two pads'
    outlines) that end within it of each other are one, and a line shorter than EDGE_MIN_M is none."""
    ways = [r for r in aeroways.ways(feats_list) if len(r[0]) >= 4]
    if not ways:
        return []

    def xy(la, lo):
        e, n, _ = sc.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
        return np.stack([np.asarray(e), np.asarray(n)], 1)
    rings = [xy(la, lo) for la, lo in ways]
    rings = [r[:-1] if float(dnp.hypot(*(r[0] - r[-1]))) < 1e-6 else r for r in rings]
    fixed = [xy(la, lo) for la, lo in aeroways.aprons(feats_list) if len(la) >= 3]
    fixed += [xy(la, lo) for la, lo in aeroways.runway_pads(feats_list) if len(la) >= 3]
    lo_hi = lambda rs: (np.array([r.min(axis=0) for r in rs]).reshape(-1, 2), np.array([r.max(axis=0) for r in rs]).reshape(-1, 2))
    wlo, whi = lo_hi(rings)
    flo, fhi = lo_hi(fixed)
    rsegs = [(np.stack([r[:-1], r[1:]], axis=1), half) for r, half, _ref in sc.runways if len(r) >= 2]
    runs, closed_runs = [], []                          # (the kept parts of the outlines, the paving's inside on their left)
    for i, ring in enumerate(rings):
        closed = np.vstack([ring, ring[:1]])
        seg = np.diff(closed, axis=0)
        ln = dnp.hypot(seg[:, 0], seg[:, 1])
        if float(ln.sum()) < EDGE_MIN_M:
            continue
        cnt = np.maximum(1, np.ceil(ln / EDGE_STEP_M).astype(np.int64))
        pts = np.concatenate([closed[j] + seg[j] * (np.arange(c) / c)[:, None] for j, c in enumerate(cnt)])
        lo, hi = pts.min(axis=0) - PAVE_BUFFER_M, pts.max(axis=0) + PAVE_BUFFER_M
        near = [fixed[j] for j in np.flatnonzero(np.all((fhi >= lo) & (flo <= hi), axis=1))]
        near += [rings[j] for j in np.flatnonzero(np.all((whi >= lo) & (wlo <= hi), axis=1)) if j != i]
        drop = _covered(pts, near, PAVE_BUFFER_M)
        for segs, half in rsegs:
            if (segs.min(axis=(0, 1)) - half - 2.0 <= hi).all() and (segs.max(axis=(0, 1)) + half + 2.0 >= lo).all():
                drop |= _pts_dist(pts, segs) < half + EDGE_RUNWAY_M
        keep = _bridge_gaps(~drop, EDGE_STEP_M, EDGE_JOIN_M)
        if keep.sum() < 2:
            continue
        area = float((ring[:, 0] * np.roll(ring[:, 1], -1) - np.roll(ring[:, 0], -1) * ring[:, 1]).sum())
        if keep.all():                                  # (the whole outline: a closed line of its own)
            run = np.vstack([pts, pts[:1]])
            closed_runs.append(run if area > 0.0 else run[::-1])
            continue
        first = int(np.flatnonzero(~keep)[0])
        cur = []
        for k in np.roll(np.arange(len(pts)), -first):
            if keep[k]:
                cur.append(pts[k])
            elif cur:
                runs.append(np.array(cur) if area > 0.0 else np.array(cur)[::-1])      # (counter-clockwise: the inside is on the left)
                cur = []
        if cur:
            runs.append(np.array(cur) if area > 0.0 else np.array(cur)[::-1])
    out = []
    for run in _chain_runs([r for r in runs if len(r) >= 2], EDGE_JOIN_M) + closed_runs:
        if len(run) < 2 or _length(run) < EDGE_MIN_M:
            continue
        run = _simplify(run, EDGE_TOL_M)
        for k in (0, 1):
            out.append((standmarks.offset(run, EDGE_IN_M + k * (EDGE_W_M + EDGE_GAP_M)), EDGE_W_M, EDGE_KIND, COLOR))
    return out


def _apron_rings(sc, feats_list):
    """The aprons' outer rings of the Features in feats_list in the scene's plane: [(k, 2)]."""
    rings = []
    for la, lo in aeroways.aprons(feats_list):
        if len(la) >= 3:
            e, n, _ = sc.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            rings.append(np.stack([np.asarray(e), np.asarray(n)], 1))
    return rings


def _inside_rings(xy, rings):
    """The parts of the polyline xy (k, 2) that lie inside one of the rings (sampled APRON_STEP_M apart, the polyline's own vertices
    kept): [(m, 2)] of at least two points, none for no rings."""
    if not rings:
        return []
    seg = np.diff(xy, axis=0)
    cnt = np.maximum(1, np.ceil(dnp.hypot(seg[:, 0], seg[:, 1]) / APRON_STEP_M).astype(np.int64))
    pts = np.concatenate([xy[j] + seg[j] * (np.arange(c) / c)[:, None] for j, c in enumerate(cnt)] + [xy[-1:]])
    inside = np.zeros(len(pts), bool)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    for r in rings:
        if (r.max(axis=0) < lo).any() or (r.min(axis=0) > hi).any():
            continue
        m = np.all((pts >= r.min(axis=0)) & (pts <= r.max(axis=0)), axis=1) & ~inside
        if m.any():
            inside[np.flatnonzero(m)[polygon.contains_points(r, pts[m])]] = True
    out, cur = [], []
    for p, ok in zip(pts, inside):
        if ok:
            cur.append(p)
        else:
            if len(cur) >= 2:
                out.append(np.array(cur))
            cur = []
    if len(cur) >= 2:
        out.append(np.array(cur))
    return out


def _branches(p, lines, lo, hi, skip, tol=AUDIT_ON_M):
    """[(unit direction leaving p, line index)] of every line but `skip` that passes through p or ends at it (within tol): a line
    through p leaves it both ways (at a vertex, along both sides)."""
    out = []
    for j in np.flatnonzero((lo <= p + tol).all(axis=1) & (hi >= p - tol).all(axis=1)):
        if j == skip:
            continue
        X = lines[j]
        d = X[1:] - X[:-1]
        sl = np.maximum(dnp.hypot(d[:, 0], d[:, 1]), _EPS)
        t = d / sl[:, None]
        for k in np.flatnonzero(_pt_segs(p, X[:-1], X[1:]) <= tol):
            s = float(dnp.vecdot(p - X[k], t[k]))
            if s > tol:                                 # (not at the segment's start: the way back along it)
                out.append((-t[k], int(j)))
            if sl[k] - s > tol:                         # (not at its end: the way on)
                out.append((t[k], int(j)))
    return out


def _ending(lines, j, p, tol=AUDIT_ON_M):
    """Does the line j end (start or finish) at p?"""
    return min(float(dnp.hypot(*(lines[j][0] - p))), float(dnp.hypot(*(lines[j][-1] - p)))) <= tol


def _fillet_ends(lines, radius):
    """`lines` ([polyline (k, 2)]) with the join of each line's end on another line made tangent: where a line ends on another line
    (or its end) at more than AUDIT_JOIN_RAD from along it, and no line of the join runs straight on from it, the line's last
    stretch is replaced by a circular arc (of `radius`; tighter where the line, or the one it joins, is short) that turns it onto the
    other line (the one it turns least onto) and meets it tangentially, a tangent length on from the junction. Where two lines
    only meet end to end (a corner), the second is cut back to where the arc meets it. The ends are taken in order (line, start then
    finish): each sees the changes before it."""
    L = [np.asarray(x, np.float64).copy() for x in lines]
    lo = np.array([x.min(axis=0) for x in L]).reshape(-1, 2)
    hi = np.array([x.max(axis=0) for x in L]).reshape(-1, 2)
    for i in range(len(L)):
        for end in (0, -1):
            X = L[i]
            if len(X) < 2 or float(dnp.hypot(*(X[0] - X[-1]))) < AUDIT_ON_M:
                continue
            p = X[end]
            br = _branches(p, L, lo, hi, i)
            if not br:
                continue
            Xf = X if end == 0 else X[::-1]                  # (leaving p)
            e = Xf[1] - Xf[0]
            mine = e / max(float(dnp.hypot(*e)), _EPS)
            ang = [_angle(mine, d) for d, _j in br]
            if min(ang) <= AUDIT_JOIN_RAD or max(ang) >= dmath.pi - AUDIT_JOIN_RAD:
                continue
            k = max(range(len(br)), key=lambda q: (round(ang[q], 9), -br[q][1]))
            db, j = br[k]
            turn = dmath.pi - ang[k]
            B = L[j]
            lenA, lenB = _length(Xf), _length(B)
            sig = _station(B, p)
            fwd = float(dnp.vecdot(standmarks.at(B, sig + 0.5 if sig + 0.5 <= lenB else sig - 0.5)[1], db)) >= 0.0     # (db's way along B)
            avail = (lenB - sig) if fwd else sig
            tl = min(radius * dmath.tan(turn / 2.0), 0.5 * lenA, 0.5 * avail)
            if tl < 0.5:
                continue
            pairs = [(tl, tl)] + sorted(((x, y) for x in FILLET_STEPS_M for y in FILLET_STEPS_M
                                         if x <= FILLET_CAP * lenA and y <= FILLET_CAP * avail), key=lambda c: (c[0] + c[1], c))
            got = None                  # (the first that is smooth: a round fillet where both lines are straight; further out where the
            for r in (radius,) + FILLET_RADII_M:        # line is already curving into the join)
                for ta, tb in pairs:
                    pa_, da = standmarks.at(Xf, ta)
                    pb_, db_ = standmarks.at(B, sig + tb if fwd else sig - tb)
                    hb = db_ if fwd else -db_
                    plan = _dubins(pa_, -da, pb_, hb, r if (ta, tb) != (tl, tl) else max(tl / dmath.tan(turn / 2.0), 1e-3))
                    if plan is not None and plan[1][3] + plan[1][6] <= _angle(-da, hb) + FILLET_EXTRA_RAD:
                        got = (plan, ta, tb)
                        break
                if got:
                    break
            if got is None:
                continue
            plan, tl, tb = got
            arc = _dubins_pts(plan)
            tail = _subpath(Xf, tl, lenA)
            new = np.vstack([arc[::-1], tail[1:]]) if len(tail) > 1 else arc[::-1]
            L[i] = new if end == 0 else new[::-1]
            if len(br) == 1 and _ending(L, j, p) and tb < lenB - 0.1:                 # (the corner's other line, cut back to the arc's end)
                L[j] = _subpath(B, tb, lenB) if fwd else _subpath(B, 0.0, lenB - tb)
            for q in (i, j):
                lo[q], hi[q] = L[q].min(axis=0), L[q].max(axis=0)
    return L


def _split_ways(ways):
    """[(polyline (k, 2), way index, starts the way, ends the way)] of `ways` ([(k, 2)]) split at each vertex where the way turns
    by more than AUDIT_JOIN_RAD and another way passes (a node they share): a way that touches another way there and goes back
    (a fillet drawn out and back, as one way) is two ways that end on it."""
    P = [np.asarray(x, np.float64) for x in ways]
    lo = np.array([x.min(axis=0) for x in P]).reshape(-1, 2) - JOIN_NODE_M
    hi = np.array([x.max(axis=0) for x in P]).reshape(-1, 2) + JOIN_NODE_M
    out = []
    for i, X in enumerate(P):
        cuts = [0]
        for k in range(1, len(X) - 1):
            d0, d1 = X[k] - X[k - 1], X[k + 1] - X[k]
            if min(float(dnp.hypot(*d0)), float(dnp.hypot(*d1))) < _EPS or _angle(d0, d1) <= AUDIT_JOIN_RAD:
                continue
            near = np.flatnonzero((lo <= X[k]).all(axis=1) & (hi >= X[k]).all(axis=1))
            if any(j != i and len(P[j]) >= 2 and float(_pt_segs(X[k], P[j][:-1], P[j][1:]).min()) <= JOIN_NODE_M for j in near):
                cuts.append(k)
        cuts.append(len(X) - 1)
        for m, (a, b) in enumerate(zip(cuts[:-1], cuts[1:])):
            out.append((X[a:b + 1], i, m == 0, m == len(cuts) - 2))
    return out


def _relax(P, radius):
    """The polyline P (k, 2) with its curves tighter than RELAX_K times `radius` eased (each vertex there, and its neighbours, moved
    towards the middle of its neighbours, over and over: the bend spreads out and its radius grows), none more than RELAX_MAX_M from
    where it was and the ends where they were. Unchanged without such a curve."""
    P = np.asarray(P, np.float64)
    if len(P) < 3:
        return P
    cur, home = P.copy(), P.copy()
    for _ in range(RELAX_ITERS):
        d0, d1 = cur[1:-1] - cur[:-2], cur[2:] - cur[1:-1]
        l0, l1 = dnp.hypot(d0[:, 0], d0[:, 1]), dnp.hypot(d1[:, 0], d1[:, 1])
        ang = np.abs(dnp.arctan2(d0[:, 0] * d1[:, 1] - d0[:, 1] * d1[:, 0], d0[:, 0] * d1[:, 0] + d0[:, 1] * d1[:, 1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            tight = (0.5 * (l0 + l1) / np.maximum(ang, 1e-9) < RELAX_K * radius) & (ang > 0.02)
        if not tight.any():
            break
        mask = tight.copy()
        mask[1:] |= tight[:-1]
        mask[:-1] |= tight[1:]
        mid = 0.25 * (cur[:-2] + cur[2:]) + 0.5 * cur[1:-1]
        new = np.where(mask[:, None], mid, cur[1:-1])
        off = new - home[1:-1]
        far = dnp.hypot(off[:, 0], off[:, 1])
        new = np.where((far > RELAX_MAX_M)[:, None], home[1:-1] + off * (RELAX_MAX_M / np.maximum(far, _EPS))[:, None], new)
        cur[1:-1] = new
    return cur


def _extend_ends(S, pieces, aprons, pads=()):
    """`S` ([polyline (k, 2)]: the smoothed pieces of mapped ways) with each way's end that touches nothing carried on, straight along
    its end, to the first thing it meets: within EXTEND_M the edge of an apron (`aprons`) that the end is inside, or of a taxiway's pad
    (`pads`: the pavement's end), a centreline that stopped short of the edge; or, within AUDIT_GAP_M and anywhere, another line (a
    gap). pieces: _split_ways' (the way's own ends are the ones)."""
    rings = list(aprons) + list(pads)
    out = [x.copy() for x in S]
    lo = np.array([x.min(axis=0) for x in out]).reshape(-1, 2)
    hi = np.array([x.max(axis=0) for x in out]).reshape(-1, 2)
    rlo = np.array([r.min(axis=0) for r in rings]).reshape(-1, 2)
    rhi = np.array([r.max(axis=0) for r in rings]).reshape(-1, 2)

    def hits(p, d, X, reach):
        """The nearest distance t in (0, reach] along p + t d where the polyline X crosses it (inf: none)."""
        a, e = X[:-1], X[1:] - X[:-1]
        den = d[0] * e[:, 1] - d[1] * e[:, 0]
        w = a - p
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (w[:, 0] * e[:, 1] - w[:, 1] * e[:, 0]) / den
            u = (w[:, 0] * d[1] - w[:, 1] * d[0]) / den
        ok = (np.abs(den) > 1e-9) & (t > 0.05) & (t <= reach) & (u >= 0.0) & (u <= 1.0)
        return float(t[ok].min()) if ok.any() else np.inf

    for i, X in enumerate(out):
        if len(X) < 2 or float(dnp.hypot(*(X[0] - X[-1]))) < AUDIT_ON_M:
            continue
        for end, own in ((0, pieces[i][2]), (-1, pieces[i][3])):
            p = X[end]
            if not own:
                continue
            others = [j for j in np.flatnonzero((lo <= p + EXTEND_M).all(axis=1) & (hi >= p - EXTEND_M).all(axis=1)) if j != i]
            if any(float(_pt_segs(p, out[j][:-1], out[j][1:]).min()) <= AUDIT_ON_M for j in others):
                continue                                # (on a line already)
            rnear = np.flatnonzero((rlo <= p + EXTEND_M).all(axis=1) & (rhi >= p - EXTEND_M).all(axis=1))
            if any(float(_pt_segs(p, rings[r][:-1], rings[r][1:]).min()) <= AUDIT_FREE_M for r in rnear):
                continue                                # (at an edge already)
            if not any(r < len(aprons) and len(rings[r]) >= 3 and bool(polygon.contains_points(rings[r], p[None])[0]) for r in rnear):
                rnear = rnear[:0]                       # (not in an apron: only a line it nearly meets)
            e = X[end] - X[1 if end == 0 else -2]
            n = float(dnp.hypot(*e))
            if n < _EPS:
                continue
            d = e / n
            t = min([hits(p, d, rings[r], EXTEND_M) for r in rnear] + [hits(p, d, out[j], AUDIT_GAP_M) for j in others] + [np.inf])
            if np.isfinite(t):
                q = p + d * t
                out[i] = np.vstack([X, q]) if end == -1 else np.vstack([q, X])
                X = out[i]
                lo[i], hi[i] = X.min(axis=0), X.max(axis=0)
    return out


def _smooth_ways(ways, radius=MIN_RADIUS_M, aprons=(), pads=()):
    """[(polyline (k, 2), way index, starts the way, ends the way)] of the polylines `ways` ([(k, 2)]: the mapped centrelines) as they
    are painted: split where a way goes back on another (_split_ways), every corner a circular arc (_round_arcs), the curves still
    tighter than the radius eased (_relax), each end that is on another way (within aeroways.JOIN_TOL_M: a node they share) moved
    onto that way's rounded line, each end that touches nothing carried on to the edge of the apron it is in or of a pad (`aprons`, `pads`:
    _pavement_rings) or the line it nearly meets (_extend_ends), and each join made tangent (_fillet_ends)."""
    pieces = _split_ways(ways)
    P = [x[0] for x in pieces]
    lo = np.array([x.min(axis=0) for x in P]).reshape(-1, 2) - aeroways.JOIN_TOL_M
    hi = np.array([x.max(axis=0) for x in P]).reshape(-1, 2) + aeroways.JOIN_TOL_M
    nodes = []                                      # (piece, end, the other piece): an end on another way
    for i, X in enumerate(P):
        if len(X) < 2 or float(dnp.hypot(*(X[0] - X[-1]))) < _EPS:
            continue
        for end in (0, -1):
            p = X[end]
            near = [(float(_pt_segs(p, P[j][:-1], P[j][1:]).min()), int(j)) for j in np.flatnonzero((lo <= p).all(axis=1) & (hi >= p).all(axis=1))
                    if j != i and len(P[j]) >= 2 and pieces[j][1] != pieces[i][1]]
            near = [c for c in near if c[0] <= aeroways.JOIN_TOL_M]
            if near:
                nodes.append((i, end, min(near)[1]))        # (on the nearest way only)
    S = [_relax(_round_arcs(X, radius), radius) if len(X) >= 3 else X.copy() for X in P]
    for i, end, j in nodes:
        q, _t, d = _project(S[j], P[i][end])
        if d <= 2.0 * aeroways.JOIN_TOL_M + radius:
            S[i][end] = q
    return [(xy,) + tuple(x[1:]) for xy, x in zip(_fillet_ends(_extend_ends(S, pieces, aprons, pads), radius), pieces)]


def _mapped_runs(sc):
    """[(xy (k, 2), class, way index, first, last)] of the mapped ways' centrelines as painted (_smooth_ways: rounded, the joins
    tangent), cut where they enter a runway (RUNWAY_GAP_M clear of its edge); first and last: the run's start / end is the way's
    own. In the scene's plane; kept on the scene: the lead-ins leave these polylines, the ones that are painted."""
    got = sc.memo.get("runs")
    if got is None:
        try:
            if SMOOTH_MAPPED_M <= 0.0:
                raise ValueError("not smoothed")
            pieces = _smooth_ways([w[0] for w in sc.ways], SMOOTH_MAPPED_M, sc.memo.get("apron_rings", ()), sc.memo.get("pad_rings", ()))
        except Exception as ex:                      # (the unsmoothed centrelines are worth more than none)
            if SMOOTH_MAPPED_M > 0.0:
                log.warning("smoothing the taxiways: %s: %s", type(ex).__name__, ex)
            pieces = [(w[0], i, True, True) for i, w in enumerate(sc.ways)]
        blocks = _runway_blocks([x[0] for x in pieces], [(r[0], r[1]) for r in sc.runways], RUNWAY_GAP_M)
        rcl = [np.stack([r[0][:-1], r[0][1:]], axis=1) for r in sc.runways if len(r[0]) >= 2]
        got = sc.memo["runs"] = []
        for (sm, wi, at_first, at_last), blk in zip(pieces, blocks):
            on = [False, False]                         # (an end of the way on a runway's centreline: it runs on to it, not cut short of the edge)
            for k, (end, own) in enumerate(((sm[0], at_first), (sm[-1], at_last))):
                on[k] = bool(own and any(float(_pts_dist(end[None], segs)[0]) <= aeroways.JOIN_TOL_M for segs in rcl))
            if blk and (on[0] or on[1]):
                blk = _open_ends(blk, len(sm) - 1, on[0], on[1])
            for run in (_free_runs(sm, blk) if blk else [sm]):
                first = at_first and bool(float(dnp.hypot(*(run[0] - sm[0]))) < _EPS)
                last = at_last and bool(float(dnp.hypot(*(run[-1] - sm[-1]))) < _EPS)
                if first and on[0]:
                    run = _lead_on(run[::-1], rcl)[::-1]
                if last and on[1]:
                    run = _lead_on(run, rcl)
                got.append((run, sc.ways[wi][3], wi, first, last))
    return got


def _open_ends(blk, nseg, first, last):
    """blk ({segment: [(lo, hi)]}: _runway_blocks') without the blocked intervals that run on to the way's start (first) or end (last):
    the way is not cut where it ends on the runway."""
    blk = {j: list(v) for j, v in blk.items()}
    for at_end, flag in ((True, last), (False, first)):
        if not flag:
            continue
        for j in (range(nseg - 1, -1, -1) if at_end else range(nseg)):
            hit = [iv for iv in blk.get(j, ()) if (iv[1] >= 1.0 - _EPS if at_end else iv[0] <= _EPS)]
            if not hit:
                break
            for iv in hit:
                blk[j].remove(iv)
            if not any((iv[0] <= _EPS if at_end else iv[1] >= 1.0 - _EPS) for iv in hit):
                break
    return {j: v for j, v in blk.items() if v}


def _lead_on(run, rcl):
    """The polyline `run` (k, 2) ending on a runway's centreline (rcl: its segments (m, 2, 2)) with its end the lead-on: at less than
    RUNWAY_LEAD_RAD to the centreline, the last straight (at least RUNWAY_LEAD_MIN_M) replaced by a tangent arc of RUNWAY_LEAD_R_M (a
    smaller radius where it is shorter) that ends along the centreline; else straight to the centreline (the end on it)."""
    seg = np.diff(run, axis=0)
    ln = dnp.hypot(seg[:, 0], seg[:, 1])
    cum = np.r_[0.0, np.cumsum(ln)]
    end = run[-1]
    best = None                                         # (the nearest centreline segment: distance, (a, b))
    for segs in rcl:
        a, ab = segs[:, 0], segs[:, 1] - segs[:, 0]
        k = np.clip(dnp.vecdot(end - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
        dd = dnp.hypot(*(a + k[:, None] * ab - end).T)
        j = int(np.argmin(dd))
        if best is None or float(dd[j]) < best[0]:
            best = (float(dd[j]), segs[j])
    if best is None:
        return run
    r = best[1][1] - best[1][0]
    r = r / max(float(dnp.hypot(*r)), _EPS)
    a, b = best[1]                                      # (the end moved onto the centreline)
    t = float(np.clip(float(dnp.dot(end - a, b - a)) / max(float(dnp.dot(b - a, b - a)), 1e-12), 0.0, 1.0))
    on = a + t * (b - a)
    back = run[cum <= cum[-1] - 3.0]                    # (the direction it arrives in: over the last 3 m)
    p3 = run[-2] if not len(back) else back[-1]
    d = on - p3
    d = d / max(float(dnp.hypot(*d)), _EPS)
    if float(d[0] * r[0] + d[1] * r[1]) < 0.0:
        r = -r
    ang = dmath.atan2(abs(float(d[0] * r[1] - d[1] * r[0])), float(d[0] * r[0] + d[1] * r[1]))
    run = np.vstack([run[:-1], on[None]]) if float(dnp.hypot(*(run[-1] - on))) > 1e-9 else run
    if ang >= RUNWAY_LEAD_RAD or ang < 0.02:
        return run
    line = run[:-1] - on                                # (how far back from the end it is straight: within STRAIGHT_TOL_M of the line)
    off = np.abs(line[:, 0] * d[1] - line[:, 1] * d[0])
    bad = np.flatnonzero(off > STRAIGHT_TOL_M)
    straight = float(dnp.hypot(*(on - run[bad[-1]]))) if len(bad) else float(cum[-1])
    tl = min(RUNWAY_LEAD_R_M * dmath.tan(ang / 2.0), straight)
    if tl < RUNWAY_LEAD_MIN_M:
        return run
    new_cum = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(run, axis=0).T))]
    keep = new_cum <= new_cum[-1] - tl + 1e-9
    head = run[keep]
    arc = _fillet(on, d, r, tl)
    return np.vstack([head, arc]) if float(dnp.hypot(*(head[-1] - arc[0]))) > 1e-6 else np.vstack([head[:-1], arc])


def _build(feats_list):
    """(lines, boxes): lines [(lat (k,), lon (k,), width, kind, color)], each a polyline (a hold marking's stroke is of
    two points), and boxes (n, 4) their lat0, lat1, lon0, lon1 extents."""
    sc = scene(feats_list)
    if sc is None:
        return [], np.zeros((0, 4))
    out = []                                    # (xy (k, 2), width, kind[, colour])
    try:                                        # (the aprons: a yellow line on one has its black border, as the lead-ins do)
        aprons = _apron_rings(sc, feats_list)
    except Exception as ex:
        log.warning("apron outlines: %s: %s", type(ex).__name__, ex)
        aprons = []
    for run, cls, _wi, _f, _l in _mapped_runs(sc):
        # (the black border under the parts on an apron; a taxilane is the aprons' own: its whole length)
        parts = [run] if cls != "taxiway" and len(run) >= 2 else _inside_rings(run, aprons)
        out += [(part, CENTRE_W_M + 2.0 * BORDER_M, UNDER_KIND, "black") for part in parts]
        out.append((run, CENTRE_W_M, CENTRE_KIND))
    for _ri, wi, site, u in sc.sites:
        for x0, y0, x1, y1, lw, kind in _hold_strokes(site, u, sc.ways[wi][1]):
            out.append((np.array([[x0, y0], [x1, y1]]), lw, kind))
    try:                                        # (the taxiways' double edge lines)
        out += [(xy, w, kind) for xy, w, kind, _c in _edge_lines(sc, feats_list)]
    except Exception as ex:                     # (the centrelines are worth more than the edges)
        log.warning("taxiway edge lines: %s: %s", type(ex).__name__, ex)
    lines, boxes = [], []
    for xy, w, kind, *colour in out:
        la, lo, _ = sc.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
        la, lo = np.asarray(la), np.asarray(lo)
        lines.append((la, lo, w, kind, colour[0] if colour else COLOR))
        boxes.append((la.min(), la.max(), lo.min(), lo.max()))
    return lines, np.array(boxes, np.float64).reshape(-1, 4)


def _marks(feats_list):
    """(lines, boxes) of _build for the list, memoized on its first Features (CACHE_MAX lists, each checked to still
    hold the same live Features); ([], empty) for Features with no airport way in them."""
    feats = [f for f in feats_list if f is not None and len(f)]
    if not any(_raw(f)[0] for f in feats):
        return [], np.zeros((0, 4))
    return _cached(feats, _MEMO, _build)


def strokes(feats_list, stands=None, site=None):
    """The markings of the Features in feats_list (module docstring) as two-point strokes (lat0, lon0, lat1, lon1,
    width, kind, color), [] for Features with no airport way in them; with `stands` (parkedaircraft's Craft), their
    lead-ins too."""
    lines = _all_marks(feats_list, stands, site)[0]
    return [(float(la[i]), float(lo[i]), float(la[i + 1]), float(lo[i + 1]), w, kind, color)
            for la, lo, w, kind, color in lines for i in range(len(la) - 1)]


# ---- the network audit: what a driver of the painted centrelines would see wrong ---------------------------------------------

def _angle(a, b):
    """The angle (radians, 0 to pi) between the vectors a and b."""
    return dmath.atan2(abs(float(a[0] * b[1] - a[1] * b[0])), float(a[0] * b[0] + a[1] * b[1]))


def _turns(P):
    """(vertex indices (m,), signed turn at each (m,) radians, station of each (m,) m along P, radius estimate of each (m,) m) of the
    vertices of the polyline P (k, 2) where it turns (a vertex's radius: half its two sides over its turn)."""
    P = np.asarray(P, np.float64)
    d = np.diff(P, axis=0)
    ln = dnp.hypot(d[:, 0], d[:, 1])
    idx = np.flatnonzero(ln > 1e-9)
    d, ln = d[idx], ln[idx]
    cum = np.r_[0.0, np.cumsum(ln)]
    out = []
    for j in range(1, len(d)):
        ang = dmath.atan2(float(d[j - 1][0] * d[j][1] - d[j - 1][1] * d[j][0]), float(d[j - 1][0] * d[j][0] + d[j - 1][1] * d[j][1]))
        if abs(ang) >= 1e-6:
            out.append((int(idx[j]), ang, float(cum[j]), 0.5 * float(ln[j - 1] + ln[j]) / abs(ang)))
    return tuple(np.array(c) for c in zip(*out)) if out else (np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0))


class Audit:
    """What audit found: gaps [(x, y, distance to the line it nearly meets)], opens [(x, y)] (ends in the open, touching nothing),
    kinks [(x, y, degrees)] (a turn along a line, or at a join), sbends [(x, y, radius)]."""
    __slots__ = ("gaps", "opens", "kinks", "sbends")

    def __init__(self):
        self.gaps, self.opens, self.kinks, self.sbends = [], [], [], []

    def counts(self):
        return {"gaps": len(self.gaps), "opens": len(self.opens), "kinks": len(self.kinks), "sbends": len(self.sbends)}

    def __len__(self):
        return len(self.gaps) + len(self.opens) + len(self.kinks) + len(self.sbends)


def _unique_marks(marks, tol=1.0):
    """marks [(x, y, ...)] without any within tol of an earlier one."""
    out = []
    for m in marks:
        if not any(abs(m[0] - o[0]) <= tol and abs(m[1] - o[1]) <= tol for o in out):
            out.append(m)
    return out


def audit(lines, stops=(), edges=(), runways=(), rmin=MIN_RADIUS_M, rmins=None, aprons=None):
    """The Audit of the painted centreline network `lines` ([polyline (k, 2) m]):
    gaps: an end of a line within AUDIT_GAP_M of another line but not on it (AUDIT_ON_M): two lines that nearly meet;
    opens: an end in the open apron (inside one of `aprons`, [ring (m, 2)]; None: every end is) further than that from every other
        line: touching nothing. Neither is reported where the end is a place a line is meant to stop: within AUDIT_FREE_M of one of `stops` ([(x, y)]: stand stop bars, runway holds) or of an
        edge of `edges` ([polyline or ring (m, 2)]: an apron's or a taxiway's), or at a runway (`runways`: [(centreline (m, 2),
        half width)]: its edge and RUNWAY_GAP_M); a closed line (its first point its last) has no ends;
    kinks: along a line, a direction change of over AUDIT_KINK_DEG within AUDIT_WINDOW_M (more where the line's `rmin`, the
        smallest radius of its curves, allows it: a tight arc turns by the window over its radius); and at a join, a line's end
        on another line (or on its end) that leaves it neither along it (within AUDIT_JOIN_DEG) nor straight through it (another
        line of the join opposite to it, the same): a crossing straight through is none;
    sbends: two curves of opposite sense within AUDIT_S_LINK_M of each other, the tighter of radius under rmin.
    rmins: the lines' own rmin (default `rmin` each); aprons: see opens."""
    P = [np.asarray(x, np.float64) for x in lines if x is not None and len(x) >= 2]
    rm = list(rmins) if rmins is not None else [rmin] * len(P)
    res = Audit()
    if not P:
        return res
    a = np.concatenate([x[:-1] for x in P])
    b = np.concatenate([x[1:] for x in P])
    lid = np.concatenate([np.full(len(x) - 1, i) for i, x in enumerate(P)])
    sl = np.maximum(dnp.hypot(*(b - a).T), _EPS)
    tt = (b - a) / sl[:, None]
    marks = [np.asarray(p, np.float64) for p in stops]
    rings = [np.asarray(e, np.float64) for e in edges if len(e) >= 2]
    rways = [(np.asarray(r, np.float64), float(h)) for r, h in runways if len(r) >= 2]

    def is_free(p):
        if any(float(dnp.hypot(*(p - q))) <= AUDIT_FREE_M for q in marks):
            return True
        for ring in rings:
            if (p >= ring.min(axis=0) - AUDIT_FREE_M).all() and (p <= ring.max(axis=0) + AUDIT_FREE_M).all():
                if float(_pt_segs(p, ring[:-1], ring[1:]).min()) <= AUDIT_FREE_M:
                    return True
        return any(float(_pt_segs(p, r[:-1], r[1:]).min()) <= h + RUNWAY_GAP_M + AUDIT_FREE_M for r, h in rways)

    def in_apron(p):
        return aprons is None or any((p >= r.min(axis=0)).all() and (p <= r.max(axis=0)).all()
                                     and bool(polygon.contains_points(r, p[None])[0]) for r in aprons if len(r) >= 3)

    def leaving(p, skip):
        """The unit directions leaving p along the lines other than `skip` that pass through or end at p."""
        out = []
        for k in np.flatnonzero((_pt_segs(p, a, b) <= AUDIT_ON_M) & (lid != skip)):
            s = float(dnp.vecdot(p - a[k], tt[k]))
            if s > AUDIT_ON_M:                      # (not at the segment's start: the way back along it)
                out.append(-tt[k])
            if sl[k] - s > AUDIT_ON_M:              # (not at its end: the way on)
                out.append(tt[k])
        return out

    gaps, opens, kinks = [], [], []
    for i, x in enumerate(P):
        if len(x) > 2 and float(dnp.hypot(*(x[0] - x[-1]))) < AUDIT_ON_M:
            ends = ()
        else:
            ends = (0, -1)
        for end in ends:
            p = x[end]
            other = lid != i
            near = float(_pt_segs(p, a[other], b[other]).min()) if other.any() else np.inf
            if near <= AUDIT_ON_M:
                mine = (x[1] - x[0]) if end == 0 else (x[-2] - x[-1])
                mine = mine / max(float(dnp.hypot(*mine)), _EPS)
                dirs = leaving(p, i)
                if not dirs:
                    continue
                ang = [_angle(mine, d) for d in dirs]
                dev = min(min(g, dmath.pi - g) for g in ang)
                if dev > AUDIT_JOIN_RAD and max(ang) < dmath.pi - AUDIT_JOIN_RAD:
                    kinks.append((float(p[0]), float(p[1]), dmath.degrees(dev)))
            elif not is_free(p):
                if near <= AUDIT_GAP_M:
                    gaps.append((float(p[0]), float(p[1]), near))
                elif in_apron(p):
                    opens.append((float(p[0]), float(p[1])))
        idx, tn, st, rd = _turns(x)
        if not len(idx):
            continue
        allowed = max(AUDIT_KINK_RAD, AUDIT_WINDOW_M / max(rm[i], _EPS) * 1.05)
        until = -1.0
        for j in range(len(idx)):                   # (turns along the line: the direction change within the window)
            if st[j] < until:
                continue
            tot = abs(float(tn[(st >= st[j] - 1e-6) & (st < st[j] + AUDIT_WINDOW_M)].sum()))
            if tot > allowed + 1e-6:
                kinks.append((float(x[idx[j]][0]), float(x[idx[j]][1]), dmath.degrees(tot)))
                until = st[j] + AUDIT_WINDOW_M
        runs, cur = [], [0]                         # (the curves: runs of turns the same way; the S of two the other way)
        for j in range(1, len(idx)):
            if tn[j] * tn[cur[-1]] > 0.0 and st[j] - st[cur[-1]] <= 3.0 * AUDIT_WINDOW_M:
                cur.append(j)
            else:
                runs.append(cur)
                cur = [j]
        runs.append(cur)
        for u, v in zip(runs, runs[1:]):
            if tn[u[0]] * tn[v[0]] < 0.0 and st[v[0]] - st[u[-1]] <= AUDIT_S_LINK_M:
                tight = min(float(rd[u].min()), float(rd[v].min()))
                if tight < rm[i] - 0.5:
                    res.sbends.append((float(x[idx[v[0]]][0]), float(x[idx[v[0]]][1]), tight))
                    break
    res.gaps = _unique_marks(sorted(gaps, key=lambda m: m[2]))
    res.opens = _unique_marks(opens)
    res.kinks = _unique_marks(sorted(kinks, key=lambda m: -m[2]))
    return res


def final_straight(P, tol=0.01):
    """The length (m) of the straight run that ends the polyline P (k, 2): back from its last point to the first vertex that turns
    by more than tol radians (an arc's chord turns by ARC_STEP_RAD / 2 at the most at its end: the arc is not the straight)."""
    P = np.asarray(P, np.float64)
    d = np.diff(P, axis=0)
    ln = dnp.hypot(d[:, 0], d[:, 1])
    total = 0.0
    for j in range(len(d) - 1, -1, -1):
        total += float(ln[j])
        if j and ln[j - 1] > 1e-9 and _angle(d[j - 1], d[j]) > tol:
            break
    return total


class Network:
    """The painted yellow centreline network of the Features and stands, in the scene's plane: lines [(xy (k, 2), role, rmin)] (role
    "mapped", "spine", "connector" or "lead"), stops [(nose (2,), unit direction (2,), kind)] (the stands' stop bars), holds [(2,)]
    (the runway holding positions), edges [(k, 2)] (the aprons' and taxiways' outlines), runways [(centreline, half width)], short
    [(nose, final straight, least straight)] (the lead-ins whose final straight is shorter than lead_straight asks), frame."""
    __slots__ = ("frame", "lines", "stops", "holds", "edges", "aprons", "runways", "short")

    def audit(self):
        return audit([x[0] for x in self.lines], [s[0] for s in self.stops] + list(self.holds), self.edges, self.runways,
                     rmins=[x[2] for x in self.lines], aprons=self.aprons)


def network(feats_list, stands=None, site=None):
    """The Network of the markings of the Features in feats_list and (with `stands`) their lead-ins: what _build and _lead_lines paint
    as the yellow centreline, before it is cut by tiles; None for Features with no airport way in them."""
    feats = [f for f in feats_list if f is not None and len(f)]
    sc = scene(feats)
    if sc is None:
        return None
    nw = Network()
    nw.frame = sc.frame
    nw.lines = [(x[0], "mapped", MIN_RADIUS_M) for x in _mapped_runs(sc)]
    nw.stops, nw.short = [], []
    nw.holds = [np.asarray(x[2], np.float64) for x in sc.sites]
    nw.runways = [(xy, half) for xy, half, _ref in sc.runways]
    nw.edges = sc.memo.get("apron_rings", []) + sc.memo.get("pad_rings", [])
    nw.aprons = sc.memo.get("apron_rings", [])
    st = [c for c in (stands or ()) if c.mode in LEAD_MODES]
    if st:
        _lead_lines(sc, stands, site)
        got = sc.memo.get(("net", ("lead", stand_key(st), None if site is None else site.key)))
        if got is not None:
            net, stops = got
            nw.lines += net
            nw.stops = stops
            for nose, u, kind in stops:
                need = lead_straight(pa.SPECS[kind][0])
                for xy, role, _rm in net:
                    if role == "lead" and float(dnp.hypot(*(xy[-1] - nose))) < 0.05:
                        f = final_straight(xy)
                        if f < need - 0.05:
                            nw.short.append((nose, f, need))
    return nw


def _inside(ex, ey, box):
    """The parts of the polyline ex, ey inside the box (x0, y0, x1, y1), clipped to it (Liang-Barsky on each segment, the
    box's edges included): [(x (k,), y (k,))], a polyline that leaves and enters again in several parts; consecutive
    segments that both continue across a vertex stay one."""
    x0, y0, x1, y1 = box[:4]
    if len(ex) < 2:
        return []
    ax, ay = ex[:-1], ey[:-1]
    dx, dy = np.diff(ex), np.diff(ey)
    tx0, tx1 = _slab(ax, dx, x0, x1)
    ty0, ty1 = _slab(ay, dy, y0, y1)
    t0, t1 = np.maximum(np.maximum(tx0, ty0), 0.0), np.minimum(np.minimum(tx1, ty1), 1.0)
    out, cur, last, end = [], None, -2, 0.0
    for i in np.flatnonzero(t0 <= t1):
        a, b = float(t0[i]), float(t1[i])
        p, q = (float(ax[i] + dx[i] * a), float(ay[i] + dy[i] * a)), (float(ax[i] + dx[i] * b), float(ay[i] + dy[i] * b))
        if cur is not None and last == i - 1 and a <= 0.0 and end >= 1.0:           # (it did not leave: the same part)
            cur[0].append(q[0])
            cur[1].append(q[1])
        else:
            if cur is not None:
                out.append(cur)
            cur = ([p[0], q[0]], [p[1], q[1]])
        last, end = int(i), b
    if cur is not None:
        out.append(cur)
    return [(np.array(px, np.float64), np.array(py, np.float64)) for px, py in out]


def _pieces(ex, ey, box):
    """The pieces (x (k,), y (k,)) of the polyline ex, ey inside the tile box (clipped to it): each part cut into
    MARK_PIECE_M at most, nodes MARK_NODE_M apart and at the polyline's own vertices (so it follows its bends)."""
    out = []
    for px, py in _inside(np.asarray(ex, np.float64), np.asarray(ey, np.float64), box):
        s = np.r_[0.0, np.cumsum(dnp.hypot(np.diff(px), np.diff(py)))]
        total = float(s[-1])
        if total < PIECE_MIN_M:
            continue
        n = max(1, int(np.ceil(total / airportgrade.MARK_PIECE_M)))
        for i in range(n):
            a, b = total * i / n, total * (i + 1) / n
            k = max(1, int(np.ceil((b - a) / airportgrade.MARK_NODE_M)))
            at = np.unique(np.r_[np.linspace(a, b, k + 1), s[(s > a + 0.05) & (s < b - 0.05)]])
            at = at[np.r_[True, np.diff(at) > 0.05]]
            if len(at) < 2:
                continue
            out.append((np.interp(at, s, px), np.interp(at, s, py)))
    return out


def tile_chunks_md(md, keys, frame, tx, ty, elev, tile_bbox):
    """tile_chunks of the infrastructure Features of the map-data cells `keys` (mapdata.MapData md; the tile's cells
    with REACH_M round it, roads.cells_of_tile), with the lead-ins of the stands in them (parkedaircraft.lead_stands:
    the cells' buildings and building parts decide where an aircraft stands) and the stand marks that read the terminals
    (the service roads, the red boxes under the jet bridges: site_of)."""
    infra = lambda k: md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
    feats = [infra(k) for k in keys]
    stands, site = [], None
    if any(pa.has_stands(f) for f in feats):
        try:
            bl = pa.building_layers_fn(md)
            stands = pa.lead_stands(keys, infra, bl)
        except Exception as ex:                 # (the centrelines are worth more than the lead-ins)
            log.warning("stand lead-ins: %s: %s", type(ex).__name__, ex)
        if stands:
            try:
                got = [bl(k) for k in keys]
                site = site_of(feats, [g[0] for g in got], [g[1] for g in got])
            except Exception as ex:             # (the stands' marks are worth more than the roads and the red boxes)
                log.warning("stand marks' site: %s: %s", type(ex).__name__, ex)
    return tile_chunks(feats, frame, tx, ty, elev, tile_bbox, stands, site)


# the layers a mark is drawn in (roadmesh.LIFT: the height over the road, so what is painted over what does not rest on the order
# of the mesh): the black borders first, the red marks, the yellow and white paint, the black lettering last
LAYER_OF = {"under": "edge", "red": "gutter", "paint": "apaint", "glyph": "glyph"}   # (roadmesh.LIFT: 1, 1.5, 3, 4.5 mm)
LAYER_ORDER = ("under", "red", "paint", "glyph")     # (the order of the chunks: the road mesh draws a material's triangles in one primitive, in
                                                     # the order the materials first appear: the lettering's (font) after the yellow's)
RIBBON_LAYER = {"black": "under", "red": "red"}


def _quads(poly):
    """The 4-corner quads of a convex polygon (k, 2), k >= 3: itself for 4, else a fan of triangles each as a quad (a triangle's
    third side halved: no zero-area triangle)."""
    p = np.asarray(poly, np.float64)
    if len(p) == 4:
        return [p]
    return [np.array([p[0], p[i], (p[i] + p[i + 1]) / 2.0, p[i + 1]]) for i in range(1, len(p) - 1)]


def _strip(px, py, width):
    """The quads (4, 2) of the polyline px, py as a strip `width` wide: a rectangle for each piece of at most EDGE_QUAD_M of each
    segment (the road mesh drapes a quad by its corners only: short ones follow the ground)."""
    out = []
    for i in range(len(px) - 1):
        a, b = np.array([px[i], py[i]]), np.array([px[i + 1], py[i + 1]])
        ln = float(dnp.hypot(*(b - a)))
        if ln < 1e-6:
            continue
        nv = np.array([-(b[1] - a[1]), b[0] - a[0]]) / ln * (width / 2.0)
        n = max(1, int(np.ceil(ln / EDGE_QUAD_M)))
        for k in range(n):
            p, q = a + (b - a) * (k / n), a + (b - a) * ((k + 1) / n)
            out.append(np.array([p - nv, q - nv, q + nv, p + nv]))
    return out


def polygons(feats_list, stands=None, site=None):
    """The stands' filled marks of the Features in feats_list as [(lat (k,), lon (k,), colour, layer)] (convex polygons: the
    boxes, bars, lettering, hatching, dashes and triangles; layer "under", "red", "paint" or "glyph": _lead_lines); [] without
    stands."""
    return _all_marks(feats_list, stands, site)[2]


def tile_chunks(feats_list, frame, tx, ty, elev, tile_bbox, stands=None, site=None):
    """The taxiway markings (strokes, polygons) inside road tile (tx, ty) of `frame` as road chunks, as
    AirportGrader.tile_chunks does the runways': the parts of each line inside the tile's box, clipped to it, in pieces of
    MARK_PIECE_M at most ({"m", "w", "tl", "p", "n": [[x, y, z], ...]}: nodes MARK_NODE_M apart, z from elev (the graded ground);
    a black line is in the road mesh's "edge" layer, a red one in "gutter": under the yellow); the polygons that meet the box
    (cut to it by the road mesh) as ONE chunk of quads for each colour and layer ({"m", "w": 0, "tl", "p", "layer", "poly",
    "uvq", "quads": 1, "n": []}: roadmesh's filled polygons, the height at the corners). feats_list: the infrastructure
    Features of the cells around the tile (REACH_M); stands: the Craft of those cells (their lead-ins and marks are painted too);
    site: their terminals' walls and bridges (Site). tile_bbox(tx, ty) -> (x0, y0, x1, y1)."""
    lines, boxes, polys, pboxes = _all_marks(feats_list, stands, site)
    if not lines and not polys:
        return []
    box = tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    pad = airportgrade.MARK_PIECE_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - pad, x1 + pad, x0 - pad, x1 + pad]),
                                      np.array([y0 - pad, y0 - pad, y1 + pad, y1 + pad]), np.zeros(4))
    inside = lambda b: np.flatnonzero((b[:, 0] <= np.max(la)) & (b[:, 1] >= np.min(la)) &
                                      (b[:, 2] <= np.max(lo)) & (b[:, 3] >= np.min(lo)))
    near = inside(boxes) if len(lines) else []
    out = []
    if len(near):
        lat = np.concatenate([lines[i][0] for i in near])
        lon = np.concatenate([lines[i][1] for i in near])
        ex, ny, _ = frame.geodetic_to_enu(lat, lon, np.zeros(len(lat)))
        ex, ny = np.asarray(ex), np.asarray(ny)
        pieces, at = [], 0
        edge_quads = []                                 # (the taxiway edge lines: quads, a chunk a tile, not a chunk a piece)
        for i in near:
            m = len(lines[i][0])
            for px, py in _pieces(ex[at:at + m], ny[at:at + m], box):
                if lines[i][3] == EDGE_KIND:
                    edge_quads += _strip(px, py, lines[i][2])
                else:
                    pieces.append((px, py, lines[i]))
            at += m
        if pieces:
            xs = np.concatenate([p[0] for p in pieces])
            ys = np.concatenate([p[1] for p in pieces])
            nla, nlo, _ = frame.enu_to_geodetic(xs, ys, np.zeros_like(xs))
            _, _, zs = frame.geodetic_to_enu(nla, nlo, np.asarray(elev(nla, nlo), np.float64))
            i = 0
            for px, py, line in pieces:
                m = len(px)
                look = airportgrade.MARK_LOOK.get(line[4], airportgrade.MARK_LOOK["white"])
                c = {"m": look["m"], "w": round(float(line[2]), 2), "tl": look["tl"], "p": look["p"],
                     "n": [[round(float(x), 3), round(float(y), 3), round(float(z), 3)]
                           for x, y, z in zip(px, py, zs[i:i + m])]}
                c["layer"] = LAYER_OF[RIBBON_LAYER.get(line[4], "paint")]     # (the yellow 2 mm over its black border)
                out.append(c)
                i += m
    pnear = inside(pboxes) if len(polys) else []
    groups = {("yellow", "paint"): list(edge_quads)} if len(near) and edge_quads else {}
    uvs = {}
    if len(pnear) or groups:
        lat = np.concatenate([polys[i][0] for i in pnear] or [np.zeros(0)])
        lon = np.concatenate([polys[i][1] for i in pnear] or [np.zeros(0)])
        if len(lat):
            ex, ny, _ = frame.geodetic_to_enu(lat, lon, np.zeros(len(lat)))
            ex, ny = np.asarray(ex), np.asarray(ny)
        else:
            ex = ny = lat
        at = 0
        for i in pnear:
            m = len(polys[i][0])
            p = np.stack([ex[at:at + m], ny[at:at + m]], 1)
            at += m
            if (p[:, 0].max() < x0 or p[:, 0].min() > x1 or p[:, 1].max() < y0 or p[:, 1].min() > y1) or m < 3:
                continue
            groups.setdefault((polys[i][2], polys[i][3]), []).extend(_quads(p))
            if len(polys[i]) > 4:                       # (a character: its quad's UVs into the font sheet, one per corner)
                uvs.setdefault((polys[i][2], polys[i][3]), []).append(np.asarray(polys[i][4], np.float64))
        for (colour, layer), quads in sorted(groups.items(), key=lambda kv: (LAYER_ORDER.index(kv[0][1]), kv[0][0])):
            look = airportgrade.MARK_LOOK.get(colour, airportgrade.MARK_LOOK["white"])
            pts = np.concatenate(quads)
            out.append({"m": look["m"], "w": 0.0, "tl": look["tl"], "p": look["p"], "layer": LAYER_OF[layer],
                        "poly": [[round(float(x), 3), round(float(y), 3)] for x, y in pts],
                        "uvq": [[round(float(u), 6), round(float(v), 6)] for u, v in np.concatenate(uvs[(colour, layer)])]
                        if (colour, layer) in uvs else [[0.5, 0.5]] * len(pts), "quads": 1, "n": []})
    return out
