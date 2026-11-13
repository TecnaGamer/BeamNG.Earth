"""The 1 m aerial packs (aerialpacks.py, imagery source "bngaer"): a pack the coverage file lists is fetched in one
request at its generation and its AVIF tiles decoded; a pack it does not list is never asked for (the next source
draws it) and its cell asked to be built once; the lazy sources' exports are left out where the packs draw the place;
cdn.chain puts the source first only when the switch (or BNGE_AERIAL) says so; the Sentinel-2 packs' per-zoom
latitude limit (imagery.zoom_caps)."""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aerialpacks  # noqa: E402
import cdn  # noqa: E402
import fast  # noqa: E402
import imagery  # noqa: E402
import netfetch  # noqa: E402
import tilepack  # noqa: E402

HAVE = fast.AVAILABLE and getattr(fast._ef, "VERSION", 0) >= aerialpacks.NATIVE and aerialpacks.avif_ok()
CELL = (588, 780)                    # Baltimore (Fells Point), region 18, 24
PACK = (CELL[0] * 8 + 2, CELL[1] * 8 + 5)    # its zoom-17 pack listed below (bit 5 * 8 + 2)
OTHER = (CELL[0] * 8 + 3, CELL[1] * 8 + 5)   # one it does not list


def avif(colour):
    buf = io.BytesIO()
    Image.fromarray(np.full((256, 256, 3), colour, np.uint8)).save(buf, "AVIF", quality=60, subsampling="4:4:4")
    return buf.getvalue()


@unittest.skipUnless(HAVE, f"earthfast {aerialpacks.NATIVE} and Pillow with AVIF")
class AerialSourceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aersrv_")
        base = os.path.join(self.root, "aerial", "v1")
        p = os.path.join(base, "g3", tilepack.path(17, *PACK))
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as fh:
            fh.write(tilepack.build(17, PACK[0], PACK[1], 3, {0: avif(130), 63: avif(40)}))
        os.makedirs(os.path.join(base, "cov", "18"))
        with open(os.path.join(base, "cov", "18", "24.json"), "w") as fh:
            json.dump({"format": 1, "region": [18, 24],
                       "cells": {f"{CELL[0]},{CELL[1]}": {"g": 3, "p": f"{1 << (5 * 8 + 2):016x}"},
                                 f"{CELL[0] + 1},{CELL[1]}": {"g": 0}}}, fh)
        root, hits, asked = self.root, [], []
        self.hits, self.asked = hits, asked

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=root, **kw)

            def do_POST(self):
                asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'{"state":"queued"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_request(self, *a):
                hits.append(self.path)

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.src = imagery.Source("aertest", url + "/aerial/v1/g{g}/{z}/{px}/{py}.bngp", 17, "test", min_zoom=14,
                                  pack=3, coverage=True)
        imagery.SOURCES["aertest"] = self.src
        self.addCleanup(imagery.SOURCES.pop, "aertest", None)
        self.cache = tempfile.mkdtemp(prefix="aercache_")
        pol = {"127.0.0.1": netfetch.HostPolicy(rate=100.0, burst=100.0, max_conc=4, daily_budget=1000,
                                                allow_prefetch=True, offline_ok=True, binary=True)}
        os.environ["BNGE_AERIAL_REQUEST_URL"] = url + "/aerial/request"
        self.addCleanup(os.environ.pop, "BNGE_AERIAL_REQUEST_URL", None)
        aerialpacks._COV.clear()
        self.addCleanup(aerialpacks._COV.clear)
        self.im = imagery.Imagery(self.cache, ["aertest", "naip"], fetcher=netfetch.Fetcher(policies=pol))

    def packs(self):
        return [h for h in self.hits if h.endswith(".bngp")]

    def test_listed_pack_one_request(self):
        a = (self.src, 17, PACK[0] * 8, PACK[1] * 8)            # index 0
        b = (self.src, 17, PACK[0] * 8 + 7, PACK[1] * 8 + 7)    # index 63
        c = (self.src, 17, PACK[0] * 8 + 1, PACK[1] * 8)        # no tile there
        got = self.im.tiles([a, b, c])
        self.assertTrue(abs(int(got[a].rgb.mean()) - 130) < 4)
        self.assertTrue(abs(int(got[b].rgb.mean()) - 40) < 4)
        self.assertIsNone(got[c])
        self.assertEqual(self.packs(), [f"/aerial/v1/g3/17/{PACK[0]}/{PACK[1]}.bngp"])
        # its zoom-16 parent pack is listed too (built where any zoom-17 pack under it is); the cell's other half not
        cov = self.im.aerial()
        self.assertEqual(cov.state(16, PACK[0] >> 1, PACK[1] >> 1), (1, 3))
        self.assertEqual(cov.state(16, (PACK[0] >> 1) + 1, PACK[1] >> 1), (0, 0))
        self.assertEqual(cov.state(17, CELL[0] * 8 + 8, CELL[1] * 8), (2, 0))     # the next cell: no photos

    def test_unlisted_pack_never_asked_and_build_requested_once(self):
        import aerialpacks
        consent = aerialpacks.consent
        aerialpacks.consent = lambda: False         # the player's statistics off: no build request at all
        self.addCleanup(setattr, aerialpacks, "consent", consent)
        aerialpacks.consent = lambda: True          # (statistics on: as below)
        k = (self.src, 17, OTHER[0] * 8, OTHER[1] * 8)
        k2 = (self.src, 17, OTHER[0] * 8 + 3, OTHER[1] * 8 + 2)
        self.assertIsNone(self.im.tiles([k])[k])
        self.assertIsNone(self.im.tiles([k2])[k2])
        self.assertEqual(self.packs(), [])
        for _ in range(50):
            if self.asked:
                break
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual(self.asked, [{"v": 1, "cell": list(CELL)}])
        # a cell with no photos is never asked for
        k3 = (self.src, 17, (CELL[0] + 1) * 64, CELL[1] * 64)
        self.assertIsNone(self.im.tiles([k3])[k3])
        time.sleep(0.2)
        self.assertEqual(len(self.asked), 1)

    def test_probe_status_and_lazy_keys(self):
        cov = self.im.aerial()
        self.assertEqual(cov.state(17, *PACK, wait=5.0), (1, 3))
        with self.im._lock:
            self.assertEqual(self.im._probe_status((self.src, 17, PACK[0] * 8, PACK[1] * 8)), "covered")
            self.assertEqual(self.im._probe_status((self.src, 17, OTHER[0] * 8, OTHER[1] * 8)), "outside")
        # NAIP's exports (lazy, 4 x 4 tiles each) are left out under the listed pack, kept under the other one
        naip = imagery.SOURCES["naip"]
        lat, lon = _tile_centre(17, PACK[0] * 8 + 4, PACK[1] * 8 + 4)
        keys = self.im.route_keys([lat], [lon], 50.0, 1.0)
        self.assertFalse([k for k in keys if k[0] is naip and (k[2] >> 3, k[3] >> 3) == PACK])
        lat2, lon2 = _tile_centre(17, OTHER[0] * 8 + 4, OTHER[1] * 8 + 4)
        keys2 = self.im.route_keys([lat2], [lon2], 50.0, 1.0)
        self.assertTrue([k for k in keys2 if k[0] is naip and (k[2] >> 3, k[3] >> 3) == OTHER])


def _tile_centre(z, x, y):
    import math
    n = 2 ** z
    lon = (x + 0.5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lat, lon


@unittest.skipUnless(HAVE, f"earthfast {aerialpacks.NATIVE} and Pillow with AVIF")
class NoRequestsTest(unittest.TestCase):
    def test_off(self):
        c = aerialpacks.Coverage(netfetch.Fetcher(offline=True), "http://127.0.0.1:9/aerial/v1",
                                 tempfile.mkdtemp(prefix="aercov_"), "off")
        self.assertFalse(c.ask_build(17, *OTHER))

    def test_offline_unknown(self):
        c = aerialpacks.Coverage(netfetch.Fetcher(offline=True), "http://127.0.0.1:9/aerial/v1",
                                 tempfile.mkdtemp(prefix="aercov_"), "off")
        self.assertIsNone(c.state(17, *PACK, wait=2.0))     # no file, no network: unknown (the live sources)
        self.assertEqual(c.state(13, 1, 1), (3, 0))


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cdnsw_")
        os.makedirs(os.path.join(self.d, "cdn"))
        self.addCleanup(os.environ.pop, imagery.S2_MAX_ZOOM_ENV, None)
        self.addCleanup(os.environ.pop, imagery.S2_MAX_LAT_ENV, None)
        self.addCleanup(imagery.set_s2_max_zoom, 12, {})
        self.addCleanup(os.environ.pop, "BNGE_AERIAL", None)

    def chain(self, doc, names=("md6in", "naip", "usgs", "eox2023")):
        with open(os.path.join(self.d, "cdn", "cdn.json"), "w") as fh:
            json.dump(doc, fh)
        return list(cdn.chain(list(names), None, self.d))

    def test_aerial_first_by_switch(self):
        os.environ.pop("BNGE_AERIAL", None)
        on = self.chain({"imagery": {"bngaer": {"enabled": True}}})
        self.assertEqual(on, (["bngaer"] if aerialpacks.avif_ok() else []) + ["md6in", "naip", "usgs", "eox2023"])
        self.assertEqual(self.chain({"imagery": {"bngaer": {"enabled": False}}}), ["md6in", "naip", "usgs", "eox2023"])
        os.environ["BNGE_AERIAL"] = "off"
        self.assertNotIn("bngaer", self.chain({"imagery": {"bngaer": {"enabled": True}}}))

    def test_s2_max_lat(self):
        self.chain({"imagery": {"bngs2": {"enabled": True, "max_zoom": 14, "max_lat": 50}}})
        s = imagery.SOURCES["bngs2"]
        self.assertEqual((s.max_zoom, s.zoom_max_lat), (14, ((14, 50.0),)))
        self.assertEqual(imagery.zoom_caps(s, 40.0), (14, 14))
        self.assertEqual(imagery.zoom_caps(s, -52.0), (13, 13))
        self.assertEqual(os.environ[imagery.S2_MAX_LAT_ENV], "14:50.0")
        self.chain({"imagery": {"bngs2": {"enabled": True, "max_zoom": 14, "max_lat": {"14": 50, "13": 70}}}})
        s = imagery.SOURCES["bngs2"]
        self.assertEqual(imagery.zoom_caps(s, 60.0), (13, 13))
        self.assertEqual(imagery.zoom_caps(s, 75.0), (12, 12))
        usgs = imagery.SOURCES["usgs"]                  # (no limits: as before)
        self.assertEqual(imagery.zoom_caps(usgs, 60.0), (usgs.max_zoom, usgs.max_view_zoom))



class ConsentTest(unittest.TestCase):
    def test_no_build_request_without_the_players_statistics_choice(self):
        import aerialpacks
        import telemetry
        c = object.__new__(aerialpacks.Coverage)
        c.request_url = "http://127.0.0.1:9/never"      # (a request would fail loudly: none must be made)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "settings"))
            old = os.environ.get("BNGE_USER_DIR")
            os.environ["BNGE_USER_DIR"] = d
            self.addCleanup(lambda: os.environ.pop("BNGE_USER_DIR", None) if old is None
                            else os.environ.__setitem__("BNGE_USER_DIR", old))
            self.assertFalse(aerialpacks.consent())      # no choice made yet
            self.assertFalse(c.ask_build(17, 1, 1))
            on = telemetry.ON
            self.addCleanup(setattr, telemetry, "ON", on)
            telemetry.ON = True
            for name in ("beamngearth.json", "beamngEarth.json"):
                with open(os.path.join(d, "settings", name), "w") as f:
                    json.dump({"telemetryChoice": telemetry.CONSENT_VERSION, "telemetry": False}, f)
            self.assertFalse(aerialpacks.consent())      # chose: off
            for name in ("beamngearth.json", "beamngEarth.json"):
                with open(os.path.join(d, "settings", name), "w") as f:
                    json.dump({"telemetryChoice": telemetry.CONSENT_VERSION, "telemetry": True}, f)
            self.assertTrue(aerialpacks.consent())       # chose: on
            telemetry.ON = False
            self.assertFalse(aerialpacks.consent())      # BNE_TELEMETRY=off


class ZoomCapsTest(unittest.TestCase):
    def test_sources_without_a_latitude_limit_unchanged(self):
        import imagery
        for name, src in imagery.SOURCES.items():
            if not src.zoom_max_lat:
                for lat in (0.0, 45.0, 70.0):
                    self.assertEqual(imagery.zoom_caps(src, lat), (src.max_zoom, src.max_view_zoom), name)

    def test_limit_drops_both_zooms(self):
        import imagery
        from dataclasses import replace
        src = replace(imagery.SOURCES["bngs2"], max_zoom=14, max_view_zoom=14, zoom_max_lat=((14, 50.0),))
        self.assertEqual(imagery.zoom_caps(src, 40.0), (14, 14))
        self.assertEqual(imagery.zoom_caps(src, 60.0), (13, 13))

if __name__ == "__main__":
    unittest.main()
