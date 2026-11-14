"""Server data glue for native physical support planning (not yet a live renderer).

Supply the actual drawn meshes and decoded quantised terrain, not a guessed
flat floor. Cached .cdae files are read here on the server, never by the mod.
"""
import json
from pathlib import Path
import struct

import numpy as np
import dnp
import cdae
import fast
import heightmap


class SurfacePending(RuntimeError):
    pass


def cached_mesh(path, kind, origin=(0., 0., 0.)):
    if kind not in ('road', 'walk'):
        raise ValueError('signal surface kind')
    path = Path(path)
    if path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('signal surface shape too large')
    shape = cdae.read(path)
    points, triangles, offset = [], [], 0
    # Generated cache shapes store the finest visible mesh at each ordinary
    # object's startMeshIndex. Collision objects and lower LODs are not the
    # drawn surface: including them can reject a perfectly safe mast foot.
    count, stride, objects = shape['objects']
    if stride != 24:
        raise ValueError('unsupported signal surface objects')
    visible = []
    for i in range(count):
        name, meshes, start, node, sibling, decal = struct.unpack_from('<6i', objects, i * stride)
        if name < 0 or name >= len(shape['names']) or meshes < 1 or start < 0 or start >= len(shape['meshes']):
            raise ValueError('invalid signal surface object')
        if not shape['names'][name].lower().startswith('colmesh'):
            visible.append(shape['meshes'][start])
    for mesh in visible:
        if mesh['type'] == 4:
            continue
        arrays = cdae.mesh_arrays(mesh)
        p, ix = arrays['verts'], arrays['indices']
        if len(ix) % 3:
            raise ValueError('signal surface triangle list')
        points.append(p)
        triangles.append(ix.astype(np.int64).reshape(-1, 3) + offset)
        offset += len(p)
    if not points:
        raise ValueError('no visible signal surface mesh')
    return dict(kind=kind, origin=origin,
                positions=np.concatenate(points) if points else np.empty((0, 3)),
                triangles=np.concatenate(triangles) if triangles else np.empty((0, 3), np.int64))


def make_scene(parts, qgrid, z_base, max_height, centre, coverage, square=heightmap.SQUARE, terrain_holes=None):
    """Build one immutable native index for a complete surface snapshot.

    coverage contains fully decoded road-tile rectangles. Pending neighbours are
    excluded. The caller must invalidate the index when mesh or terrain versions
    change, and keep each ENU anchor's indices separate.
    terrain_holes is the game's actual hole mask (idx_ter == heightmap.TER_HOLE).
    """
    if qgrid is None:
        raise SurfacePending('quantised terrain has not arrived')
    qgrid = np.asarray(qgrid)
    if qgrid.ndim != 2 or qgrid.dtype != np.uint16 or min(qgrid.shape) < 2:
        raise ValueError('expected quantised uint16 terrain grid')
    if not np.isfinite([z_base, max_height, *centre, square]).all() or max_height < 0 or square <= 0:
        raise ValueError('signal terrain metadata')
    points, triangles, kinds, offset = [], [], [], 0
    triangle_count = 0
    for i, part in enumerate(parts):
        if i >= 128:
            raise ValueError('too many signal surface parts')
        if part['kind'] not in ('road', 'walk'):
            raise ValueError('signal surface kind')
        # A freshly built road has float64 points, but the game receives float32
        # shape vertices. Use exactly those coordinates in both fresh and cached paths.
        p = np.asarray(part['positions'], np.float32).astype(np.float64)
        raw_t = np.asarray(part['triangles'])
        if not np.issubdtype(raw_t.dtype, np.integer):
            raise ValueError('signal surface indices must be integers')
        t = raw_t.astype(np.int64)
        origin = np.asarray(part.get('origin', (0., 0., 0.)), np.float64)
        basis = np.asarray(part.get('basis', np.eye(3)), np.float64).reshape(3, 3)
        if p.ndim != 2 or p.shape[1] != 3 or t.ndim != 2 or t.shape[1] != 3 or origin.shape != (3,):
            raise ValueError('signal surface mesh arrays')
        if len(t) and ((t < 0).any() or (t >= len(p)).any()):
            raise ValueError('signal surface mesh indices')
        p = dnp.matmul(p, basis.T) + origin
        points.append(p)
        triangles.append(t + offset)
        kinds.extend([1 if part['kind'] == 'road' else 2] * len(t))
        offset += len(p)
        triangle_count += len(t)
        if offset > 2_000_000 or triangle_count > 1_000_000:
            raise ValueError('signal surface snapshot too large')
    p = np.concatenate(points) if points else np.empty((0, 3))
    t = np.concatenate(triangles) if triangles else np.empty((0, 3), np.int64)
    z = heightmap.heights(qgrid, z_base, max_height)
    holes = None if terrain_holes is None else np.asarray(terrain_holes, np.uint8)
    if holes is not None and holes.shape != qgrid.shape:
        raise ValueError('terrain hole mask shape')
    origin = [centre[0] - qgrid.shape[1] * square / 2, centre[1] - qgrid.shape[0] * square / 2]
    return fast.native(294, 'Dutch physical signal planning and mounted assemblies').NlSignalScene(
        p, t, np.asarray(kinds, np.uint8), z, origin, float(square),
        np.asarray(coverage, np.float64).reshape(-1, 4), holes)


def plans(scene, requests):
    return [json.loads(s) for s in scene.plans(_rows(requests))]


def _rows(requests):
    rows = []
    for r in requests:
        if type(r['lanes']) is not int or type(r['opposite']) is not int or type(r['overhead']) is not bool:
            raise ValueError('explicit lane counts and overhead flag required')
        rows.append([*r['stop'], *r['travel'], r['lanes'], r['opposite'], r['radius'],
                     int(r['overhead']), r['before'], r['after']])
    return np.asarray(rows, np.float64).reshape(-1, 11)


def assemblies(scene, requests, catalog, turns, zones, motorway_exits):
    """Marshal context and authored components; Rust chooses every part pose.

    zones must cover the complete junction, not merely this approach. Unknown
    coverage remains -1 and therefore selects 300 mm conservatively.
    """
    if not all(type(v) is bool for v in motorway_exits):
        raise ValueError('explicit motorway-exit flags required')
    source = json.dumps(catalog, allow_nan=False, separators=(',', ':'))
    return [json.loads(s) for s in scene.assemblies(_rows(requests), source, turns, zones, motorway_exits)]
