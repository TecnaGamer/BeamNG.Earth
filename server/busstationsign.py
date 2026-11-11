"""The name sign of a bus station (task 020, the user 2026-11-06: "generate a nice sign with the name"): a blue board on
two posts at the yard's edge by the nearest street, its face toward the street: the station's name in white (any script:
the kernel's fonts cover Latin with accents and Cyrillic) and "Bus station" under it.

Made in the sign tile (signstream.build_tile) beside the direction boards and the town signs: one mesh, one set of
pictures (the tile's "dirs_faces", the mod makes their Materials: dirsigns.lua). The picture and the panel with its posts
are the kernel's (earthfast.dirsign_town_face, dirsign_mesh), as the town signs'.

    sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir) -> (mesh, faces) or (None, {})
"""
import hashlib
import json
import logging
import os

import numpy as np

import busstations
import dirsigns
import fast
import roads
import staging

log = logging.getLogger("earthd.busstations")

KERNEL = 204                    # earthfast VERSION with dirsign_town_face / dirsign_mesh
FACE_VERSION = 1
PX_PER_M = 200.0
BG, INK, EDGE, BAR = (0, 74, 153), (255, 255, 255), (255, 255, 255), (0, 0, 0)    # blue, white (the usual station blue)
CAP_M = 0.34                    # the name's cap height
EN_SHARE = 0.55                 # the English line's size against it
MIN_W_M, MAX_W_M = 2.6, 5.6
INSET_M, LINE_W_M, BAR_W_M = 0.03, 0.02, 0.07
MOUNT_M = 2.2                   # the board's bottom over the ground
SUBTITLE = "Bus station"
MAX_SIGNS = 6
BACK_GAP_M = 0.054               # the second face's offset: board A's back slab is 0.05 m deep
# the bays' number plates (the user, 2026-11-06: "numbers as posts, like signs": painted digits floated or vanished)
PLATE_CAP_M = 0.34
PLATE_MIN_W_M, PLATE_MAX_W_M = 0.5, 1.1
PLATE_MOUNT_M = 1.5             # the plate's bottom over the platform (dirsign_mesh keeps it at 1.2 m at least)
PLATE_IN_M = 0.6                # the post stands this far in from the platform's edge
PLATE_ALONG_M = 1.2             # ... and this far from the start of its bay
SLAB_H_M = 0.18                 # the platform slab's height (busstationmesh.PLATFORM_H_M)
MAX_PLATES = 40                 # plates a tile makes at most (a Material per label)


def _face(out_dir, vfs_dir, name):
    """(material, vfs path of its picture, width m, height m): drawn once (named by its hash)."""
    flat = [*BG, *INK, *EDGE, *BAR, CAP_M, EN_SHARE, MIN_W_M, MAX_W_M, INSET_M, LINE_W_M, BAR_W_M, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, name, SUBTITLE, PX_PER_M], ensure_ascii=False)
                     .encode("utf-8")).hexdigest()[:12]
    material = f"bne_bsign_{h}"
    png = os.path.join(out_dir, material + ".color.png")
    data, w_m, h_m = fast._ef.dirsign_town_face(name, SUBTITLE, False, flat, PX_PER_M)
    if not os.path.exists(png):
        tmp = staging.tmp_for(png)
        with open(tmp, "wb") as f:
            f.write(bytes(data))
        staging.replace(tmp, png)
    return material, f"{vfs_dir}/{material}.color.png", w_m, h_m


DARK_BG = (36, 40, 46)          # the EU stations' dark totem with a white number (Náchod's, Hradec's)
DARK_CAP_M = 0.44
BLUE_COUNTRIES = {"UA", "RU", "BY", "KZ", "MD", "GE", "AM", "AZ", "UZ", "KG", "TJ", "TM"}   # the post-Soviet blue plate


def plate_dark(country):
    """Does a station in this country (ISO code, "" or None unknown) have the EU totem plate (dark, white number) rather
    than the post-Soviet blue one? Unknown country: blue."""
    code = str(country or "").strip().upper()
    return bool(code) and code not in BLUE_COUNTRIES


def _plate_face(out_dir, vfs_dir, label, dark=False):
    """(material, vfs path, width m, height m) of a bay's number plate: a Material per label and style, shared by every
    station."""
    bg, cap = (DARK_BG, DARK_CAP_M) if dark else (BG, PLATE_CAP_M)
    flat = [*bg, *INK, *EDGE, *BAR, cap, EN_SHARE, PLATE_MIN_W_M, PLATE_MAX_W_M, 0.02, 0.015, BAR_W_M, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, label, PX_PER_M], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    material = f"bne_bnum_{h}"
    png = os.path.join(out_dir, material + ".color.png")
    data, w_m, h_m = fast._ef.dirsign_town_face(label, "", False, flat, PX_PER_M)
    if not os.path.exists(png):
        tmp = staging.tmp_for(png)
        with open(tmp, "wb") as f:
            f.write(bytes(data))
        staging.replace(tmp, png)
    return material, f"{vfs_dir}/{material}.color.png", w_m, h_m


BOARD_W_M = 1.5                # the departures board's width (every row the same)
BOARD_CAP_M = 0.13
BOARD_ROWS = 5
BOARD_TITLE = {"CZ": "Odjezdy", "SK": "Odchody", "PL": "Odjazdy", "DE": "Abfahrt", "AT": "Abfahrt", "CH": "Abfahrt",
               "HU": "Indulás", "UA": "Відправлення", "SI": "Odhodi", "HR": "Polasci", "NL": "Vertrek"}
FACADE_CAP_M = 0.55            # the name on the hall's wall
FACADE_MOUNT_M = 3.4           # its bottom over the ground (over the door)


def _dark_face(out_dir, vfs_dir, text, sub, cap, w_m, prefix):
    """(material, vfs path, width, height) of a dark panel with white text (the board's rows), width fixed to w_m."""
    flat = [*DARK_BG, *INK, *DARK_BG, *DARK_BG, cap, EN_SHARE, w_m, w_m, 0.02, 0.0, 0.0, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, text, sub, PX_PER_M], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    material = f"{prefix}_{h}"
    png = os.path.join(out_dir, material + ".color.png")
    data, wm, hm = fast._ef.dirsign_town_face(text, sub, False, flat, PX_PER_M)
    if not os.path.exists(png):
        tmp = staging.tmp_for(png)
        with open(tmp, "wb") as f:
            f.write(bytes(data))
        staging.replace(tmp, png)
    return material, f"{vfs_dir}/{material}.color.png", wm, hm


def board_rows(st, country=None):
    """[(text, sub)] of the departures board, top first: the title (the country's word, "Departures" under it) and
    BOARD_ROWS departures: a time and a bay, in the station's own order (a hash of it: the same board in every tile)."""
    import busbays
    code = str(country or "").upper()
    title = BOARD_TITLE.get(code, "Departures")
    rows = [(title, "Departures" if title != "Departures" else "")]
    labels = [b.label for b in busbays.layout_bays(st) if b.label] or ["1"]
    seed = int(hashlib.sha1(str(st.fid).encode()).hexdigest()[:8], 16)
    minute = 6 * 60 + seed % 50
    for k in range(BOARD_ROWS):
        minute += 7 + (seed >> (k * 3)) % 18
        rows.append((f"{minute // 60:02d}:{minute % 60:02d}   {labels[(seed + k * 7 + (seed >> (k + 5)) % 3) % len(labels)]}", ""))
    return rows


def plate_spots(st, country=None):
    """[(x, y, fx, fy, label, dark)]: where each labelled bay's post stands and the unit direction its plate faces (toward the
    bus lane): at the platform's edge, near the start of the bay."""
    import busbays
    out = []
    for bay in busbays.layout_bays(st):
        if not bay.label:
            continue
        kerb = bay.c - bay.n * (bay.width / 2.0 + busbays.KERB_GAP_M)
        for inset in (PLATE_IN_M, PLATE_IN_M + 0.5, PLATE_IN_M + 1.2):       # further in when a lane runs by the kerb
            p = kerb - bay.n * inset - bay.t * (bay.length / 2.0 - PLATE_ALONG_M)
            if not ((st.lanes or st.mouths) and bool(busstations.lane_blocked(st, p[None], pad=-1.0, core=True,
                                                                              mouths=False)[0])):
                out.append((float(p[0]), float(p[1]), float(bay.n[0]), float(bay.n[1]), bay.label, plate_dark(country)))
                break
    return out


def sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, country=None):
    """The name signs of the stations whose sign spot is in road tile (tx, ty): (mesh, faces) in dirsigns.build_tile's
    form (vertices, triangles, uv, materials, material per triangle), or (None, {})."""
    if not busstations.ON:
        return None, {}
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    keep, plates, boards, facades = [], [], [], []
    for st in busstations.stations_near(md, frame, box):
        spot = busstations.sign_spot(st) if st.name else None
        if spot is not None and x0 <= spot[0] < x1 and y0 <= spot[1] < y1:
            keep.append((st, spot))
        b = busstations.board_spot(st)
        if b is not None and x0 <= b[0] < x1 and y0 <= b[1] < y1:
            boards.append((st, b))
        f = busstations.facade_spot(st) if st.name else None
        if f is not None and x0 <= f[0] < x1 and y0 <= f[1] < y1:
            facades.append((st, f))
        plates += [s for s in plate_spots(st, country) if x0 <= s[0] < x1 and y0 <= s[1] < y1]
    if not keep and not plates and not boards and not facades:
        return None, {}
    fast.require(KERNEL, "bus station signs")
    dirsigns._set_fonts()
    keep, plates = keep[:MAX_SIGNS], plates[:MAX_PLATES]
    materials, faces, rows = [dirsigns.METAL], {}, []
    pts = np.array([[s[0], s[1]] for _, s in keep] + [[p[0], p[1]] for p in plates]
                   + [[b[0], b[1]] for _, b in boards] + [[f[0], f[1]] for _, f in facades]).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    hz = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, hz)[2], np.float64).ravel()
    for i, (st, (x, y, ux, uy)) in enumerate(keep):
        name, tex, w, h = _face(out_dir, vfs_dir, st.name)
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        zs = z[i] + SLAB_H_M                    # (on the bench's spot: a platform's slab, busstations.sign_spot)
        rows.append([x, y, zs, zs, ux, uy, w, h, materials.index(name), MOUNT_M])
        if busstations.sign_both_sides(st, (x, y, ux, uy)):
            # a road on the far side too: the same name on the back, flush against the first board's back (its slab is 0.05 m)
            rows.append([x + ux * BACK_GAP_M, y + uy * BACK_GAP_M, zs, zs, -ux, -uy, w, h, materials.index(name), MOUNT_M])
    for j, (x, y, fx, fy, label, dark) in enumerate(plates):
        name, tex, w, h = _plate_face(out_dir, vfs_dir, label, dark)
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        zz = z[len(keep) + j] + SLAB_H_M
        rows.append([x, y, zz, zz, -fx, -fy, w, h, materials.index(name), PLATE_MOUNT_M])    # (a sign faces against t)
    base = len(keep) + len(plates)
    for j, (st, (x, y, ux, uy)) in enumerate(boards):
        zz = z[base + j] + SLAB_H_M
        mount = 1.2
        for text, sub_ in reversed(board_rows(st, country)):        # (bottom row first, each one over the last)
            name, tex, w, h = _dark_face(out_dir, vfs_dir, text, sub_, BOARD_CAP_M * (1.5 if sub_ else 1.0), BOARD_W_M,
                                         "bne_bboard")
            if name not in faces:
                faces[name] = tex
                materials.append(name)
            rows.append([x, y, zz, zz, -ux, -uy, w, h, materials.index(name), mount])     # (faces away from the hall)
            mount += h + 0.02
    base += len(boards)
    for j, (st, (x, y, ux, uy, ln)) in enumerate(facades):
        zz = z[base + j]
        w_max = min(ln - 1.0, 9.0)
        name, tex, w, h = _face(out_dir, vfs_dir, st.name)
        if w > w_max:
            continue
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        # on the wall: hung by clamps (dirsign_mesh mode 0) at its left end, the board reaching along the wall
        ex, ey = -uy, ux                                # (along the wall)
        ax, ay = x + ex * (w / 2.0 - 0.2), y + ey * (w / 2.0 - 0.2)
        rows.append([ax, ay, zz, zz, ux, uy, w, h, materials.index(name), FACADE_MOUNT_M, 0.0, 1.0])
    if any(len(r) == 12 for r in rows):
        rows = [r if len(r) == 12 else r + [-1.0, 1.0] for r in rows]
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray(rows, np.float64), [float(x0), float(y0), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces


def merge(a, b):
    """One sign mesh (vertices, triangles, uv, materials, material per triangle) of two that share the metal's name, or
    whichever of them exists (the direction boards' and the stations' name signs in one signs part)."""
    if a is None:
        return b
    if b is None:
        return a
    pa, ta, uva, ma, mia = a
    pb, tb, uvb, mb, mib = b
    mats = list(ma)
    remap = []
    for name in mb:
        if name not in mats:
            mats.append(name)
        remap.append(mats.index(name))
    mib = np.asarray(remap, np.int64)[np.asarray(mib, np.int64)]
    return (np.vstack([pa, pb]), np.vstack([ta, np.asarray(tb) + len(pa)]), np.vstack([uva, uvb]), mats,
            np.concatenate([np.asarray(mia, np.int64), mib]))
