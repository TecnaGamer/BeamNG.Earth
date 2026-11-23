"""Railway catenary: masts with cantilevers and the wires between them along electrified lines (the user, 2026-11-05:
"the line is electrified (25 kV) and there are no masts or wires").

OpenStreetMap says which lines are electrified (railtags: attrs osm_electrified, contact_line / yes) and, where it is
mapped (Germany, parts of Czechia), where the masts are (power=catenary_mast nodes, feats._rail_nodes). Elsewhere the
masts stand along the line at the spacing a catenary's span allows: 60 m on a straight, shorter on a curve (the
contact wire's pull-off from the track's axis must stay within its pantograph's reach), alternating nothing, from the
line's start so that every tile finds the same masts. A mast stands 3.45 m from the axis on the side with room (not
between two tracks: the line's outer side), off the roads; its cantilever (tools/blender/build_catenary.py,
trees.lua's catmast) holds the contact wire 5.75 m over the rail head and the messenger wire above it, and the droppers
hang between them. Every span is drawn by the tile that holds its mast.

Opt-in: BNE_CATENARY=1 (railxing's BNE_RAIL_XING is the crossings'). Not done: masts on bridges."""
import os

import numpy as np
import fast

ENABLED = os.environ.get("BNE_CATENARY", "") not in ("", "0", "off", "false")

GROUP = "catmast"
MAST_H = 9.0                    # m (the model's top over its origin)
MAST_OFF_M = 3.45               # m from the track's axis to the mast's axis
SPAN_M = 60.0                   # m between masts on a straight
SPAN_MIN_M = 30.0
CURVE_K = 0.4                   # m: the pull-off allowed on a curve, a span's sagitta: span = sqrt(8 R k) ...
SPAN_MAX_M = 62.0               # ... at most this
ROAD_CLEAR_M = 2.0              # m a mast keeps off a carriageway's edge
BUILDING_CLEAR_M = 1.5          # m a mast keeps off a building's outline (it stood in a hut's wall, Kalynivka)
BETWEEN_M = 3.0                 # m a mast keeps off another track's axis (not between two tracks)
SHIFT_M = (0.0, 3.0, -3.0, 6.0, -6.0, 9.0, -9.0)    # m along the track a mast moves off a road or a track
MAPPED_NEAR_M = 8.0             # m from a line a mapped mast node belongs to it
MAPPED_GAP_M = 100.0            # m: mapped masts this close along a line are its masts, else the rule's
REACH_M = 130.0                 # m round the tile the lines are read (the next mast of a span that ends outside it)
# the model's attach points over its origin (tools/blender/build_catenary.py): the registration arm's tip, the
# messenger's insulator, and how far out
ARM_OUT_M = 3.45
Z_CONTACT = 6.18
Z_MESSENGER = 7.80
RAIL_TOP_M = 0.58               # the rail head over the bed line (railways.RAIL_TOP_M's ballast + sleeper + rail)
MAST_Z_BAND = (-0.5, 0.2)       # the mast's base over the track's bed at most this far off it
MESSENGER_SAG_M = 1.12          # at mid-span: leaves 0.5 m between the messenger and the contact wire there (Z_MESSENGER - Z_CONTACT = 1.62)
DROPPER_EVERY_M = 6.0           # m between droppers (the user, 2026-11-10: a metre, then 3 m, then "6 m"; real lines: 4-6 m)
DROPPER_FIRST_M = 3.0           # m from a mast to its first dropper
DROPPER_MIN_GAP_M = 0.12        # a dropper shorter than this is not drawn
CONTACT_R, MESSENGER_R, DROPPER_R = 0.016, 0.013, 0.010
JOINT_M = 1.5                   # m: part ends this close are one line (a joint of two OSM ways, a switch's branch)
LINK_MAX_M = 150.0              # m: the longest span a joint's link may be
# the style of a country's masts: "su" the concrete spun mast (ex-USSR, and where concrete is the usual: Poland,
# Czechia, Slovakia, Hungary, Romania, the Baltics), "eu" the steel H-beam mast (Germany and the rest: DB uses both,
# Bane NOR steel; the others' norms were not found: a guess, agreed with the user 2026-11-05). STYLE_GROUPS: mast, post.
SU_COUNTRIES = frozenset("AM AZ BY EE GE KG KZ LT LV MD RU TJ TM UA UZ PL CZ SK HU RO".split())
STYLE_GROUPS = {"su": ("catmast", "catpost"), "eu": ("catmast_eu", "catpost_eu")}
PORTAL_MIN_TRACKS = 3           # a rigid portal (a truss girder on two posts) where this many tracks run side by side
PORTAL_BAND_M = 24.0            # m across: tracks this close and parallel are one corridor
PORTAL_COS = 0.9
PORTAL_MAX_W_M = 60.0           # a wider corridor (a yard's fan) gets none
PORTAL_POST_OFF_M = 3.4         # m from the outer tracks' axes to the posts
PORTAL_MERGE_M = 14.0          # m: a portal this close along the line to one already placed is merged into it (two overlapping girders, Vinnytsia)
PORTAL_POST_CLEAR_M = 2.3       # m a post keeps off every track's axis (a post stood in the track bed beside a siding, Vinnytsia)
PORTAL_Z = 8.0                  # m: the truss's lower chord over the tracks' bed (the contact wires hang from it)
PORTAL_H = 1.0                  # m the truss girder is deep
PORTAL_PANEL_M = 1.0            # m of a diagonal's panel
POST_H = 10.4                   # m (the post models' height)
CHORD_R, DIAG_R = 0.05, 0.025
HOLDER_INS_M = 0.4             # m a rod insulator is long (two of them with a gap hold a contact wire to the girder)
INS_R, CLAMP_R = 0.035, 0.03   # their half widths
HANGER_R = 0.022               # the holder's hanger rod (a 2 cm ribbon vanished at a distance and left the insulators floating)
PORTAL_VERT_M = 2.0            # m between the girder's verticals (no diagonals: the user, 2026-11-10, "remove the triangles")   # the girder's bars' half widths (drawn as the tile's wire ribbons)
ELECTRIC = {"contact_line", "yes"}     # (not "rail": a third rail has no overhead line)
MIN_SPAN_DRAWN_M = 0.3         # (a portal and a mast may stand close: the wire must still join them)
MAX_SPAN_DRAWN_M = 200.0


def electrified(cells):
    """{fid: True} of the lines OSM says have a contact line (railtags' osm_electrified)."""
    fast.require(584, "catenary_electrified")
    return fast._ef.catenary_electrified(cells)


def span_at(xy, arc, s):
    fast.require(584, "catenary_span")
    return fast._ef.catenary_span(xy, arc, s)


def corridor(parts, arcs, i, p, u):
    fast.require(584, "catenary_corridor")
    return fast._ef.catenary_corridor(parts, arcs, i, p, u)


def span_wires(a, b):
    fast.require(584, "catenary_span_wires")
    return fast._ef.catenary_span_wires(a, b)


def stations(xy, key):
    fast.require(584, "catenary_stations")
    return fast._ef.catenary_stations(np.asarray(xy, np.float64), str(key))


def mapped_masts(frame, cells):
    """[(x, y)] of OSM's catenary mast / portal nodes of the cells in the frame."""
    fast.require(584, "catenary_mapped")
    return fast._ef.catenary_mapped(frame, cells)


def near_ring(p, rings, m=BUILDING_CLEAR_M):
    fast.require(584, "catenary_near_ring")
    return fast._ef.catenary_near_ring(p, rings, m)


def style_of(country):
    """"su" or "eu" for an ISO alpha-2 country (None: "eu")."""
    fast.require(584, "catenary_style")
    return fast._ef.catenary_style(country, SU_COUNTRIES)


def _overlaps(new, placed):
    fast.require(584, "catenary_overlaps")
    return fast._ef.catenary_overlaps(new, placed)


def masts_of(frame, cells, box, ground, rings=()):
    """The masts of the electrified lines round box: [dict(fid, xy, s, side, dir, z, base)] one per mast of a line,
    with 'tip' the contact wire's attach point and 'axis' the track's axis point. Not clipped to the tile: the spans'
    ends are needed (rows_and_wires picks the tile's)."""
    fast.require(584, "catenary_layout")
    return fast._ef.catenary_layout(frame, cells, box, ground, rings)


def portal_wires(pt):
    fast.require(584, "catenary_portal_wires")
    return fast._ef.catenary_portal_wires(pt)


MAT_COPPER, MAT_BRONZE, MAT_STEEL, MAT_INSULATOR = "bne_cat_wire_copper", "bne_cat_wire_bronze", "bne_cat_wire_steel", "bne_cat_wire_ins"


def wire_material(r, default="bne_wire"):
    """The material of a wire ribbon by its half width (the catenary's classes are told apart by it): the contact wire
    copper, the messenger and droppers bronze, the girder's bars galvanised steel, insulators dark porcelain; any other
    (a power line's) `default`."""
    if r == CONTACT_R:
        return MAT_COPPER
    if r in (MESSENGER_R, DROPPER_R):
        return MAT_BRONZE
    if r in (CHORD_R, DIAG_R, CHORD_R * 1.6, CLAMP_R, HANGER_R):
        return MAT_STEEL
    if r == INS_R:
        return MAT_INSULATOR
    return default


def wire_solid(r):
    """Whether a wire ribbon of half width r is drawn as a four-sided bar: the girder's bars, the insulators and clamps
    (the thin wires stay ribbons)."""
    return r in (CHORD_R, DIAG_R, CHORD_R * 1.6, CLAMP_R, INS_R, HANGER_R)


def rows_and_wires(masts, box_owns, style="su"):
    fast.require(584, "catenary_build")
    return fast._ef.catenary_build(masts, box_owns, STYLE_GROUPS[style])


def build(frame, tx, ty, cells, ground, rings=(), country=None):
    """(prop rows, wire lines) of road tile (tx, ty): the catenary of the electrified lines round it. rings: the
    buildings' footprints (anchor frame) the masts keep off."""
    import roads as roadsmod
    box = roadsmod.tile_bbox(tx, ty)
    masts = masts_of(frame, cells, box, ground, rings)
    return rows_and_wires(masts, lambda x, y: roadsmod.box_owns(box, x, y), style_of(country))
