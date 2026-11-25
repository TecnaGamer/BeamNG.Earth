"""Offline regression suite; no NVDB or BeamNG runtime required."""
from pathlib import Path
from unittest import TestCase,main,mock
import sys, tempfile, json
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent if HERE.name=='tests' else HERE))
from norway_signs_v073 import coverage,catalog
import norway_nvdb_destinations_v07 as d
from norway_nvdb_signs_v06 import BASE


def sample(code='713.H90', facing='Parallelt mot veg', text='Molde', color='Gul'):
    return {'id':1999777,'metadata':{'type':{'id':96}},'geometri':{'wkt':'POINT Z (62.89390647 7.6787657 2.504)','srid':4326,'egengeometri':True},
            'lokasjon':{'geometri':{'wkt':'POINT Z (62.89387043 7.67871026 2.504)','srid':4326}},
            'egenskaper':[{'id':5530,'verdi':code+' - Skilt'},{'id':1910,'verdi':text},{'id':1294,'verdi':color},{'id':1894,'verdi':facing}]}


class ModularTests(TestCase):
    def test_registry_independent(self):
        self.assertEqual(catalog.family('713.H'),'arrow')
        self.assertEqual(catalog.family('723.15'),'route')
        self.assertEqual(catalog.family('362.50'),None)
        self.assertIn('902',catalog.PLANNED)

    def test_valid_arrow_variants(self):
        for code in ['713.H','713.V','713.H90','713.V45','713.H135']:
            self.assertIn(catalog.arrow_side(code),'HV')
            self.assertIsNotNone(d.normalize(sample(code)))
        self.assertIsNone(catalog.arrow_side('713.X'))

    def test_native_compatibility(self):
        row=d.normalize(sample('713.H90'))
        self.assertEqual(row['kind'],'713')
        self.assertEqual(row['code'],'713.H90')
        self.assertEqual(catalog.arrow_side(row['code']),'H')

    def test_two_sided_shields_are_eligible(self):
        row=d.normalize(sample('723.11',facing='Tosidig',text='E39',color='Grønn'))
        self.assertIsNotNone(row)
        self.assertEqual(row['facing'],'tosidig')
        self.assertIsNone(d.normalize(sample('713.H',facing='Tosidig')))

    def test_two_sided_is_textured_both_sides(self):
        import numpy as np
        row=d.normalize(sample('723.11',facing='Tosidig',text='E39',color='Grønn'))
        p,t,uv,mi=d._panel_mesh(row,np.array([0.,0.]),0.,np.array([1.,0.]),1,[0.,0.],.7,.5,1.8,posts=False)
        self.assertEqual(sum(mi==1),4)
        self.assertTrue(any(uv[4:8,0]>0))

    def test_no_unproven_two_sided_direction(self):
        self.assertIsNone(d.normalize(sample('727.1',facing='Tosidig')))

    def test_url_pinned_to_nvdb_with_relations(self):
        import norway_nvdb_signs_v06 as base
        u=coverage.build_url(base,(5240,320),'abc')
        self.assertTrue(u.startswith(BASE+'?'))
        self.assertIn('relasjoner',u)
        self.assertIn('start=abc',u)

    def test_paginated_report_includes_reasons(self):
        import norway_nvdb_signs_v06 as base
        obj=sample('713.H')
        unsupported=sample('362.50');unsupported['id']=1999778
        pages=iter([{'objekter':[obj,unsupported],'metadata':{'neste':{'start':'next'}}},
                    {'objekter':[],'metadata':{}}])
        records,report=coverage.scan(base,(5240,320),d.normalize,lambda u:next(pages))
        self.assertEqual(len(records),1)
        self.assertEqual(report['statistics']['unsupported_family'],1)
        self.assertEqual(report['statistics']['accepted'],1)
        self.assertEqual(report['statistics']['pages'],2)

    def test_unknown_color_still_skipped(self):
        self.assertIsNone(d.normalize(sample('713.H',color='Ukjent')))

    def test_reject_unsupported_diagrams(self):
        self.assertIsNone(d.normalize(sample('703.1')))

    def test_source_code_keeps_speed_signs_separate(self):
        self.assertIsNone(d._kind('362.50'))


if __name__=='__main__':main()
