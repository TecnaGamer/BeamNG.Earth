"""Direction signs before junctions (the user, 2026-11-02: advance direction boards with the road numbers, the places
ahead in the local language and in English for the cities, and their distances; opt-in, BNE_DIRSIGNS=on or the settings
page's Direction signs).

The glue of the native kernels earthfast.dirsign_place (where the boards stand, which way each arm of the junction
goes, which places lie ahead of it), dirsign_face (the board's picture: PNG, drawn in the kernel) and dirsign_mesh
(panel and posts). The rules are there (native/earthfast/src/dirsigns.rs); here the data they need and what they
say: the places (server/data/places_<release>.json.gz, tools/build_places.py), the roads' route numbers (the map layer
road_routes, Overture's segment `routes`), and the styles by country (server/data/dirsigns_styles.json: colours, number
plates, which route network is which plate).

Built in the sign tiles (signstream.py, field "dirs"), render-only. The faces are PNGs in the session folder named by
the hash of what they show (one Material a distinct board, made by the mod: dirsigns.lua).
"""
from functools import lru_cache
import gzip
import hashlib
import json
import logging
import os
import re

import numpy as np

import exitsigns
import fast
import mapcells
import roads
import speedsigns
import staging

log = logging.getLogger("earthd.dirsigns")

ON = os.environ.get("BNE_DIRSIGNS", "off") == "on"
KERNEL = 141                    # earthfast VERSION with dirsign_place / dirsign_face / dirsign_mesh
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PLACES_FILE = os.path.join(DATA, "places_2026-10-21.0.json.gz")
PLACES_SHA256 = "aea8360e2b1a5dc51c1b54700b355aa0dfb7e699fee5853f6a3ae0f0e223b989"
STYLES_FILE = os.path.join(DATA, "dirsigns_styles.json")
FONTS = (os.path.join(DATA, "fonts", "Overpass-Bold.ttf"), os.path.join(DATA, "fonts", "FiraSansCondensed-Medium.ttf"))
FACE_VERSION = 1                # (in every face's hash: a changed drawing is another file)
MAX_FACES = 24                  # distinct boards a tile makes at most
METAL = "bne_dir_metal"
CLASS = {"village": 0, "hamlet": 0, "town": 1, "city": 2}
CITY_POP = 5000                 # a place without a class is a city from this many people on
MAX_PLATES = 3
REPEAT_MARGIN_M = 600.0         # m beyond a repeat cell the roads are read (a board stands before its junction)
_FONTS_SET = False


class Places:
    """The place snapshot as the kernel wants it: (p, 4) [lon, lat, population, class] and the names."""

    def __init__(self, doc):
        rows = doc["places"]
        self.names = [r["name"] for r in rows]
        self.en = [r.get("en", "") for r in rows]
        self.cc = [r["cc"] for r in rows]
        self.array = np.ascontiguousarray(
            [[r["lon"], r["lat"], r.get("pop", 0), CLASS.get(r.get("class"), 1 if r.get("pop", 0) >= CITY_POP else 0)]
             for r in rows], np.float64).reshape(-1, 4)


@lru_cache(maxsize=1)
def places():
    with open(PLACES_FILE, "rb") as f:
        raw = f.read()
    if hashlib.sha256(raw).hexdigest() != PLACES_SHA256:
        raise ValueError("place snapshot checksum mismatch")
    return Places(json.loads(gzip.decompress(raw)))


@lru_cache(maxsize=1)
def styles():
    with open(STYLES_FILE, encoding="utf-8") as f:
        doc = json.load(f)
    for c in doc["countries"].values():
        for rule in c["networks"]:
            rule["_ref"] = re.compile(rule["ref"]) if rule.get("ref") else None
    return doc


def _set_fonts():
    global _FONTS_SET
    if not _FONTS_SET:
        with open(FONTS[0], "rb") as a, open(FONTS[1], "rb") as b:
            fast._ef.dirsign_set_fonts(a.read(), b.read())
        _FONTS_SET = True


def enabled(server):
    """Whether the sign tiles carry direction boards: BNE_DIRSIGNS=on, or the settings page's Direction signs."""
    return ON or bool(getattr(server, "dirs_on", False))


def parse_routes(text):
    """[(network, ref, from, to)] of a road_routes row's "network|ref|from|to;..." (the layer's routes_txt)."""
    out = []
    for part in str(text).split(";"):
        bits = part.split("|")
        if len(bits) == 4 and bits[1]:
            try:
                out.append((bits[0], bits[1], float(bits[2]), float(bits[3])))
            except ValueError:
                continue
    return out


class RouteTable:
    """Route numbers as small integers for the kernel: (network, ref) <-> id."""

    def __init__(self):
        self.ids = {}
        self.items = []

    def id(self, network, ref):
        key = (network, ref)
        if key not in self.ids:
            self.ids[key] = len(self.items)
            self.items.append(key)
        return self.ids[key]


def plate_for(style, network, ref):
    """(order, plate key) of a route in a country's style, or None (a route that has no plate there)."""
    for i, rule in enumerate(style["networks"]):
        if rule["network"] != network:
            continue
        if rule["_ref"] is not None and not rule["_ref"].search(ref):
            continue
        return i, rule["plate"]
    return None


def km_text(metres, factor, rounding):
    """The distance a board shows: the straight distance times the road factor, whole km, to 5 from 20 km."""
    km = metres * factor / 1000.0
    for limit, step in rounding:
        if km < limit:
            return str(max(1, int(round(km / step) * step)))
    return str(int(round(km)))


def _arrays(rows, box, margin, table, routes_of):
    """dirsign_place's road arrays of the speed-signs' rows ({fid: (line, pts, meta, cons, limits, skips)}) near box."""
    x0, y0, x1, y1 = box
    ll, xy, off, meta = [], [], [0], []
    con_at, con_id, con_off = [], [], [0]
    rt_id, rt_from, rt_to, rt_off = [], [], [], [0]
    skip, skip_off = [], [0]
    ids = {}
    for fid, (line, pts, m, cons, _limits, skips) in sorted(rows.items()):
        if (pts[:, 0].max() < x0 - margin or pts[:, 0].min() > x1 + margin or
                pts[:, 1].max() < y0 - margin or pts[:, 1].min() > y1 + margin):
            continue
        ll.append(line)
        xy.append(pts)
        off.append(off[-1] + len(pts))
        meta.append(m)
        for f, c in cons:
            con_at.append(f)
            con_id.append(ids.setdefault(c, len(ids)))
        con_off.append(len(con_at))
        for network, ref, a, b in routes_of.get(fid, ()):
            rt_id.append(table.id(network, ref))
            rt_from.append(a)
            rt_to.append(b)
        rt_off.append(len(rt_id))
        skip.extend(skips)
        skip_off.append(len(skip))
    if not xy:
        return None
    f64 = lambda a, *shape: np.ascontiguousarray(np.asarray(a, np.float64).reshape(*shape))    # noqa: E731
    i64 = lambda a: np.ascontiguousarray(np.asarray(a, np.int64))                              # noqa: E731
    return (f64(np.concatenate(xy), -1, 2), f64(np.concatenate(ll), -1, 2), i64(off), f64(meta, -1, 5),
            f64(con_at, -1), i64(con_id), i64(con_off), i64(rt_id), f64(rt_from, -1), f64(rt_to, -1), i64(rt_off),
            f64(skip, -1, 2), i64(skip_off))


def _route_rows(md, keys, rows):
    """{fid: [(network, ref, from, to)]} of the road_routes layers of the cells (joined by the segment's id)."""
    out = {}
    for key in keys:
        try:
            layer = md.cell(key, layers=("road_routes",), kind="demand").layers.get("road_routes")
        except Exception as error:      # (optional scenery: the boards go without numbers, the roads go on)
            log.debug("road routes of %s: %s", key, error)
            continue
        if layer is None or not len(layer):
            continue
        txt = layer.attrs.get("routes_txt")
        for i, fid in enumerate(layer.ids):
            fid = str(fid)
            if fid in rows and fid not in out and txt is not None:
                out[fid] = parse_routes(txt[i])
    return out


def _params(p):
    return [p["advance_urban_m"], p["advance_rural_m"], p["advance_freeway_m"], p["lateral_urban_m"],
            p["lateral_rural_m"], p["lateral_freeway_m"], p["panel_w_m"], p["cone_deg"], p["near_min_m"],
            p["near_max_m"], p["far_max_m"], float(p["near_min_pop"]), float(p["far_min_pop"]), p["mount_m"],
            p["repeat_m"]]


def best_plates(marks):
    """The plates a row shows of its sorted (order, plate key, ref) memberships: the best class of the road (the roads
    that share a carriageway have several; a board names the highest) and the European road, at most MAX_PLATES."""
    own = [m for m in marks if m[1] != "e"]
    if own:
        own = [m for m in own if m[1] == own[0][1]]
    return (own + [m for m in marks if m[1] == "e"])[:MAX_PLATES]


def _row_content(style, row, table, pl, factor, rounding, show_en):
    """(turn, [(plate index, text)], [(local, English, km)]) of one kernel row, or None (nothing to say)."""
    turn = int(row[0])
    marks = []
    for k in range(2, 5):
        if row[k] >= 0:
            network, ref = table.items[int(row[k])]
            hit = plate_for(style, network, ref)
            if hit is not None:
                marks.append((hit[0], hit[1], ref))
    marks = best_plates(sorted(set(marks)))
    dests = []
    for idx_col, d_col in ((5, 6), (7, 8)):
        i = int(row[idx_col])
        if i < 0:
            continue
        # a city or a town has its English name under the local one, a village only the local (the user, 2026-11-02)
        city = pl.array[i, 3] >= 1
        en = pl.en[i] if (show_en and city) else ""
        dests.append((pl.names[i], en, km_text(row[d_col], factor, rounding)))
    if not dests:
        return None
    return turn, marks, dests


def _face(out_dir, vfs_dir, style, plate_keys, kind, content, face):
    """(material name, vfs texture path, width m, height m) of a board's face: drawn once (by its hash)."""
    plates = [style["plates"][k] for k in plate_keys]
    board = style["board"][kind]
    flat = [*board["bg"], *board["ink"], *board["trim"], face["cap_m"], face["en_share"], face["min_w_m"],
            face["max_w_m"], face["inset_m"], face["trim_w_m"], 0.0, 0.0, 0.0]
    rows = [(t, [(plate_keys.index(k), txt) for k, txt in marks], dests) for t, marks, dests in content]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, plates, rows, face["px_per_m"]], sort_keys=True,
                                ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    name = f"bne_dir_{h}"
    dds = os.path.join(out_dir, name + ".dds")
    spec = [[float(p[0]), *map(float, p[1]), *map(float, p[2]), *map(float, p[3])] for p in plates]
    # (drawn every time, written once: the board's size in m is the drawing's, the picture a power-of-two stretch of it)
    data, w_m, h_m = fast._ef.dirsign_face(rows, flat, spec, float(face["px_per_m"]))
    if not os.path.exists(dds):
        # a DDS (DXT1 with its mips, imagery.write_dds), as every other texture the server writes: a PNG in the session
        # folder went through the engine's texture cooker at run time, and the game crashed as it started importing
        # ("TextureCooker| Texture[] started importing", twice at Heide on a 100 mph drive, 2026-11-05)
        import io
        import imagery
        from PIL import Image
        with Image.open(io.BytesIO(bytes(data))) as im:
            rgb = np.asarray(im.convert("RGB"), np.uint8)
        imagery.write_dds(dds, rgb)
    return name, f"{vfs_dir}/{name}.dds", w_m, h_m


def build_tile(md, frame, tx, ty, elev, context, decks, left_hand, mph, out_dir, vfs_dir, show_en=True):
    """(mesh, faces) of road tile (tx, ty)'s direction boards: the mesh as signstream.write_tile takes it
    (vertices, triangles, uv, materials, material per triangle) and {material: vfs path of its picture}; (None, {})
    where there is none."""
    fast.require(KERNEL, "direction signs")
    _set_fonts()
    st = styles()
    pl = places()
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    # (the repeats of a board are thinned over a cell of a grid: the roads of every cell the tile's boards could share)
    m = max(speedsigns.MARGIN_M, st["placement"]["repeat_m"] + REPEAT_MARGIN_M)
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x1 + m]), np.array([y0 - m, y1 + m]), 0.0)
    rows, seen, keys = {}, set(), []
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = context.cell(key)
        if feats is None or not len(feats):
            continue
        keys.append(key)
        for fid, row in speedsigns._cell_rows(feats, frame, context.urban, context.widths, left_hand, mph).items():
            if fid not in seen:
                seen.add(fid)
                rows[fid] = row
    table = RouteTable()
    arrays = _arrays(rows, box, m, table, _route_rows(md, keys, rows))
    if arrays is None:
        return None, {}
    cla, clo, _ = frame.enu_to_geodetic(np.array([x0, x1, x0, x1]), np.array([y0, y0, y1, y1]), 0.0)
    poly, poly_off = speedsigns._outlines(context.pavements(frame, np.c_[clo, cla], left_hand))
    boards, krows = fast._ef.dirsign_place(*arrays, poly, poly_off, pl.array, [float(v) for v in box], bool(left_hand),
                                           _params(st["placement"]))
    if not len(boards):
        return None, {}
    face_cfg = st["face"]
    factor, rounding = st["placement"]["detour_factor"], st["distance_round"]
    kinds = ("urban", "road", "freeway")
    keep, signs, faces, materials, mats_of = [], [], {}, [METAL], {}
    for b in boards:
        if exitsigns._on_deck(b[0:2], decks, 1.5):
            continue
        first, n = int(b[10]), int(b[11])
        mine = krows[first:first + n]
        # the country's style: where the board's first place is
        cc = next((pl.cc[int(r[i])] for r in mine for i in (5, 7) if r[i] >= 0), None)
        style = st["countries"].get(cc)
        if style is None:
            continue
        content = [c for c in (_row_content(style, r, table, pl, factor, rounding, show_en) for r in mine) if c]
        if not content:
            continue
        plate_keys = sorted({k for _, marks, _ in content for _, k, _ in marks})
        flat = [(t, [(k, txt) for _, k, txt in marks], dests) for t, marks, dests in content]
        if len(faces) >= MAX_FACES:
            break
        kind = kinds[int(b[7])]
        name, tex, w, h = _face(out_dir, vfs_dir, style, plate_keys, kind, flat, face_cfg)
        if name not in mats_of:
            mats_of[name] = len(materials)
            materials.append(name)
            faces[name] = tex
        keep.append((b, mats_of[name], w, h))
    if not keep:
        return None, {}
    pts = np.vstack([np.array([k[0][0:2] for k in keep]), np.array([k[0][4:6] for k in keep])])
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    hz = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, hz)[2], np.float64).ravel()
    n = len(keep)
    for i, (b, mat, w, h) in enumerate(keep):
        signs.append([b[0], b[1], z[i], z[n + i], b[2], b[3], w, h, mat, st["placement"]["mount_m"]])
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray(signs, np.float64), [float(box[0]), float(box[1]), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces
