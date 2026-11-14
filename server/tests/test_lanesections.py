"""Merged source lane changes must agree in paint, physical width and navigation."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from test_roads import FRAME, flat, rules  # noqa: E402,F401  (first: it puts the server on the path)
import lanesections  # noqa: E402
import markings  # noqa: E402
import navgraph  # noqa: E402
import osmtags  # noqa: E402
import railtags  # noqa: E402
import roads  # noqa: E402
from test_roadlanes import highway  # noqa: E402


def merged(reverse=False, left=False, bridge=False):
    f = highway([[(0, 0), (1000, 0)]], direction=-1 if reverse else 1,
                flags=[(0, "is_bridge")] if bridge else [])
    osmtags.join(f, {1: {"lanes": 3}})
    turn = "slight_left|none|none|none" if left else "none|none|none|slight_right"
    # Mirrored travel and geometry: source placement counts from travel-left.
    place = "middle_of:3" if left else "middle_of:2"
    a = np.empty(1, object)
    a[0] = [(0., .7, "1", {"lanes": "3"}), (.7, 1., "2", {
        "lanes": "4", "turn:lanes": turn, "placement": place})]
    f.attrs[railtags.LANE_PARTS] = a
    f.rules["connectors"] = rules([(0, "start", 0, 0), (0, "end", 1, 1)])
    f.rules["max_speed_kmh"] = rules([(0, 100, 0, .8), (0, 80, .8, 1)], "float")
    return f


def lane_offsets(f, i, spec, pts, section=False):
    """The mean lateral offsets of section i's lane separators (paint: extras, with section_lines' auxiliary grid)."""
    ex = roads.extras(spec, pts, -76., country="DE")
    if section:
        ex = roads.section_lines(ex, f, i, spec, pts, FRAME, -76., False, "DE", None)
    return [(float(p[:, 1].mean()), float(np.ptp(p[:, 1])), look, w, p) for look, w, p in ex if look.get("kind") == "lane"]


def enu(f, i):
    ll = f.parts(i)[0][0]
    e, n, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
    return ll, np.c_[e, n, np.zeros(len(e))]


class LaneSections(unittest.TestCase):
    def test_through_lines_stay_fixed_as_fourth_lane_opens(self):
        """The 3-lane section's separators go on straight beside the opening fourth lane (the lanes' grid stands
        roads.driving_offset off the pavement's centre: the wider right shoulder)."""
        for rev in (False, True):
            for left in (False, True):
                with self.subTest(reverse=rev, left_exit=left):
                    f = lanesections.split(merged(rev, left), FRAME)
                    self.assertEqual(len(f), 2)
                    sp = roads.road_specs(f)
                    self.assertEqual([s.lanes for s in sp], [3, 4])
                    ll, pts = enu(f, 1)
                    tp = roads.road_tapers(f)[1]
                    self.assertEqual(len(tp), 1)
                    ws = roads.taper_widths(sp[1].width, tp, ll[:, 0], ll[:, 1])
                    self.assertAlmostEqual(ws[0], sp[0].width)
                    self.assertAlmostEqual(ws[-1], sp[1].width)
                    through = sorted({round(y, 1) for y, *_ in lane_offsets(f, 0, sp[0], enu(f, 0)[1])})
                    self.assertEqual(len(through), 2)
                    ex = roads.extras(sp[1], pts, -76., country="DE", ws=ws)
                    ex = roads.section_lines(ex, f, 1, sp[1], pts, FRAME, -76., False, "DE", ws)
                    lanes = [line for look, _w, line in ex if look.get("kind") == "lane"]
                    for target in through:
                        nearest = [p for p in lanes if abs(float(p[:, 1].mean()) - target) < .12]
                        self.assertTrue(nearest, (rev, left, target, [p[:, 1].mean() for p in lanes]))
                        self.assertLess(max(np.ptp(p[:, 1]) for p in nearest), .12)

    def test_rules_and_connector_ids_survive_split(self):
        f = lanesections.split(merged(), FRAME)
        con = f.rules["connectors"]
        self.assertIn("start", con.value[con.feature == 0])
        self.assertIn("end", con.value[con.feature == 1])
        names = [set(con.value[con.feature == i]) for i in (0, 1)]
        self.assertEqual(len(names[0] & names[1]), 1)
        speed = f.rules["max_speed_kmh"]
        self.assertAlmostEqual(float(speed.start[speed.feature == 1][-1]), 1 / 3)
        graph = navgraph.build(FRAME, [f], flat, (500., 0.), window=1500.)
        self.assertTrue(graph["edges"])
        self.assertEqual({e[3] for e in graph["edges"]}, {3, 4})
        self.assertIs(lanesections.split(f, FRAME), f)

    def test_bridge_sections_have_same_counts_widths_and_tapers(self):
        f = lanesections.split(merged(bridge=True), FRAME)
        specs = roads.bridge_specs(f)
        self.assertEqual([s.lanes for s in specs], [3, 4])
        self.assertEqual(len(roads.deck_tapers(f)[1]), 1)

    def test_missing_tags_use_primary_without_inventing_lanes(self):
        f = merged()
        f.attrs[railtags.LANE_PARTS][0][0][3].clear()
        g = lanesections.split(f, FRAME)
        self.assertEqual([sp.lanes for sp in roads.road_specs(g)], [3, 4])
        for bad in ("2;3", "3.5", None):
            f = merged()
            f.attrs[railtags.LANE_PARTS][0][1][3]["lanes"] = bad
            self.assertIs(lanesections.split(f), f)

    def test_two_way_links_and_incomplete_source_references_rejected(self):
        for variant in ("two_way", "link", "gap", "overlap"):
            f = merged()
            if variant == "two_way":
                f.rules.pop("oneway")
            elif variant == "link":
                f.rules["subclass"] = rules([(0, "link")])
            else:
                row = f.attrs[railtags.LANE_PARTS][0][1]
                f.attrs[railtags.LANE_PARTS][0][1] = (.72 if variant == "gap" else .68, *row[1:])
            self.assertIs(lanesections.split(f), f)


def fork():
    f = highway([[(-400, 0), (0, 0)], [(0, 0), (400, 0)], [(0, 0), (60, -8), (180, -60)]])
    osmtags.join(f, {1: {"lanes": 3}, 2: {"lanes": 3}, 3: {"lanes": 1}})
    f.rules["subclass"] = rules([(2, "link")])
    a = np.empty(3, object)
    a[:] = None
    a[0] = [(0., .5, "1", {"lanes": "3"}), (.5, 1., "2", {
        "lanes": "4", "turn:lanes": "none|none|none|slight_right", "placement": "middle_of:2"})]
    f.attrs[railtags.LANE_PARTS] = a
    return f


class RampMouths(unittest.TestCase):
    def test_slip_lane_starts_beside_through_lanes(self):
        """The ramp leaves from the auxiliary lane's centre: one lane outside the through lanes' grid."""
        f = lanesections.split(fork(), FRAME)
        i = f.index("id002")
        p = f.parts(i)[0][0]
        e, n, _ = FRAME.geodetic_to_enu(p[:, 1], p[:, 0], 0.)
        lane = roads.LANE_W["motorway"]
        grid = lanesections._grid("motorway", 4, lane)
        self.assertAlmostEqual(n[0], grid - 1.5 * lane - lane / 2, delta=.08)
        self.assertLess(abs(n[-1] + 60.), .3)
        self.assertGreater(np.diff(e).min(), 0.)

    def test_copies_use_mouth_owner_even_when_mainline_absent(self):
        owner = fork()
        ramp = highway([[(0, 0), (60, -8), (180, -60)]], link=True)
        osmtags.join(ramp, {1: {"lanes": 1}})
        only = lanesections.split(ramp, FRAME, lambda _la, _lo: owner)
        full = lanesections.split(owner, FRAME)
        np.testing.assert_allclose(only.parts(0)[0][0], full.parts(full.index("id002"))[0][0], atol=1e-10)

    def test_unrelated_two_way_or_opposite_flow_link_is_not_moved(self):
        for case in ("two_way", "opposite", "other_class"):
            f = fork()
            if case == "two_way":
                f.rules["oneway"] = rules([(0, 1), (1, 1)], "float")
            elif case == "opposite":
                f.rules["oneway"] = rules([(0, 1), (1, 1), (2, -1)], "float")
            else:
                f.attrs["class"][2] = "primary"
            raw = f.parts(2)[0][0].copy()
            out = lanesections.split(f, FRAME)
            np.testing.assert_allclose(out.parts(out.index("id002"))[0][0], raw, atol=1e-10)


class PlacementContinuity(unittest.TestCase):
    def test_shifted_mainline_does_not_cut_a_hole_in_its_continuation(self):
        f = lanesections.split(fork(), FRAME)
        sp = roads.road_specs(f)
        rows, seen = [], {}
        for i, s in enumerate(sp):
            ll = f.parts(i)[0][0]
            e, n, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            fid = str(f.ids[i])
            rows.append((fid, s, np.c_[e, n, np.zeros(len(e))], -76.))
            seen[fid] = (f, i, s)
        # Add mapped shared junction identities, including the shifted ramp.
        rows_con = []
        for i, fid in enumerate(f.ids):
            if fid == 'id001':
                rows_con.append((i, 'junction', 0, 0))
            elif fid == 'id002':
                rows_con.append((i, 'junction', 0, 0))
            elif '~lanes500000000-' in fid:
                rows_con.append((i, 'junction', 1, 1))
        table = f.rules['connectors']
        f.rules['connectors'] = rules(rows_con + [
            (int(i), v, a, b) for i, v, a, b in zip(table.feature, table.value, table.start, table.end)])
        pairs, stems, _whole = roads.section_continuations(rows, seen)
        aux = next(str(fid) for fid in f.ids if '~lanes500000000-' in fid)
        self.assertIn((aux, 'id001'), pairs)
        self.assertIn(('id001', aux), pairs)
        self.assertIn(('id001', 'id002'), stems)
        self.assertNotIn((aux, 'id002'), pairs)


class LaneConsumers(unittest.TestCase):
    def test_visual_traffic_stays_on_through_lane(self):
        import distanttraffic
        f = lanesections.split(merged(), FRAME)
        paths = distanttraffic.build(FRAME, [f], flat, (-50, -80, 1100, 80))
        self.assertTrue(paths)
        for path in paths:
            self.assertLess(max(abs(p[1]) for p in path["points"]), .15)

    def test_auxiliary_separator_is_wide_and_has_short_dashes(self):
        f = lanesections.split(merged(), FRAME)
        sp = roads.road_specs(f)[1]
        ll, pts = enu(f, 1)
        got = lane_offsets(f, 1, sp, pts, section=True)
        outer = min(y for y, *_ in got)
        aux = [(look, w, p) for y, _r, look, w, p in got if abs(y - outer) < .1]
        self.assertTrue(aux)
        self.assertTrue(all(abs(w - 2 * roads.CENTRE["w"] * 1.25) < .1 or w > .2 for _look, w, _p in aux))
        through = [(look, w) for y, _r, look, w, p in got if abs(y - outer) > .1]
        self.assertTrue(all(w < min(w2 for _l, w2, _p in aux) for _l, w in through))
        self.assertTrue(all(look["dash"][0] <= 3. for look, _w, _p in aux if isinstance(look.get("dash"), tuple)))

    def test_gore_stays_outside_through_lanes_after_placement(self):
        f = lanesections.split(fork(), FRAME)
        rows = []
        for i, sp in enumerate(roads.road_specs(f)):
            ll = f.parts(i)[0][0]
            e, n, _ = FRAME.geodetic_to_enu(ll[:, 1], ll[:, 0], 0.)
            rows.append((str(f.ids[i]), sp, np.c_[e, n, np.zeros(len(e))], -76.))
        gore = markings.gores(rows)
        stripe = [p for k, _w, p in gore if k == "stripe"]
        self.assertTrue(stripe)
        self.assertTrue(all(np.max(p[:, 1]) <= -5.4 + .1 for p in stripe))

    def test_placement_only_source_way_is_left_to_placement(self):
        """One lane count along the whole segment: no sections; roads.Placement moves it as any placed road."""
        f = highway()
        osmtags.join(f, {1: {"lanes": 4}})
        f.attrs[railtags.lane_attr("placement")] = np.array(["middle_of:2"], object)
        self.assertIs(lanesections.split(f, FRAME), f)
        sp = roads.road_specs(f)[0]
        ll = f.parts(0)[0][0]
        sh = roads.placement_shift(f, 0, ll[:, 0], ll[:, 1], sp)
        np.testing.assert_allclose(sh, -.5 * roads.LANE_W["motorway"], atol=.05)

class MissingPlacement(unittest.TestCase):
    def test_turn_tags_do_not_invent_a_reference_offset(self):
        f = merged()
        f.attrs[railtags.LANE_PARTS][0][1][3].pop('placement')
        out = lanesections.split(f, FRAME)
        p = out.parts(1)[0][0]
        _e, n, _z = FRAME.geodetic_to_enu(p[:, 1], p[:, 0], 0.)
        self.assertLess(float(np.abs(n).max()), .1)
        self.assertEqual(lanesections.profile(out, 1)['extra'], 0.)


class LaneCountVariants(unittest.TestCase):
    def test_two_three_and_four_through_lanes(self):
        for base in (2, 3, 4):
            f = merged()
            osmtags.join(f, {1: {"lanes": base}})
            parts = f.attrs[railtags.LANE_PARTS][0]
            parts[0][3]['lanes'] = str(base)
            tags = parts[1][3]
            tags['lanes'] = str(base + 1)
            tags['turn:lanes'] = '|'.join(['none'] * base + ['slight_right'])
            tags['placement'] = (f'middle_of:{(base + 1) // 2}' if base % 2 else f'right_of:{base // 2}')
            out = lanesections.split(f, FRAME)
            specs = roads.road_specs(out)
            through = sorted({round(y, 1) for y, *_ in lane_offsets(out, 0, specs[0], enu(out, 0)[1])})
            self.assertEqual(len(through), base - 1)
            _ll, pts = enu(out, 1)
            for left_hand in (False, True):
                ex = roads.section_lines([], out, 1, specs[1], pts, FRAME, -76., left_hand, 'DE', None)
                for y in through:
                    self.assertTrue(any(abs(float(p[:, 1].mean()) - y) < .12 for _look, _w, p in ex), (base, y))

    def test_chevron_orientation_survives_reversed_geometry(self):
        main = roads.Spec(16.8, 'paved', False, 'motorway', True, 2.5, 1, lanes=3)
        ramp = roads.Spec(6., 'paved', False, 'motorway', True, 2.5, 1, lanes=1, link=True)
        p = np.c_[np.arange(100., 401., 10.), -np.arange(0., 31., 1.), np.zeros(31)]
        main_pts = np.array([[0., 0., 0.], [400., 0., 0.]])
        forward = markings.gores([('m', main, main_pts, 0.), ('r', ramp, p, 0.)])
        backward = markings.gores([('m', main, main_pts, 0.), ('r', ramp._replace(dir=-1), p[::-1], 0.)])
        a = [q for k, _w, q in forward if k == 'stripe']
        b = [q for k, _w, q in backward if k == 'stripe']
        self.assertTrue(a)
        self.assertEqual(len(a), len(b))
        for x, y in zip(a, b):
            np.testing.assert_allclose(x, y)


class ConsecutiveWidening(unittest.TestCase):
    def test_sections_keep_one_grid_across_three_four_five_four_three(self):
        for side in (-1, 1):
            # Fractions are rounded on disk, so the cut at x=400 is less than
            # a centimetre from the matching source vertex.
            f = highway([[(0, 0), (200, 0), (400, 0), (600, 0), (800, 0), (1000, 0)]])
            osmtags.join(f, {1: {"lanes": 3}})
            bounds = [0., *map(float, np.array([.2, .4, .6, .8], np.float32)), 1.]
            cols = np.empty(1, object)
            cols[0] = []
            for j, n in enumerate((3, 4, 5, 4, 3)):
                turns = ['none'] * 3
                exits = ['slight_right' if side < 0 else 'slight_left'] * (n - 3)
                tags = {'lanes': str(n), 'placement': f'middle_of:{2 if side < 0 else n - 1}',
                        'turn:lanes': '|'.join(turns + exits if side < 0 else exits + turns)}
                cols[0].append((bounds[j], bounds[j + 1], str(j + 1), tags))
            f.attrs[railtags.LANE_PARTS] = cols
            out = lanesections.split(f, FRAME)
            specs = roads.road_specs(out)
            self.assertEqual([s.lanes for s in specs], [3, 4, 5, 4, 3])
            rows, edges = [], []
            for i, spec in enumerate(specs):
                ll, pts = enu(out, i)
                self.assertGreater(np.min(np.diff(pts[:, 0])), .009)
                widths = roads.taper_widths(spec.width, roads.road_tapers(out)[i], ll[:, 0], ll[:, 1])
                widths = np.full(len(ll), spec.width) if widths is None else widths
                rows.append((pts, widths))
                edges.append(pts[:, 1] - side * widths / 2)      # (the through lanes' side: it runs straight)
            for (a, aw), (b, bw) in zip(rows, rows[1:]):
                np.testing.assert_allclose(a[-1, :2], b[0, :2], atol=.035)
                self.assertAlmostEqual(aw[-1], bw[0], delta=.02)
            five, widths = rows[2]
            self.assertAlmostEqual(widths[0], specs[1].width, delta=.02)
            self.assertAlmostEqual(max(widths), specs[2].width, delta=.02)
            self.assertLess(np.ptp(np.concatenate(edges)), .07)

    def test_two_lane_widening_does_not_paint_the_outer_separator_prematurely(self):
        f = merged()
        tags = f.attrs[railtags.LANE_PARTS][0][1][3]
        tags.update(lanes='5', **{'turn:lanes': 'none|none|none|slight_right|slight_right'})
        out = lanesections.split(f, FRAME)
        spec = roads.road_specs(out)[1]
        _ll, pts = enu(out, 1)
        got = lane_offsets(out, 1, spec, pts, section=True)
        ys = sorted({round(y, 1) for y, *_ in got})
        outer = [p for y, _r, _l, _w, p in got if abs(y - ys[0]) < .12]
        self.assertTrue(outer)
        self.assertGreaterEqual(min(p[:, 0].min() for p in outer), pts[0, 0] + 44.)
        through = [p for y, _r, _l, _w, p in got if abs(y - ys[-1]) < .12]
        self.assertLess(min(p[:, 0].min() for p in through), pts[0, 0] + 18.)

if __name__ == "__main__":
    unittest.main()
