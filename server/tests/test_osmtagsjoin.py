"""The OSM road tags joined at the roads layer's decode (mapdata roads 10; R7 of ~/bnge-tests/manifestaudit): a cell
file is Overture's alone, its tags come from the server run's one build of the weekly tag shards (pagesdata's session),
that build is in MapData.versions() ("osmtags") and the decoded roads' cache key, a tile that cannot be read makes a
stand-in (nothing kept is made of it), and decisions' weekly slices hold what a cell's join reads.

No network: two weekly tag shard builds of a small Layercake-like file (test_osm_tag_shards' fixture), served by a
local transport; the roads layer files are written here as Overture's would be (no tags)."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "tools"), str(HERE.parent)]
import mapcells  # noqa: E402
import mapdata  # noqa: E402
import mapfixtures as mf  # noqa: E402
import mapgeom as mg  # noqa: E402
import osmtagshards  # noqa: E402
import pagesdata  # noqa: E402
import revisit  # noqa: E402
import test_osm_tag_shards as ts  # noqa: E402

BASE = "https://shards.test/osmtags"
CELL_A = mapcells.cell_of(ts.LAT0, ts.LON0)
CELL_B = mapcells.CellKey(14, CELL_A.x + 1, CELL_A.y)
LONG = 950001               # a way across both cells, its lane count changed between the two weeks


def week_rows(lanes):
    lat = sum(CELL_A.bounds()[1::2]) / 2
    rows = ts.fixture_rows()
    rows.append((LONG, dict(highway="primary", name="Long Road", lanes=lanes, surface="asphalt"),
                 np.array([[ts.LON0 - 0.035, lat], [ts.LON0 + 0.035, lat + 1e-4]])))
    return rows


def roads_file(key, rows):
    """Cell key's roads layer file as Overture's tiles give it (no OSM tags): a road segment for every fixture way
    whose line reaches the cell, its source record that way."""
    lon0, lat0, lon1, lat1 = key.bounds()
    picked = []
    for wid, tags, line in rows:
        if line is None or tags.get("highway") in ("raceway", "busway", "footway", "construction"):
            continue
        if line[:, 0].max() < lon0 or line[:, 0].min() > lon1 or line[:, 1].max() < lat0 or line[:, 1].min() > lat1:
            continue
        picked.append((wid, tags, np.asarray(line, np.float64)))
    n = len(picked)
    coord_off = np.r_[0, np.cumsum([len(p[2]) for p in picked])].astype(np.int64)
    cx = np.array([(p[2][:, 0].min() + p[2][:, 0].max()) / 2 for p in picked])
    cy = np.array([(p[2][:, 1].min() + p[2][:, 1].max()) / 2 for p in picked])
    ox, oy = mapcells.owner(cx, cy, key.z)
    out = {"ids": mg.text_column([f"{wid:08d}-0000-4000-8000-000000000000" for wid, _t, _l in picked]),
           "kind": np.full(n, mg.LINE, np.uint8), "part_off": np.arange(n + 1, dtype=np.int64),
           "ring_off": np.arange(n + 1, dtype=np.int64), "coord_off": coord_off,
           "lonlat": np.concatenate([p[2] for p in picked]) if n else np.zeros((0, 2)),
           "own": (ox == key.x) & (oy == key.y), "clipped": np.zeros(n, bool),
           "attr.class": mg.text_column([{"primary": "primary", "motorway": "motorway"}.get(t["highway"], "residential")
                                         for _w, t, _l in picked]),
           "attr.subtype": mg.text_column(["road"] * n), "attr.subclass": mg.text_column([""] * n),
           "attr.name": mg.text_column([t.get("name") for _w, t, _l in picked]),
           "attr.src_dataset": mg.text_column(["OpenStreetMap"] * n),
           "attr.src_record": mg.text_column([f"w{wid}@1" for wid, _t, _l in picked]),
           "attr.cx": cx, "attr.cy": cy,
           "provenance": np.array(json.dumps({"id": "overture:test", "datasets": []}))}
    return out


class Week:
    """A weekly build of the tag shards (built, its manifest and parts under BASE/<built>/...)."""

    def __init__(self, tmp, built, rows):
        self.built = built
        d = os.path.join(tmp, built)
        os.makedirs(d)
        parquet = os.path.join(d, "highways.parquet")
        ts.write_parquet(parquet, rows)
        out = os.path.join(d, "out")
        import build_osm_tag_shards as bt
        m = bt.build(parquet, out, workers=1, tmp=os.path.join(d, "work"), log=lambda s: None, root_max=40, z=13,
                     part_bytes=0)
        self.files = {}
        m = dict(m, built=built, archive=f"{built}/osmtags.pmtiles")
        with open(os.path.join(out, "osmtags.pmtiles"), "rb") as fh:
            self.files[f"{BASE}/{built}/osmtags.pmtiles"] = fh.read()
        self.manifest = json.dumps(m).encode()


@unittest.skipIf(ts.duckdb is None, "DuckDB not installed")
class JoinAtDecodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="osmjoin-test-")
        cls.w1 = Week(cls.tmp, "W1", week_rows("3"))
        cls.w2 = Week(cls.tmp, "W2", week_rows("5"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="osmjoin-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.enterContext(mock.patch.object(osmtagshards, "BASE", BASE))
        self.enterContext(mock.patch.object(osmtagshards, "ON", True))
        self.enterContext(mock.patch.object(mapdata, "MIRROR_MB", 0))
        self.files = {**self.w1.files, **self.w2.files}
        self.addCleanup(self.end_run)
        self.files_by_cell = {k: roads_file(k, week_rows("3")) for k in (CELL_A, CELL_B)}

    def end_run(self):
        os.environ.pop(pagesdata.SESSION_ENV, None)
        pagesdata.session_reset()

    def run_on(self, week, down=False):
        """A server run whose tag shards are week (pinned on disk at its start, a session of its own): its MapData,
        the layer files read as written here (Overture's alone)."""
        self.end_run()
        d = os.path.join(self.cache, "pagesdata", "osmtags")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "wb") as fh:
            fh.write(week.manifest)
        pagesdata.start_session(self.cache)
        t = mf.Transport(dict(self.files))
        if down:
            t.fail["shards.test"] = 503
        md = mapdata.MapData(self.cache, mf.fetcher(t), mf.RELEASE)
        self.addCleanup(md.close)
        md.rail_tags = md.lane_tags = md.station_tags = None
        md.layer_arrays = lambda key, layer, kind="demand": (copy.copy(self.files_by_cell[mapdata._key(key)]), True)
        md.osmtags = mock.Mock(cell=mock.Mock(side_effect=OSError("no Layercake here")),
                               highway_geometry=lambda *a, **k: {})
        return md

    def lanes_of(self, f, wid=LONG):
        i = list(f.attrs["src_record"]).index(f"w{wid}@1")
        return float(f.attrs["osm_lanes"][i])

    def test_cells_of_different_weeks_join_the_runs_build(self):
        """The cell files hold no tags: whenever a cell was fetched, a run joins its own week's tags into every cell,
        so the copies of one way in two cells agree; a newer week re-keys every cell of the next run."""
        md = self.run_on(self.w1)
        self.assertEqual(md.osm_tags_build(), "W1")
        self.assertEqual(md.versions()["osmtags"], "W1")
        a1 = md.cell(CELL_A, ("roads",)).layers["roads"]
        self.assertEqual(self.lanes_of(a1), 3.0)
        self.assertEqual(a1.__dict__.get("_osm_built"), "W1")
        self.assertIsNone(a1.__dict__.get("_bnge_fallback"))
        self.assertIn('"osmtags": "W1"', a1.__dict__["_bnge_cell"][2])
        # a week later: cell A's file is the one fetched in week 1, cell B's is fetched now; both join W2
        md = self.run_on(self.w2)
        self.assertEqual(md.versions()["osmtags"], "W2")
        a2 = md.cell(CELL_A, ("roads",)).layers["roads"]
        b2 = md.cell(CELL_B, ("roads",)).layers["roads"]
        self.assertEqual(self.lanes_of(a2), 5.0)
        self.assertEqual(self.lanes_of(b2), 5.0, "one way, one week's tags in both cells")
        for col in ("osm_lanes", "osm_surface", "osm_width", "osm_maxspeed"):
            self.assertEqual(repr(a2.attrs[col][list(a2.attrs["src_record"]).index(f"w{LONG}@1")]),
                             repr(b2.attrs[col][list(b2.attrs["src_record"]).index(f"w{LONG}@1")]), col)
        self.assertEqual({a2.__dict__["_osm_built"], b2.__dict__["_osm_built"]}, {"W2"})

    def test_a_join_of_the_same_build_is_the_same(self):
        """Two runs on one build (and two processes' MapData) join byte-identical layers."""
        out = []
        for _ in range(2):
            md = self.run_on(self.w2)
            out.append(mapdata._layer_arrays(md.cell(CELL_A, ("roads",)).layers["roads"]))
        self.assertEqual(list(out[0]), list(out[1]))
        for k in out[0]:
            self.assertEqual(out[0][k].dtype, out[1][k].dtype, k)
            self.assertEqual(out[0][k].tobytes(), out[1][k].tobytes(), k)

    def test_the_decoded_roads_key_holds_the_build(self):
        """roadlayercache's key of a cell changes with the run's tag build (the same file, the same switches)."""
        with mock.patch.object(mapdata, "MIRROR_MB", 64):
            keys = []
            for week in (self.w1, self.w1, self.w2):
                md = self.run_on(week)
                os.makedirs(os.path.dirname(md.path(CELL_A, "roads")), exist_ok=True)
                p = md.path(CELL_A, "roads")
                if not os.path.exists(p):
                    md._store(p, self.files_by_cell[CELL_A])
                k, vers = md._dec_key(CELL_A, "roads", self.files_by_cell[CELL_A])
                self.assertEqual(vers["osm_tags"], week.built)
                keys.append(k)
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[1], keys[2])

    def test_shards_unreachable_make_a_stand_in(self):
        """The run's build known but its tile not to be had (the Layercake file failing too): the layer has no tags of
        the run's build, so it is a stand-in: nothing kept is made of it (a revisit fault in the taker), and it is
        decoded again later."""
        md = self.run_on(self.w2, down=True)
        self.assertEqual(md.osm_tags_build(), "W2", "the run's build: decided from the pin, not the network")
        before = revisit.faults()
        f = md.cell(CELL_A, ("roads",)).layers["roads"]
        self.assertIsNotNone(f.__dict__.get("_bnge_fallback"))
        self.assertEqual(f.__dict__.get("_osm_built"), "none")
        self.assertGreater(revisit.faults(), before)
        self.assertFalse(md.roads_complete(CELL_A, f))

    def test_the_layercake_file_stands_in_for_a_down_tile(self):
        md = self.run_on(self.w2, down=True)
        md.osmtags = mock.Mock(cell=mock.Mock(return_value=({LONG: {"lanes": 9}}, [])),
                               highway_geometry=lambda *a, **k: {})
        f = md.cell(CELL_A, ("roads",)).layers["roads"]
        self.assertEqual(f.__dict__.get("_osm_built"), "layercake")
        self.assertIsNotNone(f.__dict__.get("_bnge_fallback"), "another source than the run's build: a stand-in")

    def test_a_run_without_a_build_reads_the_layercake_file_keyed_so(self):
        self.end_run()
        pagesdata.start_session(self.cache)
        t = mf.Transport({})                    # (no manifest published: none for the run)
        md = mapdata.MapData(self.cache, mf.fetcher(t), mf.RELEASE)
        self.addCleanup(md.close)
        md.rail_tags = md.lane_tags = md.station_tags = None
        md.layer_arrays = lambda key, layer, kind="demand": (copy.copy(self.files_by_cell[mapdata._key(key)]), True)
        md.osmtags = mock.Mock(cell=mock.Mock(return_value=({LONG: {"lanes": 9}}, [])),
                               highway_geometry=lambda *a, **k: {})
        self.assertEqual(md.osm_tags_build(), "layercake")
        f = md.cell(CELL_A, ("roads",)).layers["roads"]
        self.assertIsNone(f.__dict__.get("_bnge_fallback"))
        self.assertEqual(md.versions()["osmtags"], "layercake")

    def test_decisions_slices_hold_the_tags(self):
        """decisions' slice of a cell: its OSM road tags' digest, the same for two builds whose tags of the cell are
        the same, another when they differ; None when the tile cannot be had."""
        import decisions
        md = self.run_on(self.w1)
        src = decisions.weekly_of(md)
        self.assertIsNone(src["rails"])
        self.assertIsNone(decisions.slice_digest(src, tuple(CELL_A), [], fetch=False), "not in memory yet")
        d1 = decisions.slice_digest(src, tuple(CELL_A), [])
        self.assertIsNotNone(d1)
        self.assertEqual(decisions.slice_digest(src, tuple(CELL_A), [], fetch=False), d1, "now in memory")
        far = mapcells.CellKey(14, CELL_A.x, CELL_A.y + 3)      # (no way of the changed one there)
        f1 = decisions.slice_digest(src, tuple(far), [])
        md = self.run_on(self.w2)
        src = decisions.weekly_of(md)
        self.assertNotEqual(decisions.slice_digest(src, tuple(CELL_A), []), d1, "its lanes changed")
        self.assertEqual(decisions.slice_digest(src, tuple(far), []), f1, "nothing changed there")
        shutil.rmtree(os.path.join(self.cache, "pagesdata", "osmtags", "W2"))     # (the tiles not on disk either)
        md = self.run_on(self.w2, down=True)
        self.assertIsNone(decisions.slice_digest(decisions.weekly_of(md), tuple(CELL_A), []))

    def test_versions_vkey_and_slices(self):
        """The build is a weekly key: out of the decisions' vkey (per item instead), and the slices' set in it."""
        import decisions
        v = {"pipeline": 9, "osmtags": "W1"}
        self.assertEqual(decisions.vkey(v), decisions.vkey(dict(v, osmtags="W2")))
        self.assertIn("osmtags", decisions.vkey_doc(v)["slices"])


if __name__ == "__main__":
    unittest.main()
