"""A teleport's far field coarse first (earthd FIRST_RAMP, the user's goal 2026-11-11: the whole horizon within seconds
of a teleport to a place never seen, then refined): the first selection is the coarse cover (global tiles alone, K x
FIRST_RAMP_K, no forced block ring), built ahead while the first block is; the full one after it refines it (every full
tile inside one coarse tile, so each swap group is one coarse tile and the finer ones under it); and the cap on the far
tiles that wait for the map data (GRADED_SHARE: at a new place their road plans held every far worker for 15-25 s)."""
import asyncio
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import earthd  # noqa: E402
import farfield as ff  # noqa: E402

LAT, LON = 46.8625, -113.985


class _Writer:
    def write(self, data):
        pass

    def close(self):
        pass

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class MeshDriveSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_whole_upcoming_swap_precedes_nearer_side_tile(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = earthd.Server(tmp, os.path.join(tmp, "cache"), near_visual="mesh", cells=64, horizon=False)
            self.addCleanup(s.hm_pool.shutdown)
            self.addCleanup(s.tile_pool.shutdown)
            c = earthd.Connection(s, None, _Writer())
            a = s.new_anchor(LAT, LON)
            s.anchors[a.id] = a
            c.active, c.focus = a.id, (32.0, 96.0)
            c.update_far(gate=True)
            self.assertIn("0_1_0", c.desired)  # east edge in 224 m, outside the 150 m exact ring
            side = "-1_0_-1"
            self.assertIn(side, c.desired)
            c.shown = {c.ck(k): t.bbox for k, t in c.desired.items() if k != side}

            c.vel = (30.0, 0.0)
            c.update_far(gate=True)
            east = {f"-1_{i}_{j}" for i in (2, 3) for j in (0, 1)}
            self.assertEqual({t.key for t in c.queue[:4]}, east)
            self.assertIn(side, {t.key for t in c.queue[4:]})
            self.assertIn("0_-2_0", c.desired)

            # Changing direction at the same spot must invalidate the pose selection gate.
            c.vel = (-30.0, 0.0)
            c.update_far(gate=True)
            self.assertNotIn("0_-2_0", c.desired)
            self.assertTrue({f"-1_{i}_{j}" for i in (-4, -3) for j in (0, 1)} <= c.desired.keys())


class FarFirstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = earthd.Server(self.tmp.name, os.path.join(self.tmp.name, "cache"), imagery=None, cells=64, block=1024,
                               near_visual="detail", horizon=False)
        self.addCleanup(self.s.hm_pool.shutdown)
        self.addCleanup(self.s.tile_pool.shutdown)

    def selections(self):
        """(coarse, full) desired sets of a connection at a fresh anchor, the block at the origin."""
        async def go():
            c = earthd.Connection(self.s, None, _Writer())
            a = self.s.new_anchor(LAT, LON)
            a.first_terrain = (0.0, 0.0)
            self.s.anchors[a.id] = a
            c.active = a.id
            c.focus = (0.0, 0.0)
            c._set_block(a.id, 0.0, 0.0)
            c.first_ramp = time.monotonic()
            c.update_far()
            coarse = dict(c.desired)
            groups_before = c.groups
            c.first_ramp = None
            c.shown = {c.ck(k): t.bbox for k, t in coarse.items()}     # (the coarse cover swapped in)
            c.sent = {}
            c.update_far()
            return c, coarse, dict(c.desired), groups_before, c.groups
        return asyncio.run(go())

    def _drive(self, gate, steps, global_far=False):
        """A connection's far selection along steps [(x, y)] (update_far(gate) at each, a pose's), its desired keys
        and refined nodes after each; the number of selections skipped."""
        from unittest import mock

        class Clock:            # (earthd's time with a fake monotonic clock: the holds expire alike in both drives)
            def __init__(self, real):
                self.real, self.now = real, 1000.0

            def monotonic(self):
                return self.now

            def __getattr__(self, name):
                return getattr(self.real, name)
        clock = Clock(earthd.time)

        async def go():
            c = earthd.Connection(self.s, None, _Writer())
            c.global_far = global_far
            if global_far:      # (a disc, the default horizon mode's: in square mode the global part reports no slack)
                c.view_cap = 80000.0
            a = self.s.new_anchor(LAT, LON)
            a.first_terrain = (0.0, 0.0)
            self.s.anchors[a.id] = a
            c.active = a.id
            c.focus = steps[0]
            c._set_block(a.id, 0.0, 0.0)
            c.update_far()
            skipped0 = c.far.skipped        # (the selections the gate skipped: earthfast FarState)
            out = []
            for k, p in enumerate(steps):
                clock.now += 0.5
                c.focus = p
                c.update_far(gate=gate)
                out.append((sorted(c.desired), sorted(c.refined, key=str), [t.key for t in c.queue[:32]]))
            return out, c.far.skipped - skipped0
        with mock.patch.object(earthd, "time", clock):
            return asyncio.run(go())

    def test_pose_gate_never_changes_the_selection(self):
        """update_far(gate=True) (a pose) skips a selection only when it surely is the last one's (the last changed no
        node, nothing discrete changed, the move under the last selection's slack): along drives at 0, 2, 5, 20 and 60 m/s
        (2 Hz poses, the mod's POSE_INTERVAL) the desired tiles, the refined nodes and the build order after every pose
        are those of selecting at every pose, and it does skip (standing, creeping)."""
        import math
        total = {}
        for global_far in (False, True):
            for speed in (0.0, 2.0, 5.0, 20.0, 60.0):
                steps = [(137.3 + speed * 0.5 * k * math.cos(0.4), -61.9 + speed * 0.5 * k * math.sin(0.4))
                         for k in range(40)]
                want, n0 = self._drive(False, steps, global_far)
                got, skipped = self._drive(True, steps, global_far)
                self.assertEqual(n0, 0)
                for k, (w, g) in enumerate(zip(want, got)):
                    self.assertEqual(w, g, f"global {global_far}, {speed} m/s, pose {k}")
                total[(global_far, speed)] = skipped
        print(f"\n  pose gate: selections skipped of 40 per drive (global far, m/s): {total}")
        self.assertGreater(total[(False, 0.0)], 30, total)
        self.assertGreater(total[(True, 0.0)], 30, total)

    def test_coarse_cover_then_refined(self):
        c, coarse, full, _, groups = self.selections()
        self.assertGreater(min(t.level for t in full.values()), self.s.min_level - 1)
        self.assertEqual(min(t.level for t in full.values()), self.s.min_level, "the full one goes to the exact level")
        self.assertGreaterEqual(min(t.level for t in coarse.values()), earthd.FIRST_RAMP_MIN_LEVEL,
                                "no graded level in the coarse cover")
        self.assertLess(len(coarse), len(full) / 2)
        area = sum((t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]) for t in coarse.values())
        full_area = sum((t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]) for t in full.values())
        self.assertGreaterEqual(area, full_area, "the ground covered (coarse tiles across the radius reach past it)")
        # every full tile lies in one coarse tile: the refinement's swap groups are each a coarse tile and the
        # tiles under it, none waits for another coarse tile's
        for t in full.values():
            inside = [k for k, u in coarse.items() if ff.overlaps(t.bbox, u.bbox)]
            self.assertEqual(len(inside), 1, t.key)
            u = coarse[inside[0]]
            self.assertTrue(u.bbox[0] <= t.bbox[0] and t.bbox[2] <= u.bbox[2] and u.bbox[1] <= t.bbox[1]
                            and t.bbox[3] <= u.bbox[3], (t.key, u.key))
        for adds, removes in groups:
            self.assertLessEqual(len(removes), 1, (adds, removes))

    def test_off_switch(self):
        """FIRST_RAMP off: a teleport's commit selects the full far field at once."""
        old = earthd.FIRST_RAMP
        earthd.FIRST_RAMP = False
        try:
            async def go():
                c = earthd.Connection(self.s, None, _Writer())
                c.global_far = True
                a = self.s.new_anchor(LAT, LON)
                a.first_terrain = (0.0, 0.0)
                a.ramp = True
                self.s.anchors[a.id] = a
                c.commit_anchor(a.id)
                return c
            c = asyncio.run(go())
        finally:
            earthd.FIRST_RAMP = old
        self.assertIsNone(c.first_ramp)
        self.assertEqual(min(t.level for t in c.desired.values()), self.s.min_level)

    def test_teleport_commit_starts_it_once(self):
        async def go():
            out = []
            for glob in (False, True):
                c = earthd.Connection(self.s, None, _Writer())
                c.global_far = glob
                a = self.s.new_anchor(LAT, LON)
                a.first_terrain = (0.0, 0.0)
                a.ramp = True
                self.s.anchors[a.id] = a
                c.commit_anchor(a.id)
                out.append((c, a))
            return out
        (c2, _), (c, a) = asyncio.run(go())
        self.assertIsNotNone(c.first_ramp)
        self.assertFalse(a.ramp, "once per anchor")
        self.assertTrue(all(ff.is_global_key(k) for k in c.desired))
        self.assertTrue(self.s.coarse_keys >= set(c.desired), "their textures FIRST_RAMP_TEX_PX")
        for t in c.desired.values():
            self.assertLessEqual(self.s.tile_tex_px(a, t), earthd.FIRST_RAMP_NEAR_PX if t.level <= 3
                                 else earthd.FIRST_RAMP_TEX_PX)
        self.s.coarse_keys.clear()
        t = min(c.desired.values(), key=lambda u: u.level)
        self.assertGreater(self.s.tile_tex_px(a, t), earthd.FIRST_RAMP_TEX_PX, "its own size after the coarse step")
        self.assertIsNone(c2.first_ramp, "without global far tiles (an older mod): as before")

    def test_global_coarse_cover_built_ahead(self):
        """With global far tiles (hello.globalFar) the coarse cover is global tiles alone (no anchor node: no map data
        waited for), built ahead while the first block is (prebuild_coarse: the same tiles the commit then selects),
        and the full selection after it has the anchor's own levels again."""
        async def go():
            c = earthd.Connection(self.s, None, _Writer())
            c.global_far = True
            a = self.s.new_anchor(LAT, LON)
            a.first_terrain = (0.0, 0.0)
            a.prebuild_focus = (0.0, 0.0)
            asked = []
            real = self.s.tile
            self.s.tile = lambda anchor, t, shield=True: asked.append(t.key)
            try:
                n = c.prebuild_coarse(a)
                c.view_cap = 30000.0             # (a fixed view: the same radius without a horizon grid)
                n2 = c.prebuild_coarse(a)
            finally:
                self.s.tile = real
            a.ramp = True
            self.s.anchors[a.id] = a
            c.commit_anchor(a.id)
            coarse = dict(c.desired)
            c.first_ramp, c.desired = None, {}
            c.update_far()
            return n, n2, asked, coarse, dict(c.desired)
        n, n2, asked, coarse, full = asyncio.run(go())
        self.assertGreater(n, 10)
        self.assertEqual(len(asked), n + n2)
        self.assertTrue(all(ff.is_global_key(k) for k in coarse), sorted(coarse)[:5])
        self.assertTrue(set(coarse) <= set(asked), "the commit's coarse tiles were all built ahead")
        self.assertTrue(any(not ff.is_global_key(k) for k in full))
        self.assertLess(len(coarse), len(full) / 2)

    def test_graded_tiles_capped(self):
        """_next_tile(graded_ok=False) passes the graded tiles (level <= 0) over and gives them first again, in their
        order, once a graded slot is free."""
        c = earthd.Connection.__new__(earthd.Connection)
        c.s, c.active = self.s, 1
        tiles = [ff.Tile(-1, 0, 0), ff.Tile(-1, 1, 0), ff.Tile(1, 0, 0), ff.Tile(0, 3, 0), ff.Tile(2, 0, 0)]
        c.desired = {t.key: t for t in tiles}
        c.sent, c.inflight = {}, {}
        c.queue = list(tiles)

        def next_tile(ok):
            return c.far.next_tile(c.active, ok, 0, set())
        got = [next_tile(False), next_tile(False)]
        self.assertEqual([t.key for t in got], ["1_0_0", "2_0_0"])
        self.assertIsNone(next_tile(False))
        self.assertEqual([t.key for t in c.far.graded_wait], ["-1_0_0", "-1_1_0", "0_3_0"])
        self.assertEqual(next_tile(True).key, "-1_0_0")
        c.sent = {"-1_1_0": tiles[1]}               # (sent meanwhile: skipped)
        self.assertEqual(next_tile(True).key, "0_3_0")
        self.assertIsNone(next_tile(True))
        g = ff.GTile(12, 1, 1, -1, (0, 0, 1, 0, 1, 1, 0, 1), 1.0)
        c.desired = {g.key: g}
        c.queue = [g]
        self.assertIs(next_tile(False), g, "a global tile is never graded")
        self.assertEqual(c.graded_top(), 0)
        self.s.sea = object()           # (--sea on: the sea-carved levels wait for the water too)
        self.assertEqual(c.graded_top(), earthd.seacarvemod.CARVE_MAX_LEVEL)


if __name__ == "__main__":
    unittest.main()
