#!/usr/bin/env python
"""A top-down picture of an airport's buildings by role, to judge server/airportroles.py's rules without the game.

    python tools/airport_map.py <lat> <lon> [--km 2] [--out path.png] [--name X] [--lines | --paint [--zoom X Y R]]

Loads the map cells round (lat, lon) as the server does (mapdata.MapData: the cache, else the network), classifies the
buildings and parts (airportroles.classify) and draws, ~1600 px: the runways, taxiways and aprons in greys, the buildings
filled by role (legend), the gate points (a ring where the gate has a jet bridge, a cross where it has none), the jet
bridges' outer ends with an arrow out of the terminal, airside parking hatched, the CURRENT parked aircraft as outlines
(parkedaircraft.kept_stands, read only), the empty stands (parkedaircraft.lead_stands: dashed), the apron masts
(airsideprops) and the stands' lead-in lines and stand details (taxiwaymarks). Prints the count per role and, per gate in
the picture: has a bridge, has an aircraft, has a stand, and the stands whose nose no lead-in reaches (ringed in red).

The painted yellow network is audited (taxiwaymarks.audit): gaps (an end within 8 m of a line but not on it), opens (an end in the
open touching nothing), kinks (a turn of over 20 degrees within 3 m, or a join that is neither tangent nor straight through) and
S-bends tighter than the minimum radius are marked and counted (those inside the picture), and the stands whose lead-in has a
final straight shorter than taxiwaymarks.lead_straight are listed. --lines draws only that network: the pads in light grey, the
mapped centrelines, spines, connectors and lead-ins each in a colour, the stops as dots with the start of each stand's final
straight (a cross), and the audit markers (give --out a name ending _lines.png).

--paint draws what taxiwaymarks PAINTS, top down in the picture's metres (north up) and in the game's order (roadmesh.PAINT_ORDER: the
black material, the red, the yellow and white, the lettering last): the ribbons at their real width and the filled polygons (the
stands' boxes, bars, numbers, the taxiway names) on grey paving, the buildings tan. Give it --zoom X Y R to read the lettering (R m
half side about X, Y m from the airport point); --up B turns the view so that compass bearing B (degrees clockwise from north, the
direction a pilot looks along) is up, about the --zoom point (the axes then read metres right and ahead of it): the lettering reads upright.

--lights draws the airfield lights (server/airfieldlights.py: the same lights and clearing as the tiles) at night: a dark ground, the
pavement and the yellow paint faint, each light a dot of its colour sized by kind (edge, threshold, end, taxiway edge, approach,
PAPI, runway centre, taxiway centre, lead-on / lead-off, stop bar, guard, touchdown zone, wing bar), a legend with the counts; --zoom
X Y R crops it, --skip KIND,KIND leaves kinds out.

--km is the side of the square picture (default 2); data is loaded 300 m beyond it. Needs matplotlib (pip install matplotlib).
"""
import argparse
import math
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

import numpy as np  # noqa: E402

try:
    import matplotlib
except ImportError:                                 # (the venv has none: the user's site-packages, after everything else)
    sys.path.append(site.getusersitepackages())
    try:
        import matplotlib
    except ImportError:
        sys.exit("matplotlib is needed: pip install matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import PatchCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Polygon  # noqa: E402

import aeroways  # noqa: E402
import airportgrade  # noqa: E402
import airports as airportsmod  # noqa: E402
import airportroles as ar  # noqa: E402
import airfieldlights  # noqa: E402
import airsideprops  # noqa: E402
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import netfetch  # noqa: E402
import parkedaircraft as pa  # noqa: E402
import runwaysnap  # noqa: E402
import taxiwaymarks  # noqa: E402

COLOURS = {ar.TERMINAL: "#1f77b4", ar.PIER: "#17becf", ar.JET_BRIDGE: "#d62728", ar.WALKWAY: "#ff7f0e",
           ar.RAISED_BLOCK: "#9467bd", ar.TOWER: "#e377c2", ar.HANGAR: "#8c564b", ar.CANOPY: "#bcbd22",
           ar.LANDSIDE: "#98df8a", ar.OTHER: "#f2c94c"}
LAYERS = ("buildings", "building_parts", "infrastructure", "land_use")
LOAD_PAD_M = 300.0
M_PER_DEG = aeroways.M_PER_DEG


DB = None                                           # (the OurAirports database use_runways loaded: --lights reads its runways)


def use_runways():
    """Lay the mapped runway lines on the OurAirports runways, as the server does (the cached copy: no download); False without one."""
    global DB
    try:
        db = airportsmod.Store(airportsmod.DEFAULT_CACHE, netfetch.Fetcher(offline=True)).load(max_age=float("inf"))
    except (airportsmod.AirportsUnavailable, OSError, ValueError) as ex:
        print("no OurAirports copy: the mapped runway lines stay as mapped:", ex)
        return False
    DB = db
    runwaysnap.set_provider(airportgrade.AirportGrader(db.lean(), None).runways_in_box)
    return True


def load(lat, lon, km, layer_names=LAYERS):
    """(frame, keys, {layer: [Features]}, {key: {layer: Features}}) of the cells under the km square and 300 m round it."""
    half = km * 500.0 + LOAD_PAD_M
    dlat = half / M_PER_DEG
    dlon = half / (M_PER_DEG * math.cos(math.radians(lat)))
    keys = mapcells.cells_for_bbox(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    md = mapdata.MapData(fetcher=netfetch.Fetcher(offline=False))
    out, per = {k: [] for k in layer_names}, {}
    for key in keys:
        layers = md.cell(key, layers=layer_names).layers
        per[str(key)] = layers
        for name in layer_names:
            f = layers.get(name)
            if f is not None and len(f):
                out[name].append(f)
    return geodesy.EnuFrame(lat, lon, 0.0), keys, out, per, md


def ring_xy(frame, ring):
    e, n, _ = frame.geodetic_to_enu(np.asarray(ring[:, 0], np.float64), np.asarray(ring[:, 1], np.float64), 0.0)
    return np.stack([np.asarray(e), np.asarray(n)], 1)


def pad_xy(frame, rings):
    """[(k, 2) m] of aeroways' (lat, lon) rings."""
    out = []
    for la, lo in rings:
        if len(la) >= 3:
            e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            out.append(np.stack([np.asarray(e), np.asarray(n)], 1))
    return out


def aircraft_outline(c, frame):
    """([(4, 2) rectangles], (x, y), (nx, ny)) of a parkedaircraft Craft in the frame's metres."""
    e, n, _ = frame.geodetic_to_enu(np.array([c.lat]), np.array([c.lon]), 0.0)
    x, y = float(e[0]), float(n[0])
    nx, ny = float(c.ne), float(c.nn)
    r = math.hypot(nx, ny) or 1.0
    nx, ny = nx / r, ny / r
    rects = []
    for u0, u1, v0, v1 in pa._rects(c.kind):
        loc = np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]])
        rects.append(pa._world(loc, x, y, nx, ny))
    return rects, (x, y), (nx, ny)


def to_view(nw, frame, xy):
    """The points xy (k, 2) of the network's plane in the picture's frame."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    la, lo, _ = nw.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
    e, n, _ = frame.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
    return np.stack([np.asarray(e), np.asarray(n)], 1)


ROLE_COLOURS = {"mapped": "#1f5fbf", "spine": "#1a9a3a", "connector": "#f08a00", "lead": "#d62728"}
MARKERS = (("gaps", "v", "#d000d0"), ("opens", "^", "#d000d0"), ("kinks", "s", "#ff7f00"), ("sbends", "D", "#00a0c0"))


def audit_view(nw, frame, box):
    """({"gaps": [(x, y, ...)], "opens", "kinks", "sbends"} of nw.audit() in the picture's metres: only those inside box, {the same
    for the whole loaded area: counts only})."""
    got = nw.audit()
    shown, whole = {}, {}
    for key, marks in (("gaps", got.gaps), ("opens", got.opens), ("kinks", got.kinks), ("sbends", got.sbends)):
        pts = to_view(nw, frame, np.array([[m[0], m[1]] for m in marks])) if marks else np.zeros((0, 2))
        rows = [(float(p[0]), float(p[1])) + tuple(m[2:]) for p, m in zip(pts, marks)]
        whole[key] = len(rows)
        shown[key] = [m for m in rows if box[0] <= m[0] <= box[2] and box[1] <= m[1] <= box[3]]
    return shown, whole


def draw_audit(ax, shown, z=13):
    for key, marker, colour in MARKERS:
        if shown[key]:
            ax.scatter([m[0] for m in shown[key]], [m[1] for m in shown[key]], s=110, marker=marker, facecolor="none",
                       edgecolor=colour, linewidths=1.8, zorder=z)


def audit_handles(shown):
    return [Line2D([], [], marker="v", mfc="none", mec="#d000d0", ls="", label=f"gap, under 8 m from a line ({len(shown['gaps'])})"),
            Line2D([], [], marker="^", mfc="none", mec="#d000d0", ls="", label=f"open end ({len(shown['opens'])})"),
            Line2D([], [], marker="s", mfc="none", mec="#ff7f00", ls="", label=f"kink ({len(shown['kinks'])})"),
            Line2D([], [], marker="D", mfc="none", mec="#00a0c0", ls="", label=f"S-bend under the radius ({len(shown['sbends'])})")]


def draw_edges(ax, frame, infra, zorder=2.5):
    """The taxiways' double yellow edge lines (taxiwaymarks._edge_lines), a thin dark yellow line each; their count."""
    sc = taxiwaymarks.scene([f for f in infra if f is not None and len(f)])
    if sc is None:
        return 0
    lines = taxiwaymarks._edge_lines(sc, infra)
    for xy, _w, _kind, _c in lines:
        v = to_view(sc, frame, xy)
        ax.plot(v[:, 0], v[:, 1], color="#c99700", lw=0.8, zorder=zorder, solid_capstyle="butt")
    return len(lines) // 2


def lines_picture(a, out, frame, infra, nw, shown, box, name):
    """The --lines picture: the pads, the network's lines by role (~2 px), the stops, the starts of the final straights, the audit."""
    fig = plt.figure(figsize=(16, 16), dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.94])
    ax.set_facecolor("#ffffff")
    ax.set_xlim(box[0], box[2])
    ax.set_ylim(box[1], box[3])
    ax.set_aspect("equal")
    ax.tick_params(labelsize=8)
    ax.grid(True, color="#eeeeee", lw=0.5)
    for rings, colour in ((pad_xy(frame, aeroways.pads(infra, runways=True)), "#d0d0d0"),
                          (pad_xy(frame, aeroways.ways(infra)), "#dcdcdc"), (pad_xy(frame, aeroways.aprons(infra)), "#ebebeb")):
        ax.add_collection(PatchCollection([Polygon(r, closed=True) for r in rings], facecolor=colour, edgecolor="none", zorder=1))
    n_edges = draw_edges(ax, frame, infra)
    blds = []
    for la, lo in getattr(a, "blocks", ()):          # (the terminals and the other buildings: no line may run through one)
        if len(la) >= 3:
            e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            blds.append(Polygon(np.stack([np.asarray(e), np.asarray(n)], 1), closed=True))
    ax.add_collection(PatchCollection(blds, facecolor="#c8b89a", edgecolor="#8a7a5a", lw=0.6, zorder=1.5))
    for path in (a.kml or ()):                      # (a reference drawing, e.g. Google Earth paths, under the network: grey)
        import re
        txt = open(path, encoding="utf-8").read()
        for co in re.findall(r"<coordinates>(.*?)</coordinates>", txt, re.S):
            ll = np.array([[float(x) for x in c.split(",")[:2]] for c in co.split()])
            e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], np.zeros(len(ll)))
            ax.plot(np.asarray(e), np.asarray(n), color="#555555", lw=4.0, alpha=0.35, zorder=2.8, solid_capstyle="round")
    for xy, role, _rm in nw.lines:
        v = to_view(nw, frame, xy)
        ax.plot(v[:, 0], v[:, 1], color=ROLE_COLOURS[role], lw=1.45, zorder=3 + (role == "lead"), solid_capstyle="butt")
    shorts = to_view(nw, frame, np.array([s[0] for s in nw.short])) if nw.short else np.zeros((0, 2))
    if nw.stops:
        stops = to_view(nw, frame, np.array([s[0] for s in nw.stops]))
        starts = to_view(nw, frame, np.array([s[0] - s[1] * taxiwaymarks.lead_straight(pa.SPECS[s[2]][0]) for s in nw.stops]))
        ax.scatter(stops[:, 0], stops[:, 1], s=26, c="#000000", zorder=9)
        ax.scatter(starts[:, 0], starts[:, 1], s=40, marker="x", c="#008b8b", linewidths=1.2, zorder=9)
    if len(shorts):
        ax.scatter(shorts[:, 0], shorts[:, 1], s=150, marker="o", facecolor="none", edgecolor="#e60000", linewidths=1.6, zorder=12)
    draw_audit(ax, shown)
    handles = [Line2D([], [], color=c, lw=2, label=r) for r, c in ROLE_COLOURS.items()]
    handles += [Line2D([], [], color="#c99700", lw=1.5, label=f"taxiway edge line ({n_edges})"),
                Line2D([], [], marker="o", color="k", ls="", label=f"stop bar ({len(nw.stops)})"),
                Line2D([], [], marker="x", color="#008b8b", ls="", label="start of the final straight"),
                Line2D([], [], marker="o", mfc="none", mec="#e60000", ls="", label=f"final straight too short ({len(shorts)})")]
    ax.legend(handles=handles + audit_handles(shown), loc="upper left", fontsize=9, framealpha=0.9)
    fig.suptitle(f"{name}  ({a.lat:.4f}, {a.lon:.4f})  {a.km:g} km square: the painted network and its audit", fontsize=13, y=0.985)
    fig.savefig(out, dpi=100)


PAINT_RANK = {"black": 0, "red": 1, "yellow": 2, "white": 2, "text": 3, "font_black": 4, "font_yellow": 4, "font_white": 4}   # (roadmesh.PAINT_ORDER by material)
PAINT_FILL = {"black": "#000000", "red": "#d8262c", "yellow": "#ffd21a", "white": "#ffffff", "text": "#000000",
              "font_black": "#000000", "font_yellow": "#ffd21a", "font_white": "#ffffff"}
FONT_SHEET = os.path.join(os.path.dirname(HERE), "mod", "texture_sources", "levels", "beamng_earth", "art", "runway", "bne_paint_font_o.data.webp")


def draw_glyph(ax, sheet, corners, uv, colour, zorder):
    """A character of the lettering as the game draws it: the font sheet's opacity (PIL image `sheet`) over the colour, the crop the quad's
    UVs name laid on its corners (top left, top right, bottom right, bottom left; uv: the same order, the sheet's V up)."""
    from matplotlib.transforms import Affine2D
    w, h = sheet.size
    u0, v1, u1, v0 = uv[0, 0], uv[0, 1], uv[2, 0], uv[2, 1]
    crop = np.asarray(sheet.convert("L").crop((int(u0 * w), int(round((1.0 - v1) * h)), int(math.ceil(u1 * w)), int(math.ceil((1.0 - v0) * h)))), np.float64) / 255.0
    rgba = np.zeros(crop.shape + (4,))
    rgba[..., :3] = [int(colour[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    rgba[..., 3] = crop
    bl, br, tl = corners[3], corners[2], corners[0]
    mat = Affine2D.from_values(br[0] - bl[0], br[1] - bl[1], tl[0] - bl[0], tl[1] - bl[1], bl[0], bl[1])
    ax.imshow(rgba, extent=(0.0, 1.0, 0.0, 1.0), origin="upper", transform=mat + ax.transData, zorder=zorder, interpolation="bilinear")
PAINT_GROUND = (("runway", "#6f6f6f"), ("taxiway", "#868686"), ("apron", "#9c9c9c"))


def paint_picture(a, out, frame, infra, stands, site, box, name):
    """The --paint picture: the paving in greys, the buildings tan, then every ribbon (its real width) and filled polygon that
    taxiwaymarks paints, a material at a time in the order the game draws them (black, red, yellow and white, the lettering: PAINT_RANK)."""
    feats = [f for f in infra if f is not None and len(f)]
    lines, _lb, polys, _pb = taxiwaymarks._all_marks(feats, stands, site)
    pivot = np.array(a.zoom[:2] if a.zoom else (0.0, 0.0))
    up = np.array([math.sin(math.radians(a.up)), math.cos(math.radians(a.up))])
    right = np.array([up[1], -up[0]])

    def view(xy):                                           # (the picture's metres turned so that --up is up, about the pivot)
        xy = np.asarray(xy, np.float64).reshape(-1, 2) - pivot
        return np.stack([xy @ right, xy @ up], 1) if a.up else xy + pivot
    if a.up:
        box = (-math.hypot(box[2], box[3]),) * 2 + (math.hypot(box[2], box[3]),) * 2
    fig = plt.figure(figsize=(16, 16), dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.94])
    ax.set_facecolor("#e9efe3")
    ax.set_xlim(box[0], box[2])
    ax.set_ylim(box[1], box[3])
    ax.set_aspect("equal")
    ax.tick_params(labelsize=8)
    ax.grid(True, color="#ffffff", alpha=0.35, lw=0.5)
    for (_what, colour), rings in zip(PAINT_GROUND, (pad_xy(frame, aeroways.pads(infra, runways=True)),
                                                    pad_xy(frame, aeroways.ways(infra)), pad_xy(frame, aeroways.aprons(infra)))):
        ax.add_collection(PatchCollection([Polygon(view(r), closed=True) for r in rings], facecolor=colour, edgecolor="none", zorder=1))
    blds = []
    for la, lo in (site.blocks if site is not None else ()):    # (under the paint: any of it that runs into a building shows)
        if len(la) >= 3:
            e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
            blds.append(Polygon(view(np.stack([np.asarray(e), np.asarray(n)], 1)), closed=True))
    ax.add_collection(PatchCollection(blds, facecolor="#c8b89a", edgecolor="#8a7a5a", lw=0.6, zorder=2))
    by = {}                                                 # {colour: [Polygon]}
    n_ribbon = 0
    for la, lo, width, _kind, colour in lines:
        e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
        for q in taxiwaymarks._strip(np.asarray(e), np.asarray(n), width):
            by.setdefault(colour, []).append(Polygon(view(q), closed=True))
        n_ribbon += 1
    if a.zoom:                                              # (the view's limits: only the characters inside it are drawn as pictures)
        mid = np.zeros(2) if a.up else pivot
        lim = (mid[0] - a.zoom[2], mid[0] + a.zoom[2], mid[1] - a.zoom[2], mid[1] + a.zoom[2])
    else:
        lim = (box[0], box[2], box[1], box[3])
    sheet = None
    n_glyph = 0
    for la, lo, colour, _layer, *uv in polys:
        e, n, _ = frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), np.zeros(len(la)))
        v = view(np.stack([np.asarray(e), np.asarray(n)], 1))
        if uv and float(np.ptp(np.asarray(uv[0])[:, 0])) > 1e-9:       # (a character: the sheet's crop, as the game's material shows it)
            n_glyph += 1
            if v[:, 0].max() < lim[0] or v[:, 0].min() > lim[1] or v[:, 1].max() < lim[2] or v[:, 1].min() > lim[3]:
                continue
            if sheet is None:
                from PIL import Image
                sheet = Image.open(FONT_SHEET)
            draw_glyph(ax, sheet, v, np.asarray(uv[0]), PAINT_FILL[colour], 10 + PAINT_RANK[colour])
        else:                                                           # (a filled polygon; a ring's bars are quads on the sheet's solid cell)
            by.setdefault(colour, []).append(Polygon(v, closed=True))
    for colour, patches in by.items():
        ax.add_collection(PatchCollection(patches, facecolor=PAINT_FILL[colour], edgecolor="none", zorder=10 + PAINT_RANK[colour]))
    sc = taxiwaymarks.scene(feats) if a.refs else None
    for xy, _cls, wi, _f, _l in (taxiwaymarks._mapped_runs(sc) if sc is not None else ()):    # (the ways' refs along their lines, in blue)
        if sc.ways[wi][2]:
            v = view(to_view(sc, frame, xy))
            cum = np.r_[0.0, np.cumsum(np.hypot(*np.diff(v, axis=0).T))]
            for at in np.arange(min(15.0, cum[-1] / 2.0), cum[-1], 70.0):
                k = min(int(np.searchsorted(cum, at)), len(v) - 1)
                ax.text(v[k, 0], v[k, 1], sc.ways[wi][2], color="#1040ff", fontsize=11, weight="bold", ha="center", va="center", zorder=30)
    ax.set_xlim(lim[0], lim[1])                             # (imshow moves the limits)
    ax.set_ylim(lim[2], lim[3])
    handles = [Patch(facecolor=c, edgecolor="#333333", label=f"{k} ({len(by.get(k, ()))} pieces)") for k, c in PAINT_FILL.items() if not k.startswith("font_")]
    handles.append(Patch(facecolor="#808080", edgecolor="#333333", label=f"lettering ({n_glyph} characters, font sheet)"))
    handles += [Patch(facecolor=c, label=w) for w, c in PAINT_GROUND]
    ax.legend(handles=handles, loc="lower right", fontsize=9, framealpha=0.9)
    fig.suptitle(f"{name}  ({a.lat:.4f}, {a.lon:.4f})  {a.km:g} km square: the paint (taxiwaymarks), game draw order: {n_ribbon} ribbons, "
                 f"{len(polys)} polygons" + (f"; bearing {a.up:g} up" if a.up else ""), fontsize=13, y=0.985)
    fig.savefig(out, dpi=100)


# ------------------------------------------------------------------------------------------------- the stand audit
ORIENT_MAX_DEG = 10.0           # an aircraft's axis this far off the nose-in direction is flagged
FIT_MAX_M = 3.0                 # a jet bridge's outer end (its face: end_face_dist) this far from the forward left door is flagged
LEAD_NOSE_M = 1.0               # a lead-in ends within this of its aircraft's nose; ending between that and LEAD_NEAR_M it is "off
LEAD_NEAR_M = 15.0              # the nose", further (or none): the stand has no lead-in
WALL_FIND_M = 40.0              # the wall nearest the nose, within this (a remote stand has none)
WALL_NEAR_M = 15.0              # ... every wall within this of the nose is one it may be nose-in to (the tip and the side of a pier's end)
NORMAL_R_M = 10.0               # the wall's local normal: the edges within this of its nearest point
SHRINK_M = 0.25                 # an overlap counts where it is deeper than this
SAMPLE_STEP_M = 0.75
MATCH_M = 12.0                  # an aircraft whose door is this near a bridged gate's end is that gate's
RAISED_M = 3.0                  # a footprint raised this far, or narrow, is no obstacle to the wings (the fuselage's only)
NARROW_W_M = 6.0
JOIN_TAIL_M = 8.0


def wall_normals(rings, p, reach=WALL_FIND_M, near=WALL_NEAR_M):
    """[outward unit normal] of the walls the nose at p could be nose-in to: for each building ring within `near` of p, the wall's
    general line at its nearest point (parkedaircraft.wall_normal), the normal of the nearest edge itself where that is a face
    (EDGE_MIN_M or more: a finger's tip) and of every long wall (MACRO_EDGE_M) within `near` (a junction, a concave corner: each
    wall either side is one to face); where no ring is within `near`, those of the nearest within `reach`; [] where none."""
    found = []
    for ring in rings:
        if (ring.min(0) - reach > p).any() or (ring.max(0) + reach < p).any():
            continue
        d, q, j = pa._nearest_on(ring, p)
        if d <= reach:
            found.append((d, q, j, ring))
    found.sort(key=lambda t: t[0])
    out = []
    for d, q, j, ring in found:
        if d > near and out:
            break
        m = pa.wall_normal(ring, q)
        if m is not None:
            out.append(m)
        if d <= near:                                  # (a junction, a concave corner: the long walls either side are each a wall to face)
            a, b = ring, np.roll(ring, -1, axis=0)
            ab = b - a
            ln = np.hypot(ab[:, 0], ab[:, 1])
            t = np.clip(((p - a) * ab).sum(1) / np.maximum(ln * ln, 1e-12), 0.0, 1.0)
            de = np.hypot(*(a + t[:, None] * ab - p).T)
            for k in np.flatnonzero((ln >= pa.MACRO_EDGE_M) & (de <= near) & (np.arange(len(a)) != j)):
                n = np.array([-ab[k][1], ab[k][0]]) / ln[k]
                out.append(-n if bool(pa._pip(ring, (a[k] + ab[k] * 0.5 + n)[None])[0]) else n)
        e = np.roll(ring, -1, axis=0)[j] - ring[j]
        ln = float(np.hypot(*e))
        if ln >= pa.EDGE_MIN_M:                        # (the nearest face itself, a finger's tip too: the placement's own edge rule)
            n = np.array([-e[1], e[0]]) / ln
            out.append(-n if bool(pa._pip(ring, (q + n)[None])[0]) else n)
        if ln < pa.MACRO_EDGE_M and m is None:
            out.append(wall_edge_normal(ring, q, j))
    return out


def radial_dirs(rings, p, near=WALL_NEAR_M):
    """[unit direction from p to the centre of the arc] of the pier tips and corners (parkedaircraft.radial_centre) the nose at p is by:
    each ring within `near` of p whose wall there turns convex by more than parkedaircraft.RADIAL_TURN within 40 m. A gate there parks
    radially, so this is its nose-in direction (and the only one: the wall's normals are not)."""
    out = []
    for ring in rings:
        if (ring.min(0) - near > p).any() or (ring.max(0) + near < p).any():
            continue
        d, q, _j = pa._nearest_on(ring, p)
        if d > near:
            continue
        c = pa.radial_centre(ring, q, p)
        if c is not None:
            v = c - p
            out.append(v / max(float(np.hypot(*v)), 1e-9))
    return out


def wall_edge_normal(ring, q, j):
    """The outward unit normal of the ring's edge j (a short step: the length-weighted principal direction of the edges within
    NORMAL_R_M of q)."""
    a, b = ring, np.roll(ring, -1, axis=0)
    ab = b - a
    ln = np.maximum(np.hypot(ab[:, 0], ab[:, 1]), 1e-9)
    near = np.hypot(*(((a + b) / 2.0) - q).T) <= NORMAL_R_M + ln / 2.0
    w = np.minimum(ln, NORMAL_R_M)[near]
    ang = np.arctan2(ab[near, 1], ab[near, 0])
    sy, sx = float((w * np.sin(2 * ang)).sum()), float((w * np.cos(2 * ang)).sum())
    t = math.atan2(float(ab[j, 1]), float(ab[j, 0])) if math.hypot(sx, sy) < 0.3 * float(w.sum()) else 0.5 * math.atan2(sy, sx)
    n = np.array([-math.sin(t), math.cos(t)])
    return -n if bool(pa._pip(ring, (q + n)[None])[0]) else n


def _rect_pts(rect, shrink=0.0, step=SAMPLE_STEP_M):
    """Sample points (grid, edges included) of the rectangle (4, 2: corners in order), pulled in `shrink` each side."""
    p0, e1, e3 = rect[0], rect[1] - rect[0], rect[3] - rect[0]
    l1, l3 = float(np.hypot(*e1)), float(np.hypot(*e3))
    a = np.linspace(shrink, max(l1 - shrink, shrink), max(2, int(math.ceil(l1 / step)) + 1)) / max(l1, 1e-9)
    b = np.linspace(shrink, max(l3 - shrink, shrink), max(2, int(math.ceil(l3 / step)) + 1)) / max(l3, 1e-9)
    aa, bb = np.meshgrid(a, b)
    return p0 + aa.reshape(-1, 1) * e1 + bb.reshape(-1, 1) * e3


def _in_rect(pts, rect, shrink=0.0):
    """Which of pts lie inside the rectangle pulled in `shrink` each side."""
    p0, e1, e3 = rect[0], rect[1] - rect[0], rect[3] - rect[0]
    l1, l3 = float(np.hypot(*e1)), float(np.hypot(*e3))
    d = pts - p0
    a, b = d @ e1 / max(l1, 1e-9), d @ e3 / max(l3, 1e-9)
    return (a >= shrink) & (a <= l1 - shrink) & (b >= shrink) & (b <= l3 - shrink)


def _ring_pts(ring, step=SAMPLE_STEP_M):
    out = []
    for p, q in zip(ring, np.roll(ring, -1, axis=0)):
        n = max(1, int(math.ceil(float(np.hypot(*(q - p))) / step)))
        out.append(p + (q - p) * (np.arange(n)[:, None] / n))
    return np.concatenate(out)


def rect_hits_rect(a, b, shrink=SHRINK_M):
    return bool(_in_rect(_rect_pts(b), a, shrink).any() or _in_rect(_rect_pts(a), b, shrink).any())


def rect_hits_ring(rect, ring, shrink=SHRINK_M):
    lo, hi = rect.min(0), rect.max(0)
    if (ring.max(0) < lo).any() or (ring.min(0) > hi).any():
        return False
    return bool(_in_rect(_ring_pts(ring), rect, shrink).any() or pa._pip(ring, _rect_pts(rect, shrink)).any())


def door_of(c, cx, cy, nx, ny):
    """The forward left door's point of an aircraft at (cx, cy) heading (nx, ny): DOOR_K of its length back from the nose, half the
    fuselage's width to the left."""
    length, fus = pa.SPECS[c.kind][0], pa.SPECS[c.kind][3]
    return np.array([cx + nx * (length / 2.0 - pa.DOOR_K * length) - ny * fus / 2.0,
                     cy + ny * (length / 2.0 - pa.DOOR_K * length) + nx * fus / 2.0])


def end_face_dist(p, end, d, half):
    """The distance from p to a jet bridge's outer end: the face across the bridge (the end point, half its width each way, square to
    its direction d), not the point in its middle (a door can only meet the face's edge, the bridge's body being beside the fuselage)."""
    perp = np.array([-d[1], d[0]])
    t = float(np.clip((p - end) @ perp, -half, half))
    return float(np.hypot(*(p - (end + perp * t))))


def gear_points(c, cx, cy, nx, ny):
    length, span = pa.SPECS[c.kind][0], pa.SPECS[c.kind][1]
    local = np.array([[length / 2.0 - pa.NOSE_GEAR_K * length, 0.0],
                      [length / 2.0 - pa.MAIN_GEAR_K * length, pa.MAIN_TRACK_K * span],
                      [length / 2.0 - pa.MAIN_GEAR_K * length, -pa.MAIN_TRACK_K * span]])
    return pa._world(local, cx, cy, nx, ny)


def stand_audit(res, every, kept, frame, infra, nw, box):
    """The stand audit of the aircraft and stands inside box (the picture's metres): {"rows": [per aircraft], "bridges": [per bridged
    gate in the picture], "stands": [per stand that gets a lead-in]}, each a dict with a "flags" list of what is wrong (aircraft: orient,
    fit, bridge, aircraft, building, unpaved; stands: nolead, offnose)."""
    inb = lambda x, y: box[0] <= x <= box[2] and box[1] <= y <= box[3]   # noqa: E731
    walls = [u.ring for u in res.units.values() if u.role in (ar.TERMINAL, ar.PIER)]
    jets = [u for u in res.units.values() if u.role == ar.JET_BRIDGE]
    brs = res.bridges
    others = []                                  # (ring, raised or narrow: no obstacle to the wings, its label)
    for u in res.units.values():
        if u.role == ar.JET_BRIDGE:
            continue
        mh = u.mh if u.mh == u.mh else 0.0
        if u.width <= NARROW_W_M and 2.0 * u.width <= u.length <= 60.0 and u.area < pa.BRIDGE_MAX_M2:
            continue                             # (a bridge, a finger or a shed's sliver by a gate: the placement lets the stand touch it)
        others.append((u.ring, mh >= RAISED_M or (u.width <= NARROW_W_M and 2.0 * u.width <= u.length <= 60.0),
                       f"{u.role} {u.label()}"))
    pave = pad_xy(frame, aeroways.pads(infra, runways=True))
    pave_box = [(r, r.min(0), r.max(0)) for r in pave]
    craft = []
    for c in kept:
        rects, (cx, cy), (nx, ny) = aircraft_outline(c, frame)
        if inb(cx, cy):
            craft.append({"c": c, "rects": rects, "xy": (cx, cy), "n": (nx, ny), "flags": [], "info": {}})
    held = {(c.kind, round(c.lat, 7), round(c.lon, 7)) for c in kept}
    spare = []                                   # the empty stands (no aircraft: a bridged gate's fit is the same geometry)
    for c in every:
        if (c.kind, round(c.lat, 7), round(c.lon, 7)) not in held:
            rects, (cx, cy), (nx, ny) = aircraft_outline(c, frame)
            if inb(cx, cy):
                spare.append({"c": c, "rects": rects, "xy": (cx, cy), "n": (nx, ny), "flags": [], "info": {}})
    jend = {}                                    # a jet bridge's outer end and direction (a tail takes its bridge's)
    for u in jets:
        if u.id in brs:
            jend[u.id] = (np.asarray(brs[u.id][2]), np.asarray(brs[u.id][3]))
        elif brs:
            b = min(brs.values(), key=lambda t: float(np.hypot(*(u.ring - np.asarray(t[2])).T).min()))
            jend[u.id] = (np.asarray(b[2]), np.asarray(b[3]))
    bridges = []                                 # bridge fit: a bridged gate's aircraft (the door nearest the bridge's outer end; one aircraft
    pairs = []                                   # to one gate: the closest pairs first, so a neighbour's aircraft is no gate's)
    for gid, g in sorted(res.gates.items()):
        if g["bridge"] is None or not inb(*g["xy"]):
            continue
        end = np.asarray(g["end"], np.float64)
        for ai, a in enumerate(craft + spare):
            (cx, cy), (nx, ny) = a["xy"], a["n"]
            if not bool(pa._in_rects(a["c"].kind, cx, cy, nx, ny, end[None], 8.0)[0]):
                continue
            door = door_of(a["c"], cx, cy, nx, ny)
            d = end_face_dist(door, end, np.asarray(g["dir"], np.float64), res.units[g["bridge"]].width / 2.0)
            pairs.append((d, gid, ai, tuple(door)))
    gate_got, used = {}, set()
    for d, gid, ai, door in sorted(pairs, key=lambda t: t[:3]):
        if gid not in gate_got and ai not in used:
            gate_got[gid] = (d, ai, door)
            used.add(ai)
    for gid, g in sorted(res.gates.items()):
        if g["bridge"] is None or not inb(*g["xy"]):
            continue
        end = np.asarray(g["end"], np.float64)
        if gid not in gate_got:
            bridges.append({"gate": gid, "end": tuple(end), "aircraft": None, "dist": None, "flags": []})
            continue
        d, ai, door = gate_got[gid]
        a = (craft + spare)[ai]
        bridges.append({"gate": gid, "end": tuple(end), "aircraft": a, "dist": d, "door": door,
                        "flags": ["fit"] if d > FIT_MAX_M else []})
        if d <= MATCH_M and "link" not in a["info"]:
            a["info"]["link"] = (g["bridge"], np.asarray(g["dir"], np.float64))
        if d > FIT_MAX_M:
            a["flags"].append("fit")
    for a in craft:                              # orientation: the nose-in direction (the wall's local normal at the nose; at a bridged
        c = a["c"]                               # gate the bridge-axis rule at the bridge's root)
        (cx, cy), (nx, ny) = a["xy"], a["n"]
        if c.mode != "gate":
            continue
        cands = []                               # the nose-in directions it may take (the one nearest the aircraft's is its reference)
        link = a["info"].get("link")
        if link is not None:
            root = np.asarray(brs[link[0]][1], np.float64)
            d = link[1]
            for n in wall_normals(walls, root, near=0.0) or [d]:
                if float(n @ d) > 0.0:
                    cos = float(d @ n)
                    cands.append(-d if pa.BRIDGE_TURN_COS < cos < pa.BRIDGE_AXIS_COS else -n)
            cands = cands or [-d]
        else:
            half = pa.SPECS[c.kind][0] / 2.0
            nose = np.array([cx + nx * half, cy + ny * half])
            cands = radial_dirs(walls, nose)                  # (a tip or a corner: radial)
            a["info"]["radial"] = bool(cands)
            cands = cands + [-n for n in wall_normals(walls, nose)]
        if not cands:
            continue
        err, ref = min((math.degrees(math.acos(max(-1.0, min(1.0, nx * float(r[0]) + ny * float(r[1]))))), tuple(r)) for r in cands)
        a["info"]["orient"], a["info"]["ref"] = err, ref
        if err > ORIENT_MAX_DEG:
            a["flags"].append("orient")
    for i, a in enumerate(craft):                # overlaps: the fuselage with a jet bridge (less the cab, at the bridge its door is by),
        (cx, cy), (nx, ny) = a["xy"], a["n"]     # aircraft with aircraft, with buildings, the gear off the paving
        door = door_of(a["c"], cx, cy, nx, ny)
        for u in jets:
            e, d = jend[u.id]
            ring = u.ring
            if float(np.hypot(*(door - e))) <= JOIN_TAIL_M + MATCH_M:
                cut = pa._cut(ring, e, d)
                if cut is None:
                    continue
                ring = cut[0]
            if rect_hits_ring(a["rects"][0], ring):
                a["flags"].append("bridge")
                break
        for b in craft[i + 1:]:
            if math.hypot(a["xy"][0] - b["xy"][0], a["xy"][1] - b["xy"][1]) > pa._radius(a["c"].kind) + pa._radius(b["c"].kind):
                continue
            if any(rect_hits_rect(ra, rb) for ra in a["rects"] for rb in b["rects"]):
                a["flags"].append("aircraft")
                b["flags"].append("aircraft")
        for ring, raised, label in others:
            if any(rect_hits_ring(rect, ring) for k, rect in enumerate(a["rects"]) if not (raised and k)):
                a["flags"].append("building")
                a["info"]["building"] = label
                break
        gap = pa.PAVING_GAP_M if a["c"].mode == "gate" else 0.0      # (a terminal's stand: the unmapped strip between its wall and the apron)
        half = pa.SPECS[a["c"].kind][0] / 2.0
        nose = np.array([cx + nx * half, cy + ny * half])
        near_walls = [u.ring for u in res.units.values() if u.role != ar.JET_BRIDGE and pa._area(u.ring) >= pa.TERMINAL_MIN_M2
                      and pa._nearest_on(u.ring, nose)[0] <= pa.NEAR_M] if gap else []     # (the placement's: any building of that size)
        bad = []
        for p in gear_points(a["c"], cx, cy, nx, ny):
            if not pave or any((p >= lo).all() and (p <= hi).all() and bool(pa._pip(r, p[None])[0]) for r, lo, hi in pave_box):
                continue
            near = min(((pa._nearest_on(r, p)[:2]) for r, lo, hi in pave_box if (p >= lo - gap).all() and (p <= hi + gap).all()),
                       key=lambda t: t[0], default=None) if gap else None
            # (off the paving: only the strip between a terminal's wall and the paving: on some wall's side of it)
            if near is None or near[0] > gap or not any(
                    pa._nearest_on(w, p)[0] < pa._nearest_on(w, near[1])[0] + pa.PAVING_SLACK_M for w in near_walls):
                bad.append(tuple(p))
        if bad:
            a["flags"].append("unpaved")
            a["info"]["unpaved"] = bad
    ends, mapped = [], []                        # the stands' lead-ins (every stand, empty or not): one ends at the aircraft's nose, or a
    if nw is not None:                           # mapped lane already runs through it (the stand's own lead-in lane)
        for xy, role, _rm in nw.lines:
            v = to_view(nw, frame, xy)
            if role == "lead":
                ends += [v[0], v[-1]]
            else:
                mapped.append(v)
    ends = np.array(ends).reshape(-1, 2)
    stands = []
    for c in every:
        if c.mode not in taxiwaymarks.LEAD_MODES:
            continue
        _rects, (cx, cy), (nx, ny) = aircraft_outline(c, frame)
        if not inb(cx, cy):
            continue
        half = pa.SPECS[c.kind][0] / 2.0
        nose = np.array([cx + nx * half, cy + ny * half])
        d = float(np.hypot(*(ends - nose).T).min()) if len(ends) else float("inf")
        if d > LEAD_NOSE_M and any(float(pa._seg_dist(nose[None], v[:-1], v[1:]).min()) <= LEAD_NOSE_M for v in mapped):
            d = 0.0
        stands.append({"c": c, "nose": tuple(nose), "dist": d,
                       "flags": [] if d <= LEAD_NOSE_M else ["offnose"] if d <= LEAD_NEAR_M else ["nolead"]})
    return {"rows": craft, "bridges": bridges, "stands": stands}


def audit_counts(au, n_gates, whole_marks):
    """The audit's table row (a dict of numbers); whole_marks: {"gaps": n, "opens": n, "kinks": n, "sbends": n} of the picture."""
    rows, br, st = au["rows"], au["bridges"], au["stands"]
    with_ac = [r for r in br if r["aircraft"] is not None]

    def n(flag):
        return sum(1 for r in rows if flag in r["flags"])
    out = {"gates": n_gates, "stands": len(st), "aircraft": len(rows),
           "fill_pct": round(100.0 * len(rows) / n_gates, 1) if n_gates else 0.0,
           "orient": n("orient"), "orient_of": sum(1 for r in rows if r["info"].get("orient") is not None),
           "fit": sum(1 for r in with_ac if "fit" in r["flags"]), "fit_of": len(with_ac), "bridged": len(br),
           "bridge": n("bridge"), "aircraft_overlap": n("aircraft"), "building": n("building"), "unpaved": n("unpaved"),
           "nolead": sum(1 for s in st if "nolead" in s["flags"]), "offnose": sum(1 for s in st if "offnose" in s["flags"])}
    out.update(whole_marks)
    return out


def print_audit(name, au, counts):
    print(f"STAND AUDIT {name}: gates {counts['gates']}, stands {counts['stands']}, aircraft {counts['aircraft']}, fill {counts['fill_pct']}%")
    print(f"  orientation > {ORIENT_MAX_DEG:g} deg: {counts['orient']} of {counts['orient_of']} measured; bridge fit > {FIT_MAX_M:g} m: "
          f"{counts['fit']} of {counts['fit_of']} bridged gates with a stand ({counts['bridged']} bridged); fuselage in a bridge: "
          f"{counts['bridge']}")
    print(f"  overlaps: aircraft {counts['aircraft_overlap']}, buildings {counts['building']}, off paving {counts['unpaved']}; "
          f"lines: no lead-in {counts['nolead']}, lead-in off its nose {counts['offnose']}, gaps {counts['gaps']}, opens {counts['opens']}, "
          f"kinks {counts['kinks']}, S-bends {counts['sbends']}")
    for r in au["rows"]:
        if r["flags"]:
            o = r["info"].get("orient")
            print(f"    aircraft {r['c'].kind} at ({r['xy'][0]:.0f}, {r['xy'][1]:.0f}) ref {r['c'].ref or '-'}: "
                  f"{','.join(sorted(set(r['flags'])))}" + ("" if o is None else f" (off by {o:.0f}{', radial' if r['info'].get('radial') else ''})")
                  + (f" in {r['info']['building']}" if "building" in r["flags"] else ""))
    for r in au["bridges"]:
        if r["flags"]:
            print(f"    bridge fit gate {r['gate'][:8]}: {r['dist']:.1f} m ({r['aircraft']['c'].kind})")
    for s in au["stands"]:
        if s["flags"]:
            print(f"    stand {s['c'].kind} nose ({s['nose'][0]:.0f}, {s['nose'][1]:.0f}) ref {s['c'].ref or '-'}: {s['flags'][0]}"
                  + ("" if s["dist"] == float("inf") else f" ({s['dist']:.1f} m)"))


def draw_audit_marks(ax, au):
    """Mark the failures on the picture: a failing aircraft thick (red: orientation, with a green arrow where its nose should point; blue:
    bridge fit, a line from the door to the bridge's end; magenta: overlap), a green triangle for a gear leg off the paving, a ring at the
    nose of a stand with no lead-in (red) or a lead-in off its nose (orange)."""
    for r in au["rows"]:
        fl = set(r["flags"])
        if not fl:
            continue
        colour = "#e60000" if "orient" in fl else "#0050ff" if "fit" in fl else "#d000d0"
        for rect in r["rects"]:
            ax.add_patch(Polygon(rect, closed=True, facecolor="none", edgecolor=colour, linewidth=2.2, zorder=15))
        (cx, cy) = r["xy"]
        if "orient" in fl:
            ref = r["info"]["ref"]
            ax.annotate("", xy=(cx + 40 * float(ref[0]), cy + 40 * float(ref[1])), xytext=(cx, cy), zorder=16,
                        arrowprops=dict(arrowstyle="-|>", color="#00a000", lw=2.0, mutation_scale=12))
        if fl & {"aircraft", "building", "bridge"}:
            ax.scatter([cx], [cy], s=150, marker="X", c="#d000d0", edgecolor="k", linewidths=0.6, zorder=17)
        for p in r["info"].get("unpaved", ()):
            ax.scatter([p[0]], [p[1]], s=70, marker="v", c="#00b000", edgecolor="k", linewidths=0.6, zorder=17)
    for r in au["bridges"]:
        if "fit" in r["flags"]:
            ax.plot([r["door"][0], r["end"][0]], [r["door"][1], r["end"][1]], color="#0050ff", lw=2.0, zorder=16)
            ax.scatter([r["end"][0]], [r["end"][1]], s=130, facecolor="none", edgecolor="#0050ff", linewidths=2.0, zorder=16)
    for s in au["stands"]:
        if s["flags"]:
            ax.scatter([s["nose"][0]], [s["nose"][1]], s=170, marker="o", facecolor="none",
                       edgecolor="#e60000" if s["flags"][0] == "nolead" else "#ff9900", linewidths=2.2, zorder=16)


def stand_handles(counts):
    return [Line2D([], [], color="#e60000", lw=2.2, label=f"aircraft off nose-in by over {ORIENT_MAX_DEG:g} deg ({counts['orient']}), green arrow: nose-in"),
            Line2D([], [], color="#0050ff", lw=2.2, label=f"bridge end over {FIT_MAX_M:g} m from the left door ({counts['fit']})"),
            Line2D([], [], marker="X", mfc="#d000d0", mec="k", ls="",
                   label=f"overlap: aircraft / building / bridge ({counts['aircraft_overlap']} / {counts['building']} / {counts['bridge']})"),
            Line2D([], [], marker="v", mfc="#00b000", mec="k", ls="", label=f"gear off the paving ({counts['unpaved']})"),
            Line2D([], [], marker="o", mfc="none", mec="#e60000", ls="", label=f"stand without a lead-in ({counts['nolead']})"),
            Line2D([], [], marker="o", mfc="none", mec="#ff9900", ls="", label=f"lead-in off its nose ({counts['offnose']})")]


LIGHT_COLOURS = {"white": "#fff4d6", "green": "#2dff6e", "red": "#ff3b30", "blue": "#3d7bff", "yellow": "#ffd21a"}
LIGHT_SIZE = {"edge": 5, "threshold": 7, "end": 7, "taxi": 4, "approach": 6, "papi": 9, "centre": 6, "taxicl": 5, "lead": 7,
              "stopbar": 9, "guard": 12, "tdz": 6, "wing": 8}       # (marker areas in points squared at the 2 km picture)


def lights_picture(a, out, frame, keys, per, infra, box, name):
    """The --lights picture (the airfield lights as the server makes them, airfieldlights.lights_of_cells: the same clearing of the
    equipment as the tiles): a dark ground, the pavement and the paint faint, each light a dot of its colour, its size by kind."""
    db = DB
    if db is None:
        sys.exit("--lights needs the OurAirports runways (use_runways)")
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]) - 50.0, np.array([y0, y1, y0, y1]) + 50.0, np.zeros(4))
    la, lo = np.asarray(la, np.float64), np.asarray(lo, np.float64)
    infra_fn = lambda k: per[str(k)].get("infrastructure") if str(k) in per else None
    bld_fn = lambda k: per[str(k)].get("buildings") if str(k) in per else None
    road_fn = lambda k: per[str(k)].get("roads") if str(k) in per else None
    import time
    t0 = time.time()
    skip = tuple(k for k in a.skip.split(",") if k)
    lights, _masts = airfieldlights.lights_of_cells(
        keys, la, lo, infra_fn, lambda k: db.in_cell(k, margin_m=airfieldlights.TAXI_REACH_M), road_fn, bld_fn, masts=False, skip=skip)
    print(f"lights: {len(lights)} in {time.time() - t0:.1f} s")
    e, n, _ = frame.geodetic_to_enu(lights.lat, lights.lon, 0.0)
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    inside = (e >= x0) & (e <= x1) & (n >= y0) & (n <= y1)
    fig = plt.figure(figsize=(16, 16), dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.94])
    ax.set_facecolor("#03050a")
    ax.set_xlim(box[0], box[2])
    ax.set_ylim(box[1], box[3])
    ax.set_aspect("equal")
    ax.tick_params(labelsize=8, colors="#667")
    for rings, colour in ((pad_xy(frame, aeroways.aprons(infra)), "#0c0f16"), (pad_xy(frame, aeroways.pads(infra, runways=True)), "#10131b")):
        ax.add_collection(PatchCollection([Polygon(r, closed=True) for r in rings], facecolor=colour, edgecolor="none", zorder=1))
    try:                                            # (the paint, faint: where a light should sit)
        for la0, lo0, la1, lo1, _w, kind, _c in taxiwaymarks.strokes([f for f in infra if f is not None and len(f)]):
            if kind in (taxiwaymarks.CENTRE_KIND, taxiwaymarks.HOLD_KIND[True], taxiwaymarks.HOLD_KIND[False]):
                pe, pn, _ = frame.geodetic_to_enu(np.array([la0, la1]), np.array([lo0, lo1]), 0.0)
                ax.plot(pe, pn, color="#5a4d10", lw=0.8, zorder=2, solid_capstyle="butt")
    except Exception as ex:                         # noqa: BLE001
        print("paint:", type(ex).__name__, ex)
    scale = min(6.0, max(1.0, (2000.0 / max(box[2] - box[0], 1.0)) ** 0.6))
    counts = {}
    for kind_i, kind in enumerate(airfieldlights.KINDS):
        for col_i, col in enumerate(airfieldlights.COLOURS):
            sel = inside & (lights.kind == kind_i) & (lights.colour == col_i)
            if sel.any():
                counts[(kind, col)] = int(sel.sum())
                ax.scatter(e[sel], n[sel], s=LIGHT_SIZE[kind] * scale, c=LIGHT_COLOURS[col], linewidths=0, zorder=5)
    handles = [Line2D([], [], marker="o", color=LIGHT_COLOURS[c], ls="", markersize=6, label=f"{k} {c} ({v})")
               for (k, c), v in sorted(counts.items())]
    ax.legend(handles=handles, loc="lower right", fontsize=8, framealpha=0.85, facecolor="#10131b", labelcolor="#ccd")
    for (k, c), v in sorted(counts.items()):
        print(f"  {k:10s} {c:7s} {v}")
    fig.suptitle(f"{name}  night lights: {int(inside.sum())} in the picture ({len(lights)} loaded)", fontsize=13, y=0.985, color="#ccd")
    fig.patch.set_facecolor("#03050a")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fig.savefig(out, dpi=100, facecolor=fig.get_facecolor())
    print("saved", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--km", type=float, default=2.0, help="side of the picture (default 2)")
    ap.add_argument("--out", default=None, help="PNG path (default airport_map_<lat>_<lon>.png)")
    ap.add_argument("--name", default="", help="a name for the title")
    ap.add_argument("--lines", action="store_true", help="draw only the painted yellow network and its audit")
    ap.add_argument("--paint", action="store_true", help="draw what taxiwaymarks paints (ribbons and polygons by colour, the game's order), see --zoom")
    ap.add_argument("--lights", action="store_true", help="draw the airfield lights (airfieldlights) as dots on a dark ground, see --zoom")
    ap.add_argument("--skip", default="", help="--lights: kinds left out, comma separated (airfieldlights.KINDS)")
    ap.add_argument("--zoom", type=float, nargs=3, default=None, metavar=("X", "Y", "R"),
                    help="crop the picture to the square of half side R m about (X, Y) m (for a closer look at a failure)")
    ap.add_argument("--up", type=float, default=0.0, help="--paint: the compass bearing (degrees) to put up, about the --zoom point (0: north up)")
    ap.add_argument("--refs", action="store_true", help="--paint: write each named way's ref (blue) along its line")
    ap.add_argument("--as-mapped", action="store_true", help="leave the mapped runway lines where they are mapped (not laid on the OurAirports runways)")
    ap.add_argument("--kml", nargs="*", default=None, help="KML files whose paths are drawn under the --lines network (a reference drawing)")
    ap.add_argument("--summary", default=None, help="write the stand audit's counts as JSON to this path")
    a = ap.parse_args()
    out = a.out or f"airport_map_{a.lat:.4f}_{a.lon:.4f}.png"
    if not a.as_mapped:
        use_runways()
    frame, keys, layers, per, md = load(a.lat, a.lon, a.km, LAYERS + (("roads",) if a.lights else ()))
    infra = layers["infrastructure"]
    half = a.km * 500.0
    box = (-half, -half, half, half)
    if a.lights:
        if a.zoom:
            box = (a.zoom[0] - a.zoom[2], a.zoom[1] - a.zoom[2], a.zoom[0] + a.zoom[2], a.zoom[1] + a.zoom[2])
        lights_picture(a, out, frame, keys, per, infra, box, a.name or "airport")
        return
    res = None if (a.lines or a.paint) else ar.classify(layers["buildings"], layers["building_parts"], infra, frame, 0.0, 0.0, landuse=layers["land_use"])
    print(f"{a.name or 'airport'} at {a.lat}, {a.lon}: {len(keys)} cells" + ("" if res is None else
          f", {len(res)} airport footprints, {len(res.gates)} gates"))

    # the parked aircraft, masts and lead-ins as the game places them now (read only)
    infra_fn = lambda k: per[str(k)].get("infrastructure") if str(k) in per else \
        md.cell(k, layers=("infrastructure",)).layers.get("infrastructure")
    stands, every = [], []
    pa.STATS.clear()
    try:
        stands = pa.kept_stands(keys, infra_fn, pa.building_layers_fn(md))
        every = pa.lead_stands(keys, infra_fn, pa.building_layers_fn(md))
    except Exception as ex:                          # noqa: BLE001 (the picture without them)
        print("aircraft:", type(ex).__name__, ex)
    why = dict(pa.STATS)
    masts, leads, unreached, base, nw, site = [], [], [], set(), None, None
    try:
        for k in () if (a.lines or a.paint) else keys:
            f = infra_fn(k)
            if f is not None and pa.has_stands(f):
                blds = pa._flat([pa.building_layers_fn(md)(k)])
                for p in airsideprops.props(f, blds):
                    if p.group == airsideprops.MAST_GROUP:
                        e, n, _ = frame.geodetic_to_enu(np.array([p.lat]), np.array([p.lon]), 0.0)
                        masts.append((float(e[0]), float(n[0])))
        feats = [f for f in infra if pa.has_stands(f)]
        if a.paint:                                  # (the site as the tiles read it: every cell's buildings)
            site = taxiwaymarks.site_of(infra, layers["buildings"], layers["building_parts"]) if every else None
        elif every and feats:
            base = set(taxiwaymarks.strokes(feats))
            site = taxiwaymarks.site_of(infra, layers["buildings"], layers["building_parts"])
            leads = [s for s in taxiwaymarks.strokes(feats, every, site) if s not in base]
            nw = taxiwaymarks.network(feats, every, site)
            a.blocks = site.blocks                  # (the buildings the lines must keep out of, drawn in the --lines picture)
    except Exception as ex:                          # noqa: BLE001
        print("masts / lead-ins:", type(ex).__name__, ex)

    if a.paint:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        paint_picture(a, out, frame, infra, every, site, box, a.name or "airport")
        print("saved", out)
        return
    shown, whole = audit_view(nw, frame, box) if nw is not None else ({k: [] for k, _m, _c in MARKERS}, {k: 0 for k, _m, _c in MARKERS})
    print(f"audit (inside the picture; the whole loaded area): gaps {len(shown['gaps'])} ({whole['gaps']}), opens {len(shown['opens'])} "
          f"({whole['opens']}), kinks {len(shown['kinks'])} ({whole['kinks']}), S-bends {len(shown['sbends'])} ({whole['sbends']}); "
          f"final straight too short: {0 if nw is None else len(nw.short)}")
    for key, _m, _c in MARKERS:
        if shown[key]:
            print(f"  {key}: " + ", ".join("(" + ", ".join(f"{v:.0f}" if i < 2 else f"{v:.1f}" for i, v in enumerate(m)) + ")" for m in shown[key][:40]))
    if a.lines:
        if nw is None:
            sys.exit("no airport network here")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        lines_picture(a, out, frame, infra, nw, shown, box, a.name or "airport")
        print("saved", out)
        return

    fig = plt.figure(figsize=(16, 16), dpi=100)
    ax = fig.add_axes([0.02, 0.02, 0.96, 0.94])
    ax.set_facecolor("#e9efe3")
    ax.set_xlim(box[0], box[2])
    ax.set_ylim(box[1], box[3])
    ax.set_aspect("equal")
    ax.tick_params(labelsize=8)
    ax.grid(True, color="#ffffff", alpha=0.5, lw=0.5)

    def fill(rings, colour, z, **kw):
        ax.add_collection(PatchCollection([Polygon(r, closed=True) for r in rings], facecolor=colour, zorder=z, **kw))

    fill(pad_xy(frame, aeroways.pads(infra, runways=True)), "#6b6b6b", 1, edgecolor="none")     # runways (and all pads)
    fill(pad_xy(frame, aeroways.ways(infra)), "#9a9a9a", 2, edgecolor="none")
    fill(pad_xy(frame, aeroways.aprons(infra)), "#c3c3c3", 3, edgecolor="#8a8a8a", linewidth=0.6)

    units = sorted(res.units.values(), key=lambda u: (u.part, -u.area, u.id))     # (parents under their parts)
    for u in units:
        ax.add_patch(Polygon(u.ring, closed=True, facecolor=COLOURS[u.role], edgecolor="#222222" if u.part else "#555555",
                             linewidth=0.5 if u.part else 0.9, alpha=0.55 if (u.has_parts and not u.part) else 0.92,
                             zorder=4 if not u.part else 5))
    for p in res.airside_parking:
        ax.add_patch(Polygon(p["ring"], closed=True, facecolor="none", edgecolor="#7b2d9c", hatch="////", linewidth=0.8,
                             zorder=6))

    ends = []                                        # (the lead-ins' ends in metres: a stand's nose is reached by one)
    for s in leads:
        (la0, lo0, la1, lo1, _w, kind, _col) = s
        e, n, _ = frame.geodetic_to_enu(np.array([la0, la1]), np.array([lo0, lo1]), 0.0)
        if kind == taxiwaymarks.UNDER_KIND:                     # (the black borders: not drawn)
            continue
        ax.plot(e, n, color="#ffffff" if kind == taxiwaymarks.CENTRE_KIND else "#2b6cb0" if kind == taxiwaymarks.ROAD_KIND
                else "#ffe066", lw=0.9, zorder=7)
        if kind == taxiwaymarks.CENTRE_KIND:
            ends += [(float(e[0]), float(n[0])), (float(e[1]), float(n[1]))]
    kept = {(c.kind, round(c.lat, 7), round(c.lon, 7)) for c in stands}
    for c in every:
        rects, (cx, cy), (nx, ny) = aircraft_outline(c, frame)
        has = (c.kind, round(c.lat, 7), round(c.lon, 7)) in kept
        for r in rects:
            ax.add_patch(Polygon(r, closed=True, facecolor="none", edgecolor="#111111" if has else "#555555",
                                 linestyle="-" if has else (0, (3, 2)), linewidth=0.9, zorder=8))
        if c.mode in taxiwaymarks.LEAD_MODES:
            half = pa.SPECS[c.kind][0] / 2.0
            nose = (cx + nx * half, cy + ny * half)
            if not any(math.hypot(nose[0] - x, nose[1] - y) < 0.6 for x, y in ends):
                ax.scatter([nose[0]], [nose[1]], s=60, marker="o", facecolor="none", edgecolor="#e60000", linewidths=1.4,
                           zorder=12)
                unreached.append((c.kind, round(nose[0]), round(nose[1])))
    draw_edges(ax, frame, infra, zorder=7)
    draw_audit(ax, shown)
    if masts:
        ax.scatter([m[0] for m in masts], [m[1] for m in masts], marker="*", s=40, c="#ff00ff", edgecolor="k",
                   linewidths=0.3, zorder=9)

    # the gates and their jet bridges
    ac = [(c, aircraft_outline(c, frame)) for c in stands]
    st_all = [(c, aircraft_outline(c, frame)) for c in every if c.mode in taxiwaymarks.LEAD_MODES]
    rows = []
    for gid, g in sorted(res.gates.items()):
        x, y = g["xy"]
        if not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
            continue
        px, py, reach = (x, y, 15.0) if g["bridge"] is None else (*g["end"], 8.0)     # (a bridged gate: the aircraft beside its end)

        def covers(lst):
            for c, (_r, (cx, cy), (nx, ny)) in lst:
                if math.hypot(px - cx, py - cy) <= pa._radius(c.kind) + reach and bool(
                        pa._in_rects(c.kind, cx, cy, nx, ny, np.array([[px, py]]), reach)[0]):
                    return True
            return False
        has_ac, has_st = covers(ac), covers(st_all)
        rows.append((gid, g["bridge"] is not None, has_ac, has_st))
        if g["bridge"] is not None:
            ax.scatter([x], [y], s=34, facecolor="none", edgecolor="#006400", linewidths=1.5, zorder=10)
            ex, ey = g["end"]
            dx, dy = g["dir"]
            ax.annotate("", xy=(ex, ey), xytext=(ex - dx * 14.0, ey - dy * 14.0), zorder=11,
                        arrowprops=dict(arrowstyle="-|>", color="#8b0000", lw=1.6, mutation_scale=12))
        else:
            ax.scatter([x], [y], s=26, marker="x", c="#000000", linewidths=1.2, zorder=10)

    au = stand_audit(res, every, stands, frame, infra, nw, box)
    counts = audit_counts(au, len(rows), {k: len(v) for k, v in shown.items()})
    draw_audit_marks(ax, au)
    n_by = res.counts()
    handles = [Patch(facecolor=COLOURS[r], edgecolor="#333333", label=f"{r} ({n_by[r]})") for r in ar.ROLES]
    handles += [Patch(facecolor="#6b6b6b", label="runway / pads"), Patch(facecolor="#9a9a9a", label="taxiway"),
                Patch(facecolor="#c3c3c3", edgecolor="#8a8a8a", label="apron"),
                Patch(facecolor="none", edgecolor="#7b2d9c", hatch="////", label=f"airside parking ({len(res.airside_parking)})"),
                Line2D([], [], marker="o", mfc="none", mec="#006400", ls="", label="gate with jet bridge"),
                Line2D([], [], marker="x", mec="k", ls="", label="gate, no bridge"),
                Line2D([], [], marker=">", color="#8b0000", ls="", label="bridge outer end, arrow out"),
                Patch(facecolor="none", edgecolor="#111111", label=f"parked aircraft now ({len(stands)})"),
                Patch(facecolor="none", edgecolor="#555555", linestyle="--", label=f"empty stand ({len(every) - len(stands)})"),
                Line2D([], [], marker="o", mfc="none", mec="#e60000", ls="", label=f"no lead-in at the nose ({len(unreached)})"),
                Line2D([], [], marker="*", mfc="#ff00ff", mec="k", ls="", label=f"apron mast ({len(masts)})"),
                Line2D([], [], color="#ffffff", lw=1.2, label=f"stand lead-in ({len(leads)})"),
                *audit_handles(shown), *stand_handles(counts)]
    ax.legend(handles=handles, loc="lower right", fontsize=9, framealpha=0.9)
    if a.zoom:
        ax.set_xlim(a.zoom[0] - a.zoom[2], a.zoom[0] + a.zoom[2])
        ax.set_ylim(a.zoom[1] - a.zoom[2], a.zoom[1] + a.zoom[2])
    nb = sum(1 for r in rows if r[1])
    na = sum(1 for r in rows if r[2])
    ns = sum(1 for r in rows if r[3])
    fig.suptitle(f"{a.name or 'airport'}  ({a.lat:.4f}, {a.lon:.4f})  {a.km:g} km square, north up   gates {len(rows)}: "
                 f"{nb} with a bridge, {na} with an aircraft, {ns} with a stand", fontsize=13, y=0.985)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fig.savefig(out, dpi=100)
    print("roles:", ", ".join(f"{r} {n_by[r]}" for r in ar.ROLES))
    print(f"airside parking: {len(res.airside_parking)}; aircraft now: {len(stands)}; stands: {len(every)}; masts: {len(masts)}; "
          f"lead-in strokes: {len(leads)}; stands whose nose no lead-in reaches: {len(unreached)} {unreached}")
    print(f"gates in the picture: {len(rows)}, with a jet bridge {nb}, with an aircraft {na}, "
          f"with a stand {ns}, with both {sum(1 for r in rows if r[1] and r[2])}, "
          f"with neither {sum(1 for r in rows if not r[1] and not r[2])}")
    for gid, hb, hc, hs in rows:
        print(f"  gate {gid[:8]}: bridge {'yes' if hb else 'no '} aircraft {'yes' if hc else 'no '} stand {'yes' if hs else 'no'}"
              + (f"  [{res.gates[gid]['bridge'][:8]}, {res.gates[gid]['dist']:.0f} m]" if hb else ""))
    print_audit(a.name or "airport", au, counts)
    print("  why gates got no aircraft (parkedaircraft.STATS):", ", ".join(f"{k} {v}" for k, v in sorted(why.items())))
    if a.summary:
        import json
        with open(a.summary, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(counts, fh, indent=1)
    print("saved", out)


if __name__ == "__main__":
    main()
