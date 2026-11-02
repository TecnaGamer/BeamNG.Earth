#!/usr/bin/env python3
"""Compare lane preparation with raw input decoding outside the timed region.

Each pass starts with fresh Features and caches, then solves nine neighbouring
home cells through lane_flow. Up to 7x7 source cells cover their dependencies.
Missing offline cells stay absent. No network or writes to the source cache.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cache-dir', type=Path, required=True)
    ap.add_argument('--server-dir', type=Path, default=Path(__file__).resolve().parents[1]/'server')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--at', default='-36.85325222344017,174.759521484375')
    ap.add_argument('--repeat', type=int, default=3)
    a = ap.parse_args()
    if a.repeat < 1:
        ap.error('--repeat must be positive')
    sys.path.insert(0, str(a.server_dir.resolve()))
    import numpy as np
    import fast, mapcells, mapdata, mapgeom, roads
    lat, lon = map(float, a.at.split(','))
    centre = mapcells.cell_of(lat, lon)
    md = object.__new__(mapdata.MapData)
    md.cache_dir, md.release = str(a.cache_dir), mapdata.overture.RELEASE
    source, hashes = {}, {}
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            key = centre._replace(x=centre.x+dx, y=centre.y+dy)
            path = Path(md.path(key, 'roads'))
            if path.exists():
                with np.load(path, allow_pickle=False) as archive:
                    source[key] = {n: archive[n] for n in archive.files}
                hashes[str(key)] = hashlib.sha256(path.read_bytes()).hexdigest()
    wanted = [centre._replace(x=centre.x+dx, y=centre.y+dy) for dy in range(-1, 2) for dx in range(-1, 2)]
    if any(k not in source for k in wanted):
        ap.error('raw inputs for the nine target cells are required')
    original = fast._ef.lane_flow
    volume = {}

    def measured(points, off, attrs, limit):
        volume['native_calls'] += 1
        volume['points'] += len(points)
        volume['input_bytes'] += points.nbytes + off.nbytes + attrs.nbytes
        return original(points, off, attrs, limit)

    fast._ef.lane_flow = measured
    rows = []
    for repeat in range(a.repeat):
        cells = {k: mapgeom.Features.from_arrays('roads', ar, '') for k, ar in source.items()}
        for key, f in cells.items():
            f._bnge_cell = (str(key), 'roads', 'benchmark')
            f._bnge_neighbours = lambda key=key: [cells.get(k) for k in key.neighbours()]
        volume = dict(native_calls=0, points=0, input_bytes=0)
        start, cpu = time.perf_counter(), time.process_time()
        answers = {str(k): roads.lane_flow(cells[k]) for k in wanted}
        wall, cpu = time.perf_counter()-start, time.process_time()-cpu
        caches = [f.__dict__['_lane_flow_inputs'][1] for f in cells.values() if '_lane_flow_inputs' in f.__dict__]
        retained = sum(v.nbytes for cache in caches for v in cache)
        row = {'repeat': repeat+1, 'wall_s': wall, 'cpu_s': cpu, **volume,
               'retained_array_bytes': retained, 'prepared_cells': len(caches),
               'digest': hashlib.sha256(json.dumps(answers, sort_keys=True).encode()).hexdigest()}
        rows.append(row)
        print(json.dumps(row), flush=True)
    result = {'scope': __doc__, 'at': [lat, lon], 'raw_sha256': hashes, 'rows': rows,
              'features': sum(len(ar['ids']) for ar in source.values()),
              'median_s': statistics.median(r['wall_s'] for r in rows),
              'roads_sha256': hashlib.sha256((a.server_dir/'roads.py').read_bytes()).hexdigest(),
              'native_sha256': hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest()}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
