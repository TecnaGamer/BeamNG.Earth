#!/usr/bin/env python3
"""A/B the complete Python tower-avoidance consumer, including native-input packing.

The baseline is loaded from an explicit Git revision containing the original
NumPy implementation. Road lookup is held constant with synthetic centre lines;
real road-geotile builds are measured separately by profile_road_build.py.
No game/network. Both paths use the same points, candidate offsets and road order.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline-ref', required=True)
    ap.add_argument('--repeat', type=int, default=9)
    ap.add_argument('--points', type=int, default=64)
    ap.add_argument('--json', type=Path, required=True)
    args = ap.parse_args(argv)
    if args.repeat < 3 or args.points < 1:
        ap.error('--repeat must be >=3 and --points positive')
    source = subprocess.check_output(['git', 'show', f'{args.baseline_ref}:server/powerlines.py'], cwd=ROOT)
    baseline = types.ModuleType('power_baseline')
    exec(compile(source, 'baseline/server/powerlines.py', 'exec'), baseline.__dict__)
    if not callable(getattr(baseline, '_margin', None)):
        ap.error('--baseline-ref must contain the original NumPy _margin implementation')
    import fast
    import gasstations
    import numpy as np
    import powerlines
    fast.require(159, 'power clearance benchmark')
    rng = np.random.default_rng(152)
    road = np.array([[-200., 0.], [200., 0.]])
    pts = rng.uniform(-8., 8., (args.points, 2))
    ll = np.zeros((args.points, 2))
    cases = [
        ('already_clear', [(road, 5.)], pts + (0., 50.)),
        ('straight', [(road, 5.)], pts),
        ('dual_carriageway', [(road, 4.), (road + (0., 13.), 4.)], pts + (0., 5.)),
        ('crossroads', [(road, 5.), (road[:, ::-1].copy(), 5.)], pts),
        ('dense_roads', [(rng.normal(0., 20., (8, 2)), 5.) for _ in range(12)], pts),
        ('no_clear_candidate', [(road, 100.), (road[:, ::-1].copy(), 100.)], pts),
        ('no_roads', [], pts),
    ]

    class Metadata:
        def cell(self, *args, **kw):
            return types.SimpleNamespace(layers={})

    md = Metadata()
    report = dict(schema=1, baseline_ref=args.baseline_ref, baseline_source_sha256=hashlib.sha256(source).hexdigest(),
                  candidate_source_sha256=hashlib.sha256((ROOT/'server/powerlines.py').read_bytes()).hexdigest(),
                  native_version=fast._ef.VERSION, native_sha256=hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest(),
                  python=sys.version.split()[0], numpy=np.__version__, platform=platform.platform(),
                  processor=platform.processor(), logical_cpus=__import__('os').cpu_count(),
                  repeat=args.repeat, points=args.points, real_map_lookup=False, game=False, cases=[])
    for name, lines, points in cases:
        def run(module):
            return np.column_stack(module._off_roads(None, md, points[:, 0], points[:, 1], ll, clear_m=2.))
        with patch.object(gasstations, 'drive_lines', new=lambda *_: lines):
            want = run(baseline)
            got = run(powerlines)
            np.testing.assert_allclose(got, want, atol=2e-10, rtol=0)
            samples = {'baseline': [], 'native': []}
            for repeat in range(args.repeat):
                order = [('baseline', baseline), ('native', powerlines)]
                if repeat % 2:
                    order.reverse()
                for label, module in order:
                    start, cpu = time.perf_counter(), time.thread_time()
                    result = run(module)
                    samples[label].append(dict(wall_s=time.perf_counter()-start, cpu_s=time.thread_time()-cpu))
                    np.testing.assert_allclose(result, want, atol=2e-10, rtol=0)
            old = statistics.median(s['wall_s'] for s in samples['baseline'])
            new = statistics.median(s['wall_s'] for s in samples['native'])
            row = dict(name=name, max_coordinate_delta=float(np.max(np.abs(got-want))),
                       bit_identical=bool(np.array_equal(got, want)), baseline_median_s=old,
                       native_median_s=new, speedup=old/new, samples=samples)
            report['cases'].append(row)
            print(f'{name}: {old*1000:.2f} -> {new*1000:.2f} ms, {old/new:.2f}x', flush=True)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
