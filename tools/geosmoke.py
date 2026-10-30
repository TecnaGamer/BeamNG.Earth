#!/usr/bin/env python3
"""geosmoke.py [LAT LON] [--km 1.0] [--off] [--far M]: the real server (earthd) with road geotiles, driven like the mod
over the wire: hello (geoTiles 17, roadGeo 1, farGeo 1), an anchor at the place, poses until the road tiles round the
car are in, then a rebase to an anchor KM east (prepare_anchor purpose rebase, commit), poses again at the same spot.
Counts the roads_tile (content) / roads_skin / roads_exact / roads_drop messages of each phase, the far buildings' /
skyline's / far bridges' (bldfar_tile, bldsky_tile, brfar_tile: the mod's far loads) and the time until the tiles round
the car are in after the rebase (load_progress roads). --far M: the far buildings' reach M m (the skyline and far
bridges' 2 M: their env overrides, a short run), each phase waits until no far tile came for 8 s. --off: --geo-tiles
off (the anchor tiles), for the A/B. Map data from ~/.cache/beamng-earth (offline; the revisit cache off). Run from the
repository root."""
import argparse
import math
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))
sys.path.insert(0, os.path.join(HERE, "server", "tests"))
from netclient import Client, stop_server  # noqa: E402

import geodesy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lat", type=float, nargs="?", default=39.2834)
    ap.add_argument("lon", type=float, nargs="?", default=-76.5934)
    ap.add_argument("--km", type=float, default=1.0)
    ap.add_argument("--off", action="store_true")
    ap.add_argument("--far", type=float, default=0.0)
    args = ap.parse_args()
    lat, lon = args.lat, args.lon
    user = tempfile.mkdtemp(prefix="geosmoke_")
    env = dict(os.environ, BNE_REVISIT="off")
    if args.far:
        env.update(BNE_BLDFAR_SEND_M=str(args.far), BNE_SKY_SEND_M=str(2 * args.far),
                   BNE_BRIDGE_FAR_SEND_M=str(2 * args.far))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server", "earthd.py"), "--port", "0", "--user-dir", user,
         "--cache-dir", os.path.expanduser("~/.cache/beamng-earth"), "--weather", "off", "--gravity", "off",
         "--update-check", "off", "--offline", "--imagery", "off", "--near-visual", "terrain", "--buildings", "on",
         "--traffic", "off", "--geo-tiles", "off" if args.off else "on"],
        stdout=subprocess.PIPE, stderr=open(os.path.join(user, "server.log"), "w"), text=True, env=env)
    try:
        line = proc.stdout.readline()
        port = int(line.split("127.0.0.1:")[1].split()[0])
        c = Client(port)
        c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "0.4.0", "geoTiles": 17, "roadGeo": 1,
                "farGeo": 1, "vegFlat": True})
        hello, _ = c.until(lambda m: m["type"] == "hello", timeout=20)
        print(f"hello: roadGeo {hello.get('roadGeo')}, geoTiles {hello.get('geoTiles')}, farGeo {hello.get('farGeo')}")

        def anchor(rid, la, lo, purpose=None, px=0.0, py=0.0):
            m = {"type": "prepare_anchor", "requestId": rid, "lat": la, "lon": lo}
            if purpose:
                m.update(purpose=purpose, px=px, py=py)
            c.send(m)
            a, _ = c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=300)
            assert a["type"] == "anchor_ready", a
            c.send({"type": "commit_anchor", "anchorId": a["anchorId"]})
            t = a["terrain"]
            c.send({"type": "commit_terrain", "anchorId": a["anchorId"], "cx": t["cx"], "cy": t["cy"]})
            return a

        far_kinds = ("bldfar_tile", "bldsky_tile", "brfar_tile", "bldfar_drop", "bldsky_drop", "brfar_drop")

        def phase(aid, x, y, z, label, t_end=240.0):
            counts = {"roads_tile": 0, "roads_skin": 0, "roads_exact": 0, "roads_drop": 0}
            if args.far:
                counts.update({k: 0 for k in far_kinds})
                t_end = max(t_end, 900.0)
            t0 = time.monotonic()
            done_at = None
            far_at = t0
            last = 0.0
            while time.monotonic() - t0 < t_end:
                if args.far and done_at is not None and time.monotonic() - far_at > 8.0:
                    break
                if time.monotonic() - last > 0.25:
                    c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": z, "lat": lat, "lon": lon,
                            "speed": 0, "vx": 0, "vy": 0})
                    last = time.monotonic()
                try:
                    m, _ = c.until(lambda m: True, timeout=0.3)
                except Exception:
                    continue
                k = m.get("type")
                if k in counts:
                    counts[k] += 1
                if k in far_kinds:
                    far_at = time.monotonic()
                if k == "load_progress" and m.get("anchorId") == aid:
                    r = m.get("roads") or [0, 0]
                    if r[1] and r[0] >= r[1] and done_at is None:
                        done_at = time.monotonic() - t0
                        if not args.far:
                            t_end = min(t_end, done_at + 5.0)       # (5 s more: the exact variants, late ones)
            print(f"{label}: {counts}, the road tiles round the car in after "
                  f"{'%.1f s' % done_at if done_at is not None else 'never'}")
            return counts
        a1 = anchor(1, lat, lon)
        phase(a1["anchorId"], 0.0, 0.0, a1["groundZ"] + 1, "first visit")
        lon2 = lon + args.km * 1000.0 / (111320.0 * math.cos(math.radians(lat)))
        f2 = geodesy.EnuFrame(lat, lon2)
        x2, y2, _ = f2.geodetic_to_enu(lat, lon, 0.0)
        a2 = anchor(2, lat, lon2, purpose="rebase", px=float(x2), py=float(y2))
        phase(a2["anchorId"], float(x2), float(y2), a1["groundZ"] + 1, f"after a rebase {args.km:g} km east")
        c.close()
    finally:
        stop_server(proc)
        log = open(os.path.join(user, "server.log")).read()
        bad = [ln for ln in log.splitlines() if " WARNING " in ln or " ERROR " in ln]
        bad = [b for b in bad if "far tile" not in b and "DemUnavailable" not in b]     # (offline: the far field's DEM)
        print(f"server log: {len(bad)} warnings / errors besides the far field's offline DEM"
              + "".join("\n  " + b[:240] for b in bad[:12]))
        import shutil
        shutil.rmtree(user, ignore_errors=True)


if __name__ == "__main__":
    main()
