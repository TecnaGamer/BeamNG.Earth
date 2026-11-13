"""Mapped roadside bays and residential parking, using the existing tile parking rows."""

import math

import numpy as np
import dmath
import dnp

import furniture
import mapgeom
import parking
import polygon
import streetfront

STREET_SIDE = {"street_side", "lane", "half_on_kerb", "on_kerb"}
ORDINARY = {"", "yes", "normal"}
CAR_LENGTH, CAR_WIDTH = 5.0, 2.20
MIN_LENGTH, MIN_WIDTH = 4.65, 2.0
MAX_DRIVE_M = 40.
MAX_BAYS = 160
FOOTPRINT = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]])


def capacity(tags):
    try:
        return max(0, int(tags.get("capacity", 0)))
    except (TypeError, ValueError):
        return 0


def bay_kind(feats, i, tags):
    """Which polygons have their own layout, distinct from the rows of a large parking lot."""
    if feats.kind[i] != mapgeom.POLYGON:
        return None
    cls = str(feats.attrs["class"][i])
    if cls == "parking_space":
        return "space"
    if cls == "parking":
        if tags.get("parking") in STREET_SIDE:
            return "street"
        if tags.get("access") == "private" and (tags.get("parking") == "driveway" or 0 < capacity(tags) <= 4):
            return "private"
    return None


def _near_street(roads, point):
    best = None
    for _, spec, line, _ in roads:
        pos, tangent = furniture._at(line, furniture._project(line, point[None]))
        gap = float(dnp.norm(point - pos[0, :2])) - spec.width / 2
        if best is None or gap < best[0]:
            best = gap, pos[0, :2], tangent[0], spec
    return best


def _box(centre, heading, length, width):
    sideways = np.array([-heading[1], heading[0]])
    return centre + dnp.matmul(FOOTPRINT, np.stack((heading * length / 2, sideways * width / 2)))


def _candidates(outline, holes, kind, tags, streets, left_hand):
    import buildings

    count = capacity(tags)
    centre, axis, half_len, half_width = buildings.oriented_box(outline)
    if half_len < MIN_LENGTH / 2 or half_width < MIN_WIDTH / 2 - .02:
        return []
    street = _near_street(streets, centre)
    if kind == "street" and (street is None or street[0] > 12.):
        return []
    # Fit the entire footprint. A narrow parallel bay is a single row along the pavement,
    # even if its polygon is too shallow for the parking-lot grid.
    facing = axis
    if street is not None and kind != "private":
        _, pos, tangent, spec = street
        direction = tangent if not spec.oneway or (spec.dir or 0) >= 0 else -tangent
        away = centre - pos
        sideways = np.array([-tangent[1], tangent[0]])
        if dnp.dot(sideways, away) < 0:
            sideways = -sideways
        along = tangent if dnp.dot([tangent[1], -tangent[0]], sideways) > 0 else -tangent
        if left_hand:
            along = -along
        if spec.oneway:
            along = direction
        depth = float(np.ptp(dnp.matmul(outline, sideways)))
        orientation = tags.get("orientation") or tags.get("parking:orientation")
        if orientation is None:
            orientation = "perpendicular" if (kind == "street" and depth >= 4.8 and
                                                (half_width >= 2.3 or abs(dnp.matmul(axis, sideways)) > .8)) else "parallel"
        if orientation == "perpendicular":
            facing = sideways
        elif orientation == "diagonal":
            facing = (sideways + along) / math.sqrt(2)
        else:
            facing = along if abs(dnp.matmul(axis, tangent)) < .65 else axis * (1. if dnp.matmul(axis, along) >= 0 else -1.)
        # Parking rows run along the street. A short curved bay can keep its own bearing.
        if orientation != "parallel" and (abs(dnp.matmul(axis, tangent)) >= .75 or count == 1):
            axis = tangent
    elif kind == "private" and street is not None and dnp.dot(facing, centre - street[1]) < 0:
        facing = -facing
    axes = np.array([axis, [-axis[1], axis[0]]])
    limits = dnp.matmul(outline - centre, axes.T)
    lo, hi = limits.min(axis=0), limits.max(axis=0)
    results = []
    usable = float(hi[0] - lo[0])
    for length, width in ((CAR_LENGTH, CAR_WIDTH), (MIN_LENGTH, MIN_WIDTH)):
        dimensions = np.ptp(dnp.matmul(_box(np.zeros(2), facing, length, width), axes.T), axis=0)
        if (hi - lo < dimensions - .02).any():
            continue
        n = min(MAX_BAYS, max(1, int((usable + .02) / dimensions[0])), count or MAX_BAYS)
        if kind in {"space", "private"}:
            n = min(n, count or 1)
        for offset in (np.arange(n) - (n - 1) / 2) * (usable / n):
            q = centre + axis * ((lo[0] + hi[0]) / 2 + offset) + axes[1] * ((lo[1] + hi[1]) / 2)
            rect = _box(q, facing, length, width)
            check = q + .98 * (rect - q)
            if polygon.contains_points(outline, check).all() and not streetfront._inside(check, holes).any():
                results.append((q, facing, rect, kind, tags))
        if results:
            break
    return results


def _driveway_candidates(driveways, streets, homes, mapped):
    import roads
    if not homes or not streets:
        return []
    result = []
    for _, spec, line in driveways:
        if spec.width < CAR_WIDTH + .25:
            continue
        length = furniture._arclen(line)[-1]
        if not CAR_LENGTH + 3.5 <= length <= MAX_DRIVE_M:
            continue
        first = _near_street(streets, line[0, :2])[0] <= .9
        last = _near_street(streets, line[-1, :2])[0] <= .9
        if first == last:
            continue
        if last:
            line = line[::-1]
        end = line[-1, :2]
        if min(roads._seg_dist_min(np.vstack((ring, ring[:1])), end) for ring in homes) > 11.:
            continue
        station = length - CAR_LENGTH / 2 - .65
        p, d = furniture._at(line, np.array([station]))
        q, h = p[0, :2], d[0]
        if streetfront._inside(q[None], mapped).any():
            continue
        # The car needs a straight piece of driveway, clear of its mouth and terminal wall.
        p2, _ = furniture._at(line, np.array([station - 2.5, station + 2.5]))
        if dnp.norm((p2[:, :2] - q) - np.array([[-2.5], [2.5]]) * h, axis=1).max() > .45:
            continue
        result.append((q, h, _box(q, h, CAR_LENGTH, CAR_WIDTH), "private", {}))
    return result


def _paint(rect, kind, tags, centre, streets):
    if kind != "street" and not (kind == "space" and tags.get("markings") == "yes"):
        return []
    if tags.get("markings") == "no" or tags.get("surface") in parking.LOT_UNPAVED:
        return []
    edges = list(zip(rect, np.roll(rect, -1, axis=0)))
    if kind == "street" and streets:
        _, pos, _, _ = _near_street(streets, centre)
        near = min(range(4), key=lambda i: float(dnp.norm((edges[i][0] + edges[i][1]) / 2 - pos)))
        a, b = edges.pop(near)
        if dnp.norm(b - a) >= MIN_LENGTH - .05:
            # A broken line on the traffic side of a parallel row.
            edges.extend((a + (b - a) * start, a + (b - a) * (start + .13)) for start in (0., .22, .44, .66, .87))
            edges.pop((near + 1) % 4 if near < 2 else near - 2)
    return [(tuple(a), tuple(b)) for a, b in edges]


def areas(frame, box, features):
    """The parking polygons near the tile, including ones the ordinary lot grid covers."""
    seen = set()
    x0, y0, x1, y1 = box
    for feats in features:
        if feats is None or not len(feats) or "class" not in feats.attrs:
            continue
        bounds = feats.boxes(frame)
        near = ((bounds[:, 2] >= x0 - 6.) & (bounds[:, 0] <= x1 + 6.) &
                (bounds[:, 3] >= y0 - 6.) & (bounds[:, 1] <= y1 + 6.))
        for i in np.flatnonzero(near & (feats.kind == mapgeom.POLYGON) &
                                np.isin(feats.attrs["class"], ["parking", "parking_space"])):
            fid = str(feats.ids[i])
            if fid in seen:
                continue
            seen.add(fid)
            tags = streetfront.tags_of(feats, i)
            kind = bay_kind(feats, i, tags)
            for part in feats.parts(i):
                if not part or len(part[0]) < 4:
                    continue
                rings = [np.column_stack(mapgeom.enu_xy(feats, frame, p)) for p in part]
                yield kind, tags, rings, parking.lot_unpaved(feats, i)


# A square with street-side parking round it (amenity=parking + parking=street_side between the street and a mapped
# pedestrian area): the parked cars' row abuts the square, the square is the walk; no sidewalk is drawn along the street
# over the bays (Broadway Square at Fells Point, a sidewalk over its parking rows: the user's report 14, 2026-11-12).
# A bay within PLAZA_TOUCH_M of a pedestrian area cuts the generated sidewalks over it (roads.build_tile's walk_cuts).
PLAZA_TOUCH_M = 1.5
PLAZA_CUT_PAD_M = 0.5       # m the cut reaches past the bay's long sides


def plaza_bay_cuts(frame, tx, ty, md):
    """[(axis (2, 2), half width)] of road tile (tx, ty)'s street-side bays that abut a pedestrian area (squares): the
    walk cuts of build_tile. md: the map data (land_use, infrastructure)."""
    import roads
    import surface
    box = roads.tile_bbox(tx, ty)
    try:
        layers = [md.cell(key, layers=("land_use", "infrastructure"), kind="demand").layers
                  for key in roads.cells_of_tile(frame, tx, ty, 20.0)]
    except (OSError, RuntimeError, ValueError):
        return []
    near = (box[0] - 20.0, box[1] - 20.0, box[2] + 20.0, box[3] + 20.0)
    squares, seen = [], set()
    for lay in layers:
        f = lay.get("land_use")
        if f is None or not len(f):
            continue
        m = surface._plazas(f)
        if m is None:
            continue
        fb = f.boxes(frame)
        for i in np.flatnonzero(m):
            if str(f.ids[i]) in seen or fb[i, 2] < near[0] or fb[i, 0] > near[2] or fb[i, 3] < near[1] or \
                    fb[i, 1] > near[3]:
                continue
            seen.add(str(f.ids[i]))
            for part in f.parts(i):
                if part and len(part[0]) >= 4:
                    squares.append(np.column_stack(mapgeom.enu_xy(f, frame, part[0])))
    if not squares:
        return []
    bays = [rings[0] for kind, _t, rings, _u in areas(frame, near, [lay.get("infrastructure") for lay in layers])
            if kind == "street" and len(rings[0]) >= 4]
    out = []
    for ring in bays:
        ring = np.asarray(ring, np.float64)[:, :2]
        lo, hi = ring.min(0) - PLAZA_TOUCH_M, ring.max(0) + PLAZA_TOUCH_M
        touch = False
        for sq in squares:
            if (sq.max(0) < lo).any() or (sq.min(0) > hi).any():
                continue
            d = roads._on_carriageways(ring, {0: (np.vstack([sq, sq[:1]]), PLAZA_TOUCH_M)}, 0.0)
            if d.any() or polygon.contains_points(sq, ring).any():
                touch = True
                break
        if not touch:
            continue
        c = ring.mean(0)
        d = ring - c
        _w, vec = dnp.eigh(dnp.matmul(d.T, d))      # (the bay's long axis: its points' principal direction)
        u = np.asarray(vec[:, -1], np.float64)
        v = np.array([-u[1], u[0]])
        a, b = dnp.matmul(d, u), dnp.matmul(d, v)
        out.append((np.array([c + u * a.min(), c + u * a.max()]), float(b.max() - b.min()) / 2 + PLAZA_CUT_PAD_M))
    return out


def adjust_walks(walks, bays, protected, barriers):
    """Leave room for mapped bays in inferred walks. Surveyed sidewalk lines keep their geometry."""
    import roads
    import sidewalks

    bays = [rings[0] for kind, _, rings, _ in bays if kind == "street"]
    if not bays:
        return False
    wall = streetfront._walls(protected, barriers)
    moved = False
    for wk in walks:
        if ("pts" not in wk or wk.get("mapped") or wk.get("median") or wk.get("detached")
                or not wk.get("europe") or wk.get("ramps")):
            continue
        original = np.asarray(wk["pts"], np.float64)
        line = roads.densify(original, .9)
        _, out = sidewalks.run_geometry(line, wk["side"], wk["w"])
        norm = out / np.maximum(1e-9, dnp.norm(out, axis=1))[:, None]
        stations = np.arange(-wk["w"] / 2 + .12, wk["w"] / 2 + 4.8, .25)
        probes = line[:, None, :2] + stations[None, :, None] * norm[:, None, :]
        inside = streetfront._inside(probes.reshape(-1, 2), bays).reshape(probes.shape[:2])
        hit = inside[:, stations <= wk["w"] / 2].any(axis=1)
        if not hit.any():
            continue
        # A smooth lead-in avoids a sidewalk kink at either end of a bay.
        raw = np.where(hit, np.where(inside, stations, -np.inf).max(axis=1) + wk["w"] / 2 + .22, 0.)
        arc = furniture._arclen(line)
        shifted = np.maximum(0., (raw[hit][None] - .7 * np.abs(arc[:, None] - arc[hit][None])).max(axis=1))
        back = line[:, :2] + norm * (wk["w"] / 2)
        clearance = wall.rays(back, norm, 6.)
        shifted = np.where(np.isfinite(clearance), np.minimum(shifted, np.maximum(0., clearance - .15)), shifted)
        line[:, :2] += shifted[:, None] * norm
        wk["pts"] = line
        wk["bay_shifted"] = bool((shifted > .01).any())
        moved = moved or wk["bay_shifted"]
    return moved


def tile_layout(frame, box, features, carriageways, driveways, walks, buildings, homes, barriers, green, z_at,
                left_hand=False, bays=None):
    """Same accepted rectangles feed both the markings and gameplay_parking."""
    import europewalks
    import roads

    streets = [(fid, sp, line, lon) for fid, sp, line, lon in carriageways
               if sp.cls in europewalks.STREETS and sp.kind in roads.BUILT and not sp.link]
    street_cuts = [(line[:, :2], max(.5, (sp.width - roads.parking_width(sp)) / 2 - .28))
                   for _, sp, line, _ in streets]
    walk_cuts = [(np.asarray(w["pts"])[:, :2], max(.3, w["w"] / 2 - .08))
                 for w in walks if "pts" in w]
    cuts = street_cuts + walk_cuts
    road_blocks = roads._blocker_segments(cuts) if cuts else None
    blocked = list(buildings) + list(green)
    walls = streetfront._walls(blocked, barriers)
    bays = list(areas(frame, box, features)) if bays is None else bays
    mapped = [rings[0] for _, _, rings, _ in bays]
    ordinary_lots = [rings[0] for kind, _, rings, _ in bays if kind is None]
    candidates = []
    for kind, tags, (ring, *holes), unpaved in bays:
        if kind is None or tags.get("parking_space", "") not in ORDINARY or tags.get("access") == "no":
            continue
        # Ordinary lots already provide their own rows. Standalone mapped spaces can add a spot.
        if not unpaved and not (kind == "space" and streetfront._inside(ring.mean(axis=0)[None], ordinary_lots).any()):
            candidates.extend(_candidates(ring, holes, kind, tags, streets, left_hand))
    candidates.extend(_driveway_candidates(driveways, streets, homes, mapped))
    candidates = [c for c in candidates if box[0] - CAR_LENGTH <= c[0][0] <= box[2] + CAR_LENGTH and
                  box[1] - CAR_LENGTH <= c[0][1] <= box[3] + CAR_LENGTH]
    if not candidates:
        return [], []
    rects = np.array([c[2] for c in candidates])
    ok = ~streetfront._road_overlap(rects, road_blocks)
    if blocked or barriers:
        ok &= ~streetfront._green_overlap(rects, blocked, walls, barriers)
    probes = np.concatenate((rects, np.array([c[0] for c in candidates])[:, None, :]), axis=1)
    ground = np.asarray(z_at(probes[..., 0].ravel(), probes[..., 1].ravel())).reshape(-1, 5)
    ok &= np.isfinite(ground).all(axis=1) & (np.ptp(ground[:, :4], axis=1) < .65)
    rows, segs, occupied = [], set(), []
    for (q, h, rect, kind, tags), keep, zz in zip(candidates, ok, ground[:, -1]):
        if not keep or any(dnp.norm(q - p) < 2. for p in occupied):
            continue
        occupied.append(q)
        if parking._owns(box, *q):
            length = float(dnp.norm(rect[1] - rect[0]))
            width = float(dnp.norm(rect[2] - rect[1]))
            rows.append([round(float(q[0]), 2), round(float(q[1]), 2), round(float(zz), 2),
                         round(math.degrees(dmath.atan2(h[1], h[0])) % 360., 1),
                         int(kind != "street"), round(width, 2), round(length, 2)])
        for a, b in _paint(rect, kind, tags, q, streets):
            a, b = tuple(round(v, 3) for v in a), tuple(round(v, 3) for v in b)
            segs.add(tuple(sorted((a, b))))
    marks = parking._boxed_chunks(sorted(segs), box)
    return rows, marks
