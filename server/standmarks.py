"""Stand marking shapes: the lettering and the plane geometry of the painted boxes of an aircraft stand (taxiwaymarks draws them:
the stand number and the type labels on yellow boxes, the taxiways' names on black ones, the red hatched box under a jet bridge, the
service road, the equipment parking). Pure: metres in a plane, no ground; every shape is a convex polygon (k, 2), counter-clockwise
or not (the road mesh turns its triangles up), or a polyline.

Font: DejaVu Sans Bold as a texture (tools/make_font_atlas.py: the sheet's table is data/paint_font.json). A character is ONE quad with
its UVs into that sheet (the sheet is the letters' opacity, the paint's material gives the colour: line_runway_font_black / _yellow /
_white): text_quads(text, centre, up, h) -> [(corners (4, 2), uvs (4, 2))], a line of capitals h tall (h = the capital's height),
centred on `centre` (its ink, the capitals' middle: a letter with a tail hangs below), upright as read looking along `up` (left to right
across it). The corners run top left, top right, bottom right, bottom left as the reader sees them, and the UVs the same way round
(the sheet's V runs up: markings.arrow_quad's convention, its picture upright when the quad's top is ahead).
"""
import json
import os

import numpy as np

import dnp

_EPS = 1e-9
_FONT = None


def _table():
    global _FONT
    if _FONT is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "paint_font.json"), encoding="utf-8") as f:
            _FONT = json.load(f)
    return _FONT


def font():
    """The table of the font sheet: {character: {"uv": [u0, v0, u1, v1], "box": [x0, y0, x1, y1], "adv": advance}} (capital heights: tools/
    make_font_atlas.py's docstring), read once."""
    return _table()["chars"]


def supported(text):
    """Can the font draw every character of text (a non-empty text of the sheet's characters: A-Z, 0-9, - / . and the arrows)?"""
    return bool(text) and all(ch in font() for ch in text)


def solid():
    """The (u, v) of a texel in the font sheet's cell that is opaque all over: a quad with this at all four corners is a solid fill in the
    letters' material (the ring of a name's box)."""
    return _table()["solid"]


def _unit(v):
    v = np.asarray(v, np.float64)
    return v / max(float(dnp.hypot(v[0], v[1])), _EPS)


def _pens(text):
    """([pen position of each character (capital heights)], left, right): where the characters stand along the baseline and the extent
    of their quads' ink from the first pen position."""
    f, x, pens = font(), 0.0, []
    for ch in text:
        pens.append(x)
        x += f[ch]["adv"]
    return pens, f[text[0]]["box"][0], pens[-1] + f[text[-1]]["box"][2]


def text_width(text, h):
    """The width of `text` at capital height h: from the first character's left to the last one's right."""
    _pens_, left, right = _pens(text)
    return (right - left) * h


def text_quads(text, centre, up, h):
    """The quads of `text` (supported()), as the module docstring says: [(corners (4, 2), uvs (4, 2))]; [] for a text the font lacks."""
    if not supported(text):
        return []
    up = _unit(up)
    right = np.array([up[1], -up[0]])
    c = np.asarray(centre, np.float64)
    pens, left, edge = _pens(text)
    mid = (left + edge) / 2.0
    out = []
    for pen, ch in zip(pens, text):
        g = font()[ch]
        x0, y0, x1, y1 = g["box"]
        u0, v0, u1, v1 = g["uv"]
        local = [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]                 # (top left, top right, bottom right, bottom left)
        corners = np.array([c + right * ((pen + x - mid) * h) + up * ((y - 0.5) * h) for x, y in local])
        out.append((corners, np.array([[u0, v1], [u1, v1], [u1, v0], [u0, v0]])))
    return out


def box(centre, up, w, h):
    """The rectangle (4, 2) w across and h along `up`, centred on `centre`."""
    up = _unit(up)
    right = np.array([up[1], -up[0]])
    c = np.asarray(centre, np.float64)
    a, b = right * (w / 2.0), up * (h / 2.0)
    return np.array([c - a - b, c + a - b, c + a + b, c - a + b])


def outline(centre, up, w, h, lw):
    """The four bars (4, 2) of a rectangle's outline lw wide, inside the rectangle w x h."""
    up = _unit(up)
    right = np.array([up[1], -up[0]])
    c = np.asarray(centre, np.float64)
    out = []
    for sx, sy, ww, hh in ((0, -1, w, lw), (0, 1, w, lw), (-1, 0, lw, h - 2 * lw), (1, 0, lw, h - 2 * lw)):
        at = c + right * (sx * (w - lw) / 2.0) + up * (sy * (h - lw) / 2.0)
        out.append(box(at, up, ww, hh))
    return out


def clip(poly, normal, offset):
    """The part of the convex polygon (k, 2) where normal . p <= offset (Sutherland-Hodgman); (0, 2) where nothing is left."""
    pts = np.asarray(poly, np.float64)
    out = []
    n = len(pts)
    for i in range(n):
        p, q = pts[i], pts[(i + 1) % n]
        dp, dq = float(normal[0] * p[0] + normal[1] * p[1]) - offset, float(normal[0] * q[0] + normal[1] * q[1]) - offset
        if dp <= 0.0:
            out.append(p)
        if (dp < 0.0 < dq) or (dq < 0.0 < dp):
            out.append(p + (q - p) * (dp / (dp - dq)))
    return np.array(out, np.float64).reshape(-1, 2)


def hatch(centre, up, w, h, pitch, lw):
    """The 45 degree hatching of the rectangle w across and h along `up`: strips lw wide, pitch apart (across them), each
    clipped to the rectangle, [(k, 2) convex polygons]."""
    up = _unit(up)
    right = np.array([up[1], -up[0]])
    rect = box(centre, up, w, h)
    c = np.asarray(centre, np.float64)
    d = _unit(right + up)                               # the strips run along d, are spread across n
    n = np.array([-d[1], d[0]])
    reach = (w + h) / 2.0
    out = []
    k = -int(reach / pitch)
    while k * pitch <= reach:
        off = float((c[0] * n[0] + c[1] * n[1])) + k * pitch
        p = clip(clip(rect, n, off + lw / 2.0), -n, -(off - lw / 2.0))
        if len(p) >= 3:
            out.append(p)
        k += 1
    return out


def offset(pts, d):
    """The polyline pts (k, 2) moved d to its left (mitred, the miter at most 2 d long), (k, 2)."""
    P = np.asarray(pts, np.float64)
    seg = P[1:] - P[:-1]
    ln = np.maximum(dnp.hypot(seg[:, 0], seg[:, 1]), _EPS)
    nrm = np.stack([-seg[:, 1], seg[:, 0]], 1) / ln[:, None]
    out = np.empty_like(P)
    out[0] = P[0] + nrm[0] * d
    out[-1] = P[-1] + nrm[-1] * d
    for i in range(1, len(P) - 1):
        m = nrm[i - 1] + nrm[i]
        mm = float(m[0] * m[0] + m[1] * m[1])
        k = 2.0 / max(mm, 1.0) if mm > _EPS else 1.0    # (1 / cos of half the bend: capped at 2)
        out[i] = P[i] + m * (d * k)
    return out


def cumulative(pts):
    """The distance along the polyline pts at each vertex."""
    P = np.asarray(pts, np.float64)
    return np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(P, axis=0).T))]


def at(pts, s):
    """(point, unit direction) of the polyline pts a distance s along it (clamped to its ends)."""
    P = np.asarray(pts, np.float64)
    cum = cumulative(P)
    s = min(max(s, 0.0), float(cum[-1]))
    j = min(int(np.searchsorted(cum, s, side="right")) - 1, len(P) - 2)
    j = max(j, 0)
    d = P[j + 1] - P[j]
    ln = max(float(cum[j + 1] - cum[j]), _EPS)
    return P[j] + d * ((s - float(cum[j])) / ln), d / ln


def dashes(pts, dash, gap, width, start=0.0):
    """The dashes (4, 2) of width `width` along the polyline pts: dash long, gap apart, the first at `start`, the pattern
    centred so that both ends of the line end in a dash (a short line is one dash)."""
    P = np.asarray(pts, np.float64)
    total = float(cumulative(P)[-1])
    n = max(1, int((total + gap) // (dash + gap)))
    s0 = start + (total - (n * dash + (n - 1) * gap)) / 2.0
    out = []
    for i in range(n):
        a, b = s0 + i * (dash + gap), s0 + i * (dash + gap) + dash
        # (a dash across a bend is cut at the polyline's vertices: a quad per straight piece)
        cum = cumulative(P)
        cuts = [a] + [float(c) for c in cum[1:-1] if a + 1e-6 < c < b - 1e-6] + [b]
        for x, y in zip(cuts[:-1], cuts[1:]):
            (p, dp), (q, _dq) = at(P, x), at(P, y)
            nv = np.array([-dp[1], dp[0]]) * (width / 2.0)
            out.append(np.array([p - nv, q - nv, q + nv, p + nv]))
    return out


def crossings(a, b):
    """[(point, t_a along polyline a (metres), unit direction of a there)] where the polyline a (k, 2) crosses polyline b (m, 2)."""
    A, B = np.asarray(a, np.float64), np.asarray(b, np.float64)
    cum = cumulative(A)
    out = []
    for i in range(len(A) - 1):
        p, d = A[i], A[i + 1] - A[i]
        for j in range(len(B) - 1):
            q, e = B[j], B[j + 1] - B[j]
            den = d[0] * e[1] - d[1] * e[0]
            if abs(den) < _EPS:
                continue
            w = q - p
            t = (w[0] * e[1] - w[1] * e[0]) / den
            s = (w[0] * d[1] - w[1] * d[0]) / den
            if 0.0 <= t <= 1.0 and 0.0 <= s <= 1.0:
                ln = max(float(dnp.hypot(d[0], d[1])), _EPS)
                out.append((p + d * t, float(cum[i]) + t * ln, d / ln))
    return out


def triangle_row(centre, along, across_w, count, base, height):
    """A row of give-way triangles (3, 2) across a road: count of them spread over across_w, centred on `centre`, each `base`
    wide (across) and `height` long, the apex `height` along the unit vector `along` from its base."""
    along = _unit(along)
    right = np.array([along[1], -along[0]])
    c = np.asarray(centre, np.float64)
    out = []
    for i in range(count):
        x = (i + 0.5) * across_w / count - across_w / 2.0
        m = c + right * x
        out.append(np.array([m - right * (base / 2.0), m + right * (base / 2.0), m + along * height]))
    return out
