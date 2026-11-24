"""Distant cliff coverage must not add triangles, move ground or texture skirts."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import sys, tempfile, unittest, json
from pathlib import Path
from collections import Counter
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import farfield as ff
import farcliffs as fc
import cliffmaterial as cm
import cliffrocks as cr
import geodesy,cdae


def texture_source(vfs):
    """Resolve installed material paths through the shipped lossless manifest."""
    import modtextures
    root = Path(__file__).resolve().parents[2] / 'mod/texture_sources'
    entry = modtextures.manifest(root/'textures.json')[vfs.lstrip('/')]
    return root/entry['source']


def built(x0=0,slope=2):
    x,y=np.meshgrid(np.arange(9)*16.,np.arange(9)*16.)
    h=1000+slope*x
    p=np.column_stack((x.ravel(),y.ravel(),h.ravel()))
    t=ff.grid_triangles(8)
    # One vertical skirt: it must remain terrain even in an all-cliff tile.
    p=np.vstack((p,p[0]-(0,0,100),p[1]-(0,0,100)))
    t=np.vstack((t,[0,81,1],[1,81,82]))
    n=np.tile(np.array([-slope,0,1])/np.sqrt(1+slope*slope),(len(p),1))
    planar=p[:,:2]/128
    return ff.Built([x0,0,0],p,n,planar.copy(),planar,t,h,0,None,100,128)

class Cover:
    def __init__(self,value): self.value=value
    def classes(self,lat,lon,*,fetch=True):
        assert not fetch, "distant cliffs must not queue land-cover downloads"
        return np.full(lat.shape,self.value,np.uint8),True

class FarCliffsTest(unittest.TestCase):
    def test_native_photo_filter_matches_upstream_pillow_behavior(self):
        import fast
        import cliffcolors
        from PIL import Image, ImageFilter
        rng = np.random.default_rng(81)
        for h, w in ((1, 1), (1, 17), (19, 1), (23, 31)):
            pixels = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
            for radius in (0., .25, 1., 3.75, 48.):
                expected = np.asarray(Image.fromarray(pixels).filter(ImageFilter.GaussianBlur(radius)),
                                      np.float32) / 255
                expected = cliffcolors.linear(expected).astype(np.float32)
                actual = fast._ef.cliff_far_photo_field(pixels, radius)
                np.testing.assert_allclose(actual, expected, atol=2e-7, rtol=0)
                np.testing.assert_array_equal(actual, fast._ef.cliff_far_photo_field(pixels, radius))
        # NumPy views are accepted without relying on a contiguous caller buffer.
        pixels = pixels[:, ::-1]
        np.testing.assert_array_equal(fast._ef.cliff_far_photo_field(pixels, 2.),
                                      fast._ef.cliff_far_photo_field(pixels.copy(), 2.))
        for radius in (-1., float('nan'), float('inf'), 1e20):
            with self.assertRaises(ValueError):
                fast._ef.cliff_far_photo_field(pixels, radius)
        for shape in ((0, 1, 3), (1, 0, 3), (2, 2, 4)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_far_photo_field(np.zeros(shape, np.uint8), 1.)

    def test_native_boundaries_match_shared_real_terrain_vertices(self):
        import fast
        rng = np.random.default_rng(91)
        b = built()
        for _ in range(30):
            labels = rng.integers(-1, 3, len(b.tris), dtype=np.int8)
            face_z = np.cross(b.positions[b.tris[:,1]]-b.positions[b.tris[:,0]],
                              b.positions[b.tris[:,2]]-b.positions[b.tris[:,0]])[:,2]
            expected = np.intersect1d(np.unique(b.tris[labels >= 0]),
                                     np.unique(b.tris[(labels < 0) & (face_z > 1e-5)]))
            np.testing.assert_array_equal(fast._ef.cliff_far_boundaries(b.positions, b.tris, labels), expected)
        with self.assertRaises(ValueError):
            fast._ef.cliff_far_boundaries(b.positions, b.tris+len(b.positions), labels)
        self.assertEqual(fast._ef.cliff_far_boundaries(b.positions, np.empty((0,3), np.int64),
                                                     np.empty(0, np.int8)).shape, (0,))

    def test_native_layout_preserves_source_order_and_rejects_bad_indices(self):
        import fast
        triangles = np.array([[2, 0, 1], [3, 2, 1]], np.int64)
        labels = np.array([-1, 1], np.int8)
        axes = np.array([False, True])
        chart = np.array([[100., 200., 300.], [104., 208., 312.],
                          [108., 216., 324.], [112., 224., 336.]])
        uv = np.arange(8, dtype=float).reshape(4, 2)
        scales = np.array([[2., 3., 4., 5.]]*3)
        args = [triangles, labels, axes, chart, uv, uv+1, uv+2, scales]
        groups = fast._ef.cliff_far_layout(*args)
        self.assertEqual([g[0] for g in groups], [0, 2])
        np.testing.assert_array_equal(groups[0][1], [0, 1, 2])
        np.testing.assert_array_equal(groups[0][2], [[2, 0, 1]])
        np.testing.assert_array_equal(groups[0][3], uv[:3])
        np.testing.assert_array_equal(groups[0][4], (uv+1)[:3])
        np.testing.assert_array_equal(groups[0][5], (uv+2)[:3])
        np.testing.assert_array_equal(groups[1][1], [1, 2, 3])
        np.testing.assert_array_equal(groups[1][2], [[5, 4, 3]])
        absolute = chart[1:, 1:]/[2., 3.]
        normal = absolute/[4., 5.]
        np.testing.assert_allclose(groups[1][3], absolute-np.floor(absolute[0]))
        np.testing.assert_allclose(groups[1][4], normal-np.floor(normal[0]))
        np.testing.assert_array_equal(groups[1][4], groups[1][5])
        for index, value in ((0, triangles+10), (1, np.array([-1, 3], np.int8)),
                             (7, np.zeros((3, 4)))):
            invalid = args.copy()
            invalid[index] = value
            with self.assertRaises(ValueError):
                fast._ef.cliff_far_layout(*invalid)
        empty = args.copy()
        empty[:3] = [np.empty((0, 3), np.int64), np.empty(0, np.int8), np.empty(0, bool)]
        self.assertEqual(fast._ef.cliff_far_layout(*empty), [])

    def test_srgb_dds_colour_sampling_matches_legacy_without_changing_file(self):
        import imagery
        rgb = np.random.default_rng(72).integers(60, 220, (32, 32, 3), dtype=np.uint8)
        uv = np.array([[0., 0.], [.5, .5], [.9, .2], [1., 1.]])
        with tempfile.TemporaryDirectory() as tmp:
            legacy, srgb = Path(tmp)/'legacy.dds', Path(tmp)/'srgb.dds'
            legacy.write_bytes(imagery.encode_dds(rgb))
            encoded = imagery.encode_dds(rgb, srgb=True)
            srgb.write_bytes(encoded)
            actual = fc.terrain_colors(srgb, uv, 128)
            self.assertIsNotNone(actual)
            np.testing.assert_array_equal(actual, fc.terrain_colors(legacy, uv, 128))
            self.assertEqual(srgb.read_bytes(), encoded)

    def test_native_detail_reduction_matches_shader_and_reads_lossless_sources(self):
        import fast
        import cliffcolors
        from unittest.mock import patch
        from PIL import Image
        rng = np.random.default_rng(721)
        pixels = rng.integers(0, 256, (13, 48, 3), dtype=np.uint8)
        factor = np.array([.8, 1.1, .9])
        for strength in (0., .65, .84, 1.5):
            gain = np.clip(1+(2*cliffcolors.linear(pixels.astype(float)/255)-1)*strength, 0, 1)*factor
            expected = gain.reshape(13, 8, 6, 3).mean(axis=(0, 2))
            actual = fast._ef.cliff_far_detail_row(pixels, 8, strength, factor.tolist())
            np.testing.assert_allclose(actual, expected, atol=6e-8)
        for columns in (0, 7):
            with self.assertRaises(ValueError):
                fast._ef.cliff_far_detail_row(pixels, columns, .65, factor.tolist())
        fc._canyon_detail_field.cache_clear()
        try:
            with patch.object(Image, 'open', wraps=Image.open) as opened:
                for family in (1, 2):
                    field = fc._canyon_detail_field(family)
                    self.assertEqual(field.shape, (64, 64, 3))
                    self.assertTrue(np.isfinite(field).all())
                    self.assertGreater(float(np.ptp(field)), .1)
                self.assertEqual(opened.call_count, 2)
                self.assertTrue(all(Path(call.args[0]).suffix == '.webp' for call in opened.call_args_list))
                fc._canyon_detail_field(1)
                self.assertEqual(opened.call_count, 2)
        finally:
            fc._canyon_detail_field.cache_clear()

    def test_native_color_sampling_edges_wrap_and_orientation(self):
        rng = np.random.default_rng(104)
        grid = rng.random((7, 11, 3), dtype=np.float32)
        uv = np.vstack((rng.uniform(-3, 3, (1000, 2)), [[0, 0], [1, 1], [.5, .5]]))
        for periodic in (False, True):
            xy = (np.mod(uv, 1) if periodic else np.column_stack((uv[:, 0], 1-uv[:, 1]))) * [11, 7] - .5
            if not periodic:
                xy = np.clip(xy, 0, [10, 6])
            indices = np.floor(xy).astype(int)
            fractions = xy - indices
            reference = np.zeros((len(uv), 3))
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                ij = indices + [dx, dy]
                ij = ij % [11, 7] if periodic else np.clip(ij, 0, [10, 6])
                weight = np.prod(np.where([dx, dy], fractions, 1-fractions), axis=1)
                reference += grid[ij[:, 1], ij[:, 0]] * weight[:, None]
            np.testing.assert_allclose(fc._sample_colors(grid, uv, periodic=periodic), reference, atol=1e-14)
            self.assertEqual(fc._sample_colors(grid, np.empty((0, 2)), periodic=periodic).shape, (0, 3))
            np.testing.assert_allclose(fc._sample_colors(np.ones((1, 1, 3), np.float32), uv,
                                                       periodic=periodic), 1)
        for grid_bad, uv_bad in ((grid, [[np.nan, 0]]), (grid, [[0]]), (grid[:0], [[0, 0]])):
            with self.assertRaises(ValueError):
                fc._sample_colors(grid_bad, uv_bad, periodic=False)

    def setUp(self):
        from unittest.mock import patch
        import cliffsettings
        self.frame = geodesy.EnuFrame(36.1, -112.1)
        enabled = patch.object(cliffsettings, "SETTINGS", cliffsettings.CliffSettings(True, True))
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_global_refresh_keeps_corrected_shape_for_bulk_restore(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        from PIL import Image
        import earthd, farrestore, revisit, revisitpack, blockworker
        with tempfile.TemporaryDirectory() as directory:
            server = earthd.Server.__new__(earthd.Server)
            server.out_dir = server.cache_dir = directory
            server.far_format = 'cdae'
            server.surface = Cover(60)
            tile = ff.GTile(9, 101, 150, 4, (0,0,128,0,128,128,0,128), 128.)
            anchor = SimpleNamespace(id=1, frame=self.frame)
            b = built()
            b.fin_base_key = revisit.key('fargeo', 'refresh-fixture')
            photo = Path(directory)/'corrected.png'
            Image.new('RGB', (32,32), (130,110,85)).save(photo)
            previous = earthd.TileInfo([0,0,0], 1., None, 100., b.grid, 10, 0.,
                material=farrestore.placeholder(20), texture='old.png', tile=tile, tex_px=32,
                cliff_colors=True, frame=ff.global_frame_ll(tile.z, tile.x, tile.y))
            updated = replace(previous, rev=1, texture=photo.name)
            try:
                with patch.object(server, '_retexture', return_value=updated), \
                     patch.object(server, '_tile_geometry', return_value=b), \
                     patch.object(server, 'fin_name_len', return_value=20):
                    info = server.retexture(anchor, tile, previous)
                identity = farrestore.photo_identity(photo)
                key = farrestore.key(b.fin_base_key, 32, 20, identity)
                cached = revisit.load(directory, farrestore.KIND, key)
                self.assertIsNotNone(cached)
                self.assertTrue(cached[1]['cliff_colors'])
                self.assertEqual((Path(directory)/info.shape).read_bytes(),
                                 farrestore.stamp(cached[0]['shape'], 20, info.material))
                spec = ('global', tile.z, tile.x, tile.y, 10, 11, ff.Layout(32), [32], 20, identity)
                with patch.object(blockworker, '_s', SimpleNamespace(cache_dir=directory)), \
                     patch.object(blockworker, '_far_global_key', return_value=('refresh-fixture',)):
                    self.assertIsNotNone(blockworker.far_restore_find(1, 0, 0, [spec])[0])
                    self.assertIsNone(blockworker.far_restore_find(1, 0, 0, [spec[:-1]+('old-photo',)])[0])
            finally:
                revisitpack.reset()

    def test_server_refresh_writes_new_mesh_and_sends_swap_payload(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import patch
        from PIL import Image
        import earthd, cliffsettings
        with tempfile.TemporaryDirectory() as directory:
            server = earthd.Server(directory, str(Path(directory)/'cache'), imagery=None)
            try:
                tile = ff.Tile(1, 0, 0)
                anchor = SimpleNamespace(id=1, frame=self.frame)
                ck = server.far_ck(1, tile.key)
                material = server.matslots.take(ck)
                original = Path(server.out_dir)/server.tile_name(*ck)
                original.write_bytes(b'original mesh must remain intact')
                photo = server.texture_name(*ck, 1)
                Image.new('RGB', (32,32), (120,100,80)).save(Path(server.out_dir)/photo, format='PNG')
                previous = earthd.TileInfo([0,0,0], 1., None, 100., None, 10, 0., material=material,
                                          texture=server.texture_name(*ck), tile=tile, tex_px=32, cliff_colors=True)
                updated = replace(previous, rev=1, texture=photo)
                for natural in (False, True):
                    for cuts in (False, True):
                        with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(natural, cuts)), \
                             patch.object(server, '_retexture', return_value=updated), \
                             patch.object(server, '_tile_geometry', return_value=built()) as geometry:
                            info = server.retexture(anchor, tile, previous)
                        self.assertEqual(geometry.call_count, int(natural))
                        self.assertEqual(original.read_bytes(), b'original mesh must remain intact')
                        self.assertEqual(info.mesh_rev, int(natural))
                        if natural:
                            self.assertNotEqual(info.shape, original.name)
                            self.assertTrue((Path(server.out_dir)/info.shape).is_file())
                            self.assertIn(str(Path(server.out_dir)/info.shape),
                                          server.tile_files(original.name, info.texture, info.rev))
                            messages = []
                            viewer = SimpleNamespace(active=1, send=messages.append,
                                                     credits=SimpleNamespace(poke=lambda _: None))
                            with patch.object(server, '_retex_viewers', return_value=[viewer]):
                                server._send_retex(ck, info)
                            replacement = messages[0]['replacement']
                            from test_protocol_agreement import SERVER_TO_MOD, OPT
                            for message in (messages[0], replacement):
                                spec = SERVER_TO_MOD[message['type']]
                                self.assertLessEqual(set(message)-{'type'}, set(spec))
                                for field,(types,flags) in spec.items():
                                    if field in message:
                                        self.assertIsInstance(message[field], types)
                                    else:
                                        self.assertIn(OPT, flags)
                            self.assertEqual(replacement['shape'], f'{server.vfs_dir}/{info.shape}')
                            self.assertEqual(replacement['texture'], messages[0]['texture'])
                            self.assertEqual(replacement['key'], tile.key)
                            # Reslotting must read the latest revision, not the original bytes above.
                            server.tiles[ck] = info
                            reslotted = server.reslot(ck, info)
                            self.assertEqual((Path(server.out_dir)/reslotted.shape).read_bytes(),
                                             (Path(server.out_dir)/info.shape).read_bytes())
                with patch.object(server, '_retexture', return_value=previous), \
                     patch.object(server, '_tile_geometry', side_effect=AssertionError('failed photo must not rebuild')):
                    self.assertIs(server.retexture(anchor, tile, previous), previous)
            finally:
                server.tile_pool.shutdown(wait=True)
                server.hm_pool.shutdown(wait=True)

    def test_global_cache_pairs_shape_with_exact_photo_for_all_flags(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from PIL import Image
        import blockworker, cliffsettings, farrestore, revisit, revisitpack
        with tempfile.TemporaryDirectory() as directory:
            texture = Path(directory) / 'photo.png'
            state = SimpleNamespace(cache_dir=directory, surface=Cover(60))
            base = revisit.key('fargeo', 'global-test')
            try:
                self.assertTrue(revisit.store(directory, 'fargeo', base, {'geometry': b'test'}, {}))
                for natural in (False, True):
                    for cuts in (False, True):
                        settings = cliffsettings.CliffSettings(natural, cuts)
                        with patch.object(blockworker, '_s', state), \
                             patch.object(blockworker, '_far_global_key', return_value=('global-test',)), \
                             patch.object(cliffsettings, 'SETTINGS', settings):
                            identities = []
                            for color in ((120, 100, 80), (80, 100, 120)):
                                Image.new('RGB', (32, 32), color).save(texture)
                                identity = farrestore.photo_identity(texture) if natural else None
                                identities.append(identity)
                                b = built()
                                with patch.object(fc, 'geometry', wraps=fc.geometry) as geometry:
                                    blockworker._keep_fin_global(9, 101, 150, 10, 11, ff.Layout(32), b,
                                                                 32, 20, str(texture))
                                self.assertEqual(b.fin_base_key, base)
                                key = farrestore.key(base, 32, 20, identity)
                                self.assertTrue(farrestore.has(directory, key))
                                spec = ('global', 9, 101, 150, 10, 11, ff.Layout(32), [32], 20, identity)
                                self.assertIsNotNone(blockworker.far_restore_find(0, 0, 0, [spec])[0])
                                if natural:
                                    self.assertEqual(geometry.call_count, 1)
                                    self.assertEqual(b.shape_photo, identity)
                                    wrong = spec[:-1] + ('wrong-photo',)
                                    self.assertIsNone(blockworker.far_restore_find(0, 0, 0, [wrong])[0])
                                else:
                                    self.assertEqual(geometry.call_count, 0)
                            if natural:
                                self.assertNotEqual(*identities)
            finally:
                revisitpack.reset()

    def test_anchor_worker_keeps_shape_with_finished_photo(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from PIL import Image
        import blockworker
        import cliffsettings
        import farrestore
        import revisitpack

        with tempfile.TemporaryDirectory() as directory:
            texture = Path(directory) / 'finished.png'
            Image.new('RGB', (32, 32), (125, 108, 89)).save(texture)
            state = SimpleNamespace(cache_dir=directory, surface=Cover(60))
            tex = dict(path=str(texture), px=32, keep_fin=20)
            try:
                for natural in (False, True):
                    for cuts in (False, True):
                        settings = cliffsettings.CliffSettings(natural, cuts)
                        b = built()
                        with patch.object(blockworker, '_s', state), \
                             patch.object(cliffsettings, 'SETTINGS', settings), \
                             patch.object(fc, 'geometry', wraps=fc.geometry) as geometry:
                            blockworker._keep_fin('anchor', b, tex, None, (100, {}), 36.1, -112.1)
                            self.assertEqual(geometry.call_count, int(natural))
                            if natural:
                                self.assertEqual(geometry.call_args.kwargs['terrain_texture'], str(texture))
                                self.assertIs(geometry.call_args.kwargs['surface'], state.surface)
                            direct = ff.built_shape(b, farrestore.placeholder(20), 32, packed=False,
                                cliff_frame=self.frame, cliff_surface=state.surface, cliff_texture=str(texture))
                            self.assertEqual(b.shape_plain, (direct, 20, 32))
                            key = farrestore.key('anchor', 32, 20)
                            self.assertTrue(farrestore.has(directory, key))
            finally:
                revisitpack.reset()

    def test_projection_charts_keep_disconnected_faces_independent(self):
        points = np.array([[0.,0.,0.], [1.,0.,2.], [0.,1.,1.],
                           [10.,0.,0.], [11.,0.,1.], [10.,1.,2.]])
        tris = np.array([[0,1,2], [3,4,5]], np.int64)
        for labels in (np.array([1,1]), np.array([-1,-1]), np.array([0,2])):
            np.testing.assert_array_equal(fc.projection_axes(points, tris, labels), [True, False])
        self.assertEqual(fc.projection_axes(points, np.empty((0,3), np.int64), np.empty(0)).shape, (0,))
        with self.assertRaises(ValueError):
            fc.projection_axes(points, tris, np.array([1]))

    def test_direct_and_cached_writers_agree_for_all_startup_flags(self):
        from unittest.mock import patch
        import cliffsettings
        import farrestore
        b = built()
        keys = set()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tile.cdae'
            for natural in (False, True):
                for cuts in (False, True):
                    settings = cliffsettings.CliffSettings(natural, cuts)
                    with patch.object(cliffsettings, 'SETTINGS', settings), \
                         patch.object(fc, 'geometry', wraps=fc.geometry) as geometry:
                        ff.write_built(str(path), b, material='terrain', textured=True, cliff_frame=self.frame)
                        direct = path.read_bytes()
                        cached = ff.built_shape(b, 'terrain', cliff_frame=self.frame)
                        self.assertEqual(direct, cached)
                        self.assertEqual(geometry.call_count, 2 if natural else 0)
                        keys.add(farrestore.key('same-base', 256, 7))
            self.assertEqual(len(keys), 4)

    def test_cliff_shape_survives_bulk_cache_restore(self):
        import farrestore
        import revisitpack
        b = built()
        name = 'bngEarthFar_ab12_00001'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = str(root / 'cache')
            key = farrestore.key('cliff-restore-test', 256, len(name))
            self.assertTrue(farrestore.keep(cache, key, b, 256, len(name), cliff_frame=self.frame))
            revisitpack.reset()
            loc, sides = farrestore.locate(cache, key)
            shape, slot = root / 'restored.cdae', root / 'slot.cdae'
            files = (str(shape)+'.tmp', str(shape), str(slot)+'.tmp', str(slot))
            result, = farrestore.restore([(loc, farrestore.placeholder(len(name)), name, files, [])])
            self.assertIsInstance(result, tuple, result)
            reference = root / 'direct.cdae'
            ff.write_built(str(reference), b, material=name, textured=True, tex_px=256,
                           cliff_frame=self.frame)
            self.assertEqual(shape.read_bytes(), reference.read_bytes())
            self.assertEqual(slot.read_bytes(), reference.read_bytes())
            revisitpack.reset()

    def test_missing_cover_can_render_but_is_not_kept_as_final_shape(self):
        from unittest.mock import Mock, patch
        import surface
        import farrestore
        sampler = surface.Surface.__new__(surface.Surface)
        sampler.md = Mock()
        sampler.md.has.return_value = False
        sampler._queue = Mock(side_effect=AssertionError('distant cliff queued cover'))
        b = built()
        with tempfile.TemporaryDirectory() as directory, \
             patch('revisit.store', side_effect=AssertionError('incomplete cover cached')) as store:
            self.assertFalse(farrestore.keep(directory, 'incomplete-cover', b, 256, 20,
                                            cliff_frame=self.frame, cliff_surface=sampler))
            self.assertTrue(b.shape_plain[0])
            store.assert_not_called()
            sampler._queue.assert_not_called()

    def test_large_cliff_parts_preserve_geometry_normals_and_second_uvs(self):
        from unittest.mock import patch
        b = built()
        original = ff._shape_geometry(b, 'terrain', b.planar, b.detail_uvs, self.frame)
        whole = cdae.decode(cdae.encode(**original, close=False))
        # Exercise upstream's shared overflow path without a huge fixture.
        with patch.object(cdae, 'VERTS_MAX', 48):
            split = cdae.decode(cdae.encode(**original, close=False))
        meshes = [m for m in split['meshes'] if m['type'] != 4]
        self.assertGreater(len(meshes), 1)
        for mesh in meshes:
            self.assertLessEqual(len(cdae.mesh_arrays(mesh)['verts']), 48)
        def face_rows(shape):
            rows = []
            for mesh in shape['meshes']:
                if mesh['type'] == 4:
                    continue
                arrays = cdae.mesh_arrays(mesh)
                colors = np.frombuffer(mesh['colors'][2], np.uint8).reshape(-1, 4)
                records = np.column_stack([arrays[k] for k in
                    ('verts', 'norms', 'tverts', 'tverts2', 'tangents')] + [colors])
                rows.append(records[arrays['indices']])
            return np.concatenate(rows)
        np.testing.assert_array_equal(face_rows(split), face_rows(whole))
        self.assertEqual(split['materials'], whole['materials'])
        self.assertEqual(sum(len(cdae.mesh_arrays(m)['indices'])//3 for m in meshes), len(b.tris))

    def test_native_candidates_reject_bad_indices_and_ignore_missing_vertices(self):
        import fast
        points = np.array([[0.,0.,0.], [16.,0.,32.], [0.,16.,0.]])
        tris = np.array([[0,1,2]], np.int64)
        ids, centers, forest = fast._ef.cliff_far_candidates(points, tris, 16.)
        np.testing.assert_array_equal(ids, [0])
        np.testing.assert_allclose(centers, points.mean(axis=0)[None, :])
        self.assertFalse(forest[0])
        points[1, 2] = np.nan
        self.assertEqual(len(fast._ef.cliff_far_candidates(points, tris, 16.)[0]), 0)
        with self.assertRaises(ValueError):
            fast._ef.cliff_far_candidates(points, np.array([[0,1,3]], np.int64), 16.)
        with self.assertRaises(ValueError):
            fast._ef.cliff_far_candidates(points, tris, 0.)

    def test_region_is_shared_and_does_not_recolor_existing_sites(self):
        np.testing.assert_array_equal(cm.natural_family([36.1,37.2,38.5,31.8,30.3,48],
            [-112.1,-113,-109.7,-106.5,-97.8,8]),[cm.CANYON]*3+[cm.ARID_CUT,cm.NATURAL,cm.NATURAL])

    def test_coverage_and_exclusions(self):
        b=built()
        labels=fc.classify(b,self.frame)
        self.assertTrue((labels[:-2]==1).all(),'broad canyon wall qualifies without near-road cover data')
        self.assertTrue((labels[-2:]==-1).all(),'skirts never qualify')
        for cover in (10,40,50,70,80,90):
            self.assertTrue((fc.classify(b,self.frame,Cover(cover))==-1).all())
        for cover in (20,30,60):
            self.assertTrue((fc.classify(b,self.frame,Cover(cover))[:-2]==1).all())
        other=geodesy.EnuFrame(30.3,-97.8)
        self.assertTrue((fc.classify(b,other)==-1).all(),'unknown outside plateau fails closed')
        self.assertTrue((fc.classify(b,other,Cover(60))[:-2]==0).all())
        self.assertTrue((fc.classify(built(slope=.3),self.frame)==-1).all())
        b.size=2048
        self.assertTrue((fc.classify(b,self.frame)==-1).all(),'coarsest horizon stays photo terrain')

    def test_sheer_forest_rim_wall_has_fallback_without_opening_other_exclusions(self):
        for frame,family in ((self.frame,1),(geodesy.EnuFrame(31.8,-106.5),2)):
            for slope_deg,expected in ((70,-1),(80,family),(85,family)):
                b=built(slope=np.tan(np.deg2rad(slope_deg)))
                labels=fc.classify(b,frame,Cover(10))
                self.assertTrue((labels[:-2]==expected).all())
                self.assertTrue((labels[-2:]==-1).all())
                for excluded in (40,50,70,80,90):
                    self.assertTrue((fc.classify(b,frame,Cover(excluded))==-1).all())

    def test_fine_faces_do_not_lose_coverage_when_lod_refines(self):
        # The same inclined surface, with smaller triangles, must still qualify.
        for spacing in (0.5,1,2,4,8,16):
            b=built(slope=1.5)
            factor=spacing/16
            b.positions[:,:2]*=factor
            b.positions[:,2]=1000+(b.positions[:,2]-1000)*factor
            b.size*=factor
            for frame,cover,family in ((self.frame,None,1),(geodesy.EnuFrame(31.8,-106.5),Cover(60),2)):
                with self.subTest(spacing=spacing,family=family):
                    labels=fc.classify(b,frame,cover)
                    self.assertTrue((labels[:-2]==family).all())
                    self.assertTrue((labels[-2:]==-1).all())

    def test_smooth_diagonal_wall_has_shared_normal_uvs_across_triangles(self):
        b=built()
        x,y=b.positions[:81,:2].T
        b.positions[:81,2]=1000+2*(x+y)+8*np.sin(x/32)*np.sin(y/32)
        t=b.tris[:-2]
        cross=np.cross(b.positions[t[:,1]]-b.positions[t[:,0]],b.positions[t[:,2]]-b.positions[t[:,0]])
        old_axes=np.abs(cross[:,0]) >= np.abs(cross[:,1])
        self.assertEqual(len(np.unique(old_axes)),2,'fixture crosses the old 45-degree projection switch')
        labels=np.r_[np.ones(len(t),np.int8),[-1,-1]]
        axes=fc.projection_axes(b.positions,b.tris,labels)
        self.assertEqual(len(np.unique(axes[:-2])),1,'one continuous wall must have one chart')
        from unittest.mock import patch
        with patch.object(fc,'classify',return_value=labels):
            out=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain')
        ids=np.unique(out['tris'][out['tri_materials']==2])
        self.assertEqual(len(ids),81,'no duplicated wall vertices at triangle chart boundaries')
        self.assertEqual(len(out['tris']),len(b.tris))
        self.assertTrue(np.isfinite(out['uvs2']).all())
        # A perpendicular wall still chooses its well-conditioned projection.
        for slope_axis,expected in ((0,True),(1,False)):
            q=built();q.positions[:,2]=1000+2*q.positions[:,slope_axis]
            self.assertTrue((fc.projection_axes(q.positions,q.tris,labels)[:-2]==expected).all())

    def test_coarse_bend_keeps_one_chart_when_both_projections_are_valid(self):
        b=built()
        x,y=b.positions[:81,:2].T
        b.positions[:81,2]=1000+2*y+np.where(x<=64,x,64+4*(x-64))
        labels=np.r_[np.ones(len(b.tris)-2,np.int8),[-1,-1]]
        t=b.tris[:-2]
        normals=np.cross(b.positions[t[:,1]]-b.positions[t[:,0]],
                         b.positions[t[:,2]]-b.positions[t[:,0]])
        dominant=np.abs(normals[:,0])>=np.abs(normals[:,1])
        self.assertEqual(len(np.unique(dominant)),2)
        axes=fc.projection_axes(b.positions,b.tris,labels)
        self.assertEqual(len(np.unique(axes[:-2])),1,
            'a 37-degree terrain bend must not reset normal texture phase')
        from unittest.mock import patch
        with patch.object(fc,'classify',return_value=labels):
            out=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain')
        walls=np.unique(out['tris'][out['tri_materials']==2])
        self.assertEqual(len(walls),81)
        self.assertEqual(len(out['tris']),len(b.tris))

    def test_export_preserves_geometry_and_collision_exactly(self):
        b=built()
        rock=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain')
        def faces(p,t):
            return Counter(tuple(sorted(tuple(v) for v in tri)) for tri in p[t])
        self.assertEqual(faces(b.positions,b.tris),faces(rock['positions'],rock['tris']))
        self.assertEqual(len(b.tris),len(rock['tris']))
        self.assertTrue(np.isfinite(rock['uvs']).all())
        for mat in (0,2):
            selected=rock['tris'][rock['tri_materials']==mat]
            self.assertGreater(len(selected),0)
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'far.cdae')
            ff.write_built(path,b,textured=True,material='bngEarthFar_ab12_00001',cliff_frame=self.frame)
            shape=cdae.read(path)
            self.assertEqual([m['name'] for m in shape['materials']],['bngEarthFar_ab12_00001','bne_far_cliff_canyon_blend'])
            count=sum(len(cdae.mesh_arrays(m)['indices'])//3 for m in shape['meshes'] if m['type']!=4)
            self.assertEqual(count,len(b.tris))
            import matslots
            data=cdae.plain(Path(path).read_bytes())
            moved=cdae.decode(matslots.restamp(data,'bngEarthFar_ab12_00001','bngEarthFar_ab12_00002'))
            self.assertEqual([m['name'] for m in moved['materials']],
                             ['bngEarthFar_ab12_00002','bne_far_cliff_canyon_blend'])
            self.assertEqual(moved['meshes'],shape['meshes'],'slot restamping leaves rock and terrain geometry intact')

    def test_adjacent_tiles_have_matching_vertical_texture_phase(self):
        a,b=built(),built(x0=128)
        # Both sides share the same world height along x=128.
        b.positions[:,2]+=256
        a=fc.geometry(a,self.frame,a.planar,a.detail_uvs,'terrain')
        b=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain')
        def boundary(mesh,x):
            ids=np.unique(mesh['tris'][mesh['tri_materials']==2])
            ids=ids[np.isclose(mesh['positions'][ids,0],x)]
            return {round(mesh['positions'][i,1],5):mesh['uvs'][i] for i in ids}
        left,right=boundary(a,128),boundary(b,0)
        self.assertEqual(left.keys(),right.keys())
        for y in left:
            delta=left[y]-right[y]
            np.testing.assert_allclose(delta,np.round(delta),atol=1e-7)

    def test_far_normal_scale_preserves_color_geometry_and_world_phase(self):
        from unittest.mock import patch
        source=built()
        labels=fc.classify(source,self.frame)
        reference=fc.geometry(source,self.frame,source.planar,source.detail_uvs,'terrain')
        walls=np.unique(reference['tris'][reference['tri_materials']==2])
        # Hold the face mask fixed to isolate the normal UV policy across LODs.
        for size,scale in ((2048,1),(3071,1),(3072,2),(4096,2),(6144,4),(8192,4),(16384,4)):
            a,b=built(),built(x0=128)
            a.size=b.size=size
            b.positions[:,2]+=256
            with patch.object(fc,'classify',return_value=labels):
                left=fc.geometry(a,self.frame,a.planar,a.detail_uvs,'terrain')
                right=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain')
            self.assertEqual(fc.normal_scale(size),scale)
            for field in ('positions','tris','normals','uvs','colors','tri_materials'):
                np.testing.assert_array_equal(left[field],reference[field])
            # Derivatives are reduced by scale; only normal repeat changes.
            np.testing.assert_allclose(np.diff(left['uvs2'][walls],axis=0)*scale*np.array(((1.5 if size >= 6144 else cm.FAR_CANYON_NORMAL_WIDTH_SCALE),1)),
                                       np.diff(left['uvs'][walls],axis=0),atol=1e-8)
            for axis_mesh,x in ((left,128),(right,0)):
                ids=np.unique(axis_mesh['tris'][axis_mesh['tri_materials']==2])
                ids=ids[np.isclose(axis_mesh['positions'][ids,0],x)]
                boundary={round(axis_mesh['positions'][i,1],5):axis_mesh['uvs2'][i] for i in ids}
                if axis_mesh is left:
                    first=boundary
                else:
                    self.assertEqual(first.keys(),boundary.keys())
                    for y in first:
                        delta=first[y]-boundary[y]
                        np.testing.assert_allclose(delta,np.round(delta),atol=1e-7)
            # CDAE stores UV1 with V flipped; it must survive serialization.
            shape=cdae.decode(cdae.encode(**left))
            for mesh in shape['meshes']:
                if mesh['type']==4:
                    continue
                arrays=cdae.mesh_arrays(mesh)
                self.assertTrue(np.isfinite(arrays['tverts2']).all())
                self.assertTrue(np.isfinite(arrays['tangents']).all())

    def test_disabled_variant_preserves_complete_satellite_terrain(self):
        b=built()
        with tempfile.TemporaryDirectory() as d:
            rock,plain,baseline=(Path(d)/n for n in ('rock.cdae','plain.cdae','baseline.cdae'))
            from unittest.mock import patch
            import cliffsettings
            ff.write_built(str(rock),b,textured=True,cliff_frame=self.frame)
            with patch.object(cliffsettings, 'SETTINGS', cliffsettings.CliffSettings(False, False)):
                ff.write_built(str(plain),b,textured=True,cliff_frame=self.frame)
            ff.write_built(str(baseline),b,textured=True)
            self.assertEqual(plain.read_bytes(),baseline.read_bytes(),
                             'off preserves every terrain triangle, photo UV and detail channel')
            self.assertNotEqual(rock.read_bytes(),plain.read_bytes())
            names=[m['name'] for m in cdae.read(plain)['materials']]
            self.assertFalse(any('cliff' in n for n in names))

    def test_near_canyon_retains_roadcut_family(self):
        x=np.arange(17)*4.
        h=np.broadcast_to(2*x,(17,17)).copy()
        keep=np.ones((16,16),bool);cuts=np.zeros_like(keep);cuts[:,:8]=True
        mesh=cr.mesh(h,keep,x,x,np.zeros(keep.shape,int),(0,0,64,64),(0,0,0),
                     cuts=cuts,natural_family=cm.CANYON)
        self.assertEqual(set(mesh.tri_materials),{cm.CANYON,cm.LIMESTONE_CUT})
        canyon=np.unique(mesh.tris[mesh.tri_materials==cm.CANYON])
        interior=canyon[mesh.uvs2[canyon,0]<.04]
        self.assertGreater(len(interior),0)
        self.assertTrue((mesh.colors[interior,:3]==137).all())
        self.assertTrue((mesh.colors[canyon,3]==255).all())

    def test_canyon_family_does_not_rescale_roadcut_displacement(self):
        x=np.arange(17)*4.
        h=np.broadcast_to(2*x,(17,17)).copy()
        keep=np.ones((16,16),bool)
        for biome in (0,1):
            biomes=np.full(keep.shape,biome)
            old=cr.displaced_surface(h,keep,x,x,biomes,cuts=keep)
            canyon=cr.displaced_surface(h,keep,x,x,biomes,cuts=keep,natural_family=cm.CANYON)
            for expected,actual in zip(old,canyon):
                np.testing.assert_allclose(actual,expected,atol=1e-12)

    def test_uncached_far_landcover_does_not_queue_downloads(self):
        import surface
        from unittest.mock import Mock
        sampler=surface.Surface.__new__(surface.Surface)
        sampler.md=Mock()
        sampler.md.has.return_value=False
        sampler._queue=Mock(side_effect=AssertionError('unexpected download'))
        classes,complete=sampler.classes(np.array([36.1]),np.array([-112.1]),fetch=False)
        self.assertFalse(complete)
        self.assertEqual(classes.tolist(),[0])
        sampler.md.cell.assert_not_called()
        sampler._queue.assert_not_called()

    def test_canyon_near_has_satellite_hue_without_photo_shadows(self):
        import cliffcolors
        biome=np.array([1,1])
        colors=cliffcolors.canyon_tint(biome,np.array([[140,110,80],[160,155,150]]))
        self.assertFalse(np.array_equal(colors[0],colors[1]))
        shaded=cliffcolors.canyon_tint(biome,np.array([[70,55,40],[140,110,80]]))
        np.testing.assert_array_equal(shaded[0],shaded[1])
        np.testing.assert_array_equal(cliffcolors.canyon_tint(biome,np.zeros((2,3))),
                                      cliffcolors.canyon_tint(biome))

    def test_canyon_near_matches_sandy_hue_without_losing_red_regions_or_exposure(self):
        import cliffcolors as cc
        weights=np.array((.2126,.7152,.0722))
        photo=np.array([[160,150,125],[160,155,150],[170,120,105]],float)
        reference=cc.linear(cm.CANYON_MEDIAN_SRGB)*cm.CANYON_COLOR_FACTOR
        colors=cc.canyon_tint(np.ones(3),photo)
        rendered=reference*cc.linear(colors[:,:3]/255)*cm.SATELLITE_HEADROOM
        lum=photo@weights
        stone=(.7*photo+.3*lum[:,None])*(155/lum)[:,None]
        desired=cc.linear(stone/255)
        old=reference*cc.linear(cc.material_tint(desired,reference,.45,hue_amount=.30)[:,:3]/255)*cm.SATELLITE_HEADROOM
        chroma=lambda rgb: rgb/(rgb@weights)[:,None]
        error=np.linalg.norm(chroma(rendered)-chroma(desired),axis=1)
        old_error=np.linalg.norm(chroma(old)-chroma(desired),axis=1)
        self.assertTrue(np.all(error<old_error*.4),'sand must lose the fixed red cast')
        np.testing.assert_allclose(rendered@weights,desired@weights,atol=.004)
        self.assertTrue(np.all(rendered@weights > (old@weights)*1.4),
                        "authored dark albedo must not suppress mineral brightness")
        self.assertGreater(rendered[2,0]/rendered[2,1],1.5,'red imagery should retain warm rock')
        # Missing/vegetated/cool imagery must retain the neutral fallback tint.
        invalid=np.array([[0,0,0],[70,140,65],[70,80,130]])
        np.testing.assert_array_equal(cc.canyon_tint(np.ones(3),invalid),cc.canyon_tint(np.ones(3)))

    def test_near_mesh_actually_uses_canyon_color_field(self):
        x=np.arange(17)*4.
        h=np.broadcast_to(2*x,(17,17)).copy()
        keep=np.ones((16,16),bool)
        photo=np.empty((16,16,3));photo[:,:8]=[180,130,95];photo[:,8:]=[150,145,135]
        mesh=cr.mesh(h,keep,x,x,np.ones(keep.shape,int),(0,0,64,64),(0,0,0),
                     rgb=photo,natural_family=cm.CANYON)
        self.assertGreater(len(np.unique(mesh.colors[:,:3],axis=0)),1)

    def test_far_photo_retains_shadows_and_correct_north_south_mapping(self):
        from PIL import Image
        import cliffcolors
        photo=np.empty((128,128,3),np.uint8)
        photo[:64]=[170,145,120];photo[64:]=[45,50,60]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'photo.png';Image.fromarray(photo).save(path)
            sample=fc.terrain_colors(path,np.array([[.5,.9],[.5,.1]]),4096)
        self.assertGreater(sample[0].mean(),sample[1].mean()*3)
        self.assertGreater(sample[0,0],sample[0,2])
        self.assertLess(sample[1,0],sample[1,2],'blue shade is not rejected as near mineral tint would be')
        ref=cliffcolors.linear(cm.CANYON_MEDIAN_SRGB)*cm.CANYON_COLOR_FACTOR
        tint=cliffcolors.material_tint(sample,ref,fc.PHOTO_BLEND)
        self.assertGreater(cliffcolors.linear(tint[0,:3]/255).mean(),cliffcolors.linear(tint[1,:3]/255).mean()*2)

    def test_tint_encoding_matches_engine_srgb_vertex_decode(self):
        import cliffcolors
        reference=np.array([.25,.14,.10])
        photo=np.array([[.31,.29,.26],[.025,.03,.05]])
        colors=cliffcolors.material_tint(photo,reference,.9)
        shader=cliffcolors.linear(colors[:,:3]/255)*cm.SATELLITE_HEADROOM*reference
        np.testing.assert_allclose(shader,.1*reference+.9*photo,atol=.003)
        fallback=cliffcolors.material_tint(None,reference,.9,count=2)
        np.testing.assert_allclose(cliffcolors.linear(fallback[:,:3]/255)*cm.SATELLITE_HEADROOM,1,atol=.006)

    def test_far_detail_keeps_satellite_mean_color_and_shadow_contrast(self):
        from PIL import Image
        import cliffcolors
        root=Path(__file__).resolve().parents[2]/'mod/beamng_earth'
        mats=json.loads((root/'levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json').read_text())
        for name in fc.MATERIALS:
            stage=mats[name]['Stages'][0]
            with Image.open(texture_source(stage['detailMap'])) as im:
                pixels=np.asarray(im.convert('RGB'))
                stride=max(1,im.width//512)
                detail=cliffcolors.linear(pixels[::stride,::stride].astype(float)/255)
            modulation=np.clip(1+(2*detail-1)*stage['detailBaseColorMapStrength'],0,1)
            factor=cliffcolors.linear(stage['baseColorFactor'][:3])
            if 'canyon' in name:
                relative=modulation/modulation.mean(axis=(0,1))
                luminance=relative@np.array([.2126,.7152,.0722])
                # The earlier low-strength detail blend preserved the mean but
                # erased the beds. Require visible contrast across whole rows,
                # not just grain that disappears in distant mip levels.
                bands=luminance.mean(axis=1)
                self.assertGreater(np.percentile(bands,90)-np.percentile(bands,10),.22)
                self.assertGreater(luminance.std(),.16)
            for photo in ([170,165,160],[75,65,55],[100,115,125]):
                light=cliffcolors.linear(np.array(photo)/255)
                color=cliffcolors.material_tint(light[None,:],np.ones(3),1)[0,:3]/255
                result=modulation*factor*cliffcolors.linear(color)
                np.testing.assert_allclose(result.mean(axis=(0,1)),light,atol=.006)
                relative=result/np.maximum(result.mean(axis=(0,1)),1e-6)
                chroma=relative/np.maximum(relative.mean(axis=-1,keepdims=True),1e-6)
                # The selected canyon material intentionally has distinct mineral
                # colors by bed; allow that local variation while mean RGB above
                # must still match the satellite. Other rock stays restrained.
                limit=.25 if 'canyon' in name else .10
                self.assertLess(np.percentile(np.abs(chroma-1),95),limit)

    def test_cliff_boundary_uses_exact_photo_instead_of_blurred_interior(self):
        from PIL import Image
        from unittest.mock import patch
        import cliffcolors
        b=built()
        labels=fc.classify(b,self.frame)
        centers=b.positions[b.tris].mean(axis=1)
        labels[centers[:,0]<64]=-1
        face_z=np.cross(b.positions[b.tris[:,1]]-b.positions[b.tris[:,0]],
                        b.positions[b.tris[:,2]]-b.positions[b.tris[:,0]])[:,2]
        boundary=np.intersect1d(np.unique(b.tris[labels>=0]),
                               np.unique(b.tris[(labels<0)&(face_z>1e-5)]))
        photo=np.full((128,128,3),80,np.uint8)
        photo[:,60:69]=[180,160,135]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'boundary.png';Image.fromarray(photo).save(path)
            with patch.object(fc,'classify',return_value=labels):
                rock=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain',terrain_texture=path)
            exact=fc.terrain_colors(path,b.planar[boundary],b.size,blur_m=0)
            blurred=fc.terrain_colors(path,b.planar[boundary],b.size)
        self.assertGreater(np.max(exact-blurred),.1,'fixture needs a visible edge-color difference')
        walls=np.unique(rock['tris'][rock['tri_materials']==2])
        for original,want in zip(boundary,exact):
            selected=walls[np.all(np.isclose(rock['positions'][walls],b.positions[original]),axis=1)]
            self.assertGreater(len(selected),0)
            actual=cliffcolors.linear(rock['colors'][selected,:3]/255)*cm.SATELLITE_HEADROOM
            actual*=fc.canyon_detail_gain(rock['uvs'][selected])
            np.testing.assert_allclose(actual,np.broadcast_to(want,actual.shape),atol=.009)

    def test_edge_compensation_keeps_interior_layering_and_tile_phase(self):
        from PIL import Image
        from unittest.mock import patch
        import cliffcolors
        b=built()
        labels=fc.classify(b,self.frame)
        labels[b.positions[b.tris].mean(axis=1)[:,0]<64]=-1
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'photo.png';Image.new('RGB',(128,128),(160,140,125)).save(path)
            with patch.object(fc,'classify',return_value=labels):
                rock=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain',terrain_texture=path)
        walls=np.unique(rock['tris'][rock['tri_materials']==2])
        interior=walls[rock['positions'][walls,0]>96]
        want=cliffcolors.linear(np.array([160,140,125])/255)
        actual=cliffcolors.linear(rock['colors'][interior,:3]/255)*cm.SATELLITE_HEADROOM
        np.testing.assert_allclose(actual,np.broadcast_to(want,actual.shape),atol=.006)
        uv=rock['uvs'][walls]
        np.testing.assert_allclose(fc.canyon_detail_gain(uv),fc.canyon_detail_gain(uv+[5,-8]),atol=1e-10)
        np.testing.assert_array_equal(rock['normals'][walls],b.normals[:1].repeat(len(walls),axis=0))

    def test_canyon_runtime_normal_is_complete_cooked_bc5(self):
        import struct
        import modtextures
        from PIL import Image
        entries = modtextures.manifest()
        path = 'levels/beamng_earth/art/shapes/beamngEarth/cliff/canyon_far.normal.dds'
        entry = entries[path]
        self.assertEqual((entry['format'], entry['role']), ('BC5', 'normal'))
        with Image.open(texture_source(path)) as im:
            width, height = im.size
        with tempfile.TemporaryDirectory() as directory:
            todo = modtextures.plan(entries={path: entry}, env={'BNE_CLIFFS': '1'})
            modtextures.build(directory, todo, threads=1)
            data = (Path(directory)/path).read_bytes()
        self.assertEqual(data[:4], b'DDS ')
        self.assertEqual(data[84:88], b'DX10')
        self.assertEqual(struct.unpack_from('<I', data, 128)[0], 83, 'BC5_UNORM')
        self.assertEqual(struct.unpack_from('<II', data, 12), (height, width))
        levels = max(width, height).bit_length()
        self.assertEqual(struct.unpack_from('<I', data, 28)[0], levels)
        expected = 148 + sum(max(1, ((width >> level)+3)//4) *
                             max(1, ((height >> level)+3)//4)*16 for level in range(levels))
        self.assertEqual(len(data), expected, 'all installed mip payloads must be present')

    def test_far_normal_is_unit_length_and_preserves_detail_in_mips(self):
        from PIL import Image
        path = texture_source('levels/beamng_earth/art/shapes/beamngEarth/cliff/canyon_far.normal.dds')
        with Image.open(path) as im:
            n=np.asarray(im,dtype=float)/127.5-1
            mip=np.asarray(im.resize((32,32),Image.Resampling.BOX),dtype=float)/127.5-1
        np.testing.assert_allclose(np.linalg.norm(n,axis=-1),1,atol=.007)
        self.assertGreater(np.sqrt(np.mean(mip[:,:,:2]**2)),.12)
        self.assertLess(np.sqrt(np.mean(n[:,:,:2]**2)),.45,'avoid noisy overstrong relief')

    def test_far_macro_blur_removes_pixel_details_not_broad_regions(self):
        from PIL import Image
        chess=(np.indices((128,128)).sum(axis=0)%2)*100+80
        photo=np.repeat(chess[:,:,None],3,axis=2).astype(np.uint8)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'photo.png';Image.fromarray(photo).save(path)
            uv=np.column_stack((np.linspace(.3,.7,80),np.full(80,.5)))
            sampled=fc.terrain_colors(path,uv,1024)
        self.assertLess(float(np.ptp(sampled)),.01)

    def test_far_tint_changes_only_cliffs_and_survives_cdae(self):
        from PIL import Image
        b=built()
        with tempfile.TemporaryDirectory() as d:
            texture=Path(d)/'photo.png'
            photo=np.zeros((128,128,3),np.uint8)
            photo[:,:64]=[175,140,115];photo[:,64:]=[55,50,48]
            Image.fromarray(photo).save(texture)
            rock=fc.geometry(b,self.frame,b.planar,b.detail_uvs,'terrain',terrain_texture=texture)
            terrain=np.unique(rock['tris'][rock['tri_materials']==0])
            walls=np.unique(rock['tris'][rock['tri_materials']==2])
            self.assertTrue((rock['colors'][terrain]==255).all())
            self.assertGreater(np.ptp(rock['colors'][walls,0]),10)
            path=Path(d)/'photo-cliff.cdae'
            ff.write_built(str(path),b,textured=True,cliff_frame=self.frame,cliff_texture=texture)
            shape=cdae.read(path)
            self.assertEqual(sum(len(cdae.mesh_arrays(m)['indices'])//3 for m in shape['meshes'] if m['type']!=4),len(b.tris))

    def test_material_assets_and_height_are_packaged(self):
        from PIL import Image
        root=Path(__file__).resolve().parents[2]
        mod=root/'mod/beamng_earth'
        mats=json.loads((mod/'levels/beamng_earth/art/shapes/beamngEarth/cliff_materials.src.json').read_text())
        for name in (cm.MATERIALS[cm.CANYON],*fc.MATERIALS):
            stage=mats[name]['Stages'][0]
            import cliffcolors
            if name.startswith('bne_far_'):
                self.assertNotIn('baseColorMap',stage,'satellite vertex color supplies the base')
                self.assertEqual(stage['detailMapUseUV'],0)
            else:
                self.assertIn('baseColorMap',stage,'near cliffs retain full authored color detail')
                self.assertIn('detailMap',stage,'near cliffs add grain independently of the broad beds')
                for channel in ('baseColorMap', 'normalMap', 'roughnessMap', 'ambientOcclusionMap'):
                    with Image.open(texture_source(stage[channel])) as im:
                        self.assertEqual(im.size, (2048,2048) if channel == "baseColorMap" else (1024,1024), channel)

            for key,value in stage.items():
                if key.endswith('Map'):
                    if value.startswith('/temp/'):
                        self.assertTrue(value.startswith('/temp/beamngEarth/shapes/cliff_ground_v1/'))
                        continue  # private copy of an installed stock texture, not distributable
                    with Image.open(texture_source(value)) as im:
                        self.assertGreater(im.width,100)
                        for dimension in im.size:
                            self.assertGreaterEqual(dimension,16)
                            self.assertEqual(dimension & (dimension-1),0,
                                f"BeamNG TextureCooker requires power-of-two textures: {value} {im.size}")
            if name.startswith('bne_far_'):
                self.assertIn('normalMap',stage,'distant rock needs broad normal relief')
                self.assertEqual(stage['normalMapUseUV'],1,'normal scale must not resize the albedo')
                self.assertNotIn('detailNormalMap',stage,'one shared normal sample, no micro-normal layer')
        self.assertIn('cliff_canyon_height.png',(root/'tools/build_app.py').read_text())
        import revisit
        with tempfile.TemporaryDirectory() as directory:
            height = Path(directory)/'cliff_canyon_height.png'
            height.write_bytes(b'first relief')
            from cliffsettings import geometry_identity
            before = geometry_identity(directory)
            height.write_bytes(b'changed relief')
            self.assertNotEqual(before, geometry_identity(directory), 'relief changes invalidate cached geometry')
        self.assertTrue(np.isfinite(cm.sample(np.array([0,.5,1]),np.array([0,.5,1]),family=cm.CANYON)).all())

if __name__=='__main__': unittest.main()
