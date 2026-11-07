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
import vegclass  # noqa: E402  fork
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

    def test_a_wider_drawn_road_pushes_its_tree_further(self):
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        v._specs = lambda f: [SimpleNamespace(width=16.0)] * len(f.kind)     # drawn 16 m wide (parking lanes)
        it = v.cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        rlon, rlat = md.lonlat(100, 400)
        off = (np.hypot((trees.lon - rlon) * 85000, (trees.lat - rlat) * 111000)).min()
        self.assertGreater(off, 8.0 + veg.TREE_PIT_M, "past the drawn curb, not the class's")

    def test_its_sidewalks_kept_clear_too(self):
        # GitHub #424 / #425: the canopy's trees and the bushes stood on the sidewalks; a street tree (a top over the
        # road) still lands in a pit at the curb
        import roads as roadsmod
        md = FakeMapData()
        v = veg.Vegetation(md, None)
        v._specs = lambda f: [SimpleNamespace(width=8.0, walk=6.0)] * len(f.kind)
        orig = roadsmod.has_sidewalks
        roadsmod.has_sidewalks = lambda sp: True
        try:
            it = v.cell(KEY)
        finally:
            roadsmod.has_sidewalks = orig
        trees = it.take(it.kind == veg.KIND_TREE)
        self.assertEqual(len(trees), 2, "the street tree kept")
        rlon, rlat = md.lonlat(100, 400)
        off = np.hypot((trees.lon - rlon) * 85000, (trees.lat - rlat) * 111000).min()
        self.assertLess(off, 4.0 + veg.TREE_PIT_M + 0.6 + 4.0, "in a pit by the curb (whole CHM pixels), not past it")

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


class AustralianTreesTest(unittest.TestCase):
    """fork: australian_trees, the eucalypt / rainforest / palm mix inside Australia (regional() is North America's)."""
    N = 20000

    def run_at(self, generic, h, lat, lon, lcls=10, seed=1):
        u = np.random.default_rng(seed).random((self.N, 3))
        gen = np.full(self.N, generic, np.uint8)
        height = np.full(self.N, float(h))
        la, lo = np.full(self.N, lat), np.full(self.N, lon)
        cl = np.full(self.N, lcls, np.uint8)
        reg = veg.regional(gen, height, la, lo, cl, u[:, 0])
        return veg.australian_trees(gen, reg, height, la, lo, cl, u), reg

    def share(self, out, group):
        return float((out == group).mean())

    def test_group_ids_unchanged_eucalypt_last(self):
        self.assertEqual(veg.GROUPS[:11], ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper",
                                           "desert_shrub", "desert_rock", "oak_west", "tropical"))
        self.assertEqual((veg.GROUPS[11], veg.G_EUCALYPT), ("eucalypt", 11))
        self.assertEqual(veg.GROUPS[12:], ("dry_rainforest", "mallee", "scrub", "mangrove"), "the palettes' groups follow it")

    def test_brisbane_mostly_eucalypt(self):
        for generic, want in ((veg.G_DECIDUOUS, 0.85), (veg.G_CONIFER, 0.85), (veg.G_OAK_WEST, 0.70)):
            out, _ = self.run_at(generic, 15.0, -27.47, 153.03)
            self.assertAlmostEqual(self.share(out, veg.G_EUCALYPT), want, delta=0.02, msg=veg.GROUPS[generic])
            self.assertEqual(self.share(out, veg.G_EUCALYPT) + self.share(out, generic), 1.0, "the rest stay")
            self.assertEqual(self.share(out, veg.G_PALM) + self.share(out, veg.G_TROPICAL), 0.0, "Cfa, not in town")

    def test_cairns_tall_trees_partly_tropical(self):
        out, _ = self.run_at(veg.G_DECIDUOUS, 20.0, -16.92, 145.77)
        self.assertAlmostEqual(self.share(out, veg.G_TROPICAL), 0.60, delta=0.02)
        self.assertAlmostEqual(self.share(out, veg.G_EUCALYPT), 0.40 * 0.85, delta=0.02)
        low, _ = self.run_at(veg.G_DECIDUOUS, 8.0, -16.92, 145.77)
        self.assertEqual(self.share(low, veg.G_TROPICAL), 0.0, "under 12 m: no rainforest tree")
        self.assertAlmostEqual(self.share(low, veg.G_EUCALYPT), 0.85, delta=0.02)
        dry, _ = self.run_at(veg.G_DECIDUOUS, 20.0, -23.7, 133.88)      # Alice Springs (arid): none
        self.assertEqual(self.share(dry, veg.G_TROPICAL), 0.0)

    def test_queensland_towns_have_palms(self):
        wc_built = veg.WC["built"]
        out, _ = self.run_at(veg.G_DECIDUOUS, 8.0, -27.47, 153.03, wc_built)
        self.assertAlmostEqual(self.share(out, veg.G_PALM), 0.15, delta=0.02)
        south, _ = self.run_at(veg.G_DECIDUOUS, 8.0, -33.87, 151.2, wc_built)        # Sydney, south of -29: none
        self.assertEqual(self.share(south, veg.G_PALM), 0.0)
        self.assertGreater(self.share(south, veg.G_EUCALYPT), 0.8)

    def test_shrubs_and_rocks_keep_regional(self):
        for generic in (veg.G_SHRUB, veg.G_ROCK):
            for lat, lon, lcls in ((-27.47, 153.03, 20), (-25.0, 133.0, 20), (-25.0, 133.0, 60)):
                out, reg = self.run_at(generic, 1.0, lat, lon, lcls)
                self.assertTrue((out == reg).all(), (generic, lat, lon))

    def test_outside_australia_unchanged(self):
        # London, Seattle, Baltimore, Miami, Auckland, Jakarta: byte-identical to regional()
        for lat, lon in ((51.5, -0.12), (47.6, -122.3), (39.29, -76.58), (25.8, -80.2), (-36.85, 174.76), (-6.2, 106.8)):
            for generic in (veg.G_DECIDUOUS, veg.G_CONIFER, veg.G_SHRUB):
                for lcls in (10, veg.WC["built"]):
                    out, reg = self.run_at(generic, 15.0, lat, lon, lcls)
                    self.assertEqual(out.tobytes(), reg.tobytes(), (lat, lon, generic, lcls))
                    self.assertEqual(int((out == veg.G_EUCALYPT).sum()), 0)

    def test_mixed_batch_and_deterministic(self):
        lat = np.array([-27.47, 51.5, -16.92, 47.6]).repeat(1000)
        lon = np.array([153.03, -0.12, 145.77, -122.3]).repeat(1000)
        n = len(lat)
        u = np.random.default_rng(3).random((n, 3))
        gen = np.full(n, veg.G_DECIDUOUS, np.uint8)
        h = np.full(n, 15.0)
        cl = np.full(n, 10, np.uint8)
        reg = veg.regional(gen, h, lat, lon, cl, u[:, 0])
        a = veg.australian_trees(gen, reg, h, lat, lon, cl, u)
        self.assertEqual(a.tobytes(), veg.australian_trees(gen, reg, h, lat, lon, cl, u).tobytes())
        self.assertEqual(a[1000:2000].tobytes(), reg[1000:2000].tobytes(), "London in the same batch: unchanged")
        self.assertEqual(a[3000:].tobytes(), reg[3000:].tobytes(), "Seattle")
        self.assertGreater(float((a[:1000] == veg.G_EUCALYPT).mean()), 0.8)
        self.assertGreater(float((a[2000:3000] == veg.G_TROPICAL).mean()), 0.5)


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


class GradedFaceTest(unittest.TestCase):
    def test_no_tree_on_a_cutting_rocks_stay(self):
        """A road cut 3 m into a slope (Mount Washington, Pittsburgh): the tree and the bush on its face go, a tree
        0.5 m off the DEM stays, and a rock on the face stays (sunk into it)."""
        g = np.array([veg.G_DECIDUOUS, veg.G_SHRUB, veg.G_CONIFER, veg.G_ROCK], np.uint8)
        it = veg.Items(np.zeros(4), np.arange(4.0), np.full(4, 10.0), np.ones(4), np.zeros(4, np.uint8), g,
                       np.zeros(4), np.zeros(4), np.ones(4))
        zg = np.array([97.0, 98.0, 99.5, 97.0])        # graded
        raw = lambda la, lo: np.full(np.size(la), 100.0)    # noqa: E731
        it2, e2, n2, z2 = veg._off_graded(it, np.arange(4.0), np.zeros(4), zg, raw)
        self.assertEqual(list(it2.group), [veg.G_CONIFER, veg.G_ROCK])
        self.assertEqual(list(e2), [2.0, 3.0])
        self.assertEqual(list(z2), [99.5, 97.0])
        same = veg._off_graded(it, np.arange(4.0), np.zeros(4), zg, None)
        self.assertEqual(len(same[0]), 4, "no raw ground: all kept")


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


class OrchardTest(unittest.TestCase):
    def test_trees_stand_in_lines(self):
        import mapgeom
        frame = geodesy.EnuFrame(40.0, -76.0)
        e = np.array([-40.0, 40.0, 40.0, -40.0, -40.0])
        n = np.array([-20.0, -20.0, 20.0, 20.0, -20.0])
        la, lo, _ = frame.enu_to_geodetic(e, n, np.zeros(5))
        ring = np.stack([lo, la], 1)
        feats = mapgeom.build("land_use", np.array(["orch-1"]), [(mapgeom.POLYGON, [[ring]])])
        feats.attrs["class"] = np.array(["orchard"], object)
        x, y = veg._to3857(ring[:, 0], ring[:, 1])
        pad, dx = 40.0, 2.0
        x0, x1 = float(x.min()) - pad, float(x.max()) + pad
        y0, y1 = float(y.min()) - pad, float(y.max()) + pad
        cols = int(np.ceil((x1 - x0) / dx))
        rows = int(np.ceil((y1 - y0) / dx))
        chm = Raster(np.zeros((rows, cols), np.uint8), x0, y1, dx)
        clear = np.zeros((rows, cols), bool)
        items = veg.orchard_items(feats, chm, clear, None)
        self.assertGreaterEqual(len(items), 500, "rows at 2.5 m by 3 m")
        self.assertLessEqual(len(items), 1100)
        self.assertTrue(np.all(items.kind == veg.KIND_TREE))
        self.assertTrue(np.all((items.height >= 4.2) & (items.height <= 6.5)))
        yaw = (np.asarray(items.yaw) + 180.0) % 360.0 - 180.0
        self.assertLess(float(np.max(np.abs(yaw))), 16.0, "one row direction")
        _, nn, _ = frame.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        order = np.sort(np.asarray(nn))
        nlines = 1 + int(np.sum(np.diff(order) > 1.2))
        self.assertGreaterEqual(nlines, 12)
        self.assertLessEqual(nlines, 22)
        blocked = np.ones((rows, cols), bool)
        self.assertEqual(len(veg.orchard_items(feats, chm, blocked, None)), 0)
        half = np.zeros((rows, cols), bool)
        centres = x0 + (np.arange(cols) + 0.5) * dx
        half[:, centres < float(x.mean())] = True
        kept = veg.orchard_items(feats, chm, half, None)
        self.assertGreater(len(items), len(kept))
        self.assertGreater(len(kept), 0)
        xx, yy = veg._to3857(kept.lon, kept.lat)
        c = np.floor((xx - chm.x0) / chm.dx).astype(np.int64)
        r = np.floor((chm.y0 - yy) / chm.dy).astype(np.int64)
        self.assertFalse(bool(half[r, c].any()))


class FarmFoliageTest(unittest.TestCase):
    def test_shrubs_line_the_gaps_between_rows(self):
        import mapgeom
        frame = geodesy.EnuFrame(40.0, -76.0)
        e = np.array([-45.0, 45.0, 45.0, -45.0, -45.0])
        n = np.array([-22.0, -22.0, 22.0, 22.0, -22.0])
        la, lo, _ = frame.enu_to_geodetic(e, n, np.zeros(5))
        ring = np.stack([lo, la], 1)
        feats = mapgeom.build("land_use", np.array(["field-1"]), [(mapgeom.POLYGON, [[ring]])])
        feats.attrs["class"] = np.array(["farmland"], object)
        x, y = veg._to3857(ring[:, 0], ring[:, 1])
        pad, dx = 30.0, 2.0
        x0, x1 = float(x.min()) - pad, float(x.max()) + pad
        y0, y1 = float(y.min()) - pad, float(y.max()) + pad
        cols = int(np.ceil((x1 - x0) / dx))
        rows = int(np.ceil((y1 - y0) / dx))
        chm = Raster(np.zeros((rows, cols), np.uint8), x0, y1, dx)
        items = veg.farm_foliage(feats, chm, np.zeros((rows, cols), bool), None)
        self.assertGreaterEqual(len(items), 40)
        self.assertLessEqual(len(items), 280)
        self.assertTrue(np.all(items.kind == veg.KIND_BUSH))
        _, nn, _ = frame.geodetic_to_enu(items.lat, items.lon, np.zeros(len(items)))
        order = np.sort(np.asarray(nn))
        nlines = 1 + int(np.sum(np.diff(order) > 2.0))
        self.assertGreaterEqual(nlines, 8)
        self.assertLessEqual(nlines, 14)
        blocked = np.ones((rows, cols), bool)
        self.assertEqual(len(veg.farm_foliage(feats, chm, blocked, None)), 0)


# fork: the NVIS palettes (vegclass.py, data/veg_palettes.json, vegetation.apply_palette)
MT_ARCHER, DAINTREE, ALICE, DANDENONG = (-23.31, 150.60), (-16.17, 145.40), (-23.70, 133.88), (-37.85, 145.35)
ROCKHAMPTON, MELBOURNE, TOKYO = (-23.38, 150.51), (-37.81, 144.96), (35.68, 139.69)     # (the two cities: cleared land)


class VegClassTest(unittest.TestCase):
    def test_lookup_by_place(self):
        self.assertEqual(vegclass.at(*MT_ARCHER), "woodland")
        self.assertEqual(vegclass.at(*DAINTREE), "rainforest")
        self.assertEqual(vegclass.at(*ALICE), "shrubland")
        self.assertEqual(vegclass.at(*DANDENONG), "open_forest")
        for place in (ROCKHAMPTON, MELBOURNE, TOKYO, (51.5, -0.12), (0.0, 0.0), (-23.5, 160.0)):
            self.assertEqual(vegclass.at(*place), "", place)        # cleared land, the sea, other countries: none
        got = vegclass.at(np.array([MT_ARCHER[0], TOKYO[0], DAINTREE[0]]), np.array([MT_ARCHER[1], TOKYO[1], DAINTREE[1]]))
        self.assertEqual(list(got), ["woodland", "", "rainforest"])
        self.assertEqual(vegclass.at(np.zeros((2, 3)), np.zeros((2, 3))).shape, (2, 3))

    def test_palettes_are_consistent(self):
        names = vegclass.palette_names()
        self.assertEqual(names[0], "")
        for name in names[1:]:
            p = vegclass.palette(name)
            self.assertAlmostEqual(sum(s for _, s in p["trees"]), 1.0, 6, name)
            self.assertLessEqual(sum(s for _, s in p["shrubs"]), 1.0 + 1e-9, name)
            for g, _ in p["trees"] + p["shrubs"]:
                self.assertIn(g, veg.GROUPS, name)
            self.assertIn(p["ground"], vegclass.GROUND)
            self.assertTrue(0.0 < p["density"] <= 1.0 and 0.0 < p["scale"][0] <= p["scale"][1], name)
        for code in range(1, 33):       # every NVIS group but the cleared, water, bare, sea, unclassified and unknown ones has one
            self.assertEqual(f"au_mvg:{code}" in vegclass._load_palettes()[0]["classes"],
                             code not in (24, 25, 26, 27, 28, 30), code)

    def test_finest_layer_wins(self):
        class Fake:
            name = "au_mvg"

            def covers(self, lat, lon):
                return True

            def codes(self, lat, lon):
                return np.where(lat > -20, 23, 0).astype(np.uint8)      # mangroves north of 20 S; no class elsewhere

        old = vegclass.LAYERS
        try:
            vegclass.LAYERS = [Fake()] + old
            self.assertEqual(vegclass.at(*DAINTREE), "mangrove")
            self.assertEqual(vegclass.at(*MT_ARCHER), "woodland", "no class in the finer layer: the next one answers")
        finally:
            vegclass.LAYERS = old

    def test_wet_months_and_digest(self):
        self.assertEqual(vegclass.wet_months(*MT_ARCHER), [12, 1, 2, 3])
        self.assertEqual(vegclass.wet_months(*DANDENONG), [6, 7, 8, 9, 10], "the south: winter and spring")
        self.assertEqual(vegclass.wet_months(*TOKYO), [])
        self.assertEqual(vegclass.digest(), vegclass.digest())


class PaletteTest(unittest.TestCase):
    N = 20000

    def apply(self, group, place, cls=10, seed=3):
        u = np.random.default_rng(seed).random(self.N)
        la, lo = np.full(self.N, place[0]), np.full(self.N, place[1])
        g = np.full(self.N, group, np.uint8)
        return g, veg.apply_palette(g, la, lo, np.full(self.N, cls, np.uint8), u)

    def share(self, out, group):
        return float((out == group).mean())

    def test_woodland_is_eucalypt_only(self):
        for generic in (veg.G_DECIDUOUS, veg.G_CONIFER, veg.G_OAK_WEST, veg.G_EUCALYPT, veg.G_TROPICAL):
            _, out = self.apply(generic, MT_ARCHER)
            self.assertTrue((out == veg.G_EUCALYPT).all(), veg.GROUPS[generic])

    def test_rainforest_mix(self):
        _, out = self.apply(veg.G_DECIDUOUS, DAINTREE)
        self.assertAlmostEqual(self.share(out, veg.G_DRY_RAINFOREST), 0.55, delta=0.02)
        self.assertAlmostEqual(self.share(out, veg.G_TROPICAL), 0.45, delta=0.02)
        _, out = self.apply(veg.G_SHRUB, DAINTREE)         # bushes: the palette's shrub share, the rest keep their group
        self.assertAlmostEqual(self.share(out, veg.G_SCRUB), 0.3, delta=0.02)
        self.assertAlmostEqual(self.share(out, veg.G_SHRUB), 0.7, delta=0.02)

    def test_shrubland_trees_are_scrub_and_mallee(self):
        _, out = self.apply(veg.G_DECIDUOUS, ALICE)
        self.assertEqual(self.share(out, veg.G_SCRUB) + self.share(out, veg.G_MALLEE), 1.0)

    def test_untouched(self):
        for place in (ROCKHAMPTON, TOKYO, (47.6, -122.3), (-36.85, 174.76)):      # cleared land and other places
            for generic in (veg.G_DECIDUOUS, veg.G_CONIFER, veg.G_SHRUB, veg.G_ROCK, veg.G_PALM):
                g, out = self.apply(generic, place)
                self.assertEqual(out.tobytes(), g.tobytes(), (place, generic))
        for generic in (veg.G_ROCK, veg.G_PALM, veg.G_DESERT_ROCK):             # rocks and palms in the bush
            g, out = self.apply(generic, MT_ARCHER)
            self.assertEqual(out.tobytes(), g.tobytes(), generic)
        for cls in (veg.WC["built"], veg.WC["crop"]):                          # towns and farmland keep the old mix
            g, out = self.apply(veg.G_DECIDUOUS, MT_ARCHER, cls)
            self.assertEqual(out.tobytes(), g.tobytes(), cls)

    def test_items_density_and_size(self):
        n = 4000
        rng = np.random.default_rng(5)

        def items(place):
            la, lo = np.full(n, place[0]) + rng.random(n) * 1e-3, np.full(n, place[1]) + rng.random(n) * 1e-3
            return veg.Items(lo, la, np.full(n, 10.0, np.float32), np.full(n, 2.2, np.float32),
                             np.full(n, veg.KIND_TREE, np.uint8), np.full(n, veg.G_DECIDUOUS, np.uint8),
                             rng.random(n).astype(np.float32), np.zeros(n, np.float32), np.ones(n, np.float32))
        cls = np.full(n, 10, np.uint8)
        far = items(TOKYO)
        self.assertIs(veg.palette_items(far, cls), far, "no palette here: the same items")
        bush = items(MT_ARCHER)                                                 # woodland: density 1, heights 0.75 .. 1.25
        out = veg.palette_items(bush, cls)
        self.assertEqual(len(out), n)
        self.assertTrue((out.group == veg.G_EUCALYPT).all())
        self.assertGreaterEqual(float(out.height.min()), 7.5 - 1e-3)
        self.assertLessEqual(float(out.height.max()), 12.5 + 1e-3)
        self.assertGreater(float(out.height.max() - out.height.min()), 3.0)
        self.assertTrue(np.array_equal(out.rank, bush.rank))
        self.assertEqual(vegclass.at(-19.0, 136.5), "tussock")                    # the Barkly Tableland: density 0.45
        grass = veg.palette_items(items((-19.0, 136.5)), cls)
        self.assertAlmostEqual(len(grass) / n, 0.45, delta=0.03)
        self.assertLess(float(grass.rank.max()), 1.0)

    def test_cache_stamp_empties_old_cells(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            v = veg.Vegetation(None, d)
            cell = os.path.join(v.dir, "14", "1", "2.npz")
            os.makedirs(os.path.dirname(cell))
            open(cell, "wb").close()
            veg.Vegetation(None, d)
            self.assertTrue(os.path.exists(cell), "same palettes: the cells stay")
            with open(os.path.join(v.dir, "palettes.sha"), "w") as f:
                f.write("old")
            veg.Vegetation(None, d)
            self.assertFalse(os.path.exists(cell), "other palettes: the cells are dropped")
            with open(os.path.join(v.dir, "palettes.sha")) as f:
                self.assertEqual(f.read(), vegclass.digest())
