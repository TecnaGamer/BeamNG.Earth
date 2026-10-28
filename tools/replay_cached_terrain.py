"""Build logged terrain blocks using read-only cached roads/DEM, writing only to --out.

Includes grading, measured widths, full road materials and tunnel work. It excludes network,
live scheduling, imagery and sea carving, so these are isolated build timings, not drive guarantees.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

from replay_road_loading import CachedMap, CASES, use_baseline
import blockworker
import dem
import geodesy
import grading
import heightmap
import netfetch
import roadwidth
import surface


class CachedDem(dem.Dem):
    def _coverage(self):
        return None

    def _download(self, src, z, x, y, kind='demand'):
        path = Path(self._path(src, z, x, y))
        if path.is_file():
            return path.read_bytes()
        if path.with_suffix('.404').is_file():
            return None
        raise RuntimeError('Missing replay DEM: ' + str(path))

    def _mirror_put(self, key, a):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cache', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--case', choices=CASES, required=True)
    ap.add_argument('--baseline', action='store_true')
    args = ap.parse_args()
    if args.baseline:
        global surface
        use_baseline()
        surface = sys.modules['surface']
        blockworker.surfacemod = surface
    args.out.mkdir(parents=True, exist_ok=True)
    lat, lon, cx, cy = CASES[args.case]
    frame = geodesy.EnuFrame(lat, lon)
    md = CachedMap(args.cache)
    cover = surface.Surface(md)
    widths = roadwidth.RoadWidths(lambda k: md.cell(k, ('buildings',)).layers['buildings']).get
    elevation = CachedDem(str(args.cache / 'dem'), fetcher=netfetch.Fetcher(offline=True))
    grader = grading.Grader(frame, md, lambda la, lo: elevation.sample(la, lo, grading.PROFILE_ZOOM), widths)
    chunks = heightmap.ChunkCache()
    blockworker._s = SimpleNamespace(md=md, dem=elevation, widths=widths, surface=cover, airports=None, sea=None,
                                    tunnels={}, lock=threading.Lock(), anchor=lambda *a: (grader, chunks))
    results = []
    try:
        for n in range(3):
            x = cx + 128 * n
            started = time.perf_counter()
            name = f'block_{n}'
            info = blockworker.build(1, lat, lon, 16, 1/64, x, cy, 512, str(args.out), name+'.png', name+'.ter')
            elapsed = time.perf_counter() - started
            assert not grader.failed, 'Replay has missing grading inputs'
            row = dict(cx=x, cy=cy, seconds=elapsed, stages=info['stages'],
                       ter_sha256=hashlib.sha256((args.out/(name+'.ter')).read_bytes()).hexdigest())
            results.append(row)
            print(json.dumps(row), flush=True)
        (args.out/'terrain.json').write_text(json.dumps(results, indent=2))
    finally:
        cover.close()
        for pool in (elevation.pool, elevation.urgent_pool, elevation.probe_pool):
            pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
