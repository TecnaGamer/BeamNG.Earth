"""Exact captured Roundtop Road source geometry and production DEM sampler.

No network, active cache, worker pool or server is constructed. The capture's
manifest records original hashes/provenance; missing geographic input raises.
"""
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dem
import geodesy
import mapgeom

FIXTURE = Path(__file__).parent / "fixtures" / "roundtop_walls"
ROUND_TOP = "78de7c5f-612f-4f7f-8f28-9ee42f10eaeb"
MISSILE = "a966d1b3-e489-4251-9913-7ad5fe74665c"


class CapturedRoundtop:
    def __init__(self):
        self.manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        self.frame = geodesy.EnuFrame(*self.manifest["origin"], 0.)
        self.archive = np.load(FIXTURE / "dem.npz", allow_pickle=False)
        self.decoded = {}
        self.sampler = dem.Dem.__new__(dem.Dem)
        self.sampler.tiles = self.tiles

    def tiles(self, keys, progress=None):
        out = {}
        for key in keys:
            if key not in self.decoded:
                name = "tile_" + "_".join(str(v) for v in key)
                if name not in self.archive:
                    raise AssertionError(f"query outside captured DEM: {key}")
                self.decoded[key] = self.archive[name]
            out[key] = self.decoded[key]
        return out

    def raw(self, lat, lon):
        return self.sampler.sample(lat, lon, 16)

    def roads(self, x=3418, y=6133):
        with np.load(FIXTURE / f"roads_{x}_{y}.npz", allow_pickle=False) as archive:
            return mapgeom.Features.from_arrays("roads", dict(archive), "")

    def close(self):
        self.archive.close()


class CapturedRoundtopPlans:
    """Production grading against a read-only snapshot of the world's built plans.

    The four captured cells include the active world's neighbor/junction/bridge
    context. This validates consumers of that world snapshot; it does not claim
    a fresh independent rebuild of all upstream regional planning inputs.
    """
    def __init__(self, capture):
        import grading
        import geotiles
        import mapcells
        import plancache
        self.capture = capture
        self.manifest = json.loads((FIXTURE / "cached_plans_manifest.json").read_text(encoding="utf-8"))
        self.frame = geotiles.region_frame(*self.manifest["region"])
        self.planner = grading._Planner(self.frame, None, None)
        self.keys = set()
        cache = plancache.PlanCache(str(FIXTURE / "cached_plans"), self.manifest["signature"])
        for record in self.manifest["plans"]:
            key = mapcells.CellKey(*record["key"])
            got = cache.load(key)
            if got is None:
                raise AssertionError(f"captured plan missing or invalid: {key}")
            plans, keeps, covers, opens = got
            self.keys.add(key)
            self.planner.cells[key] = plans
            self.planner.keeps[key] = keeps
            self.planner.covers[key] = covers
            self.planner.opens[key] = opens

    def final(self, lat, lon, lift=None):
        import grading
        lat, lon = np.asarray(lat), np.asarray(lon)
        missing = set(grading._Planner._keys(lat, lon)) - self.keys
        if missing:
            raise AssertionError(f"query outside captured built-plan context: {sorted(str(k) for k in missing)}")
        return self.planner.apply(lat, lon, self.capture.raw(lat, lon),
                                  grading.ROAD_LIFT if lift is None else lift)
