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


class LatitudeTest(unittest.TestCase):
    """The canopy raster and the scatter grids are in Web Mercator units: the same pixels cover cos(lat) as much ground.
    A flat forest at 69 N had ~4.5x the trees per km2 of the same forest at 39 N, and its flat crowns' tops stood on a
    lattice (a player's Lapland report, 2026-11-14). Now about the same density, and no lattice."""

    @staticmethod
    def density(lat, lon, dx):
        key = mapcells.cell_of(lat, lon)
        lon0, lat0, lon1, lat1 = key.bounds()
        cx, cy = veg._to3857((lon0 + lon1) / 2, (lat0 + lat1) / 2)
        n = 360                                                   # (a window of the cell: the real raster's pixels)
        x0, y1 = cx - n * dx / 2, cy + n * dx / 2
        h = np.full((n, n), 12, np.uint8)                         # one flat 12 m forest (an upsampled canopy's plateau)

        class Trees(Raster):
            def sample(self, lon, lat):
                return np.full(np.shape(lon), 10, np.uint8)      # WorldCover tree cover

        layers = {"canopy": Raster(h, x0, y1, dx, nodata=255), "landcover": Trees(None, 0, 0, 1),
                  "buildings": Feats(3, []), "roads": Feats(2, []), "water": Feats(3, [])}
        cd = SimpleNamespace(bounds=key.bounds(), layers=layers)

        class MD:
            release = "t"

            def cell(self, k, layers=None, kind="demand"):
                if tuple(k)[1:] == (key.x, key.y):
                    return cd
                return SimpleNamespace(bounds=None, layers={"buildings": None, "roads": None, "water": None})
        it = veg.Vegetation(MD(), None).cell(key)
        trees = it.take(it.kind == veg.KIND_TREE)
        km2 = (n * dx * np.cos(np.radians((lat0 + lat1) / 2)) / 1000.0) ** 2   # the window's ground area
        return len(trees) / km2, trees

    def test_same_density_at_69_n_as_at_39_n(self):
        dx = 1.194                                                # (the z17 canopy raster's pixel: the same at both)
        d39, _ = self.density(39.29, -76.58, dx)
        d69, trees = self.density(69.0127, 28.6714, dx)
        self.assertGreater(d39, 0)
        self.assertLess(d69 / d39, 1.6, (d39, d69))
        self.assertGreater(d69 / d39, 0.6, (d39, d69))
        # no lattice: the tops of a flat crown were kept in the raster's order, rows and columns at one spacing
        lat = np.asarray(trees.lat)
        rows = np.unique(np.round((lat - lat.min()) * 1e5).astype(np.int64))
        self.assertGreater(len(rows), len(lat) ** 0.5 * 1.5, "tops on few rows: a lattice")

    def test_merc_k(self):
        self.assertAlmostEqual(veg._merc_k(veg.MERC_REF_LAT), 1.0, places=9)
        self.assertAlmostEqual(veg._merc_k(69.0), 2.137, places=2)
        self.assertAlmostEqual(veg._merc_k(-69.0), veg._merc_k(69.0), places=12)


class SpeciesTest(unittest.TestCase):
    """species_groups (29): Eurasia's birches / oaks / beeches and pines / spruces from the stock shapes, dead trees,
    the warm towns' palms, the rainforests' trees."""

    def g(self, group, lat, lon, lcls=10, h=14.0, elev=None, u=0.5, ub=None, uc=None, snag=0.9, palm=0.9):
        import climate
        la, lo = np.array([lat]), np.array([lon])
        k = climate.koppen_many(la, lo)
        return int(veg.species_groups(np.array([group], np.uint8), np.array([h]), la, lo, np.array([lcls]), k,
                                      None if elev is None else np.array([float(elev)]),
                                      np.array([u if ub is None else ub]), np.array([u if uc is None else uc]),
                                      np.array([snag]), np.array([palm]))[0])

    def test_conifers_pines_low_spruces_up(self):
        self.assertEqual(self.g(veg.G_CONIFER, 52.23, 21.0, elev=100, u=0.9), veg.G_PINE_EU, "Warsaw: pines")
        self.assertEqual(self.g(veg.G_CONIFER, 52.23, 21.0, elev=100, u=0.05), veg.G_SPRUCE, "a few spruces")
        self.assertEqual(self.g(veg.G_CONIFER, 49.25, 19.95, elev=1200, u=0.8), veg.G_SPRUCE, "the Tatras: spruces")
        self.assertEqual(self.g(veg.G_CONIFER, 52.23, 21.0, elev=None, u=0.9), veg.G_PINE_EU, "no DEM: the lowlands")

    def test_broadleaves_by_climate_and_height(self):
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, elev=100, u=0.1), veg.G_BIRCH)
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, elev=100, u=0.5), veg.G_OAK)
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, elev=100, u=0.9), veg.G_BEECH)
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, elev=100, u=0.6), veg.G_OAK, "lowland: an oak")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 49.2, 22.6, elev=900, u=0.6), veg.G_BEECH, "Bieszczady: a beech")
        for u in (0.1, 0.5, 0.95):
            self.assertEqual(self.g(veg.G_DECIDUOUS, 68.0, 25.0, elev=200, u=u), veg.G_BIRCH, "Lapland: birches")

    def test_the_americas_and_others_keep_theirs(self):
        # (36) North America's broadleaves by its own shares (broad_shares): Maryland's oaks, hardly any birches
        self.assertEqual(self.g(veg.G_DECIDUOUS, 39.3, -76.6, u=0.1), veg.G_OAK, "Maryland")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 39.3, -76.6, u=0.9), veg.G_BEECH, "its maples and beeches")
        # (43) its conifers no longer the generic group (Douglas firs): mostly pines this far south
        self.assertEqual(self.g(veg.G_CONIFER, 39.3, -76.6, u=0.9), veg.G_PINE_EU, "Maryland's pines")
        self.assertEqual(self.g(veg.G_MEDITERRANEAN, 41.9, 12.5, u=0.1), veg.G_MEDITERRANEAN, "Rome's own trees")
        self.assertEqual(self.g(veg.G_PALM, 25.8, -80.2, lcls=50, palm=0.0), veg.G_PALM, "Miami")

    def test_rainforest(self):
        self.assertEqual(self.g(veg.G_DECIDUOUS, -3.1, -60.0, h=8.0), veg.G_TROPICAL, "Manaus: no oaks")
        self.assertEqual(self.g(veg.G_PALM, -3.1, -60.0, palm=0.9), veg.G_TROPICAL, "fewer palms in the forest")
        self.assertEqual(self.g(veg.G_PALM, -3.1, -60.0, palm=0.1), veg.G_PALM)
        self.assertEqual(self.g(veg.G_PALM, -3.1, -60.0, lcls=50, palm=0.9), veg.G_PALM, "in town as before")

    def test_warm_towns_palms(self):
        self.assertEqual(self.g(veg.G_MEDITERRANEAN, 41.39, 2.17, lcls=50, palm=0.05), veg.G_PALM_DRY, "Barcelona")
        self.assertEqual(self.g(veg.G_MEDITERRANEAN, 41.39, 2.17, lcls=50, palm=0.5), veg.G_MEDITERRANEAN)
        self.assertEqual(self.g(veg.G_MEDITERRANEAN, 41.39, 2.17, lcls=10, palm=0.05), veg.G_MEDITERRANEAN,
                         "not in the hills' woods")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 25.2, 55.27, lcls=50, palm=0.3), veg.G_PALM_DRY, "Dubai")
        self.assertNotEqual(self.g(veg.G_DECIDUOUS, 48.14, 11.58, lcls=50, palm=0.01), veg.G_PALM_DRY, "Munich: none")

    def test_dead_trees(self):
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, elev=100, u=0.5, snag=0.001), veg.G_SNAG_BROAD)
        self.assertEqual(self.g(veg.G_CONIFER, 52.23, 21.0, elev=100, u=0.9, snag=0.001), veg.G_SNAG_CONIFER)
        self.assertEqual(self.g(veg.G_CONIFER, 39.3, -76.6, snag=0.001), veg.G_SNAG_CONIFER, "everywhere temperate")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, lcls=50, u=0.5, snag=0.001), veg.G_OAK, "not in town")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 52.23, 21.0, h=25.0, u=0.5, snag=0.001), veg.G_OAK, "nor tall")

    def test_stands(self):
        """Trees a few metres apart mostly share a stand's value, trees far apart do not; uniform over many stands;
        the same for the same tree."""
        rng = np.random.default_rng(3)
        x0, y0 = 2338000.0, 6840000.0       # (Warsaw, Web Mercator)
        mx = x0 + rng.uniform(0, 4000, 20000)
        my = y0 + rng.uniform(0, 4000, 20000)
        gx, gy = (mx / 1.2).astype(np.int64), (my / 1.2).astype(np.int64)
        u = veg.stand_u(mx, my, gx, gy, 52.2, 51)
        self.assertTrue(np.array_equal(u, veg.stand_u(mx, my, gx, gy, 52.2, 51)))
        self.assertAlmostEqual(float(u.mean()), 0.5, delta=0.03)
        d = veg.stand_u(mx + 3.0, my, gx + 1, gy, 52.2, 51)
        self.assertGreater(float((np.abs(d - u) < 1e-12).mean()), 0.7, "neighbours in one stand")
        far = veg.stand_u(mx + 2000.0, my, gx, gy, 52.2, 51)
        self.assertLess(float((np.abs(far - u) < 1e-12).mean()), 0.05)


class ShrubTest(unittest.TestCase):
    """shrub_groups / climate_shrubs (30): the bushes by the climate, ferns and dead wood on the forest floor; the
    mangroves' trees tropical."""

    def g(self, lat, lon, group=None, lcls=20, h=1.2, u=0.5, uf=0.9, floor=False):
        import climate
        la, lo = np.array([lat]), np.array([lon])
        g, hh = veg.shrub_groups(np.array([veg.G_SHRUB if group is None else group], np.uint8), np.array([h]), la, lo,
                                 np.array([lcls]), climate.koppen_many(la, lo), np.array([u]), np.array([uf]),
                                 np.array([floor]))
        return int(g[0]), float(hh[0])

    def test_by_the_climate(self):
        self.assertEqual(self.g(43.3, 5.4)[0], veg.G_MAQUIS, "Marseille: maquis and garrigue")
        self.assertEqual(self.g(37.98, 23.73)[0], veg.G_MAQUIS, "Athens")
        self.assertEqual(self.g(34.2, -118.4, u=0.3)[0], veg.G_MAQUIS, "Los Angeles: chaparral")
        self.assertEqual(self.g(34.2, -118.4, u=0.9)[0], veg.G_SHRUB, "... and some of the rest as before")
        self.assertEqual(self.g(34.2, -118.4, group=veg.G_DESERT_SHRUB, u=0.9)[0], veg.G_DESERT_SHRUB)
        self.assertEqual(self.g(68.0, 25.0)[0], veg.G_SHRUB_NORTH, "Lapland")
        self.assertEqual(self.g(46.5, 7.9)[0], veg.G_SHRUB_NORTH, "the Alps over the tree line (ET)")
        self.assertEqual(self.g(-3.1, -60.0)[0], veg.G_SHRUB_TROPICAL, "Manaus")
        self.assertEqual(self.g(25.2, 55.3, lcls=30)[0], veg.G_DESERT_SHRUB_HOT, "Dubai, on grass too, no junipers")
        self.assertEqual(self.g(47.9, 106.9, lcls=30)[0] in (veg.G_DESERT_SHRUB, veg.G_SHRUB), True, "Mongolia")
        self.assertEqual(self.g(52.23, 21.0)[0], veg.G_SHRUB, "Warsaw: the broadleaf bushes as before")
        self.assertEqual(self.g(39.3, -76.6)[0], veg.G_SHRUB, "Maryland")

    def test_mangroves(self):
        self.assertEqual(self.g(24.85, 66.95, lcls=veg.WC["mangrove"])[0], veg.G_SHRUB_TROPICAL,
                         "Karachi's mangroves (a hot desert's)")
        import climate
        la, lo = np.array([24.85]), np.array([66.95])
        k = climate.koppen_many(la, lo)
        for grp in (veg.G_DECIDUOUS, veg.G_MEDITERRANEAN, veg.G_PALM):
            g = veg.species_groups(np.array([grp], np.uint8), np.array([6.0]), la, lo,
                                   np.array([veg.WC["mangrove"]]), k, None, np.array([0.5]), np.array([0.5]),
                                   np.array([0.9]), np.array([0.9]))
            self.assertEqual(int(g[0]), veg.G_TROPICAL)

    def test_the_forest_floor(self):
        self.assertEqual(self.g(52.23, 21.0, lcls=10, uf=0.2, floor=True)[0], veg.G_FERN, "ferns")
        self.assertEqual(self.g(52.23, 21.0, lcls=10, uf=0.2, h=1.9, floor=True)[0], veg.G_SHRUB, "not for the tall")
        self.assertEqual(self.g(52.23, 21.0, lcls=10, uf=0.8, floor=True)[0], veg.G_SHRUB)
        self.assertEqual(self.g(52.23, 21.0, uf=0.2)[0], veg.G_SHRUB, "open land: no ferns")
        self.assertEqual(self.g(-3.1, -60.0, lcls=10, uf=0.05, floor=True)[0], veg.G_TRO_UNDERSTORY, "shade plants")
        self.assertEqual(self.g(-3.1, -60.0, lcls=30)[0], veg.G_SHRUB_TROPICAL, "in the open: bushes")
        self.assertEqual(self.g(25.2, 55.3, lcls=10, uf=0.05, floor=True)[0], veg.G_DESERT_SHRUB_HOT)
        self.assertEqual(self.g(43.3, 5.4, lcls=10, uf=0.2, floor=True)[0], veg.G_MAQUIS, "no ferns in the dry south")
        for uf in np.linspace(0, 0.999, 50):        # dead wood is added elsewhere (_remote_floor), never a bush changed
            self.assertNotEqual(self.g(52.23, 21.0, lcls=10, uf=uf, floor=True)[0], veg.G_DEADWOOD)

    def test_dead_wood_only_added_in_remote_forest(self):
        class Cover(Raster):
            def __init__(self, cls):
                super().__init__(None, 0, 0, 1)
                self.cls = cls

            def sample(self, lon, lat):
                return np.full(np.shape(lon), self.cls, np.uint8)

        def build(cls):
            md = FakeMapData()
            chm = md.cd.layers["canopy"]
            chm.data = np.full(chm.data.shape, 15, np.uint8)        # one forest over the cell
            md.cd.layers["landcover"] = Cover(cls)
            md.cd.layers["buildings"] = Feats(3, [])
            a, b = md.lonlat(256, -50), md.lonlat(256, 600)           # a road across the middle
            md.cd.layers["roads"] = Feats(2, [[(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))]], ["track"])
            return md, veg.Vegetation(md, None).cell(KEY)
        md, it = build(veg.WC["tree"])
        dead = it.take(it.group == veg.G_DEADWOOD)
        self.assertGreater(len(dead), 0, "dead wood in the forest")
        _, rlat = md.lonlat(256, 0)
        self.assertGreater(float((np.abs(dead.lat - rlat) * 111000).min()), veg.DEADWOOD_ROAD_M * 0.9, "off the road")
        self.assertTrue(np.all((dead.height >= veg.DEADWOOD_H[0]) & (dead.height <= veg.DEADWOOD_H[1])))
        _, it2 = build(veg.WC["built"])
        self.assertEqual(int((it2.group == veg.G_DEADWOOD).sum()), 0, "none in a town")
        # on level ground only: a DEM sloping 20 % everywhere keeps none, a flat one all

        class Dem:
            def __init__(self, k):
                self.k = k

            def sample(self, lat, lon, zoom):
                return np.asarray(lat, np.float64) * 111000.0 * self.k
        for k, want in ((0.2, 0), (0.0, len(dead))):
            md2, _ = build(veg.WC["tree"])
            v = veg.Vegetation(md2, None)
            v.dem = Dem(k)
            got = v.cell(KEY)
            self.assertEqual(int((got.group == veg.G_DEADWOOD).sum()), want, f"slope {k}")

    def test_palms_by_the_climate(self):
        import climate
        for (lat, lon), want in (((34.05, -118.25), veg.G_PALM_DRY), ((25.77, -80.19), veg.G_PALM),
                                 ((-3.1, -60.0), veg.G_PALM), ((41.39, 2.17), veg.G_PALM_DRY)):
            la, lo = np.array([lat]), np.array([lon])
            k = climate.koppen_many(la, lo)
            g = veg.species_groups(np.array([veg.G_PALM], np.uint8), np.array([12.0]), la, lo,
                                   np.array([veg.WC["built"]]), k, None, np.array([0.5]), np.array([0.5]),
                                   np.array([0.9]), np.array([0.1]))
            self.assertEqual(int(g[0]), want, (lat, lon, int(k[0])))

    def test_redwoods_only_in_their_fog_belt(self):
        import climate
        for (lat, lon), want in (((41.2, -124.0), veg.G_REDWOOD), ((37.9, -122.6), veg.G_REDWOOD),
                                 ((46.8, -121.7), veg.G_PINE_WEST), ((33.7, -84.4), veg.G_PINE_WEST)):
            la, lo = np.array([lat]), np.array([lon])
            g = veg.species_groups(np.array([veg.G_PINE_WEST], np.uint8), np.array([32.0]), la, lo,
                                   np.array([veg.WC["tree"]]), climate.koppen_many(la, lo), None, np.array([0.5]),
                                   np.array([0.5]), np.array([0.9]), np.array([0.9]))
            self.assertEqual(int(g[0]), want, (lat, lon))

    def test_steppe_junipers(self):
        import climate
        for (lat, lon), lc, want in (((39.9, 32.9), "grass", veg.G_JUNIPER), ((39.9, 32.9), "tree", None),
                                     ((52.2, 21.0), "grass", None), ((48.0, 68.0), "grass", None),
                                     ((40.6, -2.6), "shrub", veg.G_JUNIPER)):
            la, lo = np.array([lat]), np.array([lon])
            k = climate.koppen_many(la, lo)
            g = veg.species_groups(np.array([veg.G_DECIDUOUS], np.uint8), np.array([6.0]), la, lo,
                                   np.array([veg.WC[lc]]), k, None, np.array([0.5]), np.array([0.5]),
                                   np.array([0.9]), np.array([0.1]))
            if want is None:
                self.assertNotEqual(int(g[0]), veg.G_JUNIPER, (lat, lon, lc))
            else:
                self.assertEqual(int(g[0]), want, (lat, lon, lc, int(k[0])))

    def test_eucalypt_plantations(self):
        """(35) Portugal's and Galicia's eucalypt plantations: whole stands of the lowland forest."""
        import climate
        tree, built = veg.WC["tree"], veg.WC["built"]
        for (lat, lon), grp, lc, elev, u, want in (
                ((40.64, -8.45), veg.G_MEDITERRANEAN, tree, 80, 0.1, True),      # Aveiro's hills
                ((42.88, -8.55), veg.G_DECIDUOUS, tree, 250, 0.1, True),         # Santiago de Compostela
                ((43.30, -8.30), veg.G_CONIFER, tree, None, 0.2, True),          # A Coruna, no DEM
                ((40.64, -8.45), veg.G_MEDITERRANEAN, tree, 80, 0.8, False),     # a stand of the rest
                ((40.64, -8.45), veg.G_MEDITERRANEAN, built, 80, 0.1, False),    # not in town
                ((40.32, -7.61), veg.G_CONIFER, tree, 1500, 0.1, False),         # Serra da Estrela: frost
                ((38.57, -7.91), veg.G_MEDITERRANEAN, tree, 250, 0.2, False),    # the Alentejo's montado
                ((52.23, 21.0), veg.G_DECIDUOUS, tree, 100, 0.0, False)):        # Warsaw
            la, lo = np.array([lat]), np.array([lon])
            g = veg.species_groups(np.array([grp], np.uint8), np.array([14.0]), la, lo, np.array([lc]),
                                   climate.koppen_many(la, lo), None if elev is None else np.array([float(elev)]),
                                   np.array([0.5]), np.array([0.5]), np.array([0.9]), np.array([0.9]),
                                   u_stand=np.array([u]))
            self.assertEqual(int(g[0]) == veg.G_EUCALYPTUS, want, (lat, lon, elev, u))
        self.assertAlmostEqual(float(veg.eucalypt_share([40.6], [-8.5], [100.0])[0]), veg.EUC_SHARE)

    def test_world_biomes(self):
        """(35) _world_groups: savannas' acacias, the tropics' rainforest trees, the tropics' and the south's pines,
        the laurel forests, the south's beeches, the maritime pines."""
        import climate
        tree, grass = veg.WC["tree"], veg.WC["grass"]
        for (lat, lon), grp, lc, elev, us, want, where in (
                ((13.5, 2.1), veg.G_DECIDUOUS, grass, 200, 0.5, veg.G_SAVANNA, "Niamey's savanna"),
                ((13.5, 2.1), veg.G_TROPICAL, grass, 200, 0.5, veg.G_SAVANNA, "regional's tropical tree there"),
                ((-2.5, 37.5), veg.G_DECIDUOUS, grass, 1200, 0.5, veg.G_SAVANNA, "Kenya's"),
                ((-15.8, -47.9), veg.G_DECIDUOUS, tree, 1100, 0.5, veg.G_SAVANNA, "the Cerrado"),
                ((-3.1, -60.0), veg.G_DECIDUOUS, tree, 50, 0.5, veg.G_TROPICAL, "the Amazon"),
                ((22.5, 79.0), veg.G_DECIDUOUS, tree, 400, 0.5, veg.G_TROPICAL, "the Deccan's forests"),
                ((-41.0, -72.5), veg.G_DECIDUOUS, tree, 300, 0.1, veg.G_LAUREL, "Valdivia's evergreen forest"),
                ((-41.0, -72.5), veg.G_DECIDUOUS, tree, 300, 0.95, veg.G_BEECH, "and its Nothofagus"),
                ((-45.0, -71.5), veg.G_DECIDUOUS, tree, 600, 0.95, veg.G_BEECH, "Patagonia's lenga"),
                ((25.0, 113.0), veg.G_DECIDUOUS, tree, 300, 0.2, veg.G_LAUREL, "southern China"),
                ((31.5, 130.5), veg.G_DECIDUOUS, tree, 100, 0.2, veg.G_LAUREL, "Kyushu"),
                ((52.2, 21.0), veg.G_DECIDUOUS, tree, 100, 0.01, None, "Warsaw: no laurels"),
                ((-38.4, 176.6), veg.G_CONIFER, tree, 400, 0.5, veg.G_PINE_WEST, "Kaingaroa's radiata pines"),
                ((14.5, -86.5), veg.G_CONIFER, tree, 900, 0.5, veg.G_PINE_WEST, "Honduras' pines"),
                ((44.2, -0.8), veg.G_CONIFER, tree, 50, 0.5, veg.G_PINE_MARITIME, "the Landes"),
                ((42.9, -8.5), veg.G_CONIFER, tree, 250, 0.99, veg.G_PINE_MARITIME, "Galicia"),
                ((53.6, 17.9), veg.G_CONIFER, tree, 120, 0.5, None, "Bory Tucholskie: Scots pines"),
                ((-37.5, 145.5), veg.G_DECIDUOUS, tree, 400, 0.99, veg.G_EUCALYPTUS, "Victoria's forests (38)")):
            la, lo = np.array([lat]), np.array([lon])
            g = int(veg.species_groups(np.array([grp], np.uint8), np.array([14.0]), la, lo, np.array([lc]),
                                       climate.koppen_many(la, lo), np.array([float(elev)]), np.array([0.5]),
                                       np.array([0.9]), np.array([0.9]), np.array([0.9]), u_stand=np.array([us]))[0])
            if want is None:
                self.assertNotIn(g, (veg.G_LAUREL, veg.G_PINE_MARITIME, veg.G_PINE_WEST), where)
            else:
                self.assertEqual(veg.GROUPS[g], veg.GROUPS[want], where)
        self.assertEqual(int(veg.zones(np.array([-34.0]), np.array([-71.2]))[0]), veg.ZONE_MED, "Chile's matorral")
        self.assertEqual(int(veg.zones(np.array([-33.9]), np.array([18.5]))[0]), veg.ZONE_MED, "the Cape")
        self.assertEqual(int(veg.zones(np.array([34.05]), np.array([-118.25]))[0]), 0, "California: its own groups")

    def test_world_groups_in_the_mod(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        mod = os.path.join(root, "mod/beamng_earth/lua/ge/extensions/beamngEarth")
        lua = open(os.path.join(mod, "trees.lua"), encoding="utf-8").read()
        signs = open(os.path.join(mod, "townsigns.lua"), encoding="utf-8").read()
        for name in ("savanna", "laurel", "pine_maritime"):
            self.assertIn(f"\n  {name} = {{", lua, name)
            self.assertIn(f"{name} = true", signs, name)

    def test_no_mossy_forest_trees_in_the_open(self):
        g = veg.open_groups(np.array([veg.G_OAK, veg.G_DECIDUOUS, veg.G_SNAG_BROAD, veg.G_SNAG_CONIFER, veg.G_BEECH,
                                      veg.G_OAK], np.uint8), np.array([1, 1, 1, 1, 1, 0], bool))
        self.assertEqual(g.tolist(), [veg.G_DECIDUOUS_OPEN, veg.G_DECIDUOUS_OPEN, veg.G_DECIDUOUS_OPEN,
                                      veg.G_CONIFER_OPEN, veg.G_BEECH_OPEN, veg.G_OAK])
        md = FakeMapData()
        it = veg.Vegetation(md, None).cell(KEY)
        trees = it.take(it.kind == veg.KIND_TREE)
        rlon, rlat = md.lonlat(100, 400)
        d = np.hypot((trees.lon - rlon) * 85000, (trees.lat - rlat) * 111000)
        self.assertEqual(int(trees.group[np.argmin(d)]), veg.G_DECIDUOUS_OPEN, "the street tree")
        self.assertIn(int(trees.group[np.argmax(d)]), (veg.G_DECIDUOUS, veg.G_BIRCH, veg.G_OAK, veg.G_BEECH),
                      "the free crown, 300 m off: a forest broadleaf (36: by its species)")
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua")
        lua = open(path, encoding="utf-8").read()
        for g in veg.OPEN_FROM.values():            # every open twin the mod's catalog makes (or a group it has)
            name = veg.GROUPS[g]
            if name.endswith("_open"):
                self.assertIn(f"'{name[:-5]}'", lua[lua.index("M.FOREST_ONLY"):], name)

    def test_dead_wood_keeps_off_the_roads(self):
        m = np.zeros((200, 200), bool)
        m[100, :] = True                    # a road along row 100
        near, b = veg._coarse_near(m, 30)
        self.assertTrue(near[70 // b, 50 // b] and near[130 // b, 50 // b], "within 30 px: near")
        self.assertFalse(near[40 // b, 50 // b] or near[170 // b, 50 // b], "50+ px away: not")

    def test_others_and_the_count_unchanged(self):
        for grp in (veg.G_ROCK, veg.G_DECIDUOUS, veg.G_JUNIPER, veg.G_CREOSOTE, veg.G_VINE):
            self.assertEqual(self.g(43.3, 5.4, group=grp, uf=0.05, floor=True)[0], grp)
        n = 500
        rng = np.random.default_rng(3)
        lat, lon = rng.uniform(40, 60, n), rng.uniform(-5, 30, n)
        it = veg.Items(lon, lat, np.full(n, 1.0, np.float32), np.full(n, 0.6, np.float32),
                       np.full(n, veg.KIND_BUSH, np.uint8), np.full(n, veg.G_SHRUB, np.uint8),
                       rng.random(n).astype(np.float32), np.zeros(n, np.float32), np.ones(n, np.float32))
        a = veg.climate_shrubs(it, np.full(n, 10, np.uint8), np.ones(n, bool))
        b = veg.climate_shrubs(it, np.full(n, 10, np.uint8), np.ones(n, bool))
        self.assertEqual(len(a), n)
        np.testing.assert_array_equal(a.group, b.group)
        np.testing.assert_array_equal(a.lon, it.lon)
        self.assertEqual(int((a.group == veg.G_DEADWOOD).sum()), 0)
        self.assertGreater(int((a.group == veg.G_FERN).sum()), 0)

    def test_audio_and_the_mod_knows_them(self):
        self.assertNotIn(veg.G_DEADWOOD, veg.AUDIO_SOUND)
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(root, "mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua")
        lua = open(path, encoding="utf-8").read()
        for name in ("maquis", "shrub_north", "shrub_tropical", "fern", "deadwood"):
            self.assertIn(f"\n  {name} = {{", lua, name)


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

    def test_mediterranean(self):
        """The Mediterranean basin's Cs climates take Italy's trees (holm / cork oaks, olives, maritime pines,
        cypresses) for most broadleaves and conifers; Munich and California keep theirs."""
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 41.9, 12.5, 50, u=0.5), veg.G_MEDITERRANEAN, "Rome")
        # (39) off town a conifer stays one (the maritime and Aleppo pines, species_groups); in town Italy's trees
        self.assertEqual(self.g(veg.G_CONIFER, 15, 43.3, 5.37, 10, u=0.5), veg.G_CONIFER, "Marseille's pines")
        self.assertEqual(self.g(veg.G_CONIFER, 15, 43.3, 5.37, 50, u=0.5), veg.G_MEDITERRANEAN, "Marseille in town")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 41.9, 12.5, 10, u=0.95), veg.G_DECIDUOUS, "a share stays")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 48.14, 11.58, 50, u=0.1), veg.G_DECIDUOUS, "Munich")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 34.05, -118.25, 10, u=0.5), veg.G_OAK_WEST, "California")
        self.assertEqual(self.g(veg.G_SHRUB, 1, 41.9, 12.5, 10, u=0.1), veg.G_SHRUB, "bushes are not trees")
        # (35) by the ecoregions: North Africa's and the Adriatic's maquis in, Galicia's oak woods out
        self.assertEqual(self.g(veg.G_DECIDUOUS, 6, 36.8, 10.18, 50, u=0.5), veg.G_MEDITERRANEAN, "Tunis")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 14, 43.5, 16.44, 50, u=0.5), veg.G_MEDITERRANEAN, "Split")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 9, 42.88, -8.55, 10, u=0.5), veg.G_DECIDUOUS, "Santiago")

    def test_mediterranean_evergreen_share(self):
        """(39) off town the evergreen broadleaves by the ecoregion's inventory share (ECO_EVERGREEN)."""
        n = 4000
        u = (np.arange(n) + 0.5) / n
        for (lat, lon), lo, hi, where in (((38.5, -7.5), 0.75, 1.0, "the Alentejo's cork and holm oaks"),
                                          ((43.0, 12.0), 0.0, 0.2, "Umbria's deciduous oaks")):
            g = veg.regional(np.full(n, veg.G_DECIDUOUS, np.uint8), np.full(n, 10.0), np.full(n, lat),
                             np.full(n, lon), np.full(n, veg.WC["tree"], np.uint8), u)
            share = float(np.mean(g == veg.G_MEDITERRANEAN))
            self.assertTrue(lo <= share <= hi, f"{where}: {share:.2f}")

    def test_more_regional_trees(self):
        """2026-11-10 asset survey: Australia's eucalypts, temperate Europe's riverside poplars, the Mojave's creosote."""
        self.assertEqual(self.g(veg.G_DECIDUOUS, 15, -35.3, 149.1, 10, u=0.5), veg.G_EUCALYPTUS, "Canberra")
        self.assertEqual(self.g(veg.G_CONIFER, 20, -37.8, 145.0, 10, u=0.95), veg.G_CONIFER, "a share stays (pines)")
        self.assertEqual(self.g(veg.G_DECIDUOUS, 15, -12.4, 130.8, 10, u=0.5), veg.G_EUCALYPTUS, "Darwin's savanna")

        def rip(lat, lon, group=veg.G_DECIDUOUS, u=0.2, near=True):
            return int(veg.regional(np.array([group]), np.array([18.0]), np.array([lat]), np.array([lon]),
                                    np.array([10]), np.array([u]), riparian=np.array([near]))[0])
        self.assertEqual(rip(47.4, 0.7), veg.G_POPLAR, "the Loire's banks")
        self.assertEqual(rip(47.4, 0.7, near=False), veg.G_DECIDUOUS, "away from water")
        self.assertEqual(rip(47.4, 0.7, u=0.6), veg.G_DECIDUOUS, "a share only")
        self.assertEqual(rip(47.4, 0.7, group=veg.G_CONIFER), veg.G_CONIFER, "broadleaves only")
        self.assertEqual(rip(39.3, -76.6), veg.G_DECIDUOUS, "not in America")
        self.assertEqual(self.g(veg.G_SHRUB, 1, 35.0, -116.0, 20, u=0.3), veg.G_CREOSOTE, "the Mojave")
        self.assertEqual(self.g(veg.G_SHRUB, 1, 38.6, -109.5, 20, u=0.3), veg.G_DESERT_SHRUB, "Moab: cold desert")

    def test_beyond_north_america(self):
        """2026-10-15: Douglas firs in Punjab's fields; the Sahara's bushes were the East's."""
        self.assertLess(float(veg._conifer_share(30.9, 75.8)), 0.1, "Punjab: hardly any conifers")
        self.assertLess(float(veg._conifer_share(-1.3, 36.8)), 0.05, "Nairobi")
        self.assertGreaterEqual(float(veg._conifer_share(61.0, 10.0)), 0.6, "Norway")
        # (35) the leaf type map (leaftype.py) where it has forest, the coarse rule elsewhere
        self.assertGreater(float(veg._conifer_share(44.2, -0.8)), 0.8, "the Landes' pines")
        self.assertGreater(float(veg._conifer_share(53.6, 17.9)), 0.8, "Bory Tucholskie")
        self.assertLess(float(veg._conifer_share(49.2, 22.5)), 0.3, "the Bieszczady's beeches")
        self.assertGreater(float(veg._conifer_share(32.5, -83.5)), 0.6, "Georgia's loblolly pines (FIA: pine types 45 % statewide)")
        self.assertLess(float(veg._conifer_share(43.0, 12.0)), 0.2, "Umbria's oaks")
        self.assertGreaterEqual(float(veg._conifer_share(-38.4, 176.6)), float(veg._conifer_share_coarse(-38.4, 176.6)),
                                "Kaingaroa's radiata pines: no less than the coarse rule south of the tropics")
        self.assertEqual(float(veg._conifer_share(0.0, -30.0)), float(veg._conifer_share_coarse(0.0, -30.0)),
                         "no forest about: the coarse rule")
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


class CrownSpacingTest(unittest.TestCase):
    """(36) crown_spacing: the thinning's radius by the cell's leaf type and biome."""

    def test_by_leaf_type_and_biome(self):
        self.assertLess(veg.crown_spacing(62.0, 26.0), 0.6, "Finland's spruces and pines: narrow crowns")
        self.assertLess(veg.crown_spacing(53.6, 17.9), 0.6, "Bory Tucholskie's pines")
        self.assertGreater(veg.crown_spacing(49.99, 18.37), 0.85, "Silesia's oak-hornbeam woods: wide crowns")
        self.assertGreater(veg.crown_spacing(41.0, -77.5), 0.85, "Pennsylvania's hardwoods")
        self.assertAlmostEqual(veg.crown_spacing(-2.9, -60.0), veg.CROWN_SPACING_TROPICAL, places=2, msg="Amazonia")
        for lat, lon in ((62.0, 26.0), (49.99, 18.37), (-2.9, -60.0), (0.0, -30.0)):
            s = veg.crown_spacing(lat, lon)
            self.assertTrue(veg.CROWN_SPACING <= s <= max(veg.CROWN_SPACING_BROAD, veg.CROWN_SPACING_TROPICAL),
                            (lat, lon, s))


class ShareRulesTest(unittest.TestCase):
    """(38) the conifer share kept to the map, the beech belt, the pine woods' broadleaves."""

    def test_conifer_odds_keep_the_share(self):
        h = np.linspace(3.0, 30.0, 2000)
        for s in (0.05, 0.3, 0.7, 0.97):
            p = veg.conifer_odds(np.full(len(h), s), h)
            self.assertAlmostEqual(float(p.mean()), s, delta=0.04, msg=f"share {s}")
            self.assertLess(float(p[0]), float(p[-1]), "the tall trees more likely conifers")
        self.assertEqual(float(veg.conifer_odds(np.array([1.0]), np.array([4.0]))[0]), 1.0)
        self.assertEqual(float(veg.conifer_odds(np.array([0.0]), np.array([25.0]))[0]), 0.0)

    def _shares(self, lat, lon, k, beech_up=0.0, conifer=None):
        n = 1
        sh = veg.broad_shares(np.full(n, lat), np.full(n, lon), np.full(n, k), np.zeros(n, int),
                              np.full(n, beech_up), None if conifer is None else np.full(n, conifer))
        return sh[0]

    def test_beech_belt(self):
        z = np.array([250.0, 700.0])
        line = veg.spruce_line(49.15)
        up = np.clip((z - (line - veg.BEECH_BELT_M[0])) / (veg.BEECH_BELT_M[0] - veg.BEECH_BELT_M[1]), 0, 1)
        low, mid = (self._shares(49.15, 22.48, 26, u) for u in up)
        self.assertGreater(mid[2], 0.6, "the Bieszczady at 700 m: mostly beeches")
        self.assertLess(mid[1], 0.15, "hardly any oaks")
        self.assertGreater(low[1], 0.4, "the foothills' oaks")

    def test_east_asia_conical_conifers(self):
        import climate
        for (lat, lon), want_min, where in (((31.9, 131.3), veg.CONICAL_JAPAN, "Miyazaki's sugi"),
                                            ((24.5, 113.5), veg.CONICAL_CHINA, "Guangdong's Chinese fir")):
            n = 2000
            la, lo = np.full(n, lat), np.full(n, lon)
            u = (np.arange(n) + 0.5) / n
            g = veg.species_groups(np.full(n, veg.G_CONIFER, np.uint8), np.full(n, 14.0), la, lo,
                                   np.full(n, veg.WC["tree"]), climate.koppen_many(la, lo), np.full(n, 50.0),
                                   np.full(n, 0.5), u, np.full(n, 0.9), np.full(n, 0.9))
            spruce = float(np.mean(g == veg.G_SPRUCE))
            self.assertGreaterEqual(spruce, want_min - 0.01, where)

    def test_north_america_conifers(self):
        """(43) Canada's and New England's generic conifers mostly spruce-shaped (NFI: pines 15 % of the conifers), the
        Great Lakes' more pines; none left Douglas firs (the generic group)."""
        import climate
        for (lat, lon), lo, hi, where in (((54.0, -100.0), 0.75, 0.95, "Manitoba's boreal forest"),
                                          ((47.0, -92.0), 0.5, 0.8, "Minnesota's pinelands")):
            n = 2000
            la, lo_ = np.full(n, lat), np.full(n, lon)
            u = (np.arange(n) + 0.5) / n
            g = veg.species_groups(np.full(n, veg.G_CONIFER, np.uint8), np.full(n, 14.0), la, lo_,
                                   np.full(n, veg.WC["tree"]), climate.koppen_many(la, lo_), np.full(n, 300.0),
                                   np.full(n, 0.5), u, np.full(n, 0.9), np.full(n, 0.9))
            self.assertFalse((g == veg.G_CONIFER).any(), where)
            spruce = float(np.mean(g == veg.G_SPRUCE))
            self.assertTrue(lo <= spruce <= hi, f"{where}: {spruce:.2f}")

    def test_coast_palms(self):
        """(45) the tropics' shore: COAST_PALM_SHARE of its broadleaves palms; not inland, not a mangrove's, not a
        casuarina (pine_west), not outside the tropics' climates."""
        n = 4000
        u = (np.arange(n) + 0.5) / n
        tree, mang = veg.WC["tree"], veg.WC["mangrove"]

        def share(lat, lon, grp, lc, coast):
            g = veg.coast_palms(np.full(n, grp, np.uint8), np.full(n, lat), np.full(n, lon), np.full(n, lc, np.uint8),
                                np.full(n, coast), u)
            return float(np.mean(g == veg.G_PALM))
        self.assertAlmostEqual(share(-8.68, 115.26, veg.G_TROPICAL, tree, True), veg.COAST_PALM_SHARE, delta=0.01)
        self.assertEqual(share(-8.68, 115.26, veg.G_TROPICAL, tree, False), 0.0, "inland")
        self.assertEqual(share(-8.68, 115.26, veg.G_TROPICAL, mang, True), 0.0, "a mangrove")
        self.assertEqual(share(-8.68, 115.26, veg.G_PINE_WEST, tree, True), 0.0, "the casuarinas")
        self.assertEqual(share(-33.9, 18.4, veg.G_OAK, tree, True), 0.0, "Cape Town's shore")

    def test_pine_woods_broadleaves(self):
        mixed = self._shares(53.6, 17.9, 26, 0.0, 0.3)
        pine = self._shares(53.6, 17.9, 26, 0.0, 0.95)
        self.assertLess(pine[2], 0.02, "no beeches among Bory Tucholskie's pines")
        self.assertGreater(pine[0], mixed[0], "their birches")
        self.assertAlmostEqual(float(pine.sum()), 1.0, places=6)
        warm = self._shares(40.0, 15.0, 8, 0.0, 0.95)                # Csa: unchanged
        self.assertTrue(np.allclose(warm, self._shares(40.0, 15.0, 8, 0.0, 0.3)))


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


class ClumpTest(unittest.TestCase):
    """GitHub #1074: a forest square's trees get one far clump item; they are flagged (negative scale)."""

    def _items(self, lat, lon, group, lcls=10):
        n = len(lat)
        f32 = lambda v: np.full(n, v, np.float32)       # noqa: E731
        it = veg.Items(np.asarray(lon, float), np.asarray(lat, float), f32(20.0), f32(3.4),
                       np.full(n, veg.KIND_TREE, np.uint8), np.asarray(group, np.uint8),
                       np.linspace(0.1, 0.9, n).astype(np.float32), f32(0.0), f32(1.0))
        return it, np.full(n, lcls, np.uint8)

    def test_a_dense_square_becomes_one_clump(self):
        cell = veg.clump_cell(LAT)
        # a square's corner (Web Mercator, from the world's edge): three firs in it, one fir alone two squares away
        x0 = (np.floor((-8524000.0 + 20037508.342789244) / cell) + 0.5) * cell - 20037508.342789244
        y0 = (np.floor((4764000.0 + 20037508.342789244) / cell) + 0.5) * cell - 20037508.342789244
        xs = np.array([x0 - 2, x0 + 2, x0, x0 + 2 * cell])
        ys = np.array([y0, y0 + 1, y0 - 2, y0])
        lon, lat = veg._to4326(xs, ys)
        it, lc = self._items(lat, lon, [veg.G_CONIFER, veg.G_CONIFER, veg.G_DECIDUOUS, veg.G_CONIFER])
        out = veg.clumps(it, lc, LAT)
        self.assertEqual(len(out), 5)
        self.assertEqual(list(out.scale[:4] < 0), [True, True, True, False])
        self.assertEqual(int(out.group[4]), veg.G_CONIFER_CLUMP, "most of them firs")
        self.assertAlmostEqual(float(out.rank[4]), float(it.rank[:3].min()), places=6)
        cx, cy = veg._to3857(out.lon[4:], out.lat[4:])
        self.assertAlmostEqual(float(cx[0]), float(xs[:3].mean()), places=3)
        self.assertAlmostEqual(float(cy[0]), float(ys[:3].mean()), places=3)
        again = veg.clumps(it, lc, LAT)
        self.assertTrue(np.array_equal(again.lon, out.lon) and np.array_equal(again.yaw, out.yaw), "deterministic")

    def test_a_forest_road_through_a_square_keeps_its_clumps_off_it(self):
        # the user, 2026-11-16 ("take into consideration if the road is blocked by other buildings or trees"): a track
        # through a square of 20 m firs (a clump's radius 8 m), three each side 7-8.5 m off its line; the square's one
        # clump stood on its lanes
        cell = veg.clump_cell(LAT)
        k = 1.0 / np.cos(np.radians(LAT))               # Web Mercator m per ground m
        sx = np.floor((-8524000.0 + 20037508.342789244) / cell) * cell - 20037508.342789244
        sy = np.floor((4764000.0 + 20037508.342789244) / cell) * cell - 20037508.342789244
        mid = sy + cell / 2

        def firs(off):
            xs = sx + cell * np.array([0.1, 0.5, 0.9, 0.15, 0.55, 0.85])
            lon, lat = veg._to4326(xs, mid + k * np.asarray(off, float))
            return self._items(lat, lon, [veg.G_CONIFER] * 6)
        it, lc = firs([7.0, 8.5, 8.0, -8.0, -8.5, -7.0])
        before = veg.clumps(it, lc, LAT)
        self.assertEqual(len(before), 7, "no roads: one clump")
        _, cy0 = veg._to3857(before.lon[6:], before.lat[6:])
        self.assertLess(abs(float(cy0[0]) - mid) / k, 1.0, "on the track's line")
        # the track on a CHM-like grid round the square (1 Web Mercator m pixels)
        chm = SimpleNamespace(x0=sx - 100.0, y0=sy + cell + 100.0, dx=1.0, dy=1.0)
        a, b = veg._to4326(np.array([sx - 80.0, sx + cell + 80.0]), np.array([mid, mid]))
        road = Feats(2, [[(float(a[0]), float(b[0])), (float(a[1]), float(b[1]))]], ["track"])
        segs = veg._road_segs(road, (int(cell) + 200, int(cell) + 200), chm, veg.CLUMP_ROAD_MARGIN_DEFAULT_M,
                              margins=veg.CLUMP_ROAD_MARGIN_M)
        self.assertIsNotNone(segs)
        out = veg.clumps(it, lc, LAT, roads=veg.ClumpRoads([segs], chm))
        self.assertEqual(len(out), 8, "one clump each side of the track")
        self.assertTrue(np.all(out.scale[:6] < 0), "every fir a clump's")
        _, cy = veg._to3857(out.lon[6:], out.lat[6:])
        reach = veg.ROAD_HALF_DEFAULT_M + veg.CLUMP_ROAD_MARGIN_DEFAULT_M
        for c, rad in zip(cy, out.radius[6:]):
            self.assertGreaterEqual(abs(float(c) - mid) / k, reach + float(rad) - 0.05, "clear of the track")
        again = veg.clumps(it, lc, LAT, roads=veg.ClumpRoads([segs], chm))
        self.assertTrue(np.array_equal(again.lon, out.lon) and np.array_equal(again.lat, out.lat), "deterministic")
        # firs 3-5 m off the line: no clump fits beside the track (it would move 6 m): they stay single
        it, lc = firs([3.0, 5.0, 4.0, -4.0, -5.0, -3.0])
        out = veg.clumps(it, lc, LAT, roads=veg.ClumpRoads([segs], chm))
        self.assertEqual(len(out), 6)
        self.assertTrue(np.all(out.scale > 0), "shown far as single trees")
        # (Codex review of cff64d91) a clump moved off the track must stay in its own cell (the next cell's tiles never
        # show it) and off water and buildings (the tiles drop a clump in water): else its firs stay single
        it, lc = firs([7.0, 8.5, 8.0, -8.0, -8.5, -7.0])
        n = int(cell) + 200
        wet = np.zeros((n, n), bool)
        wet[: int(round(chm.y0 - mid)), :] = True          # water from the track's line up (north)
        out = veg.clumps(it, lc, LAT, roads=veg.ClumpRoads([segs], chm, blocked=wet))
        self.assertEqual(len(out), 7, "the north side's clump would stand in the water: left out")
        self.assertEqual(list(out.scale[:6] < 0), [False, False, False, True, True, True])
        own = (sx - 100.0, sy - 100.0, sx + 100.0, mid + k * 9.0)     # the cell ends 9 m north of the track
        out = veg.clumps(it, lc, LAT, roads=veg.ClumpRoads([segs], chm, own=own))
        self.assertEqual(len(out), 7, "the north side's clump would leave the cell: left out")
        self.assertEqual(list(out.scale[:6] < 0), [False, False, False, True, True, True])

    def test_street_trees_and_palms_stay_single(self):
        x0, y0 = -8524000.0, 4764000.0
        lon, lat = veg._to4326(np.array([x0, x0 + 1, x0 + 2]), np.array([y0, y0 + 1, y0]))
        it, lc = self._items(lat, lon, [veg.G_DECIDUOUS] * 3, lcls=veg.WC["built"])
        self.assertEqual(len(veg.clumps(it, lc, LAT)), 3)
        it, lc = self._items(lat, lon, [veg.G_PALM] * 3)
        self.assertEqual(len(veg.clumps(it, lc, LAT)), 3)

    def test_grid_fits_the_geotiles(self):
        import geotiles
        tile = 2 * np.pi * veg.R / (1 << geotiles.Z)
        for lat in (0.0, 45.0, 60.0, 80.0):
            k = tile / veg.clump_cell(lat)
            self.assertAlmostEqual(k, round(k), places=9)
            self.assertTrue(10 <= veg.clump_cell(lat) * np.cos(np.radians(lat)) <= 27)

    def test_clumps_beside_the_tile_cap(self):
        n = 30
        it = veg.Items(*(np.zeros(n) for _ in range(4)), np.zeros(n, np.uint8),
                       np.array([veg.G_CONIFER] * 20 + [veg.G_CONIFER_CLUMP] * 10, np.uint8),
                       np.linspace(0, 1, n).astype(np.float32)[::-1].copy(), np.zeros(n), np.ones(n))
        old = veg.TILE_ROWS_MAX, veg.CLUMP_ROWS_MAX
        veg.TILE_ROWS_MAX, veg.CLUMP_ROWS_MAX = 5, 3
        try:
            keep = veg._capped(it)
        finally:
            veg.TILE_ROWS_MAX, veg.CLUMP_ROWS_MAX = old
        self.assertEqual(int(np.sum(it.group[keep] == veg.G_CONIFER)), 5)
        self.assertEqual(int(np.sum(it.group[keep] == veg.G_CONIFER_CLUMP)), 3)
        self.assertTrue(np.all(np.diff(keep) > 0))


class WarmTest(unittest.TestCase):
    """warm_around runs on the event loop at every pose: it only keeps the place there. The warm thread checks the
    cells' files, each once, and builds the missing ones nearest first (up to 64 file checks a pose held the loop on a
    busy hard drive, GitHub #1054)."""

    def test_file_checks_on_the_warm_thread_once_per_cell(self):
        import collections
        import math
        import os
        import tempfile
        import threading
        import time
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            v = veg.Vegetation(None, cache_dir=tmp)
            v._path = lambda x, y: os.path.join(tmp, f"{x}_{y}.npz")
            built = []

            def load(x, y):
                built.append((x, y))
                open(v._path(x, y), "wb").close()
                return veg.Items(*(np.zeros(0) for _ in range(9)))
            v._load = load
            checks, threads, real = collections.Counter(), set(), os.path.exists

            def exists(p):
                if str(p).startswith(tmp):
                    checks[p] += 1
                    threads.add(threading.get_ident())
                return real(p)
            d = veg.WARM_M / 111320.0
            dl = d / math.cos(math.radians(LAT))
            cells = {(k.x, k.y) for k in mapcells.cells_for_bbox(LAT - d, LON - dl, LAT + d, LON + dl)}
            with mock.patch.object(veg.os.path, "exists", exists):
                for _ in range(10):                     # poses
                    v.warm_around(LAT, LON)
                for _ in range(1000):
                    if set(built) >= cells and v._warm_at is None:
                        break
                    time.sleep(0.01)
                time.sleep(0.2)                         # (the warm thread idle)
                self.assertEqual(set(built), cells)
                self.assertEqual(len(built), len(cells), "each built once")
                self.assertEqual(built[0], (KEY.x, KEY.y), "nearest first")
                self.assertNotIn(threading.get_ident(), threads, "warm_around checks no file itself")
                with v._lock:
                    v._mem.clear()                      # (out of memory: found on disk)
                n = sum(checks.values())
                for _ in range(20):
                    self.assertEqual(v._warm_cells(LAT, LON, veg.WARM_M), [])
                self.assertEqual(sum(checks.values()), n + len(cells), "each cell's file checked once in 20 places")


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


class VineyardTest(unittest.TestCase):
    def _field(self, e, n, cls="vineyard"):
        import mapgeom
        frame = geodesy.EnuFrame(44.9, -0.15)
        la, lo, _ = frame.enu_to_geodetic(np.asarray(e, float), np.asarray(n, float), np.zeros(len(e)))
        ring = np.stack([lo, la], 1)
        feats = mapgeom.build("land_use", np.array(["vy-1"]), [(mapgeom.POLYGON, [[ring]])])
        feats.attrs["class"] = np.array([cls], object)
        x, y = veg._to3857(ring[:, 0], ring[:, 1])
        pad, dx = 40.0, 2.0
        x0, y1 = float(x.min()) - pad, float(y.max()) + pad
        cols = int(np.ceil((float(x.max()) + pad - x0) / dx))
        rows = int(np.ceil((y1 - float(y.min()) + pad) / dx))
        return frame, feats, ring, Raster(np.zeros((rows, cols), np.uint8), x0, y1, dx), np.zeros((rows, cols), bool)

    def test_rows_along_the_long_sides_inside_the_field(self):
        # a parallelogram 200 m along x with 45-degree ends: its principal axis is skewed, its rows run along x
        frame, feats, ring, chm, clear = self._field([0, 200, 260, 60, 0], [0, 0, 60, 60, 0])
        it = veg.vineyard_items(feats, chm, clear, None)
        self.assertGreater(len(it), 60)
        self.assertTrue(np.all(it.group == veg.G_VINE))
        yaw = np.asarray(it.yaw) % 180.0
        self.assertTrue(np.all(np.minimum(yaw, 180.0 - yaw) < 0.5), "rows along the long sides")
        from polygon import Path
        x, y = veg._to3857(ring[:, 0], ring[:, 1])
        path = Path(np.column_stack([x, y]))
        cx, cy = veg._to3857(it.lon, it.lat)
        k = (veg.VINE_ALONG_M / 2 - 1.0) / np.cos(np.radians(44.9))
        for s in (-1, 1):
            self.assertTrue(np.all(path.contains_points(np.column_stack([cx + s * k, cy]))), "both ends inside")
        again = veg.vineyard_items(feats, chm, clear, None)
        self.assertTrue(np.array_equal(again.lon, it.lon) and np.array_equal(again.yaw, it.yaw), "deterministic")
        self.assertEqual(len(veg.vineyard_items(feats, chm, np.ones_like(clear), None)), 0, "roads / buildings: none")

    def test_only_vineyards(self):
        frame, feats, ring, chm, clear = self._field([0, 100, 100, 0, 0], [0, 0, 50, 50, 0], cls="orchard")
        self.assertEqual(len(veg.vineyard_items(feats, chm, clear, None)), 0)


class BaleTest(unittest.TestCase):
    def _fields(self, lat, cls, n=40, size=(400.0, 300.0)):
        """n fields side by side (one polygon each), size m."""
        import mapgeom
        frame = geodesy.EnuFrame(lat, 1.6)
        geoms = []
        for k in range(n):
            e = np.array([0, size[0], size[0], 0, 0.0]) + k * (size[0] + 20)
            nn = np.array([0, 0, size[1], size[1], 0.0])
            la, lo, _ = frame.enu_to_geodetic(e, nn, np.zeros(5))
            geoms.append((mapgeom.POLYGON, [[np.stack([lo, la], 1)]]))
        feats = mapgeom.build("land_use", np.array([f"f{lat}-{k}" for k in range(n)]), geoms)
        feats.attrs["class"] = np.array([cls] * n, object)
        return feats, frame

    def test_bales_on_a_share_of_the_fields(self):
        feats, frame = self._fields(48.3, "farmland")
        it = veg.bale_items(feats, None, None, None)
        self.assertTrue(np.all(it.group == veg.G_STRAW_N))
        e, n, _ = frame.geodetic_to_enu(it.lat, it.lon, np.zeros(len(it)))
        fields = set((np.asarray(e) // 420).astype(int))
        self.assertTrue(2 <= len(fields) <= 16, f"a share of the 40 fields: {len(fields)}")
        per = len(it) / max(len(fields), 1) / 12.0
        self.assertTrue(4 <= per <= 20, f"bales a hectare: {per:.1f}")
        x = np.asarray(e) % 420
        self.assertTrue(np.all((x > veg.BALE_EDGE_M - 0.5) & (x < 400 - veg.BALE_EDGE_M + 0.5)), "the headland clear")
        again = veg.bale_items(feats, None, None, None)
        self.assertTrue(np.array_equal(again.lon, it.lon), "deterministic")

    def test_by_class_hemisphere_and_size(self):
        self.assertTrue(np.all(veg.bale_items(self._fields(-35.0, "meadow")[0], None, None, None).group
                               == veg.G_SILAGE_S))
        self.assertEqual(len(veg.bale_items(self._fields(10.0, "farmland")[0], None, None, None)), 0, "not in the tropics")
        self.assertEqual(len(veg.bale_items(self._fields(48.3, "vineyard")[0], None, None, None)), 0)
        big = self._fields(48.3, "farmland", n=40, size=(2000.0, 1200.0))[0]
        self.assertEqual(len(veg.bale_items(big, None, None, None)), 0, "a polygon of many fields: none")


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


class TiedCapTest(unittest.TestCase):
    """At a tile's cap (_capped) the lowest ranks are kept, ties broken by the items' order (a stable sort): the same
    rows on every CPU. numpy's argpartition put equal ranks in its CPU's own order (x86-simd-sort's AVX2 or AVX-512
    code, the scalar code elsewhere): which of the tied items a tile kept depended on the machine (2026-11-18). The
    vegetation geotiles are kept by the code's hash (revisit kind "vegg": vegetation.py is in revisit.source_closure),
    so the new rule makes them again."""

    def test_ties_at_the_cut_keep_the_first(self):
        from unittest import mock
        rank = np.array([0.5, 0.2, 0.5, 0.1, 0.5, 0.5, 0.9, 0.5], np.float32)
        it = SimpleNamespace(group=np.zeros(len(rank), np.int64), rank=rank)
        with mock.patch.object(veg, "TILE_ROWS_MAX", 4):
            got = veg._capped(it)
        # 0.1 and 0.2, then the first two of the five tied at 0.5 (items 0 and 2), in index order
        self.assertEqual(got.tolist(), [0, 1, 2, 3])

    def test_clumps_capped_apart(self):
        from unittest import mock
        rank = np.full(6, 0.3, np.float32)
        group = np.array([0, veg.CLUMP_GROUPS[0], 0, veg.CLUMP_GROUPS[0], 0, veg.CLUMP_GROUPS[0]])
        it = SimpleNamespace(group=group, rank=rank)
        with mock.patch.object(veg, "TILE_ROWS_MAX", 2), mock.patch.object(veg, "CLUMP_ROWS_MAX", 1):
            self.assertEqual(veg._capped(it).tolist(), [0, 1, 2])

    def test_vegetation_in_the_tiles_code_key(self):
        import os
        import revisit
        self.assertIn("vegetation.py", revisit.source_closure(os.path.dirname(os.path.abspath(veg.__file__))))
