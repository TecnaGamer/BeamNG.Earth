"""Squares and pedestrian areas as a paved mesh (plazas.py; the user's report #45, 2026-10-17)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import plazas  # noqa: E402


def _area(e):
    a, b = e[:, 1, :2] - e[:, 0, :2], e[:, 2, :2] - e[:, 0, :2]
    return 0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum()

FRAME = geodesy.EnuFrame(39.2817, -76.5929)


def ring(pts):
    la, lo, _ = FRAME.enu_to_geodetic(np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float),
                                      np.zeros(len(pts)))
    return np.c_[lo, la]


class PavingTest(unittest.TestCase):
    """GitHub #70: Paris's squares and the Louvre's courtyards all red brick (and their holes paved over)."""

    def test_by_the_mapped_surface(self):
        self.assertEqual(plazas.paving("asphalt", 48.86, 2.32)[0], plazas.ASPHALT[0])
        self.assertEqual(plazas.paving("sett", 48.86, 2.32)[0], plazas.STONE[0])
        self.assertIsNone(plazas.paving("compacted", 48.86, 2.32), "the Tuileries' sand: the terrain's photo")
        self.assertEqual(plazas.paving(None, 48.86, 2.32)[0], plazas.PAVERS[0], "not mapped, in Europe: pavers")
        self.assertEqual(plazas.paving(None, 39.28, -76.59), plazas.US_AMBIGUOUS, "... in the US: by the photo")
        self.assertEqual(plazas.paving("paving_stones", 39.28, -76.59), plazas.US_AMBIGUOUS, "Fells Point's squares")
        self.assertEqual(plazas.paving("bricks", 40.71, -74.01)[0], plazas.BRICK, "mapped brick stays brick")
        self.assertEqual(plazas.paving("paving_stones", 48.86, 2.32)[0], plazas.PAVERS[0], "flat pavers, not cobbles")

    def test_holes_left_out(self):
        outer = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        hole = ring([(20, 20), (30, 20), (30, 30), (20, 30), (20, 20)])
        flat = lambda la, lo: np.full(np.shape(la), 5.0)    # noqa: E731
        m = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [(outer, [hole], "asphalt")], flat, (0.0, 0.0, 0.0))
        P, T, UV, mats, M = m
        self.assertEqual(mats, [plazas.ASPHALT[0]])
        self.assertAlmostEqual(_area(P[T]), 50 * 30 - 10 * 10, delta=30.0)
        self.assertIsNone(plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [(outer, [], "gravel")], flat, (0.0, 0.0, 0.0)))


class PlazaMeshTest(unittest.TestCase):
    def test_us_squares_by_the_photo(self):
        """A US square mapped paving_stones is red brick where the orthophoto shows brick red (Fells Point), stone where
        it shows grey or tan (the World Trade Center's granite, warm in the NYS photo); with no photo, grey pavers."""
        sq = (ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)]), [], "paving_stones")
        flat = lambda la, lo: np.full(np.shape(la), 5.0)     # noqa: E731

        class Photo:
            def __init__(self, rgb):
                self.rgb = rgb

            def colours(self, rings):
                return np.array([self.rgb] * len(rings), np.float64)
        box, o = (0.0, 0.0, 256.0, 256.0), (0.0, 0.0, 0.0)
        self.assertEqual(plazas.mesh(FRAME, box, [sq], flat, o, photo=Photo((0.62, 0.42, 0.36)))[3], [plazas.BRICK])
        self.assertEqual(plazas.mesh(FRAME, box, [sq], flat, o, photo=Photo((0.55, 0.54, 0.52)))[3],
                         [plazas.PAVERS[0]])
        self.assertEqual(plazas.mesh(FRAME, box, [sq], flat, o, photo=Photo((0.68, 0.58, 0.40)))[3],
                         [plazas.PAVERS[0]], "the WTC's granite, warm tan in the photo")
        self.assertEqual(plazas.mesh(FRAME, box, [sq], flat, o)[3], [plazas.PAVERS[0]], "no photo: grey pavers")

    def test_paved_and_draped(self):
        sq = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        elev = lambda la, lo: 5.0 + (np.asarray(lo) - (-76.5929)) * 10000.0    # noqa: E731 (a slope east)
        m = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [sq], elev, (0.0, 0.0, 0.0))
        self.assertIsNotNone(m)
        P, T, UV, mats, M = m
        self.assertEqual(mats, [plazas.BRICK])
        e = P[T]
        edges = np.hypot(*(e[:, [1, 2, 0], :2] - e[:, :, :2]).transpose(2, 0, 1))
        self.assertLessEqual(float(edges.max()), plazas.STEP_M + 1e-6, "split to follow the ground")
        area = _area(e)
        self.assertAlmostEqual(area, 50 * 30, delta=1.0, msg="the whole square, once")
        n = np.cross(e[:, 1] - e[:, 0], e[:, 2] - e[:, 0])
        self.assertTrue((n[:, 2] > 0).all(), "faces up")
        self.assertGreater(P[:, 2].max() - P[:, 2].min(), 1.0, "draped on the slope")

    def test_block_looks_kept_once_read(self):
        """blockworker._us_square_looks (the side agent's note, 2026-11-11: the photo reads slowed the blocks): a read
        square's answer is kept for the process; one the photo could not read (not fetched yet) is read again later."""
        import buildings
        import blockworker

        class Red:
            def colours(self, rings):
                return np.array([(0.62, 0.42, 0.36)] * len(rings))
        sq = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        calls = []
        orig = buildings.roof_photo
        blockworker._us_looks_kept.clear()
        try:
            buildings.roof_photo = lambda *a, **k: calls.append(1)        # (None: nothing on disk)
            looks = blockworker._us_square_looks(FRAME)
            self.assertEqual(looks([sq]), [plazas.PAVERS], "unread: grey")
            self.assertEqual(looks([sq]), [plazas.PAVERS])
            self.assertEqual(len(calls), 2, "unread: asked again")
            buildings.roof_photo = lambda *a, **k: calls.append(1) or Red()
            self.assertEqual(looks([sq]), [(plazas.BRICK, plazas.BRICK_REPEAT_M)])
            self.assertEqual(looks([sq]), [(plazas.BRICK, plazas.BRICK_REPEAT_M)])
            self.assertEqual(len(calls), 3, "read once, then kept")
        finally:
            buildings.roof_photo = orig
            blockworker._us_looks_kept.clear()

    def test_off_the_humps(self):
        """The user, 2026-11-11 at the WTC ("the texture mesh is very broken"): the elevation's hump where a building
        stands (a pyramid 8 m tall) is left to the terrain, not paved up and down in streaks."""
        sq = ring([(10, 10), (60, 10), (60, 40), (10, 40), (10, 10)])
        cx, cy = 35.0, 25.0

        def elev(la, lo):
            x, y, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.0)
            r = np.maximum(np.abs(np.asarray(x) - cx), np.abs(np.asarray(y) - cy))
            return np.clip(8.0 - 1.6 * r, 0.0, None)
        P, T, UV, mats, M = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [sq], elev, (0.0, 0.0, 0.0))
        e = P[T]
        n = np.cross(e[:, 1] - e[:, 0], e[:, 2] - e[:, 0])
        self.assertTrue((np.hypot(n[:, 0], n[:, 1]) <= plazas.MAX_SLOPE * n[:, 2] + 1e-9).all(), "no steep face")
        self.assertGreater(_area(e), 50 * 30 - 12 * 12, "the rest paved")

    def test_split_between_tiles(self):
        sq = ring([(-20, 10), (20, 10), (20, 30), (-20, 30)])
        flat = lambda la, lo: np.zeros(np.shape(la))    # noqa: E731
        a = plazas.mesh(FRAME, (-256.0, 0.0, 0.0, 256.0), [sq], flat, (0.0, 0.0, 0.0))
        b = plazas.mesh(FRAME, (0.0, 0.0, 256.0, 256.0), [sq], flat, (0.0, 0.0, 0.0))

        def area(m):
            return _area(m[0][m[1]])
        self.assertAlmostEqual(area(a) + area(b), 40 * 20, delta=1.0)

    def test_overlap_paved_once(self):
        """GitHub #127 (Federation Square): a smaller area inside a larger one is paved once, by the smaller."""
        big = ring([(-40, 10), (40, 10), (40, 70), (-40, 70)])
        small = (ring([(-10, 30), (10, 30), (10, 50), (-10, 50)]), [], "asphalt")
        flat = lambda la, lo: np.zeros(np.shape(la))    # noqa: E731
        m = plazas.mesh(FRAME, (-256.0, 0.0, 256.0, 256.0), [big, small], flat, (0.0, 0.0, 0.0))
        P, T, UV, mats, M = m
        # (by triangle centres on the 2 m split: a few m2 along the smaller one's edge)
        self.assertAlmostEqual(_area(P[T]), 80 * 60, delta=20.0, msg="the ground once, not 80*60 + 20*20")
        stone = M == mats.index(plazas.ASPHALT[0])
        self.assertAlmostEqual(_area(P[T[stone]]), 20 * 20, delta=2.0, msg="the smaller area keeps its paving")

    def test_ground_under_the_road_mesh_left_out(self):
        """GitHub #127: a pedestrian street's road mesh over a square: the square leaves that ground to it."""
        sq = ring([(-40, 10), (40, 10), (40, 70), (-40, 70)])
        flat = lambda la, lo: np.zeros(np.shape(la))    # noqa: E731
        # a 10 m wide street across the square (two triangles), in the frame
        cover = np.array([[-50, 35, 50, 35, 50, 45], [-50, 35, 50, 45, -50, 45]], float)
        m = plazas.mesh(FRAME, (-256.0, 0.0, 256.0, 256.0), [sq], flat, (0.0, 0.0, 0.0), cover)
        self.assertAlmostEqual(_area(m[0][m[1]]), 80 * 60 - 80 * 10, delta=40.0, msg="the street's ground left out")


if __name__ == "__main__":
    unittest.main()
