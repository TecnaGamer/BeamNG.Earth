"""Behavior checks for projected contact strips and privately generated cutouts."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cliffstrips as cs
from cliffstrip_reference import build_reference, _mask_cells, ReferenceProjector


def plane(z=0):
    p=np.array([[-20,-20,z],[20,-20,z],[20,20,z],[-20,20,z]],float)
    return p[[[0,1,2],[0,2,3]]]


class ProjectedStrips(unittest.TestCase):
    def test_contact_mapping_matches_world_transform_without_welding(self):
        import fast
        rng = np.random.default_rng(611)
        unique = rng.uniform(-500, 500, (100, 3))
        points = np.ascontiguousarray(unique[rng.integers(0, len(unique), 1200)])
        points[0] = [0., -0., 0.]
        points[1] = [1e-12, 0., 0.]
        for source, target in [((36.79, -118.31), (36.78, -118.27)),
                               ((-23., 179.999), (-23., -179.999)),
                               ((65., 12.), (65., 12.))]:
            expected = fast._ef.geo_world_map(*source, *target, points)
            actual = fast._ef.cliff_contact_map(points, source, target)
            np.testing.assert_array_equal(actual.view(np.uint64), expected.view(np.uint64))
        empty = fast._ef.cliff_contact_map(np.empty((0, 3)), source, target)
        self.assertEqual(empty.shape, (0, 3))
        for invalid in (np.zeros((2, 2)), np.full((1, 3), np.nan)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_contact_map(invalid, source, target)

    def test_native_patch_selection_preserves_colors_normals_and_budget(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import cliffstrip_reference as reference
        rng = np.random.default_rng(492)
        y,x = np.mgrid[:15,:17]
        p = np.column_stack((x.ravel()*4.,y.ravel()*4.,
                             ((x-4)*(x-12)*.2+y*.3).ravel()))
        tri = []
        for j in range(14):
            for i in range(16):
                a = j*17+i
                tri.extend(((a,a+1,a+18),(a,a+18,a+17)))
        tri = np.asarray(tri,np.int64)
        # Reversed and degenerate faces exercise the upward fallback normal.
        tri[::3] = tri[::3,::-1]
        tri = np.concatenate((tri,[[0,0,0]]))
        families = rng.integers(0,4,len(tri))
        colors = rng.integers(0,256,(len(p),4),dtype=np.uint8)
        normals = rng.normal(size=p.shape)
        normals /= np.linalg.norm(normals,axis=1,keepdims=True)
        found = False
        for ns in (None,normals):
            for tint in (colors, colors.astype(float), colors.astype(float)/255):
                for budget in (0,1,80,cs.PATCH_FACES_MAX):
                    rock = SimpleNamespace(positions=p,tris=tri,normals=ns,colors=colors,
                                           strip_colors=tint,tri_materials=families)
                    ground = lambda xy: xy[:,1]*.075
                    with patch.object(cs,'PATCH_FACES_MAX',budget), patch.object(reference,'PATCH_FACES_MAX',budget):
                        actual = cs.prepare(rock,ground)
                        expected = reference.prepare_reference(rock,ground)
                    self.assertEqual(len(actual),len(expected))
                    self.assertLessEqual(sum(len(r['faces']) for r in actual),budget)
                    found |= bool(actual)
                    for a,b in zip(actual,expected):
                        for key in ('run','faces','normals','albedo'):
                            np.testing.assert_allclose(a[key],b[key],rtol=1e-6,atol=1e-7)
        self.assertTrue(found)

    def test_native_patch_inputs_reject_invalid_indices_and_offsets(self):
        import fast
        p = np.array([[0.,0.,0.],[4.,0.,0.],[0.,4.,0.]])
        args = [p,np.array([[0,1,2]],np.int64),np.ones_like(p),np.zeros(1,np.int64),
                np.ones((1,3)),p[:2],np.array([0,2],np.int64),8.,100,False]
        for index,value in ((1,np.array([[0,-1,2]],np.int64)),(3,np.array([1],np.int64)),
                            (6,np.array([0,3],np.int64)),(6,np.array([0,0,2],np.int64)),
                            (7,float('nan'))):
            invalid = args.copy(); invalid[index] = value
            with self.assertRaises(ValueError):
                fast._ef.cliff_contact_patches(*invalid)
        with self.assertRaises(ValueError):
            fast._ef.cliff_contact_patches(*args,normals=np.ones((2,3)))

    def test_native_contact_contours_match_branches_loops_and_long_runs(self):
        from cliffstrip_reference import contact_runs as reference
        rng = np.random.default_rng(216)
        y,x = np.mgrid[:19,:13]
        p = np.column_stack((x.ravel()*4.,y.ravel()*7.,(.2*x+.3*y).ravel()))
        tris = []
        for row in range(18):
            for col in range(12):
                a = row*13+col
                tris.extend(((a,a+1,a+14),(a,a+14,a+13)))
        tris = np.array(tris,np.int64)
        fields = [x.ravel()-6.2, ((x-6)**2+(y-9)**2-25).ravel(),rng.normal(size=len(p))]
        for field in fields:
            for missing in (False,True):
                signed = np.array(field,float)
                if missing:
                    signed[80:90] = np.nan
                for faces in (tris,tris[rng.permutation(len(tris))],np.concatenate((tris,tris[:4]))):
                    expected = reference(p,faces,signed)
                    actual = cs.contact_runs(p,faces,signed)
                    self.assertEqual(len(actual),len(expected))
                    for a,b in zip(actual,expected):
                        np.testing.assert_allclose(a,b,rtol=1e-12,atol=1e-12)
                        length = np.linalg.norm(np.diff(a,axis=0),axis=1).sum()
                        self.assertGreaterEqual(length,4.-1e-10)
                        self.assertLessEqual(length,48.+1e-10)
        self.assertEqual(cs.contact_runs(p,tris,np.ones(len(p))),[])
        self.assertEqual(cs.contact_runs(p,tris,np.full(len(p),np.nan)),[])
        with self.assertRaises(ValueError):
            cs.contact_runs(p,np.array([[0,1,len(p)]]),np.ones(len(p)))
        with self.assertRaises(ValueError):
            cs.contact_runs(p,tris,np.ones(len(p)-1))
        with self.assertRaisesRegex(ValueError,'run budget'):
            cs.contact_runs(np.array([[0.,0.,0.],[1e9,0.,0.],[0.,1e9,0.]]),
                            np.array([[0,1,2]]),np.array([-1.,1.,1.]))

    def test_native_mask_matches_curved_footprints_and_caps(self):
        import fast
        rng = np.random.default_rng(184)
        projector = cs.Projector(plane(),[0,0,1],[0,0,0])
        for count in (2,5,12):
            line = np.column_stack((np.arange(count)*2.,rng.normal(size=count),rng.normal(size=count)))
            line = np.insert(line,1,line[0],axis=0)  # repeated centerline vertex
            for width,spacing,cap in ((1.,.8,2.),(4.,4.,4.),(.7,.3,.5)):
                expected = np.asarray(list(_mask_cells(line,projector,width,spacing,cap))).reshape(-1,12)
                local = (line-projector.origin) @ projector.axes.T
                actual = fast._ef.cliff_strip_domains(local,width,spacing,cap)
                np.testing.assert_allclose(actual,expected,rtol=1e-12,atol=1e-12)
                np.testing.assert_array_equal(actual,fast._ef.cliff_strip_domains(local,width,spacing,cap))
        for line in (np.empty((0,3)),np.zeros((1,3)),np.zeros((4,3)),np.array([[0.,0.,0.],[.5,0.,1.]])):
            self.assertEqual(fast._ef.cliff_strip_domains(line,1.,1.,1.).shape,(0,12))
        for line,width,spacing,cap in (
                (np.zeros((2,2)),1.,1.,1.),
                (np.full((2,3),np.nan),1.,1.,1.),
                (np.zeros((2,3)),0.,1.,1.),
                (np.zeros((2,3)),1.,float('nan'),1.),
                (np.array([[0.,0.,0.],[10.,0.,0.]]),1.,1e-8,1.)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_strip_domains(line,width,spacing,cap)

    def test_four_edges_erode_and_interior_remains_dense(self):
        import fast
        points = fast._ef.cliff_strip_domains(np.array([[0.,0.,0.],[0.,12.,0.]]),2.,.4,2.).reshape(-1,4)
        edges = (np.abs(points[:,0]) == 2) | (points[:,1] == 0) | (points[:,1] == 12)
        np.testing.assert_array_equal(points[edges,2],1.)
        center = np.unique(points[points[:,0] == 0],axis=0)
        self.assertEqual(center[center[:,1] == 6,2].item(),0.)
        self.assertTrue(np.all(np.diff(center[center[:,1] <= 2,2]) <= 0))
        # Run the private stock recipe on synthetic input: proprietary textures
        # and their derivatives are intentionally absent from the source tree.
        from PIL import Image
        from io import BytesIO
        import fast
        import cliffstock
        source = BytesIO()
        Image.fromarray(np.arange(256, dtype=np.uint16).reshape(16,16)*257).save(source, 'PNG')
        coverage, threshold = fast._ef.cliff_contact_maps(source.getvalue())
        material = cliffstock._material([1.,1.,1.,1.])[cliffstock.MATERIAL]
        stage = material['Stages'][0]
        def mask(data):
            with Image.open(BytesIO(data)) as image:
                return np.asarray(image.convert('RGB'),float)[...,0]/255
        h = mask(coverage)
        lookup = mask(threshold)[0]
        cutoff = material['alphaRef']/255
        for edge, expected in ((0,True),(-1,False)):
            alpha = stage['opacityFactor']*h*np.clip(2*lookup[edge],0,1)
            self.assertTrue(np.all((alpha >= cutoff) == expected))
        # Filtered samples and all mip levels remain in the same [min,max] range.
        # The previous >1 forward alpha extrapolated colours outside either
        # surface's gamut, particularly over bright HDR terrain.
        alpha = stage['opacityFactor']*h.max()*np.clip(2*lookup,0,1)
        self.assertLessEqual(float(alpha.max()),1.)
        self.assertGreaterEqual(cutoff,.85)
        self.assertTrue(np.all(np.diff(lookup)<=0))
        rock = np.array([.32,.24,.20])
        terrain = np.array([1.2,1.4,1.1])
        mixed = alpha[:,None]*rock + (1-alpha[:,None])*terrain
        self.assertTrue(np.all(mixed >= np.minimum(rock,terrain)))
        self.assertTrue(np.all(mixed <= np.maximum(rock,terrain)))
        self.assertTrue(material['alphaTest'])
        # Cutouts must render with opaque terrain before windshield blending.
        self.assertFalse(material['translucent'])
        self.assertFalse(material['castShadows'])

    def test_frontmost_receiver_and_misses(self):
        projector=cs.Projector(np.concatenate((plane(),plane(2))),[0,0,1],[0,0,0])
        strip = cs.build([[-3,0,0],[3,0,0]],projector,half_width=1)
        self.assertIsNotNone(strip)
        np.testing.assert_allclose(strip.positions[:,2],2+cs.SURFACE_OFFSET_M)
        self.assertTrue((strip.receiver >= 2).all())
        np.testing.assert_allclose(strip.normals,np.tile([0,0,1],(len(strip.positions),1)))
        self.assertIsNone(cs.build([[30,30,0],[36,30,0]],projector,half_width=1))


    def test_curved_strip_keeps_planar_uvs_and_faded_caps(self):
        projector=cs.Projector(plane(),[0,0,1],[0,0,0])
        line=np.array([[-5,-3,0],[-2,-2,0],[0,0,0],[2,1,0],[5,1,0]])
        strip=cs.build(line,projector,half_width=1)
        self.assertIsNotNone(strip)
        expected=strip.positions@projector.axes[:2].T/cs.TEXTURE_REPEAT_M
        expected[:,1]*=-1
        np.testing.assert_allclose(strip.uvs,expected,atol=1e-7)
        self.assertGreater(np.count_nonzero(strip.threshold==1),10)
        np.testing.assert_allclose(strip.positions[:,2],cs.SURFACE_OFFSET_M)
        face=np.cross(strip.positions[strip.tris[:,1]]-strip.positions[strip.tris[:,0]],strip.positions[strip.tris[:,2]]-strip.positions[strip.tris[:,0]])
        self.assertTrue(np.all(face[:,2]>0))

    def test_projection_crosses_two_receivers_without_bridging_gaps(self):
        ground=plane();rock=plane(0.2);rock[:,:,0]+=25
        projector=cs.Projector(np.concatenate((ground,rock)),[0,0,1],[0,0,0])
        strip=cs.build([[0,0,0],[10,0,0]],projector,half_width=1)
        self.assertTrue(np.any(strip.receiver<2))
        self.assertTrue(np.any(strip.receiver>=2))
        for k in range(3):
            lengths=np.linalg.norm(strip.positions[strip.tris[:,k]]-strip.positions[strip.tris[:,(k+1)%3]],axis=1)
            self.assertLess(lengths.max(),1.5)  # This fixture must not bridge the receiver gap.

    def test_crossing_receivers_cover_fold_without_holes_or_buried_faces(self):
        # Two crossing planes form a sharp V; neither can own a triangle
        # spanning the crease, even when the mask grid misses the crease.
        left=plane();left[:,:,2]=left[:,:,0]*.75
        right=plane();right[:,:,2]=-right[:,:,0]*.75
        projector=cs.Projector(np.concatenate((left,right)),[0,0,1],[0,0,0])
        strip=cs.build([[-3,0,0],[3,0,0]],projector,half_width=1,spacing=.85)
        faces=strip.positions[strip.tris]
        center=faces.mean(1)
        np.testing.assert_allclose(center[:,2]-cs.SURFACE_OFFSET_M,abs(center[:,0])*.75,atol=1e-7)
        area=np.cross(faces[:,1]-faces[:,0],faces[:,2]-faces[:,0])[:,2].sum()/2
        self.assertAlmostEqual(area,12,places=6)
        # Every vertex of a fragment belongs to the same receiver plane.
        self.assertTrue(np.all(strip.receiver[strip.tris]==strip.receiver[strip.tris[:,0,None]]))

    def test_disconnected_receivers_do_not_grow_bridge_triangles(self):
        left=plane();left[:,:,0]=left[:,:,0]/20-2
        right=plane();right[:,:,0]=right[:,:,0]/20+2
        projector=cs.Projector(np.concatenate((left,right)),[0,0,1],[0,0,0])
        strip=cs.build([[-3,0,0],[3,0,0]],projector,half_width=1,spacing=.9)
        faces=strip.positions[strip.tris]
        self.assertTrue(np.all((faces[:,:,0].max(1)<=-1+1e-7)|(faces[:,:,0].min(1)>=1-1e-7)))
        area=np.cross(faces[:,1]-faces[:,0],faces[:,2]-faces[:,0])[:,2].sum()/2
        self.assertAlmostEqual(area,8,places=6)

    def test_receiver_shading_normals_survive_projection(self):
        receiver=plane()
        normals=np.tile(cs.unit(np.array([.2,.1,1.])),(2,3,1))
        projector=cs.Projector(receiver,[0,0,1],[0,0,0],vertex_normals=normals)
        strip=cs.build([[-3,0,0],[3,0,0]],projector,half_width=1)
        np.testing.assert_allclose(strip.normals,np.tile(normals[0,0],(len(strip.positions),1)))

    def test_short_contact_has_a_dense_center_with_coarse_mask_grid(self):
        projector=cs.Projector(plane(),[0,0,1],[0,0,0])
        strip=cs.build([[0,0,0],[4,0,0]],projector,half_width=4,spacing=4,end_fade=4)
        self.assertIsNotNone(strip)
        self.assertAlmostEqual(strip.threshold.min(),0.)
        self.assertAlmostEqual(strip.threshold.max(),1.)

    def test_degenerate_centerline_is_skipped(self):
        projector=cs.Projector(plane(),[0,0,1],[0,0,0])
        self.assertIsNone(cs.build([[0,0,0],[0,0,0]],projector))


class TerrainProjection(unittest.TestCase):
    def test_native_receiver_layout_matches_terrain_grid(self):
        import fast
        from unittest.mock import patch
        rng = np.random.default_rng(921)
        for spacing in (1., 2.):
            for lo in ([-3., -4.], [0., 0.], [-2.3, 3.7], [1000000.2, -1000000.9]):
                lo = np.array(lo)
                hi = lo + rng.uniform(1, 12, 2)
                calls = []
                def ground(xy):
                    calls.append(xy.copy())
                    return .17*xy[:,0]*xy[:,1]+.23*xy[:,0]
                xs = np.arange(np.floor(lo[0]/spacing)*spacing,hi[0]+spacing,spacing)
                ys = np.arange(np.floor(lo[1]/spacing)*spacing,hi[1]+spacing,spacing)
                xx, yy = np.meshgrid(xs,ys)
                xy = np.column_stack((xx.ravel(),yy.ravel()))
                points = np.column_stack((xy,ground(xy)))
                calls.clear()
                j,i = np.mgrid[:len(ys)-1,:len(xs)-1]
                a = (j*len(xs)+i).ravel(); b=a+1; c=a+len(xs); d=c+1
                even = (((i+round(xs[0]/spacing))^(j+round(ys[0]/spacing)))&1).ravel()==0
                tris = np.concatenate((np.column_stack((a[even],b[even],d[even])),
                    np.column_stack((a[even],d[even],c[even])),
                    np.column_stack((a[~even],b[~even],c[~even])),
                    np.column_stack((b[~even],d[~even],c[~even]))))
                with patch.object(cs,'GROUND_GRID_M',spacing):
                    actual = cs.ground_faces(ground,lo,hi)
                self.assertEqual(len(calls),1)
                np.testing.assert_array_equal(calls[0],xy)
                np.testing.assert_array_equal(actual,points[tris])
        def forbidden(_):
            self.fail('invalid or empty domains must not access terrain')
        for lo,hi,spacing in (([0,0],[1,1],0),([2,0],[1,1],1),
                              ([0,0],[float('nan'),1],1),([0,0],[1e9,1e9],1)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_terrain_faces(lo,hi,spacing,forbidden)
        self.assertEqual(fast._ef.cliff_terrain_faces([0,0],[0,0],1.,forbidden).shape,(0,9))
        with self.assertRaises(ValueError):
            fast._ef.cliff_terrain_faces([0,0],[2,2],1.,lambda p:np.zeros(1))

    def test_native_sampling_uses_unique_corners_for_both_ground_grids(self):
        import fast
        from unittest.mock import patch
        class Frame:
            def enu_to_geodetic(self,x,y,z): return x,y,z
            def geodetic_to_enu(self,x,y,z): return x,y,z
        rng = np.random.default_rng(451)
        xy = np.concatenate((rng.uniform(-10,10,(150,2)),np.array([[0,0],[-2,-2],[1,1],[2,2]])))
        for spacing in (1.,2.):
            calls = []
            def drawn(x,y):
                calls.append(np.column_stack((x,y)))
                return .173*x*y+.21*x*x-.13*y
            surface = cs.TerrainSurface(Frame(),drawn,1/64)
            with patch.object(cs,'GROUND_GRID_M',spacing):
                faces = surface.faces(np.array([-4.,-4.,0]),np.array([4.,4.,0]))
                center = faces.mean(1)
                np.testing.assert_allclose(surface(center[:,:2]),center[:,2],atol=1e-12)
                calls.clear()
                actual = surface(xy)
                cell = np.floor(xy/spacing).astype(np.int64)
                frac = xy/spacing-cell
                nodes = cell[:,None,:]+np.array([[0,0],[1,0],[0,1],[1,1]])
                unique,inverse = np.unique(nodes.reshape(-1,2),axis=0,return_inverse=True)
                self.assertEqual(len(calls),1)
                np.testing.assert_array_equal(calls[0],unique*spacing)
                a,b,c,d = surface.corners(unique*spacing)[inverse].reshape(-1,4).T
                x,y = frac.T
                forward = np.where(x>y,a+x*(b-a)+y*(d-b),a+y*(c-a)+x*(d-c))
                backward = np.where(x+y<1,a+x*(b-a)+y*(c-a),d+(1-y)*(b-d)+(1-x)*(c-d))
                np.testing.assert_array_equal(actual,np.where(((cell[:,0]^cell[:,1])&1)==0,forward,backward))
        def forbidden(_):
            self.fail('empty queries must not access terrain')
        self.assertEqual(fast._ef.cliff_terrain_sample(np.empty((0,2)),1.,forbidden).shape,(0,))
        with self.assertRaises(ValueError):
            fast._ef.cliff_terrain_sample(xy,0.,forbidden)
        with self.assertRaises(ValueError):
            fast._ef.cliff_terrain_sample(xy,1.,lambda p:np.zeros(1))
        result = fast._ef.cliff_terrain_sample(xy,1.,lambda p:np.full(len(p),np.nan))
        self.assertTrue(np.isnan(result).all())

    def test_receiver_triangles_match_quantized_alternating_terrain_cells(self):
        class Frame:
            def enu_to_geodetic(self,x,y,z): return x,y,z
            def geodetic_to_enu(self,x,y,z): return x,y,z
        surface=cs.TerrainSurface(Frame(),lambda x,y: .173*x*y+.21*x*x-.13*y,1/64)
        faces=surface.faces(np.array([-3.,-3.,0]),np.array([3.,3.,0]))
        for bary in ([1/3,1/3,1/3],[.1,.2,.7],[.4,.4,.2]):
            points=np.einsum('j,ijk->ik',bary,faces)
            np.testing.assert_allclose(surface(points[:,:2]),points[:,2],atol=1e-12)
        corners=faces.reshape(-1,3)
        np.testing.assert_allclose(corners[:,2]*64,np.rint(corners[:,2]*64))

    def test_curved_quantized_ground_does_not_bury_projected_fragments(self):
        class Frame:
            def enu_to_geodetic(self,x,y,z): return x,y,z
            def geodetic_to_enu(self,x,y,z): return x,y,z
        surface=cs.TerrainSurface(Frame(),lambda x,y: .12*x*y+.08*x*x,1/64)
        receiver=surface.faces(np.array([-5.,-5.,0]),np.array([5.,5.,0]))
        strip=cs.build([[-3,0,0],[3,0,0]],cs.Projector(receiver,[0,0,1],[0,0,0]))
        points=strip.positions[strip.tris].mean(1)
        np.testing.assert_allclose(points[:,2]-surface(points[:,:2]),cs.SURFACE_OFFSET_M,atol=1e-7)


class ProductionStrips(unittest.TestCase):
    def test_native_matches_reference_on_crossing_receivers(self):
        a = plane(); b = plane()
        a[:, :, 2] = a[:, :, 0]*.3
        b[:, :, 2] = -b[:, :, 0]*.2
        projector = cs.Projector(np.concatenate((a,b)), [0,0,1], [0,0,0])
        run = np.array([[-6,0,0],[6,0,0]])
        native = cs.build(run, projector)
        reference = build_reference(run, ReferenceProjector(projector.triangles,[0,0,1],[0,0,0]))
        for value in (native, reference):
            face = value.positions[value.tris]
            area = np.linalg.norm(np.cross(face[:,1]-face[:,0],face[:,2]-face[:,0]),axis=1).sum()/2
            if value is native:
                expected = area
            else:
                self.assertAlmostEqual(area, expected, places=6)
        self.assertEqual(len(native.tris), len(reference.tris))

    def test_contact_budget_is_spread_across_separate_edges(self):
        runs=[np.array([[x,0.,0.],[x,10.,0.]]) for x in [0.,1.,2.,200.]]
        ordered=cs.spread_runs(runs)
        self.assertEqual(ordered[1][0,0],200.)

    def test_native_contact_order_preserves_reference_and_budget(self):
        import fast
        rng = np.random.default_rng(312)
        for count in (1,8,150):
            runs = [rng.normal(size=(int(rng.integers(2,15)),3)).cumsum(0)+rng.normal(size=3)*30
                    for _ in range(count)]
            centers = np.array([r.mean(0) for r in runs])
            lengths = np.array([np.linalg.norm(np.diff(r,axis=0),axis=1).sum() for r in runs])
            distance = np.full(count,np.inf)
            remaining = np.ones(count,bool)
            selected = int(lengths.argmax())
            expected = []
            for _ in range(min(count,128)):
                expected.append(selected)
                remaining[selected] = False
                distance = np.minimum(distance,((centers-centers[selected])**2).sum(1))
                if not remaining.any():
                    break
                selected = int(np.where(remaining,distance,-1).argmax())
            actual = cs.spread_runs(runs)
            self.assertEqual([next(i for i,r in enumerate(runs) if r is a) for a in actual],expected)
        points = np.tile([[0.,0.,0.],[0.,10.,0.]],(3,1))
        offsets = np.array([0,2,4,6],np.int64)
        self.assertEqual(fast._ef.cliff_contact_order(points,offsets,3),[0,1,2])
        self.assertEqual(fast._ef.cliff_contact_order(points,offsets,0),[])
        self.assertEqual(fast._ef.cliff_contact_order(np.empty((0,3)),np.array([0],np.int64),128),[])
        for bad in ([1,2,6],[0,2,2,6],[0,8],[0,-1,6]):
            with self.assertRaises(ValueError):
                fast._ef.cliff_contact_order(points,np.array(bad,np.int64),128)

    def test_batching_keeps_more_than_eight_contact_runs(self):
        p=np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
        parts=[]
        for i in range(24):
            strip=cs.Strip(p+[i*10,0,0],np.array([[0,1,2]]),p[:,:2],p[:,:2],
                           np.tile([0.,0.,1.],(3,1)),np.zeros(3,int),np.array([0.,.5,1.]))
            parts.append((strip,np.full((3,4),128,np.uint8)))
        packed=cs.pack_parts(parts)
        self.assertLessEqual(len(packed),cs.PARTS_MAX)
        self.assertEqual(sum(len(s.tris) for s,c in packed),24)
        self.assertEqual(max(s.positions[:,0].max() for s,c in packed),231.)
        np.testing.assert_array_equal(packed[0][0].threshold,np.tile([0.,.5,1.],24))

    def test_native_batches_match_ordered_attribute_compaction(self):
        from unittest.mock import patch
        import cliffstrip_reference as reference
        import fast
        rng = np.random.default_rng(704)
        parts = []
        for n in (0,1,8,19,3,0,27):
            p = rng.normal(size=(50,3))
            tri = rng.integers(0,50,(n,3),dtype=np.int64)
            strip = cs.Strip(p,tri,rng.random((50,2)),rng.random((50,2)),rng.normal(size=(50,3)),
                             rng.integers(0,10,50),rng.random(50))
            parts.append((strip,rng.integers(0,256,(50,4),dtype=np.uint8)))
        for limit in (1,7,20,100):
            with patch.object(cs,'PART_TRIANGLES',limit), patch.object(reference,'PART_TRIANGLES',limit):
                actual = cs.pack_parts(parts)
                expected = reference.pack_parts_reference(parts)
            self.assertEqual(len(actual),len(expected))
            for (a,ac),(b,bc) in zip(actual,expected):
                self.assertLessEqual(len(a.tris),limit)
                for name in cs.Strip.__dataclass_fields__:
                    np.testing.assert_array_equal(getattr(a,name),getattr(b,name))
                np.testing.assert_array_equal(ac,bc)
        self.assertEqual(cs.pack_parts([]),[])
        for triangles,counts,limit in (([],[],0),([np.array([[0,1,2]])],[2],10),
                                      ([np.array([[0,1,2]])],[],10)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_contact_batches(triangles,counts,limit)

    def test_packing_retains_contacts_beyond_old_eight_import_limit(self):
        n = cs.PART_TRIANGLES*10+1
        positions=np.tile([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]],(n,1))
        strip=cs.Strip(positions,np.arange(n*3).reshape(-1,3),positions[:,:2],positions[:,:2],
                       np.tile([0.,0.,1.],(n*3,1)),np.zeros(n*3,int),np.zeros(n*3))
        colors=np.full((n*3,4),128,np.uint8)
        parts=cs.pack_parts([(strip,colors)])
        self.assertEqual(len(parts),11)
        self.assertEqual(sum(len(s.tris) for s,c in parts),n)
        self.assertTrue(all(len(s.tris)<=8000 for s,c in parts))
        self.assertLessEqual(len(parts),cs.PARTS_MAX)

    def test_all_contact_families_match_rendered_satellite_rim(self):
        from cliffrocks import Mesh
        from cliffcolors import linear
        from clifftransition import MEANS
        p = np.array([[-6.,-8.,-6.],[6.,-8.,6.],[6.,8.,6.],[-6.,8.,-6.]])
        t = np.array([[0,1,2],[0,2,3]])
        mineral = np.tile([90,85,80,255], (4,1)).astype(np.uint8)
        visible = np.tile([200,150,110,255], (4,1)).astype(np.uint8)
        for family in range(4):
            for blend in (0., .5, 1.):
                rock = Mesh(p,t,np.zeros((4,2)),visible,tri_materials=np.full(2,family),
                            uvs2=np.tile([.5/16+blend*15/16,.5],(4,1)),
                            terrain_blend=True,strip_colors=mineral)
                patches = cs.prepare(rock,lambda xy:np.zeros(len(xy)))
                self.assertTrue(patches)
                expected = linear(visible[0,:3]/255)*((1-blend)*np.array(MEANS[family])+blend)
                for part in patches:
                    np.testing.assert_allclose(part['albedo'],
                        np.broadcast_to(expected,part['albedo'].shape),rtol=1e-6)

    def test_native_strip_mesh_preserves_welding_shading_and_uvs(self):
        import fast
        from cliffstrip_reference import _finish_strip, _barycentric
        rng = np.random.default_rng(185)
        rows = np.array([[1,1,0,.2,0],[2,1,0,.7,0],[1,2,0,1,0]],float)
        rows = np.concatenate((rows,rows,rows.copy(),rows.copy()))
        rows[6:9,4] = 1  # same geometry, different receiving triangle
        rows[9:,3] = .4  # same geometry, different coverage
        for origin in (np.zeros(3),np.array([1e6,-1e6,10.])):
            for normal in ([0,0,1],[.3,.4,1]):
                axes = cs.basis(normal)
                local = np.array([[[0.,0,0],[4,0,0],[0,4,0]]]*2)
                faces = local @ axes + origin
                for smooth in (False,True):
                    ns = cs.unit(rng.uniform(-1,1,(2,3,3))) if smooth else None
                    projector = cs.Projector(faces,normal,origin,vertex_normals=ns)
                    owners = rows[:,4].astype(np.int64)
                    world = rows[:,:3] @ projector.axes + projector.origin
                    if ns is None:
                        face = faces[owners]
                        normals = cs.unit(np.cross(face[:,1]-face[:,0],face[:,2]-face[:,0]))
                        normals *= np.where((normals @ projector.axes[2])[:,None]<0,-1,1)
                    else:
                        bary = _barycentric(projector.local[owners,:,:2],rows[:,:2])
                        normals = cs.unit(np.einsum('ij,ijk->ik',bary,ns[owners]))
                    world += projector.axes[2]*cs.SURFACE_OFFSET_M
                    expected = _finish_strip(projector,world,normals,rows[:,3],owners,
                                             np.arange(len(rows)).reshape(-1,3),4.)
                    actual,ids,tris = fast._ef.cliff_strip_mesh(rows,faces.reshape(-1,9),
                        projector.local.reshape(-1,9),projector.axes,tuple(origin),4.,cs.SURFACE_OFFSET_M,
                        cs.LOOKUP_WIDTH,None if ns is None else ns.reshape(-1,9))
                    np.testing.assert_array_equal(ids,expected.receiver)
                    np.testing.assert_array_equal(tris,expected.tris)
                    for result,reference in ((actual[:,:3],expected.positions),(actual[:,3:6],expected.normals),
                            (actual[:,6:8],expected.uvs),(actual[:,8:10],expected.uvs2),(actual[:,10],expected.threshold)):
                        np.testing.assert_allclose(result,reference,atol=1e-9,rtol=1e-10)
        args = (faces.reshape(-1,9),projector.local.reshape(-1,9),projector.axes,tuple(origin),4.,.08,1024)
        empty,ids,tris = fast._ef.cliff_strip_mesh(np.empty((0,5)),*args)
        self.assertEqual(empty.shape,(0,11)); self.assertEqual(ids.shape,(0,)); self.assertEqual(tris.shape,(0,3))
        for invalid in (rows[:2],np.full((3,5),np.nan),np.array([[0,0,0,0,-1]]*3,float),
                        np.array([[0,0,0,0,2]]*3,float)):
            with self.assertRaises(ValueError):
                fast._ef.cliff_strip_mesh(invalid,*args)

    def test_native_tint_preserves_nearby_palette_and_receiver_colors(self):
        from types import SimpleNamespace
        from cliffstrip_reference import strip_tint
        rng = np.random.default_rng(328)
        for origin in (np.zeros(3),np.array([1e6,-1e6,100.])):
            for normal in ([0,0,1],[.3,.4,1]):
                # Random triangles plus duplicate corners exercise palette averaging.
                faces = rng.uniform(-5,5,(24,3,3))+origin
                faces = np.concatenate((faces,faces[:3]))
                albedo = rng.uniform(.01,.8,faces.shape)
                ground_count = 2
                projector = cs.Projector(np.concatenate((plane()+origin,faces)),normal,origin)
                bary = rng.dirichlet([2,2,2],len(faces))
                rock = np.einsum('ij,ijk->ik',bary,faces)+projector.axes[2]*cs.SURFACE_OFFSET_M
                positions = np.concatenate((rng.uniform(-7,7,(73,3))+origin,rock))
                owners = np.r_[np.zeros(73,dtype=np.int64),np.arange(len(faces))+ground_count]
                strip = SimpleNamespace(positions=positions,receiver=owners)
                expected = strip_tint(strip,projector,faces,albedo,ground_count)
                actual = cs._strip_tint(strip,projector,faces,albedo,ground_count)
                np.testing.assert_allclose(actual,expected,atol=2e-9,rtol=1e-9)
                np.testing.assert_array_equal(actual,cs._strip_tint(strip,projector,faces,albedo,ground_count))
        # Equidistant ground colors use the first spatial cell, independent of face order.
        faces = np.array([[[-2.,0,0]]*3,[[2.,0,0]]*3])
        colors = np.array([[[.2,.3,.4]]*3,[[.7,.6,.5]]*3])
        projector = SimpleNamespace(axes=np.eye(3),origin=np.zeros(3))
        strip = SimpleNamespace(positions=np.zeros((1,3)),receiver=np.zeros(1,dtype=np.int64))
        for order in ([0,1],[1,0]):
            np.testing.assert_allclose(cs._strip_tint(strip,projector,faces[order],colors[order],1),colors[0,:1],atol=1e-15)
        for owner in (-1,3):
            strip.receiver[:] = owner
            with self.assertRaises(ValueError):
                cs._strip_tint(strip,projector,faces,colors,1)
        strip.receiver[:] = 1
        with self.assertRaisesRegex(ValueError,'degenerate'):
            cs._strip_tint(strip,projector,faces,colors,1)
        strip.receiver[:] = 0
        faces[0,0,0] = np.nan
        with self.assertRaises(ValueError):
            cs._strip_tint(strip,projector,faces,colors,1)

    def test_production_parts_cover_both_surfaces_and_write_valid_tint(self):
        from cliffrocks import Mesh
        import cdae
        import tempfile
        # A sloping rock sheet crossing flat terrain, with a long contact.
        y,x = np.mgrid[-15:16:1., -3:4:1.]
        p = np.column_stack((x.ravel(),y.ravel(),x.ravel()))
        j,i = np.mgrid[:len(y)-1,:y.shape[1]-1]
        a=(j*y.shape[1]+i).ravel();b=a+1;c=a+y.shape[1];d=c+1
        t=np.concatenate((np.column_stack((a,b,c)),np.column_stack((b,d,c))))
        for family in (0,):
            rock=Mesh(p,t,np.zeros((len(p),2)),np.full((len(p),4),128,np.uint8),
                      tri_materials=np.full(len(t),family,np.int32))
            parts=cs.tile_parts(rock,lambda xy:np.zeros(len(xy)))
            self.assertTrue(parts)
            self.assertLessEqual(len(parts),cs.PARTS_MAX)
            self.assertLessEqual(sum(len(s.tris) for s,_ in parts),cs.TILE_TRIANGLES)
            self.assertTrue(any(s.positions[:,2].min()<.1 and s.positions[:,2].max()>.3 for s,_ in parts))
            for strip,color in parts:
                self.assertEqual(color.dtype,np.uint8)
                self.assertTrue((color[:,3]==255).all())
                self.assertGreater(color[:,:3].max(),10)
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'strip.cdae';cs.write(path,parts[0])
                self.assertEqual(cdae.read(path)['materials'][0]['name'],cs.MATERIAL)
                import roads, json
                rock.strips = parts
                roads.write_tile(folder, '/test', 1, 2, 3, [], cliff_mesh=rock)
                manifest = json.loads(next(Path(folder).glob('*.json')).read_text())
                self.assertNotIn('cliffDecals', manifest)
                for i in range(1, len(parts)+1):
                    entry = manifest['cliffStrip'+str(i)]
                    self.assertEqual(entry['pos'], manifest['cliff']['pos'])
                    strip = parts[i-1][0]
                    self.assertEqual(entry['bounds'], strip.positions.min(axis=0).tolist() +
                                     strip.positions.max(axis=0).tolist())
                    shape = cdae.read(Path(folder)/Path(entry['shape']).name)
                    self.assertEqual(shape['materials'][0]['name'], cs.MATERIAL)



if __name__=='__main__':unittest.main()
