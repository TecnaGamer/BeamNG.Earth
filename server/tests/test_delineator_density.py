"""Deployment regressions: geometry, road coverage, country gates and cache.

Counts are generated scenery on synthetic roads, not measured country rates.
"""
from pathlib import Path
import sys
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'server'))
import delineatorprofiles as dp
import delineators, europeinfra, furniture, poleprofiles, regionalmarkers, roads


class DensityTest(unittest.TestCase):
    def road(self, fid='test', length=1000., cls='secondary', **kw):
        spec = roads.Spec(7., 'paved', False, cls, False)._replace(**kw)
        return (fid, spec, np.array([[0., 0., 0.], [length, 0., 0.]]), 7.8)

    def rows(self, code, rr=None, box=(-20, -200, 1020, 200), lighting=None):
        return regionalmarkers.place(rr or [self.road()], box, lambda *p: code, lighting)

    def markers(self, rows):
        return [r for r in rows if '_marker_' in r[0] or r[0] == 'fr_j6']

    def test_germany_equips_every_eligible_long_feature(self):
        for fid in map(str, range(20)):
            self.assertEqual(len(self.markers(self.rows('DE', [self.road(fid)]))), 40)

    def test_germany_tertiary_and_unclassified_are_covered(self):
        for cls in ('primary', 'secondary', 'tertiary', 'unclassified'):
            self.assertEqual(len(self.markers(self.rows('DE', [self.road(cls=cls)]))), 40)

    def test_germany_straight_spacing_both_verges(self):
        rows = self.markers(self.rows('DE'))
        for side in (-1, 1):
            xs = sorted(r[1] for r in rows if side*r[2] > 0)
            self.assertEqual(len(xs), 20)
            np.testing.assert_allclose(np.diff(xs), 50., atol=.011)
        self.assertEqual({r[1] for r in rows if r[2] < 0}, {r[1] for r in rows if r[2] > 0})

    def test_fragmented_roads_under_250m_do_not_disappear(self):
        rr = []
        for i in range(10):
            fid, spec, line, lon = self.road(str(i), length=100.)
            line[:, 0] += 100*i
            rr.append((fid, spec, line, lon))
        rows = self.markers(self.rows('DE', rr))
        self.assertEqual(len(rows), 40)
        for i in range(10):
            self.assertEqual(sum(100*i <= r[1] < 100*(i+1) for r in rows), 4)

    def test_tiny_feature_phases_keep_density_instead_of_post_per_fragment(self):
        rr = []
        for i in range(100):
            fid, spec, line, lon = self.road(str(i), length=10.)
            line[:, 0] += 10*i
            rr.append((fid, spec, line, lon))
        count = len(self.markers(self.rows('DE', rr)))
        self.assertGreaterEqual(count, 24)
        self.assertLessEqual(count, 56)

    def test_austria_is_denser_than_germany(self):
        rows = self.markers(self.rows('AT'))
        self.assertIn(len(rows), (60, 62))
        xs = sorted(r[1] for r in rows if r[2] < 0)
        np.testing.assert_allclose(np.diff(xs), 33., atol=.011)

    def test_denmark_and_poland_use_100m(self):
        for code in ('DK', 'PL'):
            rows = self.markers(self.rows(code))
            self.assertEqual(len(rows), 20)
            np.testing.assert_allclose(np.diff(sorted(r[1] for r in rows if r[2] < 0)), 100., atol=.011)

    def test_verified_50m_profiles(self):
        for code in ('CH', 'CZ', 'SK', 'ES', 'IT'):
            self.assertEqual(len(self.markers(self.rows(code))), 40)

    def test_uk_and_ireland_have_no_blanket_straight_rows(self):
        for code in ('GB', 'IE'):
            self.assertEqual(self.markers(self.rows(code)), [])

    def test_uk_and_ireland_still_get_bend_markers(self):
        rr = self.curve(40.)
        for code in ('GB', 'IE'):
            self.assertGreater(len(self.markers(self.rows(code, rr))), 0)

    def test_france_keeps_sparse_j6_not_german_deployment(self):
        selected = sum(bool(self.markers(self.rows('FR', [self.road(str(i))]))) for i in range(100))
        expected = sum(furniture._h01(str(i), 'fr-j6-section-v2') < .08 for i in range(100))
        self.assertEqual(selected, expected)
        for cls in ('tertiary', 'unclassified'):
            self.assertEqual(self.markers(self.rows('FR', [self.road(cls=cls)])), [])

    def test_unknown_countries_do_not_receive_european_models(self):
        for code in (None, 'US', 'CA', 'XX'):
            self.assertEqual(self.rows(code), [])

    def test_unverified_country_explicitly_retains_provisional_trial(self):
        self.assertEqual(dp.policy('RO').mode, 'legacy')
        self.assertIn('provisional', dp.policy('RO').evidence)
        for i in range(10):
            rows = self.markers(self.rows('RO', [self.road(str(i))]))
            self.assertEqual(bool(rows), furniture._h01(str(i), 'fr-j6-section-v2') < .35)

    def test_excludes_urban_links_paths_service_and_unpaved(self):
        cases = ({'urban': True}, {'link': True}, {'cls': 'motorway'}, {'cls': 'service'},
                 {'cls': 'residential'}, {'cls': 'track'}, {'kind': 'gravel'}, {'width': 2.5})
        for kw in cases:
            with self.subTest(kw=kw):
                self.assertEqual(self.rows('DE', [self.road(**kw)]), [])

    def test_nordic_speed_width_and_lighting_filters(self):
        for code in ('SE', 'NO'):
            self.assertEqual(len(self.markers(self.rows(code, [self.road(kmh=80)]))), 40)
            for rr in ([self.road(kmh=50)], [self.road(cls='unclassified')], [self.road(width=4.)]):
                self.assertEqual(self.markers(self.rows(code, rr)), [])
            self.assertEqual(self.markers(self.rows(code, lighting={'test': 'yes'})), [])

    def test_nordic_lit_bends_do_not_reintroduce_posts(self):
        for code in ('SE', 'NO'):
            self.assertEqual(self.markers(self.rows(code, self.curve(40.), lighting={'curve': 'yes'})), [])

    def test_iceland_main_and_minor_sides_and_gravel(self):
        self.assertEqual(len(self.markers(self.rows('IS'))), 40)
        for cls in ('tertiary', 'unclassified'):
            rows = self.markers(self.rows('IS', [self.road(cls=cls, kind='gravel')]))
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(r[2] < 0 for r in rows))

    def curve(self, radius=100.):
        angle = np.linspace(-1.1, 1.1, 151)
        _, spec, _, lon = self.road()
        line = np.c_[radius*np.sin(angle), radius*(1-np.cos(angle)), np.zeros(len(angle))]
        return [('curve', spec, line, lon)]

    def test_german_curves_get_closer_posts_without_j1_duplicate_row(self):
        rr = self.curve(200.)
        rows = self.markers(self.rows('DE', rr, box=(-300, -300, 300, 300)))
        length = furniture._arclen(rr[0][2])[-1]
        self.assertGreater(len(rows), 2*length/50*1.6)
        self.assertTrue(all(r[0] == 'eu_marker_de' for r in rows))
        self.assertTrue(all(abs(furniture._seg_dist_min(np.array(r[1:3]), rr[0][2][:, :2])-4.6) < .06 for r in rows))

    def test_curvature_recognizes_straight_and_radius(self):
        radii, turn = delineators.curvature(self.road()[2], np.array([100., 500.]))
        self.assertTrue(np.isinf(radii).all())
        rr = self.curve(120.)
        radii, _ = delineators.curvature(rr[0][2], np.array([50., 100., 150.]))
        np.testing.assert_allclose(radii, 120., atol=1.)

    def test_split_tiles_and_border_exactly_own_their_props(self):
        rr = [self.road()]
        lookup = lambda x, y, z: 'DE' if x < 500 else 'PL'
        whole = regionalmarkers.place(rr, (-20, -20, 1020, 20), lookup)
        split = regionalmarkers.place(rr, (-20, -20, 500, 20), lookup)+regionalmarkers.place(rr, (500, -20, 1020, 20), lookup)
        self.assertEqual(sorted(whole), sorted(split))
        self.assertEqual(len(whole), len({tuple(r) for r in whole}))
        self.assertTrue(all(r[0] == ('eu_marker_de' if r[1] < 500 else 'eu_marker_pl') for r in whole))

    def test_country_gate_is_at_actual_verge(self):
        rr = [self.road()]
        rows = regionalmarkers.place(rr, (-20, -20, 1020, 20), lambda x, y, z: 'DE' if y < 0 else 'GB')
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(r[2] < 0 and r[0] == 'eu_marker_de' for r in rows))

    def test_geometry_reversal_and_road_input_order(self):
        road = self.road(); other = self.road('other')
        other[2][:, 1] += 50
        rr = [road, other]
        for code in ('DE', 'IS'):
            flipped = [(fid, sp, line[::-1], lon) for fid, sp, line, lon in reversed(rr)]
            self.assertEqual(sorted(self.rows(code, rr)), sorted(self.rows(code, flipped)))

    def test_other_carriageways_block_posts(self):
        road = self.road()
        crossing = self.road('cross', cls='service', width=10.)
        crossing[2][:] = [[100., -100., 0.], [100., 100., 0.]]
        rows = self.markers(self.rows('DE', [road, crossing]))
        self.assertGreater(len(rows), 30)
        self.assertTrue(all(abs(r[1]-100) >= 5.65 for r in rows))

    def test_europe_cache_changes_and_france_cache_stays_valid(self):
        self.assertIn(dp.VERSION, poleprofiles.geographic_profile('europe')[2])
        self.assertNotIn(dp.VERSION, poleprofiles.geographic_profile('fr-varied')[2])

    def test_all_country_policies_are_explicit_or_labelled_provisional(self):
        for code in europeinfra.COUNTRIES:
            p = dp.policy(code)
            self.assertTrue(p.source)
            self.assertGreater(p.spacing, 0.)
            if code not in dp.PROFILES:
                self.assertIn('provisional', p.evidence)


if __name__ == '__main__':
    unittest.main()
