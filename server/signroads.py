"""Sign-only pavement outlines from the current road renderer's widths and centre lines (from cody196's PR #363).

These helpers never alter a road, lane count, terrain mask or bridge profile. A sign's whole source neighbourhood
(the map cells round its approach) is used in every tile, so its position has one owner.

Each map cell's rows are worked out once (road_specs on the cell, as the road tiles do, and every ground part's
pavement outline), kept on the decoded cell while the map data keeps it; a neighbourhood is the union of its cells'
rows, each feature once. (The PR merged the cells of each sign's neighbourhood into one feature set and ran
road_specs and every outline on it again for each sign: ~10 s of CPU a road tile near Baltimore, 2026-10-28.)
"""
from collections import namedtuple, OrderedDict
import threading

import numpy as np

import mapcells
import mapgeom
import railtags
import roads
import signcontext

Spec = namedtuple('SignRoad', 'width cls oneway dir link kmh', defaults=(False, None))
NEIGHBOURHOODS = 64             # combined rows kept (Context._hoods)
CELL_FRAMES = 4                 # anchors a cell keeps rows for


class Outlines(list):
    """Pavement outline polygons with their bounding boxes (lo, hi: (n, 2)), for exitsigns._clear."""

    def __init__(self, polygons):
        super().__init__(polygons)
        self.lo = np.array([p.min(0) for p in polygons]).reshape(-1, 2)
        self.hi = np.array([p.max(0) for p in polygons]).reshape(-1, 2)


class Pavements(list):
    """Immutable source ribbons (fid, spec, line, width) with their outlines and bounded, elevation-free plans
    (exit gores: exitsigns._gore; merge signs: mergesigns.plans)."""
    def __init__(self, rows=(), outlines=()):
        super().__init__(rows)
        self.outlines = Outlines(list(outlines)) if outlines else None
        self.gore_plans = OrderedDict()
        self.gore_lock = threading.Lock()
        self.plans = {}


_specs_lock = threading.Lock()


def cell_specs(feats, urban=None, widths=None, left_hand=False):
    """roads.road_specs of a decoded map cell, kept on it (the sign rows and the speed signs share one: road_specs
    is the costly part of both)."""
    key = (urban, widths, bool(left_hand))
    with _specs_lock:
        memo = feats.__dict__.setdefault('_sign_specs', {})
        got = memo.get(key)
    if got is None:
        got = roads.road_specs(feats, urban, widths, left_hand)
        with _specs_lock:
            if len(memo) >= CELL_FRAMES:
                memo.pop(next(iter(memo)))
            memo[key] = got
    return got


def spec(value, link=False):
    return Spec(value.width, value.cls, value.oneway, value.dir, link, value.kmh)


def cross(points):
    return (points[:, 3:5] if points.shape[1] == 5 else
            roads.offset_polyline(points, 1.)[:, :2] - points[:, :2])


def pavement(value, points, frame=None, left_hand=False):
    return points, value.width


def is_link(feats, i):
    return roads._rule_value(feats.rules.get('subclass'), i) == 'link'


def feature_id(feats, i):
    """'osm:w<way id>:<feature id>' for a segment from an OpenStreetMap way (Overture's src_record "w<id>@<v>"),
    which exitsigns._way_id reads to match a sign's ramp and mainline ways; else the feature id."""
    rec = feats.attrs.get('src_record')
    wid = railtags.way_id(str(rec[i])) if rec is not None else None
    return f'osm:w{wid}:{feats.ids[i]}' if wid else str(feats.ids[i])


def outline(value, line, frame=None, left_hand=False, link=False):
    """Use the beta renderer's centreline, width and mitered offsets unchanged."""
    return spec(value, link), line, value.width


def polygon(line, width):
    """The closed pavement outline of a centre line (exitsigns._outline)."""
    offset = cross(line) * width / 2
    a, b = line[:, :2] + offset, line[:, :2] - offset
    return np.vstack([a, b[::-1], a[:1]])


class Context(signcontext.Context):
    def __init__(self, cell, urban=None, widths=None):
        super().__init__(cell)
        self.urban, self.widths = urban, widths
        self._hoods = OrderedDict()

    def _cell_rows(self, feats, frame, left_hand):
        """(rows, outlines) of one decoded map cell in frame (kept on the cell for CELL_FRAMES anchors)."""
        key = (frame.lat0, frame.lon0, frame.h0, bool(left_hand), self.urban, self.widths)
        with self.lock:
            memo = feats.__dict__.setdefault('_sign_rows', {})
            got = memo.get(key)
        if got is not None:
            return got
        rows, polys = [], []
        specs = cell_specs(feats, self.urban, self.widths, left_hand)
        placement = roads.placement_context([feats], self.urban, self.widths, left_hand)  # (as the road tiles draw)
        for i, value in enumerate(specs):
            if value is None:
                continue
            fid = feature_id(feats, i)
            link = is_link(feats, i)
            for ring in roads.ground_parts(feats, i):
                if len(ring) < 2:
                    continue
                ring = placement.ring(frame, feats, i, ring, value)
                e, n = mapgeom.enu_xy(feats, frame, ring)
                line = np.c_[e, n, np.zeros(len(ring))]
                sp, line, width = outline(value, line, frame, left_hand, link)
                rows.append((fid, sp, line, width))
                polys.append(polygon(line, width))
        got = (rows, polys)
        with self.lock:
            if len(memo) >= CELL_FRAMES:
                memo.pop(next(iter(memo)))
            memo[key] = got
        return got

    def pavements(self, frame, points, left_hand=False, margin=signcontext.MARGIN_M):
        """The pavement rows of the map cells within signcontext.MARGIN_M of points (lon, lat), each feature once,
        in cell order (the same Pavements object while it is kept: its plans are reused)."""
        points = np.asarray(points, float).reshape(-1, 2)
        if not len(points):
            return Pavements()
        pad = margin / 110540.
        lon_pad = pad / max(.01, float(np.cos(np.radians(points[:, 1])).min()))
        keys = tuple(mapcells.cells_near_points(points[:, 1], points[:, 0], pad, lon_pad))
        hood = (frame.lat0, frame.lon0, frame.h0, bool(left_hand), keys)
        with self.lock:
            got = self._hoods.get(hood)
            if got is not None:
                self._hoods.move_to_end(hood)
                return got
        rows, polys, seen = [], [], set()
        for key in keys:
            feats = self.cell(key)
            if feats is None or not len(feats):
                continue
            cell_rows, cell_polys = self._cell_rows(feats, frame, left_hand)
            fids = set()
            for row, poly in zip(cell_rows, cell_polys):
                if row[0] in seen:
                    continue            # (a line is whole in every cell it crosses)
                fids.add(row[0])
                rows.append(row)
                polys.append(poly)
            seen |= fids
        got = Pavements(rows, polys)
        with self.lock:
            self._hoods[hood] = got
            while len(self._hoods) > NEIGHBOURHOODS:
                self._hoods.popitem(last=False)
        return got
