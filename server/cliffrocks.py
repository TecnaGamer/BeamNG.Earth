"""Continuous rock relief on sustained natural cliffs, streamed with road-tile content.

The mesh is a fitted rock skin, not a random boulder scatter. Every accepted ground
cell is covered; adjoining facets share their boundary positions. Selection uses
both face area and footprint, so narrow coastal walls can qualify without letting
a single very tall DEM spike qualify.
Unknown land cover fails closed. Visibility is controlled by in-game settings.
"""
from cliffsettings import CLIFF_KERNEL
from dataclasses import dataclass, field, replace
import logging
import math
import dmath

import numpy as np
import dnp
import clifftypes
import cliffmaterial
from runtimeprofiler import step as profile_step

log = logging.getLogger("earthd.cliffrocks")


MATERIAL = "bne_cliff_strata"
GRID_M = 4.0
RENDER_SUBDIVISIONS = 8    # 0.5 m terrain/heightmap samples; detection remains coarse
CLEARANCE_M = 0.35
EDGE_BLEND_M = 2.0
TERRAIN_SPAN_M = 2.0 * math.sqrt(2.0)  # diagonal of the game's 2 m terrain cell
CLEARANCE_STEP_M = 0.25
CLEARANCE_SEARCH_M = 2 * TERRAIN_SPAN_M
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
# sustained core AND a compact crest-to-toe profile in that case. Bare-ground
# scarps use the gentler core below; vegetation keeps the 50-degree requirement.
SCARP_CORE_DEG = 50.0
BARE_SCARP_AVERAGE_DEG = 30.0  # Rounded bare shoulders; a sustained core is still required.
BARE_SCARP_CORE_DEG = 40.0
BARE_SCARP_DROP_M = 6.0
BARE_SCARP_SIZE_RATIO = 0.5
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
TOE_REACH_M = GRID_M * math.sqrt(2.0)  # halo budget matching cliffcuts::TOE_REACH_M
TOE_SLOPE_DEG = 20.0  # shared fitting-domain threshold; cliffcuts owns the walk
ALBEDO_HEADROOM = 2.0  # matched by the material factor; vertex colours can only attenuate
SCAN_MEDIAN_SRGB = np.array((155, 152, 147), float) / 255
WORLD_M_PER_DEG = 111320.0
# Rock colour and relief in metres: warm desert, subdued humid rock,
# cooler alpine/boreal rock. These are visual priors, not geological data.
PALETTES = np.array(((245, 245, 245), (255, 235, 205), (238, 238, 232), (235, 245, 255)), float)
RELIEF_M = np.array((0.7, 1.0, 0.55, 1.15))


def _grow(mask, radius):
    import fast
    fast.require(CLIFF_KERNEL, "cliff_grow")
    return fast._ef.cliff_grow(np.ascontiguousarray(mask, bool), int(radius))


def _components(mask):
    import fast
    fast.require(CLIFF_KERNEL, "cliff_components")
    for cells in fast._ef.cliff_components(np.ascontiguousarray(mask, bool)):
        yield np.asarray(np.unravel_index(cells, mask.shape))


def _height_at(h, x, y):
    """Bilinear vertex-grid sample; missing/outside profile data fails closed."""
    valid = (x >= 0) & (y >= 0) & (x <= h.shape[1]-1) & (y <= h.shape[0]-1)
    ix = np.clip(np.floor(x).astype(int), 0, h.shape[1]-2)
    iy = np.clip(np.floor(y).astype(int), 0, h.shape[0]-2)
    fx, fy = x-ix, y-iy
    z = ((h[iy, ix]*(1-fx) + h[iy, ix+1]*fx)*(1-fy)
         + (h[iy+1, ix]*(1-fx) + h[iy+1, ix+1]*fx)*fy)
    return np.where(valid, z, np.nan)


def _extend_faces(keep, support, dx, dy):
    import fast
    fast.require(CLIFF_KERNEL, "cliff_extend")
    return fast._ef.cliff_extend(np.ascontiguousarray(keep, bool), np.ascontiguousarray(support, bool), float(dx), float(dy))


def qualifying_cells(heights, classes, blocked=None, step=GRID_M,
                     min_area=MIN_AREA_M2, min_slope=MIN_SLOPE_DEG,
                     city_buffer=CITY_BUFFER_M, support_slope=SUPPORT_SLOPE_DEG,
                     min_core_area=MIN_CORE_AREA_M2, min_footprint=MIN_FOOTPRINT_M2):
    """Native detector on matching vertex grids; ownership is clipped after detection."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff_select")
    h = np.ascontiguousarray(heights, dtype=float)
    c = np.ascontiguousarray(classes, dtype=np.uint8)
    if h.ndim != 2 or min(h.shape) < 2 or h.shape != c.shape:
        raise ValueError("heights and classes must be matching 2D vertex grids")
    mask = np.zeros((h.shape[0]-1, h.shape[1]-1), bool) if blocked is None else np.ascontiguousarray(blocked, bool)
    dx, dy = (float(step), float(step)) if np.ndim(step) == 0 else map(float, step)
    return fast._ef.cliff_select(h, c, mask, dx, dy, float(min_area), float(min_slope),
                                 float(city_buffer), float(support_slope), float(min_core_area), float(min_footprint))


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


def rock_colors(biomes, rgb=None, family=cliffmaterial.NATURAL, *, palette=None):
    """Native satellite mineral tint, calibrated against the actual material means."""
    import fast
    import clifftransition
    fast.require(CLIFF_KERNEL, "cliff_rock_tints")
    b = np.asarray(biomes, dtype=np.int32)
    colors = np.zeros((*b.shape, 3)) if rgb is None else np.asarray(rgb, float)
    if colors.shape != (*b.shape, 3):
        raise ValueError("terrain colours must be finite RGB, one per point")
    families = np.broadcast_to(np.asarray(family, np.int16), b.shape)
    result = fast._ef.cliff_rock_tints(np.ascontiguousarray(b.ravel()),
        np.ascontiguousarray(colors.reshape(-1,3)), np.ascontiguousarray(families.ravel()),
        np.ascontiguousarray(clifftransition.MEANS, float), palette)
    return result.reshape(*b.shape, 3)


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


def _terrain_envelope(h, dx, dy):
    """Upper chord envelope for coarse terrain cells at differing grid phases.

    Linear ramps remain unchanged; concave bends account for terrain triangles
    bridging over the finer elevation samples. The diagonal span accommodates
    geographic/world-grid rotation without tying content to a particular anchor.
    """
    import fast
    return fast._ef.cliff_terrain_envelope(np.ascontiguousarray(h, float), float(dx), float(dy))


def _cut_chart(h, envelope, cuts, dx, dy):
    """Clear terrain inside a cut while returning its perimeter to the ground."""
    import fast
    return fast._ef.cliff_cut_chart(np.ascontiguousarray(h, float),
        np.ascontiguousarray(envelope, float), np.ascontiguousarray(cuts, bool), float(dx), float(dy))


@profile_step('Cliff relief')
def displaced_surface(heights, keep, xs, ys, biomes, horizontal_scale=1.0, cuts=None, natural_family=cliffmaterial.NATURAL):
    """Native terrain-fitting relief; sample the whole halo before tile clipping."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff_surface")
    h = np.ascontiguousarray(heights, float)
    k = np.ascontiguousarray(keep, bool)
    c = np.zeros(k.shape, bool) if cuts is None else np.ascontiguousarray(cuts, bool)
    family = np.ascontiguousarray(np.broadcast_to(natural_family, k.shape), np.int16)
    maps = np.ascontiguousarray(np.stack([cliffmaterial._heightmap(f) for f in range(4)]), float)
    points, gx, gy = fast._ef.cliff_surface(h, k, c, np.ascontiguousarray(biomes, np.int32), family, maps,
        float(xs[0]), float(ys[0]), float(xs[1]-xs[0]), float(ys[1]-ys[0]), float(horizontal_scale))
    return points.reshape(*h.shape, 3), gx, gy


@profile_step('Cliff mesh assembly')
def mesh(heights, keep, xs, ys, biomes, box, origin, rgb=None,
         horizontal_scale=1.0, project=None, clip_box=None, surface_data=None, road_segments=None, cuts=None,
         road_clearance=None, natural_family=cliffmaterial.NATURAL, cache_dir=None, strip_tint=False):
    """Continuous sampled rock surface with shared positions and smooth normals.

    A positive normal clearance separates the interior from the terrain, while
    only the outermost rim sinks. Clipping interpolates normals and colour as well
    as position/UV, so tile seams do not introduce lighting discontinuities.
    """
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
    import fast
    normals = fast._ef.cliff_grid_normals(np.ascontiguousarray(surface, float), dx, dy, horizontal_scale)
    normals, terrain_normals = clifftransition.normals(normals, h, rim_weights, dx*horizontal_scale, dy)
    vertex_rgb = None if rgb is None else np.pad(rgb, ((0, 1), (0, 1), (0, 0)), mode='edge')
    family_attributes = {}
    p = surface.reshape(-1, 3)
    if project is not None:
        p = project(p)
        rim_ground = project(rim_ground)
    cut_cells = np.zeros(keep.shape, bool) if cuts is None else cuts
    families = np.where(cut_cells, cliffmaterial.cut_family(biomes), natural_family)
    groups = fast._ef.cliff_mesh_layout(np.ascontiguousarray(keep, bool),
        np.ascontiguousarray(gx, float), np.ascontiguousarray(gy, float),
        np.ascontiguousarray(np.broadcast_to(families, keep.shape), np.int16))
    out_p, out_t, out_uv, out_c, out_n, out_uv2 = [], [], [], [], [], []
    out_mat = []; out_strip_c = []
    count = 0
    for family, use_y, selected in groups:
        cut_material = family in (cliffmaterial.LIMESTONE_CUT, cliffmaterial.ARID_CUT)
        uv = fast._ef.cliff_surface_uv(np.ascontiguousarray(h),
            np.ascontiguousarray(xs, float), np.ascontiguousarray(ys, float), use_y,
            family == cliffmaterial.CANYON, horizontal_scale, cliffmaterial.WIDTH_M,
            cliffmaterial.height_span(family), cliffmaterial.UV_OFFSET)
        if family not in family_attributes:
            if family == cliffmaterial.CANYON:
                import cliffcolors
                colors = cliffcolors.canyon_tint(vb, vertex_rgb)
            else:
                colors = rock_colors(vb, vertex_rgb, family)
                colors = np.concatenate(
                    (colors, np.full((*h.shape, 1), 255, np.uint8)), axis=-1)
            # Both chart directions share tint and terrain blending. Compute
            # these once per family; only the surface UVs depend on direction.
            # Contact endpoints need full terrain weight to avoid a bright lip.
            edge_colors, edge_uv = clifftransition.attributes(colors, np.ones_like(rim_weights), rim_rgb, family)
            chart_colors, rim_uv = clifftransition.attributes(colors, rim_weights, rim_rgb, family)
            family_attributes[family] = (colors.reshape(-1, 4), edge_colors, edge_uv, chart_colors, rim_uv)
        strip_source_colors, edge_colors, edge_uv, chart_colors, rim_uv = family_attributes[family]
        boundary = np.column_stack((uv, terrain_normals, edge_colors, edge_uv))
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
        pp, tt, attrs = fast._ef.cliff_mesh_clip(np.ascontiguousarray(material_points, float),
            np.ascontiguousarray(selected, np.int64), np.ascontiguousarray(attributes, float),
            clip_box if clip_box is not None else box)
        if not len(tt):
            continue
        pp, tt, attrs = fast._ef.cliff_mesh_finish(np.ascontiguousarray(pp, float),
            np.ascontiguousarray(tt, np.int64), np.ascontiguousarray(attrs, float), tuple(origin), count)
        out_p.append(pp)
        out_t.append(tt)
        out_mat.append(np.full(len(tt), family, np.int32))
        out_uv.append(attrs[:, :2]); out_n.append(attrs[:, 2:5]); out_c.append(attrs[:, 5:9]); out_uv2.append(attrs[:, 9:11])
        if strip_tint:
            out_strip_c.append(attrs[:, 11:15])
        count += len(pp)
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
    import fast
    return fast._ef.cliff_cut_toes(np.ascontiguousarray(h, float),
        np.ascontiguousarray(seed, bool), np.ascontiguousarray(allowed, bool), float(dx), float(dy), fit_crest)


@profile_step('Cliff terrain sampling')
def _refine(xs, ys, keep, biomes, box, elev, color_at, shore=None, horizontal_scale=1.0, subdivisions=RENDER_SUBDIVISIONS):
    """Resample only the tile and a normal-calculation apron on a fine world grid.

    Detection's large halo remains coarse. Re-evaluating elevation, rather than
    subdividing coarse triangles, prevents rock sheets cutting through curved DEM
    cliffs. The apron keeps normals identical on both sides of a tile boundary.
    """
    import fast
    context = TOE_REACH_M + EDGE_BLEND_M + 2*TERRAIN_SPAN_M + CLEARANCE_SEARCH_M + GRID_M/subdivisions
    def sample_elevation(lat, lon):
        return np.asarray(elev(lat, lon), float)
    return fast._ef.cliff_refine_grid(np.ascontiguousarray(xs, float), np.ascontiguousarray(ys, float),
        np.ascontiguousarray(keep, bool), np.ascontiguousarray(biomes, np.int64), tuple(box), subdivisions,
        GRID_M, context, horizontal_scale, WORLD_M_PER_DEG, SHORE_DRY_MIN_M, sample_elevation, color_at,
        None if shore is None else np.ascontiguousarray(shore, bool))


@profile_step('Cliff generation')
def build(frame, box, elev, surface, road_cells=(), widths=None, color_at=None, cache_dir=None, source_elev=None,
          features=(True, True, True), defer_strips=False, settings=None):
    """Build one streamed tile, or None. Missing classification never permits rocks.

    color_at(lat, lon) can override the cached regional imagery sampler. No
    imagery download is added to the critical road build.
    """
    from cliffsettings import SETTINGS
    settings = SETTINGS if settings is None else settings
    if not settings.enabled:
        return None
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
    scale = dmath.cos(math.radians(frame.lat0))
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
    if not (dnp.hypot(gx, gy) >= dmath.tan(math.radians(cliffcuts.MIN_SLOPE_DEG))).any():
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
    keep = (qualifying_cells(h, classes, blocked, step=step) if settings.natural
            else np.zeros(blocked.shape, bool))
    if faces and keep.any():
        import clifftransition
        keep = clifftransition.fill_gaps(keep, h, classes, blocked, step, CITY_BUFFER_M)
    cuts = np.zeros_like(keep)
    cut_domain = np.zeros_like(keep)
    urban_domain = np.zeros_like(keep)
    road_clearance = None
    groups = cliffcuts.road_groups(road_list, source_elev is not None) if settings.road_cuts else []
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
    from cliffsettings import DEBUG
    debug_coverage, debug_cuts = [], []
    detector_family = np.where(cuts, cliffmaterial.cut_family(b),
                               cliffmaterial.natural_family(cy/WORLD_M_PER_DEG, (cx/WORLD_M_PER_DEG+180)%360-180))
    if DEBUG:
        import debuglayers
        owned = ((center_points[...,0] >= box[0]) & (center_points[...,0] < box[2]) &
                 (center_points[...,1] >= box[1]) & (center_points[...,1] < box[3]))
        debug_coverage = debuglayers.footprint_grid(keep & owned, detector_family, xs, ys)
        debug_cuts = debuglayers.footprint_grid(cuts & owned, detector_family, xs, ys)
    shore = (classes[:-1, :-1] == 80) | (classes[:-1, 1:] == 80) | (classes[1:, :-1] == 80) | (classes[1:, 1:] == 80)
    natural_family = clifftypes.compact(cliffmaterial.natural_family(la, lo))
    coarse_canyon_grid = np.all(natural_family == cliffmaterial.CANYON) and not cuts.any()
    near_subdivisions, canyon_subdivisions, lod_threshold_scale = settings.profile
    subdivisions = canyon_subdivisions if coarse_canyon_grid else near_subdivisions
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
            import fast
            return fast._ef.cliff_lod_ground(np.ascontiguousarray(points, float),
                np.ascontiguousarray(envelope, float), frame.origin, frame.rot,
                (box[0], box[1]), (xs[0], ys[0], xs[1]-xs[0], ys[1]-ys[0]), frame.lon0)
        result.lod_ground = lod_ground
        result.lod_scale *= lod_threshold_scale
        if np.any(natural_family == cliffmaterial.CANYON):
            import cliffdetail
            result = cliffdetail.refine_canyon(result, full_relief=True)
        result.debug_coverage = debug_coverage
        result.debug_cuts = debug_cuts
        result.talus = rubble
        result.debug_rocks = clifftalus.debug_footprints(frame, rubble) if DEBUG else []
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


def write(path, rock):
    """Write the rock skin with vertex tint through the existing compiled-mesh path."""
    import cdae
    import clifflod
    import clifftransition
    if len(rock.positions) > cdae.VERTS_MAX:
        raise ValueError("partition cliffs with stream_parts before writing engine meshes")
    materials = clifftransition.MATERIALS if rock.terrain_blend else cliffmaterial.MATERIALS
    return cdae.write(path, rock.positions, rock.tris, uvs=rock.uvs,
                      material=list(materials) if rock.tri_materials is not None else MATERIAL,
                      tri_materials=rock.tri_materials, colors=rock.colors, normals=rock.normals, object_name="cliff",
                      lods=[(px*rock.lod_scale, mesh) for px, mesh in clifflod.levels(rock)
                            if px*rock.lod_scale > cdae.LOD_TOP_SIZE],
                      uvs2=rock.uvs2, tangent_uvs=rock.uvs if rock.terrain_blend else rock.uvs2)


STREAM_TRIANGLES = 16384
from cliffscene import FACE_PARTS as STREAM_PARTS_MAX


def stream_parts(rock):
    """Small spatial pieces with exact shared positions, tint, UVs and normals.

    Partition before LOD generation so shared batch edges remain locked at every
    detail. The native kernel only assigns faces; it never changes the surface.
    """
    if not len(rock.tris):
        return
    import fast
    fast.require(CLIFF_KERNEL, "cliff_stream_parts")
    groups = fast._ef.cliff_stream_parts(np.ascontiguousarray(rock.positions, np.float64),
        np.ascontiguousarray(rock.tris, np.int64), STREAM_TRIANGLES, STREAM_PARTS_MAX)
    if not groups:
        return
    radius = dnp.norm(np.ptp(rock.positions, axis=0))
    for faces in groups:
        faces = np.asarray(faces, np.int64)
        vertices, triangles = np.unique(rock.tris[faces], return_inverse=True)
        attrs = {name: None if getattr(rock, name) is None else getattr(rock, name)[vertices]
                 for name in ("positions", "normals", "uvs", "uvs2", "colors")}
        scale = dnp.norm(np.ptp(attrs["positions"], axis=0))/max(radius, 1e-9)
        yield replace(rock, **attrs, tris=triangles.reshape(-1,3), lod_scale=rock.lod_scale*scale,
                      tri_materials=None if rock.tri_materials is None else rock.tri_materials[faces])
