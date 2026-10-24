"""blockworker.roads_tile: a road tile built in a road process is the tile the server's own thread
builds (roads.build_tile + roadmesh + write_tile with the same inputs), files byte for byte."""
import os
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
import roadmesh  # noqa: E402
import roads  # noqa: E402
from test_roads import FRAME, LAT, LON, features  # noqa: E402


class FakeMapData:
    """The roads layer of the tile's first cell only (every cell the same object would double them)."""

    def __init__(self, feats, first):
        self.feats, self.first = feats, first

    def cell(self, key, layers=(), kind="demand"):
        return SimpleNamespace(layers={"roads": self.feats if key == self.first else None})


class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        return np.full(np.shape(lat), 10.0)


class RoadsTileTest(unittest.TestCase):
    def test_same_as_in_process(self):
        f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ["secondary", "residential"])
        keys = roads.cells_of_tile(FRAME, 0, 0)
        md = FakeMapData(f, keys[0])
        blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None, sea=None, lock=threading.Lock(),
                                         bridges={}, anchor=lambda aid, lat, lon: (None, None))
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            got = blockworker.roads_tile(1, LAT, LON, 15, 0, 0, a, "/temp/x", True, 0.05)
            cells = [md.cell(k).layers.get("roads") for k in keys]
            elev = lambda la, lo: FakeDem().sample(la, lo, 15)      # noqa: E731
            walks = []
            bridges = roads.BridgeProfiles(FRAME, lambda k: md.cell(k).layers.get("roads"), elev)
            chunks = roads.build_tile(FRAME, 0, 0, cells, elev, None, walks=walks, bridges=bridges)
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
                                         lock=threading.Lock(), bridges={}, anchor=lambda aid, lat, lon: (None, None))
        rules = {"rightHandDrive": False, "mph": True}
        with tempfile.TemporaryDirectory() as out:
            path, n_nodes, n_edges = blockworker.navgraph(1, LAT, LON, 15, 0.0, 0.0, out, "/temp/x", 7, False, True,
                                                          rules)
            self.assertEqual(path, "/temp/x/navgraph_1_7.json")
            got = json.loads(Path(out, "navgraph_1_7.json").read_text())
        elev = lambda la_, lo_: FakeDem().sample(la_, lo_, 15)      # noqa: E731
        want = navgraph.build(FRAME, [md.cell(k).layers.get("roads") for k in keys], elev, (0.0, 0.0), None,
                              decks={}, widths=None, left_hand=False, mph=True)
        want["rules"] = rules
        self.assertEqual(got, json.loads(json.dumps(want, separators=(",", ":"))))
        self.assertGreater(n_nodes, 2)
        self.assertEqual((n_nodes, n_edges), (len(want["nodes"]), len(want["edges"])))


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
            blockworker._s = SimpleNamespace(dem=FakeDem(), airports=None, sea=None, cache_dir=cache, imagery=None,
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
        from PIL import Image
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
            Image.fromarray(rgb, "RGB").save(os.path.join(out, "server.png"), format="PNG", compress_level=0)   # (as earthd.write_base)
            got = blockworker.base_photo(*args)
            self.assertIsNotNone(got, "the tiles on disk now")
            np.testing.assert_array_equal(got[0], rgb)
            self.assertEqual(set(got[1]), set(used))
            self.assertEqual(got[2], set())
            self.assertEqual(Path(out, "worker.png").read_bytes(), Path(out, "server.png").read_bytes())
            self.assertTrue(os.path.exists(os.path.join(terraincache.cache_dir(user), terraincache.base_key(
                "/temp/x/worker.png", "/levels/x/ao.png") + ".dds")))


class BaseRoutingTest(unittest.TestCase):
    """earthd: the base photo goes to the other lane's block process when the server has no imagery analyses; with
    them (or a tile missing on disk) it is rendered in the server (write_base)."""

    def test_routing_and_fallback(self):
        import earthd
        S = earthd.Server
        s = SimpleNamespace(block_pools=[("request", None), ("prefetch", None)], imagery=object(), deshadow=None,
                            colormatch=None)
        self.assertEqual(S._base_process(s, earthd.LANE_REQUEST), "prefetch")
        self.assertEqual(S._base_process(s, earthd.LANE_PREFETCH), "request")
        for k in ("deshadow", "colormatch"):
            self.assertIsNone(S._base_process(SimpleNamespace(**{**vars(s), k: object()}), earthd.LANE_REQUEST), k)
        self.assertIsNone(S._base_process(SimpleNamespace(**{**vars(s), "block_pools": []}), earthd.LANE_REQUEST))
        calls = []

        def write_base(anchor, cx, cy, photo=None, quick=False):
            calls.append((cx, cy))
            photo.append("here")
            return "here.png"
        s.write_base = write_base
        s._base_elsewhere = lambda anchor, cx, cy, pool, photo: None         # a tile not on disk
        name, photo, _ = S._base_photo(s, None, 1, 2, "pool")
        self.assertEqual((name, photo, calls), ("here.png", ["here"], [(1, 2)]))
        s._base_elsewhere = lambda anchor, cx, cy, pool, photo: photo.append("there") or "there.png"
        name, photo, _ = S._base_photo(s, None, 3, 4, "pool")
        self.assertEqual((name, photo, calls), ("there.png", ["there"], [(1, 2)]))


if __name__ == "__main__":
    unittest.main()
