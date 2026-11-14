"""pitches: the smallest rectangle round a mapped pitch, the sport's lines laid in it, nothing where unsure."""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pitches  # noqa: E402


def rect(cx, cy, L, W, deg=0.0):
    a = math.radians(deg)
    u, v = np.array([math.cos(a), math.sin(a)]), np.array([-math.sin(a), math.cos(a)])
    c = np.array([cx, cy])
    return np.array([c - u * L / 2 - v * W / 2, c + u * L / 2 - v * W / 2, c + u * L / 2 + v * W / 2,
                     c - u * L / 2 + v * W / 2, c - u * L / 2 - v * W / 2])


def pitch(sport, L, W, deg=0.0, ring=None):
    xy = rect(10, 20, L, W, deg) if ring is None else ring
    c, u, l, w, fill = pitches.fit_rect(xy)
    return pitches.Pitch("p", sport, "", xy, c, u, l, w, fill)


class FitTest(unittest.TestCase):
    def test_a_turned_rectangle_is_found(self):
        c, u, L, W, fill = pitches.fit_rect(rect(10, 20, 100, 60, 33))
        self.assertAlmostEqual(L, 100, delta=0.01)
        self.assertAlmostEqual(W, 60, delta=0.01)
        self.assertAlmostEqual(abs(u @ [math.cos(math.radians(33)), math.sin(math.radians(33))]), 1.0, places=4)
        self.assertAlmostEqual(float(c[0]), 10, delta=0.01)
        self.assertGreater(fill, 0.99)

    def test_a_round_blob_does_not_fill_its_rectangle(self):
        t = np.linspace(0, 2 * math.pi, 40)
        self.assertLess(pitches.fit_rect(np.c_[30 * np.cos(t), 30 * np.sin(t)])[4], 0.8)


class LayoutTest(unittest.TestCase):
    def inside_pitch(self, p, lines):
        pts = np.concatenate(lines)
        self.assertLessEqual(float(np.abs(pts[:, 0]).max()), p.length / 2 + 0.01)
        self.assertLessEqual(float(np.abs(pts[:, 1]).max()), p.width / 2 + 0.01 if p.sport != "tennis" else 1e9)

    def test_football_pitch(self):
        p = pitch("soccer", 100, 64, 20)
        lines = pitches.layout(p)
        self.assertGreaterEqual(len(lines), 7)                  # outline, halfway, circle, 2 penalty + 2 goal areas
        self.inside_pitch(p, lines)

    def test_a_small_pitch_gets_no_penalty_areas(self):
        self.assertEqual(len(pitches.layout(pitch("soccer", 30, 20))), 3)

    def test_tennis_court_true_size_in_a_fenced_area(self):
        lines = pitches.layout(pitch("tennis", 36, 18))
        xs = np.concatenate(lines)[:, 0]
        self.assertAlmostEqual(float(xs.max() - xs.min()), 23.77, delta=0.01)
        self.assertEqual(len(lines), 6)

    def test_two_courts_side_by_side(self):
        self.assertEqual(len(pitches.layout(pitch("tennis", 36, 36))), 12)

    def test_basketball_and_volleyball(self):
        for sport, L, W in (("basketball", 28, 15), ("volleyball", 18, 9), ("beach", 16, 8)):
            p = pitch(sport, L, W, 5)
            lines = pitches.layout(p)
            self.assertTrue(lines, sport)
            self.inside_pitch(p, lines)

    def test_wrong_sizes_and_non_rectangles_get_nothing(self):
        self.assertIsNone(pitches.layout(pitch("soccer", 300, 200)))
        self.assertIsNone(pitches.layout(pitch("basketball", 60, 15)))
        self.assertIsNone(pitches.layout(pitch("tennis", 10, 5)))
        self.assertIsNone(pitches.layout(pitch("golf", 100, 60)))
        t = np.linspace(0, 2 * math.pi, 40)
        self.assertIsNone(pitches.layout(pitch("soccer", 0, 0, ring=np.c_[30 * np.cos(t), 20 * np.sin(t)])))


class SportTest(unittest.TestCase):
    def test_the_osm_tags(self):
        self.assertEqual(pitches.sport_of('{"leisure":"pitch","sport":"soccer"}'), "soccer")
        self.assertEqual(pitches.sport_of('{"sport":"beachvolleyball"}'), "beach")
        self.assertIsNone(pitches.sport_of('{"sport":"soccer;basketball"}'))     # shared: no lines
        self.assertIsNone(pitches.sport_of('{"leisure":"pitch"}'))
        self.assertIsNone(pitches.sport_of('{"sport":"boules"}'))
        self.assertIsNone(pitches.sport_of("not json"))


class SegmentsTest(unittest.TestCase):
    def test_world_segments_and_a_hall_left_out(self):
        p = pitch("tennis", 24, 11, 90)
        segs = pitches.segments([p])
        self.assertEqual(len(segs), 9)
        xs = [s[0][0] for s in segs] + [s[1][0] for s in segs]
        ys = [s[0][1] for s in segs] + [s[1][1] for s in segs]
        self.assertAlmostEqual(max(ys) - min(ys), 23.77, delta=0.05)           # turned 90 degrees: along y
        self.assertAlmostEqual(max(xs) - min(xs), 10.97, delta=0.05)
        hall = rect(10, 20, 40, 30)
        self.assertEqual(pitches.segments([p], [hall]), [])


class _Feats:
    def __init__(self, rows):
        n = len(rows)
        self.kind = np.full(n, pitches.mapgeom.POLYGON, np.uint8)
        self.attrs = {"class": np.array(["pitch"] * n, object), "surface": np.array([r[0] for r in rows], object),
                      "tags": np.array([r[1] for r in rows], object)}
        self.n = n

    def __len__(self):
        return self.n


class TurfTest(unittest.TestCase):
    """Mown turf: grass pitches get the fairway layer (no 3D grass through their lines), hard and sand pitches do not."""

    def test_which_pitches_are_turf(self):
        rows = [("recreation_grass", '{"sport":"soccer","surface":"grass"}'),     # grass
                ("", '{"sport":"soccer"}'),                                       # a football pitch, nothing known
                ("recreation_paved", '{"sport":"basketball"}'),                   # paved
                ("recreation_sand", '{"sport":"soccer","surface":"gravel"}'),     # gravel
                ("", '{"sport":"tennis"}'),                                       # unknown, not football
                ("", '{"sport":"soccer","surface":"tartan"}'),                    # tartan
                ("recreation_grass", '{"surface":"artificial_turf"}')]
        self.assertEqual(list(pitches.turf_mask(_Feats(rows))), [True, True, False, False, False, False, True])

    def test_off_when_disabled(self):
        from unittest import mock
        with mock.patch.object(pitches, "ON", False):
            self.assertIsNone(pitches.turf_mask(_Feats([("recreation_grass", "{}")])))


if __name__ == "__main__":
    unittest.main()
