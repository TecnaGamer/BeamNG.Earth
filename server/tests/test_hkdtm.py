"""Hong Kong's lidar terrain (hkdtm.py, dem.py): the HK1980 Grid projection against PROJ, the near terrain read from
the DTM sheets on PROJ's grid and moved to EGM2008, a cold place's tiles never waiting for a sheet (Mapterhorn's
heights for the run, nothing built on them kept, the sheets read from the next run on), the sea left to Mapterhorn,
the off switch and plans made on it kept out of every share. Synthetic sheets only (hklidarfixtures)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import credits  # noqa: E402
import dem as demmod  # noqa: E402
import hkdtm  # noqa: E402
import hklidar  # noqa: E402
import hrdem  # noqa: E402
import mapcells  # noqa: E402
import plancache  # noqa: E402
import revisit  # noqa: E402
import roadz  # noqa: E402
import xproc  # noqa: E402
from hklidarfixtures import tiff_zip  # noqa: E402
from test_dem import TerrariumFetcher  # noqa: E402

KEY = (15, 26779, 14292)                # a z15 tile over Shek Mun, Sha Tin (HK1980 Grid 838946..840078 E)
PX = 2.0
SHEETS = {"7SE12A": 838900.0, "7SE12B": 839500.0}       # west edges; each 600 x 1200 m from 826800 N
SEA_E = 839900.0                        # the sheets have no ground east of this


def ground(e, n):
    return 10.0 + 0.01 * (e - 838900.0) + 0.02 * (n - 826800.0)


def sheet_zip(x0):
    e = x0 + (np.arange(300) + 0.5) * PX
    n = 828000.0 - (np.arange(600) + 0.5) * PX
    a = ground(e[None, :], n[:, None]).astype(np.float32)
    a[:, e > SEA_E] = -9999.0
    return tiff_zip(a, x0, 828000.0, px=PX)


class Fetcher(TerrariumFetcher):
    """Mapterhorn (TerrariumFetcher: 100 + x a tile), the DTM sheet index (the fixture's sheets meeting the envelope
    asked) and the sheets, counted."""

    def __init__(self):
        super().__init__()
        self.zips = {f"https://{hklidar.SHEET_HOST}/cedd/lidar/2020dtm/{k}.zip": sheet_zip(x0)
                     for k, x0 in SHEETS.items()}
        self.downloads, self.queries = [], []

    def get(self, url, *, kind="demand", headers=None):
        u = urllib.parse.urlsplit(url)
        if u.hostname == hklidar.SHEET_HOST:
            self.downloads.append(u.path)
            return SimpleNamespace(body=self.zips[url])
        if u.hostname == "portal.csdi.gov.hk":
            q = dict(urllib.parse.parse_qsl(u.query))
            self.queries.append(q)
            x0, y0, x1, y1 = map(float, q["geometry"].split(","))
            feats = [{"attributes": {"TILE_NAME": k, "URL": f"https://{hklidar.SHEET_HOST}/cedd/lidar/2020dtm/{k}.zip"},
                      "geometry": {"rings": [[[e, 826800.0], [e + 600, 826800.0], [e + 600, 828000.0],
                                              [e, 828000.0]]]}}
                     for k, e in SHEETS.items() if e < x1 and x0 < e + 600 and 826800.0 < y1 and y0 < 828000.0]
            return SimpleNamespace(body=json.dumps({"features": feats}).encode())
        if url == demmod.COVERAGE_URL:
            import netfetch
            raise netfetch.HostUnavailable("no coverage list in this test")
        return super().get(url, kind=kind, headers=headers)


def settle(t, timeout=10.0):
    """Wait for t's background downloads."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with t.sheets._lock:
            if not t.sheets._later:
                return
        time.sleep(0.02)
    raise AssertionError("background downloads still running")


class ProjectionTest(unittest.TestCase):
    def test_hk1980_grid_as_proj(self):
        """PROJ 9.8 (pyproj 3.8, EPSG:4326 -> EPSG:2326: tmerc on intl, EPSG:1825's 7 parameters), 2026-11-10."""
        ref = {(22.383, 114.208): (839473.3990, 827087.0747), (22.37767, 114.18145): (836739.2280, 826496.5293),
               (22.2, 113.9): (807717.5243, 806848.4213), (22.55, 114.4): (859218.5790, 845596.9120),
               (22.3, 114.17): (835559.6421, 817895.7547)}
        lat, lon = np.array(list(ref)).T
        e, n = hkdtm.grid(lat, lon)
        np.testing.assert_allclose(np.c_[e, n], np.array(list(ref.values())), atol=1e-3)


class TileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.fetcher = Fetcher()

    def dtm(self):
        return hkdtm.HkDtm(hklidar.Sheets(os.path.join(self.root, "hklidar"), self.fetcher))

    def test_cold_place_waits_for_nothing_then_reads_the_next_run(self):
        with mock.patch.dict(os.environ, {xproc.RUN_ENV: "run1"}):
            t = self.dtm()
            t0 = time.monotonic()
            _h, ok, short = t.tile(*KEY)
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertTrue(short)
            self.assertFalse(ok.any())
            settle(t)
            self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in self.fetcher.downloads),
                             ["7SE12A.zip", "7SE12B.zip"])
            self.assertTrue(t.tile(*KEY)[2], "the run's decision: on disk now, read from the next run")
            self.assertTrue(self.dtm().tile(*KEY)[2], "every process of the run the same")
        with mock.patch.dict(os.environ, {xproc.RUN_ENV: "run2"}):
            t = self.dtm()
            h, ok, short = t.tile(*KEY)
            self.assertFalse(short)
        lat, lon = hrdem.tile_lat_lon(*KEY, hkdtm.TILE)
        e, n = hkdtm.grid(lat, lon)
        inside = (e > 838902) & (e < SEA_E - 2) & (n > 826802) & (n < 827998)
        np.testing.assert_allclose(h[inside], ground(e, n)[inside] + hkdtm.TO_EGM2008_M, atol=2e-3)
        self.assertTrue(ok[inside].all())
        self.assertFalse(ok[e > SEA_E + 2].any(), "the sea: no ground")
        self.assertEqual(len(self.fetcher.downloads), 2, "each sheet once")
        self.assertEqual(len(self.fetcher.queries), 1, "the index kept")

    def test_only_its_tiles(self):
        self.assertFalse(hkdtm.covers(14, KEY[1] >> 1, KEY[2] >> 1), "the far rings keep Mapterhorn's")
        paris = (15, *demmod._tile_xy(48.85, 2.35, 15))
        self.assertFalse(hkdtm.covers(*paris))
        _h, ok, short = self.dtm().tile(*paris)
        self.assertFalse(ok.any() or short)
        self.assertEqual(self.fetcher.queries, [])


class DemTest(unittest.TestCase):
    """dem.py's near tile over Hong Kong: Mapterhorn's while short (not mirrored, a revisit fault), the DTM blended in
    from the next run (mirrored apart, credited), the sea Mapterhorn's."""

    def test_cold_then_read(self):
        f = Fetcher()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {xproc.RUN_ENV: "run1"}):
                dm = demmod.Dem(os.path.join(d, "dem"), fetcher=f)
                f0 = revisit.faults()
                a = dm._load_tile(KEY)
                np.testing.assert_array_equal(a, 100 + KEY[1] % 1000)
                self.assertGreater(revisit.faults(), f0, "nothing built on it kept")
                self.assertFalse(os.path.exists(dm._mirror_path(KEY)), "not mirrored")
                self.assertNotIn(KEY, dm.hkdtm_served)
                settle(dm.hkdtm)
            with mock.patch.dict(os.environ, {xproc.RUN_ENV: "run2"}):
                dm = demmod.Dem(os.path.join(d, "dem"), fetcher=f)
                f0 = revisit.faults()
                a = dm._load_tile(KEY)
                self.assertEqual(revisit.faults(), f0)
                self.assertIn(KEY, dm.hkdtm_served)
                self.assertIn("g1h", dm._mirror_path(KEY))
                self.assertTrue(os.path.exists(dm._mirror_path(KEY)), "mirrored, apart from Mapterhorn's own")
            lat, lon = hrdem.tile_lat_lon(*KEY, hkdtm.TILE)
            e, n = hkdtm.grid(lat, lon)
            deep = (e > 839000) & (e < SEA_E - 100) & (n > 826900) & (n < 827900)
            np.testing.assert_allclose(a[deep], ground(e, n)[deep] + hkdtm.TO_EGM2008_M, atol=2e-3)
            np.testing.assert_array_equal(a[e > SEA_E + 2], 100 + KEY[1] % 1000)
            edge = (e > SEA_E - 50) & (e < SEA_E - PX)
            self.assertTrue(((a[edge] > ground(e, n)[edge] - 1) & (a[edge] < 100 + KEY[1] % 1000)).all(),
                            "feathered into Mapterhorn's at the sea")
        self.assertTrue(credits.line({"dem:hkdtm"}).startswith("Elevation: HK Gov CEDD LiDAR"))

    def test_off_switch(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(hkdtm, "ON", False):
            dm = demmod.Dem(os.path.join(d, "dem"), fetcher=Fetcher())
            self.assertIsNone(dm.hkdtm)
            self.assertNotIn("g1h", dm._mirror_path(KEY))


class NotSharedTest(unittest.TestCase):
    """Plans made on Hong Kong's terrain stay on this computer, with the road heights off too."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pc = plancache.PlanCache(os.path.join(tmp.name, "plans"), "w0s0")
        self.pc.persist = (tmp.name, (plancache.FRAMES, "w0s0", {}))
        self.cell = tuple(mapcells.cell_of(22.383, 114.208))

    def test_terrain_alone_keeps_plans_here(self):
        with mock.patch.object(roadz, "HK_ON", False):
            self.assertEqual(roadz.hk_tags(self.cell), ("hkdtm1",))
            with mock.patch("mpshare.note_built") as noted, mock.patch("revisit.store", return_value=True):
                self.assertTrue(self.pc.store(self.cell, [], [], [], [], persist=True))
            self.assertFalse(os.path.exists(self.pc._path(self.cell) + ".complete"), "no world-bundle marker")
            self.assertFalse(noted.called, "no BeamMP upload")
            with mock.patch("bakepack.find", return_value=b"baked") as find:
                self.assertIsNone(self.pc._baked(self.cell))
                self.assertEqual(find.call_count, 0)
            on = self.pc._revisit_key(self.cell)
            with mock.patch.object(hkdtm, "ON", False):
                self.assertFalse(roadz.hk_cell(self.cell))
                self.assertNotEqual(self.pc._revisit_key(self.cell), on)

    def test_share_tools_turn_it_off(self):
        """What sharehelper and bake_area plan in Hong Kong is made with neither (in their own process)."""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        for d, m in (("server", "sharehelper"), ("tools", "bake_area")):
            code = (f"import sys; sys.path.insert(0, {os.path.join(root, d)!r}); import {m}, roadz, mapcells; "
                    "print(roadz.hk_tags(tuple(mapcells.cell_of(22.383, 114.208))))")
            env = {k: v for k, v in os.environ.items() if k not in ("BNE_HKDTM", "BNE_ROADZ_HK")}
            got = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual((got.returncode, got.stdout.strip()), (0, "()"), got.stderr[-2000:])

    def test_credited(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "CREDITS.md"),
                  encoding="utf-8") as fh:
            self.assertIn(f'"Elevation: {credits.full_label("dem:hkdtm")}"', fh.read())


if __name__ == "__main__":
    unittest.main()
