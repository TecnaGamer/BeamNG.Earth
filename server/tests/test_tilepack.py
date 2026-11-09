"""tilepack.py and the imagery pack sources (cdn.py: the project's tile host): the format round trip, and a pack source
fetching a whole pack in one request from a local HTTP server, every tile of it on disk after, a missing pack every
tile a miss; cdn.chain puts the source ahead of USGS only where its switch says so."""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cdn  # noqa: E402
import imagery  # noqa: E402
import netfetch  # noqa: E402
import tilepack  # noqa: E402


def jpeg(colour):
    buf = io.BytesIO()
    Image.fromarray(np.full((256, 256, 3), colour, np.uint8)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FormatTest(unittest.TestCase):
    def test_round_trip_and_indices(self):
        tiles = {0: b"a", 5: b"bcd", 63: b"e" * 10}
        z, px, py, k, out = tilepack.parse(tilepack.build(12, 7, 9, 3, tiles))
        self.assertEqual((z, px, py, k), (12, 7, 9, 3))
        self.assertEqual(out, tiles)
        (pz, ppx, ppy), i = tilepack.pack_of(12, 7 * 8 + 5, 9 * 8 + 0, 3)
        self.assertEqual(((pz, ppx, ppy), i), ((12, 7, 9), 5))
        self.assertEqual(tilepack.tile_at(12, 7, 9, 3, 5), (12, 61, 72))
        with self.assertRaises(tilepack.PackError):
            tilepack.parse(b"nope")


class PackSourceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="packsrv_")
        p = os.path.join(self.root, "s2wc", "v1", tilepack.path(12, 10, 20))
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as fh:
            fh.write(tilepack.build(12, 10, 20, 3, {0: jpeg(120), 9: jpeg(60)}))
        root = self.root
        self.hits = []
        hits = self.hits

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=root, **kw)

            def log_request(self, *a):
                hits.append(self.path)

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.src = imagery.Source("packtest", base + "/s2wc/v1/{z}/{px}/{py}.bngp", 12, "test", pack=3,
                                  max_view_zoom=12)
        imagery.SOURCES["packtest"] = self.src
        self.addCleanup(imagery.SOURCES.pop, "packtest", None)
        self.cache = tempfile.mkdtemp(prefix="packcache_")
        pol = {"127.0.0.1": netfetch.HostPolicy(rate=100.0, burst=100.0, max_conc=4, daily_budget=1000,
                                                allow_prefetch=True, offline_ok=True, binary=True)}
        self.im = imagery.Imagery(self.cache, ["packtest"], fetcher=netfetch.Fetcher(policies=pol))

    def test_one_request_for_the_pack(self):
        a = (self.src, 12, 80, 160)          # index 0 of pack (12, 10, 20)
        b = (self.src, 12, 81, 161)          # index 9
        c = (self.src, 12, 82, 160)          # index 2: no tile in the pack
        got = self.im.tiles([a])
        self.assertIsNotNone(got[a])
        self.assertTrue(abs(int(got[a].rgb.mean()) - 120) < 4)
        got = self.im.tiles([b, c])
        self.assertTrue(abs(int(got[b].rgb.mean()) - 60) < 4)
        self.assertIsNone(got[c])
        self.assertEqual(len([h for h in self.hits if h.endswith(".bngp")]), 1)

    def test_missing_pack_is_a_miss_for_every_tile(self):
        k = (self.src, 12, 8 * 11, 8 * 20)   # pack (12, 11, 20) is not there
        self.assertIsNone(self.im.tiles([k])[k])
        k2 = (self.src, 12, 8 * 11 + 3, 8 * 20 + 4)
        self.assertIsNone(self.im.tiles([k2])[k2])
        self.assertEqual(len([h for h in self.hits if h.endswith(".bngp")]), 1)


class SwitchTest(unittest.TestCase):
    def test_chain_by_switch(self):
        d = tempfile.mkdtemp(prefix="cdnsw_")
        os.makedirs(os.path.join(d, "cdn"))
        names = ["md6in", "naip", "usgs", "eox2023"]
        for doc, want in (({"imagery": {"bngs2": {"enabled": True}}}, ["md6in", "naip", "bngs2", "usgs", "eox2023"]),
                          ({"imagery": {"bngs2": {"enabled": False}}}, names), ({}, names)):
            with open(os.path.join(d, "cdn", "cdn.json"), "w") as fh:
                json.dump(doc, fh)
            self.assertEqual(list(cdn.chain(names, None, d)), want)


if __name__ == "__main__":
    unittest.main()
