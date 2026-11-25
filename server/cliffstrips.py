"""Bounded server generation of projected cliff-contact strips.

UV0 is a fixed planar projection. UV1 carries height-erode coverage, including
both end caps. This module has no engine calls or per-frame generation work.
"""
from cliffsettings import CLIFF_KERNEL
from dataclasses import dataclass
import numpy as np
import dnp
from runtimeprofiler import step as profile_step

HALF_WIDTH_M = 2.0
END_FADE_M = 2.0
SAMPLE_M = 0.80
SURFACE_OFFSET_M = 0.08
GROUND_CLEARANCE_M = 0.035
TEXTURE_REPEAT_M = 4.0
LOOKUP_WIDTH = 1024
MAX_TRIANGLES = 12000


def unit(v):
    return v / np.maximum(dnp.norm(v, axis=-1, keepdims=True), 1e-12)


def basis(normal):
    normal = unit(np.asarray(normal, float))
    reference = np.array([0., 0., 1.]) if abs(normal[2]) < .95 else np.array([0., 1., 0.])
    right = unit(np.cross(reference, normal))
    return np.stack((right, np.cross(normal, right), normal))


class Projector:
    """Shared projection chart and receiver data for native strip clipping.

    Native clipping constructs its own receiver planes and spatial index. Keep
    only the chart needed for shading and UVs here, without duplicating that work.
    """
    def __init__(self, triangles, normal, origin, vertex_normals=None):
        self.axes = basis(normal)
        self.origin = np.asarray(origin, float)
        self.triangles = np.asarray(triangles, float)
        self.local = dnp.matmul(self.triangles-self.origin, self.axes.T)
        self.vertex_normals = vertex_normals


@dataclass
class Strip:
    positions: np.ndarray
    tris: np.ndarray
    uvs: np.ndarray
    uvs2: np.ndarray
    normals: np.ndarray
    receiver: np.ndarray
    threshold: np.ndarray


def build(centerline, projector, half_width=HALF_WIDTH_M, spacing=SAMPLE_M,
          end_fade=END_FADE_M, repeat=TEXTURE_REPEAT_M):
    """Native receiver clipping, shading, UVs and shared-vertex mesh assembly."""
    import fast
    fast.require(CLIFF_KERNEL, "cliff_strip_project")
    if min(half_width, spacing, end_fade, repeat) <= 0:
        raise ValueError('Strip dimensions and texture repeat must be positive')
    local = dnp.matmul(np.asarray(centerline, float)-projector.origin, projector.axes.T)
    domains = fast._ef.cliff_strip_domains(np.ascontiguousarray(local.reshape(-1,3)),
                                          half_width, spacing, end_fade)
    if not len(domains):
        return None
    rows = np.asarray(fast._ef.cliff_strip_project(
        np.ascontiguousarray(projector.local.reshape(-1, 9)),
        np.ascontiguousarray(np.asarray(domains).reshape(-1, 12)), MAX_TRIANGLES))
    if not len(rows):
        return None
    vertex_normals = (None if projector.vertex_normals is None else
                      np.ascontiguousarray(projector.vertex_normals.reshape(-1,9), float))
    attributes, owners, triangles = fast._ef.cliff_strip_mesh(
        np.ascontiguousarray(rows, float), np.ascontiguousarray(projector.triangles.reshape(-1,9), float),
        np.ascontiguousarray(projector.local.reshape(-1,9), float), np.ascontiguousarray(projector.axes, float),
        tuple(projector.origin), repeat, SURFACE_OFFSET_M, LOOKUP_WIDTH, vertex_normals)
    return Strip(attributes[:,:3], triangles, attributes[:,6:8], attributes[:,8:10],
                 attributes[:,3:6], owners, attributes[:,10])


# Limits apply to every cliff family, including road cuts. Sparse transition
# coverage is preferable to an unbounded geometry/import spike on a complex tile.
from cliffscene import CONTACT_PARTS as PARTS_MAX
ATTEMPTS_MAX = 128
# More bounded batches cover complex contact networks without making any
# individual synchronous game import larger than the previous 8k triangles.
PART_TRIANGLES = 8000
TILE_TRIANGLES = PARTS_MAX * PART_TRIANGLES
CONTACT_HALF_WIDTH_M = 4.0
CONTACT_SAMPLE_M = 4.0
RECEIVER_MARGIN_M = CONTACT_HALF_WIDTH_M + 4.0
from heightmap import SQUARE as GROUND_GRID_M
PATCH_FACES_MAX = 128000
MATERIAL = 'bne_cliff_contact_strip'


def contact_runs(positions, triangles, clearance):
    """Native ground-intersection contours, split by physical length."""
    import fast
    return fast._ef.cliff_contact_runs(np.ascontiguousarray(positions, float),
        np.ascontiguousarray(triangles, np.int64), np.ascontiguousarray(clearance, float))


def spread_runs(runs):
    """Deterministic farthest-first contacts prevent one contour starving others."""
    if not runs:
        return []
    import fast
    points = np.ascontiguousarray(np.concatenate(runs), float)
    offsets = np.r_[0, np.cumsum([len(r) for r in runs])].astype(np.int64)
    return [runs[i] for i in fast._ef.cliff_contact_order(points, offsets, ATTEMPTS_MAX)]


def pack_parts(parts):
    """Assemble native triangle batches, preserving all per-vertex attributes."""
    import fast
    batches = fast._ef.cliff_contact_batches(
        [np.ascontiguousarray(s.tris, np.int64) for s,c in parts],
        [len(s.positions) for s,c in parts], PART_TRIANGLES)
    output = []
    for batch in batches:
        def arrays(name):
            return np.concatenate([getattr(parts[i][0], name)[used] for i,used,t in batch])
        strip = Strip(arrays('positions'), np.concatenate([t for i,used,t in batch]),
                      arrays('uvs'), arrays('uvs2'), arrays('normals'), arrays('receiver'), arrays('threshold'))
        output.append((strip, np.concatenate([parts[i][1][used] for i,used,t in batch])))
    return output


@profile_step('Cliff contact preparation')
def prepare(rock, ground_at):
    """Build bounded, server-side contact strips; no game-thread raycasts or decals.

    Retain only bounded rock receiver patches and contact centerlines. Geotiles
    cache these inputs, then project against each anchor's rendered terrain.
    """
    from clifftransition import MEANS
    p = np.asarray(rock.positions, float); tri = np.asarray(rock.tris, np.int64)
    if not len(tri):
        return []
    ground = ground_at(p[:, :2])
    runs = contact_runs(p, tri, p[:, 2]-ground)
    if not runs:
        return []
    import fast
    runs = spread_runs(runs)[:ATTEMPTS_MAX]
    source_colors = rock.strip_colors if rock.strip_colors is not None else rock.colors
    colors = np.asarray(source_colors)
    families = np.zeros(len(tri), np.int64) if rock.tri_materials is None else rock.tri_materials
    rim = None
    if getattr(rock, 'terrain_blend', False) and rock.uvs2 is not None:
        fast.require(CLIFF_KERNEL, "contact rim colors")
        rim = (np.ascontiguousarray(rock.colors[:, :3], float),
                      np.ascontiguousarray(rock.uvs2, float))
    patches = fast._ef.cliff_contact_patches(
        np.ascontiguousarray(p), np.ascontiguousarray(tri), np.ascontiguousarray(colors[:, :3], float),
        np.ascontiguousarray(families, np.int64), np.ascontiguousarray(MEANS, float),
        np.ascontiguousarray(np.concatenate(runs), float),
        np.r_[0, np.cumsum([len(r) for r in runs])].astype(np.int64),
        RECEIVER_MARGIN_M, PATCH_FACES_MAX, np.issubdtype(colors.dtype, np.integer),
        None if rock.normals is None else np.ascontiguousarray(rock.normals, float), rim)
    return [dict(run=runs[i], faces=f.reshape(-1,3,3), normals=n.reshape(-1,3,3),
                 albedo=a.reshape(-1,3,3)) for i,f,n,a in patches]


class TerrainSurface:
    """The quantized TerrainBlock lattice in the active anchor frame.

    Sample only requested corners; both queries and receiver triangles use the
    engine's alternating diagonals. No game-thread sampling is needed.
    """
    def __init__(self, frame, drawn, quantum):
        self.frame, self.drawn, self.quantum = frame, drawn, quantum

    def corners(self, xy):
        la, lo, _ = self.frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        z = np.asarray(self.frame.geodetic_to_enu(la, lo, self.drawn(la, lo))[2])
        if self.quantum:
            z = np.rint(z/self.quantum)*self.quantum
        return z

    def __call__(self, xy):
        import fast
        return fast._ef.cliff_terrain_sample(np.ascontiguousarray(xy, float), GROUND_GRID_M, self.corners)

    def faces(self, lo, hi):
        return ground_faces(self.corners, lo, hi)


def ground_faces(ground_at, lo, hi):
    """Native TerrainBlock receiver mesh; Python supplies corner elevations."""
    import fast
    return fast._ef.cliff_terrain_faces(tuple(lo[:2]), tuple(hi[:2]), GROUND_GRID_M,
                                       ground_at).reshape(-1, 3, 3)


def map_patches(patches, source, target, origin):
    """Map cached receiver patches into the anchor that owns the drawn terrain."""
    import geotiles
    import fast
    fast.require(CLIFF_KERNEL, "cliff_contact_map")
    r, _, _ = geotiles.placement(source, target, (0,0,0))
    out = []
    for patch in patches:
        out.append(dict(run=geotiles.world_map(source, target, patch['run']+origin),
                        faces=fast._ef.cliff_contact_map(
                            np.ascontiguousarray(patch['faces'].reshape(-1,3)+origin, float),
                            (source.lat0, source.lon0), (target.lat0, target.lon0)).reshape(-1,3,3),
                        normals=unit(dnp.matmul(patch['normals'], r.T)), albedo=patch['albedo']))
    return out


def tile_parts(rock, ground_at):
    return project_parts(prepare(rock, ground_at), ground_at)


def _strip_tint(strip, projector, faces, albedo, ground_count):
    """Native linear-light color interpolation; no extra satellite textures."""
    import fast
    return fast._ef.cliff_strip_tint(np.ascontiguousarray(strip.positions, float),
        np.ascontiguousarray(strip.receiver, np.int64), np.ascontiguousarray(faces.reshape(-1,9), float),
        np.ascontiguousarray(albedo.reshape(-1,9), float), np.ascontiguousarray(projector.axes, float),
        tuple(projector.origin), ground_count)


@profile_step('Cliff contact projection')
def project_parts(patches, ground_at):
    """Project bounded cached patches onto this anchor's actual terrain lattice."""
    from cliffcolors import srgb
    parts = []; total = 0
    for patch in patches:
        if total >= TILE_TRIANGLES - 512:
            break
        run, faces, rn, albedo = (patch[k] for k in ('run', 'faces', 'normals', 'albedo'))
        lo, hi = run.min(0)-RECEIVER_MARGIN_M, run.max(0)+RECEIVER_MARGIN_M
        try:
            ground = ground_at.faces(lo,hi) if hasattr(ground_at,'faces') else ground_faces(ground_at,lo,hi)
        except ValueError as exc:
            if 'budget' not in str(exc):
                raise
            continue
        gn = unit(np.cross(ground[:, 1]-ground[:, 0], ground[:, 2]-ground[:, 0]))
        receiver = np.concatenate((ground, faces))
        normals = np.concatenate((np.repeat(gn[:, None, :], 3, axis=1), rn))
        normal = unit(rn.mean(axis=(0,1))+gn.mean(0))
        projector = Projector(receiver, normal, run.mean(0), vertex_normals=normals)
        try:
            strip = build(run, projector, half_width=CONTACT_HALF_WIDTH_M, spacing=CONTACT_SAMPLE_M, end_fade=4.0)
        except ValueError as exc:
            if 'budget' not in str(exc):
                raise
            continue
        if strip is None or total+len(strip.tris) > TILE_TRIANGLES:
            continue
        tint = _strip_tint(strip, projector, faces, albedo, len(ground))
        # The projection-axis lift can cross into the next terrain triangle on
        # a steep fold. Correct the final XY location, not the pre-lift sample.
        strip.positions[:, 2] = np.maximum(strip.positions[:, 2],
                                           ground_at(strip.positions[:, :2])+GROUND_CLEARANCE_M)
        colors = np.column_stack((np.rint(srgb(np.clip(tint, 0, 1))*255), np.full(len(tint), 255))).astype(np.uint8)
        parts.append((strip, colors)); total += len(strip.tris)
    return pack_parts(parts)


def write(path, part):
    import cdae
    strip, colors = part
    return cdae.write(str(path), strip.positions, strip.tris, uvs=strip.uvs, uvs2=strip.uvs2,
                      normals=strip.normals, colors=colors, material=MATERIAL,
                      object_name='cliffContact', tangent_uvs=strip.uvs, close=False)
