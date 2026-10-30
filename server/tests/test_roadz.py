"""Real road heights (roadz.py, earthfast roadz kernels; docs/research/2026-10-28-3d-road-data.md): source parsing,
matching a map road to a source's 3D lines, profile cleaning, the lidar deck fit, the datum fit, and the overrides in
BridgeProfiles / TunnelProfiles / grading with today's inference as the fallback."""
import os
import struct
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import fast  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import roads  # noqa: E402
import roadz  # noqa: E402
from test_approaches import overpass, flat_elev, FLAT  # noqa: E402
from test_roads import FRAME  # noqa: E402
from test_grading import FakeMD  # noqa: E402

ef = fast._ef
NATIVE = fast.AVAILABLE and getattr(ef, "VERSION", 0) >= roadz.NATIVE


class ParseTest(unittest.TestCase):
    def test_ign(self):
        feats = [
            {"properties": {"position_par_rapport_au_sol": "1", "cpx_numero": "A75", "fictif": False},
             "geometry": {"type": "LineString", "coordinates": [[3.02, 44.07, 676.1], [3.021, 44.071, -1000]]}},
            {"properties": {"position_par_rapport_au_sol": "-1", "nom_collaboratif_gauche": "Tunnel X"},
             "geometry": {"type": "MultiLineString", "coordinates": [[[3.0, 44.0, 10.0], [3.001, 44.0, 11.0]]]}},
            {"properties": {"position_par_rapport_au_sol": "0", "fictif": True},
             "geometry": {"type": "LineString", "coordinates": [[3.0, 44.0, 1.0], [3.1, 44.0, 2.0]]}},
        ]
        rows = roadz.parse_ign(feats)
        self.assertEqual(len(rows), 2, "the fictitious link is left out")
        ll, z, lvl, ref, name = rows[0]
        self.assertEqual((lvl, ref), (1, "A75"))
        self.assertAlmostEqual(z[0], 676.1)
        self.assertTrue(np.isnan(z[1]), "Z -1000 is none")
        self.assertEqual((rows[1][2], rows[1][4]), (-1, "Tunnel X"))

    def test_nvdb(self):
        objs = [{"geometri": {"wkt": "LINESTRING Z (59.90242713 10.75914291 -0.602, 59.90257539 10.75902247 -1.442)",
                              "medium": "U"},
                 "vegsystemreferanse": {"vegsystem": {"vegkategori": "E", "nummer": 18}}},
                {"geometri": {"wkt": "LINESTRING Z (59.9 10.7 5.0, 59.91 10.7 6.0)", "medium": "L"}},
                {"geometri": {"wkt": "LINESTRING Z (59.9 10.7 5.0, 59.91 10.7 6.0)"}}]
        rows = roadz.parse_nvdb(objs)
        self.assertEqual([r[2] for r in rows], [-1, 1, 0])
        self.assertAlmostEqual(rows[0][0][0, 0], 10.75914291, msg="WKT is lat lon: lon first here")
        self.assertAlmostEqual(rows[0][1][1], -1.442)
        self.assertEqual(rows[0][3], "E18")

    def test_lines_roundtrip(self):
        ln = roadz.Lines.build([(np.array([[0.0, 0.0], [1.0, 1.0]]), np.array([1.0, np.nan]), 1, "A6", "Rue")],
                               corrected=True)
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"roadz_test_{os.getpid()}.npz")
        try:
            ln.save(path)
            back = roadz.Lines.load(path)
        finally:
            if os.path.exists(path):
                os.remove(path)
        self.assertEqual(len(back), 1)
        self.assertTrue(back.corrected)
        self.assertEqual((str(back.ref[0]), str(back.name[0]), int(back.level[0])), ("A6", "Rue", 1))
        self.assertTrue(np.isnan(back.z[1]))


class NameTest(unittest.TestCase):
    def test_refs(self):
        self.assertEqual(roadz.refs_of("A 75"), {"a75"})
        self.assertEqual(roadz.refs_of("E18"), {"e18"})

    def test_name_ok(self):
        self.assertTrue(roadz.name_ok("", "Rue de Rivoli", ""))
        self.assertTrue(roadz.name_ok("Rue de Rivoli", "rue de rivoli", ""))
        self.assertTrue(roadz.name_ok("Boulevard Périphérique", "Bd Peripherique Interieur", ""))
        self.assertFalse(roadz.name_ok("Rue de Rivoli", "Avenue Kleber", ""), "two names, no word shared")
        self.assertTrue(roadz.name_ok("Autoroute du Soleil", "", "A6"), "a number alone contradicts no name")
        self.assertFalse(roadz.name_ok("A 75", "", "A6"), "two road numbers that differ")
        self.assertTrue(roadz.name_ok("E 18", "", "E18"))


class DatumTest(unittest.TestCase):
    def test_offset(self):
        dem = np.linspace(50, 80, 200)
        got = roadz.datum_fit(dem + 1.3 + np.sin(np.arange(200)) * 0.1, dem)
        self.assertEqual(got[0], 1.0)
        self.assertAlmostEqual(got[1], 1.3, places=1)

    def test_feet(self):
        dem = np.linspace(50, 80, 200)
        got = roadz.datum_fit(dem / 0.3048, dem)
        self.assertEqual(got[0], 0.3048)
        self.assertAlmostEqual(got[1], 0.0, places=3)

    def test_spread_refused(self):
        rng = np.random.default_rng(1)
        self.assertIsNone(roadz.datum_fit(rng.uniform(0, 100, 300), np.full(300, 50.0)))
        self.assertIsNone(roadz.datum_fit(np.ones(5), np.ones(5)), "too few points")


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class KernelTest(unittest.TestCase):
    def test_match_parallel_and_direction(self):
        # a source line 3 m north of the map road, rising 1 m in 100 m; a crossing line (north-south) at 2 m is not it
        q = np.c_[np.arange(0.0, 101.0, 10.0), np.zeros(11)]
        qdir = np.tile([1.0, 0.0], (11, 1))
        seg = np.array([[0.0, 3.0, 100.0, 3.0], [50.0, -50.0, 50.0, 50.0]])
        segz = np.array([[10.0, 11.0], [99.0, 99.0]])
        z, d = ef.roadz_match(q, qdir, seg, segz, np.array([1, 1], np.uint8), 6.0, 0.85)
        np.testing.assert_allclose(z, 10.0 + q[:, 0] / 100.0, atol=1e-9)
        np.testing.assert_allclose(d, 3.0, atol=1e-9)
        z2, _ = ef.roadz_match(q, qdir, seg, segz, np.array([0, 1], np.uint8), 6.0, 0.85)
        self.assertTrue(np.isnan(z2).all(), "the parallel one not allowed (level / name), the crossing one too steep")
        z3, _ = ef.roadz_match(q, qdir, seg, segz, np.array([1, 1], np.uint8), 2.0, 0.85)
        self.assertTrue(np.isnan(z3).all(), "out of reach")

    def test_clean_spike_and_gap(self):
        s = np.arange(0.0, 200.0, 4.0)
        z = 20.0 + 0.03 * s
        z[10] += 6.0                     # a spike (a sign gantry)
        z[20:24] = np.nan                # 16 m of nothing
        out = ef.profile_clean(s, z, 0.12, 1.5, 20.0, 30.0, 0.0)
        np.testing.assert_allclose(out, 20.0 + 0.03 * s, atol=1e-9)

    def test_clean_long_gap_stays(self):
        s = np.arange(0.0, 200.0, 4.0)
        z = np.full(len(s), 5.0)
        z[10:30] = np.nan
        out = ef.profile_clean(s, z, 0.12, 1.5, 20.0, 30.0, 0.0)
        self.assertTrue(np.isnan(out[10:30]).all())

    def test_deck_from_points(self):
        # a 120 m deck: ground at 0 (seen beside / under it), the road surface rising 0 -> 3.5 m -> 0 over its length
        # (9 % at most), cars 1.5 m over it at a few stations, an upper deck 9 m over it across 15 m of it
        rng = np.random.default_rng(3)
        line = np.c_[np.linspace(0, 120, 31), np.zeros(31)]
        x = rng.uniform(0, 120, 12000)
        y = rng.uniform(-6, 6, 12000)
        deck = 3.5 * np.sin(np.pi * x / 120.0)
        z = deck + rng.normal(0, 0.03, len(x))
        cls = np.full(len(x), 1, np.uint8)
        car = (np.abs(y) < 1.5) & ((x % 30) < 4)
        z[car] += 1.5
        up = (x > 50) & (x < 65) & (rng.uniform(0, 1, len(x)) < 0.6)
        z[up] = deck[up] + 9.0
        cls[up] = 17
        gx = rng.uniform(0, 120, 3000)
        g = np.c_[gx, rng.uniform(-6, 6, 3000), np.zeros(3000)]
        pts = np.ascontiguousarray(np.vstack([np.c_[x, y, z], g]))
        cl = np.concatenate([cls, np.full(3000, 2, np.uint8)])
        s, zz, cnt = ef.deck_lidar(line, pts, cl, 6.0, 4.0, 0.10, 3, 24.0, 0.0)
        want = 3.5 * np.sin(np.pi * s / 120.0)
        ok = np.isfinite(zz)
        self.assertGreater(ok.mean(), 0.8)
        mid = ok & (s > 15) & (s < 105)
        self.assertLess(float(np.abs(zz[mid] - want[mid]).max()), 0.25, "the deck, not the cars, the upper deck "
                                                                         "or the ground under it")

    def test_blend(self):
        s = np.arange(0.0, 101.0, 1.0)
        z = np.full(len(s), 8.0)
        out = ef.deck_blend(s, z, 7.0, float("nan"), 0.04, 10.0, 0.5)
        self.assertAlmostEqual(out[0], 7.0)
        self.assertAlmostEqual(out[-1], 8.0)
        self.assertLessEqual(float(np.abs(np.diff(np.diff(out))).max()), 0.04 * 1.0 + 1e-9)

    def test_las_plain(self):
        # a LAS 1.2 point format 0 file of two points (no compression)
        hdr = bytearray(227)
        hdr[0:4] = b"LASF"
        hdr[24], hdr[25] = 1, 2
        struct.pack_into("<H", hdr, 94, 227)
        struct.pack_into("<I", hdr, 96, 227)
        struct.pack_into("<I", hdr, 100, 0)
        hdr[104] = 0
        struct.pack_into("<H", hdr, 105, 20)
        struct.pack_into("<I", hdr, 107, 2)
        struct.pack_into("<3d", hdr, 131, 0.01, 0.01, 0.01)
        struct.pack_into("<3d", hdr, 155, 1000.0, 2000.0, 0.0)
        pts = b""
        for (x, y, z, c) in ((100, 200, 550, 2), (5000, 200, 900, 17)):
            rec = bytearray(20)
            struct.pack_into("<3i", rec, 0, x, y, z)
            rec[15] = c
            pts += bytes(rec)
        xyz, cls = ef.las_decode(bytes(hdr) + pts, [-1e9, -1e9, 1e9, 1e9])
        np.testing.assert_allclose(xyz, [[1001.0, 2002.0, 5.5], [1050.0, 2002.0, 9.0]])
        self.assertEqual(list(cls), [2, 17])
        xyz2, _ = ef.las_decode(bytes(hdr) + pts, [0.0, 0.0, 1010.0, 1e9])
        self.assertEqual(len(xyz2), 1, "the box")
        with self.assertRaises(ValueError):
            ef.las_decode(b"nope" * 100, [0, 0, 1, 1])


class FakeDem:
    def __init__(self, z):
        self.z = z

    def sample(self, la, lo, zoom):
        return np.full(np.shape(la), self.z, np.float64)


class FakeMDem:
    def __init__(self, z):
        self.dem = FakeDem(z)


def provider_with(lines, src="ign", dem=FLAT):
    """A RoadZ whose cells all hold `lines` (no network) on a flat DEM at `dem`."""
    rz = roadz.RoadZ(None, object(), FakeMDem(dem))
    rz.enabled = True
    rz.lines = lambda s, key: lines if s == src else roadz.Lines.build([])
    return rz


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class ObservedTest(unittest.TestCase):
    def setUp(self):
        self.old = roadz.sources_at
        roadz.sources_at = lambda lat, lon, lidar=None: ["ign"]

    def tearDown(self):
        roadz.sources_at = self.old

    def src_line(self, xs, ys, zs, level, ref="", name=""):
        la, lo, _ = FRAME.enu_to_geodetic(np.asarray(xs, float), np.asarray(ys, float), 0.0)
        return (np.c_[lo, la], np.asarray(zs, float), level, ref, name)

    def test_relative_to_source_ground(self):
        # the source's datum is 1.5 m over ours (its ground roads say so): the deck comes out 1.5 m lower than its Z
        bridge = self.src_line(np.linspace(-35, 35, 15), np.full(15, 2.0), np.full(15, FLAT + 1.5 + 6.0), 1)
        ground = self.src_line(np.linspace(-200, -35, 20), np.full(20, 2.0), np.full(20, FLAT + 1.5), 0)
        rz = provider_with(roadz.Lines.build([bridge, ground]))
        xy = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        info = {}
        z = rz.observed(FRAME, xy, 4.0, 1, "", info)
        self.assertIsNotNone(z)
        np.testing.assert_allclose(z, FLAT + 6.0, atol=1e-6)
        self.assertAlmostEqual(info["offset"], 1.5)
        self.assertIsNone(rz.observed(FRAME, xy, 4.0, -1), "no tunnel there")

    def test_level_must_agree(self):
        ground = self.src_line(np.linspace(-200, 200, 40), np.zeros(40), np.full(40, FLAT), 0)
        rz = provider_with(roadz.Lines.build([ground]))
        xy = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        self.assertIsNone(rz.observed(FRAME, xy, 4.0, 1), "a ground road gives no deck height")
        self.assertIsNotNone(rz.observed(FRAME, xy, 4.0, 0))

    def test_off(self):
        bridge = self.src_line(np.linspace(-35, 35, 15), np.zeros(15), np.full(15, 20.0), 1)
        rz = provider_with(roadz.Lines.build([bridge]))
        rz.enabled = False
        self.assertIsNone(rz.observed(FRAME, np.c_[np.linspace(-35, 35, 36), np.zeros(36)], 4.0, 1))


class StubProvider:
    """observed() from a function of the part's arc length (geodetic heights), for the override tests."""

    def __init__(self, fn, level=1):
        self.fn, self.level, self.enabled, self.calls = fn, level, True, 0

    def observed(self, frame, xy, half, level, name="", info=None):
        self.calls += 1
        if level != self.level or self.fn is None:
            return None
        xy = np.asarray(xy, np.float64)[:, :2]
        s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        return self.fn(xy, s)


def solve_overpass():
    f = overpass()
    bp = roads.BridgeProfiles(FRAME, lambda key: f, flat_elev)
    parts = bp.parts([f], (-100, -100, 100, 100))
    _fid, _spec, xy, z, _lon = parts[0]
    return np.asarray(xy, np.float64)[:, :2], np.asarray(z, np.float64)


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class BridgeOverrideTest(unittest.TestCase):
    def setUp(self):
        self.old = roadz.PROVIDER

    def tearDown(self):
        roadz.PROVIDER = self.old

    def test_fallback_is_the_inference(self):
        roadz.PROVIDER = None
        xy0, z0 = solve_overpass()
        roadz.PROVIDER = StubProvider(None)
        xy1, z1 = solve_overpass()
        np.testing.assert_allclose(z1, z0, atol=1e-12)

    def test_measured_deck_used(self):
        roadz.PROVIDER = None
        xy, z0 = solve_overpass()
        s0 = np.r_[0.0, np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]
        bump = 0.6 * np.sin(np.pi * s0 / s0[-1])

        def fn(xy_, s):
            zu = np.interp(s, s0, z0 + bump)
            la, lo, h = FRAME.enu_to_geodetic(xy_[:, 0], xy_[:, 1], zu)
            return np.asarray(h, np.float64)
        roadz.PROVIDER = StubProvider(fn)
        _xy, z1 = solve_overpass()
        np.testing.assert_allclose(z1, z0 + bump, atol=0.02)
        self.assertAlmostEqual(z1[0], z0[0], places=6, msg="the abutments keep the graded road's height")
        self.assertAlmostEqual(z1[-1], z0[-1], places=6)

    def test_wild_measurement_refused(self):
        roadz.PROVIDER = None
        xy, z0 = solve_overpass()

        def fn(xy_, s):
            return np.where((s > 30) & (s < 34), FLAT + 30.0, FLAT + 6.0)
        roadz.PROVIDER = StubProvider(fn)
        _xy, z1 = solve_overpass()
        np.testing.assert_allclose(z1, z0, atol=1e-9, err_msg="steeper than OBS_MAX_GRADE: the solve stays")


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class GradingOverrideTest(unittest.TestCase):
    def setUp(self):
        self.old = roadz.PROVIDER

    def tearDown(self):
        roadz.PROVIDER = self.old

    def test_plan_takes_measured_heights(self):
        line = np.c_[np.linspace(0, 400, 101), np.zeros(101)]
        la, lo, _ = FRAME.enu_to_geodetic(line[:, 0], line[:, 1], 0.0)
        spec = roads.road_specs(overpass())[1]       # (a motorway)
        p0 = grading.plan_roads(FRAME, [(np.c_[lo, la], spec)], flat_elev, cuts=False)[0]
        p1 = grading.plan_roads(FRAME, [(np.c_[lo, la], spec)], flat_elev, cuts=False,
                                obs=lambda xy, sp: np.full(len(xy), FLAT + 2.0))[0]
        np.testing.assert_allclose(p0.z, FLAT, atol=1e-9)
        self.assertGreater(float(np.median(p1.z)), FLAT + 1.9, "the measured profile, not the DEM's")
        p2 = grading.plan_roads(FRAME, [(np.c_[lo, la], spec)], flat_elev, cuts=False, obs=lambda xy, sp: None)[0]
        np.testing.assert_allclose(p2.z, p0.z, atol=1e-12, err_msg="nothing measured: the DEM")

    def test_ground_override_only_on_coarse_elevation(self):
        g = grading._Planner(FRAME, FakeMD(overpass()), flat_elev)
        roadz.PROVIDER = StubProvider(lambda xy, s: np.full(len(xy), 99.0), level=0)
        g.dem = type("D", (), {"coarse_at": staticmethod(lambda la, lo: False)})()
        self.assertIsNone(g._obs(mapcells.cell_of(float(FRAME.lat0), float(FRAME.lon0))))
        g.dem = type("D", (), {"coarse_at": staticmethod(lambda la, lo: True)})()
        self.assertIsNotNone(g._obs(mapcells.cell_of(float(FRAME.lat0), float(FRAME.lon0))))

    def test_measured_abutment_not_raised(self):
        # the overpass's abutments would be raised for the motorway's clearance (test_approaches); a measured deck
        # whose ends sit on the ground says they are not
        f = overpass()
        g = grading._Planner(FRAME, FakeMD(f), flat_elev)
        line = np.c_[np.linspace(-35, 35, 36), np.zeros(36)]
        roadz.PROVIDER = None
        self.assertIsNotNone(g._end_lift(line[0]), "the inference raises it")
        g2 = grading._Planner(FRAME, FakeMD(f), flat_elev)
        roadz.PROVIDER = StubProvider(lambda xy, s: FLAT + 7.0 * np.sin(np.pi * s / s[-1]))
        self.assertIsNone(g2._end_lift(line[0]), "measured at the ground there: no ramp")
        g3 = grading._Planner(FRAME, FakeMD(f), flat_elev)
        roadz.PROVIDER = StubProvider(lambda xy, s: np.full(len(xy), FLAT + 2.0))
        got = g3._end_lift(line[0])
        self.assertAlmostEqual(got[0], 2.0, places=6, msg="measured 2 m up: raised 2 m")


@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class TunnelOverrideTest(unittest.TestCase):
    def test_floor_only_deeper_and_inside(self):
        tp = object.__new__(roads.TunnelProfiles)
        tp.frame = FRAME
        xy = np.c_[np.arange(0.0, 201.0, 4.0), np.zeros(51)]
        s = xy[:, 0].copy()
        floor = np.full(len(s), 10.0)
        secs = [(0, 5, 0), (5, 45, 2), (45, 50, 0)]
        spec = roads.road_specs(overpass())[0]
        pool = {("t", 0): (spec, xy, 0.0, -1.0, 51, "")}
        old = roadz.PROVIDER
        try:
            roadz.PROVIDER = StubProvider(lambda xy_, s_: np.full(len(xy_), 4.0), level=-1)
            out = tp._observed_floor(("t", 0), pool, xy, s, floor, secs)
            self.assertAlmostEqual(out[5], 10.0, msg="the portal keeps the hull's floor")
            self.assertAlmostEqual(out[0], 10.0, msg="the open section is left")
            self.assertLess(float(out[25]), 4.2, "deeper in the middle, as measured")
            roadz.PROVIDER = StubProvider(lambda xy_, s_: np.full(len(xy_), 30.0), level=-1)
            out = tp._observed_floor(("t", 0), pool, xy, s, floor, secs)
            np.testing.assert_allclose(out, floor, err_msg="never over the ground's hull")
        finally:
            roadz.PROVIDER = old



@unittest.skipUnless(NATIVE, "earthfast roadz kernels")
class MeasuredRampFromPlanTest(unittest.TestCase):
    """The approach ramp to a measured deck end rises from the road's plan, not from the ground under the end point
    (Charles Street over I-83 in Baltimore: the deck's end over the cut's slope, 2.7 m lower than the street, the
    street raised 2.2 m over its plan to the deck's measured height counted from the slope, a 2.5 m step there)."""

    def setUp(self):
        self.old = roadz.PROVIDER

    def tearDown(self):
        roadz.PROVIDER = self.old

    def lifted(self, measured):
        f = overpass()
        spec = roads.road_specs(f)[0]
        xy = np.c_[np.linspace(-400, -35, 120), np.zeros(120)]
        la, lo, _ = FRAME.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
        plan = grading.plan_roads(FRAME, [(np.c_[lo, la], spec)], flat_elev, cuts=False)[0]

        def cut_elev(la_, lo_):         # (the ground falls 3 m into a cut right under the deck's end)
            e, n, _ = FRAME.geodetic_to_enu(np.asarray(la_, np.float64), np.asarray(lo_, np.float64), 0.0)
            return np.where(np.abs(np.asarray(e) + 35.0) < 1.0, FLAT - 3.0, FLAT)
        g = grading._Planner(FRAME, FakeMD(f), cut_elev)
        roadz.PROVIDER = StubProvider(lambda xy_, s: np.full(len(xy_), measured))
        plans = [("cross", plan)]
        g._lift_approaches(mapcells.cell_of(float(FRAME.lat0), float(FRAME.lon0)), plans, [spec])
        return float(plans[0][1].z[-1])

    def test_ramp_from_the_plan(self):
        self.assertLess(abs(self.lifted(FLAT + 0.2) - FLAT), 0.05, "measured at the street's height: not raised")
        self.assertAlmostEqual(self.lifted(FLAT + 2.0), FLAT + 2.0, delta=0.05, msg="raised to the deck's end")


if __name__ == "__main__":
    unittest.main()
