"""Post-Soviet housing (cisbuildings.py, earthfast cis_classify, buildings.plan's skin): the kind of every block."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import buildings  # noqa: E402
import cisbuildings  # noqa: E402
import fast  # noqa: E402

HAVE = getattr(fast._ef, "VERSION", 0) >= cisbuildings.KERNEL and hasattr(fast._ef, "cis_classify")
VINNYTSIA = (49.2331, 28.4682)
PRAGUE = (50.0755, 14.4378)
LYON = (45.7640, 4.8357)


def box(cx, cy, l, w):
    return np.array([[cx - l / 2, cy - w / 2], [cx + l / 2, cy - w / 2], [cx + l / 2, cy + w / 2], [cx - l / 2, cy + w / 2]])


class Countries(unittest.TestCase):
    def test_post_soviet_points_are_found_and_others_not(self):
        self.assertEqual(cisbuildings.country(*VINNYTSIA), "UA")
        self.assertEqual(cisbuildings.country(53.9, 27.56), "BY")
        self.assertEqual(cisbuildings.country(43.24, 76.9), "KZ")
        self.assertIsNone(cisbuildings.country(*PRAGUE))
        self.assertIsNone(cisbuildings.country(*LYON))
        self.assertIsNone(cisbuildings.country(None, None))

    def test_the_style_follows_the_country(self):
        self.assertEqual(buildings.region_style(*VINNYTSIA).name, "cis")
        self.assertNotEqual(buildings.region_style(*PRAGUE).name, "cis")
        self.assertNotEqual(buildings.region_style(*LYON).name, "cis")

    def test_private_roofs_are_sheet_not_clay(self):
        style = buildings.STYLES["cis"]
        mats = {m for m, _ in style.roofs}
        self.assertNotIn(buildings.ROOF_TILES, mats)
        self.assertGreater(style.hipped, 0.5)


@unittest.skipUnless(HAVE, "kernel older than the post-Soviet housing kinds")
class Kernel(unittest.TestCase):
    def rows(self, specs, roads=None):
        rings = [box(*s[:4]) for s in specs]
        attrs = [dict(num_floors=s[4]) for s in specs]
        return cisbuildings.classify_tile(rings, attrs, [0] * len(rings), roads or [], *VINNYTSIA)

    def test_a_micro_district_is_told_apart(self):
        # three five-storey slabs in a row, a nine-storey slab, a sixteen-storey tower, a deep 23-storey new block alone
        specs = [(0, 0, 62, 11.5, 5), (0, 45, 62, 11.5, 5), (0, 90, 62, 11.5, 5), (200, 0, 72, 12.5, 9),
                 (300, 0, 24, 24, 16), (900, 900, 38, 22, 23)]
        got = self.rows(specs)
        kit = [cisbuildings.KITS[int(r[0])] for r in got]
        self.assertIn(kit[0], ("khrush_panel", "khrush_brick"))
        self.assertEqual(kit[3], "panel9")
        self.assertEqual(kit[4], "tower")
        self.assertEqual(kit[5], "new_mono")
        self.assertEqual(got[0][9], 4)           # 62 m of 15.5 m sections
        self.assertEqual(got[3][10], 1)           # the nine-storey block has a technical storey
        self.assertEqual(got[0][11], 2)           # its series

    def test_outside_the_country_nothing_happens(self):
        rings = [box(0, 0, 62, 11.5)]
        self.assertIsNone(cisbuildings.classify_tile(rings, [dict(num_floors=5)], [0], [], *PRAGUE))

    def test_the_roads_set_the_entrance_side(self):
        got = self.rows([(0, 0, 60, 12, 5)], [np.array([[-100.0, 30.0], [100.0, 30.0]])])
        self.assertEqual(abs(got[0][8]), 1.0)

    def test_the_info_dict(self):
        got = self.rows([(0, 0, 72, 12.5, 9)])
        d = cisbuildings.info(got[0])
        self.assertEqual(d["kit"], "panel9")
        self.assertTrue(d["soviet"] and d["tech"])
        self.assertIsNone(cisbuildings.info(np.zeros(16)))


@unittest.skipUnless(HAVE, "kernel older than the post-Soviet housing kinds")
class Skin(unittest.TestCase):
    def plan(self, kit, nf=9, lat_lon=VINNYTSIA):
        ring = box(0, 0, 70, 12.5)
        info = dict(kit=kit, soviet=True, entrances=4, tech=True, floors=nf)
        return buildings.plan(ring, dict(num_floors=nf, _cis=info), 0, *lat_lon)

    def test_series_blocks_are_panels_with_flat_roofs(self):
        spec = self.plan("panel9")
        self.assertEqual(spec.roof, "flat")
        self.assertTrue(spec.facade.startswith(buildings.PANEL))

    def test_brick_kinds_are_brick(self):
        spec = self.plan("khrush_brick", 5)
        self.assertEqual(spec.band, buildings.BAND_BRICK)

    def test_a_stalinka_is_plaster(self):
        spec = self.plan("stalinka", 5)
        self.assertIn(spec.facade, buildings.CIS_STALINKA_TINTS)

    def test_without_the_verdict_nothing_changes(self):
        ring = box(0, 0, 70, 12.5)
        a = buildings.plan(ring, dict(num_floors=9), 0, *PRAGUE)
        b = buildings.plan(ring, dict(num_floors=9, _cis=None), 0, *PRAGUE)
        self.assertEqual((a.facade, a.band, a.roof), (b.facade, b.band, b.roof))


# Places next to the former Soviet Union that are NOT in it: nothing of this feature may touch them (the upstream PR's
# promise: "only on the territory of the former USSR"), whether the border data is there or only the offline boxes.
NEIGHBOURS = {
    "Warsaw": (52.23, 21.01), "Lublin": (51.25, 22.57), "Bialystok": (53.13, 23.17), "Hajnowka": (52.74, 23.58),
    "Przemysl": (49.78, 22.77), "Kosice": (48.72, 21.26), "Uzhhorod-side Slovakia": (48.63, 22.0), "Debrecen": (47.53, 21.63),
    "Satu Mare": (47.79, 22.88), "Suceava": (47.65, 26.25), "Iasi": (47.16, 27.59), "Galati": (45.44, 28.05),
    "Bucharest": (44.43, 26.10), "Varna": (43.21, 27.91), "Istanbul": (41.01, 28.98), "Trabzon": (41.0, 39.72),
    "Helsinki": (60.17, 24.94), "Vyborg-side Finland": (60.7, 28.8), "Oslo": (59.91, 10.75), "Stockholm": (59.33, 18.07),
    "Kaliningrad": (54.71, 20.51), "Prague": (50.08, 14.44), "Berlin": (52.52, 13.40), "Vienna": (48.21, 16.37),
    "Ulaanbaatar": (47.92, 106.92), "Urumqi": (43.83, 87.62), "Harbin": (45.80, 126.53), "Beijing": (39.90, 116.40),
    "Tehran": (35.69, 51.39), "Kabul": (34.53, 69.17), "Peshawar": (34.01, 71.58), "Lyon": (45.76, 4.84),
    "New York": (40.71, -74.0), "Tokyo": (35.68, 139.69), "Madrid": (40.42, -3.70), "Cairo": (30.04, 31.24),
}
# (the offline boxes are conservative: they keep clear of Poland, Romania and Finland, so the western edge towns (Brest, Lviv) are
# found only with the border data, which is what the game has)
SOVIET = {
    "Vinnytsia": (49.23, 28.47), "Kyiv": (50.45, 30.52), "Kharkiv": (49.99, 36.23), "Odesa": (46.48, 30.72),
    "Minsk": (53.90, 27.56), "Gomel": (52.43, 30.98), "Chisinau": (47.01, 28.86), "Moscow": (55.75, 37.62),
    "Saint Petersburg": (59.93, 30.34), "Novosibirsk": (55.03, 82.92), "Almaty": (43.24, 76.90), "Riga": (56.95, 24.10),
    "Tallinn": (59.43, 24.75), "Vilnius": (54.69, 25.28),
}


class Territory(unittest.TestCase):
    def test_nothing_outside_the_former_soviet_union_is_ever_active(self):
        for name, (la, lo) in NEIGHBOURS.items():
            self.assertIsNone(cisbuildings.country(la, lo), name)
            self.assertFalse(cisbuildings.active(la, lo), name)
            self.assertNotEqual(buildings.region_style(la, lo).name, "cis", name)

    def test_the_cities_of_the_union_are(self):
        for name, (la, lo) in SOVIET.items():
            self.assertTrue(cisbuildings.active(la, lo), name)

    def test_with_border_data_only_the_listed_countries_count(self):
        # the border index says a country by its ISO code: any other (PL, RO, FI, MN, CN ...) is no territory of this feature
        class Fake:
            def __init__(self, code):
                self.code = code

            def at(self, lat, lon):
                return self.code
        import countrybounds
        real = countrybounds.bundled
        try:
            for code, expect in (("PL", False), ("RO", False), ("FI", False), ("MN", False), ("CN", False), ("TR", False),
                                 ("DE", False), ("SK", False), ("HU", False), ("UA", True), ("BY", True), ("RU", True),
                                 ("MD", True), ("KZ", True), ("UZ", True), ("GE", True), ("AM", True), ("AZ", True),
                                 ("LV", True), ("LT", True), ("EE", True), ("KG", True), ("TJ", True), ("TM", True)):
                cisbuildings._country_cell.cache_clear()
                countrybounds.bundled = lambda c=code: Fake(c)
                self.assertEqual(cisbuildings.country(50.0, 20.0) is not None, expect, code)
        finally:
            countrybounds.bundled = real
            cisbuildings._country_cell.cache_clear()

    def test_it_can_be_switched_off(self):
        was = cisbuildings.ON
        try:
            cisbuildings.ON = False
            self.assertFalse(cisbuildings.active(*VINNYTSIA))
            self.assertNotEqual(buildings.region_style(*VINNYTSIA).name, "cis")
            self.assertIsNone(cisbuildings.classify_tile([box(0, 0, 60, 12)], [dict(num_floors=5)], [0], [], *VINNYTSIA))
        finally:
            cisbuildings.ON = was

    def test_a_plan_outside_the_union_ignores_a_verdict(self):
        ring = box(0, 0, 70, 12.5)
        info = dict(kit="panel9", soviet=True, entrances=4, tech=True, floors=9, ux=1.0, uy=0.0, cx=0.0, cy=0.0, l=70.0,
                    arm=12.5, road_side=1.0)
        a = buildings.plan(ring, dict(num_floors=9, _cis=info), 0, *PRAGUE)
        b = buildings.plan(ring, dict(num_floors=9), 0, *PRAGUE)
        self.assertEqual((a.facade, a.band, a.roof, a.roof_mat), (b.facade, b.band, b.roof, b.roof_mat))


@unittest.skipUnless(HAVE and hasattr(fast._ef, "cis_facade"), "kernel older than the facade cutter")
class Drawn(unittest.TestCase):
    """The whole path: footprints -> kernel verdict -> plan -> build, with the art's atlases and modules."""

    def mesh(self, specs, lite=False):
        rings = [box(*s[:4]) for s in specs]
        attrs = [dict(num_floors=s[4]) for s in specs]
        rows = cisbuildings.classify_tile(rings, attrs, [0] * len(rings), [np.array([[-300.0, 40.0], [300.0, 40.0]])], *VINNYTSIA)
        items = []
        for ring, at, row in zip(rings, attrs, rows):
            at = dict(at, _cis=cisbuildings.info(row))
            spec = buildings.plan(ring, at, 0, *VINNYTSIA)
            spec.door = None
            items.append((spec, 0.0, 0.0))
        return items, buildings.build(items, lite=lite)

    def used(self, built):
        P, UV, T, M, mats, C = built
        return {mats[i] for i in set(np.asarray(M).tolist())}, len(np.asarray(T))

    def test_a_khrushchyovka_is_cut_into_cells_with_balconies(self):
        items, built = self.mesh([(0, 0, 62, 11.5, 5), (0, 45, 62, 11.5, 5), (0, 90, 62, 11.5, 5)])
        spec = items[0][0]
        self.assertTrue(spec.cis and spec.cis["kit"] in ("khrush_panel", "khrush_brick"), spec.cis)
        mats, tris = self.used(built)
        self.assertTrue(any(m.startswith("bne_cis_khrush") for m in mats), mats)
        self.assertIn("bne_cis_common", mats)
        self.assertGreater(tris, 300)

    def test_a_nine_storey_slab_has_machine_rooms_and_loggias(self):
        _, built = self.mesh([(0, 0, 72, 12.5, 9), (0, 40, 72, 12.5, 9), (0, 80, 72, 12.5, 9)])
        mats, tris = self.used(built)
        self.assertIn("bne_cis_panel9", mats)
        self.assertGreater(tris, 800)

    def test_far_tiles_have_no_modules(self):
        specs = [(0, 0, 72, 12.5, 9), (0, 40, 72, 12.5, 9), (0, 80, 72, 12.5, 9)]
        near = self.mesh(specs)[1]
        far = self.mesh(specs, lite=True)[1]
        self.assertLess(len(np.asarray(far[2])), len(np.asarray(near[2])))

    def test_everything_stays_inside_the_known_materials(self):
        _, built = self.mesh([(0, 0, 72, 12.5, 9), (0, 40, 24, 24, 16), (0, 80, 38, 22, 23)])
        P, UV, T, M, mats, C = built
        self.assertTrue(set(np.asarray(M).tolist()) <= set(range(len(mats))))
        self.assertTrue(np.isfinite(np.asarray(P)).all() and np.isfinite(np.asarray(UV)).all())
        self.assertTrue(set(cisbuildings.MATERIALS) <= set(buildings.MATERIALS))

    def test_the_baked_data_is_up_to_date(self):
        import subprocess
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "..", "..", "tools", "build_cis_data.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "run tools/build_cis_data.py: " + r.stdout)


if __name__ == "__main__":
    unittest.main()
