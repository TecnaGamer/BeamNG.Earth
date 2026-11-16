"""Offline, deterministic placement tests; no BeamNG engine or network required."""
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

HELPER = Path(__file__).resolve().parent.parent / 'server' / 'norway_road_posts_v01.py'
if not HELPER.exists():  # development: helper and tests are side by side
    HELPER = Path(__file__).with_name('norway_road_posts_v01.py')
spec = importlib.util.spec_from_file_location('norway_road_posts_v01', HELPER)
posts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(posts)


class Road:
    def __init__(self, cls='secondary', width=8., urban=False, link=False,
                 oneway=False, median=None, kind='paved'):
        self.cls, self.width, self.urban, self.link = cls, width, urban, link
        self.oneway, self.median, self.kind = oneway, median, kind


class Frame:
    lat0 = 62.9
    lon0 = 7.67

    def enu_to_geodetic(self, x, y, z):
        return np.full(len(x), 62.9), np.full(len(x), 7.67), z


def at(line, stations):
    s = np.asarray(stations)
    L = float(np.linalg.norm(line[-1, :2] - line[0, :2]))
    t = np.clip(s / L, 0., 1.)
    p = line[0][None] + t[:, None] * (line[-1] - line[0])[None]
    return p, np.tile((line[-1, :2] - line[0, :2]) / L, (len(s), 1))


def candidates(fid, spec, line, country_at, lit=None):
    if not policy('NO').classes.__contains__(spec.cls) or spec.urban or spec.link or spec.kind != 'paved':
        return []
    s = (12., 50., 100., 150., 198.)
    return [(d, 1, 'NO', np.array([d, spec.width/2+1.1, 3]), np.array([1., 0.])) for d in s]


def policy(country):
    return types.SimpleNamespace(classes={'primary', 'secondary'}, min_width=6.5)


def fake_modules(junction=False, observed_width=6.8, clear_fn=None):
    def arms(roads):
        if not junction:
            return {}
        return {100: [('A', roads[0][1], None, np.array([100., 0., 3.])),
                      ('B', Road(), None, np.array([100., 0., 3.])),
                      ('C', Road(), None, np.array([100., 0., 3.]))]}
    fake = {
        'furniture': types.SimpleNamespace(_at=at, _arms=arms),
        'delineators': types.SimpleNamespace(candidates=candidates),
        'delineatorprofiles': types.SimpleNamespace(policy=policy),
        'europeinfra': types.SimpleNamespace(LEFT_HAND=frozenset()),
        'fast': types.SimpleNamespace(Others=lambda roads: types.SimpleNamespace(
            clear=clear_fn if clear_fn is not None else (lambda *a: True))),
        'geotiles': types.SimpleNamespace(box_owns=lambda box,x,y: box[0] <= x < box[2] and box[1] <= y < box[3]),
        'nvdb_width_profile_v03': types.SimpleNamespace(at_points=lambda lo,la,original: np.full(len(lo), observed_width))
    }
    return patch.dict(sys.modules, fake)


class PostTest(unittest.TestCase):
    def setUp(self):
        self.line = np.array([[0.,0.,3.],[200.,0.,3.]])
        self.roads = [('A', Road(), self.line, 0.)]
        self.box = (0., -20., 220., 20.)
        self.country_at = lambda x,y,z: 'NO'

    def test_width_offset_ground_yaw_and_end_clearance(self):
        with fake_modules():
            out = posts.replace([['eu_marker_de', 50., 5.1, 3., 0., 1., 1.15, 0.]], self.roads,
                                self.box, self.country_at, frame=Frame(),
                                ground=lambda x,y: np.full_like(x, 2.4, dtype=float))
        self.assertEqual(len(out), 3)
        self.assertEqual([r[1] for r in out], [50., 100., 150.])
        self.assertTrue(all(abs(r[2] - 4.5) < .01 for r in out))  # NVDB: 6.8 / 2 + 1.1
        self.assertTrue(all(abs(r[3] - 2.3) < .01 for r in out))  # 2.4 ground, 0.1 bury
        self.assertTrue(all(r[4] == 90. for r in out))
        self.assertTrue(all(r[6] == posts.MODEL_HEIGHT for r in out))

    def test_no_post_in_junction_or_on_deck(self):
        deck = (np.array([[145.,0.,4.], [160.,0.,4.]]), 5.)
        with fake_modules(junction=True):
            out = posts.replace([], self.roads, self.box, self.country_at,
                                frame=Frame(), ground=None, decks=[deck])
        self.assertEqual([r[1] for r in out], [50.])

    def test_urban_motorway_other_country_rows_unchanged(self):
        self.roads = [('A', Road(cls='motorway'), self.line, 0.)]
        foreign = [['eu_marker_de', 50., 4., 3., 0., 1., 1.15, 0.]]
        with fake_modules():
            self.assertEqual(posts.replace(foreign, self.roads, self.box, self.country_at), foreign)
        self.roads = [('A', Road(urban=True), self.line, 0.)]
        with fake_modules():
            self.assertEqual(posts.replace(foreign, self.roads, self.box, self.country_at), foreign)

    def test_steep_ground_skips_post(self):
        with fake_modules():
            out = posts.replace([], self.roads, self.box, self.country_at,
                                frame=Frame(), ground=lambda x,y: np.full_like(x, 8.0))
        self.assertEqual(out, [])

    def test_wrong_country_skips_and_preserves_existing(self):
        with fake_modules():
            out = posts.replace([['eu_marker_de', 0., 4., 3., 0., 1., 1.15, 0.]],
                                self.roads, self.box, lambda x,y,z: 'DE', frame=Frame())
        self.assertEqual([r[0] for r in out], ['eu_marker_de'])

    def test_road_width_fallback(self):
        with fake_modules(observed_width=8.):
            out = posts.replace([], self.roads, self.box, self.country_at, frame=Frame())
        self.assertTrue(out)
        self.assertTrue(all(abs(r[2] - 5.1) < .01 for r in out))

    def test_reversed_centerline_still_tracks_canonical_stations(self):
        with fake_modules():
            forward = posts.replace([], self.roads, self.box, self.country_at, frame=Frame())
            backward = posts.replace([], [('A', Road(), self.line[::-1], 0.)],
                                     self.box, self.country_at, frame=Frame())
        self.assertEqual(forward, backward)

    def test_bbox_short_circuit_outside_norway(self):
        class Elsewhere(Frame):
            lat0, lon0 = 40., 5.
        original = [['eu_marker_de', 50., 5., 3., 0., 1., 1.15, 0.]]
        with fake_modules():
            out = posts.replace(original, self.roads, self.box, self.country_at, frame=Elsewhere())
        self.assertEqual(out, original)

    def test_measured_width_clearance_exempts_own_nominal_width(self):
        # On the old code, a nominal 8.0 m road rejected a post at 4.5 m:
        # nominal half width 4.0 + 0.65 > 4.5. Correct code passes own=fid.
        calls = []
        def clearance(q, owner, margin):
            calls.append(owner)
            return owner == 'A'
        old = [['eu_marker_de', 50., 5.1, 3., 0., 1., 1.15, 0.]]
        with fake_modules(clear_fn=clearance):
            out = posts.replace(old, self.roads, self.box, self.country_at, frame=Frame())
        self.assertEqual(len(out), 3)
        self.assertEqual([r[0] for r in out], [posts.GROUP]*3)
        self.assertTrue(calls and all(c == 'A' for c in calls))

    def test_no_valid_replacements_preserves_visible_original(self):
        original = [['eu_marker_de', 50., 5.1, 3., 0., 1., 1.15, 0.]]
        with fake_modules():
            out = posts.replace(original, self.roads, self.box, self.country_at,
                                frame=Frame(), ground=lambda x,y: np.full_like(x, 25.0))
        self.assertEqual(out, original)

    def test_legacy_diagnostic_mode_keeps_original_rows(self):
        old = [['eu_marker_de', 50., 5.1, 3., 0., 1., 1.15, 0.]]
        with fake_modules(), patch.object(posts, 'MODE', 'legacy'):
            out = posts.replace(old, self.roads, self.box, self.country_at, frame=Frame())
        self.assertEqual(out, old)

    def test_failing_one_candidate_only_falls_back_for_that_candidate(self):
        old = [['eu_marker_de', x, 5.1, 3., 0., 1., 1.15, 0.] for x in (50., 100., 150.)]
        # Only the 50 m replacement survives. 100 and 150 retain legacy rows.
        def clearance(q, owner, margin):
            return owner == 'A' and q[0] < 75.0
        with fake_modules(clear_fn=clearance):
            out = posts.replace(old, self.roads, self.box, self.country_at, frame=Frame())
        self.assertEqual(len(out), 3)
        self.assertCountEqual([r[0] for r in out], [posts.GROUP, 'eu_marker_de', 'eu_marker_de'])

    def test_distance_to_polyline(self):
        self.assertAlmostEqual(posts._nearest_polyline_distance([5., 6.], [[0.,0.], [10.,0.]]), 6.)
        self.assertTrue(math.isinf(posts._nearest_polyline_distance([5.,6.], [[0.,0.]])))


if __name__ == '__main__':
    unittest.main()
