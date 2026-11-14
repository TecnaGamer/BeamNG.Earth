"""Collision continuity of bounded, connected bridge forks; no engine or network required."""
import json
import sys
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadmesh
import roads
from test_roads import FRAME, flat


def chunk(fid, pts, width=8):
    return dict(fid=fid, n=np.asarray(pts, float).tolist(), jn=np.asarray(pts, float).tolist(),
                w=width, m='road_asphalt_2lane', fixed=True, deck=True)


def fork(z=20, suffix=''):
    return [chunk('a' + suffix, [(-35, 0, z + 1.4), (0, 0, z)]),
            chunk('b' + suffix, [(0, 0, z), (25, 5, z - 1)]),
            chunk('c' + suffix, [(0, 0, z), (25, -8, z - 2)])]


def heights(mesh, xy, material=None):
    """Vertical triangle intersections, including overlap interiors (not just mesh vertices)."""
    p, t, _uv, mats, mi = mesh
    if material is not None:
        t = t[mi == mats.index(material)]
    a, b, c = p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]
    det = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    keep = np.abs(det) > 1e-9
    a, b, c, det = a[keep], b[keep], c[keep], det[keep]
    out = []
    for x, y in np.asarray(xy):
        u = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / det
        v = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / det
        w = 1 - u - v
        hit = (u >= -1e-8) & (v >= -1e-8) & (w >= -1e-8)
        out.append(u[hit] * a[hit, 2] + v[hit] * b[hit, 2] + w[hit] * c[hit, 2])
    return out


def tyre_tracks(chunks):
    points = []
    for c in chunks:
        p = np.asarray(c['n'])
        for a, b in zip(p[:-1], p[1:]):
            d = b[:2] - a[:2]
            normal = np.array([-d[1], d[0]]) / np.linalg.norm(d)
            for offset in (-0.9, 0, 0.9):
                points.extend(a[:2] + np.linspace(0, 1, 17)[:, None] * d + normal * offset)
    return np.asarray(points)


def fixture():
    data = json.loads((Path(__file__).parent / 'fixtures/bridge_fork_swiss.json').read_text())
    parts = [(p['fid'], roads.Spec(p['width'], 'paved', True, 'primary', False), np.c_[p['xy'], p['z']], 7.72)
             for p in data['parts']]
    all_chunks = roads._deck_chunks(parts, (-150, -70, 20, 70))
    chunks = [c for c in all_chunks if not c.get('ctx')]
    context = [c for c in all_chunks if c.get('ctx')]
    pads = [(np.array(p['ring']), np.array(p['centre']), [(np.array(a), np.array(b)) for a, b in p['walls']])
            for p in data['pads']]
    return parts, chunks, context, pads, data['ground']


class BridgeJunctionSurfaceTest(unittest.TestCase):
    def test_close_free_abutments_keep_their_pinned_heights(self):
        parts = [chunk('in', [(-25, 0, 21), (0, 0, 20)]),
                 chunk('exit1', [(0, 0, 20), (25, 5, 19)]),
                 chunk('exit2', [(0, 0, 20), (25, 8, 18.5)])]
        self.assertEqual(roadmesh._junction_surfaces(parts), {},
                         'nearby free ends with different grades must keep their ground approach heights')

    def test_guardrail_runs_follow_the_corrected_deck_edge(self):
        _parts, chunks, context, pads, _ground = fixture()
        fields = roadmesh._junction_surfaces(chunks, context)
        runs = roadmesh.parapet_runs(chunks, context, pads=pads)
        matched = [run for run in runs if chunks[run['deck']]['fid'] in fields]
        self.assertTrue(matched)
        for run in matched:
            field = fields[chunks[run['deck']]['fid']]
            np.testing.assert_allclose(run['z'], field.at(run['edge']), atol=1e-6)

    def test_connected_forks_have_same_eligibility_across_tile_boxes(self):
        parts = [chunk('a', [(20, 0, 20), (40.5, 0, 20)]),
                 chunk('b', [(20, 12, 20), (40.5, 0, 20)]),
                 chunk('link', [(40.5, 0, 20), (100.5, 0, 20)]),
                 chunk('d', [(100.5, 0, 20), (158.5, 12, 20)]),
                 chunk('e', [(100.5, 0, 20), (158.5, -12, 20)])]
        specs = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False),
                  np.asarray(c['n'], float), 7.72) for c in parts]
        for box in ((-256, -100, 0, 100), (0, -100, 256, 100)):
            with self.subTest(box=box):
                output = roads._deck_chunks(specs, box)
                self.assertTrue({'d', 'e'} <= {c['fid'] for c in output if c.get('ctx')})
                self.assertFalse(roadmesh._junction_surfaces(
                    [c for c in output if not c.get('ctx')], [c for c in output if c.get('ctx')]))

    def test_connected_forks_do_not_partially_regrade_shared_span(self):
        chunks = [chunk('a', [(-20, 0, 20), (0, 0, 20)]),
                  chunk('b', [(0, 0, 20), (-15, 12, 20)]),
                  chunk('c', [(0, 0, 20), (30, 0, 20)]),
                  chunk('d', [(30, 0, 20), (45, 12, 20)]),
                  chunk('e', [(30, 0, 20), (45, -12, 20)])]
        # (2026-11-12: two joints a span links are one cluster, one field over all of it, its lone ends pinned; never a
        # field for only one of the forks)
        got = roadmesh._junction_surfaces(chunks)
        self.assertEqual(set(got), {'a', 'b', 'c', 'd', 'e'})
        self.assertEqual(len({id(f) for f in got.values()}), 1,
                         'a span connecting two forks must not be regraded for only one junction')
        field = got['a']
        free = np.array([(-20, 0, 20), (-15, 12, 20), (45, 12, 20), (45, -12, 20)], float)
        np.testing.assert_allclose(field.at(free[:, :2]), free[:, 2], atol=roadmesh.JUNCTION_CLUSTER_PIN_TOL_M)
        with mock.patch.object(roadmesh, 'JUNCTION_CLUSTER_ON', False):
            self.assertEqual(roadmesh._junction_surfaces(chunks), {})
        # (the cluster's deck skins in world texture coordinates at the decks' own texture length)
        self.assertEqual(roadmesh.CLUSTER_SKIN_TILE_M, roads.DECK["tl"])

    def test_captured_fork_has_no_stacked_collision_faces_in_driving_lanes(self):
        _parts, chunks, ctx, pads, ground = fixture()
        mesh = roadmesh.deck(chunks, (0, 0, 0), context=ctx, pads=pads, ground=ground)
        probes = tyre_tracks([c for c in ctx if c.get('jn')])
        top = heights(mesh, probes, roadmesh.DECK_TOP)
        self.assertTrue(all(len(z) for z in top), 'continuous collision beneath all three driving lines')
        self.assertLess(max(np.ptp(z) for z in top), 0.002, 'old independent surfaces overlap by 0.361 m')
        all_faces = heights(mesh, probes)
        self.assertLess(max(max(a) - max(t) for a, t in zip(all_faces, top)), 0.002,
                        'no slab skirt or barrier protrudes through a driving lane')
        self.assertLess(len(mesh[0]), 9000)
        self.assertLess(len(mesh[1]), 11000, 'bounded mesh, not the old 46,000-vertex prototype')

    def test_captured_skin_and_markings_follow_same_collision_triangles(self):
        _parts, chunks, ctx, pads, ground = fixture()
        mesh = roadmesh.deck(chunks, (0, 0, 0), context=ctx, pads=pads, ground=ground)
        skin = roadmesh.build(FRAME, chunks, flat, (0, 0, 0), context=ctx)
        p, t, _uv, mats, mi = skin
        rng = np.random.default_rng(41)
        selected = rng.choice(len(t), min(600, len(t)), replace=False)
        points = p[t[selected]].mean(1)
        z = heights(mesh, points[:, :2], roadmesh.DECK_TOP)
        differences = []
        for i, point, collision in zip(selected, points, z):
            if not len(collision):
                continue  # edge markings can deliberately extend past the deck's carriageway
            lift = roadmesh.LIFT[roadmesh.layer_of({'m': mats[mi[i]]})] + roadmesh.DECK_SKIN
            differences.append(abs(point[2] - lift - max(collision)))
        self.assertGreater(len(differences), 100)
        self.assertLess(max(differences), 0.002)

    def test_connected_ground_approach_is_not_overlaid_by_pad_or_wall(self):
        _parts, chunks, ctx, pads, ground = fixture()
        mesh = roadmesh.deck(chunks, (0, 0, 0), context=ctx, pads=pads, ground=ground)
        approach = next(g for g in ground if g['fid'].startswith('3488'))
        p = np.asarray(approach['n'])
        centre = pads[1][1]
        if np.linalg.norm(p[-1, :2] - centre[:2]) < 1:
            p = p[::-1]
        arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
        s = np.linspace(1, 20, 180)
        xy = np.c_[np.interp(s, arc, p[:, 0]), np.interp(s, arc, p[:, 1])]
        for offset in (-0.9, 0, 0.9):
            d = np.gradient(xy, axis=0)
            normal = np.c_[-d[:, 1], d[:, 0]] / np.linalg.norm(d, axis=1)[:, None]
            hits = heights(mesh, xy + offset * normal)
            self.assertFalse(any(len(z) for z in hits), 'terrain supplies this road; no false pad floor or wall')

    def test_sloped_fork_preserves_free_abutments_and_grade(self):
        chunks = fork()
        mesh = roadmesh.deck(chunks, (0, 0, 0), parapets=[])
        probes = tyre_tracks(chunks)
        top = heights(mesh, probes, roadmesh.DECK_TOP)
        self.assertLess(max(np.ptp(z) for z in top if len(z)), 0.002)
        for c in chunks:
            p = np.asarray(c['n'])
            end = p[0] if p[0, 0] != 0 else p[-1]
            self.assertAlmostEqual(max(heights(mesh, [end[:2]], roadmesh.DECK_TOP)[0]), end[2], delta=0.002)
        surface = roadmesh._junction_surfaces(chunks)['a']
        self.assertGreater(np.ptp(surface.at(np.array([[-30, 0], [22, -7]]))), 2.0,
                           'the common surface retains real longitudinal slope')

    def test_stacked_forks_and_unconnected_crossing_stay_separate(self):
        low, high = fork(), fork(35, 'upper')
        crossing = chunk('crossing', [(-5, -25, 28), (-5, 25, 28)])
        fields = roadmesh._junction_surfaces(low + high + [crossing])
        self.assertNotIn('crossing', fields)
        self.assertIsNot(fields['a'], fields['aupper'])
        np.testing.assert_allclose(fields['aupper'].at([[4, 0], [8, -1]]) - fields['a'].at([[4, 0], [8, -1]]), 15)

    def test_complete_profiles_make_adjacent_tiles_choose_same_surface(self):
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        # The left tile holds an abutment while the fork lies outside its old 24 m context.
        surfaces = []
        for box in ((-40, -20, -30, 20), (-30, -20, 5, 20), (5, -20, 30, 20)):
            chunks = roads._deck_chunks(parts, box)
            fields = roadmesh._junction_surfaces([c for c in chunks if not c.get('ctx')],
                                                [c for c in chunks if c.get('ctx')])
            self.assertEqual(set(fields), {'a', 'b', 'c'})
            surfaces.append(fields['a'].at([[-30, 0], [-15, 0], [1, 0], [15, 1]]))
        np.testing.assert_allclose(surfaces, np.broadcast_to(surfaces[0], (3, 4)), atol=1e-10)

    def test_context_order_and_anchor_rigid_transform_do_not_change_surface(self):
        chunks = fork()
        expected = roadmesh._junction_surfaces(chunks)['a']
        q = np.array([[-30, 0], [-2, 1], [4, -2], [20, 4]])
        np.testing.assert_allclose(roadmesh._junction_surfaces(chunks[::-1])['a'].at(q), expected.at(q))
        angle = 0.31
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        delta = np.array([1002, -833, -8])
        moved = []
        for c in chunks:
            p = np.asarray(c['n'])
            moved.append(chunk(c['fid'], np.c_[p[:, :2] @ rotation.T, p[:, 2]] + delta))
        field = roadmesh._junction_surfaces(moved)['a']
        np.testing.assert_allclose(field.at(q @ rotation.T + delta[:2]) - delta[2], expected.at(q), atol=1e-9)

    def test_profile_metadata_reprojects_with_skin_after_anchor_change(self):
        import blockworker
        import geodesy
        import geotiles
        chunks = fork()
        dst = geodesy.EnuFrame(FRAME.lat0 + .015, FRAME.lon0 - .02, 0)
        mapped = blockworker._map_chunks(FRAME, dst, chunks)
        for before, after in zip(chunks, mapped):
            np.testing.assert_allclose(after['jn'], geotiles.world_map(FRAME, dst, np.asarray(before['jn'])))
            np.testing.assert_allclose(after['jn'], after['n'])
        field = roadmesh._junction_surfaces(mapped)['a']
        old = roadmesh._junction_surfaces(chunks)['a']
        xy = np.array([[-30, 0], [-2, 1], [4, -2], [20, 4]])
        world = geotiles.world_map(FRAME, dst, np.c_[xy, old.at(xy)])
        np.testing.assert_allclose(field.at(world[:, :2]), world[:, 2], atol=.002)

    def test_adjacent_render_and_collision_tiles_have_matching_join_heights(self):
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        meshes = []
        for box in ((-40, -20, 5, 20), (5, -20, 30, 20)):
            output = roads._deck_chunks(parts, box)
            chunks, ctx = [c for c in output if not c.get('ctx')], [c for c in output if c.get('ctx')]
            collider = roadmesh.deck(chunks, (0, 0, 0), context=ctx, parapets=[])
            skin = roadmesh.build(FRAME, chunks, flat, (0, 0, 0), context=ctx, clip=box)
            meshes.append((collider, skin))
        xy = np.c_[np.full(15, 5), np.linspace(-1.8, 1.8, 15)]
        z = [heights(c, xy, roadmesh.DECK_TOP) for c, _s in meshes]
        # Slabs end perpendicular to their roads, so either neighbour can supply collision at a tile edge.
        combined = [np.r_[a, b] for a, b in zip(*z)]
        self.assertTrue(all(len(h) for h in combined))
        self.assertLess(max(np.ptp(h) for h in combined), .002)
        asphalt = [heights(s, xy, 'road_asphalt_2lane') for _c, s in meshes]
        asphalt = [np.r_[a, b] for a, b in zip(*asphalt)]
        self.assertTrue(all(len(h) for h in asphalt))
        np.testing.assert_allclose([max(h) for h in asphalt],
                                   np.array([max(h) for h in combined]) + roadmesh.DECK_SKIN, atol=.002)

    def test_unsupported_fourth_arm_excludes_complete_fork(self):
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        spec = parts[0][1]
        cases = {'long': [('d', spec, np.array([[0, 0, 20], [0, 150, 20]]), 7.72)],
                 'multipart': [('d', spec, np.array([[0, 0, 20], [0, 25, 20]]), 7.72),
                               ('d', spec, np.array([[80, 0, 20], [95, 0, 20]]), 7.72)]}
        for name, extra in cases.items():
            for box in [(-50, -30, 20, 30), (-15, -30, 50, 30)]:
                with self.subTest(case=name, box=box):
                    output = roads._deck_chunks(parts + extra, box)
                    self.assertFalse(roadmesh._junction_surfaces(
                        [c for c in output if not c.get('ctx')], [c for c in output if c.get('ctx')]))

    def test_exclusion_preserves_stacked_fork_and_duplicate_order(self):
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork() + fork(40, '_upper')]
        spec = parts[0][1]
        extra = [('d', spec, np.array([[80, 0, 20], [95, 0, 20]]), 7.72),
                 ('d', spec, np.array([[0, 0, 20], [0, 25, 20]]), 7.72)]
        for order in (parts + extra, list(reversed(parts + extra))):
            output = roads._deck_chunks(order, (-50, -30, 50, 30))
            fields = roadmesh._junction_surfaces([c for c in output if not c.get('ctx')],
                                                 [c for c in output if c.get('ctx')])
            self.assertEqual(set(fields), {'a_upper', 'b_upper', 'c_upper'})

    def test_clipped_long_or_ambiguous_multipart_profiles_are_not_short_forks(self):
        chunks = fork()
        chunks[0]['jn'] = [[-180, 0, 27.2], [0, 0, 20]]
        self.assertFalse(roadmesh._junction_surfaces(chunks), 'a short visible fragment does not prove completeness')
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        parts.append((parts[0][0], parts[0][1], np.array([[90, 0, 25], [100, 0, 25]]), 7.72))
        output = roads._deck_chunks(parts, (-50, -20, 110, 20))
        self.assertTrue(all('jn' not in c for c in output if c.get('fid') == 'a'))

    def test_sidewalk_taper_and_lane_shift_paths_retain_existing_geometry(self):
        # These newer deck shapes are deliberately outside the common-field eligibility. Their own geometry
        # (including sidewalk lift, texture repeats and traffic direction) must survive the port unchanged.
        from unittest.mock import patch
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        box = (-50, -30, 50, 30)
        cases = [dict(walks={p[0]: (1.5, [1]) for p in parts}),
                 dict(shift_of=lambda fid, pts: np.full(len(pts), 1.25)),
                 dict(width_of=lambda fid, pts: np.linspace(8., 10., len(pts)))]
        for options in cases:
            with self.subTest(options=next(iter(options))):
                chunks = roads._deck_chunks(parts, box, **options)
                self.assertTrue(all(c.get('jskip') for c in chunks if c.get('deck')))
                ctx, draw = [c for c in chunks if c.get('ctx')], [c for c in chunks if not c.get('ctx')]
                self.assertFalse(roadmesh._junction_surfaces(draw, ctx))
                with patch.object(roadmesh, '_junction_surfaces', return_value={}):
                    expected = roadmesh.build(FRAME, draw, flat, (0, 0, 0), context=ctx)
                actual = roadmesh.build(FRAME, draw, flat, (0, 0, 0), context=ctx)
                for a, b in zip(actual, expected):
                    np.testing.assert_array_equal(a, b)
                if 'walks' in options:
                    walks = [c for c in draw if c['m'] == roads.DECK_WALK['m']]
                    self.assertTrue(walks)
                    self.assertTrue(all(abs(np.array(c['n'])[0, 2] - 21.43) < .001 for c in walks if c['fid'] == 'a'))
                if 'width_of' in options:
                    self.assertTrue(any('ws' in c and np.ptp(c['ws']) > 1 for c in chunks))
                if 'shift_of' in options:
                    incoming = next(c for c in ctx if c['fid'] == 'a')
                    np.testing.assert_allclose(np.array(incoming['n'])[:, 1], 1.25, atol=.001)

    def test_one_unsupported_arm_excludes_the_whole_connected_fork(self):
        parts = [(c['fid'], roads.Spec(c['w'], 'paved', True, 'primary', False), np.array(c['n']), 7.72)
                 for c in fork()]
        parts.append(('d', parts[0][1], np.array([[0., 0., 20.], [5., 30., 22.]]), 7.72))
        output = roads._deck_chunks(parts, (-50, -40, 50, 40), walks={'d': (1.5, [1])})
        self.assertFalse(roadmesh._junction_surfaces(output))

    def test_shared_fork_keeps_mixed_dirt_strip_fade_attributes(self):
        from test_roadmesh import _strip
        chunks = fork()
        for follow in (False, True):
            with self.subTest(follow=follow):
                mesh = roadmesh.build(FRAME, chunks + [_strip([(-30, 45, 0), (30, 45, 0)], slen=60)],
                                      flat, (0, 0, 0), context=chunks, follow=follow)
                self.assertEqual(len(mesh), 6)
                p, t, uv, mats, mi, fade = mesh
                self.assertEqual(fade.shape, (len(p), 2))
                self.assertTrue(np.isfinite(fade).all())
                self.assertTrue(((fade >= 0) & (fade <= 1)).all())
                paved = np.unique(t[mi != mats.index('bne_dirt_variation_01')])
                np.testing.assert_allclose(fade[paved], .5)
                self.assertLess(t.max(), len(p))

    def test_legacy_decks_without_metadata_retain_original_profiles(self):
        chunks = fork()
        for c in chunks:
            del c['jn']
        self.assertFalse(roadmesh._junction_surfaces(chunks))
        # Unrelated filler pads retain their legacy fan/drop rather than becoming global terrain smoothing.
        pad = (np.array([[80, 0, 10], [90, 0, 10], [85, 10, 10]]), np.array([85, 3, 10]), [])
        mesh = roadmesh.deck(chunks, (0, 0, 0), pads=[pad], parapets=[])
        self.assertAlmostEqual(max(heights(mesh, [[85, 3]], roadmesh.DECK_TOP)[0]), 10 - roadmesh.PAD_DROP)

    def test_edge_splits_keep_vertical_sides_below_shared_top(self):
        field = roadmesh._junction_surfaces(fork())['a']
        edge = np.array([[-5, 3], [20, -2]])
        xy, _j, _f = field.edge(edge)
        self.assertGreater(len(xy), 15)
        # Every refined side's top segment is exactly affine within one grid triangle.
        mid = (xy[:-1] + xy[1:]) / 2
        np.testing.assert_allclose(field.at(mid), (field.at(xy[:-1]) + field.at(xy[1:])) / 2, atol=1e-10)

    def test_pad_queries_beyond_initial_grid_extend_same_surface(self):
        field = roadmesh._junction_surfaces(fork())['a']
        points = np.array([[-52, -2], [-51, -2], [-51, 2], [-52, 2]])
        before = field.at([[-30, 0], [2, 1]])
        z = field.at(points)
        # The incoming road's 4% tangent continues; it does not wrap back through the final grid cell.
        self.assertGreater(np.ptp(z), .03)
        p, _uv, t, _src = field.top(np.c_[points, z], np.zeros((4, 2)), np.array([[0, 1, 2], [0, 2, 3]]))
        self.assertTrue(np.isfinite(p).all())
        np.testing.assert_allclose(p[:, 2], field.at(p[:, :2]), atol=1e-9)
        np.testing.assert_allclose(field.at([[-30, 0], [2, 1]]), before, atol=1e-10)
        reverse = roadmesh._junction_surfaces(fork())['a']
        np.testing.assert_allclose(reverse.at(points[::-1])[::-1], z, atol=1e-10)

    def test_source_cell_boundary_loads_all_incident_short_branches(self):
        import geodesy
        import mapcells
        import mapgeom
        from test_roads import features
        west = mapcells.cell_of(FRAME.lat0, FRAME.lon0)
        boundary = west.bounds()[2]
        frame = geodesy.EnuFrame(west.center()[0], boundary, 0)
        east = mapcells.cell_of(frame.lat0, boundary + .0001)

        def source(lines, ids, key):
            f = features(lines, ['primary'] * len(lines), ids=ids,
                         flags=[(i, 'is_bridge') for i in range(len(lines))])
            xy = np.concatenate([np.array(line) for line in lines])
            la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0)
            f.lonlat = np.c_[lo, la]
            f.__dict__['_bnge_cell'] = (str(key), 'roads', 'fixture')
            return f

        incoming = source([[(-20, 0), (15, 0)]], ['a'], west)
        outgoing = source([[(15, 0), (40, 5)], [(15, 0), (40, -8)]], ['b', 'c'], east)
        cells = {west: incoming, east: outgoing}
        requests = []

        def fetch(key):
            requests.append(key)
            return cells.get(key, mapgeom.empty('roads'))

        for mode in ('direct', 'chain_view'):
            with self.subTest(mode=mode):
                profiles = roads.BridgeProfiles(frame, fetch, flat) if mode == 'direct' else \
                    roads.ChainView(roads.ChainStore(roads.BridgeProfiles, fetch, flat), frame)
                # This tile's ordinary 2 m source margin stops west of the source-cell boundary. Merely
                # expanding parts()'s bbox still enumerates only incoming and misses the fork on the east.
                west_chunks = roads._bridge_chunks(frame, {}, flat, (-25, -12, -10, 12), profiles, [incoming])
                east_chunks = roads._bridge_chunks(frame, {}, flat, (0, -12, 50, 12), profiles, [incoming, outgoing])
                west_field = roadmesh._junction_surfaces(west_chunks)
                east_field = roadmesh._junction_surfaces(east_chunks)
                self.assertEqual(set(west_field), {'a', 'b', 'c'})
                self.assertEqual(set(east_field), {'a', 'b', 'c'})
                np.testing.assert_allclose(west_field['a'].at([[-15, 0], [16, 0]]),
                                           east_field['a'].at([[-15, 0], [16, 0]]), atol=1e-10)
                self.assertIn(east, requests)

                # An unsupported arm discovered only in the expanded cell excludes the entire fork
                # on both tiles, using the same context as the complete incident profiles.
                def context_of(source_cells):
                    present = any('b' in [str(fid) for fid in f.ids] for f in source_cells if f is not None)
                    return ({'b': (1.5, (1,))} if present else {}, None, None)
                west_skip = roads._bridge_chunks(frame, {}, flat, (-25, -12, -10, 12), profiles,
                                                 [incoming], context_of=context_of)
                east_skip = roads._bridge_chunks(frame, {}, flat, (0, -12, 50, 12), profiles,
                                                 [incoming, outgoing], context_of=context_of)
                self.assertFalse(roadmesh._junction_surfaces(west_skip))
                self.assertFalse(roadmesh._junction_surfaces(east_skip))

    def test_missing_source_context_is_a_fault_not_a_failed_tile(self):
        """A neighbour cell not at hand: the tile is built (no hole in the roads on a slow first visit) and counted a
        fault, so it is not cached as complete (revisit) and is built again with its fork next time."""
        import revisit
        from types import SimpleNamespace
        c = fork()[0]
        part = ('a', roads.Spec(8, 'paved', True, 'primary', False), np.array(c['n'])[:, :2],
                np.array(c['n'])[:, 2], 0)
        profile = SimpleNamespace(parts=lambda cells, box: [part], cell_fn=lambda key: None, _ground=None)
        before = revisit.faults()
        roads._bridge_chunks(FRAME, {}, flat, (-40, -10, -30, 10), profile, [])
        self.assertGreater(revisit.faults(), before)


if __name__ == '__main__':
    unittest.main()
