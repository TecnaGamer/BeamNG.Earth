import csv
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import nlrvv
import dmath
import nlrvvdata as data
import build_nl_rvv
import signstream


def record(**kw):
    return dict(id='a', rvvCode='B6', blackCode='', zoneCode='', textSigns='', status='PLACED',
                placement='L', latitude='52.0', longitude='5.0', bearing='90', **kw)


class PublicData(unittest.TestCase):
    def test_complete_text_plates_keep_their_meaning_and_order(self):
        row = record();row['textSigns']='UIT(uitgezonderd bestemmingsverkeer);VOOR(100 m)'
        got, reason = data.normalize(row)
        self.assertIsNone(reason)
        self.assertEqual(got['plates'], [dict(kind='UIT', text='uitgezonderd bestemmingsverkeer'),
                                         dict(kind='VOOR', text='100 m')])
        row['textSigns']=json.dumps([dict(type='VRIJ', text='ma t/m za 06.00-18.00 h')])
        self.assertEqual(data.normalize(row)[0]['plates'][0]['text'], 'ma t/m za 06.00-18.00 h')

    def test_incomplete_or_symbolic_plates_never_become_unqualified_signs(self):
        for value in ('VRIJ()', 'UIT(uitgezonderd)', 'RICH()', 'UIT([[fietser]])',
                      'VRIJ(onleesbaar)', 'VRIJ(goed);VRIJ()', 'VRIJ(abc\x00def)',
                      json.dumps([dict(type='VRIJ', text='maandag', openingHours='Mo 06:00-18:00')])):
            row=record();row['textSigns']=value
            self.assertEqual(data.normalize(row), (None, 'qualifier_not_supported'), value)

    def test_different_text_qualifiers_are_not_duplicate_records(self):
        a=data.normalize(record())[0]
        b=dict(a,id='b',plates=[dict(kind='VOOR',text='100 m')])
        c=dict(a,id='c',plates=[dict(kind='VOOR',text='200 m')])
        self.assertEqual(len(data.deduplicate([a,b,c], maths=dmath)),3)

    def test_additional_shared_atlas_roles(self):
        for code,role in [('C3','info.oneway'),('D3','mand.pass_either'),('F8','reg.end_restrictions'),
                          ('L2','info.crossing_right')]:
            row=record();row['rvvCode']=code
            self.assertEqual(data.normalize(row)[0]['role'],role)

    def test_only_attributes_escape_normalization(self):
        row = record(imageUrl='https://example.invalid/photo', externalId='private')
        got, reason = data.normalize(row)
        self.assertIsNone(reason)
        self.assertEqual(got['role'], 'prio.give_way')
        self.assertEqual(got['bearing'], 90)
        self.assertNotIn('imageUrl', got)
        self.assertNotIn('externalId', got)

    def test_semantics_not_guessed(self):
        for field, value in [('status', 'REMOVED'), ('placement', 'B'), ('zoneCode', 'ZB'),
                             ('textSigns', '[{"text":"uitgezonderd fietsers"}]'), ('bearing', 'nan'),
                             ('blackCode', 'unknown'), ('rvvCode', 'C22a')]:
            row = record(); row[field] = value
            got, reason = data.normalize(row)
            self.assertIsNone(got, (field, value))
            self.assertTrue(reason)
        row = record(); row.update(rvvCode='D2', blackCode='RO')
        self.assertEqual(data.normalize(row)[0]['role'], 'mand.keep_right')
        row.update(rvvCode='A1', blackCode='50')
        self.assertEqual(data.normalize(row)[0]['speed'], 50)

    def test_near_copies_collapse_but_opposed_faces_and_meanings_survive(self):
        a = data.normalize(record())[0]
        b = dict(a, id='b', lon=a['lon'] + .000001)
        c = dict(b, id='c', bearing=270)
        d = dict(b, id='d', role='prio.stop')
        got = data.deduplicate([d, b, c, a], maths=dmath)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0]['source_ids'], ['a', 'b'])
        with self.assertRaises(ValueError):
            data.deduplicate([a, a], maths=dmath)

    def test_public_producer_can_run_without_private_store(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / 'source.csv', Path(temp) / 'nl-rvv'
            row = record(imageUrl='NEVER_EXPORT_THIS')
            with source.open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader(); writer.writerow(row)
            manifest = build_nl_rvv.build(source, target)
            self.assertEqual(manifest['records'], 1)
            self.assertEqual(manifest['license'], 'CC0-1.0')
            self.assertEqual(len(manifest['source_sha256']), 64)
            x, y = data.tile(52, 5, maths=dmath)
            raw = gzip.decompress((target / str(x) / f'{y}.json.gz').read_bytes())
            self.assertNotIn(b'NEVER_EXPORT_THIS', raw)
            self.assertEqual(json.loads(raw)['records'][0]['id'], 'a')

    def test_unpublished_and_disabled_dataset_cannot_break_existing_signs(self):
        source = nlrvv.Source(None, None)
        with patch.object(nlrvv.roadsigns, 'NL_ON', False):
            self.assertEqual(source.query((4, 51, 5, 52)), [])
        with patch.object(nlrvv.roadsigns, 'NL_ON', True), patch.object(source, 'version', return_value=None):
            self.assertEqual(source.query((4, 51, 5, 52)), [])
        with patch.object(nlrvv, 'build_tile', side_effect=ValueError('bad tile')), self.assertLogs('earthd.signs'):
            self.assertEqual(signstream._ndw(None, None, 0, 0, None, None, []), (None, []))

    def test_published_public_tiles_use_normal_fetch_and_persistent_cache(self):
        row = data.normalize(record())[0]
        x, y = data.tile(row['lat'], row['lon'], maths=dmath)

        class Fetcher:
            def __init__(self):
                self.urls = []

            def get(self, url, **kw):
                self.urls.append(url)
                if url.endswith('/manifest.json'):
                    return SimpleNamespace(body=json.dumps({'built': 'test-build', 'format': data.FORMAT, 'zoom': 12}).encode())
                if url.endswith(f'/{x}/{y}.json.gz'):
                    return SimpleNamespace(body=gzip.compress(json.dumps({'format': data.FORMAT, 'records': [row]}).encode()))
                raise AssertionError('unexpected request: ' + url)

        with tempfile.TemporaryDirectory() as folder, patch.object(nlrvv.roadsigns, 'NL_ON', True):
            fetcher = Fetcher()
            source = nlrvv.Source(folder, fetcher)
            bounds = (4.99999, 51.99999, 5.00001, 52.00001)
            self.assertEqual(source.query(bounds), [row])
            self.assertEqual(source.query(bounds), [row])
            self.assertEqual(len(fetcher.urls), 2)
            reloaded = nlrvv.Source(folder, Fetcher())
            self.assertEqual(reloaded.query(bounds), [row])
            self.assertEqual(reloaded.fetcher.urls, [])

    def test_only_actual_same_meaning_same_facing_claims_replace_inferred(self):
        role = nlrvv.roadsigns.tables()[1]['rules'].index('prio.give_way')
        rows = np.zeros((4, 11))
        rows[:, 4] = role
        rows[:, 2] = 1
        rows[1, 2] = -1
        rows[2, 0] = 20
        rows[3, 4] = nlrvv.roadsigns.tables()[1]['rules'].index('prio.stop')
        claim = dict(role='prio.give_way', speed=None, point=np.array([0., 0.]), travel=np.array([1., 0.]))
        got = nlrvv.keep_inferred(rows, [claim])
        np.testing.assert_array_equal(got, rows[1:])
        np.testing.assert_array_equal(nlrvv.keep_inferred(rows, []), rows)
        qualified=dict(claim,plates=[dict(kind='UIT',text='uitgezonderd bestemmingsverkeer')])
        np.testing.assert_array_equal(nlrvv.keep_inferred(rows,[qualified]),rows)

    def test_text_panels_share_the_batch_and_remain_below_their_main_sign(self):
        if not hasattr(nlrvv.fast._ef,'road_sign_mesh'):
            self.skipTest('native renderer unavailable')
        row=dict(point=np.array([10.,20.]),travel=np.array([1.,0.]),z=0.,role='prio.give_way',
                 plates=[dict(kind='UIT',text='uitgezonderd bestemmingsverkeer'),dict(kind='VOOR',text='100 m')])
        layout=nlrvv.plate_layout(row,.75)
        self.assertEqual(' '.join(layout[0]['lines']),'uitgezonderd bestemmingsverkeer')
        p,t,uv,materials,mi=nlrvv.render([row],(0.,0.,100.,100.))
        self.assertTrue(np.isfinite(p).all())
        self.assertIn('bne_guide_letters_black',materials)
        glyph=materials.index('bne_guide_letters_black')
        letters=p[t[mi==glyph].ravel()]
        lift=sum(v['height']+.08 for v in layout)
        self.assertTrue((letters[:,2]>=2.2).all())
        self.assertTrue((letters[:,2]<2.2+lift).all())
        bad=dict(row,plates=[dict(kind='VRIJ',text='[[fietser]]')])
        self.assertIsNone(nlrvv.render([bad],(0.,0.,100.,100.)))

    def test_real_native_batch_uses_our_shared_assets(self):
        if not hasattr(nlrvv.fast._ef, 'road_sign_mesh'):
            self.skipTest('native renderer unavailable')
        records = [dict(point=np.array([10., 20.]), travel=np.array([1., 0.]), z=0., role='prio.give_way'),
                   dict(point=np.array([20., 20.]), travel=np.array([1., 0.]), z=0., role=None, speed=50)]
        p, t, uv, materials, mi = nlrvv.render(records, (0., 0., 100., 100.))
        self.assertTrue(np.isfinite(p).all())
        self.assertGreater(len(t), 0)
        self.assertEqual(len(mi), len(t))
        self.assertEqual(len(materials), len(set(materials)))
        self.assertIn('bne_signs_vienna_w', materials)


if __name__ == '__main__':
    unittest.main()
