"""Behavior checks for the experimental projected contact strips."""
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cliffstrips as cs


def plane(z=0):
    p=np.array([[-20,-20,z],[20,-20,z],[20,20,z],[-20,20,z]],float)
    return p[[[0,1,2],[0,2,3]]]


class ProjectedStrips(unittest.TestCase):
    def test_four_edges_erode_and_interior_remains_dense(self):
        x=np.linspace(-2,2,21);y=np.linspace(0,12,31)
        t=cs.threshold(x[None,:],y[:,None],12)
        np.testing.assert_allclose(t[[0,-1]],1)
        np.testing.assert_allclose(t[:,[0,-1]],1)
        self.assertEqual(t[15,10],0)
        self.assertTrue(np.all(np.diff(t[:6,10])<=0))
        # Verify the shipped masks, including DDS quantization and the engine's
        # saturating detail operation, rather than duplicating the baker formula.
        from PIL import Image
        import json
        art = Path(__file__).resolve().parents[2]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth'
        material = json.loads((art/'cliff_contact.materials.json').read_text())['bne_cliff_contact_strip']
        stage = material['Stages'][0]
        def mask(field):
            with Image.open(art/'cliff'/Path(stage[field]).name) as image:
                return np.asarray(image.convert('RGB'),float)[...,0]/255
        h = mask('opacityMap')
        lookup = mask('opacityDetailMap')[0]
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
        self.assertTrue(material['translucent'])
        self.assertEqual(material['translucentBlendOp'],'LerpAlpha')
        self.assertFalse(material['translucentZWrite'])
        self.assertFalse(material['castShadows'])

    def test_frontmost_receiver_and_misses(self):
        projector=cs.Projector(np.concatenate((plane(),plane(2))),[0,0,1],[0,0,0])
        p,n,ids=projector.sample([[0,0],[30,30]])
        self.assertAlmostEqual(p[0,2],2)
        self.assertGreaterEqual(ids[0],2)
        self.assertEqual(ids[1],-1)
        np.testing.assert_allclose(n[0],[0,0,1])

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
            self.assertLess(lengths.max(),cs.MAX_BRIDGE_M)

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
        reference = cs.build_reference(run, projector)
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

    def test_strip_uses_rock_color_even_when_satellite_rim_is_dark_or_green(self):
        from cliffrocks import Mesh
        from cliffcolors import linear, srgb
        import json
        p = np.array([[-6.,-8.,-6.],[6.,-8.,6.],[6.,8.,6.],[-6.,8.,-6.]])
        t = np.array([[0,1,2],[0,2,3]])
        original = np.tile([150,120,100,255], (4,1)).astype(np.uint8)
        means = np.array(json.loads(Path(cs.__file__).with_name('cliff_transition_means.json').read_text()))
        for family in (0,3):
            outputs = []
            for rim_color in ([4,5,6,255],[50,230,30,255]):
                rock = Mesh(p,t,np.zeros((4,2)),np.tile(rim_color,(4,1)).astype(np.uint8),
                            tri_materials=np.full(2,family),uvs2=np.tile([15.5/16,.5],(4,1)),
                            terrain_blend=True,strip_colors=original)
                parts = cs.tile_parts(rock,lambda xy:np.zeros(len(xy)))
                self.assertTrue(parts)
                expected = np.rint(srgb(linear(original[0,:3]/255)*means[family])*255)
                for strip,colors in parts:
                    np.testing.assert_allclose(colors[:,:3],np.tile(expected,(len(colors),1)),atol=1)
                    self.assertGreater(strip.threshold.max(),.99)
                    self.assertLess(strip.threshold.min(),.5)
                    self.assertTrue((strip.positions[:,2] >= cs.GROUND_CLEARANCE_M).all())
                outputs.append(np.concatenate([c for _,c in parts]))
            np.testing.assert_array_equal(*outputs)

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
        for family in (0,3):
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
