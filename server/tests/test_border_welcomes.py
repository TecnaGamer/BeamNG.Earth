"""Mapped priority and highway-border fallback through real source/placement rules."""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import numpy as np
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).parent)]
import travelboards as boards
import signstates,signroads
from test_travelboards import rows,states,ll
from test_roads import FRAME,LON,LAT,flat


class BorderWelcomesTest(unittest.TestCase):
    def test_numbered_us_state_routes_and_interstates_use_large_boards(self):
        road=rows()[0]
        for network,ref in [('US:I','80'),('US:US','30'),('us:wy','210'),('US:TX:Loop','12'),('US:TX:Spur','330')]:
            road['routes']=[(network,ref,0.,1.)]
            self.assertTrue(boards.welcome_highway(road),(network,ref))
        for network,ref in [('', ''),('US:TX:FM','1960'),('US:TX:RM','1431'),
                            ('US:TX','FM 1960'),('US:TX','RM-1431'),('US:WY:County','12'),
                            ('US:AZ:scenic','Kayenta Monument Valley Scenic Road'),('US:WY','Happy Jack Road')]:
            road['routes']=[(network,ref,0.,1.)]
            self.assertFalse(boards.welcome_highway(road),(network,ref))
        road['routes']=[('US:US','30',0.,1.),('US:WY','210',0.,1.)]
        self.assertTrue(boards.welcome_highway(road))
        road['routes']=[]
        road['spec']=signroads.Spec(14.,'motorway',True,1,False)
        road['name']='Interstate 80'
        self.assertFalse(boards.welcome_highway(road))

    def test_partial_route_membership_does_not_qualify_the_rest_of_a_way(self):
        road=rows()[0];road['routes']=[('us:wy','210',.5,1.)]
        self.assertFalse(boards.welcome_highway(road,ll(-200)))
        self.assertTrue(boards.welcome_highway(road,ll(200)))

    def test_ordinary_roads_receive_no_welcome_boards(self):
        road=rows()[0];road['routes']=[]
        node=dict(id=999,lon=float(ll(90,-18)[0]),lat=float(ll(90,-18)[1]),
                  tags=dict(board_type='welcome_sign',name='Welcome to Wyoming'))
        self.assertEqual(boards.mapped_welcomes([node],[road]),[])
        self.assertEqual(self.crossings([road]),[])
        road['routes']=[('US:US','30',0.,1.)]
        self.assertEqual(len(boards.mapped_welcomes([node],[road])),1)
        self.assertEqual(len(self.crossings([road])),2)

    def test_nearer_track_node_does_not_become_a_highway_board(self):
        highway=rows()[0];highway['routes']=[('US:I','94',0.,1.)]
        track=dict(rows(cls='track')[0],fid='dirt-track',
                   path=np.array([ll(-100,-15),ll(100,-15)]),routes=[])
        node=dict(id=6265416195,lon=float(ll(90,-18)[0]),lat=float(ll(90,-18)[1]),
                  tags=dict(board_type='welcome_sign',name='Welcome to Wyoming'))
        self.assertEqual(len(boards.mapped_welcomes([node],[highway])),1)
        self.assertEqual(boards.mapped_welcomes([node],[highway,track]),[])

    def test_legacy_small_board_record_is_not_rendered(self):
        old=dict(id=-1,travelboard=dict(kind='welcome',state='MI',welcome_size='small'))
        with mock.patch.object(boards,'place',side_effect=AssertionError('small board placed')):
            with mock.patch.object(boards,'render',return_value=None) as render:
                boards.build_tile(None,FRAME,[old],None,(-256,-256,256,256),flat,(),'/tmp','/probe',{})
        self.assertEqual(render.call_args.args[0],[])

    def test_private_driveway_does_not_receive_a_board(self):
        road=rows()[0];road['subclass']='driveway'
        self.assertEqual(self.crossings([road]),[])

    def test_border_sign_moves_past_a_side_road(self):
        road=rows()[0]
        spur=dict(rows(cls='service')[0],fid='side-road',path=np.array([ll(90,-100),ll(90,100)]),routes=[])
        found=self.crossings([road,spur])
        self.assertEqual(len(found),2)
        wy=next(o for o in found if o['travelboard']['state']=='WY')
        self.assertGreater((wy['lon']-LON)*111320*np.cos(np.radians(LAT)),130.)

    def crossings(self,roads):
        with mock.patch.object(signstates,'states',side_effect=states),mock.patch.object(boards,'boundary_near',return_value=True):
            return boards.border_welcomes(roads)

    def test_unmapped_crossing_has_receiving_state_in_both_directions(self):
        result=self.crossings(rows())
        self.assertEqual({o['travelboard']['state'] for o in result},{'CO','WY'})
        self.assertTrue(all(o['travelboard']['inferred'] for o in result))
        self.assertEqual(len(self.crossings(rows(oneway=True))),1)

    def test_split_crossing_uses_receiving_road_and_does_not_duplicate(self):
        result=self.crossings(rows(split=True))
        self.assertEqual(len(result),2)
        self.assertEqual(next(o for o in result if o['travelboard']['state']=='WY')['travelboard']['main_id'],11)
        repeated=self.crossings(rows(split=True)*2)
        self.assertEqual({o['id'] for o in result},{o['id'] for o in repeated})

    def test_bridge_crossing_moves_to_ground_beyond_border(self):
        road=rows()[0]
        road['ground']=[np.array([ll(-400),ll(-220)]),np.array([ll(220),ll(400)])]
        result=self.crossings([road])
        self.assertEqual(len(result),2)
        for obj in result:self.assertGreater(abs((obj['lon']-LON)*111320*np.cos(np.radians(LAT))),220)
        road['ground']=[]
        self.assertEqual(self.crossings([road]),[])

    def test_no_borders_for_links_or_constant_state_or_international_gap(self):
        with mock.patch.object(signstates,'states',return_value=np.array(['WY'])),mock.patch.object(boards,'boundary_near',return_value=False):
            self.assertEqual(boards.border_welcomes(rows()),[])
        link=rows()[0];link['spec']=signroads.Spec(8.,'motorway',True,1,True)
        self.assertEqual(self.crossings([link]),[])
        with mock.patch.object(signstates,'states',side_effect=lambda lat,lon:np.where(np.asarray(lon)<LON,'','WY')),mock.patch.object(boards,'boundary_near',return_value=True):
            self.assertEqual(boards.border_welcomes(rows()),[])

    def test_mapped_priority_follows_direction_and_road_not_other_crossings(self):
        inferred=self.crossings(rows());wy=next(o for o in inferred if o['travelboard']['state']=='WY')
        mapped=copy.deepcopy(wy);mapped.update(id=-123,lat=LAT+.0001)
        mapped['travelboard'].update(mapped=True,osm_node_id=123,inferred=False)
        result=boards.welcome_preference(inferred,[mapped])
        self.assertEqual(wy['travelboard']['mapped_alternative']['id'],-123)
        co=next(o for o in result if o['travelboard']['state']=='CO')
        self.assertNotIn('mapped_alternative',co['travelboard'])
        other=copy.deepcopy(mapped);other['lat']=LAT+.001
        other['travelboard'].update(feature_id='frontage-road')
        fresh=self.crossings(rows())
        boards.welcome_preference(fresh,[other])
        self.assertFalse(any('mapped_alternative' in o['travelboard'] for o in fresh))

    def test_safe_mapped_position_suppresses_fallback_across_owner_tiles(self):
        inferred=self.crossings(rows());fallback=next(o for o in inferred if o['travelboard']['state']=='WY')
        mapped=copy.deepcopy(fallback);mapped['id']=-456;mapped['travelboard'].update(mapped=True,osm_node_id=456)
        fallback['travelboard']['mapped_alternative']=mapped
        fake=dict(id=-456,extra=mapped['travelboard'],centre=[0,0,0],width=4.8)
        with mock.patch.object(boards.signplacement,'for_tile'),mock.patch.object(boards,'place',return_value=fake) as place, mock.patch.object(boards,'render',return_value=None) as render:
            boards.build_tile(None,FRAME,[fallback],None,(-256,-256,256,256),flat,(),'/tmp','/probe',{})
        self.assertEqual(place.call_count,1)
        self.assertEqual(render.call_args.args[0],[])
        with mock.patch.object(boards.signplacement,'for_tile'),mock.patch.object(boards,'place',side_effect=[None,dict(fake,id=fallback['id'])]),mock.patch.object(boards,'render',return_value=None) as render:
            boards.build_tile(None,FRAME,[fallback],None,(-256,-256,256,256),flat,(),'/tmp','/probe',{})
        self.assertEqual(len(render.call_args.args[0]),1)

    def test_mapped_sign_uses_panel_clearance_not_twenty_metre_exclusion(self):
        obj=next(o for o in self.crossings(rows()) if o['travelboard']['state']=='WY')
        obj['travelboard'].update(mapped=True)
        obj['lat']=float(ll(90,-18)[1]);obj['lon']=float(ll(90,-18)[0])
        sp=signroads.Spec(8.,'primary',False,0,False)
        context=SimpleNamespace(pavements=lambda *args:[('osm:w10:id000',sp,np.array([[-400.,0.,0.],[400.,0.,0.]]),8.)])
        # Use the fixture feature id so the real pavement matching path runs.
        obj['travelboard']['feature_id']='osm:w10:id000'
        box=(-256,-256,256,256)
        placed=boards.place(FRAME,obj,context,box,flat)
        self.assertIsNotNone(placed)
        other=dict(centre=(np.asarray(placed['centre'])+[0,8,0]).tolist(),travel=placed['travel'],width=5.5,height=3.,bottom=2.4)
        self.assertIsNotNone(boards.place(FRAME,obj,context,box,flat,existing=[other]))
        other['centre']=placed['centre']
        self.assertIsNone(boards.place(FRAME,obj,context,box,flat,existing=[other]))

    def test_inferred_highway_welcome_can_stand_near_a_clear_guide(self):
        obj=next(o for o in self.crossings(rows()) if o['travelboard']['state']=='WY')
        sp=signroads.Spec(8.,'primary',False,0,False)
        obj['travelboard']['feature_id']='osm:w10:id000'
        context=SimpleNamespace(pavements=lambda *args:[('osm:w10:id000',sp,np.array([[-400.,0.,0.],[400.,0.,0.]]),8.)])
        box=(-256,-256,256,256)
        with mock.patch.object(signstates,'states',side_effect=states):
            placed=boards.place(FRAME,obj,context,box,flat)
            self.assertIsNotNone(placed)
            guide=dict(centre=(np.asarray(placed['centre'])+np.r_[np.asarray(placed['travel'])*12.,0.]).tolist(),
                       travel=placed['travel'],width=7.4,height=3.,bottom=2.,kind='advance')
            self.assertIsNotNone(boards.place(FRAME,obj,context,box,flat,existing=[guide]))
            guide['centre']=placed['centre']
            shifted=boards.place(FRAME,obj,context,box,flat,existing=[guide])
            if shifted is not None:
                import signlayout
                self.assertFalse(signlayout.overlap(dict(shifted,bottom=0.),dict(guide,bottom=0.),clearance=2.))


if __name__=='__main__':unittest.main()
