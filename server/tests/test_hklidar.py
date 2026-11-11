"""Hong Kong's LiDAR sheets (hklidar.py), shared by the road heights and the terrain: the sheet reader, the index,
each sheet downloaded once across processes and kept in a capped folder. Synthetic sheets only (hklidarfixtures)."""
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
import hklidar  # noqa: E402
from hklidarfixtures import tiff_zip  # noqa: E402

X0, Y0 = 840000.0, 826000.0
URL = "https://bulkdata.csdi.gov.hk/cedd/lidar/2020dtm/7SE12A(e839n827,e839n827).zip"


class SheetTest(unittest.TestCase):
    def test_reads_its_pixels_across_tiles(self):
        a = np.arange(40 * 24, dtype=np.float32).reshape(24, 40)      # (partial tiles on both edges)
        a[5, 7] = -9999.0
        s = hklidar.Sheet(tiff_zip(a, X0, Y0, px=0.5))
        self.assertEqual(s.box, (X0, Y0 - 12.0, X0 + 20.0, Y0))
        cols, rows = np.array([0, 39, 17, 7, 3]), np.array([0, 23, 20, 5, 30])
        got = s.sample(X0 + cols * 0.5 + 0.25, Y0 - rows * 0.5 - 0.25)
        np.testing.assert_array_equal(got[:3], a[rows[:3], cols[:3]])
        self.assertTrue(np.isnan(got[3]), "nodata")
        self.assertTrue(np.isnan(got[4]), "outside the sheet")

    def test_bilinear_between_pixel_centres(self):
        x = np.arange(40) * 0.5 + 0.25
        a = np.tile((3.0 * x).astype(np.float32), (24, 1)) + (np.arange(24)[:, None] * 0.1).astype(np.float32)
        a[10, 20] = -9999.0
        s = hklidar.Sheet(tiff_zip(a, X0, Y0, px=0.5))
        px = X0 + np.array([1.0, 5.1, 19.9, 0.1, 10.2, 21.0])
        py = Y0 - np.array([1.0, 2.6, 11.9, 0.1, 5.2, 1.0])
        got = s.bilinear(px, py)
        row = (Y0 - py[:4]) / 0.5 - 0.5
        np.testing.assert_allclose(got[:4], 3.0 * (px[:4] - X0) + 0.1 * row, atol=1e-4)
        self.assertTrue(np.isnan(got[4]), "next to a pixel it has none of")
        self.assertTrue(np.isnan(got[5]), "outside the sheet")

    def test_refuses_what_it_cannot_read(self):
        with self.assertRaises(ValueError):
            hklidar.Sheet(tiff_zip(np.zeros((4, 4), np.float32), X0, Y0, compression=8))


class Fetcher:
    """The sheet index (one sheet) and the sheet, slowly; counts the downloads."""

    def __init__(self, delay=0.0):
        self.delay, self.downloads, self.queries = delay, [], []
        self.zip = tiff_zip(np.full((20, 30), 7.0, np.float32), X0, Y0 + 10.0)

    def get(self, url, **_kw):
        u = urllib.parse.urlsplit(url)
        if u.hostname == hklidar.SHEET_HOST:
            time.sleep(self.delay)
            self.downloads.append(u.path)
            return SimpleNamespace(body=self.zip)
        self.queries.append(dict(urllib.parse.parse_qsl(u.query)))
        ring = [[X0, Y0 - 10], [X0 + 30, Y0 - 10], [X0 + 30, Y0 + 10], [X0, Y0 + 10]]
        return SimpleNamespace(body=json.dumps({"features": [
            {"attributes": {"TILE_NAME": "7SE12A", "URL": URL}, "geometry": {"rings": [ring]}}]}).encode())


class SheetsTest(unittest.TestCase):
    def test_index_by_box(self):
        f = Fetcher()
        got = hklidar.Sheets(None, f).index("dsm", (X0, Y0, X0 + 5, Y0 + 5))
        self.assertEqual(got, [("7SE12A", URL, (X0, Y0 - 10, X0 + 30, Y0 + 10))])
        self.assertEqual(f.queries[0]["geometry"], f"{X0:.1f},{Y0:.1f},{X0 + 5:.1f},{Y0 + 5:.1f}")

    def test_kept_for_the_next_read_and_capped(self):
        f = Fetcher()
        with tempfile.TemporaryDirectory() as d:
            sheets = hklidar.Sheets(d, f)
            self.assertIsNone(sheets.kept("dtm", "7SE12A"))
            self.assertEqual(sheets.get("dtm", "7SE12A", URL).sample([X0 + 1], [Y0]).tolist(), [7.0])
            self.assertIsNotNone(hklidar.Sheets(d, f).kept("dtm", "7SE12A"), "on disk for another process")
            self.assertEqual(len(f.downloads), 1)
            with mock.patch.object(hklidar, "SHEETS_MB", 0.0):
                hklidar.Sheets(d, f).trim()
            self.assertIsNone(hklidar.Sheets(d, f).kept("dtm", "7SE12A"))

    def test_refuses_another_host(self):
        with self.assertRaises(ValueError):
            hklidar.Sheets(None, Fetcher()).get("dtm", "7SE12A", "https://example.com/7SE12A.zip")

    def test_a_sheet_another_process_reads_is_waited_for(self):
        """Two processes (two Sheets: no lock in common but the disk) after one sheet: one download."""
        f = Fetcher(delay=0.5)
        with tempfile.TemporaryDirectory() as d:
            a, b = hklidar.Sheets(d, f), hklidar.Sheets(d, f)
            ta = threading.Thread(target=a.get, args=("dtm", "7SE12A", URL))
            ta.start()
            time.sleep(0.1)
            got = b.get("dtm", "7SE12A", URL)
            ta.join()
        self.assertEqual(got.w, 30)
        self.assertEqual(len(f.downloads), 1)

    def test_later_runs_once_a_key(self):
        sheets = hklidar.Sheets(None, Fetcher())
        go, ran = threading.Event(), []

        def job():
            go.wait(5)
            ran.append(1)
        sheets.later("k", job)
        sheets.later("k", job)
        go.set()
        end = time.monotonic() + 5
        while sheets._later and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(ran, [1])


if __name__ == "__main__":
    unittest.main()
