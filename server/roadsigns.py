"""Ordinary road signs from what the road data states (task 017): the glue of the native kernels earthfast.road_sign_place
(which rules hold where: native/earthfast/src/roadsigns.rs) and road_sign_mesh (one mesh on one atlas). Built in the
sign tiles (signstream.py, field "rsigns"), render-only, where sign_systems.json knows the country.

Off unless BNE_ROADSIGNS=on or the settings page's Road signs is on with the feature enabled on the server.
"""
from functools import lru_cache
import json
import os

import numpy as np

import exitsigns
import fast
import mapcells
import mapgeom
import roads
import speedsigns

KERNEL = 153                    # earthfast VERSION with road_sign_place / road_sign_mesh
ON = os.environ.get("BNE_ROADSIGNS", "off") == "on"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SHAPES = {"circle": 0, "tri_up": 1, "tri_down": 2, "diamond": 3, "octagon": 4, "square": 5}
METAL = "bne_exit_metal"
OPEN_CLASSES = {"track", "unknown"}     # a road ending on one of these goes on
CROSSING_KINDS = {"zebra", "marked", "uncontrolled"}


def enabled(server):
    """Whether the sign tiles carry road signs: the server's flag and the settings page's switch."""
    return ON and bool(getattr(server, "rsigns_on", True))


@lru_cache(maxsize=1)
def tables():
    with open(os.path.join(DATA, "sign_roles.json"), encoding="utf-8") as f:
        roles = json.load(f)
    with open(os.path.join(DATA, "sign_systems.json"), encoding="utf-8") as f:
        systems = json.load(f)
    return roles, systems, {r["id"]: (i, r) for i, r in enumerate(roles["roles"])}


def system(country):
    """The country's sign system (sign_systems.json), or None: no road signs there yet."""
    return tables()[1]["countries"].get((country or "").upper())


def widest(sizes):
    return max(max(v) for v in sizes.values())


def is_marked_crossing(tags):
    """Whether a crossing node's OSM tags state a marked crossing without signals."""
    if not tags:
        return False
    try:
        t = json.loads(tags)
    except ValueError:
        return False
    if t.get("highway") != "crossing" or t.get("crossing:markings") == "no" or t.get("crossing:signals") == "yes":
        return False
    if t.get("foot") == "no":
        return False
    return t.get("crossing") in CROSSING_KINDS or t.get("crossing_ref") == "zebra"


def crossings(frame, cells):
    """(n, 2) of the marked crossings of infrastructure cells, in frame, each once."""
    ll = []
    for feats in cells:
        if feats is None or not len(feats):
            continue
        cls, tags, own = feats.attrs["class"], feats.attrs["tags"], feats.own
        for i in np.flatnonzero((feats.kind == mapgeom.POINT) & (cls == "crossing") & own):
            if is_marked_crossing(str(tags[i])):
                ll.append(np.asarray(feats.parts(int(i))[0][0], np.float64)[0, :2])
    if not ll:
        return np.zeros((0, 2))
    ll = np.array(ll, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return np.ascontiguousarray(np.column_stack([e, n]), np.float64)


def open_ends(frame, cells):
    """(n, 2) of the ends of the roads not in the sign network that traffic can go on along."""
    ll = []
    for feats in cells:
        cls = feats.attrs["class"]
        for i in np.flatnonzero(np.isin(cls, sorted(OPEN_CLASSES))):
            parts = feats.parts(int(i))
            if parts and len(parts[0]) and len(parts[0][0]) >= 2:
                line = np.asarray(parts[0][0], np.float64)
                ll += [line[0, :2], line[-1, :2]]
    if not ll:
        return np.zeros((0, 2))
    ll = np.array(ll, np.float64)
    e, n, _ = frame.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.0)
    return np.ascontiguousarray(np.column_stack([e, n]), np.float64)


def place(md, frame, box, context, left_hand, mph, sys_row):
    """(k, 9) of road_sign_place's rows in box (x0, y0, x1, y1)."""
    fast.require(KERNEL, "road signs")
    x0, y0, x1, y1 = box
    m = speedsigns.MARGIN_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m]), np.array([y0 - m, y1 + m]), 0.0)
    rows, seen, cells = [], set(), []
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = context.cell(key)
        if feats is None or not len(feats):
            continue
        cells.append(feats)
        for fid, row in speedsigns._cell_rows(feats, frame, context.urban, context.widths, left_hand, mph).items():
            if fid not in seen:
                seen.add(fid)
                rows.append(row)
    arrays = speedsigns._arrays(rows, box)
    if arrays is None:
        return np.zeros((0, 9))
    pts, ll, off, meta, con_at, con_id, con_off, _lim_at, _lim_val, _lim_off, skip, skip_off = arrays
    b_la, b_lo, _ = frame.enu_to_geodetic(np.array([x0 - 5, x1 + 5]), np.array([y0 - 5, y1 + 5]), 0.0)
    infra = [md.cell(k, layers=("infrastructure",), kind="prefetch").layers.get("infrastructure")
             for k in mapcells.cells_for_bbox(float(b_la.min()), float(b_lo.min()), float(b_la.max()), float(b_lo.max()))]
    cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    poly, poly_off = speedsigns._outlines(context.pavements(frame, np.c_[clo, cla], left_hand))
    sizes = tables()[1]["sizes"]["default"]
    return fast._ef.road_sign_place(pts, ll, off, meta, con_at, con_id, con_off, skip, skip_off, crossings(frame, infra),
                                    open_ends(frame, cells), poly, poly_off, [float(v) for v in box], bool(left_hand),
                                    [float(v) for v in sys_row["lateral"]], float(widest(sizes)))


def table(signs, z_ground, z_road, sys_row):
    """road_sign_mesh's rows of road_sign_place's, heights given."""
    roles, systems, by_id = tables()
    sizes = systems["sizes"]["default"]
    out = np.zeros((len(signs), 10))
    for k, row in enumerate(signs):
        cell, role = by_id[systems["rules"][int(row[4])]]
        urban, size = row[8] != 0.0, int(row[5])
        out[k] = (row[0], row[1], z_ground[k], z_road[k], row[2], row[3], cell, SHAPES[role["shape"]],
                  sizes[role["shape"]][size], sys_row["mount"][0 if urban else 1])
    return np.ascontiguousarray(out)


def build_tile(md, frame, tx, ty, elev, context, decks=(), left_hand=False, mph=True, country=None):
    """The mesh (signstream.write_tile's) of road tile (tx, ty)'s road signs, or None (none)."""
    sys_row = system(country)
    if sys_row is None:
        return None
    box = roads.tile_bbox(tx, ty)
    signs = place(md, frame, box, context, left_hand, mph, sys_row)
    keep = [k for k in range(len(signs)) if not exitsigns._on_deck(signs[k, :2], decks, 1.5)]
    if not keep:
        return None
    signs = signs[keep]
    pts = np.vstack([signs[:, 0:2], signs[:, 6:8]])
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    h = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, h)[2], np.float64).ravel()
    n = len(signs)
    roles = tables()[0]
    p, t, uv, mi = fast._ef.road_sign_mesh(table(signs, z[:n], z[n:], sys_row), roles["cols"], roles["rows"],
                                           [float(box[0]), float(box[1]), 0.0])
    if not len(t):
        return None
    return p, t, uv, ["bne_signs_" + sys_row["atlas"], METAL], mi
