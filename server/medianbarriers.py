"""I-65/I-75 median treatments, shared by colliders and sign mounts.

Pair the actual rendered carriageways, using the bundled OSM route corridors only
to identify the Interstate. The 15 m limit measures pavement edge to pavement edge.
All placement is decided before clipping so neighbouring tiles share one wall.
"""
from functools import lru_cache
import json
from pathlib import Path

import numpy as np

import exitsigns
import roadmesh
import roads

DATA = Path(__file__).with_name('ky_interstate_medians.json')
MAX_MEDIAN = 15.
HEIGHT = 1.37                  # tall 54-inch profile, scaled from the supplied reference
BASE_WIDTH = .90
TOP_WIDTH = .24
STEP = 2.
OPENING_CLEARANCE = 4.
MATERIAL = 'bne_ky_barrier_concrete'
# User-selected I-65 treatment boundaries. The first is the north end of the
# US 62 bridge in the screenshot; the last is the Exit 9 overpass in Indiana.
CABLE_START = 37.67789
CABLE_SWITCH = 37.844624
JERSEY_RESUME = 38.09602
INDIANA_END = 38.39415
OHIO_RIVER = (-85.748, 38.26145, -85.738, 38.26956)
JERSEY, CABLE_NORTH, CABLE_SOUTH = 1, 2, 3


@lru_cache(maxsize=1)
def _data():
    return json.loads(DATA.read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def inventory():
    data = _data()
    paths = [(int(r), np.asarray(p, float)) for r, _, p in data['paths'] + data.get('indiana_paths', [])]
    bounds = np.array([[*p.min(0), *p.max(0)] for _, p in paths])
    stations = np.asarray(data['stations'], float)
    return paths, bounds, stations


def station_sets():
    return [('KY', inventory()[2]), ('IN', np.asarray(_data().get('indiana_stations', []), float).reshape(-1, 6))]


@lru_cache(maxsize=1)
def direction_stations():
    return np.concatenate([rows for _, rows in station_sets()])


def treatment(route, lat, lon):
    """0: none; 1: Jersey; 2/3: cable on the N/S-bound median shoulder.

    The latitude cuts cross the identified I-65 corridor at the requested places;
    they never select an unrelated road. Ohio River decks are explicitly excluded
    even if a future download incorrectly labels one as ground.
    """
    import signstates
    lat, lon = np.broadcast_arrays(np.asarray(lat), np.asarray(lon))
    states = signstates.states(lat, lon)
    result = np.where((states == 'KY') & (route in (65, 75)), JERSEY, 0)
    if route == 65:
        result = np.where((states == 'IN') & (lat <= INDIANA_END), JERSEY, result)
        result = np.where((states == 'KY') & (lat >= CABLE_START) & (lat < CABLE_SWITCH), CABLE_NORTH, result)
        result = np.where((states == 'KY') & (lat >= CABLE_SWITCH) & (lat < JERSEY_RESUME), CABLE_SOUTH, result)
        w, s, e, n = OHIO_RIVER
        result = np.where((lon >= w) & (lon <= e) & (lat >= s) & (lat <= n), 0, result)
    return result


def in_region(frame, box):
    lat, lon, _ = frame.enu_to_geodetic(np.array([box[0], box[2]]), np.array([box[1], box[3]]), 0.)
    return lat.max() >= 36.48 and lat.min() <= 39.16 and lon.max() >= -86.7 and lon.min() <= -83.8


def corridor(frame, box):
    if not in_region(frame, box):
        return []
    paths, bounds, _ = inventory()
    la, lo, _ = frame.enu_to_geodetic(np.array([box[0]-100, box[2]+100]),
                                    np.array([box[1]-100, box[3]+100]), 0.)
    use = np.flatnonzero((bounds[:, 2] >= lo.min()) & (bounds[:, 0] <= lo.max()) &
                         (bounds[:, 3] >= la.min()) & (bounds[:, 1] <= la.max()))
    return [(paths[i][0], exitsigns._xy(frame, paths[i][1])) for i in use]


def project(points, line):
    """Vector point-to-polyline projection; return points, tangents and distances."""
    a, ab = line[:-1, :2], np.diff(line[:, :2], axis=0)
    length = np.linalg.norm(ab, axis=1)
    t = np.clip(((points[:, None, :]-a)*ab).sum(2)/np.maximum(length**2, 1e-12), 0., 1.)
    projected = a+t[:, :, None]*ab
    d2 = ((projected-points[:, None, :])**2).sum(2)
    k = d2.argmin(1)
    return projected[np.arange(len(points)), k], ab[k]/np.maximum(length[k, None], 1e-12), np.sqrt(d2[np.arange(len(points)), k])


def identify(frame, line, direction, corridors):
    # Test along the source feature, which can continue well beyond this tile.
    best = None
    for route, path in corridors:
        q, tangent, distance = project(line[:, :2], path)
        k = int(distance.argmin())
        if distance[k] > 25.:
            continue
        _, own, _ = project(q[k:k+1], line)
        if abs(float(own[0]@tangent[k])) < .9:
            continue
        if best is None or distance[k] < best[0]:
            best = (distance[k], route, q[k], own[0]*direction)
    if best is None:
        return None
    _, route, point, travel = best
    stations = direction_stations()
    stations = stations[stations[:, 0] == route]
    xy = exitsigns._xy(frame, stations[:, 2:4])
    nearest = int(np.sum((xy-point)**2, axis=1).argmin())
    return route, float(travel@stations[nearest, 4:6]) > 0.


def _heights(frame, elev, xy):
    la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
    h = np.broadcast_to(np.asarray(elev(la, lo), float), la.shape)
    return np.asarray(frame.geodetic_to_enu(la, lo, h)[2])


def runs(frame, pavements, box, elev):
    """Ground wall runs. Ramps/crossovers cut openings; separated bridge decks stay separate."""
    corridors = corridor(frame, box)
    if not corridors:
        return []
    rows, blockers = [], []
    for fid, spec, line, width in pavements:
        line = np.asarray(line, float)
        if len(line) < 2:
            continue
        if (line[:, 0].max() < box[0]-60 or line[:, 0].min() > box[2]+60 or
                line[:, 1].max() < box[1]-60 or line[:, 1].min() > box[3]+60):
            continue
        identity = (identify(frame, line, spec.dir or 1, corridors)
                    if spec.cls == 'motorway' and spec.oneway and not spec.link else None)
        if identity:
            rows.append((str(fid), spec, line, width, *identity))
        else:
            blockers.append((line, width))
    import cablebarriers
    out = cablebarriers.runs(frame, rows, blockers, box, elev)
    for fid, spec, line, width, route, north in sorted(rows, key=lambda r: r[0]):
        if not north:
            continue
        xy, _, _ = roadmesh._resample(line, STEP)
        if len(xy) < 2:
            continue
        la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.)
        if not np.any(treatment(route, la, lo) == JERSEY):
            continue
        extension = (route == 65) & (la >= JERSEY_RESUME)
        limit = np.where(extension, 80., MAX_MEDIAN)
        travel = np.gradient(xy, axis=0)*(spec.dir or 1)
        travel /= np.maximum(np.linalg.norm(travel, axis=1)[:, None], 1e-9)
        left = np.c_[-travel[:, 1], travel[:, 0]]
        best = np.full(len(xy), np.inf)
        half_width = np.full(len(xy), BASE_WIDTH/2.)
        centres = xy.copy()
        other_edges = xy.copy()
        own_edges = xy+left*width/2.
        for other_id, other, points, other_width, other_route, other_north in rows:
            if other_route != route or other_north or other_id == fid:
                continue
            nearest, tangent, _ = project(xy, points)
            delta = nearest-xy
            separation = (delta*left).sum(1)
            gap = separation-(width+other_width)/2.
            parallel = (tangent*(other.dir or 1)*travel).sum(1) < -.90
            along = np.abs((delta*travel).sum(1)) < np.where(extension, np.maximum(1.5, .15*separation), 1.5)
            # The explicit Louisville/Indiana extension continues through paired
            # ground medians; elsewhere retain the original under-15 m rule.
            # Rendered highway shoulders can overlap slightly in this urban
            # corridor. There is still median space between the yellow lines:
            # use at most 1.2 m of each 1.5 m inside shoulder, retaining 0.3 m
            # clearance to each travel lane. Never put a wall in overlapping lanes.
            available = gap + np.where(extension, 2.4, 0.)
            valid = parallel & along & (available >= .40) & (gap < limit) & (separation < best)
            best[valid] = separation[valid]
            half_width[valid] = np.minimum(BASE_WIDTH/2., (available[valid]-.06)/2.)
            opposite_edge = nearest-left*other_width/2.
            centres[valid] = ((own_edges+opposite_edge)/2.)[valid]
            other_edges[valid] = opposite_edge[valid]
        valid = np.isfinite(best)
        if not valid.any():
            continue
        # Full source samples stay identical across adjacent road/sign tiles.
        for points, blocker_width in blockers:
            _, _, distance = project(centres, points)
            valid &= distance > blocker_width/2.+BASE_WIDTH/2.+OPENING_CLEARANCE
        la, lo, _ = frame.enu_to_geodetic(centres[:, 0], centres[:, 1], 0.)
        valid &= treatment(route, la, lo) == JERSEY
        z = _heights(frame, elev, centres)
        # Start the wall at the median ground; at sloping medians raise its top
        # to remain tall on both carriageways without floating the foundation.
        shoulder_z = np.maximum(_heights(frame, elev, own_edges), _heights(frame, elev, other_edges))
        tops = np.maximum(z, shoulder_z)+HEIGHT
        valid &= np.isfinite(z) & np.isfinite(tops)
        indices = np.flatnonzero(valid)
        for group in np.split(indices, np.flatnonzero(np.diff(indices) != 1)+1):
            if len(group) < 2:
                continue
            points = np.c_[centres[group], z[group], tops[group], roadmesh._normals(centres[group]), half_width[group]]
            for piece in roads.clip_polyline(points, *box):
                piece = piece[np.r_[True, np.linalg.norm(np.diff(piece[:, :2], axis=0), axis=1) > 1e-6]]
                if len(piece) >= 2 and np.linalg.norm(piece[-1, :2]-piece[0, :2]) > .05:
                    out.append(dict(route=route, n=piece[:, :3].tolist(), top=piece[:, 3].tolist(),
                                    cross=piece[:, 4:6].tolist(),
                                    half_width=piece[:, 6].tolist(),
                                    w=BASE_WIDTH, m=MATERIAL, barrier=True))
    return out


def build(chunks, origin):
    """Closed Jersey-profile prisms, merged into the existing colliding deck mesh."""
    vertices, triangles, uvs = [], [], []
    origin = np.asarray(origin, float)
    # Broad low toe, sloped lower face, nearly vertical tall upper face.
    profile = np.array([[-.45, -.15], [.45, -.15], [.45, .08], [.23, .36],
                        [.12, HEIGHT], [-.12, HEIGHT], [-.23, .36], [-.45, .08]])
    for chunk in chunks:
        if not chunk.get('barrier') or chunk.get('barrier_type') == 'cable':
            continue
        line = np.asarray(chunk['n'], float)
        normals = np.asarray(chunk['cross']) if 'cross' in chunk else roadmesh._normals(line[:, :2])
        sections = np.repeat(line[:, None, :], len(profile), axis=1)
        half = np.asarray(chunk.get('half_width', np.full(len(line), BASE_WIDTH/2.)))
        shoulder = np.maximum(.145, half*(.23/.45))
        offsets = np.stack([-half,half,half,shoulder,np.full(len(line),.12),
                            np.full(len(line),-.12),-shoulder,-half], axis=1)
        sections[:, :, :2] += normals[:, None, :]*offsets[:, :, None]
        sections[:, :, 2] += profile[None, :, 1]
        sections[:, 4:6, 2] = np.asarray(chunk['top'])[:, None]
        station = np.r_[0., np.cumsum(np.linalg.norm(np.diff(line[:, :2], axis=0), axis=1))]
        for k in range(len(line)-1):
            for j in range(len(profile)):
                nxt = (j+1)%len(profile)
                q = np.array([sections[k, j], sections[k+1, j], sections[k+1, nxt], sections[k, nxt]])
                # The profile runs counterclockwise looking down the path.
                index = len(vertices)
                vertices.extend(q-origin)
                triangles.extend([(index, index+2, index+1), (index, index+3, index+2)])
                uvs.extend([(station[k]/4., profile[j, 1]), (station[k+1]/4., profile[j, 1]),
                            (station[k+1]/4., profile[nxt, 1]), (station[k]/4., profile[nxt, 1])])
        for k, reverse in ((0, True), (-1, False)):
            index = len(vertices)
            vertices.extend(sections[k]-origin)
            uvs.extend(profile)
            for j in range(1, len(profile)-1):
                tri = (index, index+j, index+j+1)
                triangles.append(tri[::-1] if reverse else tri)
    import cablebarriers
    import plazas
    concrete = ((np.asarray(vertices), np.asarray(triangles), np.asarray(uvs), [MATERIAL],
                 np.zeros(len(triangles), np.int32)) if triangles else None)
    return plazas.merge(concrete, cablebarriers.build(chunks, origin))


def mount(frame, obj, pavements, elev):
    """The very same wall centre and top used by road collision, before tile ownership."""
    q = exitsigns._xy(frame, [[obj['lon'], obj['lat']]])[0]
    box = (q[0]-180., q[1]-180., q[0]+180., q[1]+180.)
    candidates = []
    axis = np.asarray(obj['mile_marker']['axis'], float)
    for run in runs(frame, pavements, box, elev):
        if run.get('barrier_type') == 'cable' or run['route'] != obj['mile_marker']['route']:
            continue
        line = np.asarray(run['n'], float)
        nearest, travel, distance = project(q[None, :], line)
        if distance[0] > 80. or abs(float((nearest[0]-q)@travel[0])) > 1.5:
            continue
        # Interpolate height on the same segment used by the nearest projection.
        segments = np.diff(line[:, :2], axis=0)
        fractions = np.clip(((nearest[0]-line[:-1, :2])*segments).sum(1)/
                            np.maximum((segments**2).sum(1), 1e-12), 0., 1.)
        distances = np.linalg.norm(line[:-1, :2]+segments*fractions[:, None]-nearest[0], axis=1)
        k = int(distances.argmin())
        t = float(fractions[k])
        top = run['top'][k]*(1.-t)+run['top'][k+1]*t
        direction = travel[0] if travel[0]@axis > 0 else -travel[0]
        candidates.append((distance[0], np.r_[nearest[0], top], direction))
    return min(candidates, key=lambda c: c[0])[1:] if candidates else None
