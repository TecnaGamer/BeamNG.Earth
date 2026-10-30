"""seacarve: the sea floor carved under ocean polygons, deterministic and continuous across the shoreline
and across cells; the far tiles' DEM sea sunk; which water-tile samples are sea; near()."""
import gc
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import mapcells  # noqa: E402
import seacarve  # noqa: E402

LAT0, LON0 = 39.28, -76.59
SEA_LAT = 39.27         # the "sea": every point south of this latitude in the test's area


class Water:
    """mapgeom.Features-like: one ocean polygon (the area south of SEA_LAT) and one lake beside it."""

    def __init__(self, polys):
        self.polys = polys
        self.attrs = {"subtype": np.array([s for s, _ in polys])}
        self.kind = np.full(len(polys), 3, np.uint8)

    def parts(self, i):
        return [[np.asarray(self.polys[i][1], np.float64)]]


class Cell:
    def __init__(self, water):
        self.layers = {"water": water}


class FakeMapData:
    release = "test"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def cell(self, key, layers=None, kind="demand"):
        self.calls += 1
        if self.fail:
            raise OSError("offline")
        ocean = [(-77.5, 38.5), (-75.5, 38.5), (-75.5, SEA_LAT), (-77.5, SEA_LAT), (-77.5, 38.5)]
        lake = [(-76.62, 39.30), (-76.61, 39.30), (-76.61, 39.31), (-76.62, 39.31), (-76.62, 39.30)]
        return Cell(Water([("ocean", ocean), ("lake", lake)]))


class FakeBathy:
    """netfetch.Fetcher-like: NOAA's export, a sea floor BATHY m deep everywhere (a float TIFF)."""
    BATHY = -10.0

    def __init__(self):
        self.calls = 0

    def get(self, url, kind="demand"):
        import io
        import re
        from types import SimpleNamespace
        from PIL import Image
        self.calls += 1
        n = int(re.search(r"size=(\d+),", url).group(1))
        buf = io.BytesIO()
        Image.fromarray(np.full((n, n), self.BATHY, np.float32), mode="F").save(buf, "TIFF")
        return SimpleNamespace(body=buf.getvalue())


class BathyMapData(FakeMapData):
    def __init__(self):
        super().__init__()
        self.fetcher = FakeBathy()


def south(m):
    """The point m metres south of (SEA_LAT, LON0) (negative: north, on land)."""
    return SEA_LAT - m / 111320.0


class SeaCarveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sc = seacarve.SeaCarver(FakeMapData(), self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_depth_from_the_shore(self):
        d = [float(self.sc.depth(np.array([south(m)]), np.array([LON0]))[0]) for m in (-20, -2, 2, 10, 20, 100)]
        self.assertEqual(d[0], 0.0, "land")
        self.assertLess(d[1], 0.3, "within a pixel of the shore: hardly any depth")
        self.assertTrue(0 < d[2] < d[3] < d[4], f"deeper away from the shore: {d}")
        self.assertAlmostEqual(d[3], seacarve.DEPTH_SLOPE * 10, delta=0.6)
        self.assertEqual(d[5], seacarve.DEPTH_CAP)

    def test_lakes_are_not_sea(self):
        self.assertEqual(float(self.sc.depth(np.array([39.305]), np.array([-76.615]))[0]), 0.0)

    def test_continuous_across_cells(self):
        """Along a line through a cell edge the depth changes no more than its slope allows."""
        x, _ = seacarve._merc(np.array([SEA_LAT]), np.array([LON0]))
        edge_px = (math.floor(x[0] / seacarve.CELL_PX) + 1) * seacarve.CELL_PX
        edge_lon = edge_px / (256 << seacarve.PX_ZOOM) * 360.0 - 180.0
        lons = edge_lon + np.linspace(-0.0003, 0.0003, 61)
        d = self.sc.depth(np.full(61, south(12)), lons)
        self.assertLess(float(np.abs(np.diff(d)).max()), 0.05, d)

    def test_carve(self):
        lat = np.array([south(-50), south(50)])
        lon = np.full(2, LON0)
        z = np.array([3.21, 0.4])
        out = self.sc.carve(lat, lon, z)
        self.assertEqual(out[0], 3.21, "land: the DEM bit for bit")
        self.assertAlmostEqual(out[1], -seacarve.DEPTH_CAP, places=5)

    def test_cached_on_disk_and_deterministic(self):
        lat = np.array([south(7.3), south(31)])
        lon = np.array([LON0, LON0 + 0.001])
        a = seacarve.SeaCarver(BathyMapData(), self.tmp.name).depth(lat, lon)
        md = BathyMapData()
        b = seacarve.SeaCarver(md, self.tmp.name).depth(lat, lon)
        self.assertEqual(md.calls + md.fetcher.calls, 0, "read back from disk")
        np.testing.assert_array_equal(a, b)
        with tempfile.TemporaryDirectory() as other:
            np.testing.assert_array_equal(a, seacarve.SeaCarver(BathyMapData(), other).depth(lat, lon))

    def test_raw_copy_and_older_cells(self):
        """Read back memory-mapped (the raw copy) and from a cell cached before the raw copies (converted): the
        same water as the build."""
        import glob
        lat = np.array([south(7.3), south(31), south(-20)])
        lon = np.array([LON0, LON0 + 0.001, LON0])
        a = seacarve.SeaCarver(BathyMapData(), self.tmp.name).water(lat, lon)
        raws = glob.glob(os.path.join(self.tmp.name, "**", "*.raw.npy"), recursive=True)
        self.assertTrue(raws)
        sc = seacarve.SeaCarver(BathyMapData(), self.tmp.name)
        b = sc.water(lat, lon)
        mapped = next(r for r, _, _ in sc._mem.values() if r is not None)[0]
        self.assertFalse(mapped.flags.owndata or mapped.flags.writeable, "a view of the raw copy's mapping")
        del sc, mapped
        gc.collect()  # the mappings closed: Windows removes no mapped file
        for r in raws:
            os.remove(r)
        c = seacarve.SeaCarver(BathyMapData(), self.tmp.name).water(lat, lon)
        self.assertTrue(glob.glob(os.path.join(self.tmp.name, "**", "*.raw.npy"), recursive=True))
        for x in (b, c):
            np.testing.assert_array_equal(a[0], x[0])
            np.testing.assert_array_equal(a[1], x[1])

    def test_without_bathymetry_not_cached(self):
        lat, lon = np.array([south(50)]), np.array([LON0])
        self.sc.depth(lat, lon)
        md = FakeMapData()
        seacarve.SeaCarver(md, self.tmp.name).depth(lat, lon)
        self.assertGreater(md.calls, 0, "built again (the bathymetry may come later)")

    def test_bathymetry_depths(self):
        sc = seacarve.SeaCarver(BathyMapData(), None)
        d = [float(sc.depth(np.array([south(m)]), np.array([LON0]))[0]) for m in (-20, 3, 10, 100)]
        self.assertEqual(d[0], 0.0)
        self.assertAlmostEqual(d[2], seacarve.SHORE_SLOPE * 10, delta=1.0)
        self.assertAlmostEqual(d[3], -FakeBathy.BATHY, places=1)
        self.assertTrue(0 < d[1] < d[2] < d[3])

    def test_no_sea_floor_in_the_mosaic_generic_floor(self):
        """The mosaic's land DEM over a harbour (no depths): the generic floor, not BATHY_MIN (Sydney Harbour)."""
        md = BathyMapData()
        md.fetcher.BATHY = 3.0
        sc = seacarve.SeaCarver(md, None)
        self.assertAlmostEqual(float(sc.depth(np.array([south(100)]), np.array([LON0]))[0]), seacarve.DEPTH_CAP,
                               places=1)

    def test_harbour_water_is_sea(self):
        """natural=water + water=harbour (Port Jackson) is the sea, not a lake (its tags in Overture's attrs)."""
        class Tagged(FakeMapData):
            def cell(self, key, layers=None, kind="demand"):
                harbour = [(-77.5, 38.5), (-75.5, 38.5), (-75.5, SEA_LAT), (-77.5, SEA_LAT), (-77.5, 38.5)]
                w = Water([("water", harbour)])
                w.attrs["tags"] = np.array(['{"natural":"water","water":"harbour"}'], object)
                return Cell(w)
        sc = seacarve.SeaCarver(Tagged(), None)
        d, lv = sc.water(np.array([south(100)]), np.array([LON0]))
        self.assertEqual(float(lv[0]), seacarve.SEA_E)
        self.assertGreater(float(d[0]), 1.0)

    def test_a_lakes_harbour_is_not_the_sea(self):
        """GitHub #45: Chicago's harbours on Lake Michigan (176 m), tagged water=harbour, were carved as the sea at 0 m:
        holes 180 m deep. Near sea level (Port Jackson's DEM reads 2-6 m) it is still the sea."""
        class Tagged(FakeMapData):
            def cell(self, key, layers=None, kind="demand"):
                harbour = [(-77.5, 38.5), (-75.5, 38.5), (-75.5, SEA_LAT), (-77.5, SEA_LAT), (-77.5, 38.5)]
                w = LakeWater([("water", harbour)], ["h"])
                w.attrs["tags"] = np.array(['{"natural":"water","water":"harbour"}'], object)
                return Cell(w)

        class Dem:
            def __init__(self, z):
                self.z = z

            def sample(self, lat, lon, zoom, progress=None):
                return np.full(np.shape(lat), self.z)
        p = (np.array([south(100)]), np.array([LON0]))
        high = seacarve.SeaCarver(Tagged(), None, dem=Dem(176.0))
        self.assertFalse(bool(high.sea_at(Dem(176.0), *p, 512.0)[0]))
        self.assertAlmostEqual(float(high.lake_at(*p)[0]), 176.0, delta=seacarve.SHORE_DROP_M + 0.05)   # a lake at its level
        low = seacarve.SeaCarver(Tagged(), None, dem=Dem(4.0))
        self.assertTrue(bool(low.sea_at(Dem(4.0), *p, 512.0)[0]))

    def test_without_waiting(self):
        """Cells not at hand: the DEM band now, the cell built in the background for the next ask."""
        import time

        class Dem:
            def sample(self, lat, lon, zoom, progress=None):
                return np.full(np.shape(lat), 25.0)         # land by the band everywhere
        sc = seacarve.SeaCarver(BathyMapData(), self.tmp.name)
        p = (np.array([south(100)]), np.array([LON0]))
        self.assertFalse(sc.sea_at(Dem(), *p, 512.0, wait=False)[0], "the band (cell not at hand)")
        for _ in range(100):
            if sc.ready(*map(int, np.floor(np.array(seacarve._merc(*p)) / seacarve.CELL_PX).ravel())):
                break
            time.sleep(0.05)
        self.assertTrue(sc.sea_at(Dem(), *p, 512.0, wait=False)[0], "the polygons once built")

    def test_no_map_data_no_sea(self):
        sc = seacarve.SeaCarver(FakeMapData(fail=True), None)
        self.assertFalse(sc.depth(np.array([south(100)]), np.array([LON0])).any())

    def test_near(self):
        self.assertTrue(self.sc.near(south(-1000), LON0, 2000))
        self.assertFalse(self.sc.near(south(-5000), LON0, 2000))


LAKE_LAT, LAKE_LON = 39.30, -76.62        # a 600 m x 600 m lake (north of the test's sea)
D = 600 / 111320.0


class LakeWater(Water):
    def __init__(self, polys, ids):
        super().__init__(polys)
        self.ids = np.array(ids)
        n = len(polys)
        self.attrs["class"] = np.array([""] * n, object)
        self.attrs["is_intermittent"] = np.zeros(n, bool)


class LakeMapData(FakeMapData):
    def cell(self, key, layers=None, kind="demand"):
        self.calls += 1
        sq = lambda la, lo, d: [(lo, la), (lo + d, la), (lo + d, la + d), (lo, la + d), (lo, la)]   # noqa: E731
        return Cell(LakeWater([("lake", sq(LAKE_LAT, LAKE_LON, D)),
                               ("pond", sq(LAKE_LAT, LAKE_LON + 2 * D, 0.00005)),     # ~5 m: too small
                               ("lake", sq(LAKE_LAT + 2 * D, LAKE_LON, D))],          # not flat
                              ["a", "b", "c"]))


class LakeDem:
    """50 m inside the first lake, sloping across the third, 60 m around (a valley at 30 m east of x+D)."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        e = np.full(lat.shape, 60.0)
        e[(lon > LAKE_LON + 1.2 * D) & (lat < LAKE_LAT + D)] = 30.0
        in1 = (lat > LAKE_LAT) & (lat < LAKE_LAT + D) & (lon > LAKE_LON) & (lon < LAKE_LON + D)
        e[in1] = 50.0
        island = (np.abs(lat - LAKE_LAT - 0.75 * D) < 0.08 * D) & (np.abs(lon - LAKE_LON - 0.75 * D) < 0.08 * D)
        e[island] = 56.0                # an island inside the outline
        in3 = (lat > LAKE_LAT + 2 * D) & (lat < LAKE_LAT + 3 * D) & (lon > LAKE_LON) & (lon < LAKE_LON + D)
        e[in3] = 40.0 + (lat[in3] - LAKE_LAT - 2 * D) / D * 20.0
        return e


class LakeTest(unittest.TestCase):
    def setUp(self):
        self.sc = seacarve.SeaCarver(LakeMapData(), None, dem=LakeDem())

    def test_level_carve_and_filters(self):
        mid = (np.array([LAKE_LAT + D / 2]), np.array([LAKE_LON + D / 2]))
        self.assertEqual(float(self.sc.lake_at(*mid)[0]), 50.0)
        z = self.sc.carve(*mid, np.array([50.0]))
        self.assertAlmostEqual(float(z[0]), 50.0 - seacarve.LAKE_DEPTH_CAP, places=4)
        land = (np.array([LAKE_LAT - D / 2]), np.array([LAKE_LON + D / 2]))
        self.assertEqual(float(self.sc.carve(*land, np.array([61.25]))[0]), 61.25)
        self.assertTrue(np.isnan(self.sc.lake_at(*land)[0]))
        steep = (np.array([LAKE_LAT + 2.5 * D]), np.array([LAKE_LON + D / 2]))
        self.assertTrue(np.isnan(self.sc.lake_at(*steep)[0]), "not flat: not a lake")
        island = (np.array([LAKE_LAT + 0.75 * D]), np.array([LAKE_LON + 0.75 * D]))
        self.assertTrue(np.isnan(self.sc.lake_at(*island)[0]), "an island in the outline stays dry")
        self.assertEqual(float(self.sc.carve(*island, np.array([56.0]))[0]), 56.0, "and is not carved")
        tiny = (np.array([LAKE_LAT + 0.00002]), np.array([LAKE_LON + 2 * D + 0.00002]))
        self.assertTrue(np.isnan(self.sc.lake_at(*tiny)[0]), "too small")
        sea = (np.array([south(100)]), np.array([LON0]))
        self.assertTrue(np.isnan(self.sc.lake_at(*sea)[0]), "the sea is not a lake")

    def test_tiles_never_over_the_valley(self):
        import seatiles
        f = geodesy.EnuFrame(LAKE_LAT + D / 2, LAKE_LON + D / 2)
        dem = LakeDem()
        tiles = seatiles.plan_lakes(f, 0.0, 0.0, self.sc.lake_at, lambda la, lo: dem.sample(la, lo, 15))
        self.assertTrue(tiles)
        valley_x = float(f.geodetic_to_enu(LAKE_LAT, LAKE_LON + 1.2 * D, 0)[0])
        for t in tiles:
            self.assertLessEqual(t.x + t.size / 2, valley_x + 1e-6, t)
            la, lo, _ = f.enu_to_geodetic(t.x, t.y, 0.0)
            self.assertAlmostEqual(t.z, float(f.geodetic_to_enu(la, lo, 50.0)[2]), places=5)


CAN_LAT, CAN_LON = 39.36, -76.62          # a 40 m wide town canal the DEM does not see (flat 2 m all round),
CW, CL = 40 / 111320.0, 0.006               # and a polder's lake 2 m under the sea in ground 1.5 m under it


class CanalMapData(FakeMapData):
    def cell(self, key, layers=None, kind="demand"):
        self.calls += 1
        rect = lambda la, lo, h, w: [(lo, la), (lo + w, la), (lo + w, la + h), (lo, la + h), (lo, la)]   # noqa: E731
        return Cell(LakeWater([("water", rect(CAN_LAT, CAN_LON, CW, CL)),
                               ("lake", rect(CAN_LAT + 10 * CW, CAN_LON, 5 * CW, CL)),
                               ("lake", rect(CAN_LAT + 20 * CW, CAN_LON, 5 * CW, CL))], ["k", "p", "z"]))


class CanalDem:
    def sample(self, lat, lon, zoom, progress=None):
        lat = np.asarray(lat, np.float64)
        polder = (lat > CAN_LAT + 6 * CW) & (lat < CAN_LAT + 18 * CW)
        inside = (lat > CAN_LAT + 10 * CW) & (lat < CAN_LAT + 15 * CW)
        lon = np.asarray(lon, np.float64)
        at_sea_level = (lat > CAN_LAT + 20 * CW) & (lat < CAN_LAT + 25 * CW) & (lon > CAN_LON) & (lon < CAN_LON + CL)
        return np.where(polder, np.where(inside, -2.0, -1.5), np.where(at_sea_level, 0.1, 2.0))


class ShoreTest(unittest.TestCase):
    def test_level_no_higher_than_the_ground_round_it(self):
        # GitHub #8 / #53: a canal's water at street level flooded the streets (Petten, Maarssen)
        sc = seacarve.SeaCarver(CanalMapData(), None, dem=CanalDem())
        mid = (np.array([CAN_LAT + CW / 2]), np.array([CAN_LON + CL / 2]))
        self.assertAlmostEqual(float(sc.lake_at(*mid)[0]), 2.0 - seacarve.SHORE_DROP_M, places=4)

    def test_an_inland_lake_at_sea_level_is_not_the_sea(self):
        sc = seacarve.SeaCarver(CanalMapData(), None, dem=CanalDem())
        mid = (np.array([CAN_LAT + 22.5 * CW]), np.array([CAN_LON + CL / 2]))
        self.assertAlmostEqual(float(sc.lake_at(*mid)[0]), 0.1, places=4)
        self.assertFalse(bool(sc.sea_at(CanalDem(), *mid, 512.0)[0]))

    def test_a_lake_under_the_sea_stays_a_lake(self):
        sc = seacarve.SeaCarver(CanalMapData(), None, dem=CanalDem())
        mid = (np.array([CAN_LAT + 12.5 * CW]), np.array([CAN_LON + CL / 2]))
        self.assertAlmostEqual(float(sc.lake_at(*mid)[0]), -2.0, places=4)
        self.assertFalse(bool(sc.sea_at(CanalDem(), *mid, 512.0)[0]), "not the sea (a tile at 0 m over the polder)")


RIV_LAT, RIV_LON = 39.33, -76.62           # a 100 m wide, 1.2 km long river falling west to east, then a
W = 100 / 111320.0                          # tidal one at sea level south of it
L = 1200 / (111320.0 * math.cos(math.radians(39.33)))


class RiverMapData(FakeMapData):
    ocean = True        # the sea just south of the tidal river (False: an inland river at sea level)

    def cell(self, key, layers=None, kind="demand"):
        self.calls += 1
        rect = lambda la, lo: [(lo, la), (lo + L, la), (lo + L, la + W), (lo, la + W), (lo, la)]   # noqa: E731
        polys = [("river", rect(RIV_LAT, RIV_LON)), ("river", rect(RIV_LAT - 3 * W, RIV_LON))]
        if self.ocean:
            polys.append(("ocean", rect(RIV_LAT - 4 * W, RIV_LON)))
        return Cell(LakeWater(polys, ["r", "t", "o"][:len(polys)]))


class RiverDem:
    """The upper river's surface at 30 m falling 0.5 m per 100 m eastwards, banks 5 m higher; the tidal
    river at 0.1 m."""

    def sample(self, lat, lon, zoom, progress=None):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        fall = (lon - RIV_LON) / L * 6.0
        upper = (lat > RIV_LAT) & (lat < RIV_LAT + W)
        tidal = (lat > RIV_LAT - 3 * W) & (lat < RIV_LAT - 2 * W)
        return np.where(upper, 30.0 - fall, np.where(tidal, 0.1, 35.0 - fall))


class RiverTest(unittest.TestCase):
    def setUp(self):
        self.sc = seacarve.SeaCarver(RiverMapData(), None, dem=RiverDem())

    def test_level_follows_the_fall(self):
        la = np.full(5, RIV_LAT + W / 2)
        lo = RIV_LON + np.array([0.1, 0.3, 0.5, 0.7, 0.9]) * L
        d, lv = self.sc.water(la, lo)
        self.assertTrue(np.all(np.isfinite(lv)) and np.all(d > 0), "the bed carved below the river's level")
        np.testing.assert_allclose(lv, 30.0 - np.array([0.1, 0.3, 0.5, 0.7, 0.9]) * 6.0, atol=0.25)
        self.assertTrue(np.all(np.diff(lv) < 0), "falls downstream")
        self.assertTrue(np.all(np.isnan(self.sc.lake_at(la, lo))), "a river is not a lake (no flat tiles)")

    def test_river_edges_are_not_lakes(self):
        """Just past a river's edge (the depth is bilinear) no lookup reads a lake: tiles would sit there."""
        lo = RIV_LON + np.linspace(0.1, 0.9, 40) * L
        for off in (-2.0, -0.5, 0.5, 2.0):          # metres outside the north and south edges
            for la in (RIV_LAT + W + off / 111320.0, RIV_LAT - off / 111320.0):
                self.assertTrue(np.all(np.isnan(self.sc.lake_at(np.full(40, la), lo))), (off, la))

    def test_tidal_river_is_sea(self):
        p = (np.array([RIV_LAT - 2.5 * W]), np.array([RIV_LON + L / 2]))
        d, lv = self.sc.water(*p)
        self.assertGreater(float(d[0]), 0.0)
        self.assertEqual(float(lv[0]), seacarve.SEA_E)
        self.assertTrue(np.isnan(self.sc.lake_at(*p)[0]))

    def test_inland_river_at_sea_level_is_not_sea(self):
        # GitHub #8: the Vecht at Maarssen (~0 m, 50 km from the coast) was "tidal", the sea's tiles over the town
        md = RiverMapData()
        md.ocean = False
        sc = seacarve.SeaCarver(md, None, dem=RiverDem())
        p = (np.array([RIV_LAT - 2.5 * W]), np.array([RIV_LON + L / 2]))
        d, lv = sc.water(*p)
        self.assertGreater(float(d[0]), 0.0)
        self.assertNotEqual(float(lv[0]), seacarve.SEA_E)
        self.assertFalse(bool(sc.sea_at(RiverDem(), *p, 512.0)[0]))

    def test_no_tiles_for_rivers(self):
        import seatiles
        f = geodesy.EnuFrame(RIV_LAT + W / 2, RIV_LON + L / 2)
        dem = RiverDem()
        self.assertEqual(seatiles.plan_lakes(f, 0.0, 0.0, self.sc.lake_at, lambda la, lo: dem.sample(la, lo, 15)),
                         [], "rivers slope: River objects (riverlines.py), not flat tiles")


class SinkTest(unittest.TestCase):
    def test_dem_sea_sunk_land_kept(self):
        e = np.array([0.0, 0.25, -0.4, 0.5, 40.0, -3.0])
        out = seacarve.sink(e)
        np.testing.assert_array_equal(out[:3], [-seacarve.SINK_M] * 3)
        np.testing.assert_array_equal(out[3:], e[3:])

    def test_sea_dem(self):
        class Flat:
            def sample(self, lat, lon, zoom, progress=None):
                return np.zeros(np.shape(lat))
        sc = seacarve.SeaCarver(FakeMapData(), None)
        lat, lon = np.array([south(-50), south(50)]), np.full(2, LON0)
        np.testing.assert_allclose(seacarve.SeaDem(Flat(), sc).sample(lat, lon, 15), [0.0, -seacarve.DEPTH_CAP])
        np.testing.assert_allclose(seacarve.SeaDem(Flat()).sample(lat, lon, 15), [-seacarve.SINK_M] * 2)


class SeaAtTest(unittest.TestCase):
    def test_polygons_near_dem_band_far(self):
        class Dem:
            def sample(self, lat, lon, zoom, progress=None):
                return np.where(np.asarray(lon) > LON0, 0.1, 25.0)     # east: sea level, west: land
        sc = seacarve.SeaCarver(FakeMapData(), None)
        lat = np.array([south(-500), south(-500), south(500)])
        lon = np.array([LON0 + 0.01, LON0 - 0.01, LON0 - 0.01])
        self.assertEqual(list(sc.sea_at(Dem(), lat, lon, 512.0)), [False, False, True], "small: the polygons")
        self.assertEqual(list(sc.sea_at(Dem(), lat, lon, 4096.0)), [True, False, False], "large: the DEM band")



class PlanAgainTest(unittest.TestCase):
    """earthd: the water planned again after SEA_CHECK_S only when the last plan lacked map data."""

    def conn(self):
        import types
        import earthd
        c = earthd.Connection.__new__(earthd.Connection)
        c.s = types.SimpleNamespace(anchors={1: types.SimpleNamespace(frame=None)}, sea=None, dem=None)
        c.sea_busy, c.started = False, []

        def spawn(coro):
            c.started.append(1)
            coro.close()
        c.spawn = spawn
        return c

    def test_complete_plan_kept(self):
        import time
        import earthd
        import seatiles
        sq = seatiles.square_of(10.0, 10.0)
        old = time.monotonic() - earthd.SEA_CHECK_S - 1
        c = self.conn()
        c.sea_checked = (1, sq, old, True)
        c.sea_tiles(1, 10.0, 10.0)
        self.assertEqual(c.started, [], "complete: not planned again")
        c.sea_checked = (1, sq, old, False)
        c.sea_tiles(1, 10.0, 10.0)
        self.assertEqual(c.started, [1], "incomplete: planned again")
        c = self.conn()
        c.sea_checked = (1, sq, time.monotonic(), True)
        c.sea_tiles(1, 10.0 + seatiles.PLAN_SQUARE, 10.0)
        self.assertEqual(c.started, [1], "another square: planned")


class UndergroundLinesTest(unittest.TestCase):
    """The beds of rivers piped underground are not carved (riverlines.underground)."""

    def test_no_bed_along_a_piped_river(self):
        import riverlines
        carved = []

        class Lines:
            def __init__(self):
                self.ids = np.array(["open", "piped"])
                self.kind = np.full(2, 2, np.uint8)
                self.attrs = {"subtype": np.array(["river", "river"]), "class": np.array(["river", "river"]),
                              "is_intermittent": np.zeros(2, bool),
                              "tags": np.array(['{"waterway":"river"}', '{"waterway":"river","tunnel":"yes"}'],
                                               object)}

            def parts(self, i):
                return [[np.array([(RIV_LON, RIV_LAT + i * 0.001), (RIV_LON + L, RIV_LAT + i * 0.001)])]]

        sc = seacarve.SeaCarver(RiverMapData(), None, dem=RiverDem())
        sc._water = lambda key: Lines()
        sc._line_runs = lambda pts, n, px: carved.append(len(pts)) or []     # (one call a ring of a line kept)
        n = 8
        sc._carve_lines(0, 0, 0.0, 0.0, n, 1.0, np.zeros((n, n)), np.full((n, n), np.nan), np.zeros((n, n), bool))
        self.assertEqual(len(carved), 1, "the open river once (one cell's lines seen once), the piped one never")
        self.assertTrue(riverlines.underground(Lines(), 1))


if __name__ == "__main__":
    unittest.main()
