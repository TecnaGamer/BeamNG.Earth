"""Airport buildings: an air traffic control tower and a terminal told from the map's other buildings (airport
context, worldwide), and the tower drawn as a tower.

Overture's buildings near airports rarely carry a height, floors or a class, and have no tower or terminal class: a
tower mapped as one footprint (Brisbane's "Air Traffic Control Tower", 75 m, 219 m2, no parts) was a plain box, a
terminal not called one ("Bozeman Yellowstone Intl. Airport (BZN)") a house-sized predicted block. The cell's
infrastructure (subtype airport) says where the airfield is: its gate points (class airport_gate) and its aprons and
taxiways (aeroways.pads).

context(frame, infra_layers, x0, y0) -> Context or None (no airport in the cells): relative to the tile's (x0, y0)
like the rings tile_buildings plans. Context.tower_parts(ring, at) says whether a footprint is a tower: the parts
of a shaft, a cab and a cap (the renderer draws them as any building's parts). What is a terminal, a pier, a jet bridge, a
walkway (the terminal look, a height by its size: terminal_height) is airportroles' (buildings.tile_buildings asks it). A building with parts of its own is never asked:
Heathrow's tower is an OSM stack of parts and was right already. Context.airside(ring, at) says a footprint or a part of any
size stands on the airside (within AIRSIDE_PAD_M of an apron or taxiway, AIRSIDE_GATE_M of a gate, neither a tower nor a
hangar): Brisbane's piers, satellites and walkways come as many small parts or footprints with no height, and were brick
houses (buildings.plan: glass and light panels, a flat roof, 6-9 m). pillars(ring) are the supports of a raised airside
part (a walkway over a stand), square concrete columns. Pure.
"""
import math
import re

import numpy as np

import aeroways
import dnp
import polygon

GATE_CLASS = "airport_gate"
TOWER_CLASS = "control_tower"   # (the class its parts are drawn as: no plant on the roofs, buildings._clutter)
TOWER_NAME = re.compile(r"control tower|\batc\b|air traffic|torre de control|tour de contr[oô]le|kontrollturm|"
                        r"flugsicherung|torre di controllo")
TOWER_WORD = re.compile(r"\btower\b")           # (a "tower" needs an airport word; the names above do not)
AIRPORT_WORD = re.compile(r"airport|airfield|aerodrome|airstrip|airside|air base|airbase|aeroport|flughafen")
NOT_TOWER_NAME = ("water", "cooling", "clock", "bell", "church", "hotel", "office", "apartment", "residen",
                  "hospital", "hangar", "terminal", "parking", "garage", "car park")
NOT_TOWER_CLASS = {"hotel", "apartments", "residential", "house", "dormitory", "office", "commercial", "retail",
                   "church", "cathedral", "chapel", "hospital", "parking", "parking_garage", "hangar", "terminal"}
TERMINAL_NAME = ("terminal", "concourse")       # (buildings.plan's name test)
NOT_TERMINAL_NAME = ("hangar", "parking", "garage", "car park")
NOT_TERMINAL_CLASS = {"parking", "parking_garage", "hangar", "garage", "garages", "carport"}

TOWER_NAME_MAX_M2 = 1500.0      # m2: a named tower's footprint at most this (a "Tower" in a big building's name is not)
TOWER_MIN_M2 = 16.0             # ... and a slender one's: at most TOWER_SLENDER_M2, compact, tall, near a pad
TOWER_SLENDER_M2 = 600.0
TOWER_COMPACT = 0.62            # 4 pi A / P^2 of at least this (a square is 0.79, a 2.5 : 1 rectangle 0.64)
TOWER_MIN_HEIGHT_M = 20.0       # m (or TOWER_MIN_FLOORS storeys) for a tower not named one
TOWER_MIN_FLOORS = 6
TOWER_PAD_M = 1000.0            # m: an unnamed tower is within this of an apron or taxiway
TOWER_H_INTL_M = 40.0           # m: a tower with no height, at an international airport / elsewhere
TOWER_H_M = 25.0
TOWER_FLOOR_M = 3.2             # m per storey where only its floors are mapped
TOWER_MIN_H_M = 15.0            # m: the least the three parts are drawn at (a shorter mapped height is the cab's trouble)
SHAFT_SHARE = 0.55              # the shaft: the footprint shrunk to this about its centroid ...
SHAFT_MIN_M = 4.0               # ... and at least this across (a footprint narrower than that is not shrunk)
CAB_FROM_TOP_M = 9.0            # the cab: from this far under the top ...
CAP_FROM_TOP_M = 4.0            # ... to this far under it, the cap above (a flat dark roof, 4 m)
CAB_SMALL_M = 10.0              # a footprint narrower than this: the cab is a regular octagon, this across at least
CAB_OCTAGON_K = 1.15            # (its across flats: this many of sqrt(area))
CAP_SHARE = 0.9                 # the cap, slightly smaller than the cab
SHAFT_COLOUR = "#e6e4de"        # (a tint on the texture; each part its own, else a part takes its siblings': the cap's dark)
CAB_COLOUR = "#a8c0cc"
CAB_ROOF = "#4a4d52"
CAP_COLOUR = "#2e3135"
GLASS = "glass"
SHAFT = "concrete"
TAN_8 = 0.41421356237309503     # tan(22.5 deg): the regular octagon's side, in halves of its across flats

TERMINAL_H_M = (12.0, 20.0)     # m: a terminal's height when not mapped, by its size (terminal_height): 8 m + sqrt(area) / 12

AIRSIDE_PAD_M = 30.0            # m: a building or part within this of an apron or taxiway pad is airside ...
AIRSIDE_GATE_M = 60.0           # ... or within this of a gate point
AIRSIDE_H_M = (6.0, 9.0)        # m: its height when not mapped (a pier's or a walkway's scale), by its seed
AIRSIDE_RAISED_M = 2.0          # m: a part this far off the ground is a walkway or a bridge: it floats, on pillars
PILLAR_M = 0.6                  # m: a pillar's side
PILLAR_SPACING_M = 12.0         # m between them along the part's long axis (the ends in)
PILLAR_INSET_M = 1.0            # m: the end pillars and the rows' inset from the part's outline
PILLAR_TWIN_M = 5.0             # m: a part wider than this has a row near each long edge, else one down the middle
PILLAR_MAX = 60                 # a part's pillars at most
PILLAR_CLASS = "pillar"
SAMPLE_M = 10.0                 # m: a footprint's outline sampled this finely for its distance to a pad

_KEY = "_airport_scan"


def _scan(f):
    """(has an airport, an international one, the rows of its gate points) of an infrastructure Features, kept on it."""
    got = f.__dict__.get(_KEY)
    if got is None:
        got = (False, False, np.empty(0, np.int64))
        sub, cls = f.attrs.get("subtype"), f.attrs.get("class")
        if sub is not None and cls is not None and len(f.kind):
            air = np.asarray(sub).astype(str) == aeroways.SUBTYPE
            cls = np.asarray(cls).astype(str)
            got = (bool(air.any()), bool((air & (cls == aeroways.AERODROME)).any()),
                   np.flatnonzero(air & (cls == GATE_CLASS) & (np.asarray(f.kind) == 1)))
        f.__dict__[_KEY] = got
    return got


def context(frame, infra_layers, x0, y0):
    """The tile's airport Context from the infrastructure Features of its cells, None when none has an airport."""
    layers = [f for f in infra_layers if f is not None and len(f.kind) and _scan(f)[0]]
    return Context(frame, layers, x0, y0) if layers else None


def _area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return abs(float(dnp.dot(x, np.roll(y, -1)) - dnp.dot(y, np.roll(x, -1)))) / 2.0


def _perimeter(ring):
    d = np.roll(ring, -1, axis=0) - ring
    return float(dnp.hypot(d[:, 0], d[:, 1]).sum())


def _short_side(area, perimeter):
    """The short side of the rectangle of this area and perimeter (its outline's, near enough for a footprint)."""
    s = perimeter / 2.0
    return (s - math.sqrt(max(s * s - 4.0 * area, 0.0))) / 2.0


def _outline_dist(pts, ring):
    """The distance from each point of pts (m, 2) to the closed outline ring (k, 2)."""
    a, ab = ring, np.roll(ring, -1, axis=0) - ring
    px, py = pts[:, 0, None] - a[None, :, 0], pts[:, 1, None] - a[None, :, 1]
    t = np.clip((px * ab[None, :, 0] + py * ab[None, :, 1])
                / np.maximum(ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1], 1e-12)[None], 0.0, 1.0)
    return dnp.hypot(px - t * ab[None, :, 0], py - t * ab[None, :, 1]).min(axis=1)


def terminal_height(area):
    """The height (m) a terminal of `area` m2 is drawn at when the map gives none: two or three tall glass storeys, by its size."""
    return float(np.clip(8.0 + math.sqrt(max(float(area), 0.0)) / 12.0, TERMINAL_H_M[0], TERMINAL_H_M[1]))


def octagon(c, across):
    """A regular octagon (8, 2), counter-clockwise, about c, `across` m across its flats."""
    h = across / 2.0
    t = TAN_8 * h
    return np.asarray(c, np.float64) + np.array([(h, -t), (h, t), (t, h), (-t, h), (-h, t), (-h, -t), (-t, -h), (t, -h)])


def tower_parts(ring, h):
    """The parts of a control tower on footprint ring (k, 2) (open, counter-clockwise), h m tall: [(ring, (own height,
    bottom), own attributes)], buildings' part form. A concrete shaft (the footprint shrunk to SHAFT_SHARE about its
    centroid) to h - CAB_FROM_TOP_M, a glass cab (the footprint itself, or an octagon where it is narrower than
    CAB_SMALL_M) to h - CAP_FROM_TOP_M, a dark flat cap on it (CAP_SHARE of the cab). The attribute "_tower" (shaft /
    cab / cap) is buildings.plan's: no mast, no windows on the concrete."""
    h = max(float(h), TOWER_MIN_H_M)
    c = ring.mean(0)
    area, per = _area(ring), _perimeter(ring)
    short = _short_side(area, per)
    k = SHAFT_SHARE if short * SHAFT_SHARE >= SHAFT_MIN_M else min(1.0, SHAFT_MIN_M / max(short, 1e-6))
    shaft = c + (ring - c) * k
    if short < CAB_SMALL_M:
        cab = octagon(c, max(CAB_SMALL_M, CAB_OCTAGON_K * math.sqrt(area)))
    else:
        cab = ring
    cap = c + (cab - c) * CAP_SHARE
    z1, z2 = h - CAB_FROM_TOP_M, h - CAP_FROM_TOP_M
    return [(shaft, (z1, 0.0), {"facade_material": SHAFT, "facade_color": SHAFT_COLOUR, "roof_shape": "flat",
                                   "_tower": "shaft"}),
            (cab, (z2 - z1, z1), {"facade_material": GLASS, "facade_color": CAB_COLOUR, "roof_shape": "flat",
                                  "roof_color": CAB_ROOF, "_tower": "cab"}),
            (cap, (h - z2, z2), {"facade_material": SHAFT, "facade_color": CAP_COLOUR, "roof_shape": "flat",
                                 "roof_color": CAP_COLOUR, "_tower": "cap"})]


def _outline_samples(ring, step):
    """Points along the closed outline ring (k, 2), at most `step` m apart."""
    a, b = ring, np.roll(ring, -1, axis=0)
    seg = dnp.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1])
    n = np.maximum(np.ceil(seg / step), 1.0).astype(np.int64)
    return np.concatenate([a[i] + (b[i] - a[i]) * (np.arange(n[i]) / float(n[i]))[:, None] for i in range(len(a))])


def pillars(ring):
    """The pillars [(ring (4, 2), counter-clockwise)] under a raised part on footprint ring (k, 2): squares of PILLAR_M, one
    every PILLAR_SPACING_M or less along the part's long axis (the first and last PILLAR_INSET_M in from its ends), in
    a row near each long edge where the part is wider than PILLAR_TWIN_M, else down its middle; only where inside the
    footprint, at most PILLAR_MAX. The long axis: the longest edge's direction, or across it where the part is longer
    that way. Pure."""
    ring = np.asarray(ring, np.float64)[:, :2]
    if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    if len(ring) < 3:
        return []
    d = np.roll(ring, -1, axis=0) - ring
    k = int(np.argmax(dnp.hypot(d[:, 0], d[:, 1])))
    u = d[k] / max(float(dnp.hypot(d[k, 0], d[k, 1])), 1e-9)
    v = np.array([-u[1], u[0]])
    a, b = dnp.matmul(ring, u), dnp.matmul(ring, v)
    if float(b.max() - b.min()) > float(a.max() - a.min()):
        u, v = v, -u                            # (still a right-handed pair: v is u turned a quarter counter-clockwise)
        a, b = dnp.matmul(ring, u), dnp.matmul(ring, v)
    a0, a1, b0, b1 = float(a.min()), float(a.max()), float(b.min()), float(b.max())
    length, width = a1 - a0, b1 - b0
    if length > 2.0 * PILLAR_INSET_M + PILLAR_M:
        along = np.linspace(a0 + PILLAR_INSET_M, a1 - PILLAR_INSET_M, max(1, math.ceil(length / PILLAR_SPACING_M)) + 1)
    else:
        along = np.array([(a0 + a1) / 2.0])
    if width > PILLAR_TWIN_M:
        across = np.array([b0 + PILLAR_INSET_M, b1 - PILLAR_INSET_M])
    else:
        across = np.array([(b0 + b1) / 2.0])
    pts = np.array([x * u + y * v for x in along for y in across])
    pts = pts[np.asarray(polygon.contains_points(ring, pts), bool)]
    if not len(pts):
        c = ring.mean(0)
        pts = c[None] if bool(polygon.contains_points(ring, c[None])[0]) else pts
    if len(pts) > PILLAR_MAX:
        pts = pts[np.linspace(0, len(pts) - 1, PILLAR_MAX).round().astype(np.int64)]
    h = PILLAR_M / 2.0
    return [c + np.array([-h * u - h * v, h * u - h * v, h * u + h * v, -h * u + h * v]) for c in pts]


class Context:
    """An airport's gates and pads round a tile, in metres relative to (x0, y0), made when asked for."""

    def __init__(self, frame, layers, x0, y0):
        self.frame, self.layers, self.x0, self.y0 = frame, layers, x0, y0
        self.intl = any(_scan(f)[1] for f in layers)
        self._gates = self._pads = self._pad_rings = self._reach = None

    def gates(self):
        """The airport_gate points (m, 2)."""
        if self._gates is None:
            ll = [np.asarray(f.parts(int(i))[0][0], np.float64)[0] for f in self.layers for i in _scan(f)[2]]
            if ll:
                ll = np.asarray(ll)
                e, n, _ = self.frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
                self._gates = np.stack([np.asarray(e) - self.x0, np.asarray(n) - self.y0], 1)
            else:
                self._gates = np.empty((0, 2))
        return self._gates

    def pads(self):
        """The vertices of the aprons and taxiways (aeroways.pads) (m, 2)."""
        if self._pads is None:
            pads = aeroways.pads(self.layers)
            if pads:
                la = np.concatenate([np.asarray(p[0], np.float64) for p in pads])
                lo = np.concatenate([np.asarray(p[1], np.float64) for p in pads])
                e, n, _ = self.frame.geodetic_to_enu(la, lo, 0.0)
                self._pads = np.stack([np.asarray(e) - self.x0, np.asarray(n) - self.y0], 1)
            else:
                self._pads = np.empty((0, 2))
        return self._pads

    def pad_rings(self):
        """(the aprons' and taxiways' outlines [(k, 2)], their boxes' low and high corners (n, 2) each): aeroways.pads
        without the runways' own strips, in metres relative to the tile."""
        if self._pad_rings is None:
            rings = []
            for la, lo in aeroways.pads(self.layers, runways=False):
                if len(la) >= 3:
                    e, n, _ = self.frame.geodetic_to_enu(np.asarray(la, np.float64), np.asarray(lo, np.float64), 0.0)
                    rings.append(np.stack([np.asarray(e) - self.x0, np.asarray(n) - self.y0], 1))
            self._pad_rings = (rings, np.array([r.min(0) for r in rings]).reshape(-1, 2),
                               np.array([r.max(0) for r in rings]).reshape(-1, 2))
        return self._pad_rings

    def _airside_reach(self):
        """The box (low corner, high corner) round every pad and gate that a building could be airside in, None if none."""
        if self._reach is None:
            _, lo, hi = self.pad_rings()
            g = self.gates()
            los = ([lo.min(0) - AIRSIDE_PAD_M] if len(lo) else []) + ([g.min(0) - AIRSIDE_GATE_M] if len(g) else [])
            his = ([hi.max(0) + AIRSIDE_PAD_M] if len(hi) else []) + ([g.max(0) + AIRSIDE_GATE_M] if len(g) else [])
            self._reach = (np.min(los, axis=0), np.max(his, axis=0)) if los else False
        return self._reach or None

    def airside(self, ring, at):
        """Is the footprint ring (k, 2; relative to the tile) with attributes `at` a building or a part on the airside:
        within AIRSIDE_PAD_M of an apron or taxiway pad (on one too), or within AIRSIDE_GATE_M of a gate point; not a
        control tower (or its parts), a hangar or a parking structure. Any size: a pier, a satellite, a walkway."""
        name, cls = str(at.get("name") or "").lower(), str(at.get("class") or "").lower()
        if cls == TOWER_CLASS or at.get("_tower") or at.get("_pillar") or cls in NOT_TERMINAL_CLASS \
                or any(w in name for w in NOT_TERMINAL_NAME) or TOWER_NAME.search(name):
            return False
        reach = self._airside_reach()
        ring = np.asarray(ring, np.float64)[:, :2]
        if reach is None or len(ring) < 3:
            return False
        lo, hi = ring.min(0), ring.max(0)
        if (hi < reach[0]).any() or (lo > reach[1]).any():
            return False
        g = self.gates()
        if len(g):
            g = g[((g >= lo - AIRSIDE_GATE_M) & (g <= hi + AIRSIDE_GATE_M)).all(axis=1)]
            if len(g) and (bool(polygon.contains_points(ring, g).any())
                           or float(_outline_dist(g, ring).min()) <= AIRSIDE_GATE_M):
                return True
        rings, plo, phi = self.pad_rings()
        near = np.flatnonzero(((phi >= lo - AIRSIDE_PAD_M) & (plo <= hi + AIRSIDE_PAD_M)).all(axis=1)) if len(rings) else ()
        if not len(near):
            return False
        samples = _outline_samples(ring, SAMPLE_M)
        for i in near:
            r = rings[int(i)]
            # (near its outline, over it or inside it, or a pad vertex inside the footprint: a pad inside it)
            if float(_outline_dist(samples, r).min()) <= AIRSIDE_PAD_M or bool(polygon.contains_points(r, samples).any()) \
                    or bool(polygon.contains_points(ring, r).any()):
                return True
        return False

    def near_pad(self, c, within=TOWER_PAD_M):
        p = self.pads()
        return bool(len(p)) and float(dnp.hypot(p[:, 0] - c[0], p[:, 1] - c[1]).min()) <= within

    def tower_height(self, at):
        """The height a tower with attributes `at` is drawn at: its own, its floors', else by the airport's size."""
        h, nf = at.get("height"), at.get("num_floors")
        if h is not None and np.isfinite(h) and h > 0:
            return float(h)
        if nf is not None and np.isfinite(nf) and nf > 0:
            return float(nf) * TOWER_FLOOR_M
        return TOWER_H_INTL_M if self.intl else TOWER_H_M

    def tower_parts(self, ring, at):
        """tower_parts(ring, height) when the footprint ring (k, 2; relative to the tile) with attributes `at` is a control
        tower, else None. A named one (control tower, ATC, air traffic; "tower" with an airport word in the name), or a
        slender, compact, tall one (TOWER_*) within TOWER_PAD_M of a pad. Not one off the ground (a raised part)."""
        name, cls = str(at.get("name") or "").lower(), str(at.get("class") or "").lower()
        mh = at.get("min_height")
        if cls in NOT_TOWER_CLASS or (mh is not None and np.isfinite(mh) and mh > 0.5):
            return None
        named = bool(TOWER_NAME.search(name)) or (bool(TOWER_WORD.search(name)) and bool(AIRPORT_WORD.search(name))
                                                  and not any(w in name for w in NOT_TOWER_NAME))
        h, nf = at.get("height"), at.get("num_floors")
        tall = (h is not None and np.isfinite(h) and h >= TOWER_MIN_HEIGHT_M) or \
               (nf is not None and np.isfinite(nf) and nf >= TOWER_MIN_FLOORS)
        if not (named or tall):         # (the cheap tests first: most buildings stop here)
            return None
        ring = np.asarray(ring, np.float64)[:, :2]
        area = _area(ring)
        if named:
            if not TOWER_MIN_M2 <= area <= TOWER_NAME_MAX_M2:
                return None
        else:
            if not TOWER_MIN_M2 <= area <= TOWER_SLENDER_M2 or any(w in name for w in NOT_TOWER_NAME):
                return None
            per = _perimeter(ring)
            if 4.0 * math.pi * area / max(per * per, 1e-9) < TOWER_COMPACT or not self.near_pad(ring.mean(0)):
                return None
        return tower_parts(ring, self.tower_height(at))
