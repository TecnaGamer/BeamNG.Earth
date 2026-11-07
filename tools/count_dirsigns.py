#!/usr/bin/env python3
"""count_dirsigns.py LAT LON [RADIUS_TILES] [NAME]: how many direction boards the sign tiles around (LAT, LON) get, split
into town boards and the others, from the map data in ~/.cache/beamng-earth (offline, the same path the server builds a
sign tile by: blockworker.signs_tile). Run it from two trees (the rule before and after a change) to compare.

    python tools/count_dirsigns.py 49.2331 28.4682 4 vinnytsia

Prints one line: name, tiles read, boards, of them town boards (urban), the unique faces made."""
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

lat, lon = float(sys.argv[1]), float(sys.argv[2])
rad = int(sys.argv[3]) if len(sys.argv) > 3 else 3
name = sys.argv[4] if len(sys.argv) > 4 else f"{lat:.3f},{lon:.3f}"
revisit.ON = False
bw._s = bw._State(os.path.expanduser("~/.cache/beamng-earth"), True, True, True, True, True, True, True)
total = [0, 0]
real = fast._ef.dirsign_place


def counted(*a):
    boards, rows = real(*a)
    # (a board of a neighbouring tile's margin is counted by the tile that owns it: the kernel returns the box's own)
    total[0] += len(boards)
    total[1] += int((boards[:, 6] > 0.5).sum()) if len(boards) else 0
    return boards, rows


fast._ef.dirsign_place = counted
out = tempfile.mkdtemp(prefix="cntdir_")
frame = geodesy.EnuFrame(lat, lon)
tiles = 0
for ty in range(-rad, rad):
    for tx in range(-rad, rad):
        try:
            bw.signs_tile(1, lat, lon, 15, tx, ty, out, "/x", False, False, None, True)
            tiles += 1
        except Exception as e:      # a cell not in the cache: skipped (offline)
            print("skip", tx, ty, type(e).__name__, str(e)[:60], file=sys.stderr)
faces = len([n for n in os.listdir(out) if n.startswith("bne_dir_")])
print(f"{name}: tiles {tiles}, boards {total[0]}, town boards {total[1]}, faces {faces}")
