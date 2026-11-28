"""Airfield signs (ICAO Annex 14 chapter 5.4 / FAA AC 150/5345-44): the red runway holding position signs, the yellow-on-
black location signs and the black-on-yellow direction signs of a taxiway network, as the boards of the sign tiles
(signstream.build_tile: the same mesh and pictures as the bus and railway stations' name boards, busstationsign).

signs(feats_list, db=None, stands=None) -> [Sign]: the signs of the infrastructure Features (subtype "airport") of the map cells
around a block (and of the stands: parkedaircraft's Craft list, parkedaircraft.lead_stands); Sign = (kind, text, lat, lon, face, w, h), face the compass bearing the board looks toward (at the
pilot who approaches it) and w, h its size in metres. Memoized on the Features' scene (taxiwaymarks.scene, bounded),
so every tile of a block shares them; [] for Features with no airport way. Four kinds:
  * "hold": a mandatory instruction sign, white on red, the runway's designators ("01R-19L") on BOTH sides of a taxiway
    at each runway holding position (taxiwaymarks' sites: 60 m from the runway's centreline, or at a mapped
    holding_position). The designators are the runway line's `ref` tag (the ends in the order the pilot at the position
    sees them, the left one first), else the nearest OurAirports runway's (db: airports.AirportDB, None: the tag only);
    none of either: no sign.
  * "location": yellow on black, the taxiway's own `ref` ("A4"), and
  * "direction": black on yellow, the crossing taxiway's `ref` with the arrow of each way it leaves by ("<- B3 ->"; the
    arrows are the font's: left, half left, back left ... by the way's angle to the pilot's heading), both at a taxiway
    junction (a shared vertex of two mapped ways, or the end of one on the line of another), in a row on the left of
    each approach, before the junction. Only where the ways have a `ref` of the taxiway kind (A, B3, C3T: ICAO's
    letters and digits; "South Taxilane" is no designator): a junction of ways without one has no sign, and a way that
    leaves by the taxiway's own ref (the same taxiway going on) is no turn.
  * "stand": black on yellow and smaller (CAP_STAND_M), the gate's `ref` ("21", "A12") at each stand that has one (a gate or
    remote stand, with or without an aircraft): on a STAND_POST_M post (its top), STAND_AHEAD_M beyond the nose (past the
    stop bar) and STAND_LEFT_M to the nose's left, facing the aircraft; a stand's signs stand on its apron (no ground test),
    two boards never overlap.
A board stands OFFSET_M beside the pavement's edge, on the pilot's side of the sign's place (frangible: well back of the
strip), farther in steps while an apron, another way's or a runway's pavement is there (OFFSET_STEPS_M), and is dropped
where it finds none; two boards never overlap. The hold signs come first.

sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, db=None) -> (mesh, faces) or (None, {}): the signs whose place is in
road tile (tx, ty), in dirsigns.build_tile's form (vertices, triangles, uv, materials, material per triangle; {material:
vfs path of its picture}). The picture of a text is drawn once (the kernel's dirsign_town_face, named by its hash, in
the process: `A4` of a hundred junctions is one picture, one Material), a DDS in the session folder (the mod makes the
Materials: dirsigns.lua). The panel is ~1 m tall with its bottom BOTTOM_M over the ground on one post (two for a panel
over 1.8 m): dirsign_mesh keeps a board's bottom 1.2 m over its anchor at least, so the anchor is put that much lower
and the posts' feet are underground.
"""
import hashlib
import json
import logging
import math
import os
import re
from collections import namedtuple

import numpy as np

import aeroways
import dirsigns
import dmath
import dnp
import fast
import geotiles
import imagery
import parkedaircraft as pa
import polygon
import roads
import taxiwaymarks

KERNEL = 204                    # earthfast VERSION with dirsign_town_face / dirsign_mesh
ON = os.environ.get("BNE_AIRFIELD_SIGNS", "on") != "off"
FACE_VERSION = 1
log = logging.getLogger(__name__)
PX_PER_M = 200.0
CAP_M = 0.38                    # the legend's height: a panel ~1.0 m tall (ICAO: 0.3 - 0.4 m legends, 0.6 - 0.8 m faces)
MIN_W_M, MAX_W_M = 1.0, 10.0
INSET_M, LINE_W_M = 0.03, 0.02
RED, WHITE, YELLOW, BLACK = (200, 16, 46), (255, 255, 255), (255, 205, 0), (0, 0, 0)
# kind -> (background, legend, edge): ICAO's colours (a location sign has the yellow border of a free-standing one)
STYLES = {"hold": (RED, WHITE, WHITE), "location": (BLACK, YELLOW, YELLOW), "direction": (YELLOW, BLACK, BLACK),
          "stand": (YELLOW, BLACK, BLACK)}
KINDS = tuple(STYLES)
BOARD_GAP_M = 0.015            # a stand sign's two boards (one each way) this far either side of its place
CAP_STAND_M = 0.26              # a stand sign's legend (a panel ~0.7 m tall)
STAND_POST_M = 2.5              # m: the top of its post over the ground
STAND_AHEAD_M = 4.0             # m beyond the nose (the stop bar), along the axis
STAND_LEFT_M = 3.0              # m to the nose's left
STAND_REF_MAX = 5               # a longer ref is not signed
BOTTOM_M = 0.35                 # the panel's bottom over the ground (Annex 14: at most 0.7 m ... 0.4 m by size)
MOUNT_M = 1.2                   # dirsign_mesh's least bottom over its anchor
OFFSET_M = 10.0                 # the board's near edge this far beside the pavement's edge
OFFSET_STEPS_M = (10.0, 7.0, 5.0)
CLEAR_WAY_M = 3.0               # m beyond a way's half width, 5.0 beyond a runway's: no board nearer
CLEAR_RUNWAY_M = 5.0
HOLD_BACK_M = 1.5               # a hold sign stands this far on the taxiway side of the marking's first line
JOIN_M = 1.5                    # ways' vertices this near are one junction (and a way's end this near a line is on it)
ARM_LOOK_M = 10.0               # a way's direction at a junction: where it is this far out
STATION_M = 10.0                # the row stands this far before the junction, over the crossing way's half width
STATION_STEPS_M = (0.0, 6.0, 12.0)
ROW_GAP_M = 0.6                 # between the boards of a row
STRAIGHT_DEG = 20.0             # a way within this of straight on is no turn (no up arrow: it is the same way)
BACK_DEG = 160.0                # ... and within this of the way back is none either
MAX_FACES = 40                  # distinct pictures a tile makes at most (a Material each)
MAX_FACES_CACHE = 4096
REF = re.compile(r"^[A-Z]{1,2}[0-9]{0,2}[A-Z]?$")      # a taxiway designator: A, B3, C3T, WR12
Sign = namedtuple("Sign", "kind text lat lon face w h")
_MEMO = "signs"
_FACES = {}


# ------------------------------------------------------------------------------------------------- the pictures
def _draw(kind, text):
    """(material, width m, height m, PNG bytes) of the board of this text: the kernel draws it once (kept)."""
    got = _FACES.get((kind, text))
    if got is None:
        fast.require(KERNEL, "airfield signs")
        dirsigns._set_fonts()
        bg, ink, edge = STYLES[kind]
        flat = [*bg, *ink, *edge, *BLACK, CAP_STAND_M if kind == "stand" else CAP_M, 0.55, MIN_W_M, MAX_W_M, INSET_M, LINE_W_M, 0.07, 0.0, 0.0]
        h = hashlib.sha1(json.dumps([FACE_VERSION, flat, text, PX_PER_M], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        data, w_m, h_m = fast._ef.dirsign_town_face(text, "", False, flat, PX_PER_M)
        if len(_FACES) >= MAX_FACES_CACHE:
            _FACES.clear()
        got = _FACES[kind, text] = (f"bne_afsign_{h}", float(w_m), float(h_m), bytes(data))
    return got


def _face(out_dir, vfs_dir, kind, text):
    """(material, vfs path of its picture, width m, height m): the DDS written once."""
    material, w, h, png = _draw(kind, text)
    path = os.path.join(out_dir, material + ".color.dds")
    if not os.path.exists(path):
        imagery.write_picture_dds(path, png)      # (a DDS, never a PNG the engine cooks: imagery.write_picture_dds)
    return material, f"{vfs_dir}/{material}.color.dds", w, h


# ------------------------------------------------------------------------------------------------- geometry
def _length(line):
    return float(dnp.hypot(*np.diff(line, axis=0).T).sum())


def _at(line, s):
    """The point (2,) and the unit direction (2,) of the polyline (k, 2) s m along it (clamped to its ends)."""
    d = np.diff(line, axis=0)
    seg = dnp.hypot(d[:, 0], d[:, 1])
    cum = np.r_[0.0, np.cumsum(seg)]
    s = min(max(s, 0.0), float(cum[-1]))
    k = min(int(np.searchsorted(cum, s, side="right")) - 1, len(seg) - 1)
    t = d[k] / max(float(seg[k]), 1e-9)
    return line[k] + t * (s - cum[k]), t


def _seg_dist(pts, a, b):
    """Distance (k, m) from each point (k, 2) to each segment a -> b (m, 2)."""
    ab = b - a
    ap = pts[:, None, :] - a[None]
    t = np.clip(dnp.vecdot(ap, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12)[None], 0.0, 1.0)
    d = ap - t[..., None] * ab[None]
    return dnp.hypot(d[..., 0], d[..., 1])


def _bearing(d):
    return math.degrees(dmath.atan2(d[0], d[1])) % 360.0


class _Ground:
    """Where a board may not stand: within a way's half width + CLEAR_WAY_M of its line, a runway's + CLEAR_RUNWAY_M, or
    inside an apron (all in the scene's metre frame)."""

    def __init__(self, sc, aprons):
        lines = [w[0] for w in sc.ways] + [r[0] for r in sc.runways]
        self.a = np.concatenate([x[:-1] for x in lines])
        self.b = np.concatenate([x[1:] for x in lines])
        margin = [w[1] / 2.0 + CLEAR_WAY_M for w in sc.ways] + [r[1] + CLEAR_RUNWAY_M for r in sc.runways]
        self.margin = np.concatenate([np.full(len(x) - 1, m) for x, m in zip(lines, margin)])
        self.aprons = aprons

    def clear(self, pts):
        """Is every point of pts (k, 2) off the pavement?"""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        if (_seg_dist(pts, self.a, self.b) < self.margin[None]).any():
            return False
        for ring in self.aprons:
            if (len(ring) >= 3 and (pts >= ring.min(axis=0)).all(axis=1).any() and (pts <= ring.max(axis=0)).all(axis=1).any()
                    and polygon.contains_points(ring, pts).any()):
                return False
        return True


def _aprons(feats_list, sc):
    """[ring (k, 2)] the aprons' outer rings in the scene's frame."""
    out = []
    seen = set()
    for f in feats_list:
        if f is None or len(f) == 0:
            continue
        sub, cls = f.attrs.get("subtype"), f.attrs.get("class")
        if sub is None or cls is None:
            continue
        hit = (np.asarray(sub).astype(str) == aeroways.SUBTYPE) & (np.asarray(cls).astype(str) == aeroways.APRON)
        for i in np.flatnonzero(hit):
            if int(f.kind[i]) != 3 or str(f.ids[i]) in seen:
                continue
            seen.add(str(f.ids[i]))
            for part in f.parts(i):
                if part and len(part[0]) >= 3:
                    ring = np.asarray(part[0], np.float64)
                    e, n, _ = sc.frame.geodetic_to_enu(ring[:, 1], ring[:, 0], np.zeros(len(ring)))
                    out.append(np.c_[e, n])
    return out


# ------------------------------------------------------------------------------------------------- the runways' names
def designators(ref):
    """The runway designators of a `ref` ("01R/19L", "09/27", "18"): ["01R", "19L"] in its order, [] when it is none (a
    designator is 01 - 36 and L, R or C; two of them are of the reciprocal headings)."""
    out = []
    for part in re.split(r"[/;,\-\s]+", str(ref or "").upper().strip()):
        if not part:
            continue
        m = re.match(r"^(\d{1,2})([LRC]?)$", part)
        if m is None or not 1 <= int(m.group(1)) <= 36:
            return []
        out.append(f"{int(m.group(1)):02d}{m.group(2)}")
    if len(out) == 2 and abs(int(out[0][:2]) - int(out[1][:2])) != 18:
        return []
    return out if len(out) in (1, 2) else []


def _hold_text(sc, ri, site, u, db):
    """The text of the hold sign of runway ri at the site (point, direction away from the runway), None for no name: the
    designators in the order of the pilot's left to right (he faces -u)."""
    rxy = sc.runways[ri][0]
    left = np.array([u[1], -u[0]])
    named = designators(sc.runways[ri][2])
    if len(named) == 2:
        beta = _bearing(rxy[-1] - rxy[0])           # (the end a designator names is where a landing on it starts)
        ends = []
        for d in named:
            diff = abs((int(d[:2]) * 10.0 - beta + 180.0) % 360.0 - 180.0)
            ends.append(rxy[0] if diff <= 90.0 else rxy[-1])
        pairs = list(zip(named, ends))
    elif named:
        return named[0]
    else:
        pairs = _db_ends(sc, ri, site, db)
        if not pairs:
            return None
    pairs.sort(key=lambda p: -float(dnp.matmul(p[1] - site, left)))
    return "-".join(p[0] for p in pairs)


def _db_ends(sc, ri, site, db):
    """[(designator, end point)] of the OurAirports runway nearest the site (within 40 m of the mapped line's distance), []
    when there is none or db is None."""
    if db is None:
        return []
    rxy = sc.runways[ri][0]
    a, b = rxy[:-1], rxy[1:]
    reach = float(_seg_dist(site[None], a, b).min()) + 40.0
    la, lo, _ = sc.frame.enu_to_geodetic(float(site[0]), float(site[1]), 0.0)
    for _, rw in db.near(float(la), float(lo), reach):
        ends = []
        for e in (rw.le, rw.he):
            d = designators(e.ident)
            if len(d) != 1:
                break
            x, y, _ = sc.frame.geodetic_to_enu(e.lat, e.lon, 0.0)
            ends.append((d[0], np.array([float(x), float(y)])))
        else:
            return ends
    return []


# ------------------------------------------------------------------------------------------------- junctions
def _valid(ref):
    return bool(REF.match(ref or ""))


def _junctions(sc):
    """[(point (2,), [(way, head (k, 2))])]: where ways with a taxiway `ref` meet; a head is the way's line from the
    junction out (one arm each side of a vertex inside a way). The end of one way on the line of another is a vertex of
    that line."""
    ids = [i for i, w in enumerate(sc.ways) if _valid(w[2])]
    if len(ids) < 2:
        return []
    lines = {}
    for i in ids:
        xy = sc.ways[i][0]
        lines[i] = xy[np.r_[True, dnp.hypot(*np.diff(xy, axis=0).T) > 1e-3]]
    own = np.concatenate([np.full(len(lines[i]) - 1, i) for i in ids])
    a = np.concatenate([lines[i][:-1] for i in ids])
    b = np.concatenate([lines[i][1:] for i in ids])
    ab = b - a
    ab2 = np.maximum(dnp.vecdot(ab, ab), 1e-12)
    adds = {}                                   # (a way's end on the line of another: a vertex there)
    for i in ids:
        for e in (lines[i][0], lines[i][-1]):
            t = np.clip(dnp.vecdot(e - a, ab) / ab2, 0.0, 1.0)
            q = a + t[:, None] * ab
            d = dnp.hypot(*(q - e).T)
            d[own == i] = np.inf
            k = int(np.argmin(d))
            if d[k] <= JOIN_M and min(float(dnp.hypot(*(q[k] - a[k]))), float(dnp.hypot(*(q[k] - b[k])))) > JOIN_M:
                adds.setdefault(int(own[k]), []).append((k, float(t[k]), q[k]))
    first = {i: int(np.flatnonzero(own == i)[0]) for i in ids if (own == i).any()}
    for i, extra in adds.items():
        xy, out = lines[i], []
        for j in range(len(xy) - 1):
            out.append(xy[j])
            out += [q for k, t, q in sorted(extra, key=lambda x: x[1]) if k - first[i] == j]
        out.append(xy[-1])
        lines[i] = np.array(out)
    verts = np.concatenate([lines[i] for i in ids])
    who = np.concatenate([np.full(len(lines[i]), i) for i in ids])
    at = np.concatenate([np.arange(len(lines[i])) for i in ids])
    parent = list(range(len(verts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    grid = {}
    cell = np.floor(verts / JOIN_M).astype(np.int64)
    for n, (cx, cy) in enumerate(cell.tolist()):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in grid.get((cx + dx, cy + dy), ()):
                    if float(dnp.hypot(*(verts[n] - verts[m]))) <= JOIN_M:
                        parent[find(n)] = find(m)
        grid.setdefault((cx, cy), []).append(n)
    groups = {}
    for n in range(len(verts)):
        groups.setdefault(find(n), []).append(n)
    out = []
    for members in groups.values():
        if len({int(who[n]) for n in members}) < 2:
            continue
        arms = []
        for n in members:
            xy, j = lines[int(who[n])], int(at[n])
            if j + 1 < len(xy):
                arms.append((int(who[n]), xy[j:]))
            if j > 0:
                arms.append((int(who[n]), xy[j::-1]))
        out.append((verts[members].mean(axis=0), arms))
    return out


def _arrow(theta):
    """The arrow for a way leaving theta degrees left (+) or right (-) of the pilot's heading."""
    a = abs(theta)
    glyph = "←" if a < 112.5 and a >= 67.5 else ("↖" if a < 67.5 else "↙")
    if theta < 0:
        glyph = {"←": "→", "↖": "↗", "↙": "↘"}[glyph]
    return glyph


def _row(sc, junction):
    """The rows of one junction [(head, own half width, crossing half width, boards)]: for each approach (a way's arm:
    the pilot comes in along it), the boards [(kind, text)] of its row from the pilot's left to right: the direction signs (the left turns
    first) then the location sign."""
    point, arms = junction
    out = []
    for way, head in arms:
        own = sc.ways[way][2]
        look = _at(head, min(ARM_LOOK_M, _length(head)))[0] - head[0]
        if float(dnp.hypot(*look)) < 2.0:
            continue
        heading = -look / float(dnp.hypot(*look))                    # (the pilot's, in from the arm's far end)
        turns = {}                                                  # crossing ref -> [theta], and its half width
        wide = 0.0
        for other, h2 in arms:
            ref = sc.ways[other][2]
            if other == way or not _valid(ref) or ref == own:
                continue
            d = _at(h2, min(ARM_LOOK_M, _length(h2)))[0] - h2[0]
            if float(dnp.hypot(*d)) < 2.0:
                continue
            d /= float(dnp.hypot(*d))
            theta = math.degrees(dmath.atan2(heading[0] * d[1] - heading[1] * d[0], dnp.matmul(heading, d)))
            if STRAIGHT_DEG < abs(theta) < BACK_DEG:
                turns.setdefault(ref, []).append(theta)
                wide = max(wide, sc.ways[other][1] / 2.0)
        if not turns:
            continue
        boards = []
        for ref, thetas in sorted(turns.items(), key=lambda kv: -float(np.mean(kv[1]))):
            lefts, rights = [t for t in thetas if t > 0], [t for t in thetas if t < 0]
            parts = ([_arrow(float(np.mean(lefts)))] if lefts else []) + [ref] + \
                    ([_arrow(float(np.mean(rights)))] if rights else [])
            boards.append(("direction", " ".join(parts)))
        boards.append(("location", own))
        out.append((head, sc.ways[way][1] / 2.0, wide, boards))
    return out


# ------------------------------------------------------------------------------------------------- the signs
def _stand_places(sc, stands):
    """[(text, place (2,), face direction (2,), to the board's left (2,))] of the stand signs (module docstring) of the stands
    (parkedaircraft Craft) that have a short ref, sorted by position."""
    st = sorted((c for c in stands if c.mode in taxiwaymarks.LEAD_MODES and c.ref.strip() and len(c.ref.strip()) <= STAND_REF_MAX),
                key=lambda c: (round(c.lat, 7), round(c.lon, 7), c.kind))
    if not st:
        return []
    e, n, _ = sc.frame.geodetic_to_enu(np.array([c.lat for c in st]), np.array([c.lon for c in st]), np.zeros(len(st)))
    out = []
    for c, ce, cn in zip(st, np.asarray(e, np.float64), np.asarray(n, np.float64)):
        u = np.array([c.ne, c.nn])
        u = u / max(float(dnp.hypot(*u)), 1e-9)
        left = np.array([-u[1], u[0]])
        at = np.array([ce, cn]) + u * (pa.SPECS[c.kind][0] / 2.0 + STAND_AHEAD_M) + left * STAND_LEFT_M
        out.append((c.ref.strip().upper(), at, -u, left))
    return out


def _build(sc, aprons, db, stands=()):
    ground = _Ground(sc, aprons)
    placed = []                                 # (x, y, w) of the boards so far
    found = []                                  # (kind, text, x, y, face direction (2,), w, h)

    def free(c, w):
        return all(float(dnp.hypot(*(c - p))) >= 0.5 * min(w, pw) + 0.2 for p, pw in placed)

    def put(kind, text, c, face, lat_dir, w, h, apron=False):
        pts = c[None] + lat_dir[None] * np.array([-w / 2.0, 0.0, w / 2.0])[:, None]
        if not (free(c, w) and (apron or ground.clear(pts))):          # (a stand's sign stands on its apron)
            return False
        placed.append((c, w))
        found.append((kind, text, float(c[0]), float(c[1]), face, w, h))
        return True

    for ri, wi, site, u in sc.sites:            # (the mandatory instruction signs first: the runway holding positions)
        text = _hold_text(sc, ri, site, u, db)
        if not text:
            continue
        _, w, h, _ = _draw("hold", text)
        left = np.array([u[1], -u[0]])
        half = sc.ways[wi][1] / 2.0
        for side in (1.0, -1.0):
            for off in OFFSET_STEPS_M:
                c = site + u * HOLD_BACK_M + left * side * (half + off + w / 2.0)
                if put("hold", text, c, u, left, w, h):
                    break
    for junction in _junctions(sc):
        for head, half, wide, boards in _row(sc, junction):
            for step in STATION_STEPS_M:        # (a row stands over the crossing way's half width before the junction)
                station = wide + STATION_M + step
                if station > _length(head):
                    break
                p, t = _at(head, station)
                left = np.array([t[1], -t[0]])      # (the pilot comes in against t: his left)
                reach, n = half + OFFSET_M, 0
                for kind, text in reversed(boards):     # (nearest the pavement first: the location sign)
                    _, w, h, _ = _draw(kind, text)
                    n += put(kind, text, p + left * (reach + w / 2.0), t, left, w, h)
                    reach += w + ROW_GAP_M
                if n:
                    break
    for text, at, face, left in _stand_places(sc, stands):
        _, w, h, _ = _draw("stand", text)
        put("stand", text, at, face, left, w, h, apron=True)
    return found


def signs(feats_list, db=None, stands=None):
    """The signs of the Features in feats_list and of the stands (module docstring): [Sign], memoized on their scene, [] for
    Features with no airport way in them."""
    sc = taxiwaymarks.scene(feats_list)
    if sc is None:
        return []
    stands = [c for c in (stands or ()) if c.ref]
    skey = taxiwaymarks.stand_key(stands)
    got = sc.memo.get(_MEMO)
    if got is not None and got[0] is db and got[1] == skey:
        return got[2]
    out = []
    for kind, text, x, y, face, w, h in _build(sc, _aprons(feats_list, sc), db, stands):
        la, lo, _ = sc.frame.enu_to_geodetic(x, y, 0.0)
        out.append(Sign(kind, text, float(la), float(lo), _bearing(face), w, h))
    sc.memo[_MEMO] = (db, skey, out)
    return out


def _board_rows(sg, x, y, zg, w, h, material, mount):
    """The kernel's rows [kind, text, x, y, z ground, z road, direction, w, h, material, mount] (dirsign_mesh; the first two are
    ours) of the Sign sg: one board facing sg.face, and for a stand sign a second back to back with it, turned round (in game its
    text showed on the back only: each board BOARD_GAP_M behind its own face), so the text reads from both sides."""
    fx, fy = dmath.sin(math.radians(sg.face)), dmath.cos(math.radians(sg.face))
    out = []
    for sgn in ((1.0, -1.0) if sg.kind == "stand" else (0.0,)):
        out.append([sg.kind, sg.text, x + fx * sgn * BOARD_GAP_M, y + fy * sgn * BOARD_GAP_M, zg, zg,
                    -fx * (sgn or 1.0), -fy * (sgn or 1.0), w, h, material, mount])
    return out


def sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, db=None):
    """The signs whose place is in road tile (tx, ty) as (mesh, faces) (module docstring), or (None, {})."""
    if not ON:
        return None, {}
    box = roads.tile_bbox(tx, ty)
    keys = roads.cells_of_tile(frame, tx, ty, taxiwaymarks.REACH_M)
    infra = lambda k: md.cell(k, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
    feats = [infra(k) for k in keys]
    stands = []
    if any(pa.has_stands(f) for f in feats):
        try:
            stands = pa.lead_stands(keys, infra, pa.building_layers_fn(md))
        except Exception as ex:                 # (the taxiway signs are worth more than the stand signs)
            log.warning("stand signs: %s: %s", type(ex).__name__, ex)
    found = signs(feats, db, stands)
    if not found:
        return None, {}
    la = np.array([x.lat for x in found])
    lo = np.array([x.lon for x in found])
    e, n, _ = frame.geodetic_to_enu(la, lo, 0.0)
    own = np.flatnonzero(np.asarray(geotiles.box_owns(box, np.asarray(e), np.asarray(n)), bool))
    if not len(own):
        return None, {}
    own = own[np.argsort([KINDS.index(found[i].kind) for i in own], kind="stable")]     # (the hold signs first)
    hz = np.asarray(elev(la[own], lo[own]), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la[own], lo[own], hz)[2], np.float64).ravel()
    materials, faces, rows = [dirsigns.METAL], {}, []
    for k, i in enumerate(own):
        sg = found[i]
        if sg.text not in faces and len(faces) >= MAX_FACES and (sg.kind, sg.text) not in {(r[0], r[1]) for r in rows}:
            continue
        name, tex, w, h = _face(out_dir, vfs_dir, sg.kind, sg.text)
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        if sg.kind == "stand":                  # (on a post STAND_POST_M tall: the foot a little underground)
            mount = max(STAND_POST_M - h, MOUNT_M) + 0.1
            zg = z[k] - 0.1
        else:
            mount = MOUNT_M
            zg = z[k] - (MOUNT_M - BOTTOM_M)
        rows += _board_rows(sg, float(e[i]), float(n[i]), zg, w, h, materials.index(name), mount)
    if not rows:
        return None, {}
    x0, y0 = box[0], box[1]
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray([r[2:] for r in rows], np.float64), [float(x0), float(y0), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces
