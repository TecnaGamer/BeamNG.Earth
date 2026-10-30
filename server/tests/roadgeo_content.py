"""roadgeo_content.py OUT LAT LON ANCHOR_LAT ANCHOR_LON: a helper process of test_roadgeo.py. Builds the anchor road tile
of anchor (ANCHOR_LAT, ANCHOR_LON) that holds (LAT, LON) first (its grading views, chain views and caches warm, as a
session would have them), then the geotile under (LAT, LON)'s content in its own frame; prints the content files'
sha1s (one "name hash" line each). Map data from ~/.cache/beamng-earth, offline; the revisit cache off."""
import hashlib
import logging
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
logging.basicConfig(level=logging.WARNING)
import blockworker as bw  # noqa: E402
import geodesy  # noqa: E402
import geotiles  # noqa: E402
import revisit  # noqa: E402
import roads  # noqa: E402

out, lat, lon, alat, alon = sys.argv[1], *map(float, sys.argv[2:6])
revisit.ON = False
bw._s = bw._State(os.path.expanduser("~/.cache/beamng-earth"), True, True, True, True, True, True, True)
os.makedirs(os.path.join(out, "anchor"), exist_ok=True)
f = geodesy.EnuFrame(alat, alon)
x, y, _ = f.geodetic_to_enu(lat, lon, 0.0)
tx, ty = int(math.floor(float(x) / roads.TILE)), int(math.floor(float(y) / roads.TILE))
bw.roads_tile(1, alat, alon, 15, tx, ty, os.path.join(out, "anchor"), "/x", True, 0.0, buildings=True,
              gas_stations=True, q=1 / 32)
gx, gy = geotiles.tile_of(lat, lon)
bw.roads_geo(1, alat, alon, 15, gx, gy, 15, out, "/x", True, 0.0, True, False, True, 1 / 32, True, skin=False)
for n in sorted(os.listdir(out)):
    if n.startswith("rg_"):
        print(n, hashlib.sha1(open(os.path.join(out, n), "rb").read()).hexdigest())
