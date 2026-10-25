"""vegetation: tree tops from a canopy height model (one per crown, the tallest), never on buildings or roads,
deterministic; tiles of items in the anchor frame."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


def cell_data(key=KEY):
    """A cell whose canopy has three crowns: one free, one under a building, one on a road."""
    lon0, lat0, lon1, lat1 = key.bounds()
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
    return SimpleNamespace(bounds=key.bounds(), layers=layers), crowns, lonlat


class FakeMapData:
    release = "t"

    def __init__(self, key=KEY):
        self.key = key
        self.cd, self.crowns, self.lonlat = cell_data(key)

    def cell(self, key, layers=None, kind="demand"):
        if tuple(key)[1:] == (self.key.x, self.key.y):
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

    def test_central_european_beech_woods(self):
        """Sinaia 2026-10-25: the generic broadleaves were big rust-orange oaks over a Carpathian beech wood."""
        def beech(group, lat, lon, lcls):
            return int(veg._beech_woods(np.array([group], np.uint8), np.array([lat]), np.array([lon]),
                                        np.array([lcls], np.uint8))[0])
        self.assertEqual(beech(veg.G_DECIDUOUS, 45.31, 25.57, veg.WC["tree"]), veg.G_BEECH_EU, "Sinaia's wood")
        self.assertEqual(beech(veg.G_DECIDUOUS, 47.4, 10.3, veg.WC["tree"]), veg.G_BEECH_EU, "the Alps")
        self.assertEqual(beech(veg.G_DECIDUOUS, 45.31, 25.57, veg.WC["built"]), veg.G_DECIDUOUS, "a town's trees")
        self.assertEqual(beech(veg.G_DECIDUOUS, 45.31, 25.57, veg.WC["crop"]), veg.G_DECIDUOUS, "a field's trees")
        self.assertEqual(beech(veg.G_CONIFER, 45.31, 25.57, veg.WC["tree"]), veg.G_CONIFER, "conifers stay")
        self.assertEqual(beech(veg.G_SHRUB, 45.31, 25.57, veg.WC["tree"]), veg.G_SHRUB, "bushes stay")
        self.assertEqual(beech(veg.G_DECIDUOUS, 39.3, -76.6, veg.WC["tree"]), veg.G_DECIDUOUS, "Maryland as before")
        self.assertEqual(beech(veg.G_DECIDUOUS, 52.4, 17.0, veg.WC["tree"]), veg.G_DECIDUOUS, "Poland's lowlands")
        self.assertEqual(beech(veg.G_DECIDUOUS, 41.9, 12.5, veg.WC["tree"]), veg.G_DECIDUOUS, "Rome: Mediterranean")
        self.assertEqual(veg.GROUPS[veg.G_BEECH_EU], "beech_eu")
        self.assertEqual(veg.GROUPS[:11], ("deciduous", "conifer", "shrub", "rock", "pine_west", "palm", "juniper",
                                           "desert_shrub", "desert_rock", "oak_west", "tropical"), "ids kept")

    def test_europe_conifer_share_follows_elevation(self):
        s = lambda m: float(veg._europe_conifer_share(m))                       # noqa: E731
        self.assertAlmostEqual(s(0.0), veg.EUROPE_CONIFER_LOW)
        self.assertAlmostEqual(s(veg.EUROPE_LOW_M), veg.EUROPE_CONIFER_LOW)
        self.assertAlmostEqual(s(2400.0), veg.EUROPE_CONIFER_HIGH)
        mid = (veg.EUROPE_LOW_M + veg.EUROPE_HIGH_M) / 2
        self.assertAlmostEqual(s(mid), (veg.EUROPE_CONIFER_LOW + veg.EUROPE_CONIFER_HIGH) / 2)
        ramp = veg._europe_conifer_share(np.linspace(0, 2500, 101))
        self.assertTrue((np.diff(ramp) >= 0).all(), "never fewer conifers higher up")
        self.assertLess(s(600.0), 0.2, "the valley: mostly beech")
        self.assertGreater(s(1500.0), 0.8, "the spruce belt")

    def test_conifer_shares_use_the_dem_only_in_europe(self):
        class Flat:
            def __init__(self, z):
                self.z, self.calls = z, 0

            def sample(self, lat, lon, zoom):
                self.calls += 1
                return np.full(np.shape(lat), self.z, np.float64)

        class Offline:
            def sample(self, lat, lon, zoom):
                raise IOError("no tile")
        sinaia = (np.array([45.31, 45.31]), np.array([25.57, 25.57]))
        trees = np.array([True, False])
        v = veg.Vegetation(SimpleNamespace(release="t"), None)
        regional = float(veg._conifer_share(*sinaia)[0])
        np.testing.assert_allclose(v._conifer_shares(*sinaia, trees), regional, err_msg="no DEM: as before")
        v.dem = Flat(600.0)
        low = v._conifer_shares(*sinaia, trees)
        self.assertAlmostEqual(low[0], float(veg._europe_conifer_share(600.0)))
        self.assertAlmostEqual(low[1], regional, msg="a bush's share is not the wood's")
        v.dem = Flat(1700.0)
        self.assertAlmostEqual(v._conifer_shares(*sinaia, trees)[0], veg.EUROPE_CONIFER_HIGH)
        v.dem = Offline()
        np.testing.assert_allclose(v._conifer_shares(*sinaia, trees), regional, err_msg="no tile: as before")
        v.dem = Flat(600.0)
        md = (np.array([39.3]), np.array([-76.6]))
        self.assertEqual(float(v._conifer_shares(*md, np.array([True]))[0]), 0.2, "Maryland as before")
        self.assertEqual(v.dem.calls, 0, "no elevation asked outside Europe's box")

    def test_a_cell_in_the_carpathians(self):
        """End to end: tree cover at Sinaia: beech woods and conifers, no generic broadleaves; on grass as before."""
        class Cover(Raster):
            def sample(self, lon, lat):
                return np.full(np.shape(lon), self.data, np.uint8)

        class Flat:
            def sample(self, lat, lon, zoom):
                return np.full(np.shape(lat), 900.0, np.float64)
        key = mapcells.cell_of(45.315, 25.567)
        ids = {}
        for name, cover in (("wood", veg.WC["tree"]), ("grass", veg.WC["grass"])):
            md = FakeMapData(key)
            md.cd.layers["landcover"] = Cover(cover, 0, 0, 1)
            v = veg.Vegetation(md, None)
            v.dem = Flat()
            it = v.cell(key)
            ids[name] = set(it.group[it.kind == veg.KIND_TREE].tolist())
            self.assertGreater(len(ids[name]), 0)
        self.assertLessEqual(ids["wood"], {veg.G_BEECH_EU, veg.G_CONIFER})
        self.assertLessEqual(ids["grass"], {veg.G_DECIDUOUS, veg.G_CONIFER})


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
