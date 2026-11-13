"""Street-name signs, each country's own (data/street_sign_styles.json; sign tile field "streets"; streetsigns.rs)."""
import json
import os
from functools import cached_property, lru_cache

import numpy as np

import fast
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
KERNEL = 223                    # earthfast's street_sign_* kernels
_CACHE = {}
_CACHE_MAX = 128


# --- styles ---------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _table():
    with open(STYLES_PATH, encoding="utf-8") as f:
        return json.load(f)


def style(country):
    """The street-name sign style of a country (ISO code), or None: no street signs there."""
    return _table()["countries"].get((country or "").upper())


def countries_at(frame, xy):
    """Each point's country code, or None: "US" from signstates (offline), else countrybounds'."""
    import countrybounds
    import signstates
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    if not len(xy):
        return []
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    out = ["US" if u else None for u in signstates.states(la, lo) != ""]
    rest = [i for i, c in enumerate(out) if c is None]
    if rest and set(_table()["countries"]) - {"US"}:     # (countrybounds downloads its file)
        for i, c in zip(rest, countrybounds.bundled().codes_at([(la[i], lo[i]) for i in rest])):
            out[i] = c
    return out


def styles_at(frame, xy, world=False):
    """The style at each point xy, or None (world: WORLD_STYLE where the country has none)."""
    fallback = style(WORLD_STYLE) if world else None
    return [style(c) or fallback for c in countries_at(frame, xy)]


def short_name(name, st):
    """A road's name as the style's signs write it ("East Fayette Street" -> "E FAYETTE ST")."""
    rules = _table()["abbrev"].get(st.get("abbrev"), {})
    return fast._ef.street_sign_abbrev([signmesh.clean(name, False)], rules.get("dirs", {}), rules.get("first", {}),
                                       rules.get("last", {}), st.get("case") == "upper")[0]


# --- inputs ---------------------------------------------------------------------------------------------------------
def _signal_rows(path, frame):
    """[(x, y, z, yaw)] of a road tile JSON's signals in frame (a geotile's from its own), or None."""
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
    """The signals in box ((k, 4) [x, y, z, yaw]), each once, from the road tiles and geotiles round it."""
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
    rows = np.ascontiguousarray(np.asarray(got, np.float64).reshape(-1, 4))
    return rows[fast._ef.street_sign_signals(rows, [float(v) for v in box])]


def _cells(frame, box):
    x0, y0, x1, y1 = box
    la, lo, _ = frame.enu_to_geodetic(np.array([x0, x1]), np.array([y0, y1]), 0.0)
    return mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))


def road_lines(frame, cells_feats, box=None):
    """The drivable roads whose bounding box meets box: (pts, off, name_id (-1: unnamed), levels [line, s0, s1] (bridges
    and tunnels), names)."""
    import roads as roadsmod
    pts, off, name_id, levels, names, seen = [], [0], [], [], {}, set()
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
            levels += [(len(name_id), s0, s1) for s0, s1, _ in roadsmod.tag_level_ranges(feats, i)]
            name = str(nm[i] or "") if nm is not None else ""
            name_id.append(names.setdefault(name, len(names)) if name else -1)
            pts.append(np.asarray(parts[0][0], np.float64)[:, :2])
            off.append(off[-1] + len(pts[-1]))
    return (np.ascontiguousarray(np.vstack(pts) if pts else np.zeros((0, 2))), np.asarray(off, np.int64),
            np.asarray(name_id, np.int64), np.asarray(levels, np.float64).reshape(-1, 3), list(names))


# --- signal_arm: a blade on the mast arm ----------------------------------------------------------------------------
def cross_streets(roads, signals):
    """The street each signal's approach crosses, or ""."""
    pts, off, name_id, levels, names = roads
    got = fast._ef.street_sign_cross(np.ascontiguousarray(signals, np.float64).reshape(-1, 4), pts, off, name_id,
                                     levels)
    return [names[name_id[k]] if k >= 0 else "" for k in got]


@lru_cache(maxsize=1)
def _font():
    """signmesh's default face (the fallback's for the rest) as street_sign_mesh's arrays."""
    face, fallback = signmesh.face_font(), signmesh.face_font("fallback")
    glyphs = {**fallback["glyphs"], **face["glyphs"]}
    row = lambda g: [g["advance"], *g["bounds"], *g["uv"]]
    chars = sorted(glyphs)
    kern = face.get("kerning", {})
    return (np.array([ord(c) for c in chars], np.int64), np.asarray([row(glyphs[c]) for c in chars], np.float64),
            row(face["glyphs"].get("?", fallback["glyphs"]["?"])),
            np.array([[ord(k[0]), ord(k[1])] for k in kern], np.int64).reshape(-1, 2),
            np.array(list(kern.values()), np.float64), float(face["horizontal_scale"]), float(face["letter_spacing"]))


def blades(signals, texts, styles, origin):
    """(p, t, uv, material per triangle) of the blades."""
    rows = [[MOUNTS[st["mount"]], st["height_m"], st["text_m"], *st["width_m"], signmesh.COLOUR[st["border"]],
             signmesh.COLOUR[st["bg"]], st["ink"] == "white"] for st in styles]
    return fast._ef.street_sign_mesh(np.ascontiguousarray(signals, np.float64).reshape(-1, 4), list(texts),
                                     np.asarray(rows, np.float64).reshape(-1, 8), *_font(),
                                     [float(v) for v in origin], MAX_VERTS)


def signal_arm_signs(tile):
    """Blades on the mast arms of the tile's signals where the country's style mounts them there."""
    styles = tile.styles_at(tile.signals[:, :2])
    keep = [k for k, st in enumerate(styles) if st is not None and st["mount"] in MOUNTS]
    signals, styles = tile.signals[keep], [styles[k] for k in keep]
    names = cross_streets(tile.roads, signals) if keep else []
    texts = [short_name(n, st) if n else "" for n, st in zip(names, styles)]
    return blades(signals, texts, styles, (tile.box[0], tile.box[1], 0.0))


# a style's "mount": streetsigns.rs's code for it
MOUNTS = {"signal_arm": 0}


# --- the tile -------------------------------------------------------------------------------------------------------
class Tile:
    """What the mounts read of a tile, loaded on first use."""

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
        return road_lines(self.frame, [self.context.cell(k) for k in _cells(self.frame, box)], box)

    def styles_at(self, xy):
        return styles_at(self.frame, xy, self.world)


def build_tile(frame, aid, tx, ty, context, out_dir, world=False):
    """The tile's street-name signs as a roadmesh mesh (origin the tile's corner), or None. world: BNE_SIGNS_WORLD."""
    tile = Tile(frame, aid, tx, ty, context, out_dir, world)
    x0, y0, x1, y1 = tile.box
    probes = [(x0, y0), (x0, y1), (x1, y0), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]
    if not world and not any(tile.styles_at(probes)):
        return None
    fast.require(KERNEL, "street-name signs")
    p, t, uv, mi = signal_arm_signs(tile)
    if not len(t):
        return None
    return p, t, uv, signmesh.MATERIALS, mi
