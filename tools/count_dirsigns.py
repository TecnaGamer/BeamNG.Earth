#!/usr/bin/env python3
"""count_dirsigns.py LAT LON [RADIUS_TILES] [NAME] [--roads]: how many direction boards the sign tiles around (LAT, LON) get, split
into town boards and the others, from the map data in ~/.cache/beamng-earth (offline, the same path the server builds a
sign tile by: blockworker.signs_tile). Run it from two trees (the rule before and after a change) to compare.

    python tools/count_dirsigns.py 49.2331 28.4682 4 vinnytsia

With --roads the road tiles under the sign tiles are built first (their props hold the poles town boards hang on).
Prints one line: name, tiles read, boards, of them town boards (urban), the unique faces made, and with --roads how many
town boards hang on a pole and how many stand alone."""
import logging
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))
logging.basicConfig(level=logging.ERROR)
import blockworker as bw  # noqa: E402
import fast  # noqa: E402
import geodesy  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
with_roads = "--roads" in sys.argv or "--modes" in sys.argv
lat, lon = float(args[0]), float(args[1])
rad = int(args[2]) if len(args) > 2 else 3
name = args[3] if len(args) > 3 else f"{lat:.3f},{lon:.3f}"
revisit.ON = False
bw._s = bw._State(os.path.expanduser("~/.cache/beamng-earth"), True, True, True, True, True, True, True)
total = [0, 0]
real = fast._ef.dirsign_place


def counted(*a):
    boards, rows = real(*a)
    # (a board of a neighbouring tile's margin is counted by the tile that owns it: the kernel returns the box's own)
    total[0] += len(boards)
    total[1] += int((boards[:, 6] > 0.5).sum()) if len(boards) else 0
    if "--where" in sys.argv:
        for b in boards:
            print(f"board junction lat {b[9]:.5f} lon {b[8]:.5f} {'town' if b[6] > 0.5 else 'road'}", file=sys.stderr)
    return boards, rows


fast._ef.dirsign_place = counted
modes = {0: 0, 1: 0, -1: 0}
real_mesh = fast._ef.dirsign_mesh


def counted_mesh(signs, origin):
    for r in signs:
        modes[int(round(r[10])) if len(r) >= 12 else -1] += 1
    return real_mesh(signs, origin)


fast._ef.dirsign_mesh = counted_mesh
out = os.environ.get("BNE_COUNT_OUT") or tempfile.mkdtemp(prefix="cntdir_")     # (a game session folder: its road geotiles are the poles)
frame = geodesy.EnuFrame(lat, lon)
tiles = 0
if "--roads" in sys.argv:
    for ty in range(-rad - 1, rad + 1):
        for tx in range(-rad - 1, rad + 1):
            try:
                bw.roads_tile(1, lat, lon, 15, tx, ty, out, "/x", True, 0.0, buildings=True, gas_stations=False, q=1 / 32)
            except Exception as e:
                print("roads skip", tx, ty, type(e).__name__, str(e)[:60], file=sys.stderr)
for ty in range(-rad, rad):
    for tx in range(-rad, rad):
        try:
            bw.signs_tile(1, lat, lon, 15, tx, ty, out, "/x", False, False, None, True)
            tiles += 1
        except Exception as e:      # a cell not in the cache: skipped (offline)
            print("skip", tx, ty, type(e).__name__, str(e)[:60], file=sys.stderr)
faces = len([n for n in os.listdir(out) if n.startswith("bne_dir_")])
print(f"{name}: tiles {tiles}, boards {total[0]}, town boards {total[1]}, faces {faces}"
      + (f", on a pole {modes[0]}, on a stand {modes[1]}" if with_roads else ""))
