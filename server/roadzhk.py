"""Hong Kong's road heights for roadz's "hk" source (GitHub #1078, Path A): the Transport Department's Road Network
centrelines (signed level tier, route number, street name) with heights read from the CEDD 2020 airborne LiDAR, ground
roads from the DTM and decks from the DSM across the carriageway. Pure given the fetched inputs (RoadZ._hk fetches
them).

The centrelines come twice from the same service, in lon/lat (EPSG:4326, for the map) and in HK1980 Grid metres
(EPSG:2326, the LiDAR's grid): the server projects them, so no projection is written here."""
from typing import NamedTuple

import numpy as np

STEP_M = 4.0                # m between a line's stations (roadz.LIDAR_STEP_M)
DECK_HALF_M = 3.0           # m either side of a deck's centreline its DSM is read
DECK_READS = (-1.0, -0.5, 0.0, 0.5, 1.0)    # (times DECK_HALF_M: the reads across)
DECK_ALONG = (-2.0, 0.0, 2.0)   # m along the line from the station the reads across are taken at
# the reads' percentile: the deck's surface under vehicles, lamps and parapets, and over the gaps a speckled DSM has in
# a viaduct (the 25th of 5 reads across took the ground through them: Tsing Sha Highway at 22.3777 N 114.1815 E). Over
# the Sha Tin sites' TD decks, the median of the 15 against that: stations within 1 m of their 7-station running median
# 82 -> 93 %, crossings with the higher tier 4.5 m or more over the lower 96 -> 99 % (138), bridge parts reaching
# MIN_COVER 57 -> 67 % (82), 2026-11-10
DECK_PCT = 50.0
DECK_MIN_READS = 3
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
        seg = np.hypot(d[:, 0], d[:, 1])
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
        dist = np.hypot(*(P[:, None] - (A[None] + t[..., None] * ab[None])).transpose(2, 0, 1))
        out[idx] = (dist <= reach).any(1)
    return out


def _low(v):
    """(n,) the DECK_PCT percentile of each row of v (n, k) over its finite values; NaN with under DECK_MIN_READS."""
    v = np.sort(v, axis=1)
    cnt = np.isfinite(v).sum(1)
    out = np.full(len(v), np.nan)
    ok = np.flatnonzero(cnt >= DECK_MIN_READS)
    pos = (cnt[ok] - 1) * DECK_PCT / 100.0
    a = np.floor(pos).astype(np.int64)
    b = np.minimum(a + 1, cnt[ok] - 1)
    out[ok] = v[ok, a] + (v[ok, b] - v[ok, a]) * (pos - a)
    return out


def rows(lines, runs, dtm, dsm):
    """roadz Lines.build rows (lonlat, z, level, ref, name) of the runs (stations): ground from dtm(x, y), a deck the
    DECK_PCT percentile of dsm(x, y) across the carriageway at DECK_ALONG; NaN in a tunnel, where the LiDAR has none, and on a deck
    within OCCLUDE_M of a higher tier's line (any of lines: its DSM is that deck's). dtm / dsm are called once each, and
    only with points to read."""
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
        u = np.vstack([r[3] for r in decks])
        s = np.tile(np.asarray(DECK_READS) * DECK_HALF_M, len(DECK_ALONG))[None]
        a = np.repeat(np.asarray(DECK_ALONG, np.float64), len(DECK_READS))[None]
        px = xy[:, None, 0] + u[:, None, 0] * a - u[:, None, 1] * s
        py = xy[:, None, 1] + u[:, None, 1] * a + u[:, None, 0] * s
        got = _low(np.asarray(dsm(px.ravel(), py.ravel()), np.float64).reshape(len(xy), s.shape[1]))
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
