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
DEGRADE = os.environ.get("BNE_DEGRADE")
if DEGRADE:         # (the degradation test, degrade.py: the map data read with a step's data removed)
    sys.path.insert(0, str(HERE))
    import degrade
    degrade.install(DEGRADE)
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
import mapgeom
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


BUILDINGS_HALF_M = 250.0


def spec_lines(frame, features, quad, urban_fn, widths, left_hand):
    """The degradation test's road records round quad: each road's resolved cross-section as the road tiles take it
    (road_specs with the land cover and the measured widths), its centre line by level (anchor frame)."""
    lines = []
    seen = set()
    low, high = quad.min(0) - 20, quad.max(0) + 20
    for featureset in features:
        if featureset is None or not len(featureset):
            continue
        specs = roads.road_specs(featureset, urban_fn, widths, left_hand)
        for i, spec in enumerate(specs):
            fid = str(featureset.ids[i])
            if fid in seen or spec is None:
                continue
            seen.add(fid)
            drive, lanes = roads.lane_layout(spec)
            for level in ("ground", "bridge", "tunnel"):
                for ring in roads._parts_by_level_new(featureset, i, level, rngs=roads.tag_level_ranges(featureset, i)):
                    x, y, _ = frame.geodetic_to_enu(ring[:, 1], ring[:, 0], 0.)
                    xy = np.column_stack((x, y))
                    if len(xy) < 2 or (xy.max(0) < low).any() or (xy.min(0) > high).any():
                        continue
                    lines.append(dict(id=fid, level=level, cls=str(spec.cls), width=round(float(spec.width), 3),
                                      lanes=int(lanes), drive=round(float(drive), 3), oneway=int(bool(spec.oneway)),
                                      urban=int(bool(spec.urban)), kind=str(spec.kind),
                                      mapped_lanes=int(spec.lanes) if spec.lanes else 0, xy=xy.tolist()))
    return lines


def building_records(frame, cells, quad):
    """The degradation test's buildings in quad's box: (footprints [[id, outer ring]], drawn pieces [[ring, bottom m,
    top of walls m, floors, roof]], ids of the footprints with a mapped height or levels) in the anchor frame, from the map layers alone (buildings.tile_buildings without the
    measured extras: LoD2, 3DBAG, New York's massing, photos)."""
    import buildings
    x0, y0 = quad.min(0)
    x1, y1 = quad.max(0)
    layers = [c.layers.get("buildings") for c in cells]
    parts = [c.layers.get("building_parts") for c in cells]
    flat = lambda la, lo: np.zeros(np.shape(la))      # noqa: E731
    items = buildings.tile_buildings(frame, float(x0), float(y0), float(x1), float(y1), layers, flat,
                                     part_layers=parts)
    pieces = []
    for spec, *_ in items:
        ring = np.asarray(spec.ring, np.float64)[:, :2] + [x0, y0]
        mh = float(spec.min_h or 0.0)
        pieces.append([np.round(ring, 2).tolist(), round(mh, 3), round(mh + float(spec.height or 0.0), 3),
                       int(spec.floors or 0), str(spec.roof or "")])
    feet, mapped = [], []
    seen = set()
    for f in layers:
        if f is None or not len(f):
            continue
        a = f.attrs
        tagged = np.zeros(len(f), bool)
        for col in ("height", "num_floors"):
            if a.get(col) is not None:
                tagged |= np.isfinite(np.asarray(a[col], np.float64))
        for i in range(len(f)):
            fid = str(f.ids[i])
            if fid in seen:
                continue
            ps = f.parts(i)
            if not ps or not ps[0]:
                continue
            outer = np.asarray(ps[0][0], np.float64)
            e, n = mapgeom.enu_xy(f, frame, outer)
            c = np.array([e[:-1].mean(), n[:-1].mean()]) if len(e) > 1 else np.array([e[0], n[0]])
            if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
                continue
            seen.add(fid)
            feet.append([fid, np.round(np.column_stack((e, n)), 2).tolist()])
            if tagged[i]:
                mapped.append(fid)
    return feet, pieces, mapped


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
            if DEGRADE:     # (the degradation test: the resolved cross-sections)
                specs = spec_lines(frame, features, quad, bw._s.surface.urban if bw._s.surface else None,
                                   bw._s.widths, bool(rules["rightHandDrive"]))
                (out / (key + ".specs.json")).write_text(json.dumps(specs))
            tiles.append(dict(key=key, seconds=round(elapsed, 3), exact=result["exact"],
                              source_faults=faults.n, lidar_waits=faults.lidar))
            print(key, round(elapsed, 1), flush=True)
    finally:
        roads.build_tile, roadmesh.build = original, mesh_build
        sidewalks.Builder.add_walk = add_walk
    if DEGRADE:
        # (the degradation test's buildings: a BUILDINGS_HALF_M square round the place, read online too, so the warm
        # build caches their cells)
        box = np.array([[-BUILDINGS_HALF_M, -BUILDINGS_HALF_M], [BUILDINGS_HALF_M, BUILDINGS_HALF_M]])
        la, lo, _ = frame.enu_to_geodetic(box[:, 0], box[:, 1], 0.)
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        feet, pieces, mapped = building_records(frame, bw._s.md.cells(keys, layers=("buildings", "building_parts")),
                                                box)
        (out / "buildings.json").write_text(json.dumps(dict(footprints=feet, pieces=pieces, mapped=mapped)))
    (out / "tiles.json").write_text(json.dumps(tiles))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("place", type=json.loads)
    ap.add_argument("cache", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("radius", type=float)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    tests = Path(pwd.getpwuid(os.getuid()).pw_dir) / "bnge-tests"
    # (WIDE_CORPUS_ROOT: another corpus scratch folder under ~/bnge-tests, the degradation test's)
    scratch = Path(os.environ.get("WIDE_CORPUS_ROOT") or tests / "widecorpus").resolve() / "cache"
    if not scratch.is_relative_to(tests.resolve()) or not all(
            path.resolve().is_relative_to(scratch) for path in (args.cache, args.out)):
        ap.error("worker cache and outputs must be below the corpus scratch cache (~/bnge-tests/.../cache)")
    build(**vars(args))
