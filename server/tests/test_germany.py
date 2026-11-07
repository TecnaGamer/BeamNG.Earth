"""germany: whether a place is in Germany, over upstream's countrybounds (PR #505's border data): contains()
at border towns (upstream's fixture, tests/fixtures/regional: only places it covers), the lon / lat box asked first
(outside it no border data is asked at all), and no border data (Absent, regionaldata unconfigured, a failed fetch, a
checksum mismatch, an index that raises) meaning not Germany, never an error. The coordinates were confirmed against
placenames' town and country (Overture's divisions) when written."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import contextlib
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import borderfixtures  # noqa: E402
import build_regional_data  # noqa: E402
import countrybounds  # noqa: E402
import geodesy  # noqa: E402
import germany  # noqa: E402
import regionaldata  # noqa: E402

# (lat, lon), each inside upstream's fixture's bands (47-49.5 N)
KEHL, LOERRACH, FREILASSING, KONSTANZ = (48.573, 7.816), (47.614, 7.661), (47.841, 12.977), (47.663, 9.175)
BUESINGEN, WEIL, PASSAU, LINDAU = (47.697, 8.690), (47.594, 7.621), (48.574, 13.466), (47.546, 9.684)
SCHROBENHAUSEN, INGOLSTADT = (48.561, 11.262), (48.765, 11.424)
STRASBOURG, BASEL, SALZBURG = (48.583, 7.746), (47.558, 7.588), (47.800, 13.045)
KREUZLINGEN, SCHAFFHAUSEN, HUNINGUE = (47.646, 9.178), (47.697, 8.635), (47.592, 7.583)
SCHAERDING, BREGENZ = (48.457, 13.432), (47.503, 9.747)
# Lake Constance: the Obersee's middle (between Friedrichshafen and Romanshorn), and the Überlinger See's (between
# Sipplingen and the Bodanrück)
OBERSEE, UEBERLINGER_SEE = (47.60, 9.40), (47.78, 9.10)
# outside germany.BOX: New York, the North Atlantic, Paris, Vienna, Copenhagen (north of it), Amsterdam
AWAY = [(40.7, -74.0), (45.0, -30.0), (48.85, 2.35), (48.21, 16.37), (55.68, 12.57), (52.37, 4.90)]


def setUpModule():
    borderfixtures.require()


def _fresh_state():
    return {"cache": None, "fetcher": None, "manifest": None, "down_at": 0.0, "cells": {}}


@contextlib.contextmanager
def borders(local=None, cache=None, fetcher=None):
    """regionaldata and countrybounds as a fresh process has them: BNE_REGIONAL_DIR local (None: unset), configured
    with cache / fetcher (None: not configured); as they were afterwards."""
    env = {k: v for k, v in os.environ.items() if k != "BNE_REGIONAL_DIR"}
    if local is not None:
        env["BNE_REGIONAL_DIR"] = str(local)
    countrybounds._loaded.cache_clear()
    try:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(regionaldata, "_state", _fresh_state()), \
                mock.patch.object(regionaldata, "LOCAL", None):
            if fetcher is not None:
                regionaldata.configure(cache, fetcher)
            yield
    finally:
        countrybounds._loaded.cache_clear()
        germany._failed[:] = [-float("inf"), 0]


def lonlat(points):
    return np.array([p[1] for p in points]), np.array([p[0] for p in points])


class ContainsTest(unittest.TestCase):
    def test_border_pairs(self):
        inside = [KEHL, LOERRACH, FREILASSING, KONSTANZ, BUESINGEN, WEIL, PASSAU, LINDAU, SCHROBENHAUSEN, INGOLSTADT]
        outside = [STRASBOURG, BASEL, SALZBURG, KREUZLINGEN, SCHAFFHAUSEN, HUNINGUE, SCHAERDING, BREGENZ]
        self.assertEqual(germany.contains(*lonlat(inside + outside)).tolist(),
                         [True] * len(inside) + [False] * len(outside))

    def test_lon_first_and_lat_lon_to_countrybounds(self):
        """contains takes (lon, lat); countrybounds' codes_at (lat, lon) pairs: Kehl is Germany, Strasbourg 5 km west
        is not, and codes_at is handed the points' (lat, lon)."""
        self.assertEqual(germany.contains([KEHL[1], STRASBOURG[1]], [KEHL[0], STRASBOURG[0]]).tolist(), [True, False])
        seen = []
        real = countrybounds.bundled()
        spy = mock.Mock(codes_at=lambda pts: seen.append(np.asarray(pts).tolist()) or real.codes_at(pts))
        with mock.patch.object(countrybounds, "bundled", return_value=spy):
            self.assertEqual(germany.contains(KEHL[1], KEHL[0]).tolist(), [True])
        self.assertEqual(seen, [[[KEHL[0], KEHL[1]]]])

    def test_scalar_and_empty(self):
        self.assertEqual(germany.contains(11.262, 48.561).tolist(), [True])
        self.assertEqual(germany.contains(np.zeros(0), np.zeros(0)).shape, (0,))
        self.assertEqual(germany.contains(np.full((2, 2), 11.262), 48.561).tolist(), [True] * 4)
        self.assertEqual(germany.contains(np.nan, 48.561).tolist(), [False])

    def test_lake_constance(self):
        """Lake Constance as the border data has it: the Obersee's middle not Germany (countrybounds says CH there), the
        Überlinger See (German water) Germany; each call the same."""
        for p, inside in ((OBERSEE, False), (UEBERLINGER_SEE, True)):
            self.assertEqual([germany.contains(p[1], p[0]).tolist() for _ in range(2)], [[inside]] * 2, p)

    def test_effective_country(self):
        self.assertEqual(germany.effective_country("FR", True), "DE")
        self.assertEqual(germany.effective_country("DE", False), "DE")   # outside: unchanged, even a wrong "DE"
        self.assertIsNone(germany.effective_country(None, False))

    def test_de_at_uses_the_frame(self):
        f = geodesy.EnuFrame(SCHROBENHAUSEN[0], SCHROBENHAUSEN[1], 0.0)
        self.assertEqual(germany.de_at(f, np.array([0.0]), np.array([0.0])).tolist(), [True])
        f = geodesy.EnuFrame(KEHL[0], KEHL[1], 0.0)          # (Strasbourg 5.3 km west of Kehl)
        self.assertEqual(germany.de_at(f, np.array([0.0, -5300.0]), np.array([0.0, 750.0])).tolist(), [True, False])


class BoxTest(unittest.TestCase):
    def test_outside_the_box_asks_nothing(self):
        """A point outside germany.BOX is not Germany without countrybounds or regionaldata asked anything (no manifest,
        no file: a player far from Germany downloads no borders); the signature is not asked by contains either."""
        calls = []

        def refused(name):
            def f(*a, **k):
                calls.append(name)
                raise AssertionError(name)
            return f
        with mock.patch.object(countrybounds, "bundled", refused("bundled")), \
                mock.patch.object(countrybounds, "signature", refused("signature")), \
                mock.patch.object(regionaldata, "manifest", refused("manifest")), \
                mock.patch.object(regionaldata, "file", refused("file")):
            self.assertEqual(germany.contains(*lonlat(AWAY)).tolist(), [False] * len(AWAY))
            self.assertEqual(germany.contains(-76.59, 39.28).tolist(), [False])
            f = geodesy.EnuFrame(39.2834, -76.5934, 0.0)     # Baltimore
            self.assertEqual(germany.de_at(f, np.zeros(3), np.zeros(3)).tolist(), [False] * 3)
        self.assertEqual(calls, [])

    def test_in_box(self):
        lo, la, hi_lo, hi_la = germany.BOX
        self.assertEqual(germany.in_box([lo, hi_lo, lo - 1e-9, 11.0, np.nan], [la, hi_la, 50.0, hi_la + 1e-9, 50.0])
                         .tolist(), [True, True, False, False, False])
        self.assertTrue(germany.touches([lo - 1.0, lo + 0.001], [50.0, 50.0]))
        self.assertFalse(germany.touches([lo - 1.0, lo - 0.001], [50.0, 50.0]))

    def test_the_box_holds_the_fixtures_germany(self):
        """Every point of a 0.01 deg grid round the box (0.3 deg wide) that the fixture covers is not DE (the box's
        claim: Germany's border data lies inside it), and the Germany the fixture covers reaches within 0.1 deg of the
        box's south edge (the box is not loose there)."""
        idx = countrybounds.bundled()
        lo = np.arange(germany.BOX[0] - 0.3, germany.BOX[2] + 0.3, 0.01)
        la = np.arange(germany.BOX[1] - 0.3, 49.5, 0.01)          # (the fixture's Germany: 47-49.5 N)
        LO, LA = np.meshgrid(lo, la)
        out = ~germany.in_box(LO, LA)
        codes = np.array(idx.codes_at(np.column_stack([LA.ravel()[out], LO.ravel()[out]])), object)
        self.assertGreater(len(codes), 10000)
        self.assertFalse((codes == "DE").any())
        inside = np.column_stack([LA.ravel()[~out], LO.ravel()[~out]])
        de = inside[np.array(idx.codes_at(inside), object) == "DE"]
        self.assertLess(de[:, 0].min() - germany.BOX[1], 0.1)     # (Oberstdorf's Haldenwanger Eck, 47.27 N)


def _band_dir(root, parts, built="synthetic-1", sha=None):
    """A regional data folder (BNE_REGIONAL_DIR's layout) of a band file of parts [(code, [ring])] (rings of [lon, lat],
    tools/build_regional_data.band_file, unsimplified); sha: the manifest's checksum (None: the file's own)."""
    body = build_regional_data.band_file(parts, sorted({c for c, _ in parts}), tol=0.0)
    root = Path(root)
    (root / countrybounds.FILE).write_bytes(body)
    (root / "manifest.json").write_text(json.dumps(
        {"built": built, "files": {countrybounds.FILE: sha or hashlib.sha256(body).hexdigest()}}))
    return root


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


class NoBordersTest(unittest.TestCase):
    """No border data is not Germany, and never an error; the signature says so. The warnings the misses give are
    asserted (assertLogs), so the module passes silently."""
    POINTS = [SCHROBENHAUSEN, KEHL, BUESINGEN]

    def assert_off(self):
        self.assertEqual(germany.contains(*lonlat(self.POINTS)).tolist(), [False] * len(self.POINTS))
        self.assertTrue(germany.signature().endswith(germany.ABSENT), germany.signature())

    def assert_logged(self, logs, *parts):
        text = "\n".join(logs.output)
        for p in parts:
            self.assertIn(p, text)

    def test_absent(self):
        with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
            self.assert_off()

    def test_unconfigured(self):
        """No BNE_REGIONAL_DIR and no regionaldata.configure (a process without MapData): Absent, no network."""
        with borders(), self.assertLogs(level="WARNING") as logs:
            self.assertIsInstance(countrybounds.bundled(), countrybounds.Absent)
            self.assert_off()
        self.assert_logged(logs, "country borders (country_bands.bin) unavailable")

    def test_the_fetch_failing(self):
        class Down:
            asked = []

            def get(self, url, kind="demand"):
                self.asked.append(url)
                raise OSError("offline")
        with tempfile.TemporaryDirectory() as cache, borders(cache=cache, fetcher=Down()), \
                self.assertLogs(level="WARNING") as logs:
            self.assert_off()
            self.assertTrue(Down.asked and Down.asked[0].endswith("/regional/manifest.json"), Down.asked)
        self.assert_logged(logs, "country borders (country_bands.bin) unavailable")

    def test_a_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            _band_dir(d, [("DE", [square(11.0, 48.0, 11.5, 48.8)])], sha="0" * 64)
            with borders(local=d), self.assertLogs(level="WARNING") as logs:
                self.assertIsInstance(countrybounds.bundled(), countrybounds.Absent)
                self.assert_off()
        self.assert_logged(logs, "country_bands.bin: checksum mismatch",
                           "country borders (country_bands.bin) unavailable")

    def test_an_index_that_raises(self):
        """bundled() raising (a corrupt band file, a failed write of the download): not Germany, the signature absent,
        and bundled() not asked again for germany.RETRY_S; codes_at raising the same."""
        broken = mock.Mock(side_effect=OSError("disk full"))
        try:
            with mock.patch.object(countrybounds, "bundled", broken), self.assertLogs("germany", "WARNING") as logs:
                self.assert_off()
                self.assertEqual(germany.contains(11.262, 48.561).tolist(), [False])
            self.assertEqual(broken.call_count, 1)
            self.assert_logged(logs, "OSError: disk full (not Germany for 60 s)")
            spy = mock.Mock(codes_at=mock.Mock(side_effect=ValueError("bad band")))
            germany._failed[:] = [-float("inf"), 0]
            with mock.patch.object(countrybounds, "bundled", return_value=spy), \
                    self.assertLogs("germany", "WARNING") as logs:
                self.assertEqual(germany.contains(11.262, 48.561).tolist(), [False])
                self.assertEqual(germany.contains(11.262, 48.561).tolist(), [False])
            self.assertEqual(spy.codes_at.call_count, 1)
            self.assert_logged(logs, "ValueError: bad band (not Germany for 60 s)")
        finally:
            germany._failed[:] = [-float("inf"), 0]
        self.assertEqual(germany.contains(11.262, 48.561).tolist(), [True])


class FaultTest(unittest.TestCase):
    """A border lookup that fails marks the builds running then incomplete (revisit.fault: not kept, their keys may
    name the border data while they went on without it), and so does every lookup inside germany.BOX while its
    RETRY_S lasts; no border data (Absent: their keys say so) and points outside BOX do not."""

    def tearDown(self):
        germany._failed[:] = [-float("inf"), 0]

    def faults(self, fn):
        import revisit
        f0 = revisit.faults()
        fn()
        return revisit.faults() - f0

    def test_codes_at_raising(self):
        spy = mock.Mock(codes_at=mock.Mock(side_effect=ValueError("bad band")))
        with mock.patch.object(countrybounds, "bundled", return_value=spy), self.assertLogs("germany", "WARNING"):
            self.assertGreater(self.faults(lambda: germany.contains(11.262, 48.561)), 0)
            self.assertGreater(self.faults(lambda: germany.contains(11.262, 48.561)), 0, "(its RETRY_S)")
            self.assertEqual(self.faults(lambda: germany.contains(-76.59, 39.28)), 0, "(outside BOX)")
        self.assertEqual(spy.codes_at.call_count, 1)

    def test_the_index_raising(self):
        with mock.patch.object(countrybounds, "bundled", side_effect=OSError("disk full")), \
                self.assertLogs("germany", "WARNING"):
            self.assertGreater(self.faults(lambda: germany.contains(11.262, 48.561)), 0)
            self.assertGreater(self.faults(lambda: germany.contains(7.816, 48.573)), 0, "(its RETRY_S)")

    def test_none_without_a_failure(self):
        self.assertEqual(self.faults(lambda: germany.contains(11.262, 48.561)), 0, "(the fixture)")
        with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
            self.assertEqual(self.faults(lambda: germany.contains(11.262, 48.561)), 0, "(no border data)")


class SyntheticBordersTest(unittest.TestCase):
    def test_codes_through_regionaldata(self):
        """A band file of our own through regionaldata's local folder: DE in its square, FR next to it, a gap (None) and
        an overlap of DE and CH (None) not Germany; the signature the build's, without the absent mark."""
        with tempfile.TemporaryDirectory() as d:
            _band_dir(d, [("DE", [square(8.0, 48.0, 8.1, 48.1)]), ("FR", [square(7.9, 48.0, 8.0, 48.1)]),
                          ("CH", [square(8.08, 48.08, 8.2, 48.2)])], built="synthetic-7")
            with borders(local=d):
                got = germany.contains([8.05, 7.95, 8.5, 8.09], [48.05, 48.05, 48.05, 48.09]).tolist()
                self.assertEqual(got, [True, False, False, False])
                self.assertEqual(germany.signature(), countrybounds.ALGORITHM + ":synthetic-7")


class Pages:
    """A fetcher standing in for BeamNG.Earth-data's Pages: the files of folder under <PAGES>/regional/."""

    def __init__(self, folder):
        self.folder, self.asked = Path(folder), []

    def get(self, url, kind="demand"):
        self.asked.append(url.rsplit("/", 1)[-1])
        return mock.Mock(body=(self.folder / url.rsplit("/", 1)[-1]).read_bytes())


class EvictedTest(unittest.TestCase):
    """The band file removed from under countrybounds' index (the cache's evictable pagesdata over its size, or the
    settings page's Clear cache, while driving in Germany): a lookup in a band not decoded yet fails, the build is not
    kept, and after germany.RETRY_S the index is made again and the file fetched again through regionaldata."""

    def tearDown(self):
        germany._failed[:] = [-float("inf"), 0]

    def test_fetched_again_after_the_back_off(self):
        import revisit
        pages = Pages(borderfixtures.DIR)
        with tempfile.TemporaryDirectory() as cache, borders(cache=cache, fetcher=pages):
            self.assertEqual(germany.contains(SCHROBENHAUSEN[1], SCHROBENHAUSEN[0]).tolist(), [True])
            self.assertEqual(pages.asked, ["manifest.json", countrybounds.FILE])
            path = regionaldata.file(countrybounds.FILE)
            os.remove(path)                         # (evicted)
            f0 = revisit.faults()
            with self.assertLogs("germany", "WARNING") as logs:
                self.assertEqual(germany.contains(KONSTANZ[1], KONSTANZ[0]).tolist(), [False])    # (another band)
            self.assertIn("No such file or directory", "\n".join(logs.output))
            self.assertGreater(revisit.faults(), f0)
            self.assertTrue(germany.signature().endswith(germany.ABSENT), "(its RETRY_S: keys say no borders)")
            self.assertEqual(germany.contains(KONSTANZ[1], KONSTANZ[0]).tolist(), [False])
            self.assertEqual(pages.asked, ["manifest.json", countrybounds.FILE], "(nothing fetched meanwhile)")
            germany._failed[0] -= germany.RETRY_S        # (RETRY_S over)
            self.assertEqual(germany.contains(KONSTANZ[1], KONSTANZ[0]).tolist(), [True])
            self.assertEqual(pages.asked, ["manifest.json", countrybounds.FILE, countrybounds.FILE])
            self.assertTrue(os.path.exists(path))
            self.assertEqual(germany.signature(), countrybounds.signature())


class BackoffTest(unittest.TestCase):
    """The back-off after failures in a row (germany.RETRY_S, doubled each time up to RETRY_MAX_S; a lookup that answers
    starts it over), on germany's clock: a write of the band file's download that keeps failing fetches it again only
    as the back-off allows, not every RETRY_S."""

    def setUp(self):
        self.now = [1000.0]
        clock = mock.patch.object(germany, "time", mock.Mock(monotonic=lambda: self.now[0]))
        clock.start()
        self.addCleanup(clock.stop)
        self.addCleanup(germany._failed.__setitem__, slice(None), [-float("inf"), 0])

    def ask(self):
        return germany.contains(SCHROBENHAUSEN[1], SCHROBENHAUSEN[0]).tolist()

    def test_doubling_to_the_cap_and_starting_over(self):
        broken = mock.Mock(side_effect=OSError("No space left on device"))
        waits = [60, 120, 240, 480, 960, 1800, 1800]
        with self.assertLogs("germany", "WARNING") as logs:
            with mock.patch.object(countrybounds, "bundled", broken):
                self.assertEqual(self.ask(), [False])
                for k, wait in enumerate(waits):
                    self.now[0] += wait - 0.5
                    self.assertEqual(self.ask(), [False])
                    germany.signature()
                    self.assertEqual(broken.call_count, k + 1, f"(not before {wait} s)")
                    self.now[0] += 0.5
                    self.assertEqual(self.ask(), [False])
                    self.assertEqual(broken.call_count, k + 2, f"(again after {wait} s)")
            self.now[0] += 1800
            spy = mock.Mock(codes_at=mock.Mock(return_value=["DE"]))
            with mock.patch.object(countrybounds, "bundled", return_value=spy):
                self.assertEqual(self.ask(), [True])            # (it answers: the run of failures is over)
            again = mock.Mock(side_effect=OSError("No space left on device"))
            with mock.patch.object(countrybounds, "bundled", again):
                self.assertEqual(self.ask(), [False])
                self.now[0] += 59.5
                self.ask()
                self.assertEqual(again.call_count, 1)
                self.now[0] += 0.5
                self.ask()
                self.assertEqual(again.call_count, 2, "(60 s after a lookup that answered)")
        said = [line.rsplit("(not Germany for ", 1)[1] for line in logs.output]
        self.assertEqual(said, [f"{w} s)" for w in waits + [1800, 60, 120]])   # (each failure's own wait)

    def test_no_download_before_the_wait(self):
        """Through regionaldata, the band file's write failing (a full disk): it is downloaded once at each try, the
        tries 60, 120, 240 s apart; the disk freed, the next try keeps it and Germany answers."""
        real = regionaldata._write
        full = [True]

        def write(path, body):
            if full[0] and path.endswith(countrybounds.FILE):
                raise OSError(28, "No space left on device")
            return real(path, body)
        pages = Pages(borderfixtures.DIR)
        with tempfile.TemporaryDirectory() as cache, borders(cache=cache, fetcher=pages), \
                mock.patch.object(regionaldata, "_write", write), self.assertLogs("germany", "WARNING"):
            def bands():
                return pages.asked.count(countrybounds.FILE)
            self.assertEqual(self.ask(), [False])
            self.assertEqual(bands(), 1)
            for k, wait in enumerate((60, 120, 240)):
                self.now[0] += wait - 1.0
                self.ask()
                germany.signature()
                self.assertEqual(bands(), k + 1, f"(no download before {wait} s)")
                self.now[0] += 1.0
                self.ask()
                self.assertEqual(bands(), k + 2)
            full[0] = False
            self.now[0] += 480
            self.assertEqual(self.ask(), [True])
            self.assertEqual(bands(), 5)
            self.assertEqual(germany._failed[1], 0)


class SignatureTest(unittest.TestCase):
    def test_the_fixture_and_absent_differ(self):
        here = germany.signature()
        self.assertEqual(here, countrybounds.signature())
        self.assertFalse(here.endswith(germany.ABSENT))
        with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
            self.assertEqual(germany.signature(), here + germany.ABSENT)


if __name__ == "__main__":
    unittest.main()
