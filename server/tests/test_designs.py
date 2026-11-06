"""The German junction signs setting (designs.py): the environment switch and the server's cache signature."""
import os
os.environ.setdefault("BNE_REGIONAL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                                                       "regional"))     # (PR #505's data: the tests' slice)
import importlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import borderfixtures  # noqa: E402
import designs  # noqa: E402
import earthd  # noqa: E402
import fast  # noqa: E402
import furniture  # noqa: E402
import geodesy  # noqa: E402
import germany  # noqa: E402
import mapgeom  # noqa: E402
import numpy as np  # noqa: E402
import roads  # noqa: E402


def setUpModule():
    borderfixtures.require()        # (no border data: every German test would pass on today's output)


class SettingTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(designs)       # (the environment as it was)

    def test_env_forces(self):
        for env, flag, want in (("on", False, True), ("off", True, False), ("", True, True), ("", False, False),
                                ("maybe", True, True), ("maybe", False, False)):
            with mock.patch.dict(os.environ, {"BNE_DEJUNCTIONS": env}):
                importlib.reload(designs)
                self.assertEqual(designs.enabled(flag), want, (env, flag))

    def test_world_signature_has_the_key(self):
        with tempfile.TemporaryDirectory() as root:
            s = earthd.Server(root, os.path.join(root, "cache"), imagery=None)
            self.addCleanup(s.tile_pool.shutdown)
            self.addCleanup(s.hm_pool.shutdown)
            self.assertIs(s.dej_on, True)
            on = s.world_signature()
            self.assertIn("de_junctions", on)
            s.dej_on = False
            self.assertNotEqual(s.world_signature(), on)
            self.assertIs(s.world_signature()["de_junctions"], False)


def _features(layer, ids, **attrs):
    n = len(ids)
    return mapgeom.Features(layer=layer, ids=np.array(ids), kind=np.full(n, mapgeom.LINE, np.uint8),
                            part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64) * 2,
                            coord_off=np.arange(n + 1, dtype=np.int64) * 2,
                            lonlat=np.zeros((2 * n, 2)), attrs={k: np.array(v, object) for k, v in attrs.items()},
                            rules={})


class JunctionGlueTest(unittest.TestCase):
    FRAME = geodesy.EnuFrame(48.561, 11.262, 0.0)       # Schrobenhausen
    DIRS = {"e": (1, 0), "w": (-1, 0), "n": (0, 1), "s": (0, -1)}

    def arm(self, fid, cls, d, urban=True, oneway=False):
        dx, dy = self.DIRS[d]
        sp = roads.Spec(10.0, "paved", urban, cls, oneway)
        return (fid, sp, np.array([dx, dy], float), np.array([100.0, 100.0, 0.0]), True, 80.0)

    def test_ref_rank(self):
        for ref, want in (("B 300", 3), ("St 2049", 2), ("L 1140", 2), ("ND 15", 1), ("K 12", 1), ("A 9", 0),
                          ("", 0), (None, 0), ("M 12", 1), ("PAF 3", 1), ("FFB 8", 1), ("E 45", 0),
                          ("B 300;St 2049", 3), ("St 2049 / K 3", 2), ("MÜ 20", 1), ("AÖ 5", 1), ("TÖL 12", 1),
                          ("WÜ 3", 1), ("Mü 20", 1), ("FÜ_1", 0)):
            self.assertEqual(designs.ref_rank(ref), want, ref)
        import dirsigns         # (the road_routes rows' refs reach ref_rank whole: _routes_of)
        self.assertEqual([r for _, r, _, _ in dirsigns.parse_routes("regional|MÜ 20|0|1;regional|TÖL 12|0.5|1")],
                         ["MÜ 20", "TÖL 12"])
        self.assertEqual(designs.ref_rank("A 5", "tertiary"), 1)
        self.assertEqual(designs.ref_rank("A 9", "motorway"), 0)
        self.assertEqual(designs.ref_rank("A 9", "trunk"), 0)

    def test_t_on_a_stretch(self):
        arms = [self.arm("e", "secondary", "e"), self.arm("w", "secondary", "w"), self.arm("s", "residential", "s")]
        got = designs.controls(arms, 0)
        self.assertEqual([(a[0], w, p) for a, w, p in got], [("e", 3, -1), ("w", 3, -1), ("s", 1, -1)])

    def test_all_residential_is_right_before_left(self):
        arms = [self.arm("e", "residential", "e"), self.arm("w", "residential", "w"),
                self.arm("n", "residential", "n")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [designs.RVL] * 3)

    def test_ring_and_signals_pass_through(self):
        arms = [self.arm("e", "tertiary", "e"), self.arm("w", "tertiary", "w"), self.arm("s", "tertiary", "s")]
        got = designs.controls(arms, 0, ring={"e", "w"})
        self.assertEqual([(a[0], w, p) for a, w, p in got], [("s", furniture.G_YIELD, -1)])
        got = designs.controls(arms, 0, signals=np.array([[100.0, 100.0]]))
        self.assertEqual([(w, p) for _, w, p in got], [(furniture.G_SIGNAL, -1)] * 3)

    def test_border_junction_point_decides(self):
        """A T whose junction point is 1 m inside Germany and whose east arm reaches across the border: the junction is
        German (inside takes the junction's point), so all its arms are decided."""
        cut = 100.0         # Germany is the half-plane west of x = 100 m
        lon_cut = float(self.FRAME.enu_to_geodetic(np.array(cut), np.array(100.0), 0.0)[1])
        half = lambda lon, lat: np.asarray(lon, float) <= lon_cut     # noqa: E731
        ctx = designs.DeContext(self.FRAME, {}, {}, np.zeros((0, 3)))
        jx, jy = cut - 1.0, 100.0
        arms = [(f, sp, u, np.array([jx, jy, 0.0]), st, 80.0) for f, sp, u, _, st, _ in (
            self.arm("e", "secondary", "e"), self.arm("w", "secondary", "w"), self.arm("s", "residential", "s"))]
        self.assertGreater(jx + 80.0, cut, "the east arm's far end is across the border")
        with mock.patch.object(germany, "contains", half):
            self.assertTrue(designs.inside(ctx, jx, jy))
            self.assertFalse(designs.inside(ctx, jx + 80.0, jy), "(the arm's far end would not be)")
            got = designs.controls(arms, 0) if designs.inside(ctx, jx, jy) else None
        self.assertEqual([w for _, w, _ in got], [3, 3, 1])

    def test_living_street_junction_with_a_driveway(self):
        arms = [self.arm("e", "living_street", "e"), self.arm("w", "living_street", "w"),
                self.arm("n", "living_street", "n"), self.arm("s", "service", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [designs.RVL] * 3 + [designs.EXIT])

    def test_kernel_version_required(self):
        with mock.patch.object(fast._ef, "VERSION", 151):
            with self.assertRaises(fast.NativeMissing):
                fast.require(designs.KERNEL, "de_junction")
            with self.assertRaises(fast.NativeMissing):
                designs.context(self.FRAME, [])

    def test_driveway_is_no_junction(self):
        arms = [self.arm("e", "residential", "e"), self.arm("w", "residential", "w"), self.arm("s", "service", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [0, 0, designs.EXIT])

    def test_living_street_onto_residential(self):
        arms = [self.arm("e", "residential", "e"), self.arm("w", "residential", "w"),
                self.arm("n", "residential", "n"), self.arm("s", "living_street", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [designs.RVL] * 3 + [designs.EXIT])

    def test_two_arm_connector_is_nothing(self):
        arms = [self.arm("e", "secondary", "e"), self.arm("w", "residential", "w")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [0, 0])

    def test_link_ranks_as_its_class(self):
        arms = [self.arm("e", "secondary_link", "e"), self.arm("w", "secondary", "w"),
                self.arm("s", "residential", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [3, 3, 1])

    def test_context_info_and_stretch(self):
        feats = _features("roads", ["a", "b", "c", "d"], subtype=["road"] * 4,
                          **{"class": ["tertiary", "tertiary", "residential", "residential"],
                             "osm_priority_road": [None, "end", "designated", None],
                             "osm_zone_maxspeed": [None, None, None, "DE:20"]})
        routes = _features("road_routes", ["a", "b"],
                           routes_txt=["national|B 300|0|1;national|A 9|0|1", "national|K 12|0|1"],
                           **{"class": ["tertiary", "tertiary"]})
        ctx = designs.context(self.FRAME, [feats], [routes])
        self.assertEqual(ctx.info["a"], (3, 0, False))
        self.assertEqual(ctx.info["b"], (1, designs.PRIO["end"], False))
        self.assertEqual(ctx.info["c"], (0, designs.PRIO["designated"], False))
        self.assertEqual(ctx.info["d"], (0, 0, True))
        self.assertEqual(ctx.stretch, {"a": True, "b": False, "c": True, "d": False})
        self.assertEqual(ctx.mapped.shape, (0, 3))
        none = designs.context(self.FRAME, [feats])         # no routes layer: class alone
        self.assertEqual(none.info["a"][0], 0)
        self.assertFalse(none.stretch["a"])

    def test_numpy_wrappers(self):
        u8, i8 = np.uint8, np.int8
        got = fast._ef.de_stretch(np.array([4, 3, 1], u8), np.array([0, 1, 0], u8), np.array([0, 0, 0], u8))
        self.assertEqual(list(got), [True, True, False])
        u = np.array([[1.0, 0.0], [-1.0, 0.0], [0.0, -1.0]])
        what, plate = fast._ef.de_junction(u, np.array([4, 4, 1], u8), np.zeros(3, u8), np.array([2, 2, 0], u8),
                                           np.full(3, -1, i8), True)
        self.assertEqual((list(what), list(plate)), ([3, 3, 1], [-1, -1, -1]))
        with self.assertRaises(ValueError):
            fast._ef.de_stretch(np.zeros(3, u8), np.zeros(2, u8), np.zeros(3, u8))
        with self.assertRaises(ValueError):
            fast._ef.de_junction(u, np.zeros(2, u8), np.zeros(3, u8), np.zeros(3, u8), np.full(3, -1, i8), True)
