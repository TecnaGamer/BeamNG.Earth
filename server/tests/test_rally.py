"""rally: stage roads joined into one path, pacenotes from the road's shape."""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rally  # noqa: E402


def straight(x0, y0, hd, n, step=5.0):
    return [(x0 + math.cos(hd) * step * i, y0 + math.sin(hd) * step * i) for i in range(n + 1)]


def road(*parts):
    """parts: ("s", metres) straight or ("t", radius, degrees +left) turn -> points."""
    pts, hd = [(0.0, 0.0)], 0.0
    for p in parts:
        x, y = pts[-1]
        if p[0] == "s":
            pts += straight(x, y, hd, int(p[1] / 5))[1:]
        else:
            r, deg = p[1], p[2]
            n = max(4, int(abs(math.radians(deg)) * r / 2))
            for i in range(1, n + 1):
                a = hd + math.radians(deg) * i / n
                pa = hd + math.radians(deg) * (i - 1) / n
                ch = 2 * r * math.sin(abs(math.radians(deg)) / n / 2)
                mid = (a + pa) / 2
                x, y = x + ch * math.cos(mid), y + ch * math.sin(mid)
                pts.append((x, y))
            hd += math.radians(deg)
    return pts


class StagePathTest(unittest.TestCase):
    def test_joins_pieces_and_takes_the_long_way(self):
        a = straight(0, 0, 0, 100)                      # 500 m east
        b = [(500.0 + 1.0, 0.5)] + straight(501, 0.5, math.pi / 2, 200)[1:]     # 1 km north (ends 1 m apart)
        spur = straight(250, 0, -math.pi / 2, 10)      # a 50 m spur
        path = rally.stage_path([a, spur, b], 5000.0, start=(-20.0, 0.0))
        self.assertAlmostEqual(rally.length(path), 1500.0, delta=5)
        self.assertEqual(path[0], (0.0, 0.0))

    def test_stops_near_the_target_length(self):
        path = rally.stage_path([straight(0, 0, 0, 2000)], 3000.0)
        self.assertLessEqual(rally.length(path), 3000 * 1.15 + 1)
        self.assertGreater(rally.length(path), 3000 * 1.1)

    def test_empty(self):
        self.assertEqual(rally.stage_path([], 1000.0), [])


class PacenoteTest(unittest.TestCase):
    def notes(self, *parts):
        return rally.pacenotes(road(*parts))

    def test_grades_and_directions(self):
        n = self.notes(("s", 300), ("t", 200, 40), ("s", 300), ("t", 50, -60), ("s", 300), ("t", 12, 170), ("s", 300))
        self.assertEqual([x["dir"] for x in n], ["left", "right", "left"])
        self.assertEqual([x["grade"] for x in n], [5, 2, 0])
        self.assertIn("hairpin", n[2]["text"])
        self.assertTrue(n[0]["text"].startswith("Long left 5"), n[0]["text"])
        self.assertTrue(n[-1]["text"].endswith("to finish"))
        self.assertLess(n[0]["at"], n[1]["at"])
        self.assertAlmostEqual(n[0]["at"], 300, delta=15)

    def test_gap_calls(self):
        n = self.notes(("s", 100), ("t", 60, 50), ("s", 10), ("t", 60, -50), ("s", 150), ("t", 60, 50), ("s", 100))
        self.assertTrue(n[0]["text"].endswith("into"), n[0]["text"])
        self.assertTrue(n[1]["text"].endswith("150"), n[1]["text"])
        self.assertEqual((n[0]["link"], n[1]["link"], n[2]["link"]), ("into", 150, None))
        self.assertIn(n[0]["name"], ("2", "3"))
        self.assertEqual((n[0]["dir"], n[0]["long"]), ("left", False))

    def test_straight_has_no_notes(self):
        self.assertEqual(rally.pacenotes(straight(0, 0, 0.3, 200)), [])


if __name__ == "__main__":
    unittest.main()


class BuildTest(unittest.TestCase):
    """build() on map data: the named road only, as lat/lon, with its notes."""

    def test_build_from_map_data(self):
        import mapgeom as mg
        from test_gasstations import FRAME, FakeMD, feats
        la, lo, _ = FRAME.enu_to_geodetic(0.0, 0.0, 0.0)
        asaph = road(("s", 600), ("t", 40, 80), ("s", 800))
        rows = [("w1", asaph[:80], {"name": "Asaph Road", "class": "track"}),
                ("w2", asaph[79:], {"name": "Right Asaph Road", "class": "track"}),
                ("w3", [(0, 0), (0, -1500)], {"name": "Main Street", "class": "primary"})]
        md = FakeMD({"roads": feats("roads", rows, mg.LINE)})
        st = {"id": "t", "name": "Test", "event": "x", "area": "here", "lat": float(la), "lon": float(lo),
              "names": ["asaph"], "miles": 2.0, "surface": "gravel"}
        old = rally.STAGES
        rally.STAGES = [st]
        try:
            got = rally.build(md, "t")
        finally:
            rally.STAGES = old
        self.assertAlmostEqual(got["dist"], rally.length(asaph), delta=10)
        self.assertEqual(len(got["notes"]), 1)
        self.assertIn("left", got["notes"][0]["text"].lower())
        self.assertAlmostEqual(got["points"][0][0], float(la), places=4)
        with self.assertRaises(ValueError):
            rally.build(md, "nope")
