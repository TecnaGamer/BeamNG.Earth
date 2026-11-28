"""Airport roles: one worldwide rule set that gives every building (and building part) at an airport exactly one role.

An airport's buildings come from three Overture layers that do not agree (a terminal as one footprint, as a stack of parts,
a pier as a part with no height, a jet bridge as a 3 m wide footprint, a walkway raised 8 m over a stand), and the render
paths (airportbuildings, parkedaircraft, airsideprops) each decide what a footprint is on their own. This is the single
decision they can share: classify(buildings, parts, infra) -> {id: role}, a Roles (a dict, with the rest as attributes).

Roles, tested in this order (the first that holds; the numbers are the constants below):
  tower        airportbuildings.Context.tower_parts (named, or slender, compact and tall by a pad; a part needs a name: a tall
               part of a terminal is no tower); the parts of a tower
  hangar       class hangar, or a hangar / maintenance / cargo name; the parts of a hangar
  canopy       class roof / canopy / carport / shelter or such a name (no walls)
  pier         named a pier (Schiphol's "E-pier") or long and narrow (PIER_MIN_LEN_M, PIER_RATIO, PIER_MAX_WIDTH_M: more than
               PIER_WIDE_M wide needs PIER_WIDE_GATES gates), with a gate along it (GATE_ALONG_M) and joined (JOIN_M) to a
               terminal, or through another pier to one
  terminal     named a terminal / concourse (TERMINAL_NAME_MIN_M2 and up), or TERMINAL_MIN_M2 with TERMINAL_GATES gates along
               it, or TERMINAL_BIG_M2 with a gate near (TERMINAL_BIG_GATE_M); on the ground
  jet_bridge   elongated, narrow (BRIDGE_NARROW_M) or raised (RAISED_M) up to BRIDGE_RAISED_WIDTH_M wide, one end (the root) at
               or in a terminal or pier (ROOT_M), an end within GATE_BRIDGE_M of a gate point (mappers put the gate at the door
               of the aircraft or of the terminal; with none within GATELESS_M, the outer end on an apron); a narrow piece
               end to end with its outer end is its tail (Brisbane's bridges come in two)
  walkway      raised, narrow (WALKWAY_WIDTH_M), elongated, an end at each of two buildings (WALK_JOIN_M)
  canopy       (again) a raised thin slab (a roof with no walls: CANOPY_THICK_M)
  raised_block raised (RAISED_M) and anything else: it should stand on the ground (a part over a drive); a part standing on
               a lower part of its own building (a storey: STACK_TOL_M) is none, it takes its building's role
  terminal     a part of a terminal's building that is nothing of the above (a pier when it is one, with a gate along it)
               (a canopy along a terminal's front, Brisbane's roof strips, stays a canopy: it is drawn at its own mapped
               heights, or a canopy's default, buildings.CANOPY_UNDERSIDE_M; the terminal's mass is drawn behind it)
  other        airside (within airportbuildings' reach of a pad or a gate) and nothing of the above: a shed, a fire station
  landside     the rest within ZONE_GATE_M of a gate or ZONE_APRON_M of an apron: a car park, a hotel, an office
Roles.inside: the ids of the footprints and parts with more than INSIDE_SHARE of their area inside a terminal's or pier's
(non-part) outline that are none of terminal, pier, jet bridge, walkway, tower or hangar: they take the terminal look (the blocks
between Brisbane's terminal roofs came out brick); their role is as it was.
A building with parts is classified as a footprint of its own too (its name and size) and its parts one by one, each with the
parent's name and class (the parts have none). Buildings further from every gate and apron are no part of the airport and have
no role.

Gates: each gate point (class airport_gate) gets its jet bridge, if any: the nearest bridge with an end within GATE_BRIDGE_M,
its id, its outer end's point (the end that is not at a terminal) and the direction out of the terminal (a unit vector along
the bridge). Roles.gates[gate id] = {"xy", "bridge", "end", "dir", "dist", "n_bridges", "terminal"}.

Render (buildings.tile_buildings): terminal and pier parts stand on the ground (min height 0) unless Roles.stacked(id) (a
storey on a lower part of their building), raised blocks are extended down to the ground, jet bridges and walkways stay raised
(on pillars), a canopy stays as mapped; Roles.units[id].airside says whether the building has the airside look.

Lots: a mapped parking area (Overture infrastructure class parking, or a land use of class parking) that lies mostly inside
an apron is reported (Roles.airside_parking: [{"id", "ring"}]): the stands' paving, not a car park.

airside_rings(infra, frame) -> the aprons' and the airside lots' outlines, for the parking-lot code (no cars on the stands).

Pure, deterministic (dnp / dmath). Positions in metres relative to (x0, y0) of `frame` (default: an ENU frame at the airport's
first gate), as airportbuildings."""
import re

import numpy as np

import aeroways
import dmath
import airportbuildings as ab
import dnp
import geodesy
import polygon

TOWER, HANGAR, TERMINAL, PIER, JET_BRIDGE, WALKWAY = "tower", "hangar", "terminal", "pier", "jet_bridge", "walkway"
RAISED_BLOCK, CANOPY, LANDSIDE, OTHER = "raised_block", "canopy", "landside", "other"
ROLES = (TERMINAL, PIER, JET_BRIDGE, WALKWAY, RAISED_BLOCK, TOWER, HANGAR, CANOPY, LANDSIDE, OTHER)

RAISED_M = 2.5                  # m: a part this far off the ground (min_height) is raised
ZONE_GATE_M = 300.0             # m: a building within this of a gate point is at the airport ...
ZONE_APRON_M = 150.0            # ... or within this of an apron
GATE_ALONG_M = 40.0             # m: a gate within this of a footprint's outline is along it
TERMINAL_NAME_MIN_M2 = 600.0    # m2: a named terminal at least this big and wide (a kiosk or a bridge with "Terminal" in its name is not)
TERMINAL_MIN_WIDTH_M = 12.0
TERMINAL_MIN_M2 = 1500.0        # m2 with TERMINAL_GATES gates along it
TERMINAL_GATES = 2
TERMINAL_BIG_M2 = 10000.0       # m2: a footprint this big with a gate within TERMINAL_BIG_GATE_M of it
TERMINAL_BIG_GATE_M = 100.0
PIER_MIN_LEN_M = 40.0           # m: a pier is at least this long, at most this wide, this many times as long as wide,
PIER_MAX_WIDTH_M = 120.0        # (Schiphol's E pier is 54 m wide, its H pier ~95; one wider than PIER_WIDE_M needs PIER_WIDE_GATES gates)
PIER_RATIO = 2.5
PIER_MIN_M2 = 300.0
PIER_WIDE_M = 45.0
PIER_WIDE_GATES = 2
JOIN_M = 12.0                   # m: footprints this near are joined (a pier at a terminal, a pier on a pier)
BRIDGE_NARROW_M = 7.0           # m: a jet bridge this narrow (a bridge's tunnel is 3-6 m) or raised and at most ...
BRIDGE_RAISED_WIDTH_M = 12.0    # ... this wide,
BRIDGE_MIN_LEN_M, BRIDGE_MAX_LEN_M = 5.0, 70.0      # between these long, this many times as long as wide:
BRIDGE_RATIO = 2.0
ROOT_M = 8.0                    # m: its root end this near a terminal or a pier (in one counts), the other end this much further
ROOT_FURTHER_M = 3.0
TAIL_JOIN_M = 4.0               # m: a narrow piece this near a bridge's outer end, at least TAIL_MIN_LEN_M long, is its tail
TAIL_MIN_LEN_M = 3.0
GATELESS_M = 100.0              # m: no gate point this near a bridge: its outer end on an apron (APRON_END_M) will do
APRON_END_M = 10.0
GATE_BRIDGE_M = 25.0            # m: an end of it this near a gate point (the outer end; the root's where the gate is mapped at the terminal)
WALKWAY_WIDTH_M = 12.0          # m: a walkway is raised and at most this wide, this long at least, this many times as long as wide
WALKWAY_MIN_LEN_M = 8.0
WALK_JOIN_M = 8.0               # m: an end this near a building joins it (two different buildings: a walkway)
CANOPY_THICK_M = 4.0            # m: a raised part this thick or less (height - min_height) is a canopy, not a block
CANOPY_MIN_M2 = 60.0
INSIDE_SHARE = 0.5              # a footprint or part with more than this share of its area inside a terminal's or pier's outline
INSIDE_SAMPLES = 60             # (judged from about this many grid samples inside it) has the terminal look
STACK_TOL_M = 1.5               # m: a raised part whose underside is this near the top of a part under it is a storey of it
PARKING_APRON_SHARE = 0.5       # a lot with this share of its outline inside an apron is airside parking
SAMPLE_M = 5.0                  # m: outlines sampled this finely for the joins

HANGAR_NAME = re.compile(r"hangar|hanger|maintenance|\bmro\b|cargo|freight")
CANOPY_NAME = re.compile(r"canopy|shelter|awning|carport|car port")
CANOPY_CLASS = {"roof", "canopy", "carport", "shelter"}
PIER_NAME = re.compile(r"\bpier\b|-pier\b|\bfinger\b")
PARKING_CLASS = {"parking", "parking_space"}


class Unit:
    """A footprint to classify: a building or a part (or one polygon of a multipolygon), in metres."""
    __slots__ = ("id", "ring", "area", "length", "width", "c", "u", "mh", "h", "name", "cls", "key", "parent", "part",
                 "has_parts", "role", "box", "gates", "airside", "at")

    def label(self):
        return f"{self.id[:8]} {self.name or self.cls or ''} {self.area:.0f} m2 {self.length:.0f}x{self.width:.0f}"


class Roles(dict):
    """{id: role} of the airport's buildings and parts, with: .gates {gate id: {...}} (the module docstring), .airside_parking
    [{"id", "ring"}], .units {id: Unit} (every classified footprint, in metres), .tails {tail id: {ids of the bridges it carries on}}, .gate_xy {gate id: (x, y)}, .inside {id} (the terminal look, see above), .frame, .origin."""

    def __init__(self):
        super().__init__()
        self.gates, self.airside_parking, self.units, self.gate_xy, self.bridges = {}, [], {}, {}, {}
        self.by_key, self.inside = {}, set()
        self.tails = {}
        self.frame, self.origin = None, (0.0, 0.0)

    def stacked(self, uid):
        """Does the part uid stand on a lower part of its own building (a storey: it keeps its mapped underside)?"""
        u = self.units.get(uid)
        return u is not None and _stacked(u, self.by_key)

    def counts(self):
        out = {r: 0 for r in ROLES}
        for r in self.values():
            out[r] += 1
        return out


# ----------------------------------------------------------------------------------------------------- geometry
def _num(col, i):
    """A numeric column's value at i (nan where missing)."""
    if col is None:
        return float("nan")
    try:
        v = float(col[i])
    except (TypeError, ValueError, IndexError):
        return float("nan")
    return v


def _text(col, i):
    if col is None:
        return ""
    try:
        v = col[i]
    except IndexError:
        return ""
    return "" if v is None or (isinstance(v, float) and v != v) else str(v)


def _flat(x):
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [f for y in x for f in _flat(y)]
    return [x] if len(x) else []


def _rect(ring):
    """(length, width, centre (2,), unit long axis (2,)) of the ring's smallest bounding rectangle (along one of its edges)."""
    e = np.roll(ring, -1, axis=0) - ring
    n = dnp.hypot(e[:, 0], e[:, 1])
    ok = np.flatnonzero(n > 0.3)
    if not len(ok):
        return 0.0, 0.0, ring.mean(0), np.array([1.0, 0.0])
    if len(ok) > 160:                           # (the longest edges decide it)
        ok = ok[np.argsort(-n[ok], kind="stable")[:160]]
    u = e[ok] / n[ok][:, None]
    v = np.stack([-u[:, 1], u[:, 0]], 1)
    a, b = dnp.matmul(ring, u.T), dnp.matmul(ring, v.T)
    la, wb = a.max(0) - a.min(0), b.max(0) - b.min(0)
    j = int(np.argmin(la * wb))
    c = u[j] * (a[:, j].max() + a[:, j].min()) / 2.0 + v[j] * (b[:, j].max() + b[:, j].min()) / 2.0
    if la[j] >= wb[j]:
        return float(la[j]), float(wb[j]), c, u[j]
    return float(wb[j]), float(la[j]), c, v[j]


def _outline_samples(ring, step=SAMPLE_M):
    return ab._outline_samples(ring, step)


def _near(a, b, within):
    """Do the footprints a, b (Units) come within `within` m (one inside the other, or their outlines this near)?"""
    if (a.box[1] < b.box[0] - within).any() or (a.box[0] > b.box[1] + within).any():
        return False
    sa = _outline_samples(a.ring)
    if float(ab._outline_dist(sa, b.ring).min()) <= within:
        return True
    sb = _outline_samples(b.ring)
    if float(ab._outline_dist(sb, a.ring).min()) <= within:
        return True
    return bool(polygon.contains_points(b.ring, sa[:1]).any() or polygon.contains_points(a.ring, sb[:1]).any())


def _pt_dist(pt, ring):
    """The distance (m) from the point pt (2,) to the polygon ring (0 inside it)."""
    p = np.asarray(pt, np.float64)[None]
    if bool(polygon.contains_points(ring, p)[0]):
        return 0.0
    return float(ab._outline_dist(p, ring)[0])


def _to_xy(frame, ring, x0, y0):
    e, n, _ = frame.geodetic_to_enu(np.asarray(ring[:, 1], np.float64), np.asarray(ring[:, 0], np.float64), 0.0)
    xy = np.stack([np.asarray(e, np.float64) - x0, np.asarray(n, np.float64) - y0], 1)
    if len(xy) > 1 and np.allclose(xy[0], xy[-1]):
        xy = xy[:-1]
    return xy


def _default_frame(infra, buildings):
    """An ENU frame at the first airport gate (else the first building's first vertex): (frame, lon, lat) or None."""
    for f in infra:
        rows = ab._scan(f)[2]
        if len(rows):
            ll = np.asarray(f.parts(int(rows[0]))[0][0], np.float64)[0]
            return geodesy.EnuFrame(float(ll[1]), float(ll[0]), 0.0)
    for f in buildings:
        for i in range(len(f)):
            parts = f.parts(i)
            if parts and parts[0] and len(parts[0][0]):
                ll = np.asarray(parts[0][0], np.float64)[0]
                return geodesy.EnuFrame(float(ll[1]), float(ll[0]), 0.0)
    return None


# ----------------------------------------------------------------------------------------------------- units
def _units(buildings, parts, frame, x0, y0, reach=None):
    """[Unit] of the buildings (first) and the parts, by id: a footprint per building polygon and per part, each once; reach
    (low, high corners): only those whose box meets it (a city's cell is mostly nothing to do with its airport)."""
    out, seen, owners = [], set(), {}
    for f in buildings:
        name_col, cls_col, h_col, mh_col = (f.attrs.get(k) for k in ("name", "class", "height", "min_height"))
        hp_col, ug_col = f.attrs.get("has_parts"), f.attrs.get("is_underground")
        for i in range(len(f)):
            if int(f.kind[i]) != 3 or (ug_col is not None and _num(ug_col, i) == 1.0):
                continue
            pid = str(f.ids[i])
            for pi, part in enumerate(f.parts(i)):
                if not part or len(part[0]) < 3:
                    continue
                uid = pid if pi == 0 else f"{pid}#{pi}"
                if uid in seen:
                    continue
                seen.add(uid)
                xy = _to_xy(frame, np.asarray(part[0], np.float64), x0, y0)
                if reach is not None and ((xy.max(0) < reach[0]).any() or (xy.min(0) > reach[1]).any()):
                    continue
                u = _unit(uid, xy, _text(name_col, i).lower(),
                          _text(cls_col, i).lower(), _num(h_col, i), _num(mh_col, i), pid, None)
                if u is not None:
                    u.has_parts = bool(_num(hp_col, i) == 1.0) if hp_col is not None else False
                    out.append(u)
                    owners.setdefault(pid, (u.name, u.cls))
    for f in parts:
        bid_col, h_col, mh_col = f.attrs.get("building_id"), f.attrs.get("height"), f.attrs.get("min_height")
        for i in range(len(f)):
            if int(f.kind[i]) != 3:
                continue
            pid, bid = str(f.ids[i]), _text(bid_col, i)
            name, cls = owners.get(bid, ("", ""))
            for pi, part in enumerate(f.parts(i)):
                if not part or len(part[0]) < 3:
                    continue
                uid = pid if pi == 0 else f"{pid}#{pi}"
                if uid in seen:
                    continue
                seen.add(uid)
                xy = _to_xy(frame, np.asarray(part[0], np.float64), x0, y0)
                if reach is not None and ((xy.max(0) < reach[0]).any() or (xy.min(0) > reach[1]).any()):
                    continue
                u = _unit(uid, xy, name, cls, _num(h_col, i), _num(mh_col, i), bid or pid, bid or None)
                if u is not None:
                    u.part = True
                    out.append(u)
    out.sort(key=lambda u: (u.part, u.id))
    return out


def _unit(uid, ring, name, cls, h, mh, key, parent):
    if len(ring) < 3:
        return None
    area = ab._area(ring)
    if area < 1.0:
        return None
    length, width, c, u = _rect(ring)
    un = Unit()
    un.id, un.ring, un.area, un.length, un.width, un.c, un.u = uid, ring, area, length, width, c, u
    un.mh = mh if mh == mh else 0.0
    un.h = h if h == h else float("nan")
    un.name, un.cls, un.key, un.parent, un.part, un.has_parts, un.role = name, cls, key, parent, False, False, None
    un.box = (ring.min(0), ring.max(0))
    un.gates, un.airside = None, None
    un.at = {"name": name, "class": cls, "height": h, "min_height": mh}
    return un


def _gate_points(ctx):
    """({gate id: (x, y)}) of the airport_gate points, in ctx.gates()'s order."""
    ids = [str(f.ids[int(i)]) for f in ctx.layers for i in ab._scan(f)[2]]
    g = ctx.gates()
    out = {}
    for gid, p in zip(ids, g):
        out.setdefault(gid, (float(p[0]), float(p[1])))
    return out


def _apron_rings(ctx, infra):
    rings = []
    for la, lo in aeroways.aprons(infra):
        if len(la) >= 3:
            e, n, _ = ctx.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
            rings.append(np.stack([np.asarray(e) - ctx.x0, np.asarray(n) - ctx.y0], 1))
    return rings


def _gates_along(u, gxy):
    """How many gates lie along u's outline (within GATE_ALONG_M), and the distance to the nearest gate (inf if none)."""
    if not len(gxy):
        return 0, float("inf")
    lo, hi = u.box[0] - GATE_ALONG_M * 3.0, u.box[1] + GATE_ALONG_M * 3.0
    g = gxy[((gxy >= lo) & (gxy <= hi)).all(axis=1)]
    if not len(g):
        return 0, float("inf")
    d = ab._outline_dist(g, u.ring)
    d = np.where(polygon.contains_points(u.ring, g), 0.0, d)
    return int((d <= GATE_ALONG_M).sum()), float(d.min())


# ----------------------------------------------------------------------------------------------------- classify
def classify(buildings, parts, infra, frame=None, x0=0.0, y0=0.0, landuse=None):
    """Roles (see the module docstring) of the buildings, building parts and infrastructure Features (each one Features or a
    list of them: the airport's cells'). frame / x0 / y0: the metric plane (default an ENU frame at the first gate, 0, 0).
    landuse: land use Features, for the parking lots' second source."""
    buildings, parts, infra = _flat(buildings), _flat(parts), _flat(infra)
    res = Roles()
    if frame is None:
        frame = _default_frame(infra, buildings)
        if frame is None:
            return res
    res.frame, res.origin = frame, (x0, y0)
    ctx = ab.context(frame, infra, x0, y0)
    if ctx is None:
        return res
    gate_xy = _gate_points(ctx)
    res.gate_xy = gate_xy
    gids = sorted(gate_xy)
    gxy = np.array([gate_xy[g] for g in gids]).reshape(-1, 2)
    aprons = _apron_rings(ctx, infra)

    abox = [(r.min(0) - ZONE_APRON_M, r.max(0) + ZONE_APRON_M) for r in aprons]
    lows = [lo for lo, _ in abox] + ([gxy.min(0) - ZONE_GATE_M] if len(gxy) else [])
    highs = [hi for _, hi in abox] + ([gxy.max(0) + ZONE_GATE_M] if len(gxy) else [])
    units = _units(buildings, parts, frame, x0, y0, (np.min(lows, 0), np.max(highs, 0)) if lows else None)
    zone = []
    for u in units:
        c = u.c
        ok = bool(len(gxy)) and float(dnp.hypot(gxy[:, 0] - c[0], gxy[:, 1] - c[1]).min()) <= ZONE_GATE_M
        if not ok:
            for r, (lo, hi) in zip(aprons, abox):
                if (c >= lo).all() and (c <= hi).all() and (bool(polygon.contains_points(r, c[None])[0])
                                                           or float(ab._outline_dist(c[None], r)[0]) <= ZONE_APRON_M):
                    ok = True
                    break
        if not ok and u.mh <= 0.5 and (not u.part or u.name):     # (a control tower stands off the aprons: by a pad, TOWER_PAD_M)
            ok = ctx.tower_parts(u.ring, u.at) is not None
        if ok:
            zone.append(u)
    by_id = {u.id: u for u in zone}
    for u in zone:
        u.gates = _gates_along(u, gxy)
        u.airside = ctx.airside(u.ring, u.at)      # (not a car park's or a hangar's: their name and class)

    _stage_named(ctx, zone, by_id)
    _stage_terminals(zone, by_id)
    _stage_bridges(zone, gate_xy, gids, gxy, aprons, res)
    _stage_raised(zone)
    for u in zone:
        res.by_key.setdefault(u.key, []).append(u)
    for u in zone:
        if u.role is None:
            par = by_id.get(u.parent) if u.parent else None
            if par is not None and par.role in (TERMINAL, PIER) and u.part:
                u.role = PIER if par.role == PIER or (_elongated(u) and u.area >= PIER_MIN_M2 and u.gates[0] >= 1)                     else TERMINAL
            else:
                u.role = OTHER if u.airside else LANDSIDE
    _stage_inside(zone, res)
    for u in zone:
        res[u.id] = u.role
        res.units[u.id] = u
    _link_gates(zone, gate_xy, gids, res)
    _parking(ctx, infra, landuse, aprons, res)
    return res


def _stage_named(ctx, zone, by_id):
    """Towers, hangars, canopies by name / class (and a part takes its building's tower or hangar)."""
    for u in zone:
        par = by_id.get(u.parent) if u.parent else None
        if par is not None and par.role in (TOWER, HANGAR):
            u.role = par.role
            continue
        if (not u.part or u.name) and ctx.tower_parts(u.ring, u.at) is not None:     # (a tall part of a big building is no tower)
            u.role = TOWER
            continue
        if u.cls == "hangar" or HANGAR_NAME.search(u.name):
            u.role = HANGAR
        elif u.cls in CANOPY_CLASS or CANOPY_NAME.search(u.name):
            u.role = CANOPY


def _elongated(u):
    return u.length >= PIER_MIN_LEN_M and u.width <= PIER_MAX_WIDTH_M and u.length >= PIER_RATIO * u.width \
        and (u.width <= PIER_WIDE_M or u.gates[0] >= PIER_WIDE_GATES)


def _stage_terminals(zone, by_id):
    cand, pierable = [], []
    for u in zone:
        if u.role is not None or u.mh >= RAISED_M:
            continue
        n, d = u.gates
        named = any(w in u.name for w in ab.TERMINAL_NAME) and not any(w in u.name for w in ab.NOT_TERMINAL_NAME) \
            and u.cls not in ab.NOT_TERMINAL_CLASS and u.area >= TERMINAL_NAME_MIN_M2 and u.width >= TERMINAL_MIN_WIDTH_M
        if u.cls in ab.NOT_TERMINAL_CLASS or any(w in u.name for w in ab.NOT_TERMINAL_NAME):
            continue
        is_term = named or (u.area >= TERMINAL_MIN_M2 and n >= TERMINAL_GATES) \
            or (u.area >= TERMINAL_BIG_M2 and d <= TERMINAL_BIG_GATE_M)
        long_ = _elongated(u)
        if PIER_NAME.search(u.name) and u.area >= PIER_MIN_M2 and u.parent is None and n >= 1:
            u.role = PIER                           # (named a pier, gates along it: Schiphol's B to H piers)
            continue
        if is_term:
            cand.append(u)
        if long_ and n >= 1 and u.area >= PIER_MIN_M2:
            pierable.append(u)
    seeds = [u for u in cand if u not in pierable]
    if not seeds and cand:                      # (a linear terminal alone: the biggest is the terminal, the rest piers)
        seeds = [max(cand, key=lambda u: (u.area, u.id))]
    seed_ids = {u.id for u in seeds}
    for u in seeds:
        u.role = TERMINAL
    frontier = list(seeds)
    todo = [u for u in pierable if u.id not in seed_ids]
    while frontier and todo:
        nxt = []
        for p in list(todo):
            if any(s.key != p.key and _near(p, s, JOIN_M) for s in frontier):       # (not its own building's parts)
                p.role = PIER
                nxt.append(p)
                todo.remove(p)
        frontier = nxt
    for u in cand:                              # (a terminal candidate joined to nothing: a terminal)
        if u.role is None:
            u.role = TERMINAL


def _bridge_shape(u):
    """Is u elongated like a jet bridge (narrow, or raised and not wide), BRIDGE_MIN_LEN_M to BRIDGE_MAX_LEN_M long?"""
    if not BRIDGE_MIN_LEN_M <= u.length <= BRIDGE_MAX_LEN_M or u.length < BRIDGE_RATIO * u.width:
        return False
    return u.width <= BRIDGE_NARROW_M or (u.mh >= RAISED_M and u.width <= BRIDGE_RAISED_WIDTH_M)


def _ends(u):
    h = u.u * (u.length / 2.0)
    return u.c - h, u.c + h


def _stage_bridges(zone, gate_xy, gids, gxy, aprons, res):
    """Jet bridges: res.bridges {id: (gate distance, root end, outer end, unit direction root -> outer)}. The root is the end
    at a terminal or pier (ROOT_M), the outer end the other (further from it); the gate is within GATE_BRIDGE_M of either end
    (a gate point is mapped at the aircraft's door or at the terminal's, by the mapper)."""
    roots = [u for u in zone if u.role in (TERMINAL, PIER)]
    cands = [u for u in zone if u.role is None and _bridge_shape(u)]
    bridges = {}
    for u in cands:
        ends = _ends(u)
        td = [_root_dist(e, roots) for e in ends]
        r = 0 if td[0] <= td[1] else 1
        if td[r] > ROOT_M or td[1 - r] < td[r] + ROOT_FURTHER_M:
            continue
        root, outer = ends[r], ends[1 - r]
        d = min(float(dnp.hypot(gxy[:, 0] - e[0], gxy[:, 1] - e[1]).min()) for e in ends) if len(gxy) else float("inf")
        if d <= GATE_BRIDGE_M or (_no_gates_near(u, gxy) and _on_apron(outer, aprons)):
            v = outer - root
            u.role = JET_BRIDGE
            bridges[u.id] = (d, root, outer, v / max(float(dnp.hypot(v[0], v[1])), 1e-9))
    # (a bridge mapped in two pieces: a narrow piece end to end with a bridge's outer end carries it on, the cab)
    tails = [u for u in zone if u.role is None and u.width <= BRIDGE_NARROW_M and TAIL_MIN_LEN_M <= u.length <= BRIDGE_MAX_LEN_M]
    grown = True
    while grown and bridges:
        grown = False
        for u in tails:
            if u.role is not None:
                continue
            ends = _ends(u)
            for bid in sorted(bridges):             # (every bridge it joins carries it on: a bridge mapped twice, one over the other)
                d, root, outer, _dir = bridges[bid]
                k = 0 if float(dnp.hypot(*(ends[0] - outer))) <= float(dnp.hypot(*(ends[1] - outer))) else 1
                if float(dnp.hypot(*(ends[k] - outer))) <= TAIL_JOIN_M:
                    far = ends[1 - k]
                    v = far - root
                    if float(dnp.hypot(*v)) > float(dnp.hypot(*(outer - root))):      # (it runs on away from the root)
                        u.role = JET_BRIDGE
                        res.tails.setdefault(u.id, set()).add(bid)
                        bridges[bid] = (d, root, far, v / max(float(dnp.hypot(v[0], v[1])), 1e-9))
                        grown = True
    res.bridges = bridges


def _no_gates_near(u, gxy):
    """No gate point within GATELESS_M of u (where an airport maps none, a bridge is told by the apron its outer end is on)."""
    return not len(gxy) or float(dnp.hypot(gxy[:, 0] - u.c[0], gxy[:, 1] - u.c[1]).min()) > GATELESS_M


def _on_apron(p, aprons):
    for r in aprons:
        if (p < r.min(0) - APRON_END_M).any() or (p > r.max(0) + APRON_END_M).any():
            continue
        if _pt_dist(p, r) <= APRON_END_M:
            return True
    return False


def _root_dist(p, roots):
    """The distance from the point p to the nearest terminal or pier footprint."""
    best = float("inf")
    for t in roots:
        if (p < t.box[0] - ROOT_M).any() or (p > t.box[1] + ROOT_M).any():
            continue
        best = min(best, _pt_dist(p, t.ring))
    return best


def _stage_raised(zone):
    """Walkways, raised canopies, raised blocks."""
    others = [u for u in zone if u.role not in (JET_BRIDGE, WALKWAY)]
    by_key = {}
    for u in zone:
        by_key.setdefault(u.key, []).append(u)
    for u in zone:
        if u.role is not None or u.mh < RAISED_M:
            continue
        if u.width <= WALKWAY_WIDTH_M and u.length >= WALKWAY_MIN_LEN_M and u.length >= BRIDGE_RATIO * u.width:
            a, b = _ends(u)
            ka = {o.key for o in others if o is not u and o.key != u.key and o.area >= 30.0 and _pt_near(a, o)}
            kb = {o.key for o in others if o is not u and o.key != u.key and o.area >= 30.0 and _pt_near(b, o)}
            if any(x != y for x in ka for y in kb):
                u.role = WALKWAY
                continue
        if _stacked(u, by_key):
            continue                                # (a storey on another part: it stands on it, left to its building's role)
        thick = u.h - u.mh if u.h == u.h else float("inf")
        if thick <= CANOPY_THICK_M and u.area >= CANOPY_MIN_M2:
            u.role = CANOPY
        else:
            u.role = RAISED_BLOCK


def _share_inside(u, t):
    """The share of u's area inside t's outline, from about INSIDE_SAMPLES grid points inside u (its centre if none fall in)."""
    if (u.box[1] < t.box[0]).any() or (u.box[0] > t.box[1]).any():
        return 0.0
    lo, hi = u.box
    step = max(dmath.sqrt(u.area / INSIDE_SAMPLES), 0.25)
    gx, gy = np.meshgrid(np.arange(lo[0] + step / 2, hi[0], step), np.arange(lo[1] + step / 2, hi[1], step))
    g = np.stack([gx.ravel(), gy.ravel()], 1)
    g = g[polygon.contains_points(u.ring, g)] if len(g) else g
    if not len(g):
        g = np.asarray(u.c, np.float64)[None]
    return float(polygon.contains_points(t.ring, g).mean())


def _stage_inside(zone, res):
    """Roles.inside: what lies mostly (INSIDE_SHARE) inside a terminal's or pier's outline (a footprint, or a part of another
    building: the blocks between Brisbane's terminal roofs): the terminal look. Not a terminal, pier, bridge, walkway, tower or
    hangar (a role of their own)."""
    terms = [u for u in zone if u.role in (TERMINAL, PIER) and not u.part]
    if not terms:
        return
    for u in zone:
        if u.role in (TERMINAL, PIER, JET_BRIDGE, WALKWAY, TOWER, HANGAR):
            continue
        if any(t is not u and _share_inside(u, t) > INSIDE_SHARE for t in terms):
            res.inside.add(u.id)


def _stacked(u, by_key):
    """Does u (raised) stand on a lower part of its own building (one whose top reaches its underside, STACK_TOL_M)?"""
    for o in by_key.get(u.key, ()):
        if o is u or o.has_parts or o.h != o.h or o.mh >= u.mh or o.h < u.mh - STACK_TOL_M:
            continue
        if (o.box[0] <= u.c).all() and (u.c <= o.box[1]).all() and bool(polygon.contains_points(o.ring, u.c[None])[0]):
            return True
    return False


def _pt_near(p, o):
    if (p < o.box[0] - WALK_JOIN_M).any() or (p > o.box[1] + WALK_JOIN_M).any():
        return False
    return _pt_dist(p, o.ring) <= WALK_JOIN_M


def _link_gates(zone, gate_xy, gids, res):
    """Roles.gates: each gate's nearest bridge with an end within GATE_BRIDGE_M of it."""
    br = res.bridges
    terms = [u for u in zone if u.role in (TERMINAL, PIER)]
    for gid in gids:
        x, y = gate_xy[gid]
        got = []
        for bid in sorted(br):
            _d, root, outer, direction = br[bid]
            d = min(float(dnp.hypot(root[0] - x, root[1] - y)), float(dnp.hypot(outer[0] - x, outer[1] - y)))
            if d <= GATE_BRIDGE_M:
                got.append((d, bid, outer, direction))
        got.sort(key=lambda t: (t[0], t[1]))
        tdist = [(_pt_dist((x, y), t.ring), t.id) for t in terms
                 if (t.box[0] - 60.0 <= (x, y)).all() and ((x, y) <= t.box[1] + 60.0).all()]
        tn = min(tdist) if tdist else None
        info = {"xy": (x, y), "bridge": None, "end": None, "dir": None, "dist": None, "n_bridges": len(got),
                "terminal": tn[1] if tn and tn[0] <= 60.0 else None}
        if got:
            d, bid, outer, direction = got[0]
            info.update(bridge=bid, end=(float(outer[0]), float(outer[1])), dir=(float(direction[0]), float(direction[1])),
                        dist=d)
        res.gates[gid] = info


def _parking(ctx, infra, landuse, aprons, res):
    """Roles.airside_parking: parking lots (infrastructure and land use) mostly inside an apron."""
    if not aprons:
        return
    seen = set()
    for f in infra + _flat(landuse):
        cls = f.attrs.get("class")
        if cls is None or not len(f):
            continue
        cls = np.asarray(cls).astype(str)
        for i in np.flatnonzero(np.isin(cls, list(PARKING_CLASS)) & (np.asarray(f.kind) == 3)):
            fid = str(f.ids[int(i)])
            for pi, part in enumerate(f.parts(int(i))):
                if not part or len(part[0]) < 3:
                    continue
                uid = fid if pi == 0 else f"{fid}#{pi}"
                if uid in seen:
                    continue
                seen.add(uid)
                ring = _to_xy(ctx.frame, np.asarray(part[0], np.float64), ctx.x0, ctx.y0)
                if len(ring) < 3:
                    continue
                s = _outline_samples(ring, 4.0)
                inside = np.zeros(len(s), bool)
                for r in aprons:
                    if (ring.max(0) < r.min(0)).any() or (ring.min(0) > r.max(0)).any():
                        continue
                    inside |= np.asarray(polygon.contains_points(r, s), bool)
                if float(inside.mean()) >= PARKING_APRON_SHARE:
                    res.airside_parking.append({"id": uid, "ring": ring})
    res.airside_parking.sort(key=lambda p: p["id"])



def airside_rings(infra, frame, x0=0.0, y0=0.0, landuse=None):
    """[(k, 2)] metres: the outlines of the airport's aprons and of its mapped parking lots that lie mostly inside one
    (Roles.airside_parking): the stands' ground, where no car parks and no stalls are marked (parking.tile_lot_spots).
    [] where the infrastructure Features (one or a list: the cells') have no airport."""
    infra = _flat(infra)
    ctx = ab.context(frame, infra, x0, y0)
    if ctx is None:
        return []
    aprons = _apron_rings(ctx, infra)
    res = Roles()
    _parking(ctx, infra, landuse, aprons, res)
    return aprons + [p["ring"] for p in res.airside_parking]
