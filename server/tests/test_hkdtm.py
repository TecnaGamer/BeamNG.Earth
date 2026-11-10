"""Hong Kong's lidar terrain (hkdtm.py): the HK1980 Grid projection against PROJ, the near terrain read from the DTM
sheets on PROJ's grid and moved to EGM2008, a cold place's tiles never waiting for a sheet (short for the run, the
sheets read from the next run on). Synthetic sheets only (hklidarfixtures)."""
import json
import os
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
import dem as demmod  # noqa: E402
import hkdtm  # noqa: E402
import hklidar  # noqa: E402
import hrdem  # noqa: E402
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




if __name__ == "__main__":
    unittest.main()
