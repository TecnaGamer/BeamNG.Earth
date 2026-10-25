"""The world map's tile endpoint (server/maptiles.py) with a fake imagery object: source order,
coverage fall-through, overzoom, errors, and the HTTP server on 127.0.0.1. Also runs the map's
tile math test (test_mapmath.mjs) when node is installed."""
import asyncio
import io
import os
import shutil
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
        _, src = t.tile(11, 584, 783)
        self.assertEqual(src, "eox2023")
        _, src = t.tile(14, 4683, 6266)
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
        self.assertEqual(maptiles.MapTiles(FakeImagery(fail=("usgs",))).tile(15, 9412, 12490)[1], "eox2023")
        with self.assertRaises(imagery.ImageryError):
            maptiles.MapTiles(FakeImagery(fail=("usgs", "eox2023", "eox2016"))).tile(15, 9412, 12490)

    def test_overzoom_crops_the_ancestor(self):
        img = FakeImagery(chain=("eox2023",))
        t = maptiles.MapTiles(img)
        # z16 from the z14 ancestor: 4 x 4 children; the bottom row of children holds the
        # ancestor's rows 192..255 of the gradient
        x, y = (100 << 2) + 1, (200 << 2) + 3
        jpeg, src = t.tile(16, x, y)
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
        jpeg = t.layer_tile("map", 12, 1176, 1561)
        self.assertEqual(img.calls[-1], ("eoxmap", 12, 1176, 1561, maptiles.FETCH_KIND))
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        # z16 from the z14 tile: the bottom row of its 4 x 4 children has rows 192..255 of the gradient
        a = decode(t.layer_tile("map", 16, (100 << 2) + 1, (200 << 2) + 3))
        self.assertEqual(img.calls[-1][:4], ("eoxmap", 14, 100, 200))
        self.assertEqual(a.shape, (256, 256, 3))
        self.assertTrue(185 <= a[:, :, 1].mean() <= 230, a[:, :, 1].mean())

    def test_labels_keep_transparency(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        png = t.layer_tile("labels", 10, 5, 7)
        self.assertEqual(png[:4], b"\x89PNG")
        a = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
        self.assertEqual(a[10, 10, 3], 255)
        self.assertEqual(a[200, 200, 3], 0)
        # overzoomed: the top-left child of the opaque quarter is all opaque, the bottom-right one all clear
        tl = np.asarray(Image.open(io.BytesIO(t.layer_tile("labels", 15, 100 << 1, 200 << 1))).convert("RGBA"))
        self.assertEqual(img.calls[-1][:4], ("eoxlabels", 14, 100, 200))
        self.assertTrue((tl[8:-8, 8:-8, 3] == 255).all())
        br = np.asarray(Image.open(io.BytesIO(t.layer_tile("labels", 15, (100 << 1) + 1, (200 << 1) + 1))).convert("RGBA"))
        self.assertTrue((br[..., 3] == 0).all())

    def test_missing_and_failing_layers(self):
        self.assertIsNone(maptiles.MapTiles(FakeImagery(missing=("eoxmap",))).layer_tile("map", 5, 1, 1))
        with self.assertRaises(imagery.ImageryError):
            maptiles.MapTiles(FakeImagery(fail=("eoxlabels",))).layer_tile("labels", 5, 1, 1)
        self.assertEqual(maptiles.MapTiles(FakeImagery()).layer_tile("sat", 12, 1170, 1566)[:2], b"\xff\xd8")

    def test_apple_layer_from_terrain_light(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        jpeg = t.layer_tile("apple", 12, 1176, 1561)
        self.assertEqual(img.calls[-1], ("eoxmap", 12, 1176, 1561, maptiles.FETCH_KIND), "the map layer's source")
        self.assertEqual(jpeg[:2], b"\xff\xd8")
        a = decode(t.layer_tile("apple", 16, (100 << 2) + 1, (200 << 2) + 3))
        self.assertEqual(img.calls[-1][:4], ("eoxmap", 14, 100, 200), "overzoomed like the map layer")
        self.assertGreater(a[:, :, 1].mean(), a[:, :, 0].mean(), "the source's green gradient stays green")
        self.assertIsNone(maptiles.MapTiles(FakeImagery(missing=("eoxmap",))).layer_tile("apple", 5, 1, 1))

    def test_apple_style_palette(self):
        a = np.zeros((3, 1, 3), np.uint8)
        a[0] = (100, 150, 210)          # water
        a[1] = (120, 120, 120)          # a road (dark neutral in terrain-light)
        a[2] = (250, 250, 250)          # a block
        out = maptiles.apple_style(a).astype(int)
        self.assertGreater(out[0, 0, 2], out[0, 0, 0] + 40, "water is blue")
        self.assertTrue((out[1, 0] >= 248).all(), f"a road is white: {out[1, 0]}")
        self.assertTrue(225 <= out[2, 0, 0] <= 245, f"a block is soft near-white: {out[2, 0]}")
        self.assertGreater(out[1, 0].mean(), out[2, 0].mean(), "roads show over the blocks")
        self.assertEqual(maptiles.apple_style(a).dtype, np.uint8)
        self.assertEqual(maptiles.apple_style(np.full((2, 2, 3), 255, np.uint8)).shape, (2, 2, 3))

    def test_apple_dark_palette_is_a_real_dark_mode(self):
        a = np.zeros((4, 1, 3), np.uint8)
        a[0] = (100, 150, 210)          # water
        a[1] = (150, 150, 150)          # a motorway (the darkest roads in terrain-light)
        a[2] = (215, 215, 215)          # a minor road
        a[3] = (250, 250, 250)          # a block
        out = maptiles.apple_style(a, dark=True).astype(int)
        self.assertLess(out[3, 0].mean(), 60, f"the land goes dark, not dimmed: {out[3, 0]}")
        self.assertGreater(int(out[0, 0, 2]), int(out[0, 0, 0]) + 30, "water stays a saturated blue")
        self.assertGreater(out[2, 0].mean(), out[3, 0].mean() + 40, "minor roads are light on the dark land")
        self.assertGreater(out[1, 0].mean(), out[2, 0].mean() + 40, "and motorways are the brightest")
        self.assertEqual(maptiles.LAYERS["appledark"], "jpg")

    def test_dark_layer_and_labels(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        self.assertEqual(t.layer_tile("appledark", 12, 1176, 1561)[:2], b"\xff\xd8")
        self.assertEqual(img.calls[-1], ("eoxmap", 12, 1176, 1561, maptiles.FETCH_KIND))
        png = t.layer_tile("labelsdark", 12, 1176, 1561)
        self.assertEqual(png[:4], b"\x89PNG", "still a png overlay")
        self.assertEqual(img.calls[-1][0], "eoxlabels")

    def test_sathd_layer_is_the_imagery(self):
        img = FakeImagery()
        t = maptiles.MapTiles(img)
        self.assertEqual(t.layer_tile("sathd", 10, 100, 200), t.layer_tile("sat", 10, 100, 200),
                         "the same imagery at the source's own zoom")
        deep = t.layer_tile("sathd", 16, (100 << 2) + 1, (200 << 2) + 3)
        self.assertEqual(deep[:2], b"\xff\xd8")
        self.assertEqual(decode(deep).shape, decode(t.layer_tile("sat", 16, 0, 0)).shape)

    def test_terrain_sharpen_crisps_an_edge(self):
        a = np.full((64, 64, 3), 90, np.uint8)
        a[:, 32:] = 150
        sharp = np.asarray(Image.fromarray(a).filter(maptiles.TERRAIN_SHARPEN)).astype(int)
        edge = np.abs(np.diff(a[32, :, 0].astype(int)))
        self.assertGreater(np.abs(np.diff(sharp[32, :, 0])).max(), edge.max(),
                           "the overzoom's soft edge gains contrast")


class FakeLayers:
    """MapTiles.layer_tile with a delay per tile, a gate the first build waits on, and tiles with nothing."""

    def __init__(self, delay=0.0, gate=None, missing=(), fail=()):
        self.delay, self.gate = delay, gate
        self.missing, self.fail = set(missing), set(fail)
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
        return b"tile"


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
