"""Replay logged terrain-photo road work from an existing cache, without modifying that cache.

Use --out for timing/profile artifacts. Each case starts with cold in-memory road caches.
The saved pixels can be compared across revisions to check that optimization preserves detail.
"""
import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import pstats
import sys
import time
import subprocess
import types
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import numpy as np
import geodesy
import mapcells
import mapdata
import mapgeom
import roadcorners
import roadwidth
import surface

CASES = {
    "nashville": (36.20, -86.69, -896., -6400.),
    "murfreesboro": (35.88, -86.47, 2304., -2688.),
    "chattanooga": (35.03, -85.37, 12416., -2432.),
}


class CachedMap:
    """Read-only inputs: no network, generated caches, or changes to the player's files."""
    def __init__(self, cache):
        self.root = Path(cache) / "mapdata" / "v2"
        self.memo = {}

    def path(self, key, layer):
        folder = (self.root / "worldcover" / "v200-2021" / layer if layer == "landcover" else
                  self.root / "overture" / "2026-09-23.1" / ("roads.7" if layer == "roads" else layer))
        return folder / str(key.z) / str(key.x) / f"{key.y}.npz"

    def has(self, key, layer):
        return self.path(key, layer).is_file()

    def prefetch(self, *args):
        pass

    def cell(self, key, layers, kind=None):
        out = {}
        for layer in layers:
            token = (str(key), layer)
            if token not in self.memo:
                path = self.path(key, layer)
                if not path.is_file():
                    raise FileNotFoundError(path)
                with np.load(path, allow_pickle=False) as z:
                    arrays = dict(z)
                if layer == "landcover":
                    f = mapdata._raster_from(arrays)
                else:
                    f = mapgeom.Features.from_arrays(layer, arrays, "")
                    f.localize(geodesy.EnuFrame(*key.center()))
                self.memo[token] = f
            out[layer] = self.memo[token]
        return SimpleNamespace(layers=out)


def use_baseline():
    """Load unmodified paint code into an isolated replay process, never changing repository files."""
    global surface
    for name in ('junctions', 'roadcorners', 'surface'):
        code = subprocess.check_output(['git', '-c', 'safe.directory=' + ROOT.as_posix(),
                                        'show', 'HEAD:server/' + name + '.py'], cwd=ROOT, text=True)
        module = types.ModuleType(name)
        module.__file__ = str(ROOT / 'server' / (name + '.py'))
        sys.modules[name] = module
        exec(compile(code, module.__file__, 'exec'), module.__dict__)
    surface = sys.modules['surface']


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--case", choices=CASES, default="murfreesboro")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="Use HEAD's unmodified paint/corner code for comparison")
    args = ap.parse_args()
    if args.baseline:
        use_baseline()
    args.out.mkdir(parents=True, exist_ok=True)
    md = CachedMap(args.cache)
    cover = surface.Surface(md)
    widths = roadwidth.RoadWidths(lambda k: md.cell(k, ("buildings",)).layers["buildings"])
    lat, lon, cx, cy = CASES[args.case]
    frame = geodesy.EnuFrame(lat, lon)
    rgb = np.full((1024, 1024, 3), 190, np.uint8)
    profile = cProfile.Profile()
    try:
        t0 = time.perf_counter()
        if args.profile:
            profile.enable()
        result = surface.paint_square(rgb, frame, cx - 512, cy - 512, 1024, cover, widths.get)
        profile.disable()
        seconds = time.perf_counter() - t0
        np.save(args.out / f"{args.case}.npy", result)
        summary = {"case": args.case, "seconds": seconds,
                   "sha256": hashlib.sha256(result.tobytes()).hexdigest(),
                   "road_features": sum(len(f) for (k, layer), f in md.memo.items() if layer == "roads")}
        (args.out / f"{args.case}.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        if args.profile:
            profile.dump_stats(args.out / f"{args.case}.prof")
            pstats.Stats(profile).sort_stats("cumulative").print_stats(30)
    finally:
        cover.close()


if __name__ == "__main__":
    main()
