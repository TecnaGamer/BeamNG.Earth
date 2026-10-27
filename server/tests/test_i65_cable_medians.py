"""The requested I-65 cable interval, Louisville restart and Indiana endpoint."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import numpy as np
from test_ky_medians import pair, flat
import cablebarriers
import geodesy
import medianbarriers as barriers
import milemarkers
import roadmesh
import roads
import signroads

BOX = (-90., -200., 90., 200.)


class TreatmentTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(barriers, 'corridor', return_value=[(65, np.array([[0., -600.], [0., 600.]]))]))
        self.enterContext(mock.patch.object(barriers, 'identify', side_effect=lambda f,p,d,c: (65, (p[-1,1]-p[0,1])*d > 0)))

    def test_northbound_then_southbound_median_shoulder_and_no_concrete(self):
        for lat, lon, side, sign in ((37.75,-85.81,'N',1), (37.90,-85.70,'S',-1)):
            for gap in (4., 30.):
                with self.subTest(side=side, gap=gap):
                    frame = geodesy.EnuFrame(lat,lon)
                    chunks = barriers.runs(frame, pair(gap), BOX, flat)
                    self.assertTrue(chunks)
                    self.assertTrue(all(c['barrier_type']=='cable' and c['side']==side for c in chunks))
                    x = np.concatenate([c['n'] for c in chunks])[:,0]
                    np.testing.assert_allclose(x, sign*(gap/2.-cablebarriers.EDGE_OFFSET), atol=1e-8)

    def test_reversed_source_and_tile_seams_keep_cables_and_posts_in_place(self):
        frame = geodesy.EnuFrame(37.75,-85.81)
        whole = barriers.runs(frame,pair(),BOX,flat)
        reverse = barriers.runs(frame,pair(reverse=True),BOX,flat)
        np.testing.assert_allclose(whole[0]['n'],reverse[0]['n'])
        np.testing.assert_allclose(whole[0]['posts'],reverse[0]['posts'])
        halves = [barriers.runs(frame,pair(),b,flat) for b in
                  ((-90.,-200.,90.,0.),(-90.,0.,90.,200.))]
        np.testing.assert_allclose(halves[0][0]['n'][-1],halves[1][0]['n'][0])
        posts = np.concatenate([c['posts'] for half in halves for c in half])
        np.testing.assert_allclose(posts,whole[0]['posts'])
        self.assertEqual(len(posts),len(np.unique(posts,axis=0)))

    def test_crossover_and_too_tight_opposing_pavement_remain_clear(self):
        frame = geodesy.EnuFrame(37.75,-85.81)
        cross = ('opening',signroads.Spec(6.,'service',False,0,False),
                 np.array([[-40.,0.,0.],[40.,0.,0.]]),6.)
        chunks = barriers.runs(frame,pair()+[cross],BOX,flat)
        self.assertEqual(len(chunks),2)
        self.assertTrue(all(abs(p[1]) > 7. for c in chunks for p in c['n']))
        self.assertEqual(barriers.runs(frame,pair(.4),BOX,flat),[])

    def test_precise_switch_and_restart_do_not_put_cable_across_median(self):
        frame = geodesy.EnuFrame(barriers.CABLE_SWITCH,-85.715595)
        chunks = barriers.runs(frame,pair(),BOX,flat)
        self.assertEqual({c['side'] for c in chunks},{'N','S'})
        for c in chunks:
            y = np.asarray(c['n'])[:,1]
            self.assertTrue(np.all(y <= .1) if c['side']=='N' else np.all(y >= -.1))
        frame = geodesy.EnuFrame(barriers.JERSEY_RESUME,-85.70071)
        chunks = barriers.runs(frame,pair(),BOX,flat)
        self.assertTrue(any(c.get('barrier_type')=='cable' for c in chunks))
        self.assertTrue(any(c.get('barrier_type')!='cable' for c in chunks))
        for c in chunks:
            y = np.asarray(c['n'])[:,1]
            self.assertTrue(np.all(y <= .1) if c.get('barrier_type')=='cable' else np.all(y >= -.1))

    def test_louisville_and_indiana_restart_and_ohio_river_is_excluded(self):
        for lat,lon,expected in ((37.67,-85.85,True), (38.15,-85.72,True),
                                 (38.30,-85.75,True),(38.389,-85.761,True),
                                 (38.40,-85.764,False),(38.265,-85.744,False)):
            frame = geodesy.EnuFrame(lat,lon)
            chunks = barriers.runs(frame,pair(),BOX,flat)
            self.assertEqual(bool(chunks),expected,(lat,lon))
            self.assertTrue(all(c.get('barrier_type')!='cable' for c in chunks))
        frame = geodesy.EnuFrame(38.35,-85.7544)
        self.assertTrue(barriers.runs(frame,pair(25.),BOX,flat), 'explicit extension also covers wider paired medians')

    def test_blue_posts_only_mount_on_concrete_including_indiana(self):
        for lat,lon,state,mile,expected in ((37.75,-85.81,'KY',100.2,False),
                                          (38.30,-85.75,'IN',2.2,True),
                                          (38.265,-85.744,'IN',.2,False)):
            frame = geodesy.EnuFrame(lat,lon)
            obj = milemarkers._object(state,65,mile,'N',[lon,lat],[0.,1.],'dot:'+state.lower())
            signs = milemarkers.place(frame,[obj],pair(),BOX,flat)
            self.assertEqual(bool(signs),expected)
            if signs:
                self.assertTrue(signs[0]['double_sided'])
                self.assertEqual(signs[0]['style'],'ky_blue')
                self.assertEqual(signs[0]['mile'],mile)

    def test_cable_geometry_is_colliding_metal_with_clear_space_between_wires(self):
        frame = geodesy.EnuFrame(37.75,-85.81)
        chunks = barriers.runs(frame,pair(),(-90.,-10.,90.,10.),flat)
        mesh = roadmesh.deck(chunks,(0.,0.,0.))
        p,t,uv,mats,mi = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertEqual(mats,[cablebarriers.METAL,cablebarriers.REFLECTOR])
        # Long faces belong to individual thin cables, never a hidden solid wall.
        triangle_points = p[t]
        long = np.ptp(triangle_points[:,:,1],axis=1) > .3
        self.assertTrue(long.any())
        self.assertLess(np.ptp(triangle_points[long,:,2],axis=1).max(),.03)
        heights = triangle_points[long,:,2].mean(1)
        self.assertTrue(np.all(np.min(abs(heights[:,None]-np.array(cablebarriers.CABLE_HEIGHTS)),axis=1)<.02))
        with tempfile.TemporaryDirectory() as tmp:
            path,_ = roads.write_tile(tmp,'/session',1,0,0,chunks,deck_mesh=mesh)
            result = json.loads((Path(tmp)/Path(path).name).read_text())
            self.assertIn('deck',result['colliders'])
            self.assertEqual(result['chunks'],[])

    def test_i75_and_other_states_are_unchanged(self):
        self.assertEqual(int(barriers.treatment(75,37.75,-84.30)),barriers.JERSEY)
        self.assertEqual(int(barriers.treatment(75,38.30,-85.75)),0)
        self.assertEqual(int(barriers.treatment(65,36.30,-86.70)),0)

    def test_urban_overlapping_shoulders_never_put_concrete_in_travel_lanes(self):
        frame = geodesy.EnuFrame(38.30,-85.75)
        for gap, expected in ((-.8,True),(-1.9,True),(-2.2,False),(-3.,False)):
            chunks = barriers.runs(frame,pair(gap),BOX,flat)
            self.assertEqual(bool(chunks),expected,gap)
            if chunks:
                p = barriers.build(chunks,(0,0,0))[0]
                self.assertLess(np.max(abs(p[:,0])), gap/2.+1.5-.3)


class IndianaInventoryTest(unittest.TestCase):
    def test_every_fifth_has_indiana_mileage_through_exit_nine(self):
        stations = dict(barriers.station_sets())['IN']
        self.assertEqual([round(m*5) for m in stations[:,1]],list(range(1,46)))
        self.assertTrue(np.all(stations[:,0]==65))
        self.assertTrue(np.all(stations[:,3] <= barriers.INDIANA_END))
        np.testing.assert_allclose(np.linalg.norm(stations[:,4:6],axis=1),1.)
        objects,_ = milemarkers.references()
        objects = [o for o in objects if o['mile_marker']['state']=='IN' and o['mile_marker']['route']==65]
        self.assertEqual(len(objects),45)
        self.assertEqual({o['id'] for o in objects},
                         {milemarkers.marker_id('IN',65,t/5,'N') for t in range(1,46)})

    def test_indiana_paths_identify_both_directions_and_cables_have_packaged_materials(self):
        frame = geodesy.EnuFrame(38.36327,-85.75485)
        corridors = barriers.corridor(frame,(-200,-200,200,200))
        self.assertTrue(corridors)
        identities = [barriers.identify(frame,np.c_[p,np.zeros(len(p))],1,corridors) for r,p in corridors]
        self.assertIn((65,True),identities)
        self.assertIn((65,False),identities)
        path = Path(__file__).resolve().parents[2]/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/tunnel.materials.json'
        materials = json.loads(path.read_text())
        for name in (cablebarriers.METAL,cablebarriers.REFLECTOR):
            self.assertEqual(materials[name]['groundType'],'METAL')
            self.assertFalse(materials[name]['translucent'])


if __name__ == '__main__':
    unittest.main()
