"""Read the same server-owned terrain and road shapes sent to the client.

No support placement lives here. Matrices come from the existing geotile
placement contract, including the exact shape selected by the road tracker.
"""
import json
from pathlib import Path
import struct

import numpy as np
import geotiles
import heightmap
from nlsignalassembly import SurfacePending, cached_mesh, make_scene


def owned(root, vfs, path):
    if not isinstance(path, str) or not path.startswith(vfs.rstrip('/') + '/'):
        raise ValueError('signal input is outside this session')
    rel = Path(path[len(vfs.rstrip('/')) + 1:])
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('signal input traversal')
    out = Path(root).resolve() / rel
    if not out.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('signal input outside session')
    return out


def terrain(path):
    with open(path, 'rb') as f:
        header = f.read(5)
        if len(header) != 5:
            raise ValueError('short signal terrain header')
        version, n = struct.unpack('<BI', header)
        if version != heightmap.TER_VERSION or not 2 <= n <= 4096:
            raise ValueError('unsupported signal terrain')
        raw = f.read(n * n * 3)
        if len(raw) != n * n * 3:
            raise ValueError('short signal terrain arrays')
    q = np.frombuffer(raw[:n*n*2], '<u2').reshape(n, n).copy()
    holes = np.frombuffer(raw[n*n*2:], np.uint8).reshape(n, n) == heightmap.TER_HOLE
    return q, holes


def _json(path):
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('signal surface metadata too large')
    return json.loads(path.read_text(encoding='utf-8'))


def ground_footprints(chunks, source_frame, frame):
    """Marshal the renderer's painted-road envelopes, using its native ribbon kernel.

    No mast positions are selected here. Rust samples the committed terrain for
    these XY envelopes, so painted asphalt is not mistaken for missing geometry.
    """
    import blockworker, roadmesh
    mapped = blockworker._map_chunks(source_frame, frame, chunks)
    points, triangles, offset = [], [], 0
    for left, right, *_ in roadmesh.ribbons(mapped):
        n = len(left)
        if n < 2:
            continue
        p = np.column_stack((np.concatenate((left, right)), np.zeros(2*n)))
        i = np.arange(n-1, dtype=np.int64)
        t = np.concatenate((np.column_stack((i, i+1, n+i)),
                            np.column_stack((i+1, n+i+1, n+i))))
        points.append(p); triangles.append(t + offset); offset += len(p)
    if points:
        return dict(kind='ground_road',positions=np.concatenate(points),triangles=np.concatenate(triangles))


def session_scene(root, vfs, aid, frame, ground, centre, tiles):
    """tiles: immutable copies of the road tracker's fully sent (key, parts, exact) rows.

    Do not include retry tiles as coverage. An unloaded neighbour remains pending.
    An incomplete surface file aborts this snapshot; the tracker retries it later.
    """
    import blockworker
    if not ground.get('ter') or ground.get('provisional'):
        raise SurfacePending('final terrain file pending')
    q, holes = terrain(owned(root, vfs, f"{vfs}/{ground['ter']}"))
    parts, coverage = [], []
    for gx, gy, content, exact in tiles:
        key = geotiles.key(gx, gy)
        skin = _json(Path(root) / blockworker.geo_names(key, aid)['json'])
        if skin.get('anchorId') != aid or skin.get('key') != key:
            raise SurfacePending('road skin belongs to another anchor')
        if skin.get('chunks'):
            # A DecalRoad fallback has no authoritative drawn triangle mesh.
            raise SurfacePending('road triangle mesh pending')
        for what, kind in (('road', 'road'), ('walk', 'walk')):
            row = skin.get(what)
            if isinstance(row, dict):
                parts.append(cached_mesh(owned(root, vfs, row['shape']), kind, row['pos']))
        # Raised road decks are content parts, not the terrain-following skin.
        tile_frame = geotiles.frame(gx, gy)
        data = _json(Path(root) / blockworker.geo_names(key)['json'])
        if data.get('h') != content.get('h'):
            raise SurfacePending('road content generation changed')
        def missing():
            raise SurfacePending('painted road inputs pending')
        inputs = blockworker._skin_inputs(key, str(root), missing)
        if not inputs or inputs.get('h') != content.get('h') or 'ground_cover' not in inputs:
            raise SurfacePending('painted road generation pending')
        footprint = ground_footprints(inputs['ground_cover'], tile_frame, frame)
        if footprint is not None:
            parts.append(footprint)
        for what in ('deck',):
            row = data.get(what)
            if not isinstance(row, dict):
                continue
            model = row['shape']
            if exact.get(what) == aid:
                model = f'{vfs}/rx_{aid}_{key}_{what}.cdae'
            p = cached_mesh(owned(root, vfs, model), 'road', row['pos'])
            r, t, _ = geotiles.placement(tile_frame, frame, (0., 0., float(row.get('pivot', 0.))))
            p['positions'] = p['positions'].astype(np.float64) + np.asarray(p['origin'])
            p['origin'], p['basis'] = t, r
            parts.append(p)
        quad = np.asarray(geotiles.quad(frame, gx, gy), np.float64)
        coverage.append(quad[:,:2].reshape(8))
    return make_scene(parts, q, ground['zBase'], ground['maxHeight'], centre, [],
                      square=heightmap.SQUARE, terrain_holes=holes,coverage_quads=coverage)
