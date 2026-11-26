"""A tunnel the shapes solved has its roads' cover from the shapes, not asked again by the road laws (grading's
_over_arches): asked again, off the built floor and the plan's segments, it stood 0.68 m over a road the shape put
over the tube and a deck held down, Porte Maillot's primary, and its network of 96 roads had no answer."""
import logging
import os
import sys
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import grading  # noqa: E402

REPLAY = os.path.join(HERE, "fixtures", "drive_paris_maillot_1083.npz")
CELL = "14/8295/5634"           # (the primary under a deck over the trunk links' tubes)


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@unittest.skipUnless(os.path.exists(REPLAY), "the Porte Maillot replay")
class ShapesTunnelCoverTest(unittest.TestCase):

    def test_the_network_over_the_solved_tubes_has_an_answer(self):
        import drivability
        h = _Errors()
        log = logging.getLogger("earthd.grading")
        log.addHandler(h)
        try:
            with unittest.mock.patch.object(grading, "SHAPES_ON", True):
                drivability.measure("paris_maillot_1083")
        finally:
            log.removeHandler(h)
        bad = [ln for ln in h.lines if "road laws infeasible network in %s:" % CELL in ln]
        self.assertEqual(bad, [], bad[:1])


if __name__ == "__main__":
    unittest.main()
