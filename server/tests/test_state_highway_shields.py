"""Route identity, reproducible faces, junction stations and streamed state meshes."""
import json
import importlib.util
from pathlib import Path
import sys
import tempfile
import shutil
import unittest
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import highwayroutes as routes
import routefaces
import routeindex
import routemarkers
import signplacement
from test_roads import FRAME,flat
from test_highway_signs import fixture


def relation(network,ref,**kwargs):
    return dict(dict(type='route',route='road',network=network,ref=ref),**kwargs)


class RecognitionTest(unittest.TestCase):
    def test_relations_prefer_network_and_preserve_concurrency(self):
        got=routes.recognize({'ref':'WY 211;US 30;I 80'},
            [relation('US:WY','210'),relation('US:US','30'),relation('US:I','80')])
        self.assertEqual(got,[routes.Route('US:I','80'),routes.Route('US:US','30'),routes.Route('US:WY','210')])

    def test_same_number_different_state_and_federal_networks_stay_distinct(self):
        got=routes.recognize({},[relation(n,'30') for n in ('US:I','US:US','US:WY','US:CO')])
        self.assertEqual(len(got),4)
        self.assertEqual({routes.panel(r)['state'] for r in got},{'','WY','CO'})

    def test_no_road_class_or_destination_inference(self):
        for highway in ('primary','secondary','trunk'):
            self.assertEqual(routes.recognize({'highway':highway,'ref':'210','destination:ref':'WY 210'},state='WY'),[])
        self.assertEqual(routes.recognize({'destination:ref':'WY 210'},destination=True),[routes.Route('US:WY','210')])

    def test_state_context_and_explicit_network(self):
        self.assertEqual(routes.recognize({'ref':'SH 210'},state='CO'),[routes.Route('US:CO','210')])
        self.assertEqual(routes.recognize({'ref':'210','network':'US:WY'}),[routes.Route('US:WY','210')])
        self.assertEqual(routes.recognize({'ref':'WYO 210'}),[routes.Route('US:WY','210')])

    def test_relation_types_and_texas_subnetworks(self):
        self.assertEqual(routes.recognize({},[relation('US:WY','210',type='restriction')]),[])
        for subtype in ('FM','RM','Loop','Spur'):
            self.assertEqual(routes.recognize({},[relation('US:TX:'+subtype,'1234')])[0].network,
                             'US:TX:'+subtype.upper())
        self.assertEqual(routes.recognize({},[relation('US:I:Business:Loop','80')]),
                         [routes.Route('US:I','80','BUSINESS LOOP')])

    def test_registry_expansion_is_configuration_only(self):
        reg=routes.registry()
        states=[c for c in reg['networks'].values() if c.get('state') and c.get('kind')=='state']
        self.assertEqual(len({c['state'] for c in states}),50)
        self.assertEqual(reg['networks']['US:CO']['template'],'co')
        self.assertEqual(reg['networks']['US:TX:FM']['template'],'tx_fm')
        self.assertEqual(routes.recognize({},[relation('US:WY:Unknown','210')]),[])


class StationTest(unittest.TestCase):
    def network(self):
        coords={1:(-105.2,41.2),2:(-105.18,41.2),3:(-105.16,41.2),4:(-105.14,41.2),
                5:(-105.18,41.19),6:(-105.18,41.21)}
        tags=dict(highway='primary',ref='WY 210')
        return [(10,[1,2],tags),(11,[2,3,4],tags)],coords

    def test_way_splits_do_not_restart_stations_or_use_intervals(self):
        ways,coords=self.network()
        objects=routeindex.index_records(ways,coords)
        self.assertEqual(len(objects),2)
        merged=routeindex.index_records([(10,[1,2,3,4],ways[0][2])],coords)
        self.assertEqual([o['id'] for o in objects],[o['id'] for o in merged])
        self.assertEqual(objects,routeindex.index_records(list(reversed(ways)),coords))

    def test_junction_creates_downstream_markers_and_oneway(self):
        ways,coords=self.network()
        ways.append((12,[5,2,6],dict(highway='secondary')))
        self.assertEqual(len(routeindex.index_records(ways,coords)),4)
        one=[(w,r,dict(t,oneway='yes')) for w,r,t in ways]
        self.assertEqual(len(routeindex.index_records(one,coords)),2)

    def test_mapped_marker_wins_and_dedup_does_not_merge_other_network(self):
        ways,coords=self.network()
        point=(-105.1988,41.2)
        mapped=routeindex.mapped_records(ways,coords,{},[(999,point,
            dict(traffic_sign='US:M1-5',ref='WY 210',direction='forward'))])
        self.assertEqual(len(mapped),1)
        self.assertTrue(mapped[0]['ways'][0]['mapped'])
        got=routeindex.unique(mapped+routeindex.index_records(ways,coords))
        self.assertIn(mapped[0]['id'],[o['id'] for o in got])
        self.assertEqual(len(got),2)  # one per direction

    def test_bundled_pilot_has_wy210_us30_and_i80(self):
        objects=json.loads((Path(__file__).parent/'fixtures/wyoming_route_shields.json').read_text())['objects']
        keys={(r['network'],r['number']) for o in objects for r in o['roadside']['routes']}
        self.assertTrue({('US:WY','210'),('US:US','30'),('US:I','80')}<=keys)
        self.assertEqual(len(objects),len({o['id'] for o in objects}))

    def test_state_fallback_at_border_and_relation_identity(self):
        coords={1:(-105.2,41.03),2:(-105.2,40.97)}
        ways=[(10,[1,2],dict(highway='primary',ref='SR 210'))]
        objects=routeindex.index_records(ways,coords)
        self.assertEqual({o['ways'][0]['routes'][0]['network'] for o in objects},{'US:WY','US:CO'})
        objects=routeindex.index_records(ways,coords,{10:[relation('US:I','80')]})
        self.assertTrue(all(any(r['network']=='US:I' for r in o['ways'][0]['routes']) for o in objects))


class FacesTest(unittest.TestCase):
    def test_missing_template_uses_labelled_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('networks.json','lettering.json','lettering.png'):
                shutil.copy(routefaces.DATA/name,Path(tmp)/name)
            with mock.patch.object(routefaces,'DATA',Path(tmp)):
                routefaces.raster.cache_clear();routefaces.signature.cache_clear()
                try:
                    pixels=np.frombuffer(routefaces.raster('US:WY','210'),np.uint8).reshape(512,512,3)
                    self.assertTrue((pixels[140,200]==255).all())
                    self.assertTrue(routefaces.face(routes.panel(routes.Route('US:WY','210')),tmp,'/temp')[0])
                finally:
                    routefaces.raster.cache_clear();routefaces.signature.cache_clear()

    def test_digits_horse_and_unavailable_state_fallback(self):
        base=np.asarray(Image.open(routefaces.DATA/'wy.png').convert('RGB'))
        for number in ('7','30','210','1234'):
            face=np.frombuffer(routefaces.raster('US:WY',number),np.uint8).reshape(512,512,3)
            self.assertGreater(np.count_nonzero(face[170:355]!=base[170:355]),500)
            np.testing.assert_array_equal(face[375:480,210:310],base[375:480,210:310])
        self.assertNotEqual(routefaces.raster('US:CO','210'),routefaces.raster('US:WY','210'))

    def test_dds_cache_key_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=routes.panel(routes.Route('US:WY','210'))
            a=routefaces.face(r,tmp,'/temp/test')
            path=Path(tmp)/(a[0]+'.dds')
            self.assertEqual(path.read_bytes()[:4],b'DDS ')
            with mock.patch('imagery.write_dds',side_effect=AssertionError('cache missed')):
                self.assertEqual(routefaces.face(r,tmp,'/temp/test'),a)
            self.assertNotEqual(routefaces.face(dict(r,number='211'),tmp,'/temp/test'),a)

    def test_mesh_front_uv_posts_and_building_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            md=fixture(tmp);context=md.highway_signs.context(None,None)
            path=[]
            for x in (90.,210.):
                lat,lon,_=FRAME.enu_to_geodetic(x,100.,0.);path.append([float(lon),float(lat)])
            lat,lon,_=FRAME.enu_to_geodetic(150.,100.,0.)
            route=routes.panel(routes.Route('US:WY','210'),'EAST')
            obj=dict(id=-999,lat=float(lat),lon=float(lon),roadside=dict(type='shield',main_id=10,
                     approach=path,routes=[route]))
            pavements=routemarkers.source_pavements(FRAME,obj,context)
            box=(-500.,-500.,500.,500.)
            placed=routemarkers.place(FRAME,obj,pavements,box,flat)
            self.assertIsNotNone(placed)
            faces={}
            p,t,uv,mats,mi=routemarkers.build([placed],(0.,0.,0.),tmp,'/temp/test',faces)
            triangles=t[mi==mats.index(next(iter(faces)))];normals=np.cross(p[triangles[:,1]]-p[triangles[:,0]],p[triangles[:,2]]-p[triangles[:,0]])
            self.assertTrue((normals@np.r_[-np.asarray(placed['travel']),0.]>0).all())
            face_vertices=np.unique(triangles)
            self.assertLess(uv[face_vertices[np.argmin(p[face_vertices,2])],1],.1)
            blocked=signplacement.Context([np.array([[-500,-500],[500,-500],[500,500],[-500,500]])])
            self.assertIsNone(routemarkers.place(FRAME,obj,pavements,box,flat,obstacles=blocked))
            self.assertIsNone(routemarkers.place(FRAME,obj,pavements,(0.,200.,500.,500.),flat))
            wide_walks=[(fid,sp._replace(walk=7.,sides=(1,-1)),line,width) for fid,sp,line,width in pavements]
            shifted=routemarkers.place(FRAME,obj,wide_walks,box,flat)
            self.assertTrue(shifted is None or shifted['centre'][1]<placed['centre'][1]-4.)

    def test_state_faces_flow_through_existing_stream_manifest(self):
        import highwaysigns
        import signstream
        with tempfile.TemporaryDirectory() as tmp:
            md=fixture(tmp)
            path=[]
            for x in (90.,210.):
                lat,lon,_=FRAME.enu_to_geodetic(x,100.,0.);path.append([float(lon),float(lat)])
            lat,lon,_=FRAME.enu_to_geodetic(150.,100.,0.)
            obj=dict(id=-999,lat=float(lat),lon=float(lon),roadside=dict(type='shield',main_id=10,
                approach=path,routes=[routes.panel(routes.Route('US:WY','210'),'EAST')]))
            with mock.patch.object(highwaysigns,'tile_objects',return_value=[obj]), \
                 mock.patch.object(highwaysigns,'guide_layout',return_value=([],[])), \
                 mock.patch.object(signplacement,'for_tile',return_value=signplacement.Context()), \
                 mock.patch.object(signstream,'_speeds',return_value=None):
                result=signstream.build_tile(md,FRAME,1,0,0,flat,tmp,'/temp/shields')
            manifest=json.loads((Path(tmp)/Path(result[0]).name).read_text())
            self.assertIn('exits',manifest)
            self.assertEqual(len(manifest['dirs_faces']),1)
            for name,texture in manifest['dirs_faces'].items():
                self.assertTrue(name.startswith('bne_route_'))
                self.assertTrue((Path(tmp)/Path(texture).name).exists())


class BuilderTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('osmium'),'pyosmium is a build-only dependency')
    def test_pbf_relation_only_secondary_route_reaches_shard_reader(self):
        import osmium
        root=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(root/'tools'))
        import build_sign_tags as builder
        import railtags
        with tempfile.TemporaryDirectory() as tmp:
            xml=Path(tmp)/'route.osm';pbf=Path(tmp)/'route.osm.pbf'
            xml.write_text('<osm version="0.6">'
                '<node id="1" version="1" lon="-105.2" lat="41.2"/>'
                '<node id="2" version="1" lon="-105.18" lat="41.2"/>'
                '<way id="10" version="1"><nd ref="1"/><nd ref="2"/>'
                '<tag k="highway" v="secondary"/></way>'
                '<relation id="20" version="1"><member type="way" ref="10" role=""/>'
                '<tag k="type" v="route"/><tag k="route" v="road"/>'
                '<tag k="network" v="US:WY"/><tag k="ref" v="210"/></relation></osm>',encoding='utf-8')
            with osmium.SimpleWriter(str(pbf)) as writer:
                for element in osmium.FileProcessor(str(xml)): writer.add(element)
            ways,links,junctions,milestones,relations,rows=builder.first_pass(str(pbf))
            self.assertEqual([w for w,_,_ in ways],[10])
            objects=routeindex.index_records([(w,r.tolist(),t) for w,r,t in ways],
                                             {1:(-105.2,41.2),2:(-105.18,41.2)},relations)
            self.assertEqual(len(objects),2)
            records=[builder._record('shield',o['id'],o['lon'],o['lat'],o['ways'][0]) for o in objects]
            out=Path(tmp)/'shards';builder.write(str(out),records,{'shields':len(records)})
            decoded=railtags.SignTags(None,None,local=str(out)).query(-105.3,41.,-105.,41.4)
            self.assertEqual(len(decoded),2)
            self.assertEqual(decoded[0]['roadside']['routes'][0]['network'],'US:WY')


if __name__=='__main__': unittest.main()
