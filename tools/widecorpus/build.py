"""Build production road geotiles and capture the inputs needed to audit their emitted meshes."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "server"), str(HERE.parent)]
import npyguard

npyguard.apply()
import numpy as np

import blockworker as bw
import dem
import earthd
import furniture
import geodesy
import geotiles
import mapcells
import pwd
import navgraph
import roadmesh
import roads
import sidewalks
import revisit
import netfetch
from dataclasses import replace


def source_lines(frame, features, quad):
    """Ground/bridge/tunnel intervals from the source map, independently of emitted road meshes."""
    lines = []
    seen = set()
    low, high = quad.min(0), quad.max(0)
    for featureset in features:
        specs = roads.road_specs(featureset)
        for i, spec in enumerate(specs):
            fid = str(featureset.ids[i])
            if fid in seen or spec is None or spec.cls in {"footway", "cycleway", "path", "steps", "pedestrian"}:
                continue
            seen.add(fid)
            for level in ("ground", "bridge", "tunnel"):
                for ring in roads._parts_by_level_new(featureset, i, level, rngs=roads.tag_level_ranges(featureset, i)):
                    x, y, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.)
                    xy = np.column_stack((x, y))
                    if len(xy) < 2 or (xy.max(0) < low).any() or (xy.min(0) > high).any():
                        continue
                    lines.append(dict(id=fid, level=level, width=spec.width, lanes=spec.lanes or
                                      (1 if spec.oneway else 2), paved=spec.kind in ("paved", "cobble"),
                                      cls=str(spec.cls), drive=round(float(roads.lane_layout(spec)[0]), 2),
                                      urban=int(bool(spec.urban)), oneway=int(bool(spec.oneway)),
                                      xy=xy.tolist()))
    return lines


def build(place, cache, out, radius, offline):
    # Use the server's own cross-process admission gate for every host in a concurrent cold run.
    orig_gate = netfetch.shared_gate
    netfetch.shared_gate = lambda host, policy, d=None: orig_gate(host, replace(policy, shared=True), d)
    import fsspec, duckdb  # fail at startup if source data cannot be read
    bw.init(str(cache), offline, True, True, True, True, None, logging.WARNING, False, True)
    lat, lon = place["lat"], place["lon"]
    frame = geodesy.EnuFrame(lat, lon)
    zoom = dem.zoom_for(lat, 1.25, dem.FINE_ZOOM)
    q = earthd.survey_q(frame, bw._s.dem)
    country = place.get("country") or navgraph.country_at(bw._s.airports, lat, lon, True)
    rules = navgraph.road_rules(country)
    # Observe the game's cover list and the quantized terrain lattice used to drape the road skin.
    cover = []
    original = roads.build_tile
    def capture_cover(fr, *args, **kwargs):
        result = original(fr, *args, **kwargs)
        for line in kwargs.get("cover_out") or ():
            xy = np.asarray(line["n"])[:, :2]
            nodes = geotiles.world_xy_map(fr, frame, xy)
            cover.extend([*a, *b, line["w"]] for a, b in zip(nodes, nodes[1:]))
        return result

    outlines = []
    layers = {}
    mesh_build = roadmesh.build
    add_walk = sidewalks.Builder.add_walk

    def capture_mesh(fr, chunks, elev, origin, *args, **kwargs):
        if kwargs.get("clip_poly") is not None:
            for c in chunks:
                layers[c["m"]] = roadmesh.layer_of(c)
                if c.get("tri") and len(c.get("T",())):
                    from checks import boundary_rings
                    outlines.extend(boundary_rings(c["xy"],c["T"]))
                if c.get("poly") and not c.get("quads"):
                    outlines.append(np.asarray(c["poly"])[:, :2].tolist())
        return mesh_build(fr, chunks, elev, origin, *args, **kwargs)

    def capture_walk(builder, walk, width):
        count = len(builder.pieces)
        value = add_walk(builder, walk, width)
        for piece in builder.pieces[count:]:
            corner, curb, direction, outer, _, w = piece[:6]
            back = outer[:, :2] if corner else (curb[:, :2] + w * direction)[::-1]
            outlines.append(np.vstack((curb[:, :2], back)).tolist())
        return value

    if offline:
        roads.build_tile = capture_cover
        roadmesh.build = capture_mesh
        sidewalks.Builder.add_walk = capture_walk
    tiles = []
    try:
        for gx, gy, _ in sorted(geotiles.tiles_near(frame, 0, 0, radius, exact=True)):
            key = geotiles.key(gx, gy)
            la, lo = geotiles.centre(gx, gy)
            cover.clear()
            outlines.clear()
            layers.clear()
            quad = np.asarray(geotiles.quad(frame, gx, gy))[:, :2]
            extent = np.ptp(quad, axis=0).max() + 8
            side = 2 ** int(np.ceil(np.log2(extent)))
            cx, cy = np.round((quad.max(0) + quad.min(0)) / 2).astype(int).tolist()
            xx, yy = np.array([cx-side/2-2, cx+side/2+2]), np.array([cy-side/2-2, cy+side/2+2])
            xs, ys = np.meshgrid(xx, yy)
            lats, lons, _ = frame.enu_to_geodetic(xs.ravel(), ys.ravel(), 0.)
            cells = mapcells.cells_for_points(lats, lons)
            features = [v.layers["roads"] for v in bw._s.md.cells(cells, layers=("roads", "landcover"))]
            start = time.monotonic()
            with revisit.scope() as faults:
                result = bw.roads_geo(1, lat, lon, zoom, gx, gy, dem.zoom_for(la, 1.25, dem.FINE_ZOOM),
                                      str(out), "/tiles", True, 0., False, rules["rightHandDrive"], rules["mph"], q,
                                      False, furniture.buried_share(country), furniture.lamp_region(country, None),
                                      country, exact="auto")
            elapsed = time.monotonic() - start
            if not result["content"] or not result["skin"]:
                raise ValueError(f"{key} missing content or skin")
            block = bw.build(1, lat, lon, zoom, q, cx, cy, side, str(out), key+".png", key+".ter")
            if offline:
                if not block.get("roadsComplete") or not block.get("surfaceComplete"):
                    raise ValueError(f"{key} incomplete terrain layers")
                surveyed = source_lines(frame, features, quad)
                context = dict(lines=surveyed, quad=quad.tolist(), outlines=outlines, layers=layers,
                               block={k:block[k] for k in ("cx", "cy", "zBase", "q", "mats") if k in block})
                (out / (key + ".source.json")).write_text(json.dumps(context))
                np.save(out / (key + ".lanes.npy"), np.asarray(cover).reshape(-1, 5))
            tiles.append(dict(key=key, seconds=round(elapsed, 3), exact=result["exact"],
                              source_faults=faults.n, lidar_waits=faults.lidar))
            print(key, round(elapsed, 1), flush=True)
    finally:
        roads.build_tile, roadmesh.build = original, mesh_build
        sidewalks.Builder.add_walk = add_walk
    (out / "tiles.json").write_text(json.dumps(tiles))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("place", type=json.loads)
    ap.add_argument("cache", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("radius", type=float)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    scratch = Path(pwd.getpwuid(os.getuid()).pw_dir) / "bnge-tests/widecorpus/cache"
    if not all(path.resolve().is_relative_to(scratch) for path in (args.cache, args.out)):
        ap.error("worker cache and outputs must be below ~/bnge-tests/widecorpus/cache")
    build(**vars(args))
