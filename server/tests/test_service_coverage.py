import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'server'))
sys.path.insert(0,str(ROOT/'tools'))
import servicecoverage as coverage
import servicesigns
import build_us_service_signs as builder
from test_service_signs import osm
import servicesigns_index as index


def publish(folder,objects,k='-170_83',built='test'):
    raw = gzip.compress(json.dumps(dict(format=1,objects=objects)).encode(),mtime=0)
    path = Path(folder)/'test'/f'{k}.json.gz'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(raw)
    entry = dict(path=f'test/{k}.json.gz',sha256=hashlib.sha256(raw).hexdigest(),objects=len(objects))
    coverage.atomic_json(Path(folder)/'manifest.json',dict(format=1,cell_degrees=.5,built=built,cells={k:entry}))
    return entry


class CoverageTest(unittest.TestCase):
    def test_controlled_trunks_and_local_trunk_access(self):
        from test_service_signs import records
        data=osm()
        data['elements'][-2]['tags']['highway']='trunk'
        self.assertEqual(len(records(data)),1)
        data['elements'][-4]['tags'].update(highway='trunk',expressway='yes')
        data['elements'][-3]['tags']['highway']='trunk_link'
        self.assertEqual(len(records(data)),1)
        del data['elements'][-4]['tags']['expressway']
        self.assertEqual(records(data),[])

    def test_completed_cell_overrides_fallback_and_empty_cell_retires_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'national'
            local=Path(tmp)/'local.json'
            obj=dict(id='service:1',lon=-84.9,lat=41.7,exit={'label':{'ref':'1'}})
            local.write_text(json.dumps(dict(format=1,objects=[obj])))
            source=servicesigns.Source(local,directory)
            box=(-84.99,41.6,-84.8,41.8)
            self.assertEqual(source.query(*box)[0]['service_exit']['label']['ref'],'1')
            national=dict(obj,exit={'label':{'ref':'2'}})
            publish(directory,[national])
            self.assertEqual(len(source.query(*box)),1)
            self.assertEqual(source.query(*box)[0]['service_exit']['label']['ref'],'2')
            old=source.version()
            manifest=directory/'manifest.json'
            previous=manifest.stat()
            publish(directory,[])
            # Same-sized atomic replacements can retain the same Windows mtime.
            os.utime(manifest,ns=(previous.st_atime_ns,previous.st_mtime_ns))
            self.assertEqual(manifest.stat().st_size,previous.st_size)
            self.assertNotEqual(old,source.version())
            self.assertEqual(source.query(*box),[])

    def test_corrupt_or_missing_cell_keeps_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'national'
            obj=dict(id='service:1',lon=-84.9,lat=41.7,exit={})
            local=Path(tmp)/'local.json'
            local.write_text(json.dumps(dict(format=1,objects=[obj])))
            entry=publish(directory,[obj])
            (directory/entry['path']).write_bytes(b'bad')
            source=servicesigns.Source(local,directory)
            with self.assertLogs('earthd.servicesigns',level='WARNING'):
                self.assertEqual(len(source.query(-85,41.5,-84.5,42)),1)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry=publish(tmp,[])
            entry['path']='../outside.gz'
            coverage.atomic_json(Path(tmp)/'manifest.json',dict(format=1,cell_degrees=.5,built='t',cells={'-170_83':entry}))
            source=coverage.Source(tmp)
            with self.assertRaises(ValueError): source.query(-85,41.5,-84.5,42)

    def test_unrelated_build_progress_does_not_refresh_current_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            publish(tmp,[])
            source=coverage.Source(tmp)
            source.query(-84.99,41.6,-84.8,41.8)
            old=source.version()
            path=Path(tmp)/'manifest.json'
            data=json.loads(path.read_text())
            data['cells']['-210_82']=dict(path='elsewhere.gz',sha256='other',objects=0)
            coverage.atomic_json(path,data)
            self.assertEqual(old,source.version())

    def test_bulk_pbf_import_build_and_resume(self):
        try: import osmium
        except ImportError: self.skipTest('bulk builder requires pyosmium')
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp)
            rows=['<osm version="0.6">']
            data=osm()
            # The business crosses a half-degree cell boundary from its exit.
            for e in data['elements']:
                if e['type']=='node': e['lon']+=.499
            for e in data['elements']:
                tags=''.join(f'<tag k={quoteattr(k)} v={quoteattr(v)}/>' for k,v in e['tags'].items())
                if e['type']=='node':
                    rows.append(f'<node id="{e["id"]}" lat="{e["lat"]}" lon="{e["lon"]}">{tags}</node>')
                else:
                    refs=''.join(f'<nd ref="{n}"/>' for n in e['nodes'])
                    rows.append(f'<way id="{e["id"]}">{refs}{tags}</way>')
            rows.append('</osm>')
            xml=tmp/'fixture.osm'
            xml.write_text(''.join(rows))
            pbf=tmp/'fixture.osm.pbf'
            with osmium.SimpleWriter(str(pbf)) as writer:
                for obj in osmium.FileProcessor(str(xml)): writer.add(obj)
            db=builder.connection(tmp/'network.sqlite')
            try:
                builder.import_pbf(pbf,db,tmp,'fixture')
                builder.build_cells(db,tmp/'coverage',tmp,'fixture',builder.index.POLICY)
                manifest=json.loads((tmp/'coverage/manifest.json').read_text())
                self.assertTrue(manifest['complete'])
                self.assertEqual(sum(e['objects'] for e in manifest['cells'].values()),1)
                source=coverage.Source(tmp/'coverage')
                objects,covered=source.query(-84.6,41.6,-84.4,41.8)
                self.assertEqual(len(objects),1)
                self.assertEqual(objects[0]['exit']['services'][0]['name'],'Real Cafe')
                before=(tmp/'coverage/manifest.json').read_bytes()
                self.assertNotEqual(coverage.cell(objects[0]['lon'],objects[0]['lat']),
                                    coverage.cell(next(e for e in data['elements'] if e['id']==6 and e['type']=='node')['lon'],41.7))
                builder.import_pbf(pbf,db,tmp,'fixture')
                builder.build_cells(db,tmp/'coverage',tmp,'fixture',builder.index.POLICY)
                self.assertEqual(before,(tmp/'coverage/manifest.json').read_bytes())
            finally: db.close()


if __name__=='__main__': unittest.main()
