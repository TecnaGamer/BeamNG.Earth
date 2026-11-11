"""Court and pitch markings on the mapped sports pitches.

Overture's land_use has ~2600 `pitch` polygons per cell set, most with the sport in their OSM tags (soccer, tennis, basketball,
volleyball). Each polygon is fitted with its smallest enclosing rectangle; when the polygon fills it, the sport is one
we know and the size is plausible, the sport's white lines are laid in it as road mesh line chunks (parking.STALL_LINE:
the road mesh drapes them on the ground, as the bays of a bus yard). No sport, several sports, a roofed hall, a pitch that
is not a rectangle or whose size does not fit the sport: no lines (wrong markings are worse than none). BNE_PITCHES=off
turns the module off.
"""
import json
import math
import os

import numpy as np

import mapgeom

ON = os.environ.get("BNE_PITCHES", "on").lower() not in ("off", "0", "false")

FILL_MIN = 0.80             # the polygon's area / its rectangle's: less is not a pitch's rectangle
ARC_STEP_DEG = 12.0         # a circle or an arc as a polyline of segments this far apart
SPORTS = {"soccer": "soccer", "football": "soccer", "tennis": "tennis", "basketball": "basketball",
          "volleyball": "volleyball", "beachvolleyball": "beach"}


class Pitch:
    def __init__(self, fid, sport, surface, xy, centre, u, length, width, fill):
        self.fid, self.sport, self.surface, self.xy = fid, sport, surface, xy
        self.centre, self.u, self.length, self.width, self.fill = centre, u, length, width, fill

    def __repr__(self):
        return f"Pitch({self.fid}, {self.sport}, {self.length:.1f}x{self.width:.1f}, fill {self.fill:.2f})"


# --- geometry ----------------------------------------------------------------------------------------------------

def _area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))) / 2.0


def _hull(pts):
    p = sorted(set(map(tuple, np.round(np.asarray(pts, np.float64), 4))))
    if len(p) < 3:
        return np.array(p)

    def half(points):
        out = []
        for q in points:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (q[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(q)
        return out
    lo, up = half(p), half(p[::-1])
    return np.array(lo[:-1] + up[:-1])


def fit_rect(xy):
    """(centre (2,), long-axis unit vector (2,), length, width, fill) of the smallest rectangle round xy (k, 2), or None."""
    xy = np.asarray(xy, np.float64)
    if len(xy) < 4:
        return None
    h = _hull(xy)
    if len(h) < 3:
        return None
    best = None
    for a, b in zip(h, np.roll(h, -1, axis=0)):
        d = b - a
        n = float(np.hypot(*d))
        if n < 1e-6:
            continue
        u = d / n
        v = np.array([-u[1], u[0]])
        pu, pv = h @ u, h @ v
        area = float((pu.max() - pu.min()) * (pv.max() - pv.min()))
        if best is None or area < best[0]:
            best = (area, u, v, pu.min(), pu.max(), pv.min(), pv.max())
    area, u, v, u0, u1, v0, v1 = best
    if area < 1.0:
        return None
    centre = u * (u0 + u1) / 2 + v * (v0 + v1) / 2
    L, W = u1 - u0, v1 - v0
    if W > L:
        u, L, W = v, W, L
    return centre, u, float(L), float(W), min(1.0, _area(xy) / area)


# --- the layouts (x along the long side, y across; origin = the centre; metres) -----------------------------------------

def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def _arc(cx, cy, r, a0, a1):
    n = max(2, int(math.ceil(abs(a1 - a0) / ARC_STEP_DEG)) + 1)
    return [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in np.linspace(a0, a1, n)]


def soccer(L, W):
    """Touchlines, halfway line, centre circle, penalty and goal areas, scaled from 105 x 68 m."""
    kx, ky = L / 105.0, W / 68.0
    hl, hw = L / 2, W / 2
    out = [_rect(-hl, -hw, hl, hw), [(0, -hw), (0, hw)], _arc(0, 0, 9.15 * min(kx, ky), 0, 360)]
    if L >= 40.0:
        for s in (-1, 1):
            out.append([(s * hl, 20.15 * ky), (s * (hl - 16.5 * kx), 20.15 * ky),
                        (s * (hl - 16.5 * kx), -20.15 * ky), (s * hl, -20.15 * ky)])
            out.append([(s * hl, 9.16 * ky), (s * (hl - 5.5 * kx), 9.16 * ky),
                        (s * (hl - 5.5 * kx), -9.16 * ky), (s * hl, -9.16 * ky)])
    return out


def tennis(L, W):
    """One doubles court (23.77 x 10.97 m: singles sidelines, service lines, the centre service line); as many as the
    polygon's width holds (the usual 18.3 m a court with its run-off)."""
    out = []
    n = max(1, int(round(W / 18.3))) if W >= 29.0 else 1
    for k in range(n):
        cy = (k - (n - 1) / 2) * (W / n)
        hl, hw, sw, sv = 23.77 / 2, 10.97 / 2, 8.23 / 2, 6.40
        out += [_rect(-hl, cy - hw, hl, cy + hw), [(-hl, cy - sw), (hl, cy - sw)], [(-hl, cy + sw), (hl, cy + sw)],
                [(-sv, cy - sw), (-sv, cy + sw)], [(sv, cy - sw), (sv, cy + sw)], [(-sv, cy), (sv, cy)]]
    return out


def basketball(L, W):
    """28 x 15 m: the centre line and circle, the keys (4.9 x 5.8 m), the free-throw circles and the three-point arcs,
    scaled to the polygon's rectangle."""
    kx, ky = L / 28.0, W / 15.0
    out = [_rect(-14 * kx, -7.5 * ky, 14 * kx, 7.5 * ky), [(0, -7.5 * ky), (0, 7.5 * ky)]]

    def sc(pts):
        return [(x * kx, y * ky) for x, y in pts]
    out.append(sc(_arc(0, 0, 1.8, 0, 360)))
    for s in (-1, 1):
        out.append(sc([(s * 14, 2.45), (s * (14 - 5.8), 2.45), (s * (14 - 5.8), -2.45), (s * 14, -2.45)]))
        out.append(sc([(s * (14 - 5.8) + s * 1.8 * math.cos(math.radians(a)), 1.8 * math.sin(math.radians(a)))
                       for a in np.linspace(-90, 90, 16)]))
        bx = s * (14 - 1.575)                               # the basket's centre
        th = math.degrees(math.acos(min(1.0, 6.6 / 6.75)))       # (the arc meets the straight part 0.9 m off the sideline)
        ang = np.linspace(-(90 - th), 90 - th, 16)
        arc = [(bx - s * 6.75 * math.cos(math.radians(a)), 6.75 * math.sin(math.radians(a))) for a in ang]
        out.append(sc([(s * 14, arc[0][1])] + arc + [(s * 14, arc[-1][1])]))
    return out


def volleyball(L, W):
    """18 x 9 m: the centre line and the attack lines 3 m from it."""
    kx, ky = L / 18.0, W / 9.0
    return [_rect(-9 * kx, -4.5 * ky, 9 * kx, 4.5 * ky), [(0, -4.5 * ky), (0, 4.5 * ky)],
            [(-3 * kx, -4.5 * ky), (-3 * kx, 4.5 * ky)], [(3 * kx, -4.5 * ky), (3 * kx, 4.5 * ky)]]


def beach(L, W):
    kx, ky = L / 16.0, W / 8.0
    return [_rect(-8 * kx, -4 * ky, 8 * kx, 4 * ky), [(0, -4 * ky), (0, 4 * ky)]]


# what the polygon's rectangle must be for the sport: (length range, width range, ratio range) -> layout
RULES = {"soccer": ((20.0, 130.0), (12.0, 90.0), (1.15, 2.1), soccer),
         "tennis": ((22.0, 40.0), (10.5, 40.0), (1.0, 4.0), tennis),
         "basketball": ((22.0, 34.0), (12.0, 20.0), (1.4, 2.2), basketball),
         "volleyball": ((16.0, 20.0), (8.0, 10.5), (1.6, 2.4), volleyball),
         "beach": ((14.0, 18.0), (7.0, 9.5), (1.6, 2.4), beach)}


def layout(p):
    """The white lines of Pitch p as polylines in the local frame (or None: the sport is not marked, or the rectangle does
    not fit it). Tennis lines keep their real size (a fenced area round a court gets the court in its middle)."""
    rule = RULES.get(p.sport)
    if rule is None:
        return None
    (l0, l1), (w0, w1), (r0, r1), fn = rule
    L, W = p.length, p.width
    if not (l0 <= L <= l1 and w0 <= W <= w1 and r0 <= L / max(W, 0.1) <= r1) or p.fill < FILL_MIN:
        return None
    return fn(L, W)


def _to_world(p, lines):
    u = p.u
    v = np.array([-u[1], u[0]])
    out = []
    for ln in lines:
        a = np.asarray(ln, np.float64).reshape(-1, 2)
        w = p.centre + np.outer(a[:, 0], u) + np.outer(a[:, 1], v)
        out.append(w)
    return out


# --- reading the cells -------------------------------------------------------------------------------------------

def sport_of(tags, subtype=""):
    """The one sport of a pitch's OSM tags (a JSON string), or None (none mapped, or several)."""
    try:
        t = json.loads(tags) if isinstance(tags, str) and tags.strip().startswith("{") else {}
    except ValueError:
        t = {}
    names = {SPORTS.get(s.strip().lower()) or "other:" + s.strip().lower() for s in str(t.get("sport", "")).split(";") if s.strip()}
    if len(names) != 1:
        return None
    s = next(iter(names))
    return None if s.startswith("other:") else s


TURF_SURFACES = {"grass", "artificial_turf", "turf"}


def turf_mask(feats):
    """Which land_use polygons of Features feats are mown turf pitches (bool (n,), or None): Overture's surface
    recreation_grass, an OSM surface of grass or artificial turf, or a football pitch with no surface mapped at all.
    surface.paint_golf gives them the fairway layer: short mown grass, no 3D grass blades through the lines."""
    if not ON or feats is None or not len(feats) or feats.attrs.get("class") is None:
        return None
    cls = feats.attrs["class"].astype(str)
    sel = (cls == "pitch") & (feats.kind == mapgeom.POLYGON)
    if not sel.any():
        return None
    surf = feats.attrs.get("surface")
    tags = feats.attrs.get("tags")
    out = np.zeros(len(feats), bool)
    for i in np.flatnonzero(sel):
        osm = ""
        try:
            raw = str(tags[i]) if tags is not None else ""
            osm = str((json.loads(raw) if raw.startswith("{") else {}).get("surface", "")).lower()
        except ValueError:
            pass
        ov = str(surf[i]).strip().lower() if surf is not None else ""
        ov = "" if ov in ("nan", "none") else ov
        if ov == "recreation_grass" or osm in TURF_SURFACES:
            out[i] = True
        elif not ov and not osm and sport_of(str(tags[i]) if tags is not None else "") == "soccer":
            out[i] = True
    return out if out.any() else None


def read(frame, land_list, box, pad=40.0):
    """[Pitch] within box (x0, y0, x1, y1) grown by pad m, from the land_use Features lists of the cells round it."""
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    out, seen = [], set()
    for f in land_list or ():
        if f is None or not len(f) or f.attrs.get("class") is None:
            continue
        cls = f.attrs["class"].astype(str)
        tags = f.attrs.get("tags")
        surf = f.attrs.get("surface")
        for i in np.flatnonzero((cls == "pitch") & (f.kind == mapgeom.POLYGON)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            sport = sport_of(str(tags[i]) if tags is not None else "")
            if sport is None:
                continue
            parts = [pt for pt in f.parts(i) if pt and len(pt[0]) >= 4]
            if len(parts) != 1:
                continue
            e, n = mapgeom.enu_xy(f, frame, np.asarray(parts[0][0], np.float64))
            xy = np.column_stack((e, n)).astype(np.float64)
            if xy[:, 0].max() < x0 or xy[:, 0].min() > x1 or xy[:, 1].max() < y0 or xy[:, 1].min() > y1:
                continue
            seen.add(fid)
            fit = fit_rect(xy)
            if fit is None:
                continue
            centre, u, L, W, fill = fit
            out.append(Pitch(fid, sport, str(surf[i]) if surf is not None else "", xy, centre, u, L, W, fill))
    return out


def _in_buildings(p, rings):
    """True when the pitch's middle lies inside a building (a roofed sports hall mapped as a pitch)."""
    import walkways
    pt = p.centre[None]
    for r in rings or ():
        if r[:, 0].max() < pt[0, 0] or r[:, 0].min() > pt[0, 0] or r[:, 1].max() < pt[0, 1] or r[:, 1].min() > pt[0, 1]:
            continue
        if walkways.inside(r, pt)[0]:
            return True
    return False


def segments(pitches, buildings=None):
    """[((x, y, 0), (x, y, 0))] of the markings of the pitches (world metres), each polyline split into segments."""
    segs = []
    for p in pitches:
        lines = layout(p)
        if not lines or _in_buildings(p, buildings):
            continue
        for w in _to_world(p, lines):
            segs += [((float(a[0]), float(a[1]), 0.0), (float(b[0]), float(b[1]), 0.0)) for a, b in zip(w[:-1], w[1:])]
    return segs


def lines(frame, tx, ty, md):
    """The road mesh line chunks of the pitch markings in road tile (tx, ty) (blockworker)."""
    if not ON or md is None:
        return []
    import parking
    import roads
    box = roads.tile_bbox(tx, ty)
    land = [md.cell(k, layers=("land_use",), kind="demand").layers.get("land_use")
            for k in roads.cells_of_tile(frame, tx, ty, margin=40.0)]
    found = read(frame, land, box)
    if not found:
        return []
    import buildings as buildingsmod
    segs = segments(found, buildingsmod.near_rings(frame, tx, ty, md, margin=40.0))
    return parking._boxed_chunks(segs, box) if segs else []
