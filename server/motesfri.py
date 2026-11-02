"""Centre barriers on Sweden's 2+1 roads (mötesfri väg, "mitträcke", in the field mostly a "vajerräcke"): the road is one
two-way OpenStreetMap way, lanes=3 with lanes:forward / lanes:backward alternating 2 / 1, so medians.py (two one-way
carriageways side by side) finds nothing there. This puts the cable barrier (guardrails.G_CABLE, medians.CABLE's row)
along the line markings.lane_plan puts between the directions.

Which ways, from all 164 two-way ways of väg 41 Varberg-Borås in OpenStreetMap (Overpass, 2026-11-01) and the lane
shards around Göteborg and Stockholm:
- A 2 / 1 split alone is mostly a junction approach: a way with turn:lanes is not a section, nor a short one (väg 41's
  sections are 272-2153 m, its untagged pockets 32-146 m; Overture may cut a way: its pieces count together).
- motorroad=yes is not used: none of the 139 3-lane 2 / 1 ways between Varberg, Borås and Skövde has it.
- Between two sections lie junctions and the 1+1 stretch where the 2-lane side changes over (VGU Råd 2022:003 7.1), 358-
  1331 m on väg 41. The barrier goes on along those (Krav 2022:001 7.1: "Mitträcke ska vara kontinuerligt förutom i
  korsningar"): a connector is every way on a straight-through chain of at most CONNECTOR_MAX_M between two sections,
  and the real junctions open it.

Known limits: a long climbing lane (stigningsfält) tagged like a section gets a barrier; NVDB can't settle that from
open data (Vägräcke, with its "Gemensamt mitträcke", is "Öppna data: Nej", Dataproduktspecifikation v12.0, 2021-11-02).
Bridge decks have none: only ground parts are looked at, as with medians.py's non-slab decks.
Overture merges the ways between two junctions into one segment, and a 2+1 road's sections alternate along it, so a Way
here is a stretch of a segment, one OpenStreetMap way (roads.lane_tag_parts). A segment's width is one for all its ways.

Tile independence, as medians.rs: modules lie on a grid of arc length along their way, a tile keeps those whose middle
is in it, and whether a way counts depends only on its own tags and the ways touching its ends, all inside REACH_M of
the tile. Python, not medians.rs: this is a line along one road and median_rows already makes its Forest rows.
"""
from collections import namedtuple
import math

import numpy as np

import fast
import markings
import medians

COUNTRIES = {"SE"}
CLASSES = {"trunk", "primary", "secondary", "tertiary"}   # (väg 132, väg 100, O 650 ...: 25 of the 128 km are the last two)
MIN_KMH = 80.0              # below this a 2 / 1 split is a town's or a junction's ...
RURAL_MIN_KMH = 70.0        # ... outside a town this (E 20 and väg 69 have 2+1 stretches at 70)
MIN_WAY_M = 200.0           # m of a way's 2 / 1 split that is a section: see above; VGU Krav 2022:001 7.1's shortest
                            # omkörningssträcka is 900 m, but OpenStreetMap ways are cut at bridges and junctions
CONNECTOR_MAX_M = 1100.0    # m: the longest chain between two sections (väg 41's 15 gaps: 358-1095 m, then one of 1331)
# m around a tile its ways are read: a connector's far section is in them, whole enough to measure MIN_WAY_M
REACH_M = CONNECTOR_MAX_M + MIN_WAY_M + 40.0
JOIN_M = 1.0                # m between two ways' ends that touch
STRAIGHT_COS = 0.7          # a chain goes straight on through a joint (within 45 degrees), not round into a side road
# VGU Krav 2022:001 7.3.2.2.7 "Sidoförskjutning av räcke ska utformas med förskjutning 1:20 eller flackare": m along per
# m aside. The shift is a smoothstep, whose steepest slope is 1.5 x its mean, so 1.5 x that long stays within 1:20.
SHIFT_RATIO = 20.0
SMOOTHSTEP_PEAK = 1.5
# m of a mapped barrier from ours that stands for it: a mapped line down the road's middle is a third of the driving
# width from ours (1.83 m on an 11 m carriageway), a side rail 3.6 m or more
MAPPED_REACH_M = 3.0
MIN_RUN = 10                # modules a run has at least, as medians.rs (no 12 m stub between two openings)
WINDOW_M = 100.0            # m past the tile a run's length is judged (past MIN_RUN's 30 m, so tiles agree)
# roads that open the barrier: junctions. Service roads and tracks don't: VGU Krav 2022:001 keeps the barrier
# continuous except in junctions and has minor accesses withdrawn or made right-in right-out ("Öppningar i mitträcke
# bör dock undvikas")
OPEN_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential"}
# ... and these only in a junction area. A left turn across a 2+1 road takes a turn lane (VGU Krav 2022:001, type C or
# larger at the larger crossroads), so one meeting a section, which has none, is right-in right-out (a 15 m opening at a
# gravel road otherwise, as at väg 41's 57.47866, 12.63411: no turn lane there on the aerial photo, 2026-11-02)
SECTION_KEEPS_SHUT = {"unclassified", "residential"}

NOT_TURNS = {"through", "none", "", "merge_to_left", "merge_to_right"}     # values of a turn:lanes tag
Way = namedtuple("Way", "fid spec tags xy key")     # key: OpenStreetMap's way (its pieces are measured together)
Piece = namedtuple("Piece", "way offset section")   # offset: m left of the geometry; section: not a connector


def _lanes(spec, tags):
    """The way's own lane count where its tags say, else the segment's (of its longest way)."""
    return markings._count((tags or {}).get("lanes")) or spec.lanes


def _has_turn_lanes(tags):
    """A turn lane in tags; merge_to_left / merge_to_right are a section's own lane drop, not a junction's."""
    return any(k.startswith("turn:lanes") and set(str(v).replace(";", "|").split("|")) - NOT_TURNS
               for k, v in tags.items())


def _fast(spec):
    return spec.kmh is not None and spec.kmh >= (MIN_KMH if spec.urban else RURAL_MIN_KMH)


def _base(spec):
    """Is this road the kind a 2+1 road is: a paved two-way main road, fast enough (_fast), no ramp."""
    return (spec is not None and spec.cls in CLASSES and spec.kind == "paved" and not spec.oneway and not spec.link
            and _fast(spec))


def is_section(spec, tags):
    """Tags of a 2+1 section (a junction's turn pockets aside; length: sections())."""
    tags = tags or {}
    if not _base(spec) or _lanes(spec, tags) not in (None, 3) or _has_turn_lanes(tags):
        return False
    split = {markings._count(tags.get(k)) for k in ("lanes:forward", "lanes:backward")}
    return split == {1, 2} and not markings._count(tags.get("lanes:both_ways"))


def _carriageway(spec):
    """Is this one carriageway of a 2+1 road drawn as two one-way roads (OpenStreetMap's wiki, and 5200 km of Sweden's
    against the 128 of a two-way way: the median barrier is medians.py's there, the lane drops still ours)?"""
    return (spec is not None and spec.cls in CLASSES and spec.kind == "paved" and bool(spec.oneway) and not spec.link
            and _fast(spec))


def wanted(country, roads_near):
    """Can tile's ground carriageways roads_near hold such a road? (the tile's other work is not to be slowed)"""
    return (country or "").upper() in COUNTRIES and any(_base(item[1]) or _carriageway(item[1]) for item in roads_near)


def _length(xy):
    return float(np.hypot(*np.diff(xy[:, :2], axis=0).T).sum())


def _meets(xy, box):
    """Does polyline xy's bounding box meet box (x0, y0, x1, y1)?"""
    return (xy[:, 0].max() >= box[0] and xy[:, 0].min() <= box[2] and
            xy[:, 1].max() >= box[1] and xy[:, 1].min() <= box[3])


def _touches(a, b):
    return float(np.hypot(*(a - b))) <= JOIN_M


def centre_offset(way, left_hand=False):
    """m left of way's geometry of the line between its directions (markings.lane_plan's)."""
    import roads
    drive, lanes = roads.lane_layout(way.spec)
    lanes = _lanes(way.spec, way.tags) or lanes
    for off, role, _how in markings.lane_plan(way.tags, True, drive, lanes, left_hand) or ():
        if role == "centre":
            return off
    return 0.0


def _leaving(xy, k):
    """Unit direction out of way xy at end k (0 or -1)."""
    d = xy[k, :2] - xy[1 if k == 0 else -2, :2]
    return d / max(float(np.hypot(*d)), 1e-9)


def _connectors(ways, run):
    """The ways (ids) on a straight-through chain of at most CONNECTOR_MAX_M between two ways of run."""
    runs = {id(w) for w in run}
    found = set()
    for w in run:
        for k in (0, -1):
            todo = [(w, k, 0.0, ())]
            while todo:
                cur, end, length, path = todo.pop()
                out = _leaving(cur.xy, end)
                for n in ways:
                    if n is cur or id(n) in path:
                        continue
                    for j in (0, -1):
                        if not _touches(cur.xy[end, :2], n.xy[j, :2]) or float(np.dot(out, -_leaving(n.xy, j))) \
                                < STRAIGHT_COS:
                            continue
                        if id(n) in runs:
                            if n is not w:
                                found.update(path)
                            continue
                        total = length + _length(n.xy)
                        if total <= CONNECTOR_MAX_M:
                            todo.append((n, -1 - j, total, path + (id(n),)))
    return found


def sections(ways, left_hand=False):
    """The Pieces of ways that carry a barrier: the sections and the connectors between them."""
    base = [w for w in ways if _base(w.spec)]
    run = [w for w in base if is_section(w.spec, w.tags)]
    total = {}
    for w in run:
        total[w.key] = total.get(w.key, 0.0) + _length(w.xy)
    run = [w for w in run if total[w.key] >= MIN_WAY_M]
    links = _connectors(base, run)
    sect = {id(w) for w in run}
    return [Piece(w, centre_offset(w, left_hand), id(w) in sect) for w in base if id(w) in links or id(w) in sect]


def end_offsets(pieces):
    """[(offset at the start, offset at the end)] per piece: the mean of its own and the piece it touches there (each
    in its own geometry's left: a way drawn the other way round has its left on our right), so both shift to one point.
    A loose end keeps its own."""
    out = []
    for p in pieces:
        got = []
        for k in (0, -1):
            e = p.way.xy[k, :2]
            mate = None
            for q in pieces:
                if q.way is p.way:
                    continue
                for j in (0, -1):
                    if _touches(e, q.way.xy[j, :2]):
                        # a start meeting an end: drawn the same way; two starts or two ends: opposite
                        o = q.offset if j != k else -q.offset
                        if mate is None or (q.way.key, j) < mate[0]:     # (a fork: the same pick in every tile)
                            mate = ((q.way.key, j), o)
            got.append(p.offset if mate is None else (p.offset + mate[1]) / 2)
        out.append(tuple(got))
    return out


def offset_along(s, length, target, start, end):
    """The offset at arc lengths s: target, shifting to start / end at the way's ends (see SHIFT_RATIO)."""
    def ramp(d, value):
        span = abs(value - target) * SHIFT_RATIO * SMOOTHSTEP_PEAK
        span = min(span, length / 2)
        if span <= 0:
            return np.zeros_like(d)
        t = np.clip(d / span, 0.0, 1.0)
        return (value - target) * (1.0 - t * t * (3.0 - 2.0 * t))
    return target + ramp(np.asarray(s), start) + ramp(length - np.asarray(s), end)


def _stations(xy, offsets_of):
    """The barrier line of a way: (k, 2) points on its arc length grid, about medians.MODULE_M apart (never more: the
    models overlap by a hair rather than leave a gap), offsets_of(s, length) m left of it."""
    xy = np.asarray(xy, np.float64)[:, :2]
    seg = np.hypot(*np.diff(xy, axis=0).T)
    keep = np.r_[True, seg > 1e-6]
    xy, seg = xy[keep], np.hypot(*np.diff(xy[keep], axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    length = float(cum[-1])
    n = max(1, int(math.ceil(length / medians.MODULE_M)))
    s = np.linspace(0.0, length, n + 1)
    u = np.diff(xy, axis=0) / seg[:, None]
    # vertices' directions, their two segments' mean, blended along each segment: no jump at a vertex (medians.rs)
    v = np.vstack([u[:1], u[:-1] + u[1:], u[-1:]])
    v /= np.maximum(np.hypot(*v.T), 1e-9)[:, None]
    p = np.c_[np.interp(s, cum, xy[:, 0]), np.interp(s, cum, xy[:, 1])]
    t = np.c_[np.interp(s, cum, v[:, 0]), np.interp(s, cum, v[:, 1])]
    t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
    return p + np.c_[-t[:, 1], t[:, 0]] * np.asarray(offsets_of(s, length))[:, None]


def _distance(points, line):
    """Each of points' (m, 2) distance m to polyline line (k, 2)."""
    a, b = line[:-1], line[1:]
    ab = b - a
    t = np.clip((((points[:, None] - a[None]) * ab[None]).sum(2)) / np.maximum((ab * ab).sum(1), 1e-12)[None], 0, 1)
    d = points[:, None] - (a[None] + t[..., None] * ab[None])
    return np.hypot(d[..., 0], d[..., 1]).min(1)


def _keep_runs(ok, loose):
    """ok without its runs of fewer than MIN_RUN modules; loose[k]: module k is outside the judged window, so a run
    touching it goes on past what this tile knows and stands."""
    out = ok.copy()
    i, n = 0, len(ok)
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j < n and ok[j]:
            j += 1
        if j - i < MIN_RUN and not loose[i:j].any():
            out[i:j] = False
        i = j
    return out


def modules(ways, box, left_hand=False, mapped_rows=()):
    """(k, 11) medians.rs median_modules' layout, cable, for ways (Way) near tile box (x0, y0, x1, y1)."""
    pieces = sections(ways, left_hand)
    if not pieces:
        return np.zeros((0, 11), np.float64)
    x0, y0, x1, y1 = box
    own = {id(p.way) for p in pieces}
    near = (x0 - WINDOW_M, y0 - WINDOW_M, x1 + WINDOW_M, y1 + WINDOW_M)
    pad = (near[0] - 30, near[1] - 30, near[2] + 30, near[3] + 30)
    others = [w for w in ways if id(w) not in own and w.spec.cls in OPEN_CLASSES and len(w.xy) >= 2
              and _meets(w.xy, pad)]
    mapped = medians._mapped_segments(mapped_rows)
    out = []
    for p, (start, end) in zip(pieces, end_offsets(pieces)):
        xy = p.way.xy[:, :2]
        if not _meets(xy, near):
            continue
        pts = _stations(xy, lambda s, length, t=p.offset, a=start, b=end: offset_along(s, length, t, a, b))
        a, b = pts[:-1], pts[1:]
        mid = (a + b) / 2
        inside = (mid[:, 0] >= near[0]) & (mid[:, 0] <= near[2]) & (mid[:, 1] >= near[1]) & (mid[:, 1] <= near[3])
        ok = inside.copy()
        for w in others:
            if p.section and w.spec.cls in SECTION_KEEPS_SHUT:
                continue
            ok &= _distance(mid, np.asarray(w.xy, np.float64)[:, :2]) > float(w.spec.width) / 2 + medians.OPENING_M
        if len(mapped):
            for sa, sb in zip(mapped[:, :2], mapped[:, 2:]):
                ok &= _distance(mid, np.stack([sa, sb])) > MAPPED_REACH_M
        ok = _keep_runs(ok, ~inside) & (mid[:, 0] >= x0) & (mid[:, 0] < x1) & (mid[:, 1] >= y0) & (mid[:, 1] < y1)
        for k in np.flatnonzero(ok):
            rank = (k * 0.6180339887) % 1.0     # (median_modules': an even spread, the same in every tile)
            out.append((medians.CABLE, 0.0, *a[k], *b[k], rank, 0.0, 0.0, np.nan, np.nan))
    return np.asarray(out, np.float64).reshape(-1, 11)


def _ground_stretches(feats, i):
    """(lon/lat polyline, way id, tags) of feature i's ground parts, cut where its OpenStreetMap way changes."""
    import roads
    ground = roads.ground_ranges(roads.level_ranges(feats, i))
    out = []
    for start, end, way, tags in roads.lane_tag_parts(feats, i):
        for a, b in ground:
            lo, hi = max(a, start), min(b, end)
            if hi - lo <= roads.MIN_PART:
                continue
            for part in feats.parts(i):
                for ring in part:
                    if len(ring) >= 2:
                        r = ring if (lo, hi) == (0.0, 1.0) else roads.sub_polyline(ring, lo, hi)
                        if len(r) >= 2:
                            out.append((r, way, tags))
    return out


def _ways(frame, cells, box, urban, widths, left_hand):
    """Way of every ground stretch of the roads in cells within REACH_M of box. A feature is whole in every cell it
    crosses, so its ends are the same wherever it is read."""
    import roads
    x0, y0, x1, y1 = box
    r = REACH_M
    out, seen = [], set()
    for feats in cells:
        if feats is None or not len(feats):
            continue
        specs = roads.road_specs(feats, urban, widths, left_hand)
        boxes = feats.boxes(frame)
        for i in range(len(feats)):
            fid = str(feats.ids[i])
            if specs[i] is None or fid in seen:
                continue
            seen.add(fid)
            fb = boxes[i]
            if fb[2] < x0 - r or fb[0] > x1 + r or fb[3] < y0 - r or fb[1] > y1 + r:
                continue
            for k, (ring, way, tags) in enumerate(_ground_stretches(feats, i)):
                e, n, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.0)
                out.append(Way(f"{fid}:{k}", specs[i], tags, np.c_[e, n], way or fid))
    return out


# A section's lane drop: M20 "före plats där körfält upphör", in the middle of the lane that ends, the first arrow 2.5 m
# before its lane line stops (VGU 2024 12.2.1.5.3, K237774-6, figure 12.2.1.5.3-1), m between the arrows as its table
# 12.14 (at 90 km/h and up its last column: the table stops there, 2+1 roads run at 100 too)
M20_FIRST_M = 2.5
M20_GAPS_80, M20_GAPS_90 = (20.0, 40.0, 60.0), (25.0, 50.0, 75.0, 100.0)


def _overtaking(way):
    """(m left of way's geometry of the lane that ends, the two lanes' travel along it: +1 / -1), None if not told."""
    import roads
    drive, lanes = roads.lane_layout(way.spec)
    plan = markings.lane_plan(way.tags, True, drive, _lanes(way.spec, way.tags) or lanes, False) or ()
    centre, lane = ([off for off, role, _ in plan if role == r] for r in ("centre", "lane"))
    if len(centre) != 1 or len(lane) != 1:
        return None
    return (centre[0] + lane[0]) / 2, 1 if lane[0] < centre[0] else -1      # (traffic on the right: Sweden's)


def _back_along(way, offset, travel, d):
    """The point d m before the end of way in its travel direction, offset m left of its geometry, and its direction of
    travel; None past the way's start, with the m left over."""
    xy = way.xy[:, :2] if travel > 0 else way.xy[::-1, :2]
    seg = np.hypot(*np.diff(xy, axis=0).T)
    cum = np.r_[0.0, np.cumsum(seg)]
    if d > cum[-1] or cum[-1] <= 0.0:
        return None, d - cum[-1]
    at = cum[-1] - d
    k = min(int(np.searchsorted(cum, at, side="right")) - 1, len(seg) - 1)
    t = (xy[k + 1] - xy[k]) / max(seg[k], 1e-9)
    p = xy[k] + t * (at - cum[k])
    return p + np.array([-t[1], t[0]]) * offset * travel, t


def lane_drops(ways):
    """[(tip of an arrow (2,), direction of travel (2,))] of the M20 arrows before the lane drops of ways' sections,
    following the two lanes back past a joint in the way. Every tile sees the same list (see the module's header)."""
    run = []
    for p in sections(ways):
        over = _overtaking(p.way) if p.section else None
        if over is not None:
            run.append((p.way, over[0], over[1]))
    for w in ways:
        if _carriageway(w.spec) and _lanes(w.spec, w.tags) == 2:    # the left lane ends: its middle, left of travel
            travel = -1 if (w.spec.dir or 1) < 0 else 1
            run.append((w, travel * w.spec.width / 4, travel))
    end = lambda r: r[0].xy[-1 if r[2] > 0 else 0, :2]      # noqa: E731
    start = lambda r: r[0].xy[0 if r[2] > 0 else -1, :2]    # noqa: E731
    out = []
    for r in run:
        e = end(r)
        if any(q is not r and _touches(e, start(q)) for q in run):
            continue                                        # the two lanes go on
        if r[0].spec.oneway:                                # one lane on, not a fork, not a ring or a slip road
            on = [w for w in ways if w is not r[0] and w.spec.oneway
                  and _touches(e, w.xy[-1 if (w.spec.dir or 1) < 0 else 0, :2])]
            if len(on) != 1 or not _carriageway(on[0].spec) or _lanes(on[0].spec, on[0].tags) != 1:
                continue
        elif not any(w is not r[0] and (_touches(e, w.xy[0, :2]) or _touches(e, w.xy[-1, :2])) for w in ways):
            continue                                        # (nothing read past it: don't guess)
        d, here = M20_FIRST_M, r
        gaps = M20_GAPS_90 if (r[0].spec.kmh or 0) >= 90.0 else M20_GAPS_80    # (70 is table 12.14's 80 column too)
        for gap in (0.0, *gaps):
            d += gap
            while here is not None:
                got, rest = _back_along(here[0], here[1], here[2], d)
                if got is not None:
                    out.append((got, rest))
                    break
                d = rest
                here = next((q for q in run if q is not here and _touches(start(here), end(q))), None)
    return out


def drop_marks(ways, box):
    """The road chunks of box's M20 arrows (by an arrow's tip: lane_drops)."""
    import geotiles
    import roads
    return [roads.mark_poly(q) for tip, t in lane_drops(ways) if geotiles.box_owns(box, tip[0], tip[1])
            for q in markings.m20(tip, t)]


def tile_rows(frame, cells, box, elev, urban=None, widths=None, left_hand=False, mapped_rows=(), ways=None):
    """Forest rows (guardrails.forest_rows' layout and GROUPS) of the centre barriers in road tile box: cells the
    decoded road cells within REACH_M of it, elev(lat, lon) the ground, urban and widths as roads.build_tile's,
    mapped_rows the tile's rows so far (none of ours along a mapped barrier); ways: _ways', if read already."""
    if ways is None:
        ways = _ways(frame, cells, box, urban, widths, left_hand)
    mods = modules(ways, box, left_hand, mapped_rows)
    if not len(mods):
        return []
    fast.require(99, "median_rows")
    ends = np.concatenate([mods[:, 2:4], mods[:, 4:6]])
    la, lo, _ = frame.enu_to_geodetic(ends[:, 0], ends[:, 1], np.zeros(len(ends), np.float64))
    z = np.asarray(frame.geodetic_to_enu(la, lo, np.asarray(elev(la, lo), np.float64))[2], np.float64)
    n = len(mods)
    rows = fast._ef.median_rows(np.ascontiguousarray(mods), np.ascontiguousarray(z[:n]), np.ascontiguousarray(z[n:]))
    return np.round(np.asarray(rows, np.float64), 4).tolist()
