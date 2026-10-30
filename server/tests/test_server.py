"""Protocol 4 end-to-end against an in-process server with a synthetic DEM and synthetic
imagery (no network): anchors, terrain-follow, far-field swap groups, rebase, in terrain
near-visual mode with imagery off (the v1.2 behaviour) and in mesh mode with imagery, the mesh
mode with both tile layouts (docs/V1_4_SPEC.md §3.1 M1: 32 cells = v1.3.1, FORMAT 3, the default;
64 cells = FORMAT 4 with hello.cells and far_tile.tex), with the M4 512² terrain block (§3.5:
--block 512, hello.blockSize, terrain.size 512 on a 128 m grid, the 512 follow table's prefetch), and
with the M1h horizon (§3.6: --horizon, hello.horizon, status.radius, the disc to the line-of-sight
horizon with L10 tiles, a bigger grid for a climbing eye, a grid per anchor)."""
import asyncio
import glob
import math
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdae  # noqa: E402
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import heightmap  # noqa: E402
import imagery  # noqa: E402
import mapcells  # noqa: E402
import modsim  # noqa: E402
from netclient import Client, FarState  # noqa: E402

LAT, LON = 36.5785, -118.2923


class FakeDem:
    def sample(self, lat, lon, zoom, progress=None):
        if progress:
            progress(1, 1)
        return 1500 + 300 * np.sin(np.asarray(lat) * 90) * np.cos(np.asarray(lon) * 70)

    def stats_snapshot(self):
        return {}


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def cells(keys, cell=256.0):
    """Set of `cell`-sized squares covered by tiles (cell <= the smallest tile)."""
    out = set()
    for k in keys:
        t = ff.tile_of(k)
        n = int(t.size // cell)
        out |= {(t.i * n + a, t.j * n + b) for a in range(n) for b in range(n)}
    return out


class FakeImagery(imagery.Imagery):
    """Imagery whose 'downloads' are synthetic tiles: seeded noise, red channel 60 for usgs and
    140 for the EOX layers (no network). usgs has data only east of LON (so the chain falls back to eox2023
    west of it); counts every load."""

    def __init__(self, cache_dir, chain=("usgs", "eox2023", "eox2016"), **kw):     # (no lazy NAIP: timing)
        super().__init__(cache_dir, chain, **kw)
        self.loads = 0

    def _load(self, key):
        src, z, x, y = key
        self.loads += 1
        if src.name == "usgs" and (x + 0.5) / 2 ** z * 360 - 180 < LON:
            return None
        rng = np.random.default_rng([z, x, y])
        a = rng.integers(20, 220, (256, 256, 3), dtype=np.uint8)
        a[..., 0] = 60 if src.name == "usgs" else 140         # tells the sources apart
        return imagery.as_tile(a)


class ServerHarness(unittest.TestCase):
    """An in-process server with the synthetic DEM and a connected client, plus protocol helpers
    (no tests here; also used by test_protocol_agreement.py). Defaults: the v1.2 behaviour
    (--near-visual terrain --imagery off); cells: the tile layout (--cells); block: the terrain
    block size (--block); horizon: --horizon (M1h)."""
    far_format = "cdae"
    near_visual = "terrain"
    with_imagery = False
    cells = 32
    block = 1024
    horizon = False
    MOD_FORMAT = 4              # the mod's hello.format (core.lua FORMAT: the newest it reads)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), tile_workers=3,
                                    far_format=self.far_format, imagery=None, near_visual=self.near_visual,
                                    cells=self.cells, block=self.block, horizon=self.horizon)
        self.layout = self.server.layout
        if self.with_imagery:
            self.server.imagery = FakeImagery(os.path.join(self.tmp.name, "cache", "imagery"))
        self.server.dem = FakeDem()
        self.server.prune_delay = 0.5
        self.server.prune_recheck = 0.05
        self.server.delete_interval = 0.002
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def start():
            return await asyncio.start_server(
                lambda r, w: earthd.Connection(self.server, r, w).run(), "127.0.0.1", 0)
        self.srv = asyncio.run_coroutine_threadsafe(start(), self.loop).result(10)
        self.c = Client(self.srv.sockets[0].getsockname()[1])

    def tearDown(self):
        self.c.close()

        async def stop():
            self.srv.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.05)       # let the transports close
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()
        self.server.tile_pool.shutdown(wait=True)
        self.server.hm_pool.shutdown(wait=True)
        if self.server.horizon_pool is not None:
            self.server.horizon_pool.shutdown(wait=True)
        if self.server.imagery is not None:
            self.server.imagery.close()
        self.tmp.cleanup()

    def conn(self):
        return next(iter(self.server.connections))

    def fs(self, vfs):
        self.assertTrue(vfs.startswith(f"/temp/beamngEarth/{self.server.sid}/"))
        return os.path.join(self.tmp.name, vfs.lstrip("/"))

    def hello(self, mod_format=None):
        self.c.send({"type": "hello", "protocol": 4, "format": mod_format or self.MOD_FORMAT, "modVersion": "0.4.0"})
        m, seen = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertFalse([s for s in seen if s["type"] == "status" and "mismatch" in s["text"]])
        self.assertEqual((m["protocol"], m["format"]), (4, {32: 3, 64: 4}[self.cells]))
        # M1: hello.cells with FORMAT 4 only (a FORMAT 3 hello is exactly v1.3.1's)
        self.assertEqual(m.get("cells"), 64 if self.cells == 64 else None)
        self.assertEqual(m.get("blockSize"), None if self.block == 1024 else self.block)     # M4: --block 512 only
        self.assertEqual(m.get("horizon"), True if self.horizon else None)                 # M1h: --horizon only
        self.assertEqual(m["sessionId"], self.server.sid)
        self.assertEqual(m["farFormat"], self.far_format)
        self.assertEqual(m["material"], ff.MATERIAL)
        self.assertEqual(m["nearVisual"], self.near_visual)
        self.assertIn("Elevation:", m["attribution"])
        self.assertEqual("Imagery:" in m["attribution"], self.with_imagery)

    def prepare_anchor(self, req, lat, lon, **hint):
        self.c.send({"type": "prepare_anchor", "requestId": req, "lat": lat, "lon": lon, **hint})
        m, _ = self.c.until(lambda m: m["type"] in ("anchor_ready", "anchor_error"), timeout=60)
        self.assertEqual(m["type"], "anchor_ready", m)
        self.assertEqual(m["requestId"], req)
        return m

    def pose(self, aid, x, y, vx=0.0, vy=0.0, held=None):
        m = {"type": "pose", "anchorId": aid, "x": x, "y": y, "z": 0, "lat": LAT, "lon": LON,
             "speed": math.hypot(vx, vy), "vx": vx, "vy": vy,
             "cam": {"x": x, "y": y - 8, "z": 3, "fx": 0, "fy": 1, "fz": 0, "fovDeg": 60}}
        if held is not None:
            m["held"] = held
        self.c.send(m)

    def stream_until_done(self, far, anchor_id, timeout=120):
        done, seen = self.c.until(lambda m: m["type"] == "far_done" and m["anchorId"] == anchor_id,
                                  timeout=timeout)
        far.apply(seen)
        return done, seen

    def check_terrain(self, t, cx, cy):
        self.assertEqual(set(t), {"cx", "cy", "heightmap", "size", "squareSize", "zBase", "maxHeight", "q", "ter"})
        self.assertEqual((t["cx"], t["cy"]), (cx, cy))
        self.assertEqual((t["size"], t["squareSize"]), (self.block, 2.0))
        self.assertEqual(t["maxHeight"], t["q"] * 65536)
        self.assertEqual(t["zBase"] % t["q"], 0)
        with Image.open(self.fs(t["heightmap"])) as im:
            self.assertEqual(im.size, (self.block, self.block))
            q = np.asarray(im).astype(np.int64)[::-1]           # [gy, gx]
        return heightmap.heights(q, t["zBase"], t["maxHeight"])

    def check_tile_msgs(self, msgs, aid):
        for m in msgs:
            if m["type"] != "far_tile":
                continue
            keys = {"type", "anchorId", "key", "level", "shape", "pos", "err", "skirt", "state"}
            if m["level"] == 0 and self.near_visual == "terrain":
                keys.add("sink")
            if self.with_imagery:
                keys |= {"material", "texture", "detail"}
                if self.far_format == "cdae":           # a pooled shape file's send serial (matslots.py)
                    keys.add("slotSerial")
                if self.layout.format >= 4:             # M1: the texture size (the mod's unit kind)
                    keys.add("tex")
                    self.assertEqual(m["tex"], self.server.tex_size(m["level"]))    # M1h: 256/128 px from L8
                self.assertTrue(self.server.matslots.owns((aid, m["key"]), m["material"]), m)     # its pooled name
                self.assertEqual(m["texture"], f"/temp/beamngEarth/{self.server.sid}/far_{aid}_{m['key']}.dds")
                self.assertIs(m["detail"], True)
                self.assertTrue(os.path.exists(self.fs(m["texture"])))
            self.assertEqual(set(m), keys, m)
            t = ff.tile_of(m["key"])
            self.assertEqual(m["level"], t.level)
            self.assertEqual(m["pos"][:2], list(t.bbox[:2]))
            self.assertEqual(m["state"], "group")
            own = f"far_{aid}_{m['key']}.{self.far_format}"        # a textured cdae tile: its pooled file (matslots.py)
            if "material" in m and self.far_format == "cdae":
                own = f"slot_{m['material'].rsplit('_', 1)[1]}.cdae"
            self.assertEqual(m["shape"], f"/temp/beamngEarth/{self.server.sid}/{own}")
            self.assertGreaterEqual(m["err"], 0)
            self.assertGreaterEqual(m["skirt"], ff.skirt_depth(t.size, m["err"]) - 0.01)
            if "sink" in m:
                self.assertGreaterEqual(m["sink"], ff.SINK_MARGIN)
            self.assertTrue(os.path.exists(self.fs(m["shape"])))

    def check_swaps(self, msgs, aid):
        for m in msgs:
            if m["type"] != "far_swap":
                continue
            self.assertEqual(set(m), {"type", "anchorId", "id", "add", "remove", "dIn", "dOut"})
            self.assertEqual(m["anchorId"], aid)
            self.assertTrue(m["add"] or m["remove"])
            self.assertGreaterEqual(min(m["dIn"], m["dOut"]), 0)
            if m["add"] and m["remove"]:        # no hole, no overlap (on the group's smallest tile)
                cell = min(ff.tile_of(k).size for k in m["add"] + m["remove"])
                self.assertEqual(cells(m["add"], cell), cells(m["remove"], cell))

    def check_cover(self, far, aid, px, py, blocks, radius=ff.RADIUS):
        """The shown tiles tile the radius square exactly once (checked on 64 m cells), with all
        L0 under the blocks."""
        tiles = {k: ff.tile_of(k) for k in far.keys(aid)}
        c = cells(tiles, 64.0)
        n = sum(int(t.size // 64) ** 2 for t in tiles.values())
        self.assertEqual(n, len(c), "overlapping tiles")
        x0, x1 = math.floor((px - radius) / 64), math.ceil((px + radius) / 64)
        y0, y1 = math.floor((py - radius) / 64), math.ceil((py + radius) / 64)
        want = {(i, j) for i in range(x0, x1) for j in range(y0, y1)}
        self.assertFalse(want - c, "uncovered cells")
        for b in blocks:
            bb = ff.Block(*b).bbox
            under = [t for t in tiles.values() if overlaps(t.bbox, bb)]
            self.assertEqual(len(under), 64)
            self.assertTrue(all(t.level == 0 for t in under))


class ServerTest(ServerHarness):
    far_format = "cdae"

    def test_session(self):
        c, far = self.c, FarState()
        self.hello()

        # --- teleport: anchor 1 with its first terrain at (0, 0) ---
        a1 = self.prepare_anchor(1, LAT, LON)
        self.assertEqual(set(a1), {"type", "requestId", "anchorId", "lat", "lon", "groundZ", "floorZ", "terrain",
                                   "provisional"})
        self.assertFalse(a1["provisional"], "no grading here: the first block is the graded one")
        self.assertIsNone(a1["floorZ"], "not in a tunnel")
        aid = a1["anchorId"]
        h = self.check_terrain(a1["terrain"], 0.0, 0.0)
        self.assertEqual(a1["terrain"]["q"], 1 / 64)                  # 600 m relief
        self.assertEqual(a1["terrain"]["heightmap"], f"/temp/beamngEarth/{self.server.sid}/hm_{aid}_0_0.png")
        self.assertAlmostEqual(a1["groundZ"], h[512, 512], delta=0.001)     # sample (512, 512) = (0, 0)

        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0, 0)
        t0 = time.monotonic()
        done, seen = self.stream_until_done(far, aid)
        print(f"\n  initial far set: {done['count']} tiles in {time.monotonic() - t0:.2f} s (synthetic DEM, "
              f"build median {1000 * sorted(self.server.build_stats)[len(self.server.build_stats) // 2]:.1f} ms)",
              file=sys.stderr)
        want, _ = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        self.assertEqual(far.keys(aid), set(want))
        self.assertEqual(done["count"], len(want))
        self.assertFalse(far.held)
        tiles = [m for m in seen if m["type"] == "far_tile"]
        self.assertEqual(len(tiles), len(want))         # each sent once
        self.check_tile_msgs(seen, aid)
        self.check_swaps(seen, aid)
        self.check_cover(far, aid, 0, 0, [(0, 0)])
        # The initial set is one-sided groups (nothing to replace).
        self.assertTrue(all(not s["remove"] for s in far.swaps))
        # The 64 L0 tiles under the committed block come first, then nearest-first.
        under = [m for m in tiles if overlaps(ff.tile_of(m["key"]).bbox, ff.Block(0, 0).bbox)]
        # Builds run tile_workers at a time, so the last of them may finish after a tile that
        # started alongside it (seen under CPU load): allow that much slack.
        first = {m["key"] for m in tiles[:64 + self.server.tile_workers - 1]}
        self.assertEqual(len(under), 64)
        self.assertLessEqual({m["key"] for m in under}, first)
        # The other tiles nearest first, up to the same completion-order jitter. The block tiles
        # are left out: the slack above lets a corner one (1086 m away) arrive after position
        # 64, ahead of the 1024 m ring outside the block (a flaky failure under CPU load when
        # this took tiles[64:]).
        under_keys = {m["key"] for m in under}
        # (view-weighted, 2026-10-16: the poses' camera looks north from 8 m behind the player)
        cone = ff.view_cone({"x": 0, "y": -8, "fx": 0, "fy": 1, "fovDeg": 60})
        rest = [ff.view_distance(want[m["key"]], cone, 0, 0) for m in tiles if m["key"] not in under_keys]
        ds = sorted(rest)
        slack = self.server.tile_workers
        late = [i for i in range(len(rest)) if rest[i] > ds[min(i + slack, len(ds) - 1)]]
        self.assertFalse(late, [(i, rest[i], ds[i]) for i in late[:5]])
        # The shapes are .cdae with the static material.
        shape = cdae.read(self.fs(tiles[0]["shape"])) if self.far_format == "cdae" else None
        if shape:
            self.assertEqual(shape["materials"][0]["name"], ff.MATERIAL)

        # --- the player drives 600 m east: swap groups only ---
        mark = len(far.order)
        self.pose(aid, 600, 0)
        done, seen = self.stream_until_done(far, aid)
        self.check_tile_msgs(seen, aid)
        self.check_swaps(seen, aid)
        swaps = [m for m in seen if m["type"] == "far_swap"]
        self.assertTrue(swaps)
        self.assertTrue(any(m["add"] and m["remove"] for m in swaps))
        conn = self.conn()
        self.assertEqual(far.keys(aid), set(conn.desired))
        self.assertEqual(conn.predicted_block, ff.Block(512.0, 0.0))     # 600 m out: the next block is forced too
        self.check_cover(far, aid, 600, 0, [(0, 0), (512, 0)])
        self.assertFalse(far.held)
        # Every tile shown later was sent before its swap.
        for kind, a, x in far.order[mark:]:
            if kind == "swap":
                self.assertFalse(set(x["add"]) & set(far.held))

        # --- terrain-follow: prepare + commit a block centred at (512, 0): no tile changes ---
        c.send({"type": "prepare_terrain", "requestId": 5, "anchorId": aid, "cx": 512, "cy": 0})
        tr, seen = c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        far.apply(seen)
        self.assertFalse([m for m in seen if m["type"] in ("far_tile", "far_swap")])
        self.assertEqual(set(tr), {"type", "requestId", "anchorId", "cx", "cy", "heightmap", "size",
                                   "squareSize", "zBase", "maxHeight", "q", "ter"})
        self.assertEqual((tr["requestId"], tr["anchorId"]), (5, aid))
        h2 = self.check_terrain({k: tr[k] for k in tr if k not in ("type", "requestId", "anchorId")}, 512.0, 0.0)
        # Same anchor frame and quantum: the overlap is bit-identical (§1.8). Block 2 sample
        # (gx, gy) is block 1 sample (gx + 256, gy).
        self.assertTrue((h2[:, :768] == h[:, 256:]).all())
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 512, "cy": 0})
        quiet = c.drain(0.5)
        far.apply(quiet)
        self.assertFalse([m for m in quiet if m["type"].startswith("far_")], "a commit changed far tiles")
        self.assertEqual(self.conn().block, ff.Block(512.0, 0.0))

        # --- prefetch: driving east at 30 m/s from x = 900 predicts a block at 1024 ---
        self.pose(aid, 900, 0, vx=30)
        path = os.path.join(self.server.out_dir, f"hm_{aid}_1024_0.png")
        end = time.monotonic() + 30
        while not os.path.exists(path) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(path), "prefetch did not build the predicted terrain")
        time.sleep(0.2)
        t0 = time.monotonic()
        c.send({"type": "prepare_terrain", "requestId": 6, "anchorId": aid, "cx": 1024, "cy": 0})
        tr, seen = c.until(lambda m: m["type"] == "terrain_ready", timeout=30)
        self.assertEqual((tr["requestId"], tr["cx"]), (6, 1024.0))
        self.assertLess(time.monotonic() - t0, 0.15)            # served from the cache
        far.apply(seen)
        if not [m for m in seen if m["type"] == "far_done"] or self.conn().dirty:
            self.stream_until_done(far, aid)
        self.check_cover(far, aid, 900, 0, [(512, 0), (1024, 0)])

        # --- origin rebase: new anchor at the player's position, terrain hinted at (256, 0) ---
        lat2, lon2, _ = self.server.anchors[aid].frame.enu_to_geodetic(900.0, 0.0, 0.0)
        a2 = self.prepare_anchor(7, float(lat2), float(lon2), cx=300, cy=-20)
        aid2 = a2["anchorId"]
        self.assertGreater(aid2, aid)
        self.check_terrain(a2["terrain"], 256.0, 0.0)
        old_keys = far.keys(aid)
        f1, f2 = self.server.anchors[aid].frame, self.server.anchors[aid2].frame
        mark = len(far.order)
        c.send({"type": "commit_anchor", "anchorId": aid2})
        far.switch(aid2)
        c.send({"type": "commit_terrain", "anchorId": aid2, "cx": 256, "cy": 0})
        self.pose(aid2, 0, 0, held=[aid])                   # the mod keeps the old tiles meanwhile
        done, seen = self.stream_until_done(far, aid2)
        self.check_swaps(seen, aid2)
        # (hysteresis may keep a node refined that a fresh select() at exactly (0, 0) would not)
        self.assertEqual(far.keys(aid2), set(self.conn().desired))
        self.check_cover(far, aid2, 0, 0, [(256, 0)])
        self.assertEqual(far.keys(aid), set())              # every old tile was removed...
        order = far.order[mark:]
        old_removed = [i for i, (k, a, _) in enumerate(order) if k == "remove" and a == aid]
        self.assertEqual(len(old_removed), len(old_keys))
        # ...each one only after every new tile covering its (transformed) area had been swapped in.
        for r in old_removed:
            bb = ff.transform_bbox(f1, f2, ff.tile_of(order[r][2]).bbox)
            later = [k for kd, a, x in order[r + 1:] if kd == "swap" and a == aid2 for k in x["add"]]
            self.assertFalse([k for k in later if overlaps(ff.tile_of(k).bbox, bb)], order[r])
        self.assertGreater(max(old_removed), len(order) // 2)   # progressively, not all up front
        self.assertFalse([m for m in seen if m["type"] in ("far_tile", "far_swap", "far_done")
                          and m["anchorId"] == aid])
        # The superseded anchor's files go once the new set is complete, after a delay (the mod
        # may still show old tiles right after far_done)...
        before = len(glob.glob(os.path.join(self.server.out_dir, f"far_{aid}_*")))
        self.assertTrue(before)
        # ...and never while the mod still has objects of that anchor (pose.held). In-game
        # 2026-10-10 a rebase reload at speed outlasted the fixed delay: the engine then logged
        # "Could not open '...far_<old>_....cdae'" for every old tile still shown.
        time.sleep(4 * self.server.prune_delay)
        self.assertEqual(len(glob.glob(os.path.join(self.server.out_dir, f"far_{aid}_*"))), before)
        self.assertIn(aid, self.server.held_anchors())
        self.pose(aid2, 0, 0, held={})                      # empty Lua table: may encode as {}
        # Deleted gradually, not in one burst (a burst stalled the game ~300 ms, in-game 2026-10-10):
        # once deletion has started there are still files left for a while.
        # (delete_interval 2 ms: on Windows ~16 ms, its event loop's timer resolution)
        end = time.monotonic() + 5 + 0.02 * before
        partial = False
        while time.monotonic() < end:
            n = len(glob.glob(os.path.join(self.server.out_dir, f"far_{aid}_*")))
            partial |= 0 < n < before
            if n == 0:
                break
            time.sleep(0.01)
        self.assertTrue(partial, "old tile files were deleted all at once")
        self.assertEqual(glob.glob(os.path.join(self.server.out_dir, f"far_{aid}_*")), [])
        self.assertTrue(glob.glob(os.path.join(self.server.out_dir, f"far_{aid2}_*")))

    def test_hysteresis_no_churn(self):
        """A player oscillating +-40 m around an LOD threshold changes nothing after the first swap."""
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, -40, 0)
        self.stream_until_done(far, aid)
        self.pose(aid, 40, 0)                   # L1 node 1_3_0 refines (d < 3 * 512)
        _, seen = self.stream_until_done(far, aid)
        self.assertTrue([m for m in seen if m["type"] == "far_swap" and "1_3_0" in m["remove"]])
        time.sleep(0.3)
        for x in (-40, 40, -40, 40, -40):
            self.pose(aid, x, 0)
        quiet = c.drain(0.6)
        self.assertFalse([m for m in quiet if m["type"].startswith("far_")], quiet[:3])

    def test_pending_block_streams_first(self):
        """prepare_terrain forces L0 over the pending block, built ahead of everything else."""
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0, 0)
        self.stream_until_done(far, aid)
        # A block 3 km away (beyond the L0 range of the K rule): its 64 L0 tiles are new.
        c.send({"type": "prepare_terrain", "requestId": 2, "anchorId": aid, "cx": 3072, "cy": 3072})
        _, seen = self.stream_until_done(far, aid)
        bb = ff.Block(3072, 3072).bbox
        tiles = [m["key"] for m in seen if m["type"] == "far_tile"]
        l0 = [k for k in tiles if ff.tile_of(k).level == 0 and overlaps(ff.tile_of(k).bbox, bb)]
        self.assertEqual(len(l0), 64)
        self.assertLessEqual(set(l0), set(tiles[:64 + self.server.tile_workers - 1]))    # see test_session
        self.check_cover(far, aid, 0, 0, [(0, 0), (3072, 3072)])

    def test_resync_hint_far_from_origin_not_clamped(self):
        """Regression (in-game 2026-10-10): after a server restart the mod re-registers its origin
        and asks for the block around the player, e.g. (1280, 0). Clamping that to ±768 made the
        mod reject every answer as stale and re-request ~4 times per second, forever."""
        self.hello()
        for i, (cx, cy) in enumerate([(1280, 0), (-9984, 4096), (16128, -16128)]):  # up to the 16 km rebase max
            t = self.prepare_anchor(10 + i, LAT, LON, cx=cx, cy=cy)["terrain"]
            self.assertEqual((t["cx"], t["cy"]), (cx, cy))

    def test_teleport_streams_around_new_origin(self):
        """After a teleport the far field is built around the new origin even before
        the first pose in the new frame (the mod sends none while placing the player)."""
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 3000, 0)
        self.stream_until_done(far, aid)
        aid2 = self.prepare_anchor(2, LAT + 2, LON + 3)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid2})
        far.switch(aid2)
        c.send({"type": "commit_terrain", "anchorId": aid2, "cx": 0, "cy": 0})
        done, seen = self.stream_until_done(far, aid2)
        want, _ = ff.select(0.0, 0.0, [ff.Block(0.0, 0.0).bbox])
        adds = [k for m in seen if m["type"] == "far_swap" and m["anchorId"] == aid2 for k in m["add"]]
        self.assertEqual(sorted(adds), sorted(want))
        self.assertEqual(far.keys(aid2), set(want))
        self.assertEqual(far.keys(aid), set())

    def test_resync_streams_around_first_terrain(self):
        """A resync after a server restart commits an anchor whose first terrain is around a
        distant player: before its first pose the far field starts there, not at the origin."""
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON, cx=12288, cy=-4096)["anchorId"]

        async def commit():
            self.conn().commit_anchor(aid)
            return self.conn().focus
        self.assertEqual(asyncio.run_coroutine_threadsafe(commit(), self.loop).result(10), (12288.0, -4096.0))

    def test_fresh_terrain_survives_eviction_after_rebase(self):
        """Regression (integration drive, 2026-10-10): after a rebase the protected heightmaps (the
        last three committed, the last two sent, every recent anchor's first terrain) outnumbered
        HM_CACHE, so each new prefetch was deleted as soon as it was built and rebuilt on every
        pose. The terrain just built is kept, and a committed anchor's first terrain is not
        protected any more."""
        s = self.server
        frame = s.new_anchor(LAT, LON).frame
        s.anchors = {1: earthd.Anchor(1, LAT, LON, frame, first_terrain=(0.0, 0.0)),
                     2: earthd.Anchor(2, LAT, LON, frame, first_terrain=(512.0, 0.0))}

        class Conn:
            active = 2
        s.connections = {Conn()}
        s.hm_committed.extend([(1, 2560.0, 0.0), (1, 3072.0, 0.0), (2, 512.0, 0.0)])
        s.hm_sent.extend([(2, 512.0, 0.0), (2, 1024.0, 256.0)])
        for key in [(1, 0.0, 0.0), (1, 2048.0, 0.0), (1, 2560.0, 0.0), (1, 3072.0, 0.0), (2, 512.0, 0.0),
                    (2, 1024.0, 256.0)]:
            s.hm[key] = {}
        fresh = (2, 768.0, 256.0)
        s.hm[fresh] = {}
        s.evict_heightmaps(fresh=fresh)
        self.assertIn(fresh, s.hm)
        self.assertLessEqual(len(s.hm), earthd.HM_CACHE)
        self.assertNotIn((1, 0.0, 0.0), s.hm)               # anchor 1 is committed: not protected
        for key in set(s.hm_committed) | set(s.hm_sent):
            self.assertIn(key, s.hm)
        s.connections = set()

    def test_a_slow_terrain_request_is_logged_and_still_answered(self):
        c = self.c
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        slow = self.server.make_heightmap

        def make(*a, **kw):
            time.sleep(0.5)
            return slow(*a, **kw)
        with mock.patch.object(earthd, "TERRAIN_STUCK_S", 0.2), mock.patch.object(self.server, "make_heightmap", make), \
                self.assertLogs("earthd", "WARNING") as logs:
            c.send({"type": "prepare_terrain", "requestId": 3, "anchorId": aid, "cx": 512, "cy": 0})
            tr, _ = c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        self.assertEqual(tr["requestId"], 3)
        self.assertTrue(any("still building" in m for m in logs.output), logs.output)

    def test_prefetch_predicts_from_the_pending_block(self):
        """With a terrain request pending, the mod's next request comes after it commits that one,
        so the prefetch predicts from the pending block (not the committed one)."""
        c = self.c
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        c.send({"type": "prepare_terrain", "requestId": 2, "anchorId": aid, "cx": 512, "cy": 0})
        c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        self.pose(aid, 760, 0, vx=30)
        want = ff.Block(*earthd.predict_request(760, 0, 30, 0, 512, 0))
        self.assertEqual(want, ff.Block(1024.0, 0.0))
        self.assertNotEqual(want, ff.Block(*earthd.predict_request(760, 0, 30, 0, 0, 0)))
        end = time.monotonic() + 10
        while self.conn().predicted_block != want and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(self.conn().predicted_block, want)
        self.assertEqual(self.conn().pending_block, ff.Block(512.0, 0.0))

    def test_retired_tiles_survive_cache_eviction(self):
        """A tile swapped out (or evicted from the held set) stays on disk for retired_keep: the mod
        still shows it until the swap has run and deletes it time-sliced (minutes with a backlog)."""
        self.assertGreaterEqual(earthd.RETIRED_KEEP, 600.0)
        self.server.retired_keep = 60.0

        async def run():
            conn = self.conn()
            conn.active = 1
            conn._retire([(1, "0_0_0")])
            used = self.server.in_use()
            conn.retired[(1, "0_0_0")] -= 61.0
            return used, self.server.in_use()
        self.hello()
        used, later = asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)
        self.assertIn((1, "0_0_0"), used)
        self.assertNotIn((1, "0_0_0"), later)

    def test_cache_eviction_spares_tiles_still_to_send(self):
        """Regression (in-game 2026-10-10): with the cache over TILE_CACHE, eviction must not pick
        tiles of the active anchor that are desired but not sent yet: their files were deleted
        right before the far_tile went out, and the mod's import failed ("No Mesh")."""
        self.assertEqual(self.server.tile_cache, earthd.TILE_CACHE[self.cells])
        self.server.tile_cache = 40     # far below the ~700-tile initial set (this server only)
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0, 0)
        done, seen = self.stream_until_done(far, aid)
        missing = [m["shape"] for m in seen if m["type"] == "far_tile"
                   and not os.path.exists(os.path.join(self.server.out_dir, os.path.basename(m["shape"])))]
        self.assertEqual(missing, [])

    def test_heightmap_cleanup_and_errors(self):
        c = self.c
        self.hello()
        a = self.prepare_anchor(1, LAT, LON)
        aid = a["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        for n, cx in enumerate(range(256, 256 * 10, 256)):
            c.send({"type": "prepare_terrain", "requestId": 10 + n, "anchorId": aid, "cx": cx, "cy": 0})
            m, _ = c.until(lambda m: m["type"] == "terrain_ready" and m["requestId"] == 10 + n, timeout=60)
            if n == 2:
                c.send({"type": "commit_terrain", "anchorId": aid, "cx": cx, "cy": 0})
        time.sleep(0.2)
        files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(self.server.out_dir, "hm_*.png")))
        self.assertLessEqual(len(files), earthd.HM_CACHE, files)
        # Never deleted: the committed terrain, the two before it, the last two handed out.
        for name in (f"hm_{aid}_0_0.png", f"hm_{aid}_768_0.png", f"hm_{aid}_2048_0.png", f"hm_{aid}_2304_0.png"):
            self.assertIn(name, files)
        self.assertNotIn(f"hm_{aid}_256_0.png", files)
        # Unknown anchor / missing fields -> terrain_error; the connection stays up.
        c.send({"type": "prepare_terrain", "requestId": 99, "anchorId": 42, "cx": 0, "cy": 0})
        m, _ = c.until(lambda m: m["type"] == "terrain_error", timeout=10)
        self.assertEqual((m["requestId"], m["anchorId"]), (99, 42))
        c.send({"type": "prepare_terrain", "requestId": 100, "anchorId": aid})
        m, _ = c.until(lambda m: m["type"] == "terrain_error", timeout=10)
        self.assertEqual(m["requestId"], 100)
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": "x"})
        c.send({"type": "pose", "anchorId": aid, "x": float("nan")})
        self.hello()

    def test_protocol_mismatch(self):
        self.c.send({"type": "hello", "protocol": 3, "modVersion": "0.3.0"})
        m, _ = self.c.until(lambda m: m["type"] == "status", timeout=10)
        self.assertIn("protocol mismatch", m["text"])
        self.assertEqual(m["backlog"], 0)               # no anchor yet
        # a v1.3.1 mod (format 3) is fine with the 32-cell server; any newer format too (it reads 3)
        self.hello(3)
        self.hello(4)

    def test_vram_profile_scaling(self):
        """Feature 2: Dynamic VRAM-based render distance and tracker radius scaling."""
        # 1) Send hello with high VRAM (e.g. 16 GB -> ultra profile)
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0", "vramMB": 16384})
        m, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual(m.get("vramProfile"), "ultra")
        conn = self.conn()
        self.assertEqual(conn.vram_profile, "ultra")
        self.assertEqual(conn.roads.send_m, 1152.0)
        self.assertEqual(conn.veg.send_m, 2304.0)
        self.assertEqual(conn.bldfar.send_m, 3200.0)

        # 2) Send hello with 12 GB -> high profile
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0", "vramMB": 12288})
        m2, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual(m2.get("vramProfile"), "high")
        self.assertEqual(conn.roads.send_m, 896.0)
        self.assertEqual(conn.veg.send_m, 1792.0)
        self.assertEqual(conn.bldfar.send_m, 2400.0)

        # 3) Send hello with 4 GB -> low profile
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0", "vramMB": 4096})
        m3, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual(m3.get("vramProfile"), "low")
        self.assertEqual(conn.roads.send_m, 512.0)
        self.assertEqual(conn.veg.send_m, 960.0)
        self.assertEqual(conn.bldfar.send_m, 1200.0)

    def test_status_backlog(self):
        """status.backlog (V1_4_SPEC §4, M0) = desired - shown of the current anchor. Status goes out
        on events only by default (v1.3.1); --status-interval re-sends it while the backlog changes."""
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0, 0)
        _, seen = self.stream_until_done(far, aid)
        rest = c.drain(1.0)                 # the set around the predicted block follows; then quiet
        far.apply(rest)
        seen += rest
        status = [m for m in seen if m["type"] == "status"]
        ready = [m for m in status if m["text"].startswith("Far field ready")]
        self.assertTrue(ready)
        self.assertEqual(status[-1], ready[-1])
        self.assertEqual(ready[-1]["backlog"], 0)
        loading = [m for m in status if m["text"].startswith("Far field loading")]
        self.assertTrue(loading and all(m["backlog"] > 0 for m in loading), loading[:2])
        # default: poses alone send no status (v1.3.1)
        for _ in range(3):                  # (0, 0) sits exactly on the 1536 m L1 split distance
            self.pose(aid, 0, 0)
        self.assertFalse([m for m in c.drain(0.4) if m["type"] == "status"])
        # --status-interval: a move far enough to change the far set reports the backlog, which the
        # streaming then drains back to 0
        self.server.status_interval = 0.01
        conn = self.conn()
        self.pose(aid, 3000, 0)
        backlogs = []
        for _ in range(400):
            m, seen = c.until(lambda m: m["type"] in ("status", "far_done"), timeout=60)
            far.apply(seen + [m])
            if m["type"] == "status":
                backlogs.append(m["backlog"])
            elif m["anchorId"] == aid:
                break
            self.pose(aid, 3000, 0)
        self.assertGreater(max(backlogs), 20)
        self.pose(aid, 3000, 0)
        time.sleep(0.05)
        self.pose(aid, 3000, 0)
        m, _ = c.until(lambda m: m["type"] == "status", timeout=10)
        self.assertEqual(m["backlog"], 0)
        self.assertEqual(conn.backlog(), 0)
        self.assertEqual(m["text"], conn.last_status)       # the last text, re-sent
        self.pose(aid, 3000, 0)                             # unchanged backlog: nothing re-sent
        self.assertFalse([m for m in c.drain(0.3) if m["type"] == "status"])


class DaeServerTest(ServerTest):
    """The --far-format dae fallback: same protocol, Collada shapes."""
    far_format = "dae"

    # One full session is enough for the fallback.
    test_hysteresis_no_churn = test_pending_block_streams_first = None
    test_resync_hint_far_from_origin_not_clamped = test_teleport_streams_around_new_origin = None
    test_heightmap_cleanup_and_errors = test_protocol_mismatch = test_status_backlog = None


class MeshServerTest(ServerHarness):
    """--near-visual mesh with imagery (the v1.3 default): tiles down to the exact level (L-2; L-1
    with 64 cells), no block forcing, no sink, every tile with its own material and DDS texture."""
    near_visual = "mesh"
    with_imagery = True

    def start(self, x=0.0, y=0.0):
        far = FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, x, y)
        t0 = time.monotonic()
        done, seen = self.stream_until_done(far, aid)
        return far, aid, done, seen, time.monotonic() - t0

    def test_session(self):
        c = self.c
        far, aid, done, seen, dt = self.start()
        stats = self.server.level_stats
        print(f"\n  mesh initial set: {done['count']} tiles in {dt:.2f} s (synthetic DEM + imagery); "
              "per level build/texture ms: " + ", ".join(
                  f"L{lvl} {1000 * np.median([v[0] for v in st]):.0f}/{1000 * np.median([v[1] for v in st]):.0f}"
                  for lvl, st in sorted(stats.items())), file=sys.stderr)
        lay = self.layout
        ex = lay.exact_level
        want, _ = ff.select(0.0, 0.0, [], min_level=lay.min_level, layout=lay)
        self.assertEqual(far.keys(aid), set(want))
        self.assertEqual(done["count"], len(want))
        self.assertEqual(min(ff.tile_of(k).level for k in want), ex)
        tiles = [m for m in seen if m["type"] == "far_tile"]
        self.check_tile_msgs(seen, aid)
        self.check_swaps(seen, aid)
        self.check_cover(far, aid, 0, 0, [])
        self.assertFalse([m for m in tiles if "sink" in m])
        # nearest first (view-weighted: the poses' camera looks north from 8 m behind the player), up to the
        # builds' completion-order jitter
        cone = ff.view_cone({"x": 0, "y": -8, "fx": 0, "fy": 1, "fovDeg": 60})
        vd = sorted(ff.view_distance(ff.tile_of(k), cone, 0, 0) for k in want)
        slack = self.server.tile_workers
        self.assertTrue(all(ff.view_distance(ff.tile_of(m["key"]), cone, 0, 0) <= vd[8 + slack] for m in tiles[:8]))
        self.assertEqual(tiles[0]["level"], ex)
        # textures: DXT1 with mips, the layout's size per level (32 cells: TEX_NEAR for the two
        # finest levels, TEX_FAR beyond; 64: 256 px to L1, 512 from L2); shapes name the material
        # and have the layout's triangle count (8704 with 64 cells) up to L0
        for m in tiles[:3] + [t for t in tiles if t["level"] >= 0][:3] + [t for t in tiles if t["level"] >= 2][:2]:
            with open(self.fs(m["texture"]), "rb") as f:
                w, h, mips, fourcc, _ = imagery.read_dds_info(f.read())
            px = lay.tex_size(m["level"])
            if self.cells == 32:
                self.assertEqual(px, earthd.TEX_NEAR if m["level"] <= -1 else earthd.TEX_FAR)
            else:
                self.assertEqual(px, 512 if m["level"] >= 2 else 256)
            self.assertEqual((w, h, mips, fourcc), (px, px, int(math.log2(px)) + 1, b"DXT1"))
            shape = cdae.read(self.fs(m["shape"]))
            self.assertEqual(shape["materials"][0]["name"], m["material"])
            self.assertEqual(shape["meshes"][0]["tverts2"][0], shape["meshes"][0]["verts"][0])
            if m["level"] < ff.ADAPT_MIN_LEVEL:      # the lattice; adaptive meshes beyond (no more)
                self.assertEqual(shape["meshes"][0]["indices"][0], 3 * lay.tris)
            else:
                self.assertLessEqual(shape["meshes"][0]["indices"][0], 3 * lay.tris)
        # the terrain block is physics only: following it changes no tile
        c.send({"type": "prepare_terrain", "requestId": 5, "anchorId": aid, "cx": 512, "cy": 0})
        tr, seen = c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        far.apply(seen)
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 512, "cy": 0})
        quiet = c.drain(0.5)
        self.assertFalse([m for m in seen + quiet if m["type"].startswith("far_")])
        # driving: every step covers the radius square exactly once, swaps replace footprints
        changes = 0
        for x in range(30, 331, 60):
            self.pose(aid, float(x), 10.0, vx=30.0)
            conn = self.conn()
            t0 = time.monotonic()
            while conn.focus != (float(x), 10.0) and time.monotonic() - t0 < 10:   # pose handled
                time.sleep(0.005)
            if set(conn.desired) == far.keys(aid):
                # a step inside the hysteresis band changes nothing (64 cells: 60 m is under half the
                # exact tile), so no far_done follows; nothing may be streamed either
                seen = c.drain(0.3)
                far.apply(seen)
                self.assertFalse([m for m in seen if m["type"].startswith("far_")], seen[:2])
            else:
                changes += 1
                done, seen = self.stream_until_done(far, aid)
            self.check_tile_msgs(seen, aid)
            self.check_swaps(seen, aid)
            self.check_cover(far, aid, float(x), 10.0, [])
            self.assertEqual(far.keys(aid), set(self.conn().desired))
            r = lay.near_radii[ex]
            for k in far.keys(aid):
                t = ff.tile_of(k)
                if t.distance(x, 10.0) < r - 0.25 * r:
                    self.assertEqual(t.level, ex, k)
        self.assertGreaterEqual(changes, 2)
        self.assertFalse(far.held)

    def test_hysteresis_no_churn_near(self):
        """+-20 m around the 150 m threshold to the exact level: no tile changes after the first
        swap. (32 cells: node -1_1_0, x 128..256, refines at d < 150 m, i.e. x > -22; 64 cells:
        node 0_1_0, x 256..512, at x > 106.)"""
        node = ff.Tile(self.layout.exact_level + 1, 1, 0)
        thr = node.bbox[0] - self.layout.near_radii[self.layout.exact_level]
        far, aid, _, _, _ = self.start(thr - 38.0, 30.0)
        self.pose(aid, thr + 20.0, 30.0)
        _, seen = self.stream_until_done(far, aid)
        self.assertTrue([m for m in seen if m["type"] == "far_swap" and node.key in m["remove"]])
        time.sleep(0.3)
        for x in (thr - 20.0, thr + 20.0, thr - 20.0, thr + 20.0, thr - 20.0):
            self.pose(aid, float(x), 30.0)
        quiet = self.c.drain(0.6)
        self.assertFalse([m for m in quiet if m["type"].startswith("far_")], quiet[:3])

    def test_imagery_failure_falls_back_to_palette(self):
        class Broken(FakeImagery):
            def render(self, *a, **kw):
                raise imagery.ImageryError("offline")
        self.server.imagery = Broken(os.path.join(self.tmp.name, "cache", "broken"))
        far = FarState()
        self.c.send({"type": "hello", "protocol": 4, "format": self.MOD_FORMAT, "modVersion": "0.4.0"})
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0, 0)
        _, seen = self.stream_until_done(far, aid)
        tiles = [m for m in seen if m["type"] == "far_tile"]
        self.assertTrue(tiles)
        self.assertFalse([m for m in tiles if "material" in m or "texture" in m or "tex" in m])
        shape = cdae.read(self.fs(tiles[0]["shape"]))
        self.assertEqual(shape["materials"][0]["name"], ff.MATERIAL)
        self.assertEqual(shape["meshes"][0]["tverts2"][0], 0)


class MeshServer64Test(MeshServerTest):
    """The same with --cells 64 (docs/V1_4_SPEC.md §3.1, M1): FORMAT 4, hello.cells, far_tile.tex,
    tiles down to L-1 (the physics lattice), 8704 tris, 512 px from L2."""
    cells = 64

    def test_old_mod_format_mismatch(self):
        """A v1.3.1 mod (format 3) cannot read 64-cell tiles: the server says so."""
        self.c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "0.4.0"})
        m, _ = self.c.until(lambda m: m["type"] == "status", timeout=10)
        self.assertIn("format mismatch: mod 3, server 4", m["text"])
        h, _ = self.c.until(lambda m: m["type"] == "hello", timeout=10)
        self.assertEqual((h["format"], h["cells"]), (4, 64))


class MeshServer64Block512Test(MeshServer64Test):
    """The same with --block 512 (docs/V1_4_SPEC.md §3.5, M4): hello.blockSize, a 512² physics block
    on a 128 m grid, and the prefetch predicting the mod's 512 follow table (with its fast mode)."""
    block = 512

    def test_block_512_terrain_and_prefetch(self):
        far, aid, _, _, _ = self.start()
        c, conn = self.c, self.conn()
        self.assertEqual(conn.block, ff.Block(0.0, 0.0, 512))
        # a request snaps to the 128 m grid; the heightmap is 512² and shares the lattice
        c.send({"type": "prepare_terrain", "requestId": 7, "anchorId": aid, "cx": 130.0, "cy": -190.0})
        tr, _ = c.until(lambda m: m["type"] == "terrain_ready", timeout=60)
        self.assertEqual((tr["cx"], tr["cy"], tr["size"]), (128.0, -128.0, 512))
        h = self.check_terrain({k: tr[k] for k in ("cx", "cy", "heightmap", "size", "squareSize", "zBase",
                                                   "maxHeight", "q", "ter")}, 128.0, -128.0)
        self.assertEqual(h.shape, (512, 512))
        self.assertEqual(conn.pending_block, ff.Block(128.0, -128.0, 512))
        self.assertEqual(conn.pending_block.bbox, (-384.0, -640.0, 640.0, 384.0))
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 128.0, "cy": -128.0})
        t0 = time.monotonic()
        while conn.block != ff.Block(128.0, -128.0, 512) and time.monotonic() - t0 < 10:
            time.sleep(0.01)
        self.assertEqual((conn.block, conn.pending_block), (ff.Block(128.0, -128.0, 512), None))
        rule = earthd.FOLLOW_RULES[512]
        # normal mode: the prefetch starts 100 m out and predicts the mod's 512 target
        for (x, y, vx, vy) in ((228.0 + 0.5, -128.0, 30.0, 0.0), (128.0, -10.0, 0.0, 90.0)):
            self.pose(aid, x, y, vx, vy)
            want = ff.Block(*earthd.predict_request(x, y, vx, vy, 128.0, -128.0, rule), 512)
            end = time.monotonic() + 10
            while conn.predicted_block != want and time.monotonic() < end:
                time.sleep(0.01)
            self.assertEqual(conn.predicted_block, want, (x, y, vx, vy))
            self.assertEqual((want.cx % 128, want.cy % 128), (0.0, 0.0))
        # below the prefetch start: nothing predicted
        self.pose(aid, 200.0, -128.0, 30.0, 0.0)
        end = time.monotonic() + 10
        while conn.predicted_block is not None and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIsNone(conn.predicted_block)
        # fast mode (200 m/s): the mod tests a point 0.75 s ahead, so the prefetch starts at the centre
        self.pose(aid, 128.0, -128.0, 200.0, 0.0)
        want = ff.Block(512.0, -128.0, 512)                      # 128 + 600 -> 768, within 420 m: 512
        end = time.monotonic() + 10
        while conn.predicted_block != want and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(conn.predicted_block, want)


class MeshServer64HorizonTest(ServerHarness):
    """--horizon (docs/V1_4_SPEC.md §3.6, M1h) with 64 cells and imagery: hello.horizon; the 80 km disc
    until the anchor's zmax grid is built, then the disc to the line-of-sight horizon (the synthetic
    ground is 1200-1800 m everywhere: ~170 km from a 3 m eye) tiled exactly once with the cull; every
    status carries the radius; a climb to 10 km builds a bigger grid, re-sends the radius and brings
    L10 tiles with 128 px textures; a rebase gets its own grid and keeps the eye."""
    near_visual = "mesh"
    with_imagery = True
    cells = 64
    horizon = True

    def eye_pose(self, aid, x, y, cam_z):
        self.c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": cam_z - 2.0, "lat": LAT, "lon": LON,
                     "speed": 0.0, "vx": 0.0, "vy": 0.0,
                     "cam": {"x": x, "y": y - 8, "z": cam_z, "fx": 0, "fy": 1, "fz": 0, "fovDeg": 60}})

    def settle(self, far, aid, want_radius, timeout=180):
        """Stream until the connection's selection has the wanted radius and the mod shows it all."""
        conn = self.conn()
        end = time.monotonic() + timeout
        seen = []
        while time.monotonic() < end:
            m, before = self.c.until(lambda m: m["type"] in ("far_done", "status"), timeout=max(1.0, end - time.monotonic()))
            seen += before + [m]
            far.apply(before + [m])
            if (m["type"] == "far_done" and m["anchorId"] == aid and want_radius(conn.radius)
                    and far.keys(aid) == set(conn.desired)):
                return seen
        self.fail(f"no far_done with the wanted radius (radius {conn.radius})")

    def check_selection(self, far, aid, px, py):
        import test_horizon
        conn, grid = self.conn(), self.server.horizon_grids[aid]
        radius, _ = grid.radius(px, py, conn.eye)
        self.assertEqual(conn.radius, radius)
        cull = ff.Horizon(conn.eye, grid)
        want, refined = ff.select(px, py, (), frozenset(), min_level=self.layout.min_level, layout=self.layout,
                                  radius=radius, max_level=ff.HORIZON_MAX_LEVEL, disc=True, horizon=cull)
        self.assertEqual(far.keys(aid), set(want))
        test_horizon.check_partition(self, want, refined, px, py, radius, cull)
        return want

    def test_horizon_session(self):
        c, far = self.c, FarState()
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": aid})
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.eye_pose(aid, 0.0, 0.0, 3.0)
        seen = self.settle(far, aid, lambda r: r > ff.HORIZON_MIN)
        conn = self.conn()
        self.assertAlmostEqual(conn.eye, 3.0, delta=0.01)
        grid = self.server.horizon_grids[aid]
        self.assertEqual(grid.half, ff.horizon_extent(3.0, earthd.ANCHOR_CENTRE_MAX))
        self.assertTrue(1500.0 <= conn.zmax_region <= 1800.0, conn.zmax_region)
        self.assertAlmostEqual(conn.radius, ff.horizon_distance(conn.eye) + ff.horizon_distance(conn.zmax_region))
        want = self.check_selection(far, aid, 0.0, 0.0)
        today, _ = ff.select(0.0, 0.0, [], min_level=self.layout.min_level, layout=self.layout)
        near = {k for k, t in today.items() if t.distance(0.0, 0.0) < ff.HORIZON_MIN}
        self.assertLessEqual(near, set(want))                        # the near field is today's
        self.assertGreater(len(want), len(today))
        self.check_tile_msgs(seen, aid)
        self.check_swaps(seen, aid)
        status = [m for m in seen if m["type"] == "status"]
        self.assertTrue(status and all("radius" in m for m in status))
        self.assertEqual(status[-1]["radius"], round(conn.radius))
        print(f"\n  horizon session: eye {conn.eye:.0f} m, radius {conn.radius / 1000:.0f} km, {len(want)} tiles "
              f"({len(today)} today)", file=sys.stderr)
        # a climb to 10 km: a bigger grid, a larger radius re-sent, L10 tiles (128 px)
        r0 = conn.radius
        self.eye_pose(aid, 0.0, 0.0, 10000.0)
        seen = self.settle(far, aid, lambda r: r > 1.5 * r0)
        self.assertAlmostEqual(conn.eye, 10000.0, delta=0.01)
        self.assertGreater(self.server.horizon_grids[aid].half, grid.half)
        want = self.check_selection(far, aid, 0.0, 0.0)
        self.assertTrue([m for m in seen if m["type"] == "status" and m["radius"] == round(conn.radius)])
        l10 = [m for m in seen if m["type"] == "far_tile" and m["level"] == ff.HORIZON_MAX_LEVEL]
        self.assertTrue(l10)
        self.assertTrue(all(m["tex"] == 128 for m in l10))
        self.assertTrue(all(m["tex"] == 256 for m in seen if m["type"] == "far_tile" and m["level"] in (8, 9)))
        self.check_tile_msgs(seen, aid)
        self.check_swaps(seen, aid)
        print(f"  at 10 km: radius {conn.radius / 1000:.0f} km, {len(want)} tiles", file=sys.stderr)
        # a rebase 2 km east: the new anchor gets its own grid, the eye stays
        lat, lon, _ = self.server.anchors[aid].frame.enu_to_geodetic(2000.0, 0.0, 0.0)
        b = self.prepare_anchor(2, float(lat), float(lon), cx=0, cy=0)["anchorId"]
        c.send({"type": "commit_anchor", "anchorId": b})
        c.send({"type": "commit_terrain", "anchorId": b, "cx": 0, "cy": 0})
        self.eye_pose(b, 0.0, 0.0, 10000.0)
        self.settle(far, b, lambda r: r > 1.5 * r0)
        self.assertIn(b, self.server.horizon_grids)
        self.assertAlmostEqual(conn.eye, 10000.0, delta=0.5)
        self.check_selection(far, b, 0.0, 0.0)


class LayoutServerTest(unittest.TestCase):
    """Server construction per layout; the default is exactly v1.3.1's (docs/V1_4_SPEC.md §3.1:
    --cells stays 32 until the in-game gate P8 passes)."""

    def make(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, **kw)
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        return s

    def test_default_is_v131(self):
        self.assertEqual(earthd.CELLS_DEFAULT, 32)
        s = self.make()
        self.assertEqual(s.layout, ff.V131)
        self.assertEqual((s.layout.format, s.min_level, s.near_radii, s.tile_cache), (3, -2, {-2: 150.0, -1: 400.0}, 6000))
        self.assertEqual([s.tex_size(lvl) for lvl in range(-2, 3)], [256] * 5)
        self.assertIs(s.kfun.__self__, s.layout)                        # the layout's K table
        t = self.make(near_visual="terrain")
        self.assertEqual((t.layout.cells, t.min_level, t.near_radii), (32, 0, {}))
        self.assertEqual([t.tex_size(lvl) for lvl in (0, 1, 2)], [256] * 3)
        a = s.new_anchor(LAT, LON)
        self.assertEqual([a.zooms[lvl] for lvl in range(1, 4)], [13, 12, 11])

    def test_cells_64(self):
        s = self.make(cells=64)
        self.assertEqual((s.layout.cells, s.layout.format, s.min_level, s.tile_cache), (64, 4, -1, 2500))
        self.assertEqual(s.near_radii, {-1: 150.0, 0: 400.0})
        self.assertEqual([s.tex_size(lvl) for lvl in range(-1, 4)], [256, 256, 256, 512, 512])
        self.assertEqual([s.kfun(lvl) for lvl in range(1, 6)], [0.75, 1.5, 1.5, 1.25, 1.25])
        a = s.new_anchor(LAT, LON)
        self.assertEqual([a.zooms[lvl] for lvl in range(1, 4)], [14, 13, 12])
        # a second server with the other layout in the same process changes nothing here
        b = self.make().new_anchor(LAT, LON)
        self.assertEqual([b.zooms[lvl] for lvl in range(1, 4)], [13, 12, 11])
        self.assertEqual([s.new_anchor(LAT, LON).zooms[lvl] for lvl in range(1, 4)], [14, 13, 12])
        self.assertEqual([self.make(cells=64, tex=(128, 256)).tex_size(lvl) for lvl in (-1, 0, 4)], [128, 256, 256])
        with self.assertRaises(ValueError):
            self.make(cells=64, near_visual="terrain")              # terrain mode is frozen at 32
        with self.assertRaises(ValueError):
            self.make(cells=48)

    def test_block_512(self):
        """M4 (docs/V1_4_SPEC.md §3.5): --block 512 (mesh mode only) with either cell count; the
        default stays 1024 (BLOCK_DEFAULT) until the in-game probes pass."""
        self.assertEqual(earthd.BLOCK_DEFAULT, 1024)
        self.assertEqual((self.make().layout.block, self.make().follow), (1024, earthd.FOLLOW_RULES[1024]))
        for cells in (32, 64):
            s = self.make(cells=cells, block=512)
            self.assertEqual((s.layout.block, s.layout.grid, s.follow), (512, 128.0, earthd.FOLLOW_RULES[512]))
            self.assertEqual((s.snap(63.9), s.snap(64.0), s.snap(-64.1)), (0.0, 128.0, -128.0))
            self.assertEqual(s.layout.block_at(128.0, 0.0).bbox, (-384.0, -512.0, 640.0, 512.0))
        self.assertEqual(self.make().snap(64.0), 0.0)                   # 1024: the 256 m grid
        with self.assertRaises(ValueError):
            self.make(block=512, near_visual="terrain")             # terrain mode is frozen at 1024
        with self.assertRaises(ValueError):
            self.make(block=2048)


class SessionCleanupTest(unittest.TestCase):
    """A stale session (marker untouched for SESSION_STALE_S) goes at start; a younger one waits for a
    game to claim it (test_restart.SessionAdoptionTest)."""

    def test_old_sessions_and_engine_caches_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "temp", "beamngEarth", "20200101-000000-abcd")
            cache = os.path.join(tmp, *earthd.CDAE_ROOT, "20200101-000000-abcd")
            foreign = os.path.join(tmp, *earthd.CDAE_ROOT, "someone-else")
            for d in (old, cache, foreign):
                os.makedirs(d)
            marker = os.path.join(old, earthd.SESSION_MARKER)
            open(marker, "w").close()
            stale = time.time() - earthd.SESSION_STALE_S - 60
            os.utime(marker, (stale, stale))
            open(os.path.join(cache, "far_1_0_0_0.cdae"), "w").close()
            server = earthd.Server(tmp, os.path.join(tmp, "cache"))
            try:
                self.assertFalse(os.path.exists(old))
                self.assertFalse(os.path.exists(cache))
                self.assertTrue(os.path.exists(foreign))
                self.assertEqual(server.cdae_dir, os.path.join(tmp, "temp", "temp", "beamngearth", server.sid.lower()))
            finally:
                server.tile_pool.shutdown()
                server.hm_pool.shutdown()


class DeleteQueueTest(unittest.TestCase):
    """Rate-limited file deletes (P9: a burst of loaded files stalls the game): one file at a time,
    5 files/s in terrain mode whatever the backlog (the v1.2 rate); in mesh mode a long queue drains
    faster (the mode writes ~5x as many files per km), up to DELETE_MAX_RATE; an evicted tile that
    is built again before its queued delete keeps its new file."""

    def make(self, mode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), near_visual=mode, imagery=None)
        self.addCleanup(server.tile_pool.shutdown)
        self.addCleanup(server.hm_pool.shutdown)
        return server

    def test_gap(self):
        terrain, mesh = self.make("terrain"), self.make("mesh")
        for n in (0, 10, 300, 1200, 5000, 100000):
            for srv in (terrain, mesh):
                srv.delete_queue = earthd.deque([("x", None)] * n)
            self.assertEqual(terrain.delete_gap(), earthd.DELETE_INTERVAL, n)
            want = max(1 / earthd.DELETE_MAX_RATE, min(earthd.DELETE_INTERVAL, earthd.DELETE_DRAIN_S / max(n, 1)))
            self.assertAlmostEqual(mesh.delete_gap(), want, msg=n)
        self.assertAlmostEqual(mesh.delete_gap(), 1 / earthd.DELETE_MAX_RATE)        # capped

    def test_rate_and_rebuilt_tile(self):
        mesh = self.make("mesh")
        paths = []
        for k in range(3000):                               # backlog / DRAIN_S > the cap
            paths.append(os.path.join(mesh.out_dir, f"f{k}"))
            open(paths[-1], "w").close()
        kept = os.path.join(mesh.out_dir, "far_1_0_0_0.cdae")
        open(kept, "w").close()
        times = []
        remove = mesh._remove

        def timed(p):
            times.append(time.monotonic())
            remove(p)
        mesh._remove = timed

        async def run():
            mesh.queue_delete([kept], (1, "0_0_0"))         # evicted, then built again (below)
            mesh.queue_delete(paths)
            mesh.tiles[(1, "0_0_0")] = object()
            await asyncio.sleep(2.0)
        asyncio.run(run())
        self.assertTrue(os.path.exists(kept))
        # ~40 files/s, evenly spaced (one per step): never more than a few in any 0.1 s
        self.assertGreater(len(times), 1.5 * earthd.DELETE_MAX_RATE)
        self.assertLess(len(times), 2.0 * earthd.DELETE_MAX_RATE + 5)
        dt = np.diff(times)
        self.assertGreater(float(np.median(dt)), 0.5 / earthd.DELETE_MAX_RATE)


class WarmPlaceTest(unittest.TestCase):
    def test_roads_first_nearest_first(self):
        # an anchor request fetches the map data of the road tiles around it at once, roads before buildings and water
        got = []

        class MD:
            def warm(self, needs, kind="demand"):
                got.extend(needs)
                return len(needs)
        s = SimpleNamespace(mapdata=MD(), sea=object(), buildings=True, road_widths=None,
                            WARM_ORDER=earthd.Server.WARM_ORDER, WARM_PLACE_LAYERS=earthd.Server.WARM_PLACE_LAYERS)
        anchor = SimpleNamespace(frame=geodesy.EnuFrame(LAT, LON))
        n = earthd.Server.warm_place(s, anchor, 0.0, 0.0)
        self.assertEqual(n, len(got))
        self.assertEqual(len(got), len(set(got)))
        layers = [ly for _, ly in got]
        self.assertEqual(layers, sorted(layers, key=lambda ly: earthd.Server.WARM_ORDER[ly]), "roads first")
        own = mapcells.cell_of(LAT, LON)
        self.assertEqual(got[0], (own, "roads"), "the anchor's own cell first")
        self.assertNotIn((own, "water"), got, "the tiles' own buildings and water: when each starts")
        self.assertIn((own, "infrastructure"), got)
        self.assertEqual(earthd.Server.warm_place(SimpleNamespace(mapdata=None), anchor, 0.0, 0.0), 0)


class FlagsTest(unittest.TestCase):
    def test_texture_and_radii_flags(self):
        import argparse
        self.assertEqual(earthd.parse_tex_px("512"), 512)
        self.assertEqual(earthd.parse_tex_px("16"), 16)
        for bad in ("300", "8", "4096", "0"):
            with self.assertRaises(argparse.ArgumentTypeError):
                earthd.parse_tex_px(bad)
        self.assertEqual(earthd.parse_radii("150,400"), (150.0, 400.0))
        with self.assertRaises(argparse.ArgumentTypeError):
            earthd.parse_radii("400,150")
        self.assertEqual(earthd.parse_tex_list("256,256,256,512"), (256, 256, 256, 512))
        self.assertEqual(earthd.parse_tex_list("128"), (128,))
        with self.assertRaises(argparse.ArgumentTypeError):
            earthd.parse_tex_list("256,300")
        # --cells 64 is mesh mode only (the argument check runs before anything starts)
        import subprocess
        r = subprocess.run([sys.executable, str(Path(earthd.__file__)), "--cells", "64", "--near-visual", "terrain"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--cells 64 needs --near-visual mesh", r.stderr)
        r = subprocess.run([sys.executable, str(Path(earthd.__file__)), "--block", "512", "--near-visual", "terrain"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--block 512 needs --near-visual mesh", r.stderr)
        r = subprocess.run([sys.executable, str(Path(earthd.__file__)), "--block", "2048"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


class AnchorQTest(unittest.TestCase):
    def test_survey_q(self):
        import geodesy

        class Dem:
            def __init__(self, relief):
                self.relief = relief

            def sample(self, lat, lon, zoom, progress=None):
                self.zoom = zoom
                return 1000 + self.relief * (np.asarray(lat) - LAT) / 0.27     # +-15 km = +-0.135 deg
        f = geodesy.EnuFrame(LAT, LON)
        d = Dem(10)
        self.assertEqual(earthd.survey_q(f, d), 1 / 256)
        self.assertEqual(d.zoom, earthd.SURVEY_ZOOM)
        # the relief inside block-sized windows decides, not the whole survey's: a steady slope of
        # 3000 m over 30 km is 200 m per window (5 samples, 2 km, for 1.5 blocks of 1024 m)
        self.assertEqual(earthd.survey_q(f, Dem(3000), 1024.0), 1 / 128)
        self.assertEqual(earthd.survey_q(f, Dem(9000), 1024.0), 1 / 64)      # 600 m per window
        self.assertEqual(earthd.survey_q(f, Dem(9000), 2048.0), 1 / 32)      # bigger blocks, bigger windows

        class Peak(Dem):            # flat, with one 1200 m spike: the window over it decides
            def sample(self, lat, lon, zoom, progress=None):
                la, lo = np.asarray(lat), np.asarray(lon)
                return 1000 + 1200 * np.exp(-(((la - LAT) * 111320) ** 2 + ((lo - LON) * 86000) ** 2) / 600.0 ** 2)
        self.assertEqual(earthd.survey_q(f, Peak(0), 1024.0), 1 / 32)


class PredictTest(unittest.TestCase):
    def test_predict_centre(self):
        p = earthd.predict_centre
        self.assertEqual(p(0, 0, 0, 0, 6), (0.0, 0.0))
        self.assertEqual(p(900, 0, 30, 0, 6), (1024.0, 0.0))
        # Clamped so the player stays within REACH = 420 m of the centre (§1.2).
        self.assertEqual(p(0, 0, 200, -200, 6), (256.0, -256.0))
        self.assertEqual(earthd.FOLLOW_REACH, 420.0)
        for px in (-1000.0, -3.0, 130.0, 777.0):
            for v in (-90.0, 0.0, 90.0):
                cx, cy = p(px, -px, v, v, 6)
                self.assertEqual(cx % 256, 0)
                self.assertLessEqual(abs(cx - px), 420)
                self.assertLessEqual(abs(cy + px), 420)

    def test_predict_request_matches_mod(self):
        """The prefetch predicts exactly what the mod's follow rule (follow.lua) asks for when
        driving straight: the target at the first frame past the 300 m box."""
        def mod_request(px, py, vx, vy, cx, cy, dt=0.05):
            while max(abs(px - cx), abs(py - cy)) <= 300:
                px, py = px + vx * dt, py + vy * dt
            return earthd.predict_centre(px, py, vx, vy, 4.0)
        for cx, cy in ((0.0, 0.0), (512.0, -256.0)):
            for vx, vy in ((30, 0), (21.2, 21.2), (-12, 40), (60, -5), (8, 0)):
                for t in (5.0, 7.0, 8.5, 9.6, 12.0):
                    px, py = cx + vx * t, cy + vy * t
                    if max(abs(px - cx), abs(py - cy)) < 200:
                        continue                    # the server prefetches from 200 m on
                    self.assertEqual(earthd.predict_request(px, py, vx, vy, cx, cy),
                                     mod_request(px, py, vx, vy, cx, cy), (cx, cy, vx, vy, t))

    def test_predict_request_matches_mod_512(self):
        """M4: the same against the mod's 512 rule (modsim's port of follow.decide, checked against
        follow.lua by test_modsim), normal and fast mode, from 100 m out (the 512 prefetch start;
        fast mode: the test point 0.75 s ahead)."""
        rule = earthd.FOLLOW_RULES[512]

        def mod_request(px, py, vx, vy, cx, cy, dt=0.05):
            while True:
                a = modsim.follow_decide(px, py, vx, vy, cx, cy, None, size=512)
                if a:
                    return a[1:]
                px, py = px + vx * dt, py + vy * dt
        n = 0
        for cx, cy in ((0.0, 0.0), (384.0, -128.0)):
            for vx, vy in ((30, 0), (21.2, 21.2), (-12, 40), (60, -5), (134, 0), (95, 95), (200, 0), (-180, 110)):
                ahead = rule.mode(vx, vy)[2]
                for t in (0.0, 0.3, 0.6, 1.0, 1.5, 2.5, 4.0):
                    px, py = cx + vx * t, cy + vy * t
                    if max(abs(px + vx * ahead - cx), abs(py + vy * ahead - cy)) < rule.prefetch_start:
                        continue
                    n += 1
                    self.assertEqual(earthd.predict_request(px, py, vx, vy, cx, cy, rule),
                                     mod_request(px, py, vx, vy, cx, cy), (cx, cy, vx, vy, t))
        self.assertGreater(n, 40)


if __name__ == "__main__":
    unittest.main()


class PerfLogTest(ServerHarness):
    """The mod's performance log (perflog.lua): its lines go into the server's log, cut to size."""

    def test_lines_logged(self):
        self.hello()
        with self.assertLogs("earthd", level="INFO") as cm:
            self.c.send({"type": "perf_log", "lines": ["perf hitch 412 ms at 39.28345,-76.59341, 52 mph",
                                                       "x" * 5000, 7, "two\nlines"] + ["more"] * 100})
            self.c.send({"type": "pose", "anchorId": 1, "x": 0, "y": 0, "z": 0})     # (after the perf_log)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and sum("game: " in r for r in cm.output) < 39:
                time.sleep(0.05)
        got = [r.split("game: ", 1)[1] for r in cm.output if "game: " in r]
        self.assertEqual(got[0], "perf hitch 412 ms at 39.28345,-76.59341, 52 mph")
        self.assertEqual(len(got[1]), 2000, "a line cut to PERF_LOG_CHARS")
        self.assertEqual(got[2], "two lines", "one log line each, not a number")
        self.assertEqual(len(got), 39, "the first PERF_LOG_LINES entries of a message, the number among them left out")


class LoadProgressTest(ServerHarness):
    """The loading line's counts (Connection.send_progress): [done, wanted] of the road, tree and far tiles after a pose."""

    def test_progress_after_a_pose(self):
        self.hello()
        aid = self.prepare_anchor(1, LAT, LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.pose(aid, 0.0, 0.0)
        m, _ = self.c.until(lambda m: m["type"] == "load_progress", timeout=30)
        self.assertEqual(m["anchorId"], aid)
        for k in ("ground", "roads", "trees", "far"):
            self.assertEqual(len(m[k]), 2)
            self.assertTrue(0 <= m[k][0] <= m[k][1], k)
        self.assertGreater(m["far"][1], 0, "far tiles wanted round the player")
