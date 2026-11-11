"""Hiking guideposts' plates and arrows, and information boards and maps (stages D and E of the local points of interest).

Made in the sign tile (signstream.build_tile) beside the direction boards, the stations' name signs and the town signs: one
mesh, one set of pictures (the tile's "dirs_faces"; the mod makes their Materials: dirsigns.lua). The pictures and the boards
are the kernel's (earthfast.dirsign_town_face, dirsign_mesh), as busstationsign.py's.

A guidepost (Overture infrastructure, OpenStreetMap information=guidepost) is the galvanised tube localpoi.py draws as a prop
(lp_gp). Here it gets, hung on that tube:
  - the local plate, on top: the place's name and its height ("550 m", the `ele` tag), when it has either;
  - the arrows: up to MAX_ARROWS, each one turned to point at a target. The targets are the nearest named guideposts of the
    same kind (hiking: white; cycling, `bicycle=yes` without `hiking=yes`: yellow) within TARGET_R_M, in different directions;
    the distance is the straight line times WIND, as a walker's estimate (the map data has no trail routes to follow: Overture's
    `routes` are empty on paths and tracks). The marks of the marked trails' colours are not drawn (no data for them).
An information board or a map (information=board / map) is one panel on two posts, its picture the board's name (colour by
board_type), turned to the nearest path or road, or by its `direction` tag.

    sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, country=None) -> (mesh, faces) or (None, {})
"""
import hashlib
import json
import logging
import math
import os

import numpy as np

import dirsigns
import fast
import imagery
import localpoi
import roads

log = logging.getLogger("earthd.localpoi")

KERNEL = 204                    # earthfast VERSION with dirsign_town_face / dirsign_mesh
FACE_VERSION = 1
PX_PER_M = 900.0                # a plate is 0.4 m wide: its letters need the pixels
BOARD_PX_PER_M = 450.0
TARGET_R_M = 2500.0             # guideposts this near are candidates for an arrow
MIN_TARGET_M = 120.0
WIND = 1.25                     # a trail is longer than the straight line
MAX_ARROWS = 3
SECTOR_DEG = 40.0               # arrows point this far apart at least
MAX_POSTS = 30                  # guideposts a tile gets plates for
MAX_BOARDS = 20
PLATE_Z_M = 2.22                # the plate's bottom over the ground (the tube is 2.6 m)
STACK_GAP_M = 0.02
HOLD_M = 0.17                   # a board's row point is this far behind the tube's axis (dirsign_mesh mode 0 hangs the board
                                # 0.22 m in front of it)
PLATE_CAP_M, ARROW_CAP_M, SUB_SHARE = 0.055, 0.05, 0.8
PLATE_W_M, ARROW_W_M = (0.36, 0.46), (0.40, 0.62)
BOARD_CAP_M, BOARD_SUB_SHARE, BOARD_W_M = 0.11, 0.55, (1.0, 1.4)
BOARD_MOUNT_M = 1.0
HIKE_BG, BIKE_BG, INK = (250, 250, 248), (250, 200, 0), (22, 22, 22)
BOARD_BG = {"history": (104, 66, 38), "nature": (28, 98, 58), "geology": (150, 98, 30), "map": (0, 74, 153),
            "notice": (60, 64, 70)}
BOARD_BG_DEFAULT = (60, 64, 70)
BOARD_INK = (255, 255, 255)
MAP_TITLE = {"CZ": "Mapa", "SK": "Mapa", "PL": "Mapa", "DE": "Karte", "AT": "Karte", "CH": "Karte", "UA": "Мапа",
             "HU": "Térkép", "SI": "Zemljevid", "FR": "Carte"}
DOT_DECIMAL = {"US", "GB", "IE", "CA", "AU", "NZ", "JP", "CN", "KR"}
SUB_MAX_CHARS = 38              # a board's description longer than this is not its subtitle


def family(g):
    """'bike' for a cycling guidepost (bicycle=yes, not hiking=yes), else 'hike'."""
    return "bike" if g.get("bicycle") and not g.get("hiking") else "hike"


def distance_text(metres, country=None):
    """'3,5 km', or '850 m' under a kilometre (to 50 m)."""
    if metres < 950.0:
        return f"{max(50, int(round(metres / 50.0)) * 50)} m"
    text = f"{metres / 1000.0:.1f}"
    return (text if str(country or "").upper() in DOT_DECIMAL else text.replace(".", ",")) + " km"


def pick_targets(g, others, radius=TARGET_R_M):
    """[(name, metres along the trail (estimated), (bx, by) the unit bearing)] of up to MAX_ARROWS arrows for guidepost g:
    the nearest named guideposts of its kind, not of its own name, each in a direction at least SECTOR_DEG apart from the others."""
    cand = []
    me = g["name"].casefold()
    for o in others:
        if o["fid"] == g["fid"] or o["kind"] != "guidepost" or not o["name"] or o["name"].casefold() == me:
            continue
        if family(o) != family(g):
            continue
        dx, dy = o["x"] - g["x"], o["y"] - g["y"]
        d = math.hypot(dx, dy)
        if MIN_TARGET_M <= d <= radius:
            cand.append((d, o["name"], (dx / d, dy / d)))
    cand.sort(key=lambda c: (c[0], c[1]))
    picked, names = [], set()
    for d, name, b in cand:
        if name.casefold() in names:
            continue
        if any(math.degrees(math.acos(max(-1.0, min(1.0, b[0] * p[2][0] + b[1] * p[2][1])))) < SECTOR_DEG for p in picked):
            continue
        picked.append((name, d * WIND, b))
        names.add(name.casefold())
        if len(picked) >= MAX_ARROWS:
            break
    return picked


def _face(out_dir, vfs_dir, text, sub, bg, ink, cap, share, wmin, wmax, prefix, px=PX_PER_M):
    """(material, vfs path, width m, height m) of a board's picture: drawn once (named by its hash)."""
    flat = [*bg, *ink, *ink, *bg, cap, share, wmin, wmax, 0.012, 0.006, 0.0, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, text, sub, px], ensure_ascii=False)
                     .encode("utf-8")).hexdigest()[:12]
    material = f"{prefix}_{h}"
    path = os.path.join(out_dir, material + ".color.dds")
    data, w_m, h_m = fast._ef.dirsign_town_face(text, sub, False, flat, px)
    if not os.path.exists(path):
        imagery.write_picture_dds(path, data)      # (a DDS, never a PNG the engine cooks: imagery.write_picture_dds)
    return material, f"{vfs_dir}/{material}.color.dds", w_m, h_m


def _ele_text(ele):
    try:
        return f"{int(round(float(str(ele).replace(',', '.').split()[0])))} m"
    except (ValueError, IndexError):
        return ""


def hang_row(post, f, along, w, h, material, mount, z):
    """A dirsign_mesh row (mode 0, on clamps) for a board on a tube at post (x, y): the picture faces f (a unit vector, towards
    the reader), the board reaches `along` m along the reader's right from the tube (w/2: starting at it; 0: centred on it)."""
    fx, fy = f
    ux, uy = -fx, -fy                           # (the picture faces against the row's direction)
    rx, ry = -fy, fx                            # the reader's right (looking along -f)
    px = post[0] + ux * HOLD_M + rx * along
    py = post[1] + uy * HOLD_M + ry * along
    return [px, py, z, z, ux, uy, w, h, material, mount, 0.0, 1.0]


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-9 else (0.0, -1.0)


def board_title(g, country):
    """(text, sub) of an information board's picture."""
    name = g["name"]
    if not name:
        name = MAP_TITLE.get(str(country or "").upper(), "Map") if g["kind"] == "map" else ""
    if not name:
        name = {"history": "History", "nature": "Nature", "geology": "Geology", "notice": "Notice"}.get(g["board_type"], "")
    desc = g["description"]
    sub = desc if desc and len(desc) <= SUB_MAX_CHARS and desc.casefold() != name.casefold() else ""
    return name, sub


def facing_of(g, ways):
    """The unit vector an information board's picture faces: its `direction` tag (a compass bearing), else towards the nearest
    path or road, else south."""
    try:
        bearing = math.radians(float(str(g["direction"]).replace(",", ".").split(";")[0]))
        return math.sin(bearing), math.cos(bearing)
    except (ValueError, TypeError):
        pass
    toward = localpoi.toward_way(ways, g["x"], g["y"], 40.0)
    return toward if toward is not None else (0.0, -1.0)


def sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, country=None):
    """The plates, arrows and boards of the information points whose spot is in road tile (tx, ty): (mesh, faces) in
    dirsigns.build_tile's form (vertices, triangles, uv, materials, material per triangle), or (None, {})."""
    if not localpoi.ON:
        return None, {}
    box = roads.tile_bbox(tx, ty)
    infra_fn = lambda key: md.cell(key, layers=("infrastructure",), kind="demand").layers.get("infrastructure")
    here = localpoi.guideposts(frame, infra_fn, box)
    if not here:
        return None, {}
    posts = [g for g in here if g["kind"] == "guidepost" and (g["name"] or g["ele"] or g["hiking"] or g["bicycle"])]
    boards = [g for g in here if g["kind"] != "guidepost"]
    posts, boards = posts[:MAX_POSTS], boards[:MAX_BOARDS]
    if not posts and not boards:
        return None, {}
    fast.require(KERNEL, "local POI signs")
    dirsigns._set_fonts()
    others = localpoi.guideposts(frame, infra_fn, box, pad_m=TARGET_R_M) if posts else []
    roads_fn = lambda key: md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
    ways = localpoi.ways_near(frame, roads_fn, box, 60.0) if boards else None
    pts = np.array([[g["x"], g["y"]] for g in posts + boards], np.float64).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    hz = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, hz)[2], np.float64).ravel()
    materials, faces, rows = [dirsigns.METAL], {}, []

    def use(face):
        name, tex, w, h = face
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        return materials.index(name), w, h

    for i, g in enumerate(posts):
        bg = BIKE_BG if family(g) == "bike" else HIKE_BG
        targets = pick_targets(g, others)
        top = PLATE_Z_M
        plate_text = g["name"] or ""
        plate_sub = _ele_text(g["ele"])
        if not plate_text and plate_sub:
            plate_text, plate_sub = plate_sub, ""
        post = (g["x"], g["y"])
        if plate_text:
            fx, fy = (targets[0][2][1], -targets[0][2][0]) if targets else (0.0, -1.0)
            mi, w, h = use(_face(out_dir, vfs_dir, plate_text, plate_sub, bg, INK, PLATE_CAP_M, SUB_SHARE, *PLATE_W_M,
                                 "bne_lpp"))
            rows.append(hang_row(post, (fx, fy), 0.0, w, h, mi, PLATE_Z_M, z[i]))
            top = PLATE_Z_M - STACK_GAP_M
        else:
            top = PLATE_Z_M + 0.1
        for name, metres, b in targets:
            mi, w, h = use(_face(out_dir, vfs_dir, name, distance_text(metres, country), bg, INK, ARROW_CAP_M, SUB_SHARE,
                                 *ARROW_W_M, "bne_lpa"))
            top -= h
            rows.append(hang_row(post, (b[1], -b[0]), w / 2.0, w, h, mi, top, z[i]))
            top -= STACK_GAP_M
    base = len(posts)
    for j, g in enumerate(boards):
        text, sub = board_title(g, country)
        if not text:
            continue
        bg = BOARD_BG.get("map" if g["kind"] == "map" else g["board_type"], BOARD_BG_DEFAULT)
        f = facing_of(g, ways)
        mi, w, h = use(_face(out_dir, vfs_dir, text, sub, bg, BOARD_INK, BOARD_CAP_M, BOARD_SUB_SHARE, *BOARD_W_M,
                             "bne_lpb", BOARD_PX_PER_M))
        rows.append([g["x"], g["y"], z[base + j], z[base + j], -f[0], -f[1], w, h, mi, BOARD_MOUNT_M])
    if not rows:
        return None, {}
    if any(len(r) == 12 for r in rows):
        rows = [r if len(r) == 12 else r + [-1.0, 1.0] for r in rows]
    x0, y0 = box[0], box[1]
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray(rows, np.float64), [float(x0), float(y0), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces
