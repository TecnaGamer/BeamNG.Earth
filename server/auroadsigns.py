"""three Australian roadside signs (tools/build_au_roadsigns.py), modelled on tboards.py: the same Forest rows as
warnsigns.py (called from blockworker._kantstolpe_rows for Australia), each worked out from the roads alone, so a tile
keeps the signs whose point it owns and tiles agree.

Chevron alignment markers (AS 1742.2 D4-6): on the outside of a tight bend of a sealed rural road, a run of 3-6 yellow
plates on single poles, each pointing the way the road turns, one run for each way of travel (facing its approaching
traffic; on a two-way road the second run stands on the same outer edge, staggered half a spacing between the first's).
Not on town streets, roads with footpaths, ramps or motorways, nor on roundabouts (a short, tight, closed-ish arc), nor
within CAM_JUNCTION_M of another road (a junction, a T board).

Wrong Way Go Back: where a one-way motorway ramp ends on an ordinary road, a red board beside the ramp's last metres,
facing the road (the traffic that would enter the ramp the wrong way). Not where the ramp ends on a motorway, or where
the board would stand on another road.

Gravel road: where a sealed rural road ends at a gravel / dirt one (roads.Spec.kind, from OSM's surface tag or the
regional gravel default) that carries on from it, a red board GRAVEL ROAD / DRIVE TO PREVAILING ROAD CONDITIONS on the sealed
side, GRAVEL_BACK_M before the change, facing the traffic heading onto the gravel (not the other way, nor where the road
leaves traffic only the other way, nor where another sealed road shares the junction).
"""
import numpy as np

import motesfri
import tboards
import warnsigns

COUNTRIES = warnsigns.COUNTRIES
GROUPS = ("au_chevron_l", "au_chevron_r", "au_wrongway", "au_gravel")
HEIGHTS = {"au_chevron_l": 1.6, "au_chevron_r": 1.6, "au_wrongway": 2.1, "au_gravel": 2.5}    # (the models', build_au_roadsigns.py)
JOIN_M = 4.0                    # m: a road's end this near another's point meets it
ROAD_CLEAR_M = 1.0              # m a board stands clear of another road's edge

# --- chevron alignment markers ---
CAM_CLASSES = {"trunk", "primary", "secondary", "tertiary", "unclassified"}
CAM_NOT_CLASSES = {"motorway", "service", "track"}       # (a fast road of another class takes them too, but not these)
CAM_MIN_KMH = 80.0
CAM_RADIUS_M = 300.0            # a bend tighter than this (median radius over WINDOW_M of road) ...
CAM_MIN_DEG = 20.0              # ... turning the road at least this much
CAM_SPACING_PER_R = 0.2         # spacing = this x radius, within CAM_SPACING_M
CAM_SPACING_M = (20.0, 40.0)
CAM_COUNT = (3, 6)
CAM_OUT_M = 1.5                 # m outside the sealed edge (spec.width includes the sealed shoulders)
CAM_JUNCTION_M = 30.0           # m: no sign this near another road
CAM_EDGE_M = 5.0                # m: none this near a road's end
LOOP_M = 5.0                    # a road whose ends are this near is a loop (a roundabout ring)
ROUNDABOUT = (250.0, 60.0, 60.0)    # a road shorter than this (m) with a bend tighter than this (m) turning it this much (degrees)

# --- wrong way ---
WW_CLASSES = {"motorway", "motorway_link"}
WW_RAMP_CLASSES = {"trunk", "trunk_link"}                # (a trunk ramp only outside towns: a slip lane in town is not one)
WW_MIN_RAMP_M = 20.0
WW_BACK_M = 4.0                 # m back along the ramp from the edge of the road it ends on
WW_OUT_M = 2.0                  # m beside the ramp's sealed edge (the driver's left)

# --- gravel road ---
GRAVEL_KINDS = {"gravel", "dirt"}
GRAVEL_NOT_CLASSES = {"track", "service", "path", "footway", "cycleway", "pedestrian", "steps"}
GRAVEL_BACK_M = 40.0            # m before the change
GRAVEL_MIN_M = 15.0             # a sealed road shorter than this has no room for the sign
GRAVEL_OUT_M = 2.0


def _clean(xy):
    xy = np.asarray(xy, np.float64)[:, :2]
    if len(xy) < 2:
        return xy
    seg = np.hypot(*np.diff(xy, axis=0).T)
    return xy[np.r_[True, seg > 1e-6]]


def _point_at(xy, cum, d):
    """(point, unit tangent) of polyline xy (k, 2), cumulative length cum, d m along it."""
    d = min(max(d, 0.0), float(cum[-1]))
    k = min(max(int(np.searchsorted(cum, d, side="right")) - 1, 0), len(cum) - 2)
    t = (xy[k + 1] - xy[k]) / max(float(cum[k + 1] - cum[k]), 1e-9)
    return xy[k] + t * (d - cum[k]), t


def _near(points, lines, reach):
    """Whether any of points (n, 2) is within reach (m) of any line (xy, extra) of lines: reach + extra."""
    lo, hi = points.min(0), points.max(0)
    for xy, extra in lines:
        r = reach + extra
        if (xy.max(0) < lo - r).any() or (xy.min(0) > hi + r).any():
            continue
        if (motesfri._distance(points, xy) <= r).any():
            return True
    return False


def _others(roads, skip):
    return [(xy, spec.width / 2) for fid, spec, xy in roads if fid not in skip and len(xy) >= 2]


# --- chevron alignment markers ---

def cam_wanted(spec):
    """Whether a road of this spec takes chevrons: sealed, rural, no footpaths, not a ramp, a numbered class or unclassified,
    or any other fast one."""
    if spec.kind != "paved" or spec.urban or spec.sides or spec.link:
        return False
    if spec.cls in CAM_CLASSES:
        return True
    return spec.cls not in CAM_NOT_CLASSES and spec.kmh is not None and spec.kmh >= CAM_MIN_KMH


def chevrons(road, roads):
    """[(group, point (2,), unit travel direction (2,))] of the chevron markers of road (id, spec, xy) among roads
    [(id, spec, xy)] (every road near it, itself too). Pure."""
    fid, spec, xy = road
    if not cam_wanted(spec):
        return []
    xy = _clean(xy)
    if len(xy) < 2:
        return []
    cum = tboards._cum(xy)
    length = float(cum[-1])
    if length > 50.0 and float(np.hypot(*(xy[0] - xy[-1]))) < LOOP_M:
        return []
    found, _ = warnsigns.bends(xy, CAM_RADIUS_M, CAM_MIN_DEG)
    if not found:
        return []
    others = _others(roads, {fid})
    ways = (1.0, -1.0) if not spec.oneway else ((-1.0,) if int(spec.dir) < 0 else (1.0,))
    out = []
    for s0, s1, turning, deflection, radius in found:
        if length < ROUNDABOUT[0] and radius < ROUNDABOUT[1] and deflection >= ROUNDABOUT[2]:
            continue
        spacing = float(np.clip(CAM_SPACING_PER_R * radius, *CAM_SPACING_M))
        n = int(np.clip(round((s1 - s0 + warnsigns.STEP_M) / spacing) + 1, *CAM_COUNT))
        mid = (s0 + s1) / 2
        for way in ways:
            stagger = 0.0 if way > 0 else spacing / 2
            kept = []
            for k in range(n):
                d = mid + (k - (n - 1) / 2) * spacing + stagger
                if d < CAM_EDGE_M or d > length - CAM_EDGE_M:
                    continue
                p, t = _point_at(xy, cum, d)
                outward = np.array([t[1], -t[0]]) if turning > 0 else np.array([-t[1], t[0]])     # (away from the bend's centre)
                q = p + outward * (spec.width / 2 + CAM_OUT_M)
                if others and _near(q[None], others, CAM_JUNCTION_M):
                    continue
                left = (turning > 0) == (way > 0)         # (the bend turns left for this way of travel)
                kept.append((f"au_chevron_{'l' if left else 'r'}", q, t * way))
            if len(kept) >= CAM_COUNT[0]:
                out += kept
    return out


# --- wrong way go back ---

def wrongway(road, roads):
    """[(point (2,), unit travel direction (2,))] of the Wrong Way sign of road (id, spec, xy) if it is a one-way motorway
    ramp ending on an ordinary road: the sign faces the traffic that would enter the ramp the wrong way. Pure."""
    fid, spec, xy = road
    if not (spec.link and spec.oneway and spec.dir and spec.kind == "paved"):
        return []
    if not (spec.cls in WW_CLASSES or (spec.cls in WW_RAMP_CLASSES and not spec.urban)):
        return []
    xy = _clean(xy)
    if len(xy) < 2:
        return []
    cum = tboards._cum(xy)
    length = float(cum[-1])
    if length < WW_MIN_RAMP_M:
        return []
    first = int(spec.dir) < 0                       # (the ramp's end in its way of travel)
    e = xy[0 if first else -1]
    out_dir = tboards._tangent_at_end(xy, first)
    if out_dir is None:
        return []
    half, ordinary = 0.0, False
    for f2, s2, p2 in roads:
        if f2 == fid or len(p2) < 2 or tboards._nearest(e, p2)[0] > JOIN_M:
            continue
        if s2.cls in WW_CLASSES and not s2.link:
            return []                                # (it ends on a motorway: an entry ramp, or a merge)
        if not s2.link and s2.cls not in WW_CLASSES:
            ordinary = True
            half = max(half, s2.width / 2)
    if not ordinary:
        return []
    back = min(half + WW_BACK_M, length / 2)
    d_at = length - back if not first else back     # (along xy)
    p, t = _point_at(xy, cum, d_at)
    t = t if not first else -t                      # (the ramp's way of travel there)
    left = np.array([-t[1], t[0]])
    q = p + left * (spec.width / 2 + WW_OUT_M)
    side = np.array([-t[1], t[0]])
    spots = np.array([q + side * 0.6 * k for k in (-1.0, 0.0, 1.0)])
    if _near(spots, _others(roads, {fid}), ROAD_CLEAR_M):
        return []
    return [(q, -t)]


# --- gravel road ---

def gravel(road, roads):
    """[(point (2,), unit travel direction (2,))] of the gravel road signs of the sealed road (id, spec, xy) among roads
    [(id, spec, xy)]: one for each end of it where traffic heads onto a gravel / dirt road carrying on from it. Pure."""
    fid, spec, xy = road
    if spec.kind != "paved" or spec.urban or spec.sides or spec.link or spec.cls in ("motorway",):
        return []
    xy = _clean(xy)
    if len(xy) < 2:
        return []
    cum = tboards._cum(xy)
    length = float(cum[-1])
    if length < GRAVEL_MIN_M:
        return []
    ends = (True, False) if not spec.oneway else ((True,) if int(spec.dir) < 0 else (False,))
    out = []
    for first in ends:
        e = xy[0 if first else -1]
        d = tboards._tangent_at_end(xy, first)           # (out of the sealed road: the way the driver is heading)
        if d is None:
            continue
        onto, sealed_else = False, False
        for f2, s2, p2 in roads:
            if f2 == fid or len(p2) < 2:
                continue
            p2 = _clean(p2)
            if len(p2) < 2:
                continue
            ends2 = [(f2e, p2[0 if f2e else -1]) for f2e in (True, False)]
            if tboards._nearest(e, p2)[0] > JOIN_M:
                continue
            if s2.kind == "paved" and not s2.link and s2.cls not in GRAVEL_NOT_CLASSES:
                sealed_else = True                        # (another sealed road shares the junction: not a change of surface)
                break
            if s2.kind not in GRAVEL_KINDS or s2.link or s2.cls in GRAVEL_NOT_CLASSES:
                continue
            for f2e, pt in ends2:
                if float(np.hypot(*(pt - e))) > JOIN_M:
                    continue
                into = tboards._tangent_at_end(p2, f2e)   # (out of the gravel road's end, towards the junction)
                if into is None or float((-into) @ d) <= 0.0:
                    continue                              # (the gravel road does not carry on from here)
                if s2.oneway and (int(s2.dir) > 0) != f2e:
                    continue                              # (its traffic runs towards the junction, not onto it)
                onto = True
        if sealed_else or not onto:
            continue
        back = min(GRAVEL_BACK_M, length - 5.0)
        p, t = _point_at(xy, cum, length - back if not first else back)
        t = t if not first else -t                      # (towards the gravel)
        q = p + np.array([-t[1], t[0]]) * (spec.width / 2 + GRAVEL_OUT_M)
        side = np.array([-t[1], t[0]])
        spots = np.array([q + side * 0.6 * k for k in (-1.0, 0.0, 1.0)])
        if _near(spots, _others(roads, {fid}), ROAD_CLEAR_M):
            continue
        out.append((q, t))
    return out


def tile_rows(frame, near_roads, box, elev, country):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of the chevron, Wrong Way and gravel road signs of road
    tile box: near_roads build_tile's roads_out, elev(lat, lon) the ground. [] outside Australia."""
    if (country or "").upper() not in COUNTRIES:
        return []
    x0, y0, x1, y1 = box
    roads = [(fid, spec, np.asarray(pts, np.float64)[:, :2]) for fid, spec, pts, *_ in near_roads if len(pts) >= 2]
    found = []
    for road in roads:
        found += chevrons(road, roads)
        found += [("au_wrongway", q, t) for q, t in wrongway(road, roads)]
        found += [("au_gravel", q, t) for q, t in gravel(road, roads)]
    out = []
    for group, q, travel in found:
        if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
            continue
        la, lo, _ = frame.enu_to_geodetic(q[None, 0], q[None, 1], np.zeros(1))
        z = float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])
        out.append(warnsigns._row(group, q, travel, z, HEIGHTS[group]))
    return out
