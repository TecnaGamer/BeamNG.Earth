"""Rounded intersection pavement, shared by terrain, paint and road tiles."""
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
import numpy as np
import junctions

_CACHE = OrderedDict()
_PENDING = {}
_LOCK = Lock()


def find(rows):
    # Roundabout circulation/approaches retain their own constrained geometry.
    valid = lambda sp: sp.kind in ('paved', 'cobble') and not (sp.roundabout or sp.roundabout_entry) and sp.cls != 'raceway'
    return junctions.find(rows, valid, 0., pavement=True)[0]


def source(cells, urban=None, widths=None, frame=None, left_hand=False):
    return tuple(_entry(cells, urban, widths, frame, left_hand)[:3])


def _entry(cells, urban=None, widths=None, frame=None, left_hand=False):
    import roads, geodesy
    cells = tuple(f for f in cells if f is not None and len(f))
    if not cells:
        return [None, [], [], []]
    if frame is None:
        lon, lat = cells[0].lonlat[0]
        frame = geodesy.EnuFrame(float(lat), float(lon), 0.)
    specs = [roads.road_specs(f, urban, widths, left_hand) for f in cells]
    memos = tuple(f.__dict__['_road_specs'] for f in cells)
    key = (tuple((id(f), id(memo)) for f, memo in zip(cells, memos)),
           frame.lat0, frame.lon0, frame.h0, urban, widths, bool(left_hand))
    with _LOCK:
        saved = _CACHE.get(key)
        if saved is not None:
            _CACHE.move_to_end(key)
            return saved[1]
        pending = _PENDING.get(key)
        owner = pending is None
        if owner:
            pending = _PENDING[key] = Future()
    if not owner:
        return pending.result()  # texture and material requests share the same build
    try:
        rows, corners = _build(cells, specs, frame)
        pads = []
        for c in corners:
            for poly in ([c['fill']] if c['fill'] is not None else []) + c.get('fills', []):
                la, lo, _ = frame.enu_to_geodetic(poly[:, 0], poly[:, 1], 0.)
                pads.append((la, lo))
        entry = [frame, rows, corners, pads]
        with _LOCK:
            if len(_CACHE) >= 16:
                _CACHE.popitem(last=False)
            _CACHE[key] = ((cells, memos), entry)  # retain identities for enrichment invalidation
            _PENDING.pop(key)
        pending.set_result(entry)
        return entry
    except BaseException as exc:
        with _LOCK:
            _PENDING.pop(key, None)
        pending.set_exception(exc)
        raise


def _build(cells, specs, frame):
    import roads
    rows, seen = [], set()
    for f, fspecs in zip(cells, specs):
        for i, sp in enumerate(fspecs):
            fid = str(f.ids[i])
            if sp is None or fid in seen:
                continue
            seen.add(fid)
            for p in roads.ground_parts(f, i):
                e, n, _ = frame.geodetic_to_enu(p[:, 1], p[:, 0], 0.)
                rows.append((fid, sp, np.c_[e, n, np.zeros(len(p))], float(p[:, 0].mean())))
    rows.sort(key=lambda row: (row[0], tuple(row[2][0])))
    return rows, find(rows)


def pads(cells, urban=None, widths=None, frame=None):
    return _entry(cells, urban, widths, frame)[3]


def tile(rows, box, draw_pavement):
    """Corner fill chunks, curved edge paint, and cuts on the original straight edges."""
    import roads, roadmarkings
    by_id = {fid: sp for fid, sp, _, _ in rows}
    chunks, cuts = [], {}
    for c in find(rows):
        polys = ([c['fill']] if c['fill'] is not None else []) + c.get('fills', [])
        if not polys:
            continue
        poly = np.concatenate(polys)
        x0, y0, x1, y1 = box
        if poly[:, 0].max() < x0 or poly[:, 0].min() > x1 or poly[:, 1].max() < y0 or poly[:, 1].min() > y1:
            continue
        if draw_pavement:
            look = roads._corner_look(c['arc'][len(c['arc']) // 2], rows)
            chunks.extend(dict(look, w=0., poly=p.tolist(), n=[]) for p in polys)
        sides = c.get('road_sides')
        if not sides:
            continue
        specs = [by_id[fid] for fid, _ in sides]
        if any(roads.has_sidewalks(sp) or sp.cls not in roads.EDGE_LINE_CLASSES for sp in specs):
            continue
        inset = [max(roads.EDGE_LINE['inset'], roads.edge_margin(sp, side)) for sp, (_, side) in zip(specs, sides)]
        arc = c['arc'].copy()
        # inward points into the roadside; paint is on the pavement side of it.
        arc[:, :2] -= c['inward'] * np.linspace(inset[0], inset[1], len(arc))[:, None]
        # Exact tangent normals keep the curved line on the straight edge at each end.
        for at, cut, (_, side) in zip((0, -1), c['edge_cuts'], sides):
            u = cut[1]
            normal = np.array([-u[1], u[0]]) * (1 if at == 0 else -1)
            arc[at, :2] = c['arc'][at, :2] - normal * inset[0 if at == 0 else 1]
        for side, cut in zip(sides, c['edge_cuts']):
            cuts.setdefault(side, []).append(cut)
        width = max(roadmarkings.profile(sp.state).solid for sp in specs)
        for p in roads.clip_polyline(arc, *box):
            chunks.append(roads._chunk(roads.EDGE_LINE, width, p))
    return chunks, cuts


def grade(plans, specs):
    """Support the new pavement at the adjoining roads' solved heights."""
    import grading
    rows = [(fid, sp, np.c_[p.xy, p.z], 0.) for (fid, p), sp in zip(plans, specs)]
    result = []
    for c in find(rows):
        if c['fill'] is None:
            continue
        arc, centre = c['arc'], c['fill'][0, :2]
        # The fan between the original square corner and the return arc must all
        # be supported; overlapping approach plans already grade its two sides.
        reach = float(junctions._seg_dist(centre[None], arc[:, :2])[0])
        p = grading.Plan(arc[:, :2], arc[:, 2], reach + 1., road_half=reach + .25)
        fid = 'corner:' + repr((sorted(c['road_sides']), tuple(np.round(centre, 1))))
        p.rid = grading.road_id(fid)  # the same corner in adjacent cells is one grading surface
        result.append((fid, p))
    return result


def clear_props(props, rows):
    """Keep generated ground-level props off the added corner pavement."""
    if not props:
        return props
    points = np.array([p[1:4] for p in props], float)
    blocked = np.zeros(len(points), bool)
    for c in find(rows):
        for poly in ([c['fill']] if c['fill'] is not None else []) + c.get('fills', []):
            near = np.flatnonzero((points[:, 0] >= poly[:, 0].min()) & (points[:, 0] <= poly[:, 0].max())
                                  & (points[:, 1] >= poly[:, 1].min()) & (points[:, 1] <= poly[:, 1].max())
                                  & (np.abs(points[:, 2] - poly[:, 2].mean()) < 3.))
            if not len(near):
                continue
            x, y = points[near, 0], points[near, 1]
            inside = np.zeros(len(near), bool)
            for a, b in zip(poly, np.roll(poly, -1, axis=0)):
                if abs(b[1] - a[1]) > 1e-9:
                    inside ^= ((a[1] > y) != (b[1] > y)) & (x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
            blocked[near] |= inside
    return [p for p, hit in zip(props, blocked) if not hit]
