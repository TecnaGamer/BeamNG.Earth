"""portalview.py PLACE|LAT,LON [R] [--out DIR] [--at LAT,LON] [--cache DIR] [--tag T]: every tunnel mouth round a
place drawn as the server builds it, without the game: the graded ground on the terrain's lattice with the tunnels'
holes cut (the engine's rule: a square goes with its south-west sample), the tunnel mesh of the place (roadmesh.tunnel
on roads._tunnel_chunks: tubes, portals, retaining walls) and the floors, through a small software z-buffer; two views
per mouth (from the approach at driver's height, and from above to one side) in one image <out>/<place>_<n>_<T>.jpg.
For looking at an entrance change in seconds (2026-11-12, the user: "focus on tunnel entrances"); the game is still
the judge (camshot.py at the same spot). --at: only the mouth nearest that point. A scratch cache by default."""
import logging
import math
import multiprocessing
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
sys.path.insert(0, HERE)

W, H = 800, 450
HALF = 56.0         # m of ground drawn round a mouth
SQUARE = 1.0
MAT_RGB = {         # the materials' flat colours here
    "terrain": (96, 112, 70), "rock": (118, 112, 104), "road": (62, 62, 66), "hole": (0, 0, 0),
    "bne_tunnel_inner": (70, 70, 72), "bne_tunnel_light": (255, 244, 200), "bne_tunnel_stone": (128, 122, 112),
    "bne_tunnel_stone_in": (84, 80, 74), "bne_tunnel_soil": (88, 108, 62), "bne_wall_scree": (120, 114, 104),
    "bne_portal_rock": (104, 98, 92), "bne_portal_rail": (60, 64, 70),
}
CONCRETE = (170, 168, 162)


def render(tris, cols, eye, look, fov=62.0, sky=(170, 200, 232)):
    """tris (n, 3, 3) world triangles, cols (n, 3) their colours: an image (H, W, 3) from eye looking at look, both
    sides drawn, a sun from the south-west. Pure."""
    import numpy as np
    f = look - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, [0.0, 0.0, 1.0])
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    V = tris - eye
    cam = np.stack([V @ r, V @ u, V @ f], -1)
    near = 0.3
    keep = (cam[:, :, 2] > near).all(1)         # (no clipping: a triangle through the near plane is left out)
    cam, cols, tris = cam[keep], cols[keep], tris[keep]
    k = (W / 2) / math.tan(math.radians(fov) / 2)
    sx = W / 2 + k * cam[:, :, 0] / cam[:, :, 2]
    sy = H / 2 - k * cam[:, :, 1] / cam[:, :, 2]
    nrm = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(nrm, axis=1)
    nrm /= np.maximum(ln, 1e-12)[:, None]
    sun = np.array([-0.45, -0.35, 0.82])
    sun /= np.linalg.norm(sun)
    toeye = eye - tris.mean(1)
    flip = (nrm * toeye).sum(1) < 0
    nrm[flip] *= -1
    shade = 0.45 + 0.55 * np.clip(nrm @ sun, 0.0, 1.0)
    dist = np.linalg.norm(toeye, axis=1)
    fog = np.clip(dist / 900.0, 0.0, 0.6)
    rgb = cols * shade[:, None] * (1 - fog[:, None]) + np.array(sky) * fog[:, None]
    img = np.empty((H, W, 3), np.float32)
    img[:] = sky
    zb = np.full((H, W), np.inf, np.float32)
    iz = 1.0 / cam[:, :, 2]
    for t in range(len(cam)):
        x, y = sx[t], sy[t]
        x0, x1 = max(int(math.floor(x.min())), 0), min(int(math.ceil(x.max())), W - 1)
        y0, y1 = max(int(math.floor(y.min())), 0), min(int(math.ceil(y.max())), H - 1)
        if x0 > x1 or y0 > y1:
            continue
        d = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
        if abs(d) < 1e-9:
            continue
        px, py = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        a = ((y[1] - y[2]) * (px - x[2]) + (x[2] - x[1]) * (py - y[2])) / d
        b = ((y[2] - y[0]) * (px - x[2]) + (x[0] - x[2]) * (py - y[2])) / d
        c = 1 - a - b
        ins = (a >= -1e-4) & (b >= -1e-4) & (c >= -1e-4)
        if not ins.any():
            continue
        z = 1.0 / (a * iz[t, 0] + b * iz[t, 1] + c * iz[t, 2])
        sub = zb[y0:y1 + 1, x0:x1 + 1]
        ok = ins & (z < sub)
        sub[ok] = z[ok]
        img[y0:y1 + 1, x0:x1 + 1][ok] = rgb[t]
    return np.clip(img, 0, 255).astype(np.uint8)


def main():
    import numpy as np
    from PIL import Image
    import tunnelscan
    args = sys.argv[1:]

    def opt(name, default=None):
        if name in args:
            k = args.index(name)
            v = args[k + 1]
            del args[k:k + 2]
            return v
        return default
    out = os.path.expanduser(opt("--out", "~/bnge-tests/portals/view"))
    cache = os.path.expanduser(opt("--cache", "~/bnge-tests/tunnels/cache"))
    tag = opt("--tag", "v")
    at = opt("--at")
    if os.path.realpath(cache) == os.path.realpath(os.path.expanduser("~/.cache/beamng-earth")):
        sys.exit("not the user's cache")
    os.makedirs(out, exist_ok=True)
    name = args[0]
    if name in tunnelscan.PLACES:
        lat, lon, R = tunnelscan.PLACES[name]
    else:
        lat, lon = (float(v) for v in name.split(","))
        R = 300.0
        name = "at"
    if len(args) > 1:
        R = float(args[1])
    import blockworker
    import geodesy
    import grading as gradingmod
    import roadmesh
    import roads
    blockworker.init(cache, False, True, True, True, False, None, logging.ERROR, False, True)
    s = blockworker._s
    g, _ = s.anchor(1, lat, lon)
    frame = geodesy.EnuFrame(lat, lon)

    def to_ll(xy):
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return np.asarray(la), np.asarray(lo)

    def elev(la, lo):
        z = blockworker.ground(frame, la, lo, s.dem.sample(la, lo, 15))
        return g.apply(la, lo, z, gradingmod.ROAD_LIFT) if g is not None else z

    def graded_xy(xy):
        la, lo = to_ll(xy)
        return np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)

    tun = roads.TunnelProfiles(frame, lambda key: s.md.cell(key, layers=("roads",), kind="demand").layers.get("roads"),
                               elev)
    sea = getattr(s, "sea", None)
    tun.wet = sea.depth if sea is not None else None
    tun.water = sea.water if sea is not None else None
    tun.raw = lambda la, lo: blockworker.ground(None, la, lo, s.dem.sample(la, lo, gradingmod.PROFILE_ZOOM))
    if g is not None:
        tun.ends = lambda la, lo: g.apply(la, lo, tun.raw(la, lo), gradingmod.ROAD_LIFT, roads_only=True)
    cellkeys = {}
    for tx, ty in roads.tiles_near(0.0, 0.0, R + 100):
        for k in roads.cells_of_tile(frame, tx, ty):
            if str(k) not in cellkeys:
                cellkeys[str(k)] = s.md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
    cells = list(cellkeys.values())
    box = (-R, -R, R, R)
    parts = tun.parts(cells, box)
    mouths = []
    for fid, spec, xy, z, _lon in parts:
        xy = np.asarray(xy, np.float64)[:, :2]
        if len(xy) < 2:
            continue
        for e, (a, b) in enumerate(((0, 1), (-1, -2))):
            if tun.mouth(fid)[e] and np.abs(xy[a]).max() < R:
                t = xy[a] - xy[b]
                mouths.append((fid, spec, xy[a], t / max(float(np.hypot(*t)), 1e-9), float(np.asarray(z)[a])))
    if at:
        ala, alo = (float(v) for v in at.split(","))
        ae, an, _ = frame.geodetic_to_enu(ala, alo, 0.0)
        mouths.sort(key=lambda m: float(np.hypot(m[2][0] - ae, m[2][1] - an)))
        mouths = mouths[:1]
    print(f"{name}: {len(parts)} covered sections, {len(mouths)} mouths")
    for n, (fid, spec, p, tout, zf) in enumerate(mouths):
        cx, cy = (np.round(p / 8.0) * 8.0).tolist()
        ex = (cx - HALF - 8, cy - HALF - 8, cx + HALF + 8, cy + HALF + 8)
        chunks = roads._tunnel_chunks(tun, cells, ex)
        walls = []                  # (the cuttings' retaining walls need the tile's graded roads: not drawn here)
        mesh = roadmesh.tunnel(chunks, np.zeros(3), walls)
        nn = int(2 * HALF / SQUARE)
        ax = cx - HALF + np.arange(nn + 1) * SQUARE
        ay = cy - HALF + np.arange(nn + 1) * SQUARE
        gx, gy = np.meshgrid(ax, ay)
        u = graded_xy(np.c_[gx.ravel(), gy.ravel()]).reshape(gx.shape)
        near_parts = tun.parts(cells, ex)
        # (the low arches' soil raised over their vaults, as the terrain blocks': roads.block_arch_raise)
        u = roads.arch_raise(near_parts, tun.heights, gx.ravel(), gy.ravel(), u.ravel(), tun.mouths).reshape(gx.shape)
        hole = roads.tunnel_hole_mask(near_parts, u[:-1, :-1].copy(), cx, cy, SQUARE, tun.heights, tun.mouths)
        if hole is None:
            hole = np.zeros((nn, nn), bool)
        tris, cols = [], []
        P3 = np.stack([gx, gy, u], -1)
        for iy in range(nn):
            for ix in range(nn):
                if hole[iy, ix]:
                    continue
                q = (P3[iy, ix], P3[iy, ix + 1], P3[iy + 1, ix + 1], P3[iy + 1, ix])
                slope = max(abs(q[0][2] - q[2][2]), abs(q[1][2] - q[3][2])) / (1.41 * SQUARE)
                c = MAT_RGB["rock"] if slope > 0.9 else MAT_RGB["terrain"]
                tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
                cols += [c, c]
        if mesh is not None:
            Pm, Tm, _uv, mats, Mi = mesh[:5]
            for tri, mi in zip(Tm, Mi):
                tris.append(tuple(Pm[tri]))
                cols.append(MAT_RGB.get(mats[int(mi)], CONCRETE))
        for c in chunks:                # the floors (and the approach's surface inside the box as the terrain's)
            if not c.get("deck"):
                continue
            for left, right, _v, _a, _b, z in roadmesh.ribbons([c]):
                if z is None:
                    continue
                for i in range(len(left) - 1):
                    q = [np.r_[left[i], z[i] + 0.02], np.r_[right[i], z[i] + 0.02], np.r_[right[i + 1], z[i + 1] + 0.02],
                         np.r_[left[i + 1], z[i + 1] + 0.02]]
                    tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
                    cols += [MAT_RGB["road"], MAT_RGB["road"]]
        T = np.asarray(tris, np.float64)
        C = np.asarray(cols, np.float64)
        nrm = np.array([-tout[1], tout[0]])
        target = np.r_[p, zf + 3.0]
        zc = lambda q: float(graded_xy(np.asarray(q)[None])[0])      # noqa: E731
        e1 = p + tout * 30.0 + nrm * 1.5
        e2 = p + tout * 38.0 + nrm * 26.0
        views = [np.r_[e1, max(zc(e1), zf) + 2.2], np.r_[e2, max(zc(e2), zf) + 24.0]]
        ims = [render(T, C, v, target.copy()) for v in views]
        la, lo = to_ll(p)
        sheet = Image.new("RGB", (2 * W, H))
        for k, im in enumerate(ims):
            sheet.paste(Image.fromarray(im), (k * W, 0))
        fn = os.path.join(out, f"{name}_{n:02d}_{tag}.jpg")
        sheet.save(fn, quality=88)
        print(f"  {n} {fid[:18]} {spec.cls} th {tun.height(fid):.1f} at {float(la[0]):.6f},{float(lo[0]):.6f} -> {fn}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
