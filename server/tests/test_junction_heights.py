"""A junction a deck's embankment reaches is at the embankment's height for every road meeting it (gradeshapes.
embankments, _junction_roads; grading.shape_reach). Porte Maillot's square at the end of a 21.8 m wide deck: the
embankment went on along one road only, the five others stayed under it and the junction snap put the square back at
theirs, 1.1 m under its ground: the deck's last 5.4 m fell 1.6 m into the square."""
import logging
import os
import sys
import types
import unittest
import unittest.mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import grading  # noqa: E402
import roadlaws  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "drive_paris_maillot_1083.npz")
NODE = (48.877341, 2.279266)    # (the square's node: the deck's road, the trunk and four others)
DECK = "bb5e4cb6"               # (the deck's road, its ground part from the deck's end to the node)
TRUNK = "10ca6b31"


@unittest.skipUnless(os.path.exists(REPLAY), "the Porte Maillot replay")
class SquareAtTheDecksEndTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import drivability
        import interchanges
        got = {}
        real = grading._Planner._road_laws

        def spy(self, key, plans, specs, *a, **k):
            r = real(self, key, plans, specs, *a, **k)
            for (f, p), sp in zip(plans, specs):
                got.setdefault(str(key), []).append((str(f), np.asarray(p.xy, np.float64)[:, :2],
                                                     np.asarray(p.z, np.float64), sp))
            return r
        with unittest.mock.patch.object(grading, "SHAPES_ON", True), \
                unittest.mock.patch.object(grading._Planner, "_road_laws", spy):
            drivability.measure("paris_maillot_1083")
        frame = interchanges.load("paris_maillot_1083", prefix="drive").frame
        e, n, _ = frame.geodetic_to_enu(np.array([NODE[0]]), np.array([NODE[1]]), 0.0)
        cls.at = np.array([float(e[0]), float(n[0])])
        # (the plans through the node, of the cell that has the most of them)
        best = []
        for plans in got.values():
            here = [(f, xy, z, sp) for f, xy, z, sp in plans
                    if len(xy) and float(np.hypot(*(xy - cls.at).T).min()) < 0.5]
            if len(here) > len(best):
                best = here
        cls.plans = best

    def _at_node(self, xy, z):
        return float(z[int(np.argmin(np.hypot(*(xy - self.at).T)))])

    def test_one_height_at_the_node(self):
        self.assertGreaterEqual(len(self.plans), 6, [f[:8] for f, *_r in self.plans])
        zs = [self._at_node(xy, z) for _f, xy, z, _sp in self.plans]
        self.assertLess(max(zs) - min(zs), 0.05, dict((f[:8], round(v, 2)) for (f, *_r), v in zip(self.plans, zs)))

    def test_the_decks_end_and_the_trunk_reach_it_at_their_grade(self):
        for want in (DECK, TRUNK):
            hit = [(xy, z, sp) for f, xy, z, sp in self.plans if f.startswith(want)]
            self.assertTrue(hit, want)
            for xy, z, sp in hit:
                g = roadlaws.laws(sp if sp is not None else types.SimpleNamespace(cls="primary", kmh=None))[0]
                near = np.hypot(*(xy - self.at).T) < 15.0
                ds = np.hypot(*np.diff(xy, axis=0).T)
                k = np.flatnonzero(near[1:] & near[:-1] & (ds > 0.5))
                worst = float((np.abs(np.diff(z))[k] / ds[k]).max()) if len(k) else 0.0
                self.assertLessEqual(worst, g + 0.01, "%s: %.1f %% within 15 m of the node, its law %.1f %%"
                                     % (want, 100 * worst, 100 * g))


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@unittest.skipUnless(os.path.exists(REPLAY), "the Porte Maillot replay")
class RoadLawsHaveAnAnswerTest(unittest.TestCase):
    """The junctions' ramps and the heights the shapes set meet the road laws' own: no network of roads the laws cannot
    hold (infeasible: all its roads left as the earlier passes made them). A cut's ceiling ended 0.16 m over its ground,
    under the floor of the road it landed on in the square's own cell."""

    def test_the_replay_logs_no_infeasible_network(self):
        import drivability
        h = _Errors()
        log = logging.getLogger("earthd.grading")
        log.addHandler(h)
        try:
            with unittest.mock.patch.object(grading, "SHAPES_ON", True):
                drivability.measure("paris_maillot_1083")
        finally:
            log.removeHandler(h)
        bad = [ln for ln in h.lines if "road laws infeasible network" in ln]
        self.assertEqual(bad, [], bad[:1])


if __name__ == "__main__":
    unittest.main()
