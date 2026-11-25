"""Cliff LOD reduction must preserve boundaries, charts and disconnected faces."""
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cdae, clifflod
from cliffrocks import Mesh


def surface(n=49):
    y,x=np.mgrid[:n,:n]*.5
    p=np.stack((x,y,y*2+.1*np.sin(x)),axis=-1).reshape(-1,3)
    i=np.arange(n*n).reshape(n,n)
    a,b,c,d=i[:-1,:-1].ravel(),i[:-1,1:].ravel(),i[1:,:-1].ravel(),i[1:,1:].ravel()
    t=np.concatenate((np.stack((a,b,c),1),np.stack((b,d,c),1)))
    colors=np.full((len(p),4),180,np.uint8); colors[:,3]=255
    material=np.where(p[t].mean(axis=1)[:,0]<n*.25,0,2)
    return Mesh(p,t,p[:,:2]/91.44,colors,cdae.face_normal_sum(p,t),tri_materials=material)


def boundary(p,t):
    e,c=np.unique(clifflod.edges(t),axis=0,return_counts=True)
    return {tuple(v) for v in p[np.unique(e[c==1])]},c.max()


class CliffLodTest(unittest.TestCase):
    def test_serialized_lod_keeps_cliff_shading_and_second_uv_channels(self):
        rock = surface(3)
        channels = dict(uvs=rock.uvs, uvs2=rock.uvs*3,
                        tangent_uvs=rock.uvs[:, ::-1].copy(), colors=rock.colors,
                        normals=np.tile([.6, 0., .8], (len(rock.positions), 1)))
        coarse = dict(positions=rock.positions, tris=rock.tris, **channels)
        shape = cdae.decode(cdae.encode(rock.positions, rock.tris, **channels,
                                       lods=[(100., coarse)]))
        fine, lod = [cdae.mesh_arrays(m) for m in shape['meshes']]
        for field in ('verts', 'norms', 'tangents', 'tverts', 'tverts2'):
            np.testing.assert_array_equal(lod[field], fine[field], err_msg=field)
        self.assertEqual(shape['meshes'][0]['colors'], shape['meshes'][1]['colors'])

    def test_native_retry_exhaustion_retains_source_and_finer_mapping(self):
        rock = surface(25)
        prepared = clifflod._prepare(rock)
        initial = np.arange(len(rock.positions), dtype=np.int64)
        coarse = clifflod.simplify(rock, 1.5, prepared, initial)
        self.assertLess(len(coarse['tris']), len(rock.tris))
        baseline = prepared['accepted_labels']
        calls = []
        def missing_terrain(points):
            calls.append(len(points))
            return np.full(len(points), np.nan)
        rock.lod_ground = missing_terrain
        retained = clifflod.simplify(rock, 3.0, prepared, baseline)
        # Even the previously accepted grouping is now unsafe. Never return a
        # partially validated trial or overwrite that grouping on exhaustion.
        self.assertIs(prepared['accepted_labels'], baseline)
        for key in ('positions', 'tris', 'uvs', 'colors', 'normals'):
            np.testing.assert_array_equal(retained[key], getattr(rock, key))
        self.assertEqual(len(calls), 7+8*7)
        self.assertEqual(len(prepared['clearance']), 7)

    def test_native_face_clearance_catches_interior_ridge_and_reuses_cache(self):
        import fast
        source = np.array([[0, 0, 2], [2, 0, 2], [0, 2, 2]], dtype=float)
        trial = source.copy(); trial[:, 2] = 0
        triangles = np.array([[0, 1, 2]], np.int64)
        changed = np.array([True])
        cache = {}
        calls = []
        def ground(points):
            calls.append(len(points))
            return 1-((points[:, 0]-1)**2+points[:, 1]**2)
        # All three trial vertices are clear, but the first edge crosses a ridge.
        self.assertTrue(np.all(trial[:, 2]-ground(trial) >= 0))
        calls.clear()
        bad = fast._ef.cliff_lod_clearance(source, triangles, trial, triangles, changed, ground, cache, .03)
        np.testing.assert_array_equal(bad, [True])
        self.assertEqual(len(calls), 14)
        self.assertEqual(len(cache), 7)
        saved = dict(cache)
        calls.clear()
        safe = fast._ef.cliff_lod_clearance(source, triangles, source, triangles, changed, ground, cache, .03)
        np.testing.assert_array_equal(safe, [False])
        self.assertEqual(len(calls), 7)
        for key, value in saved.items():
            self.assertIs(cache[key], value)
        missing = lambda points: np.full(len(points), np.nan)
        np.testing.assert_array_equal(fast._ef.cliff_lod_clearance(source, triangles, source,
            triangles, changed, missing, cache, .03), [True])
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_clearance(source, triangles, trial, triangles, changed,
                lambda points: np.empty(2), {}, .03)
        wrong_cache = {key: np.empty(2) for key in cache}
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_clearance(source, triangles, trial, triangles, changed, ground, wrong_cache, .03)
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_clearance(source, triangles-1, trial, triangles, changed, ground, {}, .03)

    def test_native_output_keeps_channels_aligned_and_rounds_color_ties(self):
        import fast
        mapping = np.array([0, 0, 1, 2, 3, 4, 4], np.int64)
        pos = np.arange(15, dtype=float).reshape(5, 3)
        tri = np.array([[2, 0, 1], [3, 3, 4]], np.int64)
        keep = np.array([True, False])
        uv = np.arange(14, dtype=float).reshape(7, 2)
        uv2 = uv+20
        color = np.full((7, 4), 255., dtype=float)
        color[0, :3], color[1, :3] = [0, 1, 2], [1, 2, 3]
        normal = np.array([[1, 0, 0], [-1, 0, 0], [0, 3, 4], [2, 0, 0],
                           [0, 0, 1], [1, 0, 0], [1, 0, 0]], dtype=float)
        material = np.array([2, 1], np.int32)
        for blend in (False, True):
            result = fast._ef.cliff_lod_output(pos, tri, keep, mapping, uv, color, material, blend, normal, uv2)
            np.testing.assert_array_equal(result['positions'], pos[:3])
            np.testing.assert_array_equal(result['tris'], tri[:1])
            np.testing.assert_array_equal(result['tri_materials'], [2])
            np.testing.assert_array_equal(result['uvs'], [[1, 2], [4, 5], [6, 7]])
            np.testing.assert_array_equal(result['uvs2'], result['uvs']+20)
            np.testing.assert_array_equal(result['tangent_uvs'], result['uvs'] if blend else result['uvs2'])
            np.testing.assert_array_equal(result['colors'][0], [0, 2, 2, 255])
            self.assertEqual(result['colors'].dtype, np.uint8)
            np.testing.assert_array_equal(result['normals'], [[0, 0, 0], [0, .6, .8], [1, 0, 0]])
        result = fast._ef.cliff_lod_output(pos, tri, np.zeros(2, bool), mapping, uv, color, material, False, None, None)
        self.assertEqual(result['positions'].shape, (0, 3))
        self.assertEqual(result['colors'].shape, (0, 4))
        self.assertIsNone(result['normals'])
        self.assertIsNone(result['uvs2'])
        self.assertIsNone(result['tangent_uvs'])
        for invalid in (color[:, :3], color-256, color+np.nan):
            with self.assertRaises(ValueError):
                fast._ef.cliff_lod_output(pos, tri, keep, mapping, uv, invalid, material, False, None, None)
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_output(pos, tri, keep, mapping*0, uv, color, material, False, None, None)

    def test_native_restore_reverts_whole_clusters_to_finer_baseline(self):
        import fast
        rng = np.random.default_rng(625)
        for _ in range(30):
            mapping = rng.integers(0, 12, 40, dtype=np.int64)
            labels = mapping*2
            baseline = np.arange(40, dtype=np.int64)
            tri = rng.integers(0, 12, (20, 3), dtype=np.int64)
            bad = rng.random(20) < .2
            expected = labels.copy()
            restore = np.isin(mapping, np.unique(tri[bad]))
            expected[restore] = baseline[restore]
            actual = fast._ef.cliff_lod_restore(tri, bad, mapping, labels, baseline)
            np.testing.assert_array_equal(actual, expected)
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_restore(tri, bad[:-1], mapping, labels, baseline)
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_restore(tri-50, bad, mapping, labels, baseline)

    def test_native_terrain_sampler_matches_geodesy_and_missing_data(self):
        import fast
        from geotiles import WorldFrame
        from cliffrocks import _height_at, WORLD_M_PER_DEG
        rng = np.random.default_rng(712)
        for latitude, longitude in ((30.4, -99.7), (36.0, -114.7), (12.0, 179.999), (-25.0, -179.999)):
            frame = WorldFrame(latitude, longitude)
            dx, dy = 3.5, 4.0
            x0, y0 = longitude*WORLD_M_PER_DEG-35, latitude*WORLD_M_PER_DEG-40
            heights = rng.uniform(100, 110, (23, 25))
            heights[5, 7] = np.nan
            # Include outside-grid and missing cells, with a nonzero tile origin.
            xy = np.vstack((rng.uniform(-2, 26, (80, 2)), [[7.2, 5.1], [0, 0], [24, 22]]))
            lat = (y0+xy[:, 1]*dy)/WORLD_M_PER_DEG
            lon = (x0+xy[:, 0]*dx)/WORLD_M_PER_DEG
            world = np.column_stack(frame.geodetic_to_enu(lat, lon, rng.uniform(100, 120, len(lat))))
            offset = (500.0, -2000.0)
            points = world-np.array((*offset, 0.0))
            la, lo, _ = frame.enu_to_geodetic(*world.T)
            lo = frame.lon0+(lo-frame.lon0+180)%360-180
            sampled = _height_at(heights, (lo*WORLD_M_PER_DEG-x0)/dx, (la*WORLD_M_PER_DEG-y0)/dy)
            expected = frame.geodetic_to_enu(la, lo, sampled)[2]
            actual = fast._ef.cliff_lod_ground(points, heights, frame.origin, frame.rot,
                offset, (x0, y0, dx, dy), frame.lon0)
            np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-8, equal_nan=True)
            self.assertTrue(np.isnan(actual).any())
        for grid in ((0, 0, 0, 1), (0, 0, 1, -1), (np.nan, 0, 1, 1)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_lod_ground(points, heights, frame.origin, frame.rot, offset, grid, frame.lon0)
        self.assertEqual(fast._ef.cliff_lod_ground(np.empty((0, 3)), heights,
            frame.origin, frame.rot, offset, (x0, y0, dx, dy), frame.lon0).shape, (0,))

    def test_reduction_preserves_outline_material_seam_and_chart(self):
        rock=surface()
        levels=clifflod.levels(rock)
        self.assertGreaterEqual(len(levels),2)
        for _,coarse in levels:
            self.assertLess(len(coarse['tris']),len(rock.tris)*.75)
            np.testing.assert_allclose(coarse['uvs'],coarse['positions'][:,:2]/91.44,atol=1e-12)
            self.assertEqual(set(coarse['tri_materials']),{0,2})
            self.assertTrue(np.isfinite(coarse['normals']).all())
            b,count=boundary(coarse['positions'],coarse['tris'])
            self.assertEqual(b,boundary(rock.positions,rock.tris)[0])
            self.assertLessEqual(count,2)

    def test_curved_terrain_does_not_punch_through_lods(self):
        rock = surface(65)
        rock.tri_materials = None
        rock.lod_ground = lambda p: 2*p[:,1]+2*np.cos(.6*p[:,0])
        rock.positions[:,2] = rock.lod_ground(rock.positions)+.35
        rock.normals = cdae.face_normal_sum(rock.positions, rock.tris)
        levels = clifflod.levels(rock)
        self.assertTrue(levels)
        self.assertLess(len(levels[0][1]['tris']), len(rock.tris)*.6)
        for _, coarse in levels:
            vertices = coarse['positions'][coarse['tris']]
            # Independent, denser barycentric probes than the reducer uses.
            for a in np.linspace(0,1,9):
                for b in np.linspace(0,1-a,9):
                    q = vertices[:,0]*a+vertices[:,1]*b+vertices[:,2]*(1-a-b)
                    gap = q[:,2]-rock.lod_ground(q)
                    self.assertGreater(gap.min(), -.04)
            self.assertEqual(boundary(coarse['positions'],coarse['tris'])[0],
                             boundary(rock.positions,rock.tris)[0])

    def test_opposing_faces_and_holes_never_weld(self):
        rock=surface(35)
        center=rock.positions[rock.tris].mean(axis=1)
        keep=~((center[:,0]>5)&(center[:,0]<9)&(center[:,1]>5)&(center[:,1]<9))
        rock.tris=rock.tris[keep]; rock.tri_materials=rock.tri_materials[keep]
        p=rock.positions.copy(); p[:,2]+=.2
        paired=Mesh(np.concatenate((rock.positions,p)),np.concatenate((rock.tris,rock.tris[:,::-1]+len(p))),
                    np.tile(rock.uvs,(2,1)),np.tile(rock.colors,(2,1)),
                    np.concatenate((rock.normals,-rock.normals)),tri_materials=np.tile(rock.tri_materials,2))
        coarse=clifflod.simplify(paired,6)
        self.assertLess(len(coarse['tris']),len(paired.tris))
        self.assertEqual(boundary(paired.positions,paired.tris)[0],boundary(coarse['positions'],coarse['tris'])[0])
        nn=coarse['normals'][coarse['tris']]
        self.assertTrue(((nn[:,:,2]>0).all(axis=1)|(nn[:,:,2]<0).all(axis=1)).all())

    def test_shared_preparation_preserves_each_lod_and_reuses_source_clearance(self):
        rock = surface(65)
        rock.uvs2 = rock.uvs*3
        rock.lod_ground = lambda p: 2*p[:, 1]+2*np.cos(.6*p[:, 0])
        rock.positions[:, 2] = rock.lod_ground(rock.positions)+.35
        rock.normals = cdae.face_normal_sum(rock.positions, rock.tris)
        prepared = clifflod._prepare(rock)
        source_arrays = None
        for _, cell in clifflod.LEVELS:
            shared = clifflod.simplify(rock, cell, prepared)
            independent = clifflod.simplify(rock, cell)
            for name, value in independent.items():
                if value is not None:
                    np.testing.assert_array_equal(shared[name], value)
            if source_arrays is None:
                source_arrays = dict(prepared['clearance'])
            else:
                for key, value in source_arrays.items():
                    self.assertIs(prepared['clearance'][key], value)
        self.assertEqual(len(source_arrays), 7)

    def test_integer_edge_grouping_matches_row_sorting(self):
        rock = surface(25)
        # Include reversed, duplicated and open triangles; all affect locks.
        tris = np.concatenate((rock.tris, rock.tris[:20, ::-1]))
        expected = np.unique(clifflod.edges(tris), axis=0, return_inverse=True, return_counts=True)
        actual = clifflod.edge_groups(tris, len(rock.positions))
        for old, new in zip(expected, actual):
            np.testing.assert_array_equal(old, new)

    def test_unchanged_sliver_does_not_discard_valid_lods(self):
        rock = surface()
        # A disconnected clipped sliver has locked vertices and cannot change.
        # It must survive, without vetoing reductions elsewhere in the shape.
        sliver = np.array([[40., 0., 0.], [40.000001, 0., 0.], [40., .000001, 0.]])
        rock.tris = np.vstack((rock.tris, np.arange(3)+len(rock.positions)))
        rock.positions = np.vstack((rock.positions, sliver))
        rock.uvs = np.vstack((rock.uvs, sliver[:, :2]/91.44))
        rock.colors = np.vstack((rock.colors, np.full((3, 4), 255, np.uint8)))
        rock.normals = cdae.face_normal_sum(rock.positions, rock.tris)
        rock.tri_materials = np.append(rock.tri_materials, 0)
        for terrain in (False, True):
            rock.lod_ground = (lambda p: p[:, 1]*2-.5) if terrain else None
            levels = clifflod.levels(rock)
            self.assertGreaterEqual(len(levels), 2)
            for _, coarse in levels:
                self.assertLess(len(coarse['tris']), len(rock.tris)*.75)
                self.assertEqual(boundary(coarse['positions'], coarse['tris'])[0],
                                 boundary(rock.positions, rock.tris)[0])
                for v in sliver:
                    self.assertTrue(np.any(np.all(coarse['positions'] == v, axis=1)))

    def test_rejected_coarse_clusters_retain_valid_finer_reduction(self):
        rock = surface()
        rock.positions[:, 2] += np.cos(.6*rock.positions[:, 0])
        rock.normals = cdae.face_normal_sum(rock.positions, rock.tris)
        independent = clifflod.simplify(rock, 6)
        prepared = clifflod._prepare(rock)
        fallback = np.arange(len(rock.positions))
        previous = len(rock.tris)
        for _, cell in clifflod.LEVELS:
            coarse = clifflod.simplify(rock, cell, prepared, fallback)
            fallback = prepared['accepted_labels']
            # Rejection cannot expand an already valid previous cluster.
            self.assertLessEqual(len(coarse['tris']), previous)
            previous = len(coarse['tris'])
            self.assertEqual(boundary(coarse['positions'], coarse['tris'])[0],
                             boundary(rock.positions, rock.tris)[0])
            self.assertLessEqual(boundary(coarse['positions'], coarse['tris'])[1], 2)
            np.testing.assert_array_equal(fallback[fallback], fallback)
        self.assertLess(previous, len(independent['tris'])*.5)

    def test_native_retreat_checks_faces_and_source_vertex_normals(self):
        import fast
        p = np.array([[0.,0.,.2],[1.,0.,.2],[0.,1.,.2]])
        t = np.array([[0,1,2]],np.int64)
        mapping = np.arange(3,dtype=np.int64)
        absent = np.empty((0,3))
        reduced = p.copy(); reduced[:,2] = 0.
        self.assertTrue(fast._ef.cliff_lod_retreat(p,t,reduced,mapping,absent,.12)[0])
        self.assertFalse(fast._ef.cliff_lod_retreat(p,t,reduced,mapping,absent,.21)[0])
        reduced[:,2] = .4  # moving out from the source is not inward retreat
        self.assertFalse(fast._ef.cliff_lod_retreat(p,t,reduced,mapping,absent,.12)[0])
        reduced = p.copy(); reduced[:,0] -= .2
        normals = np.tile([1.,0.,0.],(3,1))
        self.assertFalse(fast._ef.cliff_lod_retreat(p,t,reduced,mapping,absent,.12)[0])
        self.assertTrue(fast._ef.cliff_lod_retreat(p,t,reduced,mapping,normals,.12)[0])
        for tri,mp,n,limit in ((t+3,mapping,normals,.12),(t,mapping+3,normals,.12),
                              (t,mapping,normals[:2],.12),(t,mapping,normals,-.1)):
            with self.assertRaises(ValueError): fast._ef.cliff_lod_retreat(p,tri,reduced,mp,n,limit)
        self.assertEqual(fast._ef.cliff_lod_retreat(np.empty((0,3)),np.empty((0,3),np.int64),
            np.empty((0,3)),np.empty(0,np.int64),absent,.12).shape,(0,))

    def test_native_validation_rejects_defects_without_vetoing_unchanged_slivers(self):
        import fast
        triangles = np.array([[0,1,2],[0,2,1],[1,2,3],[1,2,4],
                              [5,6,7],[8,9,10],[11,12,13],[11,11,12]],np.int64)
        weights = np.full(14,2,np.int64); weights[8:11] = 1
        normal = np.tile([0.,0.,1.],(8,1)); source = normal.copy()
        normal[4,2] = -1.  # folded face, disconnected from the bad topology
        normal[5] = 0.    # untouched source sliver is not a new defect
        changed,bad = fast._ef.cliff_lod_validate(triangles,weights,normal,source,.15)
        np.testing.assert_array_equal(changed,[True,True,True,True,True,False,True,False])
        np.testing.assert_array_equal(bad,[True,True,True,True,True,False,False,False])
        # Even an otherwise valid neighboring face must be rejected when it
        # touches a vertex of an edge shared by more than two faces.
        triangles[6] = [1,12,13]
        _,bad = fast._ef.cliff_lod_validate(triangles,weights,normal,source,.15)
        self.assertTrue(bad[6])
        for t,w,n,s,c in ((triangles+14,weights,normal,source,.15),
                        (triangles,weights*0,normal,source,.15),
                        (triangles,weights,normal[:-1],source,.15),
                        (triangles,weights,normal,source,float('nan'))):
            with self.assertRaises(ValueError): fast._ef.cliff_lod_validate(t,w,n,s,c)
        empty = fast._ef.cliff_lod_validate(np.empty((0,3),np.int64),np.empty(0,np.int64),
                                          np.empty((0,3)),np.empty((0,3)),.15)
        self.assertEqual([v.shape for v in empty],[(0,),(0,)])

    def test_native_trial_mapping_and_attribute_sums_match_source_order(self):
        import fast
        rng = np.random.default_rng(23)
        for n in (1, 7, 40):
            p = rng.normal(size=(n,3))
            t = rng.integers(0,n,(20,3),dtype=np.int64)
            labels = rng.integers(0,n,n,dtype=np.int64)
            _, expected_map = np.unique(labels,return_inverse=True)
            expected_weights = np.bincount(expected_map)
            expected_pos = np.column_stack([np.bincount(expected_map,weights=p[:,c]) for c in range(3)])
            expected_pos /= expected_weights[:,None]
            m,w,pos,tri,keep,normal = fast._ef.cliff_lod_trial(p,t,labels)
            np.testing.assert_array_equal(m,expected_map)
            np.testing.assert_array_equal(w,expected_weights)
            np.testing.assert_array_equal(pos,expected_pos)
            np.testing.assert_array_equal(tri,expected_map[t])
            np.testing.assert_array_equal(keep,(tri[:,0]!=tri[:,1]) & (tri[:,1]!=tri[:,2]) & (tri[:,0]!=tri[:,2]))
            np.testing.assert_array_equal(normal,np.cross(pos[tri[:,1]]-pos[tri[:,0]],pos[tri[:,2]]-pos[tri[:,0]]))
            uv = rng.normal(size=(n, 2))
            normals = rng.normal(size=(n, 3))
            colors = rng.integers(0, 256, (n, 4)).astype(float)
            material = np.zeros(len(t), np.int32)
            actual = fast._ef.cliff_lod_output(pos, tri, keep, m, uv, colors, material, False, normals, None)
            used = np.unique(tri[keep])
            for key, values in (('uvs', uv), ('normals', normals), ('colors', colors)):
                expected = np.column_stack([np.bincount(m, weights=values[:, c]) for c in range(values.shape[1])])/w[:, None]
                expected = expected[used]
                if key == 'normals':
                    expected /= np.maximum(np.linalg.norm(expected, axis=1, keepdims=True), 1e-12)
                elif key == 'colors':
                    expected = np.rint(expected).astype(np.uint8)
                np.testing.assert_array_equal(actual[key], expected)
        for indices in (t+40,-t-1):
            with self.assertRaises(ValueError): fast._ef.cliff_lod_trial(p,indices,labels)
        with self.assertRaises(ValueError): fast._ef.cliff_lod_trial(p,t,labels+40)
        empty = fast._ef.cliff_lod_trial(np.empty((0,3)),np.empty((0,3),np.int64),np.empty(0,np.int64))
        self.assertEqual([a.shape for a in empty],[(0,),(0,),(0,3),(0,3),(0,),(0,3)])

    def test_native_preparation_matches_boundary_and_material_locks(self):
        import fast
        rng = np.random.default_rng(719)
        for _ in range(30):
            p = rng.normal(size=(25,3))
            t = rng.integers(0,25,(40,3),dtype=np.int64)
            material = rng.integers(0,3,40,dtype=np.int32)
            e, locked, normal = fast._ef.cliff_lod_prepare(p,t,material)
            expected_edges = {}
            for triangle, value in zip(t,material):
                for a,b in ((0,1),(1,2),(0,2)):
                    edge = tuple(sorted((triangle[a],triangle[b])))
                    expected_edges.setdefault(edge,[]).append(value)
            expected_lock = np.zeros(len(p),bool)
            for edge, values in expected_edges.items():
                if len(values)!=2 or len(set(values))!=1:
                    expected_lock[list(edge)] = True
            np.testing.assert_array_equal(e,np.array(sorted(expected_edges)))
            np.testing.assert_array_equal(locked,expected_lock)
            np.testing.assert_array_equal(normal,np.cross(p[t[:,1]]-p[t[:,0]],p[t[:,2]]-p[t[:,0]]))
        e,locked,normal = fast._ef.cliff_lod_prepare(p,np.empty((0,3),np.int64),np.empty(0,np.int32))
        self.assertEqual(e.shape,(0,2)); self.assertEqual(normal.shape,(0,3))
        self.assertFalse(locked.any())
        for positions, triangles, materials in ((p,t+25,material),(p,t,material[:-1]),
                (p,t,-material-1),(p*np.nan,t,material)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_lod_prepare(positions,triangles,materials)

    def test_native_cluster_components_and_invalid_inputs(self):
        import fast
        rng = np.random.default_rng(437)
        for _ in range(30):
            p = rng.random((40,3))*3
            e = rng.integers(0,40,(120,2),dtype=np.int64)
            locked = rng.random(40)<.15
            baseline = np.arange(40,dtype=np.int64)
            for cell in (.5, 1., 2., 4.):
                cells = np.floor((p-p.min(axis=0))/cell)
                # Independent graph traversal, including links from accepted clusters.
                graph = [set() for _ in p]
                for a,b in enumerate(baseline):
                    graph[a].add(int(b)); graph[b].add(a)
                for a,b in e:
                    if not locked[a] and not locked[b] and np.array_equal(cells[a],cells[b]):
                        graph[a].add(int(b)); graph[b].add(int(a))
                expected = np.full(40,-1,np.int64)
                for seed in range(40):
                    if expected[seed]>=0: continue
                    component, todo = set(), [seed]
                    while todo:
                        v = todo.pop()
                        if v not in component:
                            component.add(v); todo.extend(graph[v]-component)
                    expected[list(component)] = min(component)
                actual = fast._ef.cliff_lod_clusters(p,e,locked,baseline,cell)
                np.testing.assert_array_equal(actual,expected)
                baseline = actual
        for cell in (0., -1., float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                fast._ef.cliff_lod_clusters(p,e,locked,baseline,cell)
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_clusters(p,e+40,locked,baseline,1.)
        cyclic = np.arange(40,dtype=np.int64); cyclic[:2] = [1,0]
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_clusters(p,e,locked,cyclic,1.)
        empty = fast._ef.cliff_lod_clusters(np.empty((0,3)),np.empty((0,2),np.int64),
                                          np.empty(0,bool),np.empty(0,np.int64),1.)
        self.assertEqual(empty.shape,(0,))
        with self.assertRaises(ValueError):
            fast._ef.cliff_lod_edges(np.array([[0,1,2]],np.int64),2)

    def test_tiny_mesh_stays_single_detail(self):
        self.assertEqual(clifflod.levels(surface(3)),[])

if __name__ == "__main__":
    unittest.main()
