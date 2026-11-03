"""Distant cliff coverage must not add triangles, move ground or texture skirts."""
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
    def setUp(self): self.frame=geodesy.EnuFrame(36.1,-112.1)

    def test_region_is_shared_and_does_not_recolor_existing_sites(self):
        np.testing.assert_array_equal(cm.natural_family([36.1,37.2,38.5,31.8,30.3,48],
            [-112.1,-113,-109.7,-106.5,-97.8,8]),[cm.CANYON]*3+[cm.NATURAL]*3)

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
            data=Path(path).read_bytes()
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
            ff.write_built(str(rock),b,textured=True,cliff_frame=self.frame,plain_path=str(plain))
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
        self.assertTrue((mesh.colors[canyon,:3]==137).all())
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
        mats=json.loads((root/'levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        for name in fc.MATERIALS:
            stage=mats[name]['Stages'][0]
            with Image.open(root/stage['detailMap'].lstrip('/')) as im:
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

    def test_far_normal_is_unit_length_and_preserves_detail_in_mips(self):
        from PIL import Image
        root=Path(__file__).resolve().parents[2]
        path=root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff/canyon_far_v3.normal.png'
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
        mats=json.loads((mod/'levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        for name in (cm.MATERIALS[cm.CANYON],*fc.MATERIALS):
            stage=mats[name]['Stages'][0]
            import cliffcolors
            if name.startswith('bne_far_'):
                self.assertNotIn('baseColorMap',stage,'satellite vertex color supplies the base')
                self.assertEqual(stage['detailMapUseUV'],0)
            else:
                self.assertIn('baseColorMap',stage,'near cliffs retain full authored color detail')
                self.assertNotIn('detailMap',stage)
                # New geological detail keeps the established near palette:
                # compare rendered mean including the intentional near exposure reduction.
                base=mod/stage['baseColorMap'].lstrip('/')
                previous=base.parent/'canyon_v3_base.color.png'
                def mean_rgb(path):
                    with Image.open(path) as im:
                        rgb=np.asarray(im.convert('RGB'))
                        stride=max(1,im.width//512)
                        return cliffcolors.linear(rgb[::stride,::stride]/255).mean(axis=(0,1))
                np.testing.assert_allclose(mean_rgb(base)*cliffcolors.linear(stage['baseColorFactor'][:3]),
                    mean_rgb(previous)*cm.CANYON_COLOR_FACTOR*cm.SATELLITE_HEADROOM*cm.NEAR_CANYON_EXPOSURE,atol=.006)
                with Image.open(base) as im:
                    self.assertEqual(im.size,(4096,4096))
            for key,value in stage.items():
                if key.endswith('Map'):
                    with Image.open(mod/value.lstrip('/')) as im:
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
        self.assertIn('cliff_canyon_height.png',(root/'server/revisit.py').read_text())
        self.assertTrue(np.isfinite(cm.sample(np.array([0,.5,1]),np.array([0,.5,1]),family=cm.CANYON)).all())

if __name__=='__main__': unittest.main()
