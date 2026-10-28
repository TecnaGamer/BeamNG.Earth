"""Green guide signs on motorways and trunk roads (the user, 2026-10-25: "big green over head signs", Pennsylvania's
general highway guide signs: "exits, mileage, and directions").

Where they stand, from each road's own data (Overture segments: routes, destinations; overture.py):
- exit direction signs: a segment whose destinations lead onto a link road (or, without destinations, a link that
  leaves its downstream end) gets advance signs 1 MILE and 1/2 MILE before the exit (1000 m / 500 m where km are
  posted) and an EXIT sign with an up-right arrow at the split; on carriageways of 3 or more lanes the advance signs
  hang over the right lanes from a truss gantry, elsewhere they stand on two posts at the shoulder;
- route signs: after a merge (a link joining the segment's start) the route's shield(s) and direction (I-76 EAST),
  and further on a mileage sign naming the next towns ahead with their distances (the populated places the level
  select uses, randomplace.towns).
Every sign is a pure function of the segment's geometry and attributes (ROADMAP "deterministic generation"); a road
tile builds the signs whose anchor is in its box. The mesh (mesh()) is one shape per tile: the panels and their text
from one atlas (guide_atlas.png / .json: tools/generate_guide_atlas.py) and the steel structure, solid like the
sidewalks (roads.lua 'guide').
"""
import json
import math
import os
import sys

import numpy as np

ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATLAS_JSON = os.path.join(ROOT, "mod", "beamng_earth", "levels", "beamng_earth", "art", "shapes", "beamngEarth",
                          "guide_atlas.json")
MATERIALS = ["bne_guide_atlas", "bne_guide_steel", "m_gm_concrete_plain"]   # (concrete: rumble.py's bars)
ATLAS, STEEL, CONCRETE = 0, 1, 2

MAIN = ("motorway", "trunk")
LINK_TOUCH_M = 4.0          # a link's end this close to a segment's end joins it there
ADVANCE_MPH = ((1609.344, "1 MILE"), (804.672, "1/2 MILE"))
ADVANCE_KM = ((1000.0, "1000 m"), (500.0, "500 m"))
EXIT_BEFORE_M = 40.0        # the EXIT sign this far before the split
END_CLEAR_M = 60.0          # no advance sign nearer a segment's start than this
ROUTE_AT_M = 250.0          # the route sign this far past a merge ...
MILEAGE_AT_M = 650.0        # ... the mileage sign this far
TOWN_MIN_M, TOWN_MAX_M, TOWN_CONE = 5000.0, 200000.0, 30.0
OVERHEAD_LANES = 3
CLEAR_M = 5.4               # overhead panel's bottom over the road (17 ft 8 in: MUTCD's 17 ft minimum)
GROUND_M = 2.1              # a ground-mounted panel's bottom over the road
SHOULDER_M = 1.8            # ground posts' panel edge beyond the carriageway
GANTRY_POST_M = 2.2         # gantry posts beyond the carriageway's edges
TEXT_M, SMALL_M = 0.62, 0.46    # glyph cell heights (m): destinations, distance / direction lines
SHIELD_M = 1.05
PAD_M, GAP_M = 0.30, 0.16
MAX_VERTS = 60000           # one shape's vertices (the engine's 16-bit indices)
# Toll gantries (the user, 2026-10-25: "on named toll roads"; Overture has toll booths but no gantries): on a toll
# road (its name or route network says so), open-road tolling gantries far apart (TOLL_GRID_DEG: the user, 2026-10-25,
# "the tolls were too frequent") and a CASHLESS TOLL sign 1/2 mile (800 m) before each.
TOLL_WORDS = ("turnpike", "tollway", "toll road", "toll highway", "thruway", "toll")
TOLL_GRID_DEG = 0.2         # a gantry where the road passes a multiple of this latitude / longitude
TOLL_CLEAR_M = 800.0        # ... this far from an interchange's ramps (and the road's ends)
TOLL_GAP_M = 5000.0         # ... and from another gantry (two grid lines close together)
TOLL_ADV_M = 804.672
JOIN_M = 6.0                # a ramp's end this far beyond the carriageway's edge joins it
EXIT_GAP_M = 300.0          # ramp ends this close together: one junction
REACH_M = 2000.0            # roads farther than this from the tile: none of their signs land in it
# Location signs (the user, 2026-10-25: "the location signs and regular dot signs"): county lines from Overture's
# county outlines (divisions), green ENTERING X COUNTY on the smaller roads, the blue Welcome sign on the main ones;
# mileposts every mile on Interstates and US routes, numbered from the state's south / west edge (an estimate:
# the real ones count along the route from where it enters the state)
COUNTY_CLASSES = ("motorway", "trunk", "primary", "secondary", "tertiary")
WELCOME_CLASSES = ("motorway", "trunk", "primary")
MILE_CLASSES = ("motorway", "trunk", "primary")
SAMPLE_M = 20.0
MILE_M = (0.16, 0.24)       # milepost glyphs: MILE, the number
MILE_BOTTOM_M = 1.2
STATE_EDGES = {     # (south latitude, west longitude) of each state (approximate)
    "AL": (30.2, -88.5), "AZ": (31.3, -114.8), "AR": (33.0, -94.6), "CA": (32.5, -124.4), "CO": (37.0, -109.05),
    "CT": (41.0, -73.7), "DE": (38.45, -75.8), "DC": (38.8, -77.12), "FL": (24.5, -87.6), "GA": (30.36, -85.6),
    "ID": (42.0, -117.24), "IL": (36.97, -91.5), "IN": (37.77, -88.1), "IA": (40.37, -96.64), "KS": (37.0, -102.05),
    "KY": (36.5, -89.57), "LA": (28.9, -94.04), "ME": (43.06, -71.08), "MD": (37.9, -79.49), "MA": (41.24, -73.5),
    "MI": (41.7, -90.42), "MN": (43.5, -97.24), "MS": (30.17, -91.65), "MO": (35.99, -95.77), "MT": (44.36, -116.05),
    "NE": (40.0, -104.05), "NV": (35.0, -120.0), "NH": (42.7, -72.56), "NJ": (38.93, -75.56), "NM": (31.33, -109.05),
    "NY": (40.5, -79.76), "NC": (33.84, -84.32), "ND": (45.94, -104.05), "OH": (38.4, -84.82), "OK": (33.62, -103.0),
    "OR": (41.99, -124.57), "PA": (39.72, -80.52), "RI": (41.15, -71.86), "SC": (32.03, -83.35), "SD": (42.48, -104.06),
    "TN": (34.98, -90.31), "TX": (25.84, -106.65), "UT": (37.0, -114.05), "VT": (42.73, -73.44), "VA": (36.54, -83.68),
    "WA": (45.54, -124.85), "WV": (37.2, -82.64), "WI": (42.49, -92.89), "WY": (41.0, -111.06)}

_atlas = None


def atlas():
    global _atlas
    if _atlas is None:
        with open(ATLAS_JSON, encoding="utf-8") as f:
            _atlas = json.load(f)
    return _atlas


# --- the data ------------------------------------------------------------------------------------------------------
def _json(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(str(v)) if str(v).strip() else None
    except ValueError:
        return None


def shield_of(network, ref):
    """(kind, number) of a route for its shield: Interstate, US route or a US state route; None elsewhere."""
    n, r = str(network or ""), str(ref or "").strip()
    if not r or not n.startswith("US:"):
        return None
    num = r.split()[-1].lstrip("I-").strip()
    if not num or len(num) > 4:
        return None
    if n == "US:I" or n.startswith("US:I:"):
        return ("interstate", num)
    if n == "US:US" or n.startswith("US:US:"):
        return ("us", num)
    return ("state", num)


def routes(v):
    """[(kind, number)] of a segment's routes (Overture routes: network, ref), shields only, de-duplicated."""
    out = []
    for r in _json(v) or []:
        if isinstance(r, dict):
            s = shield_of(r.get("network"), r.get("ref"))
            if s and s not in out:
                out.append(s)
    return out


def _label_shield(value):
    """A destination label of type route_ref ("I 376", "US 30", "PA 28") as a shield, or None."""
    p = str(value or "").replace("-", " ").split()
    if len(p) < 2:
        return None
    net = {"I": "US:I", "US": "US:US"}.get(p[0].upper(), "US:" + p[0].upper() if len(p[0]) == 2 else "")
    return shield_of(net, p[-1])


def exits(v, way):
    """The destination rules of a segment for travel along (way +1) or against (-1) its geometry: [(to segment id,
    [names], [shields])]."""
    out = []
    for rule in _json(v) or []:
        if not isinstance(rule, dict):
            continue
        h = str(((rule.get("when") or {}).get("heading")) or "").lower()
        if (h == "forward" and way < 0) or (h == "backward" and way > 0):
            continue
        names, shields = [], []
        for lab in rule.get("labels") or []:
            if not isinstance(lab, dict):
                continue
            val, typ = str(lab.get("value") or "").strip(), str(lab.get("type") or "")
            if not val:
                continue
            if "route_ref" in typ:
                s = _label_shield(val)
                if s and s not in shields:
                    shields.append(s)
            elif val not in names:
                names.append(val)
        if names or shields:
            out.append((str(rule.get("to_segment_id") or ""), names[:2], shields[:2]))
    return out


# --- where -----------------------------------------------------------------------------------------------------------
def _ordered(line, way):
    p = np.asarray(line, np.float64)
    return p if way > 0 else p[::-1].copy()


def _arc(p):
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p[:, :2], axis=0).T))])


def _at(p, s, arc):
    s = min(max(s, 0.0), float(arc[-1]))
    k = int(np.clip(np.searchsorted(arc, s, side="right") - 1, 0, len(p) - 2))
    L = max(float(arc[k + 1] - arc[k]), 1e-9)
    t = (s - arc[k]) / L
    q = p[k] + (p[k + 1] - p[k]) * t
    d = (p[k + 1, :2] - p[k, :2]) / L
    return q, d


def _cardinal(shield, d):
    """EAST / WEST for an even route number, NORTH / SOUTH for an odd one, from the travel direction d (x east)."""
    try:
        even = int("".join(c for c in shield[1] if c.isdigit()) or "0") % 2 == 0
    except ValueError:
        even = True
    if even:
        return "EAST" if d[0] >= 0 else "WEST"
    return "NORTH" if d[1] >= 0 else "SOUTH"


def towns_ahead(xy, d, towns_xy, km):
    """[(name, distance as posted)] of up to three towns ahead of xy along d (TOWN_CONE degrees, TOWN_MIN_M..MAX_M),
    nearest first; towns_xy [(x, y, name)] in the frame."""
    got = []
    for x, y, name in towns_xy:
        vx, vy = x - xy[0], y - xy[1]
        dist = math.hypot(vx, vy)
        if not TOWN_MIN_M <= dist <= TOWN_MAX_M:
            continue
        if (vx * d[0] + vy * d[1]) / dist < math.cos(math.radians(TOWN_CONE)):
            continue
        got.append((dist, name))
    got.sort()
    out, seen = [], set()
    for dist, name in got:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, str(int(round(dist / 1000.0 if km else dist / 1609.344)))))
        if len(out) == 3:
            break
    return out


def is_toll(name, routes_json):
    """Whether a road is a toll road by its name or its routes' networks / names (Pennsylvania Turnpike, I-90 Tollway)."""
    words = [str(name or "")]
    for r in _json(routes_json) or []:
        if isinstance(r, dict):
            words += [str(r.get("network") or ""), str(r.get("name") or "")]
    t = " ".join(words).lower()
    return any(w in t for w in TOLL_WORDS)


def _project(p, arc, q):
    """(arc position, distance, unit direction there) of the point of polyline p nearest q."""
    a, b = p[:-1, :2], p[1:, :2]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((q - a) * ab).sum(1) / L2, 0.0, 1.0)
    dd = np.hypot(*(a + ab * t[:, None] - q).T)
    k = int(np.argmin(dd))
    Lk = math.sqrt(L2[k])
    return float(arc[k] + t[k] * Lk), float(dd[k]), ab[k] / Lk


def _cluster(ss, gap):
    out = []
    for s in sorted(ss):
        if not out or s - out[-1] > gap:
            out.append(s)
    return out


def _grid_crossings(ll, step):
    """Indices k where the polyline's lat/lon (ll: (lat (n,), lon (n,))) passes a multiple of step between point k and
    k + 1, with the fraction along that edge."""
    out = []
    for v in ll:
        g = np.floor(np.asarray(v, np.float64) / step)
        for k in np.nonzero(np.diff(g) != 0)[0]:
            line = max(g[k], g[k + 1]) * step
            den = v[k + 1] - v[k]
            out.append((int(k), float((line - v[k]) / den) if den else 0.5))
    return out


def county_index(counties, pts):
    """Per point (n, 2) the index of the county (counties: [(name, region, [(outer Path, [hole Path])])]) it lies in,
    -1 for none."""
    pts = np.asarray(pts, np.float64)
    got = np.full(len(pts), -1, np.int64)
    for ci, (_n, _r, polys) in enumerate(counties):
        for outer, holes in polys:
            ins = outer.contains_points(pts)
            for h in holes:
                ins &= ~h.contains_points(pts)
            got[ins & (got < 0)] = ci
    return got


def mile_of(lat, lon, shield, region):
    """The approximate milepost of a point on route shield (kind, number) in state region ("US-PA"): miles from the
    state's south edge on an odd (north-south) route, from its west edge on an even one; None outside STATE_EDGES."""
    st = str(region or "")[-2:].upper()
    edge = STATE_EDGES.get(st)
    if edge is None:
        return None
    try:
        even = int("".join(c for c in shield[1] if c.isdigit()) or "0") % 2 == 0
    except ValueError:
        return None
    if even:
        return (lon - edge[1]) * 69.17 * math.cos(math.radians(lat))
    return (lat - edge[0]) * 69.05


def plan(roads, attrs, box, mph=True, left_hand=False, towns_xy=(), geo=None, counties=()):
    """The guide signs anchored in box (x0, y0, x1, y1): dicts {kind, x, y, z, d (travel unit vector), width (the
    carriageway), lanes, overhead, rows}. roads: build_tile's (fid, spec, centreline (k, 3), lon); attrs: fid ->
    {"routes", "dests"} (Overture's JSON text); towns_xy: [(x, y, name)] of the towns in the frame; geo(x, y) ->
    (lat, lon, ...) of frame points (the toll grid and mileposts; None: neither); counties: county_index's."""
    x0, y0, x1, y1 = box
    links = {}                  # fid -> (start xy, start dir, end xy, end dir) in travel order
    for fid, spec, line, _ in roads:
        if spec.cls.endswith("_link") and len(line) >= 2:
            p = _ordered(line, 1 if (spec.dir or 1) > 0 else -1)[:, :2]
            d0, d1 = p[1] - p[0], p[-1] - p[-2]
            links[fid] = (p[0], d0 / max(np.hypot(*d0), 1e-9), p[-1], d1 / max(np.hypot(*d1), 1e-9))
    out = []
    for fid, spec, line, _ in roads:
        if len(line) < 2:
            continue
        pa = np.asarray(line, np.float64)
        if (pa[:, 0].max() < x0 - REACH_M or pa[:, 0].min() > x1 + REACH_M or pa[:, 1].max() < y0 - REACH_M
                or pa[:, 1].min() > y1 + REACH_M):
            continue
        main = spec.cls in MAIN
        located = spec.cls in COUNTY_CLASSES
        if not (main or located):
            continue
        a = attrs.get(fid) or {}
        ways = ((spec.dir if spec.dir in (1, -1) else 1),) if spec.oneway else (1, -1)
        rts = routes(a.get("routes"))
        lanes = spec.lanes if spec.lanes else max(1, int(round(spec.width / 3.6)))
        if not spec.oneway:
            lanes = max(1, lanes // 2)
        reach = spec.width / 2 + JOIN_M
        for way in ways:
            p = _ordered(line, way)
            arc = _arc(p)
            L = float(arc[-1])
            if L < 1.0:
                continue
            signs = []
            if main:
                # ramps leaving (a link starting beside the road, heading the same way) and joining it, anywhere
                # along the road: Overture's segments run through interchanges
                rules = exits(a.get("dests"), way)
                outs, ins = {}, []
                for lf, (st, sd, en, ed) in sorted(links.items()):
                    s, dist, dr = _project(p, arc, st)
                    if dist <= reach and float(np.dot(sd, dr)) >= 0.5 and s > 1.0:
                        outs.setdefault(round(s, 1), []).append(lf)
                    s, dist, dr = _project(p, arc, en)
                    if dist <= reach and float(np.dot(ed, dr)) >= 0.5 and s < L - 1.0:
                        ins.append(s)
                kept = _cluster(outs, EXIT_GAP_M)
                for sd_ in kept:
                    lfs = [lf for s, v in outs.items() if sd_ <= s <= sd_ + EXIT_GAP_M for lf in v]
                    found = next(((nm, sh) for to, nm, sh in rules if to in lfs), None)
                    if found is None:
                        found = next(((nm, sh) for to, nm, sh in rules if to not in attrs), ([], []))
                    names, shields = found
                    for dist, label in (ADVANCE_MPH if mph else ADVANCE_KM):
                        if sd_ - dist >= END_CLEAR_M:
                            signs.append(("advance", sd_ - dist, names, shields, label))
                    if sd_ >= EXIT_BEFORE_M + 5.0:
                        signs.append(("exit", sd_ - EXIT_BEFORE_M, names, shields, "EXIT"))
                joins = _cluster(ins, EXIT_GAP_M)
                for sm in joins:
                    if rts and sm + ROUTE_AT_M <= L - 100.0:
                        signs.append(("route", sm + ROUTE_AT_M, [], rts[:2], None))
                        if towns_xy and sm + MILEAGE_AT_M <= L - 100.0:
                            q, d = _at(p, sm + MILEAGE_AT_M, arc)
                            ahead = towns_ahead(q[:2], d, towns_xy, not mph)
                            if len(ahead) >= 2:
                                signs.append(("mileage", sm + MILEAGE_AT_M, ahead, [], None))
                if (geo is not None and spec.cls == "motorway" and L >= 2 * TOLL_CLEAR_M
                        and is_toll(a.get("name"), a.get("routes"))):
                    # open-road tolling: where the road passes a TOLL_GRID_DEG line of latitude or longitude (one
                    # gantry every ~15-20 km, the same wherever the road is cut), clear of the interchanges
                    ll = geo(p[:, 0], p[:, 1])
                    busy = kept + joins
                    done = []
                    for k, f in _grid_crossings((ll[0], ll[1]), TOLL_GRID_DEG):
                        s = float(arc[k] + (arc[k + 1] - arc[k]) * f)
                        if s < TOLL_CLEAR_M or s > L - 200.0 or any(abs(s - b) < TOLL_CLEAR_M for b in busy):
                            continue
                        if any(abs(s - b) < TOLL_GAP_M for b in done):
                            continue
                        done.append(s)
                        signs.append(("toll", s, [], [], None))
                        if s - TOLL_ADV_M >= END_CLEAR_M:
                            signs.append(("tolladv", s - TOLL_ADV_M, [], [], "1/2 MILE" if mph else "800 m"))
            if located and (counties or geo is not None):
                signs += _located(p, arc, L, spec, rts, mph, geo, counties)
            for kind, s, names, shields, label in signs:
                q, d = _at(p, s, arc)
                if not (x0 <= q[0] < x1 and y0 <= q[1] < y1):
                    continue
                rows = _rows(kind, names, shields, label, d)
                if not rows:
                    continue
                out.append({"kind": kind, "x": round(float(q[0]), 2), "y": round(float(q[1]), 2),
                            "z": round(float(q[2]), 2), "d": [round(float(d[0]), 5), round(float(d[1]), 5)],
                            "width": float(spec.width), "lanes": int(lanes), "left_hand": bool(left_hand),
                            "overhead": kind == "advance" and lanes >= OVERHEAD_LANES, "rows": rows,
                            "id": f"{fid}:{way}:{kind}:{int(s)}"})
    out.sort(key=lambda g: g["id"])
    return out


def _located(p, arc, L, spec, rts, mph, geo, counties):
    """The county line signs (where the road enters another county) and the mileposts (Interstates and US routes) of
    one direction of a road: [(kind, s, names, shields, label)]."""
    n = max(2, int(L / SAMPLE_M) + 1)
    ss = np.linspace(0.0, L, n)
    xs, ys = np.interp(ss, arc, p[:, 0]), np.interp(ss, arc, p[:, 1])
    if not counties:
        ci = np.full(n, -1)
    elif len({c[0] for c in counties}) == 1:     # one county around: no line to sign (its state for the mileposts)
        ci = np.zeros(n, np.int64)
    else:
        ci = county_index(counties, np.c_[xs, ys])
    signs = []
    for k in np.nonzero((ci[:-1] >= 0) & (ci[1:] >= 0))[0]:
        a, b = counties[ci[k]], counties[ci[k + 1]]
        if a[0] == b[0] or not b[0]:
            continue
        s = float(ss[k + 1]) + 15.0
        if s > L - 5.0:
            continue
        kind = "welcome" if spec.cls in WELCOME_CLASSES else "county"
        signs.append((kind, s, [b[0]], [], None))
    if not mph or geo is None or spec.cls not in MILE_CLASSES:
        return signs
    sh = next((r for r in rts if r[0] == "interstate"), None) or next((r for r in rts if r[0] == "us"), None)
    if sh is None:
        return signs
    lat, lon = (np.asarray(v, np.float64) for v in geo(xs, ys)[:2])
    for k in range(n - 1):
        c = ci[k] if ci[k] >= 0 else ci[k + 1]
        if c < 0:
            continue
        m0 = mile_of(lat[k], lon[k], sh, counties[c][1])
        m1 = mile_of(lat[k + 1], lon[k + 1], sh, counties[c][1])
        if m0 is None or m1 is None or math.floor(m0) == math.floor(m1) or min(m0, m1) < 0:
            continue
        mile = int(max(math.floor(m0), math.floor(m1)))
        f = (mile - m0) / (m1 - m0) if m1 != m0 else 0.5
        signs.append(("mile", float(ss[k] + (ss[k + 1] - ss[k]) * f), [str(mile)], [sh], None))
    return signs


def _rows(kind, names, shields, label, d):
    """A panel's rows: [[item...]...], items ("text", s, size) / ("shield", kind, num) / ("arrow", name) /
    ("pair", left, right)."""
    rows = []
    if kind in ("advance", "exit"):
        if shields:
            rows.append([("shield", k, n) for k, n in shields])
        for n in names:
            rows.append([("text", n, TEXT_M)])
        if not rows:
            rows.append([("text", "Exit", TEXT_M)])
        if kind == "exit":
            rows.append([("text", "EXIT", SMALL_M), ("arrow", "arrow_exit")])
        else:
            rows.append([("text", label, SMALL_M)])
    elif kind == "route":
        for k, n in shields:
            rows.append([("shield", k, n), ("text", _cardinal((k, n), d), SMALL_M)])
    elif kind == "mileage":
        rows = [[("pair", name, dist)] for name, dist in names]
    elif kind == "tolladv":
        rows = [[("text", "CASHLESS TOLL", TEXT_M)], [("text", "NO STOPPING", SMALL_M)], [("text", label, SMALL_M)]]
    elif kind == "toll":
        rows = [["gantry"]]        # (no panel: toll_gantry draws it)
    elif kind == "county":
        nm = str(names[0]).strip()
        core = nm[:-len(" county")].strip() if nm.lower().endswith(" county") else ""
        rows = [[("text", "ENTERING", SMALL_M)], [("text", (core or nm).upper(), TEXT_M)]]
        if core:
            rows.append([("text", "COUNTY", SMALL_M)])
    elif kind == "welcome":
        rows = [[("text", "Welcome to", SMALL_M)], [("text", str(names[0]).strip(), TEXT_M)]]
    elif kind == "mile":
        rows = [[("text", "MILE", MILE_M[0])], [("text", str(names[0]), MILE_M[1])]]
    return rows


# --- the mesh --------------------------------------------------------------------------------------------------------
def _glyph(colour, ch):
    g = atlas()["glyphs"][colour]
    return g.get(ch) or g.get(ch.upper()) or g[" "]


def text_width(s, size, colour="white"):
    cell = atlas()["cell"]
    return sum(_glyph(colour, c)["adv"] for c in s) * size / cell


def _item_width(it):
    if it[0] == "text":
        return text_width(it[1], it[2])
    if it[0] == "shield":
        return SHIELD_M
    if it[0] == "arrow":
        return SMALL_M * 1.4
    if it[0] == "pair":
        return text_width(it[1], TEXT_M) + 1.2 + text_width(it[2], TEXT_M)
    return 0.0


def _item_height(it):
    return {"text": it[2] if it[0] == "text" else 0, "shield": SHIELD_M, "arrow": SMALL_M * 1.4,
            "pair": TEXT_M}.get(it[0], 0.0)


def _pad(kind):
    return (0.07, 0.05) if kind == "mile" else (PAD_M, GAP_M)


def panel_size(rows, kind=None):
    pad, gap = _pad(kind)
    w = max(sum(_item_width(i) for i in r) + gap * (len(r) - 1) for r in rows)
    h = sum(max(_item_height(i) for i in r) for r in rows) + gap * (len(rows) - 1)
    return w + 2 * pad, h + 2 * pad


class _Mesh:
    def __init__(self, origin):
        self.o = np.asarray(origin, np.float64)
        self.P, self.UV, self.T, self.M = [], [], [], []

    def quad(self, c, uv=None, mat=ATLAS):
        """c: 4 corners counter-clockwise seen from the front; uv: (u0, v0, u1, v1) of the atlas (None: (0, 0))."""
        n = len(self.P)
        for q in c:
            self.P.append(np.asarray(q, np.float64) - self.o)
        if uv is None:
            self.UV.extend([(0.0, 0.0)] * 4)
        else:
            u0, v0, u1, v1 = uv
            self.UV.extend([(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
        self.T.extend([(n, n + 1, n + 2), (n, n + 2, n + 3)])
        self.M.extend([mat, mat])

    def box(self, c, a, b, h, mat=STEEL, uv=None):
        """A box at centre c with half-axes a, b, h (vectors); every face outward."""
        c, a, b, h = (np.asarray(v, np.float64) for v in (c, a, b, h))
        for ax, u, v in ((a, b, h), (-a, h, b), (b, h, a), (-b, a, h), (h, a, b), (-h, b, a)):
            f = c + ax
            q = [f - u - v, f + u - v, f + u + v, f - u + v]
            if np.dot(np.cross(u, v), ax) < 0:
                q = q[::-1]
            self.quad(q, uv, mat)


def _uv(r, inset=0.0):
    W, H = atlas()["size"]
    x0, y0, x1, y1 = r
    return ((x0 + inset) / W, 1 - (y1 - inset) / H, (x1 - inset) / W, 1 - (y0 + inset) / H)


def _sprite_uv(name):
    return _uv(atlas()["sprites"][name], 6)


def _panel(m, base, R, F, w, h, rows, kind=None):
    """A panel w x h (m) whose front-bottom-centre is base, facing F (unit, toward the driver), R to the driver's
    right; its rows of text over the green (a welcome sign: the blue), a white border."""
    U = np.array([0.0, 0.0, 1.0])
    pad, gap = _pad(kind)

    def pt(u, v, f=0.0):
        return base + R * u + U * v + F * f
    green, white = _sprite_uv("navy" if kind == "welcome" else "green"), _sprite_uv("white")
    m.box(pt(0, h / 2, -0.05), R * (w / 2), F * 0.05, U * (h / 2), STEEL)
    m.quad([pt(-w / 2, 0, 0.002), pt(w / 2, 0, 0.002), pt(w / 2, h, 0.002), pt(-w / 2, h, 0.002)], green)
    b, e = (0.015, 0.025) if kind == "mile" else (0.05, 0.09)
    for (u0, v0, u1, v1) in ((-w / 2 + e, e, w / 2 - e, e + b), (-w / 2 + e, h - e - b, w / 2 - e, h - e),
                             (-w / 2 + e, e, -w / 2 + e + b, h - e), (w / 2 - e - b, e, w / 2 - e, h - e)):
        m.quad([pt(u0, v0, 0.004), pt(u1, v0, 0.004), pt(u1, v1, 0.004), pt(u0, v1, 0.004)], white)
    v = h - pad
    for r in rows:
        rh = max(_item_height(i) for i in r)
        if r[0][0] == "pair":
            rw = w - 2 * pad
        else:
            rw = sum(_item_width(i) for i in r) + gap * (len(r) - 1)
        u = -rw / 2
        for it in r:
            iw, ih = _item_width(it), _item_height(it)
            vb = v - rh + (rh - ih) / 2
            if it[0] == "text":
                _text(m, pt, u, vb, it[1], it[2], "white")
            elif it[0] == "pair":
                _text(m, pt, u, vb, it[1], TEXT_M, "white")
                _text(m, pt, u + rw - text_width(it[2], TEXT_M), vb, it[2], TEXT_M, "white")
            elif it[0] == "arrow":
                m.quad([pt(u, vb, 0.006), pt(u + iw, vb, 0.006), pt(u + iw, vb + ih, 0.006), pt(u, vb + ih, 0.006)],
                       _sprite_uv(it[1]))
            elif it[0] == "shield":
                kind, num = it[1], it[2]
                m.quad([pt(u, vb, 0.006), pt(u + iw, vb, 0.006), pt(u + iw, vb + ih, 0.006), pt(u, vb + ih, 0.006)],
                       _sprite_uv(kind))
                size = ih * (0.42 if len(num) <= 2 else 0.32)
                tw = text_width(num, size, "black" if kind != "interstate" else "white")
                nv = vb + ih * (0.22 if kind == "interstate" else 0.3)
                _text(m, pt, u + (iw - tw) / 2, nv, num, size, "black" if kind != "interstate" else "white", 0.008)
            u += iw + gap
        v -= rh + gap


def _text(m, pt, u, v, s, size, colour, f=0.006):
    A = atlas()
    k = size / A["cell"]
    for ch in s:
        g = _glyph(colour, ch)
        if g["r"] is not None:
            x0, y0, x1, y1 = g["r"]
            w = (x1 - x0) * k
            m.quad([pt(u, v, f), pt(u + w, v, f), pt(u + w, v + size, f), pt(u, v + size, f)], _uv(g["r"]))
        u += g["adv"] * k


def _structure(m, g, w, h):
    """The sign's steel and its panel: a truss gantry over the carriageway, or two posts at the shoulder. Returns the
    panel's front-bottom-centre, F and R."""
    c = np.array([g["x"], g["y"], g["z"]])
    d = np.array([g["d"][0], g["d"][1], 0.0])
    R = np.array([d[1], -d[0], 0.0])                # the driver's right
    F = -d                                          # toward the driver
    U = np.array([0.0, 0.0, 1.0])
    side = -1.0 if g["left_hand"] else 1.0          # the shoulder the signs stand on
    half = g["width"] / 2
    if g["overhead"]:
        lo = c + U * CLEAR_M
        pu = side * (half - w / 2 - 0.3)            # over the outer (exit side) lanes
        pu = max(-half + w / 2, min(half - w / 2, pu)) if w < 2 * half else 0.0
        top = CLEAR_M + max(h, 1.6)
        span = half + GANTRY_POST_M
        back = -0.45                                # the truss behind the panel
        for zz in (CLEAR_M + 0.35, top - 0.35):     # chords
            m.box(c + U * zz + F * back, R * span, F * 0.09, U * 0.09)
        n = max(2, int(2 * span / 1.6))
        for i in range(n + 1):                      # webs
            uu = -span + 2 * span * i / n
            m.box(c + R * uu + U * ((CLEAR_M + top) / 2) + F * back, R * 0.04, F * 0.04, U * ((top - CLEAR_M) / 2 - 0.3))
        for uu in (-span, span):                    # posts
            m.box(c + R * uu + U * ((top + 0.2) / 2) + F * back, R * 0.18, F * 0.18, U * ((top + 0.2) / 2))
        return lo + R * pu, F, R
    bottom = MILE_BOTTOM_M if g["kind"] == "mile" else GROUND_M
    pu = side * (half + SHOULDER_M + w / 2)
    base = c + R * pu + U * bottom
    if g["kind"] == "mile":                         # one small post
        m.box(c + R * pu + U * ((bottom + h) / 2) + F * -0.05, R * 0.03, F * 0.03, U * ((bottom + h) / 2))
        return base, F, R
    for uu in (-w * 0.3, w * 0.3):                  # two I-beam posts behind the panel
        m.box(c + R * (pu + uu) + U * ((bottom + h) / 2) + F * -0.16, R * 0.08, F * 0.06, U * ((bottom + h) / 2))
    return base, F, R


def toll_gantry(m, g):
    """An open-road tolling gantry over the carriageway: a truss on two posts, a reader and a camera over each lane."""
    c = np.array([g["x"], g["y"], g["z"]])
    d = np.array([g["d"][0], g["d"][1], 0.0])
    R, F, U = np.array([d[1], -d[0], 0.0]), -d, np.array([0.0, 0.0, 1.0])
    half = g["width"] / 2
    span = half + GANTRY_POST_M
    top = CLEAR_M + 1.4
    for zz in (CLEAR_M + 0.5, top - 0.2):
        for ff in (-0.5, 0.5):
            m.box(c + U * zz + F * ff, R * span, F * 0.08, U * 0.08)
    n = max(2, int(2 * span / 1.6))
    for i in range(n + 1):
        uu = -span + 2 * span * i / n
        for ff in (-0.5, 0.5):
            m.box(c + R * uu + U * ((CLEAR_M + 0.5 + top - 0.2) / 2) + F * ff, R * 0.04, F * 0.04,
                  U * ((top - 0.2 - CLEAR_M - 0.5) / 2))
    for uu in (-span, span):
        for ff in (-0.5, 0.5):
            m.box(c + R * uu + U * (top / 2) + F * ff, R * 0.16, F * 0.16, U * (top / 2))
    black = _sprite_uv("black")
    lanes = max(1, g["lanes"])
    for k in range(lanes):
        uu = -half + (k + 0.5) * g["width"] / lanes
        m.box(c + R * uu + U * (CLEAR_M + 0.95) + F * 0.0, R * 0.35, F * 0.28, U * 0.22, ATLAS, black)   # reader
        m.box(c + R * uu + U * (CLEAR_M + 0.25) + F * 0.35, R * 0.12, F * 0.18, U * 0.12, ATLAS, black)  # camera


def _rumble(m, g):
    """rumble.py's bars, all at once: each a box's top and its two faces across the road (the ones a tyre meets); no
    bottom (on the ground) and no ends (along the strip: a tyre never meets them)."""
    import rumble
    c, d = np.asarray(g["c"], np.float64), np.asarray(g["d"], np.float64)
    n = len(c)
    if not n:
        return
    a = np.column_stack([d * (rumble.BAR_ALONG_M / 2), np.zeros(n)])
    b = np.column_stack([np.stack([-d[:, 1], d[:, 0]], axis=1) * (rumble.BAR_ACROSS_M / 2), np.zeros(n)])
    up = np.array([0.0, 0.0, rumble.BAR_H_M])
    lo = np.stack([c - a - b, c + a - b, c + a + b, c - a + b], axis=1)          # (n, 4, 3)
    pts = np.concatenate([lo, lo + up], axis=1) - m.o                             # (n, 8, 3)
    base = len(m.P)
    m.P.extend(pts.reshape(-1, 3))
    m.UV.extend([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)] * (2 * n))
    # top (4 5 6 7), the face at -a (0 3 7 4) and at +a (1 5 6 2), counter-clockwise from outside
    local = np.array([(4, 5, 6), (4, 6, 7), (0, 4, 7), (0, 7, 3), (1, 2, 6), (1, 6, 5)])
    tris = (base + 8 * np.arange(n))[:, None, None] + local[None]
    m.T.extend(map(tuple, tris.reshape(-1, 3)))
    m.M.extend([CONCRETE] * (6 * n))


def mesh(signs, origin):
    """(positions (n, 3), tris (m, 3), uvs (n, 2), tri material indices (m,)) of the signs relative to origin, or
    None; materials: MATERIALS."""
    if not signs:
        return None
    m = _Mesh(origin)
    signs = [g for g in signs if g["kind"] != "rumble"] + [g for g in signs if g["kind"] == "rumble"]
    for g in signs:
        n0 = len(m.P)
        if g["kind"] == "rumble":
            _rumble(m, g)
            if len(m.P) > MAX_VERTS:
                del m.P[n0:], m.UV[n0:]
                k = next(i for i, t in enumerate(m.T) if t[0] >= n0)
                del m.T[k:], m.M[k:]
            continue
        if g["kind"] == "toll":
            toll_gantry(m, g)
            continue
        w, h = panel_size(g["rows"], g["kind"])
        base, F, R = _structure(m, g, w, h)
        _panel(m, base, R, F, w, h, g["rows"], g["kind"])
        if len(m.P) > MAX_VERTS:            # over the shape's index limit: this sign left out
            del m.P[n0:], m.UV[n0:]
            k = next(i for i, t in enumerate(m.T) if t[0] >= n0)
            del m.T[k:], m.M[k:]
            break
    if not m.P:
        return None
    return (np.asarray(m.P, np.float32), np.asarray(m.T, np.int32), np.asarray(m.UV, np.float32),
            np.asarray(m.M, np.int32))
