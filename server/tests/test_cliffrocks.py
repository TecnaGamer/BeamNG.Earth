"""Cliff eligibility, uninterrupted coverage, deterministic tint and tile transport."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffrocks as cr
import cdae
import geodesy
import roads


class CliffRocksTest(unittest.TestCase):
    def setUp(self):
        self.x = np.arange(21) * 8.0
        self.h = np.tile(self.x * 2, (21, 1))
        self.c = np.full(self.h.shape, 60, np.uint8)

    def make(self, box=(0, 0, 160, 160), biome=0):
        keep = cr.qualifying_cells(self.h, self.c, step=8)
        return cr.mesh(self.h, keep, self.x, self.x, np.full(keep.shape, biome),
                       box, (0, 0, 0))


    def test_large_divided_mountain_cut_export_has_no_cross_road_sheets(self):
        # El Paso: dense opposing walls exceed the renderer's 16-bit vertex
        # range even though the file stores uint32 indices. Exercise clipping,
        # both projection charts, both materials and pavement subdivision.
        import cliffcuts
        x = np.arange(-82, 82.5, .5)
        xx, yy = np.meshgrid(x, x)
        center = 20*np.sin(yy/12)
        distance = np.abs(xx-center)
        h = np.maximum(distance-15, 0)*2.5
        keep = (distance[:-1, :-1] > 15) & (distance[:-1, :-1] < 76)
        cuts = keep & (yy[:-1, :-1] < 15)
        road_y = np.arange(-100, 101, 2.)
        segs = roads._Segs({str(i): (np.c_[20*np.sin(road_y/12)+offset, road_y], 5.)
                           for i, offset in enumerate((-6, 6))})
        clearance = cliffcuts.RoadClearance(segs, lambda xy: np.zeros(len(xy)))
        for rotated in (False, True):
            with self.subTest(rotated=rotated), tempfile.TemporaryDirectory() as tmp:
                project = (lambda p: p[:, [1, 0, 2]]) if rotated else None
                # Rotate inputs together so clearance remains in projected space.
                rs = roads._Segs({str(i): (np.c_[road_y, 20*np.sin(road_y/12)+offset], 5.)
                                 for i, offset in enumerate((-6, 6))}) if rotated else segs
                rc = cliffcuts.RoadClearance(rs, lambda xy: np.zeros(len(xy))) if rotated else clearance
                rock = cr.mesh(h, keep, x, x, np.zeros(keep.shape, int),
                               (-79.7, -79.7, 79.7, 79.7), (0, 0, 0),
                               cuts=cuts, road_segments=rs, road_clearance=rc, project=project)
                self.assertGreater(len(rock.positions), 65535)
                self.assertEqual(set(rock.tri_materials), {0, 1})
                path = Path(tmp)/'mountain.cdae'
                cr.write(str(path), rock)
                shape = cdae.read(path)
                count = 0
                for m in shape['meshes']:
                    a = cdae.mesh_arrays(m)
                    self.assertLessEqual(len(a['verts']), 65535)
                    ids = a['indices'].reshape(-1, 3)
                    self.assertLess(int(ids.max()), len(a['verts']))
                    tri = a['verts'][ids]
                    self.assertTrue(np.isfinite(tri).all())
                    self.assertLess(np.linalg.norm(tri-np.roll(tri, 1, axis=1), axis=2).max(), 8)
                    count += len(ids)
                self.assertEqual(count, len(rock.tris), 'splitting must preserve every triangle')

    def test_arid_family_preserves_austin_and_mixed_material_exports(self):
        import cliffmaterial as cm
        import climate
        # Actual bundled climate lookup; selecting by environment, not place.
        for lat, lon, expected in ((31.87810, -106.49561, cm.ARID_CUT),
                                   (30.29941, -97.82788, cm.LIMESTONE_CUT)):
            biome = cr.biome_at(climate.koppen_at(lat, lon), np.array([30]))
            self.assertEqual(int(cm.cut_family(biome)[0]), expected)
        keep = np.ones((20, 20), bool)
        cuts = keep.copy(); cuts[:5] = False
        biomes = np.zeros(keep.shape, int); biomes[10:] = 1
        rock = cr.mesh(self.h, keep, self.x, self.x, biomes, (0, 0, 160, 160), (0, 0, 0), cuts=cuts)
        self.assertEqual(set(rock.tri_materials), {cm.NATURAL, cm.LIMESTONE_CUT, cm.ARID_CUT})
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'mixed.cdae'); cr.write(path, rock)
            self.assertEqual([m['name'] for m in cdae.read(path)['materials']], list(cm.MATERIALS))
        # Neighboring tile generation and order must not alter material phase.
        def tile(box):
            return cr.mesh(self.h, keep, self.x, self.x, biomes, box, (0, 0, 0), cuts=cuts)
        left = tile((0, 0, 80, 160)); right = tile((80, 0, 160, 160)); again = tile((0, 0, 80, 160))
        np.testing.assert_array_equal(left.positions, again.positions)
        np.testing.assert_array_equal(left.uvs, again.uvs)
        np.testing.assert_array_equal(left.tri_materials, again.tri_materials)
        self.assertGreater(len(right.tris), 0)

    def test_arid_height_and_pbr_assets_share_scale_and_periodic_chart(self):
        import cliffmaterial as cm
        from PIL import Image
        u, v = np.meshgrid(np.arange(cm.HEIGHT_SIZE[0]), np.arange(cm.HEIGHT_SIZE[1]))
        u = (u+.5)/cm.HEIGHT_SIZE[0]; v = (v+.5)/cm.HEIGHT_SIZE[1]
        with Image.open(cm.ARID_CUT_HEIGHT_PATH) as im:
            self.assertEqual(im.size, cm.HEIGHT_SIZE)
            expected = np.flipud(np.asarray(im, float))/65535*cm.ROAD_CUT_RELIEF_M
        actual = cm.sample(u, v, family=cm.ARID_CUT)
        np.testing.assert_allclose(actual, expected, atol=1e-7)
        np.testing.assert_allclose(actual, cm.sample(u+3, v-2, family=cm.ARID_CUT), atol=1e-7)
        self.assertGreater(float(np.ptp(actual)), .01)
        self.assertFalse(np.allclose(actual, cm.sample(u, v, roadcut=True)))
        root = Path(__file__).resolve().parents[2]
        mats = json.loads((root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        stage = mats[cm.ARID_CUT_MATERIAL]['Stages'][0]
        self.assertTrue(stage['vertColor'])
        for key in ('baseColorMap', 'normalMap', 'roughnessMap', 'ambientOcclusionMap'):
            self.assertNotEqual(stage[key], mats[cm.ROAD_CUT_MATERIAL]['Stages'][0][key])
            with Image.open(root/'mod/beamng_earth'/stage[key].lstrip('/')) as im:
                self.assertEqual(im.size, (4096, 4096))
        self.assertIn('cliff_arid_cut_height.png', (root/'tools/build_app.py').read_text())
        self.assertEqual((cm.WIDTH_M, cm.HEIGHT_M), (91.44, 91.44))

    def test_arid_cpu_height_change_invalidates_revisit_geometry(self):
        import revisit
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder/'cliffrocks.py').write_text('geometry')
            height = folder/'cliff_arid_cut_height.png'
            height.write_bytes(b'first height')
            with patch.object(revisit, '__file__', str(folder/'revisit.py')), patch.object(revisit, '_code', []):
                before = revisit.code_key()
                height.write_bytes(b'changed height')
                revisit._code.clear()
                self.assertNotEqual(before, revisit.code_key())

    def test_flat_and_single_tall_spike_do_not_qualify(self):
        for h in (np.zeros_like(self.h), self.h * 0.1):
            self.assertFalse(cr.qualifying_cells(h, self.c, step=8).any())
        h = np.zeros_like(self.h)
        h[10, 10] = 10000
        self.assertFalse(cr.qualifying_cells(h, self.c, step=8).any())

    def test_folded_coastal_wall_connects_through_steep_support(self):
        x, y = np.meshgrid(np.arange(25)*8.0, np.arange(49)*8.0)
        h = np.clip(x-64, 0, 16) * (1.45 + 0.45*np.cos(y/16))
        classes = np.full(h.shape, 60, np.uint8)
        # Strictly extreme faces leave gaps between folds on San Mateo's coast.
        strict = cr.qualifying_cells(h, classes, step=8, support_slope=58)
        keep = cr.qualifying_cells(h, classes, step=8)
        self.assertLess(strict.sum(), keep.sum())
        self.assertGreater(keep.sum()*64, cr.MIN_AREA_M2)
        self.assertTrue(keep[:, 8:10].all(), 'gentler folds do not leave random gaps')

    def test_moderate_hillside_with_single_spike_still_rejected(self):
        h = self.h * 0.6  # approximately 50 degrees: support, without an extreme core
        self.assertFalse(cr.qualifying_cells(h, self.c, step=8).any())
        h[10, 10] += 10000
        self.assertFalse(cr.qualifying_cells(h, self.c, step=8).any())

    def test_smoothed_scarp_qualifies_without_extreme_core(self):
        x, y = np.meshgrid(np.arange(41)*4.0, np.arange(41)*4.0)
        h = np.clip(x-48, 0, 20)*np.tan(np.radians(52))
        c = np.full(h.shape, 60)
        keep = cr.qualifying_cells(h, c)
        self.assertTrue(keep[:, 12:17].all())
        self.assertFalse(keep[:, :12].any())
        self.assertFalse(keep[:, 17:].any(), 'flat crest stays terrain')
        # Physical metres, not square-grid assumptions; also test N/S orientation.
        h = np.clip(y-48, 0, 20)*np.tan(np.radians(52))
        keep = cr.qualifying_cells(h, c, step=(3.2, 4))
        self.assertTrue(keep[12:17, 1:-1].all())

    def test_scarp_fallback_rejects_hills_small_banks_and_missing_shoulders(self):
        x, y = np.meshgrid(np.arange(81)*4.0, np.arange(81)*4.0)
        c = np.full(x.shape, 60)
        cases = {
            'open steep hillside': x*np.tan(np.radians(52)),
            'broad rounded hill': 80*np.exp(-((x-160)**2+(y-160)**2)/(2*40**2)),
            'gentler bounded slope': np.clip(x-100, 0, 24)*np.tan(np.radians(46)),
            'low bank': np.clip(x-100, 0, 4)*np.tan(np.radians(52)),
            'crest outside data': np.clip(x-304, 0, 20)*np.tan(np.radians(52)),
        }
        for name, h in cases.items():
            with self.subTest(name=name):
                self.assertFalse(cr.qualifying_cells(h, c).any())

    def test_scarp_fallback_preserves_exclusions_and_area_floor(self):
        x, y = np.meshgrid(np.arange(41)*4.0, np.arange(41)*4.0)
        h = np.clip(x-48, 0, 20)*np.tan(np.radians(52))
        c = np.full(h.shape, 60)
        blocked = np.ones((40, 40), bool)
        blocked[:5] = False
        self.assertFalse(cr.qualifying_cells(h, c, blocked).any(), 'small isolated scarp rejected')
        blocked[:] = False
        blocked[10:20] = True
        self.assertFalse(cr.qualifying_cells(h, c, blocked)[10:20].any(), 'road exclusions retained')
        c[:, 10:20] = 50
        self.assertFalse(cr.qualifying_cells(h, c).any(), 'city buffering still precedes detection')
        for cls in (0, 40, 80):
            c[:] = cls
            self.assertFalse(cr.qualifying_cells(h, c).any())

    def test_scarp_does_not_spread_onto_connected_open_hillside(self):
        x, y = np.meshgrid(np.arange(81)*4.0, np.arange(81)*4.0)
        # A terrace on a much broader 46-degree slope. Only the bounded face
        # qualifies; connected support alone must not coat the whole hillside.
        h = (np.clip(x-40, 0, 20)*np.tan(np.radians(52))
             + np.maximum(x-72, 0)*np.tan(np.radians(46)))
        c = np.full(h.shape, 60)
        keep = cr.qualifying_cells(h, c)
        self.assertTrue(keep[:, 10:15].any())
        self.assertFalse(keep[:, 19:].any())

    def test_face_growth_has_physical_distance_and_exclusion_limits(self):
        keep = np.zeros((21, 21), bool)
        keep[10, 10] = True
        support = np.ones_like(keep)
        expanded = cr._extend_faces(keep, support, 3, 4)
        self.assertTrue(expanded[10, 14] and expanded[13, 10])
        self.assertFalse(expanded[10, 15] or expanded[14, 10])
        self.assertTrue(expanded[11, 12])
        self.assertFalse(expanded[12, 12], 'distance is accumulated through the face')
        support[:, 11] = False
        expanded = cr._extend_faces(keep, support, 3, 4)
        self.assertFalse(expanded[:, 11:].any(), 'cannot cross a road or flat band')
        self.assertFalse(cr._extend_faces(np.zeros_like(keep), support, 3, 4).any(),
                         'support slopes never create independent cliffs')

    def test_folded_moderate_scarp_fills_gentler_ribs_but_not_plateau(self):
        x, y = np.meshgrid(np.arange(41)*4.0, np.arange(81)*4.0)
        h = np.clip(x-48, 0, 20)*(1.08+.18*np.cos(y/8))
        c = np.full(h.shape, 60)
        keep = cr.qualifying_cells(h, c)
        self.assertTrue(keep[4:-4, 12:17].all(), 'erosional ribs belong to the same confirmed face')
        self.assertFalse(keep[:, :12].any())
        self.assertFalse(keep[:, 17:].any(), 'extension stops at the flat crest')

    def test_profile_growth_does_not_pool_through_blocked_corridors(self):
        x, y = np.meshgrid(np.arange(41)*4.0, np.arange(41)*4.0)
        h = np.clip(x-48, 0, 20)*np.tan(np.radians(52))
        c = np.full(h.shape, 60)
        blocked = np.ones((40, 40), bool)
        blocked[2:5] = False
        blocked[6:9] = False
        self.assertFalse(cr.qualifying_cells(h, c, blocked).any(),
                         'small fragments separated by an exclusion cannot qualify together')

    def test_confirmed_face_includes_short_rounded_shoulder(self):
        x, y = np.meshgrid(np.arange(41)*4.0, np.arange(41)*4.0)
        h = (np.clip(x-48, 0, 20)*np.tan(np.radians(52))
             + np.clip(x-68, 0, 12)*np.tan(np.radians(37)))
        c = np.full(h.shape, 60)
        keep = cr.qualifying_cells(h, c)
        self.assertTrue(keep[2:-2, 12:20].all())
        self.assertFalse(keep[:, 20:].any(), 'flat ground beyond shoulder is not rock')
        shoulder_only = np.clip(x-48, 0, 32)*np.tan(np.radians(37))
        self.assertFalse(cr.qualifying_cells(shoulder_only, c).any(),
                         'the same shoulder slope alone cannot create a cliff')

    def test_minimum_footprint_and_four_connected_cells(self):
        blocked = np.ones((20, 20), bool)
        blocked[2:6, 2:6] = False   # exactly 16 * 64 m2
        keep = cr.qualifying_cells(self.h, self.c, blocked, step=8, min_footprint=1024)
        self.assertEqual(keep.sum(), 16)
        blocked[2, 2] = True
        self.assertFalse(cr.qualifying_cells(self.h, self.c, blocked, step=8, min_footprint=1024).any())
        blocked[:] = True
        blocked[1:4, 1:4] = False
        blocked[4:7, 4:7] = False  # diagonal touching cannot pool their areas
        self.assertFalse(cr.qualifying_cells(self.h, self.c, blocked, step=8, min_footprint=1024).any())

    def test_city_buffer_and_missing_data_fail_closed(self):
        self.c[10, 10] = 50
        keep = cr.qualifying_cells(self.h, self.c, step=8)
        self.assertFalse(keep[1:19, 1:19].any())
        for cls in (0, 40, 50, 70, 80, 90, 95):
            self.c[:] = cls
            self.assertFalse(cr.qualifying_cells(self.h, self.c, step=8).any(), cls)

    def test_nonfinite_heights_are_not_meshed(self):
        self.h[:] = np.nan
        self.assertFalse(cr.qualifying_cells(self.h, self.c, step=8).any())

    def test_every_cell_covered_with_upward_winding_and_no_interior_cracks(self):
        m = self.make()
        t = m.positions[m.tris].astype(float)
        cross = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        self.assertTrue((cross[:, 2] > 0).all())
        self.assertAlmostEqual(cross[:, 2].sum()/2, 160*160)
        # Weld by coordinate to inspect topology, independently of vertex indices.
        edges = {}
        for tri in t:
            for a, b in ((0, 1), (1, 2), (2, 0)):
                edge = tuple(sorted((tuple(tri[a]), tuple(tri[b]))))
                edges[edge] = edges.get(edge, 0) + 1
        for edge, count in edges.items():
            a, b = edge
            perimeter = any(a[k] == b[k] and a[k] in (0, 160) for k in (0, 1))
            self.assertEqual(count, 1 if perimeter else 2)
        self.assertTrue(np.isfinite(m.uvs).all())
        self.assertLess(len(m.positions), 65536)

    def test_adjacent_tiles_clip_the_same_surface(self):
        left, right = self.make((0, 0, 83, 160)), self.make((83, 0, 160, 160))
        def seam(m):
            used = m.positions[np.unique(m.tris)]
            return set(map(tuple, used[np.isclose(used[:, 0], 83)]))
        self.assertEqual(seam(left), seam(right))
        self.assertTrue(seam(left))

    def test_biome_changes_relief_and_tint_deterministically(self):
        a, b, repeat = self.make(biome=1), self.make(biome=3), self.make(biome=1)
        np.testing.assert_array_equal(a.positions, repeat.positions)
        np.testing.assert_array_equal(a.colors, repeat.colors)
        self.assertFalse(np.array_equal(a.positions, b.positions))
        self.assertFalse(np.array_equal(a.colors, b.colors))
        np.testing.assert_array_equal(cr.biome_at(4, self.c), np.ones_like(self.c))

    def test_photo_color_blend_is_bounded_and_desaturated(self):
        b = np.zeros((2, 2), int)
        green = np.broadcast_to((0, 255, 0), (2, 2, 3))
        out = cr.rock_colors(b, green).astype(int)
        self.assertLessEqual(np.abs(out[..., 1] - out[..., 0]).max(), 25)
        np.testing.assert_array_equal(out, cr.rock_colors(b))
        with self.assertRaises(ValueError):
            cr.rock_colors(b, np.full((2, 2, 3), np.nan))

    def test_missing_or_incomplete_surface_skips_generation(self):
        class Surface:
            def classes(self, lat, lon):
                return np.full(np.shape(lat), 60), False
        frame = geodesy.EnuFrame(46, 7)
        self.assertIsNone(cr.build(frame, (0, 0, 256, 256), None, None))
        self.assertIsNone(cr.build(frame, (0, 0, 256, 256),
                                  lambda la, lo: np.zeros_like(la), Surface()))

    def test_streamed_file_contains_tint_and_registered_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            roads.write_tile(tmp, '/test', 1, 0, 0, [], cliff_mesh=self.make())
            data = json.loads(Path(tmp, 'roads_1_0_0.json').read_text())
            self.assertEqual(data['cliff']['pos'], [0, 0, 0])
            name = roads.shape_files(1, 0, 0)['cliff']
            self.assertEqual(data['cliff']['shape'], '/test/' + name)
            decoded = cdae.read(str(Path(tmp, name)))
            self.assertIn(cr.MATERIAL.encode(), Path(tmp, name).read_bytes())
            count, size, raw = decoded['meshes'][0]['colors']
            self.assertEqual(size, 4)
            self.assertEqual(count, len(self.make().positions))
            self.assertEqual(raw, self.make().colors.tobytes())

    def test_enabled_build_keeps_full_road_corridor_clear(self):
        class Surface:
            def classes(self, lat, lon):
                return np.full(np.shape(lat), 60), True
            def urban(self, lat, lon):
                return np.zeros(np.shape(lat), bool)
        frame = geodesy.EnuFrame(46, 7)
        def elev(lat, lon):
            return 100 + (np.asarray(lon)-7) * 111320 * np.cos(np.radians(46)) * 2
        line = [('road', SimpleNamespace(width=8, urban=False, cls='trunk'), np.array(((80, -200, 0), (80, 400, 0))), 0)]
        with patch('surface._roads_list_enu', return_value=line):
            m = cr.build(frame, (0, 0, 160, 160), elev, Surface())
        self.assertIsNotNone(m)
        self.assertTrue(np.isfinite(m.positions).all())
        # 4 m half-width + 6 m clearance; no triangle spans the road.
        x = m.positions[m.tris, 0]
        self.assertTrue(((x.max(axis=1) <= 70) | (x.min(axis=1) >= 90)).all())

    def test_roadcut_build_requires_source_relief_for_urban_highways(self):
        frame = geodesy.EnuFrame(0, 0)
        def elev(lat, lon):
            x = np.asarray(lon)*cr.WORLD_M_PER_DEG
            return 20+np.clip(x-10, 0, 24)*np.tan(np.radians(42))+np.maximum(x-34, 0)*.3
        class Surface:
            def classes(self, lat, lon):
                return np.where(np.abs(lon*cr.WORLD_M_PER_DEG) <= 7, 50, 30), True
            def urban(self, lat, lon):
                return np.zeros(np.shape(lat), bool)
        spec = SimpleNamespace(width=10, urban=False, cls='trunk')
        line = [('road', spec, np.array(((0, -500, 0), (0, 500, 0))), 0)]
        with patch('surface._roads_list_enu', return_value=line):
            m = cr.build(frame, (-40, -64, 80, 64), elev, Surface())
            self.assertIsNotNone(m)
            self.assertTrue((m.tri_materials == 1).all(), 'confirmed cutting has its own material')
            self.assertTrue((m.positions[:, 0]-40 >= 8).all(), 'displaced skin clears carriageway and shoulder')
            spec.urban = True
            self.assertIsNone(cr.build(frame, (-40, -64, 80, 64), elev, Surface()))
            urban = cr.build(frame, (-40, -64, 80, 64), elev, Surface(), source_elev=elev)
            self.assertIsNotNone(urban, 'urban highway backed by original relief is a valid cut')
            self.assertTrue((urban.tri_materials == 1).all())
            self.assertTrue(np.isfinite(urban.positions).all())
            self.assertTrue((urban.positions[:, 0]-40 >= 8).all(), 'urban cut clears pavement too')
            self.assertIsNone(cr.build(frame, (-40, -64, 80, 64), elev, Surface(),
                source_elev=lambda lat, lon: np.full(np.shape(lat), 20.)),
                'the same graded wall on originally flat ground is artificial fill')

    def test_different_tile_frames_share_the_geographic_rock_lattice(self):
        class Surface:
            def classes(self, lat, lon):
                return np.full(np.shape(lat), 60), True
            def urban(self, lat, lon):
                return np.zeros(np.shape(lat), bool)
        def elev(lat, lon):
            return 300 + (np.asarray(lon)-7) * 111320 * np.cos(np.radians(46)) * 2
        frames = (geodesy.EnuFrame(46, 7), geodesy.EnuFrame(46.0002, 7.0005))
        samples = []
        for frame in frames:
            box = (-128, -128, 128, 128)
            m = cr.build(frame, box, elev, Surface())
            p = m.positions.astype(float) + (-128, -128, 0)
            lat, lon, h = frame.enu_to_geodetic(*p.T)
            samples.append({(round(a, 7), round(o, 7)): z for a, o, z in zip(lat, lon, h)})
        common = samples[0].keys() & samples[1].keys()
        self.assertGreater(len(common), 100)
        self.assertLess(max(abs(samples[0][k]-samples[1][k]) for k in common), 0.02)

    def test_urban_oblique_rounded_cut_fits_beyond_coarse_source_mask(self):
        # North Capital: rounded foot/crest straddle 4 m evidence cells. Keep
        # the real selection, refinement and mesh path in this regression.
        frame = geodesy.EnuFrame(0, 0)
        def elev(lat, lon):
            q = np.asarray(lon)*cr.WORLD_M_PER_DEG-.35*np.asarray(lat)*cr.WORLD_M_PER_DEG
            return 20+24/(1+np.exp(-np.clip((q-20)/2.5, -100, 100)))
        class Surface:
            def classes(self, lat, lon):
                q = lon*cr.WORLD_M_PER_DEG-.35*lat*cr.WORLD_M_PER_DEG
                return np.where(q >= 65, 50, np.where(np.abs(q) <= 7, 50, 30)), True
            def urban(self, lat, lon):
                return np.ones(np.shape(lat), bool)
        road = [('road', SimpleNamespace(width=10, urban=True, cls='trunk'),
                 np.array(((-175, -500, 0), (175, 500, 0))), 0)]
        with patch('surface._roads_list_enu', return_value=road):
            m = cr.build(frame, (-40, -48, 80, 48), elev, Surface(), source_elev=elev)
        self.assertIsNotNone(m)
        # World coordinates recovered from the exported tile origin.
        x, y = m.positions[:, 0]-40, m.positions[:, 1]-48
        q = x-.35*y
        self.assertGreater(q.min(), 8, 'road and shoulder remain clear')
        self.assertLess(q.max(), 40, 'gentle plateau/buildings remain clear')
        self.assertTrue((m.tri_materials == 1).all())
        # Probe the actual exported triangles along both rounded edges. Bounds
        # alone miss the alternating exposed wedges of the coarse source mask.
        triangles = m.positions[m.tris, :2]-[40, 48]
        a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
        v, w = b-a, c-a
        def cross(u, v):
            return u[..., 0]*v[..., 1]-u[..., 1]*v[..., 0]
        determinant = cross(v, w)
        valid = np.abs(determinant) > 1e-8
        divisor = np.where(valid, determinant, np.inf)
        for q in (13., 26.):
            for y in np.arange(-30., 31.):
                p = np.array([q+.35*y, y])-a
                u, z = cross(p, w)/divisor, cross(v, p)/divisor
                covered = valid & (u >= -1e-5) & (z >= -1e-5) & (u+z <= 1.00001)
                self.assertTrue(covered.any(), f'exposed rounded edge at q={q}, y={y}')

    def test_narrow_coastal_face_uses_face_area_with_a_footprint_floor(self):
        x = np.arange(41)*4.0
        h = np.broadcast_to(np.clip(x-64, 0, 8)*3, (25, 41))
        c = np.full(h.shape, 60)
        keep = cr.qualifying_cells(h, c)
        self.assertTrue(keep[:, 16:18].all())
        self.assertLess(keep.sum()*16, cr.MIN_AREA_M2)
        # A very tall but tiny patch cannot qualify merely through wall area.
        blocked = np.ones(keep.shape, bool)
        blocked[:6, 16:18] = False
        self.assertFalse(cr.qualifying_cells(h*100, c, blocked).any())

    def test_refinement_resamples_curved_terrain_and_preserves_exclusions(self):
        xs = np.arange(7)*4.0
        keep = np.ones((6, 6), bool)
        keep[:, 2] = False
        def elev(lat, lon):
            x = lon*cr.WORLD_M_PER_DEG
            return 20 + 0.2*x*x
        rx, ry, h, refined, _, _ = cr._refine(xs, xs, keep, np.zeros_like(keep, int),
                                              (4, 4, 20, 20), elev, None)
        self.assertLessEqual(np.diff(rx).max(), 1)
        np.testing.assert_allclose(h, np.broadcast_to(20+0.2*rx*rx, h.shape), atol=1e-6)
        centers = (rx[:-1]+rx[1:])/2
        self.assertFalse(refined[:, (centers >= 8) & (centers < 12)].any())

    def test_rock_surface_clearance_normals_and_large_coordinate_uvs(self):
        x, y = np.arange(33, dtype=float), np.arange(33, dtype=float)
        h = np.broadcast_to(x*2, (33, 33))
        m = cr.mesh(h, np.ones((32, 32), bool), x+13000000, y+4000000,
                    np.zeros((32, 32), int), (13000000, 4000000, 13000032, 4000032),
                    (13000000, 4000000, 0))
        p = m.positions
        inner = (p[:, 0] > 1) & (p[:, 0] < 31) & (p[:, 1] > 1) & (p[:, 1] < 31)
        residual = p[inner, 2] - p[inner, 0]*2
        self.assertGreater(residual.min(), cr.CLEARANCE_M)
        self.assertGreater(residual.std(), 0.05)
        np.testing.assert_allclose(np.linalg.norm(m.normals, axis=1), 1, atol=1e-6)
        self.assertLess(np.abs(m.uvs).max(), 100)
        uv_tris = m.uvs[m.tris]
        du, dv = uv_tris[:, 1]-uv_tris[:, 0], uv_tris[:, 2]-uv_tris[:, 0]
        self.assertTrue((np.abs(du[:, 0]*dv[:, 1]-du[:, 1]*dv[:, 0]) > 1e-7).all())

    def test_cut_chart_buries_staircase_rim_and_covers_fitted_toes(self):
        x, y = np.meshgrid(np.arange(49)*.5, np.arange(49)*.5)
        h = 25/(1+np.exp(-(x-.35*y-7)*3))
        envelope = cr._terrain_envelope(h, .5, .5)
        keep = (x[:-1, :-1]-.35*y[:-1, :-1] > 4) & (x[:-1, :-1]-.35*y[:-1, :-1] < 10)
        chart = cr._cut_chart(h, envelope, keep, .5, .5)
        touches = np.zeros(h.shape, int)
        for oy, ox in ((0,0),(1,0),(0,1),(1,1)):
            touches[oy:oy+48, ox:ox+48] += keep
        np.testing.assert_allclose(chart[touches != 4], h[touches != 4])
        self.assertTrue((chart >= h-1e-10).all())
        self.assertGreater((chart-h).max(), 1)
        surface, _, _ = cr.displaced_surface(chart, keep, np.arange(49)*.5,
            np.arange(49)*.5, np.zeros_like(keep, int), cuts=keep)
        active = touches > 0
        np.testing.assert_allclose(surface[...,0][active], x[active])
        np.testing.assert_allclose(surface[...,1][active], y[active])
        self.assertLess((surface[...,2]-chart)[active].max(), 1.1)
        rim = active & (touches != 4)
        np.testing.assert_allclose(surface[...,2][rim], h[rim]-cr.EDGE_SINK_M)

    def test_cut_toe_preserves_small_enclosed_flat_sample_but_not_exclusion(self):
        h = np.broadcast_to(np.arange(33)*1., (33,33)).copy()
        h[15:17,15:17] = 15.5
        seed = np.ones((32,32), bool)
        allowed = seed.copy()
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5)
        self.assertTrue(fitted[15,15])
        allowed[15,15] = False
        self.assertFalse(cr._fit_cut_toes(h, seed, allowed, .5, .5)[15,15])

    def test_cut_toe_follows_oblique_foot_instead_of_detection_stairs(self):
        x, y = np.arange(81)*.5, np.arange(65)*.5
        xx, yy = np.meshgrid(x, y)
        h = 2*np.clip(xx-.35*yy-8, 0, 12)
        cx, cy = np.meshgrid(x[:-1]+.25, y[:-1]+.25)
        coarse_q = (np.floor(cx/4)*4+2)-.35*(np.floor(cy/4)*4+2)-8
        seed = (coarse_q >= 1) & (coarse_q < 10)
        fitted = cr._fit_cut_toes(h, seed, np.ones_like(seed), .5, .5)
        rows = np.arange(8, 56)
        before = cx[rows, seed[rows].argmax(axis=1)]-.35*cy[rows, 0]-8
        after = cx[rows, fitted[rows].argmax(axis=1)]-.35*cy[rows, 0]-8
        self.assertGreater(np.ptp(before), 3)
        self.assertLess(np.ptp(after), .6)
        self.assertLess(np.abs(after).max(), .6)
        self.assertFalse(fitted[cx-.35*cy-8 < -.5].any(), 'flat bench stays clear')

    def test_cut_toe_stops_at_exclusions_missing_data_and_flat_benches(self):
        x = np.arange(81)*.5
        h = np.broadcast_to(2*(np.clip(x-4, 0, 4)+np.clip(x-12, 0, 10)), (33, len(x))).copy()
        seed = np.zeros((32, len(x)-1), bool)
        seed[:, 28:40] = True
        allowed = np.ones_like(seed)
        allowed[:, 26] = False
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5)
        self.assertFalse(fitted[:, :27].any(), 'cannot cross a protected strip')
        self.assertFalse(fitted[:, 40:].any(), 'no uphill growth')
        allowed[:] = True
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5)
        self.assertTrue(fitted[:, 24:28].any(), 'reach the upper lift foot')
        self.assertFalse(fitted[:, :23].any(), 'do not descend across the bench to another lift')
        h[:, 26] = np.nan
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5)
        self.assertFalse(fitted[:, :27].any(), 'missing fine elevation is not filled as zero')
        self.assertFalse(cr._fit_cut_toes(np.nan_to_num(h), seed & False, allowed, .5, .5).any())

    def test_cut_toe_reach_is_physical_and_shared_between_tile_aprons(self):
        x, y = np.arange(81)*.4, np.arange(65)*.5
        h = np.broadcast_to(x*2, (len(y), len(x)))
        seed = np.zeros((len(y)-1, len(x)-1), bool)
        seed[:, 40:60] = True
        allowed = np.ones_like(seed)
        full = cr._fit_cut_toes(h, seed, allowed, .4, .5)
        self.assertEqual(np.flatnonzero(full[20])[0], 40-int(cr.TOE_REACH_M/.4))
        part = cr._fit_cut_toes(h[8:57, 16:73], seed[8:56, 16:72], allowed[8:56, 16:72], .4, .5)
        np.testing.assert_array_equal(part[8:-8, 12:-12], full[16:48, 28:60])

    def test_crest_fitting_covers_steep_edge_but_stops_at_bench_and_exclusions(self):
        x = np.arange(97)*.5
        # Two lifts separated by a flat terrace, then a gentle 17-degree hill.
        h = np.broadcast_to(2*np.clip(x-4, 0, 12)+2*np.clip(x-20, 0, 8)
                            +.3*np.maximum(x-28, 0), (33, len(x))).copy()
        seed = np.zeros((32, len(x)-1), bool)
        seed[:, 12:24] = True
        allowed = np.ones_like(seed)
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5, fit_crest=True)
        self.assertTrue(fitted[:, 24:32].all(), 'recover crest beyond coarse seed')
        self.assertFalse(fitted[:, 32:].any(), 'cannot cross the bench into another lift')
        upper_seed = np.zeros_like(seed)
        upper_seed[:, 44:52] = True
        upper = cr._fit_cut_toes(h, upper_seed, allowed, .5, .5, fit_crest=True)
        self.assertTrue(upper[:, 52:56].all())
        self.assertFalse(upper[:, 56:].any(), 'cannot climb onto the gentle hill above the crest')
        allowed[:, 28] = False
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5, fit_crest=True)
        self.assertFalse(fitted[:, 28:].any(), 'protected crest strip is a barrier')
        allowed[:] = True
        h[:, 28] = np.nan
        self.assertFalse(cr._fit_cut_toes(h, seed, allowed, .5, .5, fit_crest=True)[:, 28:].any())
        h = np.broadcast_to(x*2, h.shape)
        fitted = cr._fit_cut_toes(h, seed, allowed, .5, .5, fit_crest=True)
        self.assertEqual(np.flatnonzero(fitted[16])[-1], 23+int(cr.TOE_REACH_M/.5),
                         'crest growth obeys the same physical distance cap as toes')

    def test_clipped_tile_seams_preserve_normals_and_texture_phase(self):
        left, right = self.make((0, 0, 83, 160)), self.make((83, 0, 160, 160))
        def seam(m):
            return {tuple(p): (n, uv) for p, n, uv in zip(m.positions, m.normals, m.uvs)
                    if np.isclose(p[0], 83)}
        a, b = seam(left), seam(right)
        self.assertEqual(a.keys(), b.keys())
        for p in a:
            np.testing.assert_allclose(a[p][0], b[p][0], atol=1e-6)
            np.testing.assert_allclose(a[p][1], b[p][1], atol=1e-6)

    def test_pbr_uvs_follow_the_same_undisplaced_chart_as_height(self):
        import cliffmaterial
        m = self.make()
        # This fixture's terrain is z=2*x. Displacement must not slide the PBR
        # maps to a different phase after sampling the corresponding height.
        # Recover the original position by projecting onto the planar terrain:
        # normal displacement is orthogonal to its tangent (1,0,2).
        original_x = (m.positions[:, 0]+2*m.positions[:, 2])/5
        inner = (original_x > 8) & (original_x < 152) & (m.positions[:, 1] > 8) & (m.positions[:, 1] < 152)
        phase = m.uvs[inner, 1] - (original_x[inner]*2)/cliffmaterial.HEIGHT_M
        np.testing.assert_allclose(phase, phase[0], atol=1e-6)

    def test_shading_changes_invalidate_streamed_cliff_content(self):
        import blockworker
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, 'cliff.cdae'))
            m = self.make()
            cr.write(path, m)
            before = blockworker._shape_info(path, (0, 0, 0), 'cliff')
            m.uvs *= 2
            cr.write(path, m)
            after = blockworker._shape_info(path, (0, 0, 0), 'cliff')
        self.assertEqual(before['pivot'], after['pivot'])
        self.assertEqual(before['relief'], after['relief'])
        self.assertNotEqual(blockworker._content_hash(json.dumps(before), '/old'),
                            blockworker._content_hash(json.dumps(after), '/new'))

    def test_heightmap_displaces_along_normal_without_slope_amplification(self):
        import cliffmaterial
        with patch.object(cliffmaterial, '_heightmap', return_value=np.full((8, 16), 0.2)):
            low = self.make(box=(-8, -8, 168, 168))
        with patch.object(cliffmaterial, '_heightmap', return_value=np.full((8, 16), 0.8)):
            high = self.make(box=(-8, -8, 168, 168))
        np.testing.assert_array_equal(low.tris, high.tris)
        original_x = (low.positions[:, 0]+2*low.positions[:, 2])/5
        middle = np.isclose(original_x, 80) & (low.positions[:, 1] == 80)
        self.assertTrue(middle.any())
        expected = 0.6 * cliffmaterial.RELIEF_M * cr.RELIEF_M[0]
        delta = high.positions[middle]-low.positions[middle]
        np.testing.assert_allclose(np.linalg.norm(delta, axis=1), expected, atol=2e-5)
        np.testing.assert_allclose(delta/expected, np.broadcast_to(np.array((-2,0,1))/np.sqrt(5), delta.shape), atol=2e-5)

    def test_near_vertical_skin_clears_coarse_terrain_at_different_grid_phases(self):
        x, y = np.arange(65)*.5, np.arange(33)*.5
        h = np.broadcast_to(40/(1+np.exp(-(x-16)/.15)), (len(y), len(x))).copy()
        keep = np.ones((len(y)-1, len(x)-1), bool)
        skin, _, _ = cr.displaced_surface(h, keep, x, y, np.zeros(keep.shape, int))
        # Before clearance correction this 40 m wall was penetrated by 2.7 m.
        # Test both vertices and points inside its triangles, not just normals.
        centres = np.stack(((skin[:-1, :-1]+skin[:-1, 1:]+skin[1:, 1:])/3,
                            (skin[:-1, :-1]+skin[1:, 1:]+skin[1:, :-1])/3))
        for phase in (0, .5, 1, 1.5):
            coarse = np.arange(-2+phase, 36+phase, 2)
            heights = np.interp(coarse, x, h[0])
            for points in (skin[6:-6, 6:-6], centres[:, 6:-6, 6:-6]):
                ground = np.interp(points[..., 0], coarse, heights)
                self.assertGreater((points[..., 2]-ground).min(), 0)

    def test_terrain_envelope_preserves_planes_and_shared_apron(self):
        x, y = np.meshgrid(np.arange(65)*.4, np.arange(49)*.5)
        plane = 12*x-4*y
        np.testing.assert_allclose(cr._terrain_envelope(plane, .4, .5), plane, atol=1e-12)
        h = plane + 3*np.sin(x)*np.cos(y)
        full = cr._terrain_envelope(h, .4, .5)
        part = cr._terrain_envelope(h[4:-4, 4:-4], .4, .5)
        np.testing.assert_allclose(full[12:-12, 12:-12], part[8:-8, 8:-8], atol=1e-12)

    def test_near_vertical_edge_fade_does_not_swallow_the_wall(self):
        h = np.broadcast_to(np.arange(17)*10., (17, 17))
        touches = np.full(h.shape, 4, np.uint8)
        touches[:, 0] = 2
        fade = cr._surface_fade(h, touches, .5, .5)
        self.assertTrue((fade[:, 0] == 0).all())
        self.assertTrue((fade[:, 1] == 1).all())
        flat = cr._surface_fade(np.zeros_like(h), touches, .5, .5)
        self.assertTrue((flat[:, 1] < .2).all())

    def test_coastal_water_labels_can_only_extend_a_confirmed_dry_cliff(self):
        x = np.arange(25)*4.0
        h = np.broadcast_to(np.clip(x-16, 0, 8)*4, (65, 25)).copy()
        classes = np.full(h.shape, 60)
        classes[:, :5] = 80  # low shore corner shares a cell with the 16 m wall
        keep = cr.qualifying_cells(h, classes)
        self.assertTrue(keep[:, 4:6].all())
        self.assertFalse(keep[:, :4].any(), 'flat water is never recovered')
        self.assertFalse(cr.qualifying_cells(h, np.full(h.shape, 80)).any(), 'water cannot seed a cliff')
        self.assertFalse(cr.qualifying_cells(h+1000, classes)[:, 4].any(), 'requires low coastal water')
        blocked = np.zeros(keep.shape, bool)
        blocked[:, 4] = True
        self.assertFalse(cr.qualifying_cells(h, classes, blocked)[:, 4].any(), 'road exclusion stays intact')
        for cover in (0, 40, 50):
            c = classes.copy(); c[:, 4] = cover
            self.assertFalse(cr.qualifying_cells(h, c)[:, 4].any(), 'unknown/cultivation/cities stay excluded')

    def test_recovered_shore_is_cut_to_dry_ground_during_refinement(self):
        x = np.arange(7)*4.0
        keep = np.ones((6, 6), bool)
        def elev(lat, lon):
            return (lon*cr.WORLD_M_PER_DEG-8)*4
        _, _, h, refined, _, _ = cr._refine(x, x, keep, np.zeros_like(keep, int),
            (0, 0, 24, 24), elev, None, shore=keep)
        lowest = np.minimum.reduce((h[:-1, :-1], h[:-1, 1:], h[1:, :-1], h[1:, 1:]))
        self.assertTrue(refined.any())
        self.assertTrue((lowest[refined] > cr.SHORE_DRY_MIN_M).all())

    def test_regional_tint_uses_mineral_colour_and_missing_imagery_falls_back(self):
        biomes = np.zeros(2, int)
        tint = cr.rock_colors(biomes, np.array(((185, 160, 125), (145, 148, 150))))
        self.assertGreater(int(tint[0, 0])-int(tint[0, 2]), 35)
        self.assertLess(abs(int(tint[1, 0])-int(tint[1, 2])), 30)
        np.testing.assert_array_equal(cr.rock_colors(biomes, np.zeros((2,3))), cr.rock_colors(biomes))

    def test_cached_colour_field_is_shared_between_tiles_and_ignores_water(self):
        import cliffcolors
        def photo(cache, lat, lon):
            out = np.broadcast_to((185.,160.,125.), (*lat.shape,3)).copy()
            out[lon < 0] = (60,100,130)  # water cannot turn coastal cliffs blue
            return out
        lat, lon = np.meshgrid(np.linspace(.01,.012,20), np.linspace(-.0001,.002,20))
        with patch.object(cliffcolors, '_samples', side_effect=photo):
            full = cliffcolors.sample('unused',lat,lon)
            part = cliffcolors.sample('unused',lat[5:15,5:15],lon[5:15,5:15])
        np.testing.assert_allclose(full[5:15,5:15],part)
        np.testing.assert_allclose(part, np.broadcast_to((185,160,125),part.shape))

    def test_regional_colour_rejects_shadow_and_olive_canopy(self):
        import cliffcolors
        def photo(cache, lat, lon):
            out = np.broadcast_to((80.,85.,58.), (*lat.shape,3)).copy()
            out[..., ::3, :] = (190,175,150)  # exposed mineral evidence
            out[..., 1::6, :] = (65,59,50)    # same ground in deep shade
            return out
        lat, lon = np.meshgrid(np.linspace(37,37.001,4), np.linspace(-122,-121.999,4))
        with patch.object(cliffcolors, '_samples', side_effect=photo):
            colour = cliffcolors.sample('unused',lat,lon)
        np.testing.assert_allclose(colour, np.broadcast_to((190,175,150),colour.shape))
        # A canopy-only region's fallback must not make the rock green either.
        fallback = cr.rock_colors(np.array([2]))[0].astype(int)
        self.assertLessEqual(fallback[1], fallback[0])

    def test_heightmap_periodicity_material_and_packaged_assets(self):
        import cliffmaterial
        from PIL import Image
        u, v = np.array((0.01, 0.27, -1.2)), np.array((0.91, -0.14, 1.2))
        np.testing.assert_allclose(cliffmaterial.sample(u, v), cliffmaterial.sample(u+1, v-2), atol=1e-6)
        root = Path(__file__).resolve().parents[2]
        art = root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth'
        material = json.loads((art/'cliff.materials.json').read_text())[cr.MATERIAL]['Stages'][0]
        self.assertEqual(material['metallicFactor'], 0)
        self.assertLessEqual(material['detailNormalMapStrength'], .3)
        self.assertGreater(material['detailScale'][0], 50)  # sub-2 m detail; macro scale unchanged
        # Fine normals are packaged alongside the macro maps, so a clean install
        # must resolve and decode them without runtime game-asset extraction.
        for key in ('baseColorMap', 'normalMap', 'roughnessMap', 'ambientOcclusionMap', 'detailNormalMap'):
            path = root/'mod/beamng_earth'/material[key].lstrip('/')
            with Image.open(path) as im:
                self.assertTrue(all(n & (n-1) == 0 for n in im.size), (key, im.size))
        with Image.open(cliffmaterial.HEIGHT_PATH) as im:
            self.assertEqual(im.size, cliffmaterial.HEIGHT_SIZE)
        self.assertIn('cliff_height.png', (root/'tools/build_app.py').read_text())

    def test_height_sampler_uses_texture_pixel_centres(self):
        import cliffmaterial
        values = np.arange(32, dtype=np.float32).reshape(4,8)/32
        u, v = np.meshgrid((np.arange(8)+.5)/8, (np.arange(4)+.5)/4)
        with patch.object(cliffmaterial, '_heightmap', return_value=values):
            np.testing.assert_allclose(cliffmaterial.sample(u,v), values*cliffmaterial.RELIEF_M)

    def test_mixed_cut_and_natural_surface_has_one_triangle_layer(self):
        keep = np.ones((20,20),bool)
        cuts = np.zeros_like(keep)
        cuts[:, :10] = True
        m = cr.mesh(self.h, keep, self.x, self.x, np.zeros_like(keep,int),
                    (0,0,160,160), (0,0,0), cuts=cuts)
        self.assertEqual(set(m.tri_materials), {0,1})
        centers = m.positions[m.tris].mean(axis=1)
        self.assertEqual(len(np.unique(centers,axis=0)),len(centers), 'no duplicate skin')
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'mixed.cdae')
            cr.write(path,m)
            shape = cdae.read(path)
            self.assertEqual(len(shape['materials']),2)

    def test_cut_heightmap_and_material_are_distinct_and_packaged(self):
        import cliffmaterial as cm
        from PIL import Image
        u,v = np.meshgrid(np.linspace(0,1,32),np.linspace(0,1,16))
        np.testing.assert_allclose(cm.sample(u,v,True),cm.sample(u+1,v-2,True),atol=1e-6)
        self.assertLess(cm.sample(u,v,True).max(),cm.RELIEF_M)
        root = Path(__file__).resolve().parents[2]
        mats = json.loads((root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        for key in ('baseColorMap','normalMap','roughnessMap','ambientOcclusionMap'):
            self.assertNotEqual(mats[cm.ROAD_CUT_MATERIAL]['Stages'][0][key],mats[cr.MATERIAL]['Stages'][0][key])
            with Image.open(root/'mod/beamng_earth'/mats[cm.ROAD_CUT_MATERIAL]['Stages'][0][key].lstrip('/')) as im:
                self.assertEqual(im.size,(4096,4096))
        with Image.open(cm.ROAD_CUT_HEIGHT_PATH) as im:
            self.assertEqual(im.size,cm.HEIGHT_SIZE)
        self.assertIn('cliff_cut_height.png',(root/'tools/build_app.py').read_text())


if __name__ == '__main__':
    unittest.main()
