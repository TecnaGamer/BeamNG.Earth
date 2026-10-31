"""ships: a moored ship (a museum ship, a hulk) drawn as a hull, not a building.

OSM maps museum ships as buildings (building=ship, historic=ship); Overture keeps the outline and its name but no
"ship" tag, so they came out as blocky prisms (USS Iowa, San Pedro: a 9-storey block; RMS Queen Mary, Long Beach).

Detection (is_ship) is from the outline and the tags only: an OSM footprint (never a machine-learned one) 25 m+ long,
3-10x as long as wide, filling half to 0.92 of its oriented box, with one end pointed or rounded, and either a ship's
class ("ship"), a ship's word in its name (USS, SS, battleship ...), an attraction's subtype with no building class, or
its centre on mapped water. Overture drops OSM's building=ship, historic=ship and ship:type: what is left of them is
subtype "entertainment" with no class, in any language (Mikasa, on land, named in Japanese; Cutty Sark, in a dry dock).
The water test is asked last and only for an outline that passed every shape test (a handful per city), so tiles do
not get slower. A pointed outline on land with none of these stays a building.

Drawing (draw): a hull lofted from STATIONS cross-sections of the footprint's own plan (its width at each station;
the bow is the narrower end): the deck rises toward the bow (sheer), the hull narrows to the waterline and keel, the
stem is raked back and the stern cut up. On it a class profile (PROFILES, keyed on the ship class or type): turrets,
superstructure, funnels, masts, a carrier's island; else a generic warship or liner. Mapped building:part outlines on
an unknown ship stand where they are mapped, each at its own mapped height (HMS Belfast: 33 parts down to its guns).

Profiles are measured along the ship from the bow (x 0) to the stern (x 1); heights are metres over the waterline for
a ship of length L (scaled to the footprint's length); widths are shares of the beam. Every number is worked out from
published overall dimensions (US Navy public-domain ship data: NHHC/DANFS fact sheets; the liners' published specs).
No third-party model or drawing is copied. Approximate by design: a ship that reads right from a car.

Blocks: (x0, x1, half width share of the beam, z0, z1, paint); z0 None: from the deck at that station.
Funnels: (x, length m, width m, top z m, paint, black top m). Turrets: (x, diameter m, height m, superfiring lift m,
guns to "bow"/"stern", barrel m). Masts: (x, top z m). Freeboard: (bow, midships, stern) m. Island: (x0, x1, width
share, top z m) on the starboard edge; flight_deck: the footprint is the flight deck.
"""
import math
import re

import numpy as np

MIN_L = 25.0                # m: a hull at least this long ...
SLENDER = (3.0, 10.0)       # ... this many times as long as wide (real hulls 5-9; a pontoon walkway is thinner)
FILL = (0.5, 0.92)          # ... filling this share of its oriented box (pointed or rounded ends)
END_SHARE = 0.65            # ... one end at most this share of the beam wide over its last END_L of length
END_L = 0.06
BOTH_ENDS_SHARE = 0.7       # ... or, tagged a ship, both ends this share (SS Great Britain: 0.65 and 0.69, a ship's bluff
                            # bow and round stern)
CLASSES = ("", "yes", "ship", "museum")     # building classes a ship may carry (Overture: none, mostly)
ATTRACTION = "entertainment"    # Overture's subtype of a museum or attraction (what it keeps of a ship's OSM tags) ...
ATTRACTION_SLENDER = 4.5        # ... only this slender: real ones 5.2-8.4; Bristol's Folk House, an arts centre, 3.2
ATTRACTION_OFF = (0.10, 0.25)   # ... and this straight: its middle line off the long axis, mean and most, of the half beam
                                # (real ones on land at most 0.09, 0.22; a museum wing 0.12-0.44, 0.23-0.91 in 7.4 M footprints)
MUSEUM = re.compile(r"\bmuseum\b(?! ?ship)")     # a building about a ship, beside it ("SS Great Britain Museum")
NAME = re.compile(r"\b(uss|ss|hms|rms|mv|ms|uscgc|battleship|ship|schooner|steamer|steamship|lightship|submarine|"
                  r"frigate|destroyer|cruiser|aircraft carrier|liner|ferry)\b")
NAVY = re.compile(r"\b(uss|hms|uscgc|battleship|submarine|frigate|destroyer|cruiser|carrier)\b")
FREEBOARD = (3.0, 12.0)     # m: an unknown ship's deck over the water (0.6 of the half beam, in this range)
SHEER = (1.35, 1.1)         # an unknown ship's deck at the bow and the stern, of its freeboard amidships
TIER_M = 3.2                # m per deck of an unknown ship's mapped superstructure
PART_BIG_M = 10.0           # m: a mapped part this long (half) is superstructure; smaller ones turrets
PART_LITE_M = 3.0           # m: the far (lite) mesh leaves out mapped parts shorter than this (guns, rails)
ON_HULL_SHARE = 0.5         # a footprint this much on a ship's hull is the ship's (on_hull)
TURRET_M = 3.5
TOP_SHARE = 0.15            # a ship over 60 m: its mast tops this share of its length over the water
STATIONS, STATIONS_LITE = 48, 16
KEEL_SHARE = 0.5            # the hull's width at the keel (under the water), of the waterline's
PIER_DECK_M = 1.5           # m: a quay structure alongside a ship (pier_alongside): a low deck
PIER_TOUCH_M = 1.0          # its outline this near the hull somewhere ...
PIER_GAP_M = 12.0           # ... its centre at most this past the ship's side and its own half width ...
PIER_SLENDER = 3.0          # ... at least this many times as long as wide, parallel within PIER_DEG
PIER_DEG = 15.0
PIER_REACH_M = 200.0        # m: ships whose centres lie this far past a tile are looked for (a liner's half length)

# The Iowa class (BB-61..64): 270.4 m, 33.0 m beam; freeboard ~11 m at the bow to ~6 m aft; three 16-inch turrets
# (two forward, the second superfiring; one aft) ~15 m across, guns ~20 m; tower bridge, two funnels, masts.
IOWA_CLASS = dict(
    L=270.4, freeboard=(11.0, 8.0, 6.0), waterline=0.94, rake=0.03, navy=True,
    blocks=[(0.335, 0.665, 0.30, None, 11.5, "upper"), (0.355, 0.47, 0.22, 11.5, 15.0, "upper"),
            (0.37, 0.45, 0.17, 15.0, 21.0, "upper"), (0.38, 0.44, 0.11, 21.0, 27.0, "upper"),
            (0.395, 0.425, 0.07, 27.0, 31.0, "upper"), (0.555, 0.665, 0.19, 11.5, 15.0, "upper"),
            (0.60, 0.65, 0.10, 15.0, 19.5, "upper")],
    funnels=[(0.495, 11.0, 7.0, 29.0, "upper", 0.0), (0.575, 10.0, 6.5, 28.0, "upper", 0.0)],
    turrets=[(0.245, 15.0, 4.0, 0.0, "bow", 20.0), (0.32, 15.0, 4.0, 3.0, "bow", 20.0),
             (0.735, 15.0, 4.0, 0.0, "stern", 20.0)],
    masts=[(0.455, 46.0), (0.615, 40.0)])
# The Queen Mary: 310.7 m, 36.1 m beam; black hull to the main deck (~16 m amidships), white promenade and sun decks,
# three red funnels with black tops (~45 m), bridge front at ~0.23.
QUEEN_MARY = dict(
    L=310.7, freeboard=(21.0, 16.0, 17.0), waterline=0.95, rake=0.035, navy=False,
    blocks=[(0.215, 0.80, 0.47, None, 20.0, "upper"), (0.225, 0.775, 0.43, 20.0, 23.0, "upper"),
            (0.23, 0.70, 0.36, 23.0, 26.0, "upper"), (0.225, 0.26, 0.47, 23.0, 28.5, "upper"),
            (0.235, 0.255, 0.12, 28.5, 31.0, "upper")],
    funnels=[(0.36, 11.0, 7.0, 45.0, "funnel", 4.5), (0.48, 11.0, 7.0, 45.0, "funnel", 4.5),
             (0.60, 10.5, 7.0, 43.0, "funnel", 4.5)],
    turrets=[], masts=[(0.13, 48.0), (0.86, 44.0)])
# Essex-class carriers: ~266 m, the footprint the ~45 m flight deck ~17 m up over a 28 m hull; island starboard.
ESSEX_CLASS = dict(
    L=266.0, freeboard=(17.0, 17.0, 17.0), waterline=0.62, rake=0.03, flight_deck=True, navy=True,
    blocks=[], funnels=[], turrets=[], island=(0.36, 0.50, 0.11, 33.0), masts=[(0.43, 45.0)])
# The Midway class: 296 m, 41 m hull under a 72 m angled flight deck ~18 m up.
MIDWAY_CLASS = dict(ESSEX_CLASS, L=296.0, freeboard=(18.0, 18.0, 18.0), waterline=0.57,
                    island=(0.34, 0.46, 0.07, 36.0), masts=[(0.40, 50.0)])
# Ships of no known class: a warship (deckhouse, bridge tower, one funnel, two masts) or a merchant ship / liner.
GENERIC_WARSHIP = dict(
    L=None, waterline=0.92, rake=0.03, navy=True, relative=True,
    blocks=[(0.33, 0.66, 0.30, None, 3.0, "upper"), (0.36, 0.48, 0.22, 3.0, 6.0, "upper"),
            (0.38, 0.45, 0.15, 6.0, 9.5, "upper")],
    funnels=[(0.53, 0.04, 0.025, 11.0, "upper", 0.0)], turrets=[], masts=[(0.43, None), (0.66, None)])
GENERIC_LINER = dict(
    L=None, waterline=0.94, rake=0.035, navy=False, relative=True,
    blocks=[(0.22, 0.78, 0.45, None, 3.0, "upper"), (0.25, 0.74, 0.40, 3.0, 6.0, "upper"),
            (0.27, 0.66, 0.33, 6.0, 9.0, "upper"), (0.26, 0.30, 0.45, 6.0, 10.5, "upper")],
    funnels=[(0.45, 0.04, 0.025, 15.0, "funnel", 0.12)], turrets=[], masts=[(0.14, None), (0.84, None)])

# Class profiles keyed on the ship class (an OSM ship:class / name carrying the class's hull numbers or its members'
# names with a navy tell): (pattern, profile, tells: one of these in the name too, or None).
PROFILES = [
    (re.compile(r"\biowa[- ]class\b|\bbb-6[1-4]\b|\b(iowa|new jersey|missouri|wisconsin)\b"), IOWA_CLASS,
     ("uss", "battleship", "bb-6", "iowa-class", "iowa class")),
    (re.compile(r"\bqueen mary\b"), QUEEN_MARY, None),
    (re.compile(r"\bmidway[- ]class\b|\bcv-41\b|\bmidway\b"), MIDWAY_CLASS, ("uss", "carrier", "cv-41", "class")),
    (re.compile(r"\bessex[- ]class\b|\bcvs?-1[0126]\b|\b(yorktown|intrepid|hornet|lexington)\b"), ESSEX_CLASS,
     ("uss", "carrier", "cv-1", "cvs-1", "class")),
]


def profile(name, ship_class=None):
    """The profile for a ship by its class tag or its name; else a generic warship (a navy word in the name) or
    liner. A class matched by a member's name needs a tell too (an "Iowa" building is not the battleship)."""
    sc, nm = str(ship_class or "").lower(), str(name or "").lower()
    for rx, prof, tells in PROFILES:
        if (sc and rx.search(sc)) or (rx.search(nm) and (tells is None or any(t in nm for t in tells))):
            return prof
    return GENERIC_WARSHIP if NAVY.search(str(name or "").lower()) else GENERIC_LINER


def _B():
    import buildings
    return buildings


def end_widths(r, c, u, hl):
    """The outline's width across u over its last END_L of length at each end (sampled along its edges)."""
    v = np.array([-u[1], u[0]])
    t = np.linspace(0, 1, 6)[:, None, None]
    pts = (r[None] + (np.roll(r, -1, 0) - r)[None] * t).reshape(-1, 2) - c
    s, d = pts @ u, pts @ v
    out = []
    for sgn in (-1, 1):
        k = (sgn * s) >= hl * (1 - 2 * END_L)
        out.append(float(d[k].max() - d[k].min()) if k.any() else 0.0)
    return out


# The water's level at points (lat, lon arrays -> (depth, level): seacarve.SeaCarver.water), set once per process by
# whoever holds the carver (blockworker). A ship floats on the water drawn, not on the ground under it (the user at
# Hamburg, 2026-10-31: "They don't always sit at water level"; the highest ground under a hull is a bank or a carved bed).
water_at = None
RIVER_ABOVE_M = 0.25        # m: no carved level here (an uncarved river): its surface over the channel (riverlines.py)


def waterline(lat, lon, ground):
    """The height above the ellipsoid of the water a ship's outline (corners lat, lon; the ground at them) floats on:
    the median level of the corners on water, else just over the lowest ground."""
    if water_at is not None:
        try:
            lv = np.asarray(water_at(np.asarray(lat, np.float64), np.asarray(lon, np.float64))[1], np.float64)
        except Exception:       # (the map data of the water not to be had: as without a carver)
            lv = np.empty(0)
        lv = lv[np.isfinite(lv)]
        if lv.size:
            return float(np.median(lv))
    return float(np.min(ground)) + RIVER_ABOVE_M


def on_water(c, water_xy):
    """Whether point c (local) lies on mapped water (buildings.water_rings), not on an island in it."""
    if not water_xy:
        return False
    from polygon import Path
    for w, holes in water_xy:
        if w[:, 0].min() <= c[0] <= w[:, 0].max() and w[:, 1].min() <= c[1] <= w[:, 1].max() \
                and Path(w).contains_points(c[None])[0] \
                and not any(Path(h).contains_points(c[None])[0] for h in holes):
            return True
    return False


def is_ship(ring, attrs, water=False):
    """Whether a footprint (local ring, counter-clockwise, not closed) is a moored ship. water: a bool or a callable
    asked only when every other test passed (the on-water test is the slow one)."""
    if attrs.get("ml"):
        return False
    cls = str(attrs.get("class") or "").lower()
    if cls not in CLASSES:
        return False
    if max(np.ptp(ring[:, 0]), np.ptp(ring[:, 1])) < MIN_L / math.sqrt(2):
        return False                            # (most outlines: cheaper than the oriented box)
    B = _B()
    c, u, hl, hw = B.oriented_box(ring)
    if 2 * hl < MIN_L or not (SLENDER[0] * hw <= hl <= SLENDER[1] * hw):
        return False
    if not FILL[0] <= (B._area2(ring) / 2) / max(4 * hl * hw, 1e-6) <= FILL[1]:
        return False
    ends = end_widths(ring, c, u, hl)
    tagged = has_evidence(attrs)
    if min(ends) > END_SHARE * 2 * hw and not (tagged and max(ends) <= BOTH_ENDS_SHARE * 2 * hw):
        return False
    if tagged and (_named(attrs) or (hl >= ATTRACTION_SLENDER * hw and _straight(ring, c, u, hl, hw))):
        return True
    return bool(water() if callable(water) else water)


def _straight(ring, c, u, hl, hw):
    """Whether an outline's middle line keeps to its long axis, as a hull's does (a wing or an L does not)."""
    off = np.abs(_widths(ring, c, u, hl, np.linspace(0.1, 0.9, 17))[:, 0]) / max(hw, 1e-6)
    return float(off.mean()) <= ATTRACTION_OFF[0] and float(off.max()) <= ATTRACTION_OFF[1]


def has_evidence(attrs):
    """Whether a footprint's tags say ship (its outline and its water aside)."""
    if MUSEUM.search(str(attrs.get("name") or "").lower()) and str(attrs.get("class") or "").lower() != "ship":
        return False
    cls = str(attrs.get("class") or "").lower()
    return _named(attrs) or cls == "museum" or (not cls and str(attrs.get("subtype") or "") == ATTRACTION)


def _named(attrs):
    """Its class or name says ship (not merely an attraction's subtype)."""
    return str(attrs.get("class") or "").lower() == "ship" or bool(NAME.search(str(attrs.get("name") or "").lower()))


def on_hull(ring, hull):
    """Whether a footprint lies mostly on a ship's hull (local rings): a second outline of the ship (machine-learned, a
    second OSM way) or something standing on it, never a building as well. A canopy the ship stands in is not."""
    if ring[:, 0].max() < hull[:, 0].min() or ring[:, 0].min() > hull[:, 0].max() \
            or ring[:, 1].max() < hull[:, 1].min() or ring[:, 1].min() > hull[:, 1].max():
        return False
    from polygon import Path
    gx, gy = np.meshgrid(np.linspace(ring[:, 0].min(), ring[:, 0].max(), 12),
                         np.linspace(ring[:, 1].min(), ring[:, 1].max(), 12))
    pts = np.stack([gx.ravel(), gy.ravel()], 1)
    pts = pts[Path(ring).contains_points(pts)]
    return len(pts) > 0 and float(Path(hull).contains_points(pts).mean()) >= ON_HULL_SHARE


def _ring_points(r, step=2.0):
    out = []
    for a, b in zip(r, np.roll(r, -1, 0)):
        k = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
        out.append(a + (b - a) * (np.arange(k)[:, None] / k))
    return np.concatenate(out)


def pier_shaped(ring):
    """Whether a footprint is long and thin enough to be a quay structure (pier_alongside's first test)."""
    _, _, hl, hw = _B().oriented_box(ring)
    return hl >= PIER_SLENDER * hw


def pier_alongside(ring, hull):
    """Whether a footprint is a quay structure along a ship's side (local rings): long and thin, parallel to the ship,
    touching its hull, lying beside it. Its scanned height is the ship's flank (the Queen Mary's landing stage,
    building=yes at 22.7 m from LiDAR, stood as a 7-storey block): it is drawn as a low deck instead."""
    B = _B()
    c, u, hl, hw = B.oriented_box(ring)
    hc, hu, hhl, hhw = B.oriented_box(hull)
    if hl < PIER_SLENDER * hw or abs(float(u @ hu)) < math.cos(math.radians(PIER_DEG)):
        return False
    hv = np.array([-hu[1], hu[0]])
    along = (ring - hc) @ hu
    if along.min() < -hhl - PIER_GAP_M or along.max() > hhl + PIER_GAP_M \
            or abs(float((c - hc) @ hv)) > hhw + hw + PIER_GAP_M:
        return False
    pts = _ring_points(ring)
    a, ab = hull, np.roll(hull, -1, 0) - hull
    t = np.clip(((pts[:, None] - a[None]) * ab[None]).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-9)[None], 0, 1)
    d = np.hypot(*(pts[:, None] - (a[None] + ab[None] * t[..., None])).transpose(2, 0, 1)).min()
    return float(d) <= PIER_TOUCH_M


def spec(ring, attrs, seed):
    """buildings.plan's Spec for a ship (cls "ship"; floor_h the freeboard amidships, height the top of its masts)."""
    B = _B()
    _, _, hl, hw = B.oriented_box(ring)
    prof = profile(attrs.get("name"), attrs.get("ship_class"))
    if prof.get("L"):
        k = 2 * hl / prof["L"]
        fb = prof["freeboard"][1] * k
        h = k * max([z for _, z in prof["masts"]] + [f[3] for f in prof["funnels"]]
                    + ([prof["island"][3]] if prof.get("island") else []))
    else:
        fb = float(np.clip(0.6 * hw, *FREEBOARD))
        h = attrs.get("height")
        h = float(h) if h is not None and np.isfinite(h) and h > 0 else 0.0
        h = max(h, fb + 3 * TIER_M, TOP_SHARE * 2 * hl if 2 * hl >= 60 else 0.0)
    hull, upper = (f"{B.STEEL}|grey", f"{B.STEEL}|grey") if prof["navy"] else (f"{B.STEEL}|black", f"{B.STEEL}|white")
    return B.Spec(ring=ring, floors=1, floor_h=fb, height=h, roof="flat", roof_mat=f"{B.ROOF_METAL}|1", facade=hull,
                  wall=upper, band=None, shop=False, plain=True, seed=seed, min_h=0.0, windows=False, steeple=False,
                  porch=False, cls="ship", deck=False, pitch=None,
                  ship={"profile": prof, "parts": attrs.get("ship_parts") or []})


def pier_spec(ring, seed):
    """A quay structure alongside a ship (pier_alongside): a low concrete deck."""
    B = _B()
    return B.Spec(ring=ring, floors=1, floor_h=PIER_DECK_M, height=PIER_DECK_M, roof="flat", roof_mat="bne_precast",
                  facade="bne_precast", band=None, shop=False, plain=True, seed=seed, min_h=0.0, wall=None,
                  windows=False, steeple=False, porch=False, cls="pier", deck=False, pitch=None)


_UV0 = ((0.002, 0.995), (0.012, 0.995), (0.012, 0.999), (0.002, 0.999))


def _quad_out(m, a, b, c, d, hint, mat):
    """A quad facing hint (its corners reordered when they face away)."""
    n = np.cross(np.subtract(b, a), np.subtract(c, a))
    if float(np.dot(n, hint)) < 0:
        a, b, c, d = a, d, c, b
    m.quad(a, b, c, d, *_UV0, mat)


def _cylinder(m, c, u, rx, ry, z0, z1, mat, segs=12, top=True):
    """An upright elliptic cylinder (rx along u, ry across) from z0 to z1."""
    v = np.array([-u[1], u[0]])
    t = np.linspace(0, 2 * math.pi, segs, endpoint=False)
    r = np.array([c + u * rx * math.cos(a) + v * ry * math.sin(a) for a in t])
    if _B()._area2(r) < 0:
        r = r[::-1]
    for i in range(len(r)):
        a, b = r[i], r[(i + 1) % len(r)]
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), *_UV0, mat)
    if top:
        m.poly([(p[0], p[1], z1) for p in r], [_UV0[0]] * len(r), mat)


def _extrude(m, r, z0, z1, mat):
    """A mapped part: its own outline (counter-clockwise) upright from z0 to z1, with a flat top."""
    if z1 <= z0 + 0.1:
        return
    for a, b in zip(r, np.roll(r, -1, 0)):
        m.quad((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1), *_UV0, mat)
    m.poly([(p[0], p[1], z1) for p in r], [_UV0[0]] * len(r), mat)


def axis(r):
    """(centre, unit vector to the bow, half length, half beam): the bow the narrower end."""
    c, u, hl, hw = _B().oriented_box(r)
    ew = end_widths(r, c, u, hl)
    return c, (-u if ew[0] < ew[1] else u), hl, hw


def _widths(r, c, ub, hl, xs):
    """(centre offset, half width) across the ship at stations xs (0 the bow, 1 the stern)."""
    v = np.array([-ub[1], ub[0]])
    a, b = r - c, np.roll(r, -1, 0) - c
    sa, sb, ta, tb = a @ ub, b @ ub, a @ v, b @ v
    out = []
    for x in xs:
        sx = hl * (1 - 2 * float(np.clip(x, 0.003, 0.997)))
        k = ((sa - sx) * (sb - sx) <= 0) & (sa != sb)
        if not k.any():
            out.append((0.0, 0.0))
            continue
        t = ta[k] + (tb[k] - ta[k]) * (sx - sa[k]) / (sb[k] - sa[k])
        out.append(((t.max() + t.min()) / 2, (t.max() - t.min()) / 2))
    return np.array(out)


def hull_sections(spec, zw, n):
    """(stations xs, centre offsets, deck half widths, waterline half widths, deck z per station) of a ship."""
    prof = spec.ship["profile"]
    r = np.asarray(spec.ring, np.float64)
    c, ub, hl, hw = axis(r)
    xs = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n + 1))       # (closer at the ends)
    tw = _widths(r, c, ub, hl, xs)
    rake, wl = prof.get("rake", 0.03), prof.get("waterline", 0.93)
    xq = xs - rake * np.clip(1 - xs / 0.15, 0, 1) + 0.012 * np.clip(1 - (1 - xs) / 0.1, 0, 1)
    w = wl * np.interp(xq, xs, tw[:, 1], left=0.0, right=0.0)
    fb = _freeboard(spec, hl)
    zd = np.array([_deck_z(x, fb, zw) for x in xs])
    return xs, tw[:, 0], tw[:, 1], w, zd


def _freeboard(spec, hl):
    prof = spec.ship["profile"]
    if prof.get("L"):
        return [f * 2 * hl / prof["L"] for f in prof["freeboard"]]
    return [spec.floor_h * SHEER[0], spec.floor_h, spec.floor_h * SHEER[1]]


def _deck_z(x, fb, zw):
    x = float(np.clip(x, 0.0, 1.0))
    if x < 0.5:
        return zw + fb[1] + (fb[0] - fb[1]) * ((0.5 - x) / 0.5) ** 2
    return zw + fb[1] + (fb[2] - fb[1]) * ((x - 0.5) / 0.5) ** 2


def draw(m, spec, gmin, gmax, mat_idx, lite=False):
    """The ship's mesh into m: the lofted hull and deck, then its profile's (or its mapped parts') upperworks."""
    B = _B()
    r = np.asarray(spec.ring, np.float64)
    c, ub, hl, hw = axis(r)
    v = np.array([-ub[1], ub[0]])
    prof = spec.ship["profile"]
    parts = spec.ship.get("parts") or []
    known = bool(prof.get("L"))
    k = 2 * hl / prof["L"] if known else 1.0
    zw = B.base_z(spec, gmin, gmax)
    zk = gmin - (B.SKIRT_LITE_M if lite else B.SKIRT_M)
    hull, upper = mat_idx[spec.facade], mat_idx[spec.wall]
    navy = bool(prof["navy"])
    paints = {"upper": upper, "hull": hull, "deck": mat_idx[spec.roof_mat],
              "funnel": mat_idx[f"{B.STEEL}|grey" if navy else f"{B.STEEL}|red"]}
    black = mat_idx[f"{B.STEEL}|black"]
    fb = _freeboard(spec, hl)

    def zd(x):
        return _deck_z(x, fb, zw)

    def zabs(z):
        return zw + z * k

    n = STATIONS_LITE if lite else STATIONS
    xs, tc, b, w, zdk = hull_sections(spec, zw, n)
    rake = prof.get("rake", 0.03)
    keel = KEEL_SHARE * w

    def P(x, t, z):
        p = c + ub * (hl * (1 - 2 * x)) + v * t
        return (float(p[0]), float(p[1]), float(z))
    xw = np.clip(xs, rake, 1 - 0.012)           # the waterline's and keel's stations (the stem raked, stern cut up)
    rows = [(keel, zk, xw), (w, zw, xw), (b, None, xs)]
    for i in range(n):
        for sgn in (1, -1):
            hint = np.array([*(v * sgn), 0.0])
            for (w0, z0, X0), (w1, z1, X1) in zip(rows[:-1], rows[1:]):
                za0 = z0 if z0 is not None else zdk[i]
                za1 = z0 if z0 is not None else zdk[i + 1]
                zb0 = z1 if z1 is not None else zdk[i]
                zb1 = z1 if z1 is not None else zdk[i + 1]
                _quad_out(m, P(X0[i], tc[i] + sgn * w0[i], za0), P(X0[i + 1], tc[i + 1] + sgn * w0[i + 1], za1),
                          P(X1[i + 1], tc[i + 1] + sgn * w1[i + 1], zb1), P(X1[i], tc[i] + sgn * w1[i], zb0), hint, hull)
        _quad_out(m, P(xs[i], tc[i] - b[i], zdk[i]), P(xs[i + 1], tc[i + 1] - b[i + 1], zdk[i + 1]),
                  P(xs[i + 1], tc[i + 1] + b[i + 1], zdk[i + 1]), P(xs[i], tc[i] + b[i], zdk[i]), (0, 0, 1),
                  paints["deck"])
    for i, sg in ((0, 1), (n, -1)):             # a flat stem or transom where the plan ends wide
        if b[i] > 0.3:
            hint = np.array([*(ub * sg), 0.0])
            for (w0, z0, X0), (w1, z1, X1) in zip(rows[:-1], rows[1:]):
                za = z0 if z0 is not None else zdk[i]
                zb_ = z1 if z1 is not None else zdk[i]
                _quad_out(m, P(X0[i], tc[i] - w0[i], za), P(X0[i], tc[i] + w0[i], za),
                          P(X1[i], tc[i] + w1[i], zb_), P(X1[i], tc[i] - w1[i], zb_), hint, hull)

    def half_b(x):
        return float(np.interp(x, xs, b))

    def at(x, t=0.0):
        return c + ub * (hl * (1 - 2 * x)) + v * (float(np.interp(x, xs, tc)) + t)
    beam = 2 * hw
    top = zw + spec.height
    blocks = [B.oriented_box(np.asarray(pr, np.float64)) for pr, ph in parts if ph is None]
    mapped = [(np.asarray(pr, np.float64), ph) for pr, ph in parts if ph is not None] if not known else []
    if mapped or (not known and any(phl >= PART_BIG_M for _, _, phl, _ in blocks)):
        zdm = zd(0.5)
        for pc, pu, phl, phw in blocks:         # mapped superstructure: tiers to 0.6 of the height; turrets
            if phl >= PART_BIG_M:
                zz, L, W = zdm - 0.3, phl, phw
                while zz + TIER_M <= zdm + 0.6 * (top - zdm) and W > 1.5:
                    B._box(m, pc, pu, L, W, zz, zz + TIER_M, upper)
                    zz, L, W = zz + TIER_M, L * 0.9, W * 0.85
            else:
                B._box(m, pc, pu, phl, phw, zdm - 0.3, zdm + (TURRET_M if phl >= 4.0 else 2.0), upper)
    else:
        for x0, x1, wsh, z0, z1, paint in prof.get("blocks", []):
            xm = (x0 + x1) / 2
            deck = min(zd(x0), zd(x1), zd(xm)) - 0.3
            if prof.get("relative"):
                za, zb_ = (deck if z0 is None else zd(xm) + z0), zd(xm) + z1
            else:
                za, zb_ = (deck if z0 is None else zabs(z0)), zabs(z1)
            hy = min(wsh * beam, 0.98 * min(half_b(x0), half_b(x1)))
            if zb_ > za + 0.5 and hy > 0.5:
                B._box(m, at(xm), ub, (x1 - x0) * hl, hy, za, zb_, paints.get(paint, upper))
    for pr, (ph, pmh) in mapped:                # each mapped part as it is mapped, from the waterline
        pc, _, phl, _ = B.oriented_box(pr)
        if lite and 2 * phl < PART_LITE_M:
            continue
        x = float(np.clip((1 - ((pc - c) @ ub) / hl) / 2, 0, 1))
        _extrude(m, pr, max(zw + pmh, zd(x) - 0.3), zw + pmh + ph, upper)
    if mapped:                                  # the map is the model: no generic funnels or masts as well
        return
    isl = prof.get("island")
    if isl:                                     # a carrier's island on its starboard edge
        x0, x1, wsh, ztop = isl
        xm = (x0 + x1) / 2
        hy = wsh * beam
        B._box(m, at(xm, -(half_b(xm) - hy)), ub, (x1 - x0) * hl, hy, zd(xm) - 0.3, zabs(ztop), upper)
    if lite:
        return
    funnels = []
    for x, flen, fwid, ztop, paint, btop in prof.get("funnels", []):
        if prof.get("relative"):
            flen, fwid, z1, btop = flen * 2 * hl, fwid * 2 * hl, zd(x) + ztop, btop * ztop
        else:
            flen, fwid, z1, btop = flen * k, fwid * k, zabs(ztop), btop * k
        funnels.append((at(x), flen / 2, fwid / 2, zd(x) - 0.3, z1, btop, paints.get(paint, upper)))
    for pc, rx, ry, z0, z1, btop, mat in funnels:
        if btop > 0 and mat is paints["funnel"] and not navy:      # a liner's: red with a black top
            _cylinder(m, pc, ub, rx, ry, z0, z1 - btop, mat, top=False)
            _cylinder(m, pc, ub, rx, ry, z1 - btop, z1, black)
        else:
            _cylinder(m, pc, ub, rx, ry, z0, z1, mat)
    for x, diam, hgt, lift, way, barrel in prof.get("turrets", []):
        diam, hgt, lift, barrel = diam * k, hgt * k, lift * k, barrel * k
        pc, z0 = at(x), zd(x) - 0.3
        if lift > 0:                            # a superfiring turret's barbette
            _cylinder(m, pc, ub, 0.4 * diam, 0.4 * diam, z0, z0 + lift + 0.3, upper)
        zt = z0 + lift + 0.3
        _cylinder(m, pc, ub, diam / 2, 0.45 * diam, zt, zt + hgt, upper)
        sg = 1 if way == "bow" else -1
        for off in (-0.22, 0.0, 0.22):          # its three guns
            B._box(m, pc + ub * sg * (0.3 * diam + barrel / 2) + v * off * diam, ub, barrel / 2, 0.35 * k,
                   zt + 0.45 * hgt, zt + 0.45 * hgt + 0.7 * k, upper)
    mt = max(0.35, 0.012 * beam)
    for x, ztop in prof.get("masts", []):
        z1 = zabs(ztop) if (known and ztop is not None) else top
        z0, pc = zd(x) - 0.3, at(x)
        B._box(m, pc, ub, mt, mt, z0, z1, upper)
        yard = z1 - 0.15 * (z1 - z0)
        B._box(m, pc, ub, mt, min(0.4 * beam, 0.9 * half_b(x) + 2.0), yard, yard + 2 * mt, upper)
