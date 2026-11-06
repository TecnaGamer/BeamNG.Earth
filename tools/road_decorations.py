#!/usr/bin/env python3
"""Read-only road-decoration probe of explicitly supplied source-cache NPZ files.

No MapData/Fetcher/server is created, no cache is configured or written, and no
network is used. Example (the paths identify exactly which source release is read):

  python -B tools/road_decorations.py --roads <roads.7/.../y.npz> \
    --landcover <landcover/.../y.npz> --id w123 --region na:sparse

Repeat --roads/--landcover to provide neighboring cells. Output is JSONL. Missing
land cover remains explicitly reported and must not be read as evidence of rural
land. Near-generation policy is recomputed; active tile cache hits are not probed.
"""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import numpy as np

import mapgeom
import maprasters
import roaddecor_debug
import roads
import surface


def arrays(path):
    with np.load(path, allow_pickle=False) as archive:
        return {key: archive[key] for key in archive.files}


class CachedCover:
    """The same warped WorldCover point lookup, with explicit incomplete coverage."""
    def __init__(self, files):
        self.layers = []
        self.queries = self.missing = 0
        for path in files:
            data = arrays(path)
            prefixes = [""] if "meta" in data else [key[:-4] for key in data if key.endswith(".meta")]
            if not prefixes:
                raise ValueError("--landcover has no WorldCover raster metadata")
            for prefix in prefixes:
                meta = json.loads(str(data[prefix + "meta"]))
                if meta["crs"] != "EPSG:4326":
                    raise ValueError("--landcover must be a cached WorldCover EPSG:4326 raster")
                self.layers.append(maprasters.Raster(data[prefix + "data"], **meta))

    def urban_locations(self, lat, lon):
        return surface.warp(np.asarray(lat), np.asarray(lon))

    def urban(self, lat, lon):
        lat, lon = self.urban_locations(lat, lon)
        values = np.zeros(lat.shape, np.uint8)
        present = np.zeros(lat.shape, bool)
        for raster in self.layers:
            rows, cols = raster.data.shape
            mask = ((lon >= raster.x0) & (lon < raster.x0 + cols * raster.dx) &
                    (lat <= raster.y0) & (lat > raster.y0 - rows * raster.dy))
            if mask.any():
                values[mask] = raster.sample(lon[mask], lat[mask])
                present[mask] = values[mask] != raster.nodata
        self.queries += int(present.size)
        self.missing += int((~present).sum())
        return values == roads.URBAN_CLASS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roads", action="append", required=True, type=Path)
    parser.add_argument("--landcover", action="append", default=[], type=Path)
    parser.add_argument("--id", action="append", default=[], help="GERS id, OSM way id or w123; default all")
    parser.add_argument("--region", default=None, help="furniture lamp region, e.g. na:sparse")
    parser.add_argument("--town", action="store_true", help="below dense-core tile coverage (does not imply urban)")
    parser.add_argument("--buried", type=float, default=0.0)
    args = parser.parse_args(argv)
    roaddecor_debug.SELECTORS = frozenset(args.id or ["*"])
    roaddecor_debug.ENABLED = True
    cover = CachedCover(args.landcover)
    urban_fn = cover.urban if args.landcover else None
    sources = []
    for path in args.roads:
        data = arrays(path)
        if "missing" in data:
            print(json.dumps({"event": "road_source_missing", "path": str(path)}))
            continue
        feats = mapgeom.Features.from_arrays("roads", data, "")
        sources.append((path, data, feats))
    # Every supplied neighbor participates in the same read-only geometric join.
    layers = [feats for _, _, feats in sources]
    for _, _, feats in sources:
        feats._bnge_pavement = lambda points: layers
    for path, data, feats in sources:
        # No _bnge_cell/revisit setup: all helper facts remain process-local.
        specs = roads._road_specs(feats, urban_fn, False)
        evidence = roads.urban_context(feats, urban_fn) if urban_fn and hasattr(roads, "urban_context") else None
        for i, spec in enumerate(specs):
            if spec is None or not roaddecor_debug.selected(feats, i):
                continue
            record = roaddecor_debug.explain(feats, i, spec, urban_fn, region=args.region, town=args.town,
                                             buried=args.buried, cache_status="offline_recomputed_from_source_cache",
                                             context=evidence[i] if evidence is not None else None)
            record["source_cache"] = {"roads": str(path), "landcover": [str(p) for p in args.landcover],
                                      "provenance": json.loads(str(data["provenance"])) if "provenance" in data else None,
                                      "sample_queries": cover.queries, "missing_landcover_queries": cover.missing,
                                      "coverage_scope": "all road candidates in the supplied source file(s)",
                                      "coverage_complete": bool(args.landcover and cover.missing == 0),
                                      "coverage_complete_meaning": "landcover queries only",
                                      "pavement_sources": [str(p) for p, _, _ in sources],
                                      "pavement_source_completeness": "supplied files only; geographic completeness not verified"}
            print(json.dumps(record, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
