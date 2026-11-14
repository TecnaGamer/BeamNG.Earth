"""Live retexture (far_retex): when a colour-match (--colormatch) or de-shadow (--deshadow) region turns
ready, the tiles a mod shows (or holds, sent) that were rendered without it get their texture rendered
again under the next revision's file name and a far_retex {anchorId, key, texture}; nearest the
player first, at most RETEX_INFLIGHT at once on the tile pool and RETEX_RATE starts per second, never
on the event loop. Tiles no longer shown get nothing (they are re-rendered when sent again);
--colormatch off (and --deshadow off) sends nothing. Eviction deletes every revision's texture.

Scheduling is tested on a server with fake connections and a fake texture render; the session tests
run the in-process server (synthetic DEM and imagery, mesh mode, 64 cells) with a fake colour match
whose regions turn ready on demand, and check the files and messages the mod gets (no network)."""
import asyncio
import concurrent.futures
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402
import geodesy  # noqa: E402
import imagery  # noqa: E402
import netfetch  # noqa: E402
import test_server  # noqa: E402
from netclient import FarState  # noqa: E402

R, R2 = (701, 1612), (702, 1612)
RETEX_FIELDS = {"type", "anchorId", "key", "texture"}


class Conn:
    """What the retexture scheduler reads of a Connection, and the messages it gets."""

    def __init__(self, aid, focus=(0.0, 0.0)):
        self.active, self.focus = aid, focus
        self.view_focus = focus         # (Connection.view_focus: the car's when the camera is near it)
        self.shown, self.sent, self.msgs = {}, {}, []
        self.desired, self.inflight, self.retired, self.held = {}, {}, {}, set()    # Server.in_use reads these
        self.slot_holds = {}            # Server.slot_held
        self.credits = types.SimpleNamespace(poke=lambda c: None)

    def send(self, m):
        self.msgs.append(m)


class SchedulerTest(unittest.TestCase):
    """The scheduler on its own: Server bookkeeping, fake connections, a fake texture render that
    records when it ran and how many ran at once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s = self.s = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), imagery=None)
        s.imagery = types.SimpleNamespace(chain=[types.SimpleNamespace(name="eox", url="https://tiles.maps.eox.at")])
        self.a = earthd.Anchor(1, test_server.LAT, test_server.LON, geodesy.EnuFrame(test_server.LAT, test_server.LON))
        s.anchors[1] = self.a
        self.starts, self.peak, self.active, self.lock = [], [0], [0], threading.Lock()
        self.threads = set()

        def fake_make_tile(anchor, tile, prev=None):
            with self.lock:
                self.active[0] += 1
                self.peak[0] = max(self.peak[0], self.active[0])
                self.starts.append((time.monotonic(), tile.key))
                self.threads.add(threading.current_thread().name)
            time.sleep(0.02)
            with self.lock:
                self.active[0] -= 1
            return replace(prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
                           stale=False, cm_pending=frozenset())
        s.make_tile = fake_make_tile
        self.conn = Conn(1)
        s.connections.add(self.conn)

    def tearDown(self):
        self.s.tile_pool.shutdown(wait=True)
        self.s.hm_pool.shutdown(wait=True)
        self.tmp.cleanup()

    def add(self, key, regions=(R,), shown=True, sent=False, rev=0):
        ck = (1, key)
        self.s.tiles[ck] = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, self.s.matslots.take(ck),
                                           self.s.texture_name(1, key, rev), cm_pending=frozenset(regions), rev=rev)
        for rk in regions:
            self.s.cm_waiting.setdefault(rk, set()).add(ck)
        if shown:
            self.conn.shown[ck] = ff.tile_of(key).bbox
        if sent:
            self.conn.sent[key] = ff.tile_of(key)
        return ck

    def run_loop(self, body, settle=3.0):
        """Run body(loop) on a fresh event loop, then until the scheduler is idle (or `settle` s)."""
        async def main():
            self.s.loop = asyncio.get_running_loop()
            await body()
            end = time.monotonic() + settle
            while time.monotonic() < end:
                await asyncio.sleep(0.01)
                if not self.s.retex_todo and self.s.retex_busy == 0 and self.s._retex_timer is None:
                    break
            await asyncio.sleep(0.05)
        asyncio.run(main())

    def test_ready_region_retextures_exactly_the_shown_tiles_nearest_first(self):
        s = self.s
        s.retex_rate = 20.0
        # string order differs from distance order (the first settled must not simply go first)
        near = [self.add("0_1_0"), self.add("0_3_0"), self.add("0_10_0")]     # 256, 768, 2304 m from (0, 0)
        held = self.add("0_2_0", shown=False, sent=True)                      # sent, not swapped in: held
        gone = self.add("0_4_0", shown=False)                                 # no longer on screen
        other = self.add("0_0_0", regions=(R2,))                             # waits for another region

        async def body():
            s._cm_ready(R, True)
        self.run_loop(body)
        got = [m["key"] for m in self.conn.msgs]
        self.assertEqual(got, ["0_1_0", "0_2_0", "0_3_0", "0_10_0"])         # nearest first, held included
        for m in self.conn.msgs:
            self.assertEqual(set(m), RETEX_FIELDS)
            self.assertEqual((m["type"], m["anchorId"]), ("far_retex", 1))
            self.assertEqual(m["texture"], f"{s.vfs_dir}/far_1_{m['key']}_r1.dds")     # a NEW file name
        for ck in near + [held]:
            self.assertEqual((s.tiles[ck].rev, s.tiles[ck].stale, s.tiles[ck].texture), (1, False, f"far_1_{ck[1]}_r1.dds"))
        # the tile nobody shows: stale (re-rendered when it is sent again), no render, no message
        self.assertEqual((s.tiles[gone].stale, s.tiles[gone].rev), (True, 0))
        self.assertNotIn("0_4_0", [k for _, k in self.starts])
        self.assertEqual(s.retex_stats["dropped"], 1)
        # the tile of another region: untouched
        self.assertEqual((s.tiles[other].stale, s.tiles[other].cm_pending), (False, frozenset({R2})))
        # bounds: one at a time, starts at least 1 / rate apart, on the tile pool (never the loop)
        self.assertEqual(self.peak[0], 1)
        t = [a for a, _ in self.starts]
        self.assertTrue(all(b - a >= 1 / s.retex_rate - 0.005 for a, b in zip(t, t[1:])), t)
        self.assertTrue(all(n.startswith("far") for n in self.threads), self.threads)
        self.assertEqual((s.retex_stats["started"], s.retex_stats["sent"]), (4, 4))

    def test_rate_bound_over_many_tiles(self):
        s = self.s
        s.retex_rate = 10.0
        for i in range(12):
            self.add(f"0_{i}_5")

        async def body():
            s._cm_ready(R, True)
        t0 = time.monotonic()
        self.run_loop(body, settle=5.0)
        self.assertEqual(len(self.conn.msgs), 12)
        # 12 starts at <= 10/s take at least 1.1 s, one render at a time
        self.assertGreaterEqual(self.starts[-1][0] - self.starts[0][0], 11 / s.retex_rate - 0.02)
        self.assertGreaterEqual(time.monotonic() - t0, 1.1)
        self.assertEqual(self.peak[0], 1)

    def test_region_that_changes_nothing(self):
        ck = self.add("0_1_0")

        async def body():
            self.s._cm_ready(R, False)
        self.run_loop(body)
        self.assertEqual(self.conn.msgs, [])
        self.assertEqual((self.s.tiles[ck].stale, self.s.tiles[ck].cm_pending), (False, frozenset()))

    def test_tile_leaving_the_screen_before_its_turn(self):
        s = self.s
        s.retex_rate = 4.0
        a, b = self.add("0_1_0"), self.add("0_2_0")

        async def body():
            s._cm_ready(R, True)
            while not self.conn.msgs:
                await asyncio.sleep(0.005)
            del self.conn.shown[b]          # swapped out before its re-render started
        self.run_loop(body)
        self.assertEqual([m["key"] for m in self.conn.msgs], ["0_1_0"])
        self.assertTrue(s.tiles[b].stale)
        self.assertEqual(s.tiles[a].rev, 1)

    def test_deshadow_region(self):
        """A de-shadow region (DS_PENDING, rx, ry) goes the same way, through Deshadow's listener."""
        s = self.s
        rk = (imagery.DS_PENDING, 5, 6)
        ck = self.add("0_1_0", regions=(rk,))
        s.deshadow = types.SimpleNamespace(status=lambda r: {(5, 6): True}.get(r))
        self.assertIs(s._region_status(rk), True)
        self.assertIsNone(s._region_status(R))          # no colour match: never ready

        async def body():
            threading.Thread(target=s._ds_listener, args=((5, 6), True)).start()   # the worker thread
        self.run_loop(body)
        self.assertEqual([m["key"] for m in self.conn.msgs], ["0_1_0"])
        self.assertEqual(s.tiles[ck].rev, 1)

    def test_region_ready_while_rendering(self):
        """A tile done after its region turned ready (status known at once) is re-rendered live too."""
        s = self.s
        rk = (imagery.DS_PENDING, 5, 6)
        s.deshadow = types.SimpleNamespace(status=lambda r: True)
        ck = (1, "0_1_0")
        self.conn.shown[ck] = ff.tile_of("0_1_0").bbox
        info = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, "m", "far_1_0_1_0.dds", cm_pending=frozenset({rk}))

        async def body():
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(info)
            s._tile_done(ck, fut)
        self.run_loop(body)
        self.assertEqual([m["key"] for m in self.conn.msgs], ["0_1_0"])

    def test_texture_rendered_again_on_send_reaches_other_viewers(self):
        """tile() re-renders a stale tile when a connection sends it again; another connection that
        shows it gets the far_retex."""
        s = self.s
        ck = self.add("0_1_0")
        s.tiles[ck].stale = True
        info = replace(s.tiles[ck], texture="far_1_0_1_0_r1.dds", rev=1, stale=False)

        async def body():
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(info)
            s._tile_done(ck, fut)
        self.run_loop(body)
        self.assertEqual(self.conn.msgs, [{"type": "far_retex", "anchorId": 1, "key": "0_1_0",
                                           "texture": f"{s.vfs_dir}/far_1_0_1_0_r1.dds"}])

    def test_off(self):
        """--retex-rate 0: stale as before, nothing rendered or sent; no imagery: nothing either."""
        for setup in (lambda s: setattr(s, "retex_rate", 0.0), lambda s: setattr(s, "imagery", None)):
            s = self.s
            setup(s)
            ck = self.add("0_1_0")

            async def body():
                s._cm_ready(R, True)
            self.run_loop(body)
            self.assertTrue(s.tiles[ck].stale)
            self.assertEqual((self.conn.msgs, self.starts, s.retex_stats["started"]), ([], [], 0))
            s.retex_rate, s.imagery = earthd.RETEX_RATE, types.SimpleNamespace()

    def test_retex_repeat_guard(self):
        ck = self.add("0_1_0")
        self.s.tiles[ck].retex_repeat = earthd.RETEX_REPEAT

        async def body():
            self.s._cm_ready(R, True)
        self.run_loop(body)
        self.assertEqual(self.conn.msgs, [])
        self.assertTrue(self.s.tiles[ck].stale)

    def test_degraded_tile_retextures_and_clears_degraded_status(self):
        """A tile built without imagery is registered in s.degraded; when s.retex_degraded()
        runs, it is re-rendered live to _r1.dds, sends far_retex, and leaves s.degraded."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        async def body():
            s.retex_degraded()
        self.run_loop(body)

        self.assertEqual(len(self.conn.msgs), 1)
        m = self.conn.msgs[0]
        self.assertEqual(m["type"], "far_retex")
        self.assertEqual(m["key"], "0_1_0")
        self.assertEqual(m["texture"], f"{s.vfs_dir}/far_1_0_1_0_r1.dds")
        self.assertNotIn(ck, s.degraded)
        self.assertEqual(s.tiles[ck].rev, 1)
        self.assertFalse(s.tiles[ck].stale)

    def test_degraded_tile_recovering_stale_counts_from_zero(self):
        """A degraded tile past RETEX_REPEAT failures that gets a new, still-stale texture (a region pending) leaves
        degraded with its stale re-render count started over, not the old count + 1 (which dropped its next
        re-render: _retex_pick skips a non-degraded tile at RETEX_REPEAT)."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        before = s.tiles[ck]
        before.retex_repeat = earthd.RETEX_REPEAT + 2
        fut = concurrent.futures.Future()
        fut.set_result(replace(before, texture=s.texture_name(1, "0_1_0", 1), rev=1, stale=True))

        async def body():
            s._tile_done(ck, fut)
        self.run_loop(body, settle=0.2)
        self.assertNotIn(ck, s.degraded)
        self.assertEqual(s.tiles[ck].retex_repeat, 1)

    def test_degraded_tile_waits_while_imagery_hosts_unavailable(self):
        """When imagery chain hosts are open/unavailable, retex_degraded does not start retexture.
        Unrelated non-optional hosts (e.g. DEM) being down do NOT gate imagery recovery."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        # An unrelated host (e.g. DEM) is down, but imagery is up -> recovery proceeds!
        unavail = {"dem.example.com"}
        fake_net = types.SimpleNamespace(offline=False,
                                         available=lambda h: h not in unavail,
                                         chain_hosts=lambda n: ("tiles.maps.eox.at",))
        s.net = fake_net

        async def body():
            s.retex_degraded()
        self.run_loop(body)
        self.assertEqual(len(self.conn.msgs), 1)
        self.assertEqual(self.conn.msgs[0]["key"], "0_1_0")
        self.assertNotIn(ck, s.degraded)

        # Now when the imagery host IS down -> recovery waits!
        ck2 = self.add("0_2_0")
        s.degraded.add(ck2)
        s.tiles[ck2].stale = True
        unavail.add("tiles.maps.eox.at")

        async def body_down():
            s.retex_degraded()
        self.run_loop(body_down)
        self.assertEqual(len(self.conn.msgs), 1)  # no new message
        self.assertIn(ck2, s.degraded)

    def test_degraded_retexture_failure_backs_off(self):
        """When retexture fails, retex_repeat increments, backoff is recorded, and net_log
        does not spam retexture on every tick until the backoff expires or circuit closes."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        # Simulate failed retexture: returns prev with retex_repeat + 1
        def failing_make_tile(anchor, tile, prev=None):
            return replace(prev, retex_repeat=prev.retex_repeat + 1)
        s.make_tile = failing_make_tile

        async def body_fail():
            s.retex_degraded()
        self.run_loop(body_fail)

        self.assertEqual(self.conn.msgs, [])
        self.assertIn(ck, s.degraded)
        self.assertGreater(s.tiles[ck].retex_repeat, 0)
        self.assertIn(ck, s.degraded_retry)
        self.assertGreater(s.degraded_retry[ck], time.monotonic())

        # Immediate next tick is skipped due to backoff
        calls = []
        s.make_tile = lambda *a, **kw: calls.append(1)
        async def body_skipped():
            s.retex_degraded()
        self.run_loop(body_skipped)
        self.assertEqual(calls, [])

        # Circuit closed resets backoff and recovers tile
        s.make_tile = lambda anchor, tile, prev=None: replace(
            prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
            stale=False, cm_pending=frozenset()
        )
        async def body_circuit_closed():
            s._on_circuit_closed("tiles.maps.eox.at")
        self.run_loop(body_circuit_closed)
        self.assertEqual(len(self.conn.msgs), 1)
        self.assertNotIn(ck, s.degraded)
        self.assertNotIn(ck, s.degraded_retry)

    def test_degraded_three_failures_recover_on_circuit_closed(self):
        """A degraded tile that fails 3 consecutive retextures (reaching RETEX_REPEAT) is not dropped
        for good; it retries under its backoff, and when an imagery circuit closes, its repeat counter
        is reset and far_retex recovers the tile."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        def failing_make_tile(anchor, tile, prev=None):
            return replace(prev, retex_repeat=prev.retex_repeat + 1)
        s.make_tile = failing_make_tile

        # 3 failures in a row
        for i in range(3):
            s.degraded_retry[ck] = 0.0  # simulate time passing past backoff
            async def body_fail():
                s.retex_degraded()
            self.run_loop(body_fail)
            self.assertEqual(s.tiles[ck].retex_repeat, i + 1)

        self.assertGreaterEqual(s.tiles[ck].retex_repeat, earthd.RETEX_REPEAT)
        self.assertIn(ck, s.degraded)
        self.assertEqual(self.conn.msgs, [])

        # Even with retex_repeat >= RETEX_REPEAT, the degraded tile is not dropped by _retex_pick
        s.degraded_retry[ck] = 0.0
        picked = []
        def recording_make_tile(anchor, tile, prev=None):
            picked.append(tile.key)
            return failing_make_tile(anchor, tile, prev)
        s.make_tile = recording_make_tile
        async def body_pick():
            s.retex_degraded()
        self.run_loop(body_pick)
        self.assertEqual(picked, ["0_1_0"])

        # Circuit breaker closes: resets repeat counter and recovers tile
        s.make_tile = lambda anchor, tile, prev=None: replace(
            prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
            stale=False, cm_pending=frozenset()
        )
        async def body_circuit_closed():
            s._on_circuit_closed("tiles.maps.eox.at")
        self.run_loop(body_circuit_closed)
        self.assertEqual(len(self.conn.msgs), 1)
        self.assertEqual(self.conn.msgs[0]["type"], "far_retex")
        self.assertEqual(self.conn.msgs[0]["key"], "0_1_0")
        self.assertNotIn(ck, s.degraded)
        self.assertEqual(s.tiles[ck].retex_repeat, 0)

    def test_on_circuit_closed_triggers_retex_degraded(self):
        """When the net circuit breaker closes for an imagery host, s._on_circuit_closed schedules retex_degraded."""
        s = self.s
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        async def body():
            s._on_circuit_closed("tiles.maps.eox.at")
        self.run_loop(body)

        self.assertEqual(len(self.conn.msgs), 1)
        self.assertEqual(self.conn.msgs[0]["key"], "0_1_0")
        self.assertNotIn(ck, s.degraded)

    def test_temporary_full_outage_recovers_via_retex(self):
        """When all breakers are open during a full temporary outage (Fetcher.offline is True,
        but offline_flag is False), tiles receive fallback material and recover in-place via far_retex."""
        s = self.s
        fake_net = types.SimpleNamespace(offline=True, offline_flag=False,
                                         available=lambda h: False,
                                         chain_hosts=lambda n: ("tiles.maps.eox.at",))
        s.net = fake_net
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True

        # Breakers close -> hosts recover
        fake_net.offline = False
        fake_net.available = lambda h: True

        s.make_tile = lambda anchor, tile, prev=None: replace(
            prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
            stale=False, cm_pending=frozenset()
        )
        async def body():
            s._on_circuit_closed("tiles.maps.eox.at")
        self.run_loop(body)

        self.assertEqual(len(self.conn.msgs), 1)
        self.assertEqual(self.conn.msgs[0]["type"], "far_retex")
        self.assertEqual(self.conn.msgs[0]["key"], "0_1_0")
        self.assertNotIn(ck, s.degraded)

    def test_full_outage_recovers_once_the_breakers_are_due_a_probe(self):
        """Real breakers on a real Fetcher (fake clock): every imagery host's breaker open, nothing else asking those
        hosts. Once the open time is over, the periodic recovery re-renders the degraded tile (its request is the
        probe) instead of waiting for breakers that only a request can close."""
        import random
        from test_netfetch import FakeClock, FakeSleep
        clock = FakeClock()
        net = netfetch.Fetcher(clock=clock, sleep=FakeSleep(clock), rng=random.Random(7), transport=mock.Mock(),
                               wall=clock)
        s = self.s
        s.net = net
        hosts = s.imagery_hosts()
        self.assertTrue(hosts)
        for h in hosts:
            net.host(h).breaker.trip(60.0)
        ck = self.add("0_1_0")
        s.degraded.add(ck)
        s.tiles[ck].stale = True
        self.assertTrue(s.imagery_down())
        clock.advance(61.0)
        self.assertFalse(s.imagery_down())
        self.assertTrue(all(net.host(h).breaker.is_open() for h in hosts))    # (still open: nothing probed yet)

        async def body():
            s.retex_degraded()
        self.run_loop(body)
        self.assertEqual([m["type"] for m in self.conn.msgs], ["far_retex"])
        self.assertNotIn(ck, s.degraded)

    def test_palette_fallback_rgb_has_water_and_height_tint(self):
        """palette_fallback_rgb produces blue for water/sea and terrain tints for land/peaks."""
        grid = np.zeros((33, 33), dtype=np.float32)
        grid[:, :16] = -2.0  # sea
        grid[:, 16:] = 2500.0  # high mountain
        rgb = ff.palette_fallback_rgb(grid, 64)
        self.assertEqual(rgb.shape, (64, 64, 3))
        # Water: blue dominant (B > R)
        water_px = rgb[32, 8]
        self.assertGreater(water_px[2], water_px[0])
        # High mountain: rock/snow tint (R >= B)
        mountain_px = rgb[32, 48]
        self.assertGreaterEqual(mountain_px[0], mountain_px[2])

    def test_tile_waiting_for_many_regions_gets_every_one(self):
        """A coarse tile spans many regions (an L5 texture up to 3 x 3 z12 regions, an L6 one 4 x 4, as
        many de-shadow ones): it is re-rendered live each time one of them turns ready, however many,
        so none is left raw on screen (review: a cap of 8 revisions left L5/L6 tiles partly raw)."""
        s = self.s
        s.retex_rate = 200.0
        regions = [(700 + i, 1600) for i in range(12)]
        ready = set()
        s.colormatch = types.SimpleNamespace(status=lambda r: True if r in ready else None)
        renders = []

        def make_tile(anchor, tile, prev=None):
            renders.append(tile.key)
            return replace(prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
                           stale=False, cm_pending=frozenset(r for r in regions if r not in ready))
        s.make_tile = make_tile
        ck = self.add("0_1_0", regions=regions)

        async def body():
            for rk in regions:                  # one at a time, each after the previous re-render landed
                n = len(self.conn.msgs)
                ready.add(rk)
                s._cm_ready(rk, True)
                end = time.monotonic() + 2.0
                while len(self.conn.msgs) == n and time.monotonic() < end:
                    await asyncio.sleep(0.005)
        self.run_loop(body)
        self.assertEqual(len(renders), len(regions))
        self.assertEqual([m["texture"].rsplit("_", 1)[1] for m in self.conn.msgs],
                         [f"r{i}.dds" for i in range(1, len(regions) + 1)])
        self.assertEqual((s.tiles[ck].stale, s.tiles[ck].cm_pending, s.tiles[ck].rev), (False, frozenset(), 12))

    def test_render_still_pending_on_a_ready_region_does_not_loop(self):
        """A render that reports a region pending although the region's status says ready (a status
        that cannot be trusted, e.g. a failed region over an unusable cache file) is not re-rendered
        over and over: at most RETEX_REPEAT re-renders that come back still waiting, then the tile
        waits to be sent again (stale)."""
        s = self.s
        s.retex_rate = 200.0
        s.colormatch = types.SimpleNamespace(status=lambda r: True)
        renders = []

        def make_tile(anchor, tile, prev=None):
            renders.append(tile.key)
            return replace(prev, texture=s.texture_name(anchor.id, tile.key, prev.rev + 1), rev=prev.rev + 1,
                           stale=False, cm_pending=frozenset({R}))
        s.make_tile = make_tile
        ck = self.add("0_1_0")

        async def body():
            s._cm_ready(R, True)
        self.run_loop(body)
        self.assertGreaterEqual(len(renders), 1)
        self.assertLessEqual(len(renders), earthd.RETEX_REPEAT, renders)
        self.assertTrue(s.tiles[ck].stale)
        self.assertEqual(s.retex_todo, set())

    def test_failing_render_is_logged(self):
        """A live re-render that raises (a bug, a full disk: not the network, which retexture() turns
        into 'still stale') is logged once, the tile stays stale and the scheduler goes on; before,
        _tile_done read the exception and nobody ever saw it."""
        s = self.s

        def make_tile(anchor, tile, prev=None):
            raise OSError(28, "No space left on device")
        s.make_tile = make_tile
        ck = self.add("0_1_0")

        async def body():
            s._cm_ready(R, True)
        with self.assertLogs("earthd", "WARNING") as cm:
            self.run_loop(body)
        self.assertTrue(any("live retexture" in line and "No space left" in line for line in cm.output), cm.output)
        self.assertTrue(s.tiles[ck].stale)
        self.assertEqual((s.retex_busy, self.conn.msgs), (0, []))

    def test_prune_forgets_the_anchors_waiting_tiles(self):
        """A pruned anchor's tiles leave the regions' waiting lists too: after a teleport the regions
        beyond REQUEST_M of the new anchor are never computed, and their lists only grew."""
        s = self.s
        a, b = self.add("0_1_0", regions=(R, R2)), self.add("0_2_0")
        s.anchors[2] = earthd.Anchor(2, test_server.LAT, test_server.LON, self.a.frame)
        keep = (2, "0_1_0")
        s.cm_waiting.setdefault(R2, set()).add(keep)
        s.retex_todo.add(b)
        s.prune(2)
        self.assertNotIn(a, s.tiles)
        self.assertEqual(s.cm_waiting, {R2: {keep}})
        self.assertEqual(s.retex_todo, set())

    def test_previous_anchor_tiles_get_nothing(self):
        """A previous anchor's tiles still on screen after a rebase are on their way out."""
        s = self.s
        ck = self.add("0_1_0")
        self.conn.active = 2

        async def body():
            s._cm_ready(R, True)
        self.run_loop(body)
        self.assertEqual(self.conn.msgs, [])
        self.assertTrue(s.tiles[ck].stale)

    def test_eviction_deletes_every_revision(self):
        s = self.s
        ck = (1, "0_1_0")
        names = ["far_1_0_1_0.cdae", "far_1_0_1_0.dds", "far_1_0_1_0_r1.dds", "far_1_0_1_0_r2.dds"]
        for n in names:
            open(os.path.join(s.out_dir, n), "wb").close()
        s.tiles[ck] = earthd.TileInfo([0, 0, 0], 0.0, None, 0.0, None, 0, 0.0, "m", "far_1_0_1_0_r2.dds", rev=2)
        s._evict_tile(ck)                   # no running loop: deleted at once
        self.assertEqual([n for n in names if os.path.exists(os.path.join(s.out_dir, n))], [])
        self.assertEqual(s.tex_rev[ck], 3)


class RegionStatusTest(unittest.TestCase):
    """status() of a region a render reported pending (earthd's _tile_done asks it on the event loop).
    A region that failed (network; tried again later) is not ready, whatever its cache folder holds:
    a file there is one the render could not use (another version, mode or reference chain), else the
    region would not have been computed. Calling it ready made a shown tile re-render in a loop
    (the render reports it pending again) and dropped it from the region's waiting list."""

    def test_deshadow_failed_region_over_an_old_cache_file(self):
        import deshadow as ds
        with tempfile.TemporaryDirectory() as tmp:
            d = ds.Deshadow(tmp, None, "full", wait_s=0)
            try:
                rk = (701, 1612)
                jp, _ = d._paths(rk)
                os.makedirs(os.path.dirname(jp), exist_ok=True)
                with open(jp, "w") as f:
                    f.write('{"version": %d, "ok": true}' % (ds.VERSION - 1))
                self.assertIsNone(d._load_params(rk))           # unusable for a render
                d._failed[rk] = time.monotonic()
                self.assertIsNone(d.status(rk))
                d._params[rk] = {"version": ds.VERSION, "ok": False}
                self.assertIs(d.status(rk), False)              # ready (memory) wins
            finally:
                d.close()

    def test_colormatch_failed_region_over_a_stale_cache_file(self):
        import colormatch as cmmod
        with tempfile.TemporaryDirectory() as tmp:
            c = cmmod.ColorMatch(tmp, "s2")
            try:
                rk = (701, 1612)
                npz, none = c._paths(rk, "field")
                os.makedirs(os.path.dirname(npz), exist_ok=True)
                np.savez(npz, meta=np.array('{"version": -1}'))
                self.assertIsNone(c._load(rk, "field"))          # unusable for a render
                c._failed[rk] = time.monotonic()
                self.assertIsNone(c.status(rk))
                os.remove(npz)
                open(none, "wb").close()                        # a stale no-seam marker the same
                self.assertIsNone(c.status(rk))
            finally:
                c.close()


class WiringTest(unittest.TestCase):
    def test_listeners_registered(self):
        """--colormatch s2 and --deshadow full: the server hears both workers."""
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), imagery=("usgs",), deshadow="full", colormatch="s2")
            try:
                self.assertEqual(s.colormatch._listeners, [s._cm_listener])
                self.assertEqual(s.deshadow._listeners, [s._ds_listener])
                self.assertEqual(s.retex_rate, earthd.RETEX_RATE)
            finally:
                s.imagery.close()
                s.tile_pool.shutdown()
                s.hm_pool.shutdown()


class FakeColorMatch:
    """colormatch.ColorMatch's interface to Imagery and earthd, with regions (z12 USGS) that turn ready
    when the test says so (from a thread, like the worker). A ready region inverts its tiles."""
    PENDING = object()
    refs = ()
    mode = "s2"

    def __init__(self):
        self.ready, self.listeners, self.lock = set(), [], threading.Lock()

    def bind(self, imagery):
        pass

    def add_listener(self, fn):
        self.listeners.append(fn)

    def prepare(self, lat, lon):
        pass

    def request(self, key):
        pass

    def handles(self, key):
        src, z, _, _ = key
        return getattr(src, "name", src) == "usgs" and 12 <= z <= 16

    def region_of(self, z, x, y):
        return x >> (z - 12), y >> (z - 12)

    def status(self, rk):
        with self.lock:
            return True if rk in self.ready else None

    def apply(self, key, rgb, imagery):
        rk = self.region_of(*key[1:])
        if self.status(rk) is None:
            return self.PENDING, rk
        return 255 - rgb, True

    def make_ready(self, rk):
        with self.lock:
            self.ready.add(rk)

        def run():
            for fn in self.listeners:
                fn(rk, True)
        t = threading.Thread(target=run)
        t.start()
        t.join()

    def summary(self):
        return "fake colormatch"

    def close(self):
        pass


def install_colormatch(harness):
    """Swap the harness server's imagery for one with a FakeColorMatch (before the session)."""
    s = harness.server
    if s.imagery is not None:
        s.imagery.close()
    cm = FakeColorMatch()
    s.colormatch = cm
    cm.add_listener(s._cm_listener)
    s.imagery = test_server.FakeImagery(os.path.join(harness.tmp.name, "cache", "imagery-cm"), colormatch=cm)
    return cm


class MeshHarness(test_server.ServerHarness):
    """Mesh mode with synthetic imagery and 64 cells (the command line's layout), no tests of its own."""
    near_visual = "mesh"
    with_imagery = True
    cells = 64
    # FakeImagery's USGS has data east of LON only, and its coverage probes (z10) leave the z10 tile
    # around LON without: an anchor 0.3 deg further east has USGS (colour-matched) tiles all around
    LON = test_server.LON + 0.3

    def start(self):
        far = FarState()
        self.hello()
        aid = self.prepare_anchor(1, test_server.LAT, self.LON)["anchorId"]
        self.c.send({"type": "commit_anchor", "anchorId": aid})
        self.c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})
        self.pose(aid, 0.0, 0.0)
        done, seen = self.stream_until_done(far, aid)
        return far, aid, done, seen, None


class SessionTest(MeshHarness):
    """The in-process server, mesh mode, 64 cells, imagery with a fake colour match."""

    def setUp(self):
        super().setUp()
        self.cm = install_colormatch(self)
        self.server.retex_rate = 200.0
        # (one re-render at a time: the order checks below are of the starts, which with RETEX_INFLIGHT > 1 can end
        # out of order)
        self.server.retex_inflight = 1

    def pending_on(self, rk, aid):
        return {k for (a, k) in self.conn().shown if a == aid and rk in self.server.tiles[(a, k)].cm_pending}

    def collect_retex(self, far, n, timeout=60):
        """The next n far_retex messages (and whatever came with them), then a quiet 0.5 s."""
        seen, got = [], []
        end = time.monotonic() + timeout
        while len(got) < n:
            m = self.c.recv(max(0.1, end - time.monotonic()))
            seen.append(m)
            if m["type"] == "far_retex":
                got.append(m)
        seen += self.c.drain(0.5)
        far.apply(seen)
        return [m for m in seen if m["type"] == "far_retex"]

    def test_region_ready_retextures_the_shown_tiles_that_use_it(self):
        far, aid, done, seen, _ = self.start()
        s = self.server
        self.assertFalse([m for m in seen if m["type"] == "far_retex"])
        regions = {}
        for (a, k) in self.conn().shown:
            for rk in s.tiles[(a, k)].cm_pending:
                regions[rk] = regions.get(rk, 0) + 1
        self.assertTrue(regions, "no tile was rendered with a pending region")
        rk = max(regions, key=lambda r: (regions[r], r))
        # move 3 km east: the near tiles at the start are swapped out (their files stay cached)
        before = self.pending_on(rk, aid)
        self.pose(aid, 3000.0, 0.0)
        _, seen2 = self.stream_until_done(far, aid)
        self.assertFalse([m for m in seen2 if m["type"] == "far_retex"])
        want = self.pending_on(rk, aid)
        left = {k for k in before - want if (aid, k) in s.tiles}
        self.assertTrue(want and left, (len(want), len(left)))
        old = {k: s.tiles[(aid, k)].texture for k in want}
        t0 = time.monotonic()
        self.cm.make_ready(rk)
        msgs = self.collect_retex(far, len(want))
        dt = time.monotonic() - t0 - 0.5                                     # (the quiet 0.5 s after)
        ms = sorted(1000 * s.tiles[(aid, m["key"])].tex_seconds for m in msgs)
        print(f"\n  live retexture: {len(msgs)} tiles in {dt:.2f} s at --retex-rate {s.retex_rate:g}, one at a "
              f"time; texture render median {ms[len(ms) // 2]:.1f} ms, max {ms[-1]:.1f} ms (synthetic imagery)",
              file=sys.stderr)
        self.assertEqual(sorted(m["key"] for m in msgs), sorted(want))      # exactly those, once each
        for m in msgs:
            self.assertEqual(set(m), RETEX_FIELDS)
            self.assertEqual(m["anchorId"], aid)
            self.assertEqual(m["texture"], f"/temp/beamngEarth/{s.sid}/far_{aid}_{m['key']}_r1.dds")
            new, prev = self.fs(m["texture"]), os.path.join(s.out_dir, old[m["key"]])
            self.assertTrue(os.path.exists(new) and os.path.exists(prev))    # the old file stays (in use)
            with open(new, "rb") as f1, open(prev, "rb") as f0:
                a, b = f1.read(), f0.read()
            self.assertEqual(len(a), len(b))
            self.assertNotEqual(a, b)
            self.assertEqual(far.tiles[(aid, m["key"])]["texture"], m["texture"])
        # tiles no longer shown: nothing sent, stale (rendered again when sent again)
        for k in left:
            self.assertTrue(s.tiles[(aid, k)].stale)
            self.assertEqual(s.tiles[(aid, k)].rev, 0)
        # nearest first: the order of the messages follows the distance to the player
        d = [ff.tile_of(m["key"]).distance(3000.0, 0.0) for m in msgs]
        self.assertEqual(d, sorted(d))
        self.assertEqual(s.retex_stats["sent"], len(want))
        # the corrected texture is what a fresh render gives now
        k = msgs[0]["key"]
        t = ff.tile_of(k)
        px = s.tex_size(t.level)
        rgb, _ = s.imagery.render(s.anchors[aid].frame, *imagery.gutter_square(*t.bbox[:2], t.size, px), px)
        with open(self.fs(msgs[0]["texture"]), "rb") as f:
            self.assertEqual(f.read(), imagery.encode_dds(rgb))
        # back to the start: a tile swapped out earlier is sent again with its re-rendered texture
        self.pose(aid, 0.0, 0.0)
        _, seen3 = self.stream_until_done(far, aid)
        resent = {m["key"]: m for m in seen3 if m["type"] == "far_tile" and m["key"] in left}
        self.assertTrue(resent)
        for k, m in resent.items():
            self.assertEqual(m["texture"], f"/temp/beamngEarth/{s.sid}/far_{aid}_{k}_r1.dds")


class OffSessionTest(MeshHarness):
    """--colormatch off (and --deshadow off): no region is ever pending, nothing re-rendered or sent."""

    def test_session(self):
        s = self.server
        self.assertIsNone(s.colormatch)
        self.assertIsNone(s.deshadow)
        far, aid, done, seen, _ = self.start()
        self.pose(aid, 3000.0, 0.0)
        _, seen2 = self.stream_until_done(far, aid)
        seen += seen2 + self.c.drain(0.5)
        self.assertFalse([m for m in seen if m["type"] == "far_retex"])
        self.assertFalse([ck for ck, t in s.tiles.items() if t.cm_pending or t.stale or t.rev])
        self.assertEqual((s.retex_stats, s.retex_todo, s.cm_waiting),
                         ({"started": 0, "sent": 0, "dropped": 0}, set(), {}))
        self.assertFalse([f for f in os.listdir(s.out_dir) if "_r" in f and f.endswith(".dds")])
        self.assertTrue(np.all([m["texture"].endswith(f"far_{aid}_{m['key']}.dds")
                                for m in seen if m["type"] == "far_tile"]))


if __name__ == "__main__":
    unittest.main()
