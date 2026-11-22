"""Hard edges, usable UVs, source-stable wall joints and boundary material contracts; no live data."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import barriers
import cdae
import roadmesh


def wall(xy=((10, 30), (90, 30)), gaps=()):
    xy = np.asarray(xy, float)
    return barriers.Run('wall', 'wall', xy, barriers._arc(xy), 1.5, .3, 'wall', barriers.WALL_NEUTRAL, gaps)


def flat(xy):
    return np.zeros(len(xy))


def mesh(run, box=(0, 0, 256, 256), **kwargs):
    return barriers.tile_meshes([run], box, flat, **kwargs)


def geometry(parts):
    for (p, t, uv, *_), _bounds in parts.values():
        yield p, t, uv


class BoundaryRenderingTest(unittest.TestCase):
    def test_prism_template_matches_existing_extrusion(self):
        a, b = np.array([13., -4.]), np.array([15., -1.])
        delta = b - a
        normal = np.array([-delta[1], delta[0]]) / np.linalg.norm(delta)
        for width, height in [(.025, .025), (.3, 1.5), (1.2, .8)]:
            m = barriers._Mesh((0, 0, 0))
            m.prism(a, b, width, 2., 3., height, 'test', 41.)
            p, t, uv, *_ = m.result()
            expected = roadmesh.barrier_solid(np.array([a, b]) - normal * width / 2,
                np.tile(normal, (2, 1)), np.array([2., 3.]), np.full(2, height),
                v=np.array([41., 41. + np.linalg.norm(delta)]),
                section=((0, 0), (width, 0), (width, 1), (0, 1)))
            for actual, want in zip((p, t, uv), expected):
                np.testing.assert_allclose(actual, want, atol=1e-12)

    def assertSurface(self, p, t, uv):
        cross = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        area = np.linalg.norm(cross, axis=1)
        self.assertTrue((area > 1e-12).all(), 'no zero-area geometry')
        du, dv = uv[t[:, 1]] - uv[t[:, 0]], uv[t[:, 2]] - uv[t[:, 0]]
        self.assertTrue((np.abs(du[:, 0] * dv[:, 1] - du[:, 1] * dv[:, 0]) > 1e-12).all(),
                        'every face has non-degenerate texture coordinates')
        return cross / area[:, None]

    def test_prisms_have_hard_normals_and_planar_end_uvs(self):
        for a, b, width, z0, z1, height in [([0, 0], [2, 0], .3, 0, 0, 1.5),
                ([15, -8], [18, -4], .3, 4, 5, 1.5), ([1, 1], [1.12, 1], .12, -.1, -.1, 1.4),
                ([0, 0], [2, 1], .025, 1.1, 1.1, .025)]:
            with self.subTest(width=width):
                m = barriers._Mesh((0, 0, 0))
                m.prism(np.array(a, float), np.array(b, float), width, z0, z1, height, 'test')
                p, t, uv, *_ = m.result()
                fn = self.assertSurface(p, t, uv)
                normals = cdae.face_normal_sum(p, t)
                np.testing.assert_allclose(normals[t], np.broadcast_to(fn[:, None], normals[t].shape), atol=1e-7)
                volume = np.einsum('ij,ij->i', p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]])).sum() / 6
                self.assertAlmostEqual(volume, np.linalg.norm(np.array(b) - a) * width * height)

    def test_continuous_wall_has_no_sampling_caps(self):
        r = wall()
        parts = mesh(r)
        p, t, uv = next(geometry(parts))
        self.assertSurface(p, t, uv)
        self.assertLess(len(t), 12 * 40)
        # Only the two true ends can have triangles entirely in a constant-x plane.
        end = np.ptp(p[t, 0], axis=1) < 1e-8
        self.assertEqual(int(end.sum()), 4)
        np.testing.assert_allclose(p.min(0), [10, 29.85, -.1], atol=1e-7)
        np.testing.assert_allclose(p.max(0), [90, 30.15, 1.5], atol=1e-7)

    def test_side_uv_phase_comes_from_original_source_station(self):
        r = wall(((10, 30), (500, 30)))
        for box in [(0, 0, 256, 256), (256, 0, 512, 256)]:
            for p, t, uv in geometry(mesh(r, box)):
                world = p + [box[0], box[1], 0]
                side = np.flatnonzero(np.abs(world[:, 1] - 29.85) < 1e-7)
                # Cap vertices use planar end UVs; the side strip has its longitudinal UV at source distance.
                expected = (world[side, 0] - 10) * roadmesh.UV_M
                self.assertGreater(np.isclose(uv[side, 0], expected, atol=1e-5).sum(), len(side) - 5)

    def test_bend_on_tile_edge_has_identical_shared_mitre(self):
        r = wall(((10, 50), (256, 50), (400, 190)))
        seam = []
        for box in [(0, 0, 256, 256), (256, 0, 512, 256)]:
            p = np.concatenate([m[0][0] for m in mesh(r, box).values()]) + [box[0], box[1], 0]
            normals = roadmesh._normals(r.xy)[1] * r.width / 2
            corners = np.array([r.xy[1] - normals, r.xy[1] + normals])
            points = p[np.min(np.linalg.norm(p[:, None, :2] - corners[None], axis=2), axis=1) < 1e-6]
            seam.append(np.unique(np.round(points, 6), axis=0))
        self.assertEqual(len(seam[0]), 4)
        np.testing.assert_array_equal(*seam)

    def test_bends_and_closed_walls_keep_hard_faces_without_corner_boxes(self):
        for xy in [((10, 20), (40, 20), (40, 55)),
                   ((10, 20), (40, 20), (40, 55), (10, 55), (10, 20)),
                   ((10, 20), (40, 20), (10, 20))]:
            with self.subTest(xy=xy):
                for p, t, uv in geometry(mesh(wall(xy))):
                    fn = self.assertSurface(p, t, uv)
                    n = cdae.face_normal_sum(p, t)
                    np.testing.assert_allclose(n[t], np.broadcast_to(fn[:, None], n[t].shape), atol=1e-7)

    def test_gate_and_road_cut_are_not_bridged_by_a_continuous_strip(self):
        r = wall(gaps=((30, 34, 'gate'),))
        for p, t, _uv in geometry(mesh(r)):
            self.assertFalse(((p[t, 0].min(1) < 40.01) & (p[t, 0].max(1) > 43.99)).any())
        road = [(np.array([[50, 0], [50, 80]]), 3.)]
        for p, t, _uv in geometry(mesh(wall(), road_lines=road)):
            self.assertFalse(((p[t, 0].min(1) < 46) & (p[t, 0].max(1) > 54)).any())

    def test_long_wall_splits_within_existing_mesh_limits(self):
        with patch.object(barriers, 'MAX_VERTS', 120):
            parts = mesh(wall())
        self.assertGreater(len(parts), 1)
        self.assertLessEqual(len(parts), barriers.PARTS)
        for p, t, uv in geometry(parts):
            self.assertLessEqual(len(p), 120)
            self.assertLess(int(t.max()), len(p))
            self.assertSurface(p, t, uv)

    def test_oversized_primitive_is_rejected_before_serialization(self):
        m = barriers._Mesh((0, 0, 0))
        with patch.object(barriers, 'MAX_VERTS', 12), self.assertRaises(ValueError):
            m.prism(np.array([0., 0.]), np.array([2., 0.]), .3, 0, 0, 1.5, 'test')

    def test_material_priority_and_unknown_fallback(self):
        for tags, expected in [({}, barriers.WALL_NEUTRAL), ({'material': 'unsupported'}, barriers.WALL_NEUTRAL),
                ({'material': ' concrete ', 'wall': 'dry_stone'}, 'bne_bridge_concrete'),
                ({'wall': 'dry_stone'}, 'bne_wall_stone'), ({'wall': 'brick'}, 'bne_wall_brick'),
                ({'material': 'stone'}, 'bne_wall_stone')]:
            self.assertEqual(barriers._style('wall', tags), ('wall', expected))

    def test_boundary_materials_do_not_depend_on_missing_vertex_colours(self):
        root = Path(barriers.__file__).resolve().parents[1]
        path = root / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/barriers.materials.json'
        mats = json.loads(path.read_text(encoding='utf-8'))
        for name in [barriers.WALL_NEUTRAL, 'bne_wall_brick', 'bne_wall_stone', barriers.WOOD]:
            self.assertFalse(mats[name].get('vertColor'))
            self.assertTrue(all(not stage.get('vertColor') for stage in mats[name]['Stages']))
            self.assertTrue(mats[name]['Stages'][0]['baseColorMap'].startswith('/assets/'))
        self.assertEqual(mats[barriers.WALL_NEUTRAL]['groundType'], 'CONCRETE')

    def test_existing_constant_barrier_profile_matches_varying_form(self):
        edge = np.array([[0., 0.], [10., 0.]])
        normal = np.tile([0., 1.], (2, 1))
        section = ((0., 0.), (.3, 0.), (.3, 1.), (0., 1.))
        a = roadmesh.barrier_solid(edge, normal, np.zeros(2), np.full(2, 1.5), section=section)
        b = roadmesh.barrier_solid(edge, normal, np.zeros(2), np.full(2, 1.5), section=np.tile(section, (2, 1, 1)))
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x, y)


if __name__ == '__main__':
    unittest.main()
