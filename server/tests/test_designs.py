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
import countrybounds  # noqa: E402
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

    def test_a_link_yields_to_the_roads_it_meets(self):
        """Where a link meets a road that is none, it ranks below every such road (205), whatever its class; among
        links alone a link ranks as its base class."""
        arms = [self.arm("e", "secondary_link", "e"), self.arm("w", "secondary", "w"),
                self.arm("s", "residential", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)], [designs.Z205, designs.Z301, designs.Z301])
        links = [self.arm("e", "secondary_link", "e"), self.arm("w", "secondary_link", "w"),
                 self.arm("s", "residential_link", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(links, 0)], [3, 3, 1])
        # a slip road onto a residential crossroads: its give-way departs from right before left there, so the other
        # three are signed too (VwV-StVO zu den Zeichen 205 und 206 Rn. 6): N - S the pair, W gives way
        arms = [self.arm("e", "residential_link", "e"), self.arm("w", "residential", "w"),
                self.arm("n", "residential", "n"), self.arm("s", "residential", "s")]
        self.assertEqual([w for _, w, _ in designs.controls(arms, 0)],
                         [designs.Z205, designs.Z205, designs.Z301, designs.Z301])

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


class MappedTest(unittest.TestCase):
    """The map's own stop and give-way signs at a German junction: designs.mapped_rows (Overture's stop / give-way
    points and the lane shards' 205 / 206 sign nodes) and controls' assignment of each to one arm, the one whose
    centreline it is nearest within furniture.MAPPED_ARM_M along it (and half its road) and its half width +
    MAPPED_SIDE_M beside it, the drivers' side breaking a tie: the kernel's mapped 205 / 206 there."""
    FRAME = JunctionGlueTest.FRAME
    DIRS = JunctionGlueTest.DIRS
    NO_SIGNALS = np.zeros((0, 2))
    arm = JunctionGlueTest.arm      # (fid, Spec 10 m wide, u, the junction (100, 100), at its start, 80 m)

    def four(self, cls="residential"):
        return [self.arm(f, cls, f) for f in "enws"]

    def sign(self, x, y, code, **more):
        """A lane shard sign node (railtags.sign_nodes') at frame (x, y)."""
        lat, lon, _ = self.FRAME.enu_to_geodetic(np.array(float(x)), np.array(float(y)), 0.0)
        return float(lon), float(lat), dict({"traffic_sign": code}, **more)

    def what(self, arms, signs=(), priority=None):
        ctx = designs.DeContext(self.FRAME, {}, {}, designs.mapped_rows(self.FRAME, priority, signs))
        return {a[0]: w for a, w, _ in designs.controls(arms, 0, signals=self.NO_SIGNALS, ctx=ctx)}

    def owner(self, arms, signs=(), priority=None):
        """[the fid of the arm each of the map's rows is given to (None: no arm's)]."""
        got = designs.mapped_arms(arms, designs.mapped_rows(self.FRAME, priority, signs))
        return [arms[i][0] if i >= 0 else None for i in got]

    RVL4 = {"e": designs.RVL, "n": designs.RVL, "w": designs.RVL, "s": designs.RVL}

    @staticmethod
    def signed(fid, what):
        """The crossroads' answer with the map's sign what on arm fid alone: no right before left among the other three
        either (VwV-StVO zu den Zeichen 205 und 206 Rn. 6: signed on both roads), the two across it the pair (301), the
        one opposite 205."""
        across = "ew" if fid in "ns" else "ns"
        return {f: what if f == fid else designs.Z301 if f in across else designs.Z205 for f in "enws"}

    def test_sign_node_without_direction_goes_to_one_arm(self):
        """A 205 node 4 m to the right of arm S, 12 m from the junction: S's alone, S gives way (a row on every arm
        would make all four 205). So does one set where its post stands, 3 m past S's kerb (its half width 5 m +
        MAPPED_SIDE_M)."""
        for p in ((104.0, 88.0), (108.0, 85.0)):
            self.assertEqual(self.owner(self.four(), [self.sign(*p, "DE:205")]), ["s"], p)
            self.assertEqual(self.what(self.four(), [self.sign(*p, "DE:205")]), self.signed("s", designs.Z205), p)

    def test_far_node_ignored(self):
        """45 m from the junction along S, or 11 m beside it (its half width 5 m + 5 m): no arm's."""
        self.assertEqual(self.what(self.four(), [self.sign(104.0, 55.0, "DE:205")]), self.RVL4)
        self.assertEqual(self.what(self.four(), priority=np.array([[100.0, 55.0, 1.0]])), self.RVL4)
        self.assertEqual(self.what(self.four(), [self.sign(111.0, 88.0, "DE:206")]), self.RVL4)

    def test_stop_node_makes_206(self):
        """Overture's highway=stop on arm S (on its way, 10 m out): S = Z206."""
        for value, want in ((1.0, designs.Z206), (0.0, designs.Z205)):
            self.assertEqual(self.what(self.four(), priority=np.array([[100.0, 90.0, value]])), self.signed("s", want))
        both = np.array([[100.0, 90.0, 0.0], [100.0, 85.0, 1.0]])         # (a give-way and a stop on one arm: a stop)
        self.assertEqual(self.owner(self.four(), priority=both), ["s", "s"])
        self.assertEqual(self.what(self.four(), priority=both), self.signed("s", designs.Z206))

    def test_306_node_adds_no_yield(self):
        """301 / 306 / 307 mark the road with the right of way, not the one that yields: no row."""
        for code in ("DE:306", "DE:301", "DE:307", "DE:306,1002-12", "DE:1002-12", "DE:2050"):
            self.assertEqual(designs.mapped_rows(self.FRAME, None, [self.sign(100.0, 90.0, code)]).shape, (0, 3), code)
        self.assertEqual(self.what(self.four(), [self.sign(100.0, 90.0, "DE:306")]), self.RVL4)

    def test_codes_by_number(self):
        """A 206 is a stop (1), a 205 a give-way (0), with their variants and the 1002 plates after them; a node with
        both is a stop; the positions in the frame."""
        for code, want in (("DE:205", 0.0), ("DE:206", 1.0), ("DE:205,1002-12", 0.0), ("DE:206,1002-21", 1.0),
                           ("DE:205[1]", 0.0), ("DE:306,DE:205", 0.0), ("DE:205,DE:206", 1.0)):
            got = designs.mapped_rows(self.FRAME, None, [self.sign(110.0, 90.0, code)])
            self.assertEqual(got.shape, (1, 3), code)
            np.testing.assert_allclose(got[0], [110.0, 90.0, want], atol=1e-6, err_msg=code)

    def test_one_row_per_sign(self):
        """A node in two cells (the join's pad) and the same sign both ways (two records) are one row; Overture's points
        come first-class beside them."""
        s = self.sign(110.0, 90.0, "DE:205")
        both = [s, s, self.sign(110.0, 90.0, "DE:205", direction="forward"),
                self.sign(110.0, 90.0, "DE:205", direction="backward")]
        self.assertEqual(designs.mapped_rows(self.FRAME, None, both).shape, (1, 3))
        got = designs.mapped_rows(self.FRAME, np.array([[1.0, 2.0, 1.0]]), both)
        self.assertEqual(sorted(map(tuple, np.round(got, 6))), [(1.0, 2.0, 1.0), (110.0, 90.0, 0.0)])
        self.assertEqual(designs.mapped_rows(self.FRAME, None, ()).shape, (0, 3))

    def test_tie_goes_to_the_drivers_side(self):
        """A sign at the corner between S and E, as near to both: it stands on the right of the drivers coming in on S
        (heading north), on the left of those on E (heading west): S's. The same between W and N: N's. Half a metre
        nearer E's line is still a tie (MAPPED_TIE_M)."""
        for p, fid in (((103.0, 97.0), "s"), ((97.0, 103.0), "n"), ((103.5, 97.0), "s")):
            self.assertEqual(self.owner(self.four(), [self.sign(*p, "DE:205")]), [fid], p)
            self.assertEqual(self.what(self.four(), [self.sign(*p, "DE:205")]), self.signed(fid, designs.Z205), p)

    def test_nearest_centreline_wins(self):
        """A node 1 m left of S's centreline, 6 m out (on W's drivers' side, 6 m off W's): S's, the nearer."""
        self.assertEqual(self.owner(self.four(), [self.sign(99.0, 94.0, "DE:205")]), ["s"])
        self.assertEqual(self.what(self.four(), [self.sign(99.0, 94.0, "DE:205")]), self.signed("s", designs.Z205))

    def test_between_two_junctions(self):
        """Two 4-ways 40 m apart on one road: a stop 25 m from the first, 15 m from the second, is the second's only
        (half the road between them each: furniture._mapped_priority's reach)."""
        def four_at(c, short):
            return [(f, sp, u, np.array([c[0], c[1], 0.0]), st, 40.0 if f == short else ln)
                    for f, sp, u, _, st, ln in self.four()]
        rows = designs.mapped_rows(self.FRAME, np.array([[100.0, 75.0, 1.0]]), ())
        ctx = designs.DeContext(self.FRAME, {}, {}, rows)
        j1 = designs.controls(four_at((100.0, 100.0), "s"), 0, signals=self.NO_SIGNALS, ctx=ctx)
        j2 = designs.controls(four_at((100.0, 60.0), "n"), 1, signals=self.NO_SIGNALS, ctx=ctx)
        self.assertEqual({a[0]: w for a, w, _ in j1}, self.RVL4)
        self.assertEqual({a[0]: w for a, w, _ in j2}, self.signed("n", designs.Z206))

    def test_a_bowed_road_between_two_junctions(self):
        """Two junctions 30 m apart joined by a road that bows out 12 m (42.8 m long): a give-way 20.4 m along it from A
        (22.4 m from B) is A's only, measured along the road itself (lines: decide's); along each end's first direction
        both would be within half the road (18.2 and 19.3 m of 21.4)."""
        bow = np.array([(0.0, 0.0, 0.0), (8.0, 12.0, 0.0), (22.0, 12.0, 0.0), (30.0, 0.0, 0.0)])
        length = float(np.hypot(*np.diff(bow[:, :2], axis=0).T).sum())
        sp = roads.Spec(10.0, "paved", True, "residential", False)

        def junction(p, u, line):
            p = np.array([p[0], p[1], 0.0])
            side = [(f, sp, np.array(d, float), p, True, 80.0) for f, d in (("w" + str(p[0]), (-1.0, 0.0)),
                                                                             ("s" + str(p[0]), (0.0, -1.0)))]
            bowed = ("bow", sp, np.array(u) / np.hypot(*u), p, bool(p[0] == 0.0), length)
            return [bowed] + side, [line, None, None]
        rows = designs.mapped_rows(self.FRAME, np.array([[14.0, 12.5, 0.0]]), ())
        ctx = designs.DeContext(self.FRAME, {}, {}, rows)
        got = {}
        for name, (arms, lines) in (("a", junction((0.0, 0.0), (8.0, 12.0), bow)),
                                    ("b", junction((30.0, 0.0), (-8.0, 12.0), bow[::-1]))):
            self.assertEqual(list(designs.mapped_arms(arms, rows)), [0], "(the straight line: both ends)")
            got[name] = designs.controls(arms, 0, signals=self.NO_SIGNALS, ctx=ctx, lines=lines)[0][1]
        self.assertEqual(got, {"a": designs.Z205, "b": designs.RVL})
        rs = [("bow", sp, bow, 0.0)] + [(f, sp, np.array([p, q], float), 0.0) for f, p, q in (
            ("wa", (0, 0, 0), (-80, 0, 0)), ("sa", (0, 0, 0), (0, -80, 0)), ("eb", (30, 0, 0), (110, 0, 0)),
            ("sb", (30, 0, 0), (30, -80, 0)))]
        decided = designs.decide(rs, ctx, (-100.0, -100.0, 200.0, 200.0), self.NO_SIGNALS)
        self.assertEqual(sorted((round(float(arms[0][3][0])), w) for arms, ctl in decided.values()
                                for a, w, _ in ctl if a[0] == "bow"), [(0, designs.Z205), (30, designs.RVL)])

    def test_a_node_on_a_driveway_is_the_driveways(self):
        """A give-way 10 m up a driveway leaving 60 deg off E, 8.7 m beside E's centreline: nearest the driveway's (no
        street's: it gives way by StVO § 10 anyway), so E keeps right before left."""
        u = np.array(unit(-60.0))
        drive = ("d", roads.Spec(4.0, "paved", True, "service", False), u, np.array([100.0, 100.0, 0.0]), True, 80.0)
        arms = [self.arm(f, "residential", f) for f in "enw"] + [drive]
        p = np.array([100.0, 100.0]) + 10.0 * u
        self.assertEqual(self.what(arms, [self.sign(p[0], p[1], "DE:205")]),
                         {"e": designs.RVL, "n": designs.RVL, "w": designs.RVL, "d": designs.EXIT})

    def test_rows_merge_over_the_links(self):
        """A link meeting a road that is none gives way there (the kernel's mapped give-way); the map's rows merge
        over it: a mapped stop on the link is 206, a mapped give-way elsewhere still counts."""
        arms = [self.arm("e", "secondary_link", "e"), self.arm("w", "secondary", "w"),
                self.arm("s", "residential", "s")]
        self.assertEqual(self.what(arms), {"e": designs.Z205, "w": designs.Z301, "s": designs.Z301})
        self.assertEqual(self.what(arms, priority=np.array([[110.0, 100.0, 1.0]]))["e"], designs.Z206)
        got = self.what(arms, [self.sign(100.0, 90.0, "DE:205")])
        self.assertEqual((got["e"], got["s"]), (designs.Z205, designs.Z205))

    def test_not_decided_places_nothing(self):
        """A mapped stop where no junction is decided: a driveway on a street, two ways meeting, a
        carriageway split: nothing there."""
        drive = [self.arm("e", "residential", "e"), self.arm("w", "residential", "w"), self.arm("s", "service", "s")]
        self.assertEqual(self.what(drive, priority=np.array([[110.0, 100.0, 1.0]])),
                         {"e": 0, "w": 0, "s": designs.EXIT})
        two = [self.arm("e", "secondary", "e"), self.arm("w", "residential", "w")]
        self.assertEqual(self.what(two, priority=np.array([[90.0, 100.0, 1.0]])), {"e": 0, "w": 0})
        split = [(f, roads.Spec(10.0, "paved", True, "secondary", bool(d), 2.5, d), np.array(u), np.array(
            [100.0, 100.0, 0.0]), True, 80.0) for f, u, d in (("two", (-1.0, 0.0), 0), ("out", unit(-10), 1),
                                                                ("in", unit(5), -1))]
        self.assertEqual(self.what(split, priority=np.array([[110.0, 101.0, 1.0], [90.0, 100.0, 1.0]])),
                         {"two": 0, "out": 0, "in": 0})

    def test_reads_here_over_the_box(self):
        """R36: the read gate's box touches Germany at a corner or its centre (a half-plane west of x = 0 here); off,
        the border data untouched. tile_reach: a tile's own box + roads.NEAR_M (+ a geotile's skin margin)."""
        lon_cut = float(self.FRAME.enu_to_geodetic(np.array(0.0), np.array(0.0), 0.0)[1])
        west = lambda lon, lat: np.atleast_1d(np.asarray(lon, float) <= lon_cut)    # noqa: E731
        with mock.patch.object(germany, "contains", west):
            self.assertTrue(designs.reads_here(True, self.FRAME, (-10.0, 0.0, 300.0, 300.0)), "a corner")
            self.assertFalse(designs.reads_here(True, self.FRAME, (10.0, 0.0, 300.0, 300.0)))
            self.assertTrue(designs.reads_here(True, self.FRAME, (-300.0, 0.0, 200.0, 300.0)), "the centre")
            self.assertTrue(designs.reads_here(True, self.FRAME, designs.tile_reach((30.0, 0.0, 286.0, 256.0))))
            self.assertFalse(designs.reads_here(True, self.FRAME, designs.tile_reach((50.0, 0.0, 306.0, 256.0))))
        self.assertEqual(designs.tile_reach((0.0, 0.0, 256.0, 256.0), 3.0), (-43.0, -43.0, 299.0, 299.0))

        def untouched(*a, **k):
            raise AssertionError("Germany's border data asked with the setting off")
        with mock.patch.object(germany, "contains", untouched), mock.patch.object(countrybounds, "bundled", untouched):
            self.assertFalse(designs.reads_here(False, self.FRAME, (-300.0, -300.0, 300.0, 300.0)))

    def test_near_is_the_box(self):
        """near(): the box against germany.BOX, no lookup (Salzburg and Strasbourg are near, Baltimore and Vienna
        not)."""
        tile = (0.0, 0.0, 256.0, 256.0)
        with mock.patch.object(germany, "contains", side_effect=AssertionError("asked")), \
                mock.patch.object(countrybounds, "bundled", side_effect=AssertionError("asked")):
            for (lat, lon), want in (((47.800, 13.045), True), ((48.583, 7.746), True), ((48.561, 11.262), True),
                                     ((39.2834, -76.5934), False), ((48.21, 16.37), False)):
                self.assertIs(designs.near(geodesy.EnuFrame(lat, lon, 0.0), tile), want, (lat, lon))

    def test_cache_parts(self):
        """The German part of a road tile's cache key: () off or away from germany.BOX (the key as before, the border
        data not asked), the border data's signature on near Germany: another for no border data (Absent)."""
        reach = designs.tile_reach((0.0, 0.0, 256.0, 256.0))
        munich, baltimore = geodesy.EnuFrame(48.137, 11.575, 0.0), geodesy.EnuFrame(39.2834, -76.5934, 0.0)
        with mock.patch.object(germany, "signature", side_effect=AssertionError("asked")), \
                mock.patch.object(countrybounds, "bundled", side_effect=AssertionError("asked")):
            self.assertEqual(designs.cache_parts(False, munich, reach), ())
            self.assertEqual(designs.cache_parts(True, baltimore, reach), ())
        here = designs.cache_parts(True, munich, reach)
        self.assertEqual(here, (countrybounds.signature(),))
        with mock.patch.object(countrybounds, "bundled", return_value=countrybounds.Absent()):
            self.assertEqual(designs.cache_parts(True, munich, reach), (countrybounds.signature() + germany.ABSENT,))
        salzburg = geodesy.EnuFrame(47.800, 13.045, 0.0)        # (in the box, not in Germany: keyed by the data too)
        self.assertEqual(designs.cache_parts(True, salzburg, reach), here)

    def test_context_fills_mapped(self):
        feats = _features("roads", ["a"], subtype=["road"], **{"class": ["residential"]})
        ctx = designs.context(self.FRAME, [feats], priority=np.array([[1.0, 2.0, 1.0]]),
                              signs=[self.sign(110.0, 90.0, "DE:205"), self.sign(120.0, 90.0, "DE:306")])
        self.assertEqual(sorted(map(tuple, np.round(ctx.mapped, 6))), [(1.0, 2.0, 1.0), (110.0, 90.0, 0.0)])


# test_furniture-style recorded rows: junction_props without the German signs on JunctionRowsTest.fixtures() (captured
# once from the branch's base, 6991db3e, the same on upstream/dev)
TODAY = {
    "stretch T": [["stop", 107.0, 92.5, 0.15, 0.0, 1.0, 3.0, 0.0]],
    "residential T": [["stop", 107.0, 92.5, 0.15, 0.0, 1.0, 3.0, 0.0]],
    "tertiary cross": [["signal", 107.5, 106.0, 0.15, 90.0, 1.0, 9.4, 0.0],
                       ["signal", 92.5, 94.0, 0.15, 270.0, 1.0, 9.4, 0.0],
                       ["signal", 94.0, 107.5, 0.15, 180.0, 1.0, 9.4, 0.0],
                       ["signal", 106.0, 92.5, 0.15, 0.0, 1.0, 9.4, 0.0]],
    "residential cross": [["stop", 107.5, 107.0, 0.15, 90.0, 1.0, 3.0, 0.0],
                          ["stop", 92.5, 93.0, 0.15, 270.0, 1.0, 3.0, 0.0],
                          ["stop", 93.0, 107.5, 0.15, 180.0, 1.0, 3.0, 0.0],
                          ["stop", 107.0, 92.5, 0.15, 0.0, 1.0, 3.0, 0.0]],
    "rural stretch T": [],
}


class Placed:
    """Where a sign row stands, round the junction C (a mixin for TestCase)."""
    C = (100.0, 100.0)
    DIRS = JunctionGlueTest.DIRS

    def where(self, row):
        """(the arm it stands at, "before" facing the drivers coming in on it, or "after" facing those who leave on it),
        from its spot and its face (the signs' local -y after the yaw, as test_furniture's front)."""
        away = np.array([row[1] - self.C[0], row[2] - self.C[1]])
        d = max(self.DIRS, key=lambda k: float(np.dot(self.DIRS[k], away)))
        u = np.array(self.DIRS[d], float)
        a = np.radians(row[4])
        front = np.array([np.sin(a), -np.cos(a)])
        right = np.array([-u[1], u[0]]) if front @ u > 0.99 else np.array([u[1], -u[0]])
        self.assertGreater(abs(float(front @ u)), 0.99, f"{row} faces along its arm")
        self.assertGreater(float(away @ right), 2.0, f"{row} stands on the drivers' right")
        self.assertLess(float(away @ u), 30.0, f"{row} stands at the junction")
        return d, "before" if front @ u > 0 else "after"

    def placed(self, rows):
        return sorted((r[0],) + self.where(r) for r in rows)


def unit(deg):
    a = np.radians(deg)
    return (float(np.cos(a)), float(np.sin(a)))


class JunctionRowsTest(Placed, unittest.TestCase):
    """The German junction signs as furniture.junction_props places them (de_on, the junction point inside Germany):
    designs.junction_rows on the kernel's decision (designs.controls), each sign where VwV-StVO stands it."""
    FRAME = JunctionGlueTest.FRAME
    BOX = (0.0, 0.0, 256.0, 256.0)
    NO_SIGNALS = np.zeros((0, 2))       # the map's traffic signals: none here (None: guessed at 3 major roads)

    def road(self, fid, cls, d, urban=True, kmh=None, oneway=0, towards=False, length=80.0):
        """A road from the junction (C) along d ("e" ... or a vector); towards: drawn from its far end to the junction;
        oneway: +1 driven along its geometry, -1 against it."""
        dx, dy = self.DIRS[d] if isinstance(d, str) else d
        pts = np.array([(self.C[0], self.C[1], 0.0), (self.C[0] + dx * length, self.C[1] + dy * length, 0.0)])
        sp = roads.Spec(10.0, "paved", urban, cls, bool(oneway), 2.5, oneway, kmh)
        return (fid, sp, pts[::-1] if towards else pts, 0.0)

    def ctx(self, stretch=None, info=None, town=None):
        return designs.DeContext(self.FRAME, info or {}, stretch or {}, np.zeros((0, 3)), town)

    def props(self, rs, ctx=None, box=None, **kw):
        return furniture.junction_props(rs, box or self.BOX, de_on=True, de_ctx=ctx or self.ctx(), **kw)

    def stretch_t(self, urban=True, **kmh):
        return [self.road("e", "secondary", "e", urban, kmh.get("e")),
                self.road("w", "secondary", "w", urban, kmh.get("w")),
                self.road("s", "residential", "s", urban, kmh.get("s"))]

    def test_props_t_on_a_stretch(self):
        rows = self.props(self.stretch_t())
        self.assertEqual(sorted(r[0] for r in rows), ["de_z205", "de_z306", "de_z306"])
        self.assertFalse({r[0] for r in rows} & {furniture.G_STOP, furniture.G_YIELD})
        for r in rows:
            self.assertEqual(len(r), 8)
            self.assertEqual(r[5], 1.0)
            self.assertEqual(r[6], designs.HEIGHTS[r[0]])
        self.assertEqual(self.placed(rows), [("de_z205", "s", "before"), ("de_z306", "e", "before"),
                                             ("de_z306", "w", "before")], "in town the 306 stands before it (Rn. 2)")

    def fixtures(self):
        res = [self.road("e", "residential", "e"), self.road("w", "residential", "w"),
               self.road("s", "residential", "s")]
        cross = [self.road(f, "tertiary", f) for f in "ewns"]
        allway = [self.road(f, "residential", f) for f in "ewns"]
        return {"stretch T": self.stretch_t(), "residential T": res, "tertiary cross": cross,
                "residential cross": allway, "rural stretch T": self.stretch_t(urban=False)}

    def test_props_off_is_todays(self):
        for name, rs in self.fixtures().items():
            self.assertEqual(furniture.junction_props(rs, self.BOX, de_on=False, de_ctx=self.ctx()), TODAY[name], name)
            self.assertEqual(furniture.junction_props(rs, self.BOX), TODAY[name], name)

    def test_props_outside_germany_is_todays(self):
        nowhere = lambda lon, lat: np.zeros(np.size(lon), bool)     # noqa: E731
        with mock.patch.object(germany, "contains", nowhere):
            for name, rs in self.fixtures().items():
                got = self.props(rs)
                self.assertEqual(got, furniture.junction_props(rs, self.BOX, de_on=False, de_ctx=self.ctx()), name)
                self.assertEqual(got, TODAY[name], name)

    def test_rural_301_at_the_junction(self):
        rs = [self.road("e", "tertiary", "e", False), self.road("w", "tertiary", "w", False),
              self.road("s", "unclassified", "s", False)]
        self.assertEqual(self.placed(self.props(rs)), [("de_z205", "s", "before"), ("de_z301", "e", "before"),
                                                       ("de_z301", "w", "before")],
                         "301 immediately before the junction out of town too (zu Zeichen 301 Rn. 1)")

    def test_tile_edge_ownership(self):
        a, b = (0.0, 0.0, self.C[0] + 0.5, 256.0), (self.C[0] + 0.5, 0.0, 356.0, 256.0)
        got = self.props(self.stretch_t(), box=a)
        self.assertEqual(len(got), 3)
        self.assertTrue(any(r[1] >= a[2] for r in got), "a sign beyond the tile's edge is still its junction's tile's")
        self.assertEqual(self.props(self.stretch_t(), box=b), [])

    def test_bent_priority_rows_use_plate_groups(self):
        for urban in (True, False):
            rs = [self.road("w", "secondary", "w", urban), self.road("s", "secondary", "s", urban),
                  self.road("e", "residential", "e", urban)]
            self.assertEqual(self.placed(self.props(rs)), [("de_z205_p3", "e", "before"), ("de_z306_p0", "s", "before"),
                                                           ("de_z306_p4", "w", "before")],
                             f"the kernel's plates (W 4, S 0, E 3), the 306 always before (Rn. 4); urban {urban}")

    def test_a_bent_junction_stands_every_306_before(self):
        """A bent priority whose 1002 picture one approach lacks (the kernel's table miss: plate -1 there): that 306
        stands before the junction as well, with the others (Rn. 4), out of town too."""
        rs = [self.road("w", "secondary", "w", False), self.road("s", "secondary", "s", False),
              self.road("e", "residential", "e", False)]
        arms = next(a for a in furniture._arms(rs).values() if len(a) == 3)
        ctl = [(a, {"w": 3, "s": 3, "e": 1}[a[0]], {"w": 4, "s": -1, "e": 3}[a[0]]) for a in arms]

        def spot(arm, group, side, yaw, back=0.0):
            q = np.asarray(arm[3][:2]) + np.asarray(arm[2]) * (7.5 + back) + side * 6.0
            return float(q[0]), float(q[1]), 0.15
        rows = designs.junction_rows(arms, ctl, self.ctx(), False, False, spot)
        self.assertEqual(self.placed(rows), [("de_z205_p3", "e", "before"), ("de_z306", "s", "before"),
                                             ("de_z306_p4", "w", "before")])

    def test_rural_306_after_the_junction(self):
        self.assertEqual(self.placed(self.props(self.stretch_t(urban=False))),
                         [("de_z205", "s", "before"), ("de_z306", "e", "after"), ("de_z306", "w", "after")],
                         "out of town the 306 stands after the junction (zu den Zeichen 306 und 307 Rn. 2)")

    def test_sizes_follow_the_limit(self):
        rows = self.props(self.stretch_t(e=120, w=60, s=30))
        self.assertEqual(self.placed(rows), [("de_z205_g1", "s", "before"), ("de_z306", "w", "before"),
                                             ("de_z306_g3", "e", "before")])
        rows = self.props(self.stretch_t(urban=False, e=120, w=60, s=30))
        self.assertEqual(self.placed(rows), [("de_z205_g1", "s", "before"), ("de_z306", "w", "after"),
                                             ("de_z306_g3", "e", "after")], "after it: the road it stands on")

    def end(self, urban, **town):
        """A stretch ending (W secondary, E a tertiary without a ref, S residential: the kernel ranks it, W-E 301)."""
        return [self.road("w", "secondary", "w", urban if "w" not in town else town["w"]),
                self.road("e", "tertiary", "e", urban), self.road("s", "residential", "s", urban)]

    def test_307_out_of_town_only(self):
        """Out of town the 307 before the stretch's end, on a post of its own, with W's 301 (Rn. 8), and the 306 after
        it for the drivers who start on the stretch W; in town no 307 (entbehrlich, Rn. 8), the stretch's 306 before
        its last junction (zu Zeichen 301 Rn. 4). A stretch ending at a bigger stretch: 307 and 205."""
        rural = self.props(self.end(False))
        self.assertEqual(self.placed(rural), [("de_z205", "s", "before"), ("de_z301", "e", "before"),
                                              ("de_z301", "w", "before"), ("de_z306", "w", "after"),
                                              ("de_z307", "w", "before")])
        g = {r[0]: r for r in rural if self.where(r) == ("w", "before")}
        self.assertGreater(self.C[0] - g["de_z307"][1], self.C[0] - g["de_z301"][1] + 5.0, "farther back than the 301")
        self.assertEqual(self.placed(self.props(self.end(True))), [("de_z205", "s", "before"),
                                                                   ("de_z301", "e", "before"),
                                                                   ("de_z306", "w", "before")])
        stem = [self.road("e", "primary", "e", False), self.road("w", "primary", "w", False),
                self.road("s", "secondary", "s", False)]
        self.assertEqual(self.placed(self.props(stem, signals=self.NO_SIGNALS)),
                         [("de_z205", "s", "before"), ("de_z306", "e", "after"), ("de_z306", "w", "after"),
                          ("de_z307", "s", "before")])

    def test_a_307_off_the_junction_sign_s_spot(self):
        """The stretch ending out of town (W 301 with its 307 BACK_307_M farther out): where W's nearer spots are all
        blocked and its 301 stands 10 m out, the 307's first spot is that one (spot does not know the rows placed): it
        goes 2 m farther out (STEP_307_M), never onto the 301's post; still blocked after three tries, none."""
        arms = next(a for a in furniture._arms(self.end(False)).values() if len(a) == 3)
        ctl = designs.controls(arms, 0, signals=self.NO_SIGNALS)

        def spot(blocked):
            def at(arm, group, side, yaw, back=0.0):
                d = max(7.5 + back, blocked)        # (every spot nearer than blocked m taken)
                q = np.asarray(arm[3][:2]) + np.asarray(arm[2]) * d + side * 6.0
                return float(q[0]), float(q[1]), 0.15
            return at

        def on_w(rows):
            return {r[0]: round(float(self.C[0] - r[1]), 2) for r in rows if self.where(r) == ("w", "before")}
        self.assertEqual(on_w(designs.junction_rows(arms, ctl, self.ctx(), False, False, spot(0.0))),
                         {"de_z301": 7.5, "de_z307": 17.5})
        self.assertEqual(on_w(designs.junction_rows(arms, ctl, self.ctx(), False, False, spot(17.5))),
                         {"de_z301": 17.5, "de_z307": 19.5})
        self.assertEqual(on_w(designs.junction_rows(arms, ctl, self.ctx(), False, False, spot(25.0))),
                         {"de_z301": 25.0})

    def test_a_mapped_priority_road_beats_a_class_stretch(self):
        """The map's priority road first (the design's B1): three Kreisstraßen (tertiary, ref rank 1, so stretches by
        class), W - S mapped priority_road=designated bending at E, which goes on straight from W: W - S the 306 pair
        with its 1002 plates, E 205 (by class alone W - E, the straightest, would take it: the opposite priority), and
        out of town no 307 on E: no stretch here either."""
        designated = designs.PRIO["designated"]
        ctx = self.ctx(stretch=dict.fromkeys("wse", True),
                       info={"w": (1, designated, False), "s": (1, designated, False), "e": (1, 0, False)})
        for urban in (True, False):
            rs = [self.road("w", "tertiary", "w", urban), self.road("s", "tertiary", "s", urban),
                  self.road("e", "tertiary", "e", urban)]
            arms = next(a for a in furniture._arms(rs).values() if len(a) == 3)
            self.assertEqual(designs.junction_stretch(arms, ctx), [a[0] in "ws" for a in arms])
            self.assertEqual(designs.stretch_flags(arms, ctx), [True] * 3, "(the road's own: a stretch)")
            self.assertEqual(self.placed(self.props(rs, ctx, signals=self.NO_SIGNALS)),
                             [("de_z205_p3", "e", "before"), ("de_z306_p0", "s", "before"),
                              ("de_z306_p4", "w", "before")], urban)
        rs = [self.road("w", "tertiary", "w"), self.road("s", "tertiary", "s"), self.road("e", "tertiary", "e")]
        plain = self.ctx(stretch=dict.fromkeys("wse", True), info=dict.fromkeys("wse", (1, 0, False)))
        self.assertEqual(self.placed(self.props(rs, plain, signals=self.NO_SIGNALS)),
                         [("de_z205", "s", "before"), ("de_z306", "e", "before"), ("de_z306", "w", "before")],
                         "(none mapped: the class's straight pair)")
        # only where two or more arms are mapped (they can be the pair), and only over class stretches that rank no
        # higher: a designated tertiary ending at an untagged Bundesstraße (a T) or crossing it leaves the B road its
        # 306; two Kreisstraßen crossing, the designated one goes through
        cases = (("T", {"w": ("primary", 3, 0), "e": ("primary", 3, 0), "s": ("tertiary", 1, designated)},
                  [("de_z205", "s", "before"), ("de_z306", "e", "before"), ("de_z306", "w", "before")]),
                 ("crossing", {"w": ("primary", 3, 0), "e": ("primary", 3, 0), "n": ("tertiary", 1, designated),
                               "s": ("tertiary", 1, designated)},
                  [("de_z205", "n", "before"), ("de_z205", "s", "before"), ("de_z306", "e", "before"),
                   ("de_z306", "w", "before")]),
                 ("equal crossing", {"w": ("tertiary", 1, 0), "e": ("tertiary", 1, 0), "n": ("tertiary", 1, designated),
                                     "s": ("tertiary", 1, designated)},
                  [("de_z205", "e", "before"), ("de_z205", "w", "before"), ("de_z306", "n", "before"),
                   ("de_z306", "s", "before")]))
        for name, roads_, want in cases:
            rs = [self.road(f, c, f) for f, (c, _, _) in roads_.items()]
            ctx = self.ctx(stretch=dict.fromkeys(roads_, True),
                           info={f: (r, p, False) for f, (_, r, p) in roads_.items()})
            self.assertEqual(self.placed(self.props(rs, ctx, signals=self.NO_SIGNALS)), want, name)

    def test_no_307_where_the_stretch_is_interrupted(self):
        """Rn. 7: a stretch going on past a 205 / 206 gets no 307, nor its 306 after that junction (the next one's)."""
        rs = [self.road("w", "secondary", "w", False), self.road("e", "secondary", "e", False),
              self.road("n", "primary", "n", False), self.road("s", "primary", "s", False)]
        self.assertEqual(self.placed(self.props(rs, signals=self.NO_SIGNALS)),
                         [("de_z205", "e", "before"), ("de_z205", "w", "before"), ("de_z306", "n", "after"),
                          ("de_z306", "s", "after")])

    def ring(self, west_cls, kmh=None, exit=None):
        """A roundabout of radius 18 round the frame's origin (Schrobenhausen), one-way, entries from the east
        (secondary, kmh) and the west (west_cls); exit: (class, link flag) of a one-way leaving it at 120 deg, or None;
        every way drawn rural, as build_tile draws a ring (the town is the context's)."""
        import math

        def arc(a0, a1):
            t = np.linspace(a0, a1, 9)
            return np.c_[18 * np.cos(t), 18 * np.sin(t), np.zeros(9)]
        sp = roads.Spec(7.0, "paved", False, "tertiary", True, 1.0, 1)
        rs = [(f"r{k}", sp, arc(k * math.pi / 3, (k + 1) * math.pi / 3), 0.0) for k in range(6)]
        for fid, cls, x in (("east", "secondary", 1), ("west", west_cls, -1)):
            rs.append((fid, roads.Spec(8.0, "paved", False, cls, False, 2.5, 0, kmh if x > 0 else None),
                       np.array([(18 * x, 0, 0), (140 * x, 0, 0)], float), 0.0))
        if exit is not None:
            d = np.array([*unit(120), 0.0])
            rs.append(("exit", roads.Spec(6.0, "paved", False, exit[0], True, 2.5, 1)._replace(link=exit[1]),
                       np.array([18 * d, 140 * d]), 0.0))
        return rs

    def test_307_before_a_roundabout_only_where_the_stretch_ends(self):
        box = (-150.0, -150.0, 150.0, 150.0)
        through = furniture.junction_props(self.ring("secondary"), box, de_on=True, de_ctx=self.ctx())
        self.assertEqual(sorted(r[0] for r in through), ["de_z205"] * 2, "the stretch goes on past it (Rn. 7)")
        ends = furniture.junction_props(self.ring("residential"), box, de_on=True, de_ctx=self.ctx())
        self.assertEqual(sorted(r[0] for r in ends), ["de_z205", "de_z205", "de_z307"])
        z307 = next(r for r in ends if r[0] == "de_z307")
        z205 = next(r for r in ends if r[0] == "de_z205" and r[1] > 0)
        self.assertGreater(z307[1], z205[1] + 5.0, "on the east entry, before its 205")
        rs = self.ring("residential")
        town = self.ctx(town={fid: True for fid, *_ in rs})
        self.assertEqual(sorted(r[0] for r in furniture.junction_props(rs, box, de_on=True, de_ctx=town)),
                         ["de_z205"] * 2, "in town none (Rn. 8): the land cover's town, not the ring's drawing")
        for exit, want in (((("secondary", False)), ["de_z205"] * 2),
                           (("trunk_link", False), ["de_z205", "de_z205", "de_z307"]),
                           (("trunk", True), ["de_z205", "de_z205", "de_z307"]),
                           (("primary_link", False), ["de_z205", "de_z205", "de_z307"])):
            got = furniture.junction_props(self.ring("residential", exit=exit), box, de_on=True, de_ctx=self.ctx())
            self.assertEqual(sorted(r[0] for r in got), want, f"an exit {exit}: a ramp is no stretch going on")

    def test_roundabout_entries_give_way_with_a_205(self):
        """Spec B1: a roundabout's entries keep today's give-way spot, a 205 without a plate, sized by the entry's
        limit; no US give-way sign inside Germany."""
        box = (-150.0, -150.0, 150.0, 150.0)
        today = furniture.junction_props(self.ring("secondary", kmh=120), box)
        got = furniture.junction_props(self.ring("secondary", kmh=120), box, de_on=True, de_ctx=self.ctx())
        self.assertEqual(sorted(r[0] for r in today), [furniture.G_YIELD] * 2)
        self.assertEqual(sorted(r[0] for r in got), ["de_z205", "de_z205_g3"])
        for r in got:
            self.assertEqual(r[6], designs.HEIGHTS[r[0]])
        for g, t in zip(sorted(got, key=lambda r: r[1]), sorted(today, key=lambda r: r[1])):
            self.assertEqual(g[4], t[4], "facing today's way")
            self.assertLessEqual(float(np.hypot(g[1] - t[1], g[2] - t[2])), 1.0 + 1e-9,
                                 "today's spot (its wider face: one step farther out at most)")

    def end_way(self, *rs):
        prio = {"d": (0, designs.PRIO["designated"], False), "x": (0, designs.PRIO["end"], False),
                "x2": (0, designs.PRIO["end"], False)}
        return self.placed(self.props(list(rs), self.ctx({"d": True, "x": False, "x2": False}, prio)))

    def test_end_of_a_priority_road(self):
        """priority_road=end: traffic entering the way at its start passes the 307 (OpenStreetMap's Key:priority_road),
        out of town; at a node of two ways (the way split at the sign) or a junction. Once: not again where the way
        goes on in another piece (Overture splits it at its connectors)."""
        for urban, towards, want in ((False, False, [("de_z307", "e", "after")]), (False, True, []), (True, False, [])):
            got = self.end_way(self.road("d", "tertiary", "w", urban),
                               self.road("x", "tertiary", "e", urban, towards=towards))
            self.assertEqual(got, want, (urban, towards))
        got = self.end_way(self.road("d", "tertiary", "w", False), self.road("x", "tertiary", "e", False),
                           self.road("s", "residential", "s", False))
        self.assertIn(("de_z307", "e", "after"), got)
        self.assertNotIn(("de_z307", "w", "before"), got, "one 307: the way's own")
        self.assertEqual(self.end_way(self.road("x", "tertiary", "w", False, towards=True),
                                      self.road("x2", "tertiary", "e", False)), [], "the way going on: no 307")
        got = self.end_way(self.road("x", "tertiary", "w", False, towards=True),
                           self.road("x2", "tertiary", "e", False), self.road("s", "residential", "s", False))
        self.assertNotIn("de_z307", [g[0] for g in got], "nor where a side street meets it")

    def test_no_us_signs_in_germany(self):
        f = self.fixtures()
        self.assertEqual(self.props(f["residential T"]), [], "right before left: no stop on the stem")
        self.assertEqual(self.props(f["residential cross"]), [], "nor the all-way coin flip")
        drive = [self.road("e", "residential", "e"), self.road("w", "residential", "w"), self.road("s", "service", "s")]
        self.assertEqual(self.props(drive), [], "a driveway is no junction (§ 10), and no US rows either")
        self.assertEqual(self.props(f["tertiary cross"]), TODAY["tertiary cross"], "signals (guessed here): today's")

    def ramp(self, cls, kind, urban, link=None):
        """A ramp node: the main carriageway coming in from the west and leaving to the east (one-way), and a link
        (link, else cls + "_link") leaving to the south-east (a diverge) or coming in from the south-west (a merge)."""
        link = link or cls + "_link"
        main = [self.road("in", cls, "w", urban, oneway=-1), self.road("out", cls, "e", urban, oneway=1)]
        if kind == "diverge":
            return main + [self.road("ramp", link, unit(-20), urban, oneway=1)]
        return main + [self.road("ramp", link, unit(200), urban, oneway=-1)]

    def terminal(self, link_cls, flag=False, urban=False):
        """A ramp's end at a two-way road: a secondary W-E, the ramp coming in from the south (link_cls; flag: the
        class of the road it leads to with Spec.link set, as Overture's subclass "link" gives it in production)."""
        fid, sp, pts, lon = self.road("ramp", link_cls, "s", urban, oneway=-1)
        return [self.road("w", "secondary", "w", urban), self.road("e", "secondary", "e", urban),
                (fid, sp._replace(link=True) if flag else sp, pts, lon)]

    def test_a_ramp_s_end_gives_way(self):
        """A ramp's end onto a Bundes- or Staatsstrasse gives way (205), the road keeps its priority; no 307 for
        the ramp; production's ramps are motorway / trunk with the link flag."""
        for cls, flag in (("motorway_link", False), ("motorway", True), ("trunk_link", False), ("trunk", True)):
            got = self.placed(self.props(self.terminal(cls, flag), signals=self.NO_SIGNALS))
            self.assertEqual(got, [("de_z205", "s", "before"), ("de_z306", "e", "after"), ("de_z306", "w", "after")],
                             (cls, flag))

    def test_an_on_ramp_never_takes_the_priority(self):
        """A link leaving the node (an on-ramp's start) ranks below the road too: a tertiary whose continuation
        bends away while the ramp leaves straight on keeps its own two arms the priority pair, nobody yields, so no
        301 (zu Zeichen 301 Rn. 2) and no 205 on the road's own continuation."""
        for link in (("motorway", True), ("trunk_link", False)):
            for bend in (15, 25, 40):
                fid, sp, pts, lon = self.road("ramp", link[0], "e", False, oneway=1)
                rs = [self.road("w", "tertiary", "w", False), self.road("e", "tertiary", unit(bend), False),
                      (fid, sp._replace(link=link[1]), pts, lon)]
                self.assertEqual(self.props(rs, signals=self.NO_SIGNALS), [], (link, bend))

    def test_an_on_ramp_straight_on_is_not_the_road_ahead(self):
        """A rural stretch (secondary) bending a few degrees where an on-ramp leaves straight on: the stretch's own two
        arms are the pair (straight within the kernel's slack, no plate), and each one's 306 stands after the junction
        on the other (zu den Zeichen 306 und 307 Rn. 2), not before it because the ramp lay straighter: a link leaving
        gives way at its end, it is not where the drivers go on."""
        for link in (("secondary_link", False), ("motorway", True)):
            fid, sp, pts, lon = self.road("ramp", link[0], "e", False, oneway=1)
            rs = [self.road("w", "secondary", "w", False), self.road("e", "secondary", unit(6), False),
                  (fid, sp._replace(link=link[1]), pts, lon)]
            self.assertEqual(self.placed(self.props(rs, signals=self.NO_SIGNALS)),
                             [("de_z306", "e", "after"), ("de_z306", "w", "after")], link)

    def test_links_among_themselves(self):
        """Two motorway links merging (every arm of the motorway's class) is no junction; nor is a node with the
        Autobahn's own carriageway."""
        rs = [self.road("a", "motorway_link", "w", False, oneway=-1), self.road("b", "motorway_link", unit(200), False,
                                                                               oneway=-1),
              self.road("c", "motorway_link", "e", False, oneway=1)]
        self.assertEqual(self.props(rs, signals=self.NO_SIGNALS), [])
        rs = [self.road("a", "motorway", "w", False, oneway=-1), self.road("b", "motorway", unit(200), False,
                                                                           oneway=-1),
              self.road("c", "motorway", "e", False, oneway=1)]
        rs[1] = (rs[1][0], rs[1][1]._replace(link=True), rs[1][2], rs[1][3])
        self.assertEqual(self.props(rs, signals=self.NO_SIGNALS), [], "the Autobahn's merge, production's classes")

    def test_ramps_are_no_junctions(self):
        """An Autobahn's through carriageway has the right of way by statute (StVO § 18 Abs. 3), no 205 at its
        entries; a diverge has nobody who must yield. Neither is decided: no German rows, no US ones (none today)."""
        for urban in (True, False):
            for kind in ("diverge", "merge"):
                for link in ("motorway_link", None):
                    rs = self.ramp("motorway", kind, urban, link)
                    if link is None:        # (production: the ramp of the motorway's class, Spec.link set)
                        rs[2] = (rs[2][0], rs[2][1]._replace(cls="motorway", link=True), rs[2][2], rs[2][3])
                    self.assertEqual(self.props(rs, signals=self.NO_SIGNALS), [], (kind, urban, link))
                    self.assertEqual(furniture.junction_props(rs, self.BOX, signals=self.NO_SIGNALS), [], "today's")
            self.assertEqual(self.props(self.ramp("trunk", "diverge", urban), signals=self.NO_SIGNALS), [],
                             ("trunk diverge", urban))
        merge = self.props(self.ramp("trunk", "merge", False), signals=self.NO_SIGNALS)
        z205 = [r for r in merge if r[0] == "de_z205"]
        self.assertEqual(len(z205), 1, "an at-grade trunk merge: the slip road gives way")
        away = np.array(z205[0][1:3]) - self.C
        self.assertEqual(max((unit(-180), unit(0), unit(200)), key=lambda v: float(np.dot(v, away))), unit(200),
                         "on the slip road")

    def test_no_301_without_a_road_that_yields(self):
        """VwV-StVO zu Zeichen 301 Rn. 2: a 301 needs a 205 or 206 on the other road; the kernel's 301 pair with the
        third arm a one-way leaving it has none, so no 301."""
        rs = [self.road("e", "tertiary", "e", False), self.road("w", "tertiary", "w", False),
              self.road("s", "residential", "s", False, oneway=1)]
        self.assertEqual(self.props(rs), [])

    def test_a_zone_junction_has_no_306(self):
        """A 30 zone's crossroads (every arm in it), W - E secondaries (stretches by their class), a mapped stop on S:
        the pair W - E 301, never 306 (StVO § 45 Abs. 1c Satz 2: the zone extends over no Vorfahrtstraße; VwV-StVO zu
        § 45 Abs. 1 bis 1e Rn. 41: 301 is the zone's means), N 205; in town no 306 for the stretch's arms, out of town
        no 306 after the junction and no 307. Outside a zone the same roads are a Vorfahrtstraße."""
        stop = np.array([[100.0, 88.0, 1.0]])
        for urban in (True, False):
            rs = [self.road(f, c, f, urban) for f, c in (("w", "secondary"), ("e", "secondary"), ("n", "residential"),
                                                          ("s", "residential"))]
            zone = designs.DeContext(self.FRAME, dict.fromkeys("wens", (0, 0, True)),
                                     {"w": True, "e": True, "n": False, "s": False}, stop)
            self.assertEqual(self.placed(self.props(rs, zone, signals=self.NO_SIGNALS)),
                             [("de_z205", "n", "before"), ("de_z206", "s", "before"), ("de_z301", "e", "before"),
                              ("de_z301", "w", "before")], urban)
            plain = designs.DeContext(self.FRAME, {}, zone.stretch, stop)
            self.assertIn("de_z306", {r[0] for r in self.props(rs, plain, signals=self.NO_SIGNALS)}, urban)

    def split(self, cls, urban, side_cls=None):
        """A carriageway split: a two-way road from the west, and two one-way carriageways east of the node, the one
        leaving at -10 deg and the one coming in at +5 deg (of side_cls, else cls)."""
        c = side_cls or cls
        return [self.road("two", cls, "w", urban), self.road("out", c, unit(-10), urban, oneway=1),
                self.road("in", c, unit(5), urban, oneway=-1)]

    def test_carriageway_split_is_no_junction(self):
        """A two-way road splitting into its two one-way carriageways is not decided: no German rows, no US
        fallback; controls gives every arm 0. A one-way pair of another class onto a two-way road stays decided."""
        for cls in ("secondary", "tertiary", "residential"):
            for urban in (True, False):
                rs = self.split(cls, urban)
                self.assertEqual(self.props(rs, signals=self.NO_SIGNALS), [], (cls, urban))
                arms = next(a for a in furniture._arms(rs).values() if len(a) == 3)
                self.assertEqual([w for _, w, _ in designs.controls(arms, 0, signals=self.NO_SIGNALS)], [0, 0, 0])
        rs = self.split("residential", True, side_cls="secondary")
        self.assertTrue(self.props(rs, signals=self.NO_SIGNALS), "a real T: the one-way pair meets another road")

    def test_town_is_the_land_cover_s(self):
        """build_tile draws an arm rural for drawing reasons (a short ring approach, a divided primary built up on
        one side, a connector): the town test reads the land cover's town (the context's), every arm's."""
        town = self.ctx(town={"e": True, "w": True, "s": True})
        rs = [self.road("e", "secondary", "e", False), self.road("w", "secondary", "w"),
              self.road("s", "residential", "s")]
        self.assertEqual(self.placed(self.props(rs, town)), [("de_z205", "s", "before"), ("de_z306", "e", "before"),
                                                             ("de_z306", "w", "before")])
        rs = [self.road("e", "unclassified", "e", False), self.road("w", "unclassified", "w"),
              self.road("s", "residential", "s")]
        self.assertEqual(self.props(rs, town), [], "in town: right before left")
        self.assertEqual(self.placed(self.props(self.end(True, w=False), town)),
                         [("de_z205", "s", "before"), ("de_z301", "e", "before"), ("de_z306", "w", "before")],
                         "no 307 in town")
        self.assertTrue(designs.in_town([a for a in furniture._arms(self.stretch_t()).values() if len(a) == 3][0]),
                        "no context: spec.urban")

    def test_probe_is_the_signs_own_height(self):
        seen = []

        class Rec:
            def clear(self, row, margin):
                seen.append(row[6])
                return True
        for g in ("de_z205_p3_g3", furniture.G_STOP, furniture.G_SIGNAL):
            furniture._off_carriageways(np.zeros(2), np.array([0.0, 1.0]), np.array([1.0, 0.0]), 2.0, 4.0, [], g,
                                        clearance=Rec())
        self.assertEqual(seen, [designs.HEIGHTS["de_z205_p3_g3"], 3.0, 9.4])


class BuildTileTest(Placed, unittest.TestCase):
    """roads.build_tile reads the tile's roads once (designs.context: their routes' refs, the land cover's town) and
    hands them to furniture.place."""

    def tee(self, frame, cls="secondary", ring_arm=False):
        """A T round (100, 100): w and e (cls), s residential; ring_arm: e tagged a roundabout (roads._rings draws it
        rural)."""
        lines = [[(20.0, 100.0), (100.0, 100.0)], [(100.0, 100.0), (180.0, 100.0)], [(100.0, 100.0), (100.0, 20.0)]]
        coords, n = [], len(lines)
        for ln in lines:
            e, nn = np.array(ln).T
            lat, lon, _ = frame.enu_to_geodetic(e, nn, 0.0)
            coords += list(zip(np.asarray(lon), np.asarray(lat)))
        attrs = {"class": np.array([cls, cls, "residential"]), "subtype": np.array(["road"] * 3)}
        if ring_arm:
            attrs["osm_junction"] = np.array([None, "roundabout", None], object)
        return mapgeom.Features(layer="roads", ids=np.array(["w", "e", "s"]), kind=np.full(n, mapgeom.LINE, np.uint8),
                                part_off=np.arange(n + 1, dtype=np.int64), ring_off=np.arange(n + 1, dtype=np.int64),
                                coord_off=np.arange(n + 1, dtype=np.int64) * 2, lonlat=np.array(coords, np.float64),
                                attrs=attrs, rules={})

    def build(self, frame, de_on, feats=None, urban=None, routes=()):
        props = []
        roads.build_tile(frame, 0, 0, [feats or self.tee(frame)], lambda la, lo: np.zeros(np.shape(la)), urban,
                         walks=[], props=props, de_on=de_on, de_routes=routes)
        return [r for r in props if r[0].startswith("de_")] if de_on else props

    def test_german_signs_in_the_road_tile(self):
        frame = JunctionGlueTest.FRAME
        on = [r[0] for r in self.build(frame, True)]
        self.assertEqual(sorted(on), ["de_z205", "de_z306", "de_z306"])
        self.assertFalse([r for r in self.build(frame, False) if r[0].startswith("de_")])

    def test_no_context_far_from_germany(self):
        frame = geodesy.EnuFrame(39.2834, -76.5934, 0.0)        # Baltimore
        with mock.patch.object(designs, "context", side_effect=AssertionError("built")):
            self.assertEqual(self.build(frame, True), [])
            self.assertEqual(roads.build_tile(frame, 0, 0, [self.tee(frame)], lambda la, lo: np.zeros(np.shape(la)),
                                              walks=[], props=[], de_on=True), roads.build_tile(
                frame, 0, 0, [self.tee(frame)], lambda la, lo: np.zeros(np.shape(la)), walks=[], props=[]))

    def test_a_kreisstrasse_is_a_stretch(self):
        """The cells' road_routes give the tertiary through the T its ref (K 12, a district code M 12): a stretch
        (306), where without them the class alone ranks it (301)."""
        frame = JunctionGlueTest.FRAME
        tee = self.tee(frame, "tertiary")
        self.assertEqual(sorted(r[0] for r in self.build(frame, True, tee)), ["de_z205", "de_z301", "de_z301"])
        for ref in ("K 12", "M 12"):
            routes = _features("road_routes", ["w", "e"], routes_txt=[f"regional|{ref}|0|1"] * 2,
                               **{"class": ["tertiary", "tertiary"]})
            self.assertEqual(sorted(r[0] for r in self.build(frame, True, tee, routes=[routes])),
                             ["de_z205", "de_z306", "de_z306"], ref)

    def test_town_before_the_drawing(self):
        """An arm build_tile draws rural for a drawing reason (here a mapped roundabout's way, roads._rings) still
        counts in town by its land cover: the 306s stand before the junction."""
        frame = JunctionGlueTest.FRAME
        town = lambda la, lo: np.ones(np.shape(la), bool)       # noqa: E731
        feats = self.tee(frame, ring_arm=True)
        self.assertFalse(roads.road_specs(feats, town)[1].urban, "(drawn rural)")
        self.assertEqual(roads.road_towns(feats, town), [True, True, True])
        self.assertEqual(self.placed(self.build(frame, True, feats, town)),
                         [("de_z205", "s", "before"), ("de_z306", "e", "before"), ("de_z306", "w", "before")])
        self.assertEqual(roads.road_towns(self.tee(frame)), [False] * 3, "no land cover: rural")


def _everywhere(la, lo):
    """An urban_fn of one identity (as a server's own land cover): in town everywhere."""
    return np.ones(np.shape(la), bool)


class FactsCacheTest(Placed, unittest.TestCase):
    """The land cover's towns with the facts cache (roads._FACTS_MEM: a layer of a map cell, _bnge_cell, as mapdata
    decodes it): a cell's specs and their towns live and die together, so no eviction or reload changes a tile's German
    rows."""

    def layer(self):
        f = BuildTileTest().tee(JunctionGlueTest.FRAME, ring_arm=True)
        f.__dict__["_bnge_cell"] = ("15/17408/11300", "roads", "v1")
        return f

    def build(self, feats):
        props = []
        roads.build_tile(JunctionGlueTest.FRAME, 0, 0, [feats], lambda la, lo: np.zeros(np.shape(la)), _everywhere,
                         walks=[], props=props, de_on=True)
        return self.placed([r for r in props if r[0].startswith("de_")])

    def test_evictions_and_reloads_keep_the_rows(self):
        with mock.patch.dict(roads._FACTS_MEM, clear=True):
            first = self.build(self.layer())
            self.assertEqual(first, [("de_z205", "s", "before"), ("de_z306", "e", "before"),
                                     ("de_z306", "w", "before")], "in town by the land cover")
            kept = len(roads._FACTS_MEM)
            with mock.patch.object(roads, "FACTS_MEM_KEPT", kept):
                for i in range(4 * kept):           # (now and then another cell's fact in, the oldest out; the cell
                    if i % 3 == 0:                  # reloaded every time: its facts remade evict its others)
                        with roads._FACTS_LOCK:
                            roads._FACTS_MEM[("another cell", i)] = [i]
                            while len(roads._FACTS_MEM) > roads.FACTS_MEM_KEPT:
                                roads._FACTS_MEM.popitem(last=False)
                    f = self.layer()
                    roads.road_specs(f, _everywhere)
                    self.assertIsNotNone(roads.road_towns(f, _everywhere), f"towns with the specs ({i})")
                    self.assertEqual(self.build(self.layer()), first, i)


    def test_any_one_entry_evicted(self):
        """Every _FACTS_MEM writer trims the oldest entry, any of this cell's: with any one of them gone, the cell
        reloaded still has its towns with its specs, and the same rows (also where the specs are kept and the towns
        trimmed)."""
        with mock.patch.dict(roads._FACTS_MEM, clear=True):
            first = self.build(self.layer())
            for key in list(roads._FACTS_MEM):
                with mock.patch.dict(roads._FACTS_MEM):
                    roads._FACTS_MEM.pop(key)
                    f = self.layer()
                    roads.road_specs(f, _everywhere)
                    self.assertIsNotNone(roads.road_towns(f, _everywhere), key)
                    self.assertEqual(self.build(self.layer()), first, key)


class RoutesReadTest(unittest.TestCase):
    """blockworker._de_routes: the cells' road_routes layers for the ref ranks; a layer that fails or is missing gives
    none, never an error."""

    def test_missing_or_failing_layers(self):
        import blockworker
        from types import SimpleNamespace
        layer = _features("road_routes", ["a"], routes_txt=["regional|K 12|0|1"])

        class Md:
            def cell(self, key, layers=(), kind=None):
                self.asked = getattr(self, "asked", []) + [(key, layers, kind)]
                if key == "boom":
                    raise OSError("no network")
                return SimpleNamespace(layers={"road_routes": layer} if key == "has" else {})
        md = Md()
        self.assertEqual(blockworker._de_routes(md, ["has", "none", "boom"]), [layer])
        self.assertEqual(md.asked[0], ("has", ("road_routes",), "demand"))


# today's junction paint of test_roads' US 4-way (test_junction_corners_crosswalks_ramps) at Schrobenhausen, country DE,
# no signal mapped: (material, width, texture length, ends) of its crosswalks and stop lines, recorded from the road
# tiles before Germany's paint (literals, no git show); "stretch": its west and east arms secondary
TODAY_PAINT = {
    "residential": [("crossing_white", 3.0, 4.8, [[125.3, 109.7], [114.7, 109.7]]),
                    ("crossing_white", 3.0, 4.8, [[130.3, 125.3], [130.3, 114.7]]),
                    ("crossing_white", 3.0, 4.8, [[114.7, 130.3], [125.3, 130.3]]),
                    ("crossing_white", 3.0, 4.8, [[109.7, 114.7], [109.7, 125.3]]),
                    ("line_white", 0.45, 6.4, [[120.2, 106.97], [125.0, 106.97]]),
                    ("line_white", 0.45, 6.4, [[133.03, 120.2], [133.03, 125.0]]),
                    ("line_white", 0.45, 6.4, [[119.8, 133.03], [115.0, 133.03]]),
                    ("line_white", 0.45, 6.4, [[106.97, 119.8], [106.97, 115.0]])],
    "stretch": [("crossing_white", 3.0, 4.8, [[125.3, 105.95], [114.7, 105.95]]),
                ("crossing_white", 3.0, 4.8, [[133.3, 126.05], [133.3, 113.95]]),
                ("crossing_white", 3.0, 4.8, [[114.7, 134.05], [125.3, 134.05]]),
                ("crossing_white", 3.0, 4.8, [[106.7, 113.95], [106.7, 126.05]]),
                ("line_white", 0.45, 6.4, [[120.2, 103.22], [125.0, 103.22]]),
                ("line_white", 0.45, 6.4, [[136.03, 120.2], [136.03, 125.75]]),
                ("line_white", 0.45, 6.4, [[119.8, 136.78], [115.0, 136.78]]),
                ("line_white", 0.45, 6.4, [[103.97, 119.8], [103.97, 114.25]])],
}


def _flat(la, lo):
    return np.full(np.shape(la), 10.0)


class MarkingsTest(unittest.TestCase):
    """Germany's junction paint in the road tiles (roads.build_tile, de_on, the junction inside Germany), as its signs
    say (one designs.controls decision for both): test_roads' US 4-way (four streets meeting at X, 180 m each) at
    Schrobenhausen. Wartelinie (Z 341) at 205, Haltlinie (Z 294) at 206, nothing at right before left, zebras only
    where mapped, the kerb ramps kept, signalised junctions as today."""
    FRAME = JunctionGlueTest.FRAME
    X = (120.0, 120.0)
    ENDS = {"e": (300.0, 120.0), "n": (120.0, 300.0), "w": (-60.0, 120.0), "s": (120.0, -60.0)}
    DIRS = {"e": (1, 0), "n": (0, 1), "w": (-1, 0), "s": (0, -1)}
    NO_SIGNALS = np.zeros((0, 2))
    RES = dict.fromkeys("enws", "residential")
    STRETCH = {"e": "secondary", "w": "secondary", "n": "residential", "s": "residential"}

    def layer(self, classes, at=(0.0, 0.0), lines=None):
        """The 4-way's roads layer, classes {arm: class}, moved by at; lines: [(fid, class, [(x, y) ...])] instead."""
        lines = lines or [(f, c, [self.X, self.ENDS[f]]) for f, c in classes.items()]
        coords, offs = [], [0]
        for _, _, ln in lines:
            e, n = (np.array(ln, np.float64) + at).T
            lat, lon, _ = self.FRAME.enu_to_geodetic(e, n, 0.0)
            coords += list(zip(np.asarray(lon), np.asarray(lat)))
            offs.append(len(coords))
        k = len(lines)
        return mapgeom.Features(layer="roads", ids=np.array([f for f, _, _ in lines]),
                                kind=np.full(k, mapgeom.LINE, np.uint8), part_off=np.arange(k + 1, dtype=np.int64),
                                ring_off=np.arange(k + 1, dtype=np.int64), coord_off=np.array(offs, np.int64),
                                lonlat=np.array(coords, np.float64),
                                attrs={"class": np.array([c for _, c, _ in lines]), "subtype": np.array(["road"] * k)},
                                rules={})

    def build(self, feats, de_on=True, signals=NO_SIGNALS, country="DE", props=None, nodes=None, tile=(0, 0)):
        walks = []
        ch = roads.build_tile(self.FRAME, tile[0], tile[1], [feats], _flat, _everywhere, walks=walks, signals=signals,
                              country=country, props=props, nodes=nodes, de_on=de_on)
        return ch, walks

    @staticmethod
    def paint(ch):
        """junctions.find's crosswalks and US stop lines, as TODAY_PAINT has them."""
        import junctions
        return [(c["m"], c["w"], c["tl"], [[round(v, 2) for v in p[:2]] for p in c["n"]]) for c in ch
                if c["m"] == junctions.CROSSWALK["m"]
                or (c["m"] == junctions.STOP_LINE["m"] and c["w"] == junctions.STOP_LINE["w"] and c.get("n"))]

    @staticmethod
    def z341(ch):
        return [np.array(c["poly"]) for c in ch if c.get("layer") == "mark" and c["m"] == "line_white"
                and len(c["poly"]) == 4]

    @staticmethod
    def z294(ch):
        return [np.array(c["n"]) for c in ch if c["m"] == "line_white" and c["w"] == 0.5 and len(c.get("n", ())) == 2]

    @staticmethod
    def ramps(walks):
        return sum(len(w["ramps"]) for w in walks if "corner" not in w) + \
            sum(len(w["corner"]["ramps"]) for w in walks if "corner" in w)

    def arm(self, p, centre=None):
        """The arm of the 4-way a point lies on (its direction from X), and m along it from X."""
        v = np.asarray(p, np.float64)[:2] - (centre if centre is not None else self.X)
        d = max(self.DIRS, key=lambda k: float(np.dot(self.DIRS[k], v)))
        return d, float(np.dot(self.DIRS[d], v))

    def sets(self, dashes, centre=None):
        """{arm: number of Z 341 dashes on it}."""
        out = {}
        for q in dashes:
            d = self.arm(q.mean(0), centre)[0]
            out[d] = out.get(d, 0) + 1
        return out

    def wide(self, feats):
        """Half the widest arm's width: the crossing road's edge, which the line stands 1 m from."""
        return max(sp.width for sp in roads.road_specs(feats, _everywhere) if sp is not None) / 2

    def per_set(self, feats, fid):
        """Whole dashes across the half of arm fid coming in (0.2 m off the middle, 0.3 m off the edge)."""
        sp = roads.road_specs(feats, _everywhere)[list(feats.ids).index(fid)]
        return int((sp.width / 2 - 0.5 + 0.25) // 0.75)

    def test_rvl_has_no_paint(self):
        ch, walks = self.build(self.layer(self.RES), props=[])
        self.assertFalse(self.paint(ch), "no crossing_white, no US stop line")
        self.assertFalse(self.z341(ch))
        self.assertFalse(self.z294(ch))
        self.assertGreaterEqual(self.ramps(walks), 8, "the kerb ramps as today")
        self.assertEqual(sum(1 for w in walks if "corner" in w), 4)

    def test_give_way_has_wait_line(self):
        feats = self.layer(self.STRETCH)
        props = []
        ch, walks = self.build(feats, props=props)
        self.assertFalse(self.paint(ch), "no crosswalk, no US stop line")
        dashes = self.z341(ch)
        self.assertEqual(self.sets(dashes), {"n": self.per_set(feats, "n"), "s": self.per_set(feats, "s")},
                         "one Z 341 set on each minor arm")
        signs = {self.arm(r[1:3])[0] for r in props if r[0].startswith("de_z205")}
        self.assertEqual(signs, {"n", "s"}, "where the 205s stand")
        at = self.wide(feats) + 1.0
        for q in dashes:
            d, along = self.arm(q.mean(0))
            self.assertAlmostEqual(along, at, delta=0.002, msg="1 m from the crossing road's edge (chunks: mm)")
            self.assertAlmostEqual(float(np.ptp(q[:, 0])), 0.5, delta=0.002)
            self.assertAlmostEqual(float(np.ptp(q[:, 1])), 0.5, delta=0.002)
            across = float(q.mean(0)[0] - self.X[0])
            self.assertGreater(across if d == "s" else -across, 0.0, "on the half coming in")
        self.assertGreaterEqual(self.ramps(walks), 8)

    def test_lines_end_at_the_wait_line(self):
        """The stem's centre line (3 m dashes, 6 m gaps from its far end: drawn toward the junction, one spans the
        Wartelinie's spot) ends at the Wartelinie, not where today's crosswalk was."""
        lines = [(f, c, [self.X, self.ENDS[f]]) for f, c in (("e", "secondary"), ("w", "secondary"), ("n", "tertiary"))]
        feats = self.layer(None, lines=lines + [("s", "tertiary", [self.ENDS["s"], self.X])])
        at, half = self.wide(feats) + 1.0, roads.road_specs(feats, _everywhere)[list(feats.ids).index("s")].width / 2

        def start(ch):
            stem = [p for c in ch if c["m"] == "line_white" and c["w"] < 0.3 and c.get("n")
                    for p in c["n"] if abs(p[0] - self.X[0]) < half and self.X[1] - p[1] > self.wide(feats)]
            self.assertTrue(stem, "the stem has lines")
            return min(self.X[1] - p[1] for p in stem)
        self.assertGreater(start(self.build(feats, False)[0]), at + 3.0, "(today: from past its crosswalk)")
        self.assertAlmostEqual(start(self.build(feats)[0]), at, delta=0.01)

    def test_stop_has_a_haltlinie(self):
        """A 206 (from the map's stops; here controls patched to give it the stem): a Haltlinie 0.50 m wide
        (Z 294, RMS 4.6; VwV-StVO zu Zeichen 206 Rn. 4) where its 206 stands, the Wartelinie on the other minor arm."""
        real = designs.controls

        def stop_on_s(arms, *a, **kw):
            return [(x, designs.Z206 if x[0] == "s" and w == designs.Z205 else w, p)
                    for x, w, p in real(arms, *a, **kw)]
        feats = self.layer(self.STRETCH)
        props = []
        with mock.patch.object(designs, "controls", stop_on_s):
            ch, _ = self.build(feats, props=props)
        self.assertEqual({self.arm(r[1:3])[0]: r[0] for r in props if r[0].startswith(("de_z205", "de_z206"))},
                         {"n": "de_z205", "s": "de_z206"})
        halt = self.z294(ch)
        self.assertEqual(len(halt), 1)
        d, along = self.arm(halt[0].mean(0))
        self.assertEqual(d, "s")
        self.assertAlmostEqual(along, self.wide(feats) + 1.0, delta=0.002)
        self.assertGreater(float(halt[0][:, 0].min()), self.X[0], "across the half coming in")
        self.assertEqual(set(self.sets(self.z341(ch))), {"n"})
        self.assertFalse(self.paint(ch))

    def test_one_decision_for_signs_and_paint(self):
        """The tile decides each junction once (designs.decide): its signs (furniture.junction_props) take that answer,
        no second kernel call."""
        real, calls = designs.controls, []

        def counted(arms, k, *a, **kw):
            calls.append(k)
            return real(arms, k, *a, **kw)
        props = []
        with mock.patch.object(designs, "controls", counted):
            self.build(self.layer(self.STRETCH), props=props)
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(sorted(r[0] for r in props if r[0].startswith("de_")), ["de_z205", "de_z205", "de_z306",
                                                                               "de_z306"])

    def test_signalised_keeps_today(self):
        at = np.array([self.X])
        for name, classes in (("residential", self.RES), ("stretch", self.STRETCH)):
            ch, walks = self.build(self.layer(classes), signals=at)
            self.assertEqual(self.paint(ch), TODAY_PAINT[name], name)
            self.assertEqual(self.paint(ch), self.paint(self.build(self.layer(classes), False, signals=at)[0]), name)
            self.assertFalse(self.z341(ch))
            self.assertFalse(self.z294(ch))
            self.assertGreaterEqual(self.ramps(walks), 8)

    def test_a_junction_near_signals_is_its_own(self):
        """J1, a signalised crossroads (signals mapped at its node and 12 m up its east approach), and J2 46 m east of
        it (secondary through, residential N-S): J2's crosswalk and stop line lie within SIGNAL_NEAR_M of a signal, but
        J2 has no signals (furniture._is_signalled: none within its reach), so they go with its 205s; J1 keeps
        today's."""
        j2 = (166.0, 120.0)
        lines = [("w", "secondary", [self.X, (-60.0, 120.0)]), ("n", "secondary", [self.X, (120.0, 300.0)]),
                 ("s", "secondary", [self.X, (120.0, -60.0)]), ("e1", "secondary", [self.X, j2]),
                 ("e2", "secondary", [j2, (300.0, 120.0)]), ("n2", "residential", [j2, (166.0, 300.0)]),
                 ("s2", "residential", [j2, (166.0, -60.0)])]
        feats = self.layer(None, lines=lines)
        signals = np.array([self.X, (132.0, 120.0)])
        today = self.paint(self.build(feats, False, signals=signals)[0])
        at_j2 = [p for p in today if np.hypot(*(np.mean(p[3], 0) - j2)) < np.hypot(*(np.mean(p[3], 0) - self.X))]
        self.assertTrue(any(np.hypot(*(signals - np.mean(p[3], 0)).T).min() < furniture.SIGNAL_NEAR_M for p in at_j2),
                        "(J2's paint within SIGNAL_NEAR_M of a signal)")
        props = []
        ch, _ = self.build(feats, signals=signals, props=props)
        self.assertEqual(self.paint(ch), [p for p in today if p not in at_j2], "J1 as today, J2's gone")
        self.assertEqual(self.sets(self.z341(ch), j2), {"n": self.per_set(feats, "n2"), "s": self.per_set(feats, "s2")})
        near_j2 = [r[0] for r in props if np.hypot(r[1] - j2[0], r[2] - j2[1]) < 20]
        self.assertNotIn(furniture.G_SIGNAL, near_j2, "(no signal heads at J2)")
        self.assertIn("de_z205", near_j2)

    def test_off_is_todays(self):
        """The setting off: today's chunks (the recorded paint, test_roads' counts); on, outside Germany: the same
        chunks and sidewalks byte for byte."""
        nowhere = lambda lon, lat: np.zeros(np.size(lon), bool)     # noqa: E731
        for name, classes in (("residential", self.RES), ("stretch", self.STRETCH)):
            feats = self.layer(classes)
            off, walks = self.build(feats, False)
            self.assertEqual(self.paint(off), TODAY_PAINT[name], name)
            self.assertEqual(self.ramps(walks), 8, name)
            self.assertFalse(self.z341(off))
            default = roads.build_tile(self.FRAME, 0, 0, [feats], _flat, _everywhere, walks=[], signals=self.NO_SIGNALS,
                                       country="DE")
            self.assertEqual(default, off, name)
            with mock.patch.object(germany, "contains", nowhere):
                on, on_walks = self.build(feats)
            self.assertEqual(on, off, name)
            self.assertEqual(repr(on_walks), repr(walks), name)

    def test_roundabout_entries_have_a_wait_line(self):
        """A roundabout's entries are 205 arms (Z 341 at a Kreisverkehr's entry); today nothing is painted there
        (no US crosswalk: the ring has no sidewalks; Sweden's M14 only in Sweden)."""
        import math
        c = np.array(self.X)

        def arc(k):
            t = np.linspace(k * math.pi / 3, (k + 1) * math.pi / 3, 9)
            return [tuple(c + 18 * np.array([math.cos(a), math.sin(a)])) for a in t]
        lines = [(f"r{k}", "tertiary", arc(k)) for k in range(6)]
        lines += [("east", "secondary", [tuple(c + (18, 0)), tuple(c + (140, 0))]),
                  ("west", "residential", [tuple(c - (18, 0)), tuple(c - (140, 0))])]
        feats = self.layer(None, lines=lines)
        off, _ = self.build(feats, False)
        self.assertFalse(self.z341(off), "today: nothing at the entries")
        self.assertFalse(self.paint(off))
        ch, _ = self.build(feats)
        got = {}
        for q in self.z341(ch):
            side = "east" if q.mean(0)[0] > c[0] else "west"
            got[side] = got.get(side, 0) + 1
            self.assertGreater(abs(float(q.mean(0)[0] - c[0])), 18.0, "outside the ring")
        self.assertEqual(sorted(got), ["east", "west"], "a Wartelinie on each entry")

    def test_mapped_zebras_only(self):
        """nodes (secrossings.nodes; Germany reads its de_crossings, Sweden's codes are not its): a mapped zebra on the
        stem is painted Z 293 (bars 0.50 m, 0.50 m apart: Sweden's M15_LOOK) and its kerb ramps added to the junction's;
        one near signals is not (VwV-StVO zu § 26 Rn. 5), nor an unmarked one (its ramps are), nor Sweden's M16
        squares; outside Germany none."""
        import secrossings
        feats = self.layer(self.STRETCH)
        nodes = {"crossings": [(120.0, 80.0, secrossings.M15), (120.0, 190.0, secrossings.M15),
                               (40.0, 120.0, 0), (200.0, 120.0, secrossings.M16)],
                 "de_crossings": [(120.0, 80.0, secrossings.M15), (120.0, 190.0, secrossings.M15),
                                  (40.0, 120.0, 0), (200.0, 120.0, 0)],
                 "stop_lines": np.zeros((0, 2)), "priority": np.zeros((0, 3))}
        signals = np.array([(120.0, 200.0)])
        ch, walks = self.build(feats, signals=signals, nodes=nodes)
        zebra = [c for c in ch if c["m"] == "crossing_white"]
        self.assertEqual(len(zebra), 1, "the stem's, not the one under signals, the unmarked or the cycle crossing")
        self.assertEqual((zebra[0]["tl"], zebra[0]["w"]), (secrossings.M15_LOOK["tl"], 3.0))
        self.assertAlmostEqual(float(np.mean(np.array(zebra[0]["n"])[:, 1])), 80.0, 1)
        self.assertEqual(self.sets(self.z341(ch)), {"n": self.per_set(feats, "n"), "s": self.per_set(feats, "s")},
                         "(no M16 squares)")
        base = self.ramps(self.build(feats, signals=signals)[1])
        self.assertEqual(self.ramps(walks), base + 8, "two ramps for each of the four mapped crossings, added")
        nowhere = lambda lon, lat: np.zeros(np.size(lon), bool)     # noqa: E731
        with mock.patch.object(germany, "contains", nowhere):
            out, _ = self.build(feats, signals=signals, nodes=nodes)
        self.assertEqual(out, self.build(feats, False, signals=signals, nodes=nodes)[0], "outside Germany: as today")

    def test_zebras_only_in_town_at_50(self):
        """VwV-StVO zu § 26 Rn. 1: a Fußgängerüberweg only in town and on a road of at most 50 km/h: the stem's mapped
        zebra is painted in town with no limit mapped (the town's 50) or with 50, not at 70 nor out of town (in town its
        kerb ramps all the same)."""
        import secrossings
        from test_roads import rules
        nodes = {"crossings": [(120.0, 80.0, secrossings.M15)], "de_crossings": [(120.0, 80.0, secrossings.M15)],
                 "stop_lines": np.zeros((0, 2)), "priority": np.zeros((0, 3))}
        nowhere = lambda la, lo: np.zeros(np.shape(la), bool)      # noqa: E731
        for town, kmh, want in ((_everywhere, None, 1), (_everywhere, 50.0, 1), (_everywhere, 70.0, 0),
                                (nowhere, None, 0), (nowhere, 50.0, 0)):
            feats = self.layer(self.STRETCH)
            if kmh is not None:
                feats.rules["max_speed_kmh"] = rules([(list(feats.ids).index("s"), kmh)], "float")
            got = {}
            for name, n in (("with", nodes), ("without", None)):
                walks = []
                ch = roads.build_tile(self.FRAME, 0, 0, [feats], _flat, town, walks=walks, signals=self.NO_SIGNALS,
                                      country="DE", nodes=n, de_on=True)
                got[name] = (len([c for c in ch if c["m"] == "crossing_white"]), self.ramps(walks))
            case = (town is _everywhere, kmh)
            self.assertEqual(got["with"][0], want, case)
            if town is _everywhere:     # (out of town no sidewalks, so no kerb ramps at all)
                self.assertEqual(got["with"][1], got["without"][1] + 2, ("its kerb ramps",) + case)

    def infra(self, rows):
        """An infrastructure layer (Overture's) of points rows [(class, frame (x, y), OSM tags)]."""
        import json
        n = len(rows)
        xy = np.array([r[1] for r in rows], np.float64)
        lat, lon, _ = self.FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        return mapgeom.Features(layer="infrastructure", ids=np.array([f"n{i}" for i in range(n)]),
                                kind=np.full(n, mapgeom.POINT, np.uint8), part_off=np.arange(n + 1, dtype=np.int64),
                                ring_off=np.arange(n + 1, dtype=np.int64), coord_off=np.arange(n + 1, dtype=np.int64),
                                lonlat=np.column_stack([np.asarray(lon), np.asarray(lat)]),
                                attrs={"class": np.array([r[0] for r in rows], object),
                                       "subtype": np.array(["transportation"] * n, object),
                                       "tags": np.array([json.dumps(r[2]) for r in rows], object)}, rules={})

    def test_germanys_crossings_by_their_tags(self):
        """From the map's tags through secrossings.nodes to junction_paint: a crossing on the stem 40 m out is
        Z 293 only where it is a marked one without signals (crossing=zebra, crossing_ref=zebra, crossing:markings=zebra
        or yes, crossing=marked or uncontrolled); a bare highway=crossing, a refuge island, an unmarked one or one at
        signals (its own tag, or a signal mapped 10 m off) gets none, its kerb ramps all the same."""
        import secrossings
        feats = self.layer(self.STRETCH)
        base = self.ramps(self.build(feats)[1])
        for tags, near, want in (({"crossing": "zebra"}, False, 1), ({"crossing_ref": "zebra"}, False, 1),
                                 ({"crossing:markings": "zebra"}, False, 1), ({"crossing:markings": "yes"}, False, 1),
                                 ({"crossing": "marked"}, False, 1), ({"crossing": "uncontrolled"}, False, 1),
                                 ({}, False, 0), ({"crossing": "island"}, False, 0),
                                 ({"crossing": "unmarked"}, False, 0), ({"crossing": "traffic_signals"}, False, 0),
                                 ({"crossing": "zebra"}, True, 0)):
            rows = [("crossing", (120.0, 80.0), tags)] + ([("traffic_signals", (120.0, 70.0), {})] if near else [])
            nodes = secrossings.nodes(self.FRAME, [self.infra(rows)])
            ch, walks = self.build(feats, nodes=nodes)
            zebra = [c for c in ch if c["m"] == "crossing_white"]
            self.assertEqual(len(zebra), want, (tags, near))
            self.assertEqual(self.ramps(walks), base + 2, (tags, near))
            if want:
                self.assertEqual(zebra[0]["tl"], secrossings.M15_LOOK["tl"])

    def test_mapped_stop_and_give_way_in_the_road_tile(self):
        """The map's rows reach the tile's one decision (designs.context's mapped): Overture's stop 12 m out on S makes
        S a 206 with its Haltlinie (and the others no right before left: W - E 301, N 205 with its Wartelinie), the
        lane shards' 205 node 12 m out on E a 205 with its Wartelinie (W and N, the two left, 301); with the setting
        off, the same nodes change nothing."""
        import railtags
        import secrossings
        feats = self.layer(self.RES)
        nodes = secrossings.nodes(self.FRAME, [self.infra([("stop", (120.0, 108.0), {})])])
        props = []
        ch, _ = self.build(feats, props=props, nodes=nodes)
        self.assertEqual({self.arm(r[1:3])[0]: r[0] for r in props if r[0].startswith("de_z")},
                         {"s": "de_z206", "n": "de_z205", "e": "de_z301", "w": "de_z301"})
        halt = self.z294(ch)
        self.assertEqual([self.arm(h.mean(0))[0] for h in halt], ["s"])
        self.assertEqual(set(self.sets(self.z341(ch))), {"n"})
        lat, lon, _ = self.FRAME.enu_to_geodetic(np.array(132.0), np.array(120.0), 0.0)
        feats.__dict__["_sign_nodes"] = [(float(lon), float(lat), {"traffic_sign": "DE:205,1002-12"})]
        self.assertEqual(len(railtags.sign_nodes(feats)), 1)
        props = []
        ch, _ = self.build(feats, props=props, nodes=nodes)
        self.assertEqual({self.arm(r[1:3])[0]: r[0] for r in props if r[0].startswith("de_z")},
                         {"s": "de_z206", "e": "de_z205", "n": "de_z301", "w": "de_z301"})
        self.assertEqual(set(self.sets(self.z341(ch))), {"e"})
        self.assertEqual([self.arm(h.mean(0))[0] for h in self.z294(ch)], ["s"])
        off = self.build(feats, False, nodes=nodes)[0]
        self.assertEqual(off, self.build(self.layer(self.RES), False)[0], "off: as today")

    def test_a_junction_at_a_tile_edge(self):
        """The junction 2 m west of the tiles' seam: its stem's Wartelinie straddles it; each dash is drawn by one tile
        (the one that owns its middle, as today's crosswalks), none twice, none lost."""
        shift = (roads.TILE - 2.0 - self.X[0], 0.0)
        feats = self.layer(self.STRETCH, at=shift)
        centre = np.array(self.X) + shift
        a = self.z341(self.build(feats)[0])
        b = self.z341(self.build(feats, tile=(1, 0))[0])
        sa = {self.arm(q.mean(0), centre)[0] for q in a}
        self.assertTrue(a and b, "both tiles draw some")
        mids = sorted(tuple(np.round(q.mean(0)[:2], 3)) for q in a + b)
        self.assertEqual(len(mids), len(set(mids)), "none twice")
        self.assertEqual(self.sets(a + b, centre), {"n": self.per_set(feats, "n"), "s": self.per_set(feats, "s")})
        self.assertIn("s", sa)

    def test_line_style_from_the_border(self):
        """markings.style by Germany's border (de_on): a French country code inside Germany paints Germany's lines (a
        town's centre line 3 m dashes, not France's 1.5 m); off, or outside Germany, the code's."""
        feats = self.layer(None, lines=[("road", "secondary", [(-60.0, 120.0), (300.0, 120.0)])])

        def centre(ch):
            return {round(float(np.hypot(*np.subtract(c["n"][-1], c["n"][0])[:2])), 1) for c in ch
                    if c["m"] == "line_white" and c["w"] == 0.12 and c.get("n")
                    and abs(np.mean(np.array(c["n"])[:, 1]) - 120.0) < 0.5}
        on = centre(self.build(feats, country="FR")[0])
        off = centre(self.build(feats, False, country="FR")[0])
        self.assertIn(3.0, on)
        self.assertNotIn(1.5, on)
        self.assertIn(1.5, off)
        nowhere = lambda lon, lat: np.zeros(np.size(lon), bool)     # noqa: E731
        with mock.patch.object(germany, "contains", nowhere):
            self.assertEqual(self.build(feats, country="FR")[0], self.build(feats, False, country="FR")[0])
