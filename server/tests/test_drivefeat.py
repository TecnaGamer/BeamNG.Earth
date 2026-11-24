"""The mapped driving features (drivefeat.py, calming.py, roadends.py, drivetags.py; BNE_CALMING, BNE_UNPAVED,
BNE_ROAD_BARRIERS, BNE_ROAD_ENDS, BNE_NODE_MARKINGS, BNE_RAIL_PROPS, BNE_LAMP_TAGS): the rules each switch adds, the
shards' reading and the profile kernel. Every switch off by default: nothing read, nothing keyed."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools"))

import fast  # noqa: E402
import calming  # noqa: E402
import drivefeat  # noqa: E402
import drivetags  # noqa: E402
import railtags  # noqa: E402

HAVE = getattr(fast, "_ef", None) is not None and int(getattr(fast._ef, "VERSION", 0)) >= 612 \
    and hasattr(fast._ef, "line_feet")
Spec = namedtuple("Spec", "width kind oneway dir lanes cls")


class DefaultsTest(unittest.TestCase):
    def test_every_switch_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if k not in drivefeat.SWITCHES}
        with mock.patch.dict(os.environ, env, clear=True):
            import importlib
            m = importlib.reload(drivefeat)
            try:
                self.assertFalse(any((m.CALMING, m.UNPAVED, m.BARRIERS, m.ENDS, m.MARKINGS, m.RAIL_PROPS,
                                      m.LAMP_TAGS, m.SHARDS)))
                self.assertIsNone(m.identity(object()))         # (no "drive" entry in MapData.versions())
            finally:
                importlib.reload(drivefeat)

    def test_border_data_keyed_only_while_on(self):
        class MD:
            drive_tags = None
        with mock.patch("countrybounds.signature", lambda: "borders:a"):
            for flag in ("CALMING", "ENDS", "MARKINGS"):
                with mock.patch.object(drivefeat, flag, True), mock.patch.object(drivefeat, "COUNTRY", True):
                    a = drivefeat.identity(MD())
                    with mock.patch("countrybounds.signature", lambda: "borders:b"):
                        b = drivefeat.identity(MD())
                    self.assertNotEqual(a, b, flag)
                    self.assertEqual(a[2], "borders:a")
            off = {f: False for f in ("CALMING", "UNPAVED", "ENDS", "MARKINGS", "RAIL_PROPS", "SHARDS", "COUNTRY")}
            with mock.patch.multiple(drivefeat, **off):
                self.assertIsNone(drivefeat.identity(MD()))
                with mock.patch("countrybounds.signature", lambda: "borders:b"):
                    self.assertIsNone(drivefeat.identity(MD()))

    def test_switches_are_build_switches(self):
        import revisit
        for k in drivefeat.SWITCHES:
            self.assertTrue(k.startswith("BNE_") and k not in revisit.NOT_SWITCHES, k)
            self.assertNotIn(k, revisit.PLAN_NEUTRAL, k)


class CalmingRulesTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(calming.kind_of({"traffic_calming": "hump"}), "hump")
        self.assertEqual(calming.kind_of({"traffic_calming": "choker;table"}), "table")
        self.assertIsNone(calming.kind_of({"traffic_calming": "chicane"}))
        self.assertIsNone(calming.kind_of({"traffic_calming": "rumble_strip"}))

    def test_profiles(self):
        h, ramp, top = calming.profile_of({"traffic_calming": "hump"}, "GB", 2.0)
        self.assertAlmostEqual(2 * ramp + top, 3.7)            # (LTN 1/07's round-top hump)
        self.assertAlmostEqual(h, 0.075)
        hn, _r, _t = calming.profile_of({"traffic_calming": "table"}, "NL", 2.0)
        self.assertAlmostEqual(hn, 0.08 * 4 / 3)
        hb, rb, tb = calming.profile_of({"traffic_calming": "bump"}, "US", 2.0)
        self.assertGreaterEqual(2 * rb + tb, 2.0 - 1e-9)       # (two lattice squares: always sampled)
        self.assertAlmostEqual(calming.profile_of({"traffic_calming": "hump", "height": "10 cm"}, "GB", 2.0)[0], 0.10)
        self.assertAlmostEqual(calming.profile_of({"traffic_calming": "hump", "height": "3 in"}, "US", 2.0)[0], 0.0762)
        self.assertAlmostEqual(calming.profile_of({"traffic_calming": "hump", "height": "1 m"}, "US", 2.0)[0],
                               calming.H_MAX)
        self.assertIsNone(calming.profile_of({"traffic_calming": "island"}, "GB", 2.0))


@unittest.skipUnless(HAVE, "earthfast 612")
class CalmingKernelTest(unittest.TestCase):
    def _plan(self):
        import grading
        xy = np.c_[np.arange(0.0, 41.0, 4.0), np.zeros(11)]
        p = grading.Plan(xy, 0.02 * xy[:, 0], 3.0)
        p.bank = np.zeros(11)
        p.cut = np.full((10, 6), np.nan)
        return p

    def test_hump_in_the_plan(self):
        p = self._plan()
        feats = np.array([[18.0, 0.4, 0.075, 1.85, 0.0]])
        self.assertEqual(calming.apply_plan(p, feats), 1)
        self.assertEqual(len(p.cut), len(p.xy) - 1)
        self.assertEqual(len(p.bank), len(p.xy))
        i = int(np.argmin(np.abs(p.xy[:, 0] - 18.0)))
        self.assertAlmostEqual(float(p.z[i]), 0.02 * 18.0 + 0.075, places=9)
        far = np.abs(p.xy[:, 0] - 18.0) > 1.85
        self.assertTrue(np.allclose(p.z[far], 0.02 * p.xy[far, 0]))
        self.assertLessEqual(float(np.max(np.diff(p.xy[np.abs(p.xy[:, 0] - 18.0) <= 1.85, 0]))), calming.STEP_M + 1e-9)

    def test_off_the_road_untouched(self):
        p = self._plan()
        before = p.xy.copy(), p.z.copy()
        self.assertEqual(calming.apply_plan(p, np.array([[18.0, 4.0, 0.075, 1.85, 0.0]])), 0)
        self.assertTrue((p.xy == before[0]).all() and (p.z == before[1]).all())

    def test_lattice_sees_a_bump(self):
        # the 1 m lattice's samples (grade_points interpolates the plan linearly) see most of a short bump's height
        p = self._plan()
        prof = calming.profile_of({"traffic_calming": "bump"}, "US", 2.0)
        calming.apply_plan(p, np.array([[20.37, 0.0, *prof]]))
        lat = np.arange(0.0, 40.0, 1.0)
        z = np.interp(lat, p.xy[:, 0], p.z) - 0.02 * lat
        self.assertGreater(float(z.max()), 0.8 * prof[0])

    def test_ride_at_limit(self):
        # a quarter car (the IRI golden car) over a mapped hump at 20 mph keeps its wheel on the road
        import drivefeat_ride
        p = self._plan()
        calming.apply_plan(p, np.array([[18.0, 0.0, 0.075, 1.85, 0.0]]))
        lat = np.arange(0.0, 40.0, 0.25)
        z = np.interp(lat, p.xy[:, 0], p.z)
        peak, least = drivefeat_ride.ride(z, 0.25, 20 * 0.44704)
        self.assertGreater(least, 0.0)


class ShardsTest(unittest.TestCase):
    def test_nodes_sorted_and_boxed(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "63"))
            with open(os.path.join(d, "manifest.json"), "w") as fh:
                json.dump({"format": 1, "built": "b"}, fh)
            doc = {"ways": {}, "nodes": [[7, -1.0, 51.0, {"traffic_calming": "hump"}],
                                         [3, -1.001, 51.0, {"highway": "turning_circle"}]],
                   "rules": {"tracktype": {"grade3": [100, 5]}}}
            import mapcells
            k = mapcells.cells_for_bbox(51.0, -1.0, 51.0, -1.0, z=7)[0]
            os.makedirs(os.path.join(d, str(k.x)), exist_ok=True)
            with gzip.open(os.path.join(d, str(k.x), f"{k.y}.json.gz"), "wt") as fh:
                json.dump(doc, fh)
            t = drivetags.DriveTags(None, None, local=d)
            got = t.nodes(50.99, -1.01, 51.01, -0.99)
            self.assertEqual([g[0] for g in got], [3, 7])
            self.assertEqual(t.nodes(50.99, -0.5, 51.01, -0.4), [])
            self.assertEqual(t.way_values("tracktype", ["100", "105", "106", None], 51.0, -1.0),
                             ["grade3", "grade3", None, None])


class UnpavedTest(unittest.TestCase):
    def test_gain(self):
        class F:
            attrs = {"osm_tracktype": np.array(["grade5", None, "grade1"], object),
                     "osm_smoothness": np.array([None, None, "horrible"], object)}
        with mock.patch.object(drivefeat, "UNPAVED", True):
            self.assertEqual(drivefeat.rough_gain(F, 0), drivefeat.TRACK_GAIN["grade5"])
            self.assertEqual(drivefeat.rough_gain(F, 1), drivefeat.DEFAULT_GAIN)    # (sparse: a default)
            self.assertEqual(drivefeat.rough_gain(F, 2), drivefeat.SMOOTH_GAIN["horrible"])   # (smoothness first)
        with mock.patch.object(drivefeat, "UNPAVED", False):
            self.assertIsNone(drivefeat.rough_gain(F, 0))
        self.assertEqual(drivefeat.rough_resolve(F, 0)[1:], ("S1:tracktype", 3))
        self.assertEqual(drivefeat.rough_resolve(F, 2)[1:], ("S1:smoothness", 3))
        self.assertEqual(drivefeat.rough_resolve(F, 1)[1:], ("S5:default", 0))      # (missing: unknown, a default)

    def test_dirt_bumps_scale(self):
        import grading
        spec = Spec(4.0, "dirt", False, 0, None, "track")
        xy = np.c_[np.arange(0.0, 400.0, 2.0), np.zeros(200)]
        with mock.patch.object(grading, "DIRT_ROUGH", 0.0):
            self.assertIsNone(grading.dirt_bumps(xy, spec, 200))
            soft = grading.dirt_bumps(xy, spec, 200, gain=0.35)
            hard = grading.dirt_bumps(xy, spec, 200, gain=1.5)
        self.assertIsNotNone(soft)
        self.assertGreater(float(np.abs(hard).max()), float(np.abs(soft).max()))

    def test_tracktype_surface(self):
        self.assertEqual(drivetags.TRACKTYPE_SURFACE["grade1"], "compacted")
        import roads
        self.assertEqual(roads.surface_kind("track", drivetags.TRACKTYPE_SURFACE["grade2"]), "gravel")


class BuilderTest(unittest.TestCase):
    def test_node_and_way_rules(self):
        import build_drive_tags as b
        self.assertIsNotNone(b.node_record({"traffic_calming": "hump", "name": "x"}))
        self.assertNotIn("name", b.node_record({"traffic_calming": "hump", "name": "x"}))
        self.assertIsNone(b.node_record({"traffic_calming": "no"}))
        self.assertIsNone(b.node_record({"ford": "no"}))
        self.assertIsNotNone(b.node_record({"railway": "signal", "railway:signal:main": "DE-ESO:hp"}))
        self.assertIsNone(b.node_record({"highway": "crossing"}))           # (Overture's: not written here)
        self.assertEqual(b.norm_way({"highway": "track", "tracktype": "Grade3"}), {"tracktype": "grade3"})
        self.assertEqual(b.norm_way({"highway": "track", "tracktype": "grade9"}), {})
        self.assertEqual(b.norm_way({"tracktype": "grade1"}), {})          # (a highway's only)


class MappedMarksTest(unittest.TestCase):
    def _t(self):
        import roads
        sp = roads.Spec(7.0, "paved", True, "residential", False, 2.0, 0, 50, None, None, 2, "asphalt")
        return [("w", sp, np.c_[np.arange(-100.0, 1.0, 10.0), np.zeros(11), np.zeros(11)], 0.0),
                ("e", sp, np.c_[np.arange(0.0, 101.0, 10.0), np.zeros(11), np.zeros(11)], 0.0),
                ("s", sp, np.c_[np.zeros(11), np.arange(0.0, -101.0, -10.0), np.zeros(11)], 0.0)]

    def test_only_what_is_mapped(self):
        rows = self._t()
        none = {"striped": [], "priority": np.zeros((0, 3)), "stop_lines": np.zeros((0, 2))}
        cw, teeth, _jl, added = drivefeat.mapped_marks(rows, none, [], {}, None, "US", False)
        self.assertEqual((cw, teeth, added), ([], [], []))           # (nothing invented)
        got = {"striped": [(60.0, 0.0)], "priority": np.array([[0.0, -12.0, 0.0]]), "stop_lines": np.zeros((0, 2))}
        cw, teeth, _jl, added = drivefeat.mapped_marks(rows, got, [], {}, None, "US", False)
        self.assertEqual(len(added), 1)
        self.assertTrue(teeth and all(t.shape == (3, 3) for t in teeth))     # (shark's teeth)
        cw, teeth, jl, _a = drivefeat.mapped_marks(rows, got, [], {}, None, "GB", True)
        self.assertTrue(teeth and all(t.shape == (4, 3) for t in teeth))     # (TSRGD 1003's broken lines)
        stop = {"striped": [], "priority": np.array([[0.0, -12.0, 1.0]]), "stop_lines": np.zeros((0, 2))}
        cw, _t, _jl, _a = drivefeat.mapped_marks(rows, stop, [], {}, None, "US", False)
        self.assertEqual(sum(1 for c in cw if c.get("stop")), 1)


class LampTagsTest(unittest.TestCase):
    def test_lamp_tags(self):
        self.assertIsNone(drivefeat.lamp_tags({"lamp_mount": "wall"}))
        g, _h, c = drivefeat.lamp_tags({"light:count": "2"})
        self.assertEqual(g, "streetlight_double_arm")
        self.assertIsNone(c)
        self.assertEqual(drivefeat.lamp_tags({"lamp_type": "LED"})[2], "white")
        self.assertEqual(drivefeat.lamp_tags({"light:colour": "orange", "lamp_type": "led"})[2], None)
        self.assertEqual(drivefeat.lamp_tags({"lamp_mount": "high_mast", "light:count": "4"})[0],
                         "streetlight_high_mast")

    def test_lens_colour(self):
        import furniture
        row = ["streetlight", 0.0, 0.0, 0.0, 0.0, 1.0, 9.0, 0.0, "white"]
        got = furniture.lamp_lenses([row])
        self.assertEqual(got[0][0], "lamplens_white")
        self.assertEqual(furniture.lamp_lenses([row[:8]])[0][0], furniture.G_LENS)


@unittest.skipUnless(HAVE, "earthfast 612")
class TileShapesTest(unittest.TestCase):
    def _roads(self, width=6.0, oneway=False):
        sp = Spec(width, "paved", oneway, 1 if oneway else 0, 2, "residential")
        return [("r1", sp, np.c_[np.arange(0.0, 81.0, 10.0), np.zeros(9), np.zeros(9)], 0.0)]

    def test_hump_triangles_only_where_standard(self):
        nodes = [(40.0, 0.2, {"traffic_calming": "hump"}, 1)]
        with mock.patch.object(drivefeat, "CALMING", True):
            gb = drivefeat.tile_teeth(self._roads(), nodes, "GB", True)
            de = drivefeat.tile_teeth(self._roads(), nodes, "DE", False)
        self.assertTrue(gb)
        self.assertEqual(de, [])
        self.assertTrue(all(p.shape == (3, 3) for p in gb))

    def test_mini_roundabout_disc(self):
        with mock.patch.object(drivefeat, "ENDS", True):
            got = drivefeat.tile_teeth(self._roads(), [(80.0, 0.5, {"highway": "mini_roundabout"}, 1)], "GB", True)
        self.assertEqual(len(got), 1)
        r = float(np.max(np.hypot(got[0][:, 0] - 80.0, got[0][:, 1] - 0.5)))
        self.assertTrue(drivefeat.MINI_R[0] - 1e-6 <= r <= drivefeat.MINI_R[1] + 1e-6)

    def test_kerbs(self):
        with mock.patch.object(drivefeat, "CALMING", True):
            ch = drivefeat.tile_kerbs(self._roads(6.0), [(40.0, 0.0, {"traffic_calming": "choker"}, 1)])
            narrow = drivefeat.tile_kerbs(self._roads(4.0), [(40.0, 0.0, {"traffic_calming": "choker"}, 1)])
            isl = drivefeat.tile_kerbs(self._roads(8.0), [(40.0, 0.0, {"traffic_calming": "island"}, 1)])
        self.assertEqual(len(ch), 2)
        self.assertEqual(narrow, [])
        self.assertEqual(len(isl), 1)
        self.assertTrue(all(o.shape[1] == 3 for o in ch + isl))       # (build_tile's walks: three coordinates)

    def test_barriers_never_close_a_driven_road(self):
        roads = self._roads(6.0)
        pts = [(40.0, 0.3, "lift_gate", {"access": "private"}, "g1"), (60.0, 0.0, "bollard", {}, "b1"),
               (20.0, 0.0, "gate", {"access": "no"}, "g2")]
        ground = lambda la, lo: np.zeros(len(np.atleast_1d(la)))      # noqa: E731
        import geodesy
        frame = geodesy.EnuFrame(51.5, 0.0)
        driven = drivefeat.barrier_rows(frame, (-100, -100, 100, 100), pts, roads, ground, {"r1": True})
        groups = [r[0] for r in driven]
        self.assertIn("dp_liftgate_up", groups)                # (open: the AI drives it)
        self.assertNotIn("dp_liftgate", groups)
        self.assertEqual(groups.count("dp_bollard"), 2)        # (at the edges, the carriageway open)
        b = [r for r in driven if r[0] == "dp_bollard"]
        self.assertTrue(all(abs(r[2]) > 3.0 for r in b))
        private = drivefeat.barrier_rows(frame, (-100, -100, 100, 100), pts, roads, ground, {"r1": False})
        self.assertIn("dp_liftgate", [r[0] for r in private])

    def test_turning_circle_only_at_a_dead_end(self):
        import geodesy
        frame = geodesy.EnuFrame(33.3, -111.86)
        roads = self._roads(7.0)
        la, lo, _ = frame.enu_to_geodetic(np.array([80.5, 40.0]), np.array([0.0, 0.0]), np.zeros(2))
        nodes = [(1, float(lo[0]), float(la[0]), {"highway": "turning_circle"}),
                 (2, float(lo[1]), float(la[1]), {"highway": "turning_circle"})]
        class US:
            @staticmethod
            def at(*_a):
                return "US"
        with mock.patch.object(drivefeat, "ENDS", True), \
                mock.patch("roadends._nodes_ll", lambda *a: nodes), mock.patch("countrybounds.bundled", lambda: US):
            import roadends
            pads = roadends.corner_pads(roads, frame)
        self.assertEqual(len(pads), 1)
        r = float(np.max(np.hypot(*(pads[0]["fills"][0][:, :2] - pads[0]["centre"]).T)))
        self.assertAlmostEqual(r, roadends.R_BIG, places=3)
        import grading
        p = grading.Plan(np.asarray(roads[0][2])[:, :2], np.full(9, 5.0), 3.5)
        got = roadends.grade(pads, [("r1", p)])
        self.assertEqual(len(got), 1)
        self.assertTrue(np.allclose(got[0][1].z, 5.0))


if __name__ == "__main__":
    unittest.main()
