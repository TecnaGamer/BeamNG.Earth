"""Offline, reproducible Swiss junction mesh comparison (no downloaded data or running game needed)."""
import argparse
import json
import statistics
import subprocess
import time
import types

import numpy as np

from test_bridge_junction_surface import fixture, heights, roadmesh, tyre_tracks


def measure(module, repeats):
    _parts, chunks, context, pads, ground = fixture()
    kwargs = dict(context=context, pads=pads, ground=ground)
    mesh = module.deck(chunks, (0, 0, 0), **kwargs)
    points = tyre_tracks([c for c in context if c.get('jn')])
    z = heights(mesh, points, roadmesh.DECK_TOP)
    wall = heights(mesh, points)
    durations = []
    for _ in range(repeats):
        start = time.perf_counter()
        module.deck(chunks, (0, 0, 0), **kwargs)
        durations.append(1000 * (time.perf_counter() - start))
    return {
        'vertices': len(mesh[0]), 'triangles': len(mesh[1]), 'sample_count': len(points),
        'covered_samples': sum(bool(len(v)) for v in z),
        'max_overlap_m': max(float(np.ptp(v)) for v in z if len(v)),
        'overlap_over_1cm_samples': sum(len(v) > 1 and np.ptp(v) > .01 for v in z),
        'max_wall_above_surface_m': max(float(max(w) - max(v)) for w, v in zip(wall, z) if len(v)),
        'construction_median_ms': statistics.median(durations), 'repeats': repeats,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='7198d96e083dc08ccbe126a5afe57ee05aeb03d3')
    parser.add_argument('--repeats', type=int, default=11)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error('--repeats must be between 1 and 100')
    source = subprocess.check_output(['git', 'show', args.baseline_ref + ':server/roadmesh.py'])
    baseline = types.ModuleType('baseline_roadmesh')
    exec(compile(source, '<baseline_roadmesh>', 'exec'), baseline.__dict__)
    result = {'baseline_ref': args.baseline_ref, 'fixture': 'bridge_fork_swiss.json',
              'before': measure(baseline, args.repeats), 'after': measure(roadmesh, args.repeats)}
    print(json.dumps(result, indent=2, default=lambda value: value.item()))


if __name__ == '__main__':
    main()
