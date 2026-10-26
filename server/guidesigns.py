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
# road (its name or route network says so), one open-road tolling gantry halfway along each segment of at least
# TOLL_MIN_M (between interchanges), and a CASHLESS TOLL sign 1/2 mile (800 m) before it.
TOLL_WORDS = ("turnpike", "tollway", "toll road", "toll highway", "thruway", "toll")
TOLL_MIN_M = 2500.0
TOLL_ADV_M = 804.672

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


def plan(roads, attrs, box, mph=True, left_hand=False, towns_xy=()):
    """The guide signs anchored in box (x0, y0, x1, y1): dicts {kind, x, y, z, d (travel unit vector), width (the
    carriageway), lanes, overhead, rows}. roads: build_tile's (fid, spec, centreline (k, 3), lon); attrs: fid ->
    {"routes", "dests"} (Overture's JSON text); towns_xy: [(x, y, name)] of the towns in the frame."""
    x0, y0, x1, y1 = box
    links = {}                  # fid -> (travel start xy, travel end xy) of the link roads
    for fid, spec, line, _ in roads:
        if spec.cls.endswith("_link") and len(line) >= 2:
            p = np.asarray(line, np.float64)
            ends = (p[0, :2], p[-1, :2]) if (spec.dir or 1) > 0 else (p[-1, :2], p[0, :2])
            links[fid] = ends
    out = []
    for fid, spec, line, _ in roads:
        if spec.cls not in MAIN or len(line) < 2:
            continue
        a = attrs.get(fid) or {}
        ways = ((spec.dir if spec.dir in (1, -1) else 1),) if spec.oneway else (1, -1)
        rts = routes(a.get("routes"))
        lanes = spec.lanes if spec.lanes else max(1, int(round(spec.width / 3.6)))
        if not spec.oneway:
            lanes = max(1, lanes // 2)
        for way in ways:
            p = _ordered(line, way)
            arc = _arc(p)
            L = float(arc[-1])
            # an exit: a destination rule onto a link road, or a link leaving the segment's downstream end (its
            # destinations then those of a rule onto a road out of sight, if any)
            rules = exits(a.get("dests"), way)
            found = next(((nm, sh) for to, nm, sh in rules if to in links), None)
            if found is None:
                touching = [lf for lf, (st, _en) in sorted(links.items()) if math.hypot(*(st - p[-1, :2])) <= LINK_TOUCH_M]
                if touching:
                    found = next(((nm, sh) for to, nm, sh in rules if to not in attrs), (["EXIT"], []))
            signs = []
            if found is not None:
                names, shields = found
                if names == ["EXIT"] and not shields:
                    names = []
                for dist, label in (ADVANCE_MPH if mph else ADVANCE_KM):
                    if L - dist >= END_CLEAR_M:
                        signs.append(("advance", L - dist, names, shields, label))
                if L >= 2 * EXIT_BEFORE_M:
                    signs.append(("exit", L - EXIT_BEFORE_M, names, shields, "EXIT"))
            if spec.cls == "motorway" and L >= TOLL_MIN_M and is_toll(a.get("name"), a.get("routes")):
                signs.append(("toll", L / 2, [], [], None))
                if L / 2 - TOLL_ADV_M >= END_CLEAR_M:
                    signs.append(("tolladv", L / 2 - TOLL_ADV_M, [], [], "1/2 MILE" if mph else "800 m"))
            merged = any(math.hypot(*(en - p[0, :2])) <= LINK_TOUCH_M for _st, en in links.values())
            if rts and merged and L >= ROUTE_AT_M + 300.0:
                signs.append(("route", ROUTE_AT_M, [], rts[:2], None))
                if towns_xy and L >= MILEAGE_AT_M + 200.0:
                    q, d = _at(p, MILEAGE_AT_M, arc)
                    ahead = towns_ahead(q[:2], d, towns_xy, not mph)
                    if len(ahead) >= 2:
                        signs.append(("mileage", MILEAGE_AT_M, ahead, [], None))
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


def panel_size(rows):
    w = max(sum(_item_width(i) for i in r) + GAP_M * (len(r) - 1) for r in rows)
    h = sum(max(_item_height(i) for i in r) for r in rows) + GAP_M * (len(rows) - 1)
    return w + 2 * PAD_M, h + 2 * PAD_M


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


def _panel(m, base, R, F, w, h, rows):
    """A panel w x h (m) whose front-bottom-centre is base, facing F (unit, toward the driver), R to the driver's
    right; its rows of text over the green, a white border."""
    U = np.array([0.0, 0.0, 1.0])

    def pt(u, v, f=0.0):
        return base + R * u + U * v + F * f
    green, white = _sprite_uv("green"), _sprite_uv("white")
    m.box(pt(0, h / 2, -0.05), R * (w / 2), F * 0.05, U * (h / 2), STEEL)
    m.quad([pt(-w / 2, 0, 0.002), pt(w / 2, 0, 0.002), pt(w / 2, h, 0.002), pt(-w / 2, h, 0.002)], green)
    b, e = 0.05, 0.09
    for (u0, v0, u1, v1) in ((-w / 2 + e, e, w / 2 - e, e + b), (-w / 2 + e, h - e - b, w / 2 - e, h - e),
                             (-w / 2 + e, e, -w / 2 + e + b, h - e), (w / 2 - e - b, e, w / 2 - e, h - e)):
        m.quad([pt(u0, v0, 0.004), pt(u1, v0, 0.004), pt(u1, v1, 0.004), pt(u0, v1, 0.004)], white)
    v = h - PAD_M
    for r in rows:
        rh = max(_item_height(i) for i in r)
        if r[0][0] == "pair":
            rw = w - 2 * PAD_M
        else:
            rw = sum(_item_width(i) for i in r) + GAP_M * (len(r) - 1)
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
            u += iw + GAP_M
        v -= rh + GAP_M


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
    bottom = GROUND_M
    pu = side * (half + SHOULDER_M + w / 2)
    base = c + R * pu + U * bottom
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
        w, h = panel_size(g["rows"])
        base, F, R = _structure(m, g, w, h)
        _panel(m, base, R, F, w, h, g["rows"])
        if len(m.P) > MAX_VERTS:            # over the shape's index limit: this sign left out
            del m.P[n0:], m.UV[n0:]
            k = next(i for i, t in enumerate(m.T) if t[0] >= n0)
            del m.T[k:], m.M[k:]
            break
    if not m.P:
        return None
    return (np.asarray(m.P, np.float32), np.asarray(m.T, np.int32), np.asarray(m.UV, np.float32),
            np.asarray(m.M, np.int32))
