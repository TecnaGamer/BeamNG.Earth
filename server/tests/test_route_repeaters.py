import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
import duckdb
import numpy as np
import routerepeaters as repeat
import routemarkers
import roads
import routeindex
from test_roads import FRAME, flat
from test_highway_signs import fixture


def corridor(two_way=False):
    return [(i+1, [[-85.1,34.7+i*.02],[-85.1,34.7+(i+1)*.02]],
             dict(highway='primary' if two_way else 'motorway',ref='US 41' if two_way else 'I 75',
                  oneway='no' if two_way else 'yes')) for i in range(30)]


class RouteRepeatersTest(unittest.TestCase):
    def test_spacing_directions_and_adjacent_queries_are_stable(self):
        for two in (False,True):
            candidates=repeat.candidates(corridor(two))
            box=(-85.2,34.75,-85.,35.25)
            all_=repeat.thin(candidates,box)
            left=repeat.thin(candidates,(-85.2,34.75,-85.,35.))
            right=repeat.thin(candidates,(-85.2,35.,-85.,35.25))
            self.assertEqual({o['id'] for o in all_},{o['id'] for o in left+right})
            self.assertEqual(len(left+right),len(all_))
            self.assertGreater(len(all_),6)
            for a in all_:
                for b in all_:
                    if a['id']!=b['id'] and repeat.keys(a)&repeat.keys(b):
                        self.assertGreaterEqual(repeat.distance(a,b),repeat.SEPARATION_M)
            directions={r['direction'] for o in all_ for r in o['roadside']['routes']}
            self.assertEqual(directions,{'NORTH','SOUTH'} if two else {'NORTH'})

    def test_no_invented_route_or_ramp_bridge_private_signs(self):
        way=corridor()[0]
        for change in ({'ref':'75'}, {'ref':'GA 2'}, {'bridge':'yes'}, {'tunnel':'yes'},
                       {'access':'private'}, {'junction':'roundabout'}, {'highway':'motorway_link'}):
            self.assertEqual(repeat.candidates([(way[0],way[1],dict(way[2],**change))]),[])

    def test_source_reuses_cells_and_prefers_existing_route_sign(self):
        source=repeat.Source('unused',None)
        source.tags.version=lambda: ('test',None)
        with mock.patch.object(repeat,'read_routes',return_value=corridor()) as read:
            first=source.query(-85.2,34.8,-85.,35.1)
            self.assertTrue(first)
            calls=read.call_count
            again=source.query(-85.2,34.8,-85.,35.1,existing=[first[0]])
            self.assertEqual(read.call_count,calls)
            self.assertNotIn(first[0],again)

    def test_real_parquet_ref_filter_and_full_geometry(self):
        # Exercise the actual DuckDB SQL and WKB path, not a mocked list of refs.
        with tempfile.TemporaryDirectory() as tmp:
            con=duckdb.connect()
            path=Path(tmp)/'highways.parquet'
            con.execute('CREATE TABLE highways(id BIGINT, highway VARCHAR, ref VARCHAR, oneway VARCHAR, geometry BLOB, '
                        'bbox STRUCT(xmin FLOAT,ymin FLOAT,xmax FLOAT,ymax FLOAT))')
            wkb=struct.pack('<BII4d',1,2,2,-85.1,34.9,-85.1,35.)
            for wid,cls,ref in ((1,'motorway','I 75'),(2,'primary','US 41'),(3,'motorway_link','I 75'),(4,'primary','GA 2')):
                con.execute('INSERT INTO highways VALUES (?,?,?,?,?,?)',[wid,cls,ref,'yes',wkb,
                            dict(xmin=-85.1,ymin=34.9,xmax=-85.1,ymax=35.)])
            con.execute(f"COPY highways TO '{path.as_posix()}' (FORMAT PARQUET)")
            con.close()
            class Connection:
                def __init__(self): self.con=duckdb.connect()
                def execute(self,sql):
                    return self.con.execute(sql.replace("'test://osm/highways.parquet'",repr(path.as_posix())))
                def close(self): self.con.close()
            columns={'id':[], 'bbox':[], 'ref':[], 'highway':[], 'oneway':[], 'geometry':[]}
            tags=SimpleNamespace(_reader=SimpleNamespace(_connect=lambda *a:(Connection(),'test',[]),_fetch=lambda *a:None),
                                 _groups=lambda *a:{0:dict(first=0,n=4,cols=columns)},_candidates=lambda *a:[0])
            ways=repeat.read_routes(tags,None,(-85.2,34.8,-85.,35.1))
            self.assertEqual({w[0] for w in ways},{1,2})
            self.assertEqual({w[2]['ref'] for w in ways},{'I 75','US 41'})
            np.testing.assert_allclose(ways[0][1],[[-85.1,34.9],[-85.1,35.]])

    def test_source_retries_a_replaced_osm_snapshot(self):
        source=repeat.Source('unused',None)
        source.tags.version=mock.Mock(side_effect=[('old',None),('new',None)])
        source.tags._forget=mock.Mock()
        with mock.patch.object(repeat,'read_routes',side_effect=[repeat.osmtags.rangeread.Changed(),corridor()]) as read:
            source._cell(-852,349)
        self.assertEqual(read.call_count,2)
        source.tags._forget.assert_called_once()
        self.assertEqual(list(source.cache),[('new',-852,349)])

    def test_shield_mesh_places_clear_of_widened_pavement(self):
        with tempfile.TemporaryDirectory() as tmp:
            md=fixture(tmp)
            context=md.highway_signs.context(None,None)
            la,lo,_=FRAME.enu_to_geodetic(150.,100.,0.)
            path=[]
            for x in (90.,210.):
                lat,lon,_=FRAME.enu_to_geodetic(x,100.,0.)
                path.append([float(lon),float(lat)])
            obj=dict(id=-999,lat=float(la),lon=float(lo),roadside=dict(type='shield',main_id=10,approach=path,
                     routes=[dict(kind='interstate',number='75',direction='NORTH')]))
            pavements=routemarkers.source_pavements(FRAME,obj,context)
            placed=routemarkers.place(FRAME,obj,pavements,(-500.,-500.,500.,500.),flat)
            self.assertIsNotNone(placed)
            self.assertLess(placed['centre'][1]+placed['width']/2,100.-roads.carriageway('motorway','paved',False,oneway=True)/2)
            mesh=routemarkers.build([placed],(0.,0.,0.))
            self.assertGreater(len(mesh[0]),100)
            self.assertTrue(np.isfinite(mesh[0]).all())


if __name__=='__main__': unittest.main()
