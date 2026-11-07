#!/usr/bin/env python3
"""Crop plants for GroundCover (docs/new/008_crop-fields): the atlas under mod/beamng_earth/art/beamngEarth/crops/.

Eight billboard cells in one 2048 x 2048 atlas (4 x 2 cells of 512 x 1024 px: a clump of plants each, standing on the
bottom edge): two cereal clumps, two maize, two sunflower, two broad-leaf (beet leaves, rape). Procedural, nothing
third-party: stems and leaves are drawn as curved polygons at 2x and averaged down, with their own random jitter so
the clumps differ. Outputs crops_v2_b.color.png (RGB, the colour pushed out into the transparent edge so the mips do not
fringe dark), crops_v2_o.data.png (greyscale opacity); a preview on a grey field when a path is given (not used by the game).

  python3 tools/make_crop_atlas.py [out_dir] [preview.png]
"""
import math
import os
import random
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mod", "beamng_earth", "art", "beamngEarth", "crops")
CELL_W, CELL_H = 512, 1024
COLS, ROWS = 4, 2
SS = 2                                  # supersampling of a cell
CELLS = ("cereal_a", "cereal_b", "maize_a", "maize_b", "sunflower_a", "sunflower_b", "broad_a", "broad_b")


def bez(p0, p1, p2, n=14):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / (n - 1) for i in range(n))]


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def blade(d, base, tip, ctrl, width, colour, rib=None, n=14):
    """A leaf / blade from base to tip bent through ctrl, `width` at its widest (a quarter of the way up)."""
    spine = bez(base, ctrl, tip, n)
    left, right = [], []
    for i, (x, y) in enumerate(spine):
        t = i / (n - 1)
        w = width * math.sin(math.pi * min(1.0, t ** 0.7)) * (1 - 0.15 * t)
        j = min(i + 1, n - 1)
        dx, dy = spine[j][0] - spine[max(i - 1, 0)][0], spine[j][1] - spine[max(i - 1, 0)][1]
        ln = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / ln, dx / ln
        left.append((x + nx * w / 2, y + ny * w / 2))
        right.append((x - nx * w / 2, y - ny * w / 2))
    d.polygon(left + right[::-1], fill=colour + (255,))
    if rib is not None:
        d.line(spine, fill=rib + (255,), width=max(1, int(width * 0.08)))


def stem(d, pts, width, colour):
    d.line(pts, fill=colour + (255,), width=width, joint="curve")


def cereal(rng, ripe_tint):
    im = Image.new("RGBA", (CELL_W * SS, CELL_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    W, H = im.size
    base_y = H - 6
    for k in range(34):
        bx = W / 2 + rng.uniform(-110, 110) * SS
        h = rng.uniform(0.55, 0.92) * H
        lean = rng.uniform(-70, 70) * SS * (0.5 + h / H)
        top = (bx + lean, base_y - h)
        mid = (bx + lean * 0.25, base_y - h * 0.55)
        stm = bez((bx, base_y), mid, top, 16)
        col = shade((150, 158, 62), rng.uniform(0.85, 1.12))
        stem(d, stm, int(5 * SS), col)
        # two long narrow leaves low on the stem
        for s in (-1, 1):
            if rng.random() < 0.8:
                yb = base_y - h * rng.uniform(0.12, 0.4)
                xb = bx + (top[0] - bx) * 0.15
                blade(d, (xb, yb), (xb + s * rng.uniform(80, 150) * SS, yb - rng.uniform(90, 200) * SS),
                      (xb + s * 20 * SS, yb - 130 * SS), 14 * SS, shade((98, 135, 48), rng.uniform(0.85, 1.15)))
        # the ear: an elongated cluster of grains with awns along the top of the stem
        ear_len = rng.uniform(0.11, 0.15) * H
        ang = math.atan2(top[0] - mid[0], mid[1] - top[1])
        for i in range(15):
            t = i / 14.0
            ex = top[0] + math.sin(ang) * (t - 0.9) * ear_len * -1
            ey = top[1] + (t - 0.9) * ear_len * 0.0 - ear_len * (1 - t) * math.cos(ang) * 0.0
            px = top[0] - math.sin(ang) * (ear_len * (0.9 - t))
            py = top[1] - math.cos(ang) * (ear_len * (0.9 - t))
            gc = shade(ripe_tint, rng.uniform(0.9, 1.1))
            for s in (-1, 1):
                d.ellipse([px + s * 6 * SS - 5 * SS, py - 9 * SS, px + s * 6 * SS + 5 * SS, py + 9 * SS], fill=gc + (255,))
                d.line([(px + s * 6 * SS, py), (px + s * 22 * SS, py - 34 * SS)], fill=shade(gc, 1.05) + (255,), width=SS)
    return im


def maize_plant(d, rng, cx, top_y, scale, tone):
    """One grown maize plant (V12 to tasselling, not ripe): a stalk with wide arching leaves from the bottom edge up to
    top_y, a small tassel on top, no cob worth drawing (husk-wrapped, hidden in the leaf axils)."""
    W, H = d.im.size
    base = (cx + rng.uniform(-8, 8) * SS, H - 4)
    top = (cx + rng.uniform(-14, 14) * SS, top_y)
    spine = bez(base, (cx + rng.uniform(-10, 10) * SS, (base[1] + top[1]) / 2), top, 24)
    stem(d, spine, int(11 * SS * scale), shade((112, 148, 56), tone))
    n = 12
    for i in range(n):
        t = 0.06 + 0.84 * i / (n - 1)
        px, py = spine[int(t * 23)]
        s = 1 if i % 2 == 0 else -1
        ln = (205 - 70 * t) * SS * scale * (0.88 + 0.24 * rng.random())
        tip = (px + s * ln * 0.92, py + ln * (0.30 + 0.25 * (1 - t)))             # wide, drooping
        ctrl = (px + s * ln * 0.5, py - ln * (0.34 + 0.2 * t))
        wd = (70 + 22 * (1 - t)) * SS * scale
        blade(d, (px, py), tip, ctrl, wd, shade((66, 124, 36), tone * rng.uniform(0.86, 1.18)), rib=(158, 192, 96), n=20)
    for i in range(8):                                                           # the tassel
        a = math.radians(-50 + i * 14)
        tipx, tipy = top[0] + math.sin(a) * 52 * SS * scale, top[1] - math.cos(a) * 26 * SS * scale + 28 * SS
        d.line(bez(top, (top[0] + math.sin(a) * 22 * SS, top[1] - 30 * SS), (tipx, tipy), 8), fill=(196, 178, 104, 255),
               width=int(2.2 * SS))


def maize(rng, tall):
    """A clump of three (a) or two (b) grown maize plants, the back ones smaller and darker."""
    im = Image.new("RGBA", (CELL_W * SS, CELL_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.im = im
    W, H = im.size
    spots = [(0.33, 0.86, 0.80), (0.67, 0.80, 0.78), (0.5, 0.97, 1.0)] if tall else [(0.36, 0.9, 0.86), (0.66, 0.97, 1.0)]
    for fx, fh, sc in spots:                                                     # back to front
        maize_plant(d, rng, W * fx, H * (1 - fh) + 18 * SS, sc, 0.82 + 0.18 * sc)
    return im


def sunflower(rng, open_head):
    im = Image.new("RGBA", (CELL_W * SS, CELL_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    W, H = im.size
    base = (W / 2, H - 6)
    top = (W / 2 + rng.uniform(-14, 14) * SS, H * 0.26)
    spine = bez(base, (W / 2 - 6 * SS, H * 0.6), top, 18)
    stem(d, spine, int(14 * SS), (96, 128, 52))
    n = 9
    for i in range(n):
        t = 0.08 + 0.7 * i / (n - 1)
        px, py = spine[int(t * 17)]
        s = 1 if i % 2 == 0 else -1
        ln = (215 - 70 * t) * SS * (0.88 + 0.2 * rng.random())
        tip = (px + s * ln * 0.9, py + ln * 0.05)
        blade(d, (px, py), tip, (px + s * ln * 0.45, py - ln * 0.55), (118 - 30 * t) * SS,
              shade((72, 118, 44), rng.uniform(0.85, 1.15)), rib=(130, 168, 80), n=16)
    hx, hy = top
    r = (82 if open_head else 62) * SS
    if open_head:
        for i in range(26):                                  # petals
            a = 2 * math.pi * i / 26 + rng.uniform(-0.05, 0.05)
            L = rng.uniform(52, 66) * SS
            tx, ty = hx + math.cos(a) * (r + L), hy + math.sin(a) * (r + L) * 0.94
            blade(d, (hx + math.cos(a) * r * 0.8, hy + math.sin(a) * r * 0.8), (tx, ty),
                  (hx + math.cos(a + 0.12) * (r + L * 0.5), hy + math.sin(a + 0.12) * (r + L * 0.5)), 26 * SS,
                  shade((236, 184, 18), rng.uniform(0.9, 1.08)), n=9)
        d.ellipse([hx - r, hy - r * 0.94, hx + r, hy + r * 0.94], fill=(64, 42, 22, 255))
        for i in range(60):                                  # florets: a spiral of lighter dots
            a, rr = i * 2.39996, math.sqrt(i / 60.0) * r * 0.9
            d.ellipse([hx + math.cos(a) * rr - 3 * SS, hy + math.sin(a) * rr - 3 * SS, hx + math.cos(a) * rr + 3 * SS,
                       hy + math.sin(a) * rr + 3 * SS], fill=(98, 66, 30, 255))
    else:                                                    # a bud: green bracts round a small yellow centre
        d.ellipse([hx - r, hy - r, hx + r, hy + r], fill=(84, 126, 46, 255))
        for i in range(14):
            a = 2 * math.pi * i / 14
            blade(d, (hx, hy), (hx + math.cos(a) * r * 1.5, hy + math.sin(a) * r * 1.5),
                  (hx + math.cos(a + 0.3) * r, hy + math.sin(a + 0.3) * r), 24 * SS, (92, 138, 52), n=8)
        d.ellipse([hx - r * 0.45, hy - r * 0.45, hx + r * 0.45, hy + r * 0.45], fill=(214, 170, 28, 255))
    return im


def broad(rng, flowering):
    im = Image.new("RGBA", (CELL_W * SS, CELL_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    W, H = im.size
    base = (W / 2, H - 6)
    if not flowering:                                        # a beet: a tuft of long upright ovate leaves
        for i in range(17):
            a = math.radians(rng.uniform(-38, 38))
            ln = rng.uniform(0.42, 0.7) * H
            tip = (base[0] + math.sin(a) * ln, base[1] - math.cos(a) * ln)
            ctrl = (base[0] + math.sin(a) * ln * 0.3, base[1] - ln * 0.62)
            blade(d, base, tip, ctrl, rng.uniform(70, 125) * SS, shade((70, 124, 56), rng.uniform(0.82, 1.2)),
                  rib=(168, 198, 130), n=16)
    else:                                                    # rape: branched stems, grey-green leaves, green pods
        trunk = bez(base, (W / 2 + 10 * SS, H * 0.55), (W / 2, H * 0.12), 16)
        stem(d, trunk, int(10 * SS), (110, 140, 70))
        for i in range(8):
            t = 0.1 + 0.62 * i / 7
            px, py = trunk[int(t * 15)]
            s = 1 if i % 2 == 0 else -1
            ln = (225 - 100 * t) * SS
            blade(d, (px, py), (px + s * ln, py - ln * 0.35), (px + s * ln * 0.5, py - ln * 0.5), 62 * SS,
                  shade((92, 136, 78), rng.uniform(0.85, 1.12)), rib=(160, 190, 130))
        for i in range(30):                                  # side shoots with pods
            t = rng.uniform(0.3, 0.95)
            px, py = trunk[int(t * 15)]
            s = rng.choice((-1, 1))
            tip = (px + s * rng.uniform(60, 180) * SS, py - rng.uniform(40, 150) * SS)
            br = bez((px, py), (px + s * 40 * SS, py - 40 * SS), tip, 8)
            d.line(br, fill=(104, 138, 66, 255), width=int(4 * SS))
            for q in br[3:]:
                d.ellipse([q[0] - 8 * SS, q[1] - 5 * SS, q[0] + 8 * SS, q[1] + 5 * SS], fill=(132, 160, 70, 255))
    return im


MAKERS = {
    "cereal_a": lambda r: cereal(r, (216, 186, 98)), "cereal_b": lambda r: cereal(r, (200, 176, 92)),
    "maize_a": lambda r: maize(r, True), "maize_b": lambda r: maize(r, False),
    "sunflower_a": lambda r: sunflower(r, True), "sunflower_b": lambda r: sunflower(r, False),
    "broad_a": lambda r: broad(r, False), "broad_b": lambda r: broad(r, True),
}


def finish(cell, seed):
    """Depth for a flat drawing: darker rims, a light-to-dark gradient up each plant, a grain of fine and broad noise."""
    a = np.asarray(cell, np.float32).copy()
    al = a[..., 3] / 255.0
    h, w = al.shape
    im_a = Image.fromarray((al * 255).astype(np.uint8), "L")
    inner = np.asarray(im_a.filter(ImageFilter.MinFilter(5)), np.float32) / 255.0
    rim = np.clip(al - inner, 0, 1)
    rng = np.random.default_rng(seed)
    fine = rng.normal(0, 1, (h, w)).astype(np.float32)
    fine = np.asarray(Image.fromarray(((fine * 40) + 128).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2)), np.float32) / 128.0 - 1.0
    broad = rng.normal(0, 1, (h // 16, w // 16)).astype(np.float32)
    broad = np.asarray(Image.fromarray(((broad * 40) + 128).clip(0, 255).astype(np.uint8)).resize((w, h), Image.BICUBIC), np.float32) / 128.0 - 1.0
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    k = 1.0 + 0.30 * fine + 0.14 * broad + 0.10 * (0.5 - ys) - 0.32 * rim
    a[..., :3] = np.clip(a[..., :3] * k[..., None], 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def bleed(rgba):
    """RGB of the transparent edge taken from the nearest opaque neighbours (blurred): mips and filtering then blend
    plant colour into the edge, not black."""
    a = np.asarray(rgba, np.float32)
    al = a[..., 3:4] / 255.0
    pr = a[..., :3] * al
    im_c = Image.fromarray(np.clip(pr, 0, 255).astype(np.uint8))
    im_a = Image.fromarray((al[..., 0] * 255).astype(np.uint8), "L")
    out = a[..., :3].copy()
    num, den = np.asarray(im_c, np.float32), np.asarray(im_a, np.float32)[..., None]
    for rad in (3, 8, 20, 48):
        bc = np.asarray(im_c.filter(ImageFilter.GaussianBlur(rad)), np.float32)
        ba = np.asarray(im_a.filter(ImageFilter.GaussianBlur(rad)), np.float32)[..., None]
        fill = bc / np.maximum(ba / 255.0, 1e-3)
        empty = (den < 8) & (ba > 4)
        num = np.where(empty, fill, num)
        den = np.where(empty, 255.0, den)
    out = np.where(al > 0.02, a[..., :3], np.clip(num, 0, 255))
    return out.astype(np.uint8)


def build(out_dir, preview=None):
    atlas = Image.new("RGBA", (CELL_W * COLS, CELL_H * ROWS), (0, 0, 0, 0))
    for i, name in enumerate(CELLS):
        rng = random.Random(1000 + i * 7919)
        cell = finish(MAKERS[name](rng), 77 + i).resize((CELL_W, CELL_H), Image.LANCZOS)
        atlas.paste(cell, ((i % COLS) * CELL_W, (i // COLS) * CELL_H))
    arr = np.asarray(atlas)
    rgb = bleed(arr)
    os.makedirs(out_dir, exist_ok=True)
    Image.fromarray(rgb, "RGB").save(os.path.join(out_dir, "crops_v2_b.color.png"), optimize=True)
    Image.fromarray(arr[..., 3], "L").save(os.path.join(out_dir, "crops_v2_o.data.png"), optimize=True)
    if preview:
        prev = Image.new("RGB", atlas.size, (118, 112, 96))
        prev.paste(atlas, (0, 0), atlas)
        prev.save(preview)
    return atlas.size


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else OUT, sys.argv[2] if len(sys.argv) > 2 else None))
