"""The road the player drives on, followed ahead: where a fast player will be, from the map's roads instead of a
straight line or a guessed bend.

A car on a road almost always stays on it. follow() snaps the player to the road it is driving along (near it, heading
with it) and walks that road ahead: at each end of a road line it takes the line that continues the same road (its
name, its class, not a ramp off it, the smallest bend), and stops where two lines continue it equally well (a real
fork) or where the known roads end. The terrain prefetch lays its chain of blocks along that line
(earthd.predict_route_chain), as it does along a GPS route.

RoadAhead keeps the road lines of the map cells round the player, read in a thread of its own from the cells on disk
only (the map data ahead, earthd.map_ahead, has them there well before the player): never a download, never on the
event loop. Off the known roads, or off any road, follow() gives None and the prefetch predicts as before.
"""
import math

import dmath
import dnp
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import lanesections
import mapcells

SNAP_M = 25.0           # m: the player this near a road line ...
SNAP_ALIGN = 0.9        # ... heading along it (cos of the angle) is on it
JOIN_M = 6.0            # m: a line starting (or ending) this near another's end continues it
TURN_MAX = math.radians(60)     # a continuation bending more than this at the join is a turn off the road
FORK_RAD = math.radians(12)     # two continuations as alike as this, bending within this of each other: a fork
CELL_RADIUS_M = 5500.0  # m: the cells this near the player are read (those on disk: past earthd.ROAD_AHEAD_MAX_M)
ROAD_CLASSES = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "unclassified",
                          "living_street", "service"})


class RoadNet:
    """Road lines in one frame: lines [(k, 2) arrays], with class, name and is-a-link per line (given as (xy, class,
    name, link[, id]): a line's id once)."""

    def __init__(self, lines=()):
        self.lines, self.cls, self.name, self.link = [], [], [], []
        lines = list(lines)
        seen = _twice(line[4] for line in lines if len(line) > 4)
        for xy, cls, name, link, *rid in lines:
            if rid:
                if rid[0] in seen:
                    continue
                seen.add(rid[0])
            self.add(xy, cls, name, link)
        self._index()

    def add(self, xy, cls, name, link):
        xy = np.asarray(xy, np.float64)
        if len(xy) >= 2:
            self.lines.append(xy)
            self.cls.append(cls)
            self.name.append(name or "")
            self.link.append(bool(link))

    def _index(self):
        n = len(self.lines)
        self.box = np.array([[*xy.min(0), *xy.max(0)] for xy in self.lines], np.float64).reshape(n, 4)
        self.ends = np.array([[xy[0], xy[-1]] for xy in self.lines], np.float64).reshape(n, 2, 2)

    def snap(self, x, y, vx, vy):
        """(line, segment, t along it, direction +1/-1) of the road the player drives along, or None."""
        sp = math.hypot(vx, vy)
        if sp < 1.0 or not self.lines:
            return None
        b = self.box
        near = np.flatnonzero((b[:, 0] - SNAP_M <= x) & (x <= b[:, 2] + SNAP_M)
                              & (b[:, 1] - SNAP_M <= y) & (y <= b[:, 3] + SNAP_M))
        best, bd = None, SNAP_M
        for i in near.tolist():
            xy = self.lines[i]
            a, d = xy[:-1], xy[1:] - xy[:-1]
            ln2 = np.maximum((d * d).sum(1), 1e-9)
            t = np.clip(((x - a[:, 0]) * d[:, 0] + (y - a[:, 1]) * d[:, 1]) / ln2, 0.0, 1.0)
            dist = dnp.hypot(a[:, 0] + d[:, 0] * t - x, a[:, 1] + d[:, 1] * t - y)
            j = int(np.argmin(dist))
            cos = (d[j, 0] * vx + d[j, 1] * vy) / (math.sqrt(ln2[j]) * sp)
            if dist[j] <= bd and abs(cos) >= SNAP_ALIGN:
                best, bd = (i, j, float(t[j]), 1 if cos > 0 else -1), float(dist[j])
        return best

    def _next(self, i, sign):
        """The line continuing line i past its end (sign +1: its last point; -1: its first) and its direction, or None
        at a fork, a turn, or the end of the known roads."""
        xy = self.lines[i]
        end, prev = (xy[-1], xy[-2]) if sign > 0 else (xy[0], xy[1])
        h0 = dmath.atan2(end[1] - prev[1], end[0] - prev[0])
        cands = []
        for d, col in ((1, 0), (-1, 1)):        # lines starting at the end (taken forwards), ending there (backwards)
            p = self.ends[:, col]
            for k in np.flatnonzero(dnp.hypot(p[:, 0] - end[0], p[:, 1] - end[1]) <= JOIN_M).tolist():
                if k == i:
                    continue
                line = self.lines[k]
                a, q = (line[0], line[1]) if d > 0 else (line[-1], line[-2])
                bend = abs((dmath.atan2(q[1] - a[1], q[0] - a[0]) - h0 + math.pi) % (2 * math.pi) - math.pi)
                if bend <= TURN_MAX:
                    # the same road first: its name, its class, not a ramp off it; then the straightest
                    rank = ((self.name[k] != self.name[i]) or not self.name[i],
                            self.cls[k] != self.cls[i], self.link[k] and not self.link[i])
                    cands.append((rank, bend, k, d))
        if not cands:
            return None
        cands.sort()
        if len(cands) > 1 and cands[1][0] == cands[0][0] and cands[1][1] - cands[0][1] < FORK_RAD:
            return None             # a fork: either way as likely
        return cands[0][2], cands[0][3]

    def follow(self, x, y, vx, vy, max_m):
        """The player's road ahead from (x, y), (k, 2), at most max_m along it; None off the known roads."""
        got = self.snap(x, y, vx, vy)
        if got is None:
            return None
        i, j, t, sign = got
        xy = self.lines[i]
        p = xy[j] + (xy[j + 1] - xy[j]) * t
        pts = [p]
        rest = xy[j + 1:] if sign > 0 else xy[j::-1]
        gone, seen = 0.0, {i}
        while True:
            for q in rest:
                gone += math.hypot(q[0] - pts[-1][0], q[1] - pts[-1][1])
                pts.append(q)
                if gone >= max_m:
                    return np.array(pts, np.float64)
            nxt = self._next(i, sign)
            if nxt is None or nxt[0] in seen:
                break
            i, sign = nxt
            seen.add(i)
            rest = self.lines[i][1:] if sign > 0 else self.lines[i][-2::-1]
        return np.array(pts, np.float64) if len(pts) >= 2 and gone > 1.0 else None


def _twice(ids):
    """Of these line ids, the ones whose road is there in another form too: a road and its lane sections
    (lanesections.split cuts a merged highway where its lane count changes; one cell's layer can hold a road whole and
    its neighbour's the sections that reach into it). The sections where they cover the road, else the road whole: both
    kept, they left its start together, as alike as two roads can, and the road ahead ended there as at a fork until
    the car was past it: no blocks built ahead for 25 s of a motorway, the next one from scratch at the edge
    (2026-11-17)."""
    ids = set(ids)
    spans = {}
    for rid in ids:
        m = lanesections.SECTION_ID.fullmatch(str(rid))
        if m:
            spans.setdefault(m.group(1), {})[rid] = (int(m.group(2)), int(m.group(3)))
    out = set()
    for road, parts in spans.items():
        if lanesections.spans_cover(parts.values()):
            out.add(road)
        elif road in ids:
            out.update(parts)
    return out


def cell_lines(feats, frame):
    """[(xy, class, name, link, id)] of a roads layer's road lines in frame (a line crossing into a neighbour cell is
    in both cells' layers, under one id: RoadNet keeps it once)."""
    out = []
    if feats is None or len(feats.kind) == 0:
        return out
    cls = feats.attrs.get("class")
    if cls is None:
        return out
    names = feats.attrs.get("name")
    sub = feats.attrs.get("subclass")
    allxy = feats.xy_in(frame)
    for i in range(len(feats.kind)):
        c = str(cls[i])
        if c not in ROAD_CLASSES:
            continue
        a = feats.coord_off[feats.ring_off[feats.part_off[i]]]
        b = feats.coord_off[feats.ring_off[feats.part_off[i + 1]]]
        if b - a < 2:
            continue
        name = str(names[i]) if names is not None and names[i] is not None else ""
        link = sub is not None and str(sub[i]) == "link"
        out.append((allxy[a:b], c, "" if name in ("None", "nan") else name, link, str(feats.ids[i])))
    return out


class RoadAhead:
    """The road lines of the cells round the player (on disk only, read in a thread of their own) and the road ahead
    of it (follow)."""

    def __init__(self, mapdata):
        self.md = mapdata
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="road-ahead")
        self.lock = threading.Lock()
        self.cells = {}         # (anchor id, cell key) -> [(xy, class, name, link, id)]
        self.loading = set()
        self.net, self.net_keys = RoadNet(), None
        self.building = False

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _load(self, aid, frame, key):
        try:
            feats = self.md.cell(key, layers=("roads",), kind="background").layers.get("roads")
            got = cell_lines(feats, frame)
        except Exception:
            got = []
        with self.lock:
            self.cells[(aid, key)] = got
            self.loading.discard((aid, key))

    def follow(self, anchor, x, y, vx, vy, max_m):
        """The road ahead of the player of anchor (its frame), or None; asks for the cells round the player."""
        if self.md is None:
            return None
        la, lo, _ = anchor.frame.enu_to_geodetic(np.array([x - CELL_RADIUS_M, x + CELL_RADIUS_M]),
                                                 np.array([y - CELL_RADIUS_M, y + CELL_RADIUS_M]), 0.0)
        try:
            keys = mapcells.cells_for_bbox(float(min(la)), float(min(lo)), float(max(la)), float(max(lo)),
                                           max_cells=144)
        except ValueError:
            return None
        want = [(anchor.id, k) for k in keys]
        with self.lock:
            for ak in want:
                if ak not in self.cells and ak not in self.loading and self.md.has(ak[1], "roads"):
                    self.loading.add(ak)
                    self.pool.submit(self._load, anchor.id, anchor.frame, ak[1])
            have = tuple(ak for ak in want if ak in self.cells)
            if have != self.net_keys and not self.building:
                self.building = True            # (made in the thread: thousands of lines, not on the event loop)
                self.pool.submit(self._build, have, set(want))
            net = self.net
        return net.follow(x, y, vx, vy, max_m)

    def _build(self, have, want):
        try:
            with self.lock:
                lines = [line for ak in have if ak in self.cells for line in self.cells[ak]]
                for ak in [ak for ak in self.cells if ak not in want]:
                    del self.cells[ak]          # (cells left behind)
            net = RoadNet(lines)
            with self.lock:
                self.net, self.net_keys = net, have
        finally:
            self.building = False
