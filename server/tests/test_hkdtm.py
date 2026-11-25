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


def reference_tile(t, z, x, y, size=hkdtm.TILE):
    """HkDtm.tile as it was before the coarse test (the whole tile projected first), run on t's own methods."""
    h = np.zeros((size, size), np.float32)
    ok = np.zeros((size, size), bool)
    lat, lon = hrdem.tile_lat_lon(z, x, y, size)
    ex, ny = hkdtm.grid(lat, lon)
    box = (float(ex.min()), float(ny.min()), float(ex.max()), float(ny.max()))
    d = z - hkdtm.INDEX_ZOOM
    chunk = (hkdtm.INDEX_ZOOM, x >> d, y >> d)
    idx = t._index(chunk)
    short = idx is None
    if short:
        t._want(chunk, box)
    taken = np.zeros((size, size), bool)
    for name, url, (x0, y0, x1, y1) in idx or ():
        if not (x0 < box[2] and box[0] < x1 and y0 < box[3] and box[1] < y1):
            continue
        m = (ex >= x0) & (ex < x1) & (ny >= y0) & (ny < y1) & ~taken
        if not m.any():
            continue
        taken |= m
        s = t._sheet(name, url)
        if s is None:
            short = True
        if not s:
            continue
        v = s.bilinear(ex[m], ny[m])
        good = np.isfinite(v)
        h[m] = np.where(good, v + hkdtm.TO_EGM2008_M, 0.0)
        ok[m] = good
    with t._lock:
        t.stats["tiles"] += 1
        t.stats["short"] += short
    return h, ok, short


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


CHUNK = (hkdtm.INDEX_ZOOM, KEY[1] >> 1, KEY[2] >> 1)
CHUNK_DECISION = "index_14_13389_7146"
URL = "https://" + hklidar.SHEET_HOST + "/cedd/lidar/2020dtm/%s.zip"


class FirstVisitTest(unittest.TestCase):
    """A tile projects only a coarse part of its pixels when this run reads none of its sheets, and answers exactly as
    when it projected them all (reference_tile): the same heights, decisions, downloads and index asked for."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.fetcher = Fetcher()
        lat, lon = hrdem.tile_lat_lon(*KEY, hkdtm.TILE)
        self.ex, self.ny = hkdtm.grid(lat, lon)
        self.box = (float(self.ex.min()), float(self.ny.min()), float(self.ex.max()), float(self.ny.max()))
        self.env = mock.patch.dict(os.environ, {xproc.RUN_ENV: "run1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def dtm(self):
        return hkdtm.HkDtm(hklidar.Sheets(os.path.join(self.root, "hklidar"), self.fetcher))

    def write_index(self, extra=()):
        """The chunk's index on disk: the extra [(name, box)] sheets first, then the fixture's two."""
        p = self.dtm()._index_path(CHUNK)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rows = [[n, URL % n, list(b)] for n, b in extra]
        rows += [[k, URL % k, [e, 826800.0, e + 600, 828000.0]] for k, e in SHEETS.items()]
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def put_sheet(self, name):
        p = self.dtm().sheets.path("dtm", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(self.fetcher.zips[URL % name])

    def put_gone(self, name):
        p = self.dtm().sheets.path("dtm", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p + ".404", "wb").close()

    def ask(self, tile, key):
        """tile(HkDtm, *key) on a new HkDtm: (h, ok, short), what it decided (a name once: asking again is the first
        ask's answer), asked to download and wanted (as [(kind, ...)] by kind, each in its order, nothing done), its
        stats, and how many points it projected."""
        t, log, points = self.dtm(), [], [0]
        decide, grid = t._decide, hkdtm.grid
        t._decide = lambda what, now: (log.append(("decide", what)) if ("decide", what) not in log else None,
                                       decide(what, now))[1]
        t._fetch_later = lambda name, url: log.append(("later", name))
        t._want = lambda chunk, box: log.append(("want", chunk, box))

        def counted(lat, lon):
            points[0] += np.broadcast(lat, lon).size
            return grid(lat, lon)
        with mock.patch.object(hkdtm, "grid", counted):
            got = tile(t, *key)
        return got, sorted(log, key=lambda r: r[0]), dict(t.stats), points[0]

    def same_as_before(self, key=KEY):
        """What tile answers, after checking that reference_tile (asked first: it makes the run's decisions) did
        the same."""
        old = self.ask(reference_tile, key)
        new = self.ask(lambda t, *k: t.tile(*k), key)
        for a, b in zip(old[0], new[0]):
            np.testing.assert_array_equal(a, b)
        self.assertEqual(old[1:3], new[1:3])
        self.assertEqual(old[3], hkdtm.TILE * hkdtm.TILE, "(the reference projects every pixel)")
        return new

    def decide_without_sheets(self):
        """The run's first ask, before the sheets are on disk: they are not read this run."""
        self.ask(reference_tile, KEY)

    def test_index_missing(self):
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (True, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("want", CHUNK, self.box)])
        self.assertEqual(stats, {"tiles": 1, "short": 1})
        self.assertLessEqual(points, 4096)

    def test_same_wants_all_over_hong_kong(self):
        for lat, lon in ((22.383, 114.208), (22.30, 114.17), (22.2, 113.9), (22.55, 114.4), (22.45, 114.1)):
            for z in (15, 16, 17):
                self.same_as_before((z, *demmod._tile_xy(lat, lon, z)))

    def test_every_sheet_gone(self):
        self.write_index()
        for k in SHEETS:
            self.put_gone(k)
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (False, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION)])
        self.assertEqual(stats, {"tiles": 1, "short": 0})
        self.assertLessEqual(points, 4096)

    def test_sheets_on_disk_not_read_this_run(self):
        self.write_index()
        self.decide_without_sheets()
        for k in SHEETS:
            self.put_sheet(k)
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (True, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("decide", "dtm_7SE12A"), ("decide", "dtm_7SE12B"),
                               ("later", "7SE12A"), ("later", "7SE12B")])
        self.assertEqual(stats, {"tiles": 1, "short": 1})
        self.assertLessEqual(points, 4096)

    def test_one_sheet_read_and_its_neighbour_not(self):
        self.write_index()
        self.put_sheet("7SE12A")
        self.decide_without_sheets()                # (7SE12A on disk at its first ask: read; 7SE12B not)
        self.put_sheet("7SE12B")
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertTrue(short)
        e, n = self.ex, self.ny
        west = (e > SHEETS["7SE12A"] + 2) & (e < SHEETS["7SE12B"] - 2) & (n > 826802) & (n < 827998)
        np.testing.assert_allclose(h[west], ground(e, n)[west] + hkdtm.TO_EGM2008_M, atol=2e-3)
        self.assertTrue(ok[west].all())
        self.assertFalse(ok[e >= SHEETS["7SE12B"]].any(), "the neighbour's pixels: Mapterhorn's")
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("decide", "dtm_7SE12A"), ("decide", "dtm_7SE12B"),
                               ("later", "7SE12B")])
        self.assertEqual(stats, {"tiles": 1, "short": 1})
        self.assertGreaterEqual(points, hkdtm.TILE * hkdtm.TILE)

    def corner_sheet(self):
        """A sheet whose box meets the tile's box only at its north-east corner: no pixel of the tile in it."""
        x0, y0 = self.box[2] - 0.001, self.box[3] - 0.001
        self.assertFalse(((self.ex >= x0) & (self.ny >= y0)).any(), "(the case: a box meeting, no pixel)")
        return "7SE99Z", (x0, y0, x0 + 600, y0 + 600)

    def test_sheet_touching_the_corner_not_gone(self):
        self.write_index([self.corner_sheet()])
        self.decide_without_sheets()
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (True, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("decide", "dtm_7SE12A"), ("decide", "dtm_7SE12B"),
                               ("later", "7SE12A"), ("later", "7SE12B")])
        self.assertGreaterEqual(points, hkdtm.TILE * hkdtm.TILE, "it may have pixels: the tile is projected")

    def test_sheet_touching_the_corner_gone(self):
        self.write_index([self.corner_sheet()])
        self.put_gone("7SE99Z")
        self.decide_without_sheets()
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (True, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("decide", "dtm_7SE12A"), ("decide", "dtm_7SE12B"),
                               ("later", "7SE12A"), ("later", "7SE12B")])
        self.assertLessEqual(points, 4096)

    def test_sheet_with_one_pixel_off_the_lattice(self):
        e, n = float(self.ex[101, 103]), float(self.ny[101, 103])
        box = (e - 0.001, n - 0.001, e + 0.001, n + 0.001)
        self.assertEqual(((self.ex >= box[0]) & (self.ex < box[2]) & (self.ny >= box[1]) & (self.ny < box[3])).sum(), 1)
        self.write_index([("7SE98Y", box)])
        self.decide_without_sheets()
        (h, ok, short), log, stats, points = self.same_as_before()
        self.assertEqual((short, ok.any(), h.any()), (True, False, False))
        self.assertEqual(log, [("decide", CHUNK_DECISION), ("decide", "dtm_7SE98Y"), ("decide", "dtm_7SE12A"),
                               ("decide", "dtm_7SE12B"), ("later", "7SE98Y"), ("later", "7SE12A"),
                               ("later", "7SE12B")])
        self.assertGreaterEqual(points, hkdtm.TILE * hkdtm.TILE)


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
