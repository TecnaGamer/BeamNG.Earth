"""Bulgaria's roadside signs that a road's own shape and the map's towns call for (signstream.py, country BG only; drawn
into the sign tile's "speeds" mesh, render-only, under the highwaySigns switch). Rules: Наредба № РД-02-21-1 от 23
ноември 2023 г. за сигнализация на пътищата с пътни знаци (ДВ 101/2023, in force 07.08.2024; it replaced Наредба № 18
от 2001) and the Закон за транслитерацията.

    С7      "Табела с направляваща стрелка": a white square with a red chevron, on the outside of a dangerous bend
            (чл. 201 (2): obligatory there; чл. 24 (1) 3: a radius of 200 m or less turning 30° or more; the other two
            tests, sight distance and a slowdown of more than 20 km/h, need data the map has not), each post 0.50 m
            past the edge line (чл. 201 (5) 1), the first and last where the straight edge lines meet that line (2),
            spaced Sa = L / round(L / Sc) by the radius (annex 23 в); on a two-way road two boards at a right angle
            on each post, one for each direction (annex 23 а). Lower edge 1.1-1.4 m above the road (чл. 19 (10)).
            Rural roads only (in a town the bends are junctions').
    Д11     "Начало на населено място": the place's name in black on white inside a black border (чл. 105, annex 5),
            just before the built-up area (чл. 119 (1)): where a road passes from other land cover into built-up
            land cover (WorldCover 50, as roads.road_specs' urban), for the traffic going in, on its right; the name
            repeated in Latin letters (Закон за транслитерацията) on a republican road (чл. 119 (2): trunk to
            tertiary here; on a municipal road "if needed": not).
    Д12     "Край на населеното място": the same with a red diagonal from the top right to the bottom left (чл. 105 (3)
            1), for the traffic coming out, on its right (чл. 120).
The place: the towns of Overture's divisions on disk (placenames.py's cells, which the status bar fetches round the
player: none fetched here), the one placenames.pick names a point TOWN_IN_M inside. Sizes: БДС 1517 (not open) has
them; the С7 board and the lettering here are the common 0.6 m and 0.15 m, the panel sized to its text.
Tile independence: every place comes from whole map lines; a tile keeps the posts in its box.
"""
import json
import math
import os
import re
from functools import lru_cache

import numpy as np

import dmath
import dnp
import exitsigns

COUNTRIES = {"BG"}
MATERIALS = ['bne_exit_white', 'bne_guide_red', 'bne_exit_metal', 'bne_bg_letters_black', 'bne_guide_black']
WHITE, RED, METAL, LETTERS, BLACK = range(5)
GLYPHS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bg_sign_glyphs.json")   # build_bg_sign_glyphs.py

# С7
C7_CLASSES = {"trunk", "primary", "secondary", "tertiary", "unclassified"}
C7_MAX_R = 200.0            # m (чл. 24 (1) 3)
C7_MIN_TURN = math.radians(30.0)
C7_STEP_M = 5.0             # m between the points a bend is read at
C7_WINDOW_M = 20.0          # m over which a point's radius is read
C7_SIDE_M = 0.50            # m past the edge line to the boards' line
C7_SIZE_M = 0.60            # m, the board's side
C7_BOTTOM_M = 1.2           # m above the road (1.1-1.4)
C7_SC = ((30, 6), (40, 8), (50, 10), (60, 12), (70, 14), (80, 16), (90, 18), (100, 20), (200, 30), (300, 45), (400, 60),
         (500, 75), (600, 90))  # annex 23 в: R m -> Sc m, interpolated
# Д11 / Д12
TOWN_CLASSES = {"trunk", "primary", "secondary", "tertiary", "unclassified"}
LATIN_CLASSES = {"trunk", "primary", "secondary", "tertiary"}      # republican roads (чл. 119 (2))
TOWN_STEP_M = 10.0          # m between the land cover samples along a road
TOWN_MIN_RUN_M = 80.0       # m: a shorter run of built-up or other land cover joins its neighbours
TOWN_OUT_M = 150.0          # m of other land cover before the built-up area, at least ...
TOWN_IN_M = 150.0           # ... and of built-up land cover after it (the place is named this far in)
TOWN_BEFORE_M = 10.0        # m before the built-up area the sign stands ("непосредствено преди")
TOWN_SIDE_M = 1.0           # m past the carriageway's edge (чл. 19 (2): 0.5-2.0)
TOWN_BOTTOM_M = 1.5         # m above the road outside a town (чл. 19 (4): 1.5-2.0)
TOWN_CLASS_OK = {"megacity", "city", "town", "village", "hamlet"}  # Overture's classed localities (not districts)
CAP_M = 0.15                # m, the lettering's cap height
LINE_M = 0.30               # m, one line of text to the next
MARGIN_M = 0.12             # m round the text inside the border
BORDER_M = (0.025, 0.05)    # m: the border's inset from the panel's edge, its inner edge
TRACK = 0.06                # letter spacing, of the cap height (the drawing's lettering is wide set)

# Закон за транслитерацията чл. 4, 5 (2) (final "ия" -> "ia"), 6 (България -> Bulgaria)
_LATIN = dict(zip("абвгдежзийклмнопрстуфхцчшщъьюя",
                  ["a", "b", "v", "g", "d", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
                   "f", "h", "ts", "ch", "sh", "sht", "a", "y", "yu", "ya"]))
_LATIN.update({"ѝ": "i", "ё": "yo", "ы": "y", "э": "e"})      # (not in the Act's table: foreign names' letters)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _word(w):
    if w.lower() == "българия":
        return "BULGARIA" if w.isupper() and len(w) > 1 else "Bulgaria"
    low = w.lower()
    end_ia = low.endswith("ия")
    out = []
    for k, c in enumerate(w):
        lc = c.lower()
        if end_ia and k >= len(w) - 2:
            latin = "ia"[k - (len(w) - 2)]
        else:
            latin = _LATIN.get(lc, c if not ("Ѐ" <= c <= "ӿ") else "")
        if c != lc:                     # capital: Zh, or ZH inside an all-capital word
            upper_word = w.isupper() and len(w) > 1
            latin = latin.upper() if upper_word else latin[:1].upper() + latin[1:]
        out.append(latin)
    return "".join(out)


def translit(name):
    """name in Latin letters by the Закон за транслитерацията (other characters kept). Pure."""
    return _WORD.sub(lambda m: _word(m.group(0)), str(name or ""))


def cyrillic(name):
    return any("Ѐ" <= c <= "ӿ" for c in str(name or ""))


# --- С7 -----------------------------------------------------------------------------------------------------------

def sc_of(radius):
    """Annex 23 в's spacing Sc (m) of С7 boards on a bend of radius m (interpolated, held at the table's ends)."""
    rs, ss = zip(*C7_SC)
    return float(dnp.interp(radius, rs, ss))


def _resample(xy, step):
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    if cum[-1] < 2 * step:
        return None, None
    s = np.arange(0.0, cum[-1] + 1e-9, step)
    return np.column_stack([dnp.interp(s, cum, xy[:, 0]), dnp.interp(s, cum, xy[:, 1])]), s


def bends(xy):
    """The dangerous bends of polyline xy (k, 2) (чл. 24 (1) 3): [(s0, s1, radius, turn sign)], arc lengths along it
    where the bend starts and ends, its mean radius (arc / turn), +1 left / -1 right along the line. Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    p, s = _resample(xy, C7_STEP_M)
    if p is None:
        return []
    d = np.diff(p, axis=0)
    head = np.unwrap(dnp.arctan2(d[:, 1], d[:, 0]))
    w = max(1, int(round(C7_WINDOW_M / C7_STEP_M / 2)))
    k = np.arange(len(head))
    a, b = np.clip(k - w, 0, len(head) - 1), np.clip(k + w, 0, len(head) - 1)
    rate = (head[b] - head[a]) / np.maximum((b - a) * C7_STEP_M, 1e-9)       # rad / m
    sharp = np.abs(rate) >= 1.0 / C7_MAX_R
    out = []
    j = 0
    while j < len(head):
        if not sharp[j]:
            j += 1
            continue
        sign = 1 if rate[j] > 0 else -1
        e = j
        while e + 1 < len(head) and sharp[e + 1] and (rate[e + 1] > 0) == (sign > 0):
            e += 1
        turn = abs(head[e] - head[j]) + abs(rate[j]) * C7_STEP_M          # (the first step's own turn)
        length = (e - j + 1) * C7_STEP_M
        if turn >= C7_MIN_TURN:
            out.append((float(s[j]), float(s[min(e + 1, len(s) - 1)]), length / turn, sign))
        j = e + 1
    return out


def _at(xy, s):
    """(point, unit tangent) s m along polyline xy."""
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    k = min(max(int(np.searchsorted(cum, s, side="right")) - 1, 0), len(seg) - 1)
    t = (xy[k + 1] - xy[k]) / max(float(seg[k]), 1e-9)
    return xy[k] + t * (s - cum[k]), t


def c7_posts(xy, width, oneway=False, direction=1):
    """The С7 posts of a road along polyline xy of carriageway width m: [(point (2,), [(face normal (2,), arrow +1
    right / -1 left as its driver sees it)], the bend's outside (2,))]. direction: a one-way road's travel along xy
    (+1 / -1). Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    out = []
    for s0, s1, radius, sign in bends(xy):
        r_line = radius + width / 2 + C7_SIDE_M         # the boards' line round the outside of the bend
        # the straight edge line meets the boards' line this far into the bend (an angle, both ends)
        lead = dmath.acos(min(1.0, (radius + width / 2) / r_line)) * radius
        a, b = s0 + lead, s1 - lead
        if b <= a:
            a = b = (s0 + s1) / 2
        n = max(1, int(round((b - a) / sc_of(radius)))) if b > a else 0
        for m in range(n + 1):
            s = a + (b - a) * m / max(n, 1)
            p, t = _at(xy, s)
            left = np.array([-t[1], t[0]])
            outside = -sign * left                        # (a left bend's outside is on the right)
            q = p + outside * (width / 2 + C7_SIDE_M + 0.05)
            boards = []
            for travel in ((direction,) if oneway else (1, -1)):
                tt = t * travel
                face = -tt - outside                       # towards the traffic and the road: two at a right angle
                face /= dnp.norm(face)
                turn = sign * travel                       # +1: the road turns left for that traffic
                boards.append((face, -turn))
            out.append((q, boards, outside))
    return out


# --- Д11 / Д12 ----------------------------------------------------------------------------------------------------

def _runs(flags, step, min_run):
    """flags with runs shorter than min_run m given to their neighbours, the shortest first (both ends kept as they
    are). Pure."""
    f = np.asarray(flags, bool).copy()
    while True:
        edges = np.flatnonzero(np.diff(f.astype(np.int8))) + 1
        starts, ends = np.r_[0, edges], np.r_[edges, len(f)]
        inner = np.arange(1, len(starts) - 1)
        if not len(inner):
            return f
        lengths = ends[inner] - starts[inner]
        k = inner[np.argmin(lengths)]
        if lengths.min() * step >= min_run:
            return f
        f[starts[k]:ends[k]] = not f[starts[k]]


def town_edges(xy, built):
    """Where polyline xy (k, 2) passes into a built-up area: [(s, direction into it +1 along xy / -1 against)], built
    the built-up flags of xy resampled every TOWN_STEP_M m (_resample). Pure."""
    f = _runs(built, TOWN_STEP_M, TOWN_MIN_RUN_M)
    out = []
    edges = np.flatnonzero(np.diff(f.astype(np.int8))) + 1
    starts, ends = np.r_[0, edges], np.r_[edges, len(f)]
    for k in range(len(starts) - 1):
        a, mid, b = starts[k], ends[k], ends[k + 1]
        before, after = (mid - a) * TOWN_STEP_M, (b - mid) * TOWN_STEP_M
        into = 1 if f[mid] else -1               # built-up after the change: going in along the line
        outside, inside = (before, after) if into > 0 else (after, before)
        if outside >= TOWN_OUT_M and inside >= TOWN_IN_M:
            out.append((float(mid * TOWN_STEP_M), into))
    return out


def town_signs(xy, width, cls, oneway, direction, built, name_at):
    """The Д11 / Д12 signs of one road: [dict(kind "d11" / "d12", point, travel, name, latin)]. built: flags at
    _resample(xy, TOWN_STEP_M)'s points; name_at(point) -> the place's name or "". Pure."""
    xy = np.asarray(xy, np.float64)[:, :2]
    out = []
    total = float(dnp.hypot(*np.diff(xy, axis=0).T).sum())
    for s, into in town_edges(xy, built):
        at = s - into * TOWN_BEFORE_M
        p, t = _at(xy, min(max(at, 0.0), total))
        travel_in = t * into
        inner, _ = _at(xy, min(max(s + into * TOWN_IN_M, 0.0), total))
        name = name_at(inner)
        if not name:
            continue
        latin = translit(name) if cls in LATIN_CLASSES and cyrillic(name) else ""
        for kind, tr in (("d11", travel_in), ("d12", -travel_in)):
            if oneway and not dnp.dot(tr, t * direction) > 0:
                continue
            right = np.array([tr[1], -tr[0]])
            out.append({"kind": kind, "point": p + right * (width / 2 + TOWN_SIDE_M), "travel": tr, "name": name,
                        "latin": latin})
    return out


# --- meshes -------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def lettering():
    with open(GLYPHS, encoding="utf-8") as f:
        return json.load(f)


def _chars(value):
    g = lettering()["glyphs"]
    return "".join(c for c in str(value) if c in g)


def text_width(value, cap=CAP_M):
    data = lettering()
    v = _chars(value)
    sc = cap / data["capHeight"]
    return (sum(data["glyphs"][c]["advance"] for c in v) * sc + max(len(v) - 1, 0) * TRACK * cap) if v else 0.0


class Mesh(exitsigns.Mesh):
    def place(self, centre, face):
        """The next shape's frame: centre (3,) its ground point, face (2,) its front's normal (towards the viewer)."""
        face = np.asarray(face, float)
        self.centre = np.asarray(centre, float)
        self.face = np.r_[face, 0.0]
        self.right = np.array([-face[1], face[0], 0.0])     # the viewer's right

    def letters(self, value, baseline, cap, depth):
        data = lettering()
        v = _chars(value)
        sc = cap / data["capHeight"]
        y = -text_width(v, cap) / 2
        for c in v:
            g = data["glyphs"][c]
            l, t, r, b = g["bbox"]
            u0, v0, u1, v1 = g["uv"]
            if r > l and b > t:
                self.quad([(depth, y + l * sc, baseline - b * sc), (depth, y + r * sc, baseline - b * sc),
                           (depth, y + r * sc, baseline - t * sc), (depth, y + l * sc, baseline - t * sc)],
                          LETTERS, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)])
            y += g["advance"] * sc + TRACK * cap

    def post(self, top, width=0.06):
        self.box((-0.09, -width / 2, -0.3), (-0.09 + width, width / 2, top), METAL)

    def ring(self, w, bottom, h, m0, m1, depth, material):
        outer = self.outline(w - 2 * m0, bottom + m0, h - 2 * m0, max(0.02, 0.06 - m0))
        inner = self.outline(w - 2 * m1, bottom + m1, h - 2 * m1, max(0.01, 0.06 - m1))
        for i in range(len(outer)):
            j = (i + 1) % len(outer)
            self.quad([(depth, *outer[i]), (depth, *outer[j]), (depth, *inner[j]), (depth, *inner[i])], material)

    def town(self, sign, ground_z, road_z):
        lines = [x for x in (sign["name"], sign["latin"]) if x]
        w = max(text_width(x) for x in lines) + 2 * (MARGIN_M + BORDER_M[1])
        h = (len(lines) - 1) * LINE_M + CAP_M + 2 * (MARGIN_M + BORDER_M[1]) + 0.04
        bottom = max(ground_z, road_z) + TOWN_BOTTOM_M - ground_z
        self.place(np.r_[sign["point"], ground_z], -np.asarray(sign["travel"], float))
        for off in (-w / 2 + 0.25, w / 2 - 0.25) if w > 1.2 else (0.0,):
            self.box((-0.09, off - 0.03, -0.3), (-0.03, off + 0.03, bottom + h - 0.05), METAL)
        self.rounded(w, bottom, h, 0.06, 0.05, METAL, back=True, front=False)
        self.rounded(w, bottom, h, 0.06, 0.05, WHITE)
        self.ring(w, bottom, h, BORDER_M[0], BORDER_M[1], 0.052, BLACK)
        top = bottom + h - BORDER_M[1] - MARGIN_M
        for k, line in enumerate(lines):
            self.letters(line, top - CAP_M - k * LINE_M, CAP_M, 0.054)
        if sign["kind"] == "d12":       # the red band, top right to bottom left
            a, b = np.array([w / 2 - 0.05, bottom + h - 0.05]), np.array([-w / 2 + 0.05, bottom + 0.05])
            d = (a - b) / dnp.norm(a - b)
            n = np.array([-d[1], d[0]]) * 0.035
            self.quad([(0.056, *(b - n)), (0.056, *(a - n)), (0.056, *(a + n)), (0.056, *(b + n))], RED)

    def c7(self, point, boards, outside, ground_z, road_z):
        """A post's С7 boards: two hinged at the post, opening away from the road (annex 23 а), or one on it."""
        s = C7_SIZE_M
        bottom = max(ground_z, road_z) + C7_BOTTOM_M - ground_z
        self.place(np.r_[point, ground_z], boards[0][0])
        self.box((-0.03, -0.03, -0.3), (0.03, 0.03, bottom + s - 0.05), METAL)
        for face, arrow in boards:
            self.place(np.r_[point, ground_z], face)
            self.centre = self.centre + self.face * 0.035
            off = 0.0 if len(boards) == 1 else math.copysign(s / 2, float(dnp.dot(self.right[:2], outside)) or 1.0)
            self.quad([(-0.005, off + s / 2, bottom), (-0.005, off - s / 2, bottom), (-0.005, off - s / 2, bottom + s),
                       (-0.005, off + s / 2, bottom + s)], METAL)                     # (the back)
            self.rectangle(s, bottom, s, 0.0, WHITE, offset=off)
            z = bottom + s / 2
            # the chevron: tip at the side the road turns to (annex 21's drawing)
            pts = np.array([(-0.40, 0.5), (-0.10, 0.5), (0.45, 0.0), (0.15, 0.0), (-0.10, -0.5), (-0.40, -0.5)]) * s
            pts[:, 0] *= arrow
            pts[:, 0] += off
            up = [pts[0], pts[1], pts[2], pts[3]]
            down = [pts[3], pts[2], pts[4], pts[5]]
            for quad in (up, down):
                q = [(0.002, float(y), z + float(v)) for y, v in quad]
                # counter-clockwise from the front whichever way the arrow points
                (ay, az), (by, bz), (cy, cz) = (q[0][1:], q[1][1:], q[2][1:])
                if (by - ay) * (cz - az) - (bz - az) * (cy - ay) < 0:
                    q = q[::-1]
                self.quad(q, RED)

    def result(self):
        if not self.t:
            return None
        return np.asarray(self.p), np.asarray(self.t), np.asarray(self.uv), list(MATERIALS), np.asarray(self.mi)


# --- a tile -------------------------------------------------------------------------------------------------------

def _ground(frame, elev, xy):
    la, lo, _ = frame.enu_to_geodetic(np.array([xy[0]]), np.array([xy[1]]), np.zeros(1))
    return float(np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2]).ravel()[0])


def rows(roads_xy, frame, urban, places):
    """The signs of roads_xy [(fid, spec, xy (k, 2) ENU)]: ([(point, boards)] С7 posts, [town sign dicts]).
    urban(lat, lon) -> built-up flags, or None (no town signs); places(lat, lon) -> a place name or "" (or None)."""
    posts, towns = [], []
    for fid, sp, xy in roads_xy:
        if sp is None or sp.kind != "paved" or getattr(sp, "link", False) or len(xy) < 2:
            continue
        direction = (int(sp.dir) or 1) if sp.oneway else 1
        if sp.cls in C7_CLASSES and not sp.urban:
            posts.extend(c7_posts(xy, sp.width, sp.oneway, direction))
        if urban is not None and places is not None and sp.cls in TOWN_CLASSES:
            p, _ = _resample(xy, TOWN_STEP_M)
            if p is None:
                continue
            la, lo, _ = frame.enu_to_geodetic(p[:, 0], p[:, 1], np.zeros(len(p)))
            built = np.asarray(urban(la, lo), bool)

            def name_at(q):
                qla, qlo, _ = frame.enu_to_geodetic(np.array([q[0]]), np.array([q[1]]), np.zeros(1))
                return places(float(qla[0]), float(qlo[0])) or ""
            towns.extend(town_signs(xy, sp.width, sp.cls, sp.oneway, direction, built, name_at))
    return posts, towns


NEAR_M = 400.0              # m round a tile whose roads are read (a bend's or a town edge's whole run)
_place_lock = __import__("threading").Lock()
_place_names = {}


def roads_near(frame, box, context, left_hand=False):
    """[(fid, spec, xy (k, 2) ENU)] of the ground roads within NEAR_M of box, each map line once (whole in every cell
    it crosses)."""
    import mapcells
    import mapgeom
    import roads
    import signroads
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - NEAR_M, x1 + NEAR_M]), np.array([y0 - NEAR_M, y1 + NEAR_M]), 0.0)
    out, seen = [], set()
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = context.cell(key)
        if feats is None or not len(feats):
            continue
        specs = signroads.cell_specs(feats, context.urban, context.widths, left_hand)
        for i, sp in enumerate(specs):
            fid = signroads.feature_id(feats, i)
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            for ring in roads.ground_parts(feats, i):
                if len(ring) >= 2:
                    e, n = mapgeom.enu_xy(feats, frame, ring)
                    out.append((fid, sp, np.column_stack([e, n])))
    return out


def places(md):
    """place(lat, lon) -> the local (primary) name of the town placenames.pick finds there, "" where none, from the
    place cells already on disk (the status bar's: nothing is fetched here), or None without map data."""
    import placenames
    cache_dir = getattr(md, "cache_dir", None)
    if cache_dir is None:
        return None
    with _place_lock:
        pn = _place_names.get(cache_dir)
        if pn is None:
            pn = _place_names[cache_dir] = placenames.PlaceNames(cache_dir, None, reader=getattr(md, "overture", None))

    def place(lat, lon):
        if not pn.ready(lat, lon):
            return ""
        cells = [pn.cell(k) for k in placenames.cells_for(lat, lon)]
        towns = placenames._concat([c[0] for c in cells])
        towns = {k: v for k, v in towns.items() if not k.startswith("name_")}     # (the primary name: the local one)
        borders = [c[2] for c in cells if c[2] is not None and len(c[2])]
        return placenames.pick(towns, {}, lat, lon, np.concatenate(borders) if borders else None)["place"]
    return place


def merge(a, b):
    """Two signstream meshes (p, t, uv, materials, mi) as one (None: absent), materials by name."""
    if a is None or b is None:
        return a if b is None else b
    pa, ta, ua, ma, ia = a
    pb, tb, ub, mb, ib = b
    mats = list(ma) + [m for m in mb if m not in ma]
    remap = np.array([mats.index(m) for m in mb], np.int64)
    return (np.vstack([pa, pb]), np.vstack([np.asarray(ta), np.asarray(tb) + len(pa)]), np.vstack([ua, ub]), mats,
            np.r_[np.asarray(ia, np.int64), remap[np.asarray(ib, np.int64)]])


def build_tile(frame, box, elev, roads_xy, urban, places, decks=()):
    """The mesh (signstream's) of the С7 posts and Д11 / Д12 signs standing in box (x0, y0, x1, y1), or None."""
    x0, y0, x1, y1 = box
    posts, towns = rows(roads_xy, frame, urban, places)
    mesh = Mesh((float(x0), float(y0), 0.0))
    inside = lambda q: x0 <= q[0] < x1 and y0 <= q[1] < y1 and not exitsigns._on_deck(q, decks, 1.5)  # noqa: E731
    for q, boards, outside in posts:
        if inside(q):
            g = _ground(frame, elev, q)
            mesh.c7(q, boards, outside, g, g)
    for sign in towns:
        if inside(sign["point"]):
            g = _ground(frame, elev, sign["point"])
            mesh.town(sign, g, g)
    return mesh.result()
