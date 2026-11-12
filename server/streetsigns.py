"""Street-name signs, each country's own (data/street_sign_styles.json; sign tile field "streets")."""
import json
import math
import os
from functools import cached_property, lru_cache

import numpy as np

import mapcells
import signmesh

ENABLED = os.environ.get("BNE_STREETSIGNS", "on").strip().lower() in ("on", "1", "yes", "true")
STYLES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "street_sign_styles.json")
WORLD_STYLE = "US"              # BNE_SIGNS_WORLD=1: the style where a country has none

SIGNAL_GROUP = "signal"
MARGIN_M = 80.0                 # m round the tile for the roads and signals looked at
DRIVE_CLASSES = frozenset(("motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                           "living_street", "service"))
MAX_VERTS = 52000               # signmesh's limit is 60000
SAME_SIGNAL_M = 1.5             # m and degrees: one signal read from two road tiles
SAME_SIGNAL_DEG = 15.0
_CACHE = {}
_CACHE_MAX = 128

# The stock boom, measured from its .dae (art/shapes/objects/s_trafficlight_boom_sn): the pole at the model's origin,
# the arm along local -x, rising from 4.8 m at the pole to 6.07 m 3.7 m out, then level to its tip at 6 m; the heads at
# 5.0-5.4 m out. ARM_PROFILE: (m from the pole, m up) of the arm's centre line
ARM_PROFILE = ((0.0, 4.8), (3.7, 6.07), (4.3, 6.17))
ARM_REACH = 0.8                 # m from the pole to the blade's near end
ARM_FRONT = 0.12                # m in front of the arm (toward the drivers) of the blade's face
CROSS_PROBE = (4.0, 8.0)        # m (left of the pole, ahead along the travel) of the junction's middle
CROSS_MAX_M = 24.0              # m from that point a cross street may be
CROSS_COS = 0.55                # |cos| of the angle between the travel and a cross street at most


# --- styles ---------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _table():
    with open(STYLES_PATH, encoding="utf-8") as f:
        return json.load(f)


def style(country):
    """The street-name sign style of a country (ISO code), or None: no street signs there."""
    return _table()["countries"].get((country or "").upper())


def countries_at(frame, xy):
    """The country code at each point xy ((n, 2) frame metres), or None: "US" where signstates finds a state (bundled,
    works offline), else the border data's (countrybounds; None while it is not downloaded)."""
    import countrybounds
    import signstates
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return []
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    out = ["US" if u else None for u in signstates.states(la, lo) != ""]
    rest = [i for i, c in enumerate(out) if c is None]
    if rest and set(_table()["countries"]) - {"US"}:     # (countrybounds downloads its file: only when it can matter)
        for i, c in zip(rest, countrybounds.bundled().codes_at([(la[i], lo[i]) for i in rest])):
            out[i] = c
    return out


def styles_at(frame, xy, world=False):
    """The style at each point xy, or None (world: WORLD_STYLE where the country has none)."""
    fallback = style(WORLD_STYLE) if world else None
    return [style(c) or fallback for c in countries_at(frame, xy)]


def short_name(name, st):
    """A road's name as the style's signs write it: its language's direction and kind words shortened, in capitals if
    the style says so ("East Fayette Street" -> "E FAYETTE ST" in the US)."""
    rules = _table()["abbrev"].get(st.get("abbrev"), {})
    dirs, first, last = rules.get("dirs", {}), rules.get("first", {}), rules.get("last", {})
    words = str(name).split()
    if not words:
        return ""
    low = [w.lower() for w in words]
    # ("North Street" and "East West Highway" keep their direction word: it is the name)
    if len(words) > 1 and low[0] in dirs and low[1] not in dirs and (len(words) > 2 or low[1] not in last):
        words[0] = dirs[low[0]]
    elif len(words) > 1 and low[0] in first:
        words[0] = first[low[0]]
    trail = []
    if len(words) > 1 and low[-1] in dirs and low[-2] not in dirs:
        trail = [dirs[low[-1]]]
        words.pop()
    if len(words) > 1 and words[-1].lower() in last:
        words[-1] = last[words[-1].lower()]
    text = " ".join(words + trail)
    return text.upper() if st.get("case") == "upper" else text


# --- inputs ---------------------------------------------------------------------------------------------------------
def _signal_rows(path, frame):
    """[(x, y, z, yaw)] of the signals in one road tile JSON, in `frame` (a geotile's rows are in its own frame:
    "frame" = [lat, lon]; the yaw turned with them). None when the file is not written."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size, frame.lat0, frame.lon0)
    if key in _CACHE:
        return _CACHE[key]
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    props = d.get("props") or {}
    groups = [str(g) for g in props.get("groups") or []]
    gi = groups.index(SIGNAL_GROUP) if SIGNAL_GROUP in groups else -1
    rows = np.asarray([r[1:5] for r in props.get("items") or [] if int(r[0]) == gi], np.float64).reshape(-1, 4)
    if len(rows) and d.get("frame"):
        import geodesy
        src = geodesy.EnuFrame(float(d["frame"][0]), float(d["frame"][1]))
        yaw = np.radians(rows[:, 3])
        ahead = rows[:, :2] + 5.0 * np.column_stack([np.cos(yaw), np.sin(yaw)])
        la, lo, h = src.enu_to_geodetic(np.r_[rows[:, 0], ahead[:, 0]], np.r_[rows[:, 1], ahead[:, 1]],
                                        np.r_[rows[:, 2], rows[:, 2]])
        x, y, z = frame.geodetic_to_enu(la, lo, h)
        n = len(rows)
        yaws = np.degrees(np.arctan2(y[n:] - y[:n], x[n:] - x[:n])) % 360.0
        rows = np.column_stack([x[:n], y[:n], z[:n], yaws])
    out = [tuple(float(v) for v in r) for r in rows]
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def signals_near(out_dir, aid, frame, tx, ty, box):
    """The signals of the road tiles round (tx, ty) (the anchor's roads_<aid>_<tx>_<ty>.json and the session's road
    geotiles rg_<key>.json), in frame metres, each once."""
    import geotiles
    got = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            rows = _signal_rows(os.path.join(out_dir, f"roads_{aid}_{tx + dx}_{ty + dy}.json"), frame)
            got.extend(rows or ())
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - MARGIN_M, x1 + MARGIN_M]),
                                      np.array([y0 - MARGIN_M, y1 + MARGIN_M]), 0.0)
    gx0, gy1 = geotiles.tile_of(float(np.min(la)), float(np.min(lo)))
    gx1, gy0 = geotiles.tile_of(float(np.max(la)), float(np.max(lo)))
    for gx in range(min(gx0, gx1), max(gx0, gx1) + 1):
        for gy in range(min(gy0, gy1), max(gy0, gy1) + 1):
            rows = _signal_rows(os.path.join(out_dir, f"rg_{geotiles.key(gx, gy)}.json"), frame)
            got.extend(rows or ())
    out = []
    for r in got:
        if not any(math.hypot(r[0] - o[0], r[1] - o[1]) < SAME_SIGNAL_M
                   and abs((r[3] - o[3] + 180.0) % 360.0 - 180.0) < SAME_SIGNAL_DEG for o in out):
            out.append(r)
    return out


def _cells(frame, box):
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), 0.0)
    return mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))


class Roads:
    """The drivable roads at ground level round a tile as segments in frame metres: A, B (m, 2), T their unit
    directions, idx their road's index into names, named whether that road has a name. box (x0, y0, x1, y1): only the
    roads whose bounding box meets it (a long road's first point may lie far off)."""

    def __init__(self, frame, cells_feats, box=None):
        import roads as roadsmod
        a, b, idx, names, seen = [], [], [], [], set()
        for feats in cells_feats:
            if feats is None or not len(feats):
                continue
            cls = feats.attrs.get("class")
            nm = feats.attrs.get("name")
            xy = feats.xy_in(frame)
            near = range(len(feats))
            if box is not None:
                bx = feats.boxes(frame)
                near = np.flatnonzero((bx[:, 0] <= box[2]) & (bx[:, 2] >= box[0])
                                      & (bx[:, 1] <= box[3]) & (bx[:, 3] >= box[1]))
            for i in near:
                fid = str(feats.ids[i])
                if fid in seen or (cls is not None and str(cls[i]) not in DRIVE_CLASSES):
                    continue
                seen.add(fid)
                parts = feats.parts(i, xy)
                if not parts or not parts[0] or len(parts[0][0]) < 2:
                    continue
                line = np.asarray(parts[0][0], np.float64)[:, :2]
                pa, pb = line[:-1], line[1:]
                # (no bridge or tunnel stretch: the freeway over a ramp's signals is no cross street)
                off = [(s0, s1) for s0, s1, _ in roadsmod.tag_level_ranges(feats, i)]
                if off:
                    cum = np.r_[0.0, np.cumsum(np.hypot(*(pb - pa).T))]
                    mid = (cum[:-1] + cum[1:]) / 2.0 / max(cum[-1], 1e-9)
                    keep = ~np.any([(mid >= s0) & (mid <= s1) for s0, s1 in off], axis=0)
                    pa, pb = pa[keep], pb[keep]
                if not len(pa):
                    continue
                idx.append(np.full(len(pa), len(names)))
                names.append(str(nm[i] or "") if nm is not None else "")
                a.append(pa)
                b.append(pb)
        self.A = np.concatenate(a) if a else np.zeros((0, 2))
        self.B = np.concatenate(b) if b else np.zeros((0, 2))
        self.idx = np.concatenate(idx) if idx else np.zeros(0, int)
        self.names = names
        self.named = np.array([bool(n) for n in names], bool)[self.idx] if names else np.zeros(0, bool)
        d = self.B - self.A
        self.T = d / np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]

    def nearest(self, q):
        """The distance (m,) of every segment to point q."""
        d = self.B - self.A
        t = np.clip(((q - self.A) * d).sum(1) / np.maximum((d * d).sum(1), 1e-12), 0.0, 1.0)
        p = self.A + t[:, None] * d
        return np.hypot(p[:, 0] - q[0], p[:, 1] - q[1])


# --- signal_arm: a blade on the mast arm ----------------------------------------------------------------------------
def cross_street(roads, x, y, yaw):
    """The name of the street the approach of the signal at (x, y) (yaw: its model's +x, the drivers' right) crosses,
    or "" (none named near, or the same street as the approach's)."""
    if not len(roads.A):
        return ""
    r = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw))])
    d = np.array([-r[1], r[0]])                     # the travel (the model's +y)
    p = np.array([x, y])
    q = p - r * CROSS_PROBE[0] + d * CROSS_PROBE[1]
    tan = roads.T
    dist = roads.nearest(q)
    ok = (dist <= CROSS_MAX_M) & (np.abs(tan @ d) <= CROSS_COS) & roads.named
    if not ok.any():
        return ""
    name = roads.names[int(roads.idx[int(np.argmin(np.where(ok, dist, np.inf)))])]
    dist0 = roads.nearest(p - r * 2.0 - d * 6.0)      # (the approach's own street: behind, in its lanes)
    along = (np.abs(tan @ d) >= 0.8) & (dist0 <= 15.0)
    if along.any() and roads.names[int(roads.idx[int(np.argmin(np.where(along, dist0, np.inf)))])] == name:
        return ""
    return name


def arm_z(out):
    """The height of the boom's arm (its centre line) out m from the pole."""
    return float(np.interp(out, *zip(*ARM_PROFILE)))


def blade(mesh, x, y, z, yaw, text, st):
    """A street-name blade in style st on the mast arm of the boom at (x, y, z) (its row's base) turned yaw. Returns
    its width."""
    r = np.array([math.cos(math.radians(yaw)), math.sin(math.radians(yaw))])
    d = np.array([-r[1], r[0]])
    h, size = float(st["height_m"]), float(st["text_m"])
    upper = st.get("case") == "upper"
    text = signmesh.clean(text, upper)
    w = float(np.clip(signmesh.advance(text) * size + 0.40, *st["width_m"]))
    out = ARM_REACH + w / 2                         # along the arm (local -x)
    up = arm_z(out)                                 # (level, its middle on the rising arm behind it)
    c = np.array([x, y]) - r * out - d * ARM_FRONT
    mesh.transform((c[0], c[1], z), d)               # local front (-y) toward the drivers
    mesh.panel_body(w + 0.03, h + 0.03, up, 0.03)
    mesh.rect(signmesh.COLOUR[st["border"]], w, h, up, -0.027, 0.03)
    mesh.rect(signmesh.COLOUR[st["bg"]], w - 0.04, h - 0.04, up, -0.029, 0.025)
    mesh.text(text, up, -0.032, size, w - 0.22, white=st["ink"] == "white", uppercase=upper, centre_ink=True)
    for sx in (-w / 3, w / 3):                       # brackets, each to the arm behind it
        mesh.box(signmesh.METAL, (sx, ARM_FRONT / 2, arm_z(out - sx)), (0.025, ARM_FRONT / 2, 0.06))
    return w


def signal_arm_signs(mesh, tile):
    """Blades on the mast arms of the tile's signals where the country's style mounts them there."""
    x0, y0, x1, y1 = tile.box
    signals = [s for s in tile.signals if x0 <= s[0] < x1 and y0 <= s[1] < y1]
    styles = tile.styles_at([(s[0], s[1]) for s in signals])
    for (x, y, z, yaw), st in zip(signals, styles):
        if st is None or st["mount"] != "signal_arm" or len(mesh.p) > MAX_VERTS:
            continue
        name = cross_street(tile.roads, x, y, yaw)
        if name:
            blade(mesh, x, y, z, yaw, short_name(name, st), st)


# a style's "mount": fn(mesh, tile) draws its signs into mesh
MOUNTS = {"signal_arm": signal_arm_signs}


# --- the tile -------------------------------------------------------------------------------------------------------
class Tile:
    """What the mounts read of a tile, each loaded the first time one asks."""

    def __init__(self, frame, aid, tx, ty, context, out_dir, world=False):
        import roads as roadsmod
        self.frame, self.aid, self.tx, self.ty, self.context, self.out_dir, self.world = (
            frame, aid, tx, ty, context, out_dir, world)
        self.box = tuple(roadsmod.tile_bbox(tx, ty))

    @cached_property
    def signals(self):
        return signals_near(self.out_dir, self.aid, self.frame, self.tx, self.ty, self.box)

    @cached_property
    def roads(self):
        x0, y0, x1, y1 = self.box
        box = (x0 - MARGIN_M, y0 - MARGIN_M, x1 + MARGIN_M, y1 + MARGIN_M)
        return Roads(self.frame, [self.context.cell(k) for k in _cells(self.frame, box)], box)

    def styles_at(self, xy):
        return styles_at(self.frame, xy, self.world)


def build_tile(frame, aid, tx, ty, context, out_dir, world=False):
    """The tile's street-name signs as a roadmesh mesh (origin the tile's corner), or None. world: BNE_SIGNS_WORLD."""
    tile = Tile(frame, aid, tx, ty, context, out_dir, world)
    x0, y0, x1, y1 = tile.box
    probes = [(x0, y0), (x0, y1), (x1, y0), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]
    if not world and not any(tile.styles_at(probes)):
        return None
    mesh = signmesh.Mesh((x0, y0, 0.0))
    for place in MOUNTS.values():
        place(mesh, tile)
    if not mesh.t:
        return None
    return np.asarray(mesh.p), np.asarray(mesh.t), np.asarray(mesh.uv), signmesh.MATERIALS, np.asarray(mesh.tm)
