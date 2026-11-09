"""Optional perimeter failures cannot remove driveable road/bridge content."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'server/tests')]
import blockworker
import facilityperimeters as fp
import mapgeom
import overture
import perimeters as p
import revisit
import roads
from test_blockworker import FakeMapData, FakeDem
from test_roads import FRAME, LAT, LON, features


class AvailabilityTest(unittest.TestCase):
    def test_road_tile_survives_optional_source_and_terrain_failures(self):
        f = features([[(-300, 100), (300, 100)]], ['secondary'])
        md = FakeMapData(f, roads.cells_of_tile(FRAME, 0, 0)[0])
        for failure in ('source', 'terrain'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as cache, tempfile.TemporaryDirectory() as out:
                state = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None,
                    sea=None, lock=threading.Lock(), cache_dir=cache, bridges={}, anchor=lambda *a: (None,None))
                def partial_rows(*args, stage_report, **kwargs):
                    stage_report.update(network_digest='regression', planned_sections=2,
                        generated_sections=1, terrain_failures={'mapped-fence':1}, placement_status='terrain_failure')
                    return [['perimeter', 0.,0.,10.,4.,0.,10.,'valid-panel','mapped-fence']]
                context = {layer:mapgeom.empty(layer) for layer in p.LAYERS+('infrastructure','land_use')}
                context['roads'] = []
                before = revisit.thread_faults()
                with patch.object(blockworker, '_s', state), patch.object(fp, 'context',
                        side_effect=ValueError('missing source') if failure=='source' else None,
                        return_value=context), patch.object(p, 'rows', side_effect=partial_rows):
                    result = blockworker.roads_tile(1,LAT,LON,15,0,0,out,'/temp/x',True,.05)
                self.assertGreater(result[1],0)
                data = json.loads((Path(out)/Path(result[0]).name).read_text())
                self.assertGreater(revisit.thread_faults(),before)
                if failure=='source':
                    self.assertEqual(data['perimeters'],[])
                    self.assertIn('missing source',data['perimeter_status']['error'])
                else:
                    self.assertEqual(len(data['perimeters']),1)
                    self.assertEqual(data['perimeter_status']['placement_status'],'terrain_failure')


class RailYardTest(unittest.TestCase):
    def test_real_complete_cheyenne_and_laramie_yards_generate_boundary_panels(self):
        fixture=json.loads((ROOT/'server/tests/fixtures/rail_yard_perimeters.geojson').read_text())
        self.assertEqual(len(fixture['features']),3)
        for feature in fixture['features']:
            props=feature['properties']
            with self.subTest(source=props['src_record']):
                rings=[np.asarray(ring) for ring in feature['geometry']['coordinates']]
                feat=mapgeom.build('perimeter_areas',np.array([props['src_record']]),[(mapgeom.POLYGON,[rings])])
                feat.attrs={key:np.array([json.dumps(value) if key=='tags' else value])
                    for key,value in props.items() if key in ('class','tags','src_record','src_dataset')}
                feat.clipped=np.array([False])
                xy=rings[0];lon,lat=xy.mean(axis=0)
                import geodesy
                frame=geodesy.EnuFrame(lat,lon)
                e,n=mapgeom.enu_xy(feat,frame,xy)
                bounds=(float(e.min()-10),float(n.min()-10),float(e.max()+10),float(n.max()+10))
                generated=p.rows(frame,lambda key:[feat],lambda key:None,bounds,None,
                    surface=lambda points:np.full(len(points),10.),extra_evidence=[])
                self.assertGreater(len(generated),100)
                from shapely.geometry import LineString,Point
                boundary=LineString(np.c_[e,n])
                self.assertTrue(all(boundary.distance(Point(row[1],row[2]))<.02 for row in generated))

    def test_source_query_accepts_railway_landuse_and_normalized_class(self):
        import duckdb
        c=duckdb.connect()
        try:
            for tags,cls in [('{"landuse":"railway"}',''),('{}','railway')]:
                row=c.execute('SELECT 1 FROM (SELECT ? AS tags, ? AS class) WHERE '+overture._PERIMETER_WHERE,[tags,cls]).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(p.classify(json.loads(tags),cls),'industrial')
        finally:c.close()
        # Tracks are transport, never standalone facility boundaries.
        self.assertIsNone(p.classify({'railway':'rail'}))


if __name__=='__main__':unittest.main()
