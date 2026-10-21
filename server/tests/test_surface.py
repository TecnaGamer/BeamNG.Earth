"""Ground surfaces under the physics block (surface.py; earthd --surface on): the land-cover class ->
material table, the steep-slope rule, the masks (one per material, every sample in exactly one,
rows as the heightmap's), the cached land cover that never waits for the network, and the server
writing the masks next to the heightmap and naming them in the terrain message."""
import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import heightmap  # noqa: E402
import mapcells  # noqa: E402
import maprasters  # noqa: E402
import surface as sf  # noqa: E402

LAT, LON = 36.5785, -118.2923


class ClassifyTest(unittest.TestCase):
    def test_classes_map_to_materials(self):
        classes = np.array([[0, 10, 20, 30], [40, 50, 60, 70], [80, 90, 95, 100]], np.uint8)
        idx = sf.classify(classes, np.zeros(classes.shape), 2.0)
        self.assertEqual(idx.tolist(), [[sf.DIRT, sf.GRASS, sf.DUSTY, sf.GRASS], [sf.DIRT, sf.DIRT, sf.LOOSE, sf.SNOW],
                                        [sf.MUD, sf.MUD, sf.MUD, sf.GRASS]])
        self.assertEqual(sf.MATERIALS[sf.DIRT], ("beamngEarthDirt", "DIRT"))
        self.assertEqual(sf.DIRT, len(sf.MATERIALS) - 1, "the default material goes last (ties pick the later layer)")

    def test_unknown_class_is_dirt(self):
        self.assertEqual(sf.classify(np.array([[7, 255]], np.uint8), np.zeros((1, 2)), 2.0).tolist(), [[sf.DIRT, sf.DIRT]])

    def test_steep_ground_is_rock_but_snow_stays(self):
        n = 8
        u = np.tile(np.arange(n) * 2.0 * 1.2, (n, 1))          # slope 1.2 (50 deg) along x
        grass = np.full((n, n), 30, np.uint8)
        self.assertTrue((sf.classify(grass, u, 2.0) == sf.ROCK).all())
        self.assertTrue((sf.classify(np.full((n, n), 70, np.uint8), u, 2.0) == sf.SNOW).all())
        gentle = np.tile(np.arange(n) * 2.0 * 0.5, (n, 1))     # 27 deg: not rock
        self.assertTrue((sf.classify(grass, gentle, 2.0) == sf.GRASS).all())

    def test_town_yards_are_mostly_lawn_in_the_east_less_in_the_dry_west(self):
        def share(lat, lon):
            la, lo = np.meshgrid(np.linspace(lat, lat + 0.02, 200), np.linspace(lon, lon + 0.02, 200))
            idx = sf.classify(np.full(la.shape, 50, np.uint8), np.zeros(la.shape), 2.0, la, lo)
            self.assertTrue(np.isin(idx, (sf.LAWN, sf.DIRT)).all())
            return float((idx == sf.LAWN).mean())
        east, moab = share(39.28, -76.60), share(38.57, -109.56)
        self.assertGreater(east, 0.8, "Baltimore")
        self.assertLess(moab, east - 0.05, "Moab: fewer (the photo decides in detail mode: photo_lawns)")
        grass = np.full((2, 2), 30, np.uint8)
        self.assertTrue((sf.classify(grass, np.zeros((2, 2)), 2.0, np.full((2, 2), 39.3), np.full((2, 2), -76.6)) == sf.GRASS).all())

    def test_photo_lawns_only_where_the_photo_is_green(self):
        import tempfile
        n = 16
        idx = np.full((n, n), sf.LAWN, np.uint8)
        idx[:, :4] = sf.ASPHALT
        rgb = np.zeros((64, 64, 3), np.uint8)
        rgb[:] = (190, 188, 180)                    # pale paving ...
        rgb[:32] = (70, 110, 50)                    # ... the north half a lawn
        with tempfile.TemporaryDirectory() as d:
            _, used = sf.write_ter(d, "b.ter", np.zeros((n, n), np.uint16), idx)
            names = sf.write_masks(idx[::-1], d, "b")
            changed = sf.photo_lawns(d, "b.ter", names, rgb)
            with open(os.path.join(d, "b.ter"), "rb") as f:
                lay = _glob(np.frombuffer(f.read()[5 + 2 * n * n:5 + 3 * n * n], np.uint8).reshape(n, n), used)
            inner = lay[1:-1, 4:-1]                 # (the edge ring is holes)
            self.assertTrue((inner[: n // 2 - 1] == sf.DIRT).all(), "south half: paved")
            self.assertTrue((inner[n // 2:] == sf.LAWN).all(), "north half: lawn")
            self.assertTrue((lay[1:-1, 1:4] == sf.ASPHALT).all())
            self.assertGreater(changed, 0)
            lawn_mask = np.array(Image.open(os.path.join(d, names[sf.LAWN])))
            self.assertEqual(lawn_mask[1, 8], 255)
            self.assertEqual(lawn_mask[-2, 8], 0)


def _glob(lay, used):
    """A .ter's layer map (indices into its own list) as MATERIALS indices."""
    lut = np.full(256, 255, np.uint8)
    lut[:len(used)] = used
    return lut[lay]


class CompactTerTest(unittest.TestCase):
    def test_the_ter_lists_only_the_materials_used(self):
        # each material a .ter lists costs the swap's first draw ~20 ms (in-game probes 2026-10-18)
        import struct
        import tempfile
        n = 16
        idx = np.full((n, n), sf.GRASS, np.uint8)
        idx[:, :4] = sf.ASPHALT
        idx[:4] = sf.LAWN
        with tempfile.TemporaryDirectory() as d:
            _, used = sf.write_ter(d, "b.ter", np.zeros((n, n), np.uint16), idx)
            self.assertEqual(used, [sf.ASPHALT, sf.GRASS, sf.LAWN, sf.DIRT])      # (a lawn may turn dirt)
            with open(os.path.join(d, "b.ter"), "rb") as f:
                raw = f.read()
            off = 5 + 3 * n * n
            self.assertEqual(struct.unpack_from("<I", raw, off)[0], 4)
            lay = np.frombuffer(raw[5 + 2 * n * n:off], np.uint8).reshape(n, n)
            self.assertEqual(lay[8, 8], 1)
            self.assertTrue((_glob(lay, used)[1:-1, 1:-1] == idx[1:-1, 1:-1]).all())

    def test_no_layers_is_the_single_dirt_layer(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sf.write_ter(d, "b.ter", np.zeros((8, 8), np.uint16), None), ("b.ter", None))


class PhotoSnowTest(unittest.TestCase):
    def test_snow_only_where_the_photo_is_white(self):
        # WorldCover "snow and ice" on the Stelvio Pass's summit where the orthophoto has grey-brown scree (#40/#44)
        import tempfile
        n = 16
        idx = np.full((n, n), sf.SNOW, np.uint8)
        rgb = np.zeros((64, 64, 3), np.uint8)
        rgb[:] = (128, 118, 100)                    # scree ...
        rgb[:32] = (235, 238, 242)                  # ... the north half snow
        with tempfile.TemporaryDirectory() as d:
            _, used = sf.write_ter(d, "b.ter", np.zeros((n, n), np.uint16), idx)
            names = sf.write_masks(idx[::-1], d, "b")
            sf.photo_lawns(d, "b.ter", names, rgb)
            with open(os.path.join(d, "b.ter"), "rb") as f:
                lay = _glob(np.frombuffer(f.read()[5 + 2 * n * n:5 + 3 * n * n], np.uint8).reshape(n, n), used)
            inner = lay[1:-1, 1:-1]
            self.assertTrue((inner[: n // 2 - 1] == sf.LOOSE).all(), "south half: scree")
            self.assertTrue((inner[n // 2:] == sf.SNOW).all(), "north half: snow")


class WarpTest(unittest.TestCase):
    """Organic class borders: the land cover is read a few metres off, by smooth world-fixed noise."""

    def grid(self, lat0=LAT, lon0=LON, n=64):
        lat = lat0 + np.arange(n)[:, None] * 2.0 / 111320.0 + np.zeros((1, n))
        lon = lon0 + np.arange(n)[None, :] * 2.0 / (111320.0 * np.cos(np.radians(lat0))) + np.zeros((n, 1))
        return lat, lon

    def offsets(self, lat, lon):
        wl, wo = sf.warp(lat, lon)
        return (wo - lon) * 111320.0 * np.cos(np.radians(lat)), (wl - lat) * 111320.0

    def test_bounded_smooth_and_not_constant(self):
        dx, dy = self.offsets(*self.grid())
        bound = sum(a for _, a in sf.WARP_OCTAVES) + 1e-6
        self.assertTrue((np.abs(dx) <= bound).all() and (np.abs(dy) <= bound).all())
        self.assertLess(np.abs(np.diff(dx, axis=1)).max(), 3.0, "smooth between 2 m samples")
        self.assertGreater(dx.std(), 1.0, "it does move the samples")

    def test_world_fixed_and_deterministic(self):
        lat, lon = self.grid()
        a = sf.warp(lat, lon)
        b = sf.warp(lat[10:20, 10:20], lon[10:20, 10:20])      # another block sharing these points
        self.assertTrue(np.array_equal(a[0][10:20, 10:20], b[0]) and np.array_equal(a[1][10:20, 10:20], b[1]))
        c = sf.warp(lat, lon)
        self.assertTrue(np.array_equal(a[0], c[0]) and np.array_equal(a[1], c[1]))

    def test_straight_border_becomes_irregular(self):
        lat, lon = self.grid(n=128)
        wl, _ = sf.warp(lat, lon)
        north = wl > lat[64, 0]                                 # a straight east-west class border
        rows = [int(np.argmax(north[:, j])) for j in range(128)]
        self.assertGreater(len(set(rows)), 4, "the border wanders instead of staying on one row")


class MaskTest(unittest.TestCase):
    def test_one_mask_per_material_every_sample_in_one(self):
        rng = np.random.default_rng(1)
        idx = rng.integers(0, len(sf.MATERIALS), (16, 16)).astype(np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            names = sf.write_masks(idx, tmp, "hm_1_0_0")
            self.assertEqual(names, sf.mask_names("hm_1_0_0"))
            masks = [np.array(Image.open(os.path.join(tmp, n))) for n in names]
            self.assertTrue(all(m.dtype == np.uint8 and set(np.unique(m)) <= {0, 255} for m in masks))
            self.assertTrue((sum((m == 255).astype(int) for m in masks) == 1).all())
            for i, m in enumerate(masks):
                self.assertTrue(((m == 255) == (idx == i)).all())
            self.assertFalse([f for f in os.listdir(tmp) if f.endswith(".tmp")])


class FakeMapData:
    """The parts of mapdata.MapData the Surface uses: path / _load / has / prefetch, in memory."""

    def __init__(self, fill):
        self.fill = fill            # key -> class value (or None: no coverage)
        self.cached = {}
        self.prefetched = []

    def path(self, key, layer):
        return (str(key), layer)

    def _load(self, path):
        return self.cached.get(path)

    def has(self, key, layer):
        return self.path(key, layer) in self.cached

    def prefetch(self, key, layers):
        self.prefetched.append((key, layers))
        self.store(key)
        return True

    def store(self, key):
        v = self.fill(key)
        path = self.path(key, "landcover")
        if v is None:
            self.cached[path] = {"missing": np.array(1), "provenance": np.array("null")}
            return
        lon0, lat0, lon1, lat1 = key.bounds()
        data = np.full((4, 4), v, np.uint8)
        meta = {"crs": "EPSG:4326", "x0": lon0, "y0": lat1, "dx": (lon1 - lon0) / 4, "dy": (lat1 - lat0) / 4,
                "nodata": 0, "files": ["fake"]}
        import json
        self.cached[path] = {"data": data, "meta": np.array(json.dumps(meta)), "provenance": np.array("null")}


class SurfaceTest(unittest.TestCase):
    def test_not_cached_is_dirt_and_queued_then_cached(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        lat = np.full((3, 3), LAT)
        lon = np.full((3, 3), LON)
        key = mapcells.cell_of(LAT, LON)
        # the first block: nothing cached yet - no data (dirt), never waiting; a fetch is queued
        md._load = lambda path, real=md._load: None if not md.prefetched else real(path)
        classes, complete = s.classes(lat, lon)
        self.assertTrue((classes == 0).all())
        s.pool.shutdown(wait=True)                      # the queued fetch ran
        self.assertIn((key, ("landcover",)), md.prefetched)
        s.pool = sf.ThreadPoolExecutor(1)
        classes, complete = s.classes(lat, lon)
        self.assertTrue((classes == 30).all() and complete)

    def test_no_coverage_is_complete_dirt(self):
        md = FakeMapData(lambda key: None)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        md.store(mapcells.cell_of(LAT, LON))
        classes, complete = s.classes(np.array([LAT]), np.array([LON]))
        self.assertEqual(classes.tolist(), [0])
        self.assertTrue(complete)

    def test_ready_waits_for_the_cells_around(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        self.addCleanup(s.close)
        self.assertTrue(s.ready(LAT, LON, radius_m=500, timeout_s=10))
        classes, complete = s.classes(np.array([LAT]), np.array([LON]))
        self.assertEqual(classes.tolist(), [30])
        self.assertTrue(complete)

    def test_ready_gives_up_after_the_timeout(self):
        md = FakeMapData(lambda key: 30)
        import threading
        gate = threading.Event()
        real = md.prefetch
        md.prefetch = lambda key, layers: (gate.wait(5), real(key, layers))[1]
        s = sf.Surface(md)
        self.addCleanup(s.close)
        import time
        t0 = time.monotonic()
        self.assertFalse(s.ready(LAT, LON, radius_m=500, timeout_s=0.3))
        self.assertLess(time.monotonic() - t0, 2.0)
        gate.set()

    def test_prefetch_queues_the_cells_around_once_per_move(self):
        md = FakeMapData(lambda key: 30)
        s = sf.Surface(md)
        s.prefetch(LAT, LON, radius_m=2000)
        s.pool.shutdown(wait=True)
        n = len(md.prefetched)
        self.assertGreaterEqual(n, 4)
        s.pool = sf.ThreadPoolExecutor(1)
        s.prefetch(LAT + 0.001, LON, radius_m=2000)    # ~110 m: not again
        s.pool.shutdown(wait=True)
        self.assertEqual(len(md.prefetched), n)


class ServerSurfaceTest(unittest.TestCase):
    """earthd with a Surface: the masks are written next to the heightmap (rows as the heightmap's,
    row 0 = north) and the terrain message names them; Server() without one sends no layers."""

    class NorthGrass:
        def classes(self, lat, lon):
            lat = np.asarray(lat)
            return np.where(lat > LAT, 30, 40).astype(np.uint8), True

        def roads(self, lat, lon):
            return [], True

    class Dem:
        def sample(self, lat, lon, zoom, progress=None):
            return np.full(np.shape(lat), 1500.0)

        def stats_snapshot(self):
            return {}

    def make(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512, **kw)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        s.dem = self.Dem()
        return s

    def test_masks_and_message(self):
        s = self.make(surface=self.NorthGrass())
        a = s.new_anchor(LAT, LON)
        info = s.make_heightmap(a, 0.0, 0.0, None)
        self.assertEqual(info["layers"], sf.mask_names(s.hm_name((a.id, 0.0, 0.0))[:-4]))
        self.assertTrue(info["surfaceComplete"])
        self.assertTrue(os.path.exists(os.path.join(s.out_dir, info["ter"])))
        self.assertTrue(os.path.exists(os.path.join(s.out_dir, s.hm_name((a.id, 0.0, 0.0)))))
        for mask_file in info["layers"]:
            self.assertTrue(os.path.exists(os.path.join(s.out_dir, mask_file)))
        # Follow blocks suppress hm PNG and mask writes
        info_follow = s.make_heightmap(a, 128.0, 0.0, None)
        self.assertFalse(os.path.exists(os.path.join(s.out_dir, s.hm_name((a.id, 128.0, 0.0)))))
        for mask_file in info_follow["layers"]:
            self.assertFalse(os.path.exists(os.path.join(s.out_dir, mask_file)))
        conn = earthd.Connection.__new__(earthd.Connection)
        conn.s = s
        msg = conn.terrain_msg((a.id, 0.0, 0.0), info)
        # only the .ter's own materials (grass and dirt), in its order
        self.assertEqual(info["mats"], [sf.GRASS, sf.DIRT])
        self.assertEqual(msg["layers"], [f"{s.vfs_dir}/{info['layers'][i]}" for i in (sf.GRASS, sf.DIRT)])
        self.assertEqual(msg["materials"], [list(sf.MATERIALS[i]) for i in (sf.GRASS, sf.DIRT)])

    def test_default_server_sends_no_layers(self):
        s = self.make()
        a = s.new_anchor(LAT, LON)
        info = s.make_heightmap(a, 0.0, 0.0, None)
        self.assertNotIn("layers", info)
        conn = earthd.Connection.__new__(earthd.Connection)
        conn.s = s
        self.assertNotIn("layers", conn.terrain_msg((a.id, 0.0, 0.0), info))

    def test_cli_flag(self):
        with open(earthd.__file__) as f:
            src = f.read()
        self.assertIn('ap.add_argument("--surface", choices=("on", "off"), default="on"', src)


class LotTest(unittest.TestCase):
    def test_parking_lot_painted_asphalt(self):
        lat0, lon0 = 40.0, -76.0
        lat = lat0 + np.arange(50)[:, None] * 2e-5 + np.zeros((1, 50))
        lon = lon0 + np.arange(50)[None, :] * 2.6e-5 + np.zeros((50, 1))

        class Lots:
            kind = np.array([3, 3], np.uint8)
            attrs = {"class": np.array(["parking", "fuel"], object)}

            def __len__(self):
                return 2

            def parts(self, i):
                a, b = lon0 + 10 * 2.6e-5, lon0 + 20 * 2.6e-5
                c, d = lat0 + 10 * 2e-5, lat0 + 20 * 2e-5
                return [[np.array([(a, c), (b, c), (b, d), (a, d), (a, c)])]]
        idx = np.full((50, 50), sf.GRASS, np.uint8)
        out = sf.paint_lots(idx, lat, lon, [Lots()])
        self.assertEqual(out[15, 15], sf.ASPHALT)
        self.assertEqual(out[40, 40], sf.GRASS)
        self.assertEqual(int((out == sf.ASPHALT).sum()), int((sf.paint_lots(idx, lat, lon, [Lots()]) == sf.ASPHALT).sum()))


    def test_square_painted_paving(self):
        """A land-use square (subtype pedestrian) is paving (the Zocalo read as dirt); a park is not."""
        lat0, lon0 = 19.43, -99.13
        lat = lat0 + np.arange(50)[:, None] * 2e-5 + np.zeros((1, 50))
        lon = lon0 + np.arange(50)[None, :] * 2.1e-5 + np.zeros((50, 1))

        class Land:
            kind = np.array([3, 3], np.uint8)
            attrs = {"class": np.array(["plaza", "park"], object), "subtype": np.array(["pedestrian", "park"], object)}

            def __len__(self):
                return 2

            def parts(self, i):
                k = 0 if i == 0 else 25
                a, b = lon0 + (5 + k) * 2.1e-5, lon0 + (20 + k) * 2.1e-5
                c, d = lat0 + 5 * 2e-5, lat0 + 20 * 2e-5
                return [[np.array([(a, c), (b, c), (b, d), (a, d), (a, c)])]]
        idx = np.full((50, 50), sf.DIRT, np.uint8)
        out = sf.paint_lots(idx, lat, lon, [Land()])
        self.assertEqual(out[12, 12], sf.ASPHALT)
        self.assertEqual(out[12, 37], sf.DIRT)


if __name__ == "__main__":
    unittest.main()
