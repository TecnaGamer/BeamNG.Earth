"""Cliff contours trim inward with shared intersections and no tile seams."""
import json
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cliffrocks as cr
import clifftransition as ct
import cliffmaterial as cm
import clifflod

class CliffTransitionTest(unittest.TestCase):
    def grid(self,n=49):
        x=np.arange(n)*.5
        xx,yy=np.meshgrid(x,x)
        h=2*xx+.1*np.sin(yy)
        p=np.stack((xx,yy,h),-1)
        keep=np.zeros((n-1,n-1),bool);keep[6:-6,6:-6]=True
        keep[20:26,20:26]=False  # excluded ground stays a hole
        return x,h,p,keep

    def test_contour_trims_steps_without_invading_exclusions(self):
        x,h,p,k=self.grid()
        out,w,_,field=ct.prepare(p,h,k,.5,.5,1,.35)
        np.testing.assert_allclose(out.reshape(-1,3)[w[:,0]<1e-12],p.reshape(-1,3)[w[:,0]<1e-12])
        for family in range(4):
            cuts=np.full(k.shape,family in (1,2));biome=np.full(k.shape,1 if family==2 else 0)
            rock=cr.mesh(h,k,x,x,biome,(0,0,x[-1],x[-1]),(0,0,0),cuts=cuts,
                         natural_family=family if family in (0,3) else 0)
            self.assertIsNotNone(rock)
            self.assertLess(len(rock.tris),int(k.sum())*2*1.1)
            self.assertEqual(set(rock.tri_materials),{family})
            self.assertTrue(np.isfinite(rock.normals).all())
        # A flat source isolates footprint correctness from the rock displacement.
        j,i=np.nonzero(k);a=j*len(x)+i
        tris=np.stack((np.c_[a,a+1,a+len(x)+1],np.c_[a,a+len(x)+1,a+len(x)]),1).reshape(-1,3)
        pts,tt,attrs=ct.clip(p.reshape(-1,3),tris,p.reshape(-1,3),field,p.reshape(-1,3))
        triangles=pts[tt]
        cross=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
        self.assertTrue((cross[:,2]>0).all())
        for bary in ([1/3]*3,[.8,.1,.1],[.1,.8,.1],[.1,.1,.8]):
            q=(triangles*np.array(bary)[None,:,None]).sum(1)
            cells=np.floor(q[:,:2]/.5).astype(int)
            self.assertTrue(k[cells[:,1],cells[:,0]].all())
        edges=np.sort(np.concatenate((tt[:,[0,1]],tt[:,[1,2]],tt[:,[2,0]])),axis=1)
        edges,count=np.unique(edges,axis=0,return_counts=True)
        rim=pts[edges[count==1]]
        delta=np.abs(rim[:,1,:2]-rim[:,0,:2])
        self.assertGreater(np.count_nonzero((delta[:,0]>1e-5)&(delta[:,1]>1e-5)),20)
        np.testing.assert_allclose(pts,attrs) # shared edge interpolation is consistent

    def test_new_boundary_is_full_satellite_and_cannot_regain_relief(self):
        ground=np.array([[0,0,0],[1,0,0],[0,1,0]],float)
        p=ground.copy();p[:,2]=5
        colors=np.tile([190,150,130,255],(3,1))
        # A baked photo shadow must survive at the contact, for every rock family.
        target=np.tile([24,28,35],(3,1))
        for family in range(4):
            for field in ([-.1,.4,.4],[0,.4,-.1]):
                c,u=ct.attributes(colors,np.array([[1],[0],[0]],float),target,family)
                ec,eu=ct.attributes(colors,np.ones((3,1)),target,family)
                q,t,a=ct.clip(p,np.array([[0,1,2]]),np.c_[c,u],np.array(field)[:,None],ground,np.c_[ec,eu])
                edge=q[:,2]==0
                self.assertTrue(edge.any())
                np.testing.assert_allclose(a[edge,:3],np.broadcast_to(target[0],a[edge,:3].shape))
                relief=ct.relief_weights(np.ones((len(a),1)),a[:,-2:])
                np.testing.assert_allclose(relief[edge],0,atol=1e-12)
                self.assertTrue(np.all(q[~edge,2]==5))

    def test_contact_normals_match_physical_terrain_slope(self):
        h=np.tile(np.arange(4)*4.,(3,1))
        normals=np.tile([0,0,1.],(h.size,1))
        weights=np.linspace(0,1,h.size)[:,None]
        mixed,ground=ct.normals(normals,h,weights,2,1)
        expected=np.array([-2,0,1])/np.sqrt(5)
        np.testing.assert_allclose(ground,np.broadcast_to(expected,ground.shape))
        np.testing.assert_allclose(mixed[0],normals[0])
        np.testing.assert_allclose(mixed[-1],expected)
        np.testing.assert_allclose(np.linalg.norm(mixed,axis=1),1)

    def test_small_threshold_gaps_preserve_hard_exclusions_and_large_benches(self):
        h=np.tile(np.arange(17)*4.,(17,1))
        classes=np.full(h.shape,60)
        keep=np.ones((16,16),bool);keep[7,7]=False
        blocked=np.zeros_like(keep)
        self.assertTrue(ct.fill_gaps(keep,h,classes,blocked,(4,4),4).all())
        for exclusion in ('road','water','city','unknown','flat'):
            b=blocked.copy();c=classes.copy();z=h.copy()
            if exclusion=='road':b[7,7]=True
            if exclusion=='water':c[7,7]=80
            if exclusion=='city':c[6,6]=50
            if exclusion=='unknown':c[7,7]=0
            if exclusion=='flat':z[7:9,7:9]=28
            np.testing.assert_array_equal(ct.fill_gaps(keep,z,c,b,(4,4),4),keep)
        large=keep.copy();large[5:10,5:10]=False
        np.testing.assert_array_equal(ct.fill_gaps(large,h,classes,blocked,(4,4),4),large)
        notch=keep.copy();notch[:8,7]=False
        np.testing.assert_array_equal(ct.fill_gaps(notch,h,classes,blocked,(4,4),4),notch)

    def test_contour_endpoint_is_not_duplicated(self):
        p=np.array([[0,0,0],[1,0,0],[0,1,0]],float)
        for f,count in (([0,-1,1],1),([0,-1,-1],0),([1,1,1],1),([-1,-1,-1],0)):
            q,t,a=ct.clip(p,np.array([[0,1,2]]),p,np.array(f,float)[:,None],p)
            self.assertEqual(len(t),count)
            if count:
                triangle=q[t]
                self.assertTrue((np.cross(triangle[:,1]-triangle[:,0],triangle[:,2]-triangle[:,0])[:,2]>0).all())

    def test_geometry_taper_and_later_relief_share_the_rim(self):
        x,h,p,k=self.grid(81)
        raised=p.copy();raised[:,:,2]+=5
        out,w,_,field=ct.prepare(raised,h,k,.5,.5,1,.35)
        visible=field[:,0]>=0
        # Clearance progresses continuously from buried terrain to full relief.
        expected=-.35+5.35*(1-w[:,0])
        np.testing.assert_allclose((out[:,:,2]-h).ravel()[visible],expected[visible],atol=1e-12)
        color=np.tile([130,140,150,255],(h.size,1))
        _,uv=ct.attributes(color,w,np.zeros((h.size,3)),0)
        relief=ct.relief_weights(np.ones((h.size,1)),uv)
        np.testing.assert_allclose(relief[:,0],1-w[:,0],atol=1e-12)
        self.assertEqual(float(relief[w[:,0]==1].max()),0.)
        self.assertTrue(np.all(relief[w[:,0]==0]==1))

    def test_rim_noise_varies_contact_without_moving_contour_or_endpoints(self):
        x,h,p,k=self.grid(161)
        h[:]=0; p[:,:,2]=5
        k[:]=False; k[12:-12,12:-12]=True
        out,w,_,field=ct.prepare(p,h,k,.5,.5,1,.35)
        repeated,rw,_,rf=ct.prepare(p,h,k,.5,.5,1,.35)
        np.testing.assert_array_equal(out,repeated)
        np.testing.assert_array_equal(w,rw)
        np.testing.assert_array_equal(field,rf)
        t=np.clip(field[:,0]/.4,0,1)
        smooth=1-t*t*(3-2*t)
        transition=(t>0)&(t<1)
        self.assertGreater(np.std((w[:,0]-smooth)[transition]),.02)
        self.assertLess(np.max(np.abs((w[:,0]-smooth)[transition])),.3)
        self.assertTrue(np.all((w>=0)&(w<=1)))
        self.assertTrue(np.all(w[t==1]==0))
        # The cut still terminates at buried ground, regardless of noise.
        rim=(field[:,0]<0)&(w[:,0]>0)
        np.testing.assert_allclose(out.reshape(-1,3)[rim,2],-.35)
        # Vary along a straight, constant-distance boundary, not just its corners.
        strip=w.reshape(h.shape)[16,30:-30]
        self.assertGreater(np.ptp(strip),.04)

    def test_halo_independence_and_tile_seams(self):
        x,h,p,k=self.grid(81)
        full,w,_,_=ct.prepare(p,h,k,.5,.5,1,.35)
        sub,sw,_,_=ct.prepare(p[2:-2,2:-2],h[2:-2,2:-2],k[2:-2,2:-2],.5,.5,1,.35)
        np.testing.assert_allclose(full[16:-16,16:-16],sub[14:-14,14:-14],atol=1e-12)
        np.testing.assert_allclose(w.reshape(h.shape)[16:-16,16:-16],sw.reshape(sub.shape[:2])[14:-14,14:-14])
        b=np.zeros(k.shape,int)
        left=cr.mesh(h,k,x,x,b,(0,0,20,40),(0,0,0))
        right=cr.mesh(h,k,x,x,b,(20,0,40,40),(0,0,0))
        def edge(rock):
            ids=np.flatnonzero(abs(rock.positions[:,0]-20)<1e-6)
            rows=np.c_[rock.positions[ids],rock.colors[ids],rock.uvs2[ids]]
            return np.unique(rows,axis=0)
        np.testing.assert_allclose(edge(left),edge(right),atol=1e-5)

    def test_interior_colors_and_pbr_chart_preserved(self):
        colors=np.array([[130,145,190,255]]*3,float)
        rgb=np.array([[170,150,120]]*3,float)
        weights=np.array([[0],[.5],[1]],float)
        for family in range(4):
            out,uv=ct.attributes(colors,weights,rgb,family)
            np.testing.assert_array_equal(out[0],colors[0])
            np.testing.assert_array_equal(out[-1,:3],rgb[-1])
            self.assertTrue(np.all(np.diff(uv[:,0])>0))
            self.assertTrue(np.all(uv[:,1]==.5))
        x,h,p,k=self.grid()
        rock=cr.mesh(h,k,x,x,np.zeros(k.shape,int),(0,0,x[-1],x[-1]),(0,0,0))
        for _,lod in clifflod.levels(rock):
            np.testing.assert_array_equal(lod['tangent_uvs'],lod['uvs'])

    def test_near_canyon_grain_has_square_physical_repeat(self):
        root=Path(__file__).resolve().parents[2]
        mats=json.loads((root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        stage=mats['bne_cliff_canyon_sat']['Stages'][0]
        repeat=np.array([cm.WIDTH_M,cm.CANYON_HEIGHT_M])/stage['detailScale']
        np.testing.assert_allclose(repeat[0],repeat[1])
        self.assertGreaterEqual(repeat[0],.25)
        self.assertLessEqual(repeat[0],3.0)
        self.assertEqual(stage['detailMapUseUV'],0)
        self.assertEqual(stage['normalDetailMapUseUV'],0)
        self.assertLessEqual(stage['detailNormalMapStrength'],.5)
        for name in ('bne_far_cliff_canyon_sat','bne_far_cliff_canyon_blend'):
            self.assertNotIn('detailNormalMap',mats[name]['Stages'][0])

    def test_all_families_share_one_opaque_mask_and_preserve_pbr(self):
        root=Path(__file__).resolve().parents[2]
        mats=json.loads((root/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff.materials.json').read_text())
        masks=set()
        for old,new in zip(cm.MATERIALS,ct.MATERIALS):
            a,b=mats[old],mats[new]
            self.assertEqual(b['activeLayers'],2, 'BeamNG defaults to one layer even when Stages[1] is populated')
            self.assertFalse(b['translucent'])
            self.assertFalse(b.get('alphaTest',False))
            source=dict(a['Stages'][0]);source['normalMapUseUV']=0
            self.assertEqual(source,b['Stages'][0])
            self.assertEqual(b['Stages'][1]['opacityMapUseUV'],1)
            masks.add(b['Stages'][1]['opacityMap'])
        self.assertEqual(len(masks),1)
        self.assertNotIn('opacityMap',mats['bne_far_cliff_canyon_sat']['Stages'][0])

if __name__=='__main__':unittest.main()
