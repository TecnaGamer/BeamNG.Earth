"""blockworker.roads_tile: a road tile built in a road process is the tile the server's own thread
builds (roads.build_tile + roadmesh + write_tile with the same inputs), files byte for byte."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import blockworker  # noqa: E402
import borderfixtures  # noqa: E402
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, LAT, LON, features  # noqa: E402


def setUpModule():
    borderfixtures.require()        # (no border data: the German read gates' tests would pass on today's reads)


class FakeMapData:
    """The roads layer of the tile's first cell only (every cell the same object would double them)."""

    def __init__(self, feats, first):
        self.feats, self.first = feats, first

    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={"roads": self.feats if key == self.first else None})

    def warm(self, needs, kind="demand"):
        return 0

    def versions(self):             # (part of the revisit cache's key)
        return {"pipeline": 0}

    def side_prefetch(self, keys):  # (mapdata.MapData's: the props' per-cell sources started early)
        return None


def tmp_cache(test):
    """A revisit cache folder of the test's own (the tiles' keep-for-the-next-visit cache), removed after it."""
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    return d.name


class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        return np.full(np.shape(lat), 10.0)


class RoadsTileTest(unittest.TestCase):
    def test_same_as_in_process(self):
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        keys = roads.cells_of_tile(FRAME, 0, 0)
        md = FakeMapData(f, keys[0])
        blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None, lock=threading.Lock(),
                                         cache_dir=tmp_cache(self),
                                         bridges={}, anchor=lambda aid, lat, lon: (None, None))
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            got = blockworker.roads_tile(1, LAT, LON, 15, 0, 0, a, "/temp/x", True, 0.05)
            cells = [md.cell(k).layers.get("roads") for k in keys]
            # the server's ground (blockworker.ground: the land off the roads roughened), not the bare DEM
            elev = lambda la, lo: blockworker.ground(FRAME, la, lo, FakeDem().sample(la, lo, 15))   # noqa: E731
            walks = []
            bridges = roads.BridgeProfiles(FRAME, lambda k: md.cell(k).layers.get("roads"), elev)
            import roadwear       # (the worker's default road wear)
            chunks = roads.build_tile(FRAME, 0, 0, cells, elev, None, walks=walks, bridges=bridges,
                                      wear=roadwear.tier_of(None))
            x0, y0, _, _ = roads.tile_bbox(0, 0)
            want = roads.write_tile(b, "/temp/x", 1, 0, 0, chunks, walks,
                                    roadmesh.build(FRAME, chunks, elev, (x0, y0, 0.0), 0.05, clip=roads.tile_bbox(0, 0),
                                                   follow=True),
                                    roadmesh.deck(chunks, (x0, y0, 0.0)))
            self.assertEqual(got[:2], want)
            self.assertEqual(got[2], [], "no gas stations' pads (gas_stations off)")
            self.assertGreater(got[1], 0)
            self.assertEqual(sorted(os.listdir(a)), sorted(os.listdir(b)))
            for name in os.listdir(a):
                self.assertEqual(Path(a, name).read_bytes(), Path(b, name).read_bytes(), name)



class GermanRoutesTest(unittest.TestCase):
    """The cells' road_routes for the German junctions' ref ranks (blockworker._de_routes) reach roads.build_tile and
    navgraph.build the same in a road process and in the server's own thread (earthd.Server's in-process paths:
    --road-processes 0, --block-process off): behind the same gate (a tile near Germany: designs.near; the graph's
    window touching it: designs.reads_here) and over the same cells (the tile's, 200 m round it with the regional pole
    kit on), so a pool tile is the in-process tile there too (a tertiary Kreisstraße a stretch in both)."""
    MUNICH, SALZBURG, BALTIMORE = (48.137, 11.575), (47.800, 13.045), (39.2834, -76.5934)

    def server(self, at, out, on, poles):
        """earthd.Server's in-process stand-in for an anchor at at (test_highway_signs' server())."""
        import earthd
        frame = blockworker.geodesy.EnuFrame(at[0], at[1])
        anchor = SimpleNamespace(id=1, lat=at[0], lon=at[1], frame=frame, zooms=[15], bridges=None, tunnels=None)
        ns = SimpleNamespace(
            mapdata=FakeMapData(None, None), anchors={1: anchor}, road_pool=None, far_bld_pool=None, sign_pool=None,
            gas_stations=False, surface=None, grader_for=lambda a: None, ground=lambda f, la, lo, z: z, dem=FakeDem(),
            grader_lock=threading.Lock(), sea=None, buildings=False, road_widths=None, left_hand=lambda a: False,
            mph=lambda a: False, anchor_q=lambda a: None, buried=lambda a: 0.0, country=lambda a: "DE",
            lamp_region=lambda a: None, keep_fuel_pads=lambda pads: None, airports=None, airports_db=None,
            near_visual="detail", out_dir=out, vfs_dir="/temp/x", sidewalks=False, _tunnel_ground=lambda a, g: None,
            dej_on=on, regional_poles=poles)
        ns.chain_view = lambda *a, **kw: earthd.Server.chain_view(ns, *a, **kw)
        return ns

    def routes(self, kind, at, on=True, poles="off"):
        """(the road process's de_routes, the server thread's) that roads.build_tile (kind "tile": tile (0, 0)) or
        navgraph.build ("graph": round (0, 0)) gets for an anchor at at; _de_routes stands in as [its cells]."""
        import earthd
        import navgraph
        import secrossings
        from unittest import mock
        got = []

        def record(*a, **k):
            got.append(k.get("de_routes", ()))
            return {"nodes": {}, "edges": []}
        blockworker._s = SimpleNamespace(md=FakeMapData(None, None), dem=FakeDem(), widths=None, surface=None,
                                         airports=None, sea=None, lock=threading.Lock(), cache_dir=tmp_cache(self),
                                         bridges={}, anchor=lambda aid, lat, lon: (None, None))
        with mock.patch.object(blockworker, "_de_routes", lambda md, keys: [tuple(sorted(keys))]), \
                mock.patch.object(secrossings, "read", lambda md, frame, keys: None), \
                tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ns = self.server(at, b, on, poles)
            if kind == "tile":
                with mock.patch.object(roads, "build_tile",
                                       side_effect=lambda *p, **k: got.append(k.get("de_routes", ())) or []):
                    blockworker.roads_tile(1, at[0], at[1], 15, 0, 0, a, "/temp/x", False, 0.0, country="DE",
                                           mph=False, pole_profile=earthd.Server.pole_profile(ns), de_junctions=on)
                    earthd.Server.build_roads_tile(ns, 1, 0, 0)
            else:
                with mock.patch.object(navgraph, "build", record), mock.patch.object(navgraph, "write",
                                                                                     lambda *p: "/temp/x/g.json"):
                    blockworker.navgraph(1, at[0], at[1], 15, 0.0, 0.0, a, "/temp/x", 7, False, False,
                                         {"country": "DE", "de_junctions": on})
                    earthd.Server.build_navgraph(ns, 1, 0.0, 0.0, 7)
        self.assertEqual(len(got), 2, kind)
        return [list(g) for g in got]

    def test_tile_routes_in_process_as_in_a_road_process(self):
        for at, on, poles, near in ((self.MUNICH, True, "off", True), (self.MUNICH, True, "europe", True),
                                    (self.MUNICH, False, "off", False), (self.SALZBURG, True, "off", True),
                                    (self.BALTIMORE, True, "off", False)):
            pool, here = self.routes("tile", at, on, poles)
            self.assertEqual(here, pool, (at, on, poles))
            self.assertEqual(bool(pool), near, (at, on, poles))
        wide = self.routes("tile", self.MUNICH, poles="europe")[0][0]
        self.assertGreater(len(wide), len(self.routes("tile", self.MUNICH)[0][0]), "the kit's 200 m: more cells")

    def test_graph_routes_in_process_as_in_a_road_process(self):
        for at, on, want in ((self.MUNICH, True, True), (self.MUNICH, False, False), (self.SALZBURG, True, False)):
            pool, here = self.routes("graph", at, on)
            self.assertEqual(here, pool, (at, on))
            self.assertEqual(bool(pool), want, (at, on))


class NavgraphTest(unittest.TestCase):
    """blockworker.navgraph: the graph built in a road process is the one navgraph.build makes with the same inputs
    (the server's way), its file written and the rules passed through."""

    def test_same_graph(self):
        import json
        import mapcells
        import navgraph
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        w = navgraph.WINDOW_M + 200.0
        la, lo, _ = FRAME.enu_to_geodetic(np.array([-w, w]), np.array([-w, w]), 0.0)
        keys = mapcells.cells_for_bbox(float(la.min()), float(lo.min()), float(la.max()), float(lo.max()))
        md = FakeMapData(f, keys[0])
        blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None,
                                         lock=threading.Lock(), bridges={}, anchor=lambda aid, lat, lon: (None, None),
                                         cache_dir=tmp_cache(self))
        rules = {"rightHandDrive": False, "mph": True}
        with tempfile.TemporaryDirectory() as out:
            path, n_nodes, n_edges = blockworker.navgraph(1, LAT, LON, 15, 0.0, 0.0, out, "/temp/x", 7, False, True,
                                                          rules)
            self.assertEqual(path, "/temp/x/navgraph_1_7.json")
            got = json.loads(Path(out, "navgraph_1_7.json").read_text())
        elev = lambda la_, lo_: blockworker.ground(FRAME, la_, lo_, FakeDem().sample(la_, lo_, 15))   # noqa: E731
        want = navgraph.build(FRAME, [md.cell(k).layers.get("roads") for k in keys], elev, (0.0, 0.0), None,
                              decks={}, widths=None, left_hand=False, mph=True)
        want["rules"] = rules
        self.assertEqual(got, json.loads(json.dumps(navgraph.short_ids(want), separators=(",", ":"))))
        self.assertGreater(n_nodes, 2)
        self.assertEqual((n_nodes, n_edges), (len(want["nodes"]), len(want["edges"])))


class GermanReadGateTest(unittest.TestCase):
    """The German read gates in a road process: Overture's nodes (secrossings.read) for a road tile or a
    road graph when the German junction signs are on and the box it decides touches Germany (the tile's own ± its
    junctions' reach, roads.NEAR_M; the graph's window): one of its corners or its centre inside (or a point of
    designs.GRID_M's grid). Off: Germany's border data untouched, no read; outside Germany: no read."""
    MUNICH, SALZBURG = (48.137, 11.575), (47.800, 13.045)

    def reads(self, kind, at, on=True, west_of=None):
        """Whether the road tile (0, 0) or the road graph round (0, 0) of an anchor at (lat, lon) at reads the nodes;
        west_of: Germany patched to the half-plane west of this frame-local x (m)."""
        import contextlib
        import navgraph
        import secrossings
        import germany
        from unittest import mock
        calls = []
        blockworker._s = SimpleNamespace(md=FakeMapData(None, None), dem=FakeDem(), widths=None, surface=None,
                                         airports=None, sea=None, lock=threading.Lock(), cache_dir=tmp_cache(self),
                                         bridges={}, anchor=lambda aid, lat, lon: (None, None))
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(secrossings, "read", lambda md, frame, keys: calls.append(keys)))
            if west_of is not None:
                frame = blockworker.geodesy.EnuFrame(at[0], at[1])
                cut = float(frame.enu_to_geodetic(np.array(float(west_of)), np.array(0.0), 0.0)[1])
                st.enter_context(mock.patch.object(germany, "contains",
                                                   lambda lon, lat: np.atleast_1d(np.asarray(lon, float) <= cut)))
            elif not on:            # (off: the border data is not to be asked at all)
                import countrybounds

                def untouched(*a, **k):
                    raise AssertionError("Germany's border data asked with the setting off")
                st.enter_context(mock.patch.object(germany, "contains", untouched))
                st.enter_context(mock.patch.object(countrybounds, "bundled", untouched))
            out = st.enter_context(tempfile.TemporaryDirectory())
            if kind == "tile":
                blockworker.roads_tile(1, at[0], at[1], 15, 0, 0, out, "/temp/x", True, 0.05, country="DE",
                                       de_junctions=on)
            else:
                st.enter_context(mock.patch.object(navgraph, "build", lambda *a, **k: {"nodes": {}, "edges": []}))
                blockworker.navgraph(1, at[0], at[1], 15, 0.0, 0.0, out, "/temp/x", 7, False, True,
                                     {"country": "DE", "de_junctions": on})
        return bool(calls)

    def test_off_reads_nothing(self):
        for kind in ("tile", "graph"):
            self.assertFalse(self.reads(kind, self.MUNICH, on=False), kind)

    def test_in_germany(self):
        for kind in ("tile", "graph"):
            self.assertTrue(self.reads(kind, self.MUNICH), kind)

    def test_outside_germany(self):
        for kind in ("tile", "graph"):
            self.assertFalse(self.reads(kind, self.SALZBURG), kind)

    def test_a_tile_across_the_border(self):
        """Tile (0, 0) is x 0 .. 256: its centre outside, Germany 20 m west of it, within its junctions' reach
        (roads.NEAR_M, 40 m): read; 60 m west, past the reach: not."""
        self.assertTrue(self.reads("tile", self.MUNICH, west_of=-20.0))
        self.assertFalse(self.reads("tile", self.MUNICH, west_of=-60.0))

    def test_the_graph_gets_the_cells_routes_in_germany(self):
        """The road graph's German context has the cells' road_routes (designs.context's ref ranks, as the road
        tiles': blockworker._de_routes over its window's cells) where its window touches Germany; none off or
        outside."""
        import navgraph
        from unittest import mock
        layer = object()
        for at, on, want in ((self.MUNICH, True, [layer]), (self.MUNICH, False, ()), (self.SALZBURG, True, ())):
            got = {}
            blockworker._s = SimpleNamespace(md=FakeMapData(None, None), dem=FakeDem(), widths=None, surface=None,
                                             airports=None, sea=None, lock=threading.Lock(), cache_dir=tmp_cache(self),
                                             bridges={}, anchor=lambda aid, lat, lon: (None, None))
            with mock.patch.object(blockworker, "_de_routes", lambda md, keys: [layer] if keys else []), \
                    mock.patch.object(navgraph, "build", lambda *a, **k: got.update(k) or {"nodes": {}, "edges": []}), \
                    tempfile.TemporaryDirectory() as out:
                blockworker.navgraph(1, at[0], at[1], 15, 0.0, 0.0, out, "/temp/x", 7, False, True,
                                     {"country": "DE", "de_junctions": on})
            self.assertEqual(list(got.get("de_routes", ())), list(want), (at, on))
            self.assertIs(got["de_on"], on)

    def test_a_graph_window_across_the_border(self):
        """The graph's window is (0, 0) +- navgraph.WINDOW_M + 200 m: Germany 50 m inside its west edge: read; 50 m past
        it: not."""
        import navgraph
        w = navgraph.WINDOW_M + 200.0
        self.assertTrue(self.reads("graph", self.MUNICH, west_of=-(w - 50.0)))
        self.assertFalse(self.reads("graph", self.MUNICH, west_of=-(w + 50.0)))


class SkinInputsTest(unittest.TestCase):
    """A geotile's skin inputs kept in memory hold only while its session file is the one they came from: a content
    restored from the revisit cache (the German junction signs switched back), by this road process or another,
    replaces the file, and the skin reads the restored inputs, not the other setting's."""

    def test_a_replaced_session_file_is_read_again(self):
        import pickle
        out = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        key = "test_skin_inputs"
        p = os.path.join(out, blockworker.geo_names(key)["inputs"])
        self.addCleanup(lambda: blockworker._geo_inputs.pop(key, None))
        built = []
        blockworker._write_bytes(p, pickle.dumps({"h": "us"}))
        self.assertEqual(blockworker._skin_inputs(key, out, lambda: built.append(1))["h"], "us")
        self.assertEqual(blockworker._skin_inputs(key, out, lambda: built.append(1))["h"], "us", "kept")
        blockworker._write_bytes(p, pickle.dumps({"h": "de"}))    # (restored: the other setting's, as large)
        self.assertEqual(blockworker._skin_inputs(key, out, lambda: built.append(1))["h"], "de")
        os.remove(p)
        self.assertIsNone(blockworker._skin_inputs(key, out, lambda: built.append(1)), "gone: built again")
        self.assertEqual(built, [1])


class GermanCacheKeyTest(unittest.TestCase):
    """The revisit keys of a road tile and of a geotile's content with the German junction signs: off, or on away
    from germany.BOX, the key's parts exactly as before (the setting's bool last; the border data not asked);
    on near Germany, also germany.signature(), so a tile built without the border data is built again with it."""
    MUNICH, BALTIMORE = (48.137, 11.575), (39.2834, -76.5934)

    def setUp(self):
        blockworker._s = SimpleNamespace(md=None, cache_dir=tmp_cache(self))

    def parts(self, kind, at, on):
        """The parts revisit.key is given for the road tile (0, 0) of an anchor at at, or the geotile under at."""
        import revisit
        from unittest import mock
        got = []
        with mock.patch.object(revisit, "key", lambda *p: got.append(p) or "k"), \
                mock.patch.object(blockworker, "_revisit_tile", lambda *a, **k: None), \
                tempfile.TemporaryDirectory() as out:
            if kind == "tile":
                blockworker._roads_tile_cached(1, at[0], at[1], 15, 0, 0, out, "/temp/x", True, 0.05, False, False,
                                               True, None, False, 0.0, None, "DE", None, de_junctions=on)
            else:
                import geotiles
                blockworker._geo_content_key(geotiles.key(*geotiles.tile_of(*at)), 15, True, False, True, False, 0.0,
                                             None, "DE", models_dir=out, de_junctions=on)
        self.assertEqual(len(got), 1)
        return got[0]

    def test_off_and_away_as_before(self):
        import countrybounds
        import regionaldata
        from unittest import mock

        def refused(*a, **k):
            raise AssertionError("the border data asked")
        for kind in ("tile", "geo"):
            with mock.patch.object(countrybounds, "bundled", refused), \
                    mock.patch.object(countrybounds, "signature", refused), \
                    mock.patch.object(regionaldata, "manifest", refused):
                off = self.parts(kind, self.MUNICH, False)
                away = self.parts(kind, self.BALTIMORE, True)
                away_off = self.parts(kind, self.BALTIMORE, False)
            self.assertIs(off[-1], False, kind)
            self.assertIs(away[-1], True, kind)
            self.assertEqual(away[:-1], away_off[:-1], kind)
            self.assertEqual(len(away), len(off), kind)

    def test_near_germany_names_the_border_data(self):
        import countrybounds
        import germany
        from unittest import mock
        for kind in ("tile", "geo"):
            off, on = self.parts(kind, self.MUNICH, False), self.parts(kind, self.MUNICH, True)
            self.assertEqual(on[:-2], off[:-1], kind)
            self.assertEqual(on[-2:], (True, germany.signature()), kind)
            self.assertFalse(on[-1].endswith(germany.ABSENT), kind)
            with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
                absent = self.parts(kind, self.MUNICH, True)
            self.assertEqual(absent[-1], on[-1] + germany.ABSENT, kind)


class NoiseFetcher:
    """Every imagery source tile a seeded noise JPEG."""

    def register_chain(self, *a):
        pass

    def miss_alive(self, miss, url):
        return False

    def get(self, url, kind="demand"):
        import io
        import zlib
        from PIL import Image
        rng = np.random.default_rng(zlib.crc32(url.encode()))
        buf = io.BytesIO()
        Image.fromarray(rng.integers(20, 220, (256, 256, 3), dtype=np.uint8)).save(buf, "JPEG")
        return SimpleNamespace(body=buf.getvalue(), elapsed=0.01)


class FarTileTest(unittest.TestCase):
    """blockworker.far_tile: the texture from the imagery on disk is the one the server renders, byte for byte; with
    a tile not on disk, none (the server renders it)."""

    def test_texture_from_disk(self):
        import farfield
        import imagery
        import rockface

        Fetcher = NoiseFetcher
        layout = farfield.Layout(cells=64)
        tile = farfield.Tile(3, 1, -2)
        px = 128
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as out:
            blockworker._s = SimpleNamespace(dem=FakeDem(), md=None, widths=None, surface=None, airports=None, sea=None,
                                             cache_dir=cache, imagery=None,
                                             anchor=lambda aid, lat, lon: (None, None))
            tex = {"path": os.path.join(out, "worker.dds"), "px": px, "chain": ["usgs"], "tone": 1.0,
                   "leaf_on": True, "season_mode": "off"}
            built, got = blockworker.far_tile(1, LAT, LON, tile, 12, None, 11, True, layout, tex)
            self.assertIsNone(got, "nothing on disk: the server renders it")
            im = imagery.Imagery(os.path.join(cache, "imagery"), ("usgs",), fetcher=Fetcher())
            x0, y0, _, _ = tile.bbox
            sq = imagery.gutter_square(x0, y0, tile.size, px)
            rgb, used = im.render(FRAME, *sq, px)
            g = np.asarray(built.grid)
            rgb = rockface.rock(rgb, FRAME, *sq, g, x0, y0, tile.size / (g.shape[1] - 1))
            n = imagery.write_dds(os.path.join(out, "server.dds"), rgb)
            built2, got = blockworker.far_tile(1, LAT, LON, tile, 12, None, 11, True, layout, tex)
            self.assertIsNotNone(got, "the tiles on disk now")
            self.assertEqual(got[0], n)
            self.assertEqual(set(got[1]), set(used))
            self.assertEqual(Path(out, "worker.dds").read_bytes(), Path(out, "server.dds").read_bytes())


class BasePhotoTest(unittest.TestCase):
    """blockworker.base_photo: the block's base photo from the imagery on disk is the one earthd.write_base renders
    (render, rock faces, PNG), and its packed copy is in the game's cache; with a tile not on disk, none."""

    def test_photo_from_disk(self):
        import heightmap
        import imagery
        import rockface
        import terraincache
        x0, y0, size, px = -512.0, -512.0, 1024.0, 256
        with tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as out, \
                tempfile.TemporaryDirectory() as user:
            blockworker._s = SimpleNamespace(dem=FakeDem(), md=None, cache_dir=cache, imagery=None)
            tex = {"chain": ["usgs"], "tone": 1.0, "leaf_on": True, "season_mode": "off"}
            args = (LAT, LON, x0, y0, size, px, tex, True, 15, os.path.join(out, "worker.png"),
                    (user, "/temp/x/worker.png", "/levels/x/ao.png", 251))
            self.assertIsNone(blockworker.base_photo(*args), "nothing on disk: the server renders it")
            im = imagery.Imagery(os.path.join(cache, "imagery"), ("usgs",), fetcher=NoiseFetcher())
            rgb, used = im.render(FRAME, x0, y0, size, px, set())
            n = 65
            xs = x0 + np.arange(n) * (size / (n - 1))
            ys = y0 + np.arange(n) * (size / (n - 1))
            _, U = heightmap.surface(FRAME, lambda la, lo: FakeDem().sample(la, lo, 15), xs, ys)
            rgb = rockface.rock(np.asarray(rgb, np.uint8), FRAME, x0, y0, size, U, x0, y0, size / (n - 1))
            Path(out, "server.png").write_bytes(terraincache.stored_photo(rgb))   # (as earthd.write_base)
            got = blockworker.base_photo(*args)
            self.assertIsNotNone(got, "the tiles on disk now")
            np.testing.assert_array_equal(got[0], rgb)
            self.assertEqual(set(got[1]), set(used))
            self.assertEqual(got[2], set())
            self.assertEqual(Path(out, "worker.png").read_bytes(), Path(out, "server.png").read_bytes())
            self.assertTrue(os.path.exists(os.path.join(terraincache.cache_dir(user), terraincache.base_key(
                "/temp/x/worker.png", "/levels/x/ao.png") + ".dds")))


class BaseRoutingTest(unittest.TestCase):
    """earthd: the base photo goes to the other lane's block process when the server has no imagery analyses, or the
    player waits for its block (URGENT_ELSEWHERE); else (or a tile missing on disk) it is rendered in the server."""

    def test_routing_and_fallback(self):
        import earthd
        S = earthd.Server
        s = SimpleNamespace(block_pools=[("request", None), ("prefetch", None)], imagery=object(), deshadow=None,
                            colormatch=None)
        self.assertEqual(S._base_process(s, earthd.LANE_REQUEST), "prefetch")
        self.assertEqual(S._base_process(s, earthd.LANE_PREFETCH), "request")
        for k in ("deshadow", "colormatch"):
            # (with the analyses on: there too, from this process's finished tiles; _base_elsewhere)
            self.assertEqual(S._base_process(SimpleNamespace(**{**vars(s), k: object()}), earthd.LANE_PREFETCH),
                             "request", k)
            self.assertEqual(S._base_process(SimpleNamespace(**{**vars(s), k: object()}), earthd.LANE_REQUEST),
                             "prefetch", k)
        self.assertIsNone(S._base_process(SimpleNamespace(**{**vars(s), "block_pools": []}), earthd.LANE_REQUEST))
        # the photo process (PHOTO_PROCESS_MIN_CPUS): a requested block's photo there below FAST_BASE_V; faster, it
        # makes the stand-in first and the photo goes to the other lane as before
        # (a prefetched block's photo there too, at any speed: in the request lane's process it held the next block the
        # game asked for)
        p = SimpleNamespace(**{**vars(s), "photo_pool": "photo", "fast": False})
        self.assertEqual((S._base_process(p, earthd.LANE_REQUEST), S._base_process(p, earthd.LANE_PREFETCH)),
                         ("photo", "photo"))
        p.fast = True
        self.assertEqual((S._base_process(p, earthd.LANE_REQUEST), S._base_process(p, earthd.LANE_PREFETCH)),
                         ("prefetch", "photo"))
        calls = []

        def write_base(anchor, cx, cy, photo=None, quick=False):
            calls.append((cx, cy))
            photo.append("here")
            return "here.png"
        s.write_base = write_base
        s._base_elsewhere = lambda anchor, cx, cy, pool, photo, plain=False: None         # a tile not on disk
        name, photo, _ = S._base_photo(s, None, 1, 2, "pool")
        self.assertEqual((name, photo, calls), ("here.png", ["here"], [(1, 2)]))
        s._base_elsewhere = lambda anchor, cx, cy, pool, photo, plain=False: photo.append("there") or "there.png"
        name, photo, _ = S._base_photo(s, None, 3, 4, "pool")
        self.assertEqual((name, photo, calls), ("there.png", ["there"], [(1, 2)]))

    def test_analyses_on_finished_first(self):
        """With the analyses on: the photo from the finished tiles there (tex fin, not stale); not all finished: a
        block the player waits for (plain) without them there (refreshed later), any other here (None)."""
        import earthd
        S = earthd.Server
        im = SimpleNamespace(chain=[SimpleNamespace(name="eox")], _fin_sigs={}, _fin_sig=lambda src: "sig1")
        tries = []

        def base_in(anchor, cx, cy, pool, photo, fin, rev=0):
            tries.append(fin)
            return None if fin and fail[0] else "p.png"
        s = SimpleNamespace(imagery=im, deshadow=object(), colormatch=None, _base_in=base_in)
        fail = [False]
        self.assertEqual(S._base_elsewhere(s, None, 1, 2, "pool", []), "p.png")
        self.assertEqual(tries, [{"eox": "sig1"}])
        fail[0] = True
        tries.clear()
        self.assertIsNone(S._base_elsewhere(s, None, 1, 2, "pool", []))
        self.assertEqual(tries, [{"eox": "sig1"}])
        tries.clear()
        self.assertEqual(S._base_elsewhere(s, None, 1, 2, "pool", [], True), "p.png")
        self.assertEqual(tries, [{"eox": "sig1"}, None])


class AnchorCacheTest(unittest.TestCase):
    """_State.anchor keeps the last ANCHORS_KEPT anchors, never evicting the one just asked for (a late job of an
    older anchor after two newer ones raised KeyError: a BeamMP player's log, 2026-11-10)."""

    def state(self):
        st = blockworker._State.__new__(blockworker._State)
        st.lock = threading.Lock()
        st.chunks, st.graders, st.grading = {}, {}, False
        return st

    def test_newest_kept(self):
        st = self.state()
        for aid in (1, 2, 3):
            st.anchor(aid, 0.0, 0.0)
        self.assertEqual(sorted(st.chunks), [2, 3])

    def test_late_older_anchor(self):
        st = self.state()
        st.anchor(2, 0.0, 0.0)
        st.anchor(3, 0.0, 0.0)
        grader, chunks = st.anchor(1, 0.0, 0.0)      # (raised KeyError: 1)
        self.assertIsNone(grader)
        self.assertIs(chunks, st.chunks[1])
        st.anchor(4, 0.0, 0.0)
        self.assertEqual(sorted(st.chunks), [3, 4])


if __name__ == "__main__":
    unittest.main()
