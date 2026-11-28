"""Paved taxiways and aprons: the ground of an airport's apron and taxiway network painted as asphalt (GitHub #607:
only the runways were, so the rest of an airfield read as dirt in the ground layers and the photo).

pads(feats_list) -> [(lat (k,), lon (k,))]: the Overture "infrastructure" Features (subtype "airport") of the cells
around a block, as pads the way surface.lot_pads gives the parking lots: each apron's outer ring, and each taxiway
and taxilane (class "taxiway" / "taxilane", lines) buffered to its width, one mitred outline per run of gentle bends
and an octagon at each end and sharp bend (where it meets another way, an apron or a runway: no notch); the mapped
runway and stopway lines (also paved) end square. A cell of a
big airport is a few hundred outlines, not one rectangle per segment (Brisbane: 4407 pads, 63 ms a 512² paint). The width is
the OSM `width` tag when it reads as a length (23, "23 m", "75 ft"), else a default by class: a taxiway of an
international airport (an "international_airport" aerodrome in the same cell's Features) is a code E strip, the rest
of the world's a code B or C one. A way or apron whose surface (the column, or the tags') is unpaved (grass, dirt,
gravel) is left to the land cover. Pure; each Features' pads are kept on it, so every tile of a cell shares them.

boundaries(feats_list) -> [(lat (k,), lon (k,))]: an airport's boundary for the ground painters (surface.photo_lots mows the
infield inside it): the aerodrome polygons of the Features (class "international_airport", "airport", "airfield" ... in
subtype "airport"), else, for Features with pads, the pads' hull grown by INFIELD_GROW_M. gates(feats_list) -> [(lat, lon)]:
the gate and parking-position points (the aprons' stands' centrelines: the oil and tyre stains).
"""
import json
import math
import re

import numpy as np

import dmath
import dnp
import runwaysnap

SUBTYPE = "airport"
APRON = "apron"
WAYS = ("taxiway", "taxilane")
AERODROME = "international_airport"
TAXIWAY_M = 15.0            # m: a taxiway's default width (code B / C: ICAO's 15 m); the taxilane's below
TAXIWAY_INTL_M = 23.0       # an international airport's (code D / E: 23 m)
TAXILANE_M = 10.5           # a taxilane (apron taxiway, code B / C)
TAXILANE_INTL_M = 15.0
MIN_WIDTH_M = 3.0           # a width tag outside these is a typo (a "2300"), the default's instead
MAX_WIDTH_M = 80.0
JOIN_SIDES = 8              # the octagon at a way's end and at a sharp bend
RUNWAY = "runway"
STOPWAY = "stopway"
RUNWAY_M = 30.0             # a runway's default width (OSM's runway line: only the fillets read it), code 3 / 4
RUNWAY_INTL_M = 45.0
JOIN_TOL_M = 2.0            # a way's end this near another way's or a runway's centreline joins it: a fillet there
FILLET_MIN_DEG = 10.0       # a way leaving the pavement it joins at less than this runs along it: no fillet
FILLET_R_K = 3.0            # a fillet's arc radius: this many of the way's half widths (ICAO: ~ the taxiway's width+)
FILLET_MAX_K = 8.0          # its tangent run along each edge at most this many of them (a shallow corner's arc tightens)
FILLET_ARC_N = 9            # points on a fillet's arc
FILLET_MIN_RUN_M = 2.0      # a fillet with less room than this on an edge is left out
STRAIGHT_DEG = 20.0         # an edge is straight while it stays within this of its first direction (_straight)
SHARP_TURN_DEG = 60.0       # a bend sharper than this: the outline breaks there and an octagon holds the join
UNPAVED = {"grass", "dirt", "gravel", "ground", "sand", "unpaved", "earth", "compacted", "fine_gravel"}
INFIELD_GROW_M = 150.0      # the pads' hull grown this far is the boundary of an airport the map draws no aerodrome polygon for
HULL_DIRS = 24              # the hull is the polygon through the pads' extreme points in this many directions (grown by an octagon)
GATE_CLASSES = ("airport_gate", "parking_position")
M_PER_DEG = 6371008.8 * math.pi / 180.0
FOOT_M = 0.3048
_WIDTH = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|ft|')?\s*$", re.I)
_KEY = "_aeroway_pads"
_SNAP = "_aeroway_snap"


def width_tag(value):
    """A width tag's metres ("23", "23.5 m", "75 ft", "75'"), None when it is no length or an implausible one."""
    m = _WIDTH.match(str(value)) if value is not None else None
    if m is None:
        return None
    w = float(m.group(1).replace(",", ".")) * (FOOT_M if (m.group(2) or "m").lower() in ("ft", "'") else 1.0)
    return w if MIN_WIDTH_M <= w <= MAX_WIDTH_M else None


def _tags(feats, i):
    values = feats.attrs.get("tags")
    if values is None or not values[i]:
        return {}
    try:
        tags = json.loads(str(values[i]))
    except (ValueError, TypeError):
        return {}
    return tags if isinstance(tags, dict) else {}


def _unpaved(feats, i, tags):
    """Is feature i's surface (the column's, else the tags') a bare one?"""
    col = feats.attrs.get("surface")
    value = col[i] if col is not None else None
    if value is None or not str(value).strip():
        value = tags.get("surface")
    return str(value or "").strip().lower() in UNPAVED


def _buffer(lat, lon, width, butt=False):
    """A line (lat, lon (n,)) buffered to width as [(lat, lon)] pads: a mitred outline per run of bends gentler than
    SHARP_TURN_DEG, an octagon (circumscribed on the half width) at each end and sharp bend; butt: flat ends (a runway's
    pavement stops square at the line's end, no octagon beyond it; the sharp bends keep theirs). Metric frame:
    equirectangular about the first point (an airfield is a few km: millimetres)."""
    lat0, lon0 = float(lat[0]), float(lon[0])
    kx = M_PER_DEG * dmath.cos(math.radians(lat0))
    x = (np.asarray(lon, np.float64) - lon0) * kx
    y = (np.asarray(lat, np.float64) - lat0) * M_PER_DEG
    keep = np.r_[True, dnp.hypot(np.diff(x), np.diff(y)) > 1e-3]       # (a repeated vertex has no direction)
    x, y = x[keep], y[keep]
    if len(x) < 2:
        return []
    half = width / 2.0
    d = np.c_[np.diff(x), np.diff(y)]
    nrm = np.c_[-d[:, 1], d[:, 0]] / dnp.hypot(d[:, 0], d[:, 1])[:, None]      # each segment's left normal
    # one outline per run of gentle bends (left side out, right side back, mitred at each vertex); a run breaks at a
    # bend sharper than SHARP_TURN_DEG (its mitre would spike), where an octagon holds the join, as at both ends
    seg = dnp.hypot(d[:, 0], d[:, 1])
    cos_t = np.clip(dnp.vecdot(nrm[:-1], nrm[1:]), -1.0, 1.0)      # each interior vertex's bend
    # how far the inner edge's mitre falls back along the segments at a vertex: half * tan(bend / 2)
    back = np.r_[0.0, half * np.sqrt((1.0 - cos_t) / (1.0 + cos_t + 1e-12)), 0.0]
    sharp = np.r_[False, cos_t < dmath.cos(math.radians(SHARP_TURN_DEG)), False]
    # a vertex also breaks the outline where the inner edge would fold over itself: its fall-back and the next
    # vertex's together longer than the segment between them (a tight bend drawn in short segments); a self-crossing
    # outline is filled even-odd, leaving holes (dirt triangles at Brisbane's runway joins)
    fold = back[:-1] + back[1:] > seg                   # per segment: it is drawn on its own, both its ends break
    brk = sharp | np.r_[fold, False] | np.r_[False, fold]
    rings, start = [], 0
    for k in range(1, len(x)):
        if k == len(x) - 1 or brk[k]:
            off = [nrm[start]]
            for j in range(start + 1, k):
                b = nrm[j - 1] + nrm[j]
                b = b / dnp.hypot(*b)
                off.append(b / float(dnp.dot(b, nrm[j])))       # the mitre: half / cos(the half bend) along the bisector
            off.append(nrm[k - 1])
            off = np.asarray(off) * half
            p = np.c_[x[start:k + 1], y[start:k + 1]]
            rings.append(np.r_[p + off, (p - off)[::-1]])
            start = k
    a = (np.arange(JOIN_SIDES) + 0.5) * 2.0 * math.pi / JOIN_SIDES
    ring = np.c_[dnp.cos(a), dnp.sin(a)] * (half / dmath.cos(math.pi / JOIN_SIDES))
    ends = ([] if butt else [0, len(x) - 1]) + [j for j in range(1, len(x) - 1) if brk[j]]
    rings += [ring + (x[j], y[j]) for j in ends]
    return [(lat0 + r[:, 1] / M_PER_DEG, lon0 + r[:, 0] / kx) for r in rings]


def _hull(pts):
    """The convex hull (counter-clockwise, (k, 2)) of points (n, 2): Andrew's monotone chain."""
    pts = np.unique(np.round(pts, 3), axis=0)
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(p)
        return h[:-1]
    return np.asarray(half(pts) + half(pts[::-1]))


def _head(xy, length):
    """The first `length` m of a polyline xy (n, 2) (its last point cut there), and its segments' left normals."""
    seg = dnp.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    k = int(np.searchsorted(cum, length))
    if k >= len(xy):
        out = xy
    else:
        f = (length - cum[k - 1]) / max(seg[k - 1], 1e-9)
        out = np.r_[xy[:k], [xy[k - 1] + f * (xy[k] - xy[k - 1])]]
    d = np.diff(out, axis=0)
    n = np.c_[-d[:, 1], d[:, 0]] / np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    return out, n


def _seg_dist(p, segs):
    """The distance from point p (2,) to each segment (m, 2, 2)."""
    a, b = segs[:, 0], segs[:, 1]
    ab = b - a
    t = np.clip(dnp.vecdot(p - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
    return dnp.hypot(*(a + t[:, None] * ab - p).T)


def _straight(line, max_deg=STRAIGHT_DEG):
    """How far a polyline (n, 2) from its first point runs on within max_deg of its first direction."""
    d = np.diff(line, axis=0)
    seg = dnp.hypot(d[:, 0], d[:, 1])
    ok = seg > 1e-6
    if not ok.any():
        return 0.0
    d, seg = d[ok], seg[ok]
    u = d / seg[:, None]
    bent = np.flatnonzero(dnp.matmul(u, u[0]) < dmath.cos(math.radians(max_deg)))
    return float(seg[:bent[0]].sum() if len(bent) else seg.sum())


def _from(line, p, direction):
    """The polyline (n, 2) from the point of `line` nearest p (projected on its nearest segment), on in `direction`."""
    a, b = line[:-1], line[1:]
    ab = b - a
    tt = np.clip(dnp.vecdot(p - a, ab) / np.maximum(dnp.vecdot(ab, ab), 1e-12), 0.0, 1.0)
    q = a + tt[:, None] * ab
    k = int(np.argmin(dnp.hypot(*(q - p).T)))
    fwd, back = np.r_[[q[k]], line[k + 1:]], np.r_[[q[k]], line[k::-1]]
    return fwd if float(dnp.matmul(ab[k], direction)) >= 0 else back


def _fillet(v, head, half, r, t, joined=None):
    """The fillets (local metres) where a way leaves the pavement it joins at v: head (n, 2) its line from v on, half
    its half width, r the joined pavement's half width, t (2,) the joined line's unit direction. [] when it leaves
    along that line (no corner). One fillet each side: in the corner between the way's edge and the joined
    pavement's edge, the area between the corner and an arc tangent to both edges (radius FILLET_R_K of the way's
    half widths, the tangent run capped at FILLET_MAX_K of them: a shallow rapid exit's acute corner gets a long,
    tighter arc, an obtuse one a short one). A convex hull's straight edge squared the grass islands off (Brisbane).
    The tangent points stay on the straight part of each edge (_straight of the way's head and of the joined line,
    `joined` (n, 2), from v): past a bend or a short way's end an arc spiked into the grass."""
    d = head[1] - head[0]
    u = d / max(float(dnp.hypot(*d)), 1e-9)
    if abs(float(u[0] * t[1] - u[1] * t[0])) < dmath.sin(math.radians(FILLET_MIN_DEG)):
        return []
    straight_u = _straight(head)
    n_u = np.array([-u[1], u[0]])
    n_t = np.array([-t[1], t[0]])
    m = n_t if float(dnp.matmul(n_t, u)) > 0 else -n_t                  # the joined edge facing the way
    out = []
    for sg in (1.0, -1.0):
        tp = t if float(u[0] * t[1] - u[1] * t[0]) * sg > 0 else -t     # the joined line's way on this side
        p1, p2 = v + sg * half * n_u, v + r * m
        den = u[0] * tp[1] - u[1] * tp[0]
        if abs(den) < 1e-9:
            continue
        w = p2 - p1
        c = p1 + u * ((w[0] * tp[1] - w[1] * tp[0]) / den)  # the corner: the two edges' crossing
        h = dmath.acos(max(-1.0, min(1.0, float(dnp.matmul(u, tp))))) / 2.0          # half the corner's angle
        if h < 1e-3 or h > math.pi / 2 - 1e-3:
            continue
        rf = FILLET_R_K * half
        run = rf / dmath.tan(h)
        # the room on each edge: its straight length from v, less how far the corner already sits along it
        room_u = straight_u - float(dnp.matmul(c - v, u))
        room_t = (_straight(_from(joined, v, tp)) if joined is not None else FILLET_MAX_K * half) - float(dnp.matmul(c - v, tp))
        cap = min(FILLET_MAX_K * half, room_u, room_t)
        if cap < FILLET_MIN_RUN_M:
            continue
        if run > cap:
            run = cap
            rf = run * dmath.tan(h)
        t1, t2 = c + run * u, c + run * tp
        bis = (u + tp) / max(float(dnp.hypot(*(u + tp))), 1e-9)
        o = c + bis * (rf / dmath.sin(h))
        a1 = dmath.atan2(t1[1] - o[1], t1[0] - o[0])
        a2 = dmath.atan2(t2[1] - o[1], t2[0] - o[0])
        da = (a2 - a1 + math.pi) % (2.0 * math.pi) - math.pi          # the short arc, facing the corner
        ang = a1 + da * np.linspace(0.0, 1.0, FILLET_ARC_N)
        arc = o + rf * np.c_[dnp.cos(ang), dnp.sin(ang)]
        out.append(np.r_[[c], arc])
    return out


def _fillets(ways, runways):
    """_fillets_py (its docstring the spec), in the native module where it has it (earthfast's aero_fillets, aerofillets.rs:
    the same rings to the bit; a quarter of an airport's cold cost was here, the user, 2026-11-17)."""
    import fast
    native = getattr(fast._ef, "aero_fillets", None) if fast._ef is not None else None
    if native is None:
        return _fillets_py(ways, runways)
    if not ways:
        return []

    def flat(lines):
        lines = [(np.asarray(xy, np.float64).reshape(-1, 2), h) for xy, h in lines if len(xy) >= 2]
        pts = np.ascontiguousarray(np.concatenate([xy for xy, _h in lines]) if lines else np.zeros((0, 2)))
        starts = np.r_[0, np.cumsum([len(xy) for xy, _h in lines])].astype(np.int64)
        return pts, np.ascontiguousarray(starts), np.ascontiguousarray(np.array([h for _xy, h in lines], np.float64))
    if any(len(xy) < 2 for xy, _h in list(ways) + list(runways)):
        return _fillets_py(ways, runways)          # (a one-point line: the Python's own handling)
    pts, starts = native(*flat(ways), *flat(runways), [JOIN_TOL_M, FILLET_MIN_DEG, FILLET_R_K, FILLET_MAX_K,
                                                       float(FILLET_ARC_N), FILLET_MIN_RUN_M, STRAIGHT_DEG])
    pts = np.asarray(pts)
    return [pts[starts[k]:starts[k + 1]].copy() for k in range(len(starts) - 1)]


def _fillets_py(ways, runways):
    """The fillets (local metres) where the ways meet other pavement: a way's end on another way's or a runway's
    centreline (JOIN_TOL_M), and a way's vertex anywhere along it on a runway's (a taxiway mapped across the runway
    in one piece: the wedge between a shallow crossing and the runway edge, Brisbane's dirt triangles). One fillet for
    each direction the way leaves in (_fillet). ways, runways: [(xy (n, 2), half width)]."""
    others = ways + runways
    if not ways:
        return []
    segs = np.concatenate([np.stack([xy[:-1], xy[1:]], axis=1) for xy, _ in others])
    seg_half = np.concatenate([np.full(len(xy) - 1, h) for xy, h in others])
    seg_owner = np.concatenate([np.full(len(xy) - 1, i) for i, (xy, _) in enumerate(others)])
    on_runway = seg_owner >= len(ways)
    out = []
    for wi, (xy, half) in enumerate(ways):
        idx = range(len(xy)) if on_runway.any() else (0, len(xy) - 1)
        for k in idx:
            end = k in (0, len(xy) - 1)
            dist = _seg_dist(xy[k], segs)
            dist[seg_owner == wi] = np.inf          # (not the way itself)
            if not end:
                dist[~on_runway] = np.inf           # (inside a way: only where it meets a runway)
            j = int(np.argmin(dist))
            if not dist[j] <= JOIN_TOL_M:
                continue
            r = float(seg_half[j])
            if r < half:                            # (a wider way ending on a narrower one: its own end is enough)
                continue
            t = segs[j, 1] - segs[j, 0]
            t = t / max(float(dnp.hypot(*t)), 1e-9)
            for head in (xy[k:], xy[k::-1]):
                if len(head) >= 2:
                    out += _fillet(xy[k], head, half, r, t, others[int(seg_owner[j])][0])
    return out


def snapped(feats):
    """(lines, ways) of the Features' runway, stopway and taxiway lines moved onto the OurAirports runways (runwaysnap.snap), keyed by
    (row, the line's index in the row): ({key: (lonlat or None, width)}, {key: lonlat}); empty without a provider (runwaysnap.set_provider).
    Kept on the Features, per provider."""
    got = feats.__dict__.get(_SNAP)
    if got is not None and got[0] == runwaysnap.token():
        return got[1]
    out = ({}, {})
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is not None and cls is not None and len(feats):
        air = np.asarray(sub).astype(str) == SUBTYPE
        cls = np.asarray(cls).astype(str)
        items = {"runway": [], "stopway": [], "way": []}
        for i in np.flatnonzero(air & np.isin(cls, (RUNWAY, STOPWAY) + WAYS) & (np.asarray(feats.kind) == 2)):
            name = "way" if cls[i] in WAYS else str(cls[i])
            k = 0
            for part in feats.parts(i):
                for ln in part:
                    if len(ln) >= 2:
                        items[name].append(((int(i), k), np.asarray(ln, np.float64)))
                        k += 1
        pts = [ln for v in items.values() for _k, ln in v]
        if items["runway"] and pts:
            allp = np.concatenate(pts)
            rws = runwaysnap.runways_in(float(allp[:, 1].min()), float(allp[:, 0].min()), float(allp[:, 1].max()), float(allp[:, 0].max()))
            if rws:
                out = runwaysnap.snap(items, rws)
    feats.__dict__[_SNAP] = (runwaysnap.token(), out)
    return out


def _feats_pads(feats, runways_too=True, only=None):
    """The pads of one Features (see pads). only: "aprons" the apron rings alone, "ways" the taxiways, taxilanes and
    their fillets alone (surface.photo_lots tones the ways inside an apron as the apron)."""
    sub = feats.attrs.get("subtype")
    cls = feats.attrs.get("class")
    if sub is None or cls is None:
        return []
    air = np.asarray(sub).astype(str) == SUBTYPE
    if not air.any():
        return []
    cls = np.asarray(cls).astype(str)
    want = air & np.isin(cls, (APRON,) + WAYS)
    if not want.any():
        return []
    intl = bool((air & (cls == AERODROME)).any())
    out = []
    snap = snapped(feats)
    ways, runways = [], []                  # (lon, lat (n, 2), half width): the fillets'
    for i in np.flatnonzero(air & np.isin(cls, (APRON, RUNWAY, STOPWAY) + WAYS)):
        if (only == "aprons" and cls[i] != APRON) or (only == "ways" and cls[i] not in WAYS + (RUNWAY, STOPWAY)) \
                or (only == "runways" and cls[i] not in (RUNWAY, STOPWAY)):
            continue
        tags = _tags(feats, i)
        kind = int(feats.kind[i])
        if cls[i] in (RUNWAY, STOPWAY):
            # OurAirports paints the runway between its two ends (airportgrade.runway_pads); the mapped line also
            # runs on over the pavement past them (a stopway, a blast pad, the turn pad a taxiway joins at the very
            # end: Brisbane's taxiway stopped ~100 m short of the runway's end), so it is paved too, at its width,
            # its ends square (an octagon's cap stuck out past the runway's end into the grass: Heathrow 27R)
            if kind == 2 and not _unpaved(feats, i, tags):
                w = width_tag(tags.get("width")) or (RUNWAY_INTL_M if intl else RUNWAY_M)
                lines = []                  # (the line, its width: laid on the OurAirports runway it is, at that one's width: runwaysnap)
                for k, ln in enumerate(ln for part in feats.parts(i) for ln in part if len(ln) >= 2):
                    moved = snap[0].get((int(i), k))
                    if moved is None:
                        lines.append((np.asarray(ln, np.float64), w))
                    elif moved[0] is not None:
                        lines.append((moved[0], moved[1]))
                runways += [(ln, wd / 2.0) for ln, wd in lines]
                for ln, wd in lines if runways_too and only in (None, "runways") else ():
                    out += _buffer(ln[:, 1], ln[:, 0], wd, butt=True)
            continue
        if _unpaved(feats, i, tags):
            continue
        if cls[i] == APRON:
            if kind != 3:
                continue
            for part in feats.parts(i):
                if part and len(part[0]) >= 3:
                    ring = np.asarray(part[0], np.float64)
                    out.append((ring[:, 1], ring[:, 0]))
        elif kind == 2:
            width = width_tag(tags.get("width"))
            if width is None:
                if cls[i] == "taxiway":
                    width = TAXIWAY_INTL_M if intl else TAXIWAY_M
                else:
                    width = TAXILANE_INTL_M if intl else TAXILANE_M
            k = 0
            for part in feats.parts(i):
                for line in part:
                    if len(line) >= 2:
                        line = snap[1].get((int(i), k), np.asarray(line, np.float64))      # (its end on a runway, moved with it)
                        k += 1
                        out += _buffer(line[:, 1], line[:, 0], width)
                        ways.append((line, width / 2.0))
    if ways:                                # the fillets, in one metric frame for the cell
        lon0, lat0 = ways[0][0][0]
        kx = M_PER_DEG * dmath.cos(math.radians(lat0))

        def to_m(ln):
            return np.c_[(ln[:, 0] - lon0) * kx, (ln[:, 1] - lat0) * M_PER_DEG]
        mw = [(to_m(ln), h) for ln, h in ways]
        for r in _fillets(mw, [(to_m(ln), h) for ln, h in runways]):
            out.append((lat0 + r[:, 1] / M_PER_DEG, lon0 + r[:, 0] / kx))
    return out


def _kept(feats_list, key, build):
    key += str(runwaysnap.token())              # (the pads are per provider of the OurAirports runways the mapped lines are moved onto)
    out = []
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        got = feats.__dict__.get(key)
        if got is None:
            got = feats.__dict__[key] = build(feats)
        out += got
    return out


def _is_aerodrome(c):
    """Is an infrastructure class (subtype "airport") an aerodrome (an airport polygon) rather than a heliport, a seaplane
    base, a closed field or a way / apron / building?"""
    c = str(c)
    if c == AERODROME:
        return True
    if "heliport" in c or "seaplane" in c or "closed" in c:
        return False
    return c.endswith(("airport", "airfield", "airstrip", "aerodrome"))


def _grown_hull(rings, grow):
    """The convex hull of rings [(lat, lon)] grown by `grow` m, as (lat, lon): the polygon through the points' extreme ones in
    HULL_DIRS directions (a few dozen points, not the pads' tens of thousands), each swelled by an octagon (equirectangular
    about the first point). None for no points."""
    rings = [(np.asarray(la, np.float64), np.asarray(lo, np.float64)) for la, lo in rings if len(la)]
    if not rings:
        return None
    lat0, lon0 = float(rings[0][0][0]), float(rings[0][1][0])
    kx = M_PER_DEG * dmath.cos(math.radians(lat0))
    xy = np.concatenate([np.c_[(lo - lon0) * kx, (la - lat0) * M_PER_DEG] for la, lo in rings])
    a = np.arange(HULL_DIRS) * (2.0 * math.pi / HULL_DIRS)
    cs, sn = dnp.cos(a), dnp.sin(a)
    ext = xy[np.argmax(xy[:, :1] * cs[None, :] + xy[:, 1:] * sn[None, :], axis=0)]
    o = (np.arange(JOIN_SIDES) + 0.5) * 2.0 * math.pi / JOIN_SIDES
    swell = np.c_[dnp.cos(o), dnp.sin(o)] * (grow / dmath.cos(math.pi / JOIN_SIDES))
    h = _hull((ext[:, None, :] + swell[None, :, :]).reshape(-1, 2))
    if len(h) < 3:
        return None
    return lat0 + h[:, 1] / M_PER_DEG, lon0 + h[:, 0] / kx


def _feats_boundary(feats):
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is None or cls is None:
        return []
    air = np.asarray(sub).astype(str) == SUBTYPE
    if not air.any():
        return []
    cls = np.asarray(cls).astype(str)
    out = []
    for i in np.flatnonzero(air):
        if int(feats.kind[i]) == 3 and _is_aerodrome(cls[i]):
            for part in feats.parts(int(i)):
                if part and len(part[0]) >= 3:
                    ring = np.asarray(part[0], np.float64)
                    out.append((ring[:, 1], ring[:, 0]))
    if out:
        return out
    pads_ = _kept([feats], _KEY, lambda f: _feats_pads(f))      # (the pads, kept as pads() keeps them)
    hull = _grown_hull(pads_, INFIELD_GROW_M) if pads_ else None
    return [] if hull is None else [hull]


def boundaries(feats_list):
    """[(lat (k,), lon (k,))]: the airports' boundaries of the Features in feats_list (see the module docstring); [] for
    Features with no airport in them."""
    return _kept(feats_list, _KEY + "_boundary", _feats_boundary)


def _feats_gates(feats):
    sub, cls = feats.attrs.get("subtype"), feats.attrs.get("class")
    if sub is None or cls is None:
        return []
    air = np.asarray(sub).astype(str) == SUBTYPE
    cls = np.asarray(cls).astype(str)
    gate = air & np.isin(cls, GATE_CLASSES)
    tags = feats.attrs.get("tags")
    if tags is not None:
        t = np.asarray(tags).astype(str)
        gate |= air & ((np.char.find(t, '"gate"') >= 0) | (np.char.find(t, "parking_position") >= 0))
    out = []
    for i in np.flatnonzero(gate & (np.asarray(feats.kind) == 1)):
        parts = feats.parts(int(i))
        if parts and parts[0] and len(parts[0][0]):
            pt = np.asarray(parts[0][0], np.float64)[0]
            out.append((float(pt[1]), float(pt[0])))
    return out


def gates(feats_list):
    """[(lat, lon)]: the gate and parking-position points of the Features in feats_list (the aircraft stands)."""
    return _kept(feats_list, _KEY + "_gates", _feats_gates)


def aprons(feats_list):
    """[(lat (k,), lon (k,))]: the aprons' outer rings of the Features in feats_list alone (pads without the ways)."""
    return _kept(feats_list, _KEY + "_aprons", lambda f: _feats_pads(f, False, "aprons"))


def runway_pads(feats_list):
    """[(lat (k,), lon (k,))]: the mapped runway and stopway lines' paving alone (pads without the aprons and the taxiways)."""
    return _kept(feats_list, _KEY + "_runways", lambda f: _feats_pads(f, True, "runways"))


def ways(feats_list):
    """[(lat (k,), lon (k,))]: the taxiway and taxilane outlines and their fillets alone (pads without the aprons and
    the runways' own strips)."""
    return _kept(feats_list, _KEY + "_taxi", lambda f: _feats_pads(f, False, "ways"))


def stamp(feats):
    """What says two reads of a cell's Features are one: the map data's own mark (cell, layer, versions: a cell the LRU dropped and
    read again is the same data), else the object itself (the caller keeps it alive); None for None. What the airport modules'
    answers that do not depend on the tile (airportscene, windsocks, airfieldlights) are kept by."""
    return None if feats is None else feats.__dict__.get("_bnge_cell") or ("obj", id(feats))


def pads(feats_list, runways=True):
    """[(lat (k,), lon (k,))] of the aprons and taxiways of the Features in feats_list (see the module docstring),
    [] for Features with no airport in them (one vectorised test of the class column, kept on the Features). runways
    False: without the mapped runway and stopway lines' paving (the aprons, taxiways and their fillets alone)."""
    out = []
    key = (_KEY if runways else _KEY + "_ways") + str(runwaysnap.token())
    for feats in feats_list:
        if feats is None or len(feats) == 0:
            continue
        got = feats.__dict__.get(key)
        if got is None:
            got = feats.__dict__[key] = _feats_pads(feats, runways)
        out += got
    return out
