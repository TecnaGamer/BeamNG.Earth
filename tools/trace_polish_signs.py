#!/usr/bin/env python3
"""Poland's sign shapes as vectors, traced from the regulation's own drawings: server/data/pl_sign_shapes.json, which
tools/build_sign_atlas.py draws the Polish cells from (D-6's pedestrian and zebra, B-33's digits).

    python tools/trace_polish_signs.py <Dz.U. 2019 poz. 2311 as PDF>      (needs poppler's pdfimages)

The source is the Annex 1 of the technical conditions for road signs (rozporządzenie Ministra Infrastruktury z dnia
3 lipca 2003 r. w sprawie szczegółowych warunków technicznych dla znaków i sygnałów drogowych ..., consolidated text
Dz.U. 2019 poz. 2311: https://api.sejm.gov.pl/eli/acts/DU/2019/2311/text.pdf). A Polish legal act and its official
signs are not under copyright (ustawa o prawie autorskim, art. 4 pkt 1 and 2).

- The digits are the regulation's one typeface (pkt 1.4: capitals and digits d = 6 gr high, strokes gr thick). 1, 2, 4,
  7, 8, 9 and 0 come from rys. 1.4.2 (page 20: outline drawings at ~660 ppi, filled here), 3 from the colour C-14
  (page 171), 5 from the colour B-33 (page 140, rys. 3.2.34.1) and 6 from the colour E-15b "601" (page 288).
- D-6's pedestrian and its zebra's three bars come from the colour D-6 (page 197, rys. 5.2.6.1); its triangle is drawn
  from the construction (rys. 9.4.9), not traced.

Each shape is a list of rings (x, y): the outer rings counter-clockwise, the holes clockwise. A digit's units: its
height 1, its baseline y = 0, its left edge x = 0. D-6's: the sign's square as [-1, 1]^2, y up.
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "server", "data", "pl_sign_shapes.json")
UP = 4                          # the colour pictures are scaled up this much (bicubic) before the threshold
TOL = 0.0015                    # the outline's simplification, of a digit's height or of the sign's half width


def images(pdf, page, tmp):
    """{index: PIL image} of a page's pictures as stored (pdfimages -png)."""
    stem = os.path.join(tmp, f"p{page}")
    subprocess.run(["pdfimages", "-f", str(page), "-l", str(page), "-png", pdf, stem], check=True)
    out = {}
    for name in sorted(os.listdir(tmp)):
        if name.startswith(f"p{page}-") and name.endswith(".png"):
            out[int(name[len(f"p{page}-"):-4])] = Image.open(os.path.join(tmp, name))
    return out


def components(mask, conn8=True):
    """(labels, count): connected components of a boolean mask (0 = background), by union-find over row runs."""
    h, w = mask.shape
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs = []                   # per row [(x0, x1, label)]
    for y in range(h):
        row = mask[y]
        d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        cur = []
        prev = runs[-1] if runs else []
        for x0, x1 in zip(starts, ends):
            lab = None
            for p0, p1, pl in prev:
                if (p0 < x1 + (1 if conn8 else 0)) and (x0 < p1 + (1 if conn8 else 0)):
                    if lab is None:
                        lab = find(pl)
                    else:
                        a, b = find(pl), lab
                        if a != b:
                            parent[max(a, b)] = min(a, b)
                            lab = min(a, b)
            if lab is None:
                lab = len(parent)
                parent.append(lab)
            cur.append((int(x0), int(x1), lab))
        runs.append(cur)
    roots = {}
    labels = np.zeros((h, w), np.int32)
    for y, cur in enumerate(runs):
        for x0, x1, lab in cur:
            r = find(lab)
            labels[y, x0:x1] = roots.setdefault(r, len(roots) + 1)
    return labels, len(roots)


def filled(ink, line=9, min_area=0.01):
    """The solid shape an outline drawing (ink: its lines) encloses, its holes open: white regions labelled, each one's
    nesting depth from the outside found across the lines between them (a region this side of a line within `line` px
    of one on the other side is one deeper), odd depths filled with the lines round them. A dashed guide line does
    not split a region; a region under min_area of the picture (a measuring point's circle) is not a body."""
    labels, n = components(~ink, conn8=False)
    h, w = ink.shape
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))) - {0}
    pairs = set()
    for dy in range(-line, line + 1):
        for dx in range(-line, line + 1):
            if dx * dx + dy * dy > line * line or (dx, dy) <= (0, 0):
                continue
            a = labels[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
            b = labels[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)]
            m = (a != b) & (a > 0) & (b > 0)
            for p, q in set(zip(a[m].tolist(), b[m].tolist())):
                pairs.add((min(p, q), max(p, q)))
    nbr = {}
    for p, q in pairs:
        nbr.setdefault(p, set()).add(q)
        nbr.setdefault(q, set()).add(p)
    depth = {b: 0 for b in border}
    todo = list(border)
    while todo:
        nxt = []
        for c in todo:
            for d in nbr.get(c, ()):
                if d not in depth:
                    depth[d] = depth[c] + 1
                    nxt.append(d)
        todo = nxt
    area = np.bincount(labels.ravel(), minlength=n + 1)
    body = np.zeros(n + 1, bool)
    for c, d in depth.items():
        body[c] = d % 2 == 1 and area[c] >= min_area * h * w
    solid = body[labels]
    grow = ImageFilter.MaxFilter(2 * (line // 2) + 1)
    grown = np.asarray(Image.fromarray(solid.astype(np.uint8) * 255).filter(grow)) > 0
    holes = (~body[labels]) & (labels > 0) & np.isin(labels, [c for c, d in depth.items() if d >= 2 and d % 2 == 0])
    return (solid | (grown & ink)) & ~holes


def smooth_mask(mask, up=UP):
    """A mask scaled up `up` times with its staircase softened: bicubic over the 0/1 picture, cut at a half."""
    im = Image.fromarray(mask.astype(np.uint8) * 255)
    im = im.resize((im.width * up, im.height * up), Image.Resampling.BICUBIC)
    return np.asarray(im) >= 128


def rings(mask):
    """The boundary rings of a mask's pixels, in pixel corner coordinates (x right, y down), inside on the left when
    walked with y up: the outer rings counter-clockwise, the holes clockwise (in y-up terms)."""
    h, w = mask.shape
    m = np.pad(mask, 1)
    edges = {}                  # start corner -> [end corner]: each pixel edge between in and out, inside on the left
    ys, xs = np.nonzero(m[1:-1, 1:-1])
    for y, x in zip(ys.tolist(), xs.tolist()):
        if not m[y, x + 1]:     # above (y - 1) is out: the top edge, walked right to left in y-down = ccw in y-up
            edges.setdefault((x + 1, y), []).append((x, y))
        if not m[y + 2, x + 1]:     # below
            edges.setdefault((x, y + 1), []).append((x + 1, y + 1))
        if not m[y + 1, x]:     # left
            edges.setdefault((x, y), []).append((x, y + 1))
        if not m[y + 1, x + 2]:     # right
            edges.setdefault((x + 1, y + 1), []).append((x + 1, y))
    out = []
    while edges:
        start = next(iter(edges))
        ring, cur, prev = [start], start, None
        while True:
            ends = edges[cur]
            if len(ends) == 1:
                nxt = ends.pop()
            else:               # (a corner two pixels meet at diagonally: turn so the pixels stay apart)
                dx0, dy0 = (cur[0] - prev[0], cur[1] - prev[1]) if prev else (0, 0)
                nxt = min(ends, key=lambda e: ((e[0] - cur[0]) * dy0 - (e[1] - cur[1]) * dx0))
                ends.remove(nxt)
            if not ends:
                del edges[cur]
            prev, cur = cur, nxt
            if cur == start:
                break
            ring.append(cur)
        out.append(ring)
    return out


def simplify(pts, tol):
    """Douglas-Peucker over a closed ring (its first point kept)."""
    pts = np.asarray(pts, np.float64)
    if len(pts) < 4:
        return pts
    far = int(np.argmax(np.hypot(*(pts - pts[0]).T)))
    out = []
    for a, b in ((0, far), (far, len(pts))):
        seg = np.vstack([pts[a:b], pts[b % len(pts)]])
        keep = np.zeros(len(seg), bool)
        keep[0] = keep[-1] = True
        stack = [(0, len(seg) - 1)]
        while stack:
            i, j = stack.pop()
            if j <= i + 1:
                continue
            p, q = seg[i], seg[j]
            d = q - p
            L = max(np.hypot(*d), 1e-12)
            dist = np.abs(d[0] * (seg[i + 1:j, 1] - p[1]) - d[1] * (seg[i + 1:j, 0] - p[0])) / L
            k = int(np.argmax(dist))
            if dist[k] > tol:
                keep[i + 1 + k] = True
                stack += [(i, i + 1 + k), (i + 1 + k, j)]
        out.append(seg[keep][:-1])
    return np.vstack(out)


def midpoints(ring):
    """A pixel-edge ring's edge midpoints: the staircase's middle line, not its corners."""
    r = np.asarray(ring, np.float64)
    return (r + np.roll(r, -1, axis=0)) / 2.0


def vector(mask, to_unit, tol):
    """The rings of a mask as lists of [x, y] in the units to_unit(x, y) gives (y up), simplified to tol there."""
    out = []
    for ring in rings(mask):
        if len(ring) < 8:
            continue
        pts = np.array([to_unit(x, y) for x, y in midpoints(ring)])
        pts = simplify(pts, tol)
        area = 0.5 * abs(float(np.sum(pts[:, 0] * np.roll(pts[:, 1], -1) - np.roll(pts[:, 0], -1) * pts[:, 1])))
        if len(pts) >= 3 and area > (tol * 4) ** 2:
            out.append([[round(float(x), 4), round(float(y), 4)] for x, y in pts])
    return out


def bbox(mask):
    ys, xs = np.nonzero(mask)
    return xs.min(), xs.max() + 1, ys.min(), ys.max() + 1


def digit_shape(mask, base, d):
    """A digit's mask (only it) as rings in digit units: d px the capital height (1), base the baseline's row (0), its
    left edge x = 0. Round digits overshoot both lines a little, as drawn."""
    x0, x1, _y0, _y1 = bbox(mask)
    return {"width": round(float(x1 - x0) / d, 4),
            "rings": vector(mask, lambda x, y: ((x - x0) / d, (base - y) / d), TOL)}


def lines_from(mask, ref):
    """(baseline row, capital height px) of a picture whose digit `mask` is the digit `ref` (digit units, as traced
    from rys. 1.4.2): its extent there gives the scale and the baseline."""
    _x0, _x1, y0, y1 = bbox(mask)
    lo = min(y for r in ref["rings"] for _x, y in r)
    hi = max(y for r in ref["rings"] for _x, y in r)
    d = (y1 - y0) / (hi - lo)
    return y1 + lo * d, d


def pick(labels, count, boxes):
    """The components of labels whose bounding boxes' centres fall in each box (x0, y0, x1, y1), as one mask each."""
    found = []
    for lab in range(1, count + 1):
        ys, xs = np.nonzero(labels == lab)
        if len(ys) < 50:
            continue
        found.append(((xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2, lab))
    out = []
    for x0, y0, x1, y1 in boxes:
        labs = [lab for cx, cy, lab in found if x0 <= cx < x1 and y0 <= cy < y1]
        out.append(np.isin(labels, labs))
    return out


def colour_digits(img, ink, boxes):
    """Masks of a colour picture's digits: ink(rgb array) -> bool, scaled up, each the components centred in its box
    (boxes in the picture's own px)."""
    a = np.asarray(img.convert("RGB")).astype(np.int32)
    labels, n = components(smooth_mask(ink(a)))
    return pick(labels, n, [tuple(v * UP for v in b) for b in boxes])


def outline_digits(strips, boxes):
    """Masks of rys. 1.4.2's outline digits (the page's strips stacked), filled, each the largest shape in its box,
    in the stacked strips' px."""
    a = np.vstack([np.asarray(s.convert("L")) > 127 for s in strips])    # (1-bit: 1 = ink)
    out = []
    for x0, y0, x1, y1 in boxes:
        labels, n = components(filled(a[y0:y1, x0:x1]))
        area = np.bincount(labels.ravel())
        area[0] = 0
        full = np.zeros(a.shape, bool)
        full[y0:y1, x0:x1] = labels == int(np.argmax(area))
        out.append(full)
    return out


def d6(img):
    """D-6's pedestrian and zebra bars (the colour picture's near-black) in the sign's [-1, 1]^2, y up."""
    a = np.asarray(img.convert("RGB")).astype(np.int32)
    blue = (a[:, :, 2] > 150) & (a[:, :, 0] < 140)
    ys, xs = np.nonzero(blue)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    ink = a.max(axis=2) < 90
    m = int(0.05 * (x1 - x0))      # (only inside the square: the picture's thin black frame round it is not the sign)
    inside = np.zeros_like(ink)
    inside[y0 + m:y1 - m, x0 + m:x1 - m] = True
    ink = smooth_mask(ink & inside)
    cx, cy, half = (x0 + x1) / 2 * UP, (y0 + y1) / 2 * UP, (x1 - x0) / 2 * UP
    return vector(ink, lambda x, y: ((x - cx) / half, (cy - y) / half), TOL * 2)


def main(pdf):
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "20"))
        p20 = images(pdf, 20, os.path.join(tmp, "20"))
        strips = [p20[k] for k in (16, 17, 18)]
        assert all(s.size[0] == 4352 for s in strips), "page 20's digit strips are not where they were"
        # rys. 1.4.2's second row, x in px of its 4352: 1, 2, 4, 7, 8, 9, 0 (the strips stacked: 710 px high)
        names = "1247890"
        boxes = [(2265, 40, 2430, 560), (2432, 40, 2700, 560), (2705, 40, 3010, 560), (3040, 40, 3290, 560),
                 (3300, 40, 3605, 560), (3625, 40, 3935, 560), (3940, 40, 4250, 560)]
        masks = dict(zip(names, outline_digits(strips, boxes)))
        _x0, _x1, top, base = bbox(masks["1"])         # (the 1 is flat at both lines: d, the baseline)
        digits = {k: digit_shape(m, base, base - top) for k, m in masks.items()}
        # each colour picture: (page, picture, ink, {digit: box}, the reference digit traced above, its box)
        for page, k, ink, want, ref in (
                (140, 0, lambda a: a.max(axis=2) < 90, {"5": (60, 100, 200, 300)}, ("0", (210, 100, 350, 300))),
                (171, 1, lambda a: a.min(axis=2) > 200, {"3": (40, 90, 190, 300)}, ("0", (210, 90, 360, 300))),
                (288, 1, lambda a: a.max(axis=2) < 90, {"6": (50, 30, 150, 170)}, ("1", (250, 30, 310, 170)))):
            d = os.path.join(tmp, str(page))
            os.makedirs(d)
            img = images(pdf, page, d)[k]
            got = colour_digits(img, ink, list(want.values()) + [ref[1]])
            base_px, d_px = lines_from(got[-1], digits[ref[0]])
            for name, m in zip(want, got[:-1]):
                digits[name] = digit_shape(m, base_px, d_px)
        d = os.path.join(tmp, "197")
        os.makedirs(d)
        ped = d6(images(pdf, 197, d)[1])
    out = {"_": __doc__.split("\n\n")[0] + " Made by tools/trace_polish_signs.py from Dz.U. 2019 poz. 2311.",
           "digits": {k: digits[k] for k in sorted(digits)}, "d6": ped}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
        f.write("\n")
    counts = ", ".join(f"{k}: {len(v['rings'])} rings" for k, v in sorted(digits.items()))
    print(OUT, os.path.getsize(OUT) // 1024, "KB;", counts, "; D-6:", len(ped), "rings")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
