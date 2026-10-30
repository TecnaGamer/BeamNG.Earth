"""server/imagery.py (docs/V1_3_SPEC.md §A): reprojection (a known lat/lon lands on the right
texel), the source chain with coverage detection (404 / blank tiles), the disk cache with
remembered 404s (and their TTL), unreachable sources (netfetch circuit breakers), prefetch
priorities, the tone curve and the DXT1 DDS with its mip chain. Synthetic tile servers behind a
netfetch.Fetcher with a fake transport, no network, except NetworkImageryTest (a few real tiles,
cached in <tmp>/beamng-earth-test-cache/imagery; skipped when offline)."""
import io
import math
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from concurrent import futures
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy as g  # noqa: E402
import imagery  # noqa: E402
import netfetch  # noqa: E402

LAT0, LON0 = 36.5785, -118.2923
CACHE = os.path.join(tempfile.gettempdir(), "beamng-earth-test-cache", "imagery")
URL = re.compile(r"/(\d+)/(\d+)/(\d+)(?:\.jpg)?$")


def png(a):
    b = io.BytesIO()
    Image.fromarray(np.asarray(a, np.uint8), "RGB").save(b, "PNG")
    return b.getvalue()


def background(z, x, y):
    """A non-uniform dark pattern (so a tile is never 'blank')."""
    i = np.arange(256)
    return np.stack(np.broadcast_arrays(np.full((256, 256), 20), (i[None, :] + x) % 40 + 10,
                                        (i[:, None] + y) % 40 + 10), -1).astype(np.uint8)


# The real host policies without their rates (the tests count requests, not seconds); retry delays
# are not slept (FakeNet's fetcher sleeps nothing).
FAST = {h: replace(p, rate=1e9, burst=1e9) for h, p in netfetch.POLICIES.items()}


class ServeTransport:
    """netfetch transport over a function source_name -> (z, x, y) -> (status, body); logs
    (name, z, x, y) of every request into `requests`."""

    def __init__(self, serve, requests):
        self.serve, self.requests = serve, requests
        self.headers = []

    def request(self, url, headers, timeout):
        name = next(n for n, s in imagery.SOURCES.items() if url.startswith(s.url.split("{z}")[0]))
        z, a, b = (int(v) for v in URL.search(url).groups())
        x, y = b, a                                         # both are .../{z}/{y}/{x}
        self.requests.append((name, z, x, y))
        self.headers.append(headers)
        status, body = self.serve(name, z, x, y)
        return status, {}, body


class FakeNet(imagery.Imagery):
    """Imagery whose network is a function source_name -> (z, x, y) -> (status, body), behind a
    netfetch.Fetcher (host policies without rate limits unless `policies` is given)."""

    def __init__(self, cache_dir, serve, chain=imagery.BASE_CHAIN, policies=None, clock=None, **kw):
        self.requests = []
        self.transport = ServeTransport(serve, self.requests)
        net = netfetch.Fetcher(transport=self.transport, sleep=lambda s: None, policies=policies or FAST,
                               **({"clock": clock} if clock else {}))
        super().__init__(cache_dir, chain, fetcher=net, **kw)
        self.serve = serve


class ReprojectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def landmark_server(self, lat, lon):
        """Tiles with a 3 x 3 white mark centred on the pixel that contains (lat, lon)."""
        def serve(name, z, x, y):
            if name != "usgs":
                return 404, b""
            a = background(z, x, y)
            fx, fy = imagery.mercator_px(lat, lon, z)
            px, py = int(math.floor(float(fx))) - 256 * x, int(math.floor(float(fy))) - 256 * y
            a[max(0, py - 1):max(0, py + 2), max(0, px - 1):max(0, px + 2)] = 255
            return 200, png(a)
        return serve

    def check(self, frame, e, n, x0, y0, size, px):
        lat, lon, h = frame.enu_to_geodetic(e, n, 0.0)
        im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), self.landmark_server(float(lat), float(lon)),
                     chain=("usgs",), tone=0.0)
        rgb, used = im.render(frame, x0, y0, size, px)
        self.assertEqual(set(used), {"usgs"})
        t = size / px
        # The mark is centred on the source pixel containing the landmark: map that pixel's
        # centre back (inverse Mercator, then into the frame) to get where it must appear.
        z = max(r[1] for r in im.requests)
        W = 256 * 2 ** z
        fx, fy = imagery.mercator_px(float(lat), float(lon), z)
        cx_px, cy_px = math.floor(float(fx)) + 0.5, math.floor(float(fy)) + 0.5
        clon = cx_px / W * 360.0 - 180.0
        clat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy_px / W))))
        ce, cn, _ = frame.geodetic_to_enu(clat, clon, float(h))
        col, row = (ce - x0) / t - 0.5, (y0 + size - cn) / t - 0.5      # texel centres at +0.5
        # (above the background's 20 by more than the finished tiles' JPEG ringing round the mark: imagery.FIN_QUALITY)
        lum = rgb[..., 0].astype(float) - 40
        w = np.clip(lum, 0, None)
        rr, cc = np.mgrid[0:px, 0:px]
        cy, cx = (w * rr).sum() / w.sum(), (w * cc).sum() / w.sum()
        # The 3 x 3 mark is symmetric around that centre, so its centroid sits on it (a
        # half-pixel convention error would move it by ~0.5 source pixel = ~1 m here).
        self.assertLess(math.hypot((cx - col) * t, (cy - row) * t), 0.2, (cx, cy, col, row))
        self.assertTrue(abs(cx * t - (e - x0)) < 2.5 and abs(cy * t - (y0 + size - n)) < 2.5)
        return im

    def test_landmark_on_its_texel(self):
        f = g.EnuFrame(LAT0, LON0)
        im = self.check(f, 123.4, -56.7, 64.0, -128.0, 128.0, 128)
        self.assertEqual({r[1] for r in im.requests}, {10, 12, 16})    # 1 m texels: max zoom (+ probes)
        # a far-off footprint in another frame (curvature / meridian convergence matter there)
        f2 = g.EnuFrame(LAT0 + 0.15, LON0 - 0.2)
        self.check(f2, 17890.0, -16720.0, 17856.0, -16768.0, 64.0, 64)

    def test_orientation(self):
        """Row 0 is north, column 0 is west: a mark north-east of the centre shows up top right."""
        f = g.EnuFrame(LAT0, LON0)
        self.check(f, 50.0, 40.0, 0.0, 0.0, 64.0, 64)

    def test_zoom_choice(self):
        self.assertEqual(imagery.zoom_for(LAT0, imagery.ground_res(LAT0, 15)), 15)
        self.assertEqual(imagery.zoom_for(LAT0, imagery.ground_res(LAT0, 9) * 1.3), 9)
        self.assertEqual(imagery.zoom_for(0.0, 0.01), 19)


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def colour(name):
        return {"usgs": (200, 30, 30), "eox2023": (30, 200, 30), "eox2016": (30, 30, 200)}[name]

    def tile(self, name, z, x, y, uniform=False):
        a = np.zeros((256, 256, 3), np.uint8) + np.array(self.colour(name), np.uint8)
        if not uniform:
            a[::2, ::2] += 6
        return 200, png(a)

    def render(self, im, x0=0.0):
        rgb, used = im.render(g.EnuFrame(LAT0, LON0), x0, 0.0, 256.0, 64)
        return rgb, used

    def test_fallback_on_404_and_blank(self):
        def serve(name, z, x, y):
            if name == "usgs":
                return (404, b"") if x % 2 else self.tile(name, z, x, y, uniform=True)   # white-tile style
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        rgb, used = self.render(im)
        self.assertEqual(set(used), {"eox2023"})
        self.assertLess(np.abs(rgb.reshape(-1, 3).mean(0) - (31.5, 201.5, 31.5)).max(), 2)
        # usgs: only its z10 coverage probe (blank / 404 here); eox at its max z14
        self.assertEqual({(r[0], r[1]) for r in im.requests}, {("usgs", 10), ("eox2023", 14)})

    def test_coverage_probe(self):
        """usgs tiles are only asked for where their z10 and z12 ancestors have data, and never
        below z10 (its global low-zoom basemap would differ from EOX on the far rings)."""
        def serve(name, z, x, y):
            if name == "usgs" and z == 12:
                return (404, b"") if self.outside else self.tile(name, z, x, y)
            return self.tile(name, z, x, y)
        for outside in (True, False):
            with self.subTest(outside=outside):
                self.outside = outside
                im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), serve, tone=0.0)
                _, used = self.render(im)
                zs = {(r[0], r[1]) for r in im.requests}
                if outside:     # the probes, no 404 storm at z15
                    self.assertEqual(zs, {("usgs", 10), ("usgs", 12), ("eox2023", 14)})
                    self.assertEqual(set(used), {"eox2023"})
                else:           # 4 m texels: usgs at z15 (3.9 m pixels here)
                    self.assertEqual(zs, {("usgs", 10), ("usgs", 12), ("usgs", 15)})
                    self.assertEqual(set(used), {"usgs"})
        # a far tile with 600 m texels (z8) never asks usgs
        im = FakeNet(tempfile.mkdtemp(dir=self.tmp.name), serve, tone=0.0)
        _, used = im.render(g.EnuFrame(LAT0, LON0), 0.0, 0.0, 153600.0, 256)
        self.assertEqual(set(used), {"eox2023"})
        self.assertFalse([r for r in im.requests if r[0] == "usgs"])

    def test_black_part_of_a_tile_from_the_next_source(self):
        """A regional photo ending mid-tile with black past its edge (New Jersey's over Manhattan, GitHub #251): the black
        part comes from the next source, the rest of the tile stays."""
        def serve(name, z, x, y):
            if name == "usgs" and z >= 15:
                a = np.zeros((256, 256, 3), np.uint8) + np.array(self.colour(name), np.uint8)
                a[::2, ::2] += 6
                a[:, :128] = 0              # its west half outside the data
                return 200, png(a)
            return self.tile(name, z, x, y)
        rgb, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertIn("usgs", used)
        self.assertIn("eox2023", used, "the black part from the next source")
        self.assertFalse((rgb.reshape(-1, 3).max(1) <= imagery.NODATA_MAX).any(), "no black texels left")

    def test_nodata_mask(self):
        rng = np.random.default_rng(1)
        a = rng.integers(20, 200, (256, 256, 3), dtype=np.uint8)
        self.assertIsNone(imagery.nodata_mask(a))
        a[30, 30] = 0
        a[50:55, 50:55] = 2             # a speck and a small deep shadow: data
        self.assertIsNone(imagery.nodata_mask(a))
        a[100:180, :60] = 0
        m = imagery.nodata_mask(a)
        self.assertTrue(m[100:180, :60].all())
        self.assertTrue(m[100:180, 60:62].all(), "its JPEG fringe too")
        self.assertFalse(m[100:180, 62:].any())
        self.assertFalse(m[50:55, 50:55].any())

    def test_blank_last_source_is_used(self):
        """EOX draws the open ocean as one uniform colour: accepted from the last source."""
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y, uniform=True)
        rgb, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertEqual(set(used), {"eox2016"})
        self.assertTrue((rgb == self.colour("eox2016")).all())

    def test_nothing_anywhere(self):
        rgb, used = self.render(FakeNet(self.tmp.name, lambda *a: (404, b""), tone=0.0))
        self.assertEqual(set(used), {"none"})
        self.assertTrue((rgb == imagery.FALLBACK_RGB).all())

    def test_disk_cache_and_remembered_404(self):
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        a, _ = self.render(im)
        n = len(im.requests)
        self.assertTrue(n)
        b, _ = self.render(im)                     # memory LRU: no requests
        self.assertEqual(len(im.requests), n)

        def offline(*a):
            raise AssertionError("network used")
        im2 = FakeNet(self.tmp.name, offline, tone=0.0)
        c, used = self.render(im2)                 # disk: tiles and 404 markers
        self.assertEqual(im2.requests, [])
        self.assertTrue((a == b).all() and (a == c).all())
        self.assertEqual(set(used), {"eox2023"})
        stats = im2.stats_snapshot()
        self.assertGreater(stats.get("finished:disk", 0), 0)    # (the finished tiles: no raw tile read again)
        markers = [f for _, _, fs in os.walk(os.path.join(self.tmp.name, "usgs")) for f in fs if f.endswith(".404")]
        self.assertTrue(markers)

    def test_unreachable_source_falls_back(self):
        def serve(name, z, x, y):
            if name == "usgs":
                return 503, b"busy"
            return self.tile(name, z, x, y)
        rgb, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertEqual(set(used), {"eox2023"})

    def test_all_unreachable_raises(self):
        def serve(*a):
            raise OSError("network down")
        with self.assertRaises(imagery.ImageryError):
            self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        # nothing was remembered as missing
        self.assertFalse([f for _, _, fs in os.walk(self.tmp.name) for f in fs])

    def test_unreachable_source_backs_off(self):
        """After failed attempts open the host's circuit, the source is skipped (no requests, no retry
        delays) until the open time ran out; then one probe, and it is used again."""
        up = {"usgs": False}
        clock = [1000.0]

        def serve(name, z, x, y):
            if name == "usgs" and not up["usgs"]:
                raise OSError("network down")
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0, clock=lambda: clock[0])
        pol = netfetch.POLICIES["basemap.nationalmap.gov"]
        for x0 in (0.0, 2000.0):            # 3 attempts per tile: the second texture opens the circuit
            _, used = self.render(im, x0=x0)
            self.assertEqual(set(used), {"eox2023"})
        n = sum(1 for r in im.requests if r[0] == "usgs")
        self.assertEqual(n, pol.breaker_threshold)     # the 6th attempt met the open circuit
        self.assertTrue(im.fetcher.host("basemap.nationalmap.gov").breaker.is_open())
        _, used = self.render(im, x0=5000.0)            # other source tiles: skipped, no requests
        self.assertEqual(set(used), {"eox2023"})
        self.assertEqual(sum(1 for r in im.requests if r[0] == "usgs"), n)
        self.assertGreater(im.stats_snapshot().get("usgs:skipped", 0), 0)
        self.assertEqual(im.fetcher.unavailable_hosts(), ["basemap.nationalmap.gov"])
        up["usgs"] = True
        clock[0] += pol.breaker_open + 1                # the open time has run out
        _, used = self.render(im, x0=10000.0)
        self.assertEqual(set(used), {"usgs"})
        self.assertEqual(im.fetcher.unavailable_hosts(), [])

    def test_open_eox_host_skips_both_layers(self):
        """eox2023 and eox2016 share one host and so one breaker: while it is open neither is asked
        and the next source of the chain fills the texture."""
        def serve(name, z, x, y):
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, chain=("eox2023", "eox2016", "usgs"), tone=0.0)
        im.fetcher.host("tiles.maps.eox.at").breaker.trip(60.0)
        _, used = self.render(im)
        self.assertEqual(set(used), {"usgs"})
        self.assertFalse([r for r in im.requests if r[0] != "usgs"])
        st = im.stats_snapshot()
        self.assertGreater(st.get("eox2023:skipped", 0), 0)
        self.assertGreater(st.get("eox2016:skipped", 0), 0)

    def test_400_is_not_a_miss(self):
        """Only 404 means "no coverage": a 400 (an overloaded ArcGIS server) leaves no marker."""
        def serve(name, z, x, y):
            return (400, b"bad request") if name == "usgs" else self.tile(name, z, x, y)
        _, used = self.render(FakeNet(self.tmp.name, serve, tone=0.0))
        self.assertEqual(set(used), {"eox2023"})
        self.assertFalse([f for _, _, fs in os.walk(self.tmp.name) for f in fs if f.endswith(".404")])

    def test_miss_marker_ttl(self):
        """A .404 marker is valid for the host's miss_ttl (90 days): older ones are asked again. An
        old-style marker (an empty file from before TTLs) works the same way."""
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        self.render(im)
        markers = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.tmp.name, "usgs"))
                   for f in fs if f.endswith(".404")]
        self.assertTrue(markers)
        n = len(im.requests)
        im2 = FakeNet(self.tmp.name, serve, tone=0.0)
        self.render(im2)
        self.assertEqual(sum(1 for r in im2.requests if r[0] == "usgs"), 0)     # fresh markers
        old = time.time() - netfetch.POLICIES["basemap.nationalmap.gov"].miss_ttl - 60
        for m in markers:
            os.utime(m, (old, old))
        im3 = FakeNet(self.tmp.name, serve, tone=0.0)
        self.render(im3)
        self.assertTrue([r for r in im3.requests if r[0] == "usgs"])            # expired: asked again
        self.assertTrue(all(time.time() - os.path.getmtime(m) < 60 for m in markers if os.path.exists(m)))
        self.assertTrue(n)

    def test_prefetch_dropped_then_fetched_on_demand(self):
        """A prefetch the limiter drops (bucket below half) is not an error: render fetches the tile."""
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        pol = netfetch.with_policy(FAST, "basemap.nationalmap.gov", rate=1e-6, burst=60.0)
        im = FakeNet(self.tmp.name, serve, tone=0.0, policies=pol)
        b = im.fetcher.host("basemap.nationalmap.gov").bucket
        b.tokens = 29.0                                  # below half: no prefetch
        f = g.EnuFrame(LAT0, LON0)
        im.prefetch(f, 0.0, 0.0, 256.0, 64)
        time.sleep(0.2)
        self.assertEqual(im.requests, [])
        # a prefetch future that was submitted and then dropped: the demand fetch replaces it
        key = (imagery.SOURCES["usgs"], 10, 175, 408)
        with im._lock:
            fut = im._future(key, "prefetch")
        with self.assertRaises(imagery.ImageryError) as cm:
            fut.result()
        self.assertTrue(cm.exception.deferred)
        _, used = self.render(im)
        self.assertEqual(set(used), {"eox2023"})
        self.assertTrue([r for r in im.requests if r[0] == "usgs"])
        self.assertGreater(im.stats_snapshot().get("usgs:deferred", 0), 0)

    def test_prefetch_skips_an_open_host(self):
        """While USGS's circuit is open render fails over to EOX, so prefetch warms the EOX tiles
        instead of stopping at USGS (and asks USGS nothing)."""
        im = FakeNet(self.tmp.name, self.tile, tone=0.0)
        im.fetcher.host("basemap.nationalmap.gov").breaker.trip(60.0)
        im.prefetch(g.EnuFrame(LAT0, LON0), 0.0, 0.0, 256.0, 64)
        self.idle(im)
        self.assertEqual({r[0] for r in im.requests}, {"eox2023"})
        n = len(im.requests)
        _, used = self.render(im)
        self.assertEqual(set(used), {"eox2023"})
        self.assertEqual(len(im.requests), n)                   # render found everything prefetched

    def test_dropped_prefetch_refetched_once(self):
        """Two renders waiting on the same dropped prefetch future fetch the tile once, together (the
        dropped future may still be in _inflight: its done callback runs after the waiters wake)."""
        gate = threading.Event()

        def serve(name, z, x, y):
            gate.wait(5)
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, chain=("eox2023",), tone=0.0)
        key = (imagery.SOURCES["eox2023"], 14, 100, 200)
        stale = futures.Future()
        stale.set_exception(imagery.ImageryError("dropped", deferred=True))
        calls = []
        future = im._future

        def counted(*a, **kw):
            calls.append(a)
            return future(*a, **kw)
        im._future = counted
        with im._lock:
            im._inflight[key] = stale
        out = []
        threads = [threading.Thread(target=lambda: out.append(im._result(key, stale, "demand"))) for _ in range(2)]
        for t in threads:
            t.start()
        t0 = time.monotonic()
        while len(calls) < 2 and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(out), 2)
        self.assertIs(out[0], out[1])
        self.assertEqual(len(im.requests), 1)

    @staticmethod
    def idle(im, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with im._lock:
                if not im._inflight:
                    return
            time.sleep(0.01)

    def test_user_agent(self):
        def serve(name, z, x, y):
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        self.render(im)
        self.assertTrue(im.transport.headers)
        self.assertTrue(all(h == {"User-Agent": netfetch.USER_AGENT} for h in im.transport.headers))

    def test_undecodable_tile_is_an_error(self):
        def serve(name, z, x, y):
            return 200, b"<html>maintenance</html>"
        with self.assertRaises(imagery.ImageryError):
            self.render(FakeNet(self.tmp.name, serve, chain=("eox2023",), tone=0.0))
        self.assertFalse([f for _, _, fs in os.walk(self.tmp.name) for f in fs if f.endswith(".jpg")])

    def test_prefetch(self):
        def serve(name, z, x, y):
            return (404, b"") if name == "usgs" else self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, tone=0.0)
        f = g.EnuFrame(LAT0, LON0)
        im.prefetch(f, 0.0, 0.0, 256.0, 64)
        time.sleep(0.3)
        self.assertTrue(im.requests and all(r[0] == "usgs" for r in im.requests))
        im.prefetch(f, 0.0, 0.0, 256.0, 64)      # usgs known missing there: eox2023 next
        time.sleep(0.3)
        self.assertIn("eox2023", {r[0] for r in im.requests})
        n = len(im.requests)
        self.render(im)
        self.assertEqual(len(im.requests), n)     # render found everything prefetched

    def test_prefetch_soon_past_the_budget(self):
        """A host that takes no prefetch (past 80 % of its daily budget): the far field's next tiles (soon) still go,
        as demand; a plain prefetch does not (GitHub #116)."""
        def serve(name, z, x, y):
            return self.tile(name, z, x, y)
        im = FakeNet(self.tmp.name, serve, chain=("eox2023",), tone=0.0)
        im.fetcher.allow = lambda host, kind="prefetch": kind == "demand"
        f = g.EnuFrame(LAT0, LON0)
        im.prefetch(f, 0.0, 0.0, 256.0, 64)
        time.sleep(0.3)
        self.assertFalse(im.requests, "a plain prefetch past the budget: nothing")
        im.prefetch(f, 0.0, 0.0, 256.0, 64, soon=True)
        time.sleep(0.3)
        self.assertTrue(im.requests and all(r[0] == "eox2023" for r in im.requests))

    def test_parse_chain_and_attribution(self):
        self.assertIsNone(imagery.parse_chain("off"))
        self.assertEqual(imagery.parse_chain("usgs,eox2016"), ("usgs", "eox2016"))
        with self.assertRaises(ValueError):
            imagery.parse_chain("google")
        self.assertEqual(imagery.Imagery(self.tmp.name).attribution(), imagery.ATTRIBUTION)
        self.assertIn("CT ECO 2023 orthoimagery (CC0)", imagery.ATTRIBUTION, "the regional sources too")
        self.assertIn("CC BY 4.0", imagery.Imagery(self.tmp.name, ("eox2016",)).attribution())
        # EOX's required wording (June 2026, docs/research/2026-10-10-rate-limits.md)
        eox = "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus Sentinel data "
        self.assertEqual(imagery.SOURCES["eox2023"].attribution, eox + "2023), CC BY-NC-SA 4.0")
        self.assertEqual(imagery.SOURCES["eox2016"].attribution, eox + "2016 & 2017), CC BY 4.0")
        self.assertEqual(imagery.Imagery(self.tmp.name, imagery.BASE_CHAIN).attribution(),
                         "Imagery: State of Maryland, MD iMAP / DoIT six-inch imagery; "
                         "USDA NAIP (public domain); "
                         "USDA NAIP via USGS The National Map (public domain); "
                         "USGS The National Map (public domain); " + eox
                         + "2023), CC BY-NC-SA 4.0; " + eox + "2016 & 2017), CC BY 4.0")
        self.assertNotIn("Sentinel-2 cloudless", imagery.ATTRIBUTION)
        for n in imagery.DEFAULT_CHAIN:                     # the overlay's font is ASCII only
            imagery.SOURCES[n].attribution.encode("ascii")


class ExportTransport:
    """netfetch transport for an export source ("naip": bbox + size in the URL) and XYZ tiles for the rest:
    serve_export(z, x, y, size) -> (status, body) for the parent tile the bbox is; XYZ as ServeTransport."""
    BBOX = re.compile(r"bbox=([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+).*size=(\d+),")

    def __init__(self, serve_export, serve_xyz, requests, by_name=False):
        self.serve_export, self.serve_xyz, self.requests = serve_export, serve_xyz, requests
        self.by_name = by_name          # serve_export(z, x, y, size, source name)

    def request(self, url, headers, timeout):
        m = self.BBOX.search(url)
        if m:
            x0, y0, x1, y1, size = (float(v) for v in m.groups())
            z = round(math.log2(imagery.EARTH_CIRC / (x1 - x0)))
            tile = imagery.EARTH_CIRC / 2 ** z
            x, y = round((x0 + imagery.EARTH_CIRC / 2) / tile), round((imagery.EARTH_CIRC / 2 - y1) / tile)
            name = "naipusda" if "fpac.usda.gov" in url else "md6in" if "mdgeodata" in url else "naip"
            self.requests.append((name, z, x, y))
            status, body = (self.serve_export(z, x, y, int(size), name) if self.by_name
                            else self.serve_export(z, x, y, int(size)))
            return status, {}, body
        name = next(n for n, s in imagery.SOURCES.items() if url.startswith(s.url.split("{z}")[0]))
        z, a, b = (int(v) for v in URL.search(url).groups())
        self.requests.append((name, z, b, a))
        status, body = self.serve_xyz(name, z, b, a)
        return status, {}, body


class ExportImageryTest(unittest.TestCase):
    """The NAIP source: one exportImage per 4 x 4 tiles split on disk, lazy (a render never waits for it:
    USGS until its tiles are on disk), black areas count as missing, warm() fetches ahead."""
    LAT, LON = 39.2834, -76.5934        # Fells Point (the USGS probes say "covered" in the fakes)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.frame = g.EnuFrame(self.LAT, self.LON)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def child_colour(x, y):
        return (50 + x % 7 * 20, 60 + y % 5 * 30, 210)

    def make(self, black=False):
        def serve_export(z, x, y, size):
            n = size // 256
            a = np.zeros((size, size, 3), np.uint8)
            for j in range(n):
                for i in range(n):
                    a[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = self.child_colour(x * n + i, y * n + j)
            a[::2, ::2] += 3                # not uniform
            if black:
                a[:, : size // 2] = 0       # the west half outside the flights
            b = io.BytesIO()
            Image.fromarray(a).save(b, "JPEG", quality=95)
            return 200, b.getvalue()

        def serve_xyz(name, z, x, y):
            a = np.zeros((256, 256, 3), np.uint8) + np.array((200, 30, 30), np.uint8)
            a[::2, ::2] += 6
            return 200, png(a)
        self.requests = []
        net = netfetch.Fetcher(transport=ExportTransport(serve_export, serve_xyz, self.requests),
                               sleep=lambda s: None, policies=FAST)
        return imagery.Imagery(self.tmp.name, ("naip", "usgs"), fetcher=net, tone=0.0)

    def wait(self, im):
        for _ in range(200):
            with im._lock:
                busy = bool(im._inflight)
            if not busy:
                return
            time.sleep(0.02)

    def warm(self, im, points, radius=300.0):
        im.warm(self.frame, points, radius, 1.0)
        for _ in range(300):
            with im._warm_cv:
                left = [k for k in im._warm_want if not im._warm_done(k)]
            if not left:
                return left
            time.sleep(0.02)
        return left

    def test_warm_with_children_evicted(self):
        # the cache cap evicted a parent's last child but not its first: warming read the first one in a loop (it
        # was on disk, the key never done: 170 s of the server's CPU in a drive, 2026-10-20). Now the parent is
        # fetched again and the thread rests.
        im = self.make()
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        n = len([r for r in self.requests if r[0] == "naip"])
        keys = list(im._warm_want)
        for src, z, x, y in keys:
            last = (1 << src.export) - 1
            path, _ = im._paths(src, z, x | last, y | last)
            os.remove(path)
        self.assertTrue(keys and all(not im._warm_done(k) for k in keys))
        im._warm_want = []
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        self.assertEqual(len([r for r in self.requests if r[0] == "naip"]), n + len(keys), "each parent once more")
        reads = []
        orig = im.disk.read
        im.disk.read = lambda path: reads.append(path) or orig(path)
        time.sleep(0.3)
        self.assertEqual(reads, [], "the warm thread rests")

    def test_lazy_then_split_tiles(self):
        im = self.make()
        # 1 m texels: z17 (0.93 m pixels here), NAIP's zoom
        rgb, used = im.render(self.frame, 0.0, 0.0, 256.0, 256)
        self.assertEqual(set(used), {"usgs"})               # never waited for, never fetched by a render
        self.wait(im)
        self.assertFalse([r for r in self.requests if r[0] == "naip"])
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        naip = [r for r in self.requests if r[0] == "naip"]
        self.assertTrue(naip)
        self.assertTrue(all(r[1] == 15 for r in naip))      # parents two levels up
        rgb, used = im.render(self.frame, 0.0, 0.0, 256.0, 256)
        self.assertEqual(set(used), {"naip"})
        self.assertEqual(len([r for r in self.requests if r[0] == "naip"]), len(naip))   # no new fetch
        # each child is its own part of the parent image
        key = next(k for k in im._lru if k and k[0].name == "naip")
        tile = im._lru[key]
        self.assertLess(np.abs(tile.rgb.reshape(-1, 3).mean(0) - (np.array(self.child_colour(key[2], key[3])) + 0.75)).max(), 3)
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "naip", "17", str(key[2])))) % 4, 0)   # whole parents

    def test_black_is_missing(self):
        im = self.make(black=True)
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        _, used = im.render(self.frame, 0.0, 0.0, 256.0, 256)
        self.wait(im)
        _, used = im.render(self.frame, 0.0, 0.0, 256.0, 256)
        self.assertIn("naip", used)
        self.assertIn("usgs", used)             # the black half: USGS
        n = len(self.requests)
        im.render(self.frame, 0.0, 0.0, 256.0, 256)
        self.wait(im)
        self.assertEqual(len(self.requests), n)     # the missing children are remembered

    def test_two_naip_services(self):
        """naipusda first; where it is black (no flights there), naip; the look-ahead asks naip only there,
        and lazy_ready counts the first source with data."""
        def serve_export(z, x, y, size, name):
            a = np.zeros((size, size, 3), np.uint8) + np.array((90, 120, 60) if name == "naip" else (60, 90, 120), np.uint8)
            a[::2, ::2] += 3
            if name == "naipusda" and x % 2:        # every other parent: USDA has nothing (black)
                a[:] = 0
            b = io.BytesIO()
            Image.fromarray(a).save(b, "JPEG", quality=95)
            return 200, b.getvalue()

        def serve_xyz(name, z, x, y):
            a = np.zeros((256, 256, 3), np.uint8) + np.array((200, 30, 30), np.uint8)
            a[::2, ::2] += 6
            return 200, png(a)
        self.requests = []
        net = netfetch.Fetcher(transport=ExportTransport(serve_export, serve_xyz, self.requests, by_name=True),
                               sleep=lambda s: None, policies=FAST)
        im = imagery.Imagery(self.tmp.name, ("naipusda", "naip", "usgs"), fetcher=net, tone=0.0)
        self.assertEqual(self.warm(im, [(0.0, 0.0), (1500.0, 0.0)], radius=700.0), [])
        usda = {r[2:] for r in self.requests if r[0] == "naipusda"}
        usgs_naip = {r[2:] for r in self.requests if r[0] == "naip"}
        self.assertTrue(usda and usgs_naip)
        self.assertTrue(all(x % 2 for x, y in usgs_naip), "USGS NAIP only where USDA was black")
        self.assertLessEqual(usgs_naip, usda)
        self.assertTrue(im.lazy_ready(self.frame, -512.0, -512.0, 1024.0, 1024))
        _, used = im._render(imagery.EnuSquare(self.frame, -512.0, -512.0, 1024.0), 1024)
        self.assertIn("naipusda", used)
        # where a USDA tile meets a USGS-NAIP one, neither has all four bilinear taps: a texel line of USGS
        self.assertLess(used.get("usgs", 0), 0.001 * 1024 * 1024)

    def test_state_source_bounds_and_white(self):
        """md6in: only asked inside its bounds; white (outside the state, inside the box) is missing, the
        next source fills it; big white roofs below the share are kept."""
        def serve_export(z, x, y, size, name):
            a = np.zeros((size, size, 3), np.uint8) + np.array((70, 100, 80), np.uint8)
            a[::2, ::2] += 3
            if name == "md6in":
                if x % 2:
                    a[:] = 253                  # outside the state: white (253 in MD iMAP's JPEGs)
                else:
                    a[:size // 8, :size // 8] = 255     # a white roof: 1.6 % of the image
            b = io.BytesIO()
            Image.fromarray(a).save(b, "JPEG", quality=95)
            return 200, b.getvalue()

        def serve_xyz(name, z, x, y):
            a = np.zeros((256, 256, 3), np.uint8) + np.array((200, 30, 30), np.uint8)
            a[::2, ::2] += 6
            return 200, png(a)
        self.requests = []
        net = netfetch.Fetcher(transport=ExportTransport(serve_export, serve_xyz, self.requests, by_name=True),
                               sleep=lambda s: None, policies=FAST)
        im = imagery.Imagery(self.tmp.name, ("md6in", "naipusda", "usgs"), fetcher=net, tone=0.0)
        self.assertEqual(self.warm(im, [(0.0, 0.0), (1500.0, 0.0)], radius=700.0), [])
        md = {r[2:] for r in self.requests if r[0] == "md6in"}
        usda = {r[2:] for r in self.requests if r[0] == "naipusda"}
        self.assertTrue(md and usda)
        self.assertTrue(all(x % 2 for x, y in usda), "USDA only where Maryland's image was white")
        _, used = im._render(imagery.EnuSquare(self.frame, -512.0, -512.0, 1024.0), 1024)
        self.assertIn("md6in", used)
        self.assertIn("naipusda", used)
        # far from Maryland: never asked
        n = len(self.requests)
        far = g.EnuFrame(LAT0, LON0)        # California
        im.warm(far, [(0.0, 0.0)], 300.0, 1.0)
        for _ in range(100):
            with im._warm_cv:
                if all(im._warm_done(k) for k in im._warm_want):
                    break
            time.sleep(0.02)
        self.assertFalse([r for r in self.requests[n:] if r[0] == "md6in"])

    def test_regional_source_never_probed_outside_its_area(self):
        """A regional source's coverage probe is not fetched for a tile outside its bounds (2026-10-15: Vermont,
        Ohio, NJ ... servers got hundreds of probe requests from a session in Maryland and Switzerland)."""
        im = imagery.Imagery(self.tmp.name, ("vermont", "usgs"), tone=0.0)      # (nothing may be fetched)
        src = imagery.SOURCES["vermont"]
        z = 17
        x, y = imagery.mercator_px(np.array([39.29]), np.array([-76.61]), z)       # Baltimore
        key = (src, z, int(x[0]) // imagery.TILE, int(y[0]) // imagery.TILE)
        with im._lock:
            self.assertEqual(im._probe_status(key), "outside")
        self.assertFalse(im._inflight)
        self.assertFalse(imagery.range_in_bounds(src, z, key[2], key[2] + 3, key[3], key[3] + 3))
        x, y = imagery.mercator_px(np.array([44.26]), np.array([-72.58]), z)       # Montpelier
        self.assertTrue(imagery.range_in_bounds(src, z, int(x[0]) // imagery.TILE, int(x[0]) // imagery.TILE,
                                                int(y[0]) // imagery.TILE, int(y[0]) // imagery.TILE))

    def test_colours_from_the_chain(self):
        """A rendered NAIP texture keeps its detail but takes the next source's large-scale colour (no seam
        against far tiles in USGS colours)."""
        im = self.make()
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        rgb, used = im.render(self.frame, 0.0, 0.0, 256.0, 256)
        raw, _ = im._render(imagery.EnuSquare(self.frame, 0.0, 0.0, 256.0), 256)
        ref, _ = im._render(imagery.EnuSquare(self.frame, 0.0, 0.0, 256.0), 256, None, skip_lazy=True)
        self.assertEqual(set(used), {"naip"})
        far, near = np.abs(raw.mean((0, 1)) - ref.mean((0, 1))), np.abs(rgb.mean((0, 1)) - ref.mean((0, 1)))
        self.assertGreater(far.max(), 50)
        self.assertTrue((near < 0.35 * far + 2).all(), (near, far))                # USGS's colour (the gain clip
        # stops the extreme fake blue short) ...
        hi = lambda a: np.abs(np.diff(a.astype(float), axis=1)).mean()
        self.assertGreater(hi(rgb), 0.5 * hi(raw))                                  # ... NAIP's detail
        same = imagery.match_colours(raw, raw, 10)
        self.assertTrue((same == raw).all(), "matching an image to itself changes nothing")

    def test_lazy_ready(self):
        im = self.make()
        self.assertFalse(im.lazy_ready(self.frame, 0.0, 0.0, 256.0, 256))
        self.assertTrue(im.lazy_ready(self.frame, 0.0, 0.0, 256.0, 64), "4 m texels: no NAIP zoom")
        self.assertEqual(self.warm(im, [(128.0, 128.0)]), [])
        self.assertTrue(im.lazy_ready(self.frame, 0.0, 0.0, 256.0, 256))
        self.assertFalse(im.lazy_ready(self.frame, 5000.0, 0.0, 256.0, 256), "elsewhere: not fetched")

    def test_warm_fetches_ahead(self):
        im = self.make()
        self.assertEqual(self.warm(im, [(0.0, 0.0), (3000.0, 0.0)]), [])
        parents = {r for r in self.requests if r[0] == "naip"}
        self.assertGreaterEqual(len(parents), 2)            # both ends of the path
        _, used = im.render(self.frame, 3000.0, 0.0, 256.0, 256)
        self.assertEqual(set(used), {"naip"})               # on disk before any render


class GutterTest(unittest.TestCase):
    """Tile textures carry a border of real imagery (the engine's sampler wraps)."""

    def test_footprint_maps_to_inner_texels(self):
        for px in (64, 256, 512):
            g = imagery.gutter(px)
            self.assertTrue(1 <= g <= 8)
            x0, y0, size = imagery.gutter_square(1000.0, -64.0, 64.0, px)
            t = size / px
            # the footprint [1000, 1064] covers texels g .. px - g exactly
            self.assertAlmostEqual(1000.0 - x0, g * t, places=9)
            self.assertAlmostEqual(x0 + size - 1064.0, g * t, places=9)
            self.assertAlmostEqual(-64.0 - y0, g * t, places=9)
            # UV0 of a footprint point = its position in the rendered square
            for f in (0.0, 0.3, 1.0):
                u = float(imagery.gutter_uv(f, px))
                self.assertAlmostEqual(u * size + x0, 1000.0 + f * 64.0, places=9)
            self.assertGreater(float(imagery.gutter_uv(0.0, px)), 0.5 / px)        # off the wrap seam
            self.assertLess(float(imagery.gutter_uv(1.0, px)), 1 - 0.5 / px)

    def test_shape_uv_inset(self):
        import farfield as ff
        import cdae
        frame = g.EnuFrame(36.0, -118.0)

        class Flat:
            def sample(self, lat, lon, zoom, progress=None):
                return np.zeros(np.shape(lat))
        tile = ff.Tile(-2, 0, 0)
        b = ff.build_tile(frame, Flat(), tile, 15, 1 / 64, with_sink=False)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.cdae")
            ff.write_built(path, b, "cdae", material="m", textured=True, tex_px=256)
            m = cdae.mesh_arrays(cdae.read(path)["meshes"][0])
        local = m["verts"].astype(np.float64)[:, :2] / tile.size
        np.testing.assert_allclose(m["tverts"][:, 0], imagery.gutter_uv(local[:, 0], 256), atol=1e-6)
        np.testing.assert_allclose(m["tverts"][:, 1], 1 - imagery.gutter_uv(local[:, 1], 256), atol=1e-6)


class ToneTest(unittest.TestCase):
    def test_lut(self):
        self.assertTrue((imagery.tone_lut(0.0) == np.arange(256)).all())
        lut = imagery.tone_lut(1.0).astype(int)
        self.assertTrue((np.diff(lut) >= 0).all())
        self.assertEqual((lut[0], lut[255]), (15, 230))
        self.assertLessEqual(abs(lut[128] - 128), 2)
        self.assertTrue((np.diff(imagery.tone_lut(3.0).astype(int)) >= 0).all())


def bc1_decode(data, w, h):
    """Minimal BC1 (DXT1) decoder for the tests: RGB [h, w, 3] float."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    blocks = np.frombuffer(data, np.uint8).reshape(bh * bw, 8)
    c0 = blocks[:, 0].astype(np.uint32) | (blocks[:, 1].astype(np.uint32) << 8)
    c1 = blocks[:, 2].astype(np.uint32) | (blocks[:, 3].astype(np.uint32) << 8)
    idx = (blocks[:, 4].astype(np.uint32) | (blocks[:, 5].astype(np.uint32) << 8)
           | (blocks[:, 6].astype(np.uint32) << 16) | (blocks[:, 7].astype(np.uint32) << 24))

    def rgb565(c):
        return np.stack([(c >> 11 & 31) * 255 / 31, (c >> 5 & 63) * 255 / 63, (c & 31) * 255 / 31], -1)
    p0, p1 = rgb565(c0), rgb565(c1)
    four = (c0 > c1)[:, None]
    pal = np.stack([p0, p1, np.where(four, (2 * p0 + p1) / 3, (p0 + p1) / 2),
                    np.where(four, (p0 + 2 * p1) / 3, 0.0)], 1)              # (n, 4, 3)
    sel = (idx[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3          # (n, 16)
    px = np.take_along_axis(pal, sel[:, :, None].astype(np.int64), 1)        # (n, 16, 3)
    img = px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)
    return img[:h, :w]


class DdsTest(unittest.TestCase):
    def test_mips_linear_light(self):
        # a black / white checker averages to the linear mid grey (186 in sRGB), not 128
        rgb = np.zeros((8, 8, 3), np.uint8)
        rgb[0::2, 0::2] = rgb[1::2, 1::2] = 255
        lv = imagery.mip_arrays(rgb)
        self.assertEqual([a.shape[0] for a in lv], [8, 4, 2, 1])
        self.assertTrue(180 <= int(lv[1][1, 1, 0]) <= 192, int(lv[1][1, 1, 0]))
        flat = np.full((16, 16, 3), 90, np.uint8)
        self.assertTrue(all(int(np.abs(a.astype(int) - 90).max()) <= 1 for a in imagery.mip_arrays(flat)),
                        "a flat image stays flat (the sharpening adds no ringing)")

    def test_dxt1_with_full_mip_chain(self):
        n = 128
        # smooth, imagery-like content (BC1 is lossy; this tests the chain, not the encoder)
        low = (np.random.default_rng(5).random((8, 8, 3)) * 100 + 60).astype(np.uint8)
        rgb = np.asarray(Image.fromarray(low, "RGB").resize((n, n), Image.BICUBIC))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.dds")
            size = imagery.write_dds(path, rgb)
            data = Path(path).read_bytes()
            self.assertEqual(size, len(data))
            w, h, mips, fourcc, sizes = imagery.read_dds_info(data)
            self.assertEqual((w, h, mips, fourcc), (n, n, 8, b"DXT1"))     # 128 .. 1
            self.assertEqual(len(data), imagery.DDS_HEADER + sum(sizes))
            self.assertEqual(sizes[0], n * n // 2)
            caps = int.from_bytes(data[108:112], "little")
            self.assertTrue(caps & imagery.DDSCAPS_MIPMAP and caps & imagery.DDSCAPS_COMPLEX)
            with Image.open(path) as im:                                    # a standard reader accepts it
                self.assertEqual(im.size, (n, n))
            # level k is the BC1 encoding of mip_arrays' level k (linear light, sharpened) ...
            off = imagery.DDS_HEADER
            for k, (sz, lv) in enumerate(zip(sizes, imagery.mip_arrays(rgb))):
                self.assertEqual(data[off:off + sz], imagery.fast._ef.bc1_encode(np.ascontiguousarray(lv)), k)
                off += sz
            # ... and decodes to the image (row 0 first, as the source image)
            got = bc1_decode(data[imagery.DDS_HEADER:imagery.DDS_HEADER + sizes[0]], n, n)
            self.assertLess(np.abs(got - rgb).mean(), 4.0)
            last = bc1_decode(data[-8:], 1, 1)[0, 0]
            lin_mean = ((rgb.reshape(-1, 3) / 255.0) ** 2.2).mean(0) ** (1 / 2.2) * 255.0
            np.testing.assert_allclose(last, lin_mean, atol=6.0)        # the linear-light mean, one RGB565 step

    def test_sizes(self):
        for px, want in ((512, 128 + sum(max(1, (512 >> k) // 4) ** 2 * 8 for k in range(10))),
                         (256, 128 + sum(max(1, (256 >> k) // 4) ** 2 * 8 for k in range(9)))):
            self.assertEqual(len(imagery.encode_dds(np.zeros((px, px, 3), np.uint8))), want)


class NetworkImageryTest(unittest.TestCase):
    """Real sources (a handful of tiles, cached across runs)."""

    def render(self, lat, lon, size=256.0, px=64):
        im = imagery.Imagery(CACHE)
        try:
            return im.render(g.EnuFrame(lat, lon), -size / 2, -size / 2, size, px)
        except imagery.ImageryError as e:
            self.skipTest(f"imagery sources unreachable: {e}")
        finally:
            im.close()

    def test_us_uses_usgs(self):
        rgb, used = self.render(LAT0, LON0)
        self.assertEqual(set(used), {"usgs"})
        self.assertGreater(rgb.std(), 5)

    def test_outside_us_falls_back_to_eox(self):
        rgb, used = self.render(48.0, 70.0)             # Kazakhstan: no regional source either
        self.assertEqual(set(used), {"eox2023"})
        self.assertGreater(rgb.std(), 5)

    def test_open_ocean(self):
        rgb, used = self.render(30.0, -140.0, size=4096.0)
        self.assertEqual(set(used), {"eox2016"})       # uniform deep blue from both EOX layers
        self.assertLess(rgb.reshape(-1, 3).std(0).max(), 2)
        self.assertGreater(int(rgb[..., 2].mean()), int(rgb[..., 0].mean()))


class LatLonBboxTest(unittest.TestCase):
    def test_wms_4326_box(self):
        """A WMS without Web Mercator gets the tile's extent as south,west,north,east in degrees (WMS 1.3.0)."""
        s, w, n, e = map(float, imagery.lat_lon_bbox(imagery.mercator_bbox(15, 18296, 10771)).split(","))
        self.assertLess(s, n)
        self.assertLess(w, e)
        self.assertAlmostEqual(w, 18296 / 2 ** 15 * 360 - 180, places=6)
        self.assertTrue(52.0 < s < 52.5 and 20.9 < w < 21.1, "Warsaw")
        self.assertIn("{bbox4326}", imagery.SOURCES["poland"].url)


if __name__ == "__main__":
    unittest.main()
