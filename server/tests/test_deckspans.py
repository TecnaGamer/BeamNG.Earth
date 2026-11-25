"""The bridges round the car for the mod's deck guard (deckspans.py, deck_spans; mod deckguard.lua)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deckspans  # noqa: E402
from test_roads import FRAME, features  # noqa: E402


def piece(fid, pts):
    return (fid, np.asarray(pts, np.float64))


class ChainsTest(unittest.TestCase):
    def test_pieces_sharing_an_end_are_one_span_from_abutment_to_abutment(self):
        got = deckspans.chains([piece("a", [(0, 0), (50, 0)]), piece("b", [(50, 0), (120, 0)])])
        self.assertEqual(len(got), 1)
        xy, bits = got[0]
        np.testing.assert_allclose(xy, [(0, 0), (50, 0), (120, 0)])
        self.assertEqual(bits, 3)

    def test_a_piece_drawn_the_other_way_is_joined_too(self):
        got = deckspans.chains([piece("a", [(50, 0), (0, 0)]), piece("b", [(50, 0), (120, 0)]),
                                piece("c", [(200, 0), (120, 0)])])
        self.assertEqual(len(got), 1)
        xy, bits = got[0]
        self.assertEqual(len(xy), 4)
        self.assertEqual({tuple(xy[0]), tuple(xy[-1])}, {(0.0, 0.0), (200.0, 0.0)})
        self.assertEqual(bits, 3)

    def test_a_fork_on_a_bridge_ends_the_spans_there_and_is_no_abutment(self):
        got = deckspans.chains([piece("a", [(0, 0), (50, 0)]), piece("b", [(50, 0), (100, 0)]),
                                piece("c", [(50, 0), (100, 30)])])
        self.assertEqual(len(got), 3)
        for xy, bits in got:
            at_fork = 1 if tuple(xy[0]) == (50.0, 0.0) else 2
            self.assertEqual(bits & at_fork, 0)       # (the fork's end: on another deck, not the ground)
            self.assertEqual(bits | at_fork, 3)

    def test_same_pieces_same_chains(self):
        ps = [piece("a", [(0, 0), (50, 0)]), piece("b", [(50, 0), (120, 0)]), piece("x", [(500, 0), (560, 0)])]
        a, b = deckspans.chains(ps), deckspans.chains(list(ps))
        self.assertEqual([(x.tolist(), k) for x, k in a], [(x.tolist(), k) for x, k in b])


class CellPiecesTest(unittest.TestCase):
    def test_tagged_bridges_of_drivable_roads_only(self):
        f = features([[(0, 0), (1000, 0)], [(0, 50), (200, 50)], [(0, 100), (200, 100)], [(0, 150), (200, 150)]],
                     ["primary", "footway", "residential", "primary"],
                     flags=[(0, "is_bridge", 0.4, 0.5), (1, "is_bridge"), (2, "is_tunnel")],
                     subtypes=["road", "road", "road", "rail"])
        got = deckspans.cell_pieces(f, FRAME)
        self.assertEqual([fid for fid, _xy in got], ["id000"])
        xy = got[0][1]
        self.assertAlmostEqual(float(xy[0][0]), 400.0, delta=1.0)     # (the bridge range 0.4-0.5 of the 1 km road)
        self.assertAlmostEqual(float(xy[-1][0]), 500.0, delta=1.0)
        self.assertLess(float(np.max(np.abs(xy[:, 1]))), 1.0)

    def test_a_level_rule_alone_is_a_bridge(self):
        f = features([[(0, 0), (300, 0)]], ["secondary"], levels=[(0, 1.0, 0.2, 0.6)])
        got = deckspans.cell_pieces(f, FRAME)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(float(got[0][1][0][0]), 60.0, delta=1.0)


class MessageTest(unittest.TestCase):
    def test_the_spans_near_the_car_nearest_first_rounded_once_each(self):
        ps = [piece("far", [(5000, 0), (5100, 0)]), piece("b", [(300.04, 0), (360, 0.06)]),
              piece("a", [(100, 20), (160, 20)]), piece("a", [(100, 20), (160, 20)])]    # (a listed by two cells)
        m = deckspans.message(3, 0.0, 0.0, ps)
        self.assertEqual(m["type"], "deck_spans")
        self.assertEqual(m["anchorId"], 3)
        self.assertEqual(m["spans"], [[100.0, 20.0, 160.0, 20.0], [300.0, 0.0, 360.0, 0.1]])
        self.assertEqual(m["abut"], [3, 3])
        self.assertEqual(m["r"], deckspans.RADIUS_M)

    def test_a_long_span_near_the_car_by_its_line_not_its_points(self):
        m = deckspans.message(1, 0.0, 0.0, [piece("a", [(-3000, 100), (3000, 100)])])
        self.assertEqual(len(m["spans"]), 1)

    def test_a_span_of_many_points_is_thinned_ends_kept(self):
        pts = [(float(i), 0.0) for i in range(500)]
        m = deckspans.message(1, 0.0, 0.0, [piece("a", pts)])
        s = m["spans"][0]
        self.assertLessEqual(len(s) // 2, deckspans.MAX_PTS)
        self.assertEqual(s[:2], [0.0, 0.0])
        self.assertEqual(s[-2:], [499.0, 0.0])


class WaitKeysTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(deckspans.wait_keys(["g17_10_20", "g15_1_2", 5, "x_1_2"]), {(10, 20)})
        self.assertEqual(deckspans.wait_keys({"1": "g17_3_4"}), {(3, 4)})
        self.assertEqual(deckspans.wait_keys(None), set())
        self.assertEqual(len(deckspans.wait_keys([f"g17_{i}_0" for i in range(50)])), deckspans.WAIT_KEYS)


class FakeMapdata:
    def __init__(self, feats):
        self.feats, self.reads = feats, 0

    def has(self, key, layer):
        return layer == "roads"

    def layer_unkept(self, key, layer, kind="background"):
        self.reads += 1
        return self.feats


class SpansTest(unittest.TestCase):
    def test_made_once_a_place_read_once_a_cell(self):
        f = features([[(0, 0), (1000, 0)]], ["primary"], flags=[(0, "is_bridge", 0.4, 0.5)])
        md = FakeMapdata(f)
        s = deckspans.Spans(md)
        self.assertTrue(s.due(1, 0.0, 0.0, now=0.0))
        m = s.make(1, FRAME, 0.0, 0.0)
        self.assertIsNotNone(m)
        self.assertEqual(len(m["spans"]), 1)        # (every cell lists the same feature: once)
        reads = md.reads
        self.assertGreater(reads, 0)
        self.assertFalse(s.due(1, 10.0, 0.0))       # (not moved far enough, not long enough)
        self.assertTrue(s.due(1, 200.0, 0.0))
        self.assertIsNone(s.make(1, FRAME, 10.0, 0.0))    # (the same spans round the same place: not sent again)
        self.assertEqual(md.reads, reads)           # (its cells kept)
        self.assertTrue(s.due(2, 10.0, 0.0))        # (another anchor)
        self.assertIsNotNone(s.make(2, FRAME, 10.0, 0.0))
        self.assertGreater(md.reads, reads)         # (read again in the new anchor's world)

    def test_no_map_data_never_due(self):
        self.assertFalse(deckspans.Spans(None).due(1, 0.0, 0.0))


class ConnectionTest(unittest.TestCase):
    """earthd Connection.deck_spans: sent to a mod with the guard (hello.deckGuard) for the active anchor only."""

    def run_poses(self, guard=True, active=1, aid=1):
        import asyncio
        import types
        from concurrent.futures import ThreadPoolExecutor
        import earthd
        f = features([[(0, 0), (1000, 0)]], ["primary"], flags=[(0, "is_bridge", 0.4, 0.5)])
        sent = []

        async def main():
            loop = asyncio.get_running_loop()
            tasks = []
            c = types.SimpleNamespace(
                deck_guard=guard, active=active, following=True, map_view=False, map_open=False, decks=None,
                focus=(0.0, 0.0), loop=loop, send=sent.append, spawn=lambda co: tasks.append(loop.create_task(co)),
                s=types.SimpleNamespace(mapdata=FakeMapdata(f), anchors={1: types.SimpleNamespace(frame=FRAME)},
                                        deck_pool=ThreadPoolExecutor(1)))
            earthd.Connection.deck_spans(c, aid, 0.0, 0.0)
            earthd.Connection.deck_spans(c, aid, 0.0, 0.0)      # (busy: no second job)
            await asyncio.gather(*tasks)
            return len(tasks)

        n = asyncio.run(main())
        return sent, n

    def test_sent_once(self):
        sent, n = self.run_poses()
        self.assertEqual(n, 1)
        self.assertEqual([m["type"] for m in sent], ["deck_spans"])
        self.assertEqual(len(sent[0]["spans"]), 1)

    def test_not_to_a_mod_without_the_guard_nor_for_another_anchor(self):
        self.assertEqual(self.run_poses(guard=False), ([], 0))
        self.assertEqual(self.run_poses(active=2), ([], 0))


class DeckFirstTest(unittest.TestCase):
    """roads.GeoTracker: the tile the mod's deck guard waits on (pose.deckWait) is built first."""

    def test_the_waited_tile_first(self):
        from concurrent.futures import Future
        from types import SimpleNamespace
        import geotiles
        import roads

        class Loop:
            def __init__(self):
                self.jobs = []

            def now(self):
                return 100.0

            def run_in_executor(self, _pool, _build, aid, gx, gy, content, skin, make):
                self.jobs.append((gx, gy))
                return Future()             # (never done: the order alone is looked at)

        far = geotiles.tiles_near(FRAME, 0.0, 550.0, 1.0, exact=True)[0][:2]
        for first in (None, far):
            t = roads.GeoTracker()
            t.pool = object()
            t.deck_first = {far} if first else set()
            loop = Loop()
            t.on_pose(SimpleNamespace(s=None, send=lambda m: None), 1, 0.0, 0.0, loop, None, vel=(0.0, 0.0),
                      frame=FRAME)
            if first:
                self.assertEqual(loop.jobs[0], far, "the bridge's tile first")
            else:
                self.assertNotIn(far, loop.jobs[:1], "else nearest first")

    def test_the_waited_tile_while_terrain_is_built(self):
        """Terrain work narrows the eligible tiles to the send radius (ground_busy); a bridge the guard waits on
        beyond it is still built, first (codex-speedlod: bridges lagged at 130 mph)."""
        from concurrent.futures import Future
        from types import SimpleNamespace
        import geotiles
        import roads

        class Loop:
            def __init__(self):
                self.jobs = []

            def now(self):
                return 100.0

            def run_in_executor(self, _pool, _build, aid, gx, gy, content, skin, make):
                self.jobs.append((gx, gy))
                return Future()

        far = geotiles.tiles_near(FRAME, 0.0, roads.SEND_M + 400.0, 1.0, exact=True)[0][:2]
        for first in (False, True):
            t = roads.GeoTracker()
            t.pool = object()
            t.deck_first = {far} if first else set()
            loop = Loop()
            t.on_pose(SimpleNamespace(s=None, send=lambda m: None), 1, 0.0, 0.0, loop, None, vel=(0.0, 0.0),
                      frame=FRAME, ground_busy=True)
            if first:
                self.assertEqual(loop.jobs[0], far, "the waited bridge first, beyond the narrowed radius")
            else:
                self.assertNotIn(far, loop.jobs, "nothing else that far while the ground is built")


if __name__ == "__main__":
    unittest.main()
