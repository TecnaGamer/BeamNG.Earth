#!/usr/bin/env python3
"""A 3D debug viewer for BeamNG.Earth's terrain and meshes around a point: shaded pictures in debug colours.

For ground and structure problems (a road under the ground, a deck without its approach, a tunnel hole with no tube, a building
on a road): Earth's own code builds, offline and unchanged (the Earth tree is only imported, never modified), the terrain block
that holds the point and the anchor's road tiles that cover the radius, and the tool draws what they hold, every surface lit by
one directional (Lambert) light and a little ambient light, in flat colours:

  brown       the ground (the block's 1 m height lattice, cropped to the radius; the .ter's tunnel holes are left open)
  magenta     ground the EarthRally grader moves (--rally; the picture then shows the graded ground)
  pink        roads (the tile's road mesh); a deeper pink: bridge decks (its deck mesh)
  steel grey  railways (rail mesh: ballast, sleepers, rails)
  white       buildings
  blue-grey   tunnel tubes and portals
  green       sidewalks;  orange: median barriers   (--kinds adds the rest: signs, wires, the collision meshes)
  dark line   the stage line (--geometry, repeatable); blue needle: the point
  cyan        reference trucks, 4 m high (every TRUCK_EVERY_M along every road of Earth's map, on the road or deck mesh
              under it (vehicles_on_lines); on a road with a height limit
              (the map's maxheight, Earth's roads.height_limit_lines) as high as the sign, a pickup under PICKUP_BELOW_M), and
              violet: reference wagons, 4.3 m high, nose to tail along every railway: what clears an underpass and what
              doesn't shows (--no-vehicles: none). A ground road's truck stands on the terrain (Earth paints those roads
              into it), a bridge's on its deck. A black post where the map has a bridge and no deck is drawn over the
              terrain, or a railway and no track: a structure Earth left out
  yellow      a truck or wagon whose body (0.5 m over its wheels to its roof) hits a road, deck, track or tunnel tube that
              isn't its own (earth_checks.vehicle_hits: from the meshes, exact)
  red         where two of the map's lines cross at different levels, the meshes of one cutting through the other's: a deck
              through a track, a track through a road's ground (earth_checks.penetrations); also in the overview
  The counts of both are in the log and, with --dump, in <name>_checks.json (server/evals/earth_eval.py compares them)

The meshes are the ones Earth writes for the game: each tile's .cdae files are read back (Earth's cdae.read), so what is drawn is what
the game would load, closed solids included. A picture is an orthographic camera, rasterised by a numpy z-buffer (headless, no GPU):

  <name>.png                every view in one picture, two per row, three for five or six views (one snapshot per point, for
                            reading by a person or an LLM):
                            overview  the area in 3D, 1 km x 1 km (OVERVIEW_M each side of the point), always from the
                                      south-west OVERVIEW_ELEV degrees above the horizon: the ground (every OVERVIEW_STEP m)
                                      and the meshes of every road tile it covers (built with it), the close-up's box as a
                                      dark line on the ground; with --compare-data the ground that rose or fell since
                                      before in red / blue
                            top    straight down, north up
                            <dir>  oblique (--elev degrees above the horizon), the camera on the compass side <dir> looking at
                                   the point (--views overview,top,SW,SE,N by default: any of N NE E SE S SW W NW or a bearing)
  <name>_overview.png, <name>_top.png, <name>_from_<dir>.png   each view on its own as well (--separate)
  <name>_panel_<view>.png, <name>_surface.npz   (--dump, for server/evals/earth_eval.py) each view's map as drawn in the picture,
                            and the close-up's surface seen from straight above every SURFACE_STEP_M: its class and height
                            (surface_grid)

A picture has a title (the point, the radius, the Earth tree), a legend of the colours in it, and on each view its name, a
scale bar and a north arrow. The default radius (40 m) frames a crossing, a deck end or a portal close up; --radius 120 for the
place around it.

  python server/evals/earth_view.py --earth . --anchor 42.17,13.83 --aid 2 --xy -446,880 \
      --geometry server/evals/geometry/ss1.json --out view-out
  python server/evals/earth_view.py --earth . --anchor 42.17,13.83 --aid 2 --at 42.1743502,13.8243183 --out view-out --rally \
      --geometry server/evals/geometry/ss1.json

A block takes minutes to build (a 1024 m square, as the server makes it), a road tile ~15 s: --reuse draws again from the data a
former run left in <out>/data (a new camera, radius, kinds) without building. Left out: the props (trees, poles, signs), the wires,
a geotile's far meshes, the game's runtime colliders; a building is the tile's own mesh, as the game draws it near the car.
"""
import argparse
import json
import math
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import earth_checks  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HM_ZOOM = 16
BLOCK_STEP = 128            # a block's centre is a multiple of this (m)
TILE = 256.0                # roads.TILE: an anchor road tile's side (m)

# ---- classes: what a triangle is, its debug colour, its name in the legend ---------------------------------------------------
GROUND, MOVED, ROAD, DECK, RAIL, BUILDING, TUNNEL, WALK, BARRIER, OTHER, TRUCK, WAGON, MISSING, ROSE, FELL, HIT, CUT = range(17)
CLASS_NAME = ["ground", "ground moved by the rally grader", "road", "bridge deck", "rail", "building", "tunnel tube / portal",
              "sidewalk", "median barrier", "other mesh (--kinds)", "reference truck, 4 m high (a signed limit: its height)",
              "reference wagon, 4.3 m high", "the map's bridge with no deck, or railway with no track, drawn",
              "ground risen since before", "ground fallen since before",
              "a truck or wagon whose body hits a road, deck, track or tube not its own (earth_checks)",
              "road / rail meshes cutting each other where the map has them at different levels (earth_checks)"]
CLASS_RGB = np.array([
    (0.55, 0.38, 0.22),     # ground: brown
    (1.00, 0.00, 1.00),     # moved by the grader: magenta
    (1.00, 0.45, 0.72),     # road: pink
    (0.90, 0.22, 0.52),     # deck: deeper pink
    (0.40, 0.38, 0.46),     # rail: steel grey
    (1.00, 1.00, 1.00),     # building: white
    (0.42, 0.62, 0.80),     # tunnel: blue-grey
    (0.50, 0.80, 0.42),     # sidewalk: green
    (1.00, 0.55, 0.10),     # barrier: orange
    (0.70, 0.55, 0.90),     # anything else: lilac
    (0.10, 0.80, 0.85),     # reference truck: cyan
    (0.45, 0.25, 0.75),     # reference wagon: violet
    (0.08, 0.08, 0.08),     # a map line with nothing under it: black posts
    (0.92, 0.30, 0.20),     # ground that rose since before: red
    (0.25, 0.45, 0.95),     # ground that fell since before: blue
    (1.00, 0.93, 0.00),     # a vehicle hitting what isn't its own: yellow
    (1.00, 0.00, 0.00),     # meshes cutting each other at a crossing of different levels: red
])
CHECK_CLASSES = {"ground": (0, 1), "deck": 3, "rail": 4, "tunnel": 6}     # (earth_checks' names for GROUND/MOVED, DECK, RAIL, TUNNEL)
CHECKS = dict(CHECK_CLASSES)    # (the classes the checks run with: main adds what the loaded Earth tree draws, rail_tunnels)
# What a vehicle's body may not cut: every Earth mesh, the terrain too (a cut's slope or a mound beside a road or a track,
# a ridge at a junction, the ground of a bridge Earth did not build): GROUND, MOVED, ROAD, DECK, RAIL, BUILDING, TUNNEL. Its
# own road or track under it never counts (earth_checks.BODY_BASE_M); kerbs, sidewalks and barriers along it do not either.
HIT_BY = (0, 1, 2, 3, 4, 5, 6)
KIND_CLASS = {"road": ROAD, "deck": DECK, "rail": RAIL, "bld": BUILDING, "b3dmr": BUILDING, "tunnel": TUNNEL, "walk": WALK,
              "barrier": BARRIER}
DEFAULT_KINDS = ("road", "deck", "tunnel", "rail", "bld", "b3dmr", "walk", "barrier")
BACKGROUND = (0.84, 0.87, 0.91)
LINE_RGB = (0.05, 0.05, 0.08)
NEEDLE_RGB = (0.0, 0.25, 1.0)
AMBIENT = 0.30
DEPTH_BIAS_M = 0.03         # m: everything but the ground is drawn this much nearer (the road mesh lies on the ground it follows)
COMPASS = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0}


# ======================================================================================================================
# pure parts: the ground mesh, the camera, the rasteriser, the shading
# ======================================================================================================================
def terrain_mesh(z, x0, y0, box, holes=None, moved=None):
    """The ground triangles of the cells of the height lattice z (row j is y0 + j, column i is x0 + i, 1 m apart) inside box
    (xmin, ymin, xmax, ymax). Returns (tris (n, 3, 3) world vertices, cls (n,) GROUND or MOVED). A cell with a hole (holes[j, i], the
    .ter's tunnel mouths) has no triangles; a cell with a moved corner (moved[j, i] bool) is MOVED."""
    ny, nx = z.shape
    i0 = int(max(0, math.floor(box[0] - x0)))
    i1 = int(min(nx - 1, math.ceil(box[2] - x0)))
    j0 = int(max(0, math.floor(box[1] - y0)))
    j1 = int(min(ny - 1, math.ceil(box[3] - y0)))
    if i1 <= i0 or j1 <= j0:
        return np.zeros((0, 3, 3)), np.zeros(0, np.int8)
    zz = z[j0:j1 + 1, i0:i1 + 1].astype(np.float64)
    h, w = zz.shape
    X = x0 + i0 + np.arange(w, dtype=np.float64)
    Y = y0 + j0 + np.arange(h, dtype=np.float64)
    V = np.stack(np.broadcast_arrays(X[None, :], Y[:, None], zz), axis=-1)        # (h, w, 3)
    a, b, c, d = V[:-1, :-1], V[:-1, 1:], V[1:, 1:], V[1:, :-1]                   # the cell's corners, counter-clockwise from below
    keep = np.isfinite(a[..., 2]) & np.isfinite(b[..., 2]) & np.isfinite(c[..., 2]) & np.isfinite(d[..., 2])
    if holes is not None:
        keep &= ~holes[j0:j1, i0:i1]
    cell_cls = np.full(keep.shape, GROUND, np.int8)
    if moved is not None:
        m = moved[j0:j1 + 1, i0:i1 + 1]
        cell_cls[m[:-1, :-1] | m[:-1, 1:] | m[1:, 1:] | m[1:, :-1]] = MOVED
    tris = np.concatenate([np.stack([a[keep], b[keep], c[keep]], axis=1), np.stack([a[keep], c[keep], d[keep]], axis=1)])
    cls = np.concatenate([cell_cls[keep], cell_cls[keep]])
    return tris, cls


def face_normals(tris):
    """Unit normals of triangles (n, 3, 3); (0, 0, 1) for a degenerate one."""
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ok = ln > 1e-12
    n[ok] /= ln[ok, None]
    n[~ok] = (0.0, 0.0, 1.0)
    return n


class Camera:
    """An orthographic camera looking along fwd at target; scale pixels per metre, the image W x H with target at its centre
    (shifted by the offset (m along right, m along up))."""

    def __init__(self, target, fwd, W, H, scale=1.0, offset=(0.0, 0.0)):
        f = np.asarray(fwd, np.float64)
        self.fwd = f / np.linalg.norm(f)
        hint = np.array([0.0, 0.0, 1.0]) if abs(self.fwd[2]) < 0.999 else np.array([0.0, 1.0, 0.0])
        r = np.cross(self.fwd, hint)
        self.right = r / np.linalg.norm(r)
        self.up = np.cross(self.right, self.fwd)
        self.target = np.asarray(target, np.float64)
        self.W, self.H, self.scale = int(W), int(H), float(scale)
        self.offset = (float(offset[0]), float(offset[1]))

    def project(self, P):
        """World points (..., 3) -> (sx, sy pixels with y down, depth along the view: larger is farther)."""
        d = np.asarray(P, np.float64) - self.target
        sx = self.W / 2.0 + (d @ self.right - self.offset[0]) * self.scale
        sy = self.H / 2.0 - (d @ self.up - self.offset[1]) * self.scale
        return sx, sy, d @ self.fwd

    def light(self):
        """The direction towards the light: from over the camera's left shoulder and above (so relief shows in every view)."""
        L = -0.55 * self.fwd - 0.45 * self.right + 0.45 * self.up
        return L / np.linalg.norm(L)


def view_camera(target, bearing_deg, elev_deg, W, H, scale=1.0, offset=(0.0, 0.0)):
    """The camera standing on the compass side bearing_deg of target (0 = north; looking back at it), elev_deg above the horizon
    (90: straight down, north up)."""
    el = math.radians(elev_deg)
    if elev_deg >= 89.9:
        fwd = (0.0, 0.0, -1.0)
    else:
        b = math.radians(bearing_deg)
        fwd = (-math.sin(b) * math.cos(el), -math.cos(b) * math.cos(el), -math.sin(el))
    return Camera(target, fwd, W, H, scale, offset)


def fit_camera(cam, points):
    """Scale and offset of cam that frame the world points (n, 3) with a 4 % margin in a W x H image."""
    d = np.asarray(points, np.float64) - cam.target
    u, v = d @ cam.right, d @ cam.up
    wm, hm = max(u.max() - u.min(), 1e-6), max(v.max() - v.min(), 1e-6)
    cam.scale = min(cam.W / (wm * 1.08), cam.H / (hm * 1.08))
    cam.offset = ((u.max() + u.min()) / 2.0, (v.max() + v.min()) / 2.0)
    return cam


def shade(normals, cam, ambient=AMBIENT):
    """Lambert shade (ambient .. 1) of triangles with the given normals, each normal turned to face the camera (two-sided)."""
    n = normals * np.where(normals @ cam.fwd > 0, -1.0, 1.0)[:, None]
    return ambient + (1.0 - ambient) * np.clip(n @ cam.light(), 0.0, 1.0)


def _split_long(scr, dep, ids, W, H, kmax, rounds=40):
    """Triangles (scr (n, 3, 2) pixels, dep (n, 3)) with no side of their pixel box over kmax: the long ones cut at the middle of
    their longest edge again and again; pieces outside the image are dropped."""
    done = []
    for _ in range(rounds):
        if not len(scr):
            break
        x, y = scr[..., 0], scr[..., 1]
        inside = (x.max(1) >= 0) & (x.min(1) < W) & (y.max(1) >= 0) & (y.min(1) < H)
        scr, dep, ids = scr[inside], dep[inside], ids[inside]
        if not len(scr):
            break
        x, y = scr[..., 0], scr[..., 1]
        big = np.maximum(x.max(1) - x.min(1), y.max(1) - y.min(1)) > kmax
        if (~big).any():
            done.append((scr[~big], dep[~big], ids[~big]))
        scr, dep, ids = scr[big], dep[big], ids[big]
        if not len(scr):
            break
        e = np.stack([np.hypot(*(scr[:, (k + 1) % 3] - scr[:, k]).T) for k in range(3)], axis=1)
        k = e.argmax(axis=1)
        r = np.arange(len(scr))
        i1, i2 = (k + 1) % 3, (k + 2) % 3
        p0, p1, p2 = scr[r, k], scr[r, i1], scr[r, i2]
        d0, d1, d2 = dep[r, k], dep[r, i1], dep[r, i2]
        pm, dm = (p0 + p1) / 2.0, (d0 + d1) / 2.0
        scr = np.concatenate([np.stack([p0, pm, p2], 1), np.stack([pm, p1, p2], 1)])
        dep = np.concatenate([np.stack([d0, dm, d2], 1), np.stack([dm, d1, d2], 1)])
        ids = np.concatenate([ids, ids])
    if not done:
        return np.zeros((0, 3, 2)), np.zeros((0, 3)), np.zeros(0, np.int64)
    return (np.concatenate([d[0] for d in done]), np.concatenate([d[1] for d in done]), np.concatenate([d[2] for d in done]))


def rasterize(scr, dep, W, H, kmax=32, budget=2_000_000):
    """Z-buffer of triangles: scr (n, 3, 2) pixel coordinates (x right, y down; a pixel's centre is at +0.5), dep (n, 3) depth
    (smaller is nearer; linear in the screen, an orthographic camera's). Returns (tri (H, W) int64 index of the nearest triangle or -1,
    depth (H, W) float64, inf where none). Batched in numpy: the triangles are bucketed by the size of their pixel box (cut when
    large), every batch tests all the pixel centres of its boxes at once and the nearest fragment per pixel is a minimum over a
    packed (depth, triangle) key."""
    scr = np.asarray(scr, np.float64)
    dep = np.asarray(dep, np.float64)
    n = len(scr)
    none = np.full(W * H, np.iinfo(np.uint64).max, np.uint64)
    if n == 0:
        return np.full((H, W), -1, np.int64), np.full((H, W), np.inf)
    ok = np.isfinite(scr).all(axis=(1, 2)) & np.isfinite(dep).all(axis=1)
    ids = np.arange(n, dtype=np.int64)[ok]
    scr, dep = scr[ok], dep[ok]
    if not len(scr):
        return np.full((H, W), -1, np.int64), np.full((H, W), np.inf)
    dmin, dmax = float(dep.min()), float(dep.max())
    q = (2.0 ** 32 - 2.0) / max(dmax - dmin, 1e-9)
    scr, dep, ids = _split_long(scr, dep, ids, W, H, kmax)
    if len(scr):
        x, y = scr[..., 0], scr[..., 1]
        x0 = np.maximum(np.ceil(x.min(1) - 0.5), 0).astype(np.int64)
        x1 = np.minimum(np.floor(x.max(1) - 0.5), W - 1).astype(np.int64)
        y0 = np.maximum(np.ceil(y.min(1) - 0.5), 0).astype(np.int64)
        y1 = np.minimum(np.floor(y.max(1) - 0.5), H - 1).astype(np.int64)
        w, h = x1 - x0 + 1, y1 - y0 + 1
        live = (w > 0) & (h > 0)
        scr, dep, ids = scr[live], dep[live], ids[live]
        x0, y0, x1, y1, w, h = x0[live], y0[live], x1[live], y1[live], w[live], h[live]
        kw = (1 << np.ceil(np.log2(np.maximum(w, 1))).astype(np.int64)).astype(np.int64)
        kh = (1 << np.ceil(np.log2(np.maximum(h, 1))).astype(np.int64)).astype(np.int64)
        for a_, b_ in set(zip(kw.tolist(), kh.tolist())):
            sel = np.nonzero((kw == a_) & (kh == b_))[0]
            step = max(1, budget // (a_ * b_))
            for s in range(0, len(sel), step):
                t = sel[s:s + step]
                v, dz, ti = scr[t], dep[t], ids[t]
                px = x0[t][:, None, None] + np.arange(a_)[None, None, :]
                py = y0[t][:, None, None] + np.arange(b_)[None, :, None]
                cx, cy = px + 0.5, py + 0.5
                ax, ay, bx, by, qx, qy = (v[:, 0, 0, None, None], v[:, 0, 1, None, None], v[:, 1, 0, None, None],
                                          v[:, 1, 1, None, None], v[:, 2, 0, None, None], v[:, 2, 1, None, None])
                area = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax)
                good = np.abs(area) > 1e-12
                area = np.where(good, area, 1.0)
                wa = ((qx - bx) * (cy - by) - (qy - by) * (cx - bx)) / area
                wb = ((ax - qx) * (cy - qy) - (ay - qy) * (cx - qx)) / area
                wc = ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax)) / area
                inside = good & (wa >= -1e-9) & (wb >= -1e-9) & (wc >= -1e-9) & (px <= x1[t][:, None, None]) & \
                    (py <= y1[t][:, None, None])
                if not inside.any():
                    continue
                z = (wa * dz[:, 0, None, None] + wb * dz[:, 1, None, None] + wc * dz[:, 2, None, None])[inside]
                tri = np.broadcast_to(ti[:, None, None], inside.shape)[inside].astype(np.uint64)
                pix = (py * W + px)[inside]
                key = (np.clip((z - dmin) * q, 0, 2.0 ** 32 - 2.0).astype(np.uint64) << np.uint64(32)) | tri
                np.minimum.at(none, pix, key)
    hit = none != np.iinfo(np.uint64).max
    tri = np.where(hit, (none & np.uint64(0xFFFFFFFF)).astype(np.int64), -1).reshape(H, W)
    depth = np.where(hit, (none >> np.uint64(32)).astype(np.float64) / q + dmin, np.inf).reshape(H, W)
    return tri, depth


def sample_polyline(P, step=0.5):
    """Points every step m along the 3D polyline P (n, 3), its vertices included."""
    P = np.asarray(P, np.float64)
    if len(P) < 2:
        return P
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.unique(np.concatenate([np.arange(0.0, s[-1], step), s]))
    return np.stack([np.interp(t, s, P[:, k]) for k in range(3)], axis=1)


def splat(img, sx, sy, rgb, radius=1, alpha=None):
    """Draw pixels at (sx, sy) in img (H, W, 3): a square of half-size radius, alpha (per point, or a number) over what is there."""
    H, W = img.shape[:2]
    rgb = np.asarray(rgb, np.float64)
    al = np.ones(len(sx)) if alpha is None else np.broadcast_to(np.asarray(alpha, np.float64), (len(sx),))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x = np.floor(sx).astype(np.int64) + dx
            y = np.floor(sy).astype(np.int64) + dy
            ok = (x >= 0) & (x < W) & (y >= 0) & (y < H)
            img[y[ok], x[ok]] = img[y[ok], x[ok]] * (1.0 - al[ok, None]) + rgb * al[ok, None]


def render(tris, cls, cam, lines=(), needle=None, ss=2, bias=DEPTH_BIAS_M, line_tol=0.6):
    """The picture (H, W, 3 floats 0..1) of triangles tris (n, 3, 3) with classes cls (n,) seen by cam, the polylines (3D point
    arrays) drawn over it as a thin dark line (full where nothing hides it by more than line_tol m, faint where it is behind
    something), needle = (x, y, z0, z1) a blue vertical marker. Rendered ss times larger and averaged down. Returns (image, used
    classes: sorted unique class numbers drawn)."""
    cs = Camera(cam.target, cam.fwd, cam.W * ss, cam.H * ss, cam.scale * ss, cam.offset)
    cls = np.asarray(cls)
    img = np.empty((cs.H, cs.W, 3))
    img[:] = BACKGROUND
    used = np.zeros(0, np.int64)
    zbuf = np.full((cs.H, cs.W), np.inf)
    if len(tris):
        sx, sy, d = cs.project(tris)
        scr = np.stack([sx, sy], axis=-1)
        d = d - np.where(cls <= MOVED, 0.0, bias)[:, None]
        tri, zbuf = rasterize(scr, d, cs.W, cs.H)
        col = CLASS_RGB[cls] * shade(face_normals(tris), cs)[:, None]
        hit = tri >= 0
        img[hit] = col[tri[hit]]
        used = np.unique(cls[np.unique(tri[hit])]) if hit.any() else used
    for P in lines:
        S = sample_polyline(P, 0.5 / cs.scale)
        sx, sy, d = cs.project(S)
        xi, yi = np.floor(sx).astype(np.int64), np.floor(sy).astype(np.int64)
        ok = (xi >= 0) & (xi < cs.W) & (yi >= 0) & (yi < cs.H)
        xi, yi, sx, sy, d = xi[ok], yi[ok], sx[ok], sy[ok], d[ok]
        seen = d <= zbuf[yi, xi] + line_tol
        splat(img, sx, sy, LINE_RGB, radius=max(1, ss // 2), alpha=np.where(seen, 1.0, 0.3))
    if needle is not None:
        x, y, z0, z1 = needle
        S = sample_polyline(np.array([[x, y, z0], [x, y, z1]]), 0.25)
        sx, sy, _ = cs.project(S)
        splat(img, sx, sy, NEEDLE_RGB, radius=max(1, ss))
        cx, cy, _ = cs.project(np.array([[x, y, z0]]))
        t = np.linspace(0, 2 * np.pi, 90)
        splat(img, cx[0] + 4.0 * ss * np.cos(t), cy[0] + 4.0 * ss * np.sin(t), NEEDLE_RGB, radius=max(1, ss // 2))
    out = img.reshape(cam.H, ss, cam.W, ss, 3).mean(axis=(1, 3)) if ss > 1 else img
    return out, used


SURFACE_STEP_M = 0.5         # m between the samples of surface_grid


def surface_grid(tris, cls, box, step=SURFACE_STEP_M):
    """What is seen from straight above at every step metres of box (x0, y0, x1, y1): (class (H, W) int8, -1 where nothing;
    height (H, W) m, NaN where nothing), by row = y, south first, a sample at the middle of its step square. The same z-buffer
    as the pictures (the meshes lying on the ground win over it by the depth bias), so two trees' grids compare sample by
    sample (server/evals/earth_eval.py)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    W, H = max(1, int(round((x1 - x0) / step))), max(1, int(round((y1 - y0) / step)))
    cls = np.asarray(cls)
    c = np.full((H, W), -1, np.int8)
    z = np.full((H, W), np.nan)
    if not len(tris):
        return c, z
    top = float(np.nanmax(tris[:, :, 2])) + 1.0
    cam = Camera(((x0 + x1) / 2.0, (y0 + y1) / 2.0, top), (0.0, 0.0, -1.0), W, H, 1.0 / step)
    sx, sy, d = cam.project(tris)
    b = np.where(cls <= MOVED, 0.0, DEPTH_BIAS_M)
    tri, zbuf = rasterize(np.stack([sx, sy], axis=-1), d - b[:, None], W, H)
    hit = tri >= 0
    c[hit] = cls[tri[hit]]
    z[hit] = top - (zbuf[hit] + b[tri[hit]])
    return c[::-1].copy(), z[::-1].copy()


# ---- reference vehicles: a full-height truck along every road, wagons along every railway --------------------------------
TRUCK_LWH = (12.0, 2.55, 4.0)       # m long, wide, high: a full-height truck (the EU's 4 m)
WAGON_LWH = (20.0, 3.0, 4.3)        # m: a full-height wagon (UIC's 4.32 m loading gauge over the rail)
TRUCK_EVERY_M = 20.0                # m between the trucks' middles along a road
# m from a road's line its trucks stand, one each side (both directions of a two-way road, both lanes of a dual
# carriageway's): a slope, a mound or a wall along either edge reaches one. A narrow road's trucks stand on its line.
SIDE_M = {"motorway": 1.75, "trunk": 1.75, "primary": 1.75, "secondary": 1.75, "tertiary": 1.75, "residential": 1.4,
          "unclassified": 1.4}
DUP_ACROSS_M = 1.0                  # m across its heading another vehicle stands from one already placed: the same one
WAGON_EVERY_M = 21.0                # m between the wagons' middles (nose to tail, 1 m apart)
NEIGH_M = 12.0                      # m round a truck: the surface whose triangles give its middle, heading and slope (a
                                    # 7 m road read 8 m round its end was near square: its heading turned 15 deg)
LEVEL_M = 2.0                       # m apart in height: another level (the road under a deck)
MIN_NEIGH = 6                       # surface triangles round a place for a vehicle there
RAIL_NEIGH_M = 4.5                  # m round a wagon: one track of a double line (their middles ~4.5 m apart)
UP_NZ = 0.8                         # a face whose normal is this near vertical is a surface a vehicle stands on (not an
                                    # abutment's wall, a parapet, a kerb's face: they turned wagons across the track) ...
                                    # ... and facing up (up_sign): a deck is a closed solid, and its underside, the girders'
                                    # depth (~2.1 m) under its road, stood a second row of trucks hanging from it
EARTH_UP_SIGN = -1.0                # Earth's .cdae winding: face_normals of its road surfaces point down (every flat face of
                                    # the SR17's road mesh at Popoli); used where no road mesh tells (up_sign)


def up_sign(tris, cls):
    """+1 or -1: the sign of face_normals' z on the faces that look up, read from the road and sidewalk meshes (single surfaces,
    all facing up: their majority), EARTH_UP_SIGN with none."""
    m = (cls == ROAD) | (cls == WALK)
    if not m.any():
        return EARTH_UP_SIGN
    nz = face_normals(tris[m])[:, 2]
    flat = np.abs(nz) >= UP_NZ
    if not flat.any():
        return EARTH_UP_SIGN
    return 1.0 if (nz[flat] > 0).sum() >= (nz[flat] < 0).sum() else -1.0
PICKUP_LWH = (5.5, 2.0)             # m long, wide: the vehicle on a road signed under PICKUP_BELOW_M (as high as the sign)
PICKUP_BELOW_M = 2.5                # m: a height limit under this is a pickup's or a van's road, not a truck's
LIMIT_NEAR_M = 5.0                  # m from a height limit's line a truck takes its height


def box_triangles(base, axis, L, W, H):
    """The 12 triangles (12, 3, 3) of a box standing on base (the middle of its bottom), its length along axis (unit; may slope),
    its height square to that and to the horizontal side."""
    ax = np.asarray(axis, np.float64)
    ax = ax / np.linalg.norm(ax)
    side = np.cross((0.0, 0.0, 1.0), ax)
    n = np.linalg.norm(side)
    side = side / n if n > 1e-9 else np.array([0.0, 1.0, 0.0])
    up = np.cross(ax, side)
    b = np.asarray(base, np.float64)
    c = np.array([[b + i * L / 2 * ax + j * W / 2 * side + k * H * up for k in (0, 1)] for i in (-1, 1) for j in (-1, 1)])
    c = c.reshape(2, 2, 2, 3)                   # [i][j][k]
    v = lambda i, j, k: c[i, j, k]              # noqa: E731
    quads = [(v(0, 0, 0), v(1, 0, 0), v(1, 1, 0), v(0, 1, 0)), (v(0, 0, 1), v(1, 0, 1), v(1, 1, 1), v(0, 1, 1)),
             (v(0, 0, 0), v(1, 0, 0), v(1, 0, 1), v(0, 0, 1)), (v(0, 1, 0), v(1, 1, 0), v(1, 1, 1), v(0, 1, 1)),
             (v(0, 0, 0), v(0, 1, 0), v(0, 1, 1), v(0, 0, 1)), (v(1, 0, 0), v(1, 1, 0), v(1, 1, 1), v(1, 0, 1))]
    return np.array([t for a, b_, c_, d in quads for t in ((a, b_, c_), (a, c_, d))], np.float64)


def _buckets(P, size):
    keys = np.floor(P[:, :2] / size).astype(np.int64)
    out = {}
    for i, k in enumerate(map(tuple, keys)):
        out.setdefault(k, []).append(i)
    return {k: np.array(v, np.int64) for k, v in out.items()}


def _near(P, buckets, size, p, reach, level):
    kx, ky = int(math.floor(p[0] / size)), int(math.floor(p[1] / size))
    r = int(math.ceil(reach / size))
    idx = [buckets[(kx + dx, ky + dy)] for dx in range(-r, r + 1) for dy in range(-r, r + 1) if (kx + dx, ky + dy) in buckets]
    if not idx:
        return np.zeros(0, np.int64)
    idx = np.concatenate(idx)
    d = P[idx] - p
    return idx[(np.hypot(d[:, 0], d[:, 1]) <= reach) & (np.abs(d[:, 2]) <= level)]


def place_vehicles(tris, every, lwh, top=False, level=LEVEL_M, neigh=NEIGH_M):
    """Boxes of lwh (long, wide, high) along the surface triangles tris (n, 3, 3), their middles every metres apart on each level:
    each on the middle of the surface triangles within neigh metres round a triangle no box is near yet (in the triangles' own
    order: deterministic), along their principal axis (the road's or the track's heading and grade), on their median height
    (top: their highest, the rails' heads over the ballast). Returns [(base (3,), axis (3,))]."""
    if not len(tris):
        return []
    C = tris.mean(axis=1)
    cb = _buckets(C, neigh)
    placed = np.zeros((0, 3))
    pb = {}
    out = []
    for i in np.lexsort((C[:, 1], C[:, 0])):
        p = C[i]
        if len(placed) and len(_near(placed, pb, every, p, every * 0.75, level)):
            continue
        q = C[_near(C, cb, neigh, p, neigh, level)]
        if len(q) < MIN_NEIGH:
            continue
        m = q.mean(axis=0)
        w, V = np.linalg.eigh(np.cov((q - m).T))
        axis = V[:, -1]
        if abs(axis[2]) > 0.5:                      # (not a road: a wall, a ramp's side)
            continue
        if axis[0] < 0 or (axis[0] == 0 and axis[1] < 0):
            axis = -axis
        base = np.array([m[0], m[1], float(q[:, 2].max() if top else np.median(q[:, 2]))])
        if len(placed) and len(_near(placed, pb, every, base, every * 0.75, level)):
            continue
        out.append((base, axis))
        placed = np.vstack([placed, base])
        pb = _buckets(placed, every)
    return out


def _dist_to_line(p, line):
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
    return float(np.hypot(*(a + ab * t[:, None] - p).T).min()) if len(a) else math.inf


NAME_REACH_M = 12.0                 # m round a point the map's lines are named from (a hit's, a cut crossing's)
OWN_REACH_M = 8.0                   # m from a vehicle its own line is looked for (a truck drives in its lane, off the line)


def lines_near(map_l, p, reach=NAME_REACH_M, kinds=("road", "rail"), n=3):
    """[{kind, id, cls, level, d}] of the map's lines within reach of p (x, y), nearest first, n at most: each line's
    feature id, class and its level there ("bridge", "tunnel" or "ground")."""
    out = []
    if not map_l:
        return out
    p = np.asarray(p, np.float64)[:2]
    for kind in kinds:
        ids, cls_ = map_l.get(kind + "_id") or [], map_l.get(kind + "_cls") or []
        for n_, (xy, ranges) in enumerate(map_l.get(kind, [])):
            xy = np.asarray(xy, np.float64)
            if len(xy) < 2 or (p < xy.min(axis=0) - reach).any() or (p > xy.max(axis=0) + reach).any():
                continue            # (outside its box grown by reach: a long segment's vertices are far from a point by it)
            a, b = xy[:-1], xy[1:]
            ab = b - a
            t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
            d = np.hypot(*(a + ab * t[:, None] - p).T)
            k = int(np.argmin(d))
            if d[k] > reach:
                continue
            seg = np.hypot(*ab.T)
            L = float(seg.sum())
            f = (float(seg[:k].sum()) + float(t[k]) * float(seg[k])) / max(L, 1e-9)
            out.append({"kind": kind, "id": ids[n_] if n_ < len(ids) else None, "cls": cls_[n_] if n_ < len(cls_) else None,
                        "level": _kind(ranges, f, L) or "ground", "d": round(float(d[k]), 1)})
    out.sort(key=lambda r: r["d"])
    seen, uniq = set(), []
    for r in out:
        if (r["kind"], r["id"]) not in seen:
            seen.add((r["kind"], r["id"]))
            uniq.append(r)
    return uniq[:n]


def line_name(r):
    return f"{r['kind']} {r['id']} ({r['cls']}, {r['level']})"


def named_hits(hits, map_l, frame):
    """Each hit of earth_checks.hit_report named: where (lat, lon), what the vehicle stands on (the map's line under it) and,
    per class it strikes, the struck triangles' heights against its body and the map's line they belong to (the nearest of
    that kind: a road's for a road or a deck, a track's for a rail, the nearest road's for ground: whose grading made it).
    Adds "lat", "lon", "on", "struck"[class]["line"] to each; returns the log lines."""
    out = []
    for h in hits:
        la, lo, _ = frame.enu_to_geodetic(np.array([h["x"]]), np.array([h["y"]]), 0.0)
        h["lat"], h["lon"] = round(float(np.ravel(la)[0]), 6), round(float(np.ravel(lo)[0]), 6)
        own = lines_near(map_l, (h["x"], h["y"]), OWN_REACH_M, kinds=("rail",) if h["kind"] == "wagon" else ("road",), n=1)
        h["on"] = own[0] if own else None
        parts = []
        for name, s in (h.get("struck") or {}).items():
            kinds = ("rail",) if name.startswith("rail") else ("road",)
            near = lines_near(map_l, (s["x"], s["y"]), kinds=kinds, n=2)
            if name != "ground":    # (a road or deck struck is another's than the one it stands on; ground may be its own's)
                near = [r for r in near if not (h["on"] and r["id"] == h["on"]["id"])] or near
            s["line"] = near[0] if near else None
            what = name if name != "ground" else "ground"
            parts.append(f"{what} at x {s['x']} y {s['y']} z {s['z_lo']}..{s['z_hi']} "
                         f"({h['hits'].get(name, 0)} m into it)"
                         + (f", {'graded by nearest' if name == 'ground' else 'of'} {line_name(s['line'])} "
                            f"{s['line']['d']} m off" if s["line"] else ", no map line within "
                            f"{NAME_REACH_M:g} m"))
        out.append(f"    {h['kind']} at {h['lat']},{h['lon']} (x {h['x']} y {h['y']}) heading {h['heading']:g}, body "
                   f"{h['body_base']}..{h['body_top']}, on " + (line_name(h["on"]) if h["on"] else "no map line")
                   + ": hits " + "; ".join(parts))
    return out


def named_cuts(xings, map_l, frame, box, label="cut"):
    """The log lines of the crossings whose meshes cut each other (earth_checks.penetrations' (x, y, cut)) in box: where
    (lat, lon) and the map's lines crossing there."""
    out = []
    for c in xings:
        if not (c[2] and box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]):
            continue
        la, lo, _ = frame.enu_to_geodetic(np.array([c[0]]), np.array([c[1]]), 0.0)
        near = lines_near(map_l, (c[0], c[1]), 3.0, n=4)
        out.append(f"    {label} at {float(np.ravel(la)[0]):.6f},{float(np.ravel(lo)[0]):.6f} (x {c[0]:.1f} y {c[1]:.1f}): "
                   + ("; ".join(line_name(r) for r in near) or "no map line within 3 m"))
    return out


def limit_at(limits, xy, reach=LIMIT_NEAR_M):
    """m: the lowest height limit whose line (in the frame) passes within reach of xy, or None."""
    hs = [h for line, h in limits if len(line) >= 2 and _dist_to_line(np.asarray(xy, np.float64), line) <= reach]
    return min(hs) if hs else None


MISSING_EVERY_M = 5.0               # m between the red posts along a map line with nothing under it
MISSING_POST = (0.6, 0.6, 6.0)      # m: a post's side, side and height
LOOK_M = 3.0                        # m ahead and behind a vehicle its slope is read (a wagon's: half its length, its chord)
RAIL_SIDES_M = tuple(0.5 + 0.05 * k for k in range(8))     # m either side of a track's line its rails' heads are looked for (0.5-0.85:
                                    # a narrow gauge's 0.5 to a broad gauge's 0.83; heads 0.07 wide): a wagon's wheels ride on them
LEVEL_MERGE_M = 1.0                 # m: surfaces under a point this close are one level (a road mesh on its deck, rails on
                                    # their sleepers on their ballast)
PREF_W = 0.5                        # the weight of a point's preferred level (its line's bridge / tunnel / ground) against its
                                    # continuity with the line's last point (1)
PATHS = ("footway", "cycleway", "path", "pedestrian", "steps", "bridleway", "sidewalk", "crossing")    # (no trucks)


class Surfaces:
    """The heights of triangles (n, 3, 3) under a point (x, y): every one whose plan holds it, at its plane there."""

    def __init__(self, tris, size=4.0):
        self.t = np.asarray(tris, np.float64)
        self.size = float(size)
        self.b = {}
        if not len(self.t):
            return
        lo = np.floor(self.t[:, :, :2].min(axis=1) / self.size).astype(np.int64)
        hi = np.floor(self.t[:, :, :2].max(axis=1) / self.size).astype(np.int64)
        for i in range(len(self.t)):
            for gx in range(lo[i, 0], hi[i, 0] + 1):
                for gy in range(lo[i, 1], hi[i, 1] + 1):
                    self.b.setdefault((gx, gy), []).append(i)

    def heights(self, x, y):
        """The sorted heights (m) of the triangles under (x, y)."""
        idx = self.b.get((int(math.floor(x / self.size)), int(math.floor(y / self.size))))
        if not idx:
            return np.zeros(0)
        t = self.t[idx]
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        v0, v1 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
        v2 = np.array([x, y]) - a[:, :2]
        d = v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0]
        ok = np.abs(d) > 1e-12
        d = np.where(ok, d, 1.0)
        u = (v2[:, 0] * v1[:, 1] - v2[:, 1] * v1[:, 0]) / d
        v = (v0[:, 0] * v2[:, 1] - v0[:, 1] * v2[:, 0]) / d
        inside = ok & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1.0 + 1e-6)
        z = a[:, 2] + u * (b[:, 2] - a[:, 2]) + v * (c[:, 2] - a[:, 2])
        return np.sort(z[inside])


def levels(z, top=False, merge=LEVEL_MERGE_M):
    """The levels of sorted heights z: runs closer than merge one level, at its highest (top: rails' heads) or its middle."""
    if not len(z):
        return []
    out, run = [], [float(z[0])]
    for v in z[1:]:
        if v - run[-1] <= merge:
            run.append(float(v))
        else:
            out.append(max(run) if top else float(np.median(run)))
            run = [float(v)]
    out.append(max(run) if top else float(np.median(run)))
    return out


def surface_levels(surf, p, t, top=False):
    """levels(surf.heights(p), top) of the point p (2,) of a line going along t (2,). With top (a track) the heights at
    RAIL_SIDES_M either side across the line too: the level is then a rail head's, which a wagon's wheels ride on, not the sleepers'
    top between the rails (0.16 m lower; the point on the line itself reads them, or the ballast between two sleepers 0.1-0.2 m
    lower still): a wagon stood 0.16-0.3 m low, so a deck's wall 0.42 m over the rail head (Birmingham's, 3.1 m from the track
    next to it) or a freeway's deck 0.43 over it stood in its body, which starts 0.5 m over its wheels."""
    z = surf.heights(*p)
    if top:
        n = np.array([-t[1], t[0]])
        z = np.sort(np.concatenate([z] + [surf.heights(*(p + s * n)) for s in RAIL_SIDES_M]
                                   + [surf.heights(*(p - s * n)) for s in RAIL_SIDES_M]))
    return levels(z, top)


def along(line, every):
    """Points every metres along line (k, 2) from every / 2 on, with their unit tangents and their fraction of its length."""
    line = np.asarray(line, np.float64)
    seg = np.hypot(*np.diff(line, axis=0).T)
    L = float(seg.sum())
    if L <= 0:
        return []
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    out = []
    for s in np.arange(every / 2.0, L, every):
        k = min(int(np.searchsorted(cum, s, side="right")) - 1, len(seg) - 1)
        while k < len(seg) - 1 and seg[k] <= 0:
            k += 1
        f = (s - cum[k]) / seg[k] if seg[k] > 0 else 0.0
        p = line[k] + f * (line[k + 1] - line[k])
        t = (line[k + 1] - line[k]) / seg[k] if seg[k] > 0 else np.array([1.0, 0.0])
        out.append((p, t, s / L))
    return out


MAX_STEP_M = 5.0                    # m: the most a road's level changes from one vehicle to the next (every 20 m: 25 %): a bigger
                                    # step is another deck's level (a road under a stack took the highest one's), never taken for a
                                    # preferred level's sake
MAX_STEP_PENALTY = 1000.0


def _choose(cands, prefs):
    """One level per point of a chain (cands: [levels], prefs: [preferred height or None]): the least sum of the steps between
    consecutive points (1 per m) and of each point's distance from its preferred height (PREF_W per m). None where a point has
    no level (the chain is cut there)."""
    out = [None] * len(cands)
    i = 0
    while i < len(cands):
        if not cands[i]:
            i += 1
            continue
        j = i
        while j < len(cands) and cands[j]:
            j += 1
        cost = [PREF_W * (abs(z - prefs[i]) if prefs[i] is not None else 0.0) for z in cands[i]]
        back = []
        for k in range(i + 1, j):
            nc, nb = [], []
            for z in cands[k]:
                steps = [cost[m] + abs(z - cands[k - 1][m]) + (MAX_STEP_PENALTY if abs(z - cands[k - 1][m]) > MAX_STEP_M else 0.0)
                         for m in range(len(cands[k - 1]))]
                m = int(np.argmin(steps))
                nc.append(steps[m] + PREF_W * (abs(z - prefs[k]) if prefs[k] is not None else 0.0))
                nb.append(m)
            cost, back = nc, back + [nb]
        m = int(np.argmin(cost))
        for k in range(j - 1, i - 1, -1):
            out[k] = cands[k][m]
            if k > i:
                m = back[k - i - 1][m]
        i = j
    return out


DECK_UNDER_M = 0.5                  # m under the terrain a bridge's deck may lie and still be its deck (its ends rest on their
                                    # embankments: a deck end 0.1-0.8 m over the ground was taken for no deck at all)


SHORT_TUNNEL_M = 60.0               # m: a road tunnel this short is open ground in Earth (gradesep's open underpasses), its
                                    # road painted on the terrain like any ground road


def _kind(ranges, f, length=None):
    """"bridge", "tunnel" or None (ground) at fraction f of a line length metres long (a tunnel stretch no longer than
    SHORT_TUNNEL_M is ground)."""
    for a, b, k in ranges:
        if a <= f <= b:
            if k == "tunnel" and length is not None and (b - a) * length <= SHORT_TUNNEL_M:
                return None
            return k
    return None


def _length(xy):
    return float(np.hypot(*np.diff(np.asarray(xy, np.float64), axis=0).T).sum()) if len(xy) >= 2 else 0.0


def chord_pose(line, s, half):
    """(centre (2,), unit heading (2,)) of a rigid vehicle 2 * half long at s metres along line (k, 2): heading along the chord
    from s - half to s + half, centred between the line's point and the chord's middle. Headed along the one segment it stood
    on, a 12 m truck 0.4-1.4 m before a vertex where a 25-30 m ramp turns 15 degrees stood 1.3-1.5 m off the line at its far end,
    over its own deck's parapet (Daikoku's service loop)."""
    L = _length(line)
    p, t = point_at(line, s)
    a, _ = point_at(line, max(0.0, s - half))
    b, _ = point_at(line, min(L, s + half))
    d = b - a
    n = float(np.hypot(*d))
    if n < 1e-6:
        return p, t
    return (p + (a + b) / 2.0) / 2.0, d / n


def point_at(line, s):
    """(point, unit tangent) at s metres along line (k, 2)."""
    line = np.asarray(line, np.float64)
    seg = np.hypot(*np.diff(line, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    k = int(np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1))
    while k < len(seg) - 1 and seg[k] <= 0:
        k += 1
    f = (s - cum[k]) / seg[k] if seg[k] > 0 else 0.0
    t = (line[k + 1] - line[k]) / seg[k] if seg[k] > 0 else np.array([1.0, 0.0])
    return line[k] + f * (line[k + 1] - line[k]), t


def crossings_under(line, others):
    """Fractions of line where it crosses under one of others: [(xy (k, 2), ranges, rail)], a railway anywhere (over it, or
    level with it), a road where it is on a bridge."""
    line = np.asarray(line, np.float64)
    L = _length(line)
    if L <= 0:
        return []
    a0, a1 = line[:-1], line[1:]
    cum_a = np.concatenate([[0.0], np.cumsum(np.hypot(*(a1 - a0).T))])
    out = []
    for oxy, oranges, rail in others:
        o = np.asarray(oxy, np.float64)
        if o is line or len(o) < 2:
            continue
        b0, b1 = o[:-1], o[1:]
        cum_b = np.concatenate([[0.0], np.cumsum(np.hypot(*(b1 - b0).T))])
        Lb = cum_b[-1]
        d1, d2 = a1 - a0, b1 - b0
        for i in range(len(a0)):
            den = d1[i, 0] * d2[:, 1] - d1[i, 1] * d2[:, 0]
            ok = np.abs(den) > 1e-12
            den = np.where(ok, den, 1.0)
            w = b0 - a0[i]
            ta = (w[:, 0] * d2[:, 1] - w[:, 1] * d2[:, 0]) / den
            tb = (w[:, 0] * d1[i, 1] - w[:, 1] * d1[i, 0]) / den
            hit = ok & (ta >= 0) & (ta <= 1) & (tb >= 0) & (tb <= 1)
            for j in np.where(hit)[0]:
                fb = (cum_b[j] + tb[j] * (cum_b[j + 1] - cum_b[j])) / Lb if Lb > 0 else 0.0
                if rail or _kind(oranges, fb, Lb) == "bridge":
                    out.append((cum_a[i] + ta[j] * (cum_a[i + 1] - cum_a[i])) / L)
    return sorted(out)


def missing_on_lines(lines, surf, box, ground=None, bridges_only=False, top=False, every=MISSING_EVERY_M):
    """[(x, y)] every metres along the lines where the meshes (surf) have nothing for the map's line: on a bridge stretch no
    deck at or over the terrain (DECK_UNDER_M); elsewhere (bridges_only False: a railway, always a mesh) no track at all. Not in a
    tunnel (drawn in its tube), nor on a road's ground stretch (Earth paints those into the terrain: no mesh to miss). Only
    inside box (x0, y0, x1, y1) less 2 m (the meshes are cut there)."""
    out = []
    for xy, ranges in lines:
        L = _length(xy)
        for p, _t, f in along(xy, every):
            if not (box[0] + 2 <= p[0] <= box[2] - 2 and box[1] + 2 <= p[1] <= box[3] - 2):
                continue
            kind = _kind(ranges, f, L)
            if kind == "tunnel" or (bridges_only and kind != "bridge"):
                continue
            lv = levels(surf.heights(*p), top)
            if kind == "bridge" and ground is not None:
                lv = [v for v in lv if v >= float(ground(*p)) - DECK_UNDER_M]
            if not lv:
                out.append((float(p[0]), float(p[1])))
    return out


def vehicles_on_lines(lines, surf, every, top=False, ground=None, level=LEVEL_M, painted=True, box=None, probes=None,
                      half=None):
    """[(base (3,), axis (3,))]: a vehicle every metres along each line of Earth's map, heading along it. lines: [(xy (k, 2),
    ranges)], ranges [(start, end, "bridge" | "tunnel")] fractions of the line (Earth's roads.level_ranges). Where it stands:
    - a ground stretch of a road (painted True): on the terrain, ground(x, y): Earth paints its ground roads into the terrain
      (surface.paint_roads), only their edges and markings are meshes;
    - a ground stretch of a railway (painted False): on the track mesh's level nearest the terrain;
    - a bridge stretch: on a level of surf (the decks) at or over the terrain (DECK_UNDER_M), the one nearest the line's last
      point (_choose: a ramp onto a stack keeps to its own deck), the highest where nothing else tells (the deck, not the edges
      of a road painted under it). A road not on a bridge there never takes a deck's level (an overpass is not the road under
      it);
    - a tunnel stretch: none (in its tube); one SHORT_TUNNEL_M or shorter is ground (Earth opens it).
    probes: per line, the fractions where it crosses under a bridge or a railway (crossings_under): a vehicle stands there
    first, the ones every metres around it.
    Its slope from the same surface LOOK_M ahead and behind (a wagon, top and half: half its length, its rigid body's chord: a
    0.25 m step in the track read +-4 % over 6 m, tilted a 20 m wagon's end 0.5 m into the rails ahead). None within 0.75 * every of one placed on the same level (copies
    of one road, segments end to end). box (x0, y0, x1, y1): only points inside it (the view)."""
    placed = np.zeros((0, 3))
    heads = np.zeros((0, 2))
    pb = {}
    out = []
    chosen = []                     # (probe?, base, axis) of every line, placed probes first
    for li, (xy, ranges) in enumerate(lines):
        L = _length(xy)
        pts = [(p, t, f, False) for p, t, f in along(xy, every)]
        for f in (probes[li] if probes is not None else []):
            p, t = point_at(xy, f * L)
            pts.append((p, t, f, True))
        pts.sort(key=lambda q: q[2])
        if not pts:
            continue
        if half:                    # (a rigid vehicle on a bend: along its chord, chord_pose)
            pts = [(*chord_pose(xy, f * L, half), f, pr) for _p, _t, f, pr in pts]
        cands, prefs, kinds = [], [], []
        for p, t_, f, _pr in pts:
            kind = _kind(ranges, f, L)
            g = float(ground(*p)) if ground is not None else None
            if box is not None and not (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]):
                lv, pref = [], None
            elif kind == "tunnel":
                lv, pref = [], None
            elif kind != "bridge" and painted and g is not None:
                lv, pref = [g], None
            elif kind == "bridge":
                lv = [v for v in surface_levels(surf, p, t_, top) if g is None or v >= g - DECK_UNDER_M]
                pref = max(lv) if lv else None
            else:
                lv = surface_levels(surf, p, t_, top)
                pref = g if lv else None
            cands.append(lv)
            prefs.append(pref)
            kinds.append(kind)
        for (p, t, _f, probe), z, kind in zip(pts, _choose(cands, prefs), kinds):
            if z is None:
                continue
            zs = []
            look = half if half and top else LOOK_M          # (a wagon lies on its chord: its pitch over its own length)
            for d in (look, -look):
                q = p + d * t
                if kind != "bridge" and painted and ground is not None:
                    zs.append(float(ground(*q)))
                    continue
                lv = surface_levels(surf, q, t, top)
                near = min(lv, key=lambda v: abs(v - z)) if lv else None
                zs.append(near if near is not None and abs(near - z) <= level else None)
            if None not in zs:
                slope = (zs[0] - zs[1]) / (2 * look)
            elif zs != [None, None]:        # (one side off the scene, the view's edge: its slope from the side it has,
                # not flat: a 12 m truck held level on a 10 % ramp at the box's edge stood 0.09 m into its own deck)
                slope = (zs[0] - z) / look if zs[0] is not None else (z - zs[1]) / look
            else:
                slope = 0.0
            chosen.append((probe, np.array([p[0], p[1], z]), np.array([t[0], t[1], slope])))
    for probe, base, axis in sorted(chosen, key=lambda c: not c[0]):        # (the probes first)
        if len(placed):
            near = _near(placed, pb, every, base, every * 0.75, level)
            if len(near):           # (the same one: along its heading within 0.75 every, across within DUP_ACROSS_M)
                d = base[:2] - placed[near, :2]
                h = heads[near]
                a_l = np.abs((d * h).sum(1))
                across = np.abs(d[:, 0] * h[:, 1] - d[:, 1] * h[:, 0])
                if ((a_l <= every * 0.75) & (across <= DUP_ACROSS_M)).any():
                    continue
        out.append((base, axis))
        placed = np.vstack([placed, base])
        a2 = np.asarray(axis[:2], np.float64)
        heads = np.vstack([heads, a2 / max(float(np.hypot(*a2)), 1e-9)])
        pb = _buckets(placed, every)
    return out


def side_lines(lines, classes, halves=None):
    """lines [(xy (k, 2), ranges)] each as its two sides (SIDE_M[class] from it, left and right), a narrow road's (no SIDE_M) as
    it is; the ranges the same fractions. halves: per line Earth's carriageway half width (m, None: not known): a side no
    farther out than a truck keeps within it (Compiegne's 5.5 m secondary: a truck 1.75 m out stood 0.28 m past its edge,
    against the fill of a track's end beside it)."""
    out = []
    for k, ((xy, ranges), c) in enumerate(zip(lines, classes)):
        off = SIDE_M.get(str(c), 0.0)
        half = halves[k] if halves is not None and k < len(halves) else None
        if half is not None:
            off = min(off, float(half) - TRUCK_LWH[1] / 2)
        if off <= 0 or len(xy) < 2:
            out.append((xy, ranges))
            continue
        out += [(offset_line(xy, off), ranges), (offset_line(xy, -off), ranges)]
    return out


def offset_line(xy, d):
    """The polyline xy (k, 2) moved d m to its left (its vertices along the mean of their segments' normals)."""
    xy = np.asarray(xy, np.float64)
    seg = np.diff(xy, axis=0)
    n = np.stack([-seg[:, 1], seg[:, 0]], 1)
    n /= np.maximum(np.hypot(n[:, 0], n[:, 1]), 1e-9)[:, None]
    vn = np.zeros_like(xy)
    vn[:-1] += n
    vn[1:] += n
    vn /= np.maximum(np.hypot(vn[:, 0], vn[:, 1]), 1e-9)[:, None]
    return xy + d * vn


def reference_vehicles(tris, cls, limits=(), lines=None, ground=None):
    """(triangles, classes) of the reference trucks along the roads and road decks (not a rail deck: a deck with the track on it)
    and the wagons along the railways. limits: [(line (k, 2) in the frame, m)], the roads' height limits: a truck near one is as
    high as it, a pickup under PICKUP_BELOW_M. lines: {"road": [(xy, ranges)], "rail": [...]} (map_lines: Earth's map), the
    vehicles along them (vehicles_on_lines), ground(x, y) the terrain's height; None: placed from the meshes alone
    (place_vehicles: their headings guessed, wrong at junctions, deck ends and wide places)."""
    up = face_normals(tris)[:, 2] * up_sign(tris, cls) >= UP_NZ if len(tris) else np.zeros(0, bool)
    road = ((cls == ROAD) | (cls == DECK)) & up
    rail = (cls == RAIL) & up
    if road.any() and rail.any():           # (a deck under a track is the railway's)
        R = tris[rail].mean(axis=1)
        rb = _buckets(R, 4.0)
        D = np.where(cls == DECK)[0]
        for i in D:
            c = tris[i].mean(axis=0)
            near = _near(R, rb, 4.0, c + (0.0, 0.0, 1.0), 3.0, 1.6)
            if len(near):
                road[i] = False
    missing = []
    if lines is not None:
        g = tris[cls <= MOVED]              # (the ground: cropped to the view's box)
        box = ((g[..., 0].min(), g[..., 1].min(), g[..., 0].max(), g[..., 1].max()) if len(g)
               else (tris[..., 0].min(), tris[..., 1].min(), tris[..., 0].max(), tris[..., 1].max()))
        rs, ts = Surfaces(tris[road]), Surfaces(tris[rail])
        roads_, rails_ = lines.get("road", []), lines.get("rail", [])
        others = [(xy, r, False) for xy, r in roads_] + [(xy, r, True) for xy, r in rails_]
        sides = side_lines(roads_, lines.get("road_cls", [None] * len(roads_)), lines.get("road_half"))
        probes = [crossings_under(xy, others) for xy, _r in sides]
        trucks = vehicles_on_lines(sides, rs, TRUCK_EVERY_M, ground=ground, box=box, probes=probes, half=TRUCK_LWH[0] / 2)
        wagons = vehicles_on_lines(lines.get("rail", []), ts, WAGON_EVERY_M, top=True, ground=ground, painted=False, box=box,
                                   half=WAGON_LWH[0] / 2)
        decks = Surfaces(tris[(cls == DECK) & up])      # (a bridge's: only a deck counts, not a road's edges painted under it)
        tracks = Surfaces(tris[rail | ((cls == DECK) & up)])    # (a railway's: its track, or a deck it runs on)
        missing = (missing_on_lines(lines.get("road", []), decks, box, ground, bridges_only=True)
                   + missing_on_lines(lines.get("rail", []), tracks, box, ground, top=True))
    else:
        trucks = place_vehicles(tris[road], TRUCK_EVERY_M, TRUCK_LWH)
        wagons = place_vehicles(tris[rail], WAGON_EVERY_M, WAGON_LWH, top=True, neigh=RAIL_NEIGH_M)
    T = []
    for base, axis in trucks:
        h = limit_at(limits, base[:2]) if limits else None
        if h is None:
            T.append(box_triangles(base, axis, *TRUCK_LWH))
        elif h < PICKUP_BELOW_M:
            T.append(box_triangles(base, axis, *PICKUP_LWH, h))
        else:
            T.append(box_triangles(base, axis, TRUCK_LWH[0], TRUCK_LWH[1], h))
    W = [box_triangles(base, axis, *WAGON_LWH) for base, axis in wagons]
    T = np.concatenate(T) if T else np.zeros((0, 3, 3))
    W = np.concatenate(W) if W else np.zeros((0, 3, 3))
    M = [box_triangles((x, y, float(ground(x, y)) if ground is not None else 0.0), (1.0, 0.0, 0.0), *MISSING_POST)
         for x, y in missing]
    M = np.concatenate(M) if M else np.zeros((0, 3, 3))
    return (np.concatenate([T, W, M]),
            np.array([TRUCK] * len(T) + [WAGON] * len(W) + [MISSING] * len(M), np.int8))


def map_lines(a, frame, box, log, margin=30.0):
    """({"road": [(xy (k, 2), ranges)], "rail": [...]}, limits): Earth's roads (not paths) and railways round box from its map
    cells, each line in the frame with its bridge / tunnel ranges (roads.level_ranges), once per feature; and the roads'
    height limits (height_limits). (None, []) when Earth's map data can't be read."""
    try:
        bw = earth_state(a, log)
        md = getattr(bw._s, "md", None)
        if md is None:
            return None, []
        import mapcells
        import roads
        xs, ys = np.array([box[0], box[2], box[0], box[2]]), np.array([box[1], box[1], box[3], box[3]])
        la, lo, _ = frame.enu_to_geodetic(xs, ys, 0.0)
        out, seen = {"road": [], "rail": [], "road_cls": [], "road_id": [], "rail_id": [], "rail_cls": [], "road_half": []}, set()
        for k in mapcells.cells_for_points(np.asarray(la, np.float64), np.asarray(lo, np.float64)):
            feats = md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if feats is None or not len(feats):
                continue
            xy = feats.xy_in(frame)
            sub, cl = feats.attrs.get("subtype"), feats.attrs.get("class")
            specs = roads.road_specs(feats)     # (Earth's carriageway widths)
            for i in range(len(feats)):
                fid = str(feats.ids[i])          # (an Overture GERS id)
                if fid in seen:
                    continue
                kind = "rail" if sub is not None and str(sub[i]) == "rail" else "road"
                if kind == "road" and cl is not None and str(cl[i]) in PATHS:
                    continue
                rings = [r for part in feats.parts(i, xy) for r in part if len(r) >= 2]
                if not rings or not any(r[:, 0].max() >= box[0] - margin and r[:, 0].min() <= box[2] + margin
                                        and r[:, 1].max() >= box[1] - margin and r[:, 1].min() <= box[3] + margin
                                        for r in rings):
                    continue
                seen.add(fid)
                try:
                    ranges = [(float(s0), float(s1), str(kd)) for s0, s1, kd in roads.level_ranges(feats, i)]
                except Exception:
                    ranges = []
                for r in rings:
                    out[kind].append((np.asarray(r, np.float64), ranges if len(rings) == 1 else []))
                    out[kind + "_cls"].append(str(cl[i]) if cl is not None else None)
                    out[kind + "_id"].append(fid)
                    if kind == "road":
                        out["road_half"].append(specs[i].width / 2 if specs[i] is not None else None)
        return out, height_limits(a, frame, box, log)
    except Exception as e:      # (the vehicles are then placed from the meshes alone)
        log(f"  map lines not read: {type(e).__name__}: {e}")
        return None, []


def height_limits(a, frame, box, log):
    """[(line (k, 2) in the frame, m)]: the roads' height limits round box (the map's maxheight: Earth's roads.height_limit_lines
    of its map cells, not those lying on the road's own bridge); [] when Earth's map data can't be read."""
    try:
        bw = earth_state(a, log)
        md = getattr(bw._s, "md", None)
        if md is None:
            return []
        import mapcells
        import roads
        xs, ys = np.array([box[0], box[2], box[0], box[2]]), np.array([box[1], box[1], box[3], box[3]])
        la, lo, _ = frame.enu_to_geodetic(xs, ys, 0.0)
        out = []
        for k in mapcells.cells_for_points(np.asarray(la, np.float64), np.asarray(lo, np.float64)):
            feats = md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
            if feats is None:
                continue
            for ring, h in roads.height_limit_lines(feats, on_bridges=False):
                ring = np.asarray(ring, np.float64)
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                out.append((np.stack([np.asarray(e, np.float64), np.asarray(n, np.float64)], axis=1), float(h)))
        return out
    except Exception as e:      # (the map data's absence only loses the signs: every truck 4 m high)
        log(f"  height limits not read: {type(e).__name__}: {e}")
        return []


# ======================================================================================================================
# the tiles' meshes, the picture's frame (title, legend, scale bar, north arrow)
# ======================================================================================================================
def kind_of(key):
    """The mesh kind of a tile JSON field (bld2 -> bld, railcol3 -> railcol)."""
    return re.sub(r"\d+$", "", key)


def tile_entries(data):
    """[(kind, shape file name, pos)] of the meshes a road tile's JSON names ({"shape": "/vfs/x.cdae", "pos": [x, y, z]} fields, and
    b3dmr's list)."""
    out = []
    for key, v in data.items():
        items = v if isinstance(v, list) else [v]
        for e in items:
            if isinstance(e, dict) and isinstance(e.get("shape"), str) and e.get("pos") is not None \
                    and e["shape"].lower().endswith(".cdae"):
                out.append((kind_of(key), e["shape"].replace("\\", "/").rsplit("/", 1)[-1], [float(c) for c in e["pos"]]))
    return out


def shape_triangles(shape, pos):
    """Triangles (n, 3, 3) of a decoded cdae shape (cdae.read's dict) in world coordinates: every standard mesh's vertices at pos."""
    import cdae
    out = []
    for m in shape["meshes"]:
        if m.get("type") != 0:
            continue
        a = cdae.mesh_arrays(m)
        idx = a["indices"].astype(np.int64)
        idx = idx[:len(idx) // 3 * 3].reshape(-1, 3)
        if not len(idx) or not len(a["verts"]):
            continue
        out.append(a["verts"].astype(np.float64)[idx] + np.asarray(pos, np.float64))
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def crop_triangles(tris, box):
    """The triangles whose x/y box meets box (xmin, ymin, xmax, ymax)."""
    if not len(tris):
        return tris
    x, y = tris[..., 0], tris[..., 1]
    k = (x.max(1) >= box[0]) & (x.min(1) <= box[2]) & (y.max(1) >= box[1]) & (y.min(1) <= box[3])
    return tris[k]


def parse_bearing(s):
    s = s.strip().upper()
    if s in COMPASS:
        return COMPASS[s]
    return float(s) % 360.0


def view_name(s):
    v = s.strip().lower()
    return v if v in ("top", "overview") else f"from_{s.strip().upper()}"


def _font(size):
    from PIL import ImageFont
    for p in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def annotate_panel(img, cam, label=None):
    """The map image (H, W, 3 floats) as a PIL image with its scale bar and north arrow, and label (the view) in its top left."""
    from PIL import Image, ImageDraw
    pic = Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8))
    d = ImageDraw.Draw(pic)
    W, H = pic.size
    f = _font(15)
    # scale bar: along the picture's horizontal, whatever the camera (orthographic: the same everywhere in it)
    for L in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if L * cam.scale > W * 0.12:
            break
    x0, y0 = 20, H - 22
    d.rectangle([x0 - 6, y0 - 22, x0 + L * cam.scale + 6, y0 + 10], fill=(255, 255, 255))
    d.line([x0, y0, x0 + L * cam.scale, y0], fill=(0, 0, 0), width=3)
    d.line([x0, y0 - 5, x0, y0 + 5], fill=(0, 0, 0), width=2)
    d.line([x0 + L * cam.scale, y0 - 5, x0 + L * cam.scale, y0 + 5], fill=(0, 0, 0), width=2)
    d.text((x0, y0 - 20), f"{L} m (across the picture)", fill=(0, 0, 0), font=f)
    # north arrow
    nx, ny = float(np.array([0, 1, 0]) @ cam.right), float(np.array([0, 1, 0]) @ cam.up)
    ln = math.hypot(nx, ny) or 1.0
    cx, cy = W - 50, 50
    d.ellipse([cx - 34, cy - 34, cx + 34, cy + 34], fill=(255, 255, 255), outline=(90, 90, 90))
    ux, uy = nx / ln, -ny / ln
    reach = 26 * max(ln, 0.35)
    d.line([cx - ux * 6, cy - uy * 6, cx + ux * reach, cy + uy * reach], fill=(200, 0, 0), width=4)
    d.text((cx + ux * (reach + 6) - 5, cy + uy * (reach + 6) - 9), "N", fill=(200, 0, 0), font=f)
    if label:
        lf = _font(18)
        tw = d.textlength(label, font=lf)
        d.rectangle([0, 0, tw + 20, 30], fill=(255, 255, 255), outline=(90, 90, 90))
        d.text((10, 4), label, fill=(0, 0, 0), font=lf)
    return pic


def legend_entries(used, has_line, has_needle, extra_legend=()):
    """[(rgb 0..255, name)] of the classes drawn (used), the stage line, the needle, and extra_legend."""
    entries = [(tuple(int(c * 255) for c in CLASS_RGB[c]), CLASS_NAME[c]) for c in sorted(set(int(u) for u in used))]
    if has_line:
        entries.append((tuple(int(c * 255) for c in LINE_RGB), "stage line (--geometry): dark; faint where hidden"))
    if has_needle:
        entries.append((tuple(int(c * 255) for c in NEEDLE_RGB), "the point (needle up from the ground, ring on it)"))
    return entries + list(extra_legend)


PANEL_GAP = 6       # px between the views of one picture


def compose(panels, title, subtitle, entries, cols=2):
    """Panels (PIL images of one size) in a grid of cols per row, PANEL_GAP apart, a title band (62 px) above and the legend
    below; a PIL image."""
    from PIL import Image, ImageDraw
    cols = max(1, min(cols, len(panels)))
    pw, ph = panels[0].size
    prow = (len(panels) + cols - 1) // cols
    W = cols * pw + (cols - 1) * PANEL_GAP
    H = prow * ph + (prow - 1) * PANEL_GAP
    f, tf = _font(15), _font(18)
    lcols = 3 if W < 1400 else 4
    lrows = (len(entries) + lcols - 1) // lcols
    row_h, head = 24, 62
    canvas = Image.new("RGB", (W, head + H + 12 + lrows * row_h + 10), (255, 255, 255))
    for k, pic in enumerate(panels):
        canvas.paste(pic, ((k % cols) * (pw + PANEL_GAP), head + (k // cols) * (ph + PANEL_GAP)))
    cd = ImageDraw.Draw(canvas)
    cd.text((12, 6), title, fill=(0, 0, 0), font=tf)
    cd.text((12, 34), subtitle, fill=(60, 60, 60), font=f)
    for k, (rgb, name) in enumerate(entries):
        cx_ = 12 + (k % lcols) * (W // lcols)
        cy_ = head + H + 12 + (k // lcols) * row_h
        cd.rectangle([cx_, cy_ + 2, cx_ + 28, cy_ + 18], fill=rgb, outline=(0, 0, 0))
        cd.text((cx_ + 36, cy_ + 1), name, fill=(0, 0, 0), font=f)
    return canvas


def frame_picture(img, cam, title, subtitle, used, has_line, has_needle, extra_legend=(), label=None):
    """One view framed alone: the map image (H, W, 3 floats) with a title bar above, the scale bar and north arrow on it, the
    legend below; a PIL image."""
    return compose([annotate_panel(img, cam, label)], title, subtitle,
                   legend_entries(used, has_line, has_needle, extra_legend), cols=1)


# ======================================================================================================================
# the overview: the area round the point in 3D, at one fixed angle
# ======================================================================================================================
OVERVIEW_M = 500.0          # m each side of the point the overview shows (1 km x 1 km)
OVERVIEW_BEARING = 225.0    # the overview's camera: always from the south-west ...
OVERVIEW_ELEV = 40.0        # ... this many degrees above the horizon
OVERVIEW_STEP = 2           # the ground's lattice taken every this many samples (m) in the overview
CHANGED_M = 0.25            # m the ground must have risen or fallen to be marked on the overview (earth_eval's MINOR_M)


def is_overview(v):
    return v.strip().lower() == "overview"


def overview_box(px, py, half=OVERVIEW_M):
    return (px - half, py - half, px + half, py + half)


def overview_ground(z, x0, y0, box, holes=None, dz=None, step=OVERVIEW_STEP, changed_m=CHANGED_M):
    """The ground triangles of box from every step-th sample of the lattice z (terrain_mesh's, coarser), (tris, cls): GROUND, or
    ROSE / FELL where dz (the lattice's change since before, z's shape) is changed_m or more up / down."""
    zs = z[::step, ::step]
    hs = holes[::step, ::step] if holes is not None else None
    ds = np.nan_to_num(dz[::step, ::step]) if dz is not None else None
    moved = (np.abs(ds) >= changed_m) if ds is not None else None
    sb = tuple(v / step for v in box)
    tris, cls = terrain_mesh(zs, x0 / step, y0 / step, sb, hs, moved)
    tris = tris.copy()
    tris[..., :2] *= step
    if ds is not None and (cls == MOVED).any():
        # a cell's change: its corner's that moved most (a cell at the edge of a fallen patch is falling, not rising)
        q = np.stack([ds[:-1, :-1], ds[:-1, 1:], ds[1:, 1:], ds[1:, :-1]])
        ext = np.take_along_axis(q, np.abs(q).argmax(axis=0)[None], axis=0)[0]
        m = np.nonzero(cls == MOVED)[0]
        c = tris[m].mean(axis=1)
        i = np.clip(np.floor((c[:, 0] - x0) / step).astype(int), 0, ext.shape[1] - 1)
        j = np.clip(np.floor((c[:, 1] - y0) / step).astype(int), 0, ext.shape[0] - 1)
        cls = cls.copy()
        cls[m] = np.where(ext[j, i] >= 0, ROSE, FELL)
    return tris, cls


def overview_scene(z, x0, y0, box, holes, meshes, dz=None):
    """(tris, cls) of the overview: the coarser ground of box and every mesh of the tiles (meshes {kind: tris}) lying in it."""
    T, C = [], []
    t, c = overview_ground(z, x0, y0, box, holes, dz)
    T.append(t)
    C.append(c)
    for kind, m in sorted(meshes.items()):
        m = inside_triangles(m, box)
        T.append(m)
        C.append(np.full(len(m), KIND_CLASS.get(kind, OTHER), np.int8))
    return np.concatenate(T), np.concatenate(C)


def overview_cuts(z, x0, y0, holes, tris, cls, lines):
    """(cut (len tris,) bool, crossings [(x, y, cut)]) of the overview's scene (tris, cls: its ground GROUND or MOVED): each
    crossing of the map's lines at different levels checked as the close-up checks it, on the lattice's own ground round
    it (terrain_mesh, 1 m) with the overview's meshes there. The overview's ground is every OVERVIEW_STEP-th sample: one of
    its triangles spans a fill's edge, and its middle falls in the road's corridor (earth_checks.GROUND_CORRIDOR_M) where
    the lattice's own do not (Compiegne: a track bed's end 5 m off the road under its deck's last metre, a cut in the
    overview only). The overview's ground triangles over a cutting one of the lattice's are marked cut."""
    cut = np.zeros(len(tris), bool)
    out = []
    if not len(tris):
        return cut, out
    ground = cls <= MOVED
    M = np.flatnonzero(~ground)
    lo, hi = tris[M][:, :, :2].min(axis=1), tris[M][:, :, :2].max(axis=1)
    G = np.flatnonzero(ground)
    gc = tris[G].mean(axis=1)[:, :2]
    r = earth_checks.CROSS_NEAR_M + 2.0
    for xing in earth_checks.level_crossings(lines):
        x, y = xing[0], xing[1]
        b = (x - r, y - r, x + r, y + r)
        g, gcl = terrain_mesh(z, x0, y0, b, holes)
        m = M[(hi[:, 0] >= b[0]) & (lo[:, 0] <= b[2]) & (hi[:, 1] >= b[1]) & (lo[:, 1] <= b[3])]
        hit, got = earth_checks.penetrations(np.concatenate([g, tris[m]]), np.concatenate([gcl, cls[m]]), lines,
                                             CHECKS, xings=[xing])
        out += got
        cut[m[hit[len(g):]]] = True
        if hit[:len(g)].any():
            c = g[hit[:len(g)]].mean(axis=1)[:, :2]
            near = (np.abs(gc[:, None, :] - c[None]) <= OVERVIEW_STEP).all(axis=2).any(axis=1)
            cut[G[near]] = True
    return cut, out


def inside_triangles(tris, box, margin=5.0):
    """The triangles lying wholly in box (xmin, ymin, xmax, ymax) grown by margin: the overview's square ends cleanly, a mesh
    crossing its edge left out."""
    if not len(tris):
        return tris
    x, y = tris[..., 0], tris[..., 1]
    k = (x.min(1) >= box[0] - margin) & (x.max(1) <= box[2] + margin) & (y.min(1) >= box[1] - margin) & (y.max(1) <= box[3] + margin)
    return tris[k]


def box_outline(z, x0, y0, box, lift=1.0, every=2.0):
    """The close-up's box as a 3D polyline on the ground (lift m over it), for render's lines."""
    (a0, b0, a1, b1) = box
    n = max(2, int(round((a1 - a0) / every)))
    xs = np.concatenate([np.linspace(a0, a1, n), np.full(n, a1), np.linspace(a1, a0, n), np.full(n, a0)])
    ys = np.concatenate([np.full(n, b0), np.linspace(b0, b1, n), np.full(n, b1), np.linspace(b1, b0, n)])
    return np.stack([xs, ys, np.asarray(bilinear(z, x0, y0, xs, ys), np.float64) + lift], axis=1)


def overview_camera(tris, target, W, H):
    """The overview's camera: from OVERVIEW_BEARING, OVERVIEW_ELEV above the horizon, framing the scene's triangles."""
    cam = view_camera(target, OVERVIEW_BEARING, OVERVIEW_ELEV, W, H)
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    fit_camera(cam, np.array([[x, y, zz] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for zz in (lo[2], hi[2])]))
    return cam


def compare_heights(data_dir, aid, cx, cy, z, log):
    """z minus the block of the same place in data_dir (another tree's build): the change since before; None when it can't be
    read or isn't the same block."""
    try:
        zb, _h, _i = load_block_files(data_dir, aid, cx, cy)
    except OSError as e:
        log(f"  --compare-data: not read ({e})")
        return None
    if zb.shape != z.shape:
        log(f"  --compare-data: the block there is {zb.shape}, here {z.shape}: not compared")
        return None
    return z - zb


# ======================================================================================================================
# Earth: the block and the tiles
# ======================================================================================================================
def parse_pair(s):
    a, b = s.split(",")
    return float(a), float(b)


def block_centre(x, y):
    """The block centre (a multiple of BLOCK_STEP) nearest the point: the point lies near the middle of its 1024 m block."""
    return float(round(x / BLOCK_STEP) * BLOCK_STEP), float(round(y / BLOCK_STEP) * BLOCK_STEP)


def tiles_covering(x, y, radius):
    """(tx, ty) of the anchor road tiles meeting the square of half-side radius around (x, y)."""
    return [(tx, ty) for tx in range(int(math.floor((x - radius) / TILE)), int(math.floor((x + radius) / TILE)) + 1)
            for ty in range(int(math.floor((y - radius) / TILE)), int(math.floor((y + radius) / TILE)) + 1)]


def earth_version(earth):
    try:
        with open(os.path.join(earth, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "?"


def block_quantum(info):
    """m a step of a block's 16-bit heights: its own maxHeight / 65536 (a block whose heights reach past the anchor's
    range is built on a coarser step than the anchor's q; decoded with the anchor's it stood at half height)."""
    mh = info.get("maxHeight")
    return float(mh) / 65536.0 if mh else float(info["q"])


def load_block_files(data_dir, aid, cx, cy):
    """(heights (n, n) by row = y, holes (n, n) bool or None, info dict) of a block's files in data_dir."""
    from PIL import Image
    info_path = os.path.join(data_dir, f"hm_{aid}_{int(cx)}_{int(cy)}.info.json")
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    png = np.asarray(Image.open(os.path.join(data_dir, f"hm_{aid}_{int(cx)}_{int(cy)}.png")), np.float64)[::-1]
    z = png * block_quantum(info) + float(info.get("zBase") or 0.0)
    holes = None
    terp = os.path.join(data_dir, f"hm_{aid}_{int(cx)}_{int(cy)}_view.ter")
    if os.path.isfile(terp):
        with open(terp, "rb") as f:
            raw = f.read()
        n = int.from_bytes(raw[1:5], "little")
        lay = np.frombuffer(raw, np.uint8, n * n, 5 + 2 * n * n).reshape(n, n)
        holes = lay == 255
        holes[0, :] = holes[-1, :] = holes[:, 0] = holes[:, -1] = False     # (the edge ring: always holes)
    return z, holes, info


def bilinear(z, x0, y0, x, y):
    fx, fy = np.asarray(x, np.float64) - x0, np.asarray(y, np.float64) - y0
    i = np.clip(np.floor(fx).astype(int), 0, z.shape[1] - 2)
    j = np.clip(np.floor(fy).astype(int), 0, z.shape[0] - 2)
    u, v = np.clip(fx - i, 0, 1), np.clip(fy - j, 0, 1)
    return z[j, i] * (1 - u) * (1 - v) + z[j, i + 1] * u * (1 - v) + z[j + 1, i] * (1 - u) * v + z[j + 1, i + 1] * u * v


def earth_state(a, log):
    """Earth's worker state (what blockworker builds from: the disk cache, the map data), set up once; the quantum a.q is read from
    the server log when not given."""
    import blockworker
    import earth_writers
    if a.q is None:
        a.q = earth_writers.log_quantum(a.server_log, a.aid) or 1 / 32
        log(f"height quantum {a.q:g} m (the server log's)")
    if getattr(blockworker, "_s", None) is None:
        # (online: what the cache lacks is fetched, as the game does. Offline, a terrain tile a change newly read
        # failed the build: Compiegne's DEM 15/16643/11199, 2026-11-28)
        blockworker._s = blockworker._State(a.cache, False, True, True, True, True, airports=not a.no_airports,
                                            sea=not a.no_sea)
        blockworker._s.anchor(a.aid, *a.anchor)
    return blockworker


def build_earth(a, frame, px, py, log, reach=None):
    """Build (or with --reuse read) the block and the road tiles of the point with Earth's own code, the tiles reach m round it
    (default: the radius). Returns (heights, holes, info, x0, y0, block (cx, cy), [(tx, ty)] of the radius, tile data dir)."""
    data_dir = os.path.join(a.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    cx, cy = a.block if a.block else block_centre(px, py)
    tiles = tiles_covering(px, py, a.radius)
    built = tiles_covering(px, py, max(a.radius, reach or 0.0))
    stem = f"hm_{a.aid}_{int(cx)}_{int(cy)}"
    timing_path = os.path.join(data_dir, "timing.json")
    timing = {}
    if not (a.reuse and os.path.isfile(os.path.join(data_dir, stem + ".info.json"))):
        blockworker = earth_state(a, log)
        lat0, lon0 = a.anchor
        log(f"building block ({cx:g}, {cy:g}), {a.block_size} samples: this takes minutes ...")
        t0 = time.perf_counter()
        info = blockworker.build(a.aid, lat0, lon0, HM_ZOOM, a.q, cx, cy, a.block_size, data_dir, stem + ".png",
                                 ter_name=stem + "_view.ter")
        info = {k: v for k, v in dict(info).items() if isinstance(v, (int, float, str, bool, type(None)))}
        timing["block_s"] = round(time.perf_counter() - t0, 2)
        info.setdefault("q", a.q)
        info["size"] = a.block_size
        with open(os.path.join(data_dir, stem + ".info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f)
    else:
        log(f"reusing the data in {data_dir}")
    for tx, ty in built:
        if os.path.isfile(os.path.join(data_dir, f"roads_{a.aid}_{tx}_{ty}.json")) and a.reuse:
            continue
        blockworker = earth_state(a, log)
        log(f"building road tile ({tx}, {ty}) ...")
        t0 = time.perf_counter()
        blockworker.roads_tile(a.aid, a.anchor[0], a.anchor[1], HM_ZOOM, tx, ty, data_dir, "/vfs", True, 0.0,
                               buildings=not a.no_buildings, left_hand=drives_left(a.anchor[0], a.anchor[1], log), mph=False,
                               q=a.q, gas_stations=True)
        timing["tiles_s"] = round(timing.get("tiles_s", 0.0) + time.perf_counter() - t0, 2)
        timing["tiles"] = timing.get("tiles", 0) + 1
    if timing:              # (Earth's own time to build the place: what a player waits for, the eval's timing table)
        with open(timing_path, "w", encoding="utf-8") as f:
            json.dump(timing, f)
    z, holes, info = load_block_files(data_dir, a.aid, cx, cy)
    n = z.shape[0]
    return z, holes, info, cx - n / 2.0, cy - n / 2.0, (cx, cy), tiles, data_dir


def drives_left(lat, lon, log):
    """Whether traffic drives on the left at (lat, lon): its country's road rules (Earth's navgraph.border_country, road_rules),
    as the server sets its tiles' left_hand. Built right-handed, Japan's link decks stood mirrored, 1.3-1.4 m to the right of
    their map lines, at Daikoku. False when the country can't be told."""
    try:
        import navgraph
        c = navgraph.border_country(float(lat), float(lon))
        if c:
            return bool(navgraph.road_rules(c)["rightHandDrive"])
    except Exception as e:      # (no border data: right-handed, as before)
        log(f"  driving side not known ({type(e).__name__}: {e}): right")
    return False


def read_tiles(data_dir, aid, tiles, kinds, log):
    """{kind: triangles (n, 3, 3)} of the road tiles' meshes (the .cdae files read back with Earth's cdae)."""
    import cdae
    got = {}
    for tx, ty in tiles:
        p = os.path.join(data_dir, f"roads_{aid}_{tx}_{ty}.json")
        if not os.path.isfile(p):
            log(f"no tile file {os.path.basename(p)}")
            continue
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for kind, shape, pos in tile_entries(data):
            if kind not in kinds:
                continue
            sp = os.path.join(data_dir, shape)
            if not os.path.isfile(sp):
                continue
            try:
                t = shape_triangles(cdae.read(sp), pos)
            except Exception as e:      # (a shape this reader does not know: left out, said)
                log(f"{shape}: not read ({type(e).__name__}: {e})")
                continue
            got.setdefault(kind, []).append(t)
    return {k: np.concatenate(v) for k, v in got.items() if v}


def rally_moved(a, frame, z, x0, y0, log):
    """EarthRally's grader (rally_grader.run, as earth_writers.rally_pass calls it) on z: (heights after, moved mask)."""
    sys.path.insert(0, os.path.join(a.earthrally, "tools"))
    import rally_grader
    earth_state(a, log)             # (the grader reads the tunnels from Earth's map data)
    geoms = rally_grader.load_geometries(a.geometry)
    if not geoms:
        log("--rally needs --geometry (the stage lines the grader shapes along): nothing moved")
        return z, np.zeros(z.shape, bool)
    n = z.shape[0]
    tunnels = rally_grader.tunnel_points(frame, (x0, y0, x0 + z.shape[1], y0 + n))
    log(f"rally grader: {len(geoms)} stage lines, {len(tunnels)} tunnel points, running shape.lua ...")
    shaping = None
    if a.rally_edition:
        with open(a.rally_edition, encoding="utf-8") as f:
            shaping = json.load(f).get("shaping")
    res = rally_grader.run(z, x0, y0, frame, geoms, tunnels, shaping)
    after = res["heights"]
    moved = np.abs(after - z) >= 0.02
    log(f"rally grader: {int(moved.sum())} samples moved 0.02 m or more, largest {np.abs(after - z).max():.2f} m")
    return after, moved


def stage_lines(paths, frame, z, x0, y0, box, mode):
    """The stage lines' 3D polylines (anchor frame) near box. mode 'geometry': the geometry's own heights (ENU of the anchor);
    'ground': the block's ground."""
    out = []
    for gp in paths:
        with open(gp, encoding="utf-8") as f:
            pts = np.asarray(json.load(f)["points"], np.float64)
        e, n, u = frame.geodetic_to_enu(pts[:, 0], pts[:, 1], pts[:, 2] if pts.shape[1] > 2 else 0.0)
        e, n, u = np.asarray(e), np.asarray(n), np.asarray(u)
        if mode == "ground":
            u = bilinear(z, x0, y0, e, n) + 0.2
        near = (e >= box[0] - 30) & (e <= box[2] + 30) & (n >= box[1] - 30) & (n <= box[3] + 30)
        if near.sum() >= 2:
            lo, hi = np.nonzero(near)[0][[0, -1]]
            out.append(np.stack([e[lo:hi + 1], n[lo:hi + 1], u[lo:hi + 1]], axis=1))
    return out


def joined_pairs(argv, names=("--anchor", "--at", "--xy", "--block")):
    """argv with "--xy -446,880" turned into "--xy=-446,880" (argparse takes a value starting with a minus for an option)."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in names and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--earth", default=os.environ.get("BEAMNG_EARTH_DIR"), help="Earth source tree (its server/); read only")
    ap.add_argument("--anchor", required=True, type=parse_pair, help="lat,lon of the anchor")
    ap.add_argument("--aid", type=int, default=2, help="the anchor's id")
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--at", type=parse_pair, help="lat,lon of the point")
    where.add_argument("--xy", type=parse_pair, help="x,y of the point in the anchor frame (m, x east, y north)")
    ap.add_argument("--radius", type=float, default=40.0, help="m around the point drawn (default 40: close up; 120 for the place)")
    ap.add_argument("--out", default="earth-view")
    ap.add_argument("--name", default="earth_view", help="the pictures' file name stem")
    ap.add_argument("--geometry", action="append", default=[], help="a EarthRally stage geometry json drawn as a thin dark line; repeatable")
    ap.add_argument("--line-z", choices=("geometry", "ground"), default="geometry",
                    help="the stage line at its own heights (default), or 0.2 m over the block's ground")
    ap.add_argument("--rally", action="store_true", help="run EarthRally's ground grader (rally_grader.py) on the block along the "
                                                         "--geometry stages and colour the ground it moves magenta")
    ap.add_argument("--rally-edition", help="the edition's legs.json, for its shaping limits")
    ap.add_argument("--earthrally", help="an EarthRally checkout: --rally runs its tools/rally_grader.py")
    ap.add_argument("--views", default="overview,top,SW,SE,N",
                    help="comma list: overview (1 km round the point in 3D, from the south-west), top, compass points or bearings "
                         "(camera side); default overview,top,SW,SE,N")
    ap.add_argument("--compare-data", help="another build's <out>/data of the same point (earth_eval: before): the overview "
                    "marks the ground that rose or fell CHANGED_M or more since then")
    ap.add_argument("--elev", type=float, default=35.0, help="the oblique cameras' elevation above the horizon (deg)")
    ap.add_argument("--image", default="960x640", help="one view's pixels, WxH (the views are put two per row, three "
                    "for five or six views, a title and legend added around them)")
    ap.add_argument("--separate", action="store_true", help="also write each view as a picture of its own")
    ap.add_argument("--no-vehicles", action="store_true", help="no reference trucks and wagons on the roads and railways")
    ap.add_argument("--dump", action="store_true", help="also write each view's map (<name>_panel_<view>.png) and the surface "
                    "grid (<name>_surface.npz) for server/evals/earth_eval.py")
    ap.add_argument("--label-prefix", default="", help="put before each view's label (earth_eval: \"before: \")")
    ap.add_argument("--ss", type=int, default=2, help="supersampling per axis (antialiasing)")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                    help="mesh kinds drawn: " + ", ".join(DEFAULT_KINDS) + "; also wire, signs, bldcol, railcol, walkcol, buscol, ...")
    ap.add_argument("--no-buildings", action="store_true", help="build the road tiles without buildings (faster)")
    ap.add_argument("--no-holes", action="store_true", help="draw the ground over the .ter's tunnel holes too")
    ap.add_argument("--bias", type=float, default=DEPTH_BIAS_M, help="m the meshes lying on the ground are drawn nearer than it")
    ap.add_argument("--reuse", action="store_true", help="draw from the data a former run left in <out>/data, building nothing")
    ap.add_argument("--block", type=parse_pair, help="cx,cy of the block (a multiple of 128; default: the one nearest the point)")
    ap.add_argument("--block-size", type=int, default=1024, help="the block's samples per edge (the server's layout)")
    ap.add_argument("--q", type=float, help="the anchor's height quantum (default: the server log's, else 1/32)")
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "beamng-earth"))
    ap.add_argument("--server-log", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "beamng-earth", "logs", "server.log"))
    ap.add_argument("--no-airports", action="store_true")
    ap.add_argument("--no-sea", action="store_true")
    a = ap.parse_args(joined_pairs(sys.argv[1:]))
    if a.rally and not a.earthrally:
        ap.error("--rally needs --earthrally (the grader is EarthRally's)")
    if not a.earth:
        sys.exit("--earth (or BEAMNG_EARTH_DIR) is needed")
    a.earth = os.path.abspath(a.earth)
    a.out = os.path.abspath(a.out)
    a.geometry = [os.path.abspath(g) for g in a.geometry]
    os.makedirs(a.out, exist_ok=True)

    def log(msg):
        print(msg, flush=True)

    os.environ["BNGE_BLOCKCACHE"] = "off"       # (a block from Earth's disk cache of graded blocks would skip the build)
    import earth_trace
    earth_trace.load_earth(a.earth)
    import railways
    CHECKS["rail_tunnels"] = bool(railways.TUNNELS_ON)     # (a railway's tunnel tube is drawn only with it)
    import geodesy
    lat0, lon0 = a.anchor
    frame = geodesy.EnuFrame(lat0, lon0)
    if a.at:
        e, n, _ = frame.geodetic_to_enu(a.at[0], a.at[1], 0.0)
        px, py = float(np.asarray(e)), float(np.asarray(n))
        la, lo = a.at
    else:
        px, py = a.xy
        la, lo, _ = frame.enu_to_geodetic(px, py, 0.0)
        la, lo = float(np.asarray(la)), float(np.asarray(lo))
    log(f"point ({px:.1f}, {py:.1f}) = {la:.7f}, {lo:.7f}; radius {a.radius:g} m")

    views = [s_ for s_ in a.views.split(",") if s_.strip()]
    want_ov = any(is_overview(v) for v in views)
    z, holes, info, x0, y0, blk, tiles, data_dir = build_earth(a, frame, px, py, log, OVERVIEW_M if want_ov else None)
    box = (px - a.radius, py - a.radius, px + a.radius, py + a.radius)
    if not (x0 + 1 < box[0] and box[2] < x0 + z.shape[1] - 2 and y0 + 1 < box[1] and box[3] < y0 + z.shape[0] - 2):
        log(f"warning: the radius leaves the block {blk} ({x0:g}..{x0 + z.shape[1] - 1:g}, {y0:g}..{y0 + z.shape[0] - 1:g}): the ground is cut there")
    moved = None
    if a.rally:
        z, moved = rally_moved(a, frame, z, x0, y0, log)
    tris, cls = terrain_mesh(z, x0, y0, box, None if a.no_holes else holes, moved)
    kinds = tuple(k.strip() for k in a.kinds.split(",") if k.strip())
    meshes = read_tiles(data_dir, a.aid, tiles, kinds, log)
    T, C = [tris], [cls]
    for kind, t in sorted(meshes.items()):
        t = crop_triangles(t, box)
        log(f"  {kind}: {len(t)} triangles in the box")
        T.append(t)
        C.append(np.full(len(t), KIND_CLASS.get(kind, OTHER), np.int8))
    tris, cls = np.concatenate(T), np.concatenate(C)
    surface = (tris, cls)           # (the surface grid: without the vehicles, before the checks' colours)
    checks = {}
    map_l, limits = map_lines(a, frame, box, log)
    if map_l is not None:
        log(f"  map lines: {len(map_l['road'])} roads, {len(map_l['rail'])} railways")
        cut, xings = earth_checks.penetrations(tris, cls, map_l, CHECKS)
        in_box = [c for c in xings if box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]]
        checks.update(crossings=len(in_box), crossings_cut=earth_checks.count_cut(xings, box))
        log(f"  crossings of the map's lines at different levels: {checks['crossings']}, "
            f"{checks['crossings_cut']} with meshes cutting each other (red)")
        for line in named_cuts(xings, map_l, frame, box):
            log(line)
        cls = np.where(cut, CUT, cls).astype(np.int8)
    if not a.no_vehicles:
        if limits:
            log(f"  height limits: " + ", ".join(f"{h:g} m" for h in sorted({h for _l, h in limits})))
        vt, vc = reference_vehicles(tris, surface[1], limits, map_l, ground=lambda x, y: bilinear(z, x0, y0, x, y))
        nv = int(np.isin(vc, (TRUCK, WAGON)).sum()) // 12
        nv_t = int((vc == TRUCK).sum()) // 12
        hb = np.isin(surface[1], HIT_BY)
        hits = earth_checks.vehicle_hits(vt[:nv * 12], tris[hb])
        names = ["truck" if vc[12 * k] == TRUCK else "wagon" for k in range(nv)]    # (before the hits are marked HIT; not kinds:
        # the overview reads its tiles' meshes of those)
        vc = vc.copy()
        vc[:nv * 12][np.repeat(hits, 12)] = HIT
        checks.update(vehicles=nv, vehicles_hit=int(hits.sum()))
        if hits.any():                  # (each one: where, what it cuts, how deep: the cases to analyse)
            checks["hits"] = earth_checks.hit_report(vt[:nv * 12], names, tris[hb], surface[1][hb], hits,
                                                     {k: CLASS_NAME[k].split(" (")[0].split(",")[0] for k in HIT_BY})
            for line in named_hits(checks["hits"], map_l, frame):
                log(line)
        log(f"  reference vehicles: {nv} ({nv_t} trucks, {nv - nv_t} wagons), {int(hits.sum())} hitting Earth's geometry not "
            f"their own (yellow)"
            + (f"; {int((vc == MISSING).sum()) // 12} points of the map's roads and railways with nothing drawn under them"
               if (vc == MISSING).any() else ""))
        tris, cls = np.concatenate([tris, vt]), np.concatenate([cls, vc])
    log(f"  ground: {int((cls <= MOVED).sum())} triangles; total {len(tris)}")
    pz = float(bilinear(z, x0, y0, px, py))
    lines = stage_lines(a.geometry, frame, z, x0, y0, box, a.line_z)
    W, H = (int(v) for v in a.image.lower().split("x"))
    ver = earth_version(a.earth)
    tree = os.path.basename(a.earth.rstrip("/\\"))
    title = f"point {la:.7f}, {lo:.7f}  (x {px:.1f}, y {py:.1f}, ground z {pz:.1f})  radius {a.radius:g} m   Earth: {tree} ({ver})"
    sub = f"anchor {a.aid} at {lat0}, {lon0}; block ({blk[0]:g}, {blk[1]:g}); tiles " + ", ".join(f"{tx}_{ty}" for tx, ty in tiles) + \
        ("; rally grader applied" if a.rally else "")
    corners = np.array([[box[i], box[j], zz] for i in (0, 2) for j in (1, 3)
                        for zz in (float(tris[:, :, 2].min()), float(tris[:, :, 2].max()))])
    target = np.array([px, py, pz])
    panels, used_all = [], set()
    for v in views:
        if is_overview(v):
            ov_box = overview_box(px, py)
            if not (x0 <= ov_box[0] and ov_box[2] <= x0 + z.shape[1] - 1 and y0 <= ov_box[1] and ov_box[3] <= y0 + z.shape[0] - 1):
                log(f"  overview: part of the {2 * OVERVIEW_M:g} m square is off the block {blk}: no ground there")
            dz = compare_heights(a.compare_data, a.aid, blk[0], blk[1], z, log) if a.compare_data else None
            ov_meshes = read_tiles(data_dir, a.aid, tiles_covering(px, py, OVERVIEW_M), kinds, log)
            ot, oc = overview_scene(z, x0, y0, ov_box, None if a.no_holes else holes, ov_meshes, dz)
            ov_map, _ = map_lines(a, frame, ov_box, log, margin=0.0)
            if ov_map is not None:
                base_cls = np.where(np.isin(oc, (ROSE, FELL)), GROUND, oc)
                ocut, oxings = overview_cuts(z, x0, y0, None if a.no_holes else holes, ot, base_cls, ov_map)
                oc = np.where(ocut, CUT, oc).astype(np.int8)
                checks.update(overview_crossings=len(oxings), overview_crossings_cut=earth_checks.count_cut(oxings))
                log(f"  overview: {len(oxings)} crossings at different levels, {checks['overview_crossings_cut']} cutting (red)")
                for line in named_cuts(oxings, ov_map, frame, ov_box, "overview cut"):
                    log(line)
            log(f"  overview: {len(ot)} triangles" + (f", {int((oc == ROSE).sum())} of rising ground and {int((oc == FELL).sum())}"
                                                     f" of falling ground since --compare-data" if dz is not None else ""))
            cam = overview_camera(ot, target, W, H)
            ov_lines = [box_outline(z, x0, y0, box)] + stage_lines(a.geometry, frame, z, x0, y0, ov_box, a.line_z)
            img, used = render(ot, oc, cam, lines=ov_lines, needle=(px, py, pz, pz + 60.0), ss=a.ss, bias=a.bias)
            label = a.label_prefix + f"overview {2 * OVERVIEW_M / 1000:g} km, from SW {OVERVIEW_ELEV:g} deg; box: close-up"
            panels.append(annotate_panel(img, cam, label))
            used_all.update(int(u) for u in used)
            if a.dump:
                panels[-1].save(os.path.join(a.out, f"{a.name}_panel_overview.png"))
            if a.separate:
                path = os.path.join(a.out, f"{a.name}_overview.png")
                frame_picture(img, cam, title, sub, used, True, True, label=label).save(path)
                log(f"wrote {path}")
            continue
        top = v.strip().lower() == "top"
        cam = view_camera(target, 0.0 if top else parse_bearing(v), 90.0 if top else a.elev, W, H)
        fit_camera(cam, corners)
        img, used = render(tris, cls, cam, lines=lines, needle=(px, py, pz, pz + 0.25 * a.radius), ss=a.ss, bias=a.bias)
        label = a.label_prefix + ("top-down, north up" if top else f"from {v.strip().upper()}, {a.elev:g} deg above the horizon")
        panels.append(annotate_panel(img, cam, label))
        if a.dump:
            panels[-1].save(os.path.join(a.out, f"{a.name}_panel_{view_name(v)}.png"))
        used_all.update(int(u) for u in used)
        if a.separate:
            path = os.path.join(a.out, f"{a.name}_{view_name(v)}.png")
            frame_picture(img, cam, title, sub, used, bool(lines), True, label=label).save(path)
            log(f"wrote {path}")
    if not panels:
        sys.exit("--views names no view")
    path = os.path.join(a.out, f"{a.name}.png")
    compose(panels, title, sub, legend_entries(sorted(used_all), bool(lines) or want_ov, True),
            cols=3 if len(panels) in (5, 6) else 2).save(path)
    log(f"wrote {path}")
    if a.dump:
        path = os.path.join(a.out, f"{a.name}_checks.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(checks, f, indent=1)
        log(f"wrote {path}")
        c, zs = surface_grid(*surface, box)
        path = os.path.join(a.out, f"{a.name}_surface.npz")
        np.savez_compressed(path, cls=c, z=zs, box=np.array(box, np.float64), step=np.float64(SURFACE_STEP_M),
                            point=np.array([px, py, pz]), used=np.array(sorted(used_all), np.int64),
                            title=np.array(title), subtitle=np.array(sub))
        log(f"wrote {path}")

if __name__ == "__main__":
    main()
