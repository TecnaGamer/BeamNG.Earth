"""vegetation: tree tops from a canopy height model (one per crown, the tallest), never on buildings or roads,
deterministic; tiles of items in the anchor frame."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import vegetation as veg  # noqa: E402

LAT, LON = 39.29, -76.58
KEY = mapcells.cell_of(LAT, LON)


class Raster:
    def __init__(self, data, x0, y0, dx, nodata=None):
        self.data, self.x0, self.y0, self.dx, self.dy, self.nodata = data, x0, y0, dx, dx, nodata

    def sample(self, lon, lat):
        return np.full(np.shape(lon), 30, np.uint8)        # grass everywhere


class Feats:
    """mapgeom.Features' fields the vegetation reads: one part of one ring per feature."""

    def __init__(self, kind, rows, cls=None):
        self.kind = np.full(len(rows), kind, np.uint8)
        self.rows = rows
        self.attrs = {"class": np.array(cls or [""] * len(rows), object)}
        self.lonlat = np.concatenate([np.asarray(r, np.float64) for r in rows]) if rows else np.zeros((0, 2))
        self.coord_off = np.cumsum([0] + [len(r) for r in rows]).astype(np.int64)
        self.ring_off = np.arange(len(rows) + 1, dtype=np.int64)
        self.part_off = np.arange(len(rows) + 1, dtype=np.int64)

    def parts(self, i):
        return [[np.asarray(self.rows[i], np.float64)]]


def cell_data():
    """A cell whose canopy has three crowns: one free, one under a building, one on a road."""
    lon0, lat0, lon1, lat1 = KEY.bounds()
    x0, y1 = veg._to3857(lon0, lat1)
    x1, y0 = veg._to3857(lon1, lat0)
    dx = (x1 - x0) / 512
    h = np.zeros((512, 512), np.uint8)
    yy, xx = np.mgrid[0:512, 0:512]
    crowns = [(100, 100), (300, 300), (100, 400)]          # (row, col)
    for r, c in crowns:
        h = np.maximum(h, np.clip(15 - np.hypot(yy - r, xx - c), 0, 15).astype(np.uint8))
    chm = Raster(h, x0, y1, dx, nodata=255)

    def lonlat(r, c):
        return veg._to4326(x0 + (c + 0.5) * dx, y1 - (r + 0.5) * dx)

    def square(r, c, half):
        pts = [lonlat(r - half, c - half), lonlat(r - half, c + half), lonlat(r + half, c + half),
               lonlat(r + half, c - half), lonlat(r - half, c - half)]
        return [(float(a), float(b)) for a, b in pts]
    building = Feats(3, [square(300, 300, 6)])
    a, b = lonlat(100, 380), lonlat(100, 420)
    road = Feats(2, [[(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))]], ["residential"])
    layers = {"canopy": chm, "landcover": Raster(None, 0, 0, 1), "buildings": building, "roads": road,
              "water": Feats(3, [])}
    return SimpleNamespace(bounds=KEY.bounds(), layers=layers), crowns, lonlat


class FakeMapData:
    release = "t"

    def __init__(self):
        self.cd, self.crowns, self.lonlat = cell_data()

    def cell(self, key, layers=None, kind="demand"):
        if tuple(key)[1:] == (KEY.x, KEY.y):
            return self.cd
        empty = SimpleNamespace(bounds=None, layers={"buildings": None, "roads": None, "water": None})
        return empty


class VegetationTest(unittest.TestCase):
    def test_one_tree_per_free_crown(self):
        md = FakeMapData()
        it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        self.assertEqual(len(trees), 2, "the building's crown is left out, the road's moves off the road")
        lon, lat = md.lonlat(100, 100)
        free = np.argmin(abs(trees.lon - lon) * 85000 + abs(trees.lat - lat) * 111000)
        self.assertLess(abs(trees.lon[free] - lon) * 85000 + abs(trees.lat[free] - lat) * 111000, 4.0)
        self.assertGreaterEqual(trees.height[free], 12.0)
        rlon, rlat = md.lonlat(100, 400)                   # on the residential road's centreline
        street = 1 - free
        off = abs(trees.lat[street] - rlat) * 111000
        curb = veg.ROAD_HALF_WIDTH_M["residential"] + veg.TREE_PIT_M + 0.6     # a tree pit just past the curb
        self.assertGreater(off, curb - 1.0, "beside the road")
        self.assertLess(off, curb + 3.0)                   # (whole CHM pixels, ~2 m here)

    def test_footway_keeps_its_tree(self):
        md = FakeMapData()
        md.cd.layers["roads"].attrs["class"][:] = "footway"   # a sidewalk mapped as a line: street trees stand on it
        it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        rlon, rlat = md.lonlat(100, 400)
        self.assertLess((np.hypot((trees.lon - rlon) * 85000, (trees.lat - rlat) * 111000)).min(), 4.0)

    def test_deterministic(self):
        a = veg.Vegetation(FakeMapData(), None).cell(KEY)
        b = veg.Vegetation(FakeMapData(), None).cell(KEY)
        for f in ("lon", "lat", "height", "kind", "group", "rank", "yaw", "scale"):
            np.testing.assert_array_equal(getattr(a, f), getattr(b, f))

    def test_thin_keeps_the_tallest(self):
        jj = np.array([10, 10, 11, 40])          # crown radius of the 14 m top: 0.5 (0.12 h + 1) / 1.2 = 1.1 px
        ii = np.array([12, 12, 12, 40])
        h = np.array([10.0, 14.0, 9.0, 8.0])
        kj, ki = veg._thin(jj, ii, h, 1.2)
        self.assertEqual(sorted(zip(kj.tolist(), ki.tolist())), [(10, 12), (40, 40)])

    def test_tile_items_in_the_anchor_frame(self):
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        f = geodesy.EnuFrame(LAT, LON)
        items = v.cell(KEY)
        e, n, _ = f.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        tx, ty = int(np.floor(e[0] / veg.TILE)), int(np.floor(n[0] / veg.TILE))
        rows = veg.tile_items(f, tx, ty, v, lambda k: True, lambda la, lo: np.full(np.shape(la), 12.0))
        self.assertTrue(rows)
        g, x, y, z, yaw, sc, hgt, rank = rows[0]
        self.assertTrue(tx * veg.TILE <= x < (tx + 1) * veg.TILE and ty * veg.TILE <= y < (ty + 1) * veg.TILE)
        self.assertAlmostEqual(z, float(f.geodetic_to_enu(items.lat[:1], items.lon[:1], np.array([12.0]))[2][0]),
                               delta=0.5)


    def test_tile_ambience(self):
        # the forest ambience (the mod's ambience.lua): the tile's trees and bushes in AUDIO_CELL_M squares, each node
        # [x, y, z, depth, size, count] inside the tile, every sounding item counted once; Baltimore's set temperate
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        f = geodesy.EnuFrame(LAT, LON)
        items = v.cell(KEY)
        e, n, _ = f.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        tx, ty = int(np.floor(e[0] / veg.TILE)), int(np.floor(n[0] / veg.TILE))
        ground = lambda la, lo: np.full(np.shape(la), 12.0)        # noqa: E731
        js = veg.tile_items(f, tx, ty, v, lambda k: True, ground, as_json=True)
        rows = veg.tile_items(f, tx, ty, v, lambda k: True, ground)
        a = np.array(js.audio).reshape(-1, 6)
        self.assertGreater(len(a), 0)
        self.assertEqual(int(a[:, 5].sum()), sum(1 for r in rows if r[0] in veg.AUDIO_SOUND))
        self.assertTrue(((a[:, 0] >= tx * veg.TILE) & (a[:, 0] < (tx + 1) * veg.TILE)).all())
        self.assertTrue(((a[:, 4] >= 0) & (a[:, 4] <= 1)).all() and (a[:, 3] >= 0).all())
        self.assertEqual(js.biome, [0, 0])
        empty = veg.tile_items(f, tx + 40, ty + 40, v, lambda k: True, ground, as_json=True)
        self.assertEqual((empty, empty.audio), ("[]", []))         # (no items: still its set)
        self.assertEqual(empty.biome, [0, 0])

    def test_dense_tile_sends_its_lowest_ranks(self):
        # GitHub #66: Bodø's forest tiles held 10-20k items each; a tile sends TILE_ROWS_MAX at most, the lowest ranks
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        f = geodesy.EnuFrame(LAT, LON)
        items = v.cell(KEY)
        e, n, _ = f.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        tx, ty = int(np.floor(e[0] / veg.TILE)), int(np.floor(n[0] / veg.TILE))
        ground = lambda la, lo: np.full(np.shape(la), 12.0)        # noqa: E731
        full = veg.tile_items(f, tx, ty, v, lambda k: True, ground)
        self.assertGreater(len(full), 20)
        old = veg.TILE_ROWS_MAX
        veg.TILE_ROWS_MAX = 10
        try:
            capped = veg.tile_items(f, tx, ty, v, lambda k: True, ground)
        finally:
            veg.TILE_ROWS_MAX = old
        self.assertEqual(len(capped), 10)
        lowest = sorted(r[7] for r in full)[:10]
        self.assertEqual(sorted(r[7] for r in capped), lowest)
        self.assertEqual([r for r in full if r in capped], capped, "in the tile's own order")


    def test_rocks_on_bare_ground_follow_its_slope(self):
        # the Bonneville Salt Flats had a rock every ~75 m2 (the user, 2026-10-20): none on flat bare ground
        class Bare(Raster):
            def sample(self, lon, lat):
                return np.full(np.shape(lon), veg.WC["bare"], np.uint8)

        class Ground:
            def __init__(self, grade):
                self.grade = grade

            def sample(self, lat, lon, zoom):
                return np.asarray(lat, np.float64) * 111320.0 * self.grade

        def rocks(dem):
            md = FakeMapData()
            md.cd.layers["landcover"] = Bare(None, 0, 0, 1)
            v = veg.Vegetation(md, None)
            v.dem = dem
            it = v.cell(KEY)
            return int(np.isin(it.group, (veg.G_ROCK, veg.G_DESERT_ROCK)).sum()), len(it)
        none, _ = rocks(None)
        self.assertGreater(none, 100)
        flat, n_flat = rocks(Ground(0.0))
        self.assertEqual(flat, 0, "a salt pan: bare")
        steep, _ = rocks(Ground(0.2))
        self.assertEqual(steep, none, "a slope over ROCK_SLOPE: every rock")
        some, _ = rocks(Ground(0.06))
        self.assertTrue(0 < some < none, "half way: some")


    def test_no_canopy_trees_on_barren_ground(self):
        # the canopy map misread the Bonneville Salt Flats' bright salt as ~19k trees up to 21 m: on bare ground with no
        # vegetated land within BARREN_NEAR_M, its tops are dropped
        class Cover(Raster):
            def sample(self, lon, lat):
                return np.full(np.shape(lon), self.data, np.uint8)
        md = FakeMapData()
        md.cd.layers["landcover"] = Cover(veg.WC["bare"], 0, 0, 1)
        it = veg.Vegetation(md, None).cell(KEY)
        self.assertEqual(int((it.kind == veg.KIND_TREE).sum()), 0)
        md.cd.layers["landcover"] = Cover(veg.WC["grass"], 0, 0, 1)
        self.assertGreater(int((veg.Vegetation(md, None).cell(KEY).kind == veg.KIND_TREE).sum()), 0, "on grass: kept")


class RegionalTest(unittest.TestCase):
    def g(self, group, h, lat, lon, lcls, u=0.9):
        return int(veg.regional(np.array([group]), np.array([h]), np.array([lat]), np.array([lon]),
                                np.array([lcls]), np.array([u]))[0])

    def test_regions(self):
        self.assertEqual(self.g(veg.G_DECIDUOUS, 12, 39.3, -76.6, 10), veg.G_DECIDUOUS, "the east keeps its trees")
        self.assertEqual(self.g(veg.G_CONIFER, 20, 37.7, -119.6, 10), veg.G_PINE_WEST, "Sierra conifers: pines")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 5, 38.6, -109.5, 20), veg.G_JUNIPER, "Moab scrub trees: junipers")
        self.assertEqual(self.g(veg.G_ROCK, 1, 38.6, -109.5, 60), veg.G_DESERT_ROCK)
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 25.8, -80.2, 50, u=0.1), veg.G_PALM, "Miami street trees")
        # GitHub #110: Houston and Austin are live oaks and loblolly pines, few palms; the Rio Grande Valley's are palms
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 29.76, -95.37, 50, u=0.1), veg.G_DECIDUOUS, "Houston street trees")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 30.27, -97.74, 50, u=0.3), veg.G_DECIDUOUS, "Austin")
        self.assertEqual(self.g(veg.G_CONIFER, 20, 30.3, -95.5, 10), veg.G_PINE_WEST, "East Texas pines")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 26.2, -98.2, 50, u=0.1), veg.G_PALM, "McAllen street trees")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 25.8, -80.2, 50, u=0.9), veg.G_DECIDUOUS)

    def test_beyond_north_america(self):
        """2026-10-15: Douglas firs in Punjab's fields; the Sahara's bushes were the East's."""
        self.assertLess(float(veg._conifer_share(30.9, 75.8)), 0.1, "Punjab: hardly any conifers")
        self.assertLess(float(veg._conifer_share(-1.3, 36.8)), 0.05, "Nairobi")
        self.assertGreaterEqual(float(veg._conifer_share(43.0, 12.0)), 0.3, "Italy's pines")
        self.assertGreaterEqual(float(veg._conifer_share(61.0, 10.0)), 0.6, "Norway")
        self.assertEqual(float(veg._conifer_share(39.3, -76.6)), 0.2, "Maryland as before")
        self.assertEqual(self.g(veg.G_SHRUB, 1, 22.3, -4.6, 60), veg.G_DESERT_SHRUB, "the Sahara")
        self.assertEqual(self.g(veg.G_ROCK, 1, -25.0, 133.0, 20), veg.G_DESERT_ROCK, "inner Australia")
        self.assertEqual(self.g(veg.G_SHRUB, 1, 52.0, 5.0, 20), veg.G_SHRUB, "Dutch shrubland: no desert")


class DilateTest(unittest.TestCase):
    def test_grows_by_r_in_both_axes(self):
        m = np.zeros((9, 9), bool)
        m[4, 4] = True
        d = veg._dilate(m, 2)
        self.assertEqual(int(d.sum()), 25)
        self.assertTrue(d[2:7, 2:7].all())


if __name__ == "__main__":
    unittest.main()


class RockSinkTest(unittest.TestCase):
    def test_rocks_sink_into_slopes_trees_do_not(self):
        # a 45-degree hillside rising to the east (z = e): a 2 m rock sinks ROCK_SINK of its height, a tree stays
        f = geodesy.EnuFrame(LAT, LON)
        e = np.array([10.0, 20.0, 30.0]); n = np.zeros(3)
        la, lo, _ = f.enu_to_geodetic(e, n, np.zeros(3))
        it = veg.Items(lo, la, np.array([2.0, 2.0, 15.0]), np.ones(3), np.zeros(3, np.uint8),
                       np.array([veg.G_ROCK, veg.G_DESERT_ROCK, veg.G_CONIFER], np.uint8), np.zeros(3), np.zeros(3),
                       np.ones(3))

        def elev(lat, lon):
            x, _, _ = f.geodetic_to_enu(np.asarray(lat), np.asarray(lon), np.zeros(np.size(lat)))
            return np.asarray(x, np.float64)
        z = veg._sink_rocks(f, it, e, n, e.copy(), elev)
        self.assertAlmostEqual(z[0], 10.0 - veg.ROCK_SINK * 2.0, delta=0.02)
        self.assertAlmostEqual(z[1], 20.0 - veg.ROCK_SINK * 2.0, delta=0.02)
        self.assertEqual(z[2], 30.0)
        flat = veg._sink_rocks(f, it, e, n, np.zeros(3), lambda la, lo: np.zeros(np.size(la)))
        self.assertTrue(np.allclose(flat, 0.0, atol=0.02), "no sinking on flat ground")


class FlatTileTest(unittest.TestCase):
    """A vegetation tile as one flat array (the mod's hello.vegFlat): no table per item in the game's Lua (trees.lua)."""

    def test_flat_json(self):
        import json
        rows = [[0, 1.5, -2.25, 3.0, 90.0, 1.0, 12.0, 0.5], [3, -0.0, 4.0, 5.5, -45.5, 0.8, 2.0, 0.0001]]
        js = veg.JsonRows(json.dumps(rows, separators=(",", ":")))
        js.n = 2
        self.assertEqual(veg.flat_json(rows), json.dumps([v for r in rows for v in r], separators=(",", ":")))
        self.assertEqual(veg.flat_json(js), veg.flat_json(rows))
        self.assertEqual(json.loads(veg.flat_json(js)), [v for r in rows for v in r])
        one = veg.JsonRows(json.dumps(rows[:1], separators=(",", ":")))
        self.assertEqual(json.loads(veg.flat_json(one)), rows[0])
        self.assertEqual(veg.flat_json([]), "[]")
        self.assertEqual(veg.flat_json(veg.JsonRows("[]")), "[]")

    def test_write_tile_both_formats(self):
        import json
        import os
        import tempfile
        rows = [[1, 10.0, 20.0, 30.0, 0.0, 1.0, 15.0, 0.25]] * 3
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(veg.write_tile(tmp, "/v", 7, -1, 2, rows), ("/v/veg_7_-1_2.json", 3))
            self.assertEqual(veg.write_tile(tmp, "/v", 7, -1, 2, rows, flat=True), ("/v/veg_7_-1_2.flat.json", 3))
            with open(os.path.join(tmp, "veg_7_-1_2.json")) as fh:
                old = json.load(fh)
            with open(os.path.join(tmp, "veg_7_-1_2.flat.json")) as fh:
                new = json.load(fh)
        self.assertEqual(old, {"groups": list(veg.GROUPS), "items": rows})        # an older mod's: unchanged
        self.assertEqual(new, {"groups": list(veg.GROUPS), "stride": veg.FLAT_STRIDE, "flat": rows[0] * 3})
        self.assertEqual(veg.FLAT_STRIDE, len(rows[0]))


class FlatHelloTest(unittest.TestCase):
    """The connection builds its vegetation tiles flat only for a mod whose hello says it reads them (vegFlat)."""

    def run_hello(self, extra):
        import test_server
        h = test_server.ServerHarness()
        h.setUp()
        try:
            asked = []
            h.server.build_veg_tile = lambda aid, tx, ty, flat=False: asked.append(flat)
            h.c.send(dict({"type": "hello", "protocol": 4, "format": h.MOD_FORMAT, "modVersion": "0.4.0"}, **extra))
            h.c.until(lambda m: m["type"] == "hello", timeout=10)
            h.conn().build_veg_tile(1, 0, 0)
            return h.conn().veg_flat, asked
        finally:
            h.tearDown()

    def test_vegflat_hello(self):
        self.assertEqual(self.run_hello({"vegFlat": True}), (True, [True]))

    def test_older_mod_gets_rows(self):
        self.assertEqual(self.run_hello({}), (False, [False]))
        self.assertEqual(self.run_hello({"vegFlat": "yes"}), (False, [False]))      # (only a JSON true)


class DemFailedTest(unittest.TestCase):
    """A cell built while the DEM could not answer is not kept on disk (from PR #295): its slope rocks were missing for
    good."""

    def test_failed_dem_cell_not_cached(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            v = veg.Vegetation(None, cache_dir=tmp)
            v._path = lambda x, y: os.path.join(tmp, f"{x}_{y}.npz")

            def build(x, y, fail):
                v._tl.dem_failed = fail
                return veg.Items(*(np.zeros(0) for _ in range(9)))
            v._build = lambda x, y: build(x, y, True)
            v._load(1, 2)
            self.assertFalse(os.path.exists(v._path(1, 2)), "a failed DEM: not kept")
            v._build = lambda x, y: build(x, y, False)
            v._load(1, 2)
            self.assertTrue(os.path.exists(v._path(1, 2)), "answered: kept")
