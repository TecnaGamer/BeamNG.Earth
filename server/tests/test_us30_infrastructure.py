"""US-30 roadside classification, using representative and captured geographic inputs."""
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import furniture
import geodesy
import mapgeom
import roads
from test_roads import FRAME, features, rules


def captured(case):
    ways = case['ways']
    lengths = [len(w['lonlat']) for w in ways]
    n = len(ways)
    attr_names = {key for w in ways for key in w['attrs']}
    attrs = {key: np.array([w['attrs'].get(key) for w in ways], dtype=object) for key in attr_names}
    tables = {}
    for name in {key for w in ways for key in w['rules']}:
        entries = [(i, value, start, end) for i, w in enumerate(ways)
                   for value, start, end in w['rules'].get(name, [])]
        numeric = name in ('width', 'level') or all(isinstance(e[1], (int, float)) for e in entries)
        tables[name] = mapgeom.RuleTable(np.array([e[0] for e in entries], np.int32),
            np.array([e[1] for e in entries], np.float32 if numeric else '<U32'),
            np.array([e[2] for e in entries], np.float32), np.array([e[3] for e in entries], np.float32))
    f = mapgeom.Features('roads', np.array([w['id'] for w in ways]), np.full(n, mapgeom.LINE, np.uint8),
                         np.arange(n+1, dtype=np.int64), np.arange(n+1, dtype=np.int64),
                         np.r_[0, np.cumsum(lengths)].astype(np.int64),
                         np.concatenate([w['lonlat'] for w in ways]), attrs=attrs, rules=tables)
    def cover(la, lo):
        return np.array([case['cover'][f'{round(float(a)*1e9)},{round(float(b)*1e9)}']
                         for a, b in zip(la, lo)], bool)
    return f, cover


class DividedPrimaryTest(unittest.TestCase):
    def corridor(self, developed=False, sidewalks=None, lanes=2, width=None):
        f = features([[(-300,50),(900,50)],[(-300,80),(900,80)]], ['primary']*2)
        f.attrs['name'] = np.array(['US Highway 30']*2)
        f.attrs['osm_lanes'] = np.array([lanes]*2)
        # (one-way carriageways, as US-30's are mapped: a two-way way with lanes is not paired as a divided road's
        # carriageway, roads.dual_carriageways: Portland's Rosa Parks Way)
        f.rules['oneway'] = rules([(0, 1.0), (1, 1.0)], 'float')
        if width is not None:
            f.attrs['osm_width'] = np.array([width]*2)
        if sidewalks is not None:
            f.attrs['osm_sidewalk'] = np.array([sidewalks]*2)
        def cover(la, lo):
            _, y, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), 0.)
            return np.ones(np.shape(y), bool) if developed else ((abs(y-50)<6)|(abs(y-80)<6))
        return f, cover

    def test_parallel_pavement_is_rural_with_two_lanes_and_edge_strips(self):
        f, cover = self.corridor()
        for spec in roads.road_specs(f, cover):
            self.assertFalse(spec.urban)
            self.assertTrue(spec.oneway)
            self.assertEqual(spec.lanes, 2)
            self.assertAlmostEqual(spec.width, 8.6)
            self.assertGreater(roads.edge_margin(spec), 0)
            self.assertFalse(roads.has_sidewalks(spec))

    def test_rural_tile_has_no_inferred_sidewalks_or_lamps(self):
        f, cover = self.corridor()
        walks, props = [], []
        roads.build_tile(FRAME,0,0,[f],lambda la,lo:np.full(np.shape(la),10.),cover,
                         walks=walks,props=props,lamp_region='na:sparse')
        self.assertEqual(walks, [])
        self.assertFalse(any(p[0] in (furniture.G_LAMP,furniture.G_POLELAMP) for p in props))

    def test_developed_divided_arterial_keeps_sidewalks(self):
        f, cover = self.corridor(developed=True)
        self.assertTrue(all(s.urban and roads.has_sidewalks(s) for s in roads.road_specs(f,cover)))

    def test_explicit_rural_sidewalk_override_is_preserved(self):
        f, cover = self.corridor(sidewalks='right')
        self.assertTrue(all(not s.urban and roads.has_sidewalks(s) for s in roads.road_specs(f,cover)))

    def test_pairing_keeps_mapped_lanes(self):
        f, cover = self.corridor(lanes=3)
        self.assertTrue(all(s.lanes==3 and abs(s.width-11.9)<1e-6 for s in roads.road_specs(f,cover)))

    def test_pairing_keeps_mapped_width(self):
        f, cover = self.corridor(width=13.)
        self.assertTrue(all(s.oneway and s.width==13. for s in roads.road_specs(f,cover)))

    def test_undivided_town_main_street_keeps_existing_inference(self):
        f = features([[(-300,50),(900,50)]], ['primary'])
        def cover(la, lo):
            _, y, _ = FRAME.geodetic_to_enu(np.asarray(la),np.asarray(lo),0.)
            return (abs(y-50)<6)|(abs(y-80)<6)
        spec = roads.road_specs(f,cover)[0]
        self.assertTrue(spec.urban)
        self.assertTrue(roads.has_sidewalks(spec))


class MedianConnectorTest(unittest.TestCase):
    def connector(self, developed=False, sidewalk=None):
        f = features([[(-500,0),(0,0)],[(-500,30),(0,30)],[(0,0),(0,30)]],['primary']*3)
        f.attrs['name'] = np.array(['US Highway 30','US Highway 30',''])
        f.attrs['osm_lanes'] = np.array([2.,2.,2.])
        f.rules['subclass'] = rules([(2,'link')])
        f.rules['oneway'] = rules([(0, 1.0), (1, 1.0)], 'float')     # (one-way carriageways, as US-30's)
        if sidewalk:
            f.attrs['osm_sidewalk'] = np.array(['','',sidewalk])
        def cover(la,lo):
            _, y, _ = FRAME.geodetic_to_enu(np.asarray(la),np.asarray(lo),0.)
            return np.ones(np.shape(y),bool) if developed else ((abs(y)<6)|(abs(y-30)<6))
        return f, cover

    def test_short_median_link_inherits_connected_rural_context(self):
        f,cover = self.connector()
        spec = roads.road_specs(f,cover)[2]
        self.assertFalse(spec.urban)
        self.assertFalse(roads.has_sidewalks(spec))
        self.assertTrue(spec.link)
        self.assertFalse(spec.oneway)
        self.assertEqual(spec.lanes,2)

    def test_mapped_sidewalk_on_link_is_preserved(self):
        f,cover = self.connector(sidewalk='both')
        self.assertTrue(roads.has_sidewalks(roads.road_specs(f,cover)[2]))

    def test_urban_median_link_keeps_sidewalks(self):
        f,cover = self.connector(developed=True)
        self.assertTrue(roads.has_sidewalks(roads.road_specs(f,cover)[2]))

    def test_nearby_unconnected_link_does_not_inherit_highway_context(self):
        f,cover = self.connector()
        f.lonlat[-2:,0] += .001
        self.assertTrue(roads.road_specs(f,cover)[2].urban)


class RoadsideParapetTest(unittest.TestCase):
    def walls(self, divided, ground):
        """ground: a height (flat), or 'cut': the grading's cutting, flat to the core's edge then 3 m up within a
        metre (test_roads.RetainingWallTest: a natural bank gets no wall since 2026-10-29)."""
        import grading
        spec = roads.Spec(8.6,'paved',False,'primary',True,median=30. if divided else None)
        p = np.c_[np.arange(0.,121.,4.),np.zeros(31),np.full(31,20.)]
        if ground == 'cut':
            edge = max(spec.width, roads.carriageway('primary', 'paved', True, None, True)) / 2 \
                + grading.shoulder(spec) + grading.WALL_FLAT_M

            def elev(la, lo):
                _, n, _ = FRAME.geodetic_to_enu(np.asarray(la), np.asarray(lo), np.zeros(np.shape(la)))
                return 20.0 + np.clip((np.abs(n) - edge) / 0.8, 0.0, 1.0) * 3.0
        else:
            def elev(la, lo):
                return np.full(np.shape(la), ground)
        return roads.retaining_walls(FRAME,[('us30',spec,p,0.)],elev,(-10.,-30.,130.,30.))

    def test_divided_highway_embankment_is_not_inferred_stone_parapet(self):
        self.assertEqual(self.walls(True,16.),[])

    def test_deep_cut_retaining_wall_is_preserved(self):
        walls = self.walls(True,'cut')
        self.assertTrue(walls)
        self.assertTrue(all(not w.get('parapet') for w in walls))
        self.assertEqual(len(walls), len(self.walls(False,'cut')), 'the same cut walls as an undivided road')

    def test_undivided_mountain_drop_protection_is_preserved(self):
        self.assertTrue(any(w.get('parapet') for w in self.walls(False,16.)))


class ActualUS30Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=json.loads((Path(__file__).parent/'fixtures/us30_rural_inputs.json').read_text())['cases']

    def test_actual_northern_us30_carriageways_keep_two_lanes_without_sidewalks(self):
        f,cover=captured(self.cases[0])
        specs=roads.road_specs(f,cover)
        self.assertEqual(len(specs),2)
        for spec in specs:
            self.assertFalse(spec.urban)
            self.assertFalse(roads.has_sidewalks(spec))
            self.assertTrue(spec.oneway)
            self.assertEqual(spec.lanes,2)
            self.assertAlmostEqual(spec.width,8.6)

    def test_actual_median_crossing_has_no_isolated_sidewalk_slabs(self):
        case=self.cases[1];f,cover=captured(case)
        i=f.index('64f74514-61ec-4169-86b3-716a0cb1dbd4')
        spec=roads.road_specs(f,cover)[i]
        self.assertFalse(spec.urban)
        self.assertTrue(spec.link)
        self.assertEqual(len(roads.ground_parts(f,i)),1)
        walks=[]
        roads.build_tile(geodesy.EnuFrame(*case['target']),0,-1,[f],
                         lambda la,lo:np.full(np.shape(la),10.),cover,walks=walks)
        self.assertFalse(any(w.get('fid')==str(f.ids[i]) for w in walks))


if __name__=='__main__':
    unittest.main()
