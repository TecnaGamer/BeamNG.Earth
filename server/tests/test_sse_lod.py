"""Screen-space LOD (farfield.View in select and select_global): a node whose built tile's split would pop more than
tau px on screen splits beyond the distance rule (within SSE_REACH of it); one whose split would pop less does not split
even inside it (merges), at most one level coarser than the distance rule; unknown nodes keep the distance rule; tau 0
is the distance rule exactly. Also the error it goes by (Built.err_n, along the normal)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import farfield as ff  # noqa: E402

LAY = ff.LAYOUTS[64] if hasattr(ff, "LAYOUTS") else None


def pick(errs, cx=0.0, cy=0.0, prev=frozenset(), dz=0.0):
    view = ff.View(cx, cy, dz, 1440 / (2 * math.tan(math.radians(65) / 2)), lambda n: errs.get(n), tau=2.0, cap=1e9)
    return ff.select(0.0, 0.0, prev_refined=prev, radius=20000, view=view)


class SseTest(unittest.TestCase):
    def setUp(self):
        self.base, self.base_ref = ff.select(0.0, 0.0, radius=20000)
        # an L3 node just past its distance-rule switch distance
        lim = ff.switch_distance(3)
        s = ff.level_size(3)
        i = int(math.ceil(lim * 1.2 / s))
        self.node = (3, i, 0)
        self.assertIn(ff.Tile(*self.node).key, self.base)

    def test_rough_tile_splits(self):
        out, ref = pick({self.node: 400.0})            # a cliff: 400 m pop
        self.assertNotIn(ff.Tile(*self.node).key, out)
        self.assertIn(self.node, ref)

    def test_smooth_or_unknown_keeps_distance_rule(self):
        for errs in ({self.node: 0.5}, {}):
            out, _ = pick(errs)
            self.assertIn(ff.Tile(*self.node).key, out)

    def test_hysteresis(self):
        lvl, i, j = self.node
        x0 = i * ff.level_size(lvl)
        d = x0
        ppr = 1440 / (2 * math.tan(math.radians(65) / 2))
        e = 1.5 * d / ppr               # 1.5 px: under tau (2), over tau / 1.5
        out, _ = pick({self.node: e})
        self.assertIn(ff.Tile(*self.node).key, out)                 # not split from scratch
        out, _ = pick({self.node: e}, prev=frozenset({self.node}))
        self.assertNotIn(ff.Tile(*self.node).key, out)              # kept split

    def test_reach_caps_the_extra_refinement(self):
        view = ff.View(0.0, 0.0, 0.0, 2000.0, lambda n: 5000.0, reach=1.0, tau=2.0)    # every tile a cliff, no reach
        out, _ = ff.select(0.0, 0.0, radius=20000, view=view)
        self.assertEqual(set(out), set(self.base))
        view = ff.View(0.0, 0.0, 0.0, 2000.0, lambda n: 5000.0, tau=2.0)       # default reach: ~2 levels more
        out, _ = ff.select(0.0, 0.0, radius=20000, view=view)
        self.assertGreater(len(out), len(self.base))
        self.assertLess(len(out), 40 * len(self.base))


class ViewOrderTest(unittest.TestCase):
    def test_in_view_tiles_first(self):
        cone = ff.view_cone({"x": 0, "y": 0, "fx": 0, "fy": 1, "fovDeg": 65})       # looking north
        ahead, behind = ff.Tile(1, 0, 4), ff.Tile(1, 0, -5)
        self.assertAlmostEqual(ff.view_distance(ahead, cone, 0, 0), ahead.distance(0, 0))
        self.assertAlmostEqual(ff.view_distance(behind, cone, 0, 0), ff.VIEW_PENALTY * behind.distance(0, 0))
        self.assertLess(ff.view_distance(ahead, cone, 0, 0), ff.view_distance(behind, cone, 0, 0))

    def test_no_heading_no_penalty(self):
        self.assertIsNone(ff.view_cone({"x": 0, "y": 0, "fx": 0, "fy": 0.05, "fovDeg": 65}))     # looking down
        self.assertIsNone(ff.view_cone({"x": 0}))
        t = ff.Tile(1, 0, -5)
        self.assertEqual(ff.view_distance(t, None, 0, 0), t.distance(0, 0))


PPR = 1440 / (2 * math.tan(math.radians(60) / 2))
MESH64 = dict(layout=ff.Layout(64, 1024, True), min_level=-1, forced_level=-1, radius=40000)


def _poly(t):
    if isinstance(t, ff.GTile):
        q = t.quad
    elif isinstance(t, ff.Piece):
        q = t.poly
    else:
        x0, y0, x1, y1 = t.bbox
        q = (x0, y0, x1, y0, x1, y1, x0, y1)
    return [(q[2 * k], q[2 * k + 1]) for k in range(len(q) // 2)]


def _inside(poly, x, y):
    s = 0
    for k in range(len(poly)):
        (ax, ay), (bx, by) = poly[k], poly[(k + 1) % len(poly)]
        c = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
        if c != 0:
            if s and (c > 0) != (s > 0):
                return False
            s = c
    return True


def leaf_level_at(tiles, x, y):
    for t in tiles.values():
        x0, y0, x1, y1 = t.bbox
        if x0 <= x <= x1 and y0 <= y <= y1 and _inside(_poly(t), x, y):
            return t.level
    return None


def nearest_inside(t, px, py):
    """The point of a tile's footprint nearest to (px, py), nudged 1 % towards its middle."""
    p = _poly(t)
    if len(p) != 4:
        x0, y0, x1, y1 = t.bbox
        p = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    best = None
    for a in range(41):
        for b in range(41):
            u, v = a / 40, b / 40
            x = (1 - u) * (1 - v) * p[0][0] + u * (1 - v) * p[1][0] + u * v * p[2][0] + (1 - u) * v * p[3][0]
            y = (1 - u) * (1 - v) * p[0][1] + u * (1 - v) * p[1][1] + u * v * p[2][1] + (1 - u) * v * p[3][1]
            d = math.hypot(x - px, y - py)
            if best is None or d < best[0]:
                best = (d, x, y)
    cx, cy = sum(q[0] for q in p) / 4, sum(q[1] for q in p) / 4
    return best[1] + 0.01 * (cx - best[1]), best[2] + 0.01 * (cy - best[2])


class MergeTest(unittest.TestCase):
    """The merging rule (2026-11-11, pixel_lod_plan step 3): with tau > 0 a node whose known error projects under
    tau / (1 + h) px does not split even inside the distance rule, at most one level coarser than the distance rule;
    the near radii, the finest split level and the holds stay; tau 0 is today's selection exactly."""

    def view(self, err, tau=2.0, **kw):
        return ff.View(150.0, 70.0, 2.0, PPR, err, tau=tau, cells=64, **kw)

    def test_tau_zero_is_the_distance_rule(self):
        base = ff.select(150.0, 70.0, **MESH64)
        for err in (lambda n: 0.0, lambda n: 1e6):
            got = ff.select(150.0, 70.0, view=self.view(err, tau=0.0), **MESH64)
            self.assertEqual(set(got[0]), set(base[0]))
            self.assertEqual(got[1], base[1])

    def test_smooth_ground_merges_one_level_at_most(self):
        base, _ = ff.select(150.0, 70.0, **MESH64)
        got, _ = ff.select(150.0, 70.0, view=self.view(lambda n: 0.001), **MESH64)
        self.assertLess(len(got), 0.6 * len(base), "flat ground: far fewer tiles")
        for t in got.values():          # (at its point nearest to the player: what the floor guarantees)
            lv = leaf_level_at(base, *nearest_inside(t, 150.0, 70.0))
            if lv is not None:
                self.assertLessEqual(t.level, lv + 1, (t.key, lv))
        # the near radii (L-1 to 150 m, L0 to 400 m) and the finest split level kept
        lim0 = ff.switch_distance(0, min_level=-1, layout=MESH64["layout"])
        for t in base.values():
            if t.distance(150.0, 70.0) < lim0:
                self.assertIn(t.key, got, t.key)
        for r, lvl in ((140.0, -1), (390.0, 0)):
            for a in range(0, 360, 30):
                x, y = 150.0 + r * math.cos(math.radians(a)), 70.0 + r * math.sin(math.radians(a))
                self.assertLessEqual(leaf_level_at(got, x, y), lvl)

    def test_rough_ground_splits_finer(self):
        base, _ = ff.select(150.0, 70.0, **MESH64)
        got, _ = ff.select(150.0, 70.0, view=self.view(lambda n: 1e4), **MESH64)
        self.assertGreater(len(got), len(base))

    def test_unknown_errors_keep_the_distance_rule(self):
        base, _ = ff.select(150.0, 70.0, **MESH64)
        got, _ = ff.select(150.0, 70.0, view=self.view(lambda n: None), **MESH64)
        self.assertEqual(set(got), set(base))

    def test_merge_hysteresis_and_holds(self):
        lay = MESH64["layout"]
        # an L3 node inside its switch distance, past its children's (+ their hysteresis: the merge band)
        lim3, lim2 = ff.switch_distance(3, layout=lay, min_level=-1), ff.switch_distance(2, layout=lay, min_level=-1)
        node = (3, 1, 0)
        d = ff.Tile(*node).distance(-150.0, 70.0)
        self.assertTrue(1.3 * lim2 < d < lim3, (lim2, d, lim3))
        dist = math.hypot(d, 2.0)
        band = 1.6 * dist / PPR             # 1.6 px: under tau (2), over tau / 1.5
        under = 1.0 * dist / PPR            # 1 px: under tau / 1.5
        err = {node: band}
        v = ff.View(-150.0, 70.0, 2.0, PPR, lambda n: err.get(n), tau=2.0, cells=64, cap=1e9)
        out, ref = ff.select(-150.0, 70.0, view=v, prev_refined=frozenset(), **MESH64)
        self.assertIn(ff.Tile(*node).key, out, "in the band, not refined before: stays merged")
        out, ref = ff.select(-150.0, 70.0, view=v, prev_refined=frozenset({node}), **MESH64)
        self.assertIn(node, ref, "in the band, refined before: stays split")
        err[node] = under
        out, ref = ff.select(-150.0, 70.0, view=v, prev_refined=frozenset({node}), **MESH64)
        self.assertIn(ff.Tile(*node).key, out, "under tau / 1.5: merges")
        out, ref = ff.select(-150.0, 70.0, view=v, prev_refined=frozenset({node}), hold=frozenset({node}), **MESH64)
        self.assertIn(node, ref, "held: keeps its state")

    def test_global_selection_merges_and_splits_in_rust(self):
        import fast
        import geodesy
        fast.require(ff.SSE_NEED, "the test")
        fr = geodesy.EnuFrame(35.46, -118.77)
        zf = ff.global_zoom(35.46)
        args = dict(layout=ff.Layout(64, 1024, True), max_level=9, min_level=-1, radius=80000.0, forced_level=-1)
        base, bref = ff.select_global(fr, 0.0, 0.0, zf, **args)
        gl = [t for t in base.values() if isinstance(t, ff.GTile)]
        # every global node a smooth one: fewer global tiles; every one a cliff: more
        nodes = {n[1:] for n in bref if len(n) == 4} | {(t.z, t.x, t.y) for t in gl}

        def view(e, tau=2.0):
            return ff.View(0.0, 0.0, 2.0, PPR, lambda n: None, tau=tau, cells=64,
                           gerr={k: e for k in nodes | {(z + 1, 2 * x + a, 2 * y + b) for z, x, y in nodes
                                                       for a in (0, 1) for b in (0, 1)}})
        z0, zref = ff.select_global(fr, 0.0, 0.0, zf, view=view(0.0, tau=0.0), **args)
        self.assertEqual(set(z0), set(base), "tau 0: today's selection")
        self.assertEqual(zref, bref)
        smooth, _ = ff.select_global(fr, 0.0, 0.0, zf, view=view(0.001), **args)
        rough, _ = ff.select_global(fr, 0.0, 0.0, zf, view=view(1e4), **args)
        n = lambda out: sum(isinstance(t, ff.GTile) for t in out.values())     # noqa: E731
        self.assertLess(n(smooth), n(base))
        self.assertGreater(n(rough), n(base))
        # one level coarser at most: a merged global tile's level against the distance rule's at its point nearest to
        # the player (where the anchor's nodes were, its grid not the global one's: the finest global level)
        for t in smooth.values():
            if isinstance(t, ff.GTile):
                p = nearest_inside(t, 0.0, 0.0)
                lv = leaf_level_at({k: u for k, u in base.items() if isinstance(u, ff.GTile)}, *p)
                if lv is not None:
                    self.assertLessEqual(t.level, lv + 1, (t.key, lv))
                elif leaf_level_at(base, *p) is not None:
                    self.assertEqual(t.level, ff.GLOBAL_MIN_LEVEL, t.key)


class NormalErrorTest(unittest.TestCase):
    """Built.err_n: the error along the surface normal (earthfast normal_err), the screen-space LOD's metric: on flat
    ground about the vertical error, on a steep slope much less (a cliff's face shifts sideways)."""

    def test_flat_and_steep(self):
        import fast
        import numpy as np
        fast.require(ff.SSE_NEED, "the test")
        n = 9
        flat_m = np.zeros((n, n))
        bump = flat_m.copy()
        bump[4, 4] = 2.0
        self.assertAlmostEqual(fast._ef.far_normal_err(bump, flat_m, 4.0), 2.0)
        # a 60-degree slope (rise tan 60 per m) with the same 2 m vertical bump: 2 cos 60 = 1
        x = np.arange(n) * 4.0
        steep = np.tile(x * math.tan(math.radians(60)), (n, 1))
        ref = steep.copy()
        ref[4, 4] += 2.0
        self.assertAlmostEqual(fast._ef.far_normal_err(ref, steep, 4.0), 1.0, places=6)

    def test_build_tile_measures_it(self):
        import fast
        import geodesy
        fast.require(ff.SSE_NEED, "the test")
        import numpy as np

        class Rough:
            def sample(self, lat, lon, zoom, progress=None):
                lat, lon = np.asarray(lat), np.asarray(lon)
                return 1500 + 900 * np.sin(lat * 900) + 6.0 * np.sin(lat * 61000) * np.cos(lon * 47000)
        fr = geodesy.EnuFrame(36.5785, -118.2923)
        lay = ff.Layout(64, 1024, True)
        b = ff.build_tile(fr, Rough(), ff.Tile(3, 1, 1), ff.zoom_for_level(lay, 36.5785, 3), layout=lay)
        self.assertIsNotNone(b.err_n)
        self.assertLessEqual(b.err_n, b.err + 1e-9)
        self.assertGreater(b.err_n, 0.0)
        self.assertEqual(b.lod_err, b.err_n)
        self.assertEqual(ff.Built(*([None] * 7), 1.5, None, 0.0, 1.0).lod_err, 1.5, "no err_n (older caches): err")


if __name__ == "__main__":
    unittest.main()
