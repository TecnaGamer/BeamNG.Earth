"""Sweden's bus stop signs (the user, 2026-11-16): at every mapped bus stop a pole at the kerb with the stop's sign, a
flag across the pavement read from both ways: E22 Busshållplats (TSFS 2019:74, blue with the bus on a white field) and
under it a white plate with the stop's name, its "Läge" (OpenStreetMap's local_ref: the stop's letter) under the name
where one is mapped. Of Sweden's 58,504 mapped stops 97.7 % have a name and 3.0 % a local_ref (Overpass, 2026-11-16).

Made in the sign tile (signstream.build_tile) beside the bus stations' name signs: one mesh, one set of pictures (the
tile's "dirs_faces", the mod makes their Materials: dirsigns.lua). The name's picture and the boards with their pole are
the kernel's (earthfast.dirsign_town_face, dirsign_mesh: a board on its own stand, twice, back to back); E22 is drawn
here. A stop is the same one the shelters stand at (shelters.py), a stop tagged shelter=no too (a pole only), and
the node on the carriageway where the bus halts (public_transport=stop_position with highway=bus_stop): most of
Sweden's town stops are mapped so, 686 of 714 in Göteborg (Overture 2026-10-21), their letter in ref. Such a node says
no side: its pole stands on the side of the nearest mapped platform, else on the right of a one-way carriageway, else
on a side picked by the stop's id.

    sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, context, decks, country) -> (mesh, faces) or (None, {})

Tile independence: a tile makes the signs of the stops whose mapped point is in its box, each beside the carriageway
nearest that point (the roads within signcontext.MARGIN_M: the same from every tile).
"""
import hashlib
import io
import json
import math
import os
from types import SimpleNamespace

import numpy as np

import dirsigns
import dnp
import exitsigns
import fast
import imagery
import mapcells
import mapgeom
import roads
import shelters

COUNTRIES = {"SE"}
KERNEL = 204                    # earthfast VERSION with dirsign_town_face / dirsign_mesh
FACE_VERSION = 1
PX_PER_M = 400.0                # (a stop's name is small: 30 px of cap height)
BLUE, WHITE, BLACK = (0, 90, 166), (255, 255, 255), (16, 16, 16)
E22_M = 0.45                    # E22's side at a stop (the small size)
E22_PX = 256
CAP_M = 0.075                   # the name's cap height
SUB_SHARE = 0.6                 # the Läge line's size against it
MIN_W_M, MAX_W_M = E22_M, 1.3
INSET_M, LINE_W_M = 0.008, 0.004
MOUNT_M = 2.1                   # the name plate's bottom over the ground (TSFS 1 kap. 13 §: 2.0 m over a pavement)
GAP_M = 0.03                    # between the plate and E22 over it
KERB_M = 0.9                    # the pole past the carriageway's edge
AHEAD_M = 2.3                   # ... and past the stop's point the way the bus goes: the head of the stop, clear of a shelter
DECK_M = 1.5
ON_ROAD_M = 1.0                 # a stop's point this near the centre line says no side (shelters._place's)
PLATFORM_M = 20.0               # a platform within this of such a stop says its side
SAME_M = 25.0
LAGE_MAX = 2                    # a ref this short is the stop's letter ("A", "B2"); a longer one is its number
MAX_NAMES = 30                  # name plates a tile makes at most (a Material per name); the stops past them keep E22
MAX_NAME_LEN = 40
MODE_STAND = 1.0                # dirsign_mesh: a board on its own stand, reaching out to one side of it


def wanted(country):
    return str(country or "").upper() in COUNTRIES


def is_stop(feats, i):
    """A bus stop: beside the road or the node on it where the bus halts (not a shelter of its own, nor a tram's or a
    train's stop position)."""
    tags = shelters._tags(feats, i)
    return shelters._cls(feats, i) == "bus_stop" or tags.get("highway", "") == "bus_stop" \
        or tags.get("amenity", "") == "bus_stop"


def _text(value):
    s = "" if value is None else str(value).strip()
    return "" if s.lower() in ("", "none", "nan") else " ".join(s.split())[:MAX_NAME_LEN]


def label(feats, i):
    """(name, läge) of stop i: the mapped name ("" none) and its letter ("" none): local_ref, else a ref of LAGE_MAX
    characters at most (tags are kept lower-case: a letter is written as a capital)."""
    names = feats.attrs.get("name")
    name = _text(names[i]) if names is not None else ""
    tags = shelters._tags(feats, i)
    ref = _text(tags.get("local_ref", "")) or _text(tags.get("ref", ""))
    return name, (ref.upper() if len(ref) <= LAGE_MAX and ref.isalnum() else "")


def e22_picture(px=E22_PX):
    """E22 as (px, px, 3) uint8: the blue field, its white edge line, the white square and the bus in it (from the
    side: body, windows, wheels). Drawn four times as large and averaged."""
    n = px * 4
    y, x = np.mgrid[0:n, 0:n]
    x, y = (x + 0.5) / n, (y + 0.5) / n
    img = np.empty((n, n, 3), np.float64)
    img[:] = BLUE

    def box(x0, y0, x1, y1):
        return (x >= x0) & (x < x1) & (y >= y0) & (y < y1)

    def disc(cx, cy, r):
        return (x - cx) * (x - cx) + (y - cy) * (y - cy) < r * r

    img[box(0.03, 0.03, 0.97, 0.97) & ~box(0.055, 0.055, 0.945, 0.945)] = WHITE
    img[box(0.17, 0.17, 0.83, 0.83)] = WHITE
    body = box(0.25, 0.36, 0.75, 0.615) & ~(box(0.25, 0.36, 0.27, 0.38) & ~disc(0.27, 0.38, 0.02)) \
        & ~(box(0.73, 0.36, 0.75, 0.38) & ~disc(0.73, 0.38, 0.02))
    img[body | box(0.24, 0.585, 0.76, 0.615)] = BLACK
    for k in range(5):                              # the windows, the door's last
        x0 = 0.275 + k * 0.092
        img[box(x0, 0.395, x0 + 0.074, 0.49 if k < 4 else 0.575)] = WHITE
    for cx in (0.355, 0.645):                       # the wheels in their arches
        img[disc(cx, 0.615, 0.062)] = WHITE
        img[disc(cx, 0.622, 0.047)] = BLACK
        img[disc(cx, 0.622, 0.018)] = WHITE
    img[box(0.17, 0.67, 0.83, 0.83)] = WHITE          # (nothing of an arch under the bus)
    out = img.reshape(px, 4, px, 4, 3).mean(axis=(1, 3))
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _png(rgb):
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _e22_face(out_dir, vfs_dir):
    """(material, vfs path of its picture): one for every stop."""
    h = hashlib.sha1(json.dumps([FACE_VERSION, BLUE, WHITE, BLACK, E22_PX]).encode("utf-8")).hexdigest()[:12]
    material = f"bne_sestop_e22_{h}"
    path = os.path.join(out_dir, material + ".color.dds")
    if not os.path.exists(path):
        imagery.write_picture_dds(path, _png(e22_picture()))    # (a DDS, never a PNG the engine cooks)
    return material, f"{vfs_dir}/{material}.color.dds"


def _name_face(out_dir, vfs_dir, name, ref):
    """(material, vfs path, width m, height m) of a stop's name plate: drawn once (named by its hash)."""
    sub = f"Läge {ref}" if ref else ""
    flat = [*WHITE, *BLACK, *BLACK, *BLACK, CAP_M, SUB_SHARE, MIN_W_M, MAX_W_M, INSET_M, LINE_W_M, 0.0, 0.0, 0.0]
    h = hashlib.sha1(json.dumps([FACE_VERSION, flat, name, sub, PX_PER_M], ensure_ascii=False)
                     .encode("utf-8")).hexdigest()[:12]
    material = f"bne_sestop_{h}"
    path = os.path.join(out_dir, material + ".color.dds")
    data, w_m, h_m = fast._ef.dirsign_town_face(name, sub, False, flat, PX_PER_M)
    if not os.path.exists(path):
        imagery.write_picture_dds(path, data)
    return material, f"{vfs_dir}/{material}.color.dds", w_m, h_m


def _to_lines(x, y, lines):
    """(distance, nearest x, y) from (x, y) to the nearest of the polylines `lines` ((k, 2) each), or None."""
    best = None
    for pts in lines:
        if len(pts) == 1:
            c = pts[0]
            d = math.hypot(float(c[0]) - x, float(c[1]) - y)
        else:
            a, ab = pts[:-1], pts[1:] - pts[:-1]
            t = np.clip(((x - a[:, 0]) * ab[:, 0] + (y - a[:, 1]) * ab[:, 1])
                        / np.maximum(ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1], 1e-9), 0.0, 1.0)
            q = a + t[:, None] * ab
            dd = dnp.hypot(q[:, 0] - x, q[:, 1] - y)
            k = int(np.argmin(dd))
            c, d = q[k], float(dd[k])
        if best is None or d < best[0]:
            best = (d, float(c[0]), float(c[1]))
    return best


def pole(x, y, near, platforms=(), fid=""):
    """(x, y, travel x, travel y) of the pole of the stop at (x, y): KERB_M past the edge of the nearest carriageway
    (near: shelters' roads, a signroads.Spec or a width each) on the stop's side, where the traffic of that side goes
    (travel x, y: right-hand traffic); None with no carriageway within shelters.NEAR_ROAD_M. A point beside the road
    has its pole AHEAD_M on (the head of the stop, clear of a shelter). A point on the road (within ON_ROAD_M of the
    centre line) has it beside itself: on the side of the nearest of platforms ((k, 2) lines) within PLATFORM_M, else
    on the right of a one-way road, else on a side by fid."""
    hit = shelters._closest(near, x, y)
    if hit is None:
        return None
    cx, cy, tx, ty, half = hit
    side = (x - cx) * -ty + (y - cy) * tx           # (> 0: left of the line's direction)
    ahead = AHEAD_M
    if abs(side) < ON_ROAD_M:
        ahead = 0.0
        got = _to_lines(x, y, platforms)
        if got is not None and got[0] <= PLATFORM_M and abs((got[1] - cx) * -ty + (got[2] - cy) * tx) > 0.5:
            side = (got[1] - cx) * -ty + (got[2] - cy) * tx
        else:
            spec = next((r[1] for r in near if _on(r, cx, cy)), None)
            if getattr(spec, "oneway", False):
                side = -float(int(getattr(spec, "dir", 0) or 1))    # (the right of its traffic)
            else:
                side = shelters._side(fid)
    s = 1.0 if side >= 0.0 else -1.0
    ax, ay = -ty * s, tx * s                        # away from the carriageway
    gx, gy = -ay, ax                                # the traffic's way there: away is on its right
    d = half + KERB_M
    return cx + ax * d + gx * ahead, cy + ay * d + gy * ahead, gx, gy


def _on(road, x, y):
    got = _to_lines(x, y, [np.asarray(road[2], np.float64)[:, :2]])
    return got is not None and got[0] < 0.01


def stops(md, frame, box):
    """([(fid, x, y, name, läge)] of the bus stops whose point is in box, by id; the platforms near it, (k, 2) lines)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    m = PLATFORM_M + ON_ROAD_M
    la, lo, _ = frame.enu_to_geodetic(np.array([x0 - m, x0 - m, x1 + m, x1 + m]),
                                      np.array([y0 - m, y1 + m, y0 - m, y1 + m]), np.zeros(4))
    out, platforms, seen = {}, [], set()
    for key in mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max())):
        feats = md.cell(key, layers=("infrastructure",), kind="prefetch").layers.get("infrastructure")
        if feats is None or not len(feats) or feats.attrs.get("class") is None and feats.attrs.get("tags") is None:
            continue
        cls = feats.attrs.get("class")
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            b = boxes[i]
            if b[2] < x0 - m or b[0] >= x1 + m or b[3] < y0 - m or b[1] >= y1 + m:
                continue
            fid = str(feats.ids[i])
            if cls is not None and str(cls[i]) == "platform":
                if fid not in seen:
                    seen.add(fid)
                    for part in feats.parts(i):
                        for ring in part:
                            e, n = mapgeom.enu_xy(feats, frame, np.asarray(ring, np.float64))
                            if len(e):
                                platforms.append(np.column_stack((e, n)))
                continue
            if int(feats.kind[i]) != mapgeom.POINT or fid in out or not is_stop(feats, i):
                continue
            spot = shelters._spot(feats, frame, i)
            if spot is not None and x0 <= spot[0] < x1 and y0 <= spot[1] < y1:
                out[fid] = (fid, spot[0], spot[1], *label(feats, i))
    return [out[k] for k in sorted(out)], platforms


def rows_of(found, near, decks=(), platforms=()):
    """[(x, y, travel x, travel y, name, läge)] of the poles of stops() beside the roads near, none on a bridge's deck,
    one for the stops of one name and letter on one side within SAME_M (a stop mapped as its pole and as its halt)."""
    out = []
    for fid, x, y, name, ref in found:
        got = pole(x, y, near, platforms, fid)
        if got is None or exitsigns._on_deck(np.array(got[:2]), decks, DECK_M):
            continue
        if any(o[4:] == (name, ref) and o[2] * got[2] + o[3] * got[3] > 0.0
               and math.hypot(o[0] - got[0], o[1] - got[1]) < SAME_M for o in out):
            continue
        out.append((*got, name, ref))
    return out


def sign_tile(md, frame, tx, ty, elev, out_dir, vfs_dir, context, decks=(), country=None, left_hand=False):
    """The stop signs of road tile (tx, ty): (mesh, faces) in dirsigns.build_tile's form (vertices, triangles, uv,
    materials, material per triangle), or (None, {})."""
    if not wanted(country) or left_hand:
        return None, {}
    box = roads.tile_bbox(tx, ty)
    found, platforms = stops(md, frame, box)
    if not found:
        return None, {}
    import busstationkeep               # (a bus station has its own plates)
    keep = busstationkeep.keepout(md, frame, box)
    if keep:
        inside = keep.contains(np.array([[f[1], f[2]] for f in found], np.float64))
        found = [f for f, drop in zip(found, inside) if not drop]
        if not found:
            return None, {}
    pts = np.array([[f[1], f[2]] for f in found], np.float64)
    la, lo, _ = frame.enu_to_geodetic(pts[:, 0], pts[:, 1], 0.0)
    near = [(r[0], SimpleNamespace(width=float(r[3]), oneway=bool(getattr(r[1], "oneway", False)),
                                   dir=getattr(r[1], "dir", 0)), r[2])
            for r in context.pavements(frame, np.c_[lo, la], left_hand)]
    poles = rows_of(found, near, decks, platforms)
    if not poles:
        return None, {}
    fast.require(KERNEL, "bus stop signs")
    dirsigns._set_fonts()
    xy = np.array([[p[0], p[1]] for p in poles], np.float64)
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
    hz = np.asarray(elev(la, lo), np.float64).ravel()
    z = np.asarray(frame.geodetic_to_enu(la, lo, hz)[2], np.float64).ravel()
    e22, e22_tex = _e22_face(out_dir, vfs_dir)
    materials, faces, rows, named = [dirsigns.METAL, e22], {e22: e22_tex}, [], set()
    for (x, y, gx, gy, name, ref), zz in zip(poles, z):
        top = MOUNT_M
        if name and ((name, ref) in named or len(named) < MAX_NAMES):
            named.add((name, ref))
            mat, tex, w, h = _name_face(out_dir, vfs_dir, name, ref)
            if mat not in faces:
                faces[mat] = tex
                materials.append(mat)
            _both(rows, x, y, zz, gx, gy, w, h, materials.index(mat), top)
            top += h + GAP_M
        _both(rows, x, y, zz, gx, gy, E22_M, E22_M, 1, top)
    p, t, uv, mi = fast._ef.dirsign_mesh(np.ascontiguousarray(rows, np.float64), [float(box[0]), float(box[1]), 0.0])
    if not len(t):
        return None, {}
    return (p, t, uv, materials, mi), faces


def _both(rows, x, y, z, gx, gy, w, h, material, mount):
    """A board on the stand at (x, y) and the same on its back: the first met by the traffic going (gx, gy), reaching
    to its right (away from the road), the second from the other way, over the same span."""
    rows.append([x, y, z, z, gx, gy, w, h, material, mount, MODE_STAND, 1.0])
    rows.append([x, y, z, z, -gx, -gy, w, h, material, mount, MODE_STAND, -1.0])
