#!/usr/bin/env python3
"""Profile the current road-geotile production path, outside BeamNG.

Offline by default. --online populates the specified download cache using normal
provider limits. --revisit measures restored generated tiles; otherwise every
iteration builds geometry again, retaining worker memory after the first one.
Use separate processes/source roots/native builds for A/B comparisons. A failed
build is recorded and returns nonzero, never counted as a successful fast build.
"""
import argparse
import cProfile
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import pstats
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]


def manifest(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file()}


def profile_rows(profile, source_root, limit=30):
    stats = pstats.Stats(profile).stats
    rows = []
    for (filename, line, name), (primitive, calls, own, cumulative, _callers) in stats.items():
        try:
            filename = Path(filename).resolve().relative_to(source_root).as_posix()
        except ValueError:
            filename = Path(filename).name  # no machine/user paths in a shareable report
        rows.append(dict(file=filename, line=line, function=name, calls=calls, primitive_calls=primitive,
                         own_wall_s=own, cumulative_wall_s=cumulative))
    return sorted(rows, key=lambda row: row['own_wall_s'], reverse=True)[:limit]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--at', required=True, help='lat,lon (one geotile containing this point)')
    ap.add_argument('--cache-dir', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True, help='JSON report, not the game user folder')
    ap.add_argument('--repeat', type=int, default=6)
    ap.add_argument('--online', action='store_true')
    ap.add_argument('--revisit', action='store_true')
    ap.add_argument('--profile', action='store_true', help='cProfile wall-time attribution; use separate runs for timing')
    ap.add_argument('--source-root', type=Path, default=ROOT, help='checkout supplying the server code')
    ap.add_argument('--native-dir', type=Path, help='directory containing earthfast for this checkout/build')
    ap.add_argument('--compare-power-ref', help='alternate original/proposed power avoidance in the same worker; one warmup per variant')
    args = ap.parse_args(argv)
    try:
        lat, lon = map(float, args.at.split(','))
        if not (math.isfinite(lat) and math.isfinite(lon) and -85 <= lat <= 85 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        ap.error('--at must be a finite lat,lon within the geotile grid (-85..85, -180..180)')
    if args.repeat < 1:
        ap.error('--repeat must be positive')
    source = args.source_root.resolve()
    cache, report_path = args.cache_dir.resolve(), args.out.resolve()
    if not (source / 'server/blockworker.py').is_file():
        ap.error('--source-root must contain server/blockworker.py')
    native_dir = args.native_dir.resolve() if args.native_dir else source / 'server'
    sys.path[:0] = [str(native_dir), str(source / 'server')]
    os.chdir(source)
    # Force a measured build rather than silently timing an old tile; use --revisit to measure restoration.
    os.environ['BNE_REVISIT'] = 'on' if args.revisit else 'off'
    os.environ['BNGE_GPU'] = '0'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    initial_cache_empty = not cache.exists() or not any(cache.iterdir())
    cache.mkdir(parents=True, exist_ok=True)
    import blockworker
    import dem
    import fast
    import geotiles
    import numpy as np
    import revisit
    import powerlines
    current_power = powerlines._off_roads
    variants = [('current', current_power)]
    if args.compare_power_ref:
        fast.require(159, 'paired power-line benchmark')
        if hasattr(powerlines, '_margin'):
            ap.error('--compare-power-ref requires the proposed power-line consumer')
        old_source = subprocess.check_output(['git', 'show', f'{args.compare_power_ref}:server/powerlines.py'], cwd=source)
        old = types.ModuleType('power_baseline')
        exec(compile(old_source, 'baseline/server/powerlines.py', 'exec'), old.__dict__)
        if not callable(getattr(old, '_margin', None)):
            ap.error('--compare-power-ref must contain the original NumPy _margin')
        variants = [('baseline', old._off_roads), ('native', current_power)]
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    report = dict(schema=1, source_revision=revision.stdout.strip() if revision.returncode == 0 else None,
                  native_version=fast._ef.VERSION, native_sha256=hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest(),
                  python=sys.version.split()[0], numpy=np.__version__, platform=platform.platform(),
                  processor=platform.processor(), logical_cpus=os.cpu_count(), lat=lat, lon=lon,
                  online=args.online, initial_cache_empty=initial_cache_empty, generated_cache=args.revisit,
                  profiled=args.profile, baseline_power_ref=args.compare_power_ref,
                  power_source_sha256=hashlib.sha256((source/'server/powerlines.py').read_bytes()).hexdigest(),
                  settings=dict(roads=True, grading=True, road_widths=True, surface=True,
                  buildings=True, sidewalks=True, airports=False, sea=False, gas_stations=False, q=1/64), rows=[])

    def save():
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')

    save()
    blockworker.init(str(cache), not args.online, True, True, True, True, None, logging.WARNING,
                     airports=False, sea=False)
    gx, gy = geotiles.tile_of(lat, lon)
    zoom = dem.zoom_for(lat, 1.25)
    gzoom = dem.zoom_for(geotiles.centre(gx, gy)[0], 1.25)
    report['geotile'] = geotiles.key(gx, gy)
    # Its parent is stable across A/B processes; any optional local-model cache shares that parent.
    scratch = report_path.parent / '.road-profile'
    scratch.mkdir(exist_ok=True)
    jobs = []
    if args.compare_power_ref:
        jobs.extend([(-1, label, fn) for label, fn in variants])
    for iteration in range(args.repeat):
        order = variants[::-1] if iteration % 2 else variants
        jobs.extend([(iteration, label, fn) for label, fn in order])
    paired = {}
    for iteration, label, power_fn in jobs:
        with tempfile.TemporaryDirectory(prefix='tile-', dir=scratch) as temporary:
            out_dir = Path(temporary)
            profile = cProfile.Profile() if args.profile else None
            before = revisit.faults()
            before_geo = blockworker.geo_stats()
            started, cpu, thread = time.perf_counter(), time.process_time(), time.thread_time()
            result, error = None, None
            if profile:
                profile.enable()
            try:
                powerlines._off_roads = power_fn
                result = blockworker.roads_geo(1, lat, lon, zoom, gx, gy, gzoom, str(out_dir), '/temp/roadProfile',
                                              True, 0., True, False, True, 1/64, False, exact='auto')
            except Exception as exc:
                error = {'type': type(exc).__name__, 'message': str(exc)}
            finally:
                powerlines._off_roads = current_power
                if profile:
                    profile.disable()
            row = dict(iteration=iteration, variant=label, warmup=iteration < 0, ok=error is None, error=error, wall_s=time.perf_counter()-started,
                       process_cpu_s=time.process_time()-cpu, thread_cpu_s=time.thread_time()-thread,
                       input_faults=revisit.faults()-before)
            if result is not None:
                row.update(chunks=result['content'][1] if result.get('content') else None,
                           stages=result.get('stages'), parts_time=result.get('time'), files=manifest(out_dir))
            row['geotile_cache_delta'] = {key: value-before_geo.get(key, 0) for key, value in blockworker.geo_stats().items()}
            report['source_versions'] = blockworker._s.md.versions()
            if args.compare_power_ref and iteration >= 0 and error is None:
                previous = paired.pop(iteration, None)
                if previous is None:
                    paired[iteration] = row
                else:
                    same = previous.get('files') == row.get('files') and previous.get('chunks') == row.get('chunks')
                    previous['matches_pair'] = row['matches_pair'] = same
                    if not same:
                        row['ok'] = False
                        row['error'] = {'type': 'OutputMismatch', 'message': 'paired variants generated different files/chunk counts'}
            if profile:
                row['profile'] = profile_rows(profile, source)
                profile.dump_stats(str(report_path.with_suffix(f'.{label}.{iteration}.pstats')))
            report['rows'].append(row)
            save()
            print(json.dumps({k: v for k, v in row.items() if k not in ('files', 'profile', 'stages')}), flush=True)
            if not row['ok']:
                return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
