"""Continuous rock relief on sustained natural cliffs, streamed with road-tile content.

The mesh is a fitted rock skin, not a random boulder scatter. Every accepted ground
cell is covered; adjoining facets share their boundary positions. Selection uses
both face area and footprint, so narrow coastal walls can qualify without letting
a single very tall DEM spike qualify.
Unknown land cover fails closed. Visibility is controlled by in-game settings.
"""
from dataclasses import dataclass, field, replace
import logging
import heapq
import math

import numpy as np
import clifftypes
import cliffmaterial

log = logging.getLogger("earthd.cliffrocks")


MATERIAL = "bne_cliff_strata"
GRID_M = 4.0
CANYON_RENDER_SUBDIVISIONS = 4  # 1 m footprint; tall walls refine vertically in surface space
RENDER_SUBDIVISIONS = 8    # 0.5 m terrain/heightmap samples; detection remains coarse
CLEARANCE_M = 0.35
EDGE_BLEND_M = 2.0
TERRAIN_SPAN_M = 2.0 * math.sqrt(2.0)  # diagonal of the game's 2 m terrain cell
CLEARANCE_STEP_M = 0.25
CLEARANCE_SEARCH_M = 2 * TERRAIN_SPAN_M
STEEP_BLEND_DEG = 70.0
TERRAIN_CLEARANCE_M = 0.20  # includes fine-terrain simplification and height quantisation
CLEARANCE_REFINE_STEPS = 5
SHORE_DRY_MIN_M = 0.5
SHORE_CELL_RELIEF_M = 6.0
MIN_SLOPE_DEG = 58.0
SUPPORT_SLOPE_DEG = 45.0   # a continuous cliff's less steep folds connect its extreme faces
MIN_AREA_M2 = 1024.0
MIN_FOOTPRINT_M2 = 256.0
MIN_CORE_AREA_M2 = 128.0   # sustained extreme ground footprint within the connected face
MIN_CORE_CELLS = 6        # a one-vertex spike steepens at most its four adjacent cells
# A DEM-smoothed escarpment can lack a 58-degree core. Require an independently
# sustained 50-degree core AND a compact crest-to-toe profile in that case.
SCARP_CORE_DEG = 50.0
FACE_SUPPORT_DEG = 40.0  # only around a confirmed cliff; never an independent seed
FACE_EDGE_DEG = 35.0     # short rounded shoulders between confirmed face sections
FACE_REACH_M = 12.0      # physical distance through connected steep ground
SCARP_DROP_M = 10.0
SCARP_REACH_M = 40.0
SCARP_WIDTH_M = 48.0
SCARP_AVERAGE_DEG = 40.0
SCARP_SHOULDER_DEG = 30.0
PROFILE_STEP_M = 4.0
PROFILE_PROBE_M = 2.0
CITY_BUFFER_M = 64.0
ROAD_BUFFER_M = 6.0
EDGE_SINK_M = 0.35
TOE_REACH_M = GRID_M * math.sqrt(2.0)  # at most one detection-cell diagonal
TOE_SLOPE_DEG = 20.0      # rounded foot, never independent cliff evidence
TOE_DOWNHILL_COS = 0.5    # stay within 60 degrees of the downhill direction
CUT_SAMPLE_HOLE_M2 = 4.0  # isolated flat samples inside an already proven wall
ALBEDO_HEADROOM = 2.0  # matched by the material factor; vertex colours can only attenuate
SCAN_MEDIAN_SRGB = np.array((155, 152, 147), float) / 255
WORLD_M_PER_DEG = 111320.0
# Rock colour and relief in metres: warm desert, subdued humid rock,
# cooler alpine/boreal rock. These are visual priors, not geological data.
PALETTES = np.array(((245, 245, 245), (255, 235, 205), (238, 238, 232), (235, 245, 255)), float)
RELIEF_M = np.array((0.7, 1.0, 0.55, 1.15))


def _grow(mask, radius):
    """Conservative square buffer, in cells; never wraps at an array edge."""
    out = mask.copy()
    for axis in (0, 1):
        src = out.copy()
        for d in range(1, min(radius + 1, mask.shape[axis])):
            a, b = [slice(None)] * 2, [slice(None)] * 2
            a[axis], b[axis] = slice(d, None), slice(None, -d)
            out[tuple(a)] |= src[tuple(b)]
            out[tuple(b)] |= src[tuple(a)]
    return out


def _components(mask):
    """Indices of each four-connected component, without diagonal bridges."""
    remaining = mask.copy()
    rows, cols = mask.shape
    for j, i in zip(*np.nonzero(mask)):
        if not remaining[j, i]:
            continue
        component, todo = [], [(j, i)]
        remaining[j, i] = False
        while todo:
            y, x = todo.pop()
            component.append((y, x))
            for yy, xx in ((y-1, x), (y+1, x), (y, x-1), (y, x+1)):
                if 0 <= yy < rows and 0 <= xx < cols and remaining[yy, xx]:
                    remaining[yy, xx] = False
                    todo.append((yy, xx))
        yield np.asarray(component).T


def _height_at(h, x, y):
    """Bilinear vertex-grid sample; missing/outside profile data fails closed."""
    valid = (x >= 0) & (y >= 0) & (x <= h.shape[1]-1) & (y <= h.shape[0]-1)
    ix = np.clip(np.floor(x).astype(int), 0, h.shape[1]-2)
    iy = np.clip(np.floor(y).astype(int), 0, h.shape[0]-2)
    fx, fy = x-ix, y-iy
    z = ((h[iy, ix]*(1-fx) + h[iy, ix+1]*fx)*(1-fy)
         + (h[iy+1, ix]*(1-fx) + h[iy+1, ix+1]*fx)*fy)
    return np.where(valid, z, np.nan)


def _bounded_scarps(h, gx, gy, candidate, dx, dy):
    """Steep cells bracketed by gentler crest/toe surfaces, not open hillsides.

    Trace each cell's uphill direction in physical metres. Both ends must have
    low *total* slope, not merely a flat contour along the sampled direction.
    Require substantial relief over a short run; missing shoulders at a data
    boundary, isolated spikes, and broad rounded hills do not supply evidence.
    """
    out = np.zeros_like(candidate)
    yy, xx = np.nonzero(candidate)
    if not len(yy):
        return out
    magnitude = np.hypot(gx[yy, xx], gy[yy, xx])
    ux, uy = gx[yy, xx]/magnitude, gy[yy, xx]/magnitude
    sides = []
    probe = PROFILE_PROBE_M
    gentle = math.tan(math.radians(SCARP_SHOULDER_DEG))
    for sign in (1, -1):
        distance = np.full(len(yy), np.nan)
        height = distance.copy()
        for r in np.arange(PROFILE_STEP_M, SCARP_REACH_M+PROFILE_STEP_M, PROFILE_STEP_M):
            x, y = xx+.5+sign*ux*r/dx, yy+.5+sign*uy*r/dy
            sx = (_height_at(h, x+probe/dx, y)-_height_at(h, x-probe/dx, y))/(2*probe)
            sy = (_height_at(h, x, y+probe/dy)-_height_at(h, x, y-probe/dy))/(2*probe)
            found = np.isnan(distance) & (np.hypot(sx, sy) <= gentle)
            distance[found], height[found] = r, _height_at(h, x[found], y[found])
        sides.append((distance, height))
    width = sides[0][0]+sides[1][0]
    drop = sides[0][1]-sides[1][1]
    out[yy, xx] = ((width <= SCARP_WIDTH_M) & (drop >= SCARP_DROP_M)
                   & (drop/width >= math.tan(math.radians(SCARP_AVERAGE_DEG))))
    return out


def _extend_faces(keep, support, dx, dy):
    """Fill nearby steep folds without flooding an attached hillside.

    Four-connected, physical-distance-limited growth cannot jump a road, an
    unsafe cell, or a flat crest/toe. Every new cell stays within FACE_REACH_M
    along the surface footprint of an independently qualified face.
    """
    distance = np.full(keep.shape, np.inf)
    distance[keep] = 0
    # Interior seeds cannot add coverage. Queue just the boundary touching
    # eligible missing cells, keeping fully covered tiles cheap to process.
    missing = support & ~keep
    boundary = np.zeros_like(keep)
    boundary[1:] |= keep[1:] & missing[:-1]
    boundary[:-1] |= keep[:-1] & missing[1:]
    boundary[:, 1:] |= keep[:, 1:] & missing[:, :-1]
    boundary[:, :-1] |= keep[:, :-1] & missing[:, 1:]
    todo = [(0.0, int(y), int(x)) for y, x in zip(*np.nonzero(boundary))]
    heapq.heapify(todo)
    rows, cols = keep.shape
    while todo:
        at, y, x = heapq.heappop(todo)
        if at != distance[y, x]:
            continue
        for yy, xx, step in ((y-1, x, dy), (y+1, x, dy), (y, x-1, dx), (y, x+1, dx)):
            to = at+step
            if (to <= FACE_REACH_M and 0 <= yy < rows and 0 <= xx < cols
                    and support[yy, xx] and to < distance[yy, xx]):
                distance[yy, xx] = to
                heapq.heappush(todo, (to, yy, xx))
    return np.isfinite(distance)


def qualifying_cells(heights, classes, blocked=None, step=GRID_M,
                     min_area=MIN_AREA_M2, min_slope=MIN_SLOPE_DEG,
                     city_buffer=CITY_BUFFER_M, support_slope=SUPPORT_SLOPE_DEG,
                     min_core_area=MIN_CORE_AREA_M2, min_footprint=MIN_FOOTPRINT_M2):
    """4-connected steep cells meeting the minimum footprint, after exclusions.

    heights/classes are matching vertex grids (south to north); blocked is a cell
    mask. Each component needs a sustained extreme core and enough connected
    steep support area, or a bounded escarpment with a sustained moderate core.
    A seed needs four known, dry, uncultivated corners. City
    exclusions precede labelling, so cities cannot join otherwise small patches.
    """
    h, c = np.asarray(heights, float), np.asarray(classes)
    if h.ndim != 2 or min(h.shape) < 2 or h.shape != c.shape:
        raise ValueError("heights and classes must be matching 2D vertex grids")
    dx, dy = (float(step), float(step)) if np.ndim(step) == 0 else map(float, step)
    if (min(dx, dy) <= 0 or min_area <= 0 or min_core_area <= 0 or min_footprint <= 0 or city_buffer < 0
            or not 0 < support_slope <= min_slope < 90):
        raise ValueError("invalid cliff selection thresholds")
    gx = ((h[:-1, 1:] - h[:-1, :-1]) + (h[1:, 1:] - h[1:, :-1])) / (2 * dx)
    gy = ((h[1:, :-1] - h[:-1, :-1]) + (h[1:, 1:] - h[:-1, 1:])) / (2 * dy)
    safe = np.isfinite(h) & np.isin(c, (10, 20, 30, 60, 100))
    city = _grow(c == 50, math.ceil(city_buffer / min(dx, dy)))
    safe &= ~city
    slope = np.hypot(gx, gy)
    extreme = slope >= math.tan(math.radians(min_slope))
    eligible = safe[:-1, :-1] & safe[:-1, 1:] & safe[1:, :-1] & safe[1:, 1:]
    if blocked is not None:
        if np.shape(blocked) != eligible.shape:
            raise ValueError("blocked must have one entry per cell")
        eligible &= ~np.asarray(blocked, bool)
    candidate = eligible & (slope >= math.tan(math.radians(support_slope)))
    keep, cores = np.zeros_like(candidate), np.zeros_like(candidate)
    required = math.ceil(min_footprint / (dx * dy))
    face_area = dx * dy * np.sqrt(1 + slope * slope)
    core_required = max(MIN_CORE_CELLS, math.ceil(min_core_area / (dx * dy)))
    for yy, xx in _components(candidate & extreme):
        if len(yy) >= core_required:
            cores[yy, xx] = True
    for yy, xx in _components(candidate):
        if len(yy) >= required and face_area[yy, xx].sum() >= min_area and cores[yy, xx].any():
            keep[yy, xx] = True
    # Gentler ribs can join a profile-confirmed face, but cannot qualify alone.
    # Keep explicit stricter support overrides useful for diagnostics/tests.
    face_angle = FACE_SUPPORT_DEG if support_slope == SUPPORT_SLOPE_DEG else support_slope
    support = eligible & (slope >= math.tan(math.radians(face_angle)))
    edge_angle = FACE_EDGE_DEG if support_slope == SUPPORT_SLOPE_DEG else support_slope
    edge_support = eligible & (slope >= math.tan(math.radians(edge_angle)))
    scarp = _bounded_scarps(h, gx, gy, support, dx, dy)
    scarp_core = scarp & (slope >= math.tan(math.radians(max(face_angle, SCARP_CORE_DEG))))
    substantial = np.zeros_like(scarp)
    for yy, xx in _components(scarp_core):
        if len(yy) >= MIN_CORE_CELLS:
            substantial[yy, xx] = True
    # Area belongs to the face, including short folds whose individual profile
    # misses the crest. Still require a full minimum footprint of direct profile
    # evidence, and measure core area only on that evidence, never on growth.
    extent = _extend_faces(scarp, edge_support, dx, dy)
    result = _extend_faces(keep, edge_support, dx, dy)
    for yy, xx in _components(extent):
        if (scarp[yy, xx].sum() >= required and face_area[yy, xx].sum() >= min_area
                and scarp_core[yy, xx].sum() >= core_required and substantial[yy, xx].any()):
            result[yy, xx] = True
    # Coarse shoreline labels sometimes call a 20 m high wall "water". Recover
    # only elevated, extreme cells next to a confirmed cliff and low coastal
    # water; these cells never supply the evidence needed to seed a cliff.
    shore_vertex = np.isfinite(h) & ~city & np.isin(c, (10, 20, 30, 60, 80, 100))
    shore = shore_vertex[:-1, :-1] & shore_vertex[:-1, 1:] & shore_vertex[1:, :-1] & shore_vertex[1:, 1:]
    wet = _grow(np.isfinite(h) & (c == 80) & (h <= SHORE_DRY_MIN_M), math.ceil(FACE_REACH_M/min(dx, dy)))
    shore &= wet[:-1, :-1] | wet[:-1, 1:] | wet[1:, :-1] | wet[1:, 1:]
    shore &= (c[:-1, :-1] == 80) | (c[:-1, 1:] == 80) | (c[1:, :-1] == 80) | (c[1:, 1:] == 80)
    corners = np.stack((h[:-1, :-1], h[:-1, 1:], h[1:, :-1], h[1:, 1:]))
    shore &= extreme & (np.ptp(corners, axis=0) >= SHORE_CELL_RELIEF_M) & (corners.max(axis=0) >= SHORE_CELL_RELIEF_M)
    if blocked is not None:
        shore &= ~np.asarray(blocked, bool)
    return _extend_faces(result, shore, dx, dy) if shore.any() and result.any() else result


def biome_at(koppen, classes):
    """Visual biome per point from the bundled climate map and land cover."""
    c = np.asarray(classes)
    b = np.zeros(c.shape, np.int32)
    if 4 <= koppen <= 7:
        b[:] = 1
    elif 1 <= koppen <= 3:
        b[:] = 2
    elif 17 <= koppen <= 30:
        b[:] = 3
    elif 8 <= koppen <= 16:
        b[c == 10] = 2
    return b


def rock_colors(biomes, rgb=None, family=cliffmaterial.NATURAL):
    """Encoded PBR vertex tint from local exposed ground; biome fallback if absent.

    Do not multiply by the photograph's baked-in shadows. Bound its luminance,
    retain mineral chromaticity, and leave the scan's internal variation intact.
    An all-zero sample means no usable cached imagery (not a black rock).
    """
    import cliffcolors as cc
    import clifftransition
    mineral = PALETTES[np.asarray(biomes)].copy()
    base = mineral / ALBEDO_HEADROOM
    if rgb is not None:
        rgb = np.asarray(rgb, float)
        if rgb.shape != base.shape or not np.isfinite(rgb).all():
            raise ValueError("terrain colours must be finite RGB, one per point")
        rgb = np.clip(rgb, 0, 255)
        grey = rgb @ np.array((0.2126, 0.7152, 0.0722))
        valid = (grey > 35) & (rgb[..., 1] < rgb[..., 0]*1.12) & (rgb[..., 2] < rgb[..., 0]*1.12)
        # Keep the inferred mineral hue, not vegetation's green excess. A
        # restrained chroma and broad luminance prior decouple photo exposure
        # from albedo; geographic variation is still supplied by the imagery.
        stone = rgb.copy()
        # Sandy/yellow minerals legitimately have more green than the average
        # of red and blue. The old channel clamp made that evidence magenta.
        # Reject vegetation above; preserve the accepted mineral hue here.
        luminance = stone @ np.array((.2126, .7152, .0722))
        stone = .7*stone + .3*luminance[..., None]
        target_light = 180 + .25*(np.clip(luminance, 130, 210)-180)
        stone *= (target_light/np.maximum(luminance, 1))[..., None]
        mineral = np.where(valid[..., None], stone, mineral)
        gain = cc.linear(np.clip(stone/255, 0, 1))/cc.linear(SCAN_MEDIAN_SRGB)/ALBEDO_HEADROOM
        base = np.where(valid[..., None], np.clip(gain, .12, 1)*255, base)
    # Preserve the established exposure, but calibrate hue against the actual
    # diffuse + material factor. A shared scan-relative gain was too pink on
    # the arid cut and exaggerated channel differences when the shader decoded
    # that gain as sRGB. Encode the corrected linear multiplier exactly once.
    reference = np.asarray(clifftransition.MEANS)[np.asarray(family)]
    weights = np.array((.2126, .7152, .0722))
    rendered = cc.linear(np.clip(base/255, 0, 1))*reference
    target = cc.linear(np.clip(mineral/255, 0, 1))
    target *= ((rendered @ weights)/np.maximum(target @ weights, 1e-8))[..., None]
    corrected = cc.srgb(np.clip(target/reference, 0, 1))*255
    calibrated = ((np.asarray(family) == cliffmaterial.NATURAL)
                  | (np.asarray(family) == cliffmaterial.ARID_CUT))
    return np.rint(np.clip(np.where(calibrated[..., None], corrected, base), 0, 255)).astype(np.uint8)


@dataclass
class Mesh:
    positions: np.ndarray
    tris: np.ndarray
    uvs: np.ndarray
    colors: np.ndarray
    normals: np.ndarray | None = None
    talus: list = field(default_factory=list)
    strips: list = field(default_factory=list)
    strip_inputs: list = field(default_factory=list)
    tri_materials: np.ndarray | None = None
    # Ephemeral build-only sampler; never serialized or sent to the engine.
    lod_ground: object = field(default=None, repr=False)
    uvs2: np.ndarray | None = None
    lod_scale: float = 1.0  # projected piece radius / original tile radius
    terrain_blend: bool = False  # UV1 is the opaque terrain rim; PBR uses UV0
    strip_colors: np.ndarray | None = None  # Build-only rock tint before satellite rim blending
    debug_rocks: list = field(default_factory=list)  # Recorded rock footprints for the map
    debug_cuts: list = field(default_factory=list)  # Confirmed road-cut detector runs
    debug_coverage: list = field(default_factory=list)  # Geographic detector runs, independent of render detail


def _surface_fade(h, touches, dx, dy):
    """Shorten horizontal blending where it would consume a near-vertical wall."""
    distance = np.where(touches == 4, np.inf, 0.0)
    # Retain the established transition on ordinary slopes. Very steep walls
    # need a shorter horizontal fade to avoid spending the whole wall inside it.
    wx = np.maximum(dx, np.abs(np.diff(h, axis=1))/math.tan(math.radians(STEEP_BLEND_DEG)))
    wy = np.maximum(dy, np.abs(np.diff(h, axis=0))/math.tan(math.radians(STEEP_BLEND_DEG)))
    for _ in range(math.ceil(EDGE_BLEND_M/min(dx, dy))):
        old = distance.copy()
        distance[:, 1:] = np.minimum(distance[:, 1:], old[:, :-1]+wx)
        distance[:, :-1] = np.minimum(distance[:, :-1], old[:, 1:]+wx)
        distance[1:] = np.minimum(distance[1:], old[:-1]+wy)
        distance[:-1] = np.minimum(distance[:-1], old[1:]+wy)
    fade = np.minimum(distance/EDGE_BLEND_M, 1.0)
    return fade*fade*(3-2*fade)


def _terrain_envelope(h, dx, dy):
    """Upper chord envelope for coarse terrain cells at differing grid phases.

    Linear ramps remain unchanged; concave bends account for terrain triangles
    bridging over the finer elevation samples. The diagonal span accommodates
    geographic/world-grid rotation without tying content to a particular anchor.
    """
    envelope = h.copy()
    for axis, spacing in ((1, dx), (0, dy)):
        src = np.moveaxis(envelope, axis, 0)
        out = src.copy()
        span = math.ceil(TERRAIN_SPAN_M/spacing)
        for left in range(1, span):
            for right in range(1, span-left+1):
                chord = (right*src[:-left-right]+left*src[left+right:])/(left+right)
                out[left:-right] = np.maximum(out[left:-right], chord)
        envelope = np.moveaxis(out, 0, axis)
    return envelope


def _cut_chart(h, envelope, cuts, dx, dy):
    """Clear terrain inside a cut while returning its perimeter to the ground."""
    touches = np.zeros(h.shape, np.uint8)
    for oy, ox in ((0, 0), (0, 1), (1, 0), (1, 1)):
        touches[oy:oy+cuts.shape[0], ox:ox+cuts.shape[1]] += cuts
    fade = _surface_fade(envelope, touches, dx, dy)
    # A raised perimeter exposes floating teeth along each lattice step.
    return h + (envelope-h)*fade


def displaced_surface(heights, keep, xs, ys, biomes, horizontal_scale=1.0, cuts=None, natural_family=cliffmaterial.NATURAL):
    """Shared terrain-following skin, including the actual faded/sunken rim."""
    if np.ndim(natural_family):
        # Only tiles crossing appearance boundaries take this path. Blend shared
        # vertices, but retain exact per-cell material ownership below.
        sums = None
        total = np.zeros(np.shape(heights), float)
        for family in np.unique(natural_family):
            weight = np.zeros(np.shape(heights), float)
            cells = np.asarray(natural_family) == family
            for oy, ox in ((0,0),(0,1),(1,0),(1,1)):
                weight[oy:oy+cells.shape[0], ox:ox+cells.shape[1]] += cells
            part = displaced_surface(heights, keep, xs, ys, biomes, horizontal_scale, cuts, int(family))
            weighted = [a * (weight[...,None] if a.ndim == 3 else weight) for a in part]
            sums = weighted if sums is None else [a+b for a,b in zip(sums, weighted)]
            total += weight
        return tuple(a / (total[...,None] if a.ndim == 3 else total) for a in sums)
    h = np.asarray(heights, float)
    touches = np.zeros(h.shape, np.uint8)
    for dy, dx in ((0, 0), (0, 1), (1, 0), (1, 1)):
        touches[dy:dy+keep.shape[0], dx:dx+keep.shape[1]] += keep
    dx, dy = float(xs[1]-xs[0]), float(ys[1]-ys[0])
    envelope = _terrain_envelope(h, dx*horizontal_scale, dy)
    # Start canyon faces on a continuous enclosing surface. Lifting individual
    # buried vertices after normal displacement stretches narrow DEM folds
    # into long fins; the envelope supplies a coherent crest/toe instead.
    base = envelope if natural_family == cliffmaterial.CANYON else h
    gy, gx = np.gradient(base, dy, dx*horizontal_scale)
    if natural_family == cliffmaterial.CANYON and (cuts is None or not cuts.any()):
        # Do not displace the sparse vertical endpoints of a dense XY lattice.
        # Normal offsets can cross adjacent columns at a rounded crest; later
        # subdivision only reproduces those folds as tall narrow curtains.
        # Keep a monotone backing surface; add relief AFTER surface refinement.
        surface = np.stack((*np.meshgrid(xs, ys), base.copy()), axis=-1)
        surface[...,2] += np.where(touches == 4, TERRAIN_CLEARANCE_M, -EDGE_SINK_M)
        return surface, gx, gy
    norm = np.sqrt(1 + gx*gx + gy*gy)
    xx, yy = np.meshgrid(xs, ys)
    vb = np.pad(biomes, ((0, 1), (0, 1)), mode='edge')
    across = np.where(np.abs(gx) >= np.abs(gy), yy, xx)
    relief = cliffmaterial.geometry_sample(across/cliffmaterial.WIDTH_M, base/cliffmaterial.height_span(natural_family),
                                  family=natural_family) * RELIEF_M[vb]
    if cuts is not None and cuts.any():
        # Shared vertices interpolate the change in relief. There is only one
        # skin, including where a confirmed cutting meets a natural cliff.
        cut_weight = np.zeros(h.shape, float)
        for oy, ox in ((0, 0), (0, 1), (1, 0), (1, 1)):
            cut_weight[oy:oy+cuts.shape[0], ox:ox+cuts.shape[1]] += cuts
        cut_weight /= np.maximum(touches, 1)
        cut_relief = cliffmaterial.sample(across/cliffmaterial.WIDTH_M, h/cliffmaterial.HEIGHT_M, roadcut=True)
        arid = cuts & (cliffmaterial.cut_family(biomes) == cliffmaterial.ARID_CUT)
        arid_weight = np.zeros(h.shape, float)
        for oy, ox in ((0, 0), (0, 1), (1, 0), (1, 1)):
            arid_weight[oy:oy+cuts.shape[0], ox:ox+cuts.shape[1]] += arid
        arid_weight /= np.maximum(touches, 1)
        if arid.any():
            arid_relief = cliffmaterial.sample(across/cliffmaterial.WIDTH_M, h/cliffmaterial.HEIGHT_M,
                                              family=cliffmaterial.ARID_CUT)
            relief = relief*(1-cut_weight)+cut_relief*(cut_weight-arid_weight)+arid_relief*arid_weight
        else:
            relief = relief*(1-cut_weight)+cut_relief*cut_weight
    fade = _surface_fade(h, touches, dx*horizontal_scale, dy)
    terrain_normal = np.stack((-gx, -gy, np.ones_like(h)), axis=-1)/norm[..., None]
    offset = (CLEARANCE_M + relief)*fade
    # Offset in physical metres along the surface normal, never in vertical
    # metres times slope. The latter turns a 3 m scan into ~18 m stretched fins.
    surface = np.stack((xx, yy, base), axis=-1)
    displacement = terrain_normal*offset[..., None]
    displacement[..., 0] /= horizontal_scale
    surface += displacement
    direction = terrain_normal.copy()
    direction[..., 0] /= horizontal_scale
    # Move outward, never lift vertically by slope (which creates tall fins).
    # Only the interior is protected; the true perimeter still meets the ground.
    for _ in range(math.ceil(CLEARANCE_SEARCH_M/CLEARANCE_STEP_M)):
        ground = _height_at(envelope, (surface[..., 0]-xs[0])/dx, (surface[..., 1]-ys[0])/dy) + TERRAIN_CLEARANCE_M
        buried = (touches == 4) & (surface[..., 2] < ground)
        if not buried.any():
            break
        trial = surface[buried] + direction[buried]*CLEARANCE_STEP_M
        trial_ground = _height_at(envelope, (trial[:, 0]-xs[0])/dx, (trial[:, 1]-ys[0])/dy) + TERRAIN_CLEARANCE_M
        improving = trial[:, 2]-trial_ground > surface[buried, 2]-ground[buried]
        update = surface[buried].copy()
        update[improving] = trial[improving]
        crossed = improving & (trial[:, 2] >= trial_ground)
        if crossed.any():
            lo, hi = surface[buried][crossed].copy(), trial[crossed].copy()
            for _ in range(CLEARANCE_REFINE_STEPS):
                mid = (lo+hi)/2
                z = _height_at(envelope, (mid[:, 0]-xs[0])/dx, (mid[:, 1]-ys[0])/dy) + TERRAIN_CLEARANCE_M
                outside = mid[:, 2] >= z
                hi[outside], lo[~outside] = mid[outside], mid[~outside]
            update[crossed] = hi
        # At a tight concave fold the outward normal can enter the opposite
        # wall. Conform to the envelope there instead of pushing deeper into it.
        update[~improving, 2] = ground[buried][~improving]
        surface[buried] = update
    ground = _height_at(envelope, (surface[..., 0]-xs[0])/dx, (surface[..., 1]-ys[0])/dy) + TERRAIN_CLEARANCE_M
    unresolved = (touches == 4) & (surface[..., 2] < ground)
    surface[unresolved, 2] = ground[unresolved]
    if cuts is not None and cuts.any():
        # A normal offset can fold a sub-metre-wide road cut back over itself
        # where its foot rounds into the shoulder. Preserve the heightfield's
        # horizontal ordering for cut vertices; its envelope already clears the
        # rendered terrain. Bounded vertical relief cannot create crossing fins.
        cut_surface = np.stack((xx, yy, h + offset), axis=-1)
        surface = surface*(1-cut_weight[..., None])+cut_surface*cut_weight[..., None]
    surface[..., 2] -= np.where(touches == 4, 0, EDGE_SINK_M)
    if natural_family == cliffmaterial.CANYON:
        # A road cut anywhere in the halo must not switch an entire canyon
        # tile back to endpoint displacement. Keep the established cut fit
        # only at vertices actually touched by those cut cells.
        natural = cut_weight == 0
        backing = np.stack((xx, yy, base + np.where(touches == 4, TERRAIN_CLEARANCE_M, -EDGE_SINK_M)),axis=-1)
        surface[natural] = backing[natural]
    return surface, gx, gy


def mesh(heights, keep, xs, ys, biomes, box, origin, rgb=None,
         horizontal_scale=1.0, project=None, clip_box=None, surface_data=None, road_segments=None, cuts=None,
         road_clearance=None, natural_family=cliffmaterial.NATURAL, cache_dir=None, strip_tint=False):
    """Continuous sampled rock surface with shared positions and smooth normals.

    A positive normal clearance separates the interior from the terrain, while
    only the outermost rim sinks. Clipping interpolates normals and colour as well
    as position/UV, so tile seams do not introduce lighting discontinuities.
    """
    from roadmesh import _clip_tris
    h = np.asarray(heights, float)
    dx, dy = float(xs[1]-xs[0]), float(ys[1]-ys[0])
    xx, yy = np.meshgrid(xs, ys)
    vb = np.pad(biomes, ((0, 1), (0, 1)), mode='edge')
    surface, gx, gy = (displaced_surface(h, keep, xs, ys, biomes, horizontal_scale, cuts, natural_family)
                       if surface_data is None else surface_data)
    import clifftransition
    rim_ground = np.stack((xx, yy, h-EDGE_SINK_M),axis=-1).reshape(-1,3)
    surface, rim_weights, rim_rgb, contour = clifftransition.prepare(surface, h, keep, dx, dy,
        horizontal_scale, EDGE_SINK_M, rgb, cache_dir, ground=rim_ground)
    # Normals of the displaced parametric surface, including horizontal motion.
    physical = surface.copy()
    physical[..., 0] *= horizontal_scale
    along_y, along_x = np.gradient(physical, dy, dx*horizontal_scale, axis=(0, 1))
    normals = np.cross(along_x, along_y)
    normals /= np.linalg.norm(normals, axis=-1, keepdims=True)
    normals, terrain_normals = clifftransition.normals(normals, h, rim_weights, dx*horizontal_scale, dy)
    vertex_rgb = None if rgb is None else np.pad(rgb, ((0, 1), (0, 1), (0, 0)), mode='edge')
    family_colors = {}
    p = surface.reshape(-1, 3)
    if project is not None:
        p = project(p)
        rim_ground = project(rim_ground)
    j, i = np.nonzero(keep)
    if not len(j):
        return None
    a = j*h.shape[1] + i
    b, c, d = a+1, a+h.shape[1]+1, a+h.shape[1]
    tris = np.stack((np.column_stack((a, b, c)), np.column_stack((a, c, d))), axis=1).reshape(-1, 3)
    # Vertical wall projection: no top-down stretching. Use two dominant planes,
    # and duplicate UVs only where the plane changes; geometric normals stay shared.
    axis = np.abs(gx[j, i]) >= np.abs(gy[j, i])
    out_p, out_t, out_uv, out_c, out_n, out_uv2 = [], [], [], [], [], []
    out_mat = []; out_strip_c = []
    cut_cells = np.zeros(len(j), bool) if cuts is None else cuts[j, i]
    families = np.where(cut_cells, cliffmaterial.cut_family(biomes[j, i]),
                        np.asarray(natural_family)[j,i] if np.ndim(natural_family) else natural_family)
    count = 0
    for family, use_y in ((f, a) for f in range(len(cliffmaterial.MATERIALS)) for a in (False, True)):
        cut_material = family in (cliffmaterial.LIMESTONE_CUT, cliffmaterial.ARID_CUT)
        selected = tris[np.repeat((axis == use_y) & (families == family), 2)]
        if not len(selected):
            continue
        u = (yy if use_y else xx) / cliffmaterial.WIDTH_M + cliffmaterial.UV_OFFSET[0]
        # Height and every PBR channel use the SAME undeformed surface chart.
        # Reprojecting at final z slides the albedo/normal away from its relief.
        chart_height = (_terrain_envelope(h, dx*horizontal_scale, dy)
                        if family == cliffmaterial.CANYON else h)
        v = chart_height / cliffmaterial.height_span(family) + cliffmaterial.UV_OFFSET[1]
        # Subtract whole repeats before float32 encoding. Neighbours have the
        # same texture phase without million-metre UV quantisation/shimmer.
        uv = np.column_stack((u.ravel()-math.floor(float(u.flat[0])),
                              v.ravel()-math.floor(float(v.flat[0]))))
        if family == cliffmaterial.CANYON:
            import cliffcolors
            chart_colors = cliffcolors.canyon_tint(vb, vertex_rgb)
        else:
            if family not in family_colors:
                colors = rock_colors(vb, vertex_rgb, family)
                family_colors[family] = np.concatenate(
                    (colors, np.full((*h.shape, 1), 255, np.uint8)), axis=-1)
            chart_colors = family_colors[family]
        # Contact attributes must be evaluated at full terrain weight. Interpolating
        # a partially faded endpoint leaves a bright lip and reintroduces relief.
        strip_source_colors = chart_colors.reshape(-1, 4)
        edge_colors, edge_uv = clifftransition.attributes(chart_colors, np.ones_like(rim_weights), rim_rgb, family)
        boundary = np.column_stack((uv, terrain_normals, edge_colors, edge_uv))
        chart_colors, rim_uv = clifftransition.attributes(chart_colors, rim_weights, rim_rgb, family)
        attributes = np.column_stack((uv, normals.reshape(-1, 3), chart_colors.reshape(-1, 4), rim_uv))
        if strip_tint:
            # Preserve the mineral tint through both contour and tile clipping.
            # Contact strips must not inherit the satellite/shadow-painted rim.
            attributes = np.column_stack((attributes, strip_source_colors))
            boundary = np.column_stack((boundary, strip_source_colors))
        material_points, selected, attributes = clifftransition.clip(p, selected, attributes, contour, rim_ground, boundary)
        if road_segments is not None:
            import cliffcuts
            if cut_material and road_clearance is not None:
                material_points, selected, attributes = cliffcuts.fit_clearance(material_points, selected, attributes, road_clearance)
            else:
                selected = cliffcuts.clear_triangles(material_points, selected, road_segments)
        if not len(selected):
            continue
        pp, tt, attrs, _ = _clip_tris(material_points, selected, attributes, np.zeros(len(selected), np.int32),
                                     clip_box if clip_box is not None else box)
        if not len(tt):
            continue
        used, inverse = np.unique(tt, return_inverse=True)
        attrs = attrs[used]
        nn = attrs[:, 2:5]
        nn /= np.linalg.norm(nn, axis=1, keepdims=True)
        out_p.append(pp[used] - np.asarray(origin))
        out_t.append(inverse.reshape(-1, 3) + count)
        out_mat.append(np.full(len(tt), family, np.int32))
        out_uv.append(attrs[:, :2]); out_n.append(nn); out_c.append(attrs[:, 5:9]); out_uv2.append(attrs[:, 9:11])
        if strip_tint:
            out_strip_c.append(attrs[:, 11:15])
        count += len(used)
    if not out_t:
        return None
    return Mesh(np.concatenate(out_p).astype(np.float32), np.concatenate(out_t).astype(np.int32),
                np.concatenate(out_uv).astype(np.float32), np.concatenate(out_c).astype(np.uint8),
                np.concatenate(out_n).astype(np.float32), tri_materials=np.concatenate(out_mat),
                uvs2=np.concatenate(out_uv2).astype(np.float32), terrain_blend=True,
                strip_colors=np.concatenate(out_strip_c).astype(np.uint8) if strip_tint else None)


def _fit_cut_toes(h, seed, allowed, dx, dy, *, fit_crest=False):
    """Finish a confirmed cut at its fine-resolution foot instead of a 4 m stair.

    Walk at most one detection-cell diagonal through safe, still-sloping ground.
    Flat benches stop the walk, so an upper lift cannot flow onto a lower one.
    This changes coverage on the existing mesh lattice, not its vertex density.
    Crest fitting uses the steeper cut-edge threshold and the same bounded walk;
    it cannot spill onto a gentle hill or cross a terrace bench.
    """
    gx = (np.diff(h, axis=1)[:-1]+np.diff(h, axis=1)[1:])/(2*dx)
    gy = (np.diff(h, axis=0)[:, :-1]+np.diff(h, axis=0)[:, 1:])/(2*dy)
    slope = np.hypot(gx, gy)
    steep = np.isfinite(slope) & (slope >= math.tan(math.radians(TOE_SLOPE_DEG)))
    # Road grading can leave a few flat samples inside a near-vertical cut.
    # Turning those into holes creates a separate sunken rim around each sample.
    # Fill only small, fully enclosed islands within the approved seed/domain;
    # open feet, benches, pavement exclusions and missing data remain boundaries.
    holes = seed & allowed & np.isfinite(slope) & ~steep
    for jj, ii in _components(holes):
        if len(jj)*dx*dy > CUT_SAMPLE_HOLE_M2:
            continue
        enclosed = True
        for oy, ox in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            y, x = jj+oy, ii+ox
            if (y < 0).any() or (x < 0).any() or (y >= seed.shape[0]).any() or (x >= seed.shape[1]).any():
                enclosed = False
                break
            if not (seed[y, x] & allowed[y, x] & (steep[y, x] | holes[y, x])).all():
                enclosed = False
                break
        if enclosed:
            steep[jj, ii] = True
    distance = np.where(seed & allowed & steep, 0., np.inf)
    # No lateral dilation: extend along the local gradient only. A crest walk
    # must stay steep at both ends of each step and shares the toe distance
    # budget, so alternating directions cannot walk indefinitely along a bank.
    neighbours = []
    if fit_crest:
        import cliffcuts
        crest_slope = math.tan(math.radians(cliffcuts.EDGE_SLOPE_DEG))
    for oy, ox in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        dst = (slice(max(0, -oy), min(seed.shape[0], seed.shape[0]-oy)),
               slice(max(0, -ox), min(seed.shape[1], seed.shape[1]-ox)))
        src = (slice(max(0, oy), min(seed.shape[0], seed.shape[0]+oy)),
               slice(max(0, ox), min(seed.shape[1], seed.shape[1]+ox)))
        step = math.hypot(ox*dx, oy*dy)
        uphill = (gx[dst]*ox*dx+gy[dst]*oy*dy) >= TOE_DOWNHILL_COS*slope[dst]*step
        if fit_crest:
            downhill = (gx[dst]*ox*dx+gy[dst]*oy*dy) <= -TOE_DOWNHILL_COS*slope[dst]*step
            uphill |= downhill & (slope[dst] >= crest_slope) & (slope[src] >= crest_slope)
        neighbours.append((dst, src, step, uphill))
    for _ in range(math.ceil(TOE_REACH_M/min(dx, dy))):
        old = distance.copy()
        for dst, src, step, uphill in neighbours:
            distance[dst] = np.minimum(distance[dst], np.where(uphill, old[src]+step, np.inf))
        distance[~(allowed & steep)] = np.inf
    return distance <= TOE_REACH_M


def _refine(xs, ys, keep, biomes, box, elev, color_at, shore=None, horizontal_scale=1.0, subdivisions=RENDER_SUBDIVISIONS):
    """Resample only the tile and a normal-calculation apron on a 0.5 m world grid.

    Detection's large halo remains coarse. Re-evaluating elevation, rather than
    subdividing coarse triangles, prevents rock sheets cutting through curved DEM
    cliffs. The apron keeps normals identical on both sides of a tile boundary.
    """
    n = subdivisions
    # Toe propagation, edge fade, the two-axis terrain envelope and outward
    # clearance all consume context. Keep the full dependency radius outside
    # the exported tile so adjoining tiles produce the same fitted boundary.
    context = TOE_REACH_M + EDGE_BLEND_M + 2*TERRAIN_SPAN_M + CLEARANCE_SEARCH_M + GRID_M/n
    ax = math.ceil(context/(GRID_M*horizontal_scale))
    ay = math.ceil(context/GRID_M)
    i0 = max(0, np.searchsorted(xs, box[0])-ax)
    i1 = min(len(xs)-1, np.searchsorted(xs, box[2])+ax)
    j0 = max(0, np.searchsorted(ys, box[1])-ay)
    j1 = min(len(ys)-1, np.searchsorted(ys, box[3])+ay)
    rx = xs[i0] + np.arange((i1-i0)*n+1)*(GRID_M/n)
    ry = ys[j0] + np.arange((j1-j0)*n+1)*(GRID_M/n)
    rk = np.repeat(np.repeat(keep[j0:j1, i0:i1], n, axis=0), n, axis=1)
    rb = np.repeat(np.repeat(biomes[j0:j1, i0:i1], n, axis=0), n, axis=1)
    xx, yy = np.meshgrid(rx, ry)
    h = np.asarray(elev(yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180)%360-180), float)
    rk &= np.isfinite(h[:-1, :-1]) & np.isfinite(h[1:, :-1]) & np.isfinite(h[:-1, 1:]) & np.isfinite(h[1:, 1:])
    if shore is not None:
        rs = np.repeat(np.repeat(shore[j0:j1, i0:i1], n, axis=0), n, axis=1)
        dry = (h[:-1, :-1] > SHORE_DRY_MIN_M) & (h[1:, :-1] > SHORE_DRY_MIN_M)
        dry &= (h[:-1, 1:] > SHORE_DRY_MIN_M) & (h[1:, 1:] > SHORE_DRY_MIN_M)
        rk &= ~rs | dry  # retain the wall above the waterline, never its submerged continuation
    rgb = None
    if color_at is not None:
        cx, cy = np.meshgrid((rx[:-1]+rx[1:])/2, (ry[:-1]+ry[1:])/2)
        rgb = color_at(cy/WORLD_M_PER_DEG, (cx/WORLD_M_PER_DEG+180)%360-180)
    return rx, ry, h, rk, rb, rgb


def build(frame, box, elev, surface, road_cells=(), widths=None, color_at=None, cache_dir=None, source_elev=None,
          features=(True, True, True), defer_strips=False):
    """Build one streamed tile, or None. Missing classification never permits rocks.

    color_at(lat, lon) can override the cached regional imagery sampler. No
    imagery download is added to the critical road build.
    """
    faces, rocks, decals = features
    if surface is None or not (faces or rocks):
        return None
    import climate
    import surface as surfacemod
    import roads
    import cliffcuts
    # A single geographic lattice across anchors AND geotiles. Local ENU grids
    # would give two neighbours different rock facets along their shared edge.
    # x is longitude in equatorial metres; physical x spacing shrinks with latitude.
    if abs(frame.lat0) >= 85.0:
        return None  # no supported land-cover/map cells at the poles
    scale = math.cos(math.radians(frame.lat0))
    step = (GRID_M * scale, GRID_M)
    ex = np.array((box[0], box[2], box[2], box[0]))
    ny = np.array((box[1], box[1], box[3], box[3]))
    la, lo, _ = frame.enu_to_geodetic(ex, ny, np.zeros(4))
    # Unwrap around this tile at the date line.
    lo = frame.lon0 + (lo-frame.lon0+180) % 360 - 180
    world_box = (lo.min()*WORLD_M_PER_DEG-GRID_M, la.min()*WORLD_M_PER_DEG-GRID_M,
                 lo.max()*WORLD_M_PER_DEG+GRID_M, la.max()*WORLD_M_PER_DEG+GRID_M)
    halo = (math.ceil(MIN_AREA_M2 / (step[0]*step[1])) * GRID_M
            + CITY_BUFFER_M / scale + GRID_M)
    xs = np.arange(math.floor((world_box[0]-halo)/GRID_M), math.ceil((world_box[2]+halo)/GRID_M)+1) * GRID_M
    ys = np.arange(math.floor((world_box[1]-halo)/GRID_M), math.ceil((world_box[3]+halo)/GRID_M)+1) * GRID_M
    xx, yy = np.meshgrid(xs, ys)
    lat, lon = yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180) % 360 - 180
    h = np.asarray(elev(lat, lon), float)
    classes, complete = surface.classes(lat, lon)
    if not complete:
        return None
    # Reject flat tiles before fetching further geometry or colour.
    gx = (np.diff(h, axis=1)[:-1]+np.diff(h, axis=1)[1:])/(2*step[0])
    gy = (np.diff(h, axis=0)[:, :-1]+np.diff(h, axis=0)[:, 1:])/(2*step[1])
    if not (np.hypot(gx, gy) >= math.tan(math.radians(cliffcuts.MIN_SLOPE_DEG))).any():
        return None
    cx, cy = np.meshgrid((xs[:-1]+xs[1:])/2, (ys[:-1]+ys[1:])/2)
    lat, lon = cy/WORLD_M_PER_DEG, (cx/WORLD_M_PER_DEG+180) % 360 - 180
    center_x, center_y, _ = frame.geodetic_to_enu(lat, lon, np.zeros(cx.shape))
    road_list = surfacemod._roads_list_enu(frame, road_cells, surface.urban, widths)
    segments = {str(k): (pts[:, :2], spec.width/2) for k, (_, spec, pts, _) in enumerate(road_list)}
    road_segments = roads._Segs(segments)
    center_points = np.stack((center_x, center_y), axis=-1)
    # Include the entire cell's footprint, not only its centre.
    blocked = road_segments.near(center_points.reshape(-1, 2),
                                        ROAD_BUFFER_M + GRID_M/math.sqrt(2)).reshape(cx.shape)
    centers = np.asarray(elev(lat, lon), float)
    blocked |= ~np.isfinite(centers)
    keep = qualifying_cells(h, classes, blocked, step=step)
    if faces and keep.any():
        import clifftransition
        keep = clifftransition.fill_gaps(keep, h, classes, blocked, step, CITY_BUFFER_M)
    cuts = np.zeros_like(keep)
    cut_domain = np.zeros_like(keep)
    urban_domain = np.zeros_like(keep)
    road_clearance = None
    groups = cliffcuts.road_groups(road_list, source_elev is not None)
    if groups:
        vx, vy, _ = frame.geodetic_to_enu(yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180)%360-180,
                                        np.zeros(h.shape))
        vertices = np.c_[vx.ravel(), vy.ravel()]
        cut_blocked = road_segments.near(center_points.reshape(-1, 2),
                                         cliffcuts.SHOULDER_M+GRID_M/math.sqrt(2)).reshape(cx.shape)
        cut_blocked |= ~np.isfinite(centers)
        backed = None
        if source_elev is not None:
            def road_height(xy):
                la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], np.zeros(len(xy)))
                return frame.geodetic_to_enu(la, lo, elev(la, lo))[2]
            xy_flat = center_points.reshape(-1, 2)
            bounds = (xy_flat.min(axis=0)-cliffcuts.REACH_M, xy_flat.max(axis=0)+cliffcuts.REACH_M)
            road_clearance = cliffcuts.RoadClearance(road_segments, road_height, bounds)
            vertex_z = frame.geodetic_to_enu(yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180)%360-180, h)[2]
            corners = (vertex_z[:-1, :-1], vertex_z[1:, :-1], vertex_z[:-1, 1:], vertex_z[1:, 1:])
            pavement = road_clearance.cell_blocked(xy_flat, np.minimum.reduce(corners).ravel(),
                np.maximum.reduce(corners).ravel(), cliffcuts.SHOULDER_M+GRID_M/math.sqrt(2)).reshape(cx.shape)
            source = np.asarray(source_elev(yy/WORLD_M_PER_DEG, (xx/WORLD_M_PER_DEG+180)%360-180), float)
            backed = cliffcuts.terrain_backed(source, h, *step)
            cut_blocked &= pavement | ~backed
            cut_blocked |= ~np.isfinite(centers)
        cuts, cut_domain, urban_domain = cliffcuts.select_roads(h, classes, xs, ys, scale, center_points,
            vertices, groups, cut_blocked, backed, refine_source=True)
        keep |= cuts
    if not keep.any():
        return None
    # Sample each climate cell once. Both sides of a tile boundary use the same
    # biome for a straddling rock, even at a climate boundary.
    kr = np.floor((90-lat)/climate.RES).astype(int)
    kc = np.floor((lon+180)/climate.RES).astype(int)
    keys, inverse = np.unique(kr*3600+kc, return_inverse=True)
    climate_values = np.array([climate.koppen_at(90-(k//3600+.5)*climate.RES,
                                               (k%3600+.5)*climate.RES-180) for k in keys])
    koppen = climate_values[inverse].reshape(cx.shape)
    b = np.zeros(cx.shape, np.int32)
    for k in np.unique(koppen):
        selected = koppen == k
        b[selected] = biome_at(k, classes[:-1, :-1][selected])
    rubble_color_at = color_at
    if color_at is None and cache_dir is not None:
        import cliffcolors
        color_at = lambda la, lo: cliffcolors.sample(cache_dir, la, lo)
        rubble_color_at = color_at
    import clifftalus
    coarse = (xs, ys, keep, classes, blocked, b)
    rubble_roads = (clifftalus.road_exclusion(frame, road_cells, surface.urban, widths, road_list)
                    if rocks else None)
    if not faces:
        # Only detection is shared with props. No render-grid resampling, relief,
        # PBR/tint work, contour meshing, canyon refinement or LODs without faces.
        result = Mesh(np.empty((0, 3)), np.empty((0, 3), np.int32),
                      np.empty((0, 2)), np.empty((0, 4)))
        if rocks:
            result.talus = clifftalus.rows(frame, box, *coarse, elev, rubble_color_at, road_segments=rubble_roads)
        return result
    import debuglayers
    detector_family = np.where(cuts, cliffmaterial.cut_family(b),
                               cliffmaterial.natural_family(cy/WORLD_M_PER_DEG, (cx/WORLD_M_PER_DEG+180)%360-180))
    owned = ((center_points[...,0] >= box[0]) & (center_points[...,0] < box[2]) &
             (center_points[...,1] >= box[1]) & (center_points[...,1] < box[3]))
    debug_coverage = debuglayers.footprint_grid(keep & owned, detector_family, xs, ys)
    debug_cuts = debuglayers.footprint_grid(cuts & owned, detector_family, xs, ys)
    shore = (classes[:-1, :-1] == 80) | (classes[:-1, 1:] == 80) | (classes[1:, :-1] == 80) | (classes[1:, 1:] == 80)
    natural_family = clifftypes.compact(cliffmaterial.natural_family(la, lo))
    coarse_canyon_grid = np.all(natural_family == cliffmaterial.CANYON) and not cuts.any()
    subdivisions = CANYON_RENDER_SUBDIVISIONS if coarse_canyon_grid else RENDER_SUBDIVISIONS
    xs, ys, h, keep, b, colors = _refine(xs, ys, keep, b, world_box, elev, color_at, shore, scale, subdivisions)
    # Transfer provenance with the same coarse-cell ownership as _refine.
    ci = np.clip(np.floor(((xs[:-1]+xs[1:])/2-coarse[0][0])/GRID_M).astype(int), 0, cuts.shape[1]-1)
    cj = np.clip(np.floor(((ys[:-1]+ys[1:])/2-coarse[1][0])/GRID_M).astype(int), 0, cuts.shape[0]-1)
    fine_cuts = cuts[np.ix_(cj, ci)] & keep
    if fine_cuts.any():
        # Use the same conservative terrain envelope for the cut's geometry and
        # UV/height chart. Correcting individual buried vertices afterwards can
        # stretch one tiny texture interval over several metres at a road edge.
        envelope = _terrain_envelope(h, (xs[1]-xs[0])*scale, ys[1]-ys[0])
        fx, fy = np.meshgrid(xs, ys)
        vx, vy, vz = frame.geodetic_to_enu(fy/WORLD_M_PER_DEG,
                                         (fx/WORLD_M_PER_DEG+180)%360-180, h)
        points = np.stack(((vx[:-1, :-1]+vx[1:, 1:])/2,
                           (vy[:-1, :-1]+vy[1:, 1:])/2), axis=-1).reshape(-1, 2)
        radius = math.hypot((xs[1]-xs[0])*scale, ys[1]-ys[0])/2
        if road_clearance is not None:
            corners = (vz[:-1, :-1], vz[1:, :-1], vz[:-1, 1:], vz[1:, 1:])
            road_blocked = road_clearance.cell_blocked(points, np.minimum.reduce(corners).ravel(),
                np.maximum.reduce(corners).ravel(), cliffcuts.SHOULDER_M+radius)
        else:
            road_blocked = road_segments.near(points, cliffcuts.SHOULDER_M+radius)
        domain = cut_domain[np.ix_(cj, ci)].copy()
        urban_land = urban_domain[np.ix_(cj, ci)]
        if urban_land.any() and source_elev is not None:
            source = np.asarray(source_elev(fy/WORLD_M_PER_DEG,
                                (fx/WORLD_M_PER_DEG+180)%360-180), float)
            # Source support is evaluated before the terrain envelope changes
            # the geometry. Only already-confirmed cuts can grow into this land.
            # Preserve support already established over a detection cell. The
            # finer check adds rounded edges; it must not punch holes into a
            # confirmed face when the source DEM has short, flatter samples.
            supported = backed[np.ix_(cj, ci)] | cliffcuts.terrain_backed(source, h,
                (xs[1]-xs[0])*scale, ys[1]-ys[0], min_slope_deg=TOE_SLOPE_DEG)
            domain |= urban_land & supported
        domain &= ~road_blocked.reshape(fine_cuts.shape)
        fitted = _fit_cut_toes(envelope, fine_cuts, domain,
                               (xs[1]-xs[0])*scale, ys[1]-ys[0], fit_crest=True)
        keep = (keep & ~fine_cuts) | fitted
        fine_cuts = fitted
        # Extend the chart through the final fitted footprint. Applying it
        # only to coarse seeds introduces artificial steps
        # at the seed boundary and excludes the very toe cells being recovered.
        h = _cut_chart(h, envelope, fitted, (xs[1]-xs[0])*scale, ys[1]-ys[0])
    # Keep missing elevations invalid through toe fitting, then prevent them
    # from poisoning finite-difference normals of neighbouring valid cells.
    h = np.nan_to_num(h, nan=0, posinf=0, neginf=0)
    fy, fx = np.meshgrid((ys[:-1]+ys[1:])/2, (xs[:-1]+xs[1:])/2, indexing="ij")
    natural_family = clifftypes.compact(cliffmaterial.natural_family(
        fy/WORLD_M_PER_DEG, (fx/WORLD_M_PER_DEG+180)%360-180))
    surface_data = displaced_surface(h, keep, xs, ys, b, scale, fine_cuts, natural_family)
    rubble = []
    if rocks:
        skin_at = clifftalus.surface_sampler(xs, ys, h, surface_data[0])
        rubble = clifftalus.rows(frame, box, *coarse, elev, rubble_color_at, skin_at, road_segments=rubble_roads, families=detector_family)

    def project(p):
        return np.column_stack(frame.geodetic_to_enu(p[:, 1]/WORLD_M_PER_DEG,
                               (p[:, 0]/WORLD_M_PER_DEG+180) % 360-180, p[:, 2]))
    result = mesh(h, keep, xs, ys, b, world_box, (box[0], box[1], 0), colors,
                  horizontal_scale=scale, project=project, clip_box=box, surface_data=surface_data,
                  road_segments=road_segments, cuts=fine_cuts, road_clearance=road_clearance,
                  natural_family=natural_family, cache_dir=cache_dir, strip_tint=decals)
    if result is not None:
        envelope = _terrain_envelope(h, (xs[1]-xs[0])*scale, ys[1]-ys[0])
        def lod_ground(points):
            world = points + np.array((box[0], box[1], 0.0))
            la, lo, _ = frame.enu_to_geodetic(*world.T)
            lo = frame.lon0 + (lo-frame.lon0+180) % 360-180
            z = _height_at(envelope, (lo*WORLD_M_PER_DEG-xs[0])/(xs[1]-xs[0]),
                           (la*WORLD_M_PER_DEG-ys[0])/(ys[1]-ys[0]))
            return frame.geodetic_to_enu(la, lo, z)[2]
        result.lod_ground = lod_ground
        if np.any(natural_family == cliffmaterial.CANYON):
            import cliffdetail
            result = cliffdetail.refine_canyon(result, full_relief=True)
        result.debug_coverage = debug_coverage
        result.debug_cuts = debug_cuts
        result.talus = rubble
        result.debug_rocks = clifftalus.debug_footprints(frame, rubble)
        if decals:
            import cliffstrips
            def strip_ground(xy):
                world = np.column_stack((xy + np.array((box[0], box[1])), np.zeros(len(xy))))
                la, lo, _ = frame.enu_to_geodetic(*world.T)
                z = elev(la, lo)
                return frame.geodetic_to_enu(la, lo, z)[2]
            try:
                if defer_strips:
                    result.strip_inputs = cliffstrips.prepare(result, strip_ground)
                else:
                    result.strips = cliffstrips.tile_parts(result, strip_ground)
            except Exception:
                log.exception("cliff contact strips omitted; keeping parent cliff")
                import revisit
                revisit.fault()
    return result


def tile_mesh(*args, **kwargs):
    """Optional scenery must not prevent the drivable road tile from loading."""
    try:
        return build(*args, **kwargs)
    except Exception:
        log.exception("cliff rocks: tile omitted")
        import revisit
        revisit.fault()
        return None


def write(path, rock, collision=None):
    """Write the rock skin with vertex tint through the existing compiled-mesh path."""
    import cdae
    import clifflod
    import clifftransition
    materials = clifftransition.MATERIALS if rock.terrain_blend else cliffmaterial.MATERIALS
    return cdae.write(path, rock.positions, rock.tris, uvs=rock.uvs,
                      material=list(materials) if rock.tri_materials is not None else MATERIAL,
                      tri_materials=rock.tri_materials, colors=rock.colors, normals=rock.normals, object_name="cliff",
                      collisions=None if collision is None else [(collision["positions"], collision["tris"],
                                                               collision["tri_materials"])],
                      split_meshes=True, lods=[(px*rock.lod_scale, mesh) for px, mesh in clifflod.levels(rock)
                            if px*rock.lod_scale > cdae.LOD_TOP_SIZE],
                      uvs2=rock.uvs2, tangent_uvs=rock.uvs if rock.terrain_blend else rock.uvs2)


STREAM_TRIANGLES = 16384
STREAM_PARTS_MAX = 64  # also roads.lua: bounded protocol fields cliff, cliff2..cliff64


def stream_parts(rock):
    """Small spatial pieces with exact shared positions, tint, UVs and normals.

    Partition before LOD generation so shared batch edges remain locked at every
    detail. The native kernel only assigns faces; it never changes the surface.
    """
    if not len(rock.tris):
        return
    import fast
    fast.require(144, "cliff_stream_parts")
    groups = fast._ef.cliff_stream_parts(np.ascontiguousarray(rock.positions, np.float64),
        np.ascontiguousarray(rock.tris, np.int64), STREAM_TRIANGLES, STREAM_PARTS_MAX)
    if not groups:
        return
    radius = np.linalg.norm(np.ptp(rock.positions, axis=0))
    for faces in groups:
        faces = np.asarray(faces, np.int64)
        vertices, triangles = np.unique(rock.tris[faces], return_inverse=True)
        attrs = {name: None if getattr(rock, name) is None else getattr(rock, name)[vertices]
                 for name in ("positions", "normals", "uvs", "uvs2", "colors")}
        scale = np.linalg.norm(np.ptp(attrs["positions"], axis=0))/max(radius, 1e-9)
        yield replace(rock, **attrs, tris=triangles.reshape(-1,3), lod_scale=rock.lod_scale*scale,
                      tri_materials=None if rock.tri_materials is None else rock.tri_materials[faces])
