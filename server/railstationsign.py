"""Name boards, number plates and the hall's name of railway stations (task 037, stage G): made in the sign tile (signstream.
build_tile) with busstationsign's faces and the kernel's dirsign_mesh: one mesh, one set of pictures per tile.

    platform_spots(st, country)   [(x, y, fx, fy, kind, text, platform)] on platforms: the name board (a post at the middle of
                                  the track edge's longest run, its face toward the track, readable from the train) and the
                                  platform's number plate (a quarter of the way along)
    facade_spot(st)               (x, y, ux, uy, length) on the hall's wall nearest the platforms: the name over the door
    sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, country=None) -> (mesh, faces) or (None, {})

A dirsign_mesh row is [x, y, z, z, dx, dy, w, h, material, mount, ...] whose face looks AGAINST (dx, dy).
"""
import hashlib
import json
import os

import numpy as np

import busstations as bs
import busstationsign as bsg
import dirsigns
import fast
import railstationmesh as rm
import railstations as rs
import roads
import staging

KERNEL = 204
SUBTITLE = {"UA": "Залізнична станція", "RU": "Железнодорожная станция", "CZ": "Železniční stanice", "SK": "Železničná stanica",
            "PL": "Stacja kolejowa", "DE": "Bahnhof", "AT": "Bahnhof", "default": "Railway station"}
FACE_VERSION = 1
MIN_PLATFORM_M = 40.0           # a platform shorter than this has no name board
NAME_IN_M = 0.9                 # the board's post stands this far in from the track edge (the safety line is 1.0 m)
NAME_MOUNT_M = 2.0
PLATE_IN_M = 0.9
PLATE_AT = 0.25                 # the number plate's place along the run
MAX_BOARDS, MAX_PLATES = 24, 40
FACADE_MOUNT_M = 3.4
FACADE_CAP_M = 0.55


def _subtitle(country):
    return SUBTITLE.get(str(country or "").upper(), SUBTITLE["default"])


def _face(out_dir, vfs_dir, name, country):
    """(material, vfs path, width m, height m) of the name board (white on the usual station blue; the country's subtitle)."""
    sub = _subtitle(country)
    flat = [*bsg.BG, *bsg.INK, *bsg.EDGE, *bsg.BAR, bsg.CAP_M, bsg.EN_SHARE, bsg.MIN_W_M, bsg.MAX_W_M, bsg.INSET_M,
            bsg.LINE_W_M, bsg.BAR_W_M, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, name, sub, bsg.PX_PER_M], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    material = f"bne_rsign_{h}"
    png = os.path.join(out_dir, material + ".color.png")
    data, w_m, h_m = fast._ef.dirsign_town_face(name, sub, False, flat, bsg.PX_PER_M)
    if not os.path.exists(png):
        tmp = staging.tmp_for(png)
        with open(tmp, "wb") as f:
            f.write(bytes(data))
        staging.replace(tmp, png)
    return material, f"{vfs_dir}/{material}.color.png", w_m, h_m


def _runs_of(pl):
    """[(points (k, 2), length m)] the runs of the platform's track edge, longest first."""
    out = []
    for run in rm._runs(pl.snapped, pl.ring, pl.tidx):
        if len(run) < 3:
            continue
        pts = pl.ring[run]
        out.append((pts, float(np.hypot(*np.diff(pts, axis=0).T).sum())))
    return sorted(out, key=lambda r: -r[1])


def _at(pts, frac, inset):
    """(x, y, nx, ny) the point `frac` along a run, `inset` m in from the edge, and the inward unit normal there."""
    s = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))))
    at = s[-1] * frac
    x, y = np.interp(at, s, pts[:, 0]), np.interp(at, s, pts[:, 1])
    k = min(max(int(np.searchsorted(s, at)), 1), len(pts) - 1)
    d = pts[k] - pts[k - 1]
    dl = float(np.hypot(*d))
    t = d / dl if dl > 1e-9 else np.array([1.0, 0.0])
    n = np.array([-t[1], t[0]])                     # (the platform is to the left of its counter-clockwise ring)
    return float(x + n[0] * inset), float(y + n[1] * inset), float(n[0]), float(n[1])


def platform_spots(st, country=None):
    """[(x, y, fx, fy, kind, text, platform)]: kind "name" (text = the station's name) and "plate" (the platform's number);
    (fx, fy) the unit direction the face looks (to the track). Deterministic."""
    out = []
    for pl in st.platforms:
        runs = _runs_of(pl)
        if not runs or runs[0][1] < MIN_PLATFORM_M:
            continue
        pts, ln = runs[0]
        if st.name:
            x, y, nx, ny = _at(pts, 0.5, NAME_IN_M)
            if bs.contains(pl.ring, (x, y)):
                out.append((x, y, -nx, -ny, "name", st.name, pl))
        if pl.ref:
            x, y, nx, ny = _at(pts, PLATE_AT, PLATE_IN_M)
            if bs.contains(pl.ring, (x, y)):
                out.append((x, y, -nx, -ny, "plate", pl.ref.replace(";", "/"), pl))
    return out


def facade_spot(st):
    """(x, y, ux, uy, length) the middle of the hall's longest wall that looks to the nearest platform (ux, uy the outward unit
    normal), or None."""
    if not st.buildings or not st.platforms:
        return None
    hall = max(st.buildings, key=lambda b: bs.area(b[1]))
    ring = bs._open(np.asarray(hall[1], float))
    if bs.area(ring) < 150.0:
        return None
    ccw = ring if float(np.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - np.dot(ring[:, 1], np.roll(ring[:, 0], -1))) >= 0 else ring[::-1]
    pc = np.asarray([bs.centroid(pl.ring) for pl in st.platforms])
    best = None
    for a, b in zip(ccw, np.roll(ccw, -1, axis=0)):
        d = b - a
        ln = float(np.hypot(*d))
        if ln < 6.0:
            continue
        t = d / ln
        o = np.array([t[1], -t[0]])                 # (outward: the right of a counter-clockwise ring)
        mid = (a + b) / 2.0
        near = float(np.min(np.hypot(*(pc - mid).T)))
        facing = float(np.max((pc - mid) @ o))      # a platform lies on the wall's outer side
        if facing <= 0.0:
            continue
        score = near - 0.05 * ln
        if best is None or score < best[0]:
            best = (score, mid, o, ln)
    if best is None:
        return None
    _s, mid, o, ln = best
    return float(mid[0] + o[0] * 0.05), float(mid[1] + o[1] * 0.05), float(o[0]), float(o[1]), ln


def sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, country=None):
    """The boards, plates and halls' names whose spot is in road tile (tx, ty): (mesh, faces) in dirsigns.build_tile's form
    (vertices, triangles, uv, materials, material per triangle), or (None, {})."""
    if not rs.ON:
        return None, {}
    box = roads.tile_bbox(tx, ty)
    x0, y0, x1, y1 = box
    spots, facades = [], []
    for st in rs.stations_near(md, frame, box):
        c = country or st.country
        for sp in platform_spots(st, c):
            if x0 <= sp[0] < x1 and y0 <= sp[1] < y1:
                spots.append((st, c) + sp)
        f = facade_spot(st) if st.name else None
        if f is not None and x0 <= f[0] < x1 and y0 <= f[1] < y1:
            facades.append((st, c, f))
    if not spots and not facades:
        return None, {}
    fast.require(KERNEL, "rail station signs")
    dirsigns._set_fonts()
    boards = [s for s in spots if s[6] == "name"][:MAX_BOARDS]
    plates = [s for s in spots if s[6] == "plate"][:MAX_PLATES]
    materials, faces, rows = [dirsigns.METAL], {}, []
    pts = np.array([[s[2], s[3]] for s in boards + plates] + [[f[2][0], f[2][1]] for f in facades]).reshape(-1, 2)
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    hz = np.asarray(elev(la, lo), np.float64).ravel()
    gz = np.asarray(frame.geodetic_to_enu(la, lo, hz)[2], np.float64).ravel()
    for i, (st, c, x, y, fx, fy, kind, text, pl) in enumerate(boards + plates):
        top = float(rm.surface(frame, elev, st, pl)(np.array([[x, y]]))[0])
        if kind == "name":
            name, tex, w, h = _face(out_dir, vfs_dir, text, c)
            mount = NAME_MOUNT_M
        else:
            name, tex, w, h = bsg._plate_face(out_dir, vfs_dir, text, bsg.plate_dark(c))
            mount = bsg.PLATE_MOUNT_M
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        rows.append([x, y, top, top, -fx, -fy, w, h, materials.index(name), mount])        # (the face looks against the row)
    base = len(boards) + len(plates)
    for j, (st, c, (x, y, ux, uy, ln)) in enumerate(facades):
        name, tex, w, h = _face(out_dir, vfs_dir, st.name, c)
        if w > min(ln - 1.0, 9.0):
            continue
        if name not in faces:
            faces[name] = tex
            materials.append(name)
        ex, ey = -uy, ux
        rows.append([x + ex * (w / 2.0 - 0.2), y + ey * (w / 2.0 - 0.2), gz[base + j], gz[base + j], -ux, -uy, w, h,
                     materials.index(name), FACADE_MOUNT_M, 0.0, 1.0])
    if not rows:
        return None, {}
    if any(len(r) == 12 for r in rows):
        rows = [r if len(r) == 12 else r + [-1.0, 1.0] for r in rows]
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray(rows, np.float64), [float(x0), float(y0), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces
