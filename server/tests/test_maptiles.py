"""The world map's tile endpoint (server/maptiles.py) with a fake imagery object: source order,
coverage fall-through, overzoom, errors, and the HTTP server on 127.0.0.1. Also runs the map's
tile math test (test_mapmath.mjs) when node is installed."""
import asyncio
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import imagery  # noqa: E402
import maptiles  # noqa: E402

S = imagery.SOURCES


class FakeImagery:
    """Imagery's .chain / .tiles / .attribution: each source's tiles are one flat colour (red channel
    = the source) with a gradient so they are not uniform; `missing` / `blank` / `fail` per source."""

    def __init__(self, chain=imagery.DEFAULT_CHAIN, missing=(), blank=(), fail=()):
        self.chain = tuple(S[n] for n in chain)
        self.missing, self.blank, self.fail = set(missing), set(blank), set(fail)
        self.calls = []

    def tiles(self, keys, kind="demand"):
        out = {}
        for k in keys:
            src, z, x, y = k
            self.calls.append((src.name, z, x, y, kind))
            if src.name in self.fail:
                raise imagery.ImageryError("down")
            if src.name in self.missing or src.name not in ("usgs", "eox2023", "eox2016"):
                out[k] = None               # (the regional sources: no tile here)
                continue
            a = np.zeros((256, 256, 3), np.uint8)
            a[..., 0] = {"usgs": 60, "eox2023": 140, "eox2016": 200}[src.name]
            if src.name not in self.blank:
                a[..., 1] = np.arange(256, dtype=np.uint8)[:, None]      # rows 0..255: a gradient north to south
            out[k] = imagery.as_tile(a)
        return out

    def raw(self, src, z, x, y, kind="demand"):
        """The map layers' source tiles (Imagery.raw): terrain-light a JPEG with a north-south gradient, the overlay
        a PNG with alpha (transparent but for an opaque top-left quarter)."""
        self.calls.append((src.name, z, x, y, kind))
        if src.name in self.fail:
            raise imagery.ImageryError("down")
        if src.name in self.missing:
            return None
        buf = io.BytesIO()
        if src.name == "eoxlabels":
            a = np.zeros((256, 256, 4), np.uint8)
            a[:128, :128] = (250, 250, 250, 255)
            Image.fromarray(a, "RGBA").save(buf, "PNG")
        else:
            a = np.zeros((256, 256, 3), np.uint8)
            a[..., 1] = np.arange(256, dtype=np.uint8)[:, None]
            Image.fromarray(a).save(buf, "JPEG")
        return buf.getvalue()

    def attribution(self):
        return "Imagery: fake"


def decode(jpeg):
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB")).astype(int)


class MapTilesTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(maptiles.parse_path("/tiles/3/2/1.jpg"), (3, 2, 1))
        self.assertEqual(maptiles.parse_path("/tiles/3/2/1?t=5"), (3, 2, 1))
        for bad in ("/tiles/3/8/1.jpg", "/tiles/-1/0/0.jpg", "/tiles/19/0/0.jpg", "/tiles/a/b/c.jpg",
                    "/x/1/0/0.jpg", "/tiles/1/0"):
            with self.assertRaises(maptiles.BadTile, msg=bad):
                maptiles.parse_path(bad)

    def test_eox_first_at_low_zoom_usgs_above(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        _, src, _, _ = t.tile(11, 584, 783)
        self.assertEqual(src, "eox2023")
        _, src, _, _ = t.tile(14, 4683, 6266)
        self.assertEqual(src, "usgs")
        self.assertTrue(all(c[4] == maptiles.FETCH_KIND for c in img.calls))

    def test_usgs_not_below_its_min_zoom(self):
        img = FakeImagery(chain=("usgs",))
        self.assertIsNone(maptiles.MapTiles(img).tile(5, 9, 12))
        self.assertEqual(img.calls, [])

    def test_fall_through_missing_and_blank(self):
        self.assertEqual(maptiles.MapTiles(FakeImagery(missing=("usgs",))).tile(15, 9412, 12490)[1], "eox2023")
        self.assertEqual(maptiles.MapTiles(FakeImagery(blank=("usgs", "eox2023"))).tile(15, 9412, 12490)[1],
                         "eox2016")
        self.assertIsNone(maptiles.MapTiles(FakeImagery(missing=("usgs", "eox2023", "eox2016"))).tile(15, 1, 1))

    def test_failure_raises_only_when_nothing_else_has_it(self):
        _, src, final, _ = maptiles.MapTiles(FakeImagery(fail=("usgs",))).tile(15, 9412, 12490)
        self.assertEqual(src, "eox2023")
        self.assertFalse(final, "a later source standing in for one that failed: a stand-in")
        self.assertTrue(maptiles.MapTiles(FakeImagery(missing=("usgs",))).tile(15, 9412, 12490)[2],
                        "one that has nothing there: the tile")
        with self.assertRaises(imagery.ImageryError):
            maptiles.MapTiles(FakeImagery(fail=("usgs", "eox2023", "eox2016"))).tile(15, 9412, 12490)

    def test_overzoom_crops_the_ancestor(self):
        img = FakeImagery(chain=("eox2023",))
        t = maptiles.MapTiles(img)
        # z16 from the z14 ancestor: 4 x 4 children; the bottom row of children holds the
        # ancestor's rows 192..255 of the gradient
        x, y = (100 << 2) + 1, (200 << 2) + 3
        jpeg, src, final, _ = t.tile(16, x, y)
        self.assertTrue(final)
        self.assertEqual(img.calls[-1][:4], ("eox2023", 14, 100, 200))
        a = decode(jpeg)
        self.assertEqual(a.shape, (256, 256, 3))
        self.assertTrue(185 <= a[:, :, 1].mean() <= 230, a[:, :, 1].mean())
        top = decode(t.tile(16, 100 << 2, 200 << 2)[0])
        self.assertLess(top[:, :, 1].mean(), 40)
        self.assertEqual(maptiles.crop_up(np.zeros((256, 256, 3), np.uint8), 0, 0, 0).shape, (256, 256, 3))

    def test_respond(self):
        t = maptiles.MapTiles(FakeImagery())
        st, ctype, body = t.respond("GET", "/tiles/12/1170/1566.jpg")
        self.assertEqual((st, ctype, body[:2]), (200, "image/jpeg", b"\xff\xd8"))
        self.assertEqual(t.respond("GET", "/tiles/40/0/0.jpg")[0], 404)
        self.assertEqual(t.respond("POST", "/tiles/1/0/0.jpg")[0], 405)
        self.assertIn(b"fake", t.respond("GET", "/attribution")[2])
        self.assertEqual(t.respond("GET", "/health")[2], b"ok")
        dead = maptiles.MapTiles(FakeImagery(fail=("usgs", "eox2023", "eox2016")))
        self.assertEqual(dead.respond("GET", "/tiles/12/1170/1566.jpg")[0], 503)
        self.assertEqual(maptiles.MapTiles(FakeImagery(missing=("usgs", "eox2023", "eox2016"))).respond(
            "GET", "/tiles/12/1170/1566.jpg")[0], 404)

    def test_http_on_localhost(self):
        async def run():
            t = maptiles.MapTiles(FakeImagery())
            srv, port = await t.start(0)
            self.assertEqual(srv.sockets[0].getsockname()[0], "127.0.0.1")

            async def get(path):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                await w.drain()
                data = await r.read()
                w.close()
                return data
            try:
                ok = await get("/tiles/3/2/3.jpg")
                bad = await get("/tiles/3/9/3.jpg")
                junk = await get("")
            finally:
                srv.close()
                await srv.wait_closed()
            return ok, bad, junk
        ok, bad, junk = asyncio.run(run())
        head, body = ok.split(b"\r\n\r\n", 1)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Access-Control-Allow-Origin: *", head)
        self.assertIn(f"Content-Length: {len(body)}".encode(), head)
        self.assertEqual(body[:2], b"\xff\xd8")
        self.assertTrue(bad.startswith(b"HTTP/1.1 404"))
        self.assertTrue(junk.startswith(b"HTTP/1.1 400"))


class LayersTest(unittest.TestCase):
    def test_paths(self):
        root = os.path.join("r", "maptiles")
        self.assertEqual(maptiles.tile_path(root, "sat", 3, 2, 1), os.path.join(root, "3", "2", "1.jpg"))
        self.assertEqual(maptiles.tile_path(root, "map", 3, 2, 1), os.path.join(root, "map", "3", "2", "1.jpg"))
        self.assertEqual(maptiles.tile_path(root, "labels", 3, 2, 1), os.path.join(root, "labels", "3", "2", "1.png"))
        with self.assertRaises(KeyError):
            maptiles.tile_path(root, "osm", 3, 2, 1)

    def test_eox_layers_not_imagery_choices(self):
        for src in maptiles.LAYER_SOURCES.values():
            self.assertNotIn(src.name, imagery.SOURCES)
            self.assertTrue(src.url.startswith("https://tiles.maps.eox.at/"))
            self.assertIn("OpenStreetMap", src.attribution)

    def test_map_layer_native_and_overzoomed(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        jpeg, final, _ = t.layer_tile("map", 12, 1176, 1561)
        self.assertTrue(final)
        self.assertEqual(img.calls[-1], ("eoxmap", 12, 1176, 1561, maptiles.FETCH_KIND))
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        # z16 from the z14 tile: the bottom row of its 4 x 4 children has rows 192..255 of the gradient
        a = decode(t.layer_tile("map", 16, (100 << 2) + 1, (200 << 2) + 3)[0])
        self.assertEqual(img.calls[-1][:4], ("eoxmap", 14, 100, 200))
        self.assertEqual(a.shape, (256, 256, 3))
        self.assertTrue(185 <= a[:, :, 1].mean() <= 230, a[:, :, 1].mean())

    def test_labels_keep_transparency(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        png = t.layer_tile("labels", 10, 5, 7)[0]
        self.assertEqual(png[:4], b"\x89PNG")
        a = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
        self.assertEqual(a[10, 10, 3], 255)
        self.assertEqual(a[200, 200, 3], 0)
        # overzoomed: the top-left child of the opaque quarter is all opaque, the bottom-right one all clear
        tl = np.asarray(Image.open(io.BytesIO(t.layer_tile("labels", 15, 100 << 1, 200 << 1)[0])).convert("RGBA"))
        self.assertEqual(img.calls[-1][:4], ("eoxlabels", 14, 100, 200))
        self.assertTrue((tl[8:-8, 8:-8, 3] == 255).all())
        br = np.asarray(Image.open(io.BytesIO(t.layer_tile("labels", 15, (100 << 1) + 1, (200 << 1) + 1)[0])).convert("RGBA"))
        self.assertTrue((br[..., 3] == 0).all())

    def test_missing_and_failing_layers(self):
        self.assertIsNone(maptiles.MapTiles(FakeImagery(missing=("eoxmap",))).layer_tile("map", 5, 1, 1))
        with self.assertRaises(imagery.ImageryError):
            maptiles.MapTiles(FakeImagery(fail=("eoxlabels",))).layer_tile("labels", 5, 1, 1)
        self.assertEqual(maptiles.MapTiles(FakeImagery()).layer_tile("sat", 12, 1170, 1566)[0][:2], b"\xff\xd8")


class LazyImagery(FakeImagery):
    """FakeImagery with a lazy regional source (md6in): its tile is undecided until fetch_lazy_tile, which `fetch`
    says how to answer: "data", "none", "fail" or "nothing" (still undecided after)."""

    def __init__(self, fetch="data", state="todo", until=None):
        super().__init__(chain=("md6in", "usgs", "eox2023"))
        self.fetch, self.state, self.until, self.fetched = fetch, state, until, []

    def lazy_tile_state(self, key):
        return self.state, (self.until if self.state == "none" else None)

    def fetch_lazy_tile(self, key):
        self.fetched.append(key[1:])
        if self.fetch == "fail":
            raise imagery.ImageryError("md down")
        if self.fetch in ("data", "none"):
            self.state = self.fetch
        return True

    def tiles(self, keys, kind="demand"):
        out = super().tiles(keys, kind)
        for k in keys:
            if k[0].name == "md6in" and self.state == "data":
                a = np.zeros((256, 256, 3), np.uint8)
                a[..., 0] = 20
                a[..., 1] = np.arange(256, dtype=np.uint8)[:, None]
                out[k] = imagery.as_tile(a)
        return out


class StandInTest(unittest.TestCase):
    """A tile from a later source standing in for a better one that could not be read now is not final (Fells Point,
    2026-11-13: an 8x enlarged Sentinel-2 tile kept for good beside sharp MD iMAP tiles on the car screen)."""
    Z, X, Y = 17, 37649, 49959          # Fells Point, Baltimore: inside md6in

    def test_lazy_tile_fetched_on_demand(self):
        img = LazyImagery("data")
        _, src, final, _ = maptiles.MapTiles(img).tile(self.Z, self.X, self.Y)
        self.assertEqual((src, final), ("md6in", True))
        self.assertEqual(img.fetched, [(self.Z, self.X, self.Y)])

    def test_known_missing_lazy_tile_is_final(self):
        img = LazyImagery(state="none")
        _, src, final, until = maptiles.MapTiles(img).tile(self.Z, self.X, self.Y)
        self.assertEqual((src, final, until), ("usgs", True, None), "nothing there for good: the next source is the tile")
        self.assertEqual(img.fetched, [], "decided already: not fetched")

    def test_failed_or_undecided_lazy_tile_is_a_stand_in(self):
        for fetch in ("fail", "nothing"):
            _, src, final, until = maptiles.MapTiles(LazyImagery(fetch)).tile(self.Z, self.X, self.Y)
            self.assertEqual((src, final, until), ("usgs", False, None), fetch)
        _, src, final, _ = maptiles.MapTiles(LazyImagery("none")).tile(self.Z, self.X, self.Y)
        self.assertEqual((src, final), ("usgs", True), "nothing there for good after the fetch: the next source is the tile")

    def test_an_expiring_nothing_there_keeps_the_next_source_a_stand_in(self):
        # (review of a9a978a9: a miss with a TTL, an expired one reused while its host is down: the better source may
        # come back, so the later source's tile is kept only until then)
        t = time.time() + 3600
        _, src, final, until = maptiles.MapTiles(LazyImagery(state="none", until=t)).tile(self.Z, self.X, self.Y)
        self.assertEqual((src, final, until), ("usgs", False, t))
        _, src, final, until = maptiles.MapTiles(LazyImagery("none", until=t)).tile(self.Z, self.X, self.Y)
        self.assertEqual((src, final, until), ("usgs", False, t), "decided by the fetch, with its expiry")


class FakeLayers:
    """MapTiles.layer_tile with a delay per tile, a gate the first build waits on, and tiles with nothing."""

    def __init__(self, delay=0.0, gate=None, missing=(), fail=(), stand_in=()):
        self.delay, self.gate = delay, gate
        self.missing, self.fail, self.stand_in = set(missing), set(fail), set(stand_in)
        self.stand_in_until = None
        self.calls = []
        self.lock = threading.Lock()

    def layer_tile(self, layer, z, x, y):
        with self.lock:
            self.calls.append((layer, z, x, y))
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.delay)
        if (layer, z, x, y) in self.fail:
            raise imagery.ImageryError("down")
        if (layer, z, x, y) in self.missing:
            return None
        if (layer, z, x, y) in self.stand_in:
            return b"blur", False, self.stand_in_until
        return b"tile", True, None


class TileFilesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.done = []
        self.cv = threading.Condition()

    def ready(self, key, ok):
        with self.cv:
            self.done.append((key, ok, time.monotonic()))
            self.cv.notify_all()

    def wait_for(self, n, timeout=5):
        with self.cv:
            self.assertTrue(self.cv.wait_for(lambda: len(self.done) >= n, timeout), self.done)

    def test_concurrent_and_each_said_at_once(self):
        # 12 tiles taking 0.2 s each: one by one (the old map_tiles) the first was said after all 12, 2.4 s
        fake = FakeLayers(delay=0.2)
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=6)
        self.addCleanup(tf.close)
        keys = [("sat", 12, x, 1561) for x in range(1170, 1182)]
        t0 = time.monotonic()
        tf.request(keys)
        self.wait_for(12)
        first, last = self.done[0][2] - t0, self.done[-1][2] - t0
        self.assertLess(first, 0.35, f"first tile after {first:.2f} s")
        self.assertLess(last, 1.0, f"all 12 after {last:.2f} s")
        self.assertTrue(all(ok for _, ok, _ in self.done))
        for k in keys:
            with open(maptiles.tile_path(self.root, *k), "rb") as f:
                self.assertEqual(f.read(), b"tile")
        self.assertEqual(tf.pending(), (0, 0))
        print(f"\nTileFiles: 12 tiles of 0.2 s, first said after {first:.2f} s, all after {last:.2f} s "
              f"(one by one: 2.4 s, said at the end)")

    def test_newest_request_first_older_dropped(self):
        gate = threading.Event()
        fake = FakeLayers(gate=gate)
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=1)
        self.addCleanup(tf.close)
        old = [("sat", 10, x, 5) for x in range(5)]
        tf.request(old)
        for _ in range(100):                    # the first one is being built (waiting on the gate)
            if fake.calls:
                break
            time.sleep(0.01)
        new = [("map", 11, 3, 3), ("labels", 11, 3, 3), ("sat", 10, 3, 5)]
        tf.request(new)
        gate.set()
        self.wait_for(4)
        self.assertEqual([k for k, _, _ in self.done], [old[0]] + new)
        self.assertEqual(fake.calls, [old[0]] + new)     # old 1, 2, 4 never started
        self.assertEqual(tf.stats["dropped"], 3)
        self.assertTrue(os.path.exists(maptiles.tile_path(self.root, "labels", 11, 3, 3)))
        # an empty request cancels what is queued
        fake.gate = threading.Event()
        tf.request([("sat", 9, 1, 1), ("sat", 9, 2, 1)])
        for _ in range(100):                    # 9/1/1 is being built
            if len(fake.calls) == 5:
                break
            time.sleep(0.01)
        tf.request([])
        fake.gate.set()
        self.wait_for(5)
        time.sleep(0.05)
        self.assertEqual(len(self.done), 5)

    def test_files_there_are_not_built_again(self):
        fake = FakeLayers()
        k = ("map", 8, 1, 2)
        p = maptiles.tile_path(self.root, *k)
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"old")
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=2)
        self.addCleanup(tf.close)
        tf.request([k])
        self.wait_for(1)
        self.assertEqual(self.done[0][:2], (k, True))
        self.assertEqual(fake.calls, [])

    def test_failed_and_missing_wait_before_asked_again(self):
        now = [100.0]
        bad, none = ("sat", 12, 1, 1), ("map", 12, 2, 2)
        fake = FakeLayers(missing=(none,), fail=(bad,))
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=2, retry_s=4.0, clock=lambda: now[0])
        self.addCleanup(tf.close)
        tf.request([bad, none])
        self.wait_for(2)
        self.assertEqual({(k, ok) for k, ok, _ in self.done}, {(bad, False), (none, False)})
        tf.request([bad, none])                 # within retry_s: not asked
        time.sleep(0.1)
        self.assertEqual(len(fake.calls), 2)
        now[0] += 5
        tf.request([bad, none])
        self.wait_for(4)
        self.assertEqual(len(fake.calls), 4)
        self.assertFalse(os.path.exists(maptiles.tile_path(self.root, *bad)))

    def test_stand_in_kept_apart_and_built_again(self):
        now = [100.0]
        k = ("sat", 17, 5, 6)
        fake = FakeLayers(stand_in=(k,))
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=1, clock=lambda: now[0])
        self.addCleanup(tf.close)
        tf.request([k])
        self.wait_for(1)
        real, prov = maptiles.tile_path(self.root, *k), maptiles.prov_path(self.root, 17, 5, 6)
        self.assertEqual(self.done[0][:2], (k, True))
        self.assertFalse(os.path.exists(real), "a stand-in never takes the tile's own path")
        with open(prov, "rb") as f:
            self.assertEqual(f.read(), b"blur")
        tf.request([k])                         # (the mod asks again while it shows the stand-in)
        time.sleep(0.1)
        self.assertEqual(len(fake.calls), 1, "not built again before PROV_RETRY_S")
        now[0] += maptiles.PROV_RETRY_S[0] + 0.1
        tf.request([k])
        self.wait_for(2)
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse(os.path.exists(real))
        now[0] += maptiles.PROV_RETRY_S[0] + 0.1
        tf.request([k])
        time.sleep(0.1)
        self.assertEqual(len(fake.calls), 2, "the second wait is longer")
        fake.stand_in.clear()                   # the better source answers now
        now[0] += maptiles.PROV_RETRY_S[1]
        tf.request([k])
        self.wait_for(3)
        with open(real, "rb") as f:
            self.assertEqual(f.read(), b"tile")
        self.assertFalse(os.path.exists(prov), "the stand-in's file goes once the tile is there")
        self.assertEqual(tf.stats["stand_in"], 2)

    def test_stand_in_built_again_once_the_better_source_s_absence_ends(self):
        now, wall = [100.0], [1000.0]
        k = ("sat", 17, 7, 8)
        fake = FakeLayers(stand_in=(k,))
        fake.stand_in_until = wall[0] + 3.0         # (the better source's miss ends in 3 s, before PROV_RETRY_S[0])
        tf = maptiles.TileFiles(fake, self.root, self.ready, workers=1, clock=lambda: now[0], wall=lambda: wall[0])
        self.addCleanup(tf.close)
        tf.request([k])
        self.wait_for(1)
        now[0] += 2.0
        tf.request([k])
        time.sleep(0.1)
        self.assertEqual(len(fake.calls), 1, "not before the absence ends")
        now[0] += 1.5
        fake.stand_in.clear()
        tf.request([k])
        self.wait_for(2)
        self.assertTrue(os.path.exists(maptiles.tile_path(self.root, *k)), "the better source's tile, as the tile")

    def test_old_sat_files_swept_once(self):
        def put(p, mtime=None):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x")
            if mtime is not None:
                os.utime(p, (mtime, mtime))
        t0 = time.time()
        old_sat = maptiles.tile_path(self.root, "sat", 17, 1, 2)
        new_sat = maptiles.tile_path(self.root, "sat", 17, 1, 3)
        old_map = maptiles.tile_path(self.root, "map", 12, 1, 2)
        old_prov = maptiles.prov_path(self.root, 17, 1, 4)
        put(old_sat, t0 - 100)
        put(new_sat, t0 + 5)
        put(old_map, t0 - 100)
        put(old_prov, t0 - 100)
        self.assertEqual(maptiles.sweep_sat(self.root, t0, gap_s=0), 1)
        self.assertFalse(os.path.exists(old_sat))
        self.assertTrue(all(os.path.exists(p) for p in (new_sat, old_map, old_prov)))
        put(old_sat, t0 - 100)
        self.assertEqual(maptiles.sweep_sat(self.root, t0, gap_s=0), 0, "once: the format file says it is done")
        self.assertTrue(os.path.exists(old_sat))
        self.assertEqual(maptiles.prune(self.root, keep=0, gap_s=0), 4, "prune never removes the format file")

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_sweep_never_leaves_the_folder(self):
        # (review of a9a978a9: a numeric entry linked elsewhere was walked and every older file there deleted)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        old = time.time() - 100

        def put(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x")
            os.utime(p, (old, old))
            return p
        victims = [put(os.path.join(outside, "3", "4.jpg")), put(os.path.join(outside, "notes.txt")),
                   put(os.path.join(outside, "9", "9", "9.jpg"))]
        os.makedirs(self.root, exist_ok=True)
        os.symlink(outside, os.path.join(self.root, "17"))                     # a zoom folder linked outside
        os.makedirs(os.path.join(self.root, "16"))
        os.symlink(os.path.join(outside, "3"), os.path.join(self.root, "16", "5"))     # a column linked outside
        os.makedirs(os.path.join(self.root, "15", "2"))
        os.symlink(victims[0], os.path.join(self.root, "15", "2", "1.jpg"))    # a tile linked outside
        own = [put(maptiles.tile_path(self.root, "sat", 14, 1, 2)), put(os.path.join(self.root, "14", "1", "x.txt"))]
        self.assertEqual(maptiles.sweep_sat(self.root, time.time(), gap_s=0), 1)
        self.assertFalse(os.path.exists(own[0]), "its own old tile goes")
        self.assertTrue(os.path.exists(own[1]), "not a tile file: kept")
        self.assertTrue(all(os.path.exists(p) for p in victims), "nothing outside the folder is touched")
        # the same without directory descriptors (Windows): paths checked to stay inside
        from unittest import mock
        os.remove(os.path.join(self.root, maptiles.FORMAT_FILE))
        put(own[0])
        with mock.patch.object(maptiles, "_DIR_FD", False):
            self.assertEqual(maptiles.sweep_sat(self.root, time.time(), gap_s=0), 1)
        self.assertFalse(os.path.exists(own[0]))
        self.assertTrue(all(os.path.exists(p) for p in victims), "nothing outside the folder is touched (paths)")
        linked = tempfile.mkdtemp()
        os.rmdir(linked)
        os.symlink(self.root, linked, target_is_directory=True)                 # the folder itself a link
        self.addCleanup(os.unlink if os.name != "nt" else os.rmdir, linked)     # (the link, not its target)
        self.assertEqual(maptiles.sweep_sat(linked, time.time(), gap_s=0), 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, maptiles.FORMAT_FILE)))

    def test_sweep_refuses_a_reparse_point_root(self):
        # (review of 3a43adcc: a Windows junction is not a symlink; its target was trusted as the root.) Here a root whose
        # lstat says reparse point (FILE_ATTRIBUTE_REPARSE_POINT); test_sweep_junctions_on_windows makes real ones.
        import types
        from unittest import mock
        old = time.time() - 100
        p = maptiles.tile_path(self.root, "sat", 14, 1, 2)
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as f:
            f.write(b"x")
        os.utime(p, (old, old))
        real_lstat = os.lstat

        def lstat(path, *a, **k):
            if os.path.abspath(path) == os.path.abspath(self.root):
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
            return real_lstat(path, *a, **k)
        for fd in (True, False):
            with mock.patch.object(maptiles, "_DIR_FD", fd), mock.patch("os.lstat", lstat):
                self.assertEqual(maptiles.sweep_sat(self.root, time.time(), gap_s=0), 0)
            self.assertTrue(os.path.exists(p))
            self.assertFalse(os.path.exists(os.path.join(self.root, maptiles.FORMAT_FILE)), "no format file written")

    def test_a_refused_root_opens_nothing_under_it(self):
        # (re-review of 37893241: the format marker was read before the root was refused; a FIFO there would block)
        import types
        from unittest import mock
        with open(os.path.join(self.root, maptiles.FORMAT_FILE), "w") as f:
            f.write("0")
        real_lstat, real_open, opened = os.lstat, open, []

        def lstat(path, *a, **k):
            if os.path.abspath(path) == os.path.abspath(self.root):
                return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
            return real_lstat(path, *a, **k)

        def spy_open(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        real_os_open = os.open

        def spy_os_open(path, *a, **k):
            opened.append(str(path))
            return real_os_open(path, *a, **k)
        with mock.patch("os.lstat", lstat), mock.patch("builtins.open", spy_open), mock.patch("os.open", spy_os_open):
            self.assertEqual(maptiles.sweep_sat(self.root, time.time(), gap_s=0), 0)
        self.assertEqual([p for p in opened if os.path.abspath(p).startswith(os.path.abspath(self.root))], [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs")
    def test_a_fifo_marker_never_blocks(self):
        os.mkfifo(os.path.join(self.root, maptiles.FORMAT_FILE))
        maptiles.sweep_sat(self.root, time.time(), gap_s=0)    # (would block forever reading the FIFO)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks")
    def test_a_linked_marker_is_not_followed(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "fmt")
        with open(target, "w") as f:
            f.write(str(maptiles.SAT_FORMAT))
        os.symlink(target, os.path.join(self.root, maptiles.FORMAT_FILE))
        self.assertEqual(maptiles._marker_format(os.path.join(self.root, maptiles.FORMAT_FILE)), 0)

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_sweep_junctions_on_windows(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        old = time.time() - 100

        def put(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x")
            os.utime(p, (old, old))
            return p

        def junction(link, target):
            subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True, capture_output=True)
            self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))     # (removes the junction, not its target)
        victims = [put(os.path.join(outside, "17", "1", "2.jpg")), put(os.path.join(outside, "3", "4.jpg"))]
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, True)
        linked_root = os.path.join(parent, "maptiles")
        junction(linked_root, outside)                       # the root itself a junction to a folder outside
        self.assertEqual(maptiles.sweep_sat(linked_root, time.time(), gap_s=0), 0)
        self.assertTrue(all(os.path.exists(v) for v in victims), "nothing under the junction's target touched")
        self.assertFalse(os.path.exists(os.path.join(outside, maptiles.FORMAT_FILE)), "no format file written there")
        os.makedirs(os.path.join(self.root, "16"), exist_ok=True)
        junction(os.path.join(self.root, "17"), outside)     # a zoom folder a junction
        junction(os.path.join(self.root, "16", "3"), os.path.join(outside, "3"))   # a column a junction
        own = put(maptiles.tile_path(self.root, "sat", 15, 1, 2))
        self.assertEqual(maptiles.sweep_sat(self.root, time.time(), gap_s=0), 1)
        self.assertFalse(os.path.exists(own))
        self.assertTrue(all(os.path.exists(v) for v in victims), "nothing through a junction inside touched")

    def test_callback_errors_do_not_stop_workers(self):
        def boom(key, ok):
            raise RuntimeError("Event loop is closed")
        tf = maptiles.TileFiles(FakeLayers(), self.root, boom, workers=1)
        self.addCleanup(tf.close)
        tf.request([("sat", 3, 1, 1), ("sat", 3, 2, 1)])
        tf.on_ready = self.ready
        tf.request([("sat", 3, 3, 1)])
        with self.cv:
            self.assertTrue(self.cv.wait_for(lambda: any(k == ("sat", 3, 3, 1) for k, _, _ in self.done), 5))
        self.assertTrue(os.path.exists(maptiles.tile_path(self.root, "sat", 3, 3, 1)))


class MapTilesMessageTest(unittest.TestCase):
    """earthd's map_tiles message: parsed (layers, bad entries), built as files, each said on the event loop."""

    def test_message(self):
        import types
        import earthd
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        s = types.SimpleNamespace(map_tiles=maptiles.MapTiles(FakeImagery()), map_files=None, root=root)

        async def run():
            got = []
            done = asyncio.Event()

            def send(m):
                got.append(m)
                if len(got) == 3:
                    done.set()
            conn = types.SimpleNamespace(s=s, loop=asyncio.get_running_loop(), send=send)
            await earthd.Connection.map_tiles(conn, {"tiles": [
                [12, 1170, 1566], [12, 1170, 1566, "map"], [12, 1170, 1566, "labels"], [12, 1170, 1566, "osm"],
                [12, 1170, 1566, ["x"]], [40, 0, 0], "junk", [1, 2]]})
            await asyncio.wait_for(done.wait(), 5)
            await earthd.Connection.map_tiles(conn, {"tiles": {}})     # an empty Lua table: cancels, no error
            return got
        got = asyncio.run(run())
        s.map_files.close()
        self.assertTrue(all(m["type"] == "map_tiles_ready" and len(m["tiles"]) == 1 for m in got))
        self.assertEqual(sorted(m["tiles"][0][4] for m in got), ["labels", "map", "sat"])
        self.assertTrue(all(m["tiles"][0][3] is True for m in got))
        for layer in ("sat", "map", "labels"):
            self.assertTrue(os.path.exists(maptiles.tile_path(os.path.join(root, "maptiles"), layer, 12, 1170, 1566)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class MapMathTest(unittest.TestCase):
    def test_mapmath(self):
        r = subprocess.run(["node", str(Path(__file__).with_name("test_mapmath.mjs"))], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)



class PruneTest(unittest.TestCase):
    def test_oldest_go_down_to_keep_and_empty_folders(self):
        import time as _t
        with tempfile.TemporaryDirectory() as root:
            paths = []
            for k in range(10):
                p = os.path.join(root, "labels" if k % 2 else "", str(k), "1", "2.jpg")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "wb").write(b"x")
                os.utime(p, (1000 + k, 1000 + k))
                paths.append(p)
            n = maptiles.prune(root, keep=4, gap_s=0)
            self.assertEqual(n, 6)
            self.assertEqual([os.path.exists(p) for p in paths], [False] * 6 + [True] * 4)
            self.assertFalse(os.path.exists(os.path.join(root, "0")))        # emptied folders gone
            self.assertEqual(maptiles.prune(root, keep=4, gap_s=0), 0)

if __name__ == "__main__":
    unittest.main()
