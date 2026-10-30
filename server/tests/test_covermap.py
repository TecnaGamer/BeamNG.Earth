"""The map's cache view (covermap.py, native cachemap.rs): cache keys by place, the detail levels from coverage at
every grid zoom (children covering part of a cell, parents covering it whole), map data layers, the summary's
readiness, the world rows of the revisit sidecar (current vs built by an older version / settings), and the
query's checks. No network, no game."""
import base64
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import covermap  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import revisit  # noqa: E402

REL = "2026-09-23.1"


class FakeCache:
    """The parts of cache.Cache the view uses: its keys, lock and ready event."""

    def __init__(self, keys):
        self._lock = threading.Lock()
        self._entries = dict.fromkeys(keys)
        self._ready_ev = threading.Event()
        self._ready_ev.set()
        self.on_change = None


class FakeMapData:
    def __init__(self, root):
        self.cache_dir = root
        self.release = REL

    def path(self, key, layer):
        return mapdatamod.MapData.path(self, key, layer)


def levels(cm, z, x0, y0, x1, y1, mode):
    r = cm.query(z, x0, y0, x1, y1, mode)
    return list(base64.b64decode(r["cells"])), r


def layer_key(layer, x, y):
    lv = mapdatamod.LAYER_VERSIONS.get(layer)
    name = layer if lv is None else f"{layer}.{lv}"
    src = "worldcover/v200-2021" if layer == "landcover" else f"overture/{REL}"
    if layer == "canopy":
        src = "chm/v2-ml3"
    return f"mapdata/v{mapdatamod.VERSION}/{src}/{name}/14/{x}/{y}.npz"


class CoverMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        revisit.COMPACTED.set()

    def make(self, keys, mapdata=True, signature=None):
        cm = covermap.CoverMap(FakeCache(keys), self.tmp.name, FakeMapData(self.tmp.name) if mapdata else None,
                               signature)
        self.assertTrue(cm.build(wait=1.0))
        return cm

    def test_keys_by_class(self):
        cm = self.make(["dem/mapterhorn/15/100/200.webp", "dem/aws-terrarium/15/100/200.png",   # one tile, two sources
                        "dem/mapterhorn/16/1/2.404", "imagery/usgs/17/5/6.jpg", "imagery/eoxmap/10/1/1.jpg",
                        "imagery/usgs-ds1-full/16/1/1.png", "dem/decoded/v4/g1/12/1/1.npy", "mapdata/raw/x.blk",
                        layer_key("roads", 4700, 6250), "mapdata/v2/overture/2026-01-01.0/roads.7/14/1/1.npz",
                        "mapdata/v2/overture/" + REL + "/roads.6/14/1/1.npz", "gravity/egm.npz"])
        self.assertEqual(cm.idx.count(covermap.DEM), (1, 1), "the same tile from two sources counts once")
        self.assertEqual(cm.idx.count(covermap.IMG), (1, 0), "the map's own layers and derived folders are not imagery")
        roads = covermap.LAYER0 + mapdatamod.ALL_LAYERS.index("roads")
        self.assertEqual(cm.idx.count(roads), (1, 0), "only the current release and layer version")

    def test_detail_from_children_and_parents(self):
        """A z12 parent covers its z14 cell whole; z16 children over a quarter of it are a partial z16; over half,
        the cell's detail is z16. Coarser grids see the same tiles as parts of their cells."""
        keys = ["dem/mapterhorn/12/1000/1500.webp"]
        keys += [f"dem/mapterhorn/16/{16000 + i}/{24000 + j}.webp" for i in range(2) for j in range(4)]   # half of 4000/6000
        keys += [f"dem/mapterhorn/16/{16004 + i}/{24000 + j}.webp" for i in range(2) for j in range(2)]  # a quarter of 4001
        cm = self.make(keys, mapdata=False)
        cells, r = levels(cm, 14, 4000, 6000, 4002, 6000, "elevation")
        self.assertEqual((r["w"], r["h"]), (3, 1))
        self.assertEqual(cells, [16, 12, 12])
        # (the quarter-covered cell: its z12 parent covers it whole, so z12 is the detail met on half of it)
        cm2 = self.make(keys[1:], mapdata=False)
        cells, _ = levels(cm2, 14, 4000, 6000, 4002, 6000, "elevation")
        self.assertEqual(cells, [16, 16 | 0x80, 0])
        # a z16 grid: the z16 tiles themselves, the z12 parent under the rest
        cells, _ = levels(cm, 16, 16000, 24000, 16003, 24000, "elevation")
        self.assertEqual(cells, [16, 16, 12, 12])
        # a z12 grid: one cell, the parent itself
        cells, _ = levels(cm, 12, 1000, 1500, 1000, 1500, "elevation")
        self.assertEqual(cells, [12])

    def test_misses_count_for_the_summary_not_the_detail(self):
        """404 markers: the source has nothing finer there; the summary counts the cell as resolved, the elevation
        view shows only real data."""
        x, y = 4680, 6260                          # a z14 cell near Baltimore
        keys = [f"dem/mapterhorn/12/{x >> 2}/{y >> 2}.webp"]
        keys += [f"dem/mapterhorn/16/{(x << 2) + i}/{(y << 2) + j}.404" for i in range(4) for j in range(4)]
        keys += [f"imagery/usgs/16/{(x << 2) + i}/{(y << 2) + j}.jpg" for i in range(4) for j in range(4)]
        keys += [layer_key(n, x, y) for n in covermap.DRIVE_LAYERS]
        cm = self.make(keys)
        self.assertEqual(levels(cm, 14, x, y, x, y, "elevation")[0], [12])
        cm.idx.set_dem_caps([(x >> 8, y >> 8, 16)], 12)     # (lidar to z16 there)
        cm.caps_id = "fixed"
        self.assertEqual(levels(cm, 14, x, y, x, y, "summary")[0], [3], "z16 resolved (its misses), photos, map data")
        cm2 = self.make([k for k in keys if not k.endswith(".404")])
        cm2.idx.set_dem_caps([], 12)
        cm2.caps_id = "fixed"
        self.assertEqual(levels(cm2, 14, x, y, x, y, "summary")[0], [3], "no lidar there: z12 is ready")
        cm2.idx.set_dem_caps([(x >> 8, y >> 8, 16)], 12)
        self.assertEqual(levels(cm2, 14, x, y, x, y, "summary")[0], [2], "lidar there: z12 is only the far view")
        cm3 = self.make([k for k in keys if not k.startswith("mapdata")])
        self.assertEqual(levels(cm3, 14, x, y, x, y, "summary")[0], [2], "no map data: far view only")
        self.assertEqual(levels(self.make([]), 14, x, y, x, y, "summary")[0], [0])

    def test_map_data_layers(self):
        x, y = 4680, 6260
        some = [layer_key("buildings", x, y)]
        every = [layer_key(n, x + 1, y) for n in covermap.DRIVE_LAYERS]
        cm = self.make(some + every)
        self.assertEqual(levels(cm, 14, x, y, x + 2, y, "mapdata")[0], [1, 2, 0])
        self.assertEqual(levels(cm, 16, x << 2, y << 2, (x << 2) + 4, y << 2, "mapdata")[0], [1, 1, 1, 1, 2],
                         "a finer grid: the cell's layers in each of its children")
        z12 = levels(cm, 12, x >> 2, y >> 2, x >> 2, y >> 2, "mapdata")[0]
        self.assertEqual(z12, [1], "a coarser grid: every layer on less than half of it")

    def test_world_rows_current_and_old(self):
        """The revisit sidecar's rows: near (roads, plans) and far kinds, current when the code and every named
        setting match world_signature(), else built by an older version or settings."""
        d = self.tmp.name
        cur = {"sidewalks": True, "md": {"overture": REL}}
        wd = os.path.join(d, revisit.SUBDIR, revisit.WHERE_DIR)
        os.makedirs(wd)
        code = revisit.code_key()
        good, stale = revisit.sig(sidewalks=True, md={"overture": REL}), revisit.sig(sidewalks=False)
        rows = [(code, good, "roads", "k1", 39.30, -76.60), (code, stale, "roads", "k2", 39.30, -76.50),
                ("0" * 16, good, "far", "k3", 39.20, -76.60), (code, good, "far", "k4", 39.20, -76.50)]
        with open(os.path.join(wd, "1_1.tsv"), "w") as f:
            for c, s, kind, k, lat, lon in rows:
                f.write(f"{c}\t{s}\t{kind}\t{k}\t{lat}\t{lon}\t{lat:.6f}\t{lon:.6f}\t{lat + 0.002:.6f}\t{lon + 0.002:.6f}\n")
            f.write("half a line without its end")
        cm = self.make([], signature=lambda: cur)
        z = 14

        def cell(lat, lon):
            k = mapcells.cell_of(lat + 0.001, lon + 0.001, z)
            return levels(cm, z, k.x, k.y, k.x, k.y, "world")[0][0]
        self.assertEqual(cell(39.30, -76.60), 4, "near, current")
        self.assertEqual(cell(39.30, -76.50), 3, "near, older settings")
        self.assertEqual(cell(39.20, -76.60), 1, "far, older code")
        self.assertEqual(cell(39.20, -76.50), 2, "far, current")
        cur["sidewalks"] = False                       # the settings changed: the old row is the current one now
        self.assertEqual(cell(39.30, -76.60), 3)
        self.assertEqual(cell(39.30, -76.50), 4)
        # the line being written comes in once it is whole; a row seen before is not counted twice
        with open(os.path.join(wd, "1_1.tsv"), "a") as f:
            f.write("\n")
            f.write(f"{code}\t{good}\troads\tk1\t39.3\t-76.6\t39.3\t-76.6\t39.302\t-76.598\n")
        cm._sidecars(force=True)
        self.assertEqual(len(cm.seen), 4)

    def test_rect_tiles(self):
        import fast
        tiles = fast._ef.cover_rect_tiles(39.28, -76.62, 39.29, -76.61)
        self.assertTrue(1 <= len(tiles) <= 4)
        self.assertTrue(all(t[0] == 15 for t in tiles))
        self.assertEqual(fast._ef.cover_rect_tiles(1.0, 0.0, 0.0, 1.0), [], "an upside-down box")
        self.assertLessEqual(len(fast._ef.cover_rect_tiles(-60, -170, 60, 170)), 64)

    def test_query_checks(self):
        cm = self.make([], mapdata=False)
        for bad in ((5, 0, 0, 1, 1, "summary"), (17, 0, 0, 1, 1, "summary"), (14, 0, 0, 1, 1, "terrain"),
                    (14, 0, 0, 200, 200, "summary"), (14, 5, 5, 4, 5, "summary"), (14, "a", 0, 1, 1, "summary"),
                    (True, 0, 0, 1, 1, "summary")):
            with self.assertRaises(ValueError, msg=bad):
                cm.query(*bad)
        r = cm.query(6, -10, -10, 3, 70, "summary")
        self.assertEqual((r["x0"], r["y0"], r["w"], r["h"]), (0, 0, 4, 64), "clipped to the world")
        self.assertEqual(len(base64.b64decode(r["cells"])), 4 * 64)
        notyet = covermap.CoverMap(None, self.tmp.name)
        self.assertEqual(notyet.query(14, 0, 0, 1, 1)["ready"], False)

    def test_files_from_other_processes_are_found_in_the_view(self):
        """A worker process's DEM tile (no bookkeeping there): a close view finds it on disk, hands it to the cache
        (tracked from now on) and shows it; a far view does not look."""
        import cache as cachemod
        root = self.tmp.name
        c = cachemod.Cache(root, 0, start=False)
        c.scan()
        cm = covermap.CoverMap(c, root)
        self.assertTrue(cm.build(wait=1.0))
        p = os.path.join(root, "dem", "mapterhorn", "16", "18740", "25070.webp")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"x" * 100)
        with open(p + ".123.456.tmp", "wb") as f:     # (being written: left alone)
            f.write(b"x")
        self.assertEqual(levels(cm, 11, 585, 783, 585, 783, "elevation")[0], [0], "too far out to look")
        cells, _ = levels(cm, 14, 4685, 6267, 4685, 6267, "elevation")
        self.assertEqual(cells, [16 | 0x80])
        self.assertEqual(c.usage(), (100, 1))
        self.assertEqual(cm.freshen(14, 4685, 6267, 1, 1), 0, "the same view again: not before FRESHEN_S")
        c.close()

    def test_hook_queues_until_the_next_query(self):
        cache = FakeCache(["imagery/usgs/16/1/1.jpg"])
        cm = covermap.CoverMap(cache, self.tmp.name)
        cm.build(wait=1.0)
        with cache._lock:
            cache.on_change("imagery/usgs/16/1/2.jpg", True)
            cache.on_change("imagery/usgs/16/1/1.jpg", False)
            cache.on_change("imagery/usgs/16/1/3.jpg", True)
        self.assertEqual(cm.idx.count(covermap.IMG), (1, 0), "queued, not applied in the hook")
        g = cm.gen
        cm.query(14, 0, 0, 1, 1)
        self.assertEqual(cm.idx.count(covermap.IMG), (2, 0))
        self.assertGreater(cm.gen, g)
        cm.close()
        self.assertIsNone(cache.on_change)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_server  # noqa: E402


class CacheMapMessageTest(test_server.ServerHarness):
    """cache_map through a session: answered with cache_map_result (its id, the clipped box, base64 levels), an error
    for a bad ask, and one without a cache view."""

    def test_cache_map_session(self):
        self.hello()
        self.c.send({"type": "cache_map", "id": 3, "z": 14, "x0": 4680, "y0": 6260, "x1": 4681, "y1": 6260})
        m, _ = self.c.until(lambda m: m["type"] == "cache_map_result", timeout=10)
        self.assertEqual((m["id"], m.get("error")), (3, "no cache view (no download cache)"))
        self.server.covermap = covermap.CoverMap(FakeCache(["imagery/usgs/16/18720/25040.jpg"]), self.tmp.name)
        self.server.covermap.build(wait=1.0)
        self.c.send({"type": "cache_map", "id": 4, "z": 14, "x0": 4680, "y0": 6260, "x1": 4681, "y1": 6260,
                     "mode": "imagery"})
        m, _ = self.c.until(lambda m: m["type"] == "cache_map_result" and m["id"] == 4, timeout=10)
        self.assertEqual((m["z"], m["x0"], m["y0"], m["w"], m["h"], m["ready"]), (14, 4680, 6260, 2, 1, True))
        self.assertEqual(list(base64.b64decode(m["cells"])), [16 | 0x80, 0])
        self.c.send({"type": "cache_map", "id": 5, "z": 3, "x0": 0, "y0": 0, "x1": 1, "y1": 1})
        m, _ = self.c.until(lambda m: m["type"] == "cache_map_result" and m["id"] == 5, timeout=10)
        self.assertIn("zoom", m["error"])


if __name__ == "__main__":
    unittest.main()
