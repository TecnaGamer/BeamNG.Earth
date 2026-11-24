"""Conservative rock skins for sustained terrain-backed road cuttings.

Road proximity alone is not evidence: a face must rise from the road bench,
face the road, and break into gentler ground above it. All distances are metres.
"""
from cliffsettings import CLIFF_KERNEL
import math
import dmath

import numpy as np
import dnp

# Selection thresholds live in native cliffcuts.rs; these values are also used
# by Python input assembly and terrain-clearance orchestration.
MIN_SLOPE_DEG = 40.0
EDGE_SLOPE_DEG = 30.0
REACH_M = 64.0
SHOULDER_M = 3.0
ROAD_LABEL_MARGIN_M = 24.0
HIGHWAY_CLASSES = ('motorway', 'trunk', 'primary')
RURAL_CLASSES = HIGHWAY_CLASSES + ('secondary', 'tertiary', 'unclassified', 'residential', 'service')
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
    alignment = (gx*dgx+gy*dgy)/np.maximum(dnp.hypot(gx, gy)*dnp.hypot(dgx, dgy), 1e-12)
    return ((dnp.hypot(gx, gy) >= dmath.tan(math.radians(min_slope_deg)))
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
            count = max(1, math.ceil(dnp.norm(end-start)/ROAD_HEIGHT_STEP_M))
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
        import fast
        fast.require(CLIFF_KERNEL, "cliff pavement clearance")
        pavement = np.column_stack((self.a, self.b, self.h, self.low, self.high))
        return fast._ef.cliff_road_clearance(np.ascontiguousarray(xy, float),
            np.ascontiguousarray(low, float), np.ascontiguousarray(high, float),
            np.ascontiguousarray(np.broadcast_to(gap, (len(xy),)), float),
            np.ascontiguousarray(pavement, float), CLEARANCE_BUCKET_M, PAVEMENT_VERTICAL_GUARD_M)



def continue_ends(seed, profiles, dx, dy):
    """Use the production native traversal for bounded, connected cut ends."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff cut ends")
    return fast._ef.cliff_cut_ends(np.ascontiguousarray(seed, bool),
        np.ascontiguousarray(profiles, bool), dx, dy)


def sustained(evidence, extent, slope, dx, dy, *, profiles=None):
    """Use the production native component filter for independently proven banks."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff cut components")
    if profiles is None:
        profiles = np.zeros(np.shape(evidence), bool)
    return fast._ef.cliff_cut_sustained(np.ascontiguousarray(evidence, bool),
        np.ascontiguousarray(extent, bool), np.ascontiguousarray(slope, float),
        np.ascontiguousarray(profiles, bool), dx, dy)


def select(h, classes, xs, ys, scale, centers, segments, blocked, road_labels, highway_corridor=None,
           *, with_domain=False):
    """Confirm road-to-crest profiles in the native detector on the shared lattice."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff_cut_select")
    corridor = np.zeros(np.shape(h), bool) if highway_corridor is None else highway_corridor
    roads = np.column_stack((segments.a, segments.b, segments.h))
    result, domain = fast._ef.cliff_cut_select(np.ascontiguousarray(h, float),
        np.ascontiguousarray(classes, np.uint8), np.ascontiguousarray(blocked, bool),
        np.ascontiguousarray(road_labels, bool), np.ascontiguousarray(corridor, bool),
        np.ascontiguousarray(centers.reshape(-1,2), float), np.ascontiguousarray(roads, float),
        float(xs[1]-xs[0])*float(scale), float(ys[1]-ys[0]), highway_corridor is not None)
    return (result, domain) if with_domain else result


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
    radius = dnp.norm(vertices-center[:, None, :], axis=2).max(axis=1)
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
        radius = dnp.norm(p[..., :2]-center[:, None], axis=2).max(axis=1)
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
