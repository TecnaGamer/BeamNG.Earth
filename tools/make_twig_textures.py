#!/usr/bin/env python3
"""Winter twig cards for the bare deciduous trees (winter W4; baretrees.lua): procedural branching, our own pixels.

The stock oaks, beeches, birches / aspens and poplars draw their crowns with leaf cards: each card samples one sprig of
a leaf atlas (several leafy sprigs on one texture). In winter (baretrees.lua) the trees are drawn with copies of the
stock shapes whose leaf materials are renamed to ours, and ours sample the textures made here: the same atlas layout,
every leafy sprig replaced by a bare twig spray of the same footprint, so the crown keeps its volume as a grey-brown
haze of fine twigs (probe P8, 2026-11-24: hiding the cards left mid-distance trees as thin sticks).

How a texture is made (deterministic, numpy + PIL only):
  1. the layout: the sprigs of the stock atlas (its opacity map, read from the player's BeamNG.drive install at build
     time, nothing of it written out): connected regions of the opacity, each with its stem's end (the extreme point
     of the region with the least mass round it: the stalk sticking out);
  2. per sprig a space-colonisation tree (Runions et al. 2007): attractors scattered over the sprig's area, a stalk
     from its stem end growing toward them, every node's thickness from the tips it carries (pipe model), the
     species' habit (oak zig-zag, beech fine and straight, birch drooping, poplar upright), fine spurs at the tips;
  3. drawn anti-aliased (coverage from the distance to each segment) in the species' bark and twig colours; oaks and
     beeches keep some dead leaves (marcescence) on a share of their sprigs, the young beech card on most;
  4. written as the mod's lossless sources (mod/texture_sources, WebP; install.py encodes them into BC7 sRGB colour
     and BC4 opacity, plain box mips: the coarser mips average the fine twigs into a faint haze, and the materials'
     low alphaRef keeps that haze drawn at range instead of dropping the twigs) with their manifest entries
     (made at every install: the settings page's Winter switch is read when the server starts, after the install;
     nothing loads them while winter is off).

  python3 tools/make_twig_textures.py [--game DIR] [--preview OUT.png] [NAME ...]
"""
import argparse
import io
import json
import math
import os
import sys
import zipfile

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
import modtextures  # noqa: E402

REL = "levels/beamng_earth/art/shapes/beamngEarth/bare"
ALPHA_REF = 100         # the materials' alphaRef (baretrees.lua MATERIAL): the preview thresholds with it
TREE_ZIP = ("content", "assets", "materials", "tree.zip")

# species habits: twig colours (sRGB: bark at the thick end, the twig tips), dead leaf colours, growth
HABITS = {
    "oak": dict(bark=(66, 61, 56), tip=(92, 76, 63), dead=((118, 78, 44), (96, 66, 40), (134, 96, 58)),
                jitter=0.55, tropism=(0.0, 0.0), r_tip=0.55, r_max=4.0, spur=0.7, step=4.0, spacing=9.0),
    "beech": dict(bark=(104, 99, 92), tip=(112, 88, 70), dead=((168, 108, 56), (150, 94, 48), (182, 128, 74)),
                  jitter=0.32, tropism=(0.0, 0.0), r_tip=0.5, r_max=3.5, spur=0.9, step=4.0, spacing=7.0),
    "birch": dict(bark=(150, 140, 132), tip=(92, 58, 54), dead=(), jitter=0.25, tropism=(0.0, 0.22), r_tip=0.45,
                  r_max=3.0, spur=1.0, step=3.5, spacing=5.5),
    "poplar": dict(bark=(110, 104, 92), tip=(98, 88, 76), dead=(), jitter=0.25, tropism=(0.0, -0.25), r_tip=0.5,
                   r_max=3.5, spur=0.7, step=4.0, spacing=7.0),
}

# name -> (stock opacity map in tree.zip, size, habit, share of sprigs keeping dead leaves, seed)
TEXTURES = {
    "oak": ("assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_o.data.dds", (1024, 1024), "oak", 0.25, 11),
    "oak_distant": ("assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_o.data.dds", (512, 512),
                    "oak", 0.25, 12),
    "beech": ("assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds", (1024, 1024), "beech", 0.2, 21),
    "beech_young": ("assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds", (1024, 1024), "beech",
                    0.85, 22),
    "beech_distant": ("assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_o.data.dds",
                      (512, 512), "beech", 0.2, 23),
    "beech_blocker": ("assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_o.data.dds", (1024, 512), "beech",
                      0.1, 24),
    "birch": ("assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_o.data.dds", (1024, 1024), "birch", 0.0, 31),
    "birch_distant": ("assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_o.data.dds",
                      (512, 512), "birch", 0.0, 32),
    "poplar_01": ("assets/materials/tree/poplar/t_poplar_leaves_01/t_poplar_leaves_o.data.dds", (1024, 1024),
                  "poplar", 0.0, 41),
    "poplar_02": ("assets/materials/tree/poplar/t_poplar_leaves_02/t_poplar_leaves_02_o.data.dds", (1024, 1024),
                  "poplar", 0.0, 42),
}


def game_dir(arg):
    if arg:
        return arg
    for c in (os.environ.get("BNGEARTH_GAME_DIR"), os.path.expanduser("~/.steam/steam/steamapps/common/BeamNG.drive"),
              os.path.expanduser("~/.local/share/Steam/steamapps/common/BeamNG.drive")):
        if c and os.path.isfile(os.path.join(c, *TREE_ZIP)):
            return c
    sys.exit("BeamNG.drive not found: --game DIR (the stock leaf atlases give the sprigs' layout)")


def stock_opacity(zf, member, size):
    """The stock atlas' opacity (0..1 float, h x w) at size (w, h)."""
    with zf.open(member) as f:
        im = Image.open(io.BytesIO(f.read()))
        im.load()
    im = im.convert("L").resize(size, Image.BILINEAR)
    return np.asarray(im, np.float32) / 255.0


# --- layout ----------------------------------------------------------------------------------------------------------
def label(mask):
    """Connected regions (4-neighbour) of a bool grid: int labels (0 = none) and their count (union-find, scanline)."""
    h, w = mask.shape
    lab = np.zeros((h, w), np.int32)
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for y in range(h):
        row = mask[y]
        for x in range(w):
            if not row[x]:
                continue
            up = lab[y - 1, x] if y else 0
            left = lab[y, x - 1] if x else 0
            if up and left:
                a, b = find(up), find(left)
                lab[y, x] = min(a, b)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            elif up or left:
                lab[y, x] = up or left
            else:
                parent.append(len(parent))
                lab[y, x] = len(parent) - 1
    roots = {}
    flat = np.array([find(i) for i in range(len(parent))], np.int32)
    out = flat[lab]
    for v in sorted(set(out[out > 0].tolist())):
        roots[v] = len(roots) + 1
    remap = np.zeros(len(parent), np.int32)
    for k, v in roots.items():
        remap[k] = v
    return remap[out], len(roots)


def sprigs(alpha, min_area_frac=0.0015):
    """The sprigs of an atlas: [(mask bool h x w, stem end (x, y), centroid (x, y))], largest first."""
    h, w = alpha.shape
    solid = alpha > 0.3
    cell = 4
    coarse = solid.reshape(h // cell, cell, w // cell, cell).any(axis=(1, 3))
    grown = np.asarray(Image.fromarray(coarse.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))) > 0
    lab, n = label(grown)
    full = np.repeat(np.repeat(lab, cell, 0), cell, 1)
    out = []
    for k in range(1, n + 1):
        m = solid & (full == k)
        area = int(m.sum())
        if area < min_area_frac * h * w:
            continue
        ys, xs = np.nonzero(m)
        cx, cy = float(xs.mean()), float(ys.mean())
        r = 0.12 * math.hypot(xs.max() - xs.min(), ys.max() - ys.min())
        best = None
        for (px, py), bias in (((xs[ys.argmax()], ys.max()), 0.8), ((xs[ys.argmin()], ys.min()), 1.0),
                               ((xs.min(), ys[xs.argmin()]), 1.0), ((xs.max(), ys[xs.argmax()]), 1.0)):
            mass = int(((xs - px) ** 2 + (ys - py) ** 2 < r * r).sum()) * bias
            if best is None or mass < best[0]:
                best = (mass, (float(px), float(py)))
        out.append((m, best[1], (cx, cy), area))
    out.sort(key=lambda s: -s[3])
    return [s[:3] for s in out]


# --- growth ----------------------------------------------------------------------------------------------------------
def colonise(mask, base, centroid, habit, rng, scale):
    """Space colonisation inside mask from base: nodes (n x 2 float), parents (n int, -1 for the root)."""
    ys, xs = np.nonzero(mask)
    spacing = habit["spacing"] * scale
    n_att = max(12, int(len(xs) / (spacing * spacing)))
    pick = rng.choice(len(xs), size=min(n_att, len(xs)), replace=False)
    att = np.stack([xs[pick], ys[pick]], 1).astype(np.float64) + rng.uniform(-0.5, 0.5, (len(pick), 2))
    step, infl, kill = habit["step"] * scale, 48.0 * scale, 6.0 * scale
    nodes = [np.array(base, np.float64)]
    parents = [-1]
    trop = np.array(habit["tropism"])
    d0 = np.array(centroid) - np.array(base)
    d0 = d0 / (np.linalg.norm(d0) or 1.0)
    # the stalk: straight toward the sprig until the attractors are in reach
    for _ in range(400):
        p = nodes[-1]
        if len(att) == 0 or (np.hypot(*(att - p).T) < infl).any():
            break
        nodes.append(p + d0 * step)
        parents.append(len(nodes) - 2)
    for _ in range(600):
        if len(att) == 0:
            break
        P = np.array(nodes)
        d = np.hypot(att[:, None, 0] - P[None, :, 0], att[:, None, 1] - P[None, :, 1])
        near = d.argmin(1)
        ok = d[np.arange(len(att)), near] < infl
        if not ok.any():
            break
        grow = {}
        for a, i in zip(np.nonzero(ok)[0], near[ok]):
            v = (att[a] - P[i]) / max(d[a, i], 1e-6)
            grow[i] = grow.get(i, 0) + v
        added = 0
        for i in sorted(grow):
            v = grow[i] / (np.linalg.norm(grow[i]) or 1.0)
            v = v + trop + rng.normal(0, habit["jitter"], 2)
            v = v / (np.linalg.norm(v) or 1.0)
            q = P[i] + v * step
            qi, qj = int(round(q[1])), int(round(q[0]))
            if not (0 <= qi < mask.shape[0] and 0 <= qj < mask.shape[1]):
                continue
            if min(np.hypot(*(P - q).T)) < step * 0.45:
                continue
            nodes.append(q)
            parents.append(i)
            added += 1
        P = np.array(nodes)
        d = np.hypot(att[:, None, 0] - P[None, :, 0], att[:, None, 1] - P[None, :, 1])
        att = att[d.min(1) > kill]
        if not added:
            break
    return np.array(nodes), np.array(parents)


def thickness(parents, habit, scale):
    """Each node's radius (px) by the pipe model: the tips it carries."""
    n = len(parents)
    kids = np.bincount(parents[parents >= 0], minlength=n)
    tips = (kids == 0).astype(np.float64)
    for i in range(n - 1, 0, -1):       # (children come after their parents)
        if parents[i] >= 0:
            tips[parents[i]] += tips[i]
    r = habit["r_tip"] * scale ** 0.5 * tips ** (1 / 2.4)
    return np.minimum(r, habit["r_max"] * scale)


# --- drawing ---------------------------------------------------------------------------------------------------------
def draw_segment(alpha, rgb, a, b, ra, rb, col):
    h, w = alpha.shape
    r = max(ra, rb)
    x0, x1 = int(max(0, math.floor(min(a[0], b[0]) - r - 1))), int(min(w - 1, math.ceil(max(a[0], b[0]) + r + 1)))
    y0, y1 = int(max(0, math.floor(min(a[1], b[1]) - r - 1))), int(min(h - 1, math.ceil(max(a[1], b[1]) + r + 1)))
    if x1 < x0 or y1 < y0:
        return
    yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
    px, py = xx + 0.5, yy + 0.5
    dx, dy = b[0] - a[0], b[1] - a[1]
    ll = dx * dx + dy * dy
    t = np.clip(((px - a[0]) * dx + (py - a[1]) * dy) / ll, 0, 1) if ll > 0 else np.zeros_like(px)
    d = np.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))
    rr = ra + (rb - ra) * t
    cov = np.clip(rr + 0.5 - d, 0, 1) * np.minimum(1.0, rr * 2 + 0.25)   # (sub-pixel twigs: fainter)
    cur = alpha[y0:y1 + 1, x0:x1 + 1]
    upd = cov > 0
    k = cov[..., None]
    rgb[y0:y1 + 1, x0:x1 + 1] = np.where(upd[..., None], rgb[y0:y1 + 1, x0:x1 + 1] * (1 - k) + np.array(col) * k,
                                         rgb[y0:y1 + 1, x0:x1 + 1])
    alpha[y0:y1 + 1, x0:x1 + 1] = np.maximum(cur, cov)


def draw_leaf(alpha, rgb, c, ang, ln, wd, col):
    h, w = alpha.shape
    R = int(ln + 2)
    x0, x1 = max(0, int(c[0]) - R), min(w - 1, int(c[0]) + R)
    y0, y1 = max(0, int(c[1]) - R), min(h - 1, int(c[1]) + R)
    if x1 < x0 or y1 < y0:
        return
    yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1]
    u = (xx + 0.5 - c[0]) * math.cos(ang) + (yy + 0.5 - c[1]) * math.sin(ang)
    v = -(xx + 0.5 - c[0]) * math.sin(ang) + (yy + 0.5 - c[1]) * math.cos(ang)
    e = np.sqrt((u / ln) ** 2 + (v / wd) ** 2)
    cov = np.clip((1 - e) * min(ln, wd) + 0.5, 0, 1)
    upd = cov > 0
    k = cov[..., None]
    rgb[y0:y1 + 1, x0:x1 + 1] = np.where(upd[..., None], rgb[y0:y1 + 1, x0:x1 + 1] * (1 - k) + np.array(col) * k,
                                         rgb[y0:y1 + 1, x0:x1 + 1])
    alpha[y0:y1 + 1, x0:x1 + 1] = np.maximum(alpha[y0:y1 + 1, x0:x1 + 1], cov)


def make(name, stock, size, habit_name, dead_share, seed):
    """(rgb uint8 h x w x 3, opacity uint8 h x w) of one twig atlas."""
    habit = HABITS[habit_name]
    rng = np.random.default_rng(seed)
    w, h = size
    scale = max(w, h) / 1024.0
    alpha = np.zeros((h, w), np.float64)
    bark, tip = np.array(habit["bark"], float), np.array(habit["tip"], float)
    rgb = np.zeros((h, w, 3), np.float64) + (bark + tip) / 2
    regions = sprigs(stock)
    dead_regions = set(rng.choice(len(regions), size=int(round(dead_share * len(regions))), replace=False).tolist()) \
        if regions and habit["dead"] else set()
    for ri, (mask, base, centroid) in enumerate(regions):
        allowed = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))) > 0
        nodes, parents = colonise(allowed, base, centroid, habit, rng, scale)
        if len(nodes) < 2:
            continue
        rad = thickness(parents, habit, scale)
        rmax = rad.max() or 1.0
        order = np.argsort(-rad)
        for i in order:
            p = parents[i]
            if p < 0:
                continue
            f = min(1.0, (rad[i] / rmax) ** 0.6)
            col = tip + (bark - tip) * f
            draw_segment(alpha, rgb, nodes[p], nodes[i], rad[p], rad[i], col)
        kids = np.bincount(parents[parents >= 0], minlength=len(nodes))
        tips_i = np.nonzero(kids == 0)[0]
        # fine spurs off the tips and the thin twigs: the haze
        for i in tips_i:
            p = parents[i]
            if p < 0:
                continue
            d = nodes[i] - nodes[p]
            base_ang = math.atan2(d[1], d[0])
            for _ in range(int(rng.integers(1, 4))):
                if rng.random() > habit["spur"]:
                    continue
                ang = base_ang + rng.normal(0, 0.7) + habit["tropism"][1] * 0.5
                ln = rng.uniform(4, 11) * scale
                e = nodes[i] + np.array([math.cos(ang), math.sin(ang)]) * ln
                draw_segment(alpha, rgb, nodes[i], e, habit["r_tip"] * 0.8 * scale ** 0.5, 0.3, tip)
        if ri in dead_regions:
            for i in tips_i:
                if rng.random() > 0.45:
                    continue
                col = np.array(habit["dead"][int(rng.integers(len(habit["dead"])))], float) * rng.uniform(0.85, 1.1)
                p = parents[i]
                d = nodes[i] - nodes[p] if p >= 0 else np.array([1.0, 0.0])
                ang = math.atan2(d[1], d[0]) + rng.normal(0, 0.6)
                ln = rng.uniform(4.0, 6.5) * scale
                c = nodes[i] + np.array([math.cos(ang), math.sin(ang)]) * ln * 0.7
                draw_leaf(alpha, rgb, c, ang, ln, ln * rng.uniform(0.4, 0.55), np.clip(col, 0, 255))
    # the colour under the transparent texels: the atlas' mean twig colour (the mips and filtering blend toward it)
    wsum = alpha.sum()
    mean = (rgb * alpha[..., None]).sum((0, 1)) / wsum if wsum > 0 else bark
    out = np.where((alpha > 0)[..., None], rgb, mean)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8), np.clip(alpha * 255 + 0.5, 0, 255).astype(np.uint8)


def preview(rgb, op, sky=(170, 195, 225), levels=5):
    """The texture over a sky as the GPU would draw it at mips 0..levels-1 (box mips, alphaRef), side by side."""
    h, w = op.shape
    tiles = []
    a = op.astype(np.float32)
    c = rgb.astype(np.float32)
    for k in range(levels):
        f = 2 ** k
        ak = a.reshape(h // f, f, w // f, f).mean((1, 3))
        ck = c.reshape(h // f, f, w // f, f, 3).mean((1, 3))
        on = ak >= ALPHA_REF
        img = np.where(on[..., None], ck, np.array(sky, np.float32))
        im = Image.fromarray(img.astype(np.uint8)).resize((w // 2, h // 2), Image.NEAREST if k >= 1 else Image.BILINEAR)
        tiles.append(im)
    sheet = Image.new("RGB", (w // 2 * levels, h // 2))
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * (w // 2), 0))
    return sheet


def webp_bytes(im):
    b = io.BytesIO()
    im.save(b, "WEBP", lossless=True, quality=100, method=6, exact=True)
    return b.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--game", help="the BeamNG.drive folder (default: found)")
    ap.add_argument("--preview", help="a PNG sheet of the textures at mips 0..4 over a sky")
    ap.add_argument("--no-write", action="store_true", help="only the preview")
    ap.add_argument("names", nargs="*")
    args = ap.parse_args()
    names = args.names or list(TEXTURES)
    zf = zipfile.ZipFile(os.path.join(game_dir(args.game), *TREE_ZIP))
    entries = modtextures.manifest()
    sheets = []
    for name in names:
        member, size, habit, dead, seed = TEXTURES[name]
        rgb, op = make(name, stock_opacity(zf, member, size), size, habit, dead, seed)
        print(f"{name}: {size[0]}x{size[1]}, coverage {float((op > ALPHA_REF).mean()):.3f}", flush=True)
        if args.preview:
            sheets.append(preview(rgb, op))
        if args.no_write:
            continue
        for suffix, img, fmt, role in (("_b.color", Image.fromarray(rgb, "RGB"), "BC7_SRGB", "color"),
                                       ("_o.data", Image.fromarray(op, "L").convert("RGB"), "BC4", "data")):
            rel = f"{REL}/bne_twig_{name}{suffix}"
            src = os.path.join(modtextures.SOURCES, *rel.split("/")) + ".webp"
            os.makedirs(os.path.dirname(src), exist_ok=True)
            with open(src, "wb") as f:
                f.write(webp_bytes(img))
            entries[rel + ".dds"] = {"source": rel + ".webp", "format": fmt, "role": role}
    if not args.no_write:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import mod_textures_dds
        mod_textures_dds.write_manifest(entries)
    if sheets:
        W = max(s.size[0] for s in sheets)
        H = sum(s.size[1] for s in sheets)
        out = Image.new("RGB", (W, H), (255, 255, 255))
        y = 0
        for s in sheets:
            out.paste(s, (0, y))
            y += s.size[1]
        out.save(args.preview)


if __name__ == "__main__":
    main()
