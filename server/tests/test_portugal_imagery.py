"""DGT WMS through the real imagery cache, fallback chain and map consumer."""
import io
import math
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geodesy
import imagery
import maptiles
import netfetch


class DgtTransport:
    def __init__(self, newer=True, older=True):
        self.newer, self.older, self.requests = newer, older, []

    def request(self, url, headers, timeout):
        query = parse_qs(urlsplit(url).query)
        if "dgterritorio" in url:
            layer = query["LAYERS"][0]
            assert query["FORMAT"] == ["image/png"]
            assert query["SRS"] == ["EPSG:3857"]
            size = int(query["WIDTH"][0])
            available = self.newer if layer == "Ortos2021-RGB" else self.older
            colour = (50, 100, 150) if layer == "Ortos2021-RGB" else (150, 100, 50)
            self.requests.append(layer)
        else:
            size, available, colour = 256, True, (50, 150, 100)
            self.requests.append("spain")
        pixels = np.full((size, size, 3), 255, np.uint8)
        if available:
            pixels[:] = colour
            pixels[::2, ::2] += 4
        output = io.BytesIO()
        Image.fromarray(pixels).save(output, "PNG")
        return 200, {}, output.getvalue()


class PortugalImageryTest(unittest.TestCase):
    def make(self, newer=True, older=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        transport = DgtTransport(newer, older)
        policies = {h: replace(p, rate=1e9, burst=1e9) for h, p in netfetch.POLICIES.items()}
        fetcher = netfetch.Fetcher(transport=transport, policies=policies, sleep=lambda _: None)
        im = imagery.Imagery(temp.name, ("pt_dgt2021", "pt_dgt2018", "spain"), fetcher=fetcher, tone=0)
        self.addCleanup(im.close)
        return im, transport

    def warm(self, im, lat=38.67254, lon=-9.15231):
        frame = geodesy.EnuFrame(lat, lon)
        im.warm(frame, [(0, 0)], 150, 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with im._warm_cv:
                if all(im._warm_done(k) for k in im._warm_want):
                    return frame
            time.sleep(.01)
        self.fail("DGT warm did not finish")

    def map_source(self, im, lat, lon):
        z = 17
        x = (lon + 180) / 360 * (1 << z)
        y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (1 << z)
        got = maptiles.MapTiles(im).tile(z, int(x), int(y))
        self.assertIsNotNone(got)
        return got[1]

    def test_newer_photos_are_used_by_ground_and_map(self):
        im, transport = self.make()
        frame = self.warm(im, 41.1413, -8.611)
        _, used = im._render(imagery.EnuSquare(frame, -64, -64, 128), 128)
        self.assertIn("pt_dgt2021", used)
        self.assertEqual(self.map_source(im, 41.1413, -8.611), "pt_dgt2021")
        self.assertIn("Ortos2021-RGB", transport.requests)

    def test_blank_northern_flights_fall_back_to_2018_and_stay_cached(self):
        im, transport = self.make(newer=False)
        self.warm(im, 39.74, -8.81)                  # Leiria: inside the 2021 box, here without its imagery
        self.assertEqual(self.map_source(im, 39.74, -8.81), "pt_dgt2018")
        self.assertIn("Ortos2021-RGB", transport.requests)
        before = len(transport.requests)
        self.assertEqual(self.map_source(im, 39.74, -8.81), "pt_dgt2018")
        self.assertEqual(len(transport.requests), before)

    def test_the_south_never_asks_the_northern_flights(self):
        """Lisbon, south of the 2021 flights: straight to 2018, no empty 2021 export first."""
        im, transport = self.make()
        self.warm(im)
        self.assertEqual(self.map_source(im, 38.67254, -9.15231), "pt_dgt2018")
        self.assertNotIn("Ortos2021-RGB", transport.requests)

    def test_empty_portuguese_coverage_allows_spanish_photos(self):
        im, _ = self.make(newer=False, older=False)
        self.warm(im, 38.88, -6.97)
        self.assertEqual(self.map_source(im, 38.88, -6.97), "spain")

    def test_no_dgt_request_outside_advertised_bounds(self):
        im, transport = self.make()
        self.warm(im, 40.4168, -3.7038)
        self.assertEqual(self.map_source(im, 40.4168, -3.7038), "spain")
        self.assertFalse(any(r.startswith("Ortos") for r in transport.requests))

    def test_default_order_and_shared_host_budget(self):
        chain = imagery.DEFAULT_CHAIN
        self.assertLess(chain.index("pt_dgt2021"), chain.index("pt_dgt2018"))
        self.assertLess(chain.index("pt_dgt2018"), chain.index("spain"))
        self.assertEqual(netfetch.POLICIES["cartografia.dgterritorio.gov.pt"].max_conc, 1)
