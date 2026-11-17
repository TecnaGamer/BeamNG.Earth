"""The generated ground's stage 4 (earthfast 313): the forest floor follows the game's own trees (vegetation's placed
trees, read from its cache): a crown-sized disc round each trunk fading out over FLOOR_FADE_M; a canopy texel without
a tree there is the open ground it would be; no trees drawn: the canopy's own colour; their cells not built yet: the
canopy's core only (no skirt) and the photo pending."""
import os
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fast  # noqa: E402
import genground as gg  # noqa: E402
import geodesy  # noqa: E402
import vegetation  # noqa: E402

fast.require(gg.NEED, "generated ground")
EF = fast._ef


def regions(base, canopy, floor=None, skirt=False):
    base = np.asarray(base, np.uint8)
    sh = base.shape
    z32 = np.zeros(sh, np.uint32)
    return EF.gg_regions(base, z32, np.zeros(0, np.uint8), z32.copy(), np.zeros(0, np.uint8),
                         np.asarray(canopy, np.uint8), np.zeros(sh, np.uint8), 1.0,
                         None if floor is None else np.asarray(floor, np.uint8), skirt)


def gen(s2, kind, floor=None, mode=0):
    n = kind.shape[0]
    return EF.gen_ground(np.ascontiguousarray(s2), np.ascontiguousarray(kind, np.uint8),
                         np.ascontiguousarray(kind.astype(np.uint32)), np.full((n, n), 255, np.uint8),
                         np.zeros((n, n), np.uint32), np.zeros((0, 2)), np.zeros(0), 0.0, 0.0, 1.0, 0, 0,
                         None if floor is None else np.ascontiguousarray(floor, np.uint8), mode)


class DiscsTest(unittest.TestCase):
    def test_disc_and_fade(self):
        out = np.zeros((20, 20), np.uint8)
        EF.gg_discs(out, np.array([10.0]), np.array([10.0]), np.array([3.0]), 2.0)
        self.assertEqual(out[10, 10], 255)
        self.assertEqual(out[10, 12], 255, "within the crown")
        self.assertTrue(0 < out[10, 13] < 255, "the fade")
        self.assertEqual(out[10, 16], 0, "past crown and fade")
        out2 = np.zeros((20, 20), np.uint8)
        EF.gg_discs(out2, np.array([10.0, 13.0]), np.array([10.0, 10.0]), np.array([3.0, 1.0]), 2.0)
        self.assertTrue((out2 >= out).all(), "the max over the trees")


class RegionsTest(unittest.TestCase):
    def test_trees_only_where_placed(self):
        G, T, C = 3, 1, 4
        base = [[G, G, T, T, C, C]]
        canopy = [[9, 9, 9, 2, 9, 0]]
        k, _ = regions(base, canopy, floor=[[255, 0, 0, 200, 0, 0]])
        self.assertEqual(k.tolist(), [[T, G, G, T, C, C]],
                         "tall canopy without a placed tree: the open ground; the land cover's trees likewise; a "
                         "placed tree's crown over low canopy: a tree")
        k, _ = regions(base, canopy)
        self.assertEqual(k.tolist(), [[T, T, T, 2, T, C]], "no placement known: the canopy decides, as before")

    def test_hedgerow_under_placed_bushes(self):
        # (stage 4b) a hedgerow's low canopy (1-3 m) where the game places bushes or trees: wooded (the forest floor
        # under them), not the open ground it became in genv4; without anything placed there: open ground as before
        G, T = 3, 1
        k, _ = regions([[G, G, G, G]], [[2, 2, 0, 2]], floor=[[255, 120, 255, 0]])
        self.assertEqual(k.tolist(), [[T, T, G, G]])

    def test_no_skirt_with_the_games_trees(self):
        base = [[4] * 8]
        canopy = [[0, 1, 9, 2, 1, 1, 1, 1]]
        k, _ = regions(base, canopy, skirt=False)
        self.assertEqual(k.tolist(), [[4, 4, 1, 4, 4, 4, 4, 4]], "the canopy's core alone")
        k, _ = regions(base, canopy, skirt=True)
        self.assertEqual(k.tolist()[0][1:6], [1] * 5, "the skirt when the trees are not drawn")


class FloorColourTest(unittest.TestCase):
    def test_floor_modes(self):
        n = 64
        s2 = np.full((n, n, 3), (40, 60, 35), np.uint8)    # (the photo's dark canopy)
        kind = np.full((n, n), 1, np.uint8)
        y = lambda o: float(o[20:44, 20:44].astype(float).mean())
        none = y(gen(s2, kind, mode=0))
        full = y(gen(s2, kind, mode=1))
        floor = np.zeros((n, n), np.uint8)
        floor[:, : n // 2] = 255
        placed = gen(s2, kind, floor, 2).astype(float)
        self.assertLess(none, full - 10, "trees drawn: the forest floor, lighter than the canopy")
        self.assertAlmostEqual(placed[20:44, 10:22].mean(), full, delta=4)
        self.assertAlmostEqual(placed[20:44, 42:54].mean(), none, delta=4)


def gg_floor():
    return (98.0, 94.0, 66.0)          # (genground.rs FLOOR)


class RoadsWinTest(unittest.TestCase):
    """(stage 4, the user: "take into consideration if the road is blocked by other buildings or trees") a road always
    wins: a drawn carriageway over a building's grown footprint and over canopy, a mapped track / path under a placed
    crown and tall canopy, never forest floor or roof."""

    def test_road_over_canopy_crowns_and_roofs(self):
        base = np.full((1, 6), 3, np.uint8)
        canopy = np.array([[12, 12, 12, 12, 0, 0]], np.uint8)
        floor = np.array([[255, 255, 255, 0, 0, 0]], np.uint8)       # (a placed crown over the first three)
        cover = np.array([[2, 0, 0, 0, 2 | 4, 4]], np.uint8)         # (paved; paved under a footprint; a roof)
        line = np.array([[0, 1, 2, 0, 0, 0]], np.uint32)            # (a gravel track, a footway)
        z32 = np.zeros((1, 6), np.uint32)
        k, _ = EF.gg_regions(base, z32, np.zeros(0, np.uint8), line, np.array([gg.GRAVEL, gg.PATH_AUTO], np.uint8),
                             canopy, cover, 1.0, floor, False)
        self.assertEqual(k.tolist(), [[gg.PAVED, gg.GRAVEL, gg.PATH, gg.GRASS, gg.PAVED, gg.ROOF]])
        # (no placement known, the canopy model alone, the skirt on: the track and footway stay, F1)
        k, _ = EF.gg_regions(base, z32, np.zeros(0, np.uint8), line, np.array([gg.GRAVEL, gg.PATH_AUTO], np.uint8),
                             canopy, cover, 1.0, None, True)
        self.assertEqual(k.tolist()[0][1:3], [gg.GRAVEL, gg.PATH])
        self.assertEqual(k.tolist()[0][3], gg.TREE, "(the canopy beside them: trees, as before)")
        sand = np.array([[0, 1, 1, 1, 0, 0]], np.uint32)
        k, _ = EF.gg_regions(base, z32, np.zeros(0, np.uint8), sand, np.array([gg.BARE], np.uint8),
                             canopy, np.zeros((1, 6), np.uint8), 1.0, None, True)
        self.assertEqual(k.tolist()[0][1:4], [gg.BARE] * 3, "a sand path under the trees")
        # (and no forest floor on them in the colours: only TREE texels take it)
        s2 = np.full((1, 6, 3), 90, np.uint8)
        n = 8
        s2 = np.full((n, n, 3), 90, np.uint8)
        kind = np.full((n, n), gg.GRAVEL, np.uint8)
        fl = np.full((n, n), 255, np.uint8)
        out = EF.gen_ground(s2, kind, kind.astype(np.uint32), np.full((n, n), 12, np.uint8),
                            np.zeros((n, n), np.uint32), np.zeros((0, 2)), np.zeros(0), 0.0, 0.0, 1.0, 0, 0, fl, 2)
        ref = EF.gen_ground(s2, kind, kind.astype(np.uint32), np.full((n, n), 12, np.uint8),
                            np.zeros((n, n), np.uint32), np.zeros((0, 2)), np.zeros(0), 0.0, 0.0, 1.0, 0, 0, None, 0)
        np.testing.assert_array_equal(out, ref)


class GapTest(unittest.TestCase):
    def test_open_ground_under_unplaced_canopy_takes_the_fields_colour(self):
        # a wood's edge in the canopy model with no tree placed: the photo is the canopy's dark green there; the open
        # ground takes its field's colour (from the texels without canopy), not the photo's
        n = 160
        s2 = np.full((n, n, 3), (150, 140, 95), np.uint8)       # (a pale field)
        s2[:, :60] = (40, 60, 35)                               # (the canopy model's wood, in the photo)
        canopy = np.zeros((n, n), np.uint8)
        canopy[:, :60] = 15
        kind = np.full((n, n), 3, np.uint8)                     # GRASS everywhere: no tree placed at all
        floor = np.zeros((n, n), np.uint8)
        # (gg_regions marks the open ground the canopy model calls wooded where no tree is placed: UNPLACED)
        _k, region = regions(kind, canopy, floor=floor)
        self.assertTrue((region[:, :60] & 0x40000000 != 0).all() and (region[:, 60:] & 0x40000000 == 0).all())
        out = EF.gen_ground(s2, kind, region, canopy, np.zeros((n, n), np.uint32), np.zeros((0, 2)),
                            np.zeros(0), 0.0, 0.0, 1.0, 0, 0, floor, 2).astype(float)
        self.assertGreater(out[40:120, 44:58].mean(), 110.0, "near the field: its colour, not the canopy's green")
        floor_y = (float(np.mean(gg_floor())) + float(np.mean((40, 60, 35)))) / 2
        self.assertAlmostEqual(out[40:120, 2:10].mean(), floor_y, delta=12,
                               msg="deep in the wood: the forest floor, as under a placed tree")
        before = EF.gen_ground(s2, kind, kind.astype(np.uint32), canopy, np.zeros((n, n), np.uint32),
                               np.zeros((0, 2)), np.zeros(0), 0.0, 0.0, 1.0, 0, 0, None, 1).astype(float)
        self.assertLess(before[40:120, 20:40].mean(), 70.0, "(placement unknown: the photo, as before)")


class StairTest(unittest.TestCase):
    def test_gap_colour_without_steps(self):
        # (stage 4b) the unplaced canopy's colour runs smoothly from the field's (near it) to the forest floor's (deep
        # in the wood): genv4 switched at the 8 m cells where the field's texels left the window (light stair-steps)
        n = 320
        s2 = np.full((n, n, 3), (150, 140, 95), np.uint8)
        s2[:, :200] = (40, 60, 35)
        canopy = np.zeros((n, n), np.uint8)
        canopy[:, :200] = 15
        kind = np.full((n, n), 3, np.uint8)
        floor = np.zeros((n, n), np.uint8)
        _k, region = regions(kind, canopy, floor=floor)
        out = EF.gen_ground(s2, kind, region, canopy, np.zeros((n, n), np.uint32), np.zeros((0, 2)),
                            np.zeros(0), 0.0, 0.0, 1.0, 0, 0, floor, 2).astype(float)
        prof = out[60:260, 20:200].mean(axis=(0, 2))
        jumps = np.abs(np.diff(prof))
        self.assertLess(float(jumps.max()), 4.0, "no step between neighbouring texels across the wood")


class _Veg:
    def __init__(self, ready=True, items=None):
        self._ready, self._items = ready, items

    def ready(self, key):
        return self._ready

    def cell(self, key):
        return self._items


def items(lats, lons, radius, kind=vegetation.KIND_TREE, rank=0.1):
    k = len(lats)
    f = lambda v, dt: np.full(k, v, dt)
    return vegetation.Items(np.asarray(lons, np.float64), np.asarray(lats, np.float64), f(10.0, np.float32),
                            f(radius, np.float32), f(kind, np.uint8), f(0, np.uint8), f(rank, np.float32),
                            f(0.0, np.float32), f(1.0, np.float32))


class TreeFloorTest(unittest.TestCase):
    def setUp(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)

    def test_discs_at_the_placed_trees(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        la, lo, _ = frame.enu_to_geodetic(np.array([10.0]), np.array([-20.0]), 0.0)
        veg = _Veg(items=items(la, lo, 3.0))
        missing = []
        floor, mode = gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg, missing)
        self.assertEqual(mode, 2)
        self.assertEqual(missing, [])
        col, row = int(10.0 + 64.0), int(64.0 - (-20.0))
        self.assertEqual(floor[row, col], 255)
        self.assertEqual(int((floor == 255).sum()) > 20, True)
        self.assertEqual(floor[row, col + 8], 0)
        # (thinned away at the 700 m ring: not counted; (stage 4b) bushes are: a hedgerow's placed shrubs, the
        # coordinator's Penns Valley finding: the hedgerows vanished in genv4)
        veg2 = _Veg(items=items(la, lo, 3.0, rank=0.9))
        self.assertEqual(int(gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg2, [])[0].sum()), 0)
        veg3 = _Veg(items=items(la, lo, 1.5, kind=vegetation.KIND_BUSH))
        self.assertEqual(gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg3, [])[0][row, col], 255)
        veg4 = _Veg(items=items(la, lo, 1.5, kind=vegetation.KIND_ROCK))
        self.assertEqual(int(gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg4, [])[0].sum()), 0, "(rocks not)")

    def test_not_built_and_no_trees(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        missing = []
        floor, mode = gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, _Veg(ready=False), missing)
        self.assertEqual((floor, mode), (None, 1))
        self.assertTrue(missing and missing[0][1] == gg.TREES_LAYER, "pending: made again once they are built")
        self.assertEqual(gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, None, []), (None, 0))

    def test_data_ready_waits_for_the_trees(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        md = types.SimpleNamespace(has=lambda k, layer: True)
        self.assertTrue(gg.data_ready(md, frame, -64.0, -64.0, 128.0, None, _Veg(ready=True)))
        self.assertFalse(gg.data_ready(md, frame, -64.0, -64.0, 128.0, None, _Veg(ready=False)))
        self.assertTrue(gg.data_ready(md, frame, -64.0, -64.0, 128.0, None, None), "no trees: nothing to wait for")


class BuildSoonTest(unittest.TestCase):
    """A block outside the pose's warm-up: the generated ground asks the vegetation to build its cells in the
    background (Vegetation.build_soon) and goes final after one redo, instead of redoing its photo at every retry
    until given up."""

    def setUp(self):
        gg._retries.clear()
        self.addCleanup(gg._retries.clear)

    def test_cells_asked_built_and_final_after_one_redo(self):
        frame = geodesy.EnuFrame(40.877, -77.566)
        la, lo, _ = frame.enu_to_geodetic(np.array([10.0]), np.array([-20.0]), 0.0)

        class Veg:
            def __init__(self):
                self.built, self.asked = set(), []

            def ready(self, key):
                return (int(key[1]), int(key[2])) in self.built

            def cell(self, key):
                return items(la, lo, 3.0)

            def build_soon(self, key):
                self.asked.append((int(key[1]), int(key[2])))
                return True
        veg = Veg()
        md = types.SimpleNamespace(has=lambda k, layer: True)
        missing = []
        floor, mode = gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg, missing)
        self.assertEqual((floor, mode), (None, 1))
        keys = {(int(k[1]), int(k[2])) for k, _l in missing}
        self.assertTrue(keys and set(veg.asked) == keys, "every cell it waits for asked to be built, at once")
        self.assertFalse(gg.data_ready(md, frame, -64.0, -64.0, 128.0, None, veg))
        veg.built |= set(veg.asked)                 # (the warm thread built them)
        self.assertTrue(gg.data_ready(md, frame, -64.0, -64.0, 128.0, None, veg), "ready: made again once")
        missing = []
        floor, mode = gg.tree_floor(frame, -64.0, -64.0, 128.0, 128, veg, missing)
        self.assertEqual((mode, missing), (2, []), "final")

    def test_vegetation_builds_them_in_the_background_bounded(self):
        import threading
        v = vegetation.Vegetation(None, None)
        gate = threading.Event()
        loaded = []

        def load(x, y):
            gate.wait(10)
            loaded.append((x, y))
            return items([], [], 1.0)
        v._load = load
        for i in range(vegetation.BUILD_SOON_MAX + 10):
            v.build_soon((14, 1000 + i, 2000))
        self.assertLessEqual(len(v._soon), vegetation.BUILD_SOON_MAX, "a bounded queue: the oldest dropped")
        gate.set()
        for _ in range(500):
            if v.ready((14, 1000 + vegetation.BUILD_SOON_MAX + 9, 2000)):
                break
            threading.Event().wait(0.01)
        self.assertTrue(v.ready((14, 1000 + vegetation.BUILD_SOON_MAX + 9, 2000)), "built by the warm thread")
        self.assertFalse(v.build_soon((14, 1000 + vegetation.BUILD_SOON_MAX + 9, 2000)), "(built: not queued)")


if __name__ == "__main__":
    unittest.main()
