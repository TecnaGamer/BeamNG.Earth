"""airsideprops: the ground vehicles at the parked aircraft keep off every building and building part (a canopy or a
covered walkway, any height) by CLEAR_BUILDING_M, as the aircraft do; a gate's own jet bridge is no obstacle."""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import airsideprops as ap  # noqa: E402
import parkedaircraft as pa  # noqa: E402
from test_parkedaircraft import BOX, FRAME, TERMINAL, aerodrome, buildings, feats, gate, ground, line, parts, poly  # noqa: E402

VEHICLES = {g: k for k, g in ap.GROUPS.items() if k not in ("container", "cone")}


def vehicles(got):
    """[(kind, footprint points (n, 2) east / north of the test frame's origin)] of the vehicles in the Props."""
    out = []
    for p in got:
        if p.group not in VEHICLES:
            continue
        e, n, _ = FRAME.geodetic_to_enu(np.array([p.lat]), np.array([p.lon]), 0.0)
        out.append((VEHICLES[p.group], ap._box_pts(VEHICLES[p.group], float(e[0]), float(n[0]), p.he, p.hn)))
    return out


def stand_row():
    """One Features (the ids fix the hashes: the runs compared share it), with vehicles to test."""
    while True:
        f = feats(aerodrome("regional_airport"), *[gate(x) for x in (-90.0, -30.0, 30.0, 90.0)], occupied=True)
        if len(vehicles(ap.props(f, [buildings(TERMINAL)]))) > 3:
            return f


class Vehicles(unittest.TestCase):
    def test_vehicles_keep_off_small_buildings_and_parts(self):
        f = stand_row()
        base = vehicles(ap.props(f, [buildings(TERMINAL)]))
        self.assertGreater(len(base), 3)
        # a 3 m shed (building) and a 3 m canopy (part) on the first two vehicles' footprints
        rings = []
        for _k, pts in base[:2]:
            c = pts.mean(axis=0)
            rings.append([(c[0] - 1.5, c[1] - 1.5), (c[0] + 1.5, c[1] - 1.5), (c[0] + 1.5, c[1] + 1.5), (c[0] - 1.5, c[1] + 1.5)])
        got = vehicles(ap.props(f, [buildings(TERMINAL, rings[0]), parts(rings[1])]))
        self.assertTrue(got)
        for _k, pts in got:
            for r in rings:
                r = np.array(r, np.float64)
                self.assertFalse(pa._pip(r, pts).any())
                self.assertGreater(pa._seg_dist(pts, r, np.roll(r, -1, axis=0)).min(), ap.CLEAR_BUILDING_M - 0.4)

    def test_a_jet_bridge_is_no_obstacle_to_the_vehicles_of_its_gate(self):
        bridges = [[(x - 2, -3), (x + 2, -3), (x + 2, 30), (x - 2, 30)] for x in (-90.0, -30.0, 30.0, 90.0)]
        f = stand_row()                                                  # (each gate touches its bridge: it is no obstacle)
        for blds in ([buildings(TERMINAL), parts(*bridges)], [buildings(TERMINAL, *bridges)]):
            self.assertEqual(len(pa.stands(f, blds)), 4)
            self.assertTrue(vehicles(ap.props(f, blds)))


def masts(got):
    """[(x, y)] of the masts in the Props, east / north of the test frame's origin."""
    out = []
    for p in got:
        if p.group == ap.MAST_GROUP:
            e, n, _ = FRAME.geodetic_to_enu(np.array([p.lat]), np.array([p.lon]), 0.0)
            out.append((float(e[0]), float(n[0])))
    return out


class Masts(unittest.TestCase):
    APRON = [(-150, 0), (150, 0), (150, 200), (-150, 200)]
    TAXIWAY = (line((-300, 10), (300, 10)), "taxiway", {})
    RUNWAY = (line((-300, 250), (300, 250)), "runway", {})

    def rows(self, *extra):
        return feats(aerodrome("regional_airport"), (poly(self.APRON), "apron", {}), *extra)

    def test_masts_stand_in_the_apron_off_the_taxiways_and_runways(self):
        free = masts(ap.props(self.rows(), []))
        got = masts(ap.props(self.rows(self.TAXIWAY, self.RUNWAY), []))
        self.assertGreater(len(free), 3)
        self.assertGreater(len(got), 0)
        self.assertLess(len(got), len(free))                         # (the ones by the taxiway and the runway are gone)
        apron = np.array(self.APRON, np.float64)
        for x, y in got:
            self.assertTrue(pa._pip(apron, np.array([[x, y]]))[0])
            self.assertGreaterEqual(abs(y - 10.0), ap.MAST_TAXI_M)
            self.assertGreaterEqual(abs(y - 250.0), ap.MAST_RUNWAY_M)

    def test_no_mast_on_a_stand(self):
        f = feats(aerodrome("regional_airport"), (poly(self.APRON), "apron", {}), gate(-60.0, 1.5), gate(60.0, 1.5), occupied=True)
        for x, y in masts(ap.props(f, [])):
            for gx in (-60.0, 60.0):
                self.assertGreaterEqual(math.hypot(x - gx, y - 1.5), ap.MAST_GATE_M)


class MastLight(unittest.TestCase):
    """An apron mast's night light rows: its own lens group (nightlights.lua gives it a wide floodlight and a share of the
    light pool), its head's lens and glow at the cluster's size, and a pool of light on the ground under it."""

    def rows(self):
        f = feats(aerodrome("regional_airport"), (poly(Masts.APRON), "apron", {}))
        return ap.tile_rows(FRAME, BOX, lambda k: f, lambda k: None, ground, keys=[0])

    def test_every_mast_has_its_own_lens_its_glow_and_a_pool(self):
        rows = self.rows()
        by = {}
        for r in rows:
            by.setdefault(r[0], []).append(r)
        masts = by[ap.MAST_GROUP]
        self.assertGreater(len(masts), 3)
        self.assertEqual({g for g in by} & {"high_mast_light"}, set())              # (not the road masts' lens)
        for group in (ap.G_LENS, "lampglow", ap.G_POOL):
            self.assertEqual(len(by[group]), len(masts), group)
        for r in rows:
            self.assertEqual(len(r), 8)
        key = lambda r: (r[1], r[2])
        for m, lens, glow, pool in zip(sorted(masts, key=key), *(sorted(by[g], key=key) for g in (ap.G_LENS, "lampglow", ap.G_POOL))):
            self.assertEqual(key(lens), key(m))
            self.assertAlmostEqual(lens[3], m[3] + 17.9326 - 0.03, 2)               # (under the head: the stock mast's)
            self.assertEqual(lens[1:], glow[1:])                                    # (the glow at the same place and size)
            self.assertEqual((lens[5], lens[6]), (1.0, ap.MAST_HEAD_SCALE))        # (a cluster of floodlights: 2.5 times a lamp's)
            self.assertEqual(key(pool), key(m))
            self.assertAlmostEqual(pool[3], m[3] + ap.POOL_LIFT_M, 2)               # (on the ground, a little over it)
            self.assertEqual((pool[4], pool[5], pool[6], pool[7]), (0.0, 1.0, 1.0, 0.0))

    def test_the_head_scale_is_the_most_trees_lua_scales_a_shape_by(self):
        self.assertLessEqual(ap.MAST_HEAD_SCALE, 2.5)                              # (trees.lua pick clamps the scale to 2.5)

    def test_the_road_masts_keep_their_lens_group(self):
        import furniture
        mast = [[ap.MAST_GROUP, 10.0, 20.0, 5.0, 0.0, 1.0, ap.MAST_H, 0.0]]
        road = furniture.lamp_lenses(mast)
        self.assertEqual([r[0] for r in road], [furniture.G_HIGH_MAST_LENS, furniture.G_GLOW])
        self.assertEqual({r[6] for r in road}, {1.0})
        own = furniture.lamp_lenses(mast, ap.G_LENS, 2.0)
        self.assertEqual([r[0] for r in own], [ap.G_LENS, furniture.G_GLOW])
        self.assertEqual({r[6] for r in own}, {2.0})


class FarMasts(unittest.TestCase):
    """The apron masts in the far and skyline tiles (far_points): a point at each head glow and one for the pool, each in one tile."""

    ROADS_LUA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mod", "beamng_earth", "lua", "ge", "extensions",
                         "beamngEarth", "roads.lua")

    def setUp(self):
        self.f = feats(aerodrome("regional_airport"), (poly(Masts.APRON), "apron", {}))

    def points(self, box=BOX):
        return ap.far_points(FRAME, box, lambda k: self.f, lambda k: None, ground)

    def test_a_head_and_a_pool_point_for_each_mast(self):
        rows = ap.tile_rows(FRAME, BOX, lambda k: self.f, lambda k: None, ground, keys=[0])
        masts = [r for r in rows if r[0] == ap.MAST_GROUP]
        got = self.points()
        self.assertGreater(len(masts), 3)
        self.assertTrue(all(len(p) == 4 for p in got))
        heads = [p for p in got if p[3] == ap.FAR_HEAD]
        pools = [p for p in got if p[3] == ap.FAR_POOL]
        self.assertEqual(len(heads), len(masts))
        self.assertEqual(len(pools), len(masts))
        self.assertEqual(len(got), 2 * len(masts))
        key = lambda p: (p[0], p[1])
        for m, h, pool in zip(sorted(masts, key=lambda r: (r[1], r[2])), sorted(heads, key=key), sorted(pools, key=key)):
            self.assertEqual(key(h), (m[1], m[2]))
            self.assertEqual(key(pool), (m[1], m[2]))
            self.assertAlmostEqual(pool[2], m[3] + ap.POOL_LIFT_M, 2)                  # (as the road tile's pool row)
            self.assertAlmostEqual(h[2], m[3] + 17.9326 - 0.03, 2)                     # (under the head, as its glow row)

    def test_each_mast_in_one_tile(self):
        whole = self.points()
        a = self.points((BOX[0], BOX[1], 0.0, BOX[3]))
        b = self.points((0.0, BOX[1], BOX[2], BOX[3]))
        self.assertEqual(len(a) + len(b), len(whole))
        self.assertTrue(all(p[0] < 0.0 for p in a) and all(p[0] >= 0.0 for p in b))

    def test_none_without_masts(self):
        self.assertEqual(ap.far_points(FRAME, BOX, lambda k: None, lambda k: None, ground), [])
        plain = feats(aerodrome("regional_airport"))
        self.assertEqual(ap.far_points(FRAME, BOX, lambda k: plain, lambda k: None, ground), [])

    def test_the_numbers_are_roads_luas(self):
        import re
        import airfieldlights as al
        with open(self.ROADS_LUA, encoding="utf-8") as fh:
            lua = fh.read()
        groups = re.findall(r"'([^']+)'", re.search(r"^M\.FAR_LAMP_GROUPS = \{([^}]*)\}", lua, re.M).group(1))
        self.assertEqual(groups[ap.FAR_POOL], ap.G_POOL, "the pool's number: its group, the last")
        self.assertEqual(ap.FAR_POOL, len(al.COLOURS) + 1)
        self.assertEqual(ap.FAR_HEAD, len(groups), "a head: the number after the last group, drawn as lampglow")
        self.assertEqual(float(re.search(r"^M\.FAR_HEAD_HEIGHT = ([\d.]+)", lua, re.M).group(1)), ap.MAST_HEAD_SCALE)


class FarMastsInTheFarTiles(unittest.TestCase):
    """blockworker's far geotiles over an airport with an apron: the masts' glow and pools in their lamps, beside the airfield lights."""

    def setUp(self):
        import types
        from unittest import mock
        import airfieldlights as al
        import airportfixtures as af
        import airports
        import blockworker as bw
        import geotiles
        self.bw, self.al = bw, al
        ap_ = af.airport(1, "YBBN", -27.39, 153.118, 5)
        row = af.runway(1, ap_, -27.39, 153.118, 90.0, 1500.0)
        db = airports.AirportDB.from_csv(af.to_csv([ap_], af.AIRPORT_COLS), af.to_csv([row], af.RUNWAY_COLS))
        self.f = feats(aerodrome("regional_airport"), (poly(Masts.APRON), "apron", {}))
        md = types.SimpleNamespace(
            has=lambda k, layer: True, warm=lambda need, kind=None: None,
            cell=lambda k, layers=(), kind=None: types.SimpleNamespace(layers={"infrastructure": self.f}))
        flat = types.SimpleNamespace(sample=lambda la, lo, z: np.full(np.shape(la), 12.0))
        for patch in (mock.patch.object(bw, "_s", types.SimpleNamespace(airports=types.SimpleNamespace(db=db), md=md, dem=flat,
                                                                        widths=None)),):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(bw._far_cells_miss.clear)
        la, lo, _ = FRAME.enu_to_geodetic(0.0, 100.0, 0.0)
        self.tile = geotiles.geobox(*geotiles.tile_of(float(la), float(lo), geotiles.FAR_Z), geotiles.FAR_Z)
        self.raw = lambda la_, lo_: np.full(np.shape(la_), 12.0)

    def test_the_tile_carries_each_mast_head_and_pool_beside_the_airfield_lights(self):
        got = self.bw._far_afl("bldfar", self.tile, self.raw)
        pools = [p for p in got if p[3] == ap.FAR_POOL]
        heads = [p for p in got if p[3] == ap.FAR_HEAD]
        self.assertGreater(len(pools), 3)
        self.assertEqual(len(pools), len(heads))
        self.assertTrue(all(len(p) == 4 for p in got))
        self.assertTrue([p for p in got if 1 <= p[3] <= len(self.al.COLOURS)], "the airfield lights too")
        by = self.bw._lamps_by_geotile(self.tile.frame, [], got)
        rows = by[self.tile.key]["rows"]
        self.assertEqual(sorted(r[3] for r in rows if len(r) == 4 and r[3] >= ap.FAR_POOL), sorted(p[3] for p in got if p[3] >= ap.FAR_POOL))

    def test_waits_for_the_buildings_too_and_the_key_is_the_airfield_lights(self):
        self.bw._s.md.has = lambda k, layer: layer != "buildings"
        self.assertIsNone(self.bw._far_afl("bldfar", self.tile, self.raw))
        self.bw._far_cells_miss.clear()
        self.assertEqual(self.bw._far_afl_key(self.tile)[0], f"afl={self.al.FAR_FORMAT}")


if __name__ == "__main__":
    unittest.main()
