"""Airside props that make an airport look busy: ground-support vehicles round each parked aircraft, floodlight masts along
the aprons' edges and rows of cargo containers beside the hangars and cargo sheds, as forest rows of the mod's static prop
groups gse_* (tools/build_gse.py; origin on the ground under the middle of the footprint, forward along local +Y, scale 1,
the row's height the model's: SPECS, trees.lua CATALOG) and the stock high mast (with its lens and glow rows, which light at
night). Pure: lat / lon points (tile_rows takes the ground and the frame), after parkedaircraft's placement.

props(feats, buildings) -> [Prop]: for a cell's infrastructure Features and its buildings (as parkedaircraft.stands: the
list of the cell's and its neighbours' buildings), kept on the Features.
Vehicles (an aircraft of the turboprop, narrow and wide kinds; the light singles get none; the Craft's own `mode` tells a
gate from a remote stand), each by a hash of the aircraft's position (never random: a rebuilt tile is the same), one handler's
livery (white, yellow or grey) for a block of stands, and only where the box of the vehicle is clear of the aircraft's
rectangles (and the other vehicles), of every building and building part (parkedaircraft's footprints: canopies and
covered walkways too, whatever their height; CLEAR_BUILDING_M margin; the small building touching a gate, a jet bridge,
excepted) and of the pavement of runways, stopways and taxiways (a taxilane's
centreline is no obstacle: a stand's lead-in runs through the nose):
  pushback tug  gates: ahead of the nose, facing the aircraft;
  baggage train a tug and 2-3 carts on the aircraft's right side, aft of the wing, the tug leading forward;
  belt loader   the boom's tip on the right side at the forward hold, the body angled off ahead;
  fuel truck    beyond the right wing tip, along the aircraft (or catering truck: left side, its rear against the fuselage
                at the aft door);
  stairs        remote stands: at the forward left door, the platform at the fuselage.
Apron masts: along the edge of each apron of at least MAST_APRON_MIN_M2 every MAST_SPACING_M (a hash picks the start),
MAST_INSET_M inside the edge (so inside the apron's polygon), skipping a spot on a taxiway's or runway's pavement, within
MAST_TAXI_M of a taxiway's or taxilane's centreline or MAST_RUNWAY_M of a runway's (an apron's grass island between them
takes none), within MAST_STAND_M of an aircraft or MAST_GATE_M of a gate, or at a building.
Clutter: by each hangar or cargo building (class hangar / warehouse / industrial, or named hangar, cargo, freight, logistic
or maintenance; CLUTTER_MIN_M2 to CLUTTER_MAX_M2) on or by an apron (within CLUTTER_APRON_M): a row of 2-4 20 ft cargo
containers (some stacked two high) along its longest free wall, CLUTTER_OFFSET_M off it, and a cone at each corner of the
coned-off rectangle.

tile_rows(frame, box, infra_fn, buildings_fn, elev) -> forest rows [group, x, y, z, yaw, 1, H, rank] of what a tile
(box: x0, y0, x1, y1 in frame, or a geotile's OwnedBox) owns (each by its place in one tile). The rank picks the model of the
group's variants (trees.lua pick: floor(rank * 7919) % count): the livery. A vehicle drops with an aircraft that parkedaircraft's
cross-cell pass drops. Same arguments as parkedaircraft.tile_rows; a cell without gates or aprons costs one test.
"""
import collections
import math
import re

import numpy as np

import aeroways
import dmath
import dnp
import geotiles
import parkedaircraft as pa

GROUPS = {"tug": "gse_tug", "cart": "gse_cart", "pushback": "gse_pushback", "belt": "gse_belt", "fuel": "gse_fuel",
          "catering": "gse_catering", "stairs": "gse_stairs", "container": "gse_container", "cone": "gse_cone"}
# the footprint along the model's +Y (from, to), its half width and its height m (build_gse.py's DIMS; checked in the tests)
SPECS = {"tug": (-1.45, 1.45, 0.71, 1.8), "cart": (-1.5, 1.5, 0.85, 1.1), "pushback": (-1.82, 1.82, 1.13, 1.77),
         "belt": (-3.0, 3.0, 1.125, 2.54), "fuel": (-3.6, 3.66, 1.25, 2.9), "catering": (-3.6, 3.66, 1.25, 4.4),
         "stairs": (-2.8, 3.2, 1.15, 3.4), "container": (-3.03, 3.06, 1.25, 2.59), "cone": (-0.18, 0.18, 0.18, 0.70)}
VARIANTS = {k: (1 if k == "cone" else 3) for k in GROUPS}       # the models of a group (liveries, container colours)
MAST_GROUP, MAST_H = "streetlight_high_mast", 18.03             # the stock high mast (worldprops.HIGH_MAST_H)
# An apron mast's light is its own, not the road masts' (furniture.G_HIGH_MAST_LENS): nightlights.lua gives it a wide, flat
# floodlight (APRON_MAST) and a share of the light pool kept for it, so a whole apron lights at once (the user, 2026-11-17:
# real aprons are one even wash of light under every mast, near and far; ours were a few bright puddles by the camera and
# nothing beyond). Two more rows at each mast: its head a cluster of floodlights (the lens and
# the glow ball at MAST_HEAD_SCALE, the largest trees.lua pick gives), and a pool of light on the ground (G_POOL: a flat,
# soft-edged glow, night only, drawn only beyond the real lights' reach: tools/build_apron_glow.py).
G_LENS, G_POOL = "apron_mast_light", "apron_glow"
MAST_HEAD_SCALE = 2.5
POOL_LIFT_M = 0.12              # the pool over the ground under the mast (clear of the pavement's own layers)
CART_PITCH_M = 3.15             # a cart's centre behind the one before (3.0 m long, 0.15 m between)
HITCH_M = 3.1                   # the first cart's centre behind the tug's
BELT_ANGLE_DEG = 50.0           # the belt loader's axis to the fuselage's
BELT_TIP_GAP_M = 0.55           # the boom's tip off the skin
CATERING_GAP_M = 0.6            # the catering truck's rear off the skin
STAIRS_GAP_M = 0.45
NOSE_TUG_GAP_M = 0.9            # the pushback tug's front off the nose
FUEL_WING_GAP_M = 2.6           # the fuel truck's axis beyond the wing tip
CLEAR_AIRCRAFT_M = 0.3          # a vehicle's margin to an aircraft's rectangles
CLEAR_BUILDING_M = 0.6
PCT = {                         # percent of the stands that have each (gate, remote)
    "pushback": (45, 0), "train": (55, 45), "belt": (45, 35), "truck": (30, 30), "stairs": (0, 65)}
MAST_SPACING_M = 90.0
MAST_APRON_MIN_M2 = 2500.0
MAST_INSET_M = 1.2
MAST_STAND_M = 3.0
MAST_BUILDING_M = 2.0
MAST_LANE_M = 4.0               # m from a taxilane's centreline
MAST_TAXI_M = 25.0              # m from any taxiway's or taxilane's centreline (wing tips pass)
MAST_RUNWAY_M = 60.0            # m from a runway's centreline
MAST_GATE_M = 10.0              # m from a gate's point (a stand, occupied or not)
MAST_APART_M = 40.0
MAST_MAX = 30                   # an apron's
CLUTTER_CLASSES = {"hangar", "warehouse", "industrial", "cargo"}
CLUTTER_NAME = re.compile(r"hangar|hangr|cargo|freight|logistic|maintenance", re.I)
CLUTTER_MIN_M2, CLUTTER_MAX_M2 = 250.0, 30000.0
CLUTTER_APRON_M = 25.0
CLUTTER_OFFSET_M = 5.0
CLUTTER_EDGE_MIN_M = 12.0
CLUTTER_ROWS_MAX = 8            # a cell's
CLUTTER_STAND_M = 3.0
CONTAINER_PITCH_M = 6.7
STEP_M = 1.0                    # the sampling of a footprint
_KEY = "_airside_props"

Prop = collections.namedtuple("Prop", "group lat lon he hn lift variant owner")    # he, hn: the model's +Y, east / north;
                                                                                   # owner: its aircraft's index in the cell's stands


def _crc(*parts):
    return pa._crc(":".join(str(p) for p in parts))


def _norm(x, y):
    n = float(dnp.hypot(x, y))
    return (x / n, y / n) if n > 1e-9 else (0.0, 1.0)


# ------------------------------------------------------------------------------------------------- footprints
def _box_pts(kind, cx, cy, hx, hy):
    """The sample points (n, 2) of a vehicle's footprint at (cx, cy), the model's +Y along (hx, hy) (a unit vector)."""
    y0, y1, hw, _h = SPECS[kind]
    us = np.linspace(y0, y1, max(2, int(math.ceil((y1 - y0) / STEP_M)) + 1))
    vs = np.linspace(-hw, hw, max(2, int(math.ceil(2.0 * hw / STEP_M)) + 1))
    g = np.stack(np.meshgrid(us, vs), -1).reshape(-1, 2)
    return np.stack([cx + g[:, 0] * hx - g[:, 1] * hy, cy + g[:, 0] * hy + g[:, 1] * hx], 1)


def _in_box(pts, kind, cx, cy, hx, hy, margin):
    """Which points lie within `margin` of a vehicle's footprint."""
    y0, y1, hw, _h = SPECS[kind]
    d = pts - (cx, cy)
    u = d[:, 0] * hx + d[:, 1] * hy
    v = -d[:, 0] * hy + d[:, 1] * hx
    return (u >= y0 - margin) & (u <= y1 + margin) & (np.abs(v) <= hw + margin)


def _reach(kind):
    y0, y1, hw, _h = SPECS[kind]
    return max(abs(y0), abs(y1)) + hw


class _Site:
    """A cell's airport in its plane: what the airside props keep off (buildings, pavement, the aircraft) and the ones
    placed so far."""

    def __init__(self, sc, obstacles, crafts, exempt=()):
        self.sc = sc
        self.world = pa._World(sc, obstacles)
        self.exempt = frozenset(exempt)         # indexes of the obstacles that are no obstacle to a vehicle (jet bridges)
        self.crafts = crafts                    # [(kind, x, y, nx, ny)]
        self.placed = []                        # (kind, cx, cy, hx, hy, pts)
        self.taxi = self._segs(sc.lanes)        # the taxiways' and taxilanes' centrelines, the runways' (a, b)
        self.runway = self._segs(sc.runways)
        self.gates = np.array([[g[1], g[2]] for g in sc.gates]).reshape(-1, 2)

    @staticmethod
    def _segs(lines):
        lines = [ln for ln in lines if len(ln) >= 2]
        if not lines:
            return np.zeros((0, 2)), np.zeros((0, 2))
        return np.concatenate([ln[:-1] for ln in lines]), np.concatenate([ln[1:] for ln in lines])

    def free(self, kind, cx, cy, hx, hy, aircraft_m=CLEAR_AIRCRAFT_M, pavement=True):
        """Is a `kind` vehicle at (cx, cy), its +Y along (hx, hy), clear of the buildings, the aircraft, the vehicles
        placed and (pavement) the runways', stopways' and taxiways' pavement?"""
        w = self.world
        pts = _box_pts(kind, cx, cy, hx, hy)
        r = _reach(kind) + CLEAR_BUILDING_M
        for oi, (ring, (x0, y0, x1, y1)) in enumerate(w.rings):
            if x0 - r > cx or x1 + r < cx or y0 - r > cy or y1 + r < cy or oi in self.exempt:
                continue
            if pa._pip(ring, pts).any() or pa._seg_dist(pts, ring, np.roll(ring, -1, axis=0)).min() < CLEAR_BUILDING_M:
                return False
        if pavement and len(w.a):
            keep = ~w.centre
            keep &= (np.minimum(w.a[:, 0], w.b[:, 0]) - r - w.half <= cx) & (np.maximum(w.a[:, 0], w.b[:, 0]) + r + w.half >= cx)
            keep &= (np.minimum(w.a[:, 1], w.b[:, 1]) - r - w.half <= cy) & (np.maximum(w.a[:, 1], w.b[:, 1]) + r + w.half >= cy)
            if keep.any() and (pa._seg_dist(pts, w.a[keep], w.b[keep]).min(0) < w.half[keep]).any():
                return False
        for k2, x2, y2, nx2, ny2 in self.crafts:
            if math.hypot(x2 - cx, y2 - cy) > pa._radius(k2) + r + aircraft_m:
                continue
            if pa._in_rects(k2, x2, y2, nx2, ny2, pts, aircraft_m).any():
                return False
            if _in_box(pa._world(pa._local_points(k2), x2, y2, nx2, ny2), kind, cx, cy, hx, hy, aircraft_m).any():
                return False
        for k2, x2, y2, hx2, hy2, pts2 in self.placed:
            if math.hypot(x2 - cx, y2 - cy) > _reach(kind) + _reach(k2) + 0.4:
                continue
            if _in_box(pts, k2, x2, y2, hx2, hy2, 0.2).any() or _in_box(pts2, kind, cx, cy, hx, hy, 0.2).any():
                return False
        return True

    def add(self, kind, cx, cy, hx, hy):
        self.placed.append((kind, cx, cy, hx, hy, _box_pts(kind, cx, cy, hx, hy)))

    def point_free(self, x, y, ring, building_m, lane_m, stand_m, taxi_m=0.0, runway_m=0.0, gate_m=0.0):
        """May a mast stand at (x, y): inside `ring`, off the buildings, the pavement of runways and taxiways, the
        taxilanes' centrelines (lane_m), the aircraft (stand_m), and clear of every taxiway's and taxilane's centreline
        (taxi_m), runway's centreline (runway_m) and gate point (gate_m)?"""
        p = np.array([[x, y]])
        if not pa._pip(ring, p)[0]:
            return False
        w = self.world
        for rg, (x0, y0, x1, y1) in w.rings:
            if x0 - building_m > x or x1 + building_m < x or y0 - building_m > y or y1 + building_m < y:
                continue
            if pa._pip(rg, p)[0] or pa._seg_dist(p, rg, np.roll(rg, -1, axis=0)).min() < building_m:
                return False
        if len(w.a):
            d = pa._seg_dist(p, w.a, w.b)[0]
            if (d < np.where(w.centre, lane_m, w.half)).any():
                return False
        for (a, b), m in ((self.taxi, taxi_m), (self.runway, runway_m)):
            if m > 0.0 and len(a) and pa._seg_dist(p, a, b).min() < m:
                return False
        if gate_m > 0.0 and len(self.gates) and float(dnp.hypot(*(self.gates - (x, y)).T).min()) < gate_m:
            return False
        for k2, x2, y2, nx2, ny2 in self.crafts:
            if math.hypot(x2 - x, y2 - y) <= pa._radius(k2) + stand_m and pa._in_rects(k2, x2, y2, nx2, ny2, p, stand_m)[0]:
                return False
        return True


# ------------------------------------------------------------------------------------------------- vehicles
def _frame(c, u, v):
    """The plane point at u along the aircraft's nose and v to its left of the aircraft c = (kind, x, y, nx, ny)."""
    return c[1] + u * c[3] - v * c[4], c[2] + u * c[4] + v * c[3]


def _dirf(c, du, dv):
    return _norm(du * c[3] - dv * c[4], du * c[4] + dv * c[3])


def _vehicles(site, i, craft, c):
    """[(kind, x, y, hx, hy)] of the vehicles at an aircraft c = (kind, x, y, nx, ny) (the Craft `craft` at index i)."""
    kind = c[0]
    if kind == "light":
        return []
    length, span, _h, fus, chord, wing_at = pa.SPECS[kind]
    gate = craft.mode == "gate"
    h = lambda tag: _crc(f"{craft.lat:.5f}", f"{craft.lon:.5f}", kind, tag)
    want = lambda tag: h(tag) % 100 < PCT[tag][0 if gate else 1]
    out = []

    def put(vk, x, y, hx, hy, margin=CLEAR_AIRCRAFT_M):
        if site.free(vk, x, y, hx, hy, margin):
            site.add(vk, x, y, hx, hy)
            out.append((vk, x, y, hx, hy))
            return True
        return False

    u_wle = wing_at + chord / 2.0                       # the wing's leading / trailing edge
    u_wte = wing_at - chord / 2.0
    if want("pushback"):                                # ahead of the nose, facing the aircraft
        x, y = _frame(c, length / 2.0 + NOSE_TUG_GAP_M + SPECS["pushback"][1], 0.0)
        hx, hy = _dirf(c, -1.0, 0.0)
        put("pushback", x, y, hx, hy)
    if want("train"):                                   # the right side, aft of the wing, the tug leading forward
        n0 = 2 + h("carts") % 2
        for lat_m in (fus / 2.0 + 4.0, fus / 2.0 + 6.5):
            done = False
            for n in range(n0, 0, -1):
                u_t = u_wte - 1.5
                els = [("tug", u_t)] + [("cart", u_t - HITCH_M - CART_PITCH_M * j) for j in range(n)]
                hx, hy = _dirf(c, 1.0, 0.0)
                spots = [(k, *_frame(c, u, -lat_m)) for k, u in els]
                if all(site.free(k, x, y, hx, hy, CLEAR_AIRCRAFT_M) for k, x, y in spots):
                    for k, x, y in spots:
                        site.add(k, x, y, hx, hy)
                        out.append((k, x, y, hx, hy))
                    done = True
                    break
            if done:
                break
    if want("belt"):                                    # the boom's tip at the forward hold, the body angled off ahead
        u_h = u_wle + (length / 2.0 - u_wle) * 0.45
        a = math.radians(BELT_ANGLE_DEG)
        half = SPECS["belt"][1]
        tip = _frame(c, u_h, -(fus / 2.0 + BELT_TIP_GAP_M))
        hx, hy = _dirf(c, -dmath.cos(a), dmath.sin(a))         # (from the body to the tip: aft and in)
        put("belt", tip[0] - hx * half, tip[1] - hy * half, hx, hy)
    if want("truck"):
        if h("which") % 2 == 0:                         # fuel, beyond the right wing tip, along the aircraft
            for side in (-1.0, 1.0):
                x, y = _frame(c, wing_at, side * (span / 2.0 + FUEL_WING_GAP_M))
                hx, hy = _dirf(c, 1.0 if h("way") % 2 else -1.0, 0.0)
                if put("fuel", x, y, hx, hy):
                    break
        else:                                           # catering, left side, the rear against the aft door
            x, y = _frame(c, -0.27 * length, fus / 2.0 + CATERING_GAP_M - SPECS["catering"][0])
            hx, hy = _dirf(c, 0.0, 1.0)
            put("catering", x, y, hx, hy)
    if want("stairs"):                                  # the forward left door, the platform at the fuselage
        x, y = _frame(c, length / 2.0 - pa.BACK_M[kind], fus / 2.0 + STAIRS_GAP_M + SPECS["stairs"][1])
        hx, hy = _dirf(c, 0.0, -1.0)
        put("stairs", x, y, hx, hy)
    return out


# ------------------------------------------------------------------------------------------------- masts, clutter
def _masts(site):
    """[(x, y)] of the apron masts."""
    sc = site.sc
    out = []
    for aid, ring, _ga in sorted(sc.aprons, key=lambda a: a[0]):
        if pa._area(ring) < MAST_APRON_MIN_M2:
            continue
        c = ring.mean(0)
        if pa._aerodrome(sc, float(c[0]), float(c[1])) == "none":
            continue
        closed = np.r_[ring, ring[:1]] if not np.allclose(ring[0], ring[-1]) else ring
        seg = np.diff(closed, axis=0)
        ln = dnp.hypot(seg[:, 0], seg[:, 1])
        cum = np.r_[0.0, np.cumsum(ln)]
        sign = 1.0 if float(dnp.dot(closed[:-1, 0], closed[1:, 1]) - dnp.dot(closed[1:, 0], closed[:-1, 1])) > 0 else -1.0
        first = (pa._crc(aid + ":mast") % 1000) / 1000.0 * MAST_SPACING_M
        got = 0
        for k in range(int(cum[-1] // MAST_SPACING_M)):
            s = first + k * MAST_SPACING_M
            j = min(int(np.searchsorted(cum, s, side="right")) - 1, len(seg) - 1)
            if ln[j] < 1e-6:
                continue
            t = seg[j] / ln[j]
            p = closed[j] + t * (s - cum[j]) + sign * np.array([-t[1], t[0]]) * MAST_INSET_M
            x, y = float(p[0]), float(p[1])
            if got >= MAST_MAX or any(math.hypot(x - x2, y - y2) < MAST_APART_M for x2, y2 in out):
                continue
            if site.point_free(x, y, ring, MAST_BUILDING_M, MAST_LANE_M, MAST_STAND_M, MAST_TAXI_M, MAST_RUNWAY_M, MAST_GATE_M):
                out.append((x, y))
                got += 1
    return out


def _text(col, i):
    if col is None:
        return ""
    v = col[i]
    return "" if v is None else str(v)


def _clutter_buildings(sc, blds):
    """The hangar and cargo buildings' rings (planes, (k, 2)) on or by an apron, largest first, deduplicated by id."""
    if not sc.aprons:
        return []
    allp = np.concatenate([r for _i, r, _g in sc.aprons])
    pad = CLUTTER_APRON_M + 20.0
    lo, hi = allp.min(0) - pad, allp.max(0) + pad
    seen, out = set(), []
    for bld in (b for b in (blds if isinstance(blds, (list, tuple)) else [blds]) if b is not None and len(b)):
        cls, name = bld.attrs.get("class"), bld.attrs.get("name")
        under = bld.attrs.get("is_underground")
        for i in range(len(bld)):
            if int(bld.kind[i]) != 3 or (under is not None and bool(under[i])):
                continue
            if not (_text(cls, i).lower() in CLUTTER_CLASSES or CLUTTER_NAME.search(_text(name, i))):
                continue
            for pi, part in enumerate(bld.parts(i)):
                if not part or len(part[0]) < 3 or (str(bld.ids[i]), pi) in seen:
                    continue
                seen.add((str(bld.ids[i]), pi))
                ring = sc.xy(part[0])
                if (ring.max(0) < lo).any() or (ring.min(0) > hi).any():
                    continue
                area = pa._area(ring)
                if CLUTTER_MIN_M2 <= area <= CLUTTER_MAX_M2 and _near_apron(sc, ring):
                    out.append((area, str(bld.ids[i]), pi, ring))
    out.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [t[3] for t in out]


def _near_apron(sc, pts):
    """Is any of the points (k, 2) in an apron, or any of an apron's edges within CLUTTER_APRON_M of them?"""
    for _i, ring, _g in sc.aprons:
        if pa._pip(ring, pts).any() or pa._seg_dist(pts, ring, np.roll(ring, -1, axis=0)).min() < CLUTTER_APRON_M:
            return True
    return False


def _clutter(site, blds):
    """[(kind, x, y, hx, hy, lift)] of the containers and cones by the hangars and cargo sheds."""
    sc = site.sc
    out = []
    for n_row, ring in enumerate(_clutter_buildings(sc, blds)):
        if n_row >= CLUTTER_ROWS_MAX:
            break
        key = _crc("clutter", f"{ring[:, 0].min():.1f}", f"{ring[:, 1].min():.1f}")
        a, b = ring, np.roll(ring, -1, axis=0)
        ln = dnp.hypot(*(b - a).T)
        for j in sorted(range(len(ring)), key=lambda j: (-float(ln[j]), j))[:4]:
            if ln[j] < CLUTTER_EDGE_MIN_M:
                break
            t = (b[j] - a[j]) / ln[j]
            nrm = np.array([-t[1], t[0]])
            if bool(pa._pip(ring, ((a[j] + b[j]) / 2.0 + nrm * 0.5)[None])[0]):
                nrm = -nrm
            n0 = 2 + (key >> 3) % 3
            for n in range(n0, 1, -1):
                row_len = n * CONTAINER_PITCH_M - 0.6
                if ln[j] < row_len + 4.0:
                    continue
                s0 = 2.0 + ((key >> 7) % 100) / 100.0 * (ln[j] - row_len - 4.0)
                cen = [a[j] + t * (s0 + 3.05 + i * CONTAINER_PITCH_M) + nrm * (CLUTTER_OFFSET_M + 1.25) for i in range(n)]
                if not all(site.free("container", float(p[0]), float(p[1]), float(t[0]), float(t[1]), CLUTTER_STAND_M)
                           and _near_apron(sc, p[None]) for p in cen):
                    continue
                for i, p in enumerate(cen):
                    site.add("container", float(p[0]), float(p[1]), float(t[0]), float(t[1]))
                    out.append(("container", float(p[0]), float(p[1]), float(t[0]), float(t[1]), 0.0))
                    if (key >> (11 + i)) % 3 == 0 and i < 2:                # (a second tier on some)
                        out.append(("container", float(p[0]), float(p[1]), float(t[0]), float(t[1]), SPECS["container"][3]))
                for s, off in ((s0 - 1.0, CLUTTER_OFFSET_M - 1.4), (s0 + row_len + 1.0, CLUTTER_OFFSET_M - 1.4),
                               (s0 - 1.0, CLUTTER_OFFSET_M + 3.9), (s0 + row_len + 1.0, CLUTTER_OFFSET_M + 3.9)):
                    p = a[j] + t * s + nrm * off
                    if site.free("cone", float(p[0]), float(p[1]), 0.0, 1.0, 0.5):
                        site.add("cone", float(p[0]), float(p[1]), 0.0, 1.0)
                        out.append(("cone", float(p[0]), float(p[1]), 0.0, 1.0, 0.0))
                break
            else:
                continue
            break
    return out


# ------------------------------------------------------------------------------------------------- the cell
def _plane(sc, craft):
    """(kind, x, y, nx, ny) of a Craft in the scene's plane."""
    return (craft.kind, (craft.lon - sc.lon0) * sc.kx, (craft.lat - sc.lat0) * aeroways.M_PER_DEG, craft.ne, craft.nn)


def props(feats, buildings=None):
    """[Prop] of a cell's infrastructure Features (see the module docstring), kept on the Features (by the buildings it
    was asked with); [] for Features without airport gates or aprons."""
    if feats is None or len(feats) == 0 or not pa.has_stands(feats):
        return []
    buildings = pa._flat(buildings)
    same = lambda a, b: len(a) == len(b) and all(x is y for x, y in zip(a, b))
    got = feats.__dict__.get(_KEY)
    if got is not None and same(got[0], buildings):
        return got[1]
    sc = feats.__dict__[pa._KEY + "_scene"]
    crafts = pa.stands(feats, buildings)
    obstacles, small = pa._buildings(sc, buildings)
    planar = [_plane(sc, c) for c in crafts]
    gate_xy = np.array([[g[1], g[2]] for g in sc.gates]).reshape(-1, 2)
    exempt = [oi for ring, _a, oi in small if len(gate_xy) and
              min(float(dnp.hypot(*(ring - g).T).min()) for g in gate_xy) <= pa.ATTACH_M]   # (a jet bridge touches its gate)
    site = _Site(sc, obstacles, planar, exempt)
    out = []

    def emit(group, x, y, hx, hy, lift=0.0, variant=0, owner=None):
        lat, lon = sc.latlon(x, y)
        out.append(Prop(group, lat, lon, hx, hy, lift, variant, owner))

    for i, (craft, c) in enumerate(zip(crafts, planar)):
        livery = (_crc(f"{craft.lat:.3f}", f"{craft.lon:.3f}", "handler") + (1 if _crc(f"{craft.lat:.5f}", f"{craft.lon:.5f}", "other") % 4 == 0 else 0)) % VARIANTS["tug"]
        for vk, x, y, hx, hy in _vehicles(site, i, craft, c):
            emit(GROUPS[vk], x, y, hx, hy, 0.0, livery, i)
    for x, y in _masts(site):
        emit(MAST_GROUP, x, y, 0.0, 1.0)
    for vk, x, y, hx, hy, lift in _clutter(site, buildings):
        emit(GROUPS[vk], x, y, hx, hy, lift, _crc(f"{x:.1f}", f"{y:.1f}", "colour") % VARIANTS[vk])
    feats.__dict__[_KEY] = (buildings, out)
    return out


def rank_for(group, variant):
    """The row's rank that makes trees.lua's pick choose the group's model number `variant` (floor(rank * 7919) % count)."""
    n = {g: VARIANTS[k] for k, g in GROUPS.items()}.get(group, 1)
    return round((3 * n + variant + 0.5) / 7919.0, 6) if n > 1 else 0.0


# ------------------------------------------------------------------------------------------------- the tile's rows
def tile_rows(frame, box, infra_fn, buildings_fn, elev, keys=None):
    """Forest rows [group, x, y, z, yaw, 1, H, rank] of the airside props a tile owns (see the module docstring)."""
    if keys is None:
        import mapcells
        x0, y0, x1, y1 = (float(v) for v in box[:4])
        la, lo, _ = frame.enu_to_geodetic(np.array([x0, x0, x1, x1]), np.array([y0, y1, y0, y1]), np.zeros(4))
        try:
            keys = mapcells.cells_for_points(la, lo, pad_lat=0.001, pad_lon=0.001, max_cells=64)
        except (ValueError, OverflowError):
            return []
    cells = []                                          # (props, stands)
    for k in keys:
        feats = infra_fn(k)
        if not pa.has_stands(feats):
            continue
        blds = pa._flat([buildings_fn(k)] + [buildings_fn(nk) for nk in pa._near_cells(k, feats)])
        cells.append((props(feats, blds), pa.stands(feats, blds)))
    if not cells or not any(p for p, _s in cells):
        return []
    crafts = [(ci, i, c) for ci, (_p, st) in enumerate(cells) for i, c in enumerate(st)]
    kept = set()
    if crafts:                                          # (the cross-cell pass of the aircraft: a vehicle goes with its own)
        lat = np.array([c.lat for _a, _b, c in crafts], np.float64)
        lon = np.array([c.lon for _a, _b, c in crafts], np.float64)
        kept = {crafts[j][:2] for j in pa._dedupe([c for _a, _b, c in crafts], lat, lon)}
    items = [p for ci, (ps, _s) in enumerate(cells) for p in ps if p.owner is None or (ci, p.owner) in kept]
    if not items:
        return []
    lat = np.array([p.lat for p in items], np.float64)
    lon = np.array([p.lon for p in items], np.float64)
    e, n, _ = frame.geodetic_to_enu(lat, lon, 0.0)
    e, n = np.asarray(e, np.float64), np.asarray(n, np.float64)
    own = np.flatnonzero(np.asarray(geotiles.box_owns(box, e, n), bool))
    if not len(own):
        return []
    seen, pick = set(), []
    for j in own.tolist():                              # (a prop twice, from two cells holding one apron)
        key = (items[j].group, round(float(e[j]) * 2.0), round(float(n[j]) * 2.0), items[j].lift)
        if key not in seen:
            seen.add(key)
            pick.append(j)
    pick = np.array(pick, np.int64)
    kx = aeroways.M_PER_DEG * dnp.cos(np.radians(lat[pick]))
    he = np.array([items[j].he for j in pick], np.float64)
    hn = np.array([items[j].hn for j in pick], np.float64)
    la2, lo2 = lat[pick] + hn * 10.0 / aeroways.M_PER_DEG, lon[pick] + he * 10.0 / kx
    e2, n2, _ = frame.geodetic_to_enu(la2, lo2, 0.0)
    h = np.asarray(elev(lat[pick], lon[pick]), np.float64)
    z = np.asarray(frame.geodetic_to_enu(lat[pick], lon[pick], h)[2], np.float64)
    heights = {g: SPECS[k][3] for k, g in GROUPS.items()}
    heights[MAST_GROUP] = MAST_H
    rows, masts = [], []
    for j, x, y, zz, xe, yn in zip(pick.tolist(), e[pick], n[pick], z, np.asarray(e2, np.float64), np.asarray(n2, np.float64)):
        p = items[j]
        row = [p.group, round(float(x), 2), round(float(y), 2), round(float(zz) + p.lift, 2),
               round(pa._yaw(float(xe - x), float(yn - y)), 1), 1.0, heights[p.group],
               rank_for(p.group, p.variant) if p.group != MAST_GROUP else 0.0]
        (masts if p.group == MAST_GROUP else rows).append(row)
    if masts:                                           # (a lens and a glow at each mast's head, a pool on the ground: they light at night)
        import furniture
        rows += masts + furniture.lamp_lenses(masts, G_LENS, MAST_HEAD_SCALE) + pool_rows(masts)
    return rows


def pool_rows(masts):
    """A G_POOL row [G_POOL, x, y, z, 0, 1, 1, 0] on the ground under each mast row (a flat glow of light, night only)."""
    return [[G_POOL, m[1], m[2], round(m[3] + POOL_LIFT_M, 2), 0.0, 1.0, 1.0, 0.0] for m in masts]


# The apron masts in the far and skyline tiles (the user, 2026-11-17: distant aprons were dark; the road tiles' props, the lens and
# glow rows and the pool, reach ~1.3 km): [x, y, z, c] points of each mast's head glow and ground pool, as airfieldlights.far_points'
# lights beside the tile's street lamps (blockworker._lamps_by_geotile). c is roads.lua's far lamp code: FAR_POOL the pool's group
# number (FAR_LAMP_GROUPS: after lampglow and the five afl colours), FAR_HEAD the head's glow, which the mod draws as lampglow at
# the height MAST_HEAD_SCALE (roads.FAR_HEAD_HEIGHT: a point has no scale of its own).
FAR_POOL, FAR_HEAD = 6, 7


def far_points(frame, box, infra_fn, buildings_fn, elev):
    """tile_rows' mast rows of a far tile (box in frame, or a geotile's OwnedBox: each mast in one tile, the arguments tile_rows')
    as [x, y, z, c]: a point at each of a mast's head glow rows and one on the ground under it (the pool). [] without apron masts."""
    import furniture
    out = []
    for r in tile_rows(frame, box, infra_fn, buildings_fn, elev):
        if r[0] == G_POOL:
            out.append([r[1], r[2], r[3], FAR_POOL])
        elif r[0] == furniture.G_GLOW:
            out.append([r[1], r[2], r[3], FAR_HEAD])
    return out
