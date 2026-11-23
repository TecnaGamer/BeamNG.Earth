"""Hong Kong's road heights for roadz's "hk" source (GitHub #1078, Path A): the Transport Department's Road Network
centrelines (signed level tier, route number, street name) with heights read from the CEDD 2020 airborne LiDAR, ground
roads from the DTM and decks from the DSM pixels round each deck line (the US source's deck_lidar picks the one surface
that runs along it). Pure given the fetched inputs (RoadZ._hk fetches them).

The centrelines come in lon/lat (EPSG:4326, for the map); their HK1980 Grid metres (EPSG:2326, the LiDAR's grid) are
hkdtm.grid's. (The service's own grid copy of the same lines agreed with it to 0.2 mm on 32,219 vertices, 2026-11-15;
asked for with fewer vertices, its two copies do not keep the same ones.)"""
from typing import NamedTuple

import numpy as np

import fast
import dnp
import hkdtm

STEP_M = 4.0                # m between a line's stations, deck_lidar's too (roadz.LIDAR_STEP_M)
PIX_M = 0.5                 # m, the DSM's pixel
VERTEX_MIN_M = PIX_M        # m along a line between the vertices kept of it (_spaced): no closer than the pixels read
DECK_HALF_M = 4.0           # m either side of a deck's line its DSM pixels count
CORRIDOR_NATIVE = 261       # earthfast.VERSION of pixel_corridor (the PIX_M pixel centres within DECK_HALF_M of a
                            # line, each once)
# earthfast.deck_lidar's settings over those pixels (taken as class 2): roadz's call has the same points, gap and
# smoothing, and a grade of 0.10 (LIDAR_MAX_GRADE). Over the median of 15 reads across a deck, at Tai Wai and Lai Chi
# Kok: bridge parts with measured heights 39 -> 63 % and 64 -> 74 % of their length, 2026-11-11
DECK_MAX_GRADE = 0.14
DECK_MIN_PTS = 3
DECK_GAP_M = 24.0           # m of a deck's stations without a surface that are bridged over
DECK_SIGMA_M = 4.0          # m, the smoothing
# roadz.observed joins a bridge part's own row with the government's deck line into one surface (one_surface). Two
# stations, one of each, further apart in height than OWN_TOL_M and OWN_GRADE of the way between them are not one
# deck. Where the government's line is the part's own (OWN_SAME_SHARE of its matched stations within OWN_SAME_M of
# the part's line: both read the same DSM pixels) it stays as it is, and the own stations it contradicts are
# dropped: deck_lidar keeps to the surface it started on, and an own row that began at a hole in the survey kept to
# the shore under a 20 m viaduct for 300 m, Hong Kong (22.28746 N 114.18924 E). Otherwise the government's line is
# another carriageway's or road's and its stations give way (a line 15 to 6 m off, 25 m over the part it crosses:
# (22.37151 N 114.07364 E)). The own stations left fill the stations the government's line has none for, held
# within OWN_GRADE of its heights: they meet in a ramp, not in a step the planner drops the part for
# (roads.OBS_MAX_GRADE). Lab 2026-11-16, 496 bridge parts of 49 places against the government's lines alone: no
# station of theirs moved, 10.7 km more measured, 0.2 km of it over 1.5 m off the DSM.
OWN_TOL_M = 1.5
OWN_GRADE = 0.12
OWN_SAME_M = DECK_HALF_M
OWN_SAME_SHARE = 0.5
# a part's DSM pixels within this of the edge of a deck that may stand over it (stacked) are that deck's, not the
# part's (hidden_pixels): a part starting under a 14 m wide viaduct read its edge beyond OCCLUDE_M of its line, 31 m
# for a road at 19.5 m, and the three parts meeting there were drawn up to 15 m over their deck, Hong Kong
# (22.33127 N 114.14363 E)
OCCLUDE_EDGE_M = 1.0
ACROSS_N = 9                # DSM reads across a deck's line at a station, DECK_HALF_M either side (across)
# a deck under a higher tier's line reads that one in the DSM; the ground under a deck keeps its DTM (no decks in it)
OCCLUDE_M = 8.0
GRID_M = 64.0               # m, the cells _near buckets segments by


class TdLine(NamedTuple):
    level: int              # ELEVATION: 0 ground, 1..4 decks, -1 / -2 tunnels
    ref: str
    name: str
    ll: np.ndarray          # (n, 2) lon, lat
    xy: np.ndarray          # (n, 2) HK1980 Grid easting, northing


def _text(v):
    if v is None or str(v).strip() in ("", "-99"):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _spaced(xy, min_m=VERTEX_MIN_M):
    """Indices of the vertices kept of the line xy (n, 2), n >= 2: the first, the last, and each one min_m or more
    along the line past the one kept before it. The service holds some curves in vertices well under a millimetre
    apart (83,150 on a 57 m line at Hong Kong (22.2778 N 114.1634 E), 985,619 in that map cell where the median cell
    has 11,212, 2026-11-15), and roadz.observed goes over every vertex of its cells on each call. roadz asks the
    service to leave them out (HK_LINE_TOL_DEG); this holds whatever it answers."""
    s = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(xy, axis=0).T))]
    if (np.diff(s) >= min_m).all():
        return np.arange(len(s))
    keep = [0]
    while True:
        i = int(np.searchsorted(s, s[keep[-1]] + min_m))
        if i >= len(s) - 1:
            break
        keep.append(i)
    return np.array(keep + [len(s) - 1])


def parse_td(feats):
    """[TdLine] of TD Road Network features fetched in EPSG:4326, their vertices VERTEX_MIN_M apart or more (_spaced,
    by their grid metres). A feature without usable paths, or a path with a bad coordinate, is left out."""
    out = []
    for f in feats or ():
        if not isinstance(f, dict):
            continue
        a = f.get("attributes") or {}
        try:
            level = int(a.get("ELEVATION") or 0)
            paths = [np.asarray(p, np.float64)[:, :2] for p in (f.get("geometry") or {}).get("paths")]
        except (TypeError, ValueError, IndexError):
            continue
        for p in paths:
            if len(p) >= 2 and np.isfinite(p).all() and (np.abs(p) <= [180.0, 90.0]).all():
                q = np.stack(hkdtm.grid(p[:, 1], p[:, 0]), 1)
                k = _spaced(q)
                out.append(TdLine(level, _text(a.get("ROUTE_NUM")), _text(a.get("STREET_ENAME")), p[k], q[k]))
    return out


def stations(lines, inside):
    """Each line every ~STEP_M (its vertices kept), cut to where inside(ll (k, 2)) -> bool (k,) holds: [(line index,
    ll (k, 2), xy (k, 2), unit direction (k, 2) in the grid)], runs of 2 stations or more."""
    out = []
    for i, ln in enumerate(lines):
        d = np.diff(ln.xy, axis=0)
        seg = dnp.hypot(d[:, 0], d[:, 1])
        n = np.maximum(1, np.ceil(seg / STEP_M - 1e-9).astype(np.int64))
        j = np.repeat(np.arange(len(seg)), n)
        t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
        xy = np.vstack([ln.xy[j] + d[j] * t[:, None], ln.xy[-1:]])
        ll = np.vstack([ln.ll[j] + (ln.ll[j + 1] - ln.ll[j]) * t[:, None], ln.ll[-1:]])
        u = d / np.maximum(seg, 1e-9)[:, None]
        u = np.vstack([u[j], u[-1:]])
        keep = np.asarray(inside(ll), bool)
        edges = np.flatnonzero(np.diff(np.r_[0, keep.astype(np.int8), 0]))
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a >= 2:
                out.append((i, ll[a:b], xy[a:b], u[a:b]))
    return out


def _near(p, a, b, reach, pair=None):
    """(n,) bool: is each point p (n, 2) within reach of a segment a[j]-b[j] ((m, 2) each)? pair: (owner of each point
    (n,), owner of each segment (m,), allowed (k, k) bool): a segment counts for a point only if its owner is allowed
    for the point's."""
    out = np.zeros(len(p), bool)
    if not len(p) or not len(a):
        return out
    lo = np.floor((np.minimum(a, b) - reach) / GRID_M).astype(np.int64)
    hi = np.floor((np.maximum(a, b) + reach) / GRID_M).astype(np.int64)
    cells = {}
    for j in range(len(a)):
        for gx in range(lo[j, 0], hi[j, 0] + 1):
            for gy in range(lo[j, 1], hi[j, 1] + 1):
                cells.setdefault((gx, gy), []).append(j)
    g = np.floor(p / GRID_M).astype(np.int64)
    keys, inv = np.unique(g, axis=0, return_inverse=True)
    for k, (gx, gy) in enumerate(keys.tolist()):
        js = cells.get((gx, gy))
        if not js:
            continue
        idx = np.flatnonzero(inv.ravel() == k)
        A, B, P = a[js], b[js], p[idx]
        ab = B - A
        t = np.clip(((P[:, None] - A[None]) * ab[None]).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-12)[None], 0.0, 1.0)
        dist = dnp.hypot(*(P[:, None] - (A[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
        hit = dist <= reach
        if pair is not None:
            hit &= pair[2][pair[0][idx][:, None], pair[1][js][None, :]]
        out[idx] = hit.any(1)
    return out


def _joined(a, b, join_m):
    """(len(a), len(b)) bool: is an end of the line a[i] within join_m of an end of the line b[j]?"""
    ea = np.array([[ln[0], ln[-1]] for ln in a], np.float64)
    eb = np.array([[ln[0], ln[-1]] for ln in b], np.float64)
    gap = ea[:, None, :, None, :] - eb[None, :, None, :, :]
    return dnp.hypot(gap[..., 0], gap[..., 1]).min(axis=(2, 3)) <= join_m


def _may_hide(parts, join_m):
    """(k, k) bool: may part j stand over part i (stacked's rule: [i, j])?"""
    lv = np.array([p[2] for p in parts], np.float64)
    joined = _joined([p[0] for p in parts], [p[0] for p in parts], join_m)
    allowed = (lv[None, :] > lv[:, None]) | ((lv[None, :] == lv[:, None]) & ~joined)
    np.fill_diagonal(allowed, False)
    return allowed


def _hider(lines, halves, allowed):
    """hide(k, p): which of the points p (m, 2) lie within halves[j] + OCCLUDE_EDGE_M of lines[j] for a j with
    allowed[k, j]."""
    lines = [np.asarray(ln, np.float64) for ln in lines]
    reach = np.asarray(halves, np.float64) + OCCLUDE_EDGE_M
    lo = np.array([ln.min(0) for ln in lines]) - reach[:, None]
    hi = np.array([ln.max(0) for ln in lines]) + reach[:, None]

    def hide(k, p):
        out = np.zeros(len(p), bool)
        if not len(p):
            return out
        plo, phi = p.min(0), p.max(0)
        for j in np.flatnonzero(allowed[k] & (lo <= phi).all(1) & (hi >= plo).all(1)).tolist():
            out |= _near(p, lines[j][:-1], lines[j][1:], float(reach[j]))
        return out
    return hide


def hidden_pixels(parts, halves, join_m):
    """hide(k, p): which of the points p (m, 2) round part k's line lie on a part that may stand over it (stacked's
    rule), within that part's half width (halves) and OCCLUDE_EDGE_M of its line."""
    if len(parts) < 2:
        return lambda k, p: np.zeros(len(p), bool)
    return _hider([p[0] for p in parts], halves, _may_hide(parts, join_m))


def hidden_by_bridges(lines, bridges, join_m):
    """hide(k, p): which of the points p (m, 2) round ground line k of lines lie on a bridge part of bridges
    [(line xy, half width)] of any level, within its half width and OCCLUDE_EDGE_M of its line, unless an end of it
    is within join_m of an end of line k (the deck it goes on from): a ground line is under or beside every other
    bridge, and what a DSM shows there is that bridge's surface."""
    if not bridges:
        return lambda k, p: np.zeros(len(p), bool)
    blines = [b[0] for b in bridges]
    return _hider(blines, [b[1] for b in bridges], ~_joined(lines, blines, join_m))


def stacked(parts, join_m):
    """[(n,) bool] per part of parts [(line xy (k, 2), stations (n, 2), level)]: is each station within OCCLUDE_M of the
    line of another part that may stand over it (a DSM shows the top surface only)? One of a higher level does; so does
    one of the same level (the map's level is 1 unless it says otherwise) unless an end of it is within join_m of an end
    of this one (the deck going on from this one, or its other carriageway). One of a lower level never does."""
    out = [np.zeros(len(p[1]), bool) for p in parts]
    if len(parts) < 2:
        return out
    allowed = _may_hide(parts, join_m)
    a = np.vstack([p[0][:-1] for p in parts])
    b = np.vstack([p[0][1:] for p in parts])
    owner_seg = np.repeat(np.arange(len(parts)), [len(p[0]) - 1 for p in parts])
    owner = np.repeat(np.arange(len(parts)), [len(p[1]) for p in parts])
    hit = _near(np.vstack([p[1] for p in parts]), a, b, OCCLUDE_M, (owner, owner_seg, allowed))
    return np.split(hit, np.cumsum([len(p[1]) for p in parts])[:-1])


def one_surface(arc, gov, far, own):
    """(gov, own) as one deck: gov (n,) the government's deck heights along arc (n,) with their lines' distances far
    (n,), own (n,) the part's own row's; NaN where either has none. The stations that are not one deck's are dropped
    and own is left only where gov has none, within OWN_GRADE of gov's heights. See OWN_TOL_M."""
    gov, own = np.array(gov, np.float64), np.array(own, np.float64)
    gi, oi = np.flatnonzero(np.isfinite(gov)), np.flatnonzero(np.isfinite(own))
    if len(gi) and len(oi):
        apart = np.abs(own[oi][:, None] - gov[gi][None, :]) > OWN_GRADE * np.abs(arc[oi][:, None]
                                                                                 - arc[gi][None, :]) + OWN_TOL_M
        if float((np.asarray(far, np.float64)[gi] <= OWN_SAME_M).mean()) >= OWN_SAME_SHARE:
            own[oi[apart.any(1)]] = np.nan
        else:
            gov[gi[apart.any(0)]] = np.nan
    own[np.isfinite(gov)] = np.nan
    gi, oi = np.flatnonzero(np.isfinite(gov)), np.flatnonzero(np.isfinite(own))
    if len(gi) and len(oi):
        reach = OWN_GRADE * np.abs(arc[oi][:, None] - arc[gi][None, :])
        lo, hi = (gov[gi][None, :] - reach).max(1), (gov[gi][None, :] + reach).min(1)
        ok = lo <= hi
        own[oi[ok]] = np.clip(own[oi[ok]], lo[ok], hi[ok])
    return gov, own


def across(runs, halves, dsm, hide=None):
    """[(n,) height] per run of runs ((n, 2) stations each): the median of ACROSS_N DSM reads across the line at each
    station, the part's half width (halves; DECK_HALF_M at most) either side: the surface most of the part's width
    lies on. NaN where none is read, or hide(k, points) says all are another deck's. One batched dsm() read."""
    pts = []
    for x, half in zip(runs, halves):
        d = np.gradient(np.asarray(x, np.float64), axis=0)
        d = d / np.maximum(dnp.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
        offs = np.linspace(-1.0, 1.0, ACROSS_N) * min(float(half), DECK_HALF_M)
        pts.append((x[:, None, :] + offs[None, :, None] * np.stack([-d[:, 1], d[:, 0]], 1)[:, None, :]).reshape(-1, 2))
    allp = np.vstack(pts)
    zz = np.asarray(dsm(np.ascontiguousarray(allp[:, 0]), np.ascontiguousarray(allp[:, 1])), np.float64)
    out = []
    for k, (p, z) in enumerate(zip(pts, np.split(zz, np.cumsum([len(p) for p in pts])[:-1]))):
        if hide is not None:
            z = np.where(hide(k, p), np.nan, z)
        z = np.sort(z.reshape(-1, ACROSS_N), axis=1)                # NaN last: the first n are the heights read
        n, at = np.isfinite(z).sum(1), np.arange(len(z))
        out.append(0.5 * (z[at, np.maximum(n - 1, 0) // 2] + z[at, n // 2]))
    return out


def _deck_heights(xy_runs, dsm, hide=None):
    """[(n,) height] per run of xy_runs ((n, 2) each): fast._ef.deck_lidar of the run's line on the DSM pixels in its
    corridor (earthfast.pixel_corridor), one batched dsm() read for all runs; NaN where it found no surface.
    hide(k, pixels (m, 2)) -> (m,) bool: the pixels of run k that are not read (hidden_pixels)."""
    fast.require(CORRIDOR_NATIVE, "Hong Kong deck pixels")
    cors = [np.asarray(fast._ef.pixel_corridor(np.ascontiguousarray(x, np.float64), DECK_HALF_M, PIX_M))
            for x in xy_runs]
    allp = np.vstack(cors)
    zz = np.asarray(dsm(np.ascontiguousarray(allp[:, 0]), np.ascontiguousarray(allp[:, 1])), np.float64)
    out = []
    for k, (x, c, z) in enumerate(zip(xy_runs, cors, np.split(zz, np.cumsum([len(c) for c in cors])[:-1]))):
        res = np.full(len(x), np.nan)
        ok = np.isfinite(z)
        if hide is not None:
            ok &= ~hide(k, c)
        if len(x) >= 2 and ok.any():
            pts = np.ascontiguousarray(np.c_[c[ok], z[ok]])
            s, zl, _cnt = fast._ef.deck_lidar(np.ascontiguousarray(x), pts, np.full(len(pts), 2, np.uint8), DECK_HALF_M,
                                              STEP_M, DECK_MAX_GRADE, DECK_MIN_PTS, DECK_GAP_M, DECK_SIGMA_M)
            f = np.isfinite(zl)
            if f.any():
                arc = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(x, axis=0).T))]
                res = dnp.interp(arc, s[f], zl[f])
                sf = s[f]
                i = np.clip(np.searchsorted(sf, arc), 1, max(len(sf) - 1, 1))
                near = np.minimum(np.abs(arc - sf[np.minimum(i, len(sf) - 1)]), np.abs(arc - sf[i - 1]))
                # (none where no surface station is within 3/4 of a step: not drawn across)
                res[near > 0.75 * (s[1] - s[0])] = np.nan
        out.append(res)
    return out


def rows(lines, runs, dtm, dsm):
    """roadz Lines.build rows (lonlat, z, level, ref, name) of the runs (stations): ground from dtm(x, y), a deck the
    one surface of dsm(x, y) along its line (_deck_heights); NaN in a tunnel, where the LiDAR has none, where a deck has
    no surface, and on a deck within OCCLUDE_M of a higher tier's line (any of lines: its DSM is that deck's). dtm / dsm
    are called once each, and only with points to read."""
    ground = [r for r in runs if lines[r[0]].level == 0]
    decks = [r for r in runs if lines[r[0]].level > 0]
    z = {}
    if ground:
        xy = np.vstack([r[2] for r in ground])
        got = np.asarray(dtm(xy[:, 0], xy[:, 1]), np.float64)
        for r, part in zip(ground, np.split(got, np.cumsum([len(r[2]) for r in ground])[:-1])):
            z[id(r)] = part
    if decks:
        xy = np.vstack([r[2] for r in decks])
        got = np.concatenate(_deck_heights([r[2] for r in decks], dsm))
        lvl = np.concatenate([np.full(len(r[2]), lines[r[0]].level) for r in decks])
        for tier in sorted(set(lvl.tolist())):
            higher = [ln.xy for ln in lines if ln.level > tier]
            if not higher:
                continue
            a = np.vstack([h[:-1] for h in higher])
            b = np.vstack([h[1:] for h in higher])
            at = np.flatnonzero(lvl == tier)
            got[at[_near(xy[at], a, b, OCCLUDE_M)]] = np.nan
        for r, part in zip(decks, np.split(got, np.cumsum([len(r[2]) for r in decks])[:-1])):
            z[id(r)] = part
    out = []
    for r in runs:
        ln = lines[r[0]]
        out.append((r[1], z.get(id(r), np.full(len(r[1]), np.nan)), ln.level, ln.ref, ln.name))
    return out
