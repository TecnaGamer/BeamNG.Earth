"""streetsigns.py: street-name blades on the signals' mast arms."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import roads as roadsmod  # noqa: E402
import streetsigns  # noqa: E402
import signmesh  # noqa: E402
import signstream  # noqa: E402


class _Feats:
    """A roads layer of one map cell, straight in frame metres (mapgeom.Features' calls Roads makes)."""

    def __init__(self, lines, names, classes=None, ranges=None):
        self.lines = [np.asarray(line, np.float64) for line in lines]
        self.ids = np.array([f"r{i}" for i in range(len(lines))])
        self.attrs = {"name": np.array(names, object),
                      "class": np.array(classes or ["residential"] * len(lines), object)}
        self._tag_ranges = ranges or [[] for _ in lines]       # (roads.tag_level_ranges reads it from here)

    def __len__(self):
        return len(self.lines)

    def xy_in(self, frame):
        return None

    def parts(self, i, xy=None):
        return [[self.lines[i]]]

    def boxes(self, frame):
        return np.array([[*line.min(0), *line.max(0)] for line in self.lines])


def _roads(lines, names, box=None, **kw):
    return streetsigns.Roads(None, [_Feats(lines, names, **kw)], box)


# North Main Street along x = 0, Elm Street along y = 20
US = streetsigns.style("US")
ROADS = _roads([[(0, -200), (0, 200)], [(-200, 20), (200, 20)]], ["North Main Street", "Elm Street"])


class CrossStreetTest(unittest.TestCase):
    def test_the_blade_names_the_street_crossed(self):
        # northbound on Main (travel +y): the pole on the drivers' right, the model's +x east: yaw 0
        self.assertEqual(streetsigns.cross_street(ROADS, 8, 8, 0.0), "Elm Street")
        # eastbound on Elm (travel +x): the drivers' right is south, yaw 270
        self.assertEqual(streetsigns.cross_street(ROADS, -10, 12, 270.0), "North Main Street")

    def test_no_blade_without_a_named_cross_street(self):
        roads = _roads([[(0, -200), (0, 200)], [(-200, 20), (200, 20)]], ["North Main Street", ""])
        self.assertEqual(streetsigns.cross_street(roads, 8, 8, 0.0), "")

    def test_no_blade_naming_the_approachs_own_street(self):
        roads = _roads([[(0, -200), (0, 200)], [(-200, 20), (200, 20)]], ["Main Street", "Main Street"])
        self.assertEqual(streetsigns.cross_street(roads, 8, 8, 0.0), "")


class RoadsTest(unittest.TestCase):
    BOX = (-100, -100, 100, 100)

    def test_a_long_road_starting_far_off_is_kept(self):
        """Its first point 5 km away: only its bounding box meets the tile's."""
        roads = _roads([[(-5000, 20), (5000, 20)], [(3000, 3000), (3100, 3000)]], ["Elm Street", "Far Street"],
                       box=self.BOX)
        self.assertEqual(roads.names, ["Elm Street"])

    def test_paths_are_no_cross_street(self):
        roads = _roads([[(0, -200), (0, 200)], [(-200, 20), (200, 20)]], ["Main Street", "Elm Path"],
                       classes=["residential", "footway"])
        self.assertEqual(roads.names, ["Main Street"])

    def test_the_freeway_over_the_junction_is_no_cross_street(self):
        """A ramp's signals under a freeway bridge (the ramp unnamed): no blade naming the freeway."""
        roads = _roads([[(0, -200), (0, 200)], [(-200, 20), (-60, 20), (60, 20), (200, 20)]],
                       ["Main Street", "Interstate 95"], ranges=[[], [(0.35, 0.65, "bridge")]])     # (a 120 m deck)
        self.assertEqual(streetsigns.cross_street(roads, 8, 8, 0.0), "")
        self.assertEqual(len(roads.A), 3)                 # (Main Street, and the freeway's ground ends)


class BladeTest(unittest.TestCase):
    def test_the_blade_hangs_on_the_arm_over_the_lanes_facing_the_drivers(self):
        m = signmesh.Mesh((0, 0, 0))
        w = streetsigns.blade(m, 8.0, 8.0, 100.0, 0.0, "ELM ST", US)
        p = np.asarray(m.p)
        # the arm reaches west (local -x) over the road from the pole at x = 8
        self.assertAlmostEqual(p[:, 0].max(), 8.0 - streetsigns.ARM_REACH, delta=0.05)
        self.assertAlmostEqual(p[:, 0].min(), 8.0 - streetsigns.ARM_REACH - w, delta=0.05)
        # in front of the arm, toward the drivers coming north (-y)
        self.assertLess(p[:, 1].max(), 8.01)
        self.assertGreater(p[:, 1].min(), 8.0 - streetsigns.ARM_FRONT - 0.1)
        # level, its middle at the rising arm's height over the pole's base
        face = p[p[:, 1] < 8.0 - streetsigns.ARM_FRONT + 0.01]
        mid = (face[:, 2].min() + face[:, 2].max()) / 2
        self.assertAlmostEqual(mid, 100.0 + streetsigns.arm_z(streetsigns.ARM_REACH + w / 2), delta=0.05)

    def test_the_arm_rises_from_the_pole_then_runs_level(self):
        self.assertAlmostEqual(streetsigns.arm_z(0.0), 4.8)
        self.assertAlmostEqual(streetsigns.arm_z(3.7), 6.07)
        self.assertAlmostEqual(streetsigns.arm_z(5.0), streetsigns.arm_z(4.3))
        self.assertLess(streetsigns.arm_z(1.0), streetsigns.arm_z(2.0))

    def test_a_long_name_is_capped(self):
        m = signmesh.Mesh((0, 0, 0))
        w = streetsigns.blade(m, 0, 0, 0, 0, "MARTIN LUTHER KING JUNIOR MEMORIAL BOULEVARD", US)
        self.assertLessEqual(w, US["width_m"][1])


class StyleTest(unittest.TestCase):
    def test_every_style_is_complete_and_drawable(self):
        table = streetsigns._table()
        for code, st in table["countries"].items():
            self.assertEqual(code, code.upper())
            self.assertIn(st["mount"], streetsigns.MOUNTS, code)
            self.assertIn(st["bg"], signmesh.COLOUR, code)
            self.assertIn(st["border"], signmesh.COLOUR, code)
            self.assertIn(st["ink"], ("white", "black"), code)
            self.assertIn(st["case"], ("upper", "as_mapped"), code)
            self.assertIn(st["abbrev"], table["abbrev"], code)
            self.assertGreater(st["height_m"], 0.0, code)
            self.assertGreater(st["text_m"], 0.0, code)
            self.assertLessEqual(st["width_m"][0], st["width_m"][1], code)

    def test_a_country_without_a_style_has_no_signs(self):
        self.assertIsNone(streetsigns.style("FR"))
        self.assertIsNone(streetsigns.style(None))
        self.assertEqual(streetsigns.style("us"), US)

    def test_the_us_short_form(self):
        self.assertEqual(streetsigns.short_name("East Fayette Street", US), "E FAYETTE ST")
        self.assertEqual(streetsigns.short_name("South Broadway", US), "S BROADWAY")
        self.assertEqual(streetsigns.short_name("Martin Luther King Jr Boulevard", US), "MARTIN LUTHER KING JR BLVD")
        self.assertEqual(streetsigns.short_name("Main Street North", US), "MAIN ST N")
        self.assertEqual(streetsigns.short_name("Broadway East", US), "BROADWAY E")
        self.assertEqual(streetsigns.short_name("North Stemmons Freeway", US), "N STEMMONS FWY")
        self.assertEqual(streetsigns.short_name("North", US), "NORTH")

    def test_a_direction_that_is_the_name_is_kept(self):
        self.assertEqual(streetsigns.short_name("North Street", US), "NORTH ST")
        self.assertEqual(streetsigns.short_name("North Avenue", US), "NORTH AVE")
        self.assertEqual(streetsigns.short_name("East West Highway", US), "EAST WEST HWY")

    def test_no_border_data_fetched_while_only_the_us_has_a_style(self):
        import countrybounds
        frame = geodesy.EnuFrame(48.85, 2.35)               # (Paris)
        with mock.patch.object(countrybounds, "bundled") as bundled:
            self.assertEqual(streetsigns.countries_at(frame, [(0.0, 0.0)]), [None])
            bundled.assert_not_called()

    def test_a_mixed_case_style_keeps_the_mapped_case(self):
        st = dict(US, case="as_mapped", abbrev="none")
        self.assertEqual(streetsigns.short_name("Rue de Rivoli", st), "Rue de Rivoli")


class MountTest(unittest.TestCase):
    class _Tile:
        box = (0, 0, 100, 100)
        roads = ROADS

        def __init__(self, signals, styles):
            self.signals, self._styles = signals, styles

        def styles_at(self, xy):
            return self._styles[:len(xy)]

    def test_a_blade_only_where_the_country_has_a_style(self):
        m = signmesh.Mesh((0, 0, 0))
        streetsigns.signal_arm_signs(m, self._Tile([(8, 8, 0, 0.0)], [US]))
        self.assertTrue(m.t)
        m = signmesh.Mesh((0, 0, 0))
        streetsigns.signal_arm_signs(m, self._Tile([(8, 8, 0, 0.0)], [None]))
        self.assertFalse(m.t)

    def test_only_the_signals_inside_the_tile(self):
        m = signmesh.Mesh((0, 0, 0))
        streetsigns.signal_arm_signs(m, self._Tile([(-8, 8, 0, 0.0)], [US]))
        self.assertFalse(m.t)


class SignalsTest(unittest.TestCase):
    FRAME = geodesy.EnuFrame(40.0, -75.0)

    def _write(self, path, rows, frame=None):
        d = {"props": {"groups": ["pole", "signal"], "items": [[1, *r] for r in rows] + [[0, 5.0, 5.0, 0.0, 0.0]]}}
        if frame:
            d["frame"] = frame
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_one_signal_in_two_road_tiles_is_one(self):
        """Read from two files a hair apart, across a rounding step and across north; another approach's stays."""
        with tempfile.TemporaryDirectory() as out:
            self._write(os.path.join(out, "roads_1_0_0.json"), [(12.49, 40.0, 0.0, 359.9), (12.5, 40.0, 0.0, 90.0)])
            self._write(os.path.join(out, "roads_1_1_0.json"), [(12.51, 40.0, 0.0, 0.1)])
            got = streetsigns.signals_near(out, 1, self.FRAME, 0, 0, roadsmod.tile_bbox(0, 0))
        self.assertEqual(sorted(round(r[3]) for r in got), [90, 360])

    def test_a_geotiles_signals_come_into_the_tiles_frame(self):
        """A geotile's rows are in its own frame: 0.001 deg north of the anchor's is ~111 m north, facing as before."""
        with tempfile.TemporaryDirectory() as out:
            path = os.path.join(out, "rg_x.json")
            self._write(path, [(0.0, 0.0, 10.0, 30.0)], frame=[40.001, -75.0])
            (x, y, z, yaw), = streetsigns._signal_rows(path, self.FRAME)
        self.assertAlmostEqual(x, 0.0, delta=0.5)
        self.assertAlmostEqual(y, 111.0, delta=1.0)
        self.assertAlmostEqual(yaw, 30.0, delta=0.1)

    def test_no_file_no_signals(self):
        self.assertIsNone(streetsigns._signal_rows("/nonexistent/roads_1_0_0.json", self.FRAME))


class SignTileTest(unittest.TestCase):
    def test_the_street_signs_are_a_sign_tile_field(self):
        self.assertIn("streets", signstream.KINDS)

    def test_a_tile_without_a_style_reads_no_road_tile(self):
        def no_reads(*a):
            raise AssertionError("road tiles read")
        with mock.patch.object(streetsigns, "styles_at", lambda frame, xy, world=False: [None] * len(xy)), \
                mock.patch.object(streetsigns, "signals_near", no_reads):
            self.assertIsNone(streetsigns.build_tile(None, 1, 0, 0, None, "/nonexistent"))

    def test_the_blades_are_written_as_the_tiles_streets_shape(self):
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(roadsmod, "tile_bbox", lambda tx, ty: (-100.0, -100.0, 100.0, 100.0)), \
                mock.patch.object(streetsigns, "styles_at", lambda frame, xy, world=False: [US] * len(xy)), \
                mock.patch.object(streetsigns, "signals_near", lambda *a: [(8.0, 8.0, 0.0, 0.0)]), \
                mock.patch.object(streetsigns.Tile, "roads", ROADS):
            mesh = streetsigns.build_tile(None, 1, 0, 0, None, out)
            self.assertEqual(len(mesh), 5)
            path, n = signstream.write_tile(out, "vfs", 1, 0, 0, (None,) * 7 + (mesh,))
            self.assertEqual(n, 1)
            self.assertTrue(os.path.isfile(os.path.join(out, "streets_1_0_0.cdae")))


if __name__ == "__main__":
    unittest.main()
