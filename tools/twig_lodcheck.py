#!/usr/bin/env python3
"""The winter bare trees' detail levels, checked offline (the user, 2026-11-27: "the tree LODs still pop a lot when
rendering leafless trees"; winter W4, baretrees.lua).

A stock tree shape draws its crown with leaf cards at each detail level (East Coast USA's oaks: a1000 / a600 with the
near leaf atlas, a200 with the coarser "distant" atlas, then the engine's imposter baked from a200); the bare copies
draw our twig cards there instead (tools/make_twig_textures.py). Fine twigs are where alpha-tested cards go wrong: the
GPU's mips average them into a faint haze and the alpha test drops it, so a crown thins out as it recedes and then pops
to the far level, whose bigger cards (and lower alphaRef) draw a dense dark crown. This renders each detail level of a
shape orthographically, as the GPU would (alpha test at the material's alphaRef, trilinear box mips with an anisotropic
mip choice, or the coverage-preserving mips of the manifest's "coverage" derive: texdds.rs coverage_levels), at the
screen size where the engine switches to the next level, and reports the crowns' drawn area and colour across each
switch: a ratio far from 1 is a pop.

The engine's choice (TSShapeInstance::setDetailFromDistance): the detail whose size the shape's projected radius in
pixels, times the viewport's aspect x 2 (and the mesh detail setting), reaches; K below is that factor (16:9: 3.56).

    python3 tools/twig_lodcheck.py SHAPE.dae [...] [--plain] [--out DIR] [--azims 0,90]

SHAPE.dae: a stock tree's COLLADA file beside its main.materials.json (extracted from the game's level zip); the stock
branch and bark textures are read from the game's content/assets/materials/tree.zip (nothing of them is written out).
numpy + PIL; deterministic.
"""
import argparse
import io
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
BARE = "levels/beamng_earth/art/shapes/beamngEarth/bare/"
SOURCES = os.path.join(ROOT, "mod", "texture_sources")
BARETREES = os.path.join(ROOT, "mod/beamng_earth/lua/ge/extensions/beamngEarth/baretrees.lua")
K = 3.56
SKY = (0.68, 0.77, 0.88)


def game_dir():
    for c in (os.environ.get("BNGEARTH_GAME_DIR"), os.path.expanduser("~/.steam/steam/steamapps/common/BeamNG.drive"),
              os.path.expanduser("~/.local/share/Steam/steamapps/common/BeamNG.drive")):
        if c and os.path.isfile(os.path.join(c, "content", "assets", "materials", "tree.zip")):
            return c
    return None


def bare_table():
    """baretrees.lua's RENAME (stock leaf material -> twig texture), FACTOR, ALPHA_REF and ALPHA_REF_FAR."""
    src = open(BARETREES, encoding="utf-8").read()
    rename = dict(re.findall(r"(\w+) = \{ '[\w]+', '(\w+)' \}", src.split("M.RENAME = {", 1)[1].split("\n}", 1)[0]))
    factor = [float(x) for x in re.search(r"M\.FACTOR = \{([^}]*)\}", src).group(1).split(",")][:3]
    near = int(re.search(r"M\.ALPHA_REF = (\d+)", src).group(1))
    far = int(re.search(r"M\.ALPHA_REF_FAR = (\d+)", src).group(1))
    return rename, factor, near, far


def manifest_derive(tex):
    m = json.load(open(os.path.join(SOURCES, "textures.json"), encoding="utf-8"))["textures"]
    return m.get(BARE + "bne_twig_%s_o.data.dds" % tex, {}).get("derive", "")


def box_mips(a):
    out = [a]
    while min(out[-1].shape[:2]) > 1:
        x = out[-1]
        x = x[:x.shape[0] // 2 * 2, :x.shape[1] // 2 * 2]
        out.append((x[0::2, 0::2] + x[1::2, 0::2] + x[0::2, 1::2] + x[1::2, 1::2]) / 4)
    return out


def coverage_mips(levels, ref, scale=1.0):
    """texdds.rs coverage_levels in floats: each level scaled so its share at or above ref is level 0's x scale."""
    target = float((levels[0] >= ref).mean()) * scale
    out = []
    for i, a in enumerate(levels):
        if (i == 0 and scale == 1.0) or target <= 0:
            out.append(a)
            continue
        q = float(np.quantile(a, 1 - min(target, 1.0)))
        out.append(np.minimum(a * (ref / q), 1.0) if q > 0 else a)
    return out


class Mat:
    def __init__(self, color, opacity, ref, factor=(1, 1, 1), derive="", twig=False):
        self.cm = box_mips(color * np.array(factor, np.float32))
        self.om = box_mips(opacity) if opacity is not None else None
        self.ref = ref / 255.0
        self.twig = twig
        if self.om is not None and derive.startswith("coverage:"):
            parts = derive.split(":")
            self.ref = int(parts[1]) / 255.0
            self.om = coverage_mips(self.om, self.ref, float(parts[2]) if len(parts) > 2 else 1.0)

    def size(self):
        a = (self.om or self.cm)[0]
        return a.shape[1], a.shape[0]

    @staticmethod
    def sample(maps, u, v, lam):
        lam = np.clip(lam, 0, len(maps) - 1)
        l0 = np.floor(lam).astype(int)
        f = lam - l0
        out = None
        for lv, wt in ((l0, 1 - f), (np.minimum(l0 + 1, len(maps) - 1), f)):
            res = np.zeros(u.shape + maps[0].shape[2:], np.float32)
            for L in np.unique(lv):
                m = lv == L
                t = maps[L]
                h, w = t.shape[:2]
                x = (u[m] % 1.0) * w - 0.5
                y = (v[m] % 1.0) * h - 0.5
                x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
                fx, fy = x - x0, y - y0
                x0 %= w
                y0 %= h
                x1, y1 = (x0 + 1) % w, (y0 + 1) % h
                if t.ndim == 3:
                    fx, fy = fx[:, None], fy[:, None]
                res[m] = (t[y0, x0] * (1 - fx) * (1 - fy) + t[y0, x1] * fx * (1 - fy) + t[y1, x0] * (1 - fx) * fy
                          + t[y1, x1] * fx * fy)
            ww = wt if res.ndim == 1 else wt[:, None]
            out = res * ww if out is None else out + res * ww
        return out


def parse_dae(path):
    """{detail size: [(material, positions n x 3 x 3, uvs n x 3 x 2)]} of a shape's detail meshes (node names *_aN)."""
    t = ET.parse(path).getroot()
    geoms = {}
    for g in t.iterfind(".//c:geometry", NS):
        srcs = {}
        for s in g.iterfind(".//c:source", NS):
            fa, acc = s.find("c:float_array", NS), s.find(".//c:accessor", NS)
            if fa is not None and acc is not None:
                srcs[s.get("id")] = np.array(fa.text.split(), np.float64).reshape(-1, int(acc.get("stride")))
        vert = g.find(".//c:vertices", NS)
        vmap = {vert.get("id"): srcs[vert.find('c:input[@semantic="POSITION"]', NS).get("source")[1:]]}
        parts = []
        for tri in list(g.iterfind(".//c:triangles", NS)) + list(g.iterfind(".//c:polylist", NS)):
            if tri.tag.endswith("polylist") and (np.array(tri.find("c:vcount", NS).text.split(), int) != 3).any():
                continue
            ins = tri.findall("c:input", NS)
            stride = max(int(i.get("offset")) for i in ins) + 1
            p = np.array(tri.find("c:p", NS).text.split(), np.int64).reshape(-1, stride)
            pos = uv = None
            for i in ins:
                src, o = i.get("source")[1:], int(i.get("offset"))
                if i.get("semantic") == "VERTEX":
                    pos = vmap[src][p[:, o]]
                elif i.get("semantic") == "TEXCOORD" and uv is None:
                    uv = srcs[src][p[:, o]][:, :2]
            if pos is None or uv is None:
                continue
            parts.append(((tri.get("material") or "").replace("-material", ""), pos.reshape(-1, 3, 3),
                          uv.reshape(-1, 3, 2)))
        geoms[g.get("id")] = parts
    lods = {}

    def walk(n, m):
        mm = n.find("c:matrix", NS)
        if mm is not None:
            m = m @ np.array(mm.text.split(), float).reshape(4, 4)
        mt = re.search(r"_[al](\d+)$", n.get("name") or "")
        for ig in n.findall("c:instance_geometry", NS):
            if mt:
                for mat, pos, uv in geoms.get(ig.get("url")[1:], []):
                    P = pos.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
                    lods.setdefault(int(mt.group(1)), []).append((mat, P.reshape(-1, 3, 3), uv))
        for c in n.findall("c:node", NS):
            walk(c, m)
    for vs in t.iterfind(".//c:visual_scene", NS):
        for n in vs.findall("c:node", NS):
            walk(n, np.eye(4))
    return lods


def materials(dae, plain=False, game=None):
    """The shape's materials as the bare copy draws them: twig cards on our textures, the rest the stock ones."""
    rename, factor, near, far = bare_table()
    js = json.load(open(os.path.join(os.path.dirname(dae), "main.materials.json"), encoding="utf-8"))
    zf = zipfile.ZipFile(os.path.join(game or game_dir(), "content", "assets", "materials", "tree.zip"))

    def stock(path):
        with zf.open(path.lstrip("/").replace(".png", ".dds")) as f:
            im = Image.open(io.BytesIO(f.read()))
            im.load()
        return im
    out = {}
    for name, d in js.items():
        st = (d.get("Stages") or [{}])[0]
        if name in rename:
            tex = rename[name]
            c = np.asarray(Image.open(os.path.join(SOURCES, BARE + "bne_twig_%s_b.color.webp" % tex)).convert("RGB"),
                           np.float32) / 255
            o = np.asarray(Image.open(os.path.join(SOURCES, BARE + "bne_twig_%s_o.data.webp" % tex)).convert("L"),
                           np.float32) / 255
            out[name] = Mat(c, o, far if "distant" in tex else near, factor,
                            "" if plain else manifest_derive(tex), twig=True)
        elif st.get("baseColorMap"):
            try:
                c = np.asarray(stock(st["baseColorMap"]).convert("RGB"), np.float32) / 255
                o = np.asarray(stock(st["opacityMap"]).convert("L"), np.float32) / 255 if st.get("opacityMap") else None
                out[name] = Mat(c, o, d.get("alphaRef") or 0, (st.get("baseColorFactor") or [1, 1, 1])[:3])
            except (KeyError, OSError):
                pass
    return out


def render(tris, mats, px, azim, radius, center, aniso=16):
    """Orthographic side view (azimuth in degrees) of one detail level, the shape's radius px pixels: (rgb, drawn,
    drawn by a twig card)."""
    W = H = int(2 * px) + 4
    s = px / radius
    ca, sa = math.cos(math.radians(azim)), math.sin(math.radians(azim))
    rgb = np.zeros((H, W, 3), np.float32) + np.array(SKY, np.float32)
    zb = np.full((H, W), np.inf, np.float32)
    hit = np.zeros((H, W), bool)
    twig = np.zeros((H, W), bool)
    for mat, P, UV in tris:
        m = mats.get(mat)
        if m is None:
            continue
        Q = P - center
        X = (Q[..., 0] * ca + Q[..., 1] * sa) * s + W / 2
        D = -Q[..., 0] * sa + Q[..., 1] * ca
        Y = H / 2 - Q[..., 2] * s
        tw, th = m.size()
        for k in range(len(P)):
            x, y, d, uv = X[k], Y[k], D[k], UV[k]
            x0, x1 = int(max(0, math.floor(x.min()))), int(min(W - 1, math.ceil(x.max())))
            y0, y1 = int(max(0, math.floor(y.min()))), int(min(H - 1, math.ceil(y.max())))
            det = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
            if x1 < x0 or y1 < y0 or abs(det) < 1e-9:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            l0 = ((y[1] - y[2]) * (gx - x[2]) + (x[2] - x[1]) * (gy - y[2])) / det
            l1 = ((y[2] - y[0]) * (gx - x[2]) + (x[0] - x[2]) * (gy - y[2])) / det
            l2 = 1 - l0 - l1
            ins = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
            if not ins.any():
                continue
            # the texel footprint d(texels)/d(pixels), one per triangle (orthographic): the mip, anisotropic x aniso
            A = np.array([[x[1] - x[0], x[2] - x[0]], [y[1] - y[0], y[2] - y[0]]])
            B = np.array([[(uv[1, 0] - uv[0, 0]) * tw, (uv[2, 0] - uv[0, 0]) * tw],
                          [(uv[1, 1] - uv[0, 1]) * th, (uv[2, 1] - uv[0, 1]) * th]])
            sv = np.linalg.svd(B @ np.linalg.inv(A), compute_uv=False)
            lam = max(math.log2(max(sv[0] / aniso, sv[1], 1e-6)), 0.0)
            u = (l0 * uv[0, 0] + l1 * uv[1, 0] + l2 * uv[2, 0])[ins]
            v = 1 - (l0 * uv[0, 1] + l1 * uv[1, 1] + l2 * uv[2, 1])[ins]
            z = (l0 * d[0] + l1 * d[1] + l2 * d[2])[ins]
            yy, xx = np.nonzero(ins)
            lamv = np.full(u.shape, lam)
            ok = m.sample(m.om, u, v, lamv) >= m.ref if m.om is not None else np.ones(u.shape, bool)
            yy, xx, u, v, z = yy[ok] + y0, xx[ok] + x0, u[ok], v[ok], z[ok]
            if not len(z):
                continue
            col = m.sample(m.cm, u, v, lamv[ok])
            front = z < zb[yy, xx]
            yy, xx = yy[front], xx[front]
            zb[yy, xx] = z[front]
            rgb[yy, xx] = col[front]
            hit[yy, xx] = True
            twig[yy, xx] = m.twig
    return rgb, hit, twig


def check(dae, plain=False, azims=(0.0, 90.0), k=K, out=None, game=None):
    """[(detail, next detail or None, radius px, drawn area this, next, twig area this, next, rgb this, next)] at each
    switch of a shape's detail levels (to the imposter: the last level alone)."""
    lods = parse_dae(dae)
    mats = materials(dae, plain, game)
    sizes = sorted(lods, reverse=True)
    allp = np.concatenate([P.reshape(-1, 3) for _, P, _ in lods[sizes[0]]])
    lo, hi = allp.min(0), allp.max(0)
    center = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - center))
    rows = []
    for i, sz in enumerate(sizes):
        nxt = sizes[i + 1] if i + 1 < len(sizes) else None
        px = sz / k
        st = []
        imgs = []
        for det in [sz] + ([nxt] if nxt else []):
            area = twa = 0
            cols = []
            for az in azims:
                rgb, hit, tw = render(lods[det], mats, px, az, radius, center)
                area += int(hit.sum())
                twa += int(tw.sum())
                cols.append(rgb[hit].mean(0) if hit.any() else np.zeros(3))
                if az == azims[0]:
                    imgs.append(rgb)
            st.append((area, twa, np.mean(cols, 0)))
        rows.append((sz, nxt, px, st))
        if out and nxt:
            os.makedirs(out, exist_ok=True)
            h = max(r.shape[0] for r in imgs)
            im = np.concatenate([np.pad(r, ((0, h - r.shape[0]), (0, 4), (0, 0)), constant_values=1) for r in imgs], 1)
            Image.fromarray((np.clip(im, 0, 1) * 255).astype(np.uint8)).save(
                os.path.join(out, "%s_a%d_a%d%s.png" % (os.path.basename(dae)[:-4], sz, nxt, "_plain" if plain else "")))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dae", nargs="+")
    ap.add_argument("--plain", action="store_true", help="plain box mips (before the coverage derive)")
    ap.add_argument("--azims", default="0,90")
    ap.add_argument("--k", type=float, default=K)
    ap.add_argument("--out")
    a = ap.parse_args()
    if not game_dir():
        sys.exit("BeamNG.drive not found (BNGEARTH_GAME_DIR): the stock bark and branch textures")
    worst = 1.0
    for dae in a.dae:
        print(os.path.basename(dae))
        for sz, nxt, px, st in check(dae, a.plain, tuple(map(float, a.azims.split(","))), a.k, a.out):
            line = "  a%d -> %s at %.0f px: area %d" % (sz, "a%d" % nxt if nxt else "imposter", px, st[0][0])
            if nxt:
                r = st[1][0] / max(st[0][0], 1)
                worst = max(worst, r, 1 / max(r, 1e-6))
                line += " -> %d (x%.2f), twig cards %d -> %d, colour %s -> %s" % (
                    st[1][0], r, st[0][1], st[1][1], " ".join("%.2f" % c for c in st[0][2]), " ".join("%.2f" % c for c in st[1][2]))
            print(line)
    print("worst area ratio across a switch: x%.2f" % worst)


if __name__ == "__main__":
    main()
