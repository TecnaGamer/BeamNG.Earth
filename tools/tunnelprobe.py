#!/usr/bin/env python3
"""Inspect production tunnel floors and meshes using actual cached map/DEM sources.

python3 tools/tunnelprobe.py 44.4499 26.1244 --cache-dir /var/tmp/bnge-20261101/tunnel-source-cache
python3 tools/tunnelprobe.py -31.943509 115.853865 --cache-dir /var/tmp/bnge-20261101/tunnel-source-cache
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import dem
import geodesy
import mapcells
import mapdata
import netfetch
import revisit
import roadmesh
import roads


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("lat", type=float)
    ap.add_argument("lon", type=float)
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    ap.add_argument("--radius", type=float, default=600.0)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    revisit.ON = False
    net = netfetch.Fetcher(offline=args.offline)
    md = mapdata.MapData(args.cache_dir, fetcher=net)
    dtm = dem.Dem(os.path.join(args.cache_dir, "dem"), fetcher=net)
    frame = geodesy.EnuFrame(args.lat, args.lon)
    load = lambda k: md.cell(k, layers=("roads",), kind="demand").layers.get("roads")
    raw = lambda la, lo: dtm.sample(la, lo, 15)
    tp = roads.TunnelProfiles(frame, load, raw)
    tp.raw = raw
    cells = [load(mapcells.cell_of(args.lat, args.lon))]
    box = (-args.radius, -args.radius, args.radius, args.radius)
    closed, opened = tp.parts(cells, box), tp.open_parts(cells, box)
    rows = []
    for fid, spec, xy, z, _lon in closed + opened:
        if spec.cls not in roads.CONCRETE_WALL_CLASSES:
            continue
        xy, z = np.asarray(xy), np.asarray(z)
        g = tp._raw_ground(xy)
        arc = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        rows.append({"id": fid, "open": fid in tp.open, "class": spec.cls,
                     "length": float(arc[-1]), "floor_min": float(z.min()), "floor_max": float(z.max()),
                     "cover_min": float((g - z).min()), "cover_max": float((g - z).max()),
                     "max_grade": float(np.max(np.abs(np.diff(z)) / np.maximum(np.diff(arc), 1e-9))),
                     "portals": tp.mouth(fid), "endpoints": np.c_[xy[[0, -1]], z[[0, -1]]].tolist()})
    chunks = roads._tunnel_chunks(tp, cells, box)
    mesh = roadmesh.tunnel(chunks, (0.0, 0.0, 0.0))
    print(json.dumps({"location": [args.lat, args.lon], "sources": md.versions(), "parts": rows,
                      "tube_chunks": sum(bool(c.get("tunnel")) for c in chunks),
                      "tube_triangles": len(mesh[1]) if mesh is not None else 0}, indent=2))


if __name__ == "__main__":
    main()
