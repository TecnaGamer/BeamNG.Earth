"""Cache keys follow every input that changes output (the road revamp's R1, 2026-11-16).

- The elevation's identity (mapdata.dem_identity: dem.ELEVATION_ID pinned per app release, the decoding, the measured
  road heights' version) is in MapData.versions(), so in the road plans' and cell facts' keys, the road tiles' and the
  shared decisions' version key: plans made on another elevation are never found under the same key.
- The BNGE_* reads of the road output modules either change no output (NO_OUTPUT: each with its reason) or are keyed
  like the build switches (revisit.OUTPUT_ENV); a new read fails here until it is one or the other.
- A helper's per-client roads-in-the-ground setting is in the road tile's key.
"""
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import decisions  # noqa: E402
import dem  # noqa: E402
import mapdata  # noqa: E402
import plancache  # noqa: E402
import revisit  # noqa: E402

# The modules whose output is road plans, cell facts, road tiles, paint or the elevation under them.
OUTPUT_MODULES = (
    "roads", "grading", "roadmesh", "surface", "mapdata", "overture", "osmtags", "oneway_joins", "lanesections",
    "levelways", "roadsmooth", "railways", "railtags", "markings", "rampmerges", "gradesep", "roadz", "dem",
    "footbridges", "bridgetypes", "carriageways", "crossings", "roadlanes", "roadwidth", "navgraph", "plancache",
    "decisions", "l0roads", "sidewalks", "guardrails", "tunnellids", "subsea", "deckspans", "railroadseam",
    "junctions", "roadcorners", "medians", "medianbarriers", "railprofile", "railbridges", "portals", "germanroads",
    "franceroads", "europewalks", "roadsurf", "lidarz", "pmtiles", "hrdem", "lidarbc", "mtarchive", "polardem",
    "bdtopo", "blockworker", "specs", "tapers", "experimental")

# BNGE_* reads of those modules that change no output, and why.
NO_OUTPUT = {
    "BNGE_FACTS_LOG": "a log line per cell's facts",
    "BNGE_HANDOVER_DUMP": "a probe's JSON of the deck ends' hand-over beside the exact deck (BNE_ROADSYS=structures)",
    "BNGE_PLAN_RUN": "the run's shared plan folder (where, not what)",
    "BNGE_ENU_AHEAD": "when the ENU frames are made (scheduling)",
    "BNGE_BNWT_URL": "where the prebaked input tiles are read (the same Overture rows)",
    "BNGE_MAP_SOURCE": "keyed by MapData.versions()['mapsrc'] (pmtiles.source_key)",
    "BNGE_OSM_FUEL": "keyed by MapData.versions()['osmfuel']",
    "BNGE_DUCKDB_THREADS": "DuckDB's threads per query",
    "BNGE_OVERTURE_RELEASE": "keyed by MapData.versions()['overture'] (overture.effective: the release every reader "
                             "of the process uses, frozen at its first ask; tests/test_overture_release.py)",
    "BNGE_OVERTURE_DEDUP_ALWAYS": "the dedup by id runs where an id repeats either way: the same rows",
    "BNGE_LIDAR_CLAIMS": "which process reads the lidar (scheduling)",
    "BNGE_LIDAR_PREFETCH": "lidar read ahead (scheduling)",
    "BNGE_LIDAR_PREFETCH_CELLS": "lidar read ahead (scheduling)",
    "BNGE_LIDAR_PREFETCH_WAIT": "lidar read ahead (scheduling)",
    "BNGE_LIDAR_THREADS": "lidar reader threads",
    "BNGE_LIDARZ": "the project's signed copy of the same lidar answers (lidarz.lkey_doc holds roadz.VERSION)",
    "BNGE_LIDARZ_URL": "where that copy is read",
    "BNGE_LIDARZ_WAIT": "how long it is waited for (a miss reads the point clouds: the same answer)",
    "BNGE_NET_MBPS": "the network's speed estimate",
    "BNGE_PRECACHE_LIDAR_WAIT": "a wait (an incomplete read is a revisit fault, never kept)",
    "BNGE_ROADZ_SESSION": "the run's session folder",
    "BNGE_DEM_ARCHIVES": "Mapterhorn's tiles from its archives or its endpoint: the same tiles (pinned per release: "
                         "dem.ELEVATION_ID)",
    "BNGE_DEM_CDN": "Mapterhorn's z0-12 from the project's packs: the same bytes as its endpoint (dem.py; set from the "
                    "remote switch file: keyed, a failed read would split the caches)",
    "BNGE_DEM_MIRROR_MB": "the decoded tiles' disk budget",
    "BNGE_DEM_MIRROR_ZSTD": "the decoded tiles' on-disk packing (lossless)",
    "BNGE_DEM_SINGLE_FLIGHT": "one download per tile on the machine (scheduling)",
    "BNGE_PLANCACHE": "the within-run plan cache on or off (the same plans)",
    "BNGE_DECISIONS": "the shared archives on or off (their items are this server's own keys' plans)",
    "BNGE_DECISIONS_PUBKEY": "the builder keys accepted",
    "BNGE_DECISIONS_REQUEST": "asking for an archive to be built",
    "BNGE_DECISIONS_REQUEST_URL": "where",
    "BNGE_DECISIONS_URL": "where the archives are read",
    "BNGE_L0_ROADS": "the teleport's provisional block only (replaced by the graded block; blockcache keys every "
                     "BNGE_*)",
    "BNGE_L0_GROUND": "the provisional block's stand-in photo (likewise)",
    "BNGE_L0_BLEND_POW": "the provisional block only (likewise)",
    "BNGE_L0_BLUR_M": "the provisional block only (likewise)",
    "BNGE_CACHE_DIR": "the cache folder",
    "BNGE_ROADSURF_T0": "the server's start time handed to its processes",
    "BNGE_PMT_BASE": "where Overture's tiles are read",
    "BNGE_MT_BINDINGS": "where Mapterhorn's archive list is read",
    "BNGE_BDTOPO_WAIT": "a wait (when a tile is built, not what)",
    "BNGE_DEM_STATS": "statistics",
    "BNGE_FARTRACE": "a trace",
    "BNGE_MEMATTR_S": "memory attribution",
    "BNGE_SAMPLE": "a sampling profiler",
    "BNGE_STACK_DUMP_S": "stack dumps",
    "BNGE_TRACEMALLOC": "memory tracing",
}

NAME = re.compile(r"BNGE_[A-Z0-9_]*[A-Z0-9]")


def reads():
    """{BNGE_* name: [modules]} of the output modules."""
    out = {}
    for m in OUTPUT_MODULES:
        p = HERE / f"{m}.py"
        if not p.exists():
            continue
        for name in set(NAME.findall(p.read_text(encoding="utf-8"))):
            out.setdefault(name, []).append(m)
    return out


class EnvReadTest(unittest.TestCase):
    def test_every_read_is_keyed_or_changes_nothing(self):
        got = reads()
        self.assertIn("BNGE_ROADS_IN_GROUND", got, "the scan sees the modules")
        unknown = {k: v for k, v in got.items() if k not in NO_OUTPUT and k not in revisit.OUTPUT_ENV}
        self.assertEqual(unknown, {}, "a BNGE_* read of an output module: key it (revisit.OUTPUT_ENV, or a BNE_* "
                                      "switch) or list it in NO_OUTPUT with the reason it changes nothing")

    def test_output_settings_are_keyed(self):
        for name in revisit.OUTPUT_ENV:
            revisit._switches.clear()
            base = revisit.switches_key()
            with mock.patch.dict(os.environ, {name: "0"}):
                revisit._switches.clear()
                self.assertNotEqual(revisit.switches_key(), base, name)
                self.assertTrue(revisit.keyed_env(name))
            revisit._switches.clear()

    def test_not_switches_stay_out(self):
        self.assertFalse(revisit.keyed_env("BNGE_DEM_CDN"))
        self.assertFalse(revisit.keyed_env("BNE_TELEMETRY"))
        self.assertTrue(revisit.keyed_env("BNE_ROADSURF"))


def _md():
    """A MapData's attributes versions() reads, nothing loaded."""
    return types.SimpleNamespace(release="2026-test", osm_tags_build=lambda network=True: "layercake")   # (versions() asks it)


class ElevationKeyTest(unittest.TestCase):
    def test_versions_carry_the_elevation(self):
        v = mapdata.MapData.versions(_md())
        self.assertEqual(v["dem"], mapdata.dem_identity())
        with mock.patch.object(dem, "ELEVATION_ID", dem.ELEVATION_ID + 1):
            self.assertNotEqual(mapdata.MapData.versions(_md())["dem"], v["dem"], "a release's new elevation")
        with mock.patch.object(dem, "DECODED_VERSION", dem.DECODED_VERSION + 1):
            self.assertNotEqual(mapdata.MapData.versions(_md())["dem"], v["dem"], "a new decoding")

    def test_decisions_and_plans_follow_it(self):
        a = decisions.vkey(mapdata.MapData.versions(_md()))
        pc = plancache.PlanCache.__new__(plancache.PlanCache)
        pc.persist = ("/nonexistent", (plancache.FRAMES, "w1s0", mapdata.MapData.versions(_md())))
        k = pc._revisit_key((14, 1, 2))
        with mock.patch.object(dem, "ELEVATION_ID", dem.ELEVATION_ID + 1):
            self.assertNotEqual(decisions.vkey(mapdata.MapData.versions(_md())), a, "the shared archives' key")
            pc.persist = ("/nonexistent", (plancache.FRAMES, "w1s0", mapdata.MapData.versions(_md())))
            self.assertNotEqual(pc._revisit_key((14, 1, 2)), k, "the plans' key")


class RoadTileKeyTest(unittest.TestCase):
    def test_roads_in_the_ground_in_the_tile_key(self):
        import blockworker
        import roads
        keys = []

        def capture(kind, *parts):
            keys.append(parts)
            raise StopIteration

        for on in (True, False):
            with mock.patch.object(roads, "ROADS_IN_GROUND", on), mock.patch.object(revisit, "key", capture), \
                    mock.patch.object(blockworker, "_s", types.SimpleNamespace(md=None)):
                with self.assertRaises(StopIteration):
                    blockworker._roads_tile_cached("a", 39.28, -76.6, 14, 0, 0, "/nonexistent", None, True, 0.0,
                                                   False, False, True, None, False, 0.0, None)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1], "a helper serving both settings keeps their tiles apart")


if __name__ == "__main__":
    unittest.main()
