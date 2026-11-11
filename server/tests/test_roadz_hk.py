"""Hong Kong's road heights (roadz "hk", roadzhk.py; GitHub #1078): the LiDAR sheet reader, the Transport Department
lines' levels / route numbers / names, ground from the DTM and decks from the DSM (none under a higher deck), the
source's bounds and off switch, the sheets kept on disk, and plans with its heights kept out of every share. Synthetic
fixtures only: a generated tiled TIFF in a zip and hand-written ArcGIS answers."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import geodesy  # noqa: E402
import hklidar  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import roadz  # noqa: E402
import roadzhk  # noqa: E402
from hklidarfixtures import tiff_zip  # noqa: E402

NATIVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= roadz.NATIVE
KEY = mapcells.cell_of(22.383, 114.208)         # Shek Mun, Sha Tin
LAT0, LON0 = KEY.center()
X0, Y0 = 840000.0, 826000.0                     # the fixture's HK1980 Grid origin


def feature(oid, level, ref, name, xy):
    xy = np.asarray(xy, np.float64)
    ll = np.c_[LON0 + xy[:, 0] / 1e5, LAT0 + xy[:, 1] / 1.1e5]
    attrs = {"OBJECTID": oid, "ELEVATION": level, "ROUTE_NUM": ref, "STREET_ENAME": name}
    return ({"attributes": attrs, "geometry": {"paths": [ll.tolist()]}},
            {"attributes": {"OBJECTID": oid}, "geometry": {"paths": [(xy + [X0, Y0]).tolist()]}})


# a ground road, a deck crossing over it, a higher deck crossing over that, a tunnel (metres east / north of X0, Y0)
FEATURES = [feature(1, 0, None, "SHA TIN ROAD", [[20, 50], [180, 50]]),
            feature(2, 1, 2, "-99", [[100, 10], [100, 190]]),
            feature(3, 2, 2.0, "TATE'S CAIRN HIGHWAY", [[60, 121], [140, 121]]),
            feature(4, -1, None, "-99", [[20, 150], [80, 150]])]


def grids():
    """(DTM, DSM) of the fixture, 1 m pixels over 200 x 200 m: the ground a plane, the decks 20 m and 30 m, a bus 4 m
    tall on the lower deck's centreline, a hole in the DTM."""
    x = np.arange(200) + 0.5
    dtm = np.tile((10.0 + 0.01 * x).astype(np.float32), (200, 1))
    north = Y0 + 200 - (np.arange(200) + 0.5)[:, None] - Y0
    dsm = dtm.copy()
    dsm[:, 96:104] = 20.0
    dsm[(north[:, 0] >= 117) & (north[:, 0] <= 125), 56:144] = 30.0
    dsm[(north[:, 0] >= 160) & (north[:, 0] <= 170), 99:101] = 24.0
    dtm[(north[:, 0] >= 45) & (north[:, 0] <= 55), 150:160] = -9999.0
    return dtm, dsm


class FakeFetcher:
    """The CSDI services for the fixture: the TD lines, the sheet index (two sheets a layer, side by side) and the
    sheets. Counts the sheet downloads."""

    def __init__(self):
        dtm, dsm = grids()
        self.zips = {}
        self.sheets = {0: [], 1: []}
        for layer, kind, a in ((0, "dtm", dtm), (1, "dsm", dsm)):
            for name, c0 in (("7SE12A", 0), ("7SE12B", 100)):
                url = f"https://bulkdata.csdi.gov.hk/cedd/lidar/2020{kind}/{name}(e839n827,e840n827).zip"
                self.zips[urllib.parse.quote(urllib.parse.urlsplit(url).path)] = tiff_zip(
                    a[:, c0:c0 + 100], X0 + c0, Y0 + 200)
                ring = [[X0 + c0, Y0], [X0 + c0 + 100, Y0], [X0 + c0 + 100, Y0 + 200], [X0 + c0, Y0 + 200]]
                self.sheets[layer].append({"attributes": {"TILE_NAME": name, "URL": url},
                                           "geometry": {"rings": [ring]}})
        self.downloads = []

    def get(self, url, **_kw):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.hostname == "bulkdata.csdi.gov.hk":
            self.downloads.append(u.path)
            body = self.zips[u.path]
        elif "td_rcd" in u.path:
            if "objectIds" in q:
                ids = {int(v) for v in q["objectIds"].split(",")}
                d = {"features": [g for f, g in FEATURES if f["attributes"]["OBJECTID"] in ids]}
            else:
                d = {"features": [f for f, _g in FEATURES]}
        else:
            d = {"features": self.sheets[int(u.path.rsplit("/", 2)[-2])]}
        return SimpleNamespace(body=body if u.hostname == "bulkdata.csdi.gov.hk" else json.dumps(d).encode())


def by_name(ln, name):
    j = [i for i in range(len(ln)) if str(ln.name[i]) == name]
    return [(ln.lonlat[ln.off[i]:ln.off[i + 1]], ln.z[ln.off[i]:ln.off[i + 1]], int(ln.level[i]), str(ln.ref[i]))
            for i in j]


def east_north(lonlat):
    return (lonlat[:, 0] - LON0) * 1e5, (lonlat[:, 1] - LAT0) * 1.1e5


class LinesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.fetcher = FakeFetcher()
        self.ln = roadz.RoadZ(self.dir, self.fetcher, None).lines("hk", KEY)

    def test_levels_route_numbers_and_names(self):
        rows = {(lv, ref, nm) for nm in ("SHA TIN ROAD", "", "TATE'S CAIRN HIGHWAY")
                for _ll, _z, lv, ref in by_name(self.ln, nm)}
        self.assertEqual(rows, {(0, "", "SHA TIN ROAD"), (1, "2", ""), (2, "2", "TATE'S CAIRN HIGHWAY"), (-1, "", "")})

    def test_ground_from_the_dtm_under_a_deck_too(self):
        (ll, z, _lv, _ref), = by_name(self.ln, "SHA TIN ROAD")
        e, _n = east_north(ll)
        self.assertLessEqual(np.diff(e).max(), roadzhk.STEP_M + 1e-6)
        hole = (e > 149) & (e < 159)
        np.testing.assert_allclose(z[~hole], 10.0 + 0.01 * (np.round(e[~hole]) + 0.5), atol=1e-4)
        self.assertTrue(np.isnan(z[hole]).all(), "where the DTM has none")
        self.assertTrue(np.isfinite(z[np.abs(e - 100) < 5]).all(), "the DTM has no decks: the road under one keeps it")

    def test_decks_from_the_dsm_none_under_a_higher_deck(self):
        lower = [r for r in by_name(self.ln, "") if r[2] == 1]
        (ll, z, _lv, _ref), = lower
        _e, n = east_north(ll)
        under = np.abs(n - 121) <= roadzhk.OCCLUDE_M
        self.assertTrue(np.isnan(z[under]).all(), "its DSM is the higher deck's")
        np.testing.assert_allclose(z[~under], 20.0, err_msg="the deck's surface, not the bus on it")
        (_ll, z2, _lv, _ref), = by_name(self.ln, "TATE'S CAIRN HIGHWAY")
        np.testing.assert_allclose(z2, 30.0)
        tunnel = [r for r in by_name(self.ln, "") if r[2] < 0]
        self.assertTrue(np.isnan(tunnel[0][1]).all(), "a tunnel keeps the portal inference")

    def test_a_speckled_deck_reads_its_surface(self):
        """A viaduct's DSM with gaps to the ground under it (a third of its pixels): the deck's height, not the
        ground's (the 25th percentile of 5 reads across took the ground at Tsing Sha Highway)."""
        xy = np.c_[np.zeros(30), np.arange(30) * 4.0]
        line = roadzhk.TdLine(1, "", "", xy * 0, xy)
        runs = roadzhk.stations([line], lambda ll: np.ones(len(ll), bool))

        def dsm(x, y):
            gap = (np.floor(x * 2) * 5 + np.floor(y * 2)) % 3 == 0
            return np.where(gap, 10.0, 20.0)
        (_ll, z, _lv, _ref, _name), = roadzhk.rows([line], runs, None, dsm)
        np.testing.assert_allclose(z, 20.0)

    def test_sheets_kept_for_the_next_cell(self):
        n = len(self.fetcher.downloads)
        self.assertEqual(n, 4, "two DTM sheets and the two DSM sheets under the decks")
        rz = roadz.RoadZ(self.dir, self.fetcher, None)
        rz._hk(KEY)
        self.assertEqual(len(self.fetcher.downloads), n, "read from the kept sheets")

    def test_kept_sheets_capped(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hklidar, "SHEETS_MB", 0.0):
            roadz.RoadZ(d, FakeFetcher(), None)._hk(KEY)
            left = [f for _dp, _dn, fs in os.walk(d) for f in fs if f.endswith(".zip")]
        self.assertEqual(left, [])


class SourceTest(unittest.TestCase):
    def test_bounds_and_off_switch(self):
        self.assertIn("hk", roadz.sources_at(22.38, 114.2))
        self.assertNotIn("hk", roadz.sources_at(22.19, 113.55), "Macau")
        self.assertTrue(roadz.hk_cell(KEY))
        self.assertFalse(roadz.hk_cell(mapcells.cell_of(48.85, 2.35)))
        with mock.patch.object(roadz, "HK_ON", False):
            self.assertNotIn("hk", roadz.sources_at(22.38, 114.2))
            self.assertNotIn("hk1", roadz.hk_tags(KEY))

    def test_credited_where_its_lines_are_kept(self):
        import credits
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(roadz.hk_credited_near(d, LAT0, LON0, 2000.0))
            rz = roadz.RoadZ(d, FakeFetcher(), None)
            rz.lines("hk", KEY)
            self.assertTrue(roadz.hk_credited_near(d, LAT0, LON0, 2000.0))
            self.assertFalse(roadz.hk_credited_near(d, LAT0 + 0.1, LON0, 2000.0), "no cell kept there")
            with mock.patch.object(roadz, "HK_ON", False):
                self.assertFalse(roadz.hk_credited_near(d, LAT0, LON0, 2000.0))
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "CREDITS.md"),
                  encoding="utf-8") as fh:
            self.assertIn(f'"Road heights: {credits.full_label("roadz:hk")}"', fh.read())
        self.assertTrue(credits.line({"roadz:hk"}).startswith("Road heights: HK Gov"))

    @unittest.skipUnless(NATIVE, "earthfast roadz kernels")
    def test_observed_deck_on_our_datum(self):
        """A map bridge over the fixture's lower deck takes its LiDAR height less the LiDAR's ground over our DEM."""
        with tempfile.TemporaryDirectory() as d:
            rz = roadz.RoadZ(d, FakeFetcher(), SimpleNamespace(dem=SimpleNamespace(
                sample=lambda la, lo, z: np.full(np.shape(la), 8.0))))
            ll = np.array([[LON0 + 100 / 1e5, LAT0 + 20 / 1.1e5], [LON0 + 100 / 1e5, LAT0 + 100 / 1.1e5]])
            frame = geodesy.EnuFrame(LAT0, LON0)
            e, n, _ = frame.geodetic_to_enu(np.linspace(ll[0, 1], ll[1, 1], 30), np.full(30, ll[0, 0]), 0.0)
            with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["hk"]):
                info = {}
                z = rz.observed(frame, np.c_[e, n], 4.0, 1, "", info)
        self.assertEqual(info["source"], "hk")
        self.assertAlmostEqual(info["offset"], 11.0 - 8.0, delta=0.5)
        np.testing.assert_allclose(z, 20.0 - info["offset"], atol=1e-6)


class SlowFetcher(FakeFetcher):
    def get(self, url, **kw):
        if urllib.parse.urlsplit(url).hostname == "bulkdata.csdi.gov.hk":
            time.sleep(0.5)
        return super().get(url, **kw)


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class ColdCellTest(unittest.TestCase):
    """A cold cell's sheets are waited for as the lidar's are (gap_answers): the inference within LIDAR_GAPS_WAIT_S for
    the rest of the run and nothing planned on it kept, while the read goes on; the next run measures."""

    def observe(self, rz):
        ll = np.array([[LON0 + 100 / 1e5, LAT0 + 20 / 1.1e5], [LON0 + 100 / 1e5, LAT0 + 100 / 1.1e5]])
        frame = geodesy.EnuFrame(LAT0, LON0)
        e, n, _ = frame.geodetic_to_enu(np.linspace(ll[0, 1], ll[1, 1], 30), np.full(30, ll[0, 0]), 0.0)
        with mock.patch.object(roadz, "sources_at", lambda lat, lon, lidar=None: ["hk"]):
            return rz.observed(frame, np.c_[e, n], 4.0, 1, "")

    def test_inference_meanwhile_measured_next_run(self):
        import revisit
        md = SimpleNamespace(dem=SimpleNamespace(sample=lambda la, lo, z: np.full(np.shape(la), 8.0)))
        fetcher = SlowFetcher()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(roadz, "LIDAR_GAPS_WAIT_S", 0.2):
            with mock.patch.dict(os.environ, {roadz.SESSION_ENV: "run1"}):
                rz = roadz.RoadZ(d, fetcher, md)
                l0, t0 = revisit.thread_lidar_faults(), time.monotonic()
                self.assertIsNone(self.observe(rz))
                self.assertLess(time.monotonic() - t0, 0.45, "waited for the sheets")
                self.assertGreater(revisit.thread_lidar_faults(), l0, "planned on the inference: not kept")
                for t in threading.enumerate():
                    if t.name == "roadz-gaps":
                        t.join()
                self.assertTrue(os.path.exists(rz._path("hk", KEY)), "read on to the end")
                self.assertIsNone(self.observe(rz), "the run's decision")
            with mock.patch.dict(os.environ, {roadz.SESSION_ENV: "run2"}):
                z = self.observe(roadz.RoadZ(d, fetcher, md))
        self.assertIsNotNone(z)
        self.assertEqual(len(fetcher.downloads), 4, "each sheet once")


class NotSharedTest(unittest.TestCase):
    """Plans with Hong Kong's heights stay on this computer: no world-bundle marker, no BeamMP upload, no baked pack,
    and a revisit key of their own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pc = plancache.PlanCache(os.path.join(tmp.name, "plans"), "w0s0")
        self.pc.persist = (tmp.name, (plancache.FRAMES, "w0s0", {}))
        self.paris = tuple(mapcells.cell_of(48.85, 2.35))

    def store(self, key):
        with mock.patch("mpshare.note_built") as noted, mock.patch("revisit.store", return_value=True):
            self.assertTrue(self.pc.store(key, [], [], [], [], persist=True))
        return os.path.exists(self.pc._path(key) + ".complete"), noted.called

    def test_hong_kong_plans_not_shared(self):
        self.assertEqual(self.store(tuple(KEY)), (False, False))
        self.assertEqual(self.store(self.paris), (True, True))
        with mock.patch("bakepack.find", return_value=b"baked") as find:
            self.assertIsNone(self.pc._baked(tuple(KEY)))
            self.assertIsNotNone(self.pc._baked(self.paris))
            self.assertEqual(find.call_count, 1)

    def test_own_revisit_key(self):
        on = self.pc._revisit_key(tuple(KEY))
        with mock.patch.object(roadz, "HK_ON", False):
            self.assertNotEqual(self.pc._revisit_key(tuple(KEY)), on)
            paris = self.pc._revisit_key(self.paris)
        self.assertEqual(self.pc._revisit_key(self.paris), paris)

    def test_share_tools_turn_it_off(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        for p in ("server/sharehelper.py", "tools/bake_area.py"):
            with open(os.path.join(root, p), encoding="utf-8") as fh:
                self.assertIn('os.environ["BNE_ROADZ_HK"] = "off"', fh.read(), p)


if __name__ == "__main__":
    unittest.main()
