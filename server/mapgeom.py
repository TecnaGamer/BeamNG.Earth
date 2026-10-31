"""Compact columnar feature tables for the map-data pipeline (mapdata.py): ragged geometry in numpy
arrays, per-feature attribute columns, linear-referenced rules, WKB decoding and polygon clipping.

Geometry layout (as GeoArrow, one level per nesting): feature i has parts
part_off[i]..part_off[i+1]; part p has rings ring_off[p]..ring_off[p+1]; ring r has coordinates
coord_off[r]..coord_off[r+1] in `lonlat` (float64 degrees, lon first) and `en` (float64 metres in
the cell's local ENU frame, east first). kind[i] is POINT, LINE or POLYGON; a multi-geometry is
several parts; a line part has one ring (its vertices); a polygon part has its exterior ring then
its holes (closed: the last vertex repeats the first); a point part is one ring of one vertex.

Attributes are numpy columns, one value per feature: text as fixed-width unicode ("" = missing),
numbers as float32 or float64 (NaN = missing), flags as bool. Rules (Overture's linear referencing,
e.g. a road's surface along part of its length) are RuleTables: rows (feature, value, start, end)
with start/end the fraction of the feature's length (0..1; a rule without `between` is 0..1).

Everything here is a pure function of its input: no dict-order, set-order or float-summation
order dependence (features come sorted by id from the reader).
"""
import math
import struct
from dataclasses import dataclass, field, replace

import numpy as np

POINT, LINE, POLYGON = 1, 2, 3


_NO_ROWS = np.zeros(0, np.int64)


@dataclass
class RuleTable:
    feature: np.ndarray                 # int32 feature index
    value: np.ndarray                   # unicode or float
    start: np.ndarray                   # float32 0..1
    end: np.ndarray                     # float32 0..1

    _rows: dict = field(default=None, repr=False, compare=False)    # rows(): feature -> its row indices
    _major: np.ndarray = field(default=None, repr=False, compare=False)     # major(): feature -> a row or -1

    def __len__(self):
        return len(self.feature)

    def rows(self, i):
        """Row indices of feature i, ascending (built once: a scan of the whole table per feature made
        the road tile builds quadratic in a cell's features, 2026-10-13 profile)."""
        if self._rows is None:
            order = np.argsort(self.feature, kind="stable")
            f = self.feature[order]
            cut = np.flatnonzero(np.diff(f)) + 1
            self._rows = {int(g[0]): r for g, r in zip(np.split(f, cut), np.split(order, cut))} if len(f) else {}
        return self._rows.get(int(i), _NO_ROWS)

    def major(self, i):
        """The row of feature i's rule covering the largest share of it (the first such row), or -1. One
        sort for the whole table (a road cell asked ~10 tables for each of its ~3000 features)."""
        if self._major is None:
            m = len(self.feature)
            f = np.asarray(self.feature, np.int64)
            maj = np.full(int(f.max()) + 1 if m else 0, -1, np.int64)
            if m:
                span = self.end - self.start
                order = np.lexsort((np.arange(m), -span, f))
                first = np.r_[True, f[order][1:] != f[order][:-1]]
                maj[f[order][first]] = order[first]
            self._major = maj
        return int(self._major[i]) if 0 <= i < len(self._major) else -1

    def for_feature(self, i):
        r = self.rows(i)
        return list(zip(self.value[r].tolist(), self.start[r].tolist(), self.end[r].tolist()))


@dataclass
class Features:
    layer: str
    ids: np.ndarray                     # unicode, sorted ascending (GERS ids)
    kind: np.ndarray                    # uint8 POINT / LINE / POLYGON
    part_off: np.ndarray                # int64, len n + 1
    ring_off: np.ndarray                # int64, len parts + 1
    coord_off: np.ndarray               # int64, len rings + 1
    lonlat: np.ndarray                  # float64 (N, 2)
    en: np.ndarray | None = None        # float64 (N, 2), set by localize()
    own: np.ndarray | None = None       # bool: build it in this cell (its bbox centre lies here, or
                                        # it is this cell's clipped piece of a polygon)
    clipped: np.ndarray | None = None   # bool: the geometry was clipped to the cell
    attrs: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.ids)

    @property
    def n_coords(self):
        return len(self.lonlat)

    def parts(self, i, xy=None):
        """Feature i as a list of parts, each a list of rings ((k, 2) arrays of lonlat, or of `xy`)."""
        xy = self.lonlat if xy is None else xy
        out = []
        for p in range(self.part_off[i], self.part_off[i + 1]):
            out.append([xy[self.coord_off[r]:self.coord_off[r + 1]]
                        for r in range(self.ring_off[p], self.ring_off[p + 1])])
        return out

    def index(self, fid):
        """Row of a feature id, or -1."""
        k = int(np.searchsorted(self.ids, fid))
        return k if k < len(self.ids) and self.ids[k] == fid else -1

    def xy_in(self, frame):
        """(N, 2) east / north of every coordinate in frame (an EnuFrame), converted once per frame and kept (the
        cells stay decoded in memory: ~60k one-ring conversions a 9-tile set before, 2026-10-15)."""
        key = (frame.lat0, frame.lon0, frame.h0)
        memo = self.__dict__.setdefault("_xy_in", {})
        got = memo.get(key)
        if got is None:
            if len(self.lonlat):
                e, n, _ = frame.geodetic_to_enu(self.lonlat[:, 1], self.lonlat[:, 0], 0.0)
                got = np.stack([e, n], axis=1)
            else:
                got = np.zeros((0, 2), np.float64)
            if len(memo) >= 2:                  # an anchor or two at a time
                memo.pop(next(iter(memo)))
            memo[key] = got
        return got

    def outer_centres(self, frame):
        """(cx, cy): per feature the mean of its first ring's points (the closing point left out) in frame, from
        cumulative sums (within ~1e-6 m of the per-ring mean: a prefilter only); NaN without a ring of >= 4
        coordinates. Kept per frame like xy_in."""
        key = (frame.lat0, frame.lon0, frame.h0)
        memo = self.__dict__.setdefault("_centres", {})
        got = memo.get(key)
        if got is None:
            n = len(self.part_off) - 1
            cx = np.full(n, np.nan)
            cy = np.full(n, np.nan)
            if n > 0 and len(self.lonlat):
                p0 = self.part_off[:-1]
                ok = self.part_off[1:] > p0
                r0 = self.ring_off[np.minimum(p0, len(self.ring_off) - 2)]
                ok &= self.ring_off[np.minimum(p0 + 1, len(self.ring_off) - 1)] > r0
                c0 = self.coord_off[np.minimum(r0, len(self.coord_off) - 2)]
                c1 = self.coord_off[np.minimum(r0 + 1, len(self.coord_off) - 1)]
                ok &= (c1 - c0) >= 4
                xy = self.xy_in(frame)
                cs = np.vstack([np.zeros((1, 2)), np.cumsum(xy, axis=0)])
                k = np.maximum(c1 - c0 - 1, 1)
                sx = cs[np.where(ok, c1 - 1, 0), 0] - cs[np.where(ok, c0, 0), 0]
                sy = cs[np.where(ok, c1 - 1, 0), 1] - cs[np.where(ok, c0, 0), 1]
                cx = np.where(ok, sx / k, np.nan)
                cy = np.where(ok, sy / k, np.nan)
            if len(memo) >= 2:
                memo.pop(next(iter(memo)))
            got = memo[key] = (cx, cy)
        return got

    def boxes(self, frame):
        """(n, 4): per feature the [min e, min n, max e, max n] of all its coordinates in frame (xy_in's numbers);
        +inf / -inf for a feature without any. Kept per frame like xy_in: a tile's loops over a cell's features test
        these first (a far buildings tile went through every road of its cells for the few near it)."""
        key = (frame.lat0, frame.lon0, frame.h0)
        memo = self.__dict__.setdefault("_boxes", {})
        got = memo.get(key)
        if got is None:
            n = len(self.part_off) - 1
            got = np.empty((max(n, 0), 4))
            got[:, :2], got[:, 2:] = np.inf, -np.inf
            if n > 0 and len(self.lonlat):
                c = self.coord_off[self.ring_off[self.part_off]]           # each feature's first coordinate
                c0, c1 = c[:-1], c[1:]
                xy = self.xy_in(frame)
                has = c1 > c0
                if has.any():
                    got[has, 0] = _reduce(np.minimum, xy[:, 0], c0[has], c1[has])
                    got[has, 1] = _reduce(np.minimum, xy[:, 1], c0[has], c1[has])
                    got[has, 2] = _reduce(np.maximum, xy[:, 0], c0[has], c1[has])
                    got[has, 3] = _reduce(np.maximum, xy[:, 1], c0[has], c1[has])
            if len(memo) >= 2:
                memo.pop(next(iter(memo)))
            got = memo[key] = got
        return got

    def localize(self, frame):
        """Fill `en` from `lonlat` with an EnuFrame (the cell's)."""
        if len(self.lonlat):
            e, n, _ = frame.geodetic_to_enu(self.lonlat[:, 1], self.lonlat[:, 0], 0.0)
            self.en = np.stack([e, n], axis=1)
        else:
            self.en = np.zeros((0, 2), np.float64)
        return self

    # --- serialisation (npz without pickles) -------------------------------------------------
    def to_arrays(self, prefix):
        a = {f"{prefix}ids": self.ids, f"{prefix}kind": self.kind, f"{prefix}part_off": self.part_off,
             f"{prefix}ring_off": self.ring_off, f"{prefix}coord_off": self.coord_off,
             f"{prefix}lonlat": self.lonlat}
        if self.en is not None:
            a[f"{prefix}en"] = self.en
        if self.own is not None:
            a[f"{prefix}own"] = self.own
        if self.clipped is not None:
            a[f"{prefix}clipped"] = self.clipped
        for k, v in self.attrs.items():
            a[f"{prefix}attr.{k}"] = v
        for k, r in self.rules.items():
            for col in ("feature", "value", "start", "end"):
                a[f"{prefix}rule.{k}.{col}"] = getattr(r, col)
        return a

    @classmethod
    def from_arrays(cls, layer, arrays, prefix):
        g = lambda k: arrays.get(prefix + k)  # noqa: E731
        f = cls(layer, g("ids"), g("kind"), g("part_off"), g("ring_off"), g("coord_off"), g("lonlat"),
                g("en"), g("own"), g("clipped"))
        rules = {}
        for k in sorted(arrays):
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if rest.startswith("attr."):
                f.attrs[rest[5:]] = arrays[k]
            elif rest.startswith("rule."):
                name, col = rest[5:].rsplit(".", 1)
                rules.setdefault(name, {})[col] = arrays[k]
        f.rules = {k: RuleTable(v["feature"], v["value"], v["start"], v["end"]) for k, v in sorted(rules.items())}
        return f


def drop_huge(f, max_m2):
    """f with every polygon whose outer ring encloses more than max_m2 collapsed onto its first point (a ring of no
    area: every reader leaves it out), or f itself when there is none. An OpenStreetMap "building" of 15.8 km2 roofed
    a whole city in Egypt (GitHub #413); the largest real buildings are about 1 km2. Its ids, attrs and rules stay."""
    n = len(f)
    if not n or not len(f.lonlat):
        return f
    huge = []
    for i in range(n):
        if f.kind[i] != POLYGON or f.part_off[i + 1] == f.part_off[i]:
            continue
        r0 = f.ring_off[f.part_off[i]]
        a, b = f.coord_off[r0], f.coord_off[r0 + 1]
        if b - a < 3:
            continue
        ll = f.lonlat[a:b]
        k = math.cos(math.radians(float(ll[:, 1].mean())))
        x, y = ll[:, 0] * (111320.0 * k), ll[:, 1] * 110540.0
        if 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) > max_m2:
            huge.append(i)
    if not huge:
        return f
    lonlat = np.array(f.lonlat, np.float64, copy=True)
    en = None if f.en is None else np.array(f.en, np.float64, copy=True)
    for i in huge:
        a = f.coord_off[f.ring_off[f.part_off[i]]]
        b = f.coord_off[f.ring_off[f.part_off[i + 1]]]
        lonlat[a:b] = lonlat[a]
        if en is not None:
            en[a:b] = en[a]
    return replace(f, lonlat=lonlat, en=en)


def empty(layer):
    return Features(layer, np.zeros(0, "<U1"), np.zeros(0, np.uint8), np.zeros(1, np.int64),
                    np.zeros(1, np.int64), np.zeros(1, np.int64), np.zeros((0, 2), np.float64))


def concat(a, b):
    """Features a with b's features after them (every id of b sorts after a's: the ids stay ascending), b's attrs
    and rules joined in (columns one side lacks filled: "" for text, NaN for numbers, False for flags). Pure."""
    if b is None or len(b) == 0:
        return a
    if len(a) and len(b) and not str(a.ids[-1]) < str(b.ids[0]):
        raise ValueError("concat: b's ids must sort after a's")
    n_a = len(a)
    ids = np.array([str(x) for x in a.ids] + [str(x) for x in b.ids])
    out = Features(a.layer, ids, np.r_[a.kind, b.kind].astype(np.uint8),
                   np.r_[a.part_off, b.part_off[1:] + a.part_off[-1]].astype(np.int64),
                   np.r_[a.ring_off, b.ring_off[1:] + a.ring_off[-1]].astype(np.int64),
                   np.r_[a.coord_off, b.coord_off[1:] + a.coord_off[-1]].astype(np.int64),
                   np.concatenate([a.lonlat.reshape(-1, 2), b.lonlat.reshape(-1, 2)]).astype(np.float64))

    def fill(col, n):
        if col.dtype.kind == "U":
            return np.full(n, "", col.dtype)
        if col.dtype.kind == "b":
            return np.zeros(n, bool)
        return np.full(n, np.nan, col.dtype if col.dtype.kind == "f" else np.float64)

    for k in sorted(set(a.attrs) | set(b.attrs)):
        ca = a.attrs.get(k)
        cb = b.attrs.get(k)
        ca = fill(cb, n_a) if ca is None else ca
        cb = fill(ca, len(b)) if cb is None else cb
        if ca.dtype.kind == "U" or cb.dtype.kind == "U":
            out.attrs[k] = text_column(list(ca.tolist()) + list(cb.tolist()))
        else:
            out.attrs[k] = np.concatenate([ca, cb])
    for k in sorted(set(a.rules) | set(b.rules)):
        ra, rb = a.rules.get(k), b.rules.get(k)
        parts = [r for r in (ra, rb) if r is not None]
        textual = any(r.value.dtype.kind == "U" for r in parts)
        vals = [v for r in parts for v in r.value.tolist()]
        out.rules[k] = RuleTable(
            np.r_[ra.feature if ra is not None else np.zeros(0, np.int32),
                  (rb.feature + n_a) if rb is not None else np.zeros(0, np.int32)].astype(np.int32),
            text_column(vals) if textual else np.array(vals, np.float32),
            np.concatenate([r.start for r in parts]).astype(np.float32),
            np.concatenate([r.end for r in parts]).astype(np.float32))
    if a.own is not None or b.own is not None:
        out.own = np.r_[a.own if a.own is not None else np.zeros(n_a, bool),
                        b.own if b.own is not None else np.zeros(len(b), bool)]
    if a.clipped is not None or b.clipped is not None:
        out.clipped = np.r_[a.clipped if a.clipped is not None else np.zeros(n_a, bool),
                            b.clipped if b.clipped is not None else np.zeros(len(b), bool)]
    return out


def text_column(values):
    """Python strings (None = missing) -> a fixed-width unicode array ("" = missing)."""
    v = ["" if x is None else str(x) for x in values]
    width = max([1] + [len(x) for x in v])
    return np.array(v, dtype=f"<U{width}")


def float_column(values, dtype=np.float32):
    return np.array([np.nan if x is None else float(x) for x in values], dtype=dtype)


def bool_column(values):
    return np.array([bool(x) for x in values], dtype=bool)


def rule_table(rows, value_kind="text"):
    """rows: (feature, value, between) with between None or [start, end]."""
    f = np.array([r[0] for r in rows], np.int32)
    vals = [r[1] for r in rows]
    v = text_column(vals) if value_kind == "text" else float_column(vals, np.float32)
    s = np.array([0.0 if not r[2] else float(r[2][0]) for r in rows], np.float32)
    e = np.array([1.0 if not r[2] else float(r[2][1]) for r in rows], np.float32)
    return RuleTable(f, v, s, e)


# --- WKB ----------------------------------------------------------------------------------------
class WkbError(ValueError):
    pass


def _coords(buf, off, n, bo, dims):
    end = off + n * dims * 8
    if end > len(buf):
        raise WkbError("truncated WKB")
    a = np.frombuffer(buf, dtype=f"{bo}f8", count=n * dims, offset=off).reshape(n, dims)[:, :2]
    return a.astype(np.float64), end


def _geom(buf, off):
    """(kind, parts, new offset); parts as lists of rings ((k, 2) arrays)."""
    if off + 5 > len(buf):
        raise WkbError("truncated WKB")
    bo = "<" if buf[off] == 1 else ">"
    t = struct.unpack_from(bo + "I", buf, off + 1)[0]
    off += 5
    dims = 2
    if t & 0x80000000:                      # EWKB Z flag
        dims += 1
    if t & 0x40000000:                      # EWKB M flag
        dims += 1
    if t & 0x20000000:                      # EWKB SRID
        off += 4
    t &= 0x0FFFFFFF
    if t >= 1000:                           # ISO Z / M / ZM
        dims = {1: 3, 2: 3, 3: 4}[t // 1000]
        t %= 1000
    if t == 1:
        c, off = _coords(buf, off, 1, bo, dims)
        return POINT, [[c]], off
    if t == 2:
        n = struct.unpack_from(bo + "I", buf, off)[0]
        c, off = _coords(buf, off + 4, n, bo, dims)
        return LINE, [[c]], off
    if t == 3:
        nr = struct.unpack_from(bo + "I", buf, off)[0]
        off += 4
        rings = []
        for _ in range(nr):
            n = struct.unpack_from(bo + "I", buf, off)[0]
            c, off = _coords(buf, off + 4, n, bo, dims)
            rings.append(c)
        return POLYGON, [rings], off
    if t in (4, 5, 6, 7):
        n = struct.unpack_from(bo + "I", buf, off)[0]
        off += 4
        kinds, parts = [], []
        for _ in range(n):
            k, p, off = _geom(buf, off)
            kinds.append(k)
            parts += p
        kind = {4: POINT, 5: LINE, 6: POLYGON}.get(t) or (max(kinds) if kinds else POINT)
        if t == 7:                          # a collection: keep the parts of its highest kind
            keep = [p for k, p in zip(kinds, parts) if k == kind] if kinds else []
            parts = keep
        return kind, parts, off
    raise WkbError(f"unsupported WKB type {t}")


def parse_wkb(buf):
    """WKB bytes -> (kind, parts)."""
    kind, parts, _ = _geom(bytes(buf), 0)
    return kind, parts


def build(layer, ids, geoms):
    """Features from sorted ids and (kind, parts) geometries (attributes added by the caller)."""
    kinds = np.array([k for k, _ in geoms], np.uint8)
    part_off, ring_off, coord_off, coords = [0], [0], [0], []
    np_ = nr = nc = 0
    for _, parts in geoms:
        for rings in parts:
            for r in rings:
                coords.append(r)
                nc += len(r)
                coord_off.append(nc)
            nr += len(rings)
            ring_off.append(nr)
        np_ += len(parts)
        part_off.append(np_)
    lonlat = np.concatenate(coords) if coords else np.zeros((0, 2), np.float64)
    return Features(layer, ids, kinds, np.array(part_off, np.int64), np.array(ring_off, np.int64),
                    np.array(coord_off, np.int64), np.ascontiguousarray(lonlat, np.float64))


# --- clipping -----------------------------------------------------------------------------------
def _clip_edge(p, axis, v, keep_ge):
    """Sutherland-Hodgman against one half-plane: p is an open ring (k, 2); returns the open ring."""
    if len(p) == 0:
        return p
    inside = p[:, axis] >= v if keep_ge else p[:, axis] <= v
    if inside.all():
        return p
    if not inside.any():
        return p[:0]
    q = np.roll(p, -1, axis=0)
    qin = np.roll(inside, -1)
    cross = inside != qin
    cnt = qin.astype(np.int64) + cross
    d = q[:, axis] - p[:, axis]
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(cross, (v - p[:, axis]) / np.where(d == 0, 1.0, d), 0.0)
    inter = p + t[:, None] * (q - p)
    inter[cross, axis] = v
    total = int(cnt.sum())
    idx = np.repeat(np.arange(len(p)), cnt)
    slot = np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    use_i = (slot == 0) & cross[idx]
    return np.where(use_i[:, None], inter[idx], q[idx])


def clip_ring(ring, bounds):
    """A closed ring clipped to the box (x0, y0, x1, y1); a closed ring, or None if < 3 vertices
    or no area is left."""
    x0, y0, x1, y1 = bounds
    p = ring[:-1] if len(ring) > 1 and np.array_equal(ring[0], ring[-1]) else ring
    for axis, v, ge in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        p = _clip_edge(p, axis, v, ge)
        if len(p) < 3:
            return None
    # drop consecutive duplicates (vertices on the box edges)
    keep = np.ones(len(p), bool)
    keep[1:] = np.any(p[1:] != p[:-1], axis=1)
    p = p[keep]
    if len(p) > 1 and np.array_equal(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 3:
        return None
    # no area left (a sliver along the box edge, a spike out and back): twice the signed area summed
    # exactly with math.fsum, so the answer does not depend on the order of the terms (the ring's
    # first vertex, or the order a BLAS sums np.dot in on another CPU: a rounded sum kept or dropped
    # such rings at random, and the cell's content with them)
    x, y = p[:, 0], p[:, 1]
    if math.fsum(np.concatenate([x * np.roll(y, -1), -(y * np.roll(x, -1))]).tolist()) == 0.0:
        return None
    return np.concatenate([p, p[:1]])


def clip_polygon_parts(parts, bounds):
    """Polygon parts clipped to the box; parts whose exterior vanishes are dropped. Returns
    (parts, changed)."""
    x0, y0, x1, y1 = bounds
    out, changed = [], False
    for rings in parts:
        ext = rings[0]
        lo, hi = ext.min(axis=0), ext.max(axis=0)
        if lo[0] >= x0 and hi[0] <= x1 and lo[1] >= y0 and hi[1] <= y1:
            out.append(rings)
            continue
        changed = True
        e = clip_ring(ext, bounds)
        if e is None:
            continue
        new = [e]
        for h in rings[1:]:
            c = clip_ring(h, bounds)
            if c is not None:
                new.append(c)
        out.append(new)
    return out, changed


def simplify_ring(ring, tol):
    """A closed ring (k, 2) with the vertices closer than tol to their neighbours' line dropped (Ramer-Douglas-Peucker,
    iterative: sidewalks.simplify's), still closed; the ring itself when fewer than 4 vertices would be left. Pure."""
    p = np.asarray(ring, np.float64)
    k = len(p)
    if k <= 4:
        return p
    keep = np.zeros(k, bool)
    far = int(np.argmax(((p - p[0]) ** 2).sum(1)))      # (a closed ring's ends are one point: split it at the far one)
    keep[[0, far, k - 1]] = True
    stack = [(0, far), (far, k - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg, rel = p[b] - p[a], p[a + 1:b] - p[a]
        L2 = float(seg @ seg)
        d = rel - np.clip(rel @ seg / L2, 0.0, 1.0)[:, None] * seg if L2 > 0 else rel
        d2 = (d * d).sum(1)
        i = int(np.argmax(d2))
        if d2[i] > tol * tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return p[keep] if keep.sum() >= 4 else p


def simplify_polygon_parts(parts, tol):
    """Polygon parts with each ring simplify_ring'd at tol (the parts' own units). Pure."""
    return [[simplify_ring(r, tol) for r in rings] for rings in parts]


def _reduce(ufunc, v, a, b):
    """ufunc.reduce of v[a[k]:b[k]] for each k (every range non-empty, ascending, not overlapping)."""
    if len(a) == 0:
        return v[:0].copy()
    # reduceat runs each range to the next start: with gaps between the ranges (outer rings only, a subset of the
    # features), the ranges are gathered end to end first (one reduce per gap was ~12k numpy calls a block photo)
    if (len(b) > 1 and (b[:-1] < a[1:]).any()) or b[-1] < len(v):
        n = b - a
        starts = np.r_[0, np.cumsum(n)[:-1]]
        idx = np.repeat(a - starts, n) + np.arange(int(n.sum()))
        return ufunc.reduceat(v[idx], starts)
    return ufunc.reduceat(v, a)


def enu_xy(feats, frame, ring):
    """(e, n) of a lon/lat ring in frame: sliced from feats.xy_in(frame) when the ring is a view of feats.lonlat
    (feats.parts' rings), else converted. Point by point the same numbers either way."""
    L = feats.lonlat if isinstance(feats, Features) else None
    if (L is not None and isinstance(ring, np.ndarray) and ring.ndim == 2 and len(ring) and L.ndim == 2
            and ring.dtype == L.dtype and ring.strides == L.strides and ring.shape[1] == L.shape[1]):
        off, rem = divmod(ring.__array_interface__["data"][0] - L.__array_interface__["data"][0], L.strides[0])
        if rem == 0 and 0 <= off and off + len(ring) <= len(L):
            xy = feats.xy_in(frame)[off:off + len(ring)]
            return xy[:, 0], xy[:, 1]
    e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
    return e, n

