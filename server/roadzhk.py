"""Hong Kong's road heights for roadz's "hk" source (GitHub #1078, Path A): the Transport Department's Road Network
centrelines (signed level tier, route number, street name) with heights read from the CEDD 2020 airborne LiDAR, ground
roads from the DTM and decks from the DSM pixels round each deck line (the US source's deck_lidar picks the one surface
that runs along it). Pure given the fetched inputs (RoadZ._hk fetches them).

The centrelines come twice from the same service, in lon/lat (EPSG:4326, for the map) and in HK1980 Grid metres
(EPSG:2326, the LiDAR's grid): the server projects them, so no projection is written here."""
from typing import NamedTuple

import numpy as np

import fast
import dnp

STEP_M = 4.0                # m between a line's stations, deck_lidar's too (roadz.LIDAR_STEP_M)
PIX_M = 0.5                 # m, the DSM's pixel
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


def parse_td(feats_ll, feats_xy):
    """[TdLine] of TD Road Network features fetched in EPSG:4326 (feats_ll) and EPSG:2326 (feats_xy), paired by
    OBJECTID. A feature whose two copies differ in shape, or with a bad coordinate, is left out."""
    xy_of = {}
    for f in feats_xy or ():
        if isinstance(f, dict):
            xy_of[(f.get("attributes") or {}).get("OBJECTID")] = (f.get("geometry") or {}).get("paths")
    out = []
    for f in feats_ll or ():
        if not isinstance(f, dict):
            continue
        a = f.get("attributes") or {}
        try:
            level = int(a.get("ELEVATION") or 0)
            pl = [np.asarray(p, np.float64)[:, :2] for p in (f.get("geometry") or {}).get("paths")]
            px = [np.asarray(p, np.float64)[:, :2] for p in xy_of.get(a.get("OBJECTID"))]
        except (TypeError, ValueError, IndexError):
            continue
        if len(pl) != len(px) or any(p.shape != q.shape for p, q in zip(pl, px)):
            continue
        for p, q in zip(pl, px):
            if (len(p) >= 2 and np.isfinite(p).all() and np.isfinite(q).all()
                    and (np.abs(p) <= [180.0, 90.0]).all()):
                out.append(TdLine(level, _text(a.get("ROUTE_NUM")), _text(a.get("STREET_ENAME")), p, q))
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


def _near(p, a, b, reach):
    """(n,) bool: is each point p (n, 2) within reach of a segment a[j]-b[j] ((m, 2) each)?"""
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
        out[idx] = (dist <= reach).any(1)
    return out


def _deck_heights(xy_runs, dsm):
    """[(n,) height] per run of xy_runs ((n, 2) each): fast._ef.deck_lidar of the run's line on the DSM pixels in its
    corridor (earthfast.pixel_corridor), one batched dsm() read for all runs; NaN where it found no surface."""
    fast.require(CORRIDOR_NATIVE, "Hong Kong deck pixels")
    cors = [np.asarray(fast._ef.pixel_corridor(np.ascontiguousarray(x, np.float64), DECK_HALF_M, PIX_M))
            for x in xy_runs]
    allp = np.vstack(cors)
    zz = np.asarray(dsm(np.ascontiguousarray(allp[:, 0]), np.ascontiguousarray(allp[:, 1])), np.float64)
    out = []
    for x, c, z in zip(xy_runs, cors, np.split(zz, np.cumsum([len(c) for c in cors])[:-1])):
        res = np.full(len(x), np.nan)
        ok = np.isfinite(z)
        if len(x) >= 2 and ok.any():
            pts = np.ascontiguousarray(np.c_[c[ok], z[ok]])
            s, zl, _cnt = fast._ef.deck_lidar(np.ascontiguousarray(x), pts, np.full(len(pts), 2, np.uint8), DECK_HALF_M,
                                              STEP_M, DECK_MAX_GRADE, DECK_MIN_PTS, DECK_GAP_M, DECK_SIGMA_M)
            f = np.isfinite(zl)
            if f.any():
                arc = np.r_[0.0, np.cumsum(dnp.hypot(*np.diff(x, axis=0).T))]
                res = np.interp(arc, s[f], zl[f])
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
