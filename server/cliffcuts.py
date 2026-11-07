"""Conservative rock skins for sustained terrain-backed road cuttings.

Road proximity alone is not evidence: a face must rise from the road bench,
face the road, and break into gentler ground above it. All distances are metres.
"""
import math
import heapq

import numpy as np

MIN_SLOPE_DEG = 40.0
SUPPORT_SLOPE_DEG = 35.0  # profile-confirmed folds around a sustained steep core
EDGE_SLOPE_DEG = 30.0     # bounded 12 m extension at a confirmed cut's rounded foot/crest
MIN_CORE_CELLS = 6
CREST_SLOPE_DEG = 38.0
SLOPE_BREAK_DEG = 12.0
MIN_RISE_M = 4.0  # substantial roadside cutting, including lower highway banks
MIN_AVERAGE_DEG = 30.0  # face profile, including its rounded crest
BENCH_APPROACH_DEG = 15.0  # flat/gentle shoulder before the first rising face
REACH_M = 64.0
PROFILE_STEP_M = 4.0
PROFILE_PROBE_M = 4.0
FACING_COS = 0.75
MIN_FACE_M2 = 192.0
MIN_FOOTPRINT_M2 = 96.0  # six nominal detector cells; length/core tests still apply
MIN_LENGTH_M = 24.0
SHOULDER_M = 3.0
# The 10 m land-cover raster can place its road strip more than one pixel from
# the vector centreline. Ignore road-strip buffer seeds; major rural highways
# can also recover road-labelled faces when a natural crest confirms the cut.
ROAD_LABEL_MARGIN_M = 24.0  # two 10 m pixels plus the detector cell at a misaligned crest
# A major rural highway can pass near built-up land without its excavation being
# a city elevation anomaly. Its independently confirmed cut still keeps a local
# built-up guard; ordinary roads and natural cliffs retain the full city buffer.
HIGHWAY_CITY_BUFFER_M = 16.0
HIGHWAY_CLASSES = ('motorway', 'trunk', 'primary')
# Rural access/frontage roads can have real excavated banks too. Their road
# class must not force a profile to start at a more distant highway below it.
RURAL_CLASSES = HIGHWAY_CLASSES + ('secondary', 'tertiary', 'unclassified', 'residential', 'service')
PROFILE_DROP_M = 1.0  # do not look over a roof/back slope for a distant natural crest
# Shorter profiles can finish an existing excavation, but cannot seed one.
END_SLOPE_DEG = 20.0
END_AVERAGE_DEG = 15.0
END_RISE_M = 1.0
END_REACH_M = 160.0  # path distance along a continuous cutting, not a dilation radius
END_NATURAL_FRACTION = 0.5  # tolerate a crest straddling the coarse road label
END_CREST_HEIGHT_TOLERANCE_M = 2.0
END_PROFILE_STEP_M = 2.0  # resolve the crest of a bank shorter than one DEM cell
END_CREST_SEARCH_M = 8.0  # within one 10 m land-cover pixel
PAVEMENT_VERTICAL_GUARD_M = 2.0
ROAD_HEIGHT_STEP_M = 4.0
CLEARANCE_BUCKET_M = 16.0
CLEARANCE_EDGE_SUBDIVISIONS = 3
SOURCE_SLOPE_DEG = 30.0
SOURCE_HEIGHT_TOLERANCE_M = 6.0
SOURCE_MATCH_CORNERS = 3
SOURCE_FACING_COS = 0.75


def road_groups(ground_roads, has_source):
    """Keep the urban-highway exception separate from ordinary rural cuts.

    The urban flag describes land cover along a road, not the origin of a wall.
    Only major ground roads may use this exception, and only with source DEM
    evidence. The caller supplies ground parts, never bridge decks.
    """
    import roads
    groups = []
    for urban in (False, True):
        if urban and not has_source:
            continue
        allowed = HIGHWAY_CLASSES if urban else RURAL_CLASSES
        selected = [(k, spec, pts) for k, (_, spec, pts, _) in enumerate(ground_roads)
                    if bool(spec.urban) == urban and spec.cls in allowed]
        if not selected:
            continue
        segments = roads._Segs({str(k): (pts[:, :2], spec.width/2) for k, spec, pts in selected})
        highways = roads._Segs({str(k): (pts[:, :2], spec.width/2) for k, spec, pts in selected
                               if spec.cls in HIGHWAY_CLASSES})
        groups.append((urban, segments, highways))
    return groups


def select_roads(h, classes, xs, ys, scale, centers, vertices, groups, blocked, backed=None,
                 *, refine_source=False):
    """Select each road group without letting rural evidence authorize urban fill.

    Urban cuts retain every profile/land-cover test and additionally require
    original terrain agreement throughout their footprint and toe-fit domain.
    With refine_source, return separate rural/urban land domains so the caller
    can check source agreement on the finer mesh grid instead of stair-stepping
    that check on the detection grid. Urban land alone never authorizes fitting.
    """
    cuts = np.zeros_like(blocked, dtype=bool)
    domain = np.zeros_like(cuts)
    urban_domain = np.zeros_like(cuts)
    for urban, segments, highways in groups:
        if urban and (backed is None or not (backed & ~blocked).any()):
            continue
        labels = segments.near(vertices, ROAD_LABEL_MARGIN_M).reshape(h.shape)
        corridor = highways.near(vertices, REACH_M).reshape(h.shape)
        excluded = blocked | ~backed if urban else blocked
        selected, land = select(h, classes, xs, ys, scale, centers, segments,
                                excluded, labels, corridor, with_domain=True)
        cuts |= selected
        # An empty pass must not lend a permissive domain to another group.
        if selected.any():
            if urban:
                urban_domain |= land
            else:
                domain |= land
    if refine_source:
        return cuts, domain, urban_domain
    if backed is not None:
        domain |= urban_domain & backed
    return cuts, domain


def terrain_backed(source, drawn, dx, dy, *, min_slope_deg=SOURCE_SLOPE_DEG):
    """Only relax road buffers where steep relief exists before road grading.

    Missing source data fails closed. This is additional evidence, never a seed:
    the normal area, natural crest, city and uphill-cut tests must still pass.
    """
    gx = (np.diff(source, axis=1)[:-1]+np.diff(source, axis=1)[1:])/(2*dx)
    gy = (np.diff(source, axis=0)[:, :-1]+np.diff(source, axis=0)[:, 1:])/(2*dy)
    dgx = (np.diff(drawn, axis=1)[:-1]+np.diff(drawn, axis=1)[1:])/(2*dx)
    dgy = (np.diff(drawn, axis=0)[:, :-1]+np.diff(drawn, axis=0)[:, 1:])/(2*dy)
    error = np.abs(source-drawn)
    corners = np.stack((error[:-1, :-1], error[1:, :-1], error[:-1, 1:], error[1:, 1:]))
    # Road grading can move a narrow, almost vertical wall within a coarse
    # cell. One corner then changes height substantially although the original
    # face still backs the other three. Requiring all four fragmented that wall
    # into strips. Require finite data everywhere and agreement on three corners
    # plus the original slope direction; unrelated fills cannot provide proof.
    matched = (corners <= SOURCE_HEIGHT_TOLERANCE_M).sum(axis=0) >= SOURCE_MATCH_CORNERS
    alignment = (gx*dgx+gy*dgy)/np.maximum(np.hypot(gx, gy)*np.hypot(dgx, dgy), 1e-12)
    return ((np.hypot(gx, gy) >= math.tan(math.radians(min_slope_deg)))
            & np.isfinite(corners).all(axis=0) & matched & (alignment >= SOURCE_FACING_COS))


class RoadClearance:
    """Height-aware pavement capsules, sampled once during tile generation.

    Use the elevation range of each short road segment, not only its nearest
    point. A triangle crossing a sloping road must remain excluded. Unknown
    heights retain the full planar exclusion. Bridges are absent from the
    ground-road input and cannot authorize a rock cutting.
    """
    def __init__(self, segments, height_at, bounds=None):
        a, b, half = [], [], []
        for start, end, width in zip(segments.a, segments.b, segments.h):
            if bounds is not None and (np.any(np.maximum(start, end)+width < bounds[0])
                                       or np.any(np.minimum(start, end)-width > bounds[1])):
                continue
            count = max(1, math.ceil(np.linalg.norm(end-start)/ROAD_HEIGHT_STEP_M))
            line = start+np.linspace(0, 1, count+1)[:, None]*(end-start)
            a.extend(line[:-1]); b.extend(line[1:]); half.extend([width]*count)
        self.a = np.asarray(a).reshape(-1, 2)
        self.b = np.asarray(b).reshape(-1, 2)
        self.h = np.asarray(half)
        z = np.asarray(height_at(np.concatenate((self.a, self.b)))) if len(a) else np.empty(0)
        za, zb = np.split(z, 2)
        self.low, self.high = np.minimum(za, zb), np.maximum(za, zb)

    def cell_blocked(self, xy, low, high, gap):
        """Keep a coarse wall cell if part of its height is clear of roads.

        A four-metre cell can include both the wall and its upper roadway. Test
        its lower, middle and upper height before throwing away the whole wall.
        This is only a search mask: clear_triangles still clips every generated
        triangle against the full pavement volume, including its interior.
        """
        levels = np.concatenate((low, (low+high)*.5, high))
        blocked = self.blocked(np.tile(xy, (3, 1)), levels, levels, gap)
        return blocked.reshape(3, -1).all(axis=0)

    def blocked(self, xy, low, high, gap):
        """Reject enclosing horizontal discs whose height slab meets pavement."""
        hit = np.zeros(len(xy), bool)
        if not len(xy):
            return hit
        gap = np.broadcast_to(gap, (len(xy),))
        # Tile-local spatial buckets avoid a full scan of every mesh triangle
        # for every four-metre road segment in a dense road network.
        keys, inverse = np.unique(np.floor(xy/CLEARANCE_BUCKET_M).astype(np.int64), axis=0, return_inverse=True)
        order = np.argsort(inverse)
        groups = np.split(order, np.cumsum(np.bincount(inverse))[:-1])
        buckets = {tuple(k): group for k, group in zip(keys, groups)}
        max_gap = float(gap.max())
        xy_low, xy_high = xy.min(axis=0), xy.max(axis=0)
        for a, b, width, z0, z1 in zip(self.a, self.b, self.h, self.low, self.high):
            lo, hi = np.minimum(a, b)-width-max_gap, np.maximum(a, b)+width+max_gap
            if np.any(hi < xy_low) or np.any(lo > xy_high):
                continue
            lo, hi = np.floor(np.maximum(lo, xy_low)/CLEARANCE_BUCKET_M).astype(int), np.floor(np.minimum(hi, xy_high)/CLEARANCE_BUCKET_M).astype(int)
            groups = [buckets[(x, y)] for x in range(lo[0], hi[0]+1) for y in range(lo[1], hi[1]+1)
                      if (x, y) in buckets]
            if not groups:
                continue
            ix = np.concatenate(groups)
            near = ~hit[ix]
            if np.isfinite(z0+z1):
                near &= (high[ix] >= z0-PAVEMENT_VERTICAL_GUARD_M) & (low[ix] <= z1+PAVEMENT_VERTICAL_GUARD_M)
            ix = ix[near]
            v = b-a
            t = np.clip((xy[ix]-a) @ v/max(float(v @ v), 1e-12), 0, 1)
            hit[ix] = np.linalg.norm(xy[ix]-a-t[:, None]*v, axis=1) <= width+gap[ix]
        return hit


def continue_ends(seed, profiles, dx, dy):
    """Follow connected, independently profiled ends from a sustained cut.

    Include diagonal neighbours so a narrow oblique face stays connected.
    Every visited cell needs a valid profile; physical path length bounds the
    extra work and keeps the result tile-stable.
    """
    distance = np.full(seed.shape, np.inf)
    distance[seed] = 0
    queue = [(0., int(y), int(x)) for y, x in zip(*np.nonzero(seed))]
    heapq.heapify(queue)
    steps = [(oy, ox, math.hypot(ox*dx, oy*dy)) for oy in (-1, 0, 1)
             for ox in (-1, 0, 1) if oy or ox]
    while queue:
        d, y, x = heapq.heappop(queue)
        if d != distance[y, x]:
            continue
        for oy, ox, step in steps:
            ny, nx, nd = y+oy, x+ox, d+step
            if (0 <= ny < seed.shape[0] and 0 <= nx < seed.shape[1]
                    and profiles[ny, nx] and nd <= END_REACH_M and nd < distance[ny, nx]):
                distance[ny, nx] = nd
                heapq.heappush(queue, (nd, ny, nx))
    return np.isfinite(distance)


def city_guard(classes, road_labels, highway_corridor, spacing):
    """Buffer actual built-up land after removing geometrically proven road cuts."""
    import cliffrocks as cr
    seeds = (classes == 50) & ~road_labels
    city = cr._grow(seeds, math.ceil(cr.CITY_BUFFER_M/spacing))
    if highway_corridor is not None:
        local = cr._grow(seeds, math.ceil(HIGHWAY_CITY_BUFFER_M/spacing))
        city = local | (city & ~highway_corridor)
    return city


def sustained(evidence, extent, slope, dx, dy, *, profiles=None):
    """Require a steep core and a substantial, independently profiled bank.

    Rounded parts may supply size only when their own road-to-crest profiles
    passed. Unprofiled support cannot make a short isolated core qualify.
    """
    import cliffrocks as cr
    result = np.zeros_like(evidence)
    for cy, cx in cr._components(extent):
        proof = evidence[cy, cx]
        if not proof.any():
            continue
        area = dx*dy*np.sqrt(1+slope[cy, cx]**2).sum()
        # Measure along the face, not only the map axes. A diagonal 25 m
        # cutting otherwise appears shorter than 20 m and loses its whole bank.
        measured = proof if profiles is None else proof | profiles[cy, cx]
        points = np.column_stack((cx[measured]*dx, cy[measured]*dy))
        centered = points-points.mean(axis=0)
        _, axes = np.linalg.eigh(centered.T @ centered)
        direction = axes[:, -1]
        along = np.ptp(points @ direction)+np.abs(direction) @ np.array((dx, dy))
        length = max(np.ptp(points[:, 0])+dx, np.ptp(points[:, 1])+dy, along)
        core = proof & (slope[cy, cx] >= math.tan(math.radians(MIN_SLOPE_DEG)))
        if (core.sum() >= MIN_CORE_CELLS and measured.sum()*dx*dy >= MIN_FOOTPRINT_M2
                and area >= MIN_FACE_M2 and length >= MIN_LENGTH_M):
            result[cy, cx] = True
    return result


def closest(points, segments, uphill=None):
    """Nearest facing carriageway edge and centreline foot, bounded to REACH_M.

    Vectorize over candidate points, not a points-by-all-roads allocation. Ignore
    zero-length segments and leave missing matches at infinite distance.
    """
    distance = np.full(len(points), np.inf)
    foot = np.zeros_like(points)
    half = np.zeros(len(points))
    if not len(points):
        return distance, foot, half
    lo, hi = points.min(0), points.max(0)
    for a, b, width in zip(segments.a, segments.b, segments.h):
        if np.any(np.maximum(a, b)+width+REACH_M < lo) or np.any(np.minimum(a, b)-width-REACH_M > hi):
            continue
        v = b-a
        length2 = v @ v
        if length2 <= 0:
            continue
        t = np.clip((points-a) @ v/length2, 0, 1)
        q = a+t[:, None]*v
        d = np.linalg.norm(points-q, axis=1)-width
        take = (d < distance) & (d <= REACH_M)
        if uphill is not None:
            # A nearer road above a terrace is its fill side, not the road that
            # excavated this face. Keep looking for a road downhill of the face.
            alignment = np.sum((points-q)*uphill, axis=1)/np.maximum(d+width, 1e-9)
            take &= alignment >= FACING_COS
        distance[take], foot[take], half[take] = d[take], q[take], width
    return distance, foot, half


def select(h, classes, xs, ys, scale, centers, segments, blocked, road_labels, highway_corridor=None,
           *, with_domain=False):
    """Return cells belonging to confirmed road cuts on the shared DEM lattice.

    segments contains rural ground roads only; blocked protects *all* roads.
    road_labels marks the rural carriageway's raster-alignment corridor on the
    vertex grid. Outside it, only sustained highway-cut profiles can correct
    built-up labels; all other built-up land still triggers city exclusion.
    with_domain also returns the land-safe domain for sub-cell toe fitting.
    The caller must reapply road clearance on the fine grid. Repeating coarse
    road exclusions here would cut stair steps into narrow walls below roads.
    This domain grants no new cliff evidence.
    """
    import cliffrocks as cr
    dx, dy = float(xs[1]-xs[0])*scale, float(ys[1]-ys[0])
    gx = (np.diff(h, axis=1)[:-1]+np.diff(h, axis=1)[1:])/(2*dx)
    gy = (np.diff(h, axis=0)[:, :-1]+np.diff(h, axis=0)[:, 1:])/(2*dy)
    slope = np.hypot(gx, gy)
    city = city_guard(classes, road_labels, highway_corridor, min(dx, dy))
    natural = np.isin(classes, (10, 20, 30, 60, 100))
    natural_samples = natural.astype(float)
    label_samples = road_labels.astype(float)
    # Coarse land cover sometimes includes a highway's excavated wall in its
    # built-up road strip. Profile confirmation below still requires a natural
    # crest, so wholly built-up roofs cannot supply proof.
    road_strip = ((classes == 50) & road_labels & highway_corridor
                  if highway_corridor is not None else np.zeros_like(natural))
    safe = np.isfinite(h) & ((natural & ~city) | road_strip)
    # Evaluate geometry before the city buffer inside major rural highway cuts.
    # A road-labelled terrace must not cast a city exclusion onto the next lift.
    # This is only a provisional search; city exclusions are reapplied below.
    probe_safe = safe.copy()
    if highway_corridor is not None:
        probe_safe |= np.isfinite(h) & highway_corridor & (natural | (classes == 50))
    eligible = probe_safe[:-1, :-1] & probe_safe[1:, :-1] & probe_safe[:-1, 1:] & probe_safe[1:, 1:] & ~blocked
    candidate = eligible & (slope >= math.tan(math.radians(END_SLOPE_DEG)))
    yy, xx = np.nonzero(candidate)
    p = centers[yy, xx]
    uphill = np.stack((gx[yy, xx], gy[yy, xx]), axis=-1)/slope[yy, xx, None]
    distance, foot, half = closest(p, segments, uphill)
    near = np.isfinite(distance) & (distance >= SHOULDER_M)
    yy, xx, p, foot, half, distance = (a[near] for a in (yy, xx, p, foot, half, distance))
    if not len(yy):
        empty = np.zeros_like(candidate)
        return (empty, empty.copy()) if with_domain else empty
    away = (p-foot)/(distance+half)[:, None]
    # Geographic east/north and tile ENU differ negligibly over these small
    # tiles. Samples stay on the shared lattice to avoid per-tile profile phases.
    facing = (gx[yy, xx]*away[:, 0]+gy[yy, xx]*away[:, 1])/slope[yy, xx]
    toe_distance = distance-SHOULDER_M
    tx = xx+.5-away[:, 0]*toe_distance/dx
    ty = yy+.5-away[:, 1]*toe_distance/dy
    toe = cr._height_at(h, tx, ty)
    # A bench is required at the foot as well as a slope break at the crest.
    inner = cr._height_at(h, tx-away[:, 0]*PROFILE_PROBE_M/dx,
                         ty-away[:, 1]*PROFILE_PROBE_M/dy)
    bench = np.abs(toe-inner)/PROFILE_PROBE_M <= math.tan(math.radians(CREST_SLOPE_DEG))
    local_angle = np.degrees(np.arctan(slope[yy, xx]))
    crest_limit = np.tan(np.radians(np.minimum(CREST_SLOPE_DEG, local_angle-SLOPE_BREAK_DEG)))
    confirmed = np.zeros(len(yy), bool)
    crest_reach = np.zeros(len(yy))
    end_confirmed = np.zeros(len(yy), bool)
    end_crest_reach = np.zeros(len(yy))
    # Upper lifts may be separated from the road by flat benches. Check that
    # whole approach, so a roof followed by a drop cannot borrow a hill's crest.
    approach_z = toe.copy()
    face_toe, face_start = toe.copy(), np.zeros(len(yy))
    on_bench = np.ones(len(yy), bool)
    previous_run = np.zeros(len(yy))
    approach = np.ones(len(yy), bool)
    cover_samples = (natural | (classes == 50)).astype(float)
    for reach in np.arange(PROFILE_STEP_M, REACH_M+PROFILE_STEP_M, PROFILE_STEP_M):
        run = np.minimum(reach, toe_distance)
        px, py = tx+away[:, 0]*run/dx, ty+away[:, 1]*run/dy
        z = cr._height_at(h, px, py)
        approach &= np.isfinite(z) & (z >= approach_z-PROFILE_DROP_M)
        approach &= cr._height_at(cover_samples, px, py) >= .999
        # A wide shoulder or drainage shelf is not part of the cut face's
        # average slope. Trim only the initial gentle approach; never skip an
        # intervening bench, a drop, or a roof on the way to a distant crest.
        delta = run-previous_run
        on_bench &= np.isfinite(z) & (np.abs(z-approach_z) <= delta*math.tan(math.radians(BENCH_APPROACH_DEG)))
        face_toe = np.where(on_bench, z, face_toe)
        face_start = np.where(on_bench, run, face_start)
        previous_run = run
        approach_z = z
    previous_z = cr._height_at(h, xx+.5, yy+.5)
    uphill_path = approach.copy()
    end_path, end_peak = approach.copy(), previous_z.copy()
    for reach in np.arange(END_PROFILE_STEP_M, REACH_M+END_PROFILE_STEP_M, END_PROFILE_STEP_M):
        px = xx+.5+away[:, 0]*reach/dx
        py = yy+.5+away[:, 1]*reach/dy
        probe = PROFILE_PROBE_M
        z = cr._height_at(h, px, py)
        cover = cr._height_at(cover_samples, px, py) >= .999
        strict_sample = reach % PROFILE_STEP_M == 0
        if strict_sample:
            uphill_path &= np.isfinite(z) & (z >= previous_z-PROFILE_DROP_M) & cover
            previous_z = z
        end_path &= np.isfinite(z) & cover & (z >= end_peak-PROFILE_DROP_M)
        end_peak = np.maximum(end_peak, z)
        sx = (cr._height_at(h, px+probe/dx, py)-cr._height_at(h, px-probe/dx, py))/(2*probe)
        sy = (cr._height_at(h, px, py+probe/dy)-cr._height_at(h, px, py-probe/dy))/(2*probe)
        crest_cover = cr._height_at(natural_samples, px, py)
        natural_crest = crest_cover >= .999
        end_cover = crest_cover.copy()
        crest_has_land = (crest_cover > 0) | (cr._height_at(label_samples, px, py) >= .999)
        # A sub-pixel lateral crest check handles road-label raster boundaries.
        # It must be at the same elevation, not a different hill behind a roof.
        for offset in (-END_CREST_SEARCH_M, -PROFILE_PROBE_M, PROFILE_PROBE_M, END_CREST_SEARCH_M):
            ex = px-away[:, 1]*offset/dx
            ey = py+away[:, 0]*offset/dy
            same_crest = (crest_has_land
                          & (np.abs(cr._height_at(h, ex, ey)-z) <= END_CREST_HEIGHT_TOLERANCE_M))
            end_cover = np.maximum(end_cover, np.where(same_crest, cr._height_at(natural_samples, ex, ey), 0))
        span = toe_distance+reach
        found = (strict_sample & (distance+reach <= REACH_M) & (facing >= FACING_COS) & bench
                 & uphill_path & natural_crest & (np.hypot(sx, sy) <= crest_limit) & (z-face_toe >= MIN_RISE_M)
                 & ((z-face_toe)/(span-face_start) >= math.tan(math.radians(MIN_AVERAGE_DEG))))
        crest_reach[found & ~confirmed] = reach
        confirmed |= found
        end_found = ((distance+reach <= REACH_M) & (facing >= FACING_COS) & bench
                     & end_path & (end_cover >= END_NATURAL_FRACTION)
                     & (np.hypot(sx, sy) <= crest_limit) & (z-toe >= END_RISE_M)
                     & ((z-toe)/span >= math.tan(math.radians(END_AVERAGE_DEG))))
        end_crest_reach[end_found] = reach
        end_confirmed |= end_found
    evidence = np.zeros_like(candidate)
    evidence[yy, xx] = confirmed & (local_angle >= SUPPORT_SLOPE_DEG)
    end_profiles = np.zeros_like(candidate)
    end_profiles[yy, xx] = end_confirmed
    if highway_corridor is not None:
        highway_cells = (highway_corridor[:-1, :-1] & highway_corridor[1:, :-1]
                         & highway_corridor[:-1, 1:] & highway_corridor[1:, 1:])
        # Only sustained, directly confirmed steep faces can authorize a raster
        # correction. Rounded support and flat benches cannot supply steep-core proof.
        proof = evidence & highway_cells
        # Tapered sections already have independent uphill/crest evidence.
        # Include them when measuring the bank, before road-label city buffers
        # can fragment the steep core. They cannot supply steep-core cells.
        profiled_bank = continue_ends(proof, end_profiles & highway_cells, dx, dy)
        recovered = sustained(proof, profiled_bank, slope, dx, dy, profiles=end_profiles)
        # Partial road labels and low terminal banks are only recoverable when
        # connected to a substantial, fully natural-crested highway cut.
        recovered = continue_ends(recovered, end_profiles & highway_cells, dx, dy)
        vertices = np.zeros_like(natural)
        for oy, ox in ((0, 0), (1, 0), (0, 1), (1, 1)):
            vertices[oy:oy+recovered.shape[0], ox:ox+recovered.shape[1]] |= recovered
        # Remove road-label buffer seeds along the proven toe-to-crest profile,
        # including intervening benches. This does not add benches to the mesh.
        take = recovered[yy, xx]
        span = toe_distance[take]+np.where(confirmed[take], crest_reach[take], end_crest_reach[take])
        for run in np.arange(0, REACH_M+min(dx, dy)/2, min(dx, dy)/2):
            active = run <= span
            px = np.floor(tx[take][active]+away[take, 0][active]*run/dx).astype(int)
            py = np.floor(ty[take][active]+away[take, 1][active]*run/dy).astype(int)
            for oy, ox in ((0, 0), (1, 0), (0, 1), (1, 1)):
                vertices[np.clip(py+oy, 0, h.shape[0]-1), np.clip(px+ox, 0, h.shape[1]-1)] = True
        city = city_guard(classes, road_labels | vertices, highway_corridor, min(dx, dy))
        safe = np.isfinite(h) & (((natural | ((classes == 50) & vertices)) & ~city) | road_strip)
    eligible = safe[:-1, :-1] & safe[1:, :-1] & safe[:-1, 1:] & safe[1:, 1:] & ~blocked
    evidence &= eligible
    support = np.zeros_like(candidate)
    support[yy, xx] = (facing >= FACING_COS) & bench & (local_angle >= EDGE_SLOPE_DEG)
    support &= eligible
    extent = cr._extend_faces(evidence, support, dx, dy)
    result = sustained(evidence, extent, slope, dx, dy, profiles=end_profiles)
    result = continue_ends(result, end_profiles & eligible, dx, dy)
    land_domain = safe[:-1, :-1] & safe[1:, :-1] & safe[:-1, 1:] & safe[1:, 1:]
    return (result, land_domain) if with_domain else result


def clear_triangles(points, triangles, segments, clearance=None):
    """Keep displaced triangles entirely outside pavement plus SHOULDER_M.

    Distance to a road capsule is 1-Lipschitz: centre distance greater than the
    triangle's enclosing radius guarantees every point is clear, including long
    edges whose endpoints alone would miss a crossing road. Bucket radii upward
    to keep the native batched query cheap and conservative.
    """
    if not len(triangles) or not len(segments.h):
        return triangles
    vertices = points[triangles, :2]
    center = vertices.mean(axis=1)
    radius = np.linalg.norm(vertices-center[:, None, :], axis=2).max(axis=1)
    if clearance is not None:
        z = points[triangles, 2]
        return triangles[~clearance.blocked(center, z.min(axis=1), z.max(axis=1), SHOULDER_M+radius)]
    buckets = np.ceil(radius)  # one-metre conservative radius bins
    clear = np.ones(len(triangles), bool)
    for r in np.unique(buckets):
        at = buckets == r
        clear[at] = ~segments.near(center[at], SHOULDER_M+r)
    return triangles[clear]


def fit_clearance(points, triangles, attributes, clearance):
    """Recover clear portions of steep boundary triangles without crossing roads.

    Only mixed triangles are subdivided, at most three times. Most of the wall
    keeps its original density. Every emitted triangle passes the same full
    enclosing-disc/height-slab check as the original pavement exclusion.
    """
    pending = np.concatenate((points[triangles], attributes[triangles]), axis=2)
    kept = []
    for level in range(CLEARANCE_EDGE_SUBDIVISIONS+1):
        if not len(pending):
            break
        p = pending[..., :3]
        center = p[..., :2].mean(axis=1)
        radius = np.linalg.norm(p[..., :2]-center[:, None], axis=2).max(axis=1)
        blocked = clearance.blocked(center, p[..., 2].min(axis=1), p[..., 2].max(axis=1), SHOULDER_M+radius)
        kept.append(pending[~blocked])
        if level == CLEARANCE_EDGE_SUBDIVISIONS:
            break
        pending = pending[blocked]
        p = pending[..., :3].reshape(-1, 3)
        corners = clearance.blocked(p[:, :2], p[:, 2], p[:, 2], SHOULDER_M).reshape(-1, 3)
        pending = pending[~corners.all(axis=1)]
        a, b, c = pending[:, 0], pending[:, 1], pending[:, 2]
        ab, bc, ca = (a+b)*.5, (b+c)*.5, (c+a)*.5
        pending = np.concatenate([np.stack(t, axis=1) for t in
                                  ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
    if not kept:
        return points[:0], triangles[:0], attributes[:0]
    packed = np.concatenate(kept).reshape(-1, 3+attributes.shape[1])
    packed, indices = np.unique(packed, axis=0, return_inverse=True)
    return packed[:, :3], indices.reshape(-1, 3), packed[:, 3:]
