#!/usr/bin/env python3
"""A kunstwerk's road paint, read from the aerial photo (PDOK's luchtfoto, 6.25 cm a pixel): the white marks (lane and
edge lines, stop lines, zebras, the blocks of a cycle crossing, arrows, shark's teeth) and the red of the cycle
crossings, on the streets only, as clean polygons in the kunstwerk's local metres.

White: what stands out from the asphalt around it (a white top-hat: brighter than the street's own brightness over
WHITE_OPEN_M, so a stripe in a shadow counts and the sunlit street does not), grey not coloured, and thin: a white car's
roof (wider than WHITE_MAX_HALF_M both ways) is left out. Red: reddish asphalt on a carriageway, long enough to be a
crossing and not a red car. Straight dashes are redrawn as rectangles; the rest keep their smoothed outline.

    python3 tools/kunstwerk_markings.py ../design/kunstwerken/droogleever/ref

Writes ref/photo_hr.jpg (cached) and ref/markings.json: {"marks": [{"kind": "white" | "red", "rings": [outer, holes]}]}.
"""
import argparse
import io
import json
import os
import sys
import urllib.request

import contourpy
import numpy as np
from PIL import Image
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PHOTO = ("https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
         "&LAYERS=Actueel_orthoHR&STYLES=&CRS=EPSG:28992&BBOX={x0},{y0},{x1},{y1}&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg")
PX = 0.0625             # m a pixel
TILE = 1024             # px a request
WHITE_OPEN_M = 1.6      # m: the street's brightness is its brightest over this (paint narrower than it stands out)
WHITE_HAT = 30          # grey levels a mark stands over the street around it
WHITE_GREY = 45         # at most this between its brightest and darkest channel (white, not coloured)
WHITE_MIN_L = 140       # and bright in itself (a sunlit gap between a tree's shadows is only brighter than them)
WHITE_MAX_HALF_M = 0.62  # m: no mark is wider than twice this (a car's roof is)
WHITE_MIN_M2 = 0.05
RAIL_CLEAR_M = 1.1     # m either side of a tram track's centre line (its rails at 0.72)
RECT_FILL = 0.74        # a block that fills this much of its smallest rectangle is that rectangle
WIDTHS = (0.10, 0.15, 0.20, 0.30, 0.40, 0.50)     # m: the standard widths of road paint
DASH_L = (0.5, 4.5)     # m: a dash's length
DASH_MAX_W = 0.45       # m: and at most its width
LINE_MIN_M = 2.0        # m: a line shorter than this is a block or nothing
ARROW_L = (2.0, 7.5)    # m: an arrow's length
RED_MIN_LEN_M = 5.5     # m: a cycle crossing spans a carriageway (a red car is shorter)
RED_MIN_M2 = 3.0


def fetch(ref, box):
    dst = os.path.join(ref, "photo_hr.jpg")
    meta = os.path.join(ref, "photo_hr.json")
    if os.path.exists(dst) and os.path.exists(meta) and json.load(open(meta))["box"] == list(box):
        return np.asarray(Image.open(dst).convert("RGB"))
    r = json.load(open(os.path.join(ref, "ref.json")))
    ox, oy = r["origin_rd"]
    x0, y0, x1, y1 = box
    W, H = int(round((x1 - x0) / PX)), int(round((y1 - y0) / PX))
    img = Image.new("RGB", (W, H))
    for r0 in range(0, H, TILE):
        for c0 in range(0, W, TILE):
            w, h = min(TILE, W - c0), min(TILE, H - r0)
            bx0 = ox + x0 + c0 * PX
            by1 = oy + y1 - r0 * PX
            url = PHOTO.format(x0=bx0, y0=by1 - h * PX, x1=bx0 + w * PX, y1=by1, w=w, h=h)
            with urllib.request.urlopen(url, timeout=60) as f:
                img.paste(Image.open(io.BytesIO(f.read())).convert("RGB"), (c0, r0))
            print(f"  photo {r0 // TILE},{c0 // TILE}", flush=True)
    img.save(dst, quality=93)
    json.dump({"box": list(box), "px": PX}, open(meta, "w"))
    return np.asarray(img)


def polygons(mask, x0, y1, smooth=0.8):
    """The mask's regions as rings (outer first, then holes) in metres (x east, y north)."""
    f = ndi.gaussian_filter(mask.astype(np.float32), smooth)
    H, W = mask.shape
    xs = x0 + (np.arange(W) + 0.5) * PX
    ys = y1 - (np.arange(H) + 0.5) * PX
    gen = contourpy.contour_generator(xs, ys, f, fill_type="OuterOffset")
    polys, offs = gen.filled(0.5, 10.0)
    out = []
    for P, O in zip(polys, offs):
        rings = [simplify(P[O[i]:O[i + 1]], 0.025) for i in range(len(O) - 1)]
        rings = [r for r in rings if len(r) >= 3]
        if rings:
            out.append(rings)
    return out


def simplify(ring, tol):
    """Douglas-Peucker on a closed ring."""
    ring = np.asarray(ring)
    if len(ring) > 1 and np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    if len(ring) < 4:
        return ring

    def dp(pts):
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        ab = b - a
        L = np.hypot(*ab)
        d = np.abs(np.cross(ab, pts - a)) / L if L > 1e-9 else np.hypot(*(pts - a).T)
        i = int(np.argmax(d))
        if d[i] <= tol:
            return np.array([a, b])
        return np.r_[dp(pts[:i + 1])[:-1], dp(pts[i:])]
    far = int(np.argmax(np.hypot(*(ring - ring[0]).T)))
    out = np.r_[dp(ring[:far + 1])[:-1], dp(np.r_[ring[far:], ring[:1]])[:-1]]
    return out


def rectangle(ys, xs, x0, y1):
    """A block's pixels (rows, cols) as its smallest rectangle (any way round), or None when they fill too little of it
    (an arrow, a shark's tooth: they keep their outline)."""
    from scipy.spatial import ConvexHull
    P = np.c_[x0 + (xs + 0.5) * PX, y1 - (ys + 0.5) * PX]
    if len(P) < 6:
        return None
    # the pixels' corners, so a one-pixel line still has a width
    Q = (P[:, None, :] + np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * PX / 2).reshape(-1, 2)
    try:
        H = Q[ConvexHull(Q).vertices]
    except Exception:
        return None
    best = None
    for i in range(len(H)):
        e = H[(i + 1) % len(H)] - H[i]
        a = e / max(np.hypot(*e), 1e-12)
        n = np.array([-a[1], a[0]])
        t, w = H @ a, H @ n
        ar = np.ptp(t) * np.ptp(w)
        if best is None or ar < best[0]:
            best = (ar, a, n, t.min(), t.max(), w.min(), w.max())
    ar, a, n, t0, t1, w0, w1 = best
    if len(P) * PX * PX < RECT_FILL * ar:
        return None
    if (t1 - t0) < (w1 - w0):
        a, n, t0, t1, w0, w1 = n, -a, w0, w1, -t1, -t0
    wc, wd = (w0 + w1) / 2, snap_width(w1 - w0) if (w1 - w0) < 0.6 else (w1 - w0)
    w0, w1 = wc - wd / 2, wc + wd / 2
    return [np.array([a * t0 + n * w0, a * t1 + n * w0, a * t1 + n * w1, a * t0 + n * w1])]


def snap_width(w):
    """A measured paint width to the nearest standard one (the photo's blur adds about a pixel)."""
    w = max(w - PX * 0.8, 0.08)
    return min(WIDTHS, key=lambda v: abs(v - w))


def dash(ys, xs, x0, y1):
    """A short narrow mark (a dash the photo frayed) as the rectangle along its axis: its length, a standard width."""
    P = np.c_[x0 + (xs + 0.5) * PX, y1 - (ys + 0.5) * PX]
    if len(P) < 8:
        return None
    c = P.mean(0)
    a = np.linalg.svd(P - c, full_matrices=False)[2][0]
    n = np.array([-a[1], a[0]])
    t, w = (P - c) @ a, (P - c) @ n
    L = np.ptp(t) + PX
    wd = 4 * np.std(w)                      # a bar's width from its spread (its frayed edge does not count)
    if not (DASH_L[0] <= L <= DASH_L[1] and wd <= DASH_MAX_W and L >= 2.5 * wd):
        return None
    wd = snap_width(wd + PX * 0.8)
    tm = (t.max() + t.min()) / 2
    cc = c + a * tm + n * np.median(w)
    return [np.array([cc - a * L / 2 - n * wd / 2, cc + a * L / 2 - n * wd / 2, cc + a * L / 2 + n * wd / 2,
                      cc - a * L / 2 + n * wd / 2])]


def line_strip(sub, s, x0, y1):
    """A long thin mark (a solid line, a curved guide line) as a smooth strip along its skeleton, its width a standard
    one; None when it is not long and thin."""
    from skimage.morphology import skeletonize
    sk = skeletonize(sub)
    pts = np.argwhere(sk)
    if len(pts) < 20:
        return None
    # the skeleton's longest path: from any point the farthest, from there the farthest again (8-neighbour BFS)
    idx = {tuple(p): k for k, p in enumerate(pts)}

    def bfs(start):
        prev = {start: None}
        order = [start]
        for cur in order:
            r, c = pts[cur]
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    k = idx.get((r + dr, c + dc))
                    if k is not None and k not in prev:
                        prev[k] = cur
                        order.append(k)
        return order[-1], prev
    far, _ = bfs(0)
    end, prev = bfs(far)
    path = []
    k = end
    while k is not None:
        path.append(pts[k])
        k = prev[k]
    path = np.array(path, np.float64)
    if len(path) < 20:
        return None
    half = ndi.distance_transform_edt(sub)[tuple(path.astype(int).T)]
    width = snap_width(2 * np.median(half) * PX)
    P = np.c_[x0 + (path[:, 1] + s[1].start + 0.5) * PX, y1 - (path[:, 0] + s[0].start + 0.5) * PX]
    k = min(15, len(P) // 3 * 2 + 1)
    P = np.c_[np.convolve(np.pad(P[:, 0], k // 2, mode="edge"), np.ones(k) / k, "valid"),
              np.convolve(np.pad(P[:, 1], k // 2, mode="edge"), np.ones(k) / k, "valid")]
    L = np.hypot(*np.diff(P, axis=0).T).sum()
    if L < LINE_MIN_M or L < 8 * width or sub.sum() * PX * PX > 1.6 * L * (width + 2 * PX):
        return None
    t = np.linspace(0, 1, max(2, int(L / 0.5) + 1))
    sl_ = np.r_[0, np.cumsum(np.hypot(*np.diff(P, axis=0).T))] / L
    P = np.c_[np.interp(t, sl_, P[:, 0]), np.interp(t, sl_, P[:, 1])]
    d = np.gradient(P, axis=0)
    nv = np.c_[-d[:, 1], d[:, 0]] / np.maximum(np.hypot(*d.T), 1e-9)[:, None]
    return [np.r_[P + nv * width / 2, (P - nv * width / 2)[::-1]]]


def arrow(ys, xs, x0, y1):
    """A straight-ahead arrow as the standard one (its shaft and head along the mark's axis, the head at its wider
    end); None when the mark is not one (a turn arrow too: drawn wrong it would say something else)."""
    P = np.c_[x0 + (xs + 0.5) * PX, y1 - (ys + 0.5) * PX]
    area = len(P) * PX * PX
    c = P.mean(0)
    a = np.linalg.svd(P - c, full_matrices=False)[2][0]
    n = np.array([-a[1], a[0]])
    t, w = (P - c) @ a, (P - c) @ n
    L = np.ptp(t)
    if not (ARROW_L[0] <= L <= ARROW_L[1] and 0.4 <= area <= 4.5 and np.ptp(w) <= 1.4):
        return None
    lo, hi = t.min() + 0.3 * L, t.max() - 0.3 * L
    wl, wh = np.ptp(w[t < lo]) if (t < lo).any() else 0, np.ptp(w[t > hi]) if (t > hi).any() else 0
    if max(wl, wh) < 1.6 * min(wl, wh) + 0.2:
        return None
    if wh < wl:
        a, n, t, w = -a, -n, -t, -w
    head_end = w[t > t.max() - 0.3 * L]
    if abs(head_end.mean() - w[t < t.min() + 0.5 * L].mean()) > 0.25:
        return None                                  # its head off to one side: a turn
    wm = np.median(w[t < t.min() + 0.5 * L])
    base = c + n * wm
    t0, t1 = t.min(), t.max()
    th = t1 - min(1.6, 0.35 * L)
    sw, hw = 0.12, 0.45
    pts = [(t0, -sw), (th, -sw), (th, -hw), (t1, 0.0), (th, hw), (th, sw), (t0, sw)]
    return [np.array([base + a * tt + n * ww for tt, ww in pts])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ref")
    a = ap.parse_args(argv)
    import kunstwerk_solid as ks
    from kunstwerk_design import carriageways
    ref = json.load(open(os.path.join(a.ref, "ref.json"), encoding="utf-8"))
    frame, _ = carriageways(a.ref)
    c0, u, n = frame
    corners = np.array([c0 + u * t + n * w for t in ks.AREA_T for w in (-ks.AREA_W, ks.AREA_W)])
    box = tuple(float(v) for v in (*np.floor(corners.min(0)), *np.ceil(corners.max(0))))
    img = fetch(a.ref, box).astype(np.int16)
    x0, y0, x1, y1 = box
    H, W = img.shape[:2]
    # where paint may be: the plan's carriageways and cycle paths (its kinds on its 0.25 m raster, to the photo's)
    plan = ks.Plan(ref, frame, box)
    lab = np.argmax(plan.share, axis=0)[::-1]          # rows north to south, as the photo
    k = int(round(ks.RES / PX))
    lab = np.repeat(np.repeat(lab, k, 0), k, 1)[:H, :W]
    names = [kk[0] for kk in ks.KINDS]
    road = lab == names.index("road")
    paved = road | (lab == names.index("cycle"))
    # the tram's rails shine: nothing within RAIL_CLEAR_M of a track's centre line is paint
    from matplotlib.path import Path as _P  # noqa: F401
    rails = np.zeros((H, W), bool)
    for f in ref["features"]:
        if f["layer"] != "spoor":
            continue
        ln = np.asarray(f["rings"][0], np.float64)
        for p0, p1 in zip(ln[:-1], ln[1:]):
            m = int(np.ceil(np.hypot(*(p1 - p0)) / (PX * 4))) + 1
            for q in np.linspace(p0, p1, m):
                c, r_ = int((q[0] - x0) / PX), int((y1 - q[1]) / PX)
                if 0 <= r_ < H and 0 <= c < W:
                    rails[r_, c] = True
    rails = ndi.binary_dilation(rails, iterations=int(RAIL_CLEAR_M / PX))
    R, G, B = img[..., 0], img[..., 1], img[..., 2]
    L = (R * 3 + G * 6 + B) // 10
    marks = []
    # white
    sz = int(WHITE_OPEN_M / PX)
    street = ndi.grey_opening(np.where(road, L, 0).astype(np.uint8), size=(sz, sz)).astype(np.int16)
    grey = (np.max(img, 2) - np.min(img, 2)) <= WHITE_GREY
    white = road & grey & ((L - street) >= WHITE_HAT) & (L > WHITE_MIN_L) & ~rails
    white = ndi.binary_opening(white, np.ones((1, 2))) | ndi.binary_opening(white, np.ones((2, 1)))  # (specks out, one-pixel lines kept)
    lbl, nlab = ndi.label(white)
    half = ndi.maximum(ndi.distance_transform_edt(white), lbl, np.arange(1, nlab + 1)) * PX
    area = ndi.sum(white, lbl, np.arange(1, nlab + 1)) * PX * PX
    keep = np.flatnonzero((half <= WHITE_MAX_HALF_M) & (area >= WHITE_MIN_M2)) + 1
    sl = ndi.find_objects(lbl)
    rest = np.zeros_like(white)
    nrect = 0
    # each mark drawn as what it is, clean: a block as its rectangle, a line as a smooth strip of a standard width, an
    # arrow as the standard arrow; what is none of these (paint merged with a shadow's edge, a pole on the photo) is
    # left out
    count = {"block": 0, "line": 0, "arrow": 0, "dropped": 0}
    for i in keep:
        s = sl[i - 1]
        sub = lbl[s] == i
        ys, xs = np.nonzero(sub)
        ys, xs = ys + s[0].start, xs + s[1].start
        shape, what = rectangle(ys, xs, x0, y1), "block"
        if shape is None:
            shape, what = dash(ys, xs, x0, y1), "block"
        if shape is None:
            shape, what = line_strip(sub, s, x0, y1), "line"
        if shape is None:
            shape, what = arrow(ys, xs, x0, y1), "arrow"
        if shape is None:
            count["dropped"] += 1
            if os.environ.get("KW_DEBUG"):
                P = np.c_[x0 + (xs + 0.5) * PX, y1 - (ys + 0.5) * PX]
                c = P.mean(0)
                if 20 < c[0] < 60 and 44 < c[1] < 62:
                    print("dropped", c.round(1), len(P), np.ptp(P, 0).round(2))
            continue
        marks.append({"kind": "white", "rings": [r.round(3).tolist() for r in shape]})
        count[what] += 1
    print(f"white: {nlab} blobs, {len(keep)} kept: {count}")
    # red: on the carriageways
    red = road & (R - G > 28) & (R > 1.25 * G) & (R > 1.2 * B)
    red = ndi.binary_closing(ndi.binary_opening(red, np.ones((4, 4))), np.ones((6, 6)))
    lbl, nlab = ndi.label(red)
    nred = 0
    for i, s in enumerate(ndi.find_objects(lbl), 1):
        ys, xs = np.nonzero(lbl[s] == i)
        if len(ys) * PX * PX < RED_MIN_M2:
            continue
        P = np.c_[xs, ys] * PX
        ext = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][0]
        if np.ptp(P @ ext) < RED_MIN_LEN_M:
            continue
        m = np.zeros(red.shape, bool)
        m[s] = lbl[s] == i
        for rings in polygons(m, x0, y1, smooth=2.0):
            marks.append({"kind": "red", "rings": [r.round(3).tolist() for r in rings]})
            nred += 1
    print(f"red: {nred} crossings")
    json.dump({"marks": marks}, open(os.path.join(a.ref, "markings.json"), "w"))


if __name__ == "__main__":
    main()
