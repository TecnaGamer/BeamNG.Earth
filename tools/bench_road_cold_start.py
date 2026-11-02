#!/usr/bin/env python3
"""Offline cold road-worker benchmark; copies raw inputs into a new run directory.

--snapshot must contain mapdata/v2 and dem/<provider> from an existing cache.
Derived mirrors, plans and revisit products are never copied. Uses one spawned
worker, the production road job, and records faults and mesh hashes. No network.
Use --trace for nested function attribution, not comparative timings.
"""
import argparse
import functools
import hashlib
import json
import multiprocessing
from pathlib import Path
import platform
import shutil
import sys
import threading
import time


def input_manifest(cache):
    """Stable identity of the raw inputs, independent of absolute cache paths."""
    digest, count, size = hashlib.sha256(), 0, 0
    for p in sorted(cache.rglob('*')):
        if p.is_file():
            data = p.read_bytes()
            digest.update(p.relative_to(cache).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(data).digest())
            count += 1
            size += len(data)
    return {'files': count, 'bytes': size, 'sha256': digest.hexdigest()}


def instrument():
    """Inclusive/exclusive wall times aggregate threads; overlapping totals are not CPU time."""
    import blockworker, buildings, fast, grading, mapdata, roads, roadwidth
    counts, local, lock = {}, threading.local(), threading.Lock()
    targets = [(blockworker, '_tile_decks'), (buildings, 'road_tile_items'),
               (roads, '_lane_flow_local'), (roads, 'lane_flow'), (roads, '_road_specs'),
               (roads, '_bridge_specs'), (roads, '_cell_ranges'),
               (roads.BridgeProfiles, '_chain_of'), (roads.BridgeProfiles, '_solve'),
               (grading._Planner, '_make_cell'), (grading._Planner, '_extensions'),
               (grading._Planner, '_pins'), (grading._Planner, '_chain_cands'),
               (grading, 'plan_roads'), (grading, 'grade_points'),
               (mapdata.MapData, 'cell'), (mapdata.MapData, '_mirror_load'),
               (mapdata.MapData, '_mirrored'), (mapdata.MapData, '_load'),
               (roadwidth.RoadWidths, 'get'), (roadwidth.RoadWidths, '_near_walls'),
               (fast._ef, 'lane_flow'), (fast._ef, 'measure_row'), (fast._ef, 'measure_row32'),
               (fast._ef, 'grade_points')]
    for owner, name in targets:
        original = getattr(owner, name)
        label = original.__module__ + '.' + original.__qualname__

        def wrap(fn, label):
            @functools.wraps(fn)
            def timed(*args, **kwargs):
                stack = getattr(local, 'stack', None)
                if stack is None:
                    stack = local.stack = []
                entry = [time.perf_counter(), 0.0]
                stack.append(entry)
                try:
                    return fn(*args, **kwargs)
                finally:
                    elapsed = time.perf_counter() - entry[0]
                    stack.pop()
                    if stack:
                        stack[-1][1] += elapsed
                    with lock:
                        row = counts.setdefault(label, {'calls': 0, 'inclusive_s': 0.0, 'exclusive_s': 0.0})
                        row['calls'] += 1
                        row['inclusive_s'] += elapsed
                        row['exclusive_s'] += elapsed - entry[1]
            return timed
        setattr(owner, name, wrap(original, label))
    return counts


def worker(config):
    sys.path.insert(0, config['server_dir'])
    import blockworker, dem, fast, geotiles, numpy, revisit
    revisit.ON = False
    out = Path(config['out'])
    shapes = out / 'shapes'
    shapes.mkdir()
    counts = instrument() if config['trace'] else None
    blockworker.init(str(out/'cache'), True, True, True, True, True, None, 30,
                     airports=False, sea=True, roads_in_ground=True, gas_stations=True)
    lat, lon = config['at']
    gx, gy = geotiles.tile_of(lat, lon)
    zoom = dem.zoom_for(lat, 2.5)
    rows = []
    for dx, dy in [(0, 0), (1, 0), (0, 1)][:config['tiles']]:
        faults = revisit.faults()
        start, cpu = time.perf_counter(), time.process_time()
        result = blockworker.roads_geo(1, lat, lon, zoom, gx+dx, gy+dy, zoom, str(shapes), '/temp/audit',
                                      True, 0.0, True, config['left_hand'], False, 1/256, True,
                                      country=config['country'], exact='auto')
        row = {'key': geotiles.key(gx+dx, gy+dy), 'wall_s': time.perf_counter()-start,
               'cpu_s': time.process_time()-cpu, 'stages': result['stages'],
               'faults': revisit.faults()-faults}
        rows.append(row)
        print(json.dumps(row), flush=True)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(shapes.glob('*.cdae'))}
        record = {'config': config, 'rows': rows, 'mesh_sha256': hashes, 'trace': counts,
                  'environment': {'python': platform.python_version(), 'numpy': numpy.__version__,
                                  'native_version': fast._ef.VERSION,
                                  'native_sha256': hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest()},
                  'server_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in sorted(Path(config['server_dir']).glob('*.py'))}}
        (out/'results.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--snapshot', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--server-dir', type=Path, default=Path(__file__).resolve().parents[1]/'server')
    ap.add_argument('--at', default='-36.85325222344017,174.759521484375')
    ap.add_argument('--country', default='NZ')
    ap.add_argument('--right-hand', action='store_true')
    ap.add_argument('--tiles', type=int, choices=(1, 2, 3), default=3)
    ap.add_argument('--trace', action='store_true')
    a = ap.parse_args()
    if a.out.exists():
        ap.error('--out must be a new directory for a genuinely cold run')
    if not (a.snapshot/'mapdata/v2').is_dir() or not (a.snapshot/'dem').is_dir():
        ap.error('--snapshot needs mapdata/v2 and dem raw inputs')
    if not (a.server_dir/'blockworker.py').is_file():
        ap.error('--server-dir does not contain the server source')
    a.out.mkdir(parents=True)
    for sub in ['mapdata/v2', 'dem/mapterhorn', 'dem/aws-terrarium']:
        if (a.snapshot/sub).exists():
            shutil.copytree(a.snapshot/sub, a.out/'cache'/sub)
    config = {'server_dir': str(a.server_dir.resolve()), 'out': str(a.out.resolve()),
              'at': list(map(float, a.at.split(','))), 'country': a.country, 'left_hand': not a.right_hand,
              'tiles': a.tiles, 'trace': a.trace, 'raw_inputs': input_manifest(a.out/'cache')}
    p = multiprocessing.get_context('spawn').Process(target=worker, args=(config,))
    p.start()
    p.join()
    raise SystemExit(p.exitcode)


if __name__ == '__main__':
    main()
