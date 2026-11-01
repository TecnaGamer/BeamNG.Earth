"""Build the visual-road stream's real geotiles from the existing disk cache, for the isolated in-game probe.

No downloads, anchors, terrain jobs or game settings. Uses the production worker and route builder; the live
companion can stay connected while this snapshot tests route heights, ownership and the production renderer.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))
import apppaths
import blockworker
import dem
import distanttraffic
import geotiles


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--x", type=float, default=0)
    ap.add_argument("--y", type=float, default=0)
    ap.add_argument("--radius", type=float, default=3500)
    ap.add_argument("--cache", default=apppaths.saved_cache_dir() or apppaths.default_cache_dir())
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "traffic-probe" / "routes")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    blockworker._s = blockworker._State(a.cache, True, True, False, True, True, sea=True)
    frame = geotiles.WorldFrame(a.lat, a.lon)
    tiles = geotiles.tiles_near(frame, a.x, a.y, min(distanttraffic.FAR_SEND_M, a.radius), distanttraffic.Z)
    manifest, paths, points, raw_bytes = [], set(), 0, 0
    t0 = time.perf_counter()
    for gx, gy, _ in tiles:
        la, _ = geotiles.centre(gx, gy, distanttraffic.Z)
        zoom = dem.zoom_for(la, 1.25, dem.FINE_ZOOM)
        result = blockworker.traffic_geo(gx, gy, zoom, str(a.out), "/temp/beamngEarthTrafficProbe/routes", False, False)
        if result is None:
            continue
        filename = os.path.basename(result[0])
        data = json.loads((a.out / filename).read_text())
        for p in data["paths"]:
            if p["id"] not in paths:
                points += len(p["points"])
                paths.add(p["id"])
        raw_bytes += (a.out / filename).stat().st_size
        manifest.append(filename)
        print(f"{data['key']}: {len(data['paths'])} paths", flush=True)
    summary = {"tiles": len(manifest), "wanted": len(tiles), "routes": len(paths), "points": points,
               "bytes": raw_bytes, "seconds": round(time.perf_counter() - t0, 3)}
    (a.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
